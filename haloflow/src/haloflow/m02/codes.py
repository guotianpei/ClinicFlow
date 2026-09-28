"""The M02 lock gateway's refusal vocabulary (CP2-2b, R-B5).

Exactly three codes, each the value of its own name. There is no fourth code: a
failure that is not one of these refusals is a generic `LockOperationFailed`, and
a caller precondition is `LockOperationPreconditionError` (architecture v3 §4).
"""

from enum import StrEnum


class LockRefusalCode(StrEnum):
    """A sanitized refusal of the lock gateway. Carries no request content."""

    LOCK_OPERATION_ID_REQUIRED = "LOCK_OPERATION_ID_REQUIRED"
    LOCK_OPERATION_NOT_FOUND = "LOCK_OPERATION_NOT_FOUND"
    LOCK_TENANT_CONTEXT_INVALID = "LOCK_TENANT_CONTEXT_INVALID"
