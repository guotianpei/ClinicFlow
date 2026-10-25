"""Harness child for the outbound-once live test (G): runs the real
``haloflow_demo.livetest.live_sms_test`` with a MockTransport, so the network
is never touched.

``--mode block``: the provider request blocks until ``release`` exists.
``--mode stubborn``: the same, but it ignores cancellation.
``--mode ok``: an immediate documented 2xx.
``--mode gated``: the request logs ``request_started`` and then waits for
``go`` before returning a documented 2xx. The test uses this to hold the match
lock between the response and the outcome commit.
``--outer-stubborn``: before the send pipeline starts, the outer operation
waits for ``release`` and ignores cancellation (logs ``outer_cancel_received``).

Phases go to ``calls.jsonl``. The exit uses the same path as
``python -m haloflow_demo live-sms-test`` (``exit_process``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx


def main_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--mode", choices=["ok", "block", "stubborn", "gated"], default="ok")
    ap.add_argument("--outer-stubborn", action="store_true")
    ap.add_argument("--drain-timeout", type=float, default=2.0)
    ap.add_argument("--cancel-grace", type=float, default=1.0)
    return ap.parse_args()


ARGS = main_args()


def _log(phase: str) -> None:
    with (ARGS.workdir / "calls.jsonl").open("a") as fh:
        fh.write(json.dumps({"phase": phase, "pid": os.getpid(), "t": time.time()}) + "\n")


async def _handler(request: httpx.Request) -> httpx.Response:
    _log("request_started")
    if ARGS.mode == "gated":
        while not (ARGS.workdir / "go").exists():
            await asyncio.sleep(0.02)
    elif ARGS.mode != "ok":
        release = ARGS.workdir / "release"
        while not release.exists():
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                _log("request_cancel_received")
                if ARGS.mode != "stubborn":
                    raise
    _log("request_finished")
    return httpx.Response(200, json={"success": True, "payload": {"smsMessageID": "m-1"}})


async def main() -> int:
    import haloflow_demo.livetest as livetest_mod
    from haloflow_demo.livetest import live_sms_test

    if ARGS.outer_stubborn:
        real_send_one = livetest_mod.send_one

        async def stubborn_send_one(*a: object, **k: object) -> object:
            _log("outer_started")
            release = ARGS.workdir / "release"
            while not release.exists():
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    _log("outer_cancel_received")
            return await real_send_one(*a, **k)  # type: ignore[arg-type]

        livetest_mod.send_one = stubborn_send_one  # type: ignore[assignment]

    return await live_sms_test(dict(os.environ), transport=httpx.MockTransport(_handler),
                               drain_timeout=ARGS.drain_timeout,
                               cancel_grace=ARGS.cancel_grace)


if __name__ == "__main__":
    from haloflow_demo.app import exit_process

    exit_process(main)
