"""The synthetic runtime lifecycle (Option B B3a; package v1 R-B3a-3..-7 as revised by v1r1
D2/D3 and v1r2 Item 1/Item 2). SYNTHETIC ONLY.

States: NEW -> STARTING -> RUNNING -> STOPPING -> STOPPED | STOP_FAILED; a failed start ends
in FAILED. Nothing here performs I/O before `start()`.

Startup (`start`), each step followed by its test-only hook point `b3.start.<step>`:
  A0 (environment and state file, no connection) -> attested reservation read -> shared K_S
  lease (`lease`) -> A1 on the full seal (before the first start) or the reduced seal (after
  it) (`a1`) -> first-start marker, written BEFORE the pool opens (`marker.before`,
  `marker.after`; only when absent) -> guarded pool (`pool`) -> composed tenant runtime
  (`runtime`) -> fake sender, simulated EHR port and the three schedulers (`wiring`) ->
  schedulers started due, reminder (`schedulers`), write-back -> lease watcher (`watcher`)
  -> admission opens. Any failure: everything opened is closed through the same drain
  coordinator, the state is FAILED and the error is a closed code or a class name only.

Shutdown: ONE `DrainCoordinator` per lifecycle. Every stop source (the watcher, a guard
refusal inside a tick, lifespan exit, a startup failure) calls `request_stop(reason)`:
synchronous, idempotent, non-blocking; it closes admission at once and sets ONE absolute
deadline `monotonic() + T_total`. The coordinator then runs, each step as its own task
waited with `asyncio.wait` (never `wait_for`: a step that swallows cancellation is abandoned
at its budget, never awaited), each preceded by `b3.stop.<step>`:
  watcher -> producers (write-back, reminder, due; attempt 1, then attempt 2 for the failed
  ones) -> port -> pool -> quiesce (no backend of this lifecycle's application_name remains)
  -> lease (release; or, when the lease was lost, cleanup of the lease backend).
The lease is released normally ONLY after quiescence is confirmed. Any unconfirmed step,
the deadline passing or attempt-2 StopFailed goes to the terminal path: one fixed line
`fatal: <CODE> <step>` and `fatal_exit(3)`; process exit then ends the pool and lease
backends; the terminal path never releases the lease early, and backend cleanup is
eventual (process exit does not guarantee any server-side teardown order). A watchdog thread
(`T_total + 1 s`) covers an event loop blocked by a synchronous call. `TerminalGate`
guarantees a single terminal disposition.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from haloflow.synthetic.attest import admin_connect, check_a0
from haloflow.synthetic.dsn import AdminEndpoint, parse_admin_dsn
from haloflow.synthetic.errors import SyntheticRefused, refused_from
from haloflow.synthetic.hooks import fire
from haloflow.synthetic.state import StateDir

STATES: Final = ("NEW", "STARTING", "RUNNING", "STOPPING", "STOPPED", "STOP_FAILED",
                 "FAILED")
DEFAULT_APPLICATION_NAME: Final = "haloflow-synthetic"
PRODUCERS: Final = ("writeback", "reminder", "due")
LEASE_LOST: Final = "RUNTIME_LEASE_LOST"
FATAL_EXIT_CODE: Final = 3
MAX_TOTAL_S: Final = 60
QUIESCE_POLL_S: Final = 0.02
CLEANUP_CONNECT_TIMEOUT_S: Final = 5

FatalExit = Callable[..., Any]


def default_fatal_exit(rc: int, code: str, step: str) -> None:
    """The fixed terminal line, then process exit (the `serve` path always uses this)."""
    with contextlib.suppress(Exception):
        sys.stderr.write(f"fatal: {code} {step}\n")
        sys.stderr.flush()
    os._exit(rc)


# ---------------------------------------------------------------------------- config

@dataclass(frozen=True)
class LifecycleConfig:
    """Watcher cadence and drain budgets (seconds). Validated before any I/O."""

    lease_check_s: int = 5
    stop_timeout_s: tuple[float, float, float] = (2, 2, 2)     # writeback, reminder, due
    t_watcher: float = 2
    t_port: float = 2
    t_pool: float = 5
    t_quiesce: float = 2
    t_lease: float = 5

    def __post_init__(self) -> None:
        lease = self.lease_check_s
        if type(lease) is not int or not 1 <= lease <= 30:
            raise SyntheticRefused("LEASE_CHECK_INVALID")
        stops = tuple(self.stop_timeout_s)
        if len(stops) != 3 or not all(_positive(v) for v in stops):
            raise SyntheticRefused("BUDGET_INVALID")
        object.__setattr__(self, "stop_timeout_s", stops)
        for value in (self.t_watcher, self.t_port, self.t_pool, self.t_quiesce, self.t_lease):
            if not _positive(value):
                raise SyntheticRefused("BUDGET_INVALID")
        if self.t_total > MAX_TOTAL_S:
            raise SyntheticRefused("BUDGET_TOTAL_EXCEEDED")

    @property
    def t_total(self) -> float:
        """v1r2 D3.4a: t_watcher + 2 x sum(stop) + t_port + t_pool + t_quiesce + t_lease."""
        return (self.t_watcher + 2 * sum(self.stop_timeout_s) + self.t_port + self.t_pool
                + self.t_quiesce + self.t_lease)

    def stop_timeout(self, producer: str) -> float:
        return float(self.stop_timeout_s[PRODUCERS.index(producer)])


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


# ---------------------------------------------------------------------------- terminal

class TerminalGate:
    """Exactly one terminal disposition, whichever of the coordinator and the watchdog
    thread reaches it first (v1r2 D3.5b/c; mutant 31)."""

    def __init__(self, fatal_exit: FatalExit | None = None) -> None:
        self._fatal_exit = fatal_exit or default_fatal_exit
        self._lock = threading.Lock()
        self.fired: tuple[str, str] | None = None

    def terminal(self, code: str, step: str) -> bool:
        with self._lock:
            if self.fired is not None:
                return False
            self.fired = (code, step)
        self._fatal_exit(FATAL_EXIT_CODE, code, step)
        return True


@dataclass(frozen=True)
class LifecycleResult:
    disposition: str                   # drained | terminal | lease_lost_drained
    code: str | None
    step: str | None
    reason: str | None
    cleanup: str | None                # None | gone | terminated | unknown
    deadline: float | None


# ---------------------------------------------------------------------------- coordinator

@dataclass
class Step:
    """One drain step: `run` is called only when the step starts; `budget` is its own bound
    (the deadline caps it); `code` is its terminal code when unconfirmed."""

    name: str
    hook: str
    budget: float
    run: Callable[[], Awaitable[Any]]
    code: str


@dataclass
class _Drain:
    reason: str
    deadline: float
    task: asyncio.Task[LifecycleResult]
    watchdog: threading.Timer | None
    abandoned: list[asyncio.Task[Any]] = field(default_factory=list)


class DrainCoordinator:
    """The single drain owner (v1r1 D3.1; v1r2 D3.4a-c, D3.5a-c)."""

    def __init__(self, *, steps: Callable[[], Sequence[Step]],
                 producers: Callable[[], Sequence[tuple[str, Callable[[], Awaitable[Any]],
                                                       float]]],
                 gate: TerminalGate, config: LifecycleConfig,
                 cleanup: Callable[[], str | None] = lambda: None,
                 on_request: Callable[[str], None] | None = None,
                 on_done: Callable[[LifecycleResult], None] | None = None) -> None:
        self._steps = steps
        self._producers = producers
        self._gate = gate
        self.config = config
        self._cleanup = cleanup
        self._on_request = on_request
        self._on_done = on_done
        self._drain: _Drain | None = None
        self.result: LifecycleResult | None = None

    @classmethod
    def for_test(cls, *, rec: Any, steps: Sequence[Any], fatal_exit: FatalExit,
                 config: LifecycleConfig) -> DrainCoordinator:
        """Fake resources with `.name` and `async close()` (unit rows D4u/D9u)."""
        del rec                                     # the resources record themselves
        by = {s.name: s for s in steps}
        budgets = {"watcher": config.t_watcher, "port": config.t_port, "pool": config.t_pool,
                   "quiesce": config.t_quiesce, "lease": config.t_lease}
        codes = {"watcher": "WATCHER_STOP_FAILED", "port": "CLOSE_TIMEOUT",
                 "pool": "POOL_NOT_QUIESCENT", "quiesce": "POOL_NOT_QUIESCENT",
                 "lease": "CLOSE_TIMEOUT"}

        def make() -> list[Step]:
            return [Step(n, n, budgets[n], by[n].close, codes[n])
                    for n in ("watcher", "port", "pool", "quiesce", "lease") if n in by]

        def producers() -> list[tuple[str, Callable[[], Awaitable[Any]], float]]:
            return [(n, by[n].close, config.stop_timeout(n)) for n in PRODUCERS if n in by]

        return cls(steps=make, producers=producers, gate=TerminalGate(fatal_exit),
                   config=config)

    # -- requests ------------------------------------------------------------------------

    @property
    def requested(self) -> bool:
        return self._drain is not None

    @property
    def deadline(self) -> float | None:
        return None if self._drain is None else self._drain.deadline

    @property
    def reason(self) -> str | None:
        return None if self._drain is None else self._drain.reason

    def request_stop(self, reason: str) -> None:
        """Synchronous, idempotent, non-blocking: the first call records the reason, sets
        the single deadline, arms the watchdog and creates the coordinator task."""
        if self._drain is not None:
            return None
        deadline = time.monotonic() + self.config.t_total
        watchdog = threading.Timer(self.config.t_total + 1.0, self._gate.terminal,
                                   args=("DRAIN_DEADLINE", "watchdog"))
        watchdog.daemon = True
        loop = asyncio.get_running_loop()
        drain = _Drain(reason, deadline, None, watchdog)  # type: ignore[arg-type]
        self._drain = drain
        if self._on_request is not None:
            self._on_request(reason)
        watchdog.start()
        drain.task = loop.create_task(self._run(drain))
        return None

    async def wait(self) -> LifecycleResult:
        assert self._drain is not None, "no stop requested"
        return await asyncio.shield(self._drain.task)

    # -- the drain -----------------------------------------------------------------------

    async def _step(self, drain: _Drain, run: Callable[[], Awaitable[Any]], budget: float
                    ) -> str:
        """`ok` | `failed` | `timeout` | `deadline`. Never awaits a cancelled step."""
        remaining = drain.deadline - time.monotonic()
        if remaining <= 0:
            return "deadline"
        bound = min(budget, remaining)

        async def call() -> Any:
            return await run()

        task = asyncio.get_running_loop().create_task(call())
        done, _ = await asyncio.wait({task}, timeout=bound)
        if not done:
            task.cancel()
            drain.abandoned.append(task)
            return "deadline" if bound < budget else "timeout"
        if task.cancelled() or task.exception() is not None:
            return "failed"
        return "ok"

    async def _run(self, drain: _Drain) -> LifecycleResult:
        try:
            result = await self._sequence(drain)
        except asyncio.CancelledError:
            # The coordinator itself was cancelled (event-loop teardown): the process is ending
            # and process exit ends every backend. No fatal_exit, and the watchdog is disarmed.
            if drain.watchdog is not None:
                drain.watchdog.cancel()
            raise
        except Exception:  # noqa: BLE001 - an unexpected fault is a terminal disposition
            result = self._terminal(drain, "RUNTIME_STOP_FAILED", "coordinator")
        if drain.watchdog is not None:
            drain.watchdog.cancel()
        self.result = result
        if self._on_done is not None:
            self._on_done(result)
        return result

    async def _sequence(self, drain: _Drain) -> LifecycleResult:
        steps = list(self._steps())
        watcher = [s for s in steps if s.name == "watcher"]
        rest = [s for s in steps if s.name != "watcher"]
        for step in watcher:
            outcome = await self._run_hooked(drain, step)
            if outcome != "ok":
                return self._terminal(drain, self._code(outcome, step.code), step.name)
        fire("b3.stop.producers", {})
        failed: list[str] = []
        for name, stop, budget in self._producers():
            if await self._step(drain, stop, budget) != "ok":
                failed.append(name)
        if failed:
            retry = {n: (s, b) for n, s, b in self._producers() if n in failed}
            failed = [n for n in failed
                      if await self._step(drain, retry[n][0], retry[n][1]) != "ok"]
            if failed:
                code = "DRAIN_DEADLINE" if time.monotonic() >= drain.deadline \
                    else "RUNTIME_STOP_FAILED"
                return self._terminal(drain, code, " ".join(failed))
        cleanup: str | None = None
        for step in rest:
            outcome = await self._run_hooked(drain, step)
            if outcome != "ok":
                return self._terminal(drain, self._code(outcome, step.code), step.name)
            if step.name == "lease":
                cleanup = self._cleanup()
                if cleanup == "unknown":
                    return self._terminal(drain, "LEASE_CLEANUP_UNKNOWN", "lease")
        disposition = "lease_lost_drained" if cleanup in ("gone", "terminated") else "drained"
        return LifecycleResult(disposition, None, None, drain.reason, cleanup, drain.deadline)

    async def _run_hooked(self, drain: _Drain, step: Step) -> str:
        fire(f"b3.stop.{step.hook}", {})
        return await self._step(drain, step.run, step.budget)

    @staticmethod
    def _code(outcome: str, code: str) -> str:
        return "DRAIN_DEADLINE" if outcome == "deadline" else code

    def _terminal(self, drain: _Drain, code: str, step: str) -> LifecycleResult:
        if drain.watchdog is not None:
            drain.watchdog.cancel()
        self._gate.terminal(code, step)
        # Reached only when fatal_exit returned (an in-process test seam; `serve` has exited):
        # dispose of abandoned steps that ignore cancellation, so the event loop can close.
        for task in drain.abandoned:
            with contextlib.suppress(Exception):
                task.get_coro().close()
        return LifecycleResult("terminal", code, step, drain.reason, None, drain.deadline)


# ---------------------------------------------------------------------------- lifecycle

class LifecycleStartFailed(Exception):
    """A startup failure that is not a closed-code refusal: the class name only."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class SyntheticLifecycle:
    def __init__(self, environ: Mapping[str, str], *, config: LifecycleConfig | None = None,
                 sender_factory: Callable[[], Any] | None = None,
                 ehr_factory: Callable[[], Any] | None = None,
                 fatal_exit: FatalExit | None = None,
                 application_name: str | None = None) -> None:
        self._environ = dict(environ)
        self.config = config or LifecycleConfig()
        self._sender_factory = sender_factory
        self._ehr_factory = ehr_factory
        self.application_name = application_name or DEFAULT_APPLICATION_NAME
        self._gate = TerminalGate(fatal_exit)
        self.state = "NEW"
        self.admission_open = False
        self.runtime: Any = None
        self.sender: Any = None
        self.port: Any = None
        self.schedulers: dict[str, Any] = {}
        self.note_keys: Any = None
        self.note_keys_names: tuple[str, ...] = ()
        self.watcher_checks = 0
        self._endpoint: AdminEndpoint | None = None
        self._started: list[str] = []
        self._watcher_task: asyncio.Task[None] | None = None
        self._lease_future: concurrent.futures.Future[Any] | None = None
        self._lease_lock = threading.Lock()
        self._lease_state = "pending"
        self._lease_lost = False
        self._guard_code: str | None = None
        self._stop_pending: str | None = None
        self._cleanup: str | None = None
        self._stopped = asyncio.Event()
        self._coordinator = DrainCoordinator(
            steps=self._drain_steps, producers=self._drain_producers, gate=self._gate,
            config=self.config, cleanup=lambda: self._cleanup, on_done=self._done)

    # -- views ---------------------------------------------------------------------------

    @property
    def deadline(self) -> float | None:
        return self._coordinator.deadline

    @property
    def stop_code(self) -> str:
        """The closed code a refused request carries once stopping has begun."""
        if self._coordinator.reason == LEASE_LOST or self._lease_lost:
            return LEASE_LOST
        return "RUNTIME_STOPPED"

    def _tick_gate(self) -> str | None:
        if self.state == "RUNNING" and not self._coordinator.requested:
            return None
        return self._guard_code or self.stop_code

    # -- start ---------------------------------------------------------------------------

    async def start(self) -> None:
        if self.state != "NEW":
            raise SyntheticRefused("RUNTIME_NOT_STARTABLE")
        self.state = "STARTING"
        try:
            await self._start()
        except BaseException as error:
            self._abandon_lease()            # synchronous: no await can interrupt it
            try:
                await self._abort()
            finally:
                self.state = "FAILED"         # also when the abort's wait is cancelled
            if isinstance(error, (SyntheticRefused, asyncio.CancelledError)):
                raise
            refused = refused_from(error)
            if refused is not None:
                raise refused from None
            raise LifecycleStartFailed(type(error).__name__) from None
        self.state = "RUNNING"
        self.admission_open = True
        if self._stop_pending is not None:
            self.request_stop(self._stop_pending)

    async def _start(self) -> None:
        from haloflow.synthetic import wiring
        from haloflow.synthetic.provision import check_state
        from haloflow.synthetic.runtime import SyntheticRuntime

        env = self._environ
        check_a0(env, attest_flag=True)                                    # A0: no I/O
        endpoint = parse_admin_dsn(env["HALOFLOW_SYNTHETIC_ADMIN_DSN"], env)
        state_dir = env["HALOFLOW_SYNTHETIC_STATE"]
        with StateDir(state_dir).locked() as st:
            state = st.read()
        if state is None:
            raise SyntheticRefused("STATE_MISSING")
        check_state(state)
        if state["phase"] != "ready":
            raise SyntheticRefused("STATE_CORRUPT", ("phase_not_ready",))
        self._endpoint = endpoint
        self.note_keys = wiring.StateNoteKeys(state["note_keys"])
        self.note_keys_names = tuple(sorted(state["note_keys"]))
        fire("b3.start.a0", {})
        runtime = SyntheticRuntime(endpoint, state, application_name=self.application_name,
                                   state_dir=state_dir, on_refused=self._on_guard_refused)
        self.runtime = runtime
        # The lease acquisition runs in its own thread and is OWNED until its outcome is known
        # (Codex review 85d836a): cancelling `start` never abandons an in-flight acquisition;
        # `_abort` waits for it (bounded) or hands a late success to `_release_late`.
        await asyncio.to_thread(runtime.read_reservation)
        self._lease_future = _in_thread(runtime.acquire_lease)
        self._lease_future.add_done_callback(self._lease_done)
        await asyncio.shield(asyncio.wrap_future(self._lease_future))
        self._claim_lease()              # synchronous, right after the await: no gap
        fire("b3.start.lease", {})
        await asyncio.to_thread(runtime.attest)
        fire("b3.start.a1", {})
        if runtime.needs_marker():
            fire("b3.start.marker.before", {})
            await asyncio.to_thread(runtime.write_marker)
            fire("b3.start.marker.after", {})
        await runtime.open_pool()
        fire("b3.start.pool", {})
        rt = runtime.compose()
        fire("b3.start.runtime", {})
        self.sender = self._sender_factory() if self._sender_factory is not None \
            else _fake_sender()
        ehr = self._ehr_factory() if self._ehr_factory is not None else _simulated_ehr()
        built = wiring.build(rt, state["tenant_id"], sender=self.sender, ehr=ehr,
                             stop_timeouts={n: self.config.stop_timeout(n) for n in PRODUCERS},
                             gate=self._tick_gate)
        self.port = built.port
        self.schedulers = built.schedulers
        fire("b3.start.wiring", {})
        for name in ("due", "reminder"):
            self.schedulers[name].start()
            self._started.append(name)
        fire("b3.start.schedulers", {})
        self.schedulers["writeback"].start()
        self._started.append("writeback")
        self._watcher_task = asyncio.get_running_loop().create_task(self._watch())
        fire("b3.start.watcher", {})

    # -- lease ownership (Codex reviews 85d836a, 0e11467) ------------------------------------
    #
    # ONE owner for the lease at every moment, never the (cancellable) startup caller:
    #   pending    the acquisition thread runs; nobody may release yet;
    #   claimed    `start` passed the lease step; from here the DRAIN (the coordinator's own
    #              task, independent of any caller) owns it and releases it after quiescence;
    #   abandoned  `start` failed before claiming; the ACQUIRING THREAD's completion callback
    #              (or, if it already completed, the abandon step itself) releases a success.
    # Transitions happen under one lock, so release happens exactly once whatever the order of
    # completion and abandonment; no transition awaits, so no cancellation can interrupt one.

    def _claim_lease(self) -> None:
        with self._lease_lock:
            if self._lease_state == "pending":
                self._lease_state = "claimed"

    def _abandon_lease(self) -> None:
        future = self._lease_future
        with self._lease_lock:
            if self._lease_state != "pending":
                return
            self._lease_state = "abandoned"
            release = future is not None and future.done() and not future.cancelled() \
                and future.exception() is None
        if release:
            threading.Thread(target=self._release_abandoned, daemon=True,
                             name="haloflow-synthetic-lease-release").start()

    def _lease_done(self, future: concurrent.futures.Future[Any]) -> None:
        """Runs in the acquiring thread when the acquisition completes."""
        with self._lease_lock:
            release = self._lease_state == "abandoned"
        if release and not future.cancelled() and future.exception() is None:
            self._release_abandoned()

    def _release_abandoned(self) -> None:
        lease = self.runtime.lease if self.runtime is not None else None
        if lease is not None:
            with contextlib.suppress(Exception):
                lease.release()

    async def _abort(self) -> None:
        """Startup failure: when the lease was claimed, everything opened is closed in reverse
        by the coordinator's own task (the caller only waits for it, shielded: a further
        cancellation stops the waiting, never the drain). An unclaimed lease is already owned
        by `_abandon_lease` / `_lease_done`, and nothing else can be open."""
        if self.runtime is None or self._lease_state != "claimed":
            return
        self._coordinator.request_stop("STARTUP_FAILED")
        await self._coordinator.wait()

    # -- stop ----------------------------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        """Synchronous, idempotent, non-blocking (v1r1 D3.1/D3.2)."""
        if self.state == "NEW":
            return None
        if self.state == "STARTING":
            if self._stop_pending is None:
                self._stop_pending = reason
            return None
        self.admission_open = False
        if self.state == "RUNNING":
            self.state = "STOPPING"
            self._coordinator.request_stop(reason)
        return None

    async def wait_stopped(self) -> LifecycleResult:
        await self._stopped.wait()
        assert self._coordinator.result is not None
        return self._coordinator.result

    def _done(self, result: LifecycleResult) -> None:
        if self.state not in ("STARTING", "FAILED"):
            self.state = "STOP_FAILED" if result.disposition == "terminal" else "STOPPED"
        self._stopped.set()

    def _on_guard_refused(self, code: str) -> None:
        """A guard refusal inside runtime work: stop, no retry (R-B3a-5, L2)."""
        if self._guard_code is None:
            self._guard_code = code
        self.request_stop(code)

    # -- watcher -------------------------------------------------------------------------

    async def _watch(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.config.lease_check_s)
            lease = self.runtime.lease
            check = loop.run_in_executor(None, lease.check)
            done, _ = await asyncio.wait({check}, timeout=self.config.lease_check_s)
            self.watcher_checks += 1
            if not done or check.exception() is not None:
                self._lease_lost = True
                self.request_stop(LEASE_LOST)
                return

    async def _watcher_stop(self) -> None:
        task = self._watcher_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -- drain steps (bound to this lifecycle's resources at drain time) -----------------

    def _drain_steps(self) -> list[Step]:
        cfg = self.config

        async def watcher() -> None:
            await self._watcher_stop()

        async def port() -> None:
            if self.port is not None:
                self.port.close()

        async def pool() -> None:
            if self.runtime is not None and self.runtime.pool is not None:
                await self.runtime.pool.close()

        async def quiesce() -> None:
            if self.runtime is None or self.runtime.pool is None:
                return
            while await asyncio.to_thread(self._runtime_backends) != 0:
                await asyncio.sleep(QUIESCE_POLL_S)

        async def lease() -> None:
            if self.runtime is None or self.runtime.lease is None:
                return
            if self._lease_lost:
                self._cleanup = await asyncio.to_thread(self._lease_cleanup)
            else:
                await asyncio.to_thread(self.runtime.lease.release)

        return [Step("watcher", "watcher", cfg.t_watcher, watcher, "WATCHER_STOP_FAILED"),
                Step("port", "port", cfg.t_port, port, "CLOSE_TIMEOUT"),
                Step("pool", "pool", cfg.t_pool, pool, "POOL_NOT_QUIESCENT"),
                Step("quiesce", "quiesce", cfg.t_quiesce, quiesce, "POOL_NOT_QUIESCENT"),
                Step("lease", "lease", cfg.t_lease, lease, "CLOSE_TIMEOUT")]

    def _drain_producers(self) -> list[tuple[str, Callable[[], Awaitable[Any]], float]]:
        out: list[tuple[str, Callable[[], Awaitable[Any]], float]] = []
        for name in PRODUCERS:
            if name in self._started:
                scheduler = self.schedulers[name]

                async def stop(s: Any = scheduler) -> None:
                    await s.stop()

                out.append((name, stop, self.config.stop_timeout(name)))
        return out

    def _runtime_backends(self) -> int:
        assert self._endpoint is not None
        with admin_connect(self._endpoint, "postgres",
                           connect_timeout=CLEANUP_CONNECT_TIMEOUT_S) as conn:
            row = conn.execute("SELECT count(*) FROM pg_catalog.pg_stat_activity"
                               " WHERE application_name = %s",
                               (self.application_name,)).fetchone()
        return int(row[0]) if row is not None else 0

    def _lease_cleanup(self) -> str:
        """v1r1 D3.3: the lease backend is checked from a fresh, bounded admin connection and
        terminated when it is still present (a failed liveness query does not prove the
        backend is gone); then the client side is closed. gone | terminated | unknown."""
        lease = self.runtime.lease
        pid = lease.pid
        assert self._endpoint is not None
        try:
            with admin_connect(self._endpoint, "postgres",
                               connect_timeout=CLEANUP_CONNECT_TIMEOUT_S) as conn:
                present = _backend_present(conn, pid)
                outcome = "gone"
                if present:
                    conn.execute("SELECT pg_catalog.pg_terminate_backend(%s)", (pid,))
                    end = time.monotonic() + self.config.t_lease / 2
                    while _backend_present(conn, pid):
                        if time.monotonic() > end:
                            return "unknown"
                        time.sleep(QUIESCE_POLL_S)
                    outcome = "terminated"
        except Exception:  # noqa: BLE001 - the admin endpoint unavailable: unknown
            return "unknown"
        finally:
            lease.close_client()
        return outcome


def _in_thread(fn: Callable[[], Any]) -> concurrent.futures.Future[Any]:
    """Run `fn` in a dedicated daemon thread; the concurrent future completes (and runs its
    done callbacks) in that thread whatever happens to the event loop."""
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn())
        except BaseException as error:  # noqa: BLE001 - delivered to the owner
            future.set_exception(error)

    threading.Thread(target=run, name="haloflow-synthetic-lease", daemon=True).start()
    return future


def _backend_present(conn: Any, pid: int | None) -> bool:
    if pid is None:
        return False
    row = conn.execute("SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE pid = %s",
                       (pid,)).fetchone()
    return bool(row is not None and int(row[0]) > 0)


def _fake_sender() -> Any:
    from haloflow.m05.outbound import FakeSender

    return FakeSender()


def _simulated_ehr() -> Any:
    from datetime import UTC, datetime

    from haloflow.m05.simulated_ehr import SimulatedEhr

    return SimulatedEhr(clock=lambda: datetime.now(UTC))
