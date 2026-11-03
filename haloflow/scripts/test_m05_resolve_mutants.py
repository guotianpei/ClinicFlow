"""Static controls for scripts/m05_resolve_mutants.py (no database).

Every mutant must be one contiguous change that composes: SQL mutants import as a
module whose recomputed hash literals match the mutated bodies, and each changes a
body; service mutants replace exactly one anchor. Every declared kill names an
existing test function in the group's module. A missing anchor is refused.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m05_resolve_mutants", HERE / "m05_resolve_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _all_sql(script: Any, checker: Any, text: str) -> tuple[Any, ...]:
    return tuple(
        mutant
        for tests in (script.BINDING, script.RESOLVE)
        for mutant in script.build_sql_mutants(text, checker.Mutant, sys.executable,
                                               only=tests)
    )


def test_sql_mutants_compose_and_change_one_span() -> None:
    script = _script()
    checker = script._load_checker()
    text = script.GATEWAYS.read_text(encoding="utf-8")
    mutants = _all_sql(script, checker, text)
    assert [m.id for m in mutants] == ["RM-ENV", "RM-DEDUPE", "RM-REUSE", "RM-CATCHALL",
                                       "RM-NOTEREQ"]
    sys.path.insert(0, "src")
    original = script.measure_bodies(sys.executable, text)
    for mutant in mutants:
        assert text.count(mutant.old) == 1, mutant.id
        mutated = text.replace(mutant.old, mutant.new)
        namespace: dict[str, Any] = {"__name__": "_mutant"}
        exec(compile(mutated, "gateways_mutant.py", "exec"), namespace)  # noqa: S102
        for body, digest in namespace["_BODIES"].values():
            assert hashlib.sha256(body.encode("utf-8")).hexdigest() == digest, mutant.id
        assert script.measure_bodies(sys.executable, mutated) != original, mutant.id
        assert mutant.kills, mutant.id


def test_service_mutants_replace_one_anchor() -> None:
    script = _script()
    checker = script._load_checker()
    text = script.SERVICE.read_text(encoding="utf-8")
    mutants = tuple(
        mutant for tests in (script.RESOLVE, script.NOTES)
        for mutant in script.build_service_mutants(text, checker.Mutant, only=tests)
    )
    assert [m.id for m in mutants] == ["SM-HMAC", "SM-ENCFIRST", "SM-ORPHAN", "SM-NORM"]
    for mutant in mutants:
        assert text.count(mutant.old) == 1, mutant.id
        compile(text.replace(mutant.old, mutant.new), "service_mutant.py", "exec")
        assert mutant.kills, mutant.id


def test_every_declared_kill_names_an_existing_test() -> None:
    script = _script()
    specs = [(tests, kills) for _, _, tests, _, kills in script.SQL_SPECS]
    specs += [(tests, kills) for _, _, tests, _, kills in script.SERVICE_SPECS]
    for tests, kills in specs:
        source = Path(tests).read_text(encoding="utf-8")
        for node in kills:
            module, name = node.split("::", 1)
            assert module == tests, node
            function = name.split("[", 1)[0]
            assert f"def {function}(" in source, node


def test_a_missing_anchor_is_refused() -> None:
    script = _script()
    checker = script._load_checker()
    text = script.SERVICE.read_text(encoding="utf-8").replace(
        "if appended.appended_or_existing", "if appended.outcome")
    try:
        script.build_service_mutants(text, checker.Mutant, only=script.RESOLVE)
    except SystemExit as refused:
        assert "SM-ORPHAN" in str(refused)
    else:  # pragma: no cover - the control must refuse
        raise AssertionError("a missing anchor was not refused")


def test_v2_secondary_file_is_restored_when_the_run_fails(tmp_path: Path) -> None:
    """Codex 000140Z: gateways_v2.py is restored byte for byte, and re-checked, even
    when the mutant run raises; the control run never synchronises."""

    script = _script()
    target = tmp_path / "gateways_v2.py"
    target.write_bytes(b"original v2 bytes\n")
    synced: list[str] = []

    def sync(path: Path, python: str) -> None:
        synced.append(python)
        path.write_bytes(b"mutated literals\n")

    class Checker:
        @staticmethod
        def run_pytest(pytest: str, label: str, evidence: Path) -> Any:
            assert target.read_bytes() == (
                b"original v2 bytes\n" if label == "control" else b"mutated literals\n")
            if label != "control":
                raise RuntimeError("pytest crashed")
            return "control-evidence"

    runner = script._v2_synced_runner(Checker, target=target, sync=sync)
    assert runner("/venv/bin/pytest", "control", tmp_path) == "control-evidence"
    assert synced == []
    try:
        runner("/venv/bin/pytest", "RM-X", tmp_path)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the run failure was swallowed")
    assert target.read_bytes() == b"original v2 bytes\n"
    assert synced == ["/venv/bin/python"]
    digest = hashlib.sha256(b"original v2 bytes\n").hexdigest()
    assert runner.records == [("control", digest, digest), ("RM-X", digest, digest)]
