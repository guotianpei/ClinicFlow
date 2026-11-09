"""L-1S package S-H, checkpoint H2: the reminder service and runner (synthetic; not
composed).

`ReminderService` is an `OutboundService` (same admission: a WORKLOAD principal whose
capability set EQUALS {m05.intake.dispatch, m02.observation.append}, refused before I/O
otherwise) adding the H2 statements. `ReminderRunner.run_once` is one bounded pass:
ready (no lock) -> per ref: render_snapshot -> render here (Q-B4) -> examine (one
transaction) -> for a claimed op, `OutboundWorker.run_claimed`. No lock is held between
refs or across a sender call. A commit-unknown examine never sends (the lease expires to
uncertain). Privacy: logs carry codes only.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from haloflow.m01.context import TenantContext
from haloflow.m05.outbound import OutboundCommitUnknown, OutboundRefused, OutboundService
from haloflow.m05.reminder_statements import (
    CLAIM_V2_KEY,
    CONSUME_KEY,
    EXAMINE_KEY,
    READ_V2_KEY,
    READY_KEY,
    SNAPSHOT_KEY,
)
from haloflow.m05.reminder_text import reminder_text

__all__ = ["ReminderRunner", "ReminderService", "RunReport", "render_inputs", "snapshot_hash"]

_LOG: Final = logging.getLogger("haloflow.m05.reminder")
_COMPLETED: Final = frozenset({"claimed", "deferred", "skipped", "removed"})


def render_inputs(inputs: Any) -> str:
    """The Q-B4 text from frozen render inputs (or a snapshot with the same fields)."""

    start = datetime.fromisoformat(str(inputs["start_at"]).replace("Z", "+00:00"))
    return reminder_text(str(inputs["first_name"]), start, str(inputs["zone"]),
                         str(inputs["clinic_display_name"]))


def snapshot_hash(snapshot: Any) -> str | None:
    """sha256 of the rendered text, or None when an input is missing."""

    if any(snapshot.get(k) is None
           for k in ("first_name", "clinic_display_name", "zone", "start_at")):
        return None
    return hashlib.sha256(render_inputs(snapshot).encode("utf-8")).hexdigest()


class ReminderService(OutboundService):
    __slots__ = ()

    def __repr__(self) -> str:
        return "ReminderService()"

    async def examine(self, context: TenantContext, appointment_ref: Any, *, op_id: Any,
                      reminder_id: Any, lease_seconds: int, text_sha256: str | None,
                      source_version: int | None, settings_version: int | None
                      ) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, EXAMINE_KEY, {
            "appointment_ref": str(appointment_ref), "op_id": str(op_id),
            "reminder_id": str(reminder_id), "lease_seconds": int(lease_seconds),
            "text_sha256": text_sha256, "source_version": source_version,
            "settings_version": settings_version})

    async def consume(self, context: TenantContext, op_id: Any, attempt_id: Any
                      ) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, CONSUME_KEY, {
            "op_id": str(op_id), "attempt_id": str(attempt_id)})

    async def claim_v2(self, context: TenantContext, op_id: Any, fence: int,
                       lease_seconds: int = 120) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, CLAIM_V2_KEY, {
            "op_id": str(op_id), "fence": int(fence), "lease_seconds": int(lease_seconds)})

    async def read_v2(self, context: TenantContext, op_id: Any, attempt_id: Any
                      ) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, READ_V2_KEY, {
            "op_id": str(op_id), "attempt_id": str(attempt_id)})

    async def ready(self, context: TenantContext, limit: int) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, READY_KEY, {"limit": int(limit)})

    async def render_snapshot(self, context: TenantContext, appointment_ref: Any
                              ) -> dict[str, Any]:
        self._admit(context)
        return await self._statement(context, SNAPSHOT_KEY,
                                     {"appointment_ref": str(appointment_ref)})


@dataclass(frozen=True, slots=True)
class RunReport:
    examined: int
    claimed: int
    sent: int


class ReminderRunner:
    """One bounded pass per `run_once` (R-H2-10: progress counts completed examinations)."""

    __slots__ = ("_batch", "_lease", "_service", "_worker")

    def __init__(self, service: Any, worker: Any, *, batch: int, lease_seconds: int) -> None:
        if not 1 <= batch <= 1000:
            raise ValueError("batch must be within 1..1000")
        self._service = service
        self._worker = worker
        self._batch = batch
        self._lease = lease_seconds

    def __repr__(self) -> str:
        return f"ReminderRunner(batch={self._batch})"

    async def run_once(self, context: Any) -> RunReport:
        examined = claimed = sent = 0
        refs = (await self._service.ready(context, self._batch))["refs"]
        for ref in refs:
            try:
                snapshot = await self._service.render_snapshot(context, ref)
                result = await self._service.examine(
                    context, ref, op_id=uuid.uuid4(), reminder_id=uuid.uuid4(),
                    lease_seconds=self._lease, text_sha256=snapshot_hash(snapshot),
                    source_version=snapshot.get("source_version"),
                    settings_version=snapshot.get("settings_version"))
            except OutboundCommitUnknown:
                _LOG.info("reminder examine commit unknown; not sent")
                continue
            except OutboundRefused as refused:
                _LOG.info("reminder examine refused code=%s", refused.code)
                continue
            outcome = result.get("outcome")
            if outcome in _COMPLETED:
                examined += 1
            if outcome != "claimed":
                _LOG.info("reminder examined outcome=%s code=%s", outcome, result.get("code"))
                continue
            claimed += 1
            if await self._worker.run_claimed(context, result["op_id"],
                                              result["attempt_id"]) == "accepted":
                sent += 1
        return RunReport(examined=examined, claimed=claimed, sent=sent)
