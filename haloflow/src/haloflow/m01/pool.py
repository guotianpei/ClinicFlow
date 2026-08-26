"""M01-owned psycopg3 pool with a fail-closed session baseline."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool

from haloflow.m01.errors import RoutingSetupFailed


class TenantPool:
    """The only runtime owner of tenant-capable physical connections."""

    def __init__(
        self,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        timeout: float = 30.0,
        name: str = "haloflow-m01-runtime",
    ) -> None:
        self._pool = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            name=name,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": None},
            check=self._check,
            reset=self._reset,
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def _connection_for_gateway(self) -> AsyncIterator[AsyncConnection]:
        async with self._pool.connection() as connection:
            yield connection

    @asynccontextmanager
    async def _connection_for_control(self) -> AsyncIterator[AsyncConnection]:
        """Restricted M01 control path; never exposed to application modules."""

        async with self._pool.connection() as connection:
            yield connection

    async def _check(self, connection: AsyncConnection) -> None:
        await self._assert_baseline(connection)

    async def _reset(self, connection: AsyncConnection) -> None:
        if connection.pgconn.transaction_status != TransactionStatus.IDLE:
            await connection.rollback()
        await connection.execute("DISCARD ALL")
        await self._assert_baseline(connection)

    async def _assert_baseline(self, connection: AsyncConnection) -> None:
        if connection.pgconn.transaction_status != TransactionStatus.IDLE:
            raise RoutingSetupFailed(reason_code="POOL_TRANSACTION_NOT_IDLE")

        row = await (
            await connection.execute("SELECT current_schemas(true), pg_my_temp_schema()")
        ).fetchone()
        if row is None or row[0] != ["pg_catalog"] or row[1] != 0:
            raise RoutingSetupFailed(reason_code="POOL_BASELINE_MISMATCH")
