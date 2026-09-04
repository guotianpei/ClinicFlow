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
from psycopg import AsyncConnection
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


VALID_CP4_TEMPLATE = "CREATE TABLE {schema}.cp4_probe (id integer PRIMARY KEY);"

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

    The provisioner and the runner call `assert_execution_roles_safe` without a
    manifest, so it loads the shipped `m01/manifests/provisioning.json` -- whose
    `execution_role_profiles` block is empty, and must stay empty: a test role
    in a production security declaration is the widening this checkpoint exists
    to prevent.

    Without this fixture every provisioner- and runner-path test below refuses
    at the "composition approved the name; the manifest never described it"
    branch and never reaches the catalogue comparison it is named for. The
    assertion still passes, because the code under test is right either way --
    which is precisely why the substitution is made explicit rather than left
    to coincidence.

    What is substituted is built by `cp4_manifest()` and therefore went through
    the real loader: a validated `ProvisioningManifest`, not a stub. A call that
    supplies its own document still reaches the original loader.
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
