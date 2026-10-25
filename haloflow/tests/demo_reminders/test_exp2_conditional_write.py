"""Experiment 2 — Codex issue 2: change between read and write.

Read-back followed by an unconditional write is a check/use race: another clinic
actor can change the value after our read. Remedy under test: every write-back
(initial and retry) is a conditional write against the version observed in the
read; a version mismatch is a conflict and is never overwritten. A null
``observed_before`` never authorises a write. A remote value that equals the
target is reported as observed convergence, not as our write.

Real EHR mutation stays disabled unless the adapter declares a verified
conditional-write contract. The synthetic adapter does not (it is synthetic),
so ``mode="real"`` is refused for it; athenahealth's contract is unverified (X2).

The negative control re-runs the race against the legacy read-then-PUT shape to
show the synthetic EHR does exhibit the lost update, so the conflict tests are
sensitive. U-layer; no database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from haloflow_demo.synthetic_ehr import ApptStatus, Fixtures, SyntheticAppointment, SyntheticEhr
from haloflow_demo.writeback import Outcome, WritebackTask, attempt_writeback, reconcile

APPT = "wb-1"
TARGET = "2"


def _ehr(initial: str | None = "1") -> SyntheticEhr:
    fx = Fixtures(
        patients={},
        appointments=[SyntheticAppointment(APPT, "p1", datetime(2026, 10, 28, 14, tzinfo=UTC),
                                           ApptStatus.SCHEDULED, confirmation=initial)],
    )
    return SyntheticEhr(fx)


def _task(state: str = "PENDING", observed_before: str | None = "1") -> WritebackTask:
    return WritebackTask(task_id=1, emr_appt_id=APPT, target_value=TARGET,
                         observed_before=observed_before, state=state)


async def _remote(ehr: SyntheticEhr) -> str | None:
    return (await ehr.read_confirmation(APPT)).value


# ── baseline behaviour ────────────────────────────────────────────────────────


async def test_exp2_clean_initial_write_applies_once() -> None:
    ehr = _ehr("1")
    res = await attempt_writeback(_task(), ehr, mode="simulated")
    assert res.outcome is Outcome.DONE
    assert res.caused_by_us is True
    assert await _remote(ehr) == TARGET
    assert ehr.applied_writes == 1


async def test_exp2_null_baseline_never_authorises_a_write() -> None:
    ehr = _ehr("1")
    res = await attempt_writeback(_task(observed_before=None), ehr, mode="simulated")
    assert res.outcome is Outcome.NO_BASELINE
    assert ehr.write_attempts == 0
    assert await _remote(ehr) == "1"


async def test_exp2_null_remote_value_with_null_baseline_still_no_write() -> None:
    ehr = _ehr(None)
    res = await attempt_writeback(_task(observed_before=None), ehr, mode="simulated")
    assert res.outcome is Outcome.NO_BASELINE
    assert ehr.write_attempts == 0


async def test_exp2_already_at_target_is_observed_convergence_not_our_write() -> None:
    ehr = _ehr(TARGET)
    res = await attempt_writeback(_task(), ehr, mode="simulated")
    assert res.outcome is Outcome.OBSERVED_CONVERGENCE
    assert res.caused_by_us is False
    assert ehr.write_attempts == 0


async def test_exp2_change_before_read_is_conflict_without_write() -> None:
    ehr = _ehr("1")
    ehr.clinic_change(APPT, "3")
    res = await attempt_writeback(_task(), ehr, mode="simulated")
    assert res.outcome is Outcome.CONFLICT
    assert ehr.write_attempts == 0
    assert await _remote(ehr) == "3"


# ── the race: change between read and write ───────────────────────────────────


@pytest.mark.parametrize("state", ["PENDING", "NOT_APPLIED"], ids=["initial", "retry"])
async def test_exp2_change_between_read_and_write_is_conflict(state: str) -> None:
    ehr = _ehr("1")
    ehr.on_after_read = lambda: ehr.clinic_change(APPT, "3")
    res = await attempt_writeback(_task(state=state), ehr, mode="simulated")
    assert res.outcome is Outcome.CONFLICT
    assert ehr.applied_writes == 0
    assert await _remote(ehr) == "3", "the clinic's change must survive"


@pytest.mark.parametrize("state", ["PENDING", "NOT_APPLIED"], ids=["initial", "retry"])
async def test_exp2_clinic_sets_target_between_read_and_write(state: str) -> None:
    ehr = _ehr("1")
    ehr.on_after_read = lambda: ehr.clinic_change(APPT, TARGET)
    res = await attempt_writeback(_task(state=state), ehr, mode="simulated")
    assert res.outcome is Outcome.OBSERVED_CONVERGENCE
    assert res.caused_by_us is False
    assert ehr.applied_writes == 0


async def test_exp2_negative_control_legacy_read_then_put_loses_the_clinic_change() -> None:
    """The legacy shape (read, compare, unconditional PUT) on the same EHR."""
    ehr = _ehr("1")
    ehr.on_after_read = lambda: ehr.clinic_change(APPT, "3")
    seen = await ehr.read_confirmation(APPT)
    assert seen.value == "1"  # check passes
    await ehr.write_confirmation_unconditional(APPT, TARGET)  # use
    assert await _remote(ehr) == TARGET, "lost update: the clinic's '3' was overwritten"
    assert ehr.lost_updates == 1


# ── failure outcomes (T41, T42) ───────────────────────────────────────────────


async def test_exp2_t41_connect_failure_is_not_applied() -> None:
    ehr = _ehr("1")
    ehr.fail_next_write = "connect"
    res = await attempt_writeback(_task(), ehr, mode="simulated")
    assert res.outcome is Outcome.NOT_APPLIED
    assert ehr.applied_writes == 0
    assert await _remote(ehr) == "1"


async def test_exp2_t42_lost_response_is_uncertain_then_reconciled_read_only() -> None:
    ehr = _ehr("1")
    ehr.fail_next_write = "lost_response"
    res = await attempt_writeback(_task(), ehr, mode="simulated")
    assert res.outcome is Outcome.UNCERTAIN
    attempts_after_write = ehr.write_attempts
    rec = await reconcile(_task(state="UNCERTAIN"), ehr)
    assert rec.outcome is Outcome.OBSERVED_CONVERGENCE
    assert rec.caused_by_us is False  # convergence observed, causation not proven
    assert ehr.write_attempts == attempts_after_write == 1


async def test_exp2_reconcile_unchanged_value_stays_uncertain() -> None:
    ehr = _ehr("1")
    rec = await reconcile(_task(state="UNCERTAIN"), ehr)
    assert rec.outcome is Outcome.UNCERTAIN
    assert ehr.write_attempts == 0


async def test_exp2_reconcile_other_value_is_conflict() -> None:
    ehr = _ehr("1")
    ehr.clinic_change(APPT, "3")
    rec = await reconcile(_task(state="UNCERTAIN"), ehr)
    assert rec.outcome is Outcome.CONFLICT
    assert ehr.write_attempts == 0


@pytest.mark.parametrize("state", ["UNCERTAIN", "IN_FLIGHT", "DONE"])
async def test_exp2_no_blind_retry_from_other_states(state: str) -> None:
    ehr = _ehr("1")
    with pytest.raises(ValueError):
        await attempt_writeback(_task(state=state), ehr, mode="simulated")
    assert ehr.write_attempts == 0


# ── real-mode gate (T46 extended) ─────────────────────────────────────────────


async def test_exp2_real_mode_refused_without_verified_conditional_contract() -> None:
    ehr = _ehr("1")
    assert ehr.conditional_contract_verified is False
    res = await attempt_writeback(_task(), ehr, mode="real")
    assert res.outcome is Outcome.WRITE_DISABLED
    assert ehr.write_attempts == 0


async def test_exp2_adapter_without_conditional_write_is_disabled_in_any_mode() -> None:
    ehr = _ehr("1")
    ehr.supports_conditional_write = False
    for mode in ("simulated", "real"):
        res = await attempt_writeback(_task(), ehr, mode=mode)  # type: ignore[arg-type]
        assert res.outcome is Outcome.WRITE_DISABLED
    assert ehr.write_attempts == 0
