"""B3b M05 staff follow-up service over the M01 gateway (package v2 + v2r1,
Claude/Codex aligned 2026-11-01; red gate PASS at 537c1b0).

The public commands carry no permissions, attribution, tenant, actor or digest: the
service builds those from the trusted TenantContext only (attribution exactly as
M02; read = m05.followup.read, act = m05.followup.apply, supervise =
m05.followup.supervise), and the SQL additionally requires current directory standing
(actions: under the membership lock; reads: the t025 reader statement first).

The action route is ONE gateway transaction: prepare (REFUSED short-circuits), then
apply with the identical command; apply is authoritative. Results are validated and
bound to the request before the marker; after it, failures are ActionCommitUnknown.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.staff.service import (
    READ_CAPABILITY,
    CommandMalformed,
    ReadFailed,
    ReadRefused,
    RouteDenied,
    actor_attribution,
    check_reader,
    exact_int,
    fetch_rows,
    is_uuid,
    member,
    read_error,
    require_capabilities,
    run_with_marker,
    single_json,
    sqlstate_and_message,
    uuid_text,
    valid_timestamp,
)
from haloflow.m02.observation import Link, Observation
from haloflow.m05.notes import (
    MAX_CIPHERTEXT_BYTES,
    NONCE_BYTES,
    TAG_BYTES,
    EncryptedNote,
    KeyUnavailable,
    NoteIntegrityFailed,
    NoteInvalid,
    decrypt_note,
    encrypt_note,
    normalize_note,
    note_hmac,
)

__all__ = [
    "ActionCommitUnknown",
    "ActionFailed",
    "ActionRefused",
    "ActionResult",
    "CommandMalformed",
    "Cursor",
    "ListQuery",
    "M05Service",
    "ReadFailed",
    "ReadRefused",
    "ResolveAction",
    "RouteDenied",
    "StaffAction",
]

PREPARE_CAPABILITY: Final = "m05.followup.prepare"
APPLY_CAPABILITY: Final = "m05.followup.apply"
SUPERVISE_CAPABILITY: Final = "m05.followup.supervise"

K_PREPARE: Final = "m05.followup.prepare"
K_APPLY: Final = "m05.followup.apply"
K_GET: Final = "m05.followup.get"
K_LIST: Final = "m05.followup.list"
K_HISTORY: Final = "m05.followup.history"

ACTION_TYPES: Final = ("claim", "start_progress", "assign", "reassign")
STATES: Final = ("open", "in_progress", "resolved")
MAX_VERSION: Final = 2147483647
ACTION_REFUSALS: Final = frozenset({
    "COMMAND_MALFORMED", "ITEM_NOT_FOUND", "NOT_AUTHORIZED", "ACTION_KEY_ACTOR_MISMATCH",
    "ACTION_KEY_CONFLICT", "ITEM_RESOLVED", "STALE_VERSION", "PRECONDITION_FAILED",
    "ACTION_NOT_IMPLEMENTED", "ASSIGNEE_NOT_ELIGIBLE",
    # B4 (package v2 1.4)
    "NOTE_REQUIRED", "RESOLUTION_EVENT_INVALID", "RESOLUTION_EVENT_REUSED",
})
ACTION_FAILURES: Final = frozenset({"ISOLATION_UNSUPPORTED", "COMPOSITION_UNSUPPORTED"})
SNAPSHOT_KEYS: Final = frozenset({
    "schema_version", "item_id", "client_action_id", "action", "version", "state",
    "assignee_id", "outcome_code",
})
ITEM_KEYS: Final = frozenset({
    "schema_version", "item_id", "source_event_id", "item_kind", "patient_id", "state",
    "assignee_id", "outcome_code", "version", "created_at",
})
ENTRY_KEYS: Final = frozenset({
    "seq", "action", "actor_id", "before", "after", "assertion_kind", "note_id",
    "created_at",
})
RESOLVE_ACTIONS: Final = ("resolve", "claim_and_resolve")
HISTORY_ACTIONS: Final = frozenset({
    "created", "claim", "assign", "reassign", "start_progress", "resolve",
    "claim_and_resolve",
})
ITEM_KINDS: Final = frozenset({"no_reply", "reschedule_request", "cancel_request",
                               "unclear_reply"})


class ActionRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("follow-up action refused")
        self.code = code


class ActionFailed(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__("follow-up action failed")
        self.reason_code = reason_code


class ActionCommitUnknown(Exception):
    """The outcome is unknown after the body completed. Recovery: retry the identical
    StaffAction (same client_action_id); it replays the stored snapshot if the first
    attempt committed and applies once if it did not."""

    def __init__(self, item_id: UUID, client_action_id: UUID) -> None:
        super().__init__("follow-up action outcome unknown")
        self.item_id = item_id
        self.client_action_id = client_action_id


# ---------------------------------------------------------------------------
# Public commands (validated on construction, exact types)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaffAction:
    item_id: UUID
    client_action_id: UUID
    expected_version: int
    type: str
    assignee_id: UUID | None = None

    def __post_init__(self) -> None:
        if not is_uuid(self.item_id):
            raise CommandMalformed("item_id")
        if not is_uuid(self.client_action_id):
            raise CommandMalformed("client_action_id")
        if not exact_int(self.expected_version) or not 1 <= self.expected_version <= MAX_VERSION:
            raise CommandMalformed("expected_version")
        if type(self.type) is not str or self.type not in ACTION_TYPES:
            raise CommandMalformed("type")
        targeted = self.type in ("assign", "reassign")
        if targeted != (self.assignee_id is not None) or (
            targeted and not is_uuid(self.assignee_id)
        ):
            raise CommandMalformed("assignee_id")


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    item_id: UUID

    def __post_init__(self) -> None:
        if (
            type(self.created_at) is not datetime
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() != timedelta(0)
        ):
            raise CommandMalformed("created_at")
        if not is_uuid(self.item_id):
            raise CommandMalformed("item_id")

    def render(self) -> dict[str, str]:
        return {"created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "item_id": str(self.item_id)}


@dataclass(frozen=True)
class ListQuery:
    limit: int
    states: tuple[str, ...] | None = None
    assignee: UUID | str | None = None
    after: Cursor | None = None

    def __post_init__(self) -> None:
        if not exact_int(self.limit) or not 1 <= self.limit <= 100:
            raise CommandMalformed("limit")
        if self.states is not None and (
            type(self.states) is not tuple or not self.states
            or not all(type(s) is str and s in STATES for s in self.states)
            or len(set(self.states)) != len(self.states)
        ):
            raise CommandMalformed("states")
        if self.assignee is not None and not is_uuid(self.assignee) and not (
            type(self.assignee) is str and self.assignee == "unassigned"
        ):
            raise CommandMalformed("assignee")
        if self.after is not None and type(self.after) is not Cursor:
            raise CommandMalformed("after")

    def render(self) -> dict[str, Any]:
        query: dict[str, Any] = {"schema_version": 1, "limit": self.limit}
        if self.states is not None:
            query["states"] = list(self.states)
        if self.assignee is not None:
            query["assignee"] = str(self.assignee)
        if self.after is not None:
            query["after"] = self.after.render()
        return query


@dataclass(frozen=True)
class ActionResult:
    outcome: str
    snapshot: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Classification and binding
# ---------------------------------------------------------------------------


def _action_error(error: psycopg.Error) -> Exception:
    sqlstate, message = sqlstate_and_message(error)
    if sqlstate == "P0001" and message in ACTION_REFUSALS:
        return ActionRefused(message)
    if sqlstate == "P0001" and message in ACTION_FAILURES:
        return ActionFailed(message)
    if sqlstate == "22023":
        return ActionFailed("TENANT_CONTEXT_INVALID")
    if sqlstate == "55P03":
        return ActionFailed("LOCK_TIMEOUT")
    return ActionFailed("ACTION_UNAVAILABLE")


def _invalid() -> ActionFailed:
    return ActionFailed("ACTION_RESULT_INVALID")


def _bound_snapshot(snap: Any, action: StaffAction, actor: str) -> dict[str, Any]:
    if type(snap) is not dict or set(snap) != SNAPSHOT_KEYS:
        raise _invalid()
    version, state, assignee = snap["version"], snap["state"], snap["assignee_id"]
    if not (
        exact_int(snap["schema_version"]) and snap["schema_version"] == 1
        and snap["item_id"] == str(action.item_id)
        and snap["client_action_id"] == str(action.client_action_id)
        and snap["action"] == action.type
        and exact_int(version) and version == action.expected_version + 1
        and snap["outcome_code"] is None
        and (assignee is None or uuid_text(assignee))
    ):
        raise _invalid()
    if action.type == "claim":
        ok = assignee == actor and member(state, ("open", "in_progress"))
    elif action.type == "start_progress":
        ok = assignee is not None and state == "in_progress"
    else:
        ok = assignee == str(action.assignee_id) and member(state, ("open", "in_progress"))
    if not ok:
        raise _invalid()
    return snap


def _decision(rows: list[Any], action: StaffAction, actor: str) -> str:
    value = single_json(rows)
    if type(value) is not dict or not exact_int(value.get("schema_version")) or value[
        "schema_version"
    ] != 1:
        raise _invalid()
    decision = value.get("decision")
    if decision == "NEW" and set(value) == {"schema_version", "decision"}:
        return "NEW"
    if decision == "REPLAY" and set(value) == {"schema_version", "decision", "snapshot"}:
        _bound_snapshot(value["snapshot"], action, actor)
        return "REPLAY"
    if (
        decision == "REFUSED" and set(value) == {"schema_version", "decision", "code"}
        and member(value["code"], ACTION_REFUSALS)
    ):
        raise ActionRefused(value["code"])
    raise _invalid()


def _applied(rows: list[Any], action: StaffAction, actor: str) -> ActionResult:
    value = single_json(rows)
    if (
        type(value) is not dict or set(value) != {"schema_version", "outcome", "snapshot"}
        or not exact_int(value["schema_version"]) or value["schema_version"] != 1
        or not member(value["outcome"], ("applied", "replayed"))
    ):
        raise _invalid()
    return ActionResult(value["outcome"], _bound_snapshot(value["snapshot"], action, actor))


def _read_invalid() -> ReadFailed:
    return ReadFailed("READ_RESULT_INVALID")


def _valid_item(view: Any) -> bool:
    return (
        type(view) is dict and set(view) == ITEM_KEYS
        and exact_int(view["schema_version"]) and view["schema_version"] == 1
        and uuid_text(view["item_id"]) and uuid_text(view["source_event_id"])
        and uuid_text(view["patient_id"]) and member(view["item_kind"], ITEM_KINDS)
        and member(view["state"], STATES)
        and (view["assignee_id"] is None or uuid_text(view["assignee_id"]))
        and (view["outcome_code"] is None or type(view["outcome_code"]) is str)
        and (view["state"] == "resolved") == (view["outcome_code"] is not None)
        and exact_int(view["version"]) and view["version"] >= 1
        and valid_timestamp(view["created_at"])
    )


def _valid_closed_state(value: Any) -> bool:
    return (
        type(value) is dict and set(value) == {"state", "assignee_id", "outcome_code"}
        and member(value["state"], STATES)
        and (value["assignee_id"] is None or uuid_text(value["assignee_id"]))
        and (value["outcome_code"] is None or type(value["outcome_code"]) is str)
    )


def _bind_item(rows: list[Any], item_id: UUID) -> dict[str, Any]:
    view = single_json(rows)
    if not _valid_item(view) or view["item_id"] != str(item_id):
        raise _read_invalid()
    return view  # type: ignore[no-any-return]


def _bind_history(rows: list[Any], item_id: UUID) -> dict[str, Any]:
    value = single_json(rows)
    if (
        type(value) is not dict or set(value) != {"schema_version", "item_id", "entries"}
        or not exact_int(value["schema_version"]) or value["schema_version"] != 1
        or value["item_id"] != str(item_id) or type(value["entries"]) is not list
    ):
        raise _read_invalid()
    for expected_seq, entry in enumerate(value["entries"], 1):
        if (
            type(entry) is not dict or set(entry) != ENTRY_KEYS
            or not exact_int(entry["seq"]) or entry["seq"] != expected_seq
            or not member(entry["action"], HISTORY_ACTIONS) or not uuid_text(entry["actor_id"])
            or (entry["before"] is not None and not _valid_closed_state(entry["before"]))
            or (entry["before"] is None) != (entry["action"] == "created")
            or not _valid_closed_state(entry["after"])
            or (entry["assertion_kind"] is not None
                and not member(entry["assertion_kind"], ("staff",)))
            or (entry["note_id"] is not None and not uuid_text(entry["note_id"]))
            or (entry["note_id"] is not None and entry["action"] not in RESOLVE_ACTIONS)
            or not valid_timestamp(entry["created_at"])
        ):
            raise _read_invalid()
    return value


def _bind_list(rows: list[Any], query: ListQuery) -> dict[str, Any]:
    value = single_json(rows)
    if (
        type(value) is not dict or set(value) != {"schema_version", "items", "next"}
        or not exact_int(value["schema_version"]) or value["schema_version"] != 1
        or type(value["items"]) is not list or len(value["items"]) > query.limit
    ):
        raise _read_invalid()
    keys: list[tuple[str, str]] = []
    for view in value["items"]:
        if not _valid_item(view):
            raise _read_invalid()
        if query.states is not None and view["state"] not in query.states:
            raise _read_invalid()
        if query.assignee == "unassigned" and view["assignee_id"] is not None:
            raise _read_invalid()
        if is_uuid(query.assignee) and view["assignee_id"] != str(query.assignee):
            raise _read_invalid()
        keys.append((view["created_at"], view["item_id"]))
    if keys != sorted(set(keys)):
        raise _read_invalid()
    after = query.after.render() if query.after is not None else None
    if after is not None and keys and keys[0] <= (after["created_at"], after["item_id"]):
        raise _read_invalid()
    nxt = value["next"]
    if nxt is not None and (
        type(nxt) is not dict or set(nxt) != {"created_at", "item_id"}
        or not valid_timestamp(nxt["created_at"]) or not uuid_text(nxt["item_id"])
        or len(keys) != query.limit or (nxt["created_at"], nxt["item_id"]) != keys[-1]
    ):
        raise _read_invalid()
    return value


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

ACTION_ROUTE: Final = frozenset({PREPARE_CAPABILITY, APPLY_CAPABILITY})
READ_ROUTE: Final = frozenset({READ_CAPABILITY})


OUTCOME_CODES: Final = ("rescheduled", "cancelled_in_ehr", "reached_no_change",
                         "no_contact_left_message", "no_contact_no_answer", "other")
APPEND_CAPABILITY: Final = "m02.observation.append"
NOTE_READ_CAPABILITY: Final = "m05.followup.note.read"
RESOLVE_ROUTE: Final = frozenset({PREPARE_CAPABILITY, APPLY_CAPABILITY, APPEND_CAPABILITY})
# Deviation D-2 (reported): the reader-standing statement (t025) requires
# m05.followup.read, so the note route is follow-up viewing plus note read (O-1).
NOTE_READ_ROUTE: Final = frozenset({READ_CAPABILITY, NOTE_READ_CAPABILITY})
K_NOTE: Final = "m05.followup.note.read"
K_APPEND_STATEMENT: Final = "m02.observation.append"
RESOLUTION_TYPE: Final = "m05_followup_resolution_asserted"
ITEM_LINK_TYPE: Final = "m05_followup_item_v1"


@dataclass(frozen=True)
class ResolveAction:
    """B4 (package v2 1.1). Validated on construction with exact types; the note is
    D11-normalized here (stored normalized; empty after trim is absent), and `other`
    requires a note before any gateway call."""

    item_id: UUID
    client_action_id: UUID
    expected_version: int
    type: str
    outcome_code: str
    note: str | None = None

    def __post_init__(self) -> None:
        if not is_uuid(self.item_id):
            raise CommandMalformed("item_id")
        if not is_uuid(self.client_action_id):
            raise CommandMalformed("client_action_id")
        if not exact_int(self.expected_version) or not 1 <= self.expected_version <= MAX_VERSION:
            raise CommandMalformed("expected_version")
        if not member(self.type, RESOLVE_ACTIONS):
            raise CommandMalformed("type")
        if not member(self.outcome_code, OUTCOME_CODES):
            raise CommandMalformed("outcome_code")
        failure: CommandMalformed | None = None
        try:
            note = normalize_note(self.note)
        except NoteInvalid:
            failure = CommandMalformed("note")
        if failure is not None:
            raise failure  # outside the handler: the note never reaches a chain
        if self.outcome_code == "other" and note is None:
            raise CommandMalformed("note")
        object.__setattr__(self, "note", note)


_NOTE_ROUTE_ERRORS: Final = frozenset({"NOTE_INTEGRITY_FAILED", "NOTE_KEY_UNAVAILABLE"})
_ENVELOPE_KEYS: Final = frozenset({
    "schema_version", "note_id", "item_id", "client_action_id", "nonce", "ciphertext",
    "enc_key_version",
})
_HEX: Final = frozenset("0123456789abcdef")
_LOG = logging.getLogger("haloflow.m05")


def _hex(value: Any, low: int, high: int) -> bool:
    return (
        type(value) is str and low <= len(value) <= high and len(value) % 2 == 0
        and set(value) <= _HEX
    )


def _key_version(value: Any) -> bool:
    return exact_int(value) and 1 <= value <= 32767


def _bound_resolve_snapshot(snap: Any, action: ResolveAction, actor: str) -> dict[str, Any]:
    if type(snap) is not dict or set(snap) != SNAPSHOT_KEYS:
        raise _invalid()
    assignee = snap["assignee_id"]
    if not (
        exact_int(snap["schema_version"]) and snap["schema_version"] == 1
        and snap["item_id"] == str(action.item_id)
        and snap["client_action_id"] == str(action.client_action_id)
        and snap["action"] == action.type
        and exact_int(snap["version"]) and snap["version"] == action.expected_version + 1
        and snap["state"] == "resolved"
        and snap["outcome_code"] == action.outcome_code
        and uuid_text(assignee)
        and (action.type != "claim_and_resolve" or assignee == actor)
    ):
        raise _invalid()
    return snap


def _resolve_decision(rows: list[Any], action: ResolveAction, actor: str, *,
                      second: bool) -> tuple[str, Any]:
    """Prepare #1: NEW{item} | REPLAY | REFUSED | REPLAY_CHALLENGE{version} (note only).
    Prepare #2: only a bound REPLAY or a known REFUSED (package v2 2.2, v2r1 A3)."""

    value = single_json(rows)
    if type(value) is not dict or not exact_int(value.get("schema_version")) or value[
        "schema_version"
    ] != 1:
        raise _invalid()
    decision = value.get("decision")
    if (
        decision == "REFUSED" and set(value) == {"schema_version", "decision", "code"}
        and member(value["code"], ACTION_REFUSALS)
    ):
        raise ActionRefused(value["code"])
    if decision == "REPLAY" and set(value) == {"schema_version", "decision", "snapshot"}:
        return "REPLAY", _bound_resolve_snapshot(value["snapshot"], action, actor)
    if second:
        raise _invalid()
    if decision == "NEW" and set(value) == {"schema_version", "decision", "item"}:
        item = value["item"]
        # Ruling 7ce0ddad (1): closed keys, bound to the request, canonical UUIDs and a
        # known kind, all before any key use or append.
        if (
            type(item) is dict
            and set(item) == {"item_id", "item_kind", "patient_id", "source_event_id"}
            and item["item_id"] == str(action.item_id)
            and member(item["item_kind"], ITEM_KINDS)
            and uuid_text(item["patient_id"]) and uuid_text(item["source_event_id"])
        ):
            return "NEW", item
        raise _invalid()
    if (
        decision == "REPLAY_CHALLENGE" and action.note is not None
        and set(value) == {"schema_version", "decision", "hmac_key_version"}
        and _key_version(value["hmac_key_version"])
    ):
        return "REPLAY_CHALLENGE", value["hmac_key_version"]
    raise _invalid()


