"""Option B increment B5: shared test support (TEST ONLY; never imported by `src`).

Governing records (Shared Workspace), Codex technical ALIGN on each revision:
- `Work Session 2026-11-16/claude_b5-package-v1-2026-11-16.md`
  sha256 a08258d5... (v1; Codex REVISE `codex_b5-package-v1-review.md`)
- `Work Session 2026-11-16/claude_b5-package-v1r1-2026-11-16.md` (Codex focused REVISE)
- `Work Session 2026-11-16/claude_b5-package-v1r2-2026-11-16.md` (readiness ordering ALIGN)
- `Work Session 2026-11-16/claude_b5-package-v1r3-2026-11-16.md`
  sha256 2462362bfaf68932dba2f00d5d09a2881f0b0b356fe0fba9a50bf83e33b3fab1 (Codex ALIGN,
  `Work Session 2026-11-17/codex_b5-package-v1r3-review.md`, relay fbfe93fb)
- `Work Session 2026-11-17/claude_b5-package-v1r4-2026-11-17.md`
  sha256 516c648c239c3736658012dc714491850dc7a63995e0bcefd0b57de2b151cb54 (Codex ALIGN,
  `Work Session 2026-11-17/codex_b5-package-v1r4-review.md`, relay 1797b2b6)
  (the later governs the earlier).
- Owner decisions (Rachel, 2026-11-17 about 8:40-8:50 AM ET,
  `Work Session 2026-11-17/claude_b5-owner-decisions-2026-11-17.md`
  sha256 9f81dc52b763384a8189574a89b568f5cfc24a66c154e94315c3ae7a50ff75cc):
  OD-B5-1 no on-demand no-reply; OD-B5-2 wording approved, message 6 replaced;
  OD-B5-3 America/New_York only, load hours [11:00, 16:00).

Red classes (as B3a/B3b/B4): every row states its class in its docstring.
- C  control: passes now, without any B5 code; proves a fixture or an oracle.
- B  behaviour-red on accepted B4 (`main` b358637): runs now and fails with a NAMED assertion
     about B4 behaviour (for example the frozen target 94, or the closed route inventory).
- A  absence-red: fails in its own body on the missing B5 interface (ModuleNotFoundError for
     `haloflow.synthetic.{scenarios,sim_actions,sim_ui,display_units,display_statements}`,
     AttributeError for the new names on existing modules, or the B5 routes not served).

Oracles are independent of the code under test:
- the Eastern placement oracle `expect_eastern` is a direct zoneinfo transcription of the
  owner rule (v1r4 §1; Codex v1r4 pin 2: never the production `place` helper);
- the reminder-eligibility oracle `a3_verdict` is a Python transcription of t062 (v1r1 A3);
- database state is read with the admin connection; the simulated EHR and FakeSender through
  their own objects (`snapshot()`, `.calls`), never through the B5 routes;
- pages are read with the B4 stdlib-HTMLParser oracle (`b4.html_ok`).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import re
import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import b4_support as b4

# ---------------------------------------------------------------------------- pinned interface

INTERFACE: Final = """
haloflow.synthetic.scenarios (new package; fixtures are packaged JSON under scenarios/):
  LOAD_ZONE = "America/New_York"          the ONLY product zone (OD-B5-3)
  LOAD_ZONES: tuple[str, ...] = (LOAD_ZONE,)
      read by sim_actions.load AT CALL TIME. Test fixtures may monkeypatch it to the five-zone
      TEST_ZONES below (v1r4 §2, Codex v1r4 Q1). No src module assigns it after definition;
      no HTTP/form/CLI/environment input selects it.
  LOAD_START = time(11, 0); LOAD_END = time(16, 0)    half-open [LOAD_START, LOAD_END) local
  START_HOUR = 18; SEND_LEAD_DAYS = 2; FAR_LEAD_DAYS = 10
  NAMES = ("S1", "S2", "S3", "S4")
  FIRST_NAMES: tuple[str, ...]            fixed synthetic list
  PROVIDERS: dict[str, str]               provider_ref -> display label (synthetic)
  DEPARTMENTS: frozenset[str]             allowed department_ref values
  class ScenarioInvalid(ValueError): .field: str
  class OutsideLoadHours(Exception)
  @dataclass(frozen=True) class Row: key, first_name, phone, provider_ref, department_ref,
      lead_days: int, ehr: bool
  @dataclass(frozen=True) class Scenario: name: str; rows: tuple[Row, ...]
  validate(raw: object) -> Scenario       strict schema; raises ScenarioInvalid(field)
  fixtures() -> dict[str, Scenario]       the four packaged scenarios, validated
  ids(tenant_id: str, scenario: str, key: str) -> tuple[uuid.UUID, uuid.UUID]
      (patient_id, appointment_ref): uuid5, deterministic, distinct per tenant/scenario/key
  place(now: datetime, zones: Sequence[str]) -> tuple[str, date]
      the first zone whose local time of the AWARE `now` is in [LOAD_START, LOAD_END), and
      that local date; raises OutsideLoadHours when none; ValueError for a naive `now`
  start_at(zone: str, local_date: date, lead_days: int) -> datetime
      aware UTC: (local_date + lead_days) at START_HOUR:00 local in `zone`
  scenario_state(present: frozenset[str], ehr: frozenset[str], refs: frozenset[str],
                 ehr_backed: frozenset[str]) -> "not_loaded" | "loaded" | "ehr_lost" | "partial"
  visible_ehr(present: frozenset[str], ehr: frozenset[str]) -> frozenset[str]   (present & ehr)

