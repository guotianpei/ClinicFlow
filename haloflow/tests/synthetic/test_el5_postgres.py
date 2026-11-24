"""Eligibility E5 (EL-12) PostgreSQL rows (synthetic app, disposable PG17 cluster). RED BY DESIGN.

Governing: E5 test cases v1r1 (Codex ALIGN 9e6974f2, incl. the P03q fresh-store/fresh-permit
repeat), architecture v1r1 (ALIGN 3671b547) with the context-bound port clarification,
requirements v1r1 (ALIGN fa5db003), owner decisions Q1-Q5 and OD-EL13. Baseline: merged E4,
`main` 8ca2217. Classes C / B / A as `el5_support` states; each row names its class.

Every row installs the E4 eligibility profile (present on the baseline) and produces REAL E1/E2
events through the app; it then reaches the first E5 surface (`lc.chart`, `lc.doc_service`,
`m05_el_doc_ops`, `sim_chart`), so on the baseline it fails in its own body (class A), never in
a fixture. Every row asserts the destination entry count and, where an item exists, its state.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import b4_support as b4
import el4_support as e4
import el5_support as e5
import ob5_support as b5
import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _haloflow_loggers_enabled() -> Iterator[None]:
    """The B3b 5a57ced fixture (log rows cannot pass vacuously)."""
    manager = logging.Logger.manager
    names = [n for n, lg in list(manager.loggerDict.items())
             if (n == "haloflow" or n.startswith("haloflow."))
             and isinstance(lg, logging.Logger) and lg.disabled]
    for n in names:
        logging.getLogger(n).disabled = False
    try:
        yield
    finally:
        for n in names:
            logging.getLogger(n).disabled = True


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    with s.cluster(tmp_path) as c:
        yield c
    s.reap()
    shutil.rmtree(tmp_path / "pg", ignore_errors=True)   # CI disk (E4 01efa26)


@pytest.fixture
def test_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e4.mod("scenarios"), "LOAD_ZONES", b5.TEST_ZONES)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def el_install(pg: s.PgCluster, tmp_path: Path) -> b3.Install:
    inst = e4.install(pg, tmp_path, e4.ELIGIBILITY)
    e4.first_start(inst, e4.ELIGIBILITY)
    return inst


async def booked(inst: b3.Install, app: Any, tok: str, name: str) -> str:
    r = await e4.load(app, tok, name)
    assert r.status == 200 and r.json.get("outcome") == "loaded", (r.status, r.raw[:300])
    ref = e4.scenario_ref(inst, name, e4.EL_ROW_KEY)
    e4.make_due(inst, ref, "booking", -1.0)
    return ref


MANUAL_SCENARIO = {"no_coverage": "E2", "incomplete": "E4", "nonpriority": "E5"}


async def make_event(inst: b3.Install, app: Any, tok: str, what: str) -> tuple[str, str]:
    """Produce ONE real case event through E1/E2 and the eligibility tick; (ref, event_id).
    what: active | inactive | inconclusive | manual:<reason> | timer."""
    if what.startswith("manual:"):
        ref = await booked(inst, app, tok, MANUAL_SCENARIO[what.split(":", 1)[1]])
    elif what == "timer":
        r = await e4.load(app, tok, "E3")
        assert r.status == 200, (r.status, r.raw[:300])
        ref = e4.scenario_ref(inst, "E3", e4.EL_ROW_KEY)
        e4.age_coverage_deadline(inst, ref, -1.0)
    else:
        if what == "inconclusive":
            await e4.member(app, tok, "SYN-E1-001", "error", "service_type_unsupported")
        else:
            await e4.member(app, tok, "SYN-E1-001", what)
        ref = await booked(inst, app, tok, "E1")
    e4.tick_counts(await e4.tick(app, tok))
    evs = [ev for ev in e5.events(inst, ref) if ev[2] is not None]
    assert len(evs) == 1, evs
    return ref, str(evs[0][0])


async def new_coverage_version(inst: b3.Install, lc: Any, ref: str, member_id: str) -> None:
    """FIXTURE (real E1 intake): re-send the E1 appointment through the same directory
    `appointment_put` the scenario loader uses, with another synthetic member id, so E1 records
    coverage version + 1 (the E2-P07c `el.put(..., member_id=...)` pattern). Test-only use of
    the loader's private helpers; no E5 surface."""
    sa = e4.mod("sim_actions")
    scen = e4.mod("scenarios")
    patient, _appt = scen.ids(tenant_id(inst), "E1", e4.EL_ROW_KEY)
    ((start, zone, dept_tz),) = e4.q(inst, "SELECT start_at, zone, department_tz FROM"
                                     " {s}.m05_el_appointments WHERE appointment_ref = %s",
                                     (ref,))
    row = scen.fixtures()["E1"].rows[0]
    coverage = dict(row.coverage)
    coverage["member_id"] = member_id
    context = await sa._context(lc)

    async def body(binding: Any) -> None:
        await sa._directory(binding, context, "appointment_put", {
            "appointment_ref": ref, "patient_id": str(patient), "start_at": sa._iso_z(start),
            "status": "booked", "first_name": row.first_name, "confirmation": "not_confirmed",
            "department_tz": dept_tz or zone, "source_status": "booked",
            "provider_ref": row.provider_ref, "department_ref": row.department_ref,
            "coverage": coverage}, frozenset({"applied"}))

    await sa.wiring.load_foundation(lc.services.gateway).run_in_transaction(context, body)


async def doc_pass(app: Any, tok: str) -> dict[str, Any]:
    """One eligibility tick (phase `document` runs after `apply`); its document counts."""
    counts = e4.tick_counts(await e4.tick(app, tok))
    return {k: v for k, v in counts.items() if k.startswith("document.")}


def chart_of(lc: Any) -> Any:
    return lc.chart


def port(lc: Any) -> Any:
    return lc.chart.for_tenant(lc.tenant_context())


def tenant_id(inst: b3.Install) -> str:
    return str(inst.state["tenant_id"])


# =========================================================================== D01 selection (R2)

@pytest.mark.parametrize("cls", list(e5.PAYER_CLASSES))
def test_el5_p01a_payer_event_one_entry(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                        cls: str) -> None:
    """A. A real E2 settlement per class (active with no case, inactive, inconclusive) -> one op
    and one entry keyed by that event."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, cls)
            await doc_pass(app, tok)
            assert [o[0] for o in e5.ops(inst)] == [ev]
            ent = e5.entries(inst)
            assert [(r[0], r[1]) for r in ent] == [(tenant_id(inst), ev)], ent
            assert e5.op(inst, ev)["state"] == "appended"

    run(body())


@pytest.mark.parametrize("reason", list(e5.MANUAL_REASONS))
def test_el5_p01b_manual_routing_one_entry(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                           reason: str) -> None:
    """A. A real E1 routing event per reason -> one manual entry."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, f"manual:{reason}")
            await doc_pass(app, tok)
            (ent,) = e5.entries(inst)
            assert ent[1] == ev
            assert ent[4]["text"] == ("Insurance needs manual check — "
                                      f"{e5.REASON_LABELS[reason]}; no payer check was made.")

    run(body())


def test_el5_p01c_real_timer_event_not_documented(pg: s.PgCluster, tmp_path: Path,
                                                  test_zones: None) -> None:
    """A. The REAL not-received tick (manual_required, coverage_not_received, no verification;
    the same class/link shape as routing) -> after processing: 0 ops, 0 entries."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            ref, _ev = await make_event(inst, app, tok, "timer")
            assert [(ev[2], ev[3], ev[4]) for ev in e5.events(inst, ref) if ev[2]] == [
                ("manual_required", e5.TIMER_REASON, None)]
            await doc_pass(app, tok)
            await doc_pass(app, tok)
            assert e5.ops(inst) == [] and e5.entries(inst) == []

    run(body())


def test_el5_p01d_note_only_and_coalesced(pg: s.PgCluster, tmp_path: Path,
                                          test_zones: None) -> None:
    """A. Note-only events (class NULL) and a trigger that settles nothing new -> no op."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            ref, ev = await make_event(inst, app, tok, "inactive")
            # A second tick with nothing new due: coalesced / no settlement, no new event.
            await doc_pass(app, tok)
            await doc_pass(app, tok)
            note_only = [x for x in e5.events(inst, ref) if x[2] is None]
            assert [o[0] for o in e5.ops(inst)] == [ev]
            assert not any(o[0] in {x[0] for x in note_only} for o in e5.ops(inst))
            assert len(e5.entries(inst)) == 1

    run(body())


