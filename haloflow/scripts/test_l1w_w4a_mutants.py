"""Static controls for scripts/l1w_w4a_mutants.py (no database, no pytest runs).

Every mutant replaces exactly one span of its target, changes the text, has a unique
id and a non-empty predeclared kill set naming collected tests of its group's module.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("_l1w_w4a_mutants", HERE / "l1w_w4a_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mutants_apply_exactly_once_and_change_the_target() -> None:
    script = _script()
    root = HERE.parent
    for mid, _desc, target, old, new, _kills in script.SPECS:
        text = (root / target).read_text(encoding="utf-8")
        assert text.count(old) == 1, mid
        assert old != new, mid


def test_ids_unique_and_kill_sets_named_in_group_module() -> None:
    script = _script()
    ids = [spec[0] for spec in script.SPECS]
    assert len(ids) == len(set(ids))
    for spec in script.SPECS:
        assert spec[5], spec[0]
        homes = [label for label, target, tests in script.GROUPS
                 if script.in_group(target, tests, spec)]
        assert len(homes) == 1, (spec[0], homes)  # every mutant runs in exactly one group


def test_every_declared_kill_is_a_collected_node() -> None:
    script = _script()
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/platform"],
        capture_output=True, text=True, check=False, cwd=HERE.parent,
    )
    assert result.returncode == 0
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    declared = {node for spec in script.SPECS for node in spec[5]}
    assert declared <= collected, sorted(declared - collected)[:5]
