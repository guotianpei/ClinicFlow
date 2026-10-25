"""``cd haloflow && PYTHONPATH=demo python -m haloflow_demo [reset-synthetic]``

Reads settings from the process environment only. Synthetic data only; local
demonstrator with no authentication.

``reset-synthetic`` clears the synthetic tables and preserves the send quota,
opt-outs, contact history and secrets. It is refused while the server runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from haloflow_demo.app import exit_process, run
from haloflow_demo.settings import DemoSettings, StartupRefused


async def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    return await run(DemoSettings.from_env(dict(os.environ)))


async def _reset() -> int:
    from haloflow_demo.reset import reset_synthetic

    try:
        await reset_synthetic(DemoSettings.from_env(dict(os.environ)))
    except StartupRefused as exc:
        print(f"reset refused: {exc.reason}", file=sys.stderr)
        return 2
    print("synthetic demo state cleared; quota, opt-outs and contact history preserved")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["reset-synthetic"]:
        sys.exit(asyncio.run(_reset()))
    if sys.argv[1:]:
        print(f"unknown command: {' '.join(sys.argv[1:])}", file=sys.stderr)
        sys.exit(64)
    exit_process(_main)
