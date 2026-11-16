"""B3b subprocess child (TEST ONLY): the B3b app over a lifecycle with t_http=1, one request
held, then request_stop with the DEFAULT fatal_exit (a real process exit), in a fresh
`python -I` (the B3a child pattern).

argv[1] = JSON {bar, environ, app, mode, item_id}
- mode "no_db": the held request is GET .../items/{item_id}; the M05 `get` is replaced by a
  coroutine that never returns, so the handler holds NO database connection.
- mode "in_txn": the held request is POST .../items/{item_id}/actions (claim); the M05
  service's `run_with_marker` is wrapped so that, INSIDE the gateway transaction and after the
  claim statements ran, the callback parks before returning (so the transaction never
  commits).
Hook barriers: every b3.* point touches <bar>/<point>.seen; at b3.stop.http the child writes
<bar>/b3.stop.http.reached and blocks until <bar>/b3.stop.http.go (bounded).
On the expected path the child never returns: default_fatal_exit prints
`fatal: HTTP_NOT_DRAINED http` and exits 3. Any other ending prints one `B3BCHILD <json>`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    cfg = json.loads(sys.argv[1])
    bar = Path(cfg["bar"])

    def fault(point: str, info: Any) -> None:
        if point.startswith("b3."):
            (bar / f"{point}.seen").touch()
        if point == "b3.stop.http":
            (bar / f"{point}.reached").write_text(json.dumps({"child_pid": os.getpid()}))
            deadline = time.time() + 120
            while not (bar / f"{point}.go").exists():
                if time.time() > deadline:
                    os._exit(98)
                time.sleep(0.05)

    out: dict[str, Any]
    try:
        importlib.import_module("haloflow.synthetic.hooks").FAULT = fault
        out = asyncio.run(_run(cfg))
    except Exception as e:  # noqa: BLE001 - reported to the parent row
        out = {"ok": False, "type": type(e).__name__, "code": getattr(e, "code", None),
               "msg": str(e)[:200] if isinstance(e, ImportError) else ""}
    print("B3BCHILD " + json.dumps(out, default=str), flush=True)


async def _run(cfg: dict[str, Any]) -> dict[str, Any]:
    import b3b_support as bb

    lifecycle = importlib.import_module("haloflow.synthetic.lifecycle")
    app_mod = importlib.import_module("haloflow.synthetic.app")
    lc = lifecycle.SyntheticLifecycle(cfg["environ"], application_name=cfg["app"],
                                      config=lifecycle.LifecycleConfig(t_http=1))
    await lc.start()
    app = app_mod.create_synthetic_app(lifecycle=lc, bind=bb.BIND)
    iid = cfg["item_id"]
    forever = asyncio.Event()
    parked_at = asyncio.Event()           # set once the handler is parked where `mode` says
    if cfg["mode"] == "no_db":
        async def never(*_a: Any, **_k: Any) -> Any:
            parked_at.set()
            await forever.wait()

        lc.services.m05.get = never
        req = asyncio.create_task(bb.asgi(app, "GET", bb.path("get", iid),
                                          headers=bb.get_headers(), timeout=600))
    else:
        service = importlib.import_module("haloflow.m05.service")
        real = service.run_with_marker

        async def parked(gateway: Any, context: Any, body: Any, unknown: Any) -> Any:
            async def body2(handle: Any, mark: Any) -> Any:
                result = await body(handle, lambda: None)
                parked_at.set()
                await forever.wait()             # inside the transaction, never commits
                mark()
                return result

            return await real(gateway, context, body2, unknown)

        service.run_with_marker = parked
        token = await bb.session_token(app)
        req = asyncio.create_task(bb.asgi(
            app, "POST", bb.path("act", iid), headers=bb.post_headers(token), timeout=600,
            body=bb.jbody({"client_action_id": bb.new_uuid(), "expected_version": 1,
                           "type": "claim"})))
    try:
        await asyncio.wait_for(parked_at.wait(), 60)
    except TimeoutError:
        return {"ok": False, "why": "request never parked", "status": (
            (await req).status if req.done() else None)}
    if lc.http_in_flight != 1:
        return {"ok": False, "why": "parked request not counted in flight"}
    lc.request_stop("TEST")
    result = await lc.wait_stopped()          # the default fatal_exit exits before this
    req.cancel()
    return {"ok": True, "disposition": result.disposition, "code": result.code}


if __name__ == "__main__":
    main()
