"""Reminder records: the atomic claim (R-12, claim part).

The send path (reservation, gateway, outcome classification) is Day-4 scope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import text

from haloflow_demo.ownership import Ownership
from haloflow_demo.synthetic_ehr import SyntheticAppointment

ClaimHook = Callable[[str], Awaitable[None]]

# Namespace for the two-key advisory form; the one-key (bigint) space used by
# ownership.advisory_key does not overlap with two-key locks in PostgreSQL.
SLOT_LOCK_CLASS = 0x534C4F54  # "SLOT"


async def claim_reminder(own: Ownership, tenant_id: str, appt: SyntheticAppointment, *,
                         slot_dedupe: bool = True,
                         hook: ClaimHook | None = None) -> int | None:
    """Insert one CLAIMED record for (tenant, appointment); None if already claimed.

    F-R1-1 (C-1): also None when any reminder already exists for the same slot —
    tenant, synthetic patient and start instant — whatever its state or epoch.
    A transaction-scoped advisory lock keyed on the slot serialises the slot
    check and the INSERT, so two claims for different IDs of one slot cannot
    both pass; claims for other slots are not serialised. A hash collision only
    adds waiting, never a wrong answer (the check itself is exact).

    ``slot_dedupe=False`` is for the outbound-once live test only, where every
    explicit, owner-confirmed run is a distinct send by design (test_lt4).
    ``hook`` is a test seam.
    """
    async with own.fenced() as conn:
        taken = None
        if slot_dedupe:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"),
                {"c": SLOT_LOCK_CLASS,
                 "k": f"{tenant_id}|{appt.emr_patient_id}|{appt.start_utc.timestamp()!r}"},
            )
            taken = (await conn.execute(
                text("SELECT 1 FROM reminder_records WHERE tenant_id = :t"
                     " AND emr_patient_id = :p AND appt_start = :s LIMIT 1"),
                {"t": tenant_id, "p": appt.emr_patient_id, "s": appt.start_utc},
            )).scalar_one_or_none()
        if hook is not None:
            await hook("slot_checked")
        if taken is not None:
            return None
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
