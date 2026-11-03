"""F-R1-1 — duplicate prevention by synthetic slot, across restarts (C-1).

A restart regenerates the synthetic appointments with epoch-qualified IDs
(``syn-e<epoch>-<date>-a-N``, Codex 175423Z P1). Before C-1, selection and the
claim deduplicated on that ID only, so a restart made every already-reminded
slot eligible again (R-1 F-R1-1, reproduced 2026-10-27).

Requirement pinned here (plan v3.1 §5 rows "Duplicate batch or webhook" and
"Restart": no duplicate send or business action; pending work resumes): an
appointment is not reminded when a reminder record already exists for the same
slot — the same tenant, the same synthetic patient and the same start INSTANT
(timestamptz equality, not formatted local time) — whatever that record's state
and whichever epoch created it. Preview, single send and batch agree, and the
claim enforces it under concurrency. Write-back pinning to the original epoch
(fail-closed) is unchanged and covered by test_inbound.

Design (package v3): inside the claim transaction, a transaction-scoped
advisory lock keyed on the slot is taken before the slot check and the INSERT,
so same-slot claims serialise and other claims do not. ``hook`` is a test seam
awaited between the check and the INSERT. The outbound-once live test opts out
(``slot_dedupe=False``): each explicit run is a distinct send by design.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import psycopg
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.reminders import claim_reminder
from haloflow_demo.sending import StubGateway, send_one
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.synthetic_ehr import ApptStatus, SyntheticAppointment, SyntheticPatient

TEST_NUMBER = "+15550100199"   # fictional 555-01xx
SENDER = "+15550100100"
SECRET = "test-callback-secret-0123456789"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # target clinic date 2026-10-28
START = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)        # 09:00 EDT


# ── helpers (independent of the module under test) ───────────────────────────


def _env(demo_env: dict[str, str], **extra: str) -> dict[str, str]:
    return {**demo_env, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER, "DEMO_SMS_SENDER": SENDER,
            "DEMO_CALLBACK_SECRET": SECRET, **extra}


async def _own(env: dict[str, str]):  # type: ignore[no-untyped-def]
    s = DemoSettings.from_env(env)
    return s, await acquire_ownership(s, validate_demo_database_url(s))


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def _counts(url: str) -> tuple[int, int, int]:
    """(reminder rows, contact_history rows, total quota used)."""
    r = _q(url, "SELECT count(*) FROM reminder_records")[0][0]
    c = _q(url, "SELECT count(*) FROM contact_history")[0][0]
    u = _q(url, "SELECT COALESCE(sum(used), 0) FROM send_quota")[0][0]
    return int(r), int(c), int(u)


def _appt(appt_id: str, patient: str = "p1", start: datetime = START) -> SyntheticAppointment:
    return SyntheticAppointment(appt_id, patient, start, ApptStatus.SCHEDULED)


async def _ids(svc: DbStaffService) -> dict[str, str]:
    """'a-N' → the current epoch's appointment id, from the staff page data."""
    return {a["emr_appt_id"].rsplit("-", 2)[-2] + "-" + a["emr_appt_id"].rsplit("-", 1)[-1]:
            a["emr_appt_id"] for a in (await svc.page_data())["appointments"]}


async def _epoch1(env: dict[str, str], url: str) -> None:
    """Epoch 1: a-1 SUBMITTED; a-2 NOT_SENT(paused); a-3 and a-4 sent, then set to
    CONFIRMED and UNCERTAIN (setup only); a-5 (Patient D, other time) never claimed."""
    s, own = await _own(env)
    svc = DbStaffService(own, s, now=_clock(NOW))
    try:
        ids = await _ids(svc)
        assert ids["a-1"].startswith("syn-e1-")
        assert (await svc.send_to_appointment(ids["a-1"]))["state"] == "SUBMITTED"
        await svc.set_paused(True)
        assert (await svc.send_to_appointment(ids["a-2"]))["state"] == "NOT_SENT"
        await svc.set_paused(False)
        assert (await svc.send_to_appointment(ids["a-3"]))["state"] == "SUBMITTED"
        assert (await svc.send_to_appointment(ids["a-4"]))["state"] == "SUBMITTED"
    finally:
        await own.close()
    _q(url, "UPDATE reminder_records SET state = 'CONFIRMED' WHERE emr_appt_id = %s",
       (ids["a-3"],))
    _q(url, "UPDATE reminder_records SET state = 'UNCERTAIN', reason = 'timeout'"
            " WHERE emr_appt_id = %s", (ids["a-4"],))


