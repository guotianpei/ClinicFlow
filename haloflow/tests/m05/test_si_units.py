"""L-1S package S-I (simulated EHR confirmation write-back) — unit rows (no database).

RED BY DESIGN until `haloflow.m05.writeback` and `haloflow.m05.simulated_ehr` exist. Every
S-I import is deferred into the row body, so the expected red cause of every S-I row is
ModuleNotFoundError for one of those modules, never a collection error.

Governing: S-I investigation v1 + v1r1 + v1r2, red-test packet v1 (Work Session
2026-11-10), Codex Option A + red-test ALIGN with pins (`codex_si-red-packet-v1-review.md`,
relay 20261110T134528Z), owner decision C5 (2026-11-10: one staff item kind + reason;
retries only on R1 unavailable, 1/5/15 min; about 42 min at the default interval as a
conditional target). Synthetic only; no real EHR; nothing composed.

Interface pins (the S-I Python contract these rows freeze):

`haloflow.m05.simulated_ehr`
- STATUSES = ("positive", "neutral", "negative", "unknown", "empty", "checked_in",
  "checked_out", "cancelled").
- `EhrRead(status, appointment_id, start_at, provider_ref, department_ref, replaced_by)`,
  frozen dataclass; `start_at` an aware datetime; refs `str | None`.
- `WriteResult(outcome)`, outcome in {"applied", "deadline_passed", "permit_closed",
  "not_found"}.
- `EhrUnavailable(Exception)`: a read or write that could not reach the synthetic EHR.
- `SimulatedEhr(*, clock)`: `clock()` returns an aware datetime. The port's own time is
  NON-DECREASING: it is max(every value `clock()` has returned). `put(read)` (fixture);
  `async read(appointment_id) -> EhrRead`; `async write_confirmation(appointment_id, *,
  permit_id, apply_deadline) -> WriteResult`; `async close_permit(permit_id)` (the
  reconciler closes a permit BEFORE it reads; a closed permit is refused for ever,
  whatever the clock); `snapshot()` / `SimulatedEhr.restore(snapshot, *, clock)` (EHR
  state, applied writes and closed permits survive a HaloFlow restart); `lock`, the one
  `asyncio.Lock` taken by every read, write and close (exposed for fault injection);
  `on_locked`, an optional async hook awaited immediately after the lock is acquired by
  `write_confirmation`; `writes`, the list of applied `(appointment_id, permit_id)`;
  `fail_reads(n)` / `fail_writes(n)` (the next n calls raise EhrUnavailable).
- The deadline check and the state change are done together in the SYNCHRONOUS method
  `_apply_locked(appointment_id, permit_id, apply_deadline) -> WriteResult`, called with
  the lock held; `write_confirmation` awaits nothing between that call and its return.

`haloflow.m05.writeback`
- RETRY_DELAYS_S = (60, 300, 900); `retry_delay_s(failures) -> int | None` (1 -> 60,
  2 -> 300, 3 -> 900, 4 -> None = raise the staff item); refuses failures < 1.
- `worst_case_escalation_s(interval_s, *, call_timeout_s=10) -> int` =
  interval + sum(RETRY_DELAYS_S) + 3 * interval + 4 * call_timeout (the C4 conditional
  target; 300 s -> 2500 s, about 42 min).
- REASONS = ("status_changed", "declined_or_unknown", "ehr_unreachable",
  "could_not_verify", "no_baseline"); ITEM_KIND = "ehr_confirmation_check".
- `WritebackRefused(code)`, `WritebackCommitUnknown`.
- `BaselineWorker(inner, capture)`: `inner` must be an `OutboundWorker` (else
  WritebackRefused("WORKER_INVALID")); `capture(context, op_id, attempt_id)` is the T_cap
  call. `run_claimed(context, op_id, attempt_id)` calls capture, then ALWAYS the inner
  `run_claimed` with the same arguments, returning its value unchanged, whatever capture
  returned or raised (an `Exception`, including WritebackCommitUnknown); only
  `asyncio.CancelledError` from capture propagates without calling inner. `recover`
  forwards unchanged and never captures. Logs on logger "haloflow.m05.writeback" carry
  the exception CLASS name only.
- `WritebackWorker(service, ehr, *, write_timeout_s=10.0, read_timeout_s=10.0)`;
  `async run_one(context, op_id) -> str`:
  1. `service.claim(context, op_id)` -> {"outcome": "claimed", "fence", "appointment_id",
     "phase": "check" | "reconcile", "permit_id" (reconcile only)} or any other outcome ->
     return "not_claimed" (WritebackRefused -> "not_claimed" too);
  2. phase "check": R1 = `ehr.read` bounded by read_timeout_s (EhrUnavailable or timeout
     = read failed, no exception escapes); `service.decide(context, op_id, fence, read,
     read_failed)` -> {"outcome": "permitted", "permit_id"} | {"outcome": "item",
     "reason"} | {"outcome": "skipped"} | {"outcome": "deferred"} -> return "item_raised" /
     "skipped" / "deferred" for the last three;
  3. permitted: `service.spend(context, op_id, fence, permit_id)` -> {"outcome": "spent",
     "apply_deadline"}; anything else or WritebackRefused -> "spend_refused";
     WritebackCommitUnknown -> "spend_unknown" (NO write);
  4. W1: exactly one `ehr.write_confirmation(appointment_id, permit_id=..,
     apply_deadline=..)`, bounded by write_timeout_s; a timeout or any Exception is
     write outcome "unknown" (attempted; never retried);
  5. R2 = `ehr.read` (failure -> read None); `service.settle(context, op_id, fence,
     permit_id, write_outcome, read)` -> {"outcome": "observed_positive" | "item"} ->
     "observed_positive" / "item_raised"; WritebackRefused -> "stale".
  Phase "reconcile" (a spent, unsettled permit, claimed only after its deadline + margin
  in database time): `ehr.close_permit(permit_id)`, then R2 and settle with write outcome
  "unknown". `asyncio.CancelledError` always propagates; any other exception from the
  service propagates too (the next run resumes from the persisted state). The worker never
  logs a phone, name, provider, department, appointment id or exception text.

Rows (requirement in brackets, packet v1 numbering):
U-W01..W05 [SI-R4] wrapper equivalence for each H2 run_claimed outcome; U-W06..W08 [R4]
capture refused / raised / commit-unknown -> inner unchanged; U-W09 / W09b [R4]
cancellation; U-W11 [R4] recover; U-W12 [R4] constructor; U-L01..L08 [R7] apply deadline,
delayed lock acquisition, serialization, boundary, mutation-without-await (static), port
clock, OLD delayed writer after the clock moves backwards (and across restart), inputs;
U-T01..T04 [R10] retry delays, conditional worst case by simulation, immediate items, a
later success; U-S06 / U-S07 [R6] one write per spent permit; U-K03 / U-K04 [R8] spend
commit-unknown, write timeout; U-Q01 [R6/R8] reconcile phase; U-V01 [R16] privacy;
U-N01 [R17] not composed.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import pathlib
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"
LOGGER = "haloflow.m05.writeback"
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
APPT = "appt-0001"
PHONE, NAME, PROVIDER, DEPARTMENT = "+15550001234", "Synthia", "prov-42", "dept-7"
H2_OUTCOMES = ("accepted", "refused", "uncertain", "not_sent", "consume_unknown")
BOUND_S = 5.0


def _ehr_mod() -> Any:
    import haloflow.m05.simulated_ehr as module

    return module


def _wb_mod() -> Any:
    import haloflow.m05.writeback as module

    return module


class Clock:
    """A settable aware clock (moves forward or backward only when a row says so)."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _read(status: str = "neutral", **over: Any) -> Any:
    fields: dict[str, Any] = {"status": status, "appointment_id": APPT,
                              "start_at": T0 + timedelta(days=2),
                              "provider_ref": PROVIDER, "department_ref": DEPARTMENT,
                              "replaced_by": None}
    fields.update(over)
    return _ehr_mod().EhrRead(**fields)


