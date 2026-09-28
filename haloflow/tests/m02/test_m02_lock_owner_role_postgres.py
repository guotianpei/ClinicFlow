"""CP2-2a role and revision rows (2A-R01–R05, R07) and graph rows (2A-M01–M06).

Shared-cluster mutations (rename, attribute change, edge change) are made by
ADMIN, restored in `finally`, and followed by a re-assertion of the restored
baseline. This module must run serially: no parallel workers.
"""

import importlib.util
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

pytestmark = pytest.mark.postgres

REVISION_004 = Path("alembic/versions/004_m02_lock_owner_role.py")
BOOTSTRAP_SUPERUSER_OID = 10


def _revision_004_sql() -> str:
    spec = importlib.util.spec_from_file_location("m02_revision_004_sql", REVISION_004)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value: str = module.LOCK_OWNER_ROLE_SQL
    return value


# --- 2A-R01 .. R04 ---------------------------------------------------------


def test_2a_r01_lock_owner_attributes_are_all_false(m02: ModuleType, m02_ids: Any) -> None:
    assert m02.role_attributes(m02_ids, m02.LOCK_OWNER) == dict.fromkeys(m02.ROLE_ATTRIBUTES, False)


def test_2a_r02_exactly_one_edge_into_the_lock_owner_n1(m02: ModuleType, m02_ids: Any) -> None:
    edges = m02.edges_into(m02_ids, m02.LOCK_OWNER)
    assert [(m, s, i, a) for m, s, i, a, _ in edges] == [(m02.MIGRATOR, True, False, False)]


def test_2a_r03_real_migration_login_chain_reaches_the_lock_owner(
    m02: ModuleType, m02_ids: Any
) -> None:
    """CP2-D17: login shim → migrator → lock owner; no superuser in the chain."""

    login = conninfo_to_dict(m02_ids.logins[m02.MIGRATOR])["user"]
    with psycopg.connect(m02_ids.logins[m02.MIGRATOR], autocommit=True) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(m02.MIGRATOR)))
        assert conn.execute("SELECT session_user, current_user").fetchone() == (
            login,
            m02.MIGRATOR,
        )
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(m02.LOCK_OWNER)))
        assert conn.execute("SELECT session_user, current_user").fetchone() == (
            login,
            m02.LOCK_OWNER,
        )
        chain_superusers = conn.execute(
            "SELECT count(*) FROM pg_catalog.pg_roles "
            "WHERE rolname IN (session_user, %s, %s) AND rolsuper",
            (m02.MIGRATOR, m02.LOCK_OWNER),
        ).fetchone()
        assert chain_superusers == (0,)


def test_2a_r04_runtime_cannot_set_role_to_the_lock_owner(m02: ModuleType, m02_ids: Any) -> None:
    """CP2-D19."""

    with m02.connect_as(m02_ids, "RT") as conn:
        m02.expect_sqlstate(
            conn, sql.SQL("SET ROLE {}").format(sql.Identifier(m02.LOCK_OWNER)), None, "42501"
        )
        m02.assert_current_user(conn, m02.RUNTIME)


# --- 2A-R05 ----------------------------------------------------------------


def test_2a_r05_revision_sql_never_normalizes_an_existing_role(
    m02: ModuleType, m02_ids: Any
) -> None:
    """Revision-isolated: ADMIN runs the `004` upgrade SQL constant directly."""

    revision_sql = _revision_004_sql()
    target = sql.Identifier(m02.LOCK_OWNER)
    try:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN").format(target))
            conn.execute(revision_sql)
        attributes = m02.role_attributes(m02_ids, m02.LOCK_OWNER)
        assert attributes is not None and attributes["rolcanlogin"] is True
        # The edge statement is idempotent: still exactly the one N1 edge.
        edges = m02.edges_into(m02_ids, m02.LOCK_OWNER)
        assert [(m, s, i, a) for m, s, i, a, _ in edges] == [(m02.MIGRATOR, True, False, False)]
    finally:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(target))
        m02.assert_baseline_role(m02_ids)


# --- 2A-R07 ----------------------------------------------------------------


