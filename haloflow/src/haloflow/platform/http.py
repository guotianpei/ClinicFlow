"""HTTP boundary literals and the outermost gate (L-1W W4a, R-W4-2, A8).

``BODIES`` is the closed literal of fixed refusal bodies. ``Gate`` is a pure ASGI
middleware, outermost on the app: it refuses every HTTP request with 503 until the
platform services are published (also under ``--lifespan off`` and from the moment
shutdown begins), then refuses any tenant-selection header or query key with 400,
before authentication. No input value is echoed.

W4b (interface pins v1 1.6, v1r1): ``BODIES`` gains FORBIDDEN (403) and NOT_FOUND
(404); ``error_response`` is the closed status map for every exception a tenant
route can see. Codes in a body come only from the closed sets below, never from an
exception message; anything unlisted is 503 UNAVAILABLE (a failure) or 500 INTERNAL.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from haloflow.m01.errors import M01Error
from haloflow.m01.staff.service import (
    ADMIN_FAILURES,
    ADMIN_REFUSALS,
    AdminCommitUnknown,
    AdminFailed,
    AdminRefused,
    CommandMalformed,
    ReadFailed,
    ReadRefused,
    RouteDenied,
)
from haloflow.m05.service import (
    ACTION_FAILURES,
    ACTION_REFUSALS,
    ActionCommitUnknown,
    ActionFailed,
    ActionRefused,
)
from haloflow.startup import TenantNotServed

LOGGER: Final = logging.getLogger("haloflow.platform")

BODIES: Final[Mapping[str, tuple[int, dict[str, str]]]] = {
    "NOT_READY": (503, {"code": "NOT_READY"}),
    "UNAUTHENTICATED": (401, {"code": "UNAUTHENTICATED"}),
    "REAUTHENTICATION_REQUIRED": (401, {"code": "REAUTHENTICATION_REQUIRED"}),
    "UNAVAILABLE": (503, {"code": "UNAVAILABLE"}),
    "TENANT_INPUT_REFUSED": (400, {"code": "TENANT_INPUT_REFUSED"}),
    "INTERNAL": (500, {"code": "INTERNAL"}),
    "FORBIDDEN": (403, {"code": "FORBIDDEN"}),
    "NOT_FOUND": (404, {"code": "NOT_FOUND"}),
}
# 409 domain refusals: the closed refusal sets only (pins v1 1.6). NOT_AUTHORIZED is
# 403 (Q-B2) and ITEM_NOT_FOUND 404; both are decided before this set is consulted.
REFUSAL_CODES: Final = ACTION_REFUSALS | ADMIN_REFUSALS
# 503 failures: the existing failure literals plus the five named in pins v1 1.6.
FAILURE_CODES: Final = ACTION_FAILURES | ADMIN_FAILURES | frozenset({
    "TENANT_CONTEXT_INVALID", "LOCK_TIMEOUT", "ADMIN_UNAVAILABLE", "READ_UNAVAILABLE",
    "ACTION_RESULT_INVALID", "ORPHAN_RESOLUTION_EVENT", "NOTE_INTEGRITY_FAILED",
    "KEY_UNAVAILABLE", "NOTE_KEY_UNAVAILABLE", "READ_RESULT_INVALID", "ADMIN_RESULT_INVALID",
    "ACTION_UNAVAILABLE",
})
_BEARER: Final = {"WWW-Authenticate": "Bearer"}
TENANT_HEADERS: Final = frozenset({b"x-tenant-id", b"x-haloflow-tenant", b"x-tenant", b"tenant"})
TENANT_QUERY_KEYS: Final = frozenset({"tenant", "tenant_id", "tenant_hint", "clinic",
                                      "clinic_id"})


def body(key: str) -> JSONResponse:
    status, content = BODIES[key]
    headers = _BEARER if status == 401 else None
    return JSONResponse(content, status_code=status, headers=headers)


def malformed(field: str) -> JSONResponse:
    return JSONResponse({"code": "MALFORMED", "field": field}, status_code=422)


def error_response(error: BaseException) -> JSONResponse:
    """The closed status map (pins v1 1.6, v1r1 clarifications)."""

    if isinstance(error, RouteDenied):
        return body("FORBIDDEN")
    if isinstance(error, (ReadRefused, ActionRefused, AdminRefused)):
        code = error.code
        if code == "NOT_AUTHORIZED":
            return body("FORBIDDEN")
        if code == "ITEM_NOT_FOUND":
            return body("NOT_FOUND")
        if type(code) is str and code in REFUSAL_CODES:
            return JSONResponse({"code": code}, status_code=409)
        return body("INTERNAL")
    if isinstance(error, CommandMalformed):
        return malformed(error.field)
    if isinstance(error, (ReadFailed, ActionFailed, AdminFailed)):
        reason = error.reason_code
        if type(reason) is str and reason in FAILURE_CODES:
            return JSONResponse({"code": reason}, status_code=503)
        return body("UNAVAILABLE")
    if isinstance(error, (ActionCommitUnknown, AdminCommitUnknown)):
        return JSONResponse({"code": "COMMIT_UNKNOWN",
                             "client_action_id": str(error.client_action_id)},
                            status_code=409)
    if isinstance(error, (M01Error, TenantNotServed)):
        return body("UNAVAILABLE")
    return body("INTERNAL")


def _tenant_input(scope: Scope) -> bool:
    for name, _value in scope.get("headers", ()):
        if bytes(name).lower() in TENANT_HEADERS:
            return True
    query = scope.get("query_string", b"").decode("latin-1")
    return any(key.lower() in TENANT_QUERY_KEYS
               for key, _ in parse_qsl(query, keep_blank_values=True))


class Gate:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state: Any = scope["app"].state
        if getattr(state, "platform", None) is None:
            await body("NOT_READY")(scope, receive, send)
            return
        if _tenant_input(scope):
            await body("TENANT_INPUT_REFUSED")(scope, receive, send)
            return
        await self.app(scope, receive, send)
