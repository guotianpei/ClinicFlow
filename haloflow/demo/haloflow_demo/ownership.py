"""Single owner process (R-3, R-4, R-5, R-13) plus the process-lifetime lock.

Acquisition order (P-BOOT), each step recorded in ``events`` when given:

1. ``process_lock`` – OS exclusive lock for the whole process lifetime
2. ``db_lock``      – dedicated, unpooled connection; ``pg_try_advisory_lock``
3. ``tables``       – identity check + ``create_all(checkfirst)`` under ownership
4. ``epoch``        – bootstrap ``demo_settings`` row, then ``owner_epoch + 1``
5. ``recovery``     – local, fenced, no external call

Release order in :meth:`Ownership.close`: drain → ``db_lock_released`` →
``process_lock_released``. The process lock is released last, so no replacement
can start while this process could still make an external call.

External calls run as tasks owned by this object (:meth:`guard_external`), not
by the request that started them, so shutdown can wait for them
(:meth:`drain`). Once :meth:`begin_closing` is called, new external calls are
refused. If :meth:`drain` cannot confirm every call finished, the caller must
not release the process lock (the runner exits and the kernel releases it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any, Protocol, TypeVar

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from haloflow_demo.db import ConnectedIdentity, check_identity, make_demo_engine
from haloflow_demo.errors import OwnerLost
from haloflow_demo.models import create_demo_tables
from haloflow_demo.process_lock import ProcessLock
from haloflow_demo.settings import DemoDbTarget, DemoSettings, StartupRefused

__all__ = [
    "DrainIncomplete",
    "NullProcessLock",
    "OwnerLost",
    "Ownership",
    "acquire_ownership",
    "recover",
]

logger = logging.getLogger(__name__)
T = TypeVar("T")


class DrainIncomplete(Exception):
    """Tracked external work is still running; the locks were NOT released."""


class _Lock(Protocol):
    def acquire(self) -> None: ...
    def release(self) -> None: ...


class NullProcessLock:
    """No-op lock. Used only by tests and the EXP1 negative control."""

    def acquire(self) -> None:
        return None

    def release(self) -> None:
        return None


# PostgreSQL scopes advisory locks to the current database, so one constant key
# identifies "the demo owner of this database" however it is reached (TCP,
# localhost, ::1 or a Unix socket). A URL-derived key would not (correction R2).
DEMO_ADVISORY_KEY = 0x48414C4F464C4F57 & 0x7FFFFFFFFFFFFFFF  # "HALOFLOW", positive bigint


def advisory_key(target: DemoDbTarget) -> int:
    return DEMO_ADVISORY_KEY


def _record(events: list[str] | None, name: str) -> None:
    if events is not None:
        events.append(name)


class Ownership:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        lock_conn: asyncpg.Connection,
        process_lock: _Lock,
        epoch: int,
        lock_backend_pid: int,
        key: int,
        events: list[str] | None,
    ) -> None:
        self.engine = engine
        self.epoch = epoch
        self.lock_backend_pid = lock_backend_pid
        self.lost = asyncio.Event()
        self._lock_conn = lock_conn
        self._process_lock = process_lock
        self._key = key
        self._events = events
        self._watchdog: asyncio.Task[None] | None = None
        self._closed = False
        self.closing_event = asyncio.Event()
        self._inflight: set[asyncio.Task[object]] = set()

    @property
    def owner_lost(self) -> bool:
        return self.lost.is_set()

    def _mark_lost(self, why: str) -> None:
        if not self.lost.is_set():
            logger.error("demo owner lost: %s", why)
            self.lost.set()

    # ── fencing ──────────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def fenced(self, *, exclusive: bool = False) -> AsyncIterator[AsyncConnection]:
        """A transaction that commits only if this process still holds the epoch.

        Allowed while closing (an in-flight call may still record its outcome
        during drain); refused once :meth:`close` has released ownership.

        ``exclusive=True`` takes the settings row FOR UPDATE. Pause and opt-out
        writers use it so they serialise with in-flight dispatches, which hold
        the row FOR SHARE from their last checks through the provider request.
        """
        if self.owner_lost or self._closed:
            raise OwnerLost("owner lost or ownership closed")
        # Statement tags identify lock waits in pg_stat_activity (tests, operators).
        mode = "FOR UPDATE /* fence:exclusive */" if exclusive else "FOR SHARE /* fence */"
        async with self.engine.begin() as conn:
            current = (await conn.execute(
                text(f"SELECT owner_epoch FROM demo_settings WHERE id = 1 {mode}")
            )).scalar_one_or_none()
            if current != self.epoch:
                self._mark_lost(f"epoch {current} != {self.epoch}")
                raise OwnerLost("epoch changed")
            yield conn

    @property
    def closing(self) -> bool:
        return self.closing_event.is_set()

    def begin_closing(self) -> None:
        """From now on every new external call is refused."""
        self.closing_event.set()

    async def guard_external(self, call: Callable[[], Awaitable[T]]) -> T:
        """Run an external call only while ownership is intact and not closing.

        The call runs as a task tracked by this object and shielded from the
        caller's cancellation, so :meth:`drain` can account for it.
        """
        if self.owner_lost or self.closing or self._closed:
            raise OwnerLost("owner lost or closing before external call")

        async def _run() -> T:
            return await call()

        task: asyncio.Task[T] = asyncio.get_running_loop().create_task(_run())
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return await asyncio.shield(task)

    def spawn_tracked(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Run long-lived internal work (the reply dispatcher) as a tracked task,
        so :meth:`drain` waits for it, cancels it after the timeout, and reports
        an incomplete drain if it ignores cancellation. The process then exits
        without releasing its locks."""
        task: asyncio.Task[T] = asyncio.get_running_loop().create_task(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return task

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    async def drain(self, timeout: float, cancel_grace: float, *, cancel: bool = True) -> bool:
        """Wait for tracked external calls; cancel stragglers. True only if all ended."""
        pending = set(self._inflight)
        if not pending:
            return True
        _, pending = await asyncio.wait(pending, timeout=timeout)
        if not pending:
            return True
        if not cancel:
            return False
        for task in pending:
            task.cancel()
        _, still = await asyncio.wait(pending, timeout=cancel_grace)
        return not still

    # ── watchdog (R-5) ───────────────────────────────────────────────────────

    def start_watchdog(self, interval: float = 2.0) -> None:
        if self._watchdog is None:
            self._watchdog = asyncio.get_running_loop().create_task(self._watch(interval))

    async def _watch(self, interval: float) -> None:
        while not self.owner_lost:
            await asyncio.sleep(interval)
            try:
                await self._lock_conn.fetchval("SELECT 1")
            except Exception as exc:  # any error means the session lock may be gone
                self._mark_lost(f"lock connection: {type(exc).__name__}")

    # ── shutdown ─────────────────────────────────────────────────────────────

    async def close(
        self,
        drain: Callable[[], Awaitable[None]] | None = None,
        *,
        drain_timeout: float = 10.0,
        cancel_grace: float = 2.0,
        cancel: bool = True,
    ) -> None:
        """Begin closing, drain tracked external work, then release DB lock and
        process lock, in that order. Raises :class:`DrainIncomplete` and
        releases NOTHING if any tracked external call is still running."""
        if self._closed:
            return
        self.begin_closing()
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watchdog
            self._watchdog = None
        if drain is not None:
            await drain()
        if not await self.drain(drain_timeout, cancel_grace, cancel=cancel):
            raise DrainIncomplete(f"{self.inflight} external call(s) still running")
        self._closed = True
        with contextlib.suppress(Exception):
            await self._lock_conn.fetchval("SELECT pg_advisory_unlock($1)", self._key)
        with contextlib.suppress(Exception):
            await self._lock_conn.close(timeout=5)
        with contextlib.suppress(Exception):
            await self.engine.dispose()
        _record(self._events, "db_lock_released")
        self._process_lock.release()
        _record(self._events, "process_lock_released")


async def _connect_lock(target: DemoDbTarget) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=target.socket_dir if target.socket_dir is not None else target.host,
        port=target.port,
        user=target.user,
        password=target.password,
        database=target.dbname,
    )


