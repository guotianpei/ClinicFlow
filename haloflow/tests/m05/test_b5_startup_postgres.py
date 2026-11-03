"""UC1 part B, tranche B5 — trusted startup, admission, sanitized errors,
compatibility (PostgreSQL 17).

Package v2 + v2r1, aligned by Codex 4b0a5448 (codex_b5-v2r1-alignment.md), revised
for the red-tranche review REVISE (R1-R5).

PINNED IMPLEMENTATION CONSTRAINTS (test seams, R1):
- `haloflow.startup` constructs `M05Service`, `StaffService` and `EventFoundation`
  through MODULE-LEVEL names of those exact spellings, looked up at call time (for
  example `from haloflow.m05.service import M05Service` at module top, then
  `M05Service(...)` inside the function). The rows replace those names with
  counting wrappers that return the real instances. B5-S01 requires exactly one
  construction of each, so a local import or another spelling fails S01 rather
  than silently counting zero.
- The static composition check reads `haloflow.composition.
  APPROVED_SUPPORTED_SCHEMA_VERSIONS` at call time (directly, or through the
  composition functions, which already do). B5-S05 patches that module attribute.
- The startup migrator connection keeps the `application_name` given in the
  factory's conninfo (B5-E04 finds the startup backend by it).

Rows:
- B5-S01: two valid tenants start; exactly one M05Service, StaffService and
  EventFoundation are constructed, and they are the bundle's objects.
- B5-S02: one refusal per kind, the bad tenant first and then last. Every break has
  a positive control: DML asserts its exact rowcount (each WHERE clause names the
  value being changed, so rowcount 1 proves a real change); DDL asserts the
  catalogue value before and after. Kinds: version-11, suspended,
  function-config-drift, test-family-active, synthetic-resolver-active,
  m05-full-activation, m05-partial-activation, m05-resolver-row-missing and, each
  on its OWN new disposable tenant per row (R5), m05-family-row-missing,
  m05-type-row-missing, test-intake-present.
- Every refusal row asserts: the reason code and tenant index; `__cause__` and
  `__context__` None; no tenant id, schema key or driver text in str, repr, args,
  every value of vars(exc) or the captured log; zero tenant-gateway connections;
  zero control-store calls; ZERO constructions of M05Service, StaffService and
  EventFoundation (R1); no leftover startup backend.
- B5-S03 non-production accepts full M05 activation. B5-S04 the served set is
  captured once. B5-S05 (R1) a static composition failure: COMPOSITION_INVALID,
  tenant index None, zero migrator connections, zero constructions.
- B5-S15a/b (SU-SNAPSHOT barrier): a commit lands during the private per-tenant
  check, after the tenants-row read. A generation-row change (a) and a
  comparator-visible change (b) are separate rows (Codex 4b0a5448: measure
  catalogue visibility; do not combine).
- B5-A (R2): admission on the gateway path, with SUFFICIENT capabilities so that
  admission is the refusal. Every M05 route (get, history, list_items, act with a
  StaffAction, act with a ResolveAction, read_note) and every staff route (admin,
  display) refuses TenantNotServed with zero gateway acquisitions and zero
  control-store calls, for three foreign contexts: an unserved real tenant (an
  independent production runtime), and two forged through an independent
  TenantResolver over a fake ControlStore: a served tenant id with another
  tenant's schema key, and a tenant id that is not in shared.tenants. Positive
  control: the same calls with a served context reach the gateway once each.
  A02 the facade refuses before resolver I/O; A03 a stale held context after a
  version bump hits the observed gateway refusal; A04 bundle introspection (not
  the proof).
- B5-E (R3), one mapping source per row: registry (S02 version-11, suspended),
  comparator (S02 function-config-drift), generation validation (the other S02
  kinds), static (S05), query-time driver error (E05: a renamed generation column
  makes the snapshot SELECT itself fail), connection (E02 non-idle, E03 factory),
  cleanup (E01 cleanup failure with no prior failure; E06 cleanup failure after a
  refusal keeps the ORIGINAL reason). Cancellation: E04 a blocked read, with the
  startup backend proven blocked by the locker (pg_blocking_pids) before the
  cancel; E07 cancellation that arrives during the cleanup of a recorded refusal
  wins, and the real connection is still closed.
- COMPAT-B5a is observation only, on a new disposable fixture. Nothing it
  observes is approved behaviour. COMPAT-B5b is version 11 refused (the S-version
  row).
- P-4 (R4): provisioning resume guard. A POPULATED interrupted fixture (the Part A
  11-unit install, then set back to `provisioning`) is refused for the 25-unit
  target before any write, with the complete tenants row, state history, ledger
  (all columns) and pg_namespace / pg_class / pg_attribute / pg_proc state equal
  before and after. Same-target resume is the control. The active retained tenant
  row uses the same full snapshot.

Fixtures are new, disposable synthetic tenants, reset before and after. No
retained database is reused. The `haloflow_test` prefix is a guard, not an
authorization. Every wait polls a condition under a deadline.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import b5_support as b5
import m02_support as m02
import m05_support as m05
import pytest
import service_support as sv
from psycopg import AsyncConnection

pytestmark = pytest.mark.postgres

STARTUP_APP = "haloflow-b5-startup"
CANARY_DRIVER = "CANARY-DRIVER-TEXT-7f3e"
DEADLINE = 20.0  # seconds, for every bounded wait in this module
UNKNOWN_TENANT = "clinic-unknown-b5"
UNKNOWN_SCHEMA = "tenant_b5unknown0"


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


def _provision_production(ids: m02.Identities, tenant: tuple[str, str]) -> Any:
    return m02.provision_sync(ids, m02.production_registry(), tenant,
                              supported=range(b5.PRODUCTION_TARGET, b5.PRODUCTION_TARGET + 1))


# ---------------------------------------------------------------------------
# Constructor spies (R1)
# ---------------------------------------------------------------------------

SPIED = ("M05Service", "StaffService", "EventFoundation")


class Constructions:
    """Replaces the three names in the `haloflow.startup` namespace with counting
    wrappers that return the REAL instances (so type checks on the bundle hold)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import haloflow.startup as startup
        from haloflow.m01.staff.service import StaffService
        from haloflow.m02.observation import EventFoundation
        from haloflow.m05.service import M05Service

        self.instances: dict[str, list[Any]] = {name: [] for name in SPIED}
        for name, real in zip(SPIED, (M05Service, StaffService, EventFoundation), strict=True):
            # raising=False: the red stub does not define the names yet; S01 is the
            # guard that the implementation really constructs through them.
            monkeypatch.setattr(startup, name, self._counting(name, real), raising=False)

    def _counting(self, name: str, real: type) -> Callable[..., Any]:
        instances = self.instances[name]

        def construct(*args: Any, **kwargs: Any) -> Any:
            instance = real(*args, **kwargs)
            instances.append(instance)
            return instance

        return construct

    def counts(self) -> dict[str, int]:
        return {name: len(found) for name, found in self.instances.items()}

    def assert_none(self) -> None:
        assert self.counts() == dict.fromkeys(SPIED, 0)

    def assert_once(self) -> None:
        assert self.counts() == dict.fromkeys(SPIED, 1)


