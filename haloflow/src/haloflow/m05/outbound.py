"""L-1S package S-H, checkpoint H1: synthetic outbound send/recovery service and worker.

Governing: S-H decision table v1r2 (D1-D6), Codex pins 1-6, the H1 packet as corrected,
the versioned-ingress correction (target 56). Synthetic only. The only sender here is
`FakeSender`; this module imports no provider, HTTP or network client.

`OutboundService` runs one gateway transaction per call through the existing M02
`EventFoundation` (the S-A pattern): before any I/O the principal must be a WORKLOAD
and the context's capability set must EQUAL {m05.intake.dispatch, m02.observation.append}
(`CAPABILITIES_INVALID` otherwise, bookkeeping included). A lost acknowledgement surfaces
as `OutboundCommitUnknown`; every statement is idempotent for an identical retry.

`OutboundWorker.run_one` is the conservative one-attempt protocol (pin 5):
claim -> read (frozen snapshot, current unexpired attempt only) -> render and re-check
the prepared hash -> send ONCE -> record the outcome -> register. It never calls the
sender twice for one attempt, never for an uncertain op, and `recover` registers only.
It does NOT guarantee exactly-once external delivery or instantaneous revocation.

Privacy: phone numbers, names and message text are never logged, never put in an
exception this module raises, and never appear in a repr; logs carry codes only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol, TypeVar

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import PrincipalKind, TenantContext
from haloflow.m02.observation import (
    AppendFailed,
    EventFoundation,
    Observation,
    TransactionBinding,
)
from haloflow.m05.intake import PRODUCER, T_DISPATCHED, _attribution, _code
from haloflow.m05.intake_statements import DISPATCH_KEY
from haloflow.m05.outbound_statements import (
    CLAIM_KEY,
    EXPIRE_KEY,
    HELD_KEY,
    OUTCOME_KEY,
    PREPARE_KEY,
    READ_KEY,
    REGISTER_KEY,
)

__all__ = [
    "Accepted",
    "FakeSender",
    "OutboundCommitUnknown",
    "OutboundRefused",
    "OutboundService",
    "OutboundWorker",
    "Refused",
    "Sender",
    "Uncertain",
]

T = TypeVar("T")
_LOG: Final = logging.getLogger("haloflow.m05.outbound")
APPEND_CAPABILITY: Final = "m02.observation.append"
CAPABILITIES: Final = frozenset({DISPATCH_KEY, APPEND_CAPABILITY})
DEFAULT_LEASE_SECONDS: Final = 120


class OutboundRefused(Exception):
    """Refused before I/O, or by a gateway function (closed code). Rolled back."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutboundCommitUnknown(Exception):
    """The body completed but the commit outcome is unknown; recovery is the identical
    call."""


# --- sender port --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Accepted:
    """Submitted to the provider (never "delivered")."""

    provider_ref: Any


@dataclass(frozen=True, slots=True)
class Refused:
    """A definite non-send."""

    code: str


@dataclass(frozen=True, slots=True)
class Uncertain:
    code: str


SendResult = Accepted | Refused | Uncertain


class Sender(Protocol):
    async def send(self, op_id: str, phone: str, text: str) -> SendResult: ...


_UNSET: Final = object()


@dataclass(eq=False)
class FakeSender:
    """The only sender in H1. Records every call; `fail` simulates failures, `accepted_ref`
    overrides the evidence returned on acceptance (to exercise its validation)."""

    fail: str | None = None
    accepted_ref: Any = _UNSET
    calls: list[tuple[str, str, str]] = field(default_factory=list, repr=False)

    async def send(self, op_id: str, phone: str, text: str) -> SendResult:
        self.calls.append((op_id, phone, text))
        if self.fail == "error":
            raise RuntimeError("fake sender failure")
        if self.fail == "timeout":
            raise TimeoutError("fake sender timeout")
        if self.fail == "cancel":
            raise asyncio.CancelledError
        if self.fail == "leaky":  # adversarial: an error carrying patient data
            raise RuntimeError(f"provider rejected {phone}: {text}") from ValueError(text)
        ref = f"fake-{uuid.uuid4().hex[:16]}" if self.accepted_ref is _UNSET else self.accepted_ref
        return Accepted(ref)

    def __repr__(self) -> str:
        return f"FakeSender(fail={self.fail!r}, calls={len(self.calls)})"


