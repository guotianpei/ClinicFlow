"""CP2-1 -- I layer: the typed path through the REAL runner, v5.1 sections 5.2-5.7.

PRE-CHANGE STATE: RED / INTERFACE, every case. Each builds its registry through
`seam.typed_definition`, which raises `InterfaceAbsent` until the typed
vocabulary exists. That is recorded as the expected pre-change observation, per
case, in `EVIDENCE.md`. It is not a verdict on any assertion below; the
assertions are what the executable-assertion review is asked to judge.

HOW THE RUNNER IS REACHED
-------------------------
Exactly as v11: `TenantMigrationRunner(connect=..., manifest=...)`, both
production constructor parameters, via the `typed_driver` fixture over
`TypedConnection` (v11's `RecordingConnection` plus raw capture, faults and
hooks). The checker, issuer, consumer and phase checks are observed through the
private seams named in `support/typed_plan_seam.py` (S-1 to S-5), using the
monkeypatch pattern `test_function_policy.py` already uses on
`_validate_local_statements`. No production parameter is added.

ORACLES THAT ARE INDEPENDENT OF THE IMPLEMENTATION
--------------------------------------------------
* Bound bytes: `validate_function_installation(...).sql_bytes` from the FROZEN CP1
  checker, computed in the test.
* Declaration checksum: `function_checksum(...)`, the frozen v3 helper.
* Legacy checksum: the unit's own v2 `checksum`, from the unchanged ordinary route.
None of them is read back from the thing under test.

WHAT THIS CANNOT ESTABLISH
--------------------------
Everything v11 says about the recording harness holds: it records calls. It is
not PostgreSQL. Function ownership, ACLs, `search_path`, privilege behaviour and
rollback are D-layer facts, and EC-1, EC-2 and EC-3 stay OPEN whatever this module
shows. TP-40 is a call-sequence control, not EC-2.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import psycopg.errors
import pytest
from psycopg import sql

from haloflow.m01.errors import MigrationUnitRejected, TenantMigrationFailed
from haloflow.m01.provisioning.codes import PreconditionCode, SanitizedErrorCode
from haloflow.m01.provisioning.function_checksum import function_checksum
from haloflow.m01.provisioning.function_policy import validate_function_installation
from haloflow.m01.provisioning.manifest import (
    ExecutionRoleProfile,
    ProvisioningManifest,
    load_provisioning_manifest,
)
from haloflow.m01.provisioning.units import (
    TenantMigrationRegistry,
    UnitDefinition,
    build_tenant_migration_registry,
)

TENANT = "clinic-a"
SCHEMA = "tenant_aaaaaaaa"
ALT_SCHEMA = "tenant_bbbbbbbb"
OWNER = "haloflow_m02_owner"
ANNEX = "haloflow_m02_annex"
MIGRATOR = "haloflow_migrator"
ROLES = frozenset({OWNER, ANNEX})

# Proposed / owner-approved codes not yet in `codes.py` -- compared as strings.
PLAN_INVALID = "INSTALL_PLAN_INVALID"
NUL_FORBIDDEN = PreconditionCode.INSTALL_NUL_FORBIDDEN.value

CHECKSUM_FIELDS = ("migration_id", "template", "execution_role", "verification", "policy")


# ---------------------------------------------------------------------------
# Construction.
# ---------------------------------------------------------------------------


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def bound_bytes(payload: dict[str, Any], schema_key: str = SCHEMA) -> bytes:
    """The independent oracle: what the frozen CP1 checker binds."""

    return validate_function_installation(encode(payload), schema_key=schema_key).sql_bytes


def checksum_of(payload: dict[str, Any]) -> str:
    return function_checksum(**{key: payload[key] for key in CHECKSUM_FIELDS})


def typed_registry(seam: Any, *payloads: dict[str, Any]) -> TenantMigrationRegistry:
    return build_tenant_migration_registry(
        {payload["migration_id"]: seam.typed_definition(payload) for payload in payloads},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )


def manifest_declaring(*roles: str) -> ProvisioningManifest:
    """v11's `dataclasses.replace` seam on the LOADED manifest (Codex-ruled legitimate)."""

    profile = ExecutionRoleProfile(
        login=False,
        superuser=False,
        createdb=False,
        createrole=False,
        replication=False,
        bypassrls=False,
        tenant_schema_privileges=("USAGE",),
    )
    return dataclasses.replace(
        load_provisioning_manifest(),
        execution_role_profiles={role: profile for role in roles},
    )


def role_answers(harness: Any) -> tuple[Any, ...]:
    """Stage 1's per-role reads, answered SAFE. Assumptions supplied, not observed."""

    return (
        harness.Answer(markers=("select rolcanlogin", "pg_roles"), rows=((False,) * 6,)),
        harness.Answer(markers=("pg_has_role",), rows=((True,),)),
    )


def drive(
    typed_driver: Any,
    harness: Any,
    registry: TenantMigrationRegistry,
    ledger: Any,
    *,
    roles: tuple[str, ...] = (OWNER,),
    name: str = "work",
) -> tuple[Any, Any]:
    """One runner over one `TypedConnection`, for `apply_within_lock`."""

    runner, (connection,) = typed_driver(
        registry,
        (*role_answers(harness), ledger),
        manifest=manifest_declaring(*roles),
        names=(name,),
    )
    return runner, connection


def spy_on(monkeypatch: Any, where: tuple[Any, str], spy: Any) -> Any:
    owner, name = where
    monkeypatch.setattr(owner, name, spy.wrap(getattr(owner, name)))
    return spy


async def refused(runner: Any, schema_key: str = SCHEMA) -> str:
    """Run, require a refusal in the runner's sanitized type (TP-R17), return its code."""

    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=schema_key)
    return caught.value.reason_code


# ---------------------------------------------------------------------------
# Trace predicates -- v11's, unchanged in meaning.
# ---------------------------------------------------------------------------


