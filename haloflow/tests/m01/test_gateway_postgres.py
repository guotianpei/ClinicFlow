import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
import pytest_asyncio
from alembic.config import Config
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import (
    InsufficientPrivilege,
    RaiseException,
    TransactionTimeout,
    UndefinedTable,
)

from alembic import command
from haloflow.m01.context import Principal, PrincipalKind, TenantContext, TrustedSource
from haloflow.m01.control_store import PsycopgControlStore
from haloflow.m01.errors import (
    NestedTenantTransaction,
    RegistryInconsistent,
    RepositoryHandleExpired,
)
from haloflow.m01.gateway import (
    TenantRepositoryHandle,
    TenantTransactionGateway,
    TransactionOptions,
)
from haloflow.m01.pool import TenantPool
from haloflow.m01.resolver import TenantResolver

pytestmark = pytest.mark.postgres

RUNTIME_ROLE = "haloflow_test_runtime"
RUNTIME_PASSWORD = "m01-local-test-only"


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


async def _drop_runtime_role(connection: AsyncConnection) -> None:
    exists = await (
        await connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
            (RUNTIME_ROLE,),
        )
    ).fetchone()
    if exists and exists[0]:
        await connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(RUNTIME_ROLE)))
        await connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(RUNTIME_ROLE)))


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
        await _drop_runtime_role(admin)
        await admin.execute(
            "DELETE FROM shared.tenants WHERE tenant_id IN ('clinic-a', 'clinic-b')"
        )
        await admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(RUNTIME_ROLE), sql.Literal(RUNTIME_PASSWORD)
            )
        )
        await admin.execute(
            sql.SQL("ALTER ROLE {} SET search_path = ''").format(sql.Identifier(RUNTIME_ROLE))
        )
        await admin.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(str(database))
            )
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

        await admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA shared TO {}").format(sql.Identifier(RUNTIME_ROLE))
        )
        await admin.execute(
            sql.SQL("GRANT SELECT ON shared.tenants TO {}").format(sql.Identifier(RUNTIME_ROLE))
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
    params.update(user=RUNTIME_ROLE, password=RUNTIME_PASSWORD)
    runtime_conninfo = make_conninfo(**params)
    yield PostgresHarness(admin_conninfo, runtime_conninfo)

    async with await AsyncConnection.connect(admin_conninfo, autocommit=True) as admin:
        await admin.execute("DROP SCHEMA IF EXISTS tenant_aaaaaaaa CASCADE")
        await admin.execute("DROP SCHEMA IF EXISTS tenant_bbbbbbbb CASCADE")
        await admin.execute(
            "DELETE FROM shared.tenants WHERE tenant_id IN ('clinic-a', 'clinic-b')"
        )
        await _drop_runtime_role(admin)


@pytest_asyncio.fixture
async def tenant_pool(postgres_harness: PostgresHarness) -> AsyncIterator[TenantPool]:
    pool = TenantPool(postgres_harness.runtime_conninfo, min_size=1, max_size=1)
    await pool.open()
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def gateway(tenant_pool: TenantPool) -> TenantTransactionGateway:
    return TenantTransactionGateway(tenant_pool)


@pytest_asyncio.fixture
async def resolver(tenant_pool: TenantPool) -> TenantResolver:
    return TenantResolver(
        PsycopgControlStore(tenant_pool),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=5),
    )


def _principal(*tenant_ids: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="integration-test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset(tenant_ids),
        capabilities=frozenset({"probe:read", "probe:write"}),
    )


async def _context(resolver: TenantResolver, tenant_id: str, operation_id: str) -> TenantContext:
    return await resolver.resolve(
        principal=_principal("clinic-a", "clinic-b"),
        tenant_hint=tenant_id,
        purpose="operations",
        capability="probe:write",
        source=TrustedSource.WORKER,
        operation_id=operation_id,
    )


