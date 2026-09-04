"""Per-tenant migration runner (M01-FR-018).

The transaction model is the point of this module, so it is stated here rather
than left to be inferred from the code:

1. Take a **session-level** advisory lock on a **dedicated** connection.
2. Read the ledger row. ``applied`` with a matching checksum is a no-op;
   ``applied`` with a different checksum is drift and changes nothing.
3. Commit ``running`` with an incremented attempt.
4. Execute the migration DDL in its own transaction on a second connection.
5. Commit ``applied``; or roll the DDL back **first**, then commit ``failed``
   with a sanitized code.
6. Release the lock only once the terminal ledger state is durable.

The lock must be session-level and it must live on its own connection. A
transaction-scoped lock would be released by the commit at step 3, which is the
exact moment the DDL window opens, and a second runner could then interleave
between ``running`` and ``applied``. Holding it on a separate connection also
means a rollback on the work connection cannot drop it.

The ledger is written before the DDL so that a process killed mid-migration
leaves evidence. That is why the runner commits between steps at all, and
therefore why the lock has to outlive those commits.
"""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from psycopg import AsyncConnection, sql
from psycopg import Error as PsycopgError

from haloflow.m01.errors import (
    ConnectionModeRejected,
    ExecutionRoleUnavailable,
    TenantMigrationFailed,
)
from haloflow.m01.provisioning.codes import PreconditionCode, SanitizedErrorCode
from haloflow.m01.provisioning.role_safety import assert_execution_roles_safe
from haloflow.m01.provisioning.roles import MIGRATOR_ROLE
from haloflow.m01.provisioning.units import TenantMigrationRegistry, TenantMigrationUnit
from haloflow.m01.resolver import TENANT_ID_PATTERN

ConnectionFactory = Callable[[], Awaitable[AsyncConnection]]

# Namespace for the two-integer advisory lock space, so a per-tenant migration
# lock cannot collide with an unrelated advisory lock elsewhere in the database.
MIGRATION_LOCK_NAMESPACE: Final = 0x4D30_3101  # "M01" + 01


def tenant_lock_key(tenant_id: str) -> int:
    """A stable signed 32-bit lock key for a tenant.

    Derived in Python rather than with PostgreSQL's ``hashtext``, whose value is
    an implementation detail that has changed across major versions; a lock key
    that moves during an upgrade would silently stop excluding anything.
    """

    digest = hashlib.sha256(tenant_id.encode("utf-8")).digest()[:4]
    return int.from_bytes(digest, "big", signed=True)


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    migration_id: str
    applied: bool
    """False when the unit was already applied at the same checksum."""


