"""L-1W W3b: HaloVox support creates a clinic's first staff member and grants
clinic_admin in one audited step (owner decision OD-FM1, 2026-11-03). Plan v1r1 §2-§3
and addendum 1 (Codex 150630Z, 151751Z).

t033 (migrator DDL) adds operation/role/created_display_name to the bootstrap key and
log tables; t034 ``m01_staff_bootstrap_first_admin`` (staff owner, EXECUTE to
haloflow_support only) writes, on success, exactly: one m01_staff row (active, no
follow-up access), one m01_staff_roles row (clinic_admin, granted_by = support actor),
one bootstrap log row and one bootstrap key row. ADMIN_EXISTS / TARGET_EXISTS write a
key row only (deliberate idempotency records); shape, identity and grant refusals write
nothing. It never writes m01_staff_admin_log or m01_staff_action_keys.

Rows: FM-01 empty clinic; FM-02 non-admin and inactive members present; FM-03 active
admin exists; FM-04 target exists (active / inactive); FM-04b PK race after the guard;
FM-05 identity/grant refusals; FM-05b revoked / renamed while waiting on the guard;
FM-06 replay and conflicts; FM-06b cross-function keys with t032 (directional codes);
FM-06c action-key namespaces independent; FM-06d renewed-grant replay; FM-07
concurrent first members; FM-07b first member vs t032; FM-08 privileges; FM-09 shape
(no access field, closed keys); FM-10 fresh production tenant, no fixture staff.
ST-01..ST-03 storage (t033).
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import l1w_w3b_support as w3b
import m02_support as m02
import psycopg
import pytest
import staff_support as st
from psycopg import errors, sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402

pytestmark = pytest.mark.postgres

A1: Final = "00000000-0000-4000-8000-0000000fa001"
A2: Final = "00000000-0000-4000-8000-0000000fa002"
EXISTING: Final = "00000000-0000-4000-8000-0000000fa0e1"
INACTIVE: Final = "00000000-0000-4000-8000-0000000fa0e2"


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        yield handle


@pytest.fixture
def clinic(
    request: pytest.FixtureRequest,
    m02_ids: m02.Identities,
    m05_reset: Callable[[tuple[str, str]], None],
) -> Iterator[st.StaffInstall]:
    """A fresh tenant per row (staff rows cannot be deleted), installed lazily."""

    tenant = m02.tenant_for(f"w3b-fm:{request.node.nodeid}")
    m05_reset(tenant)
    try:
        yield st.StaffInstall(m02_ids, tenant)
    finally:
        m05_reset(tenant)


def _support(env: db.L1WEnv, key: str, *, allowlist: bool = True) -> tuple[str, str]:
    actor = f"support-{env.token}-{key}"
    env.login(key, db.SUPPORT)
    if allowlist:
        env.allowlist_support(key, actor)
    return key, actor


def _grant(env: db.L1WEnv, install: st.StaffInstall, actor: str, **kw: Any
           ) -> tuple[Any, str]:
    return env.support_grant(actor, install.tenant[0], kw.pop("caps", [w3b.CAP]), **kw)


def _revoke(env: db.L1WEnv, grant_id: Any) -> None:
    with env.connect("admin", autocommit=True) as gate:
        gate.execute("UPDATE shared.support_access_grants SET state = 'revoked'"
                     " WHERE grant_id = %s", (grant_id,))


def _ready(install: st.StaffInstall) -> st.StaffInstall:
    install.require()
    return install


def _first(env: db.L1WEnv, key: str, install: st.StaffInstall, command: dict[str, Any]
           ) -> dict[str, Any]:
    with env.connect(key) as conn:
        result = w3b.call_on(conn, install, w3b.FIRST_ADMIN, command)
        conn.commit()
    return result


def _t032(env: db.L1WEnv, key: str, install: st.StaffInstall, command: dict[str, Any]
          ) -> dict[str, Any]:
    with env.connect(key) as conn:
        result = w3b.call_on(conn, install, w3b.BOOTSTRAP, command)
        conn.commit()
    return result


def _outcome(code: str, *, replayed: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"schema_version": 1, "outcome": code}
    if replayed:
        out["replayed"] = True
    return out


def _admins(install: st.StaffInstall) -> list[str]:
    return sorted(r["staff_id"] for r in w3b.rows(install, "m01_staff_roles")
                  if r["role"] == "clinic_admin")


# --- FM-01 / FM-02: success ---------------------------------------------------------


def test_fm01_empty_clinic_gets_member_and_admin_in_one_step(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    install = _ready(clinic)
    assert w3b.rows(install, "m01_staff") == []
    key, actor = _support(env, "fm01")
    grant_id, ticket = _grant(env, install, actor)
    cmd = w3b.command(A1, "Dr Ada Example")
    assert _first(env, key, install, cmd) == _outcome("BOOTSTRAPPED")

    staff = w3b.rows(install, "m01_staff")
    assert [(r["staff_id"], r["status"], r["display_name"]) for r in staff] == [
        (A1, "active", "Dr Ada Example")]
    assert w3b.rows(install, "m01_staff_followup_access") == []
    roles = w3b.rows(install, "m01_staff_roles")
    assert [(r["staff_id"], r["role"], r["granted_by"]) for r in roles] == [
        (A1, "clinic_admin", actor)]
    log = w3b.rows(install, "m01_staff_bootstrap_log")
    assert len(log) == 1
    assert {k: log[0][k] for k in ("client_action_id", "staff_id", "support_actor_id",
                                   "grant_id", "ticket_reference", "operation", "role",
                                   "created_display_name")} == {
        "client_action_id": cmd["client_action_id"], "staff_id": A1,
        "support_actor_id": actor, "grant_id": str(grant_id), "ticket_reference": ticket,
        "operation": "create_first_admin", "role": "clinic_admin",
        "created_display_name": "Dr Ada Example"}
    keys = w3b.rows(install, "m01_staff_bootstrap_keys")
    assert [(k["client_action_id"], k["operation"], k["actor_id"], k["staff_id"],
             k["outcome"]) for k in keys] == [
        (cmd["client_action_id"], "create_first_admin", actor, A1, "BOOTSTRAPPED")]
    digest = st.admin_rows(install, sql.SQL(
        "SELECT digest FROM {}.m01_staff_bootstrap_keys").format(
        sql.Identifier(install.schema)))
    assert bytes(digest[0][0]) == w3b.expected_digest(
        install.tenant[0], cmd["client_action_id"], actor, A1, "Dr Ada Example")
    # Never the clinic admin's own history tables (plan v1r1 §2, stated limitation).
    assert w3b.rows(install, "m01_staff_admin_log") == []
    assert w3b.rows(install, "m01_staff_action_keys") == []


@pytest.mark.parametrize("present", ["active-member", "inactive-member", "inactive-admin"])
def test_fm02_members_without_an_active_admin_do_not_block(
    env: db.L1WEnv, clinic: st.StaffInstall, present: str
) -> None:
    install = _ready(clinic)
    status = "active" if present == "active-member" else "inactive"
    w3b.seed_member_raw(install, EXISTING, status=status)  # FIXTURE SETUP
    if present == "inactive-admin":
        w3b.seed_role_raw(install, EXISTING, "clinic_admin")  # FIXTURE SETUP
    key, actor = _support(env, f"fm02{present.replace('-', '')}")
    _grant(env, install, actor)
    assert _first(env, key, install, w3b.command(A1)) == _outcome("BOOTSTRAPPED")
    assert A1 in _admins(install)


# --- FM-03 / FM-04: business refusals (key row only) --------------------------------


def test_fm03_active_admin_exists_writes_only_the_key(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    install = _ready(clinic)
    w3b.seed_member_raw(install, EXISTING)  # FIXTURE SETUP
    w3b.seed_role_raw(install, EXISTING, "clinic_admin")  # FIXTURE SETUP: active admin
    key, actor = _support(env, "fm03")
    _grant(env, install, actor)
    before = w3b.snapshot(install)
    cmd = w3b.command(A1)
    assert _first(env, key, install, cmd) == _outcome("ADMIN_EXISTS")
    after = w3b.snapshot(install)
    added = st.added_rows(before["m01_staff_bootstrap_keys"], after["m01_staff_bootstrap_keys"])
    assert [(k["client_action_id"], k["operation"], k["outcome"]) for k in added] == [
        (cmd["client_action_id"], "create_first_admin", "ADMIN_EXISTS")]
    assert {t: v for t, v in after.items() if t != "m01_staff_bootstrap_keys"} == {
        t: v for t, v in before.items() if t != "m01_staff_bootstrap_keys"}


@pytest.mark.parametrize("status", ["active", "inactive"])
def test_fm04_existing_staff_id_is_target_exists(
    env: db.L1WEnv, clinic: st.StaffInstall, status: str
) -> None:
    install = _ready(clinic)
    w3b.seed_member_raw(install, EXISTING, status=status)  # FIXTURE SETUP
    key, actor = _support(env, f"fm04{status}")
    _grant(env, install, actor)
    before = w3b.snapshot(install)
    cmd = w3b.command(EXISTING)
    assert _first(env, key, install, cmd) == _outcome("TARGET_EXISTS")
    after = w3b.snapshot(install)
    added = st.added_rows(before["m01_staff_bootstrap_keys"], after["m01_staff_bootstrap_keys"])
    assert [(k["operation"], k["outcome"]) for k in added] == [
        ("create_first_admin", "TARGET_EXISTS")]
    assert {t: v for t, v in after.items() if t != "m01_staff_bootstrap_keys"} == {
        t: v for t, v in before.items() if t != "m01_staff_bootstrap_keys"}


def test_fm04b_staff_row_inserted_while_waiting_is_target_exists_no_partial_rows(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    """The guard does not cover a raw m01_staff insert. An admin connection inserts the
    same staff_id WITHOUT taking the guard and keeps it uncommitted: the call passes the
    guard and its existence check (the row is not yet visible), then its own insert
    waits on the holder's uncommitted key. The holder commits; the call must catch the
    unique violation and return TARGET_EXISTS with no role/log row (savepoint), only its
    key row."""

    install = _ready(clinic)
    key, actor = _support(env, "fm04b")
    _grant(env, install, actor)
    holder = psycopg.connect(install.ids.admin)
    try:
        holder.execute(sql.SQL(  # FIXTURE SETUP: the racing member
            "INSERT INTO {}.m01_staff (staff_id, status, status_changed_at, display_name)"
            " VALUES (%s, 'active', clock_timestamp(), 'Racer')").format(
            sql.Identifier(install.schema)), (A1,))
        holder_pid = db.backend_pid(holder)
        with env.connect(key) as support:
            waiter_pid = db.backend_pid(support)
            support.commit()
            job = db.Background(lambda: (w3b.call_on(support, install, w3b.FIRST_ADMIN,
                                                     w3b.command(A1)), support.commit())[0]
                                ).start()
            st.wait_until_blocked_by(install, waiter_pid, holder_pid)
            holder.commit()
            assert job.join() == _outcome("TARGET_EXISTS")
    finally:
        holder.close()
    assert [r["display_name"] for r in w3b.rows(install, "m01_staff")] == ["Racer"]
    assert w3b.rows(install, "m01_staff_roles") == []
    assert w3b.rows(install, "m01_staff_bootstrap_log") == []
    assert [k["outcome"] for k in w3b.rows(install, "m01_staff_bootstrap_keys")] == [
        "TARGET_EXISTS"]


# --- FM-05: identity and grant refusals write nothing --------------------------------


@pytest.mark.parametrize("case", ["unlisted", "renamed", "no-grant", "other-tenant",
                                  "no-capability", "expired"])
def test_fm05_identity_and_grant_refusals_write_nothing(
    env: db.L1WEnv, clinic: st.StaffInstall, case: str
) -> None:
    install = _ready(clinic)
    key, actor = _support(env, f"fm05{case.replace('-', '')}", allowlist=case != "unlisted")
    if case in ("unlisted", "renamed"):
        _grant(env, install, actor)
    elif case == "other-tenant":
        env.support_grant(actor, env.tenant(), [w3b.CAP])
    elif case == "no-capability":
        _grant(env, install, actor, caps=["l1w.identity.admin"])
    elif case == "expired":
        _grant(env, install, actor, starts=timedelta(hours=-2), expires=timedelta(seconds=-1))
    if case == "renamed":
        renamed = f"haloflow_test_l1w_{env.token}_fm05renamed2"
        with env.connect("admin", autocommit=True) as conn:
            conn.execute(sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                sql.Identifier(env.logins[key]), sql.Identifier(renamed)))
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(renamed), sql.Literal(db.FIXTURE_PASSWORD)))
        env.logins[key] = renamed
    before = w3b.snapshot(install)
    assert _first(env, key, install, w3b.command(A1)) == _outcome("SUPPORT_NOT_AUTHORIZED")
    assert w3b.snapshot(install) == before


@pytest.mark.parametrize("change", ["revoked", "renamed"])
def test_fm05b_authorization_lost_while_waiting_on_the_guard(
    env: db.L1WEnv, clinic: st.StaffInstall, change: str
) -> None:
    install = _ready(clinic)
    key, actor = _support(env, f"fm05b{change}")
    grant_id, _ = _grant(env, install, actor)
    before = w3b.snapshot(install)
    holder = psycopg.connect(install.ids.admin)
    try:
        holder.execute(sql.SQL("SELECT id FROM {}.m01_staff_role_guard FOR UPDATE").format(
            sql.Identifier(install.schema)))
        holder_pid = db.backend_pid(holder)
        with env.connect(key) as support:
            waiter_pid = db.backend_pid(support)
            support.commit()
            job = db.Background(lambda: (w3b.call_on(support, install, w3b.FIRST_ADMIN,
                                                     w3b.command(A1)), support.commit())[0]
                                ).start()
            st.wait_until_blocked_by(install, waiter_pid, holder_pid)
            if change == "revoked":
                _revoke(env, grant_id)
            else:
                with env.connect("admin", autocommit=True) as conn:
                    conn.execute(
                        "UPDATE shared.support_operator_logins SET login_name = %s"
                        " WHERE actor_id = %s", (f"{env.logins[key]}_x", actor))
            holder.rollback()
            assert job.join() == _outcome("SUPPORT_NOT_AUTHORIZED")
    finally:
        holder.close()
    assert w3b.snapshot(install) == before


# --- FM-06: replay and key conflicts --------------------------------------------------


def test_fm06_replay_conflict_and_actor_mismatch(env: db.L1WEnv, clinic: st.StaffInstall
                                                 ) -> None:
    install = _ready(clinic)
    key, actor = _support(env, "fm06")
    grant_id, _ = _grant(env, install, actor)
    cmd = w3b.command(A1)
    assert _first(env, key, install, cmd) == _outcome("BOOTSTRAPPED")
    stored = w3b.snapshot(install)
    assert _first(env, key, install, cmd) == _outcome("BOOTSTRAPPED", replayed=True)
    assert w3b.snapshot(install) == stored
    for changed in (dict(cmd, staff_id=A2), dict(cmd, display_name="Someone Else")):
        assert _first(env, key, install, changed) == _outcome("ACTION_KEY_CONFLICT")
        assert w3b.snapshot(install) == stored
    other_key, other_actor = _support(env, "fm06other")
    _grant(env, install, other_actor)
    assert _first(env, other_key, install, cmd) == _outcome("ACTION_KEY_ACTOR_MISMATCH")
    assert w3b.snapshot(install) == stored
    # Revoked with no replacement: the key cannot bypass current authorization.
    _revoke(env, grant_id)
    assert _first(env, key, install, cmd) == _outcome("SUPPORT_NOT_AUTHORIZED")
    assert w3b.snapshot(install) == stored


def test_fm06b_bootstrap_keys_are_shared_with_t032_directional_codes(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    """Pin 2: t034 then t032 with the same key -> t032's unchanged ACTION_KEY_CONFLICT;
    t032 then t034 -> t034's ACTION_KEY_CONFLICT. Nothing written by the refused call."""

    install = _ready(clinic)
    key, actor = _support(env, "fm06b")
    _grant(env, install, actor)
    shared_key = str(uuid.uuid4())
    assert _first(env, key, install, w3b.command(A1, key=shared_key)) == _outcome(
        "BOOTSTRAPPED")
    stored = w3b.snapshot(install)
    t032_cmd = {"schema_version": 1, "client_action_id": shared_key, "staff_id": A1}
    assert _t032(env, key, install, t032_cmd) == _outcome("ACTION_KEY_CONFLICT")
    assert w3b.snapshot(install) == stored
    # Reverse direction on a second clinic state: t032 first (existing member, no admin).
    w3b.seed_member_raw(install, EXISTING)  # FIXTURE SETUP
    second_key = str(uuid.uuid4())
    with env.connect("admin", autocommit=True) as conn:  # FIXTURE SETUP: no active admin
        conn.execute(sql.SQL("DELETE FROM {}.m01_staff_roles").format(
            sql.Identifier(install.schema)))
    assert _t032(env, key, install, {"schema_version": 1, "client_action_id": second_key,
                                     "staff_id": EXISTING}) == _outcome("BOOTSTRAPPED")
    stored = w3b.snapshot(install)
    assert _first(env, key, install, w3b.command(A2, key=second_key)) == _outcome(
        "ACTION_KEY_CONFLICT")
    assert w3b.snapshot(install) == stored


