"""TC-E1 through TC-E26: provisioner, migration runner, and migration 003 grants.

Every privilege assertion here reads PostgreSQL's own catalogue. That is the
whole point of finding F-3: `permissions.json` had been verified only against
itself, which is why `001` shipping the provisioning roles with no grants at all
went unnoticed from merge until 2026-08-30.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from psycopg.errors import InsufficientPrivilege

from haloflow.composition import build_production_tenant_migrations
from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m01.control_store import PsycopgControlStore
from haloflow.m01.errors import (
    ProvisioningFailed,
    TenantMigrationFailed,
    TenantUnavailable,
)
from haloflow.m01.pool import TenantPool
from haloflow.m01.provisioning import (
    AUDIT_PROJECTOR_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    RUNTIME_ROLE,
    ProvisioningRequest,
    SanitizedErrorCode,
    TenantMigrationRegistry,
    TenantMigrationRunner,
    TenantProvisioner,
    report_drift,
)
from haloflow.m01.provisioning.units import (
    TENANT_MIGRATIONS,
    build_tenant_migration_registry,
)
from haloflow.m01.resolver import TenantResolver

pytestmark = pytest.mark.postgres

M01_ROOT = Path("src/haloflow/m01")
OWNER_ROLE = "haloflow_owner"
CONTROL_AUDIT_WRITER_ROLE = "haloflow_control_audit_writer"

SHARED_TABLES = (
    "tenants",
    "tenant_state_history",
    "schema_migrations",
    "support_access_grants",
    "access_audit_log",
    "isolation_alerts",
)
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
AUDIT_SEQUENCE = "shared.access_audit_log_audit_id_seq"

# The manifest's shared-schema tokens, expanded into the concrete table
# privileges each one authorizes. This mapping is the translation F-3 says has to
# exist somewhere and be checked: the manifest states intent, this states what
# that intent means in PostgreSQL, and the test compares it to what the database
# actually granted.
TOKEN_TABLE_GRANTS: dict[str, dict[str, frozenset[str]]] = {
    "shared.tenants:controlled_write": {"tenants": frozenset({"SELECT", "INSERT", "UPDATE"})},
    "shared.schema_migrations:controlled_write": {
        "schema_migrations": frozenset({"SELECT", "INSERT", "UPDATE"})
    },
    "shared.schema_migrations:read": {"schema_migrations": frozenset({"SELECT"})},
    "shared.tenant_state_history:insert": {"tenant_state_history": frozenset({"INSERT"})},
    "shared.access_audit_log:insert": {"access_audit_log": frozenset({"INSERT"})},
}

# Allow tokens that deliberately grant nothing on a shared table, each with the
# reason it is out of this control's scope. Every allow token in the manifest must
# appear either here or in TOKEN_TABLE_GRANTS, so a token nobody translated cannot
# pass unnoticed.
NON_SHARED_TABLE_TOKENS: dict[str, str] = {
    "shared:ownership": "ownership, not a grant; asserted separately by table owner",
    "shared.tenants:select(tenant_id,schema_key,lifecycle_state,schema_version)": (
        "column-scoped, not table-level; asserted by the runtime column-grant test"
    ),
    "tenant_schema:ownership": "per-tenant schema ownership; asserted on a provisioned schema",
    "tenant_schema:provision": "CREATE on the database; asserted by the provisioning tests",
    "tenant_schema:checksummed_ddl": "per-tenant CREATE; asserted on a provisioned schema",
    "tenant_schema:business_dml": "per-tenant table DML; asserted on a provisioned schema",
    "tenant_schema.access_audit_outbox:insert": "per-tenant table; asserted once provisioned",
    "tenant_schema.access_audit_outbox:select": "per-tenant table; asserted once provisioned",
    "tenant_schema:approved_support_read": "grant-mediated, never a standing privilege",
    "tenant_schema:approved_emergency_read": "grant-mediated, never a standing privilege",
    "tenant_schema:separately_approved_emergency_write": "grant-mediated, never standing",
}


# --- harness ---------------------------------------------------------------


@dataclass(frozen=True)
class ProvisioningHarness:
    admin_conninfo: str
    role_logins: dict[str, str]
    reset: Callable[[str, Sequence[str], Sequence[str]], None]


def connection_factory(conninfo: str) -> Callable[[], Awaitable[AsyncConnection]]:
    async def _connect() -> AsyncConnection:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


def make_registry(
    *extra: dict[str, str], include_baseline: bool = True
) -> TenantMigrationRegistry:
    sets: list[dict[str, str]] = [dict(TENANT_MIGRATIONS)] if include_baseline else []
    sets.extend(extra)
    return build_tenant_migration_registry(*sets, allow_test_units=True)


def make_provisioner(
    harness: ProvisioningHarness,
    registry: TenantMigrationRegistry | None = None,
    *,
    supported_schema_versions: Sequence[int] | range = range(1, 2),
    lock_timeout_seconds: float = 30.0,
) -> TenantProvisioner:
    runner = TenantMigrationRunner(
        connection_factory(harness.role_logins[MIGRATOR_ROLE]),
        registry or make_registry(),
        lock_timeout_seconds=lock_timeout_seconds,
    )
    return TenantProvisioner(
        connection_factory(harness.role_logins[PROVISIONER_ROLE]),
        runner,
        supported_schema_versions=supported_schema_versions,
    )


def request_for(tenant: tuple[str, str], **overrides: object) -> ProvisioningRequest:
    tenant_id, schema_key = tenant
    fields: dict[str, object] = {
        "tenant_id": tenant_id,
        "schema_key": schema_key,
        "actor_id": "provisioning-test",
        "execution_id": uuid5(NAMESPACE_URL, f"haloflow-test:provision:{tenant_id}"),
    }
    fields.update(overrides)
    return ProvisioningRequest(**fields)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def provisioning_harness(
    migrated_database: str,
    role_logins: dict[str, str],
    reset_tenants: Callable[[str, Sequence[str], Sequence[str]], None],
) -> ProvisioningHarness:
    return ProvisioningHarness(migrated_database, role_logins, reset_tenants)


@pytest_asyncio.fixture
async def new_tenant(
    provisioning_harness: ProvisioningHarness,
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[str, str]]:
    """A tenant identity unique to one test, removed again afterwards."""

    index = abs(hash(request.node.name)) % 10_000_000
    tenant_id = f"clinic-p{index:07d}"
    schema_key = f"tenant_p{index:07d}"
    provisioning_harness.reset(provisioning_harness.admin_conninfo, [tenant_id], [schema_key])
    yield tenant_id, schema_key
    provisioning_harness.reset(provisioning_harness.admin_conninfo, [tenant_id], [schema_key])


async def admin_row(harness: ProvisioningHarness, query: str, params: object = None) -> tuple:
    async with await AsyncConnection.connect(harness.admin_conninfo, autocommit=True) as conn:
        row = await (await conn.execute(query, params)).fetchone()  # type: ignore[arg-type]
    assert row is not None
    return row


async def admin_rows(harness: ProvisioningHarness, query: str, params: object = None) -> list:
    async with await AsyncConnection.connect(harness.admin_conninfo, autocommit=True) as conn:
        return await (await conn.execute(query, params)).fetchall()  # type: ignore[arg-type]


# --- CP-5a: tenant-schema ACL reader --------------------------------------


@pytest.mark.asyncio
async def test_acl_reader_reads_null_as_empty(
    provisioning_harness: ProvisioningHarness,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P64(b): nspacl is exploded as-is, so NULL safely yields no rows."""

    from haloflow.m01.provisioning.acl import read_schema_acl

    _, schema_key = new_tenant
    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))

        # A fresh schema has nspacl IS NULL. Exploding it as-is is safe and
        # yields no rows; coalescing it to '{}' raises on PostgreSQL 17.
        matched = await (
            await conn.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema_key,)
            )
        ).fetchone()
        assert matched == (1,)
        assert await read_schema_acl(conn, schema_key) == frozenset()

@pytest.mark.asyncio
async def test_acl_reader_returns_all_four_dimensions_including_public_and_delegation(
    provisioning_harness: ProvisioningHarness,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P64(a), V22, V23, V25: read the complete drifted ACL verbatim."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry, read_schema_acl

    _, schema_key = new_tenant
    schema = sql.Identifier(schema_key)
    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        await conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(schema))
        await conn.execute(
            sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
                schema, sql.Identifier(RUNTIME_ROLE)
            )
        )
        await conn.execute(
            sql.SQL("GRANT CREATE ON SCHEMA {} TO {} WITH GRANT OPTION").format(
                schema, sql.Identifier(MIGRATOR_ROLE)
            )
        )
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(MIGRATOR_ROLE)))
        await conn.execute(
            sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
                schema, sql.Identifier(AUDIT_PROJECTOR_ROLE)
            )
        )
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        observed = await read_schema_acl(conn, schema_key)

    assert observed == frozenset(
        {
            SchemaAclEntry("PUBLIC", "USAGE", False, PROVISIONER_ROLE),
            SchemaAclEntry(RUNTIME_ROLE, "CREATE", False, PROVISIONER_ROLE),
            SchemaAclEntry(MIGRATOR_ROLE, "CREATE", True, PROVISIONER_ROLE),
            SchemaAclEntry(AUDIT_PROJECTOR_ROLE, "CREATE", False, MIGRATOR_ROLE),
            SchemaAclEntry(PROVISIONER_ROLE, "CREATE", False, PROVISIONER_ROLE),
            SchemaAclEntry(PROVISIONER_ROLE, "USAGE", False, PROVISIONER_ROLE),
        }
    )


async def ledger_row(harness: ProvisioningHarness, tenant_id: str, migration_id: str) -> tuple:
    return await admin_row(
        harness,
        """
        SELECT state, attempt, checksum, sanitized_error_code, completed_at
        FROM shared.schema_migrations
        WHERE tenant_id = %s AND migration_id = %s
        """,
        (tenant_id, migration_id),
    )


FAILING_UNIT = {
    "t001_test_fails": (
        "CREATE TABLE {schema}.will_be_rolled_back (id integer PRIMARY KEY);"
        "SELECT 1 / 0;"
    )
}
SLOW_UNIT = {"t001_test_slow": "CREATE TABLE {schema}.slow (id integer); SELECT pg_sleep(1);"}


# --- TC-E1, TC-E2: the happy path -----------------------------------------


