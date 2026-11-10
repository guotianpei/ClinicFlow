"""L-1S package S-J: the TEST-SIDE combined synthetic lifespan (NOT installable).

This module is test-harness evidence only. It is NOT an installable application, NOT
coverage of the production lifespan (`startup.py` / `platform.app` are untouched and still
compose none of this), and NOT acceptance of production staff authentication: the staff
routes below admit a synthetic test principal named in a request header. L-1S stays open.
Option B (an installable synthetic app) is a separate owner decision and is not implied.

Governing: S-J test-case preparation v1 and the Codex review pins record (Work Session
2026-11-10; Codex 20261110T202552Z, 20261110T210601Z).

One `Stack` holds, on ONE S-I TEST runtime (registry target 94) that the harness opens and
closes itself:
- the H3 `ReminderScheduler` (runner worker = S-I `BaselineWorker`, FakeSender only);
- the S-E `DueWindowScheduler` (the S-B due-window runner);
- the proposed S-J `WritebackScheduler` (`WritebackService` + `WritebackWorker` against a
  `RecordingPort` over `SimulatedEhr`; never a real EHR);
- the UNCHANGED S-G1a ingress router (`sh3.ingress_app`);
- a TEST-ONLY staff router bound to `StaffServiceV3` (get, reason, claim, resolve).

Shutdown order (Codex pin 4): stop every producer (write-back, reminder, due-window), and
only when all three are STOPPED close the shared resources (the EHR port, then the pool).
If any stop raises a StopFailed, NOTHING is closed: the unfinished task keeps the resources
it may still use; a later `shutdown()` retries. The oracle is independent of that
orchestration: `RecordingPort.close` and the pool closer record every scheduler's state
and every live task at the moment they run.

The S-J scheduler is imported lazily: until `haloflow.m05.writeback_scheduler` exists,
building a stack raises ModuleNotFoundError (the expected red cause of the J rows).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import sa_support as sa
import sb_runtime as sbr
import service_support as sv
import sh3_support as sh3
import si_support as si
from fastapi import Request  # module level: FastAPI resolves the endpoint annotations

BOUND_S: Final = 30.0
STAFF_ROUTE: Final = "/sj-test/staff/{tenant_id}/items/{item_id}"
ACTOR_HEADER: Final = "x-sj-test-actor"
ACTOR_TENANT_HEADER: Final = "x-sj-test-actor-tenant"
CAPS_HEADER: Final = "x-sj-test-caps"
CAPS: Final = {"read": sv.READ_ONLY_CAPS, "resolve": sv.RESOLVE_CAPS, "none": frozenset()}
PRODUCERS: Final = ("writeback", "reminder", "due")


# --- the independent oracle -------------------------------------------------------------------


@dataclass
class Recorder:
    """What really happened, recorded by the resources themselves (never by the stack's
    orchestration code): each close with every scheduler's state and live task count."""

    events: list[tuple[str, dict[str, str], int]] = field(default_factory=list)
    misuse: list[str] = field(default_factory=list)
    schedulers: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, str]:
        return {name: s.state.name for name, s in self.schedulers.items()}

    def live_tasks(self) -> int:
        """Unfinished SCHEDULER tasks (loop or manual tick), by coroutine qualname; the
        pool's own worker tasks are not scheduler work and are not counted."""

        owners = ("WritebackScheduler.", "ReminderScheduler.", "DueWindowScheduler.")
        live = 0
        for task in asyncio.all_tasks():
            name = getattr(task.get_coro(), "__qualname__", "")
            if not task.done() and name.startswith(owners):
                live += 1
        return live

    def close(self, what: str) -> None:
        self.events.append((what, self.snapshot(), self.live_tasks()))