def _ehr(clock: Clock, status: str = "neutral") -> Any:
    ehr = _ehr_mod().SimulatedEhr(clock=clock)
    ehr.put(_read(status))
    return ehr


async def _bounded(awaitable: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(awaitable, BOUND_S)


# --- U-W: BaselineWorker ----------------------------------------------------------------------


def _inner(outcome: str = "accepted", *, raise_cancel: bool = False) -> Any:
    """A real OutboundWorker subclass whose run_claimed is scripted (records its calls)."""

    from haloflow.m05.outbound import FakeSender, OutboundWorker

    class Inner(OutboundWorker):
        def __init__(self) -> None:
            super().__init__(object(), FakeSender(), lambda _inputs: "")
            self.calls: list[tuple[Any, ...]] = []

        async def run_claimed(self, context: Any, op_id: Any, attempt_id: Any) -> str:
            self.calls.append(("run_claimed", context, op_id, attempt_id))
            if raise_cancel:
                raise asyncio.CancelledError
            return outcome

        async def recover(self, context: Any, op_id: Any) -> Any:
            self.calls.append(("recover", context, op_id))
            return {"outcome": "registered"}

    return Inner()


@dataclass
class Capture:
    mode: str = "captured"  # captured | refused | raise | unknown | cancel
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    order: list[str] | None = None

    async def __call__(self, context: Any, op_id: Any, attempt_id: Any) -> str:
        self.calls.append((context, op_id, attempt_id))
        if self.order is not None:
            self.order.append("capture")
        if self.mode == "raise":
            raise RuntimeError(f"capture failed for {PHONE} {NAME}")
        if self.mode == "unknown":
            raise _wb_mod().WritebackCommitUnknown()
        if self.mode == "cancel":
            raise asyncio.CancelledError
        return self.mode


@pytest.mark.parametrize("outcome", H2_OUTCOMES)
def test_si_uw01_05_wrapper_returns_every_h2_outcome_unchanged(outcome: str) -> None:
    async def body() -> None:
        inner, capture = _inner(outcome), Capture()
        worker = _wb_mod().BaselineWorker(inner, capture)
        ctx, op, att = object(), uuid.uuid4(), uuid.uuid4()
        assert await _bounded(worker.run_claimed(ctx, op, att)) == outcome
        assert capture.calls == [(ctx, op, att)]
        assert inner.calls == [("run_claimed", ctx, op, att)]

    asyncio.run(body())


def test_si_uw01b_capture_runs_before_inner() -> None:
    async def body() -> None:
        order: list[str] = []
        inner = _inner()
        original = inner.run_claimed

        async def tracked(*args: Any) -> str:
            order.append("inner")
            result: str = await original(*args)
            return result

        inner.run_claimed = tracked
        worker = _wb_mod().BaselineWorker(inner, Capture(order=order))
        await _bounded(worker.run_claimed(object(), uuid.uuid4(), uuid.uuid4()))
        assert order == ["capture", "inner"]

    asyncio.run(body())


@pytest.mark.parametrize("mode", ["refused", "raise", "unknown"])
def test_si_uw06_08_capture_failure_never_blocks_or_alters_the_send(
        mode: str, caplog: pytest.LogCaptureFixture) -> None:
    async def body() -> None:
        inner = _inner("accepted")
        worker = _wb_mod().BaselineWorker(inner, Capture(mode=mode))
        ctx, op, att = object(), uuid.uuid4(), uuid.uuid4()
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert await _bounded(worker.run_claimed(ctx, op, att)) == "accepted"
        assert inner.calls == [("run_claimed", ctx, op, att)]
        text = " ".join(r.getMessage() for r in caplog.records)
        assert PHONE not in text and NAME not in text and "capture failed" not in text
        if mode == "raise":
            assert "RuntimeError" in text  # class name only

    asyncio.run(body())


def test_si_uw09_cancellation_in_capture_propagates_without_the_inner_call() -> None:
    async def body() -> None:
        inner = _inner()
        worker = _wb_mod().BaselineWorker(inner, Capture(mode="cancel"))
        with pytest.raises(asyncio.CancelledError):
            await _bounded(worker.run_claimed(object(), uuid.uuid4(), uuid.uuid4()))
        assert inner.calls == []

    asyncio.run(body())


def test_si_uw09b_cancellation_in_inner_propagates() -> None:
    async def body() -> None:
        inner = _inner(raise_cancel=True)
        worker = _wb_mod().BaselineWorker(inner, Capture())
        with pytest.raises(asyncio.CancelledError):
            await _bounded(worker.run_claimed(object(), uuid.uuid4(), uuid.uuid4()))
        assert len(inner.calls) == 1

    asyncio.run(body())


def test_si_uw11_recover_forwards_and_never_captures() -> None:
    async def body() -> None:
        inner, capture = _inner(), Capture()
        worker = _wb_mod().BaselineWorker(inner, capture)
        ctx, op = object(), uuid.uuid4()
        assert await _bounded(worker.recover(ctx, op)) == {"outcome": "registered"}
        assert inner.calls == [("recover", ctx, op)]
        assert capture.calls == []

    asyncio.run(body())


@pytest.mark.parametrize("bad", [None, object(), "worker"])
def test_si_uw12_constructor_requires_an_outbound_worker(bad: Any) -> None:
    mod = _wb_mod()
    with pytest.raises(mod.WritebackRefused) as refused:
        mod.BaselineWorker(bad, Capture())
    assert refused.value.code == "WORKER_INVALID"


# --- U-L: SimulatedEhr apply deadline ---------------------------------------------------------


def test_si_ul01_delayed_writer_after_the_deadline_changes_nothing() -> None:
    async def body() -> None:
        clock = Clock()
        ehr, permit = _ehr(clock), str(uuid.uuid4())
        deadline = clock.now + timedelta(seconds=10)
        clock.advance(10 + 30)  # A paused between T4 and W1, past deadline + margin
        before = await ehr.read(APPT)
        result = await _bounded(ehr.write_confirmation(APPT, permit_id=permit,
                                                       apply_deadline=deadline))
        assert result.outcome == "deadline_passed"
        assert ehr.writes == []
        assert await ehr.read(APPT) == before

    asyncio.run(body())


def test_si_ul02_delayed_lock_acquisition_is_checked_when_the_lock_is_acquired() -> None:
    async def body() -> None:
        clock = Clock()
        ehr, permit = _ehr(clock), str(uuid.uuid4())
        deadline = clock.now + timedelta(seconds=10)
        await ehr.lock.acquire()  # A's call starts in time but cannot take the lock
        task = asyncio.create_task(ehr.write_confirmation(APPT, permit_id=permit,
                                                          apply_deadline=deadline))
        await asyncio.sleep(0)
        clock.advance(11)
        ehr.lock.release()
        assert (await _bounded(task)).outcome == "deadline_passed"
        assert ehr.writes == []

    asyncio.run(body())


def test_si_ul03_a_read_waits_for_a_write_in_progress_then_observes_it() -> None:
    async def body() -> None:
        clock = Clock()
        ehr, permit = _ehr(clock), str(uuid.uuid4())
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            entered.set()
            await release.wait()

        ehr.on_locked = hold
        write = asyncio.create_task(ehr.write_confirmation(
            APPT, permit_id=permit, apply_deadline=clock.now + timedelta(seconds=10)))
        await _bounded(entered.wait())
        read = asyncio.create_task(ehr.read(APPT))
        await asyncio.sleep(0)
        assert not read.done()  # serialized behind the write
        release.set()
        assert (await _bounded(write)).outcome == "applied"
        assert (await _bounded(read)).status == "positive"

    asyncio.run(body())


def test_si_ul04_deadline_boundary_is_inclusive() -> None:
    async def body() -> None:
        clock = Clock()
        deadline = clock.now + timedelta(seconds=10)
        ehr = _ehr(clock)
        clock.now = deadline
        assert (await ehr.write_confirmation(APPT, permit_id=str(uuid.uuid4()),
                                             apply_deadline=deadline)).outcome == "applied"
        late = _ehr(Clock(deadline + timedelta(microseconds=1)))
        assert (await late.write_confirmation(APPT, permit_id=str(uuid.uuid4()),
                                              apply_deadline=deadline)).outcome \
            == "deadline_passed"
        assert late.writes == []

    asyncio.run(body())


def test_si_ul05_deadline_check_and_mutation_share_one_synchronous_step() -> None:
    mod = _ehr_mod()
    apply_locked = mod.SimulatedEhr._apply_locked
    assert not inspect.iscoroutinefunction(apply_locked)
    tree = ast.parse(inspect.getsource(mod.SimulatedEhr.write_confirmation).lstrip())
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "_apply_locked"]
    assert len(calls) == 1, "exactly one call to _apply_locked"
    # The _apply_locked call is the return value inside `async with self.lock`, so nothing
    # is awaited between the check and the state change.
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)]
    assert len(withs) == 1
    last = withs[0].body[-1]
    assert isinstance(last, ast.Return) and isinstance(last.value, ast.Call)
    assert isinstance(last.value.func, ast.Attribute)
    assert last.value.func.attr == "_apply_locked"


