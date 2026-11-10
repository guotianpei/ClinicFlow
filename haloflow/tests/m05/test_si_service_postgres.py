"""L-1S package S-I — PostgreSQL 17 rows through the services (RED BY DESIGN).

Governing and pins: see `si_support` (SQL pins and service pins). Each row installs a
fresh S-I tenant in its body, so the expected red cause is ModuleNotFoundError for
`haloflow.m05.writeback*` / `haloflow.m05.simulated_ehr`.

Rows (requirement in brackets, packet v1 numbering; Codex pins in parentheses):
- P-I01 [SI-R11] an item decision appends exactly one check event and creates exactly one
  item, atomically, under the real gateway (pin 3).
- P-I02 / P-I02b [R11] a refusal AT THE CREATE STAGE (after the SQL decision and the M02
  append, proved by the statement spy) rolls back the decision / settlement, the event and
  the item: no orphan; the later call mints one identity.
- P-I03 [R11] ordinary replay: a second full worker run after a committed item creates
  nothing. P-I03b [R11] a REAL lost acknowledgement (the transaction commits, then the
  acknowledgement is lost) -> WritebackCommitUnknown; the committed state, event and item
  are exactly one each; the retry changes nothing and keeps the identity (pin 3).
- P-I04 [R11/R14] two concurrent settlements of one op: the first locks the op and mints
  and stores the event id; the second waits, sees the terminal state and is stale,
  appending nothing; the winner's identity is the stored one (pin 3).
- P-I05 [R11] the real staff path: claim -> resolve through prepare -> M02 append (contract
  v2) -> apply -> history (pin 1).
- P-I06 [R11] the four existing kinds (reschedule, cancel, unclear, no_reply) are still
  created on the S-I tenant and resolve through v3 with contract v2 events. P-I06b [R11]
  v1 compatibility: an existing kind resolved by the ORIGINAL M05Service (contract v1
  event) through the rebound v3 apply is still applied.
- P-I07 [R11] the authorized reason read: one of the five reasons, exactly the pinned keys;
  an old-kind item, an unknown item and another tenant's item are ITEM_NOT_FOUND; no
  read capability -> refused (pin 2). P-I07b a check item whose source event is not its
  check event (same tenant, FIXTURE) -> ITEM_NOT_FOUND, nothing of that event returned.
- P-I08 [R11] nothing is sent to the patient: no sender call, no outbound op.
- P-A01..A03 [R12] activation off: discover / check claim / decide / spend refused; a
  permit spent before the switch went off completes its single call; settlement and
  reconcile continue (the settlement's item through the t094 recovery create).
- P-A04..A06 [R12/R13] t094 controls (Codex 20261110T155135Z): the ordinary create key
  stays protected (catalogue and a revoked transaction), decide stays protected; at the
  SQL boundary the recovery create refuses a wrong fence, a foreign permit or event in
  the same tenant, a non-check kind, a decision-path item and another tenant's op (each
  with a positive control in the row); a repeated or concurrent recovery call is
  refused: one item, linked once.
- P-T01..T03 [R13] another tenant's op is not visible; a context without the S-I
  capabilities or not a workload is refused before any I/O (statement spy: no statement;
  gateway: no transaction). P-T04 [R13] spend / settle / capture with the other tenant's
  ids (independent, otherwise-eligible fixtures: a capturable op, an unspent permit, a
  spent permit) from this tenant's context get the documented refusals (OP_NOT_FOUND /
  OP_NOT_FOUND / stale) and change nothing in either tenant; the same calls on this
  tenant's equivalent ids succeed (same-tenant controls).
- P-Q01 [R8/R9] an ACTIVE clinic: a restarted worker on a spent, unsettled op goes check
  claim (not_claimable PERMIT_SPENT) -> reconcile claim -> close -> read -> settle, in
  that measured order.
- P-P01 [R15] restart between every step resumes and never writes twice.
- P-V01 [R16] the check event metadata is exactly {wb_op_id, appointment_ref, reason}; no
  phone, name, provider or department in events, items or logs.
- P-W10 / P-W10b / P-W11 [R4] BaselineWorker on the real runtime (one-connection pool,
  composed and used BEFORE the fault): a capture that FAILS in the database (permission
  revoked after composition; the spy shows the capture statement raised) and a capture
  whose acknowledgement is lost after commit both leave a usable connection and the inner
  send unchanged; the H3 tick with the wrapper gives the identical ready counts and a
  baseline.
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


def _remove_mapping(install: si.SIInstall, reason: str) -> None:
    """FIXTURE: the intake map row for `reason` removed, so create (v2) refuses with
    SOURCE_NOT_ELIGIBLE after the SQL decision and the M02 append have run."""

    sa.admin(install, sa.q(install, "DELETE FROM {s}.m05_intake_map WHERE source_event_type ="
                                    " %s AND discriminator_value = %s"), (si.T_CHECK, reason))


def _restore_mapping(install: si.SIInstall, reason: str) -> None:
    sa.admin(install, sa.q(install, "INSERT INTO {s}.m05_intake_map (source_event_type,"
                                    " source_contract_version, discriminator_path,"
                                    " discriminator_value, item_kind, producer_workload_id,"
                                    " active_in_production) VALUES (%s, 1, 'reason', %s, %s,"
                                    " %s, false)"),
             (si.T_CHECK, reason, si.ITEM_KIND, sa.W_DISPATCH))


def _create_stage_refusal(spy: si.StatementSpy, mark: int, step: str) -> None:
    calls = spy.since(mark)
    create = si.CREATE_SETTLED_KEY if step == "settle" else si.CREATE_KEY
    assert [k for k, _ in calls][-3:] == [si.KEYS[step], si.APPEND_KEY, create], calls
    assert [o for _, o in calls][-3:-1] == ["ok", "ok"] and calls[-1][1].startswith("raised:")


def test_si_pi02_a_create_stage_refusal_rolls_the_decision_back(
        si_install: si.SIInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackFailed

        async with si.runtime(install) as rt:
            flow, wb, fence = si.checked(install)
            _remove_mapping(install, "declined_or_unknown")
            svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
            spy = si.spy_statements(monkeypatch)
            with pytest.raises(WritebackFailed) as failed:
                await _b(svc.decide(ctx, wb, fence, _read(install, flow, "negative"), False))
            assert failed.value.reason_code == "SOURCE_NOT_ELIGIBLE"
            _create_stage_refusal(spy, 0, "decide")
            op = si.wb_op(install, wb)
            assert (op["state"], op["event_id"], op["item_id"]) == ("eligible_check", None,
                                                                    None)
            assert si.check_events(install) == [] and si.check_items(install) == []
            _restore_mapping(install, "declined_or_unknown")
            again = await _b(svc.decide(ctx, wb, fence, _read(install, flow, "negative"),
                                        False))
            assert again["outcome"] == "item", again
            assert len(si.check_events(install)) == 1 and len(si.check_items(install)) == 1

    asyncio.run(body())


def test_si_pi02b_a_create_stage_refusal_rolls_the_settlement_back(
        si_install: si.SIInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackFailed

        async with si.runtime(install) as rt:
            _flow, wb, fence, permit = si.permitted(install)
            assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
            spent = si.permit(install, wb)
            _remove_mapping(install, "could_not_verify")
            svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
            spy = si.spy_statements(monkeypatch)
            with pytest.raises(WritebackFailed) as failed:
                await _b(svc.settle(ctx, wb, fence, permit, "unknown", None))
            assert failed.value.reason_code == "SOURCE_NOT_ELIGIBLE"
            _create_stage_refusal(spy, 0, "settle")
            op = si.wb_op(install, wb)
            assert (op["state"], op["event_id"], op["item_id"]) == ("spent", None, None)
            assert si.permit(install, wb) == spent
            assert si.check_events(install) == [] and si.check_items(install) == []
            _restore_mapping(install, "could_not_verify")
            again = await _b(svc.settle(ctx, wb, fence, permit, "unknown", None))
            assert again["outcome"] == "item", again
            assert len(si.check_events(install)) == 1 and len(si.check_items(install)) == 1

    asyncio.run(body())


def test_si_pi03_a_replay_after_a_committed_item_creates_nothing(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackWorker

        async with si.runtime(install) as rt:
            flow, wb, _result = await _item_decision(rt, install)
            before = _counts(install)
            # ordinary replay: the worker runs the whole op again
            ctx = await si.wb_context(rt, install)
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install), "negative")
            outcome = await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb))
            assert outcome == "not_claimed"
            assert _counts(install) == before
            assert si.claim(install, wb)["outcome"] == "not_claimable"

    asyncio.run(body())


def test_si_pi03b_a_real_lost_acknowledgement_keeps_one_identity(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackCommitUnknown, WritebackWorker

        async with si.runtime(install) as rt:
            flow, wb, fence = si.checked(install)
            lossy = si.AckGateway(rt.gateway, lose=(1,))
            ctx = await si.wb_context(rt, install)
            with pytest.raises(WritebackCommitUnknown):
                await _b(si.wb_service_on(lossy).decide(
                    ctx, wb, fence, _read(install, flow, "negative"), False))
            assert lossy.transactions == 1
            op = si.wb_op(install, wb)  # the transaction DID commit
            (event,) = si.check_events(install)
            (item,) = si.check_items(install)
            assert (op["state"], op["event_id"], op["item_id"]) == (
                "item", event["event_id"], item["item_id"])
            before = (_counts(install), op)
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install), "negative")
            assert await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb)) \
                == "not_claimed"
            assert (_counts(install), si.wb_op(install, wb)) == before

    asyncio.run(body())


def test_si_pi04_concurrent_settlements_make_one_event_and_one_item(
        si_install: si.SIInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first settlement locks the op and mints + stores the event id; the second waits
    on that lock, then sees the terminal state: stale, nothing appended."""

    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install, max_size=4) as rt:
            _flow, wb, fence, permit = si.permitted(install)
            assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
            svc = si.wb_service(rt)
            ctx_a, ctx_b = (await si.wb_context(rt, install), await si.wb_context(rt, install))
            spy = si.spy_statements(monkeypatch)
            results = await _b(asyncio.gather(
                svc.settle(ctx_a, wb, fence, permit, "unknown", None),
                svc.settle(ctx_b, wb, fence, permit, "unknown", None),
                return_exceptions=True))
            outcomes = sorted(r["outcome"] if isinstance(r, dict) else type(r).__name__
                              for r in results)
            assert outcomes == ["item", "stale"], results
            (winner,) = [r for r in results if isinstance(r, dict) and r["outcome"] == "item"]
            (event,) = si.check_events(install)
            assert winner["event_id"] == event["event_id"] == si.wb_op(install, wb)["event_id"]
            assert len(si.check_items(install)) == 1
            appends = [c for c in spy.calls if c[0] == si.APPEND_KEY]
            assert appends == [(si.APPEND_KEY, "ok")]  # the loser appended nothing

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


