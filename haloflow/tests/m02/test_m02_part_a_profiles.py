"""UC1 part A — installed-state profile coverage for the typed gateway units (C1).

Codex ALIGN 20261031T131639Z: one function per typed unit (t008-t011), each with an
installed-state profile, and `composition.require_m02_installed_state_profiles`
covering EVERY typed M02 unit, t003 included, with missing / extra / mismatch
negative controls. Static: no database. The expected values come from the frozen
part-A manifest (part_a_support.FUNCTIONS), never from the profiles under test.
"""

from types import ModuleType
from typing import Any

import pytest

PROFILE_INVALID = "INSTALLED_STATE_PROFILE_INVALID"

PART_A_TYPED = {
    "t008_m02_canon": "m02_canon_v1",
    "t009_m02_append": "m02_append_observation",
    "t010_m02_enqueue": "m02_enqueue_event_handoffs",
    "t011_m02_envelope_read": "m02_get_observation_envelope",
}


def _build(profiles: dict[str, Any]) -> Any:
    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_TENANT_MIGRATIONS,
        require_m02_installed_state_profiles,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    registry = build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles=profiles,
    )
    require_m02_installed_state_profiles(registry)
    return registry


def _refused(build: Any) -> None:
    from haloflow.m01.errors import MigrationUnitRejected

    with pytest.raises(MigrationUnitRejected) as caught:
        build()
    assert caught.value.reason_code == PROFILE_INVALID


def _profiles() -> dict[str, Any]:
    from haloflow.composition import APPROVED_INSTALLED_STATE_PROFILES

    return dict(APPROVED_INSTALLED_STATE_PROFILES)


def test_pr1_every_m02_typed_unit_is_exactly_the_profiled_set() -> None:
    from haloflow.composition import build_production_tenant_migrations

    registry = build_production_tenant_migrations()
    typed = {u.migration_id for u in registry if u.is_typed}
    profiled = {
        u.migration_id
        for u in registry
        if registry.installed_state_requirement(u.migration_id).required
    }
    assert typed == profiled == {"t003_m02_lock_operation", *PART_A_TYPED}


@pytest.mark.parametrize("migration_id", sorted(PART_A_TYPED))
def test_pr2_missing_profile_refused(migration_id: str) -> None:
    profiles = _profiles()
    del profiles[migration_id]
    _refused(lambda: _build(profiles))


def test_pr2_control_full_profile_set_composes() -> None:
    _build(_profiles())


@pytest.mark.parametrize("target", ["t004_m02_reference", "t099_m02_unregistered"])
def test_pr3_extra_profile_refused(target: str) -> None:
    from haloflow.m02.part_a_gateways import T008_PROFILE

    profiles = {**_profiles(), target: T008_PROFILE}
    _refused(lambda: _build(profiles))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("t008_m02_canon", "t009_m02_append"),
        ("t010_m02_enqueue", "t011_m02_envelope_read"),
        ("t003_m02_lock_operation", "t009_m02_append"),
    ],
)
def test_pr4_mismatched_profile_refused(first: str, second: str) -> None:
    profiles = _profiles()
    profiles[first], profiles[second] = profiles[second], profiles[first]
    _refused(lambda: _build(profiles))


@pytest.mark.parametrize("migration_id", sorted(PART_A_TYPED))
def test_pr5_profile_matches_the_frozen_function_manifest(
    part_a: ModuleType, migration_id: str
) -> None:
    profile = _profiles()[migration_id]
    spec = next(f for f in part_a.FUNCTIONS if f.name == PART_A_TYPED[migration_id])
    assert profile.function_name == spec.name
    assert profile.argument_types == tuple(a.strip() for a in spec.args.split(","))
    assert profile.owner == profile.execution_role == spec.owner
    assert profile.security_definer is spec.security_definer is True
    assert profile.returns_set is False and profile.prokind == "f"
    grantees = {grantee for grantee, _, _, _ in profile.installed_acl}
    assert grantees == {spec.owner} | set(spec.executors)
    for grantee, privilege, grantor, grantable in profile.installed_acl:
        assert (privilege, grantor, grantable) == ("EXECUTE", spec.owner, False), grantee


# F-INT1 (Codex ALIGN 20261031T142215Z, item 3): the STATIC gate. A profile whose
# argument or return type disagrees with the unit's frozen declaration is refused at
# composition by `check_profile_agreement`, before any database work, so mutating a
# profile alone can never exercise the runtime verifier. The runtime verifier is
# reached separately, by an observed-catalogue seam, in
# test_m02_part_a_installed_state_postgres.py.
@pytest.mark.parametrize("migration_id", sorted(PART_A_TYPED))
@pytest.mark.parametrize("field", ["argument_types", "return_type"])
def test_pr6_type_disagreeing_with_the_declaration_refused_at_composition(
    migration_id: str, field: str
) -> None:
    import dataclasses

    profiles = _profiles()
    original = profiles[migration_id]
    if field == "argument_types":
        changed = tuple("int8" for _ in original.argument_types)
        assert changed != original.argument_types
        profiles[migration_id] = dataclasses.replace(original, argument_types=changed)
    else:
        assert original.return_type != "int8"
        profiles[migration_id] = dataclasses.replace(original, return_type="int8")
    _refused(lambda: _build(profiles))
