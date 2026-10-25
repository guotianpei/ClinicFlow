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

On SIGTERM or SIGINT, the bounded drain applies. If the call ignores
cancellation, the exit code is 4 and the locks are not released explicitly
(the kernel releases them when the process exits).
"""

from __future__ import annotations

import asyncio
import re
import signal
from collections.abc import Callable, Mapping
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

    if await _unresolved(own):
        await own.close()
        _say(refused="live_test_unresolved")
        return EXIT_REFUSED

    assert s.sms_sender is not None and s.sms_test_number is not None
    tz = s.clinic_tz
    day = clinic_target_date(now(), tz, days_before=s.reminder_days_before)
    start = datetime.combine(day, time(9, 0), tzinfo=ZoneInfo(tz)).astimezone(UTC)
    appt = SyntheticAppointment(f"syn-e{own.epoch}-{day:%Y%m%d}-live-1", "syn-p-live", start,
                                ApptStatus.SCHEDULED)
    patient = SyntheticPatient("syn-p-live", "Synthetic Demo", "live")
    gateway = NotifyreGateway(api_key=env["NOTIFYRE_API_KEY"], sender=s.sms_sender,
                              test_number=s.sms_test_number, transport=transport)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    send = loop.create_task(send_one(own, s, appt, patient, gateway, tenant_id=TENANT_ID,
                                     now=now))
    stopper = loop.create_task(stop.wait())
    try:
        await asyncio.wait({send, stopper}, return_when=asyncio.FIRST_COMPLETED)
        interrupted = not send.done()
        if interrupted:
            own.begin_closing()
        drained = await own.drain(drain_timeout, cancel_grace)
    finally:
        stopper.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    if not drained:
        _say(drain="incomplete")
        return EXIT_DRAIN_INCOMPLETE  # no lock release: the process must exit now

    state, reason = "INTERRUPTED", "owner_unavailable"
    try:
        result = await send
        state, reason = result.state, result.reason or "none"
    except asyncio.CancelledError:
        pass
    except Exception:
        state, reason = "ERROR", "internal"
    present = "no"
    if state != "INTERRUPTED":
        async with own.engine.connect() as conn:
            pid = (await conn.execute(text(
                "SELECT provider_message_id FROM reminder_records WHERE emr_appt_id = :a"),
                {"a": appt.emr_appt_id})).scalar_one_or_none()
        present = "yes" if pid else "no"
    await own.close()
    _say(state=state, reason=reason, provider_id_present=present,
         response_keys=",".join(gateway.last_keys) or "none")
    return EXIT_SUBMITTED if state == "SUBMITTED" else EXIT_OTHER
