"""Eligibility endpoints — staff-facing alert queue and manual check queue."""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.database import get_db
from haloflow.ehr.athenahealth import AthenaHealthAdapter
from haloflow.integrations.stedi import StediEligibilityClient
from haloflow.modules.eligibility.service import EligibilityService

router = APIRouter(prefix="/eligibility", tags=["eligibility"])
logger = logging.getLogger(__name__)


@router.get("/alerts")
async def ineligible_alerts(db: AsyncSession = Depends(get_db)) -> dict:
    """
    INACTIVE eligibility results pending office acknowledgement.
    These are appointments where automated 270 returned inactive coverage.
    """
    svc = EligibilityService(db=db, ehr=AthenaHealthAdapter(), stedi=StediEligibilityClient())
    alerts = await svc.get_ineligible_alerts()
    return {
        "count": len(alerts),
        "alerts": [
            {
                "id": a.id,
                "appt_id": a.emr_appt_id,
                "appt_date": a.appt_date.isoformat(),
                "payer_name": a.payer_name,
                "member_id": a.member_id,
                "checked_at": a.checked_at.isoformat() if a.checked_at else None,
            }
            for a in alerts
        ],
    }


@router.post("/alerts/{check_id}/acknowledge", status_code=204)
async def acknowledge_alert(
    check_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Mark an ineligible alert as seen by office staff."""
    svc = EligibilityService(db=db, ehr=AthenaHealthAdapter(), stedi=StediEligibilityClient())
    await svc.acknowledge_alert(check_id)


@router.get("/manual-queue")
async def manual_queue(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Non-priority payer appointments requiring manual staff verification.
    Sorted by appointment date ascending.
    """
    svc = EligibilityService(db=db, ehr=AthenaHealthAdapter(), stedi=StediEligibilityClient())
    items = await svc.get_manual_queue()
    return {
        "count": len(items),
        "items": [
            {
                "id": i.id,
                "appt_id": i.emr_appt_id,
                "appt_date": i.appt_date.isoformat(),
                "payer_id": i.payer_id,
                "member_id": i.member_id,
            }
            for i in items
        ],
    }
