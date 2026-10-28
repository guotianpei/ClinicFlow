"""Synthetic EHR (R-23): deterministic fixtures, versioned confirmation values,
switchable failure modes, and a hook to inject a clinic change between our read
and our write (EXP2).

Patients carry a ``phone_alias`` label, never a dialable number: R-9 makes
``DEMO_SMS_TEST_NUMBER`` the only phone setting. Names start with "Synthetic ".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo


class ApptStatus(StrEnum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True)
class SyntheticPatient:
    emr_patient_id: str
    display_name: str
    phone_alias: str | None = None


@dataclass(frozen=True)
class SyntheticAppointment:
    emr_appt_id: str
    emr_patient_id: str
    start_utc: datetime
    status: ApptStatus
    confirmation: str | None = None
    # N-TZ Step 3: zone the appointment's local date/time belong to; None = CLINIC_TZ.
    display_tz: str | None = None


@dataclass(frozen=True)
class Fixtures:
    patients: dict[str, SyntheticPatient]
    appointments: list[SyntheticAppointment]


@dataclass(frozen=True)
class Observation:
    value: str | None
    version: int


@dataclass(frozen=True)
class WriteResult:
    applied: bool
    current: Observation


class EhrConnectError(Exception):
    """The request never reached the EHR: nothing was applied."""


class EhrResponseLost(Exception):
    """The request may have been applied; the response was lost."""


def default_fixtures(anchor: date, tz: str = "America/New_York",
                     scenario: str | None = None) -> Fixtures:
    """Deterministic fixtures for one clinic day.

    Cases: plain single appointment (A); shared phone alias (B and C share "shared");
    one patient with two open appointments (D); no phone (E); cancelled (F).

    ``scenario`` qualifies the appointment IDs (``syn-<scenario>-a-N``) so that
    IDs are unique across dates and owner epochs. The staff service passes
    ``e<epoch>-<yyyymmdd>`` (Codex 175423Z, P1).
    """
    zone = ZoneInfo(tz)
    pre = "syn-a-" if scenario is None else f"syn-{scenario}-a-"

    def at(h: int, m: int = 0) -> datetime:
        return datetime.combine(anchor, time(h, m), tzinfo=zone).astimezone(UTC)

    patients = {
        "syn-p-a": SyntheticPatient("syn-p-a", "Synthetic Patient A", "alpha"),
        "syn-p-b": SyntheticPatient("syn-p-b", "Synthetic Patient B", "shared"),
        "syn-p-c": SyntheticPatient("syn-p-c", "Synthetic Patient C", "shared"),
        "syn-p-d": SyntheticPatient("syn-p-d", "Synthetic Patient D", "delta"),
        "syn-p-e": SyntheticPatient("syn-p-e", "Synthetic Patient E", None),
        "syn-p-f": SyntheticPatient("syn-p-f", "Synthetic Patient F", "foxtrot"),
    }
    appointments = [
        SyntheticAppointment(pre + "1", "syn-p-a", at(9), ApptStatus.SCHEDULED, "1"),
        SyntheticAppointment(pre + "2", "syn-p-b", at(9, 30), ApptStatus.SCHEDULED, "1"),
        SyntheticAppointment(pre + "3", "syn-p-c", at(10), ApptStatus.SCHEDULED, "1"),
        SyntheticAppointment(pre + "4", "syn-p-d", at(10, 30), ApptStatus.SCHEDULED, "1"),
        SyntheticAppointment(pre + "5", "syn-p-d", at(15), ApptStatus.SCHEDULED, "1"),
        SyntheticAppointment(pre + "6", "syn-p-e", at(11), ApptStatus.SCHEDULED, "1"),
        SyntheticAppointment(pre + "7", "syn-p-f", at(11, 30), ApptStatus.CANCELLED, "1"),
    ]
    return Fixtures(patients=patients, appointments=appointments)


@dataclass
class _ConfState:
    value: str | None
    version: int = 1


@dataclass
class SyntheticEhr:
    fixtures: Fixtures
    supports_conditional_write: bool = True
    conditional_contract_verified: bool = False  # synthetic: proves nothing about a real EHR
    fail_next_write: str | None = None  # "connect" | "lost_response"
    on_after_read: Callable[[], None] | None = None  # one-shot
    write_attempts: int = 0
    applied_writes: int = 0
    lost_updates: int = 0
    # M-2: an optional durable store (sim_store.SimStore). When set, the confirmation
    # value/version live in the demo DB keyed by slot, not in ``_conf``.
    store: Any = field(default=None, repr=False)
    _conf: dict[str, _ConfState] = field(default_factory=dict, repr=False)
    _last_read_version: dict[str, int] = field(default_factory=dict, repr=False)
    _appts: dict[str, SyntheticAppointment] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for a in self.fixtures.appointments:
            self._conf[a.emr_appt_id] = _ConfState(a.confirmation)
            self._appts[a.emr_appt_id] = a

    def appointment(self, appt_id: str) -> SyntheticAppointment | None:
        return self._appts.get(appt_id)

    def _slot(self, appt_id: str) -> Any:
        a = self._appts[appt_id]
        return self.store.slot(a.emr_patient_id, a.start_utc)

    @property
    def patients(self) -> dict[str, SyntheticPatient]:
        return self.fixtures.patients

    async def get_appointments_by_date(self, target: date, tz: str) -> list[SyntheticAppointment]:
        return [a for a in self.fixtures.appointments
                if a.start_utc.astimezone(ZoneInfo(a.display_tz or tz)).date() == target]

    # ── confirmation value ────────────────────────────────────────────────────

    async def read_confirmation(self, appt_id: str) -> Observation:
        if self.store is not None:
            obs: Observation = await self.store.read(self._slot(appt_id))
        else:
            st = self._conf[appt_id]
            obs = Observation(st.value, st.version)
        self._last_read_version[appt_id] = obs.version
        hook, self.on_after_read = self.on_after_read, None
        if hook is not None:
            hook()
        return obs

    def clinic_change(self, appt_id: str, value: str | None) -> None:
        """Another clinic actor edits the value (bumps the version)."""
        st = self._conf[appt_id]
        st.value = value
        st.version += 1

    def _consume_failure(self) -> str | None:
        mode, self.fail_next_write = self.fail_next_write, None
        return mode

    async def write_confirmation_conditional(
        self, appt_id: str, target: str, expected_version: int
    ) -> WriteResult:
        if not self.supports_conditional_write:
            raise RuntimeError("conditional write not supported by this adapter")
        self.write_attempts += 1
        mode = self._consume_failure()
        if mode == "connect":
            raise EhrConnectError("synthetic connect failure")
        if self.store is not None:
            res: WriteResult = await self.store.cas(self._slot(appt_id), target,
                                                    expected_version)
            if res.applied:
                self.applied_writes += 1
                if mode == "lost_response":
                    raise EhrResponseLost("synthetic: applied, response lost")
            return res
        st = self._conf[appt_id]
        if st.version != expected_version:
            return WriteResult(False, Observation(st.value, st.version))
        st.value = target
        st.version += 1
        self.applied_writes += 1
        if mode == "lost_response":
            raise EhrResponseLost("synthetic: applied, response lost")
        return WriteResult(True, Observation(st.value, st.version))

    async def write_confirmation_unconditional(self, appt_id: str, target: str) -> None:
        """The legacy athena shape (PUT without a precondition). Negative control only."""
        self.write_attempts += 1
        st = self._conf[appt_id]
        if st.version != self._last_read_version.get(appt_id, st.version):
            self.lost_updates += 1
        st.value = target
        st.version += 1
        self.applied_writes += 1
