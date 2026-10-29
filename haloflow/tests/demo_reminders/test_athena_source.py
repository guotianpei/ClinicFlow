"""N-ATH: athenahealth Preview sandbox READ-ONLY appointment source (package v1r1,
Codex ALIGN 2026-10-28; owner decisions OD-A1..A3).

API under test (to be implemented in haloflow_demo/athena_source.py, settings.py,
service.py, livetest.py):

- ``check_athena_mode(settings, env) -> AthenaConfig | None`` — None in synthetic
  mode; in athena_preview mode validates A-1' and the identity gate, raising
  ``StartupRefused(reason)`` with no request made.
- ``AthenaPreviewClient(config, transport=None)`` — token POST + GET booked only.
- ``await fetch_snapshot(client, target, tz, department_id, epoch) -> Snapshot``
  (fixtures, counts, truncated, taken_at); raises ``SourceInvalid(code)`` for a
  whole-fetch rejection.
- ``DbStaffService(own, settings, now=..., athena=AthenaConfig, athena_transport=...)``
  page_data()["source"] = {"kind", "error", "truncated", "counts", "snapshot_taken"}.

No test touches the network: every transport is httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.athena_source import (
    AthenaConfig,
    AthenaPreviewClient,
    SourceInvalid,
    check_athena_mode,
    fetch_snapshot,
)
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import (
    DemoSettings,
    StartupRefused,
    validate_demo_database_url,
    validate_startup,
)
from haloflow_demo.web import build_staff_app

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # target clinic date 2026-10-28
TARGET = date(2026, 10, 28)
TZ = "America/New_York"
DEPT = "21"
TEST_NUMBER = "+15550100141"
SENDER = "+15550100100"
SECRET = "SENTINEL-SECRET-x9"
TOKEN = "SENTINEL-TOKEN-y8"
ATHENA_ENV = {"DEMO_EHR_SOURCE": "athena_preview", "DEMO_ATHENA_DEPARTMENT_ID": DEPT,
              "ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
              "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID-z7",
              "ATHENA_CLIENT_SECRET": SECRET}
SENTINELS = ("SENTINEL-", "Donna", "Sandboxtest", "555-867")


def rec(appt_id: str, patient: str = "60178", *, d: str = "10/28/2026", t: str = "09:00",
        status: str = "f", dept: str = DEPT, **extra: Any) -> dict[str, Any]:
    r = {"appointmentid": appt_id, "patientid": patient, "date": d, "starttime": t,
         "appointmentstatus": status, "departmentid": dept,
         "patient": {"firstname": "Donna", "lastname": "Sandboxtest",
                     "mobilephone": "555-867-5309"}}
    r.update(extra)
    return r


class Api:
    """Scripted Preview API: booked pages served from ``records`` by offset."""

    def __init__(self, records: list[dict[str, Any]] | None = None,
                 totalcount: Any = None) -> None:
        self.records = [rec("1001"), rec("1002", "60179", t="10:30")] if records is None \
            else records
        self.totalcount = totalcount
        self.requests: list[httpx.Request] = []
        self.override: dict[str, Any] = {}     # path fragment -> Response or callable

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        for key, resp in self.override.items():
            if key in req.url.path:
                return resp(req) if callable(resp) else resp
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        if req.url.path == f"/v1/195900/departments/{DEPT}":      # N-TZ: matching IANA tz
            return httpx.Response(200, json=[{"departmentid": DEPT, "timezonename": TZ}])
        if req.url.path == "/v1/195900/appointments/booked":
            off = int(req.url.params.get("offset", "0"))
            lim = int(req.url.params.get("limit", "100"))
            total = len(self.records) if self.totalcount is None else self.totalcount
            return httpx.Response(200, json={"appointments": self.records[off:off + lim],
                                             "totalcount": total})
        return httpx.Response(404, text="SENTINEL-404")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def gets(self) -> list[httpx.Request]:
        """Booked-page GETs (N-TZ: the department GET is counted by ``dept_gets``)."""
        return [r for r in self.requests
                if r.method == "GET" and r.url.path == "/v1/195900/appointments/booked"]

    def dept_gets(self) -> int:
        return sum(r.url.path == f"/v1/195900/departments/{DEPT}" for r in self.requests)


def config() -> AthenaConfig:
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo",
                               **ATHENA_ENV})
    cfg = check_athena_mode(s, ATHENA_ENV)
    assert cfg is not None
    return cfg


async def snap(api: Api, target: date = TARGET, epoch: int = 1) -> Any:
    client = AthenaPreviewClient(config(), transport=api.transport())
    try:
        return await fetch_snapshot(client, target, TZ, DEPT, epoch)
    finally:
        await client.aclose()


def no_leak(*texts: str) -> None:
    blob = "\n".join(texts)
    for s in SENTINELS:
        assert s not in blob, s


# ── AT-A1 start-up guard and identity gate ───────────────────────────────────


def test_a1_synthetic_mode_returns_none() -> None:
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo"})
    assert check_athena_mode(s, {}) is None


@pytest.mark.parametrize("override,reason", [
    ({"DEMO_SMS_MODE": "real"}, "athena_requires_stub_sms"),
    ({"DEMO_CALLBACK_MODE": "real"}, "athena_requires_simulated_callback"),
    ({"DEMO_LIVE_TEST": "outbound-once"}, "athena_live_test_forbidden"),
    ({"DEMO_ATHENA_DEPARTMENT_ID": ""}, "athena_department"),
    ({"DEMO_ATHENA_DEPARTMENT_ID": "0"}, "athena_department"),
    ({"DEMO_ATHENA_DEPARTMENT_ID": "21a"}, "athena_department"),
    ({"ATHENA_BASE_URL": "http://api.preview.platform.athenahealth.com"}, "athena_gate"),
    ({"ATHENA_BASE_URL": "https://api.platform.athenahealth.com"}, "athena_gate"),
    ({"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com:8443"}, "athena_gate"),
    ({"ATHENA_BASE_URL": "https://u:p@api.preview.platform.athenahealth.com"}, "athena_gate"),
    ({"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com/v1"}, "athena_gate"),
    ({"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com#f"}, "athena_gate"),
    ({"ATHENA_PRACTICE_ID": "195901"}, "athena_gate"),
    ({"ATHENA_CLIENT_SECRET": ""}, "athena_gate"),
])
def test_a1_guard_refuses(override: dict[str, str], reason: str) -> None:
    env = {"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo", **ATHENA_ENV, **override}
    s = DemoSettings.from_env(env)
    with pytest.raises(StartupRefused) as exc:
        check_athena_mode(s, env)
    assert exc.value.reason == reason
    no_leak(str(exc.value), repr(exc.value))


def test_a1_unknown_source_refused_at_startup() -> None:
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo",
                               "DEMO_EHR_SOURCE": "athena_prod"})
    with pytest.raises(StartupRefused) as exc:
        validate_startup(s)
    assert exc.value.reason == "ehr_source"


def test_a1_config_repr_hides_secrets() -> None:
    no_leak(repr(config()), str(config()))


async def test_a1_live_sms_test_refuses_in_athena_mode(demo_env: dict[str, str]) -> None:
    from haloflow_demo.livetest import EXIT_REFUSED, live_sms_test
    seen: list[httpx.Request] = []
    env = {**demo_env, **ATHENA_ENV, "DEMO_LIVE_TEST": "outbound-once",
           "NOTIFYRE_API_KEY": "tok-SENTINEL-abc", "DEMO_SMS_SENDER": SENDER,
           "DEMO_SMS_TEST_NUMBER": TEST_NUMBER}
    out = io.StringIO()
    with redirect_stdout(out):
        rc = await live_sms_test(env, transport=httpx.MockTransport(
            lambda r: seen.append(r) or httpx.Response(500)), now=lambda: NOW)
    assert rc == EXIT_REFUSED and seen == []
    assert "live_test_athena_mode" in out.getvalue()


# ── AT-A2 allowlist / method / redirect ──────────────────────────────────────


async def test_a2_only_token_post_and_booked_get() -> None:
    api = Api()
    await snap(api)
    assert [(r.method, r.url.path) for r in api.requests] == [
        ("POST", "/oauth2/v1/token"), ("GET", f"/v1/195900/departments/{DEPT}"),
        ("GET", "/v1/195900/appointments/booked")]
    assert api.requests[0].url.host == "api.preview.platform.athenahealth.com"


@pytest.mark.parametrize("method,path", [("PUT", "/v1/195900/appointments/1/confirmationstatus"),
                                         ("GET", "/v1/195900/patients"),
                                         ("POST", "/v1/195900/appointments/booked"),
                                         ("DELETE", "/v1/195900/appointments/1")])
async def test_a2_disallowed_requests_never_sent(method: str, path: str) -> None:
    api = Api()
    client = AthenaPreviewClient(config(), transport=api.transport())
    with pytest.raises(SourceInvalid):
        await client._request(method, path)          # noqa: SLF001 — guard under test
    await client.aclose()
    assert api.requests == []


async def test_a2_redirect_not_followed() -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(
        302, headers={"Location": "https://evil.example/SENTINEL"})
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == "REDIRECT" and len(api.requests) == 3


# ── AT-A3 paging and fail-closed validation ──────────────────────────────────


def _many(n: int) -> list[dict[str, Any]]:
    return [rec(str(2000 + i), str(70000 + i), t=f"{8 + (i % 9):02d}:{(i * 5) % 60:02d}")
            for i in range(n)]


async def test_a3_paging_three_pages() -> None:
    api = Api(_many(230))
    s = await snap(api)
    offs = [r.url.params["offset"] for r in api.gets()]
    assert offs == ["0", "100", "200"]
    q = dict(api.gets()[0].url.params)
    assert q == {"startdate": "10/28/2026", "enddate": "10/28/2026", "departmentid": DEPT,
                 "limit": "100", "offset": "0", "showpatientdetail": "false",
                 "showinsurance": "false", "showcopay": "false", "showclaimdetail": "false"}
    assert len(s.fixtures.appointments) == 230 and s.truncated is False


async def test_a3_cap_five_pages_truncated() -> None:
    api = Api(_many(900))
    s = await snap(api)
    assert len(api.gets()) == 5 and s.truncated is True
    assert len(s.fixtures.appointments) == 500


@pytest.mark.parametrize("bad", [None, "x", -1, True, 1.5])
async def test_a3_bad_totalcount_rejected(bad: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(
        200, json={"appointments": [rec("1001")], "totalcount": bad} if bad is not None
        else {"appointments": [rec("1001")]})
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == "SOURCE_INVALID"


async def test_a3_later_page_failure_rejects_whole_fetch() -> None:
    api = Api(_many(230))

    def page(req: httpx.Request) -> httpx.Response:
        off = int(req.url.params["offset"])
        if off == 100:
            return httpx.Response(500, text="SENTINEL-body")
        return httpx.Response(200, json={"appointments": api.records[off:off + 100],
                                         "totalcount": 230})
    api.override["/appointments/booked"] = page
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == "HTTP_500"
    assert [r.url.params["offset"] for r in api.gets()] == ["0", "100"]


async def test_a3_changing_totalcount_rejected() -> None:
    api = Api(_many(230))

    def page(req: httpx.Request) -> httpx.Response:
        off = int(req.url.params["offset"])
        return httpx.Response(200, json={"appointments": api.records[off:off + 100],
                                         "totalcount": 230 if off == 0 else 240})
    api.override["/appointments/booked"] = page
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == "SOURCE_INVALID"


@pytest.mark.parametrize("second_page_size", [0, 50])
async def test_a3_premature_short_page_rejected(second_page_size: int) -> None:
    api = Api(_many(230))

    def page(req: httpx.Request) -> httpx.Response:
        off = int(req.url.params["offset"])
        size = 100 if off == 0 else second_page_size
        return httpx.Response(200, json={"appointments": api.records[off:off + size],
                                         "totalcount": 230})
    api.override["/appointments/booked"] = page
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == "SOURCE_INVALID"


async def test_a3_duplicate_appointment_id_rejected() -> None:
    with pytest.raises(SourceInvalid) as exc:
        await snap(Api([rec("1001"), rec("1001", "60179", t="11:00")]))
    assert exc.value.code == "SOURCE_INVALID"


@pytest.mark.parametrize("body", [[], {"appointments": "x", "totalcount": 1},
                                  {"appointments": ["bad"], "totalcount": 1}])
async def test_a3_malformed_shape_rejected(body: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(200, json=body)
    with pytest.raises(SourceInvalid):
        await snap(api)


# ── AT-A4 mapping, exclusions, time ───────────────────────────────────────────


async def test_a4_mapping_and_counted_exclusions() -> None:
    s = await snap(Api([
        rec("1001"),                                   # f -> scheduled
        rec("1002", "60179", status="x"),              # cancelled (kept, skip later)
        rec("1003", "60180", status="2"),              # checked in
        rec("1004", "60181", status="3"),              # completed
        rec("1005", "60182", status="4"),              # completed
        rec("1006", "60183", status="o"),              # open slot -> excluded
        rec("1007", "60184", status="Q"),              # undocumented -> excluded
        rec("1008", d="10/29/2026"),                   # wrong date
        rec("1009", dept="99"),                        # wrong department
        rec("abc"),                                    # invalid appointment id
        rec("1010", patient="p-1"),                    # invalid patient id
        rec("1011", t="9am"),                          # malformed time
        rec("1012", d=""),                             # malformed date
        {k: v for k, v in rec("1013").items() if k != "departmentid"},  # missing dept
    ]))
    by_id = {a.emr_appt_id: a for a in s.fixtures.appointments}
    assert sorted(by_id) == [f"ath-e1-100{i}" for i in range(1, 6)]
    assert [by_id[f"ath-e1-100{i}"].status.value for i in range(1, 6)] == [
        "scheduled", "cancelled", "checked_in", "completed", "completed"]
    assert by_id["ath-e1-1001"].emr_patient_id == "athp-60178"
    assert by_id["ath-e1-1001"].start_utc == datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
    assert s.counts == {"OPEN_SLOT": 1, "UNDOCUMENTED_STATUS": 1, "WRONG_DATE": 1,
                        "WRONG_DEPARTMENT": 1, "INVALID_ID": 2, "MALFORMED_RECORD": 3}


async def test_a4_dst_fall_ambiguous_excluded() -> None:
    s = await snap(Api([rec("1001", d="11/01/2026", t="01:30"),
                        rec("1002", "60179", d="11/01/2026", t="09:00")]),
                   target=date(2026, 11, 1))
    assert [a.emr_appt_id for a in s.fixtures.appointments] == ["ath-e1-1002"]
    assert s.fixtures.appointments[0].start_utc == datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
    assert s.counts == {"AMBIGUOUS_TIME": 1}


async def test_a4_dst_spring_gap_excluded() -> None:
    s = await snap(Api([rec("1001", d="03/14/2027", t="02:30"),
                        rec("1002", "60179", d="03/14/2027", t="03:30")]),
                   target=date(2027, 3, 14))
    assert [a.emr_appt_id for a in s.fixtures.appointments] == ["ath-e1-1002"]
    assert s.fixtures.appointments[0].start_utc == datetime(2027, 3, 14, 7, 30, tzinfo=UTC)
    assert s.counts == {"NONEXISTENT_TIME": 1}


# ── AT-A5 masked patients ────────────────────────────────────────────────────


async def test_a5_masked_display_and_identity() -> None:
    s = await snap(Api([rec("1001", "60178"), rec("1002", "90178", t="10:00"),
                        rec("1003", "60178", t="11:00")]))
    p = s.fixtures.patients
    assert set(p) == {"athp-60178", "athp-90178"}
    assert p["athp-60178"].display_name == p["athp-90178"].display_name == \
        "Sandbox patient …0178"
    assert p["athp-60178"].phone_alias == p["athp-90178"].phone_alias == "sandbox"
    no_leak(repr(s))


# ── AT-A7 failure classes ────────────────────────────────────────────────────


@pytest.mark.parametrize("path,resp,code", [
    ("/oauth2/v1/token", httpx.Response(401, text="SENTINEL-denied"), "HTTP_401"),
    ("/appointments/booked", httpx.Response(500, text="SENTINEL-boom"), "HTTP_500"),
    ("/appointments/booked", httpx.Response(200, text="{SENTINEL-not-json"), "BAD_JSON"),
])
async def test_a7_failure_codes_without_leak(path: str, resp: httpx.Response,
                                             code: str, caplog: Any) -> None:
    api = Api()
    api.override[path] = resp
    with pytest.raises(SourceInvalid) as exc:
        await snap(api)
    assert exc.value.code == code
    no_leak(str(exc.value), repr(exc.value), caplog.text)


async def test_a7_network_error_code(caplog: Any) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"SENTINEL {req.url}", request=req)
    client = AthenaPreviewClient(config(), transport=httpx.MockTransport(boom))
    with pytest.raises(SourceInvalid) as exc:
        await fetch_snapshot(client, TARGET, TZ, DEPT, 1)
    await client.aclose()
    assert exc.value.code == "NETWORK"
    no_leak(str(exc.value), caplog.text)


# ── D layer: service integration (AT-A6, A7, A8, A9, A10, batch) ─────────────


def _env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env, **ATHENA_ENV, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER,
            "DEMO_SMS_SENDER": SENDER, "DEMO_CALLBACK_SECRET": "test-callback-secret-0123456789"}


async def _start(env: dict[str, str], api: Api,
                 now: Any = None) -> tuple[Any, DbStaffService]:
    s = DemoSettings.from_env(env)
    cfg = check_athena_mode(s, env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    return own, DbStaffService(own, s, now=now or (lambda: NOW), athena=cfg,
                               athena_transport=api.transport())


def _dated(api: Api) -> None:
    """Serve the default two records on whatever date was requested."""
    def page(req: httpx.Request) -> httpx.Response:
        d = req.url.params["startdate"]
        recs = [rec("1001", d=d), rec("1002", "60179", d=d, t="10:30")] \
            if d == "10/28/2026" else [rec("3001", "60180", d=d)]
        return httpx.Response(200, json={"appointments": recs, "totalcount": len(recs)})
    api.override["/appointments/booked"] = page


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


async def _html(own: Any, svc: DbStaffService) -> str:
    app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8080") as c:
        return (await c.get("/demo/staff")).text


async def test_a9_page_lists_masked_sandbox_appointments(demo_env: dict[str, str]) -> None:
    api = Api()
    own, svc = await _start(_env(demo_env), api)
    try:
        data = await svc.page_data()
        html = await _html(own, svc)
    finally:
        await own.close()
    assert [a["emr_appt_id"] for a in data["appointments"]] == ["ath-e1-1001", "ath-e1-1002"]
    assert data["appointments"][0]["patient"] == "Sandbox patient …0178"
    assert data["labels"]["ehr"] == "SANDBOX READ / SIMULATED WRITE"
    assert data["labels"]["sms"] == "SIMULATED"
    src = data["source"]
    assert src["kind"] == "athena_preview" and src["error"] is None and src["truncated"] is False
    assert ("Sandbox department timezone matches clinic timezone (America/New_York)"
            " — VERIFIED.") in html and "Sandbox snapshot taken" in html
    no_leak(json.dumps(data, default=str), html)


async def test_a7_failure_shows_line_and_no_synthetic_fallback(demo_env: dict[str, str]) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(500, text="SENTINEL-body")
    own, svc = await _start(_env(demo_env), api)
    try:
        data = await svc.page_data()
        html = await _html(own, svc)
        preview = await svc.preview_batch()
    finally:
        await own.close()
    assert data["appointments"] == [] and preview == []
    assert data["source"]["error"] == "HTTP_500"
    assert "athenahealth sandbox unavailable — HTTP_500" in html
    assert "Synthetic Patient" not in html and "syn-e" not in html
    no_leak(html)


async def test_a8_one_snapshot_per_date_single_flight(demo_env: dict[str, str]) -> None:
    api = Api()
    own, svc = await _start(_env(demo_env), api)
    try:
        await asyncio.gather(svc.page_data(), svc.page_data(), svc.preview_batch())
        await svc.page_data()
    finally:
        await own.close()
    assert len(api.gets()) == 1 and api.dept_gets() == 1
    assert [r.method for r in api.requests].count("POST") == 1


async def test_a6_writeback_never_calls_athena(demo_env: dict[str, str],
                                               demo_db_url: str) -> None:
    api = Api()
    own, svc = await _start(_env(demo_env), api)
    try:
        sent = await svc.send_to_appointment("ath-e1-1001")
        assert sent["state"] == "SUBMITTED", sent
        await svc.simulate_reply(int(sent["reminder_id"]), "1")
        await svc.process_now()
        tid = int(_q(demo_db_url, "SELECT id FROM writeback_tasks")[0][0])
        assert await svc.run_writeback(tid) == "DONE"
    finally:
        await own.close()
    assert all(r.method in ("GET", "POST") for r in api.requests)
    assert not any("confirmationstatus" in r.url.path for r in api.requests)


async def test_a9_restart_prior_tasks_recover_durably_and_no_resend(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    api = Api()
    own, svc = await _start(env, api)
    try:
        s1 = await svc.send_to_appointment("ath-e1-1001")
        await svc.simulate_reply(int(s1["reminder_id"]), "1")
        await svc.process_now()
        s2 = await svc.send_to_appointment("ath-e1-1002")
        await svc.simulate_reply(int(s2["reminder_id"]), "1")
        await svc.process_now()
    finally:
        await own.close()
    tids = [int(r[0]) for r in _q(demo_db_url, "SELECT id FROM writeback_tasks ORDER BY id")]
    _q(demo_db_url, "UPDATE writeback_tasks SET state = 'UNCERTAIN' WHERE id = %s", (tids[1],))
    own, svc = await _start(env, Api())                        # restart: epoch 2
    try:
        await svc.page_data()                                  # epoch-2 snapshot registered first
        assert svc.ehr.knows("ath-e2-1001") and not svc.ehr.knows("ath-e1-1001")
        # Inspection reads go to this process's loaded instance: since M-2S the router
        # resolves every sandbox read through a reminder row and the local gate, and
        # these epoch-2 ids have no reminder rows (Codex F-MSI-1 A).
        def inst(i: int) -> Any:
            return svc.ehr._find(f"ath-e2-100{i}")                    # noqa: SLF001
        before = [await inst(i).read_confirmation(f"ath-e2-100{i}") for i in (1, 2)]
        # M-2S (package v1r2): prior-epoch tasks bind their durable sandbox slot by
        # provider appointment id; this was "scenario_unavailable" (LIMITED) before.
        assert await svc.run_writeback(tids[0]) == "DONE"
        assert await svc.reconcile_writeback(tids[1]) == "UNCERTAIN"   # nothing written
        after = [await inst(i).read_confirmation(f"ath-e2-100{i}") for i in (1, 2)]
        assert (after[0].value, after[0].version) == ("2", before[0].version + 1)
        assert after[1] == before[1]                           # no write for task 2
        page = await svc.page_data()
        again = await svc.send_to_appointment("ath-e2-1001")
    finally:
        await own.close()
    assert again["state"] == "SKIPPED"                          # slot dedupe
    status = {a["emr_appt_id"]: a["status"] for a in page["appointments"]}
    assert status["ath-e2-1001"].endswith("reminder: Confirmed by patient (before restart)")
    assert _q(demo_db_url, "SELECT count(*) FROM reminder_records") == [(2,)]
    kinds = {r[0] for r in _q(demo_db_url, "SELECT kind FROM follow_up_items")}
    assert "writeback_scenario_unavailable" not in kinds


async def test_a10_shared_number_uncorrelated_reply_staff_only(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    api = Api([rec("1001", "60178"), rec("1002", "90178", t="10:00")])   # same last-4
    own, svc = await _start(_env(demo_env), api)
    try:
        await svc.send_to_appointment("ath-e1-1001")
        await svc.send_to_appointment("ath-e1-1002")
        ts = int(NOW.timestamp())
        raw = json.dumps({"Event": "sms_received", "Timestamp": ts, "Payload": {
            "ReplyID": "r-uncorrelated", "RecipientID": "r", "RecipientNumber": SENDER,
            "SenderNumber": TEST_NUMBER, "Message": "1", "ReceivedDateUtc": ts}}).encode()
        import hashlib
        import hmac
        sig = hmac.new(b"test-callback-secret-0123456789", f"{ts}.".encode() + raw,
                       hashlib.sha256).hexdigest()
        res = await svc.receive_http(raw, [f"t={ts},v={sig}"])
        assert res.status == 204
        await svc.process_now()
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT count(*) FROM writeback_tasks") == [(0,)]
    assert _q(demo_db_url, "SELECT count(*) FROM follow_up_items") == [(1,)]


async def test_a3_truncated_disables_batch_but_not_single(demo_env: dict[str, str],
                                                          demo_db_url: str) -> None:
    api = Api(_many(900))
    own, svc = await _start(_env(demo_env), api)
    try:
        data = await svc.page_data()
        html = await _html(own, svc)
        batch = await svc.send_batch()
        assert _q(demo_db_url, "SELECT count(*) FROM reminder_records") == [(0,)]
        one = await svc.send_to_appointment(data["appointments"][0]["emr_appt_id"])
    finally:
        await own.close()
    assert data["source"]["truncated"] is True and batch == []
    assert "TRUNCATED" in html and re.search(r"Send batch[^<]*disabled", html)
    assert one["state"] == "SUBMITTED"


async def test_synthetic_mode_unchanged(demo_env: dict[str, str]) -> None:
    s = DemoSettings.from_env(demo_env)
    assert check_athena_mode(s, demo_env) is None
    own = await acquire_ownership(s, validate_demo_database_url(s))
    try:
        data = await DbStaffService(own, s, now=lambda: NOW).page_data()
    finally:
        await own.close()
    assert data["appointments"][0]["emr_appt_id"] == "syn-e1-20261028-a-1"
    assert data["labels"]["ehr"] == "SIMULATED"
    assert data.get("source", {}).get("kind", "synthetic") == "synthetic"


async def test_a8_second_date_keeps_first_date_state(demo_env: dict[str, str],
                                                     demo_db_url: str) -> None:
    clock = {"now": NOW}
    api = Api()
    _dated(api)
    own, svc = await _start(_env(demo_env), api, now=lambda: clock["now"])
    try:
        sent = await svc.send_to_appointment("ath-e1-1001")
        await svc.simulate_reply(int(sent["reminder_id"]), "1")
        await svc.process_now()
        tid = int(_q(demo_db_url, "SELECT id FROM writeback_tasks")[0][0])
        assert await svc.run_writeback(tid) == "DONE"
        written = await svc.ehr.read_confirmation("ath-e1-1001")
        clock["now"] = NOW + (datetime(2026, 10, 27, tzinfo=UTC) - datetime(2026, 10, 26,
                                                                            tzinfo=UTC))
        second = await svc.page_data()                         # target 2026-10-29
        assert [a["emr_appt_id"] for a in second["appointments"]] == ["ath-e1-3001"]
        clock["now"] = NOW
        first = await svc.page_data()                          # back to 2026-10-28
        assert [a["emr_appt_id"] for a in first["appointments"]] == ["ath-e1-1001",
                                                                     "ath-e1-1002"]
        assert await svc.ehr.read_confirmation("ath-e1-1001") == written
        assert svc.ehr.knows("ath-e1-1001") and svc.ehr.knows("ath-e1-3001")
    finally:
        await own.close()
    starts = [r.url.params["startdate"] for r in api.gets()]
    assert starts == ["10/28/2026", "10/29/2026"]                # no refetch of the first date
