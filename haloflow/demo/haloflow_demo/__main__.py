"""``cd haloflow && PYTHONPATH=demo python -m haloflow_demo``

Reads settings from the process environment only. Synthetic data only; local
demonstrator with no authentication.
"""

from __future__ import annotations

import logging
import os

from haloflow_demo.app import exit_process, run
from haloflow_demo.settings import DemoSettings


async def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    return await run(DemoSettings.from_env(dict(os.environ)))


if __name__ == "__main__":
    exit_process(_main)
