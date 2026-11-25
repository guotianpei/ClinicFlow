"""Eligibility E5 (EL-12): simulated chart documentation service. SYNTHETIC ONLY.

Documents each settled eligibility result (a payer check) and each E1 manual routing as ONE
append-only entry in the SIMULATED chart (`haloflow.m05.simulated_chart`); never a real EHR.
Governing: E5 requirements v1r1 (Codex ALIGN fa5db003), architecture v1r1 (Codex ALIGN
3671b547) with the context-bound port clarification, test cases v1r1 (Codex ALIGN 9e6974f2),
owner decisions Q1-Q5 (2026-11-22) and OD-EL13 (2026-11-23). Pins: `tests/synthetic/
el5_support.py`.

`DocService.run_pass(ctx)` is the eligibility scheduler's `document` phase for one tenant
(after `apply`): at most OPS_PER_PASS ops, each claimed under a fenced lease (t131) in the
order manual_pending resumption -> needs_check confirmation reads -> reconciliation / delivery;
then the bounded enqueue (t129). The enqueue runs LAST, so the initial attempt of an op is on
the first pass after the one that enqueued it.

Delivery (A5.2): claim -> content (t130) -> `issue_permit` (bound to the event row's
appointment) -> durable sent marker at the current epoch (t132 `mark_sent`) -> `append`
(bounded) -> settle (t132, fenced on (event_id, epoch)). A refused-before-write append is a
definitive non-write: the permit is closed and the op waits for its retry (3 attempts in total
including the initial one). Any unknown outcome reconciles (read -> close -> read; 3 reads),
never re-appends. A matching entry settles only if its appointment is the event's AND its
permit is one this op issued; anything else goes to needs_check with ONE staff item
("Chart entry needs a check"), which the hourly read-only confirmation (at most 24 reads) or
the staff manual action (t134: close -> read -> settle) closes exactly once.

Logs carry codes and counts only; never an entry body, member id or reason label.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import Principal, PrincipalKind, TenantContext
from haloflow.m05 import simulated_chart as sc
from haloflow.m05.eligibility_case_units import SUMMARY_FIELDS
from haloflow.m05.eligibility_doc_rules import (
    APPEND_ATTEMPTS,
    CONFIRM_READ_INTERVAL_S,
    CONFIRM_READS_MAX,
    DOC_KIND,
    DOC_MANUAL_REASONS,
    DOC_TITLE,
    ENQUEUE_LIMIT,
    ENTRY_KEYS,
    OPS_PER_PASS,
    OUTCOME_CONFIRMED,
    OUTCOME_MANUAL,
    PORT_TIMEOUT_S,
    READ_DELAYS_S,
    RESULT_LABELS,
    RETRY_DELAYS_S,
    entry_text,
    render_entry,
    selection_sql,
)
from haloflow.m05.eligibility_statements import (
    DOC_CAPABILITY,
    DOC_CLAIM_KEY,
    DOC_ENQUEUE_KEY,
    DOC_ENTRY_KEY,
    DOC_MANUAL_CAPABILITY,
    DOC_MANUAL_KEY,
    DOC_RESUME_KEY,
    DOC_SETTLE_KEY,
)
from haloflow.m05.intake import _attribution, _code

__all__ = [
    "APPEND_ATTEMPTS", "CONFIRM_READS_MAX", "CONFIRM_READ_INTERVAL_S", "COUNT_KEYS",
    "DOC_CAPABILITIES", "DOC_KIND", "DOC_MANUAL_REASONS", "DOC_TITLE", "ENTRY_KEYS",
    "OPS_PER_PASS", "PORT_TIMEOUT_S", "READ_DELAYS_S", "RESULT_LABELS", "RETRY_DELAYS_S",
    "SUMMARY_FIELDS", "DocFailed", "DocRefused", "DocService", "SimulatedCrash",
    "entry_text", "render_entry", "selection_sql", "staff_context_for",
]

_LOG: Final = logging.getLogger(__name__)

DOC_CAPABILITIES: Final = frozenset({DOC_CAPABILITY})
STAFF_CAPABILITIES: Final = frozenset({DOC_MANUAL_CAPABILITY})
HOOKS: Final = ("after_claim", "after_mark_sent", "after_manual_phase1", "after_reconcile_read")
COUNT_KEYS: Final = ("enqueued", "appended", "retry_wait", "reconciling", "needs_check",
                     "entry_confirmed", "manual_documentation", "refused_mismatch",
                     "foreign_permit", "tenant_mismatch", "stale", "failed")
_REFUSAL_CODES: Final = frozenset({"ITEM_NOT_FOUND", "ITEM_CLOSED", "VERSION_STALE",
                                   "NOT_ASSIGNEE", "OP_NOT_NEEDS_CHECK", "COMMAND_MALFORMED"})


class SimulatedCrash(Exception):  # noqa: N818 - the pinned name
    """Raised by a test hook: the process "crashes" at that point; durable state is whatever
    committed before it. Never raised by the service itself."""


class DocRefused(Exception):
    """The staff manual action was refused before any state change, by code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DocFailed(Exception):
    """A database refusal or an unexpected result, by code (the transaction rolled back)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ForeignStaffContext:
    """An authorized-SHAPE staff context of another synthetic tenant (cross-tenant refusal
    rows only): the tenant, the actor principal and the manual capability, as a staff
    context of that tenant would carry them. Never resolver-issued; never usable for I/O."""

    tenant_id: str
    principal: Principal
    capabilities: frozenset[str]


def staff_context_for(tenant_id: str, staff_id: str) -> ForeignStaffContext:
    """The authorized-shape staff context of another (synthetic) tenant."""
    principal = Principal(kind=PrincipalKind.ACTOR, id=staff_id, auth_method="synthetic_staff",
                          authorized_tenant_ids=frozenset({tenant_id}),
                          capabilities=STAFF_CAPABILITIES)
    return ForeignStaffContext(tenant_id, principal, STAFF_CAPABILITIES)


class _PortTimeout(Exception):
    """A port call exceeded its bound (the call itself may still complete later)."""


def _entry_ref(read: object) -> dict[str, str] | None:
    if isinstance(read, Mapping):
        return {"appointment_ref": str(read.get("appointment_ref")),
                "permit_id": str(read.get("permit_id"))}
    return None


class DocService:
    """The E5 document phase and the staff manual action for the scheduled tenants."""

    def __init__(self, foundation: Any, chart: sc.SimulatedChart,
                 tenants: tuple[str, ...]) -> None:
        if not isinstance(chart, sc.SimulatedChart):
            raise DocFailed("CHART_INVALID")
        if type(tenants) is not tuple or not tenants:
            raise DocFailed("TENANTS_INVALID")
        self._foundation = foundation
        self._chart = chart
        self._tenants = tenants
        self.hooks: dict[str, Callable[[], Any]] = {}

    def __repr__(self) -> str:
        return "DocService()"

    @property
    def chart(self) -> sc.SimulatedChart:
        return self._chart

    # -- seams -------------------------------------------------------------------------------

    async def _hook(self, name: str) -> None:
        fn = self.hooks.get(name)
        if fn is None:
            return
        result = fn()
        if inspect.isawaitable(result):
            await result

    async def _port(self, call: Awaitable[Any]) -> Any:
        """One port call, bounded. A call that outlives its bound keeps running (a held
        append may still commit); the chart tracks it."""
        bound = self._chart.port_timeout_override
        task: asyncio.Task[Any] = asyncio.ensure_future(call)
        self._chart.track(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task),
                                          PORT_TIMEOUT_S if bound is None else bound)
        except TimeoutError:
            raise _PortTimeout() from None

    # -- database ----------------------------------------------------------------------------

    async def _call(self, context: Any, key: str, command: Mapping[str, Any]) -> dict[str, Any]:
        payload = {"schema_version": 1, **command, "attribution": _attribution(context)}

        async def body(binding: Any) -> dict[str, Any]:
            failure: str | None = None
            try:
                rows = await binding.handle.fetch_all(key, {"command": Jsonb(payload)})
            except psycopg.Error as error:
                failure = _code(error)
                rows = []
            if failure is not None:
                raise DocFailed(failure)
            if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
                raise DocFailed("RESULT_INVALID")
            return dict(rows[0][0])

        result: dict[str, Any] = await self._foundation.run_in_transaction(context, body)
        return result

    async def _settle(self, context: Any, op: Mapping[str, Any], result: str, *,
                      entry: Mapping[str, Any] | None = None,
                      permit_closed: bool = False) -> str:
        row = await self._call(context, DOC_SETTLE_KEY, {
            "event_id": op["event_id"], "epoch": op["epoch"], "result": result,
            "entry": None if entry is None else dict(entry), "permit_closed": permit_closed})
        return str(row.get("outcome"))

    # -- the document phase ------------------------------------------------------------------

    @staticmethod
    def _admit(context: Any) -> None:
        if not isinstance(context, TenantContext):
            raise DocFailed("CONTEXT_INVALID")
        if context.principal.kind is not PrincipalKind.WORKLOAD:
            raise DocFailed("PRINCIPAL_NOT_WORKLOAD")
        if context.capabilities != DOC_CAPABILITIES:
            raise DocFailed("CAPABILITIES_INVALID")

    async def run_pass(self, context: TenantContext) -> dict[str, int]:
        """One bounded document pass for the context's tenant (codes and counts only)."""
        self._admit(context)
        if context.tenant_id not in self._tenants:
            raise DocFailed("TENANT_MISMATCH")
        port = self._chart.for_tenant(context)
        counts = dict.fromkeys(COUNT_KEYS, 0)
        for _ in range(OPS_PER_PASS):
            op = await self._call(context, DOC_CLAIM_KEY, {})
            action = op.get("outcome")
            if action == "idle":
                break
            if action == "deliver":
                outcome = await self._deliver(context, port, op)
            elif action == "reconcile":
                outcome = await self._reconcile(context, port, op)
            elif action == "confirm":
                outcome = await self._confirm(context, port, op)
            elif action == "manual_resume":
                outcome = await self._resume(context, port, op)
            elif action == "exhausted":
                await self._close_quietly(port, op.get("permit_id"))
                outcome = "needs_check"
            elif action == "recovered":
                outcome = "reconciling"
            else:
                raise DocFailed("RESULT_INVALID")
            counts[outcome if outcome in counts else "failed"] += 1
        row = await self._call(context, DOC_ENQUEUE_KEY, {"limit": ENQUEUE_LIMIT})
        enqueued = row.get("enqueued")
        counts["enqueued"] = enqueued if type(enqueued) is int and enqueued >= 0 else 0
        _LOG.info("eligibility doc pass %s",
                  " ".join(f"{k}={v}" for k, v in counts.items() if v))
        return counts

    async def _close_quietly(self, port: sc.TenantChart, permit: object) -> object:
        if not isinstance(permit, str):
            return None
        try:
            return await self._port(port.close_permit(permit))
        except Exception:  # noqa: BLE001 - the close is a fence; a later close repeats it
            return "unknown"

    async def _deliver(self, context: Any, port: sc.TenantChart, op: Mapping[str, Any]) -> str:
        await self._hook("after_claim")
        content = await self._call(context, DOC_ENTRY_KEY, {"event_id": op["event_id"]})
        answer = content.get("answer")
        body = render_entry(content["event"], answer if isinstance(answer, Mapping) else None)
        permit = str(op["permit_id"])
        try:
            issued = await self._port(port.issue_permit(permit, str(op["event_id"]),
                                                        str(op["appointment_ref"])))
        except Exception:  # noqa: BLE001 - nothing was sent: a definitive non-write
            issued = None
        if issued in (sc.REFUSED_MISMATCH, sc.TENANT_MISMATCH):
            await self._settle(context, op, "mismatch")
            return "refused_mismatch" if issued == sc.REFUSED_MISMATCH else "tenant_mismatch"
        if issued != sc.ISSUED:
            closed = issued in (sc.PERMIT_CLOSED, sc.PERMIT_CONFLICT)
            return await self._settle(context, op, "refused", permit_closed=closed)
        try:
            await self._settle(context, op, "mark_sent", entry={"permit_id": permit})
        except DocFailed as failed:
            if failed.code == "EPOCH_STALE":
                return "stale"                       # no current-epoch marker: no append
            raise
        await self._hook("after_mark_sent")
        try:
            outcome = await self._port(port.append(permit, body))
        except sc.ChartRefused:
            closed = await self._close_quietly(port, permit) is None
            return await self._settle(context, op, "refused", permit_closed=closed)
        except Exception:  # noqa: BLE001 - timeout / lost acknowledgement: unknown
            return await self._settle(context, op, "unknown")
        if outcome == sc.APPENDED:
            return await self._settle(context, op, "appended")
        if outcome == sc.PERMIT_CLOSED:
            return await self._settle(context, op, "refused", permit_closed=True)
        if outcome in (sc.REFUSED_MISMATCH, sc.TENANT_MISMATCH):
            await self._settle(context, op, "mismatch")
            return "refused_mismatch" if outcome == sc.REFUSED_MISMATCH else "tenant_mismatch"
        # EXISTS / ALREADY_USED: something committed; only a read can say what (never a retry).
        return await self._settle(context, op, "unknown")

    async def _read(self, port: sc.TenantChart, op: Mapping[str, Any]) -> object:
        return await self._port(port.read(str(op["event_id"])))

    async def _reconcile(self, context: Any, port: sc.TenantChart,
                         op: Mapping[str, Any]) -> str:
        if await self._settle(context, op, "charge_read") == "exhausted":
            return "needs_check"
        try:
            found = await self._read(port, op)
        except Exception:  # noqa: BLE001 - timeout / unavailable: counted, retried later
            return await self._settle(context, op, "read_unavailable")
        await self._hook("after_reconcile_read")
        settled = await self._settle(context, op, "read", entry=_entry_ref(found))
        if settled != "absent":
            return settled
        closed = await self._close_quietly(port, op.get("permit_id"))
        if closed == sc.TENANT_MISMATCH:
            await self._settle(context, op, "mismatch")
            return "tenant_mismatch"
        if closed not in (None, sc.ALREADY_USED):
            return await self._settle(context, op, "close_unknown")
        if await self._settle(context, op, "charge_read") == "exhausted":
            return "needs_check"
        try:
            found = await self._read(port, op)
        except Exception:  # noqa: BLE001
            return await self._settle(context, op, "read_unavailable")
        settled = await self._settle(context, op, "read", entry=_entry_ref(found))
        if settled != "absent":
            return settled
        if closed == sc.ALREADY_USED:
            # A used permit means its append committed; an absent read is not final yet.
            return await self._settle(context, op, "close_unknown")
        return await self._settle(context, op, "definitive")

    async def _confirm(self, context: Any, port: sc.TenantChart, op: Mapping[str, Any]) -> str:
        try:
            found = await self._read(port, op)
        except Exception:  # noqa: BLE001
            await self._settle(context, op, "confirm_unavailable")
            return "needs_check"
        settled = await self._settle(context, op, "confirm", entry=_entry_ref(found))
        return settled if settled != "absent" else "needs_check"

    async def _close_and_read(self, port: sc.TenantChart, op: Mapping[str, Any]) -> object:
        closed = await self._close_quietly(port, op.get("permit_id"))
        if closed not in (None, sc.ALREADY_USED):
            raise DocFailed("CLOSE_UNKNOWN")
        try:
            return await self._read(port, op)
        except Exception:  # noqa: BLE001
            raise DocFailed("READ_UNAVAILABLE") from None

    async def _resume(self, context: Any, port: sc.TenantChart, op: Mapping[str, Any]) -> str:
        """A manual_pending op whose phase 2 never committed: close -> read -> phase 2 with the
        recorded attribution (idempotent)."""
        try:
            found = await self._close_and_read(port, op)
        except DocFailed:
            return "failed"                   # the lease expires; the next pass resumes again
        row = await self._call(context, DOC_RESUME_KEY, {
            "phase": 2, "event_id": op["event_id"], "epoch": op["epoch"],
            "entry": _entry_ref(found)})
        return str(row.get("outcome"))

    # -- the staff manual action (t134) --------------------------------------------------------

    async def manual(self, staff_ctx: Any, item_id: str, expected_version: int) -> str:
        """"Recorded in chart manually" on the OPEN doc item (the authorized entry point).
        Returns "entry_confirmed" | "manual_documentation"; refusals raise DocRefused with no
        state change."""
        if getattr(staff_ctx, "tenant_id", None) not in self._tenants:
            raise DocRefused("TENANT_MISMATCH")
        if not isinstance(staff_ctx, TenantContext):
            raise DocRefused("CONTEXT_INVALID")
        if staff_ctx.principal.kind is not PrincipalKind.ACTOR \
                or DOC_MANUAL_CAPABILITY not in staff_ctx.capabilities:
            raise DocRefused("CAPABILITY_REFUSED")
        if type(expected_version) is not int or type(item_id) is not str:
            raise DocRefused("COMMAND_MALFORMED")
        try:
            op = await self._call(staff_ctx, DOC_MANUAL_KEY, {
                "phase": 1, "item_id": item_id, "expected_version": expected_version,
                "staff_id": staff_ctx.principal.id})
        except DocFailed as failed:
            if failed.code in _REFUSAL_CODES:
                raise DocRefused(failed.code) from None
            raise
        await self._hook("after_manual_phase1")
        port = self._chart.for_tenant(staff_ctx)
        found = await self._close_and_read(port, op)
        row = await self._call(staff_ctx, DOC_MANUAL_KEY, {
            "phase": 2, "event_id": op["event_id"], "epoch": op["epoch"],
            "entry": _entry_ref(found)})
        outcome = row.get("outcome")
        if outcome not in (OUTCOME_CONFIRMED, OUTCOME_MANUAL):
            raise DocFailed("RESULT_INVALID")
        _LOG.info("eligibility doc manual outcome=%s", outcome)
        return str(outcome)
