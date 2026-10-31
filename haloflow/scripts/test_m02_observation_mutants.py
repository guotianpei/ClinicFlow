"""Controls for scripts/m02_observation_mutants.py (Codex F-MUT1, 20261031T155616Z).

Synthetic run evidence only: no database, no real pytest subprocess. The judge must
refuse a nonzero exit that is not a completed assertion-level kill (exit 2, an
error-only exit 1, an unrelated failure, missing or disagreeing evidence) and accept
a genuine failure of a predeclared node. `main` is driven with a fake runner to
reproduce the false-pass case, and to show the target is restored on every path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parent / "m02_observation_mutants.py"
_SPEC = importlib.util.spec_from_file_location("_m02_observation_mutants", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
mm: Any = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mm
_SPEC.loader.exec_module(mm)

NODES = frozenset({"t.py::a", "t.py::b", "t.py::c"})
KILLS = frozenset({"t.py::a"})


def run(exit: int, outcomes: dict[str, str] | None, *, session: Any = "auto", **kw: Any) -> Any:
    if session == "auto" and outcomes is not None:
        failed = sum(1 for o in outcomes.values() if o in ("failure", "error"))
        session = {"exitstatus": exit, "testscollected": len(outcomes), "testsfailed": failed}
    return mm.RunEvidence(exit, None if session == "auto" else session, outcomes, **kw)


def passed(**override: str) -> dict[str, str]:
    outcomes = {node: "passed" for node in NODES}
    outcomes.update({f"t.py::{k}": v for k, v in override.items()})
    return outcomes


# --- judge_mutant ---------------------------------------------------------------


def test_genuine_failure_of_a_declared_node_is_a_kill() -> None:
    problems, failed = mm.judge_mutant(run(1, passed(a="failure")), NODES, KILLS)
    assert problems == [] and failed == ["t.py::a"]


@pytest.mark.parametrize("exit", [2, 3, 4, 5, -9])
def test_any_exit_other_than_one_is_refused(exit: int) -> None:
    problems, _ = mm.judge_mutant(run(exit, passed(a="failure")), NODES, KILLS)
    assert problems


def test_exit_two_collection_error_without_cases_is_refused() -> None:
    """Codex's reproduction: control (0, 23 passed), mutant (2, [], collection error)."""

    problems, failed = mm.judge_mutant(
        mm.RunEvidence(2, None, None, junit_problem="JUnit missing"), NODES, KILLS
    )
    assert problems and failed == []


def test_error_only_exit_one_is_refused() -> None:
    problems, _ = mm.judge_mutant(run(1, passed(a="error")), NODES, KILLS)
    assert any("error outcome" in p for p in problems)
    assert any("no assertion-level failure" in p for p in problems)


def test_a_declared_failure_plus_a_setup_error_is_refused() -> None:
    problems, _ = mm.judge_mutant(run(1, passed(a="failure", b="error")), NODES, KILLS)
    assert any("error outcome" in p for p in problems)


def test_an_unrelated_failure_is_refused() -> None:
    problems, _ = mm.judge_mutant(run(1, passed(b="failure")), NODES, KILLS)
    assert any("unrelated" in p for p in problems)


def test_a_declared_failure_plus_an_unrelated_failure_is_refused() -> None:
    problems, _ = mm.judge_mutant(run(1, passed(a="failure", c="failure")), NODES, KILLS)
    assert any("unrelated" in p for p in problems)


def test_a_skipped_case_is_refused() -> None:
    problems, _ = mm.judge_mutant(run(1, passed(a="failure", b="skipped")), NODES, KILLS)
    assert any("skipped" in p for p in problems)


def test_an_interrupted_session_is_refused_even_with_exit_one() -> None:
    evidence = run(
        1, passed(a="failure"), session={"exitstatus": 2, "testscollected": 3, "testsfailed": 1}
    )
    problems, _ = mm.judge_mutant(evidence, NODES, KILLS)
    assert any("session exitstatus" in p for p in problems)


def test_a_missing_session_record_is_refused() -> None:
    problems, _ = mm.judge_mutant(run(1, passed(a="failure"), session=None), NODES, KILLS)
    assert any("no session record" in p for p in problems)


def test_unrun_tests_are_refused() -> None:
    outcomes = {"t.py::a": "failure", "t.py::b": "passed"}  # t.py::c never ran
    evidence = run(1, outcomes, session={"exitstatus": 1, "testscollected": 3, "testsfailed": 1})
    problems, _ = mm.judge_mutant(evidence, NODES, KILLS)
    assert any("node set differs" in p for p in problems)


def test_collected_count_disagreement_is_refused() -> None:
    evidence = run(
        1, passed(a="failure"), session={"exitstatus": 1, "testscollected": 2, "testsfailed": 1}
    )
    problems, _ = mm.judge_mutant(evidence, NODES, KILLS)
    assert any("collected count" in p for p in problems)


