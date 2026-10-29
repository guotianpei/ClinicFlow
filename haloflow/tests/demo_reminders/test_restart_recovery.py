"""M-2 restart write-back recovery (packages claude_m2-restart-recovery-package-2026-10-28
v1 + v1r1; Codex ALIGN codex_m2-v1-review.md / codex_m2-v1r1-review.md).

Synthetic mode only. SIMULATED recovery evidence; nothing here is a real-EHR guarantee.

API under test (to be implemented):

- ``models.SimConfirmation`` / table ``sim_confirmations``: PK (source, tenant_id,
  emr_patient_id, appt_start), value, version (≥ 1). ``SCHEMA_VERSION`` = 4.
- ``haloflow_demo.sim_store``: ``Slot(source, tenant_id, emr_patient_id, appt_start)``;
  ``SimStore(own, tenant)`` with ``seed``, ``read``, ``cas``; ``SlotRefused`` for a slot
  whose source is not 'synthetic' or whose tenant is not the store's tenant.
- ``DbStaffService.run_writeback`` / ``reconcile_writeback`` validate EVERY task before
  any admission: reminder tenant == the active tenant, synthetic id format, and slot
  binding (a current-process id must match the reminder's patient + start; an old id
  needs exactly one durable slot row). The run uses a task-scoped EHR view that serves
  only that validated appointment; an invalid task gets the existing
  ``writeback_scenario_unavailable`` path: no admission, no EHR read or write.

Crash points (R-M2-6): (1) before the write, (2) after the committed simulated write and
before task completion, (3) after reconciliation. "Exactly one write" is asserted on the
DURABLE version, not on per-instance counters.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from haloflow_demo import tasks as tasks_mod
from haloflow_demo.models import DemoBase
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.reset import reset_synthetic
from haloflow_demo.sending import StubGateway
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url
from haloflow_demo.sim_store import SimStore, Slot, SlotRefused
from haloflow_demo.synthetic_ehr import SyntheticEhr

TEST_NUMBER = "+15550100199"
SENDER = "+15550100100"
SECRET = "test-callback-secret-0123456789"
TENANT = "demo-clinic"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # target clinic date 2026-10-28
APPT_START = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)   # syn-p-a, 09:00 EDT
PATIENT = "syn-p-a"
APPT_E1 = "syn-e1-20261028-a-1"
APPT_E2 = "syn-e2-20261028-a-1"


def _env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER, "DEMO_SMS_SENDER": SENDER,
            "DEMO_CALLBACK_SECRET": SECRET}


async def proc(env: dict[str, str], gw: StubGateway | None = None,
               now: datetime = NOW) -> tuple[Any, DbStaffService]:
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    return own, DbStaffService(own, s, now=lambda: now, gateway=gw or StubGateway(TEST_NUMBER))


def q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def store_row(url: str, patient: str = PATIENT, start: datetime = APPT_START,
              source: str = "synthetic", tenant: str = TENANT) -> tuple[Any, ...] | None:
    rows = q(url, "SELECT value, version FROM sim_confirmations WHERE source = %s AND"
                  " tenant_id = %s AND emr_patient_id = %s AND appt_start = %s",
             (source, tenant, patient, start))
    assert len(rows) <= 1
    return rows[0] if rows else None


def task(url: str, tid: int) -> tuple[Any, ...]:
    return q(url, "SELECT state, reason, attempts FROM writeback_tasks WHERE id = %s",
             (tid,))[0]


def items(url: str) -> list[str]:
    return sorted(r[0] for r in q(url, "SELECT kind FROM follow_up_items"))


async def confirmed(svc: DbStaffService, process: bool = True) -> int | None:
    """Send to appointment A, simulate reply '1'; optionally process it into a task."""
    appt = (await svc.page_data())["appointments"][0]["emr_appt_id"]
    sent = await svc.send_to_appointment(appt)
    assert sent["state"] == "SUBMITTED", sent
    res = await svc.simulate_reply(int(sent["reminder_id"]), "1")
    assert res.status == 204
    if not process:
        return None
    await svc.process_now()
    rows = q(svc._settings.database_url, "SELECT id FROM writeback_tasks")   # noqa: SLF001
    assert len(rows) == 1
    return int(rows[0][0])


class Crash(Exception):
    pass


class Spy:
    """Counts every confirmation read and write reaching the store or an instance."""

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.reads = self.writes = 0
        r_store, c_store = SimStore.read, SimStore.cas
        r_inst, w_inst = SyntheticEhr.read_confirmation, SyntheticEhr.write_confirmation_conditional

        async def store_read(this: Any, *a: Any, **kw: Any) -> Any:
            self.reads += 1
            return await r_store(this, *a, **kw)

        async def store_cas(this: Any, *a: Any, **kw: Any) -> Any:
            self.writes += 1
            return await c_store(this, *a, **kw)

        async def inst_read(this: Any, *a: Any, **kw: Any) -> Any:
            self.reads += 1
            return await r_inst(this, *a, **kw)

        async def inst_write(this: Any, *a: Any, **kw: Any) -> Any:
            self.writes += 1
            return await w_inst(this, *a, **kw)
        mp.setattr(SimStore, "read", store_read)
        mp.setattr(SimStore, "cas", store_cas)
        mp.setattr(SyntheticEhr, "read_confirmation", inst_read)
        mp.setattr(SyntheticEhr, "write_confirmation_conditional", inst_write)

    def snapshot(self) -> tuple[int, int]:
        return self.reads, self.writes


# ── recovery paths ───────────────────────────────────────────────────────────


async def test_rr1_pending_task_after_restart_done_one_durable_write(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
        assert store_row(demo_db_url) == ("1", 1)
    finally:
        await own.close()
    own, svc = await proc(env)                                 # restart: epoch 2
    try:
        page = await svc.page_data()
        result = await svc.run_writeback(tid)  # type: ignore[arg-type]
        regenerated = svc.ehr_for_date(APPT_START.date())
    finally:
        await own.close()
    assert result == "DONE"
    assert store_row(demo_db_url) == ("2", 2)                  # exactly one durable write
    assert task(demo_db_url, tid)[0] == "DONE"  # type: ignore[arg-type]
    assert "writeback_scenario_unavailable" not in items(demo_db_url)
    assert regenerated.write_attempts == 0                     # not the regenerated fixture
    status = {a["emr_appt_id"]: a["status"] for a in page["appointments"]}
    assert status[APPT_E2].endswith("reminder: Confirmed by patient (before restart)")


async def test_rr2_pending_reply_processed_after_restart(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        await confirmed(svc, process=False)                    # reply RECEIVED only
        assert q(demo_db_url, "SELECT count(*) FROM writeback_tasks") == [(0,)]
    finally:
        await own.close()
    own, svc = await proc(env)
    try:
        await svc.process_now()
        tid = int(q(demo_db_url, "SELECT id FROM writeback_tasks")[0][0])
        result = await svc.run_writeback(tid)
    finally:
        await own.close()
    assert result == "DONE" and store_row(demo_db_url) == ("2", 2)


async def test_rr3_crash_before_write_reconcile_stays_uncertain(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    q(demo_db_url, "UPDATE writeback_tasks SET state = 'IN_FLIGHT' WHERE id = %s", (tid,))
    own, svc = await proc(env)                                 # recover → UNCERTAIN(restart)
    try:
        assert task(demo_db_url, tid)[:2] == ("UNCERTAIN", "restart")  # type: ignore[arg-type]
        result = await svc.reconcile_writeback(tid)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await svc.run_writeback(tid)  # type: ignore[arg-type]     # Run not offered
    finally:
        await own.close()
    assert result == "UNCERTAIN"
    assert store_row(demo_db_url) == ("1", 1)                  # no automatic write
    assert task(demo_db_url, tid)[0] == "UNCERTAIN"  # type: ignore[arg-type]


async def test_rr4_crash_after_committed_cas_before_completion(
    demo_env: dict[str, str], demo_db_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    own, svc = await proc(env)                                 # the write runs in epoch 2

    async def crash(*a: Any, **kw: Any) -> str:
        raise Crash
    with monkeypatch.context() as m:
        m.setattr(tasks_mod, "_finish", crash)
        try:
            with pytest.raises(Crash):
                await svc.run_writeback(tid)  # type: ignore[arg-type]
        finally:
            await own.close()
    assert store_row(demo_db_url) == ("2", 2)                  # CAS committed
    assert task(demo_db_url, tid)[0] == "IN_FLIGHT"  # type: ignore[arg-type]  # not finished
    own, svc = await proc(env)                                 # epoch 3
    try:
        assert task(demo_db_url, tid)[:2] == ("UNCERTAIN", "restart")  # type: ignore[arg-type]
        result = await svc.reconcile_writeback(tid)  # type: ignore[arg-type]
    finally:
        await own.close()
    assert result == "DONE(reconciled)"
    assert task(demo_db_url, tid)[:2] == ("DONE", "reconciled")  # type: ignore[arg-type]
    assert store_row(demo_db_url) == ("2", 2)                  # still one durable write


async def test_rr5_after_reconciliation_further_restarts_change_nothing(
    demo_env: dict[str, str], demo_db_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await test_rr4_crash_after_committed_cas_before_completion(demo_env, demo_db_url,
                                                                monkeypatch)
    tid = int(q(demo_db_url, "SELECT id FROM writeback_tasks")[0][0])
    for _ in range(2):
        own, svc = await proc(_env(demo_env))
        try:
            await svc.page_data()
            with pytest.raises(ValueError):
                await svc.run_writeback(tid)
            with pytest.raises(ValueError):
                await svc.reconcile_writeback(tid)
        finally:
            await own.close()
    assert store_row(demo_db_url) == ("2", 2)
    assert task(demo_db_url, tid)[:2] == ("DONE", "reconciled")


async def test_rr6_clinic_change_before_recovery_conflict(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    q(demo_db_url, "UPDATE sim_confirmations SET value = '3', version = version + 1"
                   " WHERE emr_patient_id = %s AND appt_start = %s", (PATIENT, APPT_START))
    own, svc = await proc(env)
    try:
        result = await svc.run_writeback(tid)  # type: ignore[arg-type]
    finally:
        await own.close()
    assert result == "writeback_conflict"
    assert store_row(demo_db_url) == ("3", 2)                  # no write by us
    assert "writeback_conflict" in items(demo_db_url)


async def test_rr7_missing_store_row_is_staff_item_no_admission(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    q(demo_db_url, "DELETE FROM sim_confirmations WHERE emr_patient_id = %s", (PATIENT,))
    own, svc = await proc(env)
    try:
        result = await svc.run_writeback(tid)  # type: ignore[arg-type]   # no page load
    finally:
        await own.close()
    assert result == "scenario_unavailable"
    assert task(demo_db_url, tid) == ("PENDING", None, 0)  # type: ignore[arg-type]
    assert store_row(demo_db_url) is None
    assert "writeback_scenario_unavailable" in items(demo_db_url)


async def test_rr8_seed_once_loading_fixtures_never_resets(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
        assert await svc.run_writeback(tid) == "DONE"  # type: ignore[arg-type]
    finally:
        await own.close()
    own, svc = await proc(env)
    try:
        await svc.page_data()                                  # same date reloaded
    finally:
        await own.close()
    assert store_row(demo_db_url) == ("2", 2)
    assert q(demo_db_url, "SELECT count(*) FROM sim_confirmations") == [(7,)]


async def test_rr9_double_restart_then_run_one_write(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    for _ in range(2):
        own, svc = await proc(env)
        await own.close()
    own, svc = await proc(env)
    try:
        assert await svc.run_writeback(tid) == "DONE"  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await svc.run_writeback(tid)  # type: ignore[arg-type]
    finally:
        await own.close()
    assert store_row(demo_db_url) == ("2", 2)


# ── schema, reset ────────────────────────────────────────────────────────────


async def test_rr10_genuine_v3_database_refused_without_residue(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    v3 = [t for name, t in DemoBase.metadata.tables.items() if name != "sim_confirmations"]
    from sqlalchemy import create_engine
    eng = create_engine(demo_db_url.replace("postgresql://", "postgresql+psycopg://", 1))
    with eng.begin() as conn:
        DemoBase.metadata.create_all(conn, tables=v3)
    eng.dispose()
    q(demo_db_url, "INSERT INTO demo_meta(key, value) VALUES ('schema_version', '3'),"
                   " ('phone_key_secret', 'x')")
    before_tables = {r[0] for r in q(demo_db_url, "SELECT table_name FROM"
                                                  " information_schema.tables WHERE"
                                                  " table_schema = 'public'")}
    before_meta = q(demo_db_url, "SELECT key, value FROM demo_meta ORDER BY key")
    s = DemoSettings.from_env(_env(demo_env))
    with pytest.raises(StartupRefused) as exc:
        await acquire_ownership(s, validate_demo_database_url(s))
    assert exc.value.reason == "demo_schema_mismatch"
    after_tables = {r[0] for r in q(demo_db_url, "SELECT table_name FROM"
                                                 " information_schema.tables WHERE"
                                                 " table_schema = 'public'")}
    assert "sim_confirmations" not in after_tables and after_tables == before_tables
    assert q(demo_db_url, "SELECT key, value FROM demo_meta ORDER BY key") == before_meta


async def test_rr11_reset_synthetic_clears_store_keeps_preserved_state(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        await confirmed(svc)
    finally:
        await own.close()
    quota = q(demo_db_url, "SELECT * FROM send_quota")
    contacts = q(demo_db_url, "SELECT count(*) FROM contact_history")
    await reset_synthetic(DemoSettings.from_env(env))
    assert q(demo_db_url, "SELECT count(*) FROM sim_confirmations") == [(0,)]
    assert q(demo_db_url, "SELECT * FROM send_quota") == quota
    assert q(demo_db_url, "SELECT count(*) FROM contact_history") == contacts


# ── identity, isolation ──────────────────────────────────────────────────────


async def test_rr13_date_rollover_writes_original_slot_only(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    from datetime import timedelta
    env = _env(demo_env)
    clock = {"now": NOW}
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    svc = DbStaffService(own, s, now=lambda: clock["now"], gateway=StubGateway(TEST_NUMBER))
    try:
        tid = await confirmed(svc)
        clock["now"] = NOW + timedelta(days=1)
        await svc.page_data()
        assert await svc.run_writeback(tid) == "DONE"  # type: ignore[arg-type]
    finally:
        await own.close()
    assert store_row(demo_db_url) == ("2", 2)
    assert store_row(demo_db_url, start=APPT_START + timedelta(days=1)) == ("1", 1)


async def test_rr14_concurrent_cas_exactly_one_applies(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    own, svc = await proc(_env(demo_env))
    try:
        await svc.page_data()
        store = SimStore(own, TENANT)
        slot = Slot("synthetic", TENANT, PATIENT, APPT_START)
        a, b = await asyncio.gather(store.cas(slot, "2", 1), store.cas(slot, "3", 1))
    finally:
        await own.close()
    assert sorted([a.applied, b.applied]) == [False, True]
    assert store_row(demo_db_url)[1] == 2  # type: ignore[index]


async def test_rr15_tenant_and_source_separation(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    for src, ten in (("athena", TENANT), ("synthetic", "other-clinic")):
        q(demo_db_url, "INSERT INTO sim_confirmations(source, tenant_id, emr_patient_id,"
                       " appt_start, value, version) VALUES (%s, %s, %s, %s, '1', 1)",
          (src, ten, PATIENT, APPT_START))
    own, svc = await proc(env)
    try:
        assert await svc.run_writeback(tid) == "DONE"  # type: ignore[arg-type]
    finally:
        await own.close()
    assert store_row(demo_db_url) == ("2", 2)
    assert store_row(demo_db_url, source="athena") == ("1", 1)
    assert store_row(demo_db_url, tenant="other-clinic") == ("1", 1)


def _forge_task(url: str, appt_id: str, *, tenant: str = TENANT, patient: str = PATIENT,
                start: datetime = APPT_START) -> int:
    rid = q(url, "INSERT INTO reminder_records(tenant_id, emr_appt_id, emr_patient_id,"
                 " appt_start, state, mode) VALUES (%s, %s, %s, %s, 'CONFIRMED', 'stub')"
                 " RETURNING id", (tenant, appt_id, patient, start))[0][0]
    return int(q(url, "INSERT INTO writeback_tasks(reminder_id, state, target_value,"
                      " observed_before) VALUES (%s, 'PENDING', '2', '1') RETURNING id",
                 (rid,))[0][0])


@pytest.mark.parametrize("appt_id,forge", [
    # Same id as the legit current appointment A, another tenant (allowed by
    # uq_reminder_appt, which is per tenant), same slot and a different slot.
    (APPT_E1, {"tenant": "other-clinic"}),
    (APPT_E1, {"tenant": "other-clinic", "patient": "syn-p-b"}),
    # Own tenant, a current id that belongs to ANOTHER slot (appointment B, patient b
    # at 09:30) while the forged reminder claims patient a at 09:00: slot mismatch.
    ("syn-e1-20261028-a-2", {}),
])
async def test_rr16a_known_current_id_collision_refused_valid_still_works(
    demo_env: dict[str, str], demo_db_url: str, appt_id: str, forge: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own, svc = await proc(_env(demo_env))
    try:
        tid = await confirmed(svc)                               # legit task for APPT_E1
        forged = _forge_task(demo_db_url, appt_id, **forge)
        spy = Spy(monkeypatch)
        before = spy.snapshot()
        bad = await svc.run_writeback(forged)
        assert spy.snapshot() == before                          # no read, no write
        good = await svc.run_writeback(tid)  # type: ignore[arg-type]
    finally:
        await own.close()
    assert bad == "scenario_unavailable"
    assert task(demo_db_url, forged) == ("PENDING", None, 0)       # no admission
    assert good == "DONE" and store_row(demo_db_url) == ("2", 2)   # one legit write only
    b_slot = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
    assert store_row(demo_db_url, patient="syn-p-b", start=b_slot) == ("1", 1)


async def test_rr16b_prepared_old_id_collision_refused_valid_still_works(
    demo_env: dict[str, str], demo_db_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    own, svc = await proc(env)                                    # APPT_E1 is now an old id
    try:
        forged = _forge_task(demo_db_url, APPT_E1, tenant="other-clinic")
        spy = Spy(monkeypatch)
        s0 = spy.snapshot()
        first_bad = await svc.run_writeback(forged)               # before legit resolution
        assert spy.snapshot() == s0
        good = await svc.run_writeback(tid)  # type: ignore[arg-type]  # resolves the old id
        s1 = spy.snapshot()
        second_bad = await svc.run_writeback(forged)              # after legit resolution
        assert spy.snapshot() == s1
    finally:
        await own.close()
    assert first_bad == second_bad == "scenario_unavailable"
    assert task(demo_db_url, forged) == ("PENDING", None, 0)
    assert good == "DONE" and store_row(demo_db_url) == ("2", 2)


@pytest.mark.parametrize("old", [False, True])
async def test_rr16d_uncertain_reconcile_collisions_refused_without_read(
    demo_env: dict[str, str], demo_db_url: str, old: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconcile path: a forged cross-tenant UNCERTAIN task using the legit task's
    appointment id (current id, or an old id after a restart) is refused before AND
    after the legitimate task's resolution, with no confirmation read or write and no
    state transition; the legitimate reconcile still recovers."""
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
        if old:
            await own.close()
            own, svc = await proc(env)                            # APPT_E1 is an old id now
        forged = _forge_task(demo_db_url, APPT_E1, tenant="other-clinic")
        # legit task: committed simulated write, then left UNCERTAIN (crash point 2)
        q(demo_db_url, "UPDATE sim_confirmations SET value = '2', version = 2 WHERE"
                       " source = 'synthetic' AND tenant_id = %s AND emr_patient_id = %s"
                       " AND appt_start = %s", (TENANT, PATIENT, APPT_START))
        q(demo_db_url, "UPDATE writeback_tasks SET state = 'UNCERTAIN', reason = 'restart'"
                       " WHERE id IN (%s, %s)", (tid, forged))
        spy = Spy(monkeypatch)
        s0 = spy.snapshot()
        first_bad = await svc.reconcile_writeback(forged)
        assert spy.snapshot() == s0
        good = await svc.reconcile_writeback(tid)  # type: ignore[arg-type]
        assert spy.snapshot()[0] > s0[0] and spy.snapshot()[1] == s0[1]   # read, no write
        s1 = spy.snapshot()
        second_bad = await svc.reconcile_writeback(forged)
        assert spy.snapshot() == s1
    finally:
        await own.close()
    assert first_bad == second_bad == "scenario_unavailable"
    assert task(demo_db_url, forged)[:2] == ("UNCERTAIN", "restart")   # no transition
    assert good == "DONE(reconciled)"
    assert task(demo_db_url, tid)[:2] == ("DONE", "reconciled")  # type: ignore[arg-type]
    assert store_row(demo_db_url) == ("2", 2)


