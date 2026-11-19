"""Eligibility E2 mutation controls, group family ``el2`` (M01r..M14).

Targets predeclared in the E2 red packet v1/v1r1 (Codex red gate PASS b690404c): freshness
on settled_at (SQL) and an exclusive window bound (rules); the attached-return flag lost
(SQL) and the returned-twice refusal dropped (rules); a third dispatch slot allowed; a crash's
dispatched attempt not marked ambiguous at takeover (so it is resent / stays in flight);
settle ignores the epoch; a late answer to a closed attempt applied; expiry keeps the member
digest; an inconclusive payer error mapped to inactive; (green) a returned trigger attaches
again; (green v1r1, Codex 7a2e2b91) the version reset of the return budget, the
write-time retention cutoff for coverage and answers, and the data-based sweep. Kill sets are
declared in scripts/el2_mutant_kills.json before the CI run.

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/el1_mutants.py. Stale-bytecode guard as W4b.

Usage (from haloflow/):
    python3 -I scripts/el2_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/el2-mutation"
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
RESULTS = Path("src/haloflow/m05/eligibility_results.py")
UNITS = Path("src/haloflow/m05/eligibility_worker_units.py")
UNIT_TESTS = "tests/m05/test_el2_units.py"
PG_TESTS = "tests/m05/test_el2_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "el2_mutant_kills.json"

# (id, description, target, old, new); kill sets in el2_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M01r", "rules: freshness excludes an observation exactly at the window start", RESULTS,
     "            and observed_at is not None and observed_at >= window_start)\n",
     "            and observed_at is not None and observed_at > window_start)\n"),
    ("M01s", "SQL: freshness decided on settled_at instead of observed_at", UNITS,
     "                   AND {schema}.m05_el_fresh(v.observed_at, v_ws)\n",
     "                   AND {schema}.m05_el_fresh(v.settled_at, v_ws)\n"),
    ("M02r", "rules: an attached trigger may be returned twice", RESULTS,
     "    if returned:\n"
     "        raise ValueError(\"an attached trigger is returned at most once\")\n",
     "    if returned:\n        pass\n"),
    ("M02s", "SQL: the return does not record `returned`", UNITS,
     "               SET status = 'pending', verification_id = NULL, returned = true,\n",
     "               SET status = 'pending', verification_id = NULL, returned = false,\n"),
    ("M03", "a third dispatch slot is allowed", UNITS,
     "    IF v_count >= 2 THEN\n",
     "    IF v_count >= 3 THEN\n"),
    ("M04", "takeover leaves a dispatched attempt in flight (it would be resent)", UNITS,
     "        UPDATE {schema}.m05_el_attempts SET state = 'ambiguous', finished_at = v_now\n"
     "         WHERE verification_id = v_vid AND state = 'dispatched';\n",
     "        PERFORM 1;\n"),
    ("M05", "settle ignores the epoch", UNITS,
     "    IF v_v.epoch <> v_epoch THEN\n        \"\"\" + _ret(\"fenced\") + r\"\"\"\n"
     "    END IF;\n    IF v_v.state = 'settled' THEN\n",
     "    IF false THEN\n        \"\"\" + _ret(\"fenced\") + r\"\"\"\n"
     "    END IF;\n    IF v_v.state = 'settled' THEN\n"),
    ("M06", "a late answer to a closed attempt is applied", UNITS,
     "    IF v_at.state <> 'dispatched' THEN\n\"\"\" + _late(\"closed_attempt\"",
     "    IF false THEN\n\"\"\" + _late(\"closed_attempt\""),
    ("M07", "expiry keeps the member digest", UNITS,
     "        UPDATE {schema}.m05_el_verifications SET member_digest = NULL\n",
     "        UPDATE {schema}.m05_el_verifications SET member_digest = member_digest\n"),
    ("M08", "an inconclusive payer error is mapped to inactive", RESULTS,
     "        out = {\"class\": \"inconclusive\", \"reason\": error}\n",
     "        out = {\"class\": \"inactive\", \"reason\": error}\n"),
    ("M09", "a returned trigger attaches again (green)", UNITS,
     "               AND NOT v_t.returned\n",
     "\n"),
    ("M10", "the return budget is not reset on a new coverage version (green v1r1)", UNITS,
     "    IF NEW.coverage_version IS DISTINCT FROM OLD.coverage_version THEN\n"
     "        NEW.returned := false;\n",
     "    IF NEW.coverage_version IS DISTINCT FROM OLD.coverage_version THEN\n"
     "        NEW.returned := NEW.returned;\n"),
    ("M11", "a coverage write after the cutoff keeps identifiers (green v1r1)", UNITS,
     "    IF {schema}.el_past_cutoff(NEW.appointment_ref) THEN\n        NEW.member_id := NULL;\n",
     "    IF false THEN\n        NEW.member_id := NULL;\n"),
    ("M12", "the sweep selects by an expiry stamp, not by remaining data (green v1r1)", UNITS,
     "         WHERE a.zone IS NOT NULL\n           AND (EXISTS (",
     "         WHERE a.zone IS NOT NULL AND a.expired_at IS NULL\n           AND (EXISTS ("),
    ("M13", "an answer written after the cutoff keeps benefit fields (green v1r1)", UNITS,
     "    IF NEW.answer IS NOT NULL AND {schema}.el_past_cutoff((\n",
     "    IF false AND {schema}.el_past_cutoff((\n"),
    ("M14", "a verification created after the cutoff stores the digest (green v1r1)", UNITS,
     "                CASE WHEN {schema}.el_past_cutoff(v_k.appointment_ref) THEN NULL\n",
     "                CASE WHEN false THEN NULL\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("results", RESULTS, UNIT_TESTS),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="el2-pycache-")))
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
                                                   f"title=el2-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
