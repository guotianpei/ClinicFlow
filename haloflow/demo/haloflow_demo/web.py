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
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from haloflow_demo.errors import OwnerLost
from haloflow_demo.inbound import MAX_BODY

STAFF_BANNER = "Local demonstrator — no authentication; synthetic data only."
CSRF_COOKIE = "demo_csrf_session"
CSRF_FIELD = "csrf_token"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
UNAVAILABLE_TEXT = "demo owner unavailable"

# N-UX (package v1r1, Codex ALIGN): one result line after each staff POST. The 303
# carries only a fixed code from this closed map; the line states only what the
# returned value establishes, and any unlisted value gets a neutral "check" code.
RESULT_TEXT: dict[str, str] = {
    "send_submitted": "Reminder submitted (simulated).",
    "send_skipped": "Not sent — this appointment already has a reminder or is not eligible.",
    "send_not_sent": "Not sent — see the appointment's status.",
    "send_rejected": "Rejected by the provider — see the appointment's status.",
    "send_uncertain": "Send outcome uncertain — not re-sent.",
    "send_check": "Send attempted — check the appointment's status.",
    "batch_done": "Batch processed — check each appointment's status.",
    "reply_recorded": "Simulated reply recorded — press Process replies.",
    "reply_duplicate": "Duplicate reply — nothing new recorded.",
    "reply_not_recorded": "Simulated reply not recorded — check settings and Replies.",
    "replies_processed": "Reply processing run — see Replies and Follow-up items.",
    "noreply_checked": "No-reply check run — see Follow-up items.",
    "paused": "Sending paused.",
    "resumed": "Sending resumed.",
    "writeback_done": "Write-back done (simulated).",
    "writeback_conflict": "Write-back stopped — the EHR value changed; staff follow-up created.",
    "writeback_not_applied": "Write-back not applied — see the task.",
    "writeback_uncertain": "Write-back outcome uncertain — use Reconcile.",
    "writeback_busy": "Write-back already running.",
    "writeback_unavailable": "Write-back not possible for this task — staff follow-up created.",
    "writeback_check": "Write-back attempted — check the task's state.",
    "reconcile_done": "Reconciled — see Write-back tasks.",
    "reconcile_uncertain": ("Still uncertain — no new write attempted by this check;"
                            " try again later."),
    "item_done": "Follow-up item marked done.",
    "item_unchanged": "Item was already done or not found.",
}
_SEND_CODES = {"SUBMITTED": "send_submitted", "SKIPPED": "send_skipped",
               "NOT_SENT": "send_not_sent", "REJECTED": "send_rejected",
               "CLAIMED": "send_uncertain", "UNCERTAIN": "send_uncertain"}
_WRITEBACK_COMMON = {"writeback_conflict": "writeback_conflict",
                     "no_baseline": "writeback_not_applied",
                     "write_disabled": "writeback_not_applied",
                     "NOT_APPLIED": "writeback_not_applied",
                     "scenario_unavailable": "writeback_unavailable"}
_RUN_CODES = {"DONE": "writeback_done", "DONE(reconciled)": "writeback_done",
              "UNCERTAIN": "writeback_uncertain", "already_running": "writeback_busy",
              **_WRITEBACK_COMMON}
_RECONCILE_CODES = {"DONE": "reconcile_done", "DONE(reconciled)": "reconcile_done",
                    "UNCERTAIN": "reconcile_uncertain", **_WRITEBACK_COMMON}


def _send_code(result: Any) -> str:
    state = result.get("state") if isinstance(result, dict) else None
    return _SEND_CODES.get(state, "send_check") if isinstance(state, str) else "send_check"


def _reply_code(result: Any) -> str:
    status, reason = getattr(result, "status", None), getattr(result, "reason", None)
    if status == 204 and reason is None:
        return "reply_recorded"
    if status == 204 and reason == "duplicate":
        return "reply_duplicate"
    return "reply_not_recorded"


