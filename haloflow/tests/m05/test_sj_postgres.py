"""L-1S package S-J: combined synthetic lifecycle rows, PostgreSQL 17.

Two classes of row (the red classification Codex asked for, 20261110T210601Z):
- HARNESS CONTROLS (green now; no S-J code): J11c, J13, J14, J15 exercise the TEST-ONLY
  staff router over a check item made by the accepted-candidate S-I path WITHOUT the S-J
  scheduler (`sj_harness.check_item_without_scheduler`). They prove the harness, not S-J.
  S-I is NOT accepted (final CI pending); a failure here is a baseline/dependency failure.
- S-J ROWS (RED BY DESIGN): every other row builds the combined stack, whose
  `WritebackScheduler` import raises ModuleNotFoundError until the module exists.

Governing: S-J test-case preparation v1 and the Codex review pins record (Work Session
2026-11-10). The staff routes use a synthetic test principal and do NOT claim acceptance of
production staff authentication. F-H3-2 is kept: no row asserts time fairness.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Iterator
from typing import Any, Final

import m02_support
import pytest
import sa_support as sa
import sd_support as sd
import sh2_support as sh2
import sh3_support as sh3
import sh_support as sh
import si_support as si
import sj_harness as sj

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]

BOUND_S: Final = 60.0


@pytest.fixture
def sj_i(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
         m05_reset: Any) -> Iterator[si.SIInstall]:
    yield from si.module_install(request, m02_ids, m05_reset, f"j:{request.node.name}")


def _begin(install: si.SIInstall) -> si.SIInstall:
    install.require()
    sd.begin(install)  # type: ignore[arg-type]
    return install


async def _b(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, BOUND_S)


def _item_state(install: si.SIInstall) -> tuple[Any, ...]:
    items = sorted(tuple(sorted((k, str(v)) for k, v in r.items()))
                   for r in si.check_items(install))
    events = len(si.check_events(install))
    resolutions = len(si.rows(install, "patient_events", "event_type = %s",
                              (si.T_RESOLUTION,)))
    return tuple(items), events, resolutions


# === HARNESS CONTROLS (green now) ===========================================================


def test_sj_j11c_reason_and_get_over_the_test_route_bind_the_item(sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await sj.check_item_without_scheduler(install, rt)
            app = sj.staff_app(install, rt)
            status, reason = await sj.http(app, "GET", sj.item_path(install, flow["item_id"],
                                                                     "/reason"),
                                           headers=sj.staff_headers(caps="read"))
            assert status == 200, reason
            assert reason["reason"] == "status_changed"
            status, item = await sj.http(app, "GET", sj.item_path(install, flow["item_id"]),
                                         headers=sj.staff_headers(caps="read"))
            assert status == 200 and str(item.get("item_id")) == flow["item_id"], item
            other = str(uuid.uuid4())
            status, missing = await sj.http(app, "GET", sj.item_path(install, other, "/reason"),
                                            headers=sj.staff_headers(caps="read"))
            assert (status, missing.get("refused")) == (404, "ITEM_NOT_FOUND"), missing

    asyncio.run(body())


def test_sj_j13_wrong_actor_is_refused_without_change(sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await sj.check_item_without_scheduler(install, rt)
            app = sj.staff_app(install, rt)
            before = _item_state(install)
            for principal in (str(uuid.uuid4()), sa.W_DISPATCH):
                status, out = await sj.http(
                    app, "POST", sj.item_path(install, flow["item_id"], "/claim"),
                    headers=sj.staff_headers(principal), body={"version": 1})
                assert status in (403, 404), (principal, status, out)
                assert "outcome" not in out
            assert _item_state(install) == before

    asyncio.run(body())


def test_sj_j14_wrong_tenant_is_refused_without_change(sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await sj.check_item_without_scheduler(install, rt)
            app = sj.staff_app(install, rt)
            before = _item_state(install)
            status, out = await sj.http(  # an actor authorized for another tenant
                app, "POST", sj.item_path(install, flow["item_id"], "/claim"),
                headers=sj.staff_headers(authorized_tenant="sj-other-tenant"),
                body={"version": 1})
            assert status == 403 and "outcome" not in out, (status, out)
            status, out = await sj.http(  # a route for a tenant this harness does not serve
                app, "GET", sj.item_path(install, flow["item_id"], tenant="sj-other-tenant"),
                headers=sj.staff_headers(caps="read"))
            assert (status, out.get("refused")) == (404, "TENANT_UNKNOWN"), out
            assert _item_state(install) == before

    asyncio.run(body())


def test_sj_j15_missing_capability_is_refused_without_change(sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        async with si.runtime(install) as rt:
            flow = await sj.check_item_without_scheduler(install, rt)
            app = sj.staff_app(install, rt)
            before = _item_state(install)
            for caps, method, suffix in (("read", "POST", "/claim"),
                                         ("read", "POST", "/resolve"),
                                         ("none", "GET", "/reason")):
                status, out = await sj.http(app, method,
                                            sj.item_path(install, flow["item_id"], suffix),
                                            headers=sj.staff_headers(caps=caps),
                                            body={"version": 1, "outcome": "reached_no_change"})
                assert status == 403 and "outcome" not in out, (caps, suffix, status, out)
            assert _item_state(install) == before
            # and the same route with the right capability works (the refusal was the cap)
            status, out = await sj.http(app, "POST",
                                        sj.item_path(install, flow["item_id"], "/claim"),
                                        headers=sj.staff_headers(), body={"version": 1})
            assert (status, out.get("outcome")) == (200, "applied"), out

    asyncio.run(body())


# === S-J ROWS (RED BY DESIGN until haloflow.m05.writeback_scheduler exists) =================


def _wb_counts(result: Any) -> dict[str, int]:
    (tenant,) = result.tenants
    return dict(tenant.run.counts)


async def _sent_and_replied(stack: sj.Stack, text: str = "1", **identity: Any
                            ) -> dict[str, str]:
    flow = await sj.ingest(stack, **identity)
    before = len(stack.sender.calls)
    await stack.tick_reminder()
    assert [c[1] for c in stack.sender.calls[before:]] == [flow["phone"]]
    op = sh2.op_for(stack.install, flow["ref"])
    assert op is not None
    flow["op_id"] = str(op["op_id"])
    flow["message"] = sa.new_id()
    status, out = await sj.reply(stack, flow, text, message=flow["message"])
    assert (status, out.get("outcome")) == (202, "registered"), out
    return flow


async def _flow_ehr(install: si.SIInstall) -> tuple[Any, list[dict[str, str]]]:
    """An EHR that receives appointments as the rows ingest them (filled later)."""

    from haloflow.m05.simulated_ehr import SimulatedEhr

    return SimulatedEhr(clock=si.db_clock(install)), []


def _put(install: si.SIInstall, ehr: Any, flow: dict[str, str], **over: Any) -> None:
    from haloflow.m05.simulated_ehr import EhrRead

    fields: dict[str, Any] = {"status": "neutral", "appointment_id": flow["ref"],
                              "start_at": sh.start_of(install, flow["ref"]),
                              "provider_ref": si.PROVIDER, "department_ref": si.DEPARTMENT,
                              "replaced_by": None}
    fields.update(over)
    ehr.put(EhrRead(**fields))


# --- J01 happy path ---------------------------------------------------------------------------


def test_sj_j01_confirmed_and_matching_is_written_once_through_the_scheduler(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            flow = await _sent_and_replied(stack)
            _put(install, ehr, flow)
            result = await stack.tick_writeback()
            assert _wb_counts(result)["observed_positive"] == 1, result
            assert result.tenants[0].discover.counts == {"discovered": 1}
            assert [w[0] for w in ehr.writes] == [flow["ref"]]
            assert len(stack.sender.calls) == 1
            assert si.check_items(install) == []
            (wb,) = si.wb_ops(install)
            assert wb["state"] == "observed_positive"
            again = await stack.tick_writeback()  # nothing left; no second write
            assert sum(_wb_counts(again).values()) == 0 and len(ehr.writes) == 1
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


# --- J02 status changed -> staff resolve over the test route ---------------------------------


def test_sj_j02_status_changed_item_is_resolved_over_the_test_staff_route(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            flow = await _sent_and_replied(stack)
            _put(install, ehr, flow, provider_ref="prov-other")
            result = await stack.tick_writeback()
            assert _wb_counts(result)["item_raised"] == 1
            assert ehr.writes == []
            (item,) = si.check_items(install)
            app = sj.staff_app(install, stack.rt)
            path = sj.item_path(install, str(item["item_id"]))
            status, reason = await sj.http(app, "GET", path + "/reason",
                                           headers=sj.staff_headers(caps="read"))
            assert (status, reason.get("reason")) == (200, "status_changed"), reason
            status, out = await sj.http(app, "POST", path + "/claim",
                                        headers=sj.staff_headers(), body={"version": 1})
            assert (status, out.get("outcome")) == (200, "applied"), out
            status, out = await sj.http(app, "POST", path + "/resolve",
                                        headers=sj.staff_headers(),
                                        body={"version": 2, "outcome": "reached_no_change"})
            assert (status, out.get("outcome")) == (200, "applied"), out
            resolutions = si.rows(install, "patient_events", "event_type = %s",
                                  (si.T_RESOLUTION,))
            assert len(resolutions) == 1
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


# --- J03 / J04 no write-back for NO, reschedule, cancel, unclear, STOP -----------------------


# The accepted H3 mapping (sh_h3 harness E02/E03/E07, S-F classifier, Q-B3): NO and an
# unclear text -> unclear_reply item; 2 -> reschedule_request; 3 -> cancel_request; STOP ->
# opt-out, no item. Each item is bound to the patient and to the reply's event.
H3_ITEM: Final = {"NO": "unclear_reply", "2": "reschedule_request", "3": "cancel_request",
                  "maybe later": "unclear_reply", "STOP": None}


@pytest.mark.parametrize("text", sorted(H3_ITEM))
def test_sj_j03_j04_non_confirm_replies_create_no_write_back(sj_i: si.SIInstall,
                                                             text: str) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            flow = await _sent_and_replied(stack, text)
            _put(install, ehr, flow)
            for _ in range(2):
                result = await stack.tick_all()
                assert sum(_wb_counts(result["writeback"]).values()) == 0
            assert si.wb_ops(install) == [] and ehr.writes == []
            assert si.check_items(install) == []
            assert len(stack.sender.calls) == 1  # no further send (STOP: opted out)
            items = sa.items_for(install, flow["patient"])
            if H3_ITEM[text] is None:
                assert sh3.window_of(install, flow["ref"])["state"] == "opted_out"
                assert items == []
            else:
                reply = sh3.reply_row(install, flow["message"])
                (item,) = items  # exactly one H3 staff item, bound to this reply
                assert (item["item_kind"], item["source_event_id"], item["patient_id"]) == (
                    H3_ITEM[text], reply["event_id"], flow["patient"])
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


# --- J05 forged / wrong-tenant ingress --------------------------------------------------------


def test_sj_j05_forged_ingress_creates_no_op_or_item(sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            flow = await sj.ingest(stack)
            _put(install, ehr, flow)
            await stack.tick_reminder()
            status, _out = await sj.reply(stack, flow, "1", secret="e" * 32 + "-forged")
            assert status in (401, 403)
            result = await stack.tick_writeback()
            assert sum(_wb_counts(result).values()) == 0
            assert si.wb_ops(install) == [] and si.check_items(install) == []
            assert ehr.writes == []
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


# --- J06 duplicate YES ------------------------------------------------------------------------


def test_sj_j06_duplicate_yes_is_one_op_and_one_write(sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            flow = await _sent_and_replied(stack)
            await sj.reply(stack, flow, "YES")  # a second confirm, new provider message id
            _put(install, ehr, flow)
            for _ in range(2):
                await stack.tick_writeback()
            assert len(si.wb_ops(install)) == 1 and len(ehr.writes) == 1
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


# --- J07 a write that raises after it was applied ---------------------------------------------


class RaisingAfterApply:
    def __init__(self, ehr: Any) -> None:
        self.ehr = ehr

    async def read(self, appointment_id: str) -> Any:
        return await self.ehr.read(appointment_id)

    async def close_permit(self, permit_id: str) -> None:
        await self.ehr.close_permit(permit_id)

    async def write_confirmation(self, appointment_id: str, **kw: Any) -> Any:
        await self.ehr.write_confirmation(appointment_id, **kw)
        raise ConnectionError("acknowledgement lost")


def test_sj_j07_unknown_write_is_never_repeated(sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, RaisingAfterApply(ehr))
        try:
            flow = await _sent_and_replied(stack)
            _put(install, ehr, flow)
            await stack.tick_writeback()
            await stack.tick_writeback()
            assert len(ehr.writes) == 1  # applied once; never re-dispatched
            (wb,) = si.wb_ops(install)
            assert wb["state"] in ("observed_positive", "item"), wb
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


# --- J08 named durable restart boundaries -----------------------------------------------------


def test_sj_j08a1_restart_with_an_accepted_unregistered_send_registers_once(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        # the crash state (the sh3.persisted_accepted_op shape, on directory v3): examine ->
        # consume -> outcome accepted; the worker dies before register
        _p, _ph, ref, _zone = si.seed3(install)
        op_id, attempt = sh2.claimed(install, ref)
        assert sh2.consume(install, op_id, attempt)["outcome"] == "send"
        sh.outcome(install, op_id, attempt, "accepted", provider_ref="fake-sj-accepted")
        assert sh.op_row(install, op_id)["state"] == "accepted"
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)  # the restarted lifespan
        try:
            await stack.tick_all()
            await stack.tick_all()
            assert sh.op_row(install, op_id)["state"] == "registered"
            assert sh3.dispatch_events(install, op_id) == 1
            assert stack.sender.calls == []  # no new sender call
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


def test_sj_j08a2_restart_after_consume_with_unknown_outcome_never_resends(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        _p, _ph, ref, _zone = si.seed3(install)
        op_id, attempt = sh2.claimed(install, ref)
        assert sh2.consume(install, op_id, attempt)["outcome"] == "send"
        sh2.expire_lease(install, op_id)  # the crash may have fallen before the send
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            await stack.tick_all()
            await stack.tick_all()
            assert sh.op_row(install, op_id)["state"] == "uncertain"
            assert stack.sender.calls == []
            assert si.wb_ops(install) == [] and ehr.writes == []
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


def test_sj_j08b_restart_after_the_reply_before_discovery_discovers_once(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        first = await sj.build(install, ehr)
        try:
            flow = await _sent_and_replied(first)
        finally:
            assert await first.shutdown() == {}
        _put(install, ehr, flow)
        second = await sj.build(install, ehr)
        try:
            result = await second.tick_writeback()
            assert result.tenants[0].discover.counts == {"discovered": 1}
            await second.tick_writeback()
            assert len(si.wb_ops(install)) == 1 and len(ehr.writes) == 1
        finally:
            assert await second.shutdown() == {}

    asyncio.run(body())


async def _spent_without_write(stack: sj.Stack, flow: dict[str, str], ehr: Any) -> str:
    install, rt = stack.install, stack.rt
    svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
    await _b(svc.discover(ctx, 100))
    (row,) = await _b(svc.due(ctx, 100))
    wb = row["wb_op_id"]
    claimed = await _b(svc.claim(ctx, wb))
    fence = int(claimed["fence"])
    decided = await _b(svc.decide(ctx, wb, fence, await ehr.read(flow["ref"]), False))
    spent = await _b(svc.spend(ctx, wb, fence, decided["permit_id"]))
    assert spent["outcome"] == "spent", spent
    return str(wb)


def test_sj_j08c_restart_after_spend_with_the_write_never_issued_reconciles_only(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        first = await sj.build(install, ehr)
        try:
            flow = await _sent_and_replied(first)
            _put(install, ehr, flow)
            wb = await _spent_without_write(first, flow, ehr)  # the crash: no write issued
        finally:
            assert await first.shutdown() == {}
        si.age(install, wb, lease=si.LEASE_S + 1,
               deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
        second = await sj.build(install, ehr)
        try:
            await second.tick_writeback()
            await second.tick_writeback()
            assert ehr.writes == []  # 0 after the restart and 0 in total (Codex 201829Z)
            assert si.wb_op(install, wb)["reason"] == "could_not_verify"
            assert len(si.check_items(install)) == 1
        finally:
            assert await second.shutdown() == {}

    asyncio.run(body())


def test_sj_j08d_restart_after_an_issued_uncertain_write_is_at_most_one_write(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        first = await sj.build(install, ehr, wb_stop_timeout_s=2)
        try:
            flow = await _sent_and_replied(first)
            _put(install, ehr, flow)
            first.port.hang_after_write = True
            first.writeback.start()  # the loop issues the write, then "crashes" (hangs)
            await _b(first.port.entered.wait())
            await _b(_until_applied(first.port))
        finally:
            assert await first.shutdown() == {}  # cooperative cancel: clean stop
        (wb,) = si.wb_ops(install)
        assert wb["state"] == "spent"
        si.age(install, str(wb["wb_op_id"]), lease=si.LEASE_S + 1,
               deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
        second = await sj.build(install, ehr)
        try:
            await second.tick_writeback()
            await second.tick_writeback()
            assert len(ehr.writes) == 1  # at most one in total; reconcile, never re-write
            assert si.wb_op(install, str(wb["wb_op_id"]))["state"] in (
                "observed_positive", "item")
        finally:
            assert await second.shutdown() == {}

    asyncio.run(body())


async def _until_applied(port: Any) -> None:
    while not port.applied:
        await asyncio.sleep(0.01)


class LostSettleAck:
    """A WritebackService whose settle COMMITS and then loses the acknowledgement."""

    def __new__(cls, rt: Any) -> Any:
        from haloflow.m05.writeback import WritebackService

        base = si.wb_service(rt)

        class Lossy(WritebackService):
            async def settle(self, *a: Any, **k: Any) -> Any:
                await base.settle(*a, **k)
                raise ConnectionError("acknowledgement lost after commit")

        from haloflow.m02.observation import EventFoundation

        return Lossy(EventFoundation(rt.gateway, producer_service=sa.PRODUCER))


@pytest.mark.parametrize("fixture", ["observed_positive", "could_not_verify"])
def test_sj_j08e_restart_after_settle_commit_before_ack_changes_nothing(
        sj_i: si.SIInstall, fixture: str) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        first = await sj.build(install, ehr, wb_service=LostSettleAck)
        try:
            flow = await _sent_and_replied(first)
            if fixture == "observed_positive":
                _put(install, ehr, flow)
                result = await first.tick_writeback()  # settle commits, ack lost
                assert _wb_counts(result)["failed"] == 1
            else:
                _put(install, ehr, flow)
                wb = await _spent_without_write(first, flow, ehr)
                si.age(install, wb, lease=si.LEASE_S + 1,
                       deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
                result = await first.tick_writeback()
                assert _wb_counts(result)["failed"] == 1
        finally:
            assert await first.shutdown() == {}
        (wb_row,) = si.wb_ops(install)
        items, events = si.check_items(install), si.check_events(install)
        if fixture == "observed_positive":
            assert wb_row["state"] == "observed_positive" and items == [] and events == []
        else:
            assert wb_row["reason"] == "could_not_verify"
            assert len(items) == 1 and len(events) == 1
        second = await sj.build(install, ehr)
        try:
            await second.tick_writeback()
            await second.tick_writeback()
        finally:
            assert await second.shutdown() == {}
        assert si.wb_ops(install) == [wb_row]
        assert si.check_items(install) == items and si.check_events(install) == events
        assert len(ehr.writes) == (1 if fixture == "observed_positive" else 0)

    asyncio.run(body())


# --- J09 activation off between spend and write, then on -------------------------------------


def test_sj_j09_activation_off_blocks_new_work_but_the_spent_write_completes(
        sj_i: si.SIInstall) -> None:
    import sc_support as sc

    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            f1 = await _sent_and_replied(stack)
            f2 = await _sent_and_replied(stack)
            for f in (f1, f2):
                _put(install, ehr, f)
            stack.port.hold = asyncio.Event()
            tick = asyncio.ensure_future(stack.tick_writeback())
            await _b(stack.port.entered.wait())  # op 1 spent; its write is held
            f3 = await _sent_and_replied(stack)  # replied while on, not yet discovered
            _put(install, ehr, f3)
            sc.change(install, "revoke")
            stack.port.hold.set()
            first = await _b(tick)
            counts = _wb_counts(first)
            assert counts["observed_positive"] == 1  # the spent write + settle completed
            assert counts["not_claimed"] == 1  # op 2: no NEW check claim while off
            ops_off = {r["wb_op_id"]: r["state"] for r in si.wb_ops(install)}
            off = await stack.tick_writeback()
            assert off.tenants[0].discover.status == "ACTIVATION_REFUSED"
            assert {r["wb_op_id"]: r["state"] for r in si.wb_ops(install)} == ops_off
            assert len(ehr.writes) == 1
            sc.change(install, "grant")
            for _ in range(2):
                await stack.tick_writeback()
            states = sorted(r["state"] for r in si.wb_ops(install))
            assert states == ["observed_positive"] * 3
            assert sorted(w[0] for w in ehr.writes) == sorted(f["ref"] for f in (f1, f2, f3))
            assert si.check_items(install) == []
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())


# --- J10 lifecycle: three real loops, shutdown order, STOP_FAILED -----------------------------


def test_sj_j10a_three_loops_run_and_stop_before_shared_resources_close(
        sj_i: si.SIInstall) -> None:
    """A lifecycle oracle only (DISCLOSED, Codex 20261110T225403Z): the three loops run a
    real first tick on an EMPTY workload, then park; this row does not prove live-task
    end-to-end transfer between the loops. The chain itself is covered by the manually
    ticked rows (J01, J02)."""

    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr, parked=True)
        stack.start_all()
        for name in sj.PRODUCERS:  # each loop finished its first tick and parked
            await _b(stack.sleeps[name].parked.wait())
        assert await stack.shutdown() == {}
        events = stack.recorder.events
        assert [e[0] for e in events] == ["ehr", "pool"]
        for _what, states, live in events:
            assert states == {"writeback": "STOPPED", "reminder": "STOPPED", "due": "STOPPED"}
            assert live == 0  # no scheduler task left when a shared resource closes
        assert stack.recorder.misuse == []

    asyncio.run(body())


def test_sj_j10b_stop_failed_keeps_resources_then_a_later_shutdown_closes(
        sj_i: si.SIInstall) -> None:
    install = _begin(sj_i)

    async def body() -> None:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr, wb_stop_timeout_s=0.2, write_timeout_s=60)
        flow = await _sent_and_replied(stack)
        _put(install, ehr, flow)
        stack.port.hold = asyncio.Event()
        stack.port.ignore_cancel = True
        stack.writeback.start()
        try:
            await _b(stack.port.entered.wait())
            failed = await stack.shutdown()
            assert set(failed) == {"writeback"}
            assert stack.writeback.state.name == "STOP_FAILED"
            assert stack.recorder.events == []  # nothing closed under the unfinished task
            assert not stack.port.closed
            stack.port.hold.set()  # the held write completes against an OPEN port and pool
            assert await stack.shutdown() == {}
            assert [e[0] for e in stack.recorder.events] == ["ehr", "pool"]
            assert stack.recorder.misuse == []
            assert len(ehr.writes) == 1
        finally:  # bounded teardown: a failing row must never leave the held task behind
            stack.port.hold.set()
            with contextlib.suppress(Exception):
                await _b(stack.writeback.stop())
            with contextlib.suppress(Exception):
                await _b(stack.runtime.close())

    asyncio.run(body())


# --- J12 privacy over a full scheduled pass ---------------------------------------------------


def test_sj_j12_no_patient_text_in_any_log_line(sj_i: si.SIInstall,
                                                caplog: pytest.LogCaptureFixture) -> None:
    install = _begin(sj_i)
    # The install runs alembic, whose `fileConfig` disables every logger created before it
    # (disable_existing_loggers=True); in a full run the S-J module logger already exists.
    # Re-enable the two loggers this row reads, so a positive line is really observed.
    for name in ("haloflow.m05.writeback_scheduler", "haloflow.m05.writeback"):
        logging.getLogger(name).disabled = False
    caplog.set_level(logging.DEBUG)

    async def body() -> dict[str, str]:
        ehr, _ = await _flow_ehr(install)
        stack = await sj.build(install, ehr)
        try:
            flow = await _sent_and_replied(stack)
            _put(install, ehr, flow, provider_ref="prov-other")
            await stack.tick_all()
            return flow
        finally:
            assert await stack.shutdown() == {}

    flow = asyncio.run(body())
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith("haloflow.m05.writeback"))
    assert "writeback_scheduler tick=" in text
    for secret in (flow["phone"], flow["patient"], sh2.CLINIC):
        assert secret not in text
