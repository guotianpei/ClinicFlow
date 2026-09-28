"""CP2-2b 2B-U43 to U48 (R-B5, R-B6): the caller helper `lock_operation`.

No database: a spy connection records every statement and scripts results and
errors. Test cases v3 section 3.4; architecture v3 section 4 as clarified by test
cases v3 section 3.4 (the exception is raised OUTSIDE the `except` block, so
`__context__` is None on the object).

Interface bound (packet README, I-B8/I-B9): `haloflow.m02.lock.lock_operation(
connection, *, schema_key, operation_id)`, which uses only
`connection.info.transaction_status`, `await connection.execute(query, params)` and
`await cursor.fetchall()`; maps on `error.sqlstate` and `error.diag.message_primary`;
raises `LockOperationPreconditionError`, `LockOperationRefused(code)` or
`LockOperationFailed` with the fixed messages below.

SANITIZATION BOUNDARY (packet v3 README, proposed clarification). HELPER-ORIGINATED
material is every exception raised by a statement the helper itself issued, and its
diagnostics. None of it may appear anywhere in the raised exception's
`__cause__`/`__context__` chain or rendered traceback, and `__cause__` is always None.
CALLER-OWNED context is whatever the caller was already handling when it called the
helper, including a psycopg error of the caller's own and anything chained to it.
Python attaches it as `__context__`; the helper neither removes nor inspects it, and
it is outside the promise. With no ambient exception, `__context__` is None.

Status before implementation: DB. After: pass.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import pytest
from psycopg.pq import TransactionStatus

SCHEMA = "tenant_aaaaaaaa"
OPERATION = uuid5(NAMESPACE_URL, "haloflow-test:m02:caller:operation")
PROBE = "PROBE-7f3a-raw-server-text"
PRECONDITION_MESSAGE = "lock_operation requires an open, healthy READ COMMITTED transaction"
FAILED_MESSAGE = "lock operation failed"
MAPPED = (
    ("22004", "operation id required", "LOCK_OPERATION_ID_REQUIRED"),
    ("P0002", "operation not found", "LOCK_OPERATION_NOT_FOUND"),
    ("22023", "tenant context invalid", "LOCK_TENANT_CONTEXT_INVALID"),
)
FORBIDDEN_STATEMENTS = (
    "commit", "rollback", "savepoint", "release", "set transaction",
    "set session characteristics", "begin", "start transaction",
)


def db_error(sqlstate: str, message: str) -> psycopg.Error:
    """A real psycopg error class for `sqlstate`, whose primary message is `message`.

    CONSTRUCTED: a Python exception of the class a server error would map to. It
    is not evidence of how PostgreSQL fails.
    """

    base = psycopg.errors.lookup(sqlstate)
    fake = type(
        f"Constructed{base.__name__}",
        (base,),
        {"diag": property(lambda self: SimpleNamespace(message_primary=message))},
    )
    error: psycopg.Error = fake(f"{message} {PROBE}")
    return error


@dataclass
class _Cursor:
    rows: list[tuple[Any, ...]]

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


@dataclass
class SpyConnection:
    """Scripted `(rows | error)` per statement, in order. Records every statement."""

    status: TransactionStatus = TransactionStatus.INTRANS
    script: list[Any] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    @property
    def info(self) -> Any:
        return SimpleNamespace(transaction_status=self.status)

    async def execute(self, query: Any, params: Any = None) -> _Cursor:
        text = query if isinstance(query, str) else query.as_string(None)
        self.statements.append(text)
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Cursor(list(outcome))


ISOLATION_OK = [("read committed",)]


def valid_script() -> list[Any]:
    return [ISOLATION_OK, [(OPERATION,)]]


async def call(connection: SpyConnection, operation_id: UUID | None = OPERATION) -> Any:
    from haloflow.m02.lock import lock_operation

    return await lock_operation(connection, schema_key=SCHEMA, operation_id=operation_id)


async def raised(connection: SpyConnection) -> BaseException:
    """The helper's OWN exception. Anything else (an import error before the helper
    exists, or an unexpected type) propagates, so it can never satisfy a row."""

    from haloflow.m02.lock import (
        LockOperationFailed,
        LockOperationPreconditionError,
        LockOperationRefused,
    )

    try:
        await call(connection)
    except (LockOperationFailed, LockOperationPreconditionError, LockOperationRefused) as error:
        return error
    raise AssertionError("lock_operation returned; an exception was expected")


def assert_sanitized(error: BaseException, message: str) -> None:
    assert str(error) == message and error.args == (message,)
    assert error.__cause__ is None and error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for leaked in (PROBE, "SELECT", SCHEMA, str(OPERATION)):
        assert leaked not in rendered, leaked


# --- U43, U44: call-scoped mapping and sanitization ---------------------------


@pytest.mark.parametrize(("sqlstate", "message", "code"), MAPPED)
async def test_2b_u43_each_mapped_pair_gives_its_refusal(
    sqlstate: str, message: str, code: str
) -> None:
    from haloflow.m02.lock import LockOperationRefused

    error = await raised(SpyConnection(script=[ISOLATION_OK, db_error(sqlstate, message)]))
    assert type(error) is LockOperationRefused
    assert error.code.value == code
    assert_sanitized(error, message)


@pytest.mark.parametrize(
    ("sqlstate", "message"),
    [("22004", "tenant context invalid"), ("22023", "operation id required"),
     ("P0001", "operation not found"), ("42501", "operation id required")],
)
async def test_2b_u43_a_half_matching_error_is_a_generic_failure(
    sqlstate: str, message: str
) -> None:
    from haloflow.m02.lock import LockOperationFailed

    error = await raised(SpyConnection(script=[ISOLATION_OK, db_error(sqlstate, message)]))
    assert type(error) is LockOperationFailed
    assert_sanitized(error, FAILED_MESSAGE)


@pytest.mark.parametrize(("sqlstate", "message", "code"), MAPPED)
async def test_2b_u43_an_isolation_query_error_never_maps_as_a_refusal(
    sqlstate: str, message: str, code: str
) -> None:
    """Scoping: an IDENTICAL pair from the isolation query is not the gateway's refusal."""

    from haloflow.m02.lock import LockOperationFailed

    connection = SpyConnection(script=[db_error(sqlstate, message)])
    error = await raised(connection)
    assert type(error) is LockOperationFailed
    assert_sanitized(error, FAILED_MESSAGE)
    assert len(connection.statements) == 1


