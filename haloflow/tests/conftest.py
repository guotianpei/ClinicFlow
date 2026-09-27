"""Session database fixtures shared by every test package (M01 and M02).

Moved text-identically from `tests/m01/conftest.py` (CP2-2a E-9, owner-approved
2026-09-26) so pytest defines each fixture exactly once for `tests/m01` and
`tests/m02`, and `migrated_database` / `role_logins` run once per session.

Helpers are exposed as fixtures rather than imported across test modules. The
earlier `from conftest import ...` worked only because pytest's prepend import
mode puts this directory on sys.path, which is an avoidable dependency on
collection mechanics.
"""

import os
from collections.abc import Callable, Sequence
from typing import Any

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from alembic import command
from haloflow.m01.provisioning import (
    AUDIT_PROJECTOR_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    RUNTIME_ROLE,
)

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


# ---------------------------------------------------------------------------
# PostgreSQL fixtures shared by the gateway and provisioning suites.
#
# These are fixtures rather than importable helpers for the reason at the top of
# this file: `from conftest import ...` works only because of pytest's prepend
# import mode, and that is an avoidable dependency on collection mechanics.
# ---------------------------------------------------------------------------


def _database_url_from(params: dict[str, object], dbname: str) -> str:
    """Build a postgresql:// URL, which is what alembic/env.py can rewrite.

    D-02 (PORTABILITY-01). The authority is empty and every option goes in the
    query string, percent-encoded, so psycopg and the SQLAlchemy dialect read
    the same mapping. Nothing is invented and the environment is never read: an
    option that was not supplied is left for libpq to resolve. A ``dbname`` in
    ``params`` is replaced by the requested name, and options whose value is
    exactly "" are omitted. Unrecognised keys are refused.

    Refusals are ValueErrors with fixed messages, raised outside any ``except``
    block, so no library message (which can echo key text) is chained as
    ``__cause__`` or ``__context__``.
    """
    from urllib.parse import quote

    if not isinstance(dbname, str) or dbname == "":
        raise ValueError("requested database name must be a non-empty str")
    supplied: dict[str, str] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("connection option values must be str")
        supplied[key] = value

    # libpq validates the keys; values are not validated until it connects.
    rejected = False
    try:
        make_conninfo("", **supplied)
    except (psycopg.Error, ValueError, TypeError):
        rejected = True
    if rejected:
        raise ValueError("invalid connection options")

    carried = {k: v for k, v in supplied.items() if k != "dbname" and v != ""}
    intended = {"dbname": dbname, **carried}
    pairs = [("dbname", dbname), *sorted(carried.items())]
    url = "postgresql:///?" + "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)

    parsed: dict[str, Any] | None
    try:
        parsed = conninfo_to_dict(url)
    except (psycopg.Error, ValueError, TypeError):
        parsed = None
    if parsed != intended:
        raise ValueError("invalid connection options")
    return url


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


# ---- CP2-2a 2A-F01: shared-fixture initialization counter ----
#
# Added below the moved block (which stays text-identical). A root-level hook
# sees every fixture execution in the session, whichever package requested it.
# Only real executions are counted; a cached session value is not re-executed.

SHARED_FIXTURE_SETUPS: dict[str, int] = {"migrated_database": 0, "role_logins": 0}


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    if fixturedef.argname in SHARED_FIXTURE_SETUPS:
        SHARED_FIXTURE_SETUPS[fixturedef.argname] += 1
    yield


@pytest.fixture(scope="session")
def shared_fixture_setups() -> dict[str, int]:
    """The live counter, for the one M02 consumer that reports it (2A-F01)."""

    return SHARED_FIXTURE_SETUPS
