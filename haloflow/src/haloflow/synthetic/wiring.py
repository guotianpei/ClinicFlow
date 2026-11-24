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

from psycopg.types.json import Jsonb

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m02.observation import EventFoundation, TransactionBinding
from haloflow.m05.due_scheduler import DueWindowScheduler, SchedulerState
from haloflow.m05.due_statements import DUE_KEY
from haloflow.m05.due_windows import DueWindowRunner
from haloflow.m05.eligibility import CAPABILITIES as EL_WORKER_CAPABILITIES
from haloflow.m05.eligibility import EligibilityService, EligibilityWorker
from haloflow.m05.eligibility_cases import CASE_CAPABILITIES, EligibilityCaseService
from haloflow.m05.eligibility_doc import COUNT_KEYS as DOC_KEYS
from haloflow.m05.eligibility_doc import DOC_CAPABILITIES, DocService
from haloflow.m05.eligibility_scheduler import (
    APPLY_KEYS,
    NOT_RUN,
    RUN_KEYS,
    EligibilityScheduler,
    EligibilitySchedulerStopFailed,
    PhaseReport,
    TickResult,
)
from haloflow.m05.eligibility_statements import TICK_CAPABILITY, TICK_KEY
from haloflow.m05.intake import IntakeService, _attribution
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
INTERVALS: Final = {"due": 300, "reminder": 300, "writeback": 60, "eligibility": 300}
EL_CALL_TIMEOUT_S: Final = 10.0
E1_KEYS: Final = ("raised", "replanned", "deferred_no_zone")
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


class E1TickService:
    """The E1 not-received / zone re-plan tick (`m05.eligibility.tick`, t100) through the
    gateway as the timer workload (D-E4-8: nothing in src called it before E4)."""

    def __init__(self, foundation: EventFoundation) -> None:
        self._foundation = foundation

    async def tick(self, context: Any) -> dict[str, int]:
        async def body(binding: TransactionBinding) -> dict[str, int]:
            payload = {"schema_version": 1, "attribution": _attribution(context)}
            rows = await binding.handle.fetch_all(TICK_KEY, {"command": Jsonb(payload)})
            if len(rows) != 1 or not isinstance(rows[0][0], Mapping) \
                    or rows[0][0].get("outcome") != "applied":
                raise RuntimeError("RESULT_INVALID")
            row = rows[0][0]
            out = {k: row.get(k) for k in E1_KEYS}
            if not all(type(v) is int and v >= 0 for v in out.values()):
                raise RuntimeError("RESULT_INVALID")
            return {k: int(v) for k, v in out.items() if v is not None}

        result: dict[str, int] = await self._foundation.run_in_transaction(context, body)
        return result


@dataclass(frozen=True)
class EligibilityTick:
    """One managed eligibility tick: the scheduler's result plus the flattened counts the
    simulator tick body reports (el4_support pin): tenant codes, `run.<k>`,
    `expire.expired`, `apply.<k>`, `e1.<k>` and `<phase>.status.<STATUS>` per tenant."""

    result: TickResult
    e1: Mapping[str, int]
    e1_status: str
    documented: bool = False

    @property
    def codes(self) -> Mapping[str, int]:
        return self.result.codes

    @property
    def flat_counts(self) -> dict[str, int]:
        out: dict[str, int] = dict(self.result.codes)
        tenants = self.result.tenants
        for key in RUN_KEYS:
            out[f"run.{key}"] = sum(t.run.counts.get(key, 0) for t in tenants)
        out["expire.expired"] = sum(t.expire.counts.get("expired", 0) for t in tenants)
        for key in APPLY_KEYS:
            out[f"apply.{key}"] = sum(t.apply.counts.get(key, 0) for t in tenants)
        for key in E1_KEYS:
            out[f"e1.{key}"] = int(self.e1.get(key, 0))
        if self.documented:                   # E5: the document phase (eligibility profile)
            for key in DOC_KEYS:
                out[f"document.{key}"] = sum(_document(t).counts.get(key, 0) for t in tenants)
        statuses: dict[str, int] = {}
        phases = ("run", "expire", "apply", "document") if self.documented \
            else ("run", "expire", "apply")
        for report in tenants:
            for phase in phases:
                part = _document(report) if phase == "document" else getattr(report, phase)
                name = f"{phase}.status.{part.status}"
                statuses[name] = statuses.get(name, 0) + 1
        statuses[f"e1.status.{self.e1_status}"] = 1
        out.update(statuses)
        return out


