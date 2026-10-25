"""Day 5: inbound replies, matching, staff follow-up and write-back.

Aligned package: v1 + v1r1 + v1r2, later wins (Codex ALIGN 20261025T165034Z).
The owner decisions are Q-B3 and Q-B4, with CANCEL meaning cancel the appointment
(20261025T163355Z).

Classification is strict: trim plus upper-case.
- 1, YES, Y → CONFIRM, the only write-back path.
- 2 → RESCHEDULE.
- 3, CANCEL → CANCEL_REQUEST, a staff item with no opt-out and no EHR write.
- STOP, STOPALL, UNSUBSCRIBE, END, QUIT → OPT_OUT.
- Anything else → UNCLEAR.

Matching uses a durable contact history (phone_key, contact_seq drawn under the
match lock before dispatch). The candidate snapshot and each candidate's status
are frozen at receipt. Rules:
- |H| ≥ 2 → uncorrelated_reply, staff only.
- |H| = 1 → the candidate's status at receipt and its current state decide.

Simulated correlation is an in-process argument only. Everything is synthetic.
The reply signature fixtures prove the internal verifier only, never Notifyre's
live signing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.inbound import (
    Cls,
    ReceiveResult,
    classify,
    process_pending,
    receive,
    redact,
)
from haloflow_demo.ownership import acquire_ownership, recover
from haloflow_demo.sending import StubGateway, send_one
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.synthetic_ehr import (
    ApptStatus,
    Fixtures,
    Observation,
    SyntheticAppointment,
    SyntheticEhr,
    SyntheticPatient,
)

TEST_NUMBER = "+15550100199"   # fictional 555-01xx
OTHER_NUMBER = "+15550100142"
SENDER = "+15550100100"        # fictional configured sender (our number)
SECRET = "test-callback-secret-0123456789"
TENANT = "demo-clinic"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # send time
APPT_START = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)   # 09:00 EDT Wed
REPLY_AT = NOW + timedelta(minutes=5)


# ── helpers (independent of the module under test) ───────────────────────────


def _sig(raw: bytes, t: int, secret: str = SECRET) -> str:
    v = hmac.new(secret.encode(), str(t).encode() + b"." + raw, hashlib.sha256).hexdigest()
    return f"t={t},v={v}"


def _event(reply_id: str, message: str, *, sender: str = TEST_NUMBER,
           recipient: str = SENDER, received: datetime = REPLY_AT,
           event: str = "sms_received") -> bytes:
    ts = int(received.timestamp())
    return json.dumps({
        "Event": event, "Timestamp": ts,
        "Payload": {"ReplyID": reply_id, "RecipientID": "r-1", "RecipientNumber": recipient,
                    "SenderNumber": sender, "Message": message, "ReceivedDateUtc": ts},
    }).encode()


def _env(demo_env: dict[str, str], **extra: str) -> dict[str, str]:
    return {**demo_env, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER, "DEMO_SMS_SENDER": SENDER,
            "DEMO_CALLBACK_SECRET": SECRET, **extra}


async def _own(env: dict[str, str]):  # type: ignore[no-untyped-def]
    s = DemoSettings.from_env(env)
    return s, await acquire_ownership(s, validate_demo_database_url(s))


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def _patient(i: int) -> SyntheticPatient:
    return SyntheticPatient(f"syn-p-{i}", f"Synthetic Patient {i}", phone_alias="alpha")


def _appt(i: int, start: datetime = APPT_START) -> SyntheticAppointment:
    return SyntheticAppointment(f"syn-a-{i}", f"syn-p-{i}", start, ApptStatus.SCHEDULED,
                                confirmation="1")


def _ehr(n: int = 4, start: datetime = APPT_START) -> SyntheticEhr:
    return SyntheticEhr(Fixtures(patients={f"syn-p-{i}": _patient(i) for i in range(1, n + 1)},
                                 appointments=[_appt(i, start) for i in range(1, n + 1)]))


class FlakyEhr:
    """Read-only EHR double: the first ``fail`` reads raise; counts every call."""

    def __init__(self, inner: SyntheticEhr, fail: int) -> None:
        self.inner = inner
        self.fail = fail
        self.reads = 0
        self.writes = 0
        self.supports_conditional_write = True
        self.conditional_contract_verified = False

    async def read_confirmation(self, appt_id: str) -> Observation:
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionError("synthetic EHR read failure")
        return await self.inner.read_confirmation(appt_id)

    async def write_confirmation_conditional(self, *a: Any, **k: Any) -> Any:
        self.writes += 1
        raise AssertionError("no EHR write expected")


class RaisingEhr(FlakyEhr):
    def __init__(self) -> None:
        super().__init__(_ehr(), fail=10**6)


async def _send(own: Any, s: DemoSettings, i: int, *, start: datetime = APPT_START,
                now: datetime = NOW, gw: StubGateway | None = None,
                hook: Callable[[str], Awaitable[None]] | None = None) -> Any:
    return await send_one(own, s, _appt(i, start), _patient(i), gw or StubGateway(TEST_NUMBER),
                          tenant_id=TENANT, now=_clock(now), dispatch_hook=hook)


async def _reply(own: Any, s: DemoSettings, text: str, *, reply_id: str,
                 at: datetime = REPLY_AT, sender: str = TEST_NUMBER,
                 correlation: int | None = None,
                 hook: Callable[[str], Awaitable[None]] | None = None) -> ReceiveResult:
    raw = _event(reply_id, text, sender=sender, received=at)
    return await receive(own, s, raw, [_sig(raw, int(at.timestamp()))], now=_clock(at),
                         simulated_correlation=correlation, hook=hook)


async def _process(own: Any, s: DemoSettings, ehr: Any, *, at: datetime = REPLY_AT,
                   hook: Callable[[str, int], Awaitable[None]] | None = None) -> Any:
    return await process_pending(own, s, ehr, now=_clock(at), hook=hook)


def _reminders(url: str) -> dict[str, str]:
    return dict(_q(url, "SELECT emr_appt_id, state FROM reminder_records"))


def _items(url: str) -> list[tuple[str, int | None]]:
    return sorted(_q(url, "SELECT kind, reminder_id FROM follow_up_items"),
                  key=lambda r: (r[0], r[1] or 0))


def _tasks(url: str) -> list[tuple[Any, ...]]:
    return _q(url, "SELECT reminder_id, state, observed_before FROM writeback_tasks")


def _disposition(url: str, reply_id: str) -> tuple[str, str | None]:
    rows = _q(url, "SELECT state, disposition FROM inbound_messages WHERE provider_event_id = %s",
              (reply_id,))
    assert len(rows) == 1
    return rows[0]


def _waiters(url: str, waiter_tag: str) -> list[tuple[int, list[int]]]:
    return [(int(pid), list(blockers)) for pid, blockers in _q(
        url, "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity"
             " WHERE datname = current_database() AND wait_event_type = 'Lock'"
             " AND position(%s in query) > 0", (waiter_tag,))]


def _backend_query(url: str, pid: int) -> str:
    rows = _q(url, "SELECT query FROM pg_stat_activity WHERE pid = %s", (pid,))
    return str(rows[0][0]) if rows else ""


async def _wait_for_blocked(url: str, waiter_tag: str, *, blocker_pid: int | None = None,
                            blocker_tag: str | None = None, timeout: float = 5.0) -> None:
    """Wait until the exact operation tagged ``waiter_tag`` is lock-waiting AND is
    blocked by the expected holder (by PID, or by the holder's last statement tag).
    Never terminates anything."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        for _pid, blockers in _waiters(url, waiter_tag):
            if blocker_pid is not None and blocker_pid in blockers:
                return
            if blocker_tag is not None and any(
                    blocker_tag in _backend_query(url, b) for b in blockers):
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {waiter_tag!r} waiter blocked by the expected holder")


# ── I01: classification (U) ───────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "cls"), [
    ("1", Cls.CONFIRM), ("YES", Cls.CONFIRM), (" y ", Cls.CONFIRM), ("yes", Cls.CONFIRM),
    ("2", Cls.RESCHEDULE),
    ("3", Cls.CANCEL_REQUEST), ("cancel", Cls.CANCEL_REQUEST), (" CANCEL ", Cls.CANCEL_REQUEST),
    ("STOP", Cls.OPT_OUT), ("stopall", Cls.OPT_OUT), ("Unsubscribe", Cls.OPT_OUT),
    ("END", Cls.OPT_OUT), ("quit", Cls.OPT_OUT),
    ("NO", Cls.UNCLEAR), ("n", Cls.UNCLEAR), ("START", Cls.UNCLEAR), ("1.", Cls.UNCLEAR),
    ("yes!", Cls.UNCLEAR), ("Yes but late", Cls.UNCLEAR), ("", Cls.UNCLEAR),
    ("please stop", Cls.UNCLEAR), ("12", Cls.UNCLEAR),
])
def test_i01_classification(text: str, cls: Cls) -> None:
    assert classify(text) == cls


