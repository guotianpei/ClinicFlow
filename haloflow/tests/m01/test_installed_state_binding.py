"""CP2-2b 2B-U10 to U28 (R-B9.4): the required-profile binding (architecture v3 section 5.1).

Test cases v3 section 3.2. `consume_installed_state(plan, store, *, registry)` is
pure and synchronous; U10 to U26 call it directly. U27 and U28 drive the REAL
runner over the recording harness (`typed_driver`) to show that a refusal happens
before `running` is written and before any DDL.

Interface bound (packet README, I-B3 to I-B5): `InstalledStateProfile`,
`profile_digest`, `VerifiedInstalledState` in `installed_state`;
`TenantMigrationRegistry.installed_state_requirement(migration_id)`;
`AuthorizedPlan` / `_Expectation` fields `profile_required`, `profile`,
`profile_digest`; `typed_plan.consume_installed_state`.

Status before implementation: DB. After: pass. U27 is ME (mutant evidence).
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest

SCHEMA = "tenant_aaaaaaaa"
TENANT = "clinic-a"
LOCK_OWNER = "haloflow_m02_lock_owner"
FIXTURE_ROLE = "haloflow_m02_owner"  # the constructed CP2-1 payloads' role
PLAN_INVALID = "INSTALL_PLAN_INVALID"
T003 = "t003_m02_lock_operation"


class DependencyAbsent(Exception):
    """A 2b unit or name the row needs is not present yet: status DB, never MB."""


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def production_registry() -> Any:
    from haloflow.composition import build_production_tenant_migrations

    return build_production_tenant_migrations()


def unprofiled_registry(seam: Any, payload: dict[str, Any]) -> Any:
    """A constructed typed unit with NO installed-state profile (the not-required path)."""

    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        {payload["migration_id"]: seam.typed_definition(payload)},
        approved_execution_roles=frozenset({FIXTURE_ROLE}),
        allow_test_units=True,
    )


def issue(registry: Any, migration_id: str) -> tuple[Any, Any]:
    """Validate and issue one plan through the production functions. Returns (store, plan)."""

    from haloflow.m01.provisioning import typed_plan

    units = {unit.migration_id: unit for unit in registry}
    if migration_id not in units:
        raise DependencyAbsent(migration_id)
    unit = units[migration_id]
    result = typed_plan.validate_declaration(unit=unit, schema_key=SCHEMA)
    store = typed_plan.OperationExpectations()
    plan = typed_plan.issue_plan(
        unit=unit, schema_key=SCHEMA, registry=registry, store=store, result=result
    )
    return store, plan


def consume(plan: Any, store: Any, registry: Any) -> Any:
    from haloflow.m01.provisioning import typed_plan

    return typed_plan.consume_installed_state(plan, store, registry=registry)


def refused(plan: Any, store: Any, registry: Any) -> None:
    from haloflow.m01.errors import MigrationUnitRejected

    with pytest.raises(MigrationUnitRejected) as caught:
        consume(plan, store, registry)
    assert caught.value.reason_code == PLAN_INVALID


def gateway_profile() -> Any:
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE

    return LOCK_OPERATION_PROFILE


def digest_of(profile: Any) -> str:
    from haloflow.m01.provisioning.installed_state import profile_digest

    return str(profile_digest(profile))


def set_store(store: Any, plan: Any, **fields: Any) -> None:
    expectation = store.issued(plan.token)
    assert expectation is not None
    for name, value in fields.items():
        setattr(expectation, name, value)


def set_envelope(plan: Any, **fields: Any) -> Any:
    return dataclasses.replace(plan, **fields)


def apply(where: str, store: Any, plan: Any, **fields: Any) -> Any:
    """Apply `fields` to the store, the envelope, or both. Returns the envelope to consume."""

    if where in ("store", "both"):
        set_store(store, plan, **fields)
    if where in ("envelope", "both"):
        plan = set_envelope(plan, **fields)
    return plan


WHERE = ("store", "envelope", "both")
OMITTED = {"profile_required": False, "profile": None, "profile_digest": None}


# ---------------------------------------------------------------------------
# U10 to U21: the flag-flip matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", WHERE, ids=["U10-store", "U11-envelope", "U12-joint"])
def test_2b_u10_to_u12_omission_true_to_false_is_refused(where: str) -> None:
    """T->F on the profiled `t003`. U12 is the JOINT omission (architecture v3 5.1(4) step 5)."""

    registry = production_registry()
    store, plan = issue(registry, T003)
    plan = apply(where, store, plan, **OMITTED)
    refused(plan, store, registry)


@pytest.mark.parametrize("where", WHERE, ids=["U13-store", "U14-envelope", "U15-both"])
def test_2b_u13_to_u15_injection_false_to_true_is_refused(
    where: str, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """F->T on an unprofiled typed unit: a real profile and digest injected."""

    payload = typed_payloads["first"]
    registry = unprofiled_registry(seam, payload)
    store, plan = issue(registry, payload["migration_id"])
    profile = gateway_profile()
    plan = apply(
        where, store, plan,
        profile_required=True, profile=profile, profile_digest=digest_of(profile),
    )
    refused(plan, store, registry)


@pytest.mark.parametrize("where", WHERE, ids=["U16-store", "U17-envelope", "U18-both"])
def test_2b_u16_to_u18_required_true_profile_none_digest_set_is_refused(where: str) -> None:
    registry = production_registry()
    store, plan = issue(registry, T003)
    plan = apply(
        where, store, plan,
        profile_required=True, profile=None, profile_digest=digest_of(gateway_profile()),
    )
    refused(plan, store, registry)


@pytest.mark.parametrize("where", WHERE, ids=["U19-store", "U20-envelope", "U21-both"])
def test_2b_u19_to_u21_required_false_profile_set_digest_none_is_refused(where: str) -> None:
    registry = production_registry()
    store, plan = issue(registry, T003)
    plan = apply(
        where, store, plan, profile_required=False, profile=gateway_profile(), profile_digest=None
    )
    refused(plan, store, registry)


# ---------------------------------------------------------------------------
# U22 to U25: substitution, identity, capture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", WHERE)
def test_2b_u22_digest_substitution_is_refused(where: str) -> None:
    registry = production_registry()
    store, plan = issue(registry, T003)
    plan = apply(where, store, plan, profile_digest="0" * 64)
    refused(plan, store, registry)


def test_2b_u23_an_equal_but_distinct_profile_object_is_refused() -> None:
    registry = production_registry()
    store, plan = issue(registry, T003)
    original = gateway_profile()
    twin = dataclasses.replace(original)
    assert twin == original and twin is not original
    plan = apply("both", store, plan, profile=twin, profile_digest=digest_of(twin))
    refused(plan, store, registry)


def test_2b_u24a_a_profile_from_another_unit_is_refused() -> None:
    registry = production_registry()
    store, plan = issue(registry, T003)
    other = dataclasses.replace(gateway_profile(), function_name="m02_other_function")
    plan = apply("both", store, plan, profile=other, profile_digest=digest_of(other))
    refused(plan, store, registry)


def test_2b_u24b_a_plan_issued_against_another_registry_is_refused() -> None:
    registry = production_registry()
    other_registry = production_registry()
    assert other_registry is not registry
    store, plan = issue(other_registry, T003)
    refused(plan, store, registry)


def test_2b_u24c_the_actual_migration_identity_is_the_stores() -> None:
    """An altered envelope id is refused by the EXISTING binding (consume_plan), and
    consume_installed_state reports the STORE's id, never the envelope's."""

    from haloflow.m01.errors import MigrationUnitRejected
    from haloflow.m01.provisioning import typed_plan

    registry = production_registry()
    store, plan = issue(registry, T003)
    altered = set_envelope(plan, migration_id="t002_m02_operation_registry")
    with pytest.raises(MigrationUnitRejected) as caught:
        typed_plan.consume_plan(altered, store)
    assert caught.value.reason_code == PLAN_INVALID
    assert consume(plan, store, registry).migration_id == T003


def test_2b_u25_capture_and_no_await() -> None:
    from haloflow.m01.provisioning import typed_plan

    assert not inspect.iscoroutinefunction(typed_plan.consume_installed_state)
    registry = production_registry()
    store, plan = issue(registry, T003)
    verified = consume(plan, store, registry)
    requirement = registry.installed_state_requirement(T003)
    # Mutating the envelope AFTER consumption cannot reach what was returned.
    object.__setattr__(plan, "profile", dataclasses.replace(gateway_profile(), strict=True))
    assert verified.profile is requirement.profile
    assert verified.profile.strict is False


def test_2b_u26_coherent_states_pass_control(seam: Any, typed_payloads: dict[str, Any]) -> None:
    """CONTROL. Coherent required and not-required states both pass."""

    registry = production_registry()
    store, plan = issue(registry, T003)
    verified = consume(plan, store, registry)
    assert (verified.migration_id, verified.required) == (T003, True)
    assert verified.profile is registry.installed_state_requirement(T003).profile

    payload = typed_payloads["first"]
    plain = unprofiled_registry(seam, payload)
    plain_store, plain_plan = issue(plain, payload["migration_id"])
    unprofiled = consume(plain_plan, plain_store, plain)
    assert (unprofiled.required, unprofiled.profile) == (False, None)


# ---------------------------------------------------------------------------
# U27, U28: through the REAL runner (recording harness), before `running`
# ---------------------------------------------------------------------------


def gateway_only_registry() -> Any:
    from haloflow.m01.provisioning.units import build_tenant_migration_registry
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
    from haloflow.m02.units import T003_DEFINITION, T003_MIGRATION_ID

    return build_tenant_migration_registry(
        {T003_MIGRATION_ID: T003_DEFINITION},
        approved_execution_roles=frozenset({LOCK_OWNER}),
        installed_state_profiles={T003_MIGRATION_ID: LOCK_OPERATION_PROFILE},
    )


def observation_row() -> tuple[Any, ...]:
    """A CONSTRUCTED installed-function row matching t003's snapshot (control input)."""

    from haloflow.m02.units import T003_DEFINITION

    declared = T003_DEFINITION.policy_verification["functions"][0]
    body = declared["body"].replace("{schema}", SCHEMA)
    config = [entry.replace("{schema}", SCHEMA) for entry in declared["config"]]
    acl = [[16501, LOCK_OWNER, LOCK_OWNER, "EXECUTE", False],
           [16500, "haloflow_runtime", LOCK_OWNER, "EXECUTE", False]]
    return (70000, "f", 1, True, True, True, True, True, False, True, LOCK_OWNER, True,
            "v", "u", False, config, body, False, acl)


