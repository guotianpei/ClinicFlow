"""The sole runtime transaction path to tenant operational data."""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, TypeVar

from psycopg import AsyncConnection, sql

from haloflow.m01.activation import PROBE_SQL, ActivationPolicy
from haloflow.m01.context import TenantContext
from haloflow.m01.errors import (
    ActivationRefused,
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
    CompiledCatalog,
    StatementMode,
    TenantStatement,
    TenantStatementCatalog,
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
    """Transaction-scoped access to fixed M01-owned statements.

    L-1S S-C (D-S1): under `ActivationPolicy.ENFORCE`, the first activation-protected
    statement of the transaction is preceded by ONE probe (PROBE_SQL, t046). Membership,
    capability and write-mode checks stay first and synchronous; the probe is awaited only
    for an authorized protected statement. Success is cached for this handle only; a
    refusal poisons the handle (every later statement refuses with the same code) and the
    gateway raises it after the callback even if the callback caught it, so no earlier
    write commits. Concurrent first calls share one probe. CancelledError is never caught.
    """

    __slots__ = (
        "__activation",
        "__active",
        "__allow_writes",
        "__authorized",
        "__capabilities",
        "__catalog",
        "__connection",
        "__probe_lock",
        "__protected",
        "__refused",
    )

    def __init__(
        self,
        connection: AsyncConnection,
        catalog: TenantStatementCatalog,
        *,
        allow_writes: bool,
        capabilities: frozenset[str],
        activation_policy: ActivationPolicy = ActivationPolicy.NOT_ENFORCED,
        activation_protected: frozenset[str] = frozenset(),
    ) -> None:
        self.__connection: AsyncConnection | None = connection
        self.__catalog: TenantStatementCatalog | None = catalog
        self.__allow_writes = allow_writes
        self.__capabilities: frozenset[str] | None = capabilities
        self.__active = True
        self.__activation = activation_policy
        self.__protected = activation_protected
        self.__authorized = False
        self.__refused: str | None = None
        self.__probe_lock = asyncio.Lock()

    @property
    def activation_refused(self) -> str | None:
        return self.__refused

    async def execute(self, statement_key: str, params: Params = None) -> int:
        statement = await self._authorized_statement(statement_key)
        cursor = await self._connection().execute(statement.query, params)
        return cursor.rowcount

    async def fetch_one(self, statement_key: str, params: Params = None) -> tuple[Any, ...] | None:
        statement = await self._authorized_statement(statement_key)
        cursor = await self._connection().execute(statement.query, params)
        return await cursor.fetchone()

    async def fetch_all(self, statement_key: str, params: Params = None) -> list[tuple[Any, ...]]:
        statement = await self._authorized_statement(statement_key)
        cursor = await self._connection().execute(statement.query, params)
        return await cursor.fetchall()

    def invalidate(self) -> None:
        self.__active = False
        self.__connection = None
        self.__catalog = None
        self.__capabilities = None

    def _connection(self) -> AsyncConnection:
        if not self.__active or self.__connection is None:
            raise RepositoryHandleExpired()
        return self.__connection

    async def _authorized_statement(self, statement_key: str) -> TenantStatement:
        if self.__refused is not None:
            raise ActivationRefused(reason_code=self.__refused)
        statement = self._resolve_statement(statement_key)
        if (self.__activation is ActivationPolicy.ENFORCE
                and statement.key in self.__protected and not self.__authorized):
            await self._probe()
        return statement

    async def _probe(self) -> None:
        async with self.__probe_lock:
            if self.__refused is not None:
                raise ActivationRefused(reason_code=self.__refused)
            if self.__authorized:
                return
            try:
                cursor = await self._connection().execute(PROBE_SQL)
                rows = await cursor.fetchall()
            except Exception:  # noqa: BLE001 - CancelledError is BaseException: not caught
                self._refuse("M05_ACTIVATION_UNREADABLE")
            code = _probe_outcome(rows)
            if code is not None:
                self._refuse(code)
            self.__authorized = True

    def _refuse(self, code: str) -> NoReturn:
        self.__refused = code
        raise ActivationRefused(reason_code=code)

    def _resolve_statement(self, statement_key: str) -> TenantStatement:
        self._connection()
        if self.__catalog is None:
            raise RepositoryHandleExpired()
        statement = self.__catalog.resolve(statement_key)
        if self.__capabilities is None or statement.required_capability not in self.__capabilities:
            raise CapabilityDenied(reason_code="STATEMENT_CAPABILITY_DENIED")
        if statement.mode is StatementMode.WRITE and not self.__allow_writes:
            raise CapabilityDenied()
        return statement


def _probe_outcome(rows: Any) -> str | None:
    """None when active; otherwise the refusal code. Exact envelope, one row."""

    if type(rows) is not list or len(rows) != 1 or len(rows[0]) != 2:
        return "M05_ACTIVATION_UNREADABLE"
    value, isolation = rows[0]
    if isolation != "read committed":
        return "ACTIVATION_ISOLATION_UNSUPPORTED"
    if (not isinstance(value, dict) or set(value) != {"schema_version", "active"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["active"]) is not bool):
        return "M05_ACTIVATION_UNREADABLE"
    return None if value["active"] else "M05_NOT_ACTIVATED"


class TenantTransactionGateway:
    """Binds exactly one validated tenant context to one transaction."""

    def __init__(
        self,
        pool: TenantPool,
        catalog: CompiledCatalog,
        *,
        supported_schema_versions: Collection[int] = range(1, 2),
        clock: Callable[[], datetime] | None = None,
        activation_policy: ActivationPolicy = ActivationPolicy.NOT_ENFORCED,
    ) -> None:
        """Bind a pool to one composed, startup-frozen catalogue.

        `catalog` is required. The previous `statement_catalog=None` default
        silently produced an empty catalogue, so a misconfigured gateway failed
        with STATEMENT_NOT_REGISTERED at first use rather than at construction.
        Write capabilities are derived from the catalogue's WRITE statements and
        are not separately supplied, so the two cannot drift (B2.7, B2.9).
        """

        self._pool = pool
        self._supported_schema_versions = frozenset(supported_schema_versions)
        self._statement_catalog: TenantStatementCatalog = catalog.catalog
        self._write_capabilities = catalog.write_capabilities
        self._clock = clock or (lambda: datetime.now(UTC))
        if type(activation_policy) is not ActivationPolicy:
            raise TypeError("activation_policy must be an ActivationPolicy")
        self._activation_policy = activation_policy
        self._activation_protected = catalog.activation_protected

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
        timeout_margin_ms = transaction_options.transaction_timeout_margin_ms
        if remaining_ms <= timeout_margin_ms:
            raise ContextExpired(reason_code="CONTEXT_BUDGET_INSUFFICIENT")

        statement_timeout_ms = min(
            transaction_options.statement_timeout_ms,
            remaining_ms - timeout_margin_ms,
        )
        transaction_timeout_ms = statement_timeout_ms + timeout_margin_ms
        lock_timeout_ms = min(
            transaction_options.lock_timeout_ms,
            statement_timeout_ms,
        )
        # A multi-capability context holding any write capability makes the whole
        # transaction read-write; a transaction is read-only or it is not. Least
        # privilege for mixed flows therefore rests on the per-statement check in
        # TenantRepositoryHandle._resolve_statement, which stays exact.
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
                    capabilities=context.capabilities,
                    activation_policy=self._activation_policy,
                    activation_protected=self._activation_protected,
                )
                try:
                    result = await callback(handle)
                    # S-C pin 3: a refusal the callback caught still aborts the transaction.
                    if handle.activation_refused is not None:
                        raise ActivationRefused(reason_code=handle.activation_refused)
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
                f"haloflow:{context.execution_id}",
                context.tenant_id,
            ),
        )
