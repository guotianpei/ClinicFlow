"""The eligibility parts of the synthetic staff screen (eligibility E4, v1 D-E4-5).
SYNTHETIC ONLY: served on an eligibility-profile app only.

For an eligibility item the item view shows the OD-EL11 title (ui.KIND_LABELS), the case's
OD-EL8 reason, the OD-EL4 summary (approved field names; never coinsurance, deductible
total, a transaction reference, identity or the raw response), the case notes (an active
later check as the approved OD-EL3 "Later check: active", otherwise its reason label; never
note text, OD-D2), and two forms carrying the E3b view token (case_id, item_id,
case_version) as hidden fields:
- contact: the four OD-EL7 entries (non-closing, no note);
- close: the six OD-EL6 outcomes, `el_other` with a note (protected-note path).
Both posts are checked like the B4 forms (byte cap, raw form rules, CSRF, required fields, the
token) before any service call, then executed through `staff_routes.execute`. A stale token
(CASE_CHANGED) answers 409 with the OD-B4-2 reload message and changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from starlette.requests import Request
from starlette.responses import Response

from haloflow.m05.eligibility_labels import CONTACT_LABELS, OUTCOME_LABELS, REASON_LABELS
from haloflow.platform.routes import _Malformed, _path_uuid
from haloflow.synthetic import ui
from haloflow.synthetic.staff_routes import _eligibility_resolve, _log_contact, execute

LATER_ACTIVE: Final = "Later check: active"          # OD-EL3 (Rachel 2026-11-18), exact
# OD-EL4 (Rachel 2026-11-18) field names, in the approved order; check time last.
SUMMARY_LABELS: Final = (("payer_name", "Payer name"), ("plan", "Plan/product"),
                         ("copay", "Copay"), ("deductible_remaining", "Deductible remaining"),
                         ("referral_required", "Referral/authorization"),
                         ("auth_required", "Referral/authorization"),
                         ("checked_at", "Checked at"))
CONTACT_FIELDS: Final = frozenset({"csrf", "client_action_id", "expected_version",
                                   "contact_code", *ui.TOKEN_FIELDS})
CLOSE_REQUIRED: Final = frozenset({"csrf", "type", "client_action_id", "expected_version",
                                   "outcome_code", *ui.TOKEN_FIELDS})
CLOSE_TYPES: Final = frozenset({"resolve", "claim_and_resolve"})
MAX_FIELDS: Final = 9


def _hidden(name: str, value: object) -> str:
    return f"<input type=\"hidden\" name=\"{ui.esc(name)}\" value=\"{ui.esc(value)}\">"


def _summary_lines(summary: object) -> list[str]:
    """The OD-EL4 summary: present fields only, in the approved order; the referral and
    authorization indicators share one line (yes when either is set)."""
    if not isinstance(summary, Mapping):
        return []
    out: list[str] = []
    for key, text in SUMMARY_LABELS:
        if key == "auth_required" or key not in summary and key != "referral_required":
            continue
        if key == "referral_required":
            flags = [summary.get(k) for k in ("referral_required", "auth_required")
                     if isinstance(summary.get(k), bool)]
            if not flags:
                continue
            value: object = "yes" if any(flags) else "no"
        elif key == "checked_at":
            value = ui._when(summary[key])
        else:
            value = summary[key]
        out.append(f"<p>{ui.esc(text)}: {ui.esc(value)}</p>")
    return out


def _note_line(note: object) -> str:
    if not isinstance(note, Mapping):
        return ""
    when = ui.esc(ui._when(note.get("at")))
    if note.get("class") == "active":
        return f"<li>{ui.esc(LATER_ACTIVE)} — {when}</li>"
    return f"<li>{ui.label(REASON_LABELS, note.get('reason'))} — {when}</li>"


def section(lifecycle: Any, item: Mapping[str, Any], case: Mapping[str, Any]) -> list[str]:
    """The eligibility part of the item view (title rendered by the caller)."""
    parts = [f"<p>Reason: {ui.label(REASON_LABELS, case.get('reason'))}</p>"]
    parts.extend(_summary_lines(case.get("summary")))
    notes = [_note_line(n) for n in case.get("notes") or [] if isinstance(n, Mapping)]
    if notes:
        parts.append(f"<h2>Case notes</h2><ul>{''.join(notes)}</ul>")
    if item.get("state") not in ("open", "in_progress"):
        return parts
    services = lifecycle.services
    principal = str(services.staff_id) if services is not None else None
    assignee = item.get("assignee_id")
    if assignee is not None and str(assignee) != principal:
        return parts
    token = "".join(_hidden(n, v) for n, v in ui.case_token_fields(
        str(case.get("case_id")), str(item.get("item_id")), int(case.get("case_version", 0))))
    base = (_hidden("csrf", lifecycle.csrf_token or "")
            + _hidden("client_action_id", str(uuid.uuid4()))
            + _hidden("expected_version", str(item.get("version"))) + token)
    item_path = f"{ui.UI_PREFIX}/{ui.esc(item.get('item_id'))}"
    contacts = "".join(
        f"<p><label><input type=\"radio\" name=\"contact_code\" value=\"{ui.esc(code)}\">"
        f" {ui.esc(text)}</label></p>" for code, text in CONTACT_LABELS.items())
    parts.append(f"<form method=\"post\" action=\"{item_path}/contact\">{base}{contacts}"
                 "<button type=\"submit\">Log contact</button></form>")
    close_type = "claim_and_resolve" if assignee is None else "resolve"
    outcomes = "".join(
        f"<p><label><input type=\"radio\" name=\"outcome_code\" value=\"{ui.esc(code)}\">"
        f" {ui.esc(text)}</label></p>" for code, text in OUTCOME_LABELS.items())
    parts.append(f"<form method=\"post\" action=\"{item_path}/close\">{base}"
                 f"{_hidden('type', close_type)}{outcomes}"
                 "<p><label>Note <textarea name=\"note\" maxlength=\"500\"></textarea></label>"
                 "</p><button type=\"submit\">Close</button></form>")
    return parts


async def contact_history(lifecycle: Any, context: Any, item_id: Any) -> dict[int, str]:
    """OD-EL12: the t127 read (staff READ under the item read's resolution): {history seq:
    contact_code} of this eligibility item. Anything malformed or ITEM_NOT_FOUND -> {}."""
    from haloflow.synthetic.el4_statements import CONTACT_HISTORY_KEY

    async def run(handle: Any) -> Any:
        return await handle.fetch_all(CONTACT_HISTORY_KEY, {"item_id": str(item_id)})

    rows = await lifecycle.services.gateway.with_tenant_transaction(context, run)
    value = rows[0][0] if rows and rows[0] else None
    out: dict[int, str] = {}
    if isinstance(value, Mapping) and isinstance(value.get("entries"), list):
        for entry in value["entries"]:
            if isinstance(entry, Mapping) and type(entry.get("seq")) is int \
                    and type(entry.get("contact_code")) is str:
                out[entry["seq"]] = entry["contact_code"]
    return out


def contact_line(code: object, actor: str, when: str) -> str:
    """A contact history line: the OD-EL7 label (unknown -> "Unrecognized")."""
    return f"{ui.label(CONTACT_LABELS, code)} — {ui.esc(actor)}, {ui.esc(when)}"


def outcome_text(code: object) -> str:
    """A closed eligibility item's history line label (OD-EL6)."""
    return ui.label(OUTCOME_LABELS, code)


