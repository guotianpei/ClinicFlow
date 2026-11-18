"""Eligibility E1 test support (RED BY DESIGN until E1 source exists).

Governing: eligibility requirements v1r1; architecture v1, v1r1, v1r2 amendment, v1r3
correction (Codex ALIGN f10c1f2b); stage-3 test cases v1 + v1r1 (Codex ALIGN bca3bf33);
owner decisions OD-EL1..EL10. Synthetic only.

SQL interface pins (E1 red packet v1, section 2):
- Registry: `build_synthetic_registry(include_el1=True)` = the synthetic registry (S-I TEST units
  + B5 t095..t097) + E1 units t098..t101, target **101**. Catalogue:
  `build_synthetic_catalog(include_el1=True)`.
- t098 `m05_el_settings` (singleton: enabled bool DEFAULT false, lead_days 3..5 DEFAULT 3,
  not_received_days 0..5 DEFAULT 1, settings_version, updated_at) and
  `m05_el_priority_payers (payer_id)`; owner-written (support path), no runtime privilege.
- t099 tables `m05_el_appointments` (key row), `m05_el_coverage`, `m05_el_triggers`,
  `m05_el_verifications`, `m05_el_counts`, `m05_el_case_events`; gateway
  **`m05_directory_v4(jsonb)`** = `m05_directory_v3` + an optional `coverage` key on
  `appointment_put` (result unchanged: `{"schema_version": 1, "outcome": "applied"}`).
- t100 gateway **`m05_el_tick(jsonb)`** (timer workload): raises the not-received case event
  for pending coverage whose deadline has passed (`<= clock_timestamp()`); result
  `{"schema_version": 1, "outcome": "applied", "raised": n}`.
- t101 remove-only closure: runtime EXECUTE leaves `m05_directory_v3`.
- Coverage block: `{"state": "pending"}` | `{"state": "none"}` | `{"state": "present",
  "payer_id", "member_id", "relationship", "patient_name", "patient_dob", "subscriber_name",
  "subscriber_dob", "subscriber_member_id"}` (present: all nine keys; values string or null,
  <= 64 chars; dobs `YYYY-MM-DD` or null). Anything else -> COMMAND_MALFORMED.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any, Final

import m02_support
import sa_support as sa
import sh2_support as sh2
import si_support as si
from psycopg import sql

EL1_TARGET: Final = 101
EL1_SUPPORTED: Final = range(EL1_TARGET, EL1_TARGET + 1)
NY: Final = "America/New_York"
PRIORITY: Final = ("SYN-ANTHEM", "SYN-SENTARA", "SYN-UHC")
OTHER_PAYER: Final = "SYN-OTHER"

EL_TABLES: Final = ("m05_el_appointments", "m05_el_coverage", "m05_el_triggers",
                    "m05_el_verifications", "m05_el_counts", "m05_el_case_events")


def el1_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(include_el1=True)


class EL1Install(si.SIInstall):
    """A synthetic tenant at target 101 (S-I TEST + B5 + E1), provisioned in the row body."""

    b5 = True

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, el1_registry(), self.tenant,
                                           supported=EL1_SUPPORTED, manifest=staff_manifest())
                sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                sa.admin(self, si.si_test_profile_sql().format(schema=self.schema))
                sh2.settings_raw(self, clinic_display_name=sh2.CLINIC, clinic_tz=NY)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"E1 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[EL1Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield EL1Install(ids, tenant)
    finally:
        reset(tenant)


def ready(install: EL1Install, *, enabled: bool = True, lead_days: int = 3,
          not_received_days: int = 1, priority: tuple[str, ...] = PRIORITY) -> EL1Install:
    install.require()
    si.sc.change(install, "grant")
    el_settings(install, enabled=enabled, lead_days=lead_days,
                not_received_days=not_received_days, priority=priority)
    return install


def el_settings(install: EL1Install, *, enabled: bool, lead_days: int,
                not_received_days: int, priority: tuple[str, ...]) -> None:
    """FIXTURE: the support-path settings write (owner role), as reminder settings_raw."""
    s = sql.Identifier(install.schema)
    sa.admin(install, sql.SQL(
        "UPDATE {}.m05_el_settings SET enabled = %s, lead_days = %s, not_received_days = %s,"
        " settings_version = settings_version + 1, updated_at = clock_timestamp()").format(s),
        (enabled, lead_days, not_received_days))
    sa.admin(install, sql.SQL("DELETE FROM {}.m05_el_priority_payers").format(s))
    for payer in priority:
        sa.admin(install, sql.SQL(
            "INSERT INTO {}.m05_el_priority_payers (payer_id) VALUES (%s)").format(s), (payer,))


def present(payer: str = PRIORITY[0], *, relationship: str = "self", **over: Any
            ) -> dict[str, Any]:
    cov: dict[str, Any] = {
        "state": "present", "payer_id": payer, "member_id": "M000001",
        "relationship": relationship, "patient_name": "Synthetic Patient",
        "patient_dob": "1970-01-01", "subscriber_name": None, "subscriber_dob": None,
        "subscriber_member_id": None}
    if relationship == "dependent":
        cov.update(subscriber_name="Synthetic Subscriber", subscriber_dob="1968-02-02",
                   subscriber_member_id="M000002")
    cov.update(over)
    return cov


def put(install: EL1Install, ref: str, patient: str, start: datetime | str, *,
        coverage: dict[str, Any] | None = None, status: str = "booked",
        department_tz: str | None = NY) -> dict[str, Any]:
    """appointment_put through m05_directory_v4; `coverage=None` omits the key (legacy shape)."""
    fields = si.appointment3(ref, patient, start, status=status, department_tz=department_tz)
    if coverage is not None:
        fields["coverage"] = coverage
    return sa.call(install, "m05_directory_v4",
                   {"schema_version": 1, "op": "appointment_put", **fields,
                    "attribution": sa.attribution(sa.W_DIRECTORY)})


def tick(install: EL1Install) -> dict[str, Any]:
    return sa.call(install, "m05_el_tick",
                   {"schema_version": 1, "attribution": sa.attribution(sa.W_TIMER)})


def rows(install: EL1Install, table: str, where: str = "true", params: Any = None,
         order: str = "1") -> list[tuple[Any, ...]]:
    assert table in EL_TABLES or table.startswith("m05_"), table
    return sa.admin(install, sql.SQL("SELECT * FROM {}.{} WHERE " + where + " ORDER BY " + order)
                    .format(sql.Identifier(install.schema), sql.Identifier(table)), params)


def count(install: EL1Install, table: str, where: str = "true", params: Any = None) -> int:
    got = sa.admin(install, sql.SQL("SELECT count(*) FROM {}.{} WHERE " + where).format(
        sql.Identifier(install.schema), sql.Identifier(table)), params)
    return int(got[0][0])


def el_snapshot(install: EL1Install) -> dict[str, int]:
    return {t: count(install, t) for t in EL_TABLES}