def test_2b_u44_negative_control_from_none_inside_except_keeps_context() -> None:
    """CONSTRUCTED control: `raise ... from None` INSIDE `except` is display suppression
    only. The object still holds the raw error as `__context__`, so U44's assertion
    `__context__ is None` distinguishes the two mechanisms."""

    class Sanitized(Exception):
        pass

    def inside_except() -> None:
        try:
            raise db_error("22004", "operation id required")
        except psycopg.Error:
            raise Sanitized("operation id required") from None

    with pytest.raises(Sanitized) as caught:
        inside_except()
    assert caught.value.__suppress_context__ is True
    assert caught.value.__context__ is not None


async def test_2b_u44_every_path_is_sanitized_at_object_level() -> None:
    """Covered in the U43, U46 and U47 rows through `assert_sanitized`; this row adds
    the step-5 path and checks the exception is not chained to the database error."""

    from haloflow.m02.lock import LockOperationFailed

    error = await raised(SpyConnection(script=[ISOLATION_OK, [(PROBE,)]]))
    assert type(error) is LockOperationFailed
    assert_sanitized(error, FAILED_MESSAGE)


def _chain(error: BaseException) -> list[BaseException]:
    """Every exception reachable through BOTH `__cause__` and `__context__` links."""

    seen: list[BaseException] = []
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if any(current is known for known in seen):
            continue
        seen.append(current)
        pending.extend(
            link for link in (current.__cause__, current.__context__) if link is not None
        )
    return seen


CALLER_PROBE = "CALLER-OWNED-9c1e"


def _ambient_lookup() -> BaseException:
    return LookupError(CALLER_PROBE)


def _ambient_psycopg() -> BaseException:
    """The CALLER's own database error (caller-owned; allowed in the chain)."""

    error: BaseException = psycopg.errors.lookup("40001")(CALLER_PROBE)
    return error


def _ambient_chained() -> BaseException:
    """A caller exception chained to another caller exception."""

    outer = RuntimeError(CALLER_PROBE)
    outer.__cause__ = ValueError(CALLER_PROBE)
    return outer


AMBIENTS = {"lookup": _ambient_lookup, "caller-psycopg": _ambient_psycopg,
            "chained": _ambient_chained}


def _helper_path(path: str) -> tuple[list[Any], list[BaseException], str]:
    """(script, helper-originated errors in it, expected message) for each path."""

    if path == "isolation-error":
        error = db_error("08006", "connection lost")
        return [error], [error], FAILED_MESSAGE
    if path == "mapped":
        error = db_error("22004", "operation id required")
        return [ISOLATION_OK, error], [error], "operation id required"
    if path == "unmapped":
        error = db_error("42501", "permission denied")
        return [ISOLATION_OK, error], [error], FAILED_MESSAGE
    if path == "malformed":
        return [ISOLATION_OK, [(PROBE,)]], [], FAILED_MESSAGE
    raise AssertionError(path)


