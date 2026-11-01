"""MA-10 in M05 prepare/apply: assignee eligibility, isolation and composition guards,
and the deactivation/revocation races, on PostgreSQL 17 through the real definer
boundary (runtime -> M05 gateway -> m01_staff_assignable).

Package v1 + v1r1 delta + v1r2 adoption (Claude/Codex aligned 2026-11-01). Owner
choices (2026-11-01): assignable = active with follow-up read AND act; a deactivated
assignee's items stay assigned and visible, only a supervisor reassigns.

RED TRANCHE: every row fails in its own body until the directory gateways and the
B3a action slice are implemented. Mutants R-M1..R-M3 mutate implemented bodies and
are produced with the implementation evidence, not here.

Rows:
- MA-10a eligible target for claim (target = actor), assign, reassign -> accepted.
- MA-10b/c/d/e unknown / inactive / other-clinic / missing read or act -> refused.
- MA-10f assignee-permission keys in the command -> COMMAND_MALFORMED.
- MA-10g assignable missing EXECUTE -> the action fails, zero writes.
- MA-10i deactivated assignee: item unchanged and visible; display says inactive;
        supervisor reassigns; non-supervisor refused.
- MA-10j replay after the target is deactivated -> original snapshot.
- MA-10k isolation guard after 22023 and before shape, prepare and apply, every type.
- R-1/R-2/R-2r/R-4/R-5/R-6/R-8 races, each over deactivate / revoke read / revoke act.
- R-C1/R-C2 composition guard; no additional lock by the refused call.
- R-D1 concurrent reassign vs deactivation: never a deadlock.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import m05_support as m05
import psycopg
import pytest
import staff_support as st
from psycopg import sql
from test_m05_actions_postgres import _cmd
from test_m05_create_postgres import _command, _create, _ready, _source

pytestmark = pytest.mark.postgres

CHANGES = ("deactivate", "revoke_read", "revoke_act")


def _setup(install: st.StaffInstall) -> None:
    _ready(install)  # installs through the staff registry, then the M05 test profiles
    rows = st.admin_rows(
        install,
        sql.SQL("SELECT count(*) FROM {}.m01_staff WHERE staff_id = %s")
        .format(sql.Identifier(install.schema)), (m05.SUP,),
    )
    if rows == [(0,)]:
        for actor in (m05.S1, m05.SUP):
            st.seed_staff(install, actor, name=f"Actor {actor[-4:]}")


def _item(install: st.StaffInstall) -> str:
    return str(_create(install, _command(_source(install)))["item_id"])


def _change_command(change: str, staff: str) -> dict[str, Any]:
    if change == "deactivate":
        return st.admin_command("deactivate", staff)
    return st.admin_command("revoke", staff, access=change.split("_")[1])


def _assign(item: str, target: str, *, version: int = 1, key: str | None = None) -> dict[str, Any]:
    return _cmd(item, version, "assign", key=key, actor=m05.SUP, assignee=target,
                permissions=m05.SUPERVISOR_PERMISSIONS)


def _apply(install: st.StaffInstall, command: Any) -> dict[str, Any]:
    result: dict[str, Any] = st.call(install, "m05_apply_action", command)
    return result


def _prepare(install: st.StaffInstall, command: Any) -> dict[str, Any]:
    result: dict[str, Any] = st.call(install, "m05_prepare_action", command)
    return result


def _eligible(install: st.StaffInstall) -> str:
    staff = st.new_uuid()
    st.seed_staff(install, staff, name="Target")
    return staff


# ---------------------------------------------------------------------------
# MA-10a..k
# ---------------------------------------------------------------------------


def test_ma10a_eligible_targets_accepted(staff_install):
    _setup(staff_install)
    target, other = _eligible(staff_install), _eligible(staff_install)
    item = _item(staff_install)
    assert _apply(staff_install, _assign(item, target))["snapshot"]["assignee_id"] == target
    reassigned = _apply(staff_install, _cmd(item, 2, "reassign", actor=m05.SUP, assignee=other,
                                            permissions=m05.SUPERVISOR_PERMISSIONS))
    assert reassigned["snapshot"]["assignee_id"] == other
    claimed = _apply(staff_install, _cmd(_item(staff_install), 1, "claim", actor=m05.S1))
    assert claimed["snapshot"]["assignee_id"] == m05.S1


def _ineligible(kind: str, install: st.StaffInstall, foreign: st.StaffInstall) -> str:
    staff = st.new_uuid()
    if kind == "unknown":
        return staff
    if kind == "inactive":
        st.seed_staff(install, staff, active=False)
    elif kind == "foreign":
        foreign.require()
        st.seed_staff(foreign, staff)
    elif kind == "no_read":
        st.seed_staff(install, staff, access=("act",))
    elif kind == "no_act":
        st.seed_staff(install, staff, access=("read",))
    return staff


@pytest.mark.parametrize("kind", ["unknown", "inactive", "foreign", "no_read", "no_act"])
def test_ma10bcde_ineligible_target_refused(staff_install, staff_foreign_install, kind):
    _setup(staff_install)
    target = _ineligible(kind, staff_install, staff_foreign_install)
    item = _item(staff_install)
    before = st.full_snapshot(staff_install)
    command = _assign(item, target)
    assert _prepare(staff_install, command) == {
        "decision": "REFUSED", "code": "ASSIGNEE_NOT_ELIGIBLE", "schema_version": 1,
    }
    st.expect_code("ASSIGNEE_NOT_ELIGIBLE", lambda: _apply(staff_install, command))
    assert st.full_snapshot(staff_install) == before


def test_ma10b_claim_by_actor_not_in_directory_refused(staff_install):
    _setup(staff_install)
    outsider = st.new_uuid()
    item = _item(staff_install)
    before = st.full_snapshot(staff_install)
    st.expect_code("ASSIGNEE_NOT_ELIGIBLE",
                   lambda: _apply(staff_install, _cmd(item, 1, "claim", actor=outsider)))
    assert st.full_snapshot(staff_install) == before


@pytest.mark.parametrize(
    "extra",
    [{"assignee_permissions": {"read": True, "act": True}}, {"assignee_eligible": True}],
    ids=["permissions_object", "eligible_flag"],
)
def test_ma10f_caller_assignee_fields_rejected(staff_install, extra):
    _setup(staff_install)
    item = _item(staff_install)
    command = _assign(item, st.new_uuid())
    command["action"].update(extra)
    before = st.full_snapshot(staff_install)
    st.expect_code("COMMAND_MALFORMED", lambda: _apply(staff_install, command))
    assert st.full_snapshot(staff_install) == before


def test_ma10g_assignable_not_executable_fails_closed(staff_install):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    function = sql.SQL("{}.m01_staff_assignable(uuid)").format(
        sql.Identifier(staff_install.schema))
    st.admin_rows(staff_install, sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(
        function, sql.Identifier(st.M05_OWNER)))
    try:
        before = st.full_snapshot(staff_install)
        with pytest.raises(psycopg.Error):
            _apply(staff_install, _assign(item, target))
        assert st.full_snapshot(staff_install) == before
    finally:
        st.admin_rows(staff_install, sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
            function, sql.Identifier(st.M05_OWNER)))


def test_ma10i_deactivated_assignee_item_visible_supervisor_reassigns(staff_install):
    _setup(staff_install)
    target, replacement = _eligible(staff_install), _eligible(staff_install)
    item = _item(staff_install)
    _apply(staff_install, _assign(item, target))
    st.admin_op(staff_install, st.admin_command("deactivate", target))
    view = st.call(staff_install, "m05_get_item", item)
    assert view["assignee_id"] == target and view["version"] == 2
    assert st.display(staff_install, [target])["staff"] == [
        {"staff_id": target, "display_name": "Target", "status": "inactive"}
    ]
    st.expect_code(
        "NOT_AUTHORIZED",
        lambda: _apply(staff_install, _cmd(item, 2, "reassign", actor=m05.S1,
                                           assignee=replacement)),
    )
    moved = _apply(staff_install, _cmd(item, 2, "reassign", actor=m05.SUP, assignee=replacement,
                                       permissions=m05.SUPERVISOR_PERMISSIONS))
    assert moved["snapshot"]["assignee_id"] == replacement


def test_ma10j_replay_after_target_deactivated(staff_install):
    _setup(staff_install)
    target = _eligible(staff_install)
    item, key = _item(staff_install), st.new_uuid()
    command = _assign(item, target, key=key)
    original = _apply(staff_install, command)
    st.admin_op(staff_install, st.admin_command("deactivate", target))
    before = st.full_snapshot(staff_install)
    assert _apply(staff_install, command) == original
    assert st.full_snapshot(staff_install) == before


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
@pytest.mark.parametrize("function", ["m05_prepare_action", "m05_apply_action"])
@pytest.mark.parametrize("action", m05.NON_RESOLVE_ACTIONS)
def test_ma10k_isolation_guard(staff_install, isolation, function, action):
    _setup(staff_install)
    item = _item(staff_install)
    command = _cmd(item, 1, action, actor=m05.SUP, permissions=m05.SUPERVISOR_PERMISSIONS,
                   assignee=_eligible(staff_install) if action in ("assign", "reassign")
                   else None)
    before = st.full_snapshot(staff_install)
    # prepare RAISES here (environment fault, not a domain refusal: v1 3.5).
    st.expect_code("ISOLATION_UNSUPPORTED",
                   lambda: st.call(staff_install, function, command, isolation=isolation))
    # ...and the guard precedes the shape check.
    st.expect_code("ISOLATION_UNSUPPORTED",
                   lambda: st.call(staff_install, function, {"schema_version": 9},
                                   isolation=isolation))
    assert st.full_snapshot(staff_install) == before


# ---------------------------------------------------------------------------
# Races (v2 5.4; v1r2 correction 2 for expected writes)
# ---------------------------------------------------------------------------


def _admin_open(install: st.StaffInstall, command: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    conn = st.runtime_connection(install)
    st.set_tenant(conn, install)
    result = st.call_on(conn, install, "m01_staff_admin", command)
    return conn, result


def _assert_admin_delta(
    before: dict[str, list[str]], after: dict[str, list[str]], result: dict[str, Any]
) -> None:
    keys = st.added_rows(before["m01_staff_action_keys"], after["m01_staff_action_keys"])
    (log,) = st.added_rows(before["m01_staff_admin_log"], after["m01_staff_admin_log"])
    assert len(keys) == 1 and log["result"] == result


def _assert_assignment_delta(before: dict[str, list[str]], after: dict[str, list[str]]) -> None:
    (history,) = st.added_rows(before["m05_followup_history"], after["m05_followup_history"])
    (action,) = st.added_rows(before["m05_followup_actions"], after["m05_followup_actions"])
    assert history["action"] in ("assign", "reassign", "claim")
    assert action["client_action_id"] is not None


@pytest.mark.parametrize("change", CHANGES)
def test_r1_change_committed_before_apply(staff_install, change):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    st.admin_op(staff_install, _change_command(change, target))
    before = st.full_snapshot(staff_install)
    st.expect_code("ASSIGNEE_NOT_ELIGIBLE", lambda: _apply(staff_install, _assign(item, target)))
    assert st.full_snapshot(staff_install) == before


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
@pytest.mark.parametrize("change", CHANGES)
def test_r2_change_uncommitted_apply_waits(staff_install, change, rollback):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    before = st.full_snapshot(staff_install)
    admin_conn, admin_result = _admin_open(staff_install, _change_command(change, target))
    apply_conn = st.runtime_connection(staff_install)
    try:
        st.set_tenant(apply_conn, staff_install)
        with st.background(lambda: st.call_on(apply_conn, staff_install, "m05_apply_action",
                                              _assign(item, target))) as box:
            st.wait_until_blocked_by(staff_install, apply_conn.info.backend_pid,
                                     admin_conn.info.backend_pid)
            (admin_conn.rollback if rollback else admin_conn.commit)()
        apply_conn.commit()
    finally:
        admin_conn.close()
        apply_conn.close()
    after = st.full_snapshot(staff_install)
    if rollback:  # R-2r
        assert "error" not in box, box.get("error")
        assert box["result"]["snapshot"]["assignee_id"] == target
        _assert_assignment_delta(before, after)
        for table in st.TABLES:
            assert after[table] == before[table], table
        return
    error = box.get("error")
    assert isinstance(error, psycopg.Error) and error.sqlstate == "P0001"
    assert error.diag.message_primary == "ASSIGNEE_NOT_ELIGIBLE"
    for table in st.M05_CONTENT_TABLES:
        assert after[table] == before[table], table
    _assert_admin_delta(before, after, admin_result)


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("same_txn_prepare", [False, True], ids=["apply", "prepare_apply"])
def test_r4_r5_apply_first_admin_waits(staff_install, change, same_txn_prepare):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    before = st.full_snapshot(staff_install)
    command = _assign(item, target)
    apply_conn = st.runtime_connection(staff_install)
    admin_conn = st.runtime_connection(staff_install)
    try:
        st.set_tenant(apply_conn, staff_install)
        if same_txn_prepare:  # R-5: prepare holds the membership lock until commit
            assert st.call_on(apply_conn, staff_install, "m05_prepare_action",
                              command)["decision"] == "NEW"
        else:
            st.call_on(apply_conn, staff_install, "m05_apply_action", command)
        st.set_tenant(admin_conn, staff_install)
        with st.background(lambda: st.call_on(admin_conn, staff_install, "m01_staff_admin",
                                              _change_command(change, target))) as box:
            st.wait_until_blocked_by(staff_install, admin_conn.info.backend_pid,
                                     apply_conn.info.backend_pid)
            if same_txn_prepare:
                st.call_on(apply_conn, staff_install, "m05_apply_action", command)
            apply_conn.commit()
        admin_conn.commit()
    finally:
        apply_conn.close()
        admin_conn.close()
    assert "error" not in box, box.get("error")
    after = st.full_snapshot(staff_install)
    _assert_assignment_delta(before, after)
    _assert_admin_delta(before, after, box["result"])
    assert st.call(staff_install, "m05_get_item", item)["assignee_id"] == target


@pytest.mark.parametrize("change", CHANGES)
def test_r6_split_prepare_apply_change_between(staff_install, change):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    command = _assign(item, target)
    assert _prepare(staff_install, command)["decision"] == "NEW"
    st.admin_op(staff_install, _change_command(change, target))
    before = st.full_snapshot(staff_install)
    st.expect_code("ASSIGNEE_NOT_ELIGIBLE", lambda: _apply(staff_install, command))
    assert st.full_snapshot(staff_install) == before


@pytest.mark.parametrize("change", CHANGES)
def test_r8_lock_timeout_never_accepts(staff_install, change):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    before = st.full_snapshot(staff_install)
    admin_conn, _ = _admin_open(staff_install, _change_command(change, target))
    try:
        apply_conn = st.runtime_connection(staff_install)
        try:
            apply_conn.execute("SET LOCAL lock_timeout = '300ms'")
            st.set_tenant(apply_conn, staff_install)
            with pytest.raises(psycopg.Error) as timed_out:
                st.call_on(apply_conn, staff_install, "m05_apply_action", _assign(item, target))
            assert timed_out.value.sqlstate == "55P03"
            apply_conn.rollback()
        finally:
            apply_conn.close()
        # The blocking writer is still open: nothing it did is visible, nothing
        # the contender did persisted.
        assert st.full_snapshot(staff_install) == before
    finally:
        admin_conn.rollback()
        admin_conn.close()
    assert st.full_snapshot(staff_install) == before


# ---------------------------------------------------------------------------
# Composition (v1 3.5, v1r2 correction 5)
# ---------------------------------------------------------------------------


def _row_lockable(install: st.StaffInstall, staff: str) -> bool:
    with psycopg.connect(install.ids.admin) as conn:
        try:
            conn.execute(
                sql.SQL("SELECT 1 FROM {}.m01_staff WHERE staff_id = %s FOR UPDATE NOWAIT")
                .format(sql.Identifier(install.schema)), (staff,),
            )
            return True
        except psycopg.errors.LockNotAvailable:
            return False
        finally:
            conn.rollback()


def _refused_in_same_txn(
    install: st.StaffInstall,
    first: Callable[[Any], Any],
    second: Callable[[Any], Any],
    untouched: str,
) -> None:
    conn = st.runtime_connection(install)
    try:
        st.set_tenant(conn, install)
        first(conn)
        with pytest.raises(psycopg.Error) as refused:
            second(conn)
        assert refused.value.sqlstate == "P0001"
        assert refused.value.diag.message_primary == "COMPOSITION_UNSUPPORTED"
        # The refused call took no lock: the row it would have locked is free while
        # this (aborted) transaction is still open.
        assert _row_lockable(install, untouched)
    finally:
        conn.rollback()
        conn.close()


def test_rc1_admin_then_m05_and_reverse(staff_install):
    _setup(staff_install)
    target, other = _eligible(staff_install), _eligible(staff_install)
    item = _item(staff_install)
    _refused_in_same_txn(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("rename", other, display_name="Renamed")),
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(item, target)),
        target,
    )
    _refused_in_same_txn(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(item, target)),
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("deactivate", other)),
        other,
    )


def test_rc1_two_items_in_one_txn(staff_install):
    _setup(staff_install)
    target, other = _eligible(staff_install), _eligible(staff_install)
    first_item, second_item = _item(staff_install), _item(staff_install)
    _refused_in_same_txn(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(first_item, target)),
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(second_item, other)),
        other,
    )


def test_rc2_changed_target_and_reused_admin_key(staff_install):
    _setup(staff_install)
    a, b = _eligible(staff_install), _eligible(staff_install)
    item, key = _item(staff_install), st.new_uuid()
    _refused_in_same_txn(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m05_prepare_action", _assign(item, a, key=key)),
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(item, b, key=key)),
        b,
    )
    x, y = _eligible(staff_install), _eligible(staff_install)
    admin_key = st.new_uuid()
    _refused_in_same_txn(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("deactivate", x, key=admin_key)),
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("deactivate", y, key=admin_key)),
        y,
    )


# ---------------------------------------------------------------------------
# R-D1
# ---------------------------------------------------------------------------

DEADLOCK_ROUNDS = 10


def test_rd1_concurrent_reassign_and_deactivation_never_deadlock(staff_install):
    _setup(staff_install)
    for _ in range(DEADLOCK_ROUNDS):
        target = _eligible(staff_install)
        item = _item(staff_install)
        errors: list[BaseException] = []

        def assign(item: str = item, target: str = target) -> Any:
            return _apply(staff_install, _assign(item, target))

        def deactivate(target: str = target) -> Any:
            return st.admin_op(staff_install, st.admin_command("deactivate", target))

        with st.background(assign) as box_a, st.background(deactivate) as box_b:
            pass
        for box in (box_a, box_b):
            if "error" in box:
                errors.append(box["error"])
        for error in errors:
            assert isinstance(error, psycopg.Error)
            assert error.sqlstate != "40P01", "deadlock"
            assert error.diag.message_primary == "ASSIGNEE_NOT_ELIGIBLE", str(error)
