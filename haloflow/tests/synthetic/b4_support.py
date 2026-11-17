"""Option B increment B4: shared test support (TEST ONLY; never imported by `src`).

Governing records (Shared Workspace, Work Session 2026-11-16), Codex technical ALIGN
`codex_b4-package-v1r2-review.md` and the pin confirmation in
`codex_b3b-5a57ced-acceptance-and-b4-pin.md`:
- `claude_b4-package-v1-2026-11-16.md`
  sha256 610ebf937a2df8125531892e4d5f392f1244b8133adaaca0d017497d16677e77
- `claude_b4-package-v1r1-2026-11-16.md`
  sha256 32c0e8474c581c82ac5d9ae194a855d95322c72232245faa459715625fe18315
- `claude_b4-package-v1r2-2026-11-16.md`
  sha256 2cc8f65e32a3ba03c9d4227de0379133d73e895ae4c8daf6f32e9e0eabe86c4d
  (the later governs the earlier; v1r2 C1-C5 is the only form contract).
- Owner decisions OD-B4-1 (patient/appointment display deferred to B5) and OD-B4-2 (the five
  staff messages, approved as written), Rachel 2026-11-16 about 10:52 AM ET.

Red classes (as B3a/B3b): every row states its class in its docstring.
- C  control: passes now, without any B4 code; proves a fixture or an oracle.
- B  behaviour-red on accepted B3b (`main` 43fe4f2): runs now and fails with a NAMED assertion
     about B3b behaviour.
- A  absence-red: fails in its own body on the missing B4 interface
     (ModuleNotFoundError for `haloflow.synthetic.ui`, or the UI routes not being served).

Oracles are independent of the code under test: pages are read with the stdlib HTMLParser,
state is read from the install with the admin connection, and the W1 table is read from the
installed `m05_outcome_codes`.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Final

import b3b_support as bb

# ---------------------------------------------------------------------------- pinned interface

INTERFACE: Final = """
haloflow.synthetic.ui (new module):
  UI_FORM_ROUTE: re.Pattern   the single registered form-route pattern (v1r1 F1.1)
  UI_FORM_MAX_BYTES = 8192; UI_FORM_MAX_FIELDS = 6; UI_PAGE_SIZE = 100
  BANNER: str (B-L1, exact); UNRECOGNIZED = "Unrecognized"
  KIND_LABELS, REASON_LABELS, OUTCOME_LABELS: dict[str, str]; NOTE_REQUIRED: frozenset[str]
  MESSAGES: dict[str, str]    keys conflict, resolved, note, refused, unavailable (OD-B4-2)
  FORM_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]]   endpoint -> (required, optional)
  ENDPOINT_TYPES: dict[str, frozenset[str]]                        endpoint -> allowed `type`
  esc(value) -> str; label(mapping, code) -> str
  staff_reported(outcome_code, actor_display, when) -> str          (W2)
  order_page(rows) -> list                                          (page-local grouping)
  parse_form(body: bytes, *, allowed: frozenset[str]) -> dict[str, str]
      raises FormRefused(status=400) for v1r2 C3 and C4 steps 2-3 (encoding, structure,
      field count, duplicate or unknown names)
  class FormRefused(Exception): status: int
Routes (served by create_synthetic_app, behind the B3b gate):
  GET  /synthetic/ui/items            GET /synthetic/ui/items/{item_id}
  POST /synthetic/ui/items/{item_id}/claim   POST /synthetic/ui/items/{item_id}/resolve
  GET  /synthetic/ui/static/app.css
