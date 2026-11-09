"""L-1S package S-H H2 mutation controls, group family ``sh_h2`` (M1..M14, no M7).

Predeclared with the H2 implementation (package v1 section 3 and v1r1 delta, Codex
source gate ffbab59): the 26 h floor; the window end; confirmation at claim (X3); the
snapshot version check; deferral turned into abandonment; the re-key GREATEST(..., now)
removed; the pre-call gate removed; the ACL closure missing one unit; consume without the
spent-ticket check; the worker retrying consume after a commit-unknown; examine without
the post-lock next_check_at re-check (D2); consume without the D11 re-check; consume no
longer activation-protected.

Disclosed: M7 (examine without SKIP LOCKED) is NOT included. Under the pin-1 lock order
every writer of a schedule row (directory v2, settings_put, another examine) holds a lock
that examine takes earlier (contact or settings), so examine never meets a locked
schedule row; the mutant is equivalent and SKIP LOCKED stays as a defensive guard.

Kill sets are declared in scripts/sh_h2_mutant_kills.json before the mutation CI run.
A kill set lists every node the mutant may fail; nodes marked "may" in the review note
(P15 under M6, P24/P25 under M10) depend on ordering or timing and are allowed, not
required. Judged by the shared part-A checker exactly as sh_h1_mutants.

Usage (from haloflow/):
    python3 -I scripts/sh_h2_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/sh-h2-mutation"
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
UNITS = Path("src/haloflow/m05/reminder_units.py")
STATEMENTS = Path("src/haloflow/m05/reminder_statements.py")
WORKER = Path("src/haloflow/m05/outbound.py")
UNIT_TESTS = "tests/m05/test_sh_h2_units.py"
PG_TESTS = "tests/m05/test_sh_h2_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "sh_h2_mutant_kills.json"

# (id, description, target, old, new); kill sets in sh_h2_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("M1", "the 26 h floor becomes 20 h", UNITS,
     "interval '26 hours'", "interval '20 hours'"),
    ("M2", "the window end is inclusive", UNITS,
     "    ELSIF v_local::time >= v_ws AND v_local::time < v_we THEN\n",
     "    ELSIF v_local::time >= v_ws AND v_local::time <= v_we THEN\n"),
    ("M3", "claim v2 ignores a confirmation recorded after prepare (X3)", UNITS,
     "    IF v_p.confirmation = 'confirmed' THEN\n        UPDATE",
     "    IF false THEN\n        UPDATE"),
    ("M4", "examine skips the snapshot version check", UNITS,
     "    IF v_hash IS NULL OR v_sv IS DISTINCT FROM v_p.source_version\n"
     "       OR v_tv IS DISTINCT FROM v_s.settings_version THEN\n",
     "    IF false THEN\n"),
    ("M5", "claim v2 abandons instead of deferring", UNITS,
     "    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') = 'too_late' THEN\n"
     "        UPDATE {schema}.m05_outbound_ops AS o SET state = 'abandoned', code = 'TOO_LATE_26H',\n",  # noqa: E501
     "    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') <> 'send' THEN\n"
     "        UPDATE {schema}.m05_outbound_ops AS o SET state = 'abandoned', code = 'TOO_LATE_26H',\n"),  # noqa: E501
    ("M6", "re-keying drops GREATEST(..., clock_timestamp())", UNITS,
     '           " GREATEST(pg_catalog.timezone(t.zone, ((pg_catalog.timezone(t.zone, t.start_at))"\n'  # noqa: E501
     '           "::date OPERATOR(pg_catalog.-) s.lead_days::integer)"\n'
     '           " OPERATOR(pg_catalog.+) s.send_time), pg_catalog.clock_timestamp()) END")\n',
     '           " pg_catalog.timezone(t.zone, ((pg_catalog.timezone(t.zone, t.start_at))"\n'
     '           "::date OPERATOR(pg_catalog.-) s.lead_days::integer)"\n'
     '           " OPERATOR(pg_catalog.+) s.send_time) END")\n'),
    ("M8", "consume drops the pre-call gate", UNITS,
     "    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') <> 'send' THEN\n"
     "        UPDATE {schema}.m05_outbound_ops AS o\n"
     "           SET state = 'refused', code = 'PRECALL_WINDOW_CLOSED',",
     "    IF false THEN\n"
     "        UPDATE {schema}.m05_outbound_ops AS o\n"
     "           SET state = 'refused', code = 'PRECALL_WINDOW_CLOSED',"),
    ("M9", "the runtime closure omits t048 prepare", UNITS,
     'CLOSED_UNITS: Final = ("t038_m05_directory_put", "t048_m05_outbound_prepare",\n',
     'CLOSED_UNITS: Final = ("t038_m05_directory_put",\n'),
    ("M10", "consume ignores a spent ticket", UNITS,
     "    IF v_t.consumed_at IS NOT NULL THEN\n",
     "    IF false THEN\n"),
    ("M12", "examine skips the post-lock next_check_at re-check (D2)", UNITS,
     "    IF v_q.next_check_at OPERATOR(pg_catalog.>) pg_catalog.clock_timestamp() THEN\n",
     "    IF false THEN\n"),
    ("M13", "consume skips the D11 re-check", UNITS,
     "    IF v_code IS NOT NULL THEN\n"
     "        UPDATE {schema}.m05_outbound_ops AS o\n"
     "           SET state = 'refused', code = v_code,",
     "    IF false THEN\n"
     "        UPDATE {schema}.m05_outbound_ops AS o\n"
     "           SET state = 'refused', code = v_code,"),
    ("M14", "consume is no longer activation-protected", STATEMENTS,
     "SH2_PROTECTED: Final[frozenset[str]] = frozenset({EXAMINE_KEY, CONSUME_KEY, CLAIM_V2_KEY})\n",
     "SH2_PROTECTED: Final[frozenset[str]] = frozenset({EXAMINE_KEY, CLAIM_V2_KEY})\n"),
    ("M11", "the worker retries consume after a commit-unknown", WORKER,
     '            _LOG.info("outbound consume commit unknown; not sent")\n'
     '            return "consume_unknown"\n',
     '            consumed = await self._service.consume(context, op_id, attempt_id)\n'),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="sh-h2-pycache-")))
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
                                                   f"title=sh-h2-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
