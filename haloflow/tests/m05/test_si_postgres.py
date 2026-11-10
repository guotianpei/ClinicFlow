"""L-1S package S-I (simulated EHR confirmation write-back) — PostgreSQL 17 rows, SQL path
(RED BY DESIGN).

Governing and SQL interface pins: see `si_support`. Each row installs a fresh S-I tenant in
its body (class-B red rule), so the expected red cause is ModuleNotFoundError for
`haloflow.m05.writeback_units` / `.writeback_statements`; the controls (marked CONTROL) use
only existing code and must pass now.

Rows (requirement in brackets, packet v1 numbering):
- P-D01..D05 [SI-R1] directory v3 strict key set, version bump, v2 runtime EXECUTE closed,
  CONTROL: the H3 v2 directory is unchanged on an H3 tenant.
- P-C01..C06 [SI-R2/R3] capture values, version moved, after consume, replay, no
  UPDATE/DELETE, the two-connection capture / consume race in both orders.
- P-M01..M18 [SI-R5] the WB2 matrix against the frozen baseline, with precedence: no
  baseline > baseline unknown > identity / schedule / replacement / cancelled >
  provider or department baseline null > status (Codex pin 5: a replaced or wrong
  appointment with a positive status is never observed_positive or skipped).
- P-M20 [SI-R5] persisted NO / STOP / ambiguous replies never create write-back.
- P-Y01 [SI-R10] R1 failures: stored r1_failures and next_at at 60 / 300 / 900 s, then the
  fourth raises ehr_unreachable (owner C5).
- P-S06 [SI-R6] an unspent permit whose lease lapsed is recovered through due -> claim
  (void) -> a fresh decide -> a new permit.
- P-G02 [SI-R1/R17] the real S-I catalogue binds each rebound key exactly once.
- P-S01..S05 [SI-R6] one spend; stale fence; lease expired then void on the next claim;
  voided permit; op not permitted.
- P-K01 [SI-R8] a spent, unsettled permit is reconcile-only, and only after
  apply_deadline + RECONCILE_MARGIN_S in database time; reconcile closes the permit.
- P-R01..R04 [SI-R9] settle: observed_positive only for a positive, fully matching read;
  replaced + positive -> status_changed; no positive -> could_not_verify; stale fence.
- P-L06 [SI-R7] guards use clock_timestamp(), not the transaction start.
- P-G01 [SI-R13/R17] ACL: runtime EXECUTE on every S-I function, none on the closed
  functions; no runtime table privilege on the S-I tables.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import m02_support
import pytest
import sa_support as sa
import sh2_support as sh2
import sh3_support as sh3
import sh_support as sh
import si_support as si

pytestmark = pytest.mark.postgres


@pytest.fixture
def si_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[si.SIInstall]:
    yield from si.module_install(request, m02_ids, m05_reset, f"p:{request.node.name}")


@pytest.fixture
def h3_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[sh3.SH3Install]:
    yield from sh3.module_install(request, m02_ids, m05_reset, f"p-h3:{request.node.name}")


def _ready(install: si.SIInstall) -> si.SIInstall:
    return si.ready_install(install)


# --- P-D: directory v3 -----------------------------------------------------------------------


def test_si_pd01_directory_v3_stores_identity_and_always_bumps_the_version(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    patient, phone, ref, zone = si.seed3(install)
    profile = sh2.profile_row(install, ref)
    assert profile is not None
    assert (profile["provider_ref"], profile["department_ref"]) == (si.PROVIDER, si.DEPARTMENT)
    version = profile["source_version"]
    start = sh.start_of(install, ref)
    again = si.directory3(install, "appointment_put",
                          **si.appointment3(ref, patient, start, department_tz=zone))
    assert again["outcome"] == "applied"
    after = sh2.profile_row(install, ref)
    assert after is not None and after["source_version"] == version + 1  # identical content
    moved = si.directory3(install, "appointment_put",
                          **si.appointment3(ref, patient, start, department_tz=zone,
                                            provider_ref="prov-99", department_ref=None))
    assert moved["outcome"] == "applied"
    last = sh2.profile_row(install, ref)
    assert last is not None
    assert (last["provider_ref"], last["department_ref"], last["source_version"]) == (
        "prov-99", None, version + 2)


@pytest.mark.parametrize("change", ["v2_shape", "missing_provider", "missing_department",
                                    "extra_key", "provider_number", "department_too_long"])
def test_si_pd02_directory_v3_key_set_is_strict(si_install: si.SIInstall, change: str) -> None:
    install = _ready(si_install)
    zone, start = sh2.in_window(install)
    patient, ref = sa.new_id(), sa.new_id()
    fields = si.appointment3(ref, patient, start, department_tz=zone)
    if change == "v2_shape":
        fields.pop("provider_ref"), fields.pop("department_ref")
    elif change == "missing_provider":
        fields.pop("provider_ref")
    elif change == "missing_department":
        fields.pop("department_ref")
    elif change == "extra_key":
        fields["practitioner"] = "x"
    elif change == "provider_number":
        fields["provider_ref"] = 42
    else:
        fields["department_ref"] = "d" * 65
    command = {"schema_version": 1, "op": "appointment_put", **fields,
               "attribution": sa.attribution(sa.W_DIRECTORY)}
    sh2.refused(install, si.FN["directory"], command, "COMMAND_MALFORMED")
    assert sh2.profile_row(install, ref) is None


def test_si_pd03_rebound_directory_key_reaches_v3(si_install: si.SIInstall) -> None:
    install = _ready(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            zone, start = sh2.in_window(install)
            patient, ref = sa.new_id(), sa.new_id()
            result = await sh2.gateway_directory(rt, install, "appointment_put",
                                                 **si.appointment3(ref, patient, start,
                                                                   department_tz=zone))
            assert result["outcome"] == "applied", result
            profile = sh2.profile_row(install, ref)
            assert profile is not None and profile["provider_ref"] == si.PROVIDER

    asyncio.run(body())


def test_si_pd04_old_ingress_is_closed_to_the_runtime(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    for function in si.CLOSED_FUNCTIONS:
        assert not si.execute_privilege(install, function), function
    zone, start = sh2.in_window(install)
    command = {"schema_version": 1, "op": "appointment_put",
               **sh2.appointment_fields(sa.new_id(), sa.new_id(), start, department_tz=zone),
               "attribution": sa.attribution(sa.W_DIRECTORY)}
    with pytest.raises(Exception) as denied:  # noqa: PT011 - the database refusal class
        sa.call(install, "m05_directory_v2", command)
    assert "permission denied" in str(denied.value)


def test_si_pd05_control_h3_v2_directory_unchanged(h3_install: sh3.SH3Install) -> None:
    """CONTROL (existing code only): on an H3 tenant the v2 shape is accepted and the v3
    keys are refused, so S-I changes nothing for an older registry."""

    install = sh3.ready_install(h3_install)
    zone, start = sh2.in_window(install)
    patient, ref = sa.new_id(), sa.new_id()
    result = sh2.directory2(install, "appointment_put",
                            **sh2.appointment_fields(ref, patient, start, department_tz=zone))
    assert result["outcome"] == "applied"
    command = {"schema_version": 1, "op": "appointment_put",
               **sh2.appointment_fields(sa.new_id(), patient, start, department_tz=zone),
               "provider_ref": si.PROVIDER, "department_ref": si.DEPARTMENT,
               "attribution": sa.attribution(sa.W_DIRECTORY)}
    sh2.refused(install, "m05_directory_v2", command, "COMMAND_MALFORMED")


# --- P-C: baseline capture ---------------------------------------------------------------------


def test_si_pc01_capture_freezes_the_profile_and_op_identity(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    flow = si.sent(install)
    row = si.baseline(install, flow["op_id"])
    assert row is not None
    profile = sh2.profile_row(install, flow["ref"])
    assert profile is not None
    assert row["attempt_id"] == flow["attempt"]
    assert row["appointment_ref"] == flow["ref"]
    assert (row["confirmation"], row["provider_ref"], row["department_ref"]) == (
        "not_confirmed", si.PROVIDER, si.DEPARTMENT)
    assert row["source_version"] == profile["source_version"]
    assert datetime.fromisoformat(row["start_at"]) == sh.start_of(install, flow["ref"])


def test_si_pc02_a_profile_moved_after_prepare_is_refused(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    patient, _phone, ref, zone = si.seed3(install)
    op_id, attempt = sh2.claimed(install, ref)
    si.directory3(install, "appointment_put",
                  **si.appointment3(ref, patient, sh.start_of(install, ref),
                                    department_tz=zone))  # version moves, content identical
    result = si.call(install, "capture", op_id=op_id, attempt_id=attempt)
    assert result["outcome"] == "refused", result
    assert si.baseline(install, op_id) is None


def test_si_pc03_capture_after_consume_is_refused(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    _p, _ph, ref, _z = si.seed3(install)
    op_id, attempt = sh2.claimed(install, ref)
    assert sh2.consume(install, op_id, attempt)["outcome"] == "send"
    assert si.call(install, "capture", op_id=op_id, attempt_id=attempt)["outcome"] == "refused"
    assert si.baseline(install, op_id) is None


def test_si_pc04_a_second_capture_never_replaces_the_first(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    _p, _ph, ref, _z = si.seed3(install)
    op_id, attempt = sh2.claimed(install, ref)
    assert si.call(install, "capture", op_id=op_id, attempt_id=attempt)["outcome"] == "captured"
    first = si.baseline(install, op_id)
    assert si.call(install, "capture", op_id=op_id, attempt_id=attempt)["outcome"] == "refused"
    assert si.baseline(install, op_id) == first
    other = si.new_uuid()
    assert si.call(install, "capture", op_id=op_id, attempt_id=other)["outcome"] == "refused"


def test_si_pc05_baselines_are_insert_only(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    for privilege, expected in (("INSERT", True), ("SELECT", True), ("UPDATE", False),
                                ("DELETE", False), ("TRUNCATE", False)):
        found = sa.admin(install, "SELECT has_table_privilege('haloflow_m05_owner', %s, %s)",
                         (f"{install.schema}.m05_writeback_baselines", privilege))
        assert found[0][0] is expected, privilege


def _race(install: si.SIInstall, first: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Two connections on one ticket: `first` (capture | consume) runs and HOLDS its
    transaction open; the other is started on a thread and must block on the ticket row
    lock; then the first commits. Returns (capture result, consume result, op_id)."""

    _p, _ph, ref, _z = si.seed3(install)
    op_id, attempt = sh2.claimed(install, ref)
    capture_cmd = si.cmd(op_id=op_id, attempt_id=attempt)
    results: dict[str, dict[str, Any]] = {}

    def run(name: str) -> None:
        with sa.runtime_conn(install) as conn, conn.transaction():
            if name == "capture":
                results[name] = sa.call_in(conn, install, si.FN["capture"], capture_cmd)
            else:
                results[name] = sa.call_in(conn, install, "m05_outbound_consume",
                                           sh2.consume_cmd(op_id, attempt))

    second = "consume" if first == "capture" else "capture"
    with sa.runtime_conn(install) as conn:
        with conn.transaction():
            if first == "capture":
                results[first] = sa.call_in(conn, install, si.FN["capture"], capture_cmd)
            else:
                results[first] = sa.call_in(conn, install, "m05_outbound_consume",
                                            sh2.consume_cmd(op_id, attempt))
            thread = threading.Thread(target=run, args=(second,))
            thread.start()
            time.sleep(0.5)
            assert thread.is_alive(), "the second transaction must wait on the ticket lock"
        thread.join(30)
        assert not thread.is_alive()
    return results["capture"], results["consume"], op_id


