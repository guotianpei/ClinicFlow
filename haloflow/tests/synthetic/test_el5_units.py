"""Eligibility E5 (EL-12) unit rows (no database). RED BY DESIGN on merged E4 (8ca2217).

Governing: E5 test cases v1r1 (Codex ALIGN 9e6974f2) rows U01-U03 plus the schedule, wording
(OD-EL13) and profile pins; architecture v1r1 A2/A5. Classes C / B / A as `el5_support` states;
each row names its class. Interface pins: `el5_support` module docstring.
"""

from __future__ import annotations

import json
from typing import Any

import el5_support as e5
import pytest

# ---------------------------------------------------------------------------- controls (C)


def test_el5_c01_e1_manual_reasons_baseline() -> None:
    """C. The E1 routing reason set on the baseline is exactly the three manual reasons, and the
    timer reason is a separate event reason (source recheck, eligibility_units.py)."""
    units = e5.m05("eligibility_units")
    assert tuple(units.MANUAL_REASONS) == e5.MANUAL_REASONS
    assert e5.TIMER_REASON in units.EVENT_REASONS
    assert e5.TIMER_REASON not in units.MANUAL_REASONS


def test_el5_c02_summary_fields_baseline() -> None:
    """C. The OD-EL4 field list on the baseline (t121 source of truth)."""
    assert tuple(e5.m05("eligibility_case_units").SUMMARY_FIELDS) == e5.SUMMARY_FIELDS


def test_el5_c03_reason_labels_baseline() -> None:
    """C. The OD-EL8 labels E5 entries use are unchanged on the baseline; `coverage_active` has
    no staff label (the OD-EL13 basis for dropping the reason on active entries)."""
    labels = e5.m05("eligibility_labels").REASON_LABELS
    for code, text in e5.REASON_LABELS.items():
        assert labels[code] == text, code
    assert "coverage_active" not in labels


def test_el5_c04_default_profile_target() -> None:
    """C. The default synthetic profile target stays 97 (E5 composes only into eligibility)."""
    assert e5.synthetic("profiles").TARGETS["default"] == e5.DEFAULT_TARGET


# ---------------------------------------------------------------------------- module (A)


def test_el5_u00_doc_module_present() -> None:
    """A. The E5 documentation module exists."""
    e5.m05("eligibility_doc")


def test_el5_u00_chart_module_present() -> None:
    """A. The simulated chart port module exists with the context-bound entry point."""
    chart = e5.m05("simulated_chart")
    assert hasattr(chart.SimulatedChart, "for_tenant")
    for name in ("APPENDED", "EXISTS", "ALREADY_USED", "PERMIT_CLOSED", "PERMIT_CONFLICT",
                 "REFUSED_MISMATCH", "TENANT_MISMATCH", "ABSENT"):
        assert hasattr(chart, name), name


# ---------------------------------------------------------------------------- U01 selection (A)


def test_el5_u01_manual_reason_set_is_e1s() -> None:
    """A. DOC_MANUAL_REASONS is E1's MANUAL_REASONS (one source; R2.2b) and equals the oracle."""
    doc = e5.m05("eligibility_doc")
    assert doc.DOC_MANUAL_REASONS is e5.m05("eligibility_units").MANUAL_REASONS
    assert tuple(doc.DOC_MANUAL_REASONS) == e5.MANUAL_REASONS


def test_el5_u01_selection_sql_reason_set() -> None:
    """A. The rendered selection names exactly the three manual reasons and never the timer
    reason (A2: manual routing selected by reason, not by class + NULL verification)."""
    sql = e5.m05("eligibility_doc").selection_sql("t_x")
    for r in e5.MANUAL_REASONS:
        assert f"'{r}'" in sql, r
    assert e5.TIMER_REASON not in sql
    assert "verification_id IS NULL" in sql and "verification_id IS NOT NULL" in sql
    for c in e5.PAYER_CLASSES:
        assert f"'{c}'" in sql, c


# ---------------------------------------------------------------------------- wording (A)


@pytest.mark.parametrize(("cls", "reason", "want"), [
    ("active", "coverage_active", "Eligibility check: Active"),
    ("inactive", "coverage_inactive",
     "Eligibility check: Not active — Payer reports coverage not active"),
    ("inconclusive", "payer_unavailable",
     "Eligibility check: Inconclusive — Payer did not respond (tried twice)"),
    ("inconclusive", "response_unreadable",
     "Eligibility check: Inconclusive — Payer response could not be read"),
    ("manual_required", "no_coverage",
     "Insurance needs manual check — No insurance on file; no payer check was made."),
    ("manual_required", "nonpriority",
     "Insurance needs manual check — Payer not set up for automatic checks;"
     " no payer check was made."),
    ("manual_required", "incomplete",
     "Insurance needs manual check — Insurance details incomplete; no payer check was made."),
])
def test_el5_u04_entry_text(cls: str, reason: str, want: str) -> None:
    """A. Owner decision 2 + OD-EL13, byte-exact."""
    assert e5.m05("eligibility_doc").entry_text(cls, reason) == want


def test_el5_u04_result_labels() -> None:
    """A. OD-EL13 result labels."""
    assert dict(e5.m05("eligibility_doc").RESULT_LABELS) == e5.RESULT_LABELS


@pytest.mark.parametrize(("cls", "reason"), [
    ("manual_required", e5.TIMER_REASON), ("manual_required", "coverage_inactive"),
    ("active", "coverage_inactive"), ("unknown", "no_coverage"), (None, "no_coverage")])
