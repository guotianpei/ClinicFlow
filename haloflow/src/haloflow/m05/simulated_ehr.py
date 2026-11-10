"""L-1S package S-I: the SYNTHETIC EHR port for confirmation write-back.

Governing: S-I investigation v1 + v1r1 + v1r2, red-test packet v1 (Work Session
2026-11-10), Codex Option A and the red-test pins. Synthetic only: this module talks to
no real EHR, imports no HTTP or network client, and is not composed anywhere.

`SimulatedEhr` is an in-memory appointment store with one rule that matters for safety:
a confirmation write is applied only if, at the moment the state changes, the permit is
not closed and the port's own time is not past the permit's apply deadline. The deadline
check and the mutation are one SYNCHRONOUS step (`_apply_locked`) taken under the single
port lock, so no await can separate them. The port's time is non-decreasing: it is the
maximum of every value the injected clock has returned (and survives `snapshot` /
`restore`), so a clock that moves backwards cannot reopen a passed deadline. A permit the
reconciler closed is refused for ever, whatever the clock says.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

__all__ = [
    "STATUSES",
    "EhrRead",
    "EhrUnavailable",
    "SimulatedEhr",
    "WriteResult",
]

STATUSES: Final = ("positive", "neutral", "negative", "unknown", "empty", "checked_in",
                   "checked_out", "cancelled")
WRITE_OUTCOMES: Final = frozenset({"applied", "deadline_passed", "permit_closed", "not_found"})


class EhrUnavailable(Exception):
    """A read or write that could not reach the synthetic EHR (carries no patient data)."""


@dataclass(frozen=True, slots=True)
class EhrRead:
    """One appointment as the synthetic EHR reports it."""

    status: str
    appointment_id: str
    start_at: datetime
    provider_ref: str | None
    department_ref: str | None
    replaced_by: str | None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError("status")
        if type(self.appointment_id) is not str or not self.appointment_id:
            raise ValueError("appointment_id")
        _require_aware(self.start_at, "start_at")
        for name in ("provider_ref", "department_ref", "replaced_by"):
            value = getattr(self, name)
            if value is not None and type(value) is not str:
                raise ValueError(name)

    def __repr__(self) -> str:
        return f"EhrRead(status={self.status!r})"


@dataclass(frozen=True, slots=True)
class WriteResult:
    outcome: str

    def __post_init__(self) -> None:
        if self.outcome not in WRITE_OUTCOMES:
            raise ValueError("outcome")


def _require_aware(value: Any, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(name)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    appointments: dict[str, EhrRead]
    writes: tuple[tuple[str, str], ...]
    closed: frozenset[str]
    used: frozenset[str]
    port_time: datetime | None


class SimulatedEhr:
    """The synthetic EHR. Every read, write and close takes `lock`."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._port_time: datetime | None = None
        self._appointments: dict[str, EhrRead] = {}
        self._closed: set[str] = set()
        self._used: set[str] = set()
        self._read_failures = 0
        self._write_failures = 0
        self.lock = asyncio.Lock()
        self.on_locked: Callable[[], Awaitable[None]] | None = None
        self.writes: list[tuple[str, str]] = []

    def __repr__(self) -> str:
        return f"SimulatedEhr(appointments={len(self._appointments)}, writes={len(self.writes)})"

    # -- fixture and fault injection --------------------------------------------------------

    def put(self, read: EhrRead) -> None:
        if not isinstance(read, EhrRead):
            raise TypeError("read")
        self._appointments[read.appointment_id] = read

    def fail_reads(self, n: int) -> None:
        self._read_failures = max(0, int(n))

    def fail_writes(self, n: int) -> None:
        self._write_failures = max(0, int(n))

    # -- time -------------------------------------------------------------------------------

    def _now(self) -> datetime:
        observed = self._clock()
        _require_aware(observed, "clock")
        if self._port_time is None or observed > self._port_time:
            self._port_time = observed
        return self._port_time

    # -- port -------------------------------------------------------------------------------

    async def read(self, appointment_id: str) -> EhrRead:
        async with self.lock:
            self._now()
            if self._read_failures > 0:
                self._read_failures -= 1
                raise EhrUnavailable("read unavailable")
            found = self._appointments.get(appointment_id)
            if found is None:
                raise EhrUnavailable("appointment unavailable")
            return found

    async def write_confirmation(self, appointment_id: str, *, permit_id: str,
                                 apply_deadline: datetime) -> WriteResult:
        _require_aware(apply_deadline, "apply_deadline")
        if type(permit_id) is not str or not permit_id:
            raise ValueError("permit_id")
        if type(appointment_id) is not str or not appointment_id:
            raise ValueError("appointment_id")
        async with self.lock:
            if self.on_locked is not None:
                await self.on_locked()
            if self._write_failures > 0:
                self._write_failures -= 1
                raise EhrUnavailable("write unavailable")
            return self._apply_locked(appointment_id, permit_id, apply_deadline)

    def _apply_locked(self, appointment_id: str, permit_id: str,
                      apply_deadline: datetime) -> WriteResult:
        """The deadline check and the state change in ONE synchronous step (lock held)."""

        now = self._now()
        if permit_id in self._closed or permit_id in self._used:
            return WriteResult("permit_closed")
        if now > apply_deadline:
            return WriteResult("deadline_passed")
        found = self._appointments.get(appointment_id)
        if found is None:
            return WriteResult("not_found")
        self._appointments[appointment_id] = replace(found, status="positive")
        self._used.add(permit_id)
        self.writes.append((appointment_id, permit_id))
        return WriteResult("applied")

    async def close_permit(self, permit_id: str) -> None:
        if type(permit_id) is not str or not permit_id:
            raise ValueError("permit_id")
        async with self.lock:
            self._now()
            self._closed.add(permit_id)

    # -- restart ----------------------------------------------------------------------------

    def snapshot(self) -> _Snapshot:
        return _Snapshot(dict(self._appointments), tuple(self.writes), frozenset(self._closed),
                         frozenset(self._used), self._port_time)

    @classmethod
    def restore(cls, snapshot: _Snapshot, *, clock: Callable[[], datetime]) -> SimulatedEhr:
        if not isinstance(snapshot, _Snapshot):
            raise TypeError("snapshot")
        ehr = cls(clock=clock)
        ehr._appointments = dict(snapshot.appointments)
        ehr.writes = list(snapshot.writes)
        ehr._closed = set(snapshot.closed)
        ehr._used = set(snapshot.used)
        ehr._port_time = snapshot.port_time
        return ehr