async def _existing_item(rt: Any, install: si.SIInstall, kind: str) -> tuple[uuid.UUID,
                                                                            uuid.UUID]:
    """An item of an EXISTING kind created on the S-I tenant through intake."""

    flow = si.sent(install)
    chain = sd.Chain(install, rt)  # type: ignore[arg-type]
    if kind == "no_reply":
        reminder = str(sh.op_row(install, flow["op_id"])["reminder_id"])
        sa.shift_window(install, reminder, -25)
        closed = await _b(chain.close(uuid.UUID(reminder)))
        assert closed.decision == "no_reply", closed
    else:  # through the intake SERVICE: the SQL registration alone creates no item
        reply = {"reschedule_request": "reschedule", "cancel_request": "cancel",
                 "unclear_reply": "unclear"}[kind]
        registered = await _b(chain.inbound(flow["phone"], reply))
        assert registered.decision == "item", registered
    (row,) = sa.items_for(install, flow["patient"])
    assert row["item_kind"] == kind
    return uuid.UUID(row["item_id"]), uuid.UUID(row["source_event_id"])


@pytest.mark.parametrize("kind", ["reschedule_request", "cancel_request", "unclear_reply",
                                  "no_reply"])
def test_si_pi06_existing_kinds_still_work_through_v3(si_install: si.SIInstall,
                                                       kind: str) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            item, source = await _existing_item(rt, install, kind)
            staff = si.StaffChain(install, rt)
            assert (await _b(staff.chain.claim(item))).outcome == "applied"
            assert (await _b(staff.chain.resolve(item, "rescheduled"))).outcome == "applied"
            await sd.assert_resolved(staff.chain, item, "rescheduled", kind=kind,
                                     source_event=source)
            (event,) = sd.resolution_events(install, item)  # type: ignore[arg-type]
            assert event["contract_version"] == 2

    asyncio.run(body())


