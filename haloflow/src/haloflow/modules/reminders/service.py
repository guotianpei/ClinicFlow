"""
Reminder service — appointment confirmation + no-show rebooking.

Job 1 (daily, morning): send_upcoming_reminders()
  - Pulls appointments N days out from the EMR
  - Sends one SMS per patient if no ReminderRecord exists yet
  - Records sent message in DB

Job 2 (daily, evening): process_no_responses()
  - Marks reminders sent >24h ago with no reply as NO_RESPONSE
  - No automatic action; status is visible in the staff dashboard

Job 3 (daily, mid-morning): send_rebook_prompts()
  - Finds no-show appointments from the prior day (via EMR status)
  - Sends one rebooking prompt if not already sent
  - Never sends a second prompt to the same appointment

Webhook (real-time): handle_sms_reply()
  - Called by Notifyre webhook when a patient texts back
  - Matches reply phone → appointment → writes Y/N back to EMR

All decisions are rules-based. No AI inference. No clinical content in messages.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.config import get_settings
from haloflow.ehr.base import AppointmentStatus, EHRAdapter
from haloflow.integrations.notifyre import (
    NotifyreClient,
    care_gap_message,
    no_show_rebook_message,
    reminder_message,
)
from haloflow.modules.reminders.models import RebookPrompt, ReminderRecord, ReminderStatus

logger = logging.getLogger(__name__)
settings = get_settings()

PRACTICE_NAME = "your clinic"   # TODO: pull from tenant config
PRACTICE_PHONE = "555-555-5555"  # TODO: pull from tenant config


class ReminderService:
    def __init__(
        self,
        db: AsyncSession,
        ehr: EHRAdapter,
        sms: NotifyreClient,
    ) -> None:
        self._db = db
        self._ehr = ehr
        self._sms = sms

    # ── Job 1: Send upcoming reminders ────────────────────────────────────────

    async def send_upcoming_reminders(self) -> int:
        """
        Send confirmation SMS for appointments N days from today.
        Returns count of messages sent.
        """
        target_date = (
            datetime.now(tz=timezone.utc) + timedelta(days=settings.reminder_days_before)
        ).date()

        logger.info("Running reminder job for %s", target_date)
        appointments = await self._ehr.get_appointments_by_date(target_date)
        sent = 0

        for appt in appointments:
            if appt.status in (AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED):
                continue

            # Idempotency: skip if reminder already sent for this appointment
            existing = await self._db.scalar(
                select(ReminderRecord).where(
                    ReminderRecord.emr_appt_id == appt.emr_appt_id,
                    ReminderRecord.tenant_id == settings.tenant_id,
                    ReminderRecord.status.not_in(
                        [ReminderStatus.FAILED]
                    ),
                )
            )
            if existing:
                logger.debug("Reminder already exists for appt %s", appt.emr_appt_id)
                continue

            patient = await self._ehr.get_patient(appt.emr_patient_id)
            phone = patient.phone or patient.phone_home
            if not phone:
                logger.warning(
                    "No phone number for patient %s, skipping reminder",
                    patient.emr_patient_id,
                )
                continue

            appt_display = appt.scheduled_datetime.strftime("%A, %b %d at %-I:%M %p")
            body = reminder_message(patient.first_name, appt_display, PRACTICE_NAME)

            record = ReminderRecord(
                tenant_id=settings.tenant_id,
                emr_appt_id=appt.emr_appt_id,
                emr_patient_id=appt.emr_patient_id,
                appt_datetime=appt.scheduled_datetime,
                status=ReminderStatus.PENDING,
            )
            self._db.add(record)
            await self._db.flush()  # get ID before SMS send

            try:
                message_id = await self._sms.send_sms(to=phone, body=body)
                record.notifyre_message_id = message_id
                record.status = ReminderStatus.SENT
                record.sent_at = datetime.utcnow()
                sent += 1
            except Exception:
                logger.exception(
                    "Failed to send reminder for appt %s", appt.emr_appt_id
                )
                record.status = ReminderStatus.FAILED

        await self._db.commit()
        logger.info("Reminder job complete: %d sent for %s", sent, target_date)
        return sent

    # ── Job 2: Mark no-responses ───────────────────────────────────────────────

    async def process_no_responses(self) -> int:
        """
        Mark reminders that were sent >24h ago with no patient reply as NO_RESPONSE.
        Does NOT send any further messages — staff visibility only.
        """
        cutoff = datetime.utcnow() - timedelta(hours=24)
        result = await self._db.execute(
            select(ReminderRecord).where(
                ReminderRecord.tenant_id == settings.tenant_id,
                ReminderRecord.status == ReminderStatus.SENT,
                ReminderRecord.sent_at <= cutoff,
            )
        )
        records = result.scalars().all()
        count = 0
        for rec in records:
            rec.status = ReminderStatus.NO_RESPONSE
            rec.updated_at = datetime.utcnow()
            count += 1
        await self._db.commit()
        logger.info("Marked %d reminders as no-response", count)
        return count

    # ── Job 3: Send no-show rebooking prompts ─────────────────────────────────

    async def send_rebook_prompts(self) -> int:
        """
        Find no-shows from the prior day and send one rebooking SMS each.
        Only fires within the configured window (default: 24h after no-show).
        """
        window_hours = settings.no_show_rebook_hours
        since = datetime.utcnow() - timedelta(hours=window_hours)
        now = datetime.utcnow()

        no_show_appts = await self._ehr.get_appointments_in_range(
            start=since,
            end=now,
            status=AppointmentStatus.NO_SHOW,
        )

        sent = 0
        for appt in no_show_appts:
            # Skip if rebook prompt already sent for this appointment
            existing = await self._db.scalar(
                select(RebookPrompt).where(
                    RebookPrompt.emr_appt_id == appt.emr_appt_id,
                    RebookPrompt.tenant_id == settings.tenant_id,
                )
            )
            if existing:
                continue

            patient = await self._ehr.get_patient(appt.emr_patient_id)
            phone = patient.phone or patient.phone_home
            if not phone:
                continue

            body = no_show_rebook_message(
                patient.first_name, PRACTICE_NAME, PRACTICE_PHONE
            )

            # Find the linked ReminderRecord (may not exist if patient was new)
            reminder = await self._db.scalar(
                select(ReminderRecord).where(
                    ReminderRecord.emr_appt_id == appt.emr_appt_id,
                    ReminderRecord.tenant_id == settings.tenant_id,
                )
            )

            prompt = RebookPrompt(
                tenant_id=settings.tenant_id,
                emr_appt_id=appt.emr_appt_id,
                reminder_id=reminder.id if reminder else 0,
            )
            self._db.add(prompt)
            await self._db.flush()

            try:
                msg_id = await self._sms.send_sms(to=phone, body=body)
                prompt.notifyre_message_id = msg_id
                prompt.sent_at = datetime.utcnow()
                sent += 1
            except Exception:
                logger.exception(
                    "Failed to send rebook prompt for appt %s", appt.emr_appt_id
                )

        await self._db.commit()
        logger.info("Rebook prompt job complete: %d sent", sent)
        return sent

    # ── Webhook: handle inbound SMS reply ────────────────────────────────────

    async def handle_sms_reply(
        self,
        from_phone: str,
        body: str,
        received_at: datetime,
    ) -> None:
        """
        Process an inbound SMS reply.
        Matches by patient phone → most recent pending/sent reminder.
        Writes confirmation status back to EMR.
        """
        normalized = body.strip().upper()
        confirmed = normalized.startswith("Y")  # YES, Y
        declined = normalized.startswith("N")   # NO, N, CANCEL

        if not confirmed and not declined:
            logger.info(
                "Unrecognized SMS reply '%s' from %s — ignoring", body[:20], "***"
            )
            return

        # Find most recent sent reminder for this phone number
        # NOTE: phone numbers are not stored in reminder_records to minimize PHI
        # in our DB. We match via EMR patient lookup.
        patients = await self._ehr.search_patients(mobilephone=from_phone)
        if not patients:
            patients = await self._ehr.search_patients(homephone=from_phone)
        if not patients:
            logger.warning("No patient found for inbound SMS reply — phone not matched")
            return

        patient = patients[0]
        reminder = await self._db.scalar(
            select(ReminderRecord)
            .where(
                ReminderRecord.tenant_id == settings.tenant_id,
                ReminderRecord.emr_patient_id == patient.emr_patient_id,
                ReminderRecord.status.in_([ReminderStatus.SENT, ReminderStatus.PENDING]),
            )
            .order_by(ReminderRecord.appt_datetime.asc())
        )

        if not reminder:
            logger.warning(
                "No active reminder for patient %s — reply ignored",
                patient.emr_patient_id,
            )
            return

        reminder.status = ReminderStatus.CONFIRMED if confirmed else ReminderStatus.DECLINED
        reminder.replied_at = received_at
        reminder.reply_text = body[:200]

        if not reminder.emr_writeback_done:
            try:
                await self._ehr.write_confirmation_status(
                    reminder.emr_appt_id, confirmed=confirmed
                )
                reminder.emr_writeback_done = True
            except Exception:
                logger.exception(
                    "EMR writeback failed for appt %s", reminder.emr_appt_id
                )

        await self._db.commit()
        logger.info(
            "Processed SMS reply: appt=%s confirmed=%s",
            reminder.emr_appt_id,
            confirmed,
        )
