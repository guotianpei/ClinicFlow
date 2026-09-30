"""L-1 runtime composition: static cases (no database).

Traceability: L-1 test cases v2 + addendum 1 (owner-approved 2026-09-29), plus
C1f (owner ruling IP-5). Requirements v2 + errata 1 and 2; architecture v1 +
addenda 1 and 2. Every case id appears in its test's name.

Rules (implementation plan v2 section 7):
- The new interface is imported INSIDE each test, so that at the pre-change
  baseline each case fails on its own (structural absence), never as one
  collection error. Structural failures are not behavioural evidence.
- Constructor spies wrap the four classes' ``__init__`` and keep only weak
  references plus argument values; no strong reference to a component.
- Refusals are caught with plain try/except (not ``pytest.raises``), tracebacks
  are cleared after the assertions, and ``gc.collect()`` runs outside the except
  scope. This is an object-retention check, not proof against every reference
  escape.
- No database: dependencies are recording doubles; every counter must be zero.
"""

from __future__ import annotations

import gc
import inspect
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TrustedSource,
)
from haloflow.m01.errors import (
    M01Error,
    MigrationManifestRejected,
    MigrationUnitRejected,
    RepositoryStatementRejected,
    TenantUnavailable,
)
from haloflow.m01.gateway import TenantTransactionGateway
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.provisioner import TenantProvisioner
from haloflow.m01.provisioning.runner import TenantMigrationRunner
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver

PUBLIC_MESSAGE = "Tenant operation unavailable"
UNSUPPORTED = PreconditionCode.SCHEMA_VERSION_UNSUPPORTED.value
COMPONENT_CLASSES = (
    TenantMigrationRunner,
    TenantResolver,
    TenantTransactionGateway,
    TenantProvisioner,
)


# --- recording doubles (no database) ---------------------------------------


@dataclass
class Counters:
    pool: int = 0
    control: int = 0
    migrator: int = 0
    provisioner: int = 0

    def total(self) -> int:
        return self.pool + self.control + self.migrator + self.provisioner


class RecordingPool:
    """Stands in for TenantPool. Any use that could reach a database counts."""

    def __init__(self, counters: Counters) -> None:
        self._counters = counters

    async def open(self) -> None:
        self._counters.pool += 1

    async def close(self) -> None:
        self._counters.pool += 1

    def _connection_for_gateway(self) -> Any:
        self._counters.pool += 1
        raise AssertionError("pool used during composition")

    def _connection_for_control(self) -> Any:
        self._counters.pool += 1
        raise AssertionError("pool used during composition")


class RecordingControlStore:
    def __init__(
        self, counters: Counters, record: TenantRegistryRecord | None = None
    ) -> None:
        self._counters = counters
        self._record = record

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        self._counters.control += 1
        return self._record


def _connect(counters: Counters, name: str) -> Callable[[], Any]:
    async def _factory() -> Any:
        setattr(counters, name, getattr(counters, name) + 1)
        raise AssertionError(f"{name} connect factory used during composition")

    return _factory


def _dependencies(
    counters: Counters, record: TenantRegistryRecord | None = None
) -> Any:
    from haloflow.m01.runtime import TenantRuntimeDependencies

    return TenantRuntimeDependencies(
        pool=RecordingPool(counters),  # type: ignore[arg-type]
        control_store=RecordingControlStore(counters, record),
        migrator_connect=_connect(counters, "migrator"),
        provisioner_connect=_connect(counters, "provisioner"),
    )


# --- constructor spies ------------------------------------------------------


