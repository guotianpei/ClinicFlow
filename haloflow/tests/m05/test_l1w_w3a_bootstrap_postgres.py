"""L-1W W3a: HaloVox support bootstraps a clinic's first clinic_admin (owner decision,
2026-11-02; Codex 214930Z option (i), 215519Z replay and guard rules). Contract v1 §1.

t032 m01_staff_bootstrap_admin(p_command jsonb) -> jsonb {"schema_version": 1,
"outcome": <code>[, "replayed": true]}; codes BOOTSTRAPPED, ADMIN_EXISTS,
TARGET_NOT_ACTIVE, SUPPORT_NOT_AUTHORIZED, COMMAND_MALFORMED, ACTION_KEY_CONFLICT,
ACTION_KEY_ACTOR_MISMATCH. EXECUTE to haloflow_support only.

Support logins are synthetic, token-named and exist only in the disposable cluster
(tests/platform/l1w_db_support.py guard). Role rows seeded through the admin
connection are FIXTURE SETUP, labelled; the real path is BS-01.

Rows: BS-01 real path; BS-02 identity/grant refusals; BS-03 admin exists / inactive
target; BS-04 two concurrent bootstraps for different targets serialize on the
tenant-local guard; BS-05 grant revoked while waiting; BS-06 replay (stored outcome,
re-authorized; conflict and actor mismatch); BS-07 privilege inventory; BS-08 guard
taken by every role- or status-changing admin path (inventory against the body).
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import l1w_w3a_support as w3
import m05_support as m05
import psycopg
import pytest
import staff_support as st
from psycopg import errors, sql
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402

pytestmark = pytest.mark.postgres

CAP: Final = "l1w.roles.bootstrap"


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        yield handle


@pytest.fixture(autouse=True)
def _drop_test_grants(env: db.L1WEnv, staff_install: st.StaffInstall) -> Iterator[None]:
    """The staff tenant's reset does not cover support_access_grants (FK): remove the
    grants this module created for it after each row."""

    yield
    with env.connect("admin", autocommit=True) as conn:
        conn.execute("DELETE FROM shared.support_access_grants WHERE tenant_id = %s"
                     " AND actor_id LIKE %s", (staff_install.tenant[0], f"support-{env.token}-%"))


def _setup(install: st.StaffInstall) -> st.StaffInstall:
    w3.ready_with_staff(install)
    for staff_id in (m05.S1, m05.S2, m05.SUP):
        w3.drop_role(install, staff_id, "clinic_admin")  # FIXTURE SETUP: no admin yet
    return install


def _support(env: db.L1WEnv, key: str, *, allowlist: bool = True) -> tuple[str, str]:
    actor = f"support-{env.token}-{key}"
    env.login(key, db.SUPPORT)
    if allowlist:
        env.allowlist_support(key, actor)
    return key, actor


def _grant(env: db.L1WEnv, install: st.StaffInstall, actor: str, **kw: Any) -> tuple[Any, str]:
    return env.support_grant(actor, install.tenant[0], kw.pop("caps", [CAP]), **kw)


def _command(target: str, *, key: str | None = None) -> dict[str, Any]:
    return {"schema_version": 1, "client_action_id": key or str(uuid.uuid4()),
            "staff_id": target}


def _bootstrap_on(conn: psycopg.Connection[Any], install: st.StaffInstall,
                  command: dict[str, Any]) -> dict[str, Any]:
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
    row = conn.execute(
        sql.SQL("SELECT {}.{}(%s)").format(sql.Identifier(install.schema),
                                           sql.Identifier(w3.BOOTSTRAP)),
        (Jsonb(command),),
    ).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def _bootstrap(env: db.L1WEnv, key: str, install: st.StaffInstall,
               command: dict[str, Any]) -> dict[str, Any]:
    with env.connect(key) as conn:
        result = _bootstrap_on(conn, install, command)
        conn.commit()
    return result


def _admins(install: st.StaffInstall) -> list[str]:
    return [s for s, r in w3.roles(install) if r == "clinic_admin"]


def _log(install: st.StaffInstall) -> list[tuple[Any, ...]]:
    return st.admin_rows(install, sql.SQL(
        "SELECT staff_id::text, support_actor_id, grant_id, ticket_reference"
        " FROM {}.m01_staff_bootstrap_log ORDER BY at").format(sql.Identifier(install.schema)))


def test_bs01_real_bootstrap_path(env: db.L1WEnv, staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    key, actor = _support(env, "bs01")
    grant_id, ticket = _grant(env, install, actor)
    result = _bootstrap(env, key, install, _command(m05.S1))
    assert result == {"schema_version": 1, "outcome": "BOOTSTRAPPED"}
    assert _admins(install) == [m05.S1]
    assert _log(install)[-1] == (m05.S1, actor, grant_id, ticket)


@pytest.mark.parametrize("case", ["unlisted", "renamed", "no-grant", "other-tenant",
                                  "no-capability", "expired"])
def test_bs02_identity_and_grant_refusals_write_nothing(
    env: db.L1WEnv, staff_install: st.StaffInstall, case: str
) -> None:
    install = _setup(staff_install)
    key, actor = _support(env, f"bs02{case.replace('-', '')}", allowlist=case != "unlisted")
    if case in ("unlisted", "renamed"):
        _grant(env, install, actor)
    elif case == "other-tenant":
        env.support_grant(actor, env.tenant(), [CAP])
    elif case == "no-capability":
        _grant(env, install, actor, caps=["l1w.identity.admin"])
    elif case == "expired":
        from datetime import timedelta
        _grant(env, install, actor, starts=timedelta(hours=-2), expires=timedelta(seconds=-1))
    if case == "renamed":
        renamed = f"haloflow_test_l1w_{env.token}_bs02renamed2"
        with env.connect("admin", autocommit=True) as conn:
            conn.execute(sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                sql.Identifier(env.logins[key]), sql.Identifier(renamed)))
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(renamed), sql.Literal(db.FIXTURE_PASSWORD)))
        env.logins[key] = renamed
    before = st.full_snapshot(install)
    result = _bootstrap(env, key, install, _command(m05.S1))
    assert result["outcome"] == "SUPPORT_NOT_AUTHORIZED"
    assert st.full_snapshot(install) == before
    assert _admins(install) == []


def test_bs03_admin_exists_and_inactive_target(
    env: db.L1WEnv, staff_install: st.StaffInstall
) -> None:
    install = _setup(staff_install)
    key, actor = _support(env, "bs03")
    _grant(env, install, actor)
    w3.ensure_staff(install, "00000000-0000-4000-8000-0000000c3a01")
    st.admin_op(install, st.admin_command("deactivate", "00000000-0000-4000-8000-0000000c3a01"))
    assert _bootstrap(env, key, install, _command("00000000-0000-4000-8000-0000000c3a01")) == {
        "schema_version": 1, "outcome": "TARGET_NOT_ACTIVE"}
    w3.seed_role(install, m05.SUP, "clinic_admin")  # FIXTURE SETUP: an admin exists
    assert _bootstrap(env, key, install, _command(m05.S1)) == {
        "schema_version": 1, "outcome": "ADMIN_EXISTS"}
    assert _admins(install) == [m05.SUP]


def test_bs04_concurrent_bootstraps_for_different_targets_serialize(
    env: db.L1WEnv, staff_install: st.StaffInstall
) -> None:
    install = _setup(staff_install)
    key_a, actor_a = _support(env, "bs04a")
    key_b, actor_b = _support(env, "bs04b")
    _grant(env, install, actor_a)
    _grant(env, install, actor_b)
    with env.connect(key_a) as first, env.connect(key_b) as second:
        holder, waiter = db.backend_pid(first), db.backend_pid(second)
        first.commit()
        second.commit()
        assert _bootstrap_on(first, install, _command(m05.S1))["outcome"] == "BOOTSTRAPPED"
        job = db.Background(lambda: (_bootstrap_on(second, install, _command(m05.S2)),
                                     second.commit())[0]).start()
        db.wait_blocked_by(env, waiter, holder)
        first.commit()
        assert job.join() == {"schema_version": 1, "outcome": "ADMIN_EXISTS"}
    assert _admins(install) == [m05.S1]


def test_bs05_grant_revoked_while_waiting_on_the_guard(
    env: db.L1WEnv, staff_install: st.StaffInstall
) -> None:
    install = _setup(staff_install)
    key_a, actor_a = _support(env, "bs05a")
    key_b, actor_b = _support(env, "bs05b")
    _grant(env, install, actor_a)
    grant_b, _ = _grant(env, install, actor_b)
    with env.connect(key_a) as first, env.connect(key_b) as second:
        holder, waiter = db.backend_pid(first), db.backend_pid(second)
        first.commit()
        second.commit()
        # A holds the guard with a bootstrap it will roll back (no admin results).
        assert _bootstrap_on(first, install, _command(m05.S1))["outcome"] == "BOOTSTRAPPED"
        job = db.Background(lambda: (_bootstrap_on(second, install, _command(m05.S2)),
                                     second.commit())[0]).start()
        db.wait_blocked_by(env, waiter, holder)
        with env.connect("admin", autocommit=True) as gate:
            gate.execute("UPDATE shared.support_access_grants SET state = 'revoked'"
                         " WHERE grant_id = %s", (grant_b,))
        first.rollback()
        assert job.join()["outcome"] == "SUPPORT_NOT_AUTHORIZED"
    assert _admins(install) == []


def test_bs06_replay_returns_the_stored_outcome_after_reauthorization(
    env: db.L1WEnv, staff_install: st.StaffInstall
) -> None:
    install = _setup(staff_install)
    key, actor = _support(env, "bs06")
    grant_id, _ = _grant(env, install, actor)
    command = _command(m05.S1)
    assert _bootstrap(env, key, install, command)["outcome"] == "BOOTSTRAPPED"
    assert _bootstrap(env, key, install, command) == {
        "schema_version": 1, "outcome": "BOOTSTRAPPED", "replayed": True}
    # Changed payload under the same actor and key: the existing conflict semantics,
    # never a second target.
    changed = dict(command, staff_id=m05.S2)
    assert _bootstrap(env, key, install, changed)["outcome"] == "ACTION_KEY_CONFLICT"
    assert _admins(install) == [m05.S1]
    # Another support actor with the same key.
    other_key, other_actor = _support(env, "bs06other")
    _grant(env, install, other_actor)
    assert _bootstrap(env, other_key, install, command)["outcome"] == (
        "ACTION_KEY_ACTOR_MISMATCH")
    # The original grant revoked: an old key cannot bypass re-authorization.
    with env.connect("admin", autocommit=True) as gate:
        gate.execute("UPDATE shared.support_access_grants SET state = 'revoked'"
                     " WHERE grant_id = %s", (grant_id,))
    assert _bootstrap(env, key, install, command)["outcome"] == "SUPPORT_NOT_AUTHORIZED"


def test_bs07_bootstrap_privileges_are_exact(env: db.L1WEnv, staff_install: st.StaffInstall
                                             ) -> None:
    install = _setup(staff_install)
    acl = st.admin_rows(install, sql.SQL(
        "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
        " x.privilege_type FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,"
        " LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x"
        " WHERE n.nspname = %s AND p.proname = %s"), (install.schema, w3.BOOTSTRAP))
    assert sorted(acl) == sorted([(st.STAFF_OWNER, "EXECUTE"), (db.SUPPORT, "EXECUTE")])
    # Support reaches nothing else in the tenant schema: no other function, no table.
    others = st.admin_rows(install, sql.SQL(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND p.proname <> %s"
        " AND has_function_privilege(%s, p.oid, 'EXECUTE')"),
        (install.schema, w3.BOOTSTRAP, db.SUPPORT))
    assert others == []
    tables = st.admin_rows(install, sql.SQL(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p')"
        " AND (has_table_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
        "REFERENCES,TRIGGER') OR has_any_column_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,"
        "REFERENCES'))"), (install.schema, db.SUPPORT, db.SUPPORT))
    assert tables == []
    assert st.admin_rows(install, "SELECT has_schema_privilege(%s, %s, 'USAGE'),"
                         " has_schema_privilege(%s, %s, 'CREATE')",
                         (db.SUPPORT, install.schema, db.SUPPORT, install.schema)) == [
        (True, False)]
    # Runtime cannot call the bootstrap gateway.
    with pytest.raises(errors.InsufficientPrivilege):
        st.call(install, w3.BOOTSTRAP, _command(m05.S1))


def _guard_holder(install: st.StaffInstall) -> psycopg.Connection[Any]:
    """FIXTURE SETUP: an admin connection holding the tenant-local guard row."""

    conn = psycopg.connect(install.ids.admin)
    row = conn.execute(sql.SQL("SELECT id FROM {}.m01_staff_role_guard FOR UPDATE").format(
        sql.Identifier(install.schema))).fetchone()
    assert row == (True,)
    return conn


@pytest.mark.parametrize(
    ("operation", "takes_guard"),
    [("grant_role", True), ("revoke_role", True), ("deactivate", True), ("activate", True),
     ("rename", False), ("grant", False)],
)
def test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths(
    staff_install: st.StaffInstall, operation: str, takes_guard: bool
) -> None:
    """Dynamic oracle (Codex 221349Z): with the guard row held elsewhere, every
    role- or status-changing t029 branch BLOCKS on it (blocker proven); the other
    branches complete without waiting."""

    install = _setup(staff_install)
    w3.seed_role(install, st.ADMIN, "clinic_admin")  # FIXTURE SETUP
    target = "00000000-0000-4000-8000-0000000c8b01"
    w3.ensure_staff(install, target)
    if operation == "activate":
        st.admin_op(install, st.admin_command("deactivate", target))
    if operation == "revoke_role":
        w3.seed_role(install, target, "supervisor")  # FIXTURE SETUP
    extra = {"role": "supervisor"} if operation in ("grant_role", "revoke_role") else {}
    if operation == "rename":
        extra = {"display_name": "Renamed Staff"}
    if operation == "grant":
        extra = {"access": "read"}
        st.admin_op(install, st.admin_command("revoke", target, access="read"))
    command = st.admin_command(operation, target, extra=extra)
    holder = _guard_holder(install)
    waiter = st.runtime_connection(install)
    try:
        holder_pid = int(holder.execute("SELECT pg_backend_pid()").fetchone()[0])  # type: ignore[index]
        waiter_pid = int(waiter.execute("SELECT pg_backend_pid()").fetchone()[0])  # type: ignore[index]
        waiter.commit()
        st.set_tenant(waiter, install)
        job = db.Background(lambda: st.call_on(waiter, install, w3.ADMIN_V2, command)).start()
        if takes_guard:
            st.wait_until_blocked_by(install, waiter_pid, holder_pid)
            holder.rollback()
            assert job.join()["outcome"] == "applied"
        else:
            assert job.join(timeout_s=10)["outcome"] == "applied"
            holder.rollback()
        waiter.commit()
    finally:
        holder.close()
        waiter.close()


def test_bs08_bootstrap_blocks_on_the_guard(env: db.L1WEnv, staff_install: st.StaffInstall
                                            ) -> None:
    install = _setup(staff_install)
    key, actor = _support(env, "bs08")
    _grant(env, install, actor)
    holder = _guard_holder(install)
    try:
        holder_pid = int(holder.execute("SELECT pg_backend_pid()").fetchone()[0])  # type: ignore[index]
        with env.connect(key) as support:
            waiter_pid = db.backend_pid(support)
            support.commit()
            job = db.Background(lambda: (_bootstrap_on(support, install, _command(m05.S1)),
                                         support.commit())[0]).start()
            st.wait_until_blocked_by(install, waiter_pid, holder_pid)
            holder.rollback()
            assert job.join()["outcome"] == "BOOTSTRAPPED"
    finally:
        holder.close()
