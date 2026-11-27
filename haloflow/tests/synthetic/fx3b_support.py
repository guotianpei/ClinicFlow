"""Incoming fax routing FX-IN, package FX3b (fax staff screen): shared test support. TEST ONLY;
never imported by `src`. RED BY DESIGN.

Governing records (Shared Workspace; project docs):
- `Work Session 2026-11-26/claude_fx3b-staff-screen-package-v1-2026-11-26.md` (package v1)
- Codex `codex_fx3b-v1-and-tracker-review.md` (ALIGN for red tests after #118, with the test
  refinements), `codex_fx3b-test-preparation.md`, `codex_fx3b-conflict-fixtures.md` (cases 0-5)
- Owner decisions OD-FX7 (reason labels), OD-FX8/FX9 (filed is final; sorting section first),
  OD-FX10 (sender display), OD-FX11 (actor-only fax access), OD-FX12 (filed notice; move-versus-
  move notice; "Refresh this fax"), OD-FX13 (conflict notices 1-5)

Red classes (as B4 / FX5): every row states its class in its docstring.
- C  control: passes now; proves a fixture or an oracle.
- A  absence-red: fails in its own body on the missing FX3b interface (ModuleNotFoundError for
     `haloflow.synthetic.fax_ui`, or the /synthetic/ui/fax routes not served: 404).

Oracles are independent of the code under test: pages are parsed with the stdlib HTMLParser
(this module's own parser: it also records select options, the role="alert" notice and the
data-fax-id / data-queue-id regions), state is read with the admin connection, and the competing
staff member acts through the installed action gateway (`m05_fx_staff_action_v2`) on a runtime
connection, as the FX2 rows do at module level.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Final

import b3a_support as b3
import b3b_support as bb
import b4_support as b4
import psycopg
from psycopg.types.json import Jsonb

# ---------------------------------------------------------------------------- pinned interface

INTERFACE: Final = """
haloflow.synthetic.fax_ui (new module):
  UI_PREFIX = "/synthetic/ui/fax"
  ENTRY_LABEL = "Incoming faxes"          the staff-screen entry (OD-FX11: act staff only)
  SORTING_HEADING = "Fax needs sorting"   rendered as "Fax needs sorting (N)"
  REASON_LABELS: dict[str, str]           total over m05.fax_units.REASONS (OD-FX7)
  UNKNOWN_SENDER = "Unknown sender"; SENDER_HIDDEN = "Sender number not shown"
  AS_SENT = "(as sent)"; ENDING = " · ending "
  SYSTEM_ACTOR = "System"; UNKNOWN_STAFF = "Unknown staff member"
  FILED_NOTICE: str                       OD-FX12 board 3, exact
  CONFLICT_LEAD: str                      OD-FX12 board 4 first line, exact
  REFRESH_LABEL = "Refresh this fax"
  from_text(fax: Mapping) -> str          OD-FX10 From column (sender_display + sender_last4)
  when_text(at: str, clinic_tz: str) -> str
      ISO-8601 UTC ("...Z") -> clinic time, "Nov 24, 5:12 PM" (no leading zero on the day or
      the hour); anything unparseable -> "Unrecognized"
  conflict_notice(attempt, attempted_queue_id, expected_version, fax, history, names,
                  queue_names, clinic_tz) -> str
      attempt in {"move", "file", "sort"}; fax = the re-read `get` summary; history = the
      re-read history rows; names: staff id -> display name; queue_names: queue id -> name.
      Returns the case line that follows CONFLICT_LEAD (OD-FX13 cases 1-5, OD-FX12 case 0).
      A named case (0-4) only when ALL hold: fax.version == expected_version + 1 (exactly one
      state change since the form), the LATEST state row (moved / sorted / filed; `opened` and
      `received_*` rows never count) is the change the case describes, its actor is a staff id,
      and the re-read summary agrees with it (moved / sorted: state "routed" and queue_id ==
      the row's to_queue_id; filed: state "filed").
      Otherwise case 5 ("Check the details below."): no name, queue or time.
Routes (served by create_synthetic_app for the fax profile only, behind the B3b gate):
  GET  /synthetic/ui/fax                       route requirement fax_read
  GET  /synthetic/ui/fax/{fax_id}              fax_read; side-effect free (no history row)
  POST /synthetic/ui/fax/{fax_id}/open         fax_read; form {csrf}; 303 -> the detail page
  POST /synthetic/ui/fax/{fax_id}/move         act; form {csrf, expected_version, queue_id}
  POST /synthetic/ui/fax/{fax_id}/file         act; form {csrf, expected_version}
  POST /synthetic/ui/fax/{fax_id}/sort         act; form {csrf, expected_version, queue_id}
  Success: 303 -> /synthetic/ui/fax/{fax_id}. Refused principal: 403 HTML with no fax data.
  Missing / invalid csrf: 403 before any principal resolution or service call.
  A 409 state refusal (VERSION_CONFLICT, FAX_FILED, ALREADY_SORTED, NOT_ROUTED,
  NOT_NEEDS_SORTING): 409 HTML = the re-read detail page plus the notice.
Markup pins (the oracle reads these, nothing else):
  - every page passes b4.html_ok (banner once, SIMULATED title, security headers, no script);
  - list page: <section id="needs-sorting"> (heading text "Fax needs sorting (N)") BEFORE any
    <section data-queue-id="..."> (one per queue, t151 order); each fax row has data-fax-id and
    holds the Open form (action /synthetic/ui/fax/{id}/open);
  - detail page: the conflict notice and the filed notice are each ONE element with
    role="alert"; the Move / Sort forms carry <select name="queue_id"> whose <option value>s
    are the selectable queue ids; "Refresh this fax" is <a href="/synthetic/ui/fax/{id}">;
  - the staff screen (/synthetic/ui/items) carries <a href="/synthetic/ui/fax">Incoming
    faxes</a> only for a principal holding fax_read.
"""

UI: Final = "/synthetic/ui/fax"
ITEMS: Final = "/synthetic/ui/items"
SIM_ROUTE: Final = "/synthetic/v1/simulator/fax"
JSON_PREFIX: Final = "/synthetic/v1/fax"
FAX: Final = "fax"
CLINIC_TZ: Final = "America/New_York"
DEFAULT_QUEUES: Final = ("Labs/Results", "Imaging", "Referrals & consults", "Records requests",
                         "Insurance/Prior auth", "Pharmacy/Refills", "General")

# ---------------------------------------------------------------------------- approved copy

ENTRY_LABEL: Final = "Incoming faxes"
SORTING_HEADING: Final = "Fax needs sorting"
REASON_LABELS: Final = {                      # OD-FX7
    "no_matching_rule": "No matching rule",
    "sender_not_shown": "Sender number not shown",
    "rules_conflict": "Rules point to different queues",
}
UNKNOWN_SENDER: Final = "Unknown sender"      # OD-FX10
SENDER_HIDDEN: Final = "Sender number not shown"
SYSTEM_ACTOR: Final = "System"
UNKNOWN_STAFF: Final = "Unknown staff member"
FILED_NOTICE: Final = ("This fax has been filed. It stays in the list so you can find it again."
                       " It can't be moved or filed again.")                       # OD-FX12
CONFLICT_LEAD: Final = ("Nothing was changed. This fax was updated by someone else while you had"
                        " it open.")                                               # OD-FX12
REFRESH_LABEL: Final = "Refresh this fax"
CASES: Final = {                                                                   # OD-FX12/13
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


def case_text(case: int, **values: str) -> str:
    return CASES[case].format(**values)


def fax_ui():  # type: ignore[no-untyped-def]
    """The module under test (A rows fail here on main with ModuleNotFoundError)."""
    import importlib

    return importlib.import_module("haloflow.synthetic.fax_ui")


# ---------------------------------------------------------------------------- HTML oracle

@dataclass
class Select:
    name: str
    options: list[str] = field(default_factory=list)


@dataclass
class FForm:
    action: str
    method: str
    fields: dict[str, list[str]] = field(default_factory=dict)
    selects: dict[str, Select] = field(default_factory=dict)

    def hidden(self, name: str) -> str:
        (value,) = self.fields[name]
        return value


@dataclass
class Region:
    """The text of one marked element (by attribute) and the forms inside it."""
    key: str
    text: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[FForm] = []
        self.links: list[tuple[str, list[str]]] = []
        self.alerts: list[list[str]] = []
        self.regions: list[Region] = []      # needs-sorting / queue / fax-row, in document order
        self._stack: list[tuple[str, list[Any]]] = []   # (tag, sinks opened by this tag)
        self._form: FForm | None = None
        self._select: Select | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        sinks: list[Any] = []
        if a.get("role") == "alert":
            self.alerts.append([])
            sinks.append(self.alerts[-1])
        for key in ("id", "data-queue-id", "data-fax-id"):
            if key == "id" and a.get("id") != "needs-sorting":
                continue
            if key in a:
                region = Region(f"{key}={a[key]}")
                self.regions.append(region)
                sinks.append(region)
        if tag == "a" and "href" in a:
            self.links.append((a["href"], []))
            sinks.append(self.links[-1][1])
        if tag == "form":
            self._form = FForm(a.get("action", ""), a.get("method", "").lower())
            self.forms.append(self._form)
            for _t, open_sinks in self._stack:
                for sink in open_sinks:
                    if isinstance(sink, Region):
                        sink.forms.append(self._form.action)
        if self._form is not None:
            name = a.get("name")
            if tag == "input" and name is not None:
                self._form.fields.setdefault(name, []).append(a.get("value", ""))
            if tag == "select" and name is not None:
                self._select = Select(name)
                self._form.selects[name] = self._select
            if tag == "option" and self._select is not None:
                self._select.options.append(a.get("value", ""))
        if tag not in ("input", "br", "img", "meta", "link", "hr", "option"):
            self._stack.append((tag, sinks))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        if tag == "select":
            self._select = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        for _t, sinks in self._stack:
            for sink in sinks:
                (sink.text if isinstance(sink, Region) else sink).append(data)


def _norm(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


@dataclass
class FaxPage:
    status: int
    base: b4.Page
    forms: list[FForm]
    links: list[tuple[str, str]]
    alerts: list[str]
    regions: list[tuple[str, str, list[str]]]   # (key, text, form actions)

    @property
    def text(self) -> str:
        return self.base.text

    def form(self, action: str) -> FForm:
        found = [f for f in self.forms if f.action == action]
        assert len(found) == 1, (action, [f.action for f in self.forms])
        return found[0]

    def has_form(self, action: str) -> bool:
        return any(f.action == action for f in self.forms)

    def region(self, key: str) -> tuple[str, list[str]]:
        found = [(t, f) for k, t, f in self.regions if k == key]
        assert len(found) == 1, (key, [k for k, _t, _f in self.regions])
        return found[0]

    def region_keys(self) -> list[str]:
        return [k for k, _t, _f in self.regions]


def fax_page(r: bb.Resp, *, checked: bool = True) -> FaxPage:
    base = b4.html_ok(r) if checked else b4.page(r)
    p = _Parser()
    p.feed(r.raw.decode("utf-8"))
    p.close()
    return FaxPage(r.status, base, p.forms, [(h, _norm(t)) for h, t in p.links],
                   [_norm(x) for x in p.alerts],
                   [(g.key, _norm(g.text), g.forms) for g in p.regions])


# ---------------------------------------------------------------------------- app helpers

def detail(fax_id: str) -> str:
    return f"{UI}/{fax_id}"


def act_path(fax_id: str, action: str) -> str:
    return f"{UI}/{fax_id}/{action}"


async def get(app: Any, target: str) -> bb.Resp:
    return await bb.asgi(app, "GET", target, headers=bb.get_headers())


async def post_form(app: Any, action: str, fields: list[tuple[str, str]]) -> bb.Resp:
    return await bb.asgi(app, "POST", action, headers=b4.form_headers(),
                         body=b4.form_body(fields))


def submit(f: FForm, **override: str | None) -> list[tuple[str, str]]:
    """The fields a browser submits for form `f` (inputs as rendered; a select contributes
    only through `override`), with overrides (None removes a field)."""
    out = [(k, v) for k, vs in f.fields.items() for v in vs]
    for k, v in override.items():
        out = [(n, val) for n, val in out if n != k]
        if v is not None:
            out.append((k, v))
    return out


async def deliver(app: Any, tok: str, sender: str | None, *,
                  sender_name: str | None = None) -> str:
    payload: dict[str, Any] = {"sender": sender, "pages": 1}
    if sender_name is not None:
        payload["sender_name"] = sender_name
    r = await bb.asgi(app, "POST", SIM_ROUTE, headers=bb.post_headers(tok), body=bb.jbody(payload))
    assert r.status == 200, (r.status, r.raw[:200])
    return str(r.json["fax_id"])


# ---------------------------------------------------------------------------- install fixtures

def queue_ids(inst: b3.Install) -> dict[str, str]:
    return {str(r[1]): str(r[0]) for r in inst.admin(
        f"SELECT queue_id, name FROM {inst.schema}.m05_fx_queues")}


def seed_rule(inst: b3.Install, sender: str, queue: str) -> None:
    """FIXTURE: a privileged routing-rule row (as fx1_support.seed_rule)."""
    inst.admin(f"INSERT INTO {inst.schema}.m05_fx_rules (rule_id, sender_e164, line_e164,"
               " queue_id) VALUES (gen_random_uuid(), %s, NULL, %s::uuid)",
               (sender, queue_ids(inst)[queue]))


def save_sender(inst: b3.Install, sender: str, name: str) -> None:
    """FIXTURE: a saved sender name row (t146; the support path has its own rows)."""
    inst.admin(f"INSERT INTO {inst.schema}.m05_fx_senders (sender_id, sender_e164, display_name,"
               " updated_at) VALUES (gen_random_uuid(), %s, %s, clock_timestamp())",
               (sender, name))


def rename_queue(inst: b3.Install, queue: str, new_name: str) -> None:
    inst.admin(f"UPDATE {inst.schema}.m05_fx_queues SET name = %s WHERE name = %s",
               (new_name, queue))


def deactivate_queue(inst: b3.Install, queue: str) -> None:
    inst.admin(f"UPDATE {inst.schema}.m05_fx_queues SET active = false WHERE name = %s", (queue,))


def seed_staff(inst: b3.Install, name: str, access: tuple[str, ...] = ("read", "act")) -> str:
    """FIXTURE (after the lifecycle started, as bb.standing_override): a second staff member."""
    staff_id = str(uuid.uuid4())
    inst.admin(f"INSERT INTO {inst.schema}.m01_staff (staff_id, status, status_changed_at,"
               " display_name) VALUES (%s, 'active', clock_timestamp(), %s)", (staff_id, name))
    for value in access:
        inst.admin(f"INSERT INTO {inst.schema}.m01_staff_followup_access (staff_id, access,"
                   " granted_at) VALUES (%s, %s, clock_timestamp())", (staff_id, value))
    return staff_id


def gateway_act(inst: b3.Install, staff_id: str, action: str, fax_id: str, *,
                expected_version: int | None = None, queue_id: str | None = None
                ) -> dict[str, Any]:
    """Another staff member's action through the installed action gateway (runtime role,
    the install's tenant), committed before the screen's principal submits."""
    command: dict[str, Any] = {"schema_version": 1, "action": action, "fax_id": fax_id,
                               "staff_id": staff_id}
    if action != "open":
        command["expected_version"] = expected_version
    if queue_id is not None:
        command["queue_id"] = queue_id
    command["attribution"] = {"principal_kind": "actor", "principal_id": staff_id,
                              "auth_method": "test", "execution_id": str(uuid.uuid4()),
                              "request_id": None, "purpose_code": "operations"}
    with psycopg.connect(b3.runtime_conninfo(inst), autocommit=True) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE haloflow_runtime")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (inst.state["tenant_id"],))
        row = conn.execute(f"SELECT {inst.schema}.m05_fx_staff_action_v2(%s)",
                           (Jsonb(command),)).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def fax_row(inst: b3.Install, fax_id: str) -> dict[str, Any]:
    (row,) = inst.admin(f"SELECT state, queue_id::text, version FROM {inst.schema}.m05_fx_faxes"
                        " WHERE fax_id = %s::uuid", (fax_id,))
    return {"state": row[0], "queue_id": row[1], "version": int(row[2])}


def history(inst: b3.Install, fax_id: str) -> list[dict[str, Any]]:
    rows = inst.admin(f"SELECT seq, action, actor::text, from_queue_id::text, to_queue_id::text,"
                      f" at FROM {inst.schema}.m05_fx_history WHERE fax_id = %s::uuid"
                      " ORDER BY seq", (fax_id,))
    return [{"seq": r[0], "action": r[1], "actor": r[2], "from": r[3], "to": r[4], "at": r[5]}
            for r in rows]


def all_history(inst: b3.Install) -> list[str]:
    return sorted(str(r[0]) for r in inst.admin(
        f"SELECT to_jsonb(t) FROM {inst.schema}.m05_fx_history AS t"))


def snapshot(inst: b3.Install, fax_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return fax_row(inst, fax_id), history(inst, fax_id)


def clinic_time(at: Any) -> str:
    """Independent oracle for the [time] in a notice: "Nov 24, 5:12 PM" in the clinic zone."""
    from zoneinfo import ZoneInfo

    local = at.astimezone(ZoneInfo(CLINIC_TZ))
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


__all__ = ["INTERFACE"]
