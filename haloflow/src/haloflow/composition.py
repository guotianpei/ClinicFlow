"""The single production composition root.

ADR-011 D-11.18 (B2). Statements are composed exactly once, at startup, from
approved module definition sets. This module is the only place in production
code permitted to call ``build_statement_catalog``; repository-control tests
enforce that, and the statement-catalogue manifest pins whatever this function
produces. Without a single composition path the manifest would pin a constant
rather than the catalogue the application actually runs.

L-1: this module also declares the supported schema-version set and is the only
production caller of `compose_tenant_runtime`, through
`build_production_tenant_runtime`. No application entry point calls that yet
(L-1W).

UC1 part B tranche B5 (package v2 + v2r1, Codex 4b0a5448) composes M05 and the
staff directory into production: the catalogue holds the M02 observation append
(used by `haloflow.m02.observation`, L-1S), the three staff-directory statements
and the seven M05 follow-up statements; the registry holds t001-t025 (target 25).
This module stays declarative policy. The trusted startup procedure that consumes
it lives in `haloflow.startup`.
"""

from collections.abc import Mapping
from types import MappingProxyType

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning import TenantMigrationRegistry
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.installed_state import AclAmendment, InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import (
    ACL_CLOSURE_KIND,
    TENANT_MIGRATIONS,
    UnitDefinition,
    UnitDefinitions,
    build_tenant_migration_registry,
)
from haloflow.m01.runtime import (
    TenantRuntime,
    TenantRuntimeDependencies,
    compose_tenant_runtime,
)
from haloflow.m01.staff.l1w_units import L1W_STAFF_PROFILES, L1W_STAFF_TENANT_MIGRATIONS
from haloflow.m01.staff.l1w_w3b_units import (
    L1W_W3B_STAFF_PROFILES,
    L1W_W3B_STAFF_TENANT_MIGRATIONS,
)
from haloflow.m01.staff.units import (
    STAFF_GATEWAY_PROFILES,
    STAFF_OWNER_ROLE,
    STAFF_TENANT_MIGRATIONS,
)
from haloflow.m01.statements import (
    M01_STATEMENTS,
    CompiledCatalog,
    StatementDefinitions,
    build_statement_catalog,
)
from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
from haloflow.m02.part_a_gateways import PART_A_GATEWAY_PROFILES
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m02.statements import M02_STATEMENTS
from haloflow.m02.units import M02_TENANT_MIGRATIONS, T003_MIGRATION_ID
from haloflow.m05.gateways import M05_GATEWAY_PROFILES
from haloflow.m05.gateways_v2 import M05_V2_PROFILES, M05_V2_UNITS
from haloflow.m05.statements import M05_STATEMENTS
from haloflow.m05.units import M05_TENANT_MIGRATIONS

# Approved module definition sets, in composition order. A new module is added
# here and nowhere else, and doing so forces a manifest update in the same
# commit because the manifest test composes through this tuple.
#
# UC1 part A (Codex ALIGN 20261031T142215Z item 6): M02 contributes exactly the
# observation append statement. UC1 part B B5 (P-1(a)): M01 contributes the three
# staff-directory statements and M05 its seven follow-up statements.
APPROVED_MODULE_STATEMENTS: tuple[StatementDefinitions, ...] = (
    M01_STATEMENTS,
    M02_STATEMENTS,
    M05_STATEMENTS,
)

# L-1W W3a (contract v1, Codex 215519Z): t026-t032 in migration-id order. The staff
# units (t026-t029, t032) and the M05 v2 actions (t030, t031) interleave, so they
# compose as one id-ordered set. W3a does not close v1 (W3b).
L1W_W3A_TENANT_MIGRATIONS: UnitDefinitions = MappingProxyType(
    dict(sorted({**L1W_STAFF_TENANT_MIGRATIONS, **M05_V2_UNITS}.items()))
)