def ledger_writes(connection: Any) -> tuple[Any, ...]:
    return tuple(
        entry
        for entry in connection.statements
        if "shared.schema_migrations" in entry.fingerprint
        and not entry.fingerprint.startswith("select")
    )


def starts_with(prefix: str) -> Callable[[Any], bool]:
    lowered = prefix.casefold()
    return lambda entry: entry.kind == "statement" and entry.fingerprint.startswith(lowered)


def applied_write() -> Callable[[Any], bool]:
    """Matches the write that SETS `applied` -- not `_record_failed`'s UPDATE (v11 fix 1)."""

    return lambda entry: (
        entry.kind == "statement"
        and entry.fingerprint.startswith("update shared.schema_migrations")
        and "set state = 'applied'" in entry.fingerprint
    )


def failed_write() -> Callable[[Any], bool]:
    return lambda entry: (
        entry.kind == "statement"
        and entry.fingerprint.startswith("update shared.schema_migrations")
        and "set state = 'failed'" in entry.fingerprint
    )


def ddl_index(connection: Any, sql_bytes: bytes) -> int:
    """The single trace position whose RAW query is exactly these bytes."""

    positions = [
        seq for seq, query in connection.raw if _as_bytes(query) == sql_bytes
    ]
    assert len(positions) == 1, f"expected exactly one execution, found {len(positions)}"
    return connection.index_of(lambda entry: entry.seq == positions[0])


def _as_bytes(query: object) -> bytes | None:
    if isinstance(query, bytes):
        return query
    if isinstance(query, str):
        return query.encode("utf-8", "strict")
    return None


# Every statement a REFUSED or SKIPPED typed operation may issue, and nothing else.
# Measured on 2026-09-20 from the real runner at `621cba7` (skip, drift and install
# paths of a role-bearing unit): session setup, stage 1's four reads, and the
# ledger read. EVERY entry is an exact, complete fingerprint (Q-6, accepted).
#
# v13 matched the membership read by prefix plus a table substring, so the
# membership query with `; DROP TABLE unexpected;` appended passed (Codex, v13
# review; reproduced by executing the helper). There is no prefix or substring
# match left anywhere in this allow-list.
_SESSION_AND_READS: tuple[str, ...] = (
    "set search_path = pg_catalog",
    'set role "haloflow_migrator"',
    "select rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls"
    " from pg_roles where rolname = %s",
    "select pg_has_role(%s, %s, 'set')",
    "select rolcreaterole from pg_catalog.pg_roles where rolname = %s",
    "select state, checksum from shared.schema_migrations"
    " where tenant_id = %s and migration_id = %s",
    "select role_row.rolname as role, member_role.rolname as member, edge.set_option,"
    " edge.inherit_option, edge.admin_option from pg_auth_members as edge"
    " join pg_roles as role_row on role_row.oid = edge.roleid"
    " join pg_roles as member_role on member_role.oid = edge.member"
    " where role_row.rolname = any(%s) and member_role.rolname = any(%s)",
)


def _permitted_without_install(fingerprint: str) -> bool:
    """Exact equality with a complete reviewed fingerprint. Nothing else."""

    return fingerprint in _SESSION_AND_READS


def assert_no_install(connection: Any) -> None:
    """NOTHING but session setup and the enumerated reads was issued.

    Replaces v12's `nothing_installed`, which excluded only the exact byte strings
    it was handed -- so unrelated or altered DDL passed (Codex's counterexample:
    `DROP TABLE unexpected;` against a forbidden `CREATE FUNCTION expected;`).
    This is an ALLOW-list over every statement in the trace, `Composable` queries
    included (they are in `statements`; `executed_bytes` skips them). Any ledger
    write, role switch, DDL or other statement fails, and no transaction is opened.
    """

    unexpected = [
        entry.text for entry in connection.statements
        if not _permitted_without_install(entry.fingerprint)
    ]
    assert unexpected == [], f"statements beyond session setup and reads: {unexpected}"
    assert "txn-begin" not in connection.kinds, "a transaction was opened"


def with_nul(sql_bytes: bytes) -> bytes:
    marked = sql_bytes.replace(b"CREATE FUNCTION", b"CREATE\x00 FUNCTION", 1)
    assert marked != sql_bytes and b"\x00" in marked
    return marked


# ===========================================================================
# 5.2  Policy invocation and byte identity
# ===========================================================================


async def test_tp13_a_typed_unit_calls_the_checker_exactly_once(
    seam: Any, harness: Any, typed_driver: Any, call_spy: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """TP-13, POSITIVE CONTROL. TP-12's "zero calls" is meaningful only after this."""

    checker = spy_on(monkeypatch, seam.checker_seam(), call_spy())
    registry = typed_registry(seam, typed_payloads["first"])
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [True]
    assert len(checker.calls) == 1
    assert checker.calls[0].raised is None
    assert checker.calls[0].kwargs["schema_key"] == SCHEMA


async def test_tp12_an_ordinary_unit_never_reaches_the_checker(
    seam: Any, harness: Any, typed_driver: Any, call_spy: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """TP-12. Zero checker calls on the ordinary route -- after proving the spy sees one.

    The positive control runs FIRST, in this test, on the same spy. v10/v11 do not
    prove this row: they never had a spy on the checker at all.
    """

    checker = spy_on(monkeypatch, seam.checker_seam(), call_spy())

    typed_runner, _ = drive(
        typed_driver, harness, typed_registry(seam, typed_payloads["first"]),
        harness.ledger_absent(), name="typed",
    )
    await typed_runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert len(checker.calls) == 1, "positive control: the spy is on the typed path"

    ordinary = build_tenant_migration_registry(
        {"t001_test_cp2": UnitDefinition("CREATE TABLE {schema}.cp2_probe (id int);")},
        allow_test_units=True,
    )
    ordinary_runner, (connection,) = typed_driver(
        ordinary, (harness.ledger_absent(),), names=("ordinary",)
    )
    outcomes = await ordinary_runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [True]
    assert len(checker.calls) == 1, "the ordinary unit reached the checker"


async def test_tp10_the_checker_runs_before_running_is_written_and_before_ddl(
    seam: Any, harness: Any, typed_driver: Any, call_spy: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """TP-10. Ordered on the SHARED clock -- one true order, not two lists.

    Deliberately NOT "before every execute": stage 1's reads and the ledger read
    may precede it (v5.1 section 5.2).
    """

    payload = typed_payloads["first"]
    checker = spy_on(monkeypatch, seam.checker_seam(), call_spy())
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, payload), harness.ledger_absent()
    )

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    (call,) = checker.calls
    running = connection.trace[
        connection.index_of(starts_with("insert into shared.schema_migrations"))
    ]
    ddl = connection.trace[ddl_index(connection, bound_bytes(payload))]
    assert call.seq < running.seq < ddl.seq


async def test_tp11_the_executed_bytes_are_exactly_the_bound_bytes(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """TP-11. Byte identity with the envelope AND with the frozen checker's bytes.

    Compared from the RAW query object, not the trace text.
    """

    payload = typed_payloads["first"]
    issued: list[Any] = []
    typed_harness.intercept(
        monkeypatch, seam.issuer_seam(), after=lambda env: issued.append(env) or env
    )
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, payload), harness.ledger_absent()
    )

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    (envelope,) = issued
    expected = bound_bytes(payload)
    assert seam.read(envelope, "sql_bytes") == expected
    assert typed_harness.executed_bytes(connection).count(expected) == 1