haloflow.synthetic.display_units (new):  B5_TENANT_MIGRATIONS, B5_INSTALLED_STATE_PROFILES
  t095_m05_synthetic_display            m05_synthetic_display(p_item_id uuid) -> jsonb, STABLE
  t096_m05_synthetic_scenario_claim     m05_synthetic_scenario_claim(p_scenario text,
                                            p_refs uuid[]) -> jsonb, VOLATILE
  t097_m05_synthetic_scenario_present   m05_synthetic_scenario_present(p_refs uuid[]) -> uuid[],
                                            STABLE
  all SECURITY DEFINER, owner M05_OWNER_ROLE, EXECUTE: haloflow_runtime only (t092 pattern)
haloflow.synthetic.display_statements (new):
  DISPLAY_KEY = "m05.synthetic.display"           READ, READ_CAPABILITY
  CLAIM_KEY = "m05.synthetic.scenario_claim"      WRITE, capability "m05.synthetic.scenario_claim"
  PRESENT_KEY = "m05.synthetic.scenario_present"  READ, READ_CAPABILITY
  B5_STATEMENTS
haloflow.synthetic.composition: SYNTHETIC_TARGET = 97; the synthetic registry and catalogue
  gain exactly the three units and three keys; production {36} unchanged.

haloflow.synthetic.sim_actions (new):
  LOAD_ROUTE = "/synthetic/simulator/load-scenario"   POST JSON {"scenario": name}
  TICK_ROUTE = "/synthetic/simulator/tick"            POST JSON {"scheduler": name}
  OUTBOX_ROUTE = "/synthetic/simulator/outbox"        GET
  SCHEDULERS = ("due", "reminder", "writeback"); OUTBOX_CAP = 50
  LOAD_CAPABILITIES = frozenset({"m05.intake.directory", "m05.synthetic.scenario_claim"})
  @dataclass(frozen=True) class Result: status: int; code: str | None; body: dict
  async load(lifecycle, scenario: str) -> Result      principal composition.W_DIRECTORY
      200 None | 422 UNKNOWN_SCENARIO | 409 SCENARIO_PRESENT | 409 OUTSIDE_LOAD_HOURS
      | 500 SCENARIO_LOAD_FAILED | 500 SCENARIO_LOAD_UNKNOWN | 503 <lifecycle stop code>
      hooks (hooks.fire): b5.load.clock {"now": <ISO-8601 UTC of the database clock read in the
      load transaction>} then b5.load.after_claim, b5.load.after_ehr, b5.load.before_commit,
      b5.load.after_commit
  async tick(lifecycle, scheduler: str) -> Result    200 {"scheduler", "counts": {str: int}}
      | 422 UNKNOWN_SCHEDULER | 503 <stop code>
  outbox(lifecycle) -> list[dict]   newest first, at most OUTBOX_CAP: {"op_id","phone","text"}
  install_sim_actions(app, lifecycle)
  audit: one INFO line "SIMULATOR action=load_scenario status=<n> code=<C|-> scenario=<S1..S4|->"
         one INFO line "SIMULATOR action=tick status=<n> code=<C|-> scheduler=<name|->"