# ── T-SLOT-1/2/3: after a restart, preview, single send and batch agree ───────


async def test_slot1_single_send_after_restart_is_skipped_for_every_state(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    await _epoch1(env, demo_db_url)
    before = _counts(demo_db_url)
    s, own = await _own(env)                          # restart: epoch 2
    svc = DbStaffService(own, s, now=_clock(NOW))
    try:
        ids = await _ids(svc)
        assert ids["a-1"].startswith("syn-e2-")
        results = {k: await svc.send_to_appointment(ids[k])
                   for k in ("a-1", "a-2", "a-3", "a-4")}
    finally:
        await own.close()
    for k, r in results.items():   # SUBMITTED, NOT_SENT, CONFIRMED, UNCERTAIN
        assert r == {"reminder_id": None, "state": "SKIPPED", "reason": "not_eligible"}, k
    assert _counts(demo_db_url) == before
    assert _q(demo_db_url, "SELECT count(*) FROM reminder_records WHERE emr_appt_id LIKE %s",
              ("syn-e2-%",)) == [(0,)]


async def test_slot2_batch_after_restart_sends_only_never_reminded_slots(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    await _epoch1(env, demo_db_url)
    r0, c0, u0 = _counts(demo_db_url)
    s, own = await _own(env)
    svc = DbStaffService(own, s, now=_clock(NOW))
    try:
        ids = await _ids(svc)
        sent = await svc.send_batch()
    finally:
        await own.close()
    assert [x["state"] for x in sent] == ["SUBMITTED"]
    assert _q(demo_db_url, "SELECT emr_appt_id, state FROM reminder_records"
                           " WHERE emr_appt_id LIKE %s", ("syn-e2-%",)) == [
        (ids["a-5"], "SUBMITTED")]
    assert _counts(demo_db_url) == (r0 + 1, c0 + 1, u0 + 1)


async def test_slot3_preview_after_restart_marks_reminded_slots(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    await _epoch1(env, demo_db_url)
    before = _counts(demo_db_url)
    s, own = await _own(env)
    svc = DbStaffService(own, s, now=_clock(NOW))
    try:
        ids = await _ids(svc)
        preview = {p["emr_appt_id"]: p["decision"] for p in await svc.preview_batch()}
    finally:
        await own.close()
    for k in ("a-1", "a-2", "a-3", "a-4"):
        assert preview[ids[k]] == "skip: already_has_reminder", k
    assert preview[ids["a-5"]] == "eligible"          # Patient D, other start time
    assert preview[ids["a-6"]] == "skip: no_phone"
    assert preview[ids["a-7"]] == "skip: cancelled"
    assert _counts(demo_db_url) == before             # preview is read-only


# ── T-SLOT-4: the claim itself refuses an existing slot; controls ─────────────


async def test_slot4_claim_refuses_other_id_for_an_existing_slot(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(demo_env)
    same_instant_other_offset = START.astimezone(timezone(timedelta(hours=-4)))
    try:
        first = await claim_reminder(own, "t", _appt("x-1"))
        dup = await claim_reminder(own, "t", _appt("x-2", start=same_instant_other_offset))
        other_tenant = await claim_reminder(own, "t2", _appt("x-3"))
        other_patient = await claim_reminder(own, "t", _appt("x-4", patient="p2"))
        later = START + timedelta(hours=6)
        other_instant = await claim_reminder(own, "t", _appt("x-5", start=later))
        next_day = await claim_reminder(own, "t", _appt("x-6", start=START + timedelta(days=1)))
    finally:
        await own.close()
    assert first is not None
    assert dup is None
    assert None not in (other_tenant, other_patient, other_instant, next_day)
    assert sorted(r[0] for r in _q(demo_db_url, "SELECT emr_appt_id FROM reminder_records")) == [
        "x-1", "x-3", "x-4", "x-5", "x-6"]


async def test_slot5_date_rollover_still_sends_the_next_day(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    clock = {"now": NOW}
    s, own = await _own(_env(demo_env))
    svc = DbStaffService(own, s, now=lambda: clock["now"])
    try:
        day1 = await _ids(svc)
        assert (await svc.send_to_appointment(day1["a-1"]))["state"] == "SUBMITTED"
        clock["now"] = NOW + timedelta(days=1)
        day2 = await _ids(svc)
        second = await svc.send_to_appointment(day2["a-1"])
    finally:
        await own.close()
    assert second["state"] == "SUBMITTED"


# ── T-SLOT-6: concurrency — different IDs, same slot ──────────────────────────


def _blocked_on_lock(url: str) -> list[tuple[int, list[int]]]:
    """Backends of this test database waiting on a lock in a claim's slot lock,
    with the PIDs blocking them (pg_blocking_pids)."""
    return [(int(r[0]), list(r[1])) for r in _q(url, (
        "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity"
        " WHERE datname = current_database() AND pid <> pg_backend_pid()"
        " AND wait_event_type = 'Lock' AND query LIKE %s"
        " AND cardinality(pg_blocking_pids(pid)) > 0"), ("%pg_advisory_xact_lock%",))]


async def test_slot6_concurrent_claims_different_ids_same_slot_serialise(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(demo_env)
    reached = asyncio.Event()
    release = asyncio.Event()
    a_pid: list[int] = []

    async def hold(point: str) -> None:
        if point == "slot_checked":
            a_pid.extend(int(r[0]) for r in _q(demo_db_url, (
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid() AND state = 'idle in transaction'")))
            reached.set()
            await release.wait()

    a = asyncio.create_task(claim_reminder(own, "t", _appt("y-1"), hook=hold))
    b: asyncio.Task[int | None] | None = None
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        b = asyncio.create_task(claim_reminder(own, "t", _appt("y-2")))
        blocked: list[tuple[int, list[int]]] = []
        loop = asyncio.get_running_loop()
        end = loop.time() + 5.0
        while loop.time() < end and not blocked and not b.done():
            blocked = _blocked_on_lock(demo_db_url)
            if not blocked:
                await asyncio.sleep(0.02)
        assert blocked, "second claim must be waiting on the slot lock while the first holds it"
        assert any(set(blockers) & set(a_pid) for _, blockers in blocked), (blocked, a_pid)
        assert not b.done()
        release.set()
        ra, rb = await asyncio.wait_for(asyncio.gather(a, b), timeout=10)
    finally:
        release.set()
        for task in (a, b):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(x for x in (a, b) if x is not None), return_exceptions=True)
        await own.close()
    assert ra is not None and rb is None
    assert _q(demo_db_url, "SELECT emr_appt_id FROM reminder_records") == [("y-1",)]


async def test_slot6b_concurrent_sends_same_slot_make_one_contact_and_one_call(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    pt = SyntheticPatient("p1", "Synthetic Patient A", "alpha")
    try:
        results = await asyncio.gather(*(
            send_one(own, s, _appt(f"z-{i}"), pt, gw, tenant_id="t", now=_clock(NOW))
            for i in range(1, 4)))
    finally:
        await own.close()
    assert sorted(r.state for r in results) == ["SKIPPED", "SKIPPED", "SUBMITTED"]
    assert len(gw.calls) == 1
    assert _counts(demo_db_url) == (1, 1, 1)