@dataclass
class Spy:
    order: list[str] = field(default_factory=list)
    refs: list[weakref.ref[Any]] = field(default_factory=list)
    set_args: dict[str, list[Any]] = field(default_factory=dict)
    set_kw_passed: dict[str, list[bool]] = field(default_factory=dict)
    runner_args: list[weakref.ref[Any]] = field(default_factory=list)

    def forget_values(self) -> None:
        """Drop strong references held for assertions; weakrefs stay."""

        self.set_args.clear()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[Spy]:
    record = Spy()
    for cls in COMPONENT_CLASSES:
        original = cls.__init__

        def wrapped(
            self: Any,
            *args: Any,
            __original: Any = original,
            __name: str = cls.__name__,
            **kwargs: Any,
        ) -> None:
            record.order.append(__name)
            record.set_kw_passed.setdefault(__name, []).append(
                "supported_schema_versions" in kwargs
            )
            if "supported_schema_versions" in kwargs:
                record.set_args.setdefault(__name, []).append(
                    kwargs["supported_schema_versions"]
                )
            if __name == "TenantProvisioner":
                runner = kwargs["runner"] if "runner" in kwargs else args[1]
                record.runner_args.append(weakref.ref(runner))
            record.refs.append(weakref.ref(self))
            __original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", wrapped)
    yield record


def _assert_no_retained_component(spy: Spy) -> None:
    """Bounded object-retention oracle (addendum 1 R2). Call outside ``except``."""

    spy.forget_values()
    gc.collect()
    alive = [ref() for ref in spy.refs if ref() is not None]
    assert alive == [], (
        f"component retained after refusal: {[type(a).__name__ for a in alive]}"
    )


def _refuse(call: Callable[[], Any]) -> Exception:
    """Run ``call``; return the exception with its traceback cleared."""

    try:
        call()
    except Exception as error:  # classified by the caller
        error.__traceback__ = None
        return error
    raise AssertionError("expected a refusal; composition succeeded")


def _generic(counters: Counters, **overrides: Any) -> Callable[[], Any]:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_catalog,
        build_production_tenant_migrations,
    )
    from haloflow.m01.runtime import compose_tenant_runtime

    kwargs: dict[str, Any] = {
        "registry": build_production_tenant_migrations(),
        "catalog": build_production_catalog(),
        "supported_schema_versions": APPROVED_SUPPORTED_SCHEMA_VERSIONS,
    }
    kwargs.update(overrides)
    deps = _dependencies(counters)
    return lambda: compose_tenant_runtime(deps, **kwargs)


# --- section 2: construction, success path ---------------------------------


def test_l1_b1_each_call_returns_a_new_bundle(spy: Spy) -> None:
    from haloflow.composition import build_production_tenant_runtime
    from haloflow.m01.runtime import TenantRuntime

    counters = Counters()
    first = build_production_tenant_runtime(_dependencies(counters))
    second = build_production_tenant_runtime(_dependencies(counters))

    assert isinstance(first, TenantRuntime) and isinstance(second, TenantRuntime)
    assert first is not second
    assert first.resolver is not second.resolver
    assert first.gateway is not second.gateway
    assert first.provisioner is not second.provisioner


def test_l1_b2_one_declaration_object_reaches_every_consumer(spy: Spy) -> None:
    from haloflow import composition

    runtime = composition.build_production_tenant_runtime(_dependencies(Counters()))
    declaration = composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS

    # Wiring oracle: the one declaration object is the argument to all three
    # constructors and the bundle's own field.
    for name in ("TenantResolver", "TenantTransactionGateway", "TenantProvisioner"):
        assert len(spy.set_args[name]) == 1, name
        assert spy.set_args[name][0] is declaration, name
    assert runtime.supported_schema_versions is declaration

    # Stored-value oracle (separate): equality only, no reliance on copy identity.
    assert runtime.resolver._supported_schema_versions == frozenset({3})
    assert runtime.gateway._supported_schema_versions == frozenset({3})
    assert runtime.provisioner._supported_schema_versions == frozenset({3})


def test_l1_b3_the_gateway_set_is_passed_explicitly(spy: Spy) -> None:
    from haloflow.composition import build_production_tenant_runtime

    build_production_tenant_runtime(_dependencies(Counters()))

    assert spy.set_kw_passed["TenantTransactionGateway"] == [True]
    assert spy.set_args["TenantTransactionGateway"][0] == frozenset({3})
    assert spy.set_args["TenantTransactionGateway"][0] != frozenset(range(1, 2))