def test_si_pc06a_capture_first_then_consume_both_commit(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    captured, consumed, op_id = _race(install, "capture")
    assert captured["outcome"] == "captured" and consumed["outcome"] == "send"
    row = si.baseline(install, op_id)
    ticket = sh2.ticket_row(install, op_id)
    assert row is not None and ticket is not None
    assert row["attempt_id"] == ticket["attempt_id"] and ticket["consumed_at"] is not None


def test_si_pc06b_consume_first_refuses_the_capture(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    captured, consumed, op_id = _race(install, "consume")
    assert consumed["outcome"] == "send" and captured["outcome"] == "refused"
    assert si.baseline(install, op_id) is None


# --- P-M: the WB2 matrix (decide) ------------------------------------------------------------


def _decided(install: si.SIInstall, read_over: dict[str, Any] | None = None, *,
             status: str = "neutral", **sent_kw: Any) -> tuple[dict[str, Any], dict[str, str],
                                                               str]:
    flow, wb, fence = si.checked(install, **sent_kw)
    read = si.ehr_read(install, flow, status, **(read_over or {}))
    return si.decide(install, wb, fence, read), flow, wb


def _assert_item(install: si.SIInstall, result: dict[str, Any], wb: str, reason: str) -> None:
    """A DECISION (before any permit) that raised the staff item: no permit ever issued."""

    assert (result["outcome"], result["reason"]) == ("item", reason), result
    uuid.UUID(result["event_id"])
    op = si.wb_op(install, wb)
    assert (op["state"], op["reason"], op["event_id"]) == ("item", reason, result["event_id"])
    assert si.permits(install, wb) == []


def _assert_settled_item(install: si.SIInstall, result: dict[str, Any], wb: str, reason: str,
                         spent: dict[str, Any]) -> None:
    """A SETTLEMENT (after the spend) that raised the staff item: the spent permit is KEPT
    with the same identity and timestamps (the no-repeat proof), never deleted."""

    assert (result["outcome"], result["reason"]) == ("item", reason), result
    uuid.UUID(result["event_id"])
    op = si.wb_op(install, wb)
    assert (op["state"], op["reason"], op["event_id"]) == ("item", reason, result["event_id"])
    after = si.permit(install, wb)
    assert after is not None
    for key in ("permit_id", "issued_fence", "apply_deadline", "spent_at", "spent_fence",
                "voided_at", "r1_digest"):
        assert after[key] == spent[key], key


def test_si_pm01_neutral_matching_read_issues_one_permit(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    result, _flow, wb = _decided(install)
    assert result["outcome"] == "permitted", result
    row = si.permit(install, wb)
    assert row is not None and row["permit_id"] == result["permit_id"]
    op = si.wb_op(install, wb)
    assert op["state"] == "permitted" and row["issued_fence"] == op["fence"]
    assert row["spent_at"] is None and row["voided_at"] is None
    assert len(bytes.fromhex(row["r1_digest"].removeprefix("\\x"))) == 32


@pytest.mark.parametrize(("status", "outcome"), [("positive", "skipped"),
                                                 ("checked_in", "skipped"),
                                                 ("checked_out", "skipped")])
def test_si_pm02_04_already_confirmed_or_checked_in_is_a_quiet_skip(
        si_install: si.SIInstall, status: str, outcome: str) -> None:
    install = _ready(si_install)
    result, flow, wb = _decided(install, status=status)
    assert result["outcome"] == outcome, result
    assert si.wb_op(install, wb)["state"] == "skipped"
    assert si.permit(install, wb) is None and si.check_items(install) == []
    assert sa.items_for(install, flow["patient"]) == []


@pytest.mark.parametrize(("status", "over"), [
    ("cancelled", {}),
    ("neutral", {"replaced_by": "appt-new"}),
    ("positive", {"replaced_by": "appt-new"}),       # pin 5: replaced + positive
    ("positive", {"appointment_id": "appt-other"}),  # pin 5: wrong appointment + positive
    ("neutral", {"start_at": "2031-01-01T15:00:00.000000Z"}),
    ("positive", {"start_at": "2031-01-01T15:00:00.000000Z"}),
    ("neutral", {"provider_ref": "prov-other"}),
    ("neutral", {"department_ref": "dept-other"}),
    ("negative", {"replaced_by": "appt-new"}),      # identity before status
], ids=["cancelled", "replaced", "replaced_positive", "wrong_appt_positive", "start_moved",
        "start_moved_positive", "provider_changed", "department_changed",
        "replaced_negative"])
def test_si_pm05_13_identity_and_schedule_changes_are_status_changed(
        si_install: si.SIInstall, status: str, over: dict[str, Any]) -> None:
    install = _ready(si_install)
    result, _flow, wb = _decided(install, over, status=status)
    _assert_item(install, result, wb, "status_changed")


@pytest.mark.parametrize("status", ["negative", "unknown", "empty"])
def test_si_pm14_declined_or_unknown(si_install: si.SIInstall, status: str) -> None:
    install = _ready(si_install)
    result, _flow, wb = _decided(install, status=status)
    _assert_item(install, result, wb, "declined_or_unknown")


@pytest.mark.parametrize(("baseline", "status"), [
    ({"provider_ref": None}, "neutral"), ({"department_ref": None}, "neutral"),
    ({"provider_ref": None}, "positive")])
def test_si_pm15_a_null_identity_baseline_cannot_be_verified(
        si_install: si.SIInstall, baseline: dict[str, Any], status: str) -> None:
    install = _ready(si_install)
    result, _flow, wb = _decided(install, {"provider_ref": None, "department_ref": None},
                                 status=status, **baseline)
    _assert_item(install, result, wb, "could_not_verify")


def test_si_pm16_no_baseline_goes_to_staff_before_any_read_matters(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    result, _flow, wb = _decided(install, capture=False)
    _assert_item(install, result, wb, "no_baseline")


def test_si_pm17_baseline_unknown_goes_to_staff_before_any_write(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    patient, phone, ref, zone = si.seed3(install)
    si.directory3(install, "appointment_put",
                  **si.appointment3(ref, patient, sh.start_of(install, ref),
                                    department_tz=zone, confirmation="unknown"))
    op_id, attempt = sh2.claimed(install, ref)
    assert si.call(install, "capture", op_id=op_id, attempt_id=attempt)["outcome"] == "captured"
    assert sh2.consume(install, op_id, attempt)["outcome"] == "send"
    sh.outcome(install, op_id, attempt, "accepted", provider_ref="fake-si-unknown")
    assert sh.register(install, op_id)["outcome"] == "registered"
    flow = {"op_id": op_id, "attempt": attempt, "ref": ref, "patient": patient, "phone": phone}
    si.replied(install, flow)
    assert si.discover(install) == 1
    wb = str(si.op_for_flow(install, flow)["wb_op_id"])
    fence = int(si.claim(install, wb)["fence"])
    result = si.decide(install, wb, fence, si.ehr_read(install, flow))
    _assert_item(install, result, wb, "declined_or_unknown")


def test_si_pm18_a_repeated_decision_never_mints_a_second_event(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    result, flow, wb = _decided(install, status="negative")
    stored = si.wb_op(install, wb)["event_id"]
    again = si.decide(install, wb, si.wb_op(install, wb)["fence"],
                      si.ehr_read(install, flow, "negative"))
    assert again["outcome"] == "stale" or again.get("event_id") == stored, again
    assert si.wb_op(install, wb)["event_id"] == stored == result["event_id"]


@pytest.mark.parametrize("reply_class", ["reschedule", "cancel", "unclear", "stop"])
def test_si_pm20_persisted_no_stop_or_ambiguous_never_creates_write_back(
        si_install: si.SIInstall, reply_class: str) -> None:
    install = _ready(si_install)
    flow = si.sent(install)
    si.replied(install, flow, reply_class)
    assert si.discover(install) == 0
    assert si.wb_ops(install) == []


def test_si_pm21_discovery_is_idempotent_per_reply(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    flow = si.sent(install)
    si.replied(install, flow)
    assert si.discover(install) == 1
    assert si.discover(install) == 0
    (op,) = si.wb_ops(install)
    assert (op["op_id"], op["state"], op["fence"]) == (flow["op_id"], "pending", 0)


# --- P-Y: retry bookkeeping (owner C5) -------------------------------------------------------


def test_si_py01_r1_failures_defer_1_5_15_minutes_then_raise_ehr_unreachable(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    _flow, wb = si.confirmed_op(install)
    expected = (60, 300, 900)
    for attempt in range(1, 5):
        result = si.claim(install, wb)
        assert result["outcome"] == "claimed", (attempt, result)
        fence = int(result["fence"])
        before = si.db_now(install)
        decided = si.decide(install, wb, fence, None, read_failed=True)
        op = si.wb_op(install, wb)
        if attempt < 4:
            assert decided["outcome"] == "deferred", decided
            assert op["r1_failures"] == attempt
            delay = (datetime.fromisoformat(op["next_at"]) - before).total_seconds()
            assert expected[attempt - 1] - 2 <= delay <= expected[attempt - 1] + 2
            assert si.claim(install, wb)["outcome"] == "not_due"
            si.age(install, wb, lease=si.LEASE_S + 1, next_at=expected[attempt - 1] + 1)
        else:
            _assert_item(install, decided, wb, "ehr_unreachable")


# --- P-S: spend --------------------------------------------------------------------------------


def test_si_ps01_one_spend_per_permit_with_a_database_deadline(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    _flow, wb, fence, permit = si.permitted(install)
    before = si.db_now(install)
    spent = si.spend(install, wb, fence, permit)
    after = si.db_now(install)
    assert spent["outcome"] == "spent", spent
    deadline = datetime.fromisoformat(spent["apply_deadline"])
    assert before + timedelta(seconds=si.WRITE_TIMEOUT_S) <= deadline
    assert deadline <= after + timedelta(seconds=si.WRITE_TIMEOUT_S)
    assert si.spend(install, wb, fence, permit)["outcome"] == "refused"
    row = si.permit(install, wb)
    assert row is not None and row["spent_fence"] == fence
    assert si.wb_op(install, wb)["state"] == "spent"


@pytest.mark.parametrize("delta", [-1, 1])
def test_si_ps02_a_stale_or_future_fence_cannot_spend(si_install: si.SIInstall,
                                                       delta: int) -> None:
    install = _ready(si_install)
    _flow, wb, fence, permit = si.permitted(install)
    assert si.spend(install, wb, fence + delta, permit)["outcome"] == "refused"
    row = si.permit(install, wb)
    assert row is not None and row["spent_at"] is None


def test_si_ps03_lease_expired_before_spend_then_the_next_claim_voids(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    _flow, wb, fence, permit = si.permitted(install)
    si.age(install, wb, lease=si.LEASE_S + 1)
    assert si.spend(install, wb, fence, permit)["outcome"] == "refused"
    again = si.claim(install, wb)
    assert again["outcome"] == "claimed" and int(again["fence"]) == fence + 1, again
    assert si.permit(install, wb) is None  # no live permit after the void
    (old,) = si.permits(install, wb)
    assert (old["permit_id"], old["spent_at"]) == (permit, None) and old["voided_at"]
    assert si.wb_op(install, wb)["state"] == "eligible_check"


def test_si_ps04_a_voided_permit_is_never_spendable(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    _flow, wb, fence, permit = si.permitted(install)
    si.age(install, wb, lease=si.LEASE_S + 1)
    new_fence = int(si.claim(install, wb)["fence"])
    assert si.spend(install, wb, new_fence, permit)["outcome"] == "refused"
    assert si.spend(install, wb, fence, permit)["outcome"] == "refused"
    (old,) = si.permits(install, wb)
    assert old["spent_at"] is None and old["voided_at"] is not None


def test_si_ps05_an_op_that_is_not_permitted_cannot_spend(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    result, _flow, wb = _decided(install, status="negative")
    assert result["outcome"] == "item"
    fence = si.wb_op(install, wb)["fence"]
    assert si.spend(install, wb, fence, si.new_uuid())["outcome"] == "refused"


def test_si_ps06_an_unspent_permit_whose_lease_lapsed_is_recovered_through_due(
        si_install: si.SIInstall) -> None:
    """due -> claim (voids the old permit) -> a FRESH decide -> a NEW permit -> spend. The
    old token stays refused for ever; the op is never stuck and no old token returns."""

    install = _ready(si_install)
    flow, wb, fence, old_permit = si.permitted(install)
    assert si.due(install, wb) == []  # still leased
    si.age(install, wb, lease=si.LEASE_S + 1)
    assert si.due(install, wb) == ["check"]
    claimed = si.claim(install, wb)
    assert (claimed["outcome"], int(claimed["fence"])) == ("claimed", fence + 1), claimed
    new_fence = int(claimed["fence"])
    fresh = si.decide(install, wb, new_fence, si.ehr_read(install, flow))
    assert fresh["outcome"] == "permitted" and fresh["permit_id"] != old_permit, fresh
    assert si.spend(install, wb, new_fence, old_permit)["outcome"] == "refused"
    spent = si.spend(install, wb, new_fence, fresh["permit_id"])
    assert spent["outcome"] == "spent", spent
    history = si.permits(install, wb)
    assert [p["permit_id"] for p in history] == [old_permit, fresh["permit_id"]]
    assert history[0]["voided_at"] is not None and history[0]["spent_at"] is None
    assert history[1]["voided_at"] is None and history[1]["spent_fence"] == new_fence
    assert si.spend(install, wb, fence, old_permit)["outcome"] == "refused"


# --- P-K: a spent, unsettled permit is reconcile-only ------------------------------------------


def test_si_pk01_reconcile_only_after_the_deadline_plus_margin(si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    _flow, wb, fence, permit = si.permitted(install)
    assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
    si.age(install, wb, lease=si.LEASE_S + 1)  # the lease lapsed; deadline + margin has not
    row = si.permit(install, wb)
    assert row is not None
    horizon = datetime.fromisoformat(row["apply_deadline"]) + timedelta(
        seconds=si.RECONCILE_MARGIN_S)
    assert horizon > si.db_now(install)  # explicit: still before the reconcile horizon
    assert si.due(install, wb) == []
    assert si.claim(install, wb)["outcome"] == "not_claimable"
    early = si.call(install, "claim_reconcile", wb_op_id=wb, lease_seconds=si.LEASE_S)
    assert early["outcome"] == "not_claimable", early
    si.age(install, wb, deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
    assert si.due(install, wb) == ["reconcile"]
    assert si.claim(install, wb)["outcome"] == "not_claimable"  # never a second check/write
    late = si.call(install, "claim_reconcile", wb_op_id=wb, lease_seconds=si.LEASE_S)
    assert (late["outcome"], late["phase"], late["permit_id"]) == ("claimed", "reconcile",
                                                                   permit)
    row = si.permit(install, wb)
    assert row is not None and row["closed_at"] is not None


# --- P-R: settle ---------------------------------------------------------------------------------


def _spent(install: si.SIInstall) -> tuple[dict[str, str], str, int, str]:
    flow, wb, fence, permit = si.permitted(install)
    assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
    return flow, wb, fence, permit


def _spent_row(install: si.SIInstall, wb: str) -> dict[str, Any]:
    row = si.permit(install, wb)
    assert row is not None and row["spent_at"] is not None
    return row


def test_si_pr01_positive_fully_matching_read_is_observed_positive(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    flow, wb, fence, permit = _spent(install)
    result = si.settle(install, wb, fence, permit, "applied", si.ehr_read(install, flow,
                                                                          "positive"))
    assert result["outcome"] == "observed_positive", result
    assert si.wb_op(install, wb)["state"] == "observed_positive"
    assert si.check_items(install) == [] and si.wb_op(install, wb)["event_id"] is None


@pytest.mark.parametrize("over", [{"replaced_by": "appt-new"},
                                  {"appointment_id": "appt-other"},
                                  {"provider_ref": "prov-other"}])
def test_si_pr02_positive_but_changed_is_never_observed_positive(
        si_install: si.SIInstall, over: dict[str, Any]) -> None:
    install = _ready(si_install)
    flow, wb, fence, permit = _spent(install)
    spent = _spent_row(install, wb)
    result = si.settle(install, wb, fence, permit, "applied",
                       si.ehr_read(install, flow, "positive", **over))
    _assert_settled_item(install, result, wb, "status_changed", spent)


@pytest.mark.parametrize(("write_outcome", "read_status"), [("unknown", None),
                                                            ("applied", "neutral"),
                                                            ("deadline_passed", "neutral")])
def test_si_pr03_no_positive_read_could_not_verify(si_install: si.SIInstall,
                                                   write_outcome: str,
                                                   read_status: str | None) -> None:
    install = _ready(si_install)
    flow, wb, fence, permit = _spent(install)
    spent = _spent_row(install, wb)
    read = None if read_status is None else si.ehr_read(install, flow, read_status)
    result = si.settle(install, wb, fence, permit, write_outcome, read)
    _assert_settled_item(install, result, wb, "could_not_verify", spent)


def test_si_pr04_a_stale_writer_cannot_settle_over_the_reconciler(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    flow, wb, fence, permit = _spent(install)
    si.age(install, wb, lease=si.LEASE_S + 1,
           deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
    reconcile = si.call(install, "claim_reconcile", wb_op_id=wb, lease_seconds=si.LEASE_S)
    assert reconcile["outcome"] == "claimed"
    b_fence = int(reconcile["fence"])
    b = si.settle(install, wb, b_fence, permit, "unknown", si.ehr_read(install, flow))
    assert b["outcome"] == "item"
    state = si.wb_op(install, wb)
    a = si.settle(install, wb, fence, permit, "applied", si.ehr_read(install, flow, "positive"))
    assert a["outcome"] == "stale", a
    assert si.wb_op(install, wb) == state


# --- P-L06: database wall time ---------------------------------------------------------------


def test_si_pl06_guards_use_clock_timestamp_not_the_transaction_start(
        si_install: si.SIInstall) -> None:
    """One open transaction starts BEFORE the reconcile horizon; the call is made after the
    horizon has passed in wall time. It is claimed only if the guard reads
    clock_timestamp(); now() (the transaction start) is still before the horizon. Both
    orderings are asserted explicitly: tx start < horizon < call time."""

    install = _ready(si_install)
    _flow, wb, _fence, _permit = _spent(install)
    si.age(install, wb, lease=si.LEASE_S + 1)
    with sa.runtime_conn(install) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant_id,))
        tx_start = conn.execute("SELECT now()").fetchone()[0]  # type: ignore[index]
        horizon = tx_start + timedelta(seconds=1.5)
        si.set_deadline(install, wb, horizon - timedelta(seconds=si.RECONCILE_MARGIN_S))
        time.sleep(2.5)
        wall, tx_now = conn.execute("SELECT clock_timestamp(), now()").fetchone()  # type: ignore[misc]
        assert tx_now == tx_start < horizon < wall
        result = sa.call_in(conn, install, si.FN["claim_reconcile"],
                            si.cmd(wb_op_id=wb, lease_seconds=si.LEASE_S))
    assert result["outcome"] == "claimed", result


# --- P-G: ACL ---------------------------------------------------------------------------------


def test_si_pg01_runtime_executes_every_si_function_and_touches_no_si_table(
        si_install: si.SIInstall) -> None:
    install = _ready(si_install)
    for function in si.FN.values():
        assert si.execute_privilege(install, function), function
    for table in si.TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            found = sa.admin(install, "SELECT has_table_privilege(%s, %s, %s)",
                             (sa.RUNTIME, f"{install.schema}.{table}", privilege))
            assert found[0][0] is False, (table, privilege)


def test_si_pg02_each_rebound_key_has_exactly_one_binding_to_the_si_function() -> None:
    """The real S-I TEST catalogue builds (no duplicate key from any contributing part)
    and each rebound key resolves to its S-I function; nothing else binds those
    functions' predecessors."""

    from haloflow.m05.writeback_statements import SI_REBOUND

    catalog = si.si_catalog().catalog
    expected = {"m05.intake.directory": "m05_directory_v3",
                "m05.followup.create": "m05_create_item_v2",
                "m05.followup.prepare": "m05_prepare_action_v3",
                "m05.followup.apply": "m05_apply_action_v3"}
    assert set(SI_REBOUND) == set(expected)
    registered = catalog.keys()  # a tuple of every registered key
    for key, function in expected.items():
        assert registered.count(key) == 1
        assert f"{function}(" in catalog.resolve(key).query, key
    for key in registered:
        query = catalog.resolve(key).query
        for closed in si.CLOSED_FUNCTIONS:
            assert f"{closed}(" not in query, (key, closed)