# ===========================================================================
# 5.3  Ledger identity
# ===========================================================================


async def test_tp24a_a_valid_declaration_change_is_drift(
    seam: Any, harness: Any, typed_driver: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-24a. A TWO-FIELD coherent declaration change -> drift (Q-5 ruling).

    `execution_role` and `verification.functions[0].owner` move together, owner ->
    annex, template constant, both declarations admitted (fixture controls). This is
    evidence that a coherent declaration change moves the typed identity; it is NOT
    isolated evidence of role sensitivity.

    The ledger holds the owner declaration's typed checksum; the candidate is the
    annex declaration. The typed identity must move, so this is drift.
    """

    owner, annex = typed_payloads["first"], typed_payloads["annex"]
    assert checksum_of(owner) != checksum_of(annex)
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, annex),
        harness.ledger_row("applied", checksum_of(owner)), roles=(ANNEX,),
    )

    assert await refused(runner) == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
    assert_no_install(connection)


async def test_tp24b_i_the_runner_writes_the_typed_checksum_as_ledger_identity(
    seam: Any, harness: Any, typed_driver: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-24b-i. The `running` row carries `function_checksum` -- and NOT the v2 digest.

    Separate from TP-24b by design (Codex, v5 acceptance, correction 2): this shows
    WHICH algorithm the runner uses; TP-24b shows the algorithm is policy-sensitive.
    The v2 inequality is what lets this observation discriminate between them.
    """

    payload = typed_payloads["first"]
    registry = typed_registry(seam, payload)
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    running = connection.trace[
        connection.index_of(starts_with("insert into shared.schema_migrations"))
    ]
    assert running.params == (TENANT, payload["migration_id"], checksum_of(payload))
    legacy = build_tenant_migration_registry(
        {payload["migration_id"]: UnitDefinition(payload["template"], execution_role=OWNER)},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    ).units[0].checksum
    assert running.params[2] != legacy


async def test_tp24d_applied_at_an_equal_typed_checksum_skips_the_checker(
    seam: Any, harness: Any, typed_driver: Any, call_spy: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """TP-24d, section 3.6. Skipped: no actual-schema checker call, no execute, no write.

    Still applied to a skipped entry, and asserted: stage 1 (the membership read)
    and the ledger checksum read. Skipping is not an attestation of current
    compliance -- that limitation is the contract's, recorded, not tested away.
    """

    payload = typed_payloads["first"]
    checker = spy_on(monkeypatch, seam.checker_seam(), call_spy())
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, payload),
        harness.ledger_row("applied", checksum_of(payload)),
    )

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [False]
    assert checker.calls == []
    assert_no_install(connection)
    assert len(connection.issued("from pg_auth_members")) == 1
    assert len(connection.issued("from shared.schema_migrations")) == 1


