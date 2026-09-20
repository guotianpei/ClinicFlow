"""CP2-1 correction -- ORDER: outcomes must come back in registry order.

NEW FILE, a clearly identified supplement. Every v14 file stays byte-identical.

Responds to the v15 review, finding 4. The whole-plan preflight split the single
loop into three passes and the outcome list was not split with it: skips are
appended during pass 1 (`runner.py:195`) and installs during pass 3
(`runner.py:238`). For a registry ordered `[A pending, B applied-equal]` the
caller receives `(B, A)`; the old single loop returned `(A, B)`.

Codex asked specifically for the ORDINARY-route regression, because TP-R8
preserves that behaviour and an existing CP1 consumer is what would notice it.
Both routes are covered.

THE ORACLE
----------
`ids_of(outcomes) == registry.migration_ids`. Not "A is first": comparing
against the registry's own order means a case cannot pass by accidentally
agreeing with a hard-coded list, and stays correct if a future registry orders
itself differently.

WHY THE LEDGER IS SCRIPTED PER READ
-----------------------------------
The recording harness matches an answer by the statement's FINGERPRINT, which is
its text. Every unit's ledger read has identical text, so two `ledger_row`
answers would overlap and `_answer` rejects overlapping markers by design --
correctly, since a silently-shared answer is how a test stops meaning anything.

`ScriptedLedger` therefore answers the ledger read by call order, but each
response is BOUND to the unit it was declared for: the script is a list of
`(migration_id, rows)`, and the helper asserts the read's own `(tenant,
migration_id)` parameters match. Answering by position alone would let the
helper silently serve the wrong unit and still produce a plausible outcome
tuple -- Codex's v17 review, point 4. Every case, not only the controls, calls
`assert_fully_consumed()`, so a case cannot pass while reading the ledger a
different number of times, or in a different order, than it declared.

Nothing frozen is modified: the override is a `monkeypatch` on the CP2-1
harness extension class and is undone at teardown.

Pre-change state: RED for the three mixed cases, GREEN for the two controls
and for ORDER-01b, the mirror the current code happens to get right.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import typed_recording

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

LEDGER_MARKER = "from shared.schema_migrations"

ABSENT: tuple[tuple[Any, ...], ...] = ()


def applied(checksum: str) -> tuple[tuple[Any, ...], ...]:
    return (("applied", checksum),)


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


def ordinary_registry(seam: Any, *ids: str) -> Any:
    """Distinct ordinary units, one per id, in the order given."""

    return build_tenant_migration_registry(
        {
            migration_id: seam.ordinary_definition(
                f"CREATE TABLE {{schema}}.cp2_order_probe_{index} (id int);"
            )
            for index, migration_id in enumerate(ids)
        },
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )


def typed_registry(seam: Any, *payloads: dict[str, Any]) -> Any:
    return build_tenant_migration_registry(
        {payload["migration_id"]: seam.typed_definition(payload) for payload in payloads},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )


def drive(typed_driver: Any, harness: Any, registry: Any, ledger: Any) -> tuple[Any, Any]:
    runner, (connection,) = typed_driver(
        registry,
        (*role_answers(harness), ledger),
        manifest=manifest_declaring(OWNER),
        names=("work",),
    )
    return runner, connection


class ScriptedLedger:
    """Answer the ledger read by call order, BOUND to the unit each read is for.

    Codex, v17 review, point 4: a helper that answers purely by call order can
    silently answer for the wrong unit, and only the uniform controls were
    checking the counter. So each scripted response is declared as
    `(migration_id, rows)` and the helper asserts the read's own parameters
    match the unit it was declared for. A drifting read order now fails here
    rather than producing a plausible-looking outcome tuple.

    `assert_fully_consumed()` is called by EVERY case, not just the controls.
    """

    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self._script = script
        self.reads: list[tuple[Any, ...]] = []

    def install(self, monkeypatch: Any) -> ScriptedLedger:
        original = typed_recording.recording.RecordingConnection._answer

        def answer(inner_self: Any, entry: Any) -> Any:
            if LEDGER_MARKER not in entry.fingerprint:
                return original(inner_self, entry)
            index = len(self.reads)
            if index >= len(self._script):
                raise AssertionError(
                    f"ledger read {index + 1} was not scripted; "
                    f"{len(self._script)} reads were declared"
                )
            expected_id, rows = self._script[index]
            params = tuple(entry.params or ())
            self.reads.append(params)
            assert params == (TENANT, expected_id), (
                f"ledger read {index + 1} was declared for {expected_id!r} but the "
                f"runner read {params!r} -- the script is answering the wrong unit"
            )
            return rows

        monkeypatch.setattr(
            typed_recording.TypedConnection, "_answer", answer, raising=False
        )
        return self

    def assert_fully_consumed(self) -> None:
        assert len(self.reads) == len(self._script), (
            f"{len(self._script)} ledger reads were scripted but "
            f"{len(self.reads)} happened"
        )
        assert self.reads == [
            (TENANT, migration_id) for migration_id, _ in self._script
        ]


def scripted_ledger(monkeypatch: Any, script: list[tuple[str, Any]]) -> ScriptedLedger:
    return ScriptedLedger(script).install(monkeypatch)


def ids_of(outcomes: Any) -> tuple[str, ...]:
    return tuple(outcome.migration_id for outcome in outcomes)


# ---------------------------------------------------------------------------
# Controls. Uniform plans, where pass order and registry order coincide. These
# pass today and must keep passing: they are what shows the mixed cases below
# fail because of ORDER and not because the runner or the scripting is broken.
# ---------------------------------------------------------------------------


async def test_order_control_all_pending_is_registry_order(
    seam: Any, harness: Any, typed_driver: Any, monkeypatch: Any
) -> None:
    registry = ordinary_registry(seam, "t001_test_cp2", "t002_test_cp2")
    ledger = scripted_ledger(
        monkeypatch, [("t001_test_cp2", ABSENT), ("t002_test_cp2", ABSENT)]
    )
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert ids_of(outcomes) == registry.migration_ids
    assert [outcome.applied for outcome in outcomes] == [True, True]
    ledger.assert_fully_consumed()


async def test_order_control_all_skipped_is_registry_order(
    seam: Any, harness: Any, typed_driver: Any, monkeypatch: Any
) -> None:
    registry = ordinary_registry(seam, "t001_test_cp2", "t002_test_cp2")
    first, second = registry.units
    ledger = scripted_ledger(monkeypatch, [
        ("t001_test_cp2", applied(first.checksum)),
        ("t002_test_cp2", applied(second.checksum)),
    ])
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert ids_of(outcomes) == registry.migration_ids
    assert [outcome.applied for outcome in outcomes] == [False, False]
    ledger.assert_fully_consumed()


# ---------------------------------------------------------------------------
# ORDER-01  The ordinary route. TP-R8 preserves this behaviour, so this is the
# case an existing CP1 consumer would notice.
# ---------------------------------------------------------------------------


async def test_order01_ordinary_pending_then_skipped_returns_registry_order(
    seam: Any, harness: Any, typed_driver: Any, monkeypatch: Any
) -> None:
    """ORDER-01. Registry `[A pending, B applied-equal]` must return `(A, B)`.

    Today it returns `(B, A)`: B's skip is appended in pass 1, A's install in
    pass 3.
    """

    registry = ordinary_registry(seam, "t001_test_cp2", "t002_test_cp2")
    _, second = registry.units
    ledger = scripted_ledger(monkeypatch, [
        ("t001_test_cp2", ABSENT),
        ("t002_test_cp2", applied(second.checksum)),
    ])
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert ids_of(outcomes) == registry.migration_ids
    assert [outcome.applied for outcome in outcomes] == [True, False]
    ledger.assert_fully_consumed()


async def test_order01b_ordinary_skipped_then_pending_returns_registry_order(
    seam: Any, harness: Any, typed_driver: Any, monkeypatch: Any
) -> None:
    """ORDER-01b. The mirror image, which the current code happens to get right.

    Kept deliberately: a "fix" that reversed the outcome list would pass ORDER-01
    and break this one. Together they pin the order rather than a direction.
    """

    registry = ordinary_registry(seam, "t001_test_cp2", "t002_test_cp2")
    first, _ = registry.units
    ledger = scripted_ledger(monkeypatch, [
        ("t001_test_cp2", applied(first.checksum)),
        ("t002_test_cp2", ABSENT),
    ])
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert ids_of(outcomes) == registry.migration_ids
    assert [outcome.applied for outcome in outcomes] == [False, True]
    ledger.assert_fully_consumed()


# ---------------------------------------------------------------------------
# ORDER-02  Three units, the MIDDLE one skipped. A fix that appended skips last,
# or sorted by applied-ness, would pass ORDER-01 and fail this.
# ---------------------------------------------------------------------------


async def test_order02_a_skipped_unit_in_the_middle_keeps_registry_order(
    seam: Any, harness: Any, typed_driver: Any, monkeypatch: Any
) -> None:
    registry = ordinary_registry(
        seam, "t001_test_cp2", "t002_test_cp2", "t003_test_cp2"
    )
    _, second, _ = registry.units
    ledger = scripted_ledger(monkeypatch, [
        ("t001_test_cp2", ABSENT),
        ("t002_test_cp2", applied(second.checksum)),
        ("t003_test_cp2", ABSENT),
    ])
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert ids_of(outcomes) == registry.migration_ids
    assert [outcome.applied for outcome in outcomes] == [True, False, True]
    ledger.assert_fully_consumed()


# ---------------------------------------------------------------------------
# ORDER-03  The typed route, same shape.
# ---------------------------------------------------------------------------


async def test_order03_typed_pending_then_skipped_returns_registry_order(
    seam: Any, harness: Any, typed_driver: Any, monkeypatch: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """ORDER-03. The typed route must agree with the ordinary one."""

    first_payload = typed_payloads["first"]
    second_payload = typed_payloads["second"]
    registry = typed_registry(seam, first_payload, second_payload)
    _, second = registry.units
    ledger = scripted_ledger(monkeypatch, [
        (first_payload["migration_id"], ABSENT),
        (second_payload["migration_id"], applied(second.checksum)),
    ])
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert ids_of(outcomes) == registry.migration_ids
    assert [outcome.applied for outcome in outcomes] == [True, False]
    ledger.assert_fully_consumed()
