"""L-1W W2: privilege and catalogue inventory of revision 008 (investigation §3.2).

Rows: B-04, PRIV-01, PRIV-02, SP-05, SP-06, SP-07a, SP-07d, FX-02.
Exact sets, read from the catalogue with aclexplode: no "at least" assertions.
"""

from __future__ import annotations

from typing import Any, Final

import l1w_db_support as db
import psycopg
import pytest
from psycopg import errors, sql

pytestmark = pytest.mark.postgres

OWNER: Final = db.IDENTITY_OWNER

# Function -> exact identity arguments (SP-06: no actor_kind parameter anywhere).
SIGNATURES: Final = {
    "l1w_admit": "p_issuer text, p_subject text, p_sid text, p_auth_time timestamp with time"
                 " zone, p_assured boolean",
    "l1w_logout": "p_issuer text, p_subject text, p_sid text, p_auth_time timestamp with time"
                  " zone",
    "l1w_admin_bind": "p_tenant character varying, p_staff uuid, p_issuer text, p_subject text,"
                      " p_actor uuid",
    "l1w_admin_unbind": "p_tenant character varying, p_staff uuid, p_actor uuid",
    "l1w_admin_set_setting": "p_tenant character varying, p_setting text, p_value text,"
                             " p_actor uuid",
    "l1w_support_bind": "p_tenant character varying, p_staff uuid, p_issuer text,"
                        " p_subject text",
    "l1w_support_unbind": "p_tenant character varying, p_staff uuid",
    "l1w_support_set_setting": "p_tenant character varying, p_setting text, p_value text",
    "l1w_support_list_tenant_settings": "p_tenant character varying",
}

# Function -> exact EXECUTE grantees besides the owner (PUBLIC never).
EXECUTE: Final = {
    "l1w_admit": {db.RUNTIME},
    "l1w_logout": {db.RUNTIME},
    "l1w_admin_bind": {db.STAFF_OWNER},
    "l1w_admin_unbind": {db.STAFF_OWNER},
    "l1w_admin_set_setting": {db.STAFF_OWNER},
    "l1w_support_bind": {db.SUPPORT},
    "l1w_support_unbind": {db.SUPPORT},
    "l1w_support_set_setting": {db.SUPPORT},
    "l1w_support_list_tenant_settings": {db.SUPPORT},
}

GRANT_COLUMNS: Final = frozenset(
    {"grant_id", "actor_id", "tenant_id", "ticket_reference", "capabilities", "state",
     "starts_at", "expires_at"}
)


