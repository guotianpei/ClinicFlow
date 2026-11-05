"""L-1W W3a: composition of the seven W3a tenant units (contract v1 §1, §5; Codex
215519Z: target 32). Unit tier, no database.

W3a does NOT close v1: m01_staff_admin and m05_prepare/apply_action stay
runtime-callable until W3b (DEFERRED rows C-01, C-02 and the closure units).

Every expected value is written out here, never derived from the code under test.
"""

from __future__ import annotations

from typing import Any, Final

import m05_support as m05
import staff_support as st

TYPED_KIND: Final = "typed_function_v3"
SUPPORT: Final = "haloflow_support"

W3A_UNIT_IDS: Final = (
    "t026_m01_staff_roles",
    "t027_m01_staff_self_standing",
    "t028_m01_staff_member_standing",
    "t029_m01_staff_admin_v2",
    "t030_m05_prepare_action_v2",
    "t031_m05_apply_action_v2",
    "t032_m01_staff_bootstrap_admin",
)
# migration id -> (kind, execution role)
W3A_SHAPES: Final = {
    "t026_m01_staff_roles": (None, None),
    "t027_m01_staff_self_standing": (TYPED_KIND, st.STAFF_OWNER),
    "t028_m01_staff_member_standing": (TYPED_KIND, st.STAFF_OWNER),
    "t029_m01_staff_admin_v2": (TYPED_KIND, st.STAFF_OWNER),
    "t030_m05_prepare_action_v2": (TYPED_KIND, m05.M05_OWNER),
    "t031_m05_apply_action_v2": (TYPED_KIND, m05.M05_OWNER),
    "t032_m01_staff_bootstrap_admin": (TYPED_KIND, st.STAFF_OWNER),
}
# typed unit -> (function, argument type, return type, EXECUTE grantee besides owner)
W3A_FUNCTIONS: Final = {
    "t027_m01_staff_self_standing": ("m01_staff_self_standing", "jsonb", "jsonb", st.RUNTIME),
    "t028_m01_staff_member_standing": ("m01_staff_member_standing", "uuid", "jsonb",
                                       m05.M05_OWNER),
    "t029_m01_staff_admin_v2": ("m01_staff_admin_v2", "jsonb", "jsonb", st.RUNTIME),
    "t030_m05_prepare_action_v2": ("m05_prepare_action_v2", "jsonb", "jsonb", st.RUNTIME),
    "t031_m05_apply_action_v2": ("m05_apply_action_v2", "jsonb", "jsonb", st.RUNTIME),
    "t032_m01_staff_bootstrap_admin": ("m01_staff_bootstrap_admin", "jsonb", "jsonb", SUPPORT),
}
W3A_TARGET: Final = 32
# L-1W W3b: the production registry continues through t036 (target 36); the W3a rows
# below pin the production composition, so they follow it (node names kept).
W3B_UNIT_IDS: Final = (
    "t033_m01_staff_bootstrap_first_admin_schema",
    "t034_m01_staff_bootstrap_first_admin",
    "t035_m05_v1_runtime_closure",
    "t036_m01_staff_v1_runtime_closure",
)
PRODUCTION_TARGET: Final = 36
PRODUCTION_UNIT_IDS: Final = (
    m05.PART_A_UNIT_IDS + m05.M05_UNIT_IDS + st.STAFF_UNIT_IDS + W3A_UNIT_IDS + W3B_UNIT_IDS
)

# Statements W3a changes or adds: key -> (mode, capability, query).
W3A_STATEMENTS: Final = {
    "m01.staff.admin": ("write", "m01.staff.admin", "SELECT m01_staff_admin_v2(%(command)s)"),
    # TEST REPAIR (disclosed, W4b): WRITE, because t027 locks FOR SHARE (25006 in a
    # read-only transaction; CI 42037386795).
    "m01.staff.self_standing": ("write", "m01.staff.self_standing",
                                "SELECT m01_staff_self_standing(%(attribution)s)"),
    "m05.followup.prepare": ("write", "m05.followup.prepare",
                             "SELECT m05_prepare_action_v2(%(command)s)"),
    "m05.followup.apply": ("write", "m05.followup.apply",
                           "SELECT m05_apply_action_v2(%(command)s)"),
}


def _registry() -> Any:
    from haloflow.composition import build_production_tenant_migrations

    return build_production_tenant_migrations()


def test_w3a_u01_production_unit_ids_and_target() -> None:
    registry = _registry()
    assert registry.migration_ids == PRODUCTION_UNIT_IDS
    assert registry.target_version == PRODUCTION_TARGET


def test_w3a_u02_unit_kinds_and_execution_roles() -> None:
    by_id = {unit.migration_id: unit for unit in _registry()}
    for migration_id, shape in W3A_SHAPES.items():
        assert (by_id[migration_id].kind, by_id[migration_id].execution_role) == shape, (
            migration_id)


def test_w3a_u03_profiles_exist_for_every_typed_w3a_unit_with_exact_acl() -> None:
    from haloflow.composition import APPROVED_INSTALLED_STATE_PROFILES

    for migration_id, (name, arg, ret, grantee) in W3A_FUNCTIONS.items():
        profile = APPROVED_INSTALLED_STATE_PROFILES[migration_id]
        owner = W3A_SHAPES[migration_id][1]
        assert (profile.function_name, profile.argument_types, profile.return_type,
                profile.owner, profile.execution_role, profile.security_definer) == (
            name, (arg,), ret, owner, owner, True), migration_id
        assert set(profile.installed_acl) == {
            (owner, "EXECUTE", owner, False), (grantee, "EXECUTE", owner, False)}, migration_id


def test_w3a_u04_supported_version_literal_is_32() -> None:
    from haloflow.composition import APPROVED_SUPPORTED_SCHEMA_VERSIONS

    # Superseded by L-1W W3b: the literal is now {36} (the node name keeps "32").
    assert frozenset({PRODUCTION_TARGET}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS


def test_w3a_u05_repointed_and_new_statements() -> None:
    from haloflow.composition import build_production_catalog

    catalog = build_production_catalog().catalog
    for key, (mode, capability, query) in W3A_STATEMENTS.items():
        statement = catalog.resolve(key)
        assert (str(statement.mode), statement.required_capability,
                " ".join(statement.query.split())) == (mode, capability, query), key


def test_w3a_u06_v1_functions_stay_installed_in_w3a() -> None:
    """W3a keeps the v1 units (closure is W3b); their ids are still composed."""

    ids = _registry().migration_ids
    for migration_id in ("t015_m05_prepare_action", "t016_m05_apply_action",
                         "t023_m01_staff_admin"):
        assert migration_id in ids
