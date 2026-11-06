"""L-1S package S-B — `DueWindowRunner.run_once` on PostgreSQL 17 (real M01 runtime, real
S-A `IntakeService`, S-B TEST registry/catalogue target 45).

Governing: requirements v1r1 R-SB-2/4a-d/5/6a-d; architecture v1 sections 1 and 3; pins
P3/P4; Codex interface review 08:46 ET items 2, 3, 5; red-package delta D4: a close-context
resolver failure stops the run with run code CONTEXT_UNAVAILABLE BEFORE the attempt is
marked (the key is not attempted, resume keeps the prior checkpoint, attempted == sum of
classified outcomes holds).

Invariant asserted on every RunResult: attempted == no_reply + opted_out + not_awaiting +
not_due + commit_unknown + sum(failed).

- SB-W01  at most once per run; attempts == distinct.
- SB-W02  cap: stops before fetching another page (exactly 2 selects for L=2, A=4, 6 due);
          code CAP; resume = 4th key; the next run continues after it.
- SB-W03  wrap after a phase-A selection failure checkpoint: the next run attempts
          [k5, k1, k2, k3, k4] exactly once each (phase B bounded by `until = start`),
          wrapped True, code COMPLETE, resume None.
- SB-W03b phase-B pages that do not align with the start key (start k3, page 2): run 2
          attempts [k4, k5, k1, k2, k3] — the until bound, not page alignment, ends phase B
          (disclosed implementation-stage addition).
- SB-W04  fairness: F = 4 permanently failing earliest windows + 3 healthy newer, L = 2,
          A = 3: every healthy window is closed no_reply within ceil(7/3) + 1 = 4 runs of the
          same runner; the failing ones stay awaiting.
- SB-W06  COMPLETE (including an empty phase B) sets resume None; SB-W07 a new runner
          starts from the earliest key.
- SB-W08  two independent runners overlapping on one tenant: every due window ends
          no_reply with exactly one no_reply event and one no_reply item.
- SB-X01  selected, then the patient replies before the close -> not_awaiting, no item.
- SB-X02  selected, then STOP before the close -> not_awaiting, no no_reply item.
- SB-R01  lost acknowledgement after COMMIT -> commit_unknown; the window is no_reply;
          the next run does not attempt it; one event, one item.
- SB-R02  rollback (close statement fails) -> failed[INTERNAL]; window still awaiting; the
          next run (fault cleared) closes it.
- SB-E01  selection failure in phase A -> SELECTION_FAILED (never empty/COMPLETE), counts so
          far returned, resume = last attempted key; in phase B -> resume = last attempted
          GLOBAL key, never the temporary `after = None`.
- SB-E02  per-window failures isolated: the other windows of the run close.
- SB-E03  a close waiting on the contact lock past the gateway lock timeout -> one failed
          count with a code from the closed vocabulary; window still awaiting.
- SB-E04  failure codes are from the closed vocabulary only; no exception text in the result
          or the run log line.
- SB-E05  close-context resolver failure -> run code CONTEXT_UNAVAILABLE, key not attempted,
          resume unchanged; the next run attempts it first.
- SB-L01  exactly one run log line per run, fields in the pinned order, no PHI (no phone,
          no patient id).
- SB-A01  the runner's contexts: selection = exactly {DUE_KEY}; close = exactly S-A's set.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any

import psycopg
import pytest
import sa_support as sa
import sb_runtime as sr
import sb_support as sb

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]

FAILED_VOCABULARY = frozenset({*sa.CODES, "RESULT_INVALID", "DATABASE_ERROR", "INTERNAL"})
LINE = re.compile(
    r"^due_windows run tenant=(?P<tenant>\S+) code=(?P<code>[A-Z_]+) attempted=\d+ no_reply=\d+"
    r" opted_out=\d+ not_awaiting=\d+ not_due=\d+ commit_unknown=\d+ cancelled=\d+"
    r" failed=(?P<failed>(?:[A-Z_]+:\d+(?:,[A-Z_]+:\d+)*)?) wrapped=(?:True|False)$"
)


def _invariant(result: Any) -> None:
    assert result.attempted == sr.outcome_sum(result), result
    assert set(result.failed) <= FAILED_VOCABULARY, result.failed


def _windows(install: sb.SBInstall, n: int) -> list[str]:
    sb.close_all_awaiting(install)
    reminders = [sb.due_window(install)[2] for _ in range(n)]
    return sr.ordered_due(install, reminders)


def _go(coro: Any) -> Any:
    return asyncio.run(coro)


# --- SB-W01 / SB-A01 ----------------------------------------------------------------------


def test_sb_w01_once_per_run_and_contexts(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 5)

    async def body() -> tuple[Any, sr.Gate]:
        async with sr.runtime(sb_install) as rt:
            r, gate = sr.runner(sb_install, rt, page=2, cap=50)
            return await sr.run(r, sb_install), gate

    result, gate = _go(body())
    _invariant(result)
    assert result.code == "COMPLETE"
    assert gate.closes == keys and len(set(gate.closes)) == len(gate.closes)
    assert result.attempted == 5 == result.no_reply
    caps = {frozenset(c.capabilities) for c in gate.contexts}
    assert caps == {sb.SELECT_CAPS, sb.CLOSE_CAPS}


# --- SB-W02 -------------------------------------------------------------------------------


def test_sb_w02_cap_before_fetch(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 6)

    async def body() -> tuple[Any, Any, sr.Gate, Any]:
        async with sr.runtime(sb_install) as rt:
            r, gate = sr.runner(sb_install, rt, page=2, cap=4)
            first = await sr.run(r, sb_install)
            selects = gate.selects
            resume = r.resume_key(sb_install.tenant_id)
            second = await sr.run(r, sb_install)
            return first, (selects, resume), gate, second

    first, (selects, resume), gate, second = _go(body())
    _invariant(first)
    _invariant(second)
    assert first.code == "CAP" and first.attempted == 4
    assert selects == 2
    assert resume is not None and str(resume.reminder_id) == keys[3]
    assert gate.closes[:4] == keys[:4]
    assert gate.closes[4:6] == keys[4:6]


# --- SB-W03 / SB-E01 (phase A) / SB-W06 ------------------------------------------------------


def test_sb_w03_wrap_bounded_by_start(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 5)

    async def body() -> tuple[Any, Any, Any, list[str], Any]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_close = set(keys)          # permanently failing: stay awaiting
            gate.fail_select_at = 3              # run 1: pages k1,k2 / k3,k4 / FAIL
            r, _ = sr.runner(sb_install, rt, page=2, cap=50, gate=gate)
            first = await sr.run(r, sb_install)
            resume1 = r.resume_key(sb_install.tenant_id)
            mark = len(gate.closes)
            second = await sr.run(r, sb_install)
            return first, resume1, second, gate.closes[mark:], r.resume_key(sb_install.tenant_id)

    first, resume1, second, run2, resume2 = _go(body())
    _invariant(first)
    _invariant(second)
    assert first.code == "SELECTION_FAILED" and first.attempted == 4
    assert str(resume1.reminder_id) == keys[3]
    assert run2 == [keys[4], keys[0], keys[1], keys[2], keys[3]]
    assert second.code == "COMPLETE" and second.wrapped is True and second.attempted == 5
    assert resume2 is None


def test_sb_w03b_wrap_page_crossing_start(sb_install: sb.SBInstall) -> None:
    """Phase B pages do not align with the start key (start = k3, page 2): the until
    bound must stop phase B at k3, so k4 is never attempted twice (disclosed addition,
    implementation stage: SB-W03's aligned pages cannot distinguish an unbounded B)."""

    sb_install.require()
    keys = _windows(sb_install, 5)

    async def body() -> tuple[Any, Any, list[str]]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_close = set(keys)          # permanently failing: stay awaiting
            resolver = sr.FlakyResolver(rt.resolver)
            r, _ = sr.runner(sb_install, rt, page=2, cap=50, gate=gate, resolver=resolver)
            original = resolver.resolve
            count = {"close": 0}

            async def fourth_close_fails(**kwargs: Any) -> Any:
                if frozenset(kwargs["capabilities"]) == sb.CLOSE_CAPS:
                    count["close"] += 1
                    if count["close"] == 4:
                        raise sr.InjectedFault("resolver unavailable (test)")
                return await original(**kwargs)

            resolver.resolve = fourth_close_fails  # type: ignore[method-assign]
            first = await sr.run(r, sb_install)  # k1, k2, k3; k4's context fails
            resolver.resolve = original  # type: ignore[method-assign]
            mark = len(gate.closes)
            second = await sr.run(r, sb_install)
            return first, second, gate.closes[mark:]

    first, second, run2 = _go(body())
    _invariant(first)
    _invariant(second)
    assert first.code == "CONTEXT_UNAVAILABLE" and first.attempted == 3
    assert run2 == [keys[3], keys[4], keys[0], keys[1], keys[2]]
    assert second.code == "COMPLETE" and second.wrapped is True and second.attempted == 5


def test_sb_w06_empty_phase_b_completes(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 3)

    async def body() -> tuple[Any, Any, Any]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_select_at = 2              # run 1: page k1,k2 then FAIL
            r, _ = sr.runner(sb_install, rt, page=2, cap=50, gate=gate)
            first = await sr.run(r, sb_install)
            second = await sr.run(r, sb_install)  # A: k3; B (until k2): nothing awaiting
            return first, second, r.resume_key(sb_install.tenant_id)

    first, second, resume = _go(body())
    _invariant(second)
    assert first.no_reply == 2
    assert second.code == "COMPLETE" and second.attempted == 1 and second.no_reply == 1
    assert resume is None
    assert sb.states(sb_install, keys) == dict.fromkeys(keys, "no_reply")


def test_sb_w07_new_runner_starts_earliest(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 4)

    async def body() -> list[str]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_close = set(keys)
            r1, _ = sr.runner(sb_install, rt, page=2, cap=2, gate=gate)
            await sr.run(r1, sb_install)          # resume k2 in r1 only
            mark = len(gate.closes)
            r2, _ = sr.runner(sb_install, rt, page=2, cap=2, gate=gate)
            await sr.run(r2, sb_install)
            return gate.closes[mark:]

    assert _go(body()) == keys[:2]


# --- SB-E01 (phase B) ------------------------------------------------------------------------


def test_sb_e01_phase_b_failure_keeps_global_key(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 5)

    async def body() -> tuple[Any, Any]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_close = set(keys)
            gate.fail_select_at = 3              # run 1 ends SELECTION_FAILED, resume k4
            r, _ = sr.runner(sb_install, rt, page=2, cap=50, gate=gate)
            await sr.run(r, sb_install)
            # run 2: select #4 = A (k5, short); #5 = B first page (k1, k2); #6 = FAIL
            gate.fail_select_at = 6
            second = await sr.run(r, sb_install)
            return second, r.resume_key(sb_install.tenant_id)

    second, resume = _go(body())
    _invariant(second)
    assert second.code == "SELECTION_FAILED" and second.wrapped is True
    assert second.attempted == 3
    assert resume is not None and str(resume.reminder_id) == keys[1]


# --- SB-W04 --------------------------------------------------------------------------------------


def test_sb_w04_fair_progress(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 7)
    failing, healthy = keys[:4], keys[4:]
    bound = math.ceil(7 / 3) + 1

    async def body() -> int:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_close = set(failing)
            r, _ = sr.runner(sb_install, rt, page=2, cap=3, gate=gate)
            for n in range(1, bound + 1):
                _invariant(await sr.run(r, sb_install))
                if set(sb.states(sb_install, healthy).values()) == {"no_reply"}:
                    return n
            return -1

    runs = _go(body())
    assert 1 <= runs <= bound, runs
    assert set(sb.states(sb_install, failing).values()) == {"awaiting"}


# --- SB-W08 -----------------------------------------------------------------------------------


def test_sb_w08_independent_runners_overlap(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    made = [sb.due_window(sb_install) for _ in range(6)]

    async def body() -> list[Any]:
        async with sr.runtime(sb_install, max_size=4) as rt:
            r1, _ = sr.runner(sb_install, rt, page=2, cap=50)
            r2, _ = sr.runner(sb_install, rt, page=3, cap=50)
            return list(await asyncio.gather(sr.run(r1, sb_install), sr.run(r2, sb_install)))

    results = _go(body())
    for result in results:
        _invariant(result)
    reminders = [m[2] for m in made]
    assert sb.states(sb_install, reminders) == dict.fromkeys(reminders, "no_reply")
    for patient, _, reminder in made:
        assert sb.no_reply_events(sb_install, reminder) == 1
        assert sb.no_reply_items(sb_install, patient) == 1


# --- SB-X01 / SB-X02 ------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ("confirm", "stop"))
def test_sb_x01_x02_selected_then_reply(sb_install: sb.SBInstall, reply: str) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    patient, phone, reminder = sb.due_window(sb_install, hours_past=24.5)
    # The window is due when selected; the hook (inside the close transaction, before
    # the close statement) moves its deadline forward and registers a TIMELY reply from
    # another connection, so t041 then finds it no longer awaiting (R-SB-2).

    def deliver() -> None:
        sa.shift_window(sb_install, reminder, 2)            # deadline now in the future
        sa.inbound(sb_install, sa.new_id(), phone, reply)  # timely: window leaves awaiting

    async def body() -> Any:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.before_close[reminder] = deliver
            r, _ = sr.runner(sb_install, rt, page=10, cap=10, gate=gate)
            return await sr.run(r, sb_install)

    result = _go(body())
    _invariant(result)
    assert (result.attempted, result.not_awaiting, result.no_reply) == (1, 1, 0)
    assert sb.no_reply_items(sb_install, patient) == 0
    assert sb.no_reply_events(sb_install, reminder) == 0


# --- SB-R01 / SB-R02 / SB-E02 -------------------------


def test_sb_r01_lost_ack_after_commit(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    patient, _, reminder = sb.due_window(sb_install)

    async def body() -> tuple[Any, Any, list[str]]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.lose_ack = {reminder}
            r, _ = sr.runner(sb_install, rt, page=10, cap=10, gate=gate)
            first = await sr.run(r, sb_install)
            gate.lose_ack = set()
            mark = len(gate.closes)
            second = await sr.run(r, sb_install)
            return first, second, gate.closes[mark:]

    first, second, later = _go(body())
    _invariant(first)
    _invariant(second)
    assert (first.attempted, first.commit_unknown) == (1, 1)
    assert sb.states(sb_install, [reminder]) == {reminder: "no_reply"}
    assert later == [] and second.attempted == 0
    assert sb.no_reply_events(sb_install, reminder) == 1
    assert sb.no_reply_items(sb_install, patient) == 1


def test_sb_r02_rollback_rediscovered_and_e02_isolated(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 3)

    async def body() -> tuple[Any, Any]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_close = {keys[1]}
            r, _ = sr.runner(sb_install, rt, page=10, cap=10, gate=gate)
            first = await sr.run(r, sb_install)
            gate.fail_close = set()
            second = await sr.run(r, sb_install)
            return first, second

    first, second = _go(body())
    _invariant(first)
    _invariant(second)
    assert (first.attempted, first.no_reply, dict(first.failed)) == (3, 2, {"INTERNAL": 1})
    assert (second.attempted, second.no_reply) == (1, 1)
    assert sb.states(sb_install, keys) == dict.fromkeys(keys, "no_reply")


# --- SB-E03 -------------------------------------------------------------------------------------


def test_sb_e03_lock_timeout_is_a_window_failure(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    _, phone, reminder = sb.due_window(sb_install)
    sa.admin(sb_install, sa.q(sb_install, "INSERT INTO {s}.m05_contact_state (phone, opted_out)"
                                          " VALUES (%s, false) ON CONFLICT DO NOTHING"), (phone,))

    async def body() -> Any:
        async with sr.runtime(sb_install) as rt:
            r, _ = sr.runner(sb_install, rt, page=10, cap=10)
            return await sr.run(r, sb_install)

    with psycopg.connect(sb_install.ids.admin) as holder:
        holder.execute(sa.q(sb_install, "SELECT 1 FROM {s}.m05_contact_state"
                                        " WHERE phone = %s FOR UPDATE"), (phone,))
        result = _go(body())
        holder.rollback()
    _invariant(result)
    assert result.attempted == 1 and sum(result.failed.values()) == 1
    assert sb.states(sb_install, [reminder]) == {reminder: "awaiting"}


# --- SB-E04 / SB-L01 -------------------------


def test_sb_e04_l01_codes_and_log_line(sb_install: sb.SBInstall,
                                       caplog: pytest.LogCaptureFixture) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    made = [sb.due_window(sb_install) for _ in range(2)]

    async def body() -> Any:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.fail_close = {made[0][2]}
            r, _ = sr.runner(sb_install, rt, page=10, cap=10, gate=gate)
            return await sr.run(r, sb_install)

    with caplog.at_level(logging.INFO, logger=sr.LOGGER):
        result = _go(body())
    _invariant(result)
    assert dict(result.failed) == {"INTERNAL": 1}
    lines = sr.run_lines(caplog.records)
    assert len(lines) == 1, lines
    match = LINE.fullmatch(lines[0])
    assert match is not None, lines[0]
    assert match["tenant"] == sb_install.tenant_id and match["failed"] == "INTERNAL:1"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "test)" not in text and "close failure" not in text
    for patient, phone, _ in made:
        assert patient not in text and phone not in text


# --- SB-E05 ---------------------------------------------------------------------------------------


def test_sb_e05_close_context_unavailable(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    keys = _windows(sb_install, 3)

    async def body() -> tuple[Any, Any, Any, list[str]]:
        async with sr.runtime(sb_install) as rt:
            resolver = sr.FlakyResolver(rt.resolver)
            r, gate = sr.runner(sb_install, rt, page=10, cap=10, resolver=resolver)
            resolver.fail_close_resolve = 0
            # first close context OK, second fails: k1 closed, k2 not attempted
            original = resolver.resolve
            count = {"close": 0}

            async def second_close_fails(**kwargs: Any) -> Any:
                if frozenset(kwargs["capabilities"]) == sb.CLOSE_CAPS:
                    count["close"] += 1
                    if count["close"] == 2:
                        raise sr.InjectedFault("resolver unavailable (test)")
                return await original(**kwargs)

            resolver.resolve = second_close_fails  # type: ignore[method-assign]
            first = await sr.run(r, sb_install)
            resume = r.resume_key(sb_install.tenant_id)
            mark = len(gate.closes)
            resolver.resolve = original  # type: ignore[method-assign]
            await sr.run(r, sb_install)
            return first, resume, gate.closes[:mark], gate.closes[mark:]

    first, resume, run1, run2 = _go(body())
    _invariant(first)
    assert first.code == "CONTEXT_UNAVAILABLE"
    assert (first.attempted, first.no_reply) == (1, 1)
    assert run1 == [keys[0]]
    assert resume is not None and str(resume.reminder_id) == keys[0]
    assert run2[:1] == [keys[1]]
