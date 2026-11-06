"""L-1S package S-B mutation controls, group family ``sb_due`` (M1..M11).

Predeclared in the S-B red package v1 (red gate PASS 22be664, Codex
20261106T144748Z) with M5's killer strengthened by SB-W03b (disclosed). Judged by the
shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/sa_intake_mutants.py: each group's control run must pass completely; a mutant is
KILLED only by a completed run with at least one assertion-level failure, no
setup/teardown error, and every failed node inside its PREDECLARED kill set; the target
is restored after every run and its SHA-256 re-checked. Stale-bytecode guard as W4b.

Targets: haloflow.m05.due_units (the t045 SQL, built at import) and
haloflow.m05.due_windows (the runner). Kill sets live in scripts/sb_due_mutant_kills.json;
the first CI measurement confirms or reports them; any change is disclosed.

Usage (from haloflow/):
    python3 -I scripts/sb_due_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/sb-due-mutation"
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
UNITS = Path("src/haloflow/m05/due_units.py")
RUNNER = Path("src/haloflow/m05/due_windows.py")
SELECTOR_TESTS = "tests/m05/test_sb_selector_postgres.py"
RUNNER_TESTS = "tests/m05/test_sb_runner_postgres.py"
CANCEL_TESTS = "tests/m05/test_sb_cancellation_postgres.py"
STATIC_TESTS = "tests/m05/test_sb_static_units.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "sb_due_mutant_kills.json"

# (id, description, target, old, new); kill sets in sb_due_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M1", "the deadline instant is not due (strict <)", UNITS,
     'DUE_PREDICATE: Final = "w.deadline OPERATOR(pg_catalog.<=) v_cutoff"\n',
     'DUE_PREDICATE: Final = "w.deadline OPERATOR(pg_catalog.<) v_cutoff"\n'),
    ("M2", "selection ignores the window state", UNITS,
     "    \" WHERE w.state OPERATOR(pg_catalog.=) 'awaiting' AND \" + DUE_PREDICATE\n",
     "    \" WHERE \" + DUE_PREDICATE\n"),
    ("M3", "selection has no LIMIT", UNITS,
     '_ORDER: Final = " ORDER BY w.deadline, w.reminder_id LIMIT v_limit"\n',
     '_ORDER: Final = " ORDER BY w.deadline, w.reminder_id"\n'),
    ("M4", "the until bound is dropped", UNITS,
     '    "until": _SELECT + _UNTIL + _ORDER,\n'
     '    "after_until": _SELECT + _AFTER + _UNTIL + _ORDER,\n',
     '    "until": _SELECT + _ORDER,\n'
     '    "after_until": _SELECT + _AFTER + _ORDER,\n'),
    ("M5", "phase B is not bounded by the start key", RUNNER,
     "                                          start if phase_b else None)\n",
     "                                          None)\n"),
    ("M6", "a phase-B selection failure resets the resume key", RUNNER,
     "            except _SelectionFailed as failure:\n"
     "                return failure.code\n",
     "            except _SelectionFailed as failure:\n"
     "                if phase_b:\n"
     "                    self._resume.pop(tenant, None)\n"
     "                return failure.code\n"),
    ("M7", "the cap is checked only after fetching a page", RUNNER,
     "        while True:\n"
     "            if counts.attempted >= self._cap:\n"
     '                return "CAP"\n',
     "        while True:\n"),
    ("M8", "constructor accepts a capability superset", RUNNER,
     "        if principal.capabilities != TIMER_CAPS:\n",
     "        if not TIMER_CAPS <= principal.capabilities:\n"),
    ("M9", "pending cancellation is ignored", RUNNER,
     "    if task is not None and task.cancelling() > 0:\n"
     "        raise asyncio.CancelledError()\n",
     "    if task is None:\n"
     "        return\n"),
    ("M10", "busy is not cleared in finally", RUNNER,
     "        finally:\n"
     "            self._busy.discard(tenant_id)\n",
     "        finally:\n"
     "            pass\n"),
    ("M11", "busy is never marked before I/O", RUNNER,
     "        self._busy.add(tenant_id)\n"
     "        counts = _Counts()\n",
     "        counts = _Counts()\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("selector", UNITS, SELECTOR_TESTS),
    ("runner", RUNNER, RUNNER_TESTS),
    ("cancellation", RUNNER, CANCEL_TESTS),
    ("static", RUNNER, STATIC_TESTS),
)


def bytecode_env(empty_dir: str) -> dict[str, str]:
    """The W4B-CACHE-1 guard: compile the source on disk for every run."""

    return {"PYTHONPYCACHEPREFIX": empty_dir, "PYTHONDONTWRITEBYTECODE": "1"}


def kill_sets() -> dict[str, frozenset[str]]:
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


def in_group(target: Path, tests: str, spec: tuple[Any, ...]) -> bool:
    return bool(spec[2] == target and spec[5]
                and all(n.startswith(tests + "::") for n in spec[5]))


def build_mutants(checker: Any, target: Path, tests: str) -> tuple[Any, ...]:
    return tuple(
        checker.Mutant(spec[0], spec[1], spec[3], spec[4], spec[5])
        for spec in specs()
        if in_group(target, tests, spec)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="sb-due-pycache-")))
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = build_mutants(checker, target, tests)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=sb-due-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
