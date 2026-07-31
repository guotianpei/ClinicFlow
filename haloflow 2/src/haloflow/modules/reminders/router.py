"""
Reminders router — FastAPI endpoints.

POST /webhooks/sms/inbound   — Telnyx inbound SMS webhook
GET  /reminders/status       — Staff-facing: reminders sent today + reply status
"""
import hashlib
import hmac
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.config import get_settings
from haloflow.database import get_db
from haloflow.ehr.athenahealth import AthenaHealthAdapter
from haloflow.integrations.telnyx import TelnyxSMSClient
from haloflow.modules.reminders.models import ReminderRecord
from haloflow.modules.reminders.service import ReminderService

router = APIRouter(tags=["reminders"])
logger = logging.getLogger(__name__)
settings = get_settings()


# ── Telnyx inbound SMS webhook ────────────────────────────────────────────────

class TelnyxSMSWebhook(BaseModel):
    data: dict


@router.post("/webhooks/sms/inbound", status_code=status.HTTP_204_NO_CONTENT)
async def inbound_sms(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Receive inbound SMS replies from Telnyx.
    Validates webhook signature, then routes YES/NO replies to ReminderService.
    """
    body_bytes = await request.body()
    _verify_telnyx_signature(request, body_bytes)

    payload = await request.json()
    event_type = payload.get("data", {}).get("event_type", "")
    if event_type != "message.received":
        return  # ignore other event types (delivery receipts, etc.)

    msg = payload["data"]["payload"]
    from_phone: str = msg["from"]["phone_number"]
    text: str = msg.get("text", "")
    received_at = datetime.utcnow()

    svc = ReminderService(
        db=db,
        ehr=AthenaHealthAdapter(),
        sms=TelnyxSMSClient(),
    )
    await svc.handle_sms_reply(from_phone=from_phone, body=text, received_at=received_at)


# ── Staff status endpoint ──────────────────────────────────────────────────────

@router.get("/reminders/status")
async def reminders_status(
    date: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return reminder send/reply status for a given date (defaults to today).
    Used by a future staff dashboard.
    """
    query = select(ReminderRecord).where(
        ReminderRecord.tenant_id == settings.tenant_id,
    )
    if date:
        from datetime import date as date_type
        target = date_type.fromisoformat(date)
        query = query.where(
            ReminderRecord.appt_datetime >= datetime.combine(target, datetime.min.time()),
            ReminderRecord.appt_datetime < datetime.combine(target, datetime.max.time()),
        )

    result = await db.execute(query.order_by(ReminderRecord.appt_datetime))
    records = result.scalars().all()

    return {
        "date": date or datetime.utcnow().date().isoformat(),
        "total": len(records),
        "by_status": _count_by_status(records),
        "records": [
            {
                "appt_id": r.emr_appt_id,
                "appt_datetime": r.appt_datetime.isoformat(),
                "status": r.status.value,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "replied_at": r.replied_at.isoformat() if r.replied_at else None,
                "emr_writeback": r.emr_writeback_done,
            }
            for r in records
        ],
    }


def _verify_telnyx_signature(request: Request, body: bytes) -> None:
    """Validate Telnyx webhook signature to prevent spoofed webhooks."""
    sig = request.headers.get("telnyx-signature-ed25519-v1", "")
    timestamp = request.headers.get("telnyx-timestamp", "")
    if not sig or not timestamp:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    # Telnyx signs: timestamp + "|" + body
    signed_payload = f"{timestamp}|".encode() + body
    expected = hmac.new(
        settings.webhook_secret.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def _count_by_status(records: list[ReminderRecord]) -> dict:
    counts: dict[str, int] = {}
    for r in records:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    return counts
