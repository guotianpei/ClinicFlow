"""Offline checks for athena_department_tz_probe.py — mocked transport, no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import athena_department_tz_probe as tp

ENV = {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
       "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID",
       "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET", "CLINIC_TZ": "America/New_York"}


class Fake:
    def __init__(self, dept: Any, dept_status: int = 200, ids: Any = None) -> None:
        self.dept, self.dept_status = dept, dept_status
        self.ids = ids if ids is not None else [{"departmentid": "x"},
                                                {"departmentid": "21", "name": "SENTINEL-dept"}]
        self.seen: list[tuple[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append((req.method, req.url.path))
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "SENTINEL-TOKEN"})
        if req.url.path == "/v1/195900/departments":
            return httpx.Response(200, json={"departments": self.ids})
        if req.url.path == "/v1/195900/departments/21":
            return httpx.Response(self.dept_status, json=self.dept)
        return httpx.Response(404)


def dept(**kw: Any) -> list[dict[str, Any]]:
    return [{"departmentid": "21", "name": "SENTINEL-dept", "address": "SENTINEL-addr", **kw}]


async def go(fake: Fake, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    return await tp.probe(env or ENV, transport_factory=lambda: httpx.MockTransport(fake))


def no_leak(out: dict[str, Any]) -> None:
    blob = json.dumps(out)
    assert "SENTINEL" not in blob and '"21"' not in blob and "/departments/21" not in blob


@pytest.mark.parametrize("name,expected", [
    ("America/New_York", "VERIFIED"),
    ("US/Eastern", "UNDETERMINED_OTHER_IANA"),           # alias spelling: never MISMATCH
    ("America/Chicago", "UNDETERMINED_OTHER_IANA"),
    ("Eastern Standard Time", "UNDETERMINED_NON_IANA"),  # display name
    ("EST5EDT-ish", "UNDETERMINED_NON_IANA"),
])
async def test_classification(name: str, expected: str) -> None:
    fake = Fake(dept(timezonename=name, timezone=-5, timezoneoffset=-5,
                     doesnotobservedst="false"))
    rc, out = await go(fake)
    assert rc == 0 and out["result"] == "COMPLETED"
    assert out["classification"] == expected
    assert fake.seen == [("POST", "/oauth2/v1/token"), ("GET", "/v1/195900/departments"),
                         ("GET", "/v1/195900/departments/21")]
    assert out["request_count"] == 3 and out["department_source"] == "first_listed"
    no_leak(out)


async def test_configured_department_used_and_must_be_listed() -> None:
    fake = Fake(dept(timezonename="America/New_York"))
    rc, out = await go(fake, {**ENV, "DEMO_ATHENA_DEPARTMENT_ID": "21"})
    assert rc == 0 and out["department_source"] == "configured"
    assert fake.seen[-1] == ("GET", "/v1/195900/departments/21")
    fake2 = Fake(dept())
    rc, out = await go(fake2, {**ENV, "DEMO_ATHENA_DEPARTMENT_ID": "22"})
    assert rc == 1 and out["result"] == "DEPARTMENT_NOT_LISTED" and out["request_count"] == 2
    rc, out = await go(Fake(dept()), {**ENV, "DEMO_ATHENA_DEPARTMENT_ID": "x/1"})
    assert rc == 2


async def test_offset_and_dst_never_verify() -> None:
    rc, out = await go(Fake(dept(timezone=-5, timezoneoffset=-5, doesnotobservedst=False)))
    assert rc == 0 and out["classification"] == "UNDETERMINED_MISSING"
    assert out["fields"]["timezone"]["value"] == -5


@pytest.mark.parametrize("bad", [None, 7, "x" * 65, "SENTINEL<script>", ["America/New_York"]])
async def test_bad_name_is_fixed_marker(bad: Any) -> None:
    rc, out = await go(Fake(dept(timezonename=bad)))
    assert rc == 0 and out["classification"] == "UNDETERMINED_MISSING"
    v = out["fields"]["timezonename"]["value"]
    assert v in (None, tp.REJECTED)
    no_leak(out)


@pytest.mark.parametrize("bad", [True, 99, "SENTINEL", {"a": 1}, "-5" * 10])
async def test_bad_numeric_and_flag_are_fixed_markers(bad: Any) -> None:
    rc, out = await go(Fake(dept(timezonename="America/New_York", timezone=bad,
                                 timezoneoffset=bad, doesnotobservedst=bad)))
    assert rc == 0
    for k in ("timezone", "timezoneoffset"):
        assert out["fields"][k]["value"] == tp.REJECTED
    if not isinstance(bad, bool):
        assert out["fields"]["doesnotobservedst"]["value"] == tp.REJECTED
    no_leak(out)


async def test_only_four_fields_recorded() -> None:
    _rc, out = await go(Fake(dept(timezonename="America/New_York")))
    assert set(out["fields"]) == {"timezonename", "timezone", "timezoneoffset",
                                  "doesnotobservedst"}
    assert out["fields"]["timezone"] == {"present": False}


@pytest.mark.parametrize("shape", [[], [1], [{}, {}], "SENTINEL", 5])
async def test_bad_shape_stops(shape: Any) -> None:
    rc, out = await go(Fake(shape))
    assert rc == 1 and out["result"] == "SHAPE"
    no_leak(out)


async def test_http_error_stops_with_fixed_code() -> None:
    rc, out = await go(Fake({"error": "SENTINEL-body"}, dept_status=500))
    assert rc == 1 and out["result"] == "HTTP_500" and "fields" not in out
    no_leak(out)


async def test_no_valid_department_stops_after_two_requests() -> None:
    fake = Fake(dept(), ids=[{"departmentid": "0"}, {"departmentid": "a/b"}])
    rc, out = await go(fake)
    assert rc == 1 and out["result"] == "NO_DEPARTMENT" and out["request_count"] == 2


@pytest.mark.parametrize("override", [{"ATHENA_PRACTICE_ID": "195901"},
                                      {"ATHENA_BASE_URL": "https://api.platform.athenahealth.com"},
                                      {"ATHENA_BASE_URL": "http://api.preview.platform.athenahealth.com"},
                                      {"ATHENA_CLIENT_SECRET": ""}])
async def test_gate_refuses_before_any_request(override: dict[str, str]) -> None:
    fake = Fake(dept())
    rc, out = await go(fake, {**ENV, **override})
    assert rc == 2 and out["result"] == "GATE_REFUSED" and fake.seen == []


async def test_guard_refuses_other_paths_and_budget() -> None:
    log: list[dict[str, Any]] = []
    t = tp.Guarded(httpx.MockTransport(lambda r: httpx.Response(200)), log)
    base = "https://api.preview.platform.athenahealth.com"
    async with httpx.AsyncClient(transport=t) as c:
        for m, p in (("GET", "/v1/195900/appointments/booked"), ("PUT", "/v1/195900/departments"),
                     ("GET", "/v1/195900/departments/21/providers"),
                     ("GET", "/v1/195901/departments")):
            with pytest.raises(tp.Stop):
                await c.request(m, base + p)
        with pytest.raises(tp.Stop):
            await c.get("https://evil.example/v1/195900/departments")
        for _ in range(3):
            await c.get(base + "/v1/195900/departments")
        with pytest.raises(tp.Stop) as exc:
            await c.get(base + "/v1/195900/departments")
    assert exc.value.code == "BUDGET" and t.used == 3


def test_main_writes_only_bounded_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    envf = tmp_path / ".env"
    envf.write_text("\n".join(f"{k}={v}" for k, v in ENV.items()) + "\n")
    fake = Fake(dept(timezonename="America/New_York"))
    real = tp.probe
    monkeypatch.setattr(tp, "probe", lambda env: real(
        env, transport_factory=lambda: httpx.MockTransport(fake)))
    rc = tp.main(["--env-file", str(envf), "--out", str(tmp_path / "o")])
    files = list((tmp_path / "o").iterdir())
    assert rc == 0 and [f.name for f in files] == ["department-tz.json"]
    no_leak(json.loads(files[0].read_text()))
