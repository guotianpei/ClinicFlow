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
  (architecture v1r1 A3 + the context-bound clarification). Outcomes: APPENDED, EXISTS,
  ALREADY_USED, PERMIT_CLOSED, PERMIT_CONFLICT, REFUSED_MISMATCH, TENANT_MISMATCH, ABSENT.
haloflow.synthetic.profiles: TARGETS["eligibility"] == 134 (t128..t134); TARGETS["default"] == 97.
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
