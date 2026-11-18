"""Eligibility E1 unit rows (no database): due instants, ranges, zone choice, coverage
completeness and routing.

Governing: eligibility requirements v1r1, architecture v1..v1r3, stage-3 test cases v1 + v1r1
(Codex ALIGN bca3bf33), owner decisions OD-EL1 (timing), OD-EL2 (manual routing), OD-EL9
(missing-insurance deadline). Red-first: `haloflow.m05.eligibility_rules` does not exist yet.

Pinned interface (E1 red packet v1, section 2):
- previsit_due(start_utc, zone, lead_days) -> UTC instant: the visit's LOCAL date-time minus
  lead_days calendar days, time of day preserved. Non-existent local time -> the first valid
  instant after the gap; ambiguous -> the earlier instant.
- dayof_due(start_utc, zone) -> min(07:00 local on the service date, start - 120 min), never
  before 00:00 local on the service date.
- not_received_due(start_utc, zone, days) -> previsit rule with `days`; days == 0 -> dayof_due.
- check_lead_days(n) / check_not_received_days(n) -> n, or ValueError outside 3..5 / 0..5.
- choose_zone(department_tz, clinic_tz) -> department_tz if a valid IANA zone, else clinic_tz
  if valid, else None.
- completeness(coverage) -> "complete" | "incomplete" for a `present` coverage block.
- route(coverage, priority_payers) -> ("check", None) | ("manual", reason), reason in
  MANUAL_REASONS = ("nonpriority", "no_coverage", "incomplete").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

NY = "America/New_York"
LA = "America/Los_Angeles"
EDT = timedelta(hours=-4)
EST = timedelta(hours=-5)


def rules() -> Any:
    import haloflow.m05.eligibility_rules as module

    return module


def local(y: int, mo: int, d: int, h: int, mi: int, zone: str = NY, *, fold: int = 0
          ) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(zone), fold=fold).astimezone(UTC)


def utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# --- E1-U01 previsit: time of day preserved ---------------------------------------------

def test_e1_u01_previsit_preserves_clock_time() -> None:
    # Fri 2026-11-20 15:00 EST, lead 3 -> Tue 2026-11-17 15:00 EST.
    due = rules().previsit_due(local(2026, 11, 20, 15, 0), NY, 3)
    assert due == utc(2026, 11, 17, 20, 0)


@pytest.mark.parametrize("lead", [3, 4, 5])
def test_e1_u01_previsit_each_lead(lead: int) -> None:
    start = local(2026, 12, 10, 9, 30)
    due = rules().previsit_due(start, NY, lead)
    assert due.astimezone(ZoneInfo(NY)).time() == start.astimezone(ZoneInfo(NY)).time()
    assert (start.astimezone(ZoneInfo(NY)).date()
            - due.astimezone(ZoneInfo(NY)).date()).days == lead


def test_e1_u01_previsit_requires_aware_utc() -> None:
    with pytest.raises(ValueError):
        rules().previsit_due(datetime(2026, 11, 20, 15, 0), NY, 3)  # naive


# --- E1-U02 DST, pinned fixtures (Codex fb58adda) -------------------------------------------

def test_e1_u02_fall_back_ambiguous_takes_earlier() -> None:
    # Visit 2026-11-04 01:30 local (EST); minus 3 days -> 2026-11-01 01:30, which occurs twice;
    # the earlier instant is 01:30 EDT = 05:30Z.
    due = rules().previsit_due(local(2026, 11, 4, 1, 30), NY, 3)
    assert due == utc(2026, 11, 1, 5, 30)
    assert due.astimezone(ZoneInfo(NY)).utcoffset() == EDT


def test_e1_u02_spring_forward_gap_moves_to_first_valid_instant() -> None:
    # Visit 2026-03-11 02:30 local (EDT); minus 3 days -> 2026-03-08 02:30, which does not exist;
    # the first valid instant after the gap is 03:00 EDT = 07:00Z (NOT zoneinfo's fold=0 03:30).
    due = rules().previsit_due(local(2026, 3, 11, 2, 30), NY, 3)
    assert due == utc(2026, 3, 8, 7, 0)
    assert due.astimezone(ZoneInfo(NY)).utcoffset() == EDT


def test_e1_u02_same_fixtures_for_not_received_deadline() -> None:
    r = rules()
    assert r.not_received_due(local(2026, 11, 4, 1, 30), NY, 3) == utc(2026, 11, 1, 5, 30)
    assert r.not_received_due(local(2026, 3, 11, 2, 30), NY, 3) == utc(2026, 3, 8, 7, 0)


# --- E1-U03 day-of ------------------------------------------------------------------------

@pytest.mark.parametrize(("hour", "minute", "expect_h", "expect_m"), [
    (10, 0, 7, 0),    # 07:00 is earlier than start - 2 h
    (9, 0, 7, 0),     # equal: 07:00
    (8, 0, 6, 0),     # start - 2 h is earlier
    (1, 30, 0, 0),    # start - 2 h is the day before -> floor at 00:00 local
    (0, 0, 0, 0),     # midnight visit -> floor
])
def test_e1_u03_dayof(hour: int, minute: int, expect_h: int, expect_m: int) -> None:
    due = rules().dayof_due(local(2026, 12, 2, hour, minute), NY)
    assert due == local(2026, 12, 2, expect_h, expect_m)


def test_e1_u03_dayof_floor_on_fall_back_day() -> None:
    # 2026-11-01: midnight is unambiguous (EDT); a 01:30 visit (earlier 01:30) floors at 00:00.
    due = rules().dayof_due(local(2026, 11, 1, 1, 30), NY)
    assert due == utc(2026, 11, 1, 4, 0)


# --- E1-U04 lead-day range --------------------------------------------------------------

@pytest.mark.parametrize("n", [3, 4, 5])
def test_e1_u04_lead_days_accepted(n: int) -> None:
    assert rules().check_lead_days(n) == n


@pytest.mark.parametrize("n", [2, 6, 0, -1])
def test_e1_u04_lead_days_refused(n: int) -> None:
    with pytest.raises(ValueError):
        rules().check_lead_days(n)


@pytest.mark.parametrize("bad", [3.0, "3", True, None])
def test_e1_u04_lead_days_type(bad: Any) -> None:
    with pytest.raises(ValueError):
        rules().check_lead_days(bad)


# --- E1-U05 missing-insurance deadline (OD-EL9) -------------------------------------------

def test_e1_u05_not_received_default_one_day_same_clock() -> None:
    # Fri 2026-11-20 15:00 -> Thu 2026-11-19 15:00 (OD-EL9 "same clock time, day before").
    assert rules().not_received_due(local(2026, 11, 20, 15, 0), NY, 1) == local(
        2026, 11, 19, 15, 0)


def test_e1_u05_not_received_zero_is_dayof() -> None:
    start = local(2026, 11, 20, 8, 0)
    r = rules()
    assert r.not_received_due(start, NY, 0) == r.dayof_due(start, NY)


@pytest.mark.parametrize("n", [0, 1, 5])
def test_e1_u05_range_accepted(n: int) -> None:
    assert rules().check_not_received_days(n) == n


@pytest.mark.parametrize("n", [6, -1])
def test_e1_u05_range_refused(n: int) -> None:
    with pytest.raises(ValueError):
        rules().check_not_received_days(n)


# --- E1-U06 zone choice -------------------------------------------------------------------

def test_e1_u06_department_zone_wins() -> None:
    assert rules().choose_zone(LA, NY) == LA


@pytest.mark.parametrize("dept", [None, "", "Not/AZone", "x" * 65])
def test_e1_u06_invalid_department_falls_back_to_clinic(dept: Any) -> None:
    assert rules().choose_zone(dept, NY) == NY


def test_e1_u06_no_valid_zone_is_none() -> None:
    assert rules().choose_zone(None, None) is None
    assert rules().choose_zone("Bad/Zone", "Also/Bad") is None


# --- E1-U07 completeness ------------------------------------------------------------------

SELF: dict[str, Any] = {
    "state": "present", "payer_id": "SYN-ANTHEM", "member_id": "M000001",
    "relationship": "self", "patient_name": "Synthetic Patient", "patient_dob": "1970-01-01",
}
DEPENDENT: dict[str, Any] = {
    **SELF, "relationship": "dependent", "subscriber_name": "Synthetic Subscriber",
    "subscriber_dob": "1968-02-02", "subscriber_member_id": "M000002",
}


def test_e1_u07_complete_self_and_dependent() -> None:
    assert rules().completeness(SELF) == "complete"
    assert rules().completeness(DEPENDENT) == "complete"


@pytest.mark.parametrize("field", ["payer_id", "member_id", "patient_name", "patient_dob"])
def test_e1_u07_self_missing_field_is_incomplete(field: str) -> None:
    cov = {k: v for k, v in SELF.items() if k != field}
    assert rules().completeness(cov) == "incomplete"
    assert rules().completeness({**SELF, field: None}) == "incomplete"
    assert rules().completeness({**SELF, field: ""}) == "incomplete"


@pytest.mark.parametrize("field", ["subscriber_name", "subscriber_dob", "subscriber_member_id"])
def test_e1_u07_dependent_missing_subscriber_field_is_incomplete(field: str) -> None:
    cov = {k: v for k, v in DEPENDENT.items() if k != field}
    assert rules().completeness(cov) == "incomplete"


def test_e1_u07_unknown_relationship_is_incomplete_not_guessed() -> None:
    assert rules().completeness({**SELF, "relationship": "spouse"}) == "incomplete"
    assert rules().completeness({**SELF, "relationship": None}) == "incomplete"


# --- E1-U08 routing -----------------------------------------------------------------------

PRIORITY = frozenset({"SYN-ANTHEM", "SYN-UHC", "SYN-SENTARA"})


def test_e1_u08_priority_complete_is_checked() -> None:
    assert rules().route(SELF, PRIORITY) == ("check", None)


def test_e1_u08_nonpriority_is_manual() -> None:
    assert rules().route({**SELF, "payer_id": "SYN-OTHER"}, PRIORITY) == ("manual", "nonpriority")


def test_e1_u08_none_is_manual_no_coverage() -> None:
    assert rules().route({"state": "none"}, PRIORITY) == ("manual", "no_coverage")


def test_e1_u08_incomplete_is_manual_incomplete_even_if_priority() -> None:
    cov = {k: v for k, v in SELF.items() if k != "member_id"}
    assert rules().route(cov, PRIORITY) == ("manual", "incomplete")


def test_e1_u08_pending_is_not_routed() -> None:
    with pytest.raises(ValueError):
        rules().route({"state": "pending"}, PRIORITY)


def test_e1_u08_reason_set_is_closed() -> None:
    assert tuple(rules().MANUAL_REASONS) == ("nonpriority", "no_coverage", "incomplete")
