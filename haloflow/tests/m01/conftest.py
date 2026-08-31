"""Shared M01 test fixtures.

Helpers are exposed as fixtures rather than imported across test modules. The
earlier `from conftest import ...` worked only because pytest's prepend import
mode puts this directory on sys.path, which is an avoidable dependency on
collection mechanics.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
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


class ConfigurableControlStore:
    def __init__(self, record: TenantRegistryRecord | None) -> None:
        self._record = record

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        return self._record


def _principal_with(*capabilities: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset({"clinic-a"}),
        capabilities=frozenset(capabilities),
    )


@pytest.fixture
def execution_id() -> UUID:
    return FIXTURE_EXECUTION_ID


@pytest.fixture
def correlation_id() -> UUID:
    return FIXTURE_CORRELATION_ID


@pytest.fixture
def principal_with() -> Callable[..., Principal]:
    return _principal_with


@pytest.fixture
def control_store() -> SingleTenantControlStore:
    return SingleTenantControlStore()


@pytest.fixture
def make_control_store() -> Callable[[TenantRegistryRecord | None], ConfigurableControlStore]:
    return ConfigurableControlStore


@pytest.fixture
def make_resolver() -> Callable[..., TenantResolver]:
    def _make(store: object | None = None, *, ttl_seconds: int = 60) -> TenantResolver:
        return TenantResolver(
            store or SingleTenantControlStore(),  # type: ignore[arg-type]
            supported_schema_versions=range(1, 2),
            context_ttl=timedelta(seconds=ttl_seconds),
            clock=lambda: datetime.now(UTC),
        )

    return _make


@pytest.fixture
def resolve(
    make_resolver: Callable[..., TenantResolver],
) -> Callable[..., Awaitable[TenantContext]]:
    """Resolve a context with sensible defaults; override any argument by keyword."""

    async def _resolve(*, store: object | None = None, **overrides: Any) -> TenantContext:
        kwargs: dict[str, Any] = {
            "principal": _principal_with("appointments:read"),
            "tenant_hint": "clinic-a",
            "purpose": "treatment",
            "capabilities": frozenset({"appointments:read"}),
            "source": TrustedSource.WORKER,
            "execution_id": FIXTURE_EXECUTION_ID,
            "correlation_id": FIXTURE_CORRELATION_ID,
            "correlation_source": CorrelationSource.TRUSTED_INFRASTRUCTURE,
        }
        kwargs.update(overrides)
        return await make_resolver(store).resolve(**kwargs)

    return _resolve


async def _resolve_context(*, expired: bool = False) -> TenantContext:
    now = datetime.now(UTC)
    resolver = TenantResolver(
        SingleTenantControlStore(),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=-1 if expired else 60),
        clock=lambda: now,
    )
    return await resolver.resolve(
        principal=_principal_with("appointments:read"),
        tenant_hint="clinic-a",
        purpose="treatment",
        capabilities=frozenset({"appointments:read"}),
        source=TrustedSource.WORKER,
        execution_id=FIXTURE_EXECUTION_ID,
        correlation_id=FIXTURE_CORRELATION_ID,
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
    )


@pytest.fixture
async def active_context() -> TenantContext:
    return await _resolve_context()


@pytest.fixture
async def expired_context() -> TenantContext:
    return await _resolve_context(expired=True)
