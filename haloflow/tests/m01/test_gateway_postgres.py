import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import (
    InsufficientPrivilege,
    QueryCanceled,
    RaiseException,
    UndefinedTable,
)

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.control_store import PsycopgControlStore
from haloflow.m01.errors import (
    CapabilityDenied,
    ContextExpired,
    NestedTenantTransaction,
    RegistryInconsistent,
    RepositoryHandleExpired,
    RepositoryStatementRejected,
)
from haloflow.m01.gateway import (
    TenantRepositoryHandle,
    TenantTransactionGateway,
    TransactionOptions,
)
from haloflow.m01.pool import TenantPool
from haloflow.m01.provisioning import (
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    ProvisioningRequest,
    TenantMigrationRegistry,
    TenantMigrationRunner,
    TenantProvisioner,
)
from haloflow.m01.provisioning.units import (
    TENANT_MIGRATIONS,
    build_tenant_migration_registry,
)
from haloflow.m01.resolver import TenantResolver
from haloflow.m01.statements import StatementMode, build_statement_catalog

pytestmark = pytest.mark.postgres

RUNTIME_ROLE = "haloflow_runtime"
M01_ROOT = Path("src/haloflow/m01")

TEST_CATALOG = build_statement_catalog(
    {
        "m01_test.marker_path": (
            StatementMode.READ,
            "probe:write",
            "SELECT marker, current_schemas(true), pg_my_temp_schema() "
            "FROM isolation_probe WHERE business_id = %s",
        ),
        "m01_test.backend_marker": (
            StatementMode.READ,
            "probe:write",
            "SELECT pg_backend_pid(), marker FROM isolation_probe WHERE business_id = 42",
        ),
        "m01_test.insert": (
            StatementMode.WRITE,
            "probe:write",
            "INSERT INTO isolation_probe (business_id, marker) VALUES (%s, %s)",
        ),
        "m01_test.count_99": (
            StatementMode.READ,
            "probe:write",
            "SELECT count(*) FROM isolation_probe WHERE business_id = 99",
        ),
        "m01_test.select_one": (StatementMode.READ, "probe:read", "SELECT 1"),
        "m01_test.marker": (
            StatementMode.READ,
            "probe:write",
            "SELECT marker FROM isolation_probe WHERE business_id = 42",
        ),
        "m01_test.sleep_one": (StatementMode.READ, "probe:write", "SELECT pg_sleep(1)"),
        "m01_test.sleep_ten": (StatementMode.READ, "probe:write", "SELECT pg_sleep(10)"),
        "m01_test.other_insert": (
            StatementMode.WRITE,
            "other:write",
            "INSERT INTO isolation_probe (business_id, marker) VALUES (%s, %s)",
        ),
        "m01_test.application_name": (
            StatementMode.READ,
            "probe:read",
            "SELECT current_setting('application_name')",
        ),
        "m01_test.transaction_read_only": (
            StatementMode.READ,
            "probe:read",
            "SELECT current_setting('transaction_read_only')",
        ),
    }
)


@dataclass(frozen=True)
class PostgresHarness:
    admin_conninfo: str
    runtime_conninfo: str


# D11, as refined in review: the isolation suite runs against **provisioner-built**
# schemas, which is the strongest available evidence that the provisioner is
# correct, and one deliberately minimal hand-built schema is kept alongside them.
# A consistently wrong provisioner would otherwise become the test oracle for
# every gateway behaviour it is supposed to be validated by; `tenant_cccccccc` is
# the independent control, exercised in `test_provisioning_postgres.py`.
PROVISIONED_TENANTS: tuple[tuple[str, str, str], ...] = (
    ("clinic-a", "tenant_aaaaaaaa", "tenant-a"),
    ("clinic-b", "tenant_bbbbbbbb", "tenant-b"),
)
CONTROL_TENANT = ("clinic-c", "tenant_cccccccc", "tenant-c")

# A test-only migration unit, supplied explicitly by the tests and composed
# through the same trusted builder as production (R-E12). It installs the probe
# table the isolation suite needs. It cannot reach the production registry: that
# is `build_production_tenant_migrations`, which never passes `allow_test_units`.
TEST_ISOLATION_PROBE_SQL = """
CREATE TABLE {schema}.isolation_probe (
    business_id integer PRIMARY KEY,
    marker text NOT NULL
);
"""
TEST_TENANT_MIGRATIONS = {"t001_test_isolation_probe": TEST_ISOLATION_PROBE_SQL}


def _connection_factory(conninfo: str) -> Callable[[], Awaitable[AsyncConnection]]:
    async def _connect() -> AsyncConnection:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


def build_test_registry() -> TenantMigrationRegistry:
    return build_tenant_migration_registry(
        TENANT_MIGRATIONS, TEST_TENANT_MIGRATIONS, allow_test_units=True
    )