haloflow.synthetic.simulator (refactor, behaviour unchanged):
  async deliver_reply(lifecycle, phone: object, text: object) -> sim_actions.Result
  apply_ehr(lifecycle, fields: Mapping[str, object]) -> sim_actions.Result
haloflow.synthetic.sim_ui (new):
  PAGE_ROUTE = "/synthetic/simulator"
  FORM_ROUTES = {"load": ".../ui/load", "tick": ".../ui/tick", "reply": ".../ui/reply",
                 "ehr": ".../ui/ehr"}            (prefix "/synthetic/simulator")
  MESSAGES: dict[str, str]   exactly the OD-B5-2 keyed texts (MESSAGES below)
  PARTIAL_TEXT, RESTART_TEXT (str.format with scenario=<S1..S4>), LOADED_LABEL = "Loaded"
  HEADINGS: tuple[str, ...]; TICK_BUTTONS: dict[str, str]; LOAD_BUTTON = "Load"
  every form carries the hidden field "csrf" (the lifespan token); fields per action:
      load {csrf, scenario}  tick {csrf, scheduler}  reply {csrf, phone, text}
      ehr  {csrf, operation, ...the JSON /synthetic/simulator/ehr fields of that operation}
  after a form action: 303 to "/synthetic/simulator?m=<message key>" (no `m` on success
  without a message); refusals before the action (400/403/413/415) are B4 HTML error pages
haloflow.synthetic.ui:
  UI_FORM_ROUTE also fully matches exactly the four simulator form paths
  display_lines(read: Mapping[str, Any], *, clinic_tz: str) -> tuple[str, ...]   (unescaped)
      linked:   ("Patient: <first_name>", "Appointment: <YYYY-MM-DD HH:MM> <zone>"
                 + " — <PROVIDERS label>" when provider_ref is a known ref)
                <zone> is department_tz when it is a valid zone name, else clinic_tz
      unlinked: ("No appointment linked",)
  the item view renders those lines (escaped), each as its own <p>, after the B4
  "Synthetic patient ref" line (unchanged)
