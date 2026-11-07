"""L-1S package S-E: due-window scheduler (synthetic only; NOT composed).

`DueWindowScheduler` calls `DueWindowRunner.run_once` for each configured tenant on a
fixed-rate timer. Governing: S-E package v1 + v1r1 (Work Session 2026-11-07), Codex ALIGN
for tests 20261107T125936Z. Owner decision Q-SB1 (2026-11-07): in normal operation a
"no reply" item should be visible within 15 minutes of the 24-hour deadline.

- Latency envelope (v1r1 C2): with interval I and whole-tick duration D, a window that
  becomes due is closed within I + D when no tick is skipped, and within 2I + D when one
  tick in between fails. The target with a one-failure margin needs 2I + D <= 900 s;
  only the default I = 300 s with D <= 300 s and a per-tenant due backlog within the
  runner's attempt cap is claimed. The interval is limited to [60, 450] s.
- Fixed rate, strict lateness: tick k starts at start + k*I; a tick that ends after the
  next start skips that start (a tick ending exactly at it skips none); skipped ticks
  are counted, never replayed back to back.
- Per tick, tenants run sequentially in captured order, each with a fresh execution id.
  A tenant's failure never stops the others. Codes come from a closed allowlist; any
  other result, refusal or exception is INTERNAL. No exception text is kept.
- One-shot lifecycle: NEW -> RUNNING -> STOPPING -> STOPPED, or STOP_FAILED. A manual
  `tick()` runs only in NEW, as an owned task. `stop()` owns, cancels and awaits the
  owned task within a bound; a timeout is reported as `SchedulerStopFailed`, never as
  success, and Python cannot force a cancellation-resistant coroutine to stop.
- One summary line per tick, after the skipped count is known (loop) or with skipped=0
  (manual), also when a tick is cancelled (completed counts, cancelled=True).

Nothing here reads activation (S-C decides at the gateway), sends a message, touches an
EHR or is imported by the platform, startup or composition modules.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final
from uuid import uuid4

from haloflow.m01.resolver import TENANT_ID_PATTERN
from haloflow.m05.due_windows import DueWindowRunner, RunResult, SBRefused

LOGGER: Final = logging.getLogger(__name__)

DEFAULT_INTERVAL_S: Final = 300
INTERVAL_MIN_S: Final = 60
INTERVAL_MAX_S: Final = 450
TARGET_S: Final = 900
STOP_TIMEOUT_MAX_S: Final = 60
TENANTS_MAX: Final = 1000

RESULT_CODES: Final = frozenset({"COMPLETE", "CAP", "SELECTION_FAILED", "RUN_IN_PROGRESS",
                                 "CONTEXT_UNAVAILABLE"})
REFUSAL_CODES: Final[Mapping[str, str]] = {
    code: f"REFUSED_{code}"
    for code in ("PRINCIPAL_NOT_WORKLOAD", "CAPABILITIES_INVALID", "LIMIT_INVALID",
                 "TENANT_INVALID", "EXECUTION_ID_INVALID")
}
INTERNAL: Final = "INTERNAL"


class SchedulerRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SchedulerStopFailed(Exception):
    def __init__(self) -> None:
        super().__init__("STOP_TIMEOUT")
        self.code = "STOP_TIMEOUT"


class SchedulerState(enum.Enum):
    NEW = "new"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STOP_FAILED = "stop_failed"


@dataclass(frozen=True, slots=True)
class TickResult:
    tick: int
    duration_ms: int
    codes: Mapping[str, int]
    attempted: int
    no_reply: int
    opted_out: int
    cancelled: bool


def budget_ms(interval_s: int) -> int:
    """Whole-tick budget that keeps 2I + D within the target."""

    return (TARGET_S - 2 * interval_s) * 1000


def _cancel_pending() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


def _valid_tenants(tenants: object) -> bool:
    return (
        type(tenants) is tuple and 1 <= len(tenants) <= TENANTS_MAX
        and all(type(t) is str and TENANT_ID_PATTERN.fullmatch(t) for t in tenants)
        and len(set(tenants)) == len(tenants)
    )


def _valid_timeout(value: object) -> bool:
    return (
        type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]
        and 0 < value <= STOP_TIMEOUT_MAX_S  # type: ignore[operator]
    )


class DueWindowScheduler:
    def __init__(self, runner: DueWindowRunner, tenants: tuple[str, ...], *,
                 interval_s: int = DEFAULT_INTERVAL_S, stop_timeout_s: float = 10,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if not isinstance(runner, DueWindowRunner):
            raise SchedulerRefused("RUNNER_INVALID")
        if not _valid_tenants(tenants):
            raise SchedulerRefused("TENANTS_INVALID")
        if type(interval_s) is not int or not INTERVAL_MIN_S <= interval_s <= INTERVAL_MAX_S:
            raise SchedulerRefused("INTERVAL_INVALID")
        if not _valid_timeout(stop_timeout_s):
            raise SchedulerRefused("TIMEOUT_INVALID")
        self._runner = runner
        self._tenants = tenants
        self._interval = interval_s
        self._timeout = float(stop_timeout_s)
        self._clock = clock
        self._sleep = sleep
        self._state = SchedulerState.NEW
        self._task: asyncio.Task[object] | None = None
        self._cancel_sent = False
        self._ticks = 0

    @property
    def state(self) -> SchedulerState:
        return self._state

    # -- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        if self._state is SchedulerState.NEW and self._task is not None:
            raise SchedulerRefused("TICK_IN_PROGRESS")
        if self._state is not SchedulerState.NEW:
            raise SchedulerRefused("NOT_STARTABLE")
        self._task = asyncio.get_running_loop().create_task(self._loop())
        self._state = SchedulerState.RUNNING

    async def tick(self) -> TickResult:
        if self._state is SchedulerState.RUNNING:
            raise SchedulerRefused("LOOP_OWNS_TICKS")
        if self._state is not SchedulerState.NEW:
            raise SchedulerRefused("SCHEDULER_STOPPED")
        if self._task is not None:
            raise SchedulerRefused("TICK_IN_PROGRESS")
        task = asyncio.get_running_loop().create_task(self._manual_tick())
        self._task = task
        stopped = False
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the caller itself was cancelled
            stopped = True
        finally:
            if task.done() and self._task is task and self._state is SchedulerState.NEW:
                self._task = None
        assert stopped
        raise SchedulerRefused("STOPPED_DURING_TICK")

    async def stop(self) -> None:
        if self._state is SchedulerState.STOPPED:
            return
        task = self._task
        if task is None:
            self._state = SchedulerState.STOPPED
            return
        self._state = SchedulerState.STOPPING
        if not self._cancel_sent and not task.done():
            self._cancel_sent = True
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=self._timeout)
        if not done:
            self._state = SchedulerState.STOP_FAILED
            raise SchedulerStopFailed()
        if not task.cancelled():
            task.exception()  # retrieved; never re-raised or logged as text
        self._state = SchedulerState.STOPPED

    # -- ticks ---------------------------------------------------------------------------

    async def _manual_tick(self) -> TickResult:
        result = await self._run_tick(log_on_cancel=True)
        self._log(result, skipped=0)
        return result

    async def _loop(self) -> None:
        next_at = self._clock()
        while True:
            await self._sleep(max(0.0, next_at - self._clock()))
            _cancel_pending()
            result = await self._run_tick(log_on_cancel=True)
            end = self._clock()
            next_at += self._interval
            skipped = 0
            while end > next_at:  # strict lateness
                next_at += self._interval
                skipped += 1
            self._log(result, skipped=skipped)

    async def _run_tick(self, *, log_on_cancel: bool) -> TickResult:
        number = self._ticks
        self._ticks += 1
        started = self._clock()
        codes: Counter[str] = Counter()
        totals = [0, 0, 0]
        try:
            for tenant in self._tenants:
                _cancel_pending()
                code = await self._one(tenant, totals)
                codes[code] += 1
        except asyncio.CancelledError:
            if log_on_cancel:
                self._log(self._result(number, started, codes, totals, True), skipped=0)
            raise
        return self._result(number, started, codes, totals, False)

    async def _one(self, tenant: str, totals: list[int]) -> str:
        try:
            result = await self._runner.run_once(tenant, uuid4())
        except SBRefused as refused:
            return REFUSAL_CODES.get(refused.code, INTERNAL)
        except Exception:  # noqa: BLE001 - classified by code only, no text kept
            return INTERNAL
        if type(result) is not RunResult:
            return INTERNAL
        totals[0] += result.attempted
        totals[1] += result.no_reply
        totals[2] += result.opted_out
        return result.code if result.code in RESULT_CODES else INTERNAL

    def _result(self, number: int, started: float, codes: Counter[str], totals: list[int],
                cancelled: bool) -> TickResult:
        duration = max(0, round((self._clock() - started) * 1000))
        return TickResult(number, duration, dict(sorted(codes.items())), totals[0],
                          totals[1], totals[2], cancelled)

    def _log(self, result: TickResult, *, skipped: int) -> None:
        budget = budget_ms(self._interval)
        codes = ",".join(f"{k}:{v}" for k, v in sorted(result.codes.items()))
        LOGGER.info(
            "due_scheduler tick=%d skipped=%d duration_ms=%d budget_ms=%d over_budget=%s"
            " attempted=%d no_reply=%d opted_out=%d cancelled=%s codes=%s",
            result.tick, skipped, result.duration_ms, budget, result.duration_ms > budget,
            result.attempted, result.no_reply, result.opted_out, result.cancelled, codes,
        )
