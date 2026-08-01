"""
Notifyre integration — SMS + fax.

Notifyre offers a HIPAA BAA and covers both channels off a single API key,
replacing the two separate vendors (Telnyx for SMS, SRFax for fax) used
previously — one contract, one BAA, one client to maintain.

SMS: appointment reminders, no-show rebooking prompts, care-gap outreach.
Inbound SMS replies (Y/N confirmations) arrive via webhook → reminders router.

Fax: inbound routing (poll the dedicated inbound line) + outbound with
auto-populated cover sheet and delivery confirmation.

BAA status: Notifyre BAA confirmation is a tracked open item in vendor-baa-status.md.
Do NOT send PHI in SMS message bodies until BAA is confirmed and in place.
The SMS templates here follow minimum-necessary-use: name + date/time only.
Fax is the primary clinical-document channel and is covered once the BAA is signed.
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

NOTIFYRE_API_BASE = "https://api.notifyre.com"


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
    filename: str        # source PDF filename in Notifyre storage
    caller_id: str | None


@dataclass
class OutboundFaxResult:
    fax_id: str
    status: FaxStatus
    queued_at: datetime
    pages: int


class NotifyreClient:
    """
    Notifyre API client — SMS + fax.
    Auth: `x-api-token` header (account-level API token).
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=NOTIFYRE_API_BASE,
            headers={"x-api-token": settings.notifyre_api_key},
            timeout=30.0,
        )

    # ── SMS ──────────────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        stop=stop_after_attempt(3),
    )
    async def send_sms(
        self,
        to: str,
        body: str,
        *,
        webhook_url: str | None = None,
    ) -> str:
        """
        Send an SMS. Returns the Notifyre message ID for status tracking.

        IMPORTANT: `body` must not contain PHI beyond patient first name
        and appointment date/time until Notifyre BAA is confirmed in writing.
        """
        payload: dict[str, object] = {
            "body": body,
            "from": settings.notifyre_sms_from,
            "recipients": [{"type": "mobile_number", "value": _normalize_phone(to)}],
        }
        if webhook_url:
            payload["callbackUrl"] = webhook_url

        resp = await self._client.post("/sms/send", json=payload)
        resp.raise_for_status()
        data = resp.json()["payload"]
        message_id: str = data["smsMessageID"]
        logger.info("SMS sent to %s — message_id=%s", _mask_phone(to), message_id)
        return message_id

    # ── Fax ──────────────────────────────────────────────────────────────────

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
        resp = await self._client.get(
            "/fax/received",
            params={
                "toNumber": settings.notifyre_fax_inbound_number,
                "unreadOnly": "true" if unread_only else "false",
                "limit": 100,
            },
        )
        resp.raise_for_status()
        result = resp.json()

        if not result.get("success"):
            logger.warning("Notifyre fax/received error: %s", result.get("message"))
            return []

        faxes = []
        for raw in result.get("payload", {}).get("list", []):
            files = raw.get("files", [{}])
            filename = files[0].get("fileName", "") if files else ""
            faxes.append(InboundFax(
                fax_id=raw.get("faxID", ""),
                from_number=raw.get("sender", {}).get("number", ""),
                to_number=raw.get("recipient", {}).get("number", ""),
                received_at=_parse_notifyre_datetime(raw.get("receivedDate", "")),
                pages=int(raw.get("pages", 0)),
                filename=filename,
                caller_id=raw.get("sender", {}).get("number"),
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
        pdf_bytes: bytes,
    ) -> OutboundFaxResult:
        """
        Send an outbound fax with an auto-populated cover sheet.
        `pdf_bytes` is the raw content of the document pages, uploaded as
        multipart form data (Notifyre's fax API takes file uploads directly).
        """
        cover_text = _render_cover_sheet(cover_sheet)

        resp = await self._client.post(
            "/fax/send",
            data={
                "recipients": [{"type": "fax_number", "value": _normalize_fax(to_number)}],
                "faxCoverPage": True,
                "coverPageSubject": cover_sheet.subject,
                "coverPageMessage": cover_text,
                "clientReference": cover_sheet.receiving_org,
            },
            files={"files": ("document.pdf", pdf_bytes, "application/pdf")},
        )
        resp.raise_for_status()
        result = resp.json()

        if not result.get("success"):
            logger.error("Notifyre fax send error: %s", result.get("message"))
            raise RuntimeError(f"Fax send failed: {result.get('message')}")

        fax_id = str(result.get("payload", {}).get("faxID", ""))
        logger.info("Fax queued to %s — fax_id=%s", _mask_fax(to_number), fax_id)
        return OutboundFaxResult(
            fax_id=fax_id,
            status=FaxStatus.QUEUED,
            queued_at=datetime.utcnow(),
            pages=cover_sheet.pages_to_follow + 1,  # +1 for cover sheet
        )

    async def get_fax_status(self, fax_id: str) -> FaxStatus:
        """Poll delivery status for a sent fax."""
        resp = await self._client.get(f"/fax/send/{fax_id}")
        resp.raise_for_status()
        result = resp.json()
        raw_status = result.get("payload", {}).get("status", "").lower()
        if "success" in raw_status or "sent" in raw_status:
            return FaxStatus.SENT
        if "fail" in raw_status or "error" in raw_status:
            return FaxStatus.FAILED
        return FaxStatus.SENDING

    async def close(self) -> None:
        await self._client.aclose()


# ── SMS message templates ────────────────────────────────────────────────────
# Minimum necessary PHI: first name + date/time only. No diagnosis, no DOB,
# no insurance info. Kept under 160 chars to avoid multi-part SMS billing.

def reminder_message(first_name: str, appt_datetime: str, practice_name: str) -> str:
    """2-day pre-visit confirmation request."""
    return (
        f"Hi {first_name}, this is {practice_name}. "
        f"You have an appt on {appt_datetime}. "
        f"Reply YES to confirm or NO to cancel. "
        f"Reply STOP to opt out."
    )


def no_show_rebook_message(first_name: str, practice_name: str, phone: str) -> str:
    """Sent the day after a missed appointment."""
    return (
        f"Hi {first_name}, we missed you today at {practice_name}. "
        f"Call {phone} or reply REBOOK to reschedule. "
        f"Reply STOP to opt out."
    )


def care_gap_message(first_name: str, measure_name: str, practice_name: str, phone: str) -> str:
    """Preventive care / care-gap outreach."""
    return (
        f"Hi {first_name}, {practice_name} wants to remind you that "
        f"you may be due for your {measure_name}. "
        f"Call {phone} to schedule. "
        f"Reply STOP to opt out."
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _render_cover_sheet(cs: CoverSheetData) -> str:
    """Plain-text body for the cover page message field."""
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


def _normalize_phone(phone: str) -> str:
    """Ensure E.164 format (+1XXXXXXXXXX)."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone  # already formatted or international


def _mask_phone(phone: str) -> str:
    """Mask for logging — never log full phone numbers."""
    p = _normalize_phone(phone)
    return f"{p[:5]}***{p[-2:]}" if len(p) >= 7 else "***"


def _normalize_fax(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return number


def _mask_fax(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def _parse_notifyre_datetime(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.utcnow()
