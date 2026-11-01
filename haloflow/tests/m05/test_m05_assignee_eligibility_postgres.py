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
    before = st.state(staff_install)
    command = _assign(item, target)
    result = _apply(staff_install, command)
    delta = st.Delta(before, st.state(staff_install))
    st.check_assignment_delta(delta, before, staff_install, command, result, new_assignee=target)
    delta.untouched(st.TABLES)
    reassign = _cmd(item, 2, "reassign", actor=m05.SUP, assignee=other,
                    permissions=m05.SUPERVISOR_PERMISSIONS)
    assert _apply(staff_install, reassign)["snapshot"]["assignee_id"] == other
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


def _negative_command(install: st.StaffInstall, action: str, target: str) -> dict[str, Any]:
    """A command whose only defect is the target's eligibility: valid item state,
    version and actor permissions, so the refusal can only come from MA-10."""

    item = _item(install)
    if action == "assign":
        return _assign(item, target)
    if action == "reassign":
        current = _eligible(install)
        _apply(install, _assign(item, current))
        return _cmd(item, 2, "reassign", actor=m05.SUP, assignee=target,
                    permissions=m05.SUPERVISOR_PERMISSIONS)
    # claim: the target is the actor, presenting full service-derived permissions.
    return _cmd(item, 1, "claim", actor=target, permissions=m05.STAFF_PERMISSIONS)


@pytest.mark.parametrize("kind", ["unknown", "inactive", "foreign", "no_read", "no_act"])
@pytest.mark.parametrize("action", ["assign", "reassign", "claim"])
def test_ma10bcde_ineligible_target_refused(staff_install, staff_foreign_install, action, kind):
    _setup(staff_install)
    target = _ineligible(kind, staff_install, staff_foreign_install)
    command = _negative_command(staff_install, action, target)
    before = st.state(staff_install)
    assert _prepare(staff_install, command) == {
        "decision": "REFUSED", "code": "ASSIGNEE_NOT_ELIGIBLE", "schema_version": 1,
    }
    st.expect_code("ASSIGNEE_NOT_ELIGIBLE", lambda: _apply(staff_install, command))
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


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
    before = st.state(staff_install)
    st.expect_code("COMMAND_MALFORMED", lambda: _apply(staff_install, command))
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


def test_ma10g_assignable_not_executable_fails_closed(staff_install):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    function = sql.SQL("{}.m01_staff_assignable(uuid)").format(
        sql.Identifier(staff_install.schema))
    st.admin_rows(staff_install, sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(
        function, sql.Identifier(st.M05_OWNER)))
    try:
        before = st.state(staff_install)
        with pytest.raises(psycopg.Error):
            _apply(staff_install, _assign(item, target))
        st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)
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
    """The accepted M05 replay contract (MR-01): outcome "replayed" with the
    ORIGINAL snapshot; prepare classifies REPLAY; nothing written."""

    _setup(staff_install)
    target = _eligible(staff_install)
    item, key = _item(staff_install), st.new_uuid()
    command = _assign(item, target, key=key)
    original = _apply(staff_install, command)
    assert original["outcome"] == "applied"
    st.admin_op(staff_install, st.admin_command("deactivate", target))
    before = st.state(staff_install)
    assert _prepare(staff_install, command) == {
        "decision": "REPLAY", "schema_version": 1, "snapshot": original["snapshot"],
    }
    assert _apply(staff_install, command) == {
        "schema_version": 1, "outcome": "replayed", "snapshot": original["snapshot"],
    }
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


def _action_command(install: st.StaffInstall, action: str, item: str) -> dict[str, Any]:
    if action in ("assign", "reassign"):
        return _cmd(item, 1, action, actor=m05.SUP, permissions=m05.SUPERVISOR_PERMISSIONS,
                    assignee=_eligible(install))
    return _cmd(item, 1, action, actor=m05.SUP, permissions=m05.SUPERVISOR_PERMISSIONS)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
@pytest.mark.parametrize("function", ["m05_prepare_action", "m05_apply_action"])
@pytest.mark.parametrize("action", m05.NON_RESOLVE_ACTIONS)
def test_ma10k_isolation_guard(staff_install, isolation, function, action):
    """After 22023 and before shape; prepare RAISES (environment fault, v1 3.5).
    A competing holder owns the item and SUP's membership row, so a guard placed
    after either lock waits and fails (red review F-MA10-RED-03)."""

    _setup(staff_install)
    item = _item(staff_install)
    command = _action_command(staff_install, action, item)
    before = st.state(staff_install)
    locks = [("m05_followup_items", "item_id", item), ("m01_staff", "staff_id", m05.SUP)]
    for arg in (command, {"schema_version": 9}):
        with st.holder(staff_install, locks), st.runtime_connection(staff_install) as conn:
            conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            st.set_tenant(conn, staff_install)
            st.expect_immediate_code(
                conn, "ISOLATION_UNSUPPORTED",
                lambda arg=arg, conn=conn: st.call_on(conn, staff_install, function, arg),
            )
            conn.rollback()
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


