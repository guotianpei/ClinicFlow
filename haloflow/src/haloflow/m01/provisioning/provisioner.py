"""Tenant-schema provisioner (M01-FR-017).

Ownership, settled as decision D13 on 2026-08-31 and verified on PostgreSQL
17.11: a tenant schema is owned by ``haloflow_provisioner``. The signed-off
design had the provisioner run ``CREATE SCHEMA ... AUTHORIZATION haloflow_owner``,
which PostgreSQL refuses -- ``must be able to SET ROLE "haloflow_owner"`` -- and
the membership that would allow it also hands the provisioner INSERT, DELETE and
DROP over ``shared.access_audit_log``. Provisioner-owned tenant schemas need no
membership anywhere and leave the control plane unreachable from this path.

The sequence commits between steps so that a partially provisioned tenant is
resumable rather than ambiguous (R-E2). A tenant that does not reach ``active``
is refused by ``TenantResolver`` with ``TENANT_NOT_ACTIVE``: the fail-closed path
that already exists does the work, and this module adds no second denial
mechanism that could disagree with it.

**There is deliberately no module-callback extension point here (R-E7).** An
earlier draft let a module supply an installer that received this class's live
provisioner-role connection -- a role that owns every tenant schema and can write
the tenant registry. Nothing in that interface confined an installer to the schema
it was handed, and it would have frozen a privileged contract against M02's
requirements before those requirements exist. Ordinary per-tenant objects are
contributed as migration units through the registry, which the runner already
takes as an argument. M02 must settle its own installation mechanism -- with the
function owner, ACL and pinned ``search_path`` its SECURITY DEFINER functions
actually need -- before its implementation begins. A repository control asserts
that no callback taking a connection reappears here.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg import Error as PsycopgError

from haloflow.m01.errors import (
    ConnectionModeRejected,
    ExecutionRoleUnavailable,
    ProvisioningFailed,
)
from haloflow.m01.provisioning.acl import (
    build_expected_schema_acl,
    install_schema_acl,
    read_schema_acl,
)
from haloflow.m01.provisioning.codes import PreconditionCode, SanitizedErrorCode
from haloflow.m01.provisioning.manifest import ProvisioningManifest
from haloflow.m01.provisioning.role_safety import assert_execution_roles_safe
from haloflow.m01.provisioning.roles import (
    AUDIT_PROJECTOR_ROLE,
    PROVISIONER_ROLE,
    RUNTIME_ROLE,
)
from haloflow.m01.provisioning.runner import (
    ConnectionFactory,
    TenantMigrationRunner,
    require_explicit_transactions,
)
from haloflow.m01.resolver import (
    SCHEMA_KEY_PATTERN,
    TENANT_ID_PATTERN,
    LifecycleState,
)

_ACTOR_KINDS = frozenset({"actor", "workload"})


@dataclass(frozen=True, slots=True)
class ProvisioningRequest:
    tenant_id: str
    schema_key: str
    actor_id: str
    execution_id: UUID
    actor_kind: str = "workload"
    display_reference: str | None = None
    reason_code: str = "tenant_provisioned"


@dataclass(frozen=True, slots=True)
class ProvisioningOutcome:
    tenant_id: str
    schema_key: str
    schema_version: int
    applied_migrations: tuple[str, ...]
    resumed: bool


class TenantProvisioner:
    """Allocates, builds, verifies and activates one tenant schema."""

    def __init__(
        self,
        connect: ConnectionFactory,
        runner: TenantMigrationRunner,
        *,
        supported_schema_versions: Sequence[int] | frozenset[int] | range,
        manifest: ProvisioningManifest | None = None,
    ) -> None:
        self._connect = connect
        self._runner = runner
        self._supported_schema_versions = frozenset(supported_schema_versions)
        if manifest is not None and manifest != runner.manifest:
            raise ValueError("runner and provisioner manifests must match")
        # Adopt the runner's object so both stage-1 call sites and stages 2/3
        # are incapable of observing different declarations.
        self._manifest = runner.manifest

    async def provision(self, request: ProvisioningRequest) -> ProvisioningOutcome:
        _validate_request(request)
        target_version = self._runner.registry.target_version
        if target_version not in self._supported_schema_versions:
            # R-E10. Provisioning a tenant the resolver would then refuse is a
            # configuration error, and it is cheaper to fail before the schema
            # exists than to leave an unusable tenant behind.
            raise ProvisioningFailed(reason_code=PreconditionCode.SCHEMA_VERSION_UNSUPPORTED.value)

        connection = await self._connect()
        try:
            await _assume_provisioner(connection)
            # Stage 1, and the first thing this method does against the
            # database (Q2). Before `_register_tenant`, before `_create_schema`,
            # before the lock: a configuration fault must not leave a registry
            # row, a schema, or a `running` ledger row behind it.
            try:
                await assert_execution_roles_safe(
                    connection, self._runner.registry, manifest=self._manifest
                )
            except ExecutionRoleUnavailable as error:
                raise ProvisioningFailed(reason_code=error.reason_code) from error
            # One lock for the whole sequence, so a second provisioner cannot
            # interleave with this one between its commits either.
            async with self._runner.tenant_lock(request.tenant_id):
                resumed = await self._register_tenant(connection, request, target_version)
                await self._create_schema(connection, request)
                await self._install_and_verify_schema_acl(connection, request)
                applied = await self._runner.apply_within_lock(
                    tenant_id=request.tenant_id, schema_key=request.schema_key
                )
                await self._verify(connection, request)
                await self._activate(connection, request, target_version)
        finally:
            await connection.close()

        return ProvisioningOutcome(
            tenant_id=request.tenant_id,
            schema_key=request.schema_key,
            schema_version=target_version,
            applied_migrations=tuple(
                outcome.migration_id for outcome in applied if outcome.applied
            ),
            resumed=resumed,
        )

    # -- step 1 ------------------------------------------------------------
    async def _register_tenant(
        self, connection: AsyncConnection, request: ProvisioningRequest, version: int
    ) -> bool:
        """Insert or resume the registry row. Returns True when resuming."""

        try:
            async with connection.transaction():
                row = await (
                    await connection.execute(
                        """
                        SELECT schema_key, lifecycle_state, schema_version
                        FROM shared.tenants
                        WHERE tenant_id = %s
                        """,
                        (request.tenant_id,),
                    )
                ).fetchone()

                if row is None:
                    await connection.execute(
                        """
                        INSERT INTO shared.tenants
                            (tenant_id, schema_key, lifecycle_state, schema_version,
                             display_reference)
                        VALUES (%s, %s, 'provisioning', %s, %s)
                        """,
                        (
                            request.tenant_id,
                            request.schema_key,
                            version,
                            request.display_reference,
                        ),
                    )
                    return False

                schema_key, lifecycle_state, _ = row
                if lifecycle_state != LifecycleState.PROVISIONING.value:
                    # An active, suspended or archived tenant is not something a
                    # provisioning run may adopt. R-E2's resume window is exactly
                    # the `provisioning` state, which is also the only state the
                    # 001 immutability trigger permits a schema_key change in.
                    raise ProvisioningFailed(
                        reason_code=PreconditionCode.TENANT_NOT_RESUMABLE.value
                    )
                if schema_key != request.schema_key:
                    # Never a second identity for one tenant (R-E2): the run is
                    # refused rather than silently rebinding the tenant to a new
                    # schema and orphaning the first one.
                    raise ProvisioningFailed(reason_code=PreconditionCode.SCHEMA_KEY_CONFLICT.value)
                return True
        except PsycopgError as error:
            raise ProvisioningFailed(
                reason_code=SanitizedErrorCode.REGISTRY_WRITE_FAILED.value
            ) from _sanitize(error)

    # -- step 2 ------------------------------------------------------------
    async def _create_schema(
        self, connection: AsyncConnection, request: ProvisioningRequest
    ) -> None:
        schema = sql.Identifier(request.schema_key)
        try:
            async with connection.transaction():
                # IF NOT EXISTS makes step 2 resumable. The schema is owned by the
                # provisioner (D13), so no AUTHORIZATION clause and no membership.
                await connection.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema)
                )
        except PsycopgError as error:
            raise ProvisioningFailed(
                reason_code=SanitizedErrorCode.SCHEMA_CREATE_FAILED.value
            ) from _sanitize(error)

    # -- stages 2 and 3 -----------------------------------------------------
    async def _install_and_verify_schema_acl(
        self, connection: AsyncConnection, request: ProvisioningRequest
    ) -> None:
        """Install the manifest ACL and prove exact equality atomically.

        The transaction belongs here rather than in the installer: a failed
        comparison rolls back every grant from this attempt, while any drift
        committed before the attempt remains untouched. No REVOKE or repair is
        performed. Stage 4 cannot begin until this postcondition succeeds.
        """

        try:
            async with connection.transaction():
                await install_schema_acl(connection, request.schema_key, self._manifest)
                try:
                    observed = await read_schema_acl(connection, request.schema_key)
                except RuntimeError as error:
                    raise ProvisioningFailed(
                        reason_code=SanitizedErrorCode.SCHEMA_ACL_MISMATCH.value
                    ) from error
                expected = build_expected_schema_acl(self._manifest)
                if observed != expected:
                    raise ProvisioningFailed(
                        reason_code=SanitizedErrorCode.SCHEMA_ACL_MISMATCH.value
                    )
        except ProvisioningFailed:
            raise
        except PsycopgError as error:
            raise ProvisioningFailed(
                reason_code=SanitizedErrorCode.GRANT_APPLY_FAILED.value
            ) from _sanitize(error)
        except KeyError:
            # The validated manifest cannot reach this branch. Keep a directly
            # constructed invalid typed object inside the same sanitized
            # boundary without attaching its value to the exception chain.
            raise ProvisioningFailed(
                reason_code=SanitizedErrorCode.GRANT_APPLY_FAILED.value
            ) from RuntimeError("invalid schema privilege declaration")

    # -- step 5 ------------------------------------------------------------
    async def _verify(self, connection: AsyncConnection, request: ProvisioningRequest) -> None:
        """Read the outcome back from the catalogue rather than assuming it.

        Every check is a catalogue question, not an inference from a statement
        having succeeded: a GRANT that ran is not evidence that the privilege
        landed the way the manifest says it should.
        """

        row = await (
            await connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM pg_namespace WHERE nspname = %s),
                    has_schema_privilege(%s, %s, 'USAGE'),
                    has_schema_privilege(%s, %s, 'CREATE'),
                    has_schema_privilege(%s, %s, 'USAGE'),
                    to_regclass(%s) IS NOT NULL
                """,
                (
                    request.schema_key,
                    RUNTIME_ROLE,
                    request.schema_key,
                    RUNTIME_ROLE,
                    request.schema_key,
                    AUDIT_PROJECTOR_ROLE,
                    request.schema_key,
                    f"{request.schema_key}.access_audit_outbox",
                ),
            )
        ).fetchone()

        if row is None:
            raise ProvisioningFailed(reason_code=SanitizedErrorCode.VERIFICATION_FAILED.value)
        schema_count, runtime_usage, runtime_create, projector_usage, outbox_present = row
        if not (
            schema_count == 1
            and runtime_usage
            and not runtime_create  # the runtime role must never hold DDL
            and projector_usage
            and outbox_present
        ):
            raise ProvisioningFailed(reason_code=SanitizedErrorCode.VERIFICATION_FAILED.value)

        await self._verify_cross_tenant_probe(connection, request)

    async def _verify_cross_tenant_probe(
        self, connection: AsyncConnection, request: ProvisioningRequest
    ) -> None:
        """A negative probe: the runtime role must not reach another tenant.

        Asserted against every *other* provisioned schema, so the check has real
        content on the second tenant onward rather than being vacuously true.
        """

        rows = await (
            await connection.execute(
                """
                SELECT n.nspname, has_schema_privilege(%s, n.nspname, 'CREATE')
                FROM pg_namespace AS n
                JOIN shared.tenants AS t ON t.schema_key = n.nspname
                WHERE n.nspname <> %s
                """,
                (RUNTIME_ROLE, request.schema_key),
            )
        ).fetchall()
        if any(can_create for _, can_create in rows):
            raise ProvisioningFailed(reason_code=SanitizedErrorCode.VERIFICATION_FAILED.value)

    # -- step 6 ------------------------------------------------------------
    async def _activate(
        self, connection: AsyncConnection, request: ProvisioningRequest, version: int
    ) -> None:
        try:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE shared.tenants
                       SET lifecycle_state = 'active',
                           schema_version = %s,
                           updated_at = statement_timestamp()
                     WHERE tenant_id = %s AND lifecycle_state = 'provisioning'
                    """,
                    (version, request.tenant_id),
                )
                await connection.execute(
                    """
                    INSERT INTO shared.tenant_state_history
                        (tenant_id, prior_state, new_state, reason_code,
                         actor_kind, actor_id, execution_id)
                    VALUES (%s, 'provisioning', 'active', %s, %s, %s, %s)
                    """,
                    (
                        request.tenant_id,
                        request.reason_code,
                        request.actor_kind,
                        request.actor_id,
                        request.execution_id,
                    ),
                )
        except PsycopgError as error:
            raise ProvisioningFailed(
                reason_code=SanitizedErrorCode.REGISTRY_WRITE_FAILED.value
            ) from _sanitize(error)

async def _assume_provisioner(connection: AsyncConnection) -> None:
    # Same contract as the runner's, and enforced by the same function: the
    # provisioning sequence commits between steps so a partial tenant is
    # resumable, and on a non-autocommit connection those commits would be
    # savepoints that a close() discards. The shared check raises neutrally; a
    # provisioning call must fail as ProvisioningFailed, not as a migration error.
    try:
        await require_explicit_transactions(connection)
    except ConnectionModeRejected as error:
        raise ProvisioningFailed(reason_code=error.reason_code) from None
    await connection.execute("SET search_path = pg_catalog")
    await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))


def _validate_request(request: ProvisioningRequest) -> None:
    if not TENANT_ID_PATTERN.fullmatch(request.tenant_id):
        raise ProvisioningFailed(reason_code=PreconditionCode.TENANT_ID_INVALID.value)
    if not SCHEMA_KEY_PATTERN.fullmatch(request.schema_key):
        raise ProvisioningFailed(reason_code=PreconditionCode.SCHEMA_KEY_INVALID.value)
    if request.actor_kind not in _ACTOR_KINDS:
        raise ProvisioningFailed(reason_code=PreconditionCode.ACTOR_KIND_INVALID.value)
    if not request.actor_id:
        raise ProvisioningFailed(reason_code=PreconditionCode.ACTOR_ID_REQUIRED.value)
    if not isinstance(request.execution_id, UUID):
        raise ProvisioningFailed(reason_code=PreconditionCode.EXECUTION_ID_INVALID.value)


def _sanitize(error: PsycopgError) -> Exception:
    sqlstate = getattr(error, "sqlstate", None) or "unknown"
    return RuntimeError(f"database error, sqlstate {sqlstate}")


__all__ = [
    "ProvisioningOutcome",
    "ProvisioningRequest",
    "TenantProvisioner",
]
