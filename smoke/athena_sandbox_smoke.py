#!/usr/bin/env python3
"""N-ATH integrated sandbox READ smoke — bounded runner (plan v1 + Codex review).

Runs the accepted demonstrator code (PR #45, dea6157) in athena_preview mode
against the athenahealth Preview sandbox, in-process: the real start-up
functions (``prepare`` → ``acquire_ownership`` → ``build_service``) and the real
staff ASGI app driven through httpx.ASGITransport. No port is opened (no clash
with Thursday's 8080/8081 demo); the uvicorn listeners themselves are not
exercised here (covered by the lifecycle tests).

Owner scope: OD-A1 read-only sandbox / simulated write-back, OD-A2 masked
patients, OD-A3 stub sends. Nothing is written to athenahealth; no SMS leaves.

External request bound (all counted by a sanitising transport wrapper):
  setup: 1 OAuth token POST + 1 GET /v1/195900/departments   (department id kept
         in memory only, never written)
  app:   1 OAuth token POST + ≤ 5 GET /v1/195900/appointments/booked
  total ≤ 8. Any other request is refused before sending.
Staff-app budget: ≤ 3 page loads (GET /demo/staff), ≤ 1 send, ≤ 1 simulated
reply, ≤ 1 write-back run; redirects are never followed.

Evidence: a JSON summary (fixed codes, counts, method/category/status
counters) and, only if the page passes structural vetting, the vetted page
HTML. Vetting is structural (every appointment row must be exactly
"ath-e<n>-<id>" + "Sandbox patient …<4 digits>"; no configured phone number,
secret or token anywhere): it cannot prove the absence of arbitrary text, but
the page only renders fields this code projects. httpx INFO logging is
suppressed (it would log query URLs).

Usage (from haloflow/, with the haloflow venv):
  python athena_sandbox_smoke.py --env-file .env --admin-url postgresql://… \
      --db haloflow_smoke_ath_demo --out <dir>
Exit: 0 completed (see outcome), 1 stopped on a check, 2 refused before any effect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg

DB_RE = re.compile(r"^haloflow_smoke_[a-z0-9_]+_demo$")
ROW_RE = re.compile(r"<tr><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td>"
                    r"<td>(.*?)</td></tr>", re.S)
APPT_RE = re.compile(r"^ath-e\d+-[1-9][0-9]{0,11}$")
PATIENT_RE = re.compile(r"^Sandbox patient …\d{1,4}$")
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
HOST = "api.preview.platform.athenahealth.com"
ALLOWED = {("POST", "/oauth2/v1/token"): "token", ("GET", "/v1/195900/departments"): "departments",
           ("GET", "/v1/195900/appointments/booked"): "booked"}
MAX_EXTERNAL = 8
MAX_PAGES = 3


class Stop(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CountingTransport(httpx.AsyncBaseTransport):
    """Allowlist + budget + sanitised counters (method, category, status)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, counters: list[dict[str, Any]],
                 budget: dict[str, int]) -> None:
        self._inner, self._counters, self._budget = inner, counters, budget

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        cat = ALLOWED.get((request.method, request.url.path))
        if request.url.host != HOST or cat is None:
            raise Stop("EXTERNAL_DISALLOWED")
        if self._budget["used"] >= MAX_EXTERNAL:
            raise Stop("EXTERNAL_BUDGET")
        self._budget["used"] += 1
        resp = await self._inner.handle_async_request(request)
        self._counters.append({"method": request.method, "category": cat,
                               "status": resp.status_code})
        return resp

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _first_department(env: Mapping[str, str], transport: httpx.AsyncBaseTransport) -> str:
    """Setup: token + departments; first valid id, memory only."""
    async with httpx.AsyncClient(transport=transport, timeout=25.0,
                                 follow_redirects=False) as c:
        try:
            r = await c.post(f"https://{HOST}/oauth2/v1/token",
                             auth=(env["ATHENA_CLIENT_ID"], env["ATHENA_CLIENT_SECRET"]),
                             data={"grant_type": "client_credentials",
                                   "scope": "athena/service/Athenanet.MDP.*"})
            if r.status_code != 200:
                raise Stop(f"SETUP_TOKEN_HTTP_{r.status_code}")
            tok = r.json().get("access_token")
            r = await c.get(f"https://{HOST}/v1/195900/departments",
                            headers={"Authorization": f"Bearer {tok}"})
            if r.status_code != 200:
                raise Stop(f"SETUP_DEPARTMENTS_HTTP_{r.status_code}")
            deps = r.json().get("departments")
        except Stop:
            raise
        except Exception:
            raise Stop("SETUP_FAILED") from None
    for d in deps if isinstance(deps, list) else []:
        v = str(d.get("departmentid", "")) if isinstance(d, dict) else ""
        if ID_RE.match(v):
            return v
    raise Stop("SETUP_NO_DEPARTMENT")


