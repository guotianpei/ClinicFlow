"""Eligibility checks (E1): pure due-time, range, zone, completeness and routing rules.

Synthetic only. Governing: eligibility requirements v1r1; architecture v1..v1r3 (R5 due times);
owner decisions OD-EL1 (three checks; lead 3..5, default 3; day-of 07:00 or 2 h before an
earlier visit), OD-EL2 (manual routing), OD-EL9 (missing-insurance case at start − N days,
same clock time, N 0..5, default 1). The SQL in `eligibility_units` computes the same instants;
`tests/m05/test_el1_postgres.py` E1-P15 pins the parity.

Local-time rule (R5): the visit's LOCAL date-time minus N calendar days, time of day kept. A
local time that does not exist (spring-forward gap) moves to the first valid instant after the
gap; an ambiguous local time (fall-back) takes the earlier instant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "DAYOF_LOCAL_TIME",
    "DAYOF_MIN_LEAD",
    "MANUAL_REASONS",
    "check_lead_days",
    "check_not_received_days",
    "choose_zone",
    "completeness",
    "dayof_due",
    "not_received_due",
    "previsit_due",
    "route",
]

DAYOF_LOCAL_TIME: Final = time(7, 0)
DAYOF_MIN_LEAD: Final = timedelta(minutes=120)
MANUAL_REASONS: Final = ("nonpriority", "no_coverage", "incomplete")

_SELF_FIELDS: Final = ("payer_id", "member_id", "patient_name", "patient_dob")
_DEPENDENT_FIELDS: Final = ("subscriber_name", "subscriber_dob", "subscriber_member_id")
_MAX_ZONE_LEN: Final = 64


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return value.astimezone(UTC)


def _resolve_local(day: date, clock: time, zone: ZoneInfo) -> datetime:
    """The instant of local `day` `clock` in `zone`: earlier instant if ambiguous, first valid
    instant after the gap if non-existent."""
    naive = datetime.combine(day, clock)
    earlier = naive.replace(tzinfo=zone, fold=0)
    # Round-trip check: a non-existent local time does not survive UTC -> local.
    if earlier.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
        return earlier.astimezone(UTC)
    later = naive.replace(tzinfo=zone, fold=1)
    if later.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
        return later.astimezone(UTC)
    # In a gap: the first valid instant is the transition. The two fold readings straddle it
    # (fold=1 uses the post-transition offset and lands before it; fold=0 the pre-transition
    # offset and lands after it); bisect to the first second carrying the new offset.
    lo, hi = sorted((earlier.astimezone(UTC), later.astimezone(UTC)))
    new_offset = hi.astimezone(zone).utcoffset()
    if lo.astimezone(zone).utcoffset() == new_offset:
        raise ValueError("no valid local instant")  # pragma: no cover - not a gap
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        mid = mid.replace(microsecond=0)
        if mid.astimezone(zone).utcoffset() == new_offset:
            hi = mid
        else:
            lo = mid
    return hi


def _days_before(start: datetime, zone_name: str, days: int) -> datetime:
    zone = ZoneInfo(zone_name)
    local = _aware_utc(start).astimezone(zone)
    return _resolve_local(local.date() - timedelta(days=days), local.time().replace(tzinfo=None),
                          zone)


def previsit_due(start: datetime, zone: str, lead_days: int) -> datetime:
    """OD-EL1 pre-visit refresh instant (UTC)."""
    return _days_before(start, zone, check_lead_days(lead_days))


def dayof_due(start: datetime, zone: str) -> datetime:
    """OD-EL1 visit-day instant (UTC): min(07:00 local, start − 2 h), floored at 00:00 local of
    the service date."""
    tz = ZoneInfo(zone)
    start_utc = _aware_utc(start)
    service_day = start_utc.astimezone(tz).date()
    seven = _resolve_local(service_day, DAYOF_LOCAL_TIME, tz)
    midnight = _resolve_local(service_day, time(0, 0), tz)
    return max(min(seven, start_utc - DAYOF_MIN_LEAD), midnight)


def not_received_due(start: datetime, zone: str, days: int) -> datetime:
    """OD-EL9 missing-insurance case instant (UTC); 0 days = the visit-day instant."""
    n = check_not_received_days(days)
    if n == 0:
        return dayof_due(start, zone)
    return _days_before(start, zone, n)


def _check_int(value: Any, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{what} must be an int in {low}..{high}")
    return value


def check_lead_days(value: Any) -> int:
    return _check_int(value, 3, 5, "lead_days")


def check_not_received_days(value: Any) -> int:
    return _check_int(value, 0, 5, "not_received_days")


def _usable_zone(name: Any) -> str | None:
    if not isinstance(name, str) or not 1 <= len(name) <= _MAX_ZONE_LEN:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def choose_zone(department_tz: Any, clinic_tz: Any) -> str | None:
    """The department zone if usable, else the clinic zone if usable, else None (never a
    guessed zone)."""
    return _usable_zone(department_tz) or _usable_zone(clinic_tz)


def _present(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def completeness(coverage: dict[str, Any]) -> str:
    """'complete' | 'incomplete' for a `present` coverage block. Never guesses."""
    relationship = coverage.get("relationship")
    if relationship not in ("self", "dependent"):
        return "incomplete"
    needed = _SELF_FIELDS + (_DEPENDENT_FIELDS if relationship == "dependent" else ())
    return "complete" if all(_present(coverage.get(f)) for f in needed) else "incomplete"


def route(coverage: dict[str, Any], priority_payers: frozenset[str]) -> tuple[str, str | None]:
    """('check', None) for complete priority coverage; ('manual', reason) otherwise. A
    `pending` block is not routable (it waits for coverage or the OD-EL9 deadline)."""
    state = coverage.get("state")
    if state == "none":
        return ("manual", "no_coverage")
    if state != "present":
        raise ValueError("only present or none coverage is routable")
    if completeness(coverage) != "complete":
        return ("manual", "incomplete")
    if coverage["payer_id"] not in priority_payers:
        return ("manual", "nonpriority")
    return ("check", None)