def _document(report: Any) -> Any:
    """E5: the document phase of a tenant report (NOT_RUN when the tenant never reached it)."""
    return getattr(report, "document", None) or _DOC_NOT_RUN


_DOC_NOT_RUN: Final = PhaseReport(NOT_RUN, dict.fromkeys(DOC_KEYS, 0))


class ManagedEligibilityScheduler(_Managed, EligibilityScheduler):
    """The E2/E3a eligibility scheduler under the B3a lifecycle rules, with the E1 tick run
    first in every tick (D-E4-8)."""

    _e1: E1TickService
    _e1_contexts: Callable[[str], Awaitable[Any]]

    def _b3_stop_failed(self) -> Exception:
        return EligibilitySchedulerStopFailed()

    def _log(self, result: Any, *, skipped: int) -> None:
        inner = result.result if isinstance(result, EligibilityTick) else result
        EligibilityScheduler._log(self, inner, skipped=skipped)

    async def _run_tick(self, *args: Any, **kwargs: Any) -> Any:
        async with self._b3_lock:
            e1: dict[str, int] = {}
            status = "ok"
            for tenant in self._tenants:
                try:
                    got = await self._e1.tick(await self._e1_contexts(tenant))
                except Exception as error:  # noqa: BLE001 - class name only, reported below
                    status = type(error).__name__ if isinstance(error, SyntheticRefused) \
                        else "FAILED"
                    continue
                for key, value in got.items():
                    e1[key] = e1.get(key, 0) + value
            result = await EligibilityScheduler._run_tick(self, *args, **kwargs)
            return EligibilityTick(result, e1, status, self._doc is not None)


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


def build_doc_service(rt: Any, tenant_id: str, chart: Any) -> DocService:
    """E5: the doc service over the SIMULATED chart for the install's one tenant."""
    return DocService(EventFoundation(rt.gateway, producer_service=PRODUCER), chart,
                      (tenant_id,))


def doc_contexts(rt: Any, tenant_id: str) -> Callable[[str], Awaitable[Any]]:
    """E5: the doc service's timer-workload context (capability exactly DOC_CAPABILITIES)."""
    return _context_for(rt, tenant_id, W_TIMER, frozenset(DOC_CAPABILITIES))


def build_eligibility(rt: Any, tenant_id: str, *, clearinghouse: Any, stop_timeout: float,
                      gate: Gate, doc: DocService | None = None) -> ManagedEligibilityScheduler:
    """The eligibility profile's fourth managed scheduler (D-E4-3): E2 worker over the
    SYNTHETIC clearinghouse port, the E3a case applier, the E1 tick (D-E4-8) and (E5, when
    `doc` is given) the document phase over the SIMULATED chart."""

    foundation = EventFoundation(rt.gateway, producer_service=PRODUCER)
    service = EligibilityService(foundation)
    cases = EligibilityCaseService(EventFoundation(rt.gateway, producer_service=PRODUCER))
    worker = EligibilityWorker(service, clearinghouse, call_timeout_s=EL_CALL_TIMEOUT_S)
    cls = ManagedEligibilityScheduler
    scheduler = cls.__new__(cls)
    scheduler._b3_init(gate, float(stop_timeout), INTERVALS["eligibility"])
    cls.__init__(scheduler, contexts=_context_for(rt, tenant_id, W_TIMER,
                                                  frozenset(EL_WORKER_CAPABILITIES)),
                 service=service, worker=worker, tenants=(tenant_id,),
                 interval_s=INTERVALS["eligibility"], stop_timeout_s=float(stop_timeout) / 2,
                 clock=time.monotonic, sleep=scheduler._b3_sleep, cases=cases,
                 case_contexts=_context_for(rt, tenant_id, W_TIMER,
                                            frozenset(CASE_CAPABILITIES)),
                 doc=doc, doc_contexts=None if doc is None else doc_contexts(rt, tenant_id))
    scheduler._e1 = E1TickService(EventFoundation(rt.gateway, producer_service=PRODUCER))
    scheduler._e1_contexts = _context_for(rt, tenant_id, W_TIMER, frozenset({TICK_CAPABILITY}))
    return scheduler


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