def test_el5_p01e_two_versions_two_entries(pg: s.PgCluster, tmp_path: Path,
                                           test_zones: None) -> None:
    """A. v1 settles inactive; coverage changes through real E1 intake (v2); v2 settles active
    -> two entries, each with its own version, class and text."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ref, ev1 = await make_event(inst, app, tok, "inactive")
            await e4.member(app, tok, "SYN-E1-002", "active")
            await new_coverage_version(inst, lc, ref, "SYN-E1-002")
            ((ver,),) = e4.q(inst, "SELECT coverage_version FROM {s}.m05_el_appointments"
                             " WHERE appointment_ref = %s", (ref,))
            assert ver == 2, ver                     # E1 recorded the new version (control)
            e4.make_due(inst, ref, "booking", -1.0)
            e4.tick_counts(await e4.tick(app, tok))
            payer = [x for x in e5.events(inst, ref) if x[4] is not None]
            assert [x[2] for x in payer] == ["inactive", "active"], payer   # E2 control
            await doc_pass(app, tok)
            got = {r[1]: r[4] for r in e5.entries(inst)}
            assert set(got) == {ev1, payer[1][0]}
            assert got[ev1]["class"] == "inactive" and got[payer[1][0]]["class"] == "active"
            assert (got[ev1]["coverage_version"], got[payer[1][0]]["coverage_version"]) == (1, 2)

    run(body())


def test_el5_p01f_unlisted_manual_reason_fixture(pg: s.PgCluster, tmp_path: Path,
                                                 test_zones: None) -> None:
    """A. A manual_required event with a reason outside the routing set (fixture insert) is
    never selected."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            ref, _ev = await make_event(inst, app, tok, "manual:no_coverage")
            e4.q(inst, "INSERT INTO {s}.m05_el_case_events (event_id, appointment_ref, seq,"
                 " coverage_version, kind, class, reason, created_at) SELECT"
                 " gen_random_uuid(), appointment_ref, next_seq, coverage_version,"
                 " 'open_or_note', 'manual_required', 'appointment_cancelled', clock_timestamp()"
                 " FROM {s}.m05_el_appointments WHERE appointment_ref = %s", (ref,))
            await doc_pass(app, tok)
            assert len(e5.ops(inst)) == 1 and len(e5.entries(inst)) == 1

    run(body())


# =========================================================================== D02 content (R3)

def test_el5_p02a_entry_uses_that_events_answer(pg: s.PgCluster, tmp_path: Path,
                                                test_zones: None) -> None:
    """A. Payer entry text per OD-EL13, check time and OD-EL4 fields from THAT event's
    verification answer; a later verification does not change the existing entry."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            (before,) = e5.entry_rows_raw(inst, ev)
            body_ = e5.entries(inst)[0][4]
            assert body_["text"] == ("Eligibility check: Not active — "
                                     "Payer reports coverage not active")
            assert body_["checked_at"] is not None
            await e4.member(app, tok, "SYN-E1-001", "active")
            e4.make_due(inst, ref, "booking", -1.0)
            await doc_pass(app, tok)
            assert e5.entry_rows_raw(inst, ev) == [before]

    run(body())


def test_el5_p02b_manual_wording(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. Manual entry: owner decision 2 wording; never "Eligibility check"; no financial
    fields."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            await make_event(inst, app, tok, "manual:nonpriority")
            await doc_pass(app, tok)
            (ent,) = e5.entries(inst)
            assert not ent[4]["text"].startswith("Eligibility check")
            assert ent[4]["summary"] is None

    run(body())


def test_el5_p02c_inconclusive_no_financial(pg: s.PgCluster, tmp_path: Path,
                                            test_zones: None) -> None:
    """A. Inconclusive entry carries no financial fields."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            await make_event(inst, app, tok, "inconclusive")
            await doc_pass(app, tok)
            (ent,) = e5.entries(inst)
            assert ent[4]["summary"] is None
            assert ent[4]["text"].startswith("Eligibility check: Inconclusive — ")

    run(body())


def test_el5_p02d_after_scrub(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. An op delivered after the EL-14a scrub: class, reason, time only."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).fail_next(3)       # keep the op undelivered across the scrub
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            # Codex ruling (codex_e5-frozen-test-ruling.md): consume all three refusals across
            # eligible passes; the op is escalated before the scrub.
            for _ in range(2):
                e5.age_op(inst, ev)
                await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "needs_check"
            e4.q(inst, "UPDATE {s}.m05_el_attempts SET answer = jsonb_build_object("
                 "'class', answer->'class', 'reason', answer->'reason', 'observed_at',"
                 " answer->'observed_at') WHERE answer IS NOT NULL")
            # FIXTURE: re-open the escalated op so it is delivered after the scrub.
            e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET state = 'ready', attempts = 0,"
                 " next_at = clock_timestamp() WHERE event_id = %s", (ev,))
            await doc_pass(app, tok)
            (ent,) = e5.entries(inst)
            assert ent[4]["summary"] is None and ent[4]["checked_at"] is not None

    run(body())


# =========================================================================== D03 delivery (R5)

