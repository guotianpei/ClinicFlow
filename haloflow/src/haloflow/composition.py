"""The single production composition root for the tenant statement catalogue.

ADR-011 D-11.18 (B2). Statements are composed exactly once, at startup, from
approved module definition sets. This module is the only place in production
code permitted to call ``build_statement_catalog``; repository-control tests
enforce that, and the statement-catalogue manifest pins whatever this function
produces. Without a single composition path the manifest would pin a constant
rather than the catalogue the application actually runs.
"""

from collections.abc import Mapping
from types import MappingProxyType

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning import TenantMigrationRegistry
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.units import (
    TENANT_MIGRATIONS,
    UnitDefinitions,
    build_tenant_migration_registry,
)
from haloflow.m01.statements import (
    M01_STATEMENTS,
    CompiledCatalog,
    StatementDefinitions,
    build_statement_catalog,
)
from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
from haloflow.m02.roles import LOCK_OWNER_ROLE
from haloflow.m02.units import M02_TENANT_MIGRATIONS, T003_MIGRATION_ID

# Approved module definition sets, in composition order. A new module is added
# here and nowhere else, and doing so forces a manifest update in the same
# commit because the manifest test composes through this tuple.
APPROVED_MODULE_STATEMENTS: tuple[StatementDefinitions, ...] = (M01_STATEMENTS,)

# Approved per-tenant migration definition sets. Same rule, same reason: one
# composition path, so what a tenant schema receives is reviewable in one place.
# `allow_test_units` is never passed here -- a test-only unit cannot reach
# production through this function (R-E12).
#
# CP2-2a approved the M02 unit *set* (`t002_m02_operation_registry`, an ordinary
# migrator-owned unit). CP2-2b adds the typed gateway unit `t003_m02_lock_operation`,
# which makes the production target version 3. Version 3 says nothing about
# runtime acceptance -- no runtime serves a version-3 tenant (L-1) -- nor about
# upgrading active version-2 tenants, which have no path in 2b (L-6).
APPROVED_TENANT_MIGRATIONS: tuple[UnitDefinitions, ...] = (
    TENANT_MIGRATIONS,
    M02_TENANT_MIGRATIONS,
)

# Execution roles this deployment approves for per-tenant migrations (R-P1.2).
# CP2-2b (R-B0, C-1): exactly the M02 lock owner, which `t003` runs as. `t001` and
# `t002` run as `haloflow_migrator` by absence. A module role is added here and
# nowhere else -- M01 embeds no module role name, so this declaration and the
# manifest are the whole reviewable surface. The literal sits inside this one
# declaration because that is the only place the repository control exempts.
#
# An infrastructure role cannot be approved by adding it here: the unit refuses
# every member of `PROVISIONING_ROLES` on its own (R-P1B.22(a), D23).
APPROVED_EXECUTION_ROLES: frozenset[str] = frozenset({"haloflow_m02_lock_owner"})

# CP2-2b (architecture v3 section 5.1(1)). Installed-state profiles, keyed by
# APPROVED MIGRATION ID, never by function name. M01 binds each to its unit at
# composition and refuses any disagreement with the unit's declaration.
APPROVED_INSTALLED_STATE_PROFILES: Mapping[str, InstalledStateProfile] = MappingProxyType(
    {T003_MIGRATION_ID: LOCK_OPERATION_PROFILE}
)


def build_production_catalog() -> CompiledCatalog:
    """Compose the production statement catalogue. Startup-only."""

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS)


def require_m02_installed_state_profiles(registry: TenantMigrationRegistry) -> None:
    """The M02 rule (architecture v3 section 5.1(1)).

    The set of typed units whose execution role is the M02 lock owner must EQUAL
    the set of units the registry holds a required installed-state profile for.
    So the gateway cannot compose without its profile, and no M02 lock-owner typed
    unit can escape one. Static: no database access.
    """

    lock_owner_typed = {
        unit.migration_id
        for unit in registry
        if unit.is_typed and unit.execution_role == LOCK_OWNER_ROLE
    }
    profiled = {
        unit.migration_id
        for unit in registry
        if registry.installed_state_requirement(unit.migration_id).required
    }
    if lock_owner_typed != profiled:
        raise MigrationUnitRejected(
            reason_code=PreconditionCode.INSTALLED_STATE_PROFILE_INVALID.value
        )


def build_production_tenant_migrations() -> TenantMigrationRegistry:
    """Compose the production per-tenant migration registry. Startup-only."""

    registry = build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
    )
    require_m02_installed_state_profiles(registry)
    return registry
