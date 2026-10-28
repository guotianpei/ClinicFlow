"""N-UX staff action feedback (package v1 + v1r1, Codex ALIGN 2026-10-28).

After each staff POST the 303 redirect carries a fixed result code
(``/demo/staff?r=<code>``, closed allowlist, no data in the URL); the page shows
the mapped line. A line states only what the returned value establishes; any
unlisted value maps to that action's neutral "check" code (never success).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from haloflow_demo.inbound import ReceiveResult
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.web import CSRF_FIELD, NullStaffService, build_staff_app

BASE = "http://127.0.0.1:8080"
LOCATION_RE = re.compile(r"^/demo/staff(\?r=[a-z_]+)?$")

TEXT = {
    "send_submitted": "Reminder submitted (simulated).",
    "send_skipped": "Not sent — this appointment already has a reminder or is not eligible.",
    "send_not_sent": "Not sent — see the appointment's status.",
    "send_rejected": "Rejected by the provider — see the appointment's status.",
    "send_uncertain": "Send outcome uncertain — not re-sent.",
    "send_check": "Send attempted — check the appointment's status.",
    "batch_done": "Batch processed — check each appointment's status.",
    "reply_recorded": "Simulated reply recorded — press Process replies.",
    "reply_duplicate": "Duplicate reply — nothing new recorded.",
    "reply_not_recorded": "Simulated reply not recorded — check settings and Replies.",
    "replies_processed": "Reply processing run — see Replies and Follow-up items.",
    "noreply_checked": "No-reply check run — see Follow-up items.",
    "paused": "Sending paused.",
    "resumed": "Sending resumed.",
    "writeback_done": "Write-back done (simulated).",
    "writeback_conflict": "Write-back stopped — the EHR value changed; staff follow-up created.",
    "writeback_not_applied": "Write-back not applied — see the task.",
    "writeback_uncertain": "Write-back outcome uncertain — use Reconcile.",
    "writeback_busy": "Write-back already running.",
    "writeback_unavailable": "Write-back not possible for this task — staff follow-up created.",
    "writeback_check": "Write-back attempted — check the task's state.",
    "reconcile_done": "Reconciled — see Write-back tasks.",
    "reconcile_uncertain": ("Still uncertain — no new write attempted by this check;"
                            " try again later."),
    "item_done": "Follow-up item marked done.",
    "item_unchanged": "Item was already done or not found.",
}


# ── U layer: a scripted service ───────────────────────────────────────────────


class Scripted(NullStaffService):
    def __init__(self) -> None:
        self.send: dict[str, Any] = {"reminder_id": 1, "state": "SUBMITTED", "reason": None}
        self.reply: Any = ReceiveResult(204, None, 1)
        self.run: Any = "DONE"
        self.reconcile: Any = "DONE"
        self.done: bool = True
        self.raise_value_error = False

    async def send_to_appointment(self, emr_appt_id: str) -> dict[str, Any]:
        return self.send

    async def simulate_reply(self, reminder_id: int, message: str) -> Any:
        return self.reply

    async def run_writeback(self, task_id: int) -> str:
        if self.raise_value_error:
            raise ValueError("state")
        return str(self.run)

    async def reconcile_writeback(self, task_id: int) -> str:
        if self.raise_value_error:
            raise ValueError("state")
        return str(self.reconcile)

    async def mark_item_done(self, item_id: int) -> bool:
        return self.done


def _client(svc: Any) -> tuple[TestClient, str]:
    c = TestClient(build_staff_app(svc, csrf_secret=b"server-secret-0123456789abcdef!!"),
                   base_url=BASE)
    r = c.get("/demo/staff")
    m = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', r.text)
    assert m
    return c, m.group(1)


def _post(c: TestClient, tok: str, path: str, **fields: str) -> httpx.Response:
    return c.post(path, data={CSRF_FIELD: tok, **fields}, follow_redirects=False)


def _code(r: httpx.Response) -> str | None:
    assert r.status_code == 303, r.status_code
    loc = r.headers["location"]
    assert LOCATION_RE.match(loc), loc
    return loc.split("?r=", 1)[1] if "?r=" in loc else None


# T-U6 / T-U8: send mapping ----------------------------------------------------
@pytest.mark.parametrize("state,code", [
    ("SUBMITTED", "send_submitted"), ("SKIPPED", "send_skipped"),
    ("NOT_SENT", "send_not_sent"), ("REJECTED", "send_rejected"),
    ("CLAIMED", "send_uncertain"), ("UNCERTAIN", "send_uncertain"),
    ("SOMETHING_NEW", "send_check")])
def test_t_u6_send_mapping(state: str, code: str) -> None:
    svc = Scripted()
    svc.send = {"reminder_id": 1, "state": state, "reason": "x"}
    c, tok = _client(svc)
    assert _code(_post(c, tok, "/demo/appointments/send", emr_appt_id="a")) == code


# T-U6 / T-U7: simulated reply mapping ----------------------------------------
@pytest.mark.parametrize("result,code", [
    (ReceiveResult(204, None, 1), "reply_recorded"),
    (ReceiveResult(204, "duplicate", 1), "reply_duplicate"),
    (ReceiveResult(204, "ignored_event", None), "reply_not_recorded"),
    (ReceiveResult(400, "numbers_unset", None), "reply_not_recorded"),
    (ReceiveResult(401, "signature", None), "reply_not_recorded"),
    (ReceiveResult(409, "replay_conflict", 1), "reply_not_recorded"),
    (ReceiveResult(413, "too_large", None), "reply_not_recorded"),
    (ReceiveResult(503, "owner_unavailable", None), "reply_not_recorded"),
    (None, "reply_not_recorded")])
def test_t_u7_reply_mapping(result: Any, code: str) -> None:
    svc = Scripted()
    svc.reply = result
    c, tok = _client(svc)
    assert _code(_post(c, tok, "/demo/reply/simulate", reminder_id="1", text="1")) == code


# T-U6 / T-U9: write-back mapping ----------------------------------------------
@pytest.mark.parametrize("value,code", [
    ("DONE", "writeback_done"), ("DONE(reconciled)", "writeback_done"),
    ("writeback_conflict", "writeback_conflict"), ("no_baseline", "writeback_not_applied"),
    ("write_disabled", "writeback_not_applied"), ("NOT_APPLIED", "writeback_not_applied"),
    ("UNCERTAIN", "writeback_uncertain"), ("already_running", "writeback_busy"),
    ("scenario_unavailable", "writeback_unavailable"),
    ("owner_unavailable", "writeback_check"), ("brand_new", "writeback_check")])
def test_t_u9_run_writeback_mapping(value: str, code: str) -> None:
    svc = Scripted()
    svc.run = value
    c, tok = _client(svc)
    assert _code(_post(c, tok, "/demo/tasks/run", task_id="1")) == code


@pytest.mark.parametrize("value,code", [
    ("DONE", "reconcile_done"), ("DONE(reconciled)", "reconcile_done"),
    ("writeback_conflict", "writeback_conflict"), ("NOT_APPLIED", "writeback_not_applied"),
    ("UNCERTAIN", "reconcile_uncertain"), ("scenario_unavailable", "writeback_unavailable"),
    ("owner_unavailable", "writeback_check"), ("brand_new", "writeback_check")])
def test_t_u9_reconcile_mapping(value: str, code: str) -> None:
    svc = Scripted()
    svc.reconcile = value
    c, tok = _client(svc)
    assert _code(_post(c, tok, "/demo/tasks/reconcile", task_id="1")) == code


@pytest.mark.parametrize("done,code", [(True, "item_done"), (False, "item_unchanged")])
def test_t_u6_mark_done_mapping(done: bool, code: str) -> None:
    svc = Scripted()
    svc.done = done
    c, tok = _client(svc)
    assert _code(_post(c, tok, "/demo/items/done", item_id="1")) == code


@pytest.mark.parametrize("path,code", [
    ("/demo/batch/send", "batch_done"), ("/demo/inbound/process", "replies_processed"),
    ("/demo/noresponse/check", "noreply_checked"), ("/demo/pause", "paused"),
    ("/demo/resume", "resumed")])
def test_t_u1_fixed_codes(path: str, code: str) -> None:
    c, tok = _client(Scripted())
    assert _code(_post(c, tok, path)) == code


# T-U2: display ----------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(TEXT))
def test_t_u2_each_code_shows_its_text_once(code: str) -> None:
    c, _ = _client(Scripted())
    html = c.get(f"/demo/staff?r={code}").text
    assert html.count('role="status"') == 1
    assert html.count(TEXT[code].replace("'", "&#x27;")) == 1


@pytest.mark.parametrize("q", ["?r=unknown", "?r=<script>alert(1)</script>", "?r=",
                               "?r=send_submitted%20x", "?x=send_submitted", ""])
def test_t_u2_unknown_or_absent_code_shows_nothing(q: str) -> None:
    c, _ = _client(Scripted())
    html = c.get("/demo/staff" + q).text
    assert 'role="status"' not in html
    assert "<script>alert" not in html and "unknown" not in html


# T-U3 / T-U5: URL hygiene and error paths unchanged ---------------------------
def test_t_u5_error_responses_carry_no_code() -> None:
    svc = Scripted()
    c, tok = _client(svc)
    r = c.post("/demo/pause", data={CSRF_FIELD: "wrong"}, follow_redirects=False)
    assert r.status_code == 403 and "location" not in r.headers
    r = _post(c, tok, "/demo/tasks/run", task_id="x")
    assert r.status_code == 400 and "location" not in r.headers
    svc.raise_value_error = True
    r = _post(c, tok, "/demo/tasks/run", task_id="1")
    assert r.status_code == 409 and "location" not in r.headers
    r = _post(c, tok, "/demo/items/done", item_id="x")
    assert r.status_code == 400 and "location" not in r.headers


# ── D layer: end to end over the database (T-U1, T-U3, T-U4) ───────────────────

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)
E1 = "syn-e1-20261028-a-"


def _env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env, "DEMO_SMS_TEST_NUMBER": "+15550100141",
            "DEMO_SMS_SENDER": "+15550100100",
            "DEMO_CALLBACK_SECRET": "test-callback-secret-0123456789"}


async def _start(env: dict[str, str]) -> tuple[Any, DbStaffService]:
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    return own, DbStaffService(own, s, now=lambda: NOW)


def _aclient(own: Any, svc: DbStaffService) -> httpx.AsyncClient:
    app = build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


async def _tok(c: httpx.AsyncClient) -> str:
    m = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', (await c.get("/demo/staff")).text)
    assert m
    return m.group(1)


async def _apost(c: httpx.AsyncClient, tok: str, path: str, **f: str) -> str | None:
    r = await c.post(path, data={CSRF_FIELD: tok, **f})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert LOCATION_RE.match(loc), loc            # T-U3: no ids or data in the URL
    return loc.split("?r=", 1)[1] if "?r=" in loc else None


def _snapshot(url: str) -> dict[str, list[tuple[Any, ...]]]:
    tables = ("reminder_records", "writeback_tasks", "inbound_messages", "opt_outs",
              "contact_history", "send_quota", "follow_up_items", "demo_settings")
    with psycopg.connect(url, autocommit=True) as conn:
        return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr)
                for t in tables}


async def test_t_u1_end_to_end_over_db(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        async with _aclient(own, svc) as c:
            tok = await _tok(c)
            assert await _apost(c, tok, "/demo/appointments/send",
                                emr_appt_id=E1 + "4") == "send_submitted"
            assert await _apost(c, tok, "/demo/appointments/send",
                                emr_appt_id=E1 + "4") == "send_skipped"     # stale page
            rid = psycopg.connect(demo_db_url).execute(
                "SELECT id FROM reminder_records").fetchone()[0]
            assert await _apost(c, tok, "/demo/reply/simulate", reminder_id=str(rid),
                                text="STOP") == "reply_recorded"
            assert await _apost(c, tok, "/demo/inbound/process") == "replies_processed"
            assert await _apost(c, tok, "/demo/appointments/send",
                                emr_appt_id=E1 + "5") == "send_not_sent"     # opted out
            page = (await c.get("/demo/staff?r=send_not_sent")).text
            assert TEXT["send_not_sent"].replace("'", "&#x27;") in page
    finally:
        await own.close()


async def test_t_u1_mark_done_twice_over_db(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        sent = await svc.send_to_appointment(E1 + "1")
        await svc.simulate_reply(int(sent["reminder_id"]), "2")
        await svc.process_now()
        with psycopg.connect(demo_db_url) as conn:
            item = conn.execute("SELECT id FROM follow_up_items").fetchone()[0]
        async with _aclient(own, svc) as c:
            tok = await _tok(c)
            assert await _apost(c, tok, "/demo/items/done", item_id=str(item)) == "item_done"
            assert await _apost(c, tok, "/demo/items/done",
                                item_id=str(item)) == "item_unchanged"
    finally:
        await own.close()


async def test_t_u4_get_with_code_does_not_mutate(demo_env: dict[str, str],
                                                  demo_db_url: str) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        await svc.send_to_appointment(E1 + "1")
        before = _snapshot(demo_db_url)
        async with _aclient(own, svc) as c:
            for code in ("send_submitted", "item_done", "paused", "unknown"):
                assert (await c.get(f"/demo/staff?r={code}")).status_code == 200
        assert _snapshot(demo_db_url) == before
    finally:
        await own.close()