async def test_provisioning_builds_activates_and_records_a_tenant(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E1 and TC-E2."""

    tenant_id, schema_key = new_tenant
    outcome = await make_provisioner(provisioning_harness).provision(request_for(new_tenant))

    assert outcome.applied_migrations == ("t001_m01_baseline",)
    assert outcome.schema_version == 1
    assert outcome.resumed is False

    state, attempt, checksum, error_code, completed_at = await ledger_row(
        provisioning_harness, tenant_id, "t001_m01_baseline"
    )
    expected_checksum = build_production_tenant_migrations().units[0].checksum
    assert (state, attempt, checksum, error_code) == ("applied", 1, expected_checksum, None)
    assert completed_at is not None

    lifecycle, version = await admin_row(
        provisioning_harness,
        "SELECT lifecycle_state, schema_version FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    )
    assert (lifecycle, version) == ("active", 1)

    history = await admin_rows(
        provisioning_harness,
        """
        SELECT prior_state, new_state, execution_id IS NOT NULL
        FROM shared.tenant_state_history WHERE tenant_id = %s
        """,
        (tenant_id,),
    )
    assert history == [("provisioning", "active", True)]

    owner, outbox = await admin_row(
        provisioning_harness,
        """
        SELECT pg_get_userbyid(nspowner), to_regclass(%s) IS NOT NULL
        FROM pg_namespace WHERE nspname = %s
        """,
        (f"{schema_key}.access_audit_outbox", schema_key),
    )
    # D13: the schema is owned by the provisioning group role, not by the login
    # shim and not by haloflow_owner.
    assert owner == PROVISIONER_ROLE
    assert outbox is True


# --- TC-E3, TC-E20: failure behaviour --------------------------------------


async def test_a_failed_migration_leaves_the_tenant_inactive_and_the_schema_clean(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E3 and TC-E20."""

    tenant_id, schema_key = new_tenant
    provisioner = make_provisioner(provisioning_harness, make_registry(FAILING_UNIT))

    with pytest.raises(TenantMigrationFailed) as error:
        await provisioner.provision(request_for(new_tenant))

    assert error.value.reason_code == SanitizedErrorCode.MIGRATION_DDL_FAILED.value
    message = f"{error.value} {error.value.reason_code}"
    for leaked in ("1 / 0", "division", "will_be_rolled_back", schema_key, "SELECT"):
        assert leaked not in message

    state, attempt, _, error_code, _ = await ledger_row(
        provisioning_harness, tenant_id, "t001_test_fails"
    )
    assert (state, attempt, error_code) == (
        "failed",
        1,
        SanitizedErrorCode.MIGRATION_DDL_FAILED.value,
    )

    # The DDL was rolled back before `failed` was committed, so the table the
    # unit created before failing is not there now that `failed` is visible.
    (rolled_back,) = await admin_row(
        provisioning_harness,
        "SELECT to_regclass(%s) IS NULL",
        (f"{schema_key}.will_be_rolled_back",),
    )
    assert rolled_back is True

    (lifecycle,) = await admin_row(
        provisioning_harness,
        "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    )
    assert lifecycle == "provisioning"


async def test_a_tenant_that_failed_provisioning_is_refused_at_resolution(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E13. The existing fail-closed path, asserted rather than assumed."""

    tenant_id, _ = new_tenant
    provisioner = make_provisioner(provisioning_harness, make_registry(FAILING_UNIT))
    with pytest.raises(TenantMigrationFailed):
        await provisioner.provision(request_for(new_tenant))

    pool = TenantPool(provisioning_harness.role_logins[RUNTIME_ROLE], min_size=1, max_size=1)
    await pool.open()
    try:
        resolver = TenantResolver(
            PsycopgControlStore(pool),
            supported_schema_versions=range(1, 2),
            context_ttl=timedelta(seconds=10),
        )
        with pytest.raises(TenantUnavailable) as error:
            await resolver.resolve(
                principal=Principal(
                    kind=PrincipalKind.WORKLOAD,
                    id="provisioning-test",
                    auth_method="test",
                    authorized_tenant_ids=frozenset({tenant_id}),
                    capabilities=frozenset({"probe:read"}),
                ),
                tenant_hint=tenant_id,
                purpose="operations",
                capabilities=frozenset({"probe:read"}),
                source=TrustedSource.WORKER,
                execution_id=uuid5(NAMESPACE_URL, "haloflow-test:e13"),
                correlation_id=uuid5(NAMESPACE_URL, "haloflow-test:e13-correlation"),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
            )
        assert error.value.reason_code == "TENANT_NOT_ACTIVE"
    finally:
        await pool.close()


# --- TC-E4, TC-E5, TC-E6: resume, idempotence, drift -----------------------


async def test_a_failed_run_resumes_with_the_same_identity_and_a_new_attempt(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E4."""

    tenant_id, schema_key = new_tenant
    failing = make_provisioner(provisioning_harness, make_registry(FAILING_UNIT))
    with pytest.raises(TenantMigrationFailed):
        await failing.provision(request_for(new_tenant))

    # The operator fixes the migration; the same tenant is provisioned again.
    fixed = {"t001_test_fails": "CREATE TABLE {schema}.now_works (id integer PRIMARY KEY);"}
    outcome = await make_provisioner(provisioning_harness, make_registry(fixed)).provision(
        request_for(new_tenant)
    )

    assert outcome.resumed is True
    assert outcome.schema_key == schema_key

    state, attempt, _, error_code, _ = await ledger_row(
        provisioning_harness, tenant_id, "t001_test_fails"
    )
    assert (state, attempt, error_code) == ("applied", 2, None)

    rows = await admin_rows(
        provisioning_harness,
        "SELECT schema_key, lifecycle_state FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    )
    assert rows == [(schema_key, "active")]


async def test_reapplying_an_applied_migration_changes_nothing(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E5."""

    tenant_id, schema_key = new_tenant
    registry = make_registry()
    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]), registry
    )
    await make_provisioner(provisioning_harness, registry).provision(request_for(new_tenant))
    before = await ledger_row(provisioning_harness, tenant_id, "t001_m01_baseline")

    outcomes = await runner.apply(tenant_id=tenant_id, schema_key=schema_key)

    assert [outcome.applied for outcome in outcomes] == [False]
    assert await ledger_row(provisioning_harness, tenant_id, "t001_m01_baseline") == before


async def test_an_edited_applied_migration_is_refused_as_drift(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E6. Re-running would install a different definition silently."""

    tenant_id, schema_key = new_tenant
    original = {"t001_test_edited": "CREATE TABLE {schema}.thing (id integer PRIMARY KEY);"}
    await make_provisioner(provisioning_harness, make_registry(original)).provision(
        request_for(new_tenant)
    )
    before = await ledger_row(provisioning_harness, tenant_id, "t001_test_edited")

    edited = {"t001_test_edited": "CREATE TABLE {schema}.thing (id bigint PRIMARY KEY);"}
    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        make_registry(edited),
    )
    with pytest.raises(TenantMigrationFailed) as error:
        await runner.apply(tenant_id=tenant_id, schema_key=schema_key)

    assert error.value.reason_code == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
    assert await ledger_row(provisioning_harness, tenant_id, "t001_test_edited") == before


# --- TC-E7, TC-E16: concurrency -------------------------------------------


async def test_two_concurrent_runners_do_not_interleave(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E7 and TC-E16.

    The slow unit commits `running`, then sleeps inside the DDL transaction. A
    second runner is refused during that window, which is only true if the lock
    survived the `running` commit -- the defect a transaction-scoped lock would
    have had.
    """

    tenant_id, schema_key = new_tenant
    await make_provisioner(provisioning_harness, make_registry()).provision(
        request_for(new_tenant)
    )

    registry = make_registry(SLOW_UNIT)
    slow = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]), registry
    )
    impatient = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        registry,
        lock_timeout_seconds=0.1,
    )

    async def contend() -> BaseException | None:
        await asyncio.sleep(0.3)
        try:
            await impatient.apply(tenant_id=tenant_id, schema_key=schema_key)
        except BaseException as error:  # noqa: BLE001 - returned for assertion
            return error
        return None

    applied, contention = await asyncio.gather(
        slow.apply(tenant_id=tenant_id, schema_key=schema_key), contend()
    )

    assert [outcome.migration_id for outcome in applied if outcome.applied] == ["t001_test_slow"]
    assert isinstance(contention, TenantMigrationFailed)
    assert contention.reason_code == SanitizedErrorCode.LOCK_UNAVAILABLE.value

    rows = await admin_rows(
        provisioning_harness,
        """
        SELECT state, attempt FROM shared.schema_migrations
        WHERE tenant_id = %s AND migration_id = 't001_test_slow'
        """,
        (tenant_id,),
    )
    # PRIMARY KEY (tenant_id, migration_id) leaves exactly one row, and the
    # refused runner never got far enough to increment the attempt.
    assert rows == [("applied", 1)]


# --- TC-E8, TC-E9: what the runtime role can and cannot do -----------------


async def test_the_runtime_role_gets_data_access_and_no_ddl_on_a_tenant_schema(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E8."""

    _, schema_key = new_tenant
    registry = make_registry({"t001_test_business": "CREATE TABLE {schema}.appointment (id int);"})
    await make_provisioner(provisioning_harness, registry).provision(request_for(new_tenant))

    row = await admin_row(
        provisioning_harness,
        """
        SELECT
            has_schema_privilege(%s, %s, 'USAGE'),
            has_schema_privilege(%s, %s, 'CREATE'),
            has_table_privilege(%s, %s, 'SELECT'),
            has_table_privilege(%s, %s, 'INSERT'),
            has_table_privilege(%s, %s, 'UPDATE'),
            has_table_privilege(%s, %s, 'DELETE'),
            has_table_privilege(%s, %s, 'INSERT')
        """,
        (
            RUNTIME_ROLE, schema_key,
            RUNTIME_ROLE, schema_key,
            RUNTIME_ROLE, f"{schema_key}.appointment",
            RUNTIME_ROLE, f"{schema_key}.appointment",
            RUNTIME_ROLE, f"{schema_key}.appointment",
            RUNTIME_ROLE, f"{schema_key}.appointment",
            RUNTIME_ROLE, f"{schema_key}.access_audit_outbox",
        ),
    )
    assert row == (True, False, True, True, True, True, True)

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[RUNTIME_ROLE], autocommit=True
    ) as runtime:
        with pytest.raises(InsufficientPrivilege):
            await runtime.execute(f"CREATE TABLE {schema_key}.sneaky (id integer)")


async def test_the_runtime_role_cannot_execute_the_provisioning_path(
    provisioning_harness: ProvisioningHarness,
) -> None:
    """TC-E9. Privilege separation, M01-FR-013."""

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[RUNTIME_ROLE], autocommit=True
    ) as runtime:
        with pytest.raises(InsufficientPrivilege):
            await runtime.execute("CREATE SCHEMA tenant_notallowed")
        with pytest.raises(InsufficientPrivilege):
            await runtime.execute(
                "INSERT INTO shared.tenants "
                "(tenant_id, schema_key, lifecycle_state, schema_version) "
                "VALUES ('clinic-rogue', 'tenant_rogue001', 'active', 1)"
            )
        with pytest.raises(InsufficientPrivilege):
            await runtime.execute("SET ROLE haloflow_provisioner")


# --- TC-E10: drift reporting ----------------------------------------------


async def test_the_drift_report_finds_a_tenant_behind_the_registry(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E10."""

    tenant_id, _ = new_tenant
    await make_provisioner(provisioning_harness, make_registry()).provision(request_for(new_tenant))

    # A registry that has moved on: the tenant now lacks t002 entirely.
    ahead = make_registry({"t002_test_next": "CREATE TABLE {schema}.next (id integer);"})
    reports = await report_drift(
        connection_factory(provisioning_harness.role_logins[PROVISIONER_ROLE]), ahead
    )
    behind = {report.tenant_id: report for report in reports}

    assert tenant_id in behind
    assert behind[tenant_id].missing_migrations == ("t002_test_next",)
    assert behind[tenant_id].failed_migrations == ()
    assert behind[tenant_id].drifted_migrations == ()
    assert behind[tenant_id].is_current is False

    current = await report_drift(
        connection_factory(provisioning_harness.role_logins[PROVISIONER_ROLE]),
        make_registry(),
        include_current=True,
    )
    assert {report.tenant_id: report.is_current for report in current}[tenant_id] is True


# --- TC-E11: schema-version compatibility ---------------------------------


async def test_provisioning_is_refused_when_the_target_version_is_unsupported(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """R-E10, the provisioning half of TC-E11."""

    provisioner = make_provisioner(
        provisioning_harness, make_registry(), supported_schema_versions=range(2, 3)
    )
    with pytest.raises(ProvisioningFailed) as error:
        await provisioner.provision(request_for(new_tenant))
    assert error.value.reason_code == "SCHEMA_VERSION_UNSUPPORTED"

    rows = await admin_rows(
        provisioning_harness,
        "SELECT 1 FROM shared.tenants WHERE tenant_id = %s",
        (new_tenant[0],),
    )
    assert rows == []


async def test_a_tenant_outside_the_supported_versions_is_refused_at_resolution(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E11, the resolver half."""

    tenant_id, _ = new_tenant
    await make_provisioner(provisioning_harness, make_registry()).provision(request_for(new_tenant))

    pool = TenantPool(provisioning_harness.role_logins[RUNTIME_ROLE], min_size=1, max_size=1)
    await pool.open()
    try:
        resolver = TenantResolver(
            PsycopgControlStore(pool),
            supported_schema_versions=range(2, 3),
            context_ttl=timedelta(seconds=10),
        )
        with pytest.raises(TenantUnavailable) as error:
            await resolver.resolve(
                principal=Principal(
                    kind=PrincipalKind.WORKLOAD,
                    id="provisioning-test",
                    auth_method="test",
                    authorized_tenant_ids=frozenset({tenant_id}),
                    capabilities=frozenset({"probe:read"}),
                ),
                tenant_hint=tenant_id,
                purpose="operations",
                capabilities=frozenset({"probe:read"}),
                source=TrustedSource.WORKER,
                execution_id=uuid5(NAMESPACE_URL, "haloflow-test:e11"),
                correlation_id=uuid5(NAMESPACE_URL, "haloflow-test:e11-correlation"),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
            )
        assert error.value.reason_code == "SCHEMA_VERSION_INCOMPATIBLE"
    finally:
        await pool.close()


# --- TC-E15, TC-E22: migration 003 grants against the catalogue -----------


class UntranslatedToken(AssertionError):
    """A manifest token this control does not know how to check."""


def _expected_shared_table_grants(policy: dict[str, list[str]]) -> dict[str, frozenset[str]]:
    """Translate a role's allow tokens into the shared-table privileges they authorize.

    Fails closed. An earlier version used `TOKEN_TABLE_GRANTS.get(token, {})`, so a
    misspelled or newly added shared token translated to *no* expected privileges —
    and if the migration also omitted the grant, this supposedly exhaustive control
    passed. That is the same fail-open shape as finding F-3 itself, one level up:
    a check that cannot see a thing reports no problem with it.
    """

    expected: dict[str, frozenset[str]] = {table: frozenset() for table in SHARED_TABLES}
    for token in policy["allow"]:
        if token in TOKEN_TABLE_GRANTS:
            for table, privileges in TOKEN_TABLE_GRANTS[token].items():
                expected[table] = expected[table] | privileges
        elif token not in NON_SHARED_TABLE_TOKENS:
            raise UntranslatedToken(
                f"{token!r} is not translated to shared-table privileges and is not "
                "listed as deliberately out of scope; add it to TOKEN_TABLE_GRANTS or "
                "to NON_SHARED_TABLE_TOKENS with a reason"
            )
    return expected


async def test_actual_shared_grants_match_the_permissions_manifest(
    provisioning_harness: ProvisioningHarness,
) -> None:
    """TC-E22, the F-3 control.

    Exhaustive over every role, every shared table and every table privilege, so
    a grant the manifest does not authorize fails here as loudly as a missing one.
    The owner is excluded because ownership confers everything by definition;
    that it owns them is asserted separately.
    """

    manifest = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    checked = {role: policy for role, policy in manifest.items() if role != OWNER_ROLE}

    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as admin:
        actual: dict[tuple[str, str, str], bool] = {}
        for role in checked:
            for table in SHARED_TABLES:
                for privilege in TABLE_PRIVILEGES:
                    row = await (
                        await admin.execute(
                            "SELECT has_table_privilege(%s, %s, %s)",
                            (role, f"shared.{table}", privilege),
                        )
                    ).fetchone()
                    actual[(role, table, privilege)] = bool(row and row[0])

        owners = await (
            await admin.execute(
                """
                SELECT DISTINCT pg_get_userbyid(relowner)
                FROM pg_class
                WHERE relnamespace = 'shared'::regnamespace AND relkind = 'r'
                """
            )
        ).fetchall()

    expected = {
        (role, table, privilege): privilege in _expected_shared_table_grants(policy)[table]
        for role, policy in checked.items()
        for table in SHARED_TABLES
        for privilege in TABLE_PRIVILEGES
    }
    unexpected = {key for key, held in actual.items() if held and not expected[key]}
    missing = {key for key, held in actual.items() if not held and expected[key]}

    assert unexpected == set(), f"grants the manifest does not authorize: {sorted(unexpected)}"
    assert missing == set(), f"grants the manifest requires, database lacks: {sorted(missing)}"
    assert [owner for (owner,) in owners] == [OWNER_ROLE]


async def test_shared_schema_usage_matches_the_manifest(
    provisioning_harness: ProvisioningHarness,
) -> None:
    """TC-E15. A role with no shared-schema token must not reach the schema at all."""

    manifest = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    expected_usage = {
        role: any(token.startswith("shared.") for token in policy["allow"])
        for role, policy in manifest.items()
        if role != OWNER_ROLE
    }

    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as admin:
        actual_usage = {}
        actual_create = {}
        for role in expected_usage:
            row = await (
                await admin.execute(
                    """
                    SELECT has_schema_privilege(%s, 'shared', 'USAGE'),
                           has_schema_privilege(%s, 'shared', 'CREATE')
                    """,
                    (role, role),
                )
            ).fetchone()
            assert row is not None
            actual_usage[role] = bool(row[0])
            actual_create[role] = bool(row[1])

    assert actual_usage == expected_usage
    assert set(actual_create.values()) == {False}


async def test_the_runtime_role_keeps_only_its_column_grants_on_shared_tenants(
    provisioning_harness: ProvisioningHarness,
) -> None:
    """003 must not have widened the column-scoped read `001` gave the runtime."""

    row = await admin_row(
        provisioning_harness,
        """
        SELECT has_column_privilege(%s, 'shared.tenants', 'tenant_id', 'SELECT'),
               has_column_privilege(%s, 'shared.tenants', 'schema_key', 'SELECT'),
               has_column_privilege(%s, 'shared.tenants', 'lifecycle_state', 'SELECT'),
               has_column_privilege(%s, 'shared.tenants', 'schema_version', 'SELECT'),
               has_column_privilege(%s, 'shared.tenants', 'display_reference', 'SELECT')
        """,
        (RUNTIME_ROLE,) * 5,
    )
    assert row == (True, True, True, True, False)


# --- TC-E17, TC-E18, TC-E19, TC-E23, TC-E26: role separation --------------


async def test_the_provisioner_cannot_write_the_migration_ledger(
    provisioning_harness: ProvisioningHarness,
) -> None:
    """TC-E17."""

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        await conn.execute(f"SET ROLE {PROVISIONER_ROLE}")
        await conn.execute("SELECT count(*) FROM shared.schema_migrations")  # read is allowed
        for statement in (
            "INSERT INTO shared.schema_migrations "
            "(tenant_id, migration_id, checksum, state) VALUES ('clinic-a', 'x', 'y', 'pending')",
            "UPDATE shared.schema_migrations SET state = 'applied'",
            "DELETE FROM shared.schema_migrations",
        ):
            with pytest.raises(InsufficientPrivilege):
                await conn.execute(statement)


async def test_the_migrator_cannot_touch_the_tenant_registry_or_its_history(
    provisioning_harness: ProvisioningHarness,
) -> None:
    """TC-E18 and TC-E26."""

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[MIGRATOR_ROLE], autocommit=True
    ) as conn:
        await conn.execute(f"SET ROLE {MIGRATOR_ROLE}")
        for statement in (
            "SELECT * FROM shared.tenants",
            "UPDATE shared.tenants SET lifecycle_state = 'active'",
            "SELECT * FROM shared.tenant_state_history",
            "INSERT INTO shared.tenant_state_history "
            "(tenant_id, new_state, reason_code, actor_kind, actor_id, execution_id) "
            "VALUES ('clinic-a', 'active', 'r', 'workload', 'a', gen_random_uuid())",
        ):
            with pytest.raises(InsufficientPrivilege):
                await conn.execute(statement)


async def test_neither_provisioning_role_can_assume_the_other(
    provisioning_harness: ProvisioningHarness,
) -> None:
    """TC-E19, converted to the exact-set assertion R-P1B.7 requires.

    The behavioural half is unchanged: neither provisioning role may `SET ROLE`
    to the other, asserted against a live server.

    The catalogue half **was** `memberships == []` over a `haloflow%` prefix on
    both endpoints. R-P1B.7 says that control *becomes* an exact-set assertion,
    and Codex note-22 approved the conversion: once a declared execution role
    exists, its Alembic-created edge is legitimate and `== []` would fail on a
    correctly configured database. Keeping it would preserve an obsolete
    contract, not protect a valid one.

    Two things change and both are deliberate. The scope is now the manifest's
    **controlled role set** rather than a name prefix -- a prefix misses an
    execution role named otherwise, and captures unrelated roles that merely
    share it, which is why the old query needed its `NOT LIKE 'haloflow_test%'`
    exclusion for the harness's own LOGIN shims. And the expectation is now the
    **declaration** rather than the empty set, so the assertion states what the
    graph should be instead of that it should be nothing.

    With today's shipped manifest the declaration is empty, so this still
    asserts an empty controlled graph -- the same fact, from the right source.
    """

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest
    from haloflow.m01.provisioning.role_safety import MembershipEdge, declared_edges

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        await conn.execute(f"SET ROLE {PROVISIONER_ROLE}")
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(f"SET ROLE {MIGRATOR_ROLE}")

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[MIGRATOR_ROLE], autocommit=True
    ) as conn:
        await conn.execute(f"SET ROLE {MIGRATOR_ROLE}")
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(f"SET ROLE {PROVISIONER_ROLE}")

    manifest = load_provisioning_manifest()
    controlled = sorted(manifest.controlled_roles)

    rows = await admin_rows(
        provisioning_harness,
        """
        SELECT r.rolname, m.rolname, a.set_option, a.inherit_option, a.admin_option
        FROM pg_auth_members AS a
        JOIN pg_roles AS r ON r.oid = a.roleid
        JOIN pg_roles AS m ON m.oid = a.member
        WHERE r.rolname = ANY(%s) AND m.rolname = ANY(%s)
        """,
        (controlled, controlled),
    )
    observed = frozenset(
        MembershipEdge(
            role=row[0], member=row[1], set=row[2], inherit=row[3], admin=row[4]
        )
        for row in rows
    )

    assert observed == declared_edges(manifest.role_memberships)


async def test_the_provisioner_may_append_but_never_amend_tenant_state_history(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E23 and TC-E24.

    Privilege and the append-only trigger refuse independently, and the INSERT
    succeeds with no sequence privilege at all -- `event_id` is
    `GENERATED ALWAYS AS IDENTITY` (D12).
    """

    tenant_id, _ = new_tenant
    await make_provisioner(provisioning_harness, make_registry()).provision(request_for(new_tenant))

    (has_sequence_privilege,) = await admin_row(
        provisioning_harness,
        "SELECT has_sequence_privilege(%s, 'shared.tenant_state_history_event_id_seq', 'USAGE')",
        (PROVISIONER_ROLE,),
    )
    assert has_sequence_privilege is False

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        await conn.execute(f"SET ROLE {PROVISIONER_ROLE}")
        await conn.execute(
            """
            INSERT INTO shared.tenant_state_history
                (tenant_id, prior_state, new_state, reason_code, actor_kind, actor_id, execution_id)
            VALUES (%s, 'active', 'suspended', 'test_append', 'workload', 'test', gen_random_uuid())
            """,
            (tenant_id,),
        )
        for statement in (
            "UPDATE shared.tenant_state_history SET reason_code = 'amended'",
            "DELETE FROM shared.tenant_state_history",
        ):
            with pytest.raises(InsufficientPrivilege):
                await conn.execute(statement)


# --- TC-E25: F-4, the audit sequence -------------------------------------


async def test_neither_audit_role_can_read_the_audit_sequence_but_both_can_insert(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-E25.

    `SELECT` on the sequence let a role denied `SELECT` on the table read
    `last_value`, i.e. the global audit row count across every tenant. Neither
    role needs any sequence privilege: the key is an identity column.
    """

    for role in (AUDIT_PROJECTOR_ROLE, CONTROL_AUDIT_WRITER_ROLE):
        row = await admin_row(
            provisioning_harness,
            """
            SELECT has_sequence_privilege(%s, %s, 'USAGE'),
                   has_sequence_privilege(%s, %s, 'SELECT'),
                   has_sequence_privilege(%s, %s, 'UPDATE'),
                   has_table_privilege(%s, 'shared.access_audit_log', 'INSERT'),
                   has_table_privilege(%s, 'shared.access_audit_log', 'SELECT')
            """,
            (
                role, AUDIT_SEQUENCE,
                role, AUDIT_SEQUENCE,
                role, AUDIT_SEQUENCE,
                role,
                role,
            ),
        )
        assert row == (False, False, False, True, False), role

    # And the insert really does still work without the sequence grant.
    await make_provisioner(provisioning_harness, make_registry()).provision(
        request_for(new_tenant)
    )
    tenant_id = new_tenant[0]
    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[AUDIT_PROJECTOR_ROLE], autocommit=True
    ) as conn:
        await conn.execute(f"SET ROLE {AUDIT_PROJECTOR_ROLE}")
        await conn.execute(
            """
            INSERT INTO shared.access_audit_log
                (source_event_id, tenant_id, principal_kind, principal_id, action_code,
                 resource_class, purpose_code, outcome_code, execution_id, occurred_at)
            VALUES (gen_random_uuid(), %s, 'workload', 'projector-test', 'read',
                    'probe', 'operations', 'allowed', gen_random_uuid(), statement_timestamp())
            """,
            (tenant_id,),
        )
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(f"SELECT last_value FROM {AUDIT_SEQUENCE}")


# --- corrections from the PR-2 code review, 2026-09-01 ---------------------


async def test_the_ddl_and_its_applied_ledger_row_commit_in_one_transaction(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """Review finding 1. The crash window between the two commits is closed.

    Proven by transaction identity, not by timing. The migration records the
    transaction id it runs in; the ledger row's `xmin` is the transaction that
    last wrote it. If those are the same transaction, the DDL and the `applied`
    transition committed together and no crash can land between them.

    Timing cannot prove this. An observation partway through a slow migration
    looks identical under either design, because the window the review describes
    is the microseconds between two commits, not the seconds the DDL takes. An
    earlier version of this test slept partway through and passed against the
    very design it was written to reject.
    """

    tenant_id, schema_key = new_tenant
    await make_provisioner(provisioning_harness, make_registry()).provision(request_for(new_tenant))

    probe = {
        "t001_test_atomic": (
            "CREATE TABLE {schema}.atomic_probe (id integer PRIMARY KEY, ddl_xid bigint NOT NULL);"
            "INSERT INTO {schema}.atomic_probe VALUES (1, pg_current_xact_id()::text::bigint);"
        )
    }
    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        make_registry(probe),
    )
    await runner.apply(tenant_id=tenant_id, schema_key=schema_key)

    ddl_xid, ledger_xmin, state = await admin_row(
        provisioning_harness,
        f"""
        SELECT (SELECT ddl_xid FROM {schema_key}.atomic_probe WHERE id = 1),
               (SELECT xmin::text::bigint FROM shared.schema_migrations
                 WHERE tenant_id = %s AND migration_id = 't001_test_atomic'),
               (SELECT state FROM shared.schema_migrations
                 WHERE tenant_id = %s AND migration_id = 't001_test_atomic')
        """,
        (tenant_id, tenant_id),
    )

    assert state == "applied"
    # `xmin` is a 32-bit xid; pg_current_xact_id() is the 64-bit epoch-extended
    # form of the same number.
    assert ledger_xmin == ddl_xid % 2**32, (
        "the DDL and the `applied` ledger row were written by different "
        "transactions, so a crash can land between their commits"
    )


async def test_a_run_killed_mid_migration_recovers_on_an_ordinary_retry(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """R-E2 resumability against a real killed backend.

    This does not prove finding 1 -- the test above does that, by transaction
    identity -- because a kill lands inside the DDL under either design. What it
    proves is the resumability the finding was about: the tenant comes back with
    a clean schema and a non-terminal ledger row, and an ordinary retry reaches
    `applied` with no operator repair and no hand-written restart contract.
    """

    tenant_id, schema_key = new_tenant
    await make_provisioner(provisioning_harness, make_registry()).provision(request_for(new_tenant))

    slow_ddl = {
        "t001_test_crash": (
            "CREATE TABLE {schema}.crash_probe (id integer PRIMARY KEY);"
            "SELECT pg_sleep(3);"
        )
    }
    registry = make_registry(slow_ddl)
    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]), registry
    )

    async def kill_the_migrator() -> None:
        await asyncio.sleep(1.0)
        async with await AsyncConnection.connect(
            provisioning_harness.admin_conninfo, autocommit=True
        ) as admin:
            await admin.execute(
                """
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE query LIKE '%crash_probe%' AND pid <> pg_backend_pid()
                """
            )

    interrupted, _ = await asyncio.gather(
        runner.apply(tenant_id=tenant_id, schema_key=schema_key),
        kill_the_migrator(),
        return_exceptions=True,
    )
    assert isinstance(interrupted, BaseException)

    state, attempt, _, _, _ = await ledger_row(
        provisioning_harness, tenant_id, "t001_test_crash"
    )
    (schema_clean,) = await admin_row(
        provisioning_harness,
        "SELECT to_regclass(%s) IS NULL",
        (f"{schema_key}.crash_probe",),
    )
    # Not `applied`, and the schema carries nothing the retry would collide with.
    assert state != "applied"
    assert schema_clean is True

    retried = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]), registry
    )
    outcomes = await retried.apply(tenant_id=tenant_id, schema_key=schema_key)

    assert any(outcome.migration_id == "t001_test_crash" and outcome.applied
               for outcome in outcomes)
    final_state, final_attempt, _, final_error, _ = await ledger_row(
        provisioning_harness, tenant_id, "t001_test_crash"
    )
    assert (final_state, final_error) == ("applied", None)
    assert final_attempt > attempt


