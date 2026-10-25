"""Lifecycle harness child: runs the real demo runner ``haloflow_demo.app.run``
with both uvicorn listeners, and records what happens to files in --workdir.

Harness-only injections (never used by ``python -m haloflow_demo``):
- every outbound ``socket.connect`` destination is appended to ``connects.jsonl``
  (T02: only the demo database is contacted);
- ``--blocked``: once started, one external call is placed through the
  ownership guard and blocks until ``release`` exists;
- ``--stubborn``: that call ignores cancellation (drain cannot finish);
- after the runner reports ``closing``, a probe tries a NEW guarded call and
  records whether it was refused;
- ``--seed-reply`` (Day 5): once started, one stub reminder is sent and one
  signed simulated "1" reply is received, so the dispatcher
  (``DEMO_DISPATCHER=on``) has a CONFIRM to process;
- ``--ehr-read ok|block|stubborn``: the synthetic EHR baseline read returns at
  once (``ok``), or blocks until ``release`` exists (``stubborn`` also ignores
  cancellation). The phases go to ``calls.jsonl``.

Every runner event is appended to ``events.jsonl`` as it happens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
from pathlib import Path

_wd: Path | None = None
_real_connect = socket.socket.connect


def _log(name: str, payload: dict[str, object]) -> None:
    assert _wd is not None
    with (_wd / name).open("a") as fh:
        fh.write(json.dumps({**payload, "t": time.time()}) + "\n")


def _recording_connect(self: socket.socket, address: object) -> None:
    _log("connects.jsonl", {"family": int(self.family), "address": repr(address)})
    _real_connect(self, address)  # type: ignore[arg-type]


async def main() -> int:
    global _wd
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--watchdog", type=float, default=2.0)
    ap.add_argument("--drain-timeout", type=float, default=10.0)
    ap.add_argument("--cancel-grace", type=float, default=1.0)
    ap.add_argument("--blocked", action="store_true")
    ap.add_argument("--stubborn", action="store_true")
    ap.add_argument("--seed-reply", action="store_true")
    ap.add_argument("--ehr-read", choices=["real", "ok", "block", "stubborn"], default="real")
    args = ap.parse_args()
    _wd = args.workdir
    socket.socket.connect = _recording_connect  # type: ignore[method-assign]

    from haloflow_demo.app import run
    from haloflow_demo.ownership import OwnerLost, Ownership
    from haloflow_demo.settings import DemoSettings

    release = _wd / "release"

    if args.ehr_read != "real":
        import haloflow_demo.service as service_mod
        from haloflow_demo.synthetic_ehr import Observation

        async def _blocking_read(self: object, appt_id: str) -> Observation:
            _log("calls.jsonl", {"phase": "ehr_read_started"})
            while args.ehr_read != "ok" and not release.exists():
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    _log("calls.jsonl", {"phase": "ehr_read_cancel_received"})
                    if args.ehr_read != "stubborn":
                        raise
            _log("calls.jsonl", {"phase": "ehr_read_finished"})
            return Observation("1", 1)

        service_mod._EhrRouter.read_confirmation = _blocking_read  # type: ignore[method-assign]

    async def _blocked_call() -> str:
        _log("calls.jsonl", {"phase": "started"})
        while not release.exists():
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                _log("calls.jsonl", {"phase": "cancel_received"})
                if not args.stubborn:
                    raise
        _log("calls.jsonl", {"phase": "finished"})
        return "ok"

    async def _instant_call() -> str:
        _log("calls.jsonl", {"phase": "probe_call_ran"})
        return "probe"

    probe_tasks: list[asyncio.Task[None]] = []

    def on_started(own: Ownership) -> None:
        # Exact identity of this owner's lock session, so a test can terminate
        # THIS backend and nothing else (Codex checkpoint-2 review, item 3).
        (_wd / "owner.json").write_text(json.dumps(
            {"pid": os.getpid(), "lock_backend_pid": own.lock_backend_pid, "epoch": own.epoch}))
        if args.blocked:
            async def _place() -> None:
                try:
                    await own.guard_external(_blocked_call)
                except OwnerLost:
                    _log("calls.jsonl", {"phase": "blocked_call_refused"})
            probe_tasks.append(asyncio.get_running_loop().create_task(_place()))

        if args.seed_reply:
            async def _seed() -> None:
                import hashlib
                import hmac
                from datetime import UTC, datetime, timedelta

                from haloflow_demo.inbound import receive
                from haloflow_demo.sending import StubGateway, send_one
                from haloflow_demo.synthetic_ehr import (
                    ApptStatus,
                    SyntheticAppointment,
                    SyntheticPatient,
                )

                now = datetime.now(tz=UTC)
                appt = SyntheticAppointment("syn-a-1", "syn-p-1", now + timedelta(days=2),
                                            ApptStatus.SCHEDULED, confirmation="1")
                patient = SyntheticPatient("syn-p-1", "Synthetic Patient 1", "alpha")
                r = await send_one(own, settings, appt, patient,
                                   StubGateway(settings.sms_test_number),
                                   tenant_id="demo-clinic", now=lambda: now)
                ts = int(now.timestamp())
                raw = json.dumps({"Event": "sms_received", "Timestamp": ts, "Payload": {
                    "ReplyID": "seed-1", "RecipientNumber": settings.sms_sender,
                    "SenderNumber": settings.sms_test_number, "Message": "1",
                    "ReceivedDateUtc": ts}}).encode()
                v = hmac.new(settings.callback_secret.encode(), str(ts).encode() + b"." + raw,
                             hashlib.sha256).hexdigest()
                res = await receive(own, settings, raw, [f"t={ts},v={v}"], now=lambda: now)
                _log("calls.jsonl", {"phase": "seeded", "send": r.state, "receipt": res.status})
            probe_tasks.append(asyncio.get_running_loop().create_task(_seed()))

        async def _probe_after_closing() -> None:
            await own.closing_event.wait()
            try:
                await own.guard_external(_instant_call)
                _log("probe.jsonl", {"result": "ran"})
            except OwnerLost:
                _log("probe.jsonl", {"result": "refused"})
        probe_tasks.append(asyncio.get_running_loop().create_task(_probe_after_closing()))

    def on_event(name: str) -> None:
        _log("events.jsonl", {"event": name, "pid": os.getpid()})

    settings = DemoSettings.from_env(dict(os.environ))
    code = await run(
        settings,
        on_started=on_started,
        on_event=on_event,
        watchdog_interval=args.watchdog,
        drain_timeout=args.drain_timeout,
        cancel_grace=args.cancel_grace,
    )
    _log("exit.jsonl", {"code": code})
    return code


if __name__ == "__main__":
    from haloflow_demo.app import exit_process

    exit_process(main)  # same exit path as ``python -m haloflow_demo``