# ── I05' (U): text minimisation ───────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "call me at 555 010 0199", "+1 (555) 010-0199 please", "5550100199", "x 555.010.0199 y",
    "mail rachel.example@example.com"])
def test_i05_redaction_removes_numbers_and_email(text: str) -> None:
    out = redact(text)
    assert not re.search(r"\d(?:[\s().+-]*\d){6,}", out)
    assert "@" not in out
    assert "[number]" in out or "[email]" in out


def test_i05_redaction_keeps_short_tokens_and_bounds_length() -> None:
    assert redact("1") == "1"
    assert redact("Yes but late") == "Yes but late"
    assert len(redact("a" * 5000)) <= 1600


# ── I02/I27: signature (D via receive) ────────────────────────────────────────


async def test_i02_signature_valid_and_invalid(demo_env: dict[str, str], demo_db_url: str) -> None:
    s, own = await _own(_env(demo_env))
    t = int(REPLY_AT.timestamp())
    raw = _event("rid-ok", "1")
    try:
        ok = await receive(own, s, raw, [_sig(raw, t)], now=_clock(REPLY_AT))
        bad_cases: list[tuple[bytes, list[str], datetime]] = [
            (raw, [_sig(raw, t, secret="wrong")], REPLY_AT),
            (raw.replace(b'"1"', b'"2"'), [_sig(raw, t)], REPLY_AT),
            (raw, [], REPLY_AT),
            (raw, ["garbage"], REPLY_AT),
            (raw, [_sig(raw, t)], REPLY_AT + timedelta(seconds=301)),
            (raw, [_sig(raw, t)], REPLY_AT - timedelta(seconds=301)),
        ]
        statuses = [(await receive(own, s, r, h, now=_clock(at))).status
                    for r, h, at in bad_cases]
        edge = _event("rid-edge", "1")
        at_edge = await receive(own, s, edge, [_sig(edge, t)],
                                now=_clock(REPLY_AT + timedelta(seconds=300)))
    finally:
        await own.close()
    assert ok.status == 204
    assert statuses == [401] * 6
    assert at_edge.status == 204
    assert _q(demo_db_url, "SELECT provider_event_id FROM inbound_messages ORDER BY id") == [
        ("rid-ok",), ("rid-edge",)]


async def test_i27_signature_header_must_be_unambiguous(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    t = int(REPLY_AT.timestamp())
    raw = _event("rid-x", "1")
    good = _sig(raw, t)
    v = good.split("v=")[1]
    variants = [
        [good, good],                         # two headers
        [good + ",x=1"],                      # extra part
        [f"t={t},t={t},v={v}"],               # repeated t
        [f"t={t},v={v.upper()}"],             # upper-case hex
        [f"t={t},v={v[:-1]}"],                # 63 hex
        [f"v={v},t={t},v={v}"],               # repeated v
        [f"t={t}"],                           # missing v
    ]
    try:
        statuses = [(await receive(own, s, raw, h, now=_clock(REPLY_AT))).status
                    for h in variants]
    finally:
        await own.close()
    assert statuses == [401] * len(variants)
    assert _q(demo_db_url, "SELECT count(*) FROM inbound_messages") == [(0,)]


# ── I03/I28: event parsing (strict) ───────────────────────────────────────────


def _signed(raw: bytes) -> list[str]:
    return [_sig(raw, int(REPLY_AT.timestamp()))]


async def test_i03_i28_malformed_events_rejected(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ts = int(REPLY_AT.timestamp())

    def body(payload: dict[str, Any], **top: Any) -> bytes:
        return json.dumps({"Event": "sms_received", "Timestamp": ts, "Payload": payload,
                           **top}).encode()

    base = {"ReplyID": "r", "RecipientNumber": SENDER, "SenderNumber": TEST_NUMBER,
            "Message": "1", "ReceivedDateUtc": ts}
    bad: list[bytes] = [
        b"not json",
        body({**base, "ReplyID": ""}),
        body({k: v for k, v in base.items() if k != "ReplyID"}),
        body({**base, "ReplyID": "x" * 257}),
        body({**base, "ReplyID": 123}),
        body({**base, "SenderNumber": "abc"}),
        body({**base, "RecipientNumber": OTHER_NUMBER}),       # not our number
        body({**base, "Message": 1}),
        body({**base, "ReceivedDateUtc": "now"}),
        body(base, Timestamp=True),
        json.dumps({"Event": "sms_received", "Timestamp": ts, "Payload": "x"}).encode(),
        # duplicate key and case-variant collision (hand-written JSON)
        (b'{"Event":"sms_received","Timestamp":%d,"Payload":{"ReplyID":"a","ReplyID":"b",'
         b'"RecipientNumber":"%s","SenderNumber":"%s","Message":"1","ReceivedDateUtc":%d}}'
         % (ts, SENDER.encode(), TEST_NUMBER.encode(), ts)),
        (b'{"Event":"sms_received","Timestamp":%d,"Payload":{"ReplyID":"a","replyid":"b",'
         b'"RecipientNumber":"%s","SenderNumber":"%s","Message":"1","ReceivedDateUtc":%d}}'
         % (ts, SENDER.encode(), TEST_NUMBER.encode(), ts)),
        b"[1,2]",
    ]
    try:
        statuses = [(await receive(own, s, r, _signed(r), now=_clock(REPLY_AT))).status
                    for r in bad]
        other = body(base, Event="sms_sent")
        ignored = await receive(own, s, other, _signed(other), now=_clock(REPLY_AT))
    finally:
        await own.close()
    assert statuses == [400] * len(bad)
    assert ignored.status == 204
    assert _q(demo_db_url, "SELECT count(*) FROM inbound_messages") == [(0,)]


# ── I04/I29: replay ───────────────────────────────────────────────────────────


async def test_i04_i29_replay_same_and_conflicting_content(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        await _send(own, s, 1)
        first = await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, ehr)
        same = await _reply(own, s, "1", reply_id="rid-1")
        changed = await _reply(own, s, "3", reply_id="rid-1")
        await _process(own, s, ehr)
    finally:
        await own.close()
    assert (first.status, same.status, changed.status) == (204, 204, 409)
    assert _q(demo_db_url, "SELECT count(*) FROM inbound_messages") == [(1,)]
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "confirm")
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}
    assert len(_tasks(demo_db_url)) == 1
    assert [k for k, _ in _items(demo_db_url)] == ["replay_conflict"]


