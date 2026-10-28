"""Offline checks for athena_sandbox_smoke.py — mocked athenahealth (no network),
real local PostgreSQL for the disposable demo DB, real accepted demo code.

Run from haloflow/ with the demo package importable, e.g.
  PYTHONPATH=demo SMOKE_ADMIN_URL=postgresql://… python -m pytest -q <this file>
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import athena_sandbox_smoke as sm

ENV = {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
       "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID-a1",
       "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET-b2", "CLINIC_TZ": "America/New_York",
       "DATABASE_URL": "postgresql+asyncpg://legacy@127.0.0.1:5433/haloflow_dev"}


@pytest.fixture
def admin_url() -> str:
    url = os.environ.get("SMOKE_ADMIN_URL", "")
    if not url:
        pytest.skip("SMOKE_ADMIN_URL not set")
    return url


@pytest.fixture
def db(admin_url: str) -> Iterator[str]:
    name = f"haloflow_smoke_t{secrets.token_hex(4)}_demo"
    yield name
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class Fake:
    def __init__(self, n: int = 2, booked_status: int = 200, other_date: bool = False,
                 tz: str | None = "America/Denver") -> None:
        self.n, self.booked_status, self.other_date = n, booked_status, other_date
        self.tz = tz
        self.seen: list[tuple[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append((req.method, req.url.path))
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "SENTINEL-TOKEN-c3",
                                             "expires_in": 3600})
        if req.url.path == "/v1/195900/departments":
            return httpx.Response(200, json={"departments": [{"departmentid": "x"},
                                                             {"departmentid": "21",
                                                              "name": "SENTINEL-dept"}]})
        if req.url.path == "/v1/195900/departments/21":
            body: dict[str, Any] = {"departmentid": "21", "name": "SENTINEL-dept"}
            if self.tz is not None:
                body["timezonename"] = self.tz
            return httpx.Response(200, json=[body])
        if req.url.path == "/v1/195900/appointments/booked":
            if self.booked_status != 200:
                return httpx.Response(self.booked_status, text="SENTINEL-body")
            assert req.url.params["departmentid"] == "21"
            d = req.url.params["startdate"]
            if self.other_date:
                d = "01/01/2030"
            off, lim = int(req.url.params["offset"]), int(req.url.params["limit"])
            recs = [{"appointmentid": str(7000 + i), "patientid": str(60000 + i), "date": d,
                     "starttime": f"{8 + i % 9:02d}:{(i * 5) % 60:02d}",
                     "appointmentstatus": "f", "departmentid": "21",
                     "patient": {"firstname": "SENTINEL-Donna"}} for i in range(self.n)]
            return httpx.Response(200, json={"appointments": recs[off:off + lim],
                                             "totalcount": self.n})
        return httpx.Response(404)

    def factory(self) -> Any:
        return lambda: httpx.MockTransport(self)


async def run(fake: Fake, admin_url: str, db: str, out: Path) -> tuple[int, dict[str, Any]]:
    return await sm.smoke(ENV, admin_url, db, out, transport_factory=fake.factory())


def no_leak(*blobs: str) -> None:
    for b in blobs:
        assert "SENTINEL" not in b and "+15550100141" not in b and "smoke-" not in b


async def test_happy_path_bounded_and_vetted(admin_url: str, db: str, tmp_path: Path) -> None:
    fake = Fake(n=2)
    rc, s = await run(fake, admin_url, db, tmp_path)
    assert rc == 0, s
    assert s["result"] == "COMPLETED"
    assert "send_reply_writeback_simulated_ok" in s["steps"]
    assert s["page_loads"] == 3 and s["appointments_listed"] == 2
    assert fake.seen == [("POST", "/oauth2/v1/token"), ("GET", "/v1/195900/departments"),
                         ("POST", "/oauth2/v1/token"), ("GET", "/v1/195900/departments/21"),
                         ("GET", "/v1/195900/appointments/booked")]
    assert s["external_request_count"] == 5 <= s["external_request_bound"] == 9
    assert s["tz_status"] == "CONVERTED" and s["effective_zone"] == "America/Denver"
    assert s["provenance_basis"] == "DEPARTMENT"
    for step in ("tz_line_converted_ok", "when_column_zone_ok",
                 "stub_body_qb4_no_zone_label_ok",
                 "provenance_record_ok (value kept in memory only)"):
        assert step in s["steps"], step
    html = (tmp_path / "staff-page-vetted.html").read_text()
    assert "Sandbox patient …0000" in html and "SANDBOX READ / SIMULATED WRITE" in html
    assert " MDT</td>" in html or " MST</td>" in html
    no_leak(html, json.dumps(s))
    assert '"21"' not in json.dumps(s) and "dept=21" not in json.dumps(s)   # id memory-only


@pytest.mark.parametrize("tz,status,basis", [("America/New_York", "VERIFIED", "DEPARTMENT"),
                                             (None, "UNVERIFIED", "FALLBACK")])
async def test_other_tz_outcomes_recorded(admin_url: str, db: str, tmp_path: Path, tz: str | None,
                                          status: str, basis: str) -> None:
    rc, s = await run(Fake(n=2, tz=tz), admin_url, db, tmp_path)
    assert rc == 0 and s["result"] == "COMPLETED", s
    assert s["tz_status"] == status and s["effective_zone"] == "America/New_York"
    assert s["provenance_basis"] == basis
    html = (tmp_path / "staff-page-vetted.html").read_text()
    assert " EDT</td>" in html
    no_leak(html, json.dumps(s))


async def test_truncated_stays_within_nine_requests(admin_url: str, db: str,
                                                    tmp_path: Path) -> None:
    fake = Fake(n=900)
    rc, s = await run(fake, admin_url, db, tmp_path)
    assert rc == 0, s
    assert s["external_request_count"] == 9    # 2 setup + token + department + 5 pages
    assert [c["category"] for c in s["external_requests"]].count("booked") == 5


async def test_no_eligible_appointment_is_valid_stop(admin_url: str, db: str,
                                                     tmp_path: Path) -> None:
    rc, s = await run(Fake(n=2, other_date=True), admin_url, db, tmp_path)
    assert rc == 0 and "no_eligible_appointment_on_target_date" in s["steps"]
    assert s["page_loads"] == 2


async def test_source_failure_stops_without_leak(admin_url: str, db: str,
                                                 tmp_path: Path) -> None:
    rc, s = await run(Fake(booked_status=500), admin_url, db, tmp_path)
    assert rc == 1 and s["result"] == "SOURCE_HTTP_500"
    assert not (tmp_path / "staff-page-vetted.html").exists()
    no_leak(json.dumps(s))


async def test_existing_db_refused_before_any_request(admin_url: str, db: str,
                                                      tmp_path: Path) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')
    fake = Fake()
    rc, s = await run(fake, admin_url, db, tmp_path)
    assert rc == 1 and s["result"] == "DB_EXISTS" and fake.seen == []


async def test_gate_refused_before_any_effect(admin_url: str, db: str, tmp_path: Path) -> None:
    fake = Fake()
    rc, s = await sm.smoke({**ENV, "ATHENA_PRACTICE_ID": "195901"}, admin_url, db, tmp_path,
                           transport_factory=fake.factory())
    assert rc == 2 and s["result"] == "REFUSED:athena_gate" and fake.seen == []
    with psycopg.connect(admin_url, autocommit=True) as conn:
        assert conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,)).fetchone() \
            is None


def test_bad_db_name_refused() -> None:
    with pytest.raises(sm.Stop) as exc:
        sm._db_must_not_exist("postgresql://u@localhost/postgres", "haloflow_thu1029_demo")
    assert exc.value.code == "DB_NAME_REFUSED"


async def test_counting_transport_refuses_other_paths() -> None:
    counters: list[dict[str, Any]] = []
    t = sm.CountingTransport(httpx.MockTransport(lambda r: httpx.Response(200)), counters,
                             {"used": 0})
    async with httpx.AsyncClient(transport=t) as c:
        for m, url in (("GET", "https://api.preview.platform.athenahealth.com/v1/195900/patients"),
                       ("PUT", ("https://api.preview.platform.athenahealth.com/v1/195900/"
                                "appointments/1/confirmationstatus")),
                       ("GET", "https://evil.example/v1/195900/departments"),
                       ("GET", ("https://api.preview.platform.athenahealth.com/v1/195900/"
                                "departments/21"))):           # no department selected yet
            with pytest.raises(sm.Stop):
                await c.request(m, url)
    assert counters == []


def test_vet_page_rejects_unexpected_rows() -> None:
    good = ("<table><tr><th>A</th></tr><tr><td>ath-e1-7000</td><td>Sandbox patient …0000</td>"
            "<td>w</td><td>s</td><td></td></tr></table>")
    assert len(sm.vet_page(good, [])) == 1
    for bad in (good.replace("Sandbox patient …0000", "Donna Sandboxtest"),
                good.replace("ath-e1-7000", "syn-e1-1"),
                good + "<tr><td>x</td></tr>"):
        with pytest.raises(sm.Stop):
            sm.vet_page(bad, [])
    with pytest.raises(sm.Stop):
        sm.vet_page(good + "secret-value", ["secret-value"])



async def test_counting_transport_allows_only_selected_department() -> None:
    counters: list[dict[str, Any]] = []
    t = sm.CountingTransport(httpx.MockTransport(lambda r: httpx.Response(200)), counters,
                             {"used": 0}, {"id": "21"})
    base = "https://api.preview.platform.athenahealth.com/v1/195900/departments/"
    async with httpx.AsyncClient(transport=t) as c:
        await c.get(base + "21")
        for bad in ("22", "21/providers", "21%0A"):
            with pytest.raises(sm.Stop):
                await c.get(base + bad)
    assert [x["category"] for x in counters] == ["department"]


def test_body_and_when_patterns_exact() -> None:
    body = ("Hi Sandbox patient …0000, this is HaloVox Demo Clinic. You have an appointment on"
            " Wednesday, Oct 28 at 9:00 AM. Reply 1 to confirm, 2 to reschedule, or 3 to"
            " cancel. Reply STOP to opt out.")
    assert sm.BODY_RE.fullmatch(body)
    assert not sm.BODY_RE.fullmatch(body.replace("9:00 AM.", "9:00 AM MDT."))
    assert not sm.BODY_RE.fullmatch(body + "\n")
    m = sm.WHEN_RE.fullmatch("Wednesday, Oct 28 at 9:00 AM MDT")
    assert m is not None and m.group(2) == "MDT"
    assert sm._abbrevs("America/Denver", "2026-10-30") == {"MDT"}
    assert sm._abbrevs("America/Denver", "2026-11-01") == {"MDT", "MST"}
