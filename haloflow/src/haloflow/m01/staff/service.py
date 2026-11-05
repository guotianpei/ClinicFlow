"""B3b staff-directory service over the M01 gateway (package v2 + v2r1, Claude/Codex
aligned 2026-11-01; red gate PASS at 537c1b0).

- Public commands are frozen dataclasses validated on construction with exact types
  (`CommandMalformed(field)`); nothing trusted comes from the caller.
- Attribution comes from the TenantContext only (the M02 rule); the principal must be
  an actor with a canonical lowercase UUID id and a valid auth_method, else the route
  is refused before any gateway call.
- Each route checks its complete required capabilities before the gateway
  (`RouteDenied`); the gateway still checks every statement.
- Every statement is read with `fetch_all`; results are validated and bound to the
  request inside the transaction, before the marker, so an invalid output rolls back.
- After the marker any failure is `AdminCommitUnknown` (the M02 precedent); before it,
  database errors map to fixed codes and every other exception propagates unchanged.

Errors shared with the M05 service live here (M05 depends on M01, never the reverse).
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, TypeVar
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

T = TypeVar("T")

READ_CAPABILITY: Final = "m05.followup.read"
STAFF_ADMIN_CAPABILITY: Final = "m01.staff.admin"
STAFF_DISPLAY_CAPABILITY: Final = "m01.staff.display"

K_ADMIN: Final = "m01.staff.admin"
K_DISPLAY: Final = "m01.staff.display"
K_READER: Final = "m01.staff.followup_reader"
K_SELF_STANDING: Final = "m01.staff.self_standing"
SELF_STANDING_CAPABILITY: Final = "m01.staff.self_standing"

UUID_TEXT: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
AUTH_METHOD: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TIMESTAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
OPERATIONS: Final = ("add", "rename", "deactivate", "reactivate", "grant", "revoke",
                     "grant_role", "revoke_role", "set_auth_setting")
# L-1W W4b (interface pins v1 1.3, addendum 1 Q-3): the role and setting payloads,
# validated as the t029 / 008 rules do (ASCII digits only; 5-60 minutes).
ROLES: Final = ("supervisor", "clinic_admin")
AUTH_SETTINGS: Final = ("idle_timeout_minutes", "mfa_required")
_IDLE_VALUE: Final = re.compile(r"[0-9]{1,2}")
STANDING_STATUSES: Final = ("active", "inactive", "unknown")
# L-1W W3a: `m01.staff.admin` reaches m01_staff_admin_v2, which names reactivation
# `activate` and adds "outcome" (applied | replayed) to the v1 four-key result. The
# public command and AdminResult keep the v1 names and shape.
_V2_OPERATION: Final = {"reactivate": "activate"}
_V2_OUTCOMES: Final = ("applied", "replayed")
_WS: Final = frozenset(
    [chr(c) for c in range(0x09, 0x0E)] + [" ", "\u0085", " ", " "]
    + [chr(c) for c in range(0x2000, 0x200B)] + [" ", " ", " ", " ", "　"]
)
_FORBIDDEN: Final = re.compile("[\u0001-\u001f\u007f-\u009f  ]")

ADMIN_REFUSALS: Final = frozenset({
    "COMMAND_MALFORMED", "NOT_AUTHORIZED", "PRECONDITION_FAILED", "ACTION_KEY_CONFLICT",
    "ACTION_KEY_ACTOR_MISMATCH",
})
ADMIN_FAILURES: Final = frozenset({
    "ISOLATION_UNSUPPORTED", "COMPOSITION_UNSUPPORTED", "ADMIN_LOG_INTEGRITY",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RouteDenied(Exception):
    """The context lacks a capability the route needs; no gateway call was made."""

    def __init__(self, reason_code: str = "CAPABILITY_DENIED") -> None:
        super().__init__("route denied")
        self.reason_code = reason_code


class CommandMalformed(ValueError):
    """A public command value is malformed; raised before any gateway call."""

    def __init__(self, field: str) -> None:
        super().__init__("command malformed")
        self.field = field


class ReadRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("read refused")
        self.code = code


class ReadFailed(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__("read failed")
        self.reason_code = reason_code


class AdminRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("staff administration refused")
        self.code = code


class AdminFailed(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__("staff administration failed")
        self.reason_code = reason_code


class AdminCommitUnknown(Exception):
    """The outcome is unknown after the body completed. Recovery: retry the identical
    StaffAdminCommand (same client_action_id); it replays if the first attempt
    committed and applies once if it did not."""

    def __init__(self, staff_id: UUID, client_action_id: UUID, operation: str) -> None:
        super().__init__("staff administration outcome unknown")
        self.staff_id = staff_id
        self.client_action_id = client_action_id
        self.operation = operation


# ---------------------------------------------------------------------------
# Shared helpers (also used by haloflow.m05.service)
# ---------------------------------------------------------------------------


def is_uuid(value: Any) -> bool:
    return isinstance(value, UUID)


def exact_int(value: Any) -> bool:
    return type(value) is int


def member(value: Any, allowed: Any) -> bool:
    """Exact-str enum membership for DECODED values (Codex b00b172 R1): a JSON list
    or object must be invalid, never a TypeError from hashing it."""

    return type(value) is str and value in allowed


def require_capabilities(context: Any, required: frozenset[str]) -> None:
    if not required <= context.capabilities:
        raise RouteDenied()


def actor_attribution(context: Any) -> dict[str, Any] | None:
    """Context-only attribution (the M02 rule), or None when the principal is not an
    actor with a canonical lowercase UUID id and a valid auth_method."""

    principal = context.principal
    if principal.kind.value != "actor":
        return None
    if type(principal.id) is not str or not UUID_TEXT.fullmatch(principal.id):
        return None
    if type(principal.auth_method) is not str or not AUTH_METHOD.fullmatch(
        principal.auth_method
    ):
        return None
    return {
        "principal_kind": principal.kind.value,
        "principal_id": principal.id,
        "auth_method": principal.auth_method,
        "execution_id": str(context.execution_id),
        "request_id": context.request_id,
        "purpose_code": context.purpose,
    }


def sqlstate_and_message(error: psycopg.Error) -> tuple[str | None, str | None]:
    sqlstate = getattr(error, "sqlstate", None)
    message = getattr(getattr(error, "diag", None), "message_primary", None)
    return sqlstate, message if isinstance(message, str) else None


async def fetch_rows(
    handle: Any, key: str, params: dict[str, Any], classify: Callable[[psycopg.Error], Exception]
) -> list[Any]:
    """fetch_all; a database error becomes the classified exception, raised outside
    the handler so no driver exception is attached as cause or context."""

    failure: Exception | None = None
    try:
        return list(await handle.fetch_all(key, params))
    except psycopg.Error as error:
        failure = classify(error)
    raise failure


def single_json(rows: list[Any]) -> Any:
    """The one value of exactly one row with exactly one column, else a sentinel."""

    if len(rows) != 1 or len(rows[0]) != 1:
        return _INVALID
    return rows[0][0]


_INVALID: Final = object()


def valid_timestamp(value: Any) -> bool:
    if type(value) is not str or not TIMESTAMP.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") == value


def valid_display_name(value: Any) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 80
        and unicodedata.is_normalized("NFC", value)
        and not _FORBIDDEN.search(value)
        and value[0] not in _WS
        and value[-1] not in _WS
    )


def uuid_text(value: Any) -> bool:
    return type(value) is str and bool(UUID_TEXT.fullmatch(value))


async def run_with_marker(
    gateway: Any,
    context: Any,
    body: Callable[[Any, Callable[[], None]], Awaitable[T]],
    unknown: Callable[[], Exception],
) -> T:
    """The M02 marker pattern: `body(handle, mark)` calls `mark()` as its final act.
    Before it, exceptions propagate unchanged; after it, any Exception or
    cancellation becomes `unknown()`, raised outside the handler."""

    state = {"complete": False}

    def mark() -> None:
        state["complete"] = True

    async def callback(handle: Any) -> T:
        return await body(handle, mark)

    try:
        return await gateway.with_tenant_transaction(context, callback)  # type: ignore[no-any-return]
    except (Exception, asyncio.CancelledError):
        if not state["complete"]:
            raise
    raise unknown()


# ---------------------------------------------------------------------------
# Public commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaffAdminCommand:
    client_action_id: UUID
    operation: str
    staff_id: UUID
    display_name: str | None = None
    access: str | None = None
    role: str | None = None
    setting: str | None = None
    value: str | None = None

    def __post_init__(self) -> None:
        if not is_uuid(self.client_action_id):
            raise CommandMalformed("client_action_id")
        if not is_uuid(self.staff_id):
            raise CommandMalformed("staff_id")
        if type(self.operation) is not str or self.operation not in OPERATIONS:
            raise CommandMalformed("operation")
        named = self.operation in ("add", "rename")
        accessed = self.operation in ("grant", "revoke")
        if named != (self.display_name is not None) or (
            named and type(self.display_name) is not str
        ):
            raise CommandMalformed("display_name")
        if accessed != (self.access is not None) or (
            accessed and not member(self.access, ("read", "act"))
        ):
            raise CommandMalformed("access")
        roled = self.operation in ("grant_role", "revoke_role")
        if roled != (self.role is not None) or (roled and not member(self.role, ROLES)):
            raise CommandMalformed("role")
        configured = self.operation == "set_auth_setting"
        if configured != (self.setting is not None) or (
            configured and not member(self.setting, AUTH_SETTINGS)
        ):
            raise CommandMalformed("setting")
        if configured != (self.value is not None) or (
            configured and not _valid_setting_value(self.setting, self.value)
        ):
            raise CommandMalformed("value")


def _valid_setting_value(setting: Any, value: Any) -> bool:
    if type(value) is not str:
        return False
    if setting == "idle_timeout_minutes":
        return bool(_IDLE_VALUE.fullmatch(value)) and 5 <= int(value) <= 60
    return value in ("true", "false")


@dataclass(frozen=True)
class AdminResult:
    """The bound admin result: {schema_version, client_action_id, operation, staff},
    plus the v2 ``outcome`` (applied | replayed; interface pins v1 1.3, Q-B1)."""

    result: Mapping[str, Any]
    outcome: str


def _sorted_unique_subset(value: Any, allowed: tuple[str, ...]) -> bool:
    return (
        type(value) is tuple
        and all(member(v, allowed) for v in value)
        and list(value) == sorted(set(value))
    )


@dataclass(frozen=True)
class Standing:
    """The caller's own standing (t027; interface pins v1 1.2). Closed vocabularies,
    validated on construction; anything else is READ_RESULT_INVALID."""

    status: str
    access: tuple[str, ...]
    roles: tuple[str, ...]

    def __post_init__(self) -> None:
        if not (
            member(self.status, STANDING_STATUSES)
            and _sorted_unique_subset(self.access, ("read", "act"))
            and _sorted_unique_subset(self.roles, ROLES)
        ):
            raise ReadFailed("READ_RESULT_INVALID")


# ---------------------------------------------------------------------------
# Classification and validation
# ---------------------------------------------------------------------------


def _admin_error(error: psycopg.Error) -> Exception:
    sqlstate, message = sqlstate_and_message(error)
    if sqlstate == "P0001" and message in ADMIN_REFUSALS:
        return AdminRefused(message)
    if sqlstate == "P0001" and message in ADMIN_FAILURES:
        return AdminFailed(message)
    if sqlstate == "22023":
        return AdminFailed("TENANT_CONTEXT_INVALID")
    if sqlstate == "55P03":
        return AdminFailed("LOCK_TIMEOUT")
    return AdminFailed("ADMIN_UNAVAILABLE")


def read_error(error: psycopg.Error) -> Exception:
    sqlstate, message = sqlstate_and_message(error)
    if sqlstate == "P0001" and message in ("NOT_AUTHORIZED", "ITEM_NOT_FOUND",
                                           "COMMAND_MALFORMED"):
        return ReadRefused(message)
    if sqlstate == "22023":
        return ReadFailed("TENANT_CONTEXT_INVALID")
    return ReadFailed("READ_UNAVAILABLE")


def _valid_staff_snapshot(snap: Any, command: StaffAdminCommand) -> bool:
    if type(snap) is not dict or set(snap) != {
        "schema_version", "staff_id", "display_name", "status", "status_changed_at", "access",
    }:
        return False
    access = snap["access"]
    if not (
        exact_int(snap["schema_version"]) and snap["schema_version"] == 1
        and snap["staff_id"] == str(command.staff_id)
        and valid_display_name(snap["display_name"])
        and member(snap["status"], ("active", "inactive"))
        and valid_timestamp(snap["status_changed_at"])
        and type(access) is list
        and all(member(a, ("read", "act")) for a in access)
        and access == sorted(set(access))
    ):
        return False
    op = command.operation
    if op in ("add", "rename") and snap["display_name"] != command.display_name:
        return False
    if op == "add" and (snap["status"] != "active" or access != []):
        return False
    if op == "deactivate" and snap["status"] != "inactive":
        return False
    if op == "reactivate" and snap["status"] != "active":
        return False
    if op == "grant" and command.access not in access:
        return False
    return not (op == "revoke" and command.access in access)


def _bind_admin(rows: list[Any], command: StaffAdminCommand) -> dict[str, Any]:
    value = single_json(rows)
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "client_action_id", "operation", "staff", "outcome"}
        or not member(value["outcome"], _V2_OUTCOMES)
        or not exact_int(value["schema_version"]) or value["schema_version"] != 1
        or value["client_action_id"] != str(command.client_action_id)
        or value["operation"] != _V2_OPERATION.get(command.operation, command.operation)
        or not _valid_staff_snapshot(value["staff"], command)
    ):
        raise AdminFailed("ADMIN_RESULT_INVALID")
    return {"schema_version": 1, "client_action_id": value["client_action_id"],
            "operation": command.operation, "staff": value["staff"]}


def _bind_admin_result(rows: list[Any], command: StaffAdminCommand) -> AdminResult:
    """The v1-shaped result plus the v2 outcome (validated by ``_bind_admin``)."""

    bound = _bind_admin(rows, command)
    outcome: str = single_json(rows)["outcome"]
    return AdminResult(bound, outcome)


def _bind_standing(rows: list[Any], staff_id: str) -> Standing:
    value = single_json(rows)
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "staff_id", "status", "access", "roles"}
        or not exact_int(value["schema_version"]) or value["schema_version"] != 1
        or value["staff_id"] != staff_id
        or type(value["access"]) is not list or type(value["roles"]) is not list
    ):
        raise ReadFailed("READ_RESULT_INVALID")
    return Standing(status=value["status"], access=tuple(value["access"]),
                    roles=tuple(value["roles"]))


def _bind_display(rows: list[Any], requested: tuple[UUID, ...]) -> dict[str, Any]:
    value = single_json(rows)
    wanted = {str(u) for u in requested}
    if (
        type(value) is not dict or set(value) != {"schema_version", "staff"}
        or not exact_int(value["schema_version"]) or value["schema_version"] != 1
        or type(value["staff"]) is not list
    ):
        raise ReadFailed("READ_RESULT_INVALID")
    ids: list[str] = []
    for entry in value["staff"]:
        if (
            type(entry) is not dict or set(entry) != {"staff_id", "display_name", "status"}
            or not uuid_text(entry["staff_id"]) or entry["staff_id"] not in wanted
            or not valid_display_name(entry["display_name"])
            or not member(entry["status"], ("active", "inactive"))
        ):
            raise ReadFailed("READ_RESULT_INVALID")
        ids.append(entry["staff_id"])
    if ids != sorted(set(ids)):
        raise ReadFailed("READ_RESULT_INVALID")
    return value


async def check_reader(handle: Any, staff_id: str) -> None:
    """The v2r1 A2 adapter: exactly one row, one column, an exact bool; only True
    authorizes. False -> ReadRefused(NOT_AUTHORIZED); anything else ->
    ReadFailed(READ_RESULT_INVALID). Nothing further runs on refusal or failure."""

    rows = await fetch_rows(handle, K_READER, {"staff_id": UUID(staff_id)}, read_error)
    value = single_json(rows)
    if type(value) is not bool:
        raise ReadFailed("READ_RESULT_INVALID")
    if value is not True:
        raise ReadRefused("NOT_AUTHORIZED")


def _display_ids(staff_ids: Any) -> tuple[UUID, ...]:
    if (
        type(staff_ids) is not tuple
        or not 1 <= len(staff_ids) <= 100
        or not all(is_uuid(u) for u in staff_ids)
        or len(set(staff_ids)) != len(staff_ids)
    ):
        raise CommandMalformed("staff_ids")
    return staff_ids


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StaffService:
    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def admin(self, context: Any, command: StaffAdminCommand) -> AdminResult:
        if type(command) is not StaffAdminCommand:
            raise CommandMalformed("command")
        require_capabilities(context, frozenset({STAFF_ADMIN_CAPABILITY}))
        attribution = actor_attribution(context)
        if attribution is None:
            raise AdminRefused("NOT_AUTHORIZED")
        # AO-05: the clinic setting names the acting admin (t029 rule), refused here
        # before any I/O as well.
        if (command.operation == "set_auth_setting"
                and str(command.staff_id) != attribution["principal_id"]):
            raise CommandMalformed("staff_id")
        sql_command: dict[str, Any] = {
            "schema_version": 1,
            "client_action_id": str(command.client_action_id),
            "operation": _V2_OPERATION.get(command.operation, command.operation),
            "staff_id": str(command.staff_id),
            "permissions": {"staff_admin": STAFF_ADMIN_CAPABILITY in context.capabilities},
            "attribution": attribution,
        }
        if command.display_name is not None:
            sql_command["display_name"] = command.display_name
        if command.access is not None:
            sql_command["access"] = command.access
        if command.role is not None:
            sql_command["role"] = command.role
        if command.setting is not None:
            sql_command["setting"] = command.setting
            sql_command["value"] = command.value

        async def body(handle: Any, mark: Callable[[], None]) -> AdminResult:
            rows = await fetch_rows(handle, K_ADMIN, {"command": Jsonb(sql_command)},
                                    _admin_error)
            result = _bind_admin_result(rows, command)
            mark()
            return result

        return await run_with_marker(
            self._gateway, context, body,
            lambda: AdminCommitUnknown(command.staff_id, command.client_action_id,
                                       command.operation),
        )

    async def self_standing(self, context: Any) -> Standing:
        """The caller's own standing (t027), one gateway transaction. The context
        must carry exactly the self-standing capability (pins v1 1.2: a singleton,
        Codex 20261105T134133Z), and attribution is context-only."""

        if context.capabilities != frozenset({SELF_STANDING_CAPABILITY}):
            raise RouteDenied()
        attribution = actor_attribution(context)
        if attribution is None:
            raise ReadRefused("NOT_AUTHORIZED")

        async def callback(handle: Any) -> Standing:
            rows = await fetch_rows(handle, K_SELF_STANDING,
                                    {"attribution": Jsonb(attribution)}, read_error)
            return _bind_standing(rows, attribution["principal_id"])

        standing: Standing = await self._gateway.with_tenant_transaction(context, callback)
        return standing

    async def display(self, context: Any, staff_ids: tuple[UUID, ...]) -> Mapping[str, Any]:
        requested = _display_ids(staff_ids)
        require_capabilities(context, frozenset({STAFF_DISPLAY_CAPABILITY}))
        via_admin = STAFF_ADMIN_CAPABILITY in context.capabilities
        if not via_admin:
            require_capabilities(context, frozenset({READ_CAPABILITY}))
        attribution = actor_attribution(context)
        if attribution is None:
            raise ReadRefused("NOT_AUTHORIZED")
        query = {"schema_version": 1, "staff_ids": [str(u) for u in requested]}

        async def callback(handle: Any) -> dict[str, Any]:
            if not via_admin:
                await check_reader(handle, attribution["principal_id"])
            rows = await fetch_rows(handle, K_DISPLAY, {"query": Jsonb(query)}, read_error)
            return _bind_display(rows, requested)

        result: dict[str, Any] = await self._gateway.with_tenant_transaction(context, callback)
        return result
