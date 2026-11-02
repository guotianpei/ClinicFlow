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


# ---------------------------------------------------------------------------
# Round-1 corrections (Codex REVISE 20261031T235804Z,
# codex_b2-tests-first-review-7838244).
# ---------------------------------------------------------------------------

import queue  # noqa: E402
import threading  # noqa: E402

import psycopg  # noqa: E402

loader_probe = _tenant_fixture("loader-probe")

# A coordinated change across three registry relations, committed in ONE writer
# transaction. A coherent generation shows all of it or none of it.
_COORDINATED_APPLY = (
    "UPDATE {s}.event_types SET is_active = true"
    " WHERE event_type = 'm02_test_observation_received';"
    " UPDATE {s}.m02_link_resolvers SET is_active = true"
    " WHERE resolver_id = 'm02_test_subject_v1';"
    " INSERT INTO {s}.m05_intake_map VALUES ('m02_test_observation_received', 1,"
    " 'label_code', 'reviewed', 'reschedule_request',"
    " '00000000-0000-4000-8000-0000000b2001', false)"
)
_COORDINATED_REVERT = (
    "UPDATE {s}.event_types SET is_active = false"
    " WHERE event_type = 'm02_test_observation_received';"
    " UPDATE {s}.m02_link_resolvers SET is_active = false"
    " WHERE resolver_id = 'm02_test_subject_v1';"
    " DELETE FROM {s}.m05_intake_map"
)


def _fingerprint(generation: Any) -> tuple[bool, bool, int]:
    family = any(
        c.event_type == "m02_test_observation_received" and c.is_active
        for c in generation.contracts
    )
    resolver = any(
        r.resolver_id == "m02_test_subject_v1" and r.is_active for r in generation.resolvers
    )
    return family, resolver, len(generation.intake_rows)


def _interleave(install: M05Install, locked: str, loader: Any) -> Any:
    """Run `loader(conn, schema)` while a writer holds ACCESS EXCLUSIVE on `locked`
    and has made the coordinated change; commit only once the loader is OBSERVED
    waiting on the lock. Returns the loader's result (or raises its error)."""

    schema = sql.Identifier(install.schema)
    pids: queue.Queue[int] = queue.Queue()
    results: list[Any] = []

    def run_loader() -> None:
        async def go() -> Any:
            async with await AsyncConnection.connect(install.ids.admin, autocommit=True) as conn:
                await conn.execute("SET lock_timeout = '20s'")
                row = await (await conn.execute("SELECT pg_backend_pid()")).fetchone()
                assert row is not None
                pids.put(int(row[0]))
                return await loader(conn, install.schema)

        try:
            results.append(asyncio.run(go()))
        except Exception as error:  # noqa: BLE001 - re-raised below
            results.append(error)

    writer = psycopg.connect(install.ids.admin)
    thread: threading.Thread | None = None
    try:
        writer.execute("SET lock_timeout = '20s'")
        writer.execute(
            sql.SQL("LOCK TABLE {}.{} IN ACCESS EXCLUSIVE MODE").format(
                schema, sql.Identifier(locked)
            )
        )
        writer.execute(sql.SQL(_COORDINATED_APPLY).format(s=schema))
        thread = threading.Thread(target=run_loader, daemon=True)
        thread.start()
        pid = pids.get(timeout=20)
        waiting = False
        for _ in range(400):
            rows = _admin_rows(
                install, "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            )
            if rows == [("Lock",)]:
                waiting = True
                break
            if results:
                break
            threading.Event().wait(0.05)
        assert waiting, f"loader was not observed waiting on {locked}: {results}"
        writer.commit()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert len(results) == 1, results
        if isinstance(results[0], Exception):
            raise results[0]
        return results[0]
    finally:
        try:
            writer.rollback()
        finally:
            writer.close()
        if thread is not None:
            thread.join(timeout=5)
        _admin_exec(install.ids, sql.SQL(_COORDINATED_REVERT).format(s=schema))


_LOCK_TARGETS = ("event_types", "event_contracts", "m02_link_resolvers", "m05_intake_map")


@postgres
@pytest.mark.parametrize("locked", _LOCK_TARGETS)
def test_mc08_loader_reads_one_coherent_snapshot(loader_base: M05Install, locked: str) -> None:
    """F-B2-SNAPSHOT01. See `_interleave`. Repeated with the lock on each relation
    the loader reads: a loader reading relation by relation in separate snapshots
    reads some relation before the commit and some after in at least one run (the
    mutant control below proves the harness detects exactly that). A single
    snapshot can only return all-before or all-after."""

    loader_base.require()
    generation = _interleave(loader_base, locked, _v().load_generation)
    assert _fingerprint(generation) in ((False, False, 0), (True, True, 1)), (
        locked,
        _fingerprint(generation),
    )
    assert _fingerprint(_load(loader_base, loader_base.schema)) == (False, False, 0)


async def _per_relation_mutant(conn: Any, schema_key: str) -> Any:
    """A deliberately WRONG loader for the harness control: the same initial catalog
    existence probe as the real loader, then one autocommit (READ COMMITTED)
    statement per relation, so each read has its own snapshot."""

    from types import SimpleNamespace

    s = sql.Identifier(schema_key)
    await conn.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n"
        " ON n.oid = c.relnamespace WHERE n.nspname = %s",
        (schema_key,),
    )
    contracts = await (
        await conn.execute(
            sql.SQL(
                "SELECT c.event_type, t.is_active FROM {}.event_contracts c"
                " JOIN {}.event_types t ON t.event_type = c.event_type"
            ).format(s, s)
        )
    ).fetchall()
    resolvers = await (
        await conn.execute(
            sql.SQL("SELECT resolver_id, is_active FROM {}.m02_link_resolvers").format(s)
        )
    ).fetchall()
    intake = await (
        await conn.execute(sql.SQL("SELECT 1 FROM {}.m05_intake_map").format(s))
    ).fetchall()
    return SimpleNamespace(
        contracts=[SimpleNamespace(event_type=e, is_active=a) for e, a in contracts],
        resolvers=[SimpleNamespace(resolver_id=r, is_active=a) for r, a in resolvers],
        intake_rows=intake,
    )


