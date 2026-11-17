"""Option B increment B5: shared test support (TEST ONLY; never imported by `src`).

Named `ob5_support` (Option B B5) because `tests/m05/b5_support.py` already exists (M05 B5).

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
                                            p_refs jsonb) -> jsonb, VOLATILE
  t097_m05_synthetic_scenario_present   m05_synthetic_scenario_present(p_refs jsonb) -> jsonb
                                            {"schema_version": 1, "present": [sorted uuids]},
                                            STABLE
  p_refs: a JSON array of 0..64 canonical uuid strings (F-INT1: no array type in a
  signature; green disclosure, relay 73878cd8)
  all SECURITY DEFINER, owner M05_OWNER_ROLE, EXECUTE: haloflow_runtime only (t092 pattern)
haloflow.synthetic.display_statements (new):
  DISPLAY_KEY = "m05.synthetic.display"           READ, READ_CAPABILITY, params {"item_id"}
  CLAIM_KEY = "m05.synthetic.scenario_claim"      WRITE, capability "m05.synthetic.scenario_claim",
                                                  params {"scenario", "refs"}
  PRESENT_KEY = "m05.synthetic.scenario_present"  READ, READ_CAPABILITY, params {"refs"}
  each key's statement returns one row whose single column is the function's result
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
      the load is ONE `haloflow.m02.observation.EventFoundation.run_in_transaction(context,
      body)` as W_DIRECTORY with LOAD_CAPABILITIES; inside `body(binding)` every statement is
      `binding.handle.fetch_all(<key>, {...})`: CLAIM_KEY first, then (S1/S3) the synchronous
      SimulatedEhr.put loop, then the directory key (DIRECTORY_KEY) for every contact_put,
      then every appointment_put. A returned directory result other than
      {"schema_version": 1, "outcome": "applied"} (contact_put: also "existing") raises
      inside `body`, so the transaction rolls back.
      the b5.load.after_* / before_commit points are awaited through hooks.afire (below);
      b5.load.clock is a plain hooks.fire
      directory fields per row: appointment_put {appointment_ref, patient_id, start_at (ISO Z),
      status "booked", first_name, confirmation "not_confirmed", department_tz <placed zone>,
      source_status "booked", provider_ref, department_ref}; contact_put {phone, patient_id}
      EHR entry per S1/S3 row: EhrRead(status="neutral", appointment_id=str(appointment_ref),
      start_at=<the same start>, provider_ref, department_ref, replaced_by=None)
      success body: {"scenario": name, "outcome": "loaded"}; refusal body {"code": C}
  async tick(lifecycle, scheduler: str) -> Result    200 {"scheduler", "counts": {str: int}}
      | 422 UNKNOWN_SCHEDULER | 503 <stop code>
  outbox(lifecycle) -> list[dict]   newest first, at most OUTBOX_CAP: {"op_id","phone","text"}
  install_sim_actions(app, lifecycle)
  audit: one INFO line "SIMULATOR action=load_scenario status=<n> code=<C|-> scenario=<S1..S4|->"
         one INFO line "SIMULATOR action=tick status=<n> code=<C|-> scheduler=<name|->"
haloflow.synthetic.hooks (addition, test seam as FAULT):
  AFAULT: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None = None
  async afire(point, info=None) -> None      awaits AFAULT when set; no-op otherwise
  no src module assigns AFAULT (as FAULT)
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
  the form adapter only parses the form into the action function's arguments (EHR
  fail_next `reads`/`writes`: base-10 digit strings -> int; anything else 422); then:
      action refused 422 (MALFORMED / UNKNOWN_SCENARIO / UNKNOWN_SCHEDULER, or the ingress's
          own 4xx) -> a 422 B4 HTML error page
      a load code in CODE_MESSAGE -> 303 "/synthetic/simulator?m=<key>"; 503 -> m=stopping
      success -> 303 with m=loaded (load), m=tick (tick), no `m` (reply, EHR)
  refusals before the action (CSRF missing/wrong 403, duplicate or unknown field 400, 413,
  415) are B4 HTML error pages, checked before any principal resolution or action
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
# Green disclosure (relay 73878cd8): the platform rule F-INT1 admits no array type in a
# function signature, so p_refs is a JSON array of uuid strings and t097 returns jsonb.
B5_FUNCTIONS: Final = {"m05_synthetic_display": ("p_item_id uuid", "jsonb", "s"),
                       "m05_synthetic_scenario_claim": ("p_scenario text, p_refs jsonb",
                                                        "jsonb", "v"),
                       "m05_synthetic_scenario_present": ("p_refs jsonb", "jsonb", "s")}
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


def ehr_state(ehr: Any) -> tuple[Any, ...]:
    """The simulated EHR's full observable safety state (Codex 6322924 F1): every appointment
    as ALL of its dataclass fields (never `repr`, which shows the status only), the full write
    log, the closed and used permit sets, and the pending read/write fault counters. The
    port's monotonic time is excluded on purpose (it advances on every read)."""
    import dataclasses

    raw = ehr.snapshot()
    apps = tuple(sorted((k, tuple(sorted(dataclasses.asdict(v).items())))
                        for k, v in raw.appointments.items()))
    return (apps, tuple(raw.writes), tuple(sorted(raw.closed)), tuple(sorted(raw.used)),
            int(ehr._read_failures), int(ehr._write_failures))