def build_test_provisioner(role_logins: dict[str, str]) -> TenantProvisioner:
    runner = TenantMigrationRunner(
        _connection_factory(role_logins[MIGRATOR_ROLE]), build_test_registry()
    )
    return TenantProvisioner(
        _connection_factory(role_logins[PROVISIONER_ROLE]),
        runner,
        supported_schema_versions=range(1, 2),
    )


async def _execute_admin(conninfo: str, *statements: str) -> None:
    async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
        for statement in statements:
            await conn.execute(statement)


@pytest_asyncio.fixture(scope="session")
async def postgres_harness(
    migrated_database: str,
    role_logins: dict[str, str],
    reset_tenants: Callable[[str, Sequence[str], Sequence[str]], None],
) -> AsyncIterator[PostgresHarness]:
    admin_conninfo = migrated_database
    tenants = (*PROVISIONED_TENANTS, CONTROL_TENANT)
    reset_tenants(
        admin_conninfo,
        [tenant_id for tenant_id, _, _ in tenants],
        [schema_key for _, schema_key, _ in tenants],
    )

    provisioner = build_test_provisioner(role_logins)
    for tenant_id, schema_key, _ in PROVISIONED_TENANTS:
        await provisioner.provision(
            ProvisioningRequest(
                tenant_id=tenant_id,
                schema_key=schema_key,
                actor_id="integration-test-harness",
                execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:provision:{tenant_id}"),
            )
        )

    async with await AsyncConnection.connect(admin_conninfo, autocommit=True) as admin:
        # The independent hand-built control. Deliberately minimal: schema, probe
        # table, outbox, grants -- enough to prove isolation and per-statement
        # capability enforcement against a schema the provisioner did not produce.
        control_id, control_schema, control_marker = CONTROL_TENANT
        control = sql.Identifier(control_schema)
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(control))
        await admin.execute(
            sql.SQL(
                "CREATE TABLE {}.isolation_probe "
                "(business_id integer PRIMARY KEY, marker text NOT NULL)"
            ).format(control)
        )
        await admin.execute(
            sql.SQL(
                "CREATE TABLE {}.access_audit_outbox "
                "(source_event_id uuid PRIMARY KEY, action_code text NOT NULL)"
            ).format(control)
        )
        await admin.execute(
            sql.SQL("INSERT INTO {}.isolation_probe VALUES (42, %s)").format(control),
            (control_marker,),
        )
        await admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                control, sql.Identifier(RUNTIME_ROLE)
            )
        )
        await admin.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
                control, sql.Identifier(RUNTIME_ROLE)
            )
        )
        await admin.execute(
            """
            INSERT INTO shared.tenants
                (tenant_id, schema_key, lifecycle_state, schema_version)
            VALUES (%s, %s, 'active', 1)
            """,
            (control_id, control_schema),
        )

        # Seed the probe rows the isolation suite reads. The provisioner installs
        # no business data, which is correct: `t001` is an infrastructure
        # baseline, not a fixture.
        for _, schema_key, marker in PROVISIONED_TENANTS:
            await admin.execute(
                sql.SQL("INSERT INTO {}.isolation_probe VALUES (42, %s)").format(
                    sql.Identifier(schema_key)
                ),
                (marker,),
            )

    yield PostgresHarness(admin_conninfo, role_logins[RUNTIME_ROLE])

    reset_tenants(
        admin_conninfo,
        [tenant_id for tenant_id, _, _ in tenants],
        [schema_key for _, schema_key, _ in tenants],
    )


@pytest_asyncio.fixture
async def tenant_pool(postgres_harness: PostgresHarness) -> AsyncIterator[TenantPool]:
    pool = TenantPool(postgres_harness.runtime_conninfo, min_size=1, max_size=1)
    await pool.open()
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def gateway(tenant_pool: TenantPool) -> TenantTransactionGateway:
    return TenantTransactionGateway(tenant_pool, TEST_CATALOG)


@pytest_asyncio.fixture
async def resolver(tenant_pool: TenantPool) -> TenantResolver:
    return TenantResolver(
        PsycopgControlStore(tenant_pool),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=10),
    )


def _principal(*tenant_ids: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="integration-test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset(tenant_ids),
        capabilities=frozenset({"probe:read", "probe:write"}),
    )


async def _context(
    resolver: TenantResolver,
    tenant_id: str,
    operation_label: str,
    *,
    capabilities: frozenset[str] = frozenset({"probe:write"}),
) -> TenantContext:
    return await resolver.resolve(
        principal=_principal("clinic-a", "clinic-b"),
        tenant_hint=tenant_id,
        purpose="operations",
        capabilities=capabilities,
        source=TrustedSource.WORKER,
        execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:{operation_label}"),
        correlation_id=uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
    )