def role_answers(harness: Any) -> tuple[Any, ...]:
    return (
        harness.Answer(markers=("select rolcanlogin", "pg_roles"), rows=((False,) * 6,)),
        harness.Answer(markers=("pg_has_role",), rows=((True,),)),
    )


ADAPTER_MARKER = "-- m01 installed function verification"


def drive(typed_driver: Any, harness: Any) -> tuple[Any, Any]:
    runner, (connection,) = typed_driver(
        gateway_only_registry(),
        (
            *role_answers(harness),
            harness.ledger_absent(),
            harness.Answer(markers=(ADAPTER_MARKER,), rows=(observation_row(),)),
        ),
        names=("work",),
    )
    return runner, connection


def ledger_writes(connection: Any) -> list[Any]:
    return [
        entry for entry in connection.statements
        if "shared.schema_migrations" in entry.fingerprint
        and not entry.fingerprint.startswith("select")
    ]


async def test_2b_u27_control_the_unmodified_runner_calls_the_adapter_before_applied(
    typed_driver: Any, harness: Any
) -> None:
    """U27 CONTROL: exactly one adapter query, before the `applied` write."""

    runner, connection = drive(typed_driver, harness)
    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert [outcome.applied for outcome in outcomes] == [True]
    adapter = [e for e in connection.statements if ADAPTER_MARKER in e.fingerprint]
    applied = [e for e in connection.statements
               if e.fingerprint.startswith("update shared.schema_migrations")
               and "set state = 'applied'" in e.fingerprint]
    assert len(adapter) == 1 and len(applied) == 1
    assert adapter[0].seq < applied[0].seq


