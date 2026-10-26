"""Correction R3 (Codex finding 3): full runner lifecycle with real listeners.

The child runs the real ``haloflow_demo.app.run`` (both uvicorn servers) via
``helpers/runner_proc.py``. Order required for every shutdown:

  ownership + recovery → listeners → [SIGTERM | owner lost] → closing (new
  guarded calls refused) → listeners stopped → drain → DB lock released →
  process lock released

If drain cannot confirm that all external work has finished, the process lock
is never released explicitly: the process exits 4 and the kernel releases the
lock at death.

L1 graceful drain with a blocked outbound call: real HTTP served; after SIGTERM
   the probe is refused, both ports refuse connections, a replacement is
   refused while A drains, A releases only after the call finishes.
L2 lock loss: watchdog detects it; same shutdown order; exit 3.
L3 drain cannot finish (call ignores cancellation): exit 4; no explicit lock
   release; replacement refused until A is dead, accepted after.
T02 configured start-up with pending CLAIMED / IN_FLIGHT rows: recovery runs,
   the only outbound connections are to the demo database, 0 calls.

Day 5 (Codex 20261025T172354Z, P1 shutdown lifecycle). The reply dispatcher is
inside the bounded drain:
L4 SIGTERM while the dispatcher's baseline EHR read is suspended: listeners
   stop; a replacement is refused while A drains; the drain timeout cancels the
   read; bounded exit 0; no effect committed.
L5 SIGTERM while the dispatcher's effect transaction waits on a targeted DB
   lock (the match lock held by the test): bounded exit; no effect committed,
   even after the test releases its lock.
L6 the baseline read ignores cancellation: exit 4, no explicit lock release,
   replacement refused until A is dead; no effect committed.
L7 SIGTERM while an ADMITTED effect transaction is paused before commit: it
   must end (cancelled by the bounded drain) before the DB lock and the process
   lock are released; no effect committed.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from .harness import demo_subprocess_env, terminate_owner_backend, verify_owner_backend

RUNNER = Path(__file__).parent / "helpers" / "runner_proc.py"
OWNER = Path(__file__).parent / "helpers" / "owner_proc.py"
TIMEOUT = 30.0
ORDER = ["recovery", "listening", "closing", "listeners_stopped", "drained",
         "db_lock_released", "process_lock_released"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Children:
    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[str]] = []

    def start(self, script: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
        p = subprocess.Popen([sys.executable, str(script), *args],
                             env=demo_subprocess_env({**os.environ, **env}),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            with contextlib.suppress(Exception):
                p.communicate(timeout=TIMEOUT)


@pytest.fixture
def children() -> Iterator[Children]:
    c = Children()
    try:
        yield c
    finally:
        c.cleanup()


@pytest.fixture
def run_env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env,
            "DEMO_STAFF_BIND": f"127.0.0.1:{_free_port()}",
            "DEMO_CALLBACK_BIND": f"127.0.0.1:{_free_port()}"}


def _lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _events(wd: Path) -> list[str]:
    return [str(e["event"]) for e in _lines(wd / "events.jsonl")]


def _wait(pred, proc: subprocess.Popen[str], what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if pred():
            return
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"exited {proc.returncode} before {what}\n{out}\n{err}")
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def _port(env: dict[str, str], key: str) -> int:
    return int(env[key].rsplit(":", 1)[1])


def _refuses(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except OSError:
        return True


def _assert_order(events: list[str], expected: list[str]) -> None:
    positions = [events.index(e) for e in expected]
    assert positions == sorted(positions), events


def _replacement(
    children: Children, env: dict[str, str], wd: Path
) -> tuple[int, dict[str, object]]:
    b = children.start(OWNER, env, "--role", "B", "--workdir", str(wd))
    out, err = b.communicate(timeout=TIMEOUT)
    refused = wd / "B.refused"
    return b.returncode, (json.loads(refused.read_text()) if refused.exists() else {"out": out,
                                                                                    "err": err})


def _evidence(name: str, wd: Path, extra: dict[str, object]) -> None:
    out = os.environ.get("DEMO_EVIDENCE_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        payload = {"events": _lines(wd / "events.jsonl"), "calls": _lines(wd / "calls.jsonl"),
                   "probe": _lines(wd / "probe.jsonl"), "connects": _lines(wd / "connects.jsonl"),
                   **extra}
        Path(out, f"lifecycle_{name}.json").write_text(json.dumps(payload, indent=2, default=str))


def test_l1_graceful_drain_with_blocked_call(
    run_env: dict[str, str], tmp_path: Path, children: Children
) -> None:
    wd = tmp_path / "l1"
    wd.mkdir()
    a = children.start(RUNNER, run_env, "--workdir", str(wd), "--blocked")
    _wait(lambda: "listening" in _events(wd)
          and any(c.get("phase") == "started" for c in _lines(wd / "calls.jsonl")), a, "listening")
    staff, cb = _port(run_env, "DEMO_STAFF_BIND"), _port(run_env, "DEMO_CALLBACK_BIND")
    with urllib.request.urlopen(f"http://127.0.0.1:{staff}/demo/staff", timeout=5) as r:
        assert r.status == 200
        assert "Local demonstrator" in r.read().decode()

    a.send_signal(signal.SIGTERM)
    _wait(lambda: "listeners_stopped" in _events(wd), a, "listeners_stopped")
    _wait(lambda: bool(_lines(wd / "probe.jsonl")), a, "probe")
    assert _lines(wd / "probe.jsonl")[0]["result"] == "refused"
    assert a.poll() is None, "A is still draining"
    assert _refuses(staff) and _refuses(cb), "listeners stop before the lock is released"
    assert "process_lock_released" not in _events(wd)

    b_code, b_info = _replacement(children, run_env, wd)
    assert b_code == 2 and b_info.get("reason") == "process_lock_held", b_info

    (wd / "release").touch()
    a_out, a_err = a.communicate(timeout=TIMEOUT)
    assert a.returncode == 0, a_err
    events = _events(wd)
    _assert_order(events, ORDER)
    phases = [c["phase"] for c in _lines(wd / "calls.jsonl")]
    assert phases == ["started", "finished"], phases
    (wd / "B.ready").unlink(missing_ok=True)
    b2_code, b2_info = _replacement(children, run_env, wd)
    assert b2_code == 0, b2_info
    _evidence("l1", wd, {"a_exit": a.returncode, "b_refused": b_info, "b2_exit": b2_code})


def test_l2_lock_loss_shuts_down_in_order_with_nonzero_exit(
    run_env: dict[str, str], demo_db_url: str, tmp_path: Path, children: Children
) -> None:
    from haloflow_demo.ownership import DEMO_ADVISORY_KEY

    admin_url = os.environ["DEMO_TEST_ADMIN_URL"]
    # Unrelated advisory-lock holders that must survive: the same key in another
    # database, and another key in the demo database.
    other_db = psycopg.connect(admin_url, autocommit=True)
    other_db.execute("SELECT pg_advisory_lock(%s)", (DEMO_ADVISORY_KEY,))
    other_pid = other_db.execute("SELECT pg_backend_pid()").fetchone()
    same_db = psycopg.connect(demo_db_url, autocommit=True)
    try:
        wd = tmp_path / "l2"
        wd.mkdir()
        a = children.start(RUNNER, run_env, "--workdir", str(wd), "--watchdog", "0.3")
        _wait(lambda: "listening" in _events(wd) and (wd / "owner.json").exists(), a, "listening")
        same_db.execute("SELECT pg_advisory_lock(%s)", (12345,))
        same_pid = same_db.execute("SELECT pg_backend_pid()").fetchone()
        owner = json.loads((wd / "owner.json").read_text())
        target_pid = int(owner["lock_backend_pid"])
        # The old selection (first granted advisory lock in the cluster) is
        # ambiguous here: it sees at least three holders.
        with psycopg.connect(admin_url) as conn:
            holders = conn.execute(
                "SELECT count(DISTINCT pid) FROM pg_locks WHERE locktype='advisory' AND granted"
            ).fetchone()
        assert holders is not None and holders[0] >= 3
        assert other_pid is not None and same_pid is not None
        assert target_pid not in (other_pid[0], same_pid[0])
        # The check discriminates: both unrelated holders are rejected.
        for unrelated in (other_pid[0], same_pid[0]):
            with pytest.raises(AssertionError):
                verify_owner_backend(demo_db_url, unrelated)
        terminate_owner_backend(demo_db_url, target_pid)
        _, a_err = a.communicate(timeout=TIMEOUT)
        assert a.returncode == 3, a_err
        events = _events(wd)
        assert "owner_lost" in events
        _assert_order(events, ["listening", "owner_lost", "closing", "listeners_stopped",
                               "drained", "db_lock_released", "process_lock_released"])
        assert _lines(wd / "probe.jsonl")[0]["result"] == "refused"
        assert _lines(wd / "calls.jsonl") == []
        # The unrelated sessions are alive and still hold their locks.
        assert other_db.execute("SELECT 1").fetchone() == (1,)
        assert same_db.execute("SELECT 1").fetchone() == (1,)
        _evidence("l2", wd, {"a_exit": a.returncode, "terminated_pid": target_pid,
                             "unrelated_pids_survived": [other_pid[0], same_pid[0]],
                             "advisory_holders_seen": holders[0]})
    finally:
        other_db.close()
        same_db.close()


def test_l3_drain_incomplete_never_releases_lock_explicitly(
    run_env: dict[str, str], tmp_path: Path, children: Children
) -> None:
    wd = tmp_path / "l3"
    wd.mkdir()
    a = children.start(RUNNER, run_env, "--workdir", str(wd), "--blocked", "--stubborn",
                       "--drain-timeout", "3", "--cancel-grace", "2")
    _wait(lambda: "listening" in _events(wd)
          and any(c.get("phase") == "started" for c in _lines(wd / "calls.jsonl")), a, "listening")
    a.send_signal(signal.SIGTERM)
    _wait(lambda: "listeners_stopped" in _events(wd), a, "listeners_stopped")
    b_code, b_info = _replacement(children, run_env, wd)
    assert b_code == 2 and b_info.get("reason") == "process_lock_held", b_info
    _, a_err = a.communicate(timeout=TIMEOUT)
    assert a.returncode == 4, a_err
    events = _events(wd)
    assert "drain_incomplete" in events
    assert "db_lock_released" not in events
    assert "process_lock_released" not in events
    phases = [c["phase"] for c in _lines(wd / "calls.jsonl")]
    assert "cancel_received" in phases and "finished" not in phases
    (wd / "B.refused").unlink(missing_ok=True)
    b2_code, b2_info = _replacement(children, run_env, wd)
    assert b2_code == 0, b2_info  # kernel released the lock when A died
    _evidence("l3", wd, {"a_exit": a.returncode, "b_refused": b_info, "b2_exit": b2_code})


def test_t02_configured_startup_recovers_and_contacts_only_the_demo_db(
    run_env: dict[str, str], demo_db_url: str, tmp_path: Path, children: Children
) -> None:
    # Prepare a database with pending work from a "previous run".
    wd0 = tmp_path / "prep"
    wd0.mkdir()
    prep = children.start(OWNER, run_env, "--role", "B", "--workdir", str(wd0))
    assert prep.wait(timeout=TIMEOUT) == 0
    with psycopg.connect(demo_db_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO reminder_records(tenant_id, emr_appt_id, emr_patient_id, appt_start,"
            " state) VALUES ('t','a1','p1',now(),'CLAIMED'), ('t','a2','p2',now(),'SUBMITTED')"
        )
        rid = conn.execute("SELECT id FROM reminder_records WHERE emr_appt_id='a2'").fetchone()
        assert rid is not None
        conn.execute("INSERT INTO writeback_tasks(reminder_id, state, target_value)"
                     " VALUES (%s,'IN_FLIGHT','2')", (rid[0],))

    wd = tmp_path / "t02"
    wd.mkdir()
    a = children.start(RUNNER, run_env, "--workdir", str(wd))
    _wait(lambda: "listening" in _events(wd), a, "listening")
    a.send_signal(signal.SIGTERM)
    _, a_err = a.communicate(timeout=TIMEOUT)
    assert a.returncode == 0, a_err
    _assert_order(_events(wd), ["process_lock", "db_lock", "epoch", "recovery", "listening"])
    with psycopg.connect(demo_db_url) as conn:
        states = conn.execute(
            "SELECT emr_appt_id, state, reason FROM reminder_records ORDER BY emr_appt_id"
        ).fetchall()
        wb = conn.execute("SELECT state, reason FROM writeback_tasks").fetchall()
    assert states == [("a1", "UNCERTAIN", "restart_after_claim"), ("a2", "SUBMITTED", None)]
    assert wb == [("UNCERTAIN", "restart")]
    db_port = str(psycopg.conninfo.conninfo_to_dict(demo_db_url).get("port", "5432"))
    addresses = {str(c["address"]) for c in _lines(wd / "connects.jsonl")}
    assert addresses, "the runner must have connected to the database"
    assert all(f"'127.0.0.1', {db_port}" in a for a in addresses), addresses
    assert _lines(wd / "calls.jsonl") == []
    _evidence("t02", wd, {"a_exit": a.returncode, "states": states, "writeback": wb})


# ── Day 5: the reply dispatcher is inside the bounded drain ───────────────────


@pytest.fixture
def dispatch_env(run_env: dict[str, str]) -> dict[str, str]:
    return {**run_env, "DEMO_DISPATCHER": "on", "DEMO_SMS_TEST_NUMBER": "+15550100199",
            "DEMO_SMS_SENDER": "+15550100100",
            "DEMO_CALLBACK_SECRET": "lifecycle-test-secret-0123456789"}


def _phases(wd: Path) -> list[str]:
    return [str(c["phase"]) for c in _lines(wd / "calls.jsonl")]


def _no_effect(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        assert conn.execute("SELECT state FROM inbound_messages").fetchall() == [("RECEIVED",)]
        assert conn.execute("SELECT state FROM reminder_records").fetchall() == [("SUBMITTED",)]
        assert conn.execute("SELECT count(*) FROM writeback_tasks").fetchone() == (0,)


def test_l4_shutdown_during_suspended_baseline_read_is_bounded(
    dispatch_env: dict[str, str], demo_db_url: str, tmp_path: Path, children: Children
) -> None:
    wd = tmp_path / "l4"
    wd.mkdir()
    a = children.start(RUNNER, dispatch_env, "--workdir", str(wd), "--seed-reply",
                       "--ehr-read", "block", "--drain-timeout", "2", "--cancel-grace", "2")
    _wait(lambda: "ehr_read_started" in _phases(wd), a, "baseline read started")
    t0 = time.monotonic()
    a.send_signal(signal.SIGTERM)
    _wait(lambda: "listeners_stopped" in _events(wd), a, "listeners_stopped")
    b_code, b_info = _replacement(children, dispatch_env, wd)
    assert b_code == 2 and b_info.get("reason") == "process_lock_held", b_info
    _, a_err = a.communicate(timeout=TIMEOUT)
    elapsed = time.monotonic() - t0
    assert a.returncode == 0, a_err
    assert elapsed < 15, elapsed
    events = _events(wd)
    _assert_order(events, ORDER)
    assert "ehr_read_cancel_received" in _phases(wd)
    assert "ehr_read_finished" not in _phases(wd)
    _no_effect(demo_db_url)
    _evidence("l4", wd, {"a_exit": a.returncode, "elapsed_s": elapsed, "b_refused": b_info})


def _wait_blocked_by(url: str, waiter_tag: str, holder_pid: int,
                     proc: subprocess.Popen[str]) -> None:
    def blocked() -> bool:
        with psycopg.connect(url, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT pg_blocking_pids(pid) FROM pg_stat_activity WHERE datname ="
                " current_database() AND wait_event_type = 'Lock' AND position(%s in query) > 0",
                (waiter_tag,)).fetchall()
        return any(holder_pid in r[0] for r in rows)
    _wait(blocked, proc, f"{waiter_tag} blocked by {holder_pid}")


def test_l5_shutdown_while_effect_waits_on_db_lock_is_bounded(
    dispatch_env: dict[str, str], demo_db_url: str, tmp_path: Path, children: Children
) -> None:
    wd = tmp_path / "l5"
    wd.mkdir()
    a = children.start(RUNNER, dispatch_env, "--workdir", str(wd), "--seed-reply",
                       "--ehr-read", "ok", "--drain-timeout", "2", "--cancel-grace", "2")
    _wait(lambda: "seeded" in _phases(wd), a, "seeded")
    holder = psycopg.connect(demo_db_url)
    try:
        holder.execute("SELECT 1 FROM demo_match_lock WHERE id = 1 FOR UPDATE")
        _wait_blocked_by(demo_db_url, "match:effect", holder.info.backend_pid, a)
        t0 = time.monotonic()
        a.send_signal(signal.SIGTERM)
        _wait(lambda: "listeners_stopped" in _events(wd), a, "listeners_stopped")
        b_code, b_info = _replacement(children, dispatch_env, wd)
        assert b_code == 2 and b_info.get("reason") == "process_lock_held", b_info
        _, a_err = a.communicate(timeout=TIMEOUT)
        elapsed = time.monotonic() - t0
    finally:
        holder.rollback()
        holder.close()
    assert a.returncode == 0, a_err
    assert elapsed < 15, elapsed
    _assert_order(_events(wd), ORDER)
    time.sleep(0.5)  # after the test's lock is gone, nothing may commit late
    _no_effect(demo_db_url)
    _evidence("l5", wd, {"a_exit": a.returncode, "elapsed_s": elapsed, "b_refused": b_info})


def test_l6_cancellation_ignoring_baseline_read_exits_4_without_release(
    dispatch_env: dict[str, str], demo_db_url: str, tmp_path: Path, children: Children
) -> None:
    wd = tmp_path / "l6"
    wd.mkdir()
    a = children.start(RUNNER, dispatch_env, "--workdir", str(wd), "--seed-reply",
                       "--ehr-read", "stubborn", "--drain-timeout", "2", "--cancel-grace", "2")
    _wait(lambda: "ehr_read_started" in _phases(wd), a, "baseline read started")
    a.send_signal(signal.SIGTERM)
    _wait(lambda: "listeners_stopped" in _events(wd), a, "listeners_stopped")
    b_code, b_info = _replacement(children, dispatch_env, wd)
    assert b_code == 2 and b_info.get("reason") == "process_lock_held", b_info
    _, a_err = a.communicate(timeout=TIMEOUT)
    assert a.returncode == 4, a_err
    events = _events(wd)
    assert "drain_incomplete" in events
    assert "db_lock_released" not in events and "process_lock_released" not in events
    assert "ehr_read_cancel_received" in _phases(wd)
    _no_effect(demo_db_url)
    (wd / "B.refused").unlink(missing_ok=True)
    b2_code, b2_info = _replacement(children, dispatch_env, wd)
    assert b2_code == 0, b2_info  # the kernel released the lock when A died
    _evidence("l6", wd, {"a_exit": a.returncode, "b_refused": b_info, "b2_exit": b2_code})


def _t(wd: Path, name: str, key: str, value: str) -> float:
    for rec in _lines(wd / name):
        if rec.get(key) == value:
            return float(str(rec["t"]))
    raise AssertionError(f"{value} not recorded")


def test_l7_admitted_effect_ends_before_locks_are_released(
    dispatch_env: dict[str, str], demo_db_url: str, tmp_path: Path, children: Children
) -> None:
    wd = tmp_path / "l7"
    wd.mkdir()
    a = children.start(RUNNER, dispatch_env, "--workdir", str(wd), "--seed-reply",
                       "--ehr-read", "ok", "--effect-pause", "--drain-timeout", "2",
                       "--cancel-grace", "2")
    _wait(lambda: "effect_paused" in _phases(wd), a, "effect paused before commit")
    a.send_signal(signal.SIGTERM)
    _, a_err = a.communicate(timeout=TIMEOUT)
    assert a.returncode == 0, a_err
    events = _events(wd)
    _assert_order(events, ORDER)
    assert "effect_cancel_received" in _phases(wd), _phases(wd)
    ended = _t(wd, "calls.jsonl", "phase", "effect_cancel_received")
    assert ended < _t(wd, "events.jsonl", "event", "db_lock_released")
    assert ended < _t(wd, "events.jsonl", "event", "process_lock_released")
    _no_effect(demo_db_url)
    _evidence("l7", wd, {"a_exit": a.returncode})