"""

UI: Final = "/synthetic/ui/items"
CSS: Final = "/synthetic/ui/static/app.css"
UI_ROUTES: Final[dict[str, tuple[str, str]]] = {
    "ui_list": ("GET", UI),
    "ui_view": ("GET", UI + "/{item_id}"),
    "ui_claim": ("POST", UI + "/{item_id}/claim"),
    "ui_resolve": ("POST", UI + "/{item_id}/resolve"),
    "ui_css": ("GET", CSS),
}
FORM_CTYPE: Final = "application/x-www-form-urlencoded"

# Exact approved wording (v1r1 §5, OD-B4-2, W1, W2, C5(a)). Sources are named per entry.
BANNER: Final = "SIMULATED — synthetic data, no real patients, no texts sent, no EHR changed"
KIND_LABELS: Final = {
    "no_reply": "No reply — call patient",
    "reschedule_request": "Reschedule requested — call patient",
    "cancel_request": "Cancel requested — staff to cancel in EHR",
    "unclear_reply": "Reply not understood. Review and contact the patient if needed.",
    "ehr_confirmation_check": "EHR confirmation needs checking",
}
REASON_LABELS: Final = {
    "status_changed": "status changed",
    "declined_or_unknown": "declined or unknown",
    "ehr_unreachable": "couldn't reach EHR",
    "could_not_verify": "couldn't verify",
    "no_baseline": "no baseline",
}
OUTCOME_LABELS: Final = {
    "rescheduled": "Rescheduled",
    "cancelled_in_ehr": "Cancelled in EHR",
    "reached_no_change": "Reached – no change",
    "no_contact_left_message": "Couldn't reach – left message",
    "no_contact_no_answer": "Couldn't reach – no answer",
    "other": "Other",
}
MESSAGES: Final = {
    "conflict": "This item changed since you opened it. Reload to see the latest.",
    "resolved": "This item is already closed.",
    "note": "A note is required for Other.",
    "refused": "This action is not available.",
    "unavailable": "Service unavailable — try again.",
}
PAGE_LOCAL_LINE: Final = "Ordered within this page."
SECURITY_HEADERS: Final = {
    "content-security-policy": ("default-src 'none'; style-src 'self'; form-action 'self';"
                                " frame-ancestors 'none'; base-uri 'none'"),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


def ui_path(name: str, item_id: Any = None) -> str:
    template = UI_ROUTES[name][1]
    return template.replace("{item_id}", str(item_id)) if item_id is not None else template


def ui():  # type: ignore[no-untyped-def]
    """The module under test (A rows fail here on main with ModuleNotFoundError)."""
    import importlib

    return importlib.import_module("haloflow.synthetic.ui")


# ---------------------------------------------------------------------------- HTML oracle

@dataclass
class Form:
    action: str
    method: str
    fields: dict[str, list[str]] = field(default_factory=dict)
    radios: dict[str, list[str]] = field(default_factory=dict)

    def hidden(self, name: str) -> str:
        (value,) = self.fields[name]
        return value


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.title: list[str] = []
        self.forms: list[Form] = []
        self.links: list[str] = []
        self.tags: list[str] = []
        self.scripts = 0
        self.inline_styles = 0
        self._in_title = False
        self._form: Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        self.tags.append(tag)
        if tag == "script":
            self.scripts += 1
        if "style" in a or tag == "style":
            self.inline_styles += 1
        if tag == "title":
            self._in_title = True
        if tag == "a" and "href" in a:
            self.links.append(a["href"])
        if tag == "form":
            self._form = Form(a.get("action", ""), a.get("method", "").lower())
            self.forms.append(self._form)
        if self._form is not None and tag in ("input", "textarea", "select"):
            name = a.get("name")
            if name is None:
                return
            if a.get("type") == "radio":
                self._form.radios.setdefault(name, []).append(a.get("value", ""))
            elif tag == "input":
                self._form.fields.setdefault(name, []).append(a.get("value", ""))
            else:
                self._form.fields.setdefault(name, []).append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "form":
            self._form = None

    def handle_data(self, data: str) -> None:
        (self.title if self._in_title else self.text).append(data)


@dataclass
class Page:
    status: int
    text: str
    title: str
    forms: list[Form]
    links: list[str]
    tags: list[str]
    scripts: int
    inline_styles: int
    raw: str


def page(r: bb.Resp) -> Page:
    raw = r.raw.decode("utf-8")
    p = _Page()
    p.feed(raw)
    p.close()
    text = re.sub(r"\s+", " ", " ".join(p.text)).strip()
    return Page(r.status, text, "".join(p.title).strip(), p.forms, p.links, p.tags,
                p.scripts, p.inline_styles, raw)


def html_ok(r: bb.Resp) -> Page:
    """Common HTML-response oracle: the B-L1 label header, an HTML media type, the banner
    exactly once, a SIMULATED title, the R-B4-7 security headers and no script at all."""
    assert r.header(bb.LABEL_HEADER) == ["true"], (r.status, r.headers)
    (ctype,) = r.header("content-type")
    assert ctype.startswith("text/html"), ctype
    pg = page(r)
    assert pg.text.count(BANNER) == 1, ("banner", pg.text[:300])
    assert pg.title.startswith("SIMULATED"), pg.title
    for name, value in SECURITY_HEADERS.items():
        assert r.header(name) == [value], (name, r.header(name))
    assert pg.scripts == 0 and pg.inline_styles == 0, (pg.scripts, pg.inline_styles)
    return pg


def form_body(fields: list[tuple[str, str]]) -> bytes:
    return urllib.parse.urlencode(fields).encode("ascii")


def form_of(pg: Page, action: str) -> Form:
    found = [f for f in pg.forms if f.action == action]
    assert len(found) == 1, (action, [f.action for f in pg.forms])
    return found[0]


def submit_fields(f: Form, **override: str | None) -> list[tuple[str, str]]:
    """The fields a browser would submit for the rendered form `f`, with overrides
    (None removes a field). Radio and textarea values are supplied by the override."""
    out = [(k, v) for k, vs in f.fields.items() for v in vs if k != "note"]
    names = {k for k, _ in out}
    for k, v in override.items():
        if k in names:
            out = [(n, val) for n, val in out if n != k]
        if v is not None:
            out.append((k, v))
    return out


def form_headers(*, host: str = bb.HOST, origin: str | None = bb.ORIGIN,
                 ctype: str | None = FORM_CTYPE,
                 extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    h = [("host", host)]
    if origin is not None:
        h.append(("origin", origin))
    if ctype is not None:
        h.append(("content-type", ctype))
    return h + (extra or [])


def location(r: bb.Resp) -> str:
    (loc,) = r.header("location")
    return loc


def history_rows(inst: Any, item_id: str) -> list[dict[str, Any]]:
    rows = inst.admin(f"SELECT to_jsonb(h) FROM {inst.schema}.m05_followup_history AS h"
                      " WHERE item_id = %s::uuid ORDER BY seq", (item_id,))
    return [dict(r[0]) for r in rows]


def installed_outcomes(inst: Any) -> dict[str, tuple[str, bool]]:
    rows = inst.admin(f"SELECT outcome_code, display_label, note_required"
                      f" FROM {inst.schema}.m05_outcome_codes ORDER BY outcome_code")
    return {r[0]: (r[1], bool(r[2])) for r in rows}
