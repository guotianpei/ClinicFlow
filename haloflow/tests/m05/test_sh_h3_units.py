"""L-1S package S-H checkpoint H3 — ReminderScheduler unit rows (no database, fake clock).

RED BY DESIGN until `haloflow.m05.reminder_scheduler` exists. Every H3 import is deferred
into the row body, so the expected red cause is ModuleNotFoundError for that module.

Governing: H3 package v1 + v1r1 + v1r2 deltas and interface pins v1 (Work Session
2026-11-09; Codex ALIGN for red-test preparation 20261109T134153Z). Pinned API
(`haloflow.m05.reminder_scheduler`):
- constants DEFAULT_INTERVAL_S=300, INTERVAL_MIN_S=60, INTERVAL_MAX_S=450,
  STOP_TIMEOUT_MAX_S=60, TENANTS_MAX=1000, DEFAULT_RECOVERY_LIMIT=100,
  RECOVERY_LIMIT_MAX=1000.
- `ReminderSchedulerService(ReminderService)` adds
  `async accepted_pending(context, limit, after)` -> `{"schema_version": 1,
  "ops": [{"op_id", "updated_at"}, ...]}`; `after` is None or `(updated_at, op_id)`.
- `ReminderScheduler(*, contexts, service, worker, runner, tenants, interval_s=300,
  stop_timeout_s=10, recovery_limit=100, clock=time.monotonic, sleep=asyncio.sleep)`;
  `.state` -> `haloflow.m05.due_scheduler.SchedulerState`; `start()`; `async tick()` ->
  `TickResult`; `async stop()`.
- `ReminderSchedulerRefused(code)`; `ReminderSchedulerStopFailed` (code STOP_TIMEOUT).
- `PhaseReport(status, counts)`, `TenantReport(tenant, code, context, expire, recovery,
  ready)`, `TickResult(tick, duration_ms, codes, tenants, cancelled)`.
- One summary line per tick on logger `haloflow.m05.reminder_scheduler`.

Rows: U01 inputs; U02 lifecycle; U03 fixed rate; U04 overrun; U05 phase order; U06 batch
untouched; U07 phase isolation; U07b typed failure classes per phase; U08 privacy;
U09-U12 stop; U13-U14 static; U15-U17 stop during recovery / ready / context;
U18-U19 manual-tick ownership; U20 item mapping; U21 cursor; U22 report truth;
U23 invocation ceiling; U24 summary line.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import math
import pathlib
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import se_support as se

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"
LOGGER = "haloflow.m05.reminder_scheduler"
T_A, T_B, T_C = se.TENANT_A, se.TENANT_B, se.TENANT_C
PHONE, NAME = "+15550001234", "Synthia"
RECOVERY_KEYS = ("registered", "existing", "held", "refused_activation",
                 "refused_capability", "refused_not_accepted", "refused_other",
                 "commit_unknown", "failed")
BOUND_S = 5.0


def _mod() -> Any:
    import haloflow.m05.reminder_scheduler as module

    return module


def _state() -> Any:
    from haloflow.m05.due_scheduler import SchedulerState

    return SchedulerState


# --- doubles ----------------------------------------------------------------------------------


Script = Callable[..., Awaitable[Any]]


class World:
    """Scripted doubles sharing one ordered call log. Each phase is an async callable the
    row may replace; the defaults model an empty, healthy tenant."""

    def __init__(self, clock: se.FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[Any, ...]] = []
        self.contexts_fn: Script = self._context
        self.expire_fn: Script = self._expire
        self.pending_fn: Script = self._pending
        self.recover_fn: Script = self._recover
        self.run_fn: Script = self._run
        self.rows: dict[str, list[dict[str, str]]] = {}
        self.durations: dict[str, float] = {}

    async def _context(self, tenant: str) -> Any:
        return f"ctx:{tenant}"

    async def _expire(self, ctx: Any, limit: int) -> dict[str, Any]:
        return {"schema_version": 1, "expired": 0}

    async def _pending(self, ctx: Any, limit: int, after: Any) -> dict[str, Any]:
        rows = self.rows.get(str(ctx).removeprefix("ctx:"), [])
        if after is not None:
            rows = [r for r in rows if (r["updated_at"], r["op_id"]) > tuple(after)]
        return {"schema_version": 1, "ops": rows[:limit]}

    async def _recover(self, ctx: Any, op_id: Any) -> dict[str, Any]:
        return {"schema_version": 1, "outcome": "registered", "op_id": str(op_id)}

    async def _run(self, ctx: Any) -> Any:
        from haloflow.m05.reminder import RunReport

        tenant = str(ctx).removeprefix("ctx:")
        self.clock.now += self.durations.get(tenant, 0.0)
        await asyncio.sleep(0)
        return RunReport(examined=0, claimed=0, sent=0)

    # -- the four injected collaborators -------------------------------------------------

    def contexts(self) -> Callable[[str], Awaitable[Any]]:
        async def contexts(tenant: str) -> Any:
            self.calls.append(("context", tenant, self.clock.now))
            return await self.contexts_fn(tenant)

        return contexts

    def service(self) -> Any:
        from haloflow.m05.reminder_scheduler import ReminderSchedulerService

        world = self

        class Service(ReminderSchedulerService):
            def __init__(self) -> None:
                super().__init__(object())  # type: ignore[arg-type]

            async def expire(self, context: Any, limit: int = 100) -> dict[str, Any]:
                world.calls.append(("expire", context, limit))
                result: dict[str, Any] = await world.expire_fn(context, limit)
                return result

            async def accepted_pending(self, context: Any, limit: int,
                                       after: Any) -> dict[str, Any]:
                world.calls.append(("pending", context, limit, after))
                result: dict[str, Any] = await world.pending_fn(context, limit, after)
                return result

            def __getattribute__(self, name: str) -> Any:
                if name in ("examine", "consume", "claim", "claim_v2", "prepare",
                            "record_outcome", "register", "held", "ready"):
                    raise AssertionError(f"scheduler touched service.{name}")
                return super().__getattribute__(name)

        return Service()

    def worker(self) -> Any:
        from haloflow.m05.outbound import OutboundWorker

        world = self

        class Worker(OutboundWorker):
            def __init__(self) -> None:
                super().__init__(object(), object(), str)  # type: ignore[arg-type]

            async def recover(self, context: Any, op_id: Any) -> Any:
                world.calls.append(("recover", context, str(op_id)))
                return await world.recover_fn(context, op_id)

            async def run_one(self, *_a: Any, **_k: Any) -> str:
                raise AssertionError("scheduler called run_one")

            async def run_claimed(self, *_a: Any, **_k: Any) -> str:
                raise AssertionError("scheduler called run_claimed directly")

        return Worker()

    def runner(self) -> Any:
        from haloflow.m05.reminder import ReminderRunner

        world = self

        class Runner(ReminderRunner):
            def __init__(self) -> None:
                super().__init__(object(), object(), batch=7, lease_seconds=120)

            async def run_once(self, context: Any) -> Any:
                world.calls.append(("run_once", context, world.clock.now))
                return await world.run_fn(context)

        return Runner()

    def scheduler(self, tenants: tuple[str, ...] = (T_A,), **kwargs: Any) -> Any:
        kwargs.setdefault("interval_s", 300)
        kwargs.setdefault("stop_timeout_s", 1)
        kwargs.setdefault("recovery_limit", 3)
        return _mod().ReminderScheduler(
            contexts=self.contexts(), service=self.service(), worker=self.worker(),
            runner=self.runner(), tenants=tenants, clock=self.clock,
            sleep=self.clock.sleep, **kwargs)

    def kinds(self, tenant: str | None = None) -> list[str]:
        return [c[0] for c in self.calls
                if tenant is None or (len(c) > 1 and str(c[1]).endswith(tenant))]


def rows(n: int, start: int = 0) -> list[dict[str, str]]:
    return [{"op_id": f"00000000-0000-4000-8000-{i:012d}",
             "updated_at": f"2026-11-09T10:{i // 60:02d}:{i % 60:02d}.000000Z"}
            for i in range(start, start + n)]


def key(row: dict[str, str]) -> tuple[str, str]:
    return (row["updated_at"], row["op_id"])


def zero_recovery(**overrides: int) -> dict[str, int]:
    counts = dict.fromkeys(RECOVERY_KEYS, 0)
    counts.update(overrides)
    return counts


async def _until(predicate: Callable[[], bool], what: str, rounds: int = 2000) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"not reached: {what}")


async def _drain(s: Any, *futures: Any) -> None:
    """Bounded teardown (S-E pattern): never hangs a row."""

    stop = asyncio.ensure_future(s.stop())
    pending = {stop, *futures}
    for future in futures:
        if not future.done():
            future.cancel()
    done, still = await asyncio.wait(pending, timeout=BOUND_S)
    for future in still:
        future.cancel()
    for future in done:
        if not future.cancelled():
            future.exception()


def summaries(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.name == LOGGER]


# --- U01 inputs -------------------------------------------------------------------------------

BAD_INPUTS: dict[str, tuple[dict[str, Any], str]] = {
    "contexts-none": ({"contexts": None}, "CONTEXTS_INVALID"),
    "contexts-str": ({"contexts": "ctx"}, "CONTEXTS_INVALID"),
    "service-object": ({"service": object()}, "SERVICE_INVALID"),
    "worker-object": ({"worker": object()}, "WORKER_INVALID"),
    "runner-object": ({"runner": object()}, "RUNNER_INVALID"),
    "tenants-list": ({"tenants": [T_A]}, "TENANTS_INVALID"),
    "tenants-empty": ({"tenants": ()}, "TENANTS_INVALID"),
    "tenants-dup": ({"tenants": (T_A, T_A)}, "TENANTS_INVALID"),
    "tenants-pattern": ({"tenants": ("Bad_Tenant",)}, "TENANTS_INVALID"),
    "tenants-1001": ({"tenants": tuple(f"t-{i:04d}" for i in range(1001))},
                     "TENANTS_INVALID"),
    "interval-59": ({"interval_s": 59}, "INTERVAL_INVALID"),
    "interval-451": ({"interval_s": 451}, "INTERVAL_INVALID"),
    "interval-float": ({"interval_s": 300.0}, "INTERVAL_INVALID"),
    "interval-bool": ({"interval_s": True}, "INTERVAL_INVALID"),
    "timeout-0": ({"stop_timeout_s": 0}, "TIMEOUT_INVALID"),
    "timeout-61": ({"stop_timeout_s": 61}, "TIMEOUT_INVALID"),
    "timeout-nan": ({"stop_timeout_s": math.nan}, "TIMEOUT_INVALID"),
    "timeout-bool": ({"stop_timeout_s": True}, "TIMEOUT_INVALID"),
    "limit-0": ({"recovery_limit": 0}, "RECOVERY_LIMIT_INVALID"),
    "limit-1001": ({"recovery_limit": 1001}, "RECOVERY_LIMIT_INVALID"),
    "limit-bool": ({"recovery_limit": True}, "RECOVERY_LIMIT_INVALID"),
    "limit-float": ({"recovery_limit": 3.0}, "RECOVERY_LIMIT_INVALID"),
}


@pytest.mark.parametrize("case", sorted(BAD_INPUTS))
def test_h3_u01_inputs_refused_before_any_call(case: str) -> None:
    module = _mod()
    world = World(se.FakeClock())
    overrides, code = BAD_INPUTS[case]
    kwargs: dict[str, Any] = {
        "contexts": world.contexts(), "service": world.service(), "worker": world.worker(),
        "runner": world.runner(), "tenants": (T_A,), "clock": world.clock,
        "sleep": world.clock.sleep}
    kwargs.update(overrides)
    with pytest.raises(module.ReminderSchedulerRefused) as caught:
        module.ReminderScheduler(**kwargs)
    assert caught.value.code == code
    assert world.calls == []


def test_h3_u01b_bounds_and_defaults() -> None:
    module = _mod()
    world = World(se.FakeClock())
    assert (module.DEFAULT_INTERVAL_S, module.INTERVAL_MIN_S, module.INTERVAL_MAX_S,
            module.STOP_TIMEOUT_MAX_S, module.TENANTS_MAX, module.DEFAULT_RECOVERY_LIMIT,
            module.RECOVERY_LIMIT_MAX) == (300, 60, 450, 60, 1000, 100, 1000)
    for interval in (60, 450):
        for limit in (1, 1000):
            s = world.scheduler(interval_s=interval, stop_timeout_s=60,
                                recovery_limit=limit)
            assert s.state is _state().NEW
    assert world.calls == []


# --- U02 lifecycle ----------------------------------------------------------------------------


async def test_h3_u02_lifecycle_matrix() -> None:
    module = _mod()
    world = World(se.FakeClock(1000.0))
    s = world.scheduler()
    assert s.state is _state().NEW
    s.start()
    assert s.state is _state().RUNNING
    with pytest.raises(module.ReminderSchedulerRefused) as owned:
        await s.tick()
    assert owned.value.code == "LOOP_OWNS_TICKS"
    with pytest.raises(module.ReminderSchedulerRefused) as again:
        s.start()
    assert again.value.code == "NOT_STARTABLE"
    await asyncio.wait_for(s.stop(), BOUND_S)
    assert s.state is _state().STOPPED
    await asyncio.wait_for(s.stop(), BOUND_S)  # idempotent
    with pytest.raises(module.ReminderSchedulerRefused) as stopped:
        await s.tick()
    assert stopped.value.code == "SCHEDULER_STOPPED"
    with pytest.raises(module.ReminderSchedulerRefused) as restart:
        s.start()
    assert restart.value.code == "NOT_STARTABLE"


# --- U03 / U04 timing (origin: first tick at start time t0) ------------------------------------


async def test_h3_u03_fixed_rate_from_t0() -> None:
    clock = se.FakeClock(1000.0)
    world = World(clock)
    world.durations = {T_A: 10.0}
    s = world.scheduler(interval_s=300)
    s.start()
    try:
        await _until(lambda: world.kinds().count("run_once") >= 3, "three ticks")
    finally:
        await _drain(s)
    starts = [c[2] for c in world.calls if c[0] == "context"][:3]
    assert starts == [1000.0, 1300.0, 1600.0]


async def test_h3_u04_overrun_skips_strictly_no_burst(caplog: pytest.LogCaptureFixture
                                                      ) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = se.FakeClock(1000.0)
    world = World(clock)
    lengths = iter([0.0, 750.0, 0.0, 0.0])  # tick 2 (start t0+I) lasts 2.5 I

    async def run(ctx: Any) -> Any:
        from haloflow.m05.reminder import RunReport

        clock.now += next(lengths, 0.0)
        await asyncio.sleep(0)
        return RunReport(examined=0, claimed=0, sent=0)

    world.run_fn = run
    s = world.scheduler(interval_s=300)
    s.start()
    try:
        await _until(lambda: world.kinds().count("run_once") >= 3, "three ticks")
    finally:
        await _drain(s)
    starts = [c[2] for c in world.calls if c[0] == "context"][:3]
    assert starts == [1000.0, 1300.0, 2200.0]  # t0, t0+I, t0+4I
    lines = summaries(caplog.records)
    assert " skipped=0 " in lines[0] + " "
    assert " skipped=2 " in lines[1] + " "


# --- U05 / U06 phase order ---------------------------------------------------------------------


async def test_h3_u05_phase_order_and_limits() -> None:
    world = World(se.FakeClock())
    world.rows = {T_A: rows(2), T_B: []}
    s = world.scheduler((T_A, T_B), recovery_limit=3)
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    a = rows(2)
    assert world.calls[:6] == [
        ("context", T_A, 0.0), ("expire", f"ctx:{T_A}", 3),
        ("pending", f"ctx:{T_A}", 3, None), ("recover", f"ctx:{T_A}", a[0]["op_id"]),
        ("recover", f"ctx:{T_A}", a[1]["op_id"]), ("run_once", f"ctx:{T_A}", 0.0)]
    assert world.calls[6:] == [
        ("context", T_B, 0.0), ("expire", f"ctx:{T_B}", 3),
        ("pending", f"ctx:{T_B}", 3, None), ("run_once", f"ctx:{T_B}", 0.0)]
    assert result.codes == {"COMPLETE": 2}
    assert [t.tenant for t in result.tenants] == [T_A, T_B]
    await _drain(s)


async def test_h3_u06_full_recovery_page_leaves_runner_batch_untouched() -> None:
    world = World(se.FakeClock())
    world.rows = {T_A: rows(3)}
    s = world.scheduler(recovery_limit=3)
    await asyncio.wait_for(s.tick(), BOUND_S)
    assert world.kinds().count("recover") == 3
    # the ready phase is one unmodified run_once(context): the runner's own batch (7)
    # is the only ready bound; recovery never shrinks or replaces it
    assert [c[:2] for c in world.calls if c[0] == "run_once"] == [("run_once", f"ctx:{T_A}")]
    await _drain(s)


# --- U07 phase isolation -----------------------------------------------------------------------


def _raiser(exc: BaseException) -> Script:
    async def fn(*_a: Any) -> Any:
        raise exc

    return fn


ISOLATION = ("context", "expire", "selector", "recover_item", "run_once")


@pytest.mark.parametrize("phase", ISOLATION)
async def test_h3_u07_a_raising_phase_never_stops_other_phases_or_tenants(phase: str) -> None:
    world = World(se.FakeClock())
    world.rows = {T_A: rows(2), T_B: rows(1)}
    boom = RuntimeError("boom")
    target = {"context": "contexts_fn", "expire": "expire_fn", "selector": "pending_fn",
              "run_once": "run_fn"}
    if phase == "recover_item":
        first = rows(2)[0]["op_id"]

        async def recover(ctx: Any, op_id: Any) -> Any:
            if str(ctx).endswith(T_A) and str(op_id) == first:
                raise boom
            return {"schema_version": 1, "outcome": "registered", "op_id": str(op_id)}

        world.recover_fn = recover
    else:
        original = getattr(world, target[phase])

        async def scoped(*args: Any) -> Any:
            if str(args[0]).endswith(T_A):
                raise boom
            return await original(*args)

        setattr(world, target[phase], scoped)
    s = world.scheduler((T_A, T_B))
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    a, b = result.tenants
    assert b.code == "COMPLETE"
    assert world.kinds(T_B) == ["context", "expire", "pending", "recover", "run_once"]
    assert a.code == "DEGRADED"
    assert result.codes == {"COMPLETE": 1, "DEGRADED": 1}
    if phase == "context":
        assert (a.context, a.expire.status, a.recovery.status, a.ready.status) == (
            "INTERNAL", "NOT_RUN", "NOT_RUN", "NOT_RUN")
        assert world.kinds(T_A) == ["context"]  # nothing ran after a failed context
        return
    assert a.context == "ok"
    assert "run_once" in world.kinds(T_A)  # ready runs whatever happened before it
    expected = {
        "expire": ("INTERNAL", "ok", "ok"),
        "selector": ("ok", "INTERNAL", "ok"),
        "recover_item": ("ok", "DEGRADED_ITEMS", "ok"),
        "run_once": ("ok", "ok", "INTERNAL"),
    }[phase]
    assert (a.expire.status, a.recovery.status, a.ready.status) == expected
    if phase == "recover_item":
        assert a.recovery.counts == zero_recovery(registered=1, failed=1)
    await _drain(s)


def _typed_cases() -> dict[str, tuple[BaseException, str]]:
    from haloflow.m01.errors import ActivationRefused, CapabilityDenied, TenantDenied
    from haloflow.m05.outbound import OutboundCommitUnknown, OutboundRefused

    return {
        "activation": (ActivationRefused(), "ACTIVATION_REFUSED"),
        "capability-denied": (CapabilityDenied(), "CAPABILITY_REFUSED"),
        "tenant-denied": (TenantDenied(), "CAPABILITY_REFUSED"),
        "caps-invalid": (OutboundRefused("CAPABILITIES_INVALID"), "CAPABILITY_REFUSED"),
        "not-workload": (OutboundRefused("PRINCIPAL_NOT_WORKLOAD"), "CAPABILITY_REFUSED"),
        "refused-other": (OutboundRefused("COMMAND_MALFORMED"), "REFUSED"),
        "commit-unknown": (OutboundCommitUnknown(), "COMMIT_UNKNOWN"),
        "unexpected": (RuntimeError("x"), "INTERNAL"),
    }


TYPED = ("activation", "capability-denied", "tenant-denied", "caps-invalid",
         "not-workload", "refused-other", "commit-unknown", "unexpected")
TYPED_PHASES = {"context": "contexts_fn", "expire": "expire_fn", "selector": "pending_fn",
                "run_once": "run_fn"}


@pytest.mark.parametrize("case", TYPED)
@pytest.mark.parametrize("phase", sorted(TYPED_PHASES))
async def test_h3_u07b_typed_phase_failure_class_and_continuation(phase: str,
                                                                 case: str) -> None:
    exc, failure_class = _typed_cases()[case]
    world = World(se.FakeClock())
    world.rows = {T_A: rows(1), T_B: rows(1)}
    original = getattr(world, TYPED_PHASES[phase])

    async def scoped(*args: Any) -> Any:
        if str(args[0]).endswith(T_A):
            raise exc
        return await original(*args)

    setattr(world, TYPED_PHASES[phase], scoped)
    s = world.scheduler((T_A, T_B))
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    a, b = result.tenants
    statuses = {"context": a.context, "expire": a.expire.status,
                "selector": a.recovery.status, "run_once": a.ready.status}
    assert statuses[phase] == failure_class
    assert a.code == "DEGRADED"
    if phase == "context":
        assert (a.expire.status, a.recovery.status, a.ready.status) == ("NOT_RUN",) * 3
        assert world.kinds(T_A) == ["context"]
    else:
        assert a.context == "ok"
        others = {k: v for k, v in statuses.items() if k not in (phase, "context")}
        assert set(others.values()) == {"ok"}, others
        assert world.kinds(T_A)[-1] == "run_once"  # continuation reached the ready phase
    assert b.code == "COMPLETE"
    assert world.kinds(T_B) == ["context", "expire", "pending", "recover", "run_once"]
    await _drain(s)


# --- U08 privacy -------------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ISOLATION)
async def test_h3_u08_no_secret_text_anywhere(phase: str,
                                              caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    world = World(se.FakeClock())
    world.rows = {T_A: rows(1)}
    leak = ValueError(f"{PHONE} {NAME} Reply 1 to confirm secret=hunter2")
    target = {"context": "contexts_fn", "expire": "expire_fn", "selector": "pending_fn",
              "recover_item": "recover_fn", "run_once": "run_fn"}[phase]
    setattr(world, target, _raiser(leak))
    s = world.scheduler()
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    surfaces = [r.getMessage() for r in caplog.records]
    surfaces += [str(r.exc_info) for r in caplog.records if r.exc_info]
    surfaces += [repr(result), str(result), repr(s), repr(result.tenants)]
    for text in surfaces:
        for secret in (PHONE, NAME, "Reply 1", "hunter2"):
            assert secret not in text
    await _drain(s)


# --- U09-U12 stop ------------------------------------------------------------------------------


async def test_h3_u09_stop_before_any_tick_makes_no_call() -> None:
    world = World(se.FakeClock())
    s = world.scheduler()
    await asyncio.wait_for(s.stop(), BOUND_S)
    assert s.state is _state().STOPPED
    assert world.calls == []


def _phase_value(phase: str, args: tuple[Any, ...]) -> Any:
    """A phase-correct return value, so a swallowed cancellation that returns normally
    cannot hide a continuation error behind a wrong-typed result."""

    if phase == "context":
        return f"ctx:{args[0]}"
    if phase == "recovery":
        return {"schema_version": 1, "outcome": "registered", "op_id": str(args[1])}
    from haloflow.m05.reminder import RunReport

    return RunReport(examined=0, claimed=0, sent=0)


def _blocking(world: World, *, swallow: bool, phase: str = "ready"
              ) -> tuple[Script, asyncio.Event, list[str]]:
    release = asyncio.Event()
    seen: list[str] = []

    async def block(*args: Any) -> Any:
        seen.append("entered")
        while True:
            try:
                await release.wait()
                break
            except asyncio.CancelledError:
                seen.append("cancelled")
                if not swallow:
                    raise
        seen.append("returned")
        return _phase_value(phase, args)

    return block, release, seen


async def _settled_stop(s: Any) -> None:
    """After a hang is released, the owned task must finish: a further stop() completes
    within the bound and the scheduler reaches STOPPED (no abandoned task)."""

    await asyncio.wait_for(s.stop(), BOUND_S)
    assert s.state is _state().STOPPED


async def test_h3_u10_stop_during_blocked_ready_phase_cooperative() -> None:
    world = World(se.FakeClock())
    world.run_fn, release, seen = _blocking(world, swallow=False)
    s = world.scheduler((T_A, T_B))
    s.start()
    try:
        await _until(lambda: seen == ["entered"], "ready phase blocked")
        await asyncio.wait_for(s.stop(), BOUND_S)
    finally:
        release.set()
    assert s.state is _state().STOPPED
    assert seen == ["entered", "cancelled"]  # cancellation sent exactly once
    before = list(world.calls)
    await asyncio.sleep(0.05)
    assert world.calls == before  # no work after an acknowledged stop
    assert T_B not in [c[1] for c in world.calls if c[0] == "context"]


async def test_h3_u11_swallowed_cancellation_is_not_a_success(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    module = _mod()
    world = World(se.FakeClock())
    world.run_fn, release, seen = _blocking(world, swallow=True)
    s = world.scheduler((T_A, T_B))
    tick = asyncio.ensure_future(s.tick())
    try:
        await _until(lambda: seen == ["entered"], "ready phase blocked")
        stopper = asyncio.ensure_future(s.stop())
        await _until(lambda: "cancelled" in seen, "cancel delivered")
        release.set()  # the double swallows the cancel and returns normally
        with pytest.raises(module.ReminderSchedulerRefused) as caught:
            await asyncio.wait_for(tick, BOUND_S)
        assert caught.value.code == "STOPPED_DURING_TICK"
        await asyncio.wait_for(stopper, BOUND_S)
    finally:
        release.set()
        await _drain(s, tick)
    assert T_B not in [c[1] for c in world.calls if c[0] == "context"]
    lines = summaries(caplog.records)
    assert len(lines) == 1 and " cancelled=True " in lines[0] + " "


async def test_h3_u12_hang_that_ignores_cancel_is_stop_failed_and_owns_the_task() -> None:
    module = _mod()
    world = World(se.FakeClock())
    world.run_fn, release, seen = _blocking(world, swallow=True)
    s = world.scheduler(stop_timeout_s=0.2)
    s.start()
    try:
        await _until(lambda: seen == ["entered"], "ready phase blocked")
        with pytest.raises(module.ReminderSchedulerStopFailed) as caught:
            await asyncio.wait_for(s.stop(), BOUND_S)
        assert caught.value.code == "STOP_TIMEOUT"
        assert s.state is _state().STOP_FAILED
        assert seen.count("cancelled") == 1
        with pytest.raises(module.ReminderSchedulerRefused) as restart:
            s.start()
        assert restart.value.code == "NOT_STARTABLE"
        with pytest.raises(module.ReminderSchedulerRefused) as manual:
            await s.tick()
        assert manual.value.code == "SCHEDULER_STOPPED"
    finally:
        release.set()  # fixture cleanup releases the hang (pin)
    before = list(world.calls)
    await _settled_stop(s)
    assert world.calls == before  # the released task did no further work
    assert seen == ["entered", "cancelled", "returned"]


# --- U13 / U14 static ---------------------------------------------------------------------------


def _source() -> str:
    path = SRC / "m05" / "reminder_scheduler.py"
    assert path.exists(), "haloflow/m05/reminder_scheduler.py is absent"
    return path.read_text(encoding="utf-8")


def test_h3_u13_no_send_path_except_runner_and_recover() -> None:
    tree = ast.parse(_source())
    forbidden = {"send", "consume", "claim", "claim_v2", "run_one", "run_claimed",
                 "examine", "prepare", "register", "record_outcome", "held"}
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (used & forbidden), sorted(used & forbidden)
    assert {"recover", "run_once", "expire", "accepted_pending"} <= used


def test_h3_u14_not_composed() -> None:
    for name in ("platform", "startup.py", "composition.py"):
        path = SRC / name
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            text = file.read_text(encoding="utf-8")
            assert "reminder_scheduler" not in text, file


# --- U15-U17 stop during recovery / ready / context ------------------------------------------


STOP_PHASES = {"recovery": "recover_fn", "ready": "run_fn", "context": "contexts_fn"}
LATER_WORK = {  # side effects that must never follow the blocked phase once stop began
    "context": ["expire", "pending", "recover", "run_once"],
    "recovery": ["run_once"],
    "ready": [],
}


@pytest.mark.parametrize("swallow", [False, True], ids=["cooperative", "swallows"])
@pytest.mark.parametrize("phase", sorted(STOP_PHASES))
async def test_h3_u15_u17_stop_during_phase(phase: str, swallow: bool) -> None:
    module = _mod()
    world = World(se.FakeClock())
    world.rows = {T_A: rows(2)}
    block, release, seen = _blocking(world, swallow=swallow, phase=phase)
    setattr(world, STOP_PHASES[phase], block)
    s = world.scheduler((T_A, T_B), stop_timeout_s=0.2)
    s.start()
    try:
        await _until(lambda: seen[:1] == ["entered"], f"{phase} blocked")
        if swallow:
            with pytest.raises(module.ReminderSchedulerStopFailed):
                await asyncio.wait_for(s.stop(), BOUND_S)
            assert s.state is _state().STOP_FAILED
        else:
            await asyncio.wait_for(s.stop(), BOUND_S)
            assert s.state is _state().STOPPED
        assert seen.count("cancelled") == 1
    finally:
        release.set()
    if swallow:
        await _settled_stop(s)  # the owned task completes deterministically
    # After the stop request, nothing further ran: not the blocked phase again, not the
    # second recovery item, not a later phase, not another tenant (cancellation is
    # re-checked before every side-effecting phase and item).
    assert seen.count("entered") == 1
    expected_recover = {"context": [], "recovery": [rows(2)[0]["op_id"]],
                        "ready": [r["op_id"] for r in rows(2)]}[phase]
    assert [c[2] for c in world.calls if c[0] == "recover"] == expected_recover
    for kind in LATER_WORK[phase]:
        assert kind not in world.kinds(T_A), (phase, kind)
    assert T_B not in [c[1] for c in world.calls if c[0] == "context"]


# --- U18 / U19 manual tick ownership ---------------------------------------------------------


async def test_h3_u18_u19_manual_tick_owns_the_scheduler() -> None:
    module = _mod()
    world = World(se.FakeClock())
    world.run_fn, release, seen = _blocking(world, swallow=False)
    s = world.scheduler()
    tick = asyncio.ensure_future(s.tick())
    try:
        await _until(lambda: seen == ["entered"], "manual tick blocked")
        with pytest.raises(module.ReminderSchedulerRefused) as second:
            await s.tick()
        assert second.value.code == "TICK_IN_PROGRESS"
        with pytest.raises(module.ReminderSchedulerRefused) as started:
            s.start()
        assert started.value.code == "TICK_IN_PROGRESS"
        release.set()
        result = await asyncio.wait_for(tick, BOUND_S)
        assert result.codes == {"COMPLETE": 1}
    finally:
        release.set()
        await _drain(s, tick)


# --- U20 item outcome mapping --------------------------------------------------------------------


def _item_cases() -> dict[str, tuple[Callable[[], Any], str]]:
    from haloflow.m01.errors import ActivationRefused, CapabilityDenied, TenantDenied
    from haloflow.m05.outbound import OutboundCommitUnknown, OutboundRefused

    def ret(outcome: str) -> Callable[[], Any]:
        return lambda: {"schema_version": 1, "outcome": outcome}

    def exc(e: BaseException) -> Callable[[], Any]:
        def fn() -> Any:
            raise e

        return fn

    return {
        "registered": (ret("registered"), "registered"),
        "existing": (ret("existing"), "existing"),
        "held": (ret("held"), "held"),
        "activation": (exc(ActivationRefused()), "refused_activation"),
        "principal": (exc(OutboundRefused("PRINCIPAL_NOT_WORKLOAD")), "refused_capability"),
        "capabilities": (exc(OutboundRefused("CAPABILITIES_INVALID")), "refused_capability"),
        "capability-denied": (exc(CapabilityDenied()), "refused_capability"),
        "tenant-denied": (exc(TenantDenied()), "refused_capability"),
        "not-accepted": (exc(OutboundRefused("NOT_ACCEPTED")), "refused_not_accepted"),
        "other-refusal": (exc(OutboundRefused("COMMAND_MALFORMED")), "refused_other"),
        "commit-unknown": (exc(OutboundCommitUnknown()), "commit_unknown"),
        "unexpected": (exc(KeyError("x")), "failed"),
        "unknown-outcome": (ret("surprise"), "failed"),
    }


ITEM_CASES = ("registered", "existing", "held", "activation", "principal", "capabilities",
              "capability-denied", "tenant-denied", "not-accepted", "other-refusal",
              "commit-unknown", "unexpected", "unknown-outcome")


@pytest.mark.parametrize("case", ITEM_CASES)
async def test_h3_u20_item_outcome_mapping(case: str) -> None:
    make, counted = _item_cases()[case]
    world = World(se.FakeClock())
    world.rows = {T_A: rows(1)}

    async def recover(_ctx: Any, _op: Any) -> Any:
        return make()

    world.recover_fn = recover
    s = world.scheduler()
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    (report,) = result.tenants
    assert report.recovery.counts == zero_recovery(**{counted: 1})
    benign = counted in ("registered", "existing", "held")
    assert report.recovery.status == ("ok" if benign else "DEGRADED_ITEMS")
    assert report.code == ("COMPLETE" if benign else "DEGRADED")
    assert report.ready.status == "ok"
    await _drain(s)


# --- U21 cursor ------------------------------------------------------------------------------


def _afters(world: World) -> list[Any]:
    return [c[3] for c in world.calls if c[0] == "pending"]


def _recovered(world: World) -> list[str]:
    return [c[2] for c in world.calls if c[0] == "recover"]


async def _ticks(s: Any, n: int) -> None:
    for _ in range(n):
        await asyncio.wait_for(s.tick(), BOUND_S)


async def test_h3_u21a_short_page_resets() -> None:
    world = World(se.FakeClock())
    world.rows = {T_A: rows(2)}
    s = world.scheduler(recovery_limit=5)
    await _ticks(s, 2)
    assert _afters(world) == [None, None]
    await _drain(s)


async def test_h3_u21b_full_page_then_wrap_in_the_next_tick() -> None:
    from haloflow.m01.errors import ActivationRefused

    world = World(se.FakeClock())
    page = rows(3)
    world.rows = {T_A: page}
    world.recover_fn = _raiser(ActivationRefused())  # clinic off: every item refused
    s = world.scheduler(recovery_limit=3)
    await _ticks(s, 1)
    world.recover_fn = World(world.clock)._recover  # clinic on
    await _ticks(s, 1)
    assert _afters(world) == [None, key(page[2]), None]  # empty page -> one wrap
    assert _recovered(world) == [r["op_id"] for r in page] * 2
    await _drain(s)


async def test_h3_u21c_empty_without_cursor_does_not_wrap() -> None:
    world = World(se.FakeClock())
    s = world.scheduler(recovery_limit=3)
    await _ticks(s, 1)
    assert _afters(world) == [None]
    assert _recovered(world) == []
    await _drain(s)


async def test_h3_u21d_backlog_wraps_after_a_short_page() -> None:
    world = World(se.FakeClock())
    page = rows(7)
    world.rows = {T_A: page}
    world.recover_fn = _raiser(RuntimeError("keep"))  # rows persist
    s = world.scheduler(recovery_limit=3)
    await _ticks(s, 4)
    assert _afters(world) == [None, key(page[2]), key(page[5]), None]
    assert _recovered(world) == [r["op_id"] for r in page] + [r["op_id"] for r in page[:3]]
    await _drain(s)


async def test_h3_u21e_failed_prefix_does_not_pin_the_cursor() -> None:
    world = World(se.FakeClock())
    page = rows(4)
    world.rows = {T_A: page}
    bad = {r["op_id"] for r in page[:3]}

    async def recover(ctx: Any, op_id: Any) -> Any:
        if str(op_id) in bad:
            raise RuntimeError("persistent")
        return {"schema_version": 1, "outcome": "registered"}

    world.recover_fn = recover
    s = world.scheduler(recovery_limit=3)
    await _ticks(s, 1)
    second = await asyncio.wait_for(s.tick(), BOUND_S)
    assert _recovered(world)[3:] == [page[3]["op_id"]]
    assert second.tenants[0].recovery.counts == zero_recovery(registered=1)
    await _drain(s)


async def test_h3_u21f_restart_starts_from_none() -> None:
    world = World(se.FakeClock())
    world.rows = {T_A: rows(3)}
    first = world.scheduler(recovery_limit=3)
    await _ticks(first, 1)
    await _drain(first)
    second = world.scheduler(recovery_limit=3)
    await _ticks(second, 1)
    assert _afters(world) == [None, None]
    await _drain(second)


# --- U22 report truth -----------------------------------------------------------------------


async def test_h3_u22a_complete_means_pass_completed_not_sent() -> None:
    world = World(se.FakeClock())
    s = world.scheduler()
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    (report,) = result.tenants
    assert report.code == "COMPLETE"
    assert report.ready.counts == {"examined": 0, "claimed": 0, "sent": 0}
    assert report.expire.counts == {"expired": 0}
    await _drain(s)


async def test_h3_u22b_all_items_refused_is_degraded() -> None:
    from haloflow.m01.errors import ActivationRefused

    world = World(se.FakeClock())
    world.rows = {T_A: rows(3)}
    world.recover_fn = _raiser(ActivationRefused())
    s = world.scheduler(recovery_limit=3)
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    (report,) = result.tenants
    assert (report.code, report.recovery.status) == ("DEGRADED", "DEGRADED_ITEMS")
    assert report.recovery.counts == zero_recovery(refused_activation=3)
    assert result.codes == {"DEGRADED": 1}
    await _drain(s)


# --- U23 invocation ceiling ------------------------------------------------------------------


async def test_h3_u23_invocation_ceiling() -> None:
    from haloflow.m05.outbound import OutboundCommitUnknown

    world = World(se.FakeClock())
    page = rows(3)
    world.rows = {T_A: page}
    s = world.scheduler(recovery_limit=3)
    await _ticks(s, 1)  # full page -> cursor at page[2]
    world.calls.clear()
    registers: list[str] = []

    async def recover(ctx: Any, op_id: Any) -> Any:
        # models OutboundWorker.recover: register, one retry on commit-unknown
        registers.extend([str(op_id), str(op_id)])
        raise OutboundCommitUnknown()

    world.recover_fn = recover
    await _ticks(s, 1)
    assert world.kinds() == ["context", "expire", "pending", "pending", "recover",
                             "recover", "recover", "run_once"]
    assert len(registers) == 6  # 1 expire + 2 selector + 2*3 register + 1 run_once
    await _drain(s)


# --- U24 summary line ------------------------------------------------------------------------


async def test_h3_u24_one_summary_line_per_tick(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    world = World(se.FakeClock())
    world.rows = {T_A: rows(1)}
    s = world.scheduler((T_A, T_B))
    await asyncio.wait_for(s.tick(), BOUND_S)
    lines = summaries(caplog.records)
    assert len(lines) == 1
    line = lines[0]
    for part in ("reminder_scheduler tick=0 ", "skipped=0 ", "cancelled=False ",
                 "codes=COMPLETE:2", "registered=1 ", "examined=0 ", "sent=0"):
        assert part in line + " ", (part, line)
    with contextlib.suppress(Exception):
        await _drain(s)
