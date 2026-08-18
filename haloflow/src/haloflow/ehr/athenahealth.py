"""
athenahealth EHR adapter.

Uses athenahealth's proprietary REST API v1 (not FHIR) — it has broader
coverage for the Tier 2 transactional operations we need (appointment
status writeback, insurance details, patient lookup by phone).

Auth: OAuth 2.0 client credentials flow. Tokens expire in 3600 s;
      this adapter auto-refreshes before each call.

Base URL (sandbox): https://api.preview.platform.athenahealth.com
Base URL (prod):    https://api.platform.athenahealth.com

All endpoints are prefixed:  /v1/{practice_id}/
"""
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from haloflow.config import get_settings
from haloflow.ehr.base import (
    Appointment,
    AppointmentStatus,
    CoverSheetData,
    EHRAdapter,
    Patient,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# ── athenahealth → internal status mapping ──────────────────────────────────
_ATHENA_STATUS_MAP: dict[str, AppointmentStatus] = {
    "f": AppointmentStatus.SCHEDULED,       # future
    "x": AppointmentStatus.CANCELLED,
    "2": AppointmentStatus.CHECKED_IN,
    "3": AppointmentStatus.COMPLETED,
    "4": AppointmentStatus.NO_SHOW,
    "o": AppointmentStatus.SCHEDULED,       # open slot (unused)
}


class AthenaHealthAdapter(EHRAdapter):
    """
    athenahealth REST API v1 adapter.
    Instantiate once per process; token is cached in memory.
    """

    def __init__(self) -> None:
        self._base = f"{settings.athena_base_url}/v1/{settings.athena_practice_id}"
        self._token: str | None = None
        self._token_expiry: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._client = httpx.AsyncClient(timeout=30.0)

    # ── OAuth ──────────────────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        now = datetime.now(tz=timezone.utc)
        if self._token and now < self._token_expiry - timedelta(seconds=60):
            return self._token

        logger.debug("Refreshing athenahealth OAuth token")
        resp = await self._client.post(
            f"{settings.athena_base_url}/oauth2/v1/token",
            data={
                "grant_type": "client_credentials",
                "scope": "athena/service/Athenanet.MDP.*",
            },
            auth=(settings.athena_client_id, settings.athena_client_secret),
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = now + timedelta(seconds=payload["expires_in"])
        return self._token  # type: ignore[return-value]

    async def _get(self, path: str, **params: object) -> dict:
        token = await self._ensure_token()
        resp = await self._client.get(
            f"{self._base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params={k: v for k, v in params.items() if v is not None},
        )
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, data: dict) -> dict:
        token = await self._ensure_token()
        resp = await self._client.put(
            f"{self._base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            data=data,          # athena v1 uses form-encoded bodies
        )
        resp.raise_for_status()
        return resp.json()

    # ── Patient ────────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
    )
    async def get_patient(self, emr_patient_id: str) -> Patient:
        data = await self._get(f"/patients/{emr_patient_id}")
        return self._parse_patient(data[0] if isinstance(data, list) else data)

    async def search_patients(self, **kwargs: object) -> list[Patient]:
        data = await self._get("/patients", **kwargs)
        patients_raw = data.get("patients", [])
        return [self._parse_patient(p) for p in patients_raw]

    def _parse_patient(self, raw: dict) -> Patient:
        return Patient(
            emr_patient_id=str(raw["patientid"]),
            first_name=raw.get("firstname", ""),
            last_name=raw.get("lastname", ""),
            dob=datetime.strptime(raw["dob"], "%m/%d/%Y").date(),
            phone=raw.get("mobilephone") or raw.get("homephone"),
            phone_home=raw.get("homephone"),
            email=raw.get("email"),
            # Insurance filled separately via get_patient_insurance
            payer_id=None,
            payer_name=None,
            member_id=None,
            group_number=None,
        )

    # ── Appointments ───────────────────────────────────────────────────────

    async def get_appointments_by_date(
        self,
        target_date: date,
        *,
        department_id: str | None = None,
    ) -> list[Appointment]:
        date_str = target_date.strftime("%m/%d/%Y")
        raw = await self._get(
            "/appointments/booked",
            startdate=date_str,
            enddate=date_str,
            departmentid=department_id,
            # showpatientdetail=True would add PII inline — keep separate
            # to respect minimum-necessary-use principle in data logging
        )
        return [self._parse_appointment(a) for a in raw.get("appointments", [])]

    async def get_appointments_in_range(
        self,
        start: datetime,
        end: datetime,
        *,
        status: AppointmentStatus | None = None,
    ) -> list[Appointment]:
        raw = await self._get(
            "/appointments/booked",
            startdate=start.strftime("%m/%d/%Y"),
            enddate=end.strftime("%m/%d/%Y"),
            appointmentstatus=_reverse_status(status) if status else None,
        )
        return [self._parse_appointment(a) for a in raw.get("appointments", [])]

    async def get_appointment(self, emr_appt_id: str) -> Appointment:
        raw = await self._get(f"/appointments/{emr_appt_id}")
        appt_raw = raw[0] if isinstance(raw, list) else raw
        return self._parse_appointment(appt_raw)

    def _parse_appointment(self, raw: dict) -> Appointment:
        dt = datetime.strptime(
            f"{raw['date']} {raw.get('starttime', '00:00')}",
            "%m/%d/%Y %H:%M",
        )
        raw_status = raw.get("appointmentstatus", "f").lower()
        return Appointment(
            emr_appt_id=str(raw["appointmentid"]),
            emr_patient_id=str(raw.get("patientid", "")),
            scheduled_datetime=dt,
            duration_minutes=int(raw.get("duration", 15)),
            appointment_type=raw.get("appointmenttype", ""),
            status=_ATHENA_STATUS_MAP.get(raw_status, AppointmentStatus.SCHEDULED),
            confirmation_status=raw.get("confirmationcode"),
            provider_name=raw.get("providerloginname"),
            department=raw.get("departmentid"),
        )

    async def write_confirmation_status(
        self,
        emr_appt_id: str,
        confirmed: bool,
    ) -> None:
        """
        Updates the appointment's confirmation code.
        athenahealth uses 'x' (confirmed) or '1' (left message) etc.
        We use '2' = Patient confirmed.
        """
        confirmation_code = "2" if confirmed else "1"
        await self._put(
            f"/appointments/{emr_appt_id}",
            {"confirmationcode": confirmation_code},
        )
        logger.info(
            "Wrote confirmation_code=%s for appointment %s",
            confirmation_code,
            emr_appt_id,
        )

    # ── Insurance ─────────────────────────────────────────────────────────

    async def get_patient_insurance(self, emr_patient_id: str) -> dict:
        raw = await self._get(f"/patients/{emr_patient_id}/insurances")
        insurances = raw.get("insurances", [])
        # Return primary (sequence 1) if present
        primary = next(
            (i for i in insurances if i.get("sequencenumber") == 1), None
        )
        return primary or (insurances[0] if insurances else {})

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
        patient = await self.get_patient(emr_patient_id)
        insurance = await self.get_patient_insurance(emr_patient_id)
        return CoverSheetData(
            patient_name=f"{patient.first_name} {patient.last_name}",
            dob=patient.dob,
            member_id=insurance.get("memberId") or insurance.get("memberid"),
            referring_provider=None,  # pull from appointment if needed
            receiving_provider=receiving_provider,
            receiving_fax=receiving_fax,
            receiving_org=receiving_org,
            subject=subject,
            notes=notes,
            pages_to_follow=pages_to_follow,
        )

    # ── Care gaps ──────────────────────────────────────────────────────────

    async def get_patients_due_for_preventive_care(
        self,
        measure: str,
        *,
        due_before: date,
    ) -> list[Patient]:
        """
        athenahealth exposes care gaps via:
          GET /patients?customfieldname=<measure>&customfieldvalue=<due_date_range>

        For Tier 2 we rely on the care management / quality measure fields
        that the clinic's staff have already configured in athenahealth.
        The measure string maps to an athenahealth custom field or care program ID.

        This is a simplified implementation — the exact field names are
        confirmed during Tier 1 integration provisioning.
        """
        # Use athenahealth's quality reporting endpoint if available in the practice
        raw = await self._get(
            "/patients",
            customfieldname=f"PREVENTIVE_{measure.upper()}_DUE_DATE",
            # Return patients whose due date is before target
            customfieldvalue=f"<={due_before.strftime('%m/%d/%Y')}",
            limit=500,
        )
        return [self._parse_patient(p) for p in raw.get("patients", [])]


def _reverse_status(status: AppointmentStatus) -> str:
    """Map internal status back to athenahealth status code string."""
    reverse = {v: k for k, v in _ATHENA_STATUS_MAP.items()}
    return reverse.get(status, "f")
