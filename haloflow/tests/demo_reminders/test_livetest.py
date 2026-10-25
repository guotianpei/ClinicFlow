"""G: the outbound-once live SMS test command (Codex ALIGN 20261025T182721Z).

``python -m haloflow_demo live-sms-test`` is a separate entry point:
- arming (DEMO_LIVE_TEST=outbound-once) and local input checks happen before
  any ownership, recovery or database mutation;
- full ownership (process lock, DB advisory lock, epoch, recovery) is taken;
- a durable ``live_test_unresolved`` gate holds across processes, recovery and
  reset;
- one clinic-local synthetic appointment with a per-epoch qualified ID is
  sent through the existing ``send_one`` pipeline with a NotifyreGateway
  (real provenance): durable atomic reservation, no refund, one attempt;
- the output is a fixed enum, a boolean, and recognised documented key
  spellings only.

Every request here goes to an httpx.MockTransport: no network is used, and
nothing here proves the live Notifyre contract. All numbers are fictional
555-01xx; the token is a synthetic marker.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.livetest import live_sms_test

from .harness import demo_subprocess_env

TEST_NUMBER = "+15550100199"
SENDER = "+15550100100"
TOKEN = "tok-SYNTHETIC-MARKER-7f3a9c"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)   # 09:00 EDT, target date 2026-10-28
HELPER = Path(__file__).parent / "helpers" / "livetest_proc.py"
NUMBER_VARIANTS = ("+15550100199", "5550100199", "555-010-0199", "(555) 010-0199",
                   "555 010 0199", "555.010.0199", "+1 (555) 010-0199", "0100199")


def _env(demo_env: dict[str, str], **extra: str) -> dict[str, str]:
    base = {**demo_env, "DEMO_LIVE_TEST": "outbound-once", "NOTIFYRE_API_KEY": TOKEN,
            "DEMO_SMS_SENDER": SENDER, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER}
    base.update(extra)
    return {k: v for k, v in base.items() if v != "<unset>"}


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _tables(url: str) -> int:
    return int(_q(url, "SELECT count(*) FROM information_schema.tables"
                       " WHERE table_schema = 'public'")[0][0])


class Recorder:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def transport(self) -> httpx.MockTransport:
        def _h(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return self._respond(req)
        return httpx.MockTransport(_h)


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "payload": {"smsMessageID": "m-1",
                                                                   "invalidToNumbers": []}})


async def _run(env: dict[str, str], rec: Recorder | None = None,
               now: datetime = NOW) -> int:
    return await live_sms_test(env, transport=(rec or Recorder(_ok)).transport(),
                               now=lambda: now)


def _assert_clean(text: str) -> None:
    for marker in (TOKEN, SENDER, "Reply 1 to confirm", "HaloVox", "m-1", *NUMBER_VARIANTS):
        assert marker not in text, f"output leaked {marker!r}"


# ── arming and local inputs: refused before any ownership or mutation ─────────


@pytest.mark.parametrize(("extra", "reason"), [
    ({"DEMO_LIVE_TEST": "<unset>"}, "live_test_not_armed"),
    ({"DEMO_LIVE_TEST": "yes"}, "live_test_not_armed"),
    ({"NOTIFYRE_API_KEY": "<unset>"}, "live_test_inputs"),
    ({"NOTIFYRE_API_KEY": "   "}, "live_test_inputs"),
    ({"NOTIFYRE_API_KEY": "has space inside"}, "live_test_inputs"),
    ({"DEMO_SMS_SENDER": "<unset>"}, "live_test_inputs"),
    ({"DEMO_SMS_SENDER": "abc"}, "live_test_inputs"),
    ({"DEMO_SMS_TEST_NUMBER": "<unset>"}, "live_test_inputs"),
    ({"DEMO_SMS_TEST_NUMBER": "12"}, "live_test_inputs"),
])
async def test_lt1_lt2_refused_before_ownership(
    demo_env: dict[str, str], demo_db_url: str, capsys: pytest.CaptureFixture[str],
    extra: dict[str, str], reason: str
) -> None:
    rec = Recorder(_ok)
    code = await _run(_env(demo_env, **extra), rec)
    out = capsys.readouterr()
    assert code == 2
    assert f"refused={reason}" in out.out
    assert rec.requests == []
    assert _tables(demo_db_url) == 0, "no ownership, bootstrap or recovery happened"
    _assert_clean(out.out + out.err)


async def test_lt3_refused_while_process_lock_held(
    demo_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    from haloflow_demo.process_lock import ProcessLock

    lock = ProcessLock()
    lock.acquire()
    rec = Recorder(_ok)
    try:
        code = await _run(_env(demo_env), rec)
    finally:
        lock.release()
    assert code == 2 and "refused=process_lock_held" in capsys.readouterr().out
    assert rec.requests == []


async def test_lt3_refused_while_db_lock_held(
    demo_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    from haloflow_demo.ownership import DEMO_ADVISORY_KEY

    holder = psycopg.connect(demo_env["DEMO_DATABASE_URL"], autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (DEMO_ADVISORY_KEY,))
    rec = Recorder(_ok)
    try:
        code = await _run(_env(demo_env), rec)
    finally:
        holder.close()
    assert code == 2 and "refused=db_lock_held" in capsys.readouterr().out
    assert rec.requests == []


# ── happy path: exact request, durable reservation, real provenance ───────────


async def test_lt4_one_request_exact_shape_and_durable_state(
    demo_env: dict[str, str], demo_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from haloflow_demo.sending import reminder_text

    rec = Recorder(_ok)
    code = await _run(_env(demo_env), rec)
    out = capsys.readouterr()
    assert code == 0
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path == "/sms/send"
    assert req.headers["x-api-token"] == TOKEN
    start = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)   # 09:00 EDT on the target date
    assert json.loads(req.content) == {
        "Body": reminder_text("Demo", start, "America/New_York"),
        "From": SENDER,
        "Recipients": [{"type": "mobile_number", "value": TEST_NUMBER}],
    }
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(1,)]
    assert _q(demo_db_url, "SELECT provenance, outcome FROM contact_history") == [
        ("real", "SUBMITTED")]
    rows = _q(demo_db_url, "SELECT emr_appt_id, state, mode, appt_start FROM reminder_records")
    assert len(rows) == 1
    appt_id, state, mode, appt_start = rows[0]
    assert re.fullmatch(r"syn-e\d+-20261028-live-1", appt_id)
    assert (state, mode, appt_start) == ("SUBMITTED", "real", start)
    assert "state=SUBMITTED" in out.out and "provider_id_present=yes" in out.out
    assert "response_keys=success,payload,smsMessageID,invalidToNumbers" in out.out
    _assert_clean(out.out + out.err)


@pytest.mark.parametrize(("now", "target_text", "start"), [
    # 10:00 EDT on 10-30 → target 11-01, 09:00 EST after the DST end
    (datetime(2026, 10, 30, 14, 0, tzinfo=UTC), "Sunday, Nov 01 at 9:00 AM",
     datetime(2026, 11, 1, 14, 0, tzinfo=UTC)),
    # 23:59 EDT on 10-30 → still clinic day 10-30 → target 11-01
    (datetime(2026, 10, 31, 3, 59, tzinfo=UTC), "Sunday, Nov 01 at 9:00 AM",
     datetime(2026, 11, 1, 14, 0, tzinfo=UTC)),
    # 00:01 EDT on 10-31 → target 11-02
    (datetime(2026, 10, 31, 4, 1, tzinfo=UTC), "Monday, Nov 02 at 9:00 AM",
     datetime(2026, 11, 2, 14, 0, tzinfo=UTC)),
])
async def test_lt4_clinic_local_target_across_midnight_and_dst(
    demo_env: dict[str, str], demo_db_url: str, now: datetime, target_text: str,
    start: datetime
) -> None:
    rec = Recorder(_ok)
    assert await _run(_env(demo_env), rec, now=now) == 0
    assert target_text in json.loads(rec.requests[0].content)["Body"]
    assert _q(demo_db_url, "SELECT appt_start FROM reminder_records") == [(start,)]


async def test_lt4_two_explicit_successes_keep_distinct_identity_and_history(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    rec = Recorder(_ok)
    assert await _run(_env(demo_env), rec) == 0
    assert await _run(_env(demo_env), rec) == 0
    ids = [r[0] for r in _q(demo_db_url, "SELECT emr_appt_id FROM reminder_records ORDER BY id")]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(2,)]
    assert _q(demo_db_url, "SELECT provenance, outcome FROM contact_history ORDER BY 1, 2") == [
        ("real", "SUBMITTED"), ("real", "SUBMITTED")]
    assert len(rec.requests) == 2


# ── conservative classification; one request; reservation kept ───────────────


@pytest.mark.parametrize(("respond", "state", "reason"), [
    (lambda r: httpx.Response(200, text="{not json"), "UNCERTAIN", "incomplete_2xx"),
    (lambda r: httpx.Response(200, json={"success": True, "payload": {}}),
     "UNCERTAIN", "incomplete_2xx"),
    (lambda r: httpx.Response(200, json={"success": True, "payload": {
        "smsMessageID": "m", "invalidToNumbers": [{"number": "+15550100142"}]}}),
     "UNCERTAIN", "unexpected_invalid_to"),
    (lambda r: httpx.Response(302, headers={"location": "https://x.invalid"}),
     "UNCERTAIN", "3xx"),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t")), "UNCERTAIN", "timeout"),
    (lambda r: httpx.Response(200, json={"success": True, "payload": {
        "smsMessageID": "m", "invalidToNumbers": [{"number": TEST_NUMBER}]}}),
     "REJECTED", "invalid_to"),
])
async def test_lt5_lt10_conservative_outcomes_one_request_no_refund(
    demo_env: dict[str, str], demo_db_url: str, capsys: pytest.CaptureFixture[str],
    respond: Callable[[httpx.Request], httpx.Response], state: str, reason: str
) -> None:
    rec = Recorder(respond)
    code = await _run(_env(demo_env), rec)
    out = capsys.readouterr()
    assert code == 1
    assert len(rec.requests) == 1, "one attempt, never a retry"
    assert f"state={state}" in out.out and f"reason={reason}" in out.out
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(1,)], "no refund"
    _assert_clean(out.out + out.err)


async def test_lt5_cap_reached_sends_nothing(demo_env: dict[str, str], demo_db_url: str) -> None:
    rec = Recorder(_ok)
    from haloflow_demo.ownership import acquire_ownership
    from haloflow_demo.settings import DemoSettings, validate_demo_database_url

    s = DemoSettings.from_env(demo_env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    await own.close()
    _q(demo_db_url, "INSERT INTO send_quota(clinic_day, used) VALUES ('2026-10-26', 10)")
    code = await _run(_env(demo_env), rec)
    assert code == 1 and rec.requests == []
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(10,)]
    assert _q(demo_db_url, "SELECT state, reason FROM reminder_records") == [
        ("NOT_SENT", "daily_cap")]


@pytest.mark.parametrize("which", ["opted_out", "paused"])
async def test_lt6_lt7_suppression_sends_nothing(
    demo_env: dict[str, str], demo_db_url: str, which: str
) -> None:
    from haloflow_demo.ownership import acquire_ownership
    from haloflow_demo.sending import record_opt_out
    from haloflow_demo.settings import DemoSettings, validate_demo_database_url
    from sqlalchemy import text

    s = DemoSettings.from_env(demo_env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    try:
        if which == "opted_out":
            await record_opt_out(own, TEST_NUMBER)
        else:
            async with own.fenced(exclusive=True) as conn:
                await conn.execute(text("UPDATE demo_settings SET paused = true WHERE id = 1"))
    finally:
        await own.close()
    rec = Recorder(_ok)
    assert await _run(_env(demo_env), rec) == 1
    assert rec.requests == []
    assert _q(demo_db_url, "SELECT state, reason FROM reminder_records") == [("NOT_SENT", which)]
    assert _q(demo_db_url, "SELECT count(*) FROM send_quota") == [(0,)]


# ── durable unresolved gate ───────────────────────────────────────────────────


async def test_unresolved_after_timeout_refuses_fresh_runs_even_after_reset(
    demo_env: dict[str, str], demo_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from haloflow_demo.reset import reset_synthetic
    from haloflow_demo.settings import DemoSettings

    first = Recorder(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t")))
    assert await _run(_env(demo_env), first) == 1
    capsys.readouterr()
    second = Recorder(_ok)
    assert await _run(_env(demo_env), second, now=datetime(2026, 10, 27, 13, tzinfo=UTC)) == 2
    assert "refused=live_test_unresolved" in capsys.readouterr().out
    await reset_synthetic(DemoSettings.from_env(demo_env))
    assert await _run(_env(demo_env), second) == 2
    assert second.requests == []
    assert _q(demo_db_url, "SELECT sum(used) FROM send_quota") == [(1,)]
    assert _q(demo_db_url, "SELECT count(*) FROM reminder_records") == [(0,)]


async def test_unresolved_after_recovered_claimed_real_contact(
    demo_env: dict[str, str], demo_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from haloflow_demo.ownership import acquire_ownership
    from haloflow_demo.settings import DemoSettings, validate_demo_database_url

    s = DemoSettings.from_env(demo_env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    await own.close()
    # Harness: the state a crash leaves after real contact admission (CLAIMED, outcome NULL).
    _q(demo_db_url, "INSERT INTO reminder_records(tenant_id, emr_appt_id, emr_patient_id,"
                    " appt_start, state, mode, contact_seq) VALUES ('demo-clinic', 'syn-x',"
                    " 'syn-p', now() + interval '2 days', 'CLAIMED', 'real', 900001)")
    _q(demo_db_url, "INSERT INTO contact_history(contact_seq, phone_key, reminder_id, appt_start,"
                    " provenance) SELECT 900001, 'k', id, appt_start, 'real'"
                    " FROM reminder_records")
    rec = Recorder(_ok)
    assert await _run(_env(demo_env), rec) == 2
    assert "refused=live_test_unresolved" in capsys.readouterr().out
    assert rec.requests == []
    assert _q(demo_db_url, "SELECT outcome FROM contact_history") == [("UNCERTAIN",)]


async def test_simulated_uncertain_history_does_not_trigger_the_real_gate(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.ownership import acquire_ownership
    from haloflow_demo.sending import SendOutcome, StubGateway, send_one
    from haloflow_demo.settings import DemoSettings, validate_demo_database_url
    from haloflow_demo.synthetic_ehr import ApptStatus, SyntheticAppointment, SyntheticPatient

    s = DemoSettings.from_env(_env(demo_env))
    own = await acquire_ownership(s, validate_demo_database_url(s))
    try:
        await send_one(own, s, SyntheticAppointment("syn-s", "syn-p",
                                                    datetime(2026, 10, 28, 13, tzinfo=UTC),
                                                    ApptStatus.SCHEDULED),
                       SyntheticPatient("syn-p", "Synthetic S", "a"),
                       StubGateway(TEST_NUMBER, script=[SendOutcome("UNCERTAIN", "timeout",
                                                                    None)]),
                       tenant_id="demo-clinic", now=lambda: NOW)
    finally:
        await own.close()
    rec = Recorder(_ok)
    assert await _run(_env(demo_env), rec) == 0
    assert len(rec.requests) == 1


# ── output hygiene: fixed enum, recognised key spellings only ─────────────────


async def test_output_never_echoes_unknown_keys_values_or_exception_text(
    demo_env: dict[str, str], demo_db_url: str, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture
) -> None:
    malicious = Recorder(lambda r: httpx.Response(200, json={
        "success": True, "SECRET_5550100199": TOKEN,
        "payload": {"smsMessageID": "m-1", "note (555) 010-0199": "x", "invalidToNumbers": []}}))
    with caplog.at_level("DEBUG"):
        assert await _run(_env(demo_env), malicious) == 0
    first = capsys.readouterr()
    assert "response_keys=success,payload,smsMessageID,invalidToNumbers" in first.out

    def boom(req: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"provider said {TOKEN} for +1 (555) 010-0199")

    with caplog.at_level("DEBUG"):
        code = await _run(_env(demo_env), Recorder(boom), now=datetime(2026, 10, 27, 13,
                                                                       tzinfo=UTC))
    second = capsys.readouterr()
    assert code == 1 and "reason=transport" in second.out
    logs = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    _assert_clean(first.out + first.err + second.out + second.err + logs)
    assert "SECRET" not in first.out + second.out and "note" not in first.out


# ── never a server: no listeners, no dispatcher ───────────────────────────────


async def test_lt8_no_listener_or_dispatcher_is_ever_constructed(
    demo_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import haloflow_demo.service as service_mod
    import haloflow_demo.web as web_mod
    import uvicorn

    called: list[str] = []

    def forbid(name: str) -> Callable[..., Any]:
        def _f(*a: Any, **k: Any) -> Any:
            called.append(name)
            raise AssertionError(f"{name} must not be used by live-sms-test")
        return _f

    monkeypatch.setattr(uvicorn.Server, "__init__", forbid("uvicorn.Server"))
    monkeypatch.setattr(uvicorn.Config, "__init__", forbid("uvicorn.Config"))
    monkeypatch.setattr(web_mod, "build_staff_app", forbid("build_staff_app"))
    monkeypatch.setattr(web_mod, "build_callback_app", forbid("build_callback_app"))
    monkeypatch.setattr(service_mod.DbStaffService, "dispatcher_loop", forbid("dispatcher"))
    binds: list[object] = []
    real_bind = socket.socket.bind

    def spy_bind(self: socket.socket, addr: object) -> None:
        binds.append(addr)
        real_bind(self, addr)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "bind", spy_bind)
    assert await _run(_env(demo_env)) == 0
    assert called == [] and binds == []


def test_production_entry_point_has_no_transport_or_message_override() -> None:
    import inspect

    import haloflow_demo.__main__ as main_mod

    src = inspect.getsource(main_mod)
    assert "live-sms-test" in src
    assert "transport" not in src and "MockTransport" not in src


def test_real_server_modes_still_refused() -> None:
    from haloflow_demo.settings import DemoSettings, StartupRefused, validate_startup

    for extra, reason in (({"DEMO_SMS_MODE": "real"}, "sms_real_unverified"),
                          ({"DEMO_CALLBACK_MODE": "real", "DEMO_CALLBACK_SECRET": "s" * 32},
                           "callback_real_unverified")):
        s = DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@127.0.0.1/x_demo",
                                   **extra})
        with pytest.raises(StartupRefused) as exc:
            validate_startup(s)
        assert exc.value.reason == reason


# ── CLI lifetime: interrupt during a send, cancellation-ignoring send ─────────


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _wait_for(pred: Callable[[], bool], proc: subprocess.Popen[str], what: str) -> None:
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if pred():
            return
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"exited {proc.returncode} before {what}\n{out}\n{err}")
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def _spawn(env: dict[str, str], wd: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, str(HELPER), "--workdir", str(wd), *args],
                            env=demo_subprocess_env({**os.environ, **env}),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize(("mode", "expected_exit"), [("block", 1), ("stubborn", 4)])
def test_cli_interrupt_during_send_is_bounded_and_keeps_locks_while_a_call_can_run(
    demo_env: dict[str, str], demo_db_url: str, tmp_path: Path, mode: str, expected_exit: int
) -> None:
    env = _env(demo_env)
    a = _spawn(env, tmp_path, "--mode", mode, "--drain-timeout", "1", "--cancel-grace", "1")
    try:
        _wait_for(lambda: any(c["phase"] == "request_started"
                              for c in _lines(tmp_path / "calls.jsonl")), a, "request")
        a.send_signal(signal.SIGTERM)
        # while A is shutting down, a second invocation is refused and sends nothing
        b_wd = tmp_path / "b"
        b_wd.mkdir()
        b = _spawn(env, b_wd, "--mode", "ok")
        b_out, _ = b.communicate(timeout=30)
        a_out, a_err = a.communicate(timeout=30)
    finally:
        for p in (a,):
            if p.poll() is None:
                p.kill()
    assert b.returncode == 2 and "refused=process_lock_held" in b_out
    assert _lines(b_wd / "calls.jsonl") == []
    assert a.returncode == expected_exit, a_err
    phases = [c["phase"] for c in _lines(tmp_path / "calls.jsonl")]
    assert phases.count("request_started") == 1, "never a retry"
    assert "request_cancel_received" in phases and "request_finished" not in phases
    assert _q(demo_db_url, "SELECT used FROM send_quota") == [(1,)], "no refund"
    _assert_clean(a_out + a_err + b_out)
    if expected_exit == 4:
        assert "drain=incomplete" in a_out


async def test_lt4_appointment_is_0900_clinic_time_whatever_the_run_time(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    # a 16:00 EDT run still targets 09:00 clinic time on the target date
    rec = Recorder(_ok)
    assert await _run(_env(demo_env), rec, now=datetime(2026, 10, 26, 20, 0, tzinfo=UTC)) == 0
    assert _q(demo_db_url, "SELECT appt_start FROM reminder_records") == [
        (datetime(2026, 10, 28, 13, 0, tzinfo=UTC),)]
    assert "Wednesday, Oct 28 at 9:00 AM" in json.loads(rec.requests[0].content)["Body"]