async def test_tp25_an_applied_v2_identity_is_not_reused_by_the_same_id_typed(
    seam: Any, harness: Any, typed_driver: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-25 / TP-R7b. Ordinary id applied under v2, same id now typed: refused.

    The code is PROPOSED (README Q-3): v5.1 says "refused" without one.
    `MIGRATION_CHECKSUM_DRIFT` is what an unequal applied checksum already means
    on this runner, and it is the refusal that cannot be confused with a skip.
    """

    payload = typed_payloads["first"]
    applied_v2 = build_tenant_migration_registry(
        {payload["migration_id"]: UnitDefinition(payload["template"])},
        allow_test_units=True,
    ).units[0].checksum
    # Q-3 premise, for THIS role-less legacy fixture: the applied v2 identity and the
    # candidate's v3 identity differ, so an equal-checksum skip is impossible here.
    assert applied_v2 != checksum_of(payload)
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, payload),
        harness.ledger_row("applied", applied_v2),
    )

    assert await refused(runner) == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
    assert_no_install(connection)


# TP-26 (ordinary applied / drift paths unchanged, EX-01 preserved) is NOT
# re-written: v11's `test_applied_at_the_same_checksum_touches_nothing` and
# `test_applied_at_a_different_checksum_is_drift_and_changes_nothing` are that row,
# accepted and closed. Running v11 unchanged beside this module is the check.


# ===========================================================================
# 5.4  Binding, provenance and the plan boundary
# ===========================================================================


def _phase_spies(seam: Any, monkeypatch: Any, call_spy: Any) -> tuple[Any, Any]:
    """Spies on the two phase checks: v5.1 3.7's internal phase oracle (S-3)."""

    return (
        spy_on(monkeypatch, seam.provenance_seam(), call_spy()),
        spy_on(monkeypatch, seam.binding_seam(), call_spy()),
    )


BINDING_MUTATIONS = {
    "B-schema": ("schema_key", lambda seam, p, r: ALT_SCHEMA),
    "B-role": ("execution_role", lambda seam, p, r: ANNEX),
    "B-unit": ("migration_id", lambda seam, p, r: p["second"]["migration_id"]),
    "B-registry": ("registry", lambda seam, p, r: seam.registry_identity(r)),
    "B-checksum": ("declaration_checksum", lambda seam, p, r: checksum_of(p["annex"])),
    "B-digest": (
        "byte_digest",
        lambda seam, p, r: seam.digest_of(bound_bytes(p["first"], ALT_SCHEMA)),
    ),
    "B-policy-version": ("policy_version", lambda seam, p, r: 2),
}


@pytest.mark.parametrize("case", sorted(BINDING_MUTATIONS))
async def test_a_single_binding_mutation_is_refused_by_the_binding_phase(
    case: str, seam: Any, harness: Any, typed_harness: Any, typed_driver: Any,
    call_spy: Any, monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """B-schema, B-role, B-unit, B-registry, B-checksum, B-digest, B-policy-version.

    ONE field of a genuinely issued envelope is replaced with a real, well-formed
    alternate, provenance preserved (S-4) -- so the case reaches the binding check
    instead of being masked by provenance. The trusted store keeps the true value:
    "bound X -> store expects Y" is realised as envelope Y against store X, which
    is the same single mismatch. `B-policy-version` is exactly v5.1's framing:
    the CANDIDATE moves to 2, the store stays at the supported 1.

    `B-checksum` and `B-digest` are distinct because each moves only its own
    field, asserted below.
    """

    field, alternate = BINDING_MUTATIONS[case]
    first = typed_payloads["first"]
    other_registry = typed_registry(seam, typed_payloads["second"])
    replacement = alternate(seam, typed_payloads, other_registry)

    originals: list[Any] = []

    def mutate(envelope: Any) -> Any:
        originals.append(envelope)
        before = {name: seam.read(envelope, name) for name in seam.PLAN_FIELDS}
        changed = seam.alter(envelope, **{field: replacement})
        for other in seam.PLAN_FIELDS:
            if other != field:
                assert seam.read(changed, other) == before[other]
        assert seam.read(changed, field) != before[field]
        # Q-4: the ISSUED envelope -- what the issuer recorded -- is unchanged by
        # `alter`. What the trusted store holds is not observable through the seam;
        # this is the part of "independent expectations unchanged" a test can see.
        assert {name: seam.read(envelope, name) for name in seam.PLAN_FIELDS} == before
        return changed

    typed_harness.intercept(monkeypatch, seam.issuer_seam(), after=mutate)
    provenance, binding = _phase_spies(seam, monkeypatch, call_spy)
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first), harness.ledger_absent()
    )

    assert await refused(runner) == PLAN_INVALID
    assert len(originals) == 1
    # Positive phase control: provenance ran once and PASSED ...
    assert len(provenance.calls) == 1 and provenance.raised == ()
    # ... and the binding phase is what refused, with the plan code.
    (raised,) = binding.raised
    assert getattr(raised, "reason_code", None) == PLAN_INVALID
    assert_no_install(connection)


async def test_b_substitution_coordinated_bytes_and_digest_are_refused_at_binding(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, call_spy: Any,
    monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """B-substitution -- TP-R9b and TP-R11, the independently retained digest.

    Valid provenance. The bytes are replaced with DIFFERENT, NUL-free, admitted
    bytes (the second unit's), AND the candidate digest is replaced with the digest
    of those new bytes -- so the envelope is internally consistent. Only the
    trusted, independently retained digest still describes the original bytes.

    The premise assertion inside `substitute` establishes that a binding check
    which only verified candidate-digest == hash(candidate-bytes) would ACCEPT
    this envelope. `B-digest` (stale digest) and the bound-NUL cases (stop before
    binding) cannot show that.

    SCOPE, per Codex's v13 review: a refusal here does NOT by itself uniquely prove
    an independently retained digest -- another binding field could be what
    refuses. Two further steps are owed at implementation, when the interface
    exists: run the self-consistency-only mutant and show THIS case fails against
    it, and inspect the trusted expectations directly in the implementation review.
    """

    first = typed_payloads["first"]
    original = bound_bytes(first)
    other = bound_bytes(typed_payloads["second"])
    assert other != original and b"\x00" not in other

    def substitute(envelope: Any) -> Any:
        changed = seam.alter(envelope, sql_bytes=other, byte_digest=seam.digest_of(other))
        # Premise: the candidate is self-consistent, and differs from what was bound.
        assert seam.read(changed, "byte_digest") == seam.digest_of(seam.read(changed, "sql_bytes"))
        assert seam.read(changed, "byte_digest") != seam.read(envelope, "byte_digest")
        assert seam.read(envelope, "sql_bytes") == original
        return changed

    typed_harness.intercept(monkeypatch, seam.issuer_seam(), after=substitute)
    provenance, binding = _phase_spies(seam, monkeypatch, call_spy)
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first), harness.ledger_absent()
    )

    assert await refused(runner) == PLAN_INVALID
    assert len(provenance.calls) == 1 and provenance.raised == ()
    (raised,) = binding.raised
    assert getattr(raised, "reason_code", None) == PLAN_INVALID
    assert_no_install(connection)


async def test_b_provenance_a_caller_fabricated_envelope_is_refused_first(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, call_spy: Any,
    monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """B-provenance / TP-R12a. Identical visible fields, caller-made: refused at phase 1.

    Frozen or private construction is not provenance; the issuer comparison is.
    The binding phase is never reached.
    """

    first = typed_payloads["first"]
    typed_harness.intercept(monkeypatch, seam.issuer_seam(), after=seam.fabricate)
    provenance, binding = _phase_spies(seam, monkeypatch, call_spy)
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first), harness.ledger_absent()
    )

    assert await refused(runner) == PLAN_INVALID
    (raised,) = provenance.raised
    assert getattr(raised, "reason_code", None) == PLAN_INVALID
    assert binding.calls == []
    assert_no_install(connection)


