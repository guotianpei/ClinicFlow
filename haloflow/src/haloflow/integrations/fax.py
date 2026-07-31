"""
Fax API integration — SRFax (default).

SRFax offers a HIPAA BAA and no per-seat pricing, fitting the pilot model.
Inbound: webhooks notify on new fax → route to correct queue.
Outbound: send fax with auto-populated cover sheet, get delivery confirmation.

If the clinic's inbound line is already a cloud/eFax line, this adapter
wraps their existing API. The fax_inbound_line env var is the dedicated
number all inbound clinical faxes should route through.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from haloflow.config import get_settings
from haloflow.ehr.base import CoverSheetData

logger = logging.getLogger(__name__)
settings = get_settings()


class FaxStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    RECEIVED = "received"


@dataclass
class InboundFax:
    fax_id: str
    from_number: str
    to_number: str
    received_at: datetime
    pages: int
    filename: str        # PDF filename in SRFax storage
    caller_id: str | None


@dataclass
class OutboundFaxResult:
    fax_id: str
    status: FaxStatus
    queued_at: datetime
    pages: int


class SRFaxClient:
    """
    SRFax JSON API client.
    SRFax uses form-encoded POST for all operations (legacy SOAP-ish style).
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._base_params = {
            "action": "",  # overridden per call
            "access_id": settings.fax_account_number,
            "access_pwd": settings.fax_password,
            "sResponseFormat": "JSON",
        }

    def _params(self, **kwargs: object) -> dict:
        return {**self._base_params, **kwargs}

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
    )
    async def get_inbound_faxes(self, *, unread_only: bool = True) -> list[InboundFax]:
        """
        Poll for inbound faxes on the dedicated inbound line.
        Call this on a schedule (every 5–15 min) from the fax routing job.
        """
        resp = await self._client.post(
            settings.fax_api_url,
            data=self._params(
                action="Get_Fax_Inbox",
                sDirection="IN",
                sMarkasViewed="N" if unread_only else "Y",
            ),
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("Status") != "Success":
            logger.warning("SRFax Get_Fax_Inbox error: %s", result.get("Result"))
            return []

        faxes = []
        for raw in result.get("Result", []):
            faxes.append(InboundFax(
                fax_id=raw.get("FileName", ""),
                from_number=raw.get("CallerID", ""),
                to_number=raw.get("ToFaxNumber", ""),
                received_at=_parse_srfax_datetime(raw.get("Date", "")),
                pages=int(raw.get("Pages", 0)),
                filename=raw.get("FileName", ""),
                caller_id=raw.get("CallerID"),
            ))
        return faxes

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
    )
    async def send_fax(
        self,
        *,
        to_number: str,
        cover_sheet: CoverSheetData,
        pdf_base64: str,
    ) -> OutboundFaxResult:
        """
        Send an outbound fax with auto-populated cover sheet.
        `pdf_base64` is the base64-encoded content of the document pages.
        """
        # Build cover sheet as first page in the PDF payload
        cover_text = _render_cover_sheet(cover_sheet)

        resp = await self._client.post(
            settings.fax_api_url,
            data=self._params(
                action="Queue_Fax",
                sToFaxNumber=_normalize_fax(to_number),
                sFromFaxNumber=settings.fax_from_number,
                sCoverPage="Basic",
                sCPFromName="HaloFlow Clinic Automation",
                sCPToName=cover_sheet.receiving_org,
                sCPOrganization=cover_sheet.receiving_org,
                sCPSubject=cover_sheet.subject,
                sCPComments=cover_text,
                sFileName_x="document.pdf",
                sFileContent_x=pdf_base64,
            ),
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("Status") != "Success":
            logger.error("SRFax send error: %s", result.get("Result"))
            raise RuntimeError(f"Fax send failed: {result.get('Result')}")

        fax_id = str(result.get("Result", ""))
        logger.info("Fax queued to %s — fax_id=%s", _mask_fax(to_number), fax_id)
        return OutboundFaxResult(
            fax_id=fax_id,
            status=FaxStatus.QUEUED,
            queued_at=datetime.utcnow(),
            pages=cover_sheet.pages_to_follow + 1,  # +1 for cover sheet
        )

    async def get_fax_status(self, fax_id: str) -> FaxStatus:
        """Poll delivery status for a sent fax."""
        resp = await self._client.post(
            settings.fax_api_url,
            data=self._params(action="Get_FaxStatus", sFaxDetailsID=fax_id),
        )
        resp.raise_for_status()
        result = resp.json()
        raw_status = result.get("Result", [{}])[0].get("Status", "").lower()
        if "sent" in raw_status or "success" in raw_status:
            return FaxStatus.SENT
        if "fail" in raw_status or "error" in raw_status:
            return FaxStatus.FAILED
        return FaxStatus.SENDING

    async def close(self) -> None:
        await self._client.aclose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _render_cover_sheet(cs: CoverSheetData) -> str:
    """Plain-text body for the cover page comments field."""
    lines = [
        f"Patient: {cs.patient_name}",
        f"DOB: {cs.dob.strftime('%m/%d/%Y')}",
    ]
    if cs.member_id:
        lines.append(f"Member ID: {cs.member_id}")
    if cs.referring_provider:
        lines.append(f"Referring Provider: {cs.referring_provider}")
    if cs.receiving_provider:
        lines.append(f"Attn: {cs.receiving_provider}")
    if cs.notes:
        lines.append(f"\n{cs.notes}")
    lines.append(f"\nPages to follow: {cs.pages_to_follow}")
    return "\n".join(lines)


def _normalize_fax(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) == 10:
        return f"1{digits}"
    return digits


def _mask_fax(number: str) -> str:
    d = _normalize_fax(number)
    return f"***{d[-4:]}" if len(d) >= 4 else "***"


def _parse_srfax_datetime(s: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.utcnow()
