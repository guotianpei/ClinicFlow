"""Eligibility E3a — PostgreSQL 17 rows (RED BY DESIGN).

Each row provisions a fresh synthetic tenant at target 121 in its body, so the intended red
cause is the missing E3a source: `build_synthetic_registry(include_el3a=...)` does not exist
(TypeError at provisioning, reported as "E3a test installation failed"), or
ModuleNotFoundError for an E3a module imported first. Rows marked CONTROL use only existing
code and must pass now. Interface pins: the el3a_support docstring.

E3a owns case creation, event application and the read projection (Codex sequencing ALIGN,
relay f14a103a). Staff actions, closure and the legacy-gateway regression are E3b. The three
cross-package obligations from that review are rows here: (1) exactly-once M02 evidence across
replay and restart, including E1 manual and pre-E3 settlements -> E3a-P18, P18b; (2) applier
rule 3 (watermark) in isolation -> E3a-P06[watermark] (E3b proves it through a routed
closure); (3) an eligibility item cannot be resolved through an existing staff path -> E3a-P19.

Rows (stage-3 ids; trace in brackets):
- E3a-C00 CONTROL: an E2 install settles an inactive finding -> one UNAPPLIED case event.
- E3a-P05 first unresolved event -> one case, one item of the class's kind, note 1, one
  evidence event (effect opened) that is the item's source [R3, M2 rule 5] x3 classes
- E3a-P06 rules in isolation: older coverage version; seq <= watermark (FIXTURE); open case
  -> note; no case + active -> none; drained twice -> idle (rule 1) [M2, obligation 2]
- E3a-P07 note-only event after an unresolved finding: reason/class unchanged, version +1
  [C1, W20 applier part]
- E3a-P08 a later active check on an open case: note, case stays open, current reason
  unchanged, no second item [OD-EL3, W3/W10]
- E3a-P08b a later unresolved finding on an open case: note, current reason := the new
  finding, still ONE item [M2 rule 4]
- E3a-P09a coverage_not_received opens a manual case [OD-EL9, M3]
- E3a-P14 case view: exact keys; the OD-EL4 summary only; legacy / unknown ids ->
  ITEM_NOT_FOUND; manual case summary null; after expiry summary null [OD-EL4, R4]
- E3a-P16 the E2-P03 fixture materialized: two timeouts open inconclusive /
  payer_unavailable; the later active check is a note [T3]
- E3a-P17 view isolation: another tenant's item -> ITEM_NOT_FOUND [T3 isolation]
- E3a-P18 (v1r1) a pre-E3 backlog (E1 manual x3, E2 inactive, E2 active, a note-only
  cancellation) drained across a restart (the third transaction dies after its append):
  the expected identities come from settled VERIFICATION rows, independently of case
  events; exactly one `record: settlement` observation per verification; notes and
  findings carry no verification; only "opened" observations are item sources
  [obligation 1, R4 audit, Codex red review 1]
- E3a-P18b restart: a failure after the M02 append (and, separately, after the create)
  rolls everything back; the next run writes exactly one evidence event [obligation 1]
- E3a-P18c (v1r1) coverage_not_received is a `finding`, never a settlement
- E3a-P19 (v1r1) an eligibility item cannot be resolved through the existing staff path
  (resolve, claim_and_resolve): ActionFailed ACTION_RESULT_INVALID (green correction G-2);
  a comparable legacy item (an
  S-I ehr_confirmation_check made through the existing append + create path; green
  correction G-1) succeeds through the same actor, profile and path; the eligibility
  item's state, assignee and version unchanged; no resolution history, action or event
  [obligation 3, review 2]
- E3a-C06 (v1r1) forced overlap: applier A paused inside its transaction holding the
  appointment key while applier B runs to completion and applies nothing for it; then A
  drains; both finish; one case, notes 1..3, three evidence events [M2, review 3]
- E3a-P20 privileges and catalogue: runtime EXECUTE on t119..t121 only; no runtime table
  privilege; statement keys; production registry {36} unchanged [R4, R7]
- E3a-P21 refusals: closed key set; actor attribution; other workload -> nothing written
- E3a-P22 activation off: settled findings still reach staff (D-R2, Codex agreed); a
  checkable verification is refused at dispatch with no attempt row (no payer call)
- E3a-P23 privacy: no coverage sentinel in cases, notes, evidence or items [EL-14]
- E3a-P24 create_item_v3: legacy kinds unchanged; an eligibility source that is not an open
  case's -> SOURCE_NOT_ELIGIBLE; replay returns the same item [v1 section 5]
- E3a-P25 the scheduler runs run -> expire -> apply when given the case service [R7]
- E3a-P25b apply phase refuses a valid case context for another tenant before drain: no
  case, item or evidence for B, zero counts reported under A (TENANT_MISMATCH); matched
  control opens B's case [Codex E3a green review 2026-11-20]
- E3a-P25c pass context for another tenant: A refused before due/expire/apply
  (TENANT_MISMATCH, NOT_RUN), B's eligibility rows unchanged; matched control COMPLETE
  [Codex dcd06e8c]

Mutant killers (wired at green): M05 note-only overwrites the current reason -> P07; M07 the
view leaks coinsurance / deductible_total / txn_ref -> P14; plus E3a-only mutants declared at
green (active overwrites reason -> P08; evidence for "none" skipped -> P18; rule 2 skipped ->
P06[older_version]).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import el1_support as el
import el2_support as e2
import el3a_support as e3
import m02_support
import pytest
import sa_support as sa

pytestmark = pytest.mark.postgres

SENTINELS = ("M000001", "Synthetic Patient", "1970-01-01", "SYN-TXN-0001")


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[e3.EL3AInstall]:
    yield from e3.module_install(request, m02_ids, m05_reset, f"el3a:{request.node.name}")


@pytest.fixture
def other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
          m05_reset: Any) -> Iterator[e3.EL3AInstall]:
    yield from e3.module_install(request, m02_ids, m05_reset, f"el3a-o:{request.node.name}")


@pytest.fixture
def el2_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
                m05_reset: Any) -> Iterator[e2.EL2Install]:
    yield from e2.module_install(request, m02_ids, m05_reset, f"el3a-e2:{request.node.name}")


def _start(days: float = 20) -> datetime:
    return e2.future(days)


def _one_case(install: e3.EL3AInstall, ref: str) -> dict[str, Any]:
    got = e3.cases(install, ref)
    assert len(got) == 1, got
    return got[0]


def _item_of(install: e3.EL3AInstall, case: dict[str, Any]) -> dict[str, Any]:
    got = e3.rows(install, "m05_followup_items", "item_id = %s::uuid", (case["item_id"],))
    assert len(got) == 1, got
    return got[0]


def _effects(install: e3.EL3AInstall, ref: str) -> list[str | None]:
    return [r["effect"] for r in e3.case_events(install, ref)]


# --- E3a-C00 CONTROL ----------------------------------------------------------------------

def test_el3a_c00_control_e2_settlement_leaves_an_unapplied_event(
        el2_install: e2.EL2Install) -> None:
    inst = e2.ready(el2_install)
    ref, _ = e2.enrol(inst, _start())
    e2.due(inst)
    vid = e2.own_verification(inst, ref, "booking")
    got = e2.run(inst, vid, [e2.result_of("inactive", "coverage_inactive",
                                          datetime.now(UTC))])["settle"]
    assert got["outcome"] == "settled"
    (row,) = e2.q(inst, "SELECT kind, class, reason, applied_at FROM {s}.m05_el_case_events"
                  " WHERE appointment_ref = %s", (ref,))
    assert row == ("open_or_note", "inactive", "coverage_inactive", None)


# --- E3a-P05 ------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["inactive", "inconclusive", "manual_required"])
def test_el3a_p05_first_unresolved_event_opens_one_case_and_item(
        install: e3.EL3AInstall, scenario: str) -> None:
    inst = e3.ready(install)
    if scenario == "inactive":
        ref, patient, _ = e3.payer(inst, _start(), "inactive", "coverage_inactive")
        reason = "coverage_inactive"
    elif scenario == "inconclusive":
        ref, patient, _ = e3.payer(inst, _start(), "inconclusive", "subscriber_not_found")
        reason = "subscriber_not_found"
    else:
        ref, patient = e3.manual(inst, _start())
        reason = "nonpriority"
    assert e3.drain(inst) == {"opened": 1}
    case = _one_case(inst, ref)
    kind = e3.KIND_BY_CLASS[scenario]
    assert (case["state"], case["item_kind"], case["current_class"], case["current_reason"],
            case["case_version"]) == ("open", kind, scenario, reason, 1)
    item = _item_of(inst, case)
    assert (item["item_kind"], item["patient_id"], item["state"]) == (kind, patient, "open")
    (note,) = e3.notes(inst, case["case_id"])
    assert (note["seq"], note["class"], note["reason"]) == (1, scenario, reason)
    (ev,) = e3.evidence(inst, ref)
    assert ev["event_id"] == item["source_event_id"] == case["source_event_id"]
    assert ev["metadata"]["effect"] == "opened" and ev["metadata"]["open_kind"] == kind
    (event,) = e3.case_events(inst, ref)
    assert event["effect"] == "opened" and event["evidence_event_id"] == ev["event_id"]
    assert event["applied_at"] is not None


# --- E3a-P06 rules in isolation -----------------------------------------------------------

def test_el3a_p06_rule2_older_coverage_version_has_no_effect(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, patient = e3.manual(inst, _start())  # seq 1, coverage version 1 (nonpriority)
    el.put(inst, ref, patient, _start(), coverage=el.present(el.OTHER_PAYER, member_id="M9"))
    events = e3.case_events(inst, ref)
    assert events[0]["coverage_version"] < events[-1]["coverage_version"]
    e3.drain(inst)
    assert e3.case_events(inst, ref)[0]["effect"] == "none"
    # the new version's own manual finding opens the (single) case
    case = _one_case(inst, ref)
    assert case["current_reason"] == "nonpriority" and case["case_version"] >= 1
    assert len(e3.evidence(inst, ref)) == len(e3.case_events(inst, ref))


def test_el3a_p06_rule3_seq_at_or_below_the_watermark_has_no_effect(
        install: e3.EL3AInstall) -> None:
    """Codex obligation 2: the watermark rule alone (E3b sets it by a routed close)."""
    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())
    e3.set_watermark(inst, ref, 1)
    assert e3.drain(inst) == {"none": 1}
    assert e3.cases(inst, ref) == [] and e3.el_items(inst) == []
    (ev,) = e3.evidence(inst, ref)
    assert ev["metadata"]["effect"] == "none"


def test_el3a_p06_rule4_open_case_takes_a_note(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _, _ = e3.payer(inst, _start(), "inconclusive", "subscriber_not_found")
    e3.drain(inst)
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    assert e3.drain(inst) == {"noted": 1}
    case = _one_case(inst, ref)
    assert case["case_version"] == 2
    assert [n["reason"] for n in e3.notes(inst, case["case_id"])] == [
        "subscriber_not_found", "coverage_inactive"]


def test_el3a_p06_rule5_no_case_and_active_has_no_effect(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _, _ = e3.payer(inst, _start(), "active", "coverage_active")
    assert e3.drain(inst) == {"none": 1}
    assert e3.cases(inst, ref) == [] and e3.el_items(inst) == []


def test_el3a_p06_rule1_drained_twice_is_idle(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())
    assert e3.drain(inst) == {"opened": 1}
    before = (e3.cases(inst, ref), e3.evidence(inst, ref), e3.el_items(inst))
    assert e3.drain(inst) == {}
    assert e3.apply_one(inst) == "idle"
    assert (e3.cases(inst, ref), e3.evidence(inst, ref), e3.el_items(inst)) == before


# --- E3a-P07 / P08 / P08b / P09a ---------------------------------------------------------

def test_el3a_p07_note_only_event_keeps_the_reason_and_bumps_the_version(
        install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    start = _start()
    ref, patient, _ = e3.payer(inst, start, "inactive", "coverage_inactive")
    e3.drain(inst)
    el.put(inst, ref, patient, start, coverage=el.present(), status="cancelled")
    assert e3.drain(inst) == {"noted": 1}
    case = _one_case(inst, ref)
    assert (case["current_class"], case["current_reason"], case["case_version"],
            case["state"]) == ("inactive", "coverage_inactive", 2, "open")
    last = e3.notes(inst, case["case_id"])[-1]
    assert (last["kind"], last["class"], last["reason"]) == (
        "note_only", None, "appointment_cancelled")


def test_el3a_p08_later_active_is_a_note_and_the_case_stays_open(
        install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _, _ = e3.payer(inst, _start(), "inconclusive", "name_dob_mismatch")
    e3.drain(inst)
    e3.later(inst, ref, "previsit", "active", "coverage_active")
    assert e3.drain(inst) == {"noted": 1}
    case = _one_case(inst, ref)
    assert (case["state"], case["current_class"], case["current_reason"],
            case["case_version"]) == ("open", "inconclusive", "name_dob_mismatch", 2)
    last = e3.notes(inst, case["case_id"])[-1]
    assert (last["class"], last["reason"]) == ("active", "coverage_active")
    assert len(e3.el_items(inst)) == 1


def test_el3a_p08b_later_unresolved_finding_updates_the_current_reason(
        install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _, _ = e3.payer(inst, _start(), "inconclusive", "subscriber_not_found")
    e3.drain(inst)
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    e3.drain(inst)
    case = _one_case(inst, ref)
    assert (case["current_class"], case["current_reason"]) == ("inactive", "coverage_inactive")
    assert case["item_kind"] == "eligibility_inconclusive"  # the item kind never changes
    assert len(e3.el_items(inst)) == 1


def test_el3a_p09a_coverage_not_received_opens_a_manual_case(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install, not_received_days=1)
    ref, patient = sa.new_id(), sa.new_id()
    start = _start(0.5)  # inside N = 1 day: the not-received instant has passed
    el.put(inst, ref, patient, start, coverage={"state": "pending"})
    el.tick(inst)
    assert [(r["class"], r["reason"]) for r in e3.case_events(inst, ref)] == [
        ("manual_required", "coverage_not_received")]
    assert e3.drain(inst) == {"opened": 1}
    case = _one_case(inst, ref)
    assert (case["item_kind"], case["current_reason"]) == (
        "eligibility_manual", "coverage_not_received")


# --- E3a-P14 the case view ----------------------------------------------------------------

def test_el3a_p14_view_projects_od_el4_fields_only(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _, _ = e3.payer(inst, _start(), "inactive", "coverage_inactive",
                         payer_name="Synthetic Payer", plan="SYN-PPO", copay="25.00",
                         deductible_total="1000.00", deductible_remaining="400.00",
                         coinsurance="20", referral_required=True, auth_required=False,
                         txn_ref="SYN-TXN-0001")
    e3.drain(inst)
    case = _one_case(inst, ref)
    got = e3.view(inst, case["item_id"])
    assert sorted(got) == sorted(e3.VIEW_KEYS)
    assert (got["case_id"], got["case_version"], got["case_state"], got["item_kind"],
            got["class"], got["reason"]) == (case["case_id"], 1, "open",
                                            "eligibility_inactive", "inactive",
                                            "coverage_inactive")
    summary = got["summary"]
    assert sorted(summary) == sorted(e3.SUMMARY_KEYS)
    assert (summary["payer_name"], summary["plan"], summary["copay"],
            summary["deductible_remaining"], summary["referral_required"],
            summary["auth_required"]) == ("Synthetic Payer", "SYN-PPO", "25.00", "400.00",
                                          True, False)
    text = str(got)
    for leak in ("1000.00", "coinsurance", "deductible_total", "SYN-TXN-0001", "txn_ref",
                 "M000001", "Synthetic Patient", "1970-01-01"):
        assert leak not in text, leak
    assert [(n["seq"], n["class"], n["reason"]) for n in got["notes"]] == [
        (1, "inactive", "coverage_inactive")]


def test_el3a_p14_view_refuses_other_ids_and_has_no_summary_for_manual(
        install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())
    e3.drain(inst)
    case = _one_case(inst, ref)
    assert e3.view(inst, case["item_id"])["summary"] is None
    nf = {"schema_version": 1, "error": "ITEM_NOT_FOUND"}
    assert e3.view(inst, str(uuid.uuid4())) == nf
    legacy = e3.rows(inst, "m05_followup_items", "item_kind NOT LIKE 'eligibility%%'")
    for row in legacy[:1]:
        assert e3.view(inst, row["item_id"]) == nf


def test_el3a_p14_view_summary_is_null_after_expiry(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    start = _start(1)
    ref, _, _ = e3.payer(inst, start, "inactive", "coverage_inactive",
                         payer_name="Synthetic Payer", plan="SYN-PPO")
    e3.drain(inst)
    case = _one_case(inst, ref)
    assert e3.view(inst, case["item_id"])["summary"] is not None
    e3.q(inst, "UPDATE {s}.m05_el_appointments SET start_at = start_at - interval '40 days'"
         " WHERE appointment_ref = %s", (ref,))
    e3.q(inst, "UPDATE {s}.m05_appointments SET start_at = start_at - interval '40 days'"
         " WHERE appointment_ref = %s", (ref,))
    assert e2.expire(inst)["expired"] >= 1
    got = e3.view(inst, case["item_id"])
    assert got["summary"] is None and got["reason"] == "coverage_inactive"


# --- E3a-P16 / P17 ------------------------------------------------------------------------

def test_el3a_p16_e2_p03_fixture_materialized(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _, _ = e3.payer(inst, _start(), "timeout", "payer_unavailable")
    e3.later(inst, ref, "previsit", "active", "coverage_active")
    assert e3.drain(inst) == {"opened": 1, "noted": 1}
    case = _one_case(inst, ref)
    assert (case["item_kind"], case["current_reason"]) == (
        "eligibility_inconclusive", "payer_unavailable")
    assert [(n["class"], n["reason"]) for n in e3.notes(inst, case["case_id"])] == [
        ("inconclusive", "payer_unavailable"), ("active", "coverage_active")]


def test_el3a_p17_view_of_another_tenants_item_is_not_found(
        install: e3.EL3AInstall, other: e3.EL3AInstall) -> None:
    a, b = e3.ready(install), e3.ready(other)
    ref_b, _ = e3.manual(b, _start())
    e3.drain(b)
    item_b = _one_case(b, ref_b)["item_id"]
    assert e3.view(a, item_b) == {"schema_version": 1, "error": "ITEM_NOT_FOUND"}
    assert e3.view(b, item_b)["item_id"] == item_b


# --- E3a-P18 / P18b exactly-once evidence (Codex obligation 1) ---------------------------

def _backlog(inst: e3.EL3AInstall) -> list[str]:
    """Pre-E3 settlements left unapplied: E1 manual (x3 reasons), E2 inactive, E2 active,
    and a note-only cancellation after an unresolved finding."""
    refs = [e3.manual(inst, _start(), r)[0] for r in ("nonpriority", "no_coverage",
                                                      "incomplete")]
    refs.append(e3.payer(inst, _start(), "inactive", "coverage_inactive")[0])
    refs.append(e3.payer(inst, _start(), "active", "coverage_active")[0])
    start = _start()
    ref, patient, _ = e3.payer(inst, start, "inconclusive", "member_id_not_recognized")
    el.put(inst, ref, patient, start, coverage=el.present(), status="cancelled")
    refs.append(ref)
    return refs


def _settlement_check(inst: e3.EL3AInstall, refs: list[str]) -> None:
    """Codex red review 1: the expected identities come from settled VERIFICATION rows
    (independent of case events): exactly one settlement observation per verification;
    every other observation is a finding or a note and carries no verification."""
    expected = e3.settled_verifications(inst, refs)
    assert expected, "the backlog must contain settlements"
    evidence = [ev for r in refs for ev in e3.evidence(inst, r)]
    settlements = [ev for ev in evidence if ev["metadata"]["record"] == "settlement"]
    got = [ev["metadata"]["verification_id"] for ev in settlements]
    assert sorted(got) == sorted(expected)  # each verification exactly once, none extra
    for ev in evidence:
        meta = ev["metadata"]
        assert sorted(meta) == sorted(e3.METADATA_KEYS)
        if meta["record"] != "settlement":
            assert meta["verification_id"] is None
        if meta["reason"] in e3.NOTE_ONLY:
            assert meta["record"] == "note" and meta["class"] is None


def test_el3a_p18_backlog_one_settlement_observation_per_verification_across_restart(
        install: e3.EL3AInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    from haloflow.m02.observation import EventFoundation

    inst = e3.ready(install)
    refs = _backlog(inst)
    pending = sum(len(e3.case_events(inst, r)) for r in refs)
    assert all(ev["applied_at"] is None for r in refs for ev in e3.case_events(inst, r))
    # E1 manual settlements carry no verification id on the case event (pre-E3 shape)
    manual_events = [ev for r in refs[:3] for ev in e3.case_events(inst, r)]
    assert manual_events and all(ev["verification_id"] is None for ev in manual_events)
    # restart: the third transaction fails after its append; earlier ones stay committed
    original = EventFoundation.append_observation_in
    seen = {"n": 0}

    async def third_fails(self: Any, binding: Any, observation: Any) -> Any:
        got = await original(self, binding, observation)
        seen["n"] += 1
        if seen["n"] == 3:
            raise RuntimeError("process died after the append")
        return got

    monkeypatch.setattr(EventFoundation, "append_observation_in", third_fails)
    with pytest.raises(RuntimeError):
        e3.drain(inst)
    assert len(e3.evidence(inst)) == 2
    monkeypatch.undo()
    got = e3.drain(inst)  # a fresh runtime (restart)
    assert sum(got.values()) == pending - 2
    assert e3.drain(inst) == {}  # replay: idle
    assert len(e3.evidence(inst)) == pending
    _settlement_check(inst, refs)
    for r in refs:
        for row in e3.case_events(inst, r):
            (ev,) = [x for x in e3.evidence(inst, r)
                     if x["metadata"]["case_event_id"] == row["event_id"]]
            assert ev["event_id"] == row["evidence_event_id"]
            assert ev["producer_dedupe_key"] == f"elcase:{uuid.UUID(row['event_id']).hex}"
    # the case-open effect stays distinct and linked: only "opened" evidence is a source
    sources = {i["source_event_id"] for i in e3.el_items(inst)}
    opened = {ev["event_id"] for ev in e3.evidence(inst) if ev["metadata"]["effect"] == "opened"}
    assert sources == opened


def test_el3a_p18c_coverage_not_received_is_a_finding_not_a_settlement(
        install: e3.EL3AInstall) -> None:
    inst = e3.ready(install, not_received_days=1)
    ref, patient = sa.new_id(), sa.new_id()
    el.put(inst, ref, patient, _start(0.5), coverage={"state": "pending"})
    el.tick(inst)
    e3.drain(inst)
    (ev,) = e3.evidence(inst, ref)
    assert (ev["metadata"]["record"], ev["metadata"]["verification_id"],
            ev["metadata"]["effect"]) == ("finding", None, "opened")
    assert e3.settled_verifications(inst, [ref]) == set()


@pytest.mark.parametrize("fail_at", ["after_append", "after_create"])
def test_el3a_p18b_a_failure_mid_transaction_rolls_back_and_the_rerun_is_exactly_once(
        install: e3.EL3AInstall, monkeypatch: pytest.MonkeyPatch, fail_at: str) -> None:
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05 import eligibility_cases

    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())

    class Boom(Exception):
        pass

    if fail_at == "after_append":
        original = EventFoundation.append_observation_in

        async def failing(self: Any, binding: Any, observation: Any) -> Any:
            await original(self, binding, observation)
            raise Boom()

        monkeypatch.setattr(EventFoundation, "append_observation_in", failing)
    else:
        original_call = eligibility_cases.EligibilityCaseService._call

        async def failing_call(self: Any, binding: Any, context: Any, key: str,
                               command: Any) -> Any:
            got = await original_call(self, binding, context, key, command)
            if key == "m05.eligibility.create_item":
                raise Boom()
            return got

        monkeypatch.setattr(eligibility_cases.EligibilityCaseService, "_call", failing_call)
    with pytest.raises((Boom, eligibility_cases.EligibilityCaseFailed)):
        e3.apply_one(inst)
    (row,) = e3.case_events(inst, ref)
    assert (row["applied_at"], row["effect"], row["evidence_event_id"]) == (None, None, None)
    assert e3.evidence(inst, ref) == [] and e3.cases(inst, ref) == [] \
        and e3.el_items(inst) == []
    monkeypatch.undo()
    assert e3.drain(inst) == {"opened": 1}
    assert len(e3.evidence(inst, ref)) == 1 and len(e3.el_items(inst)) == 1


# --- E3a-P19 no legacy resolution (Codex obligation 3) -----------------------------------

def _legacy_counts(inst: e3.EL3AInstall, item: str) -> tuple[Any, ...]:
    return (e3.rows(inst, "m05_followup_items", "item_id = %s::uuid", (item,)),
            e3.count(inst, "m05_followup_history", "item_id = %s", (item,)),
            e3.count(inst, "m05_followup_actions", "item_id = %s", (item,)),
            e3.count(inst, "patient_events",
                     "event_type = 'm05_followup_resolution_asserted'"))


@pytest.mark.parametrize("path", ["resolve", "claim_and_resolve"])
def test_el3a_p19_an_eligibility_item_cannot_be_resolved_through_the_existing_staff_path(
        install: e3.EL3AInstall, path: str) -> None:
    """v1r1 (Codex red review 2): the refusal is pinned (AppendFailed COMMAND_MALFORMED,
    the contract v2 item_kind enum), a comparable LEGACY item succeeds through the same
    actor, profile and path, and the eligibility item's state and assignee are unchanged.
    Green correction G-1: the v1r1 legacy item came from an S-D unclear reply, whose
    `sa.fresh_patient` seeding calls `m05_directory_put` (t038), closed to runtime by later
    packages on this install (InsufficientPrivilege); the legacy item is now an S-I check
    item made through the existing append + `m05.followup.create` path.
    Green correction G-2 (measured): the refusal happens EARLIER than v1r1 pinned - the
    existing staff service checks the NEW decision's item kind against its closed staff
    kind set and raises ActionFailed("ACTION_RESULT_INVALID") before any key use, M02
    append or apply; v1r1 predicted AppendFailed COMMAND_MALFORMED at the append."""
    import sd_support as sd
    import si_support as si

    from haloflow.m05.service import ActionFailed, ResolveAction

    inst = e3.ready(install)
    sd.begin(inst)  # type: ignore[arg-type]
    ref, _ = e3.manual(inst, _start())
    e3.drain(inst)
    item = _one_case(inst, ref)["item_id"]

    async def act(staff: Any, target: uuid.UUID, version: int) -> Any:
        return await staff.chain.staff.act(await staff.chain.actor(), ResolveAction(
            item_id=target, client_action_id=uuid.uuid4(), expected_version=version,
            type=path, outcome_code="reached_no_change", note=None))

    async def body() -> None:
        async with e3.staff_runtime(inst) as rt:
            staff = si.StaffChain(inst, rt)
            # the comparable legacy item (an S-I check item through the existing append +
            # create path), resolved by the same actor, profile and path
            legacy = await e3.legacy_check_item(inst, rt)
            version = 1
            if path == "resolve":
                assert (await staff.chain.claim(legacy)).outcome == "applied"
                assert (await staff.chain.claim(uuid.UUID(item))).outcome == "applied"
                version = 2
            ok = await act(staff, legacy, version)
            assert (ok.outcome, ok.snapshot["state"]) == ("applied", "resolved")
            before = _legacy_counts(inst, item)
            with pytest.raises(ActionFailed) as raised:
                await act(staff, uuid.UUID(item), version)
            assert raised.value.reason_code == "ACTION_RESULT_INVALID"
            assert _legacy_counts(inst, item)[:3] == before[:3]

    asyncio.run(body())
    (row,) = e3.rows(inst, "m05_followup_items", "item_id = %s::uuid", (item,))
    if path == "claim_and_resolve":
        assert (row["state"], row["assignee_id"], row["version"]) == ("open", None, 1)
    else:
        assert (row["state"], row["assignee_id"], row["version"]) == ("open", sd.STAFF, 2)
    assert row["outcome_code"] is None
    assert e3.count(inst, "m05_followup_history",
                    "item_id = %s AND resolution_event_id IS NOT NULL", (item,)) == 0
    assert e3.count(inst, "m05_followup_actions",
                    "item_id = %s AND requested_action IN ('resolve', 'claim_and_resolve')",
                    (item,)) == 0
    # exactly one resolution event exists: the legacy item's
    assert e3.count(inst, "patient_events",
                    "event_type = 'm05_followup_resolution_asserted'") == 1
    assert _one_case(inst, ref)["state"] == "open"


# --- E3a-C06 concurrent appliers ----------------------------------------------------------

def test_el3a_c06_two_appliers_overlap_on_one_appointment_and_serialize(
        install: e3.EL3AInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    """v1r1 (Codex red review 3): forced overlap. Applier A is paused INSIDE its first
    transaction (after the gateway locked the appointment key, at the M02 append) until
    applier B has run to completion against the same appointment; B must apply nothing for
    it (the key row is locked: SKIP LOCKED). Then A resumes and drains. Both finish."""
    from haloflow.m02.observation import EventFoundation

    inst = e3.ready(install)
    start = _start()
    ref, patient, _ = e3.payer(inst, start, "inconclusive", "subscriber_not_found")
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    el.put(inst, ref, patient, start, coverage=el.present(), status="cancelled")
    total = len(e3.case_events(inst, ref))
    assert total == 3

    a_holds = threading.Event()
    b_done = threading.Event()
    original = EventFoundation.append_observation_in

    async def pausing(self: Any, binding: Any, observation: Any) -> Any:
        if threading.current_thread().name == "applier-A" and not a_holds.is_set():
            a_holds.set()
            finished = await asyncio.get_running_loop().run_in_executor(None, b_done.wait, 60)
            # Codex red v1r1 hardening: A resumes only with evidence that B finished first.
            assert finished is True, "B did not finish before A resumed"
        return await original(self, binding, observation)

    monkeypatch.setattr(EventFoundation, "append_observation_in", pausing)
    results: dict[str, dict[str, int]] = {}
    errors: list[BaseException] = []

    def run(name: str) -> None:
        try:
            results[name] = e3.drain(inst)
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)
        finally:
            if name == "applier-B":
                b_done.set()

    ta = threading.Thread(target=run, args=("applier-A",), name="applier-A")
    ta.start()
    assert a_holds.wait(60), "A never reached its append inside the transaction"
    # A holds the appointment key now: nothing of this appointment is applied yet
    assert all(r["applied_at"] is None for r in e3.case_events(inst, ref))
    tb = threading.Thread(target=run, args=("applier-B",), name="applier-B")
    tb.start()
    tb.join(60)
    assert not tb.is_alive(), "B did not finish while A held the key"
    assert results["applier-B"] == {}  # B applied nothing for the locked appointment
    ta.join(60)
    assert not ta.is_alive(), "A did not finish"
    assert errors == []
    assert sum(results["applier-A"].values()) == total
    case = _one_case(inst, ref)
    assert [n["seq"] for n in e3.notes(inst, case["case_id"])] == [1, 2, 3]
    assert case["case_version"] == total
    assert len(e3.evidence(inst, ref)) == total and len(e3.el_items(inst)) == 1


# --- E3a-P20 / P21 / P22 / P23 / P24 / P25 -----------------------------------------------

def test_el3a_p20_privileges_and_catalogue(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    fns = {name for (name,) in e3.q(
        inst, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND has_function_privilege('haloflow_runtime', p.oid,"
        " 'EXECUTE') AND p.proname IN ('m05_create_item_v3', 'm05_el_apply',"
        " 'm05_el_case_view')", (inst.schema,))}
    assert fns == {"m05_create_item_v3", "m05_el_apply", "m05_el_case_view"}
    for table in ("m05_el_cases", "m05_el_case_notes"):
        (privs,) = e3.q(inst, "SELECT has_table_privilege('haloflow_runtime', %s, 'SELECT')"
                        " OR has_table_privilege('haloflow_runtime', %s, 'INSERT')"
                        " OR has_table_privilege('haloflow_runtime', %s, 'UPDATE')",
                        (f"{inst.schema}.{table}",) * 3)
        assert privs == (False,)
    keys = set(e3.el3a_catalog().catalog.keys())
    assert set(e3.STATEMENT_KEYS) <= keys
    from haloflow.composition import APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert frozenset({36}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS


def test_el3a_p21_refusals_write_nothing(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())
    before = e3.case_events(inst, ref)
    base = {"schema_version": 1, "attribution": sa.attribution(sa.W_TIMER)}
    sa.refused(inst, "m05_el_apply", {**base, "limit": 1}, "COMMAND_MALFORMED")
    sa.refused(inst, "m05_el_apply", {"schema_version": 1,
                                      "attribution": sa.attribution(sa.W_OTHER)},
               "WORKLOAD_NOT_AUTHORIZED")
    sa.refused(inst, "m05_el_apply", {"schema_version": 1,
                                      "attribution": sa.attribution(str(uuid.uuid4()),
                                                                    kind="actor")},
               "WORKLOAD_NOT_AUTHORIZED")
    assert e3.case_events(inst, ref) == before and e3.cases(inst, ref) == []


def test_el3a_p22_activation_off_still_delivers_settled_findings(
        install: e3.EL3AInstall) -> None:
    """D-R2 (Codex agreed): already-settled findings drain after eligibility checking is
    switched off; workload authorization is unchanged (P21) and no new payer call is made:
    a checkable appointment's verification is refused at dispatch with no attempt row."""
    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())
    checkable, _ = e2.enrol(inst, _start())
    e2.due(inst)
    vid = e2.own_verification(inst, checkable, "booking")
    el.el_settings(inst, enabled=False, lead_days=3, not_received_days=1,
                   priority=el.PRIORITY)
    assert e3.drain(inst) == {"opened": 1}
    assert len(e3.el_items(inst)) == 1
    claimed = e2.claim(inst, vid)
    assert claimed["outcome"] == "claimed"
    assert e2.dispatch(inst, vid, claimed["epoch"])["outcome"] == "activation_off"
    assert e2.slots(inst, checkable) == 0  # no attempt row: no payer call can follow
    assert e3.case_events(inst, checkable) == []


