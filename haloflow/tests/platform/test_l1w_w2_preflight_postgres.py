"""L-1W W2: revision 008 refuses a pre-existing unsafe role before any mutation.

Rows: PF-01 (safe pre-existing roles stay usable), PF-02 (unsafe attribute), PF-03
(extra incoming membership), PF-04 (extra outgoing membership), PF-05 (migrator edge
with other options). Codex 20261102T191746Z: in W2, not a documented gap.

Roles are cluster-global, so a second database does not isolate them. Each case
therefore makes the *existing* role unsafe inside the disposable cluster (the harness
guard applies), runs 007 -> 008 on a fresh database copied from a 007 template, and
restores the role in a ``finally``, asserting the restoration. The oracle is: the
upgrade raises with the fixed ``L1W_ROLE_UNSAFE`` message; the fresh database has no
008 object; and the role is exactly as the test left it (008 never ALTERs it).

Pinned preflight rule (plan v1 §1 extension, for the red gate):
- both roles: NOLOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION,
  NOBYPASSRLS; no outgoing membership; a pre-existing migrator edge must be exactly
  INHERIT FALSE, SET TRUE, ADMIN FALSE;
- ``haloflow_identity_owner``: no incoming member other than the migrator;
- ``haloflow_support``: incoming members other than the migrator must be LOGIN,
  non-superuser roles without ADMIN (support logins join it out of band by design).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from typing import Any

import l1w_db_support as db
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

pytestmark = pytest.mark.postgres

ATTRIBUTES = ("LOGIN", "SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS")
ROLES = (db.IDENTITY_OWNER, db.SUPPORT)


@pytest.fixture(scope="module")
def template_007(
    env: db.L1WEnv, database_url: Callable[..., str], apply_migrations: Callable[..., None]
) -> Iterator[str]:
    name = f"haloflow_test_l1w_pf_{env.token}_tpl"
    with psycopg.connect(env.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        apply_migrations(database_url(conninfo_to_dict(env.admin), name), "007")
        yield name
    finally:
        with psycopg.connect(env.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))


@pytest.fixture
def fresh_007(
    env: db.L1WEnv, template_007: str, database_url: Callable[..., str]
) -> Iterator[str]:
    name = f"haloflow_test_l1w_pf_{env.token}_{secrets.token_hex(3)}"
    with psycopg.connect(env.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(template_007)))
    try:
        yield database_url(conninfo_to_dict(env.admin), name)
    finally:
        with psycopg.connect(env.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))


def _role_state(env: db.L1WEnv, role: str) -> tuple[Any, ...]:
    attrs = env.one(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
        " rolbypassrls FROM pg_roles WHERE rolname = %s", (role,))
    edges = env.all(
        "SELECT r.rolname, m.rolname, a.inherit_option, a.set_option, a.admin_option"
        " FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid"
        " JOIN pg_roles m ON m.oid = a.member WHERE r.rolname = %s OR m.rolname = %s"
        " ORDER BY 1, 2", (role, role))
    return attrs, tuple(edges)


def _drop_role(env: db.L1WEnv, name: str) -> None:
    """Every token role a case creates is dropped, even when its setup failed."""

    assert name.startswith(f"haloflow_test_l1w_{env.token}_")
    with psycopg.connect(env.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def _has_008(url: str) -> bool:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT to_regclass('shared.staff_sessions') IS NOT NULL"
            " OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n"
            " ON n.oid = p.pronamespace WHERE n.nspname = 'shared'"
            " AND p.proname LIKE 'l1w\\_%')").fetchone()
    assert row is not None
    return bool(row[0])


def _refused(env: db.L1WEnv, url: str, role: str,
             apply_migrations: Callable[..., None]) -> None:
    before = _role_state(env, role)
    with pytest.raises(Exception, match="L1W_ROLE_UNSAFE"):  # alembic wraps the error
        apply_migrations(url, "008")
    assert not _has_008(url), "008 objects created despite the refusal"
    assert _role_state(env, role) == before, "008 altered the pre-existing role"


def test_pf01_safe_pre_existing_roles_remain_usable(
    env: db.L1WEnv, fresh_007: str, apply_migrations: Callable[..., None]
) -> None:
    states = {role: _role_state(env, role) for role in ROLES}  # created by the session's 008
    apply_migrations(fresh_007, "008")
    assert _has_008(fresh_007)
    assert {role: _role_state(env, role) for role in ROLES} == states


@pytest.mark.parametrize("attribute", ATTRIBUTES)
@pytest.mark.parametrize("role", ROLES)
def test_pf02_unsafe_attribute_refused_before_any_mutation(
    env: db.L1WEnv, fresh_007: str, apply_migrations: Callable[..., None], role: str,
    attribute: str,
) -> None:
    safe = _role_state(env, role)
    with psycopg.connect(env.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER ROLE {} " + attribute).format(sql.Identifier(role)))
    try:
        _refused(env, fresh_007, role, apply_migrations)
    finally:
        with psycopg.connect(env.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("ALTER ROLE {} NO" + attribute).format(sql.Identifier(role)))
    assert _role_state(env, role) == safe


@pytest.mark.parametrize("member_kind", ["group-role", "superuser-login"])
@pytest.mark.parametrize("role", ROLES)
def test_pf03_extra_incoming_membership_refused(
    env: db.L1WEnv, fresh_007: str, apply_migrations: Callable[..., None], role: str,
    member_kind: str,
) -> None:
    safe = _role_state(env, role)
    member = f"haloflow_test_l1w_{env.token}_pf03{secrets.token_hex(2)}"
    attributes = "NOLOGIN" if member_kind == "group-role" else "LOGIN SUPERUSER"
    try:
        with psycopg.connect(env.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} " + attributes).format(sql.Identifier(member)))
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role),
                                                          sql.Identifier(member)))
        _refused(env, fresh_007, role, apply_migrations)
    finally:
        _drop_role(env, member)
    assert _role_state(env, role) == safe


def test_pf03_identity_owner_refuses_even_a_plain_login_member(
    env: db.L1WEnv, fresh_007: str, apply_migrations: Callable[..., None]
) -> None:
    role = db.IDENTITY_OWNER
    safe = _role_state(env, role)
    member = f"haloflow_test_l1w_{env.token}_pf03login"
    try:
        with psycopg.connect(env.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(member)))
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role),
                                                          sql.Identifier(member)))
        _refused(env, fresh_007, role, apply_migrations)
    finally:
        _drop_role(env, member)
    assert _role_state(env, role) == safe


def test_pf03_support_accepts_a_plain_login_member(
    env: db.L1WEnv, fresh_007: str, apply_migrations: Callable[..., None]
) -> None:
    """Support logins join haloflow_support out of band by design (v1r1 §4)."""

    env.login("pf03support", db.SUPPORT)
    apply_migrations(fresh_007, "008")
    assert _has_008(fresh_007)


@pytest.mark.parametrize("role", ROLES)
def test_pf04_extra_outgoing_membership_refused(
    env: db.L1WEnv, fresh_007: str, apply_migrations: Callable[..., None], role: str
) -> None:
    safe = _role_state(env, role)
    group = f"haloflow_test_l1w_{env.token}_pf04{secrets.token_hex(2)}"
    try:
        with psycopg.connect(env.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(group)))
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group),
                                                          sql.Identifier(role)))
        _refused(env, fresh_007, role, apply_migrations)
    finally:
        _drop_role(env, group)
    assert _role_state(env, role) == safe


@pytest.mark.parametrize("role", ROLES)
def test_pf05_migrator_edge_with_other_options_refused(
    env: db.L1WEnv, fresh_007: str, apply_migrations: Callable[..., None], role: str
) -> None:
    safe = _role_state(env, role)
    with psycopg.connect(env.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT {} TO haloflow_migrator WITH INHERIT TRUE").format(
            sql.Identifier(role)))
    try:
        _refused(env, fresh_007, role, apply_migrations)
    finally:
        with psycopg.connect(env.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("GRANT {} TO haloflow_migrator WITH INHERIT FALSE").format(
                sql.Identifier(role)))
    assert _role_state(env, role) == safe