# ── I05': database-wide scan for raw numbers ──────────────────────────────────


async def test_i05_no_raw_number_stored_anywhere(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        await _reply(own, s, "call me at 555 010 0199 or +1 (555) 010-0142", reply_id="rid-n")
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    cols = _q(demo_db_url, "SELECT table_name, column_name FROM information_schema.columns"
                           " WHERE table_schema = 'public' AND data_type IN ('text',"
                           " 'character varying')")
    pattern = re.compile(r"\d(?:[\s().+-]*\d){6,}")
    for table, col in cols:
        for (val,) in _q(demo_db_url, f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'):
            if table == "demo_meta" or col in ("phone_key", "content_digest"):
                continue  # hex HMAC / digest values, not phone numbers
            assert not pattern.search(str(val)), f"{table}.{col} holds a digit run"
    assert _q(demo_db_url, "SELECT cls FROM inbound_messages WHERE provider_event_id = 'rid-1'"
              ) == [("CONFIRM",)]


# ── I06/I42: simulated path through the same verifier; correlation ────────────


async def test_i06_simulated_callback_requires_secret(demo_env: dict[str, str]) -> None:
    s, own = await _own({**_env(demo_env), "DEMO_CALLBACK_SECRET": ""})
    raw = _event("rid-s", "1")
    try:
        r = await receive(own, s, raw, [_sig(raw, int(REPLY_AT.timestamp()), secret="")],
                          now=_clock(REPLY_AT))
    finally:
        await own.close()
    assert (r.status, r.reason) == (503, "callback_secret_unset")


async def test_i42_simulated_correlation_only_in_process(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        await _send(own, s, 1)
        r2 = await _send(own, s, 2)
        # in-process simulated correlation → acts on B (2)
        sim = await _reply(own, s, "1", reply_id="rid-sim", correlation=r2.reminder_id)
        # equivalent bytes with a separate ReplyID over the plain (HTTP) path → uncorrelated
        http = await _reply(own, s, "1", reply_id="rid-http")
        await _process(own, s, ehr)
    finally:
        await own.close()
    assert sim.status == http.status == 204
    assert _disposition(demo_db_url, "rid-sim") == ("PROCESSED", "confirm")
    assert _disposition(demo_db_url, "rid-http") == ("PROCESSED", "uncorrelated_reply")
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED", "syn-a-2": "CONFIRMED"}
    assert _q(demo_db_url, "SELECT simulated_correlation IS NOT NULL FROM inbound_messages"
                           " ORDER BY id") == [(True,), (False,)]


async def test_i42_http_route_cannot_supply_correlation(demo_env: dict[str, str]) -> None:
    from haloflow_demo.web import build_callback_app

    seen: list[tuple[bytes, list[str]]] = []

    async def receiver(raw: bytes, sigs: list[str]) -> ReceiveResult:
        seen.append((raw, sigs))
        return ReceiveResult(204, None, None)

    app = build_callback_app(receiver=receiver)
    raw = _event("rid", "1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8081") as c:
        r = await c.post("/webhooks/sms/inbound?simulated_correlation=1", content=raw,
                         headers={"Notifyre-Signature": "t=1,v=00",
                                  "X-Simulated-Correlation": "1"})
    assert r.status_code == 204
    # the route passes only the raw bytes and the signature header values
    assert seen == [(raw, ["t=1,v=00"])]


# ── I27: streamed size cap at the HTTP route ──────────────────────────────────


async def test_i27_streamed_body_cap() -> None:
    from haloflow_demo.web import build_callback_app

    calls: list[int] = []

    async def receiver(raw: bytes, sigs: list[str]) -> ReceiveResult:
        calls.append(len(raw))
        return ReceiveResult(204, None, None)

    app = build_callback_app(receiver=receiver)
    produced = 0

    async def gen() -> AsyncIterator[bytes]:
        nonlocal produced
        for _ in range(64):            # up to 64 KiB offered
            produced += 1024
            yield b"x" * 1024

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8081") as c:
        big = await c.post("/webhooks/sms/inbound", content=gen())
        declared = await c.post("/webhooks/sms/inbound", content=b"x" * 10,
                                headers={"Content-Length": str(16 * 1024 + 1)})
        ok = await c.post("/webhooks/sms/inbound", content=b"x" * (16 * 1024))
    assert big.status_code == 413
    assert declared.status_code == 413
    assert ok.status_code == 204
    assert calls == [16 * 1024]


# ── I07: dispatcher off ───────────────────────────────────────────────────────


async def test_i07_received_rows_wait_for_processing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = FlakyEhr(_ehr(), fail=0)
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-1") == ("RECEIVED", None)
    assert ehr.reads == 0


# ── I08/I09/I11: effects per class on the first unique contact ────────────────


async def test_i08_confirm_creates_reminder_state_and_task(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        r = await _send(own, s, 1)
        await _reply(own, s, " yes ", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}
    assert _tasks(demo_db_url) == [(r.reminder_id, "PENDING", "1")]
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "confirm")
    assert _items(demo_db_url) == []


@pytest.mark.parametrize(("text", "state", "kind"), [
    ("2", "RESCHEDULE_REQUESTED", "reschedule"),
    ("3", "CANCEL_REQUESTED", "cancel_request"),
    ("CANCEL", "CANCEL_REQUESTED", "cancel_request"),
])
async def test_i09_reschedule_and_cancel_are_staff_only(
    demo_env: dict[str, str], demo_db_url: str, text: str, state: str, kind: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = FlakyEhr(_ehr(), fail=0)
    try:
        r = await _send(own, s, 1)
        await _reply(own, s, text, reply_id="rid-1")
        await _process(own, s, ehr)
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": state}
    assert _items(demo_db_url) == [(kind, r.reminder_id)]
    assert _tasks(demo_db_url) == []
    assert ehr.writes == 0
    assert _q(demo_db_url, "SELECT count(*) FROM opt_outs") == [(0,)], "CANCEL is not an opt-out"


async def test_i10_opt_out_without_any_ehr_call(demo_env: dict[str, str], demo_db_url: str) -> None:
    s, own = await _own(_env(demo_env))
    ehr = RaisingEhr()
    gw = StubGateway(TEST_NUMBER)
    try:
        await _send(own, s, 1)
        await _reply(own, s, "stop", reply_id="rid-1")
        await _process(own, s, ehr)
        later = await _send(own, s, 2, gw=gw)
    finally:
        await own.close()
    assert ehr.reads == 0 and ehr.writes == 0
    assert _q(demo_db_url, "SELECT count(*) FROM opt_outs") == [(1,)]
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "opt_out")
    assert (later.state, later.reason) == ("NOT_SENT", "opted_out")
    assert gw.calls == []


async def test_i11_unclear_then_valid_reply(demo_env: dict[str, str], demo_db_url: str) -> None:
    s, own = await _own(_env(demo_env))
    try:
        r = await _send(own, s, 1)
        await _reply(own, s, "NO", reply_id="rid-a")
        await _process(own, s, _ehr())
        assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}
        await _reply(own, s, "1", reply_id="rid-b", at=REPLY_AT + timedelta(minutes=1))
        await _process(own, s, _ehr(), at=REPLY_AT + timedelta(minutes=1))
    finally:
        await own.close()
    assert _items(demo_db_url) == [("unclear", r.reminder_id)]
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}


async def test_i08_only_confirm_creates_a_task(demo_env: dict[str, str], demo_db_url: str) -> None:
    for text in ("2", "3", "CANCEL", "STOP", "NO", "maybe"):
        s, own = await _own(_env(demo_env))
        try:
            await _send(own, s, 1)
            await _reply(own, s, text, reply_id=f"rid-{text}")
            await _process(own, s, _ehr())
        finally:
            await own.close()
        assert _tasks(demo_db_url) == [], text
        _q(demo_db_url, "TRUNCATE inbound_candidates, inbound_messages, follow_up_items,"
                        " writeback_tasks, reminder_records, contact_history, opt_outs CASCADE")


# ── matching: H rules (I12–I16, I34–I37, I40, I41) ────────────────────────────


async def test_i12_two_contacts_make_a_reply_uncorrelated(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        await _send(own, s, 2)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "uncorrelated_reply")
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED", "syn-a-2": "SUBMITTED"}
    assert [k for k, _ in _items(demo_db_url)] == ["uncorrelated_reply"]
    assert _tasks(demo_db_url) == []


async def test_i13_late_reply_after_appointment_start(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    late = APPT_START + timedelta(minutes=1)
    try:
        r = await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-late", at=late)
        await _process(own, s, _ehr(), at=late)
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-late") == ("PROCESSED", "late_reply")
    assert _items(demo_db_url) == [("late_reply", r.reminder_id)]
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}


async def test_i13_timely_reply_processed_after_start_still_acts(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1", at=APPT_START - timedelta(minutes=1))
        await _process(own, s, _ehr(), at=APPT_START + timedelta(hours=1))
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}


async def test_i14_second_reply_after_resolution(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        r = await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
        await _reply(own, s, "3", reply_id="rid-2", at=REPLY_AT + timedelta(minutes=2))
        await _process(own, s, _ehr(), at=REPLY_AT + timedelta(minutes=2))
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}
    assert _items(demo_db_url) == [("reply_after_resolution", r.reminder_id)]
    assert len(_tasks(demo_db_url)) == 1


async def test_i15_reply_to_uncertain_send(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.sending import SendOutcome

    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER, script=[SendOutcome("UNCERTAIN", "timeout", None)])
    try:
        r = await _send(own, s, 1, gw=gw)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "reply_to_unverified_send")
    assert _items(demo_db_url) == [("reply_to_unverified_send", r.reminder_id)]
    assert _reminders(demo_db_url) == {"syn-a-1": "UNCERTAIN"}