async def test_b_replay_an_envelope_from_a_completed_operation_is_refused(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, call_spy: Any,
    monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """B-replay. A GENUINE envelope, valid in its own operation, replayed into a second."""

    first = typed_payloads["first"]
    registry = typed_registry(seam, first)
    captured: list[Any] = []

    def capture_then_replay(envelope: Any) -> Any:
        if not captured:
            captured.append(envelope)
            return envelope
        return captured[0]

    typed_harness.intercept(monkeypatch, seam.issuer_seam(), after=capture_then_replay)

    first_runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent(), name="op1")
    outcomes = await first_runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert [outcome.applied for outcome in outcomes] == [True], "the envelope was valid once"

    provenance, binding = _phase_spies(seam, monkeypatch, call_spy)
    second_runner, connection = drive(
        typed_driver, harness, registry, harness.ledger_absent(), name="op2"
    )

    assert await refused(second_runner) == PLAN_INVALID
    (raised,) = provenance.raised
    assert getattr(raised, "reason_code", None) == PLAN_INVALID
    assert binding.calls == []
    assert_no_install(connection)


async def test_b_after_await_mutating_the_source_after_the_snapshot_has_no_effect(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """B-after-await / TP-R9a. The snapshot, not the source, is what executes.

    The source envelope's bytes are replaced IN PLACE at a real `await` point --
    the `running` INSERT, which TP-R6 places after every consumption check and so
    after the snapshot. The original bytes still execute, exactly once, and the
    substituted bytes never do.
    """

    first = typed_payloads["first"]
    expected = bound_bytes(first)
    substitute = bound_bytes(typed_payloads["second"])
    issued: list[Any] = []
    typed_harness.intercept(
        monkeypatch, seam.issuer_seam(), after=lambda env: issued.append(env) or env
    )
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first), harness.ledger_absent()
    )
    connection.hooks.append(
        typed_harness.Hook(
            markers=("insert into shared.schema_migrations",),
            action=lambda: seam.force_set(issued[0], "sql_bytes", substitute),
        )
    )

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert connection.hooks[0].fired, "the mutation never happened; the case would be vacuous"
    assert [outcome.applied for outcome in outcomes] == [True]
    executed = typed_harness.executed_bytes(connection)
    assert executed.count(expected) == 1
    assert substitute not in executed


def _count_renders(monkeypatch: Any, seam: Any, calls: list[str]) -> None:
    """Replace every renderer with one that records the call and returns WRONG bytes."""

    for owner, name in seam.renderer_seams():

        def rendered(*args: Any, _name: str = name, **kwargs: Any) -> Any:
            calls.append(_name)
            return "SELECT 'rerendered'" if _name == "render" else b"SELECT 'rerendered'"

        monkeypatch.setattr(owner, name, rendered)


def _assert_no_rerender(calls: list[str], executed: tuple[bytes, ...], expected: bytes) -> None:
    """B-rerender's assertions, as one function, so the mutant case can show they bite.

    `expected` MUST be computed before any renderer is patched. v12 computed it
    afterwards, through the frozen checker -- which resolves `_render_exact_sql`
    at call time and so ran the PATCHED renderer, polluting `calls` and the oracle
    (Codex, v12 review, item 1). Each assertion carries its own message so a
    failure is attributable to the assertion that fired.
    """

    assert calls == [], f"renderer called after binding: {calls}"
    assert executed.count(expected) == 1, "the bound bytes did not execute exactly once"
    assert not any(b"rerendered" in item for item in executed), "re-rendered bytes executed"


async def test_b_rerender_replacing_the_renderer_after_binding_has_no_effect(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """B-rerender / TP-R5. Zero renderer calls after binding; the original bytes execute."""

    first = typed_payloads["first"]
    expected = bound_bytes(first)  # BEFORE any renderer is patched
    calls: list[str] = []

    def after_binding(envelope: Any) -> Any:
        _count_renders(monkeypatch, seam, calls)
        return envelope

    typed_harness.intercept(monkeypatch, seam.issuer_seam(), after=after_binding)
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first), harness.ledger_absent()
    )

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    _assert_no_rerender(calls, typed_harness.executed_bytes(connection), expected)


async def test_b_rerender_mutant_a_rerendering_consumer_fails_those_assertions(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """B-rerender-mutant. The same assertions FAIL against a consumer that re-renders.

    The mutant is isolated to this test: the consumer seam is wrapped so that it
    renders the unit again before delegating. Nothing else changes. If
    `_assert_no_rerender` still passed here, B-rerender would have no teeth.
    """

    first = typed_payloads["first"]
    expected = bound_bytes(first)  # BEFORE any renderer is patched
    registry = typed_registry(seam, first)
    calls: list[str] = []

    def after_binding(envelope: Any) -> Any:
        _count_renders(monkeypatch, seam, calls)
        return envelope

    typed_harness.intercept(monkeypatch, seam.issuer_seam(), after=after_binding)
    typed_harness.intercept(
        monkeypatch,
        seam.consumer_seam(),
        before=lambda *args, **kwargs: registry.units[0].render(SCHEMA),
    )
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    executed = typed_harness.executed_bytes(connection)
    assert calls == ["render"], "the mutant re-rendered exactly once, through the unit"
    with pytest.raises(AssertionError, match="^renderer called after binding"):
        _assert_no_rerender(calls, executed, expected)


async def test_tp20_a_body_drift_is_the_checkers_refusal_not_a_plan_refusal(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, call_spy: Any,
    monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """TP-20. `A-body-drift` -> `INSTALL_BODY_MISMATCH` (measured), preserved (TP-R17).

    The refusal comes from the checker, observed on the checker spy, and no
    envelope is ever issued -- so it cannot be mistaken for a plan refusal.
    """

    payload = typed_payloads["body_drift"]
    checker = spy_on(monkeypatch, seam.checker_seam(), call_spy())
    issues: list[Any] = []
    typed_harness.intercept(
        monkeypatch, seam.issuer_seam(), before=lambda *a, **k: issues.append(a)
    )
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, payload), harness.ledger_absent()
    )

    assert await refused(runner) == PreconditionCode.INSTALL_BODY_MISMATCH.value
    (raised,) = checker.raised
    assert isinstance(raised, MigrationUnitRejected)
    assert raised.reason_code == PreconditionCode.INSTALL_BODY_MISMATCH.value
    assert issues == []
    assert_no_install(connection)


