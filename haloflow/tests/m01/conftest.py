from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

from haloflow.m01.context import Principal, PrincipalKind, TenantContext, TrustedSource
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver


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


async def resolve_context(*, expired: bool = False) -> TenantContext:
    now = datetime.now(UTC)
    resolver = TenantResolver(
        SingleTenantControlStore(),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=-1 if expired else 60),
        clock=lambda: now,
    )
    return await resolver.resolve(
        principal=Principal(
            kind=PrincipalKind.WORKLOAD,
            id="test-worker",
            auth_method="test",
            authorized_tenant_ids=frozenset({"clinic-a"}),
            capabilities=frozenset({"appointments:read"}),
        ),
        tenant_hint="clinic-a",
        purpose="treatment",
        capability="appointments:read",
        source=TrustedSource.WORKER,
        operation_id=str(uuid5(NAMESPACE_URL, "haloflow-test:fixture")),
    )


@pytest.fixture
async def active_context() -> TenantContext:
    return await resolve_context()


@pytest.fixture
async def expired_context() -> TenantContext:
    return await resolve_context(expired=True)
