"""Eligible-reminder selection in clinic time (R-22, T47).

Rules preserved from legacy ``ReminderService.send_upcoming_reminders``:
target date = today + ``reminder_days_before``; CANCELLED and COMPLETED are
skipped; an appointment with an existing reminder record is skipped; a patient
without a phone is skipped. Change (R-22): "today" and each appointment's date
are taken in ``CLINIC_TZ``; the legacy code used UTC.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from haloflow.demo.synthetic_ehr import ApptStatus, SyntheticAppointment, SyntheticPatient


class SkipReason(StrEnum):
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    ALREADY_HAS_REMINDER = "already_has_reminder"
    NO_PHONE = "no_phone"
    UNKNOWN_PATIENT = "unknown_patient"
    OTHER_DATE = "other_date"


@dataclass(frozen=True)
class Eligible:
    appointment: SyntheticAppointment
    patient: SyntheticPatient


@dataclass(frozen=True)
class Skipped:
    emr_appt_id: str
    reason: SkipReason


@dataclass(frozen=True)
class Selection:
    target_date: date
    eligible: list[Eligible] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def clinic_target_date(now_utc: datetime, tz: str, *, days_before: int) -> date:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now_utc.astimezone(ZoneInfo(tz)).date() + timedelta(days=days_before)


def format_local(start_utc: datetime, tz: str) -> str:
    local = start_utc.astimezone(ZoneInfo(tz))
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%A, %b %d} at {hour}:{local:%M %p %Z}"


def select_eligible(
    appointments: Iterable[SyntheticAppointment],
    patients: Mapping[str, SyntheticPatient],
    target_date: date,
    tz: str,
    *,
    existing_appt_ids: set[str],
) -> Selection:
    zone = ZoneInfo(tz)
    result = Selection(target_date=target_date)
    for appt in appointments:
        if appt.start_utc.astimezone(zone).date() != target_date:
            continue  # not this clinic day; adapters may over-return around midnight
        skip: SkipReason | None = None
        patient = patients.get(appt.emr_patient_id)
        if appt.status is ApptStatus.CANCELLED:
            skip = SkipReason.CANCELLED
        elif appt.status is ApptStatus.COMPLETED:
            skip = SkipReason.COMPLETED
        elif appt.emr_appt_id in existing_appt_ids:
            skip = SkipReason.ALREADY_HAS_REMINDER
        elif patient is None:
            skip = SkipReason.UNKNOWN_PATIENT
        elif patient.phone_alias is None:
            skip = SkipReason.NO_PHONE
        if skip is not None:
            result.skipped.append(Skipped(appt.emr_appt_id, skip))
        else:
            assert patient is not None
            result.eligible.append(Eligible(appt, patient))
    return result
