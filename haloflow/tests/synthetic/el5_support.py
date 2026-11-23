"""Eligibility E5 (EL-12): shared test support (TEST ONLY; never imported by `src`). RED BY DESIGN.

Governing records (Shared Workspace, `Work Session 2026-11-23/`), Codex technical review on each:
- requirements `claude_e5-el12-requirements-v1r1-2026-11-23.md` (Codex ALIGN fa5db003)
- source recheck `claude_e5-source-recheck-2026-11-23.md` (merged main 3ed2433)
- architecture `claude_e5-el12-architecture-v1r1-2026-11-23.md` (Codex ALIGN 3671b547), with the
  context-bound port clarification in the test cases v1r1
- test cases `claude_e5-el12-test-cases-v1r1-2026-11-23.md` (Codex ALIGN 9e6974f2)
- Owner: decisions Q1-Q5 (2026-11-22, `Work Session 2026-11-22/codex_el12-owner-decisions.md`);
  OD-EL13 (2026-11-23, result wording: Active / Not active / Inconclusive; active drops the reason).
Baseline: merged E4, `main` 8ca2217. Synthetic only; simulated chart only; no deployment.

Red classes (as E4): every row states its class in its docstring.
- C  control: passes on the baseline and must keep passing.
- B  behaviour-red: runs on the baseline and fails with a NAMED assertion about baseline behaviour.
- A  absence-red: fails in its own body on the missing E5 interface.

E5 interface pinned by these rows (implementation follows the red gate):

haloflow.m05.eligibility_doc (new module):
  DOC_MANUAL_REASONS = eligibility_units.MANUAL_REASONS  (the same tuple object; R2.2b)
  SUMMARY_FIELDS     = eligibility_case_units.SUMMARY_FIELDS  (the same tuple object; R3.4)
  RESULT_LABELS = {"active": "Active", "inactive": "Not active", "inconclusive": "Inconclusive"}
                                                        (OD-EL13)
  APPEND_ATTEMPTS = 3; RETRY_DELAYS_S = (60, 300)
                                  (3 attempts incl. the initial; Codex 3671b547)
  READ_DELAYS_S = (60, 300, 900); CONFIRM_READ_INTERVAL_S = 3600; CONFIRM_READS_MAX = 24
  OPS_PER_PASS = 20; PORT_TIMEOUT_S = 5.0
  entry_text(cls: str, reason: str) -> str
      payer: "Eligibility check: Active" (active) |
             "Eligibility check: <RESULT_LABELS[cls]> — <REASON_LABELS[reason]>"
      manual: "Insurance needs manual check — <REASON_LABELS[reason]>; no payer check was made."
      anything else -> ValueError
  render_entry(event: Mapping, answer: Mapping | None) -> dict
      keys exactly ENTRY_KEYS; `summary` only for a payer active/inactive entry with an answer,
      drawn from SUMMARY_FIELDS of that answer; never identity, coinsurance, txn ref, raw response.
  selection_sql(schema: str) -> str  the rendered documentable-event predicate (A2)
haloflow.m05.simulated_chart (new module): SimulatedChart over the synthetic cluster's `sim_chart`
  schema; `for_tenant(ctx)` -> TenantChart with issue_permit / append / close_permit / read
  (architecture v1r1 A3 + the context-bound clarification). Outcomes: ISSUED, APPENDED, EXISTS,
  ALREADY_USED, PERMIT_CLOSED, PERMIT_CONFLICT, REFUSED_MISMATCH, TENANT_MISMATCH, ABSENT.
  `SimulatedChart(conninfo)`; `.conninfo`; TenantChart methods are async:
  issue_permit(permit_id, event_id, appointment_ref) -> ISSUED | refusal outcome;
  append(permit_id, body) -> APPENDED | EXISTS | ALREADY_USED | PERMIT_CLOSED | TENANT_MISMATCH;
  close_permit(permit_id) -> None-equivalent success | ALREADY_USED | TENANT_MISMATCH;
  read(event_id) -> {"appointment_ref", "permit_id", "body"} | ABSENT.
haloflow.synthetic.profiles: TARGETS["eligibility"] == 134 (t128..t134); TARGETS["default"] == 97.

Composition (eligibility profile; PostgreSQL rows):
  lc.chart        the composed SimulatedChart over the install's cluster (schema `sim_chart`);
                  `lc.chart.for_tenant(lc.tenant_context())` is the worker's port.
  lc.tenant_context()  the authorized TenantContext the eligibility scheduler uses.
  lc.doc_service  the DocService; the eligibility scheduler's tick runs phase `document` after
                  `apply` and reports counts under keys "document.<name>".
  lc.doc_service.manual(staff_ctx, item_id: str, expected_version: int) -> str
                  the staff "Recorded in chart manually" action (t134) — the authorized
                  entry point: `staff_ctx` is an authorized staff TenantContext; its tenant must
                  be the item's and its staff id is the recorded attribution. Returns the item
                  outcome "entry_confirmed" | "manual_documentation"; refusals (closed item,
                  stale version, op not in needs_check, another tenant's staff) raise
                  eligibility_doc.DocRefused(code) with no state change.
  lc.staff_context(staff_id: str) -> the authorized staff TenantContext of the install's tenant.
  eligibility_doc.staff_context_for(tenant_id: str, staff_id: str) -> an authorized-shape staff
                  context of another (synthetic) tenant, for the cross-tenant refusal row.
  lc.doc_service.hooks: dict[str, Callable[[], None]]  test seams, keys "after_claim",
                  "after_mark_sent", "after_manual_phase1"; a hook that raises
                  eligibility_doc.SimulatedCrash aborts the pass at that point (the process
                  "crashes"; durable state is whatever committed before it).
SimulatedChart test controls (synthetic only): fail_next(n) refuse before write;
  unknown_next(n) commit, then drop the acknowledgement; hold_commits() / release_commits()
  (a held append blocks before commit; release lets it commit unless its permit was closed,
  in which case it returns PERMIT_CLOSED and writes nothing); reads_unavailable(n) the next n
  reads time out; `port_timeout_override` (seconds, None = PORT_TIMEOUT_S) bounds a port call
  in rows that hold commits; `calls` list of (operation, permit_id or None) in call order.
Tick counts (document phase) include "document.refused_mismatch" and "document.foreign_permit".
simulated_chart.context_for(tenant_id: str) -> TenantContext for a second synthetic tenant
  (store-level isolation rows only; the synthetic install has one tenant).
Tables: {s}.m05_el_doc_ops (architecture A4: event_id, appointment_ref, state, epoch, permit_id,
  permit_sent, attempts, reads, confirm_reads, last_failure, lease_until, next_at, item_id,
  manual_by, created_at);
  sim_chart.permits(permit_id, tenant, event_id, appointment_ref, state);
  sim_chart.entries(tenant, event_id, appointment_ref, permit_id, body, committed_at).
  Doc item kind "eligibility_doc_check", title "Chart entry needs a check" (owner decision 4).
Schedules are in database time; rows age `next_at` (the E2 make_due pattern).
"""

