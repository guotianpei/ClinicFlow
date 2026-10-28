"""Offline acceptance AT-1..AT-9 for athena_preview_read_probe (N-EHR v1r1).

httpx.MockTransport only: no network. Run: python -m pytest -q test_athena_preview_read_probe.py
"""

from __future__ import annotations

import io
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import athena_preview_read_probe as p  # noqa: E402

TODAY = date(2026, 10, 28)
SECRET = "SENTINEL-SECRET-q7"
TOKEN = "SENTINEL-TOKEN-k3"
GOOD_ENV = {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
            "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID-w2",
            "ATHENA_CLIENT_SECRET": SECRET}
SENTINELS = ("SENTINEL-", "Sandboxtest", "Donna", "424242", "nested-secret-key")


def appt(i: int, **extra: Any) -> dict[str, Any]:
    a = {"appointmentid": str(1000 + i), "patientid": "424242", "departmentid": "1",
         "date": "10/30/2026", "starttime": "09:00", "appointmentstatus": "f",
         "patient": {"firstname": "Donna", "lastname": "Sandboxtest"}}
    a.update(extra)
    return a


class Api:
    """A scripted Preview API. Override per test; records every request."""

    def __init__(self, appts: list[dict[str, Any]] | None = None) -> None:
        self.appts = [appt(1), appt(2)] if appts is None else appts
        self.requests: list[httpx.Request] = []
        self.override: dict[str, httpx.Response] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        for key, resp in self.override.items():
            if key in path:
                return resp
        if path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        if path.endswith("/departments"):
            return httpx.Response(200, json={"departments": [{"departmentid": "1"}]})
        if path.endswith("/appointments/booked"):
            return httpx.Response(200, json={"appointments": self.appts,
                                             "totalcount": len(self.appts)})
        if path.endswith("/confirmationstatus"):
            return httpx.Response(200, json={"confirmationname": "SENTINEL-Confirmed",
                                             "shortname": "C"})
        if "/reference/" in path:
            return httpx.Response(200, json={"status": [{"x": "SENTINEL-ref"}], "totalcount": 1})
        if "/appointments/" in path:
            return httpx.Response(200, json=[self.appts[0]])
        return httpx.Response(404, text="SENTINEL-not-found")


def run(api: Api, env: dict[str, str] | None = None, tmp_path: Path | None = None,
        capsys: Any = None) -> tuple[int, dict[str, Any], str]:
    out = io.StringIO()
    rc = p.main(["--env-file", "unused", "--out", str(tmp_path)], stdout=out,
                env_loader=lambda _p: dict(GOOD_ENV if env is None else env),
                transport=httpx.MockTransport(api), today=TODAY)
    files = list(tmp_path.glob("athena-read-probe-*.json")) if tmp_path else []
    data = json.loads(files[-1].read_text()) if files else {}
    blob = out.getvalue() + (files[-1].read_text() if files else "")
    if capsys is not None:
        cap = capsys.readouterr()
        blob += cap.out + cap.err
    return rc, data, blob


def no_leak(blob: str) -> None:
    for s in SENTINELS:
        assert s not in blob, s