def test_si_ul06_port_time_never_goes_backwards() -> None:
    async def body() -> None:
        clock = Clock()
        ehr = _ehr(clock)
        deadline = clock.now + timedelta(seconds=10)
        clock.advance(20)
        await ehr.read(APPT)  # the port has now seen T0 + 20 s
        clock.now = T0  # the injected clock moves backwards
        assert (await ehr.write_confirmation(APPT, permit_id=str(uuid.uuid4()),
                                             apply_deadline=deadline)).outcome \
            == "deadline_passed"
        assert ehr.writes == []

    asyncio.run(body())


@pytest.mark.parametrize("restart", [False, True])
def test_si_ul07_old_spent_writer_after_reconcile_and_a_backwards_clock(restart: bool) -> None:
    async def body() -> None:
        mod = _ehr_mod()
        clock = Clock()
        ehr, permit = _ehr(clock), str(uuid.uuid4())
        deadline = clock.now + timedelta(seconds=10)  # A spent its permit at T0
        clock.advance(60)  # B: past deadline + margin, closes the permit, then reads
        await ehr.close_permit(permit)
        assert (await ehr.read(APPT)).status == "neutral"
        if restart:  # HaloFlow restarts; the synthetic EHR keeps its state
            clock = Clock(T0)
            ehr = mod.SimulatedEhr.restore(ehr.snapshot(), clock=clock)
        else:
            clock.now = T0  # the clock moves backwards before A resumes
        result = await _bounded(ehr.write_confirmation(APPT, permit_id=permit,
                                                       apply_deadline=deadline))
        assert result.outcome == "permit_closed"
        assert ehr.writes == []
        assert (await ehr.read(APPT)).status == "neutral"

    asyncio.run(body())


