"""The caller helper for the M02 lock gateway (CP2-2b, C-7; R-B5, R-B6).

Architecture v3 §4, with Rachel's sanitization-boundary ruling (2026-09-28).

`lock_operation` locks one `operation_registry` row through the tenant's
`m02_lock_operation(uuid)` gateway, inside the CALLER's transaction. The steps run
in this order:

1. Transaction state, with no query: it must be `INTRANS` (inside a transaction
   and idle in it). Anything else is `LockOperationPreconditionError`. The helper
   never opens a transaction implicitly.
2. Isolation: `current_setting('transaction_isolation')` must be `read committed`,
   or `LockOperationPreconditionError`. A database error here is
   `LockOperationFailed`: an identical SQLSTATE/message pair from THIS query is
   never mapped as a refusal (the mapping is call-scoped).
3. The call, schema-qualified through `sql.Identifier`, the id as a parameter.
4. Only an error from step 3 is mapped, and only when BOTH the SQLSTATE and the
   primary message match one of the three refusals. Any other database error is
   `LockOperationFailed`.
5. The result must be exactly one row of one non-NULL uuid, or `LockOperationFailed`.

OWNERSHIP. The caller owns the transaction. The helper never commits, rolls back,
sets or releases a savepoint, or changes isolation. The lock is held until the
caller's commit or rollback. After any helper exception the caller must not rely
on the lock: after a refusal or a database-caused failure the transaction is
normally aborted and a usable connection is rolled back; a broken, closed or
`UNKNOWN` connection is discarded; after a malformed-result failure the
transaction may still be healthy, and the caller should still roll back. After a
precondition error, the helper changed nothing. The helper does not set
`app.tenant_id`; the caller's transaction must (R-B2: not authentication).

SANITIZATION BOUNDARY (owner ruling). No helper-originated exception or
diagnostic appears anywhere in the raised exception's `__cause__`/`__context__`
chain or rendered traceback, and `__cause__` is always None. Every helper
exception is therefore raised OUTSIDE the `except` block that observed the
database error, and carries only a fixed message. Caller-owned context (whatever
the caller was already handling) is attached by Python as `__context__`; the
helper neither removes nor inspects it. With no ambient exception, `__context__`
is None.
"""

from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.pq import TransactionStatus

from haloflow.m02.codes import LockRefusalCode

__all__ = [
    "LockOperationFailed",
    "LockOperationPreconditionError",
    "LockOperationRefused",
    "lock_operation",
]

_PRECONDITION_MESSAGE: Final = (
    "lock_operation requires an open, healthy READ COMMITTED transaction"
)
_FAILED_MESSAGE: Final = "lock operation failed"

# (SQLSTATE, primary message) -> code. Both must match (architecture v3 §4 step 4).
_REFUSALS: Final = {
    ("22004", "operation id required"): LockRefusalCode.LOCK_OPERATION_ID_REQUIRED,
    ("P0002", "operation not found"): LockRefusalCode.LOCK_OPERATION_NOT_FOUND,
    ("22023", "tenant context invalid"): LockRefusalCode.LOCK_TENANT_CONTEXT_INVALID,
}
# Each refusal's message is the gateway's own fixed SQL message for its code.
_REFUSAL_MESSAGES: Final = {code: message for (_, message), code in _REFUSALS.items()}

_ISOLATION_QUERY: Final = "SELECT pg_catalog.current_setting('transaction_isolation')"
_READ_COMMITTED: Final = "read committed"
_GATEWAY: Final = "m02_lock_operation"


class LockOperationPreconditionError(Exception):
    """A programming or precondition error of the caller. Not a tenant-facing code."""

    def __init__(self) -> None:
        super().__init__(_PRECONDITION_MESSAGE)


class LockOperationRefused(Exception):
    """A sanitized refusal by the gateway. `code` is one of the three `LockRefusalCode`s."""

    def __init__(self, code: LockRefusalCode) -> None:
        super().__init__(_REFUSAL_MESSAGES[code])
        self.code = code


class LockOperationFailed(Exception):
    """A sanitized generic failure. Carries nothing about its cause."""

    def __init__(self) -> None:
        super().__init__(_FAILED_MESSAGE)


def _primary_message(error: psycopg.Error) -> object:
    diag = getattr(error, "diag", None)
    return getattr(diag, "message_primary", None)


def _refusal_for(error: psycopg.Error) -> Exception:
    """The sanitized exception for a step-3 error. Built from two fixed keys only."""

    sqlstate = getattr(error, "sqlstate", None)
    message = _primary_message(error)
    for (expected_state, expected_message), code in _REFUSALS.items():
        if sqlstate == expected_state and message == expected_message:
            return LockOperationRefused(code)
    return LockOperationFailed()


_MALFORMED: Final = object()


def _single_value(rows: Any) -> object:
    """The one value of exactly one row of exactly one column, or a sentinel."""

    if type(rows) is not list or len(rows) != 1:
        return _MALFORMED
    row = rows[0]
    if not isinstance(row, tuple | list) or len(row) != 1:
        return _MALFORMED
    return row[0]


async def lock_operation(
    connection: AsyncConnection[Any],
    *,
    schema_key: str,
    operation_id: UUID | None,
) -> UUID:
    """Lock the operation's row for the caller's transaction and return its id."""

    # Step 1: no query at all.
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise LockOperationPreconditionError()

    # Step 2. Every helper exception is raised AFTER the `except` block has ended.
    failure: Exception | None = None
    isolation_rows: Any = None
    try:
        cursor = await connection.execute(_ISOLATION_QUERY)
        isolation_rows = await cursor.fetchall()
    except psycopg.Error:
        failure = LockOperationFailed()
    if failure is not None:
        raise failure
    isolation = _single_value(isolation_rows)
    if isolation is _MALFORMED or type(isolation) is not str:
        raise LockOperationFailed()
    if isolation != _READ_COMMITTED:
        raise LockOperationPreconditionError()

    # Steps 3 and 4: the only statement whose error is mapped.
    call = sql.SQL("SELECT {}(%s)").format(sql.Identifier(schema_key, _GATEWAY))
    rows: Any = None
    try:
        cursor = await connection.execute(call, (operation_id,))
        rows = await cursor.fetchall()
    except psycopg.Error as error:
        failure = _refusal_for(error)
    if failure is not None:
        raise failure

    # Step 5.
    value = _single_value(rows)
    if type(value) is not UUID:
        raise LockOperationFailed()
    return value
