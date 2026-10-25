"""DB-backed staff service for Day 3: list synthetic appointments, preview the
eligible batch (no writes), pause/resume (fenced, R-14).

Mode labels are what this build can actually do. Day 3 has no real SMS, EHR or
callback path, so every label is SIMULATED whatever the settings request.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text

from haloflow_demo.ownership import Ownership
from haloflow_demo.selection import clinic_target_date, format_local, select_eligible
from haloflow_demo.sending import SmsGateway, StubGateway, send_batch
from haloflow_demo.settings import DemoSettings
from haloflow_demo.synthetic_ehr import SyntheticEhr, default_fixtures

TENANT_ID = "demo-clinic"
LABELS = {"sms": "SIMULATED", "ehr": "SIMULATED", "callback": "SIMULATED"}


# What staff see. A submission is never shown as delivered: no delivery
# callback is in scope (R-12).
STATE_LABELS = {"CLAIMED": "Sending", "SUBMITTED": "Submitted", "NOT_SENT": "Not sent",
                "REJECTED": "Rejected by provider", "UNCERTAIN": "Uncertain (not re-sent)"}


def _label(state: str | None) -> str:
    return "none" if state is None else STATE_LABELS.get(state, state)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class DbStaffService:
    def __init__(
        self,
        own: Ownership,
        settings: DemoSettings,
        *,
        now: Callable[[], datetime] = _utcnow,
        gateway: SmsGateway | None = None,
    ) -> None:
        self._own = own
        self._settings = settings
        self._now = now
        self._gateway: SmsGateway = gateway or StubGateway(settings.sms_test_number)
        self._ehr: dict[date, SyntheticEhr] = {}

    def _target(self) -> date:
        return clinic_target_date(self._now(), self._settings.clinic_tz,
                                  days_before=self._settings.reminder_days_before)

    def _ehr_for(self, target: date) -> SyntheticEhr:
        if target not in self._ehr:
            self._ehr[target] = SyntheticEhr(default_fixtures(target, self._settings.clinic_tz))
        return self._ehr[target]

    async def _reminder_states(self) -> dict[str, str]:
        async with self._own.engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT emr_appt_id, state FROM reminder_records WHERE tenant_id = :t"),
                {"t": TENANT_ID},
            )).all()
        return {str(r[0]): str(r[1]) for r in rows}

    async def _paused(self) -> bool:
        async with self._own.engine.connect() as conn:
            value = (await conn.execute(
                text("SELECT paused FROM demo_settings WHERE id = 1")
            )).scalar_one()
        return bool(value)

    async def page_data(self) -> dict[str, Any]:
        target = self._target()
        ehr = self._ehr_for(target)
        tz = self._settings.clinic_tz
        appts = await ehr.get_appointments_by_date(target, tz)
        states = await self._reminder_states()
        rows = [
            {
                "emr_appt_id": a.emr_appt_id,
                "patient": ehr.patients[a.emr_patient_id].display_name
                if a.emr_patient_id in ehr.patients else "(unknown)",
                "when": format_local(a.start_utc, tz),
                "status": f"{a.status.value}; reminder: "
                          f"{_label(states.get(a.emr_appt_id))}",
            }
            for a in appts
        ]
        return {"paused": await self._paused(), "target_date": target.isoformat(),
                "appointments": rows, "inbound": [], "items": [], "labels": dict(LABELS)}

    async def preview_batch(self) -> list[dict[str, Any]]:
        """Read-only: no claim, no reservation, no external call."""
        target = self._target()
        ehr = self._ehr_for(target)
        tz = self._settings.clinic_tz
        appts = await ehr.get_appointments_by_date(target, tz)
        result = select_eligible(appts, ehr.patients, target, tz,
                                 existing_appt_ids=set(await self._reminder_states()))
        decision: dict[str, str] = {e.appointment.emr_appt_id: "eligible" for e in result.eligible}
        decision.update({s.emr_appt_id: f"skip: {s.reason.value}" for s in result.skipped})
        return [{"emr_appt_id": a.emr_appt_id, "decision": decision[a.emr_appt_id]}
                for a in appts if a.emr_appt_id in decision]

    async def send_batch(self) -> list[dict[str, Any]]:
        """Send to every eligible appointment on the target date (stub gateway)."""
        target = self._target()
        ehr = self._ehr_for(target)
        tz = self._settings.clinic_tz
        appts = await ehr.get_appointments_by_date(target, tz)
        result = select_eligible(appts, ehr.patients, target, tz,
                                 existing_appt_ids=set(await self._reminder_states()))
        sent = await send_batch(self._own, self._settings, result.eligible, self._gateway,
                                tenant_id=TENANT_ID, now=self._now)
        return [{"reminder_id": r.reminder_id, "state": r.state, "reason": r.reason}
                for r in sent]

    async def set_paused(self, paused: bool) -> None:
        async with self._own.fenced() as conn:
            await conn.execute(text("UPDATE demo_settings SET paused = :p WHERE id = 1"),
                               {"p": paused})