# ---------------------------------------------------------------------------
# Races (v2 5.4; v1r2 correction 2; red review F-MA10-RED-02 exact deltas)
# ---------------------------------------------------------------------------


def _admin_open(install: st.StaffInstall, command: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    conn = st.runtime_connection(install)
    st.set_tenant(conn, install)
    result = st.call_on(conn, install, "m01_staff_admin", command)
    return conn, result


@pytest.mark.parametrize("change", CHANGES)
def test_r1_change_committed_before_apply(staff_install, change):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    st.admin_op(staff_install, _change_command(change, target))
    before = st.state(staff_install)
    st.expect_code("ASSIGNEE_NOT_ELIGIBLE", lambda: _apply(staff_install, _assign(item, target)))
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
@pytest.mark.parametrize("change", CHANGES)
def test_r2_change_uncommitted_apply_waits(staff_install, change, rollback):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    change_command = _change_command(change, target)
    assign = _assign(item, target)
    before = st.state(staff_install)
    admin_conn, admin_result = _admin_open(staff_install, change_command)
    apply_conn = st.runtime_connection(staff_install)
    try:
        st.set_tenant(apply_conn, staff_install)
        with st.background(lambda: st.call_on(apply_conn, staff_install, "m05_apply_action",
                                              assign)) as box:
            st.wait_until_blocked_by(staff_install, apply_conn.info.backend_pid,
                                     admin_conn.info.backend_pid)
            (admin_conn.rollback if rollback else admin_conn.commit)()
        (apply_conn.rollback if "error" in box else apply_conn.commit)()
    finally:
        admin_conn.close()
        apply_conn.close()
    delta = st.Delta(before, st.state(staff_install))
    if rollback:  # R-2r: the change vanished; the assignment is the only effect
        assert "error" not in box, box.get("error")
        st.check_assignment_delta(delta, before, staff_install, assign, box["result"],
                                  new_assignee=target)
        delta.untouched(st.TABLES)
        return
    error = box.get("error")
    assert isinstance(error, psycopg.Error) and error.sqlstate == "P0001"
    assert error.diag.message_primary == "ASSIGNEE_NOT_ELIGIBLE"
    st.check_admin_delta(delta, before, staff_install, change_command, admin_result)
    delta.untouched(st.M05_CONTENT_TABLES)


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("same_txn_prepare", [False, True], ids=["apply", "prepare_apply"])
def test_r4_r5_apply_first_admin_waits(staff_install, change, same_txn_prepare):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    command = _assign(item, target)
    change_command = _change_command(change, target)
    before = st.state(staff_install)
    apply_conn = st.runtime_connection(staff_install)
    admin_conn = st.runtime_connection(staff_install)
    try:
        st.set_tenant(apply_conn, staff_install)
        if same_txn_prepare:  # R-5: prepare holds the membership lock until commit
            assert st.call_on(apply_conn, staff_install, "m05_prepare_action",
                              command)["decision"] == "NEW"
        else:
            applied = st.call_on(apply_conn, staff_install, "m05_apply_action", command)
        st.set_tenant(admin_conn, staff_install)
        with st.background(lambda: st.call_on(admin_conn, staff_install, "m01_staff_admin",
                                              change_command)) as box:
            st.wait_until_blocked_by(staff_install, admin_conn.info.backend_pid,
                                     apply_conn.info.backend_pid)
            if same_txn_prepare:
                applied = st.call_on(apply_conn, staff_install, "m05_apply_action", command)
            apply_conn.commit()
        admin_conn.commit()
    finally:
        apply_conn.close()
        admin_conn.close()
    assert "error" not in box, box.get("error")
    delta = st.Delta(before, st.state(staff_install))
    st.check_assignment_delta(delta, before, staff_install, command, applied,
                              new_assignee=target)
    st.check_admin_delta(delta, before, staff_install, change_command, box["result"])


@pytest.mark.parametrize("change", CHANGES)
def test_r6_split_prepare_apply_change_between(staff_install, change):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    command = _assign(item, target)
    assert _prepare(staff_install, command)["decision"] == "NEW"
    st.admin_op(staff_install, _change_command(change, target))
    before = st.state(staff_install)
    st.expect_code("ASSIGNEE_NOT_ELIGIBLE", lambda: _apply(staff_install, command))
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


@pytest.mark.parametrize("change", CHANGES)
def test_r8_lock_timeout_never_accepts(staff_install, change):
    _setup(staff_install)
    target = _eligible(staff_install)
    item = _item(staff_install)
    before = st.state(staff_install)
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
        # The blocking writer is still open: nothing is visible from either side.
        st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)
    finally:
        admin_conn.rollback()
        admin_conn.close()
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