@pytest.mark.asyncio
async def test_same_business_id_isolated_between_tenants(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    context_a = await _context(resolver, "clinic-a", "op-a")
    context_b = await _context(resolver, "clinic-b", "op-b")

    async def read_marker(tx: TenantRepositoryHandle) -> tuple[str, list[str], int]:
        row = await tx.fetch_one("m01_test.marker_path", (42,))
        assert row is not None
        return row[0], row[1], row[2]

    marker_a, path_a, temp_a = await gateway.with_tenant_transaction(context_a, read_marker)
    marker_b, path_b, temp_b = await gateway.with_tenant_transaction(context_b, read_marker)

    assert marker_a == "tenant-a"
    assert marker_b == "tenant-b"
    assert path_a == ["pg_catalog", "tenant_aaaaaaaa"]
    assert path_b == ["pg_catalog", "tenant_bbbbbbbb"]
    assert temp_a == temp_b == 0


@pytest.mark.asyncio
async def test_alternating_tenants_reuse_one_clean_physical_connection(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    async def backend_and_marker(tx: TenantRepositoryHandle) -> tuple[int, str]:
        row = await tx.fetch_one("m01_test.backend_marker")
        assert row is not None
        return row[0], row[1]

    result_a = await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-a", "reuse-a"), backend_and_marker
    )
    result_b = await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-b", "reuse-b"), backend_and_marker
    )

    assert result_a[0] == result_b[0]
    assert result_a[1:] == ("tenant-a",)
    assert result_b[1:] == ("tenant-b",)


