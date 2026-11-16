"""The local exposure gate of the synthetic app (Option B B3b, R-B3b-4, R-B3b-8). SYNTHETIC ONLY.

One pure ASGI middleware runs before routing for every HTTP request, in this order (a refusal
never echoes a header or body value; every response gets the label header):
1. Host: exactly one `Host` header, equal to an entry of the allowlist built from the bound
   loopback address; otherwise 421 HOST_REFUSED (DNS rebinding, host spoofing).
2. Fetch metadata: `Sec-Fetch-Site` present and neither `same-origin` nor `none` -> 403
   CROSS_SITE_REFUSED.
3. Method: only GET and POST are routed; anything else (OPTIONS included) is 405, with no
   `Access-Control-*` header (no CORS exists).
4. Tenant input: a production tenant header or query key -> production's
   TENANT_INPUT_REFUSED (the principal and tenant are fixed server-side).
5. Every POST: `Origin` exactly once and equal to `http://` + the request's own (validated)
   Host -> else 403 ORIGIN_REFUSED; `Content-Type` exactly application/json (optionally
   `; charset=utf-8`) -> else 415; the CSRF header exactly once and equal to the lifespan's
   token (hmac.compare_digest) -> else 403 CSRF_REFUSED.
6. Admission: every served route except health is counted in flight for its whole handler,
   response body included; once admission is closed it answers 503 with the lifecycle's closed
   code. A path that matches no route goes on to the router's labelled 404.
A fault inside the gate, or an exception escaping a route before its response starts, is
answered 500 INTERNAL (labelled); the fault text never appears.
These are local exposure controls for a loopback-only synthetic app, not production
authentication (L-1W hold).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Iterable
from typing import Any, Final
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse, Response
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from haloflow.platform.http import TENANT_HEADERS, TENANT_QUERY_KEYS
from haloflow.platform.http import body as production_body
from haloflow.synthetic.errors import SyntheticRefused
from haloflow.synthetic.hooks import fire
from haloflow.synthetic.labels import LABEL_HEADER, internal, refusal, relabel
from haloflow.synthetic.ui import UI_FORM_MEDIA, UI_FORM_ROUTE

LOGGER: Final = logging.getLogger("haloflow.synthetic")
CSRF_HEADER: Final = "x-haloflow-synthetic-csrf"
HEALTH_PATH: Final = "/synthetic/health"
_JSON: Final = frozenset({"application/json", "application/json; charset=utf-8"})
_FETCH_OK: Final = frozenset({"same-origin", "none"})
_LABEL: Final = (LABEL_HEADER.encode("latin-1"), b"true")


def allowed_hosts(bind: tuple[str, int]) -> frozenset[str]:
    """The Host values a request to `bind` may carry (R-B3b-4 step 1)."""
    host, port = bind
    try:
        address = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        raise SyntheticRefused("BIND_NOT_LOOPBACK") from None
    if not address.is_loopback or type(port) is not int or not 1 <= port <= 65535:
        raise SyntheticRefused("BIND_NOT_LOOPBACK")
    literal = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return frozenset({f"{literal}:{port}", f"localhost:{port}"})


def _routed(scope: Scope) -> bool:
    """True when a served route matches the path (an unknown path goes on to the router's
    labelled 404 and is not counted)."""
    app = scope.get("app")
    routes = getattr(getattr(app, "router", None), "routes", ())
    return any(route.matches(scope)[0] != Match.NONE for route in routes)


def _ui_form(scope: Scope) -> bool:
    """True only for a POST whose path fully matches one registered B4 form route, with the
    raw path identical to the decoded one (no percent-encoded variant can match)."""
    path = scope.get("path")
    raw = scope.get("raw_path")
    if type(path) is not str or (raw is not None and bytes(raw) != path.encode("latin-1")):
        return False
    return UI_FORM_ROUTE.fullmatch(path) is not None


def _values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [bytes(v).decode("latin-1") for k, v in headers if bytes(k).lower() == name]


class LocalGate:
    def __init__(self, app: ASGIApp, *, lifecycle: Any, allowed: frozenset[str]) -> None:
        self.app = app
        self.lifecycle = lifecycle
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def labelled_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [h for h in message.get("headers", [])
                           if bytes(h[0]).lower() != _LABEL[0]]
                message = {**message, "headers": [*headers, _LABEL]}
            await send(message)

        counted = False
        try:
            fire("b3b.gate.enter", {})
            decided = self._decide(scope)
            if decided is None and scope.get("path") != HEALTH_PATH and _routed(scope):
                counted = self.lifecycle.http_enter()
                if not counted:
                    decided = refusal(503, self.lifecycle.stop_code)
            if decided is not None:
                await decided(scope, receive, labelled_send)
                return
            await self.app(scope, receive, labelled_send)
        except Exception:  # noqa: BLE001 - fixed body only; the fault text is never kept
            LOGGER.info("REQUEST gate fault")
            if not started:
                await internal()(scope, receive, labelled_send)
                return
            raise
        finally:
            if counted:
                self.lifecycle.http_leave()

    def _decide(self, scope: Scope) -> Response | None:
        headers = list(scope.get("headers", []))
        hosts = _values(headers, b"host")
        if len(hosts) != 1 or hosts[0] not in self.allowed:
            return refusal(421, "HOST_REFUSED")
        host = hosts[0]
        sites = _values(headers, b"sec-fetch-site")
        if any(site.strip().lower() not in _FETCH_OK for site in sites):
            return refusal(403, "CROSS_SITE_REFUSED")
        method = scope.get("method", "")
        if method not in ("GET", "POST"):
            return refusal(405, "METHOD_NOT_ALLOWED")
        if self._tenant_input(scope, headers):
            return relabel(production_body("TENANT_INPUT_REFUSED"))
        if method == "POST":
            origins = _values(headers, b"origin")
            if len(origins) != 1 or origins[0] != f"http://{host}":
                return refusal(403, "ORIGIN_REFUSED")
            media = _values(headers, b"content-type")
            if _ui_form(scope):
                # B4 (v1r1 F1.1, v1r2 C4): the two registered staff-screen form routes take
                # exactly the form media type; their CSRF token is in the form body and is
                # checked by the screen before any principal resolution or service call.
                if len(media) != 1 or media[0].strip().lower() != UI_FORM_MEDIA:
                    return refusal(415, "UNSUPPORTED_MEDIA")
                return None
            if len(media) != 1 or media[0].strip().lower() not in _JSON:
                return refusal(415, "UNSUPPORTED_MEDIA")
            tokens = _values(headers, CSRF_HEADER.encode("latin-1"))
            token = self.lifecycle.csrf_token
            if (len(tokens) != 1 or token is None
                    or not hmac.compare_digest(tokens[0].encode("latin-1"),
                                               token.encode("latin-1"))):
                return refusal(403, "CSRF_REFUSED")
        return None

    @staticmethod
    def _tenant_input(scope: Scope, headers: list[tuple[bytes, bytes]]) -> bool:
        if any(bytes(name).lower() in TENANT_HEADERS for name, _ in headers):
            return True
        query = bytes(scope.get("query_string", b"")).decode("latin-1")
        return any(key.lower() in TENANT_QUERY_KEYS
                   for key, _ in parse_qsl(query, keep_blank_values=True))


__all__ = ["CSRF_HEADER", "LABEL_HEADER", "JSONResponse", "LocalGate", "allowed_hosts"]