@pytest.fixture
def r07_deploy(
    m02: ModuleType,
    m02_ids: Any,
    database_url: Callable[[dict[str, object], str], str],
    apply_migrations: Callable[..., None],
) -> Iterator[dict[str, Any]]:
    """Setup (ADMIN, not measured) and cleanup for 2A-R07."""

    suffix = secrets.token_hex(4)
    dbname = f"haloflow_test_deploy_{suffix}"
    deploy = f"haloflow_test_deploy_login_{suffix}"
    password = secrets.token_hex(16)
    saved = f"{m02.LOCK_OWNER}_r07_saved"
    admin_params: dict[str, object] = dict(conninfo_to_dict(m02_ids.admin))
    renamed = False
    created_db = False
    created_role = False
    try:
        with m02.connect_admin(m02_ids) as conn:
            assert conn.execute(
                "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
            ).fetchone() == (True,), "R07 needs a superuser ADMIN (erratum 1 ET-3)"
            assert conn.execute(
                "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = %s", (saved,)
            ).fetchone() == (0,)
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
            created_db = True
        apply_migrations(database_url(admin_params, dbname), "003")
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB CREATEROLE "
                    "NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(deploy), sql.Literal(password))
            )
            created_role = True
            conn.execute(
                sql.SQL("ALTER ROLE {} SET createrole_self_grant = ''").format(
                    sql.Identifier(deploy)
                )
            )
        new_db = make_conninfo(m02_ids.admin, dbname=dbname)
        with psycopg.connect(new_db, autocommit=True) as conn:
            row = conn.execute(
                "SELECT n.nspname FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'alembic_version' AND c.relkind = 'r'"
            ).fetchall()
            assert len(row) == 1
            version_schema = row[0][0]
            conn.execute(
                sql.SQL("GRANT SELECT, UPDATE ON {} TO {}").format(
                    sql.Identifier(version_schema, "alembic_version"), sql.Identifier(deploy)
                )
            )
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(
                sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                    sql.Identifier(m02.LOCK_OWNER), sql.Identifier(saved)
                )
            )
            renamed = True
        deploy_params = {**admin_params, "user": deploy, "password": password}
        yield {
            "dbname": dbname,
            "deploy": deploy,
            "version_schema": version_schema,
            "deploy_url": database_url(deploy_params, dbname),
            "admin_new_db": new_db,
        }
    finally:
        with m02.connect_admin(m02_ids) as conn:
            if created_db:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(dbname)
                    )
                )
            if renamed:
                conn.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(m02.LOCK_OWNER))
                )
                conn.execute(
                    sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                        sql.Identifier(saved), sql.Identifier(m02.LOCK_OWNER)
                    )
                )
            if created_role:
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(deploy)))
        m02.assert_baseline_role(m02_ids)


