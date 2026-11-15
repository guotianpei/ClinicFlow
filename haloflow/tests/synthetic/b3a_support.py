"""Option B increment B3a: shared test support (TEST ONLY; never imported by `src`).

Governing records (Shared Workspace, Work Session 2026-11-14), Codex ALIGN for red preparation
20261115T021406Z (`codex_b3a-v1r2-review.md`):
- `claude_b3-package-v1-2026-11-14.md` (package), `claude_b3a-test-package-v1-2026-11-14.md`,
  `claude_b3a-v1r1-2026-11-14.md`, `claude_b3a-v1r2-2026-11-14.md` (later governs earlier);
- Codex reviews `codex_b3a-v1-recovery-review.md`, `codex_b3a-v1r1-review.md`,
  `codex_b3a-v1r2-review.md`.

Red classes (v1r1 D4.1): every row states its class in its docstring.
- C  control: passes now, without any B3 code; proves a fixture or an oracle.
- B  behaviour-red on accepted B2: runs now and fails with a NAMED assertion about B2 behaviour.
- A  absence-red: fails in its own body on the missing B3 interface (ModuleNotFoundError for
     `haloflow.synthetic.{seal_scope,runtime,lifecycle,app}`, AttributeError/TypeError for the
     new names on existing modules).

Independence of the oracles from the code under test:
- `Recorder` is fed by the resources themselves (test-only subclasses/wrappers).
- `trace()` parses the per-case cluster's own server log (log_statement=all), never the code's
  bookkeeping.
- `Journal` is an fsync'd append-only file in the case's tmp dir: external-effect evidence that
  survives the restarting process (v1r1 D4.2).
- `backends()` / `ks_free()` read pg_stat_activity / pg_locks from a separate admin connection.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import b2_support as s
import psycopg

HERE: Final = Path(__file__).resolve().parent
M05: Final = HERE.parent / "m05"
CHILD: Final = HERE / "b3a_child.py"
for _p in (M05, M05.parent / "m02"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

INTERFACE: Final = """
haloflow.m01.pool.TenantPool(conninfo, *, ..., connection_class=AsyncConnection)   (public kw)
haloflow.synthetic.guard
    GuardedRuntimeConnection, GuardedRuntimeCursor, ALLOWLIST: frozenset[str]
haloflow.synthetic.seal_scope
    INSTALL_SEALED_TABLES, RUNTIME_MUTABLE_TABLES: frozenset[str]   ("shared.<t>" | "{tenant}.<t>")
    sealed_digest(capture) -> str ; reduced_baseline(state) -> dict   (pure; no DB)
haloflow.synthetic.installed_profile  SEALED_READY_DIGEST: str   (generated)
haloflow.synthetic.runtime
    SyntheticRuntime(endpoint, state, *, application_name)  async open()/close()
    .tenant_runtime .pool .lease
haloflow.synthetic.lifecycle
    LifecycleConfig(lease_check_s=5, stop_timeout_s=(2, 2, 2), t_watcher=2, t_port=2, t_pool=5,
                    t_quiesce=2, t_lease=5)  .t_total     (refuses t_total > 60, bounds <= 0)
    SyntheticLifecycle(environ, *, config=None, sender_factory=None, ehr_factory=None,
                       fatal_exit=None, application_name=None)
    async start() ; request_stop(reason) (sync, idempotent) ; async wait_stopped() -> result
    .state in NEW|STARTING|RUNNING|STOPPING|STOPPED|STOP_FAILED|FAILED
    .admission_open: bool ; .schedulers {"due","reminder","writeback"} ; .port ; .sender
    .runtime (SyntheticRuntime) ; .deadline (monotonic, set by the first request_stop)
    LifecycleResult(disposition in {"drained","terminal","lease_lost_drained"}, code, step,
                    reason, cleanup in {None,"gone","terminated","unknown"}, deadline)
haloflow.synthetic.app   create_synthetic_app(environ=None, *, lifecycle=None) -> FastAPI
                         GET /synthetic/health -> 200 {"simulated": true, "state": ...} | 503
haloflow.synthetic.__main__   serve [--host 127.0.0.1] [--port N]
haloflow.synthetic.errors  + UNGUARDED_STATEMENT RUNTIME_NO_DDL RUNTIME_STOPPED RUNTIME_LEASE_LOST
                             RUNTIME_STOP_FAILED LEASE_CLEANUP_UNKNOWN CLOSE_TIMEOUT
                             DRAIN_DEADLINE POOL_NOT_QUIESCENT WATCHER_STOP_FAILED