def test_el3a_p23_no_coverage_sentinel_reaches_cases_notes_evidence_or_items(
        install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    _backlog(inst)
    e3.drain(inst)
    text = " ".join(str(r) for table in ("m05_el_cases", "m05_el_case_notes",
                                         "m05_followup_items")
                    for r in e3.rows(inst, table)) + " ".join(str(r) for r in e3.evidence(inst))
    for sentinel in SENTINELS:
        assert sentinel not in text, sentinel


def test_el3a_p24_create_item_v3_rules(install: e3.EL3AInstall) -> None:
    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())
    e3.drain(inst)
    case = _one_case(inst, ref)
    cmd = {"schema_version": 1, "source_event_id": case["source_event_id"],
           "item_kind": "eligibility_manual", "attribution": sa.attribution(sa.W_TIMER)}
    again = sa.call(inst, "m05_create_item_v3", cmd)
    assert again == {"schema_version": 1, "item_id": case["item_id"],
                     "created_or_existing": "existing"}
    # an evidence event that opened nothing (effect none) is not an eligible source
    ref2, _, _ = e3.payer(inst, _start(), "active", "coverage_active")
    e3.drain(inst)
    (ev,) = e3.evidence(inst, ref2)
    sa.refused(inst, "m05_create_item_v3", {**cmd, "source_event_id": ev["event_id"]},
               "SOURCE_NOT_ELIGIBLE")
    sa.refused(inst, "m05_create_item_v3", {**cmd, "item_kind": "eligibility_inactive"},
               "KIND_MISMATCH")
    assert len(e3.el_items(inst)) == 1


