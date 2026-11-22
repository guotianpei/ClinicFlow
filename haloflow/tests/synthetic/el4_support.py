"""Eligibility E4: shared test support (TEST ONLY; never imported by `src`). RED BY DESIGN.

Governing records (Shared Workspace), Codex technical review on each:
- `Work Session 2026-11-21/claude_e4-test-packet-draft-v1.md` sha256 a0095d97...fd01
  (Codex REVISE `codex_e4-draft-v1-review.md`, e486fe64...)
- `Work Session 2026-11-21/claude_e4-test-packet-draft-v1r1.md` (R1-R4; Codex ALIGN
  `codex_e4-v1r1-review.md`, with the P08b correction: dispatch commits before submit, so a
  stop timeout while submit is held leaves a DURABLE attempt for recovery (E2-C01); pin its
  identity and allowed recovery state, never zero rows; the successful drain asserts the
  expected settlement)
- Owner: OD-EL10 (no owner gate between E1-E4 packages), OD-EL11 (item titles, Rachel
  2026-11-21), OD-EL4..EL9, OD-EL5 (simulated EHR documentation later).
Baseline: merged E3b, `main` 3a8b884. Synthetic only; simulated clearinghouse; no deployment.

Red classes (as B3a..B5, EL1..EL3b): every row states its class in its docstring.
- C  control: passes on the baseline and must keep passing.
- B  behaviour-red: runs on the baseline and fails with a NAMED assertion about baseline
     behaviour (for example the frozen scheduler set or the closed route inventory).
- A  absence-red: fails in its own body on the missing E4 interface (ModuleNotFoundError
     for `haloflow.synthetic.profiles`, TypeError / AttributeError for the new keyword or
     name on an existing callable, or the E4 route not served).

E4 interface pinned by these rows (implementation follows the red gate):

haloflow.synthetic.profiles (new module):
  DEFAULT = "default"; ELIGIBILITY = "eligibility"; PROFILES = (DEFAULT, ELIGIBILITY)
  STATE_FIELD = "install_profile"   (top-level state.json key; NOT `seal.profile`, which is
                                     the installed-state digest id)
  TARGETS = {"default": 97, "eligibility": 126}
  read_profile(raw: bytes) -> str   the state file bytes; field absent -> "default";
      present -> its value when in PROFILES; unknown value, non-string, or the field given
      twice in the JSON object -> SyntheticRefused("PROFILE_INVALID")
  check_request(installed: str, requested: str) -> None
      equal -> None; different -> SyntheticRefused("PROFILE_MISMATCH")
  registry(profile) / catalog(profile): default -> build_synthetic_registry() /
      build_synthetic_catalog(); eligibility -> include_el1..include_el3b=True (target 126)
  schedulers(profile) -> tuple[str, ...]
      default -> ("due", "reminder", "writeback"); eligibility -> + ("eligibility",)
provision.init(environ, *, attest_flag, profile="default"): records STATE_FIELD only for
  the eligibility profile (a default install's state.json keeps the pre-E4 key set);
  status(environ) adds "install_profile" and "target_version".
__main__: `init --profile {default,eligibility}` (default when omitted).
SyntheticLifecycle(environ, ..., profile="default", clearinghouse_factory=None):
  the startup REQUEST. Before any pool opens or the runtime marker is written:
  check_request(installed, requested) -> refusal PROFILE_MISMATCH; an eligibility state
  whose installed registry target is not 126 -> PROFILE_TARGET_MISMATCH. The refusal is
  raised from `start()` as SyntheticRefused with that code (state file bytes and mtime
  unchanged). Eligibility profile: `lc.clearinghouse` (the composed SimulatedClearinghouse
  or the factory's object), `lc.schedulers["eligibility"]` (EligibilityScheduler with the
  E3a cases), started, drained and stopped like "writeback"; `lc.install_profile`.
Routes (eligibility profile only; default profile -> 404 / UNKNOWN_SCHEDULER):
  POST /synthetic/simulator/clearinghouse  JSON, CSRF header; body exactly one of
      {"member": {"member_id": "SYN-...", "status": "active"|"inactive"|"error",
                  "error": <E2 error code, only with "error">}}
      {"fail_next": n} | {"malformed_next": n}   n int 0..10
      -> 200 {"simulated": true, "outcome": "applied"}; anything else 422 MALFORMED, no change
  POST /synthetic/simulator/ui/clearinghouse  form twin (fields csrf, op, member_id,
      status, error, n), 303 back to the simulator page
  POST /synthetic/ui/items/{id}/contact and /synthetic/ui/items/{id}/close  staff forms
      for eligibility items; hidden fields csrf, case_id, item_id, case_version (the E3b view
      token); CASE_CHANGED -> 409 page with CASE_CHANGED_MESSAGE, zero effects
scenarios: NAMES gains E1..E5; Row gains `coverage: Mapping | None` (None on S rows,
  required on E rows; the E1 coverage block shape; a present block's member_id matches
  MEMBER_ID, its payer_id is a synthetic payer); validate() refuses anything else with
  ScenarioInvalid("coverage").
haloflow.synthetic.sim_clearinghouse (new module): parse(value) -> ("member", (member_id,
  status, error)) | ("fail_next", n) | ("malformed_next", n); ControlMalformed(ValueError)
  for anything else (the route maps it to 422 MALFORMED before any state change).
Eligibility install settings (D-E4-7, new; technical): the eligibility profile's
  `settings` phase also writes m05_el_settings (enabled true, lead_days 3,
  not_received_days 1) and m05_el_priority_payers = PRIORITY_PAYERS. Default profile: no
  eligibility rows at all (target 97 has no eligibility tables).
ui: KIND_LABELS gains the three OD-EL11 titles; MESSAGES unchanged ("conflict" is the
  CASE_CHANGED reload message); _SERVICE_MESSAGES["CASE_CHANGED"] == "conflict";
  case_token_fields(case_id, item_id, case_version) -> list[(name, value)] hidden fields
  and parse_case_token(fields: Mapping[str, str]) -> (case_id, item_id, case_version),
  ValueError on any malformed / missing part.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import b4_support as b4

# ---------------------------------------------------------------------------- pins

DEFAULT: Final = "default"
ELIGIBILITY: Final = "eligibility"
STATE_FIELD: Final = "install_profile"
DEFAULT_TARGET: Final = 97
ELIGIBILITY_TARGET: Final = 126
B5_SCHEDULERS: Final = ("due", "reminder", "writeback")
EL_SCHEDULERS: Final = (*B5_SCHEDULERS, "eligibility")

CLEARINGHOUSE_ROUTE: Final = "/synthetic/simulator/clearinghouse"
CLEARINGHOUSE_FORM: Final = "/synthetic/simulator/ui/clearinghouse"
LOAD_ROUTE: Final = "/synthetic/simulator/load-scenario"
TICK_ROUTE: Final = "/synthetic/simulator/tick"
UI_PREFIX: Final = "/synthetic/ui/items"
MEMBER_ID: Final = re.compile(r"SYN-[A-Z0-9]{1,12}(?:-[A-Z0-9]{1,8})?")
EL_SCENARIOS: Final = ("E1", "E2", "E3", "E4", "E5")
# Each E scenario is ONE row with key "el", lead_days 10 (FAR_LEAD_DAYS: only the booking
# trigger is due at load), ehr true; member ids below (E4 is incomplete: member_id null).
EL_ROW_KEY: Final = "el"
EL_LEAD_DAYS: Final = 10
EL_MEMBERS: Final[dict[str, str | None]] = {"E1": "SYN-E1-001", "E4": None, "E5": "SYN-E5-001"}
PRIORITY_PAYERS: Final = ("SYN-PAYER-A",)
NONPRIORITY_PAYER: Final = "SYN-PAYER-Z"
# Expected coverage state per E scenario (v1 D-E4-4): (state, payer class, completeness).
EL_SCENARIO_SHAPE: Final[dict[str, tuple[str, str | None, str | None]]] = {
    "E1": ("present", "priority", "complete"),
    "E2": ("none", None, None),
    "E3": ("pending", None, None),
    "E4": ("present", "priority", "incomplete"),
    "E5": ("present", "nonpriority", "complete"),
}
# Expected first outcome after the booking tick (P04a/b): (item kind or None, reason or None).
EL_SCENARIO_OUTCOME: Final[dict[str, tuple[str | None, str | None]]] = {
    "E1": (None, None),
    "E2": ("eligibility_manual", "no_coverage"),
    "E4": ("eligibility_manual", "incomplete"),
    "E5": ("eligibility_manual", "nonpriority"),
}
COVERAGE_PRESENT_KEYS: Final = frozenset({
    "state", "payer_id", "member_id", "relationship", "patient_name", "patient_dob",
    "subscriber_name", "subscriber_dob", "subscriber_member_id"})

# OD-EL11 (Rachel 2026-11-21), byte-exact.
EL_TITLES: Final[dict[str, str]] = {
    "eligibility_inactive": "Insurance not active — review coverage",
    "eligibility_inconclusive": "Insurance check inconclusive — review",
    "eligibility_manual": "Insurance needs manual check",
}
# OD-EL8 reasons (E3a), OD-EL6 outcomes and OD-EL7 contact entries (E3b): byte-exact copies
# here, independent of `m05.eligibility_labels` (the oracle never reads the code under test).
EL_REASONS: Final[dict[str, str]] = {
    "coverage_inactive": "Payer reports coverage not active",
    "subscriber_not_found": "Payer could not find the member",
    "name_dob_mismatch": "Name or date of birth does not match payer records",
    "member_id_not_recognized": "Member ID not recognized by payer",
    "service_type_unsupported": "Payer could not answer for this service type",
    "payer_unavailable": "Payer did not respond (tried twice)",
    "response_unreadable": "Payer response could not be read",
    "nonpriority": "Payer not set up for automatic checks",
    "no_coverage": "No insurance on file",
    "incomplete": "Insurance details incomplete",
    "coverage_not_received": "Insurance not received from EHR",
}
EL_OUTCOMES: Final[dict[str, str]] = {
    "el_verified_active": "Verified active by phone or payer portal",
    "el_confirmed_inactive": "Confirmed not active",
    "el_insurance_updated": "Insurance updated in EHR",
    "el_self_pay": "Patient will self-pay",
    "el_appt_cancelled": "No action – appointment cancelled",
    "el_other": "Other",
}
EL_CONTACTS: Final[dict[str, str]] = {
    "el_left_message": "Left message for patient",
    "el_called_no_answer": "Called patient – no answer",
    "el_spoke_followup": "Spoke with patient – follow-up needed",
    "el_payer_awaiting": "Contacted payer – awaiting answer",
}
# The existing OD-B4-2 conflict message is reused for CASE_CHANGED (no new staff wording).
CASE_CHANGED_MESSAGE: Final = "This item changed since you opened it. Reload to see the latest."
OUTSTANDING_EHR_ROW: Final = "simulated EHR documentation — not built (OD-EL5)"
# P06c canary: distinctive synthetic values that must never reach a log record.
CANARY: Final[dict[str, str]] = {
    "member_id": "SYN-CANARY-7Q4", "patient_dob": "1931-07-04",
    "patient_name": "Quillon Vantablack", "payer_free_text": "PAYERCANARY ZX9",
    "note": "NOTECANARY-K2W",
}
# E2 timing (Codex v1r1 review; relay 6fe473a9): database time. The retry delay after a failed
# attempt is 120 s; boundary rows age the attempt's finished_at against clock_timestamp().
RETRY_DELAY_S: Final = 120.0

FIXTURES: Final = Path(__file__).parent / "fixtures"
# Codex codex_e4-red-v1-review.md finding 1: the GENUINE pre-E4 writer (3a8b884 synthetic
# package, frozen and blob-verified by the child), run in a fresh process.
PRE_E4_ZIP: Final = FIXTURES / "el4_3a8b884_synthetic.zip"
PRE_E4_ZIP_SHA256: Final = "5cfe225cfa5d29efc1f3e18e3a486b22fb7690d311d7118bce88ce9d19345e21"
PRE_E4_CHILD: Final = Path(__file__).parent / "el4_legacy_child.py"
PRE_E4_STATE: Final = FIXTURES / "el4_pre_e4_state.json"
REPO: Final = s.HALOFLOW.parent
SCENARIO_DIR: Final = s.SRC / "haloflow" / "synthetic" / "scenarios"


def mod(name: str) -> Any:
    """Lazy import of `haloflow.synthetic.<name>` inside a row body (red rule, as B2)."""
    return s.mod(name)


# ---------------------------------------------------------------------------- install

def install(c: s.PgCluster, tmp_path: Path, profile: str | None) -> b3.Install:
    """A fresh install to `ready`. `profile=None`: the baseline call (no keyword), i.e. the
    pre-E4 install. Otherwise the E4 keyword (absent at baseline -> TypeError, class A)."""
    st_dir = s.state_dir(tmp_path)
    init = mod("provision").init
    env = s.environ_for(c, st_dir)
    r = init(env, attest_flag=True) if profile is None \
        else init(env, attest_flag=True, profile=profile)
    assert r.phase == "ready", r
    return b3.Install(c, st_dir, tmp_path)


def pre_e4_run(env: dict[str, str], tmp: Path, action: str) -> dict[str, Any]:
    """Run the frozen 3a8b884 installer (`init` | `status`) in a fresh isolated process."""
    import hashlib
    import os
    import subprocess
    import sys

    assert hashlib.sha256(PRE_E4_ZIP.read_bytes()).hexdigest() == PRE_E4_ZIP_SHA256
    payload = json.dumps({"zip": str(PRE_E4_ZIP), "extract_to": str(tmp / f"pre-e4-{action}"),
                          "alembic": str(s.HALOFLOW / "alembic"), "environ": env,
                          "action": action})
    p = subprocess.run([sys.executable, "-I", str(PRE_E4_CHILD), payload], capture_output=True,
                       text=True, timeout=900, env={"PATH": os.environ.get("PATH", "")},
                       check=False)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("EL4LEGACY ")]
    assert lines, p.stdout[-2000:] + p.stderr[-2000:]
    return dict(json.loads(lines[-1][len("EL4LEGACY "):]))


def pre_e4_install(c: s.PgCluster, tmp_path: Path) -> b3.Install:
    """A ready install whose state file and database were written by the frozen PRE-E4
    writer (nested seal, logins, support and note-key formats as 3a8b884 wrote them)."""
    st_dir = s.state_dir(tmp_path)
    out = pre_e4_run(s.environ_for(c, st_dir), tmp_path, "init")
    assert out == {"ok": True, "phase": "ready"}, out
    return b3.Install(c, st_dir, tmp_path)


class PoolCounter:
    """Codex finding 2: counts every runtime pool construction (TenantPool.__init__ as bound in
    `haloflow.synthetic.runtime`, and SyntheticRuntime.open_pool) without changing behaviour."""

    def __init__(self, monkeypatch: Any) -> None:
        runtime = mod("runtime")
        self.constructed = 0
        self.opened = 0
        real_pool = runtime.TenantPool
        real_open = runtime.SyntheticRuntime.open_pool
        counter = self

        class CountingPool(real_pool):  # type: ignore[misc, valid-type]
            def __init__(self, *a: Any, **k: Any) -> None:
                counter.constructed += 1
                super().__init__(*a, **k)

        async def open_pool(rt: Any) -> None:
            counter.opened += 1
            await real_open(rt)

        monkeypatch.setattr(runtime, "TenantPool", CountingPool)
        monkeypatch.setattr(runtime.SyntheticRuntime, "open_pool", open_pool)


def state_path(inst: b3.Install) -> Path:
    return inst.st_dir / "state.json"


def state_stat(inst: b3.Install) -> tuple[bytes, int]:
    p = state_path(inst)
    return p.read_bytes(), p.stat().st_mtime_ns


def rewrite_state(inst: b3.Install, **changes: Any) -> None:
    """FIXTURE: rewrite state.json in place (0600 kept); a value of None removes the key."""
    p = state_path(inst)
    value = json.loads(p.read_bytes())
    for k, v in changes.items():
        if v is None:
            value.pop(k, None)
        else:
            value[k] = v
    p.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    p.chmod(0o600)


def installed_target(inst: b3.Install) -> int:
    rows = inst.admin("SELECT schema_version FROM shared.tenants WHERE tenant_id = %s",
                      (str(inst.state["tenant_id"]),))
    return int(rows[0][0])


def lifecycle(inst: b3.Install, *, profile: str | None, app_name: str, **kw: Any) -> Any:
    lc_cls = mod("lifecycle").SyntheticLifecycle
    if profile is None:
        return lc_cls(inst.env, application_name=app_name, **kw)
    return lc_cls(inst.env, application_name=app_name, profile=profile, **kw)


@asynccontextmanager
async def running(inst: b3.Install, *, profile: str | None = ELIGIBILITY,
                  app_name: str = "el4-row", **kw: Any) -> AsyncIterator[tuple[Any, Any]]:
    """A started lifecycle (the E4 `profile` keyword) and the app over it; the row owns
    start and stop (as `b3b_support.running`)."""
    lc = lifecycle(inst, profile=profile, app_name=app_name, **kw)
    await lc.start()
    app = mod("app").create_synthetic_app(lifecycle=lc, bind=bb.BIND)
    try:
        yield lc, app
    finally:
        if lc.state == "RUNNING":
            lc.request_stop("TEST")
        if lc.state == "STOPPING":
            await lc.wait_stopped()


def first_start(inst: b3.Install, profile: str | None) -> None:
    async def body() -> None:
        async with running(inst, profile=profile, app_name="el4-first"):
            pass

    asyncio.run(body())


def pool_backends(inst: b3.Install, app_name: str) -> int:
    return len(b3.backends(inst, app_name))


# ---------------------------------------------------------------------------- requests

async def token(app: Any) -> str:
    return await bb.session_token(app)


async def load(app: Any, tok: str, scenario: str) -> bb.Resp:
    return await bb.asgi(app, "POST", LOAD_ROUTE, headers=bb.post_headers(tok),
                         body=bb.jbody({"scenario": scenario}))


async def tick(app: Any, tok: str, scheduler: str = "eligibility") -> bb.Resp:
    return await bb.asgi(app, "POST", TICK_ROUTE, headers=bb.post_headers(tok),
                         body=bb.jbody({"scheduler": scheduler}))


async def control(app: Any, tok: str, body: Any) -> bb.Resp:
    return await bb.asgi(app, "POST", CLEARINGHOUSE_ROUTE, headers=bb.post_headers(tok),
                         body=bb.jbody(body))


async def member(app: Any, tok: str, member_id: str, status: str,
                 error: str | None = None) -> None:
    m: dict[str, Any] = {"member_id": member_id, "status": status}
    if error is not None:
        m["error"] = error
    r = await control(app, tok, {"member": m})
    assert r.status == 200, (r.status, r.raw[:300])
    bb.assert_labelled(r)
    assert r.json == {"simulated": True, "outcome": "applied"}, r.json


async def page(app: Any, target: str) -> tuple[bb.Resp, b4.Page]:
    r = await bb.asgi(app, "GET", target, headers=bb.get_headers())
    return r, b4.page(r)


async def post_form(app: Any, action: str, fields: list[tuple[str, str]]) -> bb.Resp:
    return await bb.asgi(app, "POST", action, headers=b4.form_headers(),
                         body=b4.form_body(fields))


def tick_counts(r: bb.Resp) -> dict[str, Any]:
    assert r.status == 200, (r.status, r.raw[:300])
    bb.assert_labelled(r)
    assert r.json.get("scheduler") == "eligibility", r.json
    counts = r.json.get("counts")
    assert type(counts) is dict, r.json
    return counts


# ---------------------------------------------------------------------------- database reads

def q(inst: b3.Install, text: str, params: Any = None) -> list[tuple[Any, ...]]:
    return inst.admin(text.replace("{s}", inst.schema), params)


def scenario_ref(inst: b3.Install, scenario: str, key: str) -> str:
    """The appointment ref the scenario loader assigns (scenarios.ids, existing B5 rule)."""
    _patient, appt = mod("scenarios").ids(str(inst.state["tenant_id"]), scenario, key)
    return str(appt)


def verifications(inst: b3.Install, ref: str) -> list[tuple[Any, ...]]:
    """[(verification_id, owner_trigger, state, result_class, reason)] by creation."""
    return q(inst, "SELECT verification_id::text, owner_trigger, state, result_class, reason"
             " FROM {s}.m05_el_verifications WHERE appointment_ref = %s"
             " ORDER BY created_at, owner_trigger", (ref,))


def attempts(inst: b3.Install, verification_id: str) -> list[tuple[Any, ...]]:
    """[(attempt_id, n, state)] by n."""
    return q(inst, "SELECT attempt_id::text, n, state FROM {s}.m05_el_attempts"
             " WHERE verification_id = %s ORDER BY n", (verification_id,))


def triggers(inst: b3.Install, ref: str) -> dict[str, tuple[Any, ...]]:
    got = q(inst, "SELECT trigger, status, verification_id::text FROM {s}.m05_el_triggers"
            " WHERE appointment_ref = %s", (ref,))
    return {r[0]: (r[1], r[2]) for r in got}


def case_events(inst: b3.Install, ref: str) -> list[tuple[Any, ...]]:
    return q(inst, "SELECT seq, kind, class, reason FROM {s}.m05_el_case_events"
             " WHERE appointment_ref = %s ORDER BY seq", (ref,))


def el_items(inst: b3.Install) -> list[tuple[Any, ...]]:
    """[(item_id, item_kind, state)] for the three eligibility kinds, by creation."""
    return q(inst, "SELECT item_id::text, item_kind, state FROM {s}.m05_followup_items"
             " WHERE item_kind = ANY(%s) ORDER BY created_at", (list(EL_TITLES),))


def contact_codes(inst: b3.Install, item_id: str) -> list[str]:
    got = q(inst, "SELECT c.contact_code FROM {s}.m05_contact_entries c"
            " JOIN {s}.m05_followup_history h ON h.history_id = c.history_id"
            " WHERE h.item_id = %s::uuid ORDER BY h.created_at", (item_id,))
    return [str(r[0]) for r in got]


def case_row(inst: b3.Install, item_id: str) -> tuple[Any, ...]:
    """(case_id, state, case_version, current_reason) of the case bound to the item."""
    (row,) = q(inst, "SELECT case_id::text, state, case_version, current_reason"
               " FROM {s}.m05_el_cases WHERE item_id = %s::uuid", (item_id,))
    return row


def item_state(inst: b3.Install, item_id: str) -> tuple[Any, ...]:
    """(state, outcome_code, version) of a follow-up item."""
    (row,) = q(inst, "SELECT state, outcome_code, version FROM {s}.m05_followup_items"
               " WHERE item_id = %s::uuid", (item_id,))
    return row


def outbox_count(inst: b3.Install) -> int:
    return int(q(inst, "SELECT count(*) FROM {s}.m05_outbound_ops")[0][0])


def make_due(inst: b3.Install, ref: str, trigger: str, offset_s: float = -1.0) -> None:
    """FIXTURE: a trigger's due_at := database now + offset (E2 make_due pattern)."""
    q(inst, "UPDATE {s}.m05_el_triggers SET due_at = clock_timestamp()"
      " + make_interval(secs => %s) WHERE appointment_ref = %s AND trigger = %s",
      (offset_s, ref, trigger))