def test_fm06c_staff_action_keys_are_an_independent_namespace(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    """The same uuid used in m01_staff_action_keys (t029) and the bootstrap keys is
    independent in both orders."""

    install = _ready(clinic)
    key, actor = _support(env, "fm06c")
    _grant(env, install, actor)
    shared_key = str(uuid.uuid4())
    assert _first(env, key, install, w3b.command(A1, key=shared_key)) == _outcome(
        "BOOTSTRAPPED")
    # The new admin uses the same uuid for a t029 add.
    added = st.call(install, "m01_staff_admin_v2", st.admin_command(
        "add", A2, key=shared_key, actor=A1, display_name="Second Hire"))
    assert added["outcome"] == "applied"
    assert {r["client_action_id"] for r in w3b.rows(install, "m01_staff_action_keys")} == {
        shared_key}
    assert {r["client_action_id"] for r in w3b.rows(install, "m01_staff_bootstrap_keys")} == {
        shared_key}


def test_fm06d_replay_under_a_renewed_grant_returns_the_original_outcome(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    """Pin 1: grant_id/ticket are not digest-bound. After the original grant is revoked
    and a new capable grant issued, the same command replays with no write, and the
    log keeps the ORIGINAL grant and ticket."""

    install = _ready(clinic)
    key, actor = _support(env, "fm06d")
    grant_id, ticket = _grant(env, install, actor)
    cmd = w3b.command(A1)
    assert _first(env, key, install, cmd) == _outcome("BOOTSTRAPPED")
    stored = w3b.snapshot(install)
    _revoke(env, grant_id)
    _grant(env, install, actor)  # renewed, different grant_id and ticket
    assert _first(env, key, install, cmd) == _outcome("BOOTSTRAPPED", replayed=True)
    assert w3b.snapshot(install) == stored
    log = w3b.rows(install, "m01_staff_bootstrap_log")
    assert [(r["grant_id"], r["ticket_reference"]) for r in log] == [(str(grant_id), ticket)]


# --- FM-07: concurrency -----------------------------------------------------------------


def test_fm07_concurrent_first_members_one_wins(env: db.L1WEnv, clinic: st.StaffInstall
                                                ) -> None:
    install = _ready(clinic)
    key_a, actor_a = _support(env, "fm07a")
    key_b, actor_b = _support(env, "fm07b")
    _grant(env, install, actor_a)
    _grant(env, install, actor_b)
    with env.connect(key_a) as first, env.connect(key_b) as second:
        holder, waiter = db.backend_pid(first), db.backend_pid(second)
        first.commit()
        second.commit()
        assert w3b.call_on(first, install, w3b.FIRST_ADMIN, w3b.command(A1)) == _outcome(
            "BOOTSTRAPPED")
        job = db.Background(lambda: (w3b.call_on(second, install, w3b.FIRST_ADMIN,
                                                 w3b.command(A2)), second.commit())[0]
                            ).start()
        db.wait_blocked_by(env, waiter, holder)
        first.commit()
        assert job.join() == _outcome("ADMIN_EXISTS")
    assert _admins(install) == [A1]
    assert [r["staff_id"] for r in w3b.rows(install, "m01_staff")] == [A1]


def test_fm07b_first_member_and_t032_serialize_on_the_guard(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    install = _ready(clinic)
    w3b.seed_member_raw(install, EXISTING)  # FIXTURE SETUP: a member, no admin
    key_a, actor_a = _support(env, "fm07ba")
    key_b, actor_b = _support(env, "fm07bb")
    _grant(env, install, actor_a)
    _grant(env, install, actor_b)
    with env.connect(key_a) as first, env.connect(key_b) as second:
        holder, waiter = db.backend_pid(first), db.backend_pid(second)
        first.commit()
        second.commit()
        assert w3b.call_on(first, install, w3b.FIRST_ADMIN, w3b.command(A1)) == _outcome(
            "BOOTSTRAPPED")
        t032_cmd = {"schema_version": 1, "client_action_id": str(uuid.uuid4()),
                    "staff_id": EXISTING}
        job = db.Background(lambda: (w3b.call_on(second, install, w3b.BOOTSTRAP, t032_cmd),
                                     second.commit())[0]).start()
        db.wait_blocked_by(env, waiter, holder)
        first.commit()
        assert job.join() == _outcome("ADMIN_EXISTS")
    assert _admins(install) == [A1]


# --- FM-08 / FM-09: privileges and shape ---------------------------------------------


def test_fm08_first_member_privileges_are_exact(env: db.L1WEnv, clinic: st.StaffInstall
                                                ) -> None:
    install = _ready(clinic)
    acl = st.admin_rows(install, sql.SQL(
        "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
        " x.privilege_type FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,"
        " LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x"
        " WHERE n.nspname = %s AND p.proname = %s"), (install.schema, w3b.FIRST_ADMIN))
    assert sorted(acl) == sorted([(st.STAFF_OWNER, "EXECUTE"), (db.SUPPORT, "EXECUTE")])
    executable = st.admin_rows(install, sql.SQL(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND has_function_privilege(%s, p.oid, 'EXECUTE')"
        " ORDER BY 1"), (install.schema, db.SUPPORT))
    assert executable == [(w3b.BOOTSTRAP,), (w3b.FIRST_ADMIN,)]
    tables = st.admin_rows(install, sql.SQL(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p')"
        " AND (has_table_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
        "REFERENCES,TRIGGER') OR has_any_column_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,"
        "REFERENCES'))"), (install.schema, db.SUPPORT, db.SUPPORT))
    assert tables == []
    with pytest.raises(errors.InsufficientPrivilege):
        st.call(install, w3b.FIRST_ADMIN, w3b.command(A1))


@pytest.mark.parametrize("case", ["access-field", "extra-key", "missing-name", "blank-name",
                                  "long-name", "bad-uuid", "schema-version"])
def test_fm09_closed_shape_refusals_write_nothing(
    env: db.L1WEnv, clinic: st.StaffInstall, case: str
) -> None:
    install = _ready(clinic)
    key, actor = _support(env, f"fm09{case.replace('-', '')}")
    _grant(env, install, actor)
    cmd = w3b.command(A1)
    if case == "access-field":
        cmd["access"] = "read"
    elif case == "extra-key":
        cmd["role"] = "clinic_admin"
    elif case == "missing-name":
        del cmd["display_name"]
    elif case == "blank-name":
        cmd["display_name"] = "   "
    elif case == "long-name":
        cmd["display_name"] = "x" * 81
    elif case == "bad-uuid":
        cmd["staff_id"] = "not-a-uuid"
    else:
        cmd["schema_version"] = 2
    before = w3b.snapshot(install)
    assert _first(env, key, install, cmd) == _outcome("COMMAND_MALFORMED")
    assert w3b.snapshot(install) == before


# --- FM-10: fresh production tenant, no fixture staff ---------------------------------


def test_fm10_fresh_production_tenant_onboards_through_production_units_only(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    """Production provisioning to 36; no m01_staff row created by any fixture (fixtures
    only for the support login and grant). The created admin then adds a second member
    and grants it follow-up access through t029, and (existing t029 behaviour) may grant
    follow-up access to themselves; self clinic_admin changes stay refused."""

    registry = m02.production_registry()
    assert registry.target_version == 36
    m02.provision_sync(clinic.ids, registry, clinic.tenant, supported=range(36, 37))
    install = clinic
    install._attempted = True  # provisioned above through the production registry
    assert w3b.rows(install, "m01_staff") == []
    key, actor = _support(env, "fm10")
    _grant(env, install, actor)
    assert _first(env, key, install, w3b.command(A1, "Clinic Lead")) == _outcome(
        "BOOTSTRAPPED")
    admin = "m01_staff_admin_v2"
    assert st.call(install, admin, st.admin_command(
        "add", A2, actor=A1, display_name="Front Desk"))["outcome"] == "applied"
    assert st.call(install, admin, st.admin_command(
        "grant", A2, actor=A1, access="read"))["outcome"] == "applied"
    assert st.call(install, admin, st.admin_command(
        "grant", A1, actor=A1, access="read"))["outcome"] == "applied"
    # Existing t029 contract (Codex 154906Z): a self clinic_admin change raises P0001
    # NOT_AUTHORIZED and changes nothing.
    before = w3b.snapshot(install)
    st.expect_code("NOT_AUTHORIZED", lambda: st.call(install, admin, st.admin_command(
        "revoke_role", A1, actor=A1, extra={"role": "clinic_admin"})))
    assert w3b.snapshot(install) == before
    assert _admins(install) == [A1]


# --- ST: storage (t033) ---------------------------------------------------------------


def _columns(install: st.StaffInstall, table: str) -> list[tuple[Any, ...]]:
    return st.admin_rows(install, sql.SQL(
        "SELECT column_name, data_type, character_maximum_length, is_nullable,"
        " column_default FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"),
        (install.schema, table))


def test_st01_t033_columns_and_defaults(clinic: st.StaffInstall) -> None:
    install = _ready(clinic)
    keys = {c[0]: c[1:] for c in _columns(install, "m01_staff_bootstrap_keys")}
    assert keys["operation"][:3] == ("character varying", 24, "NO")
    assert "'bootstrap_admin'" in str(keys["operation"][3])
    log = {c[0]: c[1:] for c in _columns(install, "m01_staff_bootstrap_log")}
    assert log["operation"][:3] == ("character varying", 24, "NO")
    assert log["role"][:3] == ("character varying", 16, "NO")
    assert "'clinic_admin'" in str(log["role"][3])
    assert log["created_display_name"][:3] == ("character varying", 80, "YES")


def test_st02_t032_rows_after_t033_record_bootstrap_admin(
    env: db.L1WEnv, clinic: st.StaffInstall
) -> None:
    install = _ready(clinic)
    w3b.seed_member_raw(install, EXISTING)  # FIXTURE SETUP
    key, actor = _support(env, "st02")
    _grant(env, install, actor)
    assert _t032(env, key, install, {"schema_version": 1, "client_action_id":
                                     str(uuid.uuid4()), "staff_id": EXISTING}) == _outcome(
        "BOOTSTRAPPED")
    log = w3b.rows(install, "m01_staff_bootstrap_log")
    assert [(r["operation"], r["role"], r["created_display_name"]) for r in log] == [
        ("bootstrap_admin", "clinic_admin", None)]
    assert [k["operation"] for k in w3b.rows(install, "m01_staff_bootstrap_keys")] == [
        "bootstrap_admin"]


@pytest.mark.parametrize(("operation", "name", "ok"), [
    ("create_first_admin", None, False),
    ("bootstrap_admin", "Has A Name", False),
    ("create_first_admin", "Has A Name", True),
    ("bootstrap_admin", None, True),
])
def test_st03_display_name_present_iff_create(
    clinic: st.StaffInstall, operation: str, name: str | None, ok: bool
) -> None:
    install = _ready(clinic)
    key = str(uuid.uuid4())
    statements: Sequence[Any] = (
        sql.SQL("INSERT INTO {}.m01_staff_bootstrap_keys (client_action_id, digest, actor_id,"
                " staff_id, outcome, created_at, operation) VALUES (%s, %s, 'a', %s,"
                " 'BOOTSTRAPPED', clock_timestamp(), %s)").format(
            sql.Identifier(install.schema)),
        sql.SQL("INSERT INTO {}.m01_staff_bootstrap_log (log_id, client_action_id, staff_id,"
                " support_actor_id, grant_id, ticket_reference, at, operation,"
                " created_display_name) VALUES (%s, %s, %s, 'a', %s, 't',"
                " clock_timestamp(), %s, %s)").format(sql.Identifier(install.schema)),
    )
    with psycopg.connect(install.ids.admin) as conn:  # FIXTURE: constraint probe only
        conn.execute(statements[0], (key, b"\0" * 32, A1, operation))
        try:
            conn.execute(statements[1], (str(uuid.uuid4()), key, A1, str(uuid.uuid4()),
                                         operation, name))
        except errors.CheckViolation:
            assert not ok
        else:
            assert ok
        conn.rollback()
