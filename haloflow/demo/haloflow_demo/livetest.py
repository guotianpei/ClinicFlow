"""Outbound-once live SMS test (package v1r1 §G; Codex ALIGN 20261025T182721Z).

``cd haloflow && PYTHONPATH=demo python -m haloflow_demo live-sms-test``

A separate entry point. It never starts listeners, the dispatcher or any
server mode; ``DEMO_SMS_MODE=real`` and ``DEMO_CALLBACK_MODE=real`` stay
refused at start-up.

1. Arming (``DEMO_LIVE_TEST=outbound-once``) and local inputs are checked
   before any ownership, bootstrap or recovery.
   - The token is ``NOTIFYRE_API_KEY``. A local format check does not prove
     the provider will accept it.
   - The sender is ``DEMO_SMS_SENDER``.
   - The only recipient is ``DEMO_SMS_TEST_NUMBER``.
2. Full ownership: process lock, DB advisory lock, epoch, recovery.
3. Durable ``live_test_unresolved`` gate: any real contact that is in flight
   or uncertain, including one recovered from CLAIMED, refuses a new attempt
   before any claim, reservation or request. It survives restarts, dates and
   reset. There is no resolution path in this build.
4. One synthetic appointment: target date by the existing clinic-local
   policy, 09:00 clinic time, ID ``syn-e<epoch>-<yyyymmdd>-live-1``. It is
   sent through ``send_one`` with a ``NotifyreGateway`` (real provenance):
   durable atomic reservation (cap ≤ 10, no refunds), one attempt, no retry.
5. Output, one line: a fixed enum state and reason, whether a provider id
   was present (yes or no), and the recognised documented response key
   spellings. Never a number, token, body, value, unknown key or exception
   text.

Lifecycle (Codex 185431Z): after ownership, the whole operation (gate, claim,
reservation, contact admission, request, outcome commit, report lookup) runs
as ONE task tracked by the ownership drain, and signals are handled from that
point. On SIGTERM or SIGINT, closing begins and the bounded drain waits, then
cancels. If the operation ignores cancellation, the exit code is 4 and the locks
are not released explicitly (the kernel releases them when the process exits).
Cleanup (``own.close``) is hard-bounded by a deadline plus a cancellation grace
and is never joined beyond them. If cleanup survives, the exit code is 4. The
operation has ended by then, but cleanup may have released part of the
ownership before it stalled. There is never an unbounded wait after the drain.

Gate and operator policy are separate:
- the durable ``live_test_unresolved`` gate blocks after any attempt that may
  have reached the phone (in flight, uncertain or recovered);
- a definite NOT_SENT or REJECTED does not trigger the gate. By operator
  policy there is still no second attempt without a fresh, explicit owner
  confirmation.
"""

from __future__ import annotations

import asyncio
import re
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import text

from haloflow_demo.ownership import Ownership, acquire_ownership
from haloflow_demo.selection import clinic_target_date
from haloflow_demo.sending import NotifyreGateway, send_one
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url
from haloflow_demo.synthetic_ehr import ApptStatus, SyntheticAppointment, SyntheticPatient

ARMED = "outbound-once"
TENANT_ID = "demo-clinic"
EXIT_SUBMITTED, EXIT_OTHER, EXIT_REFUSED, EXIT_DRAIN_INCOMPLETE = 0, 1, 2, 4
_TOKEN = re.compile(r"[A-Za-z0-9._:\-]{8,512}")


