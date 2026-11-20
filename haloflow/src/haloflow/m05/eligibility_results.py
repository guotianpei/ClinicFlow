"""Eligibility checks E2: pure rules (no I/O). SYNTHETIC ONLY.

Normalization of a simulated clearinghouse response (EL-7, EL-8), the freshness key (EL-5),
trigger termination (architecture v1r2 M1) and the Python mirrors of the four SQL seams.
Governing: E2 red packet v1r1 (Codex red gate PASS b690404c).

`inactive` is produced ONLY from an explicit "inactive" status (a positive statement); every
other non-active answer is `inconclusive`, and anything malformed is `inconclusive` /
`response_unreadable` with no other field. Unknown or absent fields stay absent; nothing is
guessed. Nothing here logs or raises with patient data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

__all__ = [
    "CLASSES",
    "EXPIRY_DAYS",
    "INCONCLUSIVE_REASONS",
    "LEASE_S",
    "MAX_ATTEMPTS",
    "RETRY_DELAY_S",
    "TAKEOVER_MARGIN_S",
    "TRANSIENT",
    "FreshKey",
    "expired",
    "is_fresh",
    "normalize",
    "retry_ready",
    "takeover_ready",
    "terminate",
]

CLASSES: Final = ("active", "inactive", "inconclusive")
INCONCLUSIVE_REASONS: Final = ("subscriber_not_found", "name_dob_mismatch",
                               "member_id_not_recognized", "service_type_unsupported",
                               "payer_unavailable", "response_unreadable")
PAYER_ERRORS: Final = INCONCLUSIVE_REASONS[:4]
TRANSIENT: Final = ("timeout", "unavailable")
RETRY_DELAY_S: Final = 120
LEASE_S: Final = 120
TAKEOVER_MARGIN_S: Final = 30
EXPIRY_DAYS: Final = 30
MAX_ATTEMPTS: Final = 2
SOURCE: Final = "simulated"

_BENEFIT_STRINGS: Final = ("payer_name", "plan", "copay", "deductible_total",
                           "deductible_remaining", "coinsurance")
_BENEFIT_BOOLEANS: Final = ("referral_required", "auth_required")
_MAX_TEXT: Final = 64
_UNREADABLE: Final[dict[str, Any]] = {"class": "inconclusive", "reason": "response_unreadable",
                                      "source": SOURCE}


class _Malformed(Exception):
    pass


def _observed(value: Any) -> str:
    if type(value) is not str:
        raise _Malformed
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _Malformed from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _Malformed
    return parsed.isoformat()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > _MAX_TEXT:
        raise _Malformed
    return value


def _flag(value: Any) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise _Malformed
    return value


def _normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _Malformed
    status = raw.get("status")
    if status == "active":
        out: dict[str, Any] = {"class": "active", "reason": "coverage_active"}
    elif status == "inactive":
        out = {"class": "inactive", "reason": "coverage_inactive"}
    elif status == "error":
        error = raw.get("error")
        if error not in PAYER_ERRORS:
            raise _Malformed
        out = {"class": "inconclusive", "reason": error}
    else:
        raise _Malformed
    out["observed_at"] = _observed(raw.get("observed_at"))
    out["source"] = SOURCE
    if status == "active":
        benefits = raw.get("benefits")
        if benefits is not None:
            if not isinstance(benefits, Mapping):
                raise _Malformed
            for name in _BENEFIT_STRINGS:
                value = _text(benefits.get(name))
                if value is not None:
                    out[name] = value
            for name in _BENEFIT_BOOLEANS:
                flag = _flag(benefits.get(name))
                if flag is not None:
                    out[name] = flag
        txn = _text(raw.get("txn_ref"))
        if txn is not None:
            out["txn_ref"] = txn
    return out


def normalize(raw: Any) -> dict[str, Any]:
    """The closed normalized structure (EL-7) of one raw simulated response."""
    try:
        return _normalize(raw)
    except _Malformed:
        return dict(_UNREADABLE)


@dataclass(frozen=True, slots=True)
class FreshKey:
    tenant: str
    appointment_ref: str
    patient_id: str
    payer_id: str
    member_digest: str
    coverage_version: int
    service_date: date

    def __repr__(self) -> str:
        return "FreshKey()"


def is_fresh(result_key: FreshKey, current_key: FreshKey, result_class: str,
             observed_at: datetime | None, window_start: datetime) -> bool:
    """EL-5: every key component equal, class active, observed_at at/after the window."""
    return (result_key == current_key and result_class == "active"
            and observed_at is not None and observed_at >= window_start)


def terminate(role: str, observed_at: datetime | None, window_start: datetime,
              returned: bool) -> str:
    """M1: the terminal state of a trigger when its verification settles."""
    if role == "owner":
        return "satisfied"
    if role != "attached":
        raise ValueError("role")
    if observed_at is None or observed_at >= window_start:
        return "satisfied"
    if returned:
        raise ValueError("an attached trigger is returned at most once")
    return "returned"


def retry_ready(failed_at: datetime, now: datetime) -> bool:
    return now >= failed_at + timedelta(seconds=RETRY_DELAY_S)


def takeover_ready(lease_until: datetime, now: datetime) -> bool:
    return now >= lease_until + timedelta(seconds=TAKEOVER_MARGIN_S)


def expired(service_date: date, today: date) -> bool:
    return today > service_date + timedelta(days=EXPIRY_DAYS)
