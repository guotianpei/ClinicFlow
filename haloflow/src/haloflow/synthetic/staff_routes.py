"""The synthetic staff JSON routes (Option B B3b, R-B3b-1..3). SYNTHETIC ONLY.

Six routes under `/synthetic/v1/followup/items` over the composed synthetic runtime:
list, get, history, reason (S-I `ehr_confirmation_check`), act (StaffAction) and resolve
(ResolveAction). Every request runs, in order:
1. request parsing with the PRODUCTION parsers of the route's analogue (`platform.routes`), so
   request shapes, limits and `422 {"code":"MALFORMED","field":…}` refusals are production's;
2. the production two-step resolution for ONE fixed principal: B2's staff member
   (`kind=ACTOR`, `auth_method="synthetic"`, the install's tenant): resolve with
   `{SELF_STANDING}`, `staff.self_standing`, `capabilities_for(standing)`,
   `route_capabilities(route, held)` (None is 403), resolve with that bounded set;
3. exactly one service call; every exception maps through production's `error_response`, with
   one synthetic-only adapter: the `reason` read's ReadFailed ITEM_NOT_FOUND is 404 NOT_FOUND.
No request input selects the tenant, the principal or a capability. `reason` carries the
`get` capability requirement (it reads the same item). Not production authentication (L-1W).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m01.staff.service import ReadFailed
from haloflow.m05.service import ResolveAction, StaffAction
from haloflow.platform.capabilities import SELF_STANDING, capabilities_for, route_capabilities
from haloflow.platform.http import body, error_response, malformed
from haloflow.platform.routes import (
    ACT_FIELDS,
    RESOLVE_FIELDS,
    _json_object,
    _list_query,
    _Malformed,
    _no_query,
    _path_uuid,
    _resolve_action,
    _staff_action,
)
from haloflow.synthetic.labels import internal, labelled, relabel

PREFIX: Final = "/synthetic/v1/followup/items"
AUTH_METHOD: Final = "synthetic"
PURPOSE: Final = "operations"

Call = Callable[[Any, Any], Awaitable[Any]]


async def _parse(name: str, request: Request) -> Call:
    if name == "list":
        query = _list_query(request)
        return lambda m05, ctx: m05.list_items(ctx, query)
    item_id = _path_uuid(request.path_params["item_id"], "item_id")
    _no_query(request)
    if name in ("get", "history", "reason"):
        return lambda m05, ctx: getattr(m05, name)(ctx, item_id)
    action: StaffAction | ResolveAction
    if name == "act":
        action = _staff_action(item_id, await _json_object(request, ACT_FIELDS))
    else:
        action = _resolve_action(item_id, await _json_object(request, RESOLVE_FIELDS))

    async def act(m05: Any, ctx: Any) -> dict[str, Any]:
        result = await m05.act(ctx, action)
        return {"outcome": result.outcome, "snapshot": dict(result.snapshot)}

    return act


async def _resolve(services: Any, held: frozenset[str], requested: frozenset[str],
                   execution_id: uuid.UUID, correlation_id: uuid.UUID) -> Any:
    principal = Principal(kind=PrincipalKind.ACTOR, id=services.staff_id,
                          auth_method=AUTH_METHOD,
                          authorized_tenant_ids=frozenset({services.tenant_id}),
                          capabilities=held)
    return await services.resolver.resolve(
        principal=principal, tenant_hint=services.tenant_id, purpose=PURPOSE,
        capabilities=requested, source=TrustedSource.HTTP, execution_id=execution_id,
        correlation_id=correlation_id,
        correlation_source=CorrelationSource.ENTRY_POINT_GENERATED)


def _failure(name: str, error: BaseException) -> Response:
    """The one error map for both paths: production's `error_response`, with the single
    synthetic adapter for the S-I reason read (below), all labelled."""
    if isinstance(error, _Malformed):
        return relabel(malformed(error.field))
    if isinstance(error, ReadFailed):
        # The S-I reason read reports an unknown, other-tenant, other-kind or unbound item as
        # ReadFailed ITEM_NOT_FOUND (its pin); production has no reason route, and its map
        # would call that 503 UNAVAILABLE. For `reason` only it is production's 404 NOT_FOUND
        # (as the accepted S-J test router did); everything else is production's map.
        if name == "reason" and getattr(error, "reason_code", None) == "ITEM_NOT_FOUND":
            return relabel(body("NOT_FOUND"))
        return relabel(error_response(error))
    try:
        return relabel(error_response(error))
    except Exception:  # noqa: BLE001
        return internal()


async def execute(lifecycle: Any, name: str, call: Call) -> Any:
    """The one action path shared by the JSON routes and the B4 screen (B4 v1 Q2): the
    production two-step capability resolution for the fixed principal, then exactly one service
    call. Returns the service value, or a labelled refusal Response (production's map)."""
    services = lifecycle.services
    route = "get" if name == "reason" else name
    try:
        if services is None:
            return relabel(body("UNAVAILABLE"))
        execution_id, correlation_id = uuid.uuid4(), uuid.uuid4()
        own = frozenset({SELF_STANDING})
        context = await _resolve(services, own, own, execution_id, correlation_id)
        held = capabilities_for(await services.staff.self_standing(context))
        requested = route_capabilities(route, held)
        if requested is None:
            return relabel(body("FORBIDDEN"))
        context = await _resolve(services, held, requested, execution_id, correlation_id)
        return await call(services.m05, context)
    except Exception as error:  # noqa: BLE001 - production's closed status map
        return _failure(name, error)


async def serve(lifecycle: Any, name: str, request: Request) -> Response:
    if lifecycle.services is None:
        return relabel(body("UNAVAILABLE"))
    try:
        call = await _parse(name, request)
    except Exception as error:  # noqa: BLE001 - production's closed status map
        return _failure(name, error)
    value = await execute(lifecycle, name, call)
    if isinstance(value, Response):
        return value
    try:
        return labelled(dict(value))
    except Exception as error:  # noqa: BLE001
        return _failure(name, error)


ROUTES: Final[dict[str, tuple[str, str]]] = {
    "list": ("GET", PREFIX),
    "get": ("GET", PREFIX + "/{item_id}"),
    "history": ("GET", PREFIX + "/{item_id}/history"),
    "reason": ("GET", PREFIX + "/{item_id}/reason"),
    "act": ("POST", PREFIX + "/{item_id}/actions"),
    "resolve": ("POST", PREFIX + "/{item_id}/resolve"),
}


def install_staff_routes(app: FastAPI, lifecycle: Any) -> None:
    for name, (method, path) in ROUTES.items():
        app.add_api_route(path, _endpoint(lifecycle, name), methods=[method],
                          response_class=Response, include_in_schema=False)


def _endpoint(lifecycle: Any, name: str) -> Callable[[Request], Awaitable[Response]]:
    """The route name is bound by closure (never a parameter FastAPI could fill from input)."""
    async def endpoint(request: Request) -> Response:
        return await serve(lifecycle, name, request)

    return endpoint