async def test_the_runner_works_on_a_factory_using_psycopg_default_settings(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """Review finding 2.

    The harness elsewhere passes `autocommit=True`, which concealed the runner's
    dependency on it. This factory uses psycopg's defaults, where the first
    statement opens an implicit transaction and every `transaction()` block below
    it would degrade to a savepoint — so the ledger's intermediate commits would
    not be commits, and closing the connection would discard the sequence.
    """

    tenant_id, schema_key = new_tenant

    def default_factory(conninfo: str) -> Callable[[], Awaitable[AsyncConnection]]:
        async def _connect() -> AsyncConnection:
            return await AsyncConnection.connect(conninfo)  # autocommit=False

        return _connect

    runner = TenantMigrationRunner(
        default_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]), make_registry()
    )
    provisioner = TenantProvisioner(
        default_factory(provisioning_harness.role_logins[PROVISIONER_ROLE]),
        runner,
        supported_schema_versions=range(1, 2),
    )

    outcome = await provisioner.provision(request_for(new_tenant))

    assert outcome.applied_migrations == ("t001_m01_baseline",)
    # Durable, not rolled back with the connection.
    lifecycle, = await admin_row(
        provisioning_harness,
        "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    )
    state, _, _, _, _ = await ledger_row(provisioning_harness, tenant_id, "t001_m01_baseline")
    (outbox,) = await admin_row(
        provisioning_harness,
        "SELECT to_regclass(%s) IS NOT NULL",
        (f"{schema_key}.access_audit_outbox",),
    )
    assert (lifecycle, state, outbox) == ("active", "applied", True)


def test_every_manifest_allow_token_is_explicitly_classified() -> None:
    """Review finding 3. The F-3 control must not fail open on an unknown token."""

    manifest = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    classified = set(TOKEN_TABLE_GRANTS) | set(NON_SHARED_TABLE_TOKENS)
    unclassified = {
        token
        for role, policy in manifest.items()
        if role != OWNER_ROLE
        for token in policy["allow"]
        if token not in classified
    }

    assert unclassified == set(), (
        f"manifest allow tokens with no translation: {sorted(unclassified)}"
    )


def test_the_grant_control_rejects_an_untranslated_token() -> None:
    """Review finding 3, the negative control.

    A misspelled shared token previously translated to no expected privileges, so
    a missing grant went unnoticed. It must now fail loudly instead.
    """

    with pytest.raises(UntranslatedToken):
        _expected_shared_table_grants({"allow": ["shared.tenants:controled_write"], "deny": []})

    # ...and a correctly spelled one still translates.
    expected = _expected_shared_table_grants(
        {"allow": ["shared.tenants:controlled_write"], "deny": []}
    )
    assert expected["tenants"] == frozenset({"SELECT", "INSERT", "UPDATE"})


# --- CP-4: stage 1, the role-safety preflight against a live catalogue ------
#
# TC-P11, TC-P12, TC-P13, TC-P37, TC-P47, TC-P48, TC-P49, TC-P50, TC-P51.
# R-P1B.4, R-P1B.6, R-P1B.7, R-P1B.15; architecture A7.
#
# The comparison rules are covered by unit tests in `test_provisioning.py`,
# which is why they exist: every test in this section needs PostgreSQL 17, so
# without that split no rule could be failed anywhere Claude can run. These
# cover what only a real catalogue can show -- that the queries read the right
# columns, and that a refusal leaves nothing behind.

CP4_EXECUTION_ROLE = "haloflow_test_m02_migrator"

CP4_PROFILE = {
    "login": False,
    "superuser": False,
    "createdb": False,
    "createrole": False,
    "replication": False,
    "bypassrls": False,
    "tenant_schema_privileges": ["CREATE"],
}


def cp4_manifest(**overrides: object) -> object:
    """A manifest declaring one execution role, loaded through the real loader."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    document: dict[str, object] = {
        "execution_role_profiles": {CP4_EXECUTION_ROLE: dict(CP4_PROFILE)},
        "role_memberships": [
            {
                "role": CP4_EXECUTION_ROLE,
                "member": MIGRATOR_ROLE,
                "set": True,
                "inherit": False,
                "admin": False,
            }
        ],
        "tenant_schema_role_privileges": {
            PROVISIONER_ROLE: {
                "privileges": ["USAGE", "CREATE"],
                "is_grantable": False,
                "grantor": PROVISIONER_ROLE,
                "role_class": "owner",
            },
            MIGRATOR_ROLE: {
                "privileges": ["USAGE", "CREATE"],
                "is_grantable": False,
                "grantor": PROVISIONER_ROLE,
                "role_class": "infrastructure",
            },
            RUNTIME_ROLE: {
                "privileges": ["USAGE"],
                "is_grantable": False,
                "grantor": PROVISIONER_ROLE,
                "role_class": "infrastructure",
            },
            AUDIT_PROJECTOR_ROLE: {
                "privileges": ["USAGE"],
                "is_grantable": False,
                "grantor": PROVISIONER_ROLE,
                "role_class": "infrastructure",
            },
        },
        "tenant_table_overrides": [],
    }
    document.update(overrides)
    return load_provisioning_manifest(document)


# The safe attribute set, negations spelled out. `CREATE ROLE` defaults are safe
# for all six, but a default is not a declaration: naming each one means a test
# that flips one attribute differs from the safe shape in exactly that attribute.
SAFE_ROLE_ATTRIBUTES: tuple[str, ...] = (
    "NOLOGIN",
    "NOSUPERUSER",
    "NOCREATEDB",
    "NOCREATEROLE",
    "NOREPLICATION",
    "NOBYPASSRLS",
)

SAFE_ROLE_ATTRIBUTE_SQL = " ".join(SAFE_ROLE_ATTRIBUTES)


def attributes_with(unsafe: str) -> str:
    """The safe attribute set with exactly one attribute flipped on.

    The negation is *replaced*, not appended. PostgreSQL does not resolve
    ``CREATE ROLE ... NOBYPASSRLS LOGIN`` in favour of the later option -- it
    refuses the statement outright as ``conflicting or redundant options`` --
    so an appended attribute produces a syntax error rather than the damaged
    role the test means to observe.
    """

    replaced = tuple(
        unsafe if attribute == f"NO{unsafe}" else attribute
        for attribute in SAFE_ROLE_ATTRIBUTES
    )
    if unsafe not in replaced:
        raise AssertionError(f"{unsafe} is not one of the six attributes this helper knows")
    return " ".join(replaced)


async def shape_execution_role(
    harness: ProvisioningHarness,
    *,
    attributes: str = SAFE_ROLE_ATTRIBUTE_SQL,
    grant: str | None = "SET TRUE, INHERIT FALSE, ADMIN FALSE",
    extra_edge_from: str | None = None,
    via_intermediate: bool = False,
) -> None:
    """Create the execution role in a named shape, dropping any prior one.

    Every damaged shape this section needs is expressible here, so a test says
    which shape it wants rather than carrying its own DDL. The role is dropped
    first so a test never inherits a previous test's damage.
    """

    from psycopg import sql

    intermediate = f"{CP4_EXECUTION_ROLE}_via"
    async with await AsyncConnection.connect(harness.admin_conninfo, autocommit=True) as conn:
        await refuse_on_unowned_role(conn)
        for role in (intermediate, CP4_EXECUTION_ROLE):
            await conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
            )
        await conn.execute(
            sql.SQL("CREATE ROLE {} " + attributes).format(sql.Identifier(CP4_EXECUTION_ROLE))
        )
        if via_intermediate:
            await conn.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(intermediate))
            )
            await conn.execute(
                sql.SQL("GRANT {} TO {} WITH SET TRUE, INHERIT FALSE, ADMIN FALSE").format(
                    sql.Identifier(CP4_EXECUTION_ROLE), sql.Identifier(intermediate)
                )
            )
            await conn.execute(
                sql.SQL("GRANT {} TO {} WITH SET TRUE, INHERIT FALSE, ADMIN FALSE").format(
                    sql.Identifier(intermediate), sql.Identifier(MIGRATOR_ROLE)
                )
            )
        if grant is not None:
            await conn.execute(
                sql.SQL("GRANT {} TO {} WITH " + grant).format(
                    sql.Identifier(CP4_EXECUTION_ROLE), sql.Identifier(MIGRATOR_ROLE)
                )
            )
        if extra_edge_from is not None:
            # `extra_edge_from` must be an **existing controlled role**, and is
            # never created here. Under note-22's both-endpoints scope an edge is
            # governed only when both ends are controlled, so granting to a role
            # this helper invented would produce an edge the control correctly
            # ignores — the test would pass while measuring nothing. Dropping
            # `CP4_EXECUTION_ROLE` removes the edge, so no revoke is needed and
            # no controlled role is ever dropped.
            await conn.execute(
                sql.SQL("GRANT {} TO {} WITH SET TRUE, INHERIT FALSE, ADMIN FALSE").format(
                    sql.Identifier(CP4_EXECUTION_ROLE), sql.Identifier(extra_edge_from)
                )
            )


# The roles this section creates. Fixed names, so the same protection the ACL
# probe needed applies here: a fixed-name drop is only safe if the name could not
# belong to anything else. `haloflow_test_m02_*` is reserved for this file, and
# these helpers refuse rather than drop if one of them turns up already owning an
# object or holding a membership nobody here granted.
CP4_MANAGED_ROLES: tuple[str, ...] = (
    f"{CP4_EXECUTION_ROLE}_via",
    CP4_EXECUTION_ROLE,
)


async def refuse_on_unowned_role(conn: AsyncConnection) -> None:
    """Refuse if a managed name already exists and owns anything.

    An unconditional `DROP ROLE IF EXISTS` on a fixed name destroys whatever
    happens to hold that name — the defect corrected in the ACL probe, applied
    here to the same shape of code. A leftover from a previous run of this file
    owns nothing and is safe to drop; anything else stops the test rather than
    being deleted.
    """

    from psycopg import sql

    for role in CP4_MANAGED_ROLES:
        owned = await (
            await conn.execute(
                """SELECT 1
                     FROM pg_class
                     JOIN pg_roles ON pg_roles.oid = pg_class.relowner
                    WHERE pg_roles.rolname = %s
                    UNION ALL
                   SELECT 1
                     FROM pg_namespace
                     JOIN pg_roles ON pg_roles.oid = pg_namespace.nspowner
                    WHERE pg_roles.rolname = %s
                    LIMIT 1""",
                (role, role),
            )
        ).fetchall()
        if owned:
            raise AssertionError(
                f"{role} exists and owns database objects; refusing to drop it. "
                "This name is reserved for tests in this file — investigate before rerunning."
            )
        assert sql.Identifier(role) is not None  # name is quoted wherever it is used


async def drop_execution_role(harness: ProvisioningHarness) -> None:
    from psycopg import sql

    async with await AsyncConnection.connect(harness.admin_conninfo, autocommit=True) as conn:
        await refuse_on_unowned_role(conn)
        for role in CP4_MANAGED_ROLES:
            await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest_asyncio.fixture
async def cp4_role(provisioning_harness: ProvisioningHarness) -> AsyncIterator[None]:
    """The execution role exists only for the duration of one test."""

    await drop_execution_role(provisioning_harness)
    yield
    await drop_execution_role(provisioning_harness)


# CP-4 tests the declaration and role-safety preflight, not schema-object
# installation. Its deliberately narrow CREATE-only profile lacks USAGE, so a
# schema-qualified CREATE would start testing CP-6 once the runner honors the
# declaration. Keep this seam limited to the catalogue behavior CP-4 owns.
VALID_CP4_TEMPLATE = (
    "SELECT has_schema_privilege(current_role, '{schema}', 'CREATE'), current_role;"
)

# `t002_test_m02` yields `target_version` 2 (the leading `tNNN` of the last
# unit), so every provisioner built over this registry must widen
# `supported_schema_versions` past the `range(1, 2)` default or the request is
# refused with `SCHEMA_VERSION_UNSUPPORTED` before stage 1 is ever reached.
CP4_SUPPORTED_SCHEMA_VERSIONS = range(1, 3)


def make_registry_with_execution_role() -> TenantMigrationRegistry:
    """The baseline plus one unit that declares the execution role.

    The declaration is the point: `assert_execution_roles_safe` collects the
    distinct `execution_role` values in the registry and returns immediately
    when there are none, so a registry built from a bare template string checks
    nothing and reports no refusal.
    """

    from haloflow.m01.provisioning.units import UnitDefinition, build_tenant_migration_registry

    return build_tenant_migration_registry(
        dict(TENANT_MIGRATIONS),
        {
            "t002_test_m02": UnitDefinition(
                VALID_CP4_TEMPLATE, execution_role=CP4_EXECUTION_ROLE
            )
        },
        approved_execution_roles=frozenset({CP4_EXECUTION_ROLE}),
        allow_test_units=True,
    )


@pytest.fixture
def cp4_declared_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare the test execution role in the manifest stage 1 loads for itself.

    The runner loads the shipped `m01/manifests/provisioning.json` during
    construction and passes that object explicitly to stage 1. The provisioner
    adopts the runner's same object for its stage-1 call and stages 2/3. Its
    `execution_role_profiles` block is empty, and must stay empty: a test role
    in a production security declaration is the widening this checkpoint exists
    to prevent.

    Without this fixture every provisioner- and runner-path test below refuses
    at the "composition approved the name; the manifest never described it"
    branch and never reaches the catalogue comparison it is named for. The
    assertion still passes, because the code under test is right either way --
    which is precisely why the substitution is made explicit rather than left
    to coincidence.

    The patch must therefore be installed before the runner is constructed.
    What is substituted is built by `cp4_manifest()` and went
    through the real loader: a validated `ProvisioningManifest`, not a stub. A
    call that supplies its own document still reaches the original loader.
    """

    from haloflow.m01.provisioning import manifest as manifest_module

    original = manifest_module.load_provisioning_manifest
    declared = cp4_manifest()

    def _load(document: object | None = None) -> object:
        return declared if document is None else original(document)  # type: ignore[arg-type]

    monkeypatch.setattr(manifest_module, "load_provisioning_manifest", _load)


async def preflight_against(
    harness: ProvisioningHarness, manifest: object | None = None
) -> str | None:
    """Run stage 1 as the provisioner would. Returns the refusal code, or None."""

    from haloflow.m01.errors import ExecutionRoleUnavailable
    from haloflow.m01.provisioning.role_safety import assert_execution_roles_safe

    registry = make_registry_with_execution_role()
    async with await AsyncConnection.connect(
        harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        try:
            await assert_execution_roles_safe(conn, registry, manifest=manifest or cp4_manifest())
        except ExecutionRoleUnavailable as refusal:
            return refusal.reason_code
    return None


@pytest.mark.asyncio
async def test_the_preflight_fails_when_the_execution_role_does_not_exist(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P11 and TC-P13. Fails before any `running` row is written.

    The role is absent from the catalogue entirely. R-P1B.4 requires the refusal
    to precede the ledger, so a configuration fault is never recorded as a
    tenant migration failure — the two are different problems with different
    fixes, and a `failed` row would send an operator looking at the migration.
    """

    tenant_id, schema_key = new_tenant
    provisioner = make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    )

    with pytest.raises(ProvisioningFailed) as refused:
        await provisioner.provision(request_for(new_tenant))

    assert refused.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE"

    ledger = await admin_rows(
        provisioning_harness,
        "SELECT state FROM shared.schema_migrations WHERE tenant_id = %s",
        (tenant_id,),
    )
    assert ledger == []

    schema = await admin_rows(
        provisioning_harness,
        "SELECT 1 FROM pg_namespace WHERE nspname = %s",
        (schema_key,),
    )
    assert schema == []


@pytest.mark.asyncio
async def test_the_preflight_fails_a_role_granted_with_set_false(
    provisioning_harness: ProvisioningHarness, cp4_role: None
) -> None:
    """TC-P12. `WITH SET FALSE` satisfies MEMBER and cannot `SET ROLE` (V8).

    The measurement this test exists for: asking `pg_has_role(..., 'MEMBER')`
    would pass this exact configuration, and the runner would then attempt a
    `SET ROLE` the database refuses — mid-DDL, inside a tenant transaction.
    """

    await shape_execution_role(
        provisioning_harness, grant="SET FALSE, INHERIT FALSE, ADMIN FALSE"
    )

    assert await preflight_against(provisioning_harness) == "EXECUTION_ROLE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_the_preflight_fails_on_each_unsafe_role_attribute_in_turn(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P47. Each attribute alone, and after each: no schema grant, no `running` row.

    An allow-listed name is not a safe role. The composition allow-list is fixed
    at startup; if the role later gained SUPERUSER the runner would assume it
    deliberately, which is why this reads the catalogue rather than the manifest.
    """

    tenant_id, schema_key = new_tenant
    unsafe = ("LOGIN", "SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS")

    for attribute in unsafe:
        await shape_execution_role(
            provisioning_harness, attributes=attributes_with(attribute)
        )

        provisioner = make_provisioner(
            provisioning_harness,
            make_registry_with_execution_role(),
            supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
        )
        with pytest.raises(ProvisioningFailed) as refused:
            await provisioner.provision(request_for(new_tenant))
        assert refused.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE", attribute

        # No schema grant exists for the role: `nspacl` carries no entry for it,
        # which also means the schema itself was never created.
        acl = await admin_rows(
            provisioning_harness,
            """SELECT 1 FROM pg_namespace ns,
                      LATERAL aclexplode(ns.nspacl) AS entry
                LEFT JOIN pg_roles grantee ON grantee.oid = entry.grantee
                    WHERE ns.nspname = %s AND grantee.rolname = %s""",
            (schema_key, CP4_EXECUTION_ROLE),
        )
        assert acl == [], attribute

        ledger = await admin_rows(
            provisioning_harness,
            "SELECT state FROM shared.schema_migrations WHERE tenant_id = %s",
            (tenant_id,),
        )
        assert ledger == [], attribute


@pytest.mark.asyncio
async def test_the_preflight_fails_on_each_membership_option_in_turn(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P48. `SET FALSE`, `INHERIT TRUE`, `ADMIN TRUE` — same two assertions.

    `INHERIT TRUE` is a failure rather than a convenience (V15): the migrator's
    ordinary statements would silently carry the execution role's privileges.
    `ADMIN TRUE` would let the migrator grant the role onward.
    """

    tenant_id, schema_key = new_tenant
    mismatches = (
        "SET FALSE, INHERIT FALSE, ADMIN FALSE",
        "SET TRUE, INHERIT TRUE, ADMIN FALSE",
        "SET TRUE, INHERIT FALSE, ADMIN TRUE",
    )

    for grant in mismatches:
        await shape_execution_role(provisioning_harness, grant=grant)

        provisioner = make_provisioner(
            provisioning_harness,
            make_registry_with_execution_role(),
            supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
        )
        with pytest.raises(ProvisioningFailed) as refused:
            await provisioner.provision(request_for(new_tenant))
        assert refused.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE", grant

        acl = await admin_rows(
            provisioning_harness,
            """SELECT 1 FROM pg_namespace ns,
                      LATERAL aclexplode(ns.nspacl) AS entry
                LEFT JOIN pg_roles grantee ON grantee.oid = entry.grantee
                    WHERE ns.nspname = %s AND grantee.rolname = %s""",
            (schema_key, CP4_EXECUTION_ROLE),
        )
        assert acl == [], grant

        ledger = await admin_rows(
            provisioning_harness,
            "SELECT state FROM shared.schema_migrations WHERE tenant_id = %s",
            (tenant_id,),
        )
        assert ledger == [], grant


@pytest.mark.asyncio
async def test_the_preflight_rejects_transitive_only_satisfaction(
    provisioning_harness: ProvisioningHarness, cp4_role: None
) -> None:
    """TC-P49. The migrator reaches the role, but no direct declared edge exists.

    `pg_has_role(..., 'SET')` is true here — the capability check passes — and
    the structural check must still refuse. That is why R-P1B.4 asks for both:
    either alone would accept a path nobody declared.
    """

    await shape_execution_role(provisioning_harness, grant=None, via_intermediate=True)

    assert await preflight_against(provisioning_harness) == "EXECUTION_ROLE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_an_undeclared_membership_edge_fails_the_exact_set_control(
    provisioning_harness: ProvisioningHarness, cp4_role: None
) -> None:
    """TC-P37 and TC-P50, second instance. The graph equals the declaration.

    The declared edge is present and correct. A second, undeclared edge into the
    execution role is what fails — an undeclared path into an execution role is a
    path nobody reviewed, and the pre-R-P1B.7 control would have permitted it.

    The extra edge comes from `haloflow_runtime`, a controlled role, and that is
    load-bearing rather than incidental: note-22 scopes the control to edges with
    **both** endpoints controlled, so the invented `_extra` role this test used
    before the ruling would now produce an edge the control legitimately ignores.
    The test would still have passed, for no reason.
    """

    await shape_execution_role(provisioning_harness, extra_edge_from=RUNTIME_ROLE)

    assert await preflight_against(provisioning_harness) == "EXECUTION_ROLE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_an_edge_between_two_infrastructure_roles_fails_the_preflight(
    provisioning_harness: ProvisioningHarness, cp4_role: None
) -> None:
    """TC-P50 proper, and the case the first CP-4 draft could not see.

    `GRANT haloflow_migrator TO haloflow_runtime` lets the runtime role assume
    the migrator. Neither endpoint is the registry's execution role, so the
    per-execution-role query that shipped in the first draft never selected this
    edge and no comparison considered it. It is exactly the escalation TC-E19 was
    written to forbid, which is why R-P1B.7 says that control *becomes* this one.

    Revoked in a `finally`: a leaked escalation would fail every later test in
    this file, and the failure would look like anything but its cause.
    """

    from psycopg import sql

    await shape_execution_role(provisioning_harness)

    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        await conn.execute(
            sql.SQL("GRANT {} TO {} WITH SET TRUE, INHERIT FALSE, ADMIN FALSE").format(
                sql.Identifier(MIGRATOR_ROLE), sql.Identifier(RUNTIME_ROLE)
            )
        )
    try:
        assert await preflight_against(provisioning_harness) == "EXECUTION_ROLE_UNAVAILABLE"
    finally:
        async with await AsyncConnection.connect(
            provisioning_harness.admin_conninfo, autocommit=True
        ) as conn:
            await conn.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(MIGRATOR_ROLE), sql.Identifier(RUNTIME_ROLE)
                )
            )


@pytest.mark.asyncio
async def test_an_edge_between_two_uncontrolled_roles_passes_the_preflight(
    provisioning_harness: ProvisioningHarness, cp4_role: None
) -> None:
    """R-P1B.7 case 6. The control is scoped, not cluster-wide.

    Two roles no declaration names are not HaloFlow's business, and a control
    that failed on them would fail on every unrelated grant in the cluster. This
    is also the boundary the deferred either-endpoint question sits on: it is a
    deliberate scope, argued in note-22, not an oversight.

    It is asserted alongside a *correctly* configured execution role, so a pass
    here means the control ran and accepted, not that it never ran.
    """

    from psycopg import sql

    await shape_execution_role(provisioning_harness)
    outsiders = ("haloflow_test_m02_outsider_a", "haloflow_test_m02_outsider_b")

    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        for role in outsiders:
            await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            await conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        await conn.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(outsiders[0]), sql.Identifier(outsiders[1])
            )
        )
    try:
        assert await preflight_against(provisioning_harness) is None
    finally:
        async with await AsyncConnection.connect(
            provisioning_harness.admin_conninfo, autocommit=True
        ) as conn:
            for role in outsiders:
                await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.mark.asyncio
async def test_a_correctly_configured_execution_role_passes_the_preflight(
    provisioning_harness: ProvisioningHarness, cp4_role: None
) -> None:
    """The positive control. Without it every assertion above could pass vacuously."""

    await shape_execution_role(provisioning_harness)

    assert await preflight_against(provisioning_harness) is None


@pytest.mark.asyncio
async def test_set_true_inherit_false_keeps_set_while_usage_is_false(
    provisioning_harness: ProvisioningHarness, cp4_role: None
) -> None:
    """TC-P51 (C). V15, characterized against the live catalogue.

    The declared edge deliberately keeps `SET` while denying `USAGE`, so the
    migrator can assume the role explicitly and does not silently carry its
    privileges. This is the measurement the declaration rests on; if PostgreSQL
    ever changed it, R-P1B.3's `INHERIT FALSE` would stop meaning what A7 says.
    """

    await shape_execution_role(provisioning_harness)

    row = await admin_row(
        provisioning_harness,
        "SELECT pg_has_role(%s, %s, 'SET'), pg_has_role(%s, %s, 'USAGE')",
        (MIGRATOR_ROLE, CP4_EXECUTION_ROLE, MIGRATOR_ROLE, CP4_EXECUTION_ROLE),
    )

    assert row[0] is True
    assert row[1] is False


@pytest.mark.asyncio
async def test_the_runner_is_guarded_on_the_same_terms_as_the_provisioner(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """R-P1B.15. One component, two call sites — the runner invoked on its own.

    A runner reached outside a provisioning flow must not assume a role nobody
    checked. It raises in its own taxonomy, not the provisioner's, because a
    shared helper raising one caller's exception type is how a provisioning call
    once came to fail as a migration error.
    """

    tenant_id, schema_key = new_tenant
    await shape_execution_role(
        provisioning_harness, grant="SET FALSE, INHERIT FALSE, ADMIN FALSE"
    )

    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        make_registry_with_execution_role(),
    )

    with pytest.raises(TenantMigrationFailed) as refused:
        await runner.apply(tenant_id=tenant_id, schema_key=schema_key)

    assert refused.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE"


# --- CP-5b: manifest-driven schema grant installation --------------------


@pytest.mark.asyncio
async def test_schema_grant_installer_expands_only_the_manifest_declaration(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P46: M01 installs a module role without naming it in production code."""

    from haloflow.m01.provisioning.acl import (
        SchemaAclEntry,
        build_expected_schema_acl,
        install_schema_acl,
        read_schema_acl,
    )

    await shape_execution_role(provisioning_harness)
    manifest = cp4_manifest()
    _, schema_key = new_tenant

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))
        async with conn.transaction():
            await install_schema_acl(conn, schema_key, manifest)
        observed = await read_schema_acl(conn, schema_key)

    literal_expected = frozenset(
        {
            SchemaAclEntry(PROVISIONER_ROLE, "CREATE", False, PROVISIONER_ROLE),
            SchemaAclEntry(PROVISIONER_ROLE, "USAGE", False, PROVISIONER_ROLE),
            SchemaAclEntry(MIGRATOR_ROLE, "CREATE", False, PROVISIONER_ROLE),
            SchemaAclEntry(MIGRATOR_ROLE, "USAGE", False, PROVISIONER_ROLE),
            SchemaAclEntry(RUNTIME_ROLE, "USAGE", False, PROVISIONER_ROLE),
            SchemaAclEntry(AUDIT_PROJECTOR_ROLE, "USAGE", False, PROVISIONER_ROLE),
            SchemaAclEntry(CP4_EXECUTION_ROLE, "CREATE", False, PROVISIONER_ROLE),
        }
    )
    assert observed == literal_expected
    assert observed == build_expected_schema_acl(manifest)


@pytest.mark.asyncio
async def test_schema_grant_installer_participates_in_the_callers_transaction(
    provisioning_harness: ProvisioningHarness,
    new_tenant: tuple[str, str],
) -> None:
    """R-P1B.19 seam: CP-5c can roll back grant and postcondition together."""

    from haloflow.m01.provisioning.acl import (
        build_expected_schema_acl,
        install_schema_acl,
        read_schema_acl,
    )
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    _, schema_key = new_tenant
    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))

        with pytest.raises(RuntimeError, match="force rollback"):
            async with conn.transaction():
                manifest = load_provisioning_manifest()
                await install_schema_acl(conn, schema_key, manifest)
                assert await read_schema_acl(conn, schema_key) == build_expected_schema_acl(
                    manifest
                )
                raise RuntimeError("force rollback")

        assert await read_schema_acl(conn, schema_key) == frozenset()


