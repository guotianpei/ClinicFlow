"""B4 resolve rows on PostgreSQL 17 (package v2 + v2r1, Claude/Codex aligned
2026-11-01; owner O-1 2026-11-01: all active same-clinic follow-up viewers may read
notes through the separate m05.followup.note.read capability).

Red review REVISE 4c5b82fb applied (tests only): R1 real-append fixtures report setup
refusals with their reason code; R3 every fault row proves its fault fired (the raw
error the statement raised, recorded before classification), an unfaulted control
proves each write is reached, the item UPDATE fault is added, resolver rejections
compare the full state, and RB-10/11 assert the exact committed delta bound to the
original event and snapshot; R4 a foreign note id read by an eligible local reader, an
authenticated non-normal plaintext, the envelope binding/size corpus and error-path
canaries (SAN-2). The MR-10 matrix, RA-R, G-B4, COMPAT-01 and MX-01 are in
test_m05_resolve_binding_postgres.py.

Service rows run through the composed runtime, the real M01 gateway and the real M02
EventFoundation (producer m05_followup); SQL rows call the definer functions as the
runtime role. The oracle is `service_support.full_state`.

Declared expectations (red gate request v2): an M02 append failure inside resolve
propagates as M02's own `AppendFailed(reason_code)` (no cause/context); an M05-side
database fault is `ActionFailed("ACTION_UNAVAILABLE")`.

RED: resolve/read_note raise NotImplementedError and the B4 SQL/units do not exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import traceback
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import m05_support as m05
import psycopg
import pytest
import resolve_support as rs
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

CANARY = rs.CANARY
_svc = rs.svc
_obs = rs.obs
_resolve = rs.resolve_action
_service = rs.service
_claimed = rs.claimed
_resolve_once = rs.resolve_once
_events_for = rs.events_for


def _rows(install: st.StaffInstall, table: str, item: UUID) -> list[tuple[dict[str, Any]]]:
    return [(r,) for r in rs.rows(install, table, item)]


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
# MR-08 orphan, MR-14 route, UQ-1, ENV-1, X-R1..3
# ---------------------------------------------------------------------------


async def test_mr08_orphan_resolution_event_fails_closed(staff_install, caplog):
    item = await _claimed(staff_install)
    key = uuid4()
    await rs.append_or_setup_fail(staff_install, item, key)
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
        error, rec = await _resolve_with(staff_install, {}, _resolve(item, 2))
        assert isinstance(error, _svc().ActionFailed), error
        assert error.reason_code == "ACTION_UNAVAILABLE"
        raw = [e for k, e in rec.errors if k == sv.K_APPLY]
        assert len(raw) == 1 and isinstance(raw[0], psycopg.Error), rec.errors
        assert raw[0].sqlstate == "23505" and raw[0].diag.constraint_name == "uq1_fault"
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
    event_id = await rs.append_or_setup_fail(staff_install, item, uuid4(), **overrides)
    envelope = await asyncio.to_thread(_envelope, staff_install, event_id)
    assert "occurred_at" in envelope and "source_time_quality" in envelope
    if occurred == "null":
        assert (envelope["occurred_at"], envelope["source_time_quality"]) == (None, "absent")
    else:
        assert envelope["occurred_at"] == "2026-11-01T12:00:00.000000Z"
        assert envelope["source_time_quality"] == "trusted"


async def _expect_link_invalid(install: st.StaffInstall, item: UUID, **overrides: Any) -> None:
    before = sv.full_state(install)
    with pytest.raises(_obs().AppendFailed) as caught:
        await rs.append_resolution(install, item, uuid4(), **overrides)
    assert caught.value.reason_code == "LINK_INVALID"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sv.full_state(install) == before


@pytest.mark.parametrize("target", ["missing", "wrong-patient"])
async def test_xr_resolver_a_rejects_bad_targets(staff_install, target):
    item = await _claimed(staff_install)
    link_item = uuid4() if target == "missing" else item
    overrides: dict[str, Any] = {"links": (_obs().Link(rs.ITEM_LINK, link_item,
                                                       rs.RELATIONSHIP),)}
    if target == "wrong-patient":
        overrides["patient_id"] = UUID("00000000-0000-4000-8000-0000000b2f02")
    await _expect_link_invalid(staff_install, item, **overrides)


async def test_xr3_resolver_a_rejects_other_tenant_item(staff_install, staff_foreign_install):
    foreign_item = await _claimed(staff_foreign_install)
    item = await _claimed(staff_install)
    await _expect_link_invalid(staff_install, item, links=(
        _obs().Link(rs.ITEM_LINK, foreign_item, rs.RELATIONSHIP),))


# ---------------------------------------------------------------------------
# RB rollback / recovery (full-state oracle; each fault proven to fire)
# ---------------------------------------------------------------------------


class _Boom(Exception):
    pass


def _boom() -> None:
    raise _Boom("fault")


async def _resolve_with(install: st.StaffInstall, rec_kwargs: dict[str, Any], action: Any,
                        keys: Any = None) -> tuple[Any, sv.Recorder]:
    async with sv.runtime(install) as rt:
        rec = sv.Recorder(rt.gateway, **rec_kwargs)
        ctx = await sv.context(rt, install, m05.S1, sv.RESOLVE_CAPS)
        try:
            return await _service(rt, keys or sv.TestNoteKeys(), rec).act(ctx, action), rec
        except BaseException as error:  # noqa: BLE001 - CancelledError included
            return error, rec


def _appended(rec: sv.Recorder) -> None:
    """The append really ran and appended (the precondition for a post-append fault)."""

    results = rec.returned(sv.K_APPEND)
    assert len(results) == 1 and len(results[0]) == 1, results


async def test_rb_control_unfaulted_path_reaches_every_write(staff_install):
    """Prerequisite control for RB-3..7: the same command without a fault writes the
    event, link, outbox, note, history, action and item rows (exact delta)."""

    item = await _claimed(staff_install)
    before = sv.full_state(staff_install)
    result, rec = await _resolve_with(staff_install, {}, _resolve(item, 2, note="Control."))
    assert result.outcome == "applied", result
    assert rec.errors == []
    rs.resolution_delta(before, sv.full_state(staff_install), item,
                        snapshot=dict(result.snapshot), note=True)


@pytest.mark.parametrize("point", ["after-append", "after-apply"])
async def test_rb1_rb9_fault_before_marker_rolls_back_everything(staff_install, point):
    item = await _claimed(staff_install)
    before = sv.full_state(staff_install)
    hooks = {"after": {sv.K_APPEND if point == "after-append" else sv.K_APPLY: _boom}}
    error, rec = await _resolve_with(staff_install, hooks, _resolve(item, 2, note="Fault."))
    assert type(error) is _Boom, error  # a non-database exception propagates unchanged
    _appended(rec)
    if point == "after-apply":
        (applied,) = rec.returned(sv.K_APPLY)
        assert applied[0][0]["outcome"] == "applied"  # the writes happened, then rolled back
    else:
        assert sv.K_APPLY not in rec.statements
    assert rec.errors == []
    assert sv.full_state(staff_install) == before


FAULTS = {  # table → (trigger event, statement whose execution must raise B4_FAULT)
    "m05_followup_notes": ("INSERT", sv.K_APPLY),
    "m05_followup_history": ("INSERT", sv.K_APPLY),
    "m05_followup_actions": ("INSERT", sv.K_APPLY),
    "m05_followup_items": ("UPDATE", sv.K_APPLY),
    "event_handoff_outbox": ("INSERT", sv.K_APPEND),
}


@contextlib.contextmanager
def _fault_trigger(install: st.StaffInstall, table: str, event: str) -> Iterator[None]:
    """A test-only BEFORE trigger that raises P0001 B4_FAULT. Before raising it takes
    nextval() of a disposable SEQUENCE: sequence advances are not transactional, so
    the count survives the rollback and is a fault-hit witness separate from the
    full-state oracle (Codex ruling 4f51be67). Control: the sequence is unused before
    the row; cleanup drops trigger, function and sequence."""

    schema = sql.Identifier(install.schema)
    st.admin_rows(install, sql.SQL("CREATE SEQUENCE {}.b4_fault_hits").format(schema))
    st.admin_rows(install, sql.SQL(
        "CREATE FUNCTION {s}.b4_fault() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER"
        " SET search_path = pg_catalog AS $f$BEGIN"
        " PERFORM pg_catalog.nextval('{q}.b4_fault_hits');"
        " RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'B4_FAULT'; END$f$"
    ).format(s=schema, q=sql.SQL(install.schema)))
    try:
        st.admin_rows(install, sql.SQL(
            "CREATE TRIGGER b4_fault BEFORE {} ON {}.{} FOR EACH ROW"
            " EXECUTE FUNCTION {}.b4_fault()").format(
            sql.SQL(event), schema, sql.Identifier(table), schema))
        assert _fault_hits(install) == 0, "CONTROL: witness used before the row"
        yield
    finally:
        st.admin_rows(install, sql.SQL("DROP TRIGGER IF EXISTS b4_fault ON {}.{}").format(
            schema, sql.Identifier(table)))
        st.admin_rows(install, sql.SQL("DROP FUNCTION IF EXISTS {}.b4_fault()").format(schema))
        st.admin_rows(install, sql.SQL("DROP SEQUENCE IF EXISTS {}.b4_fault_hits").format(
            schema))


def _fault_hits(install: st.StaffInstall) -> int:
    ((last_value, is_called),) = st.admin_rows(install, sql.SQL(
        "SELECT last_value, is_called FROM {}.b4_fault_hits").format(
        sql.Identifier(install.schema)))
    return int(last_value) if is_called else 0


@pytest.mark.parametrize("table", sorted(FAULTS))
async def test_rb3_rb7_write_fault_rolls_back_everything(staff_install, table):
    event, statement = FAULTS[table]
    item = await _claimed(staff_install)
    with _fault_trigger(staff_install, table, event):
        before = sv.full_state(staff_install)
        error, rec = await _resolve_with(staff_install, {}, _resolve(item, 2, note="Fault."))
        hits = _fault_hits(staff_install)
    assert hits == 1, hits  # the durable witness: the trigger fired exactly once
    # And exactly the expected statement raised the installed trigger's error.
    assert [k for k, _ in rec.errors] == [statement], rec.errors
    raw = rec.errors[0][1]
    assert isinstance(raw, psycopg.Error) and raw.sqlstate == "P0001"
    assert raw.diag.message_primary == "B4_FAULT"
    if statement == sv.K_APPEND:
        assert type(error) is _obs().AppendFailed and error.reason_code == "APPEND_UNAVAILABLE"
    else:
        _appended(rec)
        assert type(error) is _svc().ActionFailed and error.reason_code == "ACTION_UNAVAILABLE"
    assert error.__cause__ is None and error.__context__ is None
    assert sv.full_state(staff_install) == before


async def test_rb8_invalid_apply_output_rolls_back(staff_install):
    item = await _claimed(staff_install)
    before = sv.full_state(staff_install)
    hooks = {"substitute": {sv.K_APPLY: [({"schema_version": 1, "outcome": "applied",
                                           "snapshot": {}},)]}}
    error, rec = await _resolve_with(staff_install, hooks, _resolve(item, 2, note="Bad output."))
    (real,) = rec.returned(sv.K_APPLY)
    assert real[0][0]["outcome"] == "applied"  # the real apply ran before substitution
    assert type(error) is _svc().ActionFailed and error.reason_code == "ACTION_RESULT_INVALID"
    assert sv.full_state(staff_install) == before


async def test_rb12_cancellation_before_marker(staff_install):
    item = await _claimed(staff_install)
    before = sv.full_state(staff_install)

    async def cancel() -> None:
        raise asyncio.CancelledError()

    error, rec = await _resolve_with(staff_install, {"before": {sv.K_APPLY: cancel}},
                                     _resolve(item, 2))
    assert type(error) is asyncio.CancelledError, error
    _appended(rec)
    assert rec.returned(sv.K_APPLY) == []  # cancelled before apply executed
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
        (original_rows,) = rec.returned(sv.K_APPLY)
        original = original_rows[0][0]
        after_fault = sv.full_state(staff_install)
        retry = await service.act(ctx, action)
    after_retry = sv.full_state(staff_install)
    if COMMIT_MODES[mode]:
        event_id = rs.resolution_delta(before, after_fault, item,
                                       snapshot=original["snapshot"], note=True)
        assert (retry.outcome, dict(retry.snapshot)) == ("replayed", original["snapshot"])
        assert after_retry == after_fault  # the retry wrote nothing
    else:
        assert after_fault == before
        assert retry.outcome == "applied"
        event_id = rs.resolution_delta(before, after_retry, item,
                                       snapshot=dict(retry.snapshot), note=True)
    (event,) = _events_for(staff_install, item)
    assert UUID(event["event_id"]) == event_id


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
        retry = await service.act(ctx, action)
    assert retry.outcome == "applied"
    rs.resolution_delta(before, sv.full_state(staff_install), item,
                        snapshot=dict(retry.snapshot), note=False)


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


def _hex_mutations(real: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """case → the substituted note-read rows (None = two rows of the real envelope)."""

    def with_(**changes: Any) -> list[tuple[dict[str, Any]]]:
        return [({**real, **changes},)]

    missing = dict(real)
    missing.pop("enc_key_version")
    return {
        "swapped-ciphertext": with_(ciphertext=other["ciphertext"]),
        "extra-key": with_(x=1),
        "missing-key": [(missing,)],
        "two-rows": [(real,), (real,)],
        "two-columns": [(real, 1)],
        "schema-version-2": with_(schema_version=2),
        "note-id-mismatch": with_(note_id=other["note_id"]),
        "item-id-uppercase": with_(item_id=real["item_id"].upper()),
        "item-id-mismatch": with_(item_id=other["item_id"]),
        "action-key-not-uuid": with_(client_action_id="not-a-uuid"),
        "short-nonce": with_(nonce=real["nonce"][:-2]),
        "long-nonce": with_(nonce=real["nonce"] + "00"),
        "non-hex-nonce": with_(nonce="zz" * 12),
        "null-nonce": with_(nonce=None),
        "short-ciphertext": with_(ciphertext="00" * 15),
        "oversize-ciphertext": with_(ciphertext="00" * (500 * 4 + 17)),
        "version-zero": with_(enc_key_version=0),
        "version-str": with_(enc_key_version="1"),
        "version-bool": with_(enc_key_version=True),
    }


NR5_CASES = (
    "swapped-ciphertext", "extra-key", "missing-key", "two-rows", "two-columns",
    "schema-version-2", "note-id-mismatch", "item-id-uppercase", "item-id-mismatch",
    "action-key-not-uuid", "short-nonce", "long-nonce", "non-hex-nonce", "null-nonce",
    "short-ciphertext", "oversize-ciphertext", "version-zero", "version-str", "version-bool",
)
NR5_INTEGRITY = {"swapped-ciphertext"}


async def _two_notes(install: st.StaffInstall, keys: Any) -> tuple[UUID, dict[str, Any],
                                                                    dict[str, Any]]:
    item_a = await _resolved_with_note(install, keys, "Note A.")
    item_b = await _resolved_with_note(install, keys, "Note B.")
    note_a = await asyncio.to_thread(_note_id, install, item_a)
    note_b = await asyncio.to_thread(_note_id, install, item_b)
    real_a = await asyncio.to_thread(st.call, install, "m05_read_note", str(note_a))
    real_b = await asyncio.to_thread(st.call, install, "m05_read_note", str(note_b))
    return note_a, real_a, real_b


async def _read_failure(install: st.StaffInstall, keys: Any, note_id: UUID,
                        substitute: Any = None) -> Any:
    async with sv.runtime(install) as rt:
        rec = sv.Recorder(rt.gateway)
        if substitute is not None:
            rec.substitute[sv.K_NOTE] = substitute
        ctx = await sv.context(rt, install, m05.S1, frozenset({sv.NOTE_READ}))
        with pytest.raises(_svc().ReadFailed) as caught:
            await _service(rt, keys, rec).read_note(ctx, note_id)
    return caught.value


@pytest.mark.parametrize("case", NR5_CASES)
async def test_nr5_envelope_binding_and_size_corpus(staff_install, case):
    keys = sv.TestNoteKeys()
    note_a, real_a, real_b = await _two_notes(staff_install, keys)
    error = await _read_failure(staff_install, keys, note_a,
                                _hex_mutations(real_a, real_b)[case])
    expected = "NOTE_INTEGRITY_FAILED" if case in NR5_INTEGRITY else "READ_RESULT_INVALID"
    assert error.reason_code == expected
    assert error.__cause__ is None and error.__context__ is None
    assert "Note A." not in str(error) and "Note B." not in str(error)


async def test_nr5_key_unavailable(staff_install):
    keys = sv.TestNoteKeys()
    note_a, _, _ = await _two_notes(staff_install, keys)
    keys.retire(1)
    error = await _read_failure(staff_install, keys, note_a)
    assert error.reason_code == "NOTE_KEY_UNAVAILABLE"
    assert error.__cause__ is None and error.__context__ is None


def _aead(key: bytes, tenant: str, envelope: dict[str, Any], plaintext: str) -> dict[str, Any]:
    """An INDEPENDENT AES-256-GCM encryption (not haloflow.m05.notes) under the agreed
    AAD form, so the row is authentic for the envelope it claims."""

    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aad = json.dumps(["m05.note/v1", tenant, envelope["item_id"], envelope["client_action_id"],
                      envelope["note_id"], envelope["enc_key_version"]],
                     separators=(",", ":")).encode()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    return {**envelope, "nonce": nonce.hex(), "ciphertext": ciphertext.hex()}


@pytest.mark.parametrize("plaintext", ["  padded  ", "a\r\nb", "Cafe\u0301", "a\u0007b", "",
                                       "x" * 501])
async def test_nr7_authentic_non_normal_plaintext_is_integrity_failure(staff_install, plaintext):
    keys = sv.TestNoteKeys()
    note_a, real_a, _ = await _two_notes(staff_install, keys)
    forged = _aead(keys.enc_keys[1], staff_install.tenant[0], real_a, plaintext)
    error = await _read_failure(staff_install, keys, note_a, [(forged,)])
    assert error.reason_code == "NOTE_INTEGRITY_FAILED"
    assert error.__cause__ is None and error.__context__ is None


async def test_nr7_control_authentic_normal_plaintext_reads(staff_install):
    """Control for NR-7: the independent encryption is accepted when the plaintext is
    D11-normal, so the NR-7 refusals are the post-decrypt normalization check."""

    keys = sv.TestNoteKeys()
    note_a, real_a, _ = await _two_notes(staff_install, keys)
    forged = _aead(keys.enc_keys[1], staff_install.tenant[0], real_a, "Independent text.")
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway)
        rec.substitute[sv.K_NOTE] = [(forged,)]
        ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.NOTE_READ}))
        note = await _service(rt, keys, rec).read_note(ctx, note_a)
    assert note["text"] == "Independent text."


async def test_nr6_foreign_note_id_with_eligible_local_reader(staff_install,
                                                              staff_foreign_install):
    """An eligible LOCAL reader (S1) asks for a note id that exists only in another
    clinic: identical to an unknown id."""

    foreign_keys = sv.TestNoteKeys()
    foreign_item = await _claimed(staff_foreign_install)
    await _resolve_once(staff_foreign_install, foreign_keys,
                        _resolve(foreign_item, 2, note="Foreign note."))
    foreign_note = await asyncio.to_thread(_note_id, staff_foreign_install, foreign_item)
    outcomes = []
    for note_id in (foreign_note, uuid4()):
        async with sv.runtime(staff_install) as rt:
            ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.NOTE_READ}))
            with pytest.raises(_svc().ReadRefused) as caught:
                await _service(rt, sv.TestNoteKeys()).read_note(ctx, note_id)
        outcomes.append((type(caught.value), caught.value.code, str(caught.value)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][1] == "ITEM_NOT_FOUND"


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


def _all_text(error: BaseException) -> str:
    return "".join(traceback.format_exception(error)) + repr(error) + repr(error.args)


async def _san2_case(install: st.StaffInstall, case: str) -> BaseException:
    keys = sv.TestNoteKeys()
    if case == "key-unavailable":
        item = await _claimed(install)
        keys.current_unavailable = True
        error, _ = await _resolve_with(install, {}, _resolve(item, 2, note=CANARY), keys)
        return error
    if case == "refused":
        item = await _claimed(install, m05.S2)  # S1 may not resolve S2's item
        error, _ = await _resolve_with(install, {}, _resolve(item, 2, note=CANARY), keys)
        return error
    if case == "driver-fault":
        item = await _claimed(install)
        with _fault_trigger(install, "m05_followup_notes", "INSERT"):
            error, _ = await _resolve_with(install, {}, _resolve(item, 2, note=CANARY), keys)
            assert _fault_hits(install) == 1, "SETUP: the note fault did not fire"
        return error
    # read-integrity: a canary note whose ciphertext is swapped on read
    item_a = await _resolved_with_note(install, keys, CANARY)
    item_b = await _resolved_with_note(install, keys, "Other.")
    note_a = await asyncio.to_thread(_note_id, install, item_a)
    real_a = await asyncio.to_thread(st.call, install, "m05_read_note", str(note_a))
    real_b = await asyncio.to_thread(st.call, install, "m05_read_note",
                                     str(await asyncio.to_thread(_note_id, install, item_b)))
    return await _read_failure(install, keys, note_a,
                               [({**real_a, "ciphertext": real_b["ciphertext"]},)])


SAN2_EXPECTED = {
    "key-unavailable": ("ActionFailed", "KEY_UNAVAILABLE"),
    "refused": ("ActionRefused", "NOT_AUTHORIZED"),
    "driver-fault": ("ActionFailed", "ACTION_UNAVAILABLE"),
    "read-integrity": ("ReadFailed", "NOTE_INTEGRITY_FAILED"),
}


@pytest.mark.parametrize("case", sorted(SAN2_EXPECTED))
async def test_san2_error_paths_carry_no_canary(staff_install, caplog, case):
    before = sv.full_state(staff_install)
    error = await _san2_case(staff_install, case)
    name, code = SAN2_EXPECTED[case]
    assert type(error).__name__ == name, error
    assert getattr(error, "reason_code", getattr(error, "code", None)) == code
    assert error.__cause__ is None and error.__context__ is None
    text = _all_text(error)
    for needle in (CANARY, CANARY.encode().hex(), CANARY.split()[0]):
        assert needle not in text, case
        assert needle not in caplog.text, case
    after = sv.full_state(staff_install)
    assert CANARY not in json.dumps(after, default=str)
    if case != "read-integrity":
        assert after == before  # rejected writes leave the state unchanged
