"""The single production composition root for the tenant statement catalogue.

ADR-011 D-11.18 (B2). Statements are composed exactly once, at startup, from
approved module definition sets. This module is the only place in production
code permitted to call ``build_statement_catalog``; repository-control tests
enforce that, and the statement-catalogue manifest pins whatever this function
produces. Without a single composition path the manifest would pin a constant
rather than the catalogue the application actually runs.
"""

from haloflow.m01.provisioning import TenantMigrationRegistry
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

# Approved module definition sets, in composition order. A new module is added
# here and nowhere else, and doing so forces a manifest update in the same
# commit because the manifest test composes through this tuple.
APPROVED_MODULE_STATEMENTS: tuple[StatementDefinitions, ...] = (M01_STATEMENTS,)

# Approved per-tenant migration definition sets. Same rule, same reason: one
# composition path, so what a tenant schema receives is reviewable in one place.
# `allow_test_units` is never passed here -- a test-only unit cannot reach
# production through this function (R-E12).
APPROVED_TENANT_MIGRATIONS: tuple[UnitDefinitions, ...] = (TENANT_MIGRATIONS,)

# Execution roles this deployment approves for per-tenant migrations (R-P1.2).
# Empty today: no module declares one, and `t001` runs as `haloflow_migrator` by
# absence. A module role is added here and nowhere else -- M01 embeds no module
# role name, so this tuple and the manifest are the whole reviewable surface.
#
# An infrastructure role cannot be approved by adding it here: the unit refuses
# every member of `PROVISIONING_ROLES` on its own (R-P1B.22(a), D23).
APPROVED_EXECUTION_ROLES: frozenset[str] = frozenset()


def build_production_catalog() -> CompiledCatalog:
    """Compose the production statement catalogue. Startup-only."""

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS)


def build_production_tenant_migrations() -> TenantMigrationRegistry:
    """Compose the production per-tenant migration registry. Startup-only."""

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
    )