def test_el5_u04_entry_text_refuses(cls: Any, reason: str) -> None:
    """A. No wording exists for a non-documentable pairing (incl. the timer reason)."""
    with pytest.raises(ValueError):
        e5.m05("eligibility_doc").entry_text(cls, reason)


def test_el5_u04_manual_never_says_checked() -> None:
    """A. A manual entry never states or implies a payer check (R3.2)."""
    doc = e5.m05("eligibility_doc")
    for r in e5.MANUAL_REASONS:
        text = doc.entry_text("manual_required", r)
        assert not text.startswith("Eligibility check"), text
        assert text.endswith("; no payer check was made."), text


# ---------------------------------------------------------------------------- U02 content (A)


@pytest.mark.parametrize(("cls", "reason"), [
    ("active", "coverage_active"), ("inactive", "coverage_inactive"),
    ("inconclusive", "payer_unavailable")])
def test_el5_u02_payer_body_has_no_sentinel(cls: str, reason: str) -> None:
    """A. With every field and every sentinel populated, the body carries no sentinel and only
    the closed key set (U11 pattern)."""
    body = e5.m05("eligibility_doc").render_entry(
        e5.event(cls, reason, verification=True), e5.full_answer(cls, reason))
    assert set(body) == e5.ENTRY_KEYS, sorted(body)
    dumped = json.dumps(body, sort_keys=True, default=str)
    for name, value in e5.SENTINELS.items():
        assert value not in dumped, name
    for forbidden in ("coinsurance", "deductible_total", "txn_ref", "member_id", "raw"):
        assert forbidden not in dumped, forbidden


def test_el5_u02_summary_from_that_answer() -> None:
    """A. Payer active/inactive: summary is exactly the SUMMARY_FIELDS of the given answer."""
    body = e5.m05("eligibility_doc").render_entry(
        e5.event("inactive", "coverage_inactive", verification=True),
        e5.full_answer("inactive", "coverage_inactive"))
    assert body["summary"] == e5.SUMMARY_VALUES
    assert body["checked_at"] == "2026-11-20T15:04:05Z"
    assert body["coverage_version"] == 2 and body["event_at"] == "2026-11-20T15:04:06Z"


@pytest.mark.parametrize(("cls", "reason", "verification"), [
    ("inconclusive", "payer_unavailable", True), ("manual_required", "no_coverage", False)])
def test_el5_u02_no_financial_fields(cls: str, reason: str, verification: bool) -> None:
    """A. Inconclusive and manual entries carry no financial fields (R3.5)."""
    answer = e5.full_answer(cls, reason) if verification else None
    body = e5.m05("eligibility_doc").render_entry(e5.event(cls, reason,
                                                           verification=verification), answer)
    assert body["summary"] is None
    dumped = json.dumps(body, default=str)
    for f in ("copay", "deductible_remaining", "payer_name", "plan"):
        assert f not in dumped, f


def test_el5_u02_scrubbed_answer() -> None:
    """A. After the EL-14a scrub only class/reason/observed_at remain: class, reason, time only."""
    scrubbed = {"class": "inactive", "reason": "coverage_inactive",
                "observed_at": "2026-11-20T15:04:05Z"}
    body = e5.m05("eligibility_doc").render_entry(
        e5.event("inactive", "coverage_inactive", verification=True), scrubbed)
    assert body["summary"] is None
    assert body["text"] == "Eligibility check: Not active — Payer reports coverage not active"
    assert body["checked_at"] == "2026-11-20T15:04:05Z"


# ---------------------------------------------------------------------------- U03 one list (A)


def test_el5_u03_one_summary_field_list() -> None:
    """A. The entry reader uses the t121 SUMMARY_FIELDS object (no second list; R3.4)."""
    doc = e5.m05("eligibility_doc")
    assert doc.SUMMARY_FIELDS is e5.m05("eligibility_case_units").SUMMARY_FIELDS


# ---------------------------------------------------------------------------- schedule (A)


def test_el5_u05_schedule_pins() -> None:
    """A. Codex 3671b547: 3 append attempts incl. the initial (retry delays 60 s, 300 s; no
    third delay), 3 reconciliation reads at 60/300/900 s, hourly confirmation capped at 24."""
    doc = e5.m05("eligibility_doc")
    assert doc.APPEND_ATTEMPTS == e5.APPEND_ATTEMPTS
    assert tuple(doc.RETRY_DELAYS_S) == e5.RETRY_DELAYS_S
    assert len(doc.RETRY_DELAYS_S) == doc.APPEND_ATTEMPTS - 1
    assert tuple(doc.READ_DELAYS_S) == e5.READ_DELAYS_S
    assert doc.CONFIRM_READ_INTERVAL_S == e5.CONFIRM_READ_INTERVAL_S
    assert doc.CONFIRM_READS_MAX == e5.CONFIRM_READS_MAX
    assert doc.OPS_PER_PASS == e5.OPS_PER_PASS
    assert doc.PORT_TIMEOUT_S == e5.PORT_TIMEOUT_S


# ---------------------------------------------------------------------------- profile (B)


def test_el5_u06_eligibility_profile_target() -> None:
    """B. The eligibility profile target moves 127 -> 134 (t128..t134). Baseline: 127."""
    assert e5.synthetic("profiles").TARGETS["eligibility"] == e5.ELIGIBILITY_TARGET
