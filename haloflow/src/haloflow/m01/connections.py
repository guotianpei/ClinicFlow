"""Public construction of M01-owned pools for the platform entry point (L-1W W4a).

``haloflow.m01.pool`` stays protected (TC-D10) and ``psycopg_pool`` stays M01-only
(TC-D1): the entry point opens its pools through these two functions and holds them
only as opaque resources with an async ``close``.

Acquisition ownership (Codex W4A-LIFE-1): each opener owns its pool from construction
until it returns the ``(resource, close)`` pair. If opening fails or is cancelled, the
pool is closed before the exception leaves; a close failure never replaces the
original; a cancellation (original or during the close) wins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from haloflow.m01.pool import AdmissionPool, TenantPool

Close = Callable[[], Awaitable[None]]


async def _owned_open(pool: Any) -> tuple[Any, Close]:
    try:
        await pool.open()
    except BaseException as original:
        cancelled = False
        try:
            await pool.close()
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 - never raised over the original
            pass
        if cancelled and not isinstance(original, asyncio.CancelledError):
            raise asyncio.CancelledError() from None
        raise
    return pool, pool.close


async def open_tenant_pool(conninfo: str, *, min_size: int = 1) -> tuple[Any, Close]:
    """The tenant pool (fail-closed session baseline), opened and waited for."""

    return await _owned_open(TenantPool(conninfo, min_size=min_size))


async def open_admission_pool(conninfo: str, *, min_size: int = 1) -> tuple[Any, Close]:
    """The admission pool, opened and waited for."""

    return await _owned_open(AdmissionPool(conninfo, min_size=min_size))
