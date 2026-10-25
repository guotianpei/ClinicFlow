"""Day 5: inbound replies (package v1 + v1r1 + v1r2 + P-NS, Codex ALIGN).

Receipt (``receive``), in this order:
  1. Mode gate. Only the simulated callback runs (provenance 'stub'). Real
     ingress is refused (``callback_real_unverified``) until live signing and
     exposure are verified.
  2. Size cap, then authentication over the exact bytes, before any parsing:
     exactly one ``Notifyre-Signature`` header with exactly the parts ``t`` and
     ``v``; HMAC-SHA256(secret, t + "." + raw), constant-time compare, ±300 s.
     Fixtures prove the internal verifier only, not Notifyre's live signing.
  3. Strict JSON: duplicate and case-variant keys rejected, field types checked,
     ``RecipientNumber`` must be our configured sender.
  4. One fenced transaction under the match lock:
     - replay check by ReplyID plus content digest: the same digest → 204;
       a different one → 409 plus a ``replay_conflict`` item, effects untouched;
     - the inbound row (redacted text, ``cls``, ``receipt_seq`` drawn after the
       lock);
     - the immutable candidate snapshot: every same-provenance contact for this
       phone with ``contact_seq < receipt_seq`` that is not a certain
       no-message outcome, with its status at receipt.

Processing (``process_pending``) runs one processor per owner at a time.
  - For a uniquely matched CONFIRM, a baseline EHR read happens through
    ``guard_external``, outside any transaction. Failures are bounded
    (compare-and-set attempts); after three, the confirm is applied with no
    baseline and a staff item, and is never written.
  - Then one effect transaction: fence (exclusive for STOP), match lock, inbound
    row (SKIP LOCKED), candidate reminders, then the synchronous closing and
    owner admission check, after every lock wait. That check is the
    linearisation point. The decision is recomputed under the locks, and a
    baseline bound to another reminder is discarded.

Simulated correlation is an in-process argument only; the HTTP route cannot
supply it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

from haloflow_demo.errors import OwnerLost
from haloflow_demo.ownership import Ownership
from haloflow_demo.sending import phone_key
from haloflow_demo.settings import DemoSettings, mask_phone, normalize_us_phone
from haloflow_demo.strict_json import StrictJsonError, ci_get, loads_strict
from haloflow_demo.synthetic_ehr import Observation

MAX_BODY = 16 * 1024
MAX_TEXT = 1600
SIGNATURE_TOLERANCE_S = 300
MAX_BASELINE_ATTEMPTS = 3
CONFIRM_TARGET = "2"  # synthetic confirmation code (real code-2 semantics unverified)
PENDING_STATES = ("RECEIVED", "PENDING_RETRY")
INBOUND_PROVENANCE = "stub"  # the only ingress this build accepts (simulated callback)


class Cls(StrEnum):
    CONFIRM = "CONFIRM"
    RESCHEDULE = "RESCHEDULE"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    OPT_OUT = "OPT_OUT"
    UNCLEAR = "UNCLEAR"


_CLASSES = {
    "1": Cls.CONFIRM, "YES": Cls.CONFIRM, "Y": Cls.CONFIRM,
    "2": Cls.RESCHEDULE,
    "3": Cls.CANCEL_REQUEST, "CANCEL": Cls.CANCEL_REQUEST,  # owner: CANCEL = cancel request
    "STOP": Cls.OPT_OUT, "STOPALL": Cls.OPT_OUT, "UNSUBSCRIBE": Cls.OPT_OUT,
    "END": Cls.OPT_OUT, "QUIT": Cls.OPT_OUT,
}


def classify(message: str) -> Cls:
    """Q-B3: trim + upper-case only; anything else (incl. NO, START) is UNCLEAR."""
    return _CLASSES.get(message.strip().upper(), Cls.UNCLEAR)


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NUMBER = re.compile(r"\+?(?:\d[\s().-]*){6,}\d")


def redact(message: str) -> str:
    """Stored and displayed text: e-mails and 7+-digit runs removed, then bounded.
    Other identifiers (names, addresses) are NOT detected (stated gap)."""
    out = _NUMBER.sub("[number]", _EMAIL.sub("[email]", message))
    return out[:MAX_TEXT]


# Staff-facing instructions (not patient-facing).
INSTRUCTIONS = {
    "reschedule": "Patient asked to reschedule. Call the patient.",
    "cancel_request": "Patient asked to cancel. Cancel the appointment in the EHR, "
                      "then close this item.",
    "unclear": "Reply not understood. Review and contact the patient if needed.",
    "uncorrelated_reply": "Reply could answer more than one reminder sent to this phone. "
                          "Check before acting.",
    "late_reply": "Reply arrived after the appointment time.",
    "reply_after_resolution": "Patient replied again after an earlier answer. Review.",
    "reply_to_unverified_send": "Reply to a reminder whose send was not confirmed. Review.",
    "no_match": "Reply from a number with no reminder.",
    "writeback_no_baseline": "Confirmation recorded; the EHR was not updated automatically. "
                             "Update the EHR manually.",
    "replay_conflict": "A reply event was re-sent with different content. Nothing was changed.",
    "writeback_conflict": "The EHR value changed after the reply. Nothing was written. "
                          "Check the appointment.",
}


# ── receipt ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReceiveResult:
    status: int
    reason: str | None
    inbound_id: int | None


@dataclass(frozen=True)
class _Event:
    reply_id: str
    sender: str
    message: str


class _BadEvent(Exception):
    pass


_T = re.compile(r"[0-9]{1,12}")
_V = re.compile(r"[0-9a-f]{64}")


def _parse_signature(headers: list[str]) -> tuple[int, str] | None:
    if len(headers) != 1:
        return None
    parts = headers[0].split(",")
    if len(parts) != 2:
        return None
    found: dict[str, str] = {}
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep or key not in ("t", "v") or key in found:
            return None
        found[key] = value
    t, v = found.get("t"), found.get("v")
    if t is None or v is None or not _T.fullmatch(t) or not _V.fullmatch(v):
        return None
    return int(t), v


def _signature_ok(headers: list[str], raw: bytes, secret: str, now_ts: float) -> bool:
    parsed = _parse_signature(headers)
    if parsed is None:
        return False
    t, v = parsed
    expected = hmac.new(secret.encode(), str(t).encode() + b"." + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(v, expected):
        return False
    return abs(now_ts - t) <= SIGNATURE_TOLERANCE_S


def _parse_event(raw: bytes, our_number: str | None) -> _Event | None:
    """None means a well-formed event of another type (ignored)."""
    try:
        data = loads_strict(raw)
    except (StrictJsonError, ValueError) as exc:
        raise _BadEvent("json") from exc
    if not isinstance(data, dict):
        raise _BadEvent("not_object")
    event, ts, payload = ci_get(data, "Event"), ci_get(data, "Timestamp"), ci_get(data, "Payload")
    if not isinstance(event, str) or type(ts) is not int or not isinstance(payload, dict):
        raise _BadEvent("envelope")
    if event != "sms_received":
        return None
    reply_id = ci_get(payload, "ReplyID")
    sender_raw = ci_get(payload, "SenderNumber")
    recipient_raw = ci_get(payload, "RecipientNumber")
    message = ci_get(payload, "Message")
    received = ci_get(payload, "ReceivedDateUtc")
    if not isinstance(reply_id, str) or not 1 <= len(reply_id) <= 256:
        raise _BadEvent("reply_id")
    if not isinstance(sender_raw, str) or not isinstance(recipient_raw, str):
        raise _BadEvent("numbers")
    if not isinstance(message, str) or type(received) is not int:
        raise _BadEvent("fields")
    sender = normalize_us_phone(sender_raw)
    recipient = normalize_us_phone(recipient_raw)
    if sender is None:
        raise _BadEvent("sender")
    if our_number is None or recipient != our_number:
        raise _BadEvent("recipient")
    return _Event(reply_id, sender, message)


ReceiptHook = Callable[[str], Awaitable[None]]


async def receive(
    own: Ownership,
    settings: DemoSettings,
    raw: bytes,
    signature_headers: list[str],
    *,
    now: Callable[[], datetime],
    simulated_correlation: int | None = None,
    hook: ReceiptHook | None = None,
) -> ReceiveResult:
    """Authenticate, parse and store one reply (no processing here)."""
    if settings.callback_mode != "simulated":
        return ReceiveResult(503, "callback_real_unverified", None)
    secret = settings.callback_secret
    if not secret.strip():
        return ReceiveResult(503, "callback_secret_unset", None)
    if len(raw) > MAX_BODY:
        return ReceiveResult(413, "too_large", None)
    at = now()
    if not _signature_ok(signature_headers, raw, secret, at.timestamp()):
        return ReceiveResult(401, "signature", None)
    try:
        event = _parse_event(raw, settings.sms_sender)
    except _BadEvent as exc:
        return ReceiveResult(400, str(exc), None)
    if event is None:
        return ReceiveResult(204, "ignored_event", None)
    cls = classify(event.message)
    body = redact(event.message)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        async with own.fenced() as conn:
            await conn.execute(text(
                "SELECT id FROM demo_match_lock WHERE id = 1 FOR UPDATE /* match:receipt */"))
            if hook is not None:
                await hook("receipt_locked")
            existing = (await conn.execute(text(
                "SELECT id, content_digest FROM inbound_messages WHERE provider_event_id = :r"
            ), {"r": event.reply_id})).one_or_none()
            if existing is not None:
                if hmac.compare_digest(existing.content_digest, digest):
                    return ReceiveResult(204, "duplicate", int(existing.id))
                await _item(conn, f"inbound:{existing.id}", "replay_conflict",
                            inbound_id=int(existing.id), reminder_id=None)
                return ReceiveResult(409, "replay_conflict", int(existing.id))
            key_secret = (await conn.execute(text(
                "SELECT value FROM demo_meta WHERE key = 'phone_key_secret'"))).scalar_one()
            key = phone_key(str(key_secret), event.sender)
            seq = (await conn.execute(text("SELECT nextval('match_seq')"))).scalar_one()
            iid = (await conn.execute(text(
                "INSERT INTO inbound_messages(provider_event_id, content_digest, receipt_seq,"
                " provenance, simulated_correlation, phone_key, masked_from, body, cls, state,"
                " received_at) VALUES (:r, :d, :q, :p, :c, :k, :m, :b, :cls, 'RECEIVED', :at)"
                " RETURNING id"
            ), {"r": event.reply_id, "d": digest, "q": seq, "p": INBOUND_PROVENANCE,
                "c": simulated_correlation, "k": key, "m": mask_phone(event.sender),
                "b": body, "cls": cls.value, "at": at})).scalar_one()
            await conn.execute(text(
                "INSERT INTO inbound_candidates(inbound_id, contact_seq, reminder_id, appt_start,"
                " status_at_receipt)"
                " SELECT :i, h.contact_seq, r.id, h.appt_start, COALESCE(r.state, 'GONE')"
                " FROM contact_history h LEFT JOIN reminder_records r"
                "   ON r.id = h.reminder_id AND r.contact_seq = h.contact_seq"
                " WHERE h.phone_key = :k AND h.provenance = :p AND h.contact_seq < :q"
                "   AND (h.outcome IS NULL OR h.outcome NOT IN ('NOT_SENT', 'REJECTED'))"
            ), {"i": iid, "k": key, "p": INBOUND_PROVENANCE, "q": seq})
    except OwnerLost:
        return ReceiveResult(503, "owner_unavailable", None)
    except DBAPIError:
        return ReceiveResult(503, "store_unavailable", None)
    return ReceiveResult(204, None, int(iid))


async def _item(conn: Any, source_key: str, kind: str, *, inbound_id: int | None,
                reminder_id: int | None) -> None:
    await conn.execute(text(
        "INSERT INTO follow_up_items(source_key, kind, inbound_id, reminder_id, detail)"
        " VALUES (:s, :k, :i, :r, :d) ON CONFLICT ON CONSTRAINT uq_follow_up_source_kind"
        " DO NOTHING"
    ), {"s": source_key, "k": kind, "i": inbound_id, "r": reminder_id,
        "d": INSTRUCTIONS.get(kind)})


# ── processing ────────────────────────────────────────────────────────────────


class ReadOnlyEhr(Protocol):
    async def read_confirmation(self, appt_id: str) -> Observation: ...


ProcessHook = Callable[[str, int], Awaitable[None]]


@dataclass(frozen=True)
class _Cand:
    contact_seq: int
    reminder_id: int | None
    appt_start: datetime
    status_at_receipt: str


@dataclass(frozen=True)
class _Decision:
    kind: str  # "act" or a disposition / item kind
    reminder_id: int | None


_ACT_STATE = {Cls.CONFIRM: "CONFIRMED", Cls.RESCHEDULE: "RESCHEDULE_REQUESTED",
              Cls.CANCEL_REQUEST: "CANCEL_REQUESTED"}
_ACT_DISPOSITION = {Cls.CONFIRM: "confirm", Cls.RESCHEDULE: "reschedule",
                    Cls.CANCEL_REQUEST: "cancel_request"}


def _decide(cls: Cls, cands: list[_Cand], states: dict[int, str], received_at: datetime,
            correlation: int | None) -> _Decision:
    h = [c for c in cands if correlation is None or c.reminder_id == correlation]
    if cls is Cls.OPT_OUT:
        return _Decision("opt_out", None)
    if cls is Cls.UNCLEAR:
        return _Decision("unclear", h[0].reminder_id if len(h) == 1 else None)
    if not h:
        return _Decision("no_match", None)
    if len(h) >= 2:
        return _Decision("uncorrelated_reply", None)
    c = h[0]
    if c.status_at_receipt in ("CLAIMED", "UNCERTAIN"):
        return _Decision("reply_to_unverified_send", c.reminder_id)
    if c.appt_start <= received_at:
        return _Decision("late_reply", c.reminder_id)
    if c.reminder_id is None or c.status_at_receipt == "GONE":
        return _Decision("late_reply", None)
    if c.status_at_receipt == "SUBMITTED" and states.get(c.reminder_id) == "SUBMITTED":
        return _Decision("act", c.reminder_id)
    return _Decision("reply_after_resolution", c.reminder_id)


class _Stop(Exception):
    """Admission refused (closing / owner lost): leave the row pending."""


_STOP = object()
_LOCKS: weakref.WeakKeyDictionary[Ownership, asyncio.Lock] = weakref.WeakKeyDictionary()


async def process_pending(
    own: Ownership,
    settings: DemoSettings,
    ehr: ReadOnlyEhr,
    *,
    now: Callable[[], datetime],
    hook: ProcessHook | None = None,
) -> list[tuple[int, str | None]]:
    """Process each pending reply once, oldest first. One processor per owner."""
    lock = _LOCKS.setdefault(own, asyncio.Lock())
    async with lock:
        async with own.engine.connect() as conn:
            ids = [int(r[0]) for r in (await conn.execute(text(
                "SELECT id FROM inbound_messages WHERE state IN ('RECEIVED', 'PENDING_RETRY')"
                " ORDER BY id"))).all()]
        done: list[tuple[int, str | None]] = []
        for iid in ids:
            if own.owner_lost or own.closing:
                break
            result = await _process_one(own, ehr, iid, hook)
            if result is _STOP:
                break
            done.append((iid, result if isinstance(result, str) else None))
        return done


async def _load(own: Ownership, iid: int) -> tuple[Any, list[_Cand], dict[int, str]] | None:
    async with own.engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT id, cls, attempts, state, simulated_correlation, received_at"
            " FROM inbound_messages WHERE id = :i"), {"i": iid})).one_or_none()
        if row is None or row.state not in PENDING_STATES:
            return None
        cands = [_Cand(int(c.contact_seq), c.reminder_id, c.appt_start, str(c.status_at_receipt))
                 for c in (await conn.execute(text(
                     "SELECT contact_seq, reminder_id, appt_start, status_at_receipt"
                     " FROM inbound_candidates WHERE inbound_id = :i ORDER BY contact_seq"),
                     {"i": iid})).all()]
        states = await _states(conn, [c.reminder_id for c in cands], lock=False)
    return row, cands, states


async def _states(conn: Any, ids: list[int | None], *, lock: bool) -> dict[int, str]:
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    stmt = text("SELECT id, state FROM reminder_records WHERE id IN :ids ORDER BY id"
                + (" FOR UPDATE" if lock else "")).bindparams(bindparam("ids", expanding=True))
    return {int(r.id): str(r.state) for r in (await conn.execute(stmt, {"ids": wanted})).all()}


async def _process_one(own: Ownership, ehr: ReadOnlyEhr, iid: int,
                       hook: ProcessHook | None) -> object:
    loaded = await _load(own, iid)
    if loaded is None:
        return None
    row, cands, states = loaded
    cls = Cls(row.cls)
    provisional = _decide(cls, cands, states, row.received_at, row.simulated_correlation)
    if provisional.kind == "act" and cls is Cls.CONFIRM:
        assert provisional.reminder_id is not None
        outcome = await _baseline(own, ehr, iid, int(row.attempts), provisional.reminder_id, hook)
        if outcome is not None:
            return outcome
    if own.owner_lost or own.closing:
        return _STOP
    try:
        return await _effect(own, iid, cls, cands, hook)
    except _Stop:
        return _STOP
    except OwnerLost:
        return _STOP


async def _baseline(own: Ownership, ehr: ReadOnlyEhr, iid: int, seen_attempts: int,
                    target: int, hook: ProcessHook | None) -> object | None:
    """Read and persist the baseline bound to ``target``. Returns a value to stop
    processing this row now, or None to continue to the effect transaction."""
    if hook is not None:
        await hook("before_baseline", iid)
    if own.owner_lost or own.closing:
        return _STOP
    async with own.engine.connect() as conn:
        appt_id = (await conn.execute(text(
            "SELECT emr_appt_id FROM reminder_records WHERE id = :r"), {"r": target})).scalar_one()

    async def _read() -> Observation:
        return await ehr.read_confirmation(str(appt_id))

    failed = False
    obs: Observation | None = None
    try:
        obs = await own.guard_external(_read)
    except OwnerLost:
        if hook is not None:
            await hook("after_baseline", iid)
        return _STOP
    except Exception:
        failed = True
    async with own.fenced() as conn:
        if failed:
            new = (await conn.execute(text(
                "UPDATE inbound_messages SET attempts = attempts + 1, state = 'PENDING_RETRY',"
                " baseline_reminder_id ="
                "   CASE WHEN attempts + 1 >= :max THEN CAST(:t AS integer) END,"
                " baseline_ok = CASE WHEN attempts + 1 >= :max THEN false END,"
                " baseline_value = NULL, updated_at = now()"
                " WHERE id = :i AND attempts = :seen AND state IN ('RECEIVED', 'PENDING_RETRY')"
                " RETURNING attempts"
            ), {"i": iid, "seen": seen_attempts, "t": target,
                "max": MAX_BASELINE_ATTEMPTS})).scalar_one_or_none()
        else:
            assert obs is not None
            new = None
            await conn.execute(text(
                "UPDATE inbound_messages SET baseline_reminder_id = :t, baseline_value = :v,"
                " baseline_ok = :ok, updated_at = now()"
                " WHERE id = :i AND state IN ('RECEIVED', 'PENDING_RETRY')"
            ), {"i": iid, "t": target, "v": obs.value, "ok": obs.value is not None})
    if hook is not None:
        await hook("after_baseline", iid)
    if failed and (new is None or int(new) < MAX_BASELINE_ATTEMPTS):
        return "pending_retry"
    return None


async def _effect(own: Ownership, iid: int, cls: Cls, cands: list[_Cand],
                  hook: ProcessHook | None) -> object:
    async with own.fenced(exclusive=cls is Cls.OPT_OUT) as conn:
        await conn.execute(text(
            "SELECT id FROM demo_match_lock WHERE id = 1 FOR UPDATE /* match:effect */"))
        row = (await conn.execute(text(
            "SELECT id, cls, phone_key, attempts, simulated_correlation, received_at,"
            " baseline_reminder_id, baseline_value, baseline_ok"
            " FROM inbound_messages WHERE id = :i AND state IN ('RECEIVED', 'PENDING_RETRY')"
            " FOR UPDATE SKIP LOCKED"), {"i": iid})).one_or_none()
        if row is None:
            return None
        states = await _states(conn, [c.reminder_id for c in cands], lock=True)
        # Linearisation point: after every lock wait, before any effect.
        if own.owner_lost or own.closing:
            raise _Stop()
        d = _decide(cls, cands, states, row.received_at, row.simulated_correlation)
        disposition = await _apply(conn, row, cls, d)
        if disposition is None:  # baseline not bound to this target: re-queue
            await conn.execute(text(
                "UPDATE inbound_messages SET state = 'PENDING_RETRY', baseline_reminder_id = NULL,"
                " baseline_value = NULL, baseline_ok = NULL, updated_at = now() WHERE id = :i"
            ), {"i": iid})
            return "requeued"
        if hook is not None:
            await hook("effects_written", iid)
        await conn.execute(text(
            "UPDATE inbound_messages SET state = 'PROCESSED', disposition = :d, updated_at = now()"
            " WHERE id = :i"), {"d": disposition, "i": iid})
        return disposition


async def _apply(conn: Any, row: Any, cls: Cls, d: _Decision) -> str | None:
    iid = int(row.id)
    source = f"inbound:{iid}"
    if d.kind == "opt_out":
        await conn.execute(text("INSERT INTO opt_outs(phone_key) VALUES (:k)"
                                " ON CONFLICT (phone_key) DO NOTHING"), {"k": row.phone_key})
        return "opt_out"
    if d.kind != "act":
        await _item(conn, source, d.kind, inbound_id=iid, reminder_id=d.reminder_id)
        return d.kind
    rid = d.reminder_id
    assert rid is not None
    observed: str | None = None
    if cls is Cls.CONFIRM:
        if row.baseline_reminder_id != rid or row.baseline_ok is None:
            return None  # never apply another candidate's baseline
        observed = row.baseline_value if row.baseline_ok else None
    updated = (await conn.execute(text(
        "UPDATE reminder_records SET state = :s, updated_at = now()"
        " WHERE id = :r AND state = 'SUBMITTED' RETURNING id"
    ), {"s": _ACT_STATE[cls], "r": rid})).scalar_one_or_none()
    if updated is None:  # cannot happen under the row lock; stay conservative
        await _item(conn, source, "reply_after_resolution", inbound_id=iid, reminder_id=rid)
        return "reply_after_resolution"
    if cls is Cls.CONFIRM:
        await conn.execute(text(
            "INSERT INTO writeback_tasks(reminder_id, state, target_value, observed_before)"
            " VALUES (:r, 'PENDING', :t, :o) ON CONFLICT (reminder_id) DO NOTHING"
        ), {"r": rid, "t": CONFIRM_TARGET, "o": observed})
        if observed is None:
            await _item(conn, f"reminder:{rid}", "writeback_no_baseline", inbound_id=iid,
                        reminder_id=rid)
    else:
        await _item(conn, source, _ACT_DISPOSITION[cls], inbound_id=iid, reminder_id=rid)
    return _ACT_DISPOSITION[cls]


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