def test_schema_grant_installer_is_reachable_only_from_the_provisioner_path() -> None:
    """CP5b-G1 transition: CP-5c activates the installer only with its guard."""

    provisioner = (M01_ROOT / "provisioning/provisioner.py").read_text()
    runner = (M01_ROOT / "provisioning/runner.py").read_text()
    assert "install_schema_acl" in provisioner
    assert "read_schema_acl" in provisioner
    assert "install_schema_acl" not in runner
    assert "read_schema_acl" not in runner


@pytest.mark.asyncio
async def test_migrator_cannot_grant_schema_create_to_the_execution_role(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P43 (C): the pre-CP5b grant location cannot work (V12)."""

    await shape_execution_role(provisioning_harness)
    _, schema_key = new_tenant
    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[MIGRATOR_ROLE], autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(MIGRATOR_ROLE)))
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(
                sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema_key), sql.Identifier(CP4_EXECUTION_ROLE)
                )
            )

    row = await admin_row(
        provisioning_harness,
        "SELECT has_schema_privilege(%s, %s, 'CREATE')",
        (CP4_EXECUTION_ROLE, schema_key),
    )
    assert row == (False,)


@pytest.mark.asyncio
async def test_execution_role_without_schema_create_cannot_install_ddl(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P44 (C): assuming the role is not a fallback for the missing grant."""

    await shape_execution_role(provisioning_harness)
    _, schema_key = new_tenant
    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[PROVISIONER_ROLE], autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))

    async with await AsyncConnection.connect(
        provisioning_harness.role_logins[MIGRATOR_ROLE], autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(CP4_EXECUTION_ROLE)))
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(
                sql.SQL("CREATE TABLE {}.cp5b_probe (id integer)").format(
                    sql.Identifier(schema_key)
                )
            )

    assert await admin_row(
        provisioning_harness,
        "SELECT to_regclass(%s) IS NULL",
        (f"{schema_key}.cp5b_probe",),
    ) == (True,)


