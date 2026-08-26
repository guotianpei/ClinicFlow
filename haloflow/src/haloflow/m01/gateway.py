"""The sole runtime transaction path to tenant operational data."""

import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from psycopg import AsyncConnection, sql

from haloflow.m01.context import TenantContext
from haloflow.m01.errors import (
    NestedTenantTransaction,
    RegistryInconsistent,
    RepositoryHandleExpired,
    RoutingMismatch,
)
from haloflow.m01.pool import TenantPool
from haloflow.m01.resolver import SCHEMA_KEY_PATTERN, LifecycleState

T = TypeVar("T")
Params = Sequence[Any] | dict[str, Any] | None

_ACTIVE_TENANT_TRANSACTION: ContextVar[bool] = ContextVar(
    "haloflow_m01_active_tenant_transaction", default=False
)
_PROHIBITED_REPOSITORY_SQL = re.compile(
    r"(?ix)(?:\btenant_[a-z0-9]+\s*\.|\bpublic\s*\.|\bshared\s*\.|"
    r"\bset\s+(?:local\s+)?search_path\b|\bprepare\s+)"
)


@dataclass(frozen=True, slots=True)
class TransactionOptions:
    read_only: bool = False
    timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if self.timeout_ms <= 0 or self.lock_timeout_ms <= 0:
            raise ValueError("transaction timeouts must be positive")


class TenantRepositoryHandle:
    """Transaction-scoped query capability without raw connection access."""

    __slots__ = ("__active", "__connection")

    def __init__(self, connection: AsyncConnection) -> None:
        self.__connection = connection
        self.__active = True

    async def execute(self, query: str, params: Params = None) -> int:
        self._assert_active_and_safe(query)
        cursor = await self.__connection.execute(query, params)
        return cursor.rowcount

    async def fetch_one(self, query: str, params: Params = None) -> tuple[Any, ...] | None:
        self._assert_active_and_safe(query)
        cursor = await self.__connection.execute(query, params)
        return await cursor.fetchone()

    async def fetch_all(self, query: str, params: Params = None) -> list[tuple[Any, ...]]:
        self._assert_active_and_safe(query)
        cursor = await self.__connection.execute(query, params)
        return await cursor.fetchall()

    def invalidate(self) -> None:
        self.__active = False

    def _assert_active_and_safe(self, query: str) -> None:
        if not self.__active:
            raise RepositoryHandleExpired()
        if _PROHIBITED_REPOSITORY_SQL.search(query):
            raise ValueError("qualified or session-scoped SQL is prohibited")


class TenantTransactionGateway:
    """Binds exactly one validated tenant context to one transaction."""

    def __init__(
        self,
        pool: TenantPool,
        *,
        supported_schema_versions: Collection[int] = range(1, 2),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._supported_schema_versions = frozenset(supported_schema_versions)
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
            context.assert_usable(clock=self._clock)
        timeout_ms = min(transaction_options.timeout_ms, remaining_ms)

        token = _ACTIVE_TENANT_TRANSACTION.set(True)
        try:
            async with (
                self._pool.connection_for_gateway() as connection,
                connection.transaction(),
            ):
                if transaction_options.read_only:
                    await connection.execute("SET TRANSACTION READ ONLY")
                await self._set_local_value(connection, "statement_timeout", f"{timeout_ms}ms")
                await self._set_local_value(
                    connection,
                    "lock_timeout",
                    f"{min(transaction_options.lock_timeout_ms, timeout_ms)}ms",
                )
                await self._set_local_value(connection, "transaction_timeout", f"{timeout_ms}ms")
                await self._set_local_value(
                    connection,
                    "application_name",
                    self._bounded_correlation(context.operation_id),
                )
                await self._set_local_value(connection, "app.tenant_id", context.tenant_id)

                await self._revalidate_registry(connection, context)
                await connection.execute(
                    sql.SQL("SET LOCAL search_path TO pg_catalog, {}, pg_temp").format(
                        sql.Identifier(context.schema_key)
                    )
                )
                await self._verify_effective_path(connection, context.schema_key)

                context.assert_usable(clock=self._clock)
                handle = TenantRepositoryHandle(connection)
                try:
                    return await callback(handle)
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

    async def _set_local_value(self, connection: AsyncConnection, setting: str, value: str) -> None:
        await connection.execute(
            "SELECT set_config(%s, %s, true)",
            (setting, value),
        )

    def _bounded_correlation(self, operation_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_.:-]", "_", operation_id)
        return f"haloflow:{sanitized[:96]}"
