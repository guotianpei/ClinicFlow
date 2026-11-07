"""L-1S package S-G1 (part a) — synthetic registry and catalogue in source (no DB).

Governing: S-G1 package v1 + v1r1 C2 (Work Session 2026-11-07), Codex ALIGN for tests
20261107T171844Z, owner OD-SG1 (production promotion waits for D-S3 checkpoint 2).
Every S-G import is deferred into the row body (class-B red rule).

Pinned API (`haloflow.m05.synthetic_registry`): `SYNTHETIC_TARGET == 46`;
`build_synthetic_tenant_migrations()` and `build_synthetic_statement_catalog()`, built
only from source modules (production units + the S-A/S-B/S-C source units, profiles,
statements and protected declarations). The production composition is untouched.

- SG-U01  parity with the tests' S-C registry and catalogue (ids, checksums, target,
          profiles, statement keys, protected set).
- SG-U01b no test-module import (AST); production registry target and supported set
          unchanged at 36.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import sc_support as sc

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"


def _mod() -> Any:
    import haloflow.m05.synthetic_registry as module

    return module


def _units(registry: Any) -> list[tuple[str, str]]:
    return [(unit.migration_id, unit.checksum) for unit in registry]


def test_sg_u01_registry_and_catalogue_parity() -> None:
    module = _mod()
    synthetic, reference = module.build_synthetic_tenant_migrations(), sc.sc_registry()
    assert module.SYNTHETIC_TARGET == 46 == synthetic.target_version
    assert synthetic.target_version == reference.target_version
    assert _units(synthetic) == _units(reference)
    for unit in reference:
        assert (synthetic.installed_state_requirement(unit.migration_id)
                == reference.installed_state_requirement(unit.migration_id)), unit.migration_id
        assert (synthetic.acl_amendment(unit.migration_id)
                == reference.acl_amendment(unit.migration_id)), unit.migration_id
    catalog, expected = module.build_synthetic_statement_catalog(), sc.sc_catalog()
    assert catalog.catalog.keys() == expected.catalog.keys()
    assert [(s.query, s.mode, s.required_capability) for s in catalog.catalog.statements()] == [
        (s.query, s.mode, s.required_capability) for s in expected.catalog.statements()]
    assert catalog.activation_protected == expected.activation_protected == sc.protected()


def test_sg_u01b_source_only_and_production_unchanged() -> None:
    path = SRC / "m05" / "synthetic_registry.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert all(name.startswith(("haloflow.", "__future__", "typing", "collections"))
               or name in {"", "functools"} for name in imported), imported
    assert not any("tests" in name or "support" in name for name in imported), imported
    import haloflow.composition as composition

    assert frozenset({36}) == composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert composition.build_production_tenant_migrations().target_version == 36
    assert _mod().SYNTHETIC_TARGET == 46
