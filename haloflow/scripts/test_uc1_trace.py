"""Controls for scripts/uc1_trace.py, the UC1 closeout trace (B5 v2 section 7,
v2r1; Codex 6abf4d64 point 4). No database.

Interface pinned here (new):
- `uc1_trace.TRACE`: id -> `{"status", "evidence_level", "nodes"}`.
- `uc1_trace.build_report(trace, collected)`: returns
  `{"header": str, "rows": {id: {...}}}`, or raises `uc1_trace.TraceRefused(reason)`.

The required ids come from tests/m05/uc1_trace_inventory.py, an independent literal.
`collected` is the set of node ids from `pytest --collect-only -q`; CI supplies it
through its own step.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests" / "m05"))

import uc1_trace_inventory as inv  # noqa: E402


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("_uc1_trace", HERE / "uc1_trace.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _all_ids() -> set[str]:
    return set(inv.REQUIREMENTS + inv.AGREED_TESTS + inv.B5_TESTS + inv.HOLDS)


def _collected(trace: dict[str, Any]) -> set[str]:
    return {node for row in trace.values() for node in row["nodes"]}


def test_trace_t01_every_inventory_id_is_traced_and_nothing_else() -> None:
    script = _script()
    assert set(script.TRACE) == _all_ids()


def test_trace_t02_holds_are_pending_with_no_evidence() -> None:
    script = _script()
    for hold in inv.HOLDS:
        row = script.TRACE[hold]
        assert row["status"] == "pending_hold" and row["evidence_level"] == "none", hold
        assert row["nodes"] == [], hold


def test_trace_t03_labels_are_from_the_closed_sets() -> None:
    script = _script()
    for key, row in script.TRACE.items():
        assert row["status"] in inv.STATUSES, key
        assert row["evidence_level"] in inv.EVIDENCE_LEVELS, key
        if row["status"] == "covered":
            assert row["nodes"] and row["evidence_level"] != "none", key


def test_trace_t04_report_header_states_the_milestone_is_open() -> None:
    script = _script()
    report = script.build_report(script.TRACE, _collected(script.TRACE))
    assert report["header"] == inv.REQUIRED_HEADER
    assert set(report["rows"]) == _all_ids()


def test_trace_t05_a_hold_marked_covered_is_refused() -> None:
    script = _script()
    trace = copy.deepcopy(script.TRACE)
    trace["L-1W"] = {"status": "covered", "evidence_level": "unit",
                     "nodes": [next(iter(_collected(script.TRACE)))]}
    with pytest.raises(script.TraceRefused):
        script.build_report(trace, _collected(trace))


def test_trace_t06_an_unmapped_id_is_refused() -> None:
    script = _script()
    trace = copy.deepcopy(script.TRACE)
    del trace["MA-01"]
    with pytest.raises(script.TraceRefused):
        script.build_report(trace, _collected(trace))


def test_trace_t07_a_dangling_node_is_refused() -> None:
    script = _script()
    trace = copy.deepcopy(script.TRACE)
    collected = _collected(trace)
    covered = next(key for key, row in trace.items() if row["status"] == "covered")
    trace[covered]["nodes"] = [*trace[covered]["nodes"], "tests/m05/test_nowhere.py::test_gone"]
    with pytest.raises(script.TraceRefused):
        script.build_report(trace, collected)


def test_trace_t08_every_mapped_node_is_collected_in_this_tree() -> None:
    """Against the real tree: every mapped node exists in a real collection."""

    import subprocess

    script = _script()
    output = subprocess.run(
        # -I: with `python -m`, the cwd (haloflow/) would precede site-packages and the
        # local alembic/ package would shadow the installed one (declared test defect).
        [sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/m01", "tests/m02", "tests/m05"],
        capture_output=True, text=True, check=False, cwd=HERE.parent,
    ).stdout
    collected = {line.strip() for line in output.splitlines() if "::" in line}
    missing = sorted(_collected(script.TRACE) - collected)
    assert not missing, missing[:10]
