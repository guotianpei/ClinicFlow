"""Eligibility E2 — PostgreSQL 17 rows (RED BY DESIGN).

Each row provisions a fresh synthetic tenant at target 117 in its body, so the intended red
cause is the missing E2 source: `build_synthetic_registry(include_el2=...)` does not exist
(TypeError at provisioning, reported as "E2 test installation failed"), or
ModuleNotFoundError for an E2 module imported first. Rows marked CONTROL use only existing
code and must pass now. Interface pins: the el2_support docstring.

Oracles stop at the durable case-event outbox (v1r1 T3): verifications, triggers, attempts
(dispatch SLOTS), late answers and `m05_el_case_events` rows - never items or notes (E3).
Port CALLS are counted where a port is used (C2: slots vs calls).

Rows (stage-3 ids; trace in brackets):
- E2-C00 CONTROL: the E1 tick on an E1 install.
- E2-P01 W1 booked far ahead, all active: 3 verifications, 3 slots, 3 events [W1]
- E2-P02 W2 booked inside the window: booking satisfies previsit; 2 verifications [W2]
- E2-P03 W3 booking two timeouts -> inconclusive/payer_unavailable, later active [W3]
- E2-P04 W5 same day: one verification serves three triggers [W5]
- E2-P05 W6 midnight: previsit attempt 2 observed 00:01 -> day-of skipped_fresh [W6, M1]
- E2-P06 W7 attached day-of, two timeouts: all terminal inconclusive, no third slot [W7, M1]
- E2-P07 W8 attached, observed 23:59:30: day-of returned once, owns 1 verification [W8, C2]
- E2-P08 bound: adversarial schedule per coverage version <= 3 verifications, <= 6 slots [C2]
- E2-P09 revalidation at dispatch: coverage / cancelled / activation off [R2]
- E2-P10 revalidation at settlement: same three; activation recovery settles from the stored
  answer without a new slot [R2, R1]
- E2-C01 (v1r1) the REAL worker crashes inside attempt 1 before the port: the recording
  port sees attempt 1 committed `dispatched` at entry (separate connection); takeover ->
  ambiguous, never re-sent; attempt 2 (new id) answers inactive; 2 slots, 1 call, 1 event
  [R1, C2, Codex finding 1]
- E2-C02 (v1r1) the same, crashing AFTER the port answered attempt 1: 2 slots, 2 calls with
  distinct ids, inactive, 1 event; attempt 1's late answer changes nothing [R1, W4]
- E2-C03 crash after the answer is recorded: takeover settles from it, no new slot [R1]
- E2-C04 old worker after takeover: dispatch / timeout / settle fenced, answer late [R1]
- E2-C05 takeover before lease + margin refused; seam exact instants [R1]
- E2-C06 two concurrent claimers: exactly one wins, epoch +1 once [R1]
- E2-P11 late answers: unknown attempt / closed attempt / superseded / stale epoch [EL-6c/e]
- E2-P12 echo mismatch on each correlation field -> late, nothing cleared [EL-6c/e]
- E2-P13 expiry: identifiers, benefit fields, txn ref and digest gone; class/reason/times
  kept; idempotent; not before; seam exact [R4, EL-14a]
- E2-P14 audit/privacy: column allowlists; sentinels only in coverage (and the dispatch
  request); (v1r1) digest stable per appointment+member and different across appointments
  and members; one case event per settlement carrying its verification id, unchanged by a
  replayed settle (Codex D-3) [R4, EL-14]
- E2-P16 retry delay: seam exact; live not-before/after; never a third attempt [EL-9]
- E2-P17 no retry on non-transient results: exactly one slot [EL-9]
- E2-P18 freshness against stored rows: each key component altered alone -> stale [EL-5]
- E2-P19 due: result shape, per-appointment order, limit [R2]
- E2-P20 malformed commands / wrong workload refused, nothing written; (v1r1) malformed
  answers derived from an accepted control by ONE field, after which the valid answer is
  still recorded [§6a, Codex finding 3]
- E2-P21 privileges: runtime EXECUTE on the seven gateways only; seams owner-only IMMUTABLE;
  no runtime table privilege; catalogue keys [R4, R7]
- E2-P22 routed: EligibilityService + EligibilityWorker + SimulatedClearinghouse through the
  real resolver and gateway run W1's booking check end to end [EL-6, A8]

Mutant killers (wired at green): M01 freshness on settled_at -> P05/P18(observed); M02
attached returns twice -> P07; M03 third slot allowed -> P08/P16; M04 resend the same
attempt id after a crash -> C01/C02 (+W03 unit); M05 settle ignores epoch -> C04; M06 late
answer applied -> P11/P12/C02; M07 expiry keeps the digest -> P13; M08 inconclusive mapped to
inactive -> U02/P17.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import el1_support as el
import el2_support as e2
import m02_support
import psycopg
import pytest
import sa_support as sa

pytestmark = pytest.mark.postgres

OK = {"schema_version": 1, "outcome": "settled", "class": "active", "reason": "coverage_active"}
RECORDED = {"schema_version": 1, "outcome": "recorded"}


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[e2.EL2Install]:
    yield from e2.module_install(request, m02_ids, m05_reset, f"el2:{request.node.name}")


@pytest.fixture
def el1_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
                m05_reset: Any) -> Iterator[el.EL1Install]:
    yield from el.module_install(request, m02_ids, m05_reset, f"el2-e1:{request.node.name}")


def _now() -> datetime:
    return datetime.now(UTC)


def _settled(cls: str, reason: str) -> dict[str, Any]:
    return {"schema_version": 1, "outcome": "settled", "class": cls, "reason": reason}


def _out(result: dict[str, Any]) -> str:
    return str(result["outcome"])


# --- E2-C00 CONTROL ----------------------------------------------------------------------

def test_el2_c00_control_e1_tick(el1_install: el.EL1Install) -> None:
    inst = el.ready(el1_install)
    got = el.tick(inst)
    assert got["outcome"] == "applied" and got["raised"] == 0


# --- E2-P01 W1 ---------------------------------------------------------------------------

def test_el2_p01_w1_booked_far_ahead_all_active(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, _patient = e2.enrol(inst, start)
    ws = e2.window_starts(start)
    got = e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    assert got == {"schema_version": 1, "outcome": "applied", "created": 1, "attached": 0,
                   "skipped_fresh": 0, "verifications": [vb]}
    assert e2.run(inst, vb, [e2.active_result(_now())])["settle"] == OK
    trig = e2.triggers(inst, ref)
    assert trig["booking"] == ("satisfied", vb, False)
    assert trig["previsit"][0] == trig["dayof"][0] == "pending"
    e2.make_due(inst, ref, "previsit")
    assert e2.due(inst)["created"] == 1          # the booking observation is before the window
    vp = e2.own_verification(inst, ref, "previsit")
    assert e2.run(inst, vp, [e2.active_result(ws["previsit"] + timedelta(minutes=1))])[
        "settle"] == OK
    e2.make_due(inst, ref, "dayof")
    assert e2.due(inst)["created"] == 1
    vd = e2.own_verification(inst, ref, "dayof")
    assert e2.run(inst, vd, [e2.active_result(ws["dayof"] + timedelta(hours=1))])[
        "settle"] == OK
    assert [v[1:] for v in e2.verifications(inst, ref)] == [
        ("booking", "settled", "active", "coverage_active"),
        ("previsit", "settled", "active", "coverage_active"),
        ("dayof", "settled", "active", "coverage_active")]
    assert e2.slots(inst, ref) == 3
    assert e2.events(inst, ref) == [(i, 1, "open_or_note", "active", "coverage_active")
                                    for i in (1, 2, 3)]
    assert {k: v[0] for k, v in e2.triggers(inst, ref).items()} == dict.fromkeys(
        ("booking", "previsit", "dayof"), "satisfied")


# --- E2-P02 W2 ---------------------------------------------------------------------------

def test_el2_p02_w2_booked_inside_window(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = e2.future(2)                            # previsit instant is already past
    ref, _p = e2.enrol(inst, start)
    got = e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    assert (got["created"], got["attached"], got["verifications"]) == (1, 1, [vb])
    assert e2.triggers(inst, ref)["previsit"] == ("assigned", vb, False)
    assert e2.run(inst, vb, [e2.active_result(_now())])["settle"] == OK
    assert e2.triggers(inst, ref)["previsit"] == ("satisfied", vb, False)
    e2.make_due(inst, ref, "dayof")
    e2.due(inst)
    vd = e2.own_verification(inst, ref, "dayof")
    e2.run(inst, vd, [e2.active_result(e2.window_starts(start)["dayof"]
                                       + timedelta(hours=1))])
    assert len(e2.verifications(inst, ref)) == 2
    assert e2.slots(inst, ref) == 2
    assert [e[3] for e in e2.events(inst, ref)] == ["active", "active"]


# --- E2-P03 W3 ---------------------------------------------------------------------------

def test_el2_p03_w3_two_timeouts_then_later_active(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, _p = e2.enrol(inst, start)
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    got = e2.run(inst, vb, ["timeout", "timeout"])
    assert got["settle"] == _settled("inconclusive", "payer_unavailable")
    assert [a[1:] for a in e2.attempts(inst, vb)] == [(1, "timed_out"), (2, "timed_out")]
    e2.make_due(inst, ref, "previsit")
    assert e2.due(inst)["skipped_fresh"] == 0
    vp = e2.own_verification(inst, ref, "previsit")
    e2.run(inst, vp, [e2.active_result(e2.window_starts(start)["previsit"]
                                       + timedelta(minutes=1))])
    assert e2.slots(inst, ref) == 3
    assert e2.events(inst, ref) == [
        (1, 1, "open_or_note", "inconclusive", "payer_unavailable"),
        (2, 1, "open_or_note", "active", "coverage_active")]


# --- E2-P04 W5 ---------------------------------------------------------------------------

def test_el2_p04_w5_same_day_one_verification_serves_three(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = _now().replace(microsecond=0) + timedelta(hours=2)
    ref, _p = e2.enrol(inst, start)
    e2.make_due(inst, ref, "previsit", "dayof")
    got = e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    assert (got["created"], got["attached"], got["verifications"]) == (1, 2, [vb])
    ws = e2.window_starts(start)
    observed = max(ws.values()) + timedelta(minutes=1)
    assert e2.run(inst, vb, [e2.active_result(observed)])["settle"] == OK
    assert e2.triggers(inst, ref) == {t: ("satisfied", vb, False)
                                      for t in ("booking", "previsit", "dayof")}
    assert (len(e2.verifications(inst, ref)), e2.slots(inst, ref)) == (1, 1)
    assert len(e2.events(inst, ref)) == 1


# --- E2-P05 W6 midnight -----------------------------------------------------------------

def test_el2_p05_w6_attempt_two_after_midnight_makes_dayof_fresh(
        install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, _p = e2.enrol(inst, start)
    ws = e2.window_starts(start)
    e2.due(inst)
    e2.run(inst, e2.own_verification(inst, ref, "booking"), [e2.active_result(_now())])
    e2.make_due(inst, ref, "previsit")
    e2.due(inst)
    vp = e2.own_verification(inst, ref, "previsit")
    got = e2.run(inst, vp, ["timeout", e2.active_result(ws["dayof"] + timedelta(minutes=1))])
    assert got["settle"] == OK
    e2.make_due(inst, ref, "dayof")
    got = e2.due(inst)
    assert (got["created"], got["skipped_fresh"]) == (0, 1)
    assert e2.triggers(inst, ref)["dayof"][0] == "skipped_fresh"
    assert len(e2.verifications(inst, ref)) == 2
    assert e2.slots(inst, ref) == 3


# --- E2-P06 W7 ---------------------------------------------------------------------------

def test_el2_p06_w7_attached_dayof_two_timeouts_no_third_slot(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = _now().replace(microsecond=0) + timedelta(minutes=90)
    ref, _p = e2.enrol(inst, start)
    e2.make_due(inst, ref, "previsit", "dayof")
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    got = e2.run(inst, vb, ["timeout", "timeout"])
    assert got["settle"] == _settled("inconclusive", "payer_unavailable")
    assert e2.triggers(inst, ref) == {t: ("satisfied", vb, False)
                                      for t in ("booking", "previsit", "dayof")}
    assert e2.due(inst)["created"] == 0
    assert e2.claim(inst, vb)["outcome"] == "not_claimable"
    assert e2.slots(inst, ref) == 2


# --- E2-P07 W8 ---------------------------------------------------------------------------

def test_el2_p07_w8_returned_once_then_owns_one(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, _p = e2.enrol(inst, start)
    ws = e2.window_starts(start)
    e2.make_due(inst, ref, "dayof")
    got = e2.due(inst)
    assert (got["created"], got["attached"]) == (1, 1)
    vb = e2.own_verification(inst, ref, "booking")
    e2.run(inst, vb, [e2.active_result(ws["dayof"] - timedelta(seconds=30))])
    assert e2.triggers(inst, ref)["booking"] == ("satisfied", vb, False)
    assert e2.triggers(inst, ref)["dayof"] == ("pending", None, True)
    got = e2.due(inst)
    assert (got["created"], got["attached"], got["skipped_fresh"]) == (1, 0, 0)
    vd = e2.own_verification(inst, ref, "dayof")
    e2.run(inst, vd, [e2.active_result(ws["dayof"] + timedelta(hours=7))])
    assert e2.triggers(inst, ref)["dayof"] == ("satisfied", vd, True)
    assert e2.due(inst)["created"] == 0
    assert [v[1] for v in e2.verifications(inst, ref)] == ["booking", "dayof"]


def test_el2_p07b_returned_trigger_owns_never_reattaches(install: e2.EL2Install) -> None:
    """Added at green (disclosed): after its return, the day-of trigger owns its next
    verification even when another unsettled verification exists (M1: returned once)."""
    inst = e2.ready(install)
    start = e2.future(10)
    ref, _p = e2.enrol(inst, start)
    ws = e2.window_starts(start)
    e2.make_due(inst, ref, "dayof")
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    # observed before BOTH windows: previsit is not fresh and owns a new verification
    e2.run(inst, vb, [e2.active_result(ws["previsit"] - timedelta(seconds=60))])
    assert e2.triggers(inst, ref)["dayof"] == ("pending", None, True)
    e2.make_due(inst, ref, "previsit")
    got = e2.due(inst)
    assert (got["created"], got["attached"]) == (2, 0)
    vp = e2.own_verification(inst, ref, "previsit")
    vd = e2.own_verification(inst, ref, "dayof")
    assert vp != vd
    assert e2.triggers(inst, ref)["dayof"] == ("assigned", vd, True)


# --- E2-P08 bound --------------------------------------------------------------------------

def test_el2_p08_bound_three_verifications_six_slots(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, _p = e2.enrol(inst, start)
    epochs: dict[str, int] = {}
    for trigger in ("booking", "previsit", "dayof"):
        e2.make_due(inst, ref, trigger)
        e2.due(inst)
        vid = e2.own_verification(inst, ref, trigger)
        got = e2.run(inst, vid, ["timeout", "timeout"])
        assert got["settle"] == _settled("inconclusive", "payer_unavailable")
        epochs[vid] = got["epoch"]
    e2.make_due(inst, ref, "booking", "previsit", "dayof")
    again = e2.due(inst)
    assert (again["created"], again["attached"], again["verifications"]) == (0, 0, [])
    for vid, epoch in epochs.items():
        assert e2.claim(inst, vid)["outcome"] == "not_claimable"
        assert e2.dispatch(inst, vid, epoch)["outcome"] != "dispatched"
    assert (len(e2.verifications(inst, ref)), e2.slots(inst, ref)) == (3, 6)
    assert e2.q(inst, "SELECT max(n) FROM {s}.m05_el_attempts") == [(2,)]


# --- E2-P09 / P10 revalidation ------------------------------------------------------------

def _change(inst: e2.EL2Install, ref: str, patient: str, start: datetime, cause: str) -> None:
    if cause == "coverage":
        el.put(inst, ref, patient, start, coverage=el.present(member_id="M000777"))
    elif cause == "cancelled":
        el.put(inst, ref, patient, start, coverage=el.present(), status="cancelled")
    else:
        el.el_settings(inst, enabled=False, lead_days=3, not_received_days=1,
                       priority=e2.PRIORITY)


def _enable(inst: e2.EL2Install) -> None:
    el.el_settings(inst, enabled=True, lead_days=3, not_received_days=1, priority=e2.PRIORITY)


EXPECTED = {"coverage": "superseded", "cancelled": "cancelled", "activation": "activation_off"}


@pytest.mark.parametrize("cause", ["coverage", "cancelled", "activation"])
def test_el2_p09_revalidation_at_dispatch(install: e2.EL2Install, cause: str) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, patient = e2.enrol(inst, start)
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    epoch = e2.claim(inst, vb)["epoch"]
    _change(inst, ref, patient, start, cause)
    assert _out(e2.dispatch(inst, vb, epoch)) == EXPECTED[cause]
    assert e2.slots(inst, ref) == 0
    state = e2.q(inst, "SELECT state, result_class FROM {s}.m05_el_verifications"
                 " WHERE verification_id = %s", (vb,))
    assert state == [({"coverage": "superseded", "cancelled": "cancelled",
                       "activation": "due"}[cause], None)]
    if cause == "activation":
        _enable(inst)
        assert e2.run(inst, vb, [e2.active_result(_now())])["settle"] == OK
        assert e2.slots(inst, ref) == 1


@pytest.mark.parametrize("cause", ["coverage", "cancelled", "activation"])
def test_el2_p10_revalidation_at_settlement(install: e2.EL2Install, cause: str) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, patient = e2.enrol(inst, start)
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    epoch = e2.claim(inst, vb)["epoch"]
    sent = e2.dispatch(inst, vb, epoch)
    assert e2.record(inst, sent["attempt_id"], epoch, e2.echo_of(sent),
                     e2.result_of("inactive", "coverage_inactive", _now())) == RECORDED
    before_events = e2.events(inst, ref)
    _change(inst, ref, patient, start, cause)
    assert _out(e2.settle(inst, vb, epoch)) == EXPECTED[cause]
    assert e2.q(inst, "SELECT result_class FROM {s}.m05_el_verifications"
                " WHERE verification_id = %s", (vb,)) == [(None,)]
    assert [e for e in e2.events(inst, ref) if e[3] is not None] == [
        e for e in before_events if e[3] is not None]
    if cause == "activation":
        _enable(inst)
        claimed = e2.claim(inst, vb)
        assert claimed["outcome"] == "claimed"
        assert _out(e2.dispatch(inst, vb, claimed["epoch"])) == "answered"
        assert e2.settle(inst, vb, claimed["epoch"]) == _settled("inactive", "coverage_inactive")
        assert e2.slots(inst, ref) == 1


# --- E2-C01..C06 crash, takeover, fencing --------------------------------------------------

def _claimed_dispatch(inst: e2.EL2Install) -> tuple[str, str, int, dict[str, Any]]:
    ref, _p = e2.enrol(inst, e2.future(10))
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    epoch = e2.claim(inst, vb)["epoch"]
    sent = e2.dispatch(inst, vb, epoch)
    assert sent["outcome"] == "dispatched" and sent["n"] == 1
    return ref, vb, epoch, sent


def _takeover(inst: e2.EL2Install, vb: str) -> dict[str, Any]:
    e2.age_lease(inst, vb, -(e2.TAKEOVER_MARGIN_S + 1))
    assert vb in e2.due(inst)["verifications"]
    got = e2.claim(inst, vb)
    assert got["outcome"] == "claimed" and got["takeover"] is True
    return got


async def _crash_then_recover(inst: e2.EL2Install, *, crash_before: int,
                              crash_after: int) -> tuple[Any, str, str]:
    """The REAL worker at a crash boundary, then takeover and recovery (Codex finding 1).

    Run 1 dies inside attempt 1's port call (before the inner port, or after it answered).
    The lease is aged past takeover; run 2 takes over (attempt 1 becomes ambiguous) and
    stops at the retry delay; the delay is aged; run 3 dispatches attempt 2, whose answer
    is `inactive`. Every count below comes from the recording port, never from the test."""
    from haloflow.m05.eligibility import EligibilityWorker
    from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

    member = el.present()["member_id"]
    inner = SimulatedClearinghouse(clock=_now)
    inner.set_member(member, "inactive")
    port = e2.RecordingPort(inst, inner, crash_before=crash_before, crash_after=crash_after)
    ref, _p = e2.enrol(inst, e2.future(10))
    async with e2.worker_runtime(inst) as (service, context):
        worker = EligibilityWorker(service, port, call_timeout_s=5.0)
        ids = await service.due(await context(), 100)
        vb = e2.own_verification(inst, ref, "booking")
        assert ids == [vb]
        with pytest.raises(e2.Crash):
            await worker.run_one(await context(), vb)
        first = port.entries[0]
        assert e2.attempts(inst, vb) == [(first, 1, "dispatched")]
        e2.age_lease(inst, vb, -(e2.TAKEOVER_MARGIN_S + 1))
        assert await service.due(await context(), 100) == [vb]
        assert await worker.run_one(await context(), vb) == "retry_pending"
        assert e2.attempts(inst, vb) == [(first, 1, "ambiguous")]
        assert len(port.entries) == 1                    # takeover never re-sent attempt 1
        e2.age_failure(inst, first, -(e2.RETRY_DELAY_S + 1))
        assert await worker.run_one(await context(), vb) == "settled"
    return port, ref, vb


def test_el2_c01_crash_after_dispatch_commit_before_port_call(
        install: e2.EL2Install) -> None:
    # Green deviation D-G3: a sync row (provisioning runs its own event loop) driving the
    # async worker through asyncio.run; oracles unchanged.
    inst = e2.ready(install)
    port, ref, vb = asyncio.run(_crash_then_recover(inst, crash_before=1, crash_after=0))
    first, second = port.entries
    assert first != second
    assert port.committed_at_entry == ["dispatched", "dispatched"]   # intent before send
    assert port.calls == [second]                    # attempt 1 never reached the port
    assert e2.attempts(inst, vb) == [(first, 1, "ambiguous"), (second, 2, "answered")]
    assert (e2.slots(inst, ref), len(port.calls)) == (2, 1)          # C2: slots vs calls
    assert e2.q(inst, "SELECT state, result_class, reason FROM {s}.m05_el_verifications"
                " WHERE verification_id = %s", (vb,)) == [
        ("settled", "inactive", "coverage_inactive")]
    assert e2.events(inst, ref) == [(1, 1, "open_or_note", "inactive", "coverage_inactive")]


def test_el2_c02_crash_after_port_call_attempt_two_inactive(
        install: e2.EL2Install) -> None:
    inst = e2.ready(install)                                  # D-G3: sync row
    port, ref, vb = asyncio.run(_crash_then_recover(inst, crash_before=0, crash_after=1))
    first, second = port.entries
    assert first != second
    assert port.committed_at_entry == ["dispatched", "dispatched"]
    assert port.calls == [first, second]             # attempt 1 reached the port; never resent
    assert e2.attempts(inst, vb) == [(first, 1, "ambiguous"), (second, 2, "answered")]
    assert (e2.slots(inst, ref), len(port.calls)) == (2, 2)
    assert e2.events(inst, ref) == [(1, 1, "open_or_note", "inactive", "coverage_inactive")]
    # attempt 1's real answer arrives late: recorded as late, changes nothing
    echo = {"attempt_id": first, "tenant_id": inst.tenant_id, "appointment_ref": ref,
            "patient_id": e2.q(inst, "SELECT patient_id::text FROM {s}.m05_el_appointments"
                               " WHERE appointment_ref = %s", (ref,))[0][0],
            "member_id": el.present()["member_id"], "coverage_version": 1}
    late = e2.record(inst, first, 1, echo, e2.active_result(_now()))
    assert late == {"schema_version": 1, "outcome": "late", "cause": "closed_attempt"}
    assert e2.q(inst, "SELECT result_class FROM {s}.m05_el_verifications"
                " WHERE verification_id = %s", (vb,)) == [("inactive",)]
    assert len(e2.events(inst, ref)) == 1


def test_el2_c03_crash_after_answer_settles_from_the_stored_answer(
        install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    ref, vb, epoch, sent = _claimed_dispatch(inst)
    assert e2.record(inst, sent["attempt_id"], epoch, e2.echo_of(sent),
                     e2.active_result(_now())) == RECORDED
    taken = _takeover(inst, vb)
    assert e2.attempts(inst, vb) == [(sent["attempt_id"], 1, "answered")]
    assert _out(e2.dispatch(inst, vb, taken["epoch"])) == "answered"
    assert e2.settle(inst, vb, taken["epoch"]) == OK
    assert e2.slots(inst, ref) == 1


def test_el2_c04_old_worker_is_fenced_after_takeover(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    ref, vb, epoch, sent = _claimed_dispatch(inst)
    _takeover(inst, vb)
    before = e2.table_text(inst)
    assert _out(e2.dispatch(inst, vb, epoch)) == "fenced"
    assert _out(e2.timeout(inst, sent["attempt_id"], epoch)) == "fenced"
    assert _out(e2.settle(inst, vb, epoch)) == "fenced"
    assert _out(e2.record(inst, sent["attempt_id"], epoch, e2.echo_of(sent),
                          e2.active_result(_now()))) == "late"
    after = e2.table_text(inst)
    assert {k: v for k, v in after.items() if k != "m05_el_late_answers"} == {
        k: v for k, v in before.items() if k != "m05_el_late_answers"}
    assert len(after["m05_el_late_answers"]) == len(before["m05_el_late_answers"]) + 1


def test_el2_c05_no_takeover_before_lease_plus_margin(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    _ref, vb, _epoch, sent = _claimed_dispatch(inst)
    e2.age_lease(inst, vb, -(e2.TAKEOVER_MARGIN_S - 1))
    assert vb not in e2.due(inst)["verifications"]
    assert e2.claim(inst, vb) == {"schema_version": 1, "outcome": "not_claimable"}
    assert e2.attempts(inst, vb) == [(sent["attempt_id"], 1, "dispatched")]


@pytest.mark.parametrize(("delta", "expected"), [
    (timedelta(seconds=29, microseconds=999999), False), (timedelta(seconds=30), True),
    (timedelta(seconds=31), True)])
def test_el2_c05_takeover_seam_exact(install: e2.EL2Install, delta: timedelta,
                                     expected: bool) -> None:
    inst = e2.ready(install)
    lease = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    assert e2.q(inst, "SELECT {s}.m05_el_takeover_ready(%s, %s)", (lease, lease + delta)) == [
        (expected,)]


def test_el2_c06_two_claimers_exactly_one_wins(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    _ref, vb, epoch, _sent = _claimed_dispatch(inst)
    e2.age_lease(inst, vb, -(e2.TAKEOVER_MARGIN_S + 1))
    cmd = {"schema_version": 1, "verification_id": vb,
           "attribution": sa.attribution(sa.W_TIMER)}
    with sa.runtime_conn(inst) as conn_a, sa.runtime_conn(inst) as conn_b:
        with conn_a.transaction():
            a = sa.call_in(conn_a, inst, "m05_el_claim", cmd)
            with conn_b.transaction():
                b = sa.call_in(conn_b, inst, "m05_el_claim", cmd)
        with conn_b.transaction():
            b2 = sa.call_in(conn_b, inst, "m05_el_claim", cmd)
    assert (a["outcome"], a["epoch"]) == ("claimed", epoch + 1)
    assert b["outcome"] == b2["outcome"] == "not_claimable"
    assert e2.q(inst, "SELECT epoch FROM {s}.m05_el_verifications WHERE verification_id = %s",
                (vb,)) == [(epoch + 1,)]


# --- E2-P11 / P12 late and mismatched answers -----------------------------------------

@pytest.mark.parametrize("cause", ["unknown_attempt", "closed_attempt", "superseded",
                                   "stale_epoch"])
def test_el2_p11_late_answers_change_nothing(install: e2.EL2Install, cause: str) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, patient = e2.enrol(inst, start)
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    epoch = e2.claim(inst, vb)["epoch"]
    sent = e2.dispatch(inst, vb, epoch)
    attempt, use_epoch = sent["attempt_id"], epoch
    if cause == "unknown_attempt":
        attempt = sa.new_id()
    elif cause == "closed_attempt":
        assert _out(e2.timeout(inst, sent["attempt_id"], epoch)) == "timed_out"
    elif cause == "superseded":
        el.put(inst, ref, patient, start, coverage=el.present(member_id="M000778"))
    else:
        use_epoch = epoch + 5
    echo = dict(e2.echo_of(sent), attempt_id=attempt)
    before = e2.table_text(inst)
    got = e2.record(inst, attempt, use_epoch, echo, e2.active_result(_now()))
    assert got == {"schema_version": 1, "outcome": "late", "cause": cause}
    after = e2.table_text(inst)
    assert {k: v for k, v in after.items() if k != "m05_el_late_answers"} == {
        k: v for k, v in before.items() if k != "m05_el_late_answers"}
    rows = e2.late(inst)
    assert rows[-1] == (attempt, None if cause == "unknown_attempt" else vb, cause, "active")


@pytest.mark.parametrize("field", ["attempt_id", "tenant_id", "appointment_ref", "patient_id",
                                   "member_id", "coverage_version"])
def test_el2_p12_echo_mismatch_is_late_and_clears_nothing(install: e2.EL2Install,
                                                          field: str) -> None:
    inst = e2.ready(install)
    _ref, vb, epoch, sent = _claimed_dispatch(inst)
    wrong: dict[str, Any] = {"attempt_id": sa.new_id(), "tenant_id": "el2-other-tenant",
                             "appointment_ref": sa.new_id(), "patient_id": sa.new_id(),
                             "member_id": "M999999", "coverage_version": 2}
    echo = dict(e2.echo_of(sent), **{field: wrong[field]})
    got = e2.record(inst, sent["attempt_id"], epoch, echo, e2.active_result(_now()))
    assert got == {"schema_version": 1, "outcome": "late", "cause": "mismatch"}
    assert e2.attempts(inst, vb) == [(sent["attempt_id"], 1, "dispatched")]
    assert _out(e2.settle(inst, vb, epoch)) == "not_settleable"
    assert e2.record(inst, sent["attempt_id"], epoch, e2.echo_of(sent),
                     e2.active_result(_now())) == RECORDED


# --- E2-P13 expiry --------------------------------------------------------------------

SENTINEL_COVERAGE = {"member_id": "SENTINEL-MEMBER-9931", "patient_name": "Sentinel Patientname",
                     "patient_dob": "1911-11-11"}
SENTINEL_TXN = "SENTINEL-TXN-9932"


def _settle_sentinel(inst: e2.EL2Install, start: datetime) -> tuple[str, str]:
    ref, patient = sa.new_id(), sa.new_id()
    el.put(inst, ref, patient, start, coverage=el.present(**SENTINEL_COVERAGE))
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    assert e2.run(inst, vb, [e2.active_result(_now(), txn_ref=SENTINEL_TXN)])["settle"] == OK
    return ref, vb


def test_el2_p13_expiry_removes_identifiers_keeps_class(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    old_ref, old_vb = _settle_sentinel(inst, _now().replace(microsecond=0) - timedelta(days=40))
    new_ref, new_vb = _settle_sentinel(inst, _now().replace(microsecond=0) - timedelta(days=10))
    assert e2.expire(inst) == {"schema_version": 1, "outcome": "applied", "expired": 1}
    cov = e2.q(inst, "SELECT member_id, patient_name, patient_dob, subscriber_name,"
               " subscriber_dob, subscriber_member_id, block FROM {s}.m05_el_coverage"
               " WHERE appointment_ref = %s", (old_ref,))
    assert cov == [(None, None, None, None, None, None, {"state": "present"})]
    answer = e2.q(inst, "SELECT a.answer FROM {s}.m05_el_attempts AS a"
                  " WHERE a.verification_id = %s", (old_vb,))[0][0]
    assert set(answer) == {"class", "reason", "observed_at"}
    assert e2.q(inst, "SELECT member_digest, result_class, reason, observed_at IS NOT NULL,"
                " settled_at IS NOT NULL FROM {s}.m05_el_verifications"
                " WHERE verification_id = %s", (old_vb,)) == [
        (None, "active", "coverage_active", True, True)]
    kept = e2.q(inst, "SELECT member_id FROM {s}.m05_el_coverage WHERE appointment_ref = %s",
                (new_ref,))
    assert kept == [(SENTINEL_COVERAGE["member_id"],)]
    assert e2.q(inst, "SELECT member_digest IS NOT NULL FROM {s}.m05_el_verifications"
                " WHERE verification_id = %s", (new_vb,)) == [(True,)]
    assert e2.expire(inst)["expired"] == 0                 # idempotent


@pytest.mark.parametrize(("days", "expected"), [(29, False), (30, False), (31, True)])
def test_el2_p13_expiry_seam_exact(install: e2.EL2Install, days: int, expected: bool) -> None:
    inst = e2.ready(install)
    service = date(2026, 11, 23)
    # Green deviation D-G2: ISO text dates (the platform admits no `date` parameter, F-INT1).
    assert e2.q(inst, "SELECT {s}.m05_el_expired(%s, %s)",
                (service.isoformat(), (service + timedelta(days=days)).isoformat())) == [
        (expected,)]


# --- E2-P14 audit and privacy --------------------------------------------------------------

def _columns(inst: e2.EL2Install, table: str) -> set[str]:
    got = e2.q(inst, "SELECT column_name FROM information_schema.columns"
               " WHERE table_schema = %s AND table_name = %s", (inst.schema, table))
    return {r[0] for r in got}


def test_el2_p14_audit_rows_carry_ids_class_reason_time_only(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    assert _columns(inst, "m05_el_case_events") == {
        "event_id", "appointment_ref", "seq", "coverage_version", "kind", "class", "reason",
        "created_at", "applied_at", "verification_id"}
    assert _columns(inst, "m05_el_late_answers") == {
        "late_id", "attempt_id", "verification_id", "cause", "class", "received_at"}
    _ref, _vb = _settle_sentinel(inst, e2.future(10))
    for table in e2.EL2_TABLES:
        if table == "m05_el_coverage":
            continue
        text = "\n".join(r[0] for r in e2.table_text(inst)[table])
        for s_ in SENTINEL_COVERAGE.values():
            assert s_ not in text, (table, s_)


def _digests(inst: e2.EL2Install, ref: str) -> dict[str, str]:
    got = e2.q(inst, "SELECT owner_trigger, member_digest FROM {s}.m05_el_verifications"
               " WHERE appointment_ref = %s", (ref,))
    return {r[0]: r[1] for r in got}


def test_el2_p14_digest_is_salted_per_appointment_and_stable(install: e2.EL2Install) -> None:
    """Codex finding 2: a constant or unsalted digest must fail."""
    inst = e2.ready(install)
    start = e2.future(10)
    x, _px = e2.enrol(inst, start, member_id=SENTINEL_COVERAGE["member_id"])
    y, _py = e2.enrol(inst, start, member_id=SENTINEL_COVERAGE["member_id"])
    z, _pz = e2.enrol(inst, start, member_id="SENTINEL-MEMBER-9939")
    e2.due(inst)
    for ref in (x, y, z):
        e2.run(inst, e2.own_verification(inst, ref, "booking"), [e2.active_result(_now())])
    e2.make_due(inst, x, "previsit")
    e2.due(inst)
    e2.run(inst, e2.own_verification(inst, x, "previsit"),
           [e2.active_result(e2.window_starts(start)["previsit"] + timedelta(minutes=1))])
    dx, dy, dz = _digests(inst, x), _digests(inst, y), _digests(inst, z)
    for digest in (*dx.values(), *dy.values(), *dz.values()):
        assert isinstance(digest, str) and len(digest) == 64
        assert digest == digest.lower() and all(c in "0123456789abcdef" for c in digest)
        assert SENTINEL_COVERAGE["member_id"].lower() not in digest
    assert dx["booking"] == dx["previsit"]          # stable: same appointment and member
    assert dx["booking"] != dy["booking"]           # salted: same member, other appointment
    assert dx["booking"] != dz["booking"]           # other member


def test_el2_p14_digest_binds_the_member_on_one_appointment(install: e2.EL2Install) -> None:
    """Codex b690404c (added at green, disclosed): the same appointment with a new member
    (coverage v2) gives a different digest; the member alone is varied."""
    inst = e2.ready(install)
    start = e2.future(10)
    ref, patient = e2.enrol(inst, start, member_id="SENTINEL-MEMBER-9941")
    e2.due(inst)
    v1 = e2.own_verification(inst, ref, "booking")
    e2.run(inst, v1, [e2.active_result(_now())])
    el.put(inst, ref, patient, start, coverage=el.present(member_id="SENTINEL-MEMBER-9942"))
    e2.due(inst)
    got = e2.q(inst, "SELECT coverage_version, member_digest FROM {s}.m05_el_verifications"
               " WHERE appointment_ref = %s AND owner_trigger = 'booking'"
               " ORDER BY coverage_version", (ref,))
    assert [g[0] for g in got] == [1, 2]
    assert got[0][1] != got[1][1]


def test_el2_p14_one_event_per_settlement_with_its_verification_id(
        install: e2.EL2Install) -> None:
    """Codex D-3: the settlement's durable identity and replay source for E3's single M02
    settled event; a replayed settle neither adds nor changes it."""
    inst = e2.ready(install)
    ref, _p = e2.enrol(inst, e2.future(10))
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    got = e2.run(inst, vb, [e2.active_result(_now())])
    before = e2.q(inst, "SELECT event_id::text, verification_id::text, seq"
                  " FROM {s}.m05_el_case_events WHERE appointment_ref = %s", (ref,))
    assert len(before) == 1 and before[0][1:] == (vb, 1)
    assert e2.settle(inst, vb, got["epoch"]) == OK          # replay: the stored result
    assert e2.q(inst, "SELECT event_id::text, verification_id::text, seq"
                " FROM {s}.m05_el_case_events WHERE appointment_ref = %s", (ref,)) == before
    unique = e2.q(inst, "SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND tablename ="
                  " 'm05_el_case_events' AND indexdef ILIKE '%%UNIQUE%%(verification_id)%%'",
                  (inst.schema,))
    assert unique == [(1,)]


def test_el2_p14_gateway_results_carry_no_identifiers(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    ref, patient = sa.new_id(), sa.new_id()
    el.put(inst, ref, patient, e2.future(10), coverage=el.present(**SENTINEL_COVERAGE))
    outputs = [e2.due(inst)]
    vb = e2.own_verification(inst, ref, "booking")
    claimed = e2.claim(inst, vb)
    outputs.append(claimed)
    sent = e2.dispatch(inst, vb, claimed["epoch"])
    assert sent["request"]["member_id"] == SENTINEL_COVERAGE["member_id"]   # the port needs it
    outputs.append(e2.timeout(inst, sent["attempt_id"], claimed["epoch"]))
    outputs.append(e2.dispatch(inst, vb, claimed["epoch"]))                 # retry_not_ready
    outputs.append(e2.expire(inst))
    text = repr(outputs)
    for s_ in SENTINEL_COVERAGE.values():
        assert s_ not in text, s_


# --- E2-P16 retry delay ----------------------------------------------------------------------

@pytest.mark.parametrize(("delta", "expected"), [
    (timedelta(seconds=119, microseconds=999000), False), (timedelta(seconds=120), True)])
def test_el2_p16_retry_seam_exact(install: e2.EL2Install, delta: timedelta,
                                  expected: bool) -> None:
    inst = e2.ready(install)
    failed = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    assert e2.q(inst, "SELECT {s}.m05_el_retry_ready(%s, %s)", (failed, failed + delta)) == [
        (expected,)]


def test_el2_p16_retry_not_before_delay_and_never_a_third(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    ref, vb, epoch, first = _claimed_dispatch(inst)
    assert _out(e2.timeout(inst, first["attempt_id"], epoch)) == "timed_out"
    assert _out(e2.dispatch(inst, vb, epoch)) == "retry_not_ready"
    assert e2.q(inst, "SELECT state, lease_until FROM {s}.m05_el_verifications"
                " WHERE verification_id = %s", (vb,)) == [("due", None)]
    e2.age_failure(inst, first["attempt_id"], -(e2.RETRY_DELAY_S - 5))
    assert vb not in e2.due(inst)["verifications"]
    e2.age_failure(inst, first["attempt_id"], -(e2.RETRY_DELAY_S + 1))
    assert vb in e2.due(inst)["verifications"]
    epoch2 = e2.claim(inst, vb)["epoch"]
    second = e2.dispatch(inst, vb, epoch2)
    assert (second["outcome"], second["n"]) == ("dispatched", 2)
    assert _out(e2.timeout(inst, second["attempt_id"], epoch2)) == "timed_out"
    e2.age_failure(inst, second["attempt_id"], -(e2.RETRY_DELAY_S + 1))
    assert _out(e2.dispatch(inst, vb, epoch2)) == "exhausted"
    assert e2.settle(inst, vb, epoch2) == _settled("inconclusive", "payer_unavailable")
    assert e2.slots(inst, ref) == 2


# --- E2-P17 no retry on non-transient ----------------------------------------------------

@pytest.mark.parametrize(("cls", "reason"), [
    *[("inconclusive", r) for r in e2.PAYER_ERRORS],
    ("inconclusive", "response_unreadable"), ("inactive", "coverage_inactive")])
def test_el2_p17_non_transient_settles_after_one_slot(install: e2.EL2Install, cls: str,
                                                      reason: str) -> None:
    inst = e2.ready(install)
    ref, _p = e2.enrol(inst, e2.future(10))
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    observed = None if reason == "response_unreadable" else _now()
    got = e2.run(inst, vb, [e2.result_of(cls, reason, observed)])
    assert got["settle"] == _settled(cls, reason)
    assert [a[1:] for a in e2.attempts(inst, vb)] == [(1, "answered")]
    assert e2.claim(inst, vb)["outcome"] == "not_claimable"
    assert e2.events(inst, ref) == [(1, 1, "open_or_note", cls, reason)]


# --- E2-P18 freshness against stored rows ----------------------------------------------------

ALTER = {
    "patient_id": "patient_id = gen_random_uuid()",
    "payer_id": "payer_id = 'SYN-UHC'",
    "member_digest": "member_digest = repeat('f', 64)",
    "coverage_version": "coverage_version = 99",
    "service_date": "service_date = service_date + 1",
    "observed_at": "observed_at = %(window)s - interval '1 microsecond'",
}


@pytest.mark.parametrize("component", [None, *ALTER])
def test_el2_p18_each_stored_component_alone_makes_it_stale(install: e2.EL2Install,
                                                            component: str | None) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    ref, _p = e2.enrol(inst, start)
    window = e2.window_starts(start)["dayof"]
    e2.due(inst)
    vb = e2.own_verification(inst, ref, "booking")
    assert e2.run(inst, vb, [e2.active_result(window + timedelta(minutes=1))])["settle"] == OK
    if component is not None:
        e2.q(inst, "UPDATE {s}.m05_el_verifications SET " + ALTER[component]
             + " WHERE verification_id = %(vid)s", {"window": window, "vid": vb})
    e2.make_due(inst, ref, "dayof")
    got = e2.due(inst)
    assert (got["skipped_fresh"], got["created"]) == ((1, 0) if component is None else (0, 1))


def test_el2_p18_another_appointments_result_never_counts(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    start = e2.future(10)
    patient = sa.new_id()
    x, y = sa.new_id(), sa.new_id()
    el.put(inst, x, patient, start, coverage=el.present())
    window = e2.window_starts(start)["dayof"]
    e2.due(inst)
    vx = e2.own_verification(inst, x, "booking")
    assert e2.run(inst, vx, [e2.active_result(window + timedelta(minutes=1))])["settle"] == OK
    el.put(inst, y, patient, start, coverage=el.present())      # same patient, payer, member
    e2.make_due(inst, y, "booking", offset_s=3600.0)            # keep y's booking out of the way
    e2.make_due(inst, y, "dayof")
    got = e2.due(inst)
    assert (got["skipped_fresh"], got["created"]) == (0, 1)
    assert e2.triggers(inst, y)["dayof"][0] == "assigned"


# --- E2-P19 due ---------------------------------------------------------------------------

def test_el2_p19_due_limit_and_order(install: e2.EL2Install) -> None:
    inst = e2.ready(install)
    refs = [e2.enrol(inst, e2.future(10 + i))[0] for i in range(3)]
    got = e2.due(inst, limit=2)
    assert set(got) == {"schema_version", "outcome", "created", "attached", "skipped_fresh",
                        "verifications"}
    assert got["created"] == 3 and len(got["verifications"]) == 2
    assert len(e2.due(inst, limit=100)["verifications"]) == 3
    assert e2.due(inst)["created"] == 0                     # replay creates nothing
    for ref in refs:
        assert len(e2.verifications(inst, ref)) == 1


# --- E2-P20 malformed commands and workload ------------------------------------------------

def _bad(inst: e2.EL2Install, function: str, command: dict[str, Any], code: str) -> None:
    before = e2.table_text(inst)
    with pytest.raises(psycopg.errors.RaiseException, match=code):
        sa.call(inst, function, command)
    assert e2.table_text(inst) == before


@pytest.mark.parametrize("function", e2.GATEWAYS)
def test_el2_p20_extra_key_and_wrong_workload_refused(install: e2.EL2Install,
                                                      function: str) -> None:
    inst = e2.ready(install)
    _ref, vb, epoch, sent = _claimed_dispatch(inst)
    base: dict[str, dict[str, Any]] = {
        "m05_el_due": {"limit": 10}, "m05_el_claim": {"verification_id": vb},
        "m05_el_dispatch": {"verification_id": vb, "epoch": epoch},
        "m05_el_record_answer": {"attempt_id": sent["attempt_id"], "epoch": epoch,
                                 "echo": e2.echo_of(sent),
                                 "result": e2.active_result(_now())},
        "m05_el_timeout": {"attempt_id": sent["attempt_id"], "epoch": epoch},
        "m05_el_settle": {"verification_id": vb, "epoch": epoch},
        "m05_el_expire": {"limit": 10}}
    fields = base[function]
    _bad(inst, function, {"schema_version": 1, **fields, "extra": 1,
                          "attribution": sa.attribution(sa.W_TIMER)}, "COMMAND_MALFORMED")
    _bad(inst, function, {"schema_version": 1, **fields,
                          "attribution": sa.attribution(sa.W_DIRECTORY)},
         "WORKLOAD_NOT_AUTHORIZED")


def _drop(key: str) -> Any:
    def mutate(r: dict[str, Any]) -> dict[str, Any]:
        r.pop(key)
        return r
    return mutate


def _set(key: str, value: Any) -> Any:
    def mutate(r: dict[str, Any]) -> dict[str, Any]:
        r[key] = value
        return r
    return mutate


MALFORMED_RESULT = {
    "class-unknown": _set("class", "maybe"),
    "class-missing": _drop("class"),
    "reason-missing": _drop("reason"),
    "reason-of-other-class": _set("reason", "payer_unavailable"),
    "observed-missing": _drop("observed_at"),
    "observed-not-iso": _set("observed_at", "yesterday"),
    "observed-naive": _set("observed_at", "2026-11-19T12:00:00"),
    "extra-key": _set("surprise", "x"),
    "copay-type": _set("copay", 25),
    "referral-type": _set("referral_required", "no"),
}


@pytest.mark.parametrize("variant", list(MALFORMED_RESULT))
def test_el2_p20_malformed_result_refused_attempt_stays_answerable(
        install: e2.EL2Install, variant: str) -> None:
    """Codex finding 3: each malformed answer is the accepted control with ONE field
    changed; the refusal writes nothing and the attempt still takes the valid answer."""
    inst = e2.ready(install)
    _ref, vb, epoch, sent = _claimed_dispatch(inst)
    control = e2.active_result(_now())
    bad = MALFORMED_RESULT[variant](dict(control))
    assert bad != control
    _bad(inst, "m05_el_record_answer",
         {"schema_version": 1, "attempt_id": sent["attempt_id"], "epoch": epoch,
          "echo": e2.echo_of(sent), "result": bad,
          "attribution": sa.attribution(sa.W_TIMER)}, "COMMAND_MALFORMED")
    assert e2.attempts(inst, vb) == [(sent["attempt_id"], 1, "dispatched")]
    assert e2.record(inst, sent["attempt_id"], epoch, e2.echo_of(sent), control) == RECORDED
    assert e2.settle(inst, vb, epoch) == OK


# --- E2-P21 privileges and catalogue --------------------------------------------------------

def test_el2_p21_runtime_privileges(install: e2.EL2Install) -> None:
    inst = e2.ready(install)

    def can_execute(signature: str) -> bool:
        got = e2.q(inst, "SELECT has_function_privilege('haloflow_runtime',"
                   " format('%%I.' || %s, %s::text), 'EXECUTE')", (signature, inst.schema))
        return bool(got[0][0])

    for fn in e2.GATEWAYS:
        assert can_execute(fn + "(jsonb)"), fn
    for fn, args in (("m05_el_retry_ready", "(timestamptz, timestamptz)"),
                     ("m05_el_takeover_ready", "(timestamptz, timestamptz)"),
                     ("m05_el_fresh", "(timestamptz, timestamptz)"),
                     ("m05_el_expired", "(text, text)")):          # D-G2
        assert not can_execute(fn + args), fn
    vol = e2.q(inst, "SELECT p.proname, p.provolatile FROM pg_proc p JOIN pg_namespace n"
               " ON n.oid = p.pronamespace WHERE n.nspname = %s AND p.proname = ANY(%s)",
               (inst.schema, list(e2.SEAMS)))
    assert dict(vol) == dict.fromkeys(e2.SEAMS, "i")
    for table in ("m05_el_attempts", "m05_el_late_answers", *el.EL_TABLES):
        got = e2.q(inst, "SELECT has_table_privilege('haloflow_runtime',"
                   " format('%%I.%%I', %s::text, %s::text),"
                   " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
                   (inst.schema, table))
        assert got == [(False,)], table


def test_el2_p21_catalogue_keys_and_flag_dependency() -> None:
    from haloflow.synthetic.composition import build_synthetic_catalog, build_synthetic_registry

    # Green deviation D-G4: the red row searched repr(catalog), which does not list keys;
    # the catalogue's own keys() / write_capabilities are read instead (same oracle).
    catalog = build_synthetic_catalog(include_el1=True, include_el2=True)
    keys = set(catalog.catalog.keys())
    for key in e2.STATEMENT_KEYS:
        assert key in keys, key
    assert e2.WORKER_CAPABILITY in catalog.write_capabilities
    with pytest.raises(ValueError):
        build_synthetic_registry(include_el1=False, include_el2=True)
    assert "m05.eligibility.due" not in set(
        build_synthetic_catalog(include_el1=True).catalog.keys())


# --- E2-P22 routed worker end to end ---------------------------------------------------------

def test_el2_p22_routed_worker_runs_the_booking_check(install: e2.EL2Install) -> None:
    from haloflow.m05.eligibility import EligibilityWorker
    from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

    inst = e2.ready(install)                                  # D-G3: sync row
    ref, _p = e2.enrol(inst, e2.future(10))
    inner = SimulatedClearinghouse(clock=_now)
    inner.set_member(el.present()["member_id"], "active")
    port = e2.RecordingPort(inst, inner)

    async def body() -> None:
        async with e2.worker_runtime(inst) as (service, context):
            ids = await service.due(await context(), 100)
            vb = e2.own_verification(inst, ref, "booking")
            assert ids == [vb]
            worker = EligibilityWorker(service, port, call_timeout_s=5.0)
            assert await worker.run_one(await context(), vb) == "settled"
            assert await service.expire(await context(), 100) == 0

    asyncio.run(body())
    assert port.committed_at_entry == ["dispatched"]
    assert port.calls == port.entries and len(port.calls) == 1
    assert e2.events(inst, ref) == [(1, 1, "open_or_note", "active", "coverage_active")]