class RecordingPort:
    """The EHR port the write-back worker uses: forwards to a SimulatedEhr; any use after
    close is recorded as misuse and refused."""

    def __init__(self, ehr: Any, recorder: Recorder) -> None:
        self.ehr = ehr
        self.recorder = recorder
        self.closed = False
        self.hold: asyncio.Event | None = None  # when set: write waits until released
        self.ignore_cancel = False  # the held write ignores cancellation (STOP_FAILED rows)
        self.hang_after_write = False  # apply the write, then never return (crash rows)
        self.entered = asyncio.Event()
        self.applied: list[Any] = []

    def _use(self, what: str) -> None:
        if self.closed:
            self.recorder.misuse.append(what)
            raise RuntimeError("port closed")

    async def read(self, appointment_id: str) -> Any:
        self._use("read")
        return await self.ehr.read(appointment_id)

    async def close_permit(self, permit_id: str) -> None:
        self._use("close_permit")
        await self.ehr.close_permit(permit_id)

    async def write_confirmation(self, appointment_id: str, **kw: Any) -> Any:
        self._use("write")
        self.entered.set()
        if self.hold is not None:
            while not self.hold.is_set():
                try:
                    await self.hold.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
        result = await self.ehr.write_confirmation(appointment_id, **kw)
        self.applied.append(result)
        if self.hang_after_write:
            await asyncio.Event().wait()  # the crash window: applied, never acknowledged
        return result

    def close(self) -> None:
        self.recorder.close("ehr")
        self.closed = True


class ParkingSleep:
    """A loop `sleep`: the first `free` calls return at once; every later call parks until
    cancelled. Each loop therefore runs exactly its first tick(s), then parks."""

    def __init__(self, free: int = 1) -> None:
        self.free = free
        self.calls = 0
        self.parked = asyncio.Event()

    async def __call__(self, _seconds: float) -> None:
        self.calls += 1
        if self.calls <= self.free:
            await asyncio.sleep(0)
            return
        self.parked.set()
        await asyncio.Event().wait()


# --- the runtime (opened and closed by the harness, so a failed stop can keep it) -----------


class Runtime:
    def __init__(self, install: si.SIInstall, recorder: Recorder, *,
                 policy: str = "ENFORCE") -> None:
        self.install, self.recorder, self.policy = install, recorder, policy
        self.pool: Any = None
        self.rt: Any = None

    async def open(self) -> Any:
        from haloflow.m01.activation import ActivationPolicy
        from haloflow.m01.control_store import PsycopgControlStore
        from haloflow.m01.pool import TenantPool
        from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

        self.pool = TenantPool(self.install.ids.logins[sa.RUNTIME], min_size=1, max_size=6)
        await self.pool.open()
        self.rt = compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=self.pool, control_store=PsycopgControlStore(self.pool),
                migrator_connect=sv._factory(self.install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(self.install.ids.logins["haloflow_provisioner"])),
            registry=si.si_registry(), catalog=si.si_catalog(),
            supported_schema_versions=frozenset({si.SI_TARGET}),
            activation_policy=ActivationPolicy[self.policy])
        return self.rt

    async def close(self) -> None:
        if self.pool is not None:
            self.recorder.close("pool")
            await self.pool.close()
            self.pool = None


# --- the stack --------------------------------------------------------------------------------


@dataclass
class Stack:
    install: si.SIInstall
    runtime: Runtime
    rt: Any
    port: RecordingPort
    sender: Any
    recorder: Recorder
    due: Any
    reminder: Any
    writeback: Any
    sleeps: dict[str, ParkingSleep]
    closed: bool = False

    def schedulers(self) -> dict[str, Any]:
        return {"writeback": self.writeback, "reminder": self.reminder, "due": self.due}

    async def tick_all(self) -> dict[str, Any]:
        """Manual, deterministic: one tick of each lifecycle, in data-flow order."""

        out = {"due": await asyncio.wait_for(self.due.tick(), BOUND_S),
               "reminder": await asyncio.wait_for(self.reminder.tick(), BOUND_S)}
        out["writeback"] = await asyncio.wait_for(self.writeback.tick(), BOUND_S)
        return out

    async def tick_writeback(self) -> Any:
        return await asyncio.wait_for(self.writeback.tick(), BOUND_S)

    async def tick_reminder(self) -> Any:
        return await asyncio.wait_for(self.reminder.tick(), BOUND_S)

    def start_all(self) -> None:
        for name in reversed(PRODUCERS):  # due, reminder, writeback
            self.schedulers()[name].start()

    async def shutdown(self) -> dict[str, str]:
        """Stop producers; close shared resources only if every stop succeeded."""

        failed: dict[str, str] = {}
        for name in PRODUCERS:
            try:
                await asyncio.wait_for(self.schedulers()[name].stop(), BOUND_S)
            except Exception as error:  # noqa: BLE001 - every StopFailed class
                failed[name] = type(error).__name__
        if failed:
            return failed
        if not self.closed:
            self.port.close()
            await self.runtime.close()
            self.closed = True
        return {}