# AT-1 identity gate ---------------------------------------------------------
@pytest.mark.parametrize("override", [
    {"ATHENA_BASE_URL": "http://api.preview.platform.athenahealth.com"},
    {"ATHENA_BASE_URL": "https://api.platform.athenahealth.com"},
    {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com:8443"},
    {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com/v1"},
    {"ATHENA_BASE_URL": "https://u:pw@api.preview.platform.athenahealth.com"},
    {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com#frag"},
    {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com?q=1"},
    {"ATHENA_PRACTICE_ID": "195901"},
    {"ATHENA_PRACTICE_ID": "0195900"},
    {"ATHENA_CLIENT_SECRET": ""},
])
def test_at1_gate_refuses_with_zero_requests(override: dict[str, str], tmp_path: Path,
                                             capsys: Any) -> None:
    api = Api()
    rc, data, blob = run(api, {**GOOD_ENV, **override}, tmp_path, capsys)
    assert rc == 2 and data["result"].startswith("GATE_REFUSED:")
    assert api.requests == []
    no_leak(blob)


# AT-2 non-200 at each step --------------------------------------------------
@pytest.mark.parametrize("step,expected_count", [
    ("/oauth2/v1/token", 1), ("/departments", 2), ("/appointments/booked", 3),
    ("/confirmationstatus", 5), ("/reference/", 6)])
def test_at2_non_200_stops(step: str, expected_count: int, tmp_path: Path, capsys: Any) -> None:
    api = Api([appt(1)])
    api.override[step] = httpx.Response(500, text="SENTINEL-error-body")
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "HTTP_500"
    assert len(api.requests) == expected_count
    no_leak(blob)


# AT-3 redirect not followed -------------------------------------------------
def test_at3_redirect_not_followed(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(
        302, headers={"Location": "https://evil.example/SENTINEL-redirect"})
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "REDIRECT"
    assert len(api.requests) == 3
    no_leak(blob)


# AT-4 allowlist / method guard ---------------------------------------------
@pytest.mark.parametrize("method,url", [
    ("POST", p.ORIGIN + "/v1/195900/appointments/booked"),
    ("PUT", p.ORIGIN + "/v1/195900/appointments/1001/confirmationstatus"),
    ("DELETE", p.ORIGIN + "/v1/195900/appointments/1001"),
    ("GET", p.ORIGIN + "/v1/195900/patients"),
    ("GET", p.ORIGIN + "/v1/195901/departments"),
    ("GET", p.ORIGIN + "/v1/195900/appointments/12a"),
    ("GET", p.ORIGIN + "/v1/195900/appointments/0"),
    ("GET", p.ORIGIN + "/v1/195900/appointments/../x"),
    ("GET", "https://evil.example/v1/195900/departments"),
    ("GET", p.ORIGIN + "/v1/195900/departments?x=1"),
])
def test_at4_disallowed_raises_before_send(method: str, url: str) -> None:
    api = Api()
    c = p.GuardedClient(httpx.MockTransport(api))
    with pytest.raises(p.Stop):
        c._send("x", method, url)
    assert api.requests == [] and c.count == 0
    c.close()


def test_at4_appointment_ids_validated_from_response(tmp_path: Path, capsys: Any) -> None:
    api = Api([appt(1, appointmentid="12a"), appt(2, appointmentid="../x"),
               appt(3, appointmentid="0")])
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 0
    assert not any("/appointments/12a" in str(r.url) or "/x" in r.url.path for r in api.requests)
    assert "no valid appointment id; single/confirmation reads skipped" in data["limitations"]
    no_leak(blob)


# AT-5 bounds and parameters -------------------------------------------------
def test_at5_bounds_and_params(tmp_path: Path, capsys: Any) -> None:
    api = Api([appt(i) for i in range(1, 10)])
    rc, data, _ = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 0 and len(api.requests) <= p.MAX_REQUESTS == 8
    methods = [(r.method, r.url.path) for r in api.requests]
    assert [m for m, _ in methods].count("POST") == 1 and methods[0] == ("POST", "/oauth2/v1/token")
    assert all(m == "GET" for m, _ in methods[1:])
    booked = next(r for r in api.requests if r.url.path.endswith("/appointments/booked"))
    q = dict(booked.url.params)
    assert q == {"startdate": "10/28/2026", "enddate": "11/10/2026", "departmentid": "1",
                 "limit": "50",
                 "showpatientdetail": "false", "showinsurance": "false",
                 "showcopay": "false", "showclaimdetail": "false"}
    singles = [r for r in api.requests if r.url.path.count("/") == 4
               and "/appointments/" in r.url.path and "booked" not in r.url.path]
    assert len(singles) == 3


# AT-6 empty list ------------------------------------------------------------
def test_at6_empty_list_access_only(tmp_path: Path, capsys: Any) -> None:
    rc, data, blob = run(Api([]), tmp_path=tmp_path, capsys=capsys)
    assert rc == 0 and data["result"] == "COMPLETED"
    assert data["outcome_classes"] == ["ACCESS_VERIFIED"]
    assert "appointment shape NOT TESTED (0 appointments returned)" in data["limitations"]
    no_leak(blob)


# AT-7 malformed / missing keys ---------------------------------------------
def test_at7_bad_json(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(200, text="{SENTINEL-not-json")
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "BAD_JSON"
    no_leak(blob)


def test_at7_missing_appointments_key(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(200, json={"items": []})
    rc, data, _ = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "SHAPE"


def test_at7_missing_and_null_matching_keys_are_limitations(tmp_path: Path, capsys: Any) -> None:
    a1 = appt(1)
    del a1["patientid"]
    api = Api([a1, appt(2, starttime=None)])
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 0 and "READ_SHAPE_VERIFIED" in data["outcome_classes"]
    mk = data["matching_keys"]
    assert mk["patientid"]["absent"] == 1 and mk["starttime"]["null"] == 1
    assert "matching key patientid missing or null in some records" in data["limitations"]
    no_leak(blob)


# AT-8 leakage ---------------------------------------------------------------
def test_at8_sentinels_never_emitted(tmp_path: Path, capsys: Any) -> None:
    a = appt(1, appointmentstatus="SENTINEL-status", **{"SENTINEL-topkey": "v"},
             customfields=[{"nested-secret-key": "SENTINEL-nested"}],
             appointmentmessage="SENTINEL-free-text")
    api = Api([a])
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 0
    assert data["appointment_fields"]["undocumented_key_occurrences"] == 1
    assert data["appointmentstatus_codes"]["UNDOCUMENTED"] == 1
    no_leak(blob)


def test_at8_network_exception_text_not_emitted(tmp_path: Path, capsys: Any) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"SENTINEL-url {req.url}", request=req)
    out = io.StringIO()
    rc = p.main(["--env-file", "x", "--out", str(tmp_path)], stdout=out,
                env_loader=lambda _p: dict(GOOD_ENV), transport=httpx.MockTransport(boom),
                today=TODAY)
    cap = capsys.readouterr()
    blob = out.getvalue() + cap.out + cap.err + next(tmp_path.glob("*.json")).read_text()
    assert rc == 1 and '"result": "NETWORK"' in blob
    no_leak(blob)


# AT-9 status codes ----------------------------------------------------------
def test_at9_documented_codes_counted(tmp_path: Path, capsys: Any) -> None:
    api = Api([appt(1, appointmentstatus="f"), appt(2, appointmentstatus="x"),
               appt(3, appointmentstatus="2"), appt(4, appointmentstatus="Z")])
    rc, data, _ = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 0
    c = data["appointmentstatus_codes"]
    assert (c["f"], c["x"], c["2"], c["UNDOCUMENTED"]) == (1, 1, 1, 1)
    assert set(c) == {"x", "f", "o", "2", "3", "4", "UNDOCUMENTED"}


def test_confirmation_and_reference_shapes(tmp_path: Path, capsys: Any) -> None:
    rc, data, blob = run(Api([appt(1)]), tmp_path=tmp_path, capsys=capsys)
    assert rc == 0 and "CONFIRMATION_READ_SHAPE_VERIFIED" in data["outcome_classes"]
    names = [f["field"] for f in data["confirmationstatus_fields"]["fields"]]
    assert names == ["confirmationname", "shortname"]
    assert data["reference_statuses"] == {"returned_count": 1, "totalcount_type": "number"}
    assert data["write"].startswith("NOT TESTED")
    no_leak(blob)


# Codex F-P1 / F-P2 regressions ------------------------------------------------
@pytest.mark.parametrize("conf", [{"confirmationname": None, "shortname": "C"},
                                  {"shortname": "C"}, {"confirmationname": "x", "shortname": None}])
def test_fp1_missing_or_null_confirmation_not_verified(conf: dict[str, Any], tmp_path: Path,
                                                        capsys: Any) -> None:
    api = Api([appt(1)])
    api.override["/confirmationstatus"] = httpx.Response(200, json=conf)
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 0
    assert "CONFIRMATION_READ_SHAPE_VERIFIED" not in data["outcome_classes"]
    assert any("confirmation read shape NOT verified" in x for x in data["limitations"])
    no_leak(blob)


@pytest.mark.parametrize("conf", [{"confirmationname": [], "shortname": "C"},
                                  {"confirmationname": "x", "shortname": 3}, ["x"]])
def test_fp1_wrong_type_confirmation_is_shape_failure(conf: Any, tmp_path: Path,
                                                      capsys: Any) -> None:
    api = Api([appt(1)])
    api.override["/confirmationstatus"] = httpx.Response(200, json=conf)
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "SHAPE"
    assert "CONFIRMATION_READ_SHAPE_VERIFIED" not in data["outcome_classes"]
    no_leak(blob)


@pytest.mark.parametrize("entries", [["bad"], [appt(1), "bad"], [None], [[1]]])
def test_fp1_malformed_booked_entries_are_shape_failure(entries: list[Any], tmp_path: Path,
                                                        capsys: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(
        200, json={"appointments": entries, "totalcount": len(entries)})
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "SHAPE"
    assert "READ_SHAPE_VERIFIED" not in data["outcome_classes"]
    assert not any("0 appointments returned" in x for x in data["limitations"])
    no_leak(blob)


@pytest.mark.parametrize("single", [[], ["bad"], [appt(1), 5], "SENTINEL-text", 7])
def test_fp1_malformed_single_response_is_shape_failure(single: Any, tmp_path: Path,
                                                        capsys: Any) -> None:
    api = Api([appt(1)])
    api.override["/appointments/1001"] = httpx.Response(200, json=single)
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "SHAPE"
    assert "single_appointment_fields" not in data
    no_leak(blob)


def test_fp2_refused_gate_never_claims_passed(tmp_path: Path, capsys: Any) -> None:
    rc, data, blob = run(Api(), {**GOOD_ENV, "ATHENA_PRACTICE_ID": "195901"}, tmp_path, capsys)
    assert rc == 2
    assert data["practice"] == "identity gate REFUSED"
    assert "passed" not in blob
    rc, data, _ = run(Api(), tmp_path=tmp_path / "ok", capsys=capsys)
    assert rc == 0 and data["practice"] == "published Preview sandbox (identity gate passed)"



# r2: departmentid from the departments response + safe 4xx diagnostic -----------
@pytest.mark.parametrize("deps", [[], [{"departmentid": "x1"}], ["1"], [{"name": "SENTINEL-d"}]])
def test_r2_no_valid_department_stops_before_booked(deps: list[Any], tmp_path: Path,
                                                    capsys: Any) -> None:
    api = Api()
    api.override["/departments"] = httpx.Response(200, json={"departments": deps})
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "NO_DEPARTMENT"
    assert not any(r.url.path.endswith("/booked") for r in api.requests)
    no_leak(blob)


def test_r2_first_valid_department_used(tmp_path: Path, capsys: Any) -> None:
    api = Api([appt(1)])
    api.override["/departments"] = httpx.Response(
        200, json={"departments": [{"departmentid": "0"}, {"departmentid": "21"},
                                   {"departmentid": "22"}]})
    rc, _, _ = run(api, tmp_path=tmp_path, capsys=capsys)
    booked = next(r for r in api.requests if r.url.path.endswith("/booked"))
    assert rc == 0 and booked.url.params["departmentid"] == "21"


def test_r2_400_reports_only_allowlisted_parameter_names(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(
        400, json={"error": "Additional fields are required: DepartmentID. SENTINEL-body",
                   "detailedmessage": "startdate format SENTINEL-x; departmentidx"})
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and data["result"] == "HTTP_400"
    entry = data["requests"][-1]
    assert entry["name"] == "booked" and entry["error_mentions"] == ["departmentid", "startdate"]
    no_leak(blob)


def test_r2_non_4xx_has_no_diagnostic(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(503, text="departmentid SENTINEL")
    rc, data, blob = run(api, tmp_path=tmp_path, capsys=capsys)
    assert rc == 1 and "error_mentions" not in data["requests"][-1]
    no_leak(blob)
