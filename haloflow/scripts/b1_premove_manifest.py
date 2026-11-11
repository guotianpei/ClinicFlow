"""Option B B1: generate the FROZEN pre-move manifest of the S-I TEST assembly.

Run ONCE, before any `haloflow.synthetic` source exists, from `haloflow/`:

    python -I scripts/b1_premove_manifest.py --source-commit <sha> \
        > tests/synthetic/b1_premove_manifest.json

It reads ONLY the test-side assembly as it stood at that commit
(`tests/m05/si_support.si_registry`, `si_support.si_catalog`, and the TEST profile SQL
constants in `sa_support` / `si_support`). It never imports `haloflow.synthetic`, so the
committed JSON is an oracle that is independent of the new builder (Codex pin, Option B
v1 review, Q4). The B1 equality rows compare the new builder against this file; they
must never regenerate it.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def canonical(value: Any) -> Any:
    """A deterministic, JSON-able rendering of the registry/catalogue value objects."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"__type__": type(value).__name__,
                **{f.name: canonical(getattr(value, f.name))
                   for f in dataclasses.fields(value)}}
    if isinstance(value, enum.Enum):
        return {"__enum__": type(value).__name__, "value": canonical(value.value)}
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (frozenset, set)):
        items = [canonical(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"no canonical form for {type(value).__name__}")


def digest(value: Any) -> str:
    raw = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def norm_sha(text: str) -> str:
    """The production statement-manifest normalisation (whitespace-collapsed)."""

    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def registry_manifest(registry: Any) -> dict[str, Any]:
    units = []
    for unit in registry:
        mid = unit.migration_id
        amendment = registry.acl_amendment(mid)
        units.append({
            "migration_id": mid,
            "template_sha256": text_sha(unit.template),
            "execution_role": unit.execution_role,
            "kind": unit.kind,
            "verification_sha256": digest(unit.verification),
            "policy_sha256": digest(unit.policy),
            "policy_verification_sha256": digest(unit.policy_verification),
            "closure_payload_sha256": digest(unit.closure_payload),
            "installed_state_requirement_sha256": digest(
                registry.installed_state_requirement(mid)),
            "acl_amendment_sha256": digest(amendment),
            "closure_targets": list(registry.closure_targets(mid)),
        })
    return {"target_version": registry.target_version,
            "migration_ids": list(registry.migration_ids), "units": units}


def catalog_manifest(compiled: Any) -> dict[str, Any]:
    statements = {
        s.key: {"mode": s.mode.value, "required_capability": s.required_capability,
                "sha256": norm_sha(s.query)}
        for s in compiled.catalog.statements()
    }
    return {"keys": list(compiled.catalog.keys()), "statements": statements,
            "activation_protected": sorted(compiled.activation_protected),
            "write_capabilities": sorted(compiled.write_capabilities)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()

    for sub in ("tests/m05", "tests/m02", "tests/m01", "tests", "src"):
        sys.path.insert(0, str(ROOT / sub))
    if any(name.startswith("haloflow.synthetic") for name in sys.modules):
        raise SystemExit("refusing: haloflow.synthetic already imported")

    import sa_support as sa
    import si_support as si

    manifest = {
        "schema": "b1-premove-manifest/v1",
        "source_commit": args.source_commit,
        "generated_from": ["tests/m05/si_support.py:si_registry",
                           "tests/m05/si_support.py:si_catalog",
                           "tests/m05/sa_support.py:SA_TEST_PROFILE_SQL",
                           "tests/m05/si_support.py:SI_TEST_PROFILE_SQL"],
        "registry": registry_manifest(si.si_registry()),
        "catalog": catalog_manifest(si.si_catalog()),
        "test_profile_sql": {"SA_TEST_PROFILE_SQL": text_sha(sa.SA_TEST_PROFILE_SQL),
                             "SI_TEST_PROFILE_SQL": text_sha(si.SI_TEST_PROFILE_SQL)},
    }
    if any(name.startswith("haloflow.synthetic") for name in sys.modules):
        raise SystemExit("refusing: the assembly imported haloflow.synthetic")
    json.dump(manifest, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
