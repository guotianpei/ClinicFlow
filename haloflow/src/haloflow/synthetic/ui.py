"""The synthetic HTML staff screen (Option B B4). SYNTHETIC ONLY.

Server-rendered pages over the B3b staff service path, with no JavaScript:
- `GET /synthetic/ui/items`: the list, filtered by a closed `state` value, with keyset paging
  taken from the service's own `next` cursor and page-local grouping (open-unassigned first);
- `GET /synthetic/ui/items/{item_id}`: the item, its reason (`ehr_confirmation_check`), its
  history (resolution entries in the W2 form) and the Claim / Close forms;
- `POST /synthetic/ui/items/{item_id}/claim` and `.../resolve`: form posts, each checked in the
  v1r2 C4 order (byte cap, raw validation, structure, CSRF, required fields, type) before any
  principal resolution or service call, then executed through `staff_routes.execute` (the one
  action path shared with the JSON routes) and answered 303 to the item view;
- `GET /synthetic/ui/static/app.css`.

Governing records: B4 package v1 (610ebf93), v1r1 (32c0e847), v1r2 (2cc8f65e); owner decisions
OD-B4-1 (no patient name or appointment until B5) and OD-B4-2 (the five messages). Every
label is the approved text; every dynamic value is escaped in one place (`esc`). Patient reply
text and note text are never fetched or shown (OD-D2 hold). Not production authentication.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from haloflow.m05.service import Cursor, ListQuery
from haloflow.platform.routes import (
    _Malformed,
    _path_uuid,
    _resolve_action,
    _staff_action,
)
from haloflow.synthetic.display_statements import DISPLAY_KEY
from haloflow.synthetic.provision import SETTINGS, STAFF_NAME
from haloflow.synthetic.staff_routes import execute

UI_PREFIX: Final = "/synthetic/ui/items"
CSS_PATH: Final = "/synthetic/ui/static/app.css"
# B4 staff form routes, plus (B5 v1r1 D2) exactly the four simulator form routes. The gate
# accepts the form media type only on a full match of this union.
UI_FORM_ROUTE: Final = re.compile(
    r"/synthetic/ui/items/(?P<item_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12})/(?P<endpoint>claim|resolve|contact|close)"
    r"|/synthetic/simulator/ui/(?:load|tick|reply|ehr|clearinghouse)")
# E4: contact / close are served for eligibility items, and the clearinghouse simulator form,
# only on an eligibility-profile app (elsewhere those paths are not routed: 404).
UI_FORM_MEDIA: Final = "application/x-www-form-urlencoded"
UI_FORM_MAX_BYTES: Final = 8192
UI_FORM_MAX_FIELDS: Final = 6
UI_PAGE_SIZE = 100  # module constant; tests lower it (v1r1 F2.1)

BANNER: Final = "SIMULATED — synthetic data, no real patients, no texts sent, no EHR changed"
TITLE: Final = "SIMULATED — HaloFlow synthetic staff screen"
UNRECOGNIZED: Final = "Unrecognized"
NO_APPOINTMENT: Final = "No appointment linked"     # OD-B4-1 / T-B5-2 (approved)
PAGE_LOCAL: Final = "Ordered within this page."

# Exact approved wording (v1r1 §5; demonstrator PR #43 and S-I C5(a)).
KIND_LABELS: dict[str, str] = {
    "no_reply": "No reply — call patient",
    "reschedule_request": "Reschedule requested — call patient",
    "cancel_request": "Cancel requested — staff to cancel in EHR",
    "unclear_reply": "Reply not understood. Review and contact the patient if needed.",
    "ehr_confirmation_check": "EHR confirmation needs checking",
    # OD-EL11 (Rachel 2026-11-21), exact.
    "eligibility_inactive": "Insurance not active — review coverage",
    "eligibility_inconclusive": "Insurance check inconclusive — review",
    "eligibility_manual": "Insurance needs manual check",
}
EL_KINDS: Final = frozenset({"eligibility_inactive", "eligibility_inconclusive",
                             "eligibility_manual"})
REASON_LABELS: dict[str, str] = {
    "status_changed": "status changed",
    "declined_or_unknown": "declined or unknown",
    "ehr_unreachable": "couldn't reach EHR",
    "could_not_verify": "couldn't verify",
    "no_baseline": "no baseline",
}
# W1 (Rachel 2026-10-30); parity with the installed m05_outcome_codes is a PostgreSQL row.
OUTCOME_LABELS: dict[str, str] = {
    "rescheduled": "Rescheduled",
    "cancelled_in_ehr": "Cancelled in EHR",
    "reached_no_change": "Reached – no change",
    "no_contact_left_message": "Couldn't reach – left message",
    "no_contact_no_answer": "Couldn't reach – no answer",
    "other": "Other",
}
NOTE_REQUIRED: Final = frozenset({"other"})
# OD-B4-2 (Rachel 2026-11-16), exact.
MESSAGES: dict[str, str] = {
    "conflict": "This item changed since you opened it. Reload to see the latest.",
    "resolved": "This item is already closed.",
    "note": "A note is required for Other.",
    "refused": "This action is not available.",
    "unavailable": "Service unavailable — try again.",
}

FORM_FIELDS: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    "claim": (frozenset({"csrf", "type", "client_action_id", "expected_version"}),
              frozenset()),
    "resolve": (frozenset({"csrf", "type", "client_action_id", "expected_version",
                           "outcome_code"}), frozenset({"note"})),
}
ENDPOINT_TYPES: Final[dict[str, frozenset[str]]] = {
    "claim": frozenset({"claim"}),
    "resolve": frozenset({"resolve", "claim_and_resolve"}),
}
LIST_FILTERS: Final[dict[str, tuple[str, ...] | None]] = {
    "active": ("open", "in_progress"),
    "open": ("open",),
    "in_progress": ("in_progress",),
    "resolved": ("resolved",),
    "all": None,
}
LIST_PARAMETERS: Final = frozenset({"state", "after_created_at", "after_item_id"})
SECURITY_HEADERS: Final = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; form-action 'self';"
                                " frame-ancestors 'none'; base-uri 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_SERVICE_MESSAGES: Final = {"STALE_VERSION": "conflict", "ITEM_RESOLVED": "resolved",
                            "NOTE_REQUIRED": "note",
                            # E4: the E3b view token went stale -> the OD-B4-2 reload message
                            "CASE_CHANGED": "conflict"}
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TOKEN_FIELDS: Final = ("case_id", "item_id", "case_version")


def case_token_fields(case_id: str, item_id: str, case_version: int) -> list[tuple[str, str]]:
    """E4 (D-E4-5): the E3b view token as the hidden fields of an eligibility form."""
    return [("case_id", str(case_id)), ("item_id", str(item_id)),
            ("case_version", str(case_version))]


def parse_case_token(fields: Mapping[str, str]) -> tuple[str, str, int]:
    """(case_id, item_id, case_version) from a form's hidden fields; ValueError on any missing
    or malformed part (refused before any service call)."""
    try:
        case_id, item_id, version = (fields[k] for k in TOKEN_FIELDS)
    except KeyError:
        raise ValueError("case_token") from None
    if not (type(case_id) is str and _UUID.fullmatch(case_id) and type(item_id) is str
            and _UUID.fullmatch(item_id) and type(version) is str
            and _VERSION.fullmatch(version)):
        raise ValueError("case_token")
    return case_id, item_id, int(version)
_VERSION: Final = re.compile(r"[1-9][0-9]{0,9}")
_RAW_BYTES: Final = re.compile(rb"[A-Za-z0-9*\-._+%=&]*")
_BAD_PERCENT: Final = re.compile(rb"%(?![0-9A-Fa-f]{2})")
_RANK: Final = {"open": 1, "in_progress": 2, "resolved": 3}
_CSS: Final = (
    "body{font-family:sans-serif;margin:1.5rem;max-width:60rem}"
    ".banner{background:#fde68a;border:1px solid #b45309;padding:.5rem;font-weight:bold}"
    "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.25rem .5rem}"
    ".message{border:1px solid #1d4ed8;padding:.5rem}"
)


class FormRefused(Exception):
    """A form refused before any service call (v1r2 C3/C4); `status` is the HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


