"""N-TZ Step 3 — interpretation provenance contract (package
claude_n-tz-step3-provenance-package-2026-10-28-v2.md; Codex ALIGN
codex_n-tz-step3-v2-alignment.md, incl. the concurrent different-context case).

Invariant: every retained sandbox (``ath-…``) reminder was claimed under the same
interpretation context — (department_id, effective zone, basis, clinic_tz) — that
every later snapshot of the database uses. The record is the ``demo_meta`` row
``athena_tz_interpretation`` with the exact value
``v1;dept=<id>;zone=<IANA>;basis=<DEPARTMENT|FALLBACK>;clinic=<IANA>``.

Fixed source error codes: LEGACY_ATHENA_REMINDERS and TZ_PROVENANCE_UNSUPPORTED
(process-sticky, before any athena request); TZ_CONTEXT_CHANGED (after the
department GET, before any booked GET; sticky per date). Nothing is ever deleted
or rewritten. Synthetic mode never reads or writes the record.

No test touches the network; the SMS gateway is the in-process StubGateway, whose
``calls`` list counts every external send attempt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.athena_source import check_athena_mode
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.sending import StubGateway
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.web import build_staff_app

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # NY target date 2026-10-28
D1, D2 = date(2026, 10, 28), date(2026, 10, 29)
NY, DENVER, CHICAGO = "America/New_York", "America/Denver", "America/Chicago"
DEPT = "21"
DEPT_PATH = f"/v1/195900/departments/{DEPT}"
BOOKED = "/v1/195900/appointments/booked"
KEY = "athena_tz_interpretation"
TEST_NUMBER = "+15550100141"
ATHENA_ENV = {"DEMO_EHR_SOURCE": "athena_preview", "DEMO_ATHENA_DEPARTMENT_ID": DEPT,
              "ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
              "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID",
              "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET"}


def ctx(zone: str, basis: str, clinic: str = NY) -> str:
    return f"v1;dept={DEPT};zone={zone};basis={basis};clinic={clinic}"


class Api:
    """Department zones served in order (the last repeats); ``None`` → field absent.
    Booked: one 09:00 record (patient 60178) on whatever date is requested;
    appointment id 1001 on 10/28/2026 and 2001 on any other date (ids are unique
    across a process's snapshots)."""

    def __init__(self, *zones: str | None) -> None:
        self.zones = list(zones)
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "SENTINEL-TOKEN",
                                             "expires_in": 3600})
        if req.url.path == DEPT_PATH:
            tz = self.zones.pop(0) if len(self.zones) > 1 else self.zones[0]
            body: dict[str, Any] = {"departmentid": DEPT, "name": "SENTINEL-dept"}
            if tz is not None:
                body["timezonename"] = tz
            return httpx.Response(200, json=[body])
        if req.url.path == BOOKED:
            d = req.url.params["startdate"]
            aid = "1001" if d == "10/28/2026" else "2001"
            recs = [{"appointmentid": aid, "patientid": "60178", "date": d,
                     "starttime": "09:00", "appointmentstatus": "f", "departmentid": DEPT,
                     "patient": {"firstname": "SENTINEL-Donna"}}]
            return httpx.Response(200, json={"appointments": recs, "totalcount": 1})
        return httpx.Response(404)

    def count(self, path: str) -> int:
        return sum(r.url.path == path for r in self.requests)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def for_target(self, d: date) -> None:
        self.now = datetime(d.year, d.month, d.day, 13, 0, tzinfo=UTC) - timedelta(days=2)


def _env(demo_env: dict[str, str], **over: str) -> dict[str, str]:
    return {**demo_env, **ATHENA_ENV, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER,
            "DEMO_SMS_SENDER": "+15550100100",
            "DEMO_CALLBACK_SECRET": "test-callback-secret-0123456789", **over}


async def proc(env: dict[str, str], api: Api, gw: StubGateway,
               clock: Clock | None = None) -> tuple[Any, DbStaffService]:
    """One demo process: ownership (epoch +1) and a service on ``env``."""
    clock = clock or Clock()
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    svc = DbStaffService(own, s, now=lambda: clock.now, gateway=gw,
                         athena=check_athena_mode(s, env),
                         athena_transport=httpx.MockTransport(api))
    return own, svc