def test_failed_count_disagreement_is_refused() -> None:
    evidence = run(
        1, passed(a="failure"), session={"exitstatus": 1, "testscollected": 3, "testsfailed": 2}
    )
    problems, _ = mm.judge_mutant(evidence, NODES, KILLS)
    assert any("testsfailed" in p for p in problems)


def test_duplicate_cases_or_collection_errors_are_refused() -> None:
    problems, _ = mm.judge_mutant(
        run(1, passed(a="failure"), collection_errors=["t.py::b"]), NODES, KILLS
    )
    assert any("collection errors" in p for p in problems)


# --- judge_control --------------------------------------------------------------


def test_control_all_passed_is_accepted() -> None:
    assert mm.judge_control(run(0, passed())) == []


@pytest.mark.parametrize("bad", [{"a": "failure"}, {"a": "error"}, {"a": "skipped"}])
def test_control_with_any_non_pass_is_refused(bad: dict[str, str]) -> None:
    exit = 0 if "skipped" in bad.values() else 1
    assert mm.judge_control(run(exit, passed(**bad)))


def test_control_without_junit_is_refused() -> None:
    assert mm.judge_control(mm.RunEvidence(0, None, None, junit_problem="JUnit missing"))


# --- main, with a fake runner ------------------------------------------------------


def _target(tmp_path: Path) -> tuple[Path, str]:
    """A disposable target holding every mutant's old text exactly once."""

    body = "\n".join(f"# {m.id}\n{m.old}" for m in mm.MUTANTS) + "\n"
    path = tmp_path / "observation.py"
    path.write_text(body)
    return path, hashlib.sha256(body.encode()).hexdigest()


def _main(tmp_path: Path, runner: Any) -> int:
    target, _ = _target(tmp_path)
    return int(
        mm.main(
            ["--pytest", "pytest", "--evidence", str(tmp_path / "ev"), "--target", str(target)],
            runner=runner,
        )
    )


def _control_nodes() -> dict[str, str]:
    nodes: set[str] = set()
    for mutant in mm.MUTANTS:
        nodes |= mutant.kills
    return {node: "passed" for node in nodes}


def _good_runner(label: str) -> Any:
    outcomes = _control_nodes()
    if label == "control":
        return run(0, outcomes)
    mutant = next(m for m in mm.MUTANTS if m.id == label)
    outcomes[sorted(mutant.kills)[0]] = "failure"
    return run(1, outcomes)


def test_main_accepts_genuine_kills_and_restores(tmp_path: Path) -> None:
    target, sha = _target(tmp_path)
    rc = mm.main(
        ["--pytest", "pytest", "--evidence", str(tmp_path / "ev"), "--target", str(target)],
        runner=lambda _p, label, _e: _good_runner(label),
    )
    assert rc == 0
    assert hashlib.sha256(target.read_bytes()).hexdigest() == sha


def test_main_reproduction_collection_error_is_not_a_kill(tmp_path: Path) -> None:
    """Codex F-MUT1: the previous checker returned 0 here."""

    def runner(_p: str, label: str, _e: Path) -> Any:
        if label == "control":
            return run(0, _control_nodes())
        return mm.RunEvidence(2, None, None, junit_problem="collection error")

    assert _main(tmp_path, runner) == 1


def test_main_error_only_exit_one_is_not_a_kill(tmp_path: Path) -> None:
    def runner(_p: str, label: str, _e: Path) -> Any:
        outcomes = _control_nodes()
        if label != "control":
            outcomes = {node: "error" for node in outcomes}
            return run(1, outcomes)
        return run(0, outcomes)

    assert _main(tmp_path, runner) == 1


def test_main_refuses_when_the_control_fails(tmp_path: Path) -> None:
    def runner(_p: str, label: str, _e: Path) -> Any:
        outcomes = _control_nodes()
        outcomes[sorted(outcomes)[0]] = "failure"
        return run(1, outcomes)

    assert _main(tmp_path, runner) == 1


def test_main_restores_the_target_when_the_runner_raises(tmp_path: Path) -> None:
    target, sha = _target(tmp_path)

    def runner(_p: str, label: str, _e: Path) -> Any:
        if label == "control":
            return run(0, _control_nodes())
        assert target.read_bytes() != b""  # the mutant is applied at this point
        raise RuntimeError("runner crashed mid-mutant")

    with pytest.raises(RuntimeError):
        mm.main(
            ["--pytest", "pytest", "--evidence", str(tmp_path / "ev"), "--target", str(target)],
            runner=runner,
        )
    assert hashlib.sha256(target.read_bytes()).hexdigest() == sha


def test_every_declared_kill_names_a_node_of_the_service_module() -> None:
    for mutant in mm.MUTANTS:
        assert mutant.kills, mutant.id
        assert all(node.startswith(mm.TESTS + "::") for node in mutant.kills), mutant.id