@pytest.mark.parametrize("deadline", [None, datetime(2026, 11, 10, 15, 0), "2026-11-10"])
def test_si_ul08_write_requires_an_aware_deadline(deadline: Any) -> None:
    async def body() -> None:
        ehr = _ehr(Clock())
        with pytest.raises((TypeError, ValueError)):
            await ehr.write_confirmation(APPT, permit_id=str(uuid.uuid4()),
                                         apply_deadline=deadline)
        assert ehr.writes == []

    asyncio.run(body())


# --- a service model for the worker rows ------------------------------------------------------


@dataclass
class Model:
    """An in-memory model of the S-I service contract (claim / decide / spend / settle):
    one op, R1 failures counted with RETRY_DELAYS_S, an item after the fourth failure. It
    models only what the worker can observe; the SQL rows pin the real behaviour."""

    clock: Clock
    decide_outcome: dict[str, Any] = field(default_factory=lambda: {"outcome": "permitted"})
    spend_mode: str = "spent"  # spent | refused | unknown
    settle_outcome: str = "observed_positive"
    phase: str = "check"
    failures: int = 0
    next_at: datetime | None = None
    item_at: datetime | None = None
    log: list[tuple[Any, ...]] = field(default_factory=list)

    def due(self) -> bool:
        return self.item_at is None and (self.next_at is None or self.clock.now >= self.next_at)

    async def claim(self, context: Any, op_id: Any) -> dict[str, Any]:
        self.log.append(("claim",))
        if not self.due():
            return {"outcome": "not_due"}
        claimed = {"outcome": "claimed", "fence": len(self.log), "appointment_id": APPT,
                   "phase": self.phase}
        if self.phase == "reconcile":
            claimed["permit_id"] = "permit-old"
        return claimed

    async def decide(self, context: Any, op_id: Any, fence: int, read: Any,
                     read_failed: bool) -> dict[str, Any]:
        self.log.append(("decide", read_failed, None if read is None else read.status))
        if read_failed:
            self.failures += 1
            delay = _wb_mod().retry_delay_s(self.failures)
            if delay is None:
                self.item_at = self.clock.now
                return {"outcome": "item", "reason": "ehr_unreachable"}
            self.next_at = self.clock.now + timedelta(seconds=delay)
            return {"outcome": "deferred"}
        if self.decide_outcome.get("outcome") == "item":
            self.item_at = self.clock.now
        return dict(self.decide_outcome, permit_id="permit-1")

    async def spend(self, context: Any, op_id: Any, fence: int, permit_id: str
                    ) -> dict[str, Any]:
        self.log.append(("spend", permit_id))
        if self.spend_mode == "unknown":
            raise _wb_mod().WritebackCommitUnknown()
        if self.spend_mode == "refused":
            return {"outcome": "refused", "code": "PERMIT_NOT_SPENDABLE"}
        return {"outcome": "spent", "apply_deadline": self.clock.now + timedelta(seconds=10)}

    async def settle(self, context: Any, op_id: Any, fence: int, permit_id: str,
                     write_outcome: str, read: Any) -> dict[str, Any]:
        self.log.append(("settle", permit_id, write_outcome,
                         None if read is None else read.status))
        return {"outcome": self.settle_outcome}

    def calls(self, name: str) -> list[tuple[Any, ...]]:
        return [entry for entry in self.log if entry[0] == name]


