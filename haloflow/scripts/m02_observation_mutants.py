"""Mutation controls for haloflow.m02.observation (UC1 part A).

Codex 20261031T152545Z (mutation evidence) and 20261031T155616Z (F-MUT1: a nonzero
exit is not a kill). The service test module runs once against the unmodified
source (the control) and once per mutant, with exactly one textual change applied
to `observation.py`. A run counts ONLY from structured evidence: pytest's JUnit
report (xunit1, parsed by scripts/ci_test_report.parse_junit) and the session record
written by ci/ci_pytest_session.py, never from the exit status alone.

Control accepted only if: exit 0; session exitstatus 0; collected equals the number
of JUnit cases; every case passed; no collection error, duplicate or skip.

Mutant KILLED only if: exit 1 and session exitstatus 1 (a completed run with test
failures; 2 interrupted, 3 internal, 4 usage and 5 no-tests are refused); collected
equals the control's count and the node set equals the control's; no case has
outcome `error` (setup/teardown/collection) or `skipped`; at least one case FAILED,
and every failed node is in that mutant's PREDECLARED kill set (an unrelated failure
refuses the run); session testsfailed equals the number of failed cases.

Raw output, JUnit and session record of every run are kept in the evidence
directory. The source file is restored after every run and on every exit path, and
its SHA-256 is re-checked. Exit 0 only if the control is accepted, every mutant is
killed and the source is restored byte-identically.

Usage (from haloflow/):
    python3 -I scripts/m02_observation_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/mutation"
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TARGET = Path("src/haloflow/m02/observation.py")
TESTS = "tests/m02/test_m02_part_a_service_postgres.py"
PLUGIN_DIR = HERE.parent / "ci"


def _load_report_module() -> Any:
    spec = importlib.util.spec_from_file_location("_ci_test_report", HERE / "ci_test_report.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


_REPORT = _load_report_module()

_T = TESTS + "::"
_PA06AB = (
    _T + "test_pa06ab_one_shot_commit_phase_fault_then_real_retry[commit_then_raise-existing-1]",
    _T + "test_pa06ab_one_shot_commit_phase_fault_then_real_retry[raise_no_commit-appended-0]",
)
_FOBS1_POST = (
    _T + "test_fobs1_post_marker_cancellation_is_unknown_then_retry_converges"
    "[commit_then_cancel-existing-1]",
    _T + "test_fobs1_post_marker_cancellation_is_unknown_then_retry_converges"
    "[cancel_no_commit-appended-0]",
)
_FOBS1_PRE = _T + "test_fobs1_pre_marker_cancellation_propagates_and_rolls_back"
_S01 = _T + "test_s01_event_and_domain_state_commit_together"
_ATOM = _T + "test_atom_domain_write_then_exception_neither_commits"
_EX = _T + "test_ex_existing_branch_does_not_invoke_callback"
_PA06E = _T + "test_pa06e_callback_database_exception_object_propagates"
_PA03 = _T + "test_pa03_nested_gateway_in_callback_unchanged_type"
_MK = _T + "test_mk_post_body_path_check_failure_is_unknown_not_definite"
_MKNEG = _T + "test_mk_neg_callback_exception_before_marker_is_not_unknown"
_PA06C = _T + "test_pa06c_append_failure_policy"
_PA18 = _T + "test_pa18_no_metadata_or_driver_text_in_logs"
_PA50 = _T + "test_pa50_binding_after_transaction_is_handle_expired_no_dispatch"
_BNDF = _T + "test_bnd_binding_from_another_foundation_refused"
_PA05B = _T + "test_pa05b_callback_omission_detected_by_domain_oracle[reviewed]"
_MAP = _T + "test_map_database_error_to_fixed_reason_code"
_MAP_RESULT = _T + "test_map_malformed_function_result_is_result_invalid"
_MAP_CONTROL = _T + "test_map_control_a_well_formed_seam_result_is_accepted"


@dataclass(frozen=True)
class Mutant:
    id: str
    description: str
    old: str
    new: str
    kills: frozenset[str]  # PREDECLARED relevant nodes; every failure must be one of them


def _ids(*nodes: str) -> frozenset[str]:
    return frozenset(nodes)


_BINDING = tuple(f"{_MAP}[binding-{k}]" for k in ("absent", "inactive", "mismatch"))
_RESULTS = tuple(
    f"{_MAP_RESULT}[{k}]"
    for k in (
        "no-row",
        "empty-row",
        "string",
        "empty-object",
        "extra-key",
        "version-2",
        "bad-uuid",
        "bad-outcome",
        "null-handoff",
        "two-columns",
    )
)

MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "M1",
        "callback also on existing",
        'if result.appended_or_existing == "appended":\n                await self._apply',
        "if True:\n                await self._apply",
        _ids(_EX, *_PA06AB, *_FOBS1_POST),
    ),
    Mutant(
        "M2",
        "no unknown classification after the marker",
        "            if not body_complete:\n                raise",
        "            raise",
        _ids(_MK, *_PA06AB, *_FOBS1_POST),
    ),
    Mutant(
        "M3",
        "marker set before the body runs",
        "            nonlocal body_complete\n",
        "            nonlocal body_complete\n            body_complete = True\n",
        _ids(
            _ATOM,
            _FOBS1_PRE,
            _MKNEG,
            _PA03,
            _PA06C,
            _PA06E,
            _PA18,
            *(
                f"{_MAP}[{k}]"
                for k in (
                    "binding-absent",
                    "binding-inactive",
                    "binding-mismatch",
                    "context-22023",
                    "unlisted-p0001",
                    "other-40001",
                    "other-xx000",
                )
            ),
            *_RESULTS,
        ),
    ),
    Mutant(
        "M4",
        "AppendFailed chained to the driver error",
        "            failure = _reason_for(error)\n            row = None",
        "            raise AppendFailed(_reason_for(error)) from error",
        _ids(
            _PA06C,
            *(
                f"{_MAP}[{k}]"
                for k in (
                    "binding-absent",
                    "binding-inactive",
                    "binding-mismatch",
                    "context-22023",
                    "unlisted-p0001",
                    "other-40001",
                    "other-xx000",
                )
            ),
        ),
    ),
    Mutant(
        "M5",
        "no binding liveness check",
        "        if not binding._liveness.active:\n            raise RepositoryHandleExpired()\n",
        "",
        _ids(_PA50),
    ),
    Mutant("M6", "no binding owner check", " or binding._owner is not self", "", _ids(_BNDF)),
    Mutant(
        "M7",
        "_apply does not call the callback",
        "        await apply_state_change(handle, result)",
        "        return None",
        _ids(
            _S01,
            _ATOM,
            _EX,
            _PA06E,
            _PA03,
            *_PA06AB,
            _MK,
            _MKNEG,
            _PA05B,
            *_FOBS1_POST,
            _FOBS1_PRE,
            _MAP_CONTROL,
        ),
    ),
    Mutant(
        "M8",
        "attribution principal not from the context",
        '"principal_id": principal.id,',
        '"principal_id": "00000000-0000-4000-8000-000000000001",',
        _ids(_S01),
    ),
    Mutant(
        "M9",
        "callback exception wrapped",
        "                await self._apply(apply_state_change, handle, result)",
        "                try:\n"
        "                    await self._apply(apply_state_change, handle, result)\n"
        "                except Exception:\n"
        '                    raise RuntimeError("wrapped") from None',
        _ids(_ATOM, _MKNEG, _PA03, _PA06E, _FOBS1_PRE),
    ),
    Mutant(
        "M10",
        "refusal code collapsed",
        "        if message in _REFUSAL_CODES:\n            return message",
        "        if message in _REFUSAL_CODES:\n            return _APPEND_UNAVAILABLE",
        _ids(_PA06C),
    ),
    Mutant(
        "M11",
        "post-marker cancellation not classified (F-OBS1)",
        "        except (Exception, asyncio.CancelledError):",
        "        except Exception:",
        _ids(*_FOBS1_POST),
    ),
    Mutant(
        "M12",
        "binding messages not mapped",
        "        if message in _BINDING_CODES:\n            return _BINDING_CODES[message]",
        "        if message in _BINDING_CODES:\n            return _APPEND_UNAVAILABLE",
        _ids(*_BINDING),
    ),
    Mutant(
        "M13",
        "22023 not mapped",
        '    if sqlstate == "22023":\n        return _TENANT_CONTEXT_INVALID',
        '    if sqlstate == "22023":\n        return _APPEND_UNAVAILABLE',
        _ids(f"{_MAP}[context-22023]"),
    ),
    Mutant(
        "M14",
        "result shape not validated",
        "    if row is None or len(row) != 1 or not isinstance(row[0], dict):\n        return None",
        "    if row is None or len(row) == 0 or not isinstance(row[0], dict):\n        return None",
        _ids(f"{_MAP_RESULT}[two-columns]"),
    ),
)


# ---------------------------------------------------------------------------
# One run and its judgement (pure; controlled by scripts/test_m02_observation_mutants.py)
# ---------------------------------------------------------------------------


@dataclass
class RunEvidence:
    exit: int
    session: Mapping[str, int] | None
    outcomes: dict[str, str] | None  # node id -> passed/failure/error/skipped
    multiple: list[str] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)
    junit_problem: str | None = None


def judge_control(run: RunEvidence) -> list[str]:
    problems: list[str] = []
    if run.exit != 0:
        problems.append(f"exit {run.exit}, expected 0")
    if run.outcomes is None:
        return [*problems, f"no usable JUnit: {run.junit_problem}"]
    if run.session is None:
        problems.append("no session record")
    else:
        if run.session.get("exitstatus") != 0:
            problems.append(f"session exitstatus {run.session.get('exitstatus')}")
        if run.session.get("testscollected") != len(run.outcomes):
            problems.append("collected count differs from JUnit cases")
    if run.multiple or run.collection_errors:
        problems.append("duplicate cases or collection errors")
    not_passed = sorted(n for n, o in run.outcomes.items() if o != "passed")
    if not_passed:
        problems.append(f"{len(not_passed)} case(s) did not pass")
    if not run.outcomes:
        problems.append("no cases")
    return problems


def judge_mutant(
    run: RunEvidence, control_nodes: frozenset[str], kills: frozenset[str]
) -> tuple[list[str], list[str]]:
    """(problems, failed nodes). Killed only when problems is empty."""

    problems: list[str] = []
    if run.exit != 1:
        problems.append(f"exit {run.exit}, expected 1 (completed run with test failures)")
    if run.outcomes is None:
        return [*problems, f"no usable JUnit: {run.junit_problem}"], []
    failed = sorted(n for n, o in run.outcomes.items() if o == "failure")
    if run.session is None:
        problems.append("no session record")
    else:
        if run.session.get("exitstatus") != 1:
            problems.append(f"session exitstatus {run.session.get('exitstatus')}")
        if run.session.get("testscollected") != len(control_nodes):
            problems.append("collected count differs from the control")
        if run.session.get("testsfailed") != len(failed):
            problems.append("session testsfailed differs from JUnit failures")
    if frozenset(run.outcomes) != control_nodes:
        problems.append("node set differs from the control")
    if run.multiple or run.collection_errors:
        problems.append("duplicate cases or collection errors")
    errored = sorted(n for n, o in run.outcomes.items() if o == "error")
    if errored:
        problems.append(f"{len(errored)} error outcome(s) (setup/teardown/collection)")
    skipped = sorted(n for n, o in run.outcomes.items() if o == "skipped")
    if skipped:
        problems.append(f"{len(skipped)} skipped case(s)")
    if not failed:
        problems.append("no assertion-level failure")
    unrelated = sorted(set(failed) - kills)
    if unrelated:
        problems.append(f"unrelated failure(s): {', '.join(unrelated)}")
    return problems, failed


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_pytest(pytest: str, label: str, evidence: Path) -> RunEvidence:
    junit = evidence / f"{label}.junit.xml"
    session = evidence / f"{label}.session.json"
    for path in (junit, session):
        path.unlink(missing_ok=True)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(PLUGIN_DIR) + (os.pathsep + existing if existing else "")
    env["CI_PYTEST_SESSION_FILE"] = str(session)
    env.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            pytest,
            TESTS,
            "-q",
            "-rfE",
            "-p",
            "no:cacheprovider",
            "-p",
            "ci_pytest_session",
            "-o",
            "junit_family=xunit1",
            f"--junitxml={junit}",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env=env,
    )
    (evidence / f"{label}.log").write_text(
        f"exit={completed.returncode}\n"
        f"--- stdout\n{completed.stdout}\n"
        f"--- stderr\n{completed.stderr}"
    )
    record: Mapping[str, int] | None = None
    if session.exists():
        try:
            loaded = json.loads(session.read_text())
            if isinstance(loaded, dict):
                record = loaded
        except ValueError:
            record = None
    if not junit.exists():
        return RunEvidence(completed.returncode, record, None, junit_problem="JUnit missing")
    try:
        parsed = _REPORT.parse_junit(junit.read_bytes())
    except _REPORT.ReportUnusable as error:
        return RunEvidence(completed.returncode, record, None, junit_problem=str(error))
    return RunEvidence(
        completed.returncode,
        record,
        dict(parsed.outcomes),
        list(parsed.multiple),
        list(parsed.collection_errors),
    )


Runner = Callable[[str, str, Path], RunEvidence]


def _short(node: str) -> str:
    prefix = TESTS + "::test_"
    return node[len(prefix) :] if node.startswith(prefix) else node


def _chunks(lines: list[str], limit: int) -> list[list[str]]:
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        cost = len(line.encode("utf-8")) + 4
        if chunks[-1] and size + cost > limit:
            chunks.append([])
            size = 0
        chunks[-1].append(line[:limit])
        size += cost
    return [chunk for chunk in chunks if chunk]


def main(argv: list[str] | None = None, runner: Runner = run_pytest) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--target", default=str(TARGET))
    args = parser.parse_args(argv)
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    target = Path(args.target)

    original = target.read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()
    text = original.decode("utf-8")
    results: dict[str, Any] = {
        "target": str(target),
        "target_sha256": original_sha,
        "tests": TESTS,
        "mutants": [],
    }
    ok = True
    try:
        control = runner(args.pytest, "control", evidence)
        control_problems = judge_control(control)
        control_nodes = frozenset(control.outcomes or {})
        results["control"] = {
            "exit": control.exit,
            "cases": len(control_nodes),
            "problems": control_problems,
        }
        if control_problems:
            ok = False
        else:
            for mutant in MUTANTS:
                entry: dict[str, Any] = {
                    "id": mutant.id,
                    "description": mutant.description,
                    "declared_kills": sorted(mutant.kills),
                }
                count = text.count(mutant.old)
                if count != 1:
                    entry.update(
                        applied=False,
                        occurrences=count,
                        killed=False,
                        problems=[f"old text occurs {count} times"],
                    )
                    ok = False
                else:
                    target.write_text(text.replace(mutant.old, mutant.new), encoding="utf-8")
                    try:
                        run = runner(args.pytest, mutant.id, evidence)
                    finally:
                        target.write_bytes(original)
                    problems, failed = judge_mutant(run, control_nodes, mutant.kills)
                    entry.update(
                        applied=True,
                        exit=run.exit,
                        failed=failed,
                        problems=problems,
                        killed=not problems,
                    )
                    if problems:
                        ok = False
                results["mutants"].append(entry)
    finally:
        target.write_bytes(original)
    restored = hashlib.sha256(target.read_bytes()).hexdigest() == original_sha
    results["restored"] = restored
    ok = ok and restored
    results["ok"] = ok
    (evidence / "results.json").write_text(json.dumps(results, indent=2) + "\n")

    killed = sum(1 for m in results["mutants"] if m.get("killed"))
    parts = [
        f"ok={ok} control_cases={results['control']['cases']}"
        f" control_problems={len(results['control']['problems'])}",
        f"killed={killed}/{len(MUTANTS)} restored={restored} target_sha256={original_sha}",
    ]
    for m in results["mutants"]:
        detail = f"failed={len(m.get('failed', []))}"
        if m.get("problems"):
            detail += " problems=" + "; ".join(m["problems"])[:300]
        parts.append(f"{m['id']} killed={m.get('killed')} {detail}")
    print("::notice title=m02-observation-mutants::" + " %0A".join(parts))
    # The failed node of every mutant, readable from the check-run annotations
    # (the artifact may not be reachable by a reviewer). Chunked under the measured
    # 4096-character annotation limit; the prefix "<TESTS>::test_" is elided.
    lines = [
        f"{m['id']}: " + " ".join(_short(node) for node in m.get("failed", []))
        for m in results["mutants"]
    ]
    for index, chunk in enumerate(_chunks(lines, 3800), start=1):
        print(f"::notice title=m02-observation-mutants killed-by {index}::" + " %0A".join(chunk))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
