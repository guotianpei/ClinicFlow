"""CP2-2b pure rows: 2B-U01 to U05, U07, U40 to U42, D17a.

No database. Test cases v3 sections 3.1 and 3.4, architecture v3 sections 2, 3
and 5. Status before implementation: DB (the 2b names do not exist). After: pass.

Interface bound (packet README): I-B1 `haloflow.m02.units` (`T003_MIGRATION_ID`,
`T003_DEFINITION`, `T003_SQL`), I-B2 `haloflow.m02.gateway_profile`
(`LOCK_OPERATION_PROFILE`), I-B3 `installed_state.InstalledStateProfile`, I-B4 the
registry keyword `installed_state_profiles`, I-B6 `INSTALLED_STATE_PROFILE_INVALID`,
I-B7 `composition.require_m02_installed_state_profiles`.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

LOCK_OWNER = "haloflow_m02_lock_owner"
RUNTIME = "haloflow_runtime"
T003 = "t003_m02_lock_operation"
PROFILE_INVALID = "INSTALLED_STATE_PROFILE_INVALID"
SCHEMA = "tenant_aaaaaaaa"
REVISION_001 = Path("alembic/versions/001_m01_foundation.py")


class DependencyAbsent(Exception):
    """A 2b unit or name the row needs is not present yet: status DB, never MB."""


def thaw(value: Any) -> Any:
    """A fresh, ordinary, mutable copy of a frozen declaration block."""

    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [thaw(item) for item in value]
    return value


def gateway_registry(definition: Any = None, *, profiles: Any = None, roles: Any = None) -> Any:
    from haloflow.m01.provisioning.units import build_tenant_migration_registry
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
    from haloflow.m02.units import T003_DEFINITION, T003_MIGRATION_ID

    return build_tenant_migration_registry(
        {T003_MIGRATION_ID: definition if definition is not None else T003_DEFINITION},
        approved_execution_roles=roles if roles is not None else frozenset({LOCK_OWNER}),
        installed_state_profiles=(
            profiles if profiles is not None else {T003_MIGRATION_ID: LOCK_OPERATION_PROFILE}
        ),
    )


def altered(block: str, path: tuple[Any, ...], value: Any) -> Any:
    """`T003_DEFINITION` with ONE declared field changed (`block` is policy or verification)."""

    from haloflow.m02.units import T003_DEFINITION

    policy = thaw(T003_DEFINITION.policy)
    verification = thaw(T003_DEFINITION.policy_verification)
    target = policy if block == "policy" else verification
    node = target["functions"][0]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return dataclasses.replace(
        T003_DEFINITION, policy=policy, policy_verification=verification
    )


def refused_with(code: str, build: Any) -> None:
    from haloflow.m01.errors import MigrationUnitRejected

    with pytest.raises(MigrationUnitRejected) as caught:
        build()
    assert caught.value.reason_code == code


# --- U01, U02 ---------------------------------------------------------------


def test_2b_u01_the_production_registry_holds_the_typed_gateway_unit() -> None:
    """R-B0, R-B3."""

    from haloflow.composition import build_production_tenant_migrations

    units = {u.migration_id: u for u in build_production_tenant_migrations()}
    if T003 not in units:
        raise DependencyAbsent(T003)
    unit = units[T003]
    assert (unit.execution_role, unit.is_typed, unit.verification) == (LOCK_OWNER, True, None)


def test_2b_u02_the_gateway_profile_is_exactly_the_architecture_profile() -> None:
    """R-B9.3: architecture v3 section 5.3, the profile column, field by field."""

    from haloflow.m01.provisioning.installed_state import InstalledStateProfile
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE

    assert type(LOCK_OPERATION_PROFILE) is InstalledStateProfile
    assert dataclasses.asdict(LOCK_OPERATION_PROFILE) == {
        "function_name": "m02_lock_operation",
        "argument_types": ("uuid",),
        "owner": LOCK_OWNER,
        "execution_role": LOCK_OWNER,
        "prokind": "f",
        "return_type": "uuid",
        "returns_set": False,
        "language": "plpgsql",
        "security_definer": True,
        "volatility": "v",
        "parallel": "u",
        "strict": False,
        "installed_acl": (
            (LOCK_OWNER, "EXECUTE", LOCK_OWNER, False),
            (RUNTIME, "EXECUTE", LOCK_OWNER, False),
        ),
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        LOCK_OPERATION_PROFILE.strict = True  # type: ignore[misc]


# --- U03: one sub-row per agree cell (architecture v3 section 5.3) -----------

AGREE_CELLS: tuple[tuple[str, str, tuple[Any, ...], Any], ...] = (
    ("namespace", "policy", ("schema",), "tenant_other"),
    ("name-policy", "policy", ("name",), "m02_other"),
    ("name-verification", "verification", ("name",), "m02_other"),
    ("prokind", "policy", ("is_procedure",), True),
    ("args-policy", "policy", ("inputs", 0, "type"), "text"),
    ("args-verification", "verification", ("argument_types",), ["text"]),
    ("return-type", "policy", ("return_type",), "text"),
    ("return-outputs", "policy", ("outputs",), [{"name": "operation_id", "type": "uuid"}]),
    ("language", "policy", ("language",), "sql"),
    ("owner", "verification", ("owner",), "haloflow_m02_other"),
    ("secdef-policy", "policy", ("security_definer",), False),
    ("secdef-verification", "verification", ("security_definer",), False),
    ("volatility", "policy", ("volatility",), "stable"),
    ("parallel", "policy", ("parallel",), "safe"),
    ("strict", "policy", ("strict",), True),
    ("config-policy", "policy", ("config",), ["search_path=pg_catalog, {schema}"]),
    ("config-verification", "verification", ("config",), ["search_path=pg_catalog, {schema}"]),
    ("body-policy", "policy", ("body_sha256",), "0" * 64),
    ("body-verification", "verification", ("body",), "BEGIN RETURN NULL; END;"),
    ("acl-policy", "policy", ("acl", 0, "grantee"), "haloflow_audit_projector"),
    ("acl-verification", "verification", ("acl", 0, "grantee"), "haloflow_audit_projector"),
)


def test_2b_u03_control_the_unaltered_declaration_composes() -> None:
    assert [unit.migration_id for unit in gateway_registry()] == [T003]


@pytest.mark.parametrize(
    ("block", "path", "value"),
    [cell[1:] for cell in AGREE_CELLS],
    ids=[cell[0] for cell in AGREE_CELLS],
)
def test_2b_u03_a_declaration_that_disagrees_with_the_profile_is_refused(
    block: str, path: tuple[Any, ...], value: Any
) -> None:
    refused_with(PROFILE_INVALID, lambda: gateway_registry(altered(block, path, value)))


# --- U04, U05 ---------------------------------------------------------------


def test_2b_u04a_a_profile_keyed_to_a_missing_unit_is_refused() -> None:
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE

    refused_with(PROFILE_INVALID, lambda: gateway_registry(profiles={
        T003: LOCK_OPERATION_PROFILE, "t009_m02_absent": LOCK_OPERATION_PROFILE,
    }))


def test_2b_u04b_a_profile_keyed_to_an_ordinary_unit_is_refused() -> None:
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
    from haloflow.m02.units import M02_TENANT_MIGRATIONS

    refused_with(PROFILE_INVALID, lambda: build_tenant_migration_registry(
        TENANT_MIGRATIONS, M02_TENANT_MIGRATIONS,
        approved_execution_roles=frozenset({LOCK_OWNER}),
        installed_state_profiles={
            T003: LOCK_OPERATION_PROFILE,
            "t002_m02_operation_registry": LOCK_OPERATION_PROFILE,
        },
    ))


def test_2b_u04c_a_profile_on_a_unit_with_another_role_is_refused() -> None:
    from haloflow.m02.units import T003_DEFINITION

    other = "haloflow_m02_other"
    definition = dataclasses.replace(T003_DEFINITION, execution_role=other)
    refused_with(PROFILE_INVALID, lambda: gateway_registry(
        definition, roles=frozenset({other})
    ))


def test_2b_u04d_a_profile_on_a_unit_with_another_identity_is_refused() -> None:
    definition = altered("policy", ("name",), "m02_other")
    definition = dataclasses.replace(
        definition,
        policy_verification=altered("verification", ("name",), "m02_other").policy_verification,
    )
    refused_with(PROFILE_INVALID, lambda: gateway_registry(definition))


def test_2b_u05_a_lock_owner_typed_unit_without_a_profile_fails_the_m02_rule() -> None:
    from haloflow.composition import require_m02_installed_state_profiles

    registry = gateway_registry(profiles={})
    refused_with(PROFILE_INVALID, lambda: require_m02_installed_state_profiles(registry))


def test_2b_u05_control_the_production_registry_satisfies_the_m02_rule() -> None:
    from haloflow.composition import (
        build_production_tenant_migrations,
        require_m02_installed_state_profiles,
    )

    require_m02_installed_state_profiles(build_production_tenant_migrations())


# --- U07 ---------------------------------------------------------------------


def test_2b_u07_the_profile_is_not_part_of_checksum_or_declaration_payload() -> None:
    """R-B9.6. Compared on registries built directly (the M02 rule is composition's)."""

    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE

    twin = dataclasses.replace(LOCK_OPERATION_PROFILE)
    units = [
        gateway_registry().units[0],
        gateway_registry(profiles={T003: twin}).units[0],
        gateway_registry(profiles={}).units[0],
    ]
    assert len({unit.checksum for unit in units}) == 1
    assert units[0].declaration_payload == units[1].declaration_payload
    assert units[0].declaration_payload == units[2].declaration_payload


# --- U40 to U42 ---------------------------------------------------------------


def test_2b_u40_the_frozen_checker_accepts_the_gateway_declaration() -> None:
    """R-B3, AQ-6 gate. If this fails, the body changes; the policy never does."""

    from haloflow.m01.provisioning import typed_plan

    unit = gateway_registry().units[0]
    result = typed_plan.validate_declaration(unit=unit, schema_key=SCHEMA)
    assert f"{SCHEMA}.m02_lock_operation".encode() in result.sql_bytes


_PATTERN_LITERAL = re.compile(r"'(\^\[a-z0-9\][^']*\$)'")


def test_2b_u41_the_tenant_pattern_is_the_same_literal_in_three_places() -> None:
    """R-B2: gateway body, resolver, and the `shared.tenants` CHECK in revision 001."""

    from haloflow.m01.resolver import TENANT_ID_PATTERN
    from haloflow.m02.units import T003_SQL

    in_body = _PATTERN_LITERAL.findall(T003_SQL)
    in_revision = _PATTERN_LITERAL.findall(REVISION_001.read_text(encoding="utf-8"))
    assert len(in_body) == 1 and len(in_revision) == 1
    assert in_body[0] == TENANT_ID_PATTERN.pattern == in_revision[0]


def test_2b_u42_source_order_null_check_then_context_then_table() -> None:
    """R-B1, R-B2. A SOURCE assertion over the template. It closes no ordering row (D30)."""

    from haloflow.m02.units import T003_SQL

    null_check = T003_SQL.index("p_operation_id IS NULL")
    context = T003_SQL.index("'app.tenant_id'")
    table = T003_SQL.index("{schema}.operation_registry")
    assert null_check < context < table


# --- D17a (a composition row) -------------------------------------------------


def test_2b_d17a_the_role_absent_from_the_approved_set_is_refused_at_composition() -> None:
    refused_with(
        "EXECUTION_ROLE_NOT_APPROVED", lambda: gateway_registry(roles=frozenset())
    )
