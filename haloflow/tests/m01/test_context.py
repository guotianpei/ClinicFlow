from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.errors import ContextExpired, ContextInvalid


def _construct(**overrides: object) -> None:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "tenant_id": "clinic-a",
        "schema_key": "tenant_aaaaaaaa",
        "principal": Principal(
            kind=PrincipalKind.WORKLOAD,
            id="test-worker",
            auth_method="test",
            authorized_tenant_ids=frozenset({"clinic-a"}),
            capabilities=frozenset({"appointments:read"}),
        ),
        "capabilities": frozenset({"appointments:read"}),
        "purpose": "treatment",
        "execution_id": uuid5(NAMESPACE_URL, "haloflow-test:construct"),
        "correlation_id": uuid4(),
        "correlation_source": CorrelationSource.TRUSTED_INFRASTRUCTURE,
        "request_id": None,
        "source": TrustedSource.WORKER,
        "issued_at": now,
        "expires_at": now + timedelta(minutes=1),
    }
    fields.update(overrides)
    TenantContext(**fields)  # type: ignore[arg-type]


def test_context_cannot_be_constructed_by_a_caller() -> None:
    with pytest.raises(ContextInvalid) as error:
        _construct()
    assert error.value.reason_code == "UNTRUSTED_CONTEXT_CONSTRUCTION"


def test_resolved_context_is_immutable(active_context: TenantContext) -> None:
    with pytest.raises(FrozenInstanceError):
        active_context.tenant_id = "clinic-b"  # type: ignore[misc]


def test_context_rejects_use_after_expiry(expired_context: TenantContext) -> None:
    with pytest.raises(ContextExpired):
        expired_context.assert_usable()


def test_issued_context_exposes_execution_id_and_no_operation_id(
    active_context: TenantContext,
) -> None:
    """TC-A3. `slots=True` makes the removal of the old name enforceable."""

    assert active_context.execution_id == uuid5(NAMESPACE_URL, "haloflow-test:fixture")
    with pytest.raises(AttributeError):
        _ = active_context.operation_id  # type: ignore[attr-defined]


def test_context_preserves_correlation_value_and_provenance(
    active_context: TenantContext,
) -> None:
    """TC-B5."""

    assert active_context.correlation_id == uuid5(
        NAMESPACE_URL, "haloflow-test:fixture-correlation"
    )
    assert active_context.correlation_source is CorrelationSource.TRUSTED_INFRASTRUCTURE


def test_request_id_remains_optional_and_separate_from_correlation(
    active_context: TenantContext,
) -> None:
    """TC-B6."""

    assert active_context.request_id is None
    assert active_context.correlation_id is not None


def test_principal_requires_identity() -> None:
    with pytest.raises(ContextInvalid) as error:
        Principal(
            kind=PrincipalKind.WORKLOAD,
            id="",
            auth_method="test",
            authorized_tenant_ids=frozenset(),
            capabilities=frozenset(),
        )
    assert error.value.reason_code == "PRINCIPAL_INVALID"
