from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from haloflow.m01.context import (
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.errors import ContextExpired, ContextInvalid


def test_context_cannot_be_constructed_by_a_caller() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ContextInvalid):
        TenantContext(
            tenant_id="clinic-a",
            schema_key="tenant_aaaaaaaa",
            principal=Principal(
                kind=PrincipalKind.WORKLOAD,
                id="test-worker",
                auth_method="test",
                authorized_tenant_ids=frozenset({"clinic-a"}),
                capabilities=frozenset({"appointments:read"}),
            ),
            capabilities=frozenset({"appointments:read"}),
            purpose="treatment",
            operation_id="op-1",
            request_id=None,
            source=TrustedSource.WORKER,
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
        )


def test_resolved_context_is_immutable(active_context: TenantContext) -> None:
    with pytest.raises(FrozenInstanceError):
        active_context.tenant_id = "clinic-b"  # type: ignore[misc]


def test_context_rejects_use_after_expiry(expired_context: TenantContext) -> None:
    with pytest.raises(ContextExpired):
        expired_context.assert_usable()
