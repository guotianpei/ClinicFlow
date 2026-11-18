"""The synthetic runtime's workers and schedulers (Option B B3a, R-B3a-3 step 6). SYNTHETIC ONLY.

Built from accepted `src` classes only, as the accepted test-side S-J harness composes them
(`tests/m05/sj_harness.build`): the H3 reminder scheduler (H2 one-shot send through the S-I
baseline capture), the S-E due-window scheduler, the S-J write-back scheduler against a
port over `SimulatedEhr`, and `FakeSender`. Never a real EHR, never a vendor client.

Lifecycle ownership (B3a):
- each scheduler is the accepted class with three additions (`_Managed`): its loop's FIRST
  tick waits a full interval (so starting the app performs no immediate work); a manual
  `tick()` is allowed while the loop runs, serialised with the loop's ticks (one tick at a
  time); and `stop()` lets an in-flight manual tick finish within a quarter of the bound,
  cancels it otherwise, then runs the accepted stop. A tick requested after the runtime
  began stopping is refused with the stop's closed code (no retry after a guard refusal).
- the `b3.r.*` hook points (test-only FAULT; R rows crash there) mark the J08 recovery
  boundaries: after an accepted send is recorded (before registration), after a consume
  commits (before the send), after write-back discovery commits, after a permit is spent,
  after the simulated EHR write returns, after a settle commits.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m02.observation import EventFoundation
from haloflow.m05.due_scheduler import DueWindowScheduler, SchedulerState
from haloflow.m05.due_statements import DUE_KEY
from haloflow.m05.due_windows import DueWindowRunner
from haloflow.m05.intake import IntakeService
from haloflow.m05.intake_statements import CLOSE_KEY
from haloflow.m05.notes import KeyUnavailable
from haloflow.m05.outbound import OutboundWorker
from haloflow.m05.reminder import ReminderRunner, render_inputs
from haloflow.m05.reminder_scheduler import ReminderScheduler, ReminderSchedulerService
from haloflow.m05.writeback import (
    APPEND_CAPABILITY,
    CAPABILITIES,
    CAPTURE_CAPABILITIES,
    CREATE_KEY,
    BaselineWorker,
    WritebackService,
    WritebackWorker,
)
from haloflow.m05.writeback_scheduler import WritebackScheduler
from haloflow.synthetic.composition import W_DISPATCH, W_TIMER
from haloflow.synthetic.errors import SyntheticRefused
from haloflow.synthetic.hooks import fire

PRODUCER: Final = "m05_intake"
PRODUCERS: Final = ("writeback", "reminder", "due")      # stop order (start order reversed)
TIMER_CAPS: Final = frozenset({DUE_KEY, CLOSE_KEY, APPEND_CAPABILITY, CREATE_KEY})
INTERVALS: Final = {"due": 300, "reminder": 300, "writeback": 60}
WRITE_TIMEOUT_S: Final = 10
Gate = Callable[[], "str | None"]


# ---------------------------------------------------------------------------- note keys

class StateNoteKeys:
    """`NoteKeyProvider` over the install's per-setup note keys (state file, 0600; never
    logged). One version; no rotation (no OD-D2 custody claim)."""

    def __init__(self, keys: Mapping[str, Any]) -> None:
        self._version = int(keys["version"])
        self._hmac = bytes.fromhex(str(keys["hmac"]))
        self._enc = bytes.fromhex(str(keys["enc"]))

    def __repr__(self) -> str:
        return "StateNoteKeys()"

    def current_hmac(self) -> tuple[int, bytes]:
        return self._version, self._hmac

    def hmac_key(self, version: int) -> bytes:
        if version != self._version:
            raise KeyUnavailable()
        return self._hmac

    def current_encryption(self) -> tuple[int, bytes]:
        return self._version, self._enc

    def encryption_key(self, version: int) -> bytes:
        if version != self._version:
            raise KeyUnavailable()
        return self._enc


# ---------------------------------------------------------------------------- EHR port

class SimulatedEhrPort:
    """The write-back worker's EHR port: forwards to the simulated EHR (`.ehr`); refused
    after close."""

    def __init__(self, ehr: Any) -> None:
        self.ehr = ehr
        self.closed = False

    def _use(self) -> None:
        if self.closed:
            raise RuntimeError("port closed")

    async def read(self, appointment_id: str) -> Any:
        self._use()
        return await self.ehr.read(appointment_id)

    async def close_permit(self, permit_id: str) -> None:
        self._use()
        await self.ehr.close_permit(permit_id)

    async def write_confirmation(self, appointment_id: str, **kw: Any) -> Any:
        self._use()
        result = await self.ehr.write_confirmation(appointment_id, **kw)
        fire("b3.r.after_write_issued", {})
        return result

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------- services

class _ReminderService(ReminderSchedulerService):
    async def consume(self, context: Any, op_id: Any, attempt_id: Any) -> Any:
        result = await super().consume(context, op_id, attempt_id)
        fire("b3.r.after_consume", {})
        return result


class _OutboundWorker(OutboundWorker):
    async def _record(self, context: Any, op_id: Any, attempt: Any, outcome: str,
                      **evidence: Any) -> None:
        await super()._record(context, op_id, attempt, outcome, **evidence)
        if outcome == "accepted":
            fire("b3.r.after_accept", {})


class _WritebackService(WritebackService):
    async def discover(self, context: Any, limit: int = 100) -> int:
        found = await super().discover(context, limit)
        if found:
            fire("b3.r.after_reply", {})
        return found

    async def spend(self, context: Any, wb_op_id: Any, fence: int, *args: Any,
                    **kwargs: Any) -> Any:
        result = await super().spend(context, wb_op_id, fence, *args, **kwargs)
        fire("b3.r.after_spend", {})
        return result

    async def settle(self, context: Any, wb_op_id: Any, fence: int, *args: Any,
                     **kwargs: Any) -> Any:
        result = await super().settle(context, wb_op_id, fence, *args, **kwargs)
        fire("b3.r.after_settle_commit", {})
        return result


# ---------------------------------------------------------------------------- schedulers

class _Managed:
    """Mixin over an accepted scheduler class (see the module docstring)."""

    _b3_gate: Gate
    _b3_lock: asyncio.Lock
    _b3_manual: set[asyncio.Task[Any]]
    _b3_bound: float
    _b3_interval: float
    _b3_first: bool

    def _b3_init(self, gate: Gate, bound: float, interval: float) -> None:
        self._b3_gate = gate
        self._b3_lock = asyncio.Lock()
        self._b3_manual = set()
        self._b3_bound = bound
        self._b3_interval = interval
        self._b3_first = True

    async def _b3_sleep(self, seconds: float) -> None:
        if self._b3_first:
            self._b3_first = False
            seconds = self._b3_interval
        await asyncio.sleep(max(0.0, seconds))

    async def _run_tick(self, *args: Any, **kwargs: Any) -> Any:
        async with self._b3_lock:
            return await super()._run_tick(*args, **kwargs)  # type: ignore[misc]

    async def tick(self) -> Any:
        code = self._b3_gate()
        if code is not None:
            raise SyntheticRefused(code)
        if getattr(self, "state", None) is SchedulerState.RUNNING:
            task = asyncio.get_running_loop().create_task(self._manual_tick())  # type: ignore[attr-defined]
            self._b3_manual.add(task)
            try:
                return await task
            finally:
                self._b3_manual.discard(task)
        return await super().tick()  # type: ignore[misc]

    async def stop(self) -> None:
        manual = {t for t in self._b3_manual if not t.done()}
        if manual:
            _done, pending = await asyncio.wait(manual, timeout=self._b3_bound / 4)
            for task in pending:
                task.cancel()
            if pending:
                _done, still = await asyncio.wait(pending, timeout=self._b3_bound / 4)
                if still:
                    self._state = SchedulerState.STOP_FAILED
                    raise self._b3_stop_failed()
        await super().stop()  # type: ignore[misc]

    def _b3_stop_failed(self) -> Exception:
        raise NotImplementedError


class ManagedDueScheduler(_Managed, DueWindowScheduler):
    def _b3_stop_failed(self) -> Exception:
        from haloflow.m05.due_scheduler import SchedulerStopFailed

        return SchedulerStopFailed()


class ManagedReminderScheduler(_Managed, ReminderScheduler):
    def _b3_stop_failed(self) -> Exception:
        from haloflow.m05.reminder_scheduler import ReminderSchedulerStopFailed

        return ReminderSchedulerStopFailed()


class ManagedWritebackScheduler(_Managed, WritebackScheduler):
    def _b3_stop_failed(self) -> Exception:
        from haloflow.m05.writeback_scheduler import WritebackSchedulerStopFailed

        return WritebackSchedulerStopFailed()


# ---------------------------------------------------------------------------- build

def _context_for(rt: Any, tenant_id: str, principal_id: str,
                 caps: frozenset[str]) -> Callable[[str], Awaitable[Any]]:
    async def contexts(tenant: str) -> Any:
        if tenant != tenant_id:
            raise SyntheticRefused("TARGET_MISMATCH")
        return await rt.resolver.resolve(
            principal=Principal(kind=PrincipalKind.WORKLOAD, id=principal_id,
                                auth_method="worker",
                                authorized_tenant_ids=frozenset({tenant}),
                                capabilities=caps),
            tenant_hint=tenant, purpose="operations", capabilities=caps,
            source=TrustedSource.WORKER, execution_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

    return contexts


@dataclass
class Wiring:
    sender: Any
    port: SimulatedEhrPort
    schedulers: dict[str, Any]


def build(rt: Any, tenant_id: str, *, sender: Any, ehr: Any,
          stop_timeouts: Mapping[str, float], gate: Gate) -> Wiring:
    """Construct (never start) the port and the three schedulers on the tenant runtime."""

    tenants = (tenant_id,)
    port = SimulatedEhrPort(ehr)
    svc = _ReminderService(EventFoundation(rt.gateway, producer_service=PRODUCER))
    inner = _OutboundWorker(svc, sender, render_inputs)
    wb = _WritebackService(EventFoundation(rt.gateway, producer_service=PRODUCER))
    runner = ReminderRunner(svc, BaselineWorker(inner, wb.capture_baseline), batch=10,
                            lease_seconds=120)

    def managed(cls: Any, name: str, *args: Any, **kwargs: Any) -> Any:
        bound = float(stop_timeouts[name])
        scheduler = cls.__new__(cls)
        scheduler._b3_init(gate, bound, INTERVALS[name])
        cls.__init__(scheduler, *args, interval_s=INTERVALS[name], stop_timeout_s=bound / 2,
                     clock=time.monotonic, sleep=scheduler._b3_sleep, **kwargs)
        return scheduler

    reminder = managed(ManagedReminderScheduler, "reminder",
                       contexts=_context_for(rt, tenant_id, W_DISPATCH,
                                             frozenset(CAPTURE_CAPABILITIES)),
                       service=svc, worker=inner, runner=runner, tenants=tenants,
                       recovery_limit=100)
    due_runner = DueWindowRunner(
        gateway=rt.gateway, resolver=rt.resolver,
        intake=IntakeService(EventFoundation(rt.gateway, producer_service=PRODUCER)),
        principal=Principal(kind=PrincipalKind.WORKLOAD, id=W_TIMER, auth_method="worker",
                            authorized_tenant_ids=frozenset(tenants), capabilities=TIMER_CAPS),
        page_limit=10, attempt_cap=50)
    due = managed(ManagedDueScheduler, "due", due_runner, tenants)
    writeback = managed(ManagedWritebackScheduler, "writeback",
                        contexts=_context_for(rt, tenant_id, W_DISPATCH, frozenset(CAPABILITIES)),
                        service=wb,
                        worker=WritebackWorker(wb, port, write_timeout_s=WRITE_TIMEOUT_S),
                        tenants=tenants, batch_limit=100)
    return Wiring(sender=sender, port=port,
                  schedulers={"due": due, "reminder": reminder, "writeback": writeback})


# ---------------------------------------------------------------------------- B3b services

M05_PRODUCER: Final = "m05_followup"     # the production staff-service producer (startup.py)
INGRESS_PRODUCER: Final = PRODUCER       # the S-G1a intake producer ("m05_intake")


@dataclass(frozen=True)
class SyntheticServices:
    """What the B3b staff routes and simulator call (R-B3b-2/6): the composed resolver, the
    M01 staff service (self standing), the M05 staff service (StaffServiceV3, with the install's
    note keys), the tenant runtime's gateway, and the fixed tenant and staff identities."""

    resolver: Any
    staff: Any
    m05: Any
    gateway: Any
    tenant_id: str
    staff_id: str