async def test_i16_no_match(demo_env: dict[str, str], demo_db_url: str) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "no_match")
    assert _items(demo_db_url) == [("no_match", None)]


async def test_i34_reply_while_claimed_never_becomes_actionable(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    received: list[ReceiveResult] = []

    async def hook(stage: str) -> None:
        if stage == "before_send":   # contact admitted, row still CLAIMED
            received.append(await _reply(own, s, "1", reply_id="rid-early"))

    try:
        r = await _send(own, s, 1, hook=hook)
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert r.state == "SUBMITTED"
    assert received[0].status == 204
    assert _q(demo_db_url, "SELECT status_at_receipt FROM inbound_candidates") == [("CLAIMED",)]
    assert _disposition(demo_db_url, "rid-early") == ("PROCESSED", "reply_to_unverified_send")
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}
    assert _tasks(demo_db_url) == []


async def test_i35_delayed_reply_to_a_after_b_contacted(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1, start=NOW + timedelta(hours=2))     # A: soon
        await _send(own, s, 2)                                      # B: later
        late_for_a = NOW + timedelta(hours=3)
        await _reply(own, s, "1", reply_id="rid-1", at=late_for_a)
        await _process(own, s, _ehr(), at=late_for_a)
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "uncorrelated_reply")
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED", "syn-a-2": "SUBMITTED"}
    assert _tasks(demo_db_url) == []


async def test_i36_history_survives_reset(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.reset import reset_synthetic

    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
    finally:
        await own.close()
    await reset_synthetic(s)
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 2)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "uncorrelated_reply")
    assert _reminders(demo_db_url) == {"syn-a-2": "SUBMITTED"}
    assert _q(demo_db_url, "SELECT count(*) FROM contact_history") == [(2,)]
    statuses = sorted(r[0] for r in _q(demo_db_url,
                                       "SELECT status_at_receipt FROM inbound_candidates"))
    assert statuses == ["GONE", "SUBMITTED"]


async def test_i37_first_contact_acts_second_does_not(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
        await _send(own, s, 2, now=REPLY_AT + timedelta(minutes=1))
        later = REPLY_AT + timedelta(minutes=10)
        await _reply(own, s, "1", reply_id="rid-2", at=later)
        await _process(own, s, _ehr(), at=later)
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED", "syn-a-2": "SUBMITTED"}
    assert _disposition(demo_db_url, "rid-2") == ("PROCESSED", "uncorrelated_reply")
    assert len(_tasks(demo_db_url)) == 1


async def test_i40_not_sent_contact_does_not_count(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.sending import SendOutcome

    s, own = await _own(_env(demo_env))
    gw = StubGateway(TEST_NUMBER, script=[SendOutcome("NOT_SENT", "connect", None)])
    try:
        r1 = await _send(own, s, 1, gw=gw)
        await _send(own, s, 2)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert (r1.state, r1.reason) == ("NOT_SENT", "connect")
    assert sorted(_q(demo_db_url, "SELECT outcome FROM contact_history")) == [
        ("NOT_SENT",), ("SUBMITTED",)]
    assert _reminders(demo_db_url)["syn-a-2"] == "CONFIRMED"


async def test_i40_paused_at_dispatch_is_recorded_not_sent(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from sqlalchemy import text

    s, own = await _own(_env(demo_env))

    async def hook(stage: str) -> None:
        if stage == "contact_admitted":
            async with own.fenced(exclusive=True) as conn:
                await conn.execute(text("UPDATE demo_settings SET paused = true WHERE id = 1"))

    try:
        r = await _send(own, s, 1, hook=hook)
    finally:
        await own.close()
    assert (r.state, r.reason) == ("NOT_SENT", "paused")
    assert _q(demo_db_url, "SELECT outcome FROM contact_history") == [("NOT_SENT",)]


async def test_i41_crash_after_contact_admission_counts(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))

    async def hook(stage: str) -> None:
        if stage == "contact_admitted":
            own.begin_closing()     # the call is then refused: the row stays CLAIMED

    try:
        r1 = await _send(own, s, 1, hook=hook)
    finally:
        await own.close()
    assert r1.state == "CLAIMED"
    s, own = await _own(_env(demo_env))
    try:
        await recover(own)
        await _send(own, s, 2)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT count(*) FROM contact_history WHERE outcome IS NULL"
                           " OR outcome <> 'NOT_SENT'") == [(2,)]
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "uncorrelated_reply")