@pytest.mark.asyncio
async def test_callback_failure_rolls_back_and_connection_is_reusable(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    context_a = await _context(resolver, "clinic-a", "rollback-a")

    async def fail_after_write(tx: TenantRepositoryHandle) -> None:
        await tx.execute("m01_test.insert", (99, "must-roll-back"))
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await gateway.with_tenant_transaction(context_a, fail_after_write)

    async def count_probe(tx: TenantRepositoryHandle) -> int:
        row = await tx.fetch_one("m01_test.count_99")
        assert row is not None
        return row[0]

    assert await gateway.with_tenant_transaction(context_a, count_probe) == 0
    assert (
        await gateway.with_tenant_transaction(
            await _context(resolver, "clinic-b", "rollback-b"), count_probe
        )
        == 0
    )


@pytest.mark.asyncio
async def test_read_capability_cannot_execute_registered_write(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    read_context = await _context(
        resolver,
        "clinic-a",
        "read-capability",
        capabilities=frozenset({"probe:read"}),
    )

    async def attempt_write(tx: TenantRepositoryHandle) -> None:
        assert await tx.fetch_one("m01_test.select_one") == (1,)
        with pytest.raises(CapabilityDenied):
            await tx.execute("m01_test.insert", (100, "must-not-write"))

    await gateway.with_tenant_transaction(read_context, attempt_write)


@pytest.mark.asyncio
async def test_write_capability_cannot_execute_another_capability_statement(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    context = await _context(resolver, "clinic-a", "cross-capability")

    async def attempt_other_write(tx: TenantRepositoryHandle) -> None:
        with pytest.raises(CapabilityDenied) as error:
            await tx.execute("m01_test.other_insert", (101, "must-not-write"))
        assert error.value.reason_code == "STATEMENT_CAPABILITY_DENIED"

    await gateway.with_tenant_transaction(context, attempt_other_write)


@pytest.mark.asyncio
async def test_repository_handle_expires_after_callback(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    captured: TenantRepositoryHandle | None = None

    async def capture(tx: TenantRepositoryHandle) -> None:
        nonlocal captured
        captured = tx

    await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-a", "handle-expiry"), capture
    )
    assert captured is not None
    with pytest.raises(RepositoryHandleExpired):
        await captured.fetch_one("m01_test.select_one")
    assert object.__getattribute__(captured, "_TenantRepositoryHandle__connection") is None
    assert object.__getattribute__(captured, "_TenantRepositoryHandle__catalog") is None


@pytest.mark.asyncio
async def test_nested_gateway_call_fails_before_another_checkout(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    context = await _context(resolver, "clinic-a", "nested")

    async def nested(_tx: TenantRepositoryHandle) -> None:
        with pytest.raises(NestedTenantTransaction):
            await gateway.with_tenant_transaction(context, lambda _inner: asyncio.sleep(0))

    await gateway.with_tenant_transaction(context, nested)


@pytest.mark.asyncio
async def test_registry_mapping_is_revalidated_inside_transaction(
    gateway: TenantTransactionGateway,
    resolver: TenantResolver,
    postgres_harness: PostgresHarness,
) -> None:
    context = await _context(resolver, "clinic-a", "registry-change")
    await _execute_admin(
        postgres_harness.admin_conninfo,
        "UPDATE shared.tenants SET lifecycle_state = 'suspended' WHERE tenant_id = 'clinic-a'",
    )

    try:
        with pytest.raises(RegistryInconsistent):
            await gateway.with_tenant_transaction(context, lambda _tx: asyncio.sleep(0))
    finally:
        await _execute_admin(
            postgres_harness.admin_conninfo,
            "UPDATE shared.tenants SET lifecycle_state = 'active' WHERE tenant_id = 'clinic-a'",
        )


@pytest.mark.asyncio
async def test_active_schema_identity_is_database_immutable(
    postgres_harness: PostgresHarness,
) -> None:
    async with await AsyncConnection.connect(
        postgres_harness.admin_conninfo, autocommit=True
    ) as admin:
        with pytest.raises(RaiseException, match="schema identity is immutable"):
            await admin.execute(
                "UPDATE shared.tenants SET schema_key = 'tenant_cccccccc' "
                "WHERE tenant_id = 'clinic-a'"
            )


@pytest.mark.asyncio
async def test_migration_roles_and_global_audit_grants(
    postgres_harness: PostgresHarness,
) -> None:
    permissions = json.loads((M01_ROOT / "manifests/permissions.json").read_text())
    expected_audit_writers = {
        role
        for role, policy in permissions.items()
        if "shared.access_audit_log:insert" in policy["allow"]
        or "shared:ownership" in policy["allow"]
    }

    async with await AsyncConnection.connect(
        postgres_harness.admin_conninfo, autocommit=True
    ) as admin:
        role_rows = await (
            await admin.execute(
                "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
                (list(permissions),),
            )
        ).fetchall()
        audit_privileges = {
            role: (
                await (
                    await admin.execute(
                        "SELECT has_table_privilege(%s, 'shared.access_audit_log', 'INSERT')",
                        (role,),
                    )
                ).fetchone()
            )[0]
            for role in permissions
        }
        runtime_row = await (
            await admin.execute(
                """
                SELECT
                    has_database_privilege(%s, current_database(), 'TEMPORARY'),
                    has_column_privilege(
                        %s, 'shared.tenants', 'tenant_id', 'SELECT'
                    ),
                    has_column_privilege(
                        %s, 'shared.tenants', 'schema_key', 'SELECT'
                    ),
                    has_column_privilege(
                        %s, 'shared.tenants', 'lifecycle_state', 'SELECT'
                    ),
                    has_column_privilege(
                        %s, 'shared.tenants', 'schema_version', 'SELECT'
                    ),
                    has_column_privilege(
                        %s, 'shared.tenants', 'display_reference', 'SELECT'
                    )
                """,
                (RUNTIME_ROLE,) * 6,
            )
        ).fetchone()

    assert {role for role, can_login in role_rows if not can_login} == set(permissions)
    assert {role for role, allowed in audit_privileges.items() if allowed} == (
        expected_audit_writers
    )
    assert runtime_row == (False, True, True, True, True, False)


@pytest.mark.asyncio
async def test_shared_schema_matches_classification_manifest(
    postgres_harness: PostgresHarness,
) -> None:
    manifest = json.loads((M01_ROOT / "manifests/shared_schema_classification.json").read_text())
    async with await AsyncConnection.connect(
        postgres_harness.admin_conninfo, autocommit=True
    ) as admin:
        table_rows = await (
            await admin.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'shared' AND table_type = 'BASE TABLE'
                """
            )
        ).fetchall()
        column_rows = await (
            await admin.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'shared'
                """
            )
        ).fetchall()
        comment_rows = await (
            await admin.execute(
                """
                SELECT c.relname, obj_description(c.oid, 'pg_class')
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'shared' AND c.relkind = 'r'
                """
            )
        ).fetchall()

    expected_tables = set(manifest["tables"])
    assert {row[0] for row in table_rows} == expected_tables
    actual_columns = {
        table: {column for row_table, column in column_rows if row_table == table}
        for table in expected_tables
    }
    assert actual_columns == {
        table: set(definition["columns"]) for table, definition in manifest["tables"].items()
    }
    assert {
        table for table, comment in comment_rows if comment and "PHI prohibited" in comment
    } == (expected_tables)


@pytest.mark.asyncio
async def test_ungated_tenant_query_fails_to_resolve(
    postgres_harness: PostgresHarness,
) -> None:
    async with await AsyncConnection.connect(
        postgres_harness.runtime_conninfo,
        autocommit=True,
        prepare_threshold=None,
    ) as conn:
        with pytest.raises(UndefinedTable):
            await conn.execute("SELECT * FROM isolation_probe")


@pytest.mark.asyncio
async def test_repository_cannot_qualify_another_tenant_schema(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    async def attempt_cross_tenant_query(tx: TenantRepositoryHandle) -> None:
        with pytest.raises(RepositoryStatementRejected):
            await tx.fetch_one("SELECT * FROM tenant_bbbbbbbb.isolation_probe")

    await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-a", "qualified-schema"),
        attempt_cross_tenant_query,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        'SELECT marker FROM "tenant_bbbbbbbb".isolation_probe WHERE business_id = 42',
        "SELECT marker FROM tenant_bbbbbbbb/**/.isolation_probe WHERE business_id = 42",
        'SELECT tenant_id FROM "shared".tenants LIMIT 1',
        "SELECT set_config('search_path', 'tenant_bbbbbbbb', true)",
        "RESET search_path",
    ],
)
async def test_repository_rejects_alternate_sql_boundary_bypasses(
    gateway: TenantTransactionGateway,
    resolver: TenantResolver,
    query: str,
) -> None:
    async def attempt_bypass(tx: TenantRepositoryHandle) -> None:
        with pytest.raises(RepositoryStatementRejected):
            await tx.fetch_one(query)

    await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-a", "alternate-bypass"),
        attempt_bypass,
    )


@pytest.mark.asyncio
async def test_runtime_cannot_create_temp_objects_or_write_global_audit(
    postgres_harness: PostgresHarness,
) -> None:
    async with await AsyncConnection.connect(
        postgres_harness.runtime_conninfo,
        autocommit=True,
        prepare_threshold=None,
    ) as conn:
        with pytest.raises(InsufficientPrivilege):
            await conn.execute("CREATE TEMP TABLE isolation_probe (id integer)")
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(
                "INSERT INTO shared.access_audit_log "
                "(source_event_id, tenant_id) VALUES (gen_random_uuid(), 'clinic-a')"
            )


@pytest.mark.asyncio
async def test_automatic_preparation_is_disabled(
    postgres_harness: PostgresHarness,
) -> None:
    async with await AsyncConnection.connect(
        postgres_harness.runtime_conninfo,
        autocommit=True,
        prepare_threshold=None,
    ) as conn:
        for _ in range(10):
            await conn.execute("SELECT 1")
        row = await (await conn.execute("SELECT count(*) FROM pg_prepared_statements")).fetchone()
        assert row == (0,)


@pytest.mark.asyncio
async def test_transaction_timeout_is_local_and_connection_remains_reusable(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    async def backend(tx: TenantRepositoryHandle) -> int:
        row = await tx.fetch_one("m01_test.backend_marker")
        assert row is not None
        return row[0]

    original_backend = await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-a", "timeout-before"), backend
    )

    async def exceed_timeout(tx: TenantRepositoryHandle) -> None:
        await tx.execute("m01_test.sleep_one")

    with pytest.raises(QueryCanceled):
        await gateway.with_tenant_transaction(
            await _context(resolver, "clinic-a", "timeout-a"),
            exceed_timeout,
            options=TransactionOptions(
                statement_timeout_ms=50,
                lock_timeout_ms=25,
                transaction_timeout_margin_ms=2_000,
            ),
        )

    async def backend_and_marker(tx: TenantRepositoryHandle) -> tuple[int, str]:
        row = await tx.fetch_one("m01_test.backend_marker")
        assert row is not None
        return row[0], row[1]

    reused_backend, marker = await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-b", "timeout-b"), backend_and_marker
    )
    assert reused_backend == original_backend
    assert marker == "tenant-b"


