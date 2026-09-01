"""The closed vocabulary of sanitized provisioning failure codes.

R-E9 and M01-FR-021/FR-022: a provisioning or migration failure carries a code
from these sets and nothing else. No database message, no SQL text, no value, no
tenant content ever reaches ``shared.schema_migrations.sanitized_error_code`` or
a raised exception's payload -- the column is 64 characters wide and a raw
PostgreSQL error would carry the offending row into the control plane.

There are **two** vocabularies, and the split is by whether a ledger row can
exist yet:

- ``SanitizedErrorCode`` -- terminal states written to
  ``shared.schema_migrations``. A tenant has a ledger row, and this is what that
  row records.
- ``PreconditionCode`` -- refusals raised *before* any ledger row can exist: a
  malformed request, a rejected migration unit, an unusable connection. These
  never reach the database, so they are not ledger codes; the review that found
  ``CONNECTION_NOT_IDLE`` sitting outside both vocabularies was right that
  "not a ledger code" had been standing in for "undocumented".

Both are enumerated rather than described, and a repository control asserts that
every ``reason_code`` raised in this package is a member of one of them.
"""

from enum import StrEnum


class SanitizedErrorCode(StrEnum):
    """Terminal failure codes written to the migration ledger."""

    SCHEMA_CREATE_FAILED = "SCHEMA_CREATE_FAILED"
    MIGRATION_DDL_FAILED = "MIGRATION_DDL_FAILED"
    MIGRATION_COMMIT_FAILED = "MIGRATION_COMMIT_FAILED"
    MIGRATION_CHECKSUM_DRIFT = "MIGRATION_CHECKSUM_DRIFT"
    LEDGER_WRITE_FAILED = "LEDGER_WRITE_FAILED"
    LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
    GRANT_APPLY_FAILED = "GRANT_APPLY_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REGISTRY_WRITE_FAILED = "REGISTRY_WRITE_FAILED"


class PreconditionCode(StrEnum):
    """Refusals raised before any ledger row exists, so never written to one.

    Request shape, trusted construction, and connection mode. Each is a fixed
    identifier carrying no request content: a rejected tenant id is reported as
    ``TENANT_ID_INVALID``, never as the id that was rejected.
    """

    # Connection mode (the runner and provisioner both refuse the same thing).
    CONNECTION_NOT_IDLE = "CONNECTION_NOT_IDLE"

    # Provisioning request shape.
    TENANT_ID_INVALID = "TENANT_ID_INVALID"
    SCHEMA_KEY_INVALID = "SCHEMA_KEY_INVALID"
    ACTOR_KIND_INVALID = "ACTOR_KIND_INVALID"
    ACTOR_ID_REQUIRED = "ACTOR_ID_REQUIRED"
    EXECUTION_ID_INVALID = "EXECUTION_ID_INVALID"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"

    # Resume-window refusals: the registry row exists but this run may not adopt it.
    TENANT_NOT_RESUMABLE = "TENANT_NOT_RESUMABLE"
    SCHEMA_KEY_CONFLICT = "SCHEMA_KEY_CONFLICT"

    # Trusted construction of migration units and registries.
    UNTRUSTED_MIGRATION_UNIT = "UNTRUSTED_MIGRATION_UNIT"
    UNTRUSTED_MIGRATION_REGISTRY = "UNTRUSTED_MIGRATION_REGISTRY"
    MIGRATION_ID_INVALID = "MIGRATION_ID_INVALID"
    MIGRATION_TEMPLATE_EMPTY = "MIGRATION_TEMPLATE_EMPTY"
    MIGRATION_TEMPLATE_UNSCOPED = "MIGRATION_TEMPLATE_UNSCOPED"
    MIGRATION_REGISTRY_EMPTY = "MIGRATION_REGISTRY_EMPTY"
    DUPLICATE_MIGRATION_ID = "DUPLICATE_MIGRATION_ID"
    TEST_MIGRATION_UNIT_REJECTED = "TEST_MIGRATION_UNIT_REJECTED"


# The ledger column is varchar(64). A member longer than that would fail at write
# time against a real tenant, so the width is asserted by a unit test rather than
# by a module-level `assert`, which `python -O` would strip.
LEDGER_ERROR_CODE_MAX_LENGTH = 64