def test_l1_b4_the_runner_is_constructed_first_and_shared(spy: Spy) -> None:
    from haloflow.composition import build_production_tenant_runtime

    runtime = build_production_tenant_runtime(_dependencies(Counters()))

    assert spy.order[0] == "TenantMigrationRunner"
    assert sorted(spy.order[1:]) == [
        "TenantProvisioner",
        "TenantResolver",
        "TenantTransactionGateway",
    ]
    assert len(spy.runner_args) == 1
    assert spy.runner_args[0]() is runtime.provisioner._runner


def test_l1_b5_no_database_access_across_the_whole_path(spy: Spy) -> None:
    from haloflow.composition import build_production_tenant_runtime

    counters = Counters()
    build_production_tenant_runtime(_dependencies(counters))

    assert counters.total() == 0, counters


def test_l1_b6_g1_the_production_entry_takes_only_dependencies() -> None:
    from haloflow.composition import build_production_tenant_runtime

    parameters = list(
        inspect.signature(build_production_tenant_runtime).parameters.values()
    )

    assert [p.name for p in parameters] == ["dependencies"]
    assert all(p.kind is not inspect.Parameter.VAR_KEYWORD for p in parameters)


# --- section 3: the two new refusals ---------------------------------------

INVALID_DECLARATIONS = [
    pytest.param(frozenset(), id="C1a-empty"),
    pytest.param(frozenset({3, "3"}), id="C1b-non-int"),
    pytest.param(frozenset({True}), id="C1c-bool"),
    pytest.param(frozenset({0}), id="C1d-zero"),
    pytest.param(frozenset({-3}), id="C1d-negative"),
    pytest.param({3}, id="C1e-plain-set"),
    pytest.param(range(3, 4), id="C1e-range"),
    pytest.param(frozenset({3, True}), id="C1f-three-and-bool"),
    pytest.param(frozenset({3, 0}), id="C1f-three-and-zero"),
    pytest.param(frozenset({3, -3}), id="C1f-three-and-negative"),
]


def _assert_new_check_refusal(error: Exception, spy: Spy, counters: Counters) -> None:
    assert isinstance(error, MigrationUnitRejected), type(error)
    assert error.reason_code == UNSUPPORTED
    assert str(error) == PUBLIC_MESSAGE
    assert spy.order == [], f"constructed before the static checks: {spy.order}"
    assert counters.total() == 0, counters


@pytest.mark.parametrize("declared", INVALID_DECLARATIONS)
def test_l1_c1_an_invalid_declaration_is_refused(spy: Spy, declared: Any) -> None:
    counters = Counters()
    error = _refuse(_generic(counters, supported_schema_versions=declared))
    _assert_new_check_refusal(error, spy, counters)
    del error
    _assert_no_retained_component(spy)


def test_l1_c2_a_registry_target_outside_the_set_is_refused(spy: Spy) -> None:
    counters = Counters()
    error = _refuse(_generic(counters, supported_schema_versions=frozenset({1, 2})))
    _assert_new_check_refusal(error, spy, counters)
    del error
    _assert_no_retained_component(spy)