def _synthetic_staff_v4() -> type:
    """E4: StaffServiceV4 whose item, list and history reads also accept the three
    eligibility kinds and the E3b `log_contact` history entry (the V3 shadow technique: the
    accepted validators run on a copy with the kind / action replaced by an accepted one).
    Synthetic only; src/haloflow/m05 is unchanged."""
    from typing import cast as _cast
    from uuid import UUID

    from haloflow.m01.staff import service as m01s
    from haloflow.m05 import service as staff
    from haloflow.m05.eligibility_staff import EL_KINDS, StaffServiceV4
    from haloflow.m05.writeback import ITEM_KINDS_V3, _bind_item_v3, _bind_list_v3

    kinds = frozenset(ITEM_KINDS_V3) | frozenset(EL_KINDS)
    shadow_kind = next(iter(sorted(ITEM_KINDS_V3)))

    def shadow(view: Any) -> Any:
        if type(view) is dict and view.get("item_kind") in kinds:
            return {**view, "item_kind": shadow_kind}
        return view

    def bind_item(rows: list[Any], item_id: UUID) -> dict[str, Any]:
        from haloflow.m01.staff import service as m01staff

        view = m01staff.single_json(rows)
        _bind_item_v3([(shadow(view),)], item_id)
        return _cast("dict[str, Any]", view)

    def bind_list(rows: list[Any], query: Any) -> dict[str, Any]:
        from haloflow.m01.staff import service as m01staff

        value = m01staff.single_json(rows)
        if type(value) is dict and type(value.get("items")) is list:
            _bind_list_v3([({**value, "items": [shadow(v) for v in value["items"]]},)], query)
            return _cast("dict[str, Any]", value)
        raise staff._read_invalid()

    def bind_history(rows: list[Any], item_id: UUID) -> dict[str, Any]:
        from haloflow.m01.staff import service as m01staff

        value = m01staff.single_json(rows)
        if type(value) is dict and type(value.get("entries")) is list:
            entries = [{**e, "action": "start_progress"} if type(e) is dict
                       and e.get("action") == "log_contact" else e for e in value["entries"]]
            staff._bind_history([({**value, "entries": entries},)], item_id)
            return _cast("dict[str, Any]", value)
        raise staff._read_invalid()

    class SyntheticStaffServiceV4(StaffServiceV4):
        async def get(self, context: Any, item_id: UUID) -> Mapping[str, Any]:
            if not m01s.is_uuid(item_id):
                raise staff.CommandMalformed("item_id")
            return await self._read(context, staff.K_GET, {"item_id": item_id},
                                    lambda rows: bind_item(rows, item_id))

        async def list_items(self, context: Any, query: Any) -> Mapping[str, Any]:
            if type(query) is not staff.ListQuery:
                raise staff.CommandMalformed("query")
            return await self._read(context, staff.K_LIST, {"query": Jsonb(query.render())},
                                    lambda rows: bind_list(rows, query))

        async def history(self, context: Any, item_id: UUID) -> Mapping[str, Any]:
            if not m01s.is_uuid(item_id):
                raise staff.CommandMalformed("item_id")
            return await self._read(context, staff.K_HISTORY, {"item_id": item_id},
                                    lambda rows: bind_history(rows, item_id))

    return SyntheticStaffServiceV4


def build_services(rt: Any, note_keys: Any, state: Mapping[str, Any], *,
                   profile: str = "default") -> SyntheticServices:
    from haloflow.m01.staff.service import StaffService
    from haloflow.m05.writeback import StaffServiceV3

    # E4: the eligibility profile serves the E3b v4 staff service (case view, contact,
    # per-kind outcomes); the default profile keeps StaffServiceV3.
    cls = _synthetic_staff_v4() if profile == "eligibility" else StaffServiceV3
    m05 = cls(rt.gateway, keys=note_keys,
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
