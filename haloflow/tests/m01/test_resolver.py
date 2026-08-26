from datetime import UTC

import pytest

from haloflow.m01.context import Principal, PrincipalKind, TrustedSource
from haloflow.m01.errors import TenantDenied, TenantUnavailable
from haloflow.m01.resolver import (
    LifecycleState,
    TenantRegistryRecord,
    TenantResolver,
)


class FakeControlStore:
    def __init__(self, records: dict[str, TenantRegistryRecord]) -> None:
        self._records = records
        self.lookups: list[str] = []

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        self.lookups.append(tenant_id)
        return self._records.get(tenant_id)


def principal(*, tenant_ids: set[str], capabilities: set[str]) -> Principal:
    return Principal(
        kind=PrincipalKind.ACTOR,
        id="user-123",
        auth_method="test-identity-provider",
        authorized_tenant_ids=frozenset(tenant_ids),
        capabilities=frozenset(capabilities),
    )


def record(
    tenant_id: str = "clinic-a",
    *,
    schema_key: str = "tenant_aaaaaaaa",
    state: LifecycleState = LifecycleState.ACTIVE,
) -> TenantRegistryRecord:
    return TenantRegistryRecord(
        tenant_id=tenant_id,
        schema_key=schema_key,
        lifecycle_state=state,
        schema_version=1,
    )


@pytest.mark.asyncio
async def test_resolver_issues_registry_derived_context() -> None:
    store = FakeControlStore({"clinic-a": record()})
    resolver = TenantResolver(store, supported_schema_versions=range(1, 2))

    context = await resolver.resolve(
        principal=principal(tenant_ids={"clinic-a"}, capabilities={"appointments:read"}),
        tenant_hint="clinic-a",
        purpose="treatment",
        capability="appointments:read",
        source=TrustedSource.HTTP,
        operation_id="op-1",
    )

    assert context.tenant_id == "clinic-a"
    assert context.schema_key == "tenant_aaaaaaaa"
    assert context.capabilities == frozenset({"appointments:read"})
    assert context.issued_at.tzinfo is UTC
    assert store.lookups == ["clinic-a"]


@pytest.mark.asyncio
async def test_resolver_rejects_cross_tenant_hint_before_registry_lookup() -> None:
    store = FakeControlStore({"clinic-b": record("clinic-b", schema_key="tenant_bbbbbbbb")})
    resolver = TenantResolver(store, supported_schema_versions=range(1, 2))

    with pytest.raises(TenantDenied):
        await resolver.resolve(
            principal=principal(tenant_ids={"clinic-a"}, capabilities={"appointments:read"}),
            tenant_hint="clinic-b",
            purpose="treatment",
            capability="appointments:read",
            source=TrustedSource.HTTP,
            operation_id="op-2",
        )

    assert store.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        LifecycleState.PROVISIONING,
        LifecycleState.SUSPENDED,
        LifecycleState.ARCHIVAL_PENDING,
        LifecycleState.ARCHIVED,
        LifecycleState.DECOMMISSIONED,
    ],
)
async def test_resolver_fails_closed_for_non_active_tenants(
    state: LifecycleState,
) -> None:
    store = FakeControlStore({"clinic-a": record(state=state)})
    resolver = TenantResolver(store, supported_schema_versions=range(1, 2))

    with pytest.raises(TenantUnavailable):
        await resolver.resolve(
            principal=principal(tenant_ids={"clinic-a"}, capabilities={"appointments:read"}),
            tenant_hint="clinic-a",
            purpose="treatment",
            capability="appointments:read",
            source=TrustedSource.HTTP,
            operation_id="op-3",
        )


@pytest.mark.asyncio
async def test_resolver_rejects_invalid_registry_schema_key() -> None:
    store = FakeControlStore({"clinic-a": record(schema_key='tenant_a"; DROP SCHEMA shared; --')})
    resolver = TenantResolver(store, supported_schema_versions=range(1, 2))

    with pytest.raises(TenantUnavailable):
        await resolver.resolve(
            principal=principal(tenant_ids={"clinic-a"}, capabilities={"appointments:read"}),
            tenant_hint="clinic-a",
            purpose="treatment",
            capability="appointments:read",
            source=TrustedSource.HTTP,
            operation_id="op-4",
        )


@pytest.mark.asyncio
async def test_resolver_rejects_missing_capability_without_registry_lookup() -> None:
    store = FakeControlStore({"clinic-a": record()})
    resolver = TenantResolver(store, supported_schema_versions=range(1, 2))

    with pytest.raises(TenantDenied):
        await resolver.resolve(
            principal=principal(tenant_ids={"clinic-a"}, capabilities=set()),
            tenant_hint="clinic-a",
            purpose="treatment",
            capability="appointments:read",
            source=TrustedSource.HTTP,
            operation_id="op-5",
        )

    assert store.lookups == []


@pytest.mark.asyncio
async def test_resolver_rejects_incompatible_schema_version() -> None:
    incompatible = TenantRegistryRecord(
        tenant_id="clinic-a",
        schema_key="tenant_aaaaaaaa",
        lifecycle_state=LifecycleState.ACTIVE,
        schema_version=2,
    )
    store = FakeControlStore({"clinic-a": incompatible})
    resolver = TenantResolver(store, supported_schema_versions=range(1, 2))

    with pytest.raises(TenantUnavailable):
        await resolver.resolve(
            principal=principal(tenant_ids={"clinic-a"}, capabilities={"appointments:read"}),
            tenant_hint="clinic-a",
            purpose="treatment",
            capability="appointments:read",
            source=TrustedSource.HTTP,
            operation_id="op-6",
        )
