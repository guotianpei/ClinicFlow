"""L-1 runtime composition: PostgreSQL 17 cases, plus G3.

Traceability: L-1 test cases v2 + addendum 1 (owner-approved 2026-09-29) under
requirements erratum 2 and architecture addendum 2. Case ids appear in names.

Placed in tests/m02 (not tests/m01 as plan v2 section 2 listed) because the
synthetic-tenant helpers live in `m02_support`, which by this package's rule is
reached only through the `m02` fixture (tests/m02/conftest.py).

Evidence rules (plan v2 section 5; addendum 1 R4):
- Synthetic data only. Every admin setup statement runs on a separate autocommit
  connection and completes before the gateway call opens its own connection.
- Production-entry evidence is resolution, admission and routing only; the
  production statement catalogue is empty (B-1). Statement service and data
  isolation run through the generic builder with the production registry, the
  production declaration and a TEST-ONLY catalogue. That is not evidence that
  the production composition serves tenant data (L-1S).
- The new interface is imported inside each test (plan v2 section 7.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from psycopg import AsyncConnection, sql

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TrustedSource,
)
from haloflow.m01.control_store import PsycopgControlStore
from haloflow.m01.errors import RegistryInconsistent, TenantUnavailable
from haloflow.m01.pool import TenantPool
from haloflow.m01.provisioning import (
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    ProvisioningRequest,
)
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.statements import StatementMode, build_statement_catalog

MARKER_KEY = "m01_test.l1_marker"
TEST_ONLY_CATALOG = build_statement_catalog(
    {
        MARKER_KEY: (
            StatementMode.READ,
            "probe:read",
            "SELECT marker FROM isolation_probe WHERE business_id = %s",
        )
    }
)
SENTINEL = object()


def _factory(conninfo: str) -> Callable[[], Awaitable[AsyncConnection[Any]]]:
    async def _connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


@asynccontextmanager
async def _dependencies(ids: Any) -> AsyncIterator[Any]:
    from haloflow.m01.runtime import TenantRuntimeDependencies

    pool = TenantPool(ids.logins[RUNTIME_ROLE], min_size=1, max_size=1)
    await pool.open()
    try:
        yield TenantRuntimeDependencies(
            pool=pool,
            control_store=PsycopgControlStore(pool),
            migrator_connect=_factory(ids.logins[MIGRATOR_ROLE]),
            provisioner_connect=_factory(ids.logins[PROVISIONER_ROLE]),
        )
    finally:
        await pool.close()


def _principal(*tenant_ids: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="l1-test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset(tenant_ids),
        capabilities=frozenset({"probe:read"}),
    )


async def _resolve(runtime: Any, tenant_id: str) -> Any:
    return await runtime.resolver.resolve(
        principal=_principal(tenant_id),
        tenant_hint=tenant_id,
        purpose="operations",
        capabilities=frozenset({"probe:read"}),
        source=TrustedSource.WORKER,
        execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:l1:{tenant_id}"),
        correlation_id=uuid5(NAMESPACE_URL, f"haloflow-test:l1-corr:{tenant_id}"),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
    )


async def _provision_through(runtime: Any, tenant: tuple[str, str]) -> Any:
    tenant_id, schema_key = tenant
    return await runtime.provisioner.provision(
        ProvisioningRequest(
            tenant_id=tenant_id,
            schema_key=schema_key,
            actor_id="l1-test",
            execution_id=uuid5(
                NAMESPACE_URL, f"haloflow-test:l1:provision:{tenant_id}"
            ),
        )
    )


def _admin(
    m02: ModuleType, ids: Any, statement: sql.Composable | str, params: Any = None
) -> int:
    """One committed admin statement on its own autocommit connection."""

    with m02.connect_admin(ids) as conn:
        cursor = conn.execute(statement, params)
        return int(cursor.rowcount)


def _install_probe(m02: ModuleType, ids: Any, schema_key: str, marker: str) -> None:
    schema = sql.Identifier(schema_key)
    _admin(
        m02,
        ids,
        sql.SQL(
            "CREATE TABLE {}.isolation_probe "
            "(business_id integer PRIMARY KEY, marker text NOT NULL)"
        ).format(schema),
    )
    _admin(
        m02,
        ids,
        sql.SQL("INSERT INTO {}.isolation_probe VALUES (42, %s)").format(schema),
        (marker,),
    )
    _admin(
        m02,
        ids,
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            schema, sql.Identifier(RUNTIME_ROLE)
        ),
    )
    _admin(
        m02,
        ids,
        sql.SQL("GRANT SELECT ON {}.isolation_probe TO {}").format(
            schema, sql.Identifier(RUNTIME_ROLE)
        ),
    )


@dataclass
class Callback:
    calls: int = 0

    async def __call__(self, handle: Any) -> object:
        self.calls += 1
        return SENTINEL


@asynccontextmanager
async def _tenants(
    m02: ModuleType, reset: Callable[[tuple[str, str]], None], *labels: str
) -> AsyncIterator[list[tuple[str, str]]]:
    tenants = [m02.tenant_for(label) for label in labels]
    for tenant in tenants:
        reset(tenant)
    try:
        yield tenants
    finally:
        for tenant in tenants:
            reset(tenant)


# --- R-L1.4 (i) and (ii): through the production entry ---------------------


@pytest.mark.postgres
async def test_l1_e1a_e1b_a_version_three_tenant_is_resolved_and_admitted(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    from haloflow.composition import build_production_tenant_runtime

    async with _dependencies(m02_ids) as deps:
        runtime = build_production_tenant_runtime(deps)
        outcome = await _provision_through(runtime, m02_tenant)
        assert outcome.schema_version == 3

        context = await _resolve(runtime, m02_tenant[0])  # E1a
        assert (context.tenant_id, context.schema_key) == m02_tenant

        callback = Callback()
        result = await runtime.gateway.with_tenant_transaction(context, callback)  # E1b
        assert result is SENTINEL
        assert callback.calls == 1


# --- R-L1.4 (iii) under erratum 2: generic builder, TEST-ONLY catalogue ----


@pytest.mark.postgres
async def test_l1_e1c_one_catalogued_statement_is_served(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_tenant_migrations,
        build_production_tenant_runtime,
    )
    from haloflow.m01.runtime import compose_tenant_runtime

    async with _dependencies(m02_ids) as deps:
        await _provision_through(build_production_tenant_runtime(deps), m02_tenant)
        _install_probe(m02, m02_ids, m02_tenant[1], "l1-e1c-marker")
        runtime = compose_tenant_runtime(
            deps,
            registry=build_production_tenant_migrations(),
            catalog=TEST_ONLY_CATALOG,
            supported_schema_versions=APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        )
        context = await _resolve(runtime, m02_tenant[0])

        async def read(handle: Any) -> str:
            row = await handle.fetch_one(MARKER_KEY, (42,))
            assert row is not None
            return str(row[0])

        assert (
            await runtime.gateway.with_tenant_transaction(context, read)
            == "l1-e1c-marker"
        )


# --- R-L1.5: every version outside {3} is refused ---------------------------


@pytest.mark.postgres
async def test_l1_e2_the_resolver_refuses_versions_one_two_and_four(
    m02: ModuleType, m02_ids: Any, m02_reset: Callable[[tuple[str, str]], None]
) -> None:
    from haloflow.composition import build_production_tenant_runtime
    from haloflow.m01.provisioning.units import (
        TENANT_MIGRATIONS,
        build_tenant_migration_registry,
    )
    from haloflow.m02.units import T002_MIGRATION_ID, T002_SQL

    async with (
        _tenants(m02, m02_reset, "l1-e2-v1", "l1-e2-v2", "l1-e2-v4") as (v1, v2, v4),
        _dependencies(m02_ids) as deps,
    ):
        runtime = build_production_tenant_runtime(deps)
        await m02.provision(m02_ids, m02.m01_only_registry(), v1, supported=range(1, 2))
        t001_t002 = build_tenant_migration_registry(
            TENANT_MIGRATIONS, {T002_MIGRATION_ID: T002_SQL}
        )
        await m02.provision(m02_ids, t001_t002, v2, supported=range(2, 3))
        await _provision_through(runtime, v4)
        assert (
            _admin(
                m02,
                m02_ids,
                "UPDATE shared.tenants SET schema_version = 4 WHERE tenant_id = %s",
                (v4[0],),
            )
            == 1
        )

        for tenant, version in ((v1, 1), (v2, 2), (v4, 4)):
            with pytest.raises(TenantUnavailable) as refused:
                await _resolve(runtime, tenant[0])
            assert refused.value.reason_code == "SCHEMA_VERSION_INCOMPATIBLE", version


# --- R-L1.5 as erratum 1: the four pre-callback gateway refusals -------------


@pytest.mark.postgres
@pytest.mark.parametrize("version", [1, 2, 4])
async def test_l1_e3a_a_row_leaving_the_set_is_refused_before_the_callback(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str], version: int
) -> None:
    from haloflow.composition import build_production_tenant_runtime

    async with _dependencies(m02_ids) as deps:
        runtime = build_production_tenant_runtime(deps)
        await _provision_through(runtime, m02_tenant)
        context = await _resolve(runtime, m02_tenant[0])
        assert (
            _admin(
                m02,
                m02_ids,
                "UPDATE shared.tenants SET schema_version = %s WHERE tenant_id = %s",
                (version, m02_tenant[0]),
            )
            == 1
        )

        callback = Callback()
        with pytest.raises(RegistryInconsistent) as refused:
            await runtime.gateway.with_tenant_transaction(context, callback)
        assert refused.value.reason_code == "REGISTRY_REVALIDATION_MISMATCH"
        assert callback.calls == 0


@pytest.mark.postgres
async def test_l1_e3b_a_missing_registry_row_is_refused_before_the_callback(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """A dedicated, never-provisioned synthetic row: no dependent rows exist, so
    one single-row DELETE suffices. No trigger is disabled, nothing cascades."""

    from haloflow.composition import build_production_tenant_runtime

    tenant_id, schema_key = m02_tenant
    assert (
        _admin(
            m02,
            m02_ids,
            "INSERT INTO shared.tenants (tenant_id, schema_key, lifecycle_state, schema_version) "
            "VALUES (%s, %s, 'active', 3)",
            (tenant_id, schema_key),
        )
        == 1
    )
    async with _dependencies(m02_ids) as deps:
        runtime = build_production_tenant_runtime(deps)
        context = await _resolve(runtime, tenant_id)
        assert (
            _admin(
                m02,
                m02_ids,
                "DELETE FROM shared.tenants WHERE tenant_id = %s",
                (tenant_id,),
            )
            == 1
        )

        callback = Callback()
        with pytest.raises(RegistryInconsistent) as refused:
            await runtime.gateway.with_tenant_transaction(context, callback)
        assert refused.value.reason_code == "REGISTRY_ROW_MISSING"
        assert callback.calls == 0


@pytest.mark.postgres
async def test_l1_e3c_a_non_active_tenant_is_refused_before_the_callback(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    from haloflow.composition import build_production_tenant_runtime

    async with _dependencies(m02_ids) as deps:
        runtime = build_production_tenant_runtime(deps)
        await _provision_through(runtime, m02_tenant)
        context = await _resolve(runtime, m02_tenant[0])
        update = "UPDATE shared.tenants SET lifecycle_state = %s WHERE tenant_id = %s"
        assert _admin(m02, m02_ids, update, ("suspended", m02_tenant[0])) == 1
        try:
            callback = Callback()
            with pytest.raises(RegistryInconsistent) as refused:
                await runtime.gateway.with_tenant_transaction(context, callback)
            assert refused.value.reason_code == "REGISTRY_REVALIDATION_MISMATCH"
            assert callback.calls == 0
        finally:
            _admin(m02, m02_ids, update, ("active", m02_tenant[0]))


@pytest.mark.postgres
async def test_l1_e3d_a_schema_key_mismatch_is_refused_before_the_callback(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """Trigger-compliant: three separate committed UPDATEs; triggers untouched."""

    from haloflow.composition import build_production_tenant_runtime

    tenant_id, schema_key = m02_tenant
    other_key = m02.tenant_for(f"{tenant_id}-other-key")[1]
    async with _dependencies(m02_ids) as deps:
        runtime = build_production_tenant_runtime(deps)
        await _provision_through(runtime, m02_tenant)
        context = await _resolve(runtime, tenant_id)
        set_state = (
            "UPDATE shared.tenants SET lifecycle_state = %s WHERE tenant_id = %s"
        )
        assert _admin(m02, m02_ids, set_state, ("provisioning", tenant_id)) == 1
        assert (
            _admin(
                m02,
                m02_ids,
                "UPDATE shared.tenants SET schema_key = %s WHERE tenant_id = %s",
                (other_key, tenant_id),
            )
            == 1
        )
        assert _admin(m02, m02_ids, set_state, ("active", tenant_id)) == 1

        row = m02.admin_one(
            m02_ids,
            "SELECT tenant_id, schema_key, lifecycle_state, schema_version "
            "FROM shared.tenants WHERE tenant_id = %s",
            (tenant_id,),
        )
        assert row == (tenant_id, other_key, "active", 3)
        assert other_key != context.schema_key

        callback = Callback()
        with pytest.raises(RegistryInconsistent) as refused:
            await runtime.gateway.with_tenant_transaction(context, callback)
        assert refused.value.reason_code == "REGISTRY_REVALIDATION_MISMATCH"
        assert callback.calls == 0


# --- R-L1.7 under erratum 2 -------------------------------------------------


@pytest.mark.postgres
async def test_l1_f1_f2b_two_tenants_with_identical_record_ids_stay_isolated(
    m02: ModuleType, m02_ids: Any, m02_reset: Callable[[tuple[str, str]], None]
) -> None:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_tenant_migrations,
        build_production_tenant_runtime,
    )
    from haloflow.m01.runtime import compose_tenant_runtime

    async with (
        _tenants(m02, m02_reset, "l1-f1-a", "l1-f1-b") as (tenant_a, tenant_b),
        _dependencies(m02_ids) as deps,
    ):
        production = build_production_tenant_runtime(deps)
        for tenant in (tenant_a, tenant_b):
            await _provision_through(production, tenant)
        assert tenant_a[0] != tenant_b[0] and tenant_a[1] != tenant_b[1]

        # F2b: production entry, statement-free callbacks; admission plus the
        # pre-callback current_schemas check pass for each tenant.
        for tenant in (tenant_a, tenant_b):
            callback = Callback()
            context = await _resolve(production, tenant[0])
            assert (
                await production.gateway.with_tenant_transaction(context, callback)
                is SENTINEL
            )
            assert callback.calls == 1

        # F1: generic builder with a test-only catalogue; same record id 42.
        _install_probe(m02, m02_ids, tenant_a[1], "tenant-a-marker")
        _install_probe(m02, m02_ids, tenant_b[1], "tenant-b-marker")
        runtime = compose_tenant_runtime(
            deps,
            registry=build_production_tenant_migrations(),
            catalog=TEST_ONLY_CATALOG,
            supported_schema_versions=APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        )

        async def read(handle: Any) -> str:
            row = await handle.fetch_one(MARKER_KEY, (42,))
            assert row is not None
            return str(row[0])

        observed = []
        for tenant in (tenant_a, tenant_b, tenant_a):
            context = await _resolve(runtime, tenant[0])
            observed.append(
                await runtime.gateway.with_tenant_transaction(context, read)
            )
        assert observed == ["tenant-a-marker", "tenant-b-marker", "tenant-a-marker"]


# --- R-L1.8: G3 (static; no database) ----------------------------------------


def test_l1_g3_a_fixture_widened_set_cannot_reach_the_declaration(
    m02: ModuleType,
) -> None:
    import inspect

    from haloflow import composition
    from haloflow.m01.runtime import TenantRuntimeDependencies

    widened = inspect.signature(m02.provision).parameters["supported"].default
    assert widened == range(1, 4)

    runtime = composition.build_production_tenant_runtime(
        TenantRuntimeDependencies(
            pool=object(),  # type: ignore[arg-type]
            control_store=object(),  # type: ignore[arg-type]
            migrator_connect=object(),  # type: ignore[arg-type]
            provisioner_connect=object(),  # type: ignore[arg-type]
        )
    )
    assert runtime.supported_schema_versions == frozenset({3})
    assert runtime.resolver._supported_schema_versions == frozenset({3})
    assert runtime.gateway._supported_schema_versions == frozenset({3})
    assert runtime.provisioner._supported_schema_versions == frozenset({3})
    declared = composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert declared == frozenset({3})