class TenantMigrationRunner:
    """Applies registry units to one tenant schema as ``haloflow_migrator``."""

    def __init__(
        self,
        connect: ConnectionFactory,
        registry: TenantMigrationRegistry,
        *,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        self._connect = connect
        self._registry = registry
        self._lock_timeout_seconds = lock_timeout_seconds

    @property
    def registry(self) -> TenantMigrationRegistry:
        return self._registry

    @asynccontextmanager
    async def tenant_lock(self, tenant_id: str) -> AsyncIterator[None]:
        """Hold the per-tenant migration lock on a dedicated connection.

        Public because it is the concurrency contract, not an implementation
        detail: a caller that wants to serialize a longer provisioning sequence
        against concurrent runners takes this lock around the whole of it.
        """

        _validate_tenant_id(tenant_id)
        key = tenant_lock_key(tenant_id)
        connection = await self._connect()
        try:
            await _assume_migrator(connection)
            # `SET` takes no bound parameters, so the timeout goes through
            # set_config, which does. `false` keeps it session-scoped: the lock is
            # session-level and its wait must not be governed by a setting that a
            # commit would reset.
            await connection.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{int(self._lock_timeout_seconds * 1000)}ms",),
            )
            try:
                await connection.execute(
                    "SELECT pg_advisory_lock(%s, %s)", (MIGRATION_LOCK_NAMESPACE, key)
                )
            except PsycopgError as error:
                raise TenantMigrationFailed(
                    reason_code=SanitizedErrorCode.LOCK_UNAVAILABLE.value
                ) from _sanitize(error)
            try:
                yield
            finally:
                await connection.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", (MIGRATION_LOCK_NAMESPACE, key)
                )
        finally:
            await connection.close()

    async def apply(self, *, tenant_id: str, schema_key: str) -> tuple[MigrationOutcome, ...]:
        """Bring one tenant to the registry's target version.

        Idempotent: a unit already applied at the same checksum is skipped and
        its attempt count is untouched.
        """

        _validate_tenant_id(tenant_id)
        async with self.tenant_lock(tenant_id):
            return await self._apply_locked(tenant_id=tenant_id, schema_key=schema_key)

    async def apply_within_lock(
        self, *, tenant_id: str, schema_key: str
    ) -> tuple[MigrationOutcome, ...]:
        """Apply without taking the lock, for a caller already holding it."""

        _validate_tenant_id(tenant_id)
        return await self._apply_locked(tenant_id=tenant_id, schema_key=schema_key)

    async def _apply_locked(
        self, *, tenant_id: str, schema_key: str
    ) -> tuple[MigrationOutcome, ...]:
        outcomes: list[MigrationOutcome] = []
        connection = await self._connect()
        try:
            await _assume_migrator(connection)
            # Stage 1, step 0 of A4. The runner is guarded on the same terms as
            # the provisioner (R-P1B.15), so a runner invoked outside a
            # provisioning flow does not assume a role nobody checked. One
            # component, two call sites.
            try:
                await assert_execution_roles_safe(connection, self._registry)
            except ExecutionRoleUnavailable as error:
                raise TenantMigrationFailed(reason_code=error.reason_code) from error
            for unit in self._registry:
                outcomes.append(
                    await self._apply_unit(
                        connection, unit=unit, tenant_id=tenant_id, schema_key=schema_key
                    )
                )
        finally:
            await connection.close()
        return tuple(outcomes)

    async def _apply_unit(
        self,
        connection: AsyncConnection,
        *,
        unit: TenantMigrationUnit,
        tenant_id: str,
        schema_key: str,
    ) -> MigrationOutcome:
        recorded = await self._read_ledger(connection, tenant_id, unit.migration_id)
        if recorded is not None:
            state, checksum = recorded
            if state == "applied" and checksum == unit.checksum:
                return MigrationOutcome(unit.migration_id, applied=False)
            if state == "applied":
                # Drift. Nothing is changed: re-running would silently install a
                # definition different from the one this tenant already has, and
                # rewriting the ledger would erase the evidence of that.
                raise TenantMigrationFailed(
                    reason_code=SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
                )

        rendered = unit.render(schema_key)
        await self._record_running(connection, tenant_id, unit, exists=recorded is not None)

        # The DDL and its `applied` ledger transition commit **together**.
        #
        # They were two commits in the first version of this runner, and that left a
        # durability window with no recovery: a crash after the DDL commit and
        # before the ledger write leaves `running` over a schema the DDL has already
        # changed. The next run reads `running`, re-executes the same DDL, and the
        # baseline's `CREATE TABLE` fails as a duplicate — so the retry records
        # `failed` and no further retry can ever succeed. The tenant is stranded
        # until an operator repairs it by hand, which is precisely what R-E2's
        # "safely resumable" forbids.
        #
        # One transaction removes the window rather than narrowing it. Either the
        # tenant has the objects and the ledger says `applied`, or it has neither and
        # the ledger still says `running`, which is the state a retry handles.
        # `stage` records which half raised, so the ledger's sanitized code names the
        # real failure instead of always blaming the DDL.
        stage = SanitizedErrorCode.MIGRATION_DDL_FAILED
        try:
            async with connection.transaction():
                await connection.execute(rendered)
                stage = SanitizedErrorCode.LEDGER_WRITE_FAILED
                await self._mark_applied(connection, tenant_id, unit)
                stage = SanitizedErrorCode.MIGRATION_COMMIT_FAILED
        except PsycopgError as error:
            # Rolled back by the transaction block above — both halves — so the
            # tenant schema is clean at the moment `failed` becomes visible (R-E3).
            try:
                await self._record_failed(connection, tenant_id, unit, stage)
            except PsycopgError as ledger_error:
                # The migration failed and the evidence could not be recorded.
                # Report the ledger failure: it is the more serious of the two,
                # because the tenant is now in a state no later run can reason about.
                raise TenantMigrationFailed(
                    reason_code=SanitizedErrorCode.LEDGER_WRITE_FAILED.value
                ) from _sanitize(ledger_error)
            raise TenantMigrationFailed(reason_code=stage.value) from _sanitize(error)

        return MigrationOutcome(unit.migration_id, applied=True)

    async def _read_ledger(
        self, connection: AsyncConnection, tenant_id: str, migration_id: str
    ) -> tuple[str, str] | None:
        row = await (
            await connection.execute(
                """
                SELECT state, checksum
                FROM shared.schema_migrations
                WHERE tenant_id = %s AND migration_id = %s
                """,
                (tenant_id, migration_id),
            )
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    async def _record_running(
        self,
        connection: AsyncConnection,
        tenant_id: str,
        unit: TenantMigrationUnit,
        *,
        exists: bool,
    ) -> None:
        try:
            async with connection.transaction():
                if exists:
                    await connection.execute(
                        """
                        UPDATE shared.schema_migrations
                           SET state = 'running',
                               checksum = %s,
                               attempt = attempt + 1,
                               started_at = statement_timestamp(),
                               completed_at = NULL,
                               sanitized_error_code = NULL
                         WHERE tenant_id = %s AND migration_id = %s
                        """,
                        (unit.checksum, tenant_id, unit.migration_id),
                    )
                else:
                    await connection.execute(
                        """
                        INSERT INTO shared.schema_migrations
                            (tenant_id, migration_id, checksum, state, attempt)
                        VALUES (%s, %s, %s, 'running', 1)
                        """,
                        (tenant_id, unit.migration_id, unit.checksum),
                    )
        except PsycopgError as error:
            raise TenantMigrationFailed(
                reason_code=SanitizedErrorCode.LEDGER_WRITE_FAILED.value
            ) from _sanitize(error)

    async def _mark_applied(
        self, connection: AsyncConnection, tenant_id: str, unit: TenantMigrationUnit
    ) -> None:
        """Record `applied`. Deliberately opens no transaction of its own.

        This runs inside the DDL's transaction so the two commit together; a
        `transaction()` block here would be a savepoint, not a second commit, and
        would read as though the two were still separable.
        """

        await connection.execute(
            """
            UPDATE shared.schema_migrations
               SET state = 'applied',
                   completed_at = statement_timestamp(),
                   sanitized_error_code = NULL
             WHERE tenant_id = %s AND migration_id = %s
            """,
            (tenant_id, unit.migration_id),
        )

    async def _record_failed(
        self,
        connection: AsyncConnection,
        tenant_id: str,
        unit: TenantMigrationUnit,
        code: SanitizedErrorCode,
    ) -> None:
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE shared.schema_migrations
                   SET state = 'failed',
                       completed_at = statement_timestamp(),
                       sanitized_error_code = %s
                 WHERE tenant_id = %s AND migration_id = %s
                """,
                (code.value, tenant_id, unit.migration_id),
            )


async def require_explicit_transactions(connection: AsyncConnection) -> None:
    """Put a freshly acquired connection into autocommit, or refuse it.

    Every transaction boundary in this package is explicit, and the ledger's
    intermediate commits have to be real commits. On a connection left in
    psycopg's default `autocommit=False`, the first statement opens an implicit
    transaction, every `transaction()` block below it degrades to a savepoint,
    and closing the connection rolls the whole sequence back — silently, because
    each step still appears to succeed.

    `ConnectionFactory` cannot express that in its type, so it is enforced here
    on every connection this package acquires. A factory that hands back a
    connection with work already in flight is rejected rather than adopted.

    Raises the neutral `ConnectionModeRejected`, never a caller's exception type.
    This helper serves both the runner and the provisioner, and a shared helper
    that picks one caller's taxonomy is how a provisioning call came to raise
    `TenantMigrationFailed`. Each entry point translates it below.
    """

    try:
        await connection.set_autocommit(True)
    except PsycopgError as error:
        raise ConnectionModeRejected(
            reason_code=PreconditionCode.CONNECTION_NOT_IDLE.value
        ) from _sanitize(error)


async def _assume_migrator(connection: AsyncConnection) -> None:
    try:
        await require_explicit_transactions(connection)
    except ConnectionModeRejected as error:
        raise TenantMigrationFailed(reason_code=error.reason_code) from None
    await connection.execute("SET search_path = pg_catalog")
    await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(MIGRATOR_ROLE)))


def _validate_tenant_id(tenant_id: str) -> None:
    if not TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise TenantMigrationFailed(reason_code=PreconditionCode.TENANT_ID_INVALID.value)


def _sanitize(error: PsycopgError) -> Exception:
    """Strip a database error down to its SQLSTATE before it can be chained.

    ``raise ... from error`` would otherwise attach the driver's message -- which
    carries the offending SQL and values -- to an exception whose whole purpose
    is to carry neither (R-E9). The SQLSTATE is a fixed five-character class and
    is safe to keep.
    """

    sqlstate = getattr(error, "sqlstate", None) or "unknown"
    return RuntimeError(f"database error, sqlstate {sqlstate}")
