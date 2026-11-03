"""F-TZ3-2 (Codex review of 3b95d16 / 4adf08c, authorised as a separate commit):
the athena source's exact-format checks must reject a trailing newline. Python's
``re.match`` with ``$`` accepts "value\\n"; the source uses ``fullmatch``.

No test touches the network: every transport is httpx.MockTransport.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from haloflow_demo.athena_source import (
    AthenaPreviewClient,
    check_athena_mode,
    extract_tz,
    fetch_snapshot,
)
from haloflow_demo.settings import DemoSettings, StartupRefused

TARGET = date(2026, 10, 28)
NY = "America/New_York"
ATHENA_ENV = {"DEMO_EHR_SOURCE": "athena_preview", "DEMO_ATHENA_DEPARTMENT_ID": "21",
              "ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
              "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID",
              "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET"}


def rec(**over: Any) -> dict[str, Any]:
    r = {"appointmentid": "1002", "patientid": "60179", "date": "10/28/2026",
         "starttime": "09:00", "appointmentstatus": "f", "departmentid": "21"}
    r.update(over)
    return r


async def snap(records: list[dict[str, Any]], dept_tz: str = NY) -> Any:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.url.path == "/v1/195900/departments/21":
            return httpx.Response(200, json=[{"departmentid": "21", "timezonename": dept_tz}])
        return httpx.Response(200, json={"appointments": records, "totalcount": len(records)})
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo",
                               **ATHENA_ENV})
    cfg = check_athena_mode(s, ATHENA_ENV)
    assert cfg is not None
    client = AthenaPreviewClient(cfg, transport=httpx.MockTransport(h))
    try:
        return await fetch_snapshot(client, TARGET, NY, "21", 1)
    finally:
        await client.aclose()


def test_configured_department_id_with_newline_refused() -> None:
    env = {**ATHENA_ENV, "DEMO_ATHENA_DEPARTMENT_ID": "21\n"}
    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@localhost/x_demo", **env})
    with pytest.raises(StartupRefused) as exc:
        check_athena_mode(s, env)
    assert exc.value.reason == "athena_department"


@pytest.mark.parametrize("over", [{"appointmentid": "1001\n", "patientid": "60178"},
                                  {"appointmentid": "1001", "patientid": "60178\n"}])
async def test_ids_with_newline_excluded(over: dict[str, str]) -> None:
    s = await snap([rec(**over), rec()])
    assert [a.emr_appt_id for a in s.fixtures.appointments] == ["ath-e1-1002"]
    assert s.counts == {"INVALID_ID": 1}


@pytest.mark.parametrize("over", [{"date": "10/28/2026\n"}, {"starttime": "09:00\n"}])
async def test_date_time_with_newline_excluded(over: dict[str, str]) -> None:
    s = await snap([rec(appointmentid="1001", patientid="60178", **over), rec()])
    assert [a.emr_appt_id for a in s.fixtures.appointments] == ["ath-e1-1002"]
    assert s.counts == {"MALFORMED_RECORD": 1}
    assert s.fixtures.appointments[0].start_utc == datetime(2026, 10, 28, 13, 0, tzinfo=UTC)


def test_timezone_name_with_newline_not_recognized() -> None:
    assert extract_tz([{"departmentid": "21", "timezonename": "America/Denver\n"}], "21") is None


async def test_timezone_name_with_newline_falls_back_unchanged() -> None:
    s = await snap([rec()], dept_tz="America/Denver\n")
    assert s.tz_status == "UNDETERMINED" and s.basis == "FALLBACK" and s.eff_tz == NY
    assert s.fixtures.appointments[0].start_utc == datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