# ===========================================================================
# 5.5  Whole-plan preflight and failure recording
# ===========================================================================


async def test_b_late_invalid_a_bad_second_unit_stops_the_whole_plan(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """B-late-invalid. First unit valid, second refused by policy: NOTHING runs.

    Its actual refusal is asserted -- `INSTALL_BODY_MISMATCH`, not rewritten to a
    plan code (Codex, note-349 review). Zero DDL and zero `running` writes for BOTH
    units, the valid first one included.
    """

    first, second = typed_payloads["first"], typed_payloads["second_body_drift"]
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first, second), harness.ledger_absent()
    )

    assert await refused(runner) == PreconditionCode.INSTALL_BODY_MISMATCH.value
    assert_no_install(connection)


async def test_b_late_failure_typed_first_ddl_fails_after_running_committed(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """B-late-failure, the TYPED case: two admitted declarations, distinct identities.

    v5.1 section 5.5's measured trace is an ORDINARY-runner analogue (two `CREATE
    TABLE` units) and is labelled so. This is the typed case it specifies: the
    first unit's DDL raises after its `running` row committed. Asserted, as for
    the analogue: the DDL transaction rolled back; `failed` recorded in its OWN
    later transaction with exact parameters; no `applied` write; the second unit's
    DDL never attempted.
    """

    first, second = typed_payloads["first"], typed_payloads["second"]
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first, second), harness.ledger_absent()
    )
    connection.faults.append(
        typed_harness.Fault(
            markers=("create function", "m02_annex_probe("),
            error=lambda: psycopg.errors.DuplicateFunction("injected"),
        )
    )

    assert await refused(runner) == SanitizedErrorCode.MIGRATION_DDL_FAILED.value
    assert connection.faults[0].fired

    running = connection.index_of(starts_with("insert into shared.schema_migrations"))
    ddl = ddl_index(connection, bound_bytes(first))
    failed = connection.index_of(failed_write())
    running_txn = connection.trace[running].txn
    ddl_txn = connection.trace[ddl].txn
    failed_txn = connection.trace[failed].txn

    assert running_txn is not None and ddl_txn is not None and failed_txn is not None
    assert len({running_txn, ddl_txn, failed_txn}) == 3
    assert (
        connection.index_of(lambda e: e.kind == "txn-commit" and e.txn == running_txn)
        < ddl
        < connection.index_of(lambda e: e.kind == "txn-rollback" and e.txn == ddl_txn)
        < failed
        < connection.index_of(lambda e: e.kind == "txn-commit" and e.txn == failed_txn)
    )
    assert connection.trace[running].params == (
        TENANT, first["migration_id"], checksum_of(first)
    )
    assert connection.trace[failed].params == (
        SanitizedErrorCode.MIGRATION_DDL_FAILED.value, TENANT, first["migration_id"]
    )
    assert not any(applied_write()(entry) for entry in connection.trace)
    assert bound_bytes(second) not in typed_harness.executed_bytes(connection)


# ===========================================================================
# 5.6  NUL, and pairwise precedence
# ===========================================================================


async def test_tp30_a_nul_in_the_payload_is_refused_before_render_or_execute(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """TP-30. `INSTALL_NUL_FORBIDDEN` (measured at the checker); no render, no execute.

    Composition MAY refuse it (a NUL is a declaration fact) or the runner's checker
    call may; both are allowed and the code is the same. If it gets as far as the
    runner, nothing is rendered, written or executed.
    """

    payload = typed_payloads["payload_nul"]
    renders: list[str] = []
    for owner, name in seam.renderer_seams():
        real = getattr(owner, name)

        def counted(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            renders.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)

    try:
        registry = typed_registry(seam, payload)
    except MigrationUnitRejected as error:
        assert error.reason_code == NUL_FORBIDDEN
        assert renders == []
        return

    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())
    assert await refused(runner) == NUL_FORBIDDEN
    assert renders == []
    assert_no_install(connection)


async def test_tp30a_a_nul_emitted_by_the_renderer_is_refused_at_the_render_check(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, call_spy: Any,
    monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """TP-30a. A test-only renderer emits a NUL; the checker's render check refuses it.

    Test-only: CP1's private `_render_exact_sql` is replaced for this test. No
    public API changes and no production bypass is created.
    """

    first = typed_payloads["first"]
    from haloflow.m01.provisioning import function_policy

    real_render = function_policy._render_exact_sql
    monkeypatch.setattr(
        function_policy,
        "_render_exact_sql",
        lambda template, schema_key: with_nul(real_render(template, schema_key)),
    )
    checker = spy_on(monkeypatch, seam.checker_seam(), call_spy())
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first), harness.ledger_absent()
    )

    assert await refused(runner) == NUL_FORBIDDEN
    (raised,) = checker.raised
    assert isinstance(raised, MigrationUnitRejected) and raised.reason_code == NUL_FORBIDDEN
    assert_no_install(connection)


