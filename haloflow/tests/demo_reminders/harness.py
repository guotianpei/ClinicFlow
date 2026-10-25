"""Test-harness boundary for the out-of-tree demo package (correction R1).

The demonstrator lives at ``haloflow/demo/haloflow_demo``, outside the
production source tree and outside the wheel. Tests reach it by putting
``haloflow/demo`` on ``sys.path`` (in-process) or ``PYTHONPATH`` (children).
The deployable launch is ``cd haloflow && PYTHONPATH=demo python -m haloflow_demo``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo"


def ensure_demo_on_path() -> None:
    root = str(DEMO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def demo_subprocess_env(base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(DEMO_ROOT) + (os.pathsep + existing if existing else "")
    return env
