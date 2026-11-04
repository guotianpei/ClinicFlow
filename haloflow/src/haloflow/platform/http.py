"""HTTP boundary literals and the outermost gate (L-1W W4a, R-W4-2, A8).

``BODIES`` is the closed literal of fixed refusal bodies. ``Gate`` is a pure ASGI
middleware, outermost on the app: it refuses every HTTP request with 503 until the
platform services are published (also under ``--lifespan off`` and from the moment
shutdown begins), then refuses any tenant-selection header or query key with 400,
before authentication. No input value is echoed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

BODIES: Final[Mapping[str, tuple[int, dict[str, str]]]] = {
    "NOT_READY": (503, {"code": "NOT_READY"}),
    "UNAUTHENTICATED": (401, {"code": "UNAUTHENTICATED"}),
    "REAUTHENTICATION_REQUIRED": (401, {"code": "REAUTHENTICATION_REQUIRED"}),
    "UNAVAILABLE": (503, {"code": "UNAVAILABLE"}),
    "TENANT_INPUT_REFUSED": (400, {"code": "TENANT_INPUT_REFUSED"}),
    "INTERNAL": (500, {"code": "INTERNAL"}),
}
_BEARER: Final = {"WWW-Authenticate": "Bearer"}
TENANT_HEADERS: Final = frozenset({b"x-tenant-id", b"x-haloflow-tenant", b"x-tenant", b"tenant"})
TENANT_QUERY_KEYS: Final = frozenset({"tenant", "tenant_id", "tenant_hint", "clinic",
                                      "clinic_id"})


def body(key: str) -> JSONResponse:
    status, content = BODIES[key]
    headers = _BEARER if status == 401 else None
    return JSONResponse(content, status_code=status, headers=headers)


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
