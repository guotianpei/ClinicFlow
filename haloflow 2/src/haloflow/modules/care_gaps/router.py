"""Care gap outreach endpoints — payer list upload, outreach status."""
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.config import get_settings
from haloflow.database import get_db
from haloflow.ehr.athenahealth import AthenaHealthAdapter
from haloflow.integrations.telnyx import TelnyxSMSClient
from haloflow.modules.care_gaps.models import PayerListUpload
from haloflow.modules.care_gaps.service import CareGapService

router = APIRouter(prefix="/care-gaps", tags=["care-gaps"])
logger = logging.getLogger(__name__)
settings = get_settings()


@router.post("/payer-list/upload")
async def upload_payer_list(
    payer_id: str = Form(...),
    payer_name: str = Form(...),
    measure_code: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Upload a payer care-gap list (CSV or XLSX).
    System will parse and match patients to EMR records.
    Outreach SMS is triggered separately via /care-gaps/payer-list/{upload_id}/send.
    """
    allowed = {".csv", ".xlsx", ".xls"}
    suffix = "." + (file.filename or "").rsplit(".", 1)[-1].lower()
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported file type '{suffix}'. Use CSV or XLSX.")

    content = await file.read()
    svc = CareGapService(
        db=db, ehr=AthenaHealthAdapter(), sms=TelnyxSMSClient()
    )
    upload = await svc.ingest_payer_list(
        payer_id=payer_id,
        payer_name=payer_name,
        measure_code=measure_code,
        filename=file.filename or "upload",
        file_content=content,
    )
    return {
        "upload_id": upload.id,
        "row_count": upload.row_count,
        "status": upload.status,
        "message": f"Parsed {upload.row_count} rows. Review and trigger outreach below.",
    }


@router.post("/payer-list/{upload_id}/send")
async def send_payer_list_outreach(
    upload_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Trigger SMS outreach for a processed payer list upload."""
    svc = CareGapService(
        db=db, ehr=AthenaHealthAdapter(), sms=TelnyxSMSClient()
    )
    sent = await svc.run_payer_list_outreach(upload_id)
    return {"upload_id": upload_id, "sent": sent}


@router.get("/payer-list/uploads")
async def list_uploads(db: AsyncSession = Depends(get_db)) -> dict:
    """List all payer list uploads and their outreach progress."""
    result = await db.execute(
        select(PayerListUpload)
        .where(PayerListUpload.tenant_id == settings.tenant_id)
        .order_by(PayerListUpload.uploaded_at.desc())
        .limit(50)
    )
    uploads = result.scalars().all()
    return {
        "items": [
            {
                "id": u.id,
                "payer_name": u.payer_name,
                "measure_code": u.measure_code,
                "filename": u.filename,
                "row_count": u.row_count,
                "outreach_sent": u.outreach_sent,
                "status": u.status,
                "uploaded_at": u.uploaded_at.isoformat(),
            }
            for u in uploads
        ]
    }
