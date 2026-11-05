"""Controls for scripts/l1w_trace.py (L-1W package v1r1 Q-1). No database.

The required ids come from tests/platform/l1w_trace_inventory.py, an independent
literal. The real-tree control collects tests/platform with pytest.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests" / "platform"))

import l1w_trace_inventory as inv  # noqa: E402

IDS = (*inv.W1_IDS, *inv.W2_IDS, *inv.W3A_IDS, *inv.W3B_IDS, *inv.W4A_IDS, *inv.W4B_IDS)


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("_l1w_trace", HERE / "l1w_trace.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _nodes(trace: dict[str, list[str]]) -> set[str]:
    return {node for nodes in trace.values() for node in nodes}


def test_l1w_trace_t01_ids_match_the_inventory() -> None:
    script = _script()
    script.check(script.TRACE, IDS, _nodes(script.TRACE))


def test_l1w_trace_t02_missing_id_refused() -> None:
    script = _script()
    trace = {k: v for k, v in script.TRACE.items() if k != "V-01"}
    with pytest.raises(script.TraceRefused):
        script.check(trace, IDS, _nodes(script.TRACE))


def test_l1w_trace_t03_empty_row_refused() -> None:
    script = _script()
    trace = {**script.TRACE, "K-00": []}
    with pytest.raises(script.TraceRefused):
        script.check(trace, IDS, _nodes(script.TRACE))


def test_l1w_trace_t04_dangling_node_refused() -> None:
    script = _script()
    trace = {**script.TRACE, "K-00": [*script.TRACE["K-00"], "tests/platform/x.py::gone"]}
    with pytest.raises(script.TraceRefused):
        script.check(trace, IDS, _nodes(script.TRACE))


def test_l1w_trace_t05_every_mapped_node_is_collected_in_this_tree() -> None:
    script = _script()
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/platform", *sorted(str(p.relative_to(HERE.parent))
                                   for d in ("tests/m01", "tests/m05")
                                   for p in (HERE.parent / d).glob("test_l1w_*.py")),
         *inv.HARNESS_EXTERNAL_FILES],
        capture_output=True, text=True, check=False, cwd=HERE.parent,
    )
    # Codex e0323cac: the collection itself must succeed ...
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    assert collected, "nothing collected"
    script.check(script.TRACE, IDS, collected)
    # ... and the mapped nodes must be exactly the collected L-1W nodes, so an unmapped
    # new test fails here instead of passing silently. Nodes of the two historical
    # files (HARNESS_EXTERNAL_FILES) may be mapped only by HARNESS-MIGRATION.
    def external(node: str) -> bool:
        return node.split("::")[0] in inv.HARNESS_EXTERNAL_FILES

    l1w_collected = {n for n in collected if not external(n)}
    l1w_mapped = {n for n in _nodes(script.TRACE) if not external(n)}
    assert l1w_mapped == l1w_collected, sorted(l1w_collected ^ l1w_mapped)[:10]
    for key, nodes in script.TRACE.items():
        if key != "HARNESS-MIGRATION":
            assert not any(external(n) for n in nodes), key
    assert any(external(n) for n in script.TRACE["HARNESS-MIGRATION"])


def test_l1w_trace_t06_deferred_and_retired_rows_stay_explicit() -> None:
    """Codex W2 plan disposition: deferred W3/W4 rows stay listed and are never
    satisfied by W2 SQL-tier evidence; retired rows point at mapped W2 rows."""

    script = _script()
    assert set(inv.DEFERRED).isdisjoint(IDS)
    assert set(inv.DEFERRED).isdisjoint(script.TRACE)
    assert set(inv.RETIRED).isdisjoint(script.TRACE)
    # W4b mapped every former W4 row; only the "later" pruning row stays deferred.
    assert set(inv.DEFERRED.values()) == {"later"}
    # HARNESS-MIGRATION left DEFERRED when the H-c harness landed (W3b).
    assert "HARNESS-MIGRATION" in inv.W3B_IDS and "HARNESS-MIGRATION" not in inv.DEFERRED
    for carriers in inv.RETIRED.values():
        assert set(carriers) <= set(inv.W2_IDS) | set(inv.W3A_IDS)