async def test_i17_snapshot_is_frozen_at_receipt(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _reply(own, s, "1", reply_id="rid-1")
        await _send(own, s, 1, now=REPLY_AT + timedelta(minutes=1))   # contacted after receipt
        await _process(own, s, _ehr(), at=REPLY_AT + timedelta(minutes=2))
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "no_match")
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}


async def test_i17_pinned_identity_survives_number_change(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
    finally:
        await own.close()
    s2, own = await _own({**_env(demo_env), "DEMO_SMS_TEST_NUMBER": OTHER_NUMBER})
    try:
        await _reply(own, s2, "1", reply_id="rid-old", sender=TEST_NUMBER)
        await _reply(own, s2, "1", reply_id="rid-new", sender=OTHER_NUMBER,
                     at=REPLY_AT + timedelta(seconds=1))
        await _process(own, s2, _ehr(), at=REPLY_AT + timedelta(seconds=2))
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-old") == ("PROCESSED", "confirm")
    assert _disposition(demo_db_url, "rid-new") == ("PROCESSED", "no_match")


# ── I38/I39: membership by lock order, not transaction start time ─────────────


async def test_i38_receipt_waiting_on_contact_lock_sees_the_contact(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    pending: list[asyncio.Task[ReceiveResult]] = []

    async def hook(stage: str) -> None:
        if stage == "contact_locked" and not pending:   # B holds the match lock
            pending.append(asyncio.create_task(_reply(own, s, "1", reply_id="rid-1")))
            await _wait_for_blocked(demo_db_url, "match:receipt", blocker_tag="match:contact")

    try:
        await _send(own, s, 1)
        await _send(own, s, 2, hook=hook)
        await pending[0]
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT count(*) FROM inbound_candidates") == [(2,)]
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "uncorrelated_reply")


async def test_i39_contact_waiting_on_receipt_lock_is_not_a_candidate(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    pending: list[asyncio.Task[Any]] = []

    async def hook(stage: str) -> None:
        if stage == "receipt_locked" and not pending:
            pending.append(asyncio.create_task(_send(own, s, 2)))
            await _wait_for_blocked(demo_db_url, "match:contact", blocker_tag="match:receipt")

    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1", hook=hook)
        await pending[0]
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT count(*) FROM inbound_candidates") == [(1,)]
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED", "syn-a-2": "SUBMITTED"}


# ── I18/I21: baseline read bounds and binding ─────────────────────────────────


async def test_i18_baseline_read_failures_are_bounded(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.tasks import run_task

    s, own = await _own(_env(demo_env))
    ehr = FlakyEhr(_ehr(), fail=3)
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, ehr)
        a1 = _q(demo_db_url, "SELECT state, attempts FROM inbound_messages")
        await _process(own, s, ehr)
        a2 = _q(demo_db_url, "SELECT state, attempts FROM inbound_messages")
        assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}
        await _process(own, s, ehr)
        tasks_now = _q(demo_db_url, "SELECT id FROM writeback_tasks")
        assert len(tasks_now) == 1, "bounded: the third failure applies the confirm"
        task_id = tasks_now[0][0]
        result = await run_task(own, ehr, task_id)
    finally:
        await own.close()
    assert a1 == [("PENDING_RETRY", 1)] and a2 == [("PENDING_RETRY", 2)]
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}
    assert _q(demo_db_url, "SELECT state, reason, observed_before FROM writeback_tasks") == [
        ("NOT_APPLIED", "no_baseline", None)]
    assert [k for k, _ in _items(demo_db_url)] == ["writeback_no_baseline"]
    assert result == "no_baseline"
    assert ehr.writes == 0


async def test_i18_absent_value_is_not_a_writable_baseline(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    ehr.clinic_change("syn-a-1", None)
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, ehr)
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT observed_before FROM writeback_tasks") == [(None,)]
    assert [k for k, _ in _items(demo_db_url)] == ["writeback_no_baseline"]


async def test_i21_baseline_discarded_when_candidate_changes(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()

    async def hook(stage: str, inbound_id: int) -> None:
        if stage == "after_baseline":
            # harness: a concurrent processor's committed effect resolves the reminder
            _q(demo_db_url, "UPDATE reminder_records SET state = 'CANCEL_REQUESTED'")

    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, ehr, hook=hook)
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CANCEL_REQUESTED"}
    assert _tasks(demo_db_url) == []
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "reply_after_resolution")


async def test_i21_baseline_bound_to_reminder_id(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()

    async def hook(stage: str, inbound_id: int) -> None:
        if stage == "after_baseline":
            _q(demo_db_url, "UPDATE inbound_messages SET baseline_reminder_id = -1")  # seam

    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, ehr, hook=hook)
        after_first = _q(demo_db_url, "SELECT state, attempts FROM inbound_messages")
    finally:
        await own.close()
    assert after_first == [("PENDING_RETRY", 0)], "re-queued without consuming an attempt"
    assert _tasks(demo_db_url) == []


# ── I19/I20/I31: atomicity, races, admission ──────────────────────────────────


async def test_i19_fault_before_commit_then_redispatch(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()

    async def hook(stage: str, inbound_id: int) -> None:
        if stage == "effects_written":
            raise RuntimeError("injected fault before commit")

    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        with pytest.raises(RuntimeError):
            await _process(own, s, ehr, hook=hook)
        assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}
        assert _tasks(demo_db_url) == []
        await _process(own, s, ehr)
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}
    assert len(_tasks(demo_db_url)) == 1
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "confirm")


async def test_i20_concurrent_processing_applies_effects_once(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await asyncio.gather(_process(own, s, ehr), _process(own, s, ehr), _process(own, s, ehr))
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}
    assert len(_tasks(demo_db_url)) == 1
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "confirm")


async def test_i31_processing_is_serialised_and_counts_each_failure_once(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ehr = FlakyEhr(_ehr(), fail=10)
    active = 0
    peak = 0

    async def hook(stage: str, inbound_id: int) -> None:
        nonlocal active, peak
        if stage == "before_baseline":
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
        elif stage == "after_baseline":
            active -= 1

    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await asyncio.gather(_process(own, s, ehr, hook=hook), _process(own, s, ehr, hook=hook))
    finally:
        await own.close()
    assert peak == 1, "one processor at a time"
    assert _q(demo_db_url, "SELECT attempts FROM inbound_messages") == [(ehr.reads,)]


async def test_i31_closing_before_effect_applies_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))

    async def hook(stage: str, inbound_id: int) -> None:
        if stage == "after_baseline":
            own.begin_closing()

    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr(), hook=hook)
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT state FROM inbound_messages") in ([("RECEIVED",)],
                                                                       [("PENDING_RETRY",)])
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}