@dataclass(frozen=True)
class Snap:
    """The no-write oracle (v1r1 D3/D4, Codex v1r4 pin 3, Codex 6322924 F1): database table
    digests, the simulated EHR's full state (`ehr_state`) and a copy of every FakeSender
    call (op id, phone, text)."""
    tables: dict[str, tuple[int, str]]
    ehr: tuple[Any, ...]
    sends: tuple[tuple[str, str, str], ...]

    @property
    def ehr_writes(self) -> int:
        return len(self.ehr[1])


def snap(inst: b3.Install, lc: Any) -> Snap:
    return Snap(b3.table_digests(inst), ehr_state(lc.port.ehr), tuple(lc.sender.calls))


def unchanged(before: Snap, after: Snap) -> None:
    assert b3.changed(before.tables, after.tables) == set(), \
        b3.changed(before.tables, after.tables)
    assert before.ehr == after.ehr, "simulated EHR changed"
    assert before.sends == after.sends, "FakeSender called"


# ---------------------------------------------------------------------------- parity oracle
#
# Codex 0052e54 R3 focused investigation: a TYPED comparison of two independent installs.
# - Column types come from information_schema. uuid columns, and the named text/json id
#   fields, are mapped through a per-install BIJECTIVE token map (fixture uuid5 values -> role
#   labels first, then every other id -> id<n> in a deterministic traversal), so a broken
#   link (an event, op or permit pointing elsewhere) changes the tokens.
# - timestamptz values are typed: the explicitly named INCIDENTAL record-keeping columns
#   compare as present/absent only; every other (business) time must match the other install
#   within TIME_TOL either absolutely or relative to each install's measured request anchor;
#   an unclassified timestamptz column is an error, never silently erased.
# - everything else (texts, phones, states, codes, numbers) compares literally, including
#   uuid- or timestamp-shaped text.

INCIDENTAL_TIMES: Final = frozenset({
    "created_at", "updated_at", "occurred_at", "projected_at", "completed_at", "started_at",
    "at", "issued_at", "consumed_at", "captured_at", "received_at", "detected_at", "added_at",
    "auth_time", "last_activity_at", "granted_at", "status_changed_at", "effective_from"})
BUSINESS_TIMES: Final = frozenset({
    "start_at", "deadline", "sent_at", "lease_until", "next_at", "next_check_at",
    "apply_deadline", "spent_at", "closed_at", "voided_at", "opted_out_at", "expires_at",
    "starts_at", "revoked_at", "retired_at", "available_at", "lease_expires_at",
    "delivered_at", "due_at", "next_eligible_at", "deadline_at"})
TEXT_IDS: Final = frozenset({"provider_message_id", "appointment_id", "permit_id",
                             "op_id", "attempt_id", "wb_op_id", "event_id", "item_id"})
# Install-keyed values (an HMAC under the install's own key): equal inputs differ per
# install by construction; compared as present/absent only.
# Each is derived from ids and/or the install's own key (HMAC, digest, dedupe hash); the
# relations they summarize are compared through the id tokens instead.
INCIDENTAL_VALUES: Final = frozenset({"fingerprint", "create_digest", "dispatch_fingerprint",
                                      "content_fingerprint", "dedupe_key", "r1_digest",
                                      "producer_dedupe_key"})
