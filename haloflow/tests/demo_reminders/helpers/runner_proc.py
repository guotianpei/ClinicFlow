"""Lifecycle harness child: runs the real demo runner ``haloflow_demo.app.run``
with both uvicorn listeners, and records what happens to files in --workdir.

Harness-only injections (never used by ``python -m haloflow_demo``):
- every outbound ``socket.connect`` destination is appended to ``connects.jsonl``
  (T02: only the demo database is contacted);
- ``--blocked``: once started, one external call is placed through the
  ownership guard and blocks until ``release`` exists;
- ``--stubborn``: that call ignores cancellation (drain cannot finish);
- after the runner reports ``closing``, a probe tries a NEW guarded call and
  records whether it was refused.

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
    args = ap.parse_args()
    _wd = args.workdir
    socket.socket.connect = _recording_connect  # type: ignore[method-assign]

    from haloflow_demo.app import run
    from haloflow_demo.ownership import OwnerLost, Ownership
    from haloflow_demo.settings import DemoSettings

    release = _wd / "release"

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
        if args.blocked:
            async def _place() -> None:
                try:
                    await own.guard_external(_blocked_call)
                except OwnerLost:
                    _log("calls.jsonl", {"phase": "blocked_call_refused"})
            probe_tasks.append(asyncio.get_running_loop().create_task(_place()))

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
