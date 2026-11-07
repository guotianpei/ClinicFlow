"""L-1S package S-E — DueWindowScheduler unit rows (no database, fake clock).

Governing: S-E package v1 + v1r1, Codex ALIGN for tests 20261107T125936Z. Pinned API
(`haloflow.m05.due_scheduler`):
- `DueWindowScheduler(runner, tenants, *, interval_s=300, stop_timeout_s=10,
  clock=time.monotonic, sleep=asyncio.sleep)`; `.state` -> `SchedulerState`
  (NEW, RUNNING, STOPPING, STOPPED, STOP_FAILED); `start()`; `async tick()` ->
  `TickResult`; `async stop()`.
- `SchedulerRefused(code)` codes: RUNNER_INVALID, TENANTS_INVALID, INTERVAL_INVALID,
  TIMEOUT_INVALID, NOT_STARTABLE, TICK_IN_PROGRESS, LOOP_OWNS_TICKS, SCHEDULER_STOPPED,
  STOPPED_DURING_TICK. `SchedulerStopFailed` code STOP_TIMEOUT.
- `TickResult(tick, duration_ms, codes, attempted, no_reply, opted_out, cancelled)`.
- One summary line per tick on `haloflow.m05.due_scheduler`, emitted after the skipped
  count is known (loop) or with skipped=0 (manual tick), also on cancellation:
  `due_scheduler tick=<n> skipped=<n> duration_ms=<n> budget_ms=<n> over_budget=<bool>
  attempted=<n> no_reply=<n> opted_out=<n> cancelled=<bool> codes=<K:V,...>`.
- Fixed rate with strict lateness: a tick ending exactly at the next start skips none.

- SE-U01 inputs        - SE-U02 fixed rate, tenant order, fresh execution ids
- SE-U03 overrun skip  - SE-U03b exact boundary (strict lateness)
- SE-U04 isolation and closed code allowlist
- SE-U05 lifecycle matrix  - SE-U06 TICK_IN_PROGRESS (incl. start during a manual tick)
- SE-U07 stop timeout / caller-cancelled stop / concurrent stop callers
- SE-U08 latency envelope on the fake clock (+ NC at I=450, D=100)
- SE-U09 summary line (incl. one summary on cancellation)
- SE-S01 static: not composed
"""

from __future__ import annotations

import ast
import asyncio
import logging
import math
import pathlib
import uuid
from typing import Any

import pytest
import se_support as se

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"


def _mod() -> Any:
    import haloflow.m05.due_scheduler as module

    return module


async def _until(predicate: Any, what: str, rounds: int = 2000) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"not reached: {what}")


async def _drain(s: Any, *futures: Any) -> None:
    """Bounded teardown: stop the scheduler and settle any outer future, never hang.
    Uses asyncio.wait with a timeout (it never waits for a cancelled awaitable to
    finish), so a hanging stop() or future leaves this function after the bound."""

    stop = asyncio.ensure_future(s.stop())
    pending = {stop}
    for future in futures:
        if not future.done():
            future.cancel()
        pending.add(future)
    done, still = await asyncio.wait(pending, timeout=5)
    for future in still:
        future.cancel()
    for future in done:
        if not future.cancelled():
            future.exception()  # retrieved: teardown never raises


# --- SE-U01 -----------------------------------------------------------------------------------