@postgres
def test_mc08_snapshot_harness_detects_a_per_relation_mutant(loader_base: M05Install) -> None:
    """Codex gate condition (20261101T000802Z): the interleaving must catch a
    separate-snapshot / READ COMMITTED loader. Run the same harness over the four
    lock targets with the mutant: at least one run must return a MIXED generation."""

    loader_base.require()
    coherent = {(False, False, 0), (True, True, 1)}
    observed = {
        locked: _fingerprint(_interleave(loader_base, locked, _per_relation_mutant))
        for locked in _LOCK_TARGETS
    }
    assert any(fingerprint not in coherent for fingerprint in observed.values()), observed


def _admin_rows(install: M05Install, query: str, params: Any = None) -> list[tuple[Any, ...]]:
    return m02.admin_all(install.ids, query, params)


@postgres
def test_mc08_loader_transaction_is_observed_read_only(loader_probe: M05Install) -> None:
    """F-B2-SNAPSHOT01 (read-only half). Disposable fixture: the intake table is
    fronted by a view that calls a probe function; the probe raises a NOTICE with
    `transaction_read_only` from INSIDE the loader's own transaction. The caller's
    connection is default-writable, so a read-only observation is the loader's own
    doing. One production intake row keeps the generation production-valid."""

    loader_probe.require()
    s = sql.Identifier(loader_probe.schema)
    _admin_exec(
        loader_probe.ids,
        sql.SQL(
            "ALTER TABLE {s}.m05_intake_map RENAME TO m05_intake_map_real;"
            " CREATE FUNCTION {s}.mb2_probe() RETURNS boolean LANGUAGE plpgsql AS $f$"
            " BEGIN RAISE NOTICE 'mb2-probe read_only=%',"
            " current_setting('transaction_read_only'); RETURN true; END $f$;"
            " CREATE VIEW {s}.m05_intake_map AS SELECT r.* FROM {s}.m05_intake_map_real r"
            " WHERE (SELECT {s}.mb2_probe());"
            " INSERT INTO {s}.m05_intake_map_real VALUES ('m05_followup_resolution_asserted', 1,"
            " 'outcome_code', 'other', 'unclear_reply',"
            " '00000000-0000-4000-8000-0000000b2001', true)"
        ).format(s=s),
    )
    notices: list[str] = []

    async def run() -> Any:
        async with await AsyncConnection.connect(loader_probe.ids.admin, autocommit=True) as conn:
            await conn.execute("SET default_transaction_read_only = off")
            conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
            return await _v().load_generation(conn, loader_probe.schema)

    generation = asyncio.run(run())
    probes = [n for n in notices if n.startswith("mb2-probe ")]
    assert probes, f"the probe view was not read by the loader: {notices}"
    assert set(probes) == {"mb2-probe read_only=on"}, probes
    assert len(generation.intake_rows) == 1
    assert _validate(generation, "production") is None


@postgres
@pytest.mark.parametrize(
    "malformed",
    [
        '{"handoff_kind": 1, "signal_type": "m02.event.appended"}',
        '{"handoff_kind": "event_signal", "signal_type": true}',
    ],
    ids=["handoff-kind-number", "signal-type-boolean"],
)
def test_mc08_loader_refuses_malformed_row_with_valid_controls(
    loader_base: M05Install, malformed: str
) -> None:
    """F-B2-MALFORMED01. A contract `signal_rule` whose keys pass the table CHECK
    but whose values are not strings is a malformed row. On an INACTIVE contract
    the validator would never look at it, so only the loader's own decoding can
    refuse: GENERATION_UNREADABLE, never coercion, omission or an empty accepted
    generation. Valid before/after controls on the same tenant."""

    loader_base.require()
    v = _v()
    assert _validate(_load(loader_base, loader_base.schema), "production") is None
    s = sql.Identifier(loader_base.schema)
    _admin_exec(
        loader_base.ids,
        sql.SQL(
            "UPDATE {s}.event_contracts SET signal_rule = %s::jsonb"
            " WHERE event_type = 'm05_followup_resolution_asserted'"
        ).format(s=s),
        (malformed,),
    )
    try:
        with pytest.raises(v.RegistryRefused) as caught:
            _load(loader_base, loader_base.schema)
        assert caught.value.reason_code == "GENERATION_UNREADABLE"
    finally:
        _admin_exec(
            loader_base.ids,
            sql.SQL(
                "UPDATE {s}.event_contracts SET signal_rule ="
                " '{{\"handoff_kind\":\"event_signal\",\"signal_type\":\"m02.event.appended\"}}'"
                "::jsonb WHERE event_type = 'm05_followup_resolution_asserted'"
            ).format(s=s),
        )
    assert _validate(_load(loader_base, loader_base.schema), "production") is None