async def test_i43_closing_during_match_lock_wait_applies_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        holder = psycopg.connect(demo_db_url)
        holder.execute("SELECT 1 FROM demo_match_lock WHERE id = 1 FOR UPDATE")
        task = asyncio.create_task(_process(own, s, _ehr()))
        await _wait_for_blocked(demo_db_url, "match:effect",
                                blocker_pid=holder.info.backend_pid)
        own.begin_closing()
        holder.rollback()
        holder.close()
        await task
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}
    assert _tasks(demo_db_url) == []
    assert _q(demo_db_url, "SELECT state FROM inbound_messages")[0][0] in ("RECEIVED",
                                                                           "PENDING_RETRY")


async def test_i30_stop_waits_for_dispatch_lock(demo_env: dict[str, str], demo_db_url: str) -> None:
    s, own = await _own(_env(demo_env))
    order: list[str] = []
    ehr = RaisingEhr()

    async def hook(stage: str) -> None:
        if stage == "before_send":
            stop = asyncio.create_task(_process(own, s, ehr))
            await _wait_for_blocked(demo_db_url, "fence:exclusive", blocker_tag="dispatch:gate")
            order.append("send")
            pending.append(stop)

    pending: list[asyncio.Task[Any]] = []
    try:
        await _send(own, s, 1)
        await _reply(own, s, "STOP", reply_id="rid-stop")
        await _send(own, s, 2, hook=hook)
        await pending[0]
        order.append("stop_done")
    finally:
        await own.close()
    assert order == ["send", "stop_done"]
    assert ehr.reads == 0
    assert _q(demo_db_url, "SELECT count(*) FROM opt_outs") == [(1,)]


# ── I22/I32: write-back runner ────────────────────────────────────────────────


async def _confirmed_task(own: Any, s: DemoSettings, url: str, ehr: SyntheticEhr) -> int:
    await _send(own, s, 1)
    await _reply(own, s, "1", reply_id="rid-1")
    await _process(own, s, ehr)
    return int(_q(url, "SELECT id FROM writeback_tasks")[0][0])