# --- service ------------------------------------------------------------------------------


class OutboundService:
    __slots__ = ("__foundation",)

    def __init__(self, gateway: Any) -> None:
        self.__foundation = EventFoundation(gateway, producer_service=PRODUCER)

    def __repr__(self) -> str:
        return "OutboundService()"

    async def prepare(self, context: TenantContext, appointment_ref: Any, text_sha256: str,
                      *, op_id: Any, reminder_id: Any) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, PREPARE_KEY, {
            "op_id": str(op_id), "appointment_ref": str(appointment_ref),
            "reminder_id": str(reminder_id), "text_sha256": str(text_sha256)})

    async def claim(self, context: TenantContext, op_id: Any, fence: int,
                    lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, CLAIM_KEY, {
            "op_id": str(op_id), "fence": int(fence), "lease_seconds": int(lease_seconds)})

    async def read(self, context: TenantContext, op_id: Any, attempt_id: Any) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, READ_KEY, {
            "op_id": str(op_id), "attempt_id": str(attempt_id)})

    async def record_outcome(self, context: TenantContext, op_id: Any, attempt_id: Any,
                             outcome: str, **evidence: Any) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, OUTCOME_KEY, {
            "op_id": str(op_id), "attempt_id": str(attempt_id), "outcome": outcome,
            **evidence})

    async def expire(self, context: TenantContext, limit: int = 100) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, EXPIRE_KEY, {"limit": int(limit)})

    async def held(self, context: TenantContext, limit: int = 100) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, HELD_KEY, {"limit": int(limit)})

    async def register(self, context: TenantContext, op_id: Any) -> dict[str, Any]:
        self._admit(context)
        command = {"op_id": str(op_id)}

        async def body(binding: TransactionBinding) -> dict[str, Any]:
            row = await self._call(binding, context, REGISTER_KEY, command)
            if row.get("outcome") == "registered":  # the window was created now
                reminder = uuid.UUID(str(row["reminder_id"]))
                try:
                    await self.__foundation.append_observation_in(binding, Observation(
                        event_id=uuid.UUID(str(row["event_id"])),
                        event_type=T_DISPATCHED,
                        contract_version=1,
                        correlation_id=reminder,
                        producer_dedupe_key=f"dispatch:{reminder.hex}",
                        links=(),
                        metadata={"reminder_id": str(reminder),
                                  "appointment_ref": str(row["appointment_ref"])},
                        patient_id=uuid.UUID(str(row["patient_id"])),
                        occurred_at=datetime.fromisoformat(str(row["sent_at"])),
                        source_time_quality="trusted",
                    ))
                except AppendFailed as error:
                    raise OutboundRefused(error.reason_code) from None
            return row

        return await self._run(context, body)

    # -- internals --------------------------------------------------------------------------

    @staticmethod
    def _admit(context: TenantContext) -> None:
        if context.principal.kind is not PrincipalKind.WORKLOAD:
            raise OutboundRefused("PRINCIPAL_NOT_WORKLOAD")
        if context.capabilities != CAPABILITIES:
            raise OutboundRefused("CAPABILITIES_INVALID")

    async def _statement(self, context: TenantContext, key: str,
                         command: Mapping[str, Any]) -> dict[str, Any]:
        async def body(binding: TransactionBinding) -> dict[str, Any]:
            return await self._call(binding, context, key, command)

        return await self._run(context, body)

    async def _run(self, context: TenantContext,
                   body: Callable[[TransactionBinding], Awaitable[T]]) -> T:
        complete = False

        async def marked(binding: TransactionBinding) -> T:
            nonlocal complete
            result = await body(binding)
            complete = True
            return result

        try:
            return await self.__foundation.run_in_transaction(context, marked)
        except (Exception, asyncio.CancelledError):
            if not complete:
                raise
        raise OutboundCommitUnknown()

    @staticmethod
    async def _call(binding: TransactionBinding, context: TenantContext, key: str,
                    command: Mapping[str, Any]) -> dict[str, Any]:
        payload = {"schema_version": 1, **command, "attribution": _attribution(context)}
        failure: str | None = None
        try:
            rows = await binding.handle.fetch_all(key, {"command": Jsonb(payload)})
        except psycopg.Error as error:
            failure = _code(error)
            rows = []
        if failure is not None:
            raise OutboundRefused(failure)
        if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
            raise OutboundRefused("RESULT_INVALID")
        return dict(rows[0][0])


