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

Tranche 2 (next): C01-C03 (W16/W17 projection, resolve and claim_and_resolve), C05 (forced
overlap of applier and staff close), P15 (log_contact actor standing), routed closure through
the synthetic `resolve` route (resolve AND claim_and_resolve), P01 for the four reply kinds,
mutants M01-M04/M06.
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


@pytest.mark.parametrize("case", ["legacy_item", "resolved_item", "unknown_code"])
def test_el3b_p12_log_contact_refusals(install: e4.EL3BInstall, case: str) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)

    async def body() -> str:
        from haloflow.m05.eligibility_staff import CaseToken
        from haloflow.m05.service import ActionRefused

        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            try:
                if case == "legacy_item":
                    legacy = await e3.legacy_check_item(inst, rt)
                    await staff.contact(legacy, 1, "el_left_message",
                                        CaseToken(case_id=uuid.uuid4(), item_id=legacy,
                                                  case_version=1))
                elif case == "resolved_item":
                    assert (await staff.claim(item)).outcome == "applied"
                    token = await staff.token(item)
                    assert (await staff.resolve(item, 2, "el_self_pay", token)).outcome \
                        == "applied"
                    await staff.contact(item, 3, "el_left_message", await staff.token(item))
                else:
                    await staff.contact(item, 1, "el_unknown_code", await staff.token(item))
            except ActionRefused as refused:
                return str(refused.code)
            raise AssertionError("not refused")

    code = asyncio.run(body())
    if case == "resolved_item":
        assert code not in ("", "COMMAND_MALFORMED")  # the existing state refusal, measured
    else:
        assert code == "COMMAND_MALFORMED"
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

def test_el3b_p17_another_tenants_item_gets_the_ordinary_missing_item_refusal(
        install: e4.EL3BInstall, other: e4.EL3BInstall) -> None:
    from haloflow.m05.eligibility_staff import CaseToken

    a, b = _begin(install), _begin(other)
    ref_b, item_b = _el_item(b)
    before_b = e4.side_effects(b, ref_b, str(item_b))

    async def body(target: uuid.UUID) -> str:
        from haloflow.m05.service import ActionRefused

        async with e4.staff_runtime(a) as rt:
            staff = e4.Staff(a, rt)
            token = CaseToken(case_id=uuid.uuid4(), item_id=target, case_version=1)
            codes = []
            for call in (staff.resolve(target, 1, "el_self_pay", token, kind="claim_and_resolve"),
                         staff.contact(target, 1, "el_left_message", token)):
                try:
                    await call
                    codes.append("applied")
                except ActionRefused as refused:
                    codes.append(str(refused.code))
            return ",".join(codes)

    unknown = asyncio.run(body(uuid.uuid4()))  # the oracle: a random unknown id
    assert asyncio.run(body(item_b)) == unknown
    assert "applied" not in unknown
    assert e4.side_effects(b, ref_b, str(item_b)) == before_b
    assert e3.count(a, "m05_contact_entries") == 0
    assert e3.count(a, "patient_events",
                    "event_type = 'm05_followup_resolution_asserted'") == 0


# --- E3b-C04 exact binding ----------------------------------------------------------------

def test_el3b_c04_a_token_for_another_case_is_case_changed_and_touches_nothing(
        install: e4.EL3BInstall) -> None:
    from haloflow.m05.eligibility_staff import CaseToken

    inst = _begin(install)
    ref, first = _el_item(inst)
    ref2, other_item = _el_item(inst)  # a case of ANOTHER appointment

    async def tokens() -> tuple[Any, Any]:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            return await staff.token(first), await staff.token(other_item)

    token_a, token_other = asyncio.run(tokens())

    async def close_a() -> None:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            assert (await staff.claim(first)).outcome == "applied"
            res = await staff.resolve(first, 2, "el_verified_active", await staff.token(first))
            assert res.outcome == "applied"

    asyncio.run(close_a())
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    e3.drain(inst)
    (case_b,) = [c for c in e3.cases(inst, ref) if c["state"] == "open"]
    item_b = uuid.UUID(case_b["item_id"])
    # A's last case_version equals B's current one (v1r1 T1), so only the case/item binding
    # can tell them apart.
    (case_a,) = [c for c in e3.cases(inst, ref) if c["state"] == "closed"]
    e3.q(inst, "UPDATE {s}.m05_el_cases SET case_version = %s WHERE case_id = %s",
         (int(case_b["case_version"]), case_a["case_id"]))
    before = e4.side_effects(inst, ref, str(item_b))
    before_other = e4.side_effects(inst, ref2, str(other_item))

    async def act(token: Any) -> list[str]:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            out = []
            for call in (staff.resolve(item_b, 1, "el_self_pay", token,
                                       kind="claim_and_resolve"),
                         staff.contact(item_b, 1, "el_left_message", token)):
                out.append((await call).outcome)
            return out

    stale_a = CaseToken(case_id=token_a.case_id, item_id=item_b,
                        case_version=int(case_b["case_version"]))
    assert asyncio.run(act(stale_a)) == ["case_changed", "case_changed"]
    foreign = CaseToken(case_id=token_other.case_id, item_id=item_b,
                        case_version=token_other.case_version)
    assert asyncio.run(act(foreign)) == ["case_changed", "case_changed"]
    assert e4.side_effects(inst, ref, str(item_b)) == before
    assert e4.side_effects(inst, ref2, str(other_item)) == before_other
    assert [c["state"] for c in e3.cases(inst, ref) if c["case_id"] == case_b["case_id"]] \
        == ["open"]
