"""L-1S package S-H H1 mutation controls, group family ``sh_h1`` (M1..M12).

Predeclared with the H1 red tests (relay 20261108T143131Z, Codex review of 9016dea and
the target-56 correction): the frozen-start comparison dropped; lease expiry returns an
op to prepared; outcome skips the attempt check; register remaps an identity change;
the unresolved-correlation guard dropped (v2); the guard ignores uncertain and
accepted_unregistered; the registered-replay return removed; outcome bookkeeping made
activation-protected; read ignores the lease; the worker sends after losing its claim.
Added with the R1 correction (Codex source review of 35b2346): D1 ignores a
pre-existing reminder window; prepare accepts a reminder id already in use.
Kill sets are declared in scripts/sh_h1_mutant_kills.json before the CI run.
Disclosed correction after CI 42494723730 / 42496294980: M1's declared set {P03, P04
[start_moved]} was incomplete. sh_support.to_state reaches 'abandoned' and
'accepted_unregistered' by moving the start, so six rows that use it (P09
[accepted_unregistered-0/1/2], P10[abandoned], P11[accepted_unregistered], P17) also
fail under M1, with an assertion-level failure in the helper. The set now lists all
eight measured nodes; the code and the tests are unchanged.

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/sc_activation_mutants.py: each group's control run must pass completely; a
mutant is KILLED only by a completed run with at least one assertion-level failure, no
setup/teardown error, and every failed node inside its PREDECLARED kill set; the target
is restored after every run and its SHA-256 re-checked. Stale-bytecode guard as W4b.

Usage (from haloflow/):
    python3 -I scripts/sh_h1_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/sh-h1-mutation"
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
UNITS = Path("src/haloflow/m05/outbound_units.py")
STATEMENTS = Path("src/haloflow/m05/outbound_statements.py")
WORKER = Path("src/haloflow/m05/outbound.py")
UNIT_TESTS = "tests/m05/test_sh_h1_units.py"
PG_TESTS = "tests/m05/test_sh_h1_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "sh_h1_mutant_kills.json"

# (id, description, target, old, new); kill sets in sh_h1_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M1", "D1 drops the frozen-start comparison", UNITS,
     "        ELSIF v_appt.status <> 'booked' OR v_appt.start_at <> v_o.start_at THEN\n",
     "        ELSIF v_appt.status <> 'booked' THEN\n"),
    ("M2", "lease expiry returns the op to prepared", UNITS,
     "       SET state = 'uncertain', code = 'LEASE_EXPIRED',",
     "       SET state = 'prepared', code = 'LEASE_EXPIRED',"),
    ("M3", "outcome skips the attempt_id check", UNITS,
     "    IF v_o.attempt_id IS DISTINCT FROM v_attempt THEN\n"
     "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STALE_ATTEMPT';\n"
     "    END IF;\n",
     ""),
    ("M4", "register remaps an identity change (t039 re-derives patient/phone)", UNITS,
     "    IF v_code IS NOT NULL THEN  -- never remap: hold with the code; correlation stays open\n",
     "    IF v_code = 'IDENTITY_CHANGED' THEN\n"
     "        v_code := NULL;\n"
     "    END IF;\n"
     "    IF v_code IS NOT NULL THEN  -- never remap: hold with the code;"
     " correlation stays open\n"),
    ("M5", "the v2 unresolved-correlation guard is dropped", UNITS,
     '    "               AND NOT EXISTS (SELECT 1 FROM {schema}.m05_outbound_ops AS o\\n"\n'
     '    "                                WHERE o.phone = v_phone AND o.correlation_open);\\n"\n',
     '    "               AND true;\\n"\n'),
    ("M6", "the guard ignores uncertain and accepted_unregistered ops", UNITS,
     '    "                                WHERE o.phone = v_phone AND o.correlation_open);\\n"\n',
     '    "                                WHERE o.phone = v_phone AND o.correlation_open\\n"\n'
     '    "                                  AND o.state IN'
     ' (\'attempting\', \'accepted\'));\\n"\n'),
    ("M7", "the stored-registration replay return is removed", UNITS,
     "    IF v_o.state = 'registered' THEN  -- pin 4: a stored success wins on retry\n"
     "        RETURN v_o.registered_result\n"
     "            OPERATOR(pg_catalog.||) pg_catalog.jsonb_build_object('outcome', 'existing');\n"
     "    END IF;\n",
     ""),
    ("M8", "outcome bookkeeping is activation-protected", STATEMENTS,
     "SH_PROTECTED: Final[frozenset[str]] = frozenset({PREPARE_KEY, CLAIM_KEY, REGISTER_KEY})\n",
     "SH_PROTECTED: Final[frozenset[str]] = frozenset({PREPARE_KEY, CLAIM_KEY, REGISTER_KEY,\n"
     "                                                 OUTCOME_KEY})\n"),
    ("M9", "read ignores the lease", UNITS,
     "       OR v_o.lease_until OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN\n",
     "       THEN\n"),
    ("M11", "D1 ignores a pre-existing window for the reminder (R1)", UNITS,
     "    IF v_code IS NULL AND EXISTS (SELECT 1 FROM {schema}.m05_reminder_windows AS w\n"
     "                                   WHERE w.reminder_id = v_o.reminder_id) THEN\n"
     "        v_code := 'REMINDER_IN_USE';\n"
     "    END IF;\n",
     ""),
    ("M12", "prepare accepts a reminder id already in use (R1)", UNITS,
     "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'REMINDER_IN_USE';\n",
     "        NULL;\n"),
    ("M10", "the worker sends after losing its claim", WORKER,
     '            return "not_claimed"\n',
     '            claimed = {"outcome": "claimed", "attempt_id": "lost"}\n'),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("units", UNITS, PG_TESTS),
    ("statements", STATEMENTS, PG_TESTS),
    ("worker", WORKER, UNIT_TESTS),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="sh-h1-pycache-")))
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
                                                   f"title=sh-h1-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