def test_el5_p03a_normal(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. 1 entry / appended / attempts 1 / reads 0."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            o = e5.op(inst, ev)
            assert (o["state"], o["attempts"], o["reads"]) == ("appended", 1, 0)
            assert len(e5.entries(inst)) == 1

    run(body())


def test_el5_p03b_duplicate_delivery_fresh_worker(pg: s.PgCluster, tmp_path: Path,
                                                  test_zones: None) -> None:
    """A. The same op run again by a fresh app (fresh worker, fresh chart instance) -> 1 entry."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
        async with e4.running(inst, app_name="el5-fresh") as (_lc2, app2):
            tok2 = await e4.token(app2)
            e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET next_at = clock_timestamp()")
            await doc_pass(app2, tok2)
        assert len(e5.entries(inst)) == 1 and e5.op(inst, ev)["state"] == "appended"

    run(body())


def test_el5_p03c_concurrent_claims(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. Two workers claim the same op concurrently -> one entry; the loser's settle is refused
    by the epoch fence."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            chart_of(lc).hold_commits()
            first = asyncio.create_task(doc_pass(app, tok))
            await asyncio.sleep(0.2)
            async with e4.running(inst, app_name="el5-second") as (_lc2, app2):
                tok2 = await e4.token(app2)
                e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET lease_until = clock_timestamp()")
                second = asyncio.create_task(doc_pass(app2, tok2))
                await asyncio.sleep(0.2)
                chart_of(lc).release_commits()
                await asyncio.gather(first, second, return_exceptions=True)
            assert len(e5.entries(inst)) == 1
            assert e5.op(inst, ev)["state"] in {"appended", "reconciling"}

    run(body())


def test_el5_p03d_one_refusal_then_success(pg: s.PgCluster, tmp_path: Path,
                                           test_zones: None) -> None:
    """A. fail_next(1): attempt 1 refused, attempt 2 at +1 min succeeds -> 1 / appended / 2."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).fail_next(1)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "retry_wait"
            await doc_pass(app, tok)                      # not yet due
            assert e5.op(inst, ev)["attempts"] == 1
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            o = e5.op(inst, ev)
            assert (o["state"], o["attempts"]) == ("appended", 2)
            assert len(e5.entries(inst)) == 1

    run(body())


def test_el5_p03e_three_attempts_then_needs_check(pg: s.PgCluster, tmp_path: Path,
                                                  test_zones: None) -> None:
    """A. fail_next(3): attempts at t0, +1, +6 min all refused -> 0 entries / needs_check /
    attempts 3; aged by another hour: NO fourth attempt (Codex 3671b547)."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).fail_next(5)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            for _ in range(2):
                e5.age_op(inst, ev)
                await doc_pass(app, tok)
            o = e5.op(inst, ev)
            assert (o["state"], o["attempts"]) == ("needs_check", 3)
            appends = e5.calls(chart_of(lc), "append")
            e5.age_op(inst, ev, -3600)
            await doc_pass(app, tok)
            assert e5.calls(chart_of(lc), "append") == appends == 3
            assert e5.entries(inst) == []
            assert [(st) for _i, st, _o, _v in e5.doc_items(inst)] == ["open"]

    run(body())


def test_el5_p03f_attempt_charged_durably(pg: s.PgCluster, tmp_path: Path,
                                          test_zones: None) -> None:
    """A. A crash after claim and before append charges the attempt durably; repeated crashes
    never exceed 3 attempts; the same permit is reused while unsent."""
    inst = el_install(pg, tmp_path)
    crash = e5.m05("eligibility_doc").SimulatedCrash

    def boom() -> None:
        raise crash("after_claim")

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            lc.doc_service.hooks["after_claim"] = boom
            for _ in range(5):
                e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET lease_until = clock_timestamp(),"
                     " next_at = clock_timestamp() WHERE event_id = %s", (ev,))
                await doc_pass(app, tok)
            o = e5.op(inst, ev)
            assert o["attempts"] == 3 and o["state"] == "needs_check"
            assert e5.calls(chart_of(lc), "append") == 0
            assert e5.entries(inst) == []

    run(body())


def test_el5_p03g_crash_before_mark_sent(pg: s.PgCluster, tmp_path: Path,
                                         test_zones: None) -> None:
    """A. Crash before mark-sent: a fresh worker re-appends with the SAME permit (open, unused);
    1 entry; no reconciliation read."""
    inst = el_install(pg, tmp_path)
    crash = e5.m05("eligibility_doc").SimulatedCrash

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, "inactive")

            def boom() -> None:
                lc.doc_service.hooks.pop("after_claim", None)
                raise crash("after_claim")

            lc.doc_service.hooks["after_claim"] = boom
            await doc_pass(app, tok)
            p1 = e5.op(inst, ev)["permit_id"]
            assert e5.op(inst, ev)["permit_sent"] is False
        async with e4.running(inst, app_name="el5-fresh") as (lc2, app2):
            tok2 = await e4.token(app2)
            e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET lease_until = clock_timestamp(),"
                 " next_at = clock_timestamp()")
            await doc_pass(app2, tok2)
            (ent,) = e5.entries(inst)
            assert ent[3] == p1 and e5.op(inst, ev)["reads"] == 0
            assert e5.calls(chart_of(lc2), "read") == 0

    run(body())


def test_el5_p03h_crash_after_mark_sent(pg: s.PgCluster, tmp_path: Path,
                                        test_zones: None) -> None:
    """A. Crash after mark-sent, append not reached -> reconciling; read absent -> close -> read
    absent -> definitive -> the next (charged) attempt with a NEW permit; 1 entry."""
    inst = el_install(pg, tmp_path)
    crash = e5.m05("eligibility_doc").SimulatedCrash

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, "inactive")

            def boom() -> None:
                lc.doc_service.hooks.pop("after_mark_sent", None)
                raise crash("after_mark_sent")

            lc.doc_service.hooks["after_mark_sent"] = boom
            await doc_pass(app, tok)
            p1 = e5.op(inst, ev)["permit_id"]
            e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET lease_until = clock_timestamp()")
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "reconciling"
            for _ in range(3):
                e5.age_op(inst, ev)
                await doc_pass(app, tok)
            (ent,) = e5.entries(inst)
            assert ent[3] != p1
            assert dict(e5.permits(inst, ev))[p1] == "closed"
            assert e5.op(inst, ev)["attempts"] == 2

    run(body())


def test_el5_p03i_committed_ack_lost(pg: s.PgCluster, tmp_path: Path,
                                     test_zones: None) -> None:
    """A. unknown_next(1): the append commits, the ack is dropped -> reconciling; first read
    finds the matching entry -> appended; append calls 1; entries 1."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).unknown_next(1)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "reconciling"
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "appended"
            assert e5.calls(chart_of(lc), "append") == 1 and len(e5.entries(inst)) == 1

    run(body())


def test_el5_p03j_release_between_read_and_close(pg: s.PgCluster, tmp_path: Path,
                                                 test_zones: None) -> None:
    """A. Held append; read 1 empty -> stays reconciling; release -> commit lands (permit used);
    the reconciliation's close returns already_used -> read-back -> appended; 1 entry."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ch = chart_of(lc)
            ch.hold_commits()
            ch.port_timeout_override = 0.2          # the held append times out -> unknown
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "reconciling"
            assert e5.entries(inst) == []

            # Codex ruling (codex_e5-frozen-test-ruling.md): the one-shot seam on the empty
            # read releases the held append and waits (bounded, failing on timeout) until its
            # commit is observable, so the release lands between the empty read and the close.
            async def release() -> None:
                lc.doc_service.hooks.pop("after_reconcile_read", None)
                assert e5.entries(inst) == []            # the read that just ran was empty
                ch.release_commits()
                for _ in range(100):
                    if e5.entry_rows_raw(inst, ev):
                        return
                    await asyncio.sleep(0.05)
                raise AssertionError("held append did not commit within 5 s")

            lc.doc_service.hooks["after_reconcile_read"] = release
            e5.age_op(inst, ev)
            await doc_pass(app, tok)       # read empty -> commit lands -> close already_used
            assert "after_reconcile_read" not in lc.doc_service.hooks
            o = e5.op(inst, ev)
            assert o["state"] == "appended" and len(e5.entries(inst)) == 1
            assert e5.calls(ch, "issue_permit") == 1 and e5.calls(ch, "append") == 1
            assert len(e5.permits(inst, ev)) == 1

    run(body())


def test_el5_p03k_held_uncommitted_reads_unavailable(pg: s.PgCluster, tmp_path: Path,
                                                     test_zones: None) -> None:
    """A. Held (never released) append + unavailable reads: exactly 3 attempted reads incl.
    timeouts, the count durable across a restart between reads 1 and 2; then needs_check, ONE
    item, no issue_permit after the unknown, append calls 1."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ch = chart_of(lc)
            ch.hold_commits()
            ch.port_timeout_override = 0.2
            ch.reads_unavailable(10)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["reads"] == 1
            # Codex 75eab821: the initial worker made exactly one append and one issuance.
            assert e5.calls(ch, "append") == 1 and e5.calls(ch, "issue_permit") == 1
            assert len(e5.permits(inst, ev)) == 1
        async with e4.running(inst, app_name="el5-fresh") as (lc2, app2):
            tok2 = await e4.token(app2)
            ch2 = chart_of(lc2)
            ch2.reads_unavailable(10)
            for n in (2, 3):
                e5.age_op(inst, ev)
                await doc_pass(app2, tok2)
                assert e5.op(inst, ev)["reads"] == n
            assert e5.op(inst, ev)["state"] == "needs_check"
            assert e5.calls(ch2, "issue_permit") == 0 and e5.calls(ch2, "append") == 0
            assert len(e5.permits(inst, ev)) == 1
            assert len(e5.doc_items(inst)) == 1 and e5.entries(inst) == []

    run(body())


def test_el5_p03l_stale_epoch_cannot_mark_sent(pg: s.PgCluster, tmp_path: Path,
                                               test_zones: None) -> None:
    """A. A worker whose epoch is no longer current cannot mark sent and makes NO append call."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await make_event(inst, app, tok, "inactive")

            def bump() -> None:
                e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET epoch = epoch + 1 WHERE event_id = %s",
                     (ev,))

            lc.doc_service.hooks["after_claim"] = bump
            await doc_pass(app, tok)
            assert e5.calls(chart_of(lc), "append") == 0
            assert e5.op(inst, ev)["permit_sent"] is False and e5.entries(inst) == []

    run(body())


def test_el5_p03m_exhaustion_before_new_permit(pg: s.PgCluster, tmp_path: Path,
                                               test_zones: None) -> None:
    """A. After the 3rd definitive non-write no issue_permit is made; at most 3 permits, none
    open."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).fail_next(5)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            for _ in range(3):
                e5.age_op(inst, ev)
                await doc_pass(app, tok)
            ps = e5.permits(inst, ev)
            assert len(ps) <= 3 and "open" not in {st for _p, st in ps}, ps
            assert e5.calls(chart_of(lc), "issue_permit") == len(ps)

    run(body())


def test_el5_p03n_close_before_open_tombstone(pg: s.PgCluster, tmp_path: Path,
                                              test_zones: None) -> None:
    """A. close_permit on an unissued id writes a closed tombstone; a delayed issue_permit for
    that id is refused PERMIT_CLOSED; no entry."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ref, ev = await make_event(inst, app, tok, "inactive")
            chart = e5.m05("simulated_chart")
            p = "00000000-0000-4000-8000-00000000c105"
            await port(lc).close_permit(p)
            got = await port(lc).issue_permit(p, ev, ref)
            assert got == chart.PERMIT_CLOSED
            assert e5.entry_rows_raw(inst, ev) == []

    run(body())


def test_el5_p03o_already_used_is_not_a_nonwrite(pg: s.PgCluster, tmp_path: Path,
                                                 test_zones: None) -> None:
    """A. Append with a used permit returns ALREADY_USED, treated as unknown -> read ->
    appended; never counted as a non-write."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart = e5.m05("simulated_chart")
            ref, ev = await make_event(inst, app, tok, "inactive")
            p = "00000000-0000-4000-8000-00000000a1e0"
            assert await port(lc).issue_permit(p, ev, ref) == chart.ISSUED
            assert await port(lc).append(p, {"text": "x"}) == chart.APPENDED
            assert await port(lc).append(p, {"text": "y"}) == chart.ALREADY_USED
            assert len(e5.entry_rows_raw(inst, ev)) == 1

    run(body())


@pytest.mark.parametrize("fresh_store", [False, True])
def test_el5_p03q_destination_duplicate(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                        fresh_store: bool) -> None:
    """A (Codex 9e6974f2). Entry E exists via p1; a second VALID permit p2 for the same binding
    (issued after p1 is used) -> append(p2, other body) returns EXISTS; E byte-identical; count 1.
    Repeated on a fresh store instance with a fresh valid permit."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart = e5.m05("simulated_chart")
            ref, ev = await make_event(inst, app, tok, "inactive")
            p1 = "00000000-0000-4000-8000-0000000000b1"
            assert await port(lc).issue_permit(p1, ev, ref) == chart.ISSUED
            assert await port(lc).append(p1, {"text": "E"}) == chart.APPENDED
            before = e5.entry_rows_raw(inst, ev)
            target = port(lc)
            if fresh_store:
                target = chart.SimulatedChart(lc.chart.conninfo).for_tenant(lc.tenant_context())
            p2 = "00000000-0000-4000-8000-0000000000b2"
            assert await target.issue_permit(p2, ev, ref) == chart.ISSUED
            assert await target.append(p2, {"text": "OTHER"}) == chart.EXISTS
            assert e5.entry_rows_raw(inst, ev) == before and len(before) == 1

    run(body())


def test_el5_p03r_close_first_ordering(pg: s.PgCluster, tmp_path: Path,
                                       test_zones: None) -> None:
    """A. Held append; reconciliation read empty -> close lands first -> release -> the held
    append returns PERMIT_CLOSED, nothing written -> read absent -> next charged attempt with a
    NEW permit -> 1 entry carrying the new permit."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ch = chart_of(lc)
            ch.hold_commits()
            ch.port_timeout_override = 0.2
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            p1 = e5.op(inst, ev)["permit_id"]
            e5.age_op(inst, ev)
            await doc_pass(app, tok)                  # read empty -> close p1 -> read absent
            assert dict(e5.permits(inst, ev))[p1] == "closed"
            ch.release_commits()
            await asyncio.sleep(0.2)
            assert e5.entries(inst) == []
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            (ent,) = e5.entries(inst)
            assert ent[3] != p1 and e5.op(inst, ev)["state"] == "appended"

    run(body())


# =========================================================================== D04 item (R6)

async def _escalate(inst: b3.Install, lc: Any, app: Any, tok: str) -> tuple[str, str]:
    """An inactive event whose op is driven to needs_check by three refusals; (ref, event)."""
    chart_of(lc).fail_next(3)
    ref, ev = await make_event(inst, app, tok, "inactive")
    await doc_pass(app, tok)
    for _ in range(2):
        e5.age_op(inst, ev)
        await doc_pass(app, tok)
    assert e5.op(inst, ev)["state"] == "needs_check"
    return ref, ev


def test_el5_p04a_one_item(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. Budget exhausted -> exactly one open doc item (title per owner decision 4); a further
    pass creates none."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            await _escalate(inst, lc, app, tok)
            await doc_pass(app, tok)
            assert [st for _i, st, _o, _v in e5.doc_items(inst)] == ["open"]
            assert e5.entries(inst) == []

    run(body())


def test_el5_p04b_item_survives_case_closure(pg: s.PgCluster, tmp_path: Path,
                                             test_zones: None) -> None:
    """A. Staff close the eligibility case -> the doc item stays open."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            await _escalate(inst, lc, app, tok)
            (el_item,) = [i for i, k, st in e4.el_items(inst) if k == "eligibility_inactive"]
            # FIXTURE: the case closed (E3b closure is covered by E3b rows; only its effect here).
            # Codex ruling: the merged resolved-outcome constraint needs an OD-EL6 code.
            e4.q(inst, "UPDATE {s}.m05_followup_items SET state = 'resolved',"
                 " outcome_code = 'el_confirmed_inactive' WHERE item_id = %s", (el_item,))
            e4.q(inst, "UPDATE {s}.m05_el_cases SET state = 'closed',"
                 " closed_at = clock_timestamp(), closure_outcome = 'el_confirmed_inactive'"
                 " WHERE item_id = %s", (el_item,))
            await doc_pass(app, tok)
            assert [st for _i, st, _o, _v in e5.doc_items(inst)] == ["open"]

    run(body())


def test_el5_p04c_late_commit_confirmed_once(pg: s.PgCluster, tmp_path: Path,
                                             test_zones: None) -> None:
    """A. Late commit during needs_check: the hourly confirmation read finds it -> op appended,
    item entry_confirmed exactly once; restart and further passes change nothing; 0 appends in
    needs_check."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ref, ev = await _escalate(inst, lc, app, tok)
            appends = e5.calls(chart_of(lc), "append")
            # The late commit: an entry under this op's last permit (its append landed late).
            p = e5.op(inst, ev)["permit_id"]
            inst.admin("UPDATE sim_chart.permits SET state = 'used' WHERE permit_id = %s", (p,))
            inst.admin("INSERT INTO sim_chart.entries (tenant, event_id, appointment_ref,"
                       " permit_id, body, committed_at) VALUES (%s, %s, %s, %s, '{}'::jsonb,"
                       " clock_timestamp())", (tenant_id(inst), ev, ref, p))
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "appended"
            ((_i, st, outcome, ver),) = e5.doc_items(inst)
            assert (st, outcome) == ("resolved", "entry_confirmed")
            assert e5.calls(chart_of(lc), "append") == appends
        async with e4.running(inst, app_name="el5-fresh") as (_lc2, app2):
            tok2 = await e4.token(app2)
            e5.age_op(inst, ev)
            await doc_pass(app2, tok2)
            assert e5.doc_items(inst)[0][3] == ver and len(e5.entries(inst)) == 1

    run(body())


def test_el5_p04d_confirmation_reads_capped(pg: s.PgCluster, tmp_path: Path,
                                            test_zones: None) -> None:
    """A. 24 hourly confirmation reads absent -> no 25th read; item stays open; no append."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await _escalate(inst, lc, app, tok)
            appends = e5.calls(chart_of(lc), "append")
            for _ in range(e5.CONFIRM_READS_MAX + 2):
                e5.age_op(inst, ev, -e5.CONFIRM_READ_INTERVAL_S)
                await doc_pass(app, tok)
            assert e5.op(inst, ev)["confirm_reads"] == e5.CONFIRM_READS_MAX
            assert e5.calls(chart_of(lc), "append") == appends
            assert [st for _i, st, _o, _v in e5.doc_items(inst)] == ["open"]

    run(body())


def test_el5_p04e_manual_append_never_committed(pg: s.PgCluster, tmp_path: Path,
                                                test_zones: None) -> None:
    """A. Manual: close -> read absent -> op manual_done, item manual_documentation; the held
    append, released, returns PERMIT_CLOSED; entries 0; later passes and a fresh worker: no
    append."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await _escalate(inst, lc, app, tok)
            ((item, _st, _o, ver),) = e5.doc_items(inst)
            out = await lc.doc_service.manual(lc.staff_context("staff-synthetic-1"), item, ver)
            assert out == "manual_documentation"
            assert e5.op(inst, ev)["state"] == "manual_done"
            await doc_pass(app, tok)
        async with e4.running(inst, app_name="el5-fresh") as (lc2, app2):
            tok2 = await e4.token(app2)
            await doc_pass(app2, tok2)
            assert e5.calls(chart_of(lc2), "append") == 0 and e5.entries(inst) == []

    run(body())


@pytest.mark.parametrize("winner", ["append", "manual"])
def test_el5_p04fg_manual_vs_delayed_append(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                            winner: str) -> None:
    """A. P04f append wins: the held commit is released before the close -> read finds it ->
    entry_confirmed, op appended, entries 1. P04g manual wins: the close lands before release ->
    release returns PERMIT_CLOSED, entries 0, item manual_documentation."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ch = chart_of(lc)
            ch.hold_commits()
            ch.port_timeout_override = 0.2
            ch.reads_unavailable(10)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            for _ in range(3):
                e5.age_op(inst, ev)
                await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "needs_check"
            ch.reads_unavailable(0)
            ((item, _st, _o, ver),) = e5.doc_items(inst)
            if winner == "append":
                ch.release_commits()
                await asyncio.sleep(0.2)
                out = await lc.doc_service.manual(lc.staff_context("staff-1"), item, ver)
                assert out == "entry_confirmed"
                assert e5.op(inst, ev)["state"] == "appended" and len(e5.entries(inst)) == 1
            else:
                out = await lc.doc_service.manual(lc.staff_context("staff-1"), item, ver)
                assert out == "manual_documentation"
                ch.release_commits()
                await asyncio.sleep(0.2)
                assert e5.entries(inst) == []
            assert e5.doc_items(inst)[0][1] == "resolved"

    run(body())


def test_el5_p04h_manual_pending_resumed(pg: s.PgCluster, tmp_path: Path,
                                         test_zones: None) -> None:
    """A. Crash after t134 phase 1 (manual_pending) -> a fresh scheduler resumes close -> read ->
    phase 2 with the recorded attribution; the item closes once."""
    inst = el_install(pg, tmp_path)
    crash = e5.m05("eligibility_doc").SimulatedCrash

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await _escalate(inst, lc, app, tok)
            ((item, _st, _o, ver),) = e5.doc_items(inst)

            def boom() -> None:
                raise crash("after_manual_phase1")

            lc.doc_service.hooks["after_manual_phase1"] = boom
            with pytest.raises(crash):
                await lc.doc_service.manual(lc.staff_context("staff-attrib-7"), item, ver)
            assert e5.op(inst, ev)["state"] == "manual_pending"
        async with e4.running(inst, app_name="el5-fresh") as (_lc2, app2):
            tok2 = await e4.token(app2)
            await doc_pass(app2, tok2)
            assert e5.op(inst, ev)["state"] == "manual_done"
            ((_i, st, outcome, _v),) = e5.doc_items(inst)
            assert (st, outcome) == ("resolved", "manual_documentation")
            (by,) = e4.q(inst, "SELECT manual_by FROM {s}.m05_el_doc_ops WHERE event_id = %s",
                         (ev,))
            assert by[0] == "staff-attrib-7"

    run(body())


@pytest.mark.parametrize("case", ["closed_item", "stale_version", "op_not_needs_check",
                                  "other_tenant_staff"])
def test_el5_p04i_manual_refusals(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                  case: str) -> None:
    """A. Manual action refused with no state change (op, item, permits, entries): a closed item,
    a stale expected version, an op not in needs_check, and (Codex 75eab821) tenant B's staff
    against tenant A's open item through the authorized manual entry point, followed by the
    tenant-A positive control on the same item."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev = await _escalate(inst, lc, app, tok)
            ((item, _st, _o, ver),) = e5.doc_items(inst)
            if case == "closed_item":
                # Codex ruling: the merged resolved-outcome constraint needs a code.
                e4.q(inst, "UPDATE {s}.m05_followup_items SET state = 'resolved',"
                     " outcome_code = 'manual_documentation' WHERE item_id = %s", (item,))
            elif case == "stale_version":
                ver = ver - 1
            elif case == "op_not_needs_check":
                e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET state = 'reconciling'"
                     " WHERE event_id = %s", (ev,))
            doc = e5.m05("eligibility_doc")
            staff = (doc.staff_context_for("synthetic-tenant-b", "staff-b-1")
                     if case == "other_tenant_staff" else lc.staff_context("staff-1"))
            before = (e5.op(inst, ev), e5.doc_items(inst), e5.permits(inst, ev),
                      e5.entry_rows_raw(inst, ev))
            with pytest.raises(doc.DocRefused):
                await lc.doc_service.manual(staff, item, ver)
            assert (e5.op(inst, ev), e5.doc_items(inst), e5.permits(inst, ev),
                    e5.entry_rows_raw(inst, ev)) == before
            if case == "other_tenant_staff":            # tenant-A positive control
                out = await lc.doc_service.manual(lc.staff_context("staff-a-1"), item, ver)
                assert out == "manual_documentation"
                assert e5.doc_items(inst)[0][1] == "resolved"

    run(body())


# =========================================================================== D05 identity (R4)

def test_el5_p05a_binding_from_event_row(pg: s.PgCluster, tmp_path: Path,
                                         test_zones: None) -> None:
    """A. The op carries a different appointment_ref (fixture) -> the permit and the entry still
    carry the event row's appointment."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).fail_next(1)
            ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET appointment_ref = gen_random_uuid()"
                 " WHERE event_id = %s", (ev,))
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            (ent,) = e5.entries(inst)
            assert ent[2] == ref

    run(body())


def test_el5_p05b_mismatched_first_write(pg: s.PgCluster, tmp_path: Path,
                                         test_zones: None) -> None:
    """A. An entry for (tenant, event) exists with ANOTHER appointment -> issue_permit refused
    REFUSED_MISMATCH, counted; op needs_check; entries unchanged."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).fail_next(1)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            inst.admin("INSERT INTO sim_chart.permits (permit_id, tenant, event_id,"
                       " appointment_ref, state, issued_at) VALUES (gen_random_uuid(), %s, %s,"
                       " gen_random_uuid(), 'used', clock_timestamp())", (tenant_id(inst), ev))
            inst.admin("INSERT INTO sim_chart.entries SELECT tenant, event_id, appointment_ref,"
                       " permit_id, '{}'::jsonb, clock_timestamp() FROM sim_chart.permits"
                       " WHERE event_id = %s AND state = 'used'", (ev,))
            before = e5.entry_rows_raw(inst, ev)
            e5.age_op(inst, ev)
            counts = await doc_pass(app, tok)
            assert counts.get("document.refused_mismatch", 0) == 1, counts
            assert e5.op(inst, ev)["state"] == "needs_check"
            assert e5.entry_rows_raw(inst, ev) == before

    run(body())


def test_el5_p05c_same_event_two_tenants(pg: s.PgCluster, tmp_path: Path,
                                         test_zones: None) -> None:
    """A. The same event_id in two tenants -> two independent entries; A's read never returns
    B's."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart = e5.m05("simulated_chart")
            ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            b = lc.chart.for_tenant(chart.context_for("synthetic-tenant-b"))
            pb = "00000000-0000-4000-8000-0000000000bb"
            assert await b.issue_permit(pb, ev, ref) == chart.ISSUED
            assert await b.append(pb, {"text": "B"}) == chart.APPENDED
            assert len(e5.entry_rows_raw(inst, ev)) == 2
            got_a = await port(lc).read(ev)
            assert got_a != chart.ABSENT and got_a["permit_id"] != pb

    run(body())


def test_el5_p05d_wrong_appointment_readback(pg: s.PgCluster, tmp_path: Path,
                                             test_zones: None) -> None:
    """A. Read-back returns an entry with the wrong appointment (fixture) -> never settles;
    counted; needs_check."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).unknown_next(1)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            inst.admin("UPDATE sim_chart.entries SET appointment_ref = gen_random_uuid()"
                       " WHERE event_id = %s", (ev,))
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "needs_check"

    run(body())