class CountingEhr:
    """Wraps a SimulatedEhr and counts writes / closes (the real port does the work)."""

    def __init__(self, ehr: Any, *, write_hang: bool = False) -> None:
        self.ehr = ehr
        self.write_calls = 0
        self.closes: list[str] = []
        self.write_hang = write_hang

    async def read(self, appointment_id: str) -> Any:
        return await self.ehr.read(appointment_id)

    async def write_confirmation(self, appointment_id: str, **kw: Any) -> Any:
        self.write_calls += 1
        if self.write_hang:
            await asyncio.Event().wait()
        return await self.ehr.write_confirmation(appointment_id, **kw)

    async def close_permit(self, permit_id: str) -> None:
        self.closes.append(permit_id)
        await self.ehr.close_permit(permit_id)


def _worker(model: Model, ehr: Any, **kw: Any) -> Any:
    return _wb_mod().WritebackWorker(model, ehr, **kw)


# --- U-T: retry and escalation (owner C5) -----------------------------------------------------


def test_si_ut01_retry_delays_are_1_5_15_minutes_then_the_item() -> None:
    mod = _wb_mod()
    assert mod.RETRY_DELAYS_S == (60, 300, 900)
    assert [mod.retry_delay_s(n) for n in (1, 2, 3, 4)] == [60, 300, 900, None]
    with pytest.raises(ValueError):
        mod.retry_delay_s(0)