async def acquire_ownership(
    settings: DemoSettings,
    target: DemoDbTarget,
    *,
    events: list[str] | None = None,
    process_lock: _Lock | None = None,
) -> Ownership:
    lock: _Lock = process_lock or ProcessLock()
    lock.acquire()
    _record(events, "process_lock")
    lock_conn: asyncpg.Connection | None = None
    engine: AsyncEngine | None = None
    try:
        lock_conn = await _connect_lock(target)
        row = await lock_conn.fetchrow("SELECT current_database(), host(inet_server_addr())")
        check_identity(ConnectedIdentity(database=row[0], server_addr=row[1]), target)
        key = advisory_key(target)
        if not await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
            raise StartupRefused("db_lock_held")
        pid = int(await lock_conn.fetchval("SELECT pg_backend_pid()"))
        _record(events, "db_lock")

        engine = make_demo_engine(target)
        await create_demo_tables(engine, target)
        _record(events, "tables")

        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO demo_settings(id, paused, owner_epoch) VALUES (1, false, 0) "
                "ON CONFLICT (id) DO NOTHING"
            ))
            epoch = int((await conn.execute(text(
                "UPDATE demo_settings SET owner_epoch = owner_epoch + 1 WHERE id = 1 "
                "RETURNING owner_epoch"
            ))).scalar_one())
        _record(events, "epoch")

        own = Ownership(engine=engine, lock_conn=lock_conn, process_lock=lock, epoch=epoch,
                        lock_backend_pid=pid, key=key, events=events)
        await recover(own)
        _record(events, "recovery")
        return own
    except BaseException:
        if lock_conn is not None:
            with contextlib.suppress(Exception):
                await lock_conn.close(timeout=5)
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.dispose()
        lock.release()
        raise


async def recover(own: Ownership) -> None:
    """R-13: local state only; no SMS or EHR call."""
    async with own.fenced() as conn:
        # A contact admitted before a crash is uncertain: it may have reached the phone.
        await conn.execute(text(
            "UPDATE contact_history SET outcome = 'UNCERTAIN' WHERE outcome IS NULL AND"
            " contact_seq IN (SELECT contact_seq FROM reminder_records WHERE state = 'CLAIMED'"
            " AND contact_seq IS NOT NULL)"
        ))
        await conn.execute(text(
            "UPDATE reminder_records SET state = 'UNCERTAIN', reason = 'restart_after_claim', "
            "updated_at = now() WHERE state = 'CLAIMED'"
        ))
        await conn.execute(text(
            "UPDATE writeback_tasks SET state = 'UNCERTAIN', reason = 'restart', "
            "updated_at = now() WHERE state = 'IN_FLIGHT'"
        ))
