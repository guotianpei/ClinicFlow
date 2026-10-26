"""No-response handling (plan v3.1 Day 6). Tests written before the code.

Package: Shared Workspace ``Work Session 2026-10-26/claude_no-response-package-2026-10-26-v1r1.md``
(Codex ALIGN for tests-only, 20261026T132512Z). Owner decisions (2026-10-26):
B-1 a SUBMITTED reminder with no reply 24 elapsed hours after submission becomes
NO_RESPONSE plus one staff item; B-2 a reply after that is staff-only.

Interface under test (does not exist yet):
- ``haloflow_demo.noresponse.flag_no_response(own, now, *, hook=None) -> list[int]``
  returns the reminder ids flagged. ``hook(stage)`` is awaited with ``"locked"``
  right after the match lock is held and ``"flagged"`` between the state update
  and the item insert (test seams, same pattern as the existing dispatch hooks).
  The match-lock statement carries the tag ``match:noresponse`` (lock-wait tests
  find it in pg_stat_activity, as for ``match:receipt``). If closing or owner
  loss is seen after the lock wait, it returns ``[]`` and changes nothing.
- ``haloflow_demo.noresponse.ITEM_KIND == "no_response"``.
- ``DbStaffService.check_no_response() -> list[int]``; the dispatcher loop runs it.
- Staff page button ``Check for no reply now`` posting to ``/demo/noresponse/check``.

``updated_at`` is written by the database (``now()``), so tests read the stored
submission time and drive the injected clock relative to it. Where a case needs
an exact stored instant (DST, excluded states, provenance), the test arranges the
row with SQL; that arrangement is labelled in the test.
Everything is synthetic. PG16 development evidence only.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.inbound import process_pending
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.sending import StubGateway, send_one
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.synthetic_ehr import (
    ApptStatus,
    Fixtures,
    SyntheticAppointment,
    SyntheticEhr,
    SyntheticPatient,
)

from .test_inbound import (
    OTHER_NUMBER,
    SECRET,
    SENDER,
    TEST_NUMBER,
    _reply,
    _wait_for_blocked,
)

TENANT = "demo-clinic"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # clock passed to send_one (quota day)
H24 = timedelta(hours=24)
US = timedelta(microseconds=1)
DEMO = Path(__file__).resolve().parents[2] / "demo" / "haloflow_demo"


# ── helpers ──────────────────────────────────────────────────────────────────


def _env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER, "DEMO_SMS_SENDER": SENDER,
            "DEMO_CALLBACK_SECRET": SECRET}


async def _own(env: dict[str, str]):  # type: ignore[no-untyped-def]
    s = DemoSettings.from_env(env)
    return s, await acquire_ownership(s, validate_demo_database_url(s))


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def _start() -> datetime:
    """Appointment three days from the real clock, so it is in the future at check time."""
    return (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)


def _patient(i: int) -> SyntheticPatient:
    return SyntheticPatient(f"syn-p-{i}", f"Synthetic Patient {i}", phone_alias="alpha")


def _appt(i: int, start: datetime) -> SyntheticAppointment:
    return SyntheticAppointment(f"syn-a-{i}", f"syn-p-{i}", start, ApptStatus.SCHEDULED,
                                confirmation="1")


def _ehr(start: datetime, n: int = 4) -> SyntheticEhr:
    return SyntheticEhr(Fixtures(patients={f"syn-p-{i}": _patient(i) for i in range(1, n + 1)},
                                 appointments=[_appt(i, start) for i in range(1, n + 1)]))


async def _send(own: Any, s: DemoSettings, i: int, start: datetime,
                gw: StubGateway | None = None,
                hook: Callable[[str], Awaitable[None]] | None = None) -> int:
    r = await send_one(own, s, _appt(i, start), _patient(i), gw or StubGateway(TEST_NUMBER),
                       tenant_id=TENANT, now=_clock(NOW), dispatch_hook=hook)
    assert r.state == "SUBMITTED", r
    assert r.reminder_id is not None
    return int(r.reminder_id)


def _sent_at(url: str, rid: int) -> datetime:
    rows = _q(url, "SELECT updated_at FROM reminder_records WHERE id = %s AND state = 'SUBMITTED'",
              (rid,))
    assert len(rows) == 1
    return rows[0][0]  # type: ignore[no-any-return]


def _state(url: str, rid: int) -> str:
    return str(_q(url, "SELECT state FROM reminder_records WHERE id = %s", (rid,))[0][0])


def _nr_items(url: str) -> list[tuple[int, str]]:
    return [(int(r), str(d)) for r, d in _q(
        url, "SELECT reminder_id, detail FROM follow_up_items WHERE kind = 'no_response'"
             " ORDER BY reminder_id")]


async def _flag(own: Any, at: datetime,
                hook: Callable[[str], Awaitable[None]] | None = None) -> list[int]:
    from haloflow_demo.noresponse import flag_no_response
    return sorted(await flag_no_response(own, _clock(at), hook=hook))


async def _process(own: Any, s: DemoSettings, ehr: Any, at: datetime) -> Any:
    return await process_pending(own, s, ehr, now=_clock(at))


# ── NR-T0: source inventory of writers to reminder_records (U) ───────────────

# Every UPDATE of reminder_records at 3be6a4e, normalised. While a row is
# SUBMITTED, none of these may write it except the guarded transition out of
# SUBMITTED (inbound.py). The first three sending.py statements run before the
# gateway call (call-ordering invariant, not an SQL guard); _mark is guarded
# on CLAIMED; recovery is guarded on CLAIMED.
_ALLOWED_EXISTING = sorted([
    ("inbound.py", "UPDATE reminder_records SET state = :s, updated_at = now() "
                   "WHERE id = :r AND state = 'SUBMITTED' RETURNING id"),
    ("ownership.py", "UPDATE reminder_records SET state = 'UNCERTAIN', reason = "
                     "'restart_after_claim', updated_at = now() WHERE state = 'CLAIMED'"),
    ("sending.py", "UPDATE reminder_records SET state = :s, reason = :r, provider_message_id = "
                   ":p, updated_at = now() WHERE id = :id AND state = 'CLAIMED'"),
    ("sending.py", "UPDATE reminder_records SET contact_seq = :q WHERE id = :r"),
    ("sending.py", "UPDATE reminder_records SET state = 'NOT_SENT', reason = :r, "
                   "updated_at = now() WHERE id = :id"),
    ("sending.py", "UPDATE reminder_records SET reserved_day = :d, mode = :m, phone_key = :k "
                   "WHERE id = :id"),
])


def _reminder_updates() -> list[tuple[str, str]]:
    found = []
    for p in sorted(DEMO.glob("*.py")):
        for n in ast.walk(ast.parse(p.read_text())):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and re.search(r"UPDATE\s+reminder_records", n.value)):
                found.append((p.name, re.sub(r"\s+", " ", n.value).strip()))
    return sorted(found)


def test_nr_t0_reminder_writers_are_exactly_the_reviewed_set() -> None:
    found = _reminder_updates()
    scanner = [sql for f, sql in found if f == "noresponse.py"]
    assert len(scanner) == 1, scanner
    sql = scanner[0]
    assert "SET state = 'NO_RESPONSE'" in sql
    assert "state = 'SUBMITTED'" in sql
    assert "interval '24 hours'" in sql
    assert "'1 day'" not in sql
    assert [x for x in found if x[0] != "noresponse.py"] == _ALLOWED_EXISTING


async def test_nr_t0b_updated_at_records_the_submitted_transition_not_the_claim(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    """Behavioural check of the timestamp invariant through a real stub send."""
    s, own = await _own(_env(demo_env))
    after_wait: list[datetime] = []

    async def slow(stage: str) -> None:
        if stage == "contact_admitted":
            await asyncio.sleep(1.0)  # between claim/admission and the gateway call
            after_wait.append(datetime.now(UTC))

    try:
        rid = await _send(own, s, 1, _start(), hook=slow)
    finally:
        await own.close()
    created, updated = _q(demo_db_url, "SELECT created_at, updated_at FROM reminder_records"
                                       " WHERE id = %s", (rid,))[0]
    assert updated - created >= timedelta(seconds=0.9), (created, updated)
    assert updated >= after_wait[0] - timedelta(milliseconds=50), (updated, after_wait)


# ── NR-T1 / NR-T2: the 24-elapsed-hour boundary ──────────────────────────────


async def test_nr_t1_exact_boundary(demo_env: dict[str, str], demo_db_url: str) -> None:
    s, own = await _own(_env(demo_env))
    try:
        start = _start()
        r1 = await _send(own, s, 1, start)
        t = _sent_at(demo_db_url, r1)
        assert await _flag(own, t + H24 - US) == []
        assert _state(demo_db_url, r1) == "SUBMITTED"
        assert await _flag(own, t + H24) == [r1]  # equality flags
        r2 = await _send(own, s, 2, start)
        t2 = _sent_at(demo_db_url, r2)
        assert await _flag(own, t2 + H24 + US) == [r2]
    finally:
        await own.close()
    assert _state(demo_db_url, r1) == "NO_RESPONSE"
    assert [r for r, _ in _nr_items(demo_db_url)] == [r1, r2]
    assert all("No reply" in d for _, d in _nr_items(demo_db_url))


async def test_nr_t2_24_elapsed_hours_across_dst_end(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    """Submitted 2026-10-31 12:00 EDT; 24 elapsed hours later is 2026-11-01 11:00 EST.
    The session time zone is the clinic zone, so a DST-aware '1 day' would give 12:00 EST."""
    s, own = await _own(_env(demo_env))
    sub = datetime(2026, 10, 31, 16, 0, tzinfo=UTC)   # 12:00 EDT
    start = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    try:
        rid = await _send(own, s, 1, start)
        # Arrangement: pin the stored submission instant.
        _q(demo_db_url, "UPDATE reminder_records SET updated_at = %s WHERE id = %s", (sub, rid))
        _q(demo_db_url, "ALTER DATABASE " + demo_db_url.rsplit("/", 1)[1]
           + " SET timezone = 'America/New_York'")
        await own.engine.dispose()  # new connections pick up the zone
        at_1059_est = datetime(2026, 11, 1, 15, 59, tzinfo=UTC)
        at_1100_est = datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
        assert await _flag(own, at_1059_est) == []
        assert await _flag(own, at_1100_est) == [rid]
    finally:
        await own.close()


# ── NR-T3: only SUBMITTED qualifies ──────────────────────────────────────────


@pytest.mark.parametrize("state", ["NOT_SENT", "REJECTED", "CLAIMED", "UNCERTAIN", "CONFIRMED",
                                   "RESCHEDULE_REQUESTED", "CANCEL_REQUESTED"])
async def test_nr_t3_other_states_never_flag(
    state: str, demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        t = _sent_at(demo_db_url, rid)
        # arranged: the excluded state, stored submission time unchanged
        _q(demo_db_url, "UPDATE reminder_records SET state = %s WHERE id = %s", (state, rid))
        assert await _flag(own, t + H24 * 2) == []
    finally:
        await own.close()
    assert _state(demo_db_url, rid) == state
    assert _nr_items(demo_db_url) == []


# ── NR-T4 / NR-T5: answered vs not answered ──────────────────────────────────


@pytest.mark.parametrize(("text", "correlate", "process"), [
    ("1", True, False),        # (a) explicit correlation, still RECEIVED
    ("1", False, False),       # (b) single match, uncorrelated, RECEIVED
    ("maybe", False, True),    # UNCLEAR, PROCESSED
    ("maybe", True, False),    # UNCLEAR, explicit, RECEIVED
    ("STOP", True, True),      # STOP answers this reminder
])
async def test_nr_t4_answered_reminder_is_not_flagged(
    text: str, correlate: bool, process: bool, demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    start = _start()
    try:
        rid = await _send(own, s, 1, start)
        t = _sent_at(demo_db_url, rid)
        reply_at = datetime.now(UTC)
        await _reply(own, s, text, reply_id="rid-1", at=reply_at,
                     correlation=rid if correlate else None)
        if process:
            await _process(own, s, _ehr(start), reply_at)
        assert await _flag(own, t + H24 * 2) == []
    finally:
        await own.close()
    assert _nr_items(demo_db_url) == []


async def test_nr_t5a_explicit_reply_to_a1_does_not_answer_a2(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    start = _start()
    try:
        r1 = await _send(own, s, 1, start)
        r2 = await _send(own, s, 2, start)
        await _reply(own, s, "maybe", reply_id="rid-1", at=datetime.now(UTC), correlation=r1)
        t = max(_sent_at(demo_db_url, r1), _sent_at(demo_db_url, r2))
        assert await _flag(own, t + H24) == [r2]
    finally:
        await own.close()


async def test_nr_t5b_ambiguous_reply_answers_neither(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    start = _start()
    try:
        r1 = await _send(own, s, 1, start)
        r2 = await _send(own, s, 2, start)
        await _reply(own, s, "1", reply_id="rid-1", at=datetime.now(UTC))  # 2 candidates
        t = max(_sent_at(demo_db_url, r1), _sent_at(demo_db_url, r2))
        assert await _flag(own, t + H24) == [r1, r2]
    finally:
        await own.close()


async def test_nr_t5c_other_provenance_reply_does_not_answer(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        await _reply(own, s, "maybe", reply_id="rid-1", at=datetime.now(UTC), correlation=rid)
        _q(demo_db_url, "UPDATE inbound_messages SET provenance = 'real'")  # arranged
        assert await _flag(own, _sent_at(demo_db_url, rid) + H24) == [rid]
    finally:
        await own.close()


async def test_nr_t5d_no_match_reply_does_not_answer(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        await _reply(own, s, "1", reply_id="rid-1", at=datetime.now(UTC), sender=OTHER_NUMBER)
        assert await _flag(own, _sent_at(demo_db_url, rid) + H24) == [rid]
    finally:
        await own.close()


# ── NR-T6: appointment already started ───────────────────────────────────────


async def test_nr_t6_started_appointment_is_not_flagged(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        t = _sent_at(demo_db_url, rid)
        appt = _q(demo_db_url, "SELECT appt_start FROM reminder_records WHERE id = %s",
                  (rid,))[0][0]
        assert await _flag(own, max(t + H24, appt)) == []      # start <= now
        assert _state(demo_db_url, rid) == "SUBMITTED"
    finally:
        await own.close()


# ── NR-T7 / NR-T8 / NR-T15: idempotence, atomicity, restart ──────────────────


async def test_nr_t7_repeated_and_concurrent_checks(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        at = _sent_at(demo_db_url, rid) + H24
        a, b = await asyncio.gather(_flag(own, at), _flag(own, at))
        assert sorted(a + b) == [rid]
        assert await _flag(own, at) == []
    finally:
        await own.close()
    assert _nr_items(demo_db_url) == [(rid, _nr_items(demo_db_url)[0][1])]


async def test_nr_t8_failure_between_update_and_item_commits_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))

    async def boom(stage: str) -> None:
        if stage == "flagged":
            raise RuntimeError("injected failure after the state update")

    try:
        rid = await _send(own, s, 1, _start())
        at = _sent_at(demo_db_url, rid) + H24
        with pytest.raises(RuntimeError):
            await _flag(own, at, hook=boom)
        assert _state(demo_db_url, rid) == "SUBMITTED"
        assert _nr_items(demo_db_url) == []
        assert await _flag(own, at) == [rid]
    finally:
        await own.close()
    assert len(_nr_items(demo_db_url)) == 1


async def test_nr_t15_restart_after_flag_adds_no_second_item(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    s, own = await _own(env)
    try:
        rid = await _send(own, s, 1, _start())
        at = _sent_at(demo_db_url, rid) + H24
        assert await _flag(own, at) == [rid]
    finally:
        await own.close()
    s2, own2 = await _own(env)
    try:
        assert await _flag(own2, at + H24) == []
    finally:
        await own2.close()
    assert len(_nr_items(demo_db_url)) == 1


async def test_nr_t15b_restart_with_pending_submitted_work_flags_original_once(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    s, own = await _own(env)
    try:
        rid = await _send(own, s, 1, _start())
        sent = _sent_at(demo_db_url, rid)
    finally:
        await own.close()
    s2, own2 = await _own(env)  # new process: nothing flagged yet
    try:
        assert await _flag(own2, sent + H24 - US) == []
        assert await _flag(own2, sent + H24) == [rid]
        assert await _flag(own2, sent + H24 * 2) == []
    finally:
        await own2.close()
    assert [r for r, _ in _nr_items(demo_db_url)] == [rid]
    assert _state(demo_db_url, rid) == "NO_RESPONSE"


# ── NR-T9 / NR-T10 / NR-T11: serialisation with reply receipt ────────────────


async def _cleanup(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel and await any spawned task still running, so a failed assertion
    leaks neither a task nor its database lock."""
    for t in tasks:
        if not t.done():
            t.cancel()
    for t in tasks:
        with contextlib.suppress(BaseException):
            await t


