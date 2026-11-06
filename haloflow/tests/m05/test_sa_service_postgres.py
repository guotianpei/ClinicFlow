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
          dispatch:<reminder hex> (M02 key grammar [a-z0-9:]+, pins C9),
          correlation = reminder, patient from the directory,
          workload = dispatch workload, occurred_at = stored sent_at); no item;
          retry -> the same event (existing), still one.
- SA-V03  inbound reschedule: one `m05_reminder_reply_received` event (metadata
          inbound_id, reply_class, triage_kind, appointment_ref, reminder_id) and one
          reschedule_request item; item -> source event -> appointment_ref (R-SA-7);
          redelivery -> one event, one item.
- SA-V04  confirm -> event, no item; STOP -> no event, no item; quarantined -> none.
- SA-V05  ambiguous -> unclear_reply item; metadata appointment_ref and reminder_id
          null; patient set.
- SA-V06  close -> `m05_reminder_no_reply` event (dedupe noreply:<reminder hex>) + one
          no_reply item; rerun -> the same item, one each.
- SA-V07  atomicity: a fault injected after the registration statement, after the
          append, or at the create rolls back EVERYTHING (no inbound row, window
          unchanged, no event, no item); the rerun yields exactly one of each.
- SA-V08  lost acknowledgement: the gateway raises after COMMIT -> IntakeCommitUnknown;
          the rerun returns the stored result: one inbound row, one event, one item.
- SA-V09  no side effects (R-SA-10 behavioural half): `sa_guard` (sa_support) is
          ACTIVE FOR EVERY S-A ROW (pytestmark) and asserts at teardown that no
          forbidden use happened, even one the code under test swallowed: SMS, fax,
          eligibility, EHR and legacy module services are sys.modules tripwires; httpx
          sends refused; Python socket connects refused except to the configured test
          database. This row drives every call kind and branch, incl. a due no_reply.
- SA-V10  binding (R-SA-8): the item's patient and the event's patient come from the
          directory/window, and the tenant from the context; nothing in the public
          commands can name either (SA-S08 pins the fields).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sa_support as sa
import service_support as sv

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


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


class InjectedFault(Exception):
    """The ONLY exception the fault harness raises (Codex b5d846f item 2)."""


class FaultGateway:
    """Wraps the real gateway. `fail_before`/`fail_after` raise InjectedFault around
    ONE statement key inside the transaction; `lose_ack` raises InjectedFault after
    the real transaction RETURNED (committed). Every firing is recorded in `fired`
    as (key, phase) so a row can prove the designated point fired exactly once."""

    def __init__(self, real: Any, *, fail_before: str | None = None,
                 fail_after: str | None = None, lose_ack: bool = False) -> None:
        self.real = real
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.lose_ack = lose_ack
        self.entered = 0
        self.fired: list[tuple[str, str]] = []
        self.reached: list[tuple[str, str]] = []

    async def with_tenant_transaction(self, context: Any, callback: Callable[[Any], Awaitable[Any]],
                                      **kwargs: Any) -> Any:
        self.entered += 1
        outer = self

        class Handle:
            def __init__(self, real: Any) -> None:
                self._real = real

            async def _run(self, method: str, key: str, params: Any) -> Any:
                outer.reached.append((key, "before"))
                if key == outer.fail_before:
                    outer.fired.append((key, "before"))
                    raise InjectedFault(f"before {key}")
                result = await getattr(self._real, method)(key, params)
                outer.reached.append((key, "after"))
                if key == outer.fail_after:
                    outer.fired.append((key, "after"))
                    raise InjectedFault(f"after {key}")
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
            self.fired.append(("commit", "after"))
            raise InjectedFault("acknowledgement lost after commit")
        return result


def _chain(error: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in out:
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


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
                                  f"dispatch:{reminder.hex}", sa.W_DISPATCH)
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
    assert source["producer_dedupe_key"] == f"reply:{first.inbound_id.hex}"
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
        sa.T_NO_REPLY, f"noreply:{uuid.UUID(reminder).hex}", sa.W_TIMER)
    assert source["metadata"] == {"reminder_id": reminder, "appointment_ref": ref,
                                  "triage_kind": "no_reply"}
    assert source["event_id"] == sa.window(sa_install, reminder)["closed_event_id"]


# --- SA-V07 / SA-V08 -----------------------------------------------------------------------------

