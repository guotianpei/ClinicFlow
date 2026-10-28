"""N-ATH corrections from Codex review of 3daeff2 (codex_n-ath-3daeff2-review.md).

F-ATH-I1: the real start-up path (``haloflow_demo.app.run``) must validate the
sandbox source before any DB / listener / provider effect, pass the validated
config to the service (never silently synthetic), and close the client on
shutdown. Settings and config must agree.
F-ATH-I2: a booked page with more than PAGE_LIMIT records is rejected.

The listeners are replaced by an in-process stand-in (``FakeServer``) so the
real ``run`` executes without opening ports; no test touches the network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo import app as app_mod
from haloflow_demo.athena_source import (
    PAGE_LIMIT,
    AthenaPreviewClient,
    SourceInvalid,
    check_athena_mode,
    fetch_snapshot,
)
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url

ATHENA_ENV = {"DEMO_EHR_SOURCE": "athena_preview", "DEMO_ATHENA_DEPARTMENT_ID": "21",
              "ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
              "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID",
              "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET"}


def _tables(url: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute("SELECT count(*) FROM information_schema.tables"
                           " WHERE table_schema = 'public'").fetchone()
    assert row is not None
    return int(row[0])


class FakeServer:
    """Stand-in for uvicorn.Server: reports started, then returns (run proceeds
    to closing and drain exactly as when a listener ends)."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.started = False
        self.should_exit = False

    async def serve(self) -> None:
        self.started = True


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def h(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return httpx.Response(500)
        return httpx.MockTransport(h)


# ── F-ATH-I1: real start-up path ──────────────────────────────────────────────


@pytest.mark.parametrize("override,reason", [
    ({"ATHENA_PRACTICE_ID": "195901"}, "athena_gate"),
    ({"DEMO_SMS_MODE": "real"}, "sms_real_unverified"),      # existing guard refuses first
    ({"DEMO_ATHENA_DEPARTMENT_ID": "x"}, "athena_department"),
    ({"DEMO_LIVE_TEST": "outbound-once"}, "athena_live_test_forbidden"),
])
async def test_i1_run_refuses_before_any_db_effect(
    demo_env: dict[str, str], demo_db_url: str, override: dict[str, str], reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_mod.uvicorn, "Server", FakeServer)
    rec = Recorder()
    env = {**demo_env, **ATHENA_ENV, **override}
    events: list[str] = []
    code = await app_mod.run(DemoSettings.from_env(env), env=env,
                             athena_transport=rec.transport(), on_event=events.append)
    assert code == app_mod.EXIT_REFUSED
    assert events == [f"refused:{reason}"]
    assert _tables(demo_db_url) == 0            # no ownership, no tables
    assert rec.requests == []


async def test_i1_run_wires_athena_config_and_closes_client(
    demo_env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_mod.uvicorn, "Server", FakeServer)
    built: list[DbStaffService] = []
    closed: list[bool] = []
    real_build = app_mod.build_service

    def spy_build(*a: Any, **kw: Any) -> DbStaffService:
        svc = real_build(*a, **kw)
        built.append(svc)
        orig = svc.aclose

        async def aclose() -> None:
            closed.append(True)
            await orig()
        svc.aclose = aclose  # type: ignore[method-assign]
        return svc
    monkeypatch.setattr(app_mod, "build_service", spy_build)
    rec = Recorder()
    env = {**demo_env, **ATHENA_ENV}
    events: list[str] = []
    code = await app_mod.run(DemoSettings.from_env(env), env=env,
                             athena_transport=rec.transport(), on_event=events.append)
    assert code == app_mod.EXIT_OK, events
    assert "listening" in events and "drained" in events
    assert len(built) == 1 and built[0]._athena is not None              # noqa: SLF001
    assert built[0]._athena.department_id == "21"                          # noqa: SLF001
    assert closed == [True]
    assert rec.requests == []                    # no provider call at start-up (lazy fetch)


async def test_i1_run_synthetic_mode_has_no_athena(
    demo_env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_mod.uvicorn, "Server", FakeServer)
    built: list[DbStaffService] = []
    real_build = app_mod.build_service
    monkeypatch.setattr(app_mod, "build_service",
                        lambda *a, **kw: built.append(real_build(*a, **kw)) or built[-1])
    code = await app_mod.run(DemoSettings.from_env(demo_env), env=demo_env)
    assert code == app_mod.EXIT_OK
    assert built[0]._athena is None                                        # noqa: SLF001


def test_i1_entry_point_passes_environment() -> None:
    import inspect

    from haloflow_demo import __main__ as entry
    src = inspect.getsource(entry._main)                                    # noqa: SLF001
    assert "env=env" in src and "DemoSettings.from_env(env)" in src


async def test_i1_inconsistent_config_refused(demo_env: dict[str, str]) -> None:
    athena_settings = DemoSettings.from_env({**demo_env, **ATHENA_ENV})
    synthetic_settings = DemoSettings.from_env(demo_env)
    cfg = check_athena_mode(athena_settings, {**demo_env, **ATHENA_ENV})
    own = await acquire_ownership(synthetic_settings,
                                  validate_demo_database_url(synthetic_settings))
    try:
        with pytest.raises(StartupRefused) as exc:
            DbStaffService(own, athena_settings)                   # athena settings, no config
        assert exc.value.reason == "athena_config_inconsistent"
        with pytest.raises(StartupRefused) as exc:
            DbStaffService(own, synthetic_settings, athena=cfg)    # config, synthetic settings
        assert exc.value.reason == "athena_config_inconsistent"
    finally:
        await own.close()


# ── F-ATH-I2: oversized page ──────────────────────────────────────────────────


def _rec(i: int) -> dict[str, Any]:
    return {"appointmentid": str(5000 + i), "patientid": str(80000 + i), "date": "10/28/2026",
            "starttime": f"{8 + i % 9:02d}:{(i * 5) % 60:02d}", "appointmentstatus": "f",
            "departmentid": "21"}


@pytest.mark.parametrize("n", [PAGE_LIMIT + 1, 501])
async def test_i2_oversized_page_rejected(n: int) -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"appointments": [_rec(i) for i in range(n)],
                                         "totalcount": n})
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo",
                               **ATHENA_ENV})
    cfg = check_athena_mode(s, ATHENA_ENV)
    assert cfg is not None
    client = AthenaPreviewClient(cfg, transport=httpx.MockTransport(h))
    try:
        with pytest.raises(SourceInvalid) as exc:
            await fetch_snapshot(client, date(2026, 10, 28), "America/New_York", "21", 1)
    finally:
        await client.aclose()
    assert exc.value.code == "SOURCE_INVALID"
    assert [r.method for r in seen].count("GET") == 1


async def test_i2_exact_page_limit_still_accepted() -> None:
    n = PAGE_LIMIT

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"appointments": [_rec(i) for i in range(n)],
                                         "totalcount": n})
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo",
                               **ATHENA_ENV})
    cfg = check_athena_mode(s, ATHENA_ENV)
    assert cfg is not None
    client = AthenaPreviewClient(cfg, transport=httpx.MockTransport(h))
    try:
        snap = await fetch_snapshot(client, date(2026, 10, 28), "America/New_York", "21", 1)
    finally:
        await client.aclose()
    assert len(snap.fixtures.appointments) == n and snap.truncated is False
    assert datetime(2026, 10, 28, tzinfo=UTC) <= snap.fixtures.appointments[0].start_utc
