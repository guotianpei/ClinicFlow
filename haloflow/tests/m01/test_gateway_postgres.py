import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import pytest_asyncio
from alembic.config import Config
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import (
    InsufficientPrivilege,
    QueryCanceled,
    RaiseException,
    UndefinedTable,
)

from alembic import command
from haloflow.m01.context import Principal, PrincipalKind, TenantContext, TrustedSource
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
from haloflow.m01.resolver import TenantResolver
from haloflow.m01.statements import StatementMode, _build_statement_catalog

pytestmark = pytest.mark.postgres

RUNTIME_ROLE = "haloflow_runtime"
RUNTIME_LOGIN_ROLE = "haloflow_test_runtime_login"
RUNTIME_PASSWORD = "m01-local-test-only"
M01_ROOT = Path("src/haloflow/m01")

TEST_STATEMENT_CATALOG = _build_statement_catalog(
    {
        "probe.marker_path": (
            StatementMode.READ,
            "SELECT marker, current_schemas(true), pg_my_temp_schema() "
            "FROM isolation_probe WHERE business_id = %s",
        ),
        "probe.backend_marker": (
            StatementMode.READ,
            "SELECT pg_backend_pid(), marker FROM isolation_probe WHERE business_id = 42",
        ),
        "probe.insert": (
            StatementMode.WRITE,
            "INSERT INTO isolation_probe (business_id, marker) VALUES (%s, %s)",
        ),
        "probe.count_99": (
            StatementMode.READ,
            "SELECT count(*) FROM isolation_probe WHERE business_id = 99",
        ),
        "probe.select_one": (StatementMode.READ, "SELECT 1"),
        "probe.marker": (
            StatementMode.READ,
            "SELECT marker FROM isolation_probe WHERE business_id = 42",
        ),
        "probe.sleep_one": (StatementMode.READ, "SELECT pg_sleep(1)"),
        "probe.sleep_ten": (StatementMode.READ, "SELECT pg_sleep(10)"),
    }
)


@dataclass(frozen=True)
class PostgresHarness:
    admin_conninfo: str
    runtime_conninfo: str


def _test_conninfo() -> str:
    conninfo = os.getenv("HALOFLOW_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("HALOFLOW_TEST_DATABASE_URL is not configured")
    return conninfo


async def _execute_admin(conninfo: str, *statements: str) -> None:
    async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
        for statement in statements:
            await conn.execute(statement)


async def _drop_runtime_login_role(connection: AsyncConnection) -> None:
    exists = await (
        await connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
            (RUNTIME_LOGIN_ROLE,),
        )
    ).fetchone()
    if exists and exists[0]:
        await connection.execute(
            sql.SQL("DROP OWNED BY {}").format(sql.Identifier(RUNTIME_LOGIN_ROLE))
        )
        await connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(RUNTIME_LOGIN_ROLE)))


def _apply_migrations(conninfo: str) -> None:
    previous = os.environ.get("HALOFLOW_MIGRATION_DATABASE_URL")
    os.environ["HALOFLOW_MIGRATION_DATABASE_URL"] = conninfo
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("HALOFLOW_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["HALOFLOW_MIGRATION_DATABASE_URL"] = previous


@pytest_asyncio.fixture(scope="session")
async def postgres_harness() -> AsyncIterator[PostgresHarness]:
    admin_conninfo = _test_conninfo()
    _apply_migrations(admin_conninfo)
    async with await AsyncConnection.connect(admin_conninfo, autocommit=True) as admin:
        version = int((await (await admin.execute("SHOW server_version_num")).fetchone())[0])
        database = (await (await admin.execute("SELECT current_database()")).fetchone())[0]
        if version < 170000:
            pytest.fail(f"M01 tests require PostgreSQL 17+, found {version}")
        if not str(database).startswith("haloflow_test"):
            pytest.fail("Refusing to initialize a database not named haloflow_test*")

        await admin.execute("DROP SCHEMA IF EXISTS tenant_aaaaaaaa CASCADE")
        await admin.execute("DROP SCHEMA IF EXISTS tenant_bbbbbbbb CASCADE")
        await _drop_runtime_login_role(admin)
        await admin.execute(
            "DELETE FROM shared.tenants WHERE tenant_id IN ('clinic-a', 'clinic-b')"
        )
        await admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                sql.Identifier(RUNTIME_LOGIN_ROLE),
                sql.Literal(RUNTIME_PASSWORD),
                sql.Identifier(RUNTIME_ROLE),
            )
        )
        await admin.execute(
            sql.SQL("ALTER ROLE {} SET search_path = ''").format(sql.Identifier(RUNTIME_LOGIN_ROLE))
        )

        await admin.execute(
            """
            INSERT INTO shared.tenants
                (tenant_id, schema_key, lifecycle_state, schema_version)
            VALUES
                ('clinic-a', 'tenant_aaaaaaaa', 'active', 1),
                ('clinic-b', 'tenant_bbbbbbbb', 'active', 1)
            """
        )

        for schema_key, marker in (
            ("tenant_aaaaaaaa", "tenant-a"),
            ("tenant_bbbbbbbb", "tenant-b"),
        ):
            schema_identifier = sql.Identifier(schema_key)
            await admin.execute(sql.SQL("CREATE SCHEMA {}").format(schema_identifier))
            await admin.execute(
                sql.SQL(
                    "CREATE TABLE {}.isolation_probe "
                    "(business_id integer PRIMARY KEY, marker text NOT NULL)"
                ).format(schema_identifier)
            )
            await admin.execute(
                sql.SQL(
                    "CREATE TABLE {}.access_audit_outbox "
                    "(source_event_id uuid PRIMARY KEY, action_code text NOT NULL)"
                ).format(schema_identifier)
            )
            await admin.execute(
                sql.SQL("INSERT INTO {}.isolation_probe VALUES (42, %s)").format(schema_identifier),
                (marker,),
            )

        for schema_key in ("tenant_aaaaaaaa", "tenant_bbbbbbbb"):
            await admin.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema_key), sql.Identifier(RUNTIME_ROLE)
                )
            )
            await admin.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
                ).format(sql.Identifier(schema_key), sql.Identifier(RUNTIME_ROLE))
            )

    params = conninfo_to_dict(admin_conninfo)
    params.update(user=RUNTIME_LOGIN_ROLE, password=RUNTIME_PASSWORD)
    runtime_conninfo = make_conninfo(**params)
    yield PostgresHarness(admin_conninfo, runtime_conninfo)

    async with await AsyncConnection.connect(admin_conninfo, autocommit=True) as admin:
        await admin.execute("DROP SCHEMA IF EXISTS tenant_aaaaaaaa CASCADE")
        await admin.execute("DROP SCHEMA IF EXISTS tenant_bbbbbbbb CASCADE")
        await admin.execute(
            "DELETE FROM shared.tenants WHERE tenant_id IN ('clinic-a', 'clinic-b')"
        )
        await _drop_runtime_login_role(admin)