async def build(install: si.SIInstall, ehr: Any, *, recorder: Recorder | None = None,
                policy: str = "ENFORCE", parked: bool = False, wb_stop_timeout_s: float = 2,
                wb_contexts: Callable[[str], Awaitable[Any]] | None = None,
                write_timeout_s: float = si.WRITE_TIMEOUT_S,
                wb_service: Callable[[Any], Any] | None = None) -> Stack:
    import time

    from haloflow.m05.due_scheduler import DueWindowScheduler
    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.reminder import ReminderRunner
    from haloflow.m05.reminder_scheduler import ReminderScheduler
    from haloflow.m05.writeback import BaselineWorker, WritebackWorker
    from haloflow.m05.writeback_scheduler import WritebackScheduler

    recorder = recorder or Recorder()
    runtime = Runtime(install, recorder, policy=policy)
    rt = await runtime.open()
    port = RecordingPort(ehr, recorder)
    sender = FakeSender()
    sleeps = {name: ParkingSleep() if parked else ParkingSleep(free=10**9)
              for name in PRODUCERS}
    svc = sh3.service(rt)
    inner = sh3.worker(svc, sender)
    capture = si.wb_service(rt)
    service = (wb_service or si.wb_service)(rt)
    runner = ReminderRunner(svc, BaselineWorker(inner, capture.capture_baseline),
                            batch=10, lease_seconds=120)
    reminder = ReminderScheduler(contexts=sh3.dispatch_contexts(rt, [install]), service=svc,
                                 worker=inner, runner=runner, tenants=(install.tenant_id,),
                                 interval_s=300, stop_timeout_s=2, recovery_limit=100,
                                 clock=time.monotonic, sleep=sleeps["reminder"])
    due_runner, _gate = sbr.runner(install, rt, page=10, cap=50)  # type: ignore[arg-type]
    due = DueWindowScheduler(due_runner, (install.tenant_id,), interval_s=300,
                             stop_timeout_s=2, clock=time.monotonic, sleep=sleeps["due"])

    async def wb_context(_tenant: str) -> Any:
        return await si.wb_context(rt, install)

    writeback = WritebackScheduler(
        contexts=wb_contexts or wb_context, service=service,
        worker=WritebackWorker(service, port, write_timeout_s=write_timeout_s),
        tenants=(install.tenant_id,), interval_s=60, stop_timeout_s=wb_stop_timeout_s,
        batch_limit=100, clock=time.monotonic, sleep=sleeps["writeback"])
    stack = Stack(install, runtime, rt, port, sender, recorder, due, reminder, writeback,
                  sleeps)
    recorder.schedulers = stack.schedulers()
    return stack


# --- synthetic flow helpers (the E01-E06 path, on the stack) ------------------------------


SECRET: Final = "d" * 32 + "-sj-ingress-A"


async def ingest(stack: Stack, **identity: Any) -> dict[str, str]:
    import sh2_support as sh2

    install, rt = stack.install, stack.rt
    zone, start = sh2.in_window(install)
    patient, phone, ref = sa.new_id(), sa.fresh_phone(), sa.new_id()
    contact = await sh2.gateway_directory(rt, install, "contact_put", phone=phone,
                                          patient_id=patient)
    assert contact["outcome"] in ("applied", "existing"), contact
    result = await sh2.gateway_directory(rt, install, "appointment_put",
                                         **si.appointment3(ref, patient, start,
                                                           department_tz=zone, **identity))
    assert result["outcome"] == "applied", result
    return {"patient": patient, "phone": phone, "ref": ref}


async def reply(stack: Stack, flow: dict[str, str], text: str,
                secret: str = SECRET) -> tuple[int, dict[str, Any]]:
    app = sh3.ingress_app(stack.rt, {stack.install.tenant_id: SECRET})
    return await sh3.post_reply(app, secret, flow["phone"], text, sa.new_id())