# L-1W W3b (plan v1r1 + addendum 1, Codex 151751Z; owner decision OD-FM1): t033 (the
# first-member bootstrap storage), t034 (the first-member bootstrap), and the two
# remove-only `acl_closure_v1` units that close the v1 runtime paths: t035 run as the
# M05 owner (m05_prepare_action, m05_apply_action) and t036 run as the staff owner
# (m01_staff_admin). The functions stay installed; only haloflow_runtime's EXECUTE
# goes. A closure unit carries no template: what it executes is rendered from the
# amendments below, which are its whole declaration.
T035_MIGRATION_ID = "t035_m05_v1_runtime_closure"
T036_MIGRATION_ID = "t036_m01_staff_v1_runtime_closure"
L1W_W3B_TENANT_MIGRATIONS: UnitDefinitions = MappingProxyType(
    {
        **L1W_W3B_STAFF_TENANT_MIGRATIONS,
        T035_MIGRATION_ID: UnitDefinition(
            template="", kind=ACL_CLOSURE_KIND, execution_role=M05_OWNER_ROLE
        ),
        T036_MIGRATION_ID: UnitDefinition(
            template="", kind=ACL_CLOSURE_KIND, execution_role=STAFF_OWNER_ROLE
        ),
    }
)
APPROVED_ACL_AMENDMENTS: tuple[AclAmendment, ...] = (
    AclAmendment(
        target_unit="t015_m05_prepare_action",
        activated_by=T035_MIGRATION_ID,
        removes=((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),),
    ),
    AclAmendment(
        target_unit="t016_m05_apply_action",
        activated_by=T035_MIGRATION_ID,
        removes=((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),),
    ),
    AclAmendment(
        target_unit="t023_m01_staff_admin",
        activated_by=T036_MIGRATION_ID,
        removes=((RUNTIME_ROLE, "EXECUTE", STAFF_OWNER_ROLE, False),),
    ),
)

# Approved per-tenant migration definition sets. Same rule, same reason: one
# composition path, so what a tenant schema receives is reviewable in one place.
# `allow_test_units` is never passed here -- a test-only unit cannot reach
# production through this function (R-E12).
#
# CP2-2a approved the M02 unit *set* (`t002_m02_operation_registry`, an ordinary
# migrator-owned unit). CP2-2b adds the typed gateway unit `t003_m02_lock_operation`,
# which makes the production target version 3. Version 3 says nothing about
# upgrading active version-2 tenants, which have no path in 2b (L-6). UC1 part A
# adds t004-t011 (target version 11); no upgrade path from version 3 is claimed
# either. UC1 part B B5 adds the M05 units t012-t020 and the staff-directory units
# t021-t025 (target version 25); no upgrade path from version 11 is claimed (L-6).
# L-1W W3a adds t026-t032 (target version 32); no upgrade path from 25 is claimed.
# L-1W W3b adds t033-t036 (target version 36); no upgrade path from 32 is claimed.
# Runtime acceptance is `APPROVED_SUPPORTED_SCHEMA_VERSIONS` below (L-1).
APPROVED_TENANT_MIGRATIONS: tuple[UnitDefinitions, ...] = (
    TENANT_MIGRATIONS,
    M02_TENANT_MIGRATIONS,
    M05_TENANT_MIGRATIONS,
    STAFF_TENANT_MIGRATIONS,
    L1W_W3A_TENANT_MIGRATIONS,
    L1W_W3B_TENANT_MIGRATIONS,
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
#
# UC1 part A (Codex ALIGN 20261031T131639Z, C1; delta v1r1 Delta4): the append,
# projection and read owners run t008-t011. UC1 part B B5: the M05 owner runs the
# typed M05 gateway units and the staff owner the typed staff-directory units.
APPROVED_EXECUTION_ROLES: frozenset[str] = frozenset(
    {
        "haloflow_m02_lock_owner",
        "haloflow_m02_append_owner",
        "haloflow_m02_projection_owner",
        "haloflow_m02_read_owner",
        "haloflow_m05_owner",
        "haloflow_staff_owner",
    }
)

# CP2-2b (architecture v3 section 5.1(1)). Installed-state profiles, keyed by
# APPROVED MIGRATION ID, never by function name. M01 binds each to its unit at
# composition and refuses any disagreement with the unit's declaration.
#
# UC1 part A (C1): every typed M02 unit carries a profile, t008-t011 included.
# UC1 part B B5: so does every typed M05 and staff-directory unit.
APPROVED_INSTALLED_STATE_PROFILES: Mapping[str, InstalledStateProfile] = MappingProxyType(
    {
        T003_MIGRATION_ID: LOCK_OPERATION_PROFILE,
        **PART_A_GATEWAY_PROFILES,
        **M05_GATEWAY_PROFILES,
        **STAFF_GATEWAY_PROFILES,
        **L1W_STAFF_PROFILES,
        **M05_V2_PROFILES,
        **L1W_W3B_STAFF_PROFILES,
    }
)


# L-1 (requirements v2, L1-D2 = M1). The schema versions the production runtime
# admits: exactly {36} since L-1W W3b composed t033-t036 (previously {32} from L-1W W3a,
# which composed t026-t032; before that {25} from UC1 part B
# B5, which composed t012-t025; before that {11} from
# UC1 part A, P-b, Codex ALIGN 20261031T130451Z; before that {3}). The one
# declaration -- resolver, gateway, provisioner, the bundle and startup all receive
# this object. A literal, never derived from the registry: a derived set would make
# the coordination check vacuous and silently admit a future target. Version 3 means
# "the M01 infrastructure baseline through t003" (R-E11).
APPROVED_SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({36})


def build_production_catalog() -> CompiledCatalog:
    """Compose the production statement catalogue. Startup-only."""

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS)


