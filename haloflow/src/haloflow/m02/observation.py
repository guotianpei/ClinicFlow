"""M02 observation append: the EventFoundation service layer (UC1 part A, G2).

Frozen interface (tests/m02/test_m02_part_a_service_postgres.py; G2 decision v1,
amended by Codex 1ee91aa6):

- `EventFoundation(gateway, *, producer_service)`. The producer is fixed by the
  route that constructs the foundation, never by the caller's payload (Delta2).
- `record_observation(context, observation, apply_state_change)`: exactly one
  `gateway.with_tenant_transaction`. Inside it the append runs, and only on
  `appended` the caller's `apply_state_change(handle, result)` runs, through the
  `_apply` seam. On `existing` the callback is not invoked.
- `run_in_transaction(context, body)` and `append_observation_in(binding,
  observation)`: one gateway boundary that a caller composes with its own writes
  (M05 in part B). The binding is frozen, cannot be built by a caller, belongs to
  one foundation, and expires with its transaction.

Attribution comes from the bound `TenantContext` (principal, execution id, request
id, purpose), never from the observation.

Error classification (G2 decision v1):
- A database error raised by M02's OWN append statement, before the body completes,
  becomes `AppendFailed(reason_code)`: fixed message, no cause, context suppressed,
  so no driver text or command value reaches the chain. The transaction rolled back.
- Exceptions raised by the caller's callback or body, and M01's own errors,
  propagate unchanged: the same object, never wrapped.
- Any `Exception` escaping the gateway AFTER the body completed (the body-completion
  marker) is `AppendCommitUnknown(event_id)`, whatever its type: M01's post-body
  path check, COMMIT itself, or connection loss after COMMIT was sent. This is
  conservative; an idempotent retry resolves it in either direction. Task
  cancellation (a BaseException) propagates unchanged.

Nothing here logs: the command carries metadata and identifiers (PA-18).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal, TypeVar
from uuid import UUID

from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb

from haloflow.m01.context import TenantContext
from haloflow.m01.errors import RepositoryHandleExpired
from haloflow.m01.gateway import TenantRepositoryHandle, TenantTransactionGateway
from haloflow.m02.statements import OBSERVATION_APPEND_KEY

__all__ = [
    "AppendCommitUnknown",
    "AppendFailed",
    "AppendResult",
    "BindingForeign",
    "EventFoundation",
    "Link",
    "Observation",
    "TransactionBinding",
]

T = TypeVar("T")

_COMMAND_SCHEMA_VERSION: Final = 1
_PRODUCER_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")

# P0001 refusals of `m02_append_observation`, by MESSAGE (the message IS the code).
_REFUSAL_CODES: Final = frozenset(
    {
        "COMMAND_MALFORMED",
        "CONTRACT_UNSUPPORTED",
        "PRODUCER_NOT_AUTHORIZED",
        "ATTRIBUTION_INVALID",
        "REQUIRED_LINK_MISSING",
        "LINK_INVALID",
        "LINK_UNSUPPORTED",
        "DEDUPE_CONFLICT",
        "ENVELOPE_UNSUPPORTED",
        "HANDOFF_KEY_COLLISION",
    }
)
# The tenant-binding refusals (decision TB-1) carry lower-case messages; they map
# to stable codes here.
_BINDING_CODES: Final = MappingProxyType(
    {
        "tenant binding absent": "TENANT_BINDING_ABSENT",
        "tenant binding inactive": "TENANT_BINDING_INACTIVE",
        "tenant binding mismatch": "TENANT_BINDING_MISMATCH",
    }
)
_TENANT_CONTEXT_INVALID: Final = "TENANT_CONTEXT_INVALID"  # SQLSTATE 22023
_APPEND_UNAVAILABLE: Final = "APPEND_UNAVAILABLE"  # any other database error
_RESULT_INVALID: Final = "APPEND_RESULT_INVALID"  # the function returned an unknown shape


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AppendFailed(Exception):
    """The append definitely did not commit. Carries a reason code only."""

    def __init__(self, reason_code: str) -> None:
        super().__init__("observation append failed")
        self.reason_code = reason_code

    def __repr__(self) -> str:
        return f"AppendFailed(reason_code={self.reason_code!r})"


class AppendCommitUnknown(Exception):
    """The body completed but the commit outcome is unknown. Retry idempotently."""

    def __init__(self, event_id: UUID) -> None:
        super().__init__("observation append commit outcome unknown")
        self.event_id = event_id

    def __repr__(self) -> str:
        return f"AppendCommitUnknown(event_id={self.event_id!r})"


class BindingForeign(Exception):
    """A transaction binding was issued by a different EventFoundation."""

    def __init__(self) -> None:
        super().__init__("transaction binding belongs to another foundation")


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Link:
    link_type: str
    link_id: UUID
    relationship: str


@dataclass(frozen=True, slots=True)
class Observation:
    """What the producer observed. No producer, attribution or tenant field (PRD)."""

    event_id: UUID
    event_type: str
    contract_version: int
    correlation_id: UUID
    producer_dedupe_key: str
    links: tuple[Link, ...]
    metadata: Mapping[str, Any]
    operation_id: UUID | None = None
    patient_id: UUID | None = None
    causation_event_id: UUID | None = None
    occurred_at: datetime | None = None
    source_time_quality: str = "absent"


@dataclass(frozen=True, slots=True)
class AppendResult:
    event_id: UUID
    appended_or_existing: Literal["appended", "existing"]
    handoff_state: str


class _Liveness:
    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


_BINDING_ISSUER: Final = object()


@dataclass(frozen=True, slots=True)
class TransactionBinding:
    """One gateway transaction, as issued by one EventFoundation. Not caller-built.

    Frozen: `context` and `handle` cannot be reassigned. It expires when its
    transaction ends; an expired binding is refused before any statement dispatch.
    """

    context: TenantContext
    handle: TenantRepositoryHandle
    _owner: object = field(repr=False)
    _liveness: _Liveness = field(repr=False)
    _issuer: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._issuer is not _BINDING_ISSUER or type(self._liveness) is not _Liveness:
            raise TypeError("TransactionBinding is issued by EventFoundation only")


# ---------------------------------------------------------------------------
# The foundation
# ---------------------------------------------------------------------------


class EventFoundation:
    __slots__ = ("__gateway", "__producer_service")

    def __init__(self, gateway: TenantTransactionGateway, *, producer_service: str) -> None:
        if type(producer_service) is not str or not _PRODUCER_PATTERN.fullmatch(
            producer_service
        ):
            raise ValueError("producer_service is not a valid producer identifier")
        self.__gateway = gateway
        self.__producer_service = producer_service

    # -- public API -----------------------------------------------------------

    async def record_observation(
        self,
        context: TenantContext,
        observation: Observation,
        apply_state_change: Callable[[TenantRepositoryHandle, AppendResult], Awaitable[None]],
    ) -> AppendResult:
        body_complete = False

        async def body(handle: TenantRepositoryHandle) -> AppendResult:
            nonlocal body_complete
            result = await self._append(handle, context, observation)
            if result.appended_or_existing == "appended":
                await self._apply(apply_state_change, handle, result)
            body_complete = True  # the marker: the final action of the body
            return result

        try:
            return await self.__gateway.with_tenant_transaction(context, body)
        except Exception:
            # Cancellation and other BaseExceptions are not caught: converting a
            # cancellation into a result would break the caller's task semantics.
            if not body_complete:
                raise
        # Raised outside the handler, so no exception is attached as context.
        raise AppendCommitUnknown(observation.event_id)

    async def run_in_transaction(
        self,
        context: TenantContext,
        body: Callable[[TransactionBinding], Awaitable[T]],
    ) -> T:
        liveness = _Liveness()

        async def inner(handle: TenantRepositoryHandle) -> T:
            binding = TransactionBinding(
                context, handle, self, liveness, _BINDING_ISSUER
            )
            try:
                return await body(binding)
            finally:
                liveness.active = False

        try:
            return await self.__gateway.with_tenant_transaction(context, inner)
        finally:
            liveness.active = False

    async def append_observation_in(
        self, binding: TransactionBinding, observation: Observation
    ) -> AppendResult:
        if type(binding) is not TransactionBinding or binding._owner is not self:
            raise BindingForeign()
        if not binding._liveness.active:
            raise RepositoryHandleExpired()
        return await self._append(binding.handle, binding.context, observation)

    # -- seams ------------------------------------------------------------------

    async def _apply(
        self,
        apply_state_change: Callable[[TenantRepositoryHandle, AppendResult], Awaitable[None]],
        handle: TenantRepositoryHandle,
        result: AppendResult,
    ) -> None:
        """The only place the caller's callback is invoked (PA-05b seam)."""

        await apply_state_change(handle, result)

    # -- internals --------------------------------------------------------------

    async def _append(
        self,
        handle: TenantRepositoryHandle,
        context: TenantContext,
        observation: Observation,
    ) -> AppendResult:
        command = self._command(context, observation)
        failure: str | None = None
        try:
            row = await handle.fetch_one(OBSERVATION_APPEND_KEY, {"command": Jsonb(command)})
        except DatabaseError as error:
            failure = _reason_for(error)
            row = None
        if failure is not None:
            # Outside the handler: no cause, no context, no driver text (PA-06c).
            raise AppendFailed(failure)
        result = _parse_result(row)
        if result is None:
            raise AppendFailed(_RESULT_INVALID)
        return result

    def _command(self, context: TenantContext, observation: Observation) -> dict[str, Any]:
        principal = context.principal
        return {
            "schema_version": _COMMAND_SCHEMA_VERSION,
            "event_id": _uuid_text(observation.event_id),
            "event_type": observation.event_type,
            "contract_version": observation.contract_version,
            "operation_id": _uuid_text(observation.operation_id),
            "patient_id": _uuid_text(observation.patient_id),
            "correlation_id": _uuid_text(observation.correlation_id),
            "causation_event_id": _uuid_text(observation.causation_event_id),
            "producer_dedupe_key": observation.producer_dedupe_key,
            "producer_service": self.__producer_service,
            "occurred_at": (
                None if observation.occurred_at is None else observation.occurred_at.isoformat()
            ),
            "source_time_quality": observation.source_time_quality,
            "links": [
                {
                    "link_type": link.link_type,
                    "link_id": _uuid_text(link.link_id),
                    "relationship": link.relationship,
                }
                for link in observation.links
            ],
            "metadata": dict(observation.metadata),
            "attribution": {
                "principal_kind": principal.kind.value,
                "principal_id": principal.id,
                "auth_method": principal.auth_method,
                "execution_id": str(context.execution_id),
                "request_id": context.request_id,
                "purpose_code": context.purpose,
            },
        }


