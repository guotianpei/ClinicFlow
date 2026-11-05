"""L-1W W4b: the tenant routes (plan v1 R-W4-4..R-W4-7, addendum 1 pins 3-4,
architecture v2 A5/A8, interface pins v1 1.4-1.6 and v1r1).

Every tenant request runs the same fixed sequence in ``_serve``:

1. admission (``require_admitted``): the tenant and staff id come from admission only;
2. request parsing: path, query and body against closed declarations; a refusal is
   422 ``{"code":"MALFORMED","field":<label>}`` and never reflects input;
3. the self-standing step: resolve #1 with exactly ``{SELF_STANDING}``, then
   ``staff.self_standing``; inactive or unknown standing is 403;
4. the route step: ``route_capabilities(route, capabilities_for(standing))`` (None is
   403), resolve #2 with that bounded set, then exactly one service call.

Both resolves share one fresh ``execution_id`` and one entry-point-generated
correlation id; a client-supplied correlation header is ignored (no trusted proxy is
configured in W4b). Every exception is mapped through ``http.error_response`` (the
closed status map). One INFO line per tenant request on ``haloflow.platform``:
``REQUEST route=<template> status=<int> reason=<code or -> execution_id=<uuid>
correlation_id=<uuid>``; the template is the declared path, never the raw path.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m01.staff.service import (
    OPERATIONS,
    UUID_TEXT,
    CommandMalformed,
    StaffAdminCommand,
    valid_timestamp,
)
from haloflow.m05.service import (
    STATES,
    Cursor,
    ListQuery,
    ResolveAction,
    StaffAction,
)
from haloflow.platform.capabilities import SELF_STANDING, capabilities_for, route_capabilities
from haloflow.platform.http import LOGGER, body, error_response, malformed

MAX_BODY: Final = 16 * 1024
MAX_QUERY_PARAMETERS: Final = 8
PURPOSE: Final = "operations"
AUTH_METHOD: Final = "bearer"
_LIMIT: Final = re.compile(r"[0-9]{1,3}")

# Declared request bodies (closed): key -> required?
ACT_FIELDS: Final = {"client_action_id": True, "expected_version": True, "type": True,
                     "assignee_id": False}
RESOLVE_FIELDS: Final = {"client_action_id": True, "expected_version": True, "type": True,
                         "outcome_code": True, "note": False}
DISPLAY_FIELDS: Final = {"staff_ids": True}
ADMIN_FIELDS: Final = {"client_action_id": True, "operation": True, "staff_id": False,
                       "display_name": False, "access": False, "role": False,
                       "setting": False, "value": False}
LIST_PARAMETERS: Final = ("limit", "states", "assignee", "after_created_at", "after_item_id")


class _Malformed(Exception):
    """A request-boundary refusal; ``field`` is a declared name or a constant."""

    def __init__(self, field: str) -> None:
        super().__init__("request malformed")
        self.field = field


class _Respond(Exception):
    """A fixed response decided before the service call."""

    def __init__(self, response: Response) -> None:
        super().__init__("respond")
        self.response = response


# ---------------------------------------------------------------------------
# Parsing (no input value is ever reflected)
# ---------------------------------------------------------------------------


def _path_uuid(value: str, field: str) -> uuid.UUID:
    if len(value) != 36 or not UUID_TEXT.fullmatch(value):
        raise _Malformed(field)
    return uuid.UUID(value)


def _uuid_field(value: Any, field: str) -> uuid.UUID:
    if type(value) is not str or len(value) != 36 or not UUID_TEXT.fullmatch(value):
        raise _Malformed(field)
    return uuid.UUID(value)


def _no_query(request: Request) -> None:
    if request.scope.get("query_string", b""):
        raise _Malformed("query")


async def _read_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise _Malformed("body")
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_object(request: Request, declared: dict[str, bool]) -> dict[str, Any]:
    raw = await _read_body(request)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Malformed("body") from None
    if type(value) is not dict:
        raise _Malformed("body")
    if any(key not in declared for key in value):
        raise _Malformed("unknown_field")
    for key, required in declared.items():
        if required and key not in value:
            raise _Malformed(key)
    return value


def _command(build: Callable[[], Any]) -> Any:
    failure: _Malformed | None = None
    try:
        return build()
    except CommandMalformed as error:
        failure = _Malformed(error.field)
    raise failure


def _list_query(request: Request) -> ListQuery:
    pairs = parse_qsl(request.scope.get("query_string", b"").decode("latin-1"),
                      keep_blank_values=True)
    keys = [k for k, _ in pairs]
    if (len(pairs) > MAX_QUERY_PARAMETERS or any(k not in LIST_PARAMETERS for k in keys)
            or len(set(keys)) != len(keys)):
        raise _Malformed("query")
    given = dict(pairs)
    limit = given.get("limit")
    if limit is None or not _LIMIT.fullmatch(limit) or not 1 <= int(limit) <= 100:
        raise _Malformed("limit")
    states: tuple[str, ...] | None = None
    if "states" in given:
        states = tuple(given["states"].split(","))
        if not all(s in STATES for s in states) or len(set(states)) != len(states):
            raise _Malformed("states")
    assignee: uuid.UUID | str | None = None
    if "assignee" in given:
        assignee = ("unassigned" if given["assignee"] == "unassigned"
                    else _uuid_field(given["assignee"], "assignee"))
    after: Cursor | None = None
    if "after_created_at" in given or "after_item_id" in given:
        created = given.get("after_created_at")
        if created is None or not valid_timestamp(created):
            raise _Malformed("after_created_at")
        if "after_item_id" not in given:
            raise _Malformed("after_item_id")
        item_id = _uuid_field(given["after_item_id"], "after_item_id")
        moment = datetime.fromisoformat(created.replace("Z", "+00:00"))
        after = _command(lambda: Cursor(created_at=moment, item_id=item_id))
    query: ListQuery = _command(lambda: ListQuery(limit=int(limit), states=states,
                                                  assignee=assignee, after=after))
    return query


def _staff_action(item_id: uuid.UUID, value: dict[str, Any]) -> StaffAction:
    key = _uuid_field(value["client_action_id"], "client_action_id")
    assignee = value.get("assignee_id")
    assignee_id = None if assignee is None else _uuid_field(assignee, "assignee_id")
    action: StaffAction = _command(lambda: StaffAction(
        item_id=item_id, client_action_id=key, expected_version=value["expected_version"],
        type=value["type"], assignee_id=assignee_id))
    return action


def _resolve_action(item_id: uuid.UUID, value: dict[str, Any]) -> ResolveAction:
    key = _uuid_field(value["client_action_id"], "client_action_id")
    note = value.get("note")
    if note is not None and type(note) is not str:
        raise _Malformed("note")
    action: ResolveAction = _command(lambda: ResolveAction(
        item_id=item_id, client_action_id=key, expected_version=value["expected_version"],
        type=value["type"], outcome_code=value["outcome_code"], note=note))
    return action


def _display_ids(value: dict[str, Any]) -> tuple[uuid.UUID, ...]:
    ids = value["staff_ids"]
    if type(ids) is not list or not 1 <= len(ids) <= 100:
        raise _Malformed("staff_ids")
    parsed = tuple(_uuid_field(v, "staff_ids") for v in ids)
    if len(set(parsed)) != len(parsed):
        raise _Malformed("staff_ids")
    return parsed


def _admin_command(value: dict[str, Any], staff_id: uuid.UUID) -> StaffAdminCommand:
    key = _uuid_field(value["client_action_id"], "client_action_id")
    operation = value["operation"]
    if type(operation) is not str or operation not in OPERATIONS:
        raise _Malformed("operation")
    if operation == "set_auth_setting":
        if "staff_id" in value:
            raise _Malformed("staff_id")  # the route names the acting admin
        target = staff_id
    else:
        if "staff_id" not in value:
            raise _Malformed("staff_id")
        target = _uuid_field(value["staff_id"], "staff_id")
    command: StaffAdminCommand = _command(lambda: StaffAdminCommand(
        client_action_id=key, operation=operation, staff_id=target,
        display_name=value.get("display_name"), access=value.get("access"),
        role=value.get("role"), setting=value.get("setting"), value=value.get("value")))
    return command


# ---------------------------------------------------------------------------
# The two-step context and the service call
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Ids:
    execution_id: uuid.UUID
    correlation_id: uuid.UUID


async def _resolve(services: Any, admitted: Any, ids: _Ids, held: frozenset[str],
                   requested: frozenset[str]) -> Any:
    principal = Principal(
        kind=PrincipalKind.ACTOR,
        id=str(admitted.staff_id),
        auth_method=AUTH_METHOD,
        authorized_tenant_ids=frozenset({admitted.tenant_id}),
        capabilities=held,
    )
    return await services.resolver.resolve(
        principal=principal,
        tenant_hint=admitted.tenant_id,
        purpose=PURPOSE,
        capabilities=requested,
        source=TrustedSource.HTTP,
        execution_id=ids.execution_id,
        correlation_id=ids.correlation_id,
        correlation_source=CorrelationSource.ENTRY_POINT_GENERATED,
    )


Call = Callable[[Any, Any], Awaitable[Response]]
Parse = Callable[[Request, Any], Awaitable[Call]]


async def _serve(request: Request, route: str, template: str, parse: Parse) -> Response:
    from haloflow.platform.app import _Refusal, require_admitted

    ids = _Ids(uuid.uuid4(), uuid.uuid4())
    response: Response
    try:
        try:
            admitted = await require_admitted(request)
        except _Refusal as refused:
            raise _Respond(body(refused.key)) from None
        call = await parse(request, admitted)
        services = request.app.state.platform.services
        own = frozenset({SELF_STANDING})
        context = await _resolve(services, admitted, ids, own, own)
        standing = await services.staff.self_standing(context)
        held = capabilities_for(standing)
        requested = route_capabilities(route, held)
        if requested is None:
            raise _Respond(body("FORBIDDEN"))
        context = await _resolve(services, admitted, ids, held, requested)
        response = await call(services, context)
    except _Respond as decided:
        response = decided.response
    except _Malformed as error:
        response = malformed(error.field)
    except Exception as error:  # noqa: BLE001 - mapped through the closed status map
        response = error_response(error)
    reason = "-"
    if response.status_code >= 300:
        try:
            code = json.loads(bytes(response.body))["code"]
        except (ValueError, KeyError, TypeError):
            code = "INTERNAL"
        reason = code if type(code) is str and re.fullmatch(r"[A-Z_]+", code) else "INTERNAL"
    LOGGER.info("REQUEST route=%s status=%d reason=%s execution_id=%s correlation_id=%s",
                template, response.status_code, reason, ids.execution_id, ids.correlation_id)
    return response


def _ok(value: Any) -> JSONResponse:
    return JSONResponse(dict(value))


# ---------------------------------------------------------------------------
# Route parsers (each returns the single service call)
# ---------------------------------------------------------------------------


async def _list(request: Request, _admitted: Any) -> Call:
    query = _list_query(request)

    async def call(services: Any, context: Any) -> Response:
        return _ok(await services.m05.list_items(context, query))
    return call


def _item_read(method: str) -> Parse:
    async def parse(request: Request, _admitted: Any) -> Call:
        item_id = _path_uuid(request.path_params["item_id"], "item_id")
        _no_query(request)

        async def call(services: Any, context: Any) -> Response:
            return _ok(await getattr(services.m05, method)(context, item_id))
        return call
    return parse


async def _act(request: Request, _admitted: Any) -> Call:
    item_id = _path_uuid(request.path_params["item_id"], "item_id")
    _no_query(request)
    action = _staff_action(item_id, await _json_object(request, ACT_FIELDS))

    async def call(services: Any, context: Any) -> Response:
        result = await services.m05.act(context, action)
        return JSONResponse({"outcome": result.outcome, "snapshot": dict(result.snapshot)})
    return call


async def _resolve_route(request: Request, _admitted: Any) -> Call:
    item_id = _path_uuid(request.path_params["item_id"], "item_id")
    _no_query(request)
    action = _resolve_action(item_id, await _json_object(request, RESOLVE_FIELDS))

    async def call(services: Any, context: Any) -> Response:
        result = await services.m05.act(context, action)
        return JSONResponse({"outcome": result.outcome, "snapshot": dict(result.snapshot)})
    return call


async def _read_note(request: Request, _admitted: Any) -> Call:
    note_id = _path_uuid(request.path_params["note_id"], "note_id")
    _no_query(request)

    async def call(services: Any, context: Any) -> Response:
        return _ok(await services.m05.read_note(context, note_id))
    return call


async def _display(request: Request, _admitted: Any) -> Call:
    _no_query(request)
    staff_ids = _display_ids(await _json_object(request, DISPLAY_FIELDS))

    async def call(services: Any, context: Any) -> Response:
        return _ok(await services.staff.display(context, staff_ids))
    return call


async def _admin(request: Request, admitted: Any) -> Call:
    _no_query(request)
    command = _admin_command(await _json_object(request, ADMIN_FIELDS), admitted.staff_id)

    async def call(services: Any, context: Any) -> Response:
        result = await services.staff.admin(context, command)
        return JSONResponse({**dict(result.result), "outcome": result.outcome})
    return call


# route name -> (method, path template, parser)
ROUTES: Final[dict[str, tuple[str, str, Parse]]] = {
    "list": ("GET", "/v1/followup/items", _list),
    "get": ("GET", "/v1/followup/items/{item_id}", _item_read("get")),
    "history": ("GET", "/v1/followup/items/{item_id}/history", _item_read("history")),
    "act": ("POST", "/v1/followup/items/{item_id}/actions", _act),
    "resolve": ("POST", "/v1/followup/items/{item_id}/resolve", _resolve_route),
    "read_note": ("GET", "/v1/followup/notes/{note_id}", _read_note),
    "display": ("POST", "/v1/staff/display", _display),
    "admin": ("POST", "/v1/staff/admin", _admin),
}


def _endpoint(name: str, template: str, parse: Parse) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        return await _serve(request, name, template, parse)

    return handler


def install(app: FastAPI) -> None:
    for name, (method, template, parse) in ROUTES.items():
        app.add_api_route(template, _endpoint(name, template, parse), methods=[method],
                          response_class=Response)