@pytest.mark.parametrize(
    ("case", "fabricated", "digest_follows", "code", "refusing_phase"),
    [
        ("TP-30b", False, True, NUL_FORBIDDEN, None),
        ("TP-31a", True, True, PLAN_INVALID, "provenance"),
        ("TP-31b", False, False, NUL_FORBIDDEN, None),
    ],
    ids=["TP-30b-bound-nul", "TP-31a-provenance-wins", "TP-31b-nul-before-binding"],
)
async def test_bound_nul_and_pairwise_precedence(
    case: str, fabricated: bool, digest_follows: bool, code: str, refusing_phase: str | None,
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any, call_spy: Any,
    monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """TP-30b, TP-31a, TP-31b -- v5.1 section 3.7's order: provenance -> NUL -> binding.

    TP-30b   NUL in the bound bytes, digest RECOMPUTED to match, provenance valid:
             only the NUL is wrong, so it must be what is reported.
    TP-31a   the same NUL, AND a caller-fabricated envelope: provenance wins.
    TP-31b   NUL with the digest left STALE -- NUL and binding both wrong: NUL wins,
             and the binding phase raises nothing.
    """

    first = typed_payloads["first"]
    nul_bytes = with_nul(bound_bytes(first))

    def mutate(envelope: Any) -> Any:
        fields: dict[str, Any] = {"sql_bytes": nul_bytes}
        if digest_follows:
            fields["byte_digest"] = seam.digest_of(nul_bytes)
        changed = seam.alter(envelope, **fields)
        return seam.fabricate(changed) if fabricated else changed

    typed_harness.intercept(monkeypatch, seam.issuer_seam(), after=mutate)
    provenance, binding = _phase_spies(seam, monkeypatch, call_spy)
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, first), harness.ledger_absent()
    )

    assert await refused(runner) == code
    if refusing_phase == "provenance":
        (raised,) = provenance.raised
        assert getattr(raised, "reason_code", None) == PLAN_INVALID
    else:
        assert len(provenance.calls) == 1 and provenance.raised == ()
    # Not merely "binding raised nothing": binding must not have RUN. A binding check
    # that ran and passed before the NUL refusal would breach the specified order.
    assert binding.calls == []
    assert_no_install(connection)


# TP-32 (legacy ordinary NUL) is NOT WRITTEN. EX-01: out of scope by owner
# decision, no new rejection assertion. v11's withdrawal of `B-nul-bound` x2 is
# the precedent.


# ===========================================================================
# 5.7  Typed outer-statement refusals, and typed role rows
# ===========================================================================


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("table", PreconditionCode.INSTALL_TOPLEVEL_FORM_FORBIDDEN.value),
        ("unknown_select", PreconditionCode.INSTALL_TOPLEVEL_FORM_UNKNOWN.value),
    ],
    ids=["TPX-01-A-table", "TPX-02-A-unknown-select"],
)
async def test_tpx_a_typed_outer_statement_refusal_reaches_the_runner_unchanged(
    fixture: str, code: str, seam: Any, harness: Any, typed_harness: Any,
    typed_driver: Any, call_spy: Any, monkeypatch: Any, typed_payloads: dict[str, Any],
) -> None:
    """TPX-01, TPX-02. Measured CP1 codes, preserved through the runner (TP-R17).

    O-3: the frozen outer-statement set is not widened. These rows pin that a
    typed `CREATE TABLE` and an unrecognised form are refused on the typed path,
    by the checker, with nothing written or executed.
    """

    checker = spy_on(monkeypatch, seam.checker_seam(), call_spy())
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, typed_payloads[fixture]),
        harness.ledger_absent(),
    )

    assert await refused(runner) == code
    (raised,) = checker.raised
    assert isinstance(raised, MigrationUnitRejected) and raised.reason_code == code
    assert_no_install(connection)


