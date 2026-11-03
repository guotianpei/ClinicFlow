"""N-TZ Step 3 — convert to the EHR department's timezone (packages
claude_n-tz-step3-package-2026-10-28-v1.md + v1r1 + provenance v2; Codex ALIGN
codex_n-tz-step3-v2-alignment.md; owner decisions OD-TZ1 "Convert and send" and
OD-TZ2 "Keep text, no label").

API under test (to be implemented):

- ``SyntheticAppointment.display_tz: str | None`` — the zone the appointment's local
  date/time belong to; None means CLINIC_TZ (synthetic fixtures never set it).
- ``Snapshot.tz_status`` in VERIFIED | CONVERTED | UNDETERMINED; ``Snapshot.eff_tz``
  (the zone booked times were read in); ``Snapshot.basis`` in DEPARTMENT | FALLBACK.
- Appointment date filtering, selection, the staff "when" column and the reminder
  text use ``display_tz or CLINIC_TZ``; the target clinic date and the send-quota
  day stay in CLINIC_TZ.

No test touches the network: every transport is httpx.MockTransport; the SMS
gateway is the in-process StubGateway.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.athena_source import AthenaPreviewClient, check_athena_mode, fetch_snapshot
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.sending import StubGateway, reminder_text
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.web import build_staff_app

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # NY target date 2026-10-28
TARGET = date(2026, 10, 28)
NY, DENVER = "America/New_York", "America/Denver"
DEPT = "21"
DEPT_PATH = f"/v1/195900/departments/{DEPT}"
BOOKED = "/v1/195900/appointments/booked"
TEST_NUMBER = "+15550100141"
ATHENA_ENV = {"DEMO_EHR_SOURCE": "athena_preview", "DEMO_ATHENA_DEPARTMENT_ID": DEPT,
              "ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
              "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID",
              "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET"}


def rec(appt_id: str, patient: str, d: str, t: str) -> dict[str, Any]:
    return {"appointmentid": appt_id, "patientid": patient, "date": d, "starttime": t,
            "appointmentstatus": "f", "departmentid": DEPT}


class Api:
    """Department zone ``tz`` (None → field absent); booked records by date."""

    def __init__(self, tz: str | None, records: list[dict[str, Any]] | None = None) -> None:
        self.tz = tz
        self.records = [rec("1001", "60178", "10/28/2026", "09:00")] if records is None \
            else records
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "SENTINEL-TOKEN",
                                             "expires_in": 3600})
        if req.url.path == DEPT_PATH:
            body: dict[str, Any] = {"departmentid": DEPT}
            if self.tz is not None:
                body["timezonename"] = self.tz
            return httpx.Response(200, json=[body])
        if req.url.path == BOOKED:
            d = req.url.params["startdate"]
            recs = [r for r in self.records if r["date"] == d]
            return httpx.Response(200, json={"appointments": recs, "totalcount": len(recs)})
        return httpx.Response(404)


async def snap(api: Api, target: date = TARGET, tz: str = NY) -> Any:
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo",
                               **ATHENA_ENV})
    cfg = check_athena_mode(s, ATHENA_ENV)
    assert cfg is not None
    client = AthenaPreviewClient(cfg, transport=httpx.MockTransport(api))
    try:
        return await fetch_snapshot(client, target, tz, DEPT, 1)
    finally:
        await client.aclose()


def _env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env, **ATHENA_ENV, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER,
            "DEMO_SMS_SENDER": "+15550100100",
            "DEMO_CALLBACK_SECRET": "test-callback-secret-0123456789"}


async def _svc(demo_env: dict[str, str], api: Api, now: datetime = NOW,
               gw: StubGateway | None = None) -> tuple[Any, DbStaffService, StubGateway]:
    env = _env(demo_env)
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    gw = gw or StubGateway(TEST_NUMBER)
    svc = DbStaffService(own, s, now=lambda: now, gateway=gw,
                         athena=check_athena_mode(s, env),
                         athena_transport=httpx.MockTransport(api))
    return own, svc, gw


async def _html(own: Any, svc: DbStaffService) -> str:
    app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8080") as c:
        return (await c.get("/demo/staff")).text


def _q(url: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(sql).fetchall()


# ── snapshot ─────────────────────────────────────────────────────────────────


async def test_c1_denver_read_in_department_zone() -> None:
    s = await snap(Api(DENVER))
    assert s.tz_status == "CONVERTED" and s.eff_tz == DENVER and s.basis == "DEPARTMENT"
    a = s.fixtures.appointments[0]
    assert a.start_utc == datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    assert a.display_tz == DENVER


async def test_c5_dst_exclusions_computed_in_department_zone() -> None:
    fall = await snap(Api(DENVER, [rec("1001", "60178", "11/01/2026", "01:30"),
                                   rec("1002", "60179", "11/01/2026", "09:00")]),
                      target=date(2026, 11, 1))
    assert [a.emr_appt_id for a in fall.fixtures.appointments] == ["ath-e1-1002"]
    assert fall.fixtures.appointments[0].start_utc == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
    assert fall.counts == {"AMBIGUOUS_TIME": 1}
    spring = await snap(Api(DENVER, [rec("1001", "60178", "03/14/2027", "02:30"),
                                     rec("1002", "60179", "03/14/2027", "03:30")]),
                        target=date(2027, 3, 14))
    assert [a.emr_appt_id for a in spring.fixtures.appointments] == ["ath-e1-1002"]
    assert spring.fixtures.appointments[0].start_utc == datetime(2027, 3, 14, 9, 30,
                                                                 tzinfo=UTC)
    assert spring.counts == {"NONEXISTENT_TIME": 1}


async def test_c7_matching_zone_verified_no_display_zone() -> None:
    s = await snap(Api(NY))
    assert s.tz_status == "VERIFIED" and s.eff_tz == NY and s.basis == "DEPARTMENT"
    a = s.fixtures.appointments[0]
    assert a.start_utc == datetime(2026, 10, 28, 13, 0, tzinfo=UTC) and a.display_tz is None


async def test_c8_alias_converts_with_identical_times() -> None:
    s = await snap(Api("US/Eastern"))
    assert s.tz_status == "CONVERTED" and s.eff_tz == "US/Eastern"
    a = s.fixtures.appointments[0]
    assert a.start_utc == datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
    assert a.display_tz == "US/Eastern"


@pytest.mark.parametrize("tz", [None, "Eastern Standard Time", "Not/AZone"])
async def test_c9_unknown_falls_back_to_clinic_zone(tz: str | None) -> None:
    s = await snap(Api(tz))
    assert s.tz_status == "UNDETERMINED" and s.eff_tz == NY and s.basis == "FALLBACK"
    a = s.fixtures.appointments[0]
    assert a.start_utc == datetime(2026, 10, 28, 13, 0, tzinfo=UTC) and a.display_tz is None


# ── service / page / send ────────────────────────────────────────────────────


async def test_c2_page_when_column_and_banner(demo_env: dict[str, str]) -> None:
    own, svc, _gw = await _svc(demo_env, Api(DENVER))
    try:
        data = await svc.page_data()
        html = await _html(own, svc)
    finally:
        await svc.aclose()
        await own.close()
    assert data["appointments"][0]["when"] == "Wednesday, Oct 28 at 9:00 AM MDT"
    assert data["source"]["tz_status"] == "CONVERTED"
    assert ("Sandbox department timezone America/Denver differs from clinic timezone"
            " America/New_York — appointment times are read and texted in"
            " America/Denver.") in html
    assert "UNVERIFIED" not in html


async def test_c3_reminder_text_in_department_time_exact_wording(
    demo_env: dict[str, str],
) -> None:
    own, svc, gw = await _svc(demo_env, Api(DENVER))
    try:
        sent = await svc.send_to_appointment("ath-e1-1001")
    finally:
        await svc.aclose()
        await own.close()
    assert sent["state"] == "SUBMITTED", sent
    assert len(gw.calls) == 1 and gw.calls[0][0] == TEST_NUMBER
    body = gw.calls[0][1]
    assert body == ("Hi Sandbox patient …0178, this is HaloVox Demo Clinic. You have an"
                    " appointment on Wednesday, Oct 28 at 9:00 AM. Reply 1 to confirm,"
                    " 2 to reschedule, or 3 to cancel. Reply STOP to opt out.")
    assert body == reminder_text("Sandbox patient …0178",
                                 datetime(2026, 10, 28, 15, 0, tzinfo=UTC), DENVER)
    assert "MDT" not in body and "MST" not in body                         # OD-TZ2: no label


async def test_c4_late_department_slot_stays_on_department_date(
    demo_env: dict[str, str],
) -> None:
    # Denver 10/28 23:30 = 10/29 05:30Z = NY 10/29 01:30: kept for target 10/28.
    api = Api(DENVER, [rec("1001", "60178", "10/28/2026", "23:30")])
    own, svc, _gw = await _svc(demo_env, api)
    try:
        data = await svc.page_data()
        preview = await svc.preview_batch()
    finally:
        await svc.aclose()
        await own.close()
    assert [a["emr_appt_id"] for a in data["appointments"]] == ["ath-e1-1001"]
    assert data["appointments"][0]["when"] == "Wednesday, Oct 28 at 11:30 PM MDT"
    assert data["appointments"][0]["eligible"] is True
    assert preview == [{"emr_appt_id": "ath-e1-1001", "decision": "eligible"}]


async def test_c6_quota_day_stays_clinic_day(demo_env: dict[str, str],
                                             demo_db_url: str) -> None:
    # 2026-10-27 04:30Z: New York 10-27 00:30, Denver 10-26 22:30.
    now = datetime(2026, 10, 27, 4, 30, tzinfo=UTC)
    api = Api(DENVER, [rec("1001", "60178", "10/29/2026", "09:00")])
    own, svc, _gw = await _svc(demo_env, api, now=now)
    try:
        data = await svc.page_data()
        sent = await svc.send_to_appointment("ath-e1-1001")
    finally:
        await svc.aclose()
        await own.close()
    assert data["target_date"] == "2026-10-29"                       # NY date + 2
    assert sent["state"] == "SUBMITTED", sent
    assert _q(demo_db_url, "SELECT clinic_day::text, used FROM send_quota") == \
        [("2026-10-27", 1)]


async def test_c9_page_unknown_keeps_unverified_and_clinic_times(
    demo_env: dict[str, str],
) -> None:
    own, svc, gw = await _svc(demo_env, Api(None))
    try:
        data = await svc.page_data()
        html = await _html(own, svc)
        sent = await svc.send_to_appointment("ath-e1-1001")
    finally:
        await svc.aclose()
        await own.close()
    assert data["appointments"][0]["when"] == "Wednesday, Oct 28 at 9:00 AM EDT"
    assert "Times assume the sandbox department is in America/New_York — UNVERIFIED." in html
    assert sent["state"] == "SUBMITTED" and "on Wednesday, Oct 28 at 9:00 AM." in gw.calls[0][1]


async def test_c12_synthetic_reminder_text_unchanged(demo_env: dict[str, str]) -> None:
    env = {**demo_env, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER}
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    gw = StubGateway(TEST_NUMBER)
    try:
        svc = DbStaffService(own, s, now=lambda: NOW, gateway=gw)
        data = await svc.page_data()
        target_id = next(a["emr_appt_id"] for a in data["appointments"] if a["eligible"])
        sent = await svc.send_to_appointment(target_id)
        fx = svc.ehr_for_date(TARGET).fixtures
    finally:
        await own.close()
    appt = next(a for a in fx.appointments if a.emr_appt_id == target_id)
    assert all(a.display_tz is None for a in fx.appointments)
    assert sent["state"] == "SUBMITTED", sent
    assert gw.calls[0][1] == reminder_text(
        fx.patients[appt.emr_patient_id].display_name.removeprefix("Synthetic "),
        appt.start_utc, NY)