def test_el5_p05e_permit_through_other_tenant_port(pg: s.PgCluster, tmp_path: Path,
                                                   test_zones: None) -> None:
    """A (Codex 9e6974f2). A valid permit issued through tenant A's port, presented to tenant
    B's TenantChart (append and close) -> TENANT_MISMATCH; entries unchanged; permit still
    open."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart = e5.m05("simulated_chart")
            ref, ev = await make_event(inst, app, tok, "inactive")
            pa = "00000000-0000-4000-8000-0000000000aa"
            await port(lc).issue_permit(pa, ev, ref)
            b = lc.chart.for_tenant(chart.context_for("synthetic-tenant-b"))
            assert await b.append(pa, {"text": "x"}) == chart.TENANT_MISMATCH
            assert await b.close_permit(pa) == chart.TENANT_MISMATCH
            assert e5.entry_rows_raw(inst, ev) == []
            assert dict(e5.permits(inst, ev))[pa] == "open"

    run(body())


def test_el5_p05f_foreign_permit_never_settles(pg: s.PgCluster, tmp_path: Path,
                                               test_zones: None) -> None:
    """A (Codex 9e6974f2). Same tenant/event/appointment entry under a permit NOT issued by this
    op -> no settle, no entry_confirmed; counted foreign_permit; needs_check."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).unknown_next(1)
            ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            # Codex ruling: a VALID used permit (FK kept) that this op never issued, same tenant,
            # event and appointment as the entry.
            foreign = "00000000-0000-4000-8000-0000000000f5"
            inst.admin("INSERT INTO sim_chart.permits (permit_id, tenant, event_id,"
                       " appointment_ref, state, issued_at) VALUES (%s, %s, %s, %s, 'used',"
                       " clock_timestamp())", (foreign, tenant_id(inst), ev, ref))
            inst.admin("UPDATE sim_chart.entries SET permit_id = %s WHERE event_id = %s",
                       (foreign, ev))
            (premise,) = e4.q(inst, "SELECT %s::uuid = ANY (issued_permits) FROM"
                              " {s}.m05_el_doc_ops WHERE event_id = %s", (foreign, ev))
            assert premise[0] is False                   # never issued by this op
            assert [(r[0], r[1], r[2], r[3]) for r in e5.entries(inst)] == [
                (tenant_id(inst), ev, ref, foreign)]     # same tenant / event / appointment
            e5.age_op(inst, ev)
            counts = await doc_pass(app, tok)
            assert counts.get("document.foreign_permit", 0) == 1, counts
            assert e5.op(inst, ev)["state"] == "needs_check"
            assert all(o != "entry_confirmed" for _i, _s, o, _v in e5.doc_items(inst))
            assert ref

    run(body())


