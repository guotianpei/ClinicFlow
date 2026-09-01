"""Shared M01 test fixtures.

Helpers are exposed as fixtures rather than imported across test modules. The
earlier `from conftest import ...` worked only because pytest's prepend import
mode puts this directory on sys.path, which is an avoidable dependency on
collection mechanics.
"""

import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from alembic import command
from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.provisioning import (
    AUDIT_PROJECTOR_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    RUNTIME_ROLE,
)
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver

FIXTURE_EXECUTION_ID = uuid5(NAMESPACE_URL, "haloflow-test:fixture")
FIXTURE_CORRELATION_ID = uuid5(NAMESPACE_URL, "haloflow-test:fixture-correlation")

# Login shims. Every M01 database role is NOLOGIN by design, so a test that wants
# to act as one connects through a LOGIN role that is a member of it -- which is
# also how the application connects in production. The session then issues
# `SET ROLE`, so objects are owned by the group role rather than the shim.
TEST_ROLE_PASSWORD = "m01-local-test-only"
TEST_LOGIN_ROLES: dict[str, str] = {
    RUNTIME_ROLE: "haloflow_test_runtime_login",
    PROVISIONER_ROLE: "haloflow_test_provisioner_login",
    MIGRATOR_ROLE: "haloflow_test_migrator_login",
    AUDIT_PROJECTOR_ROLE: "haloflow_test_audit_projector_login",
}


class SingleTenantControlStore:
    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        if tenant_id != "clinic-a":
            return None
        return TenantRegistryRecord(
            tenant_id="clinic-a",
            schema_key="tenant_aaaaaaaa",
            lifecycle_state=LifecycleState.ACTIVE,
            schema_version=1,
        )


class ConfigurableControlStore:
    def __init__(self, record: TenantRegistryRecord | None) -> None:
        self._record = record

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        return self._record


def _principal_with(*capabilities: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset({"clinic-a"}),
        capabilities=frozenset(capabilities),
    )


@pytest.fixture
def execution_id() -> UUID:
    return FIXTURE_EXECUTION_ID


@pytest.fixture
def correlation_id() -> UUID:
    return FIXTURE_CORRELATION_ID


@pytest.fixture
def principal_with() -> Callable[..., Principal]:
    return _principal_with


@pytest.fixture
def control_store() -> SingleTenantControlStore:
    return SingleTenantControlStore()


@pytest.fixture
def make_control_store() -> Callable[[TenantRegistryRecord | None], ConfigurableControlStore]:
    return ConfigurableControlStore


@pytest.fixture
def make_resolver() -> Callable[..., TenantResolver]:
    def _make(store: object | None = None, *, ttl_seconds: int = 60) -> TenantResolver:
        return TenantResolver(
            store or SingleTenantControlStore(),  # type: ignore[arg-type]
            supported_schema_versions=range(1, 2),
            context_ttl=timedelta(seconds=ttl_seconds),
            clock=lambda: datetime.now(UTC),
        )

    return _make


@pytest.fixture
def resolve(
    make_resolver: Callable[..., TenantResolver],
) -> Callable[..., Awaitable[TenantContext]]:
    """Resolve a context with sensible defaults; override any argument by keyword."""

    async def _resolve(*, store: object | None = None, **overrides: Any) -> TenantContext:
        kwargs: dict[str, Any] = {
            "principal": _principal_with("appointments:read"),
            "tenant_hint": "clinic-a",
            "purpose": "treatment",
            "capabilities": frozenset({"appointments:read"}),
            "source": TrustedSource.WORKER,
            "execution_id": FIXTURE_EXECUTION_ID,
            "correlation_id": FIXTURE_CORRELATION_ID,
            "correlation_source": CorrelationSource.TRUSTED_INFRASTRUCTURE,
        }
        kwargs.update(overrides)
        return await make_resolver(store).resolve(**kwargs)

    return _resolve


async def _resolve_context(*, expired: bool = False) -> TenantContext:
    now = datetime.now(UTC)
    resolver = TenantResolver(
        SingleTenantControlStore(),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=-1 if expired else 60),
        clock=lambda: now,
    )
    return await resolver.resolve(
        principal=_principal_with("appointments:read"),
        tenant_hint="clinic-a",
        purpose="treatment",
        capabilities=frozenset({"appointments:read"}),
        source=TrustedSource.WORKER,
        execution_id=FIXTURE_EXECUTION_ID,
        correlation_id=FIXTURE_CORRELATION_ID,
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
    )


@pytest.fixture
async def active_context() -> TenantContext:
    return await _resolve_context()


@pytest.fixture
async def expired_context() -> TenantContext:
    return await _resolve_context(expired=True)