from __future__ import annotations

import importlib
from typing import Any, Final

# ---------------------------------------------------------------------------- pins

DEFAULT_TARGET: Final = 97
E4_ELIGIBILITY_TARGET: Final = 127
ELIGIBILITY_TARGET: Final = 134

# E1 routing reasons (eligibility_units.py L437-445 on 3ed2433), independent copy (oracle).
MANUAL_REASONS: Final = ("nonpriority", "no_coverage", "incomplete")
TIMER_REASON: Final = "coverage_not_received"
PAYER_CLASSES: Final = ("active", "inactive", "inconclusive")

# OD-EL13 (Rachel 2026-11-23), byte-exact.
RESULT_LABELS: Final[dict[str, str]] = {
    "active": "Active", "inactive": "Not active", "inconclusive": "Inconclusive"}
# OD-EL8 reasons used by E5 entries, byte-exact copies (never read from the code under test).
REASON_LABELS: Final[dict[str, str]] = {
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
}
SUMMARY_FIELDS: Final = ("payer_name", "plan", "copay", "deductible_remaining",
                         "referral_required", "auth_required")
ENTRY_KEYS: Final = frozenset({"text", "class", "reason", "coverage_version", "event_at",
                               "checked_at", "summary"})

# Schedule (Codex 3671b547): 3 append attempts in total including the initial attempt.
APPEND_ATTEMPTS: Final = 3
RETRY_DELAYS_S: Final = (60, 300)
READ_DELAYS_S: Final = (60, 300, 900)
CONFIRM_READ_INTERVAL_S: Final = 3600
CONFIRM_READS_MAX: Final = 24
OPS_PER_PASS: Final = 20
PORT_TIMEOUT_S: Final = 5.0

# U02 sentinels: distinctive values that must never appear in an entry body.
SENTINELS: Final[dict[str, str]] = {
    "member_id": "SYN-SENTINEL-9X1", "patient_name": "Ozymandias Quickfeather",
    "patient_dob": "1929-02-29", "coinsurance": "COINS-SENTINEL-55",
    "txn_ref": "TXN-SENTINEL-4K2", "raw": "RAW-SENTINEL-PAYLOAD",
    "deductible_total": "DEDTOT-SENTINEL-77",
}
SUMMARY_VALUES: Final[dict[str, Any]] = {
    "payer_name": "Synthetic Payer A", "plan": "Synthetic PPO", "copay": "25.00",
    "deductible_remaining": "150.00", "referral_required": False, "auth_required": False,
}


