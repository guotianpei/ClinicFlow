"""The synthetic runtime's database side (Option B B3a, R-B3a-3/-4). SYNTHETIC ONLY.

`SyntheticRuntime` owns, in this order (`open()`), and releases in reverse (`close()`):
1. the shared K_S `lease` on its own connection (application_name `<app>/lease`);
2. A1: the installer's read-only ready reconciliation against the EXACT attested reservation
   (full B2 seal before the first start, reduced seal after it; no takeover, no epoch bump);
3. the first-start marker `runtime.first_epoch` (one-way, written under the state lock while
   the lease is held, BEFORE any pool opens: v1r2 D1.3a/b);
4. the runtime `pool`: `TenantPool(connection_class=GuardedRuntimeConnection)` bound to the
   attested reservation (every outermost transaction runs the shared F2 guard; nothing else
   reaches the server outside a guard: v1r1 D2.2);
5. the composed `tenant_runtime` (synthetic registry and catalogue, target 94, activation
   enforced) whose migrator/provisioner factories refuse RUNTIME_NO_DDL (D2.4).
The lifecycle drives these steps one by one (with its hook points); `open()` runs them all.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from typing import Any, Final

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo

from haloflow.m01.activation import ActivationPolicy
from haloflow.m01.control_store import PsycopgControlStore
from haloflow.m01.pool import TenantPool
from haloflow.m01.runtime import (
    TenantRuntime,
    TenantRuntimeDependencies,
    compose_tenant_runtime,
)
from haloflow.synthetic import profiles
from haloflow.synthetic.attest import admin_connect
from haloflow.synthetic.dsn import AdminEndpoint
from haloflow.synthetic.errors import SyntheticRefused
from haloflow.synthetic.guard import (
    K_S,
    cluster_identity,
    parse_reservation,
    read_reservation,
    reservation_value,
    runtime_connection_class,
)
from haloflow.synthetic.hooks import fire
from haloflow.synthetic.state import StateDir

LEASE_SUFFIX: Final = "/lease"
POOL_MIN: Final = 1
POOL_MAX: Final = 6
LEASE_CHECK_TIMEOUT: Final = "5s"
LEASE_CONNECT_TIMEOUT_S: Final = 5


class SyntheticLease:
    """The runtime's SHARED K_S session lock (B2 `RuntimeLease` semantics) on a dedicated,
    named admin connection; `check()` is the watcher's bounded liveness query."""

    def __init__(self, endpoint: AdminEndpoint, application_name: str) -> None:
        self._endpoint = endpoint
        self.application_name = application_name
        self._conn: psycopg.Connection[Any] | None = None
        self.pid: int | None = None

    def acquire(self) -> None:
        conn = admin_connect(self._endpoint, "postgres", application_name=self.application_name,
                             connect_timeout=LEASE_CONNECT_TIMEOUT_S)
        try:
            conn.execute(f"SET statement_timeout = '{LEASE_CHECK_TIMEOUT}'")
            row = conn.execute("SELECT pg_catalog.pg_try_advisory_lock_shared(%s)",
                               (K_S,)).fetchone()
        except BaseException:
            conn.close()
            raise
        if row is None or row[0] is not True:
            conn.close()
            raise SyntheticRefused("INSTALL_LOCKED")
        self._conn = conn
        self.pid = conn.info.backend_pid
        fire("b3.lease.acquired", {})    # K_S held, not yet published to the runtime

    def check(self) -> None:
        conn = self._conn
        try:
            if conn is None:
                raise SyntheticRefused("RUNTIME_LEASE_LOST")
            conn.execute("SELECT 1")
        except psycopg.Error:
            raise SyntheticRefused("RUNTIME_LEASE_LOST") from None

    def release(self) -> None:
        """Unlock the shared K_S explicitly, THEN close (B2 `_release_session_lock`)."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            with contextlib.suppress(psycopg.Error):
                conn.execute("SELECT pg_catalog.pg_advisory_unlock_shared(%s)", (K_S,))
        finally:
            conn.close()

    def close_client(self) -> None:
        """Lease lost: close the client side only (the backend is handled by cleanup)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def _refusing_factory() -> Callable[[], Any]:
    async def connect() -> AsyncConnection[Any]:
        raise SyntheticRefused("RUNTIME_NO_DDL")

    return connect


def installed_target(endpoint: AdminEndpoint, state: Mapping[str, Any]) -> int | None:
    """The installed registry target of the recorded tenant (read-only admin read; E4)."""
    with admin_connect(endpoint, endpoint.target) as conn:
        row = conn.execute("SELECT schema_version FROM shared.tenants WHERE tenant_id = %s",
                           (state["tenant_id"],)).fetchone()
    return None if row is None else int(row[0])


