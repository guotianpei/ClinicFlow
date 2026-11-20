"""Eligibility checks E2: service and worker. SYNTHETIC ONLY (not composed in production).

- `EligibilityService` runs one gateway transaction per call (the S-I service pattern) over
  the E2 statement keys, as a workload holding exactly the worker capability.
- `EligibilityWorker.run_one` is the R1 protocol for one verification: claim (fenced lease,
  or the takeover after lease + margin) -> dispatch (the attempt row is COMMITTED as
  `dispatched` before the port is called) -> ONE bounded port call -> record_answer (the
  normalized answer with the response's echo) | timeout -> dispatch again (attempt 2, or
  `retry_pending` while the delay runs) -> settle. An attempt id is sent at most once. Only an
  `Exception` from the port, or the call timeout, is a port failure; any other BaseException
  (cancellation, a process crash) propagates with nothing recorded.

Privacy (EL-14): member ids, names, dates of birth, response content and exception text are
never logged nor put in an exception raised here; logs carry outcome codes and exception CLASS
names only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, TypeVar
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import PrincipalKind, TenantContext
from haloflow.m02.observation import EventFoundation, TransactionBinding
from haloflow.m05.eligibility_results import normalize
from haloflow.m05.eligibility_statements import (
    ANSWER_KEY,
    CLAIM_KEY,
    DISPATCH_KEY,
    DUE_KEY,
    EXPIRE_KEY,
    SETTLE_KEY,
    TIMEOUT_KEY,
    WORKER_CAPABILITY,
)
from haloflow.m05.intake import _attribution, _code

__all__ = [
    "CAPABILITIES",
    "WORKER_OUTCOMES",
    "EligibilityFailed",
    "EligibilityRefused",
    "EligibilityService",
    "EligibilityWorker",
]

_LOG: Final = logging.getLogger("haloflow.m05.eligibility")
CAPABILITIES: Final = frozenset({WORKER_CAPABILITY})
WORKER_OUTCOMES: Final = ("settled", "retry_pending", "not_claimed", "fenced", "superseded",
                          "cancelled", "activation_off", "in_flight")
_STOP: Final = frozenset({"fenced", "superseded", "cancelled", "activation_off", "in_flight"})
T = TypeVar("T")


class EligibilityRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EligibilityFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _uuid_text(value: Any, name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        raise EligibilityRefused(f"{name.upper()}_INVALID") from None


def _epoch(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise EligibilityRefused("EPOCH_INVALID")
    return value


def _limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 1000:
        raise EligibilityRefused("LIMIT_INVALID")
    return value


class EligibilityService:
    __slots__ = ("__foundation",)

    def __init__(self, foundation: EventFoundation) -> None:
        # Injected (the IntakeService pattern); never constructed here.
        self.__foundation = foundation

    def __repr__(self) -> str:
        return "EligibilityService()"

    async def due(self, context: TenantContext, limit: int = 100) -> list[str]:
        row = await self._statement(context, DUE_KEY, {"limit": _limit(limit)})
        ids = row.get("verifications")
        if not isinstance(ids, list) or not all(type(i) is str for i in ids):
            raise EligibilityFailed("RESULT_INVALID")
        return [str(i) for i in ids]

    async def claim(self, context: TenantContext, verification_id: Any) -> dict[str, Any]:
        return await self._statement(context, CLAIM_KEY, {
            "verification_id": _uuid_text(verification_id, "verification_id")})

    async def dispatch(self, context: TenantContext, verification_id: Any,
                       epoch: int) -> dict[str, Any]:
        return await self._statement(context, DISPATCH_KEY, {
            "verification_id": _uuid_text(verification_id, "verification_id"),
            "epoch": _epoch(epoch)})

    async def record_answer(self, context: TenantContext, attempt_id: Any, epoch: int,
                            echo: Mapping[str, Any], result: Mapping[str, Any]
                            ) -> dict[str, Any]:
        return await self._statement(context, ANSWER_KEY, {
            "attempt_id": _uuid_text(attempt_id, "attempt_id"), "epoch": _epoch(epoch),
            "echo": dict(echo), "result": dict(result)})

    async def timeout(self, context: TenantContext, attempt_id: Any,
                      epoch: int) -> dict[str, Any]:
        return await self._statement(context, TIMEOUT_KEY, {
            "attempt_id": _uuid_text(attempt_id, "attempt_id"), "epoch": _epoch(epoch)})

    async def settle(self, context: TenantContext, verification_id: Any,
                     epoch: int) -> dict[str, Any]:
        return await self._statement(context, SETTLE_KEY, {
            "verification_id": _uuid_text(verification_id, "verification_id"),
            "epoch": _epoch(epoch)})

    async def expire(self, context: TenantContext, limit: int = 100) -> int:
        row = await self._statement(context, EXPIRE_KEY, {"limit": _limit(limit)})
        count = row.get("expired")
        if type(count) is not int or count < 0:
            raise EligibilityFailed("RESULT_INVALID")
        return count

    # -- internals ----------------------------------------------------------------------

    @staticmethod
    def _admit(context: TenantContext) -> None:
        if context.principal.kind is not PrincipalKind.WORKLOAD:
            raise EligibilityRefused("PRINCIPAL_NOT_WORKLOAD")
        if context.capabilities != CAPABILITIES:
            raise EligibilityRefused("CAPABILITIES_INVALID")

    async def _statement(self, context: TenantContext, key: str,
                         command: Mapping[str, Any]) -> dict[str, Any]:
        self._admit(context)

        async def body(binding: TransactionBinding) -> dict[str, Any]:
            payload = {"schema_version": 1, **command, "attribution": _attribution(context)}
            failure: str | None = None
            try:
                rows = await binding.handle.fetch_all(key, {"command": Jsonb(payload)})
            except psycopg.Error as error:
                failure = _code(error)
                rows = []
            if failure is not None:
                raise EligibilityFailed(failure)
            if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
                raise EligibilityFailed("RESULT_INVALID")
            return dict(rows[0][0])

        return await self._run(context, body)

    async def _run(self, context: TenantContext,
                   body: Callable[[TransactionBinding], Awaitable[T]]) -> T:
        return await self.__foundation.run_in_transaction(context, body)


class EligibilityWorker:
    """One verification per `run_one` (the R1 protocol)."""

    __slots__ = ("_port", "_service", "_timeout_s")

    def __init__(self, service: Any, port: Any, *, call_timeout_s: float = 10.0) -> None:
        if not call_timeout_s > 0:
            raise EligibilityRefused("TIMEOUT_INVALID")
        self._service = service
        self._port = port
        self._timeout_s = float(call_timeout_s)

    def __repr__(self) -> str:
        return "EligibilityWorker()"

    async def run_one(self, context: Any, verification_id: Any) -> str:
        claimed = await self._service.claim(context, verification_id)
        if claimed.get("outcome") != "claimed":
            _LOG.info("eligibility verification not claimed")
            return "not_claimed"
        epoch = claimed["epoch"]
        sent: set[str] = set()
        while True:
            got = await self._service.dispatch(context, verification_id, epoch)
            outcome = got.get("outcome")
            if outcome in ("answered", "exhausted"):
                return await self._settle(context, verification_id, epoch)
            if outcome == "retry_not_ready":
                _LOG.info("eligibility retry pending")
                return "retry_pending"
            if outcome in _STOP:
                _LOG.info("eligibility dispatch outcome=%s", outcome)
                return str(outcome)
            if outcome != "dispatched":
                _LOG.info("eligibility dispatch outcome=other")
                return "fenced"
            attempt = str(got["attempt_id"])
            if attempt in sent:                       # never the same attempt id twice
                _LOG.info("eligibility dispatch repeated an attempt id")
                return "fenced"
            sent.add(attempt)
            raw = await self._call(attempt, got.get("request"))
            if raw is None:
                await self._service.timeout(context, attempt, epoch)
                continue
            result = normalize(raw)
            echo = raw.get("echo") if isinstance(raw, Mapping) else None
            if not isinstance(echo, Mapping):
                echo = {}
            recorded = await self._service.record_answer(context, attempt, epoch,
                                                         _echo(echo), result)
            if recorded.get("outcome") != "recorded":
                _LOG.info("eligibility answer not recorded outcome=%s",
                          "late" if recorded.get("outcome") == "late" else "other")
                return "fenced"
            return await self._settle(context, verification_id, epoch)

    async def _call(self, attempt: str, request: Any) -> Any:
        """ONE bounded port call; any Exception or the timeout is None (a port failure)."""
        try:
            return await asyncio.wait_for(self._port.submit(attempt, request), self._timeout_s)
        except Exception as error:  # noqa: BLE001 - class name only (privacy)
            _LOG.info("eligibility port call failed error=%s", type(error).__name__)
            return None

    async def _settle(self, context: Any, verification_id: Any, epoch: int) -> str:
        settled = await self._service.settle(context, verification_id, epoch)
        outcome = settled.get("outcome")
        if outcome == "settled":
            return "settled"
        if outcome in _STOP:
            return str(outcome)
        _LOG.info("eligibility settle outcome=other")
        return "fenced"


_ECHO_KEYS: Final = ("attempt_id", "tenant_id", "appointment_ref", "patient_id", "member_id",
                     "coverage_version")


def _echo(echo: Mapping[str, Any]) -> dict[str, Any]:
    """The closed echo shape the gateway expects; a missing field is null (a mismatch)."""
    return {k: echo.get(k) for k in _ECHO_KEYS}