async def action(lifecycle: Any, endpoint: str, request: Request) -> Response:
    """contact | close: refusals before any principal resolution or service call."""
    try:
        item_id = _path_uuid(request.path_params["item_id"], "item_id")
    except _Malformed:
        return ui._error_page(404)
    allowed = CONTACT_FIELDS if endpoint == "contact" else CLOSE_REQUIRED | {"note"}
    required = CONTACT_FIELDS if endpoint == "contact" else CLOSE_REQUIRED
    try:
        raw = await ui._bounded_body(request)
        fields = ui.parse_form(raw, allowed=allowed, max_fields=MAX_FIELDS)
    except ui.FormRefused as refused:
        return ui._error_page(refused.status)
    if "csrf" not in fields or not ui._csrf_ok(fields["csrf"], lifecycle.csrf_token):
        return ui._error_page(403)
    if any(name not in fields for name in required):
        return ui._error_page(400)
    try:
        case_id, token_item, case_version = ui.parse_case_token(fields)
    except ValueError:
        return ui._error_page(400)
    if token_item != str(item_id):
        return ui._error_page(400)
    version = fields["expected_version"]
    value: dict[str, Any] = {
        "client_action_id": fields["client_action_id"],
        "expected_version": int(version) if ui._VERSION.fullmatch(version) else version,
        "case_token": {"case_id": case_id, "item_id": token_item,
                       "case_version": case_version}}
    try:
        if endpoint == "contact":
            value["contact_code"] = fields["contact_code"]
            command: Any = _log_contact(item_id, value)
        else:
            if fields["type"] not in CLOSE_TYPES:
                return ui._error_page(400)
            note = fields.get("note")
            value.update(type=fields["type"], outcome_code=fields["outcome_code"],
                         note=note if note else None)
            command = _eligibility_resolve(item_id, value)
    except _Malformed:
        return ui._error_page(400)

    async def act(m05: Any, ctx: Any) -> dict[str, Any]:
        result = await m05.act(ctx, command)
        return {"outcome": result.outcome}

    outcome = await execute(lifecycle, "contact" if endpoint == "contact" else "resolve", act)
    if not isinstance(outcome, Response):
        return ui._redirect(item_id)
    code = ui._code(outcome)
    if code in ("NOT_FOUND", "ITEM_NOT_FOUND"):
        return ui._error_page(404)
    if code == "CASE_CHANGED":
        return ui._page(f"<p class=\"message\">{ui.esc(ui.MESSAGES['conflict'])}</p>"
                        f"<p><a href=\"{ui.UI_PREFIX}/{item_id}\">Reload</a></p>", 409)
    if outcome.status_code == 409 and code in ui._SERVICE_MESSAGES:
        return ui._redirect(item_id, ui._SERVICE_MESSAGES[code])
    if outcome.status_code in (409, 403):
        return ui._redirect(item_id, "refused")
    if code == "MALFORMED":
        return ui._error_page(400)
    return ui._redirect(item_id, "unavailable")