# ---------------------------------------------------------------------------
# Composition (v1 3.5, v1r2 correction 5; red review F-MA10-RED-03)
# ---------------------------------------------------------------------------


def _refused_without_waiting(
    install: st.StaffInstall,
    first: Callable[[Any], Any],
    first_holds: list[tuple[str, str, Any]],
    second: Callable[[Any], Any],
    second_targets: list[tuple[str, str, Any]],
) -> None:
    """The first call runs and its locks are shown held; a separate holder then
    owns every row the second call would lock; the second call must refuse with
    COMPOSITION_UNSUPPORTED at once. A late rejection would wait on the holder
    and fail on the safety lock_timeout or the elapsed bound."""

    conn = st.runtime_connection(install)
    try:
        st.set_tenant(conn, install)
        first(conn)
        for table, column, value in first_holds:
            assert st.row_locked_elsewhere(install, table, column, value), (table, value)
        with st.holder(install, second_targets):
            st.expect_immediate_code(conn, "COMPOSITION_UNSUPPORTED", lambda: second(conn))
    finally:
        conn.rollback()
        conn.close()


def test_rc1_admin_then_m05_and_reverse(staff_install):
    _setup(staff_install)
    target, other = _eligible(staff_install), _eligible(staff_install)
    item = _item(staff_install)
    _refused_without_waiting(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("rename", other, display_name="Renamed")),
        [("m01_staff", "staff_id", other)],
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(item, target)),
        [("m05_followup_items", "item_id", item), ("m01_staff", "staff_id", target),
         ("m01_staff", "staff_id", m05.SUP)],
    )
    _refused_without_waiting(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(item, target)),
        [("m05_followup_items", "item_id", item)],
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("deactivate", other)),
        [("m01_staff", "staff_id", other)],
    )


def test_rc1_two_items_in_one_txn(staff_install):
    _setup(staff_install)
    target, other = _eligible(staff_install), _eligible(staff_install)
    first_item, second_item = _item(staff_install), _item(staff_install)
    _refused_without_waiting(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(first_item, target)),
        [("m05_followup_items", "item_id", first_item)],
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(second_item, other)),
        [("m05_followup_items", "item_id", second_item), ("m01_staff", "staff_id", other)],
    )


def test_rc2_changed_target_and_reused_admin_key(staff_install):
    _setup(staff_install)
    a, b = _eligible(staff_install), _eligible(staff_install)
    item, key = _item(staff_install), st.new_uuid()
    _refused_without_waiting(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m05_prepare_action", _assign(item, a, key=key)),
        [("m05_followup_items", "item_id", item)],
        lambda c: st.call_on(c, staff_install, "m05_apply_action", _assign(item, b, key=key)),
        [("m01_staff", "staff_id", b)],
    )
    x, y = _eligible(staff_install), _eligible(staff_install)
    admin_key = st.new_uuid()
    _refused_without_waiting(
        staff_install,
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("deactivate", x, key=admin_key)),
        [("m01_staff", "staff_id", x)],
        lambda c: st.call_on(c, staff_install, "m01_staff_admin",
                             st.admin_command("deactivate", y, key=admin_key)),
        [("m01_staff", "staff_id", y)],
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

        def assign(item: str = item, target: str = target) -> Any:
            return _apply(staff_install, _assign(item, target))

        def deactivate(target: str = target) -> Any:
            return st.admin_op(staff_install, st.admin_command("deactivate", target))

        with st.background(assign) as box_a, st.background(deactivate) as box_b:
            pass
        assert "error" not in box_b, box_b.get("error")  # admin never refused here
        error = box_a.get("error")
        if error is not None:
            assert isinstance(error, psycopg.Error)
            assert error.sqlstate != "40P01", "deadlock"
            assert error.diag.message_primary == "ASSIGNEE_NOT_ELIGIBLE", str(error)
