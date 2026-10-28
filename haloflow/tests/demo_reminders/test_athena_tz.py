"""N-TZ Step 2: sandbox department timezone check in the athena source
(package claude_n-tz-step2-package-2026-10-28-v1.md, Codex ALIGN
codex_n-tz-step2-alignment.md).

API under test (to be implemented in haloflow_demo/athena_source.py, service.py, web.py):

- ``classify_tz(name, clinic_tz, target) -> "VERIFIED" | "MISMATCH" | "UNDETERMINED"``
  VERIFIED only for a recognized IANA name exactly equal to ``clinic_tz``; MISMATCH
  when a recognized IANA name has a different UTC offset than ``clinic_tz`` at some
  instant of the target clinic date; UNDETERMINED otherwise (never upgraded).
- ``extract_tz(body, department_id) -> str | None`` — the bounded, recognized IANA
  ``timezonename`` or None; raises ``SourceInvalid("SOURCE_INVALID")`` on a malformed
  body or a conflicting ``departmentid``.
- ``fetch_snapshot`` reads ``GET /v1/195900/departments/{configured id}`` after the
  token and before any booked page; MISMATCH raises
  ``SourceInvalid("SOURCE_TZ_MISMATCH", detail=<IANA name>)``.
- ``Snapshot.tz_status`` / ``Snapshot.dept_tz``; ``page_data()["source"]`` gains
  ``tz_status`` and ``dept_tz``.

No test touches the network: every transport is httpx.MockTransport.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from haloflow_demo.athena_source import (
    AthenaPreviewClient,
    SourceInvalid,
    check_athena_mode,
    classify_tz,
    extract_tz,
    fetch_snapshot,
)
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.web import build_staff_app

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # target clinic date 2026-10-28
TARGET = date(2026, 10, 28)
TZ = "America/New_York"
DEPT = "21"
DEPT_PATH = f"/v1/195900/departments/{DEPT}"
BOOKED = "/v1/195900/appointments/booked"
ATHENA_ENV = {"DEMO_EHR_SOURCE": "athena_preview", "DEMO_ATHENA_DEPARTMENT_ID": DEPT,
              "ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
              "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID",
              "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET"}
SENTINELS = ("SENTINEL", "Donna", "<script")


def rec(appt_id: str, patient: str = "60178", t: str = "09:00") -> dict[str, Any]:
    return {"appointmentid": appt_id, "patientid": patient, "date": "10/28/2026",
            "starttime": t, "appointmentstatus": "f", "departmentid": DEPT,
            "patient": {"firstname": "Donna"}}


def dept_body(**fields: Any) -> list[dict[str, Any]]:
    return [{"departmentid": DEPT, "name": "SENTINEL-dept", "address": "SENTINEL-addr",
             **fields}]


class Api:
    def __init__(self, dept: Any = None, n: int = 2, dept_status: int = 200) -> None:
        self.dept = dept_body(timezonename=TZ) if dept is None else dept
        self.dept_status, self.n = dept_status, n
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "SENTINEL-TOKEN",
                                             "expires_in": 3600})
        if req.url.path == DEPT_PATH:
            if self.dept_status != 200:
                return httpx.Response(self.dept_status, text="SENTINEL-body")
            if isinstance(self.dept, str):
                return httpx.Response(200, text=self.dept)
            return httpx.Response(200, json=self.dept)
        if req.url.path == BOOKED:
            off, lim = int(req.url.params["offset"]), int(req.url.params["limit"])
            recs = [rec(str(1000 + i), str(60000 + i), f"{8 + i % 9:02d}:{(i * 5) % 60:02d}")
                    for i in range(self.n)]
            return httpx.Response(200, json={"appointments": recs[off:off + lim],
                                             "totalcount": self.n})
        return httpx.Response(404, text="SENTINEL-404")

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def booked(self) -> int:
        return sum(r.url.path == BOOKED for r in self.requests)


def config() -> Any:
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo",
                               **ATHENA_ENV})
    cfg = check_athena_mode(s, ATHENA_ENV)
    assert cfg is not None
    return cfg


async def snap(api: Api, tz: str = TZ, target: date = TARGET) -> Any:
    client = AthenaPreviewClient(config(), transport=httpx.MockTransport(api))
    try:
        return await fetch_snapshot(client, target, tz, DEPT, 1)
    finally:
        await client.aclose()


def no_leak(*texts: str) -> None:
    blob = "\n".join(texts)
    for s in SENTINELS:
        assert s not in blob, s


def _env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env, **ATHENA_ENV, "DEMO_SMS_TEST_NUMBER": "+15550100141",
            "DEMO_SMS_SENDER": "+15550100100",
            "DEMO_CALLBACK_SECRET": "test-callback-secret-0123456789"}


async def _page(demo_env: dict[str, str], api: Api) -> tuple[dict[str, Any], str, Any, Any]:
    env = _env(demo_env)
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    svc = DbStaffService(own, s, now=lambda: NOW, athena=check_athena_mode(s, env),
                         athena_transport=httpx.MockTransport(api))
    app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8080") as c:
        html = (await c.get("/demo/staff")).text
    data = await svc.page_data()
    return data, html, own, svc


# ── classifier (pure) ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,clinic,target,expected", [
    ("America/New_York", "America/New_York", TARGET, "VERIFIED"),
    ("America/Denver", "America/New_York", TARGET, "MISMATCH"),               # live fact
    ("US/Eastern", "America/New_York", TARGET, "UNDETERMINED"),               # TZ-4 alias
    ("America/Toronto", "America/New_York", date(2026, 11, 1), "UNDETERMINED"),
    ("America/Phoenix", "America/Denver", date(2027, 1, 15), "UNDETERMINED"),  # TZ-7 winter
    ("America/Phoenix", "America/Denver", date(2026, 7, 15), "MISMATCH"),      # TZ-7 summer
    # TZ-7 DST transition: equal offsets the day before, differ only from 06:00Z on 11-01.
    ("America/Santo_Domingo", "America/New_York", date(2026, 10, 31), "UNDETERMINED"),
    ("America/Santo_Domingo", "America/New_York", date(2026, 11, 1), "MISMATCH"),
    # US spring-forward day (2027-03-14): Santo_Domingo -4 all day, NY -5 until 07:00Z.
    ("America/Santo_Domingo", "America/New_York", date(2027, 3, 14), "MISMATCH"),
    ("America/Santo_Domingo", "America/New_York", date(2027, 3, 15), "UNDETERMINED"),
    (None, "America/New_York", TARGET, "UNDETERMINED"),
])
def test_tz_classify(name: str | None, clinic: str, target: date, expected: str) -> None:
    assert classify_tz(name, clinic, target) == expected


def test_tz_classify_never_verifies_by_offsets() -> None:
    # Identical offsets all day never upgrade a different name (Codex: no equivalence proof).
    for name in ("US/Eastern", "America/Toronto", "America/Nassau"):
        assert classify_tz(name, "America/New_York", TARGET) != "VERIFIED"


# ── extract (pure): bounded field; malformed body vs missing field ───────────


@pytest.mark.parametrize("body,expected", [
    (dept_body(timezonename="America/Denver"), "America/Denver"),
    (dept_body(timezonename="America/Denver")[0], "America/Denver"),           # bare object
    (dept_body(), None),                                                        # TZ-6 missing
    (dept_body(timezonename=None, timezone=-5, timezoneoffset=-5,
               doesnotobservedst=False), None),                                 # TZ-6 offsets
    (dept_body(timezonename="Eastern Standard Time"), None),                    # TZ-5
    (dept_body(timezonename="x" * 65), None),
    (dept_body(timezonename=7), None),
    (dept_body(timezonename="SENTINEL<script>"), None),
    (dept_body(timezonename="Not/AZone"), None),
    ([{"timezonename": "America/Denver"}], "America/Denver"),                   # no id field
    ([{"departmentid": 21, "timezonename": TZ}], TZ),                           # int id
])
def test_tz_extract(body: Any, expected: str | None) -> None:
    assert extract_tz(body, DEPT) == expected


@pytest.mark.parametrize("body", [[], [1], [{}, {}], "SENTINEL", 5, None,
                                  [{"departmentid": "22", "timezonename": TZ}]])
def test_tz_extract_malformed_rejected(body: Any) -> None:
    with pytest.raises(SourceInvalid) as exc:
        extract_tz(body, DEPT)
    assert exc.value.code == "SOURCE_INVALID"
    no_leak(str(exc.value), repr(exc.value))


# ── fetch_snapshot ───────────────────────────────────────────────────────────


async def test_tz1_verified_snapshot_and_request_order() -> None:
    api = Api()
    s = await snap(api)
    assert s.tz_status == "VERIFIED" and s.dept_tz == TZ
    assert api.paths() == [("POST", "/oauth2/v1/token"), ("GET", DEPT_PATH), ("GET", BOOKED)]
    assert len(s.fixtures.appointments) == 2


async def test_tz2_mismatch_rejects_before_any_booked_get() -> None:
    api = Api(dept_body(timezonename="America/Denver"))
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == "SOURCE_TZ_MISMATCH" and exc.value.detail == "America/Denver"
    assert api.booked() == 0
    assert api.paths() == [("POST", "/oauth2/v1/token"), ("GET", DEPT_PATH)]


@pytest.mark.parametrize("name", ["US/Eastern", "Eastern Standard Time", None])
async def test_tz4_tz5_undetermined_proceeds(name: str | None) -> None:
    api = Api(dept_body(timezonename=name))
    s = await snap(api)
    assert s.tz_status == "UNDETERMINED" and api.booked() == 1
    assert s.dept_tz == ("US/Eastern" if name == "US/Eastern" else None)
    assert s.fixtures.appointments[0].start_utc == datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("api,code", [
    (Api(dept_status=500), "HTTP_500"),
    (Api(dept_status=404), "HTTP_404"),
    (Api("{SENTINEL-not-json"), "BAD_JSON"),
    (Api([dept_body(timezonename=TZ)[0], dept_body(timezonename=TZ)[0]]), "SOURCE_INVALID"),
    (Api([{"departmentid": "22", "timezonename": TZ}]), "SOURCE_INVALID"),
])
async def test_tz8_department_failure_rejects_whole_fetch(api: Api, code: str,
                                                          caplog: Any) -> None:
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == code and api.booked() == 0
    no_leak(str(exc.value), repr(exc.value), caplog.text)


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/195900/departments/22"),
    ("GET", "/v1/195900/departments"),
    ("GET", "/v1/195900/departments/21/providers"),
    ("PUT", DEPT_PATH),
    ("GET", "/v1/195901/departments/21"),
])
async def test_tz9_only_configured_department_allowed(method: str, path: str) -> None:
    api = Api()
    client = AthenaPreviewClient(config(), transport=httpx.MockTransport(api))
    try:
        with pytest.raises(SourceInvalid) as exc:
            await client._request(method, path)          # noqa: SLF001 — guard under test
    finally:
        await client.aclose()
    assert exc.value.code == "DISALLOWED" and api.requests == []


async def test_tz10_request_bound_truncated_path() -> None:
    api = Api(n=900)
    s = await snap(api)
    assert s.truncated is True and len(api.requests) == 7         # token + dept + 5 pages
    assert sum(r.url.path == DEPT_PATH for r in api.requests) == 1


# ── service + page ───────────────────────────────────────────────────────────


async def test_tz1_page_shows_verified(demo_env: dict[str, str]) -> None:
    data, html, own, svc = await _page(demo_env, Api())
    await svc.aclose()
    await own.close()
    assert data["source"]["tz_status"] == "VERIFIED" and data["source"]["dept_tz"] == TZ
    assert ("Sandbox department timezone matches clinic timezone (America/New_York)"
            " — VERIFIED.") in html
    assert "UNVERIFIED" not in html
    no_leak(html, json.dumps(data, default=str))


async def test_tz2_tz3_page_mismatch_banner_sticky_and_no_send(
    demo_env: dict[str, str],
) -> None:
    api = Api(dept_body(timezonename="America/Denver"))
    data, html, own, svc = await _page(demo_env, api)
    try:
        before = len(api.requests)
        again = await svc.page_data()
        preview = await svc.preview_batch()
        batch = await svc.send_batch()
    finally:
        await svc.aclose()
        await own.close()
    assert data["appointments"] == [] and preview == [] and batch == []
    assert data["source"]["error"] == "SOURCE_TZ_MISMATCH"
    assert again["source"]["error"] == "SOURCE_TZ_MISMATCH"
    assert len(api.requests) == before == 2 and api.booked() == 0          # sticky, no booked
    assert "athenahealth sandbox unavailable — SOURCE_TZ_MISMATCH" in html
    assert ("Sandbox department timezone America/Denver differs from clinic timezone"
            " America/New_York.") in html
    assert "Synthetic Patient" not in html and "syn-e" not in html
    no_leak(html)


@pytest.mark.parametrize("name", ["Eastern Standard Time", "SENTINEL<script>", None])
async def test_tz5_page_undetermined_keeps_unverified(demo_env: dict[str, str],
                                                      name: str | None) -> None:
    data, html, own, svc = await _page(demo_env, Api(dept_body(timezonename=name)))
    await svc.aclose()
    await own.close()
    assert data["source"]["tz_status"] == "UNDETERMINED" and data["source"]["dept_tz"] is None
    assert "Times assume the sandbox department is in America/New_York — UNVERIFIED." in html
    assert "VERIFIED." not in html.replace("UNVERIFIED.", "")
    assert "Eastern Standard Time" not in html
    no_leak(html, json.dumps(data, default=str))


async def test_tz12_synthetic_mode_no_department_request(demo_env: dict[str, str]) -> None:
    s = DemoSettings.from_env(demo_env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    try:
        data = await DbStaffService(own, s, now=lambda: NOW).page_data()
    finally:
        await own.close()
    assert data["source"] == {"kind": "synthetic"}
