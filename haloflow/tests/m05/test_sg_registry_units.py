"""L-1S package S-G1 (part a) — synthetic composition stays test-side (no DB).

Governing: S-G1 package v1 + v1r1 (Work Session 2026-11-07); placement per Codex ALIGN
20261107T174755Z (option A): F-1/B2.11 (only `composition.py` composes a catalogue or a
registry) and SA-S01/SB-S01 (`composition.py` names no S-A/S-B module) together leave
no source location for a target-46 registry, so the synthetic registry, catalogue and
application composition are built test-side from `sc_support`. Owner OD-SG1: the units
reach the production composition only at D-S3 checkpoint 2. This test-side harness is
NOT acceptance of an installable running application (that deliverable stays open).

- SG-U01  the test-side registry: production units are an exact prefix, then exactly the
          S-A, S-B and S-C units; every function unit carries an installed-state
          profile owned by and executing as the M05 owner; the runtime role may execute
          the intake/due/probe functions but NOT activation change or state.
- SG-U01a the test-side catalogue adds exactly the five intake keys, each requiring its
          own capability and each activation-protected; production protection kept.
- SG-U01b production exclusion: target and supported set {36}; no S-A/S-B/S-C unit id
          or intake key in the production registry or catalogue.
- SG-U01c no source module reaches test support, statically or through a dynamic import.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import sa_support as sa
import sb_support as sb
import sc_support as sc

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"
OWNER = "haloflow_m05_owner"
RUNTIME = "haloflow_runtime"
NO_RUNTIME_EXECUTE = frozenset({"t042_m05_activation_change", "t043_m05_activation_state"})
UNPROFILED = frozenset({"t037_m05_intake_tables", "t044_m05_reminder_windows_due"})
INTAKE_KEYS = {"m05.intake.close": "write", "m05.intake.directory": "write",
               "m05.intake.dispatch": "write", "m05.intake.due_windows": "read",
               "m05.intake.inbound": "write"}
SYNTHETIC_IDS = (*sa.SA_UNIT_IDS, *sb.SB_UNIT_IDS, sc.SC_UNIT_ID)


def test_sg_u01_test_side_registry() -> None:
    from haloflow.composition import build_production_tenant_migrations

    registry, production = sc.sc_registry(), build_production_tenant_migrations()
    assert registry.target_version == sc.SC_TARGET == 46
    ids, prod = list(registry.migration_ids), list(production.migration_ids)
    assert ids[:len(prod)] == prod
    assert tuple(ids[len(prod):]) == SYNTHETIC_IDS
    for unit_id in SYNTHETIC_IDS:
        requirement = registry.installed_state_requirement(unit_id)
        assert registry.acl_amendment(unit_id) is None, unit_id
        if unit_id in UNPROFILED:
            assert not requirement.required and requirement.profile is None, unit_id
            continue
        profile = requirement.profile
        assert requirement.required and profile is not None, unit_id
        assert (profile.owner, profile.execution_role, profile.security_definer) == (
            OWNER, OWNER, True), unit_id
        grantees = {grantee for grantee, privilege, _grantor, _option in profile.installed_acl
                    if privilege == "EXECUTE"}
        assert OWNER in grantees, unit_id
        assert (RUNTIME in grantees) is (unit_id not in NO_RUNTIME_EXECUTE), unit_id


def test_sg_u01a_test_side_catalogue() -> None:
    from haloflow.composition import build_production_catalog

    catalog, production = sc.sc_catalog(), build_production_catalog()
    added = {s.key: s for s in catalog.catalog.statements()
             if s.key not in set(production.catalog.keys())}
    assert {key: s.mode for key, s in added.items()} == INTAKE_KEYS
    assert all(s.required_capability == key for key, s in added.items())
    assert catalog.activation_protected == sc.protected()
    assert set(INTAKE_KEYS) <= catalog.activation_protected
    assert production.activation_protected <= catalog.activation_protected


def test_sg_u01b_production_exclusion() -> None:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_catalog,
        build_production_tenant_migrations,
    )

    assert frozenset({36}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    registry = build_production_tenant_migrations()
    assert registry.target_version == 36
    assert not set(registry.migration_ids) & set(SYNTHETIC_IDS)
    assert not set(INTAKE_KEYS) & set(build_production_catalog().catalog.keys())


_FORBIDDEN_SUFFIXES = ("_support", "_harness", "conftest")


def _reaches_test_code(name: str) -> bool:
    head = name.split(".")[0]
    return head in {"tests", "conftest"} or any(
        part.endswith(_FORBIDDEN_SUFFIXES) for part in name.split("."))


def _violations(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _reaches_test_code(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found += [f"{module}.{a.name}" for a in node.names
                      if _reaches_test_code(module) or _reaches_test_code(a.name)]
        elif isinstance(node, ast.Call):
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            if name in {"import_module", "__import__", "spec_from_file_location"}:
                first = node.args[0] if node.args else None
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)
                        and not _reaches_test_code(first.value)):
                    found.append(f"dynamic {name}")
    return found


@pytest.mark.parametrize("source", [
    "import sc_support\n", "from tests.m05 import sc_support\n", "import conftest\n",
    "from sc_support import sc_registry\n", "from x import sg_harness\n",
    "import importlib\nimportlib.import_module('sc_support')\n",
    "import importlib\nimportlib.import_module(name)\n", "__import__('tests.m05')\n",
], ids=range(8))
def test_sg_u01c_detector_negative_controls(source: str) -> None:
    assert _violations(source)


def test_sg_u01c_no_source_module_reaches_test_support() -> None:
    assert _violations("import importlib\nimportlib.import_module('pglast')\n") == []
    paths = sorted(SRC.rglob("*.py"))
    assert SRC / "m05" / "synthetic_ingress.py" in paths
    offenders = {str(p.relative_to(SRC)): v for p in paths
                 if (v := _violations(p.read_text(encoding="utf-8")))}
    assert offenders == {}