def ehr_for(install: si.SIInstall, flow: dict[str, str], **over: Any) -> Any:
    return si.ehr_from_flow(install, flow, si.db_clock(install), **over)


# --- the TEST-ONLY staff router (synthetic principal; not production authentication) -------


def staff_app(install: si.SIInstall, rt: Any) -> Any:
    from fastapi import APIRouter, FastAPI
    from fastapi.responses import JSONResponse

    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.m01.staff.service import (
        ReadFailed,
        ReadRefused,
        RouteDenied,
    )
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.service import (
        ActionRefused,
        CommandMalformed,
        ResolveAction,
        StaffAction,
    )
    from haloflow.m05.writeback import StaffServiceV3

    staff = StaffServiceV3(rt.gateway,
                           foundation=EventFoundation(rt.gateway, producer_service=sv.PRODUCER),
                           keys=sv.TestNoteKeys())

    async def actor(request: Request, tenant_id: str) -> Any:
        principal_id = request.headers.get(ACTOR_HEADER, "")
        authorized = request.headers.get(ACTOR_TENANT_HEADER, tenant_id)
        caps = CAPS[request.headers.get(CAPS_HEADER, "resolve")]
        return await rt.resolver.resolve(
            principal=Principal(kind=PrincipalKind.ACTOR, id=principal_id, auth_method="test",
                                authorized_tenant_ids=frozenset({authorized}),
                                capabilities=caps),
            tenant_hint=tenant_id, purpose="operations", capabilities=caps,
            source=TrustedSource.HTTP, execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

    def refused(status: int, code: str) -> Any:
        return JSONResponse({"refused": code}, status_code=status)

    async def guarded(request: Request, tenant_id: str, call: Any) -> Any:
        if tenant_id != install.tenant_id:
            return refused(404, "TENANT_UNKNOWN")
        try:
            context = await actor(request, tenant_id)
            return JSONResponse(json.loads(json.dumps(await call(context), default=str)))
        except (RouteDenied, ReadRefused, ActionRefused) as error:
            return refused(403, type(error).__name__)
        except ReadFailed as error:
            code = str(getattr(error, "reason_code", "READ_FAILED"))
            return refused(404 if code == "ITEM_NOT_FOUND" else 409, code)
        except CommandMalformed:
            return refused(422, "MALFORMED")
        except Exception as error:  # noqa: BLE001 - class name only (privacy)
            return refused(403 if "Denied" in type(error).__name__ else 500,
                           type(error).__name__)

    router = APIRouter()

    @router.get(STAFF_ROUTE)
    async def get(request: Request, tenant_id: str, item_id: uuid.UUID) -> Any:
        return await guarded(request, tenant_id, lambda c: staff.get(c, item_id))

    @router.get(STAFF_ROUTE + "/reason")
    async def reason(request: Request, tenant_id: str, item_id: uuid.UUID) -> Any:
        return await guarded(request, tenant_id, lambda c: staff.reason(c, item_id))

    @router.post(STAFF_ROUTE + "/claim")
    async def claim(request: Request, tenant_id: str, item_id: uuid.UUID) -> Any:
        body = json.loads(await request.body() or b"{}")

        async def call(c: Any) -> Any:
            result = await staff.act(c, StaffAction(
                item_id=item_id, client_action_id=uuid.uuid4(),
                expected_version=int(body.get("version", 1)), type="claim"))
            return {"outcome": result.outcome}

        return await guarded(request, tenant_id, call)

    @router.post(STAFF_ROUTE + "/resolve")
    async def resolve(request: Request, tenant_id: str, item_id: uuid.UUID) -> Any:
        body = json.loads(await request.body() or b"{}")

        async def call(c: Any) -> Any:
            result = await staff.act(c, ResolveAction(
                item_id=item_id, client_action_id=uuid.uuid4(),
                expected_version=int(body.get("version", 2)), type="resolve",
                outcome_code=str(body.get("outcome", "")), note=None))
            return {"outcome": result.outcome}

        return await guarded(request, tenant_id, call)

    app = FastAPI()
    app.include_router(router)
    return app


async def http(app: Any, method: str, path: str, *, headers: dict[str, str] | None = None,
               body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """One in-process ASGI request (the sh3.post_reply pattern; the S-A guard blocks
    httpx sends)."""

    raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
    hdrs = [(k.encode("ascii"), v.encode("ascii")) for k, v in (headers or {}).items()]
    hdrs += [(b"content-type", b"application/json"),
             (b"content-length", str(len(raw_body)).encode("ascii"))]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode("ascii"), "root_path": "", "query_string": b"",
             "headers": hdrs, "client": ("synthetic", 1), "server": ("synthetic", 80)}
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), BOUND_S)
    status = next(int(m["status"]) for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, (json.loads(raw) if raw else {})


def staff_headers(principal: str | None = None, *, caps: str = "resolve",
                  authorized_tenant: str | None = None) -> dict[str, str]:
    import sd_support as sd

    headers = {ACTOR_HEADER: principal or sd.STAFF, CAPS_HEADER: caps}
    if authorized_tenant is not None:
        headers[ACTOR_TENANT_HEADER] = authorized_tenant
    return headers


def item_path(install: si.SIInstall, item_id: str, suffix: str = "",
              tenant: str | None = None) -> str:
    return STAFF_ROUTE.format(tenant_id=tenant or install.tenant_id, item_id=item_id) + suffix


# --- a check item WITHOUT the S-J scheduler (accepted-candidate S-I path, E02 shape) -----


async def check_item_without_scheduler(install: si.SIInstall, rt: Any) -> dict[str, Any]:
    """Directory -> one H3 tick -> reply 1 -> WritebackService discover/due ->
    WritebackWorker.run_one against an EHR showing another provider -> status_changed item.
    Used by the staff-route control rows (J11, J13-J15), which must not depend on S-J."""

    import time

    import sh2_support as sh2

    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.reminder import ReminderRunner
    from haloflow.m05.reminder_scheduler import ReminderScheduler
    from haloflow.m05.writeback import BaselineWorker, WritebackWorker

    zone, start = sh2.in_window(install)
    patient, phone, ref = sa.new_id(), sa.fresh_phone(), sa.new_id()
    await sh2.gateway_directory(rt, install, "contact_put", phone=phone, patient_id=patient)
    put = await sh2.gateway_directory(rt, install, "appointment_put",
                                      **si.appointment3(ref, patient, start,
                                                        department_tz=zone))
    assert put["outcome"] == "applied", put
    sender = FakeSender()
    svc = sh3.service(rt)
    inner = sh3.worker(svc, sender)
    wb = si.wb_service(rt)
    scheduler = ReminderScheduler(
        contexts=sh3.dispatch_contexts(rt, [install]), service=svc, worker=inner,
        runner=ReminderRunner(svc, BaselineWorker(inner, wb.capture_baseline), batch=10,
                              lease_seconds=120),
        tenants=(install.tenant_id,), interval_s=300, stop_timeout_s=2, recovery_limit=100,
        clock=time.monotonic)
    try:
        await asyncio.wait_for(scheduler.tick(), BOUND_S)
    finally:
        await asyncio.wait_for(scheduler.stop(), BOUND_S)
    flow = {"patient": patient, "phone": phone, "ref": ref}
    app = sh3.ingress_app(rt, {install.tenant_id: SECRET})
    status, body = await sh3.post_reply(app, SECRET, phone, "1", sa.new_id())
    assert (status, body.get("outcome")) == (202, "registered"), body
    ctx = await si.wb_context(rt, install)
    await wb.discover(ctx, 100)
    (row,) = await wb.due(ctx, 100)
    ehr = ehr_for(install, flow, provider_ref="prov-other")
    outcome = await WritebackWorker(wb, ehr).run_one(await si.wb_context(rt, install),
                                                     row["wb_op_id"])
    assert outcome == "item_raised", outcome
    (item,) = si.check_items(install)
    return {**flow, "item_id": str(item["item_id"]), "wb_op_id": row["wb_op_id"],
            "source_event_id": str(item["source_event_id"])}
