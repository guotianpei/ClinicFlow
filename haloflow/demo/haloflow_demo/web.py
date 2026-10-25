"""The two ASGI apps (R-6, R-7).

Staff app (loopback only): TrustedHost, Origin check, server-bound CSRF, escaped
HTML, no CORS, no docs. Callback app: exactly one route, no docs.

CSRF (precision P-CSRF): the cookie carries a random session id; the form
field must equal ``HMAC-SHA256(server_secret, session_id)``. A forged cookie
with a matching forged field fails because the attacker lacks the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from haloflow_demo.errors import OwnerLost

STAFF_BANNER = "Local demonstrator — no authentication; synthetic data only."
CSRF_COOKIE = "demo_csrf_session"
CSRF_FIELD = "csrf_token"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
UNAVAILABLE_TEXT = "demo owner unavailable"


class StaffService(Protocol):
    async def page_data(self) -> dict[str, Any]: ...
    async def set_paused(self, paused: bool) -> None: ...
    async def preview_batch(self) -> list[dict[str, Any]]: ...


class NullStaffService:
    """Empty service: lets the apps be built with no database (R-1a)."""

    async def page_data(self) -> dict[str, Any]:
        return {"paused": False, "appointments": [], "inbound": [], "items": [],
                "labels": {"sms": "SIMULATED", "ehr": "SIMULATED", "callback": "SIMULATED"}}

    async def set_paused(self, paused: bool) -> None:
        return None

    async def preview_batch(self) -> list[dict[str, Any]]:
        return []


def _token(secret: bytes, session_id: str) -> str:
    return hmac.new(secret, session_id.encode(), hashlib.sha256).hexdigest()


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _render(data: dict[str, Any], token: str, preview: list[dict[str, Any]] | None) -> str:
    rows: list[str] = []
    for a in data.get("appointments", []):
        rows.append(f"<tr><td>{_e(a.get('emr_appt_id'))}</td><td>{_e(a.get('patient'))}</td>"
                    f"<td>{_e(a.get('when'))}</td><td>{_e(a.get('status'))}</td></tr>")
    inbound = "".join(
        f"<li>{_e(m.get('masked_from'))}: {_e(m.get('text'))}</li>"
        for m in data.get("inbound", [])
    )
    items = "".join(
        f"<li>{_e(i.get('kind'))}: {_e(i.get('detail'))}</li>" for i in data.get("items", [])
    )
    labels = data.get("labels", {})
    label_text = ", ".join(f"{_e(k)}={_e(v)}" for k, v in labels.items())
    preview_html = ""
    if preview is not None:
        preview_html = "<h2>Batch preview</h2><ul>" + "".join(
            f"<li>{_e(p.get('emr_appt_id'))} — {_e(p.get('decision'))}</li>" for p in preview
        ) + "</ul>"
    hidden = f'<input type="hidden" name="{CSRF_FIELD}" value="{_e(token)}">'

    def form(action: str, label: str) -> str:
        return f'<form method="post" action="{action}">{hidden}<button>{label}</button></form>'

    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>HaloFlow demo</title></head>"
        f"<body><p><strong>{_e(STAFF_BANNER)}</strong></p>"
        f"<p>Modes: {label_text}. Paused: {_e(data.get('paused'))}.</p>"
        f"{form('/demo/batch/preview', 'Preview batch')}"
        f"{form('/demo/pause', 'Pause sending')}{form('/demo/resume', 'Resume sending')}"
        f"{preview_html}"
        "<h2>Appointments</h2><table>"
        "<tr><th>Appointment</th><th>Patient</th><th>When</th><th>Status</th></tr>"
        f"{''.join(rows)}</table>"
        f"<h2>Inbound</h2><ul>{inbound}</ul><h2>Follow-up items</h2><ul>{items}</ul>"
        "</body></html>"
    )


def _install_gate(app: FastAPI, unavailable: Callable[[], bool] | None) -> None:
    """R-5: every request gets 503 once ownership is lost or shutdown began."""

    @app.middleware("http")
    async def _gate(request: Request, call_next: Any) -> Response:
        if unavailable is not None and unavailable():
            return Response(UNAVAILABLE_TEXT, status_code=503)
        response: Response = await call_next(request)
        return response

    @app.exception_handler(OwnerLost)
    async def _owner_lost(request: Request, exc: OwnerLost) -> Response:
        return Response(UNAVAILABLE_TEXT, status_code=503)


def build_staff_app(
    service: StaffService,
    *,
    csrf_secret: bytes,
    unavailable: Callable[[], bool] | None = None,
    staff_port: int = 8080,
) -> FastAPI:
    if len(csrf_secret) < 32:
        raise ValueError("csrf_secret must be at least 32 bytes")
    allowed_origins = frozenset({f"http://127.0.0.1:{staff_port}", f"http://localhost:{staff_port}"})
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _origin_check(request: Request, call_next: Any) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return Response("forbidden origin", status_code=403)
        response: Response = await call_next(request)
        return response

    _install_gate(app, unavailable)

    # added last → runs first, so a bad Host is rejected before anything else
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    def _session(request: Request) -> str | None:
        sid = request.cookies.get(CSRF_COOKIE)
        return sid if sid and 20 <= len(sid) <= 128 else None

    async def _csrf_ok(request: Request) -> bool:
        sid = _session(request)
        if sid is None:
            return False
        form = await request.form()
        submitted = form.get(CSRF_FIELD)
        if not isinstance(submitted, str) or not submitted:
            return False
        return hmac.compare_digest(submitted, _token(csrf_secret, sid))

    async def _page(request: Request, preview: list[dict[str, Any]] | None = None) -> Response:
        sid = _session(request)
        new = sid is None
        if sid is None:
            sid = secrets.token_urlsafe(32)
        body = _render(await service.page_data(), _token(csrf_secret, sid), preview)
        resp = HTMLResponse(body)
        if new:
            resp.set_cookie(CSRF_COOKIE, sid, httponly=True, samesite="strict", path="/")
        return resp

    def _forbidden() -> Response:
        return Response("CSRF check failed", status_code=403)

    def _back() -> Response:
        return RedirectResponse("/demo/staff", status_code=303)

    @app.get("/demo/staff")
    async def staff_page(request: Request) -> Response:
        return await _page(request)

    @app.post("/demo/pause")
    async def pause(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        await service.set_paused(True)
        return _back()

    @app.post("/demo/resume")
    async def resume(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        await service.set_paused(False)
        return _back()

    @app.post("/demo/batch/preview")
    async def batch_preview(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        return await _page(request, await service.preview_batch())

    return app


def build_callback_app(*, unavailable: Callable[[], bool] | None = None) -> FastAPI:
    """R-6: one route only. Inbound processing is Day-5 scope; until then 503."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    _install_gate(app, unavailable)

    @app.post("/webhooks/sms/inbound")
    async def inbound() -> Response:
        return Response("inbound not enabled in this stage", status_code=503)

    return app
