"""
Stedi eligibility integration — EDI 270/271 (HIPAA-mandated format).

Stedi wraps the EDI transaction in a clean JSON API, so we don't need
to build raw EDI ourselves. Self-serve BAA available on Stedi's site.

HIPAA requires EDI 270/271 for real-time eligibility between covered entities.
The practical bottleneck is clearinghouse trading-partner enrollment, not the
standard itself — Stedi handles enrollment with payer networks.

Priority payers (Tier 2):
  - Anthem/HealthKeepers VA  → Stedi payer ID: ANTHM
  - UnitedHealthcare         → Stedi payer ID: UHC
  - Sentara Health Plans     → Stedi payer ID: SNTARA

Other payers → manual_verification_required flag is set; no 270 is sent.
"""
import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from haloflow.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

STEDI_API_BASE = "https://healthcare.us.stedi.com/2024-04-01"


class EligibilityStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"
    PAYER_NOT_SUPPORTED = "payer_not_supported"
    ERROR = "error"


@dataclass
class EligibilityResult:
    payer_id: str
    payer_name: str
    status: EligibilityStatus
    member_id: str | None
    group_number: str | None
    plan_name: str | None
    coverage_begin: date | None
    coverage_end: date | None
    # Raw service type benefits for office visits (service type code 98)
    office_visit_copay: str | None
    raw_response: dict | None     # stored for audit trail; never returned to patient


class StediEligibilityClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=STEDI_API_BASE,
            headers={
                "Authorization": f"Key {settings.stedi_api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        stop=stop_after_attempt(3),
    )
    async def check_eligibility(
        self,
        *,
        payer_id: str,
        member_id: str,
        member_last_name: str,
        member_first_name: str,
        member_dob: date,
        service_date: date,
        npi: str,
        group_number: str | None = None,
    ) -> EligibilityResult:
        """
        Submit a 270 eligibility inquiry and parse the 271 response.

        Returns EligibilityResult with status=PAYER_NOT_SUPPORTED if
        the payer_id is not in the configured priority list (no 270 sent).
        """
        if payer_id not in settings.priority_payer_ids:
            logger.info(
                "Payer %s not in priority list — flagging for manual verification",
                payer_id,
            )
            return EligibilityResult(
                payer_id=payer_id,
                payer_name="",
                status=EligibilityStatus.PAYER_NOT_SUPPORTED,
                member_id=member_id,
                group_number=group_number,
                plan_name=None,
                coverage_begin=None,
                coverage_end=None,
                office_visit_copay=None,
                raw_response=None,
            )

        payload = {
            "controlNumber": _control_number(),
            "tradingPartnerServiceId": payer_id,
            "provider": {"npi": npi},
            "subscriber": {
                "memberId": member_id,
                "lastName": member_last_name,
                "firstName": member_first_name,
                "dateOfBirth": member_dob.strftime("%Y%m%d"),
                **({"groupNumber": group_number} if group_number else {}),
            },
            "encounter": {
                "serviceTypeCodes": ["30", "98"],   # 30=Health Benefit Plan, 98=Professional
                "dateOfService": service_date.strftime("%Y%m%d"),
            },
        }

        resp = await self._client.post(
            "/transactions/eligibility",
            json=payload,
        )

        if resp.status_code == 422:
            # Payer returned a rejection (not a transport error)
            logger.warning("Stedi 271 rejection for payer %s: %s", payer_id, resp.text)
            return EligibilityResult(
                payer_id=payer_id,
                payer_name="",
                status=EligibilityStatus.ERROR,
                member_id=member_id,
                group_number=group_number,
                plan_name=None,
                coverage_begin=None,
                coverage_end=None,
                office_visit_copay=None,
                raw_response=resp.json(),
            )

        resp.raise_for_status()
        return self._parse_271(payer_id, resp.json())

    def _parse_271(self, payer_id: str, raw: dict) -> EligibilityResult:
        """Parse Stedi's normalized 271 JSON response."""
        # Stedi returns a structured object; key paths below follow their schema.
        try:
            subscriber = raw.get("subscriber", {})
            benefits = raw.get("benefitsInformation", [])

            # Active coverage: look for benefit code "1" (Active Coverage)
            active = any(
                b.get("code") == "1" for b in benefits
            )
            inactive = any(
                b.get("code") == "6" for b in benefits  # 6 = Inactive
            )

            if active:
                status = EligibilityStatus.ACTIVE
            elif inactive:
                status = EligibilityStatus.INACTIVE
            else:
                status = EligibilityStatus.UNKNOWN

            # Office visit copay from service type 98
            copay_benefit = next(
                (b for b in benefits if "98" in b.get("serviceTypeCodes", [])), None
            )
            copay = None
            if copay_benefit:
                for info in copay_benefit.get("benefitAmount", []):
                    if info.get("code") == "B":   # B = Co-Payment
                        copay = f"${info.get('amount', '')}"

            # Date ranges
            coverage_begin: date | None = None
            coverage_end: date | None = None
            for benefit in benefits:
                for dr in benefit.get("benefitDateInformation", []):
                    if dr.get("code") == "346":   # 346 = Plan/Coverage begin
                        try:
                            coverage_begin = date.fromisoformat(dr["date"])
                        except (KeyError, ValueError):
                            pass
                    if dr.get("code") == "347":   # 347 = Plan/Coverage end
                        try:
                            coverage_end = date.fromisoformat(dr["date"])
                        except (KeyError, ValueError):
                            pass

            payer_info = raw.get("payer", {})
            plan_info = subscriber.get("healthCareCodeInformation", [{}])[0]

            return EligibilityResult(
                payer_id=payer_id,
                payer_name=payer_info.get("name", ""),
                status=status,
                member_id=subscriber.get("memberId"),
                group_number=subscriber.get("groupNumber"),
                plan_name=plan_info.get("diagnosisDescription"),
                coverage_begin=coverage_begin,
                coverage_end=coverage_end,
                office_visit_copay=copay,
                raw_response=raw,
            )
        except Exception:
            logger.exception("Error parsing 271 response for payer %s", payer_id)
            return EligibilityResult(
                payer_id=payer_id,
                payer_name="",
                status=EligibilityStatus.ERROR,
                member_id=None,
                group_number=None,
                plan_name=None,
                coverage_begin=None,
                coverage_end=None,
                office_visit_copay=None,
                raw_response=raw,
            )

    async def close(self) -> None:
        await self._client.aclose()


_control_seq = 0

def _control_number() -> str:
    """Generate a unique 9-digit control number for each 270 transaction."""
    global _control_seq
    _control_seq = (_control_seq + 1) % 1_000_000_000
    return str(_control_seq).zfill(9)
