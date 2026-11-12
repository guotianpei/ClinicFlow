"""B2 subprocess child (TEST ONLY): runs one action of `haloflow.synthetic` in a fresh
`python -I`, with the test-only fault hook wired to file barriers.

argv[1] = JSON {action, pause, crash, bar, environ, extra}
- pause "point" or "point#n": at the n-th call of hook `point` (default 1st) write
  `<bar>/<point>.reached` (JSON of the hook info plus pid) and block until `<bar>/<point>.go`.
- crash "point" or "point#n": at that call, `os._exit(97)` (simulated process death; no
  finally blocks, no rollback other than what PostgreSQL does for a dead backend).
Prints exactly one `B2CHILD <json>` line: {"ok": true, ...} or {"ok": false, "code": ...}.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from typing import Any


def main() -> None:
    cfg = json.loads(sys.argv[1])
    bar = cfg["bar"]
    counts: dict[str, int] = {}

    def split(spec: str) -> tuple[str, int]:
        if not spec:
            return "", 0
        name, _, n = spec.partition("#")
        return name, int(n or 1)

    p_name, p_n = split(cfg["pause"])
    c_name, c_n = split(cfg["crash"])
    r_name, r_n = split(cfg.get("raise_at", ""))
    k_name, k_n = split(cfg.get("kill_at", ""))

    def fault(point: str, info: Any) -> None:
        counts[point] = counts.get(point, 0) + 1
        k = counts[point]
        if point == c_name and k == c_n:
            os._exit(97)
        if point == k_name and k == k_n:
            import psycopg

            with psycopg.connect(cfg["environ"]["HALOFLOW_SYNTHETIC_ADMIN_DSN"],
                                 autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (int(info["backend_pid"]),))
        if point == r_name and k == r_n:
            import psycopg

            raise psycopg.OperationalError("B2 test fault: acknowledgement lost")
        if point == p_name and k == p_n:
            data = dict(info or {})
            data["child_pid"] = os.getpid()
            with open(f"{bar}/{point}.reached", "w") as fh:
                json.dump(data, fh, default=str)
            while not os.path.exists(f"{bar}/{point}.go"):
                time.sleep(0.05)

    environ = cfg["environ"]
    action = cfg["action"]
    try:
        hooks = importlib.import_module("haloflow.synthetic.hooks")
        hooks.FAULT = fault
        if action == "init":
            provision = importlib.import_module("haloflow.synthetic.provision")
            r = provision.init(environ, attest_flag=True)
            out: dict[str, Any] = {"ok": True, "phase": r.phase}
        elif action == "status":
            provision = importlib.import_module("haloflow.synthetic.provision")
            out = {"ok": True, "status": provision.status(environ)}
        elif action == "begin_run":
            dsn = importlib.import_module("haloflow.synthetic.dsn")
            guard = importlib.import_module("haloflow.synthetic.guard")

            ep = dsn.parse_admin_dsn(environ["HALOFLOW_SYNTHETIC_ADMIN_DSN"], {})
            x = cfg["extra"]
            out = {"ok": True, "result": guard.begin_run(ep, x["phase"], x["nonce"], x["epoch"])}
        elif action == "migrate":
            migrate = importlib.import_module("haloflow.synthetic.migrate")

            before, after = migrate.upgrade_guarded(cfg["extra"]["conninfo"],
                                                    cfg["extra"]["expected"])
            out = {"ok": True, "before": before, "after": after}
        else:
            raise SystemExit(f"unknown action {action}")
    except Exception as e:  # noqa: BLE001 - reported to the parent row
        msg = str(e)[:200] if isinstance(e, ImportError) else ""
        out = {"ok": False, "type": type(e).__name__, "code": getattr(e, "code", None),
               "msg": msg,
               "detail": list(getattr(e, "detail", ()) or ())}
    print("B2CHILD " + json.dumps(out, default=str), flush=True)
    if not out.get("ok") and out.get("type") == "ModuleNotFoundError":
        sys.exit(3)


if __name__ == "__main__":
    main()
