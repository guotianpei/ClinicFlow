"""M-2: durable SIMULATED EHR confirmation store (package v1 + v1r1, Codex ALIGN).

One row per appointment slot (source, tenant_id, emr_patient_id, appt_start) in
``sim_confirmations``. Seeded once (never reset by loading fixtures). Seeding,
reads and compare-and-set writes are single fenced statements.
Synthetic source only; any other source or tenant is refused (``SlotRefused``).
Simulated evidence only: nothing here describes a real EHR's semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from haloflow_demo.ownership import Ownership
from haloflow_demo.synthetic_ehr import Fixtures, Observation, WriteResult

SOURCE = "synthetic"
SYNTHETIC_ID_RE = re.compile(r"syn-e\d+-\d{8}-a-\d+")


class SlotRefused(LookupError):
    """The slot is not served by this store (foreign source or tenant, or absent)."""


@dataclass(frozen=True)
class Slot:
    source: str
    tenant_id: str
    emr_patient_id: str
    appt_start: datetime


_KEY = ("source = :s AND tenant_id = :t AND emr_patient_id = :p AND appt_start = :a")


def _params(slot: Slot) -> dict[str, Any]:
    return {"s": slot.source, "t": slot.tenant_id, "p": slot.emr_patient_id,
            "a": slot.appt_start}


class SimStore:
    def __init__(self, own: Ownership, tenant: str) -> None:
        self._own, self._tenant = own, tenant

    def slot(self, emr_patient_id: str, appt_start: datetime) -> Slot:
        return Slot(SOURCE, self._tenant, emr_patient_id, appt_start)

    def _check(self, slot: Slot) -> None:
        if slot.source != SOURCE or slot.tenant_id != self._tenant:
            raise SlotRefused("slot not served by this store")

    async def seed(self, fixtures: Fixtures) -> None:
        """Insert each fixture slot only if absent; never overwrite a durable row."""
        async with self._own.fenced() as conn:
            for a in fixtures.appointments:
                await conn.execute(text(
                    "INSERT INTO sim_confirmations(source, tenant_id, emr_patient_id,"
                    " appt_start, value, version) VALUES (:s, :t, :p, :a, :v, 1)"
                    " ON CONFLICT ON CONSTRAINT pk_sim_confirmations DO NOTHING"),
                    {**_params(self.slot(a.emr_patient_id, a.start_utc)),
                     "v": a.confirmation})

    async def read(self, slot: Slot) -> Observation:
        """A fenced read (R-M2-3): refused once ownership is lost or closed."""
        self._check(slot)
        async with self._own.fenced() as conn:
            row = (await conn.execute(text(
                f"SELECT value, version FROM sim_confirmations WHERE {_KEY}"),
                _params(slot))).one_or_none()
        if row is None:
            raise SlotRefused("no durable row for this slot")
        return Observation(row.value, int(row.version))

    async def cas(self, slot: Slot, target: str, expected_version: int) -> WriteResult:
        """One fenced UPDATE … WHERE version = :expected; on a miss, the current row."""
        self._check(slot)
        async with self._own.fenced() as conn:
            row = (await conn.execute(text(
                "UPDATE sim_confirmations SET value = :v, version = version + 1,"
                f" updated_at = now() WHERE {_KEY} AND version = :e"
                " RETURNING value, version"),
                {**_params(slot), "v": target, "e": expected_version})).one_or_none()
            if row is not None:
                return WriteResult(True, Observation(row.value, int(row.version)))
            cur = (await conn.execute(text(
                f"SELECT value, version FROM sim_confirmations WHERE {_KEY}"),
                _params(slot))).one_or_none()
        if cur is None:
            raise SlotRefused("no durable row for this slot")
        return WriteResult(False, Observation(cur.value, int(cur.version)))

    async def count(self, slot: Slot) -> int:
        self._check(slot)
        async with self._own.fenced() as conn:
            return int((await conn.execute(text(
                f"SELECT count(*) FROM sim_confirmations WHERE {_KEY}"),
                _params(slot))).scalar_one())