# ---------------------------------------------------------------------------
# PostgreSQL fixtures shared by the gateway and provisioning suites.
#
# These are fixtures rather than importable helpers for the reason at the top of
# this file: `from conftest import ...` works only because of pytest's prepend
# import mode, and that is an avoidable dependency on collection mechanics.
# ---------------------------------------------------------------------------


def _database_url_from(params: dict[str, object], dbname: str) -> str:
    """Build a postgresql:// URL, which is what alembic/env.py can rewrite."""

    user = params.get("user") or "postgres"
    password = params.get("password")
    credentials = f"{user}:{password}" if password else f"{user}"
    host = params.get("host") or "127.0.0.1"
    port = params.get("port") or 5432
    return f"postgresql://{credentials}@{host}:{port}/{dbname}"


def _apply_migrations_to(conninfo: str, revision: str = "head") -> None:
    previous = os.environ.get("HALOFLOW_MIGRATION_DATABASE_URL")
    os.environ["HALOFLOW_MIGRATION_DATABASE_URL"] = conninfo
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        if previous is None:
            os.environ.pop("HALOFLOW_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["HALOFLOW_MIGRATION_DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def test_conninfo() -> str:
    conninfo = os.getenv("HALOFLOW_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("HALOFLOW_TEST_DATABASE_URL is not configured")
    return conninfo


@pytest.fixture(scope="session")
def database_url() -> Callable[[dict[str, object], str], str]:
    return _database_url_from


@pytest.fixture(scope="session")
def apply_migrations() -> Callable[..., None]:
    return _apply_migrations_to


@pytest.fixture(scope="session")
def migrated_database(test_conninfo: str) -> str:
    """The test database at `head`, with the server version checked once.

    Both PostgreSQL suites depend on this, and Alembic is a no-op when already at
    head, so it is safe for whichever runs first to do the work.
    """

    _apply_migrations_to(test_conninfo)
    with psycopg.connect(test_conninfo, autocommit=True) as conn:
        version = int(conn.execute("SHOW server_version_num").fetchone()[0])  # type: ignore[index]
        database = conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
    if version < 170000:
        pytest.fail(f"M01 tests require PostgreSQL 17+, found {version}")
    if not str(database).startswith("haloflow_test"):
        pytest.fail("Refusing to initialize a database not named haloflow_test*")
    return test_conninfo


@pytest.fixture(scope="session")
def role_logins(migrated_database: str) -> dict[str, str]:
    """Conninfo per M01 role, reached through a LOGIN member of that role.

    Idempotent, so two session-scoped harnesses can both depend on it.
    """

    with psycopg.connect(migrated_database, autocommit=True) as conn:
        for group_role, login_role in TEST_LOGIN_ROLES.items():
            exists = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (login_role,)
            ).fetchone()
            if not (exists and exists[0]):
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                        sql.Identifier(login_role),
                        sql.Literal(TEST_ROLE_PASSWORD),
                        sql.Identifier(group_role),
                    )
                )
            conn.execute(
                sql.SQL("ALTER ROLE {} SET search_path = ''").format(sql.Identifier(login_role))
            )

    conninfos: dict[str, str] = {}
    for group_role, login_role in TEST_LOGIN_ROLES.items():
        params = conninfo_to_dict(migrated_database)
        params.update(user=login_role, password=TEST_ROLE_PASSWORD)
        conninfos[group_role] = make_conninfo(**params)
    return conninfos


def _reset_tenants_in(conninfo: str, tenant_ids: Sequence[str], schema_keys: Sequence[str]) -> None:
    """Remove test tenants and their schemas so a suite starts from nothing.

    Four tables reference `shared.tenants`, and two of them --
    `tenant_state_history` and `access_audit_log` -- are append-only by trigger,
    so a plain DELETE cannot clear a tenant the provisioner has activated. Both
    triggers are disabled for the duration, as the table owner, and re-enabled in
    a `finally`. This is a harness escape hatch and deliberately the only one: no
    production role can do it, and TC-E23 and TC-E25 assert that.
    """

    append_only = (
        ("shared.tenant_state_history", "tenant_state_history_append_only"),
        ("shared.access_audit_log", "access_audit_log_append_only"),
    )
    referencing = (
        "shared.schema_migrations",
        "shared.tenant_state_history",
        "shared.access_audit_log",
        "shared.isolation_alerts",
    )

    with psycopg.connect(conninfo, autocommit=True) as conn:
        for schema_key in schema_keys:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_key))
            )
        for table, trigger in append_only:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        try:
            for table in referencing:
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(%s)", (list(tenant_ids),)
                )
            conn.execute(
                "DELETE FROM shared.tenants WHERE tenant_id = ANY(%s)", (list(tenant_ids),)
            )
        finally:
            for table, trigger in append_only:
                conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


@pytest.fixture(scope="session")
def reset_tenants() -> Callable[[str, Sequence[str], Sequence[str]], None]:
    return _reset_tenants_in
