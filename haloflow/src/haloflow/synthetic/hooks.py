"""Test-only fault hook of the synthetic installer (Option B B2). SYNTHETIC ONLY.

`FAULT` is None in every non-test process; only the test child (tests/synthetic/b2_child.py)
and in-process test rows assign it (B2-U-H1 proves no `src` module does). The installer calls
`fire(point, info)` at named points; with no hook installed that is a no-op.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

FAULT: Callable[[str, Mapping[str, Any]], None] | None = None


def fire(point: str, info: Mapping[str, Any] | None = None) -> None:
    hook = FAULT
    if hook is not None:
        hook(point, dict(info or {}))
