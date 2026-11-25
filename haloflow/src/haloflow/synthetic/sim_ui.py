"""The B5 simulator page and its form adapter (Option B B5). SYNTHETIC ONLY.

`GET /synthetic/simulator` renders, SIMULATED, the four scenarios with their state (one staff
READ of t097 through the B4 two-step staff resolution), the three tick buttons, the patient
reply form, the simulated EHR (entries visible only for refs present in the database) and
the outbox (FakeSender calls: nothing was sent).

The four `POST /synthetic/simulator/ui/<action>` routes are the form adapter. Every refusal
before the action (413, a body or field-set refusal 400, CSRF missing or wrong 403; 415 and
the host/origin checks are the gate's) is a B4 HTML error page and happens before any
principal resolution or action. Then the adapter only parses the form into the SAME action
function the JSON route calls (`sim_actions.load` / `tick`, `simulator.deliver_reply` /
`apply_ehr`, each writing its own audit line) and maps the result: a 4xx refusal -> 422 HTML
error page; a load code in CODE_MESSAGE -> 303 `?m=<key>`; 503 -> `?m=stopping`; success ->
303 with `m=loaded` (load), `m=tick` (tick), no `m` (reply, EHR).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from fastapi import FastAPI
from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from haloflow.m05.simulated_ehr import STATUSES
from haloflow.synthetic import scenarios, sim_actions, simulator
from haloflow.synthetic.display_statements import PRESENT_KEY
from haloflow.synthetic.staff_routes import execute
from haloflow.synthetic.ui import (
    FormRefused,
    _bounded_body,
    _csrf_ok,
    _error_page,
    _page,
    esc,
    parse_form,
)

PAGE_ROUTE: Final = "/synthetic/simulator"
FORM_ROUTES: Final = {k: f"{PAGE_ROUTE}/ui/{k}" for k in ("load", "tick", "reply", "ehr")}

# OD-B5-2 (Rachel 2026-11-17, message 6 replaced: "State the hours").
MESSAGES: Final[dict[str, str]] = {
    "loaded": "Scenario loaded.",
    "tick": "Tick finished.",
    "stopping": "The app is stopping. Try again after restart.",
    "outbox_empty": "Outbox is empty. It resets when the app restarts.",
    "present": "This scenario is already loaded. Reset to load it again.",
    "hours": "Scenarios can be loaded between 11:00 AM and 4:00 PM Eastern. Try again then.",
    "failed": "The scenario did not load. Try again.",
    "unknown": "Not sure the scenario loaded. Load it again to check.",
}
PARTIAL_TEXT: Final = "Scenario data is incomplete. Reset to run scenarios again."
RESTART_TEXT: Final = ("The simulated EHR for {scenario} was cleared by a restart. Reset to run"
                       " scenarios again.")
LOADED_LABEL: Final = "Loaded"
HEADINGS: Final = ("Scenarios", "Run a tick now", "Simulate patient reply", "Simulated EHR",
                   "Outbox (nothing was sent)")
TICK_BUTTONS: Final = {"due": "Due windows", "reminder": "Reminders", "writeback": "Write-back"}
LOAD_BUTTON: Final = "Load"
CODE_MESSAGE: Final = {"SCENARIO_PRESENT": "present", "OUTSIDE_LOAD_HOURS": "hours",
                       "SCENARIO_LOAD_FAILED": "failed", "SCENARIO_LOAD_UNKNOWN": "unknown"}

_EHR_FIELDS: Final = frozenset({"operation", "appointment_id", "status", "start_at",
                                "provider_ref", "department_ref", "replaced_by",
                                "reads", "writes"})
FORM_FIELDS: Final[dict[str, frozenset[str]]] = {
    "load": frozenset({"csrf", "scenario"}),
    "tick": frozenset({"csrf", "scheduler"}),
    "reply": frozenset({"csrf", "phone", "text"}),
    "ehr": frozenset({"csrf"}) | _EHR_FIELDS,
}
_DIGITS: Final = re.compile(r"[0-9]{1,9}")


# ---------------------------------------------------------------------------- page

def _redirect(message: str | None) -> Response:
    return RedirectResponse(PAGE_ROUTE + (f"?m={message}" if message else ""), status_code=303)


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def _form(action: str, csrf: str, inner: str, button: str) -> str:
    return (f'<form method="post" action="{esc(FORM_ROUTES[action])}">{_hidden("csrf", csrf)}'
            f'{inner}<button type="submit">{esc(button)}</button></form>')


def _listed(lifecycle: Any) -> dict[str, Any]:
    """The scenarios this app lists: S1..S4, plus E1..E5 on an eligibility-profile app (E4)."""
    eligibility = (getattr(lifecycle, "install_profile", None)
                   or getattr(lifecycle, "profile", "default")) in ("eligibility", "fax")
    return {name: spec for name, spec in scenarios.fixtures().items()
            if eligibility or name not in scenarios.EL_NAMES}


def _all_refs(tenant: str, lifecycle: Any = None) -> dict[str, list[str]]:
    return {name: [str(scenarios.ids(tenant, name, row.key)[1]) for row in spec.rows]
            for name, spec in _listed(lifecycle).items()}


async def _present(lifecycle: Any, refs: list[str]) -> Response | frozenset[str]:
    """One staff READ of t097 (route `get`, READ capability)."""
    out: list[frozenset[str]] = []

    async def call(_m05: Any, context: Any) -> dict[str, Any]:
        async def run(handle: Any) -> Any:
            return await handle.fetch_all(PRESENT_KEY, {"refs": Jsonb(refs)})

        rows = await lifecycle.services.gateway.with_tenant_transaction(context, run)
        value = rows[0][0] if len(rows) == 1 else None
        if not isinstance(value, Mapping) or value.get("schema_version") != 1 \
                or not isinstance(value.get("present"), list):
            raise RuntimeError("present read invalid")
        out.append(frozenset(str(v) for v in value["present"]))
        return {}

    result = await execute(lifecycle, "get", call)
    if isinstance(result, Response):
        return result
    return out[0]


def _scenario_rows(lifecycle: Any, csrf: str, refs: dict[str, list[str]],
                   present: frozenset[str], ehr: frozenset[str]) -> str:
    tenant = str(lifecycle.services.tenant_id)
    rows = []
    for name, spec in _listed(lifecycle).items():
        backed = frozenset(str(scenarios.ids(tenant, name, r.key)[1]) for r in spec.rows if r.ehr)
        state = scenarios.scenario_state(present, ehr, frozenset(refs[name]), backed)
        if state == "loaded":
            cell = esc(LOADED_LABEL)
        elif state == "partial":
            cell = esc(PARTIAL_TEXT)
        elif state == "ehr_lost":
            cell = esc(RESTART_TEXT.format(scenario=name))
        else:
            cell = _form("load", csrf, _hidden("scenario", name), LOAD_BUTTON)
        rows.append(f"<tr><td>{esc(name)}</td><td>{cell}</td></tr>")
    return "<table><tr><th>Scenario</th><th>State</th></tr>" + "".join(rows) + "</table>"


async def page(lifecycle: Any, request: Request) -> Response:
    if lifecycle.services is None or lifecycle.csrf_token is None:
        return _error_page(503)
    csrf = str(lifecycle.csrf_token)
    tenant = str(lifecycle.services.tenant_id)
    refs = _all_refs(tenant, lifecycle)
    present = await _present(lifecycle, [r for name in refs for r in refs[name]])
    if isinstance(present, Response):
        return _error_page(present.status_code if 400 <= present.status_code < 600 else 500)
    snapshot = lifecycle.port.ehr.snapshot()
    ehr_refs = frozenset(str(k) for k in snapshot.appointments)
    key = request.query_params.get("m")
    parts = []
    if key in MESSAGES and key != "outbox_empty":
        parts.append(f'<p class="message">{esc(MESSAGES[key])}</p>')
    parts.append(f"<h2>{esc(HEADINGS[0])}</h2>")
    parts.append(_scenario_rows(lifecycle, csrf, refs, present, ehr_refs))
    parts.append(f"<h2>{esc(HEADINGS[1])}</h2>")
    parts.extend(_form("tick", csrf, _hidden("scheduler", name), label)
                 for name, label in TICK_BUTTONS.items())
    parts.append(f"<h2>{esc(HEADINGS[2])}</h2>")
    parts.append(_form("reply", csrf,
                       '<p><label>Phone <input type="text" name="phone"></label></p>'
                       '<p><label>Text <input type="text" name="text"></label></p>', "Send reply"))
    parts.append(f"<h2>{esc(HEADINGS[3])}</h2>")
    visible = sorted(scenarios.visible_ehr(present, ehr_refs))
    if visible:
        parts.append("<table><tr><th>Appointment</th><th>Status</th><th>Start</th></tr>"
                     + "".join(f"<tr><td>{esc(ref)}</td>"
                               f"<td>{esc(snapshot.appointments[ref].status)}</td>"
                               f"<td>{esc(snapshot.appointments[ref].start_at.isoformat())}</td>"
                               "</tr>" for ref in visible) + "</table>")
    options = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in sorted(STATUSES))
    parts.append(_form("ehr", csrf, _hidden("operation", "put_appointment")
                       + '<p><label>Appointment <input type="text" name="appointment_id">'
                       "</label></p>"
                       f'<p><label>Status <select name="status">{options}</select></label></p>'
                       '<p><label>Start <input type="text" name="start_at"></label></p>',
                       "Set appointment"))
    parts.append(_form("ehr", csrf, _hidden("operation", "fail_next")
                       + '<p><label>Fail next reads <input type="text" name="reads" value="0">'
                       "</label></p>"
                       '<p><label>Fail next writes <input type="text" name="writes" value="0">'
                       "</label></p>", "Set failures"))
    parts.append(f"<h2>{esc(HEADINGS[4])}</h2>")
    sent = sim_actions.outbox(lifecycle)
    if sent:
        parts.append("<table><tr><th>Phone</th><th>Text</th></tr>"
                     + "".join(f"<tr><td>{esc(o['phone'])}</td><td>{esc(o['text'])}</td></tr>"
                               for o in sent) + "</table>")
    else:
        parts.append(f"<p>{esc(MESSAGES['outbox_empty'])}</p>")
    return _page("".join(parts))


# ---------------------------------------------------------------------------- form adapter

async def _fields(lifecycle: Any, action: str, request: Request) -> dict[str, str] | Response:
    try:
        raw = await _bounded_body(request)
        fields = parse_form(raw, allowed=FORM_FIELDS[action],
                            max_fields=len(FORM_FIELDS[action]))
    except FormRefused as refused:
        return _error_page(refused.status)
    if "csrf" not in fields or not _csrf_ok(fields["csrf"], lifecycle.csrf_token):
        return _error_page(403)
    return {k: v for k, v in fields.items() if k != "csrf"}


def _mapped(result: sim_actions.Result, success: str | None) -> Response:
    if 200 <= result.status < 300:
        return _redirect(success)
    if result.code in CODE_MESSAGE:
        return _redirect(CODE_MESSAGE[result.code])
    if result.status == 503:
        return _redirect("stopping")
    if 400 <= result.status < 500:
        return _error_page(422)
    return _error_page(result.status if 500 <= result.status < 600 else 500)


async def form(lifecycle: Any, action: str, request: Request) -> Response:
    fields = await _fields(lifecycle, action, request)
    if isinstance(fields, Response):
        return fields
    if action == "load":
        refused = sim_actions.load_value(lifecycle, fields)
        if refused is not None:
            return _mapped(refused, None)
        return _mapped(await sim_actions.load(lifecycle, fields["scenario"]), "loaded")
    if action == "tick":
        refused = sim_actions.tick_value(fields)
        if refused is not None:
            return _mapped(refused, None)
        return _mapped(await sim_actions.tick(lifecycle, fields["scheduler"]), "tick")
    if action == "reply":
        shaped = simulator.reply_value(fields)
        if isinstance(shaped, simulator._Bad):
            return _mapped(simulator._refused_reply(shaped.field), None)
        return _mapped(await simulator.deliver_reply(lifecycle, *shaped), None)
    value: dict[str, object] = {k: (int(v) if k in ("reads", "writes") and _DIGITS.fullmatch(v)
                                    else v) for k, v in fields.items()}
    return _mapped(simulator.apply_ehr(lifecycle, value), None)


def install_sim_ui(app: FastAPI, lifecycle: Any) -> None:
    async def page_endpoint(request: Request) -> Response:
        return await page(lifecycle, request)

    app.add_api_route(PAGE_ROUTE, page_endpoint, methods=["GET"], response_class=Response,
                      include_in_schema=False)
    def endpoint_for(name: str) -> Any:
        async def endpoint(request: Request) -> Response:
            return await form(lifecycle, name, request)

        return endpoint

    for name, path in FORM_ROUTES.items():
        app.add_api_route(path, endpoint_for(name), methods=["POST"], response_class=Response,
                          include_in_schema=False)
    if getattr(lifecycle, "profile", "default") in ("eligibility", "fax"):  # E4 (D-E4-4)
        async def clearinghouse_endpoint(request: Request) -> Response:
            return await clearinghouse_form(lifecycle, request)

        app.add_api_route(CLEARINGHOUSE_FORM, clearinghouse_endpoint, methods=["POST"],
                          response_class=Response, include_in_schema=False)


# ---------------------------------------------------------------------------- E4 clearinghouse

CLEARINGHOUSE_FORM: Final = f"{PAGE_ROUTE}/ui/clearinghouse"
CLEARINGHOUSE_FIELDS: Final = frozenset({"csrf", "op", "member_id", "status", "error", "n"})


async def clearinghouse_form(lifecycle: Any, request: Request) -> Response:
    """The form twin of `POST /synthetic/simulator/clearinghouse` (eligibility profile only):
    op=member (member_id, status, optional error) | op=fail_next / malformed_next (n)."""
    try:
        raw = await _bounded_body(request)
        fields = parse_form(raw, allowed=CLEARINGHOUSE_FIELDS,
                            max_fields=len(CLEARINGHOUSE_FIELDS))
    except FormRefused as refused:
        return _error_page(refused.status)
    if not _csrf_ok(fields.get("csrf"), lifecycle.csrf_token):
        return _error_page(403)
    op = fields.get("op")
    value: object
    if op == "member":
        member = {k: fields[k] for k in ("member_id", "status", "error") if k in fields}
        value = {"member": member}
    elif op in ("fail_next", "malformed_next") and _DIGITS.fullmatch(fields.get("n", "")):
        value = {op: int(fields["n"])}
    else:
        value = None
    return _mapped(sim_actions.clearinghouse(lifecycle, value), None)