BAD_INPUTS: dict[str, tuple[dict[str, Any], str]] = {
    "runner-object": ({"runner": object()}, "RUNNER_INVALID"),
    "tenants-list": ({"tenants": [se.TENANT_A]}, "TENANTS_INVALID"),
    "tenants-empty": ({"tenants": ()}, "TENANTS_INVALID"),
    "tenants-dup": ({"tenants": (se.TENANT_A, se.TENANT_A)}, "TENANTS_INVALID"),
    "tenants-pattern": ({"tenants": ("Bad_Tenant",)}, "TENANTS_INVALID"),
    "tenants-nonstr": ({"tenants": (1,)}, "TENANTS_INVALID"),
    "tenants-1001": ({"tenants": tuple(f"t-{i:04d}" for i in range(1001))},
                     "TENANTS_INVALID"),
    "interval-59": ({"interval_s": 59}, "INTERVAL_INVALID"),
    "interval-451": ({"interval_s": 451}, "INTERVAL_INVALID"),
    "interval-900": ({"interval_s": 900}, "INTERVAL_INVALID"),
    "interval-float": ({"interval_s": 300.0}, "INTERVAL_INVALID"),
    "interval-bool": ({"interval_s": True}, "INTERVAL_INVALID"),
    "timeout-0": ({"stop_timeout_s": 0}, "TIMEOUT_INVALID"),
    "timeout-61": ({"stop_timeout_s": 61}, "TIMEOUT_INVALID"),
    "timeout-bool": ({"stop_timeout_s": True}, "TIMEOUT_INVALID"),
    "timeout-nan": ({"stop_timeout_s": math.nan}, "TIMEOUT_INVALID"),
    "timeout-inf": ({"stop_timeout_s": math.inf}, "TIMEOUT_INVALID"),
    "timeout-str": ({"stop_timeout_s": "5"}, "TIMEOUT_INVALID"),
}


@pytest.mark.parametrize("case", sorted(BAD_INPUTS))
def test_se_u01_inputs_refused(case: str) -> None:
    module = _mod()
    clock = se.FakeClock()
    overrides, code = BAD_INPUTS[case]
    tenants = overrides.get("tenants", (se.TENANT_A,))
    runner = overrides.get("runner") or se.fake_runner(se.timed(clock, {}))
    kwargs = {k: v for k, v in overrides.items() if k not in ("runner", "tenants")}
    with pytest.raises(module.SchedulerRefused) as caught:
        module.DueWindowScheduler(runner, tenants, clock=clock, sleep=clock.sleep, **kwargs)
    assert caught.value.code == code


def test_se_u01_valid_bounds_accepted() -> None:
    module = _mod()
    clock = se.FakeClock()
    runner = se.fake_runner(se.timed(clock, {}))
    for interval in (60, 300, 450):
        for timeout in (0.5, 60):
            s = module.DueWindowScheduler(runner, (se.TENANT_A,), interval_s=interval,
                                          stop_timeout_s=timeout, clock=clock,
                                          sleep=clock.sleep)
            assert s.state is module.SchedulerState.NEW
    defaults = module.DueWindowScheduler(runner, (se.TENANT_A,))
    assert (module.DEFAULT_INTERVAL_S, module.INTERVAL_MIN_S, module.INTERVAL_MAX_S) == (
        300, 60, 450)
    assert defaults.state is module.SchedulerState.NEW


# --- SE-U02 -----------------------------------------------------------------------------------


async def test_se_u02_fixed_rate_order_and_ids() -> None:
    clock = se.FakeClock(1000.0)
    log: list[tuple[str, float, float]] = []
    tenants = (se.TENANT_A, se.TENANT_B, se.TENANT_C)
    runner = se.fake_runner(se.timed(clock, {se.TENANT_A: 40, se.TENANT_B: 20,
                                             se.TENANT_C: 10}, log=log), tenants)
    s = se.scheduler(runner, tenants, clock)
    s.start()
    await _until(lambda: len(runner.calls) >= 9, "three ticks")
    await s.stop()
    starts = [(t, start) for t, start, _ in log[:9]]
    assert starts == [
        (se.TENANT_A, 1000.0), (se.TENANT_B, 1040.0), (se.TENANT_C, 1060.0),
        (se.TENANT_A, 1300.0), (se.TENANT_B, 1340.0), (se.TENANT_C, 1360.0),
        (se.TENANT_A, 1600.0), (se.TENANT_B, 1640.0), (se.TENANT_C, 1660.0),
    ]
    ids = [execution for _, execution in runner.calls]
    assert all(type(e) is uuid.UUID and e.version == 4 for e in ids)
    assert len(set(ids)) == len(ids)


# --- SE-U03 / SE-U03b -------------------------------------------------------------------------


