"""Test-only fault hook of the synthetic installer (Option B B2). SYNTHETIC ONLY.

`FAULT` is None in every non-test process; only the test child (tests/synthetic/b2_child.py)
and in-process test rows assign it (B2-U-H1 proves no `src` module does). The installer calls
`fire(point, info)` at named points; with no hook installed that is a no-op.

`AFAULT` (Option B B5) is the awaited counterpart: the scenario load awaits
`afire(point, info)` at its `b5.load.*` points, so an in-process test can PAUSE a load (await
an event) without blocking the event loop. It is None in every non-test process; only test
rows and the B5 test child assign it (B5 U-H1 proves no `src` module does).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

FAULT: Callable[[str, Mapping[str, Any]], None] | None = None
AFAULT: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None = None


def fire(point: str, info: Mapping[str, Any] | None = None) -> None:
    hook = FAULT
    if hook is not None:
        hook(point, dict(info or {}))


async def afire(point: str, info: Mapping[str, Any] | None = None) -> None:
    hook = AFAULT
    if hook is not None:
        await hook(point, dict(info or {}))
