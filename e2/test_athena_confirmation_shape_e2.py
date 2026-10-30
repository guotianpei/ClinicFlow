"""Offline tests T1-T10 (+T4b, T7b, T8b) for athena_confirmation_shape_e2 (E2 plan v1r2).

httpx.MockTransport only: no network. Written before the runner (tests first).
Run: python -m pytest -q e2/test_athena_confirmation_shape_e2.py
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
import athena_confirmation_shape_e2 as e2  # noqa: E402

TODAY = date(2026, 10, 30)
SECRET = "CANARY-SECRET-q7"
TOKEN = "CANARY-TOKEN-k3"
GOOD_ENV = {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
            "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "CANARY-CID-w2",
            "ATHENA_CLIENT_SECRET": SECRET}
# Every string a mocked response can carry starts with CANARY or is a canary id/date.
CANARY_IDS = ("987654321", "987654322", "987654323", "987654324", "987654325")
CANARIES = ("CANARY", "Sandboxtest", "10/31/2026", "424242", *CANARY_IDS)


def appt(i: int, **extra: Any) -> dict[str, Any]:
    a = {"appointmentid": CANARY_IDS[i], "patientid": "424242", "departmentid": "1",
         "date": "10/31/2026", "starttime": "09:00", "appointmentstatus": "f",
         "patient": {"lastname": "Sandboxtest"}}
    a.update(extra)
    return a


def vocab(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"statusid": 7000 + start + k, "name": f"CANARY-status-{start + k}"}
            for k in range(n)]


class Api:
    """Scripted Preview API. Records every request. Vocabulary pages keyed by offset."""

    def __init__(self, appts: list[dict[str, Any]] | None = None,
                 pages: dict[int, Any] | None = None,
                 conf: Any = None) -> None:
        self.appts = [appt(0), appt(1)] if appts is None else appts
        self.pages = {0: {"status": vocab(2), "totalcount": 2}} if pages is None else pages
        self.conf = ({"confirmationname": "CANARY-status-0", "shortname": "CANARY-C"}
                     if conf is None else conf)
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
            return httpx.Response(200, json=self.conf)
        if path.endswith("/reference/appointmentconfirmationstatus"):
            off = int(dict(req.url.params).get("offset", "0"))
            body = self.pages.get(off, {"status": [], "totalcount": 0})
            return httpx.Response(200, json=body)
        return httpx.Response(404, text="CANARY-not-found")


def run(api: Api, tmp_path: Path, capsys: Any, env: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, Any], str]:
    out = io.StringIO()
    rc = e2.main(["--env-file", "unused", "--out", str(tmp_path)], stdout=out,
                 env_loader=lambda _p: dict(GOOD_ENV if env is None else env),
                 transport=httpx.MockTransport(api), today=TODAY)
    files = sorted(tmp_path.glob("athena-confirmation-shape-e2-*.json"))
    data = json.loads(files[-1].read_text()) if files else {}
    cap = capsys.readouterr()
    blob = out.getvalue() + (files[-1].read_text() if files else "") + cap.out + cap.err
    return rc, data, blob


def no_leak(blob: str) -> None:
    for s in CANARIES:
        assert s not in blob, s


def paths(api: Api) -> list[str]:
    return [r.url.path for r in api.requests]


# T1 identity gate -----------------------------------------------------------
@pytest.mark.parametrize("override", [
    {"ATHENA_BASE_URL": "http://api.preview.platform.athenahealth.com"},
    {"ATHENA_BASE_URL": "https://api.platform.athenahealth.com"},
    {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com:8443"},
    {"ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com/v1"},
    {"ATHENA_PRACTICE_ID": "195901"},
    {"ATHENA_PRACTICE_ID": "0195900"},
    {"ATHENA_CLIENT_ID": ""},
    {"ATHENA_CLIENT_SECRET": ""},
])
def test_t1_gate_refuses_with_zero_requests(override: dict[str, str], tmp_path: Path,
                                            capsys: Any) -> None:
    api = Api()
    rc, data, blob = run(api, tmp_path, capsys, {**GOOD_ENV, **override})
    assert rc == 2 and data["result"].startswith("GATE_REFUSED:")
    assert api.requests == []
    no_leak(blob)


# T2 allowlist / method guard ------------------------------------------------
@pytest.mark.parametrize("method,url", [
    ("PUT", e2.ORIGIN + "/v1/195900/appointments/1001/confirmationstatus"),
    ("POST", e2.ORIGIN + "/v1/195900/appointments/changed/subscription"),
    ("DELETE", e2.ORIGIN + "/v1/195900/appointments/changed/subscription"),
    ("GET", e2.ORIGIN + "/v1/195900/appointments/changed"),
    ("GET", e2.ORIGIN + "/v1/195900/appointments/1001"),
    ("GET", e2.ORIGIN + "/v1/195900/patients"),
    ("GET", e2.ORIGIN + "/v1/195901/departments"),
    ("GET", e2.ORIGIN + "/v1/195900/appointments/12a/confirmationstatus"),
    ("GET", e2.ORIGIN + "/v1/195900/appointments/0/confirmationstatus"),
    ("GET", "https://evil.example/v1/195900/departments"),
    ("GET", e2.ORIGIN + "/v1/195900/departments?x=1"),
])
def test_t2_disallowed_raises_before_send(method: str, url: str) -> None:
    api = Api()
    c = e2.GuardedClient(httpx.MockTransport(api))
    with pytest.raises(e2.Stop):
        c._send("x", method, url)
    assert api.requests == [] and c.count == 0
    c.close()


def test_t2_only_expected_requests_on_happy_path(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 0 and data["result"] == "COMPLETED"
    methods = [(r.method, r.url.path) for r in api.requests]
    assert methods[0] == ("POST", "/oauth2/v1/token")
    assert all(m == "GET" for m, _ in methods[1:])
    assert [m for m, _ in methods].count("POST") == 1
    booked = next(r for r in api.requests if r.url.path.endswith("/appointments/booked"))
    assert dict(booked.url.params) == {
        "startdate": "10/30/2026", "enddate": "11/12/2026", "departmentid": "1", "limit": "50",
        "showpatientdetail": "false", "showinsurance": "false", "showcopay": "false",
        "showclaimdetail": "false"}
    ref = [r for r in api.requests if "/reference/" in r.url.path]
    assert [dict(r.url.params) for r in ref] == [{"limit": "100", "offset": "0"}]
    no_leak(blob)


# T3 request cap --------------------------------------------------------------
def test_t3_eleventh_request_impossible() -> None:
    api = Api()
    c = e2.GuardedClient(httpx.MockTransport(api))
    url = e2.ORIGIN + "/v1/195900/departments"
    for _ in range(e2.MAX_REQUESTS):
        c._send("x", "GET", url)
    with pytest.raises(e2.Stop) as ex:
        c._send("x", "GET", url)
    assert ex.value.code == "BUDGET"
    assert e2.MAX_REQUESTS == 10 and len(api.requests) == 10
    c.close()


def test_t3_cap_during_paging_is_incomplete(tmp_path: Path, capsys: Any) -> None:
    pages = {o: {"status": vocab(100, o), "totalcount": 500} for o in (0, 100, 200, 300)}
    api = Api(appts=[appt(i) for i in range(5)], pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    assert len(api.requests) == 10
    v = data["vocabulary"]
    assert v["pages"] == 4 and v["total_reached"] is False
    assert v["complete"] is False and v["incomplete_reason"] == "CAP"
    assert "VOCABULARY_INCOMPLETE" in data["outcome_classes"]
    no_leak(blob)


# T4 vocabulary paging --------------------------------------------------------
@pytest.mark.parametrize("n", [1, 2, 4])
def test_t4_complete_in_n_pages(n: int, tmp_path: Path, capsys: Any) -> None:
    total = 100 * (n - 1) + 37
    pages = {}
    for k in range(n):
        size = 100 if k < n - 1 else 37
        pages[100 * k] = {"status": vocab(size, 100 * k), "totalcount": total}
    api = Api(pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    v = data["vocabulary"]
    assert rc == 0 and v["pages"] == n and v["entries_seen"] == total
    assert v["total_reached"] is True and v["complete"] is True
    assert v["incomplete_reason"] is None
    assert v["duplicate_statusid"] == 0 and v["names_unique"] is True
    assert {"TOTAL_REACHED", "VOCABULARY_COMPLETE",
            "VOCABULARY_ELEMENT_SHAPE_VERIFIED"} <= set(data["outcome_classes"])
    no_leak(blob)


@pytest.mark.parametrize("pages,reason", [
    ({0: {"status": vocab(100), "totalcount": 300},
      100: {"status": vocab(100, 100), "totalcount": 300},
      200: {"status": vocab(40, 200), "totalcount": 300}}, "SHORT_PAGE"),
    ({0: {"status": vocab(100), "totalcount": 150},
      100: {"status": vocab(50, 100), "totalcount": 151}}, "TOTAL_CHANGED"),
    ({0: {"status": vocab(1)}}, "BAD_TOTAL"),
    ({0: {"status": vocab(1), "totalcount": "1"}}, "BAD_TOTAL"),
    ({0: {"status": vocab(1), "totalcount": 1.0}}, "BAD_TOTAL"),
    ({0: {"status": vocab(50), "totalcount": 120}}, "SHORT_PAGE"),
    ({0: {"status": [], "totalcount": 3}}, "SHORT_PAGE"),
])
def test_t4_incomplete(pages: dict[int, Any], reason: str, tmp_path: Path,
                       capsys: Any) -> None:
    api = Api(pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    v = data["vocabulary"]
    assert v["complete"] is False and v["total_reached"] is False
    assert v["incomplete_reason"] == reason
    assert "VOCABULARY_INCOMPLETE" in data["outcome_classes"]
    assert "VOCABULARY_COMPLETE" not in data["outcome_classes"]
    no_leak(blob)


# T4b paging hostility -------------------------------------------------------
@pytest.mark.parametrize("pages,reason", [
    ({0: {"status": vocab(1), "totalcount": True}}, "BAD_TOTAL"),
    ({0: {"status": vocab(1), "totalcount": -1}}, "BAD_TOTAL"),
    ({0: {"status": vocab(3), "totalcount": 2}}, "OVERRUN"),
    ({0: {"status": vocab(100), "totalcount": 200},
      100: {"status": vocab(100), "totalcount": 200}}, "REPEATED_PAGE"),
    ({0: {"status": {"a": 1}, "totalcount": 1}}, "MALFORMED"),
    ({0: {"status": ["CANARY-x"], "totalcount": 1}}, "MALFORMED"),
    ({0: ["CANARY-not-object"]}, "MALFORMED"),
    ({0: {"status": [{"statusid": 7001, "name": "CANARY-a"},
                     {"statusid": 7001, "name": "CANARY-b"}], "totalcount": 2}},
     "DUPLICATE_ID"),
])
def test_t4b_hostile_pages(pages: dict[int, Any], reason: str, tmp_path: Path,
                           capsys: Any) -> None:
    api = Api(pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    v = data["vocabulary"]
    assert v["complete"] is False and v["incomplete_reason"] == reason
    no_leak(blob)


def test_t4b_next_links_never_followed(tmp_path: Path, capsys: Any) -> None:
    nxt = e2.ORIGIN + "/v1/195900/reference/appointmentconfirmationstatus?offset=999&CANARY=1"
    pages = {0: {"status": vocab(2), "totalcount": 2, "next": nxt, "previous": nxt}}
    api = Api(pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    ref = [r for r in api.requests if "/reference/" in r.url.path]
    assert len(ref) == 1 and dict(ref[0].url.params) == {"limit": "100", "offset": "0"}
    assert data["vocabulary"]["complete"] is True
    no_leak(blob)


# T5 confirmation read shape --------------------------------------------------
def test_t5_empty_confirmation_object_is_limitation(tmp_path: Path, capsys: Any) -> None:
    api = Api(conf={})
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 0
    reads = data["confirmation_reads"]
    assert len(reads) == 2 and all(r["empty"] is True and r["verified"] is False
                                   for r in reads)
    assert all(r["q5_match"] == "UNAVAILABLE" for r in reads)
    assert "CONFIRMATION_READ_SHAPE_VERIFIED" not in data["outcome_classes"]
    no_leak(blob)


@pytest.mark.parametrize("conf", [
    {"confirmationname": 5}, {"shortname": ["CANARY-x"]}, ["CANARY-list"], "CANARY-str"])
def test_t5_wrong_types_are_shape(conf: Any, tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/confirmationstatus"] = httpx.Response(200, json=conf)
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 1 and data["result"] == "SHAPE"
    no_leak(blob)


def test_t5_populated_read_verified(tmp_path: Path, capsys: Any) -> None:
    rc, data, blob = run(Api(), tmp_path, capsys)
    reads = data["confirmation_reads"]
    assert all(r["verified"] is True and r["empty"] is False for r in reads)
    assert reads[0]["keys"] == {"confirmationname": "string", "shortname": "string"}
    no_leak(blob)


# T6 id selection -------------------------------------------------------------
@pytest.mark.parametrize("appts,expected", [
    ([], 0),
    ([appt(0)], 1),
    ([appt(0), appt(1), appt(2)], 3),
    ([appt(i) for i in range(5)], 3),
    ([appt(0), appt(0), appt(1)], 2),
    ([appt(0, appointmentid="12a"), appt(1, appointmentid="0"),
      appt(2, appointmentid="../x"), appt(3, appointmentid=987654324),
      appt(4)], 1),
])
def test_t6_confirmation_reads_bounded_and_validated(appts: list[dict[str, Any]],
                                                     expected: int, tmp_path: Path,
                                                     capsys: Any) -> None:
    api = Api(appts=appts)
    rc, data, blob = run(api, tmp_path, capsys)
    conf = [x for x in paths(api) if x.endswith("/confirmationstatus")]
    assert len(conf) == expected
    assert len(set(conf)) == len(conf)
    for x in paths(api):
        assert "12a" not in x and "/0/" not in x and ".." not in x
    if not appts:
        assert "no valid appointment id; confirmation reads skipped" in data["limitations"]
    no_leak(blob)


def test_t6_malformed_booked_collection_is_shape(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/appointments/booked"] = httpx.Response(
        200, json={"appointments": ["CANARY-x"], "totalcount": 1})
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 1 and data["result"] == "SHAPE"
    no_leak(blob)


# T7 / T7b capture scans ------------------------------------------------------
def test_t7_no_values_in_capture(tmp_path: Path, capsys: Any) -> None:
    pages = {0: {"status": vocab(100), "totalcount": 150},
             100: {"status": vocab(50, 100), "totalcount": 150}}
    rc, data, blob = run(Api(appts=[appt(i) for i in range(4)], pages=pages),
                         tmp_path, capsys)
    assert rc == 0
    no_leak(blob)
    assert TOKEN not in blob and SECRET not in blob


def test_t7b_hostile_key_names_never_echoed(tmp_path: Path, capsys: Any) -> None:
    conf = {"confirmationname": "CANARY-status-0", "CANARY-key-conf": 1}
    pages = {0: {"status": [{"statusid": 7000, "name": "CANARY-status-0",
                             "CANARY-key-elem": "x"}],
                 "totalcount": 1, "CANARY-key-page": True}}
    api = Api(conf=conf, pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 0
    assert data["confirmation_reads"][0]["undocumented_key_count"] == 1
    assert data["vocabulary"]["undocumented_page_key_count"] == 1
    assert data["vocabulary"]["undocumented_element_key_count"] == 1
    assert "VOCABULARY_ELEMENT_SHAPE_VERIFIED" not in data["outcome_classes"]
    no_leak(blob)


# T8 / T8b uniqueness, Q5 ----------------------------------------------------
def test_t8_duplicate_names_flagged(tmp_path: Path, capsys: Any) -> None:
    pages = {0: {"status": [{"statusid": 7000, "name": "CANARY-dup"},
                            {"statusid": 7001, "name": "CANARY-dup"}], "totalcount": 2}}
    api = Api(conf={"confirmationname": "CANARY-dup"}, pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    v = data["vocabulary"]
    assert v["complete"] is True and v["names_unique"] is False
    assert data["confirmation_reads"][0]["q5_match"] == "multiple"
    no_leak(blob)


@pytest.mark.parametrize("elem", [
    {"name": "CANARY-a"}, {"statusid": True, "name": "CANARY-a"},
    {"statusid": "7000", "name": "CANARY-a"}, {"statusid": 7000},
    {"statusid": 7000, "name": 3}])
def test_t8_bad_element_makes_metrics_unavailable(elem: dict[str, Any], tmp_path: Path,
                                                  capsys: Any) -> None:
    pages = {0: {"status": [{"statusid": 7001, "name": "CANARY-b"}, elem], "totalcount": 2}}
    api = Api(pages=pages)
    rc, data, blob = run(api, tmp_path, capsys)
    v = data["vocabulary"]
    assert v["total_reached"] is True and v["complete"] is False
    assert v["duplicate_statusid"] == "UNAVAILABLE"
    assert v["names_unique"] == "UNAVAILABLE"
    assert all(r["q5_match"] == "UNAVAILABLE" for r in data["confirmation_reads"])
    assert "VOCABULARY_ELEMENT_SHAPE_VERIFIED" not in data["outcome_classes"]
    no_leak(blob)


@pytest.mark.parametrize("name,expected", [
    ("CANARY-status-0", "1"), ("CANARY-none", "0")])
def test_t8_q5_cardinality(name: str, expected: str, tmp_path: Path, capsys: Any) -> None:
    api = Api(conf={"confirmationname": name})
    rc, data, blob = run(api, tmp_path, capsys)
    assert data["confirmation_reads"][0]["q5_match"] == expected
    no_leak(blob)


def test_t8c_zero_total_complete_but_element_shape_not_verified(tmp_path: Path,
                                                                 capsys: Any) -> None:
    # Codex qualification: a zero total may establish count completeness, but zero
    # samples cannot verify the element schema.
    api = Api(pages={0: {"status": [], "totalcount": 0}})
    rc, data, blob = run(api, tmp_path, capsys)
    v = data["vocabulary"]
    assert rc == 0 and v["total_reached"] is True and v["complete"] is True
    assert v["entries_seen"] == 0
    assert "VOCABULARY_ELEMENT_SHAPE_VERIFIED" not in data["outcome_classes"]
    assert "vocabulary empty; element schema NOT verified (0 samples)" in data["limitations"]
    no_leak(blob)


def test_t8b_shortname_on_element_is_undocumented(tmp_path: Path, capsys: Any) -> None:
    pages = {0: {"status": [{"statusid": 7000, "name": "CANARY-a", "shortname": "CANARY-s"}],
                 "totalcount": 1}}
    rc, data, blob = run(Api(pages=pages), tmp_path, capsys)
    v = data["vocabulary"]
    assert v["undocumented_element_key_count"] == 1
    assert set(v["element_keys"]) == {"statusid", "name"}
    no_leak(blob)


# T9 non-200, redirect --------------------------------------------------------
@pytest.mark.parametrize("step,count", [
    ("/oauth2/v1/token", 1), ("/departments", 2), ("/appointments/booked", 3),
    ("/confirmationstatus", 4), ("/reference/", 6)])
def test_t9_non_200_stops_without_body(step: str, count: int, tmp_path: Path,
                                       capsys: Any) -> None:
    api = Api()
    api.override[step] = httpx.Response(400, text="CANARY-error departmentid limit")
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 1 and data["result"] == "HTTP_400" and len(api.requests) == count
    last = data["requests"][-1]
    assert set(last.get("error_mentions", [])) <= set(e2.DIAG_PARAM_NAMES)
    no_leak(blob)


def test_t9_redirect_refused(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/reference/"] = httpx.Response(
        302, headers={"Location": "https://evil.example/CANARY-redirect"})
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 1 and data["result"] == "REDIRECT"
    assert not any("evil" in str(r.url) for r in api.requests)
    no_leak(blob)


# T10 exceptions, transport errors --------------------------------------------
def test_t10_transport_error_fixed_code(tmp_path: Path, capsys: Any) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/appointments/booked"):
            raise httpx.ConnectTimeout("CANARY-timeout " + str(req.url))
        return Api()(req)
    out = io.StringIO()
    rc = e2.main(["--env-file", "unused", "--out", str(tmp_path)], stdout=out,
                 env_loader=lambda _p: dict(GOOD_ENV), transport=httpx.MockTransport(boom),
                 today=TODAY)
    blob = out.getvalue() + capsys.readouterr().err
    assert rc == 1 and json.loads(out.getvalue())["result"] == "NETWORK"
    no_leak(blob)


def test_t10_internal_exception_fixed_code(tmp_path: Path, capsys: Any,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("CANARY-internal")
    monkeypatch.setattr(e2, "_vocabulary", explode)
    rc, data, blob = run(Api(), tmp_path, capsys)
    assert rc == 1 and data["result"] == "INTERNAL"
    no_leak(blob)


def test_t10_bad_json(tmp_path: Path, capsys: Any) -> None:
    api = Api()
    api.override["/confirmationstatus"] = httpx.Response(200, text="{CANARY-not-json")
    rc, data, blob = run(api, tmp_path, capsys)
    assert rc == 1 and data["result"] == "BAD_JSON"
    no_leak(blob)
