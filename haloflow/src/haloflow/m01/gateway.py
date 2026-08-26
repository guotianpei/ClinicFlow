"""The sole runtime transaction path to tenant operational data."""

from collections.abc import Awaitable, Callable, Collection, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from psycopg import AsyncConnection, sql

from haloflow.m01.context import TenantContext
from haloflow.m01.errors import (
    CapabilityDenied,
    ContextExpired,
    NestedTenantTransaction,
    RegistryInconsistent,
    RepositoryHandleExpired,
    RoutingMismatch,
)
from haloflow.m01.pool import TenantPool
from haloflow.m01.resolver import SCHEMA_KEY_PATTERN, LifecycleState
from haloflow.m01.statements import (
    StatementMode,
    TenantStatement,
    TenantStatementCatalog,
    _build_statement_catalog,
)

T = TypeVar("T")
Params = Sequence[Any] | dict[str, Any] | None

_ACTIVE_TENANT_TRANSACTION: ContextVar[bool] = ContextVar(
    "haloflow_m01_active_tenant_transaction", default=False
)


@dataclass(frozen=True, slots=True)
class TransactionOptions:
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000
    transaction_timeout_margin_ms: int = 2_000

    def __post_init__(self) -> None:
        if (
            self.statement_timeout_ms <= 0
            or self.lock_timeout_ms <= 0
            or self.transaction_timeout_margin_ms <= 0
        ):
            raise ValueError("transaction timeouts must be positive")


class TenantRepositoryHandle:
    """Transaction-scoped access to fixed M01-owned statements."""

    __slots__ = ("__active", "__allow_writes", "__catalog", "__connection")

    def __init__(
        self,
        connection: AsyncConnection,
        catalog: TenantStatementCatalog,
        *,
        allow_writes: bool,
    ) -> None:
        self.__connection: AsyncConnection | None = connection
        self.__catalog: TenantStatementCatalog | None = catalog
        self.__allow_writes = allow_writes
        self.__active = True

    async def execute(self, statement_key: str, params: Params = None) -> int:
        statement = self._resolve_statement(statement_key)
        cursor = await self._connection().execute(statement.query, params)
        return cursor.rowcount

    async def fetch_one(self, statement_key: str, params: Params = None) -> tuple[Any, ...] | None:
        statement = self._resolve_statement(statement_key)
        cursor = await self._connection().execute(statement.query, params)
        return await cursor.fetchone()

    async def fetch_all(self, statement_key: str, params: Params = None) -> list[tuple[Any, ...]]:
        statement = self._resolve_statement(statement_key)
        cursor = await self._connection().execute(statement.query, params)
        return await cursor.fetchall()

    def invalidate(self) -> None:
        self.__active = False
        self.__connection = None
        self.__catalog = None

    def _connection(self) -> AsyncConnection:
        if not self.__active or self.__connection is None:
            raise RepositoryHandleExpired()
        return self.__connection

    def _resolve_statement(self, statement_key: str) -> TenantStatement:
        self._connection()
        if self.__catalog is None:
            raise RepositoryHandleExpired()
        statement = self.__catalog.resolve(statement_key)
        if statement.mode is StatementMode.WRITE and not self.__allow_writes:
            raise CapabilityDenied()
        return statement