# --- worker -------------------------------------------------------------------------------


def _valid_ref(value: Any) -> bool:
    return type(value) is str and 1 <= len(value) <= 128


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OutboundWorker:
    """One conservative attempt per claimed op (pin 5). `render` builds the message from
    the frozen snapshot only."""

    __slots__ = ("_render", "_sender", "_service")

    def __init__(self, service: Any, sender: Sender,
                 render: Callable[[Mapping[str, Any]], str]) -> None:
        self._service = service
        self._sender = sender
        self._render = render

    def __repr__(self) -> str:
        return "OutboundWorker()"

    async def run_one(self, context: Any, op_id: Any, fence: int) -> str:
        try:
            claimed = await self._service.claim(context, op_id, fence)
        except OutboundRefused as refused:
            _LOG.info("outbound claim refused code=%s", refused.code)
            return "not_claimed"
        if claimed.get("outcome") != "claimed":
            _LOG.info("outbound op not claimed outcome=%s code=%s", claimed.get("outcome"),
                      claimed.get("code"))
            return str(claimed.get("outcome"))
        attempt = claimed["attempt_id"]
        try:  # the last claim check before the call (fresh database clock)
            snapshot = await self._service.read(context, op_id, attempt)
        except OutboundRefused as refused:
            _LOG.info("outbound claim lost before send code=%s", refused.code)
            return "claim_lost"  # no send; lease expiry records it uncertain
        text = self._render(snapshot)
        if _digest(text) != snapshot["text_sha256"]:
            await self._record(context, op_id, attempt, "refused", code="RENDER_CHANGED")
            return "refused"
        try:
            result = await self._sender.send(str(op_id), str(snapshot["phone"]), text)
        except asyncio.CancelledError:
            await self._record(context, op_id, attempt, "uncertain", code="SENDER_CANCELLED")
            raise
        except TimeoutError:
            await self._record(context, op_id, attempt, "uncertain", code="SENDER_TIMEOUT")
            return "uncertain"
        except Exception:  # noqa: BLE001 - never logged with its text (privacy)
            await self._record(context, op_id, attempt, "uncertain", code="SENDER_ERROR")
            return "uncertain"
        if isinstance(result, Accepted):
            if not _valid_ref(result.provider_ref):
                await self._record(context, op_id, attempt, "uncertain",
                                   code="ACCEPTED_EVIDENCE_INVALID")
                return "uncertain"
            await self._record(context, op_id, attempt, "accepted",
                               provider_ref=result.provider_ref)
            await self._register(context, op_id)
            return "accepted"
        if isinstance(result, Refused):
            await self._record(context, op_id, attempt, "refused", code=result.code)
            return "refused"
        if isinstance(result, Uncertain):
            await self._record(context, op_id, attempt, "uncertain", code=result.code)
            return "uncertain"
        await self._record(context, op_id, attempt, "uncertain", code="SENDER_RESULT_INVALID")
        return "uncertain"

    async def recover(self, context: Any, op_id: Any) -> Mapping[str, Any]:
        """Registration only: never claims, reads or sends."""

        return await self._register(context, op_id)

    async def _record(self, context: Any, op_id: Any, attempt: Any, outcome: str,
                      **evidence: Any) -> None:
        try:
            await self._service.record_outcome(context, op_id, attempt, outcome, **evidence)
        except OutboundCommitUnknown:
            await self._service.record_outcome(context, op_id, attempt, outcome, **evidence)

    async def _register(self, context: Any, op_id: Any) -> Mapping[str, Any]:
        try:
            result: Mapping[str, Any] = await self._service.register(context, op_id)
        except OutboundCommitUnknown:
            result = await self._service.register(context, op_id)
        return result
