"""L-1W W3a: lock order, tenant-derived identity writes and exact grants.

Contract v1 §2-§4 (Codex 213713Z lock pins, 214312Z, 215519Z). PostgreSQL plus one
static row. Blocks are proven with pg_blocking_pids before release; a timeout is a
failure, never a pass.

Rows: L-01/L-02 reciprocal clinic_admin revoke, both id orders, serialize on the guard;
L-03 M05 assign vs deactivate of the assignee; L-04 self-claim (actor = target);
L-05 static order over the v2 bodies; L-06 bootstrap waits on an admin role change;
B-05 cross-tenant / self bind refused; A-06 + SP-07c writes land on this schema's
tenant only; SP-07b admin_v2 takes no tenant; PIN2-01 M05 owner effective privileges;
PRIV-W3-01 alembic 009 column grants exact.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any, Final

import l1w_w3a_support as w3
import m05_support as m05
import psycopg
import pytest
import staff_support as st
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402

pytestmark = pytest.mark.postgres

A: Final = "00000000-0000-4000-8000-0000000c1a01"  # A < B in uuid order
B: Final = "00000000-0000-4000-8000-0000000c1b02"


def _setup(install: st.StaffInstall) -> st.StaffInstall:
    w3.ready_with_staff(install)
    for staff_id in (A, B, st.ADMIN):
        w3.ensure_staff(install, staff_id)
    for staff_id in (A, B, st.ADMIN, m05.SUP, m05.S1, m05.S2):
        for role in ("supervisor", "clinic_admin"):
            w3.drop_role(install, staff_id, role)  # FIXTURE SETUP
    return install


def _held(install: st.StaffInstall) -> psycopg.Connection[Any]:
    conn = st.runtime_connection(install)
    st.set_tenant(conn, install)
    return conn


def _pid(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT pg_backend_pid()").fetchone()
    assert row is not None
    return int(row[0])


def _role_log(install: st.StaffInstall) -> list[tuple[Any, ...]]:
    return st.admin_rows(install, sql.SQL(
        "SELECT operation FROM {}.m01_staff_admin_log WHERE operation IN"
        " ('grant_role', 'revoke_role') ORDER BY 1").format(sql.Identifier(install.schema)))


@pytest.mark.parametrize(("first", "second"), [(A, B), (B, A)], ids=["a-first", "b-first"])
def test_l01_l02_reciprocal_admin_revoke_serializes_on_the_guard(
    staff_install: st.StaffInstall, first: str, second: str
) -> None:
    install = _setup(staff_install)
    for staff_id in (A, B):
        w3.seed_role(install, staff_id, "clinic_admin")  # FIXTURE SETUP
    log_before = _role_log(install)
    holder = _held(install)
    waiter = _held(install)
    try:
        holder_pid, waiter_pid = _pid(holder), _pid(waiter)
        result = st.call_on(holder, install, w3.ADMIN_V2,
                            w3.role_command("revoke_role", second, "clinic_admin", actor=first))
        assert result["outcome"] == "applied"
        job = db.Background(lambda: st.call_on(
            waiter, install, w3.ADMIN_V2,
            w3.role_command("revoke_role", first, "clinic_admin", actor=second))).start()
        st.wait_until_blocked_by(install, waiter_pid, holder_pid)
        holder.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="NOT_AUTHORIZED"):
            job.join()
        waiter.rollback()
    finally:
        holder.close()
        waiter.close()
    assert ("clinic_admin" in {r for s, r in w3.roles(install) if s == first})
    assert (second, "clinic_admin") not in w3.roles(install)
    assert len(_role_log(install)) == len(log_before) + 1


def test_l03_m05_assign_and_deactivate_of_the_assignee_serialize(
    staff_install: st.StaffInstall,
) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, m05.SUP, "supervisor")  # FIXTURE SETUP
    w3.seed_role(install, st.ADMIN, "clinic_admin")  # FIXTURE SETUP
    item = w3.item(install)
    holder = _held(install)
    waiter = _held(install)
    try:
        holder_pid, waiter_pid = _pid(holder), _pid(waiter)
        assigned = st.call_on(holder, install, w3.APPLY_V2,
                              w3.action(item, 1, "assign", actor=m05.SUP, assignee=m05.S2))
        assert assigned["outcome"] == "applied"
        job = db.Background(lambda: st.call_on(
            waiter, install, w3.ADMIN_V2, st.admin_command("deactivate", m05.S2))).start()
        st.wait_until_blocked_by(install, waiter_pid, holder_pid)
        holder.commit()
        assert job.join()["outcome"] == "applied"
        waiter.commit()
    finally:
        holder.close()
        waiter.close()
    status = st.admin_rows(install, sql.SQL(
        "SELECT status FROM {}.m01_staff WHERE staff_id = %s").format(
        sql.Identifier(install.schema)), (m05.S2,))
    assert status == [("inactive",)]


def test_l04_self_claim_takes_the_actor_row_once(staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    item = w3.item(install)
    claimed = w3.apply_v2(install, w3.action(item, 1, "claim", actor=m05.S1, supervise=False))
    assert claimed["outcome"] == "applied"


def test_l05_v2_bodies_take_every_membership_lock_before_the_item(
    staff_install: st.StaffInstall,
) -> None:
    install = _setup(staff_install)
    bodies = dict(st.admin_rows(install, sql.SQL(
        "SELECT p.proname, p.prosrc FROM pg_proc p JOIN pg_namespace n"
        " ON n.oid = p.pronamespace WHERE n.nspname = %s AND p.proname = ANY(%s)"),
        (install.schema, [w3.PREPARE_V2, w3.APPLY_V2])))
    assert set(bodies) == {w3.PREPARE_V2, w3.APPLY_V2}
    for name, body in bodies.items():
        item_lock = body.find("m05_followup_items")
        calls = [i for i in range(len(body))
                 if body.startswith("m01_staff_member_standing(", i)]
        assert calls, name
        assert item_lock != -1 and all(i < item_lock for i in calls), name
        assert "m01_staff_assignable(" not in body, name


def test_l06_bootstrap_waits_on_an_admin_role_change_then_sees_the_admin(
    staff_install: st.StaffInstall, migrated_database: str
) -> None:
    install = _setup(staff_install)
    for staff_id in (A, B):
        w3.seed_role(install, staff_id, "clinic_admin")  # FIXTURE SETUP
    with db.l1w_environment(migrated_database) as env:
        actor = f"support-{env.token}-l06"
        env.login("l06", db.SUPPORT)
        env.allowlist_support("l06", actor)
        env.support_grant(actor, install.tenant[0], ["l1w.roles.bootstrap"])
        holder = _held(install)
        try:
            holder_pid = _pid(holder)
            st.call_on(holder, install, w3.ADMIN_V2,
                       w3.role_command("revoke_role", B, "clinic_admin", actor=A))
            with env.connect("l06") as support:
                waiter_pid = db.backend_pid(support)
                support.commit()

                def bootstrap() -> Any:
                    support.execute("SELECT set_config('app.tenant_id', %s, true)",
                                    (install.tenant[0],))
                    row = support.execute(sql.SQL("SELECT {}.{}(%s)").format(
                        sql.Identifier(install.schema), sql.Identifier(w3.BOOTSTRAP)),
                        (psycopg.types.json.Jsonb({"schema_version": 1,
                                                   "client_action_id": str(uuid.uuid4()),
                                                   "staff_id": m05.S1}),)).fetchone()
                    support.commit()
                    return row[0] if row else None

                job = db.Background(bootstrap).start()
                st.wait_until_blocked_by(install, waiter_pid, holder_pid)
                holder.commit()
                assert job.join() == {"schema_version": 1, "outcome": "ADMIN_EXISTS"}
        finally:
            holder.close()
            with env.connect("admin", autocommit=True) as conn:
                conn.execute("DELETE FROM shared.support_access_grants WHERE tenant_id = %s"
                             " AND actor_id = %s", (install.tenant[0], actor))
    assert [s for s, r in w3.roles(install) if r == "clinic_admin"] == [A]


@pytest.mark.parametrize("change", ["deactivate", "activate"])
def test_l06_bootstrap_waits_on_a_status_change_of_an_admin(
    staff_install: st.StaffInstall, migrated_database: str, change: str
) -> None:
    """Status changes move the active-admin predicate, so they take the guard too.
    A (admin) deactivates or activates B (also a role holder) while a support
    bootstrap waits; afterwards A is still an active admin -> ADMIN_EXISTS.

    The reverse ordering (bootstrap holding the guard while a status change waits)
    needs an ACTIVE admin actor to issue the v2 change, which contradicts the
    bootstrap precondition (no active admin); through v1, which takes no guard, it is
    the explicit W3b hold (v1 bypass)."""

    install = _setup(staff_install)
    w3.normalize_staff(install, B)  # FIXTURE SETUP: the other parameter may leave B inactive
    for staff_id in (A, B):
        w3.seed_role(install, staff_id, "clinic_admin")  # FIXTURE SETUP
    if change == "activate":
        st.admin_op(install, st.admin_command("deactivate", B))
    with db.l1w_environment(migrated_database) as env:
        actor = f"support-{env.token}-l06{change}"
        env.login(f"l06{change}", db.SUPPORT)
        env.allowlist_support(f"l06{change}", actor)
        env.support_grant(actor, install.tenant[0], ["l1w.roles.bootstrap"])
        holder = _held(install)
        try:
            holder_pid = _pid(holder)
            st.call_on(holder, install, w3.ADMIN_V2, st.admin_command(change, B, actor=A))
            with env.connect(f"l06{change}") as support:
                waiter_pid = db.backend_pid(support)
                support.commit()

                def bootstrap() -> Any:
                    support.execute("SELECT set_config('app.tenant_id', %s, true)",
                                    (install.tenant[0],))
                    row = support.execute(sql.SQL("SELECT {}.{}(%s)").format(
                        sql.Identifier(install.schema), sql.Identifier(w3.BOOTSTRAP)),
                        (psycopg.types.json.Jsonb({"schema_version": 1,
                                                   "client_action_id": str(uuid.uuid4()),
                                                   "staff_id": m05.S1}),)).fetchone()
                    support.commit()
                    return row[0] if row else None

                job = db.Background(bootstrap).start()
                st.wait_until_blocked_by(install, waiter_pid, holder_pid)
                holder.commit()
                assert job.join() == {"schema_version": 1, "outcome": "ADMIN_EXISTS"}
        finally:
            holder.close()
            with env.connect("admin", autocommit=True) as conn:
                conn.execute("DELETE FROM shared.support_access_grants WHERE tenant_id = %s"
                             " AND actor_id = %s", (install.tenant[0], actor))


# ---- identity writes go only to this schema's tenant --------------------------------


def _bind(install: st.StaffInstall, target: str, ident: tuple[str, str], *,
          actor: str = st.ADMIN) -> Any:
    return w3.admin_v2(install, st.admin_command(
        "bind_identity", target, actor=actor,
        extra={"issuer": ident[0], "subject": ident[1]}))


def test_b05_self_bind_and_foreign_target_are_refused(
    staff_install: st.StaffInstall, staff_foreign_install: st.StaffInstall
) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, st.ADMIN, "clinic_admin")  # FIXTURE SETUP
    ident = ("https://issuer.l1w-test.invalid/w3", f"sub-w3-{uuid.uuid4().hex[:8]}")
    st.expect_code("NOT_AUTHORIZED", lambda: _bind(install, st.ADMIN, ident))
    foreign_staff = "00000000-0000-4000-8000-0000000cf001"  # exists in no member row here
    st.expect_code("STAFF_NOT_FOUND", lambda: _bind(install, foreign_staff, ident))
    assert st.admin_rows(install, "SELECT count(*) FROM shared.staff_identity_bindings"
                         " WHERE subject = %s", (ident[1],)) == [(0,)]


def test_a06_sp07c_writes_land_on_this_schemas_tenant_only(
    staff_install: st.StaffInstall, staff_foreign_install: st.StaffInstall
) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, st.ADMIN, "clinic_admin")  # FIXTURE SETUP
    foreign = staff_foreign_install.tenant[0]
    before_foreign = st.admin_rows(
        install, "SELECT * FROM shared.tenant_auth_settings WHERE tenant_id = %s", (foreign,))
    result = w3.admin_v2(install, st.admin_command(
        "set_auth_setting", st.ADMIN, extra={"setting": "idle_timeout_minutes", "value": "20"}))
    assert result["outcome"] == "applied"
    assert st.admin_rows(install, "SELECT idle_timeout_minutes FROM shared.tenant_auth_settings"
                         " WHERE tenant_id = %s", (install.tenant[0],)) == [(20,)]
    assert st.admin_rows(install, "SELECT * FROM shared.tenant_auth_settings"
                         " WHERE tenant_id = %s", (foreign,)) == before_foreign
    ident = ("https://issuer.l1w-test.invalid/w3", f"sub-w3-{uuid.uuid4().hex[:8]}")
    assert _bind(install, m05.S1, ident)["outcome"] == "applied"
    assert st.admin_rows(install, "SELECT tenant_id FROM shared.staff_identity_bindings"
                         " WHERE subject = %s", (ident[1],)) == [(install.tenant[0],)]


def test_sp07b_admin_v2_takes_no_tenant_argument(staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    args = st.admin_rows(install, sql.SQL(
        "SELECT pg_get_function_identity_arguments(p.oid) FROM pg_proc p JOIN pg_namespace n"
        " ON n.oid = p.pronamespace WHERE n.nspname = %s AND p.proname = %s"),
        (install.schema, w3.ADMIN_V2))
    assert args == [("p_command jsonb",)]


# ---- exact privileges --------------------------------------------------------------


def test_pin201_m05_owner_reads_staff_standing_only_through_two_gateways(
    staff_install: st.StaffInstall,
) -> None:
    install = _setup(staff_install)
    executable = st.admin_rows(install, sql.SQL(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND pg_get_userbyid(p.proowner) = %s"
        " AND has_function_privilege(%s, p.oid, 'EXECUTE') ORDER BY 1"),
        (install.schema, st.STAFF_OWNER, m05.M05_OWNER))
    assert executable == [("m01_staff_assignable",), ("m01_staff_member_standing",)]
    for table in ("m01_staff", "m01_staff_roles", "m01_staff_role_guard",
                  "m01_staff_followup_access"):
        assert st.admin_rows(install, sql.SQL(
            "SELECT has_table_privilege(%s, %s, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
            "REFERENCES,TRIGGER') OR has_any_column_privilege(%s, %s, 'SELECT,INSERT,UPDATE,"
            "REFERENCES')"), (m05.M05_OWNER, f"{install.schema}.{table}", m05.M05_OWNER,
                              f"{install.schema}.{table}")) == [(False,)], table


EXPECTED_009: Final = {
    "tenants": {"tenant_id", "schema_key"},
    "support_operator_logins": {"login_oid", "login_name", "actor_id"},
    "support_access_grants": {"grant_id", "actor_id", "tenant_id", "ticket_reference",
                              "capabilities", "state", "starts_at", "expires_at"},
}


def test_privw301_staff_owner_shared_column_grants_are_exact(
    staff_install: st.StaffInstall,
) -> None:
    install = _setup(staff_install)
    for table, columns in EXPECTED_009.items():
        granted = st.admin_rows(install, sql.SQL(
            "SELECT a.attname FROM pg_attribute a, LATERAL aclexplode(a.attacl) x"
            " WHERE a.attrelid = %s::regclass AND x.grantee = %s::regrole"
            " AND x.privilege_type = 'SELECT'"), (f"shared.{table}", st.STAFF_OWNER))
        assert {row[0] for row in granted} == columns, table
        assert st.admin_rows(install, "SELECT has_table_privilege(%s, %s,"
                             " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
                             (st.STAFF_OWNER, f"shared.{table}")) == [(False,)], table
        # Effective privilege per live column (Codex 221349Z): inherited and PUBLIC
        # grants count too. SELECT exactly on the expected set; no write anywhere.
        effective = st.admin_rows(install, sql.SQL(
            "SELECT a.attname, has_column_privilege(%s, a.attrelid, a.attname, 'SELECT'),"
            " has_column_privilege(%s, a.attrelid, a.attname, 'INSERT,UPDATE,REFERENCES')"
            " FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0"
            " AND NOT a.attisdropped"), (st.STAFF_OWNER, st.STAFF_OWNER, f"shared.{table}"))
        assert {name for name, select, _ in effective if select} == columns, table
        assert [name for name, _, write in effective if write] == [], table
