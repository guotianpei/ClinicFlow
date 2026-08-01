"""
Fax routing service — inbound sort + outbound auto-populate.

INBOUND Job (every 15 min): poll_inbound_faxes()
  - Polls Notifyre for new faxes on the dedicated inbound line
  - Matches sender number against FaxRoutingRule table (exact → prefix → catch-all)
  - Writes FaxRecord with queue assignment
  - Unmatched faxes → "general" queue, flagged for staff review

OUTBOUND (on-demand, staff-triggered): send_outbound_fax()
  - Staff selects document(s) and provides: recipient fax, org, subject
  - Auto-populates cover sheet from EMR patient record
  - Sends via Notifyre
  - Logs FaxRecord; polls for delivery confirmation

No OCR in Tier 2 — content routing is purely by sender number.
OCR-based content review (missing signatures, incomplete records) is Tier 3.
"""
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.config import get_settings
from haloflow.ehr.base import EHRAdapter
from haloflow.integrations.notifyre import FaxStatus, InboundFax, NotifyreClient
from haloflow.modules.fax.models import (
    FaxDirection,
    FaxQueueStatus,
    FaxRecord,
    FaxRoutingRule,
)

logger = logging.getLogger(__name__)
settings = get_settings()

DEFAULT_QUEUE = "general"