async def test_i22_run_done(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.tasks import run_task

    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        tid = await _confirmed_task(own, s, demo_db_url, ehr)
        state = await run_task(own, ehr, tid)
    finally:
        await own.close()
    assert state == "DONE"
    assert ehr.applied_writes == 1
    assert (await ehr.read_confirmation("syn-a-1")).value == "2"


async def test_i22_lost_response_then_reconcile(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.tasks import reconcile_task, run_task

    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        tid = await _confirmed_task(own, s, demo_db_url, ehr)
        ehr.fail_next_write = "lost_response"
        first = await run_task(own, ehr, tid)
        with pytest.raises(ValueError):
            await run_task(own, ehr, tid)            # no Retry from UNCERTAIN
        second = await reconcile_task(own, ehr, tid)
    finally:
        await own.close()
    assert (first, second) == ("UNCERTAIN", "DONE(reconciled)")
    assert ehr.write_attempts == 1


async def test_i22_connect_failure_then_retry(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.tasks import run_task

    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        tid = await _confirmed_task(own, s, demo_db_url, ehr)
        ehr.fail_next_write = "connect"
        first = await run_task(own, ehr, tid)
        second = await run_task(own, ehr, tid)
    finally:
        await own.close()
    assert (first, second) == ("NOT_APPLIED", "DONE")
    assert ehr.applied_writes == 1


async def test_i22_intervening_change_conflict(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.tasks import run_task

    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        tid = await _confirmed_task(own, s, demo_db_url, ehr)
        ehr.clinic_change("syn-a-1", "3")
        state = await run_task(own, ehr, tid)
    finally:
        await own.close()
    assert state == "writeback_conflict"
    assert ehr.write_attempts == 0
    assert "writeback_conflict" in [k for k, _ in _items(demo_db_url)]


async def test_i22_crash_after_in_flight(demo_env: dict[str, str], demo_db_url: str) -> None:
    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    try:
        tid = await _confirmed_task(own, s, demo_db_url, ehr)
    finally:
        await own.close()
    _q(demo_db_url, "UPDATE writeback_tasks SET state = 'IN_FLIGHT' WHERE id = %s", (tid,))
    s, own = await _own(_env(demo_env))
    try:
        await recover(own)
    finally:
        await own.close()
    assert _q(demo_db_url, "SELECT state, reason FROM writeback_tasks") == [
        ("UNCERTAIN", "restart")]
    assert ehr.write_attempts == 0


async def test_i32_concurrent_runs_make_one_call(
    demo_env: dict[str, str], demo_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import haloflow_demo.tasks as tasks_mod
    from haloflow_demo.tasks import run_task

    s, own = await _own(_env(demo_env))
    ehr = _ehr()
    real_load = tasks_mod._load
    arrived = 0
    both = asyncio.Event()

    async def barrier_load(o: Any, task_id: int) -> Any:
        # Barrier: both runs read the task (PENDING) before either tries to admit it.
        nonlocal arrived
        row = await real_load(o, task_id)
        arrived += 1
        if arrived >= 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=5)
        return row

    try:
        tid = await _confirmed_task(own, s, demo_db_url, ehr)
        monkeypatch.setattr(tasks_mod, "_load", barrier_load)
        results = await asyncio.gather(run_task(own, ehr, tid), run_task(own, ehr, tid),
                                       return_exceptions=True)
    finally:
        await own.close()
    assert ehr.write_attempts == 1
    assert sorted(str(r) for r in results) == ["DONE", "already_running"]


# ── I24: drain ────────────────────────────────────────────────────────────────


async def test_i24_closing_mid_baseline_read_commits_nothing(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))

    class SlowEhr:
        async def read_confirmation(self, appt_id: str) -> Observation:
            own.begin_closing()
            return Observation("1", 1)

    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, SlowEhr())
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}
    s, own = await _own(_env(demo_env))
    try:
        await recover(own)
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert _reminders(demo_db_url) == {"syn-a-1": "CONFIRMED"}


# ── I25/I33: schema and reset ─────────────────────────────────────────────────


async def test_i25_older_schema_refused(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.settings import StartupRefused

    s, own = await _own(_env(demo_env))
    await own.close()
    _q(demo_db_url, "UPDATE demo_meta SET value = '2' WHERE key = 'schema_version'")
    with pytest.raises(StartupRefused) as exc:
        await _own(_env(demo_env))
    assert exc.value.reason == "demo_schema_mismatch"


async def test_i33_reset_preserves_quota_suppression_secret_and_history(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.reset import reset_synthetic
    from haloflow_demo.sending import record_opt_out

    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        await record_opt_out(own, OTHER_NUMBER)
    finally:
        await own.close()
    before = {t: _q(demo_db_url, f"SELECT * FROM {t} ORDER BY 1") for t in
              ("send_quota", "opt_outs", "contact_history")}
    secret = _q(demo_db_url, "SELECT value FROM demo_meta WHERE key = 'phone_key_secret'")
    await reset_synthetic(s)
    after = {t: _q(demo_db_url, f"SELECT * FROM {t} ORDER BY 1") for t in before}
    assert after["send_quota"] == before["send_quota"]
    assert after["opt_outs"] == before["opt_outs"]
    assert [r[:2] for r in after["contact_history"]] == [r[:2] for r in before["contact_history"]]
    assert _q(demo_db_url, "SELECT value FROM demo_meta WHERE key = 'phone_key_secret'") == secret
    assert _q(demo_db_url, "SELECT count(*) FROM reminder_records") == [(0,)]
    # sequence continues (no reused ordering token)
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 2)
    finally:
        await own.close()
    seqs = [r[0] for r in _q(demo_db_url, "SELECT contact_seq FROM contact_history ORDER BY 1")]
    assert len(seqs) == 2 and seqs[1] > seqs[0]


async def test_i33_reset_refused_while_owner_runs(demo_env: dict[str, str]) -> None:
    from haloflow_demo.process_lock import ProcessLock
    from haloflow_demo.reset import reset_synthetic
    from haloflow_demo.settings import StartupRefused

    s = DemoSettings.from_env(_env(demo_env))
    lock = ProcessLock()
    lock.acquire()
    try:
        with pytest.raises(StartupRefused) as exc:
            await reset_synthetic(s)
    finally:
        lock.release()
    assert exc.value.reason == "process_lock_held"


# ── I23: staff surface ────────────────────────────────────────────────────────


async def test_i23_staff_page_shows_replies_items_and_labels(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.service import DbStaffService
    from haloflow_demo.web import CSRF_FIELD, build_staff_app

    s, own = await _own(_env(demo_env))
    svc = DbStaffService(own, s, now=_clock(NOW))
    app = build_staff_app(svc, csrf_secret=b"s" * 32)
    try:
        await _send(own, s, 1)
        await _reply(own, s, "<script>alert(1)</script> 555 010 0199", reply_id="rid-x")
        await _reply(own, s, "3", reply_id="rid-3", at=REPLY_AT + timedelta(seconds=1))
        await _process(own, s, _ehr(), at=REPLY_AT + timedelta(seconds=2))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8080") as c:
            page = (await c.get("/demo/staff")).text
            no_csrf = [(await c.post(path, data=data)).status_code for path, data in [
                ("/demo/inbound/process", {}),
                ("/demo/reply/simulate", {"reminder_id": "1", "text": "1"}),
                ("/demo/appointments/send", {"emr_appt_id": "syn-a-1"}),
                ("/demo/tasks/run", {"task_id": "1"}),
                ("/demo/tasks/reconcile", {"task_id": "1"}),
            ]]
            tok = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page)
    finally:
        await own.close()
    assert "&lt;script&gt;" in page and "<script>alert" not in page
    assert "5550100199" not in page and "555 010 0199" not in page
    assert "Cancel requested" in page and "Cancel the appointment in the EHR" in page
    assert "SIMULATED" in page and "Delivered" not in page
    assert no_csrf == [403] * 5
    assert _reminders(demo_db_url) == {"syn-a-1": "CANCEL_REQUESTED"}, "no action ran"
    assert tok is not None


async def test_i23_simulate_reply_action_is_labelled(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.service import DbStaffService

    s, own = await _own(_env(demo_env))
    svc = DbStaffService(own, s, now=_clock(REPLY_AT))
    try:
        await _send(own, s, 1)
        r2 = await _send(own, s, 2)
        res = await svc.simulate_reply(r2.reminder_id, "1")
        await _process(own, s, _ehr())
        page = await svc.page_data()
    finally:
        await own.close()
    assert res.status == 204
    assert _reminders(demo_db_url)["syn-a-2"] == "CONFIRMED"
    inbound = page["inbound"]
    assert any("SIMULATED — correlated by the demo, not by SMS" in str(row) for row in inbound)


# ── P-NS: provenance namespaces (Codex ALIGN 20261025T165627Z) ────────────────
# A send's provenance comes from its gateway's construction: StubGateway gives
# 'stub', NotifyreGateway gives 'real'. An unknown gateway fails closed. A reply's
# provenance comes from the callback mode: simulated (demo secret, HTTP or
# in-process) gives 'stub'. Real ingress needs the real callback gate, which is
# still refused. A stub-provenance reply never matches real history, so the
# first real scenario is unique only if real history is empty. MockTransport
# real sends prove nothing about live sends.


def _real_gw(status: int = 200) -> Any:
    from haloflow_demo.sending import NotifyreGateway

    def _h(req: httpx.Request) -> httpx.Response:
        if status == 200:
            return httpx.Response(200, json={"success": True,
                                             "payload": {"smsMessageID": "n-1"}})
        return httpx.Response(status)

    return NotifyreGateway(api_key="k", sender=SENDER, test_number=TEST_NUMBER,
                           transport=httpx.MockTransport(_h))


def test_p4_gateway_provenance_comes_from_construction() -> None:
    assert StubGateway(TEST_NUMBER).provenance == "stub"
    assert _real_gw().provenance == "real"


async def test_p4_unknown_gateway_fails_closed(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.sending import SendOutcome

    class Foreign:
        calls = 0

        async def send(self, to: str, body: str) -> SendOutcome:
            Foreign.calls += 1
            return SendOutcome("SUBMITTED", None, "x")

    class Forged(Foreign):
        provenance = "realish"

    s, own = await _own(_env(demo_env))
    try:
        r1 = await send_one(own, s, _appt(1), _patient(1), Foreign(), tenant_id=TENANT,  # type: ignore[arg-type]
                            now=_clock(NOW))
        r2 = await send_one(own, s, _appt(2), _patient(2), Forged(), tenant_id=TENANT,  # type: ignore[arg-type]
                            now=_clock(NOW))
    finally:
        await own.close()
    assert (r1.state, r1.reason) == ("NOT_SENT", "unknown_gateway")
    assert (r2.state, r2.reason) == ("NOT_SENT", "unknown_gateway")
    assert Foreign.calls == 0
    assert _q(demo_db_url, "SELECT count(*) FROM contact_history") == [(0,)]
    assert _q(demo_db_url, "SELECT count(*) FROM send_quota") == [(0,)]


async def test_p1_simulated_reply_matches_only_stub_history(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)                       # stub A
        await _send(own, s, 2)                       # stub B
        await send_one(own, s, _appt(3), _patient(3), _real_gw(), tenant_id=TENANT,
                       now=_clock(NOW))              # real C (MockTransport)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert sorted(_q(demo_db_url, "SELECT provenance FROM contact_history")) == [
        ("real",), ("stub",), ("stub",)]
    cands = _q(demo_db_url, "SELECT r.emr_appt_id FROM inbound_candidates c JOIN"
                            " reminder_records r ON r.id = c.reminder_id ORDER BY 1")
    assert cands == [("syn-a-1",), ("syn-a-2",)]
    assert _disposition(demo_db_url, "rid-1") == ("PROCESSED", "uncorrelated_reply")
    assert _reminders(demo_db_url)["syn-a-3"] == "SUBMITTED"


async def test_p3_simulated_reply_never_matches_real_history(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        r = await send_one(own, s, _appt(1), _patient(1), _real_gw(), tenant_id=TENANT,
                           now=_clock(NOW))
        await _reply(own, s, "1", reply_id="rid-http")
        await _reply(own, s, "1", reply_id="rid-sim", correlation=r.reminder_id,
                     at=REPLY_AT + timedelta(seconds=1))
        await _process(own, s, _ehr(), at=REPLY_AT + timedelta(seconds=2))
    finally:
        await own.close()
    assert _disposition(demo_db_url, "rid-http") == ("PROCESSED", "no_match")
    assert _disposition(demo_db_url, "rid-sim") == ("PROCESSED", "no_match")
    assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED"}
    assert _q(demo_db_url, "SELECT DISTINCT provenance FROM inbound_messages") == [("stub",)]


async def test_p2_real_ingress_refused(demo_env: dict[str, str], demo_db_url: str) -> None:
    from haloflow_demo.settings import StartupRefused, validate_startup

    real = DemoSettings.from_env({**_env(demo_env), "DEMO_CALLBACK_MODE": "real"})
    with pytest.raises(StartupRefused) as exc:
        validate_startup(real)
    assert exc.value.reason == "callback_real_unverified"
    s, own = await _own(_env(demo_env))
    raw = _event("rid-r", "1")
    try:
        res = await receive(own, real, raw, [_sig(raw, int(REPLY_AT.timestamp()))],
                            now=_clock(REPLY_AT))
    finally:
        await own.close()
    assert (res.status, res.reason) == (503, "callback_real_unverified")
    assert _q(demo_db_url, "SELECT count(*) FROM inbound_messages") == [(0,)]


async def test_p3_forged_provenance_fields_ignored(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    ts = int(REPLY_AT.timestamp())
    raw = json.dumps({"Event": "sms_received", "Timestamp": ts, "Provenance": "real",
                      "Payload": {"ReplyID": "rid-f", "RecipientNumber": SENDER,
                                  "SenderNumber": TEST_NUMBER, "Message": "1",
                                  "ReceivedDateUtc": ts, "Provenance": "real"}}).encode()
    try:
        await send_one(own, s, _appt(1), _patient(1), _real_gw(), tenant_id=TENANT,
                       now=_clock(NOW))
        res = await receive(own, s, raw, [_sig(raw, ts)], now=_clock(REPLY_AT))
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert res.status == 204
    assert _q(demo_db_url, "SELECT provenance FROM inbound_messages") == [("stub",)]
    assert _disposition(demo_db_url, "rid-f") == ("PROCESSED", "no_match")


async def test_p_replay_keeps_original_provenance_and_effects_once(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
        again = await _reply(own, s, "1", reply_id="rid-1")
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert again.status == 204
    assert _q(demo_db_url, "SELECT provenance, state FROM inbound_messages") == [
        ("stub", "PROCESSED")]
    assert len(_tasks(demo_db_url)) == 1


async def test_p_uncertain_real_attempt_counts_in_real_history_and_survives_reset(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.reset import reset_synthetic

    s, own = await _own(_env(demo_env))
    try:
        r = await send_one(own, s, _appt(1), _patient(1), _real_gw(500), tenant_id=TENANT,
                           now=_clock(NOW))
        await _send(own, s, 2)
    finally:
        await own.close()
    assert (r.state, r.reason) == ("UNCERTAIN", "5xx")
    await reset_synthetic(s)
    assert sorted(_q(demo_db_url, "SELECT provenance, outcome FROM contact_history")) == [
        ("real", "UNCERTAIN"), ("stub", "SUBMITTED")]



# ── Codex tests-only review 20261025T170549Z: impostors, correlation replay ───


async def test_p4_impostor_gateways_claiming_valid_provenance_fail_closed(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    from haloflow_demo.sending import SendOutcome

    calls: list[str] = []

    class ClaimsReal:
        provenance = "real"

        async def send(self, to: str, body: str) -> SendOutcome:
            calls.append("real")
            return SendOutcome("SUBMITTED", None, "x")

    class ClaimsStub(ClaimsReal):
        provenance = "stub"

    StubGateway_ = type("StubGateway", (ClaimsStub,), {})       # same-name impostor
    NotifyreGateway_ = type("NotifyreGateway", (ClaimsReal,), {})

    s, own = await _own(_env(demo_env))
    try:
        results = [await send_one(own, s, _appt(i), _patient(i), gw(), tenant_id=TENANT,  # type: ignore[arg-type]
                                  now=_clock(NOW))
                   for i, gw in enumerate((ClaimsReal, ClaimsStub, StubGateway_,
                                           NotifyreGateway_), start=1)]
    finally:
        await own.close()
    assert [(r.state, r.reason) for r in results] == [("NOT_SENT", "unknown_gateway")] * 4
    assert calls == []
    assert _q(demo_db_url, "SELECT count(*) FROM contact_history") == [(0,)]
    assert _q(demo_db_url, "SELECT count(*) FROM send_quota") == [(0,)]


async def test_p4_real_gateway_with_tampered_provenance_fails_closed(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    gw = _real_gw()
    gw.provenance = "stub"         # instance tampering after construction
    s, own = await _own(_env(demo_env))
    try:
        r = await send_one(own, s, _appt(1), _patient(1), gw, tenant_id=TENANT, now=_clock(NOW))
    finally:
        await own.close()
    assert (r.state, r.reason) == ("NOT_SENT", "unknown_gateway")
    assert _q(demo_db_url, "SELECT count(*) FROM contact_history") == [(0,)]


@pytest.mark.parametrize("first_correlated", [False, True])
async def test_replay_same_reply_id_cannot_switch_correlation(
    demo_env: dict[str, str], demo_db_url: str, first_correlated: bool
) -> None:
    s, own = await _own(_env(demo_env))
    try:
        await _send(own, s, 1)
        r2 = await _send(own, s, 2)
        corr_first = r2.reminder_id if first_correlated else None
        corr_second = None if first_correlated else r2.reminder_id
        a = await _reply(own, s, "1", reply_id="rid-x", correlation=corr_first)
        cands_before = _q(demo_db_url, "SELECT contact_seq, reminder_id, status_at_receipt"
                                       " FROM inbound_candidates ORDER BY 1")
        b = await _reply(own, s, "1", reply_id="rid-x", correlation=corr_second)
        await _process(own, s, _ehr())
        c = await _reply(own, s, "1", reply_id="rid-x", correlation=corr_second)
        await _process(own, s, _ehr())
    finally:
        await own.close()
    assert (a.status, b.status, c.status) == (204, 204, 204)
    assert _q(demo_db_url, "SELECT simulated_correlation, provenance FROM inbound_messages") == [
        (corr_first, "stub")]
    assert _q(demo_db_url, "SELECT contact_seq, reminder_id, status_at_receipt"
                           " FROM inbound_candidates ORDER BY 1") == cands_before
    if first_correlated:
        assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED", "syn-a-2": "CONFIRMED"}
        assert len(_tasks(demo_db_url)) == 1
    else:
        assert _disposition(demo_db_url, "rid-x") == ("PROCESSED", "uncorrelated_reply")
        assert _reminders(demo_db_url) == {"syn-a-1": "SUBMITTED", "syn-a-2": "SUBMITTED"}
        assert _tasks(demo_db_url) == []