async def test_2b_u27_mutant_hook_deleted_is_refused_before_running(
    typed_driver: Any, harness: Any, monkeypatch: Any
) -> None:
    """U27 ME: `consume_installed_state` not called (stubbed to 'not required').

    Observable: the runner's defence-in-depth check refuses INSTALL_PLAN_INVALID,
    and no ledger write, role switch or DDL happens. Test evidence, not the design.
    """

    from haloflow.m01.errors import TenantMigrationFailed
    from haloflow.m01.provisioning import typed_plan
    from haloflow.m01.provisioning.installed_state import VerifiedInstalledState

    def stub(plan: Any, store: Any, *, registry: Any) -> Any:
        return VerifiedInstalledState(migration_id=T003, required=False, profile=None)

    monkeypatch.setattr(typed_plan, "consume_installed_state", stub)
    runner, connection = drive(typed_driver, harness)
    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert caught.value.reason_code == PLAN_INVALID
    assert ledger_writes(connection) == []
    assert not any(ADAPTER_MARKER in e.fingerprint for e in connection.statements)
    assert not any(e.fingerprint.startswith("create function") for e in connection.statements)


async def test_2b_u28_defence_in_depth_mismatch_is_refused_before_running(
    typed_driver: Any, harness: Any, monkeypatch: Any
) -> None:
    """U28: a verified state that differs from the registry (distinct equal profile)."""

    from haloflow.m01.errors import TenantMigrationFailed
    from haloflow.m01.provisioning import typed_plan
    from haloflow.m01.provisioning.installed_state import VerifiedInstalledState

    twin = dataclasses.replace(gateway_profile())

    def stub(plan: Any, store: Any, *, registry: Any) -> Any:
        return VerifiedInstalledState(migration_id=T003, required=True, profile=twin)

    monkeypatch.setattr(typed_plan, "consume_installed_state", stub)
    runner, connection = drive(typed_driver, harness)
    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert caught.value.reason_code == PLAN_INVALID
    assert ledger_writes(connection) == []