def test_el3a_p25_scheduler_runs_the_apply_phase_after_expire(install: e3.EL3AInstall) -> None:
    from haloflow.m05.eligibility import EligibilityWorker
    from haloflow.m05.eligibility_scheduler import EligibilityScheduler
    from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

    inst = e3.ready(install)
    ref, _ = e3.manual(inst, _start())

    async def body() -> None:
        async with e3.worker_runtime(inst) as (service, context), \
                e3.case_runtime(inst) as (cases, case_context):
            port = SimulatedClearinghouse(clock=lambda: datetime.now(UTC))
            worker = EligibilityWorker(service, port, call_timeout_s=5)
            scheduler = EligibilityScheduler(contexts=context, service=service, worker=worker,
                                             tenants=(inst.tenant_id,), cases=cases,
                                             case_contexts=case_context)
            result = await scheduler.tick()
            (report,) = result.tenants
            assert report.apply.counts == {"opened": 1, "noted": 0, "none": 0}
            plain = EligibilityScheduler(contexts=context, service=service, worker=worker,
                                         tenants=(inst.tenant_id,))
            (again,) = (await plain.tick()).tenants
            assert again.apply.counts == {"opened": 0, "noted": 0, "none": 0}

    asyncio.run(body())
    assert len(e3.el_items(inst)) == 1 and _one_case(inst, ref)["state"] == "open"