class StaffService(Protocol):
    async def page_data(self) -> dict[str, Any]: ...
    async def set_paused(self, paused: bool) -> None: ...
    async def preview_batch(self) -> list[dict[str, Any]]: ...
    async def send_batch(self) -> list[dict[str, Any]]: ...
    async def send_to_appointment(self, emr_appt_id: str) -> dict[str, Any]: ...
    async def simulate_reply(self, reminder_id: int, message: str) -> Any: ...
    async def process_now(self) -> Any: ...
    async def check_no_response(self) -> list[int]: ...
    async def run_writeback(self, task_id: int) -> str: ...
    async def reconcile_writeback(self, task_id: int) -> str: ...
    async def mark_item_done(self, item_id: int) -> bool: ...


Receiver = Callable[[bytes, list[str]], Awaitable[Any]]


class NullStaffService:
    """Empty service: lets the apps be built with no database (R-1a)."""

    async def page_data(self) -> dict[str, Any]:
        return {"paused": False, "appointments": [], "inbound": [], "items": [],
                "labels": {"sms": "SIMULATED", "ehr": "SIMULATED", "callback": "SIMULATED"}}

    async def set_paused(self, paused: bool) -> None:
        return None

    async def preview_batch(self) -> list[dict[str, Any]]:
        return []

    async def send_batch(self) -> list[dict[str, Any]]:
        return []

    async def send_to_appointment(self, emr_appt_id: str) -> dict[str, Any]:
        return {}

    async def simulate_reply(self, reminder_id: int, message: str) -> Any:
        return None

    async def process_now(self) -> Any:
        return []

    async def check_no_response(self) -> list[int]:
        return []

    async def run_writeback(self, task_id: int) -> str:
        return "none"

    async def reconcile_writeback(self, task_id: int) -> str:
        return "none"

    async def mark_item_done(self, item_id: int) -> bool:
        return False


def _token(secret: bytes, session_id: str) -> str:
    return hmac.new(secret, session_id.encode(), hashlib.sha256).hexdigest()


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _source_html(src: Any) -> str:
    """N-ATH banners. Fixed text plus fixed codes/counts only (never response data)."""
    if not isinstance(src, dict) or src.get("kind") != "athena_preview":
        return ""
    if src.get("error"):
        return f"<p><strong>athenahealth sandbox unavailable — {_e(src['error'])}</strong></p>"
    lines = [f"Sandbox snapshot taken {_e(src.get('snapshot_taken'))}.",
             f"Times assume the sandbox department is in {_e(src.get('tz'))} — UNVERIFIED."]
    counts = src.get("counts") or {}
    if counts:
        lines.append("Excluded records: " + ", ".join(
            f"{_e(k)}={_e(v)}" for k, v in sorted(counts.items())) + ".")
    if src.get("truncated"):
        lines.append("TRUNCATED — more than 500 appointments; Send batch disabled.")
    return "".join(f"<p>{line}</p>" for line in lines)


