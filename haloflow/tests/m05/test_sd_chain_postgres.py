"""L-1S package S-D — service-chain integration (tests only, no source change).

Governing: S-D package v1 + v1r1 (Work Session 2026-11-06 / 2026-11-07), Codex ALIGN
20261107T121737Z. One S-C TEST runtime (target 46, S-C TEST catalogue,
ActivationPolicy.ENFORCE, real TenantResolver and TenantTransactionGateway); public
services only (IntakeService, DueWindowRunner, M05Service). Reply class and dispatch are
supplied facts; resolution is a staff assertion; nothing is sent and nothing is written to
an EHR (the S-A side-effect guard is active for every row). Every scenario begins with
`sd.begin` (v1r1 C2) BEFORE its first row snapshot; every assertion on rows is an exact
per-key count delta.

- SD-01  dispatch -> confirm: window replied, reply event, no item; the due runner then
         attempts nothing.
- SD-02  dispatch -> reschedule / cancel / unclear (one current window): window replied,
         one item of the exact kind; staff list, claim (open, v2), resolve (v3) with the
         assigned outcome; the shared positive oracle `sd.assert_resolved`.
- SD-03  dispatch, deadline passed (fixture), run_once -> no_reply item (event at the
         stored deadline); staff resolve no_contact_left_message.
- SD-04  STOP on a phone with two awaiting windows: both opted_out (closed_decision
         NULL), no event, no item; runner attempts nothing; a new dispatch to the phone
         -> IntakeFailed CONTACT_OPTED_OUT, no rows.
- SD-05  duplicates converge: dispatch, inbound redelivery, INBOUND_CONFLICT, second
         run, direct close replay, claim replay, resolve replay -> zero deltas.
- SD-06  tenant isolation and authorization (two tenants, same patient/appointment ids).
- SD-07  revoke interval on a genuinely due awaiting window (v1r1 C4).
- SD-08a before-commit failure rolls back; a fresh identical call applies exactly once.
- SD-08b after-commit uncertainty; the identical retry returns the stored result.
- NC-1..NC-5 negative controls: each proves its oracle can fail (v1 section 4, v1r1 C1).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import m02_support
import pytest
import sa_support as sa
import sb_runtime as sr
import sb_support as sb
import sc_support as sc
import sd_support as sd
import service_support as sv

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


@pytest.fixture(scope="module")
def sd_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[sc.SCInstall]:
    yield from sc.module_install(request, m02_ids, m05_reset, "sd")


@pytest.fixture(scope="module")
def sd_other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
             m05_reset: Any) -> Iterator[sc.SCInstall]:
    yield from sc.module_install(request, m02_ids, m05_reset, "sd:other")


def _activation_refused(error: BaseException) -> str:
    """v1r1 C3: the public intake and M05 read surfaces raise the gateway's
    ActivationRefused unwrapped."""

    from haloflow.m01.errors import ActivationRefused

    assert type(error) is ActivationRefused, repr(error)
    code: str = error.reason_code
    return code


# --- SD-01 ----------------------------------------------------------------------------------


def test_sd01_dispatch_confirm(sd_install: sc.SCInstall) -> None:
    sd.begin(sd_install)
    patient, phone, (ref,) = sa.fresh_patient(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            before = sd.counts(sd_install)
            dispatched = await chain.dispatch(ref)
            assert (dispatched.outcome, str(dispatched.patient_id)) == ("registered", patient)
            mid = sd.counts(sd_install)
            assert sd.delta(before, mid) == sd.DISPATCH
            reply = await chain.inbound(phone, "confirm")
            assert (reply.outcome, reply.decision, reply.triage_kind, reply.item_id) == (
                "registered", "recorded", None, None)
            after = sd.counts(sd_install)
            assert sd.delta(mid, after) == sd.CONFIRM
            window = sa.window(sd_install, str(dispatched.reminder_id))
            assert (window["state"], window["closed_decision"]) == ("replied", None)
            sa.shift_window(sd_install, str(dispatched.reminder_id), -25)
            run = await chain.run_once()
            assert (run.code, run.attempted, run.no_reply) == ("COMPLETE", 0, 0)
            sd.assert_no_change(sd.delta(after, sd.counts(sd_install)))

    asyncio.run(body())


# --- SD-02 ----------------------------------------------------------------------------------

SD02: dict[str, tuple[str, str]] = {
    "reschedule": ("reschedule_request", "rescheduled"),
    "cancel": ("cancel_request", "cancelled_in_ehr"),
    "unclear": ("unclear_reply", "reached_no_change"),
}


async def _reply_item(chain: sd.Chain, reply_class: str) -> tuple[Any, Any]:
    """dispatch + one reply on a fresh single-window patient; returns (dispatch, reply)."""

    _, phone, (ref,) = sa.fresh_patient(chain.install)
    dispatched = await chain.dispatch(ref)
    return dispatched, await chain.inbound(phone, reply_class)


@pytest.mark.parametrize("reply_class", sorted(SD02))
def test_sd02_reply_item_staff_resolve(sd_install: sc.SCInstall, reply_class: str) -> None:
    kind, outcome = SD02[reply_class]
    sd.begin(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sd_install)
            dispatched = await chain.dispatch(ref)
            before = sd.counts(sd_install)
            reply = await chain.inbound(phone, reply_class)
            assert (reply.outcome, reply.decision, reply.triage_kind) == (
                "registered", "item", kind)
            assert reply.item_id is not None and reply.event_id is not None
            created = sd.counts(sd_install)
            assert sd.delta(before, created) == sd.reply_item(kind)
            assert sa.window(sd_install, str(dispatched.reminder_id))["state"] == "replied"
            item = reply.item_id
            assert str(item) in await chain.listed()
            claimed = await chain.claim(item)
            assert claimed.outcome == "applied"
            assert (claimed.snapshot["state"], claimed.snapshot["assignee_id"],
                    claimed.snapshot["version"]) == ("open", sd.STAFF, 2)
            after_claim = sd.counts(sd_install)
            assert sd.delta(created, after_claim) == sd.CLAIM
            resolved = await chain.resolve(item, outcome)
            assert resolved.outcome == "applied"
            assert (resolved.snapshot["state"], resolved.snapshot["outcome_code"],
                    resolved.snapshot["version"]) == ("resolved", outcome, 3)
            assert sd.delta(after_claim, sd.counts(sd_install)) == sd.resolve(kind)
            await sd.assert_resolved(chain, item, outcome, kind=kind,
                                     source_event=reply.event_id)

    asyncio.run(body())


# --- SD-03 ----------------------------------------------------------------------------------


def _check_no_reply_run(run: Any, change: dict[str, int]) -> None:
    """The SD-03 close oracle (NC-2 drives it with an undue window)."""

    assert (run.code, run.attempted, run.no_reply, run.opted_out) == ("COMPLETE", 1, 1, 0), run
    assert change == sd.NO_REPLY, change


def test_sd03_no_reply_item_staff_resolve(sd_install: sc.SCInstall) -> None:
    sd.begin(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            patient, _, (ref,) = sa.fresh_patient(sd_install)
            dispatched = await chain.dispatch(ref)
            reminder = str(dispatched.reminder_id)
            sa.shift_window(sd_install, reminder, -25)
            assert [r for _, r in sb.awaiting_due(sd_install)] == [reminder]  # type: ignore[arg-type]
            before = sd.counts(sd_install)
            run = await chain.run_once()
            after = sd.counts(sd_install)
            _check_no_reply_run(run, sd.delta(before, after))
            window = sa.window(sd_install, reminder)
            assert (window["state"], window["closed_decision"]) == ("no_reply", "no_reply")
            event = sa.event(sd_install, window["closed_event_id"])
            assert (event["event_type"], event["occurred_at"], event["patient_id"]) == (
                sd.T_NO_REPLY, window["deadline"], patient)
            (item_view,) = sa.items_for(sd_install, patient)
            assert (item_view["item_kind"], item_view["source_event_id"]) == (
                "no_reply", window["closed_event_id"])
            item = uuid.UUID(item_view["item_id"])
            assert (await chain.claim(item)).outcome == "applied"
            mid = sd.counts(sd_install)
            assert (await chain.resolve(item, "no_contact_left_message")).outcome == "applied"
            assert sd.delta(mid, sd.counts(sd_install)) == sd.resolve("no_reply")
            await sd.assert_resolved(chain, item, "no_contact_left_message", kind="no_reply",
                                     source_event=uuid.UUID(window["closed_event_id"]))

    asyncio.run(body())


# --- SD-04 ----------------------------------------------------------------------------------


def test_sd04_stop_opts_out_the_number(sd_install: sc.SCInstall) -> None:
    from haloflow.m05.intake import IntakeFailed

    sd.begin(sd_install)
    _, phone, (ref1, ref2, ref3) = sa.fresh_patient(sd_install, appointments=3)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            reminders = [str((await chain.dispatch(ref)).reminder_id) for ref in (ref1, ref2)]
            before = sd.counts(sd_install)
            stop = await chain.inbound(phone, "stop")
            assert (stop.outcome, stop.decision, stop.triage_kind, stop.event_id,
                    stop.item_id) == ("registered", "opt_out", None, None, None)
            after = sd.counts(sd_install)
            assert sd.delta(before, after) == {"replies": 1, "opted_out": 1,
                                               "windows:awaiting": -2, "windows:opted_out": 2}
            for reminder in reminders:
                window = sa.window(sd_install, reminder)
                assert (window["state"], window["closed_decision"]) == ("opted_out", None)
                sa.shift_window(sd_install, reminder, -25)
            run = await chain.run_once()
            assert (run.code, run.attempted) == ("COMPLETE", 0)
            with pytest.raises(IntakeFailed) as caught:
                await chain.dispatch(ref3)
            assert caught.value.code == "CONTACT_OPTED_OUT"
            sd.assert_no_change(sd.delta(after, sd.counts(sd_install)))

    asyncio.run(body())


# --- SD-05 ----------------------------------------------------------------------------------


def test_sd05_duplicates_converge(sd_install: sc.SCInstall) -> None:
    from haloflow.m05.intake import IntakeFailed

    sd.begin(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            # Dispatch repeat.
            _, phone, (ref,) = sa.fresh_patient(sd_install)
            reminder = uuid.uuid4()
            first = await chain.dispatch(ref, reminder)
            mark = sd.counts(sd_install)
            again = await chain.dispatch(ref, reminder)
            assert (again.outcome, again.event_id) == ("existing", first.event_id)
            sd.assert_no_change(sd.delta(mark, sd.counts(sd_install)))
            # Inbound redelivery (same id, same content), then a conflicting one.
            message = sa.new_id()
            reply = await chain.inbound(phone, "reschedule", message)
            mark = sd.counts(sd_install)
            redelivered = await chain.inbound(phone, "reschedule", message)
            assert (redelivered.outcome, redelivered.inbound_id, redelivered.event_id,
                    redelivered.item_id, redelivered.decision) == (
                "existing", reply.inbound_id, reply.event_id, reply.item_id, "item")
            with pytest.raises(IntakeFailed) as caught:
                await chain.inbound(phone, "cancel", message)
            assert caught.value.code == "INBOUND_CONFLICT"
            sd.assert_no_change(sd.delta(mark, sd.counts(sd_install)))
            # Second run; direct close replay.
            patient2, _, (ref2,) = sa.fresh_patient(sd_install)
            due = await chain.dispatch(ref2)
            sa.shift_window(sd_install, str(due.reminder_id), -25)
            closed = await chain.run_once()
            assert (closed.attempted, closed.no_reply) == (1, 1)
            mark = sd.counts(sd_install)
            second = await chain.run_once()
            assert (second.code, second.attempted) == ("COMPLETE", 0)
            window = sa.window(sd_install, str(due.reminder_id))
            (created,) = sa.items_for(sd_install, patient2)
            replay = await chain.close(due.reminder_id)
            assert (replay.outcome, replay.decision, str(replay.event_id),
                    str(replay.item_id)) == ("existing", "no_reply", window["closed_event_id"],
                                             created["item_id"])
            sd.assert_no_change(sd.delta(mark, sd.counts(sd_install)))
            # Claim and resolve replays (same client_action_id).
            item = reply.item_id
            claim_key, resolve_key = uuid.uuid4(), uuid.uuid4()
            claimed = await chain.claim(item, claim_key)
            resolved = await chain.resolve(item, "rescheduled", resolve_key)
            mark = sd.counts(sd_install)
            claim_again = await chain.claim(item, claim_key)
            assert (claim_again.outcome, dict(claim_again.snapshot)) == (
                "replayed", dict(claimed.snapshot))
            resolve_again = await chain.resolve(item, "rescheduled", resolve_key)
            assert (resolve_again.outcome, dict(resolve_again.snapshot)) == (
                "replayed", dict(resolved.snapshot))
            sd.assert_no_change(sd.delta(mark, sd.counts(sd_install)))
            await sd.assert_resolved(chain, item, "rescheduled", kind="reschedule_request",
                                     source_event=reply.event_id)

    asyncio.run(body())


# --- SD-06 ----------------------------------------------------------------------------------


def test_sd06_tenant_and_authorization(sd_install: sc.SCInstall, sd_other: sc.SCInstall) -> None:
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.m01.errors import TenantDenied
    from haloflow.m01.staff.service import ReadRefused, RouteDenied
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.due_windows import DueWindowRunner, SBRefused
    from haloflow.m05.intake import IntakeRefused, IntakeService
    from haloflow.m05.service import ActionRefused, StaffAction

    sd.begin(sd_other)
    sd.begin(sd_install)
    patient, phone, (ref,) = sa.fresh_patient(sd_install)
    # Tenant B: the SAME patient and appointment ids, its own phone and item.
    other_phone = sa.fresh_phone()
    sa.directory(sd_other, "contact_put", phone=other_phone, patient_id=patient)
    sa.directory(sd_other, "appointment_put", appointment_ref=ref, patient_id=patient,
                 start_at=sa.FUTURE.format(n=1), status="booked")

    async def other_item() -> Any:
        async with sd.runtime(sd_other) as rt:
            chain = sd.Chain(sd_other, rt)
            await chain.dispatch(ref)
            return (await chain.inbound(other_phone, "reschedule")).item_id

    b_item = asyncio.run(other_item())
    assert b_item is not None

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            await chain.dispatch(ref)
            a_item = (await chain.inbound(phone, "reschedule")).item_id
            before_a, before_b = sd.counts(sd_install), sd.counts(sd_other)
            visible = await chain.listed()
            assert str(a_item) in visible and str(b_item) not in visible
            with pytest.raises(ReadRefused) as read:
                await chain.get(b_item)
            assert read.value.code == "ITEM_NOT_FOUND"
            with pytest.raises(ActionRefused) as act:
                await chain.claim(b_item)
            assert act.value.code == "ITEM_NOT_FOUND"
            # A principal authorized only for A, hinting B: the real resolver refuses.
            with pytest.raises(TenantDenied) as denied:
                await rt.resolver.resolve(
                    principal=Principal(kind=PrincipalKind.ACTOR, id=sd.STAFF,
                                        auth_method="test",
                                        authorized_tenant_ids=frozenset({sd_install.tenant_id}),
                                        capabilities=sv.RESOLVE_CAPS),
                    tenant_hint=sd_other.tenant_id, purpose="operations",
                    capabilities=sv.RESOLVE_CAPS, source=TrustedSource.HTTP,
                    execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
                    correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)
            assert denied.value.reason_code == "TENANT_BINDING_MISMATCH"
            # Staff context without m05.followup.apply.
            read_only = await chain.actor(sv.READ_ONLY_CAPS)
            with pytest.raises(RouteDenied) as route:
                await chain.staff.act(read_only, StaffAction(
                    item_id=a_item, client_action_id=uuid.uuid4(), expected_version=1,
                    type="claim"))
            assert route.value.reason_code == "CAPABILITY_DENIED"
            # Intake context with the wrong exact capability set.
            wrong = await sv.context(rt, sd_install, sa.W_DISPATCH, sa.CAPS["inbound"],
                                     kind="workload")
            from haloflow.m05.intake import DispatchCommand

            with pytest.raises(IntakeRefused) as intake:
                await chain.intake.register_dispatch(wrong, DispatchCommand(
                    reminder_id=uuid.uuid4(), appointment_ref=uuid.UUID(ref)))
            assert intake.value.code == "CAPABILITIES_INVALID"
            # Runner principal with non-timer capabilities.
            with pytest.raises(SBRefused) as runner:
                DueWindowRunner(
                    gateway=rt.gateway, resolver=rt.resolver,
                    intake=IntakeService(EventFoundation(rt.gateway,
                                                         producer_service=sa.PRODUCER)),
                    principal=Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_TIMER,
                                        auth_method="worker",
                                        authorized_tenant_ids=frozenset({sd_install.tenant_id}),
                                        capabilities=sb.SELECT_CAPS),
                    page_limit=10, attempt_cap=50)
            assert runner.value.code == "CAPABILITIES_INVALID"
            sd.assert_no_change(sd.delta(before_a, sd.counts(sd_install)))
            sd.assert_no_change(sd.delta(before_b, sd.counts(sd_other)))

    asyncio.run(body())


# --- SD-07 ----------------------------------------------------------------------------------


def test_sd07_revoke_interval_on_a_due_window(sd_install: sc.SCInstall) -> None:
    sd.begin(sd_install)
    patient, phone, (ref,) = sa.fresh_patient(sd_install)

    async def body() -> None:
        from haloflow.m05.service import ListQuery

        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            dispatched = await chain.dispatch(ref)
            reminder = str(dispatched.reminder_id)
            sa.shift_window(sd_install, reminder, -25)
            assert [r for _, r in sb.awaiting_due(sd_install)] == [reminder]  # type: ignore[arg-type]
            sc.change(sd_install, "revoke")
            before = sd.counts(sd_install)
            run = await chain.run_once()
            assert (run.code, run.attempted, run.no_reply) == ("SELECTION_FAILED", 0, 0)
            assert sa.window(sd_install, reminder)["state"] == "awaiting"
            with pytest.raises(Exception) as inbound:  # noqa: B017 - exact type asserted
                await chain.inbound(phone, "reschedule")
            assert _activation_refused(inbound.value) == "M05_NOT_ACTIVATED"
            with pytest.raises(Exception) as listed:  # noqa: B017 - exact type asserted
                await chain.staff.list_items(await chain.actor(), ListQuery(limit=10))
            assert _activation_refused(listed.value) == "M05_NOT_ACTIVATED"
            sd.assert_no_change(sd.delta(before, sd.counts(sd_install)))
            sc.change(sd_install, "grant")
            granted = sd.counts(sd_install)
            run = await chain.run_once()
            _check_no_reply_run(run, sd.delta(granted, sd.counts(sd_install)))
            window = sa.window(sd_install, reminder)
            assert (window["state"], window["closed_decision"]) == ("no_reply", "no_reply")
            (item,) = sa.items_for(sd_install, patient)
            assert (item["item_kind"], item["source_event_id"]) == (
                "no_reply", window["closed_event_id"])

    asyncio.run(body())


# --- SD-08a: before-commit failure ---------------------------------------------------------


def test_sd08a_before_commit_rolls_back(sd_install: sc.SCInstall) -> None:
    from test_m05_service_postgres import _faulting_pool
    from test_sa_service_postgres import FaultGateway, InjectedFault, _chain

    sd.begin(sd_install)
    patient, phone, (ref,) = sa.fresh_patient(sd_install)
    reminder = uuid.uuid4()
    message = sa.new_id()

    async def intake_side() -> Any:
        async with sd.runtime(sd_install) as rt:
            for key, call in (("m05.intake.dispatch", "dispatch"),
                              ("m05.intake.inbound", "inbound")):
                gateway = FaultGateway(rt.gateway, fail_after=key)
                faulty = sd.Chain(sd_install, rt, gateway=gateway)
                before = sd.counts(sd_install)
                with pytest.raises(Exception) as caught:  # noqa: B017 - identity below
                    if call == "dispatch":
                        await faulty.dispatch(ref, reminder)
                    else:
                        await faulty.inbound(phone, "reschedule", message)
                assert gateway.fired == [(key, "after")], gateway.fired
                assert any(isinstance(e, InjectedFault) for e in _chain(caught.value))
                sd.assert_no_change(sd.delta(before, sd.counts(sd_install)))
                chain = sd.Chain(sd_install, rt)
                if call == "dispatch":
                    result = await chain.dispatch(ref, reminder)
                    assert result.outcome == "registered"
                    assert sd.delta(before, sd.counts(sd_install)) == sd.DISPATCH
                else:
                    result = await chain.inbound(phone, "reschedule", message)
                    assert (result.outcome, result.decision) == ("registered", "item")
                    assert sd.delta(before, sd.counts(sd_install)) == sd.reply_item(
                        "reschedule_request")
            # Close: the runner's close transaction fails before COMMIT.
            patient2, _, (ref2,) = sa.fresh_patient(sd_install)
            due = await sd.Chain(sd_install, rt).dispatch(ref2)
            sa.shift_window(sd_install, str(due.reminder_id), -25)
            gate = sr.Gate(rt.gateway)
            gate.fail_close = {str(due.reminder_id)}
            before = sd.counts(sd_install)
            failed = await sd.Chain(sd_install, rt).run_once(gate)
            assert (failed.code, failed.attempted, dict(failed.failed)) == (
                "COMPLETE", 1, {"INTERNAL": 1})
            sd.assert_no_change(sd.delta(before, sd.counts(sd_install)))
            recovered = await sd.Chain(sd_install, rt).run_once()
            _check_no_reply_run(recovered, sd.delta(before, sd.counts(sd_install)))
            assert len(sa.items_for(sd_install, patient2)) == 1
            return result.item_id

    item = asyncio.run(intake_side())
    assert len(sa.items_for(sd_install, patient)) == 1

    async def staff_side() -> None:
        fired: list[str] = []
        async with sd.runtime(sd_install) as rt:
            assert (await sd.Chain(sd_install, rt).claim(item)).outcome == "applied"
        before = sd.counts(sd_install)
        async with sd.runtime(sd_install,
                              pool_class=_faulting_pool("raise_no_commit", fired)) as rt:
            chain = sd.Chain(sd_install, rt)
            key = uuid.uuid4()
            from haloflow.m05.service import ActionCommitUnknown

            with pytest.raises(ActionCommitUnknown):
                await chain.resolve(item, "rescheduled", key)
            assert fired == ["raise_no_commit"]
            sd.assert_no_change(sd.delta(before, sd.counts(sd_install)))
            retry = await chain.resolve(item, "rescheduled", key)
            assert retry.outcome == "applied"
            assert sd.delta(before, sd.counts(sd_install)) == sd.resolve("reschedule_request")

    asyncio.run(staff_side())


# --- SD-08b: after-commit uncertainty --------------------------------------------------------


def _converged(before: Any, after: Any, expected: dict[str, int]) -> None:
    """The SD-08b convergence oracle: exactly one success's rows (NC-5 drives it)."""

    assert sd.delta(before, after) == expected, sd.delta(before, after)