def test_el3a_p25b_apply_phase_refuses_another_tenants_case_context(
        install: e3.EL3AInstall, other: e3.EL3AInstall) -> None:
    """Codex E3a green review (2026-11-20): while scheduling tenant A, the case-context
    factory returns a valid context for tenant B. The apply phase refuses before drain: B's
    pending case event stays unapplied (no case, no item, no evidence), A reports zero apply
    counts under TENANT_MISMATCH. Matched control: B scheduled with B's context opens B's case."""
    from haloflow.m05.eligibility import EligibilityWorker
    from haloflow.m05.eligibility_scheduler import EligibilityScheduler
    from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

    a, b = e3.ready(install), e3.ready(other)
    ref_b, _ = e3.manual(b, _start())
    events_before = e3.case_events(b, ref_b)
    assert events_before, "B must have a pending case event"

    async def body() -> None:
        async with e3.worker_runtime(a) as (service_a, context_a), \
                e3.case_runtime(b) as (cases_b, case_context_b):
            port = SimulatedClearinghouse(clock=lambda: datetime.now(UTC))
            worker = EligibilityWorker(service_a, port, call_timeout_s=5)
            wrong = EligibilityScheduler(contexts=context_a, service=service_a, worker=worker,
                                         tenants=(a.tenant_id,), cases=cases_b,
                                         case_contexts=case_context_b)
            (report,) = (await wrong.tick()).tenants
            assert report.tenant == a.tenant_id
            assert report.apply.status == "TENANT_MISMATCH"
            assert report.apply.counts == {"opened": 0, "noted": 0, "none": 0}
            assert report.code == "DEGRADED"

    asyncio.run(body())
    assert e3.cases(b, ref_b) == [] and e3.el_items(b) == [] and e3.evidence(b, ref_b) == []
    assert e3.case_events(b, ref_b) == events_before
    assert e3.el_items(a) == [] and e3.evidence(a) == []

    async def control() -> None:
        async with e3.worker_runtime(b) as (service_b, context_b), \
                e3.case_runtime(b) as (cases_b, case_context_b):
            port = SimulatedClearinghouse(clock=lambda: datetime.now(UTC))
            worker = EligibilityWorker(service_b, port, call_timeout_s=5)
            right = EligibilityScheduler(contexts=context_b, service=service_b, worker=worker,
                                         tenants=(b.tenant_id,), cases=cases_b,
                                         case_contexts=case_context_b)
            (report,) = (await right.tick()).tenants
            assert report.apply.status == "ok"
            assert report.apply.counts == {"opened": 1, "noted": 0, "none": 0}

    asyncio.run(control())
    assert _one_case(b, ref_b)["state"] == "open" and len(e3.el_items(b)) == 1


