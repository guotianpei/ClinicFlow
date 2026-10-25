"""Write-back runner (R-20/R-21 with the EXP2 conditional-write rules).

``run_task`` admits a task by compare-and-set: PENDING or NOT_APPLIED becomes
IN_FLIGHT, committed before the EHR call. A concurrent second run gets
"already_running" and makes no call. UNCERTAIN offers only ``reconcile_task``,
which reads and never writes. A crash after IN_FLIGHT is recovered as
UNCERTAIN(restart). EHR mode stays SIMULATED; real writes are disabled until the
provider contract is verified.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import text

from haloflow_demo.errors import OwnerLost
from haloflow_demo.inbound import INSTRUCTIONS
from haloflow_demo.ownership import Ownership
from haloflow_demo.writeback import (
    ConditionalEhr,
    Outcome,
    WritebackResult,
    WritebackTask,
    attempt_writeback,
    reconcile,
)

_STATE_FOR: dict[Outcome, tuple[str, str | None]] = {
    Outcome.DONE: ("DONE", None),
    Outcome.OBSERVED_CONVERGENCE: ("DONE", "reconciled"),
    Outcome.CONFLICT: ("CONFLICT", "writeback_conflict"),
    Outcome.NO_BASELINE: ("NOT_APPLIED", "no_baseline"),
    Outcome.WRITE_DISABLED: ("NOT_APPLIED", "write_disabled"),
    Outcome.NOT_APPLIED: ("NOT_APPLIED", None),
    Outcome.UNCERTAIN: ("UNCERTAIN", None),
}


async def _load(own: Ownership, task_id: int) -> Any:
    async with own.engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT t.id, t.state, t.target_value, t.observed_before, t.reminder_id,"
            " r.emr_appt_id FROM writeback_tasks t JOIN reminder_records r ON r.id = t.reminder_id"
            " WHERE t.id = :t"), {"t": task_id})).one_or_none()
    if row is None:
        raise ValueError(f"no write-back task {task_id}")
    return row


async def _finish(own: Ownership, row: Any, result: WritebackResult) -> str:
    state, reason = _STATE_FOR[result.outcome]
    async with own.fenced() as conn:
        await conn.execute(text(
            "UPDATE writeback_tasks SET state = :s, reason = :r, updated_at = now()"
            " WHERE id = :t AND state IN ('IN_FLIGHT', 'UNCERTAIN')"
        ), {"s": state, "r": reason, "t": row.id})
        if result.outcome is Outcome.CONFLICT:
            await conn.execute(text(
                "INSERT INTO follow_up_items(source_key, kind, reminder_id, detail)"
                " VALUES (:s, 'writeback_conflict', :r, :d)"
                " ON CONFLICT ON CONSTRAINT uq_follow_up_source_kind DO NOTHING"
            ), {"s": f"task:{row.id}", "r": row.reminder_id,
                "d": INSTRUCTIONS["writeback_conflict"]})
    return result.outcome.value


async def run_task(own: Ownership, ehr: ConditionalEhr, task_id: int, *,
                   mode: Literal["simulated", "real"] = "simulated") -> str:
    row = await _load(own, task_id)
    if row.state == "IN_FLIGHT":
        return "already_running"
    if row.state not in ("PENDING", "NOT_APPLIED"):
        raise ValueError(f"write-back not allowed from state {row.state}")
    async with own.fenced() as conn:
        admitted = (await conn.execute(text(
            "UPDATE writeback_tasks SET state = 'IN_FLIGHT', attempts = attempts + 1,"
            " updated_at = now() WHERE id = :t AND state IN ('PENDING', 'NOT_APPLIED')"
            " RETURNING id"), {"t": task_id})).scalar_one_or_none()
    if admitted is None:
        return "already_running"
    task = WritebackTask(task_id=int(row.id), emr_appt_id=str(row.emr_appt_id),
                         target_value=str(row.target_value),
                         observed_before=row.observed_before, state=str(row.state))

    async def _call() -> WritebackResult:
        return await attempt_writeback(task, ehr, mode=mode)

    try:
        result = await own.guard_external(_call)
    except OwnerLost:
        return "owner_unavailable"  # stays IN_FLIGHT; recovery marks it UNCERTAIN
    return await _finish(own, row, result)


async def reconcile_task(own: Ownership, ehr: ConditionalEhr, task_id: int) -> str:
    row = await _load(own, task_id)
    if row.state != "UNCERTAIN":
        raise ValueError(f"reconcile not allowed from state {row.state}")
    task = WritebackTask(task_id=int(row.id), emr_appt_id=str(row.emr_appt_id),
                         target_value=str(row.target_value),
                         observed_before=row.observed_before, state=str(row.state))

    async def _call() -> WritebackResult:
        return await reconcile(task, ehr)

    try:
        result = await own.guard_external(_call)
    except OwnerLost:
        return "owner_unavailable"
    if result.outcome is Outcome.UNCERTAIN:
        return result.outcome.value
    return await _finish(own, row, result)
