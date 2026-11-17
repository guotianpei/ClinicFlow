"""B5 subprocess child (TEST ONLY): one load in a fresh `python -I` that is killed at a hook
point (the B3a child pattern), for P-RDY4.

argv[1] = JSON {bar, environ, app, crash, scenario}
The app runs over a started lifecycle with the five-zone TEST set (ob5_support.TEST_ZONES)
installed on `scenarios.LOAD_ZONES`. `hooks.AFAULT` calls os._exit(97) at `crash` (no cleanup,
no lease release: the parent proves the old generation is gone). Any other ending prints one
`B5CHILD <json>` line.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    cfg = json.loads(sys.argv[1])
    out: dict[str, Any]
    try:
        out = asyncio.run(_run(cfg))
    except Exception as e:  # noqa: BLE001 - reported to the parent row
        out = {"ok": False, "type": type(e).__name__, "code": getattr(e, "code", None),
               "msg": str(e)[:200] if isinstance(e, ImportError) else ""}
    print("B5CHILD " + json.dumps(out, default=str), flush=True)


async def _run(cfg: dict[str, Any]) -> dict[str, Any]:
    import b3b_support as bb
    import ob5_support as b5

    hooks = importlib.import_module("haloflow.synthetic.hooks")
    scenarios = importlib.import_module("haloflow.synthetic.scenarios")
    scenarios.LOAD_ZONES = b5.TEST_ZONES

    async def crash(point: str, info: Any) -> None:
        if point == cfg["crash"]:
            os._exit(97)

    hooks.AFAULT = crash
    lifecycle = importlib.import_module("haloflow.synthetic.lifecycle")
    app_mod = importlib.import_module("haloflow.synthetic.app")
    lc = lifecycle.SyntheticLifecycle(cfg["environ"], application_name=cfg["app"])
    await lc.start()
    app = app_mod.create_synthetic_app(lifecycle=lc, bind=bb.BIND)
    token = await bb.session_token(app)
    r = await b5.load_json(app, token, cfg["scenario"])
    lc.request_stop("TEST")
    await lc.wait_stopped()
    return {"ok": False, "why": "crash point not reached", "status": r.status}


if __name__ == "__main__":
    main()