@pytest.mark.asyncio
async def test_production_runner_leaves_the_complete_schema_acl_unchanged(
    provisioning_harness: ProvisioningHarness,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P71 (C): the entire production registry preserves stage 2's ACL."""

    from haloflow.m01.provisioning.acl import (
        install_schema_acl,
        read_schema_acl,
    )
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    tenant_id, schema_key = new_tenant
    manifest = load_provisioning_manifest()
    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        await conn.execute(
            """
            INSERT INTO shared.tenants
                (tenant_id, schema_key, lifecycle_state, schema_version)
            VALUES (%s, %s, 'provisioning', 1)
            """,
            (tenant_id, schema_key),
        )
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))
        async with conn.transaction():
            await install_schema_acl(conn, schema_key, manifest)
        before = await read_schema_acl(conn, schema_key)

    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        build_production_tenant_migrations(),
    )
    await runner.apply(tenant_id=tenant_id, schema_key=schema_key)

    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        after = await read_schema_acl(conn, schema_key)

    assert after == before


# --- CP-5c: atomic schema-ACL activation (tests frozen before production) --

CP5C_ACL_MISMATCH_CODE = SanitizedErrorCode.SCHEMA_ACL_MISMATCH.value


async def schema_acl(harness: ProvisioningHarness, schema_key: str) -> frozenset[object]:
    """Read the complete four-dimensional ACL through the production reader."""

    from haloflow.m01.provisioning.acl import read_schema_acl

    async with await AsyncConnection.connect(harness.admin_conninfo, autocommit=True) as conn:
        return await read_schema_acl(conn, schema_key)


async def tenant_ledger_snapshot(
    harness: ProvisioningHarness, tenant_id: str
) -> list[tuple]:
    """Capture every durable ledger field CP-5c is forbidden to change."""

    return await admin_rows(
        harness,
        """
        SELECT migration_id, checksum, state, attempt,
               sanitized_error_code, started_at, completed_at
          FROM shared.schema_migrations
         WHERE tenant_id = %s
         ORDER BY migration_id
        """,
        (tenant_id,),
    )


@pytest.mark.asyncio
async def test_cp5c_installs_and_verifies_acl_before_any_runner_ledger_write(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P45: stages 2/3 finish exactly before stage 4 can touch the ledger."""

    from haloflow.m01.provisioning.acl import build_expected_schema_acl

    await shape_execution_role(provisioning_harness)
    tenant_id, schema_key = new_tenant
    registry = make_registry_with_execution_role()
    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]), registry
    )
    original_apply = runner.apply_within_lock
    reached_runner = False

    async def _observe_boundary(*, tenant_id: str, schema_key: str) -> object:
        nonlocal reached_runner
        reached_runner = True
        assert await schema_acl(provisioning_harness, schema_key) == build_expected_schema_acl(
            cp4_manifest()
        )
        assert await tenant_ledger_snapshot(provisioning_harness, tenant_id) == []
        return await original_apply(tenant_id=tenant_id, schema_key=schema_key)

    monkeypatch.setattr(runner, "apply_within_lock", _observe_boundary)
    provisioner = TenantProvisioner(
        connection_factory(provisioning_harness.role_logins[PROVISIONER_ROLE]),
        runner,
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    )

    outcome = await provisioner.provision(request_for(new_tenant))
    assert reached_runner is True
    assert outcome.schema_version == 2


@pytest.mark.asyncio
async def test_cp5c_unsafe_declared_role_never_receives_create_and_retry_converges(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P59: stage 1 leaves no residue; repairing the role makes retry succeed."""

    from haloflow.m01.provisioning.acl import build_expected_schema_acl

    tenant_id, schema_key = new_tenant
    await shape_execution_role(
        provisioning_harness, attributes=attributes_with("SUPERUSER")
    )
    provisioner = make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    )

    with pytest.raises(ProvisioningFailed) as refused:
        await provisioner.provision(request_for(new_tenant))
    assert refused.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE"
    assert await schema_acl(provisioning_harness, schema_key) == frozenset()
    assert await tenant_ledger_snapshot(provisioning_harness, tenant_id) == []
    assert await admin_rows(
        provisioning_harness,
        "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    ) == []

    await shape_execution_role(provisioning_harness)
    outcome = await provisioner.provision(request_for(new_tenant))
    assert outcome.resumed is False
    assert await admin_row(
        provisioning_harness,
        "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    ) == ("active",)
    assert await schema_acl(provisioning_harness, schema_key) == build_expected_schema_acl(
        cp4_manifest()
    )


@pytest.mark.asyncio
async def test_cp5c_overbroad_stage3_grant_rolls_back_all_new_acl_and_ledger_state(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P60: a postcondition mismatch rolls back the whole grant transaction."""

    from haloflow.m01.provisioning import provisioner as provisioner_module
    from haloflow.m01.provisioning.acl import install_schema_acl

    await shape_execution_role(provisioning_harness)
    tenant_id, schema_key = new_tenant

    async def _install_overbroad(connection: object, key: str, manifest: object) -> None:
        await install_schema_acl(connection, key, manifest)  # type: ignore[arg-type]
        await connection.execute(  # type: ignore[attr-defined]
            sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
                sql.Identifier(key), sql.Identifier(RUNTIME_ROLE)
            )
        )

    monkeypatch.setattr(
        provisioner_module, "install_schema_acl", _install_overbroad, raising=False
    )
    provisioner = make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    )

    with pytest.raises(ProvisioningFailed) as refused:
        await provisioner.provision(request_for(new_tenant))
    assert refused.value.reason_code == CP5C_ACL_MISMATCH_CODE
    assert await schema_acl(provisioning_harness, schema_key) == frozenset()
    assert await tenant_ledger_snapshot(provisioning_harness, tenant_id) == []


async def assert_cp5c_catalogue_mismatch_rolls_back(
    harness: ProvisioningHarness,
    tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[frozenset[object]], frozenset[object]],
    *,
    expected_ledger: list[tuple] | None = None,
) -> None:
    """Present one synthetic catalogue mismatch at stage 3 and assert residue."""

    from haloflow.m01.provisioning import provisioner as provisioner_module
    from haloflow.m01.provisioning.acl import read_schema_acl

    async def _drifted_read(connection: object, schema_key: str) -> frozenset[object]:
        return mutate(await read_schema_acl(connection, schema_key))

    monkeypatch.setattr(provisioner_module, "read_schema_acl", _drifted_read, raising=False)
    tenant_id, schema_key = tenant
    provisioner = make_provisioner(
        harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    )
    with pytest.raises(ProvisioningFailed) as refused:
        await provisioner.provision(request_for(tenant))
    assert refused.value.reason_code == CP5C_ACL_MISMATCH_CODE
    assert await schema_acl(harness, schema_key) == frozenset()
    assert await tenant_ledger_snapshot(harness, tenant_id) == (expected_ledger or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ("extra_usage", "missing_usage", "second_role"))
async def test_cp5c_acl_postcondition_is_exact_not_a_presence_check(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """TC-P61: surplus, deficit, and a second principal each fail equality."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)

    def _mutate(actual: frozenset[object]) -> frozenset[object]:
        usage = SchemaAclEntry(CP4_EXECUTION_ROLE, "USAGE", False, PROVISIONER_ROLE)
        migrator_usage = SchemaAclEntry(MIGRATOR_ROLE, "USAGE", False, PROVISIONER_ROLE)
        stray = SchemaAclEntry(RUNTIME_ROLE, "CREATE", False, PROVISIONER_ROLE)
        if shape == "extra_usage":
            return actual | {usage}
        if shape == "missing_usage":
            return actual - {migrator_usage}
        return actual | {stray}

    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness, new_tenant, monkeypatch, _mutate
    )


@pytest.mark.asyncio
async def test_cp5c_acl_postcondition_compares_grant_option(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P62: matching privilege names with delegation still fail closed."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)

    def _mutate(actual: frozenset[object]) -> frozenset[object]:
        plain = SchemaAclEntry(CP4_EXECUTION_ROLE, "CREATE", False, PROVISIONER_ROLE)
        delegated = SchemaAclEntry(CP4_EXECUTION_ROLE, "CREATE", True, PROVISIONER_ROLE)
        return (actual - {plain}) | {delegated}

    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness, new_tenant, monkeypatch, _mutate
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("grantee", ("PUBLIC", OWNER_ROLE))
async def test_cp5c_acl_postcondition_rejects_every_undeclared_grantee(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    grantee: str,
) -> None:
    """TC-P63: PUBLIC and an undeclared role are both first-class ACL rows."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness,
        new_tenant,
        monkeypatch,
        lambda actual: actual
        | {SchemaAclEntry(grantee, "USAGE", False, PROVISIONER_ROLE)},
    )


@pytest.mark.asyncio
async def test_cp5c_complete_five_class_acl_activates(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P65: the complete declaration, including projector, passes live."""

    from haloflow.m01.provisioning.acl import build_expected_schema_acl

    await shape_execution_role(provisioning_harness)
    outcome = await make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    ).provision(request_for(new_tenant))
    assert outcome.schema_version == 2
    assert await schema_acl(provisioning_harness, new_tenant[1]) == build_expected_schema_acl(
        cp4_manifest()
    )
    assert await admin_row(
        provisioning_harness,
        "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s",
        (new_tenant[0],),
    ) == ("active",)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ("grant_without_declaration", "declaration_without_grant"))
async def test_cp5c_missing_schema_acl_declaration_fails_in_both_directions(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    """TC-P66: neither an undeclared grant nor an ungranted declaration passes."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    projector = SchemaAclEntry(AUDIT_PROJECTOR_ROLE, "USAGE", False, PROVISIONER_ROLE)
    undeclared = SchemaAclEntry(OWNER_ROLE, "USAGE", False, PROVISIONER_ROLE)
    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness,
        new_tenant,
        monkeypatch,
        (lambda actual: actual | {undeclared})
        if direction == "grant_without_declaration"
        else (lambda actual: actual - {projector}),
    )


@pytest.mark.asyncio
async def test_cp5c_extra_database_grantee_cannot_be_masked_by_expected_data(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P67(a): an extra live grantee always fails the public path."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness,
        new_tenant,
        monkeypatch,
        lambda actual: actual
        | {SchemaAclEntry("PUBLIC", "CREATE", False, PROVISIONER_ROLE)},
    )


@pytest.mark.asyncio
async def test_cp5c_runtime_create_is_rejected_by_live_postcondition(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P68 postgres half: runtime is USAGE-only, never a DDL principal."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness,
        new_tenant,
        monkeypatch,
        lambda actual: actual
        | {SchemaAclEntry(RUNTIME_ROLE, "CREATE", False, PROVISIONER_ROLE)},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ("narrower", "delegated"))
async def test_cp5c_migrator_privileges_match_the_declaration_exactly(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """TC-P69: migrator deficit and delegation widening both fail."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    create = SchemaAclEntry(MIGRATOR_ROLE, "CREATE", False, PROVISIONER_ROLE)
    delegated = SchemaAclEntry(MIGRATOR_ROLE, "CREATE", True, PROVISIONER_ROLE)
    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness,
        new_tenant,
        monkeypatch,
        (lambda actual: actual - {create})
        if shape == "narrower"
        else (lambda actual: (actual - {create}) | {delegated}),
    )


@pytest.mark.asyncio
async def test_cp5c_owner_baseline_and_grantor_are_compared_explicitly(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P70: owner tuples are not skipped and grantor is not inferred."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    expected = SchemaAclEntry(PROVISIONER_ROLE, "CREATE", False, PROVISIONER_ROLE)
    wrong = SchemaAclEntry(PROVISIONER_ROLE, "CREATE", False, MIGRATOR_ROLE)
    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness,
        new_tenant,
        monkeypatch,
        lambda actual: (actual - {expected}) | {wrong},
    )


async def prepare_cp5c_resume_state(
    harness: ProvisioningHarness,
    tenant: tuple[str, str],
    *,
    install_grants: bool = False,
) -> None:
    """Commit the durable boundary left by an interrupted stage 2 or stage 4."""

    from haloflow.m01.provisioning.acl import install_schema_acl

    tenant_id, schema_key = tenant
    async with await AsyncConnection.connect(harness.admin_conninfo, autocommit=True) as conn:
        await conn.execute(
            """
            INSERT INTO shared.tenants
                (tenant_id, schema_key, lifecycle_state, schema_version)
            VALUES (%s, %s, 'provisioning', 2)
            """,
            (tenant_id, schema_key),
        )
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))
        if install_grants:
            async with conn.transaction():
                await install_schema_acl(conn, schema_key, cp4_manifest())