# =========================================================================== D06 activation (R7)

def test_el5_p06a_documented_after_switch_off(pg: s.PgCluster, tmp_path: Path,
                                              test_zones: None) -> None:
    """A. An event settled before enabled := false is documented after the switch-off."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            chart_of(lc).fail_next(1)
            _ref, ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            e4.q(inst, "UPDATE {s}.m05_el_settings SET enabled = false")
            e5.age_op(inst, ev)
            await doc_pass(app, tok)
            assert e5.op(inst, ev)["state"] == "appended" and len(e5.entries(inst)) == 1

    run(body())


def test_el5_p06b_disabled_no_new_payer_entries(pg: s.PgCluster, tmp_path: Path,
                                                test_zones: None) -> None:
    """A. With enabled = false no new payer-check events -> no new entries; entries follow the
    events that exist (E1 routing while disabled is measured, not assumed)."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            e4.q(inst, "UPDATE {s}.m05_el_settings SET enabled = false")
            await e4.member(app, tok, "SYN-E1-001", "inactive")
            ref = await booked(inst, app, tok, "E1")
            await doc_pass(app, tok)
            documentable = [x for x in e5.events(inst, ref) if x[2] is not None]
            assert sorted(r[1] for r in e5.entries(inst)) == sorted(x[0] for x in documentable)
            assert not [x for x in documentable if x[4] is not None]

    run(body())


