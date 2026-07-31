"""
Care gap / preventive care outreach service.

Two modes (both required — workflow differs by source):

Mode A — EMR due-date tracking:
  - Queries EMR for patients with preventive measures due within N days
  - One SMS per patient per measure per cycle
  - Rules-based intervals only (e.g., AWV annual, A1c quarterly)
  - No diagnosis/clinical-complexity logic — that's Tier 3 recall

Mode B — Payer list outreach:
  - Staff uploads a payer care-gap CSV/XLSX
  - System parses: member_id, last_name, first_name, dob, measure
  - Matches each row to an EMR patient by member_id → name/DOB fallback
  - Sends one SMS per matched patient per upload
  - Tracks match failures (staff to handle manually)

Example measures: Medicare AWV, post-discharge TCM, breast & colorectal
cancer screening, diabetes A1c & eye exam, blood-pressure follow-up.

No clinical content in messages. SMS body = first name + measure name + call-to-action.
"""
import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from haloflow.config import get_settings
from haloflow.ehr.base import EHRAdapter
from haloflow.integrations.telnyx import TelnyxSMSClient, care_gap_message
from haloflow.modules.care_gaps.models import (
    CareGapMeasure,
    CareGapRecord,
    CareGapSource,
    OutreachStatus,
    PayerListUpload,
)

logger = logging.getLogger(__name__)
settings = get_settings()

PRACTICE_NAME = "your clinic"   # TODO: from tenant config
PRACTICE_PHONE = "555-555-5555"  # TODO: from tenant config