async def stop(own: Any, svc: DbStaffService) -> None:
    await svc.aclose()
    await own.close()


async def tables(env: dict[str, str]) -> None:
    """Create the demo tables (one synthetic-mode ownership), then release."""
    s = DemoSettings.from_env({k: v for k, v in env.items() if not k.startswith(
        ("DEMO_EHR_SOURCE", "DEMO_ATHENA", "ATHENA_"))})
    own = await acquire_ownership(s, validate_demo_database_url(s))
    await own.close()


def sql(url: str, q: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(q, params)
        return cur.fetchall() if cur.description else []


def marker(url: str) -> str | None:
    rows = sql(url, "SELECT value FROM demo_meta WHERE key = %s", (KEY,))
    return rows[0][0] if rows else None


def insert_reminder(url: str, appt_id: str, patient: str, start: datetime) -> None:
    sql(url, "INSERT INTO reminder_records (tenant_id, emr_appt_id, emr_patient_id,"
             " appt_start, state, mode) VALUES ('demo-clinic', %s, %s, %s, 'SUBMITTED',"
             " 'stub')", (appt_id, patient, start))


def reminders(url: str) -> list[tuple[str, datetime]]:
    return [(r[0], r[1]) for r in sql(
        url, "SELECT emr_appt_id, appt_start FROM reminder_records ORDER BY id")]


async def html_of(own: Any, svc: DbStaffService) -> str:
    app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8080") as c:
        return (await c.get("/demo/staff")).text


T13 = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)         # 09:00 read as New York
T15 = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)         # 09:00 read as Denver


# ── P0: before any athena request ────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "department_tz_v1",                                                  # v1r1 form
    "v2;dept=21;zone=America/Denver;basis=DEPARTMENT;clinic=America/New_York",
    "v1;dept=21;zone=America/Denver;basis=DEPARTMENT",                   # missing field
    "v1;dept=21;zone=Mountain Time;basis=DEPARTMENT;clinic=America/New_York",
    "v1;dept=21;zone=America/Denver;basis=OTHER;clinic=America/New_York",
    "v1;dept=21;zone=America/Denver;basis=DEPARTMENT;clinic=America/New_York;x=1",
    "v1;dept=x;zone=America/Denver;basis=DEPARTMENT;clinic=America/New_York",
    # F-TZ3-1 (Codex review of 3b95d16): a valid context plus a trailing newline.
    "v1;dept=21;zone=America/Denver;basis=DEPARTMENT;clinic=America/New_York\n",
])
async def test_p1_unsupported_value_refused_before_requests(
    demo_env: dict[str, str], demo_db_url: str, bad: str,
) -> None:
    env = _env(demo_env)
    await tables(env)
    sql(demo_db_url, "INSERT INTO demo_meta(key, value) VALUES (%s, %s)", (KEY, bad))
    api, gw = Api(DENVER), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, api, gw)
    try:
        data = await svc.page_data()
        again = await svc.page_data()
        sent = await svc.send_to_appointment("ath-e2-1001")
        batch = await svc.send_batch()
    finally:
        await stop(own, svc)
    assert data["source"]["error"] == again["source"]["error"] == "TZ_PROVENANCE_UNSUPPORTED"
    assert data["appointments"] == [] and batch == [] and sent["state"] == "SKIPPED"
    assert api.requests == [] and gw.calls == []
    assert marker(demo_db_url) == bad


async def test_p2_unmarked_legacy_refused_untouched(demo_env: dict[str, str],
                                                    demo_db_url: str) -> None:
    env = _env(demo_env)
    await tables(env)
    insert_reminder(demo_db_url, "ath-e1-1001", "athp-60178", T13)
    api, gw = Api(DENVER), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, api, gw)
    try:
        data = await svc.page_data()
        sent = await svc.send_to_appointment("ath-e2-1001")
        batch = await svc.send_batch()
    finally:
        await stop(own, svc)
    assert data["source"]["error"] == "LEGACY_ATHENA_REMINDERS"
    assert data["appointments"] == [] and batch == [] and sent["state"] == "SKIPPED"
    assert api.requests == [] and gw.calls == []
    assert reminders(demo_db_url) == [("ath-e1-1001", T13)]
    assert marker(demo_db_url) is None


