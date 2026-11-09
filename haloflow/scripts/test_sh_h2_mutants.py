"""Static controls for scripts/sh_h2_mutants.py (no database, no pytest runs).

Every mutant replaces exactly one span of its target, changes the text, has a unique
id and a non-empty predeclared kill set naming collected tests of its group's module;
the kill-set file names exactly the declared mutants (M1..M14, no M7).
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
        "_sh_h2_mutants", HERE / "sh_h2_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mutants_apply_exactly_once_and_change_the_target() -> None:
    script = _script()
    root = HERE.parent
    for mid, _desc, target, old, new, _kills in script.specs():
        text = (root / target).read_text(encoding="utf-8")
        assert text.count(old) == 1, mid
        assert old != new, mid


def test_ids_unique_and_kill_sets_named_in_group_module() -> None:
    script = _script()
    ids = [spec[0] for spec in script.specs()]
    assert len(ids) == len(set(ids))
    for spec in script.specs():
        assert spec[5], spec[0]
        homes = [label for label, target, tests in script.GROUPS
                 if script.in_group(target, tests, spec)]
        assert len(homes) == 1, (spec[0], homes)  # every mutant runs in exactly one group


def test_every_declared_kill_is_a_collected_node() -> None:
    script = _script()
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/m05/test_sh_h2_units.py", "tests/m05/test_sh_h2_postgres.py"],
        capture_output=True, text=True, check=False, cwd=HERE.parent,
    )
    assert result.returncode == 0
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    declared = {node for spec in script.specs() for node in spec[5]}
    assert declared <= collected, sorted(declared - collected)[:5]


def test_kill_file_names_exactly_the_declared_mutants() -> None:
    script = _script()
    assert set(script.kill_sets()) == {edit[0] for edit in script.EDITS}


def _probe(tmp: Path, env: dict[str, str] | None) -> str:
    """Prime a valid .pyc for value 1, rewrite the module to an equal-size value 2
    with the identical mtime, then import it in a fresh interpreter."""

    import os

    src = tmp / "probe_mod.py"
    src.write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(src, (1_700_000_000, 1_700_000_000))
    subprocess.run([sys.executable, "-c", "import probe_mod"], cwd=tmp, check=True,
                   env={k: v for k, v in os.environ.items()
                        if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")})
    assert list((tmp / "__pycache__").glob("probe_mod.*.pyc"))  # primed
    src.write_text("VALUE = 2\n", encoding="utf-8")  # same size
    os.utime(src, (1_700_000_000, 1_700_000_000))  # same mtime
    base = {k: v for k, v in os.environ.items()
            if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    result = subprocess.run([sys.executable, "-c", "import probe_mod; print(probe_mod.VALUE)"],
                            cwd=tmp, env={**base, **(env or {})}, capture_output=True,
                            text=True, check=True)
    return result.stdout.strip()


def test_stale_bytecode_hazard_is_real_without_the_guard(tmp_path: Path) -> None:
    # Negative control: the probe really reproduces the CP-DRIFT hazard.
    assert _probe(tmp_path, None) == "1"


def test_bytecode_env_compiles_the_source_on_disk(tmp_path: Path) -> None:
    # W4B-CACHE-1 regression control: primed, same size, same mtime -> the new source.
    script = _script()
    empty = tmp_path / "prefix"
    empty.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    assert _probe(work, script.bytecode_env(str(empty))) == "2"
    assert not any(empty.rglob("*.pyc"))  # nothing written under the prefix