async def test_tp40_typed_role_transition_sequence(
    seam: Any, harness: Any, typed_driver: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-40. RUNNER ROLE-SWITCH CONTROL on the typed path. **Not EC-2.**

    The complete ordered role-transition sequence -- a subset assertion would pass
    with an extra transition spliced in (v11 fix 2). The role is assumed
    immediately before the installation DDL and dropped immediately after, all in
    one transaction, and the `running` row is written under the migrator in an
    earlier one. The safe-role answers are supplied by this test; ownership,
    membership and privilege are D-layer facts and stay PENDING.
    """

    payload = typed_payloads["first"]
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, payload), harness.ledger_absent()
    )

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert [outcome.applied for outcome in outcomes] == [True]

    assert tuple(
        entry.text
        for entry in connection.statements
        if entry.fingerprint.startswith(("set role", "set local role"))
    ) == (
        f'SET ROLE "{MIGRATOR}"',
        f'SET LOCAL ROLE "{OWNER}"',
        f'SET LOCAL ROLE "{MIGRATOR}"',
    )

    running = connection.index_of(starts_with("insert into shared.schema_migrations"))
    assume = connection.index_of(starts_with(f'set local role "{OWNER}"'))
    ddl = ddl_index(connection, bound_bytes(payload))
    restore = connection.index_of(starts_with(f'set local role "{MIGRATOR}"'))
    applied = connection.index_of(applied_write())

    assert running < assume
    assert connection.trace[running].txn != connection.trace[assume].txn
    assert assume + 1 == ddl
    assert ddl + 1 == restore
    assert restore < applied
    assert len({connection.trace[i].txn for i in (assume, ddl, restore, applied)}) == 1


async def test_tp41_a_typed_role_the_manifest_never_describes_is_refused_at_stage_one(
    seam: Any, harness: Any, typed_harness: Any, typed_driver: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """TP-41. Approved by name at composition, undescribed in the manifest: stage 1 refuses.

    The SHIPPED manifest is used unmodified, so the role has no profile. No ledger
    write: a configuration fault is not a tenant migration failure.
    """

    payload = typed_payloads["first"]
    runner, (connection,) = typed_driver(
        typed_registry(seam, payload), (harness.ledger_absent(),), names=("work",)
    )

    assert await refused(runner) == PreconditionCode.EXECUTION_ROLE_UNAVAILABLE.value
    assert_no_install(connection)


async def test_tp42_the_typed_applied_write_sets_applied_exactly(
    seam: Any, harness: Any, typed_driver: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-42. The `applied` write actually sets `state = 'applied'` -- not a prefix match.

    v11 fix 1 showed an `UPDATE shared.schema_migrations` with these parameters
    also describes `_record_failed`. Exactly one applied write, exact parameters,
    and no failed write at all.
    """

    payload = typed_payloads["first"]
    runner, connection = drive(
        typed_driver, harness, typed_registry(seam, payload), harness.ledger_absent()
    )

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    applied = connection.index_of(applied_write())
    assert connection.trace[applied].params == (TENANT, payload["migration_id"])
    assert not any(failed_write()(entry) for entry in connection.trace)


# ===========================================================================
# HELPER CONTROLS -- run TODAY, against the REAL runner on the ORDINARY route.
#
# Codex, v12 review item 2: the v12 no-install helper passed a trace containing
# `DROP TABLE unexpected;`. These controls show the replacement admits exactly a
# real refused/skipped trace and catches each kind of intrusion, including a
# `Composable` query and changed DDL bytes. They exercise the HELPERS, not the
# typed path -- they say nothing about typed behaviour.
# ===========================================================================

_ORDINARY_ROLE_SQL = "CREATE TABLE {schema}.cp2_helper_probe (id int);"


def _ordinary_role_registry() -> TenantMigrationRegistry:
    return build_tenant_migration_registry(
        {"t001_test_cp2_helper": UnitDefinition(_ORDINARY_ROLE_SQL, execution_role=OWNER)},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )


async def _real_skip_trace(typed_driver: Any, harness: Any) -> Any:
    """A real runner trace that installs nothing: role-bearing unit, applied, same checksum."""

    registry = _ordinary_role_registry()
    runner, connection = drive(
        typed_driver, harness, registry,
        harness.ledger_row("applied", registry.units[0].checksum),
    )
    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    assert [outcome.applied for outcome in outcomes] == [False]
    return connection


async def test_helper_assert_no_install_admits_a_real_skip_trace(
    typed_driver: Any, harness: Any
) -> None:
    """CONTROL: every statement the real runner issues on a no-install path is allowed."""

    connection = await _real_skip_trace(typed_driver, harness)
    assert len(connection.statements) == 7, "session setup, four stage-1 reads, ledger read"
    assert_no_install(connection)


async def test_helper_assert_no_install_rejects_a_real_install_trace(
    typed_driver: Any, harness: Any
) -> None:
    """The same unit actually installed: the helper must refuse that trace."""

    runner, connection = drive(
        typed_driver, harness, _ordinary_role_registry(), harness.ledger_absent()
    )
    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    with pytest.raises(AssertionError, match="statements beyond session setup and reads"):
        assert_no_install(connection)


def _intrusions() -> dict[str, Any]:
    changed = _ORDINARY_ROLE_SQL.replace("{schema}", SCHEMA).replace("(id int)", "(id bigint)")
    return {
        "unrelated-ddl": "DROP TABLE unexpected;",
        "changed-ddl-bytes": changed.encode("utf-8"),
        "composable-role-switch": sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER)),
        "ledger-write": "UPDATE shared.schema_migrations SET state = 'failed' "
        "WHERE tenant_id = %s AND migration_id = %s",
    }


@pytest.mark.parametrize("intrusion", sorted(_intrusions()))
async def test_helper_assert_no_install_catches_each_intrusion(
    intrusion: str, typed_driver: Any, harness: Any
) -> None:
    """COUNTEREXAMPLES: one extra statement, of each kind, appended to a clean real trace."""

    connection = await _real_skip_trace(typed_driver, harness)
    assert_no_install(connection)  # clean before the intrusion

    await connection.execute(_intrusions()[intrusion])

    with pytest.raises(AssertionError, match="statements beyond session setup and reads"):
        assert_no_install(connection)


@pytest.mark.parametrize(
    "reviewed",
    ["membership", "ledger-read"],
)
async def test_helper_assert_no_install_catches_ddl_appended_to_a_reviewed_read(
    reviewed: str, typed_driver: Any, harness: Any
) -> None:
    """COUNTEREXAMPLE (Codex, v13 review): the ACTUAL clean query, with DDL appended.

    The clean statement text is taken from the real trace, not retyped, so this is
    exactly the query the runner issues plus `; DROP TABLE unexpected;`. It is
    answered as a read (it is one, to the fake), and must still be refused.
    """

    connection = await _real_skip_trace(typed_driver, harness)
    assert_no_install(connection)  # clean control first
    marker = {"membership": "from pg_auth_members", "ledger-read": "from shared.schema_migrations"}
    (clean,) = connection.issued(marker[reviewed])

    await connection.execute(f"{clean.text.rstrip()}; DROP TABLE unexpected;")

    with pytest.raises(AssertionError, match="statements beyond session setup and reads"):
        assert_no_install(connection)


async def test_helper_assert_no_install_catches_an_opened_transaction(
    typed_driver: Any, harness: Any
) -> None:
    connection = await _real_skip_trace(typed_driver, harness)
    async with connection.transaction():
        pass

    with pytest.raises(AssertionError, match="a transaction was opened"):
        assert_no_install(connection)


def test_helper_rerender_assertions_attribute_each_failure() -> None:
    """Codex item 1: the clean control passes and each defect fails at ITS assertion."""

    expected = b"CREATE FUNCTION expected;"
    _assert_no_rerender([], (expected,), expected)  # clean control

    with pytest.raises(AssertionError, match="^renderer called after binding"):
        _assert_no_rerender(["render"], (expected,), expected)
    with pytest.raises(AssertionError, match="^the bound bytes did not execute exactly once"):
        _assert_no_rerender([], (), expected)
    with pytest.raises(AssertionError, match="^re-rendered bytes executed"):
        _assert_no_rerender([], (expected, b"SELECT 'rerendered'"), expected)