async def assert_cp5c_tenant_is_refused_at_resolution(
    harness: ProvisioningHarness, tenant_id: str
) -> None:
    """Pin the caller-visible consequence of remaining in provisioning."""

    pool = TenantPool(harness.role_logins[RUNTIME_ROLE], min_size=1, max_size=1)
    await pool.open()
    try:
        resolver = TenantResolver(
            PsycopgControlStore(pool),
            supported_schema_versions=range(1, 3),
            context_ttl=timedelta(seconds=10),
        )
        with pytest.raises(TenantUnavailable) as refused:
            await resolver.resolve(
                principal=Principal(
                    kind=PrincipalKind.WORKLOAD,
                    id="cp5c-resolution-test",
                    auth_method="test",
                    authorized_tenant_ids=frozenset({tenant_id}),
                    capabilities=frozenset({"probe:read"}),
                ),
                tenant_hint=tenant_id,
                purpose="operations",
                capabilities=frozenset({"probe:read"}),
                source=TrustedSource.WORKER,
                execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:cp5c:{tenant_id}"),
                correlation_id=uuid5(
                    NAMESPACE_URL, f"haloflow-test:cp5c-correlation:{tenant_id}"
                ),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
            )
        assert refused.value.reason_code == "TENANT_NOT_ACTIVE"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_cp5c_resume_from_committed_schema_without_grants_activates(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P73: an empty-ACL schema is the ordinary repairable resume state."""

    from haloflow.m01.provisioning.acl import build_expected_schema_acl

    await shape_execution_role(provisioning_harness)
    await prepare_cp5c_resume_state(provisioning_harness, new_tenant)
    assert await schema_acl(provisioning_harness, new_tenant[1]) == frozenset()
    outcome = await make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    ).provision(request_for(new_tenant))
    assert outcome.resumed is True
    assert await schema_acl(provisioning_harness, new_tenant[1]) == build_expected_schema_acl(
        cp4_manifest()
    )


@pytest.mark.asyncio
async def test_cp5c_resume_regrants_exact_acl_byte_identically(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P74: PostgreSQL's idempotence, not an application branch, preserves nspacl."""

    from haloflow.m01.provisioning import provisioner as provisioner_module
    from haloflow.m01.provisioning.acl import install_schema_acl

    await shape_execution_role(provisioning_harness)
    await prepare_cp5c_resume_state(
        provisioning_harness, new_tenant, install_grants=True
    )
    query = "SELECT nspacl::text FROM pg_namespace WHERE nspname = %s"
    before = await admin_row(provisioning_harness, query, (new_tenant[1],))
    stage2_called = False

    async def _observed_install(connection: object, key: str, manifest: object) -> None:
        nonlocal stage2_called
        stage2_called = True
        await install_schema_acl(connection, key, manifest)  # type: ignore[arg-type]

    monkeypatch.setattr(
        provisioner_module, "install_schema_acl", _observed_install, raising=False
    )
    outcome = await make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    ).provision(request_for(new_tenant))
    after = await admin_row(provisioning_harness, query, (new_tenant[1],))
    assert outcome.resumed is True
    assert stage2_called is True
    assert after == before


@pytest.mark.asyncio
async def test_cp5c_stage3_failure_preserves_preexisting_ledger_byte_for_byte(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-P75: stage 2/3 neither creates nor edits migration history."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    await prepare_cp5c_resume_state(provisioning_harness, new_tenant)
    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        await conn.execute(
            """
            INSERT INTO shared.schema_migrations
                (tenant_id, migration_id, checksum, state, attempt,
                 sanitized_error_code, completed_at)
            VALUES (%s, 'prior_failed', 'prior-checksum-a', 'failed', 3,
                    'MIGRATION_DDL_FAILED', statement_timestamp()),
                   (%s, 'prior_running', 'prior-checksum-b', 'running', 2, NULL, NULL)
            """,
            (new_tenant[0], new_tenant[0]),
        )
    before = await tenant_ledger_snapshot(provisioning_harness, new_tenant[0])
    await assert_cp5c_catalogue_mismatch_rolls_back(
        provisioning_harness,
        new_tenant,
        monkeypatch,
        lambda actual: actual
        | {SchemaAclEntry("PUBLIC", "USAGE", False, PROVISIONER_ROLE)},
        expected_ledger=before,
    )
    assert await tenant_ledger_snapshot(provisioning_harness, new_tenant[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ("public", "undeclared_role"))
async def test_cp5c_committed_acl_drift_survives_refusal_and_tenant_stays_inactive(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    drift: str,
) -> None:
    """TC-P76: stage 2 issues no REVOKE and stage 3 performs no repair."""

    from haloflow.m01.provisioning.acl import SchemaAclEntry

    await shape_execution_role(provisioning_harness)
    await prepare_cp5c_resume_state(provisioning_harness, new_tenant)
    grantee = "PUBLIC" if drift == "public" else OWNER_ROLE
    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        target = sql.SQL("PUBLIC") if grantee == "PUBLIC" else sql.Identifier(grantee)
        await conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(new_tenant[1]), target
            )
        )
    drift_entry = SchemaAclEntry(grantee, "USAGE", False, PROVISIONER_ROLE)
    provisioner = make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    )
    with pytest.raises(ProvisioningFailed) as refused:
        await provisioner.provision(request_for(new_tenant))
    assert refused.value.reason_code == CP5C_ACL_MISMATCH_CODE
    assert drift_entry in await schema_acl(provisioning_harness, new_tenant[1])
    assert await admin_row(
        provisioning_harness,
        "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s",
        (new_tenant[0],),
    ) == ("provisioning",)
    if drift == "public":
        await assert_cp5c_tenant_is_refused_at_resolution(
            provisioning_harness, new_tenant[0]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_state", ("empty_acl", "exact_acl", "prior_history", "stage1"))
async def test_cp5c_each_repairable_state_converges_on_retry(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
    resume_state: str,
) -> None:
    """TC-P77: all documented repairable boundaries reach one exact outcome."""

    from haloflow.m01.provisioning.acl import build_expected_schema_acl

    if resume_state == "stage1":
        await shape_execution_role(
            provisioning_harness, attributes=attributes_with("SUPERUSER")
        )
        provisioner = make_provisioner(
            provisioning_harness,
            make_registry_with_execution_role(),
            supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
        )
        with pytest.raises(ProvisioningFailed):
            await provisioner.provision(request_for(new_tenant))
    else:
        await shape_execution_role(provisioning_harness)
        await prepare_cp5c_resume_state(
            provisioning_harness,
            new_tenant,
            install_grants=resume_state == "exact_acl",
        )
        if resume_state == "prior_history":
            async with await AsyncConnection.connect(
                provisioning_harness.admin_conninfo, autocommit=True
            ) as conn:
                await conn.execute(
                    """INSERT INTO shared.schema_migrations
                       (tenant_id, migration_id, checksum, state, attempt,
                        sanitized_error_code, completed_at)
                       VALUES (%s, 'prior_failed', 'prior', 'failed', 1,
                               'MIGRATION_DDL_FAILED', statement_timestamp())""",
                    (new_tenant[0],),
                )
    if resume_state == "stage1":
        await shape_execution_role(provisioning_harness)
    outcome = await make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    ).provision(request_for(new_tenant))
    assert outcome.schema_version == 2
    assert await schema_acl(provisioning_harness, new_tenant[1]) == build_expected_schema_acl(
        cp4_manifest()
    )


