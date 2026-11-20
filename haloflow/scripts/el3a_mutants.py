"""Eligibility E3a mutation controls, group family ``el3a`` (M05, M07, M09..M12).

Targets predeclared in the E3a red packet v1/v1r1 (Codex red gate PASS e4e7aadd): a note-only
or active finding overwrites the case's current reason (M05); the case view leaks
coinsurance (M07); the evidence observation is skipped for an applied event with no effect
(M09); applier rule 2 (older coverage version) skipped (M10); rule 3 (closure watermark)
skipped (M11); an E1 manual settlement not resolved to its booking verification (M12). Kill
sets are declared in scripts/el3a_mutant_kills.json before the CI run.

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/el2_mutants.py. Stale-bytecode guard as W4b.

Usage (from haloflow/):
    python3 -I scripts/el3a_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/el3a-mutation"
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
CASES = Path("src/haloflow/m05/eligibility_cases.py")
UNITS = Path("src/haloflow/m05/eligibility_case_units.py")
UNIT_TESTS = "tests/m05/test_el3a_units.py"
PG_TESTS = "tests/m05/test_el3a_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "el3a_mutant_kills.json"

# (id, description, target, old, new); kill sets in el3a_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M05", "a note-only or active finding overwrites the current reason", UNITS,
     "THEN v_e.reason ELSE c.current_reason END\n",
     "THEN v_e.reason ELSE v_e.reason END\n"),
    ("M07", "the case view summary leaks coinsurance", UNITS,
     '                         "referral_required", "auth_required")\n',
     '                         "referral_required", "auth_required", "coinsurance")\n'),
    ("M09", "no evidence observation for an applied event with no effect", CASES,
     "                await self.__foundation.append_observation_in(binding, observation)\n",
     "                if effect != \"none\":\n"
     "                    await self.__foundation.append_observation_in(binding, observation)\n"),
    ("M10", "applier rule 2 skipped (older coverage version)", UNITS,
     "    IF v_e.coverage_version < v_k.coverage_version THEN\n",
     "    IF false THEN\n"),
    ("M11", "applier rule 3 skipped (closure watermark)", UNITS,
     "    ELSIF v_e.seq <= v_k.closure_watermark THEN\n",
     "    ELSIF false THEN\n"),
    ("M12", "an E1 manual settlement is not resolved to its booking verification", UNITS,
     "        v_record := CASE WHEN v_vid IS NULL THEN 'finding' ELSE 'settlement' END;\n",
     "        v_vid := NULL;\n        v_record := 'finding';\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("service", CASES, UNIT_TESTS),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="el3a-pycache-")))
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
                                                   f"title=el3a-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
