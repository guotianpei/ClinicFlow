"""L-1S package S-I — end-to-end harness rows E01-E06, PostgreSQL 17 (RED BY DESIGN).

Governing and pins: see `si_support`. Each row STARTS from synthetic directory ingestion
through the rebound gateway key (`m05.intake.directory` -> m05_directory_v3, provider and
department included), drives ONE ReminderScheduler tick whose runner holds the
BaselineWorker (FakeSender only), delivers the patient reply through the S-G1a synthetic
HTTP ingress (in process, as `sh3.post_reply`), discovers and runs write-back through
WritebackService + WritebackWorker against SimulatedEhr (synthetic; never a real EHR),
and works any staff item through StaffServiceV3. Each row runs on a FRESH tenant. Nothing
is sent to the patient by write-back; the S-A side-effect guard is active.

- E01  reply 1, EHR neutral and matching -> one write, verified, observed_positive, no item.
- E02  reply 1, EHR shows another provider -> no write, item "status_changed"; staff read
       the reason, claim and resolve it.
- E03  reply 1, EHR unreachable -> three deferred retries, then item "ehr_unreachable";
       never a write.
- E04  delayed writer: A spends its permit and pauses before the call; after the deadline
       + margin B reconciles (closes the permit, reads, raises "could_not_verify"); A
       resumes: the port refuses (permit closed), A's settlement is stale; one item, no
       write.
- E05  restart after the spend: a new runtime reconciles; exactly one outcome, no second
       write.
- E06  STOP -> no write-back op, no write, no item.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any, Final

import m02_support
import pytest
import sa_support as sa
import sd_support as sd
import sh2_support as sh2
import sh3_support as sh3
import si_support as si

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]

SECRET: Final = "c" * 32 + "-si-ingress-A"
BOUND_S: Final = 60.0


@pytest.fixture
def si_e(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
         m05_reset: Any) -> Iterator[si.SIInstall]:
    yield from si.module_install(request, m02_ids, m05_reset, f"e:{request.node.name}")


def _begin(install: si.SIInstall) -> si.SIInstall:
    install.require()
    sd.begin(install)  # type: ignore[arg-type]
    return install


async def _b(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, BOUND_S)


async def _ingest(rt: Any, install: si.SIInstall, **identity: Any) -> tuple[str, str, str]:
    """Synthetic directory ingestion through the REBOUND key (v3): contact + one booked,
    due, in-window appointment with provider and department."""

    zone, start = sh2.in_window(install)
    patient, phone, ref = sa.new_id(), sa.fresh_phone(), sa.new_id()
    contact = await sh2.gateway_directory(rt, install, "contact_put", phone=phone,
                                          patient_id=patient)
    assert contact["outcome"] in ("applied", "existing"), contact
    result = await sh2.gateway_directory(rt, install, "appointment_put",
                                         **si.appointment3(ref, patient, start,
                                                           department_tz=zone, **identity))
    assert result["outcome"] == "applied", result
    return patient, phone, ref


async def _tick(rt: Any, install: si.SIInstall) -> Any:
    """One ReminderScheduler tick; the runner's worker is BaselineWorker(inner, capture);
    the scheduler keeps the inner OutboundWorker (v1r1 P2 composition)."""

    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.reminder import ReminderRunner
    from haloflow.m05.reminder_scheduler import ReminderScheduler
    from haloflow.m05.writeback import BaselineWorker

    sender = FakeSender()
    svc = sh3.service(rt)
    inner = sh3.worker(svc, sender)
    runner = ReminderRunner(svc, BaselineWorker(inner, si.wb_service(rt).capture_baseline),
                            batch=10, lease_seconds=120)
    scheduler = ReminderScheduler(contexts=sh3.dispatch_contexts(rt, [install]), service=svc,
                                  worker=inner, runner=runner, tenants=(install.tenant_id,),
                                  interval_s=300, stop_timeout_s=10, recovery_limit=100)
    try:
        result = await _b(scheduler.tick())
    finally:
        await _b(scheduler.stop())
    assert sh3.report(result, install).ready.counts == sh3.ready_counts(1, 1, 1)
    return sender


async def _reminded_and_replied(rt: Any, install: si.SIInstall, text: str = "1",
                                **identity: Any) -> dict[str, str]:
    patient, phone, ref = await _ingest(rt, install, **identity)
    sender = await _tick(rt, install)
    assert [c[1] for c in sender.calls] == [phone]
    op = sh2.op_for(install, ref)
    assert op is not None and si.baseline(install, str(op["op_id"])) is not None
    app = sh3.ingress_app(rt, {install.tenant_id: SECRET})
    message = sa.new_id()
    status, body = await sh3.post_reply(app, SECRET, phone, text, message)
    assert (status, body.get("outcome")) == (202, "registered"), body
    return {"patient": patient, "phone": phone, "ref": ref, "op_id": str(op["op_id"]),
            "message": message}


async def _discover(rt: Any, install: si.SIInstall) -> list[str]:
    svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
    await _b(svc.discover(ctx, 100))
    return [o["wb_op_id"] for o in await _b(svc.due(ctx, 100))]


async def _run(rt: Any, install: si.SIInstall, wb: str, ehr: Any) -> str:
    from haloflow.m05.writeback import WritebackWorker

    ctx = await si.wb_context(rt, install)
    outcome: str = await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb))
    return outcome


# --- E01 ------------------------------------------------------------------------------------


def test_si_e01_confirmed_and_matching_is_written_once_and_observed(si_e: si.SIInstall) -> None:
    install = _begin(si_e)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await _reminded_and_replied(rt, install)
            (wb,) = await _discover(rt, install)
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install))
            assert await _run(rt, install, wb, ehr) == "observed_positive"
            assert [w[0] for w in ehr.writes] == [flow["ref"]]
            assert (await ehr.read(flow["ref"])).status == "positive"
            assert si.check_items(install) == [] and si.check_events(install) == []
            assert si.wb_op(install, wb)["state"] == "observed_positive"

    asyncio.run(body())


# --- E02 ------------------------------------------------------------------------------------


def test_si_e02_another_provider_raises_status_changed_and_staff_resolve_it(
        si_e: si.SIInstall) -> None:
    install = _begin(si_e)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await _reminded_and_replied(rt, install)
            (wb,) = await _discover(rt, install)
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install),
                                   provider_ref="prov-other")
            assert await _run(rt, install, wb, ehr) == "item_raised"
            assert ehr.writes == []
            (item_row,) = si.check_items(install)
            staff = si.StaffChain(install, rt)
            item = uuid.UUID(item_row["item_id"])
            assert (await _b(staff.reason(item)))["reason"] == "status_changed"
            assert (await _b(staff.chain.claim(item))).outcome == "applied"
            assert (await _b(staff.chain.resolve(item, "reached_no_change"))).outcome \
                == "applied"
            await sd.assert_resolved(staff.chain, item, "reached_no_change", kind=si.ITEM_KIND,
                                     source_event=uuid.UUID(item_row["source_event_id"]))
            assert item_row["patient_id"] == flow["patient"]

    asyncio.run(body())


# --- E03 ------------------------------------------------------------------------------------


def test_si_e03_an_unreachable_ehr_retries_three_times_then_raises_ehr_unreachable(
        si_e: si.SIInstall) -> None:
    install = _begin(si_e)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await _reminded_and_replied(rt, install)
            (wb,) = await _discover(rt, install)
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install))
            ehr.fail_reads(10)
            outcomes = []
            for delay in (60, 300, 900, 0):
                outcomes.append(await _run(rt, install, wb, ehr))
                if delay:
                    si.age(install, wb, lease=si.LEASE_S + 1, next_at=delay + 1)
            assert outcomes == ["deferred", "deferred", "deferred", "item_raised"]
            assert ehr.writes == []
            op = si.wb_op(install, wb)
            assert (op["state"], op["reason"], op["r1_failures"]) == ("item", "ehr_unreachable",
                                                                      4)
            assert len(si.check_items(install)) == 1

    asyncio.run(body())


# --- E04 ------------------------------------------------------------------------------------


class PausingEhr:
    """Forwards to a SimulatedEhr, but A's write waits at the port boundary until released
    (A spent its permit and paused before W1)."""

    def __init__(self, ehr: Any) -> None:
        self.ehr = ehr
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.write_results: list[Any] = []

    async def read(self, appointment_id: str) -> Any:
        return await self.ehr.read(appointment_id)

    async def close_permit(self, permit_id: str) -> None:
        await self.ehr.close_permit(permit_id)

    async def write_confirmation(self, appointment_id: str, **kw: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        result = await self.ehr.write_confirmation(appointment_id, **kw)
        self.write_results.append(result)
        return result


def test_si_e04_a_delayed_writer_is_refused_after_the_reconciler_settles(
        si_e: si.SIInstall) -> None:
    install = _begin(si_e)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackWorker

        async with si.runtime(install) as rt:
            flow = await _reminded_and_replied(rt, install)
            (wb,) = await _discover(rt, install)
            shared = si.ehr_from_flow(install, flow, si.db_clock(install))
            a_port = PausingEhr(shared)
            ctx_a = await si.wb_context(rt, install)
            a = asyncio.create_task(WritebackWorker(si.wb_service(rt), a_port,
                                                    write_timeout_s=BOUND_S)
                                    .run_one(ctx_a, wb))
            await _b(a_port.entered.wait())  # A has spent and is paused before the call
            assert si.wb_op(install, wb)["state"] == "spent"
            si.age(install, wb, lease=si.LEASE_S + 1,
                   deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
            assert await _run(rt, install, wb, shared) == "item_raised"  # B reconciles
            a_port.release.set()
            assert await _b(a) == "stale"
            assert [r.outcome for r in a_port.write_results] == ["permit_closed"]
            assert shared.writes == []
            (item,) = si.check_items(install)
            assert si.wb_op(install, wb)["reason"] == "could_not_verify"
            assert item["item_kind"] == si.ITEM_KIND

    asyncio.run(body())


# --- E05 ------------------------------------------------------------------------------------


def test_si_e05_restart_after_the_spend_reconciles_once(si_e: si.SIInstall) -> None:
    install = _begin(si_e)

    async def body() -> None:
        from haloflow.m05.simulated_ehr import SimulatedEhr

        clock = si.db_clock(install)
        async with si.runtime(install) as rt:
            flow = await _reminded_and_replied(rt, install)
            (wb,) = await _discover(rt, install)
            ehr = si.ehr_from_flow(install, flow, clock)
            svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
            claimed = await _b(svc.claim(ctx, wb))
            fence = int(claimed["fence"])
            decided = await _b(svc.decide(ctx, wb, fence, await ehr.read(flow["ref"]), False))
            assert (await _b(svc.spend(ctx, wb, fence, decided["permit_id"])))["outcome"] \
                == "spent"
        ehr = SimulatedEhr.restore(ehr.snapshot(), clock=clock)
        si.age(install, wb, lease=si.LEASE_S + 1,
               deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
        async with si.runtime(install) as rt:
            assert await _run(rt, install, wb, ehr) == "item_raised"
            assert await _run(rt, install, wb, ehr) == "not_claimed"
        assert ehr.writes == [] and len(si.check_items(install)) == 1

    asyncio.run(body())


# --- E06 ------------------------------------------------------------------------------------


def test_si_e06_stop_creates_no_write_back(si_e: si.SIInstall) -> None:
    install = _begin(si_e)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await _reminded_and_replied(rt, install, text="STOP")
            assert sh3.window_of(install, flow["ref"])["state"] == "opted_out"
            assert await _discover(rt, install) == []
            assert si.wb_ops(install) == [] and si.check_items(install) == []

    asyncio.run(body())
