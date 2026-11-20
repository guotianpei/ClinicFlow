"""Eligibility E3b - PostgreSQL rows, tranche 1 (RED BY DESIGN).

The E3b install (target 125) fails before any row body runs its scenario (TypeError on
`include_el3b`), so every row below is red until E3b source exists; the v3 parity oracle runs
on a separate E3a install (target 121) and is real now. Interface pins: the el3b_support
docstring, pins v1 + v1r1.

Tranche 1 rows:
- E3b-P01a refusal precedence parity (F1): bad W1-shaped code, and `other` without a note,
  on an unknown item -> v4 prepare result == v3 prepare result (COMMAND_MALFORMED,
  NOTE_REQUIRED), measured against v3 on an E3a install.
- E3b-P01b legacy resolution through StaffServiceV4 == through StaffServiceV3 (E3a install)
  for every W1 code on an `ehr_confirmation_check` item: outcome, state, outcome code,
  history actions, resolution contract version 2.
- E3b-P02 wrong-kind codes -> COMMAND_MALFORMED: eligibility item + W1 code; legacy item +
  OD-EL6 code; legacy item + el_other WITHOUT a note (F1: never NOTE_REQUIRED).
- E3b-P03 eligibility item + el_other: without a note NOTE_REQUIRED; with a note applied,
  the note text in no row of history, item, case or event.
- E3b-P04 runtime EXECUTE leaves t090 / t091; v4 is executable by the runtime role.
- E3b-P09 replay of the original close after a later case opened: stored result, ZERO new
  items, the later case untouched.
- E3b-P10 close sets closure_outcome, closes the case, watermark = next_seq - 1, resolution
  observation at contract v3; a later unresolved finding opens exactly one new case + item.
- E3b-P11 log_contact: each of the four codes on an open item; state / assignee / outcome /
  item version / case_version unchanged; one contact entry each; replay idempotent.
- E3b-P12 log_contact refused on a legacy item, on a resolved eligibility item, with an
  unknown code; zero contact rows.
- E3b-P13 log_contact token: missing -> COMMAND_MALFORMED; stale -> case_changed, nothing
  recorded; matching -> recorded, case_version unchanged.
- E3b-P17 action on another tenant's item -> the ordinary missing-item refusal (the code a
  random unknown id gets); zero effects in both tenants.
- E3b-C04 exact binding: two cases on one appointment (A closed, B open), A's token on B's
  item -> case_changed, B open, zero side effects; a token naming a case of ANOTHER
  appointment -> case_changed, that case untouched.

Tranche 2 rows (this file): P01c (the four reminder kinds x six W1 codes, v4 == v3), P15
(log_contact standing == start_progress, v3 oracle), C01-C03 (W16 unapplied / W17 applied but
unseen x resolve / claim_and_resolve: committed projection read from a separate transaction, no
closure effect, refreshed close once, replay), C05a / C05b (forced overlap in both orders,
Codex d2c52494: the close is OBSERVED waiting on a lock in m05_prepare_action_v4; the applier
COMPLETES without applying independently visible pending work while the close holds the
key). Routed rows: test_el3b_routes_postgres.py.

Mutant killers (test cases v1 E3-M rows; the el3b mutation harness is wired at green, as E1-E3a):
- M01 the (kind, code) membership check moved BEFORE the item lock -> P01a (legacy precedence
  for W1 inputs must stay v3's), P02 legacy_item_el_other_no_note (COMMAND_MALFORMED, not
  NOTE_REQUIRED), P03.
- M02 the (kind, code) membership check skipped -> P02 (all three cases).
- M03 watermark := next_seq WITHOUT projecting first -> C01-C03 (W16), P10 (next_seq - 1).
- M04 CASE_CHANGED rolls back its projection -> C01-C03 (projection committed, read from a
  separate transaction), P13 (stale contact).
- M06 log_contact allowed on a legacy kind -> P12 legacy_item.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import el2_support as e2
import el3a_support as e3
import el3b_support as e4
import m02_support
import pytest
import sa_support as sa

NOTE_TEXT = "CANARY-e3b-7f2a Mrs Z Example 555-0123"


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[e4.EL3BInstall]:
    yield from e4.module_install(request, m02_ids, m05_reset, f"el3b:{request.node.name}")


@pytest.fixture
def other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
          m05_reset: Any) -> Iterator[e4.EL3BInstall]:
    yield from e4.module_install(request, m02_ids, m05_reset, f"el3b-o:{request.node.name}")


@pytest.fixture
def el3a(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
         m05_reset: Any) -> Iterator[e3.EL3AInstall]:
    yield from e3.module_install(request, m02_ids, m05_reset, f"el3b-v3:{request.node.name}")


def _start(days: float = 20) -> datetime:
    return e2.future(days)


def _begin(inst: Any) -> Any:
    import sd_support as sd

    e3.ready(inst)
    sd.begin(inst)
    return inst


def _el_item(inst: Any) -> tuple[str, uuid.UUID]:
    """One open eligibility case and item (an E2 payer settlement, inactive, applied);
    later findings on it go through `e3.later` (previsit)."""
    ref, _, _ = e3.payer(inst, _start(), "inactive", "coverage_inactive")
    e3.drain(inst)
    (case,) = [c for c in e3.cases(inst, ref) if c["state"] == "open"]
    return ref, uuid.UUID(case["item_id"])


def _prepare(inst: Any, function: str, outcome: str, note_present: bool) -> dict[str, Any]:
    from test_m05_actions_postgres import _cmd

    cmd = _cmd(str(uuid.uuid4()), 1, "resolve",
               action_extra={"outcome_code": outcome, "note_present": note_present})
    return sa.call(inst, function, cmd)


# --- E3b-P01a precedence parity (F1) ------------------------------------------------------

@pytest.mark.parametrize(("outcome", "note_present", "code"), [
    ("bogus_code", False, "COMMAND_MALFORMED"),
    ("other", False, "NOTE_REQUIRED"),
])
def test_el3b_p01a_legacy_refusal_precedence_equals_v3(
        install: e4.EL3BInstall, el3a: e3.EL3AInstall, outcome: str, note_present: bool,
        code: str) -> None:
    v3 = _prepare(_begin(el3a), "m05_prepare_action_v3", outcome, note_present)
    assert v3 == {"schema_version": 1, "decision": "REFUSED", "code": code}  # the oracle
    v4 = _prepare(_begin(install), "m05_prepare_action_v4", outcome, note_present)
    assert v4 == v3


# --- E3b-P01b legacy resolution parity ----------------------------------------------------

def _legacy_resolution(inst: Any, v4: bool, outcome: str) -> tuple[Any, ...]:
    note = "legacy note" if outcome == "other" else None

    async def body() -> tuple[Any, ...]:
        async with e4.staff_runtime(inst, el3b=v4) as rt:
            staff = e4.Staff(inst, rt, v4=v4)
            legacy = await e3.legacy_check_item(inst, rt)
            assert (await staff.claim(legacy)).outcome == "applied"
            res = await staff.legacy_resolve(legacy, 2, outcome, note=note)
            return legacy, res.outcome, res.snapshot["state"], res.snapshot["outcome_code"]

    legacy, *result = asyncio.run(body())
    actions = [r[0] for r in e3.q(inst, "SELECT action FROM {s}.m05_followup_history"
                                  " WHERE item_id = %s ORDER BY seq", (str(legacy),))]
    events = e3.rows(inst, "patient_events",
                     "event_type = 'm05_followup_resolution_asserted'")
    assert len(events) == 1
    return (*result, actions, events[0]["contract_version"],
            events[0]["metadata"]["outcome_code"])


@pytest.mark.parametrize("outcome", e4.W1_CODES)
def test_el3b_p01b_legacy_resolution_through_v4_equals_v3(
        install: e4.EL3BInstall, el3a: e3.EL3AInstall, outcome: str) -> None:
    expected = _legacy_resolution(_begin(el3a), False, outcome)
    assert expected[:3] == ("applied", "resolved", outcome) and expected[4] == 2  # oracle
    assert _legacy_resolution(_begin(install), True, outcome) == expected


def _kind_resolution(inst: Any, v4: bool, kind: str, outcome: str) -> tuple[Any, ...]:
    note = "legacy note" if outcome == "other" else None

    async def body() -> tuple[Any, ...]:
        async with e4.staff_runtime(inst, el3b=v4) as rt:
            staff = e4.Staff(inst, rt, v4=v4)
            target = await e4.legacy_item(inst, rt, kind)
            assert (await staff.claim(target)).outcome == "applied"
            res = await staff.legacy_resolve(target, 2, outcome, note=note)
            return target, res.outcome, res.snapshot["state"], res.snapshot["outcome_code"]

    target, *result = asyncio.run(body())
    actions = [r[0] for r in e3.q(inst, "SELECT action FROM {s}.m05_followup_history"
                                  " WHERE item_id = %s ORDER BY seq", (str(target),))]
    (event,) = e3.rows(inst, "patient_events",
                       "event_type = 'm05_followup_resolution_asserted'")
    return (*result, actions, event["contract_version"], event["metadata"]["item_kind"])


@pytest.mark.parametrize("outcome", e4.W1_CODES)
@pytest.mark.parametrize("kind", e4.HISTORICAL_KINDS)
def test_el3b_p01c_reminder_kinds_resolution_through_v4_equals_v3(
        install: e4.EL3BInstall, el3a: e3.EL3AInstall, kind: str, outcome: str) -> None:
    """Tranche 2: P01 for the four reminder kinds (fixture `el3b_support.legacy_item`)."""
    expected = _kind_resolution(_begin(el3a), False, kind, outcome)
    assert expected[:3] == ("applied", "resolved", outcome) and expected[5] == kind  # oracle
    assert _kind_resolution(_begin(install), True, kind, outcome) == expected


# --- E3b-P02 wrong-kind codes -------------------------------------------------------------

@pytest.mark.parametrize("case", ["el_item_w1_code", "legacy_item_el_code",
                                  "legacy_item_el_other_no_note"])
def test_el3b_p02_wrong_kind_codes_are_malformed(install: e4.EL3BInstall, case: str) -> None:
    inst = _begin(install)
    ref, el_item = _el_item(inst)

    async def body() -> str:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            from haloflow.m05.service import ActionRefused

            try:
                if case == "el_item_w1_code":
                    assert (await staff.claim(el_item)).outcome == "applied"
                    await staff.resolve(el_item, 2, "reached_no_change",
                                        await staff.token(el_item))
                else:
                    legacy = await e3.legacy_check_item(inst, rt)
                    assert (await staff.claim(legacy)).outcome == "applied"
                    outcome = "el_self_pay" if case == "legacy_item_el_code" else "el_other"
                    await staff.legacy_resolve(legacy, 2, outcome)
            except ActionRefused as refused:
                return str(refused.code)
            raise AssertionError("not refused")

    before = e4.side_effects(inst, ref, str(el_item))
    assert asyncio.run(body()) == "COMMAND_MALFORMED"
    after = e4.side_effects(inst, ref, str(el_item))
    if case != "el_item_w1_code":
        assert after == before
    assert e3.count(inst, "patient_events",
                    "event_type = 'm05_followup_resolution_asserted'") == 0


# --- E3b-P03 el_other note rule on an eligibility item ------------------------------------

def test_el3b_p03_el_other_note_required_then_protected(install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)

    async def body() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            assert (await staff.claim(item)).outcome == "applied"
            from haloflow.m05.service import ActionRefused

            with pytest.raises(ActionRefused) as raised:
                await staff.resolve(item, 2, "el_other", await staff.token(item))
            assert raised.value.code == "NOTE_REQUIRED"
            return await staff.resolve(item, 2, "el_other", await staff.token(item),
                                       note=NOTE_TEXT)

    res = asyncio.run(body())
    assert (res.outcome, res.snapshot["state"], res.snapshot["outcome_code"]) == (
        "applied", "resolved", "el_other")
    text = " ".join(str(r) for t in ("m05_followup_items", "m05_followup_history",
                                     "m05_el_cases", "m05_el_case_notes", "patient_events")
                    for r in e3.rows(inst, t))
    assert NOTE_TEXT not in text
    (case,) = e3.cases(inst, ref)
    assert (case["state"], case["closure_outcome"]) == ("closed", "el_other")


# --- E3b-P04 runtime closure --------------------------------------------------------------

def test_el3b_p04_v3_actions_closed_to_runtime_v4_open(install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    got = e3.q(inst, "SELECT p.proname, pg_catalog.has_function_privilege(%s, p.oid,"
               " 'EXECUTE') FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n"
               " ON n.oid = p.pronamespace WHERE n.nspname = %s AND p.proname IN"
               " ('m05_prepare_action_v3', 'm05_apply_action_v3', 'm05_prepare_action_v4',"
               " 'm05_apply_action_v4')", (sa.RUNTIME, inst.schema))
    assert dict(got) == {"m05_prepare_action_v3": False, "m05_apply_action_v3": False,
                         "m05_prepare_action_v4": True, "m05_apply_action_v4": True}


# --- E3b-P09 / P10 close, replay, later case ----------------------------------------------

def test_el3b_p10_close_sets_watermark_and_a_later_finding_opens_one_new_case(
        install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)

    async def close() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            assert (await staff.claim(item)).outcome == "applied"
            return await staff.resolve(item, 2, "el_verified_active", await staff.token(item))

    res = asyncio.run(close())
    assert (res.outcome, res.snapshot["state"]) == ("applied", "resolved")
    (case,) = e3.cases(inst, ref)
    assert (case["state"], case["closure_outcome"]) == ("closed", "el_verified_active")
    key = e4.key_row(inst, ref)
    assert key["closure_watermark"] == key["next_seq"] - 1
    (event,) = e3.rows(inst, "patient_events",
                       "event_type = 'm05_followup_resolution_asserted'")
    assert event["contract_version"] == 3
    assert event["metadata"]["item_kind"] in e4.EL_KINDS
    items_before = len(e3.el_items(inst))
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    e3.drain(inst)
    assert len(e3.el_items(inst)) == items_before + 1
    states = sorted(c["state"] for c in e3.cases(inst, ref))
    assert states == ["closed", "open"]


def test_el3b_p09_replay_of_the_close_after_a_later_case_creates_nothing(
        install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)
    key = uuid.uuid4()

    async def close(token: Any) -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            return await staff.resolve(item, 2, "el_verified_active", token, key=key)

    async def first() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            assert (await staff.claim(item)).outcome == "applied"
            return await staff.token(item)

    token = asyncio.run(first())
    original = asyncio.run(close(token))
    assert original.outcome == "applied"
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    e3.drain(inst)
    later = [c for c in e3.cases(inst, ref) if c["state"] == "open"]
    assert len(later) == 1
    snapshot = (len(e3.el_items(inst)), later, e3.count(
        inst, "patient_events", "event_type = 'm05_followup_resolution_asserted'"))
    replay = asyncio.run(close(token))
    assert replay.outcome == "replayed"
    assert dict(replay.snapshot) == dict(original.snapshot)
    assert (len(e3.el_items(inst)),
            [c for c in e3.cases(inst, ref) if c["state"] == "open"],
            e3.count(inst, "patient_events",
                     "event_type = 'm05_followup_resolution_asserted'")) == snapshot


# --- E3b-P11 .. P13 log_contact -----------------------------------------------------------

def test_el3b_p11_each_contact_code_records_an_entry_and_changes_nothing_else(
        install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)
    keys = {code: uuid.uuid4() for code in e4.CONTACT_LABELS}

    async def body() -> list[Any]:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            out = []
            for code, key in keys.items():
                out.append(await staff.contact(item, 1, code, await staff.token(item), key=key))
            replay = await staff.contact(item, 1, "el_left_message", await staff.token(item),
                                         key=keys["el_left_message"])
            out.append(replay)
            return out

    before_item = e4.item(inst, str(item))
    (before_case,) = e3.cases(inst, ref)
    results = asyncio.run(body())
    assert [r.outcome for r in results] == ["applied"] * 4 + ["replayed"]
    after_item = e4.item(inst, str(item))
    for field in ("state", "assignee_id", "outcome_code", "version"):
        assert after_item[field] == before_item[field], field
    (after_case,) = e3.cases(inst, ref)
    assert after_case["case_version"] == before_case["case_version"]
    assert e4.contact_entries(inst, str(item)) == list(e4.CONTACT_LABELS)
    assert e3.count(inst, "m05_followup_history",
                    "item_id = %s AND action = 'log_contact'", (str(item),)) == 4


def _resolved_refusal_oracle(inst: Any) -> str:
    """The existing state refusal for acting on a RESOLVED item: start_progress on a
    resolved legacy item through v3 (E3a install). Measured now."""
    from haloflow.m05.service import ActionRefused, StaffAction

    async def body() -> str:
        async with e4.staff_runtime(inst, el3b=False) as rt:
            staff = e4.Staff(inst, rt, v4=False)
            legacy = await e3.legacy_check_item(inst, rt)
            assert (await staff.claim(legacy)).outcome == "applied"
            assert (await staff.legacy_resolve(legacy, 2, "reached_no_change")).outcome \
                == "applied"
            action = StaffAction(item_id=legacy, client_action_id=uuid.uuid4(),
                                 expected_version=3, type="start_progress")
            actor = await staff.actor()
            try:
                await staff.chain.staff.act(actor, action)
            except ActionRefused as refused:
                return str(refused.code)
            raise AssertionError("start_progress on a resolved item was not refused")

    return asyncio.run(body())


@pytest.mark.parametrize("case", ["legacy_item", "resolved_item", "unknown_code"])
def test_el3b_p12_log_contact_refusals(install: e4.EL3BInstall, el3a: e3.EL3AInstall,
                                       case: str) -> None:
    """Codex tranche-1 review: the resolved-item code comes from the existing contract
    (oracle, pinned ITEM_RESOLVED); only the contact call itself may raise."""
    expected = "COMMAND_MALFORMED"
    if case == "resolved_item":
        expected = _resolved_refusal_oracle(_begin(el3a))
        assert expected == "ITEM_RESOLVED"
    from haloflow.m05.eligibility_staff import CaseToken
    from haloflow.m05.service import ActionRefused

    inst = _begin(install)
    ref, item = _el_item(inst)

    async def body() -> str:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            version = 1
            if case == "legacy_item":
                target = await e3.legacy_check_item(inst, rt)
                token = CaseToken(case_id=uuid.uuid4(), item_id=target, case_version=1)
            else:
                target, token = item, await staff.token(item)
            if case == "resolved_item":
                assert (await staff.claim(item)).outcome == "applied"
                assert (await staff.resolve(item, 2, "el_self_pay", token)).outcome == "applied"
                token, version = await staff.token(item), 3
            code = "el_unknown_code" if case == "unknown_code" else "el_left_message"
            contact = staff.contact(target, version, code, token)
            try:
                await contact
            except ActionRefused as refused:
                return str(refused.code)
            return "applied"

    assert asyncio.run(body()) == expected
    assert e3.count(inst, "m05_contact_entries") == 0


def test_el3b_p13_log_contact_token_missing_stale_matching(install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)

    async def missing() -> str:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            from haloflow.m05.service import ActionRefused

            with pytest.raises(ActionRefused) as raised:
                await staff.contact(item, 1, "el_left_message", None)
            return str(raised.value.code)

    assert asyncio.run(missing()) == "COMMAND_MALFORMED"

    async def stale() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            return await staff.token(item)

    old = asyncio.run(stale())
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")  # unapplied finding
    before = e4.side_effects(inst, ref, str(item))

    async def act(token: Any) -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).contact(item, 1, "el_left_message", token)

    changed = asyncio.run(act(old))
    assert changed.outcome == "case_changed"
    assert int(changed.snapshot["case_version"]) == old.case_version + 1  # projected
    assert e4.side_effects(inst, ref, str(item)) == before
    (case,) = e3.cases(inst, ref)
    assert case["case_version"] == old.case_version + 1  # the projection is committed

    async def fresh() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            return await staff.contact(item, 1, "el_left_message", await staff.token(item))

    assert asyncio.run(fresh()).outcome == "applied"
    assert e4.contact_entries(inst, str(item)) == ["el_left_message"]
    (case,) = e3.cases(inst, ref)
    assert case["case_version"] == old.case_version + 1  # a contact entry is not a finding


# --- E3b-P17 cross-tenant -----------------------------------------------------------------

def test_el3b_p17_cross_tenant_ids_and_tokens(install: e4.EL3BInstall,
                                               other: e4.EL3BInstall) -> None:
    """Codex tranche-1 review: an unknown id and another tenant's item id are each refused
    exactly ITEM_NOT_FOUND (resolve and contact); a LOCAL item with a token naming the other
    tenant's case is case_changed; both tenants' durable state is unchanged."""
    from haloflow.m05.eligibility_staff import CaseToken
    from haloflow.m05.service import ActionRefused

    a, b = _begin(install), _begin(other)
    ref_a, item_a = _el_item(a)
    ref_b, item_b = _el_item(b)
    (case_b,) = e3.cases(b, ref_b)
    before = (e4.side_effects(a, ref_a, str(item_a)), e4.side_effects(b, ref_b, str(item_b)))

    async def refusals(target: uuid.UUID) -> list[str]:
        out = []
        async with e4.staff_runtime(a) as rt:
            staff = e4.Staff(a, rt)
            token = CaseToken(case_id=uuid.uuid4(), item_id=target, case_version=1)
            for make in (lambda: staff.resolve(target, 1, "el_self_pay", token,
                                               kind="claim_and_resolve"),
                         lambda: staff.contact(target, 1, "el_left_message", token)):
                call = make()
                try:
                    await call
                    out.append("applied")
                except ActionRefused as refused:
                    out.append(str(refused.code))
        return out

    assert asyncio.run(refusals(uuid.uuid4())) == ["ITEM_NOT_FOUND", "ITEM_NOT_FOUND"]
    assert asyncio.run(refusals(item_b)) == ["ITEM_NOT_FOUND", "ITEM_NOT_FOUND"]

    async def foreign_token() -> list[str]:
        async with e4.staff_runtime(a) as rt:
            staff = e4.Staff(a, rt)
            token = CaseToken(case_id=uuid.UUID(case_b["case_id"]), item_id=item_a,
                              case_version=int(case_b["case_version"]))
            r1 = await staff.resolve(item_a, 1, "el_self_pay", token, kind="claim_and_resolve")
            r2 = await staff.contact(item_a, 1, "el_left_message", token)
            return [r1.outcome, r2.outcome]

    assert asyncio.run(foreign_token()) == ["case_changed", "case_changed"]
    assert (e4.side_effects(a, ref_a, str(item_a)),
            e4.side_effects(b, ref_b, str(item_b))) == before
    assert e3.count(a, "m05_contact_entries") == 0 and e3.count(b, "m05_contact_entries") == 0


# --- E3b-C04 exact binding ----------------------------------------------------------------

def _bound_act(inst: Any, item: uuid.UUID, token: Any, version: int = 1) -> list[str]:
    async def body() -> list[str]:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            r1 = await staff.resolve(item, version, "el_self_pay", token,
                                     kind="claim_and_resolve")
            r2 = await staff.contact(item, version, "el_left_message", token)
            return [r1.outcome, r2.outcome]

    return asyncio.run(body())


def test_el3b_c04_exact_binding_and_no_lock_on_a_named_foreign_case(
        install: e4.EL3BInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    """v1r1 T1 + Codex tranche-1 review. Appointment R: case A closed, case B open, with A's
    case_version forced equal to B's; appointment R2: case X open. Each of these is
    case_changed for resolve AND contact, with zero side effects on B, X and A:
    (1) B's item + A's case id; (2) B's item + X's case (another appointment);
    (3) reverse: X's item + B's case; (4) B's item + B's case but X's item id.
    Lock evidence for (2): while the action is paused right after its prepare statement
    (transaction open), a separate connection can lock X's case row and R2's key row NOWAIT,
    and cannot lock B's case row (the action holds it). (5) Matched control: B's item with
    its own token applies."""
    import threading

    from haloflow.m05.eligibility_staff import CaseToken

    inst = _begin(install)
    ref, first = _el_item(inst)
    ref2, x_item = _el_item(inst)

    async def close_first() -> None:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            assert (await staff.claim(first)).outcome == "applied"
            res = await staff.resolve(first, 2, "el_verified_active", await staff.token(first))
            assert res.outcome == "applied"

    asyncio.run(close_first())
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    e3.drain(inst)
    (case_b,) = [c for c in e3.cases(inst, ref) if c["state"] == "open"]
    (case_a,) = [c for c in e3.cases(inst, ref) if c["state"] == "closed"]
    (case_x,) = e3.cases(inst, ref2)
    item_b = uuid.UUID(case_b["item_id"])
    e3.q(inst, "UPDATE {s}.m05_el_cases SET case_version = %s WHERE case_id = %s",
         (int(case_b["case_version"]), case_a["case_id"]))
    vb, vx = int(case_b["case_version"]), int(case_x["case_version"])

    def tok(case: dict[str, Any], item: uuid.UUID, version: int) -> Any:
        return CaseToken(case_id=uuid.UUID(case["case_id"]), item_id=item,
                         case_version=version)

    before = (e4.side_effects(inst, ref, str(item_b)),
              e4.side_effects(inst, ref2, str(x_item)), e3.cases(inst, ref))
    assert _bound_act(inst, item_b, tok(case_a, item_b, vb)) == ["case_changed"] * 2   # (1)

    hold = e4.hold_after(monkeypatch, "m05.followup.prepare", "actor-c04")
    got: dict[str, Any] = {}

    def run() -> None:
        try:
            got["out"] = _bound_act(inst, item_b, tok(case_x, item_b, vx))             # (2)
        except BaseException as error:  # noqa: BLE001 - asserted below
            got["error"] = error
            hold.held.set()

    worker = threading.Thread(target=run, name="actor-c04")
    worker.start()
    assert hold.held.wait(60), "the action never reached its prepare statement"
    assert "error" not in got, got.get("error")
    probe = (e4.lockable(inst, "m05_el_cases", "case_id", case_x["case_id"]),
             e4.lockable(inst, "m05_el_appointments", "appointment_ref", ref2),
             e4.lockable(inst, "m05_el_cases", "case_id", case_b["case_id"]))
    hold.release.set()
    worker.join(60)
    assert not worker.is_alive() and "error" not in got, got.get("error")
    assert probe == (True, True, False)
    assert got["out"] == ["case_changed"] * 2
    monkeypatch.undo()

    assert _bound_act(inst, x_item, tok(case_b, x_item, vb)) == ["case_changed"] * 2   # (3)
    assert _bound_act(inst, item_b, tok(case_b, x_item, vb)) == ["case_changed"] * 2   # (4)
    assert (e4.side_effects(inst, ref, str(item_b)),
            e4.side_effects(inst, ref2, str(x_item)), e3.cases(inst, ref)) == before

    async def matched() -> Any:                                                         # (5)
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            return await staff.resolve(item_b, 1, "el_self_pay", await staff.token(item_b),
                                       kind="claim_and_resolve")

    assert asyncio.run(matched()).outcome == "applied"
    assert [c["state"] for c in e3.cases(inst, ref)].count("open") == 0


# --- E3b-P15 log_contact actor standing (tranche 2) ---------------------------------------

def _standing_code(inst: Any, v4: bool, condition: str) -> tuple[str, str | None]:
    """Claim an item as the staff member, change their standing (`deactivate` or `revoke`
    act access), then attempt start_progress (and, on v4, log_contact). Returns the two
    refusal codes ("applied" if not refused; None where not attempted)."""
    import m05_support as m05
    import staff_support as st

    from haloflow.m05.service import ActionRefused, StaffAction

    ref, item = _el_item(inst) if v4 else (None, None)

    async def body() -> tuple[str, str | None]:
        async with e4.staff_runtime(inst, el3b=v4) as rt:
            staff = e4.Staff(inst, rt, v4=v4)
            target = item if v4 else await e3.legacy_check_item(inst, rt)
            assert target is not None
            assert (await staff.claim(target)).outcome == "applied"
            token = await staff.token(target) if v4 else None
            if condition == "deactivate":
                st.admin_op(inst, st.admin_command("deactivate", m05.S1))
            else:
                st.admin_op(inst, st.admin_command("revoke", m05.S1, access="act"))

            async def code(call: Any) -> str:
                try:
                    await call
                    return "applied"
                except ActionRefused as refused:
                    return str(refused.code)

            progress = await code(staff.chain.staff.act(await staff.actor(), StaffAction(
                item_id=target, client_action_id=uuid.uuid4(), expected_version=2,
                type="start_progress")))
            contact = (await code(staff.contact(target, 2, "el_left_message", token))
                       if v4 else None)
            return progress, contact

    return asyncio.run(body())


@pytest.mark.parametrize("condition", ["deactivate", "revoke_act"])
def test_el3b_p15_log_contact_standing_equals_start_progress(
        install: e4.EL3BInstall, el3a: e3.EL3AInstall, condition: str) -> None:
    """Architecture v1r2 M4: log_contact uses the start_progress standing rule. Oracle: the
    start_progress refusal for the same standing change on the E3a install (v3, measured
    now); on E3b both start_progress and log_contact give exactly that code, and no contact
    entry is written."""
    expected, _ = _standing_code(_begin(el3a), False, condition)
    assert expected != "applied"  # the oracle refuses
    inst = _begin(install)
    progress, contact = _standing_code(inst, True, condition)
    assert (progress, contact) == (expected, expected)
    assert e3.count(inst, "m05_contact_entries") == 0


# --- E3b-C01 .. C03 version-bound closure (W16 / W17, both resolve paths; tranche 2) ------

@pytest.mark.parametrize("path", ["resolve", "claim_and_resolve"])
@pytest.mark.parametrize("scenario", ["w16_unapplied", "w17_applied_unseen"])
def test_el3b_c01_c03_stale_close_is_case_changed_then_a_refreshed_close_succeeds_once(
        install: e4.EL3BInstall, scenario: str, path: str) -> None:
    """Test cases v1 E3-C01..C03 + Codex v1r3 review. Staff hold a token for case_version v.
    W16: a newer unresolved finding (seq2) is committed but UNAPPLIED; W17: seq2 is already
    APPLIED but was never displayed. A close with the old token -> case_changed (a committed
    business response, the snapshot is the refreshed case view at v + 1); read from a SEPARATE
    transaction afterwards: the case is at v + 1 with seq2's note and seq2 applied (W16: the
    action projected it), and there is NO closure effect (item state / assignee / outcome /
    version, case state, watermark, history, resolution events, contact entries). The
    refreshed close then succeeds once; its replay creates nothing."""
    inst = _begin(install)
    ref, item = _el_item(inst)

    async def token() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            if path == "resolve":
                assert (await staff.claim(item)).outcome == "applied"
            return await staff.token(item)

    old = asyncio.run(token())
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")  # seq2, unapplied
    if scenario == "w17_applied_unseen":
        assert e3.drain(inst) == {"noted": 1}
    before = e4.side_effects(inst, ref, str(item))
    version = 2 if path == "resolve" else 1

    async def close(tok: Any, key: uuid.UUID) -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).resolve(item, version, "el_confirmed_inactive",
                                                    tok, kind=path, key=key)

    changed = asyncio.run(close(old, uuid.uuid4()))
    assert changed.outcome == "case_changed"
    assert int(changed.snapshot["case_version"]) == old.case_version + 1
    # a separate transaction (admin connection, after the call returned)
    (case,) = e3.cases(inst, ref)
    assert (case["state"], case["case_version"]) == ("open", old.case_version + 1)
    assert all(r["applied_at"] is not None for r in e3.case_events(inst, ref))
    assert len(e3.notes(inst, case["case_id"])) == 2
    after = e4.side_effects(inst, ref, str(item))
    assert after[0] == before[0] and after[2:] == before[2:]  # no closure effect
    assert [(c["state"], c.get("closure_outcome")) for c in e3.cases(inst, ref)] == [
        ("open", None)]

    async def refreshed() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).token(item)

    key = uuid.uuid4()
    fresh = asyncio.run(refreshed())
    first = asyncio.run(close(fresh, key))
    assert (first.outcome, first.snapshot["state"]) == ("applied", "resolved")
    replay = asyncio.run(close(fresh, key))
    assert replay.outcome == "replayed"
    (case,) = e3.cases(inst, ref)
    assert (case["state"], case["closure_outcome"]) == ("closed", "el_confirmed_inactive")
    assert e3.count(inst, "patient_events",
                    "event_type = 'm05_followup_resolution_asserted'") == 1
    key_row = e4.key_row(inst, ref)
    assert key_row["closure_watermark"] == key_row["next_seq"] - 1


# --- E3b-C05 applier vs staff close, forced overlap (tranche 2; Codex d2c52494 evidence) ----

def test_el3b_c05a_applier_holds_the_appointment_staff_close_waits_then_is_case_changed(
        install: e4.EL3BInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlap evidence + durable state. The applier (thread applier-A) is paused INSIDE its
    transaction at its M02 append for seq2, holding the appointment key. A staff close with the
    pre-seq2 token (thread staff-S) starts; BEFORE A is released, a separate connection must
    observe S's backend WAITING ON A LOCK in `m05_prepare_action_v4` (pg_stat_activity, bounded
    poll) and the key row must not be lockable. Then A is released (always, in finally): A
    applies seq2 (note, case_version + 1) and S finishes case_changed. Durable, exactly once:
    one open case, seq2 applied once with one note, no closure and no resolution event; a
    refreshed close then succeeds once and the watermark = next_seq - 1."""
    import threading
    import time

    from haloflow.m02.observation import EventFoundation

    inst = _begin(install)
    ref, item = _el_item(inst)

    async def tok() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).token(item)

    old = asyncio.run(tok())
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")  # seq2, unapplied
    a_holds, a_release = threading.Event(), threading.Event()
    original = EventFoundation.append_observation_in

    async def pausing(self: Any, binding: Any, observation: Any) -> Any:
        if threading.current_thread().name == "applier-A" and not a_holds.is_set():
            a_holds.set()
            released = await asyncio.get_running_loop().run_in_executor(
                None, a_release.wait, 60)
            assert released
        return await original(self, binding, observation)

    monkeypatch.setattr(EventFoundation, "append_observation_in", pausing)
    out: dict[str, Any] = {}

    def applier() -> None:
        try:
            out["applied"] = e3.drain(inst)
        except BaseException as error:  # noqa: BLE001 - asserted below
            out["applier_error"] = error
            a_holds.set()

    async def close() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).resolve(item, 1, "el_self_pay", old,
                                                    kind="claim_and_resolve")

    def staff() -> None:
        try:
            out["close"] = asyncio.run(close())
        except BaseException as error:  # noqa: BLE001 - asserted below
            out["staff_error"] = error

    ta = threading.Thread(target=applier, name="applier-A")
    ts = threading.Thread(target=staff, name="staff-S")
    waiting, key_locked = 0, None
    try:
        ta.start()
        assert a_holds.wait(60) and "applier_error" not in out, out.get("applier_error")
        ts.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and waiting == 0 and ts.is_alive():
            waiting = e4.lock_waiters(inst, "m05_prepare_action_v4")
            if waiting == 0:
                time.sleep(0.1)
        key_locked = not e4.lockable(inst, "m05_el_appointments", "appointment_ref", ref)
    finally:
        a_release.set()
        ta.join(60)
        ts.join(60)
    assert waiting >= 1, "the staff close never waited on a lock in m05_prepare_action_v4"
    assert key_locked is True
    assert not ta.is_alive() and not ts.is_alive()
    assert "applier_error" not in out and "staff_error" not in out, out
    assert out["applied"] == {"noted": 1}
    assert out["close"].outcome == "case_changed"
    (case,) = e3.cases(inst, ref)
    assert (case["state"], case["case_version"]) == ("open", old.case_version + 1)
    assert [n["seq"] for n in e3.notes(inst, case["case_id"])] == [1, 2]  # seq2 exactly once
    assert len(e3.evidence(inst, ref)) == 2
    assert e3.count(inst, "patient_events",
                    "event_type = 'm05_followup_resolution_asserted'") == 0
    monkeypatch.undo()

    async def refreshed() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff_ = e4.Staff(inst, rt)
            return await staff_.resolve(item, 1, "el_self_pay", await staff_.token(item),
                                        kind="claim_and_resolve")

    assert asyncio.run(refreshed()).outcome == "applied"
    assert [c["state"] for c in e3.cases(inst, ref)] == ["closed"]
    key = e4.key_row(inst, ref)
    assert key["closure_watermark"] == key["next_seq"] - 1


