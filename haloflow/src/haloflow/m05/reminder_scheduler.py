"""L-1S package S-H, checkpoint H3: reminder scheduler (synthetic only; NOT composed).

`ReminderScheduler` runs, on a fixed-rate timer, one bounded pass per configured tenant:

1. context: one fresh tenant context per tenant per tick (the injected `contexts`), shared
   by the three phases below; a failure leaves every phase NOT_RUN;
2. expire: `OutboundService.expire` (t051) turns an `attempting` op whose lease expired
   into `uncertain` (never a resend);
3. accepted recovery: t075 `accepted_pending` lists ops in state `accepted` (provider
   accepted, registration not completed); each is passed to `OutboundWorker.recover`,
   which is registration only. A per-tenant in-memory keyset cursor advances past every
   returned row whatever its outcome; an empty page with a cursor set wraps once to the
   start (at most two selector calls); a short page resets the cursor;
4. ready: the injected H2 `ReminderRunner.run_once`, bounded by the runner's own batch.

Governing: H3 package v1 + v1r1 + v1r2 deltas, red interface pins v1, the implementation
checklist and mutation map v1 (Work Session 2026-11-09), Codex source gate PASS
20261109T201944Z.

- Send authority: this module never calls a sender, examine, consume or claim. The only
  send path is `run_once` -> `run_claimed` through H2's committed one-shot ticket.
- Isolation: the context, each phase and each recovery item has its own `try`; ready
  work runs whatever happened in expire and recovery (a failure that raises, not a
  hang). One tenant never stops another. Cancellation is never caught; it is re-checked
  before every side-effecting phase and item and after every tenant.
- Reporting is what is observable, nothing inferred: a phase is `ok`, `NOT_RUN`,
  `DEGRADED_ITEMS` (recovery: a selector returned but an item failed) or the failure class
  of the exception it raised. The tenant code is COMPLETE when the pass completed with
  every phase `ok` (pass completion, NOT business success: zero sends can be COMPLETE),
  else DEGRADED. The ready pass's swallowed examine/consume refusals are not reported
  (F-H3-1). No exception text, phone, name or message text is kept or logged.
- Bounded work: per tenant per tick at most 1 expire, 2 selector calls, `recovery_limit`
  recover calls (each at most two registers, H1) and one `run_once`. Progress assumes
  each awaited operation completes; there is no phase timeout (F-H3-2), so a hung await
  blocks the rest of that tick. `stop()` is bounded independently: cancel once, wait at
  most `stop_timeout_s`, else `ReminderSchedulerStopFailed` (STOP_FAILED keeps ownership
  of the task: no restart, no manual tick; a later `stop()` settles it).
- Lifecycle, fixed rate and strict lateness follow the S-E `DueWindowScheduler` (whose
  `SchedulerState` is reused, unedited). One summary line per tick.

Nothing here reads activation itself (S-C decides at the gateway), touches an EHR or is
imported by the platform, startup or composition modules.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from haloflow.m01.context import TenantContext
from haloflow.m01.errors import ActivationRefused, CapabilityDenied, TenantDenied
from haloflow.m01.resolver import TENANT_ID_PATTERN
from haloflow.m05.due_scheduler import SchedulerState
from haloflow.m05.outbound import OutboundCommitUnknown, OutboundRefused, OutboundWorker
from haloflow.m05.reminder import ReminderRunner, ReminderService, RunReport
from haloflow.m05.reminder_scheduler_statements import ACCEPTED_PENDING_KEY

LOGGER: Final = logging.getLogger(__name__)

DEFAULT_INTERVAL_S: Final = 300
INTERVAL_MIN_S: Final = 60
INTERVAL_MAX_S: Final = 450
STOP_TIMEOUT_MAX_S: Final = 60
TENANTS_MAX: Final = 1000
DEFAULT_RECOVERY_LIMIT: Final = 100
RECOVERY_LIMIT_MAX: Final = 1000

COMPLETE: Final = "COMPLETE"
DEGRADED: Final = "DEGRADED"
OK: Final = "ok"
NOT_RUN: Final = "NOT_RUN"
DEGRADED_ITEMS: Final = "DEGRADED_ITEMS"
INTERNAL: Final = "INTERNAL"

RECOVERY_KEYS: Final = ("registered", "existing", "held", "refused_activation",
                        "refused_capability", "refused_not_accepted", "refused_other",
                        "commit_unknown", "failed")
_BENIGN: Final = frozenset({"registered", "existing", "held"})
_CAPABILITY_CODES: Final = frozenset({"PRINCIPAL_NOT_WORKLOAD", "CAPABILITIES_INVALID"})
READY_KEYS: Final = ("examined", "claimed", "sent")

Cursor = tuple[str, str]


class ReminderSchedulerRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReminderSchedulerStopFailed(Exception):
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
    expire: PhaseReport
    recovery: PhaseReport
    ready: PhaseReport


@dataclass(frozen=True, slots=True)
class TickResult:
    tick: int
    duration_ms: int
    codes: Mapping[str, int]
    tenants: tuple[TenantReport, ...]
    cancelled: bool


class ReminderSchedulerService(ReminderService):
    """`ReminderService` plus the t075 accepted selector."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ReminderSchedulerService()"

    async def accepted_pending(self, context: TenantContext, limit: int,
                               after: Cursor | None) -> dict[str, Any]:
        self._admit(context)
        cursor = None if after is None else {"updated_at": str(after[0]),
                                              "op_id": str(after[1])}
        return await self._statement(context, ACCEPTED_PENDING_KEY,
                                     {"limit": int(limit), "after": cursor})