# Green classification (B5 green packet): the FakeSender's accepted message id, stored as
# m05_outbound_ops.provider_ref, is `fake-` + 16 random hex per send (m05.outbound.FakeSender).
# Incidental ONLY in that table and ONLY in that exact shape; any other value compares
# literally (provider_ref elsewhere -- the appointment profile -- is a business field).
INCIDENTAL_TABLE_VALUES: Final = {("m05_outbound_ops", "provider_ref"):
                                  re.compile(r"fake-[0-9a-f]{16}")}
TENANT_FIELDS: Final = frozenset({"tenant_id"})
SCHEMA_FIELDS: Final = frozenset({"schema_key", "schema_name"})
TIME_TOL: Final = 5.0
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def ref_labels(tenant_id: str, ids: Any = None) -> dict[str, str]:
    """uuid text -> stable role label ("S1.confirm.patient" / "S1.confirm.ref") for every
    fixture row of the four scenarios, so two installs (different tenants, different uuid5
    values) compare by role. `ids` defaults to the pinned `scenarios.ids`; a control may pass
    a stand-in with the same signature (U-C7)."""
    ids_fn = ids if ids is not None else mod("scenarios").ids
    out: dict[str, str] = {}
    for name in NAMES:
        for key in FIXTURE_SHAPE[name][0]:
            patient, ref = (str(x) for x in ids_fn(tenant_id, name, key))
            out[patient] = f"{name}.{key}.patient"
            out[ref] = f"{name}.{key}.ref"
    return out


def is_load_success(action: str, outcome: tuple[int, str | None]) -> bool:
    """P-PAR's predicate for "this compared request was a SUCCESSFUL scenario load" (Codex
    07f30e0 fix 2): only a load action, and only its success result on either adapter
    (JSON 200, form 303 m=loaded). A successful tick or EHR action is never a load."""
    return action == "load" and outcome in ((200, None), (303, "loaded"))


class Unclassified(AssertionError):
    pass


@dataclass(frozen=True)
class Capture:
    """One install's rows (parsed to_jsonb objects), column types and the request anchor."""
    tables: dict[str, list[dict[str, Any]]]
    types: dict[str, dict[str, str]]
    anchor: datetime


@dataclass(frozen=True)
class Side:
    before: Capture
    after: Capture
    labels: Mapping[str, str]
    tenant: str
    schema: str
    ehr: tuple[Any, Any] | None = None
    sends: tuple[tuple[str, str, str], ...] = ()


def capture(inst: b3.Install, anchor: datetime) -> Capture:
    """Admin read of every user table (to_jsonb objects) and of the column types."""
    import psycopg

    tables: dict[str, list[dict[str, Any]]] = {}
    types: dict[str, dict[str, str]] = {}

    def key(sc: str, t: str) -> str:
        return ("{tenant}" if sc == inst.schema else sc) + "." + t

    with inst.c.connect() as a:
        for sc, t, col, ty, udt in a.execute(
                "SELECT table_schema, table_name, column_name, data_type, udt_name FROM"
                " information_schema.columns WHERE table_schema NOT IN"
                " ('pg_catalog','information_schema')").fetchall():
            types.setdefault(key(sc, t), {})[col] = "uuid[]" if udt == "_uuid" else ty
        tabs = a.execute("SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n"
                         " ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname"
                         " NOT IN ('pg_catalog','information_schema') ORDER BY 1, 2").fetchall()
        for sc, t in tabs:
            q = psycopg.sql.SQL("SELECT to_jsonb(r) FROM {}.{} AS r").format(
                psycopg.sql.Identifier(sc), psycopg.sql.Identifier(t))
            tables[key(sc, t)] = [dict(r[0]) for r in a.execute(q).fetchall()]
    return Capture(tables, types, anchor)