def test_el3b_c05b_staff_close_holds_the_appointment_applier_skips_pending_work(
        install: e4.EL3BInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse order, with INDEPENDENTLY VISIBLE pending work. seq2 is committed and
    unapplied. A staff close attempt (thread staff-S, pre-seq2 token) is paused right after its
    prepare statement: it holds the appointment key and has projected seq2 UNCOMMITTED. While
    it is paused: a separate connection still sees seq2 unapplied (applied_at NULL) and cannot
    lock the key row; an applier run (thread applier-B) COMPLETES within a bound and applies
    nothing (SKIP LOCKED, not blocked). S is released (always, in finally) and returns
    case_changed with the projection committed. Exactly once: seq2 applied once (one note, one
    evidence event); a further applier run finds nothing; a refreshed close succeeds once."""
    import threading

    inst = _begin(install)
    ref, item = _el_item(inst)

    async def tok() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).token(item)

    old = asyncio.run(tok())
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")  # seq2, unapplied
    (seq2,) = [r for r in e3.case_events(inst, ref) if r["applied_at"] is None]
    hold = e4.hold_after(monkeypatch, "m05.followup.prepare", "staff-S")
    out: dict[str, Any] = {}

    async def close() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).resolve(item, 1, "el_self_pay", old,
                                                    kind="claim_and_resolve")

    def staff() -> None:
        try:
            out["close"] = asyncio.run(close())
        except BaseException as error:  # noqa: BLE001 - asserted below
            out["staff_error"] = error
            hold.held.set()

    def applier() -> None:
        try:
            out["applied"] = e3.drain(inst)
        except BaseException as error:  # noqa: BLE001 - asserted below
            out["applier_error"] = error

    ts = threading.Thread(target=staff, name="staff-S")
    tb = threading.Thread(target=applier, name="applier-B")
    pending_visible = key_locked = None
    applier_done = False
    try:
        ts.start()
        assert hold.held.wait(60) and "staff_error" not in out, out.get("staff_error")
        pending_visible = [r["applied_at"] for r in e3.case_events(inst, ref)
                           if r["case_event_id"] == seq2["case_event_id"]] == [None]
        key_locked = not e4.lockable(inst, "m05_el_appointments", "appointment_ref", ref)
        tb.start()
        tb.join(30)
        applier_done = not tb.is_alive()
    finally:
        hold.release.set()
        ts.join(60)
        tb.join(60)
    assert pending_visible is True, "seq2 was not independently visible as pending"
    assert key_locked is True, "the close did not hold the appointment key"
    assert applier_done, "the applier blocked instead of skipping the locked appointment"
    assert "applier_error" not in out and "staff_error" not in out, out
    assert out["applied"] == {}
    assert out["close"].outcome == "case_changed"
    monkeypatch.undo()
    (case,) = e3.cases(inst, ref)
    assert (case["state"], case["case_version"]) == ("open", old.case_version + 1)
    assert [n["seq"] for n in e3.notes(inst, case["case_id"])] == [1, 2]  # seq2 exactly once
    assert len(e3.evidence(inst, ref)) == 2
    assert e3.drain(inst) == {}

    async def refreshed() -> Any:
        async with e4.staff_runtime(inst) as rt:
            staff_ = e4.Staff(inst, rt)
            return await staff_.resolve(item, 1, "el_self_pay", await staff_.token(item),
                                        kind="claim_and_resolve")

    assert asyncio.run(refreshed()).outcome == "applied"
    assert [c["state"] for c in e3.cases(inst, ref)] == ["closed"]
    key = e4.key_row(inst, ref)
    assert key["closure_watermark"] == key["next_seq"] - 1


def test_el3b_c05p_control_the_lock_wait_probe_sees_a_waiting_backend(
        el3a: e3.EL3AInstall) -> None:
    """CONTROL (passes now): `el3b_support.lock_waiters` counts a backend that is waiting on
    a row lock while running a statement containing the needle, and stops counting once the
    holder releases. Proves the C05a probe can observe a lock wait at all."""
    import threading
    import time

    import psycopg

    inst = _begin(el3a)
    ref, _ = e3.manual(inst, _start())
    needle = "e3b_c05p_" + uuid.uuid4().hex[:8]
    assert e4.lock_waiters(inst, needle) == 0
    with psycopg.connect(inst.ids.admin) as holder:
        holder.execute(sa.q(inst, "SELECT 1 FROM {s}.m05_el_appointments"
                                  " WHERE appointment_ref = %s FOR UPDATE"), (ref,))
        done = threading.Event()

        def waiter() -> None:
            with psycopg.connect(inst.ids.admin) as conn:
                conn.execute(sa.q(inst, f"SELECT 1 /* {needle} */ FROM {{s}}.m05_el_appointments"
                                        " WHERE appointment_ref = %s FOR UPDATE"), (ref,))
                conn.rollback()
            done.set()

        t = threading.Thread(target=waiter)
        t.start()
        seen, deadline = 0, time.monotonic() + 30
        try:
            while time.monotonic() < deadline and seen == 0:
                seen = e4.lock_waiters(inst, needle)
                if seen == 0:
                    time.sleep(0.05)
        finally:
            holder.rollback()
            t.join(30)
    assert seen == 1 and done.is_set()
    assert e4.lock_waiters(inst, needle) == 0