@pytest.mark.asyncio
async def test_cp5c_schema_owner_and_all_grant_options_match_the_declaration(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp4_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P79: ownership and non-delegation are catalogue facts, not assumptions."""

    from haloflow.m01.provisioning.acl import build_expected_schema_acl

    await shape_execution_role(provisioning_harness)
    await make_provisioner(
        provisioning_harness,
        make_registry_with_execution_role(),
        supported_schema_versions=CP4_SUPPORTED_SCHEMA_VERSIONS,
    ).provision(request_for(new_tenant))
    assert await admin_row(
        provisioning_harness,
        """SELECT owner.rolname
             FROM pg_namespace ns JOIN pg_roles owner ON owner.oid = ns.nspowner
            WHERE ns.nspname = %s""",
        (new_tenant[1],),
    ) == (PROVISIONER_ROLE,)
    observed = await schema_acl(provisioning_harness, new_tenant[1])
    assert observed == build_expected_schema_acl(cp4_manifest())
    assert observed
    assert all(entry.is_grantable is False for entry in observed)  # type: ignore[attr-defined]
    assert all(
        entry.grantee not in {MIGRATOR_ROLE, CP4_EXECUTION_ROLE}  # type: ignore[attr-defined]
        or entry.grantor == PROVISIONER_ROLE  # type: ignore[attr-defined]
        for entry in observed
    )


def test_cp5c_live_provisioning_path_cannot_bypass_stage3() -> None:
    """CP5c-E1: every live runner call is guarded by install plus exact readback."""

    source = (M01_ROOT / "provisioning/provisioner.py").read_text()
    runner_source = (M01_ROOT / "provisioning/runner.py").read_text()
    assert source.count("apply_within_lock(") == 1
    assert "install_schema_acl" in source
    assert "read_schema_acl" in source
    assert "_apply_grants" not in source
    assert "install_schema_acl" not in runner_source
    assert "read_schema_acl" not in runner_source


# --- CP-6: transaction-local execution-role switching ---------------------


def make_cp6_registry(
    *units: tuple[str, str, str | None],
) -> TenantMigrationRegistry:
    """Build the baseline plus ordered CP-6 test units."""

    from haloflow.m01.provisioning.units import UnitDefinition

    definitions = {
        migration_id: UnitDefinition(template, execution_role=execution_role)
        for migration_id, template, execution_role in units
    }
    return build_tenant_migration_registry(
        dict(TENANT_MIGRATIONS),
        definitions,
        approved_execution_roles=frozenset({CP4_EXECUTION_ROLE}),
        allow_test_units=True,
    )


@pytest.fixture
def cp6_declared_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare the execution role with the privileges its CP-6 DDL exercises.

    CP-4 deliberately uses CREATE without USAGE to prove PostgreSQL keeps the
    privileges independent. These CP-6 units use schema-qualified object names,
    so their validated declaration requests both privileges.
    """

    from haloflow.m01.provisioning import manifest as manifest_module

    original = manifest_module.load_provisioning_manifest
    profile = dict(CP4_PROFILE)
    profile["tenant_schema_privileges"] = ["USAGE", "CREATE"]
    declared = cp4_manifest(
        execution_role_profiles={CP4_EXECUTION_ROLE: profile}
    )

    def _load(document: object | None = None) -> object:
        return declared if document is None else original(document)  # type: ignore[arg-type]

    monkeypatch.setattr(manifest_module, "load_provisioning_manifest", _load)


@pytest.mark.asyncio
async def test_cp6_unit_without_declared_role_is_owned_by_the_migrator(
    provisioning_harness: ProvisioningHarness,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P1 (R): absence of a declaration preserves migrator execution."""

    registry = make_cp6_registry(
        (
            "t002_test_cp6_default_role",
            "CREATE TABLE {schema}.cp6_default_role (id integer PRIMARY KEY);",
            None,
        )
    )
    await make_provisioner(
        provisioning_harness,
        registry,
        supported_schema_versions=range(1, 3),
    ).provision(request_for(new_tenant))

    assert await admin_row(
        provisioning_harness,
        """SELECT owner.rolname
             FROM pg_class relation
             JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
             JOIN pg_roles owner ON owner.oid = relation.relowner
            WHERE namespace.nspname = %s AND relation.relname = 'cp6_default_role'""",
        (new_tenant[1],),
    ) == (MIGRATOR_ROLE,)


@pytest.mark.asyncio
async def test_cp6_declared_execution_role_owns_its_objects(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp6_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P2: a declared, approved role executes the unit's DDL."""

    await shape_execution_role(provisioning_harness)
    registry = make_cp6_registry(
        (
            "t002_test_cp6_declared_role",
            "CREATE TABLE {schema}.cp6_declared_role (id integer PRIMARY KEY);",
            CP4_EXECUTION_ROLE,
        )
    )
    await make_provisioner(
        provisioning_harness,
        registry,
        supported_schema_versions=range(1, 3),
    ).provision(request_for(new_tenant))

    assert await admin_row(
        provisioning_harness,
        """SELECT owner.rolname
             FROM pg_class relation
             JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
             JOIN pg_roles owner ON owner.oid = relation.relowner
            WHERE namespace.nspname = %s AND relation.relname = 'cp6_declared_role'""",
        (new_tenant[1],),
    ) == (CP4_EXECUTION_ROLE,)


@pytest.mark.asyncio
async def test_cp6_execution_role_change_is_checksum_drift_on_reapplication(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp6_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P6: the database path enforces the checksum's role binding."""

    await shape_execution_role(provisioning_harness)
    migration_id = "t002_test_cp6_role_drift"
    template = "CREATE TABLE {schema}.cp6_role_drift (id integer PRIMARY KEY);"
    without_role = make_cp6_registry((migration_id, template, None))
    await make_provisioner(
        provisioning_harness,
        without_role,
        supported_schema_versions=range(1, 3),
    ).provision(request_for(new_tenant))

    with_role = make_cp6_registry((migration_id, template, CP4_EXECUTION_ROLE))
    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        with_role,
    )
    with pytest.raises(TenantMigrationFailed) as drift:
        await runner.apply(tenant_id=new_tenant[0], schema_key=new_tenant[1])

    assert drift.value.reason_code == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value


@pytest.mark.asyncio
async def test_cp6_returns_explicitly_to_migrator_before_the_next_unit(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp6_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P7: a later undeclared unit runs as migrator, never the login shim."""

    await shape_execution_role(provisioning_harness)
    registry = make_cp6_registry(
        (
            "t002_test_cp6_assumed",
            "CREATE TABLE {schema}.cp6_assumed (id integer PRIMARY KEY);",
            CP4_EXECUTION_ROLE,
        ),
        (
            "t003_test_cp6_returned",
            """CREATE TABLE {schema}.cp6_returned (
                   id integer PRIMARY KEY,
                   observed_role text NOT NULL DEFAULT current_role
               );
               INSERT INTO {schema}.cp6_returned (id) VALUES (1);""",
            None,
        ),
    )
    await make_provisioner(
        provisioning_harness,
        registry,
        supported_schema_versions=range(1, 4),
    ).provision(request_for(new_tenant))

    assumed_owner = await admin_row(
        provisioning_harness,
        """SELECT owner.rolname
             FROM pg_class relation
             JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
             JOIN pg_roles owner ON owner.oid = relation.relowner
            WHERE namespace.nspname = %s AND relation.relname = 'cp6_assumed'""",
        (new_tenant[1],),
    )
    returned_owner, observed_role = await admin_row(
        provisioning_harness,
        f"""SELECT owner.rolname, probe.observed_role
              FROM {new_tenant[1]}.cp6_returned probe
              JOIN pg_class relation ON relation.relname = 'cp6_returned'
              JOIN pg_namespace namespace
                ON namespace.oid = relation.relnamespace
               AND namespace.nspname = %s
              JOIN pg_roles owner ON owner.oid = relation.relowner
             WHERE probe.id = 1""",
        (new_tenant[1],),
    )
    assert assumed_owner == (CP4_EXECUTION_ROLE,)
    assert returned_owner == MIGRATOR_ROLE
    assert observed_role == MIGRATOR_ROLE


@pytest.mark.asyncio
async def test_cp6_role_switched_ddl_and_applied_row_share_one_transaction(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp6_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P8: role switching preserves PR-2's DDL/ledger atomic boundary."""

    await shape_execution_role(provisioning_harness)
    migration_id = "t002_test_cp6_atomic"
    registry = make_cp6_registry(
        (
            migration_id,
            """CREATE TABLE {schema}.cp6_atomic (ddl_xid bigint NOT NULL);
               INSERT INTO {schema}.cp6_atomic
               VALUES (pg_current_xact_id()::text::bigint);""",
            CP4_EXECUTION_ROLE,
        )
    )
    await make_provisioner(
        provisioning_harness,
        registry,
        supported_schema_versions=range(1, 3),
    ).provision(request_for(new_tenant))

    owner, ddl_xid, ledger_xmin = await admin_row(
        provisioning_harness,
        f"""SELECT (SELECT owner.rolname
                       FROM pg_class relation
                       JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                       JOIN pg_roles owner ON owner.oid = relation.relowner
                      WHERE namespace.nspname = %s
                        AND relation.relname = 'cp6_atomic'),
                   (SELECT ddl_xid FROM {new_tenant[1]}.cp6_atomic),
                   (SELECT xmin::text::bigint FROM shared.schema_migrations
                     WHERE tenant_id = %s AND migration_id = %s)""",
        (new_tenant[1], new_tenant[0], migration_id),
    )
    assert owner == CP4_EXECUTION_ROLE
    assert ledger_xmin == ddl_xid % 2**32


@pytest.mark.asyncio
async def test_cp6_failed_assumed_role_does_not_leak_into_the_next_run(
    provisioning_harness: ProvisioningHarness,
    cp4_role: None,
    cp6_declared_manifest: None,
    new_tenant: tuple[str, str],
) -> None:
    """TC-P9: rollback discards the local role; retry executes as migrator."""

    await shape_execution_role(provisioning_harness)
    await make_provisioner(provisioning_harness).provision(request_for(new_tenant))
    migration_id = "t002_test_cp6_failed_role"
    failing = make_cp6_registry(
        (
            migration_id,
            """CREATE TABLE {schema}.cp6_rolled_back (id integer PRIMARY KEY);
               SELECT 1 / CASE
                   WHEN current_role = 'haloflow_test_m02_migrator' THEN 0
                   ELSE 1
               END;""",
            CP4_EXECUTION_ROLE,
        )
    )
    runner = TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        failing,
    )
    with pytest.raises(TenantMigrationFailed) as failed:
        await runner.apply(tenant_id=new_tenant[0], schema_key=new_tenant[1])
    assert failed.value.reason_code == SanitizedErrorCode.MIGRATION_DDL_FAILED.value
    assert await admin_row(
        provisioning_harness,
        """SELECT state, sanitized_error_code
             FROM shared.schema_migrations
            WHERE tenant_id = %s AND migration_id = %s""",
        (new_tenant[0], migration_id),
    ) == ("failed", SanitizedErrorCode.MIGRATION_DDL_FAILED.value)

    retry = make_cp6_registry(
        (
            migration_id,
            """CREATE TABLE {schema}.cp6_after_failure (
                   id integer PRIMARY KEY,
                   observed_role text NOT NULL DEFAULT current_role
               );
               INSERT INTO {schema}.cp6_after_failure (id) VALUES (1);""",
            None,
        )
    )
    await TenantMigrationRunner(
        connection_factory(provisioning_harness.role_logins[MIGRATOR_ROLE]),
        retry,
    ).apply(tenant_id=new_tenant[0], schema_key=new_tenant[1])

    owner, observed_role = await admin_row(
        provisioning_harness,
        f"""SELECT owner.rolname, probe.observed_role
              FROM {new_tenant[1]}.cp6_after_failure probe
              JOIN pg_class relation ON relation.relname = 'cp6_after_failure'
              JOIN pg_namespace namespace
                ON namespace.oid = relation.relnamespace
               AND namespace.nspname = %s
              JOIN pg_roles owner ON owner.oid = relation.relowner
             WHERE probe.id = 1""",
        (new_tenant[1],),
    )
    assert owner == MIGRATOR_ROLE
    assert observed_role == MIGRATOR_ROLE


def test_cp6_runner_never_emits_reset_role() -> None:
    """TC-P10 (G): explicit return is the only accepted implementation shape."""

    source = (M01_ROOT / "provisioning/runner.py").read_text()
    assert "RESET ROLE" not in source


# --- CP-7b: live function metadata verification ---------------------------

def cp7b_function(**changes):
    from haloflow.m01.provisioning.verification import AclEntry, FunctionExpectation

    values = dict(name="cp7b_probe", argument_types=("integer", "text[]"),
                  owner=MIGRATOR_ROLE, security_definer=True,
                  config=("search_path=pg_catalog",),
                  acl=(AclEntry(MIGRATOR_ROLE, ("EXECUTE",)),), body="SELECT 1")
    return FunctionExpectation(**(values | changes))


def cp7b_ddl(*, body="SELECT 1", security="SECURITY DEFINER",
             config="SET search_path = pg_catalog", acl=True, extra="",
             arguments="integer, text[]", prefix=""):
    revoke = ("REVOKE ALL ON FUNCTION {schema}.cp7b_probe(" + arguments +
              ") FROM PUBLIC;") if acl else ""
    return ("-- cp7b fixture\n" + prefix +
            "CREATE TABLE {schema}.cp7b_marker (ddl_xid bigint);"
            "INSERT INTO {schema}.cp7b_marker VALUES (pg_current_xact_id()::text::bigint);"
            "CREATE FUNCTION {schema}.cp7b_probe(" + arguments + ") RETURNS integer "
            "LANGUAGE sql " + security + " " + config + " AS $body$" + body + "$body$;" +
            revoke + extra)


def cp7b_registry(template, *functions, execution_role=None):
    from haloflow.m01.provisioning.units import UnitDefinition
    from haloflow.m01.provisioning.verification import FunctionMetadataVerification

    return build_tenant_migration_registry(
        dict(TENANT_MIGRATIONS),
        {"t002_test_cp7b": UnitDefinition(template, execution_role=execution_role,
            verification=FunctionMetadataVerification(tuple(functions)))},
        approved_execution_roles=frozenset({CP4_EXECUTION_ROLE}), allow_test_units=True)


@pytest.fixture
def cp7b_observe(monkeypatch, new_tenant):
    """Observe successful DDL before verification, and the actual verification call.

    The independent snapshot proves fixture setup succeeded. The query guard
    runs before forwarding verification SQL to PostgreSQL, including mutants.
    """
    from haloflow.m01.provisioning import verification

    original = AsyncConnection.execute
    observations = {"ddl": [], "verification": []}

    async def execute(connection, query, params=None, **kwargs):
        if (observations.get("require_order") and observations["ddl"]
                and isinstance(query, str) and "SET state = 'applied'" in query):
            assert observations["verification"], "verification must precede applied"
        if isinstance(query, str) and "M01 function metadata verification" in query:
            assert query == verification.FUNCTION_METADATA_QUERY
            assert params == (new_tenant[1],)
            role = await (await original(connection, "SELECT current_role")).fetchone()
            assert role == (MIGRATOR_ROLE,)
            observations["verification"].append((query, params))
        result = await original(connection, query, params, **kwargs)
        if isinstance(query, str) and query.startswith("-- cp7b fixture"):
            rows = await (await original(connection, """
                SELECT p.proname, r.rolname, p.prosecdef, p.proconfig,
                       p.proacl IS NULL, p.prosrc,
                       ARRAY(SELECT a.grantee::text || ':' || a.privilege_type || ':' ||
                             a.is_grantable::text FROM pg_catalog.aclexplode(p.proacl) a),
                       ARRAY(SELECT pg_catalog.format_type(t.oid, NULL)
                             FROM pg_catalog.unnest(p.proargtypes) WITH ORDINALITY t(oid, pos)
                             ORDER BY t.pos)
                  FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n
                    ON n.oid=p.pronamespace
                  JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
                 WHERE n.nspname=%s ORDER BY p.proname
                """, (new_tenant[1],))).fetchall()
            observations["ddl"].extend(rows)
        return result

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    return observations


async def cp7b_assert_failed(harness, tenant, registry, observed):
    with pytest.raises(TenantMigrationFailed) as error:
        await make_provisioner(harness, registry,
                              supported_schema_versions=range(1, 3)).provision(request_for(tenant))
    assert error.value.reason_code == "VERIFICATION_FAILED"
    assert observed["ddl"], "DDL must have succeeded before the verifier refused it"
    row = await ledger_row(harness, tenant[0], "t002_test_cp7b")
    assert (row[0], row[1], row[3]) == ("failed", 1, "VERIFICATION_FAILED")
    assert await admin_row(harness,
        "SELECT to_regclass(%s), lifecycle_state FROM shared.tenants WHERE tenant_id=%s",
        (tenant[1] + ".cp7b_marker", tenant[0])) == (None, "provisioning")
    assert await admin_row(harness,
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=%s AND p.proname LIKE 'cp7b%%'", (tenant[1],)) == (0,)
    # No function body, supplied type text, or schema in the public error chain.
    assert tenant[1] not in str(error.value)
    assert "SELECT" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["owner", "security", "config_missing", "config_wrong",
    "config_extra", "acl_null", "acl_public", "acl_extra", "acl_missing", "acl_grantable",
    "body", "body_whitespace", "missing", "argument_order", "alias_int4", "alias_qualified",
    "hostile", "acl_owner_omitted", "argument_sentinel"])
async def test_cp7b_metadata_mismatch_rolls_back(
    provisioning_harness, new_tenant, cp7b_observe, fault
):
    """TC-P16–P22/P58, F1/F2: isolate each mismatch after successful DDL."""
    function = cp7b_function()
    options = {}
    if fault == "owner":
        function = cp7b_function(owner=OWNER_ROLE)
    elif fault == "security":
        options["security"] = "SECURITY INVOKER"
    elif fault == "config_missing":
        options["config"] = ""
    elif fault == "config_wrong":
        options["config"] = "SET search_path = public"
    elif fault == "config_extra":
        options["config"] = "SET search_path = pg_catalog SET work_mem = '4MB'"
    elif fault == "acl_null":
        options["acl"] = False
    elif fault in {"acl_public", "acl_extra", "acl_grantable"}:
        grantee = "PUBLIC" if fault == "acl_public" else RUNTIME_ROLE
        options["extra"] = ("GRANT EXECUTE ON FUNCTION {schema}.cp7b_probe(integer,text[]) TO " +
                            grantee +
                            (" WITH GRANT OPTION" if fault == "acl_grantable" else "") + ";")
        if fault == "acl_grantable":
            from haloflow.m01.provisioning.verification import AclEntry
            function = cp7b_function(acl=(AclEntry(MIGRATOR_ROLE, ("EXECUTE",)),
                                        AclEntry(RUNTIME_ROLE, ("EXECUTE",))))
    elif fault == "acl_missing":
        from haloflow.m01.provisioning.verification import AclEntry
        function = cp7b_function(acl=(AclEntry(MIGRATOR_ROLE, ("EXECUTE",)),
                                    AclEntry(RUNTIME_ROLE, ("EXECUTE",))))
    elif fault == "acl_owner_omitted":
        function = cp7b_function(acl=())
    elif fault == "argument_sentinel":
        function = cp7b_function(argument_types=("{schema}.cp7b_type", "text[]"))
    elif fault == "body":
        options["body"] = "SELECT 2"
    elif fault == "body_whitespace":
        options["body"] = "SELECT  1"
    elif fault == "missing":
        function = cp7b_function(name="cp7b_absent")
    elif fault == "argument_order":
        function = cp7b_function(argument_types=("text[]", "integer"))
    elif fault.startswith("alias"):
        function = cp7b_function(argument_types=(
            "int4" if fault == "alias_int4" else "pg_catalog.int4", "text[]"))
    elif fault == "hostile":
        function = cp7b_function(argument_types=(
            "integer); SELECT 1/0; -- {schema}", "text[]"))
    await cp7b_assert_failed(provisioning_harness, new_tenant,
        cp7b_registry(cp7b_ddl(**options), function), cp7b_observe)
    row = cp7b_observe["ddl"][0]
    assert row[0] == "cp7b_probe" and row[1] == MIGRATOR_ROLE
    assert row[2] is (fault != "security")
    assert row[4] is (fault == "acl_null")
    assert row[5] == options.get("body", "SELECT 1")
    if fault == "acl_public":
        assert "0:EXECUTE:false" in row[6]
    if fault == "acl_extra":
        assert len(row[6]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [
    "exact", "unrelated", "overload", "rendered", "normalized", "empty_acl"])
async def test_cp7b_matching_metadata_is_verified_and_applied_atomically(
    provisioning_harness, new_tenant, cp7b_observe, variant
):
    """TC-P15/P21/P22/P58, F8: positive path must actually execute verification."""
    function = cp7b_function()
    options = {}
    cp7b_observe["require_order"] = True
    if variant in {"unrelated", "overload"}:
        identity = "cp7b_other()" if variant == "unrelated" else "cp7b_probe(text)"
        options["extra"] = ("CREATE FUNCTION {schema}." + identity +
                            " RETURNS integer LANGUAGE sql AS 'SELECT 9';")
    elif variant == "rendered":
        body = "SELECT 1 /* {schema} {ordinary} */"
        options["body"] = body
        function = cp7b_function(body=body, config=("search_path={schema}, pg_catalog",))
        options["config"] = "SET search_path = {schema}, pg_catalog"
    elif variant == "normalized":
        options["body"] = "SELECT 1 /* cafe\u0301\r\n */"
        function = cp7b_function(body="SELECT 1 /* café\n */")
    elif variant == "empty_acl":
        options["extra"] = (
            "REVOKE ALL ON FUNCTION {schema}.cp7b_probe(integer,text[]) FROM haloflow_migrator;")
        function = cp7b_function(acl=())
    registry = cp7b_registry(cp7b_ddl(**options), function)
    await make_provisioner(provisioning_harness, registry,
        supported_schema_versions=range(1, 3)).provision(request_for(new_tenant))
    assert cp7b_observe["verification"], "declared verification must be executed before applied"
    if variant == "empty_acl":
        assert cp7b_observe["ddl"][0][4] is False
        assert cp7b_observe["ddl"][0][6] == []
    assert await admin_row(provisioning_harness,
        "SELECT state, sanitized_error_code FROM shared.schema_migrations "
        "WHERE tenant_id=%s AND migration_id='t002_test_cp7b'",
        (new_tenant[0],)) == ("applied", None)
    assert await admin_row(provisioning_harness,
        f"SELECT m.xmin::text::bigint = probe.ddl_xid %% 4294967296 "
        f"FROM shared.schema_migrations m CROSS JOIN {new_tenant[1]}.cp7b_marker probe "
        "WHERE m.tenant_id=%s AND m.migration_id='t002_test_cp7b'", (new_tenant[0],)) == (True,)


@pytest.mark.asyncio
async def test_cp7b_ddl_search_path_cannot_change_argument_identity(
    provisioning_harness, new_tenant, cp7b_observe
):
    """F7/F2: tenant enum and array stay schema-qualified after DDL changes path."""
    arguments = "{schema}.cp7b_type, {schema}.cp7b_type[]"
    ddl = cp7b_ddl(arguments=arguments,
        prefix="CREATE TYPE {schema}.cp7b_type AS ENUM ('a');",
        extra="SET LOCAL search_path = {schema}, pg_catalog;")
    function = cp7b_function(argument_types=(new_tenant[1] + ".cp7b_type",
                                             new_tenant[1] + ".cp7b_type[]"))
    await make_provisioner(provisioning_harness, cp7b_registry(ddl, function),
        supported_schema_versions=range(1, 3)).provision(request_for(new_tenant))
    assert cp7b_observe["verification"], "path-independent verification was not exercised"
    assert cp7b_observe["ddl"][0][7] == ["cp7b_type", "cp7b_type[]"]


@pytest.mark.asyncio
async def test_cp7b_verification_runs_after_execution_role_return(
    provisioning_harness, cp4_role, cp6_declared_manifest, new_tenant, cp7b_observe
):
    """R-P2.8/F8: observe migrator at verification after execution-role DDL."""
    await shape_execution_role(provisioning_harness)
    from haloflow.m01.provisioning.verification import AclEntry
    function = cp7b_function(owner=CP4_EXECUTION_ROLE,
                             acl=(AclEntry(CP4_EXECUTION_ROLE, ("EXECUTE",)),))
    await make_provisioner(provisioning_harness,
        cp7b_registry(cp7b_ddl(), function, execution_role=CP4_EXECUTION_ROLE),
        supported_schema_versions=range(1, 3)).provision(request_for(new_tenant))
    assert cp7b_observe["verification"]


@pytest.mark.asyncio
async def test_cp7b_failed_verification_is_refused_by_real_resolver(
    provisioning_harness, new_tenant, cp7b_observe
):
    """TC-P29: the refusal originates in verification, then resolver fails closed."""
    await cp7b_assert_failed(provisioning_harness, new_tenant,
        cp7b_registry(cp7b_ddl(body="SELECT 2"), cp7b_function()), cp7b_observe)
    pool = TenantPool(provisioning_harness.role_logins[RUNTIME_ROLE], min_size=1, max_size=1)
    await pool.open()
    try:
        resolver = TenantResolver(PsycopgControlStore(pool), supported_schema_versions=range(1, 3),
                                  context_ttl=timedelta(seconds=10))
        with pytest.raises(TenantUnavailable) as error:
            await resolver.resolve(
                principal=Principal(kind=PrincipalKind.WORKLOAD, id="cp7b-test", auth_method="test",
                    authorized_tenant_ids=frozenset({new_tenant[0]}),
                    capabilities=frozenset({"probe:read"})),
                tenant_hint=new_tenant[0], purpose="operations",
                capabilities=frozenset({"probe:read"}),
                source=TrustedSource.WORKER, execution_id=uuid5(NAMESPACE_URL, "cp7b-resolver"),
                correlation_id=uuid5(NAMESPACE_URL, "cp7b-correlation"),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)
        assert error.value.reason_code == "TENANT_NOT_ACTIVE"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_cp7b_body_whitespace_and_migration_whitespace_do_not_cross(
    provisioning_harness, new_tenant, cp7b_observe
):
    """TC-P58: independent checksum and live body-comparison consequences."""
    from haloflow.m01.provisioning.checksum import unit_checksum

    ddl = cp7b_ddl()
    assert unit_checksum(migration_id="t002_test_cp7b", template=ddl) == unit_checksum(
        migration_id="t002_test_cp7b", template=ddl.replace("CREATE TABLE", "CREATE  TABLE"))
    first = cp7b_registry(ddl, cp7b_function())
    spaced = cp7b_registry(ddl, cp7b_function(body="SELECT  1"))
    assert tuple(first)[-1].checksum != tuple(spaced)[-1].checksum
    await cp7b_assert_failed(provisioning_harness, new_tenant, spaced, cp7b_observe)


# --- CP-8: tenant table grants, kept separate from schema ACLs and edges ---


def _cp8_expected_table_grants(
    permissions: dict[str, dict[str, list[str]]],
    manifest: object,
    tables: Sequence[str],
) -> dict[tuple[str, str, str], bool]:
    """Expand per-token grants, replacing only the token an override narrows.

    Ownership is asserted independently by the caller. The baseline's creator
    is the migrator: its owner privileges are not checksummed_ddl token grants.
    No role is removed from the matrix, including that owner. Ordinary owner
    privileges are revocable: ownership alone does not make these cells true.
    The seven owner-revocation cases separately prove that distinction (C3).

    Standing table expectations come from recognized allow tokens and structured
    overrides. Deny lists are outside this SQL-grant control: they also describe
    login, ownership and approval restrictions, not a table-ACL subtraction DSL.
    In particular, no deny token is needed to make conditional support authority
    confer zero standing grants. This does not claim general deny-policy coverage.
    """
    from haloflow.m01.provisioning.manifest import classify_tenant_schema_token

    overrides = {
        (item.role, item.table, item.narrows): item.privileges
        for item in manifest.tenant_table_overrides
    }
    result = {}
    for role, policy in permissions.items():
        for table in tables:
            grants: set[str] = set()
            for token in policy["allow"]:
                if not token.startswith(("tenant_schema:", "tenant_schema.")):
                    continue
                privileges = classify_tenant_schema_token(token)
                if token.startswith("tenant_schema."):
                    named_table = token.split(":", 1)[0].split(".", 1)[1]
                    if named_table != table:
                        continue
                grants.update(overrides.get((role, table, token), privileges))
            if role == MIGRATOR_ROLE:
                grants.update(TABLE_PRIVILEGES)
            for privilege in TABLE_PRIVILEGES:
                result[(role, table, privilege)] = privilege in grants
    return result


async def _cp8_table_matrix(
    connection: AsyncConnection, schema_key: str, roles: Sequence[str]
) -> tuple[list[str], dict[tuple[str, str, str], bool]]:
    """R-P3 checks effective privileges, including PUBLIC/inherited access.

    Unlike the D21 schema-ACL control, this seven-privilege matrix makes no
    grantor or grant-option assertion. R-P3 specifies effective table access;
    schema delegation provenance is a separate contract, not silently extended
    to tables here. The separate controlled membership graph remains enforced.
    """
    rows = await (
        await connection.execute(
            """SELECT c.relname, pg_get_userbyid(c.relowner)
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY c.relname""",
            (schema_key,),
        )
    ).fetchall()
    assert rows, "a zero-table matrix is not evidence"
    assert {owner for _, owner in rows} == {MIGRATOR_ROLE}
    tables = [name for name, _ in rows]
    actual = {}
    for role in roles:
        for table in tables:
            for privilege in TABLE_PRIVILEGES:
                row = await (
                    await connection.execute(
                        "SELECT has_table_privilege(%s, format('%%I.%%I', %s::text, %s::text), %s)",
                        (role, schema_key, table, privilege),
                    )
                ).fetchone()
                assert row is not None
                actual[(role, table, privilege)] = row[0]
    assert len(actual) == len(roles) * len(tables) * 7
    return tables, actual


@pytest.mark.parametrize(
    "token",
    ["approved_support_read", "approved_emergency_read", "separately_approved_emergency_write"],
)
def test_cp8_approval_capabilities_confer_no_standing_table_grant(token: str) -> None:
    """R-P3.5 / TC-P35: conditional authority is not a standing SQL grant."""
    from haloflow.m01.provisioning.manifest import classify_tenant_schema_token

    assert classify_tenant_schema_token(f"tenant_schema:{token}") == ()


async def test_cp8_every_role_table_and_privilege_matches_manifest(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str]
) -> None:
    """TC-P30: real provisioned schema, every declared role, seven privileges."""
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    await make_provisioner(
        provisioning_harness,
        make_registry(
            {"t002_test_cp8_business": "CREATE TABLE {schema}.business_probe (id integer)"}
        ),
        supported_schema_versions=range(1, 3),
    ).provision(request_for(new_tenant))
    permissions = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    manifest = load_provisioning_manifest()
    assert set(permissions) == manifest.controlled_roles
    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn:
        tables, actual = await _cp8_table_matrix(conn, new_tenant[1], sorted(permissions))
    assert tables == ["access_audit_outbox", "business_probe"]
    assert actual == _cp8_expected_table_grants(permissions, manifest, tables)


@pytest.mark.parametrize(
    "privilege", ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
)
async def test_cp8_override_detects_each_deficit_or_excess_on_second_table(
    provisioning_harness: ProvisioningHarness, new_tenant: tuple[str, str], privilege: str
) -> None:
    """TC-P32/P33/P38: second-table drift cannot hide behind a correct first table.

    This control isolates runtime so the independent support-token defect cannot
    be the producer of its mismatch. Every tamper has its own catalogue witness.
    """
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    await make_provisioner(provisioning_harness).provision(request_for(new_tenant))
    schema_key = new_tenant[1]
    permissions = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    runtime = {RUNTIME_ROLE: permissions[RUNTIME_ROLE]}
    manifest = load_provisioning_manifest()
    table_id = sql.Identifier(schema_key, "operation_registry")
    async with await AsyncConnection.connect(
        provisioning_harness.admin_conninfo, autocommit=True
    ) as conn, conn.transaction():
        # Everything this probe changes is rolled back, including the new table.
        await conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(MIGRATOR_ROLE)))
        await conn.execute(sql.SQL("CREATE TABLE {} (id integer)").format(table_id))
        tables, inherited = await _cp8_table_matrix(conn, schema_key, [RUNTIME_ROLE])
        assert tables == ["access_audit_outbox", "operation_registry"]
        assert {
            p for p in TABLE_PRIVILEGES if inherited[(RUNTIME_ROLE, "operation_registry", p)]
        } == {"SELECT", "INSERT", "UPDATE", "DELETE"}
        expected = _cp8_expected_table_grants(runtime, manifest, tables)
        assert expected[(RUNTIME_ROLE, "operation_registry", "SELECT")]
        assert expected[(RUNTIME_ROLE, "operation_registry", "INSERT")]
        assert not expected[(RUNTIME_ROLE, "operation_registry", "UPDATE")]
        assert not expected[(RUNTIME_ROLE, "operation_registry", "DELETE")]
        assert inherited != expected
        await conn.execute(
            sql.SQL("REVOKE UPDATE, DELETE ON {} FROM {}").format(
                table_id, sql.Identifier(RUNTIME_ROLE)
            )
        )
        _, correct = await _cp8_table_matrix(conn, schema_key, [RUNTIME_ROLE])
        assert correct == expected
        verb = "REVOKE" if privilege in {"SELECT", "INSERT"} else "GRANT"
        direction = "FROM" if verb == "REVOKE" else "TO"
        await conn.execute(
            sql.SQL("{} {} ON {} {} {}").format(
                sql.SQL(verb),
                sql.SQL(privilege),
                table_id,
                sql.SQL(direction),
                sql.Identifier(RUNTIME_ROLE),
            )
        )
        _, drifted = await _cp8_table_matrix(conn, schema_key, [RUNTIME_ROLE])
        differences = {key for key in expected if drifted[key] != expected[key]}
        assert differences == {(RUNTIME_ROLE, "operation_registry", privilege)}
        await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # Explicit rollback prevents the probe becoming fixture state.
        from psycopg import Rollback

        raise Rollback()


