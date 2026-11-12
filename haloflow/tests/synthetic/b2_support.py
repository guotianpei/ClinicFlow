"""Option B increment B2: shared test support (TEST ONLY; never imported by `src`).

Governing records (Shared Workspace, Work Session 2026-11-11 / 2026-11-12):
- `claude_option-b-b2-package-v1-2026-11-11.md` + v1r1 delta, Codex `codex_b2-v1-review.md`,
  `codex_b2-v1r1-review.md`;
- B2-I1: `claude_b2-i1-continuation-2026-11-12.md` + v1r1, Codex
  `codex_b2-i1-protocol-f-review.md`, `codex_b2-i1-v1r1-investigation-aligned.md` (ALIGN, pins).

What lives here, and why it is independent of the code under test:
- `PgCluster`: one `initdb` cluster per test case, Unix socket in the case's tmp dir only
  (`listen_addresses=''`), binaries from `HALOFLOW_SYNTHETIC_PG_BIN` (must be PostgreSQL 17.x;
  absent => the PostgreSQL rows FAIL, class-B rule, they never skip). Fresh by construction:
  there is no reset or teardown SQL anywhere in B2.
- `snapshot()`: the catalog oracle used by every "no change" assertion. It is written here from
  the catalogs directly and does not call `haloflow.synthetic`. Its sensitivity is itself tested
  (B2-O1: every fresh-baseline class it is asked to detect changes the snapshot).
- `semantic()`: row-content oracle (sorted multiset of `to_jsonb(row)` per table) with an
  EXPLICIT list of nondeterministic audit columns (Codex pin 2026-11-12: no blanket type-based
  exclusion). The list was MEASURED: two independent, identical provisioning runs to target 94
  differ in exactly the six provisioning columns below and nothing else (raw evidence
  `claude_b2-red/nondet-measurement.json`). The phase-table entries are the clock-defaulted
  audit columns of the tables those phases write; the reference-equality rows confirm them at
  implementation, and any column added later needs a measured reason.

The interface the rows pin (`haloflow.synthetic.*`, does not exist yet) is documented in
`INTERFACE` below; every PostgreSQL and unit row imports it lazily so that each row fails in
its own body until it exists (red rule as B1).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import psycopg
from psycopg import sql

HALOFLOW: Final = Path(__file__).resolve().parents[2]
SRC: Final = HALOFLOW / "src"
CHILD: Final = Path(__file__).resolve().parent / "b2_child.py"

INTERFACE: Final = """
haloflow.synthetic.errors   SyntheticRefused(code: str, detail: tuple[str, ...] = ())  .code .detail
                            CODES: frozenset[str]   (closed set, B2-U-C1)
haloflow.synthetic.dsn      parse_admin_dsn(dsn, environ, *, resolve, uid) -> AdminEndpoint
                            AdminEndpoint.conninfo(dbname) -> str ; .target -> str
haloflow.synthetic.attest   check_a0(environ, *, attest_flag: bool) -> None
                            fresh_failures(conn, target) -> tuple[str, ...]   (FRESH_CLASSES order)
                            FRESH_CLASSES: tuple[str, ...]
haloflow.synthetic.guard    K_X, K_S, RESERVATION_PREFIX, reservation_value(nonce,sysid,oid,epoch)
                            guard_sync(conn, expected, *, shared=False)
                            async guard_async(conn, expected, *, shared=False)
                            guarded_factory(conninfo, expected) -> ConnectionFactory
                            begin_run(endpoint, phase, nonce, epoch) -> "RESERVED" | "TAKEN_OVER"
                            RuntimeLease(endpoint)  .acquire() / .release()   (shared K_S; B3 uses)
haloflow.synthetic.state    PHASES ; StateDir(path).locked() -> ctx with .read() / .write(dict)
haloflow.synthetic.migrate  upgrade_guarded(conninfo, expected) -> (revision_before, revision_after)
haloflow.synthetic.provision init(environ, *, attest_flag) -> InitResult(.phase)
                             status(environ) -> dict
haloflow.synthetic.hooks    FAULT: Callable[[str, Mapping[str, Any]], None] | None = None
                            (test-only)