# =========================================================================== D07-D09, profile

def test_el5_p07a_logs_codes_and_counts_only(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                             caplog: pytest.LogCaptureFixture) -> None:
    """A. After a full run with escalation, no log record carries an entry body, member id or
    reason label (codes and counts only)."""
    inst = el_install(pg, tmp_path)
    caplog.set_level(logging.DEBUG, logger="haloflow")

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            await _escalate(inst, lc, app, tok)
            assert e5.doc_items(inst)

    run(body())
    text = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in ("SYN-E1-001", "Payer reports coverage not active", "Eligibility check"):
        assert forbidden not in text, forbidden


def test_el5_p08a_app_shows_entries_read_only(pg: s.PgCluster, tmp_path: Path,
                                              test_zones: None) -> None:
    """A. The eligibility app shows the simulated chart entries read-only (no write control);
    the E4-P07 outstanding row text is no longer shown."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            ref, _ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            r = await bb.asgi(app, "GET", f"/synthetic/ui/chart/{ref}", headers=bb.get_headers())
            assert r.status == 200, (r.status, r.raw[:200])
            html = r.raw.decode("utf-8") if isinstance(r.raw, bytes) else str(r.raw)
            assert "Eligibility check: Not active" in html
            assert "<form" not in html
            assert e4.OUTSTANDING_EHR_ROW not in html

    run(body())


def test_el5_p09a_entries_persist(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. Entries persist across app restart; no expiry removes them."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
        before = e5.entries(inst)
        async with e4.running(inst, app_name="el5-fresh") as (_lc2, app2):
            tok2 = await e4.token(app2)
            await doc_pass(app2, tok2)
        assert e5.entries(inst) == before and len(before) == 1

    run(body())


def test_el5_p10a_eligibility_target(pg: s.PgCluster, tmp_path: Path) -> None:
    """B. The eligibility install's target is 135 (t128..t135). Baseline: 127."""
    inst = e4.install(pg, tmp_path, e4.ELIGIBILITY)
    assert e4.installed_target(inst) == e5.ELIGIBILITY_TARGET


def test_el5_p10b_default_target_unchanged(pg: s.PgCluster, tmp_path: Path) -> None:
    """C. The default install's target stays 97; no sim_chart schema is created."""
    inst = e4.install(pg, tmp_path, e4.DEFAULT)
    assert e4.installed_target(inst) == e5.DEFAULT_TARGET
    rows = inst.admin("SELECT count(*) FROM pg_namespace WHERE nspname = 'sim_chart'")
    assert rows[0][0] == 0


def test_el5_p10c_production_set_unchanged() -> None:
    """C. Production supported schema set unchanged: frozenset({36})."""
    comp = importlib.import_module("haloflow.composition")
    assert frozenset({36}) == comp.APPROVED_SUPPORTED_SCHEMA_VERSIONS




