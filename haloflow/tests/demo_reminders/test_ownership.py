"""D-layer ownership: T05/T06 (R-3), T08 (R-5), fencing (R-4), recovery (R-13:
T24/T45 local part), bootstrap (P-BOOT), claim (T21 claim part, R-12).

Codex issue 1 (process-lifetime exclusion across DB lock loss and a pre-call
pause) is proved with two real processes in ``test_exp1_process_lock.py``; the
tests here cover the in-process contract.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from haloflow_demo.ownership import NullProcessLock, OwnerLost, acquire_ownership
from haloflow_demo.process_lock import LOCK_PATH, ProcessLock
from haloflow_demo.reminders import claim_reminder
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url
from haloflow_demo.synthetic_ehr import ApptStatus, SyntheticAppointment

from .harness import terminate_owner_backend


def _settings(env: dict[str, str]) -> DemoSettings:
    return DemoSettings.from_env(env)


async def _own(env: dict[str, str], events: list[str] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    s = _settings(env)
    return await acquire_ownership(s, validate_demo_database_url(s), events=events, **kw)


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


# ── P-BOOT + epoch ────────────────────────────────────────────────────────────


async def test_p_boot_fresh_database_bootstraps_under_ownership(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    events: list[str] = []
    own = await _own(demo_env, events)
    try:
        assert own.epoch == 1
        assert _q(demo_db_url, "SELECT id, paused, owner_epoch FROM demo_settings") == [
            (1, False, 1)
        ]
        assert events.index("process_lock") < events.index("db_lock") < events.index("tables")
        assert events.index("tables") < events.index("epoch") < events.index("recovery")
    finally:
        await own.close()
    own2 = await _own(demo_env)
    try:
        assert own2.epoch == 2
    finally:
        await own2.close()


# ── T05: second instance refused ──────────────────────────────────────────────


async def test_t05_second_instance_refused_by_process_lock(demo_env: dict[str, str]) -> None:
    own = await _own(demo_env)
    try:
        with pytest.raises(StartupRefused) as exc:
            await _own(demo_env)
        assert exc.value.reason == "process_lock_held"
        async with own.fenced():
            pass  # first unaffected
    finally:
        await own.close()


async def test_t05_db_advisory_lock_is_a_second_layer(demo_env: dict[str, str]) -> None:
    own = await _own(demo_env)
    try:
        with pytest.raises(StartupRefused) as exc:
            await _own(demo_env, process_lock=NullProcessLock())
        assert exc.value.reason == "db_lock_held"
        assert own.epoch == 1
    finally:
        await own.close()


async def test_t05_refused_instance_does_not_bump_epoch(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own = await _own(demo_env)
    try:
        with pytest.raises(StartupRefused):
            await _own(demo_env)
        with pytest.raises(StartupRefused):
            await _own(demo_env, process_lock=NullProcessLock())
        assert _q(demo_db_url, "SELECT owner_epoch FROM demo_settings") == [(1,)]
    finally:
        await own.close()


# ── process lock file semantics ───────────────────────────────────────────────


def test_process_lock_never_unlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "locks" / "x.lock"

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("ProcessLock must never unlink its file")

    monkeypatch.setattr(os, "unlink", _boom)
    monkeypatch.setattr(os, "remove", _boom)
    lock = ProcessLock(path)
    lock.acquire()
    lock.release()
    assert path.exists()
    lock2 = ProcessLock(path)
    lock2.acquire()
    lock2.release()


def test_process_lock_second_holder_refused_until_release(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "y.lock"
    a, b = ProcessLock(path), ProcessLock(path)
    a.acquire()
    with pytest.raises(StartupRefused) as exc:
        b.acquire()
    assert exc.value.reason == "process_lock_held"
    a.release()
    b.acquire()
    b.release()


def test_r2_lock_location_is_one_fixed_host_wide_path() -> None:
    # Not a setting and not derived from the database URL (correction R2).
    assert Path("/tmp/haloflow-demo-locks/reminders-demo.lock") == LOCK_PATH
    assert ProcessLock().path == LOCK_PATH
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@127.0.0.1/haloflow_x_demo",
                               "DEMO_LOCK_DIR": "/somewhere/else"})
    assert not any("lock" in name for name in vars(s))


def test_r2_advisory_key_is_identical_for_equivalent_endpoints() -> None:
    from haloflow_demo.ownership import advisory_key

    def t(url: str):  # type: ignore[no-untyped-def]
        return validate_demo_database_url(DemoSettings.from_env({"DEMO_DATABASE_URL": url}))

    keys = {
        advisory_key(t("postgresql://a@127.0.0.1:5433/haloflow_x_demo")),
        advisory_key(t("postgresql://a@localhost:5433/haloflow_x_demo")),
        advisory_key(t("postgresql://a@/haloflow_x_demo?host=/tmp&port=5433")),
    }
    assert len(keys) == 1


def test_r2_symlinked_lock_file_refused(tmp_path: Path) -> None:
    d = tmp_path / "locks"
    d.mkdir(mode=0o700)
    (d / "z.lock").symlink_to(tmp_path / "elsewhere.lock")
    with pytest.raises(StartupRefused) as exc:
        ProcessLock(d / "z.lock").acquire()
    assert exc.value.reason == "process_lock_unavailable"


def test_r2_group_accessible_lock_dir_refused(tmp_path: Path) -> None:
    d = tmp_path / "open"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(StartupRefused) as exc:
        ProcessLock(d / "w.lock").acquire()
    assert exc.value.reason == "process_lock_unavailable"


def test_r2_symlinked_lock_dir_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(StartupRefused) as exc:
        ProcessLock(link / "v.lock").acquire()
    assert exc.value.reason == "process_lock_unavailable"


# ── R-4 fencing ───────────────────────────────────────────────────────────────


async def test_r4_fenced_transaction_aborts_after_epoch_change(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own = await _own(demo_env)
    try:
        _q(demo_db_url, "UPDATE demo_settings SET owner_epoch = owner_epoch + 1 WHERE id = 1")
        with pytest.raises(OwnerLost):
            async with own.fenced():
                pass
    finally:
        await own.close()


async def test_r4_fenced_write_rolls_back_on_owner_lost(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from sqlalchemy import text

    own = await _own(demo_env)
    try:
        _q(demo_db_url, "UPDATE demo_settings SET owner_epoch = 99 WHERE id = 1")
        with pytest.raises(OwnerLost):
            async with own.fenced() as conn:
                await conn.execute(text("UPDATE demo_settings SET paused = true WHERE id = 1"))
        assert _q(demo_db_url, "SELECT paused FROM demo_settings") == [(False,)]
    finally:
        await own.close()


# ── T08: lock loss ────────────────────────────────────────────────────────────


async def test_t08_lock_backend_terminated_sets_owner_lost_within_interval(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own = await _own(demo_env)
    try:
        own.start_watchdog(interval=2.0)
        t0 = time.monotonic()
        terminate_owner_backend(demo_db_url, own.lock_backend_pid)
        await asyncio.wait_for(own.lost.wait(), timeout=3.0)
        assert time.monotonic() - t0 <= 3.0
        assert own.owner_lost is True
        with pytest.raises(OwnerLost):
            async with own.fenced():
                pass
    finally:
        await own.close()


# ── T06 + shutdown ordering ───────────────────────────────────────────────────


async def test_t06_close_releases_process_lock_last(demo_env: dict[str, str]) -> None:
    events: list[str] = []
    own = await _own(demo_env, events)
    drained: list[str] = []

    async def _drain() -> None:
        drained.append("drained")
        events.append("drain")

    await own.close(drain=_drain)
    assert drained == ["drained"]
    tail = events[events.index("drain"):]
    assert tail == ["drain", "db_lock_released", "process_lock_released"]


# ── R-13 recovery (local part of T24/T45) ─────────────────────────────────────


async def test_r13_recovery_marks_claimed_and_in_flight_uncertain(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own = await _own(demo_env)
    await own.close()
    _q(
        demo_db_url,
        "INSERT INTO reminder_records(tenant_id, emr_appt_id, emr_patient_id, appt_start, state)"
        " VALUES ('t', 'a1', 'p1', now(), 'CLAIMED'), ('t', 'a2', 'p2', now(), 'SUBMITTED')",
    )
    rid = _q(demo_db_url, "SELECT id FROM reminder_records WHERE emr_appt_id='a2'")[0][0]
    _q(
        demo_db_url,
        "INSERT INTO writeback_tasks(reminder_id, state, target_value)"
        " VALUES (%s, 'IN_FLIGHT', '2')",
        (rid,),
    )
    own = await _own(demo_env)
    try:
        rows = _q(demo_db_url,
                  "SELECT emr_appt_id, state, reason FROM reminder_records ORDER BY emr_appt_id")
        assert rows == [
            ("a1", "UNCERTAIN", "restart_after_claim"),
            ("a2", "SUBMITTED", None),
        ]
        assert _q(demo_db_url, "SELECT state, reason FROM writeback_tasks") == [
            ("UNCERTAIN", "restart")
        ]
    finally:
        await own.close()


# ── R-12 claim (T21 claim part) ───────────────────────────────────────────────


async def test_t21_concurrent_claims_yield_one_claimed_row(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own = await _own(demo_env)
    appt = SyntheticAppointment("a1", "p1", datetime(2026, 10, 28, 14, tzinfo=UTC),
                                ApptStatus.SCHEDULED)
    try:
        results = await asyncio.gather(*(claim_reminder(own, "t", appt) for _ in range(5)))
        assert sum(1 for r in results if r is not None) == 1
        assert _q(demo_db_url, "SELECT emr_appt_id, state FROM reminder_records") == [
            ("a1", "CLAIMED")
        ]
    finally:
        await own.close()


async def test_t21_claim_is_fenced(demo_env: dict[str, str], demo_db_url: str) -> None:
    own = await _own(demo_env)
    appt = SyntheticAppointment("a1", "p1", datetime(2026, 10, 28, 14, tzinfo=UTC),
                                ApptStatus.SCHEDULED)
    try:
        _q(demo_db_url, "UPDATE demo_settings SET owner_epoch = 99 WHERE id = 1")
        with pytest.raises(OwnerLost):
            await claim_reminder(own, "t", appt)
        assert _q(demo_db_url, "SELECT count(*) FROM reminder_records") == [(0,)]
    finally:
        await own.close()


# ── Codex checkpoint-1 lifecycle detail: Ownership itself enforces drain ──────


async def test_close_refuses_to_release_while_guarded_work_runs(
    demo_env: dict[str, str],
) -> None:
    from haloflow_demo.ownership import DrainIncomplete

    own = await _own(demo_env)
    release = asyncio.Event()

    async def _blocked() -> str:
        await release.wait()
        return "ok"

    call = asyncio.get_running_loop().create_task(own.guard_external(_blocked))
    await asyncio.sleep(0.05)
    assert own.inflight == 1
    with pytest.raises(DrainIncomplete):
        await own.close(drain_timeout=0.2, cancel_grace=0.0, cancel=False)
    # the locks are still held: a second owner is refused by the process lock
    with pytest.raises(StartupRefused) as exc:
        await _own(demo_env)
    assert exc.value.reason == "process_lock_held"
    release.set()
    assert await call == "ok"
    await own.close()
    own2 = await _own(demo_env)
    await own2.close()


async def test_fenced_refused_after_close(demo_env: dict[str, str]) -> None:
    own = await _own(demo_env)
    await own.close()
    with pytest.raises(OwnerLost):
        async with own.fenced():
            pass


async def test_fenced_still_allowed_while_closing_for_outcome_records(
    demo_env: dict[str, str],
) -> None:
    own = await _own(demo_env)
    try:
        own.begin_closing()
        async with own.fenced():
            pass  # an in-flight call may still record its outcome during drain
    finally:
        await own.close()


# ── lock file kind and unknown holders (Codex direction alignment, R2) ─────────


def test_r2_non_regular_lock_file_refused(tmp_path: Path) -> None:
    d = tmp_path / "locks"
    d.mkdir(mode=0o700)
    os.mkfifo(d / "fifo.lock")
    with pytest.raises(StartupRefused) as exc:
        ProcessLock(d / "fifo.lock").acquire()
    assert exc.value.reason == "process_lock_unavailable"


def test_r2_unknown_external_holder_is_refused_not_killed() -> None:
    import subprocess
    import sys

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         f"os.makedirs({str(LOCK_PATH.parent)!r}, mode=0o700, exist_ok=True)\n"
         f"fd = os.open({str(LOCK_PATH)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(30)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(StartupRefused) as exc:
            ProcessLock().acquire()
        assert exc.value.reason == "process_lock_held"
        assert holder.poll() is None, "the demo must never kill an unknown holder"
    finally:
        holder.kill()
        holder.wait(timeout=10)
