"""L-1S package S-I mutation controls, group family ``si`` (E1..E4, W1..W8, S1..S17,
V1..V11).

Predeclared from the S-I mutation-to-assertion map v1 (Work Session 2026-11-10,
`claude_si-mutation-map-v1-2026-11-10.md`). Five groups, one target and one test module
each (the shared checker binds a mutant to exactly one module):

- ``ehr-units``: SimulatedEhr defects judged by the unit rows.
- ``worker-units``: BaselineWorker / WritebackWorker defects judged by the unit rows.
- ``sql``: unit-body defects judged by the SQL-path PostgreSQL rows.
- ``service-py``: WritebackService / StaffServiceV3 defects judged by the service rows.
- ``service-sql``: unit-body defects judged by the service rows.

Kill sets are MEASURED (scripts/si_mutant_kills.json): every node a mutant may fail; a
mutant is killed only if it fails and every failure is in its set (the part-A rule).
V2 and V5 have dedicated oracles (P-Q02: no reconcile fallback on an ordinary
not_claimable; P-I07c: a check item bound to another op's check event). V9 removes only
the recovery create's envelope binding, which the stored event-id check precedes; a
survivor is a coverage gap until semantic equivalence is shown, and is reported, never
silently dropped.

Usage (from haloflow/):
    python3 -I scripts/si_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/si-mutation"
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
EHR = Path("src/haloflow/m05/simulated_ehr.py")
PY = Path("src/haloflow/m05/writeback.py")
SQL = Path("src/haloflow/m05/writeback_units.py")
UNIT_TESTS = "tests/m05/test_si_units.py"
PG_TESTS = "tests/m05/test_si_postgres.py"
SVC_TESTS = "tests/m05/test_si_service_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "si_mutant_kills.json"

_DEADLINE = "        if now > apply_deadline:\n"
_IDENTITY_POS = "        IF v_reason IS NULL THEN\n            IF v_status IN ('positive', 'checked_in', 'checked_out') THEN\n"

# (id, description, target, old, new); kill sets in si_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    # -- SimulatedEhr ----------------------------------------------------------------------
    ("E1", "the deadline is never checked", EHR,
     _DEADLINE + '            return WriteResult("deadline_passed")\n', ""),
    ("E2", "a closed permit is not refused", EHR,
     "        if permit_id in self._closed or permit_id in self._used:\n",
     "        if permit_id in self._used:\n"),
    ("E3", "the port time follows the raw clock", EHR,
     "        if self._port_time is None or observed > self._port_time:\n"
     "            self._port_time = observed\n        return self._port_time\n",
     "        self._port_time = observed\n        return observed\n"),
    ("E4", "the deadline boundary is exclusive", EHR, _DEADLINE,
     "        if now >= apply_deadline:\n"),
    # -- workers ---------------------------------------------------------------------------
    ("W1", "a capture exception propagates (no inner send)", PY,
     "        except Exception as error:  # noqa: BLE001 - logged by class name only (privacy)\n"
     '            _LOG.info("writeback baseline not captured error=%s", type(error).__name__)\n',
     "        except asyncio.CancelledError:\n            raise\n"),
    ("W2", "the inner send is skipped when capture is refused", PY,
     '            if outcome != "captured":\n                _LOG.info(',
     '            if outcome != "captured":\n                return "not_sent"\n'
     "                _LOG.info("),
    ("W3", "an unknown write is retried (a second write)", PY,
     '        write_outcome = await self._write(appointment, permit, _deadline(spent["apply_deadline"]))\n',
     '        write_outcome = await self._write(appointment, permit, _deadline(spent["apply_deadline"]))\n'
     '        if write_outcome == "unknown":\n'
     '            write_outcome = await self._write(appointment, permit,\n'
     '                                              _deadline(spent["apply_deadline"]))\n'),
    ("W4", "the reconcile read does not wait for close", PY,
     "            await asyncio.wait_for(self._ehr.close_permit(permit), self._read_timeout_s)\n",
     "            asyncio.ensure_future(self._ehr.close_permit(permit))\n"),
    ("W5", "a failed close is ignored", PY,
     '            _LOG.info("writeback reconcile close failed error=%s", type(error).__name__)\n'
     '            return "reconcile_failed"\n',
     '            _LOG.info("writeback reconcile close failed error=%s", type(error).__name__)\n'),
    ("W6", "an R1 failure is passed as read_failed=False", PY,
     "        decided = await self._service.decide(context, op_id, fence, r1, r1 is None)\n",
     "        decided = await self._service.decide(context, op_id, fence, r1, False)\n"),
    ("W7", "exception text is logged", PY,
     '            _LOG.info("writeback %s read failed error=%s", stage, type(error).__name__)\n',
     '            _LOG.info("writeback %s read failed error=%s", stage, error)\n'),
    ("W8", "a spend commit-unknown proceeds to the write", PY,
     '            _LOG.info("writeback spend commit unknown; no write")\n'
     '            return "spend_unknown"\n',
     '            spent = {"outcome": "spent", "apply_deadline": "9999-12-31T00:00:00+00:00"}\n'),
    # -- SQL, judged by the SQL-path rows --------------------------------------------------
    ("S1", "a positive / checked-in R1 issues a permit", SQL,
     "            IF v_status IN ('positive', 'checked_in', 'checked_out') THEN\n",
     "            IF false THEN\n"),
    ("S2", "the replaced_by identity check is removed", SQL,
     "       OR (v_read OPERATOR(pg_catalog.->>) 'replaced_by') IS NOT NULL\n", ""),
    ("S3", "status judged before identity", SQL, _IDENTITY_POS,
     "        IF v_reason IS NULL OR v_status IN ('positive', 'checked_in', 'checked_out') THEN\n"
     "            IF v_status IN ('positive', 'checked_in', 'checked_out') THEN\n"),
    ("S4", "a null provider / department is not could_not_verify", SQL,
     "    ELSIF v_b.provider_ref IS NULL OR v_b.department_ref IS NULL\n"
     "          OR v_r_provider IS NULL OR v_r_department IS NULL THEN\n"
     "        v_reason := 'could_not_verify';\n", ""),
    ("S5", "the no-baseline check is removed", SQL,
     "    IF NOT FOUND THEN\n        v_reason := 'no_baseline';\n"
     "    ELSIF v_b.confirmation IS NULL OR v_b.confirmation = 'unknown' THEN\n",
     "    IF false THEN\n        v_reason := 'no_baseline';\n"
     "    ELSIF v_b.confirmation IS NULL OR v_b.confirmation = 'unknown' THEN\n"),
    ("S6", "the first retry delay is 300 s", SQL,
     '.replace("@@D1@@", str(RETRY_DELAYS_S[0]))', '.replace("@@D1@@", "300")'),
    ("S7", "spend ignores the lease", SQL,
     "       AND v_w.lease_until IS NOT NULL AND v_w.lease_until OPERATOR(pg_catalog.>) v_now;\n",
     "       AND true;\n"),
    ("S8", "spend ignores the issued fence and the op fence", SQL,
     "       AND p.spent_at IS NULL AND p.voided_at IS NULL AND p.issued_fence = v_fence\n"
     "       AND v_w.fence = v_fence AND v_w.state = 'permitted'\n",
     "       AND p.spent_at IS NULL AND p.voided_at IS NULL\n"
     "       AND v_w.state = 'permitted'\n"),
    ("S9", "a check claim does not void a live unspent permit", SQL,
     "    UPDATE {schema}.m05_writeback_permits AS p SET voided_at = v_now\n"
     "     WHERE p.wb_op_id = v_wb AND p.voided_at IS NULL AND p.spent_at IS NULL;\n", ""),
    ("S10", "a spent permit is not PERMIT_SPENT on the check claim", SQL,
     "    IF v_w.state = 'spent' THEN\n", "    IF false THEN\n"),
    ("S11", "the reconcile margin is removed", SQL,
     "    IF NOT v_now OPERATOR(pg_catalog.>) (v_pr.apply_deadline\n"
     "           OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => @@MARGIN@@)) THEN\n",
     "    IF NOT v_now OPERATOR(pg_catalog.>) v_pr.apply_deadline THEN\n"),
    ("S12", "the reconcile guard reads now()", SQL,
     "    v_now := pg_catalog.clock_timestamp();  -- database wall time, never now()\n",
     "    v_now := pg_catalog.now();\n"),
    ("S13", "settle: any positive read is observed_positive", SQL,
     "        IF v_reason IS NULL AND v_status = 'positive' THEN\n",
     "        IF v_status = 'positive' THEN\n"),
    ("S14", "capture ignores consumed_at", SQL,
     "    IF v_t.consumed_at IS NOT NULL THEN\n", "    IF false THEN\n"),
    ("S15", "capture ignores the source version", SQL,
     "    IF v_p.source_version IS NULL OR v_p.source_version IS DISTINCT FROM v_i.source_version THEN\n",
     "    IF v_p.source_version IS NULL THEN\n"),
    ("S16", "discovery takes every recorded reply class", SQL,
     "             WHERE r.reply_class = 'confirm' AND r.decision = 'recorded'\n",
     "             WHERE r.decision = 'recorded'\n"),
    ("S17", "directory v3 does not update provider_ref", SQL,
     '    "               provider_ref = EXCLUDED.provider_ref,\\n"\n',
     '    "               provider_ref = p.provider_ref,\\n"\n'),
    # -- service, Python -------------------------------------------------------------------
    ("V1", "claim never falls back to the reconcile claim", PY,
     '        if row is not None and not (row.get("outcome") == "not_claimable"\n'
     '                                    and row.get("code") == "PERMIT_SPENT"):\n',
     "        if row is not None:\n"),
    ("V2", "claim falls back on every not_claimable", PY,
     '        if row is not None and not (row.get("outcome") == "not_claimable"\n'
     '                                    and row.get("code") == "PERMIT_SPENT"):\n',
     '        if row is not None and row.get("outcome") != "not_claimable":\n'),
    ("V3", "the decision item path skips the create", PY,
     "                created = await self._call(binding, context, CREATE_KEY, {\n"
     '                    "source_event_id": str(event), "item_kind": ITEM_KIND})\n',
     '                created = {"item_id": str(event)}\n'),
    ("V6", "the resolution is appended at contract v1", PY,
     "            contract_version=RESOLUTION_CONTRACT_VERSION,\n",
     "            contract_version=1,\n"),
    ("V11", "settle creates through the ordinary (protected) create", PY,
     "            if key == SETTLE_KEY:  # the recovery create (t094), bound to this settlement\n",
     "            if False:\n"),
    # -- service, SQL ----------------------------------------------------------------------
    ("V4", "create v2 does not link item_id onto the op", SQL,
     '    "        UPDATE {schema}.m05_writeback_ops AS w SET item_id = v_item\\n"\n'
     '    "         WHERE w.event_id = v_source AND w.item_id IS NULL;\\n"\n',
     '    "        PERFORM 1;\\n"\n'),
    ("V5", "the reason read skips its op binding", SQL,
     "    IF NOT EXISTS (SELECT 1 FROM {schema}.m05_writeback_ops AS w\n"
     "                    WHERE w.event_id = v_item.source_event_id\n"
     "                      AND w.item_id = v_item.item_id) THEN\n",
     "    IF false THEN\n"),
    ("V7", "apply v3 refuses a contract v1 resolution", SQL,
     "    \"                OR ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '1'::jsonb\\n\"\n",
     "    \"                OR ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '9'::jsonb\\n\"\n"),
    ("V8", "the recovery create accepts any live spent permit", SQL,
     "     WHERE p.permit_id = v_permit AND p.wb_op_id = v_wb AND p.voided_at IS NULL\n",
     "     WHERE p.voided_at IS NULL\n"),
    ("V9", "the recovery create skips the canonical-event check", SQL,
     "       OR (v_env OPERATOR(pg_catalog.->) 'metadata_paths' OPERATOR(pg_catalog.->>) 'wb_op_id')\n"
     "          IS DISTINCT FROM v_wb::text\n", ""),
    ("V10", "the recovery create does not require an unlinked op", SQL,
     "       OR v_w.event_id IS DISTINCT FROM v_event OR v_w.item_id IS NOT NULL THEN\n",
     "       OR v_w.event_id IS DISTINCT FROM v_event THEN\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("ehr-units", EHR, UNIT_TESTS),
    ("worker-units", PY, UNIT_TESTS),
    ("sql", SQL, PG_TESTS),
    ("service-py", PY, SVC_TESTS),
    ("service-sql", SQL, SVC_TESTS),
)
# Which group each mutant belongs to (a target alone is ambiguous for PY and SQL).
GROUP_OF: dict[str, str] = {
    **{f"E{n}": "ehr-units" for n in range(1, 5)},
    **{f"W{n}": "worker-units" for n in range(1, 9)},
    **{f"S{n}": "sql" for n in range(1, 18)},
    **{m: "service-py" for m in ("V1", "V2", "V3", "V6", "V11")},
    **{m: "service-sql" for m in ("V4", "V5", "V7", "V8", "V9", "V10")},
}


def bytecode_env(empty_dir: str) -> dict[str, str]:
    """The W4B-CACHE-1 guard: compile the source on disk for every run."""

    return {"PYTHONPYCACHEPREFIX": empty_dir, "PYTHONDONTWRITEBYTECODE": "1"}


def kill_sets() -> dict[str, frozenset[str]]:
    if not KILLS_FILE.exists():
        return {}
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


def in_group(label: str, spec: tuple[Any, ...]) -> bool:
    return GROUP_OF.get(spec[0]) == label


def build_mutants(checker: Any, label: str) -> tuple[Any, ...]:
    return tuple(
        checker.Mutant(spec[0], spec[1], spec[3], spec[4], spec[5])
        for spec in specs()
        if in_group(label, spec)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--group", action="append", default=None,
                        help="run only the named group(s) (local measurement)")
    parser.add_argument("--mutant", action="append", default=None,
                        help="run only the named mutant(s) (local re-measurement)")
    args = parser.parse_args(argv)
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="si-pycache-")))
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        if args.group is not None and label not in args.group:
            continue
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = tuple(m for m in build_mutants(checker, label)
                                if args.mutant is None or m.id in args.mutant)
        if not checker.MUTANTS:
            continue
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=si-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