# ---------------------------------------------------------------------------
# U10 to U24 THROUGH THE REAL RUNNER (Codex packet-v1 review, P1)
#
# The same altered states, produced by wrapping the runner's own `issue_plan`
# seam (module attribute, as the runner calls it: runner.py `typed_plan.issue_plan`).
# Each case must be refused INSTALL_PLAN_INVALID with NO ledger write and NO DDL
# or role switch. The successful controls prove the harness itself installs.
# ---------------------------------------------------------------------------


def manifest_declaring(*roles: str) -> Any:
    """The LOADED manifest with the given execution-role profiles (v11's seam)."""

    from haloflow.m01.provisioning.manifest import (
        ExecutionRoleProfile,
        load_provisioning_manifest,
    )

    profile = ExecutionRoleProfile(
        login=False, superuser=False, createdb=False, createrole=False,
        replication=False, bypassrls=False, tenant_schema_privileges=("USAGE",),
    )
    return dataclasses.replace(
        load_provisioning_manifest(), execution_role_profiles={role: profile for role in roles}
    )


def mutate_after_issue(monkeypatch: Any, mutate: Any) -> None:
    from haloflow.m01.provisioning import typed_plan

    original = typed_plan.issue_plan

    def wrapper(*, unit: Any, schema_key: str, registry: Any, store: Any, result: Any) -> Any:
        plan = original(
            unit=unit, schema_key=schema_key, registry=registry, store=store, result=result
        )
        return mutate(store, plan)

    monkeypatch.setattr(typed_plan, "issue_plan", wrapper)


def assert_refused_before_running(connection: Any, reason_code: str) -> None:
    assert reason_code == PLAN_INVALID
    assert ledger_writes(connection) == []
    fingerprints = [entry.fingerprint for entry in connection.statements]
    assert not any(f.startswith("create function") for f in fingerprints), fingerprints
    assert not any(f.startswith("set local role") for f in fingerprints), fingerprints
    assert not any(ADAPTER_MARKER in f for f in fingerprints), fingerprints


def drive_unprofiled(
    typed_driver: Any, harness: Any, seam: Any, payload: dict[str, Any]
) -> tuple[Any, Any]:
    runner, (connection,) = typed_driver(
        unprofiled_registry(seam, payload),
        (*role_answers(harness), harness.ledger_absent()),
        manifest=manifest_declaring(FIXTURE_ROLE),
        names=("work",),
    )
    return runner, connection


def _profiled_cases() -> list[tuple[str, Any]]:
    """(id, mutate(store, plan) -> plan) for the profiled `t003`."""

    def fields(where: str, **values: Any) -> Any:
        return lambda store, plan: apply(where, store, plan, **values)

    cases: list[tuple[str, Any]] = []
    for where in WHERE:
        cases.append((f"U10-12-omission-{where}", fields(where, **OMITTED)))
        cases.append((f"U16-18-required-no-profile-{where}", lambda s, p, w=where: apply(
            w, s, p, profile_required=True, profile=None,
            profile_digest=digest_of(gateway_profile()))))
        cases.append((f"U19-21-unrequired-with-profile-{where}", lambda s, p, w=where: apply(
            w, s, p, profile_required=False, profile=gateway_profile(), profile_digest=None)))
        cases.append((f"U22-digest-{where}", fields(where, profile_digest="0" * 64)))

    def twin(store: Any, plan: Any) -> Any:
        copy = dataclasses.replace(gateway_profile())
        return apply("both", store, plan, profile=copy, profile_digest=digest_of(copy))

    def other_unit(store: Any, plan: Any) -> Any:
        other = dataclasses.replace(gateway_profile(), function_name="m02_other_function")
        return apply("both", store, plan, profile=other, profile_digest=digest_of(other))

    cases += [("U23-equal-distinct", twin), ("U24a-other-unit", other_unit)]
    return cases