@pytest.mark.asyncio
async def test_same_business_id_isolated_between_tenants(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    context_a = await _context(resolver, "clinic-a", "op-a")
    context_b = await _context(resolver, "clinic-b", "op-b")

    async def read_marker(tx: TenantRepositoryHandle) -> tuple[str, list[str], int]:
        row = await tx.fetch_one(
            "SELECT marker, current_schemas(true), pg_my_temp_schema() "
            "FROM isolation_probe WHERE business_id = %s",
            (42,),
        )
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
        row = await tx.fetch_one(
            "SELECT pg_backend_pid(), marker FROM isolation_probe WHERE business_id = 42"
        )
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
        await tx.execute(
            "INSERT INTO isolation_probe (business_id, marker) VALUES (%s, %s)",
            (99, "must-roll-back"),
        )
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await gateway.with_tenant_transaction(context_a, fail_after_write)

    async def count_probe(tx: TenantRepositoryHandle) -> int:
        row = await tx.fetch_one("SELECT count(*) FROM isolation_probe WHERE business_id = 99")
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
        await captured.fetch_one("SELECT 1")


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
    async with await AsyncConnection.connect(
        postgres_harness.admin_conninfo, autocommit=True
    ) as admin:
        row = await (
            await admin.execute(
                """
                SELECT
                    has_database_privilege(%s, current_database(), 'TEMPORARY'),
                    has_table_privilege(%s, 'shared.access_audit_log', 'INSERT'),
                    has_table_privilege(
                        'haloflow_audit_projector',
                        'shared.access_audit_log',
                        'INSERT'
                    )
                """,
                (RUNTIME_ROLE, RUNTIME_ROLE),
            )
        ).fetchone()

    assert row == (False, False, True)


@pytest.mark.asyncio
async def test_ungated_tenant_query_fails_to_resolve(tenant_pool: TenantPool) -> None:
    async with tenant_pool.connection_for_control() as conn:
        with pytest.raises(UndefinedTable):
            await conn.execute("SELECT * FROM isolation_probe")


@pytest.mark.asyncio
async def test_repository_cannot_qualify_another_tenant_schema(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    async def attempt_cross_tenant_query(tx: TenantRepositoryHandle) -> None:
        with pytest.raises(ValueError, match="qualified"):
            await tx.fetch_one("SELECT * FROM tenant_bbbbbbbb.isolation_probe")

    await gateway.with_tenant_transaction(
        await _context(resolver, "clinic-a", "qualified-schema"),
        attempt_cross_tenant_query,
    )


@pytest.mark.asyncio
async def test_runtime_cannot_create_temp_objects_or_write_global_audit(
    tenant_pool: TenantPool,
) -> None:
    async with tenant_pool.connection_for_control() as conn:
        with pytest.raises(InsufficientPrivilege):
            await conn.execute("CREATE TEMP TABLE isolation_probe (id integer)")
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(
                "INSERT INTO shared.access_audit_log "
                "(source_event_id, tenant_id) VALUES (gen_random_uuid(), 'clinic-a')"
            )


@pytest.mark.asyncio
async def test_automatic_preparation_is_disabled(tenant_pool: TenantPool) -> None:
    async with tenant_pool.connection_for_control() as conn:
        for _ in range(10):
            await conn.execute("SELECT 1")
        row = await (await conn.execute("SELECT count(*) FROM pg_prepared_statements")).fetchone()
        assert row == (0,)


@pytest.mark.asyncio
async def test_transaction_timeout_is_local_and_connection_remains_reusable(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    async def exceed_timeout(tx: TenantRepositoryHandle) -> None:
        await tx.execute("SELECT pg_sleep(1)")

    with pytest.raises(TransactionTimeout):
        await gateway.with_tenant_transaction(
            await _context(resolver, "clinic-a", "timeout-a"),
            exceed_timeout,
            options=TransactionOptions(timeout_ms=50, lock_timeout_ms=25),
        )

    async def marker(tx: TenantRepositoryHandle) -> str:
        row = await tx.fetch_one("SELECT marker FROM isolation_probe WHERE business_id = 42")
        assert row is not None
        return row[0]

    assert (
        await gateway.with_tenant_transaction(
            await _context(resolver, "clinic-b", "timeout-b"), marker
        )
        == "tenant-b"
    )


@pytest.mark.asyncio
async def test_cancellation_does_not_contaminate_next_tenant(
    gateway: TenantTransactionGateway, resolver: TenantResolver
) -> None:
    started = asyncio.Event()

    async def wait_in_database(tx: TenantRepositoryHandle) -> None:
        started.set()
        await tx.execute("SELECT pg_sleep(10)")

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
        row = await tx.fetch_one("SELECT marker FROM isolation_probe WHERE business_id = 42")
        assert row is not None
        return row[0]

    assert (
        await gateway.with_tenant_transaction(
            await _context(resolver, "clinic-b", "cancel-b"), marker
        )
        == "tenant-b"
    )
