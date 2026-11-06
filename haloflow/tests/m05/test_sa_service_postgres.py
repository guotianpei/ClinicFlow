"""L-1S package S-A — `haloflow.m05.intake.IntakeService` through the real M01 gateway.

Pins v1 section 6; architecture v1 section 1 (one transaction per call: gateway
statement + M02 append + t014 create) and v1r1 section 5 (exact capability sets,
workload principals only); requirements v1r1 R-SA-2, R-SA-3, R-SA-7, R-SA-8, R-SA-10.
Every row runs through `compose_tenant_runtime` with the S-A TEST registry (target
42) and the S-A TEST catalogue; production composition is not touched.

- SA-V01  before any I/O: an actor principal -> IntakeRefused PRINCIPAL_NOT_WORKLOAD;
          a capability set that is not EXACTLY the call's set (subset, superset)
          -> IntakeRefused CAPABILITIES_INVALID; the gateway is never entered.
- SA-V02  dispatch: one window + one `m05_reminder_dispatched` event (dedupe key
          dispatch:<reminder>, correlation = reminder, patient from the directory,
          workload = dispatch workload, occurred_at = stored sent_at); no item;
          retry -> the same event (existing), still one.
- SA-V03  inbound reschedule: one `m05_reminder_reply_received` event (metadata
          inbound_id, reply_class, triage_kind, appointment_ref, reminder_id) and one
          reschedule_request item; item -> source event -> appointment_ref (R-SA-7);
          redelivery -> one event, one item.
- SA-V04  confirm -> event, no item; STOP -> no event, no item; quarantined -> none.
- SA-V05  ambiguous -> unclear_reply item; metadata appointment_ref and reminder_id
          null; patient set.
- SA-V06  close -> `m05_reminder_no_reply` event (dedupe no_reply:<reminder>) + one
          no_reply item; rerun -> the same item, one each.
- SA-V07  atomicity: a fault injected after the registration statement, after the
          append, or at the create rolls back EVERYTHING (no inbound row, window
          unchanged, no event, no item); the rerun yields exactly one of each.
- SA-V08  lost acknowledgement: the gateway raises after COMMIT -> IntakeCommitUnknown;
          the rerun returns the stored result: one inbound row, one event, one item.
- SA-V09  no side effects (R-SA-10 behavioural half): SMS, fax, eligibility and EHR
          adapter methods are replaced by spies that FAIL on any call, and Python
          socket connects to non-loopback addresses are refused, for every call kind.
- SA-V10  binding (R-SA-8): the item's patient and the event's patient come from the
          directory/window, and the tenant from the context; nothing in the public
          commands can name either (SA-S08 pins the fields).
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sa_support as sa
import service_support as sv

pytestmark = pytest.mark.postgres


# --- harness -------------------------------------------------------------------------------------


@asynccontextmanager
async def _runtime(install: sa.SAInstall) -> AsyncIterator[Any]:
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=2)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=sa.sa_registry(),
            catalog=sa.sa_catalog(),
            supported_schema_versions=frozenset({sa.SA_TARGET}),
        )
    finally:
        await pool.close()


async def _context(rt: Any, install: sa.SAInstall, principal: str, call: str, *,
                   kind: str = "workload", capabilities: frozenset[str] | None = None) -> Any:
    return await sv.context(rt, install, principal,
                            sa.CAPS[call] if capabilities is None else capabilities, kind=kind)


class FaultGateway:
    """Wraps the real gateway. `fail_before`/`fail_after` raise around ONE statement
    key inside the transaction; `lose_ack` raises after the transaction committed."""

    def __init__(self, real: Any, *, fail_before: str | None = None,
                 fail_after: str | None = None, lose_ack: bool = False) -> None:
        self.real = real
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.lose_ack = lose_ack
        self.entered = 0

    async def with_tenant_transaction(self, context: Any, callback: Callable[[Any], Awaitable[Any]],
                                      **kwargs: Any) -> Any:
        self.entered += 1
        outer = self

        class Handle:
            def __init__(self, real: Any) -> None:
                self._real = real

            async def _run(self, method: str, key: str, params: Any) -> Any:
                if key == outer.fail_before:
                    raise RuntimeError(f"injected before {key}")
                result = await getattr(self._real, method)(key, params)
                if key == outer.fail_after:
                    raise RuntimeError(f"injected after {key}")
                return result

            async def fetch_one(self, key: str, params: Any = None) -> Any:
                return await self._run("fetch_one", key, params)

            async def fetch_all(self, key: str, params: Any = None) -> Any:
                return await self._run("fetch_all", key, params)

            async def execute(self, key: str, params: Any = None) -> Any:
                return await self._run("execute", key, params)

        async def wrapped(handle: Any) -> Any:
            return await callback(Handle(handle))

        result = await self.real.with_tenant_transaction(context, wrapped, **kwargs)
        if self.lose_ack:
            raise ConnectionError("acknowledgement lost after commit")
        return result


def _service(gateway: Any) -> Any:
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.intake import IntakeService

    return IntakeService(EventFoundation(gateway, producer_service=sa.PRODUCER))


def _run(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(coro_factory())


def _commands() -> Any:
    from haloflow.m05 import intake

    return intake


def _events(install: sa.SAInstall, event_type: str | None = None) -> list[dict[str, Any]]:
    clause = "" if event_type is None else " WHERE event_type = %s"
    rows = sa.admin(install, sa.q(install, "SELECT to_jsonb(e) FROM {s}.patient_events e"
                                           + clause + " ORDER BY received_at, event_id"),
                    None if event_type is None else (event_type,))
    return [r[0] for r in rows]


def _dispatched(install: sa.SAInstall) -> tuple[str, str, str, str]:
    """Directory + dispatch through SQL (fixture), returning patient, phone, ref,
    reminder. Service-level dispatch has its own row (SA-V02)."""

    install.require()
    patient, phone, (ref,) = sa.fresh_patient(install)
    reminder = sa.new_id()
    sa.dispatch(install, reminder, ref)
    return patient, phone, ref, reminder


def _inbound_command(phone: str, reply_class: str, message: str | None = None) -> Any:
    return _commands().InboundCommand(channel="sms", provider_message_id=message or sa.new_id(),
                                      phone=phone, reply_class=reply_class)


# --- SA-V01 --------------------------------------------------------------------------------------


CALLS = {
    "dispatch": (sa.W_DISPATCH, "register_dispatch"),
    "inbound": (sa.W_INGRESS, "register_inbound"),
    "close": (sa.W_TIMER, "close_window"),
}


def _argument(call: str) -> Any:
    intake = _commands()
    if call == "dispatch":
        return intake.DispatchCommand(reminder_id=uuid.uuid4(), appointment_ref=uuid.uuid4())
    if call == "inbound":
        return _inbound_command(sa.PHONE_1, "confirm")
    return uuid.uuid4()


@pytest.mark.parametrize("call", sorted(CALLS))
@pytest.mark.parametrize("case", ("actor", "subset", "superset"))
def test_sa_v01_refused_before_io(sa_install: sa.SAInstall, call: str, case: str) -> None:
    sa_install.require()
    principal, method = CALLS[call]

    async def body() -> None:
        from haloflow.m05.intake import IntakeRefused

        async with _runtime(sa_install) as rt:
            gateway = FaultGateway(rt.gateway)
            caps = sa.CAPS[call]
            if case == "actor":
                ctx = await _context(rt, sa_install, sa.ACTOR, call, kind="actor")
                code = "PRINCIPAL_NOT_WORKLOAD"
            else:
                altered = (frozenset(sorted(caps)[:1]) if case == "subset"
                           else caps | {"m05.followup.read"})
                ctx = await _context(rt, sa_install, principal, call, capabilities=altered)
                code = "CAPABILITIES_INVALID"
            with pytest.raises(IntakeRefused) as caught:
                await getattr(_service(gateway), method)(ctx, _argument(call))
            assert caught.value.code == code
            assert gateway.entered == 0

    _run(body)


# --- SA-V02 .. SA-V06 ----------------------------------------------------------------------------


def test_sa_v02_dispatch_service(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    patient, _, (ref,) = sa.fresh_patient(sa_install)
    reminder = uuid.uuid4()

    async def body() -> tuple[Any, Any]:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_DISPATCH, "dispatch")
            service = _service(rt.gateway)
            command = _commands().DispatchCommand(reminder_id=reminder,
                                                  appointment_ref=uuid.UUID(ref))
            return await service.register_dispatch(ctx, command), \
                await service.register_dispatch(ctx, command)

    first, second = _run(body)
    events = [e for e in _events(sa_install, sa.T_DISPATCHED)
              if e["correlation_id"] == str(reminder)]
    assert len(events) == 1
    e = events[0]
    w = sa.window(sa_install, str(reminder))
    assert (e["event_id"], e["patient_id"], e["producer_service"], e["producer_dedupe_key"],
            e["workload_id"]) == (w["dispatch_event_id"], patient, sa.PRODUCER,
                                  f"dispatch:{reminder}", sa.W_DISPATCH)
    assert e["metadata"] == {"reminder_id": str(reminder), "appointment_ref": ref}
    assert str(first.event_id) == str(second.event_id) == w["dispatch_event_id"]
    assert sa.items_for(sa_install, patient) == []


def test_sa_v03_inbound_item_and_correlation(sa_install: sa.SAInstall) -> None:
    patient, phone, ref, reminder = _dispatched(sa_install)
    message = sa.new_id()

    async def body() -> tuple[Any, Any]:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            service = _service(rt.gateway)
            command = _inbound_command(phone, "reschedule", message)
            return await service.register_inbound(ctx, command), \
                await service.register_inbound(ctx, command)

    first, again = _run(body)
    items = sa.items_for(sa_install, patient)
    assert len(items) == 1 and items[0]["item_kind"] == "reschedule_request"
    assert str(first.item_id) == str(again.item_id) == items[0]["item_id"]
    source = sa.event(sa_install, items[0]["source_event_id"])
    assert source["event_type"] == sa.T_REPLY and source["patient_id"] == patient
    assert source["metadata"] == {"inbound_id": str(first.inbound_id),
                                  "reply_class": "reschedule",
                                  "triage_kind": "reschedule_request",
                                  "appointment_ref": ref, "reminder_id": reminder}
    assert source["producer_dedupe_key"] == f"reply:{first.inbound_id}"
    assert len([e for e in _events(sa_install, sa.T_REPLY)
                if e["patient_id"] == patient]) == 1


@pytest.mark.parametrize(("reply_class", "event", "item"), (
    ("confirm", True, False), ("stop", False, False),
))
def test_sa_v04_confirm_and_stop(sa_install: sa.SAInstall, reply_class: str, event: bool,
                                 item: bool) -> None:
    patient, phone, _, _ = _dispatched(sa_install)

    async def body() -> Any:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            return await _service(rt.gateway).register_inbound(
                ctx, _inbound_command(phone, reply_class))

    result = _run(body)
    assert result.item_id is None
    got = [e for e in _events(sa_install, sa.T_REPLY) if e["patient_id"] == patient]
    assert len(got) == (1 if event else 0)
    assert (len(sa.items_for(sa_install, patient)) > 0) is item


def test_sa_v04b_quarantined(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    before = (len(_events(sa_install)), sa.snapshot(sa_install)["m05_followup_items"])

    async def body() -> Any:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            return await _service(rt.gateway).register_inbound(
                ctx, _inbound_command(sa.PHONE_UNKNOWN, "reschedule"))

    result = _run(body)
    assert (result.decision, result.event_id, result.item_id) == ("quarantined", None, None)
    assert (len(_events(sa_install)), sa.snapshot(sa_install)["m05_followup_items"]) == before


def test_sa_v05_ambiguous(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    patient, phone, refs = sa.fresh_patient(sa_install, appointments=2)
    for ref in refs:
        sa.dispatch(sa_install, sa.new_id(), ref)

    async def body() -> Any:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            return await _service(rt.gateway).register_inbound(
                ctx, _inbound_command(phone, "cancel"))

    result = _run(body)
    (item,) = sa.items_for(sa_install, patient)
    assert item["item_kind"] == "unclear_reply" and str(result.item_id) == item["item_id"]
    source = sa.event(sa_install, item["source_event_id"])
    assert (source["metadata"]["appointment_ref"], source["metadata"]["reminder_id"],
            source["metadata"]["triage_kind"]) == (None, None, "unclear_reply")


def test_sa_v06_close_service(sa_install: sa.SAInstall) -> None:
    patient, _, ref, reminder = _dispatched(sa_install)
    sa.shift_window(sa_install, reminder, -25)

    async def body() -> tuple[Any, Any]:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_TIMER, "close")
            service = _service(rt.gateway)
            return await service.close_window(ctx, uuid.UUID(reminder)), \
                await service.close_window(ctx, uuid.UUID(reminder))

    first, again = _run(body)
    (item,) = sa.items_for(sa_install, patient)
    assert item["item_kind"] == "no_reply"
    assert str(first.item_id) == str(again.item_id) == item["item_id"]
    source = sa.event(sa_install, item["source_event_id"])
    assert (source["event_type"], source["producer_dedupe_key"], source["workload_id"]) == (
        sa.T_NO_REPLY, f"no_reply:{reminder}", sa.W_TIMER)
    assert source["metadata"] == {"reminder_id": reminder, "appointment_ref": ref,
                                  "triage_kind": "no_reply"}
    assert source["event_id"] == sa.window(sa_install, reminder)["closed_event_id"]


# --- SA-V07 / SA-V08 -----------------------------------------------------------------------------

FAULTS: dict[str, dict[str, Any]] = {
    "after-register": {"fail_after": "m05.intake.inbound"},
    "after-append": {"fail_after": "m02.observation.append"},
    "at-create": {"fail_before": "m05.followup.create"},
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_sa_v07_atomic_then_recovered(sa_install: sa.SAInstall, fault: str) -> None:
    patient, phone, _, reminder = _dispatched(sa_install)
    message = sa.new_id()
    before = sa.snapshot(sa_install)
    window_before = sa.window(sa_install, reminder)

    async def faulty() -> None:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            gateway = FaultGateway(rt.gateway, **FAULTS[fault])
            with pytest.raises(Exception):  # noqa: B017 - any failure; state is asserted
                await _service(gateway).register_inbound(
                    ctx, _inbound_command(phone, "reschedule", message))

    _run(faulty)
    assert sa.snapshot(sa_install) == before
    assert sa.window(sa_install, reminder) == window_before

    async def rerun() -> Any:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            return await _service(rt.gateway).register_inbound(
                ctx, _inbound_command(phone, "reschedule", message))

    result = _run(rerun)
    assert len(sa.items_for(sa_install, patient)) == 1
    assert str(result.item_id) == sa.items_for(sa_install, patient)[0]["item_id"]


@pytest.mark.parametrize("call", ("inbound", "close"))
def test_sa_v08_lost_ack(sa_install: sa.SAInstall, call: str) -> None:
    patient, phone, _, reminder = _dispatched(sa_install)
    message = sa.new_id()
    if call == "close":
        sa.shift_window(sa_install, reminder, -25)

    async def attempt(lose: bool) -> Any:
        from haloflow.m05.intake import IntakeCommitUnknown

        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, CALLS[call][0], call)
            service = _service(FaultGateway(rt.gateway, lose_ack=lose))
            argument = (_inbound_command(phone, "reschedule", message) if call == "inbound"
                        else uuid.UUID(reminder))
            if lose:
                with pytest.raises(IntakeCommitUnknown):
                    await getattr(service, CALLS[call][1])(ctx, argument)
                return None
            return await getattr(service, CALLS[call][1])(ctx, argument)

    _run(lambda: attempt(True))
    result = _run(lambda: attempt(False))
    items = sa.items_for(sa_install, patient)
    assert len(items) == 1 and str(result.item_id) == items[0]["item_id"]
    events = [e for e in _events(sa_install) if e["patient_id"] == patient
              and e["event_type"] != sa.T_DISPATCHED]
    assert len(events) == 1


# --- SA-V09 --------------------------------------------------------------------------------------


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def spy(name: str) -> Callable[..., Any]:
        def _fail(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"S-A must not call {name}")
        return _fail

    targets = (
        ("haloflow.integrations.notifyre", "NotifyreClient",
         ("send_sms", "send_fax", "get_inbound_faxes", "get_fax_status")),
        ("haloflow.integrations.stedi", "StediEligibilityClient", None),
        ("haloflow.ehr.athenahealth", "AthenaHealthAdapter", None),
    )
    import importlib

    for module_name, class_name, methods in targets:
        cls = getattr(importlib.import_module(module_name), class_name)
        names = methods or [n for n in vars(cls) if not n.startswith("_")
                            and callable(getattr(cls, n))]
        for name in names:
            monkeypatch.setattr(cls, name, spy(f"{class_name}.{name}"))

    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if self.family == socket.AF_UNIX or host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        calls.append(f"socket.connect {host}")
        raise AssertionError(f"S-A must not open a network connection to {host}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return calls


def test_sa_v09_no_side_effects(sa_install: sa.SAInstall, no_side_effects: list[str]) -> None:
    sa_install.require()
    patient, phone, (ref,) = sa.fresh_patient(sa_install)
    reminder = uuid.uuid4()

    async def body() -> None:
        intake = _commands()
        async with _runtime(sa_install) as rt:
            service = _service(rt.gateway)
            await service.register_dispatch(
                await _context(rt, sa_install, sa.W_DISPATCH, "dispatch"),
                intake.DispatchCommand(reminder_id=reminder, appointment_ref=uuid.UUID(ref)))
            ingress = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            for reply_class in ("confirm", "reschedule", "stop"):
                await service.register_inbound(ingress, _inbound_command(phone, reply_class))
            sa.shift_window(sa_install, str(reminder), -25)
            await service.close_window(await _context(rt, sa_install, sa.W_TIMER, "close"),
                                       reminder)

    _run(body)
    assert no_side_effects == []
    assert sa.items_for(sa_install, patient)  # the calls really ran


# --- SA-V10 --------------------------------------------------------------------------------------


def test_sa_v10_binding_from_trusted_sources(sa_install: sa.SAInstall,
                                             sa_other_install: sa.SAInstall) -> None:
    patient, phone, _, _ = _dispatched(sa_install)
    sa_other_install.require()

    async def body() -> Any:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            return await _service(rt.gateway).register_inbound(
                ctx, _inbound_command(phone, "cancel"))

    result = _run(body)
    (item,) = sa.items_for(sa_install, patient)
    assert item["patient_id"] == patient == str(result.patient_id)
    assert sa.items_for(sa_other_install, patient) == []
