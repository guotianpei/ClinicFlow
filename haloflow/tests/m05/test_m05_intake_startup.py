"""UC1 part B, tranche B2 — MC-08: a production generation refuses test intake rows.

Design delta v1 Δ4, ALIGNed by Codex 20261031T233740Z: SQL has no trusted
deployment flag, so the production-generation refusal lives at the D3 startup
validator (`haloflow.m02.registry_validation`). The validator gains intake rows and
one rule: in a PRODUCTION deployment, any intake row with `active_in_production =
false` refuses with `TEST_INTAKE_PRESENT`.

These rows are the PURE validator half and say nothing about loading. The loading
half (a real generation loaded from a PG17 tenant reaching the same refusal) waits
on Codex's ruling on relay 20261031T233833Z: no generation loader exists in src
today (pre-existing part A gap, measured at 3e24500).

Interface pinned here (new): `IntakeRow(source_event_type, source_contract_version,
active_in_production)`; `LoadedGeneration.intake_rows` defaulting to `()` so every
existing part A constructor keeps its meaning (existing validator contracts
preserved; tests/m02/test_m02_part_a_registry_units.py is not edited).
"""

from __future__ import annotations

from typing import Any

import pytest


def _v() -> Any:
    from haloflow.m02 import registry_validation

    return registry_validation


def _generation(v: Any, *intake: Any) -> Any:
    return v.LoadedGeneration(
        contracts=(), resolvers=(), test_targets_present=False, intake_rows=tuple(intake)
    )


def test_mc08_production_with_test_intake_row_is_refused() -> None:
    v = _v()
    row = v.IntakeRow("m02_test_observation_received", 1, False)
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(_generation(v, row), deployment="production")
    assert caught.value.reason_code == "TEST_INTAKE_PRESENT"


def test_mc08_non_production_with_test_intake_row_is_accepted() -> None:
    v = _v()
    row = v.IntakeRow("m02_test_observation_received", 1, False)
    assert v.validate_loaded_generation(_generation(v, row), deployment="non_production") is None


def test_mc08_production_with_empty_or_production_intake_is_accepted() -> None:
    v = _v()
    assert v.validate_loaded_generation(_generation(v), deployment="production") is None
    live = v.IntakeRow("m07_reply_received", 1, True)
    assert v.validate_loaded_generation(_generation(v, live), deployment="production") is None


def test_mc08_one_test_row_among_production_rows_still_refuses() -> None:
    v = _v()
    rows = (
        v.IntakeRow("m07_reply_received", 1, True),
        v.IntakeRow("m02_test_observation_received", 1, False),
    )
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(_generation(v, *rows), deployment="production")
    assert caught.value.reason_code == "TEST_INTAKE_PRESENT"


def test_mc08_existing_constructor_keeps_its_meaning() -> None:
    """The three-argument part A constructor still works and means 'no intake'."""

    v = _v()
    generation = v.LoadedGeneration(contracts=(), resolvers=(), test_targets_present=False)
    assert generation.intake_rows == ()
    assert v.validate_loaded_generation(generation, deployment="production") is None


# ---------------------------------------------------------------------------
# Loader rows (PG17). Codex ALIGN 20261031T234337Z, codex_b2-loader-scope-
# disposition. These prove LOADER + VALIDATOR on real rows; they are NOT startup
# enforcement. The trusted startup caller and refusal-before-traffic proof are a
# mandatory B5 obligation (D3 end-to-end remains open until then).
#
# Interface pinned (new): `async load_generation(connection, schema_key) ->
# LoadedGeneration` in haloflow.m02.registry_validation.
#   - The caller supplies an existing connection (here: the disposable-test admin;
#     no credentials, SET ROLE or deployment inference inside the loader).
#   - Read-only, one consistent snapshot for every registry read.
#   - Contract activity = event_types.is_active.
#   - Refusals (RegistryRefused): SCHEMA_KEY_INVALID for a key outside the tenant
#     schema grammar; GENERATION_UNREADABLE for a missing required M02 relation, a
#     permission failure, a nonexistent schema, or M05 objects present without a
#     readable intake table. Never an empty accepted generation.
#   - Intake absent counts as empty ONLY for a recognised pre-M05 generation: no
#     m05_* relation at all.
# Fixtures isolate one refusal each, on an otherwise production-valid registry
# (production-seeded M05 test install: test families inactive, synthetic resolvers
# inactive, no test targets, intake empty). Mutations are admin-applied to
# disposable test tenants and reverted.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402

import m02_support as m02  # noqa: E402
from m05_harness import M05Install  # noqa: E402
from psycopg import AsyncConnection, sql  # noqa: E402

postgres = pytest.mark.postgres


