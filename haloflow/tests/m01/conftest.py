from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver

FIXTURE_EXECUTION_ID = uuid5(NAMESPACE_URL, "haloflow-test:fixture")
FIXTURE_CORRELATION_ID = uuid5(NAMESPACE_URL, "haloflow-test:fixture-correlation")


class SingleTenantControlStore:
    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        if tenant_id != "clinic-a":
            return None
        return TenantRegistryRecord(
            tenant_id="clinic-a",
            schema_key="tenant_aaaaaaaa",
            lifecycle_state=LifecycleState.ACTIVE,
            schema_version=1,
        )


def principal_with(*capabilities: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset({"clinic-a"}),
        capabilities=frozenset(capabilities),
    )


async def resolve_context(
    *,
    expired: bool = False,
    capabilities: frozenset[str] = frozenset({"appointments:read"}),
    principal: Principal | None = None,
    execution_id: UUID | None = None,
    correlation_id: UUID | None = None,
    correlation_source: CorrelationSource = CorrelationSource.TRUSTED_INFRASTRUCTURE,
) -> TenantContext:
    now = datetime.now(UTC)
    resolver = TenantResolver(
        SingleTenantControlStore(),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=-1 if expired else 60),
        clock=lambda: now,
    )
    return await resolver.resolve(
        principal=principal or principal_with(*capabilities),
        tenant_hint="clinic-a",
        purpose="treatment",
        capabilities=capabilities,
        source=TrustedSource.WORKER,
        execution_id=execution_id or FIXTURE_EXECUTION_ID,
        correlation_id=correlation_id or FIXTURE_CORRELATION_ID,
        correlation_source=correlation_source,
    )


@pytest.fixture
async def active_context() -> TenantContext:
    return await resolve_context()


@pytest.fixture
async def expired_context() -> TenantContext:
    return await resolve_context(expired=True)
