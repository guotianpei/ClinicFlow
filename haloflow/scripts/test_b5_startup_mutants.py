"""Static controls for scripts/b5_startup_mutants.py (no database).

Every mutant must be one contiguous change that composes: its `old` text occurs
exactly once in the target, and the mutated target compiles. Every declared kill
names a test node that pytest really collects (parametrized ids included), and
each group's kills are in that group's module. A missing anchor is refused.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_b5_startup_mutants", HERE / "b5_startup_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _group_mutants(script: Any, checker: Any) -> dict[str, tuple[Any, ...]]:
    return {
        label: script.build_mutants(target.read_text(encoding="utf-8"), checker.Mutant,
                                    target, only=tests)
        for label, target, tests in script.GROUPS
    }


def test_groups_hold_the_declared_mutants() -> None:
    script = _script()
    groups = _group_mutants(script, script._load_checker())
    assert {label: [m.id for m in mutants] for label, mutants in groups.items()} == {
        "startup-postgres": ["SU-NOVALIDATE", "SU-PERTENANT", "SU-ADMIT", "SU-FACADE",
                             "SU-SNAPSHOT", "SU-CONSTRUCT", "SU-CHAIN"],
        "startup-units": ["SU-KEYS"],
        "registry-postgres": ["SU-NOACTIVATION", "SU-LINKAGE"],
        "registry-units": ["SU-NOACTIVATION", "SU-LINKAGE"],
    }
    sizes = {mutant_id: len(kills) for mutant_id, _, _, _, kills in script.SPECS}
    assert sizes == {"SU-NOVALIDATE": 10, "SU-NOACTIVATION": 5, "SU-LINKAGE": 15,
                     "SU-PERTENANT": 11, "SU-KEYS": 1, "SU-ADMIT": 1, "SU-FACADE": 2,
                     "SU-SNAPSHOT": 1, "SU-CONSTRUCT": 29, "SU-CHAIN": 17}
    # Each mutant's kills are split over its groups without loss.
    for mutant_id, _, _, _, kills in script.SPECS:
        found = frozenset().union(*(m.kills for mutants in groups.values()
                                    for m in mutants if m.id == mutant_id))
        assert found == kills, mutant_id


def test_every_mutant_composes_as_one_contiguous_change() -> None:
    script = _script()
    for label, mutants in _group_mutants(script, script._load_checker()).items():
        target = dict((g[0], g[1]) for g in script.GROUPS)[label]
        text = target.read_text(encoding="utf-8")
        for mutant in mutants:
            assert text.count(mutant.old) == 1, mutant.id
            assert mutant.old != mutant.new, mutant.id
            mutated = text.replace(mutant.old, mutant.new)
            compile(mutated, f"{target.name}_{mutant.id}.py", "exec")
            assert mutated != text, mutant.id
            assert mutant.kills, mutant.id


def test_every_declared_kill_names_a_collected_node() -> None:
    script = _script()
    done = subprocess.run(
        # The pytest binary beside the interpreter: `python -m pytest` from haloflow/
        # would put the CWD first on sys.path, where alembic/ shadows the package.
        [str(Path(sys.executable).with_name("pytest")), "--collect-only", "-q",
         "-p", "no:cacheprovider", script.PG, script.UNITS],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    collected = {line.strip() for line in done.stdout.splitlines() if "::" in line}
    for mutant_id, _, _, _, kills in script.SPECS:
        missing = sorted(kills - collected)
        assert not missing, (mutant_id, missing)
    for label, mutants in _group_mutants(script, script._load_checker()).items():
        tests = dict((g[0], g[2]) for g in script.GROUPS)[label]
        for mutant in mutants:
            assert all(node.startswith(f"{tests}::") for node in mutant.kills), mutant.id


def test_a_missing_anchor_is_refused() -> None:
    script = _script()
    checker = script._load_checker()
    cases = (
        (script.STARTUP, "self.__allowed_ids:\n", "self.__allowed:\n", "SU-FACADE"),
        (script.STARTUP, "keys=ProductionNoteKeys()", "keys=ProductionNoteKeys(1)", "SU-KEYS"),
        (script.REGISTRY, "    return flags.pop()\n", "    return flags.pop()  # x\n",
         "SU-LINKAGE"),
    )
    for target, present, absent, mutant_id in cases:
        text = target.read_text(encoding="utf-8")
        assert present in text
        try:
            script.build_mutants(text.replace(present, absent), checker.Mutant, target)
        except SystemExit as refused:
            assert mutant_id in str(refused)
        else:  # pragma: no cover - the control must refuse
            raise AssertionError(f"a missing {mutant_id} anchor was not refused")
