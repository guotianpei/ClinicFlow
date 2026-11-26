"""Incoming fax routing FX-IN, package FX3a: the synthetic fax staff JSON routes. SYNTHETIC ONLY.

Five routes under `/synthetic/v1/fax`, in the B3b `staff_routes` pattern:

    GET  /faxes?queue=needs_sorting|<queue uuid>   list     (route requirement `list`)
    GET  /faxes/{fax_id}                            get      (`get`)
    GET  /faxes/{fax_id}/history                    history  (`history`)
    POST /faxes/{fax_id}/open        body {}        open     (`get`: OPEN_KEY is fixed to open)
    POST /faxes/{fax_id}/actions     body {"action": move|file|sort, "expected_version",
                                           "queue_id" (move / sort only)}   act (`act`)

Every request: strict parsing (422 MALFORMED {field}); the production two-step resolution for
the install's ONE fixed principal (`lifecycle.services`: B2's staff member, the install's
tenant): resolve with {SELF_STANDING}, `staff.self_standing`, `capabilities_for(standing)`,
`route_capabilities(route, held)` (None is 403 FORBIDDEN), resolve with that bounded set; then
exactly one `services.fax` (FaxService) call. No request input selects the tenant, the
principal or a capability. Refusals: FAX_NOT_FOUND / QUEUE_NOT_FOUND -> 404 NOT_FOUND; the
state codes (VERSION_CONFLICT, FAX_FILED, NOT_ROUTED, ALREADY_SORTED, NOT_NEEDS_SORTING) ->
409 CONFLICT {"code"}; ACTOR_INACTIVE -> 403 FORBIDDEN; COMMAND_MALFORMED -> 422 MALFORMED;
anything else through production's `error_response`. Every body is labelled SIMULATED. CSRF
and the local exposure gate are the app's (`gate`). There is no settings, rule or line route.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from haloflow.m05.fax import FaxAction, FaxRefused
from haloflow.platform.capabilities import SELF_STANDING, capabilities_for, route_capabilities
from haloflow.platform.http import body, error_response
from haloflow.synthetic.labels import internal, labelled, relabel
from haloflow.synthetic.staff_routes import _resolve

PREFIX: Final = "/synthetic/v1/fax"
NEEDS_SORTING: Final = "needs_sorting"
MAX_BODY: Final = 4096
NOT_FOUND_CODES: Final = frozenset({"FAX_NOT_FOUND", "QUEUE_NOT_FOUND"})
CONFLICT_CODES: Final = frozenset({"VERSION_CONFLICT", "FAX_FILED", "NOT_ROUTED",
                                   "ALREADY_SORTED", "NOT_NEEDS_SORTING"})
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# route name -> (platform route requirement, method, path)
ROUTES: Final[dict[str, tuple[str, str, str]]] = {
    # FX5 (OD-FX11): every fax read / open needs read AND act (`fax_read`)
    "list": ("fax_read", "GET", PREFIX + "/faxes"),
    "get": ("fax_read", "GET", PREFIX + "/faxes/{fax_id}"),
    "history": ("fax_read", "GET", PREFIX + "/faxes/{fax_id}/history"),
    "open": ("fax_read", "POST", PREFIX + "/faxes/{fax_id}/open"),
    "act": ("act", "POST", PREFIX + "/faxes/{fax_id}/actions"),
    "queues": ("fax_read", "GET", PREFIX + "/queues"),  # FX4 (t145; FX5 t151)
}

Call = Callable[[Any, Any], Awaitable[Any]]


class _Malformed(Exception):
    def __init__(self, field: str) -> None:
        super().__init__("malformed")
        self.field = field


def _malformed(field: str) -> Response:
    return labelled({"code": "MALFORMED", "field": field}, 422)


def _fax_id(request: Request) -> uuid.UUID:
    value = request.path_params.get("fax_id")
    if type(value) is not str or not _UUID.fullmatch(value):
        raise _Malformed("fax_id")
    return uuid.UUID(value)


def _no_query(request: Request) -> None:
    if request.scope.get("query_string", b""):
        raise _Malformed("query")


async def _object(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_BODY - len(raw):
            raise _Malformed("body")
        raw += chunk
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Malformed("body") from None
    if type(value) is not dict:
        raise _Malformed("body")
    return value


def _queue_param(request: Request) -> uuid.UUID | None:
    items = list(request.query_params.multi_items())
    if len(items) != 1 or items[0][0] != "queue":
        raise _Malformed("queue")
    value = items[0][1]
    if value == NEEDS_SORTING:
        return None
    if not _UUID.fullmatch(value):
        raise _Malformed("queue")
    return uuid.UUID(value)


def _action(fax_id: uuid.UUID, value: dict[str, Any]) -> FaxAction:
    action = value.get("action")
    if action not in ("move", "file", "sort"):
        raise _Malformed("action")
    allowed = {"action", "expected_version"} | ({"queue_id"} if action != "file" else set())
    if set(value) - allowed:
        raise _Malformed("unknown_field")
    version = value.get("expected_version")
    if type(version) is not int or not 1 <= version <= 999_999_999:
        raise _Malformed("expected_version")
    queue: uuid.UUID | None = None
    if action != "file":
        raw = value.get("queue_id")
        if type(raw) is not str or not _UUID.fullmatch(raw):
            raise _Malformed("queue_id")
        queue = uuid.UUID(raw)
    return FaxAction(action, fax_id, version, queue)


async def _parse(name: str, request: Request) -> Call:
    if name == "queues":
        _no_query(request)
        return lambda fax, ctx: fax.queues(ctx)
    if name == "list":
        queue = _queue_param(request)
        return lambda fax, ctx: fax.list(ctx, queue)
    fax_id = _fax_id(request)
    _no_query(request)
    if name == "get":
        return lambda fax, ctx: fax.get(ctx, fax_id)
    if name == "history":
        return lambda fax, ctx: fax.history(ctx, fax_id)
    value = await _object(request)
    if name == "open":
        if value:
            raise _Malformed("unknown_field")
        return lambda fax, ctx: fax.open(ctx, fax_id)
    action = _action(fax_id, value)
    return lambda fax, ctx: fax.act(ctx, action)


def _failure(error: BaseException) -> Response:
    if isinstance(error, _Malformed):
        return _malformed(error.field)
    if isinstance(error, FaxRefused):
        if error.code in NOT_FOUND_CODES:
            return relabel(body("NOT_FOUND"))
        if error.code in CONFLICT_CODES:
            return labelled({"code": error.code}, 409)
        if error.code == "ACTOR_INACTIVE":
            return relabel(body("FORBIDDEN"))
        if error.code == "COMMAND_MALFORMED":
            return _malformed("body")
        return relabel(body("UNAVAILABLE"))
    try:
        return relabel(error_response(error))
    except Exception:  # noqa: BLE001
        return internal()


async def serve(lifecycle: Any, name: str, request: Request) -> Response:
    services = lifecycle.services
    if services is None or getattr(services, "fax", None) is None:
        return relabel(body("UNAVAILABLE"))
    try:
        call = await _parse(name, request)
        execution_id, correlation_id = uuid.uuid4(), uuid.uuid4()
        own = frozenset({SELF_STANDING})
        context = await _resolve(services, own, own, execution_id, correlation_id)
        held = capabilities_for(await services.staff.self_standing(context))
        requested = route_capabilities(ROUTES[name][0], held)
        if requested is None:
            return relabel(body("FORBIDDEN"))
        context = await _resolve(services, held, requested, execution_id, correlation_id)
        value = await call(services.fax, context)
    except Exception as error:  # noqa: BLE001 - the closed status map above
        return _failure(error)
    try:
        return labelled(dict(value))
    except Exception as error:  # noqa: BLE001
        return _failure(error)


def install_fax_staff_routes(app: FastAPI, lifecycle: Any) -> None:
    for name, (_route, method, path) in ROUTES.items():
        app.add_api_route(path, _endpoint(lifecycle, name), methods=[method],
                          response_class=Response, include_in_schema=False)


def _endpoint(lifecycle: Any, name: str) -> Callable[[Request], Awaitable[Response]]:
    """The route name is bound by closure (never a parameter FastAPI could fill from input)."""
    async def endpoint(request: Request) -> Response:
        return await serve(lifecycle, name, request)

    return endpoint


__all__ = ["PREFIX", "ROUTES", "install_fax_staff_routes"]