def _release(holder: psycopg.Connection[Any] | None) -> None:
    if holder is not None and not holder.closed:
        with contextlib.suppress(Exception):
            holder.rollback()
        holder.close()


async def test_nr_t9_receipt_commits_first_then_no_flag(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    scanner: list[asyncio.Task[Any]] = []
    try:
        rid = await _send(own, s, 1, _start())
        at = _sent_at(demo_db_url, rid) + H24

        async def on_receipt(stage: str) -> None:
            if stage == "receipt_locked":
                scanner.append(asyncio.create_task(_flag(own, at)))
                await _wait_for_blocked(demo_db_url, "match:noresponse",
                                        blocker_tag="match:receipt")

        await _reply(own, s, "maybe", reply_id="rid-1", at=datetime.now(UTC), hook=on_receipt)
        assert await scanner[0] == []
    finally:
        await _cleanup(scanner)
        await own.close()
    assert _state(demo_db_url, rid) == "SUBMITTED"
    assert _nr_items(demo_db_url) == []


async def test_nr_t10_scanner_commits_first_then_late_yes_is_staff_only(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    start = _start()
    ehr = _ehr(start)
    receipt: list[asyncio.Task[Any]] = []
    try:
        rid = await _send(own, s, 1, start)
        at = _sent_at(demo_db_url, rid) + H24

        async def on_locked(stage: str) -> None:
            if stage == "locked":
                receipt.append(asyncio.create_task(
                    _reply(own, s, "1", reply_id="rid-1", at=datetime.now(UTC))))
                await _wait_for_blocked(demo_db_url, "match:receipt",
                                        blocker_tag="match:noresponse")

        assert await _flag(own, at, hook=on_locked) == [rid]
        await receipt[0]
        await _process(own, s, ehr, datetime.now(UTC))
    finally:
        await _cleanup(receipt)
        await own.close()
    assert _state(demo_db_url, rid) == "NO_RESPONSE"
    rows = _q(demo_db_url, "SELECT kind, reminder_id FROM follow_up_items ORDER BY kind")
    assert ("reply_after_resolution", rid) in rows
    assert ("no_response", rid) in rows
    assert _q(demo_db_url, "SELECT count(*) FROM writeback_tasks") == [(0,)]
    assert ehr.write_attempts == 0


@pytest.mark.parametrize("loss", ["closing", "owner_lost"])
async def test_nr_t11_closing_or_owner_lost_during_lock_wait_changes_nothing(
    loss: str, demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    holder: psycopg.Connection[Any] | None = None
    tasks: list[asyncio.Task[Any]] = []
    try:
        rid = await _send(own, s, 1, _start())
        at = _sent_at(demo_db_url, rid) + H24
        holder = psycopg.connect(demo_db_url)
        holder.execute("SELECT 1 FROM demo_match_lock WHERE id = 1 FOR UPDATE")
        tasks.append(asyncio.create_task(_flag(own, at)))
        await _wait_for_blocked(demo_db_url, "match:noresponse",
                                blocker_pid=holder.info.backend_pid)
        if loss == "closing":
            own.begin_closing()
        else:
            own.lost.set()  # the watchdog's signal; public Event on Ownership
        holder.commit()
        holder.close()
        assert await tasks[0] == []
    finally:
        _release(holder)
        await _cleanup(tasks)
        await own.close()
    assert _state(demo_db_url, rid) == "SUBMITTED"
    assert _nr_items(demo_db_url) == []


# ── NR-T12 / NR-T13: opt-out ─────────────────────────────────────────────────


async def test_nr_t12_independent_opt_out_flags_with_call_only_detail(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    start = _start()
    try:
        r1 = await _send(own, s, 1, start)
        r2 = await _send(own, s, 2, start)
        now = datetime.now(UTC)
        await _reply(own, s, "STOP", reply_id="rid-1", at=now, correlation=r1)
        await _process(own, s, _ehr(start), now)
        assert _q(demo_db_url, "SELECT count(*) FROM opt_outs") == [(1,)]
        t = max(_sent_at(demo_db_url, r1), _sent_at(demo_db_url, r2))
        assert await _flag(own, t + H24) == [r2]
    finally:
        await own.close()
    [(rid, detail)] = _nr_items(demo_db_url)
    assert rid == r2
    assert "opted out of SMS — call only" in detail


async def test_nr_t12b_no_opt_out_detail_without_opt_out(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        assert await _flag(own, _sent_at(demo_db_url, rid) + H24) == [rid]
    finally:
        await own.close()
    [(_, detail)] = _nr_items(demo_db_url)
    assert "opted out" not in detail


async def test_nr_t13_stop_after_no_response_still_opts_out(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    start = _start()
    try:
        rid = await _send(own, s, 1, start)
        assert await _flag(own, _sent_at(demo_db_url, rid) + H24) == [rid]
        now = datetime.now(UTC)
        await _reply(own, s, "STOP", reply_id="rid-1", at=now, correlation=rid)
        await _process(own, s, _ehr(start), now)
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT count(*) FROM opt_outs") == [(1,)]
    assert _state(demo_db_url, rid) == "NO_RESPONSE"


# ── NR-T14: staff button, dispatcher, pause; no external call ────────────────


async def test_nr_t14a_service_check_flags_while_paused_without_gateway_calls(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.service import DbStaffService

    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    try:
        rid = await _send(own, s, 1, _start(), gw=gw)
        calls = len(gw.calls)
        at = _sent_at(demo_db_url, rid) + H24
        svc = DbStaffService(own, s, now=_clock(at), gateway=gw)
        await svc.set_paused(True)
        assert sorted(await svc.check_no_response()) == [rid]
        assert len(gw.calls) == calls
    finally:
        await own.close()


async def test_nr_t14b_dispatcher_runs_the_check(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.service import DbStaffService

    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        svc = DbStaffService(own, s, now=_clock(_sent_at(demo_db_url, rid) + H24))
        loop = asyncio.create_task(svc.dispatcher_loop(interval=0.05))
        for _ in range(100):
            if _state(demo_db_url, rid) == "NO_RESPONSE":
                break
            await asyncio.sleep(0.05)
        own.begin_closing()
        await asyncio.wait_for(loop, timeout=5)
    finally:
        await own.close()
    assert _state(demo_db_url, rid) == "NO_RESPONSE"
    assert len(_nr_items(demo_db_url)) == 1


async def test_nr_t14c_staff_button_runs_the_check(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.service import DbStaffService
    from haloflow_demo.web import CSRF_FIELD, build_staff_app

    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        svc = DbStaffService(own, s, now=_clock(_sent_at(demo_db_url, rid) + H24))
        app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8080") as c:
            page = (await c.get("/demo/staff")).text
            assert 'action="/demo/noresponse/check"' in page
            assert "Check for no reply now" in page
            m = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page)
            assert m
            r = await c.post("/demo/noresponse/check", data={CSRF_FIELD: m.group(1)})
            assert r.status_code in (200, 303)
    finally:
        await own.close()
    assert _state(demo_db_url, rid) == "NO_RESPONSE"


async def test_nr_t14d_staff_check_requires_csrf(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    """Pins the existing local staff boundary on the new route; no authentication claim."""
    from haloflow_demo.service import DbStaffService
    from haloflow_demo.web import CSRF_FIELD, build_staff_app

    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        svc = DbStaffService(own, s, now=_clock(_sent_at(demo_db_url, rid) + H24))
        app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8080") as c:
            page = (await c.get("/demo/staff")).text
            missing = await c.post("/demo/noresponse/check", data={})
            assert missing.status_code == 403
            invalid = await c.post("/demo/noresponse/check", data={CSRF_FIELD: "not-a-token"})
            assert invalid.status_code == 403
            assert _state(demo_db_url, rid) == "SUBMITTED"
            assert _nr_items(demo_db_url) == []
            m = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page)
            assert m
            ok = await c.post("/demo/noresponse/check", data={CSRF_FIELD: m.group(1)})
            assert ok.status_code in (200, 303)
    finally:
        await own.close()
    assert _state(demo_db_url, rid) == "NO_RESPONSE"
    assert len(_nr_items(demo_db_url)) == 1


# ── r2 additions (Codex implementation review r1): mutation coverage ─────────


async def test_nr_t16_uncorrelated_single_match_to_a1_does_not_answer_later_a2(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    """Kills N13: the single candidate must be this reminder, not any reminder."""
    s, own = await _own(_env(demo_env))
    start = _start()
    try:
        r1 = await _send(own, s, 1, start)
        await _reply(own, s, "maybe", reply_id="rid-1", at=datetime.now(UTC))  # snapshot {a-1}
        r2 = await _send(own, s, 2, start)
        snap = _q(demo_db_url, "SELECT reminder_id FROM inbound_candidates")
        assert snap == [(r1,)]
        t = max(_sent_at(demo_db_url, r1), _sent_at(demo_db_url, r2))
        assert await _flag(own, t + H24) == [r2]
    finally:
        await own.close()
    assert _state(demo_db_url, r1) == "SUBMITTED"


async def test_nr_t17_epoch_change_before_check_is_owner_lost_with_no_effect(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    """Kills N17: the check runs inside the ownership fence."""
    from haloflow_demo.errors import OwnerLost

    s, own = await _own(_env(demo_env))
    try:
        rid = await _send(own, s, 1, _start())
        at = _sent_at(demo_db_url, rid) + H24
        _q(demo_db_url, "UPDATE demo_settings SET owner_epoch = owner_epoch + 1")
        with pytest.raises(OwnerLost):
            await _flag(own, at)
    finally:
        await own.close()
    assert _state(demo_db_url, rid) == "SUBMITTED"
    assert _nr_items(demo_db_url) == []


async def test_nr_t18_reply_explicitly_correlated_to_another_reminder_does_not_answer(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    """Kills N06 through the staff service: a-2 is NOT_SENT (paused), so a reply
    simulated for a-2 has the snapshot {a-1} only; it must not answer a-1."""
    from haloflow_demo.service import DbStaffService

    s, own = await _own(_env(demo_env))
    start = _start()
    try:
        r1 = await _send(own, s, 1, start)
        svc = DbStaffService(own, s, now=lambda: datetime.now(UTC))
        await svc.set_paused(True)
        r2 = await send_one(own, s, _appt(2, start), _patient(2), StubGateway(TEST_NUMBER),
                            tenant_id=TENANT, now=_clock(NOW))
        assert r2.state == "NOT_SENT" and r2.reminder_id is not None
        res = await svc.simulate_reply(int(r2.reminder_id), "maybe")
        assert res is not None
        rows = _q(demo_db_url, "SELECT m.simulated_correlation, c.reminder_id"
                               " FROM inbound_messages m"
                               " JOIN inbound_candidates c ON c.inbound_id = m.id")
        assert rows == [(int(r2.reminder_id), r1)]
        assert await _flag(own, _sent_at(demo_db_url, r1) + H24) == [r1]
    finally:
        await own.close()
