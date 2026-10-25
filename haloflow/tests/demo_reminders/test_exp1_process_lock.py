"""Experiment 1 — Codex issue 1: single-host process-lifetime exclusion.

Sequence Codex gave (codex_package-v2-and-pr37-review-2026-10-25.md): A commits
an authorisation at epoch 1; A's lock connection dies; B acquires ownership and
commits epoch 2; A then starts the external call using its earlier
authorisation. Database fencing alone permits this.

Remedy under test: an OS exclusive lock (``fcntl.flock``) held for the whole
process lifetime, keyed to the canonical demo instance, never unlinked. While A
is alive, B is refused even though A's database session is gone.

Causal ordering is enforced by the harness, not inferred from clocks: each step
waits for the previous step's file before acting. Wall-clock times are recorded
as supporting evidence only (same host).

Scope: single host only. This is not a distributed fencing guarantee.

S1 protected race    — lock lost, B tried while A paused (B refused), A then calls.
S2 watchdog first    — lock lost, watchdog fires before A resumes; A refuses the call.
S3 negative control  — process lock disabled: B takes over while A is alive and
                       A's call lands after B's epoch commit. Shows the detector
                       is sensitive and that DB fencing alone is insufficient.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest

HELPER = Path(__file__).parent / "helpers" / "owner_proc.py"
TIMEOUT = 30.0


def _spawn(env: dict[str, str], wd: Path, role: str, *extra: str) -> subprocess.Popen[str]:
    full_env = {**os.environ, **env}
    return subprocess.Popen(
        [sys.executable, str(HELPER), "--role", role, "--workdir", str(wd), *extra],
        env=full_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _await_file(path: Path, proc: subprocess.Popen[str] | None = None) -> dict[str, object]:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())  # type: ignore[no-any-return]
        if proc is not None and proc.poll() is not None and not path.exists():
            out, err = proc.communicate()
            raise AssertionError(f"{path.name} never written; exit {proc.returncode}\n{out}\n{err}")
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {path}")


def _finish(proc: subprocess.Popen[str]) -> tuple[int, str, str]:
    out, err = proc.communicate(timeout=TIMEOUT)
    return proc.returncode, out, err


def _calls(wd: Path) -> list[dict[str, object]]:
    p = wd / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _terminate_backend(db_url: str, pid: object) -> None:
    with psycopg.connect(db_url, autocommit=True) as conn:
        ok = conn.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
    assert ok == (True,)


def _epoch(db_url: str) -> int:
    with psycopg.connect(db_url) as conn:
        row = conn.execute("SELECT owner_epoch FROM demo_settings WHERE id=1").fetchone()
    assert row is not None
    return int(row[0])


def _evidence(wd: Path, name: str, payload: dict[str, object]) -> None:
    out = os.environ.get("DEMO_EVIDENCE_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        Path(out, f"exp1_{name}.json").write_text(json.dumps(payload, indent=2, default=str))


def test_exp1_s1_replacement_refused_while_old_process_alive(
    demo_env: dict[str, str], demo_db_url: str, tmp_path: Path
) -> None:
    wd = tmp_path / "s1"
    wd.mkdir()
    a = _spawn(demo_env, wd, "A", "--watchdog", "60")  # watchdog cannot rescue this run
    a_ready = _await_file(wd / "A.ready", a)
    _await_file(wd / "A.reserved", a)
    _terminate_backend(demo_db_url, a_ready["lock_backend_pid"])

    b = _spawn(demo_env, wd, "B")
    b_code, b_out, _ = _finish(b)
    assert a.poll() is None, "A must still be alive when B is refused"
    assert b_code == 2, b_out
    assert json.loads((wd / "B.refused").read_text())["reason"] == "process_lock_held"
    assert _epoch(demo_db_url) == 1, "refused B must not bump the epoch"

    (wd / "A.go").touch()
    a_code, a_out, a_err = _finish(a)
    calls = _calls(wd)
    assert a_code == 0, a_err
    assert [c["epoch"] for c in calls] == [1]

    b2 = _spawn(demo_env, wd, "B")
    b2_code, _, b2_err = _finish(b2)
    assert b2_code == 0, b2_err
    b2_ready = json.loads((wd / "B.ready").read_text())
    assert b2_ready["epoch"] == 2
    assert all(float(c["t"]) < float(b2_ready["t_owned"]) for c in calls)  # type: ignore[arg-type]
    assert json.loads((wd / "B.state").read_text())["rows"] == [
        ["exp1-appt", "SUBMITTED", None]
    ]
    assert len(_calls(wd)) == 1, "B made no provider call"
    b_refused = json.loads((wd / "B.refused").read_text())
    _evidence(wd, "s1", {"a_ready": a_ready, "b_refused": b_refused,
                         "calls": calls, "b2_ready": b2_ready,
                         "b2_state": json.loads((wd / "B.state").read_text()),
                         "exit_codes": {"A": a_code, "B": b_code, "B2": b2_code}})


def test_exp1_s2_watchdog_stops_call_after_lock_loss(
    demo_env: dict[str, str], demo_db_url: str, tmp_path: Path
) -> None:
    wd = tmp_path / "s2"
    wd.mkdir()
    a = _spawn(demo_env, wd, "A", "--watchdog", "0.5")
    a_ready = _await_file(wd / "A.ready", a)
    _await_file(wd / "A.reserved", a)
    _terminate_backend(demo_db_url, a_ready["lock_backend_pid"])
    time.sleep(1.5)  # > 2 watchdog intervals
    (wd / "A.go").touch()
    a_code, _, a_err = _finish(a)
    assert a_code == 3, a_err
    assert json.loads((wd / "A.outcome").read_text())["outcome"] == "refused_owner_lost"
    assert _calls(wd) == []

    b = _spawn(demo_env, wd, "B")
    b_code, _, b_err = _finish(b)
    assert b_code == 0, b_err
    assert json.loads((wd / "B.state").read_text())["rows"] == [
        ["exp1-appt", "UNCERTAIN", "restart_after_claim"]
    ]
    assert _calls(wd) == [], "recovery makes no provider call"
    _evidence(wd, "s2", {"a_ready": a_ready,
                         "a_outcome": json.loads((wd / "A.outcome").read_text()),
                         "b_state": json.loads((wd / "B.state").read_text()),
                         "calls": [], "exit_codes": {"A": a_code, "B": b_code}})


def test_exp1_s3_negative_control_db_fencing_alone_permits_the_race(
    demo_env: dict[str, str], demo_db_url: str, tmp_path: Path
) -> None:
    wd = tmp_path / "s3"
    wd.mkdir()
    a = _spawn(demo_env, wd, "A", "--watchdog", "60", "--no-process-lock")
    a_ready = _await_file(wd / "A.ready", a)
    _await_file(wd / "A.reserved", a)
    _terminate_backend(demo_db_url, a_ready["lock_backend_pid"])

    b = _spawn(demo_env, wd, "B", "--no-process-lock", "--hold")
    b_ready = _await_file(wd / "B.ready", b)
    assert a.poll() is None
    assert b_ready["epoch"] == 2, "without the process lock B takes over while A is alive"

    (wd / "A.go").touch()  # only after B.ready: causally after B's epoch commit
    a_code, _, _ = _finish(a)
    calls = _calls(wd)
    (wd / "B.go").touch()
    b_code, _, b_err = _finish(b)
    assert b_code == 0, b_err

    # The violation the remedy prevents: an epoch-1 call after the epoch-2 commit.
    assert [c["epoch"] for c in calls] == [1]
    assert float(calls[0]["t"]) > float(b_ready["t_owned"])  # type: ignore[arg-type]
    # A's follow-up fenced write is rejected, which is all DB fencing can do.
    assert a_code == 3
    assert json.loads((wd / "A.outcome").read_text())["outcome"] == "called_then_owner_lost"
    _evidence(wd, "s3", {"a_ready": a_ready, "b_ready": b_ready, "calls": calls,
                         "a_outcome": json.loads((wd / "A.outcome").read_text()),
                         "exit_codes": {"A": a_code, "B": b_code}})


def test_exp1_lock_file_survives_and_is_reused(
    demo_env: dict[str, str], tmp_path: Path
) -> None:
    wd = tmp_path / "s4"
    wd.mkdir()
    for _ in range(2):
        b = _spawn(demo_env, wd, "B")
        code, _, err = _finish(b)
        assert code == 0, err
        (wd / "B.ready").unlink()
        (wd / "B.state").unlink()
    locks = list(Path(demo_env["DEMO_LOCK_DIR"]).glob("*.lock"))
    assert len(locks) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock only")
def test_exp1_lock_released_by_kernel_when_holder_killed(
    demo_env: dict[str, str], demo_db_url: str, tmp_path: Path
) -> None:
    wd = tmp_path / "s5"
    wd.mkdir()
    a = _spawn(demo_env, wd, "A", "--watchdog", "60")
    _await_file(wd / "A.reserved", a)
    a.kill()
    a.wait(timeout=TIMEOUT)
    b = _spawn(demo_env, wd, "B")
    code, _, err = _finish(b)
    assert code == 0, err
    assert json.loads((wd / "B.state").read_text())["rows"] == [
        ["exp1-appt", "UNCERTAIN", "restart_after_claim"]
    ]
    assert _calls(wd) == []