PROFILED_CASES = _profiled_cases()


@pytest.mark.parametrize(("case", "mutate"), PROFILED_CASES, ids=[c[0] for c in PROFILED_CASES])
async def test_2b_u10_to_u24_through_the_runner_profiled_unit(
    typed_driver: Any, harness: Any, monkeypatch: Any, case: str, mutate: Any
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed

    mutate_after_issue(monkeypatch, mutate)
    runner, connection = drive(typed_driver, harness)
    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert_refused_before_running(connection, caught.value.reason_code)


async def test_2b_u24b_through_the_runner_a_plan_issued_against_another_registry(
    typed_driver: Any, harness: Any, monkeypatch: Any
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed
    from haloflow.m01.provisioning import typed_plan

    original = typed_plan.issue_plan
    other_registry = gateway_only_registry()

    def wrapper(*, unit: Any, schema_key: str, registry: Any, store: Any, result: Any) -> Any:
        assert other_registry is not registry
        return original(
            unit=unit, schema_key=schema_key, registry=other_registry, store=store, result=result
        )

    monkeypatch.setattr(typed_plan, "issue_plan", wrapper)
    runner, connection = drive(typed_driver, harness)
    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert_refused_before_running(connection, caught.value.reason_code)


async def test_2b_u24c_through_the_runner_an_altered_envelope_migration_id_has_no_effect(
    typed_driver: Any, harness: Any, monkeypatch: Any
) -> None:
    """U24c at the runner boundary: the envelope names another unit. The EXISTING binding
    refuses it before `running`; no ledger write, DDL, role switch or adapter query."""

    from haloflow.m01.errors import TenantMigrationFailed

    mutate_after_issue(
        monkeypatch,
        lambda store, plan: set_envelope(plan, migration_id="t002_m02_operation_registry"),
    )
    runner, connection = drive(typed_driver, harness)
    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert_refused_before_running(connection, caught.value.reason_code)


@pytest.mark.parametrize("where", WHERE, ids=["U13-store", "U14-envelope", "U15-both"])
async def test_2b_u13_to_u15_through_the_runner_unprofiled_unit(
    typed_driver: Any, harness: Any, monkeypatch: Any, seam: Any,
    typed_payloads: dict[str, Any], where: str,
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed

    profile = gateway_profile()
    mutate_after_issue(monkeypatch, lambda store, plan: apply(
        where, store, plan,
        profile_required=True, profile=profile, profile_digest=digest_of(profile),
    ))
    runner, connection = drive_unprofiled(typed_driver, harness, seam, typed_payloads["first"])
    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert_refused_before_running(connection, caught.value.reason_code)


async def test_2b_u13_control_the_unprofiled_unit_installs_through_the_runner(
    typed_driver: Any, harness: Any, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """CONTROL for the runner matrix: the unaltered unprofiled path installs."""

    runner, connection = drive_unprofiled(typed_driver, harness, seam, typed_payloads["first"])
    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert [outcome.applied for outcome in outcomes] == [True]
    assert not any(ADAPTER_MARKER in e.fingerprint for e in connection.statements)


async def test_2b_u25_through_the_runner_a_post_consumption_mutation_does_not_reach_the_adapter(
    typed_driver: Any, harness: Any, monkeypatch: Any
) -> None:
    """U25 at the runner boundary: after `consume_installed_state` returns, the envelope's
    profile is replaced. The adapter must receive the REGISTRY's profile."""

    from haloflow.m01.provisioning import installed_state, typed_plan

    registry_profile = gateway_profile()
    replacement = dataclasses.replace(registry_profile, strict=True)
    real_consume = typed_plan.consume_installed_state

    def consume_then_mutate(plan: Any, store: Any, *, registry: Any) -> Any:
        verified = real_consume(plan, store, registry=registry)
        object.__setattr__(plan, "profile", replacement)
        return verified

    seen: list[Any] = []
    real_compare = installed_state.compare_installed_function

    def spy(profile: Any, **kwargs: Any) -> None:
        seen.append(profile)
        real_compare(profile, **kwargs)

    monkeypatch.setattr(typed_plan, "consume_installed_state", consume_then_mutate)
    monkeypatch.setattr(installed_state, "compare_installed_function", spy)
    runner, _ = drive(typed_driver, harness)
    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert [outcome.applied for outcome in outcomes] == [True]
    assert len(seen) == 1 and seen[0] is registry_profile and seen[0].strict is False