# --- helpers ------------------------------------------------------------------------------


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


def _failure_class(error: Exception) -> str:
    """Closed mapping from the raised type (and a refusal's code); never the text."""

    if isinstance(error, ActivationRefused):
        return "ACTIVATION_REFUSED"
    if isinstance(error, (CapabilityDenied, TenantDenied)):
        return "CAPABILITY_REFUSED"
    if isinstance(error, OutboundCommitUnknown):
        return "COMMIT_UNKNOWN"
    if isinstance(error, OutboundRefused):
        return "CAPABILITY_REFUSED" if error.code in _CAPABILITY_CODES else "REFUSED"
    return INTERNAL


def _item_count(error: Exception) -> str:
    if isinstance(error, ActivationRefused):
        return "refused_activation"
    if isinstance(error, (CapabilityDenied, TenantDenied)):
        return "refused_capability"
    if isinstance(error, OutboundCommitUnknown):
        return "commit_unknown"
    if isinstance(error, OutboundRefused):
        if error.code in _CAPABILITY_CODES:
            return "refused_capability"
        return "refused_not_accepted" if error.code == "NOT_ACCEPTED" else "refused_other"
    return "failed"


def _page(result: object) -> list[Cursor]:
    """The selector's rows as `(updated_at, op_id)` keys; anything else is malformed."""

    if not isinstance(result, Mapping) or result.get("schema_version") != 1:
        raise ValueError("selector result invalid")
    ops = result.get("ops")
    if not isinstance(ops, list):
        raise ValueError("selector result invalid")
    rows: list[Cursor] = []
    for row in ops:
        if (not isinstance(row, Mapping) or type(row.get("updated_at")) is not str
                or type(row.get("op_id")) is not str):
            raise ValueError("selector result invalid")
        rows.append((row["updated_at"], row["op_id"]))
    return rows


