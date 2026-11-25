"""Eligibility E5 (EL-12): documentation rules — selection, wording, content, schedule pins.
SYNTHETIC ONLY. Pure (no I/O); re-exported by `haloflow.m05.eligibility_doc`.

Governing: E5 requirements v1r1 R2/R3 (Codex ALIGN fa5db003), architecture v1r1 A2/A5
(Codex ALIGN 3671b547, schedule pins 3671b547), owner decisions Q1-Q5 (2026-11-22) and
OD-EL13 (Rachel 2026-11-23: Active / Not active / Inconclusive; active entries drop the
reason).

- One source for the manual routing reasons: `DOC_MANUAL_REASONS` IS E1's
  `eligibility_units.MANUAL_REASONS` (R2.2b); the timer reason `coverage_not_received` is
  never documentable.
- One source for the summary fields: `SUMMARY_FIELDS` IS the t121 tuple (R3.4).
- Reason labels come from the approved OD-EL8 table (`eligibility_labels.REASON_LABELS`).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from haloflow.m05.eligibility_case_units import SUMMARY_FIELDS
from haloflow.m05.eligibility_labels import REASON_LABELS
from haloflow.m05.eligibility_units import MANUAL_REASONS

DOC_MANUAL_REASONS: Final = MANUAL_REASONS
PAYER_CLASSES: Final = ("active", "inactive", "inconclusive")
MANUAL_CLASS: Final = "manual_required"

# OD-EL13 (Rachel 2026-11-23), byte-exact.
RESULT_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"active": "Active", "inactive": "Not active", "inconclusive": "Inconclusive"})
PAYER_PREFIX: Final = "Eligibility check: "
MANUAL_PREFIX: Final = "Insurance needs manual check — "
MANUAL_SUFFIX: Final = "; no payer check was made."

# Schedule (Codex 3671b547): 3 append attempts in total including the initial one; the
# retry delays follow the 1st and 2nd definitive non-write (no third delay).
APPEND_ATTEMPTS: Final = 3
RETRY_DELAYS_S: Final = (60, 300)
READ_DELAYS_S: Final = (60, 300, 900)
CONFIRM_READ_INTERVAL_S: Final = 3600
CONFIRM_READS_MAX: Final = 24
OPS_PER_PASS: Final = 20
PORT_TIMEOUT_S: Final = 5.0
LEASE_S: Final = 60
ENQUEUE_LIMIT: Final = 100

# Owner decision 4 (2026-11-22): the staff item.
DOC_KIND: Final = "eligibility_doc_check"
DOC_TITLE: Final = "Chart entry needs a check"
OUTCOME_CONFIRMED: Final = "entry_confirmed"
OUTCOME_MANUAL: Final = "manual_documentation"

ENTRY_KEYS: Final = frozenset({"text", "class", "reason", "coverage_version", "event_at",
                               "checked_at", "summary"})


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def selection_sql(schema: str) -> str:
    """The documentable-event selection (architecture A2), rendered from the constants: a
    payer check (`verification_id IS NOT NULL`, a payer class) or a manual routing
    (`verification_id IS NULL`, `manual_required`, a reason in DOC_MANUAL_REASONS). It reads
    the EVENT only, never case state. Columns: event_id, appointment_ref, created_at."""
    return (
        "SELECT e.event_id, e.appointment_ref, e.created_at"
        f" FROM {schema}.m05_el_case_events AS e"
        " WHERE e.kind = 'open_or_note' AND ("
        f"(e.verification_id IS NOT NULL AND e.class IN ({_in(PAYER_CLASSES)}))"
        f" OR (e.verification_id IS NULL AND e.class = '{MANUAL_CLASS}'"
        f" AND e.reason IN ({_in(tuple(DOC_MANUAL_REASONS))})))")


def entry_text(cls: str, reason: str) -> str:
    """Owner decision 2 + OD-EL13 wording. Payer: "Eligibility check: Active" (active) or
    "Eligibility check: <result> — <reason label>"; manual: "Insurance needs manual check —
    <reason label>; no payer check was made.". Any other pairing -> ValueError."""
    if cls == "active":
        if reason != "coverage_active":
            raise ValueError("NOT_DOCUMENTABLE")
        return PAYER_PREFIX + RESULT_LABELS["active"]
    if cls in ("inactive", "inconclusive"):
        if reason not in REASON_LABELS or reason in DOC_MANUAL_REASONS:
            raise ValueError("NOT_DOCUMENTABLE")
        if cls == "inactive" and reason != "coverage_inactive":
            raise ValueError("NOT_DOCUMENTABLE")
        if cls == "inconclusive" and reason == "coverage_inactive":
            raise ValueError("NOT_DOCUMENTABLE")
        return f"{PAYER_PREFIX}{RESULT_LABELS[cls]} — {REASON_LABELS[reason]}"
    if cls == MANUAL_CLASS:
        if reason not in DOC_MANUAL_REASONS:
            raise ValueError("NOT_DOCUMENTABLE")
        return f"{MANUAL_PREFIX}{REASON_LABELS[reason]}{MANUAL_SUFFIX}"
    raise ValueError("NOT_DOCUMENTABLE")


def render_entry(event: Mapping[str, Any], answer: Mapping[str, Any] | None) -> dict[str, Any]:
    """The entry body for one documentable event (R3): exactly ENTRY_KEYS. `summary` only for
    a payer active / inactive entry whose answer carries a permitted field, drawn from
    SUMMARY_FIELDS of THAT answer; `checked_at` is the answer's observation time. Nothing else
    of the answer is read (never identity, coinsurance, deductible total, txn ref or raw)."""
    cls = event["class"]
    reason = event["reason"]
    text = entry_text(cls, reason)
    payer = cls in PAYER_CLASSES
    summary: dict[str, Any] | None = None
    checked_at: Any = None
    if payer and answer is not None:
        checked_at = answer.get("observed_at")
        if cls in ("active", "inactive"):
            picked = {f: answer[f] for f in SUMMARY_FIELDS if f in answer}
            summary = picked or None
    return {"text": text, "class": cls, "reason": reason,
            "coverage_version": event.get("coverage_version"),
            "event_at": event.get("created_at"), "checked_at": checked_at,
            "summary": summary}
