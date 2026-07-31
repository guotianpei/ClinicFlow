"""
Eligibility service — pre-visit insurance verification.

Job (daily): check_upcoming_eligibility()
  - Pulls appointments N days out (default 3–5 days, configurable)
  - For each appointment:
      a) Gets patient insurance from EMR
      b) If payer is in priority list → submits EDI 270 to Stedi
      c) If not priority → sets MANUAL_REQUIRED flag, logs for staff
  - On INACTIVE result → queues office notification (in DB, surfaced by router)

Priority payers (Tier 2):
  - Anthem/HealthKeepers VA  (ANTHM)
  - UnitedHealthcare          (UHC)
  - Sentara Health Plans      (SNTARA)

Non-priority payers → MANUAL_REQUIRED. Volume tracked per payer to inform
which payer gets added to the priority list next (Tier 2 roadmap note).

NPI: pulled from tenant config (TODO: add to Settings once confirmed with clinic).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.config import get_settings
from haloflow.ehr.base import AppointmentStatus, EHRAdapter
from haloflow.integrations.stedi import EligibilityStatus, StediEligibilityClient
from haloflow.modules.eligibility.models import EligibilityCheck, EligibilityCheckStatus

logger = logging.getLogger(__name__)
settings = get_settings()

# Placeholder NPI — confirmed with clinic during Tier 1 provisioning
PRACTICE_NPI = "0000000000"


class EligibilityService:
    def __init__(
        self,
        db: AsyncSession,
        ehr: EHRAdapter,
        stedi: StediEligibilityClient,
    ) -> None:
        self._db = db
        self._ehr = ehr
        self._stedi = stedi

    async def check_upcoming_eligibility(self) -> dict[str, int]:
        """
        Run eligibility checks for appointments N days out.
        Returns counts by outcome for monitoring.
        """
        target_date = (
            datetime.now(tz=timezone.utc)
            + timedelta(days=settings.eligibility_days_before)
        ).date()

        logger.info("Running eligibility job for appts on %s", target_date)
        appointments = await self._ehr.get_appointments_by_date(target_date)

        results: dict[str, int] = {
            "active": 0,
            "inactive": 0,
            "manual": 0,
            "error": 0,
            "skipped": 0,
        }

        for appt in appointments:
            if appt.status in (AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED):
                results["skipped"] += 1
                continue

            # Idempotency: skip if already checked for this appointment
            existing = await self._db.scalar(
                select(EligibilityCheck).where(
                    EligibilityCheck.emr_appt_id == appt.emr_appt_id,
                    EligibilityCheck.tenant_id == settings.tenant_id,
                    EligibilityCheck.status.not_in([EligibilityCheckStatus.ERROR]),
                )
            )
            if existing:
                results["skipped"] += 1
                continue

            try:
                outcome = await self._check_single(appt)
                results[outcome] = results.get(outcome, 0) + 1
            except Exception:
                logger.exception(
                    "Eligibility check failed for appt %s", appt.emr_appt_id
                )
                results["error"] += 1

        await self._db.commit()
        logger.info("Eligibility job complete for %s: %s", target_date, results)
        return results

    async def _check_single(self, appt: object) -> str:
        """Check one appointment. Returns outcome string for counters."""
        from haloflow.ehr.base import Appointment as Appt
        assert isinstance(appt, Appt)

        insurance = await self._ehr.get_patient_insurance(appt.emr_patient_id)
        payer_id: str = (
            insurance.get("insuranceid")
            or insurance.get("payerid")
            or insurance.get("payer_id")
            or ""
        )
        member_id: str = insurance.get("memberId") or insurance.get("memberid") or ""
        group_number: str | None = insurance.get("groupNumber") or insurance.get("groupnumber")

        is_priority = payer_id in settings.priority_payer_ids

        check = EligibilityCheck(
            tenant_id=settings.tenant_id,
            emr_appt_id=appt.emr_appt_id,
            emr_patient_id=appt.emr_patient_id,
            appt_date=appt.scheduled_datetime.date(),
            payer_id=payer_id,
            member_id=member_id,
            group_number=group_number,
            is_priority_payer=is_priority,
        )
        self._db.add(check)
        await self._db.flush()

        if not is_priority:
            check.status = EligibilityCheckStatus.MANUAL_REQUIRED
            logger.info(
                "Payer %s not priority — manual flag set for appt %s",
                payer_id,
                appt.emr_appt_id,
            )
            return "manual"

        # Priority payer → submit 270
        patient = await self._ehr.get_patient(appt.emr_patient_id)
        result = await self._stedi.check_eligibility(
            payer_id=payer_id,
            member_id=member_id,
            member_last_name=patient.last_name,
            member_first_name=patient.first_name,
            member_dob=patient.dob,
            service_date=appt.scheduled_datetime.date(),
            npi=PRACTICE_NPI,
            group_number=group_number,
        )

        check.payer_name = result.payer_name
        check.plan_name = result.plan_name
        check.office_visit_copay = result.office_visit_copay
        check.coverage_begin = result.coverage_begin
        check.coverage_end = result.coverage_end
        check.raw_response = result.raw_response
        check.checked_at = datetime.utcnow()

        if result.status == EligibilityStatus.ACTIVE:
            check.status = EligibilityCheckStatus.ACTIVE
            return "active"
        elif result.status == EligibilityStatus.INACTIVE:
            check.status = EligibilityCheckStatus.INACTIVE
            # Flag for office notification — surfaced via /eligibility/alerts
            check.office_notified = False
            logger.warning(
                "INACTIVE coverage: appt=%s payer=%s member=%s",
                appt.emr_appt_id,
                payer_id,
                member_id[:4] + "***",
            )
            return "inactive"
        else:
            check.status = EligibilityCheckStatus.ERROR
            return "error"

    async def get_ineligible_alerts(self) -> list[EligibilityCheck]:
        """Return INACTIVE checks not yet acknowledged by office staff."""
        result = await self._db.execute(
            select(EligibilityCheck).where(
                EligibilityCheck.tenant_id == settings.tenant_id,
                EligibilityCheck.status == EligibilityCheckStatus.INACTIVE,
                EligibilityCheck.office_notified == False,  # noqa: E712
            )
        )
        return list(result.scalars().all())

    async def acknowledge_alert(self, check_id: int) -> None:
        """Mark an ineligible alert as acknowledged (staff has seen it)."""
        check = await self._db.get(EligibilityCheck, check_id)
        if check:
            check.office_notified = True
            await self._db.commit()

    async def get_manual_queue(self) -> list[EligibilityCheck]:
        """Return non-priority payer appointments pending manual verification."""
        result = await self._db.execute(
            select(EligibilityCheck).where(
                EligibilityCheck.tenant_id == settings.tenant_id,
                EligibilityCheck.status == EligibilityCheckStatus.MANUAL_REQUIRED,
            ).order_by(EligibilityCheck.appt_date)
        )
        return list(result.scalars().all())
