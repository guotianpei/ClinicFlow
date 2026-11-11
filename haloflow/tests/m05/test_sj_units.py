"""L-1S package S-J: WritebackScheduler unit rows (no database, fake clock).

RED BY DESIGN until `haloflow.m05.writeback_scheduler` exists. Every S-J import is deferred
into the row body, so the expected red cause is ModuleNotFoundError for that module. The
U-J00 rows are DEPENDENCY CONTROLS on accepted-candidate S-I / H3 code; they must pass now.
S-I is NOT accepted (final CI 42869646639 pending); these rows pin its interface, so an S-I
correction that changes it fails here first and pauses the affected S-J part.

Governing: next-package assessment v1 + v1r1 + v1r2, S-J test-case preparation v1 and the
Codex review pins record (Work Session 2026-11-10; Codex 20261110T202552Z,
20261110T210601Z). Proposed pinned API (`haloflow.m05.writeback_scheduler`), mirroring the
H3 ReminderScheduler precedent:
- constants DEFAULT_INTERVAL_S=60, INTERVAL_MIN_S=30, INTERVAL_MAX_S=300,
  STOP_TIMEOUT_MAX_S=60, TENANTS_MAX=1000, DEFAULT_BATCH_LIMIT=100, BATCH_LIMIT_MAX=1000.
- `WritebackScheduler(*, contexts, service, worker, tenants, interval_s=60,
  stop_timeout_s=10, batch_limit=100, clock=time.monotonic, sleep=asyncio.sleep)`;
  `service` is a `WritebackService`, `worker` a `WritebackWorker`; `.state` ->
  `haloflow.m05.due_scheduler.SchedulerState`; `start()`; `async tick()` -> `TickResult`;
  `async stop()`.
- `WritebackSchedulerRefused(code)`; `WritebackSchedulerStopFailed` (code STOP_TIMEOUT).
- `PhaseReport(status, counts)`, `TenantReport(tenant, code, context, discover, run)`,
  `TickResult(tick, duration_ms, codes, tenants, cancelled)`.
- Per tenant per tick (context granularity 1+N, Codex pin 1): ONE fresh pass context ->
  `service.discover(ctx, batch_limit)` -> `service.due(ctx, batch_limit)`; then for each due
  row (at most `batch_limit`), ONE fresh context -> `worker.run_one(ctx_i, wb_op_id)`. The
  run phase runs whatever happened in discover. A pass-context failure leaves both phases
  NOT_RUN; a per-op context failure is counted `context_failed` and the next op continues.
  Cancellation is never caught.
- Counts: discover {"discovered": n}; run RUN_KEYS (the worker's outcomes, plus
  "context_failed" and "failed").
- One summary line per tick on logger `haloflow.m05.writeback_scheduler`; codes and counts
  only, never exception text.
- F-H3-2 limitation kept (Codex pin 5): there is no phase timeout; a hung await blocks the
  rest of that tick; `stop()` is bounded independently. No row asserts time fairness.

Rows: U-J00 dependency controls; U-J01 inputs; U-J02 lifecycle; U-J03 pass order and fresh
contexts; U-J04 batch bound; U-J05 isolation; U-J05b failure classes; U-J06 outcome
mapping; U-J07 stop during run_one; U-J08 stop during a per-op context; U-J09 stop idle;
U-J10 hang -> STOP_FAILED; U-J11 fixed rate and strict lateness; U-J12 privacy; U-J13
summary line; U-J14 static seams; U-J15 not composed; U-J16 manual-tick ownership.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import math
import pathlib
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import se_support as se

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"
MODULE_FILE = SRC / "m05" / "writeback_scheduler.py"
LOGGER = "haloflow.m05.writeback_scheduler"
T_A, T_B = se.TENANT_A, se.TENANT_B
PHONE, NAME = "+15550001234", "Synthia"
RUN_KEYS = ("observed_positive", "item_raised", "skipped", "deferred", "not_claimed",
            "stale", "spend_unknown", "spend_refused", "reconcile_failed", "context_failed",
            "failed")
WORKER_OUTCOMES = RUN_KEYS[:9]
BOUND_S = 5.0


def _mod() -> Any:
    import haloflow.m05.writeback_scheduler as module

    return module


def _state() -> Any:
    from haloflow.m05.due_scheduler import SchedulerState

    return SchedulerState


def op(i: int) -> str:
    return f"00000000-0000-4000-8000-{i:012d}"


# --- doubles ----------------------------------------------------------------------------------


Script = Callable[..., Awaitable[Any]]


class Ctx:
    """A distinct context object per construction (identity is what U-J03 checks)."""

    def __init__(self, tenant: str, n: int) -> None:
        self.tenant, self.n = tenant, n

    def __repr__(self) -> str:
        return f"Ctx({self.tenant}#{self.n})"


class World:
    """Scripted doubles sharing one ordered call log; defaults model a healthy tenant."""

    def __init__(self, clock: se.FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[Any, ...]] = []
        self.built = 0
        self.ops: dict[str, list[str]] = {}
        self.outcomes: dict[str, Any] = {}
        self.contexts_fn: Script = self._context
        self.discover_fn: Script = self._discover
        self.due_fn: Script = self._due
        self.run_fn: Script = self._run
        self.durations: dict[str, float] = {}

    async def _context(self, tenant: str) -> Any:
        self.built += 1
        return Ctx(tenant, self.built)

    async def _discover(self, ctx: Any, limit: int) -> int:
        return 0

    async def _due(self, ctx: Any, limit: int) -> list[dict[str, Any]]:
        return [{"wb_op_id": o, "phase": "check"} for o in self.ops.get(ctx.tenant, [])]

    async def _run(self, ctx: Any, op_id: str) -> Any:
        self.clock.now += self.durations.get(ctx.tenant, 0.0)
        await asyncio.sleep(0)
        return self.outcomes.get(op_id, "observed_positive")

    def contexts(self) -> Callable[[str], Awaitable[Any]]:
        async def contexts(tenant: str) -> Any:
            ctx = await self.contexts_fn(tenant)
            self.calls.append(("context", tenant, self.clock.now, ctx))
            return ctx

        return contexts

    def service(self) -> Any:
        from haloflow.m05.writeback import WritebackService

        world = self

        class Service(WritebackService):
            def __init__(self) -> None:
                super().__init__(object())  # type: ignore[arg-type]

            async def discover(self, context: Any, limit: int = 100) -> int:
                world.calls.append(("discover", context, limit))
                result: int = await world.discover_fn(context, limit)
                return result

            async def due(self, context: Any, limit: int = 100) -> list[dict[str, Any]]:
                world.calls.append(("due", context, limit))
                result: list[dict[str, Any]] = await world.due_fn(context, limit)
                return result

            def __getattribute__(self, name: str) -> Any:
                if name in ("claim", "decide", "spend", "settle", "capture_baseline"):
                    raise AssertionError(f"scheduler touched service.{name}")
                return super().__getattribute__(name)

        return Service()

    def worker(self) -> Any:
        from haloflow.m05.writeback import WritebackWorker

        world = self

        class Worker(WritebackWorker):
            def __init__(self) -> None:
                super().__init__(object(), object())

            async def run_one(self, context: Any, op_id: Any) -> str:
                world.calls.append(("run_one", context, str(op_id)))
                result: str = await world.run_fn(context, str(op_id))
                return result

        return Worker()

    def scheduler(self, tenants: tuple[str, ...] = (T_A,), **kwargs: Any) -> Any:
        kwargs.setdefault("interval_s", 60)
        kwargs.setdefault("stop_timeout_s", 1)
        kwargs.setdefault("batch_limit", 3)
        return _mod().WritebackScheduler(
            contexts=self.contexts(), service=self.service(), worker=self.worker(),
            tenants=tenants, clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def kinds(self, tenant: str | None = None) -> list[str]:
        out = []
        for c in self.calls:
            subject = c[1]
            name = subject if isinstance(subject, str) else getattr(subject, "tenant", "")
            if tenant is None or name == tenant:
                out.append(c[0])
        return out


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


def zero_run(**overrides: int) -> dict[str, int]:
    counts = dict.fromkeys(RUN_KEYS, 0)
    counts.update(overrides)
    return counts


def _raiser(exc: BaseException) -> Script:
    async def fn(*_a: Any) -> Any:
        raise exc

    return fn


# --- U-J00 dependency controls (GREEN now: accepted-candidate S-I / H3 interfaces) -----------


def test_sj_u00a_writeback_worker_and_service_interface() -> None:
    from haloflow.m05.writeback import WritebackService, WritebackWorker

    assert list(inspect.signature(WritebackWorker.run_one).parameters) == [
        "self", "context", "op_id"]
    assert list(inspect.signature(WritebackService.discover).parameters) == [
        "self", "context", "limit"]
    assert list(inspect.signature(WritebackService.due).parameters) == [
        "self", "context", "limit"]
    assert inspect.iscoroutinefunction(WritebackWorker.run_one)
    assert inspect.iscoroutinefunction(WritebackService.discover)
    assert inspect.iscoroutinefunction(WritebackService.due)


def test_sj_u00b_worker_outcome_vocabulary_is_closed() -> None:
    """Every string `run_one` can return (read from the accepted-candidate source) is a
    WORKER_OUTCOMES key; a new one is an interface change (pause S-J)."""

    source = (SRC / "m05" / "writeback.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    worker = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name == "WritebackWorker")
    returned: set[str] = set()
    protocol = [m for m in worker.body if isinstance(m, ast.AsyncFunctionDef)
                and m.name in ("run_one", "_check", "_reconcile", "_settle")]
    assert len(protocol) == 4
    for node in (n for m in protocol for n in ast.walk(m)):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            returned.add(node.value.value)
    assert returned <= set(WORKER_OUTCOMES), sorted(returned - set(WORKER_OUTCOMES))
    assert {"observed_positive", "item_raised", "not_claimed"} <= returned


def test_sj_u00c_activation_protected_set_is_the_accepted_one() -> None:
    """J09 depends on this: new work (discover, check claim, decide, spend) is protected;
    due, reconcile claim and settle are not."""

    from haloflow.m05 import writeback_statements as ws

    expected = frozenset({ws.DISCOVER_KEY, ws.CLAIM_KEY, ws.DECIDE_KEY, ws.SPEND_KEY})
    assert expected == ws.SI_PROTECTED
    for key in (ws.DUE_KEY, ws.CLAIM_RECONCILE_KEY, ws.SETTLE_KEY):
        assert key not in ws.SI_PROTECTED


def test_sj_u00d_lifecycle_precedent_present() -> None:
    from haloflow.m05.due_scheduler import SchedulerState
    from haloflow.m05.reminder_scheduler import ReminderScheduler

    assert {s.name for s in SchedulerState} >= {"NEW", "RUNNING", "STOPPING", "STOPPED",
                                                 "STOP_FAILED"}
    for name in ("start", "tick", "stop"):
        assert hasattr(ReminderScheduler, name)


# --- U-J01 inputs -----------------------------------------------------------------------------

BAD_INPUTS: dict[str, tuple[dict[str, Any], str]] = {
    "contexts-none": ({"contexts": None}, "CONTEXTS_INVALID"),
    "contexts-str": ({"contexts": "ctx"}, "CONTEXTS_INVALID"),
    "service-object": ({"service": object()}, "SERVICE_INVALID"),
    "worker-object": ({"worker": object()}, "WORKER_INVALID"),
    "tenants-list": ({"tenants": [T_A]}, "TENANTS_INVALID"),
    "tenants-empty": ({"tenants": ()}, "TENANTS_INVALID"),
    "tenants-dup": ({"tenants": (T_A, T_A)}, "TENANTS_INVALID"),
    "tenants-pattern": ({"tenants": ("Bad_Tenant",)}, "TENANTS_INVALID"),
    "tenants-1001": ({"tenants": tuple(f"t-{i:04d}" for i in range(1001))},
                     "TENANTS_INVALID"),
    "interval-29": ({"interval_s": 29}, "INTERVAL_INVALID"),
    "interval-301": ({"interval_s": 301}, "INTERVAL_INVALID"),
    "interval-float": ({"interval_s": 60.0}, "INTERVAL_INVALID"),
    "interval-bool": ({"interval_s": True}, "INTERVAL_INVALID"),
    "timeout-0": ({"stop_timeout_s": 0}, "TIMEOUT_INVALID"),
    "timeout-61": ({"stop_timeout_s": 61}, "TIMEOUT_INVALID"),
    "timeout-nan": ({"stop_timeout_s": math.nan}, "TIMEOUT_INVALID"),
    "timeout-bool": ({"stop_timeout_s": True}, "TIMEOUT_INVALID"),
    "batch-0": ({"batch_limit": 0}, "BATCH_LIMIT_INVALID"),
    "batch-1001": ({"batch_limit": 1001}, "BATCH_LIMIT_INVALID"),
    "batch-bool": ({"batch_limit": True}, "BATCH_LIMIT_INVALID"),
    "batch-float": ({"batch_limit": 3.0}, "BATCH_LIMIT_INVALID"),
}


@pytest.mark.parametrize("case", sorted(BAD_INPUTS))
def test_sj_u01_inputs_refused_before_any_call(case: str) -> None:
    module = _mod()
    world = World(se.FakeClock())
    overrides, code = BAD_INPUTS[case]
    kwargs: dict[str, Any] = {
        "contexts": world.contexts(), "service": world.service(), "worker": world.worker(),
        "tenants": (T_A,), "clock": world.clock, "sleep": world.clock.sleep}
    kwargs.update(overrides)
    with pytest.raises(module.WritebackSchedulerRefused) as caught:
        module.WritebackScheduler(**kwargs)
    assert caught.value.code == code
    assert world.calls == []


def test_sj_u01b_bounds_and_defaults() -> None:
    module = _mod()
    world = World(se.FakeClock())
    assert (module.DEFAULT_INTERVAL_S, module.INTERVAL_MIN_S, module.INTERVAL_MAX_S,
            module.STOP_TIMEOUT_MAX_S, module.TENANTS_MAX, module.DEFAULT_BATCH_LIMIT,
            module.BATCH_LIMIT_MAX) == (60, 30, 300, 60, 1000, 100, 1000)
    assert tuple(module.RUN_KEYS) == RUN_KEYS
    for interval in (30, 300):
        for limit in (1, 1000):
            s = world.scheduler(interval_s=interval, stop_timeout_s=60, batch_limit=limit)
            assert s.state is _state().NEW
    assert world.calls == []


# --- U-J02 lifecycle --------------------------------------------------------------------------


async def test_sj_u02_lifecycle_matrix() -> None:
    module = _mod()
    world = World(se.FakeClock(1000.0))
    s = world.scheduler()
    assert s.state is _state().NEW
    s.start()
    assert s.state is _state().RUNNING
    with pytest.raises(module.WritebackSchedulerRefused) as owned:
        await s.tick()
    assert owned.value.code == "LOOP_OWNS_TICKS"
    with pytest.raises(module.WritebackSchedulerRefused) as again:
        s.start()
    assert again.value.code == "NOT_STARTABLE"
    await asyncio.wait_for(s.stop(), BOUND_S)
    assert s.state is _state().STOPPED
    await asyncio.wait_for(s.stop(), BOUND_S)  # idempotent
    with pytest.raises(module.WritebackSchedulerRefused) as stopped:
        await s.tick()
    assert stopped.value.code == "SCHEDULER_STOPPED"
    with pytest.raises(module.WritebackSchedulerRefused) as restart:
        s.start()
    assert restart.value.code == "NOT_STARTABLE"


# --- U-J03 pass order and fresh contexts (1+N) ------------------------------------------------


async def test_sj_u03_pass_order_and_one_fresh_context_per_op() -> None:
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1), op(2)], T_B: []}
    s = world.scheduler((T_A, T_B), batch_limit=3)
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    shape = [(c[0], c[1] if isinstance(c[1], str) else c[1].tenant) for c in world.calls]
    assert shape == [
        ("context", T_A), ("discover", T_A), ("due", T_A),
        ("context", T_A), ("run_one", T_A), ("context", T_A), ("run_one", T_A),
        ("context", T_B), ("discover", T_B), ("due", T_B)]
    built = [c[3] for c in world.calls if c[0] == "context"]
    assert len({id(c) for c in built}) == len(built) == 4  # 1 + N per tenant, all fresh
    pass_ctx, op1_ctx, op2_ctx = built[0], built[1], built[2]
    calls = {c[0] + (c[2] if c[0] == "run_one" else ""): c for c in world.calls
             if c[0] != "context" and c[1].tenant == T_A}
    assert calls["discover"][1] is pass_ctx and calls["due"][1] is pass_ctx
    assert calls["run_one" + op(1)][1] is op1_ctx and calls["run_one" + op(2)][1] is op2_ctx
    assert [c[2] for c in world.calls if c[0] in ("discover", "due")] == [3, 3, 3, 3]
    assert result.codes == {"COMPLETE": 2}
    a, b = result.tenants
    assert (a.tenant, a.context, a.discover.status, a.run.status) == (T_A, "ok", "ok", "ok")
    assert a.run.counts == zero_run(observed_positive=2)
    assert b.run.counts == zero_run()
    await _drain(s)


# --- U-J04 batch bound ------------------------------------------------------------------------


async def test_sj_u04_at_most_batch_limit_ops_even_if_due_overreturns() -> None:
    world = World(se.FakeClock())
    world.ops = {T_A: [op(i) for i in range(5)]}  # due() ignores the limit (misbehaving)
    s = world.scheduler(batch_limit=3)
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    assert [c[2] for c in world.calls if c[0] == "run_one"] == [op(0), op(1), op(2)]
    assert sum(result.tenants[0].run.counts.values()) == 3
    await _drain(s)


# --- U-J05 isolation --------------------------------------------------------------------------

ISOLATION = ("pass_context", "discover", "due", "op_context", "run_one")


@pytest.mark.parametrize("phase", ISOLATION)
async def test_sj_u05_a_failure_never_stops_later_ops_or_tenants(phase: str) -> None:
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1), op(2)], T_B: [op(3)]}
    boom = RuntimeError("boom")
    original_ctx, original_run = world.contexts_fn, world.run_fn
    built_for_a = 0

    async def ctx_fn(tenant: str) -> Any:
        nonlocal built_for_a
        if tenant == T_A:
            built_for_a += 1
            if (phase == "pass_context" and built_for_a == 1) or \
                    (phase == "op_context" and built_for_a == 2):
                raise boom
        return await original_ctx(tenant)

    async def run_fn(ctx: Any, op_id: str) -> Any:
        if phase == "run_one" and op_id == op(1):
            raise boom
        return await original_run(ctx, op_id)

    async def scoped_raise(ctx: Any, limit: int) -> Any:
        if ctx.tenant == T_A:
            raise boom
        return await (World._discover(world, ctx, limit) if phase == "discover"
                      else World._due(world, ctx, limit))

    world.contexts_fn, world.run_fn = ctx_fn, run_fn
    if phase in ("discover", "due"):
        setattr(world, f"{phase}_fn", scoped_raise)
    s = world.scheduler((T_A, T_B))
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    a, b = result.tenants
    assert a.code == "DEGRADED"
    assert b.code == "COMPLETE" and b.run.counts == zero_run(observed_positive=1)
    ran_a = [c[2] for c in world.calls if c[0] == "run_one" and c[1].tenant == T_A]
    if phase == "pass_context":
        assert a.context == "INTERNAL"
        assert (a.discover.status, a.run.status) == ("NOT_RUN", "NOT_RUN")
        assert ran_a == []
    elif phase == "discover":
        assert (a.discover.status, a.run.status) == ("INTERNAL", "ok")
        assert ran_a == [op(1), op(2)]  # the run phase runs whatever discover did
    elif phase == "due":
        assert (a.discover.status, a.run.status) == ("ok", "INTERNAL")
        assert ran_a == []
    elif phase == "op_context":
        assert a.run.status == "DEGRADED_ITEMS"
        assert a.run.counts == zero_run(context_failed=1, observed_positive=1)
        assert ran_a == [op(2)]  # op 1 skipped (no context); op 2 still runs
    else:
        assert a.run.status == "DEGRADED_ITEMS"
        assert a.run.counts == zero_run(failed=1, observed_positive=1)
        assert ran_a == [op(1), op(2)]
    await _drain(s)


def _typed_cases() -> dict[str, tuple[Exception, str]]:
    from haloflow.m01.errors import ActivationRefused, CapabilityDenied, TenantDenied
    from haloflow.m05.writeback import WritebackFailed, WritebackRefused

    return {
        "activation": (ActivationRefused(), "ACTIVATION_REFUSED"),
        "capability": (CapabilityDenied(), "CAPABILITY_REFUSED"),
        "tenant": (TenantDenied(), "CAPABILITY_REFUSED"),
        "refused-caps": (WritebackRefused("CAPABILITIES_INVALID"), "CAPABILITY_REFUSED"),
        "refused-workload": (WritebackRefused("PRINCIPAL_NOT_WORKLOAD"), "CAPABILITY_REFUSED"),
        "refused-other": (WritebackRefused("LIMIT_INVALID"), "REFUSED"),
        "failed": (WritebackFailed("RESULT_INVALID"), "INTERNAL"),
        "other": (KeyError("x"), "INTERNAL"),
    }


TYPED_CASES = ("activation", "capability", "tenant", "refused-caps", "refused-workload",
               "refused-other", "failed", "other")


@pytest.mark.parametrize("case", TYPED_CASES)
@pytest.mark.parametrize("phase", ["discover", "due"])
async def test_sj_u05b_typed_phase_failure_class(phase: str, case: str) -> None:
    exc, failure_class = _typed_cases()[case]
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1)]}
    setattr(world, f"{phase}_fn", _raiser(exc))
    s = world.scheduler()
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    (a,) = result.tenants
    assert getattr(a, phase if phase == "discover" else "run").status == failure_class
    assert a.code == "DEGRADED"
    await _drain(s)


# --- U-J06 outcome mapping --------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [*WORKER_OUTCOMES, "unknown-word", None, 7])
async def test_sj_u06_run_one_outcome_mapping(outcome: Any) -> None:
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1)]}
    world.outcomes = {op(1): outcome}
    s = world.scheduler()
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    (a,) = result.tenants
    expected = outcome if outcome in WORKER_OUTCOMES else "failed"
    assert a.run.counts == zero_run(**{expected: 1})
    assert a.run.status == ("ok" if expected != "failed" else "DEGRADED_ITEMS")
    assert a.code == ("COMPLETE" if expected != "failed" else "DEGRADED")
    await _drain(s)


@pytest.mark.parametrize("rows", [
    "not-a-list", [{"phase": "check"}], [{"wb_op_id": 5}], ["x"]])
async def test_sj_u06b_malformed_due_rows_are_internal_and_run_nothing(rows: Any) -> None:
    world = World(se.FakeClock())

    async def due(ctx: Any, limit: int) -> Any:
        return rows

    world.due_fn = due
    s = world.scheduler()
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    assert result.tenants[0].run.status == "INTERNAL"
    assert "run_one" not in world.kinds()
    await _drain(s)


# --- U-J07 / U-J08 stop during run_one and during a per-op context ----------------------------


def _blocking(*, swallow: bool, value: Any) -> tuple[Script, asyncio.Event, list[str]]:
    release = asyncio.Event()
    seen: list[str] = []

    async def block(*_args: Any) -> Any:
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
        return value

    return block, release, seen


@pytest.mark.parametrize("swallow", [False, True], ids=["cooperative", "swallows"])
@pytest.mark.parametrize("where", ["run_one", "op_context"])
async def test_sj_u07_u08_stop_mid_pass_runs_no_later_op(where: str, swallow: bool) -> None:
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1), op(2)], T_B: [op(3)]}
    original_ctx = world.contexts_fn
    builds = 0
    if where == "run_one":
        block, release, seen = _blocking(swallow=swallow, value="observed_positive")

        async def run(ctx: Any, op_id: str) -> Any:
            return await block() if op_id == op(1) else "observed_positive"

        world.run_fn = run
    else:
        block, release, seen = _blocking(swallow=swallow, value=Ctx(T_A, 99))

        async def ctx_fn(tenant: str) -> Any:
            nonlocal builds
            builds += 1
            return await block() if builds == 2 else await original_ctx(tenant)

        world.contexts_fn = ctx_fn
    s = world.scheduler((T_A, T_B), stop_timeout_s=0.2)
    s.start()
    try:
        await _until(lambda: seen[:1] == ["entered"], f"{where} blocked")
        stopping = asyncio.ensure_future(s.stop())
        await _until(lambda: "cancelled" in seen, "cancel delivered")
        release.set()
        await asyncio.wait_for(stopping, BOUND_S)
    finally:
        release.set()  # guaranteed release before the drain, even on an early failure
        await _drain(s)
    assert s.state is _state().STOPPED
    ran = [c[2] for c in world.calls if c[0] == "run_one"]
    assert op(2) not in ran and op(3) not in ran  # nothing after the blocked step
    if where == "op_context":
        assert ran == []


async def test_sj_u09_stop_before_any_tick_makes_no_call() -> None:
    world = World(se.FakeClock())
    s = world.scheduler()
    await asyncio.wait_for(s.stop(), BOUND_S)
    assert s.state is _state().STOPPED
    assert world.calls == []


async def test_sj_u10_hang_that_ignores_cancel_is_stop_failed_and_owns_the_task() -> None:
    """The stop is observed by an independently bounded `asyncio.wait`, never by awaiting
    stop() itself: an unbounded stop (mutant J-M5) fails the bounded assertion instead of
    hanging the row, and the hold is always released in `finally`."""

    module = _mod()
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1)]}
    block, release, seen = _blocking(swallow=True, value="observed_positive")
    world.run_fn = block
    s = world.scheduler(stop_timeout_s=0.05)
    s.start()
    stopping: asyncio.Future[Any] | None = None
    later: asyncio.Future[Any] | None = None
    try:
        await _until(lambda: seen[:1] == ["entered"], "run_one blocked")
        stopping = asyncio.ensure_future(s.stop())
        done, _ = await asyncio.wait({stopping}, timeout=BOUND_S)
        assert stopping in done, "stop() did not return within its bound (unbounded stop)"
        with pytest.raises(module.WritebackSchedulerStopFailed) as failed:
            stopping.result()
        assert failed.value.code == "STOP_TIMEOUT"
        assert s.state is _state().STOP_FAILED
        with pytest.raises(module.WritebackSchedulerRefused):
            s.start()
        with pytest.raises(module.WritebackSchedulerRefused):
            await s.tick()
        release.set()
        later = asyncio.ensure_future(s.stop())  # a later stop() settles the owned task
        done, _ = await asyncio.wait({later}, timeout=BOUND_S)
        assert later in done and later.exception() is None
        assert s.state is _state().STOPPED
    finally:
        release.set()
        await _drain(s, *(f for f in (stopping, later) if f is not None))


# --- U-J11 fixed rate and strict lateness -----------------------------------------------------


async def test_sj_u11_fixed_rate_and_strict_lateness(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = se.FakeClock(1000.0)
    world = World(clock)
    world.ops = {T_A: [op(1)]}
    lengths = iter([0.0, 150.0, 0.0, 0.0])  # tick 2 lasts 2.5 intervals

    async def run(ctx: Any, op_id: str) -> Any:
        clock.now += next(lengths, 0.0)
        await asyncio.sleep(0)
        return "observed_positive"

    world.run_fn = run
    s = world.scheduler(interval_s=60)
    s.start()
    try:
        await _until(lambda: world.kinds().count("run_one") >= 3, "three ticks")
    finally:
        await _drain(s)
    starts = [c[2] for c in world.calls if c[0] == "context"][::2][:3]  # pass contexts
    assert starts == [1000.0, 1060.0, 1240.0]  # t0, t0+I, t0+4I: counted, never replayed
    lines = summaries(caplog.records)
    assert " skipped=0 " in lines[0] + " "
    assert " skipped=2 " in lines[1] + " "


# --- U-J12 privacy ----------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ISOLATION)
async def test_sj_u12_no_secret_text_anywhere(phase: str,
                                              caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1)]}
    leak = ValueError(f"{PHONE} {NAME} Reply 1 to confirm secret=hunter2")
    if phase in ("pass_context", "op_context"):
        original = world.contexts_fn
        builds = 0

        async def ctx_fn(tenant: str) -> Any:
            nonlocal builds
            builds += 1
            if builds == (1 if phase == "pass_context" else 2):
                raise leak
            return await original(tenant)

        world.contexts_fn = ctx_fn
    else:
        setattr(world, {"discover": "discover_fn", "due": "due_fn",
                        "run_one": "run_fn"}[phase], _raiser(leak))
    s = world.scheduler()
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    surfaces = [r.getMessage() for r in caplog.records]
    surfaces += [str(r.exc_info) for r in caplog.records if r.exc_info]
    surfaces += [repr(result), str(result), repr(s), repr(result.tenants)]
    for text in surfaces:
        for secret in (PHONE, NAME, "Reply 1", "hunter2"):
            assert secret not in text
    await _drain(s)


# --- U-J13 one summary line per tick ----------------------------------------------------------


async def test_sj_u13_one_summary_line_per_tick(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1), op(2)]}
    world.outcomes = {op(2): "item_raised"}

    async def discover(ctx: Any, limit: int) -> int:
        return 2

    world.discover_fn = discover
    s = world.scheduler()
    await asyncio.wait_for(s.tick(), BOUND_S)
    (line,) = summaries(caplog.records)
    assert line.startswith("writeback_scheduler tick=0 skipped=0 ")
    for part in ("cancelled=False", "codes=COMPLETE:1", "discovered=2",
                 "observed_positive=1", "item_raised=1", "failed=0", "context_failed=0"):
        assert part in line, part
    await _drain(s)


# --- U-J14 / U-J15 static ---------------------------------------------------------------------


def _source() -> str:
    return MODULE_FILE.read_text(encoding="utf-8")


def test_sj_u14_only_discover_due_run_one_and_no_network_or_platform_import() -> None:
    tree = ast.parse(_source())
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden = {"claim", "decide", "spend", "settle", "capture_baseline",
                 "write_confirmation", "close_permit", "read", "send", "run_claimed"}
    assert not (used & forbidden), sorted(used & forbidden)
    assert {"discover", "due", "run_one"} <= used
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    banned = ("haloflow.platform", "haloflow.startup", "haloflow.composition", "httpx",
              "requests", "aiohttp", "urllib", "socket", "http", "haloflow.modules",
              "haloflow.m05.simulated_ehr")
    for name in imported:
        assert not any(name == b or name.startswith(b + ".") for b in banned), name


def test_sj_u15_not_composed() -> None:
    assert MODULE_FILE.exists()
    for name in ("platform", "startup.py", "composition.py"):
        path = SRC / name
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            assert "writeback_scheduler" not in file.read_text(encoding="utf-8"), file


# --- U-J16 manual-tick ownership --------------------------------------------------------------


async def test_sj_u16_manual_tick_owns_the_scheduler() -> None:
    module = _mod()
    world = World(se.FakeClock())
    world.ops = {T_A: [op(1)]}
    block, release, seen = _blocking(swallow=False, value="observed_positive")
    world.run_fn = block
    s = world.scheduler()
    first = asyncio.ensure_future(s.tick())
    try:
        await _until(lambda: seen[:1] == ["entered"], "manual tick in run_one")
        with pytest.raises(module.WritebackSchedulerRefused) as again:
            await s.tick()
        assert again.value.code == "TICK_IN_PROGRESS"
        with pytest.raises(module.WritebackSchedulerRefused) as start:
            s.start()
        assert start.value.code == "TICK_IN_PROGRESS"
        release.set()
        result = await asyncio.wait_for(first, BOUND_S)
        assert result.codes == {"COMPLETE": 1}
        second = await asyncio.wait_for(s.tick(), BOUND_S)  # a finished tick frees it
        assert second.tick == 1
    finally:
        await _drain(s, first)