async def _tick_starts(durations: list[float], ticks: int,
                       caplog: pytest.LogCaptureFixture) -> tuple[list[float], list[str]]:
    clock = se.FakeClock(0.0)
    starts: list[float] = []
    queue = list(durations)

    async def behaviour(_tenant: str, _execution: uuid.UUID) -> Any:
        starts.append(clock.now)
        clock.now += queue.pop(0) if queue else 0.0
        await asyncio.sleep(0)
        return se.run_result()

    runner = se.fake_runner(behaviour)
    s = se.scheduler(runner, (se.TENANT_A,), clock)
    with caplog.at_level(logging.INFO, logger=se.LOGGER):
        s.start()
        await _until(lambda: len(starts) >= ticks, f"{ticks} ticks")
        await _until(lambda: len(se.summaries(caplog.records)) >= ticks - 1, "summaries")
        await s.stop()
    return starts[:ticks], se.summaries(caplog.records)


async def test_se_u03_overrun_skips_missed_ticks(caplog: pytest.LogCaptureFixture) -> None:
    starts, lines = await _tick_starts([700.0, 0.0], 2, caplog)
    assert starts == [0.0, 900.0]  # 300 and 600 skipped, no burst
    assert "skipped=2 " in lines[0], lines[0]


async def test_se_u03b_exact_boundary_strict_lateness(caplog: pytest.LogCaptureFixture) -> None:
    starts, lines = await _tick_starts([300.0, 0.0], 2, caplog)
    assert starts == [0.0, 300.0]
    assert "skipped=0 " in lines[0], lines[0]
    caplog.clear()
    starts, lines = await _tick_starts([300.001, 0.0], 2, caplog)
    assert starts == [0.0, 600.0]
    assert "skipped=1 " in lines[0], lines[0]


# --- SE-U04 -----------------------------------------------------------------------------------


async def test_se_u04_isolation_and_closed_allowlist() -> None:
    from haloflow.m05.due_windows import SBRefused

    clock = se.FakeClock()
    tenants = ("t-a", "t-b", "t-c", "t-d", "t-e", "t-f")
    results = {
        "t-a": se.run_result("COMPLETE", attempted=2, no_reply=1, opted_out=1),
        "t-b": RuntimeError("secret detail must not leak"),
        "t-c": se.run_result("SELECTION_FAILED"),
        "t-d": SBRefused("TENANT_INVALID"),
        "t-e": se.run_result("SOMETHING_NEW"),
        "t-f": SBRefused("NOT_A_KNOWN_CODE"),
    }
    runner = se.fake_runner(se.timed(clock, {}, results), tenants)
    s = se.scheduler(runner, tenants, clock)
    result = await s.tick()
    assert [t for t, _ in runner.calls] == list(tenants)
    assert dict(result.codes) == {"COMPLETE": 1, "INTERNAL": 3, "SELECTION_FAILED": 1,
                                  "REFUSED_TENANT_INVALID": 1}
    assert (result.attempted, result.no_reply, result.opted_out, result.cancelled) == (
        2, 1, 1, False)
    assert s.state is _mod().SchedulerState.NEW


# --- SE-U05 / SE-U06 --------------------------------------------------------------------------


async def test_se_u05_lifecycle_matrix() -> None:
    module = _mod()
    states = module.SchedulerState
    clock = se.FakeClock()
    runner = se.fake_runner(se.timed(clock, {}))
    # stop before start: STOPPED, no task, idempotent; start and tick refused afterwards.
    s = se.scheduler(runner, (se.TENANT_A,), clock)
    await s.stop()
    await s.stop()
    assert s.state is states.STOPPED
    with pytest.raises(module.SchedulerRefused) as refused:
        s.start()
    assert refused.value.code == "NOT_STARTABLE"
    with pytest.raises(module.SchedulerRefused) as refused:
        await s.tick()
    assert refused.value.code == "SCHEDULER_STOPPED"
    assert runner.calls == []
    # manual tick, then start allowed (never started); then loop owns ticks.
    s = se.scheduler(runner, (se.TENANT_A,), clock)
    await s.tick()
    s.start()
    assert s.state is states.RUNNING
    with pytest.raises(module.SchedulerRefused) as refused:
        s.start()
    assert refused.value.code == "NOT_STARTABLE"
    with pytest.raises(module.SchedulerRefused) as refused:
        await s.tick()
    assert refused.value.code == "LOOP_OWNS_TICKS"
    await _until(lambda: len(runner.calls) >= 3, "loop ticks")
    await s.stop()
    assert s.state is states.STOPPED
    calls = len(runner.calls)
    await se.settle(200)
    assert len(runner.calls) == calls  # no run_once after stop returned
    await s.stop()  # idempotent
    with pytest.raises(module.SchedulerRefused) as refused:
        s.start()
    assert refused.value.code == "NOT_STARTABLE"