state.json  + optional "runtime": {"first_epoch": <epoch>}   (phase "ready" only)
hooks (test-only FAULT, unchanged mechanism), new points:
    b3.start.<a0|lease|a1|marker.before|marker.after|pool|runtime|wiring|schedulers|watcher>
    b3.stop.<watcher|producers|port|pool|quiesce|lease>
    b3.r.<after_accept|after_consume|after_reply|after_spend|after_write_issued|
          after_settle_commit>   (fired by the B3a wiring wrappers; R rows crash there)
lifecycle extras the rows read: .watcher_checks (int), .note_keys (the state's keys),
    .config, .runtime.lease.pid, .runtime.dependencies; the lease connection's
    application_name is "<application_name>/lease"
"""

# v1r1 D1.1 + F-S1 + F-R1 (Codex 20261115T023713Z): the composed write paths measured on a
# REAL B2 install (claude_b3a-measure/b2-runtime-measure.jsonl, sc1.jsonl) and, with the
# resolution activation EMULATED admin-side, durable reachability of every W1 outcome incl.
# the note-required Other (fr1-reach.jsonl): notes and resolution links persist and replay.
# `m05_outbound_schedule` is transient (inserted at registration, deleted at claim): measured
# separately with a not-yet-due appointment.
EXPECTED_RUNTIME_MUTABLE: Final = frozenset({
    "{tenant}.m05_followup_notes",
    "{tenant}.event_handoff_outbox", "{tenant}.m05_appointment_profile",
    "{tenant}.m05_appointments", "{tenant}.m05_contact_state", "{tenant}.m05_followup_actions",
    "{tenant}.m05_followup_history", "{tenant}.m05_followup_items",
    "{tenant}.m05_inbound_replies", "{tenant}.m05_outbound_ops",
    "{tenant}.m05_outbound_render_inputs", "{tenant}.m05_outbound_schedule",
    "{tenant}.m05_outbound_send_tickets", "{tenant}.m05_patient_contacts",
    "{tenant}.m05_reminder_windows", "{tenant}.m05_writeback_baselines",
    "{tenant}.m05_writeback_ops", "{tenant}.m05_writeback_permits",
    "{tenant}.patient_event_links", "{tenant}.patient_events",
})
# Install-sealed by ruling (Codex Q4 / Q-T1 / F-N1): never runtime-mutable.
MUST_BE_SEALED: Final = frozenset({
    "{tenant}.m01_staff", "{tenant}.m01_staff_roles", "{tenant}.m01_staff_followup_access",
    "{tenant}.m01_staff_action_keys", "{tenant}.m01_staff_admin_log",
    "{tenant}.m05_activation_records", "{tenant}.m05_reminder_settings",
    "shared.isolation_alerts",
})


def mod(name: str) -> Any:
    """Lazy import of `haloflow.synthetic.<name>` inside a row body (red rule, as B2)."""
    return s.mod(name)


# ---------------------------------------------------------------------------- install

@dataclass
class Install:
    c: s.PgCluster
    st_dir: Path
    tmp: Path

    @property
    def env(self) -> dict[str, str]:
        return s.environ_for(self.c, self.st_dir)

    @property
    def state(self) -> dict[str, Any]:
        return s.read_state(self.st_dir)

    @property
    def schema(self) -> str:
        return str(self.state["schema_key"])

    def admin(self, q: str, params: Any = None, db: str = s.TARGET_DB) -> list[tuple[Any, ...]]:
        with self.c.connect(db) as a:
            cur = a.execute(q, params)
            return cur.fetchall() if cur.description else []


def install(c: s.PgCluster, tmp_path: Path) -> Install:
    """A fresh B2 install to `ready` (Codex Q-T4: fresh per case, no copied datadir)."""
    st_dir = s.state_dir(tmp_path)
    r = s.mod("provision").init(s.environ_for(c, st_dir), attest_flag=True)
    assert r.phase == "ready"
    return Install(c, st_dir, tmp_path)


def as_si_install(inst: Install) -> Any:
    """The B2 install in the S-I harness shape (C/B rows only): B2's OWN logins and tenant,
    marked installed so the suite provisioner never runs. The accepted S-J harness then
    drives real (unguarded, test-side) runtime activity on a real B2 install."""
    import m02_support
    import sa_support as sa
    import si_support as si

    st = inst.state
    lg = st["logins"]

    def dsn(k: str) -> str:
        return inst.c.dsn(user=lg[k]["role"], password=lg[k]["password"])

    ids = m02_support.Identities(admin=inst.c.dsn(s.TARGET_DB), logins={
        sa.RUNTIME: dsn("runtime"), sa.MIGRATOR: dsn("migrator"),
        "haloflow_provisioner": dsn("provisioner")})
    si_inst = si.SIInstall(ids, (st["tenant_id"], st["schema_key"]))
    si_inst._attempted = True
    return si_inst


def runtime_conninfo(inst: Install, **kw: str) -> str:
    lg = inst.state["logins"]["runtime"]
    return psycopg.conninfo.make_conninfo(inst.c.dsn(user=lg["role"], password=lg["password"]),
                                          **kw)


# ---------------------------------------------------------------------------- tables

def table_digests(inst: Install) -> dict[str, tuple[int, str]]:
    """Row count + md5 of the sorted to_jsonb rows of every user table, keyed
    `shared.<t>` / `{tenant}.<t>` / `<schema>.<t>` (admin connection, read only)."""
    out: dict[str, tuple[int, str]] = {}
    schema = inst.schema
    with inst.c.connect() as a:
        tabs = a.execute("SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n"
                         " ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname"
                         " NOT IN ('pg_catalog','information_schema') ORDER BY 1, 2").fetchall()
        for sc, t in tabs:
            q = psycopg.sql.SQL(
                "SELECT count(*), md5(coalesce(string_agg(x, '|' ORDER BY x), ''))"
                " FROM (SELECT to_jsonb(r)::text AS x FROM {}.{} AS r) AS z").format(
                    psycopg.sql.Identifier(sc), psycopg.sql.Identifier(t))
            n, h = a.execute(q).fetchone()  # type: ignore[misc]
            out[("{tenant}" if sc == schema else sc) + "." + t] = (int(n), str(h))
    return out


def changed(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    return {t for t in set(before) | set(after) if before.get(t) != after.get(t)}


def live_tables(inst: Install) -> set[str]:
    return set(table_digests(inst))


# ---------------------------------------------------------------------------- server trace

def trace_on(inst: Install) -> None:
    """log_statement=all with pid, xid-free prefix, application_name (per-case cluster only)."""
    with inst.c.connect("postgres") as a:
        a.execute("ALTER SYSTEM SET log_statement = 'all'")
        a.execute("ALTER SYSTEM SET log_line_prefix = '[%p] [%a] '")
        a.execute("SELECT pg_reload_conf()")
    time.sleep(0.3)


# Simple protocol logs "statement: ..."; the extended protocol (psycopg with parameters)
# logs "execute <name>: ..." (found by control C3); parameter DETAIL lines are not statements.
_LINE: Final = re.compile(
    r"^\[(\d+)\] \[([^\]]*)\] LOG:  (?:statement|execute [^:]*): (.*)$")


def trace(inst: Install, app_prefix: str) -> dict[int, list[str]]:
    """Statements per backend pid for backends whose application_name starts with
    `app_prefix` (multi-line statements are joined)."""
    per: dict[int, list[str]] = {}
    last: int | None = None
    for raw in (inst.c.root / "log").read_text(errors="replace").splitlines():
        m = _LINE.match(raw)
        if m:
            pid, app, stmt = int(m.group(1)), m.group(2), m.group(3)
            if app.startswith(app_prefix):
                per.setdefault(pid, []).append(stmt.strip())
                last = pid
            else:
                last = None
        elif last is not None and raw.startswith(("\t", " ")) and "DETAIL:" not in raw:
            per[last][-1] += " " + raw.strip()
        else:
            last = None
    return per


F2_PREFIX: Final = (
    re.compile(r"^SET TRANSACTION READ WRITE$", re.I),
    re.compile(r"^SELECT pg_catalog\.pg_advisory_xact_lock_shared\(", re.I),
    re.compile(r"current_setting\('transaction_isolation'\)", re.I),
    re.compile(r"pg_control_system\(\)", re.I),
    re.compile(r"shobj_description\(10, 'pg_authid'\)", re.I),
)


def transactions(stmts: list[str]) -> tuple[list[list[str]], list[str]]:
    """Split one backend's statements into explicit transactions (BEGIN..COMMIT/ROLLBACK,
    aborted ones included) and the statements run outside any transaction."""
    txs: list[list[str]] = []
    outside: list[str] = []
    cur: list[str] | None = None
    for st in stmts:
        u = st.upper()
        if u.startswith("BEGIN"):
            cur = []
        elif cur is not None and (u.startswith("COMMIT") or u.startswith("ROLLBACK")):
            txs.append(cur)
            cur = None
        elif cur is not None:
            cur.append(st)
        else:
            outside.append(st)
    if cur is not None:
        txs.append(cur)
    return txs, outside


# ---------------------------------------------------------------------------- recorder

@dataclass
class Recorder:
    """Ordered resource events recorded BY THE RESOURCES (wrappers), with the live
    scheduler-task count at each event."""

    events: list[tuple[str, int]] = field(default_factory=list)

    def note(self, what: str) -> None:
        import asyncio

        owners = ("WritebackScheduler.", "ReminderScheduler.", "DueWindowScheduler.")
        try:
            tasks = asyncio.all_tasks()
        except RuntimeError:
            tasks = set()
        live = sum(1 for t in tasks if not t.done()
                   and getattr(t.get_coro(), "__qualname__", "").startswith(owners))
        self.events.append((what, live))

    def order(self) -> list[str]:
        return [e[0] for e in self.events]


# ---------------------------------------------------------------------------- journal

class Journal:
    """fsync'd append-only external-effect evidence (survives os._exit of the writer)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, **rec: Any) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True, default=str) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]