@pytest.fixture
def constructions(monkeypatch: pytest.MonkeyPatch) -> Constructions:
    return Constructions(monkeypatch)


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


class Calls:
    def __init__(self) -> None:
        self.n = 0


def _migrator_factory(ids: m02.Identities, *, wrap: Any = None, calls: Calls | None = None) -> Any:
    conninfo = _tagged(ids.logins["haloflow_migrator"], STARTUP_APP)

    async def connect() -> AsyncConnection[Any]:
        if calls is not None:
            calls.n += 1
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


def _startup_backends(ids: m02.Identities) -> list[int]:
    rows = m02.admin_all(
        ids, "SELECT pid FROM pg_catalog.pg_stat_activity WHERE application_name = %s"
        " ORDER BY pid", (STARTUP_APP,)
    )
    return [int(row[0]) for row in rows]


async def _settle(ids: m02.Identities) -> int:
    """0 once no startup backend is left; polled under the deadline."""

    deadline = time.monotonic() + DEADLINE
    while True:
        left = len(await asyncio.to_thread(_startup_backends, ids))
        if left == 0 or time.monotonic() >= deadline:
            return left
        await asyncio.sleep(0.05)


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
# Refusal oracle (R1, R3)
# ---------------------------------------------------------------------------


def _check_sanitized(refused: Any, log_text: str, *forbidden: str) -> None:
    assert refused.__cause__ is None and refused.__context__ is None
    texts = [str(refused), repr(refused), repr(refused.args), log_text]
    texts += [repr(value) for value in vars(refused).values()]
    texts += [str(value) for value in vars(refused).values()]
    for text in texts:
        for value in forbidden:
            assert value not in text


