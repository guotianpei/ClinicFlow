"""Experiment 1 child process: one demo owner instance, driven by files in --workdir.

Exit codes: 0 finished; 2 start-up refused (reason printed); 3 owner lost.

Role A: acquire ownership, claim one synthetic appointment (the fenced commit
that authorises an external call), write ``A.reserved``, pause until ``A.go``
exists, then make one recorded "provider call" through the ownership guard,
record SUBMITTED, close.

Role B: acquire ownership, write ``B.ready`` (epoch, wall time), optionally hold
until ``B.go``, write recovered reminder states to ``B.state``, close.

``--no-process-lock`` is a negative control used only by this harness: it
replaces the OS process lock with ``NullProcessLock`` so the test can show the
race that the process lock exists to prevent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from haloflow.demo.ownership import NullProcessLock, OwnerLost, acquire_ownership
from haloflow.demo.reminders import claim_reminder
from haloflow.demo.settings import DemoSettings, StartupRefused, validate_demo_database_url
from haloflow.demo.synthetic_ehr import ApptStatus, SyntheticAppointment


def _write(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(path)


async def _wait_for(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.02)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["A", "B"], required=True)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--watchdog", type=float, default=2.0)
    ap.add_argument("--no-process-lock", action="store_true")
    ap.add_argument("--hold", action="store_true")
    args = ap.parse_args()
    wd: Path = args.workdir
    settings = DemoSettings.from_env(dict(os.environ))
    target = validate_demo_database_url(settings)
    kwargs: dict[str, object] = {}
    if args.no_process_lock:
        kwargs["process_lock"] = NullProcessLock()
    try:
        own = await acquire_ownership(settings, target, **kwargs)  # type: ignore[arg-type]
    except StartupRefused as exc:
        _write(wd / f"{args.role}.refused", {"reason": exc.reason, "t": time.time()})
        print(f"refused: {exc.reason}", flush=True)
        return 2
    own.start_watchdog(interval=args.watchdog)
    _write(
        wd / f"{args.role}.ready",
        {"pid": os.getpid(), "epoch": own.epoch, "lock_backend_pid": own.lock_backend_pid,
         "t_owned": time.time()},
    )
    code = 0
    try:
        if args.role == "A":
            appt = SyntheticAppointment(
                "exp1-appt", "p1", datetime(2026, 10, 28, 14, tzinfo=UTC), ApptStatus.SCHEDULED
            )
            rid = await claim_reminder(own, "exp1", appt)
            _write(wd / "A.reserved", {"reminder_id": rid, "t": time.time()})
            await _wait_for(wd / "A.go")

            async def _provider_call() -> str:
                with (wd / "calls.jsonl").open("a") as fh:
                    fh.write(json.dumps({"role": "A", "epoch": own.epoch, "t": time.time()}) + "\n")
                return "provider-msg-1"

            try:
                msg_id = await own.guard_external(_provider_call)
            except OwnerLost:
                _write(wd / "A.outcome", {"outcome": "refused_owner_lost", "t": time.time()})
                return 3
            try:
                async with own.fenced() as conn:
                    await conn.execute(
                        text("UPDATE reminder_records SET state='SUBMITTED', "
                             "provider_message_id=:m WHERE id=:r AND state='CLAIMED'"),
                        {"m": msg_id, "r": rid},
                    )
                _write(wd / "A.outcome", {"outcome": "submitted", "t": time.time()})
            except OwnerLost:
                _write(wd / "A.outcome", {"outcome": "called_then_owner_lost", "t": time.time()})
                code = 3
        else:
            if args.hold:
                await _wait_for(wd / "B.go")
            async with own.fenced() as conn:
                rows = (await conn.execute(
                    text("SELECT emr_appt_id, state, reason FROM reminder_records ORDER BY id")
                )).all()
            _write(wd / "B.state", {"rows": [list(r) for r in rows], "t": time.time()})
    finally:
        await own.close()
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