class TenantTransactionGateway:
    """Binds exactly one validated tenant context to one transaction."""

    def __init__(
        self,
        pool: TenantPool,
        *,
        supported_schema_versions: Collection[int] = range(1, 2),
        statement_catalog: TenantStatementCatalog | None = None,
        write_capabilities: Collection[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._supported_schema_versions = frozenset(supported_schema_versions)
        self._statement_catalog = statement_catalog or _build_statement_catalog({})
        self._write_capabilities = frozenset(write_capabilities)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def with_tenant_transaction(
        self,
        context: TenantContext,
        callback: Callable[[TenantRepositoryHandle], Awaitable[T]],
        *,
        options: TransactionOptions | None = None,
    ) -> T:
        if _ACTIVE_TENANT_TRANSACTION.get():
            raise NestedTenantTransaction()

        context.assert_usable(clock=self._clock)
        transaction_options = options or TransactionOptions()
        remaining_ms = int((context.expires_at - self._clock()).total_seconds() * 1000)
        if remaining_ms <= 0:
            raise ContextExpired()

        statement_timeout_ms = min(
            transaction_options.statement_timeout_ms,
            max(1, remaining_ms - 1),
        )
        transaction_timeout_ms = min(
            remaining_ms,
            statement_timeout_ms + transaction_options.transaction_timeout_margin_ms,
        )
        lock_timeout_ms = min(
            transaction_options.lock_timeout_ms,
            statement_timeout_ms,
        )
        allow_writes = bool(context.capabilities & self._write_capabilities)

        token = _ACTIVE_TENANT_TRANSACTION.set(True)
        try:
            async with (
                self._pool._connection_for_gateway() as connection,
                connection.transaction(),
            ):
                if not allow_writes:
                    await connection.execute("SET TRANSACTION READ ONLY")
                await self._set_local_values(
                    connection,
                    context=context,
                    statement_timeout_ms=statement_timeout_ms,
                    lock_timeout_ms=lock_timeout_ms,
                    transaction_timeout_ms=transaction_timeout_ms,
                )

                await self._revalidate_registry(connection, context)
                await connection.execute(
                    sql.SQL("SET LOCAL search_path TO pg_catalog, {}, pg_temp").format(
                        sql.Identifier(context.schema_key)
                    )
                )
                await self._verify_effective_path(connection, context.schema_key)

                context.assert_usable(clock=self._clock)
                handle = TenantRepositoryHandle(
                    connection,
                    self._statement_catalog,
                    allow_writes=allow_writes,
                )
                try:
                    result = await callback(handle)
                    await self._verify_effective_path(connection, context.schema_key)
                    return result
                finally:
                    handle.invalidate()
        finally:
            _ACTIVE_TENANT_TRANSACTION.reset(token)

    async def _revalidate_registry(
        self, connection: AsyncConnection, context: TenantContext
    ) -> None:
        row = await (
            await connection.execute(
                """
                SELECT schema_key, lifecycle_state, schema_version
                FROM shared.tenants
                WHERE tenant_id = %s
                """,
                (context.tenant_id,),
            )
        ).fetchone()
        if row is None:
            raise RegistryInconsistent(reason_code="REGISTRY_ROW_MISSING")
        if (
            row[0] != context.schema_key
            or not SCHEMA_KEY_PATTERN.fullmatch(row[0])
            or row[1] != LifecycleState.ACTIVE.value
            or row[2] not in self._supported_schema_versions
        ):
            raise RegistryInconsistent(reason_code="REGISTRY_REVALIDATION_MISMATCH")

    async def _verify_effective_path(self, connection: AsyncConnection, schema_key: str) -> None:
        row = await (
            await connection.execute("SELECT current_schemas(true), pg_my_temp_schema()")
        ).fetchone()
        if row is None or row[0] != ["pg_catalog", schema_key] or row[1] != 0:
            raise RoutingMismatch()

    async def _set_local_values(
        self,
        connection: AsyncConnection,
        *,
        context: TenantContext,
        statement_timeout_ms: int,
        lock_timeout_ms: int,
        transaction_timeout_ms: int,
    ) -> None:
        await connection.execute(
            """
            SELECT
                set_config('statement_timeout', %s, true),
                set_config('lock_timeout', %s, true),
                set_config('transaction_timeout', %s, true),
                set_config('application_name', %s, true),
                set_config('app.tenant_id', %s, true)
            """,
            (
                f"{statement_timeout_ms}ms",
                f"{lock_timeout_ms}ms",
                f"{transaction_timeout_ms}ms",
                f"haloflow:{context.operation_id}",
                context.tenant_id,
            ),
        )
