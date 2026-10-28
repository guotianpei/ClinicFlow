#!/usr/bin/env python3
"""N-ATH integrated sandbox READ smoke — bounded runner v2 (N-TZ Step 3 revision).

v2 (2026-10-28): revised for main 425146d (PR #46 department GET, PR #47
department-timezone conversion + interpretation provenance). Changes from v1:
the configured department's detail GET is allowed (that one id only), the bound
is 9, exact-format checks use fullmatch, and the run records and checks the
timezone outcome (VERIFIED / CONVERTED / UNVERIFIED), the zone shown in the
"when" column, the stub SMS body format (Q-B4, no zone label) and the
provenance record. The department id still stays in memory only.

Runs the accepted demonstrator code in athena_preview mode
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
  app:   1 OAuth token POST + 1 GET /v1/195900/departments/{selected id}
         + ≤ 5 GET /v1/195900/appointments/booked
  total ≤ 9. Any other request (incl. any other department id) is refused
  before sending.
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
                    r"<td>(.*?)</td></tr>", re.DOTALL)
APPT_RE = re.compile(r"^ath-e\d+-[1-9][0-9]{0,11}$")
PATIENT_RE = re.compile(r"^Sandbox patient …\d{1,4}$")
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
HOST = "api.preview.platform.athenahealth.com"
ALLOWED = {("POST", "/oauth2/v1/token"): "token", ("GET", "/v1/195900/departments"): "departments",
           ("GET", "/v1/195900/appointments/booked"): "booked"}
MAX_EXTERNAL = 9
TZ_LINES = (
    (re.compile(r"Sandbox department timezone matches clinic timezone \(([A-Za-z_/+\-]+)\)"
                r" — VERIFIED\."), "VERIFIED"),
    (re.compile(r"Sandbox department timezone ([A-Za-z_/+\-]+) differs from clinic timezone"
                r" [A-Za-z_/+\-]+ — appointment times are read and texted in [A-Za-z_/+\-]+\."),
     "CONVERTED"),
    (re.compile(r"Times assume the sandbox department is in ([A-Za-z_/+\-]+) — UNVERIFIED\."),
     "UNVERIFIED"),
)
BODY_RE = re.compile(r"Hi Sandbox patient …\d{1,4}, this is HaloVox Demo Clinic\. You have an"
                     r" appointment on ([A-Z][a-z]+day, [A-Z][a-z]{2} \d{2} at \d{1,2}:\d{2}"
                     r" [AP]M)\. Reply 1 to confirm, 2 to reschedule, or 3 to cancel\. Reply"
                     r" STOP to opt out\.")
DATE_FMT = re.compile(r"\d{2}/\d{2}/\d{4}")
TIME_FMT = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
WHEN_RE = re.compile(r"([A-Z][a-z]+day, [A-Z][a-z]{2} \d{2} at \d{1,2}:\d{2} [AP]M) ([A-Z]{3,5})")
MAX_PAGES = 3


class Stop(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CountingTransport(httpx.AsyncBaseTransport):
    """Allowlist + budget + sanitised counters (method, category, status)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, counters: list[dict[str, Any]],
                 budget: dict[str, int], dept: dict[str, str] | None = None,
                 raw_times: dict[str, tuple[str, str]] | None = None) -> None:
        self._inner, self._counters, self._budget = inner, counters, budget
        self._dept = dept if dept is not None else {}
        # Independent expectation source (memory only, never written): the booked
        # records' raw appointmentid -> (date, starttime), exact formats only.
        self._raw = raw_times if raw_times is not None else {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # F-SM2-1: compare the canonical RAW path (not the percent-decoded one), and
        # require https on the default port, before anything is sent.
        raw = request.url.raw_path.split(b"?", 1)[0]
        try:
            path = raw.decode("ascii")
        except UnicodeDecodeError:
            raise Stop("EXTERNAL_DISALLOWED") from None
        cat = ALLOWED.get((request.method, path))
        sel = self._dept.get("id")
        if cat is None and sel and request.method == "GET" \
                and path == f"/v1/195900/departments/{sel}":
            cat = "department"
        if (request.url.scheme != "https" or request.url.host != HOST
                or request.url.port not in (None, 443) or cat is None):
            raise Stop("EXTERNAL_DISALLOWED")
        if self._budget["used"] >= MAX_EXTERNAL:
            raise Stop("EXTERNAL_BUDGET")
        self._budget["used"] += 1
        resp = await self._inner.handle_async_request(request)
        self._counters.append({"method": request.method, "category": cat,
                               "status": resp.status_code})
        if cat == "department" and resp.status_code == 200:
            await resp.aread()
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001 — the app reports its own fixed code
                body = None
            if isinstance(body, list) and len(body) == 1:
                body = body[0]
            name = body.get("timezonename") if isinstance(body, dict) else None
            self._dept["tz"] = name if isinstance(name, str) else ""
        if cat == "booked" and resp.status_code == 200:
            await resp.aread()
            try:
                items = resp.json().get("appointments")
            except Exception:  # noqa: BLE001 — the app reports its own fixed code
                items = None
            for r in items if isinstance(items, list) else []:
                if not isinstance(r, dict):
                    continue
                aid, d, t = r.get("appointmentid"), r.get("date"), r.get("starttime")
                if (isinstance(aid, str) and ID_RE.fullmatch(aid) and isinstance(d, str)
                        and DATE_FMT.fullmatch(d) and isinstance(t, str)
                        and TIME_FMT.fullmatch(t)):
                    self._raw[aid] = (d, t)
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
        except Exception:  # noqa: BLE001 — never surface exception text
            raise Stop("SETUP_FAILED") from None
    for d in deps if isinstance(deps, list) else []:
        v = str(d.get("departmentid", "")) if isinstance(d, dict) else ""
        if ID_RE.fullmatch(v):
            return v
    raise Stop("SETUP_NO_DEPARTMENT")


def _with_db(url: str, db: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + db, p.query, ""))