documentation/synthetic-app.md (new; v1 R-B5-7, v1r3 §3, v1r4 §3)
"""

# ---------------------------------------------------------------------------- owner values

LOAD_ZONE: Final = "America/New_York"
EASTERN: Final = ZoneInfo(LOAD_ZONE)
LOAD_START: Final = time(11, 0)
LOAD_END: Final = time(16, 0)
START_HOUR: Final = 18
SEND_LEAD: Final = 2
FAR_LEAD: Final = 10
NAMES: Final = ("S1", "S2", "S3", "S4")
# v1r1 E1: the five fixed-offset zones; v1r4 §2: TEST ONLY (never in src).
TEST_ZONES: Final = ("Pacific/Honolulu", "America/Bogota", "Africa/Abidjan", "Asia/Karachi",
                     "Pacific/Port_Moresby")
# v1r1 A3 / synthetic/provision.py SETTINGS (the installed reminder settings).
SETTINGS: Final = {"lead_days": 2, "send_time": time(10, 0), "window_start": time(9, 0),
                   "window_end": time(19, 0)}

# OD-B5-2 (approved as written, message 6 replaced). Keys are the redirect `m` values.
MESSAGES: Final = {
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
NO_APPOINTMENT: Final = "No appointment linked"     # OD-B4-1 / T-B5-2
CODE_MESSAGE: Final = {"SCENARIO_PRESENT": "present", "OUTSIDE_LOAD_HOURS": "hours",
                       "SCENARIO_LOAD_FAILED": "failed", "SCENARIO_LOAD_UNKNOWN": "unknown"}

# ---------------------------------------------------------------------------- routes

SIM: Final = "/synthetic/simulator"
LOAD_ROUTE: Final = SIM + "/load-scenario"
TICK_ROUTE: Final = SIM + "/tick"
OUTBOX_ROUTE: Final = SIM + "/outbox"
PAGE_ROUTE: Final = SIM
FORM_ROUTES: Final = {k: f"{SIM}/ui/{k}" for k in ("load", "tick", "reply", "ehr")}
# R7u closed inventory additions (name -> (method, path)).
B5_ROUTES: Final[dict[str, tuple[str, str]]] = {
    "sim_load": ("POST", LOAD_ROUTE),
    "sim_tick": ("POST", TICK_ROUTE),
    "sim_outbox": ("GET", OUTBOX_ROUTE),
    "sim_page": ("GET", PAGE_ROUTE),
    **{f"sim_ui_{k}": ("POST", v) for k, v in FORM_ROUTES.items()},
}
B5_UNITS: Final = ("t095_m05_synthetic_display", "t096_m05_synthetic_scenario_claim",
                   "t097_m05_synthetic_scenario_present")
B5_FUNCTIONS: Final = {"m05_synthetic_display": ("p_item_id uuid", "jsonb", "s"),
                       "m05_synthetic_scenario_claim": ("p_scenario text, p_refs uuid[]",
                                                        "jsonb", "v"),
                       "m05_synthetic_scenario_present": ("p_refs uuid[]", "uuid[]", "s")}
# key -> (manifest mode, required capability); READ_CAPABILITY = "m05.followup.read" (t092).
B5_KEYS: Final = {"m05.synthetic.display": ("read", "m05.followup.read"),
                  "m05.synthetic.scenario_claim": ("write", "m05.synthetic.scenario_claim"),
                  "m05.synthetic.scenario_present": ("read", "m05.followup.read")}
HOOKS: Final = ("b5.load.clock", "b5.load.after_claim", "b5.load.after_ehr",
                "b5.load.before_commit", "b5.load.after_commit")


def mod(name: str) -> Any:
    """Lazy import of `haloflow.synthetic.<name>` inside a row body (red rule, as B2-B4)."""
    return importlib.import_module(f"haloflow.synthetic.{name}")


# ---------------------------------------------------------------------------- oracles

def expect_eastern(now: datetime) -> datetime | None:
    """Codex v1r4 pin 2: the INDEPENDENT Eastern oracle. The expected appointment start
    (aware, UTC) of a sendable scenario loaded at the aware instant `now`, or None when the
    owner rule refuses (local time outside [11:00, 16:00) America/New_York)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("naive")
    local = now.astimezone(EASTERN)
    if not LOAD_START <= local.time() < LOAD_END:
        return None
    day = local.date() + timedelta(days=SEND_LEAD)
    return datetime(day.year, day.month, day.day, START_HOUR, tzinfo=EASTERN).astimezone(UTC)


def expect_start(now: datetime, zone: str, lead_days: int) -> datetime:
    """(local date of `now` in `zone` + lead_days) at 18:00 local, aware UTC (oracle)."""
    tz = ZoneInfo(zone)
    day = now.astimezone(tz).date() + timedelta(days=lead_days)
    return datetime(day.year, day.month, day.day, START_HOUR, tzinfo=tz).astimezone(UTC)


def a3_verdict(now: datetime, start: datetime, zone: str) -> str:
    """t062 `m05_reminder_decision` transcribed (v1r1 A3) with the installed settings."""
    tz = ZoneInfo(zone)
    due_day = start.astimezone(tz).date() - timedelta(days=SETTINGS["lead_days"])
    due = datetime.combine(due_day, SETTINGS["send_time"], tz)
    local = now.astimezone(tz)
    if start - now < timedelta(hours=26):
        return "too_late"
    if now < due:
        return "not_due"
    if SETTINGS["window_start"] <= local.time() < SETTINGS["window_end"]:
        return "send"
    return "outside_window"


def eastern(y: int, mo: int, d: int, h: int, mi: int = 0, sec: int = 0, us: int = 0) -> datetime:
    """An aware America/New_York wall time (fold 0)."""
    return datetime(y, mo, d, h, mi, sec, us, tzinfo=EASTERN)