def journal_sender(journal: Journal) -> Any:
    """A FakeSender subclass that journals each send BEFORE returning to the worker."""
    from haloflow.m05.outbound import FakeSender

    class JournalSender(FakeSender):  # type: ignore[misc]
        async def send(self, op_id: str, phone: str, text: str) -> Any:
            result = await super().send(op_id, phone, text)
            journal.append(effect="send", op_id=str(op_id), phone=phone)
            return result

    return JournalSender()


def journal_ehr(journal: Journal, clock: Callable[[], Any]) -> Any:
    from haloflow.m05.simulated_ehr import SimulatedEhr

    class JournalEhr(SimulatedEhr):  # type: ignore[misc]
        async def write_confirmation(self, appointment_id: str, **kw: Any) -> Any:
            result = await super().write_confirmation(appointment_id, **kw)
            journal.append(effect="write_confirmation", appointment_id=appointment_id,
                           permit_id=str(kw.get("permit_id")))
            return result

    return JournalEhr(clock=clock)


# ---------------------------------------------------------------------------- observers

def backends(inst: Install, app: str) -> list[int]:
    with inst.c.connect("postgres") as a:
        return [int(r[0]) for r in a.execute(
            "SELECT pid FROM pg_stat_activity WHERE application_name = %s", (app,)).fetchall()]


