"""Eligibility checks E2: the SYNTHETIC clearinghouse port (EL-6). SYNTHETIC ONLY.

An in-memory, deterministic stand-in for a clearinghouse: a scenario table keyed by member
id, an injected clock for the observation time, and fault injection (unavailable, malformed).
It exchanges a simulated structured request/response, which is NOT X12 270/271
interoperability evidence. It imports no HTTP or network client and is composed nowhere in
production. An unknown member answers `error` / `subscriber_not_found`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

__all__ = ["STATUSES", "ClearinghouseUnavailable", "SimulatedClearinghouse"]

STATUSES: Final = ("active", "inactive", "error")
_ERRORS: Final = ("subscriber_not_found", "name_dob_mismatch", "member_id_not_recognized",
                  "service_type_unsupported")
_ECHO: Final = ("attempt_id", "tenant_id", "appointment_ref", "patient_id", "member_id",
                "coverage_version")


class ClearinghouseUnavailable(Exception):
    """The synthetic clearinghouse could not be reached (carries no patient data)."""


class SimulatedClearinghouse:
    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._members: dict[str, tuple[str, str | None, dict[str, Any] | None]] = {}
        self._fail = 0
        self._malformed = 0
        self._serial = 0
        self.lock = asyncio.Lock()
        self.calls: list[str] = []

    def __repr__(self) -> str:
        return f"SimulatedClearinghouse(members={len(self._members)}, calls={len(self.calls)})"

    # -- scenario table and fault injection ------------------------------------------------

    def set_member(self, member_id: str, status: str, *, error: str | None = None,
                   benefits: Mapping[str, Any] | None = None) -> None:
        if type(member_id) is not str or not member_id:
            raise ValueError("member_id")
        if status not in STATUSES:
            raise ValueError("status")
        if (status == "error") != (error is not None) or (error is not None
                                                          and error not in _ERRORS):
            raise ValueError("error")
        self._members[member_id] = (status, error, None if benefits is None else dict(benefits))

    def fail_next(self, n: int) -> None:
        self._fail = max(0, int(n))

    def malformed_next(self, n: int) -> None:
        self._malformed = max(0, int(n))

    # -- port ----------------------------------------------------------------------------------

    async def submit(self, attempt_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        if type(attempt_id) is not str or not attempt_id:
            raise ValueError("attempt_id")
        if not isinstance(request, Mapping):
            raise ValueError("request")
        async with self.lock:
            self.calls.append(attempt_id)
            if self._fail > 0:
                self._fail -= 1
                raise ClearinghouseUnavailable("unavailable")
            if self._malformed > 0:
                self._malformed -= 1
                return {"attempt_id": attempt_id, "status": "???"}
            observed = self._clock()
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError("clock")
            self._serial += 1
            echo = {k: request.get(k) for k in _ECHO}
            echo["attempt_id"] = attempt_id
            status, error, benefits = self._members.get(
                str(request.get("member_id")), ("error", "subscriber_not_found", None))
            answer: dict[str, Any] = {
                "attempt_id": attempt_id, "echo": echo, "status": status,
                "observed_at": observed.astimezone(UTC).isoformat(),
                "txn_ref": f"SYN-TXN-{self._serial:06d}"}
            if error is not None:
                answer["error"] = error
            if benefits is not None:
                answer["benefits"] = dict(benefits)
            return answer
