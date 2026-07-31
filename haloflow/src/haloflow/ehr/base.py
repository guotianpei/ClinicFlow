"""
Abstract EHR adapter.

All Tier 2 service logic talks to this interface — not to any EMR directly.
Swapping CGM eMDs for athenahealth (or any future EMR) means writing a new
subclass here; the business logic above never changes.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass
class Patient:
    emr_patient_id: str
    first_name: str
    last_name: str
    dob: date
    phone: str | None          # mobile preferred
    phone_home: str | None
    email: str | None
    payer_id: str | None       # primary insurance payer (clearinghouse ID)
    payer_name: str | None
    member_id: str | None      # insurance member/subscriber ID
    group_number: str | None


@dataclass
class Appointment:
    emr_appt_id: str
    emr_patient_id: str
    scheduled_datetime: datetime
    duration_minutes: int
    appointment_type: str
    status: AppointmentStatus
    confirmation_status: str | None   # EMR-specific field value
    provider_name: str | None
    department: str | None


@dataclass
class CoverSheetData:
    """All data needed to auto-populate an outbound fax cover sheet."""
    patient_name: str
    dob: date
    member_id: str | None
    referring_provider: str | None
    receiving_provider: str | None
    receiving_fax: str
    receiving_org: str
    subject: str
    notes: str | None
    pages_to_follow: int


class EHRAdapter(ABC):
    """
    EMR-agnostic interface used by all Tier 2 modules.

    Implementations: AthenaHealthAdapter, CGMeMDsAdapter (future)
    """

    # ── Patient ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_patient(self, emr_patient_id: str) -> Patient:
        """Fetch a single patient record by EMR ID."""
        ...

    @abstractmethod
    async def search_patients(self, **kwargs: object) -> list[Patient]:
        """Search patients by name, DOB, phone, etc."""
        ...

    # ── Appointments ─────────────────────────────────────────────────────────

    @abstractmethod
    async def get_appointments_by_date(
        self,
        target_date: date,
        *,
        department_id: str | None = None,
    ) -> list[Appointment]:
        """
        Return all scheduled/confirmed appointments for a given calendar date.
        Used by the daily reminder and eligibility jobs.
        """
        ...

    @abstractmethod
    async def get_appointments_in_range(
        self,
        start: datetime,
        end: datetime,
        *,
        status: AppointmentStatus | None = None,
    ) -> list[Appointment]:
        """
        Return appointments within a datetime range, optionally filtered by status.
        Used for no-show detection (query yesterday's no-shows each morning).
        """
        ...

    @abstractmethod
    async def get_appointment(self, emr_appt_id: str) -> Appointment:
        """Fetch a single appointment by EMR ID."""
        ...

    @abstractmethod
    async def write_confirmation_status(
        self,
        emr_appt_id: str,
        confirmed: bool,
    ) -> None:
        """
        Write patient-confirmed status back to the EMR after an SMS reply.
        Maps to the EMR-specific confirmation field (structured or note).
        """
        ...

    # ── Insurance / cover sheet ───────────────────────────────────────────────

    @abstractmethod
    async def get_patient_insurance(self, emr_patient_id: str) -> dict[str, object]:
        """
        Return raw insurance data for a patient (primary and secondary).
        Service layer extracts payer_id, member_id, group_number.
        """
        ...

    @abstractmethod
    async def get_cover_sheet_data(
        self,
        emr_patient_id: str,
        receiving_fax: str,
        receiving_org: str,
        receiving_provider: str | None,
        subject: str,
        notes: str | None,
        pages_to_follow: int,
    ) -> CoverSheetData:
        """
        Assemble cover sheet data from EMR patient/provider records.
        Used by the outbound fax module.
        """
        ...

    # ── Care gaps ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_patients_due_for_preventive_care(
        self,
        measure: str,
        *,
        due_before: date,
    ) -> list[Patient]:
        """
        Pull patients due for a specific preventive measure (e.g., AWV, A1c)
        based on due-date fields in the EMR. Rules-based interval only — no
        diagnosis/clinical logic.
        """
        ...
