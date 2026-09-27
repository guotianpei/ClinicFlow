"""Shared M01 test fixtures.

Helpers are exposed as fixtures rather than imported across test modules. The
earlier `from conftest import ...` worked only because pytest's prepend import
mode puts this directory on sys.path, which is an avoidable dependency on
collection mechanics.
"""

import copy
import importlib.util
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
import recording
import typed_recording

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.provisioning.runner import TenantMigrationRunner
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


# ---- conftest-addition.py ----

# Stage 1 (`assert_execution_roles_safe`) runs on every runner entry and makes
# two reads that no test is about: the controlled membership graph (answered with
# the shipped manifest's one declared edge, E-10a), and the migrator's
# `rolcreaterole`. They are answered here so a test declares only the answers it
# is actually reasoning about. A test that wants stage 1 to REFUSE
# supplies a conflicting answer explicitly rather than relying on omission.
STAGE_ONE_ANSWERS = (recording.SHIPPED_CONTROLLED_EDGES, recording.MIGRATOR_SAFE)


@pytest.fixture
def harness():
    """The recording harness module, so test modules import nothing across modules.

    `conftest.py` is the one place that imports `recording`, which is where
    pytest's prepend-import mechanic is meant to be used. Tests reach the
    declarative pieces -- `harness.Answer`, `harness.ledger_absent()`,
    `harness.UnscriptedQuery` -- through this fixture.
    """

    return recording


@pytest.fixture
def shared_clock():
    """One monotonic counter per test, shared by every connection it builds.

    `apply` drives two connections. Without a shared clock their traces cannot
    be ordered against each other, and an assertion built from two independent
    traces would pass a runner that released the lock before doing any work.
    """

    return recording.SharedClock()


@pytest.fixture
def recording_connection(shared_clock):
    """Build a `RecordingConnection` with stage 1's answers plus the test's.

    Returns the factory, not a connection: a test driving `apply` needs two
    connections and must be able to ask for them separately. Every connection
    it builds shares the test's clock.
    """

    def build(*answers, name: str = "connection") -> recording.RecordingConnection:
        return recording.RecordingConnection(
            answers=[*STAGE_ONE_ANSWERS, *answers], name=name, clock=shared_clock
        )

    return build


@pytest.fixture
def migration_driver(recording_connection):
    """Build the real `TenantMigrationRunner` over recording connections.

    `connect` is a production constructor parameter, so this wires a test
    connection into the shipping runner without patching, wrapping or
    monkeypatching anything. `manifest` is likewise a production parameter.

    Returns `(runner, connections)` where `connections` is the tuple handed to
    the factory in order -- for `apply_within_lock` that is one connection, for
    `apply` it is the lock connection then the work connection.
    """

    def build(registry, *answer_sets, manifest=None, names=()):
        labels = tuple(names) or tuple(f"c{index}" for index in range(len(answer_sets)))
        connections = tuple(
            recording_connection(*answers, name=label)
            for answers, label in zip(answer_sets, labels, strict=True)
        )
        runner = TenantMigrationRunner(
            recording.connection_factory(*connections),
            registry,
            **({"manifest": manifest} if manifest is not None else {}),
        )
        return runner, connections

    return build


# ---- conftest-addition-v12.py ----

_SEAM_SPEC = importlib.util.spec_from_file_location(
    "cp2_typed_plan_seam", Path(__file__).parent / "support" / "typed_plan_seam.py"
)
assert _SEAM_SPEC is not None and _SEAM_SPEC.loader is not None
_SEAM = importlib.util.module_from_spec(_SEAM_SPEC)
_SEAM_SPEC.loader.exec_module(_SEAM)


_POLICY_FIXTURES = Path(__file__).parent / "fixtures" / "function_policy"


