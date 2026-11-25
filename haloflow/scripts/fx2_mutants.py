"""Incoming fax routing FX-IN FX2 mutation controls, group family ``fx2`` (M-FX2-1..10).

Targets predeclared in the FX2 red packet v1 (test_fx2_postgres docstring): the version check
removed; the filed guard removed (OD-FX8); the sort-once guard removed; the target queue's
active check removed; the provisioner's try-lock made blocking; line uniqueness checked per
tenant only; the staff check removed; the receipt fence removed (FX1 M8, re-killed by the
FX2 provisioning rows); the version checked before the state guard; act authorizing without
read (M10, Codex 4eabf042). The in-use check removed is NOT declared: the rule foreign key is a
deliberate backstop yielding the identical atomic LINE_IN_USE (equivalent mutant, evidenced
separately in the FX2 green packet). Kill sets in scripts/fx2_mutant_kills.json.

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/fx1_mutants.py. Usage (from haloflow/):
    python3 -I scripts/fx2_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/fx2-mutation"
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
STAFF = Path("src/haloflow/m05/fax_staff_units.py")
LINES = Path("src/haloflow/synthetic/fax_lines.py")
FENCE = Path("src/haloflow/m05/fax_units.py")
PG_TESTS = "tests/m05/test_fx2_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "fx2_mutant_kills.json"

_VERSION_SQL = (
    "    v_version := (p_command OPERATOR(pg_catalog.->>) 'expected_version')::integer;\n"
    "    IF v_fax.version <> v_version THEN\n"
    "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'VERSION_CONFLICT';\n"
    "    END IF;\n")

# (id, description, target, old, new); kill sets in fx2_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M1", "the version check is removed", STAFF,
     "    IF v_fax.version <> v_version THEN\n",
     "    IF false THEN\n"),
    ("M2", "the filed guard is removed (OD-FX8)", STAFF,
     "    ELSIF v_fax.state = 'filed' THEN\n",
     "    ELSIF false THEN\n"),
    ("M3", "the sort-once guard is removed", STAFF,
     "        IF v_sort.state = 'resolved' THEN\n",
     "        IF false THEN\n"),
    ("M4", "the target queue's active check is removed", STAFF,
     "          WHERE q.queue_id = v_queue AND q.active FOR SHARE;\n",
     "          WHERE q.queue_id = v_queue FOR SHARE;\n"),
    ("M5", "the provisioner's try-lock is made blocking", LINES,
     '                locked = conn.execute("SELECT pg_try_advisory_xact_lock(%s)",\n',
     '                locked = conn.execute("SELECT pg_advisory_xact_lock(%s) IS NULL OR true",\n'),
    ("M6", "line uniqueness is checked per tenant only", LINES,
     "                        if item.line in owners:\n",
     "                        if owners.get(item.line) == item.tenant_id:\n"),
    ("M7", "the staff check is removed from actions", STAFF,
     "    IF NOT {schema}.m05_fx_staff_check(v_staff,\n"
     "            CASE WHEN v_action = 'open' THEN 'read' ELSE 'act' END) THEN\n",
     "    IF false AND {schema}.m05_fx_staff_check(v_staff,\n"
     "            CASE WHEN v_action = 'open' THEN 'read' ELSE 'act' END) THEN\n"),
    ("M8", "the receipt's shared line fence is removed (FX1 M8)", FENCE,
     "    PERFORM pg_catalog.pg_advisory_xact_lock_shared(@@KEY@@::bigint);\n",
     "    PERFORM 1;\n"),
    ("M9", "the version is checked before the state guard", STAFF,
     "    -- 3. the state guard (before the version: test cases v1r1 §1).\n",
     _VERSION_SQL + "    -- 3. the state guard (before the version: test cases v1r1 §1).\n"),
    ("M10", "act authorizes without read (Codex 4eabf042)", STAFF,
     "                    WHERE a.staff_id = p_staff_id AND a.access = 'read')\n",
     "                    WHERE a.staff_id = p_staff_id AND a.access = 'read'\n"
     "                       OR p_access = 'act')\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("staff", STAFF, PG_TESTS),
    ("lines", LINES, PG_TESTS),
    ("fence", FENCE, PG_TESTS),
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
    parser.add_argument("--group", action="append", default=None,
                        help="run only the named group(s) (local measurement)")
    args = parser.parse_args(argv)
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="fx2-pycache-")))
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        if args.group is not None and label not in args.group:
            continue
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = build_mutants(checker, target, tests)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=fx2-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