def require_installed_state_profiles(registry: TenantMigrationRegistry) -> None:
    """The generalized profile rule (B5 package v2 section 1).

    The set of typed units whose execution role is an approved execution role (the
    M02 lock, append, projection and read owners, the M05 owner and the staff owner)
    must EQUAL the set of units the registry holds a required installed-state
    profile for. So no gateway composes without its profile, and no typed unit run
    by an approved role can escape one. Static: no database access.
    """

    typed = {
        unit.migration_id
        for unit in registry
        if unit.is_typed and unit.execution_role in APPROVED_EXECUTION_ROLES
    }
    profiled = {
        unit.migration_id
        for unit in registry
        if registry.installed_state_requirement(unit.migration_id).required
    }
    if typed != profiled:
        raise MigrationUnitRejected(
            reason_code=PreconditionCode.INSTALLED_STATE_PROFILE_INVALID.value
        )


# The CP2-2b / UC1 part A name, kept as an alias because existing M02 rows
# (2B-U05, PR1-PR4) import it. Since B5 the M02-only rule would refuse the
# production registry (it profiles M05 and staff units too), so the alias is the
# generalized rule, which covers every M02 typed unit as before.
require_m02_installed_state_profiles = require_installed_state_profiles


def build_production_tenant_migrations() -> TenantMigrationRegistry:
    """Compose the production per-tenant migration registry. Startup-only."""

    registry = build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
        acl_amendments=APPROVED_ACL_AMENDMENTS,
    )
    require_installed_state_profiles(registry)
    return registry


def build_production_tenant_runtime(dependencies: TenantRuntimeDependencies) -> TenantRuntime:
    """Compose the production tenancy runtime. Startup-only.

    Takes no registry, catalogue or set: a caller cannot substitute them. The
    registry and catalogue are built first, so their own failures keep their own
    outcomes and the generic builder is never reached. The declaration is read
    here, at call time. The caller owns the pool's lifecycle (L-1W).
    """

    registry = build_production_tenant_migrations()
    catalog = build_production_catalog()
    return compose_tenant_runtime(
        dependencies,
        registry=registry,
        catalog=catalog,
        supported_schema_versions=APPROVED_SUPPORTED_SCHEMA_VERSIONS,
    )
