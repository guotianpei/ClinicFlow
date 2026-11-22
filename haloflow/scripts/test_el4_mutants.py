"""Static controls for scripts/el4_mutants.py (no database, no pytest runs).

Every mutant replaces exactly one span of its target, changes the text, has a unique id and a
non-empty declared row set naming collected E4 tests; the ids are exactly the predeclared E4
mutants run at this layer (M06 is equivalent at E4 and killed at E3a, see the script).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("_el4_mutants", HERE / "el4_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mutants() -> tuple[Any, ...]:
    script = _script()
    return tuple(script.mutants(script._b3()))


def test_mutants_apply_exactly_once_and_change_the_target() -> None:
    for m in _mutants():
        for target, old, new in m.edits():
            assert (ROOT / target).read_text(encoding="utf-8").count(old) == 1, m.id
            assert old != new, m.id


def test_ids_are_the_predeclared_e4_set() -> None:
    ids = [m.id for m in _mutants()]
    assert len(set(ids)) == len(ids)
    assert ids == ["M-E4-1", "M-E4-2", "M-E4-3", "M-E4-4", "M-E4-5", "M-E4-7", "M-E4-8"]


def test_declared_rows_name_e4_test_functions() -> None:
    names: dict[str, set[str]] = {}
    for m in _mutants():
        assert m.rows, m.id
        for row in m.rows:
            path, _, test = row.partition("::")
            assert path in ("tests/synthetic/test_el4_units.py",
                            "tests/synthetic/test_el4_postgres.py"), (m.id, row)
            if path not in names:
                tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
                names[path] = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
            assert test.split("[", 1)[0] in names[path], (m.id, row)
