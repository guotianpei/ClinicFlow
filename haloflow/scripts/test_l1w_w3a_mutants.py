"""Static controls for scripts/l1w_w3a_mutants.py (no database, no mutant runs).

Every mutant replaces exactly one span of its target and changes it; ids are unique;
every mutant has a non-empty predeclared kill set of collected W3a nodes; the SHA
re-synchronisation rewrites exactly the declared literals and nothing else.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("_l1w_w3a_mutants", HERE / "l1w_w3a_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mutants_apply_exactly_once_and_change_the_target() -> None:
    script = _script()
    for target, specs in ((script.STAFF_TARGET, script.STAFF_SPECS),
                          (script.M05_TARGET, script.M05_SPECS),
                          (script.ROLE_SAFETY_TARGET, script.ROLE_SAFETY_SPECS)):
        text = (HERE.parent / target).read_text(encoding="utf-8")
        for mid, _desc, old, new in specs:
            assert text.count(old) == 1, mid
            assert old != new, mid


def test_ids_unique_and_every_mutant_has_a_w3a_kill_set() -> None:
    script = _script()
    ids = [spec[0] for spec in (*script.STAFF_SPECS, *script.M05_SPECS,
                                *script.ROLE_SAFETY_SPECS)]
    assert len(ids) == len(set(ids))
    assert set(script.KILLS) == set(ids)
    for mid in ids:
        kills = script.KILLS[mid]
        assert kills, mid
        assert all(node.split("::")[0] in script.TEST_FILES for node in kills), mid


def test_every_declared_kill_is_a_collected_node() -> None:
    script = _script()
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         *script.TEST_FILES],
        capture_output=True, text=True, check=False, cwd=HERE.parent,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    declared = {node for kills in script.KILLS.values() for node in kills}
    assert declared <= collected, sorted(declared - collected)[:5]


def test_sha_sync_rewrites_only_the_declared_literals(tmp_path: Path) -> None:
    """On the unmutated targets the sync is the identity (literals already match)."""

    script = _script()
    for target in (script.STAFF_TARGET, script.M05_TARGET):
        path = HERE.parent / target
        backup = tmp_path / target.name
        shutil.copyfile(path, backup)
        try:
            script.sync_sha_literals(path)
            assert path.read_bytes() == backup.read_bytes(), target
        finally:
            shutil.copyfile(backup, path)


def test_sync_drops_the_targets_stale_bytecode(tmp_path: Path) -> None:
    """CI #229 measured stale literals: a same-size rewrite within the same second as
    a compile leaves a timestamp-valid .pyc. drop_bytecode removes the target's
    cached bytecode (and only the target's)."""

    script = _script()
    package = tmp_path / "pkg"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    (package / "gateways_v2.py").write_text("X = 1\n")
    stale = cache / "gateways_v2.cpython-312.pyc"
    other = cache / "gateways.cpython-312.pyc"
    stale.write_bytes(b"stale")
    other.write_bytes(b"keep")
    script.drop_bytecode(package / "gateways_v2.py")
    assert not stale.exists()
    assert other.exists()