@pytest_asyncio.fixture
async def tenant_pool(postgres_harness: PostgresHarness) -> AsyncIterator[TenantPool]:
    pool = TenantPool(postgres_harness.runtime_conninfo, min_size=1, max_size=1)
    await pool.open()
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def gateway(tenant_pool: TenantPool) -> TenantTransactionGateway:
    return TenantTransactionGateway(
        tenant_pool,
        statement_catalog=TEST_STATEMENT_CATALOG,
        write_capabilities={"probe:write"},
    )


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
    capability: str = "probe:write",
) -> TenantContext:
    return await resolver.resolve(
        principal=_principal("clinic-a", "clinic-b"),
        tenant_hint=tenant_id,
        purpose="operations",
        capability=capability,
        source=TrustedSource.WORKER,
        operation_id=str(uuid5(NAMESPACE_URL, f"haloflow-test:{operation_label}")),
    )


@pytest.mark.asyncio
async def test_same_business_id_isolated_between_tenants(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    context_a = await _context(resolver, "clinic-a", "op-a")
    context_b = await _context(resolver, "clinic-b", "op-b")

    async def read_marker(tx: TenantRepositoryHandle) -> tuple[str, list[str], int]:
        row = await tx.fetch_one("probe.marker_path", (42,))
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
        row = await tx.fetch_one("probe.backend_marker")
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
        await tx.execute("probe.insert", (99, "must-roll-back"))
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await gateway.with_tenant_transaction(context_a, fail_after_write)

    async def count_probe(tx: TenantRepositoryHandle) -> int:
        row = await tx.fetch_one("probe.count_99")
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
        capability="probe:read",
    )

    async def attempt_write(tx: TenantRepositoryHandle) -> None:
        with pytest.raises(CapabilityDenied):
            await tx.execute("probe.insert", (100, "must-not-write"))

    await gateway.with_tenant_transaction(read_context, attempt_write)


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
        await captured.fetch_one("probe.select_one")
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
        row = await tx.fetch_one("probe.backend_marker")
        assert row is not None
        return row[0]

    original_backend = await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-a", "timeout-before"), backend
    )

    async def exceed_timeout(tx: TenantRepositoryHandle) -> None:
        await tx.execute("probe.sleep_one")

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
        row = await tx.fetch_one("probe.backend_marker")
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
        statement_catalog=TEST_STATEMENT_CATALOG,
        write_capabilities={"probe:write"},
        clock=edge_clock,
    )

    with pytest.raises(ContextExpired):
        await edge_gateway.with_tenant_transaction(
            context,
            lambda _tx: asyncio.sleep(0),
        )


@pytest.mark.asyncio
async def test_cancellation_does_not_contaminate_next_tenant(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    started = asyncio.Event()

    async def wait_in_database(tx: TenantRepositoryHandle) -> None:
        started.set()
        await tx.execute("probe.sleep_ten")

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
        row = await tx.fetch_one("probe.marker")
        assert row is not None
        return row[0]

    assert (
        await gateway.with_tenant_transaction(
            await _context(resolver, "clinic-b", "cancel-b"), marker
        )
        == "tenant-b"
    )
