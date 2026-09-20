"""CP2-1 correction -- OWNERSHIP: the declaration snapshot must be owned.

NEW FILE, a clearly identified supplement. Every v14 file stays byte-identical.

Responds to the v15 review, finding 2. `units.py` copies only the OUTER
dictionary (`dict(self.policy or {})`), so every nested list and mapping is
still the caller's object and `checksum` recomputes from those live values.
`@dataclass(frozen=True)` freezes the reference, not the graph.

Measured on b5b7f12 before these tests were written:

    PREFIX=6ef0819028a95867dee62685212cd7028400bce5d62e1ee19691dc4e6556c6be
    AFTER =09fe5c6697e5d532f9d1fc3b341d21b7f84e279a15071114f169a51a4b94c429
    MOVED=True          <- a caller mutating its own dict moved the unit's
                           LEDGER IDENTITY after composition
    PAYLOAD_LEAK='mutated via payload'
                        <- and mutating the RETURNED payload reaches the same
                           shared object

Why this is severe rather than untidy: the checksum is what pass 1 classifies
against and what is written to the ledger. If it can move between classification
and the ledger write -- and `_apply_locked` awaits in between -- the declaration
that was checked is not the declaration that is recorded.

WHY A MUTATION THAT RAISES IS NOT THE INTERESTING ONE
-----------------------------------------------------
Appending to a function's `config` raises `ValueError: Duplicate set identity`
in the canonicalizer, because `config` is set-keyed. That mutation fails LOUDLY
and is not the threat. These tests use `comment` and nested `inputs`, which
digest silently. A test suite that only covered the loud path would prove
nothing about the quiet one.

Pre-change state: RED. OWN-07 is GREEN today and must STAY green -- it is the
guard that the fix does not move any existing typed checksum.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any

import pytest
import typed_recording

from haloflow.m01.provisioning.function_policy import validate_function_installation
from haloflow.m01.provisioning.manifest import (
    ExecutionRoleProfile,
    ProvisioningManifest,
    load_provisioning_manifest,
)
from haloflow.m01.provisioning.units import build_tenant_migration_registry

TENANT = "clinic-a"
SCHEMA = "tenant_aaaaaaaa"
OWNER = "haloflow_m02_owner"
ROLES = frozenset({"haloflow_m02_owner", "haloflow_m02_annex"})


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

# Measured on b5b7f12, unmutated, for EVERY admitted typed fixture. Pinned as
# literals so that a correction which "works" by changing tenants' ledger
# identity cannot pass silently. Carried forward per Codex's v17 review.
ADMITTED_BASELINES = {
    "first": "6ef0819028a95867dee62685212cd7028400bce5d62e1ee19691dc4e6556c6be",
    "second": "fb54de3b588f356fae739f8618353e8a993badefd9f113c6927bf6682fcb1302",
    "second_body_drift": "cdca6609a357923e16e16353b897c457b16acc27b1c3ab466885b689e9ec25fa",
    "annex": "b2c180430b5d8e41e26996cdf56cf13a3f97b877ced33b4634db5fb1db459565",
    "body_drift": "d1fdb29da8d41800ad2621aca0672797d2d3aad8d6ad5b22b2605d20003e65c4",
    "table": "83ef32f3091ebc05b975a1de0c70308720c628a5494bdd0ffb03ea054e09014b",
    "unknown_select": "5144450b0025678a741687aa7e0522922e159f11125b91aa4f65dae269bfd4e2",
    "payload_nul": "1b7163fdec1ce4b1e2b928a21fb9320027914e7c1f6ec25fda04d73e825caf04",
}
PRE_FIX_CHECKSUM = ADMITTED_BASELINES["first"]


def compose_one(seam: Any, payload: dict[str, Any]) -> Any:
    registry = build_tenant_migration_registry(
        {payload["migration_id"]: seam.typed_definition(payload)},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )
    (unit,) = registry.units
    return unit


# ---------------------------------------------------------------------------
# OWN-07. The control, and the reason the rest of this file is safe to land.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "expected"), sorted(ADMITTED_BASELINES.items()))
def test_own07_every_admitted_typed_checksum_is_unchanged_by_this_correction(
    fixture: str, expected: str, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """OWN-07, CONTROL. Green today, and must stay green after the fix.

    v17 pinned ONE fixture. Codex's v17 review, additional precision: that pins
    one checksum, not every admitted typed checksum, so a correction could move
    the others without any test noticing. All eight admitted fixtures are now
    carried, measured at `b5b7f12`.

    Owning the declaration must not change what a typed unit digests to. A tuple
    canonicalizes byte-identically to a list and a `MappingProxyType` is not
    JSON-serializable at all, so the expected outcome is either "identical" or
    "a loud TypeError" -- never a quietly different digest.

    If any of these reddens, the correction has moved ledger identity for real
    tenants. I stop and report rather than updating the literal.
    """

    assert compose_one(seam, typed_payloads[fixture]).checksum == expected


# ---------------------------------------------------------------------------
# OWN-01..02  The caller mutates what it still holds.
# ---------------------------------------------------------------------------


def test_own01_mutating_the_original_policy_does_not_move_the_checksum(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """OWN-01. The exact defect measured above."""

    payload = typed_payloads["first"]
    unit = compose_one(seam, payload)
    before = unit.checksum
    retained = copy.deepcopy(unit.declaration_payload)

    payload["policy"]["functions"][0]["comment"] = "mutated after composition"

    assert unit.checksum == before
    assert unit.checksum == PRE_FIX_CHECKSUM
    # Codex, v17 review, point 3: the checksum alone is too narrow. A digest can
    # be cached while the declaration underneath still leaks.
    assert unit.declaration_payload == retained


def test_own02_mutating_the_original_verification_does_not_move_the_checksum(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """OWN-02. The other declaration block, which shares the same defect."""

    payload = typed_payloads["first"]
    unit = compose_one(seam, payload)
    before = unit.checksum
    retained = copy.deepcopy(unit.declaration_payload)

    payload["verification"]["functions"][0]["config"].clear()
    payload["verification"]["functions"][0]["owner"] = "mutated_owner"

    assert unit.checksum == before
    assert unit.declaration_payload == retained


def test_own06_a_deeply_nested_mutation_does_not_move_the_checksum(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """OWN-06. Two levels down, inside a function's `inputs`.

    Separate from OWN-01 because a one-level-deep copy would pass OWN-01's
    sibling cases while still failing here. This is what distinguishes
    "recursively owned" from "copied a bit harder".
    """

    payload = typed_payloads["first"]
    unit = compose_one(seam, payload)
    before = unit.checksum
    retained = copy.deepcopy(unit.declaration_payload)

    inputs = payload["policy"]["functions"][0]["inputs"]
    if inputs:
        inputs[0]["type"] = "bigint"
    else:
        inputs.append({"name": "injected", "type": "bigint"})

    assert unit.checksum == before
    assert unit.declaration_payload == retained


# ---------------------------------------------------------------------------
# OWN-03  The caller mutates what composition HANDED BACK.
# ---------------------------------------------------------------------------


def test_own03_mutating_a_returned_payload_does_not_reach_the_snapshot(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """OWN-03. `PAYLOAD_LEAK` above: the returned payload aliases the internals.

    Compared against a SEPARATELY RETAINED original, not against the mutated
    object (Codex, correction-plan review, point 4). Asserting only
    `!= "mutated via payload"` would pass for a payload that had been corrupted
    in some OTHER way, and would pass vacuously if the value were merely absent.
    The whole payload must be equal to what it was before the edit.
    """

    unit = compose_one(seam, typed_payloads["first"])
    before = unit.checksum
    retained = copy.deepcopy(unit.declaration_payload)

    handed_out = unit.declaration_payload
    handed_out["policy"]["functions"][0]["comment"] = "mutated via payload"
    handed_out["policy"]["functions"][0]["acl"].append({"grantee": "x", "privileges": []})
    # Codex, v17 review, point 3: the verification block is handed out too and
    # v17 only exercised the policy branch.
    handed_out["verification"]["functions"][0]["owner"] = "mutated via payload"
    handed_out["verification"]["kind"] = "mutated via payload"

    assert unit.declaration_payload == retained
    assert unit.checksum == before


# ---------------------------------------------------------------------------
# OWN-04  Mutation through the unit's own fields fails closed.
# ---------------------------------------------------------------------------


def test_own04_the_stored_declaration_refuses_mutation(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """OWN-04. Not merely "the caller cannot reach it" -- it cannot be mutated.

    Deep-copying alone would leave an internally mutable structure that happens
    to be unreachable. Owner decision A3 is copy AND freeze, and this is the
    half that a deep-copy-only fix would not satisfy.
    """

    unit = compose_one(seam, typed_payloads["first"])

    with pytest.raises((TypeError, AttributeError)):
        unit.policy["functions"][0]["comment"] = "mutated through the unit"

    with pytest.raises((TypeError, AttributeError)):
        unit.policy_verification["functions"].append({})


# ---------------------------------------------------------------------------
# OWN-05  Across an await, which is where the runner actually lives.
# ---------------------------------------------------------------------------


async def test_own05_a_mutation_inside_the_real_runner_does_not_move_the_recorded_identity(
    seam: Any, harness: Any, typed_driver: Any, call_spy: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """OWN-05. Rewritten after Codex's correction-plan review, point 4.

    The first version read the property twice around `asyncio.sleep(0)`. That is
    a scheduling example, not an integration test -- it would pass against a
    runner that never existed. This drives the REAL runner and lands the mutation
    inside it, using the harness hook to fire DURING THE LEDGER READ, BEFORE
    CLASSIFICATION COMPLETES.

    That timing wording is a correction Codex had to make twice. My v17 handoff
    said "after pass-1 classification"; the v18 handoff claimed the wording was
    fixed when it had not been, and Codex caught the source still saying it. The
    ledger-read await PRECEDES classification -- the read's result is what pass 1
    classifies on -- so the hook fires during the read, not after the classifying.

    The oracle is the identity actually WRITTEN, read out of the ledger write's
    parameters -- not a property re-read. That is the harm the finding names: the
    declaration that was classified is not the one recorded.

    MEASURED LEDGER SHAPE, so the oracle is not guessed at: the `running` INSERT
    carries `(tenant, migration_id, checksum)`; the `applied` UPDATE carries only
    `(tenant, migration_id)`. The checksum therefore reaches the ledger exactly
    once, at the INSERT, and that is the value this pins.

    PRE-FIX SYMPTOM: today this does not merely record the wrong digest -- the run
    REFUSES. The mutation lands during the ledger read, so pass 2's checker sees a
    declaration the caller changed after it was read for classification and
    rejects it, and the whole tenant migration fails. A spurious refusal and a
    wrong recorded identity are the same defect seen from two sides: the
    declaration in hand stopped being the declaration that was classified. After
    the correction the run completes and writes the original identity, which is
    what this asserts.
    """

    payload = typed_payloads["first"]
    migration_id = payload["migration_id"]
    registry = build_tenant_migration_registry(
        {migration_id: seam.typed_definition(payload)},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )
    (unit,) = registry.units

    # Independent oracles, captured BEFORE the mutation: the identity, the
    # declaration, and the bytes the frozen CP1 checker binds for it. Held by
    # value, so nothing here can drift with the payload.
    classified = unit.checksum
    assert classified == PRE_FIX_CHECKSUM
    original_declaration = copy.deepcopy(unit.declaration_payload)
    original_sql = validate_function_installation(
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        schema_key=SCHEMA,
    ).sql_bytes

    runner, (connection,) = typed_driver(
        registry,
        (*role_answers(harness), harness.ledger_absent()),
        manifest=manifest_declaring(OWNER),
        names=("work",),
    )

    checker = call_spy()
    owner_object, name = seam.checker_seam()
    monkeypatch.setattr(
        owner_object, name, checker.wrap(getattr(owner_object, name))
    )

    def mutate_mid_flight() -> None:
        payload["policy"]["functions"][0]["comment"] = "mutated inside the runner"
        payload["verification"]["functions"][0]["owner"] = "mutated_owner"

    hook = typed_recording.Hook(
        markers=("from shared.schema_migrations",), action=mutate_mid_flight
    )
    connection.hooks.append(hook)

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    # The mutation must actually have happened, or every assertion below is vacuous.
    assert hook.fired, "the hook never fired; nothing was mutated and this proves nothing"
    assert payload["policy"]["functions"][0]["comment"] == "mutated inside the runner"

    # The run SUCCEEDS. Ownership means a caller's later edit is simply irrelevant.
    assert [outcome.applied for outcome in outcomes] == [True]

    # EXACTLY the intended running INSERT, with its COMPLETE parameters. v17
    # searched every write for any 64-character string, which would have been
    # satisfied by a checksum appearing anywhere for any reason.
    inserts = [
        entry
        for entry in connection.statements
        if entry.fingerprint.startswith("insert into shared.schema_migrations")
    ]
    assert len(inserts) == 1, f"expected one running INSERT, saw {len(inserts)}"
    assert tuple(inserts[0].params or ()) == (TENANT, migration_id, classified)

    # The declaration the checker was given, and the bytes actually executed,
    # both still correspond to the ORIGINAL. A cached checksum sitting on top of
    # a leaking declaration would satisfy the INSERT assertion alone and fail here.
    assert checker.calls, "the checker was never called through the seam"
    checked = json.loads(checker.calls[0].args[0].decode("utf-8"))
    assert checked == original_declaration, (
        "the checker was handed a declaration the caller changed during the ledger read"
    )
    assert original_sql in typed_recording.executed_bytes(connection), (
        "the bytes executed are not the bytes bound from the original declaration"
    )


# ---------------------------------------------------------------------------
# Independence control.
# ---------------------------------------------------------------------------


def test_own08_two_units_from_one_payload_do_not_share_a_snapshot(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """OWN-08. Each unit hands out its OWN fresh, mutable payload view.

    Revised after Codex's v17 review: immutable internal interning is not
    inherently unsafe, and v17's wording banned it. What matters is narrower --
    every `declaration_payload` read must return a fresh MUTABLE structure, so
    one caller's edit to what it was handed cannot reach another unit or another
    read. Sharing a frozen internal snapshot between units with equal
    declarations would be fine; sharing a mutable one would not.
    """

    payload = typed_payloads["first"]
    first = compose_one(seam, payload)
    second = compose_one(seam, copy.deepcopy(payload))

    assert first.checksum == second.checksum
    assert first.declaration_payload is not second.declaration_payload
    assert (
        first.declaration_payload["policy"]["functions"]
        is not second.declaration_payload["policy"]["functions"]
    )
