"""Focused mutation controls for UC1 part B, B4 (resolve and notes).

Package v2 7 (and v1 4) promised these mutants, written with the implementation:

SQL (src/haloflow/m05/gateways.py; the prepare/apply bodies are generated from one
template, so every mutant recomputes both body hash literals):
- RM-ENV       the envelope binding removed (only the NULL / unknown check stays);
- RM-DEDUPE    the producer_dedupe_key predicate removed;
- RM-REUSE     the reuse pre-check AND the named-constraint catch removed (removing
               the pre-check alone is an EQUIVALENT mutant: the named catch then
               yields the same RESOLUTION_EVENT_REUSED; disclosed, not run);
- RM-CATCHALL  the catch maps ANY unique violation to RESOLUTION_EVENT_REUSED;
- RM-NOTEREQ   the SQL NOTE_REQUIRED check removed (`other` without a note).
Service (src/haloflow/m05/service.py):
- SM-HMAC      the replay challenge HMACs with the CURRENT key, not the stored version;
- SM-ENCFIRST  the current encryption key is taken before the prepare classification;
- SM-ORPHAN    the existing-event (orphan) check removed;
- SM-NORM      the D11-normalized note is not stored (the raw note is HMAC'd).

The judging rules are exactly the part A checker's (scripts/m02_observation_mutants.py):
the control run of the group's test module must pass completely, a mutant is KILLED
only by a completed run whose every failure is in its PREDECLARED kill set (no
error, no skip, the control's node set), and the target is restored after every run
and its SHA-256 re-checked. This script runs that checker once per group (target,
test module, mutants) and adds no judging logic of its own.

Body hashes: gateways.py builds its bodies at import time, so the mutated bodies are
measured by importing the MUTATED module text in the project interpreter (the
`python` beside --pytest), never by trusting a literal.

Usage (from haloflow/):
    python3 -I scripts/m05_resolve_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/m05-resolve-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GATEWAYS = Path("src/haloflow/m05/gateways.py")
SERVICE = Path("src/haloflow/m05/service.py")
RESOLVE = "tests/m05/test_m05_resolve_postgres.py"
BINDING = "tests/m05/test_m05_resolve_binding_postgres.py"
NOTES = "tests/m05/test_m05_notes_units.py"
TITLE_FROM = "m02-observation-mutants"

_SHA = {
    "_PREPARE_BODY": re.compile(r'_PREPARE_BODY_SHA256: Final = "([0-9a-f]{64})"\n'),
    "_APPLY_BODY": re.compile(r'_APPLY_BODY_SHA256: Final = "([0-9a-f]{64})"\n'),
}

MATRIX = ("type", "version", "level", "status", "operation-id", "occurred-at-only",
          "time-quality-only", "actor", "patient", "causation", "links-none",
          "links-extra", "links-relationship", "links-other-item", "metadata-outcome",
          "metadata-assertion", "metadata-item-kind", "metadata-missing-key", "dedupe")
DESCRIPTORS = ("no-item", "item-not-object", "missing-key", "extra-key", "wrong-item",
               "item-id-uppercase", "patient-not-uuid", "source-uppercase", "unknown-kind",
               "kind-not-str")


def _nodes(module: str, *names: str) -> frozenset[str]:
    return frozenset(f"{module}::{name}" for name in names)


_ENV_CONDITION_START = (
    "        IF v_env IS NULL\n           OR (v_env OPERATOR(pg_catalog.->>) 'event_type')"
)
_DEDUPE = (
    "           OR (v_env OPERATOR(pg_catalog.->>) 'producer_dedupe_key') IS DISTINCT FROM\n"
    "              ('m05r1:' || pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(\n"
    "                  '[\"m05.resolve/v1\",' || pg_catalog.to_json(v_setting)::text\n"
    "                  || ',' || pg_catalog.to_json(v_item::text)::text\n"
    "                  || ',' || pg_catalog.to_json(v_key::text)::text || ']', 'UTF8')), 'hex'))\n"
)
_REUSE_PRE = (
    "        IF EXISTS (SELECT 1 FROM {schema}.m05_followup_history AS h\n"
    "                    WHERE h.resolution_event_id = v_event) THEN\n"
    "            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'RESOLUTION_EVENT_REUSED';\n"
    "        END IF;\n"
)
_REUSE_CATCH = (
    "            IF v_constraint = 'm05_followup_history_resolution_event_key' THEN\n"
)
_NOTE_REQUIRED = (
    "        IF v_note_required AND NOT v_note_present THEN\n"
    "            @@REFUSE:NOTE_REQUIRED@@\n"
    "        END IF;\n"
)


def _env_removed(text: str) -> tuple[str, str]:
    """The whole binding condition -> `IF v_env IS NULL THEN` (one contiguous span)."""

    start = text.index(_ENV_CONDITION_START)
    end = text.index("        THEN\n", start) + len("        THEN\n")
    return text[start:end], "        IF v_env IS NULL THEN\n"


# (id, description, target, tests, [(anchor, replacement)] or callable, kills)
SQL_SPECS: tuple[tuple[str, str, str, Any, frozenset[str]], ...] = (
    ("RM-ENV", "envelope binding removed (NULL check kept)", BINDING, _env_removed,
     _nodes(BINDING, *(f"test_mr10_one_predicate_false_is_invalid[{c}]" for c in MATRIX),
            "test_mr10_workload_principal_is_invalid",
            "test_mr10_real_other_type_event_is_invalid",
            "test_mr10_precedence_binding_before_reuse")),
    ("RM-DEDUPE", "producer_dedupe_key predicate removed", BINDING, ((_DEDUPE, ""),),
     _nodes(BINDING, "test_mr10_one_predicate_false_is_invalid[dedupe]")),
    ("RM-REUSE", "reuse pre-check and named catch removed", BINDING,
     ((_REUSE_PRE, ""), (_REUSE_CATCH, "            IF false THEN\n")),
     _nodes(BINDING, "test_mr10_genuine_reuse_is_reused")),
    ("RM-CATCHALL", "any unique violation mapped to REUSED", RESOLVE,
     ((_REUSE_CATCH, "            IF true THEN\n"),),
     _nodes(RESOLVE, "test_uq1_only_the_named_constraint_maps_to_reused")),
    ("RM-NOTEREQ", "SQL NOTE_REQUIRED check removed", RESOLVE, ((_NOTE_REQUIRED, ""),),
     _nodes(RESOLVE, "test_ma_or_other_requires_note_in_sql_too")),
)

SERVICE_SPECS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...], frozenset[str]], ...] = (
    ("SM-HMAC", "replay challenge HMAC with the current key", RESOLVE,
     (("key = self._key(lambda: self._keys.hmac_key(detail),",
       "key = self._key(lambda: self._keys.current_hmac()[1],"),),
     _nodes(RESOLVE, "test_rp1_rp2_exact_retry_replays[note]",
            "test_rp6_rotated_key_retained_replays_with_stored_version",
            "test_rp7_retired_version_is_unverifiable",
            "test_rp12_historical_replay_with_current_keys_unavailable")),
    ("SM-ENCFIRST", "current encryption key taken before classification", RESOLVE,
     (("            handle = binding.handle\n",
       "            handle = binding.handle\n"
       "            if action.note is not None:\n"
       "                self._key(lambda: self._keys.current_encryption(),\n"
       "                          lambda: ActionFailed(\"KEY_UNAVAILABLE\"))\n"),),
     _nodes(RESOLVE, "test_rp1_rp2_exact_retry_replays[note]",
            "test_rp12_historical_replay_with_current_keys_unavailable",
            *(f"test_rp14_new_descriptor_bound_before_keys_and_append[{c}]"
              for c in DESCRIPTORS))),
    ("SM-ORPHAN", "existing-event (orphan) check removed", RESOLVE,
     (('        if appended.appended_or_existing != "appended":\n',
       "        if False:\n"),),
     _nodes(RESOLVE, "test_mr08_orphan_resolution_event_fails_closed")),
    ("SM-NORM", "normalized note not stored (raw note HMAC'd)", NOTES,
     (('        object.__setattr__(self, "note", note)\n', ""),),
     _nodes(NOTES, "test_rv1_resolve_action_controls")),
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


_MEASURE = """
import hashlib, json, sys
sys.path.insert(0, "src")
text = sys.stdin.read()
ns = {"__name__": "_m05_gateways_mutant"}
exec(compile(text, "gateways_mutant.py", "exec"), ns)
print(json.dumps({k: hashlib.sha256(ns[k].encode("utf-8")).hexdigest()
                  for k in ("_PREPARE_BODY", "_APPLY_BODY")}))