def _load(install: M05Install | m02.Identities, schema: str) -> Any:
    ids = install.ids if isinstance(install, M05Install) else install

    async def run() -> Any:
        async with await AsyncConnection.connect(ids.admin, autocommit=True) as conn:
            return await _v().load_generation(conn, schema)

    return asyncio.run(run())


def _admin_exec(ids: m02.Identities, statement: Any, params: Any = None) -> None:
    with m02.connect_admin(ids) as conn:
        conn.execute(statement, params)


def _tenant_fixture(label: str) -> Callable[..., Iterator[M05Install]]:
    @pytest.fixture(scope="module")
    def fixture(
        request: pytest.FixtureRequest, m02_ids: m02.Identities, m05_reset: Any
    ) -> Iterator[M05Install]:
        tenant = m02.tenant_for(f"{request.module.__name__}:m05:{label}")
        m05_reset(tenant)
        try:
            yield M05Install(m02_ids, tenant)
        finally:
            m05_reset(tenant)

    return fixture


loader_base = _tenant_fixture("loader-base")
loader_drop_intake = _tenant_fixture("loader-drop-intake")
loader_drop_required = _tenant_fixture("loader-drop-required")


def _validate(generation: Any, deployment: str) -> str | None:
    v = _v()
    try:
        v.validate_loaded_generation(generation, deployment=deployment)
    except v.RegistryRefused as refused:
        return str(refused.reason_code)
    return None


def _mutated(install: M05Install, apply: str, revert: str) -> Any:
    """Apply an admin mutation, load, revert (always); return the loaded generation."""

    q = sql.Identifier(install.schema)
    _admin_exec(install.ids, sql.SQL(apply).format(s=q))
    try:
        return _load(install, install.schema)
    finally:
        _admin_exec(install.ids, sql.SQL(revert).format(s=q))


_TEST_INTAKE_ROW = (
    "INSERT INTO {s}.m05_intake_map VALUES ('m02_test_observation_received', 1,"
    " 'label_code', 'reviewed', 'reschedule_request',"
    " '00000000-0000-4000-8000-0000000b2001', false)"
)
_TEST_INTAKE_REVERT = "DELETE FROM {s}.m05_intake_map"


@postgres
def test_mc08_loader_production_valid_registry_is_accepted(loader_base: M05Install) -> None:
    loader_base.require()
    generation = _load(loader_base, loader_base.schema)
    assert generation.intake_rows == ()
    assert generation.test_targets_present is False
    assert {(c.event_type, c.version, c.is_active) for c in generation.contracts} == {
        ("m02_test_observation_received", 1, False),
        ("m05_followup_resolution_asserted", 1, False),
    }
    assert _validate(generation, "production") is None
    assert _validate(generation, "non_production") is None


@postgres
def test_mc08_loader_real_test_intake_row_reaches_test_intake_present(
    loader_base: M05Install,
) -> None:
    loader_base.require()
    generation = _mutated(loader_base, _TEST_INTAKE_ROW, _TEST_INTAKE_REVERT)
    v = _v()
    assert generation.intake_rows == (
        v.IntakeRow("m02_test_observation_received", 1, False),
    )
    assert _validate(generation, "production") == "TEST_INTAKE_PRESENT"
    assert _validate(generation, "non_production") is None
    # Reverted: the same tenant loads as production-valid again.
    assert _validate(_load(loader_base, loader_base.schema), "production") is None


@postgres
@pytest.mark.parametrize(
    ("case", "apply", "revert", "production", "non_production"),
    [
        (
            "active-test-family",
            "UPDATE {s}.event_types SET is_active = true"
            " WHERE event_type = 'm02_test_observation_received'",
            "UPDATE {s}.event_types SET is_active = false"
            " WHERE event_type = 'm02_test_observation_received'",
            "TEST_FAMILY_ACTIVE_IN_PRODUCTION",
            None,
        ),
        (
            "active-synthetic-resolver",
            "UPDATE {s}.m02_link_resolvers SET is_active = true"
            " WHERE resolver_id = 'm02_test_subject_v1'",
            "UPDATE {s}.m02_link_resolvers SET is_active = false"
            " WHERE resolver_id = 'm02_test_subject_v1'",
            "SYNTHETIC_DISPATCH_IN_PRODUCTION",
            None,
        ),
        (
            "test-targets-present",
            "CREATE TABLE {s}.m02_test_link_targets (x integer)",
            "DROP TABLE {s}.m02_test_link_targets",
            "SYNTHETIC_DISPATCH_IN_PRODUCTION",
            None,
        ),
        (
            "active-route-not-installed",
            "UPDATE {s}.event_contracts SET projection_rule_id = 'mb2_probe'"
            " WHERE event_type = 'm05_followup_resolution_asserted';"
            " UPDATE {s}.event_types SET is_active = true"
            " WHERE event_type = 'm05_followup_resolution_asserted'",
            "UPDATE {s}.event_types SET is_active = false"
            " WHERE event_type = 'm05_followup_resolution_asserted';"
            " UPDATE {s}.event_contracts SET projection_rule_id = NULL"
            " WHERE event_type = 'm05_followup_resolution_asserted'",
            "ROUTE_NOT_INSTALLED",
            "ROUTE_NOT_INSTALLED",
        ),
    ],
)
def test_mc08_loader_existing_part_a_refusals_on_real_rows(
    loader_base: M05Install,
    case: str,
    apply: str,
    revert: str,
    production: str | None,
    non_production: str | None,
) -> None:
    loader_base.require()
    generation = _mutated(loader_base, apply, revert)
    assert (_validate(generation, "production"), _validate(generation, "non_production")) == (
        production,
        non_production,
    ), case


