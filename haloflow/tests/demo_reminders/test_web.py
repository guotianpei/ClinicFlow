"""U-layer HTTP boundary: T04 (R-6), T10 + P-CSRF (R-7), T11 (R-7), staff page labels.

P-CSRF (Codex precision): the submitted token must be bound to a server-held
secret. Matching an attacker-chosen cookie to an attacker-chosen field must
fail.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from haloflow_demo.web import (
    CSRF_COOKIE,
    CSRF_FIELD,
    STAFF_BANNER,
    NullStaffService,
    build_callback_app,
    build_staff_app,
)

BASE = "http://127.0.0.1:8080"


class RecordingStaffService(NullStaffService):
    def __init__(self, inbound_texts: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self._inbound = inbound_texts or []

    async def page_data(self) -> dict[str, Any]:
        data = await super().page_data()
        data["inbound"] = [{"masked_from": "+1********99", "text": t} for t in self._inbound]
        return data

    async def set_paused(self, paused: bool) -> None:
        self.calls.append(f"set_paused:{paused}")


def _staff(svc: RecordingStaffService | None = None) -> tuple[TestClient, RecordingStaffService]:
    svc = svc or RecordingStaffService()
    app = build_staff_app(svc, csrf_secret=b"server-secret-0123456789abcdef!!")
    return TestClient(app, base_url=BASE), svc


def _token_from_page(html: str) -> str:
    m = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', html)
    assert m, "CSRF hidden field missing from staff page"
    return m.group(1)


def _session(client: TestClient) -> str:
    r = client.get("/demo/staff")
    assert r.status_code == 200
    assert CSRF_COOKIE in r.cookies
    return _token_from_page(r.text)


# ── T04 ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/demo/staff", "/health"])
def test_t04_callback_app_exposes_nothing_but_the_webhook(path: str) -> None:
    client = TestClient(build_callback_app())
    assert client.get(path).status_code == 404


def test_t04_callback_app_has_exactly_one_route() -> None:
    app = build_callback_app()
    routes = [(r.path, sorted(getattr(r, "methods", []) or [])) for r in app.routes]
    assert routes == [("/webhooks/sms/inbound", ["POST"])]


def test_t04_staff_app_has_no_docs() -> None:
    client, _ = _staff()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ── T10 + P-CSRF ──────────────────────────────────────────────────────────────


def test_t10_valid_token_is_accepted() -> None:
    client, svc = _staff()
    token = _session(client)
    r = client.post("/demo/pause", data={CSRF_FIELD: token}, follow_redirects=False)
    assert r.status_code == 303
    assert svc.calls == ["set_paused:True"]


def test_t10_bad_host_rejected() -> None:
    client, svc = _staff()
    token = _session(client)
    r = client.post("/demo/pause", data={CSRF_FIELD: token}, headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert svc.calls == []


def test_t10_foreign_origin_rejected() -> None:
    client, svc = _staff()
    token = _session(client)
    r = client.post(
        "/demo/pause", data={CSRF_FIELD: token}, headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert svc.calls == []


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8080", "http://localhost:8080"])
def test_t10_own_origin_allowed(origin: str) -> None:
    client, svc = _staff()
    token = _session(client)
    r = client.post(
        "/demo/pause", data={CSRF_FIELD: token}, headers={"Origin": origin},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert svc.calls == ["set_paused:True"]


def test_t10_missing_token_rejected() -> None:
    client, svc = _staff()
    _session(client)
    assert client.post("/demo/pause", data={}).status_code == 403
    assert svc.calls == []


def test_t10_wrong_token_rejected() -> None:
    client, svc = _staff()
    _session(client)
    assert client.post("/demo/pause", data={CSRF_FIELD: "wrong"}).status_code == 403
    assert svc.calls == []


def test_p_csrf_forged_matching_cookie_and_field_rejected() -> None:
    client, svc = _staff()
    forged = "A" * 43
    client.cookies.set(CSRF_COOKIE, forged)
    r = client.post("/demo/pause", data={CSRF_FIELD: forged})
    assert r.status_code == 403
    assert svc.calls == []


def test_p_csrf_token_from_other_server_secret_rejected() -> None:
    other = TestClient(
        build_staff_app(NullStaffService(), csrf_secret=b"another-secret-0123456789abcdef!"),
        base_url=BASE,
    )
    stolen = _session(other)
    stolen_cookie = other.cookies.get(CSRF_COOKIE)
    assert stolen_cookie is not None
    client, svc = _staff()
    client.cookies.set(CSRF_COOKIE, stolen_cookie)
    r = client.post("/demo/pause", data={CSRF_FIELD: stolen})
    assert r.status_code == 403
    assert svc.calls == []


def test_p_csrf_token_not_transferable_between_sessions() -> None:
    a, _ = _staff()
    token_a = _session(a)
    b, svc_b = _staff()
    _session(b)  # b has its own cookie
    r = b.post("/demo/pause", data={CSRF_FIELD: token_a})
    assert r.status_code == 403
    assert svc_b.calls == []


def test_p_csrf_cookie_flags() -> None:
    client, _ = _staff()
    r = client.get("/demo/staff")
    header = r.headers.get("set-cookie", "")
    assert "HttpOnly" in header
    assert "SameSite=strict" in header or "SameSite=Strict" in header


def test_t10_get_does_not_mutate() -> None:
    client, svc = _staff()
    assert client.get("/demo/pause").status_code == 405
    assert svc.calls == []


def test_r7_no_cors_headers() -> None:
    client, _ = _staff()
    r = client.get("/demo/staff", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ── T11 + page labels ─────────────────────────────────────────────────────────


def test_t11_inbound_text_is_escaped() -> None:
    client, _ = _staff(RecordingStaffService(["<script>alert(1)</script>"]))
    html = client.get("/demo/staff").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_staff_page_states_demo_limits() -> None:
    client, _ = _staff()
    html = client.get("/demo/staff").text
    assert STAFF_BANNER == "Local demonstrator — no authentication; synthetic data only."
    assert STAFF_BANNER in html
