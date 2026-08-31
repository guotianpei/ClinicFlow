from collections.abc import Awaitable, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.errors import TenantDenied, TenantUnavailable
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver

pytestmark = pytest.mark.asyncio

Resolve = Callable[..., Awaitable[TenantContext]]
MakePrincipal = Callable[..., Principal]
MakeResolver = Callable[..., TenantResolver]
MakeControlStore = Callable[..., object]


# --- registry and request shape (pre-existing behaviour, preserved) ---


async def test_unknown_tenant_is_denied(resolve: Resolve, principal_with: MakePrincipal) -> None:
    with pytest.raises(TenantDenied) as error:
        await resolve(principal=principal_with("appointments:read"), tenant_hint="clinic-zzz")
    assert error.value.reason_code == "TENANT_BINDING_MISMATCH"


async def test_tenant_not_in_registry_is_denied(
    resolve: Resolve, make_control_store: MakeControlStore
) -> None:
    with pytest.raises(TenantDenied) as error:
        await resolve(store=make_control_store(None))
    assert error.value.reason_code == "TENANT_NOT_AUTHORIZED"


async def test_inactive_tenant_is_unavailable(
    resolve: Resolve, make_control_store: MakeControlStore
) -> None:
    suspended = TenantRegistryRecord(
        tenant_id="clinic-a",
        schema_key="tenant_aaaaaaaa",
        lifecycle_state=LifecycleState.SUSPENDED,
        schema_version=1,
    )
    with pytest.raises(TenantUnavailable) as error:
        await resolve(store=make_control_store(suspended))
    assert error.value.reason_code == "TENANT_NOT_ACTIVE"


# --- D7: purpose validation is unchanged, and now has regression cover ---


async def test_purpose_outside_the_allowed_set_is_denied(resolve: Resolve) -> None:
    with pytest.raises(TenantDenied) as error:
        await resolve(purpose="marketing")
    assert error.value.reason_code == "PURPOSE_DENIED"


@pytest.mark.parametrize("purpose", ["treatment", "payment", "operations"])
async def test_default_purposes_remain_accepted(resolve: Resolve, purpose: str) -> None:
    context = await resolve(purpose=purpose)
    assert context.purpose == purpose


async def test_malformed_purpose_is_denied(resolve: Resolve) -> None:
    with pytest.raises(TenantDenied) as error:
        await resolve(purpose="TREATMENT")
    assert error.value.reason_code == "PURPOSE_DENIED"


# --- A: execution_id is a typed UUID (D1) ---


async def test_string_execution_id_is_rejected_at_runtime(
    resolve: Resolve, execution_id: UUID
) -> None:
    """TC-A1. The guard does not rely on static typing alone."""

    with pytest.raises(TenantDenied) as error:
        await resolve(execution_id=str(execution_id))
    assert error.value.reason_code == "EXECUTION_ID_INVALID"


@pytest.mark.parametrize("value", [None, 42, b"bytes", object()])
async def test_non_uuid_execution_ids_are_rejected(resolve: Resolve, value: object) -> None:
    """TC-A2."""

    with pytest.raises(TenantDenied) as error:
        await resolve(execution_id=value)
    assert error.value.reason_code == "EXECUTION_ID_INVALID"


async def test_execution_id_renders_canonically_regardless_of_caller_spelling(
    resolve: Resolve,
) -> None:
    """TC-A2b. Canonical identity now holds by type, not by string validation."""

    canonical = uuid5(NAMESPACE_URL, "haloflow-test:canonical")
    spellings = (
        UUID(str(canonical).upper()),
        UUID("{" + str(canonical) + "}"),
        UUID(str(canonical).replace("-", "")),
    )
    for spelling in spellings:
        context = await resolve(execution_id=spelling)
        assert str(context.execution_id) == str(canonical)


async def test_one_execution_id_may_span_several_tenant_contexts(resolve: Resolve) -> None:
    """TC-A5. M06 batch fan-out; M01 enforces no uniqueness."""

    shared_execution = uuid4()
    first = await resolve(execution_id=shared_execution)
    second = await resolve(execution_id=shared_execution)

    assert first.execution_id == second.execution_id == shared_execution


# --- B: correlation contract (FR-031) ---


@pytest.mark.parametrize("value", [None, "not-a-uuid", "6f1c8f1e-0000-4000-8000-000000000000", 7])
async def test_non_uuid_correlation_ids_are_rejected(resolve: Resolve, value: object) -> None:
    """TC-B2. A string is never coerced, parsed leniently, or hashed."""

    with pytest.raises(TenantDenied) as error:
        await resolve(correlation_id=value)
    assert error.value.reason_code == "CORRELATION_ID_INVALID"


@pytest.mark.parametrize("value", ["trusted_infrastructure", None, "guessed"])
async def test_correlation_source_outside_the_vocabulary_is_rejected(
    resolve: Resolve, value: object
) -> None:
    """TC-B3. The controlled value, not merely its spelling."""

    with pytest.raises(TenantDenied) as error:
        await resolve(correlation_source=value)
    assert error.value.reason_code == "CORRELATION_SOURCE_INVALID"


