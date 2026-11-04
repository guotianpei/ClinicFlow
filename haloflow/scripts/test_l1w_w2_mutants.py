"""Static controls for scripts/l1w_w2_mutants.py (no database, no pytest runs).

Every mutant replaces exactly one span of alembic 008, changes the text, has a unique
id and a non-empty predeclared kill set of collected tests/platform W2 nodes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("_l1w_w2_mutants", HERE / "l1w_w2_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mutants_apply_exactly_once_and_change_the_target() -> None:
    script = _script()
    text = (HERE.parent / script.TARGET).read_text(encoding="utf-8")
    for mid, _desc, old, new in script.SPECS:
        assert text.count(old) == 1, mid
        assert old != new, mid


def test_ids_unique_and_every_mutant_has_a_w2_kill_set() -> None:
    script = _script()
    ids = [spec[0] for spec in script.SPECS]
    assert len(ids) == len(set(ids))
    assert set(script.KILLS) == set(ids)
    added = script.W4A_ADDED_KILLS  # W4a option B (Codex 366f7e94): additive only
    assert set(added) <= set(ids)
    for mid in ids:
        kills = script.KILLS[mid]
        extra = added.get(mid, frozenset())
        base = kills - extra
        assert base, mid  # every mutant keeps its own W2 kill rows
        assert all(node.startswith("tests/platform/test_l1w_w2_") for node in base), mid
        assert all(node.startswith("tests/platform/test_l1w_w4a_") for node in extra), mid


def test_every_declared_kill_is_a_collected_node() -> None:
    script = _script()
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/platform"],
        capture_output=True, text=True, check=False, cwd=HERE.parent,
    )
    assert result.returncode == 0
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    declared = {node for kills in script.KILLS.values() for node in kills}
    assert declared <= collected, sorted(declared - collected)[:5]
