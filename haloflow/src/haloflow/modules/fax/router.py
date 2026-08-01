"""Fax routing endpoints — staff queue, outbound send, routing rule management."""
import logging

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.config import get_settings
from haloflow.database import get_db
from haloflow.ehr.athenahealth import AthenaHealthAdapter
from haloflow.integrations.notifyre import NotifyreClient
from haloflow.modules.fax.models import FaxDirection, FaxQueueStatus, FaxRecord
from haloflow.modules.fax.service import FaxService

router = APIRouter(prefix="/fax", tags=["fax"])
logger = logging.getLogger(__name__)
settings = get_settings()


@router.get("/inbound/queue")
async def inbound_queue(
    queue: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return unacknowledged inbound faxes, optionally filtered by queue name."""
    query = select(FaxRecord).where(
        FaxRecord.tenant_id == settings.tenant_id,
        FaxRecord.direction == FaxDirection.INBOUND,
    )
    if queue:
        query = query.where(FaxRecord.routed_to_queue == queue)
    query = query.order_by(FaxRecord.received_at.desc())

    result = await db.execute(query)
    records = result.scalars().all()
    return {
        "count": len(records),
        "items": [
            {
                "id": r.id,
                "from_number": r.from_number,
                "queue": r.routed_to_queue,
                "pages": r.pages,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "status": r.status.value,
                "external_fax_id": r.external_fax_id,
            }
            for r in records
        ],
    }


@router.post("/outbound/send")
async def send_fax(
    emr_patient_id: str = Form(...),
    receiving_fax: str = Form(...),
    receiving_org: str = Form(...),
    subject: str = Form(...),
    receiving_provider: str | None = Form(None),
    notes: str | None = Form(None),
    document: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Send an outbound fax. Cover sheet is auto-populated from EMR.
    Staff provides: patient ID, recipient fax/org, subject, and the PDF to send.
    """
    pdf_bytes = await document.read()
    svc = FaxService(db=db, ehr=AthenaHealthAdapter(), fax_client=NotifyreClient())
    record = await svc.send_outbound_fax(
        emr_patient_id=emr_patient_id,
        receiving_fax=receiving_fax,
        receiving_org=receiving_org,
        receiving_provider=receiving_provider,
        subject=subject,
        notes=notes,
        pdf_bytes=pdf_bytes,
    )
    return {
        "fax_record_id": record.id,
        "external_fax_id": record.external_fax_id,
        "status": record.status.value,
    }


@router.post("/routing-rules")
async def create_routing_rule(
    from_number: str | None = None,
    from_number_prefix: str | None = None,
    sender_org_name: str | None = None,
    queue: str = "general",
    is_catch_all: bool = False,
    priority: int = 100,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new inbound fax routing rule."""
    svc = FaxService(db=db, ehr=AthenaHealthAdapter(), fax_client=NotifyreClient())
    rule = await svc.create_routing_rule(
        from_number=from_number,
        from_number_prefix=from_number_prefix,
        sender_org_name=sender_org_name,
        queue=queue,
        is_catch_all=is_catch_all,
        priority=priority,
    )
    return {"id": rule.id, "queue": rule.queue}


@router.get("/outbound/history")
async def outbound_history(db: AsyncSession = Depends(get_db)) -> dict:
    """Recent outbound faxes and their delivery status."""
    result = await db.execute(
        select(FaxRecord)
        .where(
            FaxRecord.tenant_id == settings.tenant_id,
            FaxRecord.direction == FaxDirection.OUTBOUND,
        )
        .order_by(FaxRecord.created_at.desc())
        .limit(50)
    )
    records = result.scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "to_number": r.to_number,
                "receiving_org": r.receiving_org,
                "subject": r.subject,
                "pages": r.pages,
                "status": r.status.value,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "delivery_confirmed_at": (
                    r.delivery_confirmed_at.isoformat()
                    if r.delivery_confirmed_at else None
                ),
            }
            for r in records
        ]
    }