# ===================================================================== D11 staff route (R6.3b)
# REVISE of 2026-11-24 (codex_e5-green-review.md): the staff action "Recorded in chart
# manually" (OD-EL14, Rachel 2026-11-24) on the open "Chart entry needs a check" item (owner
# decision 4), exercised ONLY through HTTP. Design:
# claude_e5-manual-route-proposal-v1-2026-11-24.md + its D2 correction (Codex ALIGN
# codex_e5-manual-route-ruling.md, riders adopted). Class A unless stated.

CHART_RECORDED: Final = "Recorded in chart manually"          # OD-EL14, exact
FALLBACK_STAFF: Final = "staff-1"     # the fixed fallback identity M-E5-18 substitutes


def _recorded_action(item: str) -> str:
    return f"{e4.UI_PREFIX}/{item}/chart-recorded"


def _attribution(inst: b3.Install, ev: str) -> tuple[Any, Any]:
    ((by, at),) = e4.q(inst, "SELECT manual_by, manual_at FROM {s}.m05_el_doc_ops"
                       " WHERE event_id = %s", (ev,))
    return by, at


def _snapshot(inst: b3.Install, ev: str) -> tuple[Any, ...]:
    return (e5.op(inst, ev), e5.doc_items(inst), e5.permits(inst, ev),
            e5.entry_rows_raw(inst, ev), _attribution(inst, ev))