def test_si_pi06b_a_contract_v1_resolution_is_still_applied_through_v3(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install) as rt:
            item, source = await _existing_item(rt, install, "reschedule_request")
            chain = sd.Chain(install, rt)  # type: ignore[arg-type]  # ORIGINAL M05Service
            assert (await _b(chain.claim(item))).outcome == "applied"
            assert (await _b(chain.resolve(item, "rescheduled"))).outcome == "applied"
            await sd.assert_resolved(chain, item, "rescheduled", kind="reschedule_request",
                                     source_event=source)
            (event,) = sd.resolution_events(install, item)  # type: ignore[arg-type]
            assert event["contract_version"] == 1

    asyncio.run(body())


def test_si_pi07_the_reason_read_is_authorized_narrow_and_tenant_local(
        si_install: si.SIInstall, si_other: si.SIInstall) -> None:
    install, other = _begin(si_install), _begin(si_other)

    async def body() -> None:
        from haloflow.m05.service import ReadFailed, RouteDenied

        async with si.runtime(install) as rt:
            mine, _wb, result = await _item_decision(rt, install)
            staff = si.StaffChain(install, rt)
            item = uuid.UUID(result["item_id"])
            read = await _b(staff.reason(item))
            assert read == {"schema_version": 1, "item_id": str(item),
                            "reason": "declined_or_unknown"}
            old = si.sent(install)
            old_reply = await _b(sd.Chain(install, rt).inbound(  # type: ignore[arg-type]
                old["phone"], "reschedule"))
            assert old_reply.decision == "item", old_reply
            (old_item,) = sa.items_for(install, old["patient"])
            other_flow, _owb, other_result = await _item_decision(rt, other)
            for missing in (uuid.UUID(old_item["item_id"]), uuid.uuid4(),
                            uuid.UUID(other_result["item_id"])):
                with pytest.raises(ReadFailed) as failed:
                    await _b(staff.reason(missing))
                assert failed.value.reason_code == "ITEM_NOT_FOUND", missing
            with pytest.raises(RouteDenied):  # the staff route refusal (no read capability)
                await _b(staff.reason(item, caps=frozenset({sv.PREPARE})))
            assert other_flow["patient"] != mine["patient"]

    asyncio.run(body())