def _db_must_not_exist(admin_url: str, db: str) -> None:
    if not DB_RE.fullmatch(db):
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
        if not APPT_RE.fullmatch(appt) or not PATIENT_RE.fullmatch(patient):
            raise Stop("VET_FAILED")
        rows.append({"appt": appt, "patient": patient, "when": when, "status": status})
    if html.count("<tr>") - 1 != len(rows):              # header row + appointment rows only
        raise Stop("VET_FAILED")
    return rows


def _iana(name: str) -> bool:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
    if name not in available_timezones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _abbrevs(zone: str, day: str) -> set[str]:
    """Zone abbreviations in effect at any hour of ``day`` (handles a DST day)."""
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo
    d = date.fromisoformat(day)
    z = ZoneInfo(zone)
    start = datetime(d.year, d.month, d.day, tzinfo=z)
    return {(start + timedelta(hours=h)).astimezone(z).tzname() or "" for h in range(26)}


def expected_local(d: str, t: str, zone: str) -> tuple[str, str]:
    """Independent of the app's formatting helpers: the expected
    '<Weekday>, <Mon> <dd> at <h:mm> <AM|PM>' text and zone abbreviation for a
    raw sandbox date + starttime read in ``zone``."""
    from zoneinfo import ZoneInfo
    mm, dd, yyyy = (int(x) for x in d.split("/"))
    hh, mi = (int(x) for x in t.split(":"))
    local = datetime(yyyy, mm, dd, hh, mi, tzinfo=ZoneInfo(zone))
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
              "Dec")
    h12 = hh % 12 or 12
    text = f"{days[local.weekday()]}, {months[mm - 1]} {dd:02d} at {h12}:{mi:02d} " \
           f"{'AM' if hh < 12 else 'PM'}"
    return text, local.tzname() or ""


def independent_tz(dept_tz: str, clinic: str) -> tuple[str, str, str]:
    """Expected (status, effective zone, basis) from the department zone the runner
    observed: exact recognized IANA name → VERIFIED (equal) or CONVERTED, basis
    DEPARTMENT; anything else → UNVERIFIED, clinic zone, basis FALLBACK."""
    if re.fullmatch(r"[A-Za-z0-9_/+\-. ]{1,64}", dept_tz) and _iana(dept_tz):
        return ("VERIFIED" if dept_tz == clinic else "CONVERTED"), dept_tz, "DEPARTMENT"
    return "UNVERIFIED", clinic, "FALLBACK"


