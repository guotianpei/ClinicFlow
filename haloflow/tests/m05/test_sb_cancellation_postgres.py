"""L-1S package S-B — cancellation of `run_once` (real `task.cancel()`, PostgreSQL 17).

Governing: requirements v1r1 R-SB-6c; architecture v1 section 2; pins P4 item 6; Codex
interface review 08:46 ET items 2/3. The runner never swallows CancelledError: run_once
raises it; no further close is invoked after the cancellation; the run log line is written
with code CANCELLED and partial counts incl. `cancelled` (the attempted window that has no
CloseResult) and `commit_unknown` (post-marker).

- SB-C01  cancel while the selection context is being resolved (before any selection) ->
          CancelledError; no close; resume unchanged (None); log CANCELLED attempted=0.
- SB-C02  cancel during the selection transaction -> CancelledError; no close; resume
          unchanged.
- SB-C03  cancel AFTER the S-A body-complete marker of the first close (seam: parked
          inside the open close transaction): S-A turns the CancelledError into
          IntakeCommitUnknown; the runner observes the task's pending cancellation, raises
          CancelledError, invokes NO later close (3 due windows, exactly 1 close); resume =
          that key; log CANCELLED attempted=1 commit_unknown=1 cancelled=0.
- SB-C04  cancel while a close waits on the contact lock (before the marker) -> CancelledError;
          the window is still awaiting; log CANCELLED attempted=1 cancelled=1; after the
          lock is released a new run on the SAME runner closes it (busy cleared, resume =
          that key, wrap reaches it).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import psycopg
import pytest
import sa_support as sa
import sb_runtime as sr
import sb_support as sb

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


def _windows(install: sb.SBInstall, n: int) -> list[str]:
    sb.close_all_awaiting(install)
    reminders = [sb.due_window(install)[2] for _ in range(n)]
    return sr.ordered_due(install, reminders)


def _field(line: str, name: str) -> str:
    match = re.search(rf"\b{name}=(\S*)", line)
    assert match is not None, (name, line)
    return match.group(1)


def _one_line(records: list[logging.LogRecord]) -> str:
    lines = sr.run_lines(records)
    assert len(lines) == 1, lines
    return lines[0]


# --- SB-C01 --------------------------------------------------------------------------------


def test_sb_c01_cancel_before_selection(sb_install: sb.SBInstall,
                                        caplog: pytest.LogCaptureFixture) -> None:
    sb_install.require()
    _windows(sb_install, 2)

    async def body() -> tuple[sr.Gate, Any]:
        async with sr.runtime(sb_install) as rt:
            resolver = sr.FlakyResolver(rt.resolver)
            resolver.park_select = True
            r, gate = sr.runner(sb_install, rt, page=10, cap=10, resolver=resolver)
            task = asyncio.create_task(sr.run(r, sb_install))
            await resolver.parked.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return gate, r.resume_key(sb_install.tenant_id)

    with caplog.at_level(logging.INFO, logger=sr.LOGGER):
        gate, resume = asyncio.run(body())
    assert gate.closes == [] and gate.selects == 0 and resume is None
    line = _one_line(caplog.records)
    assert (_field(line, "code"), _field(line, "attempted")) == ("CANCELLED", "0")


# --- SB-C02 --------------------------------------------------------------------------------


def test_sb_c02_cancel_during_selection(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    _windows(sb_install, 2)

    async def body() -> tuple[sr.Gate, Any]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.park_select_at = 1
            r, _ = sr.runner(sb_install, rt, page=10, cap=10, gate=gate)
            task = asyncio.create_task(sr.run(r, sb_install))
            await gate.select_parked.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return gate, r.resume_key(sb_install.tenant_id)

    gate, resume = asyncio.run(body())
    assert gate.closes == [] and resume is None


# --- SB-C03 --------------------------------------------------------------------------------


def test_sb_c03_cancel_after_body_marker(sb_install: sb.SBInstall,
                                         caplog: pytest.LogCaptureFixture) -> None:
    sb_install.require()
    keys = _windows(sb_install, 3)

    async def body() -> tuple[sr.Gate, Any]:
        async with sr.runtime(sb_install) as rt:
            gate = sr.Gate(rt.gateway)
            gate.park_after_body = keys[0]
            r, _ = sr.runner(sb_install, rt, page=10, cap=10, gate=gate)
            task = asyncio.create_task(sr.run(r, sb_install))
            await gate.parked.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return gate, r.resume_key(sb_install.tenant_id)

    with caplog.at_level(logging.INFO, logger=sr.LOGGER):
        gate, resume = asyncio.run(body())
    assert gate.closes == [keys[0]]                     # no later close was invoked
    assert resume is not None and str(resume.reminder_id) == keys[0]
    line = _one_line(caplog.records)
    assert [_field(line, f) for f in ("code", "attempted", "commit_unknown", "cancelled")] == [
        "CANCELLED", "1", "1", "0"]


# --- SB-C04 --------------------------------------------------------------------------------


def test_sb_c04_cancel_during_lock_wait(sb_install: sb.SBInstall,
                                        caplog: pytest.LogCaptureFixture) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    _, phone, reminder = sb.due_window(sb_install)
    sa.admin(sb_install, sa.q(sb_install, "INSERT INTO {s}.m05_contact_state (phone, opted_out)"
                                          " VALUES (%s, false) ON CONFLICT DO NOTHING"), (phone,))
    holder = psycopg.connect(sb_install.ids.admin)
    holder.execute(sa.q(sb_install, "SELECT 1 FROM {s}.m05_contact_state WHERE phone = %s"
                                    " FOR UPDATE"), (phone,))

    async def body() -> tuple[Any, Any, Any]:
        async with sr.runtime(sb_install) as rt:
            r, gate = sr.runner(sb_install, rt, page=10, cap=10)
            task = asyncio.create_task(sr.run(r, sb_install))
            await asyncio.to_thread(_wait_lock, sb_install)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            resume = r.resume_key(sb_install.tenant_id)
            state = sb.states(sb_install, [reminder])
            holder.rollback()
            again = await sr.run(r, sb_install)
            return resume, state, again

    try:
        with caplog.at_level(logging.INFO, logger=sr.LOGGER):
            resume, state, again = asyncio.run(body())
    finally:
        holder.close()
    assert state == {reminder: "awaiting"}
    assert resume is not None and str(resume.reminder_id) == reminder
    lines = sr.run_lines(caplog.records)
    assert len(lines) == 2, lines
    assert [_field(lines[0], f) for f in ("code", "attempted", "cancelled")] == [
        "CANCELLED", "1", "1"]
    assert again.code == "COMPLETE" and again.no_reply == 1
    assert sb.states(sb_install, [reminder]) == {reminder: "no_reply"}


def _wait_lock(install: sb.SBInstall, timeout_s: float = 20.0) -> None:
    """Poll until a backend running the close gateway waits on a heavyweight lock."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = sa.admin(install, "SELECT count(*) FROM pg_stat_activity"
                                 " WHERE wait_event_type = 'Lock'"
                                 " AND query LIKE '%%m05_close_window%%'")
        if rows[0][0]:
            return
        time.sleep(0.02)
    raise AssertionError("the close never blocked on the contact lock")