def test_si_pi07b_a_check_item_with_a_foreign_source_event_is_not_read(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.service import ReadFailed

        async with si.runtime(install) as rt:
            _flow, _wb, result = await _item_decision(rt, install)
            other = si.sent(install)
            reply = si.replied(install, other, "reschedule")
            foreign = reply["event_id"]
            sa.admin(install, sa.q(install, "UPDATE {s}.m05_followup_items SET source_event_id"
                                            " = %s WHERE item_id = %s"),
                     (foreign, result["item_id"]))  # FIXTURE: a mismatched source binding
            staff = si.StaffChain(install, rt)
            with pytest.raises(ReadFailed) as failed:
                await _b(staff.reason(uuid.UUID(result["item_id"])))
            assert failed.value.reason_code == "ITEM_NOT_FOUND"
            assert "reschedule" not in repr(failed.value)

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


def _foundation(rt: Any) -> Any:
    from haloflow.m02.observation import EventFoundation

    return EventFoundation(rt.gateway, producer_service=sa.PRODUCER)


def _sql_command(ctx: Any, **fields: Any) -> Any:
    from psycopg.types.json import Jsonb

    from haloflow.m05.intake import _attribution

    return {"command": Jsonb({"schema_version": 1, **fields,
                              "attribution": _attribution(ctx)})}


async def _append_check(binding: Any, row: dict[str, Any]) -> None:
    """The service's check observation for a settle/decide `item` row (same values)."""

    from haloflow.m02.observation import Observation

    wb = uuid.UUID(row["wb_op_id"])
    await _foundation_of(binding).append_observation_in(binding, Observation(
        event_id=uuid.UUID(row["event_id"]), event_type=si.T_CHECK, contract_version=1,
        correlation_id=wb, producer_dedupe_key=f"wbcheck:{wb.hex}", links=(),
        metadata={"wb_op_id": str(wb), "appointment_ref": row["appointment_ref"],
                  "reason": row["reason"]},
        patient_id=uuid.UUID(row["patient_id"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        source_time_quality="trusted"))


_FOUNDATIONS: dict[int, Any] = {}


def _foundation_of(binding: Any) -> Any:
    return _FOUNDATIONS[id(binding.handle)]


async def _recovery_tx(rt: Any, ctx: Any, wb: str, fence: int, permit: str, /,
                       **create_over: Any) -> dict[str, Any]:
    """ONE gateway transaction at the SQL boundary (no WritebackService validation):
    settle (unknown, no read -> could_not_verify item), append the check event, then the
    t094 recovery create with `create_over` replacing command fields. A refusal raises
    psycopg.Error and rolls the whole transaction back."""

    foundation = _foundation(rt)

    async def body(binding: Any) -> dict[str, Any]:
        _FOUNDATIONS[id(binding.handle)] = foundation
        try:
            (settled,) = await binding.handle.fetch_all(si.KEYS["settle"], _sql_command(
                ctx, wb_op_id=wb, fence=fence, permit_id=permit, write_outcome="unknown",
                read=None))
            row = dict(settled[0])
            assert row["outcome"] == "item", row
            await _append_check(binding, row)
            fields = {"wb_op_id": wb, "fence": fence, "permit_id": permit,
                      "source_event_id": row["event_id"], "item_kind": si.ITEM_KIND,
                      **create_over}
            (created,) = await binding.handle.fetch_all(si.CREATE_SETTLED_KEY,
                                                        _sql_command(ctx, **fields))
            return {"settled": row, "created": dict(created[0])}
        finally:
            _FOUNDATIONS.pop(id(binding.handle), None)

    result: dict[str, Any] = await foundation.run_in_transaction(ctx, body)
    return result


def _not_eligible(error: BaseException) -> bool:
    diag = getattr(error, "diag", None)
    return diag is not None and diag.message_primary == "SETTLED_ITEM_NOT_ELIGIBLE"


def test_si_pa04_revoked_activation_still_blocks_new_work_and_ordinary_create(
        si_install: si.SIInstall) -> None:
    """Controls for the t094 correction: the ordinary create key stays protected (the
    catalogue AND a revoked transaction that calls it), decide stays protected, and the
    recovery key is the only unprotected create."""

    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m01.errors import ActivationRefused

        protected = si.si_catalog().activation_protected
        assert si.CREATE_KEY in protected and si.CREATE_SETTLED_KEY not in protected
        assert {si.KEYS[k] for k in ("discover", "claim", "decide", "spend")} <= protected
        async with si.runtime(install) as rt:
            flow, wb, fence = si.checked(install)
            source = si.sent(install)
            reply = await _b(sd.Chain(install, rt).inbound(  # type: ignore[arg-type]
                source["phone"], "confirm"))
            sc.change(install, "revoke")
            ctx = await si.wb_context(rt, install)
            before = (_counts(install), si.wb_op(install, wb))
            with pytest.raises(ActivationRefused):  # a decision's item (new work)
                await _b(si.wb_service(rt).decide(ctx, wb, fence,
                                                  _read(install, flow, "negative"), False))

            async def ordinary(handle: Any) -> Any:  # the ordinary create, alone
                return await handle.fetch_all(si.CREATE_KEY, _sql_command(
                    ctx, source_event_id=str(reply.event_id), item_kind="unclear_reply"))

            with pytest.raises(ActivationRefused):
                await _b(rt.gateway.with_tenant_transaction(ctx, ordinary))
            assert (_counts(install), si.wb_op(install, wb)) == before

    asyncio.run(body())


@pytest.mark.parametrize("case", ["fence", "foreign_permit", "foreign_event", "kind",
                                  "decide_item", "other_tenant"])
def test_si_pa05_the_recovery_create_refuses_anything_but_its_own_settlement(
        si_install: si.SIInstall, si_other: si.SIInstall, case: str) -> None:
    """Direct SQL-boundary negatives for t094, each against a POSITIVE control in the same
    row (the unmutated call succeeds afterwards), so a refusal is the binding rule."""

    install, other = _begin(si_install), _begin(si_other)

    async def body() -> None:
        async with si.runtime(install) as rt:
            ctx = await si.wb_context(rt, install)
            _flow, wb, fence, permit = si.permitted(install)
            assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
            over: dict[str, Any] = {}
            target = (wb, fence, permit)
            if case == "fence":
                over = {"fence": fence + 1}
            elif case == "foreign_permit":  # another op's spent permit, same tenant
                _f2, wb2, fence2, permit2 = si.permitted(install)
                assert si.spend(install, wb2, fence2, permit2)["outcome"] == "spent"
                over = {"permit_id": permit2}
            elif case == "foreign_event":  # another op's canonical check event, same tenant
                _f3, wb3, _r3 = await _item_decision(rt, install)
                over = {"source_event_id": si.wb_op(install, wb3)["event_id"]}
            elif case == "kind":
                over = {"item_kind": "unclear_reply"}
            elif case == "decide_item":  # a decision-path item op: no spent permit
                _f4, wb4, fence4 = si.checked(install)
                decided = si.decide(install, wb4, fence4,
                                    si.ehr_read(install, _f4, "negative"))
                assert decided["outcome"] == "item", decided
                over = {"wb_op_id": wb4, "fence": fence4,
                        "source_event_id": decided["event_id"]}
            else:  # another tenant's spent permit and op
                _f5, wb5, fence5, permit5 = si.permitted(other)
                assert si.spend(other, wb5, fence5, permit5)["outcome"] == "spent"
                over = {"wb_op_id": wb5, "fence": fence5, "permit_id": permit5}
            before = (_counts(install), _counts(other), si.wb_ops(install), si.wb_ops(other))
            with pytest.raises(Exception) as refused:  # noqa: PT011 - checked by code below
                await _b(_recovery_tx(rt, ctx, *target, **over))
            if case == "kind":
                assert getattr(refused.value, "diag", None) is not None
                assert refused.value.diag.message_primary == "COMMAND_MALFORMED"
            else:
                assert _not_eligible(refused.value), repr(refused.value)
            assert (_counts(install), _counts(other), si.wb_ops(install),
                    si.wb_ops(other)) == before  # the whole transaction rolled back
            done = await _b(_recovery_tx(rt, ctx, *target))  # positive control
            item = done["created"]["item_id"]
            assert si.wb_op(install, wb)["item_id"] == item
            assert [i["item_id"] for i in si.check_items(install)].count(item) == 1

    asyncio.run(body())


def test_si_pa06_repeated_and_concurrent_recovery_create_one_item(
        si_install: si.SIInstall) -> None:
    """A repeat after the commit is refused; a concurrent recovery call waits on the op
    lock, then is refused: exactly one item, linked once."""

    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install, max_size=4) as rt:
            ctx_a, ctx_b = await si.wb_context(rt, install), await si.wb_context(rt, install)
            _flow, wb, fence, permit = si.permitted(install)
            assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
            foundation = _foundation(rt)
            created, release = asyncio.Event(), asyncio.Event()
            event: dict[str, str] = {}

            async def first(binding: Any) -> Any:
                _FOUNDATIONS[id(binding.handle)] = foundation
                (settled,) = await binding.handle.fetch_all(si.KEYS["settle"], _sql_command(
                    ctx_a, wb_op_id=wb, fence=fence, permit_id=permit,
                    write_outcome="unknown", read=None))
                row = dict(settled[0])
                await _append_check(binding, row)
                event["id"] = row["event_id"]
                (made,) = await binding.handle.fetch_all(si.CREATE_SETTLED_KEY, _sql_command(
                    ctx_a, wb_op_id=wb, fence=fence, permit_id=permit,
                    source_event_id=row["event_id"], item_kind=si.ITEM_KIND))
                created.set()
                await release.wait()  # hold the op lock open
                return dict(made[0])

            async def second(handle: Any) -> Any:
                return await handle.fetch_all(si.CREATE_SETTLED_KEY, _sql_command(
                    ctx_b, wb_op_id=wb, fence=fence, permit_id=permit,
                    source_event_id=event["id"], item_kind=si.ITEM_KIND))

            task_a = asyncio.create_task(foundation.run_in_transaction(ctx_a, first))
            await _b(created.wait())
            task_b = asyncio.create_task(rt.gateway.with_tenant_transaction(ctx_b, second))
            await asyncio.sleep(0.5)
            assert not task_b.done()  # waits on the op lock
            release.set()
            made = await _b(task_a)
            with pytest.raises(Exception) as concurrent:  # noqa: PT011 - checked below
                await _b(task_b)
            assert _not_eligible(concurrent.value), repr(concurrent.value)

            async def repeat(handle: Any) -> Any:
                return await handle.fetch_all(si.CREATE_SETTLED_KEY, _sql_command(
                    ctx_b, wb_op_id=wb, fence=fence, permit_id=permit,
                    source_event_id=event["id"], item_kind=si.ITEM_KIND))

            with pytest.raises(Exception) as repeated:  # noqa: PT011 - checked below
                await _b(rt.gateway.with_tenant_transaction(ctx_b, repeat))
            assert _not_eligible(repeated.value), repr(repeated.value)
            (item,) = si.check_items(install)
            assert item["item_id"] == made["item_id"] == si.wb_op(install, wb)["item_id"]
            _FOUNDATIONS.clear()

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
def test_si_pt02_03_admission_is_refused_before_any_io(
        si_install: si.SIInstall, case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackRefused

        async with si.runtime(install) as rt:
            flow, wb = si.confirmed_op(install)
            caps = si.wb_caps()
            if case == "missing_capability":
                ctx = await si.wb_context(rt, install, caps=frozenset(caps - {si.WB_CAP}))
            elif case == "extra_capability":
                ctx = await si.wb_context(rt, install, caps=caps | {"m05.intake.inbound"})
            else:
                ctx = await si.wb_context(rt, install, principal=sd.STAFF, kind="actor")
            before = si.wb_op(install, wb)
            counting = si.AckGateway(rt.gateway)
            svc = si.wb_service_on(counting)
            spy = si.spy_statements(monkeypatch)
            for call in (svc.claim(ctx, wb), svc.discover(ctx, 10), svc.due(ctx, 10),
                         svc.decide(ctx, wb, 1, _read(install, flow), False),
                         svc.spend(ctx, wb, 1, si.new_uuid()),
                         svc.settle(ctx, wb, 1, si.new_uuid(), "unknown", None)):
                with pytest.raises(WritebackRefused) as refused:
                    await _b(call)
                assert refused.value.code in ("CAPABILITIES_INVALID", "PRINCIPAL_NOT_WORKLOAD")
            assert spy.calls == [] and counting.transactions == 0  # no I/O at all
            assert si.wb_op(install, wb) == before

    asyncio.run(body())


def _capturable(install: si.SIInstall) -> tuple[str, str]:
    """An otherwise-eligible capture fixture: a reminder examined (claimed), its ticket's
    attempt current, not consumed, not captured: (op_id, attempt_id)."""

    _patient, _phone, ref, _zone = si.seed3(install)
    return sh2.claimed(install, ref)


def _tenant_state(install: si.SIInstall, wbs: list[str], ops: list[str]) -> Any:
    return (si.wb_ops(install), [si.permits(install, wb) for wb in wbs],
            [si.baseline(install, op) for op in ops], si.check_events(install),
            si.check_items(install))


def test_si_pt04_another_tenants_ids_change_nothing_anywhere(
        si_install: si.SIInstall, si_other: si.SIInstall) -> None:
    """Three INDEPENDENT, otherwise-eligible fixtures in EACH tenant (a capturable op, an
    unspent permit, a spent unsettled permit). Through this tenant's service and context the
    other tenant's ids get exactly the documented refusals (capture "refused" with code
    OP_NOT_FOUND, spend "refused" with code OP_NOT_FOUND, settle "stale"), and neither
    tenant changes; the SAME calls with this tenant's own equivalent ids then succeed
    (captured, spent, observed_positive), so the refusal is the tenant boundary and not
    an ineligible fixture."""

    install, other = _begin(si_install), _begin(si_other)

    async def body() -> None:
        fixtures: dict[str, dict[str, Any]] = {}
        for name, tenant in (("a", install), ("b", other)):
            _f1, spend_wb, spend_fence, spend_permit = si.permitted(tenant)
            settle_flow, settle_wb, settle_fence, settle_permit = si.permitted(tenant)
            assert si.spend(tenant, settle_wb, settle_fence, settle_permit)["outcome"] \
                == "spent"
            cap_op, cap_attempt = _capturable(tenant)  # last: holds its ticket claimed
            fixtures[name] = {
                "spend": (spend_wb, spend_fence, spend_permit),
                "settle": (settle_wb, settle_fence, settle_permit, settle_flow),
                "capture": (cap_op, cap_attempt),
                "wbs": [spend_wb, settle_wb], "ops": [cap_op]}
        a, b = fixtures["a"], fixtures["b"]
        mine = _tenant_state(install, a["wbs"], a["ops"])
        theirs = _tenant_state(other, b["wbs"], b["ops"])
        assert si.baseline(other, b["capture"][0]) is None  # eligible, not yet captured

        async with si.runtime(install) as rt:
            svc, ctx = si.wb_service(rt), await si.wb_context(rt, install)
            dispatch = await sh3.dispatch_context(rt, install)

            # the other tenant's ids: the documented refusals, nothing changes anywhere
            wb, fence, permit = b["spend"]
            spent = await _b(svc.spend(ctx, wb, fence, permit))
            assert (spent["outcome"], spent["code"]) == ("refused", "OP_NOT_FOUND"), spent
            wb, fence, permit, flow = b["settle"]
            settled = await _b(svc.settle(ctx, wb, fence, permit, "applied",
                                          _read(other, flow, "positive")))
            assert settled["outcome"] == "stale", settled
            assert settled.get("event_id") is None and settled.get("reason") is None
            op, attempt = b["capture"]
            assert await _b(svc.capture_baseline(dispatch, op, attempt)) == "refused"
            sql = si.call(install, "capture", op_id=op, attempt_id=attempt)
            assert (sql["outcome"], sql["code"]) == ("refused", "OP_NOT_FOUND"), sql
            assert _tenant_state(install, a["wbs"], a["ops"]) == mine
            assert _tenant_state(other, b["wbs"], b["ops"]) == theirs

            # same-tenant controls: the same calls on this tenant's equivalent ids succeed
            wb, fence, permit = a["spend"]
            assert (await _b(svc.spend(ctx, wb, fence, permit)))["outcome"] == "spent"
            wb, fence, permit, flow = a["settle"]
            own = await _b(svc.settle(ctx, wb, fence, permit, "applied",
                                      _read(install, flow, "positive")))
            assert own["outcome"] == "observed_positive", own
            op, attempt = a["capture"]
            assert await _b(svc.capture_baseline(dispatch, op, attempt)) == "captured"
            assert si.baseline(install, op) is not None
            assert _tenant_state(other, b["wbs"], b["ops"]) == theirs  # still unchanged

    asyncio.run(body())


def test_si_pq01_active_clinic_restart_reconciles_in_the_measured_order(
        si_install: si.SIInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    install = _begin(si_install)

    async def body() -> None:
        from haloflow.m05.writeback import WritebackWorker

        flow, wb, fence, permit = si.permitted(install)
        assert si.spend(install, wb, fence, permit)["outcome"] == "spent"
        si.age(install, wb, lease=si.LEASE_S + 1,
               deadline=si.WRITE_TIMEOUT_S + si.RECONCILE_MARGIN_S + 1)
        async with si.runtime(install) as rt:  # the clinic stays ACTIVE (granted)
            ehr = si.ehr_from_flow(install, flow, si.db_clock(install))
            order: list[str] = []
            original_close, original_read = ehr.close_permit, ehr.read

            async def close(permit_id: str) -> None:
                order.append("close")
                await original_close(permit_id)

            async def read(appointment_id: str) -> Any:
                order.append("read")
                return await original_read(appointment_id)

            ehr.close_permit, ehr.read = close, read
            spy = si.spy_statements(monkeypatch)
            ctx = await si.wb_context(rt, install)
            assert await _b(WritebackWorker(si.wb_service(rt), ehr).run_one(ctx, wb)) \
                == "item_raised"
            keys = [k for k in spy.ordered_keys() if k.startswith("m05.writeback.")]
            assert keys == [si.KEYS["claim"], si.KEYS["claim_reconcile"], si.KEYS["settle"]]
            assert order == ["close", "read"] and ehr.writes == []

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


async def _wrapped_send(rt: Any, install: si.SIInstall, capture_service: Any
                        ) -> tuple[str, str]:
    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.writeback import BaselineWorker

    svc = sh3.service(rt)
    inner = sh3.worker(svc, FakeSender())
    wrapper = BaselineWorker(inner, capture_service.capture_baseline)
    _p, _ph, ref, _z = si.seed3(install)
    op_id, attempt = sh2.claimed(install, ref)
    ctx = await sh3.dispatch_context(rt, install)
    outcome: str = await _b(wrapper.run_claimed(ctx, op_id, attempt))
    return outcome, op_id


def test_si_pw10_a_capture_failing_in_the_database_leaves_a_usable_connection(
        si_install: si.SIInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install, max_size=1) as rt:  # ONE connection, reused
            await sh3.dispatch_context(rt, install)  # composed and used before the fault
            sa.admin(install, sa.q(install, "REVOKE EXECUTE ON FUNCTION"
                                            " {s}.m05_writeback_capture_baseline(jsonb)"
                                            " FROM haloflow_runtime"))  # FIXTURE
            spy = si.spy_statements(monkeypatch)
            outcome, op_id = await _wrapped_send(rt, install, si.wb_service(rt))
            assert outcome == "accepted", outcome
            capture = [c for c in spy.calls if c[0] == si.KEYS["capture"]]
            assert len(capture) == 1 and capture[0][1].startswith("raised:"), capture
            later = spy.ordered_keys()[spy.ordered_keys().index(si.KEYS["capture"]) + 1:]
            assert "m05.outbound.consume" in later  # the same pool served the inner send
            assert si.baseline(install, op_id) is None
            assert sh.op_row(install, op_id)["state"] in ("accepted", "registered")

    asyncio.run(body())


def test_si_pw10b_a_capture_whose_acknowledgement_is_lost_still_sends(
        si_install: si.SIInstall) -> None:
    install = _begin(si_install)

    async def body() -> None:
        async with si.runtime(install, max_size=1) as rt:
            lossy = si.AckGateway(rt.gateway, lose=(1,))  # the capture commits, ack lost
            outcome, op_id = await _wrapped_send(rt, install, si.wb_service_on(lossy))
            assert outcome == "accepted", outcome
            assert lossy.transactions == 1
            assert si.baseline(install, op_id) is not None  # it did commit
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