@pytest.mark.parametrize(("interval_s", "expected_s"), [(60, 1540), (300, 2500), (450, 3100)])
def test_si_ut02_conditional_worst_case_target_by_simulation(interval_s: int,
                                                             expected_s: int) -> None:
    """The C4 formula, then a tick simulation: with the reply recorded just after a tick
    and every retry becoming due just after a tick (the worst phase), the item appears no
    later than the formula; 300 s is about 42 minutes. A conditional target only: the
    simulation assumes every tick runs and each read fails at its timeout."""

    mod = _wb_mod()
    assert mod.worst_case_escalation_s(interval_s) == expected_s
    assert mod.worst_case_escalation_s(300) / 60 == pytest.approx(41.7, abs=0.1)

    async def body() -> None:
        clock = Clock()
        reply_at = clock.now
        model = Model(clock)
        ehr = _ehr(clock)
        ehr.fail_reads(10)
        worker = _worker(model, ehr)
        tick = 1  # the reply is recorded just after tick 0: the first chance is tick 1
        while model.item_at is None and tick < 100:
            clock.now = reply_at + timedelta(seconds=tick * interval_s)
            if model.due():
                clock.advance(10)  # the read fails at its 10 s timeout
                await _bounded(worker.run_one(object(), uuid.uuid4()))
            tick += 1
        assert model.item_at is not None
        elapsed = (model.item_at - reply_at).total_seconds()
        assert elapsed <= expected_s
        assert len(model.calls("decide")) == 4
        assert all(entry[1] is True for entry in model.calls("decide"))
        assert model.calls("spend") == []

    asyncio.run(body())