def _say(**fields: str) -> None:
    print("live-sms-test: " + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


async def _unresolved(own: Ownership) -> bool:
    async with own.engine.connect() as conn:
        found = (await conn.execute(text(
            "SELECT 1 FROM contact_history WHERE provenance = 'real'"
            " AND (outcome IS NULL OR outcome = 'UNCERTAIN')"
            " UNION ALL SELECT 1 FROM reminder_records WHERE mode = 'real'"
            " AND state IN ('CLAIMED', 'UNCERTAIN') LIMIT 1"))).scalar_one_or_none()
    return found is not None


def _local_inputs(env: Mapping[str, str], s: DemoSettings) -> str | None:
    if not _TOKEN.fullmatch(env.get("NOTIFYRE_API_KEY", "")):
        return "live_test_inputs"
    if s.sms_sender is None or s.sms_test_number is None:
        return "live_test_inputs"
    try:
        cap = s.sms_daily_cap
        ZoneInfo(s.clinic_tz)
    except (StartupRefused, ZoneInfoNotFoundError, ValueError):
        return "live_test_inputs"
    if not 1 <= cap <= 10:
        return "live_test_inputs"
    return None


async def _provider_id_present(own: Ownership, appt_id: str) -> bool:
    async with own.engine.connect() as conn:
        pid = (await conn.execute(text(
            "SELECT provider_message_id FROM reminder_records WHERE emr_appt_id = :a"),
            {"a": appt_id})).scalar_one_or_none()
    return bool(pid)


@dataclass(frozen=True)
class _Outcome:
    refused: str | None = None
    state: str = "INTERRUPTED"
    reason: str = "owner_unavailable"
    provider_id: str = "no"


async def _operation(own: Ownership, s: DemoSettings, token: str,
                     transport: httpx.AsyncBaseTransport | None,
                     now: Callable[[], datetime], gateway_out: list[NotifyreGateway]) -> _Outcome:
    """Everything after ownership, run as ONE tracked task: the gate, the send
    and the report lookup are all inside the bounded drain. Every failure is
    reduced to a fixed enum, with no exception text."""
    try:
        unresolved = await _unresolved(own)
    except Exception:
        return _Outcome(refused="gate_check_failed")
    if unresolved:
        return _Outcome(refused="live_test_unresolved")
    assert s.sms_sender is not None and s.sms_test_number is not None
    tz = s.clinic_tz
    day = clinic_target_date(now(), tz, days_before=s.reminder_days_before)
    start = datetime.combine(day, time(9, 0), tzinfo=ZoneInfo(tz)).astimezone(UTC)
    appt = SyntheticAppointment(f"syn-e{own.epoch}-{day:%Y%m%d}-live-1", "syn-p-live", start,
                                ApptStatus.SCHEDULED)
    patient = SyntheticPatient("syn-p-live", "Synthetic Demo", "live")
    gateway = NotifyreGateway(api_key=token, sender=s.sms_sender,
                              test_number=s.sms_test_number, transport=transport)
    gateway_out.append(gateway)
    try:
        # Each explicit, owner-confirmed run is a distinct send by design (test_lt4),
        # so the restart slot dedupe (F-R1-1) does not apply to this entry point.
        result = await send_one(own, s, appt, patient, gateway, tenant_id=TENANT_ID, now=now,
                                slot_dedupe=False)
    except Exception:
        return _Outcome(state="ERROR", reason="internal")
    present = "no"
    try:
        present = "yes" if await _provider_id_present(own, appt.emr_appt_id) else "no"
    except Exception:
        present = "unknown"
    return _Outcome(state=result.state, reason=result.reason or "none", provider_id=present)


async def live_sms_test(
    env: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,  # internal test dependency only
    now: Callable[[], datetime] = _utcnow,
    drain_timeout: float = 10.0,
    cancel_grace: float = 2.0,
) -> int:
    if env.get("DEMO_LIVE_TEST") != ARMED:
        _say(refused="live_test_not_armed")
        return EXIT_REFUSED
    s = DemoSettings.from_env(env)
    bad = _local_inputs(env, s)
    if bad is not None:
        _say(refused=bad)
        return EXIT_REFUSED
    try:
        target = validate_demo_database_url(s)
        own = await acquire_ownership(s, target)
    except StartupRefused as exc:
        _say(refused=exc.reason)
        return EXIT_REFUSED
    except Exception:
        _say(refused="startup_failed")
        return EXIT_REFUSED

    # From here on, everything is either tracked (bounded by the drain) or
    # local. Signals are handled from the moment ownership exists.
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    gateways: list[NotifyreGateway] = []
    op = own.spawn_tracked(_operation(own, s, env["NOTIFYRE_API_KEY"], transport, now,
                                      gateways))
    stopper = loop.create_task(stop.wait())
    try:
        await asyncio.wait({op, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if not op.done():
            own.begin_closing()
        drained = await own.drain(drain_timeout, cancel_grace)
        if not drained:
            _say(drain="incomplete")
            return EXIT_DRAIN_INCOMPLETE  # no lock release: the process must exit now

        # The drain confirmed the operation ended: reading it cannot block.
        outcome = _Outcome()
        if op.done() and not op.cancelled() and op.exception() is None:
            outcome = op.result()
        # Hard-bounded cleanup: an explicit task with a deadline, then a bounded
        # cancellation grace. It is never joined beyond that. If cleanup
        # survives, exit 4 now. The operation has already ended, but cleanup
        # may have released part of the ownership before it stalled.
        closer = loop.create_task(own.close())
        done, _ = await asyncio.wait({closer}, timeout=drain_timeout)
        if not done:
            closer.cancel()
            done, _ = await asyncio.wait({closer}, timeout=cancel_grace)
        if not done or closer.cancelled() or closer.exception() is not None:
            _say(drain="incomplete")
            return EXIT_DRAIN_INCOMPLETE
    finally:
        stopper.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    if outcome.refused is not None:
        _say(refused=outcome.refused)
        return EXIT_REFUSED
    keys = gateways[0].last_keys if gateways else ()
    _say(state=outcome.state, reason=outcome.reason, provider_id_present=outcome.provider_id,
         response_keys=",".join(keys) or "none")
    return EXIT_SUBMITTED if outcome.state == "SUBMITTED" else EXIT_OTHER