def _with_db(url: str, db: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + db, p.query, ""))


def _db_must_not_exist(admin_url: str, db: str) -> None:
    if not DB_RE.match(db):
        raise Stop("DB_NAME_REFUSED")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone():
            raise Stop("DB_EXISTS")                       # never overwrite


def _create_fresh_db(admin_url: str, db: str) -> None:
    _db_must_not_exist(admin_url, db)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')


def vet_page(html: str, forbidden: list[str]) -> list[dict[str, str]]:
    """Structural vetting; returns parsed rows or raises Stop('VET_FAILED')."""
    for f in forbidden:
        if f and f in html:
            raise Stop("VET_FAILED")
    rows = []
    for appt, patient, when, status, _actions in ROW_RE.findall(html):
        if not APPT_RE.match(appt) or not PATIENT_RE.match(patient):
            raise Stop("VET_FAILED")
        rows.append({"appt": appt, "patient": patient, "when": when, "status": status})
    if html.count("<tr>") - 1 != len(rows):              # header row + appointment rows only
        raise Stop("VET_FAILED")
    return rows


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not m:
        raise Stop("NO_CSRF")
    return m.group(1)


async def smoke(env: Mapping[str, str], admin_url: str, db: str, out: Path,
                transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
                ) -> tuple[int, dict[str, Any]]:
    from haloflow_demo.app import build_service, prepare
    from haloflow_demo.ownership import acquire_ownership
    from haloflow_demo.settings import DemoSettings, StartupRefused
    from haloflow_demo.web import build_staff_app

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    counters: list[dict[str, Any]] = []
    budget = {"used": 0}
    summary: dict[str, Any] = {"runner": "athena_sandbox_smoke v1", "steps": [],
                               "started_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "write_to_athena": "none (simulated store only)",
                               "sms": "stub (nothing sent)"}

    def tr() -> httpx.AsyncBaseTransport:
        inner = transport_factory() if transport_factory else httpx.AsyncHTTPTransport()
        return CountingTransport(inner, counters, budget)

    forbidden = [env.get(k, "") for k in ("ATHENA_CLIENT_ID", "ATHENA_CLIENT_SECRET",
                                          "DEMO_SMS_TEST_NUMBER", "DEMO_CALLBACK_SECRET")]
    # Fictional numbers (555-01xx) and a fresh callback secret: stub sends only, so
    # nothing leaves the process and no real phone number is involved.
    run_env = {**env, "DEMO_EHR_SOURCE": "athena_preview", "DEMO_SMS_MODE": "",
               "DEMO_CALLBACK_MODE": "", "DEMO_LIVE_TEST": "",
               "DEMO_SMS_TEST_NUMBER": "+15550100141", "DEMO_SMS_SENDER": "+15550100100",
               "DEMO_CALLBACK_SECRET": "smoke-" + secrets.token_hex(16),
               "DEMO_STAFF_BIND": "", "DEMO_CALLBACK_BIND": "", "DEMO_DISPATCHER": "",
               "DEMO_DATABASE_URL": _with_db(admin_url, db)}
    forbidden += [run_env["DEMO_SMS_TEST_NUMBER"], run_env["DEMO_SMS_SENDER"],
                  run_env["DEMO_CALLBACK_SECRET"]]
    own = svc = None
    rc = 0
    try:
        settings0 = DemoSettings.from_env({**run_env, "DEMO_ATHENA_DEPARTMENT_ID": "1"})
        prepare(settings0, {**run_env, "DEMO_ATHENA_DEPARTMENT_ID": "1"})  # gate before effects
        summary["steps"].append("gate_ok")
        _db_must_not_exist(admin_url, db)
        dept = await _first_department(run_env, tr())
        summary["steps"].append("department_selected (id kept in memory only)")
        run_env["DEMO_ATHENA_DEPARTMENT_ID"] = dept
        _create_fresh_db(admin_url, db)
        summary["steps"].append("fresh_db_created")
        settings = DemoSettings.from_env(run_env)
        target, athena = prepare(settings, run_env)
        own = await acquire_ownership(settings, target)
        svc = build_service(own, settings, athena, athena_transport=tr())
        app = build_staff_app(svc, csrf_secret=secrets.token_bytes(32),
                              unavailable=lambda: own.owner_lost or own.closing)
        pages = 0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8080",
                                     follow_redirects=False) as c:
            async def page() -> str:
                nonlocal pages
                if pages >= MAX_PAGES:
                    raise Stop("PAGE_BUDGET")
                pages += 1
                r = await c.get("/demo/staff")
                if r.status_code != 200:
                    raise Stop(f"PAGE_HTTP_{r.status_code}")
                return r.text

            async def post(path: str, **f: str) -> None:
                r = await c.post(path, data={"csrf_token": tok, **f})
                if r.status_code != 303:
                    raise Stop(f"POST_HTTP_{r.status_code}")

            html1 = await page()
            if "athenahealth sandbox unavailable" in html1:
                m = re.search(r"unavailable — ([A-Z0-9_]+)", html1)
                raise Stop(f"SOURCE_{m.group(1) if m else 'UNAVAILABLE'}")
            rows = vet_page(html1, forbidden)
            for needle, step in (("SANDBOX READ / SIMULATED WRITE", "label_ok"),
                                 ("Sandbox snapshot taken", "snapshot_banner_ok"),
                                 ("UNVERIFIED", "tz_unverified_banner_ok")):
                if needle not in html1:
                    raise Stop(f"MISSING_{step.upper()}")
                summary["steps"].append(step)
            summary["appointments_listed"] = len(rows)
            ex = re.search(r"Excluded records: ([^<]*)\.", html1)
            summary["excluded"] = ex.group(1) if ex else ""
            tok = _csrf(html1)
            html2 = await page()
            vet_page(html2, forbidden)
            booked = sum(1 for x in counters if x["category"] == "booked")
            if booked < 1 or booked > 5:
                raise Stop("BOOKED_COUNT")
            summary["steps"].append("single_snapshot_ok")
            send_rows = [r for r in ROW_RE.findall(html1)
                         if "Send to this appointment" in r[4]]
            if not send_rows:
                summary["steps"].append("no_eligible_appointment_on_target_date")
                html_final = html2
            else:
                appt = send_rows[0][0]
                await post("/demo/appointments/send", emr_appt_id=appt)
                async with own.engine.connect() as conn:
                    from sqlalchemy import text
                    rid = (await conn.execute(text(
                        "SELECT id FROM reminder_records WHERE emr_appt_id = :a"),
                        {"a": appt})).scalar_one_or_none()
                if rid is None:
                    raise Stop("SEND_NO_REMINDER")
                await post("/demo/reply/simulate", reminder_id=str(rid), text="1")
                await post("/demo/inbound/process")
                async with own.engine.connect() as conn:
                    from sqlalchemy import text
                    tid = (await conn.execute(text(
                        "SELECT id FROM writeback_tasks WHERE reminder_id = :r"),
                        {"r": rid})).scalar_one_or_none()
                if tid is None:
                    raise Stop("NO_WRITEBACK_TASK")
                await post("/demo/tasks/run", task_id=str(tid))
                html_final = await page()
                vet_page(html_final, forbidden)
                if f"Task {tid} " not in html_final or " DONE" not in html_final:
                    raise Stop("WRITEBACK_NOT_DONE")
                summary["steps"].append("send_reply_writeback_simulated_ok")
            out.mkdir(parents=True, exist_ok=True)
            (out / "staff-page-vetted.html").write_text(html_final)
        summary["page_loads"] = pages
        summary["result"] = "COMPLETED"
    except StartupRefused as exc:
        summary["result"] = f"REFUSED:{exc.reason}"
        rc = 2
    except Stop as s:
        summary["result"] = s.code
        rc = 1
    except Exception:  # noqa: BLE001 — never print exception text
        summary["result"] = "INTERNAL"
        rc = 1
    finally:
        if svc is not None:
            await svc.aclose()
        if own is not None:
            await own.close()
        summary["external_requests"] = counters
        summary["external_request_count"] = budget["used"]
        summary["external_request_bound"] = MAX_EXTERNAL
    return rc, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True, type=Path)
    ap.add_argument("--admin-url", required=True)
    ap.add_argument("--db", default="haloflow_smoke_ath_demo")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    from dotenv import dotenv_values
    env = {k: v for k, v in dotenv_values(a.env_file).items() if v is not None}
    rc, summary = asyncio.run(smoke(env, a.admin_url, a.db, a.out))
    a.out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, indent=2, sort_keys=True)
    (a.out / "smoke-summary.json").write_text(text + "\n")
    print(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