def _shared_function_rows(env: db.L1WEnv) -> list[tuple[Any, ...]]:
    """One row per pg_proc entry (overloads are separate rows, never collapsed):
    (name, identity arguments, owner, security definer, config, oid)."""

    return env.all(
        "SELECT p.proname, pg_get_function_identity_arguments(p.oid),"
        " pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig, p.oid"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'shared' AND p.proname LIKE 'l1w\\_%'")


def _installed(env: db.L1WEnv, name: str) -> bool:
    return any(row[0] == name for row in _shared_function_rows(env))


def test_sp06_definer_signatures_are_exact(env: db.L1WEnv) -> None:
    pairs = [(row[0], row[1]) for row in _shared_function_rows(env)]
    assert len(pairs) == len(SIGNATURES), "extra or missing l1w_* entries (overloads count)"
    assert sorted(pairs) == sorted(SIGNATURES.items())


def test_priv02_every_l1w_function_is_a_pinned_security_definer(env: db.L1WEnv) -> None:
    rows = _shared_function_rows(env)
    assert sorted((row[0], row[1]) for row in rows) == sorted(SIGNATURES.items())
    for name, args, owner, secdef, config, _oid in rows:
        assert owner == OWNER, (name, args)
        assert secdef is True, (name, args)
        assert config == ["search_path=pg_catalog, pg_temp"], (name, args)


def test_priv01_function_execute_grantees_are_exact(env: db.L1WEnv) -> None:
    rows = _shared_function_rows(env)
    assert sorted((row[0], row[1]) for row in rows) == sorted(SIGNATURES.items())
    for name, args, _owner, _secdef, _config, oid in rows:
        acl = env.all(
            "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
            " x.privilege_type, x.is_grantable FROM pg_proc p,"
            " LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x"
            " WHERE p.oid = %s", (oid,))
        assert sorted(acl) == sorted(
            (grantee, "EXECUTE", False) for grantee in EXECUTE[name] | {OWNER}), (name, args)


def test_priv01_tables_have_no_grant_beyond_the_owner(env: db.L1WEnv) -> None:
    rows = env.all(
        "SELECT c.relname, pg_get_userbyid(c.relowner),"
        " (SELECT array_agg(DISTINCT CASE WHEN x.grantee = 0 THEN 'PUBLIC'"
        "   ELSE pg_get_userbyid(x.grantee) END)"
        "  FROM aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) x),"
        " (SELECT count(*) FROM pg_attribute a WHERE a.attrelid = c.oid"
        "   AND a.attacl IS NOT NULL)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'shared' AND c.relname = ANY(%s)", (list(db.L1W_TABLES),))
    assert {row[0] for row in rows} == set(db.L1W_TABLES)
    for name, owner, grantees, column_acls in rows:
        assert owner == OWNER, name
        assert set(grantees) == {OWNER}, name
        # Version-scoped (Codex 223109Z/224307Z): alembic 009 (L-1W W3a) adds exactly
        # the staff owner's SELECT on three support_operator_logins columns; every
        # other table keeps no column ACL. The exact 009 set is pinned below and by
        # PRIV-W3-01.
        assert column_acls == (3 if name == "support_operator_logins" else 0), name
    assert {(c, g, p) for c, g, p in env.all(
        "SELECT a.attname, pg_get_userbyid(x.grantee), x.privilege_type FROM pg_attribute a,"
        " LATERAL aclexplode(a.attacl) x"
        " WHERE a.attrelid = 'shared.support_operator_logins'::regclass")} == {
        (column, db.STAFF_OWNER, "SELECT") for column in ("login_oid", "login_name", "actor_id")}


def test_priv01_identity_owner_reads_exactly_eight_grant_columns(env: db.L1WEnv) -> None:
    rows = env.all(
        "SELECT a.attname, x.privilege_type FROM pg_attribute a,"
        " LATERAL aclexplode(a.attacl) x"
        " WHERE a.attrelid = 'shared.support_access_grants'::regclass"
        " AND x.grantee = %s::regrole", (OWNER,))
    assert {(name, privilege) for name, privilege in rows} == {
        (column, "SELECT") for column in GRANT_COLUMNS}
    assert env.one(
        "SELECT has_table_privilege(%s, 'shared.support_access_grants',"
        " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')", (OWNER,)) == (False,)


def test_priv01_schema_usage_and_role_attributes(env: db.L1WEnv) -> None:
    for role in (OWNER, db.SUPPORT, db.STAFF_OWNER, db.RUNTIME):
        assert env.one(
            "SELECT has_schema_privilege(%s, 'shared', 'USAGE'),"
            " has_schema_privilege(%s, 'shared', 'CREATE')", (role, role)) == (True, False), role
    for role in (OWNER, db.SUPPORT):
        assert env.one(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
            " rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)
        ) == (False, False, False, False, False, False), role
        # Exclude only the exact fixture logins this environment created; any other
        # member, whatever its name, must be the migrator edge (Codex 192346Z).
        fixtures = sorted(env.logins.values())
        edges = env.all(
            "SELECT m.rolname, a.inherit_option, a.set_option, a.admin_option"
            " FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid"
            " JOIN pg_roles m ON m.oid = a.member"
            " WHERE r.rolname = %s AND NOT (m.rolname = ANY(%s)) ORDER BY 1", (role, fixtures))
        assert edges == [("haloflow_migrator", False, True, False)], role
        # The fixture logins' own edges, asserted separately: support fixture logins are
        # plain LOGIN members (CREATE ROLE ... IN ROLE); nothing joins the identity owner.
        fixture_edges = env.all(
            "SELECT m.rolname, m.rolcanlogin, m.rolsuper, a.admin_option"
            " FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid"
            " JOIN pg_roles m ON m.oid = a.member"
            " WHERE r.rolname = %s AND m.rolname = ANY(%s) ORDER BY 1", (role, fixtures))
        if role == OWNER:
            assert fixture_edges == [], role
        else:
            assert all(edge[1:] == (True, False, False) for edge in fixture_edges), fixture_edges
        assert env.all(
            "SELECT r.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid"
            " JOIN pg_roles m ON m.oid = a.member WHERE m.rolname = %s", (role,)) == [], role


@pytest.mark.parametrize("role_key", ["runtime", "staff_owner", "support"])
def test_b04_no_role_but_the_owner_can_touch_the_identity_tables(
    env: db.L1WEnv, role_key: str
) -> None:
    group = {"runtime": db.RUNTIME, "staff_owner": db.STAFF_OWNER, "support": db.SUPPORT}[role_key]
    if role_key == "support":
        env.login("b04support", db.SUPPORT)
        role_key = "b04support"
    for table in db.L1W_TABLES:
        if role_key == "staff_owner" and table == "support_operator_logins":
            # Version-scoped: alembic 009 (L-1W W3a) grants the staff owner SELECT on
            # exactly login_oid, login_name and actor_id; no table privilege, no other
            # column, no write.
            assert env.one(
                "SELECT has_table_privilege(%s, %s, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
                "REFERENCES,TRIGGER') OR has_any_column_privilege(%s, %s, 'INSERT,UPDATE,"
                "REFERENCES')", (group, f"shared.{table}", group, f"shared.{table}")
            ) == (False,)
            with env.connect(role_key) as conn, pytest.raises(errors.InsufficientPrivilege):
                conn.execute("SELECT added_at FROM shared.support_operator_logins LIMIT 1")
            continue
        assert env.one(
            "SELECT has_table_privilege(%s, %s, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
            "REFERENCES,TRIGGER') OR has_any_column_privilege(%s, %s, 'SELECT,INSERT,UPDATE,"
            "REFERENCES')", (group, f"shared.{table}", group, f"shared.{table}")) == (False,)
        with env.connect(role_key) as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute(sql.SQL("SELECT 1 FROM shared.{} LIMIT 1").format(
                sql.Identifier(table)))


_CROSS_CALLS: Final = [
    ("support", "l1w_admin_set_setting", "SELECT shared.l1w_admin_set_setting('x','y','z',"
     "gen_random_uuid())"),
    ("support", "l1w_admit", "SELECT * FROM shared.l1w_admit('i','s','d',now(),true)"),
    ("runtime", "l1w_support_unbind", "SELECT shared.l1w_support_unbind('x',gen_random_uuid())"),
    ("runtime", "l1w_admin_bind", "SELECT shared.l1w_admin_bind('x',gen_random_uuid(),'i','s',"
     "gen_random_uuid())"),
    ("staff_owner", "l1w_support_bind", "SELECT shared.l1w_support_bind('x',gen_random_uuid(),"
     "'i','s')"),
    ("staff_owner", "l1w_admit", "SELECT * FROM shared.l1w_admit('i','s','d',now(),true)"),
]


@pytest.mark.parametrize(("role_key", "function", "statement"), _CROSS_CALLS,
                         ids=[f"{r}-{f}" for r, f, _ in _CROSS_CALLS])
def test_sp05_sp07a_cross_role_calls_are_permission_denied(
    env: db.L1WEnv, role_key: str, function: str, statement: str
) -> None:
    # The definer must exist (so a tree without 008 fails here rather than passing on
    # a missing schema privilege), and the cross-role call must still be refused.
    assert function in SIGNATURES
    assert _installed(env, function), f"{function} is not installed"
    if role_key == "support":
        env.login("sp05support", db.SUPPORT)
        role_key = "sp05support"
    with env.connect(role_key) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement)