async def test_se_u06_tick_in_progress_and_stop_during_manual_tick() -> None:
    module = _mod()
    clock = se.FakeClock()
    parked, release = asyncio.Event(), asyncio.Event()

    async def behaviour(_tenant: str, _execution: uuid.UUID) -> Any:
        parked.set()
        await release.wait()
        return se.run_result()

    runner = se.fake_runner(behaviour)
    s = se.scheduler(runner, (se.TENANT_A,), clock)
    manual = asyncio.ensure_future(s.tick())
    try:
        await asyncio.wait_for(parked.wait(), 5)
        assert s.state is module.SchedulerState.NEW
        with pytest.raises(module.SchedulerRefused) as refused:
            await s.tick()
        assert refused.value.code == "TICK_IN_PROGRESS"
        with pytest.raises(module.SchedulerRefused) as refused:
            s.start()  # NEW while a manual tick is owned: no loop, ownership unchanged
        assert refused.value.code == "TICK_IN_PROGRESS"
        assert s.state is module.SchedulerState.NEW
        await asyncio.wait_for(s.stop(), 5)
        with pytest.raises(module.SchedulerRefused) as refused:
            await asyncio.wait_for(manual, 5)
        assert refused.value.code == "STOPPED_DURING_TICK"
        assert s.state is module.SchedulerState.STOPPED
        assert len(runner.calls) == 1
    finally:
        release.set()
        await _drain(s, manual)


async def test_se_u06b_caller_cancels_manual_tick() -> None:
    module = _mod()
    clock = se.FakeClock()
    parked, cleaned = asyncio.Event(), asyncio.Event()

    async def behaviour(_tenant: str, _execution: uuid.UUID) -> Any:
        if parked.is_set():
            return se.run_result()  # the second manual tick completes
        parked.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return se.run_result()

    runner = se.fake_runner(behaviour)
    s = se.scheduler(runner, (se.TENANT_A,), clock)
    manual = asyncio.ensure_future(s.tick())
    try:
        await asyncio.wait_for(parked.wait(), 5)
        manual.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(manual, 5)
        assert cleaned.is_set()  # the owned tick finished its cleanup first
        await asyncio.wait_for(s.tick(), 5)  # ownership released: a new tick is accepted
        assert len(runner.calls) == 2
        assert s.state is module.SchedulerState.NEW
    finally:
        await _drain(s, manual)


# --- SE-U07 -----------------------------------------------------------------------------------


async def test_se_u07_stop_timeout_then_retry() -> None:
    module = _mod()
    clock = se.FakeClock()
    parked, release = asyncio.Event(), asyncio.Event()

    async def resistant(_tenant: str, _execution: uuid.UUID) -> Any:
        parked.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # cancellation-resistant (test double)
        return se.run_result()

    runner = se.fake_runner(resistant)
    s = se.scheduler(runner, (se.TENANT_A,), clock, stop_timeout_s=0.2)
    s.start()
    created: list[Any] = []
    try:
        await asyncio.wait_for(parked.wait(), 5)
        # Watchdog: stop() runs as its own task, observed with a bounded wait, so a
        # stop that hangs fails this row instead of hanging it.
        first = asyncio.ensure_future(s.stop())
        created.append(first)
        done, _ = await asyncio.wait({first}, timeout=5)
        assert first in done, "stop() did not return within the watchdog bound"
        error = first.exception()
        assert isinstance(error, module.SchedulerStopFailed), repr(error)
        assert error.code == "STOP_TIMEOUT"
        assert s.state is module.SchedulerState.STOP_FAILED
        with pytest.raises(module.SchedulerRefused):
            s.start()
        release.set()
        second = asyncio.ensure_future(s.stop())
        created.append(second)
        done, _ = await asyncio.wait({second}, timeout=5)
        assert second in done and second.exception() is None
        assert s.state is module.SchedulerState.STOPPED
        assert len(runner.calls) == 1
    finally:  # release FIRST, then bounded drain: never leave the resistant double parked
        release.set()
        await _drain(s, *created)


