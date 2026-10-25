"""Day 4 — controlled send path (R-9..R-14) and owner decision Q-B4.

Per appointment:
  1. fenced CLAIM (one row per tenant+appointment; a second claim is skipped);
  2. one fenced transaction: preconditions in order (test number configured,
     not paused, not opted out) then the atomic cap reservation on the clinic
     day; the reserved day is recorded on the reminder;
  3. quota-day revalidation before handing off: if the clinic day has
     changed since the reservation, NOT_SENT(quota_day_expired), no call
     (reservations are never refunded);
  4. ONE provider attempt through ``Ownership.guard_external``. Inside that
     tracked task (``_dispatch``) the settings row is held FOR SHARE from the
     last checks (epoch, pause, opt-out, clinic day) through the request, so
     pause/opt-out writers (FOR UPDATE) serialise with it; a final synchronous
     ownership/closing admission runs immediately before the request;
  5. the outcome committed immediately (fenced). If ownership is lost before
     or after the call, the row stays CLAIMED and restart recovery marks it
     UNCERTAIN(restart_after_claim); it is never re-sent blindly.

Day 5 adds:
  * contact admission (package v1r2): after the quota-day check and before
    dispatch, a separate fenced transaction takes the match lock, draws
    ``contact_seq`` and records the contact in ``contact_history`` with the
    gateway's construction provenance. The outcome commit also records the
    outcome there. NOT_SENT and REJECTED are the certain no-message outcomes;
  * provenance (P-NS): StubGateway is 'stub' and NotifyreGateway is 'real'.
    Any other gateway fails closed (NOT_SENT(unknown_gateway)) before any
    reservation, contact or call;
  * N1/N2: documented Notifyre request key casing, and strict 2xx decoding.

Real Notifyre mode is refused at start-up until the send contract (endpoint,
payload, response id, sender registration) is verified against Notifyre's
documentation and a controlled test (``sms_real_unverified``). MockTransport
tests prove nothing about live sends.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from haloflow_demo.errors import OwnerLost
from haloflow_demo.ownership import Ownership
from haloflow_demo.reminders import claim_reminder
from haloflow_demo.selection import Eligible
from haloflow_demo.settings import DemoSettings, normalize_us_phone
from haloflow_demo.strict_json import StrictJsonError, ci_get, loads_strict
from haloflow_demo.synthetic_ehr import SyntheticAppointment, SyntheticPatient

CLINIC_NAME = "HaloVox Demo Clinic"  # Q-B4 (Rachel, 2026-10-25)
NOTIFYRE_API_BASE = "https://api.notifyre.com"


# ── Q-B4 text ─────────────────────────────────────────────────────────────────


def first_name_of(patient: SyntheticPatient) -> str:
    return patient.display_name.removeprefix("Synthetic ")


def reminder_text(first_name: str, start_utc: datetime, tz: str) -> str:
    """Exact owner-approved text (Q-B4). Date/time in clinic time, no zone suffix."""
    local = start_utc.astimezone(ZoneInfo(tz))
    hour = local.strftime("%I").lstrip("0") or "12"
    when = f"{local:%A, %b %d} at {hour}:{local:%M %p}"
    return (
        f"Hi {first_name}, this is {CLINIC_NAME}. You have an appointment on {when}. "
        "Reply 1 to confirm, 2 to reschedule, or 3 to cancel. Reply STOP to opt out."
    )


# ── outcomes and classification (R-12) ───────────────────────────────────────


@dataclass(frozen=True)
class SendOutcome:
    state: str  # SUBMITTED | NOT_SENT | REJECTED | UNCERTAIN
    reason: str | None
    provider_message_id: str | None


def classify_exception(exc: BaseException) -> SendOutcome:
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return SendOutcome("NOT_SENT", "connect", None)  # never reached the provider
    if isinstance(exc, httpx.TimeoutException):
        return SendOutcome("UNCERTAIN", "timeout", None)
    return SendOutcome("UNCERTAIN", "transport", None)


def _decode_2xx(resp: httpx.Response, recipient: str | None) -> SendOutcome:
    """N2: Success must be boolean true; SmsMessageID a non-empty string; an
    InvalidToNumbers listing our recipient is REJECTED(invalid_to). Duplicate or
    conflicting keys, or a malformed InvalidToNumbers, are malformed_2xx. Last
    key never wins."""
    try:
        data = loads_strict(resp.content)
    except StrictJsonError:
        return SendOutcome("UNCERTAIN", "malformed_2xx", None)
    except Exception:
        return SendOutcome("UNCERTAIN", "incomplete_2xx", None)
    if not isinstance(data, dict):
        return SendOutcome("UNCERTAIN", "incomplete_2xx", None)
    payload = ci_get(data, "Payload")
    invalid = ci_get(payload, "InvalidToNumbers") if isinstance(payload, dict) else None
    if invalid is not None:
        if not isinstance(invalid, list) or not all(
            isinstance(i, dict) and isinstance(ci_get(i, "Number"), str) for i in invalid
        ):
            return SendOutcome("UNCERTAIN", "malformed_2xx", None)
        if invalid:
            # Certain rejection only if every listed number IS our one intended
            # recipient. Anything unrelated or unparseable is contradictory.
            listed = {normalize_us_phone(ci_get(i, "Number")) for i in invalid}
            if recipient is not None and listed == {recipient}:
                return SendOutcome("REJECTED", "invalid_to", None)
            return SendOutcome("UNCERTAIN", "unexpected_invalid_to", None)
    if ci_get(data, "Success") is not True or not isinstance(payload, dict):
        return SendOutcome("UNCERTAIN", "incomplete_2xx", None)
    msg_id = ci_get(payload, "SmsMessageID")
    if isinstance(msg_id, str) and msg_id:
        return SendOutcome("SUBMITTED", None, msg_id)
    return SendOutcome("UNCERTAIN", "incomplete_2xx", None)


# Documented Notifyre response names (Send SMS). Only these spellings are ever
# reported, and only their names, never values (live-sms-test output).
DOCUMENTED_RESPONSE_KEYS = ("success", "statuscode", "message", "payload", "errors",
                            "smsmessageid", "friendlyid", "invalidtonumbers")


def observed_response_keys(resp: httpx.Response) -> tuple[str, ...]:
    """The documented key names present (top level and Payload), in their observed
    casing. Unknown keys are never returned; a malformed body gives ()."""
    try:
        data = loads_strict(resp.content)
    except Exception:
        return ()
    if not isinstance(data, dict):
        return ()
    found: list[str] = []
    payload: object = None
    for k, v in data.items():
        if k.casefold() in DOCUMENTED_RESPONSE_KEYS:
            found.append(k)
            if k.casefold() == "payload":
                payload = v
    if isinstance(payload, dict):
        found.extend(k for k in payload if k.casefold() in DOCUMENTED_RESPONSE_KEYS)
    return tuple(found)


def classify_response(resp: httpx.Response, recipient: str | None = None) -> SendOutcome:
    """``recipient``: the normalised number this request was sent to."""
    code = resp.status_code
    if 200 <= code < 300:
        return _decode_2xx(resp, recipient)
    if 300 <= code < 400:
        return SendOutcome("UNCERTAIN", "3xx", None)
    if 400 <= code < 500:
        try:
            body = resp.json()
        except Exception:
            return SendOutcome("UNCERTAIN", "malformed_4xx", None)
        if isinstance(body, dict):
            return SendOutcome("REJECTED", "4xx", None)
        return SendOutcome("UNCERTAIN", "malformed_4xx", None)
    if code >= 500:
        return SendOutcome("UNCERTAIN", "5xx", None)
    return SendOutcome("UNCERTAIN", "1xx", None)


# ── gateways ──────────────────────────────────────────────────────────────────


class SmsGateway(Protocol):
    provenance: str

    async def send(self, to: str, body: str) -> SendOutcome: ...


@dataclass
class StubGateway:
    """Labelled SIMULATED gateway. Refuses every recipient except the test number."""

    test_number: str | None
    script: list[SendOutcome | Exception] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.provenance = "stub"  # never leaves the process

    async def send(self, to: str, body: str) -> SendOutcome:
        if self.test_number is None or to != self.test_number:
            return SendOutcome("NOT_SENT", "not_test_number", None)
        self.calls.append((to, body))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if item.state == "SUBMITTED" and item.provider_message_id is None:
                return SendOutcome("SUBMITTED", None, f"stub-{len(self.calls)}")
            return item
        return SendOutcome("SUBMITTED", None, f"stub-{len(self.calls)}")


class NotifyreGateway:
    """One-attempt Notifyre SMS client: no redirects, no transport retries, no
    retry decorator. Payload shape follows the legacy client; not used live until
    the contract is verified (see module docstring)."""

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        test_number: str | None,
        base_url: str = NOTIFYRE_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.provenance = "real"  # a Notifyre HTTP attempt
        self.last_keys: tuple[str, ...] = ()
        self.test_number = test_number
        self._sender = sender
        self.transport_retries = 0
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"x-api-token": api_key},
            follow_redirects=False,
            transport=transport or httpx.AsyncHTTPTransport(retries=self.transport_retries),
            timeout=timeout,
        )

    async def send(self, to: str, body: str) -> SendOutcome:
        if self.test_number is None or to != self.test_number:
            return SendOutcome("NOT_SENT", "not_test_number", None)
        # N1: documented key casing (docs.notifyre.com, Send SMS curl sample).
        # The recipient type is ambiguous in the docs; settled by the live test.
        payload = {"Body": body, "From": self._sender,
                   "Recipients": [{"type": "mobile_number", "value": to}]}
        try:
            resp = await self.client.post("/sms/send", json=payload)
        except Exception as exc:
            return classify_exception(exc)
        self.last_keys = observed_response_keys(resp)
        return classify_response(resp, recipient=to)


# ── phone identity (opt-out) ──────────────────────────────────────────────────


def phone_key(secret: str, e164: str) -> str:
    """Stable, non-reversible identity for the one configured number (keyed HMAC;
    the key is a per-install secret in demo_meta)."""
    return "hmac-sha256:" + hmac.new(secret.encode(), e164.encode(), hashlib.sha256).hexdigest()


async def _phone_secret(conn: Any) -> str:
    value = (await conn.execute(
        text("SELECT value FROM demo_meta WHERE key = 'phone_key_secret'")
    )).scalar_one()
    return str(value)


def gateway_provenance(gateway: object) -> str | None:
    """Provenance from trusted construction: the known gateway classes set it in
    their constructors. Anything else, or a mismatch, is unknown (fail closed)."""
    p = getattr(gateway, "provenance", None)
    if isinstance(gateway, NotifyreGateway) and p == "real":
        return "real"
    if isinstance(gateway, StubGateway) and p == "stub":
        return "stub"
    return None


async def record_opt_out(own: Ownership, e164: str) -> None:
    # exclusive: serialises with in-flight dispatches (see _dispatch)
    async with own.fenced(exclusive=True) as conn:
        key = phone_key(await _phone_secret(conn), e164)
        await conn.execute(text("INSERT INTO opt_outs(phone_key) VALUES (:k) "
                                "ON CONFLICT (phone_key) DO NOTHING"), {"k": key})


# ── pipeline ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SendResult:
    reminder_id: int | None
    state: str
    reason: str | None


def _clinic_day(now: datetime, tz: str) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


async def _mark(own: Ownership, rid: int, state: str, reason: str | None,
                provider_id: str | None = None) -> None:
    """Commit the outcome on the reminder and on its contact-history row together,
    under the match lock (established order: fence, then match lock), because
    the outcome changes candidate membership and status."""
    async with own.fenced() as conn:
        await conn.execute(text(
            "SELECT id FROM demo_match_lock WHERE id = 1 FOR UPDATE /* match:outcome */"))
        await conn.execute(text(
            "UPDATE reminder_records SET state = :s, reason = :r, provider_message_id = :p,"
            " updated_at = now() WHERE id = :id AND state = 'CLAIMED'"
        ), {"s": state, "r": reason, "p": provider_id, "id": rid})
        await conn.execute(text(
            "UPDATE contact_history SET outcome = :s WHERE contact_seq ="
            " (SELECT contact_seq FROM reminder_records WHERE id = :id) AND outcome IS NULL"
        ), {"s": state, "id": rid})


async def _admit_contact(own: Ownership, rid: int, key: str, appt_start: datetime,
                         provenance: str, hook: DispatchHook | None) -> None:
    """Contact admission (v1r2): membership is ordered by the match lock, and
    ``contact_seq`` is drawn only after it is held."""
    async with own.fenced() as conn:
        await conn.execute(text(
            "SELECT id FROM demo_match_lock WHERE id = 1 FOR UPDATE /* match:contact */"))
        if hook is not None:
            await hook("contact_locked")
        seq = (await conn.execute(text("SELECT nextval('match_seq')"))).scalar_one()
        await conn.execute(text(
            "INSERT INTO contact_history(contact_seq, phone_key, reminder_id, appt_start,"
            " provenance) VALUES (:q, :k, :r, :a, :p)"
        ), {"q": seq, "k": key, "r": rid, "a": appt_start, "p": provenance})
        await conn.execute(text("UPDATE reminder_records SET contact_seq = :q WHERE id = :r"),
                           {"q": seq, "r": rid})


DispatchHook = Callable[[str], Awaitable[None]]


async def _dispatch(
    own: Ownership,
    gateway: SmsGateway,
    to: str,
    body: str,
    day: date,
    tz: str,
    now: Callable[[], datetime],
    hook: DispatchHook | None,
) -> SendOutcome:
    """The last checks and the one request, inside the tracked external task.

    Linearisation (Codex reviews of b8e8bd1 and 9dbf74c): a read-only
    transaction holds the settings row FOR SHARE from the checks through the
    provider request. Pause and opt-out writers take that row FOR UPDATE, so a
    writer either commits before this lock (the send is blocked) or waits until
    this request has been made (the change applies to later sends). The owner
    epoch is checked under the same lock. After every await, a final
    synchronous ownership/closing admission runs immediately before the
    request. ``hook`` exists for tests only: points "before_lock", "locked",
    "before_send".
    """
    if hook is not None:
        await hook("before_lock")
    try:
        conn = await own.engine.connect()
    except DBAPIError:
        return SendOutcome("NOT_SENT", "dispatch_check_failed", None)
    trans = None
    try:
        try:
            trans = await conn.begin()
        except DBAPIError:
            return SendOutcome("NOT_SENT", "dispatch_check_failed", None)
        try:
            row = (await conn.execute(text(
                "SELECT paused, owner_epoch FROM demo_settings WHERE id = 1 FOR SHARE"
                " /* dispatch:gate */"
            ))).one()
            if hook is not None:
                await hook("locked")
            if row.owner_epoch != own.epoch:
                own._mark_lost(f"epoch {row.owner_epoch} != {own.epoch} at dispatch")
                raise OwnerLost("epoch changed before dispatch")
            if row.paused:
                return SendOutcome("NOT_SENT", "paused", None)
            key = phone_key(await _phone_secret(conn), to)
            opted = (await conn.execute(
                text("SELECT 1 FROM opt_outs WHERE phone_key = :k /* dispatch:gate */"),
                {"k": key},
            )).scalar_one_or_none()
            if opted is not None:
                return SendOutcome("NOT_SENT", "opted_out", None)
        except DBAPIError:
            return SendOutcome("NOT_SENT", "dispatch_check_failed", None)
        if _clinic_day(now(), tz) != day:  # clock read last, closest to the request
            return SendOutcome("NOT_SENT", "quota_day_expired", None)
        if hook is not None:
            await hook("before_send")
        # Final synchronous admission: no await between this check and the request.
        if own.owner_lost or own.closing:
            raise OwnerLost("owner lost or closing at dispatch")
        try:
            return await gateway.send(to, body)
        except Exception as exc:
            return classify_exception(exc)
    finally:
        if trans is not None:
            with contextlib.suppress(Exception):
                await trans.rollback()
        with contextlib.suppress(Exception):
            await conn.close()


async def send_one(
    own: Ownership,
    settings: DemoSettings,
    appt: SyntheticAppointment,
    patient: SyntheticPatient,
    gateway: SmsGateway,
    *,
    tenant_id: str,
    now: Callable[[], datetime],
    before_call: Callable[[], None] | None = None,
    dispatch_hook: DispatchHook | None = None,
) -> SendResult:
    rid = await claim_reminder(own, tenant_id, appt)
    if rid is None:
        return SendResult(None, "SKIPPED", "already_claimed")
    provenance = gateway_provenance(gateway)
    if provenance is None:  # fail closed: no reservation, no contact, no call
        await _mark(own, rid, "NOT_SENT", "unknown_gateway")
        return SendResult(rid, "NOT_SENT", "unknown_gateway")
    to = settings.sms_test_number
    tz = settings.clinic_tz
    key: str | None = None

    # 2. preconditions + reservation, one fenced transaction
    reason: str | None = None
    day = _clinic_day(now(), tz)
    try:
        async with own.fenced() as conn:
            paused = (await conn.execute(
                text("SELECT paused FROM demo_settings WHERE id = 1"))).scalar_one()
            if to is None:
                reason = "no_test_number"
            elif paused:
                reason = "paused"
            else:
                key = phone_key(await _phone_secret(conn), to)
                opted = (await conn.execute(
                    text("SELECT 1 FROM opt_outs WHERE phone_key = :k"), {"k": key}
                )).scalar_one_or_none()
                if opted is not None:
                    reason = "opted_out"
            if reason is None:
                used = (await conn.execute(text(
                    "INSERT INTO send_quota(clinic_day, used) VALUES (:d, 1) "
                    "ON CONFLICT (clinic_day) DO UPDATE SET used = send_quota.used + 1 "
                    "WHERE send_quota.used < :cap RETURNING used"
                ), {"d": day, "cap": settings.sms_daily_cap})).scalar_one_or_none()
                if used is None:
                    reason = "daily_cap"
                else:  # key was computed by the opt-out check above (to is set)
                    await conn.execute(text(
                        "UPDATE reminder_records SET reserved_day = :d, mode = :m,"
                        " phone_key = :k WHERE id = :id"),
                        {"d": day, "m": provenance, "k": key, "id": rid})
            if reason is not None:
                await conn.execute(text(
                    "UPDATE reminder_records SET state = 'NOT_SENT', reason = :r,"
                    " updated_at = now() WHERE id = :id"), {"r": reason, "id": rid})
    except DBAPIError:
        await _mark(own, rid, "NOT_SENT", "counter_unavailable")
        return SendResult(rid, "NOT_SENT", "counter_unavailable")
    if reason is not None:
        return SendResult(rid, "NOT_SENT", reason)
    assert to is not None

    # 3. quota-day revalidation immediately before the call
    if _clinic_day(now(), tz) != day:
        await _mark(own, rid, "NOT_SENT", "quota_day_expired")
        return SendResult(rid, "NOT_SENT", "quota_day_expired")

    # 3b. contact admission (v1r2), committed before dispatch
    assert key is not None
    if own.owner_lost or own.closing:
        return SendResult(rid, "CLAIMED", "owner_unavailable")
    try:
        await _admit_contact(own, rid, key, appt.start_utc, provenance, dispatch_hook)
    except OwnerLost:
        return SendResult(rid, "CLAIMED", "owner_unavailable")
    except DBAPIError:  # no contact recorded, so nothing may be sent
        await _mark(own, rid, "NOT_SENT", "contact_unavailable")
        return SendResult(rid, "NOT_SENT", "contact_unavailable")
    if dispatch_hook is not None:
        await dispatch_hook("contact_admitted")

    # 4. one guarded attempt
    if before_call is not None:
        before_call()
    body = reminder_text(first_name_of(patient), appt.start_utc, tz)

    async def _call() -> SendOutcome:
        return await _dispatch(own, gateway, to, body, day, tz, now, dispatch_hook)

    call: Callable[[], Awaitable[SendOutcome]] = _call
    try:
        outcome = await own.guard_external(call)
    except OwnerLost:
        return SendResult(rid, "CLAIMED", "owner_unavailable")

    # 5. commit the outcome immediately
    try:
        await _mark(own, rid, outcome.state, outcome.reason, outcome.provider_message_id)
    except OwnerLost:
        return SendResult(rid, "CLAIMED", "owner_unavailable")
    return SendResult(rid, outcome.state, outcome.reason)


async def send_batch(
    own: Ownership,
    settings: DemoSettings,
    eligible: list[Eligible],
    gateway: SmsGateway,
    *,
    tenant_id: str,
    now: Callable[[], datetime],
) -> list[SendResult]:
    """Sequential; each appointment's outcome is committed independently (T25)."""
    return [await send_one(own, settings, e.appointment, e.patient, gateway,
                           tenant_id=tenant_id, now=now) for e in eligible]