@pytest.mark.parametrize("reason", ["status_changed", "declined_or_unknown",
                                    "could_not_verify", "no_baseline"])
def test_si_ut03_other_reasons_raise_the_item_at_once(reason: str) -> None:
    async def body() -> None:
        clock = Clock()
        model = Model(clock, decide_outcome={"outcome": "item", "reason": reason})
        ehr = CountingEhr(_ehr(clock, "negative"))
        assert await _bounded(_worker(model, ehr).run_one(object(), uuid.uuid4())) \
            == "item_raised"
        assert model.calls("decide") == [("decide", False, "negative")]
        assert model.calls("spend") == [] and ehr.write_calls == 0

    asyncio.run(body())


def test_si_ut04_a_read_that_succeeds_on_retry_takes_the_normal_path() -> None:
    async def body() -> None:
        clock = Clock()
        model = Model(clock)
        raw = _ehr(clock)
        raw.fail_reads(2)
        ehr = CountingEhr(raw)
        worker = _worker(model, ehr)
        outcomes = []
        for _ in range(3):
            clock.advance(1000)
            outcomes.append(await _bounded(worker.run_one(object(), uuid.uuid4())))
        assert outcomes == ["deferred", "deferred", "observed_positive"]
        assert model.item_at is None and ehr.write_calls == 1

    asyncio.run(body())


# --- U-S / U-K / U-Q: one write per spent permit ----------------------------------------------


def test_si_us06_exactly_one_write_per_spent_permit_then_verify() -> None:
    async def body() -> None:
        clock = Clock()
        model = Model(clock)
        ehr = CountingEhr(_ehr(clock))
        assert await _bounded(_worker(model, ehr).run_one(object(), uuid.uuid4())) \
            == "observed_positive"
        assert ehr.write_calls == 1
        assert model.calls("settle") == [("settle", "permit-1", "applied", "positive")]

    asyncio.run(body())