def ks_free(inst: Install) -> bool:
    """True iff the exclusive K_S session lock can be taken now (then released)."""
    k_s = s.mod("guard").K_S
    with inst.c.connect("postgres") as a:
        got = bool(a.execute("SELECT pg_try_advisory_lock(%s)", (k_s,)).fetchone()[0])  # type: ignore[index]
        if got:
            a.execute("SELECT pg_advisory_unlock(%s)", (k_s,))
        return got


@contextmanager
def held_install_lock(inst: Install) -> Iterator[None]:
    lock = s.mod("guard").InstallLock(s.mod("dsn").parse_admin_dsn(
        inst.env["HALOFLOW_SYNTHETIC_ADMIN_DSN"], {}))
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def wait_until(pred: Callable[[], Any], timeout: float, step: float = 0.02) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"condition not reached within {timeout}s")


# ---------------------------------------------------------------------------- children

@dataclass
class B3Child:
    proc: subprocess.Popen[str]
    bar: Path
    app: str

    def reached(self, point: str, timeout: float = 120) -> dict[str, Any]:
        return s.Child(self.proc, self.bar).reached(point, timeout)

    def go(self, point: str) -> None:
        (self.bar / f"{point}.go").touch()

    def seen(self, point: str, timeout: float = 120) -> None:
        """Non-blocking barrier: the child touched <bar>/<point>.seen at that b3.* hook."""
        f = self.bar / f"{point}.seen"
        end = time.time() + timeout
        while time.time() < end:
            if f.exists():
                return
            if self.proc.poll() is not None:
                raise AssertionError(f"child exited before {point}: rc={self.proc.returncode}"
                                     f" {self.proc.stdout.read() if self.proc.stdout else ''}")
            time.sleep(0.05)
        raise AssertionError(f"{point} not seen")

    def wait(self, timeout: float) -> tuple[int, str]:
        out, _ = self.proc.communicate(timeout=timeout)
        return self.proc.returncode, out