FAULTS: dict[str, tuple[dict[str, Any], tuple[str, str]]] = {
    "after-register": ({"fail_after": "m05.intake.inbound"}, ("m05.intake.inbound", "after")),
    "after-append": ({"fail_after": "m02.observation.append"},
                     ("m02.observation.append", "after")),
    "at-create": ({"fail_before": "m05.followup.create"}, ("m05.followup.create", "before")),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_sa_v07_atomic_then_recovered(sa_install: sa.SAInstall, fault: str) -> None:
    patient, phone, _, reminder = _dispatched(sa_install)
    message = sa.new_id()
    before = sa.snapshot(sa_install)
    window_before = sa.window(sa_install, reminder)
    options, expected = FAULTS[fault]

    async def faulty() -> FaultGateway:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            gateway = FaultGateway(rt.gateway, **options)
            with pytest.raises(Exception) as caught:  # noqa: B017 - identity asserted below
                await _service(gateway).register_inbound(
                    ctx, _inbound_command(phone, "reschedule", message))
            # The designated point fired exactly once, and the failure IS that
            # injection (directly or as the cause), not an unrelated error.
            assert gateway.fired == [expected], gateway.fired
            assert any(isinstance(e, InjectedFault) for e in _chain(caught.value)), caught.value
            return gateway

    _run(faulty)
    assert sa.snapshot(sa_install) == before
    assert sa.window(sa_install, reminder) == window_before

    async def rerun() -> Any:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            return await _service(rt.gateway).register_inbound(
                ctx, _inbound_command(phone, "reschedule", message))

    result = _run(rerun)
    items = sa.items_for(sa_install, patient)
    assert len(items) == 1 and str(result.item_id) == items[0]["item_id"]


@pytest.mark.parametrize("call", ("inbound", "close"))
def test_sa_v08_lost_ack(sa_install: sa.SAInstall, call: str) -> None:
    patient, phone, _, reminder = _dispatched(sa_install)
    message = sa.new_id()
    if call == "close":
        sa.shift_window(sa_install, reminder, -25)

    def argument() -> Any:
        return (_inbound_command(phone, "reschedule", message) if call == "inbound"
                else uuid.UUID(reminder))

    async def lost() -> None:
        from haloflow.m05.intake import IntakeCommitUnknown

        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, CALLS[call][0], call)
            gateway = FaultGateway(rt.gateway, lose_ack=True)
            with pytest.raises(IntakeCommitUnknown):
                await getattr(_service(gateway), CALLS[call][1])(ctx, argument())
            # Fired once, AFTER the real transaction returned (nothing before it).
            assert gateway.fired == [("commit", "after")], gateway.fired

    _run(lost)
    # The commit really happened: capture the committed identities BEFORE retrying.
    committed_items = sa.items_for(sa_install, patient)
    assert len(committed_items) == 1
    committed_event = committed_items[0]["source_event_id"]
    committed_inbound = sa.admin(sa_install, sa.q(sa_install,
                                                  "SELECT inbound_id::text FROM"
                                                  " {s}.m05_inbound_replies WHERE"
                                                  " provider_message_id = %s"), (message,))

    async def retry() -> Any:
        async with _runtime(sa_install) as rt:
            ctx = await _context(rt, sa_install, CALLS[call][0], call)
            return await getattr(_service(rt.gateway), CALLS[call][1])(ctx, argument())

    result = _run(retry)
    assert sa.items_for(sa_install, patient) == committed_items
    assert str(result.item_id) == committed_items[0]["item_id"]
    assert sa.admin(sa_install, sa.q(sa_install, "SELECT inbound_id::text FROM"
                                                 " {s}.m05_inbound_replies WHERE"
                                                 " provider_message_id = %s"), (message,)) \
        == committed_inbound
    events = [e["event_id"] for e in _events(sa_install) if e["patient_id"] == patient
              and e["event_type"] != sa.T_DISPATCHED]
    assert events == [committed_event]


# --- SA-V09 --------------------------------------------------------------------------------------


def test_sa_v09_no_side_effects(sa_install: sa.SAInstall, sa_guard: list[str]) -> None:
    """Every call kind and branch under the guard (also active for every other S-A
    row, asserted at teardown): dispatch; inbound confirm, reschedule, cancel,
    unclear, ambiguous, quarantined, STOP; a DUE unanswered window closed no_reply."""

    sa_install.require()
    patient, phone, refs = sa.fresh_patient(sa_install, appointments=3)
    reminders = [uuid.uuid4() for _ in refs]

    async def body() -> None:
        intake = _commands()
        async with _runtime(sa_install) as rt:
            service = _service(rt.gateway)
            dispatch_ctx = await _context(rt, sa_install, sa.W_DISPATCH, "dispatch")
            for reminder, ref in zip(reminders, refs, strict=True):
                await service.register_dispatch(dispatch_ctx, intake.DispatchCommand(
                    reminder_id=reminder, appointment_ref=uuid.UUID(ref)))
            ingress = await _context(rt, sa_install, sa.W_INGRESS, "inbound")
            # Three current windows: every reply is ambiguous -> unclear_reply items.
            for reply_class in ("confirm", "reschedule", "cancel", "unclear"):
                await service.register_inbound(ingress, _inbound_command(phone, reply_class))
            await service.register_inbound(ingress,
                                           _inbound_command(sa.PHONE_UNKNOWN, "cancel"))
            # A single-window patient: exact kinds.
            p2, phone2, (ref2,) = sa.fresh_patient(sa_install)
            await service.register_dispatch(dispatch_ctx, intake.DispatchCommand(
                reminder_id=uuid.uuid4(), appointment_ref=uuid.UUID(ref2)))
            for reply_class in ("reschedule", "cancel", "unclear", "confirm"):
                await service.register_inbound(ingress, _inbound_command(phone2, reply_class))
            # A due unanswered window -> no_reply item.
            sa.shift_window(sa_install, str(reminders[0]), -25)
            timer = await _context(rt, sa_install, sa.W_TIMER, "close")
            closed = await service.close_window(timer, reminders[0])
            assert closed.item_id is not None
            await service.register_inbound(ingress, _inbound_command(phone, "stop"))
            assert sa.items_for(sa_install, p2)

    _run(body)
    assert sa_guard == []
    kinds = sorted(i["item_kind"] for i in sa.items_for(sa_install, patient))
    assert "no_reply" in kinds and "unclear_reply" in kinds


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