@pytest.mark.parametrize(("mode", "expected"), [("refused", "spend_refused"),
                                                ("unknown", "spend_unknown")])
def test_si_us07_uk03_no_write_without_a_committed_spend(mode: str, expected: str) -> None:
    async def body() -> None:
        clock = Clock()
        model = Model(clock, spend_mode=mode)
        ehr = CountingEhr(_ehr(clock))
        assert await _bounded(_worker(model, ehr).run_one(object(), uuid.uuid4())) == expected
        assert ehr.write_calls == 0 and model.calls("settle") == []

    asyncio.run(body())


def test_si_uk04_write_timeout_is_attempted_and_never_retried() -> None:
    async def body() -> None:
        clock = Clock()
        model = Model(clock, settle_outcome="item")
        ehr = CountingEhr(_ehr(clock), write_hang=True)
        result = await _bounded(_worker(model, ehr, write_timeout_s=0.05)
                                .run_one(object(), uuid.uuid4()))
        assert result == "item_raised"
        assert ehr.write_calls == 1
        assert model.calls("settle") == [("settle", "permit-1", "unknown", "neutral")]

    asyncio.run(body())


def test_si_uq01_reconcile_closes_the_permit_before_reading_and_never_writes() -> None:
    async def body() -> None:
        clock = Clock()
        model = Model(clock, phase="reconcile", settle_outcome="item")
        ehr = CountingEhr(_ehr(clock))
        assert await _bounded(_worker(model, ehr).run_one(object(), uuid.uuid4())) \
            == "item_raised"
        assert ehr.closes == ["permit-old"] and ehr.write_calls == 0
        assert model.calls("decide") == [] and model.calls("spend") == []
        assert model.calls("settle") == [("settle", "permit-old", "unknown", "neutral")]

    asyncio.run(body())


def test_si_uq02_cancellation_propagates_from_the_worker() -> None:
    async def body() -> None:
        clock = Clock()
        model = Model(clock)
        ehr = CountingEhr(_ehr(clock), write_hang=True)
        task = asyncio.create_task(_worker(model, ehr, write_timeout_s=60)
                                   .run_one(object(), uuid.uuid4()))
        for _ in range(50):
            await asyncio.sleep(0)
            if ehr.write_calls:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _bounded(task)

    asyncio.run(body())


# --- U-V: privacy -----------------------------------------------------------------------------


def test_si_uv01_no_sensitive_value_or_exception_text_is_logged(
        caplog: pytest.LogCaptureFixture) -> None:
    async def body() -> None:
        clock = Clock()

        class Leaky(Model):
            async def settle(self, *args: Any) -> dict[str, Any]:
                raise RuntimeError(f"{PHONE} {NAME} {PROVIDER} {DEPARTMENT} {APPT}")

        model = Leaky(clock)
        with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(RuntimeError):
            await _bounded(_worker(model, _ehr(clock)).run_one(object(), uuid.uuid4()))
        raw = _ehr(clock)
        raw.fail_reads(1)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await _bounded(_worker(Model(clock), raw).run_one(object(), uuid.uuid4()))
        text = " ".join(r.getMessage() for r in caplog.records)
        for secret in (PHONE, NAME, PROVIDER, DEPARTMENT, APPT):
            assert secret not in text

    asyncio.run(body())


# --- U-N: not composed ------------------------------------------------------------------------


def test_si_un01_writeback_and_simulated_ehr_are_not_composed() -> None:
    _wb_mod(), _ehr_mod()  # the modules exist (red until they do)
    forbidden = ("haloflow.m05.writeback", "haloflow.m05.simulated_ehr")
    paths = [SRC / "composition.py", SRC / "startup.py", *sorted((SRC / "platform").rglob("*.py"))]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"
