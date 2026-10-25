"""Day 4 — controlled send path (package v2 R-9..R-14, T14–T27) + owner decision Q-B4
(exact SMS text) + Codex precision "quota-day revalidation across midnight".

Pipeline per appointment: fenced CLAIM → fenced preconditions + cap reservation
(records the reserved clinic day) → quota-day revalidation immediately before
the call → ONE guarded provider attempt → outcome committed immediately.
Reservations are never refunded. UNCERTAIN is never re-sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.selection import Eligible
from haloflow_demo.sending import (
    NotifyreGateway,
    SendOutcome,
    StubGateway,
    classify_exception,
    classify_response,
    first_name_of,
    reminder_text,
    send_batch,
    send_one,
)
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url
from haloflow_demo.synthetic_ehr import ApptStatus, SyntheticAppointment, SyntheticPatient

TEST_NUMBER = "+15550100199"  # fictional 555-01xx range
OTHER_NUMBER = "+15550100142"
TENANT = "demo-clinic"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # 09:00 EDT, clinic day 2026-10-26


def _patient(i: int = 1) -> SyntheticPatient:
    return SyntheticPatient(f"syn-p-{i}", f"Synthetic Patient {i}", phone_alias="alpha")


def _appt(i: int = 1) -> SyntheticAppointment:
    return SyntheticAppointment(f"syn-a-{i}", f"syn-p-{i}",
                                datetime(2026, 10, 28, 13, 0, tzinfo=UTC), ApptStatus.SCHEDULED)


def _eligible(n: int) -> list[Eligible]:
    return [Eligible(_appt(i), _patient(i)) for i in range(1, n + 1)]


async def _own(env: dict[str, str]):  # type: ignore[no-untyped-def]
    s = DemoSettings.from_env(env)
    return s, await acquire_ownership(s, validate_demo_database_url(s))


def _env(demo_env: dict[str, str], **extra: str) -> dict[str, str]:
    return {**demo_env, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER, **extra}


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _clock(start: datetime) -> Callable[[], datetime]:
    return lambda: start


# ── Q-B4 exact text ───────────────────────────────────────────────────────────


def test_qb4_exact_reminder_text() -> None:
    text = reminder_text("Patient A", datetime(2026, 10, 28, 13, 0, tzinfo=UTC), "America/New_York")
    assert text == (
        "Hi Patient A, this is HaloVox Demo Clinic. You have an appointment on "
        "Wednesday, Oct 28 at 9:00 AM. Reply 1 to confirm, 2 to reschedule, or 3 to cancel. "
        "Reply STOP to opt out."
    )


def test_qb4_time_in_clinic_zone_after_dst() -> None:
    # 2026-11-02 14:00Z is 09:00 EST (after the 11-01 DST end).
    text = reminder_text("Patient B", datetime(2026, 11, 2, 14, 0, tzinfo=UTC), "America/New_York")
    assert "on Monday, Nov 02 at 9:00 AM." in text
    assert "EST" not in text and "EDT" not in text


def test_first_name_of_synthetic_patient() -> None:
    assert first_name_of(SyntheticPatient("p", "Synthetic Patient A", "x")) == "Patient A"


# ── real mode gated (Notifyre send contract X7 not yet verified) ──────────────


def test_real_sms_mode_refused_until_contract_verified() -> None:
    from haloflow_demo.settings import validate_startup

    s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@127.0.0.1/haloflow_x_demo",
                               "DEMO_SMS_MODE": "real"})
    with pytest.raises(StartupRefused) as exc:
        validate_startup(s)
    assert exc.value.reason == "sms_real_unverified"


# ── T22/T23: one attempt, classification (U, MockTransport) ───────────────────


def _mock(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[NotifyreGateway, list[int]]:
    seen: list[int] = []

    def _h(req: httpx.Request) -> httpx.Response:
        seen.append(1)
        return handler(req)

    gw = NotifyreGateway(api_key="k", sender="+15550100100", test_number=TEST_NUMBER,
                         transport=httpx.MockTransport(_h))
    return gw, seen


def _raise(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def _h(req: httpx.Request) -> httpx.Response:
        raise exc
    return _h


@pytest.mark.parametrize(
    ("handler", "state", "reason"),
    [
        (_raise(httpx.ConnectError("x")), "NOT_SENT", "connect"),
        (_raise(httpx.ConnectTimeout("x")), "NOT_SENT", "connect"),
        (_raise(httpx.ReadTimeout("x")), "UNCERTAIN", "timeout"),
        (_raise(httpx.WriteTimeout("x")), "UNCERTAIN", "timeout"),
        (_raise(httpx.RemoteProtocolError("x")), "UNCERTAIN", "transport"),
        (lambda r: httpx.Response(302, headers={"location": "https://x"}), "UNCERTAIN", "3xx"),
        (lambda r: httpx.Response(500), "UNCERTAIN", "5xx"),
        (lambda r: httpx.Response(400, json={"success": False, "message": "bad"}),
         "REJECTED", "4xx"),
        (lambda r: httpx.Response(400, text="<html>bad</html>"), "UNCERTAIN", "malformed_4xx"),
        (lambda r: httpx.Response(200, json={"payload": {}}), "UNCERTAIN", "incomplete_2xx"),
        (lambda r: httpx.Response(200, text="not json"), "UNCERTAIN", "incomplete_2xx"),
        (lambda r: httpx.Response(200, json={"payload": {"smsMessageID": "m-1"}}),
         "SUBMITTED", None),
    ],
    ids=["connect", "connect_timeout", "read_timeout", "write_timeout", "transport", "302",
         "500", "400json", "400html", "200_no_id", "200_non_json", "200_ok"],
)
async def test_t22_classification_with_exactly_one_request(
    handler: Callable[[httpx.Request], httpx.Response], state: str, reason: str | None
) -> None:
    gw, seen = _mock(handler)
    out = await gw.send(TEST_NUMBER, "body")
    assert (out.state, out.reason) == (state, reason)
    assert len(seen) == 1, "exactly one request, never a retry"
    if state == "SUBMITTED":
        assert out.provider_message_id == "m-1"


async def test_t22_request_shape() -> None:
    captured: list[httpx.Request] = []

    def _h(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"payload": {"smsMessageID": "m-1"}})

    gw = NotifyreGateway(api_key="secret-token", sender="+15550100100", test_number=TEST_NUMBER,
                         transport=httpx.MockTransport(_h))
    await gw.send(TEST_NUMBER, "hello")
    req = captured[0]
    assert req.method == "POST" and req.url.path == "/sms/send"
    assert req.headers["x-api-token"] == "secret-token"
    import json
    body = json.loads(req.content)
    assert body["recipients"] == [{"type": "mobile_number", "value": TEST_NUMBER}]
    assert body["body"] == "hello" and body["from"] == "+15550100100"


def test_t23_client_never_follows_redirects_and_never_retries() -> None:
    gw = NotifyreGateway(api_key="k", sender="+15550100100", test_number=TEST_NUMBER)
    assert gw.client.follow_redirects is False
    assert gw.transport_retries == 0


async def test_t14_gateway_refuses_any_other_recipient_without_a_request() -> None:
    gw, seen = _mock(lambda r: httpx.Response(200, json={"payload": {"smsMessageID": "m"}}))
    out = await gw.send(OTHER_NUMBER, "body")
    assert (out.state, out.reason) == ("NOT_SENT", "not_test_number")
    assert seen == []
    stub = StubGateway(TEST_NUMBER)
    out2 = await stub.send(OTHER_NUMBER, "body")
    assert (out2.state, out2.reason) == ("NOT_SENT", "not_test_number")
    assert stub.calls == []


def test_classify_helpers_are_total() -> None:
    assert classify_exception(RuntimeError("x")) == SendOutcome("UNCERTAIN", "transport", None)
    assert classify_response(httpx.Response(101)).state == "UNCERTAIN"


# ── D-layer send pipeline ─────────────────────────────────────────────────────


async def test_t14_missing_or_invalid_test_number_sends_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    for value in ("", "abc"):
        s, own = await _own({**demo_env, "DEMO_SMS_TEST_NUMBER": value})
        gw = StubGateway(None)
        try:
            r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT,
                               now=_clock(NOW))
        finally:
            await own.close()
        assert (r.state, r.reason) == ("NOT_SENT", "no_test_number")
        assert gw.calls == []
        _q(demo_db_url, "DELETE FROM reminder_records")
    assert _q(demo_db_url, "SELECT count(*) FROM send_quota") == [(0,)]


async def test_submitted_path_persists_provider_id_and_text(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert r.state == "SUBMITTED"
    assert gw.calls == [(TEST_NUMBER, reminder_text("Patient 1", _appt(1).start_utc,
                                                    "America/New_York"))]
    assert _q(demo_db_url, "SELECT state, provider_message_id, reserved_day, mode"
                           " FROM reminder_records") == [
        ("SUBMITTED", "stub-1", date(2026, 10, 26), "stub")]
    assert _q(demo_db_url, "SELECT clinic_day, used FROM send_quota") == [(date(2026, 10, 26), 1)]


async def test_t15_paused_and_opted_out_send_nothing_and_reserve_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.sending import record_opt_out

    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    try:
        await _set_paused(own, True)
        r1 = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW))
        await _set_paused(own, False)
        await record_opt_out(own, TEST_NUMBER)
        r2 = await send_one(own, s, _appt(2), _patient(2), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert (r1.state, r1.reason) == ("NOT_SENT", "paused")
    assert (r2.state, r2.reason) == ("NOT_SENT", "opted_out")
    assert gw.calls == []
    assert _q(demo_db_url, "SELECT count(*) FROM send_quota") == [(0,)]
    # opt-out identity is not the raw number
    rows = _q(demo_db_url, "SELECT phone_key FROM opt_outs")
    assert len(rows) == 1 and "5550100199" not in rows[0][0]


async def _set_paused(own: Any, paused: bool) -> None:
    from sqlalchemy import text

    async with own.fenced() as conn:
        await conn.execute(text("UPDATE demo_settings SET paused = :p WHERE id = 1"), {"p": paused})


async def test_t17_cap_ten_with_mixed_outcomes_no_refunds(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    script: list[SendOutcome | Exception] = [
        SendOutcome("SUBMITTED", None, None)] * 7 + [
        SendOutcome("UNCERTAIN", "timeout", None), SendOutcome("UNCERTAIN", "5xx", None),
        SendOutcome("NOT_SENT", "connect", None)]
    gw = StubGateway(TEST_NUMBER, script=list(script))
    try:
        results = await send_batch(own, s, _eligible(11), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert [r.reason for r in results][-1] == "daily_cap"
    assert results[-1].state == "NOT_SENT"
    assert len(gw.calls) == 10
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(10,)]


async def test_t18_concurrent_attempts_respect_cap(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    _q(demo_db_url, "INSERT INTO send_quota(clinic_day, used) VALUES ('2026-10-26', 8)")
    gw = StubGateway(TEST_NUMBER)
    try:
        results = await asyncio.gather(*(
            send_one(own, s, _appt(i), _patient(i), gw, tenant_id=TENANT, now=_clock(NOW))
            for i in range(1, 16)))
    finally:
        await own.close()
    assert sum(1 for r in results if r.state == "SUBMITTED") == 2
    assert sum(1 for r in results if r.reason == "daily_cap") == 13
    assert len(gw.calls) == 2
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(10,)]


async def test_t19_counter_unavailable_sends_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    _q(demo_db_url, "ALTER TABLE send_quota RENAME TO send_quota_broken")  # test-harness fault
    gw = StubGateway(TEST_NUMBER)
    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert (r.state, r.reason) == ("NOT_SENT", "counter_unavailable")
    assert gw.calls == []


async def test_t20_counter_keyed_on_clinic_day_and_survives_restart(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    # 23:59 EDT on 10-31 and 00:01 EDT on 11-01, then 00:30 EST after the
    # 01:00 fall-back is still clinic day 11-01.
    t1 = datetime(2026, 11, 1, 3, 59, tzinfo=UTC)   # 2026-10-31 23:59 EDT
    t2 = datetime(2026, 11, 1, 4, 1, tzinfo=UTC)    # 2026-11-01 00:01 EDT
    t3 = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)   # 2026-11-01 01:30 EST (second 01:30)
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    try:
        await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(t1))
        await send_one(own, s, _appt(2), _patient(2), gw, tenant_id=TENANT, now=_clock(t2))
    finally:
        await own.close()
    s, own = await _own(_env(demo_env))  # restart
    try:
        await send_one(own, s, _appt(3), _patient(3), gw, tenant_id=TENANT, now=_clock(t3))
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT clinic_day, used FROM send_quota ORDER BY clinic_day") == [
        (date(2026, 10, 31), 1), (date(2026, 11, 1), 2)]


async def test_qd_reservation_from_previous_clinic_day_is_not_used(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    """Codex precision: a dispatch reserved before midnight and sent after it must
    not debit the old day silently; it is refused with no call (no refund)."""
    before = datetime(2026, 10, 27, 3, 59, 59, tzinfo=UTC)  # 10-26 23:59:59 EDT
    after = datetime(2026, 10, 27, 4, 0, 1, tzinfo=UTC)     # 10-27 00:00:01 EDT
    times = iter([before, after])
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT,
                           now=lambda: next(times))
    finally:
        await own.close()
    assert (r.state, r.reason) == ("NOT_SENT", "quota_day_expired")
    assert gw.calls == []
    assert _q(demo_db_url, "SELECT clinic_day, used FROM send_quota") == [(date(2026, 10, 26), 1)]


async def test_t21_concurrent_claims_one_gateway_call(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    try:
        results = await asyncio.gather(*(
            send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW))
            for _ in range(5)))
    finally:
        await own.close()
    assert len(gw.calls) == 1
    assert sorted(r.state for r in results).count("SUBMITTED") == 1
    assert sum(1 for r in results if r.reason == "already_claimed") == 4
    assert _q(demo_db_url, "SELECT count(*) FROM reminder_records") == [(1,)]
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(1,)]


async def test_t24_crash_after_claim_before_call_then_restart(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    class Crash(Exception):
        pass

    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)

    def _boom() -> None:
        raise Crash()

    try:
        with pytest.raises(Crash):
            await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT,
                           now=_clock(NOW), before_call=_boom)
    finally:
        await own.close()
    s, own = await _own(_env(demo_env))
    await own.close()
    assert gw.calls == []
    assert _q(demo_db_url, "SELECT state, reason FROM reminder_records") == [
        ("UNCERTAIN", "restart_after_claim")]


async def test_t25_batch_commits_each_outcome_independently(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER, script=[
        SendOutcome("SUBMITTED", None, None), SendOutcome("UNCERTAIN", "timeout", None),
        SendOutcome("SUBMITTED", None, None)])
    try:
        await send_batch(own, s, _eligible(3), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT emr_appt_id, state, reason FROM reminder_records"
                           " ORDER BY emr_appt_id") == [
        ("syn-a-1", "SUBMITTED", None), ("syn-a-2", "UNCERTAIN", "timeout"),
        ("syn-a-3", "SUBMITTED", None)]


async def test_uncertain_and_submitted_are_never_resent(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER, script=[SendOutcome("UNCERTAIN", "timeout", None),
                                          SendOutcome("SUBMITTED", None, None)])
    try:
        await send_batch(own, s, _eligible(2), gw, tenant_id=TENANT, now=_clock(NOW))
        again = await send_batch(own, s, _eligible(2), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert len(gw.calls) == 2
    assert {r.reason for r in again} == {"already_claimed"}
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(2,)]


async def test_t27_pause_stops_new_sends_and_keeps_existing_states(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)
    try:
        await send_batch(own, s, _eligible(1), gw, tenant_id=TENANT, now=_clock(NOW))
        await _set_paused(own, True)
        results = await send_batch(own, s, _eligible(3)[1:], gw, tenant_id=TENANT,
                                   now=_clock(NOW))
    finally:
        await own.close()
    assert len(gw.calls) == 1
    assert {(r.state, r.reason) for r in results} == {("NOT_SENT", "paused")}
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(1,)]
    assert ("syn-a-1", "SUBMITTED") in _q(demo_db_url,
                                          "SELECT emr_appt_id, state FROM reminder_records")


async def test_owner_lost_before_call_makes_no_call(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)

    def _lose() -> None:
        own.begin_closing()

    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT,
                           now=_clock(NOW), before_call=_lose)
    finally:
        await own.close()
    assert r.state == "CLAIMED" and r.reason == "owner_unavailable"
    assert gw.calls == []
    # stays CLAIMED until restart recovery marks it UNCERTAIN (never re-sent blindly)
    assert _q(demo_db_url, "SELECT state FROM reminder_records") == [("CLAIMED",)]


# ── T26 + staff action ────────────────────────────────────────────────────────


async def test_t26_staff_send_action_and_submitted_label(demo_env: dict[str, str]) -> None:
    import re

    from haloflow_demo.service import DbStaffService
    from haloflow_demo.web import CSRF_FIELD, build_staff_app

    s, own = await _own(_env(demo_env))
    try:
        svc = DbStaffService(own, s, now=_clock(datetime(2026, 10, 26, 13, 0, tzinfo=UTC)),
                             gateway=StubGateway(TEST_NUMBER))
        app = build_staff_app(svc, csrf_secret=b"k" * 32)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8080") as c:
            page = await c.get("/demo/staff")
            tok = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page.text)
            assert tok
            r = await c.post("/demo/batch/send", data={CSRF_FIELD: tok.group(1)},
                             follow_redirects=True)
            assert r.status_code == 200
            assert "Submitted" in r.text
            assert "Delivered" not in r.text
            assert "SIMULATED" in r.text
    finally:
        await own.close()


# ── Codex Day-4 review P1: the reservation-to-dispatch gap ────────────────────
# The tracked external task may start after the outer checks. Day, pause and
# opt-out are re-checked inside the tracked callable, immediately before the
# provider request. The reservation stays consumed (no refund).


class _MutableClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


async def test_p1_midnight_between_outer_check_and_child_start(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    clock = _MutableClock(datetime(2026, 10, 27, 3, 59, 59, tzinfo=UTC))  # 10-26 23:59:59 EDT
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)

    def _advance() -> None:  # runs after the outer checks, before the child starts
        clock.t = datetime(2026, 10, 27, 4, 0, 1, tzinfo=UTC)  # 10-27 00:00:01 EDT

    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=clock,
                           before_call=_advance)
    finally:
        await own.close()
    assert (r.state, r.reason) == ("NOT_SENT", "quota_day_expired")
    assert gw.calls == []
    assert _q(demo_db_url, "SELECT state, reason FROM reminder_records") == [
        ("NOT_SENT", "quota_day_expired")]
    assert _q(demo_db_url, "SELECT clinic_day, used FROM send_quota") == [(date(2026, 10, 26), 1)]


async def test_p1_pause_in_the_gap_sends_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)

    def _pause() -> None:
        _q(demo_db_url, "UPDATE demo_settings SET paused = true WHERE id = 1")

    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW),
                           before_call=_pause)
    finally:
        await own.close()
    assert (r.state, r.reason) == ("NOT_SENT", "paused")
    assert gw.calls == []
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(1,)]  # reservation kept


async def test_p1_opt_out_in_the_gap_sends_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.sending import phone_key

    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER)

    def _opt_out() -> None:
        secret = _q(demo_db_url, "SELECT value FROM demo_meta WHERE key='phone_key_secret'")[0][0]
        _q(demo_db_url, "INSERT INTO opt_outs(phone_key) VALUES (%s)",
           (phone_key(secret, TEST_NUMBER),))

    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW),
                           before_call=_opt_out)
    finally:
        await own.close()
    assert (r.state, r.reason) == ("NOT_SENT", "opted_out")
    assert gw.calls == []
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(1,)]


async def test_p1_dispatch_check_runs_inside_the_tracked_task(
    demo_env: dict[str, str],
) -> None:
    """The last checks and the request run in the same tracked task (no gap)."""
    s, own = await _own(_env(demo_env))
    seen: list[int] = []

    class _Probe(StubGateway):
        async def send(self, to: str, body: str) -> SendOutcome:
            seen.append(own.inflight)
            return await super().send(to, body)

    gw = _Probe(TEST_NUMBER)
    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert r.state == "SUBMITTED"
    assert seen == [1], "the provider request ran inside the tracked external task"
