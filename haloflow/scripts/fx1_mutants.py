"""Incoming fax routing FX-IN FX1 mutation controls, group family ``fx1`` (M-FX1-1..11).

Targets predeclared in the FX1 red packet v1 (test_fx1_postgres docstring; Codex 65d1f9db): the
level order reversed (rules and SQL); rule rows counted instead of distinct destinations; the
duplicate key without the provider; the mismatch digest ignoring the sender; the support
authority accepting any capability; the post-lock authority re-check removed (queue_deactivate
and rule_put); the inactive-queue filter removed; the receipt's shared line fence removed;
the rename re-check removed; a case-sensitive rename collision; the receipt binding
check removed. Kill sets are declared in
scripts/fx1_mutant_kills.json before the CI run.

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/el1_mutants.py: each group's control run must pass completely; a mutant is KILLED only
by a completed run with at least one assertion-level failure, no setup/teardown error, and
every failed node inside its PREDECLARED kill set; the target is restored after every run and
its SHA-256 re-checked. Stale-bytecode guard as W4b.

Usage (from haloflow/):
    python3 -I scripts/fx1_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/fx1-mutation"
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
RULES = Path("src/haloflow/m05/fax_rules.py")
UNITS = Path("src/haloflow/m05/fax_units.py")
UNIT_TESTS = "tests/m05/test_fx1_units.py"
PG_TESTS = "tests/m05/test_fx1_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "fx1_mutant_kills.json"

# (id, description, target, old, new); kill sets in fx1_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M1r", "rules: the level order is reversed", RULES,
     "    for level in (1, 2, 3):\n",
     "    for level in (3, 2, 1):\n"),
    ("M1s", "SQL: the level order is reversed", UNITS,
     "     GROUP BY m.lvl ORDER BY m.lvl LIMIT 1;\n",
     "     GROUP BY m.lvl ORDER BY m.lvl DESC LIMIT 1;\n"),
    ("M2", "SQL: rule rows are counted instead of distinct destinations", UNITS,
     "    SELECT m.lvl, pg_catalog.array_agg(DISTINCT m.queue_id) INTO v_level, v_queues\n",
     "    SELECT m.lvl, pg_catalog.array_agg(m.queue_id) INTO v_level, v_queues\n"),
    ("M3", "the duplicate key drops the provider", UNITS,
     "    CONSTRAINT m05_fx_faxes_identity UNIQUE (provider, provider_fax_id),\n",
     "    CONSTRAINT m05_fx_faxes_identity UNIQUE (provider_fax_id),\n"),
    ("M4", "the mismatch digest ignores the sender", UNITS,
     "    + META_TAG + \"', v_line, COALESCE(v_sender, ''), v_pages::text, v_received_text,\"\n",
     "    + META_TAG + \"', v_line, v_pages::text, v_received_text,\"\n"),
    ("M5", "the support authority accepts any capability", UNITS,
     "       AND '@@CAP@@' = ANY (g.capabilities)\n",
     "       AND true\n"),
    ("M6d", "no authority re-check after the queue_deactivate lock", UNITS,
     "        -- ... and re-checked after the target row lock.\"\"\" + _AUTHORIZE + r\"\"\"\n",
     "        -- ... and re-checked after the target row lock.\"\"\" + r\"\"\"\n"),
    ("M6r", "no authority re-check after the rule_put queue lock", UNITS,
     "        v_found := FOUND;\"\"\" + _AUTHORIZE + r\"\"\"\n        IF NOT v_found THEN\n"
     "            \"\"\" + _raise(\"QUEUE_NOT_FOUND\") + r\"\"\"\n        END IF;\n"
     "        IF v_line IS NOT NULL",
     "        v_found := FOUND;\"\"\" + r\"\"\"\n        IF NOT v_found THEN\n"
     "            \"\"\" + _raise(\"QUEUE_NOT_FOUND\") + r\"\"\"\n        END IF;\n"
     "        IF v_line IS NOT NULL"),
    ("M7", "the inactive-queue filter is removed from routing", UNITS,
     "              JOIN {schema}.m05_fx_queues AS q ON q.queue_id = r.queue_id AND q.active\n",
     "              JOIN {schema}.m05_fx_queues AS q ON q.queue_id = r.queue_id\n"),
    ("M8", "the receipt's shared line fence is removed", UNITS,
     "    PERFORM pg_catalog.pg_advisory_xact_lock_shared(@@KEY@@::bigint);\n",
     "    PERFORM 1;\n"),
    ("M9", "no authority re-check after the queue_rename lock", UNITS,
     "        -- ... and re-checked after the rename lock.\"\"\" + _AUTHORIZE + r\"\"\"\n",
     "        -- ... and re-checked after the rename lock.\"\"\" + r\"\"\"\n"),
    ("M10", "the rename name collision is case-sensitive", UNITS,
     "                    WHERE pg_catalog.lower(q.name) = pg_catalog.lower(v_name)\n"
     "                      AND q.queue_id <> v_target) THEN\n",
     "                    WHERE q.name = v_name\n"
     "                      AND q.queue_id <> v_target) THEN\n"),
    ("M11", "the receipt's tenant binding check is removed (Codex 3e5361ff)", UNITS,
     "BEGIN\"\"\" + _TENANT + BINDING + r\"\"\"\n"
     "    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'doc_ref', 'line', 'pages',"
     " 'provider',\n",
     "BEGIN\"\"\" + _TENANT + r\"\"\"\n"
     "    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'doc_ref', 'line', 'pages',"
     " 'provider',\n"),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="fx1-pycache-")))
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
                                                   f"title=fx1-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