def _uuid_text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _reason_for(error: DatabaseError) -> str:
    """A fixed code for a database error raised by the append statement itself."""

    sqlstate = getattr(error, "sqlstate", None)
    diag = getattr(error, "diag", None)
    message = getattr(diag, "message_primary", None)
    if sqlstate == "P0001" and isinstance(message, str):
        if message in _REFUSAL_CODES:
            return message
        if message in _BINDING_CODES:
            return _BINDING_CODES[message]
    if sqlstate == "22023":
        return _TENANT_CONTEXT_INVALID
    return _APPEND_UNAVAILABLE


def _parse_result(row: tuple[Any, ...] | None) -> AppendResult | None:
    if row is None or len(row) != 1 or not isinstance(row[0], dict):
        return None
    value = row[0]
    if set(value) != {"schema_version", "event_id", "appended_or_existing", "handoff_state"}:
        return None
    if value["schema_version"] != 1 or value["appended_or_existing"] not in (
        "appended",
        "existing",
    ):
        return None
    if not isinstance(value["event_id"], str) or not isinstance(value["handoff_state"], str):
        return None
    try:
        event_id = UUID(value["event_id"])
    except ValueError:
        return None
    return AppendResult(
        event_id=event_id,
        appended_or_existing=value["appended_or_existing"],
        handoff_state=value["handoff_state"],
    )
