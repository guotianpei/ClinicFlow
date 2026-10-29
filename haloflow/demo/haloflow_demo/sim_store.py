"""M-2: durable SIMULATED EHR confirmation store (package v1 + v1r1, Codex ALIGN).

One row per appointment slot (source, tenant_id, emr_patient_id, appt_start) in
``sim_confirmations``. Seeded once (never reset by loading fixtures). Seeding,
reads and compare-and-set writes are single fenced statements.
M-2S (package v1r2): the same table also serves the athena_preview source.
A sandbox row carries the provider appointment id it was seeded for
(``provider_appt_id``, immutable); ``seed_bound`` inserts and classifies in one
fenced transaction and never re-points, resets or deletes a row. A store serves
exactly one source and one tenant; anything else is refused (``SlotRefused``).
Simulated evidence only: nothing here describes a real EHR's semantics.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from haloflow_demo.ownership import Ownership
from haloflow_demo.synthetic_ehr import Fixtures, Observation, WriteResult

SOURCE = "synthetic"
ATHENA = "athena_preview"
SOURCES = frozenset({SOURCE, ATHENA})
PAID_RE = re.compile(r"[1-9][0-9]{0,11}")
BOUND, SLOT_TAKEN, PAID_ELSEWHERE = "BOUND", "SLOT_TAKEN", "PAID_ELSEWHERE"
SeedHook = Callable[[str], Awaitable[None]]
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
    def __init__(self, own: Ownership, tenant: str, source: str = SOURCE) -> None:
        if source not in SOURCES:
            raise ValueError("unsupported store source")
        self._own, self._tenant, self._source = own, tenant, source

    @property
    def source(self) -> str:
        return self._source

    def slot(self, emr_patient_id: str, appt_start: datetime) -> Slot:
        return Slot(self._source, self._tenant, emr_patient_id, appt_start)

    def _check(self, slot: Slot) -> None:
        if slot.source != self._source or slot.tenant_id != self._tenant:
            raise SlotRefused("slot not served by this store")

    async def seed(self, fixtures: Fixtures) -> None:
        """Synthetic: insert each fixture slot only if absent; never overwrite a row."""
        if self._source != SOURCE:
            raise SlotRefused("seed() serves the synthetic source only")
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

    async def bump(self, slot: Slot, value: str | None) -> Observation:
        """Another clinic actor's edit (test/demo hook): one fenced UPDATE that sets
        the value and increments the version; refused for an absent or foreign slot."""
        self._check(slot)
        async with self._own.fenced() as conn:
            row = (await conn.execute(text(
                "UPDATE sim_confirmations SET value = :v, version = version + 1,"
                f" updated_at = now() WHERE {_KEY} RETURNING value, version"),
                {**_params(slot), "v": value})).one_or_none()
        if row is None:
            raise SlotRefused("no durable row for this slot")
        return Observation(row.value, int(row.version))

    async def count(self, slot: Slot) -> int:
        self._check(slot)
        async with self._own.fenced() as conn:
            return int((await conn.execute(text(
                f"SELECT count(*) FROM sim_confirmations WHERE {_KEY}"),
                _params(slot))).scalar_one())

    async def seed_bound(self, entries: Iterable[tuple[str, str, datetime]], *,
                         initial: str = "1", hook: SeedHook | None = None
                         ) -> dict[str, str]:
        """M-2S: one fenced transaction. Each ``(paid, emr_patient_id, appt_start)``
        is inserted only if neither its slot nor its PAID is taken, in ascending PAID
        order, then classified: BOUND (the slot row carries this PAID), SLOT_TAKEN
        (the slot carries another PAID), PAID_ELSEWHERE (this PAID is bound to another
        slot). Unique constraints decide contention; a row is never updated, re-pointed
        or deleted. ``hook("inserted")`` runs after the inserts, before commit."""
        if self._source != ATHENA:
            raise SlotRefused("seed_bound() serves the athena_preview source only")
        rows = sorted(entries, key=lambda e: (int(e[0]), e[0]))
        if any(not PAID_RE.fullmatch(p) for p, _, _ in rows):
            raise ValueError("malformed provider appointment id")
        if len({p for p, _, _ in rows}) != len(rows):
            raise ValueError("duplicate provider appointment id in one seed")
        out: dict[str, str] = {}
        async with self._own.fenced() as conn:
            for paid, pid, start in rows:
                await conn.execute(text(
                    "INSERT INTO sim_confirmations(source, tenant_id, emr_patient_id,"
                    " appt_start, value, version, provider_appt_id)"
                    " VALUES (:s, :t, :p, :a, :v, 1, :i) ON CONFLICT DO NOTHING"),
                    {**_params(self.slot(pid, start)), "v": initial, "i": paid})
            if hook is not None:
                await hook("inserted")
            for paid, pid, start in rows:
                at_slot = (await conn.execute(text(
                    f"SELECT provider_appt_id FROM sim_confirmations WHERE {_KEY}"),
                    _params(self.slot(pid, start)))).scalar_one_or_none()
                if at_slot == paid:
                    out[paid] = BOUND
                elif at_slot is not None:
                    out[paid] = SLOT_TAKEN
                else:
                    out[paid] = PAID_ELSEWHERE
        return out

    async def bound(self, slot: Slot, paid: str) -> bool:
        """M-2S: exactly one durable row at this slot, bound to this PAID."""
        self._check(slot)
        if self._source != ATHENA or not PAID_RE.fullmatch(paid):
            return False
        async with self._own.fenced() as conn:
            n = int((await conn.execute(text(
                f"SELECT count(*) FROM sim_confirmations WHERE {_KEY}"
                " AND provider_appt_id = :i"), {**_params(slot), "i": paid})).scalar_one())
        return n == 1

    async def any_rows(self) -> bool:
        async with self._own.fenced() as conn:
            return (await conn.execute(text(
                "SELECT 1 FROM sim_confirmations WHERE source = :s AND tenant_id = :t"
                " LIMIT 1"), {"s": self._source, "t": self._tenant})
            ).scalar_one_or_none() is not None
