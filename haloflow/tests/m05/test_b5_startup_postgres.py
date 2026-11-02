"""UC1 part B, tranche B5 — trusted startup, admission, sanitized errors,
compatibility (PostgreSQL 17).

Package v2 + v2r1, aligned by Codex 4b0a5448 (codex_b5-v2r1-alignment.md).

- B5-S: startup over real tenants provisioned with the PRODUCTION registry.
  - Every refusal row puts the bad tenant first and then last.
  - Each asserts the reason code, the tenant index, `__cause__` and `__context__`
    both None, canary-free text and logs, zero tenant-gateway connections, and no
    leftover startup backend.
- B5-S15a/b (SU-SNAPSHOT barrier): a commit lands during the private per-tenant
  check, after the tenants-row read. A generation-row change (a) and a
  comparator-visible change (b) are pinned as separate rows (Codex 4b0a5448:
  measure catalogue visibility; do not combine).
- B5-A: admission on the gateway path:
  - an independently obtained context for an unserved tenant;
  - a stale context after a version bump (the observed gateway code is pinned);
  - the facade refuses before any backing-resolver I/O;
  - bundle introspection.
- B5-E: cleanup failure with no prior failure, a non-idle connection, a factory
  failure and cancellation.
- COMPAT-B5a is observation only, on a new disposable fixture. Nothing it
  observes is approved behaviour. COMPAT-B5b is version 11 refused (the
  S-version row).
- P-4: provisioning resume guard. Mismatched target refused before writes, with
  full ledger and catalogue equality; same-target resume is the control.

Fixtures are new, disposable synthetic tenants, reset before and after. No
retained database is reused. The `haloflow_test` prefix is a guard, not an
authorization.

Open dependency: Codex ruling 0be43eba. The migrator cannot read shared.tenants
today. These rows assert behaviour that holds under either option (a) or (b).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import b5_support as b5
import m02_support as m02
import m05_support as m05
import pytest
from psycopg import AsyncConnection

pytestmark = pytest.mark.postgres

STARTUP_APP = "haloflow-b5-startup"
CANARY_DRIVER = "CANARY-DRIVER-TEXT-7f3e"


# ---------------------------------------------------------------------------
# Production-registry tenants (installed lazily, in each row's body)
# ---------------------------------------------------------------------------


class ProductionInstall:
    def __init__(self, ids: m02.Identities, tenant: tuple[str, str]) -> None:
        self.ids = ids
        self.tenant = tenant
        self._error: Exception | None = None
        self._done = False

    @property
    def tenant_id(self) -> str:
        return self.tenant[0]

    @property
    def schema(self) -> str:
        return self.tenant[1]

    def require(self) -> None:
        if not self._done:
            self._done = True
            try:
                m02.provision_sync(
                    self.ids, m02.production_registry(), self.tenant,
                    supported=range(b5.PRODUCTION_TARGET, b5.PRODUCTION_TARGET + 1),
                )
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"production installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def _fixture(label: str) -> Callable[..., Iterator[ProductionInstall]]:
    @pytest.fixture(scope="module")
    def fixture(m02_ids: m02.Identities, m05_reset: Any) -> Iterator[ProductionInstall]:
        tenant = m02.tenant_for(f"b5:startup:{label}")
        m05_reset(tenant)
        try:
            yield ProductionInstall(m02_ids, tenant)
        finally:
            m05_reset(tenant)

    return fixture


tenant_a = _fixture("a")
tenant_b = _fixture("b")
tenant_c = _fixture("c-unserved")


def _admin(ids: m02.Identities, statement: str, params: Any = None) -> None:
    with m02.connect_admin(ids) as conn:
        conn.execute(statement, params)


# ---------------------------------------------------------------------------
# Dependencies with spies
# ---------------------------------------------------------------------------


def _tagged(conninfo: str, tag: str) -> str:
    return f"{conninfo} application_name={tag}"


class CountingPool:
    """A TenantPool whose gateway connections are counted."""

    def __init__(self, login: str) -> None:
        from haloflow.m01.pool import TenantPool

        self.gateway_connections = 0
        outer = self

        class _Pool(TenantPool):
            @asynccontextmanager
            async def _connection_for_gateway(self) -> AsyncIterator[Any]:
                outer.gateway_connections += 1
                async with super()._connection_for_gateway() as connection:
                    yield connection

        self.pool = _Pool(login, min_size=1, max_size=2)


class CountingStore:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0

    async def get_tenant(self, tenant_id: str) -> Any:
        self.calls += 1
        return await self.inner.get_tenant(tenant_id)


def _migrator_factory(ids: m02.Identities, *, wrap: Any = None) -> Any:
    conninfo = _tagged(ids.logins["haloflow_migrator"], STARTUP_APP)

    async def connect() -> AsyncConnection[Any]:
        connection = await AsyncConnection.connect(conninfo, autocommit=True)
        return connection if wrap is None else await wrap(connection)

    return connect


@asynccontextmanager
async def _deps(ids: m02.Identities, *, migrator: Any = None) -> AsyncIterator[Any]:
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.runtime import TenantRuntimeDependencies

    counting = CountingPool(ids.logins["haloflow_runtime"])
    await counting.pool.open()
    store = CountingStore(PsycopgControlStore(counting.pool))
    provisioner = ids.logins["haloflow_provisioner"]

    async def provisioner_connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(provisioner, autocommit=True)

    try:
        deps = TenantRuntimeDependencies(
            pool=counting.pool,
            control_store=store,
            migrator_connect=migrator or _migrator_factory(ids),
            provisioner_connect=provisioner_connect,
        )
        yield deps, counting, store
    finally:
        await counting.pool.close()


def _leftover_startup_backends(ids: m02.Identities) -> int:
    rows = m02.admin_all(
        ids, "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (STARTUP_APP,)
    )
    return int(rows[0][0])


async def _settle(ids: m02.Identities) -> int:
    for _ in range(50):
        left = await asyncio.to_thread(_leftover_startup_backends, ids)
        if left == 0:
            return 0
        await asyncio.sleep(0.1)
    return await asyncio.to_thread(_leftover_startup_backends, ids)


async def _context(services_or_resolver: Any, tenant_id: str, caps: frozenset[str]) -> Any:
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource

    principal = str(uuid5(NAMESPACE_URL, "haloflow-test:b5:principal"))
    return await services_or_resolver.resolve(
        principal=Principal(kind=PrincipalKind("actor"), id=principal, auth_method="test",
                            authorized_tenant_ids=frozenset({tenant_id}), capabilities=caps),
        tenant_hint=tenant_id, purpose="operations", capabilities=caps,
        source=TrustedSource.HTTP, execution_id=uuid5(NAMESPACE_URL, f"b5:{uuid.uuid4()}"),
        correlation_id=uuid.uuid4(), correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
        request_id=None,
    )


READ_CAPS = frozenset({"m05.followup.read"})


# ---------------------------------------------------------------------------
# B5-S01 happy path (two tenants)
# ---------------------------------------------------------------------------


def test_b5_s01_two_valid_tenants_start_and_serve(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, tenant_b: ProductionInstall
) -> None:
    from haloflow.m05.service import ReadRefused
    from haloflow.startup import Deployment, ProductionServices, start_production_services

    tenant_a.require()
    tenant_b.require()

    async def run() -> None:
        async with _deps(m02_ids) as (deps, counting, _store):
            services = await start_production_services(
                deps, deployment=Deployment.PRODUCTION,
                served_tenants=(tenant_a.tenant_id, tenant_b.tenant_id),
            )
            assert type(services) is ProductionServices
            assert await _settle(m02_ids) == 0  # startup connections closed
            assert counting.gateway_connections == 0  # nothing served yet
            for install in (tenant_a, tenant_b):
                ctx = await _context(services.resolver, install.tenant_id, READ_CAPS)
                assert ctx.tenant_id == install.tenant_id
                with pytest.raises(ReadRefused):  # the synthetic principal is not staff
                    await services.m05.get(ctx, uuid.uuid4())
            assert counting.gateway_connections == 2

    asyncio.run(run())


# ---------------------------------------------------------------------------
# B5-S02..S10 one refusal each, bad tenant first and last
# ---------------------------------------------------------------------------

# (kind, break SQL, restore SQL, expected reason). {s} = schema, {t} = tenant id.
BREAKS: dict[str, tuple[str, str, str]] = {
    "version-11": (
        "UPDATE shared.tenants SET schema_version = 11 WHERE tenant_id = '{t}'",
        "UPDATE shared.tenants SET schema_version = 25 WHERE tenant_id = '{t}'",
        "SCHEMA_VERSION_UNSUPPORTED",
    ),
    "suspended": (
        "UPDATE shared.tenants SET lifecycle_state = 'suspended' WHERE tenant_id = '{t}'",
        "UPDATE shared.tenants SET lifecycle_state = 'active' WHERE tenant_id = '{t}'",
        "TENANT_NOT_ACTIVE",
    ),
    "function-config-drift": (
        "ALTER FUNCTION {s}.m05_get_item(uuid) SET work_mem = '64kB'",
        "ALTER FUNCTION {s}.m05_get_item(uuid) RESET work_mem",
        "INSTALLED_STATE_DRIFT",
    ),
    "test-family-active": (
        "UPDATE {s}.event_types SET is_active = true"
        " WHERE event_type = 'm02_test_observation_received'",
        "UPDATE {s}.event_types SET is_active = false"
        " WHERE event_type = 'm02_test_observation_received'",
        "TEST_FAMILY_ACTIVE_IN_PRODUCTION",
    ),
    "synthetic-resolver-active": (
        "UPDATE {s}.m02_link_resolvers SET is_active = true"
        " WHERE resolver_id = 'm02_test_subject_v1'",
        "UPDATE {s}.m02_link_resolvers SET is_active = false"
        " WHERE resolver_id = 'm02_test_subject_v1'",
        "SYNTHETIC_DISPATCH_IN_PRODUCTION",
    ),
    "m05-full-activation": (
        m05.B4_RESOLUTION_PROFILE_SQL.replace("{schema}", "{s}"),
        m05.B4_RESOLUTION_PROFILE_SQL.replace("{schema}", "{s}").replace("true", "false"),
        "M05_ACTIVATION_NOT_AUTHORIZED",
    ),
    "m05-partial-activation": (
        "UPDATE {s}.m02_link_resolvers SET is_active = true"
        " WHERE resolver_id = 'm05_followup_item_v1'",
        "UPDATE {s}.m02_link_resolvers SET is_active = false"
        " WHERE resolver_id = 'm05_followup_item_v1'",
        "ROUTE_NOT_INSTALLED",
    ),
    "m05-resolver-row-missing": (
        "DELETE FROM {s}.m02_link_resolvers WHERE resolver_id = 'm05_followup_item_v1'",
        "INSERT INTO {s}.m02_link_resolvers (resolver_id, relationship, resolver_kind, is_active)"
        " VALUES ('m05_followup_item_v1', 'case', 'module_owned', false)",
        "GENERATION_UNREADABLE",
    ),
}


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("kind", list(BREAKS))
def test_b5_s02_one_bad_tenant_refuses_startup(
    kind: str, position: str, m02_ids: m02.Identities,
    tenant_a: ProductionInstall, tenant_b: ProductionInstall,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from haloflow.startup import Deployment, StartupRefused, start_production_services

    tenant_a.require()
    tenant_b.require()
    bad, good = tenant_a, tenant_b
    served = (bad.tenant_id, good.tenant_id) if position == "first" else (
        good.tenant_id, bad.tenant_id)
    index = served.index(bad.tenant_id)
    broken, restore, reason = BREAKS[kind]
    _admin(m02_ids, broken.format(s=bad.schema, t=bad.tenant_id))
    caplog.set_level(logging.DEBUG)
    try:
        async def run() -> StartupRefused:
            async with _deps(m02_ids) as (deps, counting, _store):
                with pytest.raises(StartupRefused) as caught:
                    await start_production_services(
                        deps, deployment=Deployment.PRODUCTION, served_tenants=served)
                assert counting.gateway_connections == 0
                assert await _settle(m02_ids) == 0
                return caught.value

        refused = asyncio.run(run())
    finally:
        _admin(m02_ids, restore.format(s=bad.schema, t=bad.tenant_id))
    assert refused.reason_code == reason
    assert refused.tenant_index == index
    assert refused.__cause__ is None and refused.__context__ is None
    for text in (str(refused), repr(refused), repr(refused.args), caplog.text):
        assert bad.schema not in text and bad.tenant_id not in text


def test_b5_s03_non_production_accepts_full_m05_activation(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    from haloflow.startup import Deployment, start_production_services

    tenant_a.require()
    profile = m05.B4_RESOLUTION_PROFILE_SQL.replace("{schema}", tenant_a.schema)
    _admin(m02_ids, profile)
    try:
        async def run() -> None:
            async with _deps(m02_ids) as (deps, _counting, _store):
                await start_production_services(
                    deps, deployment=Deployment.NON_PRODUCTION,
                    served_tenants=(tenant_a.tenant_id,))

        asyncio.run(run())
    finally:
        _admin(m02_ids, profile.replace("true", "false"))


def test_b5_s04_served_set_is_captured_once(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, tenant_c: ProductionInstall
) -> None:
    """The caller's list is mutated during the first tenant check. The bundle still
    admits only the originally captured tenant."""

    from haloflow.startup import Deployment, TenantNotServed, start_production_services

    tenant_a.require()
    tenant_c.require()
    served = [tenant_a.tenant_id]

    async def mutate(connection: AsyncConnection[Any]) -> AsyncConnection[Any]:
        served.append(tenant_c.tenant_id)
        return connection

    async def run() -> None:
        async with _deps(m02_ids, migrator=_migrator_factory(m02_ids, wrap=mutate)) as (
            deps, _c, store):
            services = await start_production_services(
                deps, deployment=Deployment.PRODUCTION, served_tenants=served)
            before = store.calls
            with pytest.raises(TenantNotServed):
                await _context(services.resolver, tenant_c.tenant_id, READ_CAPS)
            assert store.calls == before  # refused before backing-resolver I/O

    asyncio.run(run())


# ---------------------------------------------------------------------------
# B5-S15 SU-SNAPSHOT barrier (private checker hook)
# ---------------------------------------------------------------------------

SNAPSHOT_CHANGES = {
    "generation-row": (
        "UPDATE {s}.m02_link_resolvers SET is_active = true"
        " WHERE resolver_id = 'm05_followup_item_v1'",
        "UPDATE {s}.m02_link_resolvers SET is_active = false"
        " WHERE resolver_id = 'm05_followup_item_v1'",
    ),
    "comparator-config": (
        "ALTER FUNCTION {s}.m05_get_item(uuid) SET work_mem = '64kB'",
        "ALTER FUNCTION {s}.m05_get_item(uuid) RESET work_mem",
    ),
}


@pytest.mark.parametrize("change", list(SNAPSHOT_CHANGES))
def test_b5_s15_commit_after_tenant_row_read_is_invisible(
    change: str, m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    """Correct behaviour: one REPEATABLE READ snapshot taken before the commit, so
    the tenant validates. SU-SNAPSHOT (a split read) refuses instead.

    PostgreSQL catalogue visibility (pg_proc under REPEATABLE READ) is MEASURED by
    the comparator-config row, not assumed (Codex 4b0a5448). If that row refuses on
    the correct code, the result is reported, not forced."""

    from haloflow.startup import Deployment, _verify_tenant

    tenant_a.require()
    applied, restore = SNAPSHOT_CHANGES[change]

    async def barrier() -> None:
        await asyncio.to_thread(_admin, m02_ids, applied.format(s=tenant_a.schema))

    async def run() -> tuple[str, str, int]:
        connection = await _migrator_factory(m02_ids)()
        try:
            return await _verify_tenant(
                connection, tenant_id=tenant_a.tenant_id, deployment=Deployment.PRODUCTION,
                after_tenant_row=barrier)
        finally:
            await connection.close()

    try:
        validated = asyncio.run(run())
    finally:
        _admin(m02_ids, restore.format(s=tenant_a.schema))
    assert validated == (tenant_a.tenant_id, tenant_a.schema, b5.PRODUCTION_TARGET)


# ---------------------------------------------------------------------------
# B5-A admission on the gateway path
# ---------------------------------------------------------------------------


async def _independent_context(ids: m02.Identities, tenant_id: str) -> Any:
    """A context from a separately composed production runtime: a valid context
    for an active tenant that the bundle never validated."""

    from haloflow.composition import build_production_tenant_runtime

    async with _deps(ids) as (deps, _c, _s):
        runtime = build_production_tenant_runtime(deps)
        return await _context(runtime.resolver, tenant_id, READ_CAPS)


async def _bundle(ids: m02.Identities, served: tuple[str, ...]) -> Any:
    from haloflow.startup import Deployment, start_production_services

    holder: dict[str, Any] = {}
    cm = _deps(ids)
    deps, counting, store = await cm.__aenter__()
    holder.update(cm=cm, counting=counting, store=store)
    holder["services"] = await start_production_services(
        deps, deployment=Deployment.PRODUCTION, served_tenants=served)
    return holder


def test_b5_a01_independent_context_for_unserved_tenant_refused_on_every_service(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, tenant_c: ProductionInstall
) -> None:
    from haloflow.startup import TenantNotServed

    tenant_a.require()
    tenant_c.require()

    async def run() -> None:
        foreign = await _independent_context(m02_ids, tenant_c.tenant_id)
        held = await _bundle(m02_ids, (tenant_a.tenant_id,))
        try:
            services, counting = held["services"], held["counting"]
            with pytest.raises(TenantNotServed):
                await services.m05.get(foreign, uuid.uuid4())
            with pytest.raises(TenantNotServed):
                await services.staff.display(foreign, (uuid.uuid4(),))
            assert counting.gateway_connections == 0
        finally:
            await held["cm"].__aexit__(None, None, None)

    asyncio.run(run())


def test_b5_a02_facade_refuses_unserved_and_unknown_before_resolver_io(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, tenant_c: ProductionInstall
) -> None:
    from haloflow.startup import TenantNotServed

    tenant_a.require()
    tenant_c.require()

    async def run() -> None:
        held = await _bundle(m02_ids, (tenant_a.tenant_id,))
        try:
            services, store = held["services"], held["store"]
            for tenant_id in (tenant_c.tenant_id, "clinic-unknown-b5"):
                before = store.calls
                with pytest.raises(TenantNotServed):
                    await _context(services.resolver, tenant_id, READ_CAPS)
                assert store.calls == before
        finally:
            await held["cm"].__aexit__(None, None, None)

    asyncio.run(run())


def test_b5_a03_held_context_after_version_bump_observed_gateway_refusal(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    """The admission tuple (tenant_id, schema_key) still matches. The existing
    gateway re-reads shared.tenants every transaction. Statically, at f9d90e1,
    gateway.py:213-234 refuses an unsupported version with RegistryInconsistent
    REGISTRY_REVALIDATION_MISMATCH. This row pins that observed behaviour."""

    from haloflow.m01.errors import RegistryInconsistent

    tenant_a.require()

    async def run() -> None:
        held = await _bundle(m02_ids, (tenant_a.tenant_id,))
        try:
            services = held["services"]
            ctx = await _context(services.resolver, tenant_a.tenant_id, READ_CAPS)
            await asyncio.to_thread(
                _admin, m02_ids, BREAKS["version-11"][0].format(t=tenant_a.tenant_id))
            try:
                with pytest.raises(RegistryInconsistent) as caught:
                    await services.m05.get(ctx, uuid.uuid4())
                assert caught.value.reason_code == "REGISTRY_REVALIDATION_MISMATCH"
            finally:
                await asyncio.to_thread(
                    _admin, m02_ids, BREAKS["version-11"][1].format(t=tenant_a.tenant_id))
        finally:
            await held["cm"].__aexit__(None, None, None)

    asyncio.run(run())


def test_b5_a04_bundle_exposes_only_the_facade_and_admitted_services(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    import dataclasses

    from haloflow.m01.resolver import TenantResolver
    from haloflow.m01.staff.service import StaffService
    from haloflow.m05.service import M05Service
    from haloflow.startup import ServedTenantGateway, ServedTenantResolver

    tenant_a.require()

    async def run() -> None:
        held = await _bundle(m02_ids, (tenant_a.tenant_id,))
        try:
            services = held["services"]
            assert [f.name for f in dataclasses.fields(services)] == ["m05", "staff", "resolver"]
            assert type(services.m05) is M05Service
            assert type(services.staff) is StaffService
            assert type(services.resolver) is ServedTenantResolver
            assert not isinstance(services.resolver, TenantResolver)
            assert type(services.m05._gateway) is ServedTenantGateway
            assert type(services.staff._gateway) is ServedTenantGateway
            foundation = services.m05._foundation
            assert type(foundation._EventFoundation__gateway) is ServedTenantGateway
        finally:
            await held["cm"].__aexit__(None, None, None)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# B5-E sanitized connection failures, cleanup and cancellation
# ---------------------------------------------------------------------------


def _refused_by(m02_ids: m02.Identities, served: tuple[str, ...], migrator: Any) -> Any:
    from haloflow.startup import Deployment, StartupRefused, start_production_services

    async def run() -> Any:
        async with _deps(m02_ids, migrator=migrator) as (deps, counting, _s):
            with pytest.raises(StartupRefused) as caught:
                await start_production_services(
                    deps, deployment=Deployment.PRODUCTION, served_tenants=served)
            assert counting.gateway_connections == 0
            assert await _settle(m02_ids) == 0
            return caught.value

    return asyncio.run(run())


def _check_sanitized(refused: Any, *forbidden: str) -> None:
    assert refused.__cause__ is None and refused.__context__ is None
    for text in (str(refused), repr(refused)):
        for value in forbidden:
            assert value not in text


def test_b5_e01_cleanup_failure_without_prior_failure_refuses(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    tenant_a.require()

    async def failing_close(connection: AsyncConnection[Any]) -> AsyncConnection[Any]:
        real_close = connection.close

        async def close() -> None:
            await real_close()
            raise RuntimeError(CANARY_DRIVER)

        connection.close = close  # type: ignore[method-assign]
        return connection

    refused = _refused_by(m02_ids, (tenant_a.tenant_id,),
                          _migrator_factory(m02_ids, wrap=failing_close))
    assert refused.reason_code == "STARTUP_CONNECTION_FAILED"
    _check_sanitized(refused, CANARY_DRIVER, tenant_a.schema)


def test_b5_e02_non_idle_connection_refused_not_adopted(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    tenant_a.require()

    async def in_transaction(connection: AsyncConnection[Any]) -> AsyncConnection[Any]:
        await connection.set_autocommit(False)
        await connection.execute("SELECT 1")
        return connection

    refused = _refused_by(m02_ids, (tenant_a.tenant_id,),
                          _migrator_factory(m02_ids, wrap=in_transaction))
    assert refused.reason_code == "STARTUP_CONNECTION_FAILED"
    _check_sanitized(refused, tenant_a.schema)


def test_b5_e03_factory_failure_is_sanitized(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    import psycopg

    tenant_a.require()

    async def failing() -> AsyncConnection[Any]:
        raise psycopg.OperationalError(CANARY_DRIVER)

    refused = _refused_by(m02_ids, (tenant_a.tenant_id,), failing)
    assert refused.reason_code == "STARTUP_CONNECTION_FAILED"
    _check_sanitized(refused, CANARY_DRIVER)


def test_b5_e04_cancellation_propagates_and_cleans_up(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    """An admin ACCESS EXCLUSIVE lock on a generation table blocks the startup read.
    The task is cancelled: CancelledError propagates unchanged, and no startup
    backend is left."""

    import psycopg

    from haloflow.startup import Deployment, start_production_services

    tenant_a.require()

    async def run() -> None:
        locker = await psycopg.AsyncConnection.connect(m02_ids.admin)
        try:
            await locker.execute(
                f"LOCK TABLE {tenant_a.schema}.event_types IN ACCESS EXCLUSIVE MODE")
            async with _deps(m02_ids) as (deps, _c, _s):
                task = asyncio.ensure_future(start_production_services(
                    deps, deployment=Deployment.PRODUCTION,
                    served_tenants=(tenant_a.tenant_id,)))
                for _ in range(100):
                    if await asyncio.to_thread(_leftover_startup_backends, m02_ids):
                        break
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            await locker.rollback()
            await locker.close()
        assert await _settle(m02_ids) == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# COMPAT-B5a (observation only) and P-4 resume guard
# ---------------------------------------------------------------------------

LEDGER = (
    "SELECT migration_id, checksum, state FROM shared.schema_migrations"
    " WHERE tenant_id = %s ORDER BY migration_id"
)
CATALOGUE = (
    "SELECT c.relname, c.relkind, c.relacl::text FROM pg_class c"
    " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s"
    " UNION ALL SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',"
    " 'f', p.proacl::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = %s ORDER BY 1"
)
TENANT_ROW = (
    "SELECT tenant_id, schema_key, lifecycle_state, schema_version FROM shared.tenants"
    " WHERE tenant_id = %s"
)


def _part_a_registry() -> Any:
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
    from haloflow.m02.part_a_gateways import PART_A_GATEWAY_PROFILES
    from haloflow.m02.units import M02_TENANT_MIGRATIONS, T003_MIGRATION_ID

    return build_tenant_migration_registry(
        TENANT_MIGRATIONS, M02_TENANT_MIGRATIONS,
        approved_execution_roles=b5.M02_ROLES,
        installed_state_profiles={T003_MIGRATION_ID: LOCK_OPERATION_PROFILE,
                                  **PART_A_GATEWAY_PROFILES},
    )


def test_compat_b5a_runner_over_an_11_unit_ledger_is_observed_only(
    m02_ids: m02.Identities, m05_reset: Any
) -> None:
    """OBSERVATION ONLY (Codex 6abf4d64 and 4b0a5448). A new disposable tenant is
    installed with the 11-unit Part A registry, then the 25-unit production runner
    is applied directly. The row records what happens. It approves nothing, and no
    B5 behaviour relies on it."""

    from haloflow.m01.provisioning import MIGRATOR_ROLE, TenantMigrationRunner

    tenant = m02.tenant_for("b5:compat-b5a:observe")
    m05_reset(tenant)
    try:
        m02.provision_sync(m02_ids, _part_a_registry(), tenant, supported=range(11, 12))
        before = m02.admin_all(m02_ids, LEDGER, (tenant[0],))

        async def apply() -> Any:
            runner = TenantMigrationRunner(
                m02._connection_factory(m02_ids.logins[MIGRATOR_ROLE]),
                m02.production_registry())
            return await runner.apply(tenant_id=tenant[0], schema_key=tenant[1])

        try:
            outcome: Any = asyncio.run(apply())
            observed = f"returned {len(outcome)} outcomes"
        except Exception as error:  # noqa: BLE001 - observation only
            observed = f"raised {type(error).__name__} {getattr(error, 'reason_code', '')}"
        after = m02.admin_all(m02_ids, LEDGER, (tenant[0],))
        print(f"COMPAT-B5a observed: {observed}; ledger rows {len(before)} -> {len(after)}")
        assert before  # the fixture was installed; the observation itself is not judged
    finally:
        m05_reset(tenant)


def _provision_production(ids: m02.Identities, tenant: tuple[str, str]) -> Any:
    return m02.provision_sync(ids, m02.production_registry(), tenant,
                              supported=range(b5.PRODUCTION_TARGET, b5.PRODUCTION_TARGET + 1))


def test_p4_mismatched_target_resume_refused_before_writes(
    m02_ids: m02.Identities, m05_reset: Any
) -> None:
    from haloflow.m01.errors import ProvisioningFailed

    tenant = m02.tenant_for("b5:p4:mismatch")
    m05_reset(tenant)
    try:
        _admin(m02_ids,
               "INSERT INTO shared.tenants (tenant_id, schema_key, lifecycle_state,"
               " schema_version) VALUES (%s, %s, 'provisioning', 11)", tenant)
        snapshot = (m02.admin_all(m02_ids, TENANT_ROW, (tenant[0],)),
                    m02.admin_all(m02_ids, LEDGER, (tenant[0],)),
                    m02.admin_all(m02_ids, CATALOGUE, (tenant[1], tenant[1])))
        with pytest.raises(ProvisioningFailed) as caught:
            _provision_production(m02_ids, tenant)
        assert caught.value.reason_code == "TENANT_NOT_RESUMABLE"
        assert (m02.admin_all(m02_ids, TENANT_ROW, (tenant[0],)),
                m02.admin_all(m02_ids, LEDGER, (tenant[0],)),
                m02.admin_all(m02_ids, CATALOGUE, (tenant[1], tenant[1]))) == snapshot
    finally:
        m05_reset(tenant)


def test_p4_control_same_target_interrupted_provisioning_resumes(
    m02_ids: m02.Identities, m05_reset: Any
) -> None:
    tenant = m02.tenant_for("b5:p4:same-target")
    m05_reset(tenant)
    try:
        _admin(m02_ids,
               "INSERT INTO shared.tenants (tenant_id, schema_key, lifecycle_state,"
               " schema_version) VALUES (%s, %s, 'provisioning', %s)",
               (*tenant, b5.PRODUCTION_TARGET))
        _provision_production(m02_ids, tenant)
        (row,) = m02.admin_all(m02_ids, TENANT_ROW, (tenant[0],))
        assert row == (tenant[0], tenant[1], "active", b5.PRODUCTION_TARGET)
    finally:
        m05_reset(tenant)


def test_p4_active_retained_tenant_refused_before_writes(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    from haloflow.m01.errors import ProvisioningFailed

    tenant_a.require()
    snapshot = (m02.admin_all(m02_ids, TENANT_ROW, (tenant_a.tenant_id,)),
                m02.admin_all(m02_ids, LEDGER, (tenant_a.tenant_id,)),
                m02.admin_all(m02_ids, CATALOGUE, (tenant_a.schema, tenant_a.schema)))
    with pytest.raises(ProvisioningFailed) as caught:
        _provision_production(m02_ids, tenant_a.tenant)
    assert caught.value.reason_code == "TENANT_NOT_RESUMABLE"
    assert (m02.admin_all(m02_ids, TENANT_ROW, (tenant_a.tenant_id,)),
            m02.admin_all(m02_ids, LEDGER, (tenant_a.tenant_id,)),
            m02.admin_all(m02_ids, CATALOGUE, (tenant_a.schema, tenant_a.schema))) == snapshot
