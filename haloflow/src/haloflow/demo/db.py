"""Demo database engine and connected-identity check (R-8, precision P-ID).

The demo has its own engine, built only from a validated :class:`DemoDbTarget`.
Nothing from the legacy database module (its engine, session factory or
request dependency) is used. After connecting, the server-reported identity is checked as well
as the parsed URL, so an equivalent or proxied URL cannot land on another DB.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from haloflow.demo.settings import LOOPBACK_HOSTS, DemoDbTarget, StartupRefused


@dataclass(frozen=True)
class ConnectedIdentity:
    database: str
    server_addr: str | None  # None when connected over a Unix socket


def sqlalchemy_url(target: DemoDbTarget) -> URL:
    query: dict[str, str] = {}
    if target.socket_dir is not None:
        query["host"] = target.socket_dir
    return URL.create(
        "postgresql+asyncpg",
        username=target.user,
        password=target.password,
        host=target.host if target.socket_dir is None else None,
        port=target.port,
        database=target.dbname,
        query=query,
    )


def make_demo_engine(target: DemoDbTarget) -> AsyncEngine:
    if not isinstance(target, DemoDbTarget):
        raise TypeError("make_demo_engine requires a validated DemoDbTarget")
    return create_async_engine(sqlalchemy_url(target), pool_size=5, max_overflow=5,
                               pool_pre_ping=True)


async def read_identity(conn: AsyncConnection) -> ConnectedIdentity:
    row = (await conn.execute(
        text("SELECT current_database(), host(inet_server_addr())")
    )).one()
    return ConnectedIdentity(database=str(row[0]), server_addr=row[1])


def check_identity(observed: ConnectedIdentity, target: DemoDbTarget) -> None:
    if observed.database != target.dbname or not observed.database.endswith("_demo"):
        raise StartupRefused("demo_db_identity", "connected database differs from target")
    if observed.server_addr is not None and observed.server_addr not in LOOPBACK_HOSTS:
        raise StartupRefused("demo_db_identity", "connected server is not loopback")