# DST transition dates (US rule: second Sunday of March, first Sunday of November).
DST: Final = {2026: (date(2026, 3, 8), date(2026, 11, 1)),
              2027: (date(2027, 3, 14), date(2027, 11, 7))}


# ---------------------------------------------------------------------------- fixtures oracle

FIXTURE_SHAPE: Final = {
    # name -> (row keys in order, lead days per row, ehr-backed per row)
    "S1": (("confirm",), (SEND_LEAD,), (True,)),
    "S2": (("reschedule", "cancel", "unclear", "stop"), (SEND_LEAD,) * 4, (False,) * 4),
    "S3": (("check",), (SEND_LEAD,), (True,)),
    "S4": (("far",), (FAR_LEAD,), (False,)),
}
CANONICAL: Final = re.compile(r"\+155555501[0-9]{2}")


def scenario_refs(tenant_id: str, name: str) -> dict[str, tuple[str, str]]:
    """key -> (patient_id, appointment_ref) as text, through the pinned `ids`."""
    sc = mod("scenarios")
    keys = FIXTURE_SHAPE[name][0]
    return {k: tuple(str(x) for x in sc.ids(tenant_id, name, k)) for k in keys}  # type: ignore[misc]


def ehr_backed(tenant_id: str, name: str) -> set[str]:
    keys, _, flags = FIXTURE_SHAPE[name]
    refs = scenario_refs(tenant_id, name)
    return {refs[k][1] for k, f in zip(keys, flags, strict=True) if f}


# ---------------------------------------------------------------------------- install state