def build_services(rt: Any, note_keys: Any, state: Mapping[str, Any]) -> SyntheticServices:
    from haloflow.m01.staff.service import StaffService
    from haloflow.m05.writeback import StaffServiceV3

    m05 = StaffServiceV3(rt.gateway, keys=note_keys,
                         foundation=EventFoundation(rt.gateway, producer_service=M05_PRODUCER))
    return SyntheticServices(resolver=rt.resolver, staff=StaffService(rt.gateway), m05=m05,
                             gateway=rt.gateway, tenant_id=str(state["tenant_id"]),
                             staff_id=str(state["staff_id"]))


def load_foundation(gateway: Any) -> EventFoundation:
    """The B5 scenario load's transaction foundation (`sim_actions.load`): the S-G1a intake
    producer; the load appends no observation, it only runs its statements in one
    transaction."""
    return EventFoundation(gateway, producer_service=INGRESS_PRODUCER)


def reply_intake(gateway: Any) -> Any:
    """The S-G1a intake chain (ReplyIntake over IntakeService), as the accepted harness builds
    it, for the simulator's unmounted ingress app."""
    from haloflow.m05.reply_classifier import ReplyIntake

    return ReplyIntake(IntakeService(EventFoundation(gateway,
                                                     producer_service=INGRESS_PRODUCER)))
