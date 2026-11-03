"""Fixture D: one patient with two open appointments on the same clinic day.

Aligned package: claude_fixture-d-proposal-2026-10-26-v1.md plus Codex's additive
assertions (codex_rehearsal-v1r4-and-fixture-d-review.md, relay 20261026T185120Z).
Tests only; no production change.

Uses the real ``default_fixtures``: patient ``syn-p-d`` with ``syn-a-4`` (10:30) and
``syn-a-5`` (15:00) on Wed 2026-10-28, America/New_York. Sends and replies happen on
Mon 2026-10-26, before both appointments.

- FD-1 is a characterization of existing per-appointment selection (two reminders for
  one patient on one day). It pins current demonstrator behaviour; it is not approval of
  a production texting policy (owner decision 2026-10-26: keep two texts for the demo,
  production choice goes to the gap register).
- FD-3 uses SIMULATED correlation (an in-process argument). It does not verify real
  reply matching.
- In the stub demo every reminder goes to the one configured test number, so the opt-out
  in FD-4 is phone-level on that number.

Everything is synthetic.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from haloflow_demo.inbound import receive
from haloflow_demo.selection import SkipReason, select_eligible
from haloflow_demo.sending import StubGateway, send_one
from haloflow_demo.synthetic_ehr import Fixtures, SyntheticEhr, default_fixtures
from haloflow_demo.tasks import run_task

from .test_inbound import (
    NOW,
    REPLY_AT,
    TENANT,
    TEST_NUMBER,
    _clock,
    _disposition,
    _env,
    _event,
    _items,
    _own,
    _process,
    _q,
    _reminders,
    _sig,
    _tasks,
)

FIXTURE_DAY = date(2026, 10, 28)
TZ = "America/New_York"
D_FIRST, D_SECOND = "syn-a-4", "syn-a-5"


def _fx() -> Fixtures:
    return default_fixtures(FIXTURE_DAY, TZ)


def _ehr() -> SyntheticEhr:
    return SyntheticEhr(_fx())


async def _send_d(own: Any, s: Any, appt_id: str, gw: StubGateway | None = None) -> Any:
    fx = _fx()
    appt = next(a for a in fx.appointments if a.emr_appt_id == appt_id)
    return await send_one(own, s, appt, fx.patients[appt.emr_patient_id],
                          gw or StubGateway(TEST_NUMBER), tenant_id=TENANT, now=_clock(NOW))


async def _deliver(own: Any, s: Any, text: str, reply_id: str,
                   correlation: int | None = None) -> Any:
    """Deliver one signed simulated reply event; calling twice replays the same bytes."""
    raw = _event(reply_id, text, received=REPLY_AT)
    return await receive(own, s, raw, [_sig(raw, int(REPLY_AT.timestamp()))],
                         now=_clock(REPLY_AT), simulated_correlation=correlation)


def _count(url: str, table: str) -> int:
    return int(_q(url, f"SELECT count(*) FROM {table}")[0][0])


def _quota_used(url: str) -> int:
    return int(_q(url, "SELECT coalesce(sum(used), 0) FROM send_quota")[0][0])


# ── FD-1 (U): selection characterization ──────────────────────────────────────


def test_fd1_one_patient_two_appointments_both_selected() -> None:
    fx = _fx()
    sel = select_eligible(fx.appointments, fx.patients, FIXTURE_DAY, TZ, existing_appt_ids=set())
    eligible = {e.appointment.emr_appt_id: e.patient.emr_patient_id for e in sel.eligible}
    assert eligible[D_FIRST] == "syn-p-d"
    assert eligible[D_SECOND] == "syn-p-d"
    assert sorted(eligible) == ["syn-a-1", "syn-a-2", "syn-a-3", D_FIRST, D_SECOND]
    assert {(s.emr_appt_id, s.reason) for s in sel.skipped} == {
        ("syn-a-6", SkipReason.NO_PHONE), ("syn-a-7", SkipReason.CANCELLED)}


# ── FD-2 (D): uncorrelated reply, replayed ────────────────────────────────────


async def test_fd2_uncorrelated_reply_is_staff_only_and_replay_safe(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        await _send_d(own, s, D_FIRST)
        await _send_d(own, s, D_SECOND)
        first = await _deliver(own, s, "1", "rid-d")
        await _process(own, s, ehr)
        replay = await _deliver(own, s, "1", "rid-d")
        await _process(own, s, ehr)
    finally:
        await own.close()
    assert first.reason != "duplicate" and first.inbound_id is not None
    assert (replay.reason, replay.inbound_id) == ("duplicate", first.inbound_id)
    assert _count(demo_db_url, "inbound_messages") == 1
    assert _disposition(demo_db_url, "rid-d") == ("PROCESSED", "uncorrelated_reply")
    assert _items(demo_db_url) == [("uncorrelated_reply", None)]
    assert _reminders(demo_db_url) == {D_FIRST: "SUBMITTED", D_SECOND: "SUBMITTED"}
    assert _tasks(demo_db_url) == []
    assert ehr.applied_writes == 0
    assert (await ehr.read_confirmation(D_FIRST)).value == "1"
    assert (await ehr.read_confirmation(D_SECOND)).value == "1"


# ── FD-3 (D): simulated correlation to one appointment, replayed ──────────────


async def test_fd3_simulated_correlation_acts_on_one_appointment_only(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        await _send_d(own, s, D_FIRST)
        r5 = await _send_d(own, s, D_SECOND)
        first = await _deliver(own, s, "1", "rid-d5", correlation=r5.reminder_id)
        await _process(own, s, ehr)
        replay = await _deliver(own, s, "1", "rid-d5", correlation=r5.reminder_id)
        await _process(own, s, ehr)
        assert ehr.applied_writes == 0, "processing never writes to the EHR"
        tasks = _tasks(demo_db_url)
        assert len(tasks) == 1 and tasks[0][0] == r5.reminder_id and tasks[0][1] == "PENDING"
        task_id = int(_q(demo_db_url, "SELECT id FROM writeback_tasks")[0][0])
        state = await run_task(own, ehr, task_id)
    finally:
        await own.close()
    assert (replay.reason, replay.inbound_id) == ("duplicate", first.inbound_id)
    assert _count(demo_db_url, "inbound_messages") == 1
    assert _reminders(demo_db_url) == {D_FIRST: "SUBMITTED", D_SECOND: "CONFIRMED"}
    assert _items(demo_db_url) == []
    assert state == "DONE"
    assert ehr.applied_writes == 1
    assert (await ehr.read_confirmation(D_SECOND)).value == "2"
    assert (await ehr.read_confirmation(D_FIRST)).value == "1", "other appointment untouched"


# ── FD-4 (D): uncorrelated STOP, replayed; later send refused ─────────────────


async def test_fd4_uncorrelated_stop_opts_out_once_and_blocks_later_send(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    gw = StubGateway(TEST_NUMBER)
    try:
        await _send_d(own, s, D_FIRST)
        await _send_d(own, s, D_SECOND)
        first = await _deliver(own, s, "STOP", "rid-stop")
        await _process(own, s, ehr)
        replay = await _deliver(own, s, "STOP", "rid-stop")
        await _process(own, s, ehr)
        quota_before = _quota_used(demo_db_url)
        later = await _send_d(own, s, "syn-a-1", gw=gw)
        quota_after = _quota_used(demo_db_url)
    finally:
        await own.close()
    assert (replay.reason, replay.inbound_id) == ("duplicate", first.inbound_id)
    assert _disposition(demo_db_url, "rid-stop") == ("PROCESSED", "opt_out")
    assert _count(demo_db_url, "opt_outs") == 1
    assert (later.state, later.reason) == ("NOT_SENT", "opted_out")
    assert gw.calls == []
    assert quota_after == quota_before == 2
    assert _tasks(demo_db_url) == []
    assert _reminders(demo_db_url) == {D_FIRST: "SUBMITTED", D_SECOND: "SUBMITTED",
                                       "syn-a-1": "NOT_SENT"}
    assert ehr.applied_writes == 0