def test_sd08b_after_commit_uncertainty(sd_install: sc.SCInstall) -> None:
    from test_m05_service_postgres import _faulting_pool
    from test_sa_service_postgres import FaultGateway

    from haloflow.m05.intake import IntakeCommitUnknown
    from haloflow.m05.service import ActionCommitUnknown

    sd.begin(sd_install)
    _, phone, (ref,) = sa.fresh_patient(sd_install)
    reminder = uuid.uuid4()
    message = sa.new_id()

    async def intake_side() -> Any:
        async with sd.runtime(sd_install) as rt:
            before = sd.counts(sd_install)
            lost = sd.Chain(sd_install, rt, gateway=FaultGateway(rt.gateway, lose_ack=True))
            with pytest.raises(IntakeCommitUnknown):
                await lost.dispatch(ref, reminder)
            committed = sd.counts(sd_install)
            assert sd.delta(before, committed) == sd.DISPATCH
            retry = await sd.Chain(sd_install, rt).dispatch(ref, reminder)
            assert retry.outcome == "existing"
            _converged(before, sd.counts(sd_install), sd.DISPATCH)

            before = sd.counts(sd_install)
            with pytest.raises(IntakeCommitUnknown):
                await lost.inbound(phone, "reschedule", message)
            stored = sa.admin(sd_install, sa.q(sd_install,
                                               "SELECT inbound_id::text FROM"
                                               " {s}.m05_inbound_replies WHERE"
                                               " provider_message_id = %s"), (message,))
            reply = await sd.Chain(sd_install, rt).inbound(phone, "reschedule", message)
            assert (reply.outcome, [(str(reply.inbound_id),)]) == ("existing", stored)
            _converged(before, sd.counts(sd_install), sd.reply_item("reschedule_request"))

            # Close: committed by the runner, acknowledgement lost.
            patient2, _, (ref2,) = sa.fresh_patient(sd_install)
            due = await sd.Chain(sd_install, rt).dispatch(ref2)
            sa.shift_window(sd_install, str(due.reminder_id), -25)
            gate = sr.Gate(rt.gateway)
            gate.lose_ack = {str(due.reminder_id)}
            before = sd.counts(sd_install)
            run = await sd.Chain(sd_install, rt).run_once(gate)
            assert (run.code, run.attempted, run.commit_unknown) == ("COMPLETE", 1, 1)
            replay = await sd.Chain(sd_install, rt).close(due.reminder_id)
            (item,) = sa.items_for(sd_install, patient2)
            assert (replay.outcome, replay.decision, str(replay.item_id)) == (
                "existing", "no_reply", item["item_id"])
            _converged(before, sd.counts(sd_install), sd.NO_REPLY)
            return reply.item_id

    item = asyncio.run(intake_side())

    async def staff_side() -> None:
        async with sd.runtime(sd_install) as rt:
            assert (await sd.Chain(sd_install, rt).claim(item)).outcome == "applied"
        fired: list[str] = []
        before = sd.counts(sd_install)
        async with sd.runtime(sd_install,
                              pool_class=_faulting_pool("commit_then_raise", fired)) as rt:
            chain = sd.Chain(sd_install, rt)
            key = uuid.uuid4()
            with pytest.raises(ActionCommitUnknown):
                await chain.resolve(item, "rescheduled", key)
            assert fired == ["commit_then_raise"]
            retry = await chain.resolve(item, "rescheduled", key)
            assert (retry.outcome, retry.snapshot["version"]) == ("replayed", 3)
            _converged(before, sd.counts(sd_install), sd.resolve("reschedule_request"))
            await sd.assert_resolved(chain, item, "rescheduled", kind="reschedule_request",
                                     source_event=uuid.UUID(sd.item_row(
                                         sd_install, item)["source_event_id"]))

    asyncio.run(staff_side())


