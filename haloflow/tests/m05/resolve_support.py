"""B4 shared helpers (package v2 + v2r1; red review REVISE 4c5b82fb R1/R3).

- The service path: `resolve_action`, `service`, `claimed`, `resolve_once`.
- `append_resolution`: a REAL producer append (EventFoundation). A refusal is
  reported as an explicit SETUP failure carrying the M02 reason code (R1), never as a
  shared fixed message.
- `synthetic_event`: the controlled persisted-event seam for the MR-10 matrix. The
  admin INSERTs one patient_events row and its links in ONE transaction in normal
  mode: every FK, CHECK and trigger is honoured and producer validation is not
  touched (R1: "do not weaken producer validation"). Before returning, it reads the
  persisted row back and asserts it differs from the valid baseline in exactly the
  intended field (the setup control).
- `resolution_delta`: the exact committed-recovery delta (R3).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID, uuid4

import m05_support as m05
import pytest
import service_support as sv
import staff_support as st
from psycopg import sql
from psycopg.types.json import Jsonb

CANARY = "CANARY-b4-9c1d Mrs Q Example 555-0199 DOB 01/02/1950"
ITEM_LINK = "m05_followup_item_v1"
RELATIONSHIP = "case"


def svc() -> Any:
    import haloflow.m05.service as service

    return service


def obs() -> Any:
    import haloflow.m02.observation as observation

    return observation


def resolve_action(item: UUID, version: int = 1, kind: str = "resolve",
                   outcome: str = "rescheduled", note: str | None = None,
                   key: UUID | None = None) -> Any:
    return svc().ResolveAction(item_id=item, client_action_id=key or uuid4(),
                               expected_version=version, type=kind, outcome_code=outcome,
                               note=note)


def service(rt: Any, keys: Any, rec: Any = None) -> Any:
    gateway = rec or rt.gateway
    foundation = obs().EventFoundation(gateway, producer_service=sv.PRODUCER)
    return svc().M05Service(gateway, foundation=foundation, keys=keys)


async def claimed(install: st.StaffInstall, actor: str = m05.S1) -> UUID:
    """An item assigned to `actor` (version 2), via the accepted SQL claim."""

    from test_m05_actions_postgres import _cmd
    from test_m05_service_postgres import _aitem

    item = await _aitem(install)
    await asyncio.to_thread(st.call, install, "m05_apply_action_v2",
                            _cmd(str(item), 1, "claim", actor=actor))
    return item


async def resolve_once(install: st.StaffInstall, keys: Any, action: Any,
                       caps: frozenset[str] = sv.RESOLVE_CAPS, actor: str = m05.S1) -> Any:
    async with sv.runtime(install) as rt:
        ctx = await sv.context(rt, install, actor, caps)
        return await service(rt, keys).act(ctx, action)


def rows(install: st.StaffInstall, table: str, item: UUID) -> list[dict[str, Any]]:
    found = st.admin_rows(install, sql.SQL(
        "SELECT to_jsonb(t) FROM {}.{} t WHERE t.item_id = %s").format(
        sql.Identifier(install.schema), sql.Identifier(table)), (str(item),))
    return [r[0] for r in found]


def events_for(install: st.StaffInstall, item: UUID) -> list[dict[str, Any]]:
    found = st.admin_rows(install, sql.SQL(
        "SELECT to_jsonb(e) FROM {}.patient_events e JOIN {}.patient_event_links l"
        " ON l.event_id = e.event_id WHERE l.link_id = %s AND e.event_type = %s").format(
        sql.Identifier(install.schema), sql.Identifier(install.schema)),
        (str(item), sv.RESOLUTION_TYPE))
    return [r[0] for r in found]


def item_view(install: st.StaffInstall, item: UUID) -> dict[str, Any]:
    view: dict[str, Any] = st.call(install, "m05_get_item", str(item))
    return view


# ---------------------------------------------------------------------------
# The real producer append (fixture), with explicit setup reporting
# ---------------------------------------------------------------------------


async def append_resolution(install: st.StaffInstall, item: UUID, key: UUID, *,
                            actor: str = m05.S1, **overrides: Any) -> UUID:
    view = await asyncio.to_thread(item_view, install, item)
    o = obs()
    values: dict[str, Any] = {
        "event_id": uuid4(), "event_type": sv.RESOLUTION_TYPE, "contract_version": 1,
        "correlation_id": uuid4(),
        "producer_dedupe_key": sv.resolution_dedupe_key(install.tenant[0], str(item), str(key)),
        "links": (o.Link(ITEM_LINK, item, RELATIONSHIP),),
        "metadata": {"outcome_code": "rescheduled", "assertion_kind": "staff",
                     "item_kind": view["item_kind"]},
        "patient_id": UUID(view["patient_id"]),
        "causation_event_id": UUID(view["source_event_id"]),
        "occurred_at": None, "source_time_quality": "absent",
    }
    values.update(overrides)
    observation = o.Observation(**values)
    async with sv.runtime(install) as rt:
        ctx = await sv.context(rt, install, actor, frozenset({sv.APPEND}))
        foundation = o.EventFoundation(rt.gateway, producer_service=sv.PRODUCER)

        async def body(binding: Any) -> Any:
            return await foundation.append_observation_in(binding, observation)

        await foundation.run_in_transaction(ctx, body)
    return observation.event_id  # type: ignore[no-any-return]


async def append_or_setup_fail(install: st.StaffInstall, item: UUID, key: UUID,
                               **kwargs: Any) -> UUID:
    """A fixture append that MUST succeed; a refusal fails the row as SETUP with the
    M02 reason code, so the census reports each setup cause (R1)."""

    try:
        return await append_resolution(install, item, key, **kwargs)
    except obs().AppendFailed as error:
        pytest.fail(f"SETUP: fixture append refused: {error.reason_code}")


# ---------------------------------------------------------------------------
# The synthetic persisted-event seam (MR-10 matrix)
# ---------------------------------------------------------------------------

EVENT_FIELDS = (
    "event_type", "contract_version", "event_level", "status", "operation_id",
    "occurred_at", "source_time_quality", "actor_id", "workload_id", "patient_id",
    "causation_event_id", "producer_dedupe_key", "metadata",
)


def baseline_event(install: st.StaffInstall, item: UUID, key: UUID,
                   actor: str = m05.S1) -> dict[str, Any]:
    """Every envelope-relevant field of a resolution event that apply must accept."""

    view = item_view(install, item)
    return {
        "event_type": sv.RESOLUTION_TYPE, "contract_version": 1,
        "event_level": "observation", "status": "completed", "operation_id": None,
        "occurred_at": None, "source_time_quality": "absent",
        "actor_id": actor, "workload_id": None,
        "patient_id": view["patient_id"], "causation_event_id": view["source_event_id"],
        "producer_dedupe_key": sv.resolution_dedupe_key(install.tenant[0], str(item), str(key)),
        "metadata": {"outcome_code": "rescheduled", "assertion_kind": "staff",
                     "item_kind": view["item_kind"]},
        "links": [{"link_type": ITEM_LINK, "link_id": str(item), "relationship": RELATIONSHIP}],
    }


def _operation(conn: Any, schema: str) -> str:
    operation = str(uuid4())
    conn.execute(sql.SQL(
        "INSERT INTO {}.operation_registry (operation_id, owner_service, action_code,"
        " business_key_fingerprint, business_key_version, subject_type, subject_id,"
        " correlation_id, producer_version) VALUES (%s, 'm05_followup', 'b4_fixture', %s, 1,"
        " 'b4_fixture', %s, %s, 'b4-test')").format(sql.Identifier(schema)),
        (operation, os.urandom(32), str(uuid4()), str(uuid4())))
    return operation


def _version_two_contract(conn: Any, schema: str) -> None:
    """A second contract version for the type (needed by the FK), inactive type."""

    conn.execute(sql.SQL(
        "INSERT INTO {s}.event_contracts SELECT (jsonb_populate_record(NULL::{s}.event_contracts,"
        " to_jsonb(c) || '{{\"version\": 2}}'::jsonb)).* FROM {s}.event_contracts c"
        " WHERE c.event_type = %s AND c.version = 1 ON CONFLICT DO NOTHING").format(
        s=sql.Identifier(schema)), (sv.RESOLUTION_TYPE,))


def _other_contract(conn: Any, schema: str) -> tuple[str, int]:
    row = conn.execute(sql.SQL(
        "SELECT event_type, version FROM {}.event_contracts WHERE event_type <> %s"
        " AND event_level = 'observation' ORDER BY event_type, version LIMIT 1").format(
        sql.Identifier(schema)), (sv.RESOLUTION_TYPE,)).fetchone()
    assert row is not None, "SETUP: no other observation contract"
    return row[0], row[1]


def _persisted(conn: Any, schema: str, event_id: str) -> dict[str, Any]:
    row = conn.execute(sql.SQL(
        "SELECT to_jsonb(e) FROM {}.patient_events e WHERE e.event_id = %s").format(
        sql.Identifier(schema)), (event_id,)).fetchone()
    assert row is not None
    event = {f: row[0][f] for f in EVENT_FIELDS}
    links = conn.execute(sql.SQL(
        "SELECT link_type, link_id::text, relationship FROM {}.patient_event_links"
        " WHERE event_id = %s ORDER BY link_type, link_id, relationship").format(
        sql.Identifier(schema)), (event_id,)).fetchall()
    event["links"] = [{"link_type": t, "link_id": i, "relationship": r} for t, i, r in links]
    return event


def synthetic_event(install: st.StaffInstall, item: UUID, key: UUID, *,
                    field: str | None = None, value: Any = None,
                    actor: str = m05.S1) -> UUID:
    """Persist one event equal to the valid baseline except `field` := `value`, and
    prove it (setup control) before returning its id. `value` may be a callable taking
    the admin connection (for values that need a row to exist first)."""

    import psycopg

    base = baseline_event(install, item, key, actor)
    event_id = str(uuid4())
    schema = install.schema
    with psycopg.connect(install.ids.admin) as conn, conn.transaction():
        target = dict(base)
        if field is not None:
            target[field] = value(conn) if callable(value) else value
        if field == "contract_version":
            _version_two_contract(conn, schema)
        if field == "event_type":
            target["event_type"], target["contract_version"] = _other_contract(conn, schema)
        if field == "operation_id" and target["operation_id"] == "NEW":
            target["operation_id"] = _operation(conn, schema)
        conn.execute(sql.SQL(
            "INSERT INTO {}.patient_events (event_id, operation_id, patient_id, event_type,"
            " contract_version, event_level, status, correlation_id, causation_event_id,"
            " producer_dedupe_key, content_fingerprint, producer_service, actor_id,"
            " workload_id, auth_method, occurred_at, source_time_quality, received_at,"
            " metadata) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " 'test', %s, %s, statement_timestamp(), %s)").format(sql.Identifier(schema)),
            (event_id, target["operation_id"], target["patient_id"], target["event_type"],
             target["contract_version"], target["event_level"], target["status"],
             str(uuid4()), target["causation_event_id"], target["producer_dedupe_key"],
             os.urandom(32), sv.PRODUCER, target["actor_id"], target["workload_id"],
             target["occurred_at"], target["source_time_quality"], Jsonb(target["metadata"])))
        for link in target["links"]:
            conn.execute(sql.SQL(
                "INSERT INTO {}.patient_event_links (event_id, link_type, link_id,"
                " relationship) VALUES (%s, %s, %s, %s)").format(sql.Identifier(schema)),
                (event_id, link["link_type"], link["link_id"], link["relationship"]))
        persisted = _persisted(conn, schema, event_id)
    expected = dict(target)
    expected["links"] = sorted(target["links"], key=lambda x: (x["link_type"], x["link_id"],
                                                               x["relationship"]))
    if expected["occurred_at"] is not None:
        expected["occurred_at"] = persisted["occurred_at"]  # server text form
    assert persisted == expected, ("SETUP CONTROL: persisted event differs from target",
                                   persisted, expected)
    differs = {f for f in base if json.dumps(persisted[f], sort_keys=True)
               != json.dumps(base[f], sort_keys=True)}
    if field is None:
        intended: set[str] = set()
    elif field == "event_type":  # the other contract's version may also be 1
        intended = {"event_type"} | ({"contract_version"} if target["contract_version"] != 1
                                     else set())
    else:
        intended = {field}
    assert differs == intended, ("SETUP CONTROL: wrong fields differ", differs, intended)
    return UUID(event_id)


def apply_command(item: UUID, key: UUID, event_id: UUID, version: int = 2,
                  outcome: str = "rescheduled", actor: str = m05.S1,
                  kind: str = "actor") -> dict[str, Any]:
    return {
        "schema_version": 1, "item_id": str(item), "client_action_id": str(key),
        "expected_version": version,
        "action": {"type": "resolve", "outcome_code": outcome, "note": None,
                   "payload": {"event_id": str(event_id), "note_row": None}},
        "permissions": m05.STAFF_PERMISSIONS,
        "attribution": st.attribution(actor, kind=kind),
    }


# ---------------------------------------------------------------------------
# Exact committed-recovery delta (R3)
# ---------------------------------------------------------------------------


def _m02_added(before: dict[str, Any], after: dict[str, Any], table: str) -> list[dict[str, Any]]:
    b, a = set(before[table]), set(after[table])
    assert not (b - a), (table, "removed", sorted(b - a))
    return [json.loads(x) for x in sorted(a - b)]


def resolution_delta(before: dict[str, Any], after: dict[str, Any], item: UUID, *,
                     snapshot: dict[str, Any], note: bool) -> UUID:
    """Assert the complete delta of one committed resolve; return its event id."""

    events = _m02_added(before, after, "patient_events")
    assert len(events) == 1, events
    event_id = events[0]["event_id"]
    assert events[0]["event_type"] == sv.RESOLUTION_TYPE
    assert _m02_added(before, after, "patient_event_links") == [
        {"event_id": event_id, "link_type": ITEM_LINK, "link_id": str(item),
         "relationship": RELATIONSHIP}]
    outbox = _m02_added(before, after, "event_handoff_outbox")
    assert len(outbox) == 1 and outbox[0]["source_event_id"] == event_id, outbox
    delta = st.Delta(before, after)
    delta.untouched(st.TABLES)
    (changed,) = delta.changed["m05_followup_items"]
    assert changed[0]["item_id"] == str(item)
    assert (changed[1]["state"], changed[1]["assignee_id"], changed[1]["outcome_code"],
            changed[1]["version"]) == (snapshot["state"], snapshot["assignee_id"],
                                       snapshot["outcome_code"], snapshot["version"])
    assert not delta.added["m05_followup_items"] and not delta.removed["m05_followup_items"]
    (history,) = delta.added["m05_followup_history"]
    assert (history["item_id"], history["resolution_event_id"]) == (str(item), event_id)
    (action,) = delta.added["m05_followup_actions"]
    assert (action["item_id"], action["client_action_id"], action["event_id"]) == (
        str(item), snapshot["client_action_id"], event_id)
    assert action["result_snapshot"] == snapshot
    assert len(delta.added["m05_followup_notes"]) == (1 if note else 0)
    for table in ("m05_followup_history", "m05_followup_actions", "m05_followup_notes"):
        assert not delta.removed[table] and not delta.changed[table], table
    return UUID(event_id)