@pytest.mark.parametrize(
    "role", ["haloflow_support_ro", "haloflow_breakglass_ro", "haloflow_breakglass_rw"]
)
@pytest.mark.parametrize("privilege", TABLE_PRIVILEGES)
async def test_cp8_support_and_breakglass_have_no_schema_or_table_privileges(
    provisioning_harness: ProvisioningHarness,
    new_tenant: tuple[str, str],
    role: str,
    privilege: str,
) -> None:
    """TC-P35: direct catalogue evidence, independent of token translation."""
    from psycopg import Rollback

    await make_provisioner(provisioning_harness).provision(request_for(new_tenant))
    schema_key = new_tenant[1]
    async with (
        await AsyncConnection.connect(provisioning_harness.admin_conninfo, autocommit=True) as conn,
        conn.transaction(),
    ):
        _, clean = await _cp8_table_matrix(conn, schema_key, [role])
        assert not any(clean.values())
        schema = await (
            await conn.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE'), "
                "has_schema_privilege(%s, %s, 'CREATE')",
                (role, schema_key, role, schema_key),
            )
        ).fetchone()
        assert schema == (False, False)
        await conn.execute(
            sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(privilege),
                sql.Identifier(schema_key, "access_audit_outbox"),
                sql.Identifier(role),
            )
        )
        _, drifted = await _cp8_table_matrix(conn, schema_key, [role])
        assert {key for key, held in drifted.items() if held} == {
            (role, "access_audit_outbox", privilege)
        }
        assert drifted != clean
        raise Rollback()


@pytest.mark.parametrize(
    "extra_token,table,narrows,privileges",
    [
        (
            "tenant_schema:business_runtime",
            "operation_registry",
            "tenant_schema:business_dml",
            ["SELECT", "INSERT"],
        ),
        (
            "tenant_schema:write",
            "operation_registry",
            "tenant_schema:business_dml",
            ["SELECT", "INSERT"],
        ),
        (
            "tenant_schema.operation_registry:insert",
            "operation_registry",
            "tenant_schema:business_dml",
            ["SELECT"],
        ),
        (None, "operation_registry", "tenant_schema.access_audit_outbox:insert", []),
    ],
)
def test_cp8_loader_refuses_rewidening_or_wrong_table_narrowing(
    monkeypatch: pytest.MonkeyPatch,
    extra_token: str | None,
    table: str,
    narrows: str,
    privileges: list[str],
) -> None:
    """C1 / A5: a locally valid subset cannot become a decorative override."""
    from haloflow.m01.errors import MigrationManifestRejected
    from haloflow.m01.provisioning import manifest as manifest_module

    permissions = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    document = json.loads((M01_ROOT / "manifests/provisioning.json").read_text())
    # Establish a healthy loader before introducing only the hazard under test.
    assert manifest_module.load_provisioning_manifest(document)
    if extra_token:
        permissions[RUNTIME_ROLE]["allow"].append(extra_token)
    else:
        # Isolate table targeting: no second token may trigger re-widening.
        permissions[RUNTIME_ROLE]["allow"] = [narrows]
    document["tenant_table_overrides"] = [
        {
            "role": RUNTIME_ROLE,
            "table": table,
            "narrows": narrows,
            "privileges": privileges,
        }
    ]
    assert narrows in permissions[RUNTIME_ROLE]["allow"]
    assert set(privileges) < set(manifest_module.classify_tenant_schema_token(narrows))
    monkeypatch.setattr(manifest_module, "_permissions_document", lambda: permissions)
    if extra_token is None:
        valid_target = json.loads(json.dumps(document))
        valid_target["tenant_table_overrides"][0]["table"] = "access_audit_outbox"
        assert manifest_module.load_provisioning_manifest(valid_target)
    with pytest.raises(MigrationManifestRejected) as error:
        manifest_module.load_provisioning_manifest(document)
    assert error.value.reason_code == "MANIFEST_OVERRIDE_INVALID"


@pytest.mark.parametrize(
    "extra_token",
    [
        "tenant_schema.operation_registry:insert",
        "tenant_schema.access_audit_outbox:insert",
        "tenant_schema:checksummed_ddl",
    ],
)
def test_cp8_loader_allows_tokens_that_do_not_restore_removed_privileges(
    monkeypatch: pytest.MonkeyPatch,
    extra_token: str,
) -> None:
    """C1 positive controls: shared retained privileges and other tables are safe."""
    from haloflow.m01.provisioning import manifest as manifest_module

    permissions = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    permissions[RUNTIME_ROLE]["allow"].append(extra_token)
    monkeypatch.setattr(manifest_module, "_permissions_document", lambda: permissions)
    manifest = manifest_module.load_provisioning_manifest()
    expected = _cp8_expected_table_grants(
        {RUNTIME_ROLE: permissions[RUNTIME_ROLE]}, manifest, ["operation_registry"]
    )
    assert {privilege for (_, _, privilege), held in expected.items() if held} == {
        "SELECT",
        "INSERT",
    }


def test_cp8_table_scoped_tokens_are_additive_and_stay_on_their_table() -> None:
    """Q3: prove the contribution even when a broader token does not mask it."""
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    policy = {
        AUDIT_PROJECTOR_ROLE: {
            "allow": ["tenant_schema.access_audit_outbox:select"],
            "deny": [],
        }
    }
    expected = _cp8_expected_table_grants(
        policy,
        load_provisioning_manifest(),
        ["access_audit_outbox", "operation_registry"],
    )
    assert {key for key, held in expected.items() if held} == {
        (AUDIT_PROJECTOR_ROLE, "access_audit_outbox", "SELECT")
    }


def test_cp8_table_privilege_vocabulary_matches_loader() -> None:
    """C5: test matrix and override validation must cover the same seven names."""
    from haloflow.m01.provisioning.manifest import TABLE_PRIVILEGES as declared

    assert frozenset(TABLE_PRIVILEGES) == declared
    assert len(TABLE_PRIVILEGES) == 7


@pytest.mark.parametrize("privilege", TABLE_PRIVILEGES)
async def test_cp8_owner_privilege_cells_detect_revocation_without_ownership_change(
    provisioning_harness: ProvisioningHarness,
    new_tenant: tuple[str, str],
    privilege: str,
) -> None:
    """C3 counterexample: owner retains ownership yet can lose an ordinary privilege."""
    from psycopg import Rollback

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    await make_provisioner(provisioning_harness).provision(request_for(new_tenant))
    schema_key = new_tenant[1]
    permissions = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    async with (
        await AsyncConnection.connect(
            provisioning_harness.admin_conninfo, autocommit=True
        ) as conn,
        conn.transaction(),
    ):
        tables, before = await _cp8_table_matrix(conn, schema_key, [MIGRATOR_ROLE])
        expected = _cp8_expected_table_grants(
            {MIGRATOR_ROLE: permissions[MIGRATOR_ROLE]},
            load_provisioning_manifest(),
            tables,
        )
        assert before == expected
        assert all(before.values())
        await conn.execute(
            sql.SQL("REVOKE {} ON {} FROM {}").format(
                sql.SQL(privilege),
                sql.Identifier(schema_key, "access_audit_outbox"),
                sql.Identifier(MIGRATOR_ROLE),
            )
        )
        # The reader reasserts actual relowner. Ownership and ordinary grants
        # are distinct: a non-superuser owner can revoke its own table grants.
        _, after = await _cp8_table_matrix(conn, schema_key, [MIGRATOR_ROLE])
        assert {key for key in expected if after[key] != expected[key]} == {
            (MIGRATOR_ROLE, "access_audit_outbox", privilege)
        }
        assert not after[(MIGRATOR_ROLE, "access_audit_outbox", privilege)]
        raise Rollback()
