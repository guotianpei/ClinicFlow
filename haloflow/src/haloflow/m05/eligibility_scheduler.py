"""Eligibility checks E2: the eligibility scheduler (SYNTHETIC ONLY; NOT composed).

`EligibilityScheduler` runs, on a fixed-rate timer, one bounded pass per configured tenant
(the S-J WritebackScheduler pattern):

1. pass context: one fresh tenant context (the injected `contexts`); a failure leaves both
   phases NOT_RUN;
2. run: `EligibilityService.due(ctx, batch_limit)`, then for each verification id (at most
   `batch_limit`) ONE fresh context and `EligibilityWorker.run_one(ctx_i, vid)`; a per-id
   context failure is counted `context_failed` and the next id continues;
3. expire: `EligibilityService.expire(pass ctx, batch_limit)`;
4. apply (E3a, only when `cases` and `case_contexts` are given): one fresh case context and
   `EligibilityCaseService.drain(ctx, batch_limit)`; without them the phase is NOT_RUN with
   zero counts and every E2 behaviour is unchanged.

This module never claims, dispatches, answers, settles or calls the port itself. One tenant
never stops another; cancellation is never caught and is re-checked before every
side-effecting step. One summary line per tick on this module's logger, codes and counts
only (no exception text, member id, name or date of birth). There is no phase timeout
(the S-J F-H3-2 limitation); `stop()` is bounded independently.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final

from haloflow.m01.context import TenantContext
from haloflow.m01.errors import ActivationRefused, CapabilityDenied, TenantDenied
from haloflow.m01.resolver import TENANT_ID_PATTERN
from haloflow.m05.due_scheduler import SchedulerState
from haloflow.m05.eligibility import (
    WORKER_OUTCOMES,
    EligibilityRefused,
    EligibilityService,
    EligibilityWorker,
)
from haloflow.m05.eligibility_cases import EligibilityCaseRefused, EligibilityCaseService

LOGGER: Final = logging.getLogger(__name__)

DEFAULT_INTERVAL_S: Final = 60
INTERVAL_MIN_S: Final = 30
INTERVAL_MAX_S: Final = 300
STOP_TIMEOUT_MAX_S: Final = 60
TENANTS_MAX: Final = 1000
DEFAULT_BATCH_LIMIT: Final = 100
BATCH_LIMIT_MAX: Final = 1000

COMPLETE: Final = "COMPLETE"
DEGRADED: Final = "DEGRADED"
OK: Final = "ok"
NOT_RUN: Final = "NOT_RUN"
DEGRADED_ITEMS: Final = "DEGRADED_ITEMS"
INTERNAL: Final = "INTERNAL"
TENANT_MISMATCH: Final = "TENANT_MISMATCH"

RUN_KEYS: Final = (*WORKER_OUTCOMES, "context_failed", "failed")
APPLY_KEYS: Final = ("opened", "noted", "none")
_CAPABILITY_CODES: Final = frozenset({"PRINCIPAL_NOT_WORKLOAD", "CAPABILITIES_INVALID"})


class EligibilitySchedulerRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EligibilitySchedulerStopFailed(Exception):
    def __init__(self) -> None:
        super().__init__("STOP_TIMEOUT")
        self.code = "STOP_TIMEOUT"


@dataclass(frozen=True, slots=True)
class PhaseReport:
    status: str
    counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class TenantReport:
    tenant: str
    code: str
    context: str
    run: PhaseReport
    expire: PhaseReport
    apply: PhaseReport = PhaseReport(NOT_RUN, dict.fromkeys(APPLY_KEYS, 0))


@dataclass(frozen=True, slots=True)
class TickResult:
    tick: int
    duration_ms: int
    codes: Mapping[str, int]
    tenants: tuple[TenantReport, ...]
    cancelled: bool


def _cancel_pending() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


def _valid_tenants(tenants: object) -> bool:
    return (type(tenants) is tuple and 1 <= len(tenants) <= TENANTS_MAX
            and all(type(t) is str and TENANT_ID_PATTERN.fullmatch(t) for t in tenants)
            and len(set(tenants)) == len(tenants))


def _valid_timeout(value: object) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]
            and 0 < value <= STOP_TIMEOUT_MAX_S)  # type: ignore[operator]


def _failure_class(error: Exception) -> str:
    if isinstance(error, ActivationRefused):
        return "ACTIVATION_REFUSED"
    if isinstance(error, (CapabilityDenied, TenantDenied)):
        return "CAPABILITY_REFUSED"
    if isinstance(error, EligibilityRefused):
        return "CAPABILITY_REFUSED" if error.code in _CAPABILITY_CODES else "REFUSED"
    if isinstance(error, EligibilityCaseRefused):
        return "CAPABILITY_REFUSED" if error.code in _CAPABILITY_CODES else "REFUSED"
    return INTERNAL


_RUN_NOT_RUN: Final = PhaseReport(NOT_RUN, dict.fromkeys(RUN_KEYS, 0))
_EXPIRE_NOT_RUN: Final = PhaseReport(NOT_RUN, {"expired": 0})


def _apply_not_run() -> PhaseReport:
    return PhaseReport(NOT_RUN, dict.fromkeys(APPLY_KEYS, 0))


class EligibilityScheduler:
    def __init__(self, *, contexts: Callable[[str], Awaitable[TenantContext]],
                 service: EligibilityService, worker: EligibilityWorker,
                 tenants: tuple[str, ...], interval_s: int = DEFAULT_INTERVAL_S,
                 stop_timeout_s: float = 10, batch_limit: int = DEFAULT_BATCH_LIMIT,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 cases: EligibilityCaseService | None = None,
                 case_contexts: Callable[[str], Awaitable[TenantContext]] | None = None,
                 ) -> None:
        if not callable(contexts):
            raise EligibilitySchedulerRefused("CONTEXTS_INVALID")
        if not isinstance(service, EligibilityService):
            raise EligibilitySchedulerRefused("SERVICE_INVALID")
        if not isinstance(worker, EligibilityWorker):
            raise EligibilitySchedulerRefused("WORKER_INVALID")
        if not _valid_tenants(tenants):
            raise EligibilitySchedulerRefused("TENANTS_INVALID")
        if type(interval_s) is not int or not INTERVAL_MIN_S <= interval_s <= INTERVAL_MAX_S:
            raise EligibilitySchedulerRefused("INTERVAL_INVALID")
        if not _valid_timeout(stop_timeout_s):
            raise EligibilitySchedulerRefused("TIMEOUT_INVALID")
        if type(batch_limit) is not int or not 1 <= batch_limit <= BATCH_LIMIT_MAX:
            raise EligibilitySchedulerRefused("BATCH_LIMIT_INVALID")
        if (cases is None) != (case_contexts is None) or (
                cases is not None and (not isinstance(cases, EligibilityCaseService)
                                       or not callable(case_contexts))):
            raise EligibilitySchedulerRefused("CASES_INVALID")
        self._cases = cases
        self._case_contexts = case_contexts
        self._contexts = contexts
        self._service = service
        self._worker = worker
        self._tenants = tenants
        self._interval = interval_s
        self._timeout = float(stop_timeout_s)
        self._limit = batch_limit
        self._clock = clock
        self._sleep = sleep
        self._state = SchedulerState.NEW
        self._task: asyncio.Task[object] | None = None
        self._cancel_sent = False
        self._ticks = 0

    def __repr__(self) -> str:
        return f"EligibilityScheduler(state={self._state.value}, tenants={len(self._tenants)})"

    @property
    def state(self) -> SchedulerState:
        return self._state

    def start(self) -> None:
        if self._state is SchedulerState.NEW and self._task is not None:
            raise EligibilitySchedulerRefused("TICK_IN_PROGRESS")
        if self._state is not SchedulerState.NEW:
            raise EligibilitySchedulerRefused("NOT_STARTABLE")
        self._task = asyncio.get_running_loop().create_task(self._loop())
        self._state = SchedulerState.RUNNING

    async def tick(self) -> TickResult:
        if self._state is SchedulerState.RUNNING:
            raise EligibilitySchedulerRefused("LOOP_OWNS_TICKS")
        if self._state is not SchedulerState.NEW:
            raise EligibilitySchedulerRefused("SCHEDULER_STOPPED")
        if self._task is not None:
            raise EligibilitySchedulerRefused("TICK_IN_PROGRESS")
        task = asyncio.get_running_loop().create_task(self._manual_tick())
        self._task = task
        stopped = False
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            stopped = True
        finally:
            if task.done() and self._task is task and self._state is SchedulerState.NEW:
                self._task = None
        assert stopped
        raise EligibilitySchedulerRefused("STOPPED_DURING_TICK")

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
            raise EligibilitySchedulerStopFailed()
        if not task.cancelled():
            task.exception()
        self._state = SchedulerState.STOPPED

    async def _manual_tick(self) -> TickResult:
        result = await self._run_tick()
        self._log(result, skipped=0)
        return result

    async def _loop(self) -> None:
        next_at = self._clock()
        while True:
            await self._sleep(max(0.0, next_at - self._clock()))
            _cancel_pending()
            result = await self._run_tick()
            end = self._clock()
            next_at += self._interval
            skipped = 0
            while end > next_at:
                next_at += self._interval
                skipped += 1
            self._log(result, skipped=skipped)

    async def _run_tick(self) -> TickResult:
        number = self._ticks
        self._ticks += 1
        started = self._clock()
        reports: list[TenantReport] = []
        try:
            for tenant in self._tenants:
                _cancel_pending()
                reports.append(await self._one(tenant))
                _cancel_pending()
        except asyncio.CancelledError:
            self._log(self._result(number, started, reports, True), skipped=0)
            raise
        return self._result(number, started, reports, False)

    async def _one(self, tenant: str) -> TenantReport:
        try:
            context = await self._contexts(tenant)
        except Exception as error:  # noqa: BLE001
            return TenantReport(tenant, DEGRADED, _failure_class(error), _RUN_NOT_RUN,
                                _EXPIRE_NOT_RUN, _apply_not_run())
        _cancel_pending()
        run = await self._run_phase(tenant, context)
        _cancel_pending()
        expire = await self._expire_phase(context)
        apply = _apply_not_run()
        if self._cases is not None:
            _cancel_pending()
            apply = await self._apply_phase(tenant)
        ok = run.status == OK and expire.status == OK and (
            self._cases is None or apply.status == OK)
        return TenantReport(tenant, COMPLETE if ok else DEGRADED, OK, run, expire, apply)

    async def _apply_phase(self, tenant: str) -> PhaseReport:
        counts = dict.fromkeys(APPLY_KEYS, 0)
        assert self._cases is not None and self._case_contexts is not None
        try:
            context = await self._case_contexts(tenant)
            # The scheduled tenant owns this phase: a context resolved for any other tenant
            # is refused before drain, so nothing is written or counted under the wrong
            # tenant (Codex E3a green review, 2026-11-20).
            if getattr(context, "tenant_id", None) != tenant:
                return PhaseReport(TENANT_MISMATCH, counts)
            _cancel_pending()
            drained = await self._cases.drain(context, self._limit)
        except Exception as error:  # noqa: BLE001
            return PhaseReport(_failure_class(error), counts)
        if not isinstance(drained, dict) or not set(drained) <= set(APPLY_KEYS):
            return PhaseReport(INTERNAL, counts)
        counts.update(drained)
        return PhaseReport(OK, counts)

    async def _run_phase(self, tenant: str, context: TenantContext) -> PhaseReport:
        counts = dict.fromkeys(RUN_KEYS, 0)
        try:
            ids = await self._service.due(context, self._limit)
        except Exception as error:  # noqa: BLE001
            return PhaseReport(_failure_class(error), counts)
        if not isinstance(ids, list) or not all(type(i) is str for i in ids):
            return PhaseReport(INTERNAL, counts)
        for vid in ids[:self._limit]:
            _cancel_pending()
            try:
                item_context = await self._contexts(tenant)
            except Exception:  # noqa: BLE001
                counts["context_failed"] += 1
                continue
            _cancel_pending()
            try:
                outcome = await self._worker.run_one(item_context, vid)
            except Exception:  # noqa: BLE001
                counts["failed"] += 1
                continue
            counts[outcome if outcome in WORKER_OUTCOMES else "failed"] += 1
        bad = counts["failed"] + counts["context_failed"]
        return PhaseReport(DEGRADED_ITEMS if bad else OK, counts)

    async def _expire_phase(self, context: TenantContext) -> PhaseReport:
        try:
            count = await self._service.expire(context, self._limit)
        except Exception as error:  # noqa: BLE001
            return PhaseReport(_failure_class(error), {"expired": 0})
        if type(count) is not int or count < 0:
            return PhaseReport(INTERNAL, {"expired": 0})
        return PhaseReport(OK, {"expired": count})

    def _result(self, number: int, started: float, reports: list[TenantReport],
                cancelled: bool) -> TickResult:
        duration = max(0, round((self._clock() - started) * 1000))
        codes = Counter(report.code for report in reports)
        return TickResult(number, duration, dict(sorted(codes.items())), tuple(reports),
                          cancelled)

    def _log(self, result: TickResult, *, skipped: int) -> None:
        budget = self._interval * 1000
        codes = ",".join(f"{k}:{v}" for k, v in sorted(result.codes.items()))
        expired = sum(t.expire.counts.get("expired", 0) for t in result.tenants)
        run = " ".join(f"{k}={sum(t.run.counts.get(k, 0) for t in result.tenants)}"
                       for k in RUN_KEYS)
        LOGGER.info("eligibility_scheduler tick=%d skipped=%d duration_ms=%d budget_ms=%d"
                    " over_budget=%s cancelled=%s codes=%s expired=%d %s",
                    result.tick, skipped, result.duration_ms, budget,
                    result.duration_ms > budget, result.cancelled, codes, expired, run)
