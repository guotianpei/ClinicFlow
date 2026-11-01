"""B4 resolve rows on PostgreSQL 17 (package v2 + v2r1, Claude/Codex aligned
2026-11-01; owner O-1 2026-11-01: all active same-clinic follow-up viewers may read
notes through the separate m05.followup.note.read capability).

Service rows run through the composed runtime, the real M01 gateway and the real M02
EventFoundation (producer m05_followup); SQL rows call the definer functions as the
runtime role. The oracle is `service_support.full_state`: every directory, M05 and M02
event/link/outbox row.

Map: MA-01/03/03s/07r/OR, RP-1..13, KP-1, MR-08, MR-10a..n, MR-14, RB-1..12,
NR-1..9, SAN-1, HN-1 (history note_id), UQ-1, ENV-1, X-R1..3, G-B4.

RED TRANCHE: resolve/read_note raise NotImplementedError; the resolution contract is
inactive (activation, resolver (a), the t011 delta and the grants come with the
implementation), so every row fails in its own body.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import m05_support as m05
import psycopg
import pytest
import service_support as sv
import staff_support as st
from psycopg import sql
from test_m05_create_postgres import _ready
from test_m05_service_postgres import (
    _aitem,
    _faulting_pool,
    _post_body_fault,
    _staff,
)

pytestmark = pytest.mark.postgres

CANARY = "CANARY-b4-9c1d Mrs Q Example 555-0199 DOB 01/02/1950"
S1 = UUID(m05.S1)


def _svc() -> Any:
    import haloflow.m05.service as service

    return service


def _obs() -> Any:
    import haloflow.m02.observation as observation

    return observation


def _resolve(item: UUID, version: int = 1, kind: str = "resolve", outcome: str = "rescheduled",
             note: str | None = None, key: UUID | None = None) -> Any:
    return _svc().ResolveAction(item_id=item, client_action_id=key or uuid4(),
                                expected_version=version, type=kind, outcome_code=outcome,
                                note=note)


def _service(rt: Any, keys: Any, rec: Any = None) -> Any:
    gateway = rec or rt.gateway
    foundation = _obs().EventFoundation(gateway, producer_service=sv.PRODUCER)
    return _svc().M05Service(gateway, foundation=foundation, keys=keys)


async def _claimed(install: st.StaffInstall, actor: str = m05.S1) -> UUID:
    """An item assigned to `actor` (version 2), via the accepted SQL claim."""

    from test_m05_actions_postgres import _cmd

    item = await _aitem(install)
    await asyncio.to_thread(st.call, install, "m05_apply_action", _cmd(str(item), 1, "claim",
                                                                         actor=actor))
    return item


def _rows(install: st.StaffInstall, table: str, item: UUID) -> list[Any]:
    return st.admin_rows(install, sql.SQL(
        "SELECT to_jsonb(t) FROM {}.{} t WHERE t.item_id = %s").format(
        sql.Identifier(install.schema), sql.Identifier(table)), (str(item),))


def _events_for(install: st.StaffInstall, item: UUID) -> list[dict[str, Any]]:
    rows = st.admin_rows(install, sql.SQL(
        "SELECT to_jsonb(e) FROM {}.patient_events e JOIN {}.patient_event_links l"
        " ON l.event_id = e.event_id WHERE l.link_id = %s AND e.event_type = %s").format(
        sql.Identifier(install.schema), sql.Identifier(install.schema)),
        (str(item), sv.RESOLUTION_TYPE))
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# MA rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start", ["open_assigned", "in_progress"])
async def test_ma01_assignee_resolves_own_item(staff_install, start):
    from test_m05_actions_postgres import _cmd

    item = await _claimed(staff_install)
    version = 2
    if start == "in_progress":
        await asyncio.to_thread(st.call, staff_install, "m05_apply_action",
                                _cmd(str(item), 2, "start_progress"))
        version = 3
    keys = sv.TestNoteKeys()
    action = _resolve(item, version, note="Called; left voicemail.")
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        result = await _service(rt, keys, rec).act(ctx, action)
    assert result.outcome == "applied"
    assert dict(result.snapshot) == {
        "schema_version": 1, "item_id": str(item), "client_action_id": str(action.client_action_id),
        "action": "resolve", "version": version + 1, "state": "resolved",
        "assignee_id": m05.S1, "outcome_code": "rescheduled"}
    assert rec.calls == 1 and rec.statements == [sv.K_PREPARE, sv.K_APPEND, sv.K_APPLY]
    (event,) = _events_for(staff_install, item)
    assert (event["event_type"], event["contract_version"], event["event_level"],
            event["status"], event["operation_id"], event["occurred_at"],
            event["source_time_quality"], event["actor_id"]) == (
        sv.RESOLUTION_TYPE, 1, "observation", "completed", None, None, "absent", m05.S1)
    assert event["producer_dedupe_key"] == sv.resolution_dedupe_key(
        staff_install.tenant[0], str(item), str(action.client_action_id))
    assert event["metadata"] == {"outcome_code": "rescheduled", "assertion_kind": "staff",
                                 "item_kind": "reschedule_request"}
    history = sorted((r[0] for r in _rows(staff_install, "m05_followup_history", item)),
                     key=lambda h: h["seq"])
    last = history[-1]
    assert (last["action"], last["assertion_kind"], last["resolution_event_id"]) == (
        "resolve", "staff", event["event_id"])
    (action_row,) = [r[0] for r in _rows(staff_install, "m05_followup_actions", item)
                     if r[0]["client_action_id"] == str(action.client_action_id)]
    assert action_row["event_id"] == event["event_id"] and action_row["hmac_key_version"] == 1
    assert len(_rows(staff_install, "m05_followup_notes", item)) == 1


async def test_ma03_claim_and_resolve_unassigned_one_history_row(staff_install):
    item = await _aitem(staff_install)
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        result = await _service(rt, sv.TestNoteKeys()).act(
            ctx, _resolve(item, 1, "claim_and_resolve", "no_contact_no_answer"))
    assert (result.snapshot["assignee_id"], result.snapshot["state"]) == (m05.S1, "resolved")
    history = sorted((r[0] for r in _rows(staff_install, "m05_followup_history", item)),
                     key=lambda h: h["seq"])
    assert [h["action"] for h in history] == ["created", "claim_and_resolve"]
    assert history[1]["before"] == {"state": "open", "assignee_id": None, "outcome_code": None}
    assert history[1]["after"] == {"state": "resolved", "assignee_id": m05.S1,
                                   "outcome_code": "no_contact_no_answer"}


async def test_ma03_claim_and_resolve_on_assigned_item_refused(staff_install):
    item = await _claimed(staff_install, m05.S2)
    before = sv.full_state(staff_install)
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _service(rt, sv.TestNoteKeys()).act(ctx, _resolve(item, 2, "claim_and_resolve"))
    assert caught.value.code == "PRECONDITION_FAILED"
    assert sv.full_state(staff_install) == before


async def test_ma03s_others_item_needs_supervise(staff_install):
    item = await _claimed(staff_install, m05.S2)
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        before = sv.full_state(staff_install)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _service(rt, sv.TestNoteKeys()).act(ctx, _resolve(item, 2))
        assert caught.value.code == "NOT_AUTHORIZED"
        assert sv.full_state(staff_install) == before
        sup = await sv.context(rt, staff_install, m05.SUP, sv.SUPERVISOR_RESOLVE_CAPS)
        result = await _service(rt, sv.TestNoteKeys()).act(sup, _resolve(item, 2))
    assert (result.snapshot["assignee_id"], result.snapshot["state"]) == (m05.S2, "resolved")


async def test_ma07r_resolved_item_refused_no_reopen(staff_install):
    item = await _claimed(staff_install)
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        service = _service(rt, sv.TestNoteKeys())
        await service.act(ctx, _resolve(item, 2))
        before = sv.full_state(staff_install)
        for action in (_resolve(item, 3), _resolve(item, 3, "claim_and_resolve")):
            with pytest.raises(_svc().ActionRefused) as caught:
                await service.act(ctx, action)
            assert caught.value.code == "ITEM_RESOLVED"
    assert sv.full_state(staff_install) == before


async def test_ma_or_other_requires_note_in_sql_too(staff_install):
    """The service refuses `other` without a note before the gateway (RV-1); the SQL
    refuses it independently: NOTE_REQUIRED."""

    item = await _claimed(staff_install)
    command = {
        "schema_version": 1, "item_id": str(item), "client_action_id": str(uuid4()),
        "expected_version": 2,
        "action": {"type": "resolve", "outcome_code": "other", "note": None,
                   "payload": {"event_id": str(uuid4()), "note_row": None}},
        "permissions": m05.STAFF_PERMISSIONS,
        "attribution": st.attribution(m05.S1),
    }
    st.expect_code("NOTE_REQUIRED", lambda: st.call(staff_install, "m05_apply_action", command))


# ---------------------------------------------------------------------------
# RP replay protocol, KP-1
# ---------------------------------------------------------------------------


async def _resolve_once(install: st.StaffInstall, keys: Any, action: Any,
                        caps: frozenset[str] = sv.RESOLVE_CAPS) -> Any:
    async with sv.runtime(install) as rt:
        ctx = await sv.context(rt, install, m05.S1, caps)
        return await _service(rt, keys).act(ctx, action)


async def _retry(install: st.StaffInstall, keys: Any, action: Any) -> tuple[Any, sv.Recorder]:
    async with sv.runtime(install) as rt:
        rec = sv.Recorder(rt.gateway)
        ctx = await sv.context(rt, install, m05.S1, sv.RESOLVE_CAPS)
        try:
            return await _service(rt, keys, rec).act(ctx, action), rec
        except Exception as error:  # noqa: BLE001 - returned for assertions
            return error, rec


@pytest.mark.parametrize("note", [None, "Called; left voicemail."], ids=["note-less", "note"])
async def test_rp1_rp2_exact_retry_replays(staff_install, note):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    action = _resolve(item, 2, note=note)
    first = await _resolve_once(staff_install, keys, action)
    before = sv.full_state(staff_install)
    retry, rec = await _retry(staff_install, keys, action)
    assert (retry.outcome, dict(retry.snapshot)) == ("replayed", dict(first.snapshot))
    assert sv.full_state(staff_install) == before
    expected = ([sv.K_PREPARE, sv.K_APPLY] if note is None
                else [sv.K_PREPARE, sv.K_PREPARE, sv.K_APPLY])
    assert rec.statements == expected  # no append on replay
    assert ("current_encryption", None) not in keys.uses[-3:]


@pytest.mark.parametrize("change", ["text", "added", "removed", "outcome", "type"])
async def test_rp3_rp5_changed_retry_conflicts(staff_install, change):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    key = uuid4()
    base_note = None if change == "added" else "Original note."
    await _resolve_once(staff_install, keys, _resolve(item, 2, note=base_note, key=key))
    variant = {
        "text": _resolve(item, 2, note="Different note.", key=key),
        "added": _resolve(item, 2, note="Now with a note.", key=key),
        "removed": _resolve(item, 2, note=None, key=key),
        "outcome": _resolve(item, 2, outcome="reached_no_change", note=base_note, key=key),
        "type": _resolve(item, 2, kind="claim_and_resolve", note=base_note, key=key),
    }[change]
    before = sv.full_state(staff_install)
    retry, _ = await _retry(staff_install, keys, variant)
    assert isinstance(retry, _svc().ActionRefused) and retry.code == "ACTION_KEY_CONFLICT"
    assert sv.full_state(staff_install) == before


async def test_rp6_rotated_key_retained_replays_with_stored_version(staff_install):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    action = _resolve(item, 2, note="Rotation case.")
    first = await _resolve_once(staff_install, keys, action)
    keys.rotate()
    keys.uses.clear()
    retry, _ = await _retry(staff_install, keys, action)
    assert (retry.outcome, dict(retry.snapshot)) == ("replayed", dict(first.snapshot))
    assert ("hmac_key", 1) in keys.uses and ("current_hmac", None) not in keys.uses


async def test_rp7_retired_version_is_unverifiable(staff_install):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    action = _resolve(item, 2, note="Retired case.")
    await _resolve_once(staff_install, keys, action)
    keys.rotate()
    keys.retire(1)
    before = sv.full_state(staff_install)
    retry, rec = await _retry(staff_install, keys, action)
    assert isinstance(retry, _svc().ActionRefused) and retry.code == "REPLAY_UNVERIFIABLE"
    assert sv.K_APPEND not in rec.statements and sv.K_APPLY not in rec.statements
    assert sv.full_state(staff_install) == before


async def test_rp12_historical_replay_with_current_keys_unavailable(staff_install):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    action = _resolve(item, 2, note="Historical case.")
    first = await _resolve_once(staff_install, keys, action)
    keys.current_unavailable = True
    retry, _ = await _retry(staff_install, keys, action)
    assert (retry.outcome, dict(retry.snapshot)) == ("replayed", dict(first.snapshot))


async def test_kp1_note_less_resolve_touches_no_key(staff_install):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    keys.all_unavailable = True
    result = await _resolve_once(staff_install, keys, _resolve(item, 2))
    assert result.outcome == "applied" and keys.uses == []


async def test_kp2_new_note_with_current_key_unavailable_fails_before_writes(staff_install):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    keys.current_unavailable = True
    before = sv.full_state(staff_install)
    with pytest.raises(_svc().ActionFailed) as caught:
        await _resolve_once(staff_install, keys, _resolve(item, 2, note="No key."))
    assert caught.value.reason_code == "KEY_UNAVAILABLE"
    assert sv.full_state(staff_install) == before


PREPARE2_CORRUPT = {
    "new": {"schema_version": 1, "decision": "NEW"},
    "rechallenge": {"schema_version": 1, "decision": "REPLAY_CHALLENGE", "hmac_key_version": 1},
    "version-mismatch": {"schema_version": 1, "decision": "REPLAY_CHALLENGE",
                         "hmac_key_version": 2},
    "malformed-version": {"schema_version": 1, "decision": "REPLAY_CHALLENGE",
                          "hmac_key_version": True},
    "unbound-snapshot": "UNBOUND",
}


@pytest.mark.parametrize("case", sorted(PREPARE2_CORRUPT))
async def test_rp13_prepare2_only_bound_replay_or_known_refusal(staff_install, case):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    action = _resolve(item, 2, note="Challenge case.")
    first = await _resolve_once(staff_install, keys, action)
    row = PREPARE2_CORRUPT[case]
    if row == "UNBOUND":
        row = {"schema_version": 1, "decision": "REPLAY",
               "snapshot": {**dict(first.snapshot), "item_id": str(uuid4())}}
    before = sv.full_state(staff_install)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        rec.substitute_nth[(sv.K_PREPARE, 2)] = [(row,)]
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        with pytest.raises(_svc().ActionFailed) as caught:
            await _service(rt, keys, rec).act(ctx, action)
    assert caught.value.reason_code == "ACTION_RESULT_INVALID"
    assert sv.K_APPEND not in rec.statements and sv.K_APPLY not in rec.statements
    assert sv.full_state(staff_install) == before


@pytest.mark.parametrize("value", [0, -1, True, "1", None, 1.0])
async def test_rp13_prepare1_challenge_version_exact(staff_install, value):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    action = _resolve(item, 2, note="Exact version.")
    await _resolve_once(staff_install, keys, action)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        rec.substitute_nth[(sv.K_PREPARE, 1)] = [(
            {"schema_version": 1, "decision": "REPLAY_CHALLENGE", "hmac_key_version": value},)]
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        with pytest.raises(_svc().ActionFailed) as caught:
            await _service(rt, keys, rec).act(ctx, action)
    assert caught.value.reason_code == "ACTION_RESULT_INVALID"
    assert rec.statements == [sv.K_PREPARE]


# ---------------------------------------------------------------------------
# MR-08 orphan, MR-14 route, MR-10 event binding (direct SQL apply)
# ---------------------------------------------------------------------------


async def _append_resolution(install: st.StaffInstall, item: UUID, key: UUID, *,
                             actor: str = m05.S1, **overrides: Any) -> UUID:
    """Append a resolution observation through the real EventFoundation (fixture)."""

    view = await asyncio.to_thread(st.call, install, "m05_get_item", str(item))
    obs = _obs()
    values: dict[str, Any] = {
        "event_id": uuid4(), "event_type": sv.RESOLUTION_TYPE, "contract_version": 1,
        "correlation_id": uuid4(),
        "producer_dedupe_key": sv.resolution_dedupe_key(install.tenant[0], str(item), str(key)),
        "links": (obs.Link("m05_followup_item_v1", item, "case"),),
        "metadata": {"outcome_code": "rescheduled", "assertion_kind": "staff",
                     "item_kind": view["item_kind"]},
        "patient_id": UUID(view["patient_id"]),
        "causation_event_id": UUID(view["source_event_id"]),
        "occurred_at": None, "source_time_quality": "absent",
    }
    values.update(overrides)
    observation = obs.Observation(**values)
    async with sv.runtime(install) as rt:
        ctx = await sv.context(rt, install, actor, frozenset({sv.APPEND}))
        foundation = obs.EventFoundation(rt.gateway, producer_service=sv.PRODUCER)

        async def body(binding: Any) -> Any:
            return await foundation.append_observation_in(binding, observation)

        await foundation.run_in_transaction(ctx, body)
    return observation.event_id  # type: ignore[no-any-return]


async def test_mr08_orphan_resolution_event_fails_closed(staff_install, caplog):
    item = await _claimed(staff_install)
    key = uuid4()
    await _append_resolution(staff_install, item, key)
    before = sv.full_state(staff_install)
    with pytest.raises(_svc().ActionFailed) as caught:
        await _resolve_once(staff_install, sv.TestNoteKeys(),
                            _resolve(item, 2, key=key, note=CANARY))
    assert caught.value.reason_code == "ORPHAN_RESOLUTION_EVENT"
    assert sv.full_state(staff_install) == before
    assert CANARY not in caplog.text


async def test_mr14_resolve_without_append_capability_route_denied(staff_install):
    item = await _claimed(staff_install)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        keys = sv.TestNoteKeys()
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        with pytest.raises(_svc().RouteDenied):
            await _service(rt, keys, rec).act(ctx, _resolve(item, 2, note="x"))
    assert rec.calls == 0 and keys.uses == []


def _apply_command(item: UUID, key: UUID, event_id: UUID, version: int = 2,
                   outcome: str = "rescheduled") -> dict[str, Any]:
    return {
        "schema_version": 1, "item_id": str(item), "client_action_id": str(key),
        "expected_version": version,
        "action": {"type": "resolve", "outcome_code": outcome, "note": None,
                   "payload": {"event_id": str(event_id), "note_row": None}},
        "permissions": m05.STAFF_PERMISSIONS,
        "attribution": st.attribution(m05.S1),
    }


MR10 = {
    "unknown-event": None,
    "forged-actor": {"actor": m05.S2},
    "forged-outcome": {"metadata_outcome": "cancelled_in_ehr"},
    "forged-item-kind": {"metadata_item_kind": "cancel_request"},
    "wrong-causation": {"causation_event_id": "RANDOM"},
    "wrong-dedupe": {"producer_dedupe_key": "m05r1:" + "0" * 64},
    "occurred-at-set": {"occurred_at": "NOW", "source_time_quality": "trusted"},
    "operation-id-set": {"operation_id": "RANDOM"},
}


@pytest.mark.parametrize("case", sorted(MR10))
async def test_mr10_direct_apply_event_binding(staff_install, case):
    from datetime import UTC, datetime

    item = await _claimed(staff_install)
    key = uuid4()
    spec = MR10[case]
    if spec is None:
        event_id = uuid4()
    else:
        overrides: dict[str, Any] = {}
        view = await asyncio.to_thread(st.call, staff_install, "m05_get_item", str(item))
        meta = {"outcome_code": "rescheduled", "assertion_kind": "staff",
                "item_kind": view["item_kind"]}
        for field, value in spec.items():
            if field == "metadata_outcome":
                meta["outcome_code"] = value
            elif field == "metadata_item_kind":
                meta["item_kind"] = value
            elif field == "actor":
                continue
            elif value == "RANDOM":
                overrides[field] = uuid4()
            elif value == "NOW":
                overrides[field] = datetime.now(UTC)
            else:
                overrides[field] = value
        event_id = await _append_resolution(staff_install, item, key,
                                            actor=spec.get("actor", m05.S1), metadata=meta,
                                            **overrides)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   _apply_command(item, key, event_id)))
    assert sv.full_state(staff_install) == before


async def test_mr10_event_already_referenced_is_reused(staff_install):
    first_item = await _claimed(staff_install)
    result = await _resolve_once(staff_install, sv.TestNoteKeys(), _resolve(first_item, 2))
    assert result.outcome == "applied"
    (event,) = _events_for(staff_install, first_item)
    second = await _claimed(staff_install)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_REUSED",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   _apply_command(second, uuid4(), UUID(event["event_id"]))))
    assert sv.full_state(staff_install) == before


async def test_mr10_other_items_event_refused(staff_install):
    item_a, item_b = await _claimed(staff_install), await _claimed(staff_install)
    key = uuid4()
    event_id = await _append_resolution(staff_install, item_a, key)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   _apply_command(item_b, key, event_id)))
    assert sv.full_state(staff_install) == before


async def test_mr10_other_tenant_event_identical_to_unknown(staff_install, staff_foreign_install):
    foreign_item = await _claimed(staff_foreign_install)
    key = uuid4()
    foreign_event = await _append_resolution(staff_foreign_install, foreign_item, key)
    item = await _claimed(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   _apply_command(item, key, foreign_event)))


async def test_mr10_wrong_event_type_refused(staff_install):
    from test_m05_create_postgres import _source

    item = await _claimed(staff_install)
    other = await asyncio.to_thread(_source, staff_install)  # an M02 test observation
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   _apply_command(item, uuid4(), UUID(other))))


# ---------------------------------------------------------------------------
# UQ-1, ENV-1, X-R1..3, G-B4
# ---------------------------------------------------------------------------


async def test_uq1_only_the_named_constraint_maps_to_reused(staff_install):
    """A unique violation from any OTHER constraint is re-raised unchanged and the
    service maps it to ACTION_UNAVAILABLE, never RESOLUTION_EVENT_REUSED (v2r1 A2)."""

    item = await _claimed(staff_install)  # history already holds a 'claim' row
    schema = sql.Identifier(staff_install.schema)
    await asyncio.to_thread(st.admin_rows, staff_install, sql.SQL(
        "CREATE UNIQUE INDEX uq1_fault ON {}.m05_followup_history (item_id)"
        " WHERE action IN ('claim', 'resolve')").format(schema))
    try:
        before = sv.full_state(staff_install)
        with pytest.raises(_svc().ActionFailed) as caught:
            await _resolve_once(staff_install, sv.TestNoteKeys(), _resolve(item, 2))
        assert caught.value.reason_code == "ACTION_UNAVAILABLE"
        assert sv.full_state(staff_install) == before
    finally:
        await asyncio.to_thread(st.admin_rows, staff_install, sql.SQL(
            "DROP INDEX IF EXISTS {}.uq1_fault").format(schema))


def _envelope(install: st.StaffInstall, event_id: UUID) -> dict[str, Any]:
    with psycopg.connect(install.ids.admin) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
        row = conn.execute(sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(
            sql.Identifier(install.schema)), (str(event_id),)).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


@pytest.mark.parametrize("occurred", ["null", "set"])
async def test_env1_envelope_carries_occurred_at_and_time_quality(staff_install, occurred):
    from datetime import UTC, datetime

    item = await _claimed(staff_install)
    overrides: dict[str, Any] = {}
    if occurred == "set":
        overrides = {"occurred_at": datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
                     "source_time_quality": "trusted"}
    event_id = await _append_resolution(staff_install, item, uuid4(), **overrides)
    envelope = await asyncio.to_thread(_envelope, staff_install, event_id)
    assert "occurred_at" in envelope and "source_time_quality" in envelope
    if occurred == "null":
        assert (envelope["occurred_at"], envelope["source_time_quality"]) == (None, "absent")
    else:
        assert envelope["occurred_at"] == "2026-11-01T12:00:00.000000Z"
        assert envelope["source_time_quality"] == "trusted"


@pytest.mark.parametrize("target", ["missing", "wrong-patient"])
async def test_xr_resolver_a_rejects_bad_targets(staff_install, target):
    item = await _claimed(staff_install)
    link_item = uuid4() if target == "missing" else item
    overrides: dict[str, Any] = {"links": (_obs().Link("m05_followup_item_v1", link_item,
                                                       "case"),)}
    if target == "wrong-patient":
        overrides["patient_id"] = UUID("00000000-0000-4000-8000-0000000b2f02")
    with pytest.raises(_obs().AppendFailed) as caught:
        await _append_resolution(staff_install, item, uuid4(), **overrides)
    assert caught.value.reason_code == "LINK_INVALID"


async def test_xr3_resolver_a_rejects_other_tenant_item(staff_install, staff_foreign_install):
    foreign_item = await _claimed(staff_foreign_install)
    item = await _claimed(staff_install)
    with pytest.raises(_obs().AppendFailed) as caught:
        await _append_resolution(staff_install, item, uuid4(), links=(
            _obs().Link("m05_followup_item_v1", foreign_item, "case"),))
    assert caught.value.reason_code == "LINK_INVALID"


def test_gb4_exact_added_grants(staff_install):
    _ready(staff_install)
    schema = staff_install.schema

    def col(table: str, column: str, privilege: str, role: str = m05.M05_OWNER) -> bool:
        return st.admin_rows(staff_install,
                             "SELECT pg_catalog.has_column_privilege(%s, %s, %s, %s)",
                             (role, f"{schema}.{table}", column, privilege))[0][0]

    assert col("m05_followup_items", "outcome_code", "UPDATE")
    assert col("m05_followup_history", "resolution_event_id", "SELECT")
    assert col("m05_followup_actions", "event_id", "SELECT")
    assert col("m05_followup_actions", "hmac_key_version", "SELECT")
    for column in ("note_id", "item_id", "client_action_id", "nonce", "ciphertext",
                   "enc_key_version", "created_at"):
        assert col("m05_followup_notes", column, "INSERT"), column
        assert col("m05_followup_notes", column, "SELECT"), column
    for column in ("item_id", "patient_id"):
        assert col("m05_followup_items", column, "SELECT", "haloflow_m02_append_owner"), column
    assert not col("m05_followup_items", "state", "SELECT", "haloflow_m02_append_owner")
    resolver = st.admin_rows(staff_install, sql.SQL(
        "SELECT resolver_kind FROM {}.m02_link_resolvers WHERE link_type = 'm05_followup_item_v1'"
    ).format(sql.Identifier(schema)))
    assert resolver == [("module_owned",)]


# ---------------------------------------------------------------------------
# RB rollback / recovery (full-state oracle)
# ---------------------------------------------------------------------------


class _Boom(Exception):
    pass


def _boom() -> None:
    raise _Boom("fault")


async def _resolve_with(install: st.StaffInstall, rec_hooks: dict[str, Any],
                        action: Any) -> tuple[Any, sv.Recorder]:
    async with sv.runtime(install) as rt:
        rec = sv.Recorder(rt.gateway, **rec_hooks)
        ctx = await sv.context(rt, install, m05.S1, sv.RESOLVE_CAPS)
        try:
            return await _service(rt, sv.TestNoteKeys(), rec).act(ctx, action), rec
        except BaseException as error:  # noqa: BLE001 - CancelledError included
            return error, rec


@pytest.mark.parametrize("point", ["after-append", "after-apply"])
async def test_rb1_rb9_fault_before_marker_rolls_back_everything(staff_install, point):
    item = await _claimed(staff_install)
    before = sv.full_state(staff_install)
    hooks = {"after": {sv.K_APPEND if point == "after-append" else sv.K_APPLY: _boom}}
    error, rec = await _resolve_with(staff_install, hooks, _resolve(item, 2, note="Fault."))
    assert isinstance(error, _Boom)  # a non-database exception propagates unchanged
    assert sv.K_APPEND in rec.statements
    assert sv.full_state(staff_install) == before


@pytest.mark.parametrize("table", ["m05_followup_notes", "m05_followup_history",
                                   "m05_followup_actions", "event_handoff_outbox"])
async def test_rb3_rb7_write_fault_rolls_back_everything(staff_install, table):
    item = await _claimed(staff_install)
    schema = sql.Identifier(staff_install.schema)
    st.admin_rows(staff_install, sql.SQL(
        "CREATE FUNCTION {}.b4_fault() RETURNS trigger LANGUAGE plpgsql AS"
        " $f$BEGIN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'B4_FAULT'; END$f$"
    ).format(schema))
    try:
        st.admin_rows(staff_install, sql.SQL(
            "CREATE TRIGGER b4_fault BEFORE INSERT ON {}.{} FOR EACH ROW"
            " EXECUTE FUNCTION {}.b4_fault()").format(schema, sql.Identifier(table), schema))
        before = sv.full_state(staff_install)
        error, _ = await _resolve_with(staff_install, {}, _resolve(item, 2, note="Fault."))
        assert isinstance(error, (_svc().ActionFailed, _obs().AppendFailed)), error
        assert sv.full_state(staff_install) == before
    finally:
        st.admin_rows(staff_install, sql.SQL("DROP TRIGGER IF EXISTS b4_fault ON {}.{}").format(
            schema, sql.Identifier(table)))
        st.admin_rows(staff_install, sql.SQL("DROP FUNCTION IF EXISTS {}.b4_fault()").format(
            schema))


async def test_rb8_invalid_apply_output_rolls_back(staff_install):
    item = await _claimed(staff_install)
    before = sv.full_state(staff_install)
    hooks = {"substitute": {sv.K_APPLY: [({"schema_version": 1, "outcome": "applied",
                                           "snapshot": {}},)]}}
    error, _ = await _resolve_with(staff_install, hooks, _resolve(item, 2, note="Bad output."))
    assert isinstance(error, _svc().ActionFailed) and error.reason_code == "ACTION_RESULT_INVALID"
    assert sv.full_state(staff_install) == before


async def test_rb12_cancellation_before_marker(staff_install):
    item = await _claimed(staff_install)
    before = sv.full_state(staff_install)

    async def cancel() -> None:
        raise asyncio.CancelledError()

    error, _ = await _resolve_with(staff_install, {"before": {sv.K_APPLY: cancel}},
                                   _resolve(item, 2))
    assert isinstance(error, asyncio.CancelledError)
    assert sv.full_state(staff_install) == before


COMMIT_MODES = {"commit_then_raise": True, "commit_then_cancel": True, "raise_no_commit": False}


@pytest.mark.parametrize("mode", sorted(COMMIT_MODES))
async def test_rb10_unknown_commit_recovery_exactly_once(staff_install, mode):
    item = await _claimed(staff_install)
    keys = sv.TestNoteKeys()
    action = _resolve(item, 2, note="Commit fault.")
    before = sv.full_state(staff_install)
    fired: list[str] = []
    async with sv.runtime(staff_install, pool_class=_faulting_pool(mode, fired)) as rt:
        rec = sv.Recorder(rt.gateway)
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        service = _service(rt, keys, rec)
        with pytest.raises(_svc().ActionCommitUnknown) as caught:
            await service.act(ctx, action)
        assert fired == [mode]
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        original = rec.returned(sv.K_APPLY)[0][0][0]
        after_fault = sv.full_state(staff_install)
        retry = await service.act(ctx, action)
    events = _events_for(staff_install, item)
    assert len(events) == 1
    if COMMIT_MODES[mode]:
        assert (retry.outcome, dict(retry.snapshot)) == ("replayed", original["snapshot"])
        assert sv.full_state(staff_install) == after_fault
    else:
        assert after_fault == before and retry.outcome == "applied"
    assert len(_rows(staff_install, "m05_followup_notes", item)) == 1


async def test_rb11_post_body_fault_unknown_then_applies_once(staff_install, monkeypatch):
    item = await _claimed(staff_install)
    action = _resolve(item, 2)
    before = sv.full_state(staff_install)
    async with sv.runtime(staff_install) as rt:
        calls = _post_body_fault(monkeypatch, rt.gateway)
        ctx = await sv.context(rt, staff_install, m05.S1, sv.RESOLVE_CAPS)
        service = _service(rt, sv.TestNoteKeys())
        with pytest.raises(_svc().ActionCommitUnknown):
            await service.act(ctx, action)
        assert calls["n"] == 2 and sv.full_state(staff_install) == before
        monkeypatch.undo()
        assert (await service.act(ctx, action)).outcome == "applied"
    assert len(_events_for(staff_install, item)) == 1


# ---------------------------------------------------------------------------
# NR note read (owner O-1), HN-1, SAN-1
# ---------------------------------------------------------------------------


async def _resolved_with_note(install: st.StaffInstall, keys: Any, text: str = CANARY) -> UUID:
    item = await _claimed(install)
    await _resolve_once(install, keys, _resolve(item, 2, note=text))
    return item


def _note_id(install: st.StaffInstall, item: UUID) -> UUID:
    history = st.call(install, "m05_get_history", str(item))
    ids = [e["note_id"] for e in history["entries"] if e["note_id"] is not None]
    assert len(ids) == 1, history
    return UUID(ids[0])


async def test_nr1_ordinary_reader_reads_note(staff_install):
    keys = sv.TestNoteKeys()
    item = await _resolved_with_note(staff_install, keys, "Patient will call back.")
    note_id = await asyncio.to_thread(_note_id, staff_install, item)
    reader = await asyncio.to_thread(_staff, staff_install, access=("read",))  # no act
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        ctx = await sv.context(rt, staff_install, str(reader), frozenset({sv.NOTE_READ}))
        note = await _service(rt, keys, rec).read_note(ctx, note_id)
    assert note == {"note_id": str(note_id), "item_id": str(item),
                    "text": "Patient will call back."}
    assert rec.statements == [sv.K_READER, sv.K_NOTE]


@pytest.mark.parametrize("who", ["inactive", "no-view", "foreign-clinic", "workload"])
async def test_nr2_reader_without_standing_refused(staff_install, staff_foreign_install, who):
    keys = sv.TestNoteKeys()
    item = await _resolved_with_note(staff_install, keys)
    note_id = await asyncio.to_thread(_note_id, staff_install, item)
    if who == "inactive":
        actor, kind = str(await asyncio.to_thread(_staff, staff_install, active=False)), "actor"
    elif who == "no-view":
        actor, kind = str(await asyncio.to_thread(_staff, staff_install, access=("act",))), "actor"
    elif who == "foreign-clinic":
        await asyncio.to_thread(staff_foreign_install.require)
        actor, kind = str(await asyncio.to_thread(_staff, staff_foreign_install)), "actor"
    else:
        actor, kind = m05.S1, "workload"
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        ctx = await sv.context(rt, staff_install, actor, frozenset({sv.NOTE_READ}), kind=kind)
        with pytest.raises(_svc().ReadRefused) as caught:
            await _service(rt, keys, rec).read_note(ctx, note_id)
    assert caught.value.code == "NOT_AUTHORIZED"
    assert sv.K_NOTE not in rec.statements and keys.uses.count(("encryption_key", 1)) == 0


async def test_nr3_missing_capability_route_denied(staff_install):
    keys = sv.TestNoteKeys()
    item = await _resolved_with_note(staff_install, keys)
    note_id = await asyncio.to_thread(_note_id, staff_install, item)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        ctx = await sv.context(rt, staff_install, m05.S1, sv.READ_ONLY_CAPS)
        with pytest.raises(_svc().RouteDenied):
            await _service(rt, keys, rec).read_note(ctx, note_id)
    assert rec.calls == 0


async def test_nr4_unknown_note_is_item_not_found(staff_install):
    await asyncio.to_thread(_ready, staff_install)
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.NOTE_READ}))
        with pytest.raises(_svc().ReadRefused) as caught:
            await _service(rt, sv.TestNoteKeys()).read_note(ctx, uuid4())
    assert caught.value.code == "ITEM_NOT_FOUND"


@pytest.mark.parametrize("case", ["swapped-ciphertext", "key-unavailable", "extra-key",
                                  "short-nonce", "bool-version", "two-rows"])
async def test_nr5_envelope_and_integrity_failures(staff_install, case):
    keys = sv.TestNoteKeys()
    item_a = await _resolved_with_note(staff_install, keys, "Note A.")
    item_b = await _resolved_with_note(staff_install, keys, "Note B.")
    note_a = await asyncio.to_thread(_note_id, staff_install, item_a)
    note_b = await asyncio.to_thread(_note_id, staff_install, item_b)
    real_b = await asyncio.to_thread(st.call, staff_install, "m05_read_note", str(note_b))
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        if case == "key-unavailable":
            keys.retire(1)
        else:
            probe = sv.Recorder(rt.gateway)
            ctx0 = await sv.context(rt, staff_install, m05.S1, frozenset({sv.NOTE_READ}))
            await _service(rt, keys, probe).read_note(ctx0, note_a)
            real_a = probe.returned(sv.K_NOTE)[0][0][0]
            bad = {
                "swapped-ciphertext": {**real_a, "ciphertext": real_b["ciphertext"]},
                "extra-key": {**real_a, "x": 1},
                "short-nonce": {**real_a, "nonce": real_a["nonce"][:-2]},
                "bool-version": {**real_a, "enc_key_version": True},
                "two-rows": None,
            }[case]
            rec.substitute[sv.K_NOTE] = ([(real_a,), (real_a,)] if bad is None else [(bad,)])
        ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.NOTE_READ}))
        with pytest.raises(_svc().ReadFailed) as caught:
            await _service(rt, keys, rec).read_note(ctx, note_a)
    expected = {"swapped-ciphertext": "NOTE_INTEGRITY_FAILED",
                "key-unavailable": "NOTE_KEY_UNAVAILABLE"}.get(case, "READ_RESULT_INVALID")
    assert caught.value.reason_code == expected
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "Note A." not in str(caught.value)


async def test_hn1_history_note_id_on_every_entry(staff_install):
    keys = sv.TestNoteKeys()
    with_note = await _resolved_with_note(staff_install, keys, "With note.")
    without = await _claimed(staff_install)
    await _resolve_once(staff_install, keys, _resolve(without, 2))
    for item, has_note in ((with_note, True), (without, False)):
        history = await asyncio.to_thread(st.call, staff_install, "m05_get_history", str(item))
        assert all("note_id" in e for e in history["entries"])
        resolved = [e for e in history["entries"] if e["action"] == "resolve"]
        others = [e for e in history["entries"] if e["action"] != "resolve"]
        assert all(e["note_id"] is None for e in others)
        assert (resolved[0]["note_id"] is not None) is has_note


async def test_san1_canary_absent_everywhere(staff_install, caplog):
    keys = sv.TestNoteKeys()
    item = await _resolved_with_note(staff_install, keys, CANARY)
    everything = sv.full_state(staff_install)
    assert CANARY not in str(everything)
    assert CANARY.encode().hex() not in str(everything)
    history = await asyncio.to_thread(st.call, staff_install, "m05_get_history", str(item))
    assert CANARY not in str(history)
    assert CANARY not in caplog.text
    (note_row,) = [r[0] for r in _rows(staff_install, "m05_followup_notes", item)]
    assert set(note_row) >= {"nonce", "ciphertext", "enc_key_version"}
