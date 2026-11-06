"""L-1S package S-A: the reply / no-reply intake service (synthetic only).

`IntakeService` composes, in ONE gateway transaction per call (architecture v1
section 1, v1r1 section 5): the registration gateway (t039/t040/t041), the M02
observation append, and (for an item decision) the M05 create (t014). A failure
anywhere rolls all three back; a lost acknowledgement surfaces as
`IntakeCommitUnknown` and the identical call is the recovery (every append and
create input is a stored value, so the retry is idempotent).

Before any I/O: the principal must be a WORKLOAD and the context's capability set
must EQUAL the call's set. Tenant, patient, appointment and times never come from a
caller: the public commands carry none of them.

Nothing here sends a message or touches an EHR. NOT composed into production; the
S-A statements exist only in the S-A test catalogue.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, TypeVar
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import PrincipalKind, TenantContext
from haloflow.m02.observation import (
    AppendFailed,
    EventFoundation,
    Observation,
    TransactionBinding,
)
from haloflow.m05.intake_statements import CLOSE_KEY, DISPATCH_KEY, INBOUND_KEY

__all__ = [
    "CloseResult",
    "CommandMalformed",
    "DispatchCommand",
    "DispatchResult",
    "InboundCommand",
    "InboundResult",
    "IntakeCommitUnknown",
    "IntakeFailed",
    "IntakeRefused",
    "IntakeService",
]

T = TypeVar("T")

PRODUCER: Final = "m05_intake"
CREATE_KEY: Final = "m05.followup.create"
APPEND_CAPABILITY: Final = "m02.observation.append"
CAPABILITIES: Final = {
    DISPATCH_KEY: frozenset({DISPATCH_KEY, APPEND_CAPABILITY}),
    INBOUND_KEY: frozenset({INBOUND_KEY, APPEND_CAPABILITY, CREATE_KEY}),
    CLOSE_KEY: frozenset({CLOSE_KEY, APPEND_CAPABILITY, CREATE_KEY}),
}
T_DISPATCHED: Final = "m05_reminder_dispatched"
T_REPLY: Final = "m05_reminder_reply_received"
T_NO_REPLY: Final = "m05_reminder_no_reply"

_PHONE: Final = re.compile(r"\+[1-9][0-9]{7,14}")
_REPLY_CLASSES: Final = frozenset({"confirm", "reschedule", "cancel", "unclear", "stop"})


class CommandMalformed(ValueError):
    """A public command value is not acceptable (raised at construction)."""


class IntakeRefused(Exception):
    """Refused before any I/O (principal kind or capability set)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IntakeFailed(Exception):
    """A gateway refusal or an append failure, by code. The transaction rolled back."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IntakeCommitUnknown(Exception):
    """The transaction body completed but its commit outcome is unknown. Recovery is
    the identical call."""


@dataclass(frozen=True, slots=True)
class DispatchCommand:
    reminder_id: UUID
    appointment_ref: UUID

    def __post_init__(self) -> None:
        if type(self.reminder_id) is not UUID or type(self.appointment_ref) is not UUID:
            raise CommandMalformed("dispatch command")


@dataclass(frozen=True, slots=True)
class InboundCommand:
    channel: str
    provider_message_id: str
    phone: str
    reply_class: str

    def __post_init__(self) -> None:
        if (
            type(self.channel) is not str or self.channel != "sms"
            or type(self.provider_message_id) is not str
            or not 1 <= len(self.provider_message_id) <= 128
            or type(self.phone) is not str or not _PHONE.fullmatch(self.phone)
            or type(self.reply_class) is not str or self.reply_class not in _REPLY_CLASSES
        ):
            raise CommandMalformed("inbound command")


@dataclass(frozen=True, slots=True)
class DispatchResult:
    outcome: str
    reminder_id: UUID
    patient_id: UUID
    event_id: UUID


@dataclass(frozen=True, slots=True)
class InboundResult:
    outcome: str
    inbound_id: UUID
    decision: str
    triage_kind: str | None
    patient_id: UUID | None
    event_id: UUID | None
    item_id: UUID | None


@dataclass(frozen=True, slots=True)
class CloseResult:
    outcome: str
    decision: str | None
    event_id: UUID | None
    item_id: UUID | None


def _uuid(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


def _required_uuid(value: Any) -> UUID:
    result = _uuid(value)
    if result is None:
        raise IntakeFailed("RESULT_INVALID")
    return result


def _time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


class IntakeService:
    __slots__ = ("__foundation",)

    def __init__(self, foundation: EventFoundation) -> None:
        self.__foundation = foundation

    # -- public API -------------------------------------------------------------------

    async def register_dispatch(
        self, context: TenantContext, command: DispatchCommand
    ) -> DispatchResult:
        self._admit(context, DISPATCH_KEY)
        if type(command) is not DispatchCommand:
            raise CommandMalformed("dispatch command")

        async def body(binding: TransactionBinding) -> DispatchResult:
            row = await self._call(binding, DISPATCH_KEY, {
                "schema_version": 1,
                "reminder_id": str(command.reminder_id),
                "appointment_ref": str(command.appointment_ref),
                "attribution": _attribution(context),
            })
            reminder = _required_uuid(row["reminder_id"])
            patient = _required_uuid(row["patient_id"])
            event = _required_uuid(row["event_id"])
            await self._append(binding, Observation(
                event_id=event,
                event_type=T_DISPATCHED,
                contract_version=1,
                correlation_id=reminder,
                producer_dedupe_key=f"dispatch:{reminder.hex}",
                links=(),
                metadata={"reminder_id": str(reminder),
                          "appointment_ref": str(row["appointment_ref"])},
                patient_id=patient,
                occurred_at=_time(row["sent_at"]),
                source_time_quality="trusted",
            ))
            return DispatchResult(str(row["outcome"]), reminder, patient, event)

        return await self._run(context, body)

    async def register_inbound(
        self, context: TenantContext, command: InboundCommand
    ) -> InboundResult:
        self._admit(context, INBOUND_KEY)
        if type(command) is not InboundCommand:
            raise CommandMalformed("inbound command")

        async def body(binding: TransactionBinding) -> InboundResult:
            row = await self._call(binding, INBOUND_KEY, {
                "schema_version": 1,
                "channel": command.channel,
                "provider_message_id": command.provider_message_id,
                "phone": command.phone,
                "reply_class": command.reply_class,
                "attribution": _attribution(context),
            })
            inbound = _required_uuid(row["inbound_id"])
            decision = str(row["decision"])
            kind = row["triage_kind"]
            patient = _uuid(row["patient_id"])
            event = _uuid(row["event_id"])
            item: UUID | None = None
            if decision in ("recorded", "item"):
                if event is None or patient is None:
                    raise IntakeFailed("RESULT_INVALID")
                await self._append(binding, Observation(
                    event_id=event,
                    event_type=T_REPLY,
                    contract_version=1,
                    correlation_id=inbound,
                    producer_dedupe_key=f"reply:{inbound.hex}",
                    links=(),
                    metadata={
                        "inbound_id": str(inbound),
                        "reply_class": command.reply_class,
                        "triage_kind": "none" if kind is None else str(kind),
                        "appointment_ref": row["appointment_ref"],
                        "reminder_id": row["reminder_id"],
                    },
                    patient_id=patient,
                    occurred_at=_time(row["received_at"]),
                    source_time_quality="trusted",
                ))
            if decision == "item":
                if event is None:
                    raise IntakeFailed("RESULT_INVALID")
                item = await self._create(binding, context, event, str(kind))
            return InboundResult(str(row["outcome"]), inbound, decision,
                                 None if kind is None else str(kind), patient, event, item)

        return await self._run(context, body)

    async def close_window(self, context: TenantContext, reminder_id: UUID) -> CloseResult:
        self._admit(context, CLOSE_KEY)
        if type(reminder_id) is not UUID:
            raise CommandMalformed("reminder id")

        async def body(binding: TransactionBinding) -> CloseResult:
            row = await self._call(binding, CLOSE_KEY, {
                "schema_version": 1,
                "reminder_id": str(reminder_id),
                "attribution": _attribution(context),
            })
            decision = row["decision"]
            event = _uuid(row["event_id"])
            item: UUID | None = None
            if decision == "no_reply":
                if event is None:
                    raise IntakeFailed("RESULT_INVALID")
                await self._append(binding, Observation(
                    event_id=event,
                    event_type=T_NO_REPLY,
                    contract_version=1,
                    correlation_id=reminder_id,
                    producer_dedupe_key=f"noreply:{reminder_id.hex}",
                    links=(),
                    metadata={"reminder_id": str(reminder_id),
                              "appointment_ref": str(row["appointment_ref"]),
                              "triage_kind": "no_reply"},
                    patient_id=_required_uuid(row["patient_id"]),
                    occurred_at=_time(row["deadline"]),  # SA-EVENT-TIME-1: stored
                    source_time_quality="trusted",
                ))
                item = await self._create(binding, context, event, "no_reply")
            return CloseResult(str(row["outcome"]), None if decision is None else str(decision),
                               event, item)

        return await self._run(context, body)

    # -- internals ----------------------------------------------------------------------

    @staticmethod
    def _admit(context: TenantContext, key: str) -> None:
        if context.principal.kind is not PrincipalKind.WORKLOAD:
            raise IntakeRefused("PRINCIPAL_NOT_WORKLOAD")
        if context.capabilities != CAPABILITIES[key]:
            raise IntakeRefused("CAPABILITIES_INVALID")

    async def _run(
        self, context: TenantContext, body: Callable[[TransactionBinding], Awaitable[T]]
    ) -> T:
        complete = False

        async def marked(binding: TransactionBinding) -> T:
            nonlocal complete
            result = await body(binding)
            complete = True  # the final action of the body
            return result

        try:
            return await self.__foundation.run_in_transaction(context, marked)
        except (Exception, asyncio.CancelledError):
            # After the marker the commit outcome is unknown (the PA-05 rule);
            # KeyboardInterrupt and SystemExit always propagate.
            if not complete:
                raise
        raise IntakeCommitUnknown()

    @staticmethod
    async def _call(binding: TransactionBinding, key: str,
                    command: Mapping[str, Any]) -> Mapping[str, Any]:
        failure: str | None = None
        try:
            rows = await binding.handle.fetch_all(key, {"command": Jsonb(dict(command))})
        except psycopg.Error as error:
            failure = _code(error)
            rows = []
        if failure is not None:
            raise IntakeFailed(failure)
        if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
            raise IntakeFailed("RESULT_INVALID")
        result: Mapping[str, Any] = rows[0][0]
        return result

    async def _append(self, binding: TransactionBinding, observation: Observation) -> None:
        try:
            await self.__foundation.append_observation_in(binding, observation)
        except AppendFailed as error:
            raise IntakeFailed(error.reason_code) from None

    @staticmethod
    async def _create(binding: TransactionBinding, context: TenantContext, event: UUID,
                      kind: str) -> UUID:
        row = await IntakeService._call(binding, CREATE_KEY, {
            "schema_version": 1,
            "source_event_id": str(event),
            "item_kind": kind,
            "attribution": _attribution(context),
        })
        return _required_uuid(row["item_id"])


def _attribution(context: TenantContext) -> dict[str, Any]:
    principal = context.principal
    return {
        "principal_kind": principal.kind.value,
        "principal_id": principal.id,
        "auth_method": principal.auth_method,
        "execution_id": str(context.execution_id),
        "request_id": context.request_id,
        "purpose_code": context.purpose,
    }


def _code(error: psycopg.Error) -> str:
    diag = getattr(error, "diag", None)
    message = None if diag is None else diag.message_primary
    if error.sqlstate == "P0001" and isinstance(message, str) and re.fullmatch(
        r"[A-Z][A-Z_]{2,63}", message
    ):
        return message
    return "DATABASE_ERROR"