# ── P1: context compare after the department GET ─────────────────────────────


async def test_p3_marked_fallback_then_recognized_refused(demo_env: dict[str, str],
                                                          demo_db_url: str) -> None:
    env, gw = _env(demo_env), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, Api(None), gw)                     # metadata missing
    try:
        first = await svc.send_to_appointment("ath-e1-1001")
    finally:
        await stop(own, svc)
    assert first["state"] == "SUBMITTED"
    assert marker(demo_db_url) == ctx(NY, "FALLBACK")
    assert reminders(demo_db_url) == [("ath-e1-1001", T13)]
    api = Api(DENVER)                                             # metadata now recognized
    own, svc = await proc(env, api, gw)
    try:
        data = await svc.page_data()
        again = await svc.send_to_appointment("ath-e2-1001")
        batch = await svc.send_batch()
    finally:
        await stop(own, svc)
    assert data["source"]["error"] == "TZ_CONTEXT_CHANGED"
    assert again["state"] == "SKIPPED" and batch == []
    assert api.count("/oauth2/v1/token") == 1 and api.count(DEPT_PATH) == 1
    assert api.count(BOOKED) == 0
    assert len(gw.calls) == 1                                     # no second send
    assert reminders(demo_db_url) == [("ath-e1-1001", T13)]       # untouched
    assert marker(demo_db_url) == ctx(NY, "FALLBACK")             # unchanged


async def test_p4_recognized_zone_change_refused(demo_env: dict[str, str],
                                                 demo_db_url: str) -> None:
    env, gw = _env(demo_env), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, Api(DENVER), gw)
    try:
        assert (await svc.send_to_appointment("ath-e1-1001"))["state"] == "SUBMITTED"
    finally:
        await stop(own, svc)
    api = Api(CHICAGO)
    own, svc = await proc(env, api, gw)
    try:
        data = await svc.page_data()
        again = await svc.send_to_appointment("ath-e2-1001")
    finally:
        await stop(own, svc)
    assert data["source"]["error"] == "TZ_CONTEXT_CHANGED" and again["state"] == "SKIPPED"
    assert api.count(BOOKED) == 0 and len(gw.calls) == 1
    assert reminders(demo_db_url) == [("ath-e1-1001", T15)]
    assert marker(demo_db_url) == ctx(DENVER, "DEPARTMENT")


async def test_p5_stable_restart_dedupes_same_slot(demo_env: dict[str, str],
                                                   demo_db_url: str) -> None:
    env, gw = _env(demo_env), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, Api(DENVER), gw)
    try:
        assert (await svc.send_to_appointment("ath-e1-1001"))["state"] == "SUBMITTED"
    finally:
        await stop(own, svc)
    api = Api(DENVER)
    own, svc = await proc(env, api, gw)
    try:
        data = await svc.page_data()
        again = await svc.send_to_appointment("ath-e2-1001")
    finally:
        await stop(own, svc)
    assert data["source"]["error"] is None and api.count(BOOKED) == 1
    assert [a["emr_appt_id"] for a in data["appointments"]] == ["ath-e2-1001"]
    assert again["state"] == "SKIPPED"
    assert len(gw.calls) == 1 and reminders(demo_db_url) == [("ath-e1-1001", T15)]
    assert marker(demo_db_url) == ctx(DENVER, "DEPARTMENT")


async def test_p6_clinic_zone_change_refused(demo_env: dict[str, str],
                                             demo_db_url: str) -> None:
    gw = StubGateway(TEST_NUMBER)
    own, svc = await proc(_env(demo_env), Api(DENVER), gw)
    try:
        assert (await svc.send_to_appointment("ath-e1-1001"))["state"] == "SUBMITTED"
    finally:
        await stop(own, svc)
    api = Api(DENVER)
    own, svc = await proc(_env(demo_env, CLINIC_TZ=CHICAGO), api, gw)
    try:
        data = await svc.page_data()
    finally:
        await stop(own, svc)
    assert data["source"]["error"] == "TZ_CONTEXT_CHANGED" and api.count(BOOKED) == 0
    assert len(gw.calls) == 1 and marker(demo_db_url) == ctx(DENVER, "DEPARTMENT")