def m05(name: str) -> Any:
    """Lazy import of `haloflow.m05.<name>` inside a row body (absence fails in the row)."""
    return importlib.import_module(f"haloflow.m05.{name}")


def synthetic(name: str) -> Any:
    """Lazy import of `haloflow.synthetic.<name>` inside a row body."""
    return importlib.import_module(f"haloflow.synthetic.{name}")


def full_answer(cls: str, reason: str) -> dict[str, Any]:
    """An attempt answer with every permitted field AND every sentinel populated."""
    out: dict[str, Any] = {"class": cls, "reason": reason, "observed_at": "2026-11-20T15:04:05Z"}
    out.update(SUMMARY_VALUES)
    out.update(SENTINELS)
    return out


def event(cls: str, reason: str, *, verification: bool) -> dict[str, Any]:
    """A case-event mapping as the per-event reader receives it."""
    return {"event_id": "00000000-0000-4000-8000-000000000001", "kind": "open_or_note",
            "class": cls, "reason": reason, "coverage_version": 2,
            "created_at": "2026-11-20T15:04:06Z",
            "verification_id": "00000000-0000-4000-8000-0000000000aa" if verification else None}


# ---------------------------------------------------------------------------- database reads

DOC_KIND: Final = "eligibility_doc_check"
DOC_TITLE: Final = "Chart entry needs a check"


def ops(inst: Any) -> list[tuple[Any, ...]]:
    """[(event_id, state, attempts, reads, confirm_reads, permit_id, permit_sent, item_id)]."""
    return inst.admin(
        f"SELECT event_id::text, state, attempts, reads, confirm_reads, permit_id::text,"
        f" permit_sent, item_id::text FROM {inst.schema}.m05_el_doc_ops ORDER BY created_at")


def op(inst: Any, event_id: str) -> dict[str, Any]:
    (row,) = [r for r in ops(inst) if r[0] == event_id]
    keys = ("event_id", "state", "attempts", "reads", "confirm_reads", "permit_id",
            "permit_sent", "item_id")
    return dict(zip(keys, row, strict=True))


def entries(inst: Any, tenant: str | None = None) -> list[tuple[Any, ...]]:
    """[(tenant, event_id, appointment_ref, permit_id, body)] from the simulated chart."""
    rows = inst.admin("SELECT tenant, event_id::text, appointment_ref::text, permit_id::text,"
                      " body FROM sim_chart.entries ORDER BY committed_at")
    return [r for r in rows if tenant is None or r[0] == tenant]


def entry_rows_raw(inst: Any, event_id: str) -> list[tuple[Any, ...]]:
    """Every column of the entries for one event, for byte-identity checks."""
    return inst.admin("SELECT tenant, event_id::text, appointment_ref::text, permit_id::text,"
                      " body::text, committed_at FROM sim_chart.entries WHERE event_id = %s",
                      (event_id,))


def permits(inst: Any, event_id: str) -> list[tuple[Any, ...]]:
    """[(permit_id, state)] for one event."""
    return inst.admin("SELECT permit_id::text, state FROM sim_chart.permits"
                      " WHERE event_id = %s ORDER BY issued_at", (event_id,))


def doc_items(inst: Any) -> list[tuple[Any, ...]]:
    """[(item_id, state, outcome_code, version)] of the doc-check kind."""
    return inst.admin(f"SELECT item_id::text, state, outcome_code, version FROM"
                      f" {inst.schema}.m05_followup_items WHERE item_kind = %s"
                      f" ORDER BY created_at", (DOC_KIND,))


def events(inst: Any, ref: str) -> list[tuple[Any, ...]]:
    """[(event_id, kind, class, reason, verification_id)] for one appointment, by seq."""
    return inst.admin(f"SELECT event_id::text, kind, class, reason, verification_id::text FROM"
                      f" {inst.schema}.m05_el_case_events WHERE appointment_ref = %s ORDER BY seq",
                      (ref,))


def age_op(inst: Any, event_id: str, offset_s: float = -1.0) -> None:
    """FIXTURE: an op's next_at := database now + offset (makes a scheduled step due)."""
    inst.admin(f"UPDATE {inst.schema}.m05_el_doc_ops SET next_at = clock_timestamp()"
               f" + make_interval(secs => %s) WHERE event_id = %s", (offset_s, event_id))


def calls(chart: Any, operation: str) -> int:
    return sum(1 for c in chart.calls if c[0] == operation)