async def test_se_u07b_caller_cancelled_stop_and_concurrent_stops() -> None:
    module = _mod()
    clock = se.FakeClock()
    parked, release = asyncio.Event(), asyncio.Event()

    entered = asyncio.Event()
    completed: list[bool] = []

    async def slow_cleanup(_tenant: str, _execution: uuid.UUID) -> Any:
        parked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            entered.set()
            await release.wait()  # cleanup in progress until released; a second
            completed.append(True)  # cancel would abort it before this line
            raise
        return se.run_result()

    runner = se.fake_runner(slow_cleanup)
    s = se.scheduler(runner, (se.TENANT_A,), clock, stop_timeout_s=5)
    s.start()
    created: list[Any] = []
    try:
        await asyncio.wait_for(parked.wait(), 5)
        first = asyncio.ensure_future(s.stop())
        created.append(first)
        await asyncio.wait_for(entered.wait(), 5)  # the owned work is in its cleanup
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 5)
        assert s.state is module.SchedulerState.STOPPING
        second = asyncio.ensure_future(s.stop())
        third = asyncio.ensure_future(s.stop())
        created += [second, third]
        await se.settle()
        # Before release: both stops wait on the SAME owned work, still in cleanup.
        assert not second.done() and not third.done()
        assert completed == [] and s.state is module.SchedulerState.STOPPING
        release.set()
        await asyncio.wait_for(asyncio.gather(second, third), 5)
        assert completed == [True]  # cleanup ran to completion (not re-cancelled)
        assert s.state is module.SchedulerState.STOPPED
        assert len(runner.calls) == 1  # no new work from repeated stops
    finally:
        release.set()
        await _drain(s, *created)


# --- SE-U08 latency envelope (fake clock) --------------------------------------------------


async def _closure_delay(interval: int, durations: dict[str, float]) -> float:
    """Worst phase: tenant X's window becomes due just after X's run in tick 0 started;
    X's run in tick 1 fails (one failed tick); the delay is measured to the COMPLETION
    of the run that closes it (item visible), with earlier tenants' time included."""

    clock = se.FakeClock(0.0)
    tenants = ("t-a", "t-x")
    state: dict[str, Any] = {"due": None, "closed_at": None, "x_runs": 0}

    async def behaviour(tenant: str, _execution: uuid.UUID) -> Any:
        start = clock.now
        clock.now += durations[tenant]
        await asyncio.sleep(0)
        if tenant != "t-x":
            return se.run_result()
        state["x_runs"] += 1
        if state["x_runs"] == 1:
            state["due"] = start + 0.001  # due just after X's selection
            return se.run_result()
        if state["x_runs"] == 2:
            return se.run_result("SELECTION_FAILED")  # the one failed tick
        if state["closed_at"] is None:
            state["closed_at"] = clock.now  # completion: item visible
        return se.run_result(attempted=1, no_reply=1)

    runner = se.fake_runner(behaviour, tenants)
    s = se.scheduler(runner, tenants, clock, interval_s=interval)
    s.start()
    try:
        await _until(lambda: state["closed_at"] is not None, "closure")
    finally:
        await _drain(s)
    return float(state["closed_at"] - state["due"])


def _within_target(delay: float) -> None:
    """The SE-U08 oracle: item visible within 15 minutes of the deadline."""

    assert delay <= se.TARGET_S, delay


async def test_se_u08_default_envelope_meets_target_with_one_failed_tick() -> None:
    delay = await _closure_delay(300, {"t-a": 290.0, "t-x": 10.0})  # I=300, D=300
    _within_target(delay)
    assert round(delay, 3) == 609.999