def appointments(inst: b3.Install, refs: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Admin read: ref -> {start_at (aware), status, patient_id, department_tz, first_name,
    provider_ref} for the refs present in m05_appointments."""
    if not refs:
        return {}
    rows = inst.admin(
        f"SELECT a.appointment_ref::text, a.start_at, a.status, a.patient_id::text,"
        f" p.department_tz, p.first_name, p.provider_ref"
        f" FROM {inst.schema}.m05_appointments AS a"
        f" LEFT JOIN {inst.schema}.m05_appointment_profile AS p"
        f" ON p.appointment_ref = a.appointment_ref"
        f" WHERE a.appointment_ref = ANY(%s::uuid[])", (list(refs),))
    return {r[0]: {"start_at": r[1], "status": r[2], "patient_id": r[3],
                   "department_tz": r[4], "first_name": r[5], "provider_ref": r[6]}
            for r in rows}


def db_now(inst: b3.Install) -> datetime:
    (row,) = inst.admin("SELECT clock_timestamp()")
    value = row[0]
    assert isinstance(value, datetime) and value.tzinfo is not None
    return value.astimezone(UTC)


def outbound_ops(inst: b3.Install, refs: Sequence[str]) -> list[dict[str, Any]]:
    rows = inst.admin(f"SELECT to_jsonb(o) FROM {inst.schema}.m05_outbound_ops AS o"
                      " WHERE o.appointment_ref = ANY(%s::uuid[]) ORDER BY 1::text",
                      (list(refs),))
    return [dict(r[0]) for r in rows]


def windows(inst: b3.Install, refs: Sequence[str]) -> list[dict[str, Any]]:
    rows = inst.admin(f"SELECT to_jsonb(w) FROM {inst.schema}.m05_reminder_windows AS w"
                      " WHERE w.appointment_ref = ANY(%s::uuid[]) ORDER BY 1::text",
                      (list(refs),))
    return [dict(r[0]) for r in rows]


def baselines(inst: b3.Install, refs: Sequence[str]) -> list[dict[str, Any]]:
    rows = inst.admin(f"SELECT to_jsonb(b) FROM {inst.schema}.m05_writeback_baselines AS b"
                      " WHERE b.appointment_ref = ANY(%s::uuid[]) ORDER BY 1::text",
                      (list(refs),))
    return [dict(r[0]) for r in rows]


def wb_ops(inst: b3.Install, refs: Sequence[str]) -> list[dict[str, Any]]:
    rows = inst.admin(f"SELECT to_jsonb(w) FROM {inst.schema}.m05_writeback_ops AS w"
                      " WHERE w.appointment_ref = ANY(%s::uuid[]) ORDER BY 1::text",
                      (list(refs),))
    return [dict(r[0]) for r in rows]


@dataclass(frozen=True)
class Snap:
    """The no-write oracle (v1r1 D3/D4, Codex v1r4 pin 3): database table digests, the
    simulated EHR's own snapshot, and the FakeSender call count."""
    tables: dict[str, tuple[int, str]]
    ehr: tuple[tuple[str, str], ...]
    ehr_writes: int
    sends: int


def snap(inst: b3.Install, lc: Any) -> Snap:
    raw = lc.port.ehr.snapshot()
    ehr = tuple(sorted((k, repr(v)) for k, v in raw.appointments.items()))
    return Snap(b3.table_digests(inst), ehr, len(raw.writes), len(lc.sender.calls))


def unchanged(before: Snap, after: Snap) -> None:
    assert b3.changed(before.tables, after.tables) == set(), \
        b3.changed(before.tables, after.tables)
    assert before.ehr == after.ehr, "simulated EHR changed"
    assert before.ehr_writes == after.ehr_writes, "simulated EHR written"
    assert before.sends == after.sends, "FakeSender called"


# ---------------------------------------------------------------------------- requests

async def load_json(app: Any, token: str, scenario: Any) -> bb.Resp:
    return await bb.asgi(app, "POST", LOAD_ROUTE, headers=bb.post_headers(token),
                         body=bb.jbody({"scenario": scenario}))


async def tick_json(app: Any, token: str, scheduler: Any) -> bb.Resp:
    return await bb.asgi(app, "POST", TICK_ROUTE, headers=bb.post_headers(token),
                         body=bb.jbody({"scheduler": scheduler}))


async def sim_page(app: Any, m: str | None = None) -> tuple[bb.Resp, b4.Page]:
    target = PAGE_ROUTE + (f"?m={m}" if m else "")
    r = await bb.asgi(app, "GET", target, headers=bb.get_headers())
    assert r.status == 200, (target, r.status, r.raw[:300])
    return r, b4.html_ok(r)


async def post_form(app: Any, action: str, fields: list[tuple[str, str]], **hk: Any) -> bb.Resp:
    return await bb.asgi(app, "POST", FORM_ROUTES[action], headers=b4.form_headers(**hk),
                         body=b4.form_body(fields))


def page_redirect(r: bb.Resp, key: str | None) -> None:
    assert r.status == 303, (r.status, r.raw[:300])
    assert r.header(bb.LABEL_HEADER) == ["true"], r.headers
    want = PAGE_ROUTE + (f"?m={key}" if key else "")
    assert b4.location(r) == want, (b4.location(r), want)


async def page_csrf(app: Any) -> str:
    """The per-lifespan CSRF value the simulator page renders in its forms."""
    _, pg = await sim_page(app)
    values = {v for f in pg.forms if f.method == "post" for v in f.fields.get("csrf", [])}
    assert len(values) == 1, values
    return values.pop()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------- source scans

HALOFLOW: Final = s.HALOFLOW
SRC_SYN: Final = s.SRC / "haloflow" / "synthetic"


def src_modules() -> dict[Path, ast.Module]:
    return {p: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(SRC_SYN.rglob("*.py"))}


def assigns_name(tree: ast.Module, name: str) -> list[int]:
    """Line numbers of assignments to `name` or `<anything>.name` (incl. setattr calls)."""
    out: list[int] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            if (isinstance(t, ast.Name) and t.id == name) or \
                    (isinstance(t, ast.Attribute) and t.attr == name):
                out.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "setattr" and len(node.args) >= 2 \
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == name:
            out.append(node.lineno)
    return out


def doc_text() -> str:
    return (HALOFLOW.parent / "documentation" / "synthetic-app.md").read_text(encoding="utf-8")


def qs(fields: Mapping[str, str]) -> bytes:
    return urllib.parse.urlencode(list(fields.items())).encode("ascii")


def new_id() -> str:
    return str(uuid.uuid4())


__all__ = ["json", "s"]