@postgres
def test_mc08_loader_pre_m05_generation_loads_empty_intake(
    m02_ids: m02.Identities, m05_reset: Any
) -> None:
    """A part A production tenant (no m05_* relation): intake absent means empty."""

    tenant = m02.tenant_for("test_m05_intake_startup:pre-m05")
    m05_reset(tenant)
    try:
        m02.provision_sync(
            m02_ids, m02.production_registry(), tenant, supported=m02.PRODUCTION_SUPPORTED
        )
        generation = _load(m02_ids, tenant[1])
        assert generation.intake_rows == ()
        assert _validate(generation, "production") is None
    finally:
        m05_reset(tenant)


@postgres
def test_mc08_loader_m05_present_without_intake_is_unreadable(
    loader_drop_intake: M05Install,
) -> None:
    loader_drop_intake.require()
    _admin_exec(
        loader_drop_intake.ids,
        sql.SQL("DROP TABLE {}.m05_intake_map").format(sql.Identifier(loader_drop_intake.schema)),
    )
    v = _v()
    with pytest.raises(v.RegistryRefused) as caught:
        _load(loader_drop_intake, loader_drop_intake.schema)
    assert caught.value.reason_code == "GENERATION_UNREADABLE"


@postgres
def test_mc08_loader_missing_required_relation_is_unreadable(
    loader_drop_required: M05Install,
) -> None:
    loader_drop_required.require()
    _admin_exec(
        loader_drop_required.ids,
        sql.SQL("DROP TABLE {}.m02_link_resolvers CASCADE").format(
            sql.Identifier(loader_drop_required.schema)
        ),
    )
    v = _v()
    with pytest.raises(v.RegistryRefused) as caught:
        _load(loader_drop_required, loader_drop_required.schema)
    assert caught.value.reason_code == "GENERATION_UNREADABLE"


@postgres
def test_mc08_loader_schema_identity_and_permission_fail_closed(
    loader_base: M05Install,
) -> None:
    loader_base.require()
    v = _v()
    with pytest.raises(v.RegistryRefused) as caught:
        _load(loader_base, "Bad;Schema")
    assert caught.value.reason_code == "SCHEMA_KEY_INVALID"
    with pytest.raises(v.RegistryRefused) as caught:
        _load(loader_base, "tenant_mzzzzzzzz")  # valid grammar, no such schema
    assert caught.value.reason_code == "GENERATION_UNREADABLE"

    # Permission failure: a connection whose role reads nothing in the tenant.
    async def as_runtime() -> Any:
        async with await AsyncConnection.connect(
            loader_base.ids.logins["haloflow_runtime"], autocommit=True
        ) as conn:
            await conn.execute("SET ROLE haloflow_runtime")
            return await v.load_generation(conn, loader_base.schema)

    with pytest.raises(v.RegistryRefused) as caught:
        asyncio.run(as_runtime())
    assert caught.value.reason_code == "GENERATION_UNREADABLE"


@postgres
def test_mc08_loader_is_read_only_and_leaves_no_transaction(loader_base: M05Install) -> None:
    """On a session forced read-only (every transaction READ ONLY), the loader still
    succeeds, so it issues no write; afterwards the connection is idle."""

    loader_base.require()

    async def run() -> Any:
        async with await AsyncConnection.connect(loader_base.ids.admin, autocommit=True) as conn:
            await conn.execute("SET default_transaction_read_only = on")
            generation = await _v().load_generation(conn, loader_base.schema)
            return generation, conn.info.transaction_status

    generation, status = asyncio.run(run())
    from psycopg.pq import TransactionStatus

    assert generation.intake_rows == ()
    assert status == TransactionStatus.IDLE
