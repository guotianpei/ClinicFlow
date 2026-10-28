"""DB-backed staff service.

Day 3: list, preview and pause. Day 4: send batch (stub). Day 5:
- send to one appointment;
- simulated replies (in-process, labelled, through the same verifier);
- process replies;
- follow-up items;
- write-back Run and Reconcile.

Mode labels are what this build can actually do. There is no real SMS, EHR or
callback path, so every label is SIMULATED whatever the settings request.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, NamedTuple

from sqlalchemy import text

from haloflow_demo.athena_source import (
    EHR_LABEL,
    AthenaConfig,
    AthenaPreviewClient,
    SourceInvalid,
    fetch_snapshot,
)
from haloflow_demo.inbound import ReceiveResult, process_pending, receive
from haloflow_demo.noresponse import flag_no_response
from haloflow_demo.ownership import Ownership
from haloflow_demo.selection import clinic_target_date, format_local, select_eligible
from haloflow_demo.sending import SmsGateway, StubGateway, phone_key, send_batch, send_one
from haloflow_demo.settings import DemoSettings
from haloflow_demo.synthetic_ehr import Observation, SyntheticEhr, WriteResult, default_fixtures
from haloflow_demo.tasks import reconcile_task, run_task

logger = logging.getLogger(__name__)

TENANT_ID = "demo-clinic"
LABELS = {"sms": "SIMULATED", "ehr": "SIMULATED", "callback": "SIMULATED"}
SIMULATED_CORRELATION_LABEL = "SIMULATED — correlated by the demo, not by SMS"

# What staff see. A submission is never shown as delivered: no delivery
# callback is in scope (R-12).
STATE_LABELS = {"CLAIMED": "Sending", "SUBMITTED": "Submitted", "NOT_SENT": "Not sent",
                "REJECTED": "Rejected by provider", "UNCERTAIN": "Uncertain (not re-sent)",
                "CONFIRMED": "Confirmed by patient",
                "RESCHEDULE_REQUESTED": "Reschedule requested — call patient",
                "CANCEL_REQUESTED": "Cancel requested — staff to cancel in EHR",
                "NO_RESPONSE": "No reply — call patient"}


def _label(state: str | None) -> str:
    return "none" if state is None else STATE_LABELS.get(state, state)


# Staff-screen package v1/v1r1 (G-E3-1/2/3, G-R1-1). Display only: every guard
# stays server-side.
REPLY_STATES = frozenset({"SUBMITTED", "NO_RESPONSE"})  # R-S2 (NO_RESPONSE: late reply)
BEFORE_RESTART = " (before restart)"                   # R-S5
OPTED_OUT_SUFFIX = "; Opted out — no texts"            # R-S4 (owner decision OD-S2)


class _ReminderRow(NamedTuple):
    id: int
    emr_appt_id: str
    emr_patient_id: str
    appt_start: datetime
    state: str
    phone_key: str | None


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class ScenarioUnavailable(LookupError):
    """The appointment belongs to no synthetic scenario in this process (another
    owner epoch, i.e. before a restart, or a date never loaded). Fail closed."""


class _EhrRouter:
    """Routes a call to the synthetic EHR that created the appointment.

    Appointment IDs are qualified by owner epoch and clinic date, so they are
    unique. The index maps each exact ID to its one EHR instance, and nothing is
    reconstructed from an ID: a task from an earlier process cannot reach
    regenerated fixtures.
    """

    supports_conditional_write = True
    conditional_contract_verified = False  # synthetic: proves nothing about a real EHR

    def __init__(self) -> None:
        self._index: dict[str, SyntheticEhr] = {}

    def register(self, ehr: SyntheticEhr) -> None:
        for a in ehr.fixtures.appointments:
            if a.emr_appt_id in self._index and self._index[a.emr_appt_id] is not ehr:
                raise RuntimeError("duplicate synthetic appointment id")
            self._index[a.emr_appt_id] = ehr

    def knows(self, appt_id: str) -> bool:
        return appt_id in self._index

    def _find(self, appt_id: str) -> SyntheticEhr:
        ehr = self._index.get(appt_id)
        if ehr is None:
            raise ScenarioUnavailable("appointment not in this process's synthetic scenario")
        return ehr

    async def read_confirmation(self, appt_id: str) -> Observation:
        return await self._find(appt_id).read_confirmation(appt_id)

    async def write_confirmation_conditional(
        self, appt_id: str, target: str, expected_version: int
    ) -> WriteResult:
        return await self._find(appt_id).write_confirmation_conditional(
            appt_id, target, expected_version)


class DbStaffService:
    def __init__(
        self,
        own: Ownership,
        settings: DemoSettings,
        *,
        now: Callable[[], datetime] = _utcnow,
        gateway: SmsGateway | None = None,
        athena: AthenaConfig | None = None,
        athena_transport: Any = None,
    ) -> None:
        self._own = own
        self._settings = settings
        self._now = now
        self._gateway: SmsGateway = gateway or StubGateway(settings.sms_test_number)
        self._ehr: dict[date, SyntheticEhr] = {}
        self.ehr = _EhrRouter()
        # N-ATH: read-only sandbox source (None = synthetic fixtures, unchanged).
        self._athena = athena
        self._athena_transport = athena_transport
        self._athena_client: AthenaPreviewClient | None = None
        self._source_locks: dict[date, asyncio.Lock] = {}
        self._source_meta: dict[date, dict[str, Any]] = {}

    def _target(self) -> date:
        return clinic_target_date(self._now(), self._settings.clinic_tz,
                                  days_before=self._settings.reminder_days_before)

    def _ehr_for(self, target: date) -> SyntheticEhr:
        if target not in self._ehr:
            scenario = f"e{self._own.epoch}-{target:%Y%m%d}"
            ehr = SyntheticEhr(default_fixtures(target, self._settings.clinic_tz, scenario))
            self.ehr.register(ehr)
            self._ehr[target] = ehr
        return self._ehr[target]

    async def _source(self, target: date) -> SyntheticEhr | None:
        """The EHR for ``target``: synthetic fixtures, or (athena mode) one sandbox
        snapshot per (process, date), fetched once under a per-date lock. A failed
        fetch is remembered for the date (no synthetic fallback); None means
        "no appointments available"."""
        if self._athena is None:
            return self._ehr_for(target)
        lock = self._source_locks.setdefault(target, asyncio.Lock())
        async with lock:
            if target in self._ehr:
                return self._ehr[target]
            meta = self._source_meta.get(target)
            if meta is not None and meta["error"] is not None:
                return None
            if self._athena_client is None:
                self._athena_client = AthenaPreviewClient(self._athena,
                                                          transport=self._athena_transport)
            try:
                snap = await fetch_snapshot(self._athena_client, target,
                                            self._settings.clinic_tz,
                                            self._athena.department_id, self._own.epoch)
            except SourceInvalid as exc:
                self._source_meta[target] = {"error": exc.code, "truncated": False,
                                             "counts": {}, "taken_at": None}
                return None
            ehr = SyntheticEhr(snap.fixtures)
            self.ehr.register(ehr)
            self._ehr[target] = ehr
            self._source_meta[target] = {"error": None, "truncated": snap.truncated,
                                         "counts": dict(snap.counts),
                                         "taken_at": snap.taken_at}
            return ehr

    def _source_block(self, target: date) -> dict[str, Any]:
        if self._athena is None:
            return {"kind": "synthetic"}
        meta = self._source_meta.get(target, {})
        taken = meta.get("taken_at")
        return {"kind": "athena_preview", "error": meta.get("error"),
                "truncated": bool(meta.get("truncated", False)),
                "counts": dict(meta.get("counts", {})),
                "snapshot_taken": format_local(taken, self._settings.clinic_tz)
                if taken is not None else None,
                "tz": self._settings.clinic_tz}

    def ehr_for_date(self, target: date) -> SyntheticEhr:
        """The synthetic EHR this process created for ``target`` (tests, inspection)."""
        return self._ehr[target]

    async def _reminder_rows(self) -> list[_ReminderRow]:
        """This tenant's reminders, ordered by id (tenant filter kept, Codex review)."""
        async with self._own.engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT id, emr_appt_id, emr_patient_id, appt_start, state, phone_key"
                     " FROM reminder_records WHERE tenant_id = :t ORDER BY id"),
                {"t": TENANT_ID},
            )).all()
        return [_ReminderRow(int(r[0]), str(r[1]), str(r[2]), r[3], str(r[4]),
                             None if r[5] is None else str(r[5])) for r in rows]

    async def _opt_out_state(self) -> tuple[set[str], str | None]:
        """R-S4: opted-out phone keys, and the key any send would use (the one
        configured test number), or None when no test number is configured."""
        async with self._own.engine.connect() as conn:
            opted = {str(r[0]) for r in (await conn.execute(
                text("SELECT phone_key FROM opt_outs"))).all()}
            to = self._settings.sms_test_number
            test_key = None
            if to is not None:
                secret = (await conn.execute(
                    text("SELECT value FROM demo_meta WHERE key = 'phone_key_secret'")
                )).scalar_one()
                test_key = phone_key(str(secret), to)
        return opted, test_key

    async def mark_item_done(self, item_id: int) -> bool:
        """R-S3: open -> done, fenced; False when the item is unknown or already done."""
        async with self._own.fenced() as conn:
            result = await conn.execute(
                text("UPDATE follow_up_items SET status = 'done'"
                     " WHERE id = :id AND status = 'open'"), {"id": item_id})
        return bool(result.rowcount == 1)

    async def _existing_ids(self, appts: list[Any]) -> set[str]:
        """IDs to skip as ALREADY_HAS_REMINDER: an exact-ID reminder, or (F-R1-1) any
        reminder for the same slot — synthetic patient and start instant — from any
        epoch. Shared by preview, single send and batch so all three agree."""
        async with self._own.engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT emr_appt_id, emr_patient_id, appt_start FROM reminder_records"
                     " WHERE tenant_id = :t"), {"t": TENANT_ID},
            )).all()
        ids = {str(r[0]) for r in rows}
        slots = {(str(r[1]), r[2]) for r in rows}
        return ids | {a.emr_appt_id for a in appts if (a.emr_patient_id, a.start_utc) in slots}

    async def _paused(self) -> bool:
        async with self._own.engine.connect() as conn:
            value = (await conn.execute(
                text("SELECT paused FROM demo_settings WHERE id = 1")
            )).scalar_one()
        return bool(value)

    async def _lists(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                    list[dict[str, Any]]]:
        tz = self._settings.clinic_tz
        async with self._own.engine.connect() as conn:
            inbound = [
                {"masked_from": r.masked_from, "text": r.body, "cls": r.cls,
                 "state": r.state, "disposition": r.disposition,
                 "received": format_local(r.received_at, tz),
                 "label": SIMULATED_CORRELATION_LABEL if r.simulated_correlation is not None
                 else "SIMULATED"}
                for r in (await conn.execute(text(
                    "SELECT masked_from, body, cls, state, disposition, received_at,"
                    " simulated_correlation FROM inbound_messages ORDER BY id DESC LIMIT 50"
                ))).all()]
            items = [
                {"id": r.id, "kind": r.kind, "detail": r.detail, "appointment": r.emr_appt_id,
                 "status": r.status}
                for r in (await conn.execute(text(
                    "SELECT f.id, f.kind, f.detail, f.status, r.emr_appt_id"
                    " FROM follow_up_items f"
                    " LEFT JOIN reminder_records r ON r.id = f.reminder_id"
                    " ORDER BY (f.status <> 'open'), f.id"   # R-S3: open first
                ))).all()]
            tasks = [
                {"task_id": r.id, "appointment": r.emr_appt_id, "state": r.state,
                 "reason": r.reason}
                for r in (await conn.execute(text(
                    "SELECT t.id, t.state, t.reason, r.emr_appt_id FROM writeback_tasks t"
                    " JOIN reminder_records r ON r.id = t.reminder_id ORDER BY t.id"
                ))).all()]
        return inbound, items, tasks

    async def page_data(self) -> dict[str, Any]:
        target = self._target()
        ehr = await self._source(target)
        tz = self._settings.clinic_tz
        if ehr is None:
            inbound, items, tasks = await self._lists()
            return {"paused": await self._paused(), "target_date": target.isoformat(),
                    "appointments": [], "inbound": inbound, "items": items, "tasks": tasks,
                    "labels": self._labels(), "source": self._source_block(target)}
        appts = await ehr.get_appointments_by_date(target, tz)
        reminders = await self._reminder_rows()
        by_id = {r.emr_appt_id: r for r in reminders}
        by_slot: dict[tuple[str, datetime], _ReminderRow] = {}
        for r in reminders:                       # ordered by id: the highest id wins
            by_slot[(r.emr_patient_id, r.appt_start)] = r
        eligible = {e.appointment.emr_appt_id for e in select_eligible(
            appts, ehr.patients, target, tz,
            existing_appt_ids=await self._existing_ids(appts)).eligible}  # R-S1 = Preview
        opted, test_key = await self._opt_out_state()
        rows = []
        for a in appts:
            exact = by_id.get(a.emr_appt_id)
            rem = exact if exact is not None else by_slot.get((a.emr_patient_id, a.start_utc))
            label = _label(rem.state if rem else None)
            if rem is not None and exact is None:
                label += BEFORE_RESTART                                    # R-S5
            patient = ehr.patients.get(a.emr_patient_id)
            key = (rem.phone_key if rem is not None and rem.phone_key is not None
                   else test_key if patient is not None and patient.phone_alias is not None
                   else None)
            opted_out = key is not None and key in opted                   # R-S4
            rows.append({
                "emr_appt_id": a.emr_appt_id,
                "reminder_id": exact.id if exact else None,
                "patient": patient.display_name if patient is not None else "(unknown)",
                "when": format_local(a.start_utc, tz),
                "status": f"{a.status.value}; reminder: {label}"
                          + (OPTED_OUT_SUFFIX if opted_out else ""),
                "eligible": a.emr_appt_id in eligible,
                "can_reply": exact is not None and exact.state in REPLY_STATES,  # R-S2
                "opted_out": opted_out,
            })
        inbound, items, tasks = await self._lists()
        return {"paused": await self._paused(), "target_date": target.isoformat(),
                "appointments": rows, "inbound": inbound, "items": items, "tasks": tasks,
                "labels": self._labels(), "source": self._source_block(target)}

    def _labels(self) -> dict[str, str]:
        labels = dict(LABELS)
        if self._athena is not None:
            labels["ehr"] = EHR_LABEL
        return labels

    async def preview_batch(self) -> list[dict[str, Any]]:
        """Read-only: no claim, no reservation, no external send."""
        target = self._target()
        ehr = await self._source(target)
        if ehr is None:
            return []
        tz = self._settings.clinic_tz
        appts = await ehr.get_appointments_by_date(target, tz)
        result = select_eligible(appts, ehr.patients, target, tz,
                                 existing_appt_ids=await self._existing_ids(appts))
        decision: dict[str, str] = {e.appointment.emr_appt_id: "eligible" for e in result.eligible}
        decision.update({s.emr_appt_id: f"skip: {s.reason.value}" for s in result.skipped})
        return [{"emr_appt_id": a.emr_appt_id, "decision": decision[a.emr_appt_id]}
                for a in appts if a.emr_appt_id in decision]

    async def _eligible(self) -> list[Any]:
        target = self._target()
        ehr = await self._source(target)
        if ehr is None:
            return []
        tz = self._settings.clinic_tz
        appts = await ehr.get_appointments_by_date(target, tz)
        return select_eligible(appts, ehr.patients, target, tz,
                               existing_appt_ids=await self._existing_ids(appts)).eligible

    async def send_batch(self) -> list[dict[str, Any]]:
        """Send to every eligible appointment on the target date (stub gateway).
        Refused (no send) while the sandbox snapshot is TRUNCATED (A-3c)."""
        if self._athena is not None:
            target = self._target()
            await self._source(target)
            if self._source_meta.get(target, {}).get("truncated"):
                return []
        sent = await send_batch(self._own, self._settings, await self._eligible(),
                                self._gateway, tenant_id=TENANT_ID, now=self._now)
        return [{"reminder_id": r.reminder_id, "state": r.state, "reason": r.reason}
                for r in sent]

    async def send_to_appointment(self, emr_appt_id: str) -> dict[str, Any]:
        """Send one reminder, so the demo phone has a single contact."""
        for e in await self._eligible():
            if e.appointment.emr_appt_id == emr_appt_id:
                r = await send_one(self._own, self._settings, e.appointment, e.patient,
                                   self._gateway, tenant_id=TENANT_ID, now=self._now)
                return {"reminder_id": r.reminder_id, "state": r.state, "reason": r.reason}
        return {"reminder_id": None, "state": "SKIPPED", "reason": "not_eligible"}

    async def receive_http(self, raw: bytes, signature_headers: list[str]) -> ReceiveResult:
        """The callback route's receiver: never carries a simulated correlation."""
        return await receive(self._own, self._settings, raw, signature_headers, now=self._now)

    async def simulate_reply(self, reminder_id: int, message: str) -> ReceiveResult:
        """A labelled simulated reply from the test number, signed with the demo
        secret and passed through the same verifier, with an in-process correlation."""
        at = self._now()
        ts = int(at.timestamp())
        to = self._settings.sms_test_number
        sender = self._settings.sms_sender
        if to is None or sender is None:
            return ReceiveResult(400, "numbers_unset", None)
        raw = json.dumps({"Event": "sms_received", "Timestamp": ts, "Payload": {
            "ReplyID": "sim-" + secrets.token_hex(12), "RecipientID": "sim",
            "RecipientNumber": sender, "SenderNumber": to, "Message": message[:1600],
            "ReceivedDateUtc": ts}}).encode()
        secret = self._settings.callback_secret
        v = hmac.new(secret.encode(), str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()
        return await receive(self._own, self._settings, raw, [f"t={ts},v={v}"], now=lambda: at,
                             simulated_correlation=reminder_id)

    async def process_now(self) -> list[tuple[int, str | None]]:
        return await process_pending(self._own, self._settings, self.ehr, now=self._now)

    async def check_no_response(self) -> list[int]:
        """Day 6: flag overdue unanswered reminders (no external call)."""
        return await flag_no_response(self._own, self._now)

    async def run_writeback(self, task_id: int) -> str:
        return await run_task(self._own, self.ehr, task_id)

    async def reconcile_writeback(self, task_id: int) -> str:
        return await reconcile_task(self._own, self.ehr, task_id)

    async def dispatcher_loop(self, interval: float = 2.0) -> None:
        """R-16: runs only after start-up; stops when closing or ownership is lost."""
        while not (self._own.closing or self._own.owner_lost):
            try:
                await self.process_now()
            except Exception as exc:
                # Class name only: DB errors can carry SQL and parameter values.
                logger.error("dispatcher iteration failed: %s", type(exc).__name__)
            if not (self._own.closing or self._own.owner_lost):
                try:
                    await self.check_no_response()
                except Exception as exc:
                    logger.error("no-response check failed: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._own.closing_event.wait(), timeout=interval)

    async def set_paused(self, paused: bool) -> None:
        async with self._own.fenced(exclusive=True) as conn:
            await conn.execute(text("UPDATE demo_settings SET paused = :p WHERE id = 1"),
                               {"p": paused})
