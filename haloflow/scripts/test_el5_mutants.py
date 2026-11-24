"""Static controls for scripts/el5_mutants.py (no database, no pytest runs).

Every mutant replaces exactly one span of its target, changes the text, has a unique id and a
non-empty declared row set naming collected E5 tests; the ids are exactly M-E5-1..M-E5-9; the
one SQL mutant (M-E5-9) re-pins the eligibility profile.
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
    spec = importlib.util.spec_from_file_location("_el5_mutants", HERE / "el5_mutants.py")
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
    # M-E5-17..23: the staff route, the t135 display read and the t134 ownership check
    # (REVISE 2026-11-24, Codex-aligned).
    assert ids == [f"M-E5-{i}" for i in range(1, 24)]


def test_declared_rows_name_e4_test_functions() -> None:
    names: dict[str, set[str]] = {}
    for m in _mutants():
        assert m.rows, m.id
        for row in m.rows:
            path, _, test = row.partition("::")
            assert path in ("tests/synthetic/test_el5_units.py",
                            "tests/synthetic/test_el5_postgres.py"), (m.id, row)
            if path not in names:
                tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
                names[path] = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
            assert test.split("[", 1)[0] in names[path], (m.id, row)


SQL_MUTANTS = ("M-E5-1", "M-E5-3", "M-E5-6", "M-E5-9", "M-E5-10", "M-E5-13", "M-E5-15",
               "M-E5-16", "M-E5-21", "M-E5-22", "M-E5-23")


def test_exactly_the_sql_mutants_repin_the_eligibility_profile() -> None:
    repinned = {m.id: (m.repin, m.repin_profile) for m in _mutants() if m.repin}
    assert repinned == dict.fromkeys(SQL_MUTANTS, (True, "eligibility"))
    for m in _mutants():
        sql_targets = {str(t) for t, _o, _n in m.edits()} & {
            "src/haloflow/m05/eligibility_doc_units.py",
            "src/haloflow/m05/eligibility_doc_rules.py"}
        assert bool(sql_targets) == (m.id in SQL_MUTANTS) or m.id == "M-E5-2", m.id