class SyntheticRuntime:
    def __init__(self, endpoint: AdminEndpoint, state: Mapping[str, Any], *,
                 application_name: str, state_dir: str | None = None,
                 on_refused: Callable[[str], None] | None = None,
                 profile: str = "default") -> None:
        self.endpoint = endpoint
        self.profile = profile
        self.state: dict[str, Any] = dict(state)
        self.application_name = application_name
        self._state_dir = state_dir
        self._on_refused = on_refused
        self.expected: str | None = None
        self.lease: SyntheticLease | None = None
        self.pool: TenantPool | None = None
        self.dependencies: TenantRuntimeDependencies | None = None
        self.tenant_runtime: TenantRuntime | None = None
        self.catalog: Any = None

    # -- the steps (driven one by one by the lifecycle) ------------------------------------

    def read_reservation(self) -> str:
        """The attested reservation: the recorded cluster/target identity and nonce, at the
        epoch current when the state is read (an installer takeover after this is fenced)."""
        sysid, oid = cluster_identity(self.endpoint)
        if self.state["cluster_sysid"] != sysid:
            raise SyntheticRefused("CLUSTER_MISMATCH")
        if self.state["target"] != self.endpoint.target or self.state["target_oid"] != oid:
            raise SyntheticRefused("TARGET_MISMATCH")
        # E4: after the cluster / target identity, before the lease, any pool or the marker:
        # the installed registry target must be the recorded install profile's.
        if installed_target(self.endpoint, self.state) != profiles.TARGETS[self.profile]:
            raise SyntheticRefused("PROFILE_TARGET_MISMATCH")
        current = read_reservation(self.endpoint)
        parsed = None if current is None else parse_reservation(current)
        if current is None or parsed is None:
            raise SyntheticRefused("NOT_RESERVED")
        if current != reservation_value(self.state["nonce"], sysid, oid, parsed[3]):
            raise SyntheticRefused("RESERVED_BY_OTHER")
        self.expected = current
        return current

    def acquire_lease(self) -> None:
        lease = SyntheticLease(self.endpoint, self.application_name + LEASE_SUFFIX)
        lease.acquire()
        self.lease = lease

    def attest(self) -> None:
        from haloflow.synthetic.provision import attest_ready

        assert self.expected is not None
        attest_ready(self.endpoint, self.state, self.expected)

    @property
    def epoch(self) -> str:
        assert self.expected is not None
        parsed = parse_reservation(self.expected)
        assert parsed is not None
        return parsed[3]

    def needs_marker(self) -> bool:
        return "runtime" not in self.state

    def write_marker(self) -> None:
        """v1r2 D1.3b: one atomic state write (temp, fsync, replace, dir fsync) under the B2
        state lock, after A1 on the full seal, while the lease is held."""
        from haloflow.synthetic.provision import check_state

        assert self._state_dir is not None
        with StateDir(self._state_dir).locked() as st:
            current = st.read()
            if current is None:
                raise SyntheticRefused("STATE_MISSING")
            check_state(current)
            if current != self.state:
                raise SyntheticRefused("STATE_DIVERGED", ("state_changed",))
            marked = dict(current)
            marked["runtime"] = {"first_epoch": self.epoch}
            st.write(marked)
        self.state = marked

    async def open_pool(self) -> None:
        assert self.expected is not None
        login = self.state["logins"]["runtime"]
        conninfo = make_conninfo(self.endpoint.conninfo(self.endpoint.target),
                                 user=login["role"], password=login["password"],
                                 application_name=self.application_name)
        pool = TenantPool(conninfo, min_size=POOL_MIN, max_size=POOL_MAX,
                          name="haloflow-synthetic-runtime",
                          connection_class=runtime_connection_class(self.expected,
                                                                    self._on_refused))
        self.pool = pool
        await pool.open()

    def compose(self) -> TenantRuntime:
        assert self.pool is not None
        self.dependencies = TenantRuntimeDependencies(
            pool=self.pool, control_store=PsycopgControlStore(self.pool),
            migrator_connect=_refusing_factory(), provisioner_connect=_refusing_factory())
        self.catalog = profiles.catalog(self.profile)     # E4: the composed catalogue (views)
        self.tenant_runtime = compose_tenant_runtime(
            self.dependencies, registry=profiles.registry(self.profile),
            catalog=self.catalog,
            supported_schema_versions=frozenset({profiles.TARGETS[self.profile]}),
            activation_policy=ActivationPolicy.ENFORCE)
        return self.tenant_runtime

    # -- whole sequence (direct use) -------------------------------------------------------

    async def open(self) -> None:
        await asyncio.to_thread(self.read_reservation)
        await asyncio.to_thread(self.acquire_lease)
        try:
            await asyncio.to_thread(self.attest)
            if self.needs_marker():
                await asyncio.to_thread(self.write_marker)
            await self.open_pool()
            self.compose()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        if self.lease is not None:
            await asyncio.to_thread(self.lease.release)
            self.lease = None