def _derive_typed_payloads() -> dict:
    """Every payload the typed cases use, derived from the FROZEN CP1 fixtures.

    Nothing here is authored from scratch. Each entry is a CP1 variant, or a CP1
    variant with the named single edit. Whether the frozen checker admits or
    refuses each one is asserted by `test_typed_plan_checksum.py`'s fixture
    controls, which run today -- so a typed case that later fails cannot be
    blamed on a fixture nobody checked.
    """

    variants = {
        variant["case_id"]: variant["payload"]
        for variant in json.loads((_POLICY_FIXTURES / "sql-fixtures.json").read_text())[
            "variants"
        ]
    }

    def renamed(payload: dict, migration_id: str, function_name: str) -> dict:
        # A second, distinct typed unit: new migration id AND new function name,
        # edited in the three places the name occurs. The body is untouched, so
        # `body_sha256` stays valid.
        result = copy.deepcopy(payload)
        result["migration_id"] = migration_id
        result["template"] = result["template"].replace("m02_annex_probe", function_name)
        result["policy"]["functions"][0]["name"] = function_name
        result["verification"]["functions"][0]["name"] = function_name
        return result

    first = copy.deepcopy(variants["POS-quoted-words"])
    annex = copy.deepcopy(first)
    # TP-24a / B-role alternate. The declared owner IS the execution role, so both
    # move together; an annex role with an m02_owner owner would describe a
    # function the D-layer verifier must then refuse.
    annex["execution_role"] = "haloflow_m02_annex"
    annex["verification"]["functions"][0]["owner"] = "haloflow_m02_annex"
    payload_nul = copy.deepcopy(first)
    # CP1 `test_nul_intake[template]`'s exact edit.
    payload_nul["template"] = payload_nul["template"].replace(
        "CREATE FUNCTION", "CREATE\0 FUNCTION", 1
    )
    return {
        "first": first,
        "second": renamed(first, "t003_annex_probe", "m02_annex_second"),
        "second_body_drift": renamed(
            variants["A-body-drift"], "t003_annex_probe", "m02_annex_second"
        ),
        "annex": annex,
        "body_drift": copy.deepcopy(variants["A-body-drift"]),
        "table": copy.deepcopy(variants["A-table"]),
        "unknown_select": copy.deepcopy(variants["A-unknown-select"]),
        "payload_nul": payload_nul,
    }


@pytest.fixture
def typed_payloads():
    """A fresh deep copy of every derived payload, so no test mutates another's."""

    return _derive_typed_payloads()


@pytest.fixture
def seam():
    """The typed-plan vocabulary. See `support/typed_plan_seam.py`."""

    return _SEAM


@pytest.fixture
def typed_harness():
    """The typed harness extension module (`TypedConnection`, `Fault`, `Hook`, ...)."""

    return typed_recording


@pytest.fixture
def call_spy(shared_clock):
    """A `CallSpy` on the test's shared clock, so its calls order against the trace."""

    def build() -> typed_recording.CallSpy:
        return typed_recording.CallSpy(clock=shared_clock)

    return build


@pytest.fixture
def typed_driver(shared_clock):
    """The real `TenantMigrationRunner` over `TypedConnection`s.

    Same shape as v11's `migration_driver`: `connect` and `manifest` are
    production constructor parameters, nothing is patched. Stage 1's two reads are
    answered first, exactly as there, so a test declares only the answers it is
    reasoning about.

    Returns `(runner, connections)`. Each answer set is a tuple; a connection that
    needs faults or hooks gets them by mutating `connection.faults` /
    `connection.hooks` before the runner is driven.
    """

    def build(registry, *answer_sets, manifest=None, names=()):
        labels = tuple(names) or tuple(f"c{index}" for index in range(len(answer_sets)))
        connections = tuple(
            typed_recording.TypedConnection(
                answers=[*STAGE_ONE_ANSWERS, *answers],
                name=label,
                clock=shared_clock,
            )
            for answers, label in zip(answer_sets, labels, strict=True)
        )
        runner = TenantMigrationRunner(
            recording.connection_factory(*connections),
            registry,
            **({"manifest": manifest} if manifest is not None else {}),
        )
        return runner, connections

    return build
