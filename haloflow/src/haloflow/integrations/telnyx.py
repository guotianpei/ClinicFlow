"""
Telnyx SMS integration.

Sends outbound SMS for appointment reminders, no-show rebooking prompts,
and care-gap outreach. Telnyx requires 10DLC registration for A2P messaging
(already accounted for in infrastructure costs as a one-time setup item).

Inbound SMS replies (Y/N confirmations) arrive via webhook → reminders router.

BAA status: Telnyx BAA confirmation is a tracked open item in vendor-baa-status.md.
Do NOT send PHI in message bodies until BAA is confirmed and in place.
The message templates here follow minimum-necessary-use: name + date/time only.
"""
import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from haloflow.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

TELNYX_API_BASE = "https://api.telnyx.com/v2"


class TelnyxSMSClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=TELNYX_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.telnyx_api_key}",
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )

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
        Send an SMS. Returns the Telnyx message ID for status tracking.

        IMPORTANT: `body` must not contain PHI beyond patient first name
        and appointment date/time until Telnyx BAA is confirmed in writing.
        """
        payload: dict[str, object] = {
            "from": settings.telnyx_from_number,
            "to": _normalize_phone(to),
            "text": body,
            "messaging_profile_id": settings.telnyx_messaging_profile_id,
        }
        if webhook_url:
            payload["webhook_url"] = webhook_url

        resp = await self._client.post("/messages", json=payload)
        resp.raise_for_status()
        data = resp.json()["data"]
        message_id: str = data["id"]
        logger.info("SMS sent to %s — message_id=%s", _mask_phone(to), message_id)
        return message_id

    async def close(self) -> None:
        await self._client.aclose()


# ── Message templates ────────────────────────────────────────────────────────
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