def test_l1_c3_the_production_entry_reaches_the_coordination_check(
    spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Static substitution confined to this test; not a production path."""

    from haloflow import composition

    monkeypatch.setattr(
        composition, "APPROVED_SUPPORTED_SCHEMA_VERSIONS", frozenset({4})
    )
    counters = Counters()
    deps = _dependencies(counters)
    error = _refuse(lambda: composition.build_production_tenant_runtime(deps))
    _assert_new_check_refusal(error, spy, counters)
    del error
    _assert_no_retained_component(spy)


@dataclass
class BuilderCalls:
    count: int = 0


@pytest.fixture
def builder_calls(monkeypatch: pytest.MonkeyPatch) -> BuilderCalls:
    """Records calls to the generic builder without changing its behaviour.

    Wrapped at both lookup locations the production entry can use: the runtime
    module attribute and, if composition binds the name itself, the composition
    module attribute. The wrapper calls through, so it is never the source of an
    expected error.
    """

    from haloflow import composition
    from haloflow.m01 import runtime

    calls = BuilderCalls()
    original = runtime.compose_tenant_runtime

    def recording(*args: Any, **kwargs: Any) -> Any:
        calls.count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "compose_tenant_runtime", recording)
    if hasattr(composition, "compose_tenant_runtime"):
        monkeypatch.setattr(composition, "compose_tenant_runtime", recording)
    return calls


def test_l1_d0_the_builder_spy_observes_the_production_entry(
    builder_calls: BuilderCalls,
) -> None:
    """Positive control for D1/D2: zero calls there is not a blind spy."""

    from haloflow.composition import build_production_tenant_runtime

    build_production_tenant_runtime(_dependencies(Counters()))
    assert builder_calls.count == 1


# --- section 4: existing refusal outcomes kept distinct ---------------------


def test_l1_d1_a_registry_failure_keeps_its_own_code(
    spy: Spy, monkeypatch: pytest.MonkeyPatch, builder_calls: BuilderCalls
) -> None:
    from haloflow import composition
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, UnitDefinition

    unapproved = {
        "t009_m09_probe": UnitDefinition(
            "SELECT 1;", execution_role="haloflow_m09_other_owner"
        )
    }
    monkeypatch.setattr(
        composition, "APPROVED_TENANT_MIGRATIONS", (TENANT_MIGRATIONS, unapproved)
    )
    counters = Counters()
    deps = _dependencies(counters)
    error = _refuse(lambda: composition.build_production_tenant_runtime(deps))

    assert type(error) is MigrationUnitRejected
    assert error.reason_code == PreconditionCode.EXECUTION_ROLE_NOT_APPROVED.value
    assert builder_calls.count == 0, "the generic builder was called"
    assert spy.order == [], "a component was constructed"
    assert counters.total() == 0
    del error
    _assert_no_retained_component(spy)


def test_l1_d1b_an_empty_registry_surfaces_its_own_code_at_step_two(spy: Spy) -> None:
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    counters = Counters()
    error = _refuse(_generic(counters, registry=build_tenant_migration_registry()))

    assert type(error) is MigrationUnitRejected
    assert error.reason_code == PreconditionCode.MIGRATION_REGISTRY_EMPTY.value
    assert error.reason_code != UNSUPPORTED
    assert spy.order == []
    assert counters.total() == 0
    del error
    _assert_no_retained_component(spy)


def test_l1_d2_a_catalogue_failure_keeps_its_own_outcome(
    spy: Spy, monkeypatch: pytest.MonkeyPatch, builder_calls: BuilderCalls
) -> None:
    from haloflow import composition
    from haloflow.m01.statements import StatementMode

    unprefixed = {"l1_bad.key": (StatementMode.READ, "probe:read", "SELECT 1")}
    monkeypatch.setattr(composition, "APPROVED_MODULE_STATEMENTS", (unprefixed,))
    counters = Counters()
    deps = _dependencies(counters)
    error = _refuse(lambda: composition.build_production_tenant_runtime(deps))

    assert type(error) is RepositoryStatementRejected
    assert error.reason_code == "STATEMENT_KEY_NOT_MODULE_PREFIXED"
    assert builder_calls.count == 0, "the generic builder was called"
    assert spy.order == [], "a component was constructed"
    assert counters.total() == 0
    del error
    _assert_no_retained_component(spy)


def test_l1_d3_a_manifest_failure_is_the_only_manifest_rejection(
    spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from haloflow.m01.provisioning import manifest as manifest_module

    def _malformed(document: Any = None) -> Any:
        raise MigrationManifestRejected(
            reason_code=PreconditionCode.PROVISIONING_MANIFEST_MALFORMED.value
        )

    monkeypatch.setattr(manifest_module, "load_provisioning_manifest", _malformed)
    counters = Counters()
    error = _refuse(_generic(counters))

    assert type(error) is MigrationManifestRejected
    assert error.reason_code == PreconditionCode.PROVISIONING_MANIFEST_MALFORMED.value
    assert spy.order == ["TenantMigrationRunner"], spy.order
    assert counters.total() == 0
    del error
    _assert_no_retained_component(spy)


class _SentinelFailure(M01Error):
    code = "L1_TEST_SENTINEL"


def test_l1_d4_a_later_constructor_failure_propagates_unchanged(
    spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable only through a double: no real constructor here raises today."""

    sentinel = _SentinelFailure(reason_code="L1_TEST_SENTINEL")
    wrapped_gateway_init = TenantTransactionGateway.__init__

    def _raise_in_gateway(self: Any, *args: Any, **kwargs: Any) -> None:
        wrapped_gateway_init(self, *args, **kwargs)
        raise sentinel

    monkeypatch.setattr(TenantTransactionGateway, "__init__", _raise_in_gateway)
    counters = Counters()
    error = _refuse(_generic(counters))

    assert error is sentinel
    assert error.__cause__ is None
    assert error.__context__ is None
    assert (
        "TenantMigrationRunner" in spy.order and "TenantTransactionGateway" in spy.order
    )
    assert counters.total() == 0
    del error
    sentinel.__traceback__ = None
    _assert_no_retained_component(spy)


# --- composed resolver keeps its existing outcomes (Q-4 ruling) ------------


def _principal(tenant_id: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="l1-test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset({tenant_id}),
        capabilities=frozenset({"probe:read"}),
    )


def _resolve(runtime: Any, tenant_id: str) -> Any:
    return runtime.resolver.resolve(
        principal=_principal(tenant_id),
        tenant_hint=tenant_id,
        purpose="operations",
        capabilities=frozenset({"probe:read"}),
        source=TrustedSource.WORKER,
        execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:l1:{tenant_id}"),
        correlation_id=uuid5(NAMESPACE_URL, f"haloflow-test:l1-corr:{tenant_id}"),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
    )


async def test_l1_e4c_composed_resolver_refuses_a_registry_tenant_mismatch() -> None:
    from haloflow.composition import build_production_tenant_runtime

    record = TenantRegistryRecord(
        tenant_id="clinic-l1-other",
        schema_key="tenant_l1e4cxxx",
        lifecycle_state=LifecycleState.ACTIVE,
        schema_version=3,
    )
    runtime = build_production_tenant_runtime(_dependencies(Counters(), record))

    with pytest.raises(TenantUnavailable) as refused:
        await _resolve(runtime, "clinic-l1-e4c")
    assert refused.value.reason_code == "REGISTRY_TENANT_MISMATCH"


async def test_l1_e4d_composed_resolver_refuses_an_invalid_schema_key() -> None:
    from haloflow.composition import build_production_tenant_runtime

    record = TenantRegistryRecord(
        tenant_id="clinic-l1-e4d",
        schema_key="shared",
        lifecycle_state=LifecycleState.ACTIVE,
        schema_version=3,
    )
    runtime = build_production_tenant_runtime(_dependencies(Counters(), record))

    with pytest.raises(TenantUnavailable) as refused:
        await _resolve(runtime, "clinic-l1-e4d")
    assert refused.value.reason_code == "SCHEMA_KEY_INVALID"


# --- section 7: production and test composition stay separate -------------


def test_l1_g4_generic_test_composition_does_not_touch_the_declaration() -> None:
    from haloflow import composition
    from haloflow.m01.runtime import compose_tenant_runtime

    wider = compose_tenant_runtime(
        _dependencies(Counters()),
        registry=composition.build_production_tenant_migrations(),
        catalog=composition.build_production_catalog(),
        supported_schema_versions=frozenset({1, 2, 3}),
    )
    production = composition.build_production_tenant_runtime(_dependencies(Counters()))

    assert wider.supported_schema_versions == frozenset({1, 2, 3})
    assert production.supported_schema_versions == frozenset({3})
    declared = composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert declared == frozenset({3})