@pytest.mark.parametrize(
    "provenance",
    [CorrelationSource.TRUSTED_INFRASTRUCTURE, CorrelationSource.ENTRY_POINT_GENERATED],
)
async def test_correlation_value_and_provenance_are_preserved(
    resolve: Resolve, provenance: CorrelationSource
) -> None:
    """TC-B5."""

    correlation = uuid4()
    context = await resolve(correlation_id=correlation, correlation_source=provenance)

    assert context.correlation_id == correlation
    assert context.correlation_source is provenance


async def test_resolve_requires_correlation_arguments(
    make_resolver: MakeResolver, principal_with: MakePrincipal, execution_id: UUID
) -> None:
    """TC-B1. Required, not defaulted -- at runtime, not only under mypy."""

    with pytest.raises(TypeError):
        await make_resolver().resolve(  # type: ignore[call-arg]
            principal=principal_with("appointments:read"),
            tenant_hint="clinic-a",
            purpose="treatment",
            capabilities=frozenset({"appointments:read"}),
            source=TrustedSource.WORKER,
            execution_id=execution_id,
        )


# --- C: multi-capability issuance (F1) ---


async def test_requested_capability_subset_is_issued_exactly(
    resolve: Resolve, principal_with: MakePrincipal
) -> None:
    """TC-C1."""

    context = await resolve(
        principal=principal_with("a:read", "b:write", "c:admin"),
        capabilities=frozenset({"a:read", "b:write"}),
    )
    assert context.capabilities == frozenset({"a:read", "b:write"})


async def test_capability_not_held_denies_the_whole_request(
    resolve: Resolve, principal_with: MakePrincipal
) -> None:
    """TC-C2. No partial issuance."""

    with pytest.raises(TenantDenied) as error:
        await resolve(
            principal=principal_with("a:read"), capabilities=frozenset({"a:read", "b:write"})
        )
    assert error.value.reason_code == "CAPABILITY_DENIED"


async def test_context_never_inherits_the_principals_other_capabilities(
    resolve: Resolve, principal_with: MakePrincipal
) -> None:
    """TC-C3."""

    context = await resolve(
        principal=principal_with("a:read", "b:write", "c:admin"),
        capabilities=frozenset({"a:read"}),
    )
    assert context.capabilities == frozenset({"a:read"})


async def test_empty_capability_set_is_a_request_shape_error(resolve: Resolve) -> None:
    """TC-C4 / D9. Not CAPABILITY_DENIED: the principal's grants are not at fault."""

    with pytest.raises(TenantDenied) as error:
        await resolve(capabilities=frozenset())
    assert error.value.reason_code == "CAPABILITIES_EMPTY"


async def test_capability_cannot_be_injected_into_an_issued_context(
    resolve: Resolve, principal_with: MakePrincipal
) -> None:
    """TC-C8. The frozenset is immutable and the field cannot be rebound."""

    context = await resolve(
        principal=principal_with("a:read", "b:write"), capabilities=frozenset({"a:read"})
    )

    with pytest.raises(AttributeError):
        context.capabilities |= {"b:write"}  # type: ignore[misc]
    assert not hasattr(context.capabilities, "add")
    assert context.capabilities == frozenset({"a:read"})


# --- request shape is validated before authorization (ChatGPT review, answer 1) ---


@pytest.mark.parametrize("container", [["a:read"], ("a:read",), "a:read", {"a:read": 1}, None])
async def test_malformed_capability_container_is_a_sanitized_denial(
    resolve: Resolve, principal_with: MakePrincipal, container: object
) -> None:
    """A list or string would reach `<= frozenset` and raise a raw TypeError.

    Shape is validated before the authorization comparison, so this is a
    sanitized TenantDenied like every other boundary rejection.
    """

    with pytest.raises(TenantDenied) as error:
        await resolve(principal=principal_with("a:read"), capabilities=container)
    assert error.value.reason_code in {"CAPABILITIES_INVALID", "CAPABILITIES_EMPTY"}


async def test_non_string_capability_members_are_rejected(
    resolve: Resolve, principal_with: MakePrincipal
) -> None:
    with pytest.raises(TenantDenied) as error:
        await resolve(principal=principal_with("a:read"), capabilities=frozenset({1, 2}))
    assert error.value.reason_code == "CAPABILITIES_INVALID"


async def test_request_shape_is_reported_before_authorization(
    resolve: Resolve, principal_with: MakePrincipal, execution_id: UUID
) -> None:
    """A request that is both malformed and unauthorized reports the malformation.

    The operator is told the request is wrong, not that the principal's grants
    are wrong, which is the more actionable of the two.
    """

    with pytest.raises(TenantDenied) as error:
        await resolve(
            principal=principal_with("a:read"),
            capabilities=frozenset({"never:granted"}),
            execution_id=str(execution_id),
        )
    assert error.value.reason_code == "EXECUTION_ID_INVALID"


async def test_resolved_context_carries_the_fixture_defaults(resolve: Resolve) -> None:
    context = await resolve()
    assert context.tenant_id == "clinic-a"
    assert context.schema_key == "tenant_aaaaaaaa"
    assert isinstance(context.execution_id, UUID)
    assert isinstance(context.correlation_id, UUID)
