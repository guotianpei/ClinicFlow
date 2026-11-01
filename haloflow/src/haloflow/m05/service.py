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
    "seq", "action", "actor_id", "before", "after", "assertion_kind", "created_at",
})
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
NOTE_READ_ROUTE: Final = frozenset({NOTE_READ_CAPABILITY})


@dataclass(frozen=True)
class ResolveAction:
    """B4 (package v2 1.1). RED TRANCHE: not yet validated (D11 note normalization,
    outcome set and `other` requires a note arrive with the implementation)."""

    item_id: UUID
    client_action_id: UUID
    expected_version: int
    type: str
    outcome_code: str
    note: str | None = None


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
        raise NotImplementedError("B4 resolve route")

    async def read_note(self, context: Any, note_id: UUID) -> Mapping[str, Any]:
        raise NotImplementedError("B4 note read route")
