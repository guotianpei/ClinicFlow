"""Reminder records: the atomic claim (R-12, claim part).

The send path (reservation, gateway, outcome classification) is Day-4 scope.
"""

from __future__ import annotations

from sqlalchemy import text

from haloflow.demo.ownership import Ownership
from haloflow.demo.synthetic_ehr import SyntheticAppointment


async def claim_reminder(own: Ownership, tenant_id: str, appt: SyntheticAppointment) -> int | None:
    """Insert one CLAIMED record for (tenant, appointment); None if already claimed."""
    async with own.fenced() as conn:
        rid = (await conn.execute(
            text(
                "INSERT INTO reminder_records"
                " (tenant_id, emr_appt_id, emr_patient_id, appt_start, state, mode)"
                " VALUES (:t, :a, :p, :s, 'CLAIMED', 'stub')"
                " ON CONFLICT ON CONSTRAINT uq_reminder_appt DO NOTHING RETURNING id"
            ),
            {"t": tenant_id, "a": appt.emr_appt_id, "p": appt.emr_patient_id,
             "s": appt.start_utc},
        )).scalar_one_or_none()
    return None if rid is None else int(rid)