# --- negative controls (each proves its oracle can fail) -----------------------------------


def test_sd_nc1_without_grant_the_chain_refuses(sd_install: sc.SCInstall) -> None:
    sd.begin(sd_install, "revoke")
    _, _, (ref,) = sa.fresh_patient(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            before = sd.counts(sd_install)
            with pytest.raises(Exception) as caught:  # noqa: B017 - exact type asserted
                await sd.Chain(sd_install, rt).dispatch(ref)
            assert _activation_refused(caught.value) == "M05_NOT_ACTIVATED"
            sd.assert_no_change(sd.delta(before, sd.counts(sd_install)))

    asyncio.run(body())


def test_sd_nc2_undue_window_fails_the_close_oracle(sd_install: sc.SCInstall) -> None:
    sd.begin(sd_install)
    _, _, (ref,) = sa.fresh_patient(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            await chain.dispatch(ref)  # no shift_window: not due
            before = sd.counts(sd_install)
            run = await chain.run_once()
            with pytest.raises(AssertionError):
                _check_no_reply_run(run, sd.delta(before, sd.counts(sd_install)))
            assert (run.code, run.attempted) == ("COMPLETE", 0)

    asyncio.run(body())


def test_sd_nc3_resolving_another_item_fails_the_oracle_on_a(sd_install: sc.SCInstall) -> None:
    """v1r1 C1 + Codex pins: A is produced and explicitly claimed (version 2); a separate
    valid B is claimed and resolved SUCCESSFULLY; the shared positive oracle on A must
    then fail (A unchanged), and only that call is inside pytest.raises."""

    sd.begin(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            _, reply_a = await _reply_item(chain, "reschedule")
            _, reply_b = await _reply_item(chain, "unclear")
            a, b = reply_a.item_id, reply_b.item_id
            assert (await chain.claim(a)).outcome == "applied"
            a_before = sd.item_row(sd_install, a)
            a_history = await chain.history(a)
            assert (await chain.claim(b)).outcome == "applied"
            resolved_b = await chain.resolve(b, "rescheduled")
            assert (resolved_b.outcome, resolved_b.snapshot["state"]) == ("applied", "resolved")
            await sd.assert_resolved(chain, b, "rescheduled", kind="unclear_reply",
                                     source_event=reply_b.event_id)
            with pytest.raises(AssertionError):
                await sd.assert_resolved(chain, a, "rescheduled", kind="reschedule_request",
                                         source_event=reply_a.event_id)
            # A unchanged: open, the claimant assigned, version 2, history and links as before.
            assert sd.item_row(sd_install, a) == a_before
            assert (a_before["state"], a_before["assignee_id"], a_before["version"]) == (
                "open", sd.STAFF, 2)
            assert await chain.history(a) == a_history
            assert sd.resolution_events(sd_install, a) == []

    asyncio.run(body())


def test_sd_nc4_new_message_id_fails_the_zero_delta_oracle(sd_install: sc.SCInstall) -> None:
    sd.begin(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            chain = sd.Chain(sd_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sd_install)
            await chain.dispatch(ref)
            await chain.inbound(phone, "reschedule")
            before = sd.counts(sd_install)
            again = await chain.inbound(phone, "reschedule")  # a NEW provider_message_id
            assert again.outcome == "registered"
            with pytest.raises(AssertionError):
                sd.assert_no_change(sd.delta(before, sd.counts(sd_install)))

    asyncio.run(body())


def test_sd_nc5_new_reminder_id_fails_the_convergence_oracle(sd_install: sc.SCInstall) -> None:
    from test_sa_service_postgres import FaultGateway

    from haloflow.m05.intake import IntakeCommitUnknown

    sd.begin(sd_install)
    _, _, (ref,) = sa.fresh_patient(sd_install)

    async def body() -> None:
        async with sd.runtime(sd_install) as rt:
            before = sd.counts(sd_install)
            lost = sd.Chain(sd_install, rt, gateway=FaultGateway(rt.gateway, lose_ack=True))
            with pytest.raises(IntakeCommitUnknown):
                await lost.dispatch(ref, uuid.uuid4())
            retry = await sd.Chain(sd_install, rt).dispatch(ref, uuid.uuid4())  # NOT identical
            assert retry.outcome == "registered"
            with pytest.raises(AssertionError):
                _converged(before, sd.counts(sd_install), sd.DISPATCH)

    asyncio.run(body())