async def test_se_u08_nc_i450_d100_breaches_target() -> None:
    delay = await _closure_delay(450, {"t-a": 90.0, "t-x": 10.0})  # I=450, D=100
    assert round(delay, 3) == 909.999
    with pytest.raises(AssertionError):
        _within_target(delay)


# --- SE-U09 -----------------------------------------------------------------------------------


async def test_se_u09_summary_line(caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.m05.due_windows import SBRefused

    clock = se.FakeClock()
    tenants = ("t-a", "t-b", "t-c")
    results = {"t-a": se.run_result("COMPLETE", attempted=3, no_reply=2, opted_out=1),
               "t-b": RuntimeError("PHI-LIKE secret 555-0199"),
               "t-c": SBRefused("TENANT_INVALID")}
    runner = se.fake_runner(se.timed(clock, {"t-a": 1.5, "t-b": 0.25}, results), tenants)
    s = se.scheduler(runner, tenants, clock)
    with caplog.at_level(logging.INFO, logger=se.LOGGER):
        await s.tick()
    (line,) = se.summaries(caplog.records)
    assert line == ("due_scheduler tick=0 skipped=0 duration_ms=1750 budget_ms=300000"
                    " over_budget=False attempted=3 no_reply=2 opted_out=1 cancelled=False"
                    " codes=COMPLETE:1,INTERNAL:1,REFUSED_TENANT_INVALID:1")
    assert "secret" not in line and "555" not in line


async def test_se_u09b_one_summary_on_cancellation(caplog: pytest.LogCaptureFixture) -> None:
    clock = se.FakeClock()
    tenants = ("t-a", "t-b")
    parked = asyncio.Event()

    async def behaviour(tenant: str, _execution: uuid.UUID) -> Any:
        if tenant == "t-b":
            parked.set()
            await asyncio.Event().wait()
        return se.run_result("COMPLETE", attempted=1, no_reply=1)

    runner = se.fake_runner(behaviour, tenants)
    s = se.scheduler(runner, tenants, clock)
    try:
        with caplog.at_level(logging.INFO, logger=se.LOGGER):
            s.start()
            await asyncio.wait_for(parked.wait(), 5)
            await asyncio.wait_for(s.stop(), 5)
    finally:
        await _drain(s)
    (line,) = se.summaries(caplog.records)
    assert line == ("due_scheduler tick=0 skipped=0 duration_ms=0 budget_ms=300000"
                    " over_budget=False attempted=1 no_reply=1 opted_out=0 cancelled=True"
                    " codes=COMPLETE:1")


@pytest.mark.parametrize(("duration", "flag"), [(300.0, "False"), (300.001, "True")])
async def test_se_u09d_over_budget_in_summary(caplog: pytest.LogCaptureFixture,
                                              duration: float, flag: str) -> None:
    clock = se.FakeClock()
    runner = se.fake_runner(se.timed(clock, {se.TENANT_A: duration}))
    s = se.scheduler(runner, (se.TENANT_A,), clock)  # I=300: budget 300000 ms
    with caplog.at_level(logging.INFO, logger=se.LOGGER):
        result = await s.tick()
    (line,) = se.summaries(caplog.records)
    assert f"duration_ms={result.duration_ms} budget_ms=300000 over_budget={flag} " in line
    assert result.duration_ms == round(duration * 1000)


def test_se_u09c_budget_flag() -> None:
    module = _mod()
    assert module.budget_ms(300) == 300_000
    assert module.budget_ms(450) == 0
    assert module.budget_ms(60) == 780_000


# --- SE-S01 -----------------------------------------------------------------------------------


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def test_se_s01_not_composed() -> None:
    targets = [SRC / "startup.py", SRC / "composition.py", SRC / "main.py",
               SRC / "scheduler.py", *sorted((SRC / "platform").glob("*.py"))]
    for path in targets:
        assert not any("due_scheduler" in name for name in _imports(path)), path
    import haloflow.composition as composition

    assert frozenset({36}) == composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert pathlib.Path(_mod().__file__).name == "due_scheduler.py"