_P25C_TABLES = ("m05_el_verifications", "m05_el_attempts", "m05_el_case_events",
                "m05_el_cases", "m05_followup_items")


def _snapshot(inst: e3.EL3AInstall) -> dict[str, list[str]]:
    return {t: sorted(str(r) for r in e3.rows(inst, t)) for t in _P25C_TABLES}


def test_el3a_p25c_pass_context_for_another_tenant_is_refused(
        install: e3.EL3AInstall, other: e3.EL3AInstall) -> None:
    """Codex dcd06e8c: while scheduling tenant A, the worker-context factory returns a valid
    context for tenant B. A is refused before due/expire/apply (context TENANT_MISMATCH,
    phases NOT_RUN); B's eligibility rows are unchanged. Matched control: A's own context
    runs A to COMPLETE."""
    from haloflow.m05.eligibility import EligibilityWorker
    from haloflow.m05.eligibility_scheduler import EligibilityScheduler
    from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

    a, b = e3.ready(install), e3.ready(other)
    e3.manual(b, _start())
    before = _snapshot(b)

    async def body(use_b: bool) -> Any:
        async with e3.worker_runtime(a) as (service_a, context_a), \
                e3.worker_runtime(b) as (_service_b, context_b), \
                e3.case_runtime(a) as (cases_a, case_context_a):
            port = SimulatedClearinghouse(clock=lambda: datetime.now(UTC))
            worker = EligibilityWorker(service_a, port, call_timeout_s=5)
            scheduler = EligibilityScheduler(
                contexts=context_b if use_b else context_a, service=service_a,
                worker=worker, tenants=(a.tenant_id,), cases=cases_a,
                case_contexts=case_context_a)
            (report,) = (await scheduler.tick()).tenants
            return report

    wrong = asyncio.run(body(True))
    assert (wrong.tenant, wrong.code, wrong.context) == (a.tenant_id, "DEGRADED",
                                                         "TENANT_MISMATCH")
    assert (wrong.run.status, wrong.expire.status, wrong.apply.status) == ("NOT_RUN",) * 3
    assert _snapshot(b) == before
    right = asyncio.run(body(False))
    assert (right.code, right.context, right.run.status, right.expire.status,
            right.apply.status) == ("COMPLETE", "ok", "ok", "ok", "ok")
    assert _snapshot(b) == before


_ = (timedelta, threading)