def _spy_manual(lc: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Counts DocService.manual calls (the route must refuse BEFORE any doc call)."""
    seen: list[tuple[Any, ...]] = []
    cls = type(lc.doc_service)
    original = cls.manual

    async def spy(self: Any, staff_ctx: Any, item_id: str, expected_version: int) -> str:
        seen.append((getattr(staff_ctx, "tenant_id", None),
                     str(getattr(getattr(staff_ctx, "principal", None), "id", None)),
                     item_id, expected_version))
        return await original(self, staff_ctx, item_id, expected_version)

    monkeypatch.setattr(cls, "manual", spy)
    return seen


async def _open_doc_item(inst: b3.Install, lc: Any, app: Any,
                         tok: str) -> tuple[str, str, str, int]:
    ref, ev = await _escalate(inst, lc, app, tok)
    ((item, st, _o, ver),) = e5.doc_items(inst)
    assert st == "open"
    return ref, ev, item, ver


def _the_form(pg_: Any, item: str) -> Any:
    f = b4.form_of(pg_, _recorded_action(item))
    assert f.method.lower() == "post"
    return f


def test_el5_p11a_view_shows_the_action(pg: s.PgCluster, tmp_path: Path,
                                        test_zones: None) -> None:
    """A. The open doc item's view: title "Chart entry needs a check" (owner decision 4) and
    exactly one form posting to .../chart-recorded, hidden fields exactly csrf and
    expected_version (the item version), button text exactly "Recorded in chart manually"."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, _ev, item, ver = await _open_doc_item(inst, lc, app, tok)
            r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert r.status == 200, (r.status, r.raw[:300])
            assert f"<h1>{e5.DOC_TITLE}</h1>" in pg_.raw
            f = _the_form(pg_, item)
            assert set(f.fields) == {"csrf", "expected_version"}, f.fields
            assert f.hidden("expected_version") == str(ver)
            assert f.radios == {}
            assert pg_.raw.count(CHART_RECORDED) == 1
            assert f"<button type=\"submit\">{CHART_RECORDED}</button>" in pg_.raw

    run(body())


def test_el5_p11b_manual_closure_through_the_route(pg: s.PgCluster, tmp_path: Path,
                                                   test_zones: None) -> None:
    """A. POST the rendered form (entry absent) -> 303 to the item; item resolved once with
    manual_documentation; the persisted manual_by is the RESOLVED principal (the install's
    staff id, asserted distinct from the fixed fallback identity), manual_at set; the permit is
    closed; later passes and a fresh worker append nothing (R6.4); the view then shows no
    action."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, _ver = await _open_doc_item(inst, lc, app, tok)
            principal = str(lc.services.staff_id)
            assert principal != FALLBACK_STAFF                  # premise (Codex rider)
            assert _attribution(inst, ev) == (None, None)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            r = await e4.post_form(app, f.action, b4.submit_fields(f))
            assert r.status == 303, (r.status, r.raw[:300])
            ((_i, st, outcome, _v),) = e5.doc_items(inst)
            assert (st, outcome) == ("resolved", "manual_documentation")
            by, at = _attribution(inst, ev)
            assert str(by) == principal and at is not None
            assert e5.op(inst, ev)["state"] == "manual_done"
            assert all(state != "open" for _p, state in e5.permits(inst, ev))
            appends = e5.calls(chart_of(lc), "append")
            for _ in range(2):
                e5.age_op(inst, ev)
                await doc_pass(app, tok)
            assert e5.calls(chart_of(lc), "append") == appends
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert _recorded_action(item) not in [x.action for x in pg_.forms]
            assert CHART_RECORDED not in pg_.raw
        async with e4.running(inst, app_name="el5-fresh") as (lc2, app2):
            tok2 = await e4.token(app2)
            await doc_pass(app2, tok2)
            assert e5.calls(chart_of(lc2), "append") == 0 and e5.entries(inst) == []

    run(body())


def test_el5_p11c_entry_present_confirmed(pg: s.PgCluster, tmp_path: Path,
                                          test_zones: None) -> None:
    """A. The entry landed late (P04c fixture) and staff record before the confirmation read:
    the route's manual action reads it -> outcome entry_confirmed, one entry, item resolved
    once, attribution recorded."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ref, ev, item, _ver = await _open_doc_item(inst, lc, app, tok)
            p = e5.op(inst, ev)["permit_id"]
            inst.admin("UPDATE sim_chart.permits SET state = 'used' WHERE permit_id = %s", (p,))
            inst.admin("INSERT INTO sim_chart.entries (tenant, event_id, appointment_ref,"
                       " permit_id, body, committed_at) VALUES (%s, %s, %s, %s, '{}'::jsonb,"
                       " clock_timestamp())", (tenant_id(inst), ev, ref, p))
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            r = await e4.post_form(app, f.action, b4.submit_fields(f))
            assert r.status == 303, (r.status, r.raw[:300])
            ((_i, st, outcome, _v),) = e5.doc_items(inst)
            assert (st, outcome) == ("resolved", "entry_confirmed")
            assert len(e5.entries(inst)) == 1
            assert str(_attribution(inst, ev)[0]) == str(lc.services.staff_id)

    run(body())


def test_el5_p11d_stale_version(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A. The form is rendered at version v; the item then changes underneath (FIXTURE: its
    version -> v + 1, a concurrent change), and the rendered form is posted unchanged: v is a
    syntactically valid PRIOR version, so the post reaches concurrency validation -> 409 with
    the OD-B4-2 reload message; op, item (beyond the fixture), permits, entries and attribution
    unchanged."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, ver = await _open_doc_item(inst, lc, app, tok)
            seen = _spy_manual(lc, monkeypatch)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            assert f.hidden("expected_version") == str(ver)
            e4.q(inst, "UPDATE {s}.m05_followup_items SET version = version + 1"
                 " WHERE item_id = %s", (item,))
            prior = ver
            before = _snapshot(inst, ev)
            r = await e4.post_form(app, f.action, b4.submit_fields(f))
            assert r.status == 409, (r.status, r.raw[:300])
            assert e4.mod("ui").MESSAGES["conflict"] in r.raw.decode()
            assert len(seen) == 1 and seen[0][3] == prior   # it reached the doc service
            assert _snapshot(inst, ev) == before

    run(body())


@pytest.mark.parametrize("csrf", ["missing", "wrong"])
def test_el5_p11e_csrf(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                       monkeypatch: pytest.MonkeyPatch, csrf: str) -> None:
    """A. A missing or wrong CSRF token -> 403 before any doc call; nothing changes."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, _ver = await _open_doc_item(inst, lc, app, tok)
            seen = _spy_manual(lc, monkeypatch)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            before = _snapshot(inst, ev)
            value = None if csrf == "missing" else "x" * len(f.hidden("csrf"))
            r = await e4.post_form(app, f.action, b4.submit_fields(f, csrf=value))
            assert r.status == 403, (r.status, r.raw[:300])
            assert seen == []
            assert _snapshot(inst, ev) == before

    run(body())


@pytest.mark.parametrize("standing", ["read_only", "inactive"])
def test_el5_p11f_unauthorized_staff(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                     monkeypatch: pytest.MonkeyPatch, standing: str) -> None:
    """A. The staff member may not resolve items (read-only access, or inactive) -> refused by
    the existing two-step resolution; DocService.manual is never called; nothing changes,
    manual_by stays null."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, _ver = await _open_doc_item(inst, lc, app, tok)
            seen = _spy_manual(lc, monkeypatch)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            before = _snapshot(inst, ev)
            if standing == "read_only":
                bb.standing_override(inst, access=("read",))
            else:
                bb.standing_override(inst, status="inactive")
            r = await e4.post_form(app, f.action, b4.submit_fields(f))
            assert r.status in (303, 403), (r.status, r.raw[:300])
            if r.status == 303:
                assert "refused" in " ".join(r.header("location"))
            assert seen == []
            assert _snapshot(inst, ev) == before
            assert _attribution(inst, ev) == (None, None)

    run(body())


@pytest.mark.parametrize("case", ["closed_item", "op_not_needs_check"])
def test_el5_p11g_not_eligible(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                               case: str) -> None:
    """A. A closed doc item: the view shows NO action (asserted by GET before posting) and a
    POST of the earlier rendered form is refused. An open item whose op is not needs_check: the
    POST is refused by t134. Nothing changes in either case."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, _ver = await _open_doc_item(inst, lc, app, tok)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            if case == "closed_item":
                e4.q(inst, "UPDATE {s}.m05_followup_items SET state = 'resolved',"
                     " outcome_code = 'manual_documentation' WHERE item_id = %s", (item,))
                _r, pg2 = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
                assert _recorded_action(item) not in [x.action for x in pg2.forms]
                assert CHART_RECORDED not in pg2.raw
            else:
                e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET state = 'manual_pending'"
                     " WHERE event_id = %s", (ev,))
            before = _snapshot(inst, ev)
            r = await e4.post_form(app, f.action, b4.submit_fields(f))
            assert r.status == 303, (r.status, r.raw[:300])
            assert "refused" in " ".join(r.header("location"))
            assert _snapshot(inst, ev) == before

    run(body())


def test_el5_p11h_wrong_kind(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. chart-recorded on an E4 eligibility item (not the doc kind) -> 404; the item is
    unchanged."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            await make_event(inst, app, tok, "manual:no_coverage")
            ((el_item, _k, _st),) = [x for x in e4.el_items(inst)]
            before = e4.item_state(inst, el_item)
            ((ver,),) = e4.q(inst, "SELECT version FROM {s}.m05_followup_items"
                             " WHERE item_id = %s", (el_item,))
            r = await e4.post_form(app, _recorded_action(el_item), [
                ("csrf", lc.csrf_token), ("expected_version", str(ver))])
            assert r.status == 404, (r.status, r.raw[:300])
            assert e4.item_state(inst, el_item) == before

    run(body())


@pytest.mark.parametrize("shape", ["extra_field", "duplicate_field", "non_integer_version",
                                   "missing_version"])
def test_el5_p11i_form_shape(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                             monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """A. Field set exactly {csrf, expected_version}: an extra or duplicate field, a
    non-canonical version, or a missing version -> 400 before any doc call; unchanged."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, ver = await _open_doc_item(inst, lc, app, tok)
            seen = _spy_manual(lc, monkeypatch)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            fields = b4.submit_fields(f)
            if shape == "extra_field":
                fields.append(("staff_id", FALLBACK_STAFF))
            elif shape == "duplicate_field":
                fields.append(("expected_version", str(ver)))
            elif shape == "non_integer_version":
                fields = b4.submit_fields(f, expected_version="01")
            else:
                fields = b4.submit_fields(f, expected_version=None)
            before = _snapshot(inst, ev)
            r = await e4.post_form(app, f.action, fields)
            assert r.status == 400, (shape, r.status, r.raw[:300])
            assert seen == []
            assert _snapshot(inst, ev) == before

    run(body())


def test_el5_p11j_chart_page_read_only(pg: s.PgCluster, tmp_path: Path,
                                       test_zones: None) -> None:
    """C. The chart page stays read-only: no form on it, and POST to it is not served."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            ref, _ev = await make_event(inst, app, tok, "inactive")
            await doc_pass(app, tok)
            r, pg_ = await e4.page(app, f"/synthetic/ui/chart/{ref}")
            assert r.status == 200 and pg_.forms == [] and "<form" not in pg_.raw
            r = await e4.post_form(app, f"/synthetic/ui/chart/{ref}",
                                   [("csrf", lc.csrf_token)])
            # 415: the B3b gate refuses a form body on a path with no form handler (pre-routing).
            assert r.status in (404, 405, 415), (r.status, r.raw[:200])

    run(body())


def test_el5_p11k_list_shows_the_doc_item(pg: s.PgCluster, tmp_path: Path,
                                          test_zones: None) -> None:
    """A. With an open doc item present, the staff item list still renders (200) and lists it
    under its owner-decision-4 title (the synthetic read validators accept the doc kind)."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            await _open_doc_item(inst, lc, app, tok)
            r, pg_ = await e4.page(app, e4.UI_PREFIX)
            assert r.status == 200, (r.status, r.raw[:300])
            assert e5.DOC_TITLE in pg_.text

    run(body())


PENDING_TEXT: Final = "Being confirmed"                     # OD-EL15 (Rachel 2026-11-24), exact


def test_el5_p11l_pending_shows_being_confirmed(pg: s.PgCluster, tmp_path: Path,
                                                test_zones: None,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A (Codex D2 ruling (b), A6.3): an open doc item whose op is manual_pending (FIXTURE: the
    state t134 phase 1 leaves until phase 2 commits) is shown as "Being confirmed" with NO
    action; a POST of the form rendered while it was actionable is refused (t134), with op,
    item, permits, entries and attribution unchanged."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, _ver = await _open_doc_item(inst, lc, app, tok)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = _the_form(pg_, item)
            assert PENDING_TEXT not in pg_.raw
            e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET state = 'manual_pending'"
                 " WHERE event_id = %s", (ev,))
            r, pg2 = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert r.status == 200, (r.status, r.raw[:300])
            assert f"<p>{PENDING_TEXT}</p>" in pg2.raw
            assert _recorded_action(item) not in [x.action for x in pg2.forms]
            assert CHART_RECORDED not in pg2.raw
            seen = _spy_manual(lc, monkeypatch)
            before = _snapshot(inst, ev)
            r = await e4.post_form(app, f.action, b4.submit_fields(f))
            assert r.status == 303, (r.status, r.raw[:300])
            assert "refused" in " ".join(r.header("location"))
            assert len(seen) == 1                    # refused by t134, server-side
            assert _snapshot(inst, ev) == before

    run(body())


def test_el5_p11m_view_only_staff_sees_the_state(pg: s.PgCluster, tmp_path: Path,
                                                 test_zones: None) -> None:
    """A (Codex D2 ruling (b)): staff with view access only (no resolve) still open the doc
    item: 200, its title, and its display state (the bounded state read needs the view
    capability only, never the manual/write capability)."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (lc, app):
            tok = await e4.token(app)
            _ref, ev, item, _ver = await _open_doc_item(inst, lc, app, tok)
            bb.standing_override(inst, access=("read",))
            e4.q(inst, "UPDATE {s}.m05_el_doc_ops SET state = 'manual_pending'"
                 " WHERE event_id = %s", (ev,))
            r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert r.status == 200, (r.status, r.raw[:300])
            assert f"<h1>{e5.DOC_TITLE}</h1>" in pg_.raw
            assert f"<p>{PENDING_TEXT}</p>" in pg_.raw

    run(body())
