"""U-layer: synthetic appointment source (R-23), eligible selection and clinic time
(R-22, T47).

Eligibility rules preserved from the legacy ``send_upcoming_reminders``:
target date = today + ``reminder_days_before``; CANCELLED and COMPLETED are
skipped; an appointment that already has a reminder record is skipped; a patient
without any phone is skipped. The one intended change is R-22: "today" and the
appointment's date are taken in ``CLINIC_TZ``, not UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from haloflow_demo.selection import (
    SkipReason,
    clinic_target_date,
    format_local,
    select_eligible,
)
from haloflow_demo.synthetic_ehr import (
    ApptStatus,
    SyntheticAppointment,
    SyntheticEhr,
    SyntheticPatient,
    default_fixtures,
)

NY = ZoneInfo("America/New_York")


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# ── clinic target date ────────────────────────────────────────────────────────


def test_target_date_uses_clinic_zone_not_utc() -> None:
    # 2026-10-30 23:30 EDT == 2026-10-31 03:30Z. Legacy UTC logic gives 11-02.
    now = _utc(2026, 10, 31, 3, 30)
    assert clinic_target_date(now, "America/New_York", days_before=2) == date(2026, 11, 1)
    assert (now + timedelta(days=2)).date() == date(2026, 11, 2)  # the legacy answer


def test_target_date_rejects_naive_now() -> None:
    import pytest

    with pytest.raises(ValueError):
        clinic_target_date(datetime(2026, 10, 30, 12), "America/New_York", days_before=2)


# ── T47: DST end 2026-11-01 ───────────────────────────────────────────────────


def _dst_patients() -> dict[str, SyntheticPatient]:
    return {
        "p1": SyntheticPatient("p1", "Synthetic Patient One", phone_alias="A"),
        "p2": SyntheticPatient("p2", "Synthetic Patient Two", phone_alias="B"),
        "p3": SyntheticPatient("p3", "Synthetic Patient Three", phone_alias="C"),
    }


def _dst_appointments() -> list[SyntheticAppointment]:
    first_0130 = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=0)   # EDT, 05:30Z
    second_0130 = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=1)  # EST, 06:30Z
    return [
        SyntheticAppointment("a1", "p1", first_0130.astimezone(UTC), ApptStatus.SCHEDULED),
        SyntheticAppointment("a2", "p2", second_0130.astimezone(UTC), ApptStatus.SCHEDULED),
        SyntheticAppointment(
            "a3", "p3", datetime(2026, 11, 2, 9, 0, tzinfo=NY).astimezone(UTC),
            ApptStatus.SCHEDULED,
        ),
    ]


def test_t47_repeated_hour_both_selected_and_next_day_not() -> None:
    result = select_eligible(
        _dst_appointments(), _dst_patients(), date(2026, 11, 1), "America/New_York",
        existing_appt_ids=set(),
    )
    assert [e.appointment.emr_appt_id for e in result.eligible] == ["a1", "a2"]
    assert result.skipped == []


def test_t47_display_in_clinic_zone_distinguishes_repeated_hour() -> None:
    a1, a2, a3 = _dst_appointments()
    assert format_local(a1.start_utc, "America/New_York") == "Sunday, Nov 01 at 1:30 AM EDT"
    assert format_local(a2.start_utc, "America/New_York") == "Sunday, Nov 01 at 1:30 AM EST"
    assert format_local(a3.start_utc, "America/New_York") == "Monday, Nov 02 at 9:00 AM EST"


def test_t47_utc_date_differs_but_clinic_date_wins() -> None:
    # 2026-11-01 21:00 EST == 2026-11-02 02:00Z: belongs to 11-01 in the clinic.
    late = SyntheticAppointment(
        "late", "p1", datetime(2026, 11, 1, 21, 0, tzinfo=NY).astimezone(UTC),
        ApptStatus.SCHEDULED,
    )
    on_1101 = select_eligible([late], _dst_patients(), date(2026, 11, 1), "America/New_York",
                              existing_appt_ids=set())
    on_1102 = select_eligible([late], _dst_patients(), date(2026, 11, 2), "America/New_York",
                              existing_appt_ids=set())
    assert [e.appointment.emr_appt_id for e in on_1101.eligible] == ["late"]
    assert on_1102.eligible == []


# ── legacy eligibility rules ──────────────────────────────────────────────────


def test_eligibility_skips_cancelled_completed_existing_and_no_phone() -> None:
    day = date(2026, 10, 28)
    start = datetime(2026, 10, 28, 10, 0, tzinfo=NY).astimezone(UTC)
    patients = {
        "p1": SyntheticPatient("p1", "Synthetic One", phone_alias="A"),
        "p2": SyntheticPatient("p2", "Synthetic Two", phone_alias=None),
    }
    appts = [
        SyntheticAppointment("ok", "p1", start, ApptStatus.SCHEDULED),
        SyntheticAppointment("conf", "p1", start, ApptStatus.CONFIRMED),
        SyntheticAppointment("cx", "p1", start, ApptStatus.CANCELLED),
        SyntheticAppointment("done", "p1", start, ApptStatus.COMPLETED),
        SyntheticAppointment("dup", "p1", start, ApptStatus.SCHEDULED),
        SyntheticAppointment("nophone", "p2", start, ApptStatus.SCHEDULED),
        SyntheticAppointment("ghost", "p404", start, ApptStatus.SCHEDULED),
    ]
    result = select_eligible(appts, patients, day, "America/New_York", existing_appt_ids={"dup"})
    assert [e.appointment.emr_appt_id for e in result.eligible] == ["ok", "conf"]
    assert {(s.emr_appt_id, s.reason) for s in result.skipped} == {
        ("cx", SkipReason.CANCELLED),
        ("done", SkipReason.COMPLETED),
        ("dup", SkipReason.ALREADY_HAS_REMINDER),
        ("nophone", SkipReason.NO_PHONE),
        ("ghost", SkipReason.UNKNOWN_PATIENT),
    }


# ── R-23 synthetic fixtures ───────────────────────────────────────────────────


def test_r23_fixtures_are_deterministic() -> None:
    assert default_fixtures(date(2026, 10, 28)) == default_fixtures(date(2026, 10, 28))


def test_r23_fixtures_include_required_cases() -> None:
    fx = default_fixtures(date(2026, 10, 28))
    by_alias: dict[str | None, list[str]] = {}
    for p in fx.patients.values():
        by_alias.setdefault(p.phone_alias, []).append(p.emr_patient_id)
    assert any(len(ids) >= 2 for alias, ids in by_alias.items() if alias), "shared-number case"
    per_patient: dict[str, int] = {}
    for a in fx.appointments:
        if a.status is ApptStatus.SCHEDULED:
            per_patient[a.emr_patient_id] = per_patient.get(a.emr_patient_id, 0) + 1
    assert any(n >= 2 for n in per_patient.values()), "two-open-reminders case"


def test_r23_fixture_names_are_obviously_synthetic() -> None:
    fx = default_fixtures(date(2026, 10, 28))
    assert all(p.display_name.startswith("Synthetic ") for p in fx.patients.values())


def test_r23_synthetic_patients_hold_no_phone_numbers() -> None:
    # R-9: the only phone setting is DEMO_SMS_TEST_NUMBER. Fixtures carry an
    # alias label, never a dialable number.
    fx = default_fixtures(date(2026, 10, 28))
    for p in fx.patients.values():
        assert p.phone_alias is None or not any(ch.isdigit() for ch in p.phone_alias)


async def test_synthetic_ehr_lists_by_clinic_date() -> None:
    ehr = SyntheticEhr(default_fixtures(date(2026, 10, 28)))
    appts = await ehr.get_appointments_by_date(date(2026, 10, 28), "America/New_York")
    assert appts, "fixtures must contain appointments on the anchor date"
    for a in appts:
        assert a.start_utc.astimezone(NY).date() == date(2026, 10, 28)
