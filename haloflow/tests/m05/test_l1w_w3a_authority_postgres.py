"""L-1W W3a: authority from role rows, with positive controls beside every forged-flag
refusal (Codex 213713Z). Contract v1 §1, §4. PostgreSQL, through the runtime shim.

Rows: C-03 (forged supervise refused) and C-03+ (real supervisor applied: assign,
reassign, start_progress and resolve on another member's item); C-04 (forged
staff_admin refused) and C-04+ (real clinic admin's grant_role applied); R-02
(supervisor removed -> refused at the next call); R-03 (self grant / self revoke of
clinic_admin refused).

W3a keeps v1 callable (W3b closes it), so these rows prove the v2 paths only.
"""

from __future__ import annotations

import l1w_w3a_support as w3
import m05_support as m05
import pytest
import staff_support as st

pytestmark = pytest.mark.postgres


def _setup(install: st.StaffInstall) -> st.StaffInstall:
    """In the test body (class-B red rule): a missing W3a fails the row, not setup."""

    w3.ready_with_staff(install)
    w3.ensure_staff(install, st.ADMIN)
    for staff_id in (m05.SUP, st.ADMIN, m05.S1, m05.S2):
        for role in ("supervisor", "clinic_admin"):
            w3.drop_role(install, staff_id, role)  # FIXTURE SETUP: clean slate
    return install


# ---- C-03 / C-03+: supervise comes only from the supervisor role row ----------------


def test_c03_forged_supervise_without_role_row_is_refused_state_unchanged(
    staff_install: st.StaffInstall,
) -> None:
    install = _setup(staff_install)
    item = w3.item(install)
    before = st.full_snapshot(install)
    st.expect_code("NOT_AUTHORIZED", lambda: w3.apply_v2(
        install, w3.action(item, 1, "assign", actor=m05.SUP, assignee=m05.S1, supervise=True)))
    refused = w3.prepare_v2(
        install, w3.action(item, 1, "assign", actor=m05.SUP, assignee=m05.S1, supervise=True))
    assert (refused["decision"], refused["code"]) == ("REFUSED", "NOT_AUTHORIZED")
    assert st.full_snapshot(install) == before


def test_c03p_real_supervisor_assign_and_reassign_are_applied(
    staff_install: st.StaffInstall,
) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, m05.SUP, "supervisor")  # FIXTURE SETUP
    item = w3.item(install)
    assigned = w3.apply_v2(
        install, w3.action(item, 1, "assign", actor=m05.SUP, assignee=m05.S1))
    assert assigned["outcome"] == "applied"
    reassigned = w3.apply_v2(
        install, w3.action(item, 2, "reassign", actor=m05.SUP, assignee=m05.S2))
    assert reassigned["outcome"] == "applied"


def test_c03p_real_supervisor_acts_on_another_members_item(staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, m05.SUP, "supervisor")  # FIXTURE SETUP
    item = w3.item(install)
    assert w3.apply_v2(install, w3.action(item, 1, "claim", actor=m05.S1,
                                          supervise=False))["outcome"] == "applied"
    started = w3.apply_v2(install, w3.action(item, 2, "start_progress", actor=m05.SUP))
    assert started["outcome"] == "applied"


def test_c03_caller_flag_only_narrows(staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    """A real supervisor who sends supervise=false is refused: the flag can narrow."""

    w3.seed_role(install, m05.SUP, "supervisor")  # FIXTURE SETUP
    item = w3.item(install)
    st.expect_code("NOT_AUTHORIZED", lambda: w3.apply_v2(
        install, w3.action(item, 1, "assign", actor=m05.SUP, assignee=m05.S1, supervise=False)))


def test_r02_supervisor_removed_is_refused_at_the_next_call(staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, m05.SUP, "supervisor")  # FIXTURE SETUP
    item = w3.item(install)
    assert w3.apply_v2(install, w3.action(item, 1, "assign", actor=m05.SUP,
                                          assignee=m05.S1))["outcome"] == "applied"
    w3.drop_role(install, m05.SUP, "supervisor")  # FIXTURE SETUP
    st.expect_code("NOT_AUTHORIZED", lambda: w3.apply_v2(
        install, w3.action(item, 2, "reassign", actor=m05.SUP, assignee=m05.S2)))


# ---- C-04 / C-04+: clinic_admin comes only from the role row ------------------------


def test_c04_forged_staff_admin_without_role_row_is_refused(staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    before = st.full_snapshot(install)
    st.expect_code("NOT_AUTHORIZED", lambda: w3.admin_v2(
        install, w3.role_command("grant_role", m05.S1, "supervisor", actor=m05.S2,
                                 staff_admin=True)))
    assert st.full_snapshot(install) == before
    assert w3.roles(install) == []


def test_c04p_real_clinic_admin_grants_a_role(staff_install: st.StaffInstall) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, st.ADMIN, "clinic_admin")  # FIXTURE SETUP
    result = w3.admin_v2(install, w3.role_command("grant_role", m05.SUP, "supervisor"))
    assert result["outcome"] == "applied"
    assert (m05.SUP, "supervisor") in w3.roles(install)


@pytest.mark.parametrize("operation", ["grant_role", "revoke_role"])
def test_r03_self_change_of_clinic_admin_is_refused(
    staff_install: st.StaffInstall, operation: str
) -> None:
    install = _setup(staff_install)
    w3.seed_role(install, st.ADMIN, "clinic_admin")  # FIXTURE SETUP
    before = w3.roles(install)
    st.expect_code("NOT_AUTHORIZED", lambda: w3.admin_v2(
        install, w3.role_command(operation, st.ADMIN, "clinic_admin")))
    assert w3.roles(install) == before
