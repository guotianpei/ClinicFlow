"""Tenant-schema provisioning and per-tenant migrations (M01-FR-017, FR-018).

Composition lives in `haloflow.m01.provisioning.units` and is deliberately NOT
re-exported here. This is the same rule the statement catalogue follows, for the
same reason: a convenience re-export on the package is a second path to the
builder, and the production registry can only be reviewable in one place if
there is exactly one. `haloflow.composition` is that place.
"""

from haloflow.m01.provisioning.codes import SanitizedErrorCode
from haloflow.m01.provisioning.drift import TenantDrift, report_drift, report_drift_on
from haloflow.m01.provisioning.provisioner import (
    ProvisioningOutcome,
    ProvisioningRequest,
    TenantProvisioner,
)
from haloflow.m01.provisioning.roles import (
    AUDIT_PROJECTOR_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    RUNTIME_ROLE,
)
from haloflow.m01.provisioning.runner import (
    ConnectionFactory,
    MigrationOutcome,
    TenantMigrationRunner,
    require_explicit_transactions,
    tenant_lock_key,
)
from haloflow.m01.provisioning.units import TenantMigrationRegistry, TenantMigrationUnit

__all__ = [
    "AUDIT_PROJECTOR_ROLE",
    "MIGRATOR_ROLE",
    "PROVISIONER_ROLE",
    "RUNTIME_ROLE",
    "ConnectionFactory",
    "MigrationOutcome",
    "ProvisioningOutcome",
    "ProvisioningRequest",
    "SanitizedErrorCode",
    "TenantDrift",
    "TenantMigrationRegistry",
    "TenantMigrationRunner",
    "TenantMigrationUnit",
    "TenantProvisioner",
    "report_drift",
    "report_drift_on",
    "require_explicit_transactions",
    "tenant_lock_key",
]