def _time(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _is_id_key(k: str) -> bool:
    return k in TEXT_IDS or k.endswith("_id") or k.endswith("_ref") or k in ("id", "ref")


class _Tok:
    """One install's bijective id -> token map and the typed row encoder."""

    def __init__(self, side: Side) -> None:
        self.side = side
        self.map: dict[str, str] = dict(side.labels)
        self.found: set[str] = set()

    def tok(self, v: str) -> str:
        if v not in self.map:
            self.found.add(v)
            return v
        return self.map[v]

    def enc(self, name: str, v: Any, ty: str | None, times: list[Any], *, assign: bool) -> Any:
        if v is None:
            return None
        if name in INCIDENTAL_VALUES:
            return "<inc>"
        if name in TENANT_FIELDS and v == self.side.tenant:
            return "<tenant>"
        if name in SCHEMA_FIELDS and v == self.side.schema:
            return "<schema>"
        if ty == "uuid[]" and isinstance(v, list):
            return [self.tok(x) if assign else self.map.get(x, "?") for x in v]
        if ty == "uuid" or (isinstance(v, str) and _is_id_key(name) and _UUID_RE.fullmatch(v)):
            return self.tok(v) if assign else self.map.get(v, "?")
        if ty == "timestamp with time zone" or (ty is None and isinstance(v, str)
                                                 and (name in INCIDENTAL_TIMES
                                                      or name in BUSINESS_TIMES)):
            if name in INCIDENTAL_TIMES:
                return "<inc>"
            if name not in BUSINESS_TIMES:
                raise Unclassified(f"unclassified timestamp field {name!r}")
            t = _time(v)
            times.append((name, t, (t - self.side.after.anchor).total_seconds()))
            return f"<T:{name}>"
        if isinstance(v, dict):
            return {k: self.enc(k, x, None, times, assign=assign) for k, x in sorted(v.items())}
        if isinstance(v, list):
            return [self.enc(name, x, None, times, assign=assign) for x in v]
        return v

    def row(self, table: str, row: Mapping[str, Any], *, assign: bool
            ) -> tuple[str, list[Any]]:
        types = self.side.after.types.get(table) or self.side.before.types.get(table) or {}
        row = _scrub(table, row)
        times: list[Any] = []
        obj = {k: self.enc(k, v, types.get(k), times, assign=assign)
               for k, v in sorted(row.items())}
        return json.dumps(obj, sort_keys=True, default=str), times


def _ids_in(row: Mapping[str, Any], types: Mapping[str, str]) -> dict[str, list[str]]:
    """column path -> the generated ids it holds (uuid, uuid[] and id-named json/text)."""
    out: dict[str, list[str]] = {}

    def walk(path: str, k: str, v: Any, ty: str | None) -> None:
        if v is None:
            return
        if ty == "uuid[]" and isinstance(v, list):
            out.setdefault(path, []).extend(str(x) for x in v)
        elif ty == "uuid" or (isinstance(v, str) and _is_id_key(k) and _UUID_RE.fullmatch(v)):
            out.setdefault(path, []).append(str(v))
        elif isinstance(v, dict):
            for a, b in v.items():
                walk(f"{path}.{a}", a, b, None)
        elif isinstance(v, list):
            for x in v:
                walk(path + "[]", k, x, None)

    for k, v in row.items():
        walk(k, k, v, types.get(k))
    return out


def _scrub(table: str, row: Mapping[str, Any]) -> Mapping[str, Any]:
    """INCIDENTAL_TABLE_VALUES applied to one row of `table` ("{tenant}.<name>" or bare)."""
    name = table.rsplit(".", 1)[-1]
    out = dict(row)
    for (t, col), shape in INCIDENTAL_TABLE_VALUES.items():
        v = out.get(col)
        if t == name and isinstance(v, str) and shape.fullmatch(v):
            out[col] = "<inc:" + col + ">"
    return out


def _mask(row: Mapping[str, Any], types: Mapping[str, str]) -> str:
    """Row text independent of ids, times and install-keyed values (id signatures)."""
    def m(k: str, v: Any, ty: str | None) -> Any:
        if k in INCIDENTAL_VALUES or k in TENANT_FIELDS or k in SCHEMA_FIELDS:
            return ""
        if ty in ("uuid", "uuid[]", "timestamp with time zone") or (
                isinstance(v, str) and (_UUID_RE.fullmatch(v) or k in BUSINESS_TIMES
                                        or k in INCIDENTAL_TIMES)):
            return ""
        if isinstance(v, dict):
            return {a: m(a, b, None) for a, b in sorted(v.items())}
        if isinstance(v, list):
            return [m(k, x, None) for x in v]
        return v
    return json.dumps({k: m(k, v, types.get(k)) for k, v in sorted(row.items())},
                      sort_keys=True, default=str)


def _encoded_delta(side: Side) -> tuple[_Tok, dict[str, tuple[list[Any], list[Any]]]]:
    from collections import Counter

    tok = _Tok(side)
    # Pass 1: collect every generated id and its SIGNATURE: the sorted (table, column,
    # masked row) of each occurrence (masked = ids, times and install-keyed values removed).
    # Ids are then numbered in signature order, so the same business role gets the same
    # token on both installs whatever its random value (bijective per install).
    sig: dict[str, list[str]] = {}
    for cap in (side.after, side.before):
        for t in sorted(cap.tables):
            types = cap.types.get(t, {})
            for r in cap.tables[t]:
                masked = _mask(_scrub(t, r), types)
                for col, ids in _ids_in(r, types).items():
                    for x in ids:
                        if x not in tok.map:
                            sig.setdefault(x, []).append(f"{t}|{col}|{masked}")
    for n, x in enumerate(sorted(sig, key=lambda i: sorted(sig[i]))):
        tok.map[x] = f"id{n}"
    out: dict[str, tuple[list[Any], list[Any]]] = {}
    for t in sorted(set(side.before.tables) | set(side.after.tables)):
        def raw(rows: list[dict[str, Any]]) -> Counter[str]:
            return Counter(json.dumps(r, sort_keys=True, default=str) for r in rows)
        b, a = raw(side.before.tables.get(t, [])), raw(side.after.tables.get(t, []))
        removed = [tok.row(t, json.loads(x), assign=False) for x in (b - a).elements()]
        added = [tok.row(t, json.loads(x), assign=False) for x in (a - b).elements()]
        if removed or added:
            out[t] = (sorted(removed, key=lambda e: e[0]), sorted(added, key=lambda e: e[0]))
    return tok, out


def _times_close(x: list[Any], y: list[Any]) -> bool:
    if len(x) != len(y):
        return False
    for (n1, a1, r1), (n2, a2, r2) in zip(x, y, strict=True):
        if n1 != n2:
            return False
        if abs((a1 - a2).total_seconds()) > TIME_TOL and abs(r1 - r2) > TIME_TOL:
            return False
    return True


def _rows_equal(xs: list[Any], ys: list[Any]) -> bool:
    if [e[0] for e in xs] != [e[0] for e in ys]:
        return False
    return all(_times_close(a[1], b[1]) for a, b in zip(xs, ys, strict=True))


def _ehr_enc(tok: _Tok, state: Any) -> tuple[str, list[Any]]:
    """Structural EHR encoding (no repr): appointment and permit ids through the token map,
    datetimes as typed business times, everything else literal."""
    apps, writes, closed, used, rf, wf = state
    times: list[Any] = []
    obj = {
        "apps": [[tok.map.get(k, k), {f: tok.enc(f, v, None, times, assign=False)
                                      if f not in ("start_at",) else tok.enc(
                                          f, v.isoformat(), None, times, assign=False)
                                      for f, v in fields}] for k, fields in apps],
        "writes": [[tok.map.get(a, a), tok.map.get(p, "?")] for a, p in writes],
        "closed": sorted(tok.map.get(p, "?") for p in closed),
        "used": sorted(tok.map.get(p, "?") for p in used),
        "faults": [rf, wf]}
    return json.dumps(obj, sort_keys=True, default=str), times


def parity_mismatches(a: Side, b: Side) -> list[str]:
    """[] when the two installs' typed deltas are equivalent; otherwise the named
    differences (table / EHR / sender)."""
    ta, da = _encoded_delta(a)
    tb, db = _encoded_delta(b)
    out: list[str] = []
    for t in sorted(set(da) | set(db)):
        ra, aa = da.get(t, ([], []))
        rb, ab = db.get(t, ([], []))
        if not (_rows_equal(ra, rb) and _rows_equal(aa, ab)):
            out.append(f"table {t}: A removed {[e[0] for e in ra]} added {[e[0] for e in aa]}"
                       f" / B removed {[e[0] for e in rb]} added {[e[0] for e in ab]}")
    if (a.ehr is None) != (b.ehr is None):
        out.append("ehr captured on one side only")
    elif a.ehr is not None and b.ehr is not None:
        for i, label in ((0, "before"), (1, "after")):
            ea, eb = _ehr_enc(ta, a.ehr[i]), _ehr_enc(tb, b.ehr[i])
            if ea[0] != eb[0] or not _times_close(ea[1], eb[1]):
                out.append(f"ehr {label}: {ea[0]} != {eb[0]}")
    sa = [(ta.map.get(o, "?"), p, x) for o, p, x in a.sends]
    sb = [(tb.map.get(o, "?"), p, x) for o, p, x in b.sends]
    if sa != sb:
        out.append(f"sends: {sa} != {sb}")
    return out


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
