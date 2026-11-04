"""Public construction of M01-owned pools for the platform entry point (L-1W W4a).

``haloflow.m01.pool`` stays protected (TC-D10) and ``psycopg_pool`` stays M01-only
(TC-D1): the entry point opens its pools through these two functions and holds them
only as opaque resources with an async ``close``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from haloflow.m01.pool import AdmissionPool, TenantPool

Close = Callable[[], Awaitable[None]]


async def open_tenant_pool(conninfo: str, *, min_size: int = 1) -> tuple[Any, Close]:
    """The tenant pool (fail-closed session baseline), opened and waited for."""

    pool = TenantPool(conninfo, min_size=min_size)
    await pool.open()
    return pool, pool.close


async def open_admission_pool(conninfo: str, *, min_size: int = 1) -> tuple[Any, Close]:
    """The admission pool, opened and waited for."""

    pool = AdmissionPool(conninfo, min_size=min_size)
    await pool.open()
    return pool, pool.close