Hook points (FAULT(point, info)); the rows pause or crash at them:
  reserve.before_txn            after the read-only preflight, before the reserving transaction
  takeover.before_commit        inside the takeover transaction (K_X held), before COMMIT
  migrate.before_commit         all Alembic DDL done, before COMMIT   info: backend_pid
  guarded_txn                   after the guard of EVERY guarded transaction
                                info: n (per-process count), backend_pid, install_lock_pid
  reserve.after_commit          D1 (the reservation) committed, before anything else
  phase.<p>.before_commit       last DB statement of phase p done, before its COMMIT
                                info: backend_pid (the connection that will COMMIT)
  phase.<p>.commit_ack          COMMIT returned; fired INSIDE the commit error-handling scope,
                                so an exception raised here is handled exactly like an exception
                                from COMMIT itself (lost acknowledgement of a real commit)
  phase.<p>.after_commit        phase p committed and acknowledged, before the state write of p
  state.after_temp_fsync        temp file written+fsynced, before os.replace   info: phase
For the multi-transaction phase `tenant` (the M01 provisioner), before_commit/commit_ack refer to
its FIRST guarded transaction; mid-phase interruption is covered by `guarded_txn` (I7 P1/P2).
"""

PHASES: Final = ("intent", "stamped", "migrated", "logins", "tenant", "profile", "activated",
                 "settings", "staff", "ready")
TARGET_DB: Final = "haloflow_test_syn_target"
# The four logins and their ONLY group (the suite's own login shims use exactly these groups:
# tests/conftest.py TEST_LOGIN_ROLES). Membership options pinned: admin false, inherit true,
# set true, granted by the bootstrap superuser.
LOGIN_GROUPS: Final = {"runtime": "haloflow_runtime", "migrator": "haloflow_migrator",
                       "provisioner": "haloflow_provisioner",
                       "audit_projector": "haloflow_audit_projector",
                       # Fifth login (Codex ALIGN 20261112T155235Z): the real first-member
                       # path (t034) authorizes a SUPPORT session_user by OID+name.
                       "support": "haloflow_support"}
LOGIN_SUFFIXES: Final = tuple(LOGIN_GROUPS)

# Measured (two independent provisioning runs to target 94; see module docstring).
NONDETERMINISTIC_PROVISIONING: Final = frozenset({
    ("shared.schema_migrations", "started_at"),
    ("shared.schema_migrations", "completed_at"),
    ("shared.tenant_state_history", "occurred_at"),
    ("shared.tenants", "created_at"),
    ("shared.tenants", "updated_at"),
    ("{tenant}.m05_reminder_settings", "updated_at"),
})
# MEASURED (two independent installs to `ready`, identities normalised; raw evidence
# `claude_b2-impl/nondet-phases-measurement.json`): the ONLY columns of the phase tables that
# differ. Each entry has a stated cause; anything else must be equal.
NONDETERMINISTIC_PHASES: Final = frozenset({
    # clock values written by the existing functions
    ("{tenant}.m05_activation_records", "created_at"),
    ("{tenant}.m01_staff", "status_changed_at"),
    ("{tenant}.m01_staff_roles", "granted_at"),
    ("{tenant}.m01_staff_followup_access", "granted_at"),
    ("{tenant}.m01_staff_bootstrap_keys", "created_at"),
    ("{tenant}.m01_staff_bootstrap_log", "at"),
    ("{tenant}.m01_staff_action_keys", "created_at"),
    ("{tenant}.m01_staff_admin_log", "created_at"),
    # random identifiers (gen_random_uuid inside the existing functions)
    ("{tenant}.m05_activation_records", "record_id"),
    ("{tenant}.m01_staff_bootstrap_log", "log_id"),
    ("{tenant}.m01_staff_admin_log", "log_id"),
    # sha256 over canonical commands that embed install-specific identities (tenant, staff,
    # action keys): not comparable ACROSS installs; same-install no-change rows keep them raw
    ("{tenant}.m01_staff_bootstrap_keys", "digest"),
    ("{tenant}.m01_staff_action_keys", "digest"),
    ("{tenant}.m01_staff_admin_log", "digest"),
})
# A clock value NESTED inside JSON snapshot columns: only that one key is removed; the rest of
# each snapshot (status, display name, access) is compared.
NESTED_CLOCK: Final = {("{tenant}.m01_staff_admin_log", ("before", "after", "result")):
                       "status_changed_at"}
# The ledger attempt counter is NOT excluded wholesale: rows compare it explicitly
# (exactly the interrupted unit, exactly +1; every other unit equal).
ATTEMPT: Final = ("shared.schema_migrations", "attempt")


def mod(name: str) -> ModuleType:
    """Lazy import of the code under test, so each row fails in its own body while absent."""

    return importlib.import_module(f"haloflow.synthetic.{name}")


# ---------------------------------------------------------------------------- clusters

class PgBinMissing(AssertionError):
    pass


def pg_bin() -> Path:
    raw = os.environ.get("HALOFLOW_SYNTHETIC_PG_BIN")
    if not raw:
        raise PgBinMissing("HALOFLOW_SYNTHETIC_PG_BIN is required for the B2 PostgreSQL rows")
    path = Path(raw)
    for tool in ("initdb", "pg_ctl", "postgres"):
        if not (path / tool).is_file():
            raise PgBinMissing(f"{tool} missing from HALOFLOW_SYNTHETIC_PG_BIN")
    out = subprocess.run([str(path / "postgres"), "--version"], capture_output=True, text=True,
                         check=True).stdout
    if not re.search(r"\(PostgreSQL\) 17\.\d+", out):
        raise PgBinMissing(f"PostgreSQL 17.x required, got {out.strip()!r}")
    return path


@dataclass
class PgCluster:
    """A per-case cluster. Socket only. Superuser `postgres`, `trust` locally unless `scram`."""

    root: Path
    port: int = field(default_factory=lambda: 50000 + secrets.randbelow(10000))
    scram: bool = False

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def sock(self) -> Path:
        return self.root / "s"

    def start(self, *, target: str | None = TARGET_DB) -> PgCluster:
        bin_ = pg_bin()
        self.sock.mkdir(mode=0o700, parents=True)
        auth = ["--auth-local=scram-sha-256", "--pwfile", str(self._pwfile())] if self.scram \
            else ["-A", "trust"]
        subprocess.run([str(bin_ / "initdb"), "-D", str(self.data), "-U", "postgres",
                        "--no-instructions", *auth], check=True, capture_output=True)
        subprocess.run([str(bin_ / "pg_ctl"), "-D", str(self.data), "-w", "-l",
                        str(self.root / "log"), "-o",
                        f"-c listen_addresses='' -c unix_socket_directories={self.sock}"
                        f" -p {self.port}", "start"], check=True, capture_output=True)
        if target:
            with self.connect("postgres") as c:
                c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        return self

    def _pwfile(self) -> Path:
        p = self.root / "pw"
        self.root.mkdir(parents=True, exist_ok=True)
        p.write_text("bootstrap-test-only\n")
        return p

    def stop(self) -> None:
        subprocess.run([str(pg_bin() / "pg_ctl"), "-D", str(self.data), "-m", "immediate",
                        "stop"], capture_output=True)

    def dsn(self, db: str = TARGET_DB, user: str = "postgres", password: str | None = None) -> str:
        parts = {"host": str(self.sock), "port": str(self.port), "dbname": db, "user": user}
        if password is None and self.scram and user == "postgres":
            password = "bootstrap-test-only"
        if password is not None:
            parts["password"] = password
        return psycopg.conninfo.make_conninfo(**parts)

    def connect(self, db: str = TARGET_DB, **kw: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn(db, **kw), autocommit=True)


@contextmanager
def cluster(tmp_path: Path, **kw: Any) -> Iterator[PgCluster]:
    c = PgCluster(tmp_path / "pg", **kw).start()
    try:
        yield c
    finally:
        c.stop()


def environ_for(c: PgCluster, state: Path, **extra: str) -> dict[str, str]:
    """A minimal, explicit environment for the code under test (no inherited PG* vars)."""

    env = {"HALOFLOW_SYNTHETIC_ADMIN_DSN": c.dsn(), "HALOFLOW_SYNTHETIC_STATE": str(state),
           "HALOFLOW_SYNTHETIC_DISPOSABLE": "1"}
    env.update(extra)
    return env


def state_dir(tmp_path: Path, name: str = "state") -> Path:
    d = tmp_path / name
    d.mkdir(mode=0o700)
    return d


# ---------------------------------------------------------------------------- oracle

SNAPSHOT_SHARED: Final = {
    "roles": "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
             " rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil::text,"
             " (rolpassword IS NULL) AS no_password FROM pg_authid ORDER BY 1",
    "memberships": "SELECT r.rolname, m.rolname, g.rolname, a.admin_option, a.inherit_option,"
                   " a.set_option FROM pg_auth_members a JOIN pg_authid r ON r.oid=a.roleid"
                   " JOIN pg_authid m ON m.oid=a.member JOIN pg_authid g ON g.oid=a.grantor"
                   " ORDER BY 1,2",
    # A digest of every stored verifier, so a SILENT verifier replacement (one non-NULL value for
    # another) changes the snapshot. The verifier itself never enters it.
    "verifier_digests": "SELECT rolname, encode(sha256(convert_to(coalesce(rolpassword,''),"
                        " 'UTF8')), 'hex') FROM pg_authid ORDER BY 1",
    "role_settings": "SELECT setdatabase, setrole::regrole::text, setconfig::text"
                     " FROM pg_db_role_setting ORDER BY 1,2",
    "databases": "SELECT datname, pg_get_userbyid(datdba), coalesce(datacl::text,''),"
                 " datallowconn, datconnlimit, coalesce(shobj_description(oid,'pg_database'),'')"
                 " FROM pg_database ORDER BY 1",
    "tablespaces": "SELECT spcname, pg_get_userbyid(spcowner), coalesce(spcacl::text,'')"
                   " FROM pg_tablespace ORDER BY 1",
    "shared_comments": "SELECT classoid::regclass::text, objoid::text, description"
                       " FROM pg_shdescription ORDER BY 1,2",
}
SNAPSHOT_PER_DB: Final = {
    "schemas": "SELECT nspname, pg_get_userbyid(nspowner), coalesce(nspacl::text,'')"
               " FROM pg_namespace ORDER BY 1",
    "extensions": "SELECT extname, extversion, extnamespace::regnamespace::text FROM pg_extension"
                  " ORDER BY 1",
    "relations": "SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner),"
                 " coalesce(c.relacl::text,''), c.relrowsecurity FROM pg_class c"
                 " JOIN pg_namespace n ON n.oid=c.relnamespace"
                 " WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')"
                 " ORDER BY 1,2",
    "functions": "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),"
                 " md5(coalesce(p.prosrc,'')), p.prosecdef, coalesce(p.proacl::text,''),"
                 " coalesce(p.proconfig::text,''), pg_get_userbyid(p.proowner)"
                 " FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                 " WHERE n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,3",
    "types": "SELECT n.nspname, t.typname, t.typtype FROM pg_type t"
             " JOIN pg_namespace n ON n.oid=t.typnamespace"
             " WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')"
             " AND t.typrelid = 0 ORDER BY 1,2",
    "event_triggers": "SELECT evtname, evtevent, evtenabled FROM pg_event_trigger ORDER BY 1",
    "default_acls": "SELECT defaclrole::regrole::text, defaclnamespace::text, defaclobjtype,"
                    " defaclacl::text FROM pg_default_acl ORDER BY 1,2,3",
    "languages": "SELECT lanname, pg_get_userbyid(lanowner) FROM pg_language ORDER BY 1",
    "fdw": "SELECT fdwname FROM pg_foreign_data_wrapper UNION ALL"
           " SELECT 'server:'||srvname FROM pg_foreign_server ORDER BY 1",
    "publications": "SELECT pubname FROM pg_publication ORDER BY 1",
    # Keyed by object IDENTITY, never by OID (OIDs differ between equal clusters, e.g. after a
    # rolled-back attempt consumed some).
    "comments": "SELECT pg_describe_object(classoid, objoid, objsubid), description"
                " FROM pg_description WHERE objoid >= 16384 ORDER BY 1, 2",
    # The same comments keyed by RAW identity (class, OID, sub-id). The semantic key above
    # survives a drop-and-recreate with the same name and comment; this one does not, so a
    # same-install "no change" assertion also catches object REPLACEMENT. OIDs differ between
    # clusters, so cross-cluster comparisons drop this class (see `comparable()`).
    "comments_raw": "SELECT classoid::regclass::text, objoid::text, objsubid::text, description"
                    " FROM pg_description WHERE objoid >= 16384 ORDER BY 1, 2, 3",
}


def snapshot(c: PgCluster, *, rows: bool = True) -> dict[str, Any]:
    """Every database's catalog state plus (optionally) every user table's row multiset.
    Passwords never enter it (only `rolpassword IS NULL`)."""

    snap: dict[str, Any] = {}
    with c.connect("postgres") as a:
        for k, q in SNAPSHOT_SHARED.items():
            snap[k] = [list(map(str, r)) for r in a.execute(q).fetchall()]
        dbs = [r[0] for r in a.execute(
            "SELECT datname FROM pg_database WHERE datallowconn ORDER BY 1").fetchall()]
    for db in dbs:
        with c.connect(db) as d:
            for k, q in SNAPSHOT_PER_DB.items():
                snap[f"{db}:{k}"] = [list(map(str, r)) for r in d.execute(q).fetchall()]
            if rows:
                snap[f"{db}:rows"] = _rows(d, exclude=frozenset())
    return snap


def _rows(d: psycopg.Connection[Any], *, exclude: frozenset[tuple[str, str]],
          tenant: str | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    tables = d.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE c.relkind IN ('r','p') AND n.nspname NOT IN"
        " ('pg_catalog','information_schema','pg_toast') ORDER BY 1,2").fetchall()
    for n, t in tables:
        name = f"{n}.{t}"
        drop = [col for (tab, col) in exclude
                if tab == name or (tenant is not None and tab == "{tenant}." + t and n == tenant)]
        q = sql.SQL("SELECT (to_jsonb(x) - %s::text[])::text FROM {}.{} x").format(
            sql.Identifier(n), sql.Identifier(t))
        out[name] = sorted(r[0] for r in d.execute(q, (drop,)).fetchall())
    return out


def semantic(c: PgCluster, tenant_schema: str, *, db: str = TARGET_DB,
             include_attempt: bool = False) -> dict[str, list[str]]:
    """Row contents minus ONLY the explicit nondeterministic columns (and, unless asked, the
    ledger attempt, which the rows compare separately and exactly)."""

    exclude = set(NONDETERMINISTIC_PROVISIONING | NONDETERMINISTIC_PHASES)
    if not include_attempt:
        exclude.add(ATTEMPT)
    with c.connect(db) as d:
        d.execute("SET TimeZone = 'UTC'")   # one rendering for intent-recorded timestamps
        rows = _rows(d, exclude=frozenset(exclude), tenant=tenant_schema)
    for (table, columns), key in NESTED_CLOCK.items():
        name = table.replace("{tenant}", tenant_schema)
        if name in rows:
            fixed = []
            for text in rows[name]:
                row = json.loads(text)
                for col in columns:
                    row[col] = _strip_key(row.get(col), key)
                fixed.append(json.dumps(row, sort_keys=True))
            rows[name] = sorted(fixed)
    return rows


def _strip_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return {k: _strip_key(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_strip_key(v, key) for v in value]
    return value


def ledger_running(c: PgCluster) -> frozenset[str]:
    """INDEPENDENT interruption evidence: ledger units left `running` (read from the DB at the
    barrier/crash point, never from the implementation's own report)."""

    with c.connect() as d:
        if d.execute("SELECT to_regclass('shared.schema_migrations')").fetchone() == (None,):
            return frozenset()
        return frozenset(r[0] for r in d.execute(
            "SELECT migration_id FROM shared.schema_migrations WHERE state = 'running'"))


def ledger_attempts(c: PgCluster) -> dict[str, int]:
    with c.connect() as d:
        return {r[0]: int(r[1]) for r in d.execute(
            "SELECT migration_id, attempt FROM shared.schema_migrations ORDER BY 1").fetchall()}


def diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Keys whose values differ (for readable assertion messages)."""

    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def slot(c: PgCluster) -> str | None:
    with c.connect("postgres") as a:
        v = a.execute("SELECT shobj_description(10,'pg_authid')").fetchone()
    return None if v is None else v[0]


# ---------------------------------------------------------------------------- children

@dataclass
class Child:
    proc: subprocess.Popen[str]
    bar: Path

    def reached(self, point: str, timeout: float = 120) -> dict[str, Any]:
        f = self.bar / f"{point}.reached"
        end = time.time() + timeout
        while time.time() < end:
            if f.exists():
                time.sleep(0.05)
                return dict(json.loads(f.read_text()))
            if self.proc.poll() is not None:
                raise AssertionError(f"child exited before {point}: rc={self.proc.returncode}"
                                     f" {self.proc.stdout.read() if self.proc.stdout else ''}")
            time.sleep(0.05)
        raise AssertionError(f"barrier {point} not reached")

    def crashed(self, timeout: float = 300) -> None:
        """The child must die at its crash point (exit 97); otherwise show what it printed."""
        out, _ = self.proc.communicate(timeout=timeout)
        assert self.proc.returncode == 97, f"crash point not reached: rc={self.proc.returncode}" \
            f" {out[-1500:]}"

    def go(self, point: str) -> None:
        (self.bar / f"{point}.go").touch()

    def result(self, timeout: float = 300) -> dict[str, Any]:
        out, _ = self.proc.communicate(timeout=timeout)
        lines = [ln for ln in out.splitlines() if ln.startswith("B2CHILD ")]
        assert lines, f"no child result (rc={self.proc.returncode}): {out[-2000:]}"
        return dict(json.loads(lines[-1][len("B2CHILD "):]))


CHILDREN: list[subprocess.Popen[str]] = []


def reap() -> None:
    """Kill any child a failed row left behind (bounded cleanup; a mutant cannot strand the
    suite on a barrier)."""

    while CHILDREN:
        p = CHILDREN.pop()
        if p.poll() is None:
            p.kill()
            p.wait(10)


def spawn(bar: Path, action: str, *, environ: dict[str, str], pause: str = "",
          crash: str = "", raise_at: str = "", kill_at: str = "",
          extra: dict[str, Any] | None = None) -> Child:
    """Run `b2_child.py` in a fresh `python -I` with an explicit environment.

    `pause` = a hook point name (optionally `point#n`) to block at; `crash` = a hook point at
    which the child calls `os._exit(97)` (simulated process death); `raise_at` = raise
    psycopg.OperationalError at that point (with `phase.<p>.commit_ack`: a LOST ACKNOWLEDGEMENT
    of a commit that really happened); `kill_at` = terminate the backend named by the hook
    info's `backend_pid` from a separate connection, then continue (a real rollback through
    connection loss)."""

    bar.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"action": action, "pause": pause, "crash": crash, "raise_at": raise_at,
                          "kill_at": kill_at, "bar": str(bar), "environ": environ,
                          "extra": extra or {}})
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC)}
    proc = subprocess.Popen([sys.executable, "-I", str(CHILD), payload], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    CHILDREN.append(proc)
    return Child(proc, bar)


def terminate_backend(c: PgCluster, pid: int) -> bool:
    with c.connect("postgres") as a:
        row = a.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
    return bool(row and row[0])


def waiting_advisory(c: PgCluster) -> list[int]:
    with c.connect("postgres") as a:
        return [r[0] for r in a.execute(
            "SELECT pid FROM pg_locks WHERE locktype='advisory' AND NOT granted").fetchall()]


def wait_for(pred: Callable[[], Any], timeout: float = 30) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def copy_state(src: Path, dst: Path) -> Path:
    """A COPIED state directory (new inode for state.lock), as an operator might make."""

    shutil.copytree(src, dst)
    os.chmod(dst, 0o700)
    return dst


def read_state(d: Path) -> dict[str, Any]:
    return dict(json.loads((d / "state.json").read_text()))