async def test_p7_reestablish_only_without_retained_athena_reminders(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    await tables(env)
    sql(demo_db_url, "INSERT INTO demo_meta(key, value) VALUES (%s, %s)",
        (KEY, ctx(CHICAGO, "DEPARTMENT")))
    api, gw = Api(DENVER), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, api, gw)
    try:
        data = await svc.page_data()
        sent = await svc.send_to_appointment("ath-e2-1001")
    finally:
        await stop(own, svc)
    assert data["source"]["error"] is None and sent["state"] == "SUBMITTED"
    assert marker(demo_db_url) == ctx(DENVER, "DEPARTMENT")


async def test_p8_in_process_change_refused_first_date_kept(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    clock, gw = Clock(), StubGateway(TEST_NUMBER)
    api = Api(DENVER, CHICAGO)                     # date A: Denver; date B: Chicago
    own, svc = await proc(_env(demo_env), api, gw, clock)
    try:
        assert (await svc.send_to_appointment("ath-e1-1001"))["state"] == "SUBMITTED"
        clock.for_target(D2)
        second = await svc.page_data()
        clock.for_target(D1)
        first = await svc.page_data()
    finally:
        await stop(own, svc)
    assert second["source"]["error"] == "TZ_CONTEXT_CHANGED"
    assert second["appointments"] == []
    assert first["source"]["error"] is None
    assert [a["emr_appt_id"] for a in first["appointments"]] == ["ath-e1-1001"]
    starts = [r.url.params["startdate"] for r in api.requests if r.url.path == BOOKED]
    assert starts == ["10/28/2026"]                               # no booked GET for B
    assert len(gw.calls) == 1 and marker(demo_db_url) == ctx(DENVER, "DEPARTMENT")


async def test_p9_concurrent_first_use_equal_context_converges(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    api, gw = Api(DENVER), StubGateway(TEST_NUMBER)
    own, svc = await proc(_env(demo_env), api, gw)
    try:
        a, b = await asyncio.gather(svc._source(D1), svc._source(D2))  # noqa: SLF001
        meta = [svc._source_block(d) for d in (D1, D2)]                  # noqa: SLF001
    finally:
        await stop(own, svc)
    assert a is not None and b is not None
    assert [m["error"] for m in meta] == [None, None]
    assert sql(demo_db_url, "SELECT count(*) FROM demo_meta WHERE key = %s", (KEY,)) == [(1,)]
    assert marker(demo_db_url) == ctx(DENVER, "DEPARTMENT")
    assert gw.calls == []


async def test_p9b_concurrent_first_use_different_context_one_winner(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    clock, gw = Clock(), StubGateway(TEST_NUMBER)
    api = Api(DENVER, CHICAGO)                     # the two first department reads differ
    own, svc = await proc(_env(demo_env), api, gw, clock)
    try:
        results = await asyncio.gather(svc._source(D1), svc._source(D2))  # noqa: SLF001
        errors = [svc._source_block(d)["error"] for d in (D1, D2)]        # noqa: SLF001
        winner = D1 if results[0] is not None else D2
        loser = D2 if winner == D1 else D1
        stored = marker(demo_db_url)
        clock.for_target(winner)
        page = await svc.page_data()
        sent = await svc.send_to_appointment(page["appointments"][0]["emr_appt_id"])
        clock.for_target(loser)
        refused = await svc.page_data()
    finally:
        await stop(own, svc)
    assert sorted(e or "" for e in errors) == ["", "TZ_CONTEXT_CHANGED"]
    assert sum(r is not None for r in results) == 1
    assert stored in (ctx(DENVER, "DEPARTMENT"), ctx(CHICAGO, "DEPARTMENT"))
    booked = [r.url.params["startdate"] for r in api.requests if r.url.path == BOOKED]
    assert booked == [winner.strftime("%m/%d/%Y")]                  # loser: 0 booked
    assert page["source"]["error"] is None and sent["state"] == "SUBMITTED"
    assert refused["source"]["error"] == "TZ_CONTEXT_CHANGED" and refused["appointments"] == []
    assert len(gw.calls) == 1 and marker(demo_db_url) == stored


# ── scope / display ──────────────────────────────────────────────────────────


async def test_p10_synthetic_mode_never_reads_or_writes(demo_env: dict[str, str],
                                                        demo_db_url: str) -> None:
    await tables(demo_env)
    insert_reminder(demo_db_url, "ath-e1-1001", "athp-60178", T13)
    sql(demo_db_url, "INSERT INTO demo_meta(key, value) VALUES (%s, 'garbage')", (KEY,))
    s = DemoSettings.from_env(demo_env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    try:
        data = await DbStaffService(own, s, now=lambda: NOW).page_data()
    finally:
        await own.close()
    assert data["source"] == {"kind": "synthetic"}
    assert data["appointments"] and data["appointments"][0]["emr_appt_id"].startswith("syn-e")
    assert marker(demo_db_url) == "garbage"


async def test_p11_synthetic_rows_are_not_legacy(demo_env: dict[str, str],
                                                 demo_db_url: str) -> None:
    env = _env(demo_env)
    await tables(env)
    insert_reminder(demo_db_url, "syn-e1-20261028-a-1", "syn-p-1", T13)
    api, gw = Api(DENVER), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, api, gw)
    try:
        data = await svc.page_data()
    finally:
        await stop(own, svc)
    assert data["source"]["error"] is None and api.count(BOOKED) == 1
    assert marker(demo_db_url) == ctx(DENVER, "DEPARTMENT")


async def test_p12_banners_exact_and_no_leak(demo_env: dict[str, str],
                                             demo_db_url: str) -> None:
    env, gw = _env(demo_env), StubGateway(TEST_NUMBER)
    own, svc = await proc(env, Api(None), gw)
    try:
        await svc.send_to_appointment("ath-e1-1001")
    finally:
        await stop(own, svc)
    own, svc = await proc(env, Api(DENVER), gw)
    try:
        changed = await html_of(own, svc)
    finally:
        await stop(own, svc)
    assert "athenahealth sandbox unavailable — TZ_CONTEXT_CHANGED" in changed
    assert ("Sandbox appointment times would now be read in America/Denver (DEPARTMENT)"
            " but earlier sandbox reminders in this database used America/New_York"
            " (FALLBACK); use a fresh demo database.") in changed
    sql(demo_db_url, "DELETE FROM demo_meta WHERE key = %s", (KEY,))   # → legacy shape
    own, svc = await proc(env, Api(DENVER), gw)
    try:
        legacy = await html_of(own, svc)
    finally:
        await stop(own, svc)
    assert "athenahealth sandbox unavailable — LEGACY_ATHENA_REMINDERS" in legacy
    assert ("This demo database holds sandbox reminders from before timezone conversion;"
            " use a fresh demo database.") in legacy
    sql(demo_db_url, "INSERT INTO demo_meta(key, value) VALUES (%s, %s)",
        (KEY, "v1;dept=21;zone=SENTINEL<script>;basis=DEPARTMENT;clinic=America/New_York"))
    own, svc = await proc(env, Api(DENVER), gw)
    try:
        unsupported = await html_of(own, svc)
    finally:
        await stop(own, svc)
    assert "athenahealth sandbox unavailable — TZ_PROVENANCE_UNSUPPORTED" in unsupported
    assert ("This demo database's timezone record is not recognized; use a fresh demo"
            " database.") in unsupported
    for page in (changed, legacy, unsupported):
        assert "SENTINEL" not in page and "<script" not in page


async def test_p13_fallback_context_visible_and_sends(demo_env: dict[str, str],
                                                      demo_db_url: str) -> None:
    gw = StubGateway(TEST_NUMBER)
    own, svc = await proc(_env(demo_env), Api(None), gw)
    try:
        html = await html_of(own, svc)
        sent = await svc.send_to_appointment("ath-e1-1001")
    finally:
        await stop(own, svc)
    assert "Times assume the sandbox department is in America/New_York — UNVERIFIED." in html
    assert sent["state"] == "SUBMITTED" and len(gw.calls) == 1
    assert marker(demo_db_url) == ctx(NY, "FALLBACK")