class CareGapService:
    def __init__(
        self,
        db: AsyncSession,
        ehr: EHRAdapter,
        sms: TelnyxSMSClient,
    ) -> None:
        self._db = db
        self._ehr = ehr
        self._sms = sms

    # ── Mode A: EMR due-date outreach ─────────────────────────────────────────

    async def run_emr_due_date_outreach(self, measure_code: str) -> int:
        """
        Fetch patients due for a measure and send outreach SMS if not already sent.
        Returns count of messages sent.
        """
        measure = await self._get_measure(measure_code)
        if not measure or not measure.interval_months:
            logger.warning(
                "Measure %s not found or has no interval — skipping EMR outreach",
                measure_code,
            )
            return 0

        due_before = date.today() + timedelta(days=30)  # due within 30 days
        patients = await self._ehr.get_patients_due_for_preventive_care(
            measure_code, due_before=due_before
        )

        sent = 0
        for patient in patients:
            # Skip if already sent this cycle (within the measure's interval)
            already_sent = await self._db.scalar(
                select(CareGapRecord).where(
                    and_(
                        CareGapRecord.tenant_id == settings.tenant_id,
                        CareGapRecord.emr_patient_id == patient.emr_patient_id,
                        CareGapRecord.measure_code == measure_code,
                        CareGapRecord.source == CareGapSource.EMR_DUE_DATE,
                        CareGapRecord.status.in_(
                            [OutreachStatus.SENT, OutreachStatus.RESPONDED]
                        ),
                        CareGapRecord.created_at
                        >= datetime.utcnow() - timedelta(days=measure.interval_months * 30),
                    )
                )
            )
            if already_sent:
                continue

            phone = patient.phone or patient.phone_home
            if not phone:
                continue

            record = CareGapRecord(
                tenant_id=settings.tenant_id,
                emr_patient_id=patient.emr_patient_id,
                measure_code=measure_code,
                source=CareGapSource.EMR_DUE_DATE,
                status=OutreachStatus.PENDING,
            )
            self._db.add(record)
            await self._db.flush()

            body = care_gap_message(
                patient.first_name,
                measure.sms_label,
                PRACTICE_NAME,
                PRACTICE_PHONE,
            )
            try:
                msg_id = await self._sms.send_sms(to=phone, body=body)
                record.telnyx_message_id = msg_id
                record.status = OutreachStatus.SENT
                record.sent_at = datetime.utcnow()
                sent += 1
            except Exception:
                logger.exception(
                    "Failed to send care gap SMS for patient %s", patient.emr_patient_id
                )
                record.status = OutreachStatus.FAILED

        await self._db.commit()
        logger.info("Care gap EMR outreach (%s): %d sent", measure_code, sent)
        return sent

    # ── Mode B: Payer list outreach ────────────────────────────────────────────

    async def ingest_payer_list(
        self,
        *,
        payer_id: str,
        payer_name: str,
        measure_code: str,
        filename: str,
        file_content: bytes,
    ) -> PayerListUpload:
        """
        Parse an uploaded payer care-gap CSV/XLSX and create CareGapRecord entries.
        Returns the upload record (outreach is triggered separately).

        Expected CSV columns (flexible header matching):
          member_id, last_name, first_name, dob, [measure_code]
        """
        rows = _parse_care_gap_file(file_content, filename)

        upload = PayerListUpload(
            tenant_id=settings.tenant_id,
            payer_id=payer_id,
            payer_name=payer_name,
            measure_code=measure_code,
            filename=filename,
            row_count=len(rows),
            status="processing",
        )
        self._db.add(upload)
        await self._db.flush()

        matched = 0
        for row in rows:
            # Match row to EMR patient
            patient = await self._match_patient(row)
            if not patient:
                logger.debug("No EMR match for payer list row: %s", row.get("last_name", "?"))
                continue

            # Skip if already outreached from this upload
            existing = await self._db.scalar(
                select(CareGapRecord).where(
                    CareGapRecord.payer_list_upload_id == upload.id,
                    CareGapRecord.emr_patient_id == patient.emr_patient_id,
                    CareGapRecord.measure_code == measure_code,
                )
            )
            if existing:
                continue

            record = CareGapRecord(
                tenant_id=settings.tenant_id,
                emr_patient_id=patient.emr_patient_id,
                measure_code=measure_code,
                source=CareGapSource.PAYER_LIST,
                payer_list_upload_id=upload.id,
                payer_id=payer_id,
                status=OutreachStatus.PENDING,
            )
            self._db.add(record)
            matched += 1

        upload.status = "pending_outreach"
        upload.processed_at = datetime.utcnow()
        upload.row_count = len(rows)

        await self._db.commit()
        logger.info(
            "Payer list ingested: upload_id=%d measure=%s rows=%d matched=%d",
            upload.id,
            measure_code,
            len(rows),
            matched,
        )
        return upload

    async def run_payer_list_outreach(self, upload_id: int) -> int:
        """
        Send SMS outreach for all PENDING CareGapRecords from a payer list upload.
        Returns count of messages sent.
        """
        upload = await self._db.get(PayerListUpload, upload_id)
        if not upload:
            raise ValueError(f"Upload {upload_id} not found")

        measure = await self._get_measure(upload.measure_code)
        sms_label = measure.sms_label if measure else upload.measure_code

        result = await self._db.execute(
            select(CareGapRecord).where(
                CareGapRecord.payer_list_upload_id == upload_id,
                CareGapRecord.status == OutreachStatus.PENDING,
            )
        )
        records = list(result.scalars().all())

        sent = 0
        for record in records:
            patient = await self._ehr.get_patient(record.emr_patient_id)
            phone = patient.phone or patient.phone_home
            if not phone:
                record.status = OutreachStatus.FAILED
                record.notes = "No phone number in EMR"
                continue

            body = care_gap_message(
                patient.first_name, sms_label, PRACTICE_NAME, PRACTICE_PHONE
            )
            try:
                msg_id = await self._sms.send_sms(to=phone, body=body)
                record.telnyx_message_id = msg_id
                record.status = OutreachStatus.SENT
                record.sent_at = datetime.utcnow()
                sent += 1
            except Exception:
                logger.exception(
                    "Failed SMS for care gap record %d", record.id
                )
                record.status = OutreachStatus.FAILED

        upload.outreach_sent = sent
        upload.status = "outreach_complete"
        await self._db.commit()
        logger.info("Payer list outreach upload=%d: %d sent", upload_id, sent)
        return sent

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_measure(self, code: str) -> CareGapMeasure | None:
        return await self._db.scalar(
            select(CareGapMeasure).where(
                CareGapMeasure.tenant_id == settings.tenant_id,
                CareGapMeasure.code == code,
                CareGapMeasure.is_active == True,  # noqa: E712
            )
        )

    async def _match_patient(self, row: dict[str, str]) -> object | None:
        """
        Match a payer list row to an EMR patient.
        Primary: member_id lookup. Fallback: name + DOB.
        """
        member_id = row.get("member_id", "").strip()
        if member_id:
            patients = await self._ehr.search_patients(insuranceid=member_id)
            if patients:
                return patients[0]

        # Fallback: last name + DOB
        last_name = row.get("last_name", "").strip()
        dob_str = row.get("dob", "").strip()
        if last_name and dob_str:
            patients = await self._ehr.search_patients(
                lastname=last_name, dob=dob_str
            )
            if len(patients) == 1:
                return patients[0]

        return None


def _parse_care_gap_file(content: bytes, filename: str) -> list[dict[str, str]]:
    """
    Parse CSV or XLSX care-gap file into a list of row dicts.
    Column headers are normalized (lowercase, underscores).
    """
    lower = filename.lower()

    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return _parse_xlsx(content)

    # Default: CSV
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [
        {_normalize_col(k): v for k, v in row.items()}
        for row in reader
        if any(v.strip() for v in row.values())
    ]


def _parse_xlsx(content: bytes) -> list[dict[str, str]]:
    """Parse XLSX using openpyxl (included in dependencies)."""
    import openpyxl  # type: ignore[import]
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    if ws is None:
        return []
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [_normalize_col(str(h)) for h in rows[0]]
    result = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        result.append({h: str(v or "") for h, v in zip(headers, row)})
    return result


def _normalize_col(name: str) -> str:
    """'Member ID' → 'member_id'"""
    return name.strip().lower().replace(" ", "_").replace("-", "_")
