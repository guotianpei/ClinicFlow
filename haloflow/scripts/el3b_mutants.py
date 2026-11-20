"""Eligibility E3b mutation controls, group family ``el3b`` (M01, M02, M03, M03w, M04, M06).

Targets predeclared in the E3b red packet v1 (test_el3b_postgres.py docstring, test cases v1
E3-M rows): the deferred OD-EL6 note rule moved before the item lock / kind check (M01); the
(kind, code) membership check skipped (M02); the projection skipped - the token compared
against the unprojected case (M03); the closure watermark := next_seq (M03w, the C2 rule);
CASE_CHANGED rolls back its projection (M04); log_contact allowed on a legacy kind (M06).
Kill sets in scripts/el3b_mutant_kills.json (measured at green, each a superset-free
declaration of the failing rows).

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/el3a_mutants.py. Stale-bytecode guard as W4b.

Usage (from haloflow/):
    python3 -I scripts/el3b_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/el3b-mutation"
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
UNITS = Path("src/haloflow/m05/eligibility_staff_units.py")
PG_TESTS = "tests/m05/test_el3b_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "el3b_mutant_kills.json"

# (id, description, target, old, new); kill sets in el3b_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M01", "the OD-EL6 note rule runs before the item lock and the kind check", UNITS,
     '"           AND v_outcome IN (" + _W1_SQL + ") THEN")',
     '"           AND true THEN")'),
    ("M02", "the (kind, code) membership check skipped", UNITS,
     '"         WHERE ok.item_kind OPERATOR(pg_catalog.=) v_row.item_kind\\n"',
     '"         WHERE true OR ok.item_kind OPERATOR(pg_catalog.=) v_row.item_kind\\n"'),
    ("M03", "the projection skipped (token compared with the unprojected case)", UNITS,
     '"        v_proj := {schema}.m05_el_project(pg_catalog.jsonb_build_object(\\n"',
     '"        v_proj := pg_catalog.jsonb_build_object(\'evidence\', \'[]\'::jsonb,"\n'
     '        " \'skipped\', pg_catalog.jsonb_build_object(\\n"'),
    ("M03w", "the closure watermark := next_seq (not next_seq - 1)", UNITS,
     '"               SET closure_watermark = a.next_seq OPERATOR(pg_catalog.-) 1\\n"',
     '"               SET closure_watermark = a.next_seq\\n"'),
    ("M04", "CASE_CHANGED rolls back its projection", UNITS,
     '"                \'decision\', \'CASE_CHANGED\',\\n"',
     '"                \'decision\', \'CASE_CHANGED\', \'rollback\', 1 / 0,\\n"'),
    ("M06", "log_contact allowed on a legacy kind", UNITS,
     '"    ELSIF v_tok IS NOT NULL OR v_type = \'log_contact\' THEN\\n"',
     '"    ELSIF v_tok IS NOT NULL AND v_type <> \'log_contact\' THEN\\n"'),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="el3b-pycache-")))
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
                                                   f"title=el3b-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