def expected_utc(d: str, t: str, zone: str) -> datetime:
    """Independent expected instant for a raw sandbox date + starttime in ``zone``."""
    from zoneinfo import ZoneInfo
    mm, dd, yyyy = (int(x) for x in d.split("/"))
    hh, mi = (int(x) for x in t.split(":"))
    return datetime(yyyy, mm, dd, hh, mi, tzinfo=ZoneInfo(zone)).astimezone(UTC)


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
    dept_sel: dict[str, str] = {}
    raw_times: dict[str, tuple[str, str]] = {}
    summary: dict[str, Any] = {"runner": "athena_sandbox_smoke v2", "steps": [],
                               "started_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "write_to_athena": "none (simulated store only)",
                               "listeners": "not exercised (in-process ASGI; uvicorn "
                                            "listeners covered by the lifecycle tests)",
                               "sms": "stub (nothing sent)"}

    def tr() -> httpx.AsyncBaseTransport:
        inner = transport_factory() if transport_factory else httpx.AsyncHTTPTransport()
        return CountingTransport(inner, counters, budget, dept_sel, raw_times)

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
        dept_sel["id"] = dept
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
                                 ("Sandbox snapshot taken", "snapshot_banner_ok")):
                if needle not in html1:
                    raise Stop(f"MISSING_{step.upper()}")
                summary["steps"].append(step)
            found = [(st, m.group(1)) for rx, st in TZ_LINES if (m := rx.search(html1))]
            if len(found) != 1:
                raise Stop("TZ_LINE")
            tz_status, shown = found[0]
            clinic = settings.clinic_tz
            from haloflow_demo.selection import clinic_target_date
            day = clinic_target_date(datetime.now(UTC), clinic,
                                     days_before=settings.reminder_days_before).isoformat()
            # F-SM2-2: the expected outcome comes from the department response the
            # runner itself observed (memory only), not from the app's page.
            exp_status, eff, exp_basis = independent_tz(dept_sel.get("tz", ""), clinic)
            page_zone = shown if tz_status == "CONVERTED" else clinic
            if tz_status != exp_status or page_zone != eff or (
                    tz_status != "CONVERTED" and shown != clinic):
                raise Stop("TZ_NOT_INDEPENDENTLY_EXPECTED")
            summary["tz_status"], summary["effective_zone"] = tz_status, eff
            summary["steps"].append(f"tz_line_{tz_status.lower()}_ok")
            for r in rows:
                wm = WHEN_RE.fullmatch(r["when"])
                if wm is None or wm.group(2) not in _abbrevs(eff, day):
                    raise Stop("WHEN_ZONE")
                raw = raw_times.get(r["appt"].rsplit("-", 1)[1])
                if raw is None:
                    raise Stop("WHEN_NO_RAW_RECORD")
                exp_text, exp_abbr = expected_local(raw[0], raw[1], eff)
                if (wm.group(1), wm.group(2)) != (exp_text, exp_abbr):
                    raise Stop("WHEN_NOT_INDEPENDENTLY_EXPECTED")
            if rows:
                summary["steps"].append("when_column_matches_independent_expectation")
            summary["appointments_listed"] = len(rows)
            ex = re.search(r"Excluded records: ([^<]*)\.", html1)
            summary["excluded"] = ex.group(1) if ex else ""
            tok = _csrf(html1)
            booked1 = sum(1 for x in counters if x["category"] == "booked")
            html2 = await page()
            vet_page(html2, forbidden)
            booked = sum(1 for x in counters if x["category"] == "booked")
            if booked < 1 or booked > 5 or booked != booked1:
                raise Stop("BOOKED_COUNT")
            if sum(1 for x in counters if x["category"] == "department") != 1:
                raise Stop("DEPARTMENT_COUNT")
            summary["steps"].append("single_snapshot_reused_ok")
            summary["steps"].append("department_detail_once_for_selected_id_ok")
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
                calls = getattr(getattr(svc, "_gateway", None), "calls", None)
                if not isinstance(calls, list) or len(calls) != 1:
                    raise Stop("STUB_CALLS")
                bm = BODY_RE.fullmatch(calls[0][1])
                raw = raw_times[appt.rsplit("-", 1)[1]]
                if bm is None or bm.group(1) != expected_local(raw[0], raw[1], eff)[0]:
                    raise Stop("STUB_BODY")
                async with own.engine.connect() as conn:
                    from sqlalchemy import text
                    stored = (await conn.execute(text(
                        "SELECT appt_start FROM reminder_records WHERE id = :r"),
                        {"r": rid})).scalar_one()
                if stored != expected_utc(raw[0], raw[1], eff):
                    raise Stop("STORED_INSTANT_NOT_INDEPENDENTLY_EXPECTED")
                summary["steps"].append("stored_instant_matches_independent_utc")
                summary["steps"].append("stub_body_qb4_no_zone_label_ok")
                html_final = await page()
                vet_page(html_final, forbidden)
                if f"Task {tid} " not in html_final or " DONE" not in html_final:
                    raise Stop("WRITEBACK_NOT_DONE")
                summary["steps"].append("send_reply_writeback_simulated_ok")
            async with own.engine.connect() as conn:
                from sqlalchemy import text
                rec_value = (await conn.execute(text(
                    "SELECT value FROM demo_meta WHERE key = 'athena_tz_interpretation'"
                ))).scalar_one_or_none()
            basis = exp_basis
            if rec_value != f"v1;dept={dept};zone={eff};basis={basis};clinic={clinic}":
                raise Stop("PROVENANCE_RECORD")
            summary["provenance_basis"] = basis
            summary["steps"].append("provenance_record_ok (value kept in memory only)")
            if sum(1 for x in counters if x["category"] == "booked") != booked:
                raise Stop("SNAPSHOT_NOT_REUSED")
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
