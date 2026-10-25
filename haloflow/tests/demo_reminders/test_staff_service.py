"""Day-3 staff surface: DB-backed service (list, preview, pause), the owner-lost
503 gate (R-5 / rest of T08), and Origin derived from the configured staff port.

Preview makes no writes (no claim, no reservation). Pause is a fenced toggle
(R-14). After ownership is lost every handler returns 503.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import psycopg
from fastapi.testclient import TestClient
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.web import CSRF_FIELD, NullStaffService, build_callback_app, build_staff_app

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # 09:00 EDT; target date 2026-10-28


def _client(app: object, port: int = 8080) -> TestClient:
    return TestClient(app, base_url=f"http://127.0.0.1:{port}")  # type: ignore[arg-type]


def _token(html: str) -> str:
    m = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', html)
    assert m
    return m.group(1)


# ── U: gate and origin ────────────────────────────────────────────────────────


def test_gate_returns_503_on_staff_and_callback() -> None:
    staff = _client(build_staff_app(NullStaffService(), csrf_secret=b"s" * 32,
                                    unavailable=lambda: True))
    assert staff.get("/demo/staff").status_code == 503
    assert staff.post("/demo/pause", data={}).status_code == 503
    cb = TestClient(build_callback_app(unavailable=lambda: True))
    assert cb.post("/webhooks/sms/inbound").status_code == 503


def test_origin_follows_configured_staff_port() -> None:
    app = build_staff_app(NullStaffService(), csrf_secret=b"s" * 32, staff_port=9090)
    c = _client(app, 9090)
    tok = _token(c.get("/demo/staff").text)
    ok = c.post("/demo/pause", data={CSRF_FIELD: tok}, headers={"Origin": "http://127.0.0.1:9090"},
                follow_redirects=False)
    assert ok.status_code == 303
    bad = c.post("/demo/pause", data={CSRF_FIELD: tok}, headers={"Origin": "http://127.0.0.1:8080"})
    assert bad.status_code == 403


# ── D: DB-backed service ──────────────────────────────────────────────────────


async def _service(env: dict[str, str]):  # type: ignore[no-untyped-def]
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    return own, DbStaffService(own, s, now=lambda: NOW)


async def test_page_lists_synthetic_appointments_in_clinic_time(demo_env: dict[str, str]) -> None:
    own, svc = await _service(demo_env)
    try:
        data = await svc.page_data()
        ids = [a["emr_appt_id"] for a in data["appointments"]]
        assert ids == [f"syn-a-{i}" for i in range(1, 8)]
        first = data["appointments"][0]
        assert first["patient"] == "Synthetic Patient A"
        assert first["when"] == "Wednesday, Oct 28 at 9:00 AM EDT"
        assert data["target_date"] == "2026-10-28"
        assert data["labels"] == {"sms": "SIMULATED", "ehr": "SIMULATED", "callback": "SIMULATED"}
    finally:
        await own.close()


async def test_preview_decisions_and_no_writes(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _service(demo_env)
    try:
        preview = {p["emr_appt_id"]: p["decision"] for p in await svc.preview_batch()}
        assert preview == {
            "syn-a-1": "eligible", "syn-a-2": "eligible", "syn-a-3": "eligible",
            "syn-a-4": "eligible", "syn-a-5": "eligible",
            "syn-a-6": "skip: no_phone", "syn-a-7": "skip: cancelled",
        }
        with psycopg.connect(demo_db_url) as conn:
            assert conn.execute("SELECT count(*) FROM reminder_records").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM send_quota").fetchone() == (0,)
    finally:
        await own.close()


async def test_pause_resume_is_fenced_toggle(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _service(demo_env)
    try:
        await svc.set_paused(True)
        with psycopg.connect(demo_db_url) as conn:
            assert conn.execute("SELECT paused FROM demo_settings").fetchone() == (True,)
        assert (await svc.page_data())["paused"] is True
        await svc.set_paused(False)
        with psycopg.connect(demo_db_url) as conn:
            assert conn.execute("SELECT paused FROM demo_settings").fetchone() == (False,)
    finally:
        await own.close()


def _aclient(app: object) -> httpx.AsyncClient:
    # Same event loop as the test's database engine (the sync TestClient would
    # run the app on another loop and break the asyncpg pool).
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
                             base_url="http://127.0.0.1:8080")


async def test_staff_app_over_db_service_end_to_end(demo_env: dict[str, str]) -> None:
    own, svc = await _service(demo_env)
    try:
        app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
        async with _aclient(app) as c:
            page = await c.get("/demo/staff")
            assert page.status_code == 200
            assert "Synthetic Patient A" in page.text
            r = await c.post("/demo/batch/preview", data={CSRF_FIELD: _token(page.text)})
            assert r.status_code == 200
            assert "syn-a-6 — skip: no_phone" in r.text
    finally:
        await own.close()


async def test_owner_lost_makes_staff_app_503(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _service(demo_env)
    try:
        app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
        async with _aclient(app) as c:
            tok = _token((await c.get("/demo/staff")).text)
            with psycopg.connect(demo_db_url, autocommit=True) as conn:
                conn.execute("UPDATE demo_settings SET owner_epoch = owner_epoch + 1")
            r = await c.post("/demo/pause", data={CSRF_FIELD: tok})
            assert r.status_code == 503  # the fenced write detected the loss
            assert own.owner_lost is True
            assert (await c.get("/demo/staff")).status_code == 503
    finally:
        await own.close()
