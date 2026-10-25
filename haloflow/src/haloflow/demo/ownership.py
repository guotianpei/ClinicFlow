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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, TypeVar

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from haloflow.demo.db import ConnectedIdentity, check_identity, make_demo_engine
from haloflow.demo.models import create_demo_tables
from haloflow.demo.process_lock import ProcessLock, lock_path_for
from haloflow.demo.settings import DemoDbTarget, DemoSettings, StartupRefused

logger = logging.getLogger(__name__)
T = TypeVar("T")


class OwnerLost(Exception):
    """This process is no longer the demo owner; no further effects allowed."""


class _Lock(Protocol):
    def acquire(self) -> None: ...
    def release(self) -> None: ...


class NullProcessLock:
    """No-op lock. Used only by tests and the EXP1 negative control."""

    def acquire(self) -> None:
        return None

    def release(self) -> None:
        return None


def advisory_key(target: DemoDbTarget) -> int:
    return int(target.key_hash[:15], 16)  # < 2**60, fits a signed bigint


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

    @property
    def owner_lost(self) -> bool:
        return self.lost.is_set()

    def _mark_lost(self, why: str) -> None:
        if not self.lost.is_set():
            logger.error("demo owner lost: %s", why)
            self.lost.set()

    # ── fencing ──────────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def fenced(self) -> AsyncIterator[AsyncConnection]:
        """A transaction that commits only if this process still holds the epoch."""
        if self.owner_lost:
            raise OwnerLost("owner lost")
        async with self.engine.begin() as conn:
            current = (await conn.execute(
                text("SELECT owner_epoch FROM demo_settings WHERE id = 1 FOR SHARE")
            )).scalar_one_or_none()
            if current != self.epoch:
                self._mark_lost(f"epoch {current} != {self.epoch}")
                raise OwnerLost("epoch changed")
            yield conn

    async def guard_external(self, call: Callable[[], Awaitable[T]]) -> T:
        """Run an external call only while ownership is intact."""
        if self.owner_lost or self._closed:
            raise OwnerLost("owner lost before external call")
        return await call()

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

    async def close(self, drain: Callable[[], Awaitable[None]] | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watchdog
        if drain is not None:
            await drain()
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
    lock: _Lock = process_lock or ProcessLock(lock_path_for(settings.lock_dir, target))
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
        await conn.execute(text(
            "UPDATE reminder_records SET state = 'UNCERTAIN', reason = 'restart_after_claim', "
            "updated_at = now() WHERE state = 'CLAIMED'"
        ))
        await conn.execute(text(
            "UPDATE writeback_tasks SET state = 'UNCERTAIN', reason = 'restart', "
            "updated_at = now() WHERE state = 'IN_FLIGHT'"
        ))