def test_2a_r07_non_superuser_deployment_login_runs_revision_004(
    m02: ModuleType,
    m02_ids: Any,
    r07_deploy: dict[str, Any],
    apply_migrations: Callable[..., None],
) -> None:
    """EC-3 evidence (not closure): the real Alembic connection is a non-superuser."""

    from sqlalchemy import event
    from sqlalchemy.pool import Pool

    deploy = r07_deploy["deploy"]
    new_db = r07_deploy["admin_new_db"]

    # Pre-assertions: DEPLOY's exact attributes and rights, and no haloflow membership.
    with psycopg.connect(new_db, autocommit=True) as conn:
        attributes = conn.execute(
            "SELECT rolsuper, rolcreatedb, rolreplication, rolbypassrls, rolcreaterole, "
            "rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = %s",
            (deploy,),
        ).fetchone()
        assert attributes == (False, False, False, False, True, True)
        rights = conn.execute(
            "SELECT pg_catalog.has_database_privilege(%s, current_database(), 'CONNECT'), "
            "pg_catalog.has_schema_privilege(%s, %s, 'USAGE'), "
            "pg_catalog.has_table_privilege(%s, %s, 'SELECT'), "
            "pg_catalog.has_table_privilege(%s, %s, 'UPDATE')",
            (
                deploy,
                deploy,
                r07_deploy["version_schema"],
                deploy,
                f"{r07_deploy['version_schema']}.alembic_version",
                deploy,
                f"{r07_deploy['version_schema']}.alembic_version",
            ),
        ).fetchone()
        assert rights == (True, True, True, True)
        memberships = conn.execute(
            "SELECT count(*) FROM pg_catalog.pg_auth_members AS a "
            "JOIN pg_catalog.pg_roles AS r ON r.oid = a.roleid "
            "JOIN pg_catalog.pg_roles AS m ON m.oid = a.member "
            "WHERE m.rolname = %s AND r.rolname LIKE 'haloflow\\_%%'",
            (deploy,),
        ).fetchone()
        assert memberships == (0,)

    probes: list[tuple[Any, ...]] = []

    def _probe(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(
            "SELECT session_user, current_user, "
            "(SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = session_user), "
            "current_setting('createrole_self_grant')"
        )
        probes.append(tuple(cursor.fetchone()))
        cursor.close()
        dbapi_connection.rollback()

    event.listen(Pool, "connect", _probe)
    try:
        apply_migrations(r07_deploy["deploy_url"], "004")
    finally:
        event.remove(Pool, "connect", _probe)

    # Probe: env.py opens one NullPool engine and connects once (:37–43).
    assert probes == [(deploy, deploy, False, "")]

    with psycopg.connect(new_db, autocommit=True) as conn:
        assert conn.execute(
            sql.SQL("SELECT version_num FROM {}").format(
                sql.Identifier(r07_deploy["version_schema"], "alembic_version")
            )
        ).fetchall() == [("004",)]
        created = conn.execute(
            f"SELECT {', '.join(m02.ROLE_ATTRIBUTES)} FROM pg_catalog.pg_roles WHERE rolname = %s",
            (m02.LOCK_OWNER,),
        ).fetchone()
        assert created == (False,) * 6
        edges = conn.execute(
            """
            SELECT m.rolname, a.admin_option, a.set_option, a.inherit_option, a.grantor
              FROM pg_catalog.pg_auth_members AS a
              JOIN pg_catalog.pg_roles AS r ON r.oid = a.roleid
              JOIN pg_catalog.pg_roles AS m ON m.oid = a.member
             WHERE r.rolname = %s
             ORDER BY m.rolname
            """,
            (m02.LOCK_OWNER,),
        ).fetchall()
        deploy_oid = conn.execute(
            "SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s", (deploy,)
        ).fetchone()
    assert deploy_oid is not None
    by_member = {row[0]: row[1:] for row in edges}
    assert len(edges) == 2
    # (a) creator edge: ADMIN true, SET/INHERIT false, grantor bootstrap superuser.
    assert by_member[deploy] == (True, False, False, BOOTSTRAP_SUPERUSER_OID)
    # (b) N1 edge: SET true, INHERIT false, ADMIN false, grantor DEPLOY.
    assert by_member[m02.MIGRATOR] == (False, True, False, deploy_oid[0])
    # Scope (stated, not asserted): stage 1's graph check reads only edges whose
    # endpoints are both controlled roles. DEPLOY is not one, so edge (a) is not
    # examined by it and is not part of the N1 exception.


# --- 2A-M01 .. M06 (graph rows moved to 2a) --------------------------------


def _provision_production(m02: ModuleType, ids: Any, tenant: tuple[str, str]) -> Any:
    return m02.provision_sync(ids, m02.production_registry(), tenant)


def test_2a_m01_positive_control_provisioning_succeeds(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    m02.assert_baseline_role(m02_ids)
    outcome = _provision_production(m02, m02_ids, m02_tenant)
    assert outcome.schema_version == 3


def _expect_stage_one_refusal(m02: ModuleType, ids: Any, tenant: tuple[str, str]) -> None:
    from haloflow.m01.errors import ProvisioningFailed

    with pytest.raises(ProvisioningFailed) as refused:
        _provision_production(m02, ids, tenant)
    m02.assert_stage_one_refusal(ids, refused.value, tenant)


def test_2a_m02_revision_effect_absent_role_renamed_away(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """CP2-D08."""

    saved = f"{m02.LOCK_OWNER}_m02_saved"
    with m02.connect_admin(m02_ids) as conn:
        conn.execute(
            sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                sql.Identifier(m02.LOCK_OWNER), sql.Identifier(saved)
            )
        )
    try:
        _expect_stage_one_refusal(m02, m02_ids, m02_tenant)
    finally:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(
                sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                    sql.Identifier(saved), sql.Identifier(m02.LOCK_OWNER)
                )
            )
        m02.assert_baseline_role(m02_ids)


@pytest.mark.parametrize(
    "row,options",
    [
        ("m03-edge-revoked", None),
        ("m04-set-false", "INHERIT FALSE, SET FALSE, ADMIN FALSE"),
        ("m05a-inherit-true", "INHERIT TRUE, SET TRUE, ADMIN FALSE"),
        ("m05b-admin-option", "INHERIT FALSE, SET TRUE, ADMIN TRUE"),
    ],
)
def test_2a_m03_to_m05_edge_variants_are_refused(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str], row: str, options: str | None
) -> None:
    """CP2-D10, D14, D15 (ET-3: revoke all, assert none, then grant the variant)."""

    try:
        m02.set_lock_owner_edge(m02_ids, options)
        _expect_stage_one_refusal(m02, m02_ids, m02_tenant)
    finally:
        m02.restore_lock_owner(m02_ids)


def test_2a_m06_undeclared_controlled_edge_is_refused(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """CP2-D16: `GRANT haloflow_m02_lock_owner TO haloflow_runtime`."""

    lock_owner, runtime = sql.Identifier(m02.LOCK_OWNER), sql.Identifier(m02.RUNTIME)
    with m02.connect_admin(m02_ids) as conn:
        conn.execute(sql.SQL("GRANT {} TO {}").format(lock_owner, runtime))
    try:
        _expect_stage_one_refusal(m02, m02_ids, m02_tenant)
    finally:
        with m02.connect_admin(m02_ids) as conn:
            grantors = conn.execute(
                "SELECT g.rolname FROM pg_catalog.pg_auth_members AS a "
                "JOIN pg_catalog.pg_roles AS r ON r.oid = a.roleid "
                "JOIN pg_catalog.pg_roles AS m ON m.oid = a.member "
                "JOIN pg_catalog.pg_roles AS g ON g.oid = a.grantor "
                "WHERE r.rolname = %s AND m.rolname = %s",
                (m02.LOCK_OWNER, m02.RUNTIME),
            ).fetchall()
            for (grantor,) in grantors:
                conn.execute(
                    sql.SQL("REVOKE {} FROM {} GRANTED BY {}").format(
                        lock_owner, runtime, sql.Identifier(grantor)
                    )
                )
        m02.assert_baseline_role(m02_ids)
