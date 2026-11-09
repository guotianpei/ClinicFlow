"""L-1S package S-H, checkpoint H3 — end-to-end harness rows E01-E08, PostgreSQL 17
(RED BY DESIGN).

Governing: H3 package v1 + v1r1 + v1r2 (Work Session 2026-11-09); see `sh3_support`. Each
row STARTS from synthetic directory ingestion through the rebound gateway key
(`sh2.gateway_directory` -> m05_directory_v2; never a test-supplied register_dispatch),
drives ONE ReminderScheduler tick (FakeSender only), delivers the patient reply through
the S-G1a synthetic HTTP ingress route (secret -> clinic, classifier, intake v2), and
works staff items through `M05Service.act` (claim / resolve via `sd.Chain`). Resolution
is a staff assertion; nothing is written to an EHR; the S-A side-effect guard is active.
Each row runs on a FRESH tenant (exact count deltas, `sd.counts`).

DISCLOSED: S-G1b's `create_synthetic_app` composes the S-C registry (target 46), so the
unchanged S-G1a router is mounted on a bare FastAPI app bound to the H3 TEST runtime, and
requests are delivered through the ASGI interface in process (sa_guard blocks
httpx.AsyncClient.send). Staff actions use the service, not the HTTP staff routes.

- E01  reply 1 -> confirmed: window replied, decision recorded, no staff item.
- E02  reply 2 -> reschedule_request item -> claim -> resolve "rescheduled".
- E03  reply 3 -> cancel_request item -> resolve "cancelled_in_ehr" (staff assertion only;
       the directory appointment is unchanged).
- E04  no reply -> DueWindowRunner past the deadline -> no_reply item.
- E05  STOP -> opt-out (window opted_out, no item); a second appointment on that phone ->
       the next tick sends nothing (examine skips OPTED_OUT).
- E06  two appointments on one phone, both reminded in one tick; reply 1 -> unclear_reply
       item, no window changed, nothing confirmed.
- E07  reply NO -> unclear_reply item; the appointment and its profile are unchanged.
- E08  a reply for clinic B's patient presented with clinic A's secret -> quarantined in
       A, nothing in B; an unknown secret -> 401, nothing anywhere.
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

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]

SECRET_A: Final = "a" * 32 + "-sh3-ingress-A"
SECRET_B: Final = "b" * 32 + "-sh3-ingress-B"
T_REPLY: Final = sd.T_REPLY


@pytest.fixture
def h3(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
       m05_reset: Any) -> Iterator[sh3.SH3Install]:
    yield from sh3.module_install(request, m02_ids, m05_reset, f"e:{request.node.name}")


@pytest.fixture
def h3_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
         m05_reset: Any) -> Iterator[sh3.SH3Install]:
    yield from sh3.module_install(request, m02_ids, m05_reset, f"e-b:{request.node.name}")


def _begin(install: sh3.SH3Install) -> None:
    """Install (row body), the S-D staff member and B4 profile, no awaiting windows, the
    clinic granted (sd.begin, v1r1 C2)."""

    install.require()
    sd.begin(install)  # type: ignore[arg-type]


async def _tick(rt: Any, installs: list[sh3.SH3Install], sender: Any = None) -> tuple[Any, Any]:
    rig = sh3.rig(rt, installs, sender=sender)
    try:
        result = await rig.tick()
    finally:
        await rig.stop()
    return result, rig.sender


async def _reminded(rt: Any, install: sh3.SH3Install, **kw: Any) -> tuple[str, str, str]:
    """Directory ingestion through the gateway, then one scheduler tick that sends exactly
    this reminder and opens its awaiting window."""

    patient, phone, ref = await sh3.ingest(rt, install, **kw)
    result, sender = await _tick(rt, [install])
    assert sh3.report(result, install).ready.counts == sh3.ready_counts(1, 1, 1)
    assert [c[1] for c in sender.calls] == [phone]
    assert sh3.window_of(install, ref)["state"] == "awaiting"
    return patient, phone, ref


async def _reply_item(rt: Any, install: sh3.SH3Install, text: str, kind: str
                      ) -> tuple[sd.Chain, uuid.UUID, uuid.UUID]:
    patient, phone, ref = await _reminded(rt, install)
    app = sh3.ingress_app(rt, {install.tenant_id: SECRET_A})
    before = sd.counts(install)  # type: ignore[arg-type]
    message = sa.new_id()
    status, body = await sh3.post_reply(app, SECRET_A, phone, text, message)
    assert (status, body.get("outcome"), body.get("decision")) == (202, "registered", "item")
    assert sd.delta(before, sd.counts(install)) == sd.reply_item(kind)  # type: ignore[arg-type]
    assert sh3.window_of(install, ref)["state"] == "replied"
    reply = sh3.reply_row(install, message)
    (item,) = sa.items_for(install, patient)
    assert (item["item_kind"], item["source_event_id"]) == (kind, reply["event_id"])
    chain = sd.Chain(install, rt)  # type: ignore[arg-type]
    return chain, uuid.UUID(item["item_id"]), uuid.UUID(reply["event_id"])


async def _claim_resolve(chain: sd.Chain, item: uuid.UUID, outcome: str, kind: str,
                         source: uuid.UUID) -> None:
    claimed = await chain.claim(item)
    assert claimed.outcome == "applied"
    resolved = await chain.resolve(item, outcome)
    assert resolved.outcome == "applied"
    await sd.assert_resolved(chain, item, outcome, kind=kind, source_event=source)


# --- E01 ------------------------------------------------------------------------------------


def test_h3_e01_reply_1_confirms_without_item(h3: sh3.SH3Install) -> None:
    _begin(h3)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            patient, phone, ref = await _reminded(rt, h3)
            app = sh3.ingress_app(rt, {h3.tenant_id: SECRET_A})
            before = sd.counts(h3)  # type: ignore[arg-type]
            message = sa.new_id()
            status, reply = await sh3.post_reply(app, SECRET_A, phone, "1", message)
            assert (status, reply["outcome"], reply["decision"]) == (202, "registered",
                                                                     "recorded")
            assert sd.delta(before, sd.counts(h3)) == sd.CONFIRM  # type: ignore[arg-type]
            assert sh3.window_of(h3, ref)["state"] == "replied"
            row = sh3.reply_row(h3, message)
            assert (row["reply_class"], row["triage_kind"], row["appointment_ref"]) == (
                "confirm", None, ref)
            assert sa.items_for(h3, patient) == []

    asyncio.run(body())


# --- E02 / E03 ------------------------------------------------------------------------------


def test_h3_e02_reply_2_reschedule_item_resolved(h3: sh3.SH3Install) -> None:
    _begin(h3)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            chain, item, source = await _reply_item(rt, h3, "2", "reschedule_request")
            await _claim_resolve(chain, item, "rescheduled", "reschedule_request", source)

    asyncio.run(body())


def test_h3_e03_reply_3_cancel_item_staff_assertion_only(h3: sh3.SH3Install) -> None:
    _begin(h3)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            chain, item, source = await _reply_item(rt, h3, "3", "cancel_request")
            row = sd.item_row(h3, item)  # type: ignore[arg-type]
            appointment_before = sh3.appointment_rows(h3, row["appointment_ref"])
            await _claim_resolve(chain, item, "cancelled_in_ehr", "cancel_request", source)
            # A staff assertion only: the directory appointment is untouched (no EHR write).
            assert sh3.appointment_rows(h3, row["appointment_ref"]) == appointment_before
            (appt,), _profile = appointment_before
            assert appt[0]["status"] == "booked"

    asyncio.run(body())


# --- E04 ------------------------------------------------------------------------------------


def test_h3_e04_no_reply_item_after_deadline(h3: sh3.SH3Install) -> None:
    _begin(h3)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            patient, _phone, ref = await _reminded(rt, h3)
            window = sh3.window_of(h3, ref)
            sa.shift_window(h3, str(window["reminder_id"]), -25)  # FIXTURE: deadline passed
            chain = sd.Chain(h3, rt)  # type: ignore[arg-type]
            before = sd.counts(h3)  # type: ignore[arg-type]
            run = await asyncio.wait_for(chain.run_once(), sh3.BOUND_S)
            assert (run.code, run.attempted, run.no_reply) == ("COMPLETE", 1, 1), run
            assert sd.delta(before, sd.counts(h3)) == sd.NO_REPLY  # type: ignore[arg-type]
            closed = sh3.window_of(h3, ref)
            assert (closed["state"], closed["closed_decision"]) == ("no_reply", "no_reply")
            (item,) = sa.items_for(h3, patient)
            assert (item["item_kind"], item["source_event_id"]) == (
                "no_reply", closed["closed_event_id"])

    asyncio.run(body())


# --- E05 ------------------------------------------------------------------------------------


def test_h3_e05_stop_opts_out_and_blocks_later_reminders(h3: sh3.SH3Install) -> None:
    _begin(h3)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            patient, phone, ref = await _reminded(rt, h3)
            app = sh3.ingress_app(rt, {h3.tenant_id: SECRET_A})
            before = sd.counts(h3)  # type: ignore[arg-type]
            status, reply = await sh3.post_reply(app, SECRET_A, phone, "STOP")
            assert (status, reply["decision"]) == (202, "opt_out")
            assert sd.delta(before, sd.counts(h3)) == {  # type: ignore[arg-type]
                "replies": 1, "opted_out": 1, "windows:awaiting": -1, "windows:opted_out": 1}
            assert sh3.window_of(h3, ref)["state"] == "opted_out"
            contact = sa.contact(h3, phone)
            assert contact is not None and contact["opted_out"] is True
            assert sa.items_for(h3, patient) == []
            # A second appointment for the same patient and phone: ingested, never sent.
            _p, _ph, second = await sh3.ingest(rt, h3, patient=patient, phone=phone,
                                               contact=False)
            result, sender = await _tick(rt, [h3])
            assert sender.calls == []
            assert sh3.report(result, h3).ready.counts == sh3.ready_counts(1, 0, 0)
            assert sh2.op_for(h3, second) is None
            row = sh2.schedule_row(h3, second)
            assert row is not None and row["last_reason"] == "OPTED_OUT"

    asyncio.run(body())


# --- E06 / E07 ------------------------------------------------------------------------------


def test_h3_e06_two_appointments_one_phone_reply_is_unclear(h3: sh3.SH3Install) -> None:
    _begin(h3)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            patient, phone, ref_1 = await sh3.ingest(rt, h3)
            _p, _ph, ref_2 = await sh3.ingest(rt, h3, patient=patient, phone=phone,
                                              contact=False)
            result, sender = await _tick(rt, [h3])
            assert sh3.report(result, h3).ready.counts == sh3.ready_counts(2, 2, 2)
            assert [c[1] for c in sender.calls] == [phone, phone]
            app = sh3.ingress_app(rt, {h3.tenant_id: SECRET_A})
            before = sd.counts(h3)  # type: ignore[arg-type]
            message = sa.new_id()
            status, reply = await sh3.post_reply(app, SECRET_A, phone, "1", message)
            assert (status, reply["decision"]) == (202, "item")
            change = sd.delta(before, sd.counts(h3))  # type: ignore[arg-type]
            assert change.get("items:unclear_reply:open") == 1 and change["replies"] == 1
            assert not [k for k in change if k.startswith("windows")], change  # no guess
            assert [sh3.window_of(h3, r)["state"] for r in (ref_1, ref_2)] == ["awaiting"] * 2
            row = sh3.reply_row(h3, message)
            assert (row["reply_class"], row["triage_kind"], row["reminder_id"]) == (
                "confirm", "unclear_reply", None)
            assert sorted(row["matched_reminders"]) == sorted(
                sh3.window_of(h3, r)["reminder_id"] for r in (ref_1, ref_2))
            assert [i["item_kind"] for i in sa.items_for(h3, patient)] == ["unclear_reply"]

    asyncio.run(body())


def test_h3_e07_reply_no_is_unclear_and_changes_nothing(h3: sh3.SH3Install) -> None:
    _begin(h3)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            patient, phone, ref = await _reminded(rt, h3)
            appointment_before = sh3.appointment_rows(h3, ref)
            app = sh3.ingress_app(rt, {h3.tenant_id: SECRET_A})
            before = sd.counts(h3)  # type: ignore[arg-type]
            message = sa.new_id()
            status, reply = await sh3.post_reply(app, SECRET_A, phone, "NO", message)
            assert (status, reply["decision"]) == (202, "item")
            assert sd.delta(before, sd.counts(h3)) == sd.reply_item(  # type: ignore[arg-type]
                "unclear_reply")
            assert sh3.reply_row(h3, message)["reply_class"] == "unclear"
            assert [i["item_kind"] for i in sa.items_for(h3, patient)] == ["unclear_reply"]
            assert sh3.appointment_rows(h3, ref) == appointment_before

    asyncio.run(body())


# --- E08 ------------------------------------------------------------------------------------


def test_h3_e08_cross_clinic_reply_refused(h3: sh3.SH3Install, h3_b: sh3.SH3Install) -> None:
    _begin(h3)
    _begin(h3_b)

    async def body() -> None:
        async with sh3.runtime(h3) as rt:
            _patient, phone_b, ref_b = await _reminded(rt, h3_b)
            app = sh3.ingress_app(rt, {h3.tenant_id: SECRET_A, h3_b.tenant_id: SECRET_B})
            before_a, before_b = sd.counts(h3), sd.counts(h3_b)  # type: ignore[arg-type]
            # Clinic A's secret selects clinic A: B's phone is unknown there.
            status, reply = await sh3.post_reply(app, SECRET_A, phone_b, "1")
            assert (status, reply["decision"]) == (202, "quarantined")
            assert sd.delta(before_a, sd.counts(h3)) == {"replies": 1}  # type: ignore[arg-type]
            assert sd.delta(before_b, sd.counts(h3_b)) == {}  # type: ignore[arg-type]
            assert sh3.window_of(h3_b, ref_b)["state"] == "awaiting"
            # An unknown secret selects no clinic at all.
            mid_a, mid_b = sd.counts(h3), sd.counts(h3_b)  # type: ignore[arg-type]
            status, reply = await sh3.post_reply(app, "c" * 40, phone_b, "1")
            assert (status, reply) == (401, {"code": "INGRESS_UNAUTHENTICATED"})
            assert sd.delta(mid_a, sd.counts(h3)) == {}  # type: ignore[arg-type]
            assert sd.delta(mid_b, sd.counts(h3_b)) == {}  # type: ignore[arg-type]

    asyncio.run(body())
