"""B3a subprocess child (TEST ONLY): one lifecycle generation or one `serve`, in a fresh
`python -I`, with the test-only fault hook wired to file barriers (the B2 child pattern).

argv[1] = JSON {action, pause, crash, bar, environ, app, extra}
- action "lifecycle": SyntheticLifecycle with the JOURNAL sender/EHR factories (external-effect
  evidence in <bar>/journal.jsonl), application_name = app. Writes <bar>/started when RUNNING,
  then waits for <bar>/stop (bounded), calls request_stop("TEST") and waits for the result.
  extra.ticks = ["due"|"reminder"|"writeback", ...]: manual ticks run before waiting.
- action "serve": `python -m haloflow.synthetic serve` in-process via __main__.main, with the
  environment set; extra.args = the CLI argument list after "serve".
- pause "point[#n]" blocks at that hook call until <bar>/<point>.go; crash "point[#n]" calls
  os._exit(97) there (simulated process death).
Prints exactly one `B3CHILD <json>` line on a normal return.
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

# `python -I` ignores PYTHONPATH and does not add the script directory: put the test support
# directory on the path explicitly (haloflow itself comes from the installed environment).
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    cfg = json.loads(sys.argv[1])
    bar = Path(cfg["bar"])
    counts: dict[str, int] = {}

    def split(spec: str) -> tuple[str, int]:
        name, _, n = spec.partition("#")
        return name, int(n or 1)

    p_name, p_n = split(cfg["pause"]) if cfg["pause"] else ("", 0)
    c_name, c_n = split(cfg["crash"]) if cfg["crash"] else ("", 0)

    armed = {"on": not cfg["extra"].get("pause_after_start")}
    if cfg["extra"].get("pause_after_start"):
        p_name, p_n = cfg["extra"]["pause_after_start"], 1

    def fault(point: str, info: Any) -> None:
        if point.startswith("b3."):
            (bar / f"{point}.seen").touch()
        if point == cfg["extra"].get("block_at"):
            time.sleep(float(cfg["extra"].get("block_s", 3600)))  # blocks the event loop
        if not armed["on"] and point == p_name:
            return
        counts[point] = counts.get(point, 0) + 1
        k = counts[point]
        if point == c_name and k == c_n:
            os._exit(97)
        if point == p_name and k == p_n:
            (bar / f"{point}.reached").write_text(json.dumps(
                {**dict(info or {}), "child_pid": os.getpid()}, default=str))
            deadline = time.time() + 120
            while not (bar / f"{point}.go").exists():
                if time.time() > deadline:
                    os._exit(98)
                time.sleep(0.05)

    out: dict[str, Any]
    try:
        importlib.import_module("haloflow.synthetic.hooks").FAULT = fault
        if cfg["extra"].get("hang_stop") == "writeback":
            ws = importlib.import_module("haloflow.m05.writeback_scheduler")

            async def never(self: Any) -> None:
                await asyncio.sleep(3600)

            ws.WritebackScheduler.stop = never  # type: ignore[method-assign]
        cfg["_armed"] = armed
        if cfg["action"] == "lifecycle":
            out = asyncio.run(_lifecycle(cfg, bar))
        elif cfg["action"] == "serve":
            os.environ.clear()
            os.environ.update(cfg["environ"])
            main_mod = importlib.import_module("haloflow.synthetic.__main__")
            rc = main_mod.main(["serve", *cfg["extra"].get("args", [])])
            out = {"ok": True, "rc": rc}
        else:
            raise SystemExit(f"unknown action {cfg['action']}")
    except Exception as e:  # noqa: BLE001 - reported to the parent row
        out = {"ok": False, "type": type(e).__name__, "code": getattr(e, "code", None),
               "msg": str(e)[:200] if isinstance(e, ImportError) else ""}
    print("B3CHILD " + json.dumps(out, default=str), flush=True)


async def _lifecycle(cfg: dict[str, Any], bar: Path) -> dict[str, Any]:
    from datetime import UTC, datetime

    import b3a_support as b3

    lifecycle = importlib.import_module("haloflow.synthetic.lifecycle")
    journal = b3.Journal(bar / "journal.jsonl")
    extra = cfg["extra"]
    config = lifecycle.LifecycleConfig(lease_check_s=extra["lease_check_s"]) \
        if "lease_check_s" in extra else None
    lc = lifecycle.SyntheticLifecycle(
        cfg["environ"], application_name=cfg["app"], config=config,
        sender_factory=lambda: b3.journal_sender(journal),
        ehr_factory=lambda: b3.journal_ehr(journal, lambda: datetime.now(UTC),
                                           seed=extra.get("ehr_seed")))
    await lc.start()
    (bar / "started").touch()
    cfg["_armed"]["on"] = True
    checks0 = lc.watcher_checks
    codes: list[Any] = []
    for name in extra.get("ticks", []):
        try:
            await asyncio.wait_for(lc.schedulers[name].tick(), 120)
            codes.append(None)
        except Exception as e:  # noqa: BLE001 - recorded for the parent row
            if not extra.get("tick_codes"):
                raise
            codes.append(getattr(e, "code", type(e).__name__))
    delta = lc.watcher_checks - checks0
    deadline = time.time() + 120
    while not (bar / "stop").exists() and time.time() < deadline:
        await asyncio.sleep(0.05)
    lc.request_stop("TEST")
    result = await lc.wait_stopped()
    return {"ok": True, "disposition": result.disposition, "code": result.code,
            "tick_codes": codes, "watcher_checks_delta": delta}


if __name__ == "__main__":
    main()
