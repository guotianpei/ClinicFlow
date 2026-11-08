"""L-1S package S-G1b — test-side synthetic application harness, unit rows (no database).

Governing: S-G1b plan v1 + v1r1 (Work Session 2026-11-07), Codex ALIGN for tests
20261107T182348Z (`codex_sg1b-plan-v1r1-review.md`), placement option A
(`codex_sg1-placement-resolution.md`). Test-side harness evidence only: NOT an
installable application, NOT production-lifespan coverage; L-1S stays open. Every
harness import is deferred into the row body (class-B red rule).

PINNED INTERFACE (`sg_harness`, a test-side module in tests/m05):
- `SyntheticConfigInvalid(field)`: `.code == "SYNTHETIC_CONFIG_INVALID"`, `.field`;
  `args == ("SYNTHETIC_CONFIG_INVALID", field)`; never carries a DSN, secret or token.
- `SyntheticConfig.build(*, deployment, runtime_dsn, migrator_dsn, provisioner_dsn,
  manifest_token, served_tenants, ingress_secrets, interval_s=300, environ=os.environ)`,
  pure (no I/O). First: `deployment is Deployment.PRODUCTION` ->
  `startup.StartupRefused("SYNTHETIC_IN_PRODUCTION")`. Then `SyntheticConfigInvalid`:
  `deployment` (not exactly Deployment); `environment` (any of ROUTING_ENV set in
  `environ`); `database` (a DSN that does not parse, has a key outside {host, port,
  dbname, user, password}, lacks host/port/dbname/user, has a multi-host or empty host,
  a non-numeric port, host/port/dbname differing between the three DSNs, or a dbname
  not starting with `haloflow_test`); `manifest_token` (not a canonical lower-case
  UUID string); `served_tenants` (not a non-empty tuple of unique exact str);
  `ingress_secrets` (IngressConfig.build refuses; the IngressConfigInvalid code is not
  exposed); `interval_s` (not exactly int in [60, 450]). Only user and password may
  differ between the DSNs. `repr` is `SyntheticConfig(clinics=<n>)`.
- `ROUTING_ENV` = {PGHOST, PGHOSTADDR, PGPORT, PGDATABASE, PGSERVICE, PGSERVICEFILE,
  PGOPTIONS, PGTARGETSESSIONATTRS, PGLOADBALANCEHOSTS}.
- `Identity(database: str, port: int, manifest: tuple[tuple[str, tuple[str, ...]], ...])`.
- `check_identity(config, runtime: Identity, migrator: Identity) -> None`: for BOTH
  identities: database == the configured dbname, port == the configured port, exactly
  one manifest row, its token == config.manifest_token, its tenants (no duplicates)
  equal served_tenants as a set; else `StartupRefused("SYNTHETIC_DATABASE_UNBOUND")`.
- `async read_identity(dsn, *, connect) -> Identity`: ONE connection from `await
  connect(dsn)`; one `execute(IDENTITY_SQL)` then `fetchall()`; any failure ->
  `StartupRefused("SYNTHETIC_DATABASE_UNBOUND")` (no chained text); the connection is
  closed on every path (success, refusal, cancellation; cancellation propagates).
- `forbid_provisioning() -> factory`: an async factory that always raises
  `StartupRefused("SYNTHETIC_PROVISIONING_FORBIDDEN")`.
- `classify_readiness(error: BaseException | None) -> str`: None -> "active";
  `ActivationRefused(reason_code="M05_NOT_ACTIVATED")` -> "inactive"; anything else
  (other activation codes included) -> `StartupRefused("SYNTHETIC_TENANT_NOT_READY")`.
- `build_static_plan(registry) -> startup._StaticPlan`: a mirror of
  `startup._build_static_plan` over any registry (supported = {target}).
- `async close_all(closers: list) -> bool`: the `platform.app._close_all` semantics.
- `ShutdownFailed(code)`: `.code`; codes SCHEDULER_STOP_TIMEOUT (stop raised
  `SchedulerStopFailed`), SCHEDULER_STOP_UNVERIFIED (stop raised anything else, or
  returned with state not STOPPED), SCHEDULER_FACTORY_UNVERIFIED (the factory call
  created asyncio tasks, or returned a scheduler whose state is not NEW).
- `SyntheticSteps` (frozen dataclass), awaited in this order, each once:
  1 `guard(config)`; 2 `open_tenant_pool(config) -> (pool, close)`;
  3 `verify_tenants(config, pool) -> validated`;
  4 `build_services(config, pool, validated) -> services`;
  5 `readiness(config, services) -> Mapping[str, str]` (published as
  `app.state.activation`; the opened tenant and admission pool objects are published
  as `app.state.pools` = {"tenant": ..., "admission": ...} and set to None on
  unpublish); 6 `open_admission_pool(config) -> (pool, close)`;
  7 `open_keyset(config) -> (keyset, close)`; 8 `build_verifier(config, keyset)`;
  9 `build_admission(admission_pool)`. Then publish
  `app.state.platform = PlatformServices(services, admission, verifier)`, then
  `scheduler = scheduler_factory(services.runner, config.served_tenants,
  config.interval_s)` (synchronous; if the call created asyncio tasks or the returned
  state is not NEW, the scheduler is neither started nor stopped and the exit is
  SCHEDULER_FACTORY_UNVERIFIED), then `scheduler.start()`, then yield.
  `services` exposes `runner`, `reply` (`.receive(context, message)`) and
  `ingress_context(tenant_id)` (async).
- `create_synthetic_app(config, *, steps, scheduler_factory) -> FastAPI`: a fresh
  `platform.app.create_app()` (no I/O) whose `router.lifespan_context` is the harness
  lifespan, with the S-G1a ingress router included (context_for ->
  services.ingress_context, receive -> services.reply.receive, IngressConfig from
  config.ingress_secrets). No other change to the production app.
- Lifespan exits (L1 quiescence rule). Quiescent = `scheduler.stop()` returned AND
  `scheduler.state is SchedulerState.STOPPED`. Every exit once a scheduler object
  exists: unpublish (`app.state.platform = None`), then the bounded stop in its own task
  awaited until done even if the caller is cancelled meanwhile; quiescent -> close all
  in reverse, then re-raise the original (none on a normal exit); not quiescent ->
  close NOTHING, `app.state.retained` = the closers (in open order), ONE ERROR line
  `SHUTDOWN_FAILED reason_code=<code>` on logger `haloflow.synthetic`, raise
  `ShutdownFailed(code)` with `__cause__` = the original (None on a normal exit).
  Before a scheduler exists: close what was opened, re-raise (close_all semantics).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

TENANT_A = "clinic-sg-a"
TENANT_B = "clinic-sg-b"
SECRET_A = "a" * 31 + "Z" + "-synthetic-ingress-A"
SECRET_B = "b" * 31 + "Y" + "-synthetic-ingress-B"
TOKEN = "3f1c2b9e-6d0a-4c55-9a51-0b7e5d2c8a11"
DB = "haloflow_test_sg1b"
RUNTIME = f"host=db.local port=5432 dbname={DB} user=rt password=p1"
MIGRATOR = f"host=db.local port=5432 dbname={DB} user=mig password=p2"
PROVISIONER = f"host=db.local port=5432 dbname={DB} user=prov password=p3"
WATCHDOG = 5.0
STOP_WAIT = 0.2
BACKSTOP = 120.0  # process-level backstop (faulthandler), seconds per row
LOGGER = "haloflow.synthetic"


def _mod() -> Any:
    import sg_harness as module

    return module


def _deployment(name: str = "NON_PRODUCTION") -> Any:
    from haloflow.startup import Deployment

    return Deployment[name]


def _config(**overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        deployment=_deployment(), runtime_dsn=RUNTIME, migrator_dsn=MIGRATOR,
        provisioner_dsn=PROVISIONER, manifest_token=TOKEN,
        served_tenants=(TENANT_A, TENANT_B),
        ingress_secrets={TENANT_A: SECRET_A, TENANT_B: SECRET_B}, environ={})
    values.update(overrides)
    return _mod().SyntheticConfig.build(**values)


# --- G-U06 / G-U08 configuration ------------------------------------------------------------


def test_sg_u06_production_refused_first() -> None:
    from haloflow.startup import StartupRefused

    with pytest.raises(StartupRefused) as caught:
        _config(deployment=_deployment("PRODUCTION"), runtime_dsn="not a dsn",
                served_tenants=())
    assert caught.value.reason_code == "SYNTHETIC_IN_PRODUCTION"
    assert caught.value.tenant_index is None


def _dsn(**parts: str) -> str:
    base = {"host": "db.local", "port": "5432", "dbname": DB, "user": "rt", "password": "p1"}
    base.update(parts)
    return " ".join(f"{k}={v}" for k, v in base.items() if v != "<drop>")


BAD_CONFIG: dict[str, tuple[dict[str, Any], str]] = {
    "deployment-str": ({"deployment": "non_production"}, "deployment"),
    "deployment-none": ({"deployment": None}, "deployment"),
    "dsn-unparsable": ({"runtime_dsn": "host='unterminated"}, "database"),
    "dsn-hostaddr": ({"runtime_dsn": _dsn(hostaddr="10.0.0.9")}, "database"),
    "dsn-service": ({"migrator_dsn": _dsn(user="mig", service="other")}, "database"),
    "dsn-options": ({"runtime_dsn": _dsn(options="-csearch_path=x")}, "database"),
    "dsn-sslmode": ({"runtime_dsn": _dsn(sslmode="disable")}, "database"),
    "dsn-multi-host": ({"runtime_dsn": _dsn(host="db.local,db2.local")}, "database"),
    "dsn-empty-host": ({"runtime_dsn": _dsn(host="''")}, "database"),
    "dsn-no-host": ({"runtime_dsn": _dsn(host="<drop>")}, "database"),
    "dsn-no-port": ({"runtime_dsn": _dsn(port="<drop>")}, "database"),
    "dsn-no-dbname": ({"runtime_dsn": _dsn(dbname="<drop>")}, "database"),
    "dsn-no-user": ({"runtime_dsn": _dsn(user="<drop>")}, "database"),
    "dsn-port-text": ({"runtime_dsn": _dsn(port="54x2")}, "database"),
    "dsn-port-list": ({"runtime_dsn": _dsn(port="5432,5433")}, "database"),
    "host-differs": ({"migrator_dsn": _dsn(user="mig", host="db2.local")}, "database"),
    "port-differs": ({"provisioner_dsn": _dsn(user="prov", port="5433")}, "database"),
    "dbname-differs": ({"migrator_dsn": _dsn(user="mig", dbname=DB + "_x")}, "database"),
    "dbname-prefix": ({"runtime_dsn": _dsn(dbname="haloflow_dev"),
                       "migrator_dsn": _dsn(user="mig", dbname="haloflow_dev"),
                       "provisioner_dsn": _dsn(user="prov", dbname="haloflow_dev")},
                      "database"),
    "env-hostaddr": ({"environ": {"PGHOSTADDR": "10.0.0.9"}}, "environment"),
    "env-service": ({"environ": {"PGSERVICE": "x"}}, "environment"),
    "env-servicefile": ({"environ": {"PGSERVICEFILE": "/tmp/x"}}, "environment"),
    "env-host": ({"environ": {"PGHOST": "elsewhere"}}, "environment"),
    "env-port": ({"environ": {"PGPORT": "6543"}}, "environment"),
    "env-database": ({"environ": {"PGDATABASE": "haloflow_dev"}}, "environment"),
    "env-options": ({"environ": {"PGOPTIONS": "-c x=y"}}, "environment"),
    "env-target": ({"environ": {"PGTARGETSESSIONATTRS": "any"}}, "environment"),
    "env-loadbalance": ({"environ": {"PGLOADBALANCEHOSTS": "random"}}, "environment"),
    "token-not-uuid": ({"manifest_token": "token"}, "manifest_token"),
    "token-upper": ({"manifest_token": TOKEN.upper()}, "manifest_token"),
    "token-uuid-obj": ({"manifest_token": uuid.UUID(TOKEN)}, "manifest_token"),
    "tenants-empty": ({"served_tenants": ()}, "served_tenants"),
    "tenants-list": ({"served_tenants": [TENANT_A]}, "served_tenants"),
    "tenants-dup": ({"served_tenants": (TENANT_A, TENANT_A),
                     "ingress_secrets": {TENANT_A: SECRET_A}}, "served_tenants"),
    "tenants-non-str": ({"served_tenants": (TENANT_A, 7)}, "served_tenants"),
    "secrets-unserved": ({"served_tenants": (TENANT_A,)}, "ingress_secrets"),
    "secrets-short": ({"ingress_secrets": {TENANT_A: "x" * 31}}, "ingress_secrets"),
    "interval-59": ({"interval_s": 59}, "interval_s"),
    "interval-451": ({"interval_s": 451}, "interval_s"),
    "interval-bool": ({"interval_s": True}, "interval_s"),
    "interval-float": ({"interval_s": 300.0}, "interval_s"),
    "interval-str": ({"interval_s": "300"}, "interval_s"),
}


@pytest.mark.parametrize("case", sorted(BAD_CONFIG))
def test_sg_u08_config_refusals(case: str) -> None:
    module = _mod()
    overrides, expected = BAD_CONFIG[case]
    with pytest.raises(module.SyntheticConfigInvalid) as caught:
        _config(**overrides)
    error = caught.value
    assert (error.code, error.field) == ("SYNTHETIC_CONFIG_INVALID", expected)
    assert error.args == ("SYNTHETIC_CONFIG_INVALID", expected)
    for secret in ("p1", "p2", "p3", SECRET_A, TOKEN, "db.local"):
        assert secret not in str(error) and secret not in repr(error)


def test_sg_u08_valid_config_and_redacted_repr() -> None:
    module = _mod()
    assert frozenset({
        "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE",
        "PGOPTIONS", "PGTARGETSESSIONATTRS", "PGLOADBALANCEHOSTS"}) == module.ROUTING_ENV
    config = _config(environ={"PGUSER": "ignored", "PGPASSWORD": "ignored", "HOME": "/x"})
    assert repr(config) == str(config) == "SyntheticConfig(clinics=2)"
    assert config.served_tenants == (TENANT_A, TENANT_B) and config.interval_s == 300
    assert config.manifest_token == TOKEN
    for bound in (60, 450):
        assert _config(interval_s=bound).interval_s == bound
    url = _config(runtime_dsn=f"postgresql://rt:p1@db.local:5432/{DB}")
    assert url.served_tenants == (TENANT_A, TENANT_B)  # URL form, same routing


# --- G-U08b identity guard ----------------------------------------------------------------------


def _identity(**overrides: Any) -> Any:
    values: dict[str, Any] = dict(database=DB, port=5432,
                                  manifest=((TOKEN, (TENANT_A, TENANT_B)),))
    values.update(overrides)
    return _mod().Identity(**values)


IDENTITY_MISSES: dict[str, dict[str, Any]] = {
    "database": {"database": "haloflow_dev"},
    "port": {"port": 5433},
    "no-row": {"manifest": ()},
    "two-rows": {"manifest": ((TOKEN, (TENANT_A, TENANT_B)), (TOKEN, (TENANT_A,)))},
    "token": {"manifest": (("00000000-0000-4000-8000-000000000001", (TENANT_A, TENANT_B)),)},
    "tenants-subset": {"manifest": ((TOKEN, (TENANT_A,)),)},
    "tenants-extra": {"manifest": ((TOKEN, (TENANT_A, TENANT_B, "clinic-sg-c")),)},
    "tenants-dup": {"manifest": ((TOKEN, (TENANT_A, TENANT_B, TENANT_B)),)},
}


@pytest.mark.parametrize("side", ["runtime", "migrator"])
@pytest.mark.parametrize("miss", sorted(IDENTITY_MISSES))
def test_sg_u08b_identity_checked_on_both_connections(side: str, miss: str) -> None:
    from haloflow.startup import StartupRefused

    module = _mod()
    good, bad = _identity(), _identity(**IDENTITY_MISSES[miss])
    runtime, migrator = (bad, good) if side == "runtime" else (good, bad)
    with pytest.raises(StartupRefused) as caught:
        module.check_identity(_config(), runtime, migrator)
    assert caught.value.reason_code == "SYNTHETIC_DATABASE_UNBOUND"
    assert caught.value.tenant_index is None
    assert TOKEN not in str(caught.value) and DB not in str(caught.value)


def test_sg_u08b_identity_match_passes() -> None:
    module = _mod()
    reordered = _identity(manifest=((TOKEN, (TENANT_B, TENANT_A)),))
    assert module.check_identity(_config(), _identity(), reordered) is None


class _Conn:
    def __init__(self, rows: Any = None, error: BaseException | None = None,
                 hang: bool = False) -> None:
        self.rows, self.error, self.hang = rows, error, hang
        self.executed: list[str] = []
        self.closed = 0

    async def execute(self, query: str, *_: Any) -> _Conn:
        self.executed.append(query)
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return self

    async def fetchall(self) -> Any:
        return self.rows

    async def close(self) -> None:
        self.closed += 1


def _connector(conn: _Conn, seen: list[str]) -> Callable[[str], Any]:
    async def connect(dsn: str) -> _Conn:
        seen.append(dsn)
        return conn

    return connect


async def test_sg_u08b_read_identity_closes_on_every_path() -> None:
    from haloflow.startup import StartupRefused

    module = _mod()
    seen: list[str] = []
    for conn in (_Conn(error=RuntimeError("relation missing: secret-text")),
                 _Conn(rows="not rows"), _Conn(rows=[(1, 2)])):
        with pytest.raises(StartupRefused) as caught:
            await module.read_identity(RUNTIME, connect=_connector(conn, seen))
        assert caught.value.reason_code == "SYNTHETIC_DATABASE_UNBOUND"
        assert caught.value.__cause__ is None and "secret-text" not in str(caught.value)
        assert conn.closed == 1 and conn.executed == [module.IDENTITY_SQL]
    hanging = _Conn(hang=True)
    task = asyncio.ensure_future(module.read_identity(RUNTIME,
                                                     connect=_connector(hanging, seen)))
    for _ in range(5):
        await asyncio.sleep(0)
    assert hanging.executed == [module.IDENTITY_SQL]  # parked inside the query
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=WATCHDOG)  # independent observation
    assert done and task.cancelled()
    assert hanging.closed == 1
    assert seen == [RUNTIME] * 4

    async def refuse(_dsn: str) -> Any:
        raise OSError("connection refused: secret-text")

    with pytest.raises(StartupRefused) as caught:
        await module.read_identity(RUNTIME, connect=refuse)
    assert caught.value.reason_code == "SYNTHETIC_DATABASE_UNBOUND"


async def test_sg_u08c_provisioning_is_forbidden() -> None:
    from haloflow.startup import StartupRefused

    factory = _mod().forbid_provisioning()
    for _ in range(2):
        with pytest.raises(StartupRefused) as caught:
            await factory()
        assert caught.value.reason_code == "SYNTHETIC_PROVISIONING_FORBIDDEN"


# --- G-U13 readiness classification -------------------------------------------------------------


def test_sg_u13_readiness_classification() -> None:
    from haloflow.m01.errors import ActivationRefused
    from haloflow.startup import StartupRefused

    module = _mod()
    assert module.classify_readiness(None) == "active"
    assert module.classify_readiness(
        ActivationRefused(reason_code="M05_NOT_ACTIVATED")) == "inactive"
    for error in (ActivationRefused(reason_code="M05_ACTIVATION_UNREADABLE"),
                  ActivationRefused(reason_code="ACTIVATION_ISOLATION_UNSUPPORTED"),
                  ActivationRefused(), RuntimeError("M05_NOT_ACTIVATED"),
                  TimeoutError()):
        with pytest.raises(StartupRefused) as caught:
            module.classify_readiness(error)
        assert caught.value.reason_code == "SYNTHETIC_TENANT_NOT_READY"


# --- G-U12 static plan mirror -------------------------------------------------------------------


def test_sg_u12_static_plan_mirror_parity() -> None:
    import sc_support as sc

    from haloflow import composition, startup

    module = _mod()
    production = composition.build_production_tenant_migrations()
    assert module.build_static_plan(production) == startup._build_static_plan()
    plan46 = module.build_static_plan(sc.sc_registry())
    assert plan46.supported == frozenset({46})
    names = {check.profile.function_name for check in plan46.functions}
    production_names = {c.profile.function_name for c in startup._build_static_plan().functions}
    assert production_names < names
    assert {"m05_directory_put", "m05_register_inbound", "m05_due_windows",
            "m05_activation_probe", "m05_activation_change"} <= names


# --- G-U14 close-all parity ----------------------------------------------------------------------


SCRIPTS: dict[str, tuple[str, ...]] = {
    "ok": ("ok", "ok", "ok"),
    "one-fails": ("ok", "fail", "ok"),
    "one-cancels": ("ok", "cancel", "ok"),
    "fail-and-cancel": ("fail", "ok", "cancel"),
    "empty": (),
}


async def _run_close(helper: Any, script: tuple[str, ...]) -> tuple[list[int], str]:
    order: list[int] = []

    def closer(index: int, kind: str) -> Any:
        async def close() -> None:
            order.append(index)
            if kind == "fail":
                raise RuntimeError("close failed")
            if kind == "cancel":
                raise asyncio.CancelledError()

        return close

    closers = [closer(i, kind) for i, kind in enumerate(script)]
    try:
        outcome = "failed" if await helper(closers) else "clean"
    except asyncio.CancelledError:
        outcome = "cancelled"
    assert closers == []
    return order, outcome


@pytest.mark.parametrize("script", sorted(SCRIPTS))
async def test_sg_u14_close_all_parity(script: str) -> None:
    from haloflow.platform.app import _close_all

    mirror = await _run_close(_mod().close_all, SCRIPTS[script])
    assert mirror == await _run_close(_close_all, SCRIPTS[script])
    assert mirror[0] == list(reversed(range(len(SCRIPTS[script]))))


# --- lifespan rig -------------------------------------------------------------------------------
#
# Watchdog discipline (Codex d568580 review item 1): the harness deliberately keeps
# awaiting a shielded stop when its caller is cancelled, so `asyncio.timeout` or
# `wait_for` around the lifespan is NOT an independent deadline. Every row runs the
# lifespan in its own task (`_Life`) and observes it only through `asyncio.wait(...,
# timeout=...)`, which never cancels the observed task. Cleanup releases fakes
# deterministically in `finally`; a process-level backstop (`faulthandler`, module
# fixture) ends the run if a row still hangs. G-U07w proves the watchdog against a
# stop that never returns.


@pytest.fixture(autouse=True)
def _process_backstop() -> Any:
    import faulthandler

    faulthandler.dump_traceback_later(BACKSTOP, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@dataclass
class _Journal:
    events: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    # what each closer observed at the moment it ran: (name, unpublished, scheduler state)
    seen_at_close: list[tuple[str, bool, Any]] = field(default_factory=list)
    app: Any = None
    holder: dict[str, Any] = field(default_factory=dict)


class _Spy:
    def __init__(self) -> None:
        self.contexts: list[str] = []
        self.received: list[Any] = []

    async def ingress_context(self, tenant_id: str) -> Any:
        self.contexts.append(tenant_id)
        return ("ctx", tenant_id)

    async def receive(self, context: Any, message: Any) -> Any:
        self.received.append((context, message))
        from haloflow.m05.intake import InboundResult

        return InboundResult(outcome="registered", inbound_id=uuid.UUID(int=7),
                             decision="item", triage_kind="unclear_reply",
                             patient_id=uuid.UUID(int=8), event_id=uuid.UUID(int=9),
                             item_id=uuid.UUID(int=10))


@dataclass
class _Services:
    spy: _Spy
    runner: object = field(default_factory=object)

    @property
    def reply(self) -> _Spy:
        return self.spy

    async def ingress_context(self, tenant_id: str) -> Any:
        return await self.spy.ingress_context(tenant_id)


class _Verifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, _token: str) -> Any:
        self.calls += 1
        from haloflow.platform.verify import AuthRefused

        raise AuthRefused("UNAUTHENTICATED")


def _steps(journal: _Journal, spy: _Spy, *, fail_at: str | None = None,
           verifier: _Verifier | None = None) -> Any:
    module = _mod()

    def step(name: str, result: Any = None, closes: bool = False) -> Any:
        async def run(*_: Any) -> Any:
            journal.events.append(name)
            if fail_at == name:
                raise RuntimeError(f"fault-{name}")
            if closes:
                async def close() -> None:
                    sched = journal.holder.get("s")
                    journal.seen_at_close.append(
                        (name, journal.app.state.platform is None,
                         None if sched is None else sched.state))
                    journal.closed.append(name)

                return (f"res:{name}", close)
            return result

        return run

    return module.SyntheticSteps(
        guard=step("guard"),
        open_tenant_pool=step("tenant_pool", closes=True),
        verify_tenants=step("verify", result=()),
        build_services=step("services", result=_Services(spy)),
        readiness=step("readiness", result={TENANT_A: "active", TENANT_B: "inactive"}),
        open_admission_pool=step("admission_pool", closes=True),
        open_keyset=step("keyset", closes=True),
        build_verifier=step("verifier", result=verifier or _Verifier()),
        build_admission=step("admission", result=object()),
    )


STEP_ORDER = ["guard", "tenant_pool", "verify", "services", "readiness", "admission_pool",
              "keyset", "verifier", "admission"]
OPENED = ["tenant_pool", "admission_pool", "keyset"]


class _Sched:
    """A duck-typed scheduler using the real SchedulerState values.

    Stop modes: ok; parked (stop waits on `stop_gate`); self-cancelled (the stop raises
    CancelledError of its own); cancel-caller (the stop completes STOPPED and cancels the
    lifespan task in the same step); stubborn (the work ignores
    cancellation, stop times out after STOP_WAIT -> STOP_FAILED); hang (stop never
    returns until released, ignoring cancellation); raise-other; lie (returns while
    RUNNING). Start modes: ok; raise; task-then-raise; stubborn-then-raise.
    Worker-entry synchronisation (review item 3): the work sets `entered` as its first
    action, and this fake's stop waits for `entered` (bounded) before cancelling, so a
    cancellation can never land before the coroutine starts and fake a clean stop.
    """

    def __init__(self, journal: _Journal, *, stop: str = "ok", start: str = "ok") -> None:
        from haloflow.m05.due_scheduler import SchedulerState

        self.S = SchedulerState
        self.journal, self.stop_mode, self.start_mode = journal, stop, start
        self.state = SchedulerState.NEW
        self.task: asyncio.Task[None] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.stop_gate = asyncio.Event()
        self.stop_entered = asyncio.Event()
        self.stop_calls = 0
        self.caller: asyncio.Task[None] | None = None

    def _stubborn(self) -> bool:
        return self.start_mode == "stubborn-then-raise" or self.stop_mode == "stubborn"

    def start(self) -> None:
        self.journal.events.append("scheduler.start")
        if self.start_mode == "raise":
            raise RuntimeError("start failed")
        self.task = asyncio.get_running_loop().create_task(self._work())
        self.state = self.S.RUNNING
        if self.start_mode in ("task-then-raise", "stubborn-then-raise"):
            raise RuntimeError("start failed after creating work")

    async def _work(self) -> None:
        self.entered.set()
        if self._stubborn():
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue  # ignores cancellation until released
            return
        await asyncio.Event().wait()

    async def stop(self) -> None:
        from haloflow.m05.due_scheduler import SchedulerStopFailed

        self.stop_calls += 1
        self.journal.events.append("scheduler.stop")
        self.stop_entered.set()
        if self.stop_mode == "raise-other":
            raise RuntimeError("stop broke")
        if self.stop_mode == "self-cancelled":
            raise asyncio.CancelledError()  # the stop itself was cancelled independently
        if self.stop_mode == "lie":
            return  # returns while still RUNNING
        if self.stop_mode == "parked":
            await self.stop_gate.wait()
        if self.stop_mode == "hang":
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
        if self.task is not None:
            await asyncio.wait({asyncio.ensure_future(self.entered.wait())}, timeout=STOP_WAIT)
            assert self.entered.is_set(), "worker never started"
            self.task.cancel()
            done, _ = await asyncio.wait({self.task}, timeout=STOP_WAIT)
            if not done:
                self.state = self.S.STOP_FAILED
                raise SchedulerStopFailed()
        self.state = self.S.STOPPED
        if self.stop_mode == "cancel-caller" and self.caller is not None:
            self.caller.cancel()  # coincident: the caller is cancelled as the stop completes

    async def terminate(self) -> bool:
        """Test hygiene: end retained work; True when the work task is gone."""

        self.release.set()
        self.stop_gate.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.wait({self.task}, timeout=WATCHDOG)
        return self.task is None or self.task.done()


@dataclass
class _Rig:
    journal: _Journal
    spy: _Spy
    app: Any
    verifier: _Verifier

    @property
    def sched(self) -> _Sched:
        sched: _Sched = self.journal.holder["s"]
        return sched


def _rig(*, fail_at: str | None = None, stop: str = "ok", start: str = "ok",
         factory: Callable[..., Any] | None = None) -> _Rig:
    module = _mod()
    journal, spy, verifier = _Journal(), _Spy(), _Verifier()

    def default_factory(_runner: Any, tenants: tuple[str, ...], interval: int) -> _Sched:
        journal.events.append(f"factory:{tenants}:{interval}")
        sched = _Sched(journal, stop=stop, start=start)
        journal.holder["s"] = sched
        return sched

    app = module.create_synthetic_app(_config(), steps=_steps(journal, spy, fail_at=fail_at,
                                                               verifier=verifier),
                                      scheduler_factory=factory or default_factory)
    journal.app = app
    return _Rig(journal, spy, app, verifier)


class _Life:
    """The lifespan in its own task, observed only with asyncio.wait(timeout=...)."""

    def __init__(self, app: Any, *, thrown: BaseException | None = None) -> None:
        self.app, self.thrown = app, thrown
        self.up, self.down = asyncio.Event(), asyncio.Event()
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        async with self.app.router.lifespan_context(self.app):
            self.up.set()
            await self.down.wait()
            if self.thrown is not None:
                raise self.thrown

    async def started(self, timeout: float = WATCHDOG) -> bool:
        waiter = asyncio.ensure_future(self.up.wait())
        await asyncio.wait({waiter, self.task}, timeout=timeout,
                           return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        return self.up.is_set() and not self.task.done()

    def shut(self) -> None:
        self.down.set()

    async def settled(self, timeout: float = WATCHDOG) -> bool:
        done, _ = await asyncio.wait({self.task}, timeout=timeout)
        return bool(done)

    async def abandon(self) -> None:
        """Last-resort hygiene after the assertions: never awaits without a bound."""

        if not self.task.done():
            self.task.cancel()
            await asyncio.wait({self.task}, timeout=WATCHDOG)
        if self.task.done() and not self.task.cancelled():
            self.task.exception()  # retrieved


async def _until(event: asyncio.Event) -> bool:
    waiter = asyncio.ensure_future(event.wait())
    done, _ = await asyncio.wait({waiter}, timeout=WATCHDOG)
    if not done:
        waiter.cancel()
    return event.is_set()


@asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        yield client


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno >= logging.ERROR]


def _quiet_close(rig: _Rig) -> list[tuple[str, bool, Any]]:
    """Every closer ran after unpublish and after the scheduler was STOPPED."""

    stopped = rig.sched.S.STOPPED
    return [(name, True, stopped) for name in reversed(OPENED)]


# --- G-U07 lifecycle ----------------------------------------------------------------------------


async def test_sg_u07w_watchdog_is_independent_of_a_non_returning_stop() -> None:
    rig = _rig(stop="hang")
    life = _Life(rig.app)
    try:
        assert await life.started()
        life.shut()
        assert await _until(rig.sched.stop_entered)
        life.task.cancel()  # the harness shields the stop: this cancellation is absorbed
        assert not await life.settled(timeout=0.3)  # observed, not hung: still parked
        assert rig.journal.closed == []  # nothing closed beneath a live stop
        rig.sched.release.set()  # the stop now returns and quiesces
        assert await life.settled()
        assert life.task.cancelled()  # the absorbed cancellation is re-raised after close
        assert rig.journal.seen_at_close == _quiet_close(rig)
    finally:
        await rig.sched.terminate()
        await life.abandon()


async def test_sg_u07a_normal_start_and_stop_order(caplog: pytest.LogCaptureFixture) -> None:
    rig = _rig()
    life = _Life(rig.app)
    try:
        assert await life.started()
        assert rig.journal.events == [*STEP_ORDER, f"factory:{(TENANT_A, TENANT_B)}:300",
                                      "scheduler.start"]
        platform = rig.app.state.platform
        assert platform is not None and platform.verifier is rig.verifier
        assert dict(rig.app.state.activation) == {TENANT_A: "active", TENANT_B: "inactive"}
        assert dict(rig.app.state.pools) == {"tenant": "res:tenant_pool",
                                             "admission": "res:admission_pool"}
        rig.journal.events.clear()
        life.shut()
        assert await life.settled()
        assert life.task.exception() is None
        assert rig.journal.events == ["scheduler.stop"]
        assert rig.journal.seen_at_close == _quiet_close(rig)
        assert rig.app.state.platform is None and rig.app.state.pools is None
        assert _errors(caplog) == []
    finally:
        await rig.sched.terminate()
        await life.abandon()


@pytest.mark.parametrize("fail_at", STEP_ORDER)
async def test_sg_u07_startup_failure_before_scheduler(fail_at: str) -> None:
    rig = _rig(fail_at=fail_at)
    life = _Life(rig.app)
    try:
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, RuntimeError) and str(error) == f"fault-{fail_at}"
        opened = [n for n in OPENED if STEP_ORDER.index(n) < STEP_ORDER.index(fail_at)]
        assert rig.journal.closed == list(reversed(opened))
        assert all(seen == (name, True, None) for seen, name in
                   zip(rig.journal.seen_at_close, reversed(opened), strict=True))
        assert rig.app.state.platform is None and "s" not in rig.journal.holder
    finally:
        await life.abandon()


async def test_sg_u07b_stop_timeout_retains_everything(
        caplog: pytest.LogCaptureFixture) -> None:
    module = _mod()
    rig = _rig(stop="stubborn")
    life = _Life(rig.app)
    try:
        assert await life.started()
        assert await _until(rig.sched.entered)
        life.shut()
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_STOP_TIMEOUT" and error.__cause__ is None
        assert rig.journal.closed == [] and rig.app.state.platform is None
        assert rig.sched.state is rig.sched.S.STOP_FAILED
        assert _errors(caplog) == ["SHUTDOWN_FAILED reason_code=SCHEDULER_STOP_TIMEOUT"]
        retained = list(rig.app.state.retained)
        assert len(retained) == 3
        assert rig.sched.task is not None and not rig.sched.task.done()  # still owned
        assert await rig.sched.terminate()  # end the work FIRST (review item 3)
        for close in retained:  # then the retained closers, in open order, still work
            await close()
        assert rig.journal.closed == OPENED
    finally:
        await rig.sched.terminate()
        await life.abandon()


async def test_sg_u07c_cancelled_during_completing_stop() -> None:
    rig = _rig(stop="parked")
    life = _Life(rig.app)
    try:
        assert await life.started()
        assert await _until(rig.sched.entered)
        life.shut()
        assert await _until(rig.sched.stop_entered)
        life.task.cancel()
        assert not await life.settled(timeout=0.2)  # the shielded stop is still awaited
        assert rig.journal.closed == []
        rig.sched.stop_gate.set()
        assert await life.settled()
        assert life.task.cancelled()
        assert rig.sched.state is rig.sched.S.STOPPED
        assert rig.journal.seen_at_close == _quiet_close(rig)
    finally:
        await rig.sched.terminate()
        await life.abandon()


async def test_sg_u07c2_cancelled_during_failing_stop(caplog: pytest.LogCaptureFixture) -> None:
    module = _mod()
    rig = _rig(stop="stubborn")
    life = _Life(rig.app)
    try:
        assert await life.started()
        assert await _until(rig.sched.entered)
        life.shut()
        assert await _until(rig.sched.stop_entered)
        life.task.cancel()
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_STOP_TIMEOUT"
        assert isinstance(error.__cause__, asyncio.CancelledError)
        assert rig.journal.closed == [] and len(rig.app.state.retained) == 3
        assert _errors(caplog) == ["SHUTDOWN_FAILED reason_code=SCHEDULER_STOP_TIMEOUT"]
    finally:
        await rig.sched.terminate()
        await life.abandon()


async def test_sg_u07c3_error_thrown_into_lifetime_is_reraised_after_stop() -> None:
    rig = _rig()
    life = _Life(rig.app, thrown=LookupError("thrown"))
    try:
        assert await life.started()
        life.shut()
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, LookupError) and str(error) == "thrown"
        assert rig.journal.events[-1] == "scheduler.stop"
        assert rig.journal.seen_at_close == _quiet_close(rig)
    finally:
        await rig.sched.terminate()
        await life.abandon()


@pytest.mark.parametrize("mode", ["raise", "task-then-raise"])
async def test_sg_u07d_start_failure_stops_then_closes(mode: str) -> None:
    rig = _rig(start=mode)
    life = _Life(rig.app)
    try:
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, RuntimeError) and str(error).startswith("start failed")
        assert not life.up.is_set()
        sched = rig.sched
        assert sched.stop_calls == 1 and sched.state is sched.S.STOPPED
        if mode == "task-then-raise":
            assert sched.entered.is_set() and sched.task is not None and sched.task.done()
        assert rig.journal.seen_at_close == _quiet_close(rig)
    finally:
        await rig.sched.terminate()
        await life.abandon()


async def test_sg_u07d2_start_failure_with_stubborn_work_retains(
        caplog: pytest.LogCaptureFixture) -> None:
    module = _mod()
    rig = _rig(start="stubborn-then-raise")
    life = _Life(rig.app)
    try:
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_STOP_TIMEOUT"
        assert isinstance(error.__cause__, RuntimeError)
        assert str(error.__cause__) == "start failed after creating work"
        assert rig.sched.entered.is_set()  # the timeout is real, not a pre-start cancel
        assert rig.journal.closed == [] and len(rig.app.state.retained) == 3
        assert _errors(caplog) == ["SHUTDOWN_FAILED reason_code=SCHEDULER_STOP_TIMEOUT"]
    finally:
        await rig.sched.terminate()
        await life.abandon()


@pytest.mark.parametrize("mode", ["raise-other", "lie"])
async def test_sg_u07ef_unverified_stop_retains(mode: str,
                                                caplog: pytest.LogCaptureFixture) -> None:
    module = _mod()
    rig = _rig(stop=mode)
    life = _Life(rig.app)
    try:
        assert await life.started()
        assert await _until(rig.sched.entered)
        life.shut()
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_STOP_UNVERIFIED" and error.__cause__ is None
        assert rig.journal.closed == [] and len(rig.app.state.retained) == 3
        assert _errors(caplog) == ["SHUTDOWN_FAILED reason_code=SCHEDULER_STOP_UNVERIFIED"]
    finally:
        await rig.sched.terminate()
        await life.abandon()


async def test_sg_u07g_factory_that_spawns_work_is_unverified(
        caplog: pytest.LogCaptureFixture) -> None:
    module = _mod()
    spawned: list[asyncio.Task[bool]] = []
    box: dict[str, _Sched] = {}

    def spawning(_runner: Any, _tenants: tuple[str, ...], _interval: int) -> Any:
        spawned.append(asyncio.get_running_loop().create_task(asyncio.Event().wait()))
        box["s"] = _Sched(_Journal())
        return box["s"]

    rig = _rig(factory=spawning)
    life = _Life(rig.app)
    try:
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_FACTORY_UNVERIFIED"
        assert rig.journal.closed == [] and len(rig.app.state.retained) == 3
        assert rig.app.state.platform is None
        assert box["s"].journal.events == []  # neither started nor stopped
        assert set(rig.app.state.retained_tasks) == set(spawned)
        assert _errors(caplog) == ["SHUTDOWN_FAILED reason_code=SCHEDULER_FACTORY_UNVERIFIED"]
    finally:
        for task in spawned:
            task.cancel()
        if spawned:
            await asyncio.wait(spawned, timeout=WATCHDOG)
        await life.abandon()


async def test_sg_u07g2_factory_returning_started_scheduler_is_unverified() -> None:
    module = _mod()
    box: dict[str, _Sched] = {}

    def started(_runner: Any, _tenants: tuple[str, ...], _interval: int) -> Any:
        sched = _Sched(_Journal())
        sched.state = sched.S.RUNNING
        box["s"] = sched
        return sched

    rig = _rig(factory=started)
    life = _Life(rig.app)
    try:
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_FACTORY_UNVERIFIED"
        assert rig.journal.closed == [] and box["s"].journal.events == []
    finally:
        await life.abandon()


async def test_sg_u07g3_factory_raising_without_work_closes_and_reraises() -> None:
    def failing(_runner: Any, _tenants: tuple[str, ...], _interval: int) -> Any:
        raise LookupError("factory failed")

    rig = _rig(factory=failing)
    life = _Life(rig.app)
    try:
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, LookupError) and str(error) == "factory failed"
        assert rig.journal.closed == list(reversed(OPENED))
        assert [seen[:2] for seen in rig.journal.seen_at_close] == [
            (name, True) for name in reversed(OPENED)]
    finally:
        await life.abandon()


async def test_sg_u07g4_factory_spawning_then_raising_is_unverified(
        caplog: pytest.LogCaptureFixture) -> None:
    """F-H1 (Codex c50951c review): work created by a factory call that then raises is
    still live work; nothing is closed beneath it."""

    module = _mod()
    spawned: list[asyncio.Task[bool]] = []

    def spawn_then_raise(_runner: Any, _tenants: tuple[str, ...], _interval: int) -> Any:
        spawned.append(asyncio.get_running_loop().create_task(asyncio.Event().wait()))
        raise LookupError("factory failed after spawning")

    rig = _rig(factory=spawn_then_raise)
    life = _Life(rig.app)
    try:
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_FACTORY_UNVERIFIED"
        assert isinstance(error.__cause__, LookupError)
        assert rig.journal.closed == [] and len(rig.app.state.retained) == 3
        assert rig.app.state.platform is None
        assert spawned and not spawned[0].done()  # still owned: never closed beneath it
        owned = rig.app.state.retained_tasks  # the harness holds its OWN strong references
        assert set(owned) == set(spawned) and all(not task.done() for task in owned)
        assert _errors(caplog) == ["SHUTDOWN_FAILED reason_code=SCHEDULER_FACTORY_UNVERIFIED"]
    finally:
        for task in spawned:
            task.cancel()
        if spawned:
            await asyncio.wait(spawned, timeout=WATCHDOG)
        await life.abandon()


async def test_sg_u07h_independently_cancelled_stop_is_unverified(
        caplog: pytest.LogCaptureFixture) -> None:
    """F-H2: a stop that ends in its own CancelledError is not a verified stop."""

    module = _mod()
    rig = _rig(stop="self-cancelled")
    life = _Life(rig.app)
    try:
        assert await life.started()
        assert await _until(rig.sched.entered)
        life.shut()
        assert await life.settled()
        error = life.task.exception()
        assert isinstance(error, module.ShutdownFailed)
        assert error.code == "SCHEDULER_STOP_UNVERIFIED" and error.__cause__ is None
        assert rig.journal.closed == [] and len(rig.app.state.retained) == 3
        assert _errors(caplog) == ["SHUTDOWN_FAILED reason_code=SCHEDULER_STOP_UNVERIFIED"]
    finally:
        await rig.sched.terminate()
        await life.abandon()


async def test_sg_u07i_caller_cancel_coincident_with_stop_completion_is_kept() -> None:
    """F-H2: the caller is cancelled in the same step the stop completes; the
    cancellation must not be swallowed (closers run, then CancelledError propagates)."""

    rig = _rig(stop="cancel-caller")
    life = _Life(rig.app)
    try:
        assert await life.started()
        assert await _until(rig.sched.entered)
        rig.sched.caller = life.task
        life.shut()
        assert await life.settled()
        assert life.task.cancelled()
        assert rig.sched.state is rig.sched.S.STOPPED
        assert rig.journal.seen_at_close == _quiet_close(rig)
    finally:
        await rig.sched.terminate()
        await life.abandon()


# --- G-U10 / G-U11 routing and the real Gate ---------------------------------------------------


PRODUCTION_REQUESTS: list[tuple[str, str]] = [
    ("GET", "/healthz"), ("POST", "/v1/session/logout"), ("GET", "/v1/followup/items"),
    ("GET", f"/v1/followup/items/{uuid.UUID(int=1)}"),
    ("GET", f"/v1/followup/items/{uuid.UUID(int=1)}/history"),
    ("POST", f"/v1/followup/items/{uuid.UUID(int=1)}/actions"),
    ("POST", f"/v1/followup/items/{uuid.UUID(int=1)}/resolve"),
    ("GET", f"/v1/followup/notes/{uuid.UUID(int=1)}"), ("POST", "/v1/staff/display"),
    ("POST", "/v1/staff/admin"), ("GET", "/v1/unknown"), ("POST", "/v1/synthetic/x"),
]
INGRESS = "/v1/synthetic/inbound-sms"


def _production_app(verifier: _Verifier) -> Any:
    from haloflow.platform.app import StartupSteps, create_app

    async def opened(*_: Any) -> Any:
        async def close() -> None:
            return None

        return object(), close

    async def value(*_: Any) -> Any:
        return object()

    async def give_verifier(*_: Any) -> Any:
        return verifier

    steps = StartupSteps(open_tenant_pool=opened, start_services=value,
                         open_admission_pool=opened, open_keyset=opened,
                         build_verifier=give_verifier, build_admission=value)
    return create_app(config=object(), steps=steps)


async def test_sg_u10_same_routes_plus_exactly_the_ingress() -> None:
    from fastapi.routing import APIRoute

    from haloflow.platform.app import create_app

    production_routes = {(r.path, frozenset(r.methods or ())) for r in create_app().routes
                         if isinstance(r, APIRoute)}
    assert {path for path, _ in production_routes} >= {p for _, p in PRODUCTION_REQUESTS[:2]}
    rig = _rig()
    production = _production_app(_Verifier())
    prod_life, life = _Life(production), _Life(rig.app)
    try:
        assert await prod_life.started() and await life.started()
        async with _client(production) as prod, _client(rig.app) as harness:
            for method, path in PRODUCTION_REQUESTS:
                want = await prod.request(method, path, json={})
                got = await harness.request(method, path, json={})
                assert (got.status_code, got.content) == (want.status_code, want.content), path
            absent = await prod.post(INGRESS, json={})
            present = await harness.post(INGRESS, json={})
        assert absent.status_code == 404
        assert (present.status_code, present.json()) == (
            401, {"code": "INGRESS_UNAUTHENTICATED"})
        assert rig.spy.contexts == [] and rig.spy.received == []
        prod_life.shut()
        life.shut()
        assert await prod_life.settled() and await life.settled()
    finally:
        await rig.sched.terminate()
        await prod_life.abandon()
        await life.abandon()


def _ingress_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {SECRET_A}", "content-type": "application/json"}
    headers.update(extra or {})
    return headers


BODY = {"channel": "sms", "provider_message_id": "pm-1", "phone": "+15555550123",
        "text": "YES"}


async def _asgi(app: Any, method: str, path: str, headers: list[tuple[bytes, bytes]],
                body: bytes) -> tuple[int, bytes, int]:
    """Drive the ASGI app directly; returns (status, body, receive() calls)."""

    pending = [body]
    receives = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal receives
        receives += 1
        if pending:
            return {"type": "http.request", "body": pending.pop(), "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1),
             "server": ("t", 80), "root_path": "", "app": app}
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, out, receives


async def _both_refused(rig: _Rig) -> None:
    import json

    ingress = await _asgi(rig.app, "POST", INGRESS,
                          [(b"authorization", f"Bearer {SECRET_A}".encode()),
                           (b"content-type", b"application/json")],
                          json.dumps(BODY).encode())
    staff = await _asgi(rig.app, "GET", "/v1/followup/items",
                        [(b"authorization", b"Bearer x.y.z")], b"")
    for status, body, receives in (ingress, staff):
        assert (status, json.loads(body)) == (503, {"code": "NOT_READY"})
        assert receives == 0  # the body was never read


async def test_sg_u11_gate_refuses_before_during_and_after_shutdown() -> None:
    """Review item 2: requests DURING a parked stop are refused by the real Gate, so
    unpublish provably precedes the scheduler stop; closers then observe both."""

    rig = _rig(stop="parked")
    await _both_refused(rig)  # before publish
    life = _Life(rig.app)
    try:
        assert await life.started()
        async with _client(rig.app) as client:
            live = await client.post(INGRESS, json=BODY, headers=_ingress_headers())
        assert live.status_code == 202
        assert rig.spy.contexts == [TENANT_A] and len(rig.spy.received) == 1
        rig.spy.contexts.clear()
        rig.spy.received.clear()
        calls = rig.verifier.calls
        life.shut()
        assert await _until(rig.sched.stop_entered)  # the stop is parked now
        assert rig.app.state.platform is None
        await _both_refused(rig)  # during shutdown
        assert rig.journal.closed == []  # nothing closed while the stop is parked
        rig.sched.stop_gate.set()
        assert await life.settled() and life.task.exception() is None
        assert rig.journal.seen_at_close == _quiet_close(rig)
        await _both_refused(rig)  # after shutdown
        assert rig.spy.contexts == [] and rig.spy.received == []
        assert rig.verifier.calls == calls
    finally:
        await rig.sched.terminate()
        await life.abandon()


@pytest.mark.parametrize(("name", "where"), [("x-tenant-id", "header"),
                                             ("tenant_id", "query")])
async def test_sg_u11b_tenant_input_refused_on_ingress(name: str, where: str) -> None:
    rig = _rig()
    life = _Life(rig.app)
    try:
        assert await life.started()
        async with _client(rig.app) as client:
            if where == "header":
                response = await client.post(INGRESS, json=BODY,
                                             headers=_ingress_headers({name: TENANT_B}))
            else:
                response = await client.post(f"{INGRESS}?{name}={TENANT_B}", json=BODY,
                                             headers=_ingress_headers())
        assert (response.status_code, response.json()) == (
            400, {"code": "TENANT_INPUT_REFUSED"})
        assert rig.spy.contexts == [] and rig.spy.received == []
        life.shut()
        assert await life.settled()
    finally:
        await rig.sched.terminate()
        await life.abandon()


# --- G-S02 static -------------------------------------------------------------------------------


def test_sg_s02_harness_unreachable_from_source() -> None:
    import test_sg_registry_units as reg

    for source in ("import sg_harness\n", "from sg_harness import create_synthetic_app\n",
                   "import importlib\nimportlib.import_module('sg_harness')\n"):
        assert reg._violations(source), source
    reg.test_sg_u01c_no_source_module_reaches_test_support()
    module = _mod()
    assert not hasattr(module, "app")  # no module-level application object