def test_sp07a_runtime_has_no_execute_on_admin_or_support_definers(env: db.L1WEnv) -> None:
    for name in SIGNATURES:
        expected = name in ("l1w_admit", "l1w_logout")
        row = env.one(
            "SELECT has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'shared' AND p.proname = %s", (db.RUNTIME, name))
        assert row == (expected,), name


def test_sp07d_staff_owner_cannot_log_in_and_has_no_runtime_edge(env: db.L1WEnv) -> None:
    # The admin definers this tranche gives the staff owner must exist (red without 008).
    for name in ("l1w_admin_bind", "l1w_admin_unbind", "l1w_admin_set_setting"):
        assert env.one(
            "SELECT has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'shared' AND p.proname = %s", (db.STAFF_OWNER, name)
        ) == (True,), name
    assert env.one("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s",
                   (db.STAFF_OWNER,)) == (False,)
    assert env.one("SELECT pg_has_role(%s, %s, 'MEMBER'), pg_has_role(%s, %s, 'MEMBER')",
                   (db.RUNTIME, db.STAFF_OWNER, db.STAFF_OWNER, db.RUNTIME)) == (False, False)
    # 008 adds a runtime-reachable path to the admin definers only through W3's
    # m01_staff_admin_v2; W2 adds no edge (checked against the support role too).
    assert env.one("SELECT pg_has_role(%s, %s, 'MEMBER')",
                   (db.RUNTIME, db.SUPPORT)) == (False,)


def test_fx02_cleanup_restores_triggers_even_when_the_statement_fails(env: db.L1WEnv) -> None:
    with env.connect("admin", autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            db._with_triggers_disabled(  # noqa: SLF001 - the harness helper under test
                conn, "staff_sessions", sql.SQL("SELECT no_such_column FROM shared.{}").format(
                    sql.Identifier("staff_sessions")), None)
        states = conn.execute(
            "SELECT tgname, tgenabled FROM pg_trigger"
            " WHERE tgrelid = 'shared.staff_sessions'::regclass AND NOT tgisinternal").fetchall()
    assert states, "the session table has no user triggers"
    assert {state for _, state in states} == {"O"}
