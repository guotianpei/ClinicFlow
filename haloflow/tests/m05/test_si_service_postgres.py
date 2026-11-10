"""L-1S package S-I — PostgreSQL 17 rows through the services (RED BY DESIGN).

Governing and pins: see `si_support` (SQL pins and service pins). Each row installs a
fresh S-I tenant in its body, so the expected red cause is ModuleNotFoundError for
`haloflow.m05.writeback*` / `haloflow.m05.simulated_ehr`.

Rows (requirement in brackets, packet v1 numbering; Codex pins in parentheses):
- P-I01 [SI-R11] an item decision appends exactly one check event and creates exactly one
  item, atomically, under the real gateway (pin 3).
- P-I02 [R11] a failure inside the item transaction (create refused) rolls back the
  decision, the event and the item: no orphan; the later decision mints one identity.
- P-I03 [R11] the retry after a committed item never creates a second event or item (the
  op is no longer claimable; the stored identity is reused) (pin 3).
- P-I04 [R11/R14] two concurrent settlements of one op with distinct candidate events:
  one event, one item, the other stale (pin 3).
- P-I05 [R11] the real staff path: claim -> resolve through prepare -> M02 append (contract
  v2) -> apply -> history (pin 1).
- P-I06 [R11] the four existing kinds still resolve through v3 with contract v2 events, and
  the S-I tenant still creates them through intake (regression).
- P-I07 [R11] the authorized reason read: one of the five reasons, exactly the pinned keys;
  an old-kind item, an unknown item and another tenant's item are ITEM_NOT_FOUND; no
  read capability -> refused (pin 2).
- P-I08 [R11] nothing is sent to the patient: no sender call, no outbound op.
- P-A01..A03 [R12] activation off: discover / check claim / decide / spend refused; a
  permit spent before the switch went off completes its single call; settlement and
  reconcile continue.
- P-T01..T03 [R13] another tenant's op is not visible; a context without the S-I
  capabilities or not a workload is refused before any I/O.
- P-P01 [R15] restart between every step resumes and never writes twice.
- P-V01 [R16] the check event metadata is exactly {wb_op_id, appointment_ref, reason}; no
  phone, name, provider or department in events, items or logs.
- P-W10 / P-W11 [R4] BaselineWorker on the real runtime: a capture that FAILS in the
  database (permission denied) leaves a usable connection for the inner send; the H3 tick
  with the wrapper injected gives the identical ready counts and a baseline.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import m02_support
import pytest
import sa_support as sa
import sc_support as sc
import sd_support as sd
import service_support as sv
import sh2_support as sh2
import sh3_support as sh3
import sh_support as sh
import si_support as si

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]
BOUND_S = 60.0


@pytest.fixture
def si_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[si.SIInstall]:
    yield from si.module_install(request, m02_ids, m05_reset, f"s:{request.node.name}")


@pytest.fixture
def si_other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
             m05_reset: Any) -> Iterator[si.SIInstall]:
    yield from si.module_install(request, m02_ids, m05_reset, f"s-b:{request.node.name}")


def _begin(install: si.SIInstall) -> si.SIInstall:
    """Install (row body), the S-D staff member and profile, the clinic granted."""

    install.require()
    sd.begin(install)  # type: ignore[arg-type]
    return install


async def _b(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, BOUND_S)


def _read(install: si.SIInstall, flow: dict[str, str], status: str = "neutral",
          **over: Any) -> Any:
    from haloflow.m05.simulated_ehr import EhrRead

    fields: dict[str, Any] = {"status": status, "appointment_id": flow["ref"],
                              "start_at": sh.start_of(install, flow["ref"]),
                              "provider_ref": si.PROVIDER, "department_ref": si.DEPARTMENT,
                              "replaced_by": None}
    fields.update(over)
    return EhrRead(**fields)


def _counts(install: si.SIInstall) -> tuple[int, int, int]:
    return (len(si.check_events(install)), len(si.check_items(install)),
            len(sa.admin(install, sa.q(install, "SELECT op_id FROM {s}.m05_outbound_ops"))))


async def _item_decision(rt: Any, install: si.SIInstall, status: str = "negative"
                         ) -> tuple[dict[str, str], str, dict[str, Any]]:
    """A checked op decided through the SERVICE to a staff item."""

    flow, wb, fence = si.checked(install)
    svc = si.wb_service(rt)
    ctx = await si.wb_context(rt, install)
    result = await _b(svc.decide(ctx, wb, fence, _read(install, flow, status), False))
    assert result["outcome"] == "item", result
    return flow, wb, result


# --- P-I: the staff item path (Option A) ---------------------------------------------------


def test_si_pi01_an_item_decision_is_one_event_and_one_item_atomically(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow, wb, result = await _item_decision(rt, install)
            (event,) = si.check_events(install)
            (item,) = si.check_items(install)
            op = si.wb_op(install, wb)
            assert event["event_id"] == op["event_id"] == result["event_id"]
            assert (item["item_id"], item["source_event_id"], item["patient_id"]) == (
                result["item_id"], event["event_id"], flow["patient"])
            assert op["item_id"] == item["item_id"] and op["reason"] == "declined_or_unknown"
            assert event["metadata"] == {"wb_op_id": wb, "appointment_ref": flow["ref"],
                                         "reason": "declined_or_unknown"}
            assert (event["contract_version"], event["correlation_id"]) == (1, wb)

    asyncio.run(body())


def test_si_pi02_a_failed_item_transaction_leaves_no_orphan(si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow, wb, fence = si.checked(install)
            mapping = sa.q(install, "DELETE FROM {s}.m05_intake_map WHERE source_event_type = %s"
                                    " AND discriminator_value = 'declined_or_unknown'")
            sa.admin(install, mapping, (si.T_CHECK,))  # FIXTURE: create v2 must refuse
            svc = si.wb_service(rt)
            ctx = await si.wb_context(rt, install)
            with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal class
                await _b(svc.decide(ctx, wb, fence, _read(install, flow, "negative"), False))
            op = si.wb_op(install, wb)
            assert (op["state"], op["event_id"], op["item_id"]) == ("eligible_check", None,
                                                                    None)
            assert si.check_events(install) == [] and si.check_items(install) == []
            sa.admin(install, sa.q(install, "INSERT INTO {s}.m05_intake_map (source_event_type,"
                                            " source_contract_version, discriminator_path,"
                                            " discriminator_value, item_kind,"
                                            " producer_workload_id, active_in_production)"
                                            " VALUES (%s, 1, 'reason', 'declined_or_unknown',"
                                            " %s, %s, false)"),
                     (si.T_CHECK, si.ITEM_KIND, sa.W_DISPATCH))
            again = await _b(svc.decide(ctx, wb, fence, _read(install, flow, "negative"),
                                        False))
            assert again["outcome"] == "item", again
            assert len(si.check_events(install)) == 1 and len(si.check_items(install)) == 1

    asyncio.run(body())


def test_si_pi03_a_retry_after_a_committed_item_creates_nothing(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackWorker

        async with si.runtime(install) as rt:
            flow, wb, _result = await _item_decision(rt, install)
            before = _counts(install)
            # the acknowledgement was lost: the worker runs the whole op again
            ctx = await si.wb_context(rt, install)
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install), "negative")
            outcome = await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb))
            assert outcome == "not_claimed"
            assert _counts(install) == before
            assert si.claim(install, wb)["outcome"] == "not_claimable"

    asyncio.run(body())


def test_si_pi04_concurrent_settlements_make_one_event_and_one_item(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install, max_size=4) as rt:
            flow, wb, fence, permit = si.permitted(install)
            assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
            svc = si.wb_service(rt)
            ctx_a, ctx_b = (await si.wb_context(rt, install), await si.wb_context(rt, install))
            results = await _b(asyncio.gather(
                svc.settle(ctx_a, wb, fence, permit, "unknown", None),
                svc.settle(ctx_b, wb, fence, permit, "unknown", None),
                return_exceptions=True))
            outcomes = sorted(r["outcome"] if isinstance(r, dict) else type(r).__name__
                              for r in results)
            assert outcomes == ["item", "stale"], results
            assert len(si.check_events(install)) == 1 and len(si.check_items(install)) == 1
            assert si.wb_op(install, wb)["event_id"] == si.check_events(install)[0]["event_id"]

    asyncio.run(body())


def test_si_pi05_staff_claim_and_resolve_through_v3_with_a_contract_v2_event(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            _flow, _wb, result = await _item_decision(rt, install)
            staff = si.StaffChain(install, rt)
            item, source = uuid.UUID(result["item_id"]), uuid.UUID(result["event_id"])
            assert (await _b(staff.chain.claim(item))).outcome == "applied"
            assert (await _b(staff.chain.resolve(item, "reached_no_change"))).outcome \
                == "applied"
            await sd.assert_resolved(staff.chain, item, "reached_no_change", kind=si.ITEM_KIND,
                                     source_event=source)
            (event,) = sd.resolution_events(install, item)  # type: ignore[arg-type]
            assert event["contract_version"] == 2

    asyncio.run(body())


@pytest.mark.parametrize(("reply_class", "kind"), [("reschedule", "reschedule_request"),
                                                   ("cancel", "cancel_request"),
                                                   ("unclear", "unclear_reply")])
def test_si_pi06_existing_kinds_still_work_through_v3(si_install: si.SIInstall,
                                                       reply_class: str, kind: str) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = si.sent(install)
            result = si.replied(install, flow, reply_class)
            assert result["decision"] == "item", result
            staff = si.StaffChain(install, rt)
            (row,) = sa.items_for(install, flow["patient"])
            item, source = uuid.UUID(row["item_id"]), uuid.UUID(row["source_event_id"])
            assert row["item_kind"] == kind
            assert (await _b(staff.chain.claim(item))).outcome == "applied"
            assert (await _b(staff.chain.resolve(item, "rescheduled"))).outcome == "applied"
            await sd.assert_resolved(staff.chain, item, "rescheduled", kind=kind,
                                     source_event=source)

    asyncio.run(body())


def test_si_pi07_the_reason_read_is_authorized_narrow_and_tenant_local(
        si_install: si.SIInstall, si_other: si.SIInstall) -> None:
    install, other = _begin(si_install), _begin(si_other)

    async def body() -> None:
        from haloflow.m05.service import ReadFailed

        async with si.runtime(install) as rt:
            mine, _wb, result = await _item_decision(rt, install)
            staff = si.StaffChain(install, rt)
            item = uuid.UUID(result["item_id"])
            read = await _b(staff.reason(item))
            assert read == {"schema_version": 1, "item_id": str(item),
                            "reason": "declined_or_unknown"}
            old = si.sent(install)
            si.replied(install, old, "reschedule")
            (old_item,) = sa.items_for(install, old["patient"])
            other_flow, _owb, other_result = await _item_decision(rt, other)
            for missing in (uuid.UUID(old_item["item_id"]), uuid.uuid4(),
                            uuid.UUID(other_result["item_id"])):
                with pytest.raises(ReadFailed) as failed:
                    await _b(staff.reason(missing))
                assert failed.value.reason_code == "ITEM_NOT_FOUND", missing
            with pytest.raises(Exception) as denied:  # noqa: PT011 - capability refusal
                await _b(staff.reason(item, caps=frozenset({sv.PREPARE})))
            assert "CAPABILITY" in repr(denied.value).upper()
            assert other_flow["patient"] != mine["patient"]

    asyncio.run(body())


def test_si_pi08_write_back_never_sends_to_the_patient(si_install: si.SIInstall) -> None:
    """A full write-back run (permit, write, verify) and an item run create no outbound op
    and no dispatch observation: write-back has no sender at all."""

    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackWorker

        async with si.runtime(install) as rt:
            positive, wb_pos = si.confirmed_op(install)
            negative, wb_neg = si.confirmed_op(install)
            ops = _counts(install)[2]
            dispatches = sh3.dispatch_events(install, positive["op_id"]) + \
                sh3.dispatch_events(install, negative["op_id"])
            ctx = await si.wb_context(rt, install)
            clock = si.db_clock(install)
            for flow, wb, status, expected in ((positive, wb_pos, "neutral",
                                                "observed_positive"),
                                               (negative, wb_neg, "negative", "item_raised")):
                ehr = si.ehr_from_flow(install, flow, clock, status)
                assert await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb)) \
                    == expected
            assert _counts(install)[2] == ops
            assert sh3.dispatch_events(install, positive["op_id"]) + \
                sh3.dispatch_events(install, negative["op_id"]) == dispatches

    asyncio.run(body())


# --- P-A: activation -------------------------------------------------------------------------


def test_si_pa01_switch_off_refuses_new_work(si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m01.errors import ActivationRefused

        async with si.runtime(install) as rt:
            flow, wb = si.confirmed_op(install)
            svc = si.wb_service(rt)
            sc.change(install, "revoke")
            ctx = await si.wb_context(rt, install)
            for call in (svc.discover(ctx, 10), svc.decide(ctx, wb, 0, _read(install, flow),
                                                           False),
                         svc.spend(ctx, wb, 0, si.new_uuid())):
                with pytest.raises(ActivationRefused):
                    await _b(call)
            claimed = await _b(svc.claim(ctx, wb))  # check refused; reconcile not applicable
            assert claimed["outcome"] == "not_claimable", claimed
            assert si.wb_op(install, wb)["state"] == "pending"

    asyncio.run(body())


def test_si_pa02_a_permit_spent_before_the_switch_completes_its_single_call(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow, wb, fence, permit = si.permitted(install)
            spent = si.spend(install, wb, fence, permit)
            sc.change(install, "revoke")
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install))
            write = await _b(ehr.write_confirmation(
                flow["ref"], permit_id=permit, apply_deadline=datetime.fromisoformat(
                    spent["apply_deadline"])))
            assert write.outcome == "applied" and len(ehr.writes) == 1
            ctx = await si.wb_context(rt, install)
            settled = await _b(si.wb_service(rt).settle(
                ctx, wb, fence, permit, "applied", await ehr.read(flow["ref"])))
            assert settled["outcome"] == "observed_positive", settled

    asyncio.run(body())


def test_si_pa03_switch_off_settlement_and_reconcile_continue(si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow, wb, fence, permit = si.permitted(install)
            assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
            sc.change(install, "revoke")
            si.age(install, wb, lease=si.LEASE_S + 1,
                   deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
            svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
            claimed = await _b(svc.claim(ctx, wb))
            assert (claimed["outcome"], claimed["phase"]) == ("claimed", "reconcile"), claimed
            settled = await _b(svc.settle(ctx, wb, int(claimed["fence"]), permit, "unknown",
                                          None))
            assert settled["outcome"] == "item" and len(si.check_items(install)) == 1
            _ = flow

    asyncio.run(body())


# --- P-T: tenant and authorization ---------------------------------------------------------


def test_si_pt01_another_tenants_op_is_not_visible(si_install: si.SIInstall,
                                                  si_other: si.SIInstall) -> None:
    install, other = _begin(si_install), _begin(si_other)
    _flow, wb_b = si.confirmed_op(other)
    before = si.wb_op(other, wb_b)
    result = si.claim(install, wb_b)
    assert result["outcome"] in ("not_due", "not_claimable"), result
    assert si.call(install, "due", limit=1000)["ops"] == []
    assert si.wb_op(other, wb_b) == before


@pytest.mark.parametrize("case", ["missing_capability", "extra_capability", "actor"])
def test_si_pt02_03_admission_is_refused_before_any_io(si_install: si.SIInstall,
                                                       case: str) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackRefused

        async with si.runtime(install) as rt:
            _flow, wb = si.confirmed_op(install)
            caps = si.wb_caps()
            if case == "missing_capability":
                ctx = await si.wb_context(rt, install, caps=frozenset(caps - {si.WB_CAP}))
            elif case == "extra_capability":
                ctx = await si.wb_context(rt, install, caps=caps | {"m05.intake.inbound"})
            else:
                ctx = await si.wb_context(rt, install, principal=sd.STAFF, kind="actor")
            before = si.wb_op(install, wb)
            with pytest.raises(WritebackRefused) as refused:
                await _b(si.wb_service(rt).claim(ctx, wb))
            assert refused.value.code in ("CAPABILITIES_INVALID", "PRINCIPAL_NOT_WORKLOAD")
            assert si.wb_op(install, wb) == before

    asyncio.run(body())


# --- P-P: restart ---------------------------------------------------------------------------


@pytest.mark.parametrize("stop_after", ["claim", "decide", "spend", "write"])
def test_si_pp01_restart_between_steps_never_writes_twice(si_install: si.SIInstall,
                                                          stop_after: str) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.simulated_ehr import SimulatedEhr
        from haloflow.m05.writeback import WritebackWorker

        clock = si.db_clock(install)
        flow, wb = si.confirmed_op(install)
        ehr = si.ehr_from_flow(install, flow, clock)
        async with si.runtime(install) as rt:  # first process: stops after `stop_after`
            svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
            claimed = await _b(svc.claim(ctx, wb))
            fence = int(claimed["fence"])
            if stop_after != "claim":
                decided = await _b(svc.decide(ctx, wb, fence, _read(install, flow), False))
                permit = decided["permit_id"]
                if stop_after in ("spend", "write"):
                    spent = await _b(svc.spend(ctx, wb, fence, permit))
                    if stop_after == "write":
                        await _b(ehr.write_confirmation(
                            flow["ref"], permit_id=permit,
                            apply_deadline=datetime.fromisoformat(spent["apply_deadline"])))
        ehr = SimulatedEhr.restore(ehr.snapshot(), clock=clock)  # the EHR keeps its state
        si.age(install, wb, lease=si.LEASE_S + 1,
               deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
        async with si.runtime(install) as rt:  # second process
            ctx = await si.wb_context(rt, install)
            outcome = await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb))
        # claim / decide: the next process voids, re-decides and writes once; spend (no
        # write): reconcile only, no write; write: reconcile observes the one write.
        writes, final = {"claim": (1, "observed_positive"), "decide": (1, "observed_positive"),
                         "spend": (0, "item_raised"), "write": (1, "observed_positive")}[
                             stop_after]
        assert (len(ehr.writes), outcome) == (writes, final)
        state = {"observed_positive": "observed_positive", "item_raised": "item"}[final]
        assert si.wb_op(install, wb)["state"] == state

    asyncio.run(body())


# --- P-V: privacy ---------------------------------------------------------------------------


def test_si_pv01_events_items_and_logs_carry_no_sensitive_value(
        si_install: si.SIInstall, caplog: pytest.LogCaptureFixture) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackWorker

        with caplog.at_level(logging.DEBUG):
            async with si.runtime(install) as rt:
                flow, wb = si.confirmed_op(install)
                ehr = si.ehr_from_flow(install, flow, si.db_clock(install), "negative")
                ctx = await si.wb_context(rt, install)
                assert await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb)) \
                    == "item_raised"
        (event,) = si.check_events(install)
        assert set(event["metadata"]) == {"wb_op_id", "appointment_ref", "reason"}
        stored = repr(si.check_events(install)) + repr(si.check_items(install))
        text = caplog.text + stored
        for secret in (flow["phone"], sh2.FIRST_NAME, si.PROVIDER, si.DEPARTMENT):
            assert secret not in text, secret

    asyncio.run(body())


# --- P-W: BaselineWorker on the real runtime ------------------------------------------------


def test_si_pw10_a_capture_failing_in_the_database_leaves_a_usable_connection(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import BaselineWorker

        sa.admin(install, sa.q(install, "REVOKE EXECUTE ON FUNCTION"
                                        " {s}.m05_writeback_capture_baseline(jsonb)"
                                        " FROM haloflow_runtime"))  # FIXTURE: capture fails
        async with si.runtime(install, max_size=1) as rt:  # ONE connection: must be reused
            svc = sh3.service(rt)
            from haloflow.m05.outbound import FakeSender

            inner = sh3.worker(svc, FakeSender())
            wrapper = BaselineWorker(inner, si.wb_service(rt).capture_baseline)
            _p, _ph, ref, _z = si.seed3(install)
            op_id, attempt = sh2.claimed(install, ref)
            ctx = await sh3.dispatch_context(rt, install)
            outcome = await _b(wrapper.run_claimed(ctx, op_id, attempt))
            assert outcome == "accepted", outcome
            assert si.baseline(install, op_id) is None
            assert sh.op_row(install, op_id)["state"] in ("accepted", "registered")

    asyncio.run(body())


def test_si_pw11_h3_tick_with_the_wrapper_is_equivalent_and_captures(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.outbound import FakeSender
        from haloflow.m05.reminder import ReminderRunner
        from haloflow.m05.reminder_scheduler import ReminderScheduler
        from haloflow.m05.writeback import BaselineWorker

        async with si.runtime(install) as rt:
            _p, phone, ref, _z = si.seed3(install)
            sender = FakeSender()
            svc = sh3.service(rt)
            inner = sh3.worker(svc, sender)
            wrapper = BaselineWorker(inner, si.wb_service(rt).capture_baseline)
            runner = ReminderRunner(svc, wrapper, batch=10, lease_seconds=120)
            scheduler = ReminderScheduler(
                contexts=sh3.dispatch_contexts(rt, [install]), service=svc, worker=inner,
                runner=runner, tenants=(install.tenant_id,), interval_s=300,
                stop_timeout_s=10, recovery_limit=100)
            try:
                result = await _b(scheduler.tick())
            finally:
                await _b(scheduler.stop())
            assert sh3.report(result, install).ready.counts == sh3.ready_counts(1, 1, 1)
            assert [c[1] for c in sender.calls] == [phone]
            op = sh2.op_for(install, ref)
            assert op is not None and si.baseline(install, str(op["op_id"])) is not None

    asyncio.run(body())