@pytest.mark.parametrize("ambient", sorted(AMBIENTS))
@pytest.mark.parametrize("path", ["isolation-error", "mapped", "unmapped", "malformed"])
async def test_2b_u44_ambient_caller_context_is_kept_and_no_helper_error_leaks(
    ambient: str, path: str
) -> None:
    """U44 AMBIENT CASES (Codex packet-v1 P2, v2 review): the precise boundary.

    Helper-originated errors (the SAME objects the spy raised) are nowhere in the chain,
    and their probe text is nowhere in the traceback; `__cause__` is None; the caller's
    ambient exception is preserved as `__context__`, whatever it is.
    """

    script, helper_errors, message = _helper_path(path)
    caller = AMBIENTS[ambient]()
    try:
        raise caller
    except BaseException:
        error = await raised(SpyConnection(script=script))
    assert str(error) == message and error.__cause__ is None
    assert error.__context__ is caller
    chain = _chain(error)
    assert not any(link is helper for link in chain for helper in helper_errors)
    rendered = "".join(traceback.format_exception(error))
    assert PROBE not in rendered
    assert CALLER_PROBE in rendered  # the caller-owned context is preserved, not stripped


# --- U45, U46: transaction-state precheck, then isolation ---------------------


@pytest.mark.parametrize(
    "status",
    [TransactionStatus.IDLE, TransactionStatus.INERROR, TransactionStatus.ACTIVE,
     TransactionStatus.UNKNOWN],
    ids=["IDLE", "INERROR", "ACTIVE", "UNKNOWN"],
)
async def test_2b_u45_a_non_intrans_connection_is_refused_with_no_statement(
    status: TransactionStatus,
) -> None:
    from haloflow.m02.lock import LockOperationPreconditionError

    connection = SpyConnection(status=status, script=valid_script())
    error = await raised(connection)
    assert type(error) is LockOperationPreconditionError
    assert_sanitized(error, PRECONDITION_MESSAGE)
    assert connection.statements == []


@pytest.mark.parametrize("isolation", ["repeatable read", "serializable", "read uncommitted"])
async def test_2b_u46_isolation_other_than_read_committed_is_a_precondition_error(
    isolation: str,
) -> None:
    from haloflow.m02.lock import LockOperationPreconditionError

    connection = SpyConnection(script=[[(isolation,)], [(OPERATION,)]])
    error = await raised(connection)
    assert type(error) is LockOperationPreconditionError
    assert_sanitized(error, PRECONDITION_MESSAGE)
    assert len(connection.statements) == 1


async def test_2b_u46_a_database_error_on_the_isolation_query_is_a_generic_failure() -> None:
    from haloflow.m02.lock import LockOperationFailed

    error = await raised(SpyConnection(script=[db_error("08006", "connection lost")]))
    assert type(error) is LockOperationFailed
    assert_sanitized(error, FAILED_MESSAGE)


# --- U47: return cardinality and shape -----------------------------------------


@pytest.mark.parametrize(
    "rows",
    [[], [(OPERATION,), (OPERATION,)], [(OPERATION, 1)], [(None,)], [("not-a-uuid",)]],
    ids=["zero-rows", "two-rows", "two-columns", "null", "not-uuid"],
)
async def test_2b_u47_a_malformed_return_is_a_generic_failure(rows: list[Any]) -> None:
    from haloflow.m02.lock import LockOperationFailed

    error = await raised(SpyConnection(script=[ISOLATION_OK, rows]))
    assert type(error) is LockOperationFailed
    assert_sanitized(error, FAILED_MESSAGE)


# --- U48: the valid control ------------------------------------------------------


async def test_2b_u48_valid_control_returns_the_uuid_and_touches_no_transaction_state() -> None:
    connection = SpyConnection(script=valid_script())
    assert await call(connection) == OPERATION
    assert len(connection.statements) == 2
    assert "transaction_isolation" in connection.statements[0]
    assert f'"{SCHEMA}"."m02_lock_operation"' in connection.statements[1]
    lowered = [statement.strip().casefold() for statement in connection.statements]
    for statement in lowered:
        assert not statement.startswith(FORBIDDEN_STATEMENTS), statement
    assert connection.info.transaction_status == TransactionStatus.INTRANS