# ---------------------------------------------------------------------------- render helpers

def esc(value: object) -> str:
    """The one escaping function for every dynamic value (R-B4-7)."""
    return html.escape(str(value), quote=True)


def label(mapping: Mapping[str, str], code: object) -> str:
    """The escaped approved label of a closed-set code; anything else is the fixed text."""
    if type(code) is str and code in mapping:
        return esc(mapping[code])
    return UNRECOGNIZED


def staff_reported(outcome_code: object, actor_display: object, when: object) -> str:
    """W2 (Rachel 2026-10-30): a staff-stated outcome, never shown as EHR-verified."""
    return (f"Staff reported: {label(OUTCOME_LABELS, outcome_code)} — by {esc(actor_display)},"
            f" {esc(when)} (not verified in EHR)")


def order_page(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Page-local grouping (v1r1 F2.1): open-unassigned, open-assigned, in progress, resolved;
    stable within a group, so the service order is kept there."""
    def rank(row: Mapping[str, Any]) -> int:
        state = row.get("state")
        if state == "open" and row.get("assignee_id") is None:
            return 0
        return _RANK.get(state, 4) if type(state) is str else 4

    return sorted(rows, key=rank)


def _page(body: str, status: int = 200) -> HTMLResponse:
    document = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{esc(TITLE)}</title>"
        f"<link rel=\"stylesheet\" href=\"{CSS_PATH}\"></head><body>"
        f"<p class=\"banner\">{esc(BANNER)}</p>{body}</body></html>")
    return HTMLResponse(document, status_code=status, headers=dict(SECURITY_HEADERS))


def _error_page(status: int) -> HTMLResponse:
    return _page(f"<p class=\"message\">{esc(MESSAGES['refused'])}</p>"
                 f"<p><a href=\"{UI_PREFIX}\">{esc('Items')}</a></p>", status)


def _when(value: object) -> str:
    if type(value) is not str:
        return UNRECOGNIZED
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return UNRECOGNIZED
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _zone(name: object, fallback: str) -> ZoneInfo:
    if type(name) is str and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return ZoneInfo(fallback)


def display_lines(read: Mapping[str, Any], *, clinic_tz: str) -> tuple[str, ...]:
    """B5 (T-B5-1/T-B5-2, v1r1 C2/C3): the synthetic display lines, UNESCAPED (the page
    escapes each line once). Linked: the first name, then the appointment's local date and
    time in its department zone (the clinic zone when that is not a valid zone name) and,
    for a known packaged provider ref, its label. Anything else, or any incomplete linked
    read, is exactly "No appointment linked" (fail closed; never a guess)."""
    from haloflow.synthetic.scenarios import PROVIDERS

    first = read.get("first_name")
    raw_start = read.get("start_at")
    if read.get("linked") is not True or type(first) is not str or type(raw_start) is not str:
        return (NO_APPOINTMENT,)
    try:
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
    except ValueError:
        return (NO_APPOINTMENT,)
    if start.tzinfo is None:
        return (NO_APPOINTMENT,)
    tz = _zone(read.get("department_tz"), clinic_tz)
    when = f"Appointment: {start.astimezone(tz):%Y-%m-%d %H:%M} {tz.key}"
    provider = read.get("provider_ref")
    label_text = PROVIDERS.get(provider) if type(provider) is str else None
    return (f"Patient: {first}", when + (f" — {label_text}" if label_text else ""))


def _patient_ref(patient_id: object) -> str:
    text = str(patient_id).replace("-", "")
    return esc(text[:8]) if re.fullmatch(r"[0-9a-f]{32}", text) else UNRECOGNIZED


# ---------------------------------------------------------------------------- form parsing

def parse_form(body: bytes, *, allowed: frozenset[str],
               max_fields: int = UI_FORM_MAX_FIELDS) -> dict[str, str]:
    """v1r2 C3 then C4 steps 2-3: byte set, explicit percent triplets, pair structure, field
    count, strict decoding, then duplicate and unknown names. Raises FormRefused(400).
    `max_fields` is UI_FORM_MAX_FIELDS for the B4 forms; the B5 simulator EHR form, whose
    closed field set is larger, passes its own bound."""
    if not _RAW_BYTES.fullmatch(body) or _BAD_PERCENT.search(body):
        raise FormRefused(400)
    pairs = body.split(b"&")
    if any(not pair or b"=" not in pair or pair.startswith(b"=") for pair in pairs):
        raise FormRefused(400)
    if len(pairs) > max_fields:
        raise FormRefused(400)
    try:
        decoded = parse_qsl(body.decode("ascii"), keep_blank_values=True,
                            strict_parsing=True, encoding="utf-8", errors="strict",
                            max_num_fields=max_fields)
    except (UnicodeDecodeError, ValueError):
        raise FormRefused(400) from None
    names = [name for name, _ in decoded]
    if len(set(names)) != len(names) or any(name not in allowed for name in names):
        raise FormRefused(400)
    return dict(decoded)


async def _bounded_body(request: Request) -> bytes:
    """v1r1 F1.2: the stream is read with or without a Content-Length and refused 413 past the
    cap, or past a declared length, before anything is decoded; a body shorter than its
    declared length is 400."""
    declared = request.headers.getlist("content-length")
    length: int | None = None
    if declared:
        if len(declared) != 1 or not declared[0].isdigit():
            raise FormRefused(400)
        length = int(declared[0])
        if length > UI_FORM_MAX_BYTES:
            raise FormRefused(413)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > UI_FORM_MAX_BYTES or (length is not None and size > length):
            raise FormRefused(413)
        chunks.append(chunk)
    if length is not None and size != length:
        raise FormRefused(400)  # shorter than declared
    return b"".join(chunks)


def _csrf_ok(given: str | None, token: str | None) -> bool:
    import hmac

    if given is None or token is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))


# ---------------------------------------------------------------------------- service access

class _Failed(Exception):
    """A service read answered with a production refusal (its JSON code and status)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _code(response: Response) -> str:
    try:
        content = json.loads(bytes(response.body))
        code = content.get("code") if type(content) is dict else None
    except ValueError:
        code = None
    return code if type(code) is str else "INTERNAL"


async def _read(lifecycle: Any, route: str,
                call: Callable[[Any, Any], Awaitable[Any]]) -> Mapping[str, Any]:
    outcome = await execute(lifecycle, route, call)
    if isinstance(outcome, Response):
        raise _Failed(outcome.status_code, _code(outcome))
    return dict(outcome)


def _actor(lifecycle: Any, actor_id: object) -> str:
    services = lifecycle.services
    if services is not None and str(actor_id) == str(services.staff_id):
        return STAFF_NAME
    return UNRECOGNIZED


async def _display_read(lifecycle: Any, context: Any, item_id: uuid.UUID) -> Mapping[str, Any]:
    """B5 (v1r1 C1/C2): the staff READ of the synthetic display (t095), under the same
    two-step staff resolution as the item read (route `get`, READ capability)."""
    async def run(handle: Any) -> Any:
        return await handle.fetch_all(DISPLAY_KEY, {"item_id": str(item_id)})

    rows = await lifecycle.services.gateway.with_tenant_transaction(context, run)
    value = rows[0][0] if rows and rows[0] else None
    return dict(value) if isinstance(value, Mapping) else {"linked": False}


# ---------------------------------------------------------------------------- pages

async def list_page(lifecycle: Any, request: Request) -> Response:
    pairs = parse_qsl(request.scope.get("query_string", b"").decode("latin-1"),
                      keep_blank_values=True)
    names = [name for name, _ in pairs]
    given = dict(pairs)
    if (len(set(names)) != len(names) or any(n not in LIST_PARAMETERS for n in names)
            or given.get("state", "active") not in LIST_FILTERS
            or ("after_created_at" in given) != ("after_item_id" in given)):
        return _error_page(422)
    state = given.get("state", "active")
    after: Cursor | None = None
    if "after_item_id" in given:
        try:
            moment = datetime.fromisoformat(given["after_created_at"].replace("Z", "+00:00"))
            after = Cursor(created_at=moment, item_id=uuid.UUID(given["after_item_id"]))
        except Exception:  # noqa: BLE001 - any bad cursor is 422
            return _error_page(422)
    try:
        query = ListQuery(limit=UI_PAGE_SIZE, states=LIST_FILTERS[state], after=after)
    except Exception:  # noqa: BLE001 - production's command check
        return _error_page(422)
    try:
        value = await _read(lifecycle, "list", lambda m05, ctx: m05.list_items(ctx, query))
    except _Failed as failure:
        return _error_page(failure.status)
    rows = [row for row in value.get("items", []) if isinstance(row, Mapping)]
    options = "".join(
        f"<option value=\"{esc(code)}\"{' selected' if code == state else ''}>{esc(code)}"
        "</option>" for code in LIST_FILTERS)
    lines = []
    for row in order_page(rows):
        item_id = esc(row.get("item_id"))
        assignee = _actor(lifecycle, row.get("assignee_id")) \
            if row.get("assignee_id") is not None else "—"
        lines.append(
            f"<tr><td>{label(KIND_LABELS, row.get('item_kind'))}</td>"
            f"<td>{esc(row.get('state'))}</td><td>{esc(assignee)}</td>"
            f"<td>{esc(_when(row.get('created_at')))}</td>"
            f"<td><a href=\"{UI_PREFIX}/{item_id}\">View</a></td></tr>")
    nxt = value.get("next")
    more = ""
    if isinstance(nxt, Mapping):
        link = UI_PREFIX + "?" + urlencode({"state": state,
                                            "after_created_at": nxt.get("created_at", ""),
                                            "after_item_id": nxt.get("item_id", "")})
        more = f"<p><a href=\"{esc(link)}\">Next</a></p>"
    body = (
        "<h1>Follow-up items</h1>"
        f"<form method=\"get\" action=\"{UI_PREFIX}\"><select name=\"state\">{options}</select>"
        "<button type=\"submit\">Show</button></form>"
        f"<p>{esc(PAGE_LOCAL)}</p>"
        "<table><tr><th>Kind</th><th>State</th><th>Assignee</th><th>Created</th><th></th></tr>"
        f"{''.join(lines)}</table>{more}")
    return _page(body)


def _form(lifecycle: Any, item: Mapping[str, Any], endpoint: str, type_: str) -> str:
    action = f"{UI_PREFIX}/{esc(item.get('item_id'))}/{endpoint}"
    hidden = {"csrf": lifecycle.csrf_token or "", "type": type_,
              "client_action_id": str(uuid.uuid4()),
              "expected_version": str(item.get("version"))}
    fields = "".join(f"<input type=\"hidden\" name=\"{name}\" value=\"{esc(value)}\">"
                     for name, value in hidden.items())
    if endpoint == "claim":
        return (f"<form method=\"post\" action=\"{action}\">{fields}"
                "<button type=\"submit\">Claim</button></form>")
    radios = "".join(
        f"<p><label><input type=\"radio\" name=\"outcome_code\" value=\"{esc(code)}\">"
        f" {esc(text)}</label></p>" for code, text in OUTCOME_LABELS.items())
    return (f"<form method=\"post\" action=\"{action}\">{fields}{radios}"
            "<p><label>Note <textarea name=\"note\" maxlength=\"500\"></textarea></label></p>"
            "<button type=\"submit\">Close</button></form>")


async def view_page(lifecycle: Any, request: Request) -> Response:
    try:
        item_id = _path_uuid(request.path_params["item_id"], "item_id")
    except _Malformed:
        return _error_page(404)
    query = dict(parse_qsl(request.scope.get("query_string", b"").decode("latin-1"),
                           keep_blank_values=True))
    message = query.get("m")
    try:
        item = await _read(lifecycle, "get", lambda m05, ctx: m05.get(ctx, item_id))
        history = await _read(lifecycle, "history",
                              lambda m05, ctx: m05.history(ctx, item_id))
        reason: Mapping[str, Any] | None = None
        if item.get("item_kind") == "ehr_confirmation_check":
            reason = await _read(lifecycle, "reason",
                                 lambda m05, ctx: m05.reason(ctx, item_id))
        display = await _read(lifecycle, "get",
                              lambda _m05, ctx: _display_read(lifecycle, ctx, item_id))
        case: Mapping[str, Any] | None = None
        if item.get("item_kind") in EL_KINDS:                       # E4 (D-E4-5)
            case = await _read(lifecycle, "case", lambda m05, ctx: m05.case(ctx, item_id))
    except _Failed as failure:
        return _error_page(failure.status)
    parts = []
    if type(message) is str and message in MESSAGES:
        parts.append(f"<p class=\"message\">{esc(MESSAGES[message])}</p>")
    assignee = item.get("assignee_id")
    parts.append(
        f"<h1>{label(KIND_LABELS, item.get('item_kind'))}</h1>"
        f"<p>State: {esc(item.get('state'))}</p>"
        f"<p>Assignee: {esc(_actor(lifecycle, assignee)) if assignee is not None else '—'}</p>"
        f"<p>Created: {esc(_when(item.get('created_at')))}</p>"
        f"<p>Synthetic patient ref: {_patient_ref(item.get('patient_id'))}</p>")
    parts.extend(f"<p>{esc(line)}</p>"
                 for line in display_lines(display, clinic_tz=str(SETTINGS["clinic_tz"])))
    if reason is not None:
        parts.append(f"<p>Reason: {label(REASON_LABELS, reason.get('reason'))}</p>")
    if case is not None:
        from haloflow.synthetic import el_ui

        parts.extend(el_ui.section(lifecycle, item, case))
    rows = []
    for entry in history.get("entries", []):
        if not isinstance(entry, Mapping):
            continue
        actor = _actor(lifecycle, entry.get("actor_id"))
        when = _when(entry.get("created_at"))
        raw_after = entry.get("after")
        after: Mapping[str, Any] = raw_after if isinstance(raw_after, Mapping) else {}
        if entry.get("assertion_kind") == "staff" and case is not None:
            from haloflow.synthetic import el_ui

            text = (f"Staff reported: {el_ui.outcome_text(after.get('outcome_code'))} — by"
                    f" {esc(actor)}, {esc(when)} (not verified in EHR)")
        elif entry.get("assertion_kind") == "staff":
            text = staff_reported(after.get("outcome_code"), actor, when)
        else:
            text = f"{esc(entry.get('action'))} — {esc(actor)}, {esc(when)}"
        note = " (note recorded)" if entry.get("note_id") is not None else ""
        rows.append(f"<li>{text}{note}</li>")
    parts.append(f"<h2>History</h2><ul>{''.join(rows)}</ul>")
    state = item.get("state")
    services = lifecycle.services
    principal = str(services.staff_id) if services is not None else None
    if state in ("open", "in_progress") and case is None:
        if assignee is None:
            parts.append(_form(lifecycle, item, "claim", "claim"))
            parts.append(_form(lifecycle, item, "resolve", "claim_and_resolve"))
        elif str(assignee) == principal:
            parts.append(_form(lifecycle, item, "resolve", "resolve"))
    parts.append(f"<p><a href=\"{UI_PREFIX}\">All items</a></p>")
    return _page("".join(parts))


# ---------------------------------------------------------------------------- actions

def _redirect(item_id: uuid.UUID, message: str | None = None) -> Response:
    target = f"{UI_PREFIX}/{item_id}" + (f"?m={message}" if message else "")
    return RedirectResponse(target, status_code=303)


async def action(lifecycle: Any, endpoint: str, request: Request) -> Response:
    """v1r2 C4: steps 1-7 refuse before any principal resolution or service call."""
    try:
        item_id = _path_uuid(request.path_params["item_id"], "item_id")
    except _Malformed:
        return _error_page(404)
    required, optional = FORM_FIELDS[endpoint]
    try:
        raw = await _bounded_body(request)                       # step 1
        fields = parse_form(raw, allowed=required | optional)    # steps 2-3
    except FormRefused as refused:
        return _error_page(refused.status)
    if "csrf" not in fields:                                     # step 4
        return _error_page(403)
    if not _csrf_ok(fields["csrf"], lifecycle.csrf_token):       # step 5
        return _error_page(403)
    if any(name not in fields for name in required):             # step 6
        return _error_page(400)
    if fields["type"] not in ENDPOINT_TYPES[endpoint]:           # step 7
        return _error_page(400)
    version_text = fields["expected_version"]
    value: dict[str, Any] = {
        "client_action_id": fields["client_action_id"], "type": fields["type"],
        "expected_version": int(version_text) if _VERSION.fullmatch(version_text)
        else version_text}
    try:                                                         # step 8
        if endpoint == "claim":
            command: Any = _staff_action(item_id, value)
        else:
            note = fields.get("note")
            value.update(outcome_code=fields["outcome_code"], note=note if note else None)
            command = _resolve_action(item_id, value)
    except _Malformed as error:
        if error.field == "note":
            return _redirect(item_id, "note")
        return _error_page(400)

    async def act(m05: Any, ctx: Any) -> dict[str, Any]:
        result = await m05.act(ctx, command)
        return {"outcome": result.outcome}

    outcome = await execute(lifecycle, "act" if endpoint == "claim" else "resolve", act)
    if not isinstance(outcome, Response):                        # step 9
        return _redirect(item_id)
    code = _code(outcome)
    if code == "NOT_FOUND":
        return _error_page(404)
    if outcome.status_code == 409 and code in _SERVICE_MESSAGES:
        return _redirect(item_id, _SERVICE_MESSAGES[code])
    if outcome.status_code in (409, 403):
        return _redirect(item_id, "refused")
    if code == "MALFORMED":
        return _error_page(400)
    return _redirect(item_id, "unavailable")


async def stylesheet() -> Response:
    return Response(_CSS, media_type="text/css", headers=dict(SECURITY_HEADERS))


def install_ui(app: FastAPI, lifecycle: Any) -> None:
    async def items(request: Request) -> Response:
        return await list_page(lifecycle, request)

    async def item(request: Request) -> Response:
        return await view_page(lifecycle, request)

    async def claim(request: Request) -> Response:
        return await action(lifecycle, "claim", request)

    async def resolve(request: Request) -> Response:
        return await action(lifecycle, "resolve", request)

    async def css() -> Response:
        return await stylesheet()

    for path, handler, method in (
            (UI_PREFIX, items, "GET"), (UI_PREFIX + "/{item_id}", item, "GET"),
            (UI_PREFIX + "/{item_id}/claim", claim, "POST"),
            (UI_PREFIX + "/{item_id}/resolve", resolve, "POST"), (CSS_PATH, css, "GET")):
        app.add_api_route(path, handler, methods=[method], response_class=Response,
                          include_in_schema=False)
    if getattr(lifecycle, "profile", "default") == "eligibility":     # E4 (D-E4-5)
        from haloflow.synthetic import el_ui

        async def contact(request: Request) -> Response:
            return await el_ui.action(lifecycle, "contact", request)

        async def close(request: Request) -> Response:
            return await el_ui.action(lifecycle, "close", request)

        for path, handler in ((UI_PREFIX + "/{item_id}/contact", contact),
                              (UI_PREFIX + "/{item_id}/close", close)):
            app.add_api_route(path, handler, methods=["POST"], response_class=Response,
                              include_in_schema=False)
