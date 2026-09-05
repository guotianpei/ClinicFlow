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
    SCHEMA_ACL_MISMATCH = "SCHEMA_ACL_MISMATCH"
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

    # Typed verification construction (R-P2.3): each prohibited input has its
    # own cause; malformed expected state is distinct from executable input.
    VERIFICATION_SQL_FORBIDDEN = "VERIFICATION_SQL_FORBIDDEN"
    VERIFICATION_CALL_FORBIDDEN = "VERIFICATION_CALL_FORBIDDEN"
    VERIFICATION_QUERY_FORBIDDEN = "VERIFICATION_QUERY_FORBIDDEN"
    VERIFICATION_KIND_UNKNOWN = "VERIFICATION_KIND_UNKNOWN"
    VERIFICATION_SPEC_INVALID = "VERIFICATION_SPEC_INVALID"
    VERIFICATION_IDENTIFIER_INVALID = "VERIFICATION_IDENTIFIER_INVALID"
    VERIFICATION_CONFIG_INVALID = "VERIFICATION_CONFIG_INVALID"
    VERIFICATION_ACL_INVALID = "VERIFICATION_ACL_INVALID"

    # Canonical checksum payload (R-P4.5). Ordering resolves presentation, not
    # contradiction: a collection that states the same identity twice is refused
    # rather than sorted, because sorting only hides which one the digest took.
    DUPLICATE_FUNCTION_IDENTITY = "DUPLICATE_FUNCTION_IDENTITY"
    DUPLICATE_ACL_ENTRY = "DUPLICATE_ACL_ENTRY"
    CONFLICTING_CONFIG_KEY = "CONFLICTING_CONFIG_KEY"

    # Canonicalization must not erase supplied data (Codex note-07). A member of
    # a recognized collection that is not the type the payload requires, and two
    # mapping keys that normalize to one, are both refused: a canonicalizer that
    # drops what it cannot order produces one digest for two specifications,
    # which is the collision the versioned payload exists to prevent.
    CHECKSUM_PAYLOAD_MALFORMED = "CHECKSUM_PAYLOAD_MALFORMED"
    DUPLICATE_PAYLOAD_KEY = "DUPLICATE_PAYLOAD_KEY"

    # A unit's declared execution role, refused at composition (R-P1.2, R-P1.3,
    # R-P1B.22). Three codes because there are three independent controls, and a
    # single code would make it impossible to tell an unapproved role from a
    # malformed one from an infrastructure role in an incident.
    # `EXECUTION_ROLE_UNAVAILABLE` is a different failure -- the stage-1
    # preflight against a live catalogue -- and is not these.
    EXECUTION_ROLE_INVALID = "EXECUTION_ROLE_INVALID"
    EXECUTION_ROLE_NOT_APPROVED = "EXECUTION_ROLE_NOT_APPROVED"
    EXECUTION_ROLE_IS_INFRASTRUCTURE = "EXECUTION_ROLE_IS_INFRASTRUCTURE"

    # The provisioning manifest, refused at load (R-P1B.12, R-P3.2, R-P3.4, A5,
    # A7). Distinct causes rather than one code, on the same principle as the
    # execution-role refusals: an operator reading a code should know whether the
    # file is structurally wrong, names something that does not exist, contradicts
    # itself, or declares an access widening that the design forbids outright.
    PROVISIONING_MANIFEST_MALFORMED = "PROVISIONING_MANIFEST_MALFORMED"
    MANIFEST_TOKEN_UNRECOGNIZED = "MANIFEST_TOKEN_UNRECOGNIZED"
    MANIFEST_ROLE_UNKNOWN = "MANIFEST_ROLE_UNKNOWN"
    MANIFEST_TABLE_UNKNOWN = "MANIFEST_TABLE_UNKNOWN"
    MANIFEST_PRIVILEGE_UNKNOWN = "MANIFEST_PRIVILEGE_UNKNOWN"
    MANIFEST_DUPLICATE_DECLARATION = "MANIFEST_DUPLICATE_DECLARATION"
    MANIFEST_OVERRIDE_INVALID = "MANIFEST_OVERRIDE_INVALID"
    # The tenant-schema ACL declaration (D22). A missing entry is an error rather
    # than an empty set: a silently absent entry is how the audit-projector gap
    # arose, and it is the reason stage 3 declares all five grantee classes.
    SCHEMA_PRIVILEGE_DECLARATION_INVALID = "SCHEMA_PRIVILEGE_DECLARATION_INVALID"
    SCHEMA_PRIVILEGE_DECLARATION_MISSING = "SCHEMA_PRIVILEGE_DECLARATION_MISSING"
    # The manifest declares fixed invariants, not options (Codex note-17). Stages
    # 1-3 compare the live catalogue against these declarations, so a manifest
    # able to describe unsafe state would make those controls agree with the
    # danger rather than catch it. The safe profile (R-P1B.4) and the membership
    # edge (R-P1B.3) are pinned by value, not merely type-checked.
    EXECUTION_ROLE_PROFILE_UNSAFE = "EXECUTION_ROLE_PROFILE_UNSAFE"
    ROLE_MEMBERSHIP_DECLARATION_UNSAFE = "ROLE_MEMBERSHIP_DECLARATION_UNSAFE"
    ROLE_MEMBERSHIP_DECLARATION_MISSING = "ROLE_MEMBERSHIP_DECLARATION_MISSING"

    # Stage 1, the role-safety preflight (R-P1B.4). One code for every cause:
    # an operator learns that the role is unusable, and which of the six
    # attributes or three options differed belongs in telemetry rather than in
    # an exception payload a tenant-facing error might carry (R-E9). Raised
    # before any schema grant and before any `running` row, so a configuration
    # fault is never recorded as a tenant migration failure.
    EXECUTION_ROLE_UNAVAILABLE = "EXECUTION_ROLE_UNAVAILABLE"


# The ledger column is varchar(64). A member longer than that would fail at write
# time against a real tenant, so the width is asserted by a unit test rather than
# by a module-level `assert`, which `python -O` would strip.
LEDGER_ERROR_CODE_MAX_LENGTH = 64