def spawn(inst: Install, bar: Path, action: str, *, app: str, pause: str = "",
          crash: str = "", extra: dict[str, Any] | None = None) -> B3Child:
    """Run `b3a_child.py` (fresh `python -I`, explicit environment) for one lifecycle
    generation or one `serve`. `crash` = os._exit(97) at that hook point."""
    bar.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"action": action, "pause": pause, "crash": crash, "bar": str(bar),
                          "environ": inst.env, "app": app, "extra": extra or {}})
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{s.SRC}{os.pathsep}{HERE}"}
    proc = subprocess.Popen([sys.executable, "-I", str(CHILD), payload], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    s.CHILDREN.append(proc)
    return B3Child(proc, bar, app)


def assert_gone(inst: Install, child: B3Child, timeout: float = 30) -> None:
    """v1r1 D4.2 + Codex v1r2: the old generation is gone and its backends are cleaned up
    (eventually; no ordering claim between backends)."""
    assert child.proc.poll() is not None, "old generation still running"
    wait_until(lambda: backends(inst, child.app) == [], timeout)
    wait_until(lambda: ks_free(inst), timeout)


# ---------------------------------------------------------------------------- test-side flow

def _staff_ctx(rt: Any, tenant: str, staff_id: str) -> Any:
    import uuid

    import sj_harness as sj

    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource

    caps = sj.CAPS["resolve"]
    return rt.resolver.resolve(
        principal=Principal(kind=PrincipalKind.ACTOR, id=staff_id, auth_method="test",
                            authorized_tenant_ids=frozenset({tenant}), capabilities=caps),
        tenant_hint=tenant, purpose="operations", capabilities=caps, source=TrustedSource.HTTP,
        execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)


def sj_flow(inst: Install, *, resolve: bool = True) -> dict[str, Any]:
    """CONTROL-SIDE runtime activity on a real B2 install through the accepted S-J harness
    (test-side composition, unguarded): J01 confirm write-back; J02 status-changed item,
    claim + resolve `reached_no_change` (B2's own staff member); J03 replies 2/3/NO/STOP; and
    one appointment registered but not yet due (its schedule row persists: F-S1).
    `resolve=False` stops after the claim: resolution needs the F-R1 amendment (on accepted B2
    it fails CONTRACT_UNSUPPORTED), so controls and B2 premises use resolve=False."""
    import asyncio
    import uuid

    import sa_support as sa
    import service_support as sv
    import sh2_support as sh2
    import si_support as si
    import sj_harness as sj
    import test_sj_postgres as tsj

    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.service import ResolveAction, StaffAction
    from haloflow.m05.writeback import StaffServiceV3

    si_inst = as_si_install(inst)
    st = inst.state
    rec: dict[str, Any] = {}

    async def body() -> None:
        ehr, _ = await tsj._flow_ehr(si_inst)
        stack = await sj.build(si_inst, ehr)
        try:
            f1 = await tsj._sent_and_replied(stack)
            tsj._put(si_inst, ehr, f1)
            rec["j01"] = tsj._wb_counts(await stack.tick_writeback())
            f2 = await tsj._sent_and_replied(stack)
            tsj._put(si_inst, ehr, f2, provider_ref="prov-other")
            rec["j02"] = tsj._wb_counts(await stack.tick_writeback())
            (item,) = si.check_items(si_inst)
            iid = uuid.UUID(str(item["item_id"]))
            staff = StaffServiceV3(stack.rt.gateway, foundation=EventFoundation(
                stack.rt.gateway, producer_service=sv.PRODUCER), keys=sv.TestNoteKeys())
            ctx = lambda: _staff_ctx(stack.rt, si_inst.tenant_id, st["staff_id"])  # noqa: E731
            rec["claim"] = (await staff.act(await ctx(), StaffAction(
                item_id=iid, client_action_id=uuid.uuid4(), expected_version=1,
                type="claim"))).outcome
            if resolve:
                rec["resolve"] = (await staff.act(await ctx(), ResolveAction(
                    item_id=iid, client_action_id=uuid.uuid4(), expected_version=2,
                    type="resolve", outcome_code="reached_no_change"))).outcome
            for text in ("2", "3", "NO", "STOP"):
                f = await tsj._sent_and_replied(stack, text)
                tsj._put(si_inst, ehr, f)
                await stack.tick_all()
            now = sh2.db_now(si_inst)
            zone = sh2.zone_with_local_hour(now, 11, 15)
            patient, phone, ref = sa.new_id(), sa.fresh_phone(), sa.new_id()
            await sh2.gateway_directory(stack.rt, si_inst, "contact_put", phone=phone,
                                        patient_id=patient)
            later = await sh2.gateway_directory(stack.rt, si_inst, "appointment_put",
                                                **si.appointment3(ref, patient,
                                                                  sh2.at_local(now, zone, 10, 18),
                                                                  department_tz=zone))
            rec["not_yet_due"] = later["outcome"]
            rec["sends"] = len(stack.sender.calls)
            rec["ehr_writes"] = len(ehr.writes)
        finally:
            rec["shutdown"] = await stack.shutdown()

    asyncio.run(body())
    return rec