@pytest.mark.asyncio
async def test_sub_millisecond_context_lifetime_fails_before_checkout(
    tenant_pool: TenantPool,
    resolver: TenantResolver,
) -> None:
    context = await _context(resolver, "clinic-a", "sub-millisecond")

    def edge_clock() -> datetime:
        return context.expires_at - timedelta(microseconds=500)

    edge_gateway = TenantTransactionGateway(
        tenant_pool,
        TEST_CATALOG,
        clock=edge_clock,
    )

    with pytest.raises(ContextExpired):
        await edge_gateway.with_tenant_transaction(
            context,
            lambda _tx: asyncio.sleep(0),
        )


@pytest.mark.asyncio
async def test_context_without_transaction_timeout_margin_fails_before_checkout(
    tenant_pool: TenantPool,
    resolver: TenantResolver,
) -> None:
    context = await _context(resolver, "clinic-a", "timeout-margin")

    def edge_clock() -> datetime:
        return context.expires_at - timedelta(milliseconds=2_000)

    edge_gateway = TenantTransactionGateway(
        tenant_pool,
        TEST_CATALOG,
        clock=edge_clock,
    )

    with pytest.raises(ContextExpired) as error:
        await edge_gateway.with_tenant_transaction(
            context,
            lambda _tx: asyncio.sleep(0),
        )
    assert error.value.reason_code == "CONTEXT_BUDGET_INSUFFICIENT"


