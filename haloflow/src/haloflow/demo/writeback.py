"""Write-back decision logic with a conditional write (amended R-21; EXP2).

Initial attempt and retry share one path:

1. ``observed_before`` is null → NO_BASELINE (never a write).
2. Read ``(value, version)``.
3. value == target → OBSERVED_CONVERGENCE (``caused_by_us=False``), no write.
4. value != observed_before → CONFLICT, no write.
5. Conditional write on the read version. A version mismatch → CONFLICT, or
   OBSERVED_CONVERGENCE if the current value already equals the target.

Real mode requires an adapter that declares a verified conditional-write
contract. An adapter without conditional write is disabled in every mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from haloflow.demo.synthetic_ehr import EhrConnectError, Observation, WriteResult

RETRYABLE_STATES = frozenset({"PENDING", "NOT_APPLIED"})


class Outcome(StrEnum):
    DONE = "DONE"
    OBSERVED_CONVERGENCE = "DONE(reconciled)"
    CONFLICT = "writeback_conflict"
    NO_BASELINE = "no_baseline"
    WRITE_DISABLED = "write_disabled"
    NOT_APPLIED = "NOT_APPLIED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class WritebackTask:
    task_id: int
    emr_appt_id: str
    target_value: str
    observed_before: str | None
    state: str


@dataclass(frozen=True)
class WritebackResult:
    outcome: Outcome
    caused_by_us: bool = False
    observed: Observation | None = None


class ConditionalEhr(Protocol):
    supports_conditional_write: bool
    conditional_contract_verified: bool

    async def read_confirmation(self, appt_id: str) -> Observation: ...

    async def write_confirmation_conditional(
        self, appt_id: str, target: str, expected_version: int
    ) -> WriteResult: ...


async def attempt_writeback(
    task: WritebackTask, ehr: ConditionalEhr, *, mode: Literal["simulated", "real"]
) -> WritebackResult:
    if task.state not in RETRYABLE_STATES:
        raise ValueError(f"write-back not allowed from state {task.state}")
    if not ehr.supports_conditional_write:
        return WritebackResult(Outcome.WRITE_DISABLED)
    if mode == "real" and not ehr.conditional_contract_verified:
        return WritebackResult(Outcome.WRITE_DISABLED)
    if task.observed_before is None:
        return WritebackResult(Outcome.NO_BASELINE)
    obs = await ehr.read_confirmation(task.emr_appt_id)
    if obs.value == task.target_value:
        return WritebackResult(Outcome.OBSERVED_CONVERGENCE, observed=obs)
    if obs.value != task.observed_before:
        return WritebackResult(Outcome.CONFLICT, observed=obs)
    try:
        res = await ehr.write_confirmation_conditional(
            task.emr_appt_id, task.target_value, obs.version
        )
    except EhrConnectError:
        return WritebackResult(Outcome.NOT_APPLIED, observed=obs)
    except Exception:
        return WritebackResult(Outcome.UNCERTAIN, observed=obs)
    if res.applied:
        return WritebackResult(Outcome.DONE, caused_by_us=True, observed=res.current)
    if res.current.value == task.target_value:
        return WritebackResult(Outcome.OBSERVED_CONVERGENCE, observed=res.current)
    return WritebackResult(Outcome.CONFLICT, observed=res.current)


async def reconcile(task: WritebackTask, ehr: ConditionalEhr) -> WritebackResult:
    """Read-only. Convergence is observed, never claimed as caused by us."""
    obs = await ehr.read_confirmation(task.emr_appt_id)
    if obs.value == task.target_value:
        return WritebackResult(Outcome.OBSERVED_CONVERGENCE, observed=obs)
    if task.observed_before is not None and obs.value == task.observed_before:
        return WritebackResult(Outcome.UNCERTAIN, observed=obs)
    return WritebackResult(Outcome.CONFLICT, observed=obs)
