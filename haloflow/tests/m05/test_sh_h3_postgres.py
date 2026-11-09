"""L-1S package S-H, checkpoint H3 — ReminderScheduler and the t075 accepted selector,
PostgreSQL 17 rows (RED BY DESIGN).

Governing: H3 package v1 + v1r1 + v1r2 deltas and interface pins v1 (Work Session
2026-11-09; Codex ALIGN for red-test preparation); see `sh3_support` for the pinned API and
the disclosed test-side choices. Synthetic only; FakeSender (or a BlockingSender double)
only; TEST registry only (target 75); nothing composed; the S-A side-effect guard is active
for every row. Every H3 import is deferred into the row body or a support function, so the
expected red cause is ModuleNotFoundError for `haloflow.m05.reminder_scheduler*` (reported
by `SH3Install.require()` or by the deferred import), never a collection error.

Effective activation set (verified against the H1/H2 catalogues): expire, held, outcome,
ready, snapshot and t075 are NOT protected; register (t052, also when reached from
run_claimed or recover) and examine / consume / claim_v2 ARE protected.

Rows (P = PostgreSQL; P01-P10 each run on a FRESH tenant, because they count every send and
every registration in the tenant; P11 runs on two module tenants):
- P01  one ready reminder -> one tick -> exactly one sender call, op registered, one
       awaiting reply window; the tick report is COMPLETE with exact phase counts.
- P02  two schedulers (two runtimes) tick one tenant concurrently (asyncio.gather) for up
       to three rounds: every op sent exactly once; each persisted accepted op registered
       exactly once (one window), never twice.
- P03  held-lock seam (H2 P35 pattern, admin FOR UPDATE): a locked schedule row is skipped
       (not_ready LOCKED, not counted as examined), a locked expired attempt is skipped by
       expire (SKIP LOCKED); the tick completes, nothing is sent twice; after release the
       next tick sends the skipped ref once and expires the attempt.
- P04  (a) a persisted `accepted` op (crash before register) -> registered on the next tick
       with ZERO sender calls; (b) accepted_unregistered / uncertain / refused rows are
       never selected (no NOT_ACCEPTED, no failed item) and stay byte-identical;
       (c) a registered op is untouched; (d) an expired attempt -> uncertain LEASE_EXPIRED,
       zero sends.
- P04b `held` already holds >= recovery_limit terminal rows (newer than the accepted one):
       the accepted row is still registered in the same tick (the selector is not held).
- P05  backlog N = 2L+1 accepted rows whose first L fail persistently (DISCLOSED worker
       double): every healthy row registered within ceil(N/L)+1 ticks; the ready phase runs
       and examines new work every tick.
- P06  stop while the sender is blocked -> STOPPED; op uncertain SENDER_CANCELLED; a new
       scheduler's tick sends nothing for it.
- P07  clinic off with exactly L accepted rows -> recovery DEGRADED_ITEMS refused_activation
       = L, rows stay accepted, zero sends; clinic on -> the next tick wraps (after the
       cursor the page is empty) and registers all L, zero sends.
- P07b clinic off with L+1 rows -> tick 1 refuses L; clinic on -> tick 2 registers the
       (L+1)th after the cursor (short page resets), tick 3 registers the first L.
- P08  clinic switched off between a committed examine and consume (test barrier in a
       service subclass) -> consume refused M05_NOT_ACTIVATED, no send, ticket unspent.
- P09  two tenants, one whose context fails capability admission -> that tenant DEGRADED
       with context CAPABILITY_REFUSED and no phase run; the other sends.
- P10  caplog (DEBUG, every logger) over a tick with an adversarial leaky sender and a
       recovery item: no phone, first name or message text in any record, exception text
       or repr/str of the result.
- P11  t075 shape: install / binding / ACL / profile equal to the t056 pattern (a);
       COMMAND_MALFORMED cases with zero writes (b); attribution and capability refusals,
       not activation-protected (c); accepted only, (updated_at, op_id) ascending, strict
       cursor, paging, tie order by op_id, no writes (values and xmin unchanged) (d);
       tenant isolation with identical op ids and the gateway key (e).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import re
from collections.abc import Iterator
from typing import Any

import m02_support
import psycopg
import pytest
import sa_support as sa
import sc_support as sc
import sh2_support as sh2
import sh3_support as sh3
import sh_support as sh

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


@pytest.fixture
def h3(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
       m05_reset: Any) -> Iterator[sh3.SH3Install]:
    """A fresh tenant per row (installed in the row body: class-B red rule)."""

    yield from sh3.module_install(request, m02_ids, m05_reset, f"row:{request.node.name}")


@pytest.fixture
def h3_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
         m05_reset: Any) -> Iterator[sh3.SH3Install]:
    yield from sh3.module_install(request, m02_ids, m05_reset, f"row-b:{request.node.name}")


@pytest.fixture(scope="module")
def shape(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
          m05_reset: Any) -> Iterator[sh3.SH3Install]:
    yield from sh3.module_install(request, m02_ids, m05_reset, "shape")


@pytest.fixture(scope="module")
def shape_other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
                m05_reset: Any) -> Iterator[sh3.SH3Install]:
    yield from sh3.module_install(request, m02_ids, m05_reset, "shape:other")


async def _ticks(install: sh3.SH3Install, n: int = 1, **kw: Any) -> tuple[sh3.Rig, list[Any]]:
    """One scheduler, `n` manual ticks, then a bounded stop."""

    async with sh3.runtime(install) as rt:
        rig = sh3.rig(rt, [install], **kw)
        results: list[Any] = []
        try:
            for _ in range(n):
                results.append(await rig.tick())
        finally:
            await rig.stop()
    return rig, results


def _op_id(install: sa.SAInstall, ref: str) -> str:
    op = sh2.op_for(install, ref)
    assert op is not None, ref
    return str(op["op_id"])


# --- P01 ------------------------------------------------------------------------------------


def test_h3_p01_one_ready_reminder_one_send(h3: sh3.SH3Install) -> None:
    sh3.ready_install(h3)
    _p, phone, ref, _zone = sh3.sendable(h3)
    rig, (result,) = asyncio.run(_ticks(h3))
    rep = sh3.report(result, h3)
    assert (rep.code, rep.context) == ("COMPLETE", "ok")
    assert (rep.expire.status, rep.expire.counts) == ("ok", {"expired": 0})
    assert (rep.recovery.status, rep.recovery.counts) == ("ok", sh3.zero_recovery())
    assert (rep.ready.status, rep.ready.counts) == ("ok", sh3.ready_counts(1, 1, 1))
    assert (result.codes, result.cancelled) == ({"COMPLETE": 1}, False)
    op_id = _op_id(h3, ref)
    assert sh3.sent_ops(rig.sender) == [op_id]
    assert rig.sender.calls[0][1] == phone
    assert sh3.op_state(h3, op_id)[0] == "registered"
    assert [c[1] for c in rig.service.calls("register")] == [op_id]  # one t052 call
    window = sh3.window_of(h3, ref)
    assert window["state"] == "awaiting"
    assert window["reminder_id"] == sh.op_row(h3, op_id)["reminder_id"]


# --- P02 ------------------------------------------------------------------------------------


def test_h3_p02_two_schedulers_same_tenant(h3: sh3.SH3Install) -> None:
    sh3.ready_install(h3)
    refs = [sh3.sendable(h3)[2] for _ in range(4)]
    accepted = [sh3.persisted_accepted_op(h3) for _ in range(2)]

    async def body() -> tuple[list[sh3.Rig], list[Any]]:
        async with sh3.runtime(h3) as rt_a, sh3.runtime(h3) as rt_b:
            rigs = [sh3.rig(rt_a, [h3]), sh3.rig(rt_b, [h3])]
            rounds: list[Any] = []
            try:
                for _ in range(3):
                    rounds.extend(await asyncio.gather(*(r.tick() for r in rigs)))
            finally:
                for r in rigs:
                    await r.stop()
        return rigs, rounds

    rigs, results = asyncio.run(body())
    calls = [op for r in rigs for op in sh3.sent_ops(r.sender)]
    assert sorted(calls) == sorted(_op_id(h3, ref) for ref in refs)  # once each, never twice
    # A concurrent recovery may also register an op the OTHER scheduler's ready phase has
    # just recorded accepted (t052 then returns `existing` to the sender's own register),
    # so the exact-once oracle is one window per op, not a fixed `registered` total.
    assert sum(sh3.report(res, h3).recovery.counts["registered"] for res in results) >= 2
    assert all(res.codes == {"COMPLETE": 1} for res in results), [r.codes for r in results]
    for op_id, ref in accepted:
        assert sh3.op_state(h3, op_id)[0] == "registered"
        assert len(sh.windows_for(h3, ref)) == 1
    for ref in refs:
        assert sh3.op_state(h3, _op_id(h3, ref))[0] == "registered"
        assert len(sh.windows_for(h3, ref)) == 1
    assert not set(calls) & {op for op, _ref in accepted}  # recovery never sends


# --- P03 ------------------------------------------------------------------------------------


def test_h3_p03_locked_rows_skipped_never_double_sent(h3: sh3.SH3Install) -> None:
    from haloflow.m05.outbound import FakeSender

    sh3.ready_install(h3)
    _p, _ph, locked_ref, _z = sh3.sendable(h3)
    _p, _ph, free_ref, _z = sh3.sendable(h3)
    expired, _ = sh3.op_in_state(h3, "expired")
    sender = FakeSender()
    with psycopg.connect(h3.ids.admin) as holder, holder.transaction():
        holder.execute(sa.q(h3, "SELECT 1 FROM {s}.m05_outbound_schedule"
                                " WHERE appointment_ref = %s FOR UPDATE"), (locked_ref,))
        holder.execute(sa.q(h3, "SELECT 1 FROM {s}.m05_outbound_ops WHERE op_id = %s"
                                " FOR UPDATE"), (expired,))  # FIXTURE held locks
        _rig, (first,) = asyncio.run(_ticks(h3, sender=sender))
        rep = sh3.report(first, h3)
        assert rep.ready.counts == sh3.ready_counts(1, 1, 1)  # LOCKED is not examined
        assert rep.expire.counts == {"expired": 0}  # SKIP LOCKED: never waits
        assert sh3.sent_ops(sender) == [_op_id(h3, free_ref)]
        assert sh2.op_for(h3, locked_ref) is None
        assert sh3.op_state(h3, expired)[0] == "attempting"
    _rig, (second,) = asyncio.run(_ticks(h3, sender=sender))
    rep = sh3.report(second, h3)
    assert (rep.code, rep.ready.counts, rep.expire.counts) == (
        "COMPLETE", sh3.ready_counts(1, 1, 1), {"expired": 1})
    assert sorted(sh3.sent_ops(sender)) == sorted([_op_id(h3, free_ref),
                                                   _op_id(h3, locked_ref)])
    assert sh3.op_state(h3, expired) == ("uncertain", "LEASE_EXPIRED")


# --- P04 / P04b -----------------------------------------------------------------------------


def test_h3_p04_recovery_states(h3: sh3.SH3Install) -> None:
    sh3.ready_install(h3)
    accepted, accepted_ref = sh3.persisted_accepted_op(h3)
    untouched = {state: sh3.op_in_state(h3, state)[0]
                 for state in ("accepted_unregistered", "uncertain", "refused", "registered")}
    expired, _ = sh3.op_in_state(h3, "expired")
    sh2.clear_schedule(h3)  # FIXTURE: the ready phase has nothing; recovery is the subject
    before = {op: sh.op_row(h3, op) for op in untouched.values()}
    rig, (first, second) = asyncio.run(_ticks(h3, 2))
    rep = sh3.report(first, h3)
    assert rep.code == "COMPLETE"
    assert rep.expire.counts == {"expired": 1}
    assert (rep.recovery.status, rep.recovery.counts) == ("ok", sh3.zero_recovery(registered=1))
    assert rep.ready.counts == sh3.ready_counts()
    assert rig.sender.calls == []  # (a) recovery registers only
    assert sh3.op_state(h3, accepted)[0] == "registered"
    assert len(sh.windows_for(h3, accepted_ref)) == 1
    for op in untouched.values():  # (b) (c) never selected: byte-identical
        assert sh.op_row(h3, op) == before[op], op
    assert sh3.op_state(h3, expired) == ("uncertain", "LEASE_EXPIRED")  # (d)
    assert [c[1] for c in rig.service.calls("register")] == [accepted]
    rep2 = sh3.report(second, h3)
    assert (rep2.code, rep2.recovery.counts, rep2.expire.counts) == (
        "COMPLETE", sh3.zero_recovery(), {"expired": 0})


def test_h3_p04b_held_backlog_does_not_hide_accepted(h3: sh3.SH3Install) -> None:
    limit = 3
    sh3.ready_install(h3)
    accepted, _ref = sh3.persisted_accepted_op(h3)  # the OLDEST op
    terminal = [sh3.op_in_state(h3, state)[0]
                for state in ("uncertain", "refused", "accepted_unregistered", "uncertain")]
    assert len(terminal) >= limit
    held = sh.held_ops(h3, limit)["ops"]  # what a held-based selector would see
    assert accepted not in {o["op_id"] for o in held} and len(held) == limit
    sh2.clear_schedule(h3)
    rig, (result,) = asyncio.run(_ticks(h3, recovery_limit=limit))
    rep = sh3.report(result, h3)
    assert rep.recovery.counts == sh3.zero_recovery(registered=1)
    assert sh3.op_state(h3, accepted)[0] == "registered"
    assert rig.sender.calls == []


# --- P05 ------------------------------------------------------------------------------------


def test_h3_p05_failed_prefix_backlog_drains(h3: sh3.SH3Install) -> None:
    limit = 3
    total = 2 * limit + 1
    sh3.ready_install(h3)
    ops = [sh3.persisted_accepted_op(h3)[0] for _ in range(total)]
    page = sh3.pending(h3)
    assert [o["op_id"] for o in page] == ops  # creation order is (updated_at, op_id) order
    failing, healthy = ops[:limit], ops[limit:]
    sh2.clear_schedule(h3)
    bound = math.ceil(total / limit) + 1

    async def body() -> tuple[sh3.Rig, list[Any], list[str]]:
        async with sh3.runtime(h3) as rt:
            rig = sh3.rig(rt, [h3], recovery_limit=limit, fail_ops=failing)
            results, fresh = [], []
            try:
                for _ in range(bound):
                    fresh.append(sh3.sendable(h3)[2])  # new ready work every tick
                    results.append(await rig.tick())
            finally:
                await rig.stop()
        return rig, results, fresh

    rig, results, fresh = asyncio.run(body())
    for op in healthy:
        assert sh3.op_state(h3, op)[0] == "registered", op
    for op in failing:
        assert sh3.op_state(h3, op)[0] == "accepted", op
    reps = [sh3.report(r, h3) for r in results]
    assert sum(r.recovery.counts["registered"] for r in reps) == len(healthy)
    assert all(r.ready.status == "ok" and r.ready.counts == sh3.ready_counts(1, 1, 1)
               for r in reps), [(r.ready.status, r.ready.counts) for r in reps]
    assert reps[0].recovery.counts == sh3.zero_recovery(failed=limit)
    assert sorted(sh3.sent_ops(rig.sender)) == sorted(_op_id(h3, ref) for ref in fresh)
    assert not set(sh3.sent_ops(rig.sender)) & set(ops)


# --- P06 ------------------------------------------------------------------------------------


def test_h3_p06_stop_while_sender_blocked(h3: sh3.SH3Install) -> None:
    from haloflow.m05.due_scheduler import SchedulerState
    from haloflow.m05.outbound import FakeSender

    sh3.ready_install(h3)
    _p, _ph, ref, _z = sh3.sendable(h3)

    async def body() -> tuple[Any, Any, Any, Any]:
        sender = sh3.BlockingSender()
        async with sh3.runtime(h3) as rt:
            rig = sh3.rig(rt, [h3], sender=sender)
            try:
                rig.scheduler.start()
                await asyncio.wait_for(sender.entered.wait(), sh3.BOUND_S)
                await rig.stop()
                state = rig.scheduler.state
            finally:
                sender.release.set()
                with contextlib.suppress(Exception):
                    await rig.stop()
            after = sh3.op_state(h3, _op_id(h3, ref))
            again = FakeSender()
            fresh = sh3.rig(rt, [h3], sender=again)
            try:
                result = await fresh.tick()
            finally:
                await fresh.stop()
        return state, sender, after, (again, result)

    state, sender, after, (again, result) = asyncio.run(body())
    assert state is SchedulerState.STOPPED
    assert len(sender.calls) == 1
    assert after == ("uncertain", "SENDER_CANCELLED")
    assert again.calls == []
    rep = sh3.report(result, h3)
    assert (rep.recovery.counts, rep.ready.counts) == (sh3.zero_recovery(), sh3.ready_counts())


# --- P07 / P07b -----------------------------------------------------------------------------


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["updated_at"], row["op_id"])


def test_h3_p07_clinic_off_full_page_then_wrap(h3: sh3.SH3Install) -> None:
    limit = 3
    sh3.ready_install(h3)
    ops = [sh3.persisted_accepted_op(h3)[0] for _ in range(limit)]
    page = sh3.pending(h3)
    assert [o["op_id"] for o in page] == ops
    sh2.clear_schedule(h3)
    sc.change(h3, "revoke")

    async def body() -> tuple[sh3.Rig, Any, list[str], Any]:
        async with sh3.runtime(h3) as rt:
            rig = sh3.rig(rt, [h3], recovery_limit=limit)
            try:
                off = await rig.tick()
                states = [sh3.op_state(h3, op)[0] for op in ops]
                sc.change(h3, "grant")
                on = await rig.tick()
            finally:
                await rig.stop()
        return rig, off, states, on

    try:
        rig, off, states, on = asyncio.run(body())
    finally:
        sc.change(h3, "grant")
    a = sh3.report(off, h3)
    assert (a.code, a.recovery.status) == ("DEGRADED", "DEGRADED_ITEMS")
    assert a.recovery.counts == sh3.zero_recovery(refused_activation=limit)
    assert (a.context, a.expire.status, a.ready.status) == ("ok", "ok", "ok")
    assert states == ["accepted"] * limit
    b = sh3.report(on, h3)
    assert (b.code, b.recovery.counts) == ("COMPLETE", sh3.zero_recovery(registered=limit))
    pending_calls = rig.service.calls("accepted_pending")
    assert [c[1] for c in pending_calls] == [limit] * 3
    assert [c[2] for c in pending_calls] == [None, _key(page[-1]), None]  # one bounded wrap
    assert rig.sender.calls == []
    assert [sh3.op_state(h3, op)[0] for op in ops] == ["registered"] * limit


def test_h3_p07b_clinic_off_cap_plus_one(h3: sh3.SH3Install) -> None:
    limit = 3
    sh3.ready_install(h3)
    ops = [sh3.persisted_accepted_op(h3)[0] for _ in range(limit + 1)]
    page = sh3.pending(h3)
    assert [o["op_id"] for o in page] == ops
    sh2.clear_schedule(h3)
    sc.change(h3, "revoke")

    async def body() -> tuple[sh3.Rig, list[Any]]:
        async with sh3.runtime(h3) as rt:
            rig = sh3.rig(rt, [h3], recovery_limit=limit)
            try:
                results = [await rig.tick()]
                sc.change(h3, "grant")
                results += [await rig.tick(), await rig.tick()]
            finally:
                await rig.stop()
        return rig, results

    try:
        rig, results = asyncio.run(body())
    finally:
        sc.change(h3, "grant")
    counts = [sh3.report(r, h3).recovery.counts for r in results]
    assert counts == [sh3.zero_recovery(refused_activation=limit),
                      sh3.zero_recovery(registered=1), sh3.zero_recovery(registered=limit)]
    assert [c[2] for c in rig.service.calls("accepted_pending")] == [
        None, _key(page[limit - 1]), None]
    assert rig.sender.calls == []
    assert [sh3.op_state(h3, op)[0] for op in ops] == ["registered"] * (limit + 1)


# --- P08 ------------------------------------------------------------------------------------


def test_h3_p08_clinic_off_between_examine_and_consume(h3: sh3.SH3Install) -> None:
    sh3.ready_install(h3)
    _p, _ph, ref, _z = sh3.sendable(h3)

    async def barrier(result: dict[str, Any]) -> None:
        if result.get("outcome") == "claimed":
            sc.change(h3, "revoke")  # TEST barrier: examine committed, consume not yet run

    try:
        rig, (result,) = asyncio.run(_ticks(h3, after_examine=barrier))
    finally:
        sc.change(h3, "grant")
    rep = sh3.report(result, h3)
    assert rig.sender.calls == []
    assert [c[0] for c in rig.service.log if c[0] in ("examine", "consume")] == [
        "examine", "consume"]
    assert (rep.code, rep.ready.status) == ("DEGRADED", "ACTIVATION_REFUSED")
    op_id = _op_id(h3, ref)
    assert sh3.op_state(h3, op_id)[0] == "attempting"
    ticket = sh2.ticket_row(h3, op_id)
    assert ticket is not None and ticket["consumed_at"] is None
    assert sh.windows_for(h3, ref) == []


# --- P09 ------------------------------------------------------------------------------------


def test_h3_p09_capability_refused_tenant_is_isolated(h3: sh3.SH3Install,
                                                      h3_b: sh3.SH3Install) -> None:
    for install in (h3, h3_b):
        sh3.ready_install(install)
    _p, _ph, ref_a, _z = sh3.sendable(h3)
    _p, _ph, ref_b, _z = sh3.sendable(h3_b)

    async def body() -> tuple[sh3.Rig, Any]:
        async with sh3.runtime(h3) as rt:
            installs = [h3, h3_b]
            rig = sh3.rig(rt, installs, contexts=sh3.dispatch_contexts(
                rt, installs, denied=[h3_b.tenant_id]))
            try:
                return rig, await rig.tick()
            finally:
                await rig.stop()

    rig, result = asyncio.run(body())
    assert [t.tenant for t in result.tenants] == [h3.tenant_id, h3_b.tenant_id]
    a, b = sh3.report(result, h3), sh3.report(result, h3_b)
    assert (b.code, b.context) == ("DEGRADED", "CAPABILITY_REFUSED")
    assert (b.expire.status, b.recovery.status, b.ready.status) == ("NOT_RUN",) * 3
    assert (a.code, a.ready.counts) == ("COMPLETE", sh3.ready_counts(1, 1, 1))
    assert result.codes == {"COMPLETE": 1, "DEGRADED": 1}
    assert sh3.sent_ops(rig.sender) == [_op_id(h3, ref_a)]
    assert sh2.op_for(h3_b, ref_b) is None


# --- P10 ------------------------------------------------------------------------------------


def test_h3_p10_no_phone_name_or_text_anywhere(h3: sh3.SH3Install,
                                                caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.m05.outbound import FakeSender

    caplog.set_level(logging.DEBUG)
    name = "Zephyrine"
    sh3.ready_install(h3)
    _p, phone, ref, _z = sh3.sendable(h3, first_name=name)
    assert phone is not None
    sh3.persisted_accepted_op(h3)  # a recovery item in the same tick
    sender = FakeSender(fail="leaky")  # adversarial: its exception carries phone and text
    rig, (result,) = asyncio.run(_ticks(h3, sender=sender))
    assert len(sender.calls) == 1
    text = sender.calls[0][2]
    assert name in text and sender.calls[0][1] == phone  # the secrets are real
    formatter = logging.Formatter()
    surfaces = [r.getMessage() for r in caplog.records]
    surfaces += [repr(r.args) for r in caplog.records]
    surfaces += [formatter.formatException(r.exc_info) for r in caplog.records if r.exc_info]
    surfaces += [repr(result), str(result), repr(result.tenants), repr(rig.scheduler)]
    for surface in surfaces:
        for secret in (phone, name, text):
            assert secret not in surface
    assert sh3.op_state(h3, _op_id(h3, ref)) == ("uncertain", "SENDER_ERROR")
    assert [r for r in caplog.records if r.name == sh3.LOGGER], "no tick summary line"


# --- P11 t075 shape -------------------------------------------------------------------------


def _function_acl(install: sa.SAInstall, name: str) -> list[tuple[Any, ...]]:
    return sorted(sa.admin(install, (
        "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
        " x.privilege_type, pg_get_userbyid(x.grantor), x.is_grantable"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,"
        " LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x"
        " WHERE n.nspname = %s AND p.proname = %s"), (install.schema, name)))


def _function_shape(install: sa.SAInstall, name: str) -> tuple[Any, ...]:
    rows = sa.admin(install, "SELECT p.provolatile, p.prosecdef, p.proretset,"
                             " pg_get_function_identity_arguments(p.oid),"
                             " p.prorettype::regtype::text, pg_get_userbyid(p.proowner)"
                             " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                             " WHERE n.nspname = %s AND p.proname = %s", (install.schema, name))
    assert len(rows) == 1, (name, rows)
    return tuple(rows[0])


def test_h3_p11a_install_binding_acl_profile(shape: sh3.SH3Install) -> None:
    from haloflow.composition import APPROVED_TENANT_MIGRATIONS
    from haloflow.m05.intake_statements import DISPATCH_KEY
    from haloflow.m05.outbound_statements import HELD_KEY, SH_STATEMENTS
    from haloflow.m05.outbound_units import SH_INSTALLED_STATE_PROFILES
    from haloflow.m05.reminder_scheduler_statements import (
        ACCEPTED_PENDING_KEY,
        SH3_PROTECTED,
        SH3_STATEMENTS,
    )
    from haloflow.m05.reminder_scheduler_units import (
        SH3_INSTALLED_STATE_PROFILES,
        SH3_TENANT_MIGRATIONS,
    )

    shape.require()
    assert tuple(SH3_TENANT_MIGRATIONS) == sh3.SH3_UNIT_IDS
    assert sh3.sh3_registry().target_version == sh3.SH3_TARGET == 75
    production_ids = {unit_id for group in APPROVED_TENANT_MIGRATIONS for unit_id in group}
    assert not production_ids & set(sh3.SH3_UNIT_IDS)
    assert set(SH3_INSTALLED_STATE_PROFILES) == set(sh3.SH3_UNIT_IDS)
    # The t056 pattern: identical profile except the function name; identical ACL.
    assert SH3_INSTALLED_STATE_PROFILES[sh3.SH3_UNIT_IDS[0]] == dataclasses.replace(
        SH_INSTALLED_STATE_PROFILES[sh3.HELD_UNIT], function_name=sh3.PENDING_FUNCTION)
    owner = (sa.M05_OWNER, "EXECUTE", sa.M05_OWNER, False)
    assert _function_acl(shape, sh3.PENDING_FUNCTION) == sorted(
        [owner, (sa.RUNTIME, "EXECUTE", sa.M05_OWNER, False)])
    assert _function_acl(shape, sh3.PENDING_FUNCTION) == _function_acl(shape, sh3.HELD_FUNCTION)
    assert _function_shape(shape, sh3.PENDING_FUNCTION) == _function_shape(
        shape, sh3.HELD_FUNCTION)
    # Statement binding: the new gateway key, dispatch capability, held's mode (DISCLOSED:
    # the pins fix key, capability and function; the mode follows the t056 binding).
    assert ACCEPTED_PENDING_KEY == sh3.PENDING_KEY
    assert dict(SH3_STATEMENTS) == {ACCEPTED_PENDING_KEY: (
        SH_STATEMENTS[HELD_KEY][0], DISPATCH_KEY,
        "SELECT m05_outbound_accepted_pending(%(command)s)")}
    assert isinstance(SH3_PROTECTED, frozenset) and not SH3_PROTECTED
    compiled = sh3.sh3_catalog()
    bound = compiled.catalog.resolve(ACCEPTED_PENDING_KEY)
    assert (bound.required_capability, bound.query) == (
        DISPATCH_KEY, "SELECT m05_outbound_accepted_pending(%(command)s)")
    assert ACCEPTED_PENDING_KEY not in compiled.activation_protected
    assert compiled.activation_protected == sh2.protected()


TS = "2026-11-09T10:00:00.000000Z"


def _malformed(case: str) -> dict[str, Any]:
    op = sa.new_id()
    good_after = {"updated_at": TS, "op_id": op}
    base = sh3.pending_cmd(10, None)
    if case == "extra-key":
        return {**base, "extra": 1}
    if case in ("missing-after", "missing-limit", "missing-attribution"):
        cmd = dict(base)
        del cmd[case.removeprefix("missing-")]
        return cmd
    limits = {"limit-string": "10", "limit-float": 1.5, "limit-zero": 0, "limit-1001": 1001,
              "limit-negative": -1, "limit-bool": True, "limit-null": None,
              "limit-huge": 10 ** 12}
    if case in limits:
        return sh3.pending_cmd(limits[case], None)
    afters: dict[str, Any] = {
        "after-list": [TS, op], "after-string": TS, "after-number": 1, "after-empty": {},
        "after-only-ts": {"updated_at": TS}, "after-only-op": {"op_id": op},
        "after-extra": {**good_after, "x": 1},
        "after-ts-null": {"updated_at": None, "op_id": op},
        "after-op-null": {"updated_at": TS, "op_id": None},
        "after-ts-no-micros": {"updated_at": "2026-11-09T10:00:00Z", "op_id": op},
        "after-ts-offset": {"updated_at": "2026-11-09T10:00:00.000000+00:00", "op_id": op},
        "after-ts-space": {"updated_at": "2026-11-09 10:00:00.000000Z", "op_id": op},
        "after-ts-millis": {"updated_at": "2026-11-09T10:00:00.000Z", "op_id": op},
        "after-ts-number": {"updated_at": 1, "op_id": op},
        "after-op-upper": {"updated_at": TS, "op_id": op.upper()},
        "after-op-bad": {"updated_at": TS, "op_id": "not-a-uuid"},
    }
    if case in afters:
        return sh3.pending_cmd(10, afters[case])
    if case == "schema-version-2":
        return {**base, "schema_version": 2}
    if case == "attribution-shape":
        attribution = dict(base["attribution"])
        del attribution["purpose_code"]
        return {**base, "attribution": attribution}
    raise AssertionError(case)


MALFORMED = (
    "extra-key", "missing-after", "missing-limit", "missing-attribution", "limit-string",
    "limit-float", "limit-zero", "limit-1001", "limit-negative", "limit-bool", "limit-null",
    "limit-huge", "after-list", "after-string", "after-number", "after-empty",
    "after-only-ts", "after-only-op", "after-extra", "after-ts-null", "after-op-null",
    "after-ts-no-micros", "after-ts-offset", "after-ts-space", "after-ts-millis",
    "after-ts-number", "after-op-upper", "after-op-bad", "schema-version-2",
    "attribution-shape",
)


@pytest.mark.parametrize("case", MALFORMED)
def test_h3_p11b_command_malformed(shape: sh3.SH3Install, case: str) -> None:
    shape.require()
    sh2.refused(shape, sh3.PENDING_FUNCTION, _malformed(case), "COMMAND_MALFORMED")


def test_h3_p11b_bounds_accepted(shape: sh3.SH3Install) -> None:
    """Control: limit 1 and 1000, after null, and a well-formed cursor are accepted."""

    shape.require()
    for limit in (1, 1000):
        assert isinstance(sh3.pending(shape, limit), list)
    assert isinstance(sh3.pending(shape, 10, {"updated_at": TS, "op_id": sa.new_id()}), list)


def test_h3_p11c_attribution_capability_activation(shape: sh3.SH3Install) -> None:
    import service_support as sv

    shape.require()
    for principal, kind in ((sa.W_INGRESS, "workload"), (sa.W_OTHER, "workload"),
                            (sa.ACTOR, "actor")):
        cmd = sh3.pending_cmd(10, None)
        cmd["attribution"] = sa.attribution(principal, kind=kind)
        sh2.refused(shape, sh3.PENDING_FUNCTION, cmd, "WORKLOAD_NOT_AUTHORIZED")

    async def body() -> None:
        from haloflow.m05.outbound import OutboundRefused

        async with sh3.runtime(shape) as rt:
            svc = sh3.service(rt)
            wrong = await sv.context(rt, shape, sa.W_DISPATCH, sa.CAPS["inbound"],
                                     kind="workload")
            with pytest.raises(OutboundRefused) as caps:
                await svc.accepted_pending(wrong, 10, None)
            assert caps.value.code == "CAPABILITIES_INVALID"
            actor = await sv.context(rt, shape, sa.ACTOR, sa.CAPS["dispatch"])
            with pytest.raises(OutboundRefused) as kind_refused:
                await svc.accepted_pending(actor, 10, None)
            assert kind_refused.value.code == "PRINCIPAL_NOT_WORKLOAD"
            sc.change(shape, "revoke")  # t075 is bookkeeping: not activation-protected
            try:
                ctx = await sh3.dispatch_context(rt, shape)
                result = await svc.accepted_pending(ctx, 10, None)
                assert result["schema_version"] == 1 and isinstance(result["ops"], list)
            finally:
                sc.change(shape, "grant")

    asyncio.run(body())


TIE = "2090-01-01 00:00:00+00"
TIE_TEXT = "2090-01-01T00:00:00.000000Z"


def test_h3_p11d_accepted_only_ordered_strict_cursor_no_writes(shape: sh3.SH3Install) -> None:
    shape.require()
    sc.change(shape, "grant")
    accepted = [sh3.op_in_state(shape, "accepted")[0] for _ in range(3)]
    others = [sh3.op_in_state(shape, state)[0] for state in (
        "prepared", "attempting", "expired", "uncertain", "refused", "accepted_unregistered",
        "registered")]
    before_rows, before_h2 = sh3.ops_with_xmin(shape), sh2.h2_snapshot(shape)
    full = sh3.pending(shape)
    ids = [o["op_id"] for o in full]
    now_accepted = {str(r[0]) for r in sa.admin(shape, sa.q(
        shape, "SELECT op_id FROM {s}.m05_outbound_ops WHERE state = 'accepted'"))}
    assert set(ids) == now_accepted and set(accepted) <= set(ids)
    assert not set(others) & set(ids)
    for row in full:
        assert set(row) == {"op_id", "updated_at"}, row
        assert re.fullmatch(sa.TIMESTAMP_PATTERN, row["updated_at"]), row
    keys = [_key(o) for o in full]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    # Strictly after the cursor, and paging reproduces the full ascending list.
    assert sh3.pending(shape, 1000, sh3.cursor(full[0])) == full[1:]
    assert sh3.pending(shape, 1000, sh3.cursor(full[-1])) == []
    collected: list[dict[str, Any]] = []
    after: Any = None
    for _ in range(len(full) + 1):
        page = sh3.pending(shape, 2, after)
        collected += page
        if len(page) < 2:
            break
        after = sh3.cursor(page[-1])
    assert collected == full
    # Never writes: every op row's values AND xmin unchanged; H2 tables unchanged.
    assert sh3.ops_with_xmin(shape) == before_rows
    assert sh2.h2_snapshot(shape) == before_h2
    # Tie order: equal updated_at -> op_id ascending; the cursor is strict on both columns.
    lo, hi = sorted(accepted[:2])
    sh3.set_updated_at(shape, [lo, hi], TIE)  # FIXTURE
    tail = sh3.pending(shape)[-2:]
    assert tail == [{"op_id": lo, "updated_at": TIE_TEXT}, {"op_id": hi, "updated_at": TIE_TEXT}]
    assert sh3.pending(shape, 1000, {"updated_at": TIE_TEXT, "op_id": lo}) == [tail[1]]
    assert sh3.pending(shape, 1000, {"updated_at": TIE_TEXT, "op_id": hi}) == []
    assert sh3.pending(shape, 1, None)[0] == full[0]


def test_h3_p11e_tenant_isolation_and_gateway_key(shape: sh3.SH3Install,
                                                  shape_other: sh3.SH3Install) -> None:
    for install in (shape, shape_other):
        install.require()
        sc.change(install, "grant")
    same = sa.new_id()
    _p, _ph, ref_a, _z = sh3.sendable(shape)
    claimed_a = sh2.examine(shape, ref_a, op_id=same)
    assert claimed_a["outcome"] == "claimed", claimed_a
    assert sh2.consume(shape, same, claimed_a["attempt_id"])["outcome"] == "send"
    sh.outcome(shape, same, claimed_a["attempt_id"], "accepted", provider_ref="fake-h3-iso")
    _p, _ph, ref_b, _z = sh3.sendable(shape_other)
    claimed_b = sh2.examine(shape_other, ref_b, op_id=same)
    assert claimed_b["outcome"] == "claimed", claimed_b
    assert sh2.consume(shape_other, same, claimed_b["attempt_id"])["outcome"] == "send"
    sh.outcome(shape_other, same, claimed_b["attempt_id"], "uncertain", code="FAKE_TIMEOUT")
    before_b = sh3.ops_with_xmin(shape_other)
    raw_a, raw_b = sh3.pending(shape), sh3.pending(shape_other)
    assert same in {o["op_id"] for o in raw_a}
    assert same not in {o["op_id"] for o in raw_b}
    assert sh3.ops_with_xmin(shape_other) == before_b

    async def body() -> None:
        async with sh3.runtime(shape) as rt:
            svc = sh3.service(rt)
            ca = await sh3.dispatch_context(rt, shape)
            cb = await sh3.dispatch_context(rt, shape_other)
            got_a = await svc.accepted_pending(ca, 1000, None)
            got_b = await svc.accepted_pending(cb, 1000, None)
            assert got_a == {"schema_version": 1, "ops": raw_a}
            assert got_b == {"schema_version": 1, "ops": raw_b}
            first = raw_a[0]
            later = await svc.accepted_pending(ca, 1000, (first["updated_at"], first["op_id"]))
            assert later["ops"] == raw_a[1:]

    asyncio.run(body())
