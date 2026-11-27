"""Incoming fax routing FX-IN, package FX3b: the synthetic fax staff screen. SYNTHETIC ONLY;
served on a fax-profile app only.

Server-rendered pages over the FX3a/FX4/FX5 fax staff services, with no JavaScript:
- `GET /synthetic/ui/fax`: the "Fax needs sorting" section (OD-FX9: first), then every queue in
  the t151 order with its faxes (inactive queues listed and marked, never offered as a target);
- `GET /synthetic/ui/fax/{fax_id}`: the fax, its history (actor display names), the filed notice
  (OD-FX12) and the actions of its state (routed: Move, Mark filed; needs sorting: Sort;
  filed: none). Side-effect free: opening is the Open form's POST only;
- `POST /synthetic/ui/fax/{fax_id}/open|move|file|sort`: form posts checked like the B4 forms
  (byte cap, raw form rules, CSRF) before any principal resolution or service call, then one
  FaxService call; 303 to the detail page. A state refusal (409) answers 409 with the re-read
  detail page and the OD-FX12 / OD-FX13 conflict notice.

Every page and post resolves the install's one principal exactly as the fax JSON routes do
(`fax_staff_routes.staff_context`): reads and open need `fax_read` (OD-FX11: read AND act),
move / file / sort need `act`; a refused principal gets the B4 refusal page with no fax data.
Actor names come from `StaffService.display` under the `display` route (tenant-scoped); a
staff id with no name is "Unknown staff member" and never named in a conflict notice.
Governing: FX3b package v1 and red packet v1/r1 (Codex accepted, e5cf5f7); OD-FX7, OD-FX8/9,
OD-FX10, OD-FX11, OD-FX12, OD-FX13. Not production authentication.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from haloflow.m05.fax import FaxAction, FaxRefused
from haloflow.platform.http import error_response
from haloflow.synthetic import ui
from haloflow.synthetic.fax_staff_routes import CONFLICT_CODES, NOT_FOUND_CODES, staff_context
from haloflow.synthetic.provision import SETTINGS

UI_PREFIX: Final = "/synthetic/ui/fax"
ENTRY_LABEL: Final = "Incoming faxes"
SORTING_HEADING: Final = "Fax needs sorting"
REASON_LABELS: Final[dict[str, str]] = {                   # OD-FX7, exact
    "no_matching_rule": "No matching rule",
    "sender_not_shown": "Sender number not shown",
    "rules_conflict": "Rules point to different queues",
}
UNKNOWN_SENDER: Final = "Unknown sender"                     # OD-FX10
SENDER_HIDDEN: Final = "Sender number not shown"
AS_SENT: Final = "(as sent)"
ENDING: Final = " · ending "
SYSTEM_ACTOR: Final = "System"
UNKNOWN_STAFF: Final = "Unknown staff member"
UNRECOGNIZED: Final = "Unrecognized"
FILED_NOTICE: Final = ("This fax has been filed. It stays in the list so you can find it again."
                       " It can't be moved or filed again.")                       # OD-FX12
CONFLICT_LEAD: Final = ("Nothing was changed. This fax was updated by someone else while you had"
                        " it open.")                                               # OD-FX12
REFRESH_LABEL: Final = "Refresh this fax"
_CASES: Final[dict[int, str]] = {                                                  # OD-FX12/13
    0: ("You tried to move it to {x}. It is now in {y}, moved there by {name} at {time}."
        " Check the details below, then choose again if it still needs to move."),
    1: ("You tried to move it to {x}. It was marked filed by {name} at {time}."
        " A filed fax can't be moved."),
    2: ("You tried to mark it filed. It is now in {y}, moved there by {name} at {time}."
        " Check the details below, then choose again."),
    3: "You tried to mark it filed. It was already marked filed by {name} at {time}.",
    4: ("You tried to sort it into {x}. It was already sorted into {y} by {name} at {time}."
        " Check the details below, then move it if it still needs to change."),
    5: "Check the details below.",
}
# (attempt, the competing state row's action) -> case
_CASE_OF: Final[dict[tuple[str, str], int]] = {
    ("move", "moved"): 0, ("move", "filed"): 1, ("file", "moved"): 2, ("file", "filed"): 3,
    ("sort", "sorted"): 4,
}
STATE_ACTIONS: Final = ("moved", "sorted", "filed")
_HISTORY_LABELS: Final[dict[str, str]] = {
    "received_routed": "Received", "received_needs_sorting": "Received", "opened": "Opened",
    "moved": "Moved", "sorted": "Sorted", "filed": "Marked filed",
}
FIELDS: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    # endpoint -> (form fields, of which the queue field is required for move / sort)
    "open": (frozenset({"csrf"}), frozenset()),
    "move": (frozenset({"csrf", "expected_version", "queue_id"}), frozenset()),
    "file": (frozenset({"csrf", "expected_version"}), frozenset()),
    "sort": (frozenset({"csrf", "expected_version", "queue_id"}), frozenset()),
}
_VERSION: Final = re.compile(r"[1-9][0-9]{0,8}")
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

FaxCall = Callable[[Any, Any], Awaitable[Any]]


def esc(value: object) -> str:
    return ui.esc(value)


# ---------------------------------------------------------------------------- pure builders

def from_text(fax: Mapping[str, Any]) -> str:
    """OD-FX10, UNESCAPED: the saved name, else the name as sent marked "(as sent)", else
    "Unknown sender"; " · ending NNNN" when the last four digits are known; a withheld number
    with no name is "Sender number not shown"."""
    display = fax.get("sender_display")
    text = display.get("text") if isinstance(display, Mapping) else None
    source = display.get("source") if isinstance(display, Mapping) else None
    last4 = fax.get("sender_last4")
    last = last4 if type(last4) is str and last4 else None
    if type(text) is str and text and source == "saved":
        base = text
    elif type(text) is str and text and source == "as_sent":
        base = f"{text} {AS_SENT}"
    elif last is None:
        return SENDER_HIDDEN
    else:
        base = UNKNOWN_SENDER
    return base + (f"{ENDING}{last}" if last is not None else "")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo("America/New_York")


def when_text(at: object, clinic_tz: str) -> str:
    """ISO-8601 UTC -> the clinic time, "Nov 24, 5:12 PM"; anything else "Unrecognized"."""
    if type(at) is not str:
        return UNRECOGNIZED
    try:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return UNRECOGNIZED
    if moment.tzinfo is None:
        return UNRECOGNIZED
    local = moment.astimezone(_zone(clinic_tz))
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def _seq(row: Mapping[str, Any]) -> int:
    seq = row.get("seq")
    return seq if type(seq) is int else -1


def conflict_notice(attempt: str, attempted_queue_id: str | None, expected_version: int,
                    fax: Mapping[str, Any], history: Sequence[Mapping[str, Any]],
                    names: Mapping[str, str], queue_names: Mapping[str, str], clinic_tz: str,
                    principal_id: object) -> str:
    """The line after CONFLICT_LEAD, UNESCAPED. A named case (0-4) only when exactly one state
    change happened since the form (version step 1), the latest moved / sorted / filed row is
    the change the case describes, its actor is a named staff member other than the principal
    (compared by id), and the re-read summary agrees with it; otherwise case 5 (no name, queue
    or time)."""
    neutral = _CASES[5]
    version = fax.get("version")
    if type(version) is not int or type(expected_version) is not int \
            or version != expected_version + 1:
        return neutral
    rows = [r for r in history if isinstance(r, Mapping) and r.get("action") in STATE_ACTIONS]
    if not rows:
        return neutral
    row = max(rows, key=_seq)
    action = str(row.get("action"))
    case = _CASE_OF.get((attempt, action))
    actor = row.get("actor")
    if case is None or type(actor) is not str or actor == str(principal_id) \
            or actor not in names:
        return neutral
    when = when_text(row.get("at"), clinic_tz)
    if when == UNRECOGNIZED:
        return neutral
    to = row.get("to_queue_id")
    if action == "filed":
        agrees = fax.get("state") == "filed"
    else:
        agrees = fax.get("state") == "routed" and type(to) is str and fax.get("queue_id") == to \
            and to in queue_names
    if not agrees:
        return neutral
    values = {"name": names[actor], "time": when, "y": queue_names.get(str(to), "")}
    if attempt in ("move", "sort"):
        if attempted_queue_id is None or attempted_queue_id not in queue_names:
            return neutral
        values["x"] = queue_names[attempted_queue_id]
    return _CASES[case].format_map(_Default(values))


class _Default(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


# ---------------------------------------------------------------------------- service access

class _Refused(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


async def _fax(lifecycle: Any, route: str, call: FaxCall) -> dict[str, Any]:
    services = lifecycle.services
    if services is None or getattr(services, "fax", None) is None:
        raise _Refused(503, "UNAVAILABLE")
    try:
        context = await staff_context(services, route)
        if context is None:
            raise _Refused(403, "FORBIDDEN")
        value = await call(services.fax, context)
    except _Refused:
        raise
    except FaxRefused as error:
        if error.code in NOT_FOUND_CODES:
            raise _Refused(404, "NOT_FOUND") from None
        if error.code in CONFLICT_CODES:
            raise _Refused(409, error.code) from None
        if error.code == "ACTOR_INACTIVE":
            raise _Refused(403, "FORBIDDEN") from None
        raise _Refused(503, "UNAVAILABLE") from None
    except Exception as error:  # noqa: BLE001 - production's closed status map
        try:
            status = error_response(error).status_code
        except Exception:  # noqa: BLE001
            status = 503
        raise _Refused(status, "REFUSED") from None
    return dict(value)


async def _names(lifecycle: Any, ids: Sequence[str]) -> dict[str, str]:
    """Display names for the given staff ids (StaffService.display under `display`); {} when
    the read is refused or fails (every actor then shows "Unknown staff member")."""
    wanted = sorted({i for i in ids if _UUID.fullmatch(i)})[:100]
    services = lifecycle.services
    if not wanted or services is None:
        return {}
    try:
        context = await staff_context(services, "display")
        if context is None:
            return {}
        value = await services.staff.display(context, tuple(uuid.UUID(i) for i in wanted))
    except Exception:  # noqa: BLE001 - names are optional; the fallback is fixed text
        return {}
    staff = value.get("staff") if isinstance(value, Mapping) else None
    return {str(e["staff_id"]): str(e["display_name"]) for e in staff or []
            if isinstance(e, Mapping)}


async def holds_fax_read(lifecycle: Any) -> bool:
    """OD-FX11: the staff-screen entry is shown only to a principal holding fax_read."""
    services = lifecycle.services
    if services is None or getattr(services, "fax", None) is None:
        return False
    try:
        return await staff_context(services, "fax_read") is not None
    except Exception:  # noqa: BLE001 - no entry when the standing cannot be read
        return False


# ---------------------------------------------------------------------------- rendering

def _hidden(name: str, value: object) -> str:
    return f"<input type=\"hidden\" name=\"{esc(name)}\" value=\"{esc(value)}\">"


def _open_form(lifecycle: Any, fax_id: str) -> str:
    return (f"<form method=\"post\" action=\"{UI_PREFIX}/{esc(fax_id)}/open\">"
            f"{_hidden('csrf', lifecycle.csrf_token or '')}"
            "<button type=\"submit\">Open</button></form>")


def _queue_map(queues: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = queues.get("queues")
    return [q for q in rows if isinstance(q, Mapping)] if isinstance(rows, list) else []


def _row(lifecycle: Any, fax: Mapping[str, Any], *, reason: bool) -> str:
    fax_id = str(fax.get("fax_id"))
    cells = [esc(when_text(fax.get("received_at"), _tz())), esc(from_text(fax))]
    if reason:
        cells.append(ui.label(REASON_LABELS, fax.get("reason")))
    else:
        cells.append("Filed" if fax.get("state") == "filed" else "")
    cells.append(esc(fax.get("pages")))
    tds = "".join(f"<td>{c}</td>" for c in cells)
    return (f"<tr data-fax-id=\"{esc(fax_id)}\">{tds}"
            f"<td>{_open_form(lifecycle, fax_id)}</td></tr>")


def _list_call(queue_id: uuid.UUID) -> FaxCall:
    async def call(fax: Any, context: Any) -> Any:
        return await fax.list(context, queue_id)

    return call


def _tz() -> str:
    return str(SETTINGS.get("clinic_tz", "America/New_York"))


def _faxes(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = value.get("faxes")
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []


async def list_page(lifecycle: Any) -> Response:
    try:
        queues = _queue_map(await _fax(lifecycle, "fax_read", lambda f, c: f.queues(c)))
        sorting = _faxes(await _fax(lifecycle, "fax_read", lambda f, c: f.list(c, None)))
        per_queue: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
        for queue in queues:
            qid = uuid.UUID(str(queue.get("queue_id")))
            listed = await _fax(lifecycle, "fax_read", _list_call(qid))
            per_queue.append((queue, _faxes(listed)))
    except _Refused as refused:
        return ui._error_page(refused.status)
    head = ("<tr><th>Received</th><th>From</th><th>{}</th><th>Pages</th><th></th></tr>")
    sort_rows = "".join(_row(lifecycle, f, reason=True) for f in sorting)
    parts = [f"<h1>{esc(ENTRY_LABEL)}</h1>",
             "<section id=\"needs-sorting\">"
             f"<h2>{esc(SORTING_HEADING)} ({len(sorting)})</h2>"
             f"<table>{head.format('Reason')}{sort_rows}</table></section>"]
    for queue, rows in per_queue:
        marker = "" if queue.get("active") is True else " (inactive)"
        body_rows = "".join(_row(lifecycle, f, reason=False) for f in rows)
        parts.append(f"<section data-queue-id=\"{esc(queue.get('queue_id'))}\">"
                     f"<h2>{esc(queue.get('name'))}{esc(marker)}</h2>"
                     f"<table>{head.format('Status')}{body_rows}</table></section>")
    parts.append(f"<p><a href=\"{ui.UI_PREFIX}\">Follow-up items</a></p>")
    return ui._page("".join(parts))


def _select(queues: Sequence[Mapping[str, Any]], exclude: object) -> str:
    options = "".join(
        f"<option value=\"{esc(q.get('queue_id'))}\">{esc(q.get('name'))}</option>"
        for q in queues if q.get("active") is True and q.get("queue_id") != exclude)
    return f"<select name=\"queue_id\">{options}</select>"


def _actions(lifecycle: Any, fax: Mapping[str, Any],
             queues: Sequence[Mapping[str, Any]]) -> list[str]:
    fax_id = esc(fax.get("fax_id"))
    base = (_hidden("csrf", lifecycle.csrf_token or "")
            + _hidden("expected_version", fax.get("version")))
    state = fax.get("state")
    if state == "routed":
        return [f"<form method=\"post\" action=\"{UI_PREFIX}/{fax_id}/move\">{base}"
                f"<label>Move to {_select(queues, fax.get('queue_id'))}</label>"
                "<button type=\"submit\">Move</button></form>",
                f"<form method=\"post\" action=\"{UI_PREFIX}/{fax_id}/file\">{base}"
                "<button type=\"submit\">Mark filed</button></form>"]
    if state == "needs_sorting":
        return [f"<form method=\"post\" action=\"{UI_PREFIX}/{fax_id}/sort\">{base}"
                f"<label>Sort into {_select(queues, None)}</label>"
                "<button type=\"submit\">Sort</button></form>"]
    return []


def _history_rows(history: Sequence[Mapping[str, Any]], names: Mapping[str, str],
                  queue_names: Mapping[str, str]) -> str:
    out = []
    for row in sorted(history, key=_seq):
        actor = row.get("actor")
        who = SYSTEM_ACTOR if actor is None else names.get(str(actor), UNKNOWN_STAFF)
        what = _HISTORY_LABELS.get(str(row.get("action")), UNRECOGNIZED)
        to = row.get("to_queue_id")
        where = f" — {queue_names[str(to)]}" if to is not None and str(to) in queue_names \
            else ""
        out.append(f"<li data-history-seq=\"{esc(_seq(row))}\">"
                   f"{esc(when_text(row.get('at'), _tz()))} — {esc(what)}{esc(where)}"
                   f" — {esc(who)}</li>")
    return f"<ol>{''.join(out)}</ol>"


async def _detail(lifecycle: Any, fax_id: uuid.UUID,
                  notice: str | None = None) -> tuple[Mapping[str, Any], str]:
    got = await _fax(lifecycle, "fax_read", lambda f, c: f.get(c, fax_id))
    hist = await _fax(lifecycle, "fax_read", lambda f, c: f.history(c, fax_id))
    queues = _queue_map(await _fax(lifecycle, "fax_read", lambda f, c: f.queues(c)))
    fax = got.get("fax") if isinstance(got.get("fax"), Mapping) else {}
    assert isinstance(fax, Mapping)
    rows = [r for r in hist.get("history") or [] if isinstance(r, Mapping)]
    names = await _names(lifecycle, [str(r["actor"]) for r in rows
                                     if type(r.get("actor")) is str])
    queue_names = {str(q.get("queue_id")): str(q.get("name")) for q in queues}
    state = fax.get("state")
    queue = queue_names.get(str(fax.get("queue_id")), "")
    status = {"routed": f"In {queue}", "filed": f"Filed in {queue}",
              "needs_sorting": SORTING_HEADING}.get(str(state), UNRECOGNIZED)
    parts = ["<h1>Fax</h1>"]
    if notice is not None:
        parts.append(f"<div class=\"message\" role=\"alert\"><p>{esc(CONFLICT_LEAD)}</p>"
                     f"<p>{esc(notice)}</p>"
                     f"<p><a href=\"{UI_PREFIX}/{esc(fax_id)}\">{esc(REFRESH_LABEL)}</a></p>"
                     "</div>")
    if state == "filed":
        role = "" if notice is not None else " role=\"alert\""
        parts.append(f"<p class=\"message\"{role}>{esc(FILED_NOTICE)}</p>")
    details = [("Received", when_text(fax.get("received_at"), _tz())),
               ("From", from_text(fax)), ("Pages", fax.get("pages")), ("Status", status)]
    if state == "needs_sorting":
        details.append(("Reason", REASON_LABELS.get(str(fax.get("reason")), UNRECOGNIZED)))
    parts.append("<dl>" + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in details)
                 + "</dl>")
    parts.append("<p>Page images: synthetic sample (no real document).</p>")
    parts.extend(_actions(lifecycle, fax, queues))
    parts.append(f"<h2>History</h2>{_history_rows(rows, names, queue_names)}")
    parts.append(f"<p><a href=\"{UI_PREFIX}\">{esc(ENTRY_LABEL)}</a></p>")
    return {"fax": fax, "history": rows, "names": names, "queues": queue_names}, "".join(parts)


def _path_fax(request: Request) -> uuid.UUID | None:
    value = request.path_params.get("fax_id")
    if type(value) is not str or not _UUID.fullmatch(value):
        return None
    return uuid.UUID(value)


async def detail_page(lifecycle: Any, request: Request) -> Response:
    fax_id = _path_fax(request)
    if fax_id is None:
        return ui._error_page(404)
    try:
        _data, body = await _detail(lifecycle, fax_id)
    except _Refused as refused:
        return ui._error_page(refused.status)
    return ui._page(body)


async def action(lifecycle: Any, endpoint: str, request: Request) -> Response:
    """Form checks before any principal resolution or service call: path, byte cap, raw form
    rules (unknown or duplicate fields 400), csrf (403), required fields and values (400)."""
    fax_id = _path_fax(request)
    if fax_id is None:
        return ui._error_page(404)
    allowed, _optional = FIELDS[endpoint]
    try:
        raw = await ui._bounded_body(request)
        # an empty body is a form with no fields (then refused 403 for the missing csrf)
        fields = {} if raw == b"" else ui.parse_form(raw, allowed=allowed,
                                                     max_fields=len(allowed))
    except ui.FormRefused as refused:
        return ui._error_page(refused.status)
    if "csrf" not in fields or not ui._csrf_ok(fields["csrf"], lifecycle.csrf_token):
        return ui._error_page(403)
    if set(fields) != allowed:
        return ui._error_page(400)
    target = f"{UI_PREFIX}/{fax_id}"
    queue_text: str | None = None
    version = 0
    try:
        if endpoint == "open":
            await _fax(lifecycle, "fax_read", lambda f, c: f.open(c, fax_id))
            return RedirectResponse(target, status_code=303)
        if not _VERSION.fullmatch(fields["expected_version"]):
            return ui._error_page(400)
        version = int(fields["expected_version"])
        queue_text = fields.get("queue_id")
        if queue_text is not None and not _UUID.fullmatch(queue_text):
            return ui._error_page(400)
        command = FaxAction(endpoint, fax_id, version,
                            None if queue_text is None else uuid.UUID(queue_text))
        await _fax(lifecycle, "act", lambda f, c: f.act(c, command))
        return RedirectResponse(target, status_code=303)
    except _Refused as refused:
        if refused.status != 409:
            return ui._error_page(refused.status)
    services = lifecycle.services
    principal = services.staff_id if services is not None else None
    try:
        data, _body = await _detail(lifecycle, fax_id)
        line = conflict_notice(endpoint, queue_text, version, data["fax"], data["history"],
                               data["names"], data["queues"], _tz(), principal)
        _data, body = await _detail(lifecycle, fax_id, line)
    except _Refused as refused:
        return ui._error_page(refused.status)
    return ui._page(body, 409)


def install_fax_ui(app: FastAPI, lifecycle: Any) -> None:
    async def listing(request: Request) -> Response:
        return await list_page(lifecycle)

    async def detail(request: Request) -> Response:
        return await detail_page(lifecycle, request)

    app.add_api_route(UI_PREFIX, listing, methods=["GET"], response_class=Response,
                      include_in_schema=False)
    app.add_api_route(UI_PREFIX + "/{fax_id}", detail, methods=["GET"],
                      response_class=Response, include_in_schema=False)
    for endpoint in FIELDS:
        app.add_api_route(UI_PREFIX + "/{fax_id}/" + endpoint, _post(lifecycle, endpoint),
                          methods=["POST"], response_class=Response, include_in_schema=False)


def _post(lifecycle: Any, endpoint: str) -> Callable[[Request], Awaitable[Response]]:
    """The endpoint is bound by closure (never a parameter FastAPI could fill from input)."""
    async def handler(request: Request) -> Response:
        return await action(lifecycle, endpoint, request)

    return handler


__all__ = ["ENTRY_LABEL", "REASON_LABELS", "UI_PREFIX", "conflict_notice", "from_text",
           "holds_fax_read", "install_fax_ui", "when_text"]
