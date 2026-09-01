"""Sanitized M01 error taxonomy.

Detailed causes belong in PHI-safe structured telemetry. These exception
messages intentionally do not disclose whether a tenant exists.
"""


class M01Error(Exception):
    """Base class for failures at the tenant data-access boundary."""

    code = "M01_ERROR"
    public_message = "Tenant operation unavailable"

    def __init__(self, *, reason_code: str | None = None) -> None:
        super().__init__(self.public_message)
        self.reason_code = reason_code or self.code


class TenantDenied(M01Error):
    code = "TENANT_DENIED"
    public_message = "Tenant operation denied"


class TenantUnavailable(M01Error):
    code = "TENANT_INACTIVE"


class ContextInvalid(M01Error):
    code = "CONTEXT_INVALID"
    public_message = "Tenant authorization is invalid"


class ContextExpired(ContextInvalid):
    code = "CONTEXT_EXPIRED"
    public_message = "Tenant authorization has expired"


class NestedTenantTransaction(M01Error):
    code = "NESTED_TENANT_TRANSACTION"


class RegistryInconsistent(M01Error):
    code = "REGISTRY_INCONSISTENT"


class RoutingSetupFailed(M01Error):
    code = "ROUTING_SETUP_FAILED"


class RoutingMismatch(M01Error):
    code = "ROUTING_MISMATCH"


class RepositoryHandleExpired(M01Error):
    code = "REPOSITORY_HANDLE_EXPIRED"


class RepositoryStatementRejected(M01Error):
    code = "REPOSITORY_STATEMENT_REJECTED"


class CapabilityDenied(M01Error):
    code = "CAPABILITY_DENIED"


class MigrationUnitRejected(M01Error):
    """A per-tenant migration unit or registry failed trusted construction."""

    code = "MIGRATION_UNIT_REJECTED"


class TenantMigrationFailed(M01Error):
    """A per-tenant migration did not reach `applied`.

    `reason_code` is always a member of `provisioning.SanitizedErrorCode` or a
    request-shape code; it never carries a database message, SQL text, or a value.
    """

    code = "TENANT_MIGRATION_FAILED"


class ProvisioningFailed(M01Error):
    """Tenant provisioning stopped before activation. The tenant stays unusable."""

    code = "PROVISIONING_FAILED"