def age_failure(inst: b3.Install, attempt_id: str, offset_s: float) -> None:
    """FIXTURE: a failed attempt's finished_at := database now + offset (E2 retry aging)."""
    q(inst, "UPDATE {s}.m05_el_attempts SET finished_at = clock_timestamp()"
      " + make_interval(secs => %s) WHERE attempt_id = %s", (offset_s, attempt_id))


def age_coverage_deadline(inst: b3.Install, ref: str, offset_s: float) -> None:
    """FIXTURE: the pending-coverage (OD-EL9) deadline := database now + offset."""
    q(inst, "UPDATE {s}.m05_el_appointments SET not_received_due_at = clock_timestamp()"
      " + make_interval(secs => %s) WHERE appointment_ref = %s", (offset_s, ref))


# ---------------------------------------------------------------------------- spies

class SpyClearinghouse:
    """Wraps the composed SimulatedClearinghouse (via `clearinghouse_factory`): records each
    submit's attempt id and can hold a submit on an event the row controls (P08)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.submits: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def submit(self, attempt_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        self.submits.append(attempt_id)
        self.requests.append(dict(request))
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        result: dict[str, Any] = await self.inner.submit(attempt_id, request)
        return result


def spy_factory() -> tuple[list[SpyClearinghouse], Any]:
    made: list[SpyClearinghouse] = []

    def factory() -> SpyClearinghouse:
        from datetime import UTC, datetime

        from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

        spy = SpyClearinghouse(SimulatedClearinghouse(clock=lambda: datetime.now(UTC)))
        made.append(spy)
        return spy

    return made, factory


@dataclass
class Order:
    """P08 order oracle: named events in arrival order."""

    events: list[str]

    def mark(self, name: str) -> None:
        self.events.append(name)

    def before(self, a: str, b: str) -> bool:
        return a in self.events and b in self.events and self.events.index(a) < self.events.index(b)