def _render(data: dict[str, Any], token: str, preview: list[dict[str, Any]] | None,
            result: str | None = None) -> str:
    hidden = f'<input type="hidden" name="{CSRF_FIELD}" value="{_e(token)}">'

    def form(action: str, label: str, fields: dict[str, object] | None = None) -> str:
        extra = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
                        for k, v in (fields or {}).items())
        return (f'<form method="post" action="{action}">{hidden}{extra}'
                f"<button>{_e(label)}</button></form>")

    rows: list[str] = []
    for a in data.get("appointments", []):
        # G-E3-1: controls only where they apply (R-S1, R-S2); the server still guards.
        actions = ""
        if a.get("eligible"):
            actions += form("/demo/appointments/send", "Send to this appointment (simulated)",
                            {"emr_appt_id": a.get("emr_appt_id")})
        if a.get("can_reply") and a.get("reminder_id") is not None:
            actions += "".join(
                form("/demo/reply/simulate", f"Simulate reply {r}",
                     {"reminder_id": a.get("reminder_id"), "text": r})
                for r in ("1", "2", "3", "STOP"))
        rows.append(f"<tr><td>{_e(a.get('emr_appt_id'))}</td><td>{_e(a.get('patient'))}</td>"
                    f"<td>{_e(a.get('when'))}</td><td>{_e(a.get('status'))}</td>"
                    f"<td>{actions}</td></tr>")
    inbound = "".join(
        f"<li>{_e(m.get('received'))} {_e(m.get('masked_from'))}: {_e(m.get('text'))}"
        f" [{_e(m.get('cls'))}; {_e(m.get('state'))}; {_e(m.get('disposition'))}]"
        f" ({_e(m.get('label'))})</li>"
        for m in data.get("inbound", [])
    )
    items = "".join(  # G-E3-2 (R-S3): status shown; open items can be marked done
        f"<li>{_e(i.get('kind'))} ({_e(i.get('appointment'))}) [{_e(i.get('status'))}]:"
        f" {_e(i.get('detail'))}"
        + (form("/demo/items/done", "Mark done", {"item_id": i.get("id")})
           if i.get("status") == "open" else "")
        + "</li>"
        for i in data.get("items", [])
    )
    tasks = "".join(
        f"<li>Task {_e(tk.get('task_id'))} ({_e(tk.get('appointment'))}):"
        f" {_e(tk.get('state'))} {_e(tk.get('reason') or '')}"
        + (form("/demo/tasks/run", "Run write-back (simulated)", {"task_id": tk.get("task_id")})
           if tk.get("state") in ("PENDING", "NOT_APPLIED") else "")
        + (form("/demo/tasks/reconcile", "Reconcile (read only)",
                {"task_id": tk.get("task_id")}) if tk.get("state") == "UNCERTAIN" else "")
        + "</li>"
        for tk in data.get("tasks", [])
    )
    labels = data.get("labels", {})
    label_text = ", ".join(f"{_e(k)}={_e(v)}" for k, v in labels.items())
    source_html = _source_html(data.get("source"))
    status_html = f'<p role="status">{_e(result)}</p>' if result is not None else ""
    preview_html = ""
    if preview is not None:
        preview_html = "<h2>Batch preview</h2><ul>" + "".join(
            f"<li>{_e(p.get('emr_appt_id'))} — {_e(p.get('decision'))}</li>" for p in preview
        ) + "</ul>"

    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>HaloFlow demo</title></head>"
        f"<body><p><strong>{_e(STAFF_BANNER)}</strong></p>"
        f"{status_html}"
        f"<p>Modes: {label_text}. Paused: {_e(data.get('paused'))}.</p>"
        f"{source_html}"
        f"{form('/demo/batch/preview', 'Preview batch')}"
        f"{form('/demo/batch/send', 'Send batch (simulated)')}"
        f"{form('/demo/inbound/process', 'Process replies now')}"
        f"{form('/demo/noresponse/check', 'Check for no reply now')}"
        f"{form('/demo/pause', 'Pause sending')}{form('/demo/resume', 'Resume sending')}"
        f"{preview_html}"
        "<h2>Appointments</h2><table>"
        "<tr><th>Appointment</th><th>Patient</th><th>When</th><th>Status</th><th></th></tr>"
        f"{''.join(rows)}</table>"
        f"<h2>Replies</h2><ul>{inbound}</ul><h2>Follow-up items</h2><ul>{items}</ul>"
        f"<h2>Write-back tasks</h2><ul>{tasks}</ul>"
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

    async def _page(request: Request, preview: list[dict[str, Any]] | None = None,
                    result: str | None = None) -> Response:
        sid = _session(request)
        new = sid is None
        if sid is None:
            sid = secrets.token_urlsafe(32)
        body = _render(await service.page_data(), _token(csrf_secret, sid), preview, result)
        resp = HTMLResponse(body)
        if new:
            resp.set_cookie(CSRF_COOKIE, sid, httponly=True, samesite="strict", path="/")
        return resp

    def _forbidden() -> Response:
        return Response("CSRF check failed", status_code=403)

    def _back(code: str | None = None) -> Response:
        # N-UX: only a code from the closed RESULT_TEXT map ever enters the URL.
        suffix = f"?r={code}" if code in RESULT_TEXT else ""
        return RedirectResponse("/demo/staff" + suffix, status_code=303)

    @app.get("/demo/staff")
    async def staff_page(request: Request) -> Response:
        code = request.query_params.get("r")
        return await _page(request, result=RESULT_TEXT.get(code) if code else None)

    @app.post("/demo/pause")
    async def pause(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        await service.set_paused(True)
        return _back("paused")

    @app.post("/demo/resume")
    async def resume(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        await service.set_paused(False)
        return _back("resumed")

    @app.post("/demo/batch/send")
    async def batch_send(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        await service.send_batch()
        return _back("batch_done")

    @app.post("/demo/noresponse/check")
    async def noresponse_check(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        await service.check_no_response()
        return _back("noreply_checked")

    @app.post("/demo/batch/preview")
    async def batch_preview(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        return await _page(request, await service.preview_batch())

    async def _field(request: Request, name: str) -> str | None:
        value = (await request.form()).get(name)
        return value if isinstance(value, str) else None

    async def _int_field(request: Request, name: str) -> int | None:
        value = await _field(request, name)
        return int(value) if value is not None and value.isdigit() else None

    @app.post("/demo/appointments/send")
    async def send_one_appt(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        appt = await _field(request, "emr_appt_id")
        if appt is None:
            return Response("bad request", status_code=400)
        return _back(_send_code(await service.send_to_appointment(appt)))

    @app.post("/demo/reply/simulate")
    async def simulate_reply(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        rid = await _int_field(request, "reminder_id")
        message = await _field(request, "text")
        if rid is None or message is None:
            return Response("bad request", status_code=400)
        return _back(_reply_code(await service.simulate_reply(rid, message)))

    @app.post("/demo/inbound/process")
    async def process_now(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        await service.process_now()
        return _back("replies_processed")

    @app.post("/demo/tasks/run")
    async def run_task(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        tid = await _int_field(request, "task_id")
        if tid is None:
            return Response("bad request", status_code=400)
        try:
            outcome = await service.run_writeback(tid)
        except ValueError:
            return Response("not allowed in this state", status_code=409)
        return _back(_RUN_CODES.get(str(outcome), "writeback_check"))

    @app.post("/demo/tasks/reconcile")
    async def reconcile_task(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        tid = await _int_field(request, "task_id")
        if tid is None:
            return Response("bad request", status_code=400)
        try:
            outcome = await service.reconcile_writeback(tid)
        except ValueError:
            return Response("not allowed in this state", status_code=409)
        return _back(_RECONCILE_CODES.get(str(outcome), "writeback_check"))

    @app.post("/demo/items/done")
    async def mark_item_done(request: Request) -> Response:
        if not await _csrf_ok(request):
            return _forbidden()
        item_id = await _int_field(request, "item_id")
        if item_id is None:
            return Response("bad request", status_code=400)
        changed = await service.mark_item_done(item_id)  # unknown or already done: no change
        return _back("item_done" if changed is True else "item_unchanged")

    return app


def build_callback_app(
    *, unavailable: Callable[[], bool] | None = None, receiver: Receiver | None = None
) -> FastAPI:
    """R-6: one route only. The route passes ONLY the raw bytes and the signature
    header values to ``receiver``; nothing from the request can supply a
    simulated correlation or a provenance. The body is capped while it is read."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    _install_gate(app, unavailable)

    @app.post("/webhooks/sms/inbound")
    async def inbound(request: Request) -> Response:
        if receiver is None:
            return Response("inbound not enabled", status_code=503)
        declared = request.headers.get("content-length")
        if declared is not None:
            if not declared.isdigit():
                return Response(status_code=400)
            if int(declared) > MAX_BODY:
                return Response(status_code=413)
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > MAX_BODY:
                return Response(status_code=413)
        result = await receiver(bytes(buf), request.headers.getlist("notifyre-signature"))
        return Response(status_code=int(result.status))

    return app
