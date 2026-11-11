"""L-1S package S-I: synthetic EHR confirmation write-back (service, workers, staff v3).

Governing: S-I investigation v1 + v1r1 + v1r2, red-test packet v1 (Work Session
2026-11-10), Codex Option A and the red-test pins, owner decision C5 (2026-11-10: one
staff item kind plus a reason; R1-unavailable retries at 1 / 5 / 15 minutes). Synthetic
only: the only EHR is `haloflow.m05.simulated_ehr.SimulatedEhr`; nothing here sends to a
patient; NOT composed (the S-I statements exist only in the S-I TEST catalogue).

- `BaselineWorker` wraps the H2 `OutboundWorker.run_claimed`: the pre-consume baseline
  capture (T_cap) first, then ALWAYS the unchanged inner send. A capture failure never
  blocks or alters the send.
- `WritebackWorker.run_one` is the five-step protocol: claim (fenced lease) -> R1 ->
  decide (the WB2 matrix in SQL: permit, item, skip or defer) -> spend (one conditional
  UPDATE; the database sets apply_deadline) -> W1 (exactly one write, bounded) -> R2 ->
  settle (fenced). A spent, unsettled permit is reconcile-only, after its deadline plus
  margin in database time: the permit is CLOSED at the port before the reconciliation
  read. Every port call is bounded; cancellation always propagates.
- `WritebackService` runs one gateway transaction per call; an "item" decision or
  settlement appends the `m05_writeback_check_raised` observation and creates the staff
  item (kind `ehr_confirmation_check`) in that same transaction.
- `StaffServiceV3` is `M05Service` with resolution observations at contract version 2
  (every kind, including the new one) and the authorized reason read.

Privacy: phone numbers, names, provider and department references, appointment ids and
exception text are never logged and never put in an exception this module raises; logs
carry codes and exception CLASS names only.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Final, TypeVar
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import PrincipalKind, TenantContext
from haloflow.m01.staff import service as m01staff
from haloflow.m02.observation import (
    AppendFailed,
    EventFoundation,
    Link,
    Observation,
    TransactionBinding,
)
from haloflow.m05 import notes
from haloflow.m05 import service as staff
from haloflow.m05.intake import _attribution, _code
from haloflow.m05.intake_statements import DISPATCH_KEY
from haloflow.m05.outbound import OutboundWorker
from haloflow.m05.simulated_ehr import EhrRead
from haloflow.m05.writeback_statements import (
    CAPTURE_KEY,
    CLAIM_KEY,
    CLAIM_RECONCILE_KEY,
    CREATE_SETTLED_KEY,
    DECIDE_KEY,
    DISCOVER_KEY,
    DUE_KEY,
    REASON_KEY,
    SETTLE_KEY,
    SPEND_KEY,
    WRITEBACK_CAPABILITY,
)

__all__ = [
    "CAPABILITIES",
    "ITEM_KIND",
    "REASONS",
    "RECONCILE_MARGIN_S",
    "RETRY_DELAYS_S",
    "BaselineWorker",
    "StaffServiceV3",
    "WritebackCommitUnknown",
    "WritebackFailed",
    "WritebackRefused",
    "WritebackService",
    "WritebackWorker",
    "retry_delay_s",
    "worst_case_escalation_s",
]

T = TypeVar("T")
_LOG: Final = logging.getLogger("haloflow.m05.writeback")

RETRY_DELAYS_S: Final = (60, 300, 900)
REASONS: Final = ("status_changed", "declined_or_unknown", "ehr_unreachable",
                  "could_not_verify", "no_baseline")
ITEM_KIND: Final = "ehr_confirmation_check"
T_CHECK: Final = "m05_writeback_check_raised"
WRITE_TIMEOUT_S: Final = 10
RECONCILE_MARGIN_S: Final = 30
LEASE_S: Final = 60

APPEND_CAPABILITY: Final = "m02.observation.append"
CREATE_KEY: Final = "m05.followup.create"
CAPABILITIES: Final = frozenset({WRITEBACK_CAPABILITY, APPEND_CAPABILITY, CREATE_KEY})
CAPTURE_CAPABILITIES: Final = frozenset({DISPATCH_KEY, APPEND_CAPABILITY})
WRITE_OUTCOMES: Final = frozenset({"applied", "deadline_passed", "permit_closed",
                                   "not_found", "unknown"})


def retry_delay_s(failures: int) -> int | None:
    """The owner C5 schedule: the delay after the n-th consecutive R1 failure, or None
    when the n-th failure raises the staff item (ehr_unreachable)."""

    if type(failures) is not int or failures < 1:
        raise ValueError("failures")
    if failures <= len(RETRY_DELAYS_S):
        return RETRY_DELAYS_S[failures - 1]
    return None


def worst_case_escalation_s(interval_s: int, *, call_timeout_s: int = WRITE_TIMEOUT_S) -> int:
    """The C4 CONDITIONAL target (not a guarantee): the first tick after the reply, the
    three retry delays, each retry waiting up to one more tick, and four reads each
    failing at their timeout."""

    if type(interval_s) is not int or interval_s < 1:
        raise ValueError("interval_s")
    return interval_s + sum(RETRY_DELAYS_S) + 3 * interval_s + 4 * call_timeout_s


# --- errors -------------------------------------------------------------------------------


class WritebackRefused(Exception):
    """Refused before any I/O (principal, capability set, constructor input)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WritebackFailed(Exception):
    """A database refusal or an append failure, by code. The transaction rolled back."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class WritebackCommitUnknown(Exception):
    """The transaction body completed but its commit outcome is unknown. The persisted
    state decides what the next run does (nothing is retried here)."""


# --- BaselineWorker -----------------------------------------------------------------------


class BaselineWorker:
    """T_cap before the H2 one-shot send: capture, then ALWAYS the inner `run_claimed`."""

    __slots__ = ("_capture", "_inner")

    def __init__(self, inner: Any,
                 capture: Callable[[Any, Any, Any], Awaitable[Any]]) -> None:
        if not isinstance(inner, OutboundWorker):
            raise WritebackRefused("WORKER_INVALID")
        if not callable(capture):
            raise WritebackRefused("CAPTURE_INVALID")
        self._inner = inner
        self._capture = capture

    def __repr__(self) -> str:
        return "BaselineWorker()"

    async def run_claimed(self, context: Any, op_id: Any, attempt_id: Any) -> str:
        try:
            outcome = await self._capture(context, op_id, attempt_id)
        except Exception as error:  # noqa: BLE001 - logged by class name only (privacy)
            _LOG.info("writeback baseline not captured error=%s", type(error).__name__)
        else:
            if outcome != "captured":
                _LOG.info("writeback baseline not captured outcome=%s",
                          outcome if outcome in ("refused",) else "other")
        result: str = await self._inner.run_claimed(context, op_id, attempt_id)
        return result

    async def recover(self, context: Any, op_id: Any) -> Mapping[str, Any]:
        result: Mapping[str, Any] = await self._inner.recover(context, op_id)
        return result


# --- WritebackWorker ----------------------------------------------------------------------


def _deadline(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


class WritebackWorker:
    """One write-back op per `run_one` (the five-step protocol)."""

    __slots__ = ("_ehr", "_read_timeout_s", "_service", "_write_timeout_s")

    def __init__(self, service: Any, ehr: Any, *, write_timeout_s: float = 10.0,
                 read_timeout_s: float = 10.0) -> None:
        if write_timeout_s <= 0 or read_timeout_s <= 0:
            raise WritebackRefused("TIMEOUT_INVALID")
        self._service = service
        self._ehr = ehr
        self._write_timeout_s = float(write_timeout_s)
        self._read_timeout_s = float(read_timeout_s)

    def __repr__(self) -> str:
        return "WritebackWorker()"

    async def _read(self, appointment_id: str, stage: str) -> Any:
        """A bounded read: any failure or timeout is None (never retried here)."""

        try:
            return await asyncio.wait_for(self._ehr.read(appointment_id), self._read_timeout_s)
        except Exception as error:  # noqa: BLE001 - class name only (privacy)
            _LOG.info("writeback %s read failed error=%s", stage, type(error).__name__)
            return None

    async def run_one(self, context: Any, op_id: Any) -> str:
        try:
            claimed = await self._service.claim(context, op_id)
        except WritebackRefused as refused:
            _LOG.info("writeback claim refused code=%s", refused.code)
            return "not_claimed"
        if claimed.get("outcome") != "claimed":
            _LOG.info("writeback op not claimed outcome=%s", claimed.get("outcome"))
            return "not_claimed"
        fence = claimed["fence"]
        appointment = str(claimed["appointment_id"])
        if claimed.get("phase") == "reconcile":
            return await self._reconcile(context, op_id, fence, appointment,
                                         str(claimed["permit_id"]))
        return await self._check(context, op_id, fence, appointment)

    async def _check(self, context: Any, op_id: Any, fence: Any, appointment: str) -> str:
        r1 = await self._read(appointment, "r1")
        if r1 is not None and not isinstance(r1, EhrRead):
            r1 = None
        decided = await self._service.decide(context, op_id, fence, r1, r1 is None)
        outcome = decided.get("outcome")
        if outcome == "item":
            return "item_raised"
        if outcome in ("skipped", "deferred"):
            return str(outcome)
        if outcome != "permitted":
            _LOG.info("writeback decision outcome=%s", "stale" if outcome == "stale" else "other")
            return "stale"
        permit = str(decided["permit_id"])
        try:
            spent = await self._service.spend(context, op_id, fence, permit)
        except WritebackCommitUnknown:
            _LOG.info("writeback spend commit unknown; no write")
            return "spend_unknown"
        except WritebackRefused as refused:
            _LOG.info("writeback spend refused code=%s", refused.code)
            return "spend_refused"
        if spent.get("outcome") != "spent":
            _LOG.info("writeback spend refused code=%s", spent.get("code"))
            return "spend_refused"
        write_outcome = await self._write(appointment, permit, _deadline(spent["apply_deadline"]))
        r2 = await self._read(appointment, "r2")
        return await self._settle(context, op_id, fence, permit, write_outcome, r2)

    async def _write(self, appointment: str, permit: str, deadline: datetime) -> str:
        """W1: exactly one bounded call; a timeout or any failure is "unknown"."""

        try:
            result = await asyncio.wait_for(
                self._ehr.write_confirmation(appointment, permit_id=permit,
                                             apply_deadline=deadline),
                self._write_timeout_s)
        except Exception as error:  # noqa: BLE001 - class name only (privacy)
            _LOG.info("writeback write outcome unknown error=%s", type(error).__name__)
            return "unknown"
        outcome = getattr(result, "outcome", None)
        return outcome if outcome in WRITE_OUTCOMES else "unknown"

    async def _reconcile(self, context: Any, op_id: Any, fence: Any, appointment: str,
                         permit: str) -> str:
        try:
            await asyncio.wait_for(self._ehr.close_permit(permit), self._read_timeout_s)
        except Exception as error:  # noqa: BLE001 - class name only (privacy)
            _LOG.info("writeback reconcile close failed error=%s", type(error).__name__)
            return "reconcile_failed"
        r2 = await self._read(appointment, "reconcile")
        return await self._settle(context, op_id, fence, permit, "unknown", r2)

    async def _settle(self, context: Any, op_id: Any, fence: Any, permit: str,
                      write_outcome: str, read: Any) -> str:
        if read is not None and not isinstance(read, EhrRead):
            read = None
        try:
            settled = await self._service.settle(context, op_id, fence, permit, write_outcome,
                                                 read)
        except WritebackRefused as refused:
            _LOG.info("writeback settle refused code=%s", refused.code)
            return "stale"
        outcome = settled.get("outcome")
        if outcome == "observed_positive":
            return "observed_positive"
        if outcome == "item":
            return "item_raised"
        return "stale"


# --- WritebackService ---------------------------------------------------------------------


def _iso(value: datetime) -> str:
    from datetime import UTC

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_json(read: Any) -> Any:
    if read is None:
        return None
    if not isinstance(read, EhrRead):
        raise WritebackRefused("READ_INVALID")
    return {"status": read.status, "appointment_id": read.appointment_id,
            "start_at": _iso(read.start_at), "provider_ref": read.provider_ref,
            "department_ref": read.department_ref, "replaced_by": read.replaced_by}


def _uuid_text(value: Any, name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        raise WritebackRefused(f"{name.upper()}_INVALID") from None


def _fence(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise WritebackRefused("FENCE_INVALID")
    return value


class WritebackService:
    __slots__ = ("__foundation",)

    def __init__(self, foundation: EventFoundation) -> None:
        # Injected (the IntakeService pattern); never constructed here.
        self.__foundation = foundation

    def __repr__(self) -> str:
        return "WritebackService()"

    # -- public API -------------------------------------------------------------------

    async def capture_baseline(self, context: TenantContext, op_id: Any,
                               attempt_id: Any) -> str:
        self._admit(context, CAPTURE_CAPABILITIES)
        command = {"op_id": _uuid_text(op_id, "op_id"),
                   "attempt_id": _uuid_text(attempt_id, "attempt_id")}
        row = await self._statement(context, CAPTURE_KEY, command)
        return str(row.get("outcome"))

    async def discover(self, context: TenantContext, limit: int = 100) -> int:
        self._admit(context, CAPABILITIES)
        row = await self._statement(context, DISCOVER_KEY, {"limit": int(limit)})
        count = row.get("discovered")
        if type(count) is not int:
            raise WritebackFailed("RESULT_INVALID")
        return count

    async def due(self, context: TenantContext, limit: int = 100) -> list[dict[str, Any]]:
        self._admit(context, CAPABILITIES)
        row = await self._statement(context, DUE_KEY, {"limit": int(limit)})
        ops = row.get("ops")
        if type(ops) is not list:
            raise WritebackFailed("RESULT_INVALID")
        return [dict(op) for op in ops]

    async def claim(self, context: TenantContext, wb_op_id: Any) -> dict[str, Any]:
        """The protected check claim first; ONLY a spent-permit refusal or an activation
        refusal falls back to the unprotected reconcile claim."""

        from haloflow.m01.errors import ActivationRefused

        self._admit(context, CAPABILITIES)
        command = {"wb_op_id": _uuid_text(wb_op_id, "wb_op_id"), "lease_seconds": LEASE_S}
        try:
            row = await self._statement(context, CLAIM_KEY, command)
        except ActivationRefused:
            row = None
        if row is not None and not (row.get("outcome") == "not_claimable"
                                    and row.get("code") == "PERMIT_SPENT"):
            return self._claimed(row)
        return self._claimed(await self._statement(context, CLAIM_RECONCILE_KEY, command))

    async def decide(self, context: TenantContext, wb_op_id: Any, fence: int, read: Any,
                     read_failed: bool) -> dict[str, Any]:
        self._admit(context, CAPABILITIES)
        if type(read_failed) is not bool:
            raise WritebackRefused("READ_INVALID")
        command = {"wb_op_id": _uuid_text(wb_op_id, "wb_op_id"), "fence": _fence(fence),
                   "read": _read_json(read), "read_failed": read_failed}
        return await self._decision(context, DECIDE_KEY, command)

    async def spend(self, context: TenantContext, wb_op_id: Any, fence: int,
                    permit_id: Any) -> dict[str, Any]:
        self._admit(context, CAPABILITIES)
        command = {"wb_op_id": _uuid_text(wb_op_id, "wb_op_id"), "fence": _fence(fence),
                   "permit_id": _uuid_text(permit_id, "permit_id")}
        return await self._statement(context, SPEND_KEY, command)

    async def settle(self, context: TenantContext, wb_op_id: Any, fence: int, permit_id: Any,
                     write_outcome: str, read: Any) -> dict[str, Any]:
        self._admit(context, CAPABILITIES)
        if write_outcome not in WRITE_OUTCOMES:
            raise WritebackRefused("WRITE_OUTCOME_INVALID")
        command = {"wb_op_id": _uuid_text(wb_op_id, "wb_op_id"), "fence": _fence(fence),
                   "permit_id": _uuid_text(permit_id, "permit_id"),
                   "write_outcome": write_outcome, "read": _read_json(read)}
        return await self._decision(context, SETTLE_KEY, command)

    # -- internals ----------------------------------------------------------------------

    @staticmethod
    def _admit(context: TenantContext, capabilities: frozenset[str]) -> None:
        if context.principal.kind is not PrincipalKind.WORKLOAD:
            raise WritebackRefused("PRINCIPAL_NOT_WORKLOAD")
        if context.capabilities != capabilities:
            raise WritebackRefused("CAPABILITIES_INVALID")

    @staticmethod
    def _claimed(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        if result.get("outcome") == "claimed":
            result["appointment_id"] = result.get("appointment_ref")
        return result

    async def _statement(self, context: TenantContext, key: str,
                         command: Mapping[str, Any]) -> dict[str, Any]:
        async def body(binding: TransactionBinding) -> dict[str, Any]:
            return await self._call(binding, context, key, command)

        return await self._run(context, body)

    async def _decision(self, context: TenantContext, key: str,
                        command: Mapping[str, Any]) -> dict[str, Any]:
        """decide / settle: an "item" result appends the check observation and creates the
        staff item in the SAME transaction (a failure anywhere rolls all three back). A
        decision creates through the ordinary (protected) create; a settlement through the
        t094 recovery create bound to its own op, fence and spent permit."""

        async def body(binding: TransactionBinding) -> dict[str, Any]:
            row = await self._call(binding, context, key, command)
            if row.get("outcome") != "item":
                return row
            reason = row.get("reason")
            if reason not in REASONS:
                raise WritebackFailed("RESULT_INVALID")
            try:
                event = UUID(str(row["event_id"]))
                wb_op = UUID(str(row["wb_op_id"]))
                patient = UUID(str(row["patient_id"]))
                appointment = str(UUID(str(row["appointment_ref"])))
                occurred = datetime.fromisoformat(str(row["occurred_at"]))
            except (KeyError, TypeError, ValueError):
                raise WritebackFailed("RESULT_INVALID") from None
            try:
                await self.__foundation.append_observation_in(binding, Observation(
                    event_id=event,
                    event_type=T_CHECK,
                    contract_version=1,
                    correlation_id=wb_op,
                    producer_dedupe_key=f"wbcheck:{wb_op.hex}",
                    links=(),
                    metadata={"wb_op_id": str(wb_op), "appointment_ref": appointment,
                              "reason": str(reason)},
                    patient_id=patient,
                    occurred_at=occurred,
                    source_time_quality="trusted",
                ))
            except AppendFailed as error:
                raise WritebackFailed(error.reason_code) from None
            if key == SETTLE_KEY:  # the recovery create (t094), bound to this settlement
                created = await self._call(binding, context, CREATE_SETTLED_KEY, {
                    "wb_op_id": command["wb_op_id"], "fence": command["fence"],
                    "permit_id": command["permit_id"], "source_event_id": str(event),
                    "item_kind": ITEM_KIND})
            else:
                created = await self._call(binding, context, CREATE_KEY, {
                    "source_event_id": str(event), "item_kind": ITEM_KIND})
            try:
                item = str(UUID(str(created["item_id"])))
            except (KeyError, TypeError, ValueError):
                raise WritebackFailed("RESULT_INVALID") from None
            return {"schema_version": 1, "outcome": "item", "reason": reason,
                    "event_id": str(event), "item_id": item, "wb_op_id": str(wb_op)}

        return await self._run(context, body)

    async def _run(self, context: TenantContext,
                   body: Callable[[TransactionBinding], Awaitable[T]]) -> T:
        complete = False

        async def marked(binding: TransactionBinding) -> T:
            nonlocal complete
            result = await body(binding)
            complete = True  # the final action of the body
            return result

        try:
            return await self.__foundation.run_in_transaction(context, marked)
        except (Exception, asyncio.CancelledError):
            if not complete:
                raise
        raise WritebackCommitUnknown()

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
            raise WritebackFailed(failure)
        if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
            raise WritebackFailed("RESULT_INVALID")
        return dict(rows[0][0])


# --- StaffServiceV3 -----------------------------------------------------------------------

ITEM_KINDS_V3: Final = frozenset({*staff.ITEM_KINDS, ITEM_KIND})
RESOLUTION_CONTRACT_VERSION: Final = 2


def _valid_item_v3(view: Any) -> bool:
    """`service._valid_item` with the five S-I kinds."""

    if type(view) is not dict or not m01staff.member(view.get("item_kind"), ITEM_KINDS_V3):
        return False
    shadow = {**view, "item_kind": next(iter(sorted(staff.ITEM_KINDS)))}
    return bool(staff._valid_item(shadow))


def _bind_item_v3(rows: list[Any], item_id: UUID) -> dict[str, Any]:
    view = m01staff.single_json(rows)
    if not _valid_item_v3(view) or view["item_id"] != str(item_id):
        raise staff._read_invalid()
    return view  # type: ignore[no-any-return]


def _bind_list_v3(rows: list[Any], query: staff.ListQuery) -> dict[str, Any]:
    value = m01staff.single_json(rows)
    if type(value) is dict and type(value.get("items")) is list:
        for view in value["items"]:
            if not _valid_item_v3(view):
                raise staff._read_invalid()
        shadowed = {**value, "items": [
            {**view, "item_kind": next(iter(sorted(staff.ITEM_KINDS)))}
            for view in value["items"]]}
        staff._bind_list([(shadowed,)], query)
        return value
    raise staff._read_invalid()


def _resolve_decision_v3(rows: list[Any], action: staff.ResolveAction, actor: str, *,
                         second: bool) -> tuple[str, Any]:
    """`service._resolve_decision` accepting the five S-I kinds on NEW."""

    value = m01staff.single_json(rows)
    if (not second and type(value) is dict and value.get("decision") == "NEW"
            and type(value.get("item")) is dict
            and value["item"].get("item_kind") == ITEM_KIND):
        shadow = {**value, "item": {**value["item"],
                                    "item_kind": next(iter(sorted(staff.ITEM_KINDS)))}}
        _decision, _item = staff._resolve_decision([(shadow,)], action, actor, second=second)
        return "NEW", value["item"]
    return staff._resolve_decision(rows, action, actor, second=second)


def _reason_error(error: psycopg.Error) -> Exception:
    """The reason read's classification (pin: ReadFailed ITEM_NOT_FOUND for an unknown,
    other-tenant, other-kind or unbound item); otherwise the staff read rule."""

    sqlstate, message = m01staff.sqlstate_and_message(error)
    if sqlstate == "P0001" and message == "ITEM_NOT_FOUND":
        return staff.ReadFailed("ITEM_NOT_FOUND")
    return m01staff.read_error(error)


class StaffServiceV3(staff.M05Service):
    """`M05Service` with the resolution observation at contract version 2 (every kind),
    the fifth item kind accepted, and `reason(context, item_id)`. The production
    `M05Service` and every production binding are unchanged."""

    async def get(self, context: Any, item_id: UUID) -> Mapping[str, Any]:
        if not m01staff.is_uuid(item_id):
            raise staff.CommandMalformed("item_id")
        return await self._read(context, staff.K_GET, {"item_id": item_id},
                                lambda rows: _bind_item_v3(rows, item_id))

    async def list_items(self, context: Any, query: staff.ListQuery) -> Mapping[str, Any]:
        if type(query) is not staff.ListQuery:
            raise staff.CommandMalformed("query")
        return await self._read(context, staff.K_LIST, {"query": Jsonb(query.render())},
                                lambda rows: _bind_list_v3(rows, query))

    async def reason(self, context: Any, item_id: UUID) -> dict[str, Any]:
        if not m01staff.is_uuid(item_id):
            raise staff.CommandMalformed("item_id")

        def bind(rows: list[Any]) -> dict[str, Any]:
            value = m01staff.single_json(rows)
            if (type(value) is not dict
                    or set(value) != {"schema_version", "item_id", "reason"}
                    or value["schema_version"] != 1 or value["item_id"] != str(item_id)
                    or value["reason"] not in REASONS):
                raise staff._read_invalid()
            return value

        m01staff.require_capabilities(context, staff.READ_ROUTE)
        attribution = m01staff.actor_attribution(context)
        if attribution is None:
            raise staff.ReadRefused("NOT_AUTHORIZED")

        async def callback(handle: Any) -> dict[str, Any]:
            await m01staff.check_reader(handle, attribution["principal_id"])
            return bind(await m01staff.fetch_rows(handle, REASON_KEY, {"item_id": item_id},
                                               _reason_error))

        result: dict[str, Any] = await self._gateway.with_tenant_transaction(context, callback)
        return result

    async def _resolve(self, context: Any, action: staff.ResolveAction) -> staff.ActionResult:
        """`M05Service._resolve` with the v3 decision binder (identical flow)."""

        m01staff.require_capabilities(context, staff.RESOLVE_ROUTE)
        attribution = m01staff.actor_attribution(context)
        if attribution is None:
            raise staff.ActionRefused("NOT_AUTHORIZED")
        if self._foundation is None:
            raise staff.ActionFailed("ACTION_UNAVAILABLE")
        actor = attribution["principal_id"]
        tenant = context.tenant_id
        caps = context.capabilities
        base = {
            "schema_version": 1,
            "item_id": str(action.item_id),
            "client_action_id": str(action.client_action_id),
            "expected_version": action.expected_version,
            "permissions": {"read": m01staff.READ_CAPABILITY in caps,
                            "act": staff.APPLY_CAPABILITY in caps,
                            "supervise": staff.SUPERVISE_CAPABILITY in caps},
            "attribution": attribution,
        }

        def command(operation: dict[str, Any]) -> Jsonb:
            return Jsonb({**base, "action": {"type": action.type,
                                             "outcome_code": action.outcome_code,
                                             **operation}})

        async def replay(handle: Any, note: dict[str, Any] | None) -> staff.ActionResult:
            rows = await m01staff.fetch_rows(handle, staff.K_APPLY, {"command": command(
                {"note": note, "payload": None})}, staff._action_error)
            return staff._resolve_applied(rows, action, actor, "replayed")

        async def body(binding: Any, mark: Callable[[], None]) -> staff.ActionResult:
            handle = binding.handle
            first = await m01staff.fetch_rows(handle, staff.K_PREPARE, {"command": command(
                {"note_present": action.note is not None})}, staff._action_error)
            decision, detail = _resolve_decision_v3(first, action, actor, second=False)
            if decision == "REPLAY":
                result = await replay(handle, None)
            elif decision == "REPLAY_CHALLENGE":
                key = self._key(lambda: self._keys.hmac_key(detail),
                                lambda: staff.ActionRefused("REPLAY_UNVERIFIABLE"))
                note = {"note_hmac": notes.note_hmac(key, tenant, action.note or ""),
                        "hmac_key_version": detail}
                second = await m01staff.fetch_rows(handle, staff.K_PREPARE, {"command": command(
                    {"note_present": True, "note": note})}, staff._action_error)
                _resolve_decision_v3(second, action, actor, second=True)
                result = await replay(handle, note)
            else:
                result = await self._resolve_new(binding, handle, action, actor, tenant,
                                                  context, detail, command)
            mark()
            return result

        state = {"complete": False}

        def mark() -> None:
            state["complete"] = True

        async def inner(binding: Any) -> staff.ActionResult:
            return await body(binding, mark)

        try:
            result: staff.ActionResult = await self._foundation.run_in_transaction(context,
                                                                                   inner)
            return result
        except (Exception, asyncio.CancelledError):
            if not state["complete"]:
                raise
        raise staff.ActionCommitUnknown(action.item_id, action.client_action_id)

    async def _resolve_new(self, binding: Any, handle: Any, action: staff.ResolveAction,
                           actor: str, tenant: str, context: Any, item: dict[str, Any],
                           command: Callable[[dict[str, Any]], Jsonb]) -> staff.ActionResult:
        """`M05Service._resolve_new` with the observation at contract version 2."""

        note: dict[str, Any] | None = None
        note_row: dict[str, Any] | None = None
        if action.note is not None:
            def unavailable() -> Exception:
                return staff.ActionFailed("KEY_UNAVAILABLE")

            version, mac_key = self._key(lambda: self._keys.current_hmac(), unavailable)
            enc_version, enc_key = self._key(lambda: self._keys.current_encryption(),
                                             unavailable)
            note = {"note_hmac": notes.note_hmac(mac_key, tenant, action.note),
                    "hmac_key_version": version}
            sealed = notes.encrypt_note(enc_key, enc_version, tenant, action.item_id,
                                        action.client_action_id, action.note)
            note_row = {"note_id": str(sealed.note_id), "nonce": sealed.nonce.hex(),
                        "ciphertext": sealed.ciphertext.hex(),
                        "enc_key_version": sealed.enc_key_version}
        event_id = uuid.uuid4()
        observation = Observation(
            event_id=event_id,
            event_type=staff.RESOLUTION_TYPE,
            contract_version=RESOLUTION_CONTRACT_VERSION,
            correlation_id=context.correlation_id,
            producer_dedupe_key=staff._resolution_dedupe_key(tenant, action),
            links=(Link(staff.ITEM_LINK_TYPE, action.item_id, "case"),),
            metadata={"outcome_code": action.outcome_code, "assertion_kind": "staff",
                      "item_kind": item["item_kind"]},
            patient_id=UUID(item["patient_id"]),
            causation_event_id=UUID(item["source_event_id"]),
            occurred_at=None,
            source_time_quality="absent",
        )
        appended = await self._foundation.append_observation_in(binding, observation)
        if appended.appended_or_existing != "appended":
            _LOG.warning("m05 resolve refused: orphan resolution event")
            raise staff.ActionFailed("ORPHAN_RESOLUTION_EVENT")
        if appended.event_id != event_id:
            raise staff._invalid()
        rows = await m01staff.fetch_rows(handle, staff.K_APPLY, {"command": command(
            {"note": note, "payload": {"event_id": str(event_id), "note_row": note_row}})},
            staff._action_error)
        return staff._resolve_applied(rows, action, actor, "applied")
