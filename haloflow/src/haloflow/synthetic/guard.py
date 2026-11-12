"""Protocol F of the synthetic installer (Option B B2; B2-I1, Codex ALIGN 2026-11-12).
SYNTHETIC ONLY.

F1  The cluster reservation is the comment on the bootstrap superuser (oid 10, a shared
    catalog entry, NULL on a fresh initdb):
        haloflow-synthetic-reservation:v2:<nonce>:<sysid>:<target oid>:<epoch>
    It is first set only from NULL, by a transaction holding pg_advisory_xact_lock(K_X) in
    `postgres` that re-validates the fresh baseline.
F2  Every committing transaction runs, FIRST:
        SET TRANSACTION READ WRITE
        SELECT pg_advisory_xact_lock(K_X)            (shared for runtime transactions)
        READ COMMITTED check
        identity of THIS connection (cluster sysid, current database oid) == the attested value
        the reservation, read in a SEPARATE statement (a fresh snapshot after the lock wait),
        == the exact attested value
    Lock, check and commit share one transaction on one connection.
F3  Every run first bumps the epoch (takeover) under the target's exclusive K_X: it waits for
    every in-flight guarded transaction, and from its commit on every older epoch is fenced.
F4  Guarded sessions are read-only by default: a write outside a guarded transaction is
    refused by PostgreSQL.
K_S A session lock in `postgres`: exclusive for an installer run, shared for the runtime
    lease. An early, cheap refusal only; no safety argument rests on it.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.pq import TransactionStatus

from haloflow.synthetic.attest import admin_connect, fresh_failures
from haloflow.synthetic.dsn import AdminEndpoint
from haloflow.synthetic.errors import SyntheticRefused
from haloflow.synthetic.hooks import fire

K_X: Final = 0x4846_4C57_4232_4931   # "HFLWB2I1": transaction-scoped guard key (fixed)
K_S: Final = 0x4846_4C57_4232_4C53   # "HFLWB2LS": session lease key (fixed)
RESERVATION_PREFIX: Final = "haloflow-synthetic-reservation:v2:"
RES_SQL: Final = "SELECT pg_catalog.shobj_description(10, 'pg_authid')"
IDENT_SQL: Final = (
    "SELECT (SELECT system_identifier FROM pg_catalog.pg_control_system())::text,"
    " (SELECT oid FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database())")

ConnectionFactory = Callable[[], Awaitable[AsyncConnection[Any]]]

# Process-local run context (set by provision.py; read by the hook payloads).
_TXN_COUNT: list[int] = [0]
INSTALL_LOCK_PID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "haloflow_synthetic_install_lock_pid", default=None)
PHASE_HOOK: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "haloflow_synthetic_phase_hook", default=None)


def reservation_value(nonce: str, sysid: str, target_oid: int, epoch: str) -> str:
    return f"{RESERVATION_PREFIX}{nonce}:{sysid}:{int(target_oid)}:{epoch}"


def parse_reservation(value: str) -> tuple[str, str, int, str] | None:
    if not value.startswith(RESERVATION_PREFIX):
        return None
    parts = value[len(RESERVATION_PREFIX):].split(":")
    if len(parts) != 4 or not parts[2].isdigit():
        return None
    return parts[0], parts[1], int(parts[2]), parts[3]


def _check(identity: Any, current: Any, expected: str) -> None:
    want = parse_reservation(expected)
    if want is None:
        raise SyntheticRefused("NOT_RESERVED")
    _, sysid, oid, _ = want
    if identity is None or str(identity[0]) != sysid:
        raise SyntheticRefused("CLUSTER_MISMATCH")
    if int(identity[1]) != oid:
        raise SyntheticRefused("TARGET_MISMATCH")
    if current is None:
        raise SyntheticRefused("NOT_RESERVED")
    if current != expected:
        raise SyntheticRefused("EPOCH_SUPERSEDED" if current.startswith(
            expected.rsplit(":", 1)[0] + ":") else "RESERVED_BY_OTHER")


def guard_sync(conn: Any, expected: str, *, shared: bool = False) -> None:
    """F2 on a psycopg (sync) connection whose transaction has just begun."""

    conn.execute("SET TRANSACTION READ WRITE")
    conn.execute("SELECT pg_catalog.pg_advisory_xact_lock_shared(%s)" if shared
                 else "SELECT pg_catalog.pg_advisory_xact_lock(%s)", (K_X,))
    iso = conn.execute("SELECT pg_catalog.current_setting('transaction_isolation')").fetchone()
    if iso is None or iso[0] != "read committed":
        raise SyntheticRefused("ISOLATION_NOT_READ_COMMITTED")
    identity = conn.execute(IDENT_SQL).fetchone()
    row = conn.execute(RES_SQL).fetchone()                 # separate statement: fresh snapshot
    _check(identity, None if row is None else row[0], expected)


async def guard_async(conn: AsyncConnection[Any], expected: str, *,
                      shared: bool = False) -> None:
    await conn.execute("SET TRANSACTION READ WRITE")
    await conn.execute("SELECT pg_catalog.pg_advisory_xact_lock_shared(%s)" if shared
                       else "SELECT pg_catalog.pg_advisory_xact_lock(%s)", (K_X,))
    iso = await (await conn.execute(
        "SELECT pg_catalog.current_setting('transaction_isolation')")).fetchone()
    if iso is None or iso[0] != "read committed":
        raise SyntheticRefused("ISOLATION_NOT_READ_COMMITTED")
    identity = await (await conn.execute(IDENT_SQL)).fetchone()
    row = await (await conn.execute(RES_SQL)).fetchone()
    _check(identity, None if row is None else row[0], expected)


class GuardedAsyncConnection(AsyncConnection[Any]):
    """Every OUTERMOST transaction() is guarded (F2); savepoints are not re-guarded. The session
    is read-only by default (F4). Injected through the existing M01 ConnectionFactory."""

    expected: str = ""

    @asynccontextmanager
    async def transaction(
        self, savepoint_name: str | None = None, force_rollback: bool = False
    ) -> AsyncIterator[Any]:
        if self.info.transaction_status != TransactionStatus.IDLE:
            async with super().transaction(savepoint_name, force_rollback) as inner:
                yield inner
            return
        phase = PHASE_HOOK.get()
        if phase is not None:
            PHASE_HOOK.set(None)                           # the phase's FIRST transaction only
        async with super().transaction(savepoint_name, force_rollback) as tx:
            await guard_async(self, self.expected)
            _TXN_COUNT[0] += 1
            fire("guarded_txn", {"n": _TXN_COUNT[0], "backend_pid": self.info.backend_pid,
                                 "install_lock_pid": INSTALL_LOCK_PID.get()})
            yield tx
            if phase is not None:
                fire(f"phase.{phase}.before_commit", {"backend_pid": self.info.backend_pid})
        if phase is not None:
            fire(f"phase.{phase}.commit_ack", {"backend_pid": self.info.backend_pid})


def guarded_factory(conninfo: str, expected: str) -> ConnectionFactory:
    async def connect() -> AsyncConnection[Any]:
        conn = await GuardedAsyncConnection.connect(conninfo, autocommit=True)
        conn.expected = expected
        await conn.execute("SET default_transaction_read_only = on")
        return conn

    return connect


# ---------------------------------------------------------------------------- F1 / F3

def _bootstrap_name(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute("SELECT rolname FROM pg_catalog.pg_authid WHERE oid = 10").fetchone()
    assert row is not None
    return str(row[0])


def _set_reservation(conn: psycopg.Connection[Any], value: str) -> None:
    conn.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(
        sql.Identifier(_bootstrap_name(conn)), sql.Literal(value)))


def cluster_identity(endpoint: AdminEndpoint, db: str | None = None) -> tuple[str, int]:
    with admin_connect(endpoint, db or endpoint.target) as conn:
        row = conn.execute(IDENT_SQL).fetchone()
    if row is None:
        raise SyntheticRefused("TARGET_MISMATCH")
    return str(row[0]), int(row[1])


def read_reservation(endpoint: AdminEndpoint) -> str | None:
    with admin_connect(endpoint, "postgres") as conn:
        row = conn.execute(RES_SQL).fetchone()
    return None if row is None else row[0]


def begin_run(endpoint: AdminEndpoint, phase: str | None, nonce: str, epoch: str) -> str:
    """Start-of-run decision BEFORE any mutation (matrix rows 1-8, 26-27; v1r1 §2)."""

    current = read_reservation(endpoint)
    if current is None:
        if phase not in (None, "intent"):
            raise SyntheticRefused("STATE_DIVERGED")
        bad = fresh_failures(endpoint, endpoint.target)           # read-only preflight
        if bad:
            raise SyntheticRefused("CLUSTER_NOT_FRESH", bad)
        fire("reserve.before_txn", {})
        _reserve(endpoint, nonce, epoch)
        fire("reserve.after_commit", {})
        return "RESERVED"
    if phase is None:
        raise SyntheticRefused("CLUSTER_NOT_FRESH", ("reservation_slot",))
    _takeover(endpoint, nonce, epoch)
    return "TAKEN_OVER"


def _reserve(endpoint: AdminEndpoint, nonce: str, epoch: str) -> None:
    sysid, oid = cluster_identity(endpoint)
    with admin_connect(endpoint, "postgres") as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")
        conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (K_X,))
        row = conn.execute(RES_SQL).fetchone()
        if row is not None and row[0] is not None:
            raise SyntheticRefused("CLUSTER_NOT_FRESH", ("reservation_slot",))
        bad = fresh_failures(endpoint, endpoint.target)           # serialized re-validation
        if bad:
            raise SyntheticRefused("CLUSTER_NOT_FRESH", bad)
        _set_reservation(conn, reservation_value(nonce, sysid, oid, epoch))


def _takeover(endpoint: AdminEndpoint, nonce: str, epoch: str) -> None:
    with admin_connect(endpoint, endpoint.target) as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")
        conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (K_X,))
        identity = conn.execute(IDENT_SQL).fetchone()
        row = conn.execute(RES_SQL).fetchone()
        current = None if row is None else row[0]
        if current is None:
            raise SyntheticRefused("NOT_RESERVED")
        parsed = parse_reservation(current)
        if parsed is None or parsed[0] != nonce:
            raise SyntheticRefused("RESERVED_BY_OTHER")
        if identity is None or parsed[1] != str(identity[0]):
            raise SyntheticRefused("CLUSTER_MISMATCH")
        if parsed[2] != int(identity[1]):
            raise SyntheticRefused("TARGET_MISMATCH")
        _set_reservation(conn, reservation_value(nonce, parsed[1], parsed[2], epoch))
        fire("takeover.before_commit", {"backend_pid": conn.info.backend_pid})


def new_epoch() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------- K_S

class InstallLock:
    """The installer's exclusive K_S session lock (early refusal) on a dedicated connection."""

    def __init__(self, endpoint: AdminEndpoint) -> None:
        self._endpoint = endpoint
        self._conn: psycopg.Connection[Any] | None = None

    @property
    def pid(self) -> int | None:
        return None if self._conn is None else self._conn.info.backend_pid

    def acquire(self) -> None:
        conn = admin_connect(self._endpoint, "postgres")
        row = conn.execute("SELECT pg_catalog.pg_try_advisory_lock(%s)", (K_S,)).fetchone()
        if row is None or row[0] is not True:
            conn.close()
            raise SyntheticRefused("INSTALL_LOCKED")
        self._conn = conn

    def check(self) -> None:
        try:
            assert self._conn is not None
            self._conn.execute("SELECT 1")
        except (AssertionError, psycopg.Error):
            raise SyntheticRefused("INSTALL_LOCK_LOST") from None

    def release(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


class RuntimeLease:
    """The runtime's SHARED K_S lease (B3 holds it for the app's lifetime)."""

    def __init__(self, endpoint: AdminEndpoint) -> None:
        self._endpoint = endpoint
        self._conn: psycopg.Connection[Any] | None = None

    def acquire(self) -> None:
        conn = admin_connect(self._endpoint, "postgres")
        row = conn.execute("SELECT pg_catalog.pg_try_advisory_lock_shared(%s)",
                           (K_S,)).fetchone()
        if row is None or row[0] is not True:
            conn.close()
            raise SyntheticRefused("INSTALL_LOCKED")
        self._conn = conn

    def release(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
