from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from conftest import (
    FIXTURE_CORRELATION_ID,
    FIXTURE_EXECUTION_ID,
    SingleTenantControlStore,
    principal_with,
    resolve_context,
)

from haloflow.m01.context import CorrelationSource, TrustedSource
from haloflow.m01.errors import TenantDenied, TenantUnavailable
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver

pytestmark = pytest.mark.asyncio


class ConfigurableControlStore:
    def __init__(self, record: TenantRegistryRecord | None) -> None:
        self._record = record

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        return self._record


def _resolver(store: object | None = None) -> TenantResolver:
    return TenantResolver(
        store or SingleTenantControlStore(),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=60),
        clock=lambda: datetime.now(UTC),
    )


async def _resolve(**overrides: object) -> object:
    kwargs: dict[str, object] = {
        "principal": principal_with("appointments:read"),
        "tenant_hint": "clinic-a",
        "purpose": "treatment",
        "capabilities": frozenset({"appointments:read"}),
        "source": TrustedSource.WORKER,
        "execution_id": FIXTURE_EXECUTION_ID,
        "correlation_id": FIXTURE_CORRELATION_ID,
        "correlation_source": CorrelationSource.TRUSTED_INFRASTRUCTURE,
    }
    kwargs.update(overrides)
    return await _resolver().resolve(**kwargs)  # type: ignore[arg-type]


# --- registry and request shape (pre-existing behaviour, preserved) ---


async def test_unknown_tenant_is_denied() -> None:
    with pytest.raises(TenantDenied) as error:
        await _resolve(
            principal=principal_with("appointments:read"),
            tenant_hint="clinic-zzz",
        )
    assert error.value.reason_code == "TENANT_BINDING_MISMATCH"


async def test_tenant_not_in_registry_is_denied() -> None:
    resolver = _resolver(ConfigurableControlStore(None))
    with pytest.raises(TenantDenied) as error:
        await resolver.resolve(
            principal=principal_with("appointments:read"),
            tenant_hint="clinic-a",
            purpose="treatment",
            capabilities=frozenset({"appointments:read"}),
            source=TrustedSource.WORKER,
            execution_id=FIXTURE_EXECUTION_ID,
            correlation_id=FIXTURE_CORRELATION_ID,
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
        )
    assert error.value.reason_code == "TENANT_NOT_AUTHORIZED"


async def test_inactive_tenant_is_unavailable() -> None:
    resolver = _resolver(
        ConfigurableControlStore(
            TenantRegistryRecord(
                tenant_id="clinic-a",
                schema_key="tenant_aaaaaaaa",
                lifecycle_state=LifecycleState.SUSPENDED,
                schema_version=1,
            )
        )
    )
    with pytest.raises(TenantUnavailable) as error:
        await resolver.resolve(
            principal=principal_with("appointments:read"),
            tenant_hint="clinic-a",
            purpose="treatment",
            capabilities=frozenset({"appointments:read"}),
            source=TrustedSource.WORKER,
            execution_id=FIXTURE_EXECUTION_ID,
            correlation_id=FIXTURE_CORRELATION_ID,
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
        )
    assert error.value.reason_code == "TENANT_NOT_ACTIVE"


# --- D7: purpose validation is unchanged, and now has regression cover ---


async def test_purpose_outside_the_allowed_set_is_denied() -> None:
    with pytest.raises(TenantDenied) as error:
        await _resolve(purpose="marketing")
    assert error.value.reason_code == "PURPOSE_DENIED"


@pytest.mark.parametrize("purpose", ["treatment", "payment", "operations"])
async def test_default_purposes_remain_accepted(purpose: str) -> None:
    context = await _resolve(purpose=purpose)
    assert context.purpose == purpose  # type: ignore[attr-defined]


async def test_malformed_purpose_is_denied() -> None:
    with pytest.raises(TenantDenied) as error:
        await _resolve(purpose="TREATMENT")
    assert error.value.reason_code == "PURPOSE_DENIED"


# --- A: execution_id is a typed UUID (D1) ---


async def test_string_execution_id_is_rejected_at_runtime() -> None:
    """TC-A1. The guard does not rely on static typing alone."""

    with pytest.raises(TenantDenied) as error:
        await _resolve(execution_id=str(FIXTURE_EXECUTION_ID))
    assert error.value.reason_code == "EXECUTION_ID_INVALID"


@pytest.mark.parametrize("value", [None, 42, b"bytes", object()])
async def test_non_uuid_execution_ids_are_rejected(value: object) -> None:
    """TC-A2."""

    with pytest.raises(TenantDenied) as error:
        await _resolve(execution_id=value)
    assert error.value.reason_code == "EXECUTION_ID_INVALID"