"""


def measure_bodies(python: str, text: str) -> dict[str, str]:
    """The SHA-256 of the prepare and apply bodies that `text` builds."""

    done = subprocess.run([python, "-c", _MEASURE], input=text, capture_output=True,
                          text=True, timeout=120, check=False)
    if done.returncode != 0:
        raise SystemExit(f"measuring the mutated bodies failed: {done.stderr[-400:]}")
    measured: dict[str, str] = json.loads(done.stdout)
    return measured


def _with_hashes(text: str, digests: dict[str, str]) -> str:
    for name, pattern in _SHA.items():
        match = pattern.search(text)
        if match is None:
            raise SystemExit(f"{name} hash literal not found")
        text = text[: match.start(1)] + digests[name] + text[match.end(1):]
    return text


def build_sql_mutants(text: str, mutant_class: Any, python: str,
                      only: str | None = None) -> tuple[Any, ...]:
    """One contiguous change per mutant, from its first anchor through the last hash
    literal; the original hash literals must match the original bodies."""

    original = measure_bodies(python, text)
    for name, pattern in _SHA.items():
        match = pattern.search(text)
        if match is None or match.group(1) != original[name]:
            raise SystemExit(f"the unmutated {name} hash literal does not match its body")
    mutants = []
    for mutant_id, description, tests, change, kills in SQL_SPECS:
        if only is not None and tests != only:
            continue
        pairs = change(text) if callable(change) else change
        mutated = text
        for anchor, replacement in ((pairs,) if isinstance(pairs[0], str) else pairs):
            if mutated.count(anchor) != 1:
                raise SystemExit(f"{mutant_id}: anchor occurs {mutated.count(anchor)} times")
            mutated = mutated.replace(anchor, replacement, 1)
        digests = measure_bodies(python, mutated)
        if digests == original:
            raise SystemExit(f"{mutant_id}: the mutation does not change any body")
        mutated = _with_hashes(mutated, digests)
        first = min(text.index(a) for a, _ in ((pairs,) if isinstance(pairs[0], str) else pairs))
        last = max(p.search(text).end() for p in _SHA.values())  # type: ignore[union-attr]
        tail = len(text) - last
        old = text[first:last]
        new = mutated[first: len(mutated) - tail]
        if text.replace(old, new, 1) != mutated:
            raise SystemExit(f"{mutant_id}: the change is not one contiguous span")
        mutants.append(mutant_class(mutant_id, description, old, new, kills))
    return tuple(mutants)


def build_service_mutants(text: str, mutant_class: Any,
                          only: str | None = None) -> tuple[Any, ...]:
    mutants = []
    for mutant_id, description, tests, pairs, kills in SERVICE_SPECS:
        if only is not None and tests != only:
            continue
        ((anchor, replacement),) = pairs
        if text.count(anchor) != 1:
            raise SystemExit(f"{mutant_id}: anchor occurs {text.count(anchor)} times")
        mutants.append(mutant_class(mutant_id, description, anchor, replacement, kills))
    return tuple(mutants)


GROUPS = (
    ("sql-binding", GATEWAYS, BINDING),
    ("sql-resolve", GATEWAYS, RESOLVE),
    ("service-resolve", SERVICE, RESOLVE),
    ("service-notes", SERVICE, NOTES),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    python = str(Path(args.pytest).with_name("python"))
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        text = target.read_text(encoding="utf-8")
        if target == GATEWAYS:
            checker.MUTANTS = build_sql_mutants(text, checker.Mutant, python, only=tests)
        else:
            checker.MUTANTS = build_service_mutants(text, checker.Mutant, only=tests)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=m05-resolve-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
