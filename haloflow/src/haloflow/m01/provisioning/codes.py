"""The closed vocabulary of sanitized provisioning failure codes.

R-E9 and M01-FR-021/FR-022: a provisioning or migration failure records a code
from this set and nothing else. No database message, no SQL text, no value, no
tenant content ever reaches ``shared.schema_migrations.sanitized_error_code`` or
a raised exception's payload -- the column is 64 characters wide and a raw
PostgreSQL error would carry the offending row into the control plane.
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


# The ledger column is varchar(64). A member longer than that would fail at write
# time against a real tenant, so the width is asserted by a unit test rather than
# by a module-level `assert`, which `python -O` would strip.
LEDGER_ERROR_CODE_MAX_LENGTH = 64