async def test_execution_id_renders_canonically_regardless_of_caller_spelling() -> None:
    """TC-A2b. Canonical identity now holds by type, not by string validation."""

    canonical = uuid5(NAMESPACE_URL, "haloflow-test:canonical")
    from_upper = UUID(str(canonical).upper())
    from_braced = UUID("{" + str(canonical) + "}")
    from_bare = UUID(str(canonical).replace("-", ""))

    for spelling in (from_upper, from_braced, from_bare):
        context = await _resolve(execution_id=spelling)
        assert str(context.execution_id) == str(canonical)  # type: ignore[attr-defined]


async def test_one_execution_id_may_span_several_tenant_contexts() -> None:
    """TC-A5. M06 batch fan-out; M01 enforces no uniqueness."""

    shared_execution = uuid4()
    first = await _resolve(execution_id=shared_execution)
    second = await _resolve(execution_id=shared_execution)

    assert first.execution_id == second.execution_id == shared_execution  # type: ignore[attr-defined]


# --- B: correlation contract (FR-031) ---


@pytest.mark.parametrize("value", [None, "not-a-uuid", str(uuid4()), 7])
async def test_non_uuid_correlation_ids_are_rejected(value: object) -> None:
    """TC-B2. A string is never coerced, parsed leniently, or hashed."""

    with pytest.raises(TenantDenied) as error:
        await _resolve(correlation_id=value)
    assert error.value.reason_code == "CORRELATION_ID_INVALID"


@pytest.mark.parametrize("value", ["trusted_infrastructure", None, "guessed"])
async def test_correlation_source_outside_the_vocabulary_is_rejected(value: object) -> None:
    """TC-B3. The controlled value, not merely its spelling."""

    with pytest.raises(TenantDenied) as error:
        await _resolve(correlation_source=value)
    assert error.value.reason_code == "CORRELATION_SOURCE_INVALID"


@pytest.mark.parametrize(
    "provenance",
    [CorrelationSource.TRUSTED_INFRASTRUCTURE, CorrelationSource.ENTRY_POINT_GENERATED],
)
async def test_correlation_value_and_provenance_are_preserved(
    provenance: CorrelationSource,
) -> None:
    """TC-B5."""

    correlation = uuid4()
    context = await _resolve(correlation_id=correlation, correlation_source=provenance)

    assert context.correlation_id == correlation  # type: ignore[attr-defined]
    assert context.correlation_source is provenance  # type: ignore[attr-defined]


# --- C: multi-capability issuance (F1) ---


async def test_requested_capability_subset_is_issued_exactly() -> None:
    """TC-C1."""

    context = await _resolve(
        principal=principal_with("a:read", "b:write", "c:admin"),
        capabilities=frozenset({"a:read", "b:write"}),
    )
    assert context.capabilities == frozenset({"a:read", "b:write"})  # type: ignore[attr-defined]


async def test_capability_not_held_denies_the_whole_request() -> None:
    """TC-C2. No partial issuance."""

    with pytest.raises(TenantDenied) as error:
        await _resolve(
            principal=principal_with("a:read"),
            capabilities=frozenset({"a:read", "b:write"}),
        )
    assert error.value.reason_code == "CAPABILITY_DENIED"


async def test_context_never_inherits_the_principals_other_capabilities() -> None:
    """TC-C3."""

    context = await _resolve(
        principal=principal_with("a:read", "b:write", "c:admin"),
        capabilities=frozenset({"a:read"}),
    )
    assert context.capabilities == frozenset({"a:read"})  # type: ignore[attr-defined]


async def test_empty_capability_set_is_a_request_shape_error() -> None:
    """TC-C4 / D9. Not CAPABILITY_DENIED: the principal's grants are not at fault."""

    with pytest.raises(TenantDenied) as error:
        await _resolve(capabilities=frozenset())
    assert error.value.reason_code == "CAPABILITIES_EMPTY"


async def test_resolved_context_carries_the_fixture_defaults() -> None:
    context = await resolve_context()
    assert context.tenant_id == "clinic-a"
    assert context.schema_key == "tenant_aaaaaaaa"
    assert isinstance(context.execution_id, UUID)
    assert isinstance(context.correlation_id, UUID)


async def test_resolve_requires_correlation_arguments() -> None:
    """TC-B1. Required, not defaulted -- at runtime, not only under mypy."""

    resolver = _resolver()
    with pytest.raises(TypeError):
        await resolver.resolve(  # type: ignore[call-arg]
            principal=principal_with("appointments:read"),
            tenant_hint="clinic-a",
            purpose="treatment",
            capabilities=frozenset({"appointments:read"}),
            source=TrustedSource.WORKER,
            execution_id=FIXTURE_EXECUTION_ID,
        )


async def test_capability_cannot_be_injected_into_an_issued_context() -> None:
    """TC-C8. The frozenset is immutable and the field cannot be rebound."""

    context = await _resolve(
        principal=principal_with("a:read", "b:write"),
        capabilities=frozenset({"a:read"}),
    )

    with pytest.raises(AttributeError):
        context.capabilities |= {"b:write"}  # type: ignore[misc]
    assert not hasattr(context.capabilities, "add")
    assert context.capabilities == frozenset({"a:read"})