class FaxService:
    def __init__(
        self,
        db: AsyncSession,
        ehr: EHRAdapter,
        fax_client: NotifyreClient,
    ) -> None:
        self._db = db
        self._ehr = ehr
        self._fax = fax_client

    # ── Inbound polling job ───────────────────────────────────────────────────

    async def poll_inbound_faxes(self) -> int:
        """
        Fetch new inbound faxes and route each to the correct queue.
        Returns count of faxes processed.
        """
        faxes = await self._fax.get_inbound_faxes(unread_only=True)
        if not faxes:
            return 0

        # Load routing rules once, sorted by priority
        rules_result = await self._db.execute(
            select(FaxRoutingRule)
            .where(FaxRoutingRule.tenant_id == settings.tenant_id)
            .order_by(FaxRoutingRule.priority)
        )
        rules = list(rules_result.scalars().all())

        processed = 0
        for fax in faxes:
            await self._route_inbound(fax, rules)
            processed += 1

        await self._db.commit()
        logger.info("Fax polling: routed %d inbound faxes", processed)
        return processed

    async def _route_inbound(
        self, fax: InboundFax, rules: list[FaxRoutingRule]
    ) -> None:
        """Apply routing rules to a single inbound fax and create FaxRecord."""
        from_number = fax.from_number or ""
        queue, rule_id = self._match_route(from_number, rules)
        is_unrouted = queue == DEFAULT_QUEUE and rule_id is None

        record = FaxRecord(
            tenant_id=settings.tenant_id,
            direction=FaxDirection.INBOUND,
            status=FaxQueueStatus.ROUTED if not is_unrouted else FaxQueueStatus.UNROUTED,
            external_fax_id=fax.fax_id,
            from_number=fax.from_number,
            to_number=fax.to_number,
            pages=fax.pages,
            routed_to_queue=queue,
            routing_rule_id=rule_id,
            received_at=fax.received_at,
        )
        self._db.add(record)

        if is_unrouted:
            logger.info(
                "Inbound fax %s from %s — no rule matched, sent to '%s' for staff review",
                fax.fax_id,
                _mask_fax(from_number),
                DEFAULT_QUEUE,
            )
        else:
            logger.info(
                "Inbound fax %s from %s → queue='%s' (rule %s)",
                fax.fax_id,
                _mask_fax(from_number),
                queue,
                rule_id,
            )

    def _match_route(
        self, from_number: str, rules: list[FaxRoutingRule]
    ) -> tuple[str, int | None]:
        """
        Priority order:
        1. Exact match on from_number
        2. Prefix match on from_number_prefix
        3. Catch-all rule
        4. DEFAULT_QUEUE (staff review)
        """
        # Exact match
        for rule in rules:
            if rule.from_number and rule.from_number == from_number:
                return rule.queue, rule.id

        # Prefix match
        for rule in rules:
            if rule.from_number_prefix and from_number.startswith(rule.from_number_prefix):
                return rule.queue, rule.id

        # Catch-all
        for rule in rules:
            if rule.is_catch_all:
                return rule.queue, rule.id

        return DEFAULT_QUEUE, None

    # ── Outbound fax ──────────────────────────────────────────────────────────

    async def send_outbound_fax(
        self,
        *,
        emr_patient_id: str,
        receiving_fax: str,
        receiving_org: str,
        receiving_provider: str | None,
        subject: str,
        notes: str | None,
        pdf_bytes: bytes,
    ) -> FaxRecord:
        """
        Send an outbound fax with auto-populated cover sheet.
        Cover sheet data is pulled from the EMR; staff provides recipient + subject.
        """
        pages = max(1, len(pdf_bytes) // 50_000)  # rough page estimate

        cover_sheet = await self._ehr.get_cover_sheet_data(
            emr_patient_id=emr_patient_id,
            receiving_fax=receiving_fax,
            receiving_org=receiving_org,
            receiving_provider=receiving_provider,
            subject=subject,
            notes=notes,
            pages_to_follow=pages,
        )

        result = await self._fax.send_fax(
            to_number=receiving_fax,
            cover_sheet=cover_sheet,
            pdf_bytes=pdf_bytes,
        )

        record = FaxRecord(
            tenant_id=settings.tenant_id,
            direction=FaxDirection.OUTBOUND,
            status=FaxQueueStatus.QUEUED,
            external_fax_id=result.fax_id,
            to_number=receiving_fax,
            from_number=settings.notifyre_fax_from_number,
            pages=result.pages,
            emr_patient_id=emr_patient_id,
            subject=subject,
            receiving_org=receiving_org,
            sent_at=result.queued_at,
        )
        self._db.add(record)
        await self._db.commit()
        logger.info(
            "Outbound fax queued: fax_id=%s to %s for patient %s",
            result.fax_id,
            _mask_fax(receiving_fax),
            emr_patient_id,
        )
        return record

    async def confirm_outbound_delivery(self) -> int:
        """
        Poll Notifyre for delivery confirmation on QUEUED outbound faxes.
        Updates status and EMR-logged flag. Run every 30 min.
        """
        result = await self._db.execute(
            select(FaxRecord).where(
                FaxRecord.tenant_id == settings.tenant_id,
                FaxRecord.direction == FaxDirection.OUTBOUND,
                FaxRecord.status == FaxQueueStatus.QUEUED,
            )
        )
        pending = list(result.scalars().all())
        confirmed = 0

        for record in pending:
            if not record.external_fax_id:
                continue
            try:
                delivery_status = await self._fax.get_fax_status(record.external_fax_id)
                if delivery_status == FaxStatus.SENT:
                    record.status = FaxQueueStatus.SENT
                    record.delivery_confirmed_at = datetime.utcnow()
                    confirmed += 1
                    logger.info("Outbound fax %s confirmed delivered", record.external_fax_id)
                elif delivery_status == FaxStatus.FAILED:
                    record.status = FaxQueueStatus.FAILED
                    logger.warning("Outbound fax %s failed", record.external_fax_id)
            except Exception:
                logger.exception(
                    "Error checking fax status for %s", record.external_fax_id
                )

        await self._db.commit()
        return confirmed

    # ── Routing rule management ────────────────────────────────────────────────

    async def create_routing_rule(
        self,
        *,
        from_number: str | None = None,
        from_number_prefix: str | None = None,
        sender_org_name: str | None = None,
        queue: str,
        is_catch_all: bool = False,
        priority: int = 100,
    ) -> FaxRoutingRule:
        rule = FaxRoutingRule(
            tenant_id=settings.tenant_id,
            from_number=from_number,
            from_number_prefix=from_number_prefix,
            sender_org_name=sender_org_name,
            queue=queue,
            is_catch_all=is_catch_all,
            priority=priority,
        )
        self._db.add(rule)
        await self._db.commit()
        return rule


def _mask_fax(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"
