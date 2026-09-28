"""CP2-2b D-layer support: gateway calls, blocking probes, bare-schema installs.

A NEW module, imported directly by the 2b PostgreSQL modules (packet README,
"Convention departure"): adding it as a fixture would edit `tests/m02/conftest.py`,
an existing file outside the approved X-1 to X-13/H-1 inventory. It reuses
`m02_support` read-only.

Every identifier is synthetic (R-X4). The superuser ADMIN connection is used for
setup, observation and cleanup only; it is never the measured identity.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import m02_support as m02
import psycopg
from psycopg import AsyncConnection, sql

GATEWAY = "m02_lock_operation"
RUNTIME_SEARCH_PATH = ""  # set explicitly and asserted; never inferred (09-17 v2 section 4)
WAIT_TIMEOUT_MS = 3000
AUDIT_PROJECTOR = "haloflow_audit_projector"


# ---------------------------------------------------------------------------
# Runtime caller: the real non-superuser login shim, SET ROLE, explicit path
# ---------------------------------------------------------------------------


@contextmanager
def runtime(ids: m02.Identities) -> Iterator[psycopg.Connection[Any]]:
    """`haloflow_test_runtime_login` -> SET ROLE haloflow_runtime; identity asserted."""

    with psycopg.connect(ids.logins[m02.RUNTIME], autocommit=True) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(m02.RUNTIME)))
        conn.execute("SET search_path = ''")
        row = conn.execute(
            "SELECT session_user, current_user, pg_catalog.current_setting('search_path'),"
            " (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = session_user)"
        ).fetchone()
        assert row is not None
        session_user, current, path, superuser = row
        assert current == m02.RUNTIME and session_user != m02.RUNTIME
        assert path == '""' and superuser is False
        yield conn


def call_gateway(
    conn: psycopg.Connection[Any], schema: str, operation_id: UUID | None,
    *, function: str = GATEWAY,
) -> Any:
    row = conn.execute(
        sql.SQL("SELECT {}(%s)").format(sql.Identifier(schema, function)), (operation_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def set_tenant(conn: psycopg.Connection[Any], value: str | None) -> None:
    """Transaction-local `app.tenant_id`, as M01's gateway sets it (gateway.py:259)."""

    if value is not None:
        conn.execute("SELECT pg_catalog.set_config('app.tenant_id', %s, true)", (value,))


def expect_call_sqlstate(
    conn: psycopg.Connection[Any], schema: str, tenant: str | None,
    operation_id: UUID | None, sqlstate: str,
) -> psycopg.Error:
    try:
        with conn.transaction():
            set_tenant(conn, tenant)
            call_gateway(conn, schema, operation_id)
    except psycopg.Error as error:
        assert error.sqlstate == sqlstate, f"expected {sqlstate}, got {error.sqlstate}"
        return error
    raise AssertionError(f"expected SQLSTATE {sqlstate}; the call succeeded")


def seed(ids: m02.Identities, schema: str, label: str) -> UUID:
    """Insert one synthetic registry row as the table owner (MIG). Returns its id."""

    row = m02.seeded_row(label)
    with m02.connect_as(ids, "MIG") as conn:
        conn.execute(m02.insert_statement(schema), row)
    operation_id: UUID = row["operation_id"]
    return operation_id


# ---------------------------------------------------------------------------
# Blocking probe: a second session that waits, observed from ADMIN
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    """Outcome of a caller run in a thread under a bounded statement timeout."""

    sqlstate: str | None = None
    value: Any = None
    pid: int | None = None
    lock_wait_seen: bool = False
    elapsed: float | None = None
    error: BaseException | None = None
    done: threading.Event = field(default_factory=threading.Event)


