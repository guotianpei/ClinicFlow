"""T01 (R-1a) and precision P-ENG (separate demo engine; no legacy session).

Measured before this suite was written (2026-10-25, Claude container): with an
empty environment, ``import haloflow.ehr.base`` and
``import haloflow.modules.reminders.models`` both raise a pydantic
``ValidationError`` (``database_url`` missing), and with ``DATABASE_URL`` set the
models import pulls in ``haloflow.database``, which builds the legacy engine.
The demo therefore lives in the self-contained package ``haloflow.demo``
(deviation D-1 from package v2 §2.3) and must not import any of the modules
listed in ``FORBIDDEN`` below.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

DEMO_PKG = Path(__file__).resolve().parents[2] / "src" / "haloflow" / "demo"

FORBIDDEN = (
    "haloflow.config",
    "haloflow.database",
    "haloflow.ehr",
    "haloflow.integrations",
    "haloflow.scheduler",
    "haloflow.main",
    "haloflow.modules",
    "apscheduler",
    "tenacity",
)

_CHILD = r"""
import json, socket, sys

class _NoSocket(Exception):
    pass

def _refuse(*a, **k):
    raise _NoSocket("socket creation attempted")

socket.socket = _refuse            # type: ignore[assignment,misc]
socket.create_connection = _refuse # type: ignore[assignment]
socket.getaddrinfo = _refuse       # type: ignore[assignment]

from haloflow.demo.web import build_callback_app, build_staff_app
from haloflow.demo.web import NullStaffService

callback = build_callback_app()
staff = build_staff_app(NullStaffService(), csrf_secret=b"x" * 32)
print(json.dumps({
    "modules": sorted(sys.modules),
    "callback_type": type(callback).__name__,
    "staff_type": type(staff).__name__,
}))
"""


def _clean_env() -> dict[str, str]:
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV"}
    return {k: v for k, v in os.environ.items() if k in keep}


def test_t01_construct_both_apps_with_empty_env_opens_no_socket() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["callback_type"] == "FastAPI"
    assert report["staff_type"] == "FastAPI"
    loaded = report["modules"]
    leaked = sorted(
        m for m in loaded for f in FORBIDDEN if m == f or m.startswith(f + ".")
    )
    assert leaked == [], f"demo construction imported forbidden modules: {leaked}"


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_p_eng_demo_package_never_imports_legacy_db_or_settings() -> None:
    sources = sorted(DEMO_PKG.rglob("*.py"))
    assert sources, f"no demo sources found under {DEMO_PKG}"
    offenders: dict[str, list[str]] = {}
    for src in sources:
        bad = sorted(
            n for n in _imported_names(src)
            for f in FORBIDDEN if n == f or n.startswith(f + ".")
        )
        text = src.read_text()
        for token in ("get_db", "AsyncSessionLocal"):
            if token in text:
                bad.append(token)
        if bad:
            offenders[str(src.relative_to(DEMO_PKG))] = bad
    assert offenders == {}