@pytest.mark.parametrize("appt_id", ["ath-e1-1001", "syn-a-1", "syn-e1-20261028-a-1x"])
async def test_rr16c_non_synthetic_or_bad_format_refused(
    demo_env: dict[str, str], demo_db_url: str, appt_id: str,
) -> None:
    own, svc = await proc(_env(demo_env))
    try:
        await svc.page_data()
        forged = _forge_task(demo_db_url, appt_id)
        result = await svc.run_writeback(forged)
    finally:
        await own.close()
    assert result == "scenario_unavailable"
    assert task(demo_db_url, forged) == ("PENDING", None, 0)
    assert store_row(demo_db_url) == ("1", 1)


async def test_rr17_store_refuses_foreign_slot(
    demo_env: dict[str, str],
) -> None:
    own, svc = await proc(_env(demo_env))
    try:
        await svc.page_data()
        store = SimStore(own, TENANT)
        for slot in (Slot("athena", TENANT, PATIENT, APPT_START),
                     Slot("synthetic", "other-clinic", PATIENT, APPT_START)):
            with pytest.raises(SlotRefused):
                await store.read(slot)
            with pytest.raises(SlotRefused):
                await store.cas(slot, "2", 1)
    finally:
        await own.close()


async def test_rr18_concurrent_first_load_one_seed_one_instance(
    demo_env: dict[str, str], demo_db_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeds: list[int] = []
    real_seed = SimStore.seed

    async def slow_seed(this: Any, *a: Any, **kw: Any) -> Any:
        seeds.append(1)
        for _ in range(5):                                       # let the other caller in
            await asyncio.sleep(0)
        return await real_seed(this, *a, **kw)
    monkeypatch.setattr(SimStore, "seed", slow_seed)
    own, svc = await proc(_env(demo_env))
    try:
        d = APPT_START.date()
        a, b = await asyncio.gather(svc._source(d), svc._source(d))   # noqa: SLF001
        pa, pb = await asyncio.gather(svc.page_data(), svc.page_data())
    finally:
        await own.close()
    assert a is not None and a is b                              # both callers share it
    assert len(seeds) == 1                                       # one initialization
    assert [x["emr_appt_id"] for x in pa["appointments"]] == \
        [x["emr_appt_id"] for x in pb["appointments"]]
    assert q(demo_db_url, "SELECT count(*), min(version), max(version), min(value),"
                          " max(value) FROM sim_confirmations") == [(7, 1, 1, "1", "1")]


async def test_rr19_direct_task_runner_bypass_fails_closed(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    own, svc = await proc(env)
    try:
        tid = await confirmed(svc)
    finally:
        await own.close()
    own, svc = await proc(env)
    try:
        await svc.page_data()
        result = await tasks_mod.run_task(own, svc.ehr, tid)  # type: ignore[arg-type]
    finally:
        await own.close()
    assert result == "scenario_unavailable"
    assert task(demo_db_url, tid) == ("PENDING", None, 0)  # type: ignore[arg-type]
    assert store_row(demo_db_url) == ("1", 1)


# ── RR-20 (Codex freeze ee09206, D-a): fenced store reads ─────────────────────


async def test_rr20_store_reads_and_writes_refused_after_close(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    """Store reads are fenced (R-M2-3): once ownership is closed, read, instance read
    and CAS are all refused (OwnerLost) and nothing changes."""
    from haloflow_demo.errors import OwnerLost
    own, svc = await proc(_env(demo_env))
    await svc.page_data()
    ehr = svc.ehr_for_date(APPT_START.date())
    store = SimStore(own, TENANT)
    slot = Slot("synthetic", TENANT, PATIENT, APPT_START)
    assert (await store.read(slot)).value == "1"
    await own.close()
    with pytest.raises(OwnerLost):
        await store.read(slot)
    with pytest.raises(OwnerLost):
        await ehr.read_confirmation(APPT_E1)
    with pytest.raises(OwnerLost):
        await store.cas(slot, "2", 1)
    assert store_row(demo_db_url) == ("1", 1)