def probe_call(
    ids: m02.Identities, schema: str, tenant: str | None, operation_id: UUID | None,
    *, timeout_ms: int = WAIT_TIMEOUT_MS, function: str = "m02_lock_operation",
) -> Probe:
    """Call the gateway in a new runtime session (thread); ADMIN polls its wait state.

    The probe's transaction is ended (rolled back on error) inside the thread, so a
    `57014` never leaves an aborted transaction for a later retry (09-17 v2 section 3).
    """

    probe = Probe()

    def run() -> None:
        try:
            with runtime(ids) as conn:
                probe.pid = conn.info.backend_pid
                started = time.monotonic()
                try:
                    with conn.transaction():
                        conn.execute(
                            "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
                            (f"{timeout_ms}ms",),
                        )
                        set_tenant(conn, tenant)
                        probe.value = call_gateway(conn, schema, operation_id, function=function)
                except psycopg.Error as error:
                    probe.sqlstate = error.sqlstate
                probe.elapsed = time.monotonic() - started
        except BaseException as error:  # propagated to the test after join
            probe.error = error
        finally:
            probe.done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + (timeout_ms / 1000) + 5
    with m02.connect_admin(ids) as admin:
        while not probe.done.is_set() and time.monotonic() < deadline:
            if probe.pid is not None:
                row = admin.execute(
                    "SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid = %s",
                    (probe.pid,),
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    probe.lock_wait_seen = True
            time.sleep(0.05)
    thread.join(timeout=10)
    assert not thread.is_alive() and probe.done.is_set(), "the probe thread did not finish"
    if probe.error is not None:
        raise AssertionError("the probe thread failed") from probe.error
    return probe


@contextmanager
def holding_lock(
    ids: m02.Identities, schema: str, tenant: str, operation_id: UUID, *, end: str = "rollback"
) -> Iterator[Any]:
    """A runtime session that holds the row lock until the block exits, then ends
    its transaction by `end` ('commit' or 'rollback')."""

    with runtime(ids) as conn:
        conn.execute("BEGIN")
        try:
            set_tenant(conn, tenant)
            assert call_gateway(conn, schema, operation_id) == operation_id
            yield conn
        finally:
            conn.execute("COMMIT" if end == "commit" else "ROLLBACK")


# ---------------------------------------------------------------------------
# Catalogue reads (ADMIN observes; never the measured identity)
# ---------------------------------------------------------------------------


def regprocedure(schema: str) -> str:
    return f"{schema}.{GATEWAY}(uuid)"


def gateway_acl(ids: m02.Identities, schema: str) -> set[tuple[Any, ...]]:
    rows = m02.admin_all(
        ids,
        """
        SELECT e.grantee, grantee.rolname, grantor.rolname, e.privilege_type, e.is_grantable
          FROM pg_catalog.pg_proc AS p,
               LATERAL pg_catalog.aclexplode(p.proacl) AS e
          LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = e.grantee
          LEFT JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = e.grantor
         WHERE p.oid = pg_catalog.to_regprocedure(%s)
        """,
        (regprocedure(schema),),
    )
    return {(int(r[0]) == 0, r[1], r[2], r[3], r[4]) for r in rows}


EXPECTED_ACL = {
    (False, m02.LOCK_OWNER, m02.LOCK_OWNER, "EXECUTE", False),
    (False, m02.RUNTIME, m02.LOCK_OWNER, "EXECUTE", False),
}


def gateway_absent(ids: m02.Identities, schema: str) -> bool:
    return bool(m02.admin_one(
        ids, "SELECT pg_catalog.to_regprocedure(%s) IS NULL", (regprocedure(schema),)
    )[0])


# ---------------------------------------------------------------------------
# Bare-schema install: the schema exists BEFORE the runner (TC-P71 pattern)
# ---------------------------------------------------------------------------


def prepare_bare_schema(ids: m02.Identities, tenant: tuple[str, str]) -> None:
    """Tenant row in `provisioning`, schema created by the provisioner, stage-2 ACL
    installed from the shipped manifest. Mirrors test_provisioning_postgres TC-P71."""

    from haloflow.m01.provisioning import PROVISIONER_ROLE
    from haloflow.m01.provisioning.acl import install_schema_acl
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    tenant_id, schema_key = tenant

    async def _prepare() -> None:
        async with await AsyncConnection.connect(ids.admin, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO shared.tenants (tenant_id, schema_key, lifecycle_state,"
                " schema_version) VALUES (%s, %s, 'provisioning', 1)",
                (tenant_id, schema_key),
            )
            await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
            await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))
            async with conn.transaction():
                await install_schema_acl(conn, schema_key, load_provisioning_manifest())

    asyncio.run(_prepare())


def run_migrations(
    ids: m02.Identities, registry: Any, tenant: tuple[str, str],
    connection_class: type[AsyncConnection[Any]] = AsyncConnection,
) -> Any:
    """The REAL runner as the migrator login (non-superuser), over `registry`."""

    from haloflow.m01.provisioning import TenantMigrationRunner

    async def _connect() -> AsyncConnection[Any]:
        return await connection_class.connect(ids.logins[m02.MIGRATOR], autocommit=True)

    async def _apply() -> Any:
        runner = TenantMigrationRunner(_connect, registry)
        return await runner.apply(tenant_id=tenant[0], schema_key=tenant[1])

    return asyncio.run(_apply())


# ---------------------------------------------------------------------------
# Default-ACL planting (ADMIN setup, labelled), always cleaned
# ---------------------------------------------------------------------------


@contextmanager
def planted_default(ids: m02.Identities, grant: str, revert: str) -> Iterator[None]:
    """Run `grant` (an ALTER DEFAULT PRIVILEGES statement) as ADMIN; `revert` afterwards,
    then require that no default-ACL row remains for the lock owner."""

    with m02.connect_admin(ids) as conn:
        conn.execute(grant)  # type: ignore[call-overload]
    try:
        yield
    finally:
        with m02.connect_admin(ids) as conn:
            conn.execute(revert)  # type: ignore[call-overload]
        assert m02.admin_one(
            ids,
            "SELECT count(*) FROM pg_catalog.pg_default_acl"
            " WHERE defaclrole = %s::pg_catalog.regrole",
            (m02.LOCK_OWNER,),
        ) == (0,)


# ---------------------------------------------------------------------------
# Adapter spy (I-B11: the runner calls installed_state.compare_installed_function
# through the module attribute)
# ---------------------------------------------------------------------------


@contextmanager
def adapter_calls(monkeypatch: Any) -> Iterator[list[str]]:
    from haloflow.m01.provisioning import installed_state

    calls: list[str] = []
    original: Callable[..., None] = installed_state.compare_installed_function

    def spy(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs.get("schema_key", ""))
        original(*args, **kwargs)

    monkeypatch.setattr(installed_state, "compare_installed_function", spy)
    yield calls


class ChainObservingConnection(AsyncConnection):  # type: ignore[type-arg]
    """Records `(session_user, current_user, session_user is superuser)` immediately
    after the gateway's CREATE FUNCTION executes, in the same transaction (D31)."""

    observed: list[tuple[Any, ...]] = []

    async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:  # type: ignore[override]
        result = await super().execute(query, params, **kwargs)
        raw = query if isinstance(query, bytes) else (
            query.encode() if isinstance(query, str) else b""
        )
        if b"CREATE FUNCTION" in raw and GATEWAY.encode() in raw:
            cursor = await super().execute(
                "SELECT session_user, current_user,"
                " (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = session_user)"
            )
            row = await cursor.fetchone()
            ChainObservingConnection.observed.append(tuple(row or ()))
        return result
