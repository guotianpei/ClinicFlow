"""Static controls for scripts/m05_create_mutants.py (no database).

Each mutant must be one contiguous change whose result composes: the mutated body
appears exactly as intended, its hash literal is recomputed, and nothing outside
the anchor-to-hash span changes. A mismatch here would make every mutant run
error at composition, which the checker would (correctly) refuse as not-a-kill.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m05_create_mutants", HERE / "m05_create_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _body_and_hash(text: str) -> tuple[str, str]:
    body = re.search(r'_CREATE_BODY: Final = r"""(.*?)"""', text, re.DOTALL)
    digest = re.search(r'_CREATE_BODY_SHA256: Final = "([0-9a-f]{64})"', text)
    assert body is not None and digest is not None
    return body.group(1), digest.group(1)


def test_each_mutant_is_one_change_that_recomputes_the_hash() -> None:
    script = _script()
    checker = script._load_checker()
    text = script.TARGET.read_text(encoding="utf-8")
    body, digest = _body_and_hash(text)
    assert hashlib.sha256(body.encode()).hexdigest() == digest
    mutants = script.build_mutants(text, checker.Mutant)
    assert [m.id for m in mutants] == ["MC-COUNT", "MC-PRODUCER", "MC-WORKLOAD", "MC-DIGEST"]
    for (mutant_id, _, anchor, replacement, kills), mutant in zip(
        script.SPECS, mutants, strict=True
    ):
        assert text.count(mutant.old) == 1, mutant_id
        mutated = text.replace(mutant.old, mutant.new)
        mutated_body, mutated_digest = _body_and_hash(mutated)
        assert mutated_body == body.replace(anchor, replacement, 1), mutant_id
        assert mutated_body != body, mutant_id
        assert hashlib.sha256(mutated_body.encode()).hexdigest() == mutated_digest, mutant_id
        # Nothing outside the body and its hash literal changes.
        assert mutated.replace(mutated_body, "").replace(mutated_digest, "") == text.replace(
            body, ""
        ).replace(digest, ""), mutant_id
        assert mutant.kills == kills and kills, mutant_id


def test_every_declared_kill_names_an_existing_test() -> None:
    script = _script()
    source = Path(script.TESTS).read_text(encoding="utf-8")
    for _, _, _, _, kills in script.SPECS:
        for node in kills:
            name = node.split("::", 1)[1]
            assert f"def {name}(" in source, node


def test_a_missing_anchor_is_refused() -> None:
    script = _script()
    checker = script._load_checker()
    text = script.TARGET.read_text(encoding="utf-8").replace("IF v_matches <> 1 THEN", "IF 1 THEN")
    try:
        script.build_mutants(text, checker.Mutant)
    except SystemExit as refused:
        assert "MC-COUNT" in str(refused) or "hash literal" in str(refused)
    else:  # pragma: no cover - the control must refuse
        raise AssertionError("a missing anchor was not refused")
