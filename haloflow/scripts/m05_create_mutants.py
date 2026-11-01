"""Focused mutation controls for m05_create_item (UC1 part B, B2).

Codex 20261101T003810Z (codex_b2-implementation-review-cd7d999): four named
mutants of the t014 body, each run against tests/m05/test_m05_create_postgres.py:

- MC-COUNT     the `count <> 1` intake guard removed (a multi-match picks a row);
- MC-PRODUCER  the intake-row producer = caller check bypassed;
- MC-WORKLOAD  the source workload = caller check bypassed;
- MC-DIGEST    the existing-row digest comparison bypassed.

The judging rules are exactly the part A checker's (scripts/m02_observation_mutants.py,
reviewed with its 32 controls): the control run must pass completely; a mutant is
KILLED only by a completed run whose every failure is in its PREDECLARED kill set,
with no error/skip and the control's node set; the target is restored after every
run and its SHA-256 re-checked. This script loads that checker and points it at
the M05 target, test module and mutants; it adds no judging logic of its own.

The t014 body's SHA-256 is a reviewed literal, so a mutated body with the old
literal would fail composition (every test erroring, which the checker refuses as
not-a-kill). Each mutant is therefore ONE contiguous textual change spanning the
mutated statement through the `_CREATE_BODY_SHA256` line, with the hash recomputed
for the mutated body. `build_mutants()` derives these from the file as it stands
and refuses unless each anchor occurs exactly once.

Usage (from haloflow/):
    python3 -I scripts/m05_create_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/m05-mutation"
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TARGET = Path("src/haloflow/m05/gateways.py")
TESTS = "tests/m05/test_m05_create_postgres.py"
TITLE_FROM = "m02-observation-mutants"
TITLE_TO = "m05-create-mutants"

_BODY = re.compile(r'(_CREATE_BODY: Final = r""")(.*?)(""")', re.DOTALL)
_SHA_LINE = re.compile(r'_CREATE_BODY_SHA256: Final = "[0-9a-f]{64}"\n')

_T = TESTS + "::"
_MC03 = _T + "test_mc03_same_source_and_kind_other_purpose_is_dedupe_conflict"
_MC04 = _T + "test_mc04_ineligible_callers_and_sources_share_one_shape"
_MC05 = _T + "test_mc05_unmapped_ambiguous_absent_and_null_patient_refused"

# (id, description, exact anchor in the t014 body, replacement, predeclared kills)
SPECS: tuple[tuple[str, str, str, str, frozenset[str]], ...] = (
    (
        "MC-COUNT",
        "multi-match intake guard removed",
        "    IF v_matches <> 1 THEN\n"
        "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';\n"
        "    END IF;\n",
        "",
        frozenset({_MC05}),
    ),
    (
        "MC-PRODUCER",
        "intake-row producer = caller check bypassed",
        "    IF v_row.producer_workload_id IS DISTINCT FROM v_principal\n       OR ",
        "    IF ",
        frozenset({_MC04}),
    ),
    (
        "MC-WORKLOAD",
        "source workload = caller check bypassed",
        "\n       OR (v_env OPERATOR(pg_catalog.->>) 'workload_id') IS DISTINCT FROM"
        " v_principal::text THEN",
        " THEN",
        frozenset({_MC04}),
    ),
    (
        "MC-DIGEST",
        "existing-row digest comparison bypassed",
        "        IF v_existing.create_digest <> v_digest THEN\n"
        "            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DEDUPE_CONFLICT';\n"
        "        END IF;\n",
        "",
        frozenset({_MC03}),
    ),
)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_mutants(text: str, mutant_class: Any) -> tuple[Any, ...]:
    """One contiguous change per mutant: from its anchor through the hash line."""

    body_match = _BODY.search(text)
    sha_match = _SHA_LINE.search(text)
    if body_match is None or sha_match is None or sha_match.start() < body_match.end():
        raise SystemExit("t014 body or hash literal not found in the expected order")
    body = body_match.group(2)
    if hashlib.sha256(body.encode("utf-8")).hexdigest() not in sha_match.group(0):
        raise SystemExit("the unmutated t014 hash literal does not match its body")
    mutants = []
    for mutant_id, description, anchor, replacement, kills in SPECS:
        if body.count(anchor) != 1:
            raise SystemExit(f"{mutant_id}: anchor occurs {body.count(anchor)} times")
        start = body_match.start(2) + body.index(anchor)
        end = sha_match.end()
        mutated_body = body.replace(anchor, replacement, 1)
        digest = hashlib.sha256(mutated_body.encode("utf-8")).hexdigest()
        old = text[start:end]
        new = (
            replacement
            + text[start + len(anchor) : sha_match.start()]
            + f'_CREATE_BODY_SHA256: Final = "{digest}"\n'
        )
        mutants.append(mutant_class(mutant_id, description, old, new, kills))
    return tuple(mutants)


def main(argv: list[str] | None = None) -> int:
    checker = _load_checker()
    checker.TARGET = TARGET
    checker.TESTS = TESTS
    checker.MUTANTS = build_mutants(TARGET.read_text(encoding="utf-8"), checker.Mutant)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        status = int(checker.main(argv))
    sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}", f"title={TITLE_TO}"))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
