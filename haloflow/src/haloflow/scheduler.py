"""
APScheduler job registry — all Tier 2 background jobs.

Jobs wired here:

  DAILY 08:00 — send_upcoming_reminders      (2 days out by default)
  DAILY 08:05 — check_upcoming_eligibility   (3 days out by default)
  DAILY 09:00 — send_rebook_prompts          (no-shows from prior day)
  DAILY 20:00 — process_no_responses         (mark 24h-expired reminders)
  EVERY 15min — poll_inbound_faxes
  EVERY 30min — confirm_outbound_delivery
  DAILY 08:10 — emr_due_date_care_gaps       (all active measures)

Times are in the practice's local timezone (America/New_York for pilot clinic).
Jobs are idempotent — safe to re-run if a prior run failed.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from haloflow.database import AsyncSessionLocal
from haloflow.ehr.athenahealth import AthenaHealthAdapter
from haloflow.integrations.notifyre import NotifyreClient
from haloflow.integrations.stedi import StediEligibilityClient
from haloflow.modules.care_gaps.models import CareGapMeasure
from haloflow.modules.care_gaps.service import CareGapService
from haloflow.modules.eligibility.service import EligibilityService
from haloflow.modules.fax.service import FaxService
from haloflow.modules.reminders.service import ReminderService

logger = logging.getLogger(__name__)
TIMEZONE = "America/New_York"


async def _job_send_reminders() -> None:
    async with AsyncSessionLocal() as db:
        svc = ReminderService(db=db, ehr=AthenaHealthAdapter(), sms=NotifyreClient())
        count = await svc.send_upcoming_reminders()
        logger.info("[scheduler] send_reminders: %d sent", count)


async def _job_check_eligibility() -> None:
    async with AsyncSessionLocal() as db:
        svc = EligibilityService(
            db=db, ehr=AthenaHealthAdapter(), stedi=StediEligibilityClient()
        )
        results = await svc.check_upcoming_eligibility()
        logger.info("[scheduler] eligibility: %s", results)


async def _job_rebook_prompts() -> None:
    async with AsyncSessionLocal() as db:
        svc = ReminderService(db=db, ehr=AthenaHealthAdapter(), sms=NotifyreClient())
        count = await svc.send_rebook_prompts()
        logger.info("[scheduler] rebook_prompts: %d sent", count)


async def _job_no_responses() -> None:
    async with AsyncSessionLocal() as db:
        svc = ReminderService(db=db, ehr=AthenaHealthAdapter(), sms=NotifyreClient())
        count = await svc.process_no_responses()
        logger.info("[scheduler] no_responses marked: %d", count)


async def _job_poll_fax() -> None:
    async with AsyncSessionLocal() as db:
        svc = FaxService(db=db, ehr=AthenaHealthAdapter(), fax_client=NotifyreClient())
        count = await svc.poll_inbound_faxes()
        if count:
            logger.info("[scheduler] fax_poll: %d faxes routed", count)


async def _job_confirm_fax_delivery() -> None:
    async with AsyncSessionLocal() as db:
        svc = FaxService(db=db, ehr=AthenaHealthAdapter(), fax_client=NotifyreClient())
        count = await svc.confirm_outbound_delivery()
        if count:
            logger.info("[scheduler] fax_delivery_confirmed: %d", count)


async def _job_emr_care_gaps() -> None:
    """Run EMR due-date outreach for all active measures."""
    from sqlalchemy import select

    from haloflow.config import get_settings
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CareGapMeasure).where(
                CareGapMeasure.tenant_id == settings.tenant_id,
                CareGapMeasure.is_active == True,   # noqa: E712
                CareGapMeasure.interval_months.is_not(None),
            )
        )
        measures = result.scalars().all()

        svc = CareGapService(db=db, ehr=AthenaHealthAdapter(), sms=NotifyreClient())
        for measure in measures:
            count = await svc.run_emr_due_date_outreach(measure.code)
            logger.info(
                "[scheduler] care_gap EMR outreach (%s): %d sent", measure.code, count
            )


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # ── Daily reminder job — 8:00 AM ────────────────────────────────────────
    scheduler.add_job(
        _job_send_reminders,
        CronTrigger(hour=8, minute=0, timezone=TIMEZONE),
        id="send_reminders",
        name="Send appointment reminder SMS",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── Eligibility checks — 8:05 AM ─────────────────────────────────────────
    scheduler.add_job(
        _job_check_eligibility,
        CronTrigger(hour=8, minute=5, timezone=TIMEZONE),
        id="check_eligibility",
        name="Pre-visit eligibility checks",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── EMR care gap outreach — 8:10 AM ──────────────────────────────────────
    scheduler.add_job(
        _job_emr_care_gaps,
        CronTrigger(hour=8, minute=10, timezone=TIMEZONE),
        id="emr_care_gaps",
        name="EMR due-date care gap outreach",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── No-show rebooking — 9:00 AM ───────────────────────────────────────────
    scheduler.add_job(
        _job_rebook_prompts,
        CronTrigger(hour=9, minute=0, timezone=TIMEZONE),
        id="rebook_prompts",
        name="No-show rebooking prompts",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── Mark no-responses — 8:00 PM ───────────────────────────────────────────
    scheduler.add_job(
        _job_no_responses,
        CronTrigger(hour=20, minute=0, timezone=TIMEZONE),
        id="no_responses",
        name="Mark no-response reminders",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── Inbound fax polling — every 15 min ────────────────────────────────────
    scheduler.add_job(
        _job_poll_fax,
        IntervalTrigger(minutes=15),
        id="poll_fax",
        name="Inbound fax polling",
        replace_existing=True,
    )

    # ── Outbound fax delivery confirmation — every 30 min ─────────────────────
    scheduler.add_job(
        _job_confirm_fax_delivery,
        IntervalTrigger(minutes=30),
        id="confirm_fax_delivery",
        name="Outbound fax delivery confirmation",
        replace_existing=True,
    )

    return scheduler