def _zero(keys: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(keys, 0)


_EXPIRE_NOT_RUN: Final = PhaseReport(NOT_RUN, {"expired": 0})
_RECOVERY_NOT_RUN: Final = PhaseReport(NOT_RUN, dict.fromkeys(RECOVERY_KEYS, 0))
_READY_NOT_RUN: Final = PhaseReport(NOT_RUN, dict.fromkeys(READY_KEYS, 0))


# --- scheduler ----------------------------------------------------------------------------


class ReminderScheduler:
    def __init__(self, *, contexts: Callable[[str], Awaitable[TenantContext]],
                 service: ReminderSchedulerService, worker: OutboundWorker,
                 runner: ReminderRunner, tenants: tuple[str, ...],
                 interval_s: int = DEFAULT_INTERVAL_S, stop_timeout_s: float = 10,
                 recovery_limit: int = DEFAULT_RECOVERY_LIMIT,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if not callable(contexts):
            raise ReminderSchedulerRefused("CONTEXTS_INVALID")
        if not isinstance(service, ReminderSchedulerService):
            raise ReminderSchedulerRefused("SERVICE_INVALID")
        if not isinstance(worker, OutboundWorker):
            raise ReminderSchedulerRefused("WORKER_INVALID")
        if not isinstance(runner, ReminderRunner):
            raise ReminderSchedulerRefused("RUNNER_INVALID")
        if not _valid_tenants(tenants):
            raise ReminderSchedulerRefused("TENANTS_INVALID")
        if type(interval_s) is not int or not INTERVAL_MIN_S <= interval_s <= INTERVAL_MAX_S:
            raise ReminderSchedulerRefused("INTERVAL_INVALID")
        if not _valid_timeout(stop_timeout_s):
            raise ReminderSchedulerRefused("TIMEOUT_INVALID")
        if type(recovery_limit) is not int or not 1 <= recovery_limit <= RECOVERY_LIMIT_MAX:
            raise ReminderSchedulerRefused("RECOVERY_LIMIT_INVALID")
        self._contexts = contexts
        self._service = service
        self._worker = worker
        self._runner = runner
        self._tenants = tenants
        self._interval = interval_s
        self._timeout = float(stop_timeout_s)
        self._limit = recovery_limit
        self._clock = clock
        self._sleep = sleep
        self._state = SchedulerState.NEW
        self._task: asyncio.Task[object] | None = None
        self._cancel_sent = False
        self._ticks = 0
        self._cursors: dict[str, Cursor | None] = {}

    def __repr__(self) -> str:
        return f"ReminderScheduler(state={self._state.value}, tenants={len(self._tenants)})"

    @property
    def state(self) -> SchedulerState:
        return self._state

    # -- lifecycle (the S-E shape) -------------------------------------------------------

    def start(self) -> None:
        if self._state is SchedulerState.NEW and self._task is not None:
            raise ReminderSchedulerRefused("TICK_IN_PROGRESS")
        if self._state is not SchedulerState.NEW:
            raise ReminderSchedulerRefused("NOT_STARTABLE")
        self._task = asyncio.get_running_loop().create_task(self._loop())
        self._state = SchedulerState.RUNNING

    async def tick(self) -> TickResult:
        if self._state is SchedulerState.RUNNING:
            raise ReminderSchedulerRefused("LOOP_OWNS_TICKS")
        if self._state is not SchedulerState.NEW:
            raise ReminderSchedulerRefused("SCHEDULER_STOPPED")
        if self._task is not None:
            raise ReminderSchedulerRefused("TICK_IN_PROGRESS")
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
        raise ReminderSchedulerRefused("STOPPED_DURING_TICK")

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
            raise ReminderSchedulerStopFailed()
        if not task.cancelled():
            task.exception()  # retrieved; never re-raised or logged as text
        self._state = SchedulerState.STOPPED

    # -- ticks ---------------------------------------------------------------------------

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
            while end > next_at:  # strict lateness: counted, never replayed
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
                _cancel_pending()  # a collaborator that swallowed cancellation
        except asyncio.CancelledError:
            self._log(self._result(number, started, reports, True), skipped=0)
            raise
        return self._result(number, started, reports, False)

    async def _one(self, tenant: str) -> TenantReport:
        try:
            context = await self._contexts(tenant)
        except Exception as error:  # noqa: BLE001 - classified by type only, no text kept
            return TenantReport(tenant, DEGRADED, _failure_class(error), _EXPIRE_NOT_RUN,
                                _RECOVERY_NOT_RUN, _READY_NOT_RUN)
        _cancel_pending()
        expire = await self._expire_phase(context)
        _cancel_pending()
        recovery = await self._recovery_phase(tenant, context)
        _cancel_pending()
        ready = await self._ready_phase(context)
        phases = (expire.status, recovery.status, ready.status)
        code = COMPLETE if all(status == OK for status in phases) else DEGRADED
        return TenantReport(tenant, code, OK, expire, recovery, ready)

    async def _expire_phase(self, context: TenantContext) -> PhaseReport:
        try:
            result = await self._service.expire(context, self._limit)
        except Exception as error:  # noqa: BLE001
            return PhaseReport(_failure_class(error), {"expired": 0})
        expired = result.get("expired") if isinstance(result, Mapping) else None
        if type(expired) is not int or expired < 0:
            return PhaseReport(INTERNAL, {"expired": 0})
        return PhaseReport(OK, {"expired": expired})

    async def _recovery_phase(self, tenant: str, context: TenantContext) -> PhaseReport:
        counts = _zero(RECOVERY_KEYS)
        cursor = self._cursors.get(tenant)
        try:
            page = _page(await self._service.accepted_pending(context, self._limit, cursor))
            if not page and cursor is not None:  # bounded wrap: once, from the start
                cursor = None
                self._cursors[tenant] = None
                _cancel_pending()
                page = _page(await self._service.accepted_pending(context, self._limit, None))
        except Exception as error:  # noqa: BLE001
            return PhaseReport(_failure_class(error), counts)
        for key in page:
            _cancel_pending()
            counts[await self._recover_item(context, key[1])] += 1
            self._cursors[tenant] = key  # past every row, whatever its outcome
        if len(page) < self._limit:
            self._cursors[tenant] = None
        failed = sum(v for k, v in counts.items() if k not in _BENIGN)
        return PhaseReport(DEGRADED_ITEMS if failed else OK, counts)

    async def _recover_item(self, context: TenantContext, op_id: str) -> str:
        try:
            result = await self._worker.recover(context, op_id)
        except Exception as error:  # noqa: BLE001
            return _item_count(error)
        outcome = result.get("outcome") if isinstance(result, Mapping) else None
        return outcome if outcome in _BENIGN else "failed"

    async def _ready_phase(self, context: TenantContext) -> PhaseReport:
        try:
            report = await self._runner.run_once(context)
        except Exception as error:  # noqa: BLE001
            return PhaseReport(_failure_class(error), _zero(READY_KEYS))
        if type(report) is not RunReport:
            return PhaseReport(INTERNAL, _zero(READY_KEYS))
        return PhaseReport(OK, {"examined": report.examined, "claimed": report.claimed,
                                "sent": report.sent})

    # -- reporting -----------------------------------------------------------------------

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
        registered = sum(t.recovery.counts.get("registered", 0) for t in result.tenants)
        recovery_failed = sum(v for t in result.tenants
                              for k, v in t.recovery.counts.items() if k not in _BENIGN)
        ready = [sum(t.ready.counts.get(k, 0) for t in result.tenants) for k in READY_KEYS]
        LOGGER.info(
            "reminder_scheduler tick=%d skipped=%d duration_ms=%d budget_ms=%d"
            " over_budget=%s cancelled=%s codes=%s expired=%d registered=%d"
            " recovery_failed=%d examined=%d claimed=%d sent=%d",
            result.tick, skipped, result.duration_ms, budget, result.duration_ms > budget,
            result.cancelled, codes, expired, registered, recovery_failed, *ready,
        )
