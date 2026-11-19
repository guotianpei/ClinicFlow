"""Eligibility E1 mutation controls, group family ``el1`` (M01r..M11).

Targets predeclared in the E1 red packet v1r1 §3 (Codex red gate PASS 61b5336d): due drops
the time of day (rules and SQL); lead off by one (rules and SQL); omission treated as `none`;
counts keyed without the appointment; pending opens a case at booking; seq read before the
key lock; seam `<` instead of `<=`; tick compares outside the seam; a guessed zone when none
is usable; (green review feb1fea9) a JSON-null relationship read as complete and a corrected
department zone not adopted. Kill sets are declared in scripts/el1_mutant_kills.json before
the CI run.

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/sc_activation_mutants.py: each group's control run must pass completely; a mutant is
KILLED only by a completed run with at least one assertion-level failure, no setup/teardown
error, and every failed node inside its PREDECLARED kill set; the target is restored after
every run and its SHA-256 re-checked. Stale-bytecode guard as W4b.

Usage (from haloflow/):
    python3 -I scripts/el1_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/el1-mutation"
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
RULES = Path("src/haloflow/m05/eligibility_rules.py")
UNITS = Path("src/haloflow/m05/eligibility_units.py")
UNIT_TESTS = "tests/m05/test_el1_units.py"
PG_TESTS = "tests/m05/test_el1_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "el1_mutant_kills.json"

# (id, description, target, old, new); kill sets in el1_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M01r", "rules: the pre-visit instant drops the visit's time of day", RULES,
     "    return _resolve_local(local.date() - timedelta(days=days), local.time().replace("
     "tzinfo=None),\n",
     "    return _resolve_local(local.date() - timedelta(days=days), time(0, 0),\n"),
    ("M01s", "SQL: the pre-visit instant drops the visit's time of day", UNITS,
     '             f" (pg_catalog.timezone({zone}, {start}))::time")\n',
     '             f" time \'00:00\'")\n'),
    ("M02r", "rules: lead days off by one", RULES,
     "    return _days_before(start, zone, check_lead_days(lead_days))\n",
     "    return _days_before(start, zone, check_lead_days(lead_days) + 1)\n"),
    ("M02s", "SQL: lead days off by one", UNITS,
     '        v_pre := """ + _days_before("v_k.start_at", "v_k.zone", "v_s.lead_days") + r""";\n',
     '        v_pre := """ + _days_before("v_k.start_at", "v_k.zone", "v_s.lead_days + 1")'
     ' + r""";\n'),
    ("M03", "an omitted coverage block is treated as `none`", UNITS,
     "    v_cov := CASE WHEN v_has THEN p_command OPERATOR(pg_catalog.->) 'coverage' END;\n",
     "    v_cov := CASE WHEN v_has THEN p_command OPERATOR(pg_catalog.->) 'coverage'\n"
     "                  ELSE '{\"state\": \"none\"}'::jsonb END;\n"
     "    v_has := true;\n"),
    ("M04", "counts keyed without the appointment", UNITS,
     "    PRIMARY KEY (period, bucket, payer_key, appointment_ref)\n",
     "    PRIMARY KEY (period, bucket, payer_key)\n"),
    ("M05", "pending coverage opens a manual case at booking", UNITS,
     "        IF v_c.state = 'none' THEN\n            v_reason := 'no_coverage';\n",
     "        IF v_c.state IN ('none', 'pending') THEN\n            v_reason := 'no_coverage';\n"),
    ("M06", "the put reads the key row (next_seq) without its lock", UNITS,
     "     WHERE a.appointment_ref = v_ref FOR UPDATE;\n    IF NOT FOUND THEN\n",
     "     WHERE a.appointment_ref = v_ref;\n    IF NOT FOUND THEN\n"),
    ("M07", "the deadline seam uses < instead of <=", UNITS,
     "    RETURN p_due <= p_now;\n",
     "    RETURN p_due < p_now;\n"),
    ("M08", "the tick compares the deadline outside the seam", UNITS,
     "           AND {schema}.m05_el_deadline_passed(a.not_received_due_at, v_now)\n",
     "           AND a.not_received_due_at <= v_now\n"),
    ("M10", "a JSON-null relationship falls through as complete (green review item 1)", UNITS,
     "             WHEN COALESCE(v_cov OPERATOR(pg_catalog.->>) 'relationship', '')\n",
     "             WHEN (v_cov OPERATOR(pg_catalog.->>) 'relationship')\n"),
    ("M11", "a corrected department zone is not adopted (green review item 2)", UNITS,
     "        IF v_zone IS NOT NULL AND v_zone IS DISTINCT FROM v_k.zone THEN\n",
     "        IF false THEN\n"),
    ("M09", "a zone is guessed (UTC) when none is usable", UNITS,
     '    return f"COALESCE({ok(dept)}, {ok(clinic)})"\n',
     '    return f"COALESCE({ok(dept)}, {ok(clinic)}, \'UTC\')"\n'),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("rules", RULES, UNIT_TESTS),
    ("units", UNITS, PG_TESTS),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="el1-pycache-")))
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
                                                   f"title=el1-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