def _bind_note(rows: list[Any], note_id: UUID) -> dict[str, Any]:
    value = single_json(rows)
    if not (
        type(value) is dict and set(value) == _ENVELOPE_KEYS
        and exact_int(value["schema_version"]) and value["schema_version"] == 1
        and value["note_id"] == str(note_id)
        and uuid_text(value["item_id"]) and uuid_text(value["client_action_id"])
        and _hex(value["nonce"], 2 * NONCE_BYTES, 2 * NONCE_BYTES)
        and _hex(value["ciphertext"], 2 * TAG_BYTES, 2 * MAX_CIPHERTEXT_BYTES)
        and _key_version(value["enc_key_version"])
    ):
        raise _read_invalid()
    envelope: dict[str, Any] = value
    return envelope


class M05Service:
    def __init__(self, gateway: Any, *, foundation: Any = None, keys: Any = None) -> None:
        self._gateway = gateway
        self._foundation = foundation  # B4: EventFoundation for the resolution append
        self._keys = keys  # B4: NoteKeyProvider

    async def act(self, context: Any, action: StaffAction | ResolveAction) -> ActionResult:
        if type(action) is ResolveAction:
            return await self._resolve(context, action)
        if type(action) is not StaffAction:
            raise CommandMalformed("action")
        require_capabilities(context, ACTION_ROUTE)
        attribution = actor_attribution(context)
        if attribution is None:
            raise ActionRefused("NOT_AUTHORIZED")
        actor = attribution["principal_id"]
        if action.type == "assign" and str(action.assignee_id) == actor:
            raise CommandMalformed("assignee_id")  # self-assignment is claim
        operation: dict[str, Any] = {"type": action.type}
        if action.assignee_id is not None:
            operation["assignee_id"] = str(action.assignee_id)
        caps = context.capabilities
        command = {
            "schema_version": 1,
            "item_id": str(action.item_id),
            "client_action_id": str(action.client_action_id),
            "expected_version": action.expected_version,
            "action": operation,
            "permissions": {"read": READ_CAPABILITY in caps, "act": APPLY_CAPABILITY in caps,
                            "supervise": SUPERVISE_CAPABILITY in caps},
            "attribution": attribution,
        }

        async def body(handle: Any, mark: Callable[[], None]) -> ActionResult:
            prepared = await fetch_rows(handle, K_PREPARE, {"command": Jsonb(command)},
                                        _action_error)
            _decision(prepared, action, actor)  # REFUSED raises; NEW / REPLAY continue
            applied = await fetch_rows(handle, K_APPLY, {"command": Jsonb(command)},
                                       _action_error)
            result = _applied(applied, action, actor)
            mark()
            return result

        return await run_with_marker(
            self._gateway, context, body,
            lambda: ActionCommitUnknown(action.item_id, action.client_action_id),
        )

    async def _read(self, context: Any, key: str, params: dict[str, Any],
                    bind: Callable[[list[Any]], dict[str, Any]]) -> dict[str, Any]:
        require_capabilities(context, READ_ROUTE)
        attribution = actor_attribution(context)
        if attribution is None:
            raise ReadRefused("NOT_AUTHORIZED")

        async def callback(handle: Any) -> dict[str, Any]:
            await check_reader(handle, attribution["principal_id"])
            return bind(await fetch_rows(handle, key, params, read_error))

        result: dict[str, Any] = await self._gateway.with_tenant_transaction(context, callback)
        return result

    async def get(self, context: Any, item_id: UUID) -> Mapping[str, Any]:
        if not is_uuid(item_id):
            raise CommandMalformed("item_id")
        return await self._read(context, K_GET, {"item_id": item_id},
                                lambda rows: _bind_item(rows, item_id))

    async def history(self, context: Any, item_id: UUID) -> Mapping[str, Any]:
        if not is_uuid(item_id):
            raise CommandMalformed("item_id")
        return await self._read(context, K_HISTORY, {"item_id": item_id},
                                lambda rows: _bind_history(rows, item_id))

    async def list_items(self, context: Any, query: ListQuery) -> Mapping[str, Any]:
        if type(query) is not ListQuery:
            raise CommandMalformed("query")
        return await self._read(context, K_LIST, {"query": Jsonb(query.render())},
                                lambda rows: _bind_list(rows, query))

    async def _resolve(self, context: Any, action: ResolveAction) -> ActionResult:
        """Package v2 2.2: one gateway transaction, at most two prepares; the NEW path
        alone touches the current keys, encrypts and appends."""

        require_capabilities(context, RESOLVE_ROUTE)
        attribution = actor_attribution(context)
        if attribution is None:
            raise ActionRefused("NOT_AUTHORIZED")
        if self._foundation is None:
            raise ActionFailed("ACTION_UNAVAILABLE")  # no resolution append available
        actor = attribution["principal_id"]
        tenant = context.tenant_id
        caps = context.capabilities
        base = {
            "schema_version": 1,
            "item_id": str(action.item_id),
            "client_action_id": str(action.client_action_id),
            "expected_version": action.expected_version,
            "permissions": {"read": READ_CAPABILITY in caps, "act": APPLY_CAPABILITY in caps,
                            "supervise": SUPERVISE_CAPABILITY in caps},
            "attribution": attribution,
        }

        def command(operation: dict[str, Any]) -> Jsonb:
            return Jsonb({**base, "action": {"type": action.type,
                                             "outcome_code": action.outcome_code,
                                             **operation}})

        async def replay(handle: Any, note: dict[str, Any] | None) -> ActionResult:
            rows = await fetch_rows(handle, K_APPLY, {"command": command(
                {"note": note, "payload": None})}, _action_error)
            return _resolve_applied(rows, action, actor, "replayed")

        async def body(binding: Any, mark: Callable[[], None]) -> ActionResult:
            handle = binding.handle
            first = await fetch_rows(handle, K_PREPARE, {"command": command(
                {"note_present": action.note is not None})}, _action_error)
            decision, detail = _resolve_decision(first, action, actor, second=False)
            if decision == "REPLAY":
                result = await replay(handle, None)
            elif decision == "REPLAY_CHALLENGE":
                key = self._key(lambda: self._keys.hmac_key(detail),
                                lambda: ActionRefused("REPLAY_UNVERIFIABLE"))
                note = {"note_hmac": note_hmac(key, tenant, action.note or ""),
                        "hmac_key_version": detail}
                second = await fetch_rows(handle, K_PREPARE, {"command": command(
                    {"note_present": True, "note": note})}, _action_error)
                _resolve_decision(second, action, actor, second=True)
                result = await replay(handle, note)
            else:
                result = await self._resolve_new(binding, handle, action, actor, tenant,
                                                  context, detail, command)
            mark()
            return result

        state = {"complete": False}

        def mark() -> None:
            state["complete"] = True

        async def inner(binding: Any) -> ActionResult:
            return await body(binding, mark)

        try:
            result: ActionResult = await self._foundation.run_in_transaction(context, inner)
            return result
        except (Exception, asyncio.CancelledError):
            if not state["complete"]:
                raise
        raise ActionCommitUnknown(action.item_id, action.client_action_id)

    def _key(self, fetch: Callable[[], Any], failure: Callable[[], Exception]) -> Any:
        """A key-provider call; any failure becomes `failure()` with no chain."""

        if self._keys is None:
            raise failure()
        error: Exception | None = None
        try:
            return fetch()
        except KeyUnavailable:
            error = failure()
        except Exception:  # noqa: BLE001 - a provider fault is unavailability too
            error = failure()
        raise error

    async def _resolve_new(self, binding: Any, handle: Any, action: ResolveAction, actor: str,
                           tenant: str, context: Any, item: dict[str, Any],
                           command: Callable[[dict[str, Any]], Jsonb]) -> ActionResult:
        note: dict[str, Any] | None = None
        note_row: dict[str, Any] | None = None
        if action.note is not None:
            def unavailable() -> Exception:
                return ActionFailed("KEY_UNAVAILABLE")

            version, mac_key = self._key(lambda: self._keys.current_hmac(), unavailable)
            enc_version, enc_key = self._key(lambda: self._keys.current_encryption(),
                                             unavailable)
            note = {"note_hmac": note_hmac(mac_key, tenant, action.note),
                    "hmac_key_version": version}
            sealed = encrypt_note(enc_key, enc_version, tenant, action.item_id,
                                  action.client_action_id, action.note)
            note_row = {"note_id": str(sealed.note_id), "nonce": sealed.nonce.hex(),
                        "ciphertext": sealed.ciphertext.hex(),
                        "enc_key_version": sealed.enc_key_version}
        event_id = uuid.uuid4()
        observation = Observation(
            event_id=event_id,
            event_type=RESOLUTION_TYPE,
            contract_version=1,
            correlation_id=context.correlation_id,
            producer_dedupe_key=_resolution_dedupe_key(tenant, action),
            links=(Link(ITEM_LINK_TYPE, action.item_id, "case"),),
            metadata={"outcome_code": action.outcome_code, "assertion_kind": "staff",
                      "item_kind": item["item_kind"]},
            patient_id=UUID(item["patient_id"]),
            causation_event_id=UUID(item["source_event_id"]),
            occurred_at=None,
            source_time_quality="absent",
        )
        appended = await self._foundation.append_observation_in(binding, observation)
        if appended.appended_or_existing != "appended":
            # A resolution event with this action's dedupe key exists but no action
            # record does: never adopt it. Structural line only (no ids, no note).
            _LOG.warning("m05 resolve refused: orphan resolution event")
            raise ActionFailed("ORPHAN_RESOLUTION_EVENT")
        if appended.event_id != event_id:
            raise _invalid()
        rows = await fetch_rows(handle, K_APPLY, {"command": command(
            {"note": note, "payload": {"event_id": str(event_id), "note_row": note_row}})},
            _action_error)
        return _resolve_applied(rows, action, actor, "applied")

    async def read_note(self, context: Any, note_id: UUID) -> Mapping[str, Any]:
        """Package v2 4 + O-1: route, reader standing, the envelope, the key for its
        version, AEAD with the rebuilt AAD, then the D11 re-check. No plaintext in
        any exception or log."""

        if not is_uuid(note_id):
            raise CommandMalformed("note_id")
        require_capabilities(context, NOTE_READ_ROUTE)
        attribution = actor_attribution(context)
        if attribution is None:
            raise ReadRefused("NOT_AUTHORIZED")
        tenant = context.tenant_id

        async def callback(handle: Any) -> dict[str, Any]:
            await check_reader(handle, attribution["principal_id"])
            envelope = _bind_note(
                await fetch_rows(handle, K_NOTE, {"note_id": note_id}, read_error), note_id)
            key = self._key(lambda: self._keys.encryption_key(envelope["enc_key_version"]),
                            lambda: ReadFailed("NOTE_KEY_UNAVAILABLE"))
            sealed = EncryptedNote(
                note_id=UUID(envelope["note_id"]), nonce=bytes.fromhex(envelope["nonce"]),
                ciphertext=bytes.fromhex(envelope["ciphertext"]),
                enc_key_version=envelope["enc_key_version"])
            failure: ReadFailed | None = None
            try:
                text = decrypt_note(key, tenant, UUID(envelope["item_id"]),
                                    UUID(envelope["client_action_id"]), sealed)
            except NoteIntegrityFailed:
                failure = ReadFailed("NOTE_INTEGRITY_FAILED")
            if failure is not None:
                raise failure
            return {"note_id": envelope["note_id"], "item_id": envelope["item_id"],
                    "text": text}

        result: dict[str, Any] = await self._gateway.with_tenant_transaction(context, callback)
        return result


def _resolution_dedupe_key(tenant: str, action: ResolveAction) -> str:
    """MK-V1: m05r1: + sha256 of the compact JSON preimage, hex."""

    import hashlib
    import json

    preimage = json.dumps(["m05.resolve/v1", tenant, str(action.item_id),
                           str(action.client_action_id)], separators=(",", ":"),
                          ensure_ascii=False)
    return "m05r1:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def _resolve_applied(rows: list[Any], action: ResolveAction, actor: str,
                     expected: str) -> ActionResult:
    value = single_json(rows)
    if (
        type(value) is not dict or set(value) != {"schema_version", "outcome", "snapshot"}
        or not exact_int(value["schema_version"]) or value["schema_version"] != 1
        or value["outcome"] != expected
    ):
        raise _invalid()
    return ActionResult(expected, _bound_resolve_snapshot(value["snapshot"], action, actor))
