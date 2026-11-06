"""L-1S package S-A mutation controls, group family ``sa_intake`` (M1..M17).

Predeclared in the red package (v1 section 4, v1r1, v1r2; red gate PASS db07570,
Codex 20261106T003822Z). Judged by the shared part-A checker
(scripts/m02_observation_mutants.py) exactly as scripts/l1w_w4b_mutants.py: each
group's control run must pass completely; a mutant is KILLED only by a completed run
with at least one assertion-level failure, no setup/teardown error, and every failed
node inside its PREDECLARED kill set; the target is restored after every run and its
SHA-256 re-checked. Stale-bytecode guard as W4b (W4B-CACHE-1).

Targets: haloflow.m05.intake_units (the unit SQL, built at import, so a mutated body
is what the S-A test registry installs) and haloflow.m05.intake (the service).
Every group runs ONE PostgreSQL 17 module; measured in CI only (this container has no
PostgreSQL 17). Kill sets live in scripts/sa_intake_mutant_kills.json; the first CI
measurement confirms or reports them, and any change is disclosed, never silent.

Usage (from haloflow/):
    python3 -I scripts/sa_intake_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/sa-intake-mutation"
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
UNITS = Path("src/haloflow/m05/intake_units.py")
INTAKE = Path("src/haloflow/m05/intake.py")
DIRECTORY_TESTS = "tests/m05/test_sa_directory_dispatch_postgres.py"
INBOUND_TESTS = "tests/m05/test_sa_inbound_postgres.py"
TIMER_TESTS = "tests/m05/test_sa_timer_interleavings_postgres.py"
SERVICE_TESTS = "tests/m05/test_sa_service_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "sa_intake_mutant_kills.json"

_T040_LOCK = (
    '    v_class := p_command OPERATOR(pg_catalog.->>) \'reply_class\';\n'
    '""" + _contact_lock("v_phone", "v_opted") + r"""\n'
    "    v_received := pg_catalog.clock_timestamp();  -- AFTER the lock (SA-TIME-1)\n"
)
_T041_LOCK = (
    '""" + _contact_lock("v_phone", "v_opted") + r"""\n'
    "    SELECT * INTO v_w FROM {schema}.m05_reminder_windows AS w\n"
    "     WHERE w.reminder_id = v_reminder FOR UPDATE;\n"
)
_DIR_LOCK = (
    "    INSERT INTO {schema}.m05_contact_state (phone, opted_out, opted_out_at)\n"
    "    SELECT p, false, NULL FROM pg_catalog.unnest(v_locked) AS p ORDER BY p\n"
    "        ON CONFLICT (phone) DO NOTHING;\n"
    "    PERFORM 1 FROM {schema}.m05_contact_state AS c\n"
    "      WHERE c.phone = ANY (v_locked) ORDER BY c.phone FOR UPDATE;\n"
)

# (id, description, target, old, new); kill sets in sa_intake_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M1", "inbound takes no contact lock (row read without FOR UPDATE)", UNITS, _T040_LOCK,
     '    v_class := p_command OPERATOR(pg_catalog.->>) \'reply_class\';\n'
     '""" + _contact_lock("v_phone", "v_opted").replace(" FOR UPDATE", "") + r"""\n'
     "    v_received := pg_catalog.clock_timestamp();  -- AFTER the lock (SA-TIME-1)\n"),
    ("M2", "timer takes no contact lock", UNITS, _T041_LOCK,
     "    SELECT * INTO v_w FROM {schema}.m05_reminder_windows AS w\n"
     "     WHERE w.reminder_id = v_reminder FOR UPDATE;\n"),
    ("M3", "received_at read before the contact lock", UNITS, _T040_LOCK,
     '    v_class := p_command OPERATOR(pg_catalog.->>) \'reply_class\';\n'
     "    v_received := pg_catalog.clock_timestamp();\n"
     '""" + _contact_lock("v_phone", "v_opted") + r"""\n'),
    ("M4", "ambiguous reply marks candidate windows replied", UNITS,
     "                -- D-S2 row 7 / v1r1 section 1: never a guess, no window change.\n"
     "                v_kind := 'unclear_reply';\n",
     "                UPDATE {schema}.m05_reminder_windows AS w SET state = 'replied'\n"
     "                 WHERE w.reminder_id = ANY (v_candidates) AND w.state = 'awaiting';\n"
     "                v_kind := 'unclear_reply';\n"),
    ("M5", "STOP leaves the phone's windows untouched", UNITS,
     "        UPDATE {schema}.m05_reminder_windows AS w SET state = 'opted_out'\n"
     "         WHERE w.phone = v_phone AND w.state = 'awaiting'\n"
     "           AND {schema}.intake_reply_is_timely(v_received, w.deadline);\n",
     ""),
    ("M6", "inbound fingerprint includes received_at", UNITS,
     "        || ',' || pg_catalog.to_json(v_class)::text || ']', 'UTF8'));\n",
     "        || ',' || pg_catalog.to_json(v_class)::text\n"
     "        || ',' || pg_catalog.to_json(v_received)::text || ']', 'UTF8'));\n"),
    ("M7", "timer ignores the opt-out", UNITS,
     "    IF v_opted THEN\n"
     "        UPDATE {schema}.m05_reminder_windows AS w\n"
     "           SET state = 'opted_out', closed_decision = 'opted_out'\n",
     "    IF false THEN\n"
     "        UPDATE {schema}.m05_reminder_windows AS w\n"
     "           SET state = 'opted_out', closed_decision = 'opted_out'\n"),
    ("M8", "item created in a separate transaction", INTAKE,
     "                item = await self._create(binding, context, event, str(kind))\n",
     "                item = await self.__foundation.run_in_transaction(\n"
     "                    context, lambda b: self._create(b, context, event, str(kind)))\n"),
    ("M9", "a send added to dispatch", INTAKE,
     "            return DispatchResult(str(row[\"outcome\"]), reminder, patient, event)\n",
     "            import haloflow.integrations.notifyre as _sms\n"
     "            await _sms.NotifyreClient().send_sms(\"+15550100001\", \"reminder\")\n"
     "            return DispatchResult(str(row[\"outcome\"]), reminder, patient, event)\n"),
    ("M10", "boundary predicate uses <=", UNITS,
     "    AS $body$ SELECT $1 OPERATOR(pg_catalog.<) $2 $body$;\n",
     "    AS $body$ SELECT $1 OPERATOR(pg_catalog.<=) $2 $body$;\n"),
    ("M11", "dispatch checks the opt-out before the existing-reminder lookup", UNITS,
     "    -- Existing reminder FIRST (M11): its stored original wins, opted out or not.\n",
     "    IF EXISTS (SELECT 1 FROM {schema}.m05_contact_state AS c\n"
     "                JOIN {schema}.m05_reminder_windows AS w2 ON w2.phone = c.phone\n"
     "               WHERE w2.reminder_id = v_reminder AND c.opted_out) THEN\n"
     "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTACT_OPTED_OUT';\n"
     "    END IF;\n"),
    ("M12", "dispatch fingerprint includes the routed phone", UNITS,
     "        || ',' || pg_catalog.to_json(v_ref::text)::text || ']', 'UTF8'));\n",
     "        || ',' || pg_catalog.to_json(v_ref::text)::text || ',' || pg_catalog.to_json((\n"
     "            SELECT min(c.phone::text) FROM {schema}.m05_patient_contacts AS c\n"
     "              JOIN {schema}.m05_appointments AS a ON a.patient_id = c.patient_id\n"
     "             WHERE a.appointment_ref = v_ref))::text || ']', 'UTF8'));\n"),
    ("M13", "directory write takes no contact locks", UNITS, _DIR_LOCK, ""),
    ("M14", "sender identity read from windows instead of the directory", UNITS,
     "        SELECT pg_catalog.array_agg(c.patient_id ORDER BY c.patient_id) INTO v_patients\n"
     "          FROM {schema}.m05_patient_contacts AS c WHERE c.phone = v_phone;\n",
     "        SELECT pg_catalog.array_agg(DISTINCT c.patient_id) INTO v_patients\n"
     "          FROM {schema}.m05_reminder_windows AS c WHERE c.phone = v_phone;\n"),
    ("M15", "directory does not lock the prospective phone", UNITS,
     "            UNION SELECT v_phone WHERE v_phone IS NOT NULL) AS x;\n",
     "            ) AS x;\n"),
    ("M16", "candidate ignores the appointment's current patient", UNITS,
     "               AND a.patient_id = w.patient_id AND a.status = 'booked'\n",
     "               AND a.status = 'booked'\n"),
    ("M17", "directory locks a grown set instead of refusing", UNITS,
     "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DIRECTORY_CONTENDED';\n",
     "        PERFORM 1 FROM {schema}.m05_contact_state AS c\n"
     "          WHERE c.phone = ANY (v_after) ORDER BY c.phone FOR UPDATE;\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("directory_dispatch", UNITS, DIRECTORY_TESTS),
    ("inbound", UNITS, INBOUND_TESTS),
    ("timer_interleavings", UNITS, TIMER_TESTS),
    ("service", INTAKE, SERVICE_TESTS),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="sa-intake-pycache-")))
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
                                                   f"title=sa-intake-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
