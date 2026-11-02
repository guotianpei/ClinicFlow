"""UC1 part B, tranche B1 — static rows (no database).

Rows (ids are B1-local; package ids where one exists):
- MB1-S01 the M05 unit set, order and kinds;
- MB1-S02 installed-state profiles for every typed M05 unit, exact values;
- MB1-S03 the M05 test registry: production ids + M05 ids, target 20;
- MB1-S04 the M05 statement definitions, exact;
- MB1-S05 retired in B5: it pinned production unchanged in B1..B4 (Codex
  20261031T190524Z); B5 composes M05 into production, pinned by B5-C01..C10
  (test_b5_composition_units.py);
- MB1-S06 part A checksum pins for t004/t009 (the resolver change in B4 must move
  them together with the retained-installation compatibility case);
- MB1-S07 retired in B5: the M05 test manifest differed from production in exactly
  one value; B5 makes that change in production (B5-C08 pins the manifest).

Expected values come from tests/m05/m05_support.py, never from the code under test.
"""

from __future__ import annotations

from typing import Any

import m05_support as m05
from m05_harness import m05_registry

TYPED_KIND = "typed_function_v3"
ORDINARY_UNITS = ("t012_m05_tables", "t013_m05_seed")


def _unit_shape(definition: Any) -> tuple[str | None, str | None]:
    """(kind, execution_role) of a unit definition; a plain string is ordinary."""

    if isinstance(definition, str):
        return None, None
    return definition.kind, definition.execution_role


def test_mb1_s01_m05_unit_set_order_and_kinds() -> None:
    from haloflow.m05.units import M05_TENANT_MIGRATIONS

    assert tuple(M05_TENANT_MIGRATIONS) == m05.M05_UNIT_IDS
    for migration_id in ORDINARY_UNITS:
        assert _unit_shape(M05_TENANT_MIGRATIONS[migration_id]) == (None, None), migration_id
    for migration_id in m05.M05_TYPED_UNITS:
        assert _unit_shape(M05_TENANT_MIGRATIONS[migration_id]) == (
            TYPED_KIND,
            m05.M05_OWNER,
        ), migration_id


def test_mb1_s02_installed_state_profiles_exact() -> None:
    from haloflow.m05.gateways import M05_GATEWAY_PROFILES

    assert set(M05_GATEWAY_PROFILES) == set(m05.M05_TYPED_UNITS)
    specs = {spec.name: spec for spec in m05.FUNCTIONS}
    for migration_id, name in m05.M05_TYPED_UNITS.items():
        profile = M05_GATEWAY_PROFILES[migration_id]
        spec = specs[name]
        assert (
            profile.function_name,
            profile.argument_types,
            profile.owner,
            profile.execution_role,
            profile.prokind,
            profile.return_type,
            profile.returns_set,
            profile.language,
            profile.security_definer,
            profile.volatility,
            profile.parallel,
            profile.strict,
            profile.installed_acl,
        ) == (
            name,
            (spec.args,),
            m05.M05_OWNER,
            m05.M05_OWNER,
            "f",
            spec.returns,
            False,
            "plpgsql",
            True,
            m05.FUNCTION_VOLATILITY[name],
            "u",
            False,
            m05.FUNCTION_ACL,
        ), migration_id


def test_mb1_s03_test_registry_ids_target_and_roles() -> None:
    registry = m05_registry()
    assert registry.migration_ids == m05.PART_A_UNIT_IDS + m05.M05_UNIT_IDS
    assert registry.target_version == m05.M05_TEST_TARGET
    by_id = {unit.migration_id: unit for unit in registry}
    for migration_id in ORDINARY_UNITS:
        assert (by_id[migration_id].kind, by_id[migration_id].execution_role) == (None, None)
    for migration_id in m05.M05_TYPED_UNITS:
        assert (by_id[migration_id].kind, by_id[migration_id].execution_role) == (
            TYPED_KIND,
            m05.M05_OWNER,
        )


def test_mb1_s04_statement_definitions_exact() -> None:
    from haloflow.m05.statements import M05_STATEMENTS

    actual = {
        key: (str(mode), capability, query)
        for key, (mode, capability, query) in M05_STATEMENTS.items()
    }
    assert actual == m05.STATEMENTS


def test_mb1_s06_part_a_checksum_pins() -> None:
    from haloflow.composition import build_production_tenant_migrations

    checksums = {unit.migration_id: unit.checksum for unit in build_production_tenant_migrations()}
    for migration_id, expected in m05.PART_A_CHECKSUMS.items():
        assert checksums[migration_id] == expected, migration_id


def test_mb1_s08_ci_gate_covers_m05() -> None:
    """The CI gate lints, type-checks and tests M05. The red commit adds tests/m05
    (ruff, pytest); src/haloflow/m05 joins ruff and mypy — and the M01 repository
    control's checked set — in the implementation commit, when the path exists."""

    from pathlib import Path

    workflow = Path("../.github/workflows/m01.yml").read_text().splitlines()
    ruff_line = next(line for line in workflow if "bin/ruff check" in line)
    mypy_line = next(line for line in workflow if "bin/mypy " in line)
    pytest_line = next(line for line in workflow if "bin/pytest tests" in line)
    for path in ("src/haloflow/m05", "tests/m05"):
        assert path in ruff_line.split(), path
    assert "src/haloflow/m05" in mypy_line.split()
    assert "tests/m05" in pytest_line.split()