def _refused_by(
    ids: m02.Identities, served: tuple[str, ...], migrator: Any = None, *,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> Any:
    """Run startup to a StartupRefused and check every refusal-row invariant
    except the reason, index and canaries (the caller's)."""

    from haloflow.startup import Deployment, StartupRefused, start_production_services

    caplog.set_level(logging.DEBUG)
    caplog.clear()

    async def run() -> Any:
        async with _deps(ids, migrator=migrator) as (deps, counting, store):
            with pytest.raises(StartupRefused) as caught:
                async with asyncio.timeout(DEADLINE):
                    await start_production_services(
                        deps, deployment=Deployment.PRODUCTION, served_tenants=served)
            assert counting.gateway_connections == 0
            assert store.calls == 0
            assert await _settle(ids) == 0
            return caught.value

    refused = asyncio.run(run())
    constructions.assert_none()
    assert type(refused) is StartupRefused
    assert refused.reason_code in b5.REASONS
    return refused


# ---------------------------------------------------------------------------
# Breaks with positive controls (R5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Break:
    """`statements`: (SQL, exact rowcount), or rowcount None for DDL, which then needs
    `changed` (one bool row: False before the break, True after). {s} = schema,
    {t} = tenant id. A `disposable` break runs on its own new tenant, reset after."""

    statements: tuple[tuple[str, int | None], ...]
    reason: str
    restore: tuple[str, ...] = ()
    changed: str | None = None
    disposable: bool = False


def _apply_break(ids: m02.Identities, brk: Break, tenant: tuple[str, str]) -> None:
    tenant_id, schema = tenant
    with m02.connect_admin(ids) as conn:
        if brk.changed is not None:
            assert conn.execute(brk.changed.format(s=schema, t=tenant_id)).fetchone() == (False,)
        for statement, expected in brk.statements:
            cursor = conn.execute(statement.format(s=schema, t=tenant_id))
            assert expected is not None or brk.changed is not None
            if expected is not None:
                assert cursor.rowcount == expected, (statement, cursor.rowcount)
        if brk.changed is not None:
            assert conn.execute(brk.changed.format(s=schema, t=tenant_id)).fetchone() == (True,)


def _restore_break(ids: m02.Identities, brk: Break, tenant: tuple[str, str]) -> None:
    with m02.connect_admin(ids) as conn:
        for statement in brk.restore:
            conn.execute(statement.format(s=tenant[1], t=tenant[0]))


WORK_MEM_SET = (
    "SELECT coalesce('work_mem=64kB' = ANY(p.proconfig), false) FROM pg_catalog.pg_proc p"
    " WHERE p.oid = '{s}.m05_get_item(uuid)'::regprocedure"
)
_M05_TYPE_ROWS: tuple[tuple[str, int | None], ...] = (
    ("DELETE FROM {s}.m02_envelope_path_allowlist"
     " WHERE event_type = 'm05_followup_resolution_asserted'", 3),
    ("DELETE FROM {s}.event_contracts WHERE event_type = 'm05_followup_resolution_asserted'", 1),
    ("DELETE FROM {s}.event_types WHERE event_type = 'm05_followup_resolution_asserted'", 1),
)
_TEST_INTAKE_ROW = m05.B2_NO_ALLOWLIST_PROFILE_SQL.replace("{schema}", "{s}").strip().rstrip(";")

BREAKS: dict[str, Break] = {
    "version-11": Break(
        (("UPDATE shared.tenants SET schema_version = 11"
          " WHERE tenant_id = '{t}' AND schema_version = 36", 1),),  # L-1W W3b target
        "SCHEMA_VERSION_UNSUPPORTED",
        restore=("UPDATE shared.tenants SET schema_version = 36 WHERE tenant_id = '{t}'",),
    ),
    "suspended": Break(
        (("UPDATE shared.tenants SET lifecycle_state = 'suspended'"
          " WHERE tenant_id = '{t}' AND lifecycle_state = 'active'", 1),),
        "TENANT_NOT_ACTIVE",
        restore=("UPDATE shared.tenants SET lifecycle_state = 'active' WHERE tenant_id = '{t}'",),
    ),
    "function-config-drift": Break(
        (("ALTER FUNCTION {s}.m05_get_item(uuid) SET work_mem = '64kB'", None),),
        "INSTALLED_STATE_DRIFT",
        restore=("ALTER FUNCTION {s}.m05_get_item(uuid) RESET work_mem",),
        changed=WORK_MEM_SET,
    ),
    "test-family-active": Break(
        (("UPDATE {s}.event_types SET is_active = true"
          " WHERE event_type = 'm02_test_observation_received' AND is_active = false", 1),),
        "TEST_FAMILY_ACTIVE_IN_PRODUCTION",
        restore=("UPDATE {s}.event_types SET is_active = false"
                 " WHERE event_type = 'm02_test_observation_received'",),
    ),
    "synthetic-resolver-active": Break(
        (("UPDATE {s}.m02_link_resolvers SET is_active = true"
          " WHERE resolver_id = 'm02_test_subject_v1' AND is_active = false", 1),),
        "SYNTHETIC_DISPATCH_IN_PRODUCTION",
        restore=("UPDATE {s}.m02_link_resolvers SET is_active = false"
                 " WHERE resolver_id = 'm02_test_subject_v1'",),
    ),
    # The three statements of m05.B4_RESOLUTION_PROFILE_SQL, one by one.
    "m05-full-activation": Break(
        (("UPDATE {s}.ref_action_families SET is_active = true"
          " WHERE action_family = 'm05_followup' AND is_active = false", 1),
         ("UPDATE {s}.event_types SET is_active = true"
          " WHERE event_type = 'm05_followup_resolution_asserted' AND is_active = false", 1),
         ("UPDATE {s}.m02_link_resolvers SET is_active = true"
          " WHERE resolver_id = 'm05_followup_item_v1' AND is_active = false", 1)),
        "M05_ACTIVATION_NOT_AUTHORIZED",
        restore=(
            m05.B4_RESOLUTION_PROFILE_SQL.replace("{schema}", "{s}").replace("true", "false"),),
    ),
    "m05-partial-activation": Break(
        (("UPDATE {s}.m02_link_resolvers SET is_active = true"
          " WHERE resolver_id = 'm05_followup_item_v1' AND is_active = false", 1),),
        "ROUTE_NOT_INSTALLED",
        restore=("UPDATE {s}.m02_link_resolvers SET is_active = false"
                 " WHERE resolver_id = 'm05_followup_item_v1'",),
    ),
    "m05-resolver-row-missing": Break(
        (("DELETE FROM {s}.m02_link_resolvers WHERE resolver_id = 'm05_followup_item_v1'", 1),),
        "GENERATION_UNREADABLE",
        restore=("INSERT INTO {s}.m02_link_resolvers (resolver_id, relationship, resolver_kind,"
                 " is_active) VALUES ('m05_followup_item_v1', 'case', 'module_owned', false)",),
    ),
    # R5: destructive, each on its own disposable tenant (reset, never restored).
    "m05-family-row-missing": Break(
        (*_M05_TYPE_ROWS,
         ("DELETE FROM {s}.ref_action_families WHERE action_family = 'm05_followup'", 1)),
        "GENERATION_UNREADABLE", disposable=True,
    ),
    "m05-type-row-missing": Break(_M05_TYPE_ROWS, "GENERATION_UNREADABLE", disposable=True),
    "test-intake-present": Break(
        ((_TEST_INTAKE_ROW, 1),), "TEST_INTAKE_PRESENT", disposable=True,
    ),
}


# ---------------------------------------------------------------------------
# B5-S01 happy path (two tenants)
# ---------------------------------------------------------------------------


def test_b5_s01_two_valid_tenants_start_and_serve(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, tenant_b: ProductionInstall,
    constructions: Constructions,
) -> None:
    from haloflow.m01.staff.service import StaffService
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.service import M05Service, ReadRefused
    from haloflow.startup import Deployment, ProductionServices, start_production_services

    tenant_a.require()
    tenant_b.require()

    async def run() -> None:
        async with _deps(m02_ids) as (deps, counting, store):
            async with asyncio.timeout(DEADLINE):
                services = await start_production_services(
                    deps, deployment=Deployment.PRODUCTION,
                    served_tenants=(tenant_a.tenant_id, tenant_b.tenant_id),
                )
            assert type(services) is ProductionServices
            # R1: exactly one of each, and they are the bundle's objects.
            constructions.assert_once()
            (m05_service,) = constructions.instances["M05Service"]
            (staff_service,) = constructions.instances["StaffService"]
            (foundation,) = constructions.instances["EventFoundation"]
            assert type(m05_service) is M05Service and services.m05 is m05_service
            assert type(staff_service) is StaffService and services.staff is staff_service
            assert type(foundation) is EventFoundation
            assert services.m05._foundation is foundation
            assert await _settle(m02_ids) == 0  # startup connections closed
            assert counting.gateway_connections == 0  # nothing served yet
            assert store.calls == 0  # startup reads only through the migrator
            for install in (tenant_a, tenant_b):
                ctx = await _context(services.resolver, install.tenant_id, READ_CAPS)
                assert ctx.tenant_id == install.tenant_id
                with pytest.raises(ReadRefused):  # the synthetic principal is not staff
                    await services.m05.get(ctx, uuid.uuid4())
            assert counting.gateway_connections == 2
            constructions.assert_once()  # serving constructs nothing more

    asyncio.run(run())


# ---------------------------------------------------------------------------
# B5-S02 one refusal each, bad tenant first and last
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("kind", list(BREAKS))
def test_b5_s02_one_bad_tenant_refuses_startup(
    kind: str, position: str, m02_ids: m02.Identities, m05_reset: Any,
    tenant_a: ProductionInstall, tenant_b: ProductionInstall,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> None:
    brk = BREAKS[kind]
    tenant_b.require()
    if brk.disposable:
        bad = m02.tenant_for(f"b5:s02:{kind}:{position}")
        m05_reset(bad)
    else:
        tenant_a.require()
        bad = tenant_a.tenant
    good = tenant_b.tenant
    try:
        if brk.disposable:
            _provision_production(m02_ids, bad)
        served = (bad[0], good[0]) if position == "first" else (good[0], bad[0])
        index = served.index(bad[0])
        _apply_break(m02_ids, brk, bad)
        try:
            refused = _refused_by(m02_ids, served, constructions=constructions, caplog=caplog)
            log_text = caplog.text
        finally:
            _restore_break(m02_ids, brk, bad)
    finally:
        if brk.disposable:
            m05_reset(bad)
    assert refused.reason_code == brk.reason
    assert refused.tenant_index == index
    _check_sanitized(refused, log_text, *bad, *good)


def test_b5_s03_non_production_accepts_full_m05_activation(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    from haloflow.startup import Deployment, start_production_services

    tenant_a.require()
    brk = BREAKS["m05-full-activation"]
    _apply_break(m02_ids, brk, tenant_a.tenant)
    try:
        async def run() -> None:
            async with _deps(m02_ids) as (deps, _counting, _store):
                async with asyncio.timeout(DEADLINE):
                    await start_production_services(
                        deps, deployment=Deployment.NON_PRODUCTION,
                        served_tenants=(tenant_a.tenant_id,))

        asyncio.run(run())
    finally:
        _restore_break(m02_ids, brk, tenant_a.tenant)


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
            async with asyncio.timeout(DEADLINE):
                services = await start_production_services(
                    deps, deployment=Deployment.PRODUCTION, served_tenants=served)
            before = store.calls
            with pytest.raises(TenantNotServed):
                await _context(services.resolver, tenant_c.tenant_id, READ_CAPS)
            assert store.calls == before  # refused before backing-resolver I/O

    asyncio.run(run())


def test_b5_s05_static_composition_failure_refused_before_any_connection(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, monkeypatch: pytest.MonkeyPatch,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> None:
    """R1 static row: the composition's supported set no longer holds the registry
    target (25). Refused before any I/O, so no tenant index and no migrator call."""

    import haloflow.composition as composition

    tenant_a.require()
    monkeypatch.setattr(composition, "APPROVED_SUPPORTED_SCHEMA_VERSIONS", frozenset({24}))
    calls = Calls()
    refused = _refused_by(m02_ids, (tenant_a.tenant_id,), _migrator_factory(m02_ids, calls=calls),
                          constructions=constructions, caplog=caplog)
    assert refused.reason_code == "COMPOSITION_INVALID"
    assert refused.tenant_index is None
    assert calls.n == 0
    _check_sanitized(refused, caplog.text, *tenant_a.tenant)


# ---------------------------------------------------------------------------
# B5-S15 SU-SNAPSHOT barrier (private checker hook)
# ---------------------------------------------------------------------------

SNAPSHOT_CHANGES = {
    "generation-row": BREAKS["m05-partial-activation"],
    "comparator-config": BREAKS["function-config-drift"],
}


@pytest.mark.parametrize("change", list(SNAPSHOT_CHANGES))
def test_b5_s15_commit_after_tenant_row_read_is_invisible(
    change: str, m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    """Correct behaviour: one REPEATABLE READ snapshot taken before the commit, so
    the tenant validates. SU-SNAPSHOT (a split read) refuses instead.

    PostgreSQL catalogue visibility (pg_proc under REPEATABLE READ) is MEASURED by
    the comparator-config row, not assumed (Codex 4b0a5448). If that row refuses on
    the correct code, the result is reported, not forced. The barrier's own
    positive control (rowcount / catalogue value) runs inside it."""

    from haloflow.startup import Deployment, _verify_tenant

    tenant_a.require()
    brk = SNAPSHOT_CHANGES[change]

    async def barrier() -> None:
        await asyncio.to_thread(_apply_break, m02_ids, brk, tenant_a.tenant)

    async def run() -> tuple[str, str, int]:
        connection = await _migrator_factory(m02_ids)()
        try:
            async with asyncio.timeout(DEADLINE):
                return await _verify_tenant(
                    connection, tenant_id=tenant_a.tenant_id,
                    deployment=Deployment.PRODUCTION, after_tenant_row=barrier)
        finally:
            await connection.close()

    try:
        validated = asyncio.run(run())
    finally:
        _restore_break(m02_ids, brk, tenant_a.tenant)
    assert validated == (tenant_a.tenant_id, tenant_a.schema, b5.PRODUCTION_TARGET)


# ---------------------------------------------------------------------------
# B5-A admission on the gateway path (R2)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _bundle(ids: m02.Identities, served: tuple[str, ...]) -> AsyncIterator[dict[str, Any]]:
    """The started bundle over its own dependencies. If startup raises, the entered
    dependencies (and their pool) are exited before the error propagates."""

    from haloflow.startup import Deployment, start_production_services

    async with _deps(ids) as (deps, counting, store):
        async with asyncio.timeout(DEADLINE):
            services = await start_production_services(
                deps, deployment=Deployment.PRODUCTION, served_tenants=served)
        yield {"services": services, "counting": counting, "store": store}


class ForgedStore:
    """A fake ControlStore: returns one fixed record for its tenant id, else None."""

    def __init__(self, record: Any) -> None:
        self.record = record
        self.calls = 0

    async def get_tenant(self, tenant_id: str) -> Any:
        self.calls += 1
        return self.record if tenant_id == self.record.tenant_id else None


Route = tuple[str, frozenset[str], Callable[[Any, Any], Awaitable[Any]]]


def _routes() -> tuple[Route, ...]:
    """Every service route, each with capabilities SUFFICIENT for its route check,
    so that only admission can refuse it before the gateway."""

    from haloflow.m01.staff.service import StaffAdminCommand
    from haloflow.m05.service import ListQuery, ResolveAction, StaffAction

    def claim() -> Any:
        return StaffAction(item_id=uuid.uuid4(), client_action_id=uuid.uuid4(),
                           expected_version=1, type="claim")

    def resolve() -> Any:
        return ResolveAction(item_id=uuid.uuid4(), client_action_id=uuid.uuid4(),
                             expected_version=1, type="resolve", outcome_code="rescheduled")

    def rename() -> Any:
        return StaffAdminCommand(client_action_id=uuid.uuid4(), operation="rename",
                                 staff_id=uuid.uuid4(), display_name="B5 Admission")

    return (
        ("m05.get", sv.READ_ONLY_CAPS, lambda s, c: s.m05.get(c, uuid.uuid4())),
        ("m05.history", sv.READ_ONLY_CAPS, lambda s, c: s.m05.history(c, uuid.uuid4())),
        ("m05.list_items", sv.READ_ONLY_CAPS,
         lambda s, c: s.m05.list_items(c, ListQuery(limit=10))),
        ("m05.act.claim", sv.STAFF_CAPS, lambda s, c: s.m05.act(c, claim())),
        ("m05.act.resolve", sv.RESOLVE_CAPS, lambda s, c: s.m05.act(c, resolve())),
        ("m05.read_note", sv.NOTE_READER_CAPS, lambda s, c: s.m05.read_note(c, uuid.uuid4())),
        ("staff.admin", frozenset({sv.STAFF_ADMIN}), lambda s, c: s.staff.admin(c, rename())),
        ("staff.display", frozenset({sv.STAFF_DISPLAY, sv.READ}),
         lambda s, c: s.staff.display(c, (uuid.uuid4(),))),
    )


def test_b5_a01_independent_context_for_unserved_tenant_refused_on_every_service(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, tenant_c: ProductionInstall
) -> None:
    from haloflow.composition import build_production_tenant_runtime
    from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver
    from haloflow.startup import TenantNotServed

    tenant_a.require()
    tenant_c.require()
    forged = {
        # (a) a SERVED tenant id bound to another (real) tenant's schema key.
        "served-id-other-schema": TenantRegistryRecord(
            tenant_id=tenant_a.tenant_id, schema_key=tenant_c.schema,
            lifecycle_state=LifecycleState.ACTIVE, schema_version=b5.PRODUCTION_TARGET),
        # (b) a tenant id that was never in shared.tenants.
        "unknown-tenant": TenantRegistryRecord(
            tenant_id=UNKNOWN_TENANT, schema_key=UNKNOWN_SCHEMA,
            lifecycle_state=LifecycleState.ACTIVE, schema_version=b5.PRODUCTION_TARGET),
    }
    assert not m02.admin_all(m02_ids, "SELECT 1 FROM shared.tenants WHERE tenant_id = %s",
                             (UNKNOWN_TENANT,))

    async def run() -> None:
        async with _deps(m02_ids) as (other, _oc, _os), _bundle(
                m02_ids, (tenant_a.tenant_id,)) as held:
            services, counting, store = held["services"], held["counting"], held["store"]
            sources: dict[str, tuple[Any, str, str]] = {
                # An independent production runtime: a valid context for an active
                # tenant that the bundle never validated.
                "unserved-tenant": (build_production_tenant_runtime(other).resolver,
                                    tenant_c.tenant_id, tenant_c.schema),
            }
            for label, record in forged.items():
                resolver = TenantResolver(
                    ForgedStore(record),
                    supported_schema_versions=frozenset({b5.PRODUCTION_TARGET}))
                sources[label] = (resolver, record.tenant_id, record.schema_key)

            for label, (resolver, tenant_id, schema_key) in sources.items():
                for route, caps, call in _routes():
                    ctx = await _context(resolver, tenant_id, caps)
                    assert (ctx.tenant_id, ctx.schema_key) == (tenant_id, schema_key)
                    before = store.calls
                    with pytest.raises(TenantNotServed):
                        await call(services, ctx)
                    assert counting.gateway_connections == 0, (label, route)
                    assert store.calls == before, (label, route)

            # Positive control: the same calls, capabilities and services with a
            # served context are not refused by admission and reach the gateway once
            # each (whatever the SQL then decides for a non-staff principal).
            for route, caps, call in _routes():
                ctx = await _context(services.resolver, tenant_a.tenant_id, caps)
                before = counting.gateway_connections
                outcome: BaseException | None = None
                try:
                    await call(services, ctx)
                except Exception as error:  # noqa: BLE001 - only admission is judged
                    outcome = error
                assert not isinstance(outcome, TenantNotServed), route
                assert counting.gateway_connections == before + 1, route

    asyncio.run(run())


def test_b5_a02_facade_refuses_unserved_and_unknown_before_resolver_io(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, tenant_c: ProductionInstall
) -> None:
    from haloflow.startup import TenantNotServed

    tenant_a.require()
    tenant_c.require()

    async def run() -> None:
        async with _bundle(m02_ids, (tenant_a.tenant_id,)) as held:
            services, store = held["services"], held["store"]
            for tenant_id in (tenant_c.tenant_id, UNKNOWN_TENANT):
                before = store.calls
                with pytest.raises(TenantNotServed):
                    await _context(services.resolver, tenant_id, READ_CAPS)
                assert store.calls == before

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
    brk = BREAKS["version-11"]

    async def run() -> None:
        async with _bundle(m02_ids, (tenant_a.tenant_id,)) as held:
            services = held["services"]
            ctx = await _context(services.resolver, tenant_a.tenant_id, READ_CAPS)
            await asyncio.to_thread(_apply_break, m02_ids, brk, tenant_a.tenant)
            try:
                with pytest.raises(RegistryInconsistent) as caught:
                    await services.m05.get(ctx, uuid.uuid4())
                assert caught.value.reason_code == "REGISTRY_REVALIDATION_MISMATCH"
            finally:
                await asyncio.to_thread(_restore_break, m02_ids, brk, tenant_a.tenant)

    asyncio.run(run())


def test_b5_a04_bundle_exposes_only_the_facade_and_admitted_services(
    m02_ids: m02.Identities, tenant_a: ProductionInstall
) -> None:
    """Introspection only; the admission proof is B5-A01."""

    import dataclasses

    from haloflow.m01.resolver import TenantResolver
    from haloflow.m01.staff.service import StaffService
    from haloflow.m05.service import M05Service
    from haloflow.startup import ServedTenantGateway, ServedTenantResolver

    tenant_a.require()

    async def run() -> None:
        async with _bundle(m02_ids, (tenant_a.tenant_id,)) as held:
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

    asyncio.run(run())


# ---------------------------------------------------------------------------
# B5-E sanitized connection failures, cleanup and cancellation (R3)
# ---------------------------------------------------------------------------


async def _wrap_failing_close(connection: AsyncConnection[Any]) -> AsyncConnection[Any]:
    """close() closes the real connection, then raises driver-like canary text."""

    real_close = connection.close

    async def close() -> None:
        await real_close()
        raise RuntimeError(CANARY_DRIVER)

    connection.close = close  # type: ignore[method-assign]
    return connection


def test_b5_e01_cleanup_failure_without_prior_failure_refuses(
    m02_ids: m02.Identities, tenant_a: ProductionInstall,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> None:
    tenant_a.require()
    refused = _refused_by(m02_ids, (tenant_a.tenant_id,),
                          _migrator_factory(m02_ids, wrap=_wrap_failing_close),
                          constructions=constructions, caplog=caplog)
    assert refused.reason_code == "STARTUP_CONNECTION_FAILED"
    assert refused.tenant_index == 0
    _check_sanitized(refused, caplog.text, CANARY_DRIVER, *tenant_a.tenant)


def test_b5_e02_non_idle_connection_refused_not_adopted(
    m02_ids: m02.Identities, tenant_a: ProductionInstall,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> None:
    tenant_a.require()

    async def in_transaction(connection: AsyncConnection[Any]) -> AsyncConnection[Any]:
        await connection.set_autocommit(False)
        await connection.execute("SELECT 1")
        return connection

    refused = _refused_by(m02_ids, (tenant_a.tenant_id,),
                          _migrator_factory(m02_ids, wrap=in_transaction),
                          constructions=constructions, caplog=caplog)
    assert refused.reason_code == "STARTUP_CONNECTION_FAILED"
    _check_sanitized(refused, caplog.text, *tenant_a.tenant)


def test_b5_e03_factory_failure_is_sanitized(
    m02_ids: m02.Identities, tenant_a: ProductionInstall,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> None:
    import psycopg

    tenant_a.require()

    async def failing() -> AsyncConnection[Any]:
        raise psycopg.OperationalError(CANARY_DRIVER)

    refused = _refused_by(m02_ids, (tenant_a.tenant_id,), failing,
                          constructions=constructions, caplog=caplog)
    assert refused.reason_code == "STARTUP_CONNECTION_FAILED"
    _check_sanitized(refused, caplog.text, CANARY_DRIVER, *tenant_a.tenant)


async def _until_blocked_by(ids: m02.Identities, blocker: int, task: asyncio.Future[Any]) -> int:
    """The single startup backend's pid, once `blocker` is in its pg_blocking_pids."""

    query = (
        "SELECT a.pid, %s = ANY(pg_catalog.pg_blocking_pids(a.pid))"
        " FROM pg_catalog.pg_stat_activity a WHERE a.application_name = %s"
    )
    deadline = time.monotonic() + DEADLINE
    while True:
        assert not task.done(), "startup finished before it was blocked"
        rows = await asyncio.to_thread(m02.admin_all, ids, query, (blocker, STARTUP_APP))
        assert len(rows) <= 1, "more than one startup backend"
        if rows and rows[0][1] is True:
            return int(rows[0][0])
        assert time.monotonic() < deadline, "the startup backend was never blocked"
        await asyncio.sleep(0.05)


async def _reap(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_b5_e04_cancellation_propagates_and_cleans_up(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, constructions: Constructions
) -> None:
    """An admin ACCESS EXCLUSIVE lock on a generation table blocks the startup read.
    Once the startup backend is OBSERVED blocked by the locker (pg_blocking_pids),
    the task is cancelled: CancelledError propagates unchanged, nothing is
    constructed, and no startup backend is left."""

    import psycopg

    from haloflow.startup import Deployment, start_production_services

    tenant_a.require()

    async def run() -> None:
        locker = await psycopg.AsyncConnection.connect(m02_ids.admin)
        try:
            await locker.execute(
                f"LOCK TABLE {tenant_a.schema}.event_types IN ACCESS EXCLUSIVE MODE")
            locker_pid = locker.info.backend_pid
            async with _deps(m02_ids) as (deps, counting, _s):
                task = asyncio.ensure_future(start_production_services(
                    deps, deployment=Deployment.PRODUCTION,
                    served_tenants=(tenant_a.tenant_id,)))
                try:
                    startup_pid = await _until_blocked_by(m02_ids, locker_pid, task)
                    assert startup_pid != locker_pid
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        async with asyncio.timeout(DEADLINE):
                            await task
                    assert task.cancelled()
                finally:
                    await _reap(task)
                assert counting.gateway_connections == 0
        finally:
            await locker.rollback()
            await locker.close()
        assert await _settle(m02_ids) == 0

    asyncio.run(run())
    constructions.assert_none()


def test_b5_e05_query_time_driver_error_maps_to_generation_unreadable(
    m02_ids: m02.Identities, m05_reset: Any,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> None:
    """A renamed generation column: the relation set is intact, so the snapshot
    SELECT itself raises (UndefinedColumn, 42703) inside the startup transaction.
    The driver error maps to GENERATION_UNREADABLE with no driver text."""

    tenant = m02.tenant_for("b5:e05:driver-error")
    m05_reset(tenant)
    brk = Break(
        (("ALTER TABLE {s}.event_contracts RENAME COLUMN signal_rule TO signal_rule_b5x",
          None),),
        "GENERATION_UNREADABLE",
        changed="SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute"
                " WHERE attrelid = '{s}.event_contracts'::regclass"
                " AND attname = 'signal_rule_b5x' AND NOT attisdropped)",
        disposable=True,
    )
    try:
        _provision_production(m02_ids, tenant)
        _apply_break(m02_ids, brk, tenant)
        refused = _refused_by(m02_ids, (tenant[0],), constructions=constructions, caplog=caplog)
        log_text = caplog.text
    finally:
        m05_reset(tenant)
    assert refused.reason_code == brk.reason
    assert refused.tenant_index == 0
    _check_sanitized(refused, log_text, *tenant, "signal_rule", "does not exist", "42703")


def test_b5_e06_cleanup_failure_after_refusal_keeps_the_original_reason(
    m02_ids: m02.Identities, tenant_a: ProductionInstall,
    constructions: Constructions, caplog: pytest.LogCaptureFixture,
) -> None:
    tenant_a.require()
    brk = BREAKS["suspended"]
    _apply_break(m02_ids, brk, tenant_a.tenant)
    try:
        refused = _refused_by(m02_ids, (tenant_a.tenant_id,),
                              _migrator_factory(m02_ids, wrap=_wrap_failing_close),
                              constructions=constructions, caplog=caplog)
        log_text = caplog.text
    finally:
        _restore_break(m02_ids, brk, tenant_a.tenant)
    assert refused.reason_code == "TENANT_NOT_ACTIVE"
    assert refused.tenant_index == 0
    _check_sanitized(refused, log_text, CANARY_DRIVER, *tenant_a.tenant)


def test_b5_e07_cancellation_during_refusal_cleanup_wins(
    m02_ids: m02.Identities, tenant_a: ProductionInstall, constructions: Constructions
) -> None:
    """A refusal is recorded (suspended tenant); its cleanup close() hangs. The task
    is cancelled once close() is entered: CancelledError propagates (cancellation
    wins over the recorded refusal), and the wrapper still closes the real
    connection, so no startup backend is left."""

    from haloflow.startup import Deployment, start_production_services

    tenant_a.require()
    brk = BREAKS["suspended"]

    async def run() -> None:
        closing = asyncio.Event()
        never = asyncio.Event()

        async def hanging_close(connection: AsyncConnection[Any]) -> AsyncConnection[Any]:
            real_close = connection.close

            async def close() -> None:
                closing.set()
                try:
                    await never.wait()
                finally:
                    await real_close()

            connection.close = close  # type: ignore[method-assign]
            return connection

        migrator = _migrator_factory(m02_ids, wrap=hanging_close)
        async with _deps(m02_ids, migrator=migrator) as (deps, counting, _s):
            task = asyncio.ensure_future(start_production_services(
                deps, deployment=Deployment.PRODUCTION, served_tenants=(tenant_a.tenant_id,)))
            try:
                async with asyncio.timeout(DEADLINE):
                    await closing.wait()
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    async with asyncio.timeout(DEADLINE):
                        await task
                assert task.cancelled()
            finally:
                await _reap(task)
            assert counting.gateway_connections == 0
        assert await _settle(m02_ids) == 0

    _apply_break(m02_ids, brk, tenant_a.tenant)
    try:
        asyncio.run(run())
    finally:
        _restore_break(m02_ids, brk, tenant_a.tenant)
    constructions.assert_none()


# ---------------------------------------------------------------------------
# COMPAT-B5a (observation only) and P-4 resume guard (R4)
# ---------------------------------------------------------------------------

LEDGER = (
    "SELECT migration_id, checksum, state FROM shared.schema_migrations"
    " WHERE tenant_id = %s ORDER BY migration_id"
)
TENANT_ROW = (
    "SELECT tenant_id, schema_key, lifecycle_state, schema_version FROM shared.tenants"
    " WHERE tenant_id = %s"
)

# The COMPLETE state a refused provisioning must leave untouched. %(t)s = tenant
# id, %(s)s = schema key.
FULL_STATE = {
    "tenant": "SELECT to_jsonb(t)::text FROM shared.tenants t WHERE t.tenant_id = %(t)s",
    "history": "SELECT to_jsonb(h)::text FROM shared.tenant_state_history h"
               " WHERE h.tenant_id = %(t)s ORDER BY h.event_id",
    "ledger": "SELECT to_jsonb(m)::text FROM shared.schema_migrations m"
              " WHERE m.tenant_id = %(t)s ORDER BY m.migration_id",
    "namespace": "SELECT n.nspowner::regrole::text, n.nspacl::text"
                 " FROM pg_catalog.pg_namespace n WHERE n.nspname = %(s)s",
    "class": "SELECT c.relname::text, c.relkind::text, c.relowner::regrole::text,"
             " c.relacl::text FROM pg_catalog.pg_class c"
             " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
             " WHERE n.nspname = %(s)s ORDER BY 1",
    "attribute": "SELECT c.relname::text, a.attnum, a.attname::text, a.attisdropped,"
                 " a.attacl::text FROM pg_catalog.pg_class c"
                 " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
                 " JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0"
                 " WHERE n.nspname = %(s)s ORDER BY 1, 2",
    "proc": "SELECT p.proname::text, pg_catalog.pg_get_function_identity_arguments(p.oid),"
            " p.prosrc, p.proconfig::text, p.proowner::regrole::text, p.proacl::text,"
            " p.prosecdef, p.provolatile::text FROM pg_catalog.pg_proc p"
            " JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = %(s)s ORDER BY 1, 2",
}


def _full_state(ids: m02.Identities, tenant: tuple[str, str]) -> dict[str, Any]:
    params = {"t": tenant[0], "s": tenant[1]}
    return {name: m02.admin_all(ids, query, params) for name, query in FULL_STATE.items()}


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


def test_p4_mismatched_target_resume_refused_before_writes(
    m02_ids: m02.Identities, m05_reset: Any
) -> None:
    """A POPULATED interrupted provisioning recorded at target 11: the Part A
    11-unit install (real schema, ledger and history), then set back to
    `provisioning` (the 001 trigger guards tenant_id and, outside provisioning,
    schema_key; it permits a lifecycle change). The 25-unit run is refused before
    any write, and the complete state is equal before and after."""

    from haloflow.m01.errors import ProvisioningFailed

    tenant = m02.tenant_for("b5:p4:mismatch")
    m05_reset(tenant)
    try:
        m02.provision_sync(m02_ids, _part_a_registry(), tenant, supported=range(11, 12))
        with m02.connect_admin(m02_ids) as conn:
            cursor = conn.execute(
                "UPDATE shared.tenants SET lifecycle_state = 'provisioning'"
                " WHERE tenant_id = %s AND lifecycle_state = 'active' AND schema_version = 11",
                (tenant[0],))
            assert cursor.rowcount == 1
        before = _full_state(m02_ids, tenant)
        # The fixture is what it claims: interrupted at 11, populated.
        assert m02.admin_all(m02_ids, TENANT_ROW, (tenant[0],)) == [
            (tenant[0], tenant[1], "provisioning", 11)]
        ledger = m02.admin_all(m02_ids, LEDGER, (tenant[0],))
        assert len(ledger) == len(m05.PART_A_UNIT_IDS)
        assert {row[2] for row in ledger} == {"applied"}
        assert before["namespace"] and before["class"] and before["proc"] and before["history"]

        with pytest.raises(ProvisioningFailed) as caught:
            _provision_production(m02_ids, tenant)
        assert caught.value.reason_code == "TENANT_NOT_RESUMABLE"
        assert _full_state(m02_ids, tenant) == before
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
    before = _full_state(m02_ids, tenant_a.tenant)
    assert before["tenant"] and before["ledger"] and before["class"] and before["proc"]
    with pytest.raises(ProvisioningFailed) as caught:
        _provision_production(m02_ids, tenant_a.tenant)
    assert caught.value.reason_code == "TENANT_NOT_RESUMABLE"
    assert _full_state(m02_ids, tenant_a.tenant) == before
