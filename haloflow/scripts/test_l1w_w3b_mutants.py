"""Static controls for scripts/l1w_w3b_mutants.py (no database, no mutant runs).

Every mutant replaces exactly one span of its target, changes it, and leaves valid
Python; ids are unique; every mutant has a non-empty predeclared kill set of nodes
collected from its own group's test files; the SHA re-synchronisation covers t034.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("_l1w_w3b_mutants", HERE / "l1w_w3b_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mutants_apply_exactly_once_change_the_target_and_compile() -> None:
    script = _script()
    for group in script.GROUPS:
        text = (HERE.parent / group.target).read_text(encoding="utf-8")
        for mid, _desc, old, new in group.specs:
            assert text.count(old) == 1, mid
            assert old != new, mid
            compile(text.replace(old, new), str(group.target), "exec")


def test_ids_unique_and_every_mutant_has_a_kill_set_in_its_group() -> None:
    script = _script()
    ids = [spec[0] for group in script.GROUPS for spec in group.specs]
    assert len(ids) == len(set(ids))
    assert set(script.KILLS) == set(ids)
    for group in script.GROUPS:
        for mid, *_ in group.specs:
            kills = script.KILLS[mid]
            assert kills, mid
            assert all(node.split("::")[0] in group.test_files for node in kills), mid


def test_every_declared_kill_is_a_collected_node() -> None:
    script = _script()
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         *script.TEST_FILES],
        capture_output=True, text=True, check=False, cwd=HERE.parent,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    declared = set().union(*script.KILLS.values())
    assert declared <= collected, sorted(declared - collected)


def test_sha_literals_cover_t034_and_keep_the_w3a_targets() -> None:
    script = _script()
    literals = script.sha_literals()
    assert literals[script.FIRST_MEMBER_TARGET] == (
        "haloflow.m01.staff.l1w_w3b_units", (("_T034_SHA256", "_FIRST_ADMIN_BODY"),))
    assert script.STAFF_TARGET in literals and script.M05_TARGET in literals
    assert script.UNITS_TARGET not in literals and script.RUNNER_TARGET not in literals


def test_sha_sync_reproduces_every_committed_literal() -> None:
    """On the unmutated tree the sync's formula yields exactly the reviewed literals,
    so a mutant run differs from the control only by its SQL."""

    script = _script()
    for target in script.sha_literals():
        text = (HERE.parent / target).read_text(encoding="utf-8")
        for literal, sha in script.current_shas(target).items():
            assert f'{literal}: Final = "{sha}"' in text, (target, literal)
