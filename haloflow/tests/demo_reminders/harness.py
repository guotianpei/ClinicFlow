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


def verify_owner_backend(db_url: str, pid: int) -> None:
    """Raise AssertionError unless ``pid`` holds the demo advisory key in the
    database named by ``db_url``. Tests call this before terminating a backend,
    so a shared PostgreSQL server never loses an unrelated session."""
    import psycopg
    from haloflow_demo.ownership import DEMO_ADVISORY_KEY

    with psycopg.connect(db_url) as conn:
        rows = conn.execute(
            "SELECT l.pid FROM pg_locks l JOIN pg_database d ON d.oid = l.database"
            " WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1"
            " AND d.datname = current_database() AND l.pid = %s"
            " AND ((l.classid::bigint << 32) | l.objid::bigint) = %s",
            (pid, DEMO_ADVISORY_KEY),
        ).fetchall()
    assert rows == [(pid,)], f"backend {pid} does not hold the demo key in this database"


def terminate_owner_backend(db_url: str, pid: int) -> None:
    import psycopg

    verify_owner_backend(db_url, pid)
    with psycopg.connect(db_url, autocommit=True) as conn:
        ok = conn.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
    assert ok == (True,)