@pytest.mark.asyncio
async def test_cancellation_does_not_contaminate_next_tenant(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    started = asyncio.Event()

    async def wait_in_database(tx: TenantRepositoryHandle) -> None:
        started.set()
        await tx.execute("m01_test.sleep_ten")

    task = asyncio.create_task(
        gateway.with_tenant_transaction(
            await _context(resolver, "clinic-a", "cancel-a"), wait_in_database
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def marker(tx: TenantRepositoryHandle) -> str:
        row = await tx.fetch_one("m01_test.marker")
        assert row is not None
        return row[0]

    assert (
        await gateway.with_tenant_transaction(
            await _context(resolver, "clinic-b", "cancel-b"), marker
        )
        == "tenant-b"
    )


@pytest.mark.asyncio
async def test_application_name_carries_the_execution_id(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    """TC-A4."""

    context = await _context(
        resolver,
        "clinic-a",
        "application-name",
        capabilities=frozenset({"probe:read"}),
    )

    async def read_application_name(tx: TenantRepositoryHandle) -> str:
        row = await tx.fetch_one("m01_test.application_name")
        assert row is not None
        return str(row[0])

    observed = await gateway.with_tenant_transaction(context, read_application_name)
    assert observed == f"haloflow:{context.execution_id}"


@pytest.mark.asyncio
async def test_per_statement_enforcement_survives_a_multi_capability_context(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    """TC-C5. A context holding read+write still cannot reach a third capability."""

    context = await _context(
        resolver,
        "clinic-a",
        "multi-capability",
        capabilities=frozenset({"probe:read", "probe:write"}),
    )
    assert context.capabilities == frozenset({"probe:read", "probe:write"})

    async def attempt_unauthorised(tx: TenantRepositoryHandle) -> None:
        assert await tx.fetch_one("m01_test.select_one") == (1,)
        await tx.execute("m01_test.insert", (77, "multi"))
        with pytest.raises(CapabilityDenied) as error:
            await tx.execute("m01_test.other_insert", (78, "denied"))
        assert error.value.reason_code == "STATEMENT_CAPABILITY_DENIED"

    await gateway.with_tenant_transaction(context, attempt_unauthorised)


@pytest.mark.asyncio
async def test_mixed_capability_context_yields_a_read_write_transaction(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    """TC-C7. The accepted consequence of F1, pinned so it cannot change silently.

    `allow_writes` is the intersection of context capabilities with the derived
    write set, so one write capability makes the whole transaction read-write.
    Least privilege for such flows rests on the per-statement check above.
    """

    context = await _context(
        resolver,
        "clinic-a",
        "mixed-read-write",
        capabilities=frozenset({"probe:read", "probe:write"}),
    )

    async def assert_read_write(tx: TenantRepositoryHandle) -> None:
        row = await tx.fetch_one("m01_test.transaction_read_only")
        assert row is not None
        assert row[0] == "off"

    await gateway.with_tenant_transaction(context, assert_read_write)


@pytest.mark.asyncio
async def test_read_only_context_yields_a_read_only_transaction(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    """TC-C6."""

    context = await _context(
        resolver,
        "clinic-a",
        "read-only-transaction",
        capabilities=frozenset({"probe:read"}),
    )

    async def assert_read_only(tx: TenantRepositoryHandle) -> None:
        row = await tx.fetch_one("m01_test.transaction_read_only")
        assert row is not None
        assert row[0] == "on"

    await gateway.with_tenant_transaction(context, assert_read_only)


@pytest.mark.asyncio
async def test_gateway_requires_an_explicit_catalogue(tenant_pool: TenantPool) -> None:
    """TC-D5. No silent empty-catalogue default."""

    with pytest.raises(TypeError):
        TenantTransactionGateway(tenant_pool)  # type: ignore[call-arg]


@pytest.mark.postgres
def test_migration_002_renamed_and_retyped_the_execution_id_columns(
    postgres_harness: PostgresHarness,
) -> None:
    """TC-A6 and TC-A7, asserted from the catalogue rather than from the migration source."""

    import psycopg

    with psycopg.connect(postgres_harness.admin_conninfo) as conn:
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'shared' AND column_name IN ('execution_id', 'operation_id')
            ORDER BY table_name
            """
        ).fetchall()

    assert rows == [
        ("access_audit_log", "execution_id", "uuid", "NO"),
        ("isolation_alerts", "execution_id", "uuid", "YES"),
        ("tenant_state_history", "execution_id", "uuid", "NO"),
    ]


@pytest.mark.postgres
def test_migration_002_downgrade_raises() -> None:
    """TC-A8. Consistent with 001."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m01_migration_002", "alembic/versions/002_execution_id_rename.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(RuntimeError):
        module.downgrade()


@pytest.mark.postgres
def test_migration_002_preflight_blocks_a_non_castable_value(
    postgres_harness: PostgresHarness,
    database_url: Callable[[dict[str, object], str], str],
    apply_migrations: Callable[..., None],
) -> None:
    """TC-A9.

    The guard is the only thing standing between an unanticipated value and a
    cast failure partway through a deploy, so it is tested against a real one
    rather than asserted to exist. `isolation_alerts` is used because it carries
    no append-only trigger and can therefore be seeded and corrected; the other
    two tables cannot, which is what the guard's own message says.
    """

    import psycopg
    from psycopg.errors import DataException
    from sqlalchemy.exc import DataError

    scratch = "haloflow_test_m01_preflight"
    params = conninfo_to_dict(postgres_harness.admin_conninfo)
    admin_conninfo = make_conninfo(**{**params, "dbname": "postgres"})
    # psycopg accepts a keyword/value conninfo, but alembic/env.py expects a URL
    # it can rewrite to postgresql+psycopg://, so the migration target is built
    # as a URL rather than reusing the keyword form.
    scratch_url = database_url(params, scratch)
    scratch_conninfo = make_conninfo(**{**params, "dbname": scratch})

    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {scratch}")
        admin.execute(f"CREATE DATABASE {scratch}")

    try:
        apply_migrations(scratch_url, revision="001")

        with psycopg.connect(scratch_conninfo, autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO shared.tenants
                    (tenant_id, schema_key, lifecycle_state, schema_version)
                VALUES ('clinic-x', 'tenant_xxxxxxxx', 'active', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO shared.isolation_alerts
                    (alert_id, tenant_id, source_code, alert_type, severity, operation_id)
                VALUES (gen_random_uuid(), 'clinic-x', 'seed', 'seed', 1, 'legacy-trace-00042')
                """
            )

        with pytest.raises((DataError, DataException)) as error:
            apply_migrations(scratch_url, revision="002")
        message = str(error.value)
        assert "preflight failed" in message
        assert "No schema change has been made" in message
        # PHI-safe: the offending value is counted, never echoed.
        assert "legacy-trace-00042" not in message

        with psycopg.connect(scratch_conninfo) as conn:
            still_unchanged = conn.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'shared' AND table_name = 'isolation_alerts'
                  AND column_name IN ('operation_id', 'execution_id')
                """
            ).fetchall()
            head = conn.execute("SELECT version_num FROM alembic_version").fetchone()

        assert still_unchanged == [("operation_id", "character varying")]
        assert head == ("001",)

        # Correct the value; 002 then applies.
        with psycopg.connect(scratch_conninfo, autocommit=True) as conn:
            conn.execute(
                "UPDATE shared.isolation_alerts SET operation_id = gen_random_uuid()::text"
            )
        apply_migrations(scratch_url, revision="002")

        with psycopg.connect(scratch_conninfo) as conn:
            converted = conn.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'shared' AND table_name = 'isolation_alerts'
                  AND column_name IN ('operation_id', 'execution_id')
                """
            ).fetchall()
        assert converted == [("execution_id", "uuid")]
    finally:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {scratch}")


@pytest.mark.postgres
def test_append_only_triggers_still_fire_after_the_rename(
    postgres_harness: PostgresHarness,
) -> None:
    """TC-A10. The triggers are defined on the table, but that is asserted, not assumed."""

    import psycopg

    with psycopg.connect(postgres_harness.admin_conninfo, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO shared.tenants (tenant_id, schema_key, lifecycle_state, schema_version)
            VALUES ('clinic-trigger', 'tenant_tttttttt', 'active', 1)
            ON CONFLICT (tenant_id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO shared.tenant_state_history
                (tenant_id, new_state, reason_code, actor_kind, actor_id, execution_id)
            VALUES ('clinic-trigger', 'active', 'seed', 'workload', 'w1', gen_random_uuid())
            """
        )
        try:
            with pytest.raises(RaiseException):
                conn.execute(
                    "UPDATE shared.tenant_state_history SET reason_code = 'x' "
                    "WHERE tenant_id = 'clinic-trigger'"
                )
            with pytest.raises(RaiseException):
                conn.execute(
                    "DELETE FROM shared.tenant_state_history WHERE tenant_id = 'clinic-trigger'"
                )
        finally:
            conn.execute("DELETE FROM shared.isolation_alerts WHERE tenant_id = 'clinic-trigger'")


@pytest.mark.postgres
def test_no_correlation_column_was_added_to_the_shared_schema(
    postgres_harness: PostgresHarness,
) -> None:
    """TC-B7. M01 carries correlation; M02 persists it."""

    import psycopg

    with psycopg.connect(postgres_harness.admin_conninfo) as conn:
        rows = conn.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'shared'
              AND column_name IN ('correlation_id', 'correlation_source')
            """
        ).fetchall()

    assert rows == []


@pytest.mark.postgres
def test_migration_002_lock_prevents_a_writer_racing_the_preflight(
    postgres_harness: PostgresHarness,
) -> None:
    """F-3. The guard scans, then converts; a writer must not slip in between.

    What this proves: `002` takes ACCESS EXCLUSIVE on all three tables before it
    scans, and while that lock is held a concurrent INSERT cannot land. What it
    does not prove is the full interleaving of a real migration against a live
    writer -- that is not deterministically testable -- so the mechanism the
    migration depends on is asserted directly instead.
    """

    import psycopg
    from psycopg.errors import QueryCanceled

    holder_conninfo = postgres_harness.admin_conninfo
    with (
        psycopg.connect(holder_conninfo) as holder,
        psycopg.connect(holder_conninfo, autocommit=True) as writer,
    ):
        # Same lock, same order, as LOCK_SQL in the migration.
        holder.execute("LOCK TABLE shared.tenant_state_history IN ACCESS EXCLUSIVE MODE")
        holder.execute("LOCK TABLE shared.access_audit_log IN ACCESS EXCLUSIVE MODE")
        holder.execute("LOCK TABLE shared.isolation_alerts IN ACCESS EXCLUSIVE MODE")

        writer.execute("SET statement_timeout = '750ms'")
        with pytest.raises(QueryCanceled):
            writer.execute(
                """
                INSERT INTO shared.isolation_alerts
                    (alert_id, tenant_id, source_code, alert_type, severity, execution_id)
                VALUES (gen_random_uuid(), NULL, 'race', 'race', 1, gen_random_uuid())
                """
            )
        holder.rollback()

        # Once the lock is released the same insert succeeds, proving the block
        # above was the lock and not a broken statement.
        writer.execute("SET statement_timeout = '5s'")
        writer.execute(
            """
            INSERT INTO shared.isolation_alerts
                (alert_id, tenant_id, source_code, alert_type, severity, execution_id)
            VALUES (gen_random_uuid(), NULL, 'race', 'race', 1, gen_random_uuid())
            """
        )
        writer.execute("DELETE FROM shared.isolation_alerts WHERE source_code = 'race'")


@pytest.mark.postgres
def test_migration_002_takes_the_lock_before_scanning(
    postgres_harness: PostgresHarness,
) -> None:
    """F-3. Ordering is the property that matters, so it is asserted on the source."""

    from pathlib import Path

    source = Path("alembic/versions/002_execution_id_rename.py").read_text()
    upgrade_body = source[source.index("def upgrade()") :]

    lock_at = upgrade_body.index("LOCK_SQL")
    preflight_at = upgrade_body.index("PREFLIGHT_SQL")
    rename_at = upgrade_body.index("RENAME_SQL")

    assert lock_at < preflight_at < rename_at


@pytest.mark.asyncio
async def test_the_isolation_suite_schemas_were_built_by_the_provisioner(
    postgres_harness: PostgresHarness,
) -> None:
    """TC-E14 and the D11 hybrid control.

    `clinic-a` and `clinic-b` carry applied ledger rows and provisioner-owned
    schemas, so every test in this module now runs against schemas the
    provisioner produced. `clinic-c` deliberately does not: it is hand-built, so
    a consistently wrong provisioner cannot become the oracle for every gateway
    behaviour it is itself meant to be validated by.
    """

    async with await AsyncConnection.connect(
        postgres_harness.admin_conninfo, autocommit=True
    ) as admin:
        rows = await (
            await admin.execute(
                """
                SELECT t.tenant_id,
                       pg_get_userbyid(n.nspowner),
                       (SELECT count(*) FROM shared.schema_migrations AS m
                         WHERE m.tenant_id = t.tenant_id AND m.state = 'applied')
                FROM shared.tenants AS t
                JOIN pg_namespace AS n ON n.nspname = t.schema_key
                WHERE t.tenant_id IN ('clinic-a', 'clinic-b', 'clinic-c')
                ORDER BY t.tenant_id
                """
            )
        ).fetchall()

    by_tenant = {tenant: (owner, applied) for tenant, owner, applied in rows}

    for provisioned in ("clinic-a", "clinic-b"):
        owner, applied = by_tenant[provisioned]
        assert owner == PROVISIONER_ROLE
        assert applied == 2  # the baseline plus the test-only isolation-probe unit

    control_owner, control_applied = by_tenant["clinic-c"]
    assert control_owner != PROVISIONER_ROLE
    assert control_applied == 0
