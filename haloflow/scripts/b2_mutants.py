"""Option B B2 mutation controls, group family ``b2`` (B2-M1..B2-M19).

Safety-focused (B2-I1 and B2 red-packet mutation lists, Codex 2026-11-12): protocol F's
guard, takeover and reservation ordering; the DSN and state-directory defences; the fresh
attestation; and the installer's commit/acknowledge/ownership/expiry rules. Five groups, one
target and one test module each (the shared part-A checker binds a mutant to one module):

- ``dsn``       src/haloflow/synthetic/dsn.py        judged by tests/synthetic/test_b2_units.py
- ``state``     src/haloflow/synthetic/state.py      judged by tests/synthetic/test_b2_units.py
- ``guard``     src/haloflow/synthetic/guard.py      judged by test_b2_guard_postgres.py
- ``attest``    src/haloflow/synthetic/attest.py     judged by test_b2_guard_postgres.py
- ``provision`` src/haloflow/synthetic/provision.py  judged by test_b2_init_postgres.py

Kill sets are MEASURED (scripts/b2_mutant_kills.json): every node a mutant may fail; a mutant
is killed only if it fails and every failure is in its set (the part-A rule). A survivor is
reported, never silently dropped. The PostgreSQL groups need HALOFLOW_SYNTHETIC_PG_BIN (and
the init module HALOFLOW_TEST_DATABASE_URL), as the gated suite does.

Usage (from haloflow/):
    python3 -I scripts/b2_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/b2-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SYN = Path("src/haloflow/synthetic")
DSN, STATE, GUARD, ATTEST, PROVISION = (SYN / "dsn.py", SYN / "state.py", SYN / "guard.py",
                                        SYN / "attest.py", SYN / "provision.py")
UNITS = "tests/synthetic/test_b2_units.py"
GUARD_TESTS = "tests/synthetic/test_b2_guard_postgres.py"
INIT_TESTS = "tests/synthetic/test_b2_init_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "b2_mutant_kills.json"

_GUARD_SYNC_OLD = (
    '    conn.execute("SET TRANSACTION READ WRITE")\n'
    '    conn.execute("SELECT pg_catalog.pg_advisory_xact_lock_shared(%s)" if shared\n'
    '                 else "SELECT pg_catalog.pg_advisory_xact_lock(%s)", (K_X,))\n'
    '    iso = conn.execute("SELECT pg_catalog.current_setting(\'transaction_isolation\')")'
    '.fetchone()\n'
    '    if iso is None or iso[0] != "read committed":\n'
    '        raise SyntheticRefused("ISOLATION_NOT_READ_COMMITTED")\n'
    '    identity = conn.execute(IDENT_SQL).fetchone()\n'
    '    row = conn.execute(RES_SQL).fetchone()                 # separate statement: fresh '
    'snapshot\n')
# MUTANT: the reservation is read BEFORE the lock wait (the A3m stale-snapshot defect).
_GUARD_SYNC_NEW = (
    '    conn.execute("SET TRANSACTION READ WRITE")\n'
    '    row = conn.execute(RES_SQL).fetchone()\n'
    '    conn.execute("SELECT pg_catalog.pg_advisory_xact_lock_shared(%s)" if shared\n'
    '                 else "SELECT pg_catalog.pg_advisory_xact_lock(%s)", (K_X,))\n'
    '    iso = conn.execute("SELECT pg_catalog.current_setting(\'transaction_isolation\')")'
    '.fetchone()\n'
    '    if iso is None or iso[0] != "read committed":\n'
    '        raise SyntheticRefused("ISOLATION_NOT_READ_COMMITTED")\n'
    '    identity = conn.execute(IDENT_SQL).fetchone()\n')

# (id, description, target, old, new); kill sets in b2_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    # -- dsn ------------------------------------------------------------------------------
    ("B2-M1", "dsn: hostaddr is accepted", DSN,
     'ALLOWED_KEYS: Final = frozenset({"host", "port", "dbname", "user", "password", '
     '"sslmode"})\n',
     'ALLOWED_KEYS: Final = frozenset({"host", "port", "dbname", "user", "password", '
     '"sslmode", "hostaddr"})\n'),
    ("B2-M2", "dsn: libpq PG* environment variables are ignored", DSN,
     "    if any(_PG_ENV.match(k) for k in environ):\n",
     "    if False:\n"),
    ("B2-M3", "dsn: a symlinked socket path is followed", DSN,
     "        if stat.S_ISLNK(st.st_mode):\n            return False\n",
     "        if False:\n            return False\n"),
    # -- state ----------------------------------------------------------------------------
    ("B2-M4", "state: state.json is opened through a symlink", STATE,
     "            fd = os.open(STATE_FILE, os.O_RDONLY | _NOFOLLOW, dir_fd=self._dir_fd)\n",
     "            fd = os.open(STATE_FILE, os.O_RDONLY, dir_fd=self._dir_fd)\n"),
    ("B2-M5", "state: the state file is replaced before the crash window (non-atomic)", STATE,
     '        fire("state.after_temp_fsync", {"phase": value.get("phase")})\n'
     "        self._check_lock()\n"
     "        os.replace(tmp, STATE_FILE, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)\n",
     "        self._check_lock()\n"
     "        os.replace(tmp, STATE_FILE, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)\n"
     '        fire("state.after_temp_fsync", {"phase": value.get("phase")})\n'),
    ("B2-M6", "state: the lock inode is not re-checked before a write", STATE,
     "        if (st.st_dev, st.st_ino) != self._inode:\n",
     "        if False:\n"),
    # -- guard ----------------------------------------------------------------------------
    ("B2-M7", "guard: the actual connection identity is not checked (T2m)", GUARD,
     "    if identity is None or str(identity[0]) != sysid:\n"
     '        raise SyntheticRefused("CLUSTER_MISMATCH")\n'
     "    if int(identity[1]) != oid:\n"
     '        raise SyntheticRefused("TARGET_MISMATCH")\n',
     ""),
    ("B2-M8", "guard: the reservation is read on a snapshot taken before the lock (A3m)",
     GUARD, _GUARD_SYNC_OLD, _GUARD_SYNC_NEW),
    ("B2-M9", "guard: the takeover does not take the exclusive K_X (no drain, Dm)", GUARD,
     '        conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (K_X,))\n'
     "        identity = conn.execute(IDENT_SQL).fetchone()\n",
     "        identity = conn.execute(IDENT_SQL).fetchone()\n"),
    ("B2-M10", "guard: guarded sessions are not read-only by default", GUARD,
     '        await conn.execute("SET default_transaction_read_only = on")\n',
     ""),
    ("B2-M11", "guard: any epoch of the same reservation is accepted", GUARD,
     "    if current != expected:\n",
     '    if current.rsplit(":", 1)[0] != expected.rsplit(":", 1)[0]:\n'),
    ("B2-M12", "guard: the reserving transaction does not re-validate freshness", GUARD,
     "        bad = fresh_failures(endpoint, endpoint.target)           # serialized "
     "re-validation\n",
     "        bad = ()                                                  # serialized "
     "re-validation\n"),
    ("B2-M13", "guard: a missing reservation after `stamped` is re-reserved", GUARD,
     '        if phase not in (None, "intent"):\n',
     "        if False:\n"),
    ("B2-M14", "guard: a non-READ COMMITTED transaction is accepted", GUARD,
     '    if iso is None or iso[0] != "read committed":\n'
     '        raise SyntheticRefused("ISOLATION_NOT_READ_COMMITTED")\n'
     "    identity = conn.execute(IDENT_SQL).fetchone()\n",
     "    identity = conn.execute(IDENT_SQL).fetchone()\n"),
    # -- attest ---------------------------------------------------------------------------
    ("B2-M15", "attest: event triggers are not part of the fresh baseline", ATTEST,
     '    "event_trigger": "SELECT count(*) = 0 FROM pg_catalog.pg_event_trigger",\n',
     '    "event_trigger": "SELECT true",\n'),
    # -- provision ------------------------------------------------------------------------
    ("B2-M16", "provision: commit_ack fires BEFORE the COMMIT (a rollback in disguise)",
     PROVISION,
     '        fire(f"phase.{point}.before_commit", {"backend_pid": conn.info.backend_pid})\n'
     '    fire(f"phase.{point}.commit_ack", {"backend_pid": conn.info.backend_pid})\n',
     '        fire(f"phase.{point}.before_commit", {"backend_pid": conn.info.backend_pid})\n'
     '        fire(f"phase.{point}.commit_ack", {"backend_pid": conn.info.backend_pid})\n'),
    ("B2-M17", "provision: an owned login is acknowledged on its name alone", PROVISION,
     "            if (tuple(row[:9]) != _LOGIN_ATTRS or not same\n",
     "            if (False\n"),
    ("B2-M18", "provision: an expired support grant is used anyway before T2", PROVISION,
     '    if t2 == "none" and expired:\n',
     "    if False:\n"),
    ("B2-M19", "provision: the phase is recorded BEFORE its database commit", PROVISION,
     "        lock.check()\n"
     '        if phase != "ready":\n',
     "        lock.check()\n"
     '        state["phase"] = phase\n'
     "        st.write(state)\n"
     '        if phase != "ready":\n'),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("dsn", DSN, UNITS),
    ("state", STATE, UNITS),
    ("guard", GUARD, GUARD_TESTS),
    ("attest", ATTEST, GUARD_TESTS),
    ("provision", PROVISION, INIT_TESTS),
)
GROUP_OF: dict[str, str] = {
    **{m: "dsn" for m in ("B2-M1", "B2-M2", "B2-M3")},
    **{m: "state" for m in ("B2-M4", "B2-M5", "B2-M6")},
    **{f"B2-M{i}": "guard" for i in range(7, 15)},
    "B2-M15": "attest",
    **{m: "provision" for m in ("B2-M16", "B2-M17", "B2-M18", "B2-M19")},
}


def bytecode_env(empty_dir: str) -> dict[str, str]:
    """The W4B-CACHE-1 guard: compile the source on disk for every run."""

    return {"PYTHONPYCACHEPREFIX": empty_dir, "PYTHONDONTWRITEBYTECODE": "1"}


def kill_sets() -> dict[str, frozenset[str]]:
    if not KILLS_FILE.exists():
        return {}
    raw = json.loads(KILLS_FILE.read_text(encoding="utf-8"))
    return {mid: frozenset(nodes) for mid, nodes in raw.items()}


def specs() -> tuple[tuple[str, str, Path, str, str, frozenset[str]], ...]:
    kills = kill_sets()
    return tuple((*edit, kills.get(edit[0], frozenset())) for edit in EDITS)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def in_group(label: str, spec: tuple[Any, ...]) -> bool:
    return GROUP_OF.get(spec[0]) == label


def build_mutants(checker: Any, label: str) -> tuple[Any, ...]:
    return tuple(
        checker.Mutant(spec[0], spec[1], spec[3], spec[4], spec[5])
        for spec in specs()
        if in_group(label, spec)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--group", action="append", default=None,
                        help="run only the named group(s) (local measurement)")
    parser.add_argument("--mutant", action="append", default=None,
                        help="run only the named mutant(s) (local re-measurement)")
    args = parser.parse_args(argv)
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="b2-pycache-")))
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        if args.group is not None and label not in args.group:
            continue
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = tuple(m for m in build_mutants(checker, label)
                                if args.mutant is None or m.id in args.mutant)
        if not checker.MUTANTS:
            continue
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=b2-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
