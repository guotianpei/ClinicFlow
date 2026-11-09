"""L-1S package S-H, checkpoint H2 (reminder scheduling + one-shot send ticket): red-test
support.

Governing: H2 package v1 (claude_sh-h2-package-v1-2026-11-08.md, sha256 3d590468...) and
v1r1 delta (D1-D6), Codex ALIGN for red tests (codex_h2-v1r1-red-test-alignment.md, seven
pins), owner decisions Q-SH2 and P-1, and the red-test interface pins
(claude_sh-h2-red-interface-pins-v1r1-2026-11-08.md, D7-D10). Synthetic data only; fake
sender only; TEST registry only; not composed. Every import of H2 code is deferred into the
row body (class-B red rule), so a missing module fails each row, never collection.

Interface pins (asserted by the rows; the pins note discloses them):
- `haloflow.m05.reminder_units`: SH2_TENANT_MIGRATIONS (t057..t073 in order, none
  omitted), SH2_INSTALLED_STATE_PROFILES (every typed H2 unit) and SH2_ACL_AMENDMENTS
  (t072 removes haloflow_runtime EXECUTE from the UNCHANGED t038 directory, t048 prepare,
  t049 claim and t055 read). Target 73. No H1 or S-A unit is rewritten.
- Typed functions (jsonb -> jsonb, P0001 closed codes): runtime-executable
  m05_directory_v2 (t063), m05_outbound_claim_v2 (t066), m05_outbound_read_v2 (t067),
  m05_outbound_ready (t068), m05_outbound_render_snapshot (t069), m05_outbound_examine
  (t070), m05_outbound_consume (t071); OWNER-ONLY m05_reminder_decision (t062, STABLE,
  the D7 clock seam) and m05_outbound_prepare_rendered (t065); SUPPORT-ONLY
  m05_reminder_settings_put (t064, EXECUTE to haloflow_support, D8).
- `haloflow.m05.reminder_statements`: SH2_STATEMENTS (keys EXAMINE_KEY, CONSUME_KEY,
  CLAIM_V2_KEY, READ_V2_KEY, READY_KEY, SNAPSHOT_KEY under the dispatch capability),
  SH2_DIRECTORY_STATEMENTS (the EXISTING key m05.intake.directory bound to
  m05_directory_v2) and SH2_PROTECTED (examine, consume, claim_v2). The H2 test catalogue
  drops m05.outbound.prepare / .claim / .read and the S-A directory binding.
- `haloflow.m05.reminder_text`: reminder_text(first_name, start_utc, zone,
  clinic_display_name) and first_name_valid(value).
- `haloflow.m05.reminder`: ReminderService(foundation) (an OutboundService adding examine,
  consume, claim_v2, read_v2, ready, render_snapshot); ReminderRunner(service, worker, *,
  batch, lease_seconds).run_once(context) -> report with integer `examined`, `claimed`,
  `sent`.
- `haloflow.m05.outbound.OutboundWorker.run_claimed(context, op_id, attempt_id)`: consume ->
  render(inputs) -> hash check -> one send -> outcome -> register. Results "accepted",
  "refused", "uncertain", "not_sent" (consume refused) and "consume_unknown" (commit
  unknown: no send, no retry).
- Settings (t057, singleton): lead_days 2, send_time 10:00, window 09:00-19:00,
  clinic_tz NULL, clinic_display_name NULL, settings_version 1 at install.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Final
from zoneinfo import ZoneInfo

import m02_support
import psycopg
import sa_support as sa
import sb_support as sb
import sc_support as sc
import sh_support as sh
from psycopg import sql
from psycopg.types.json import Jsonb

SH2_UNIT_IDS: Final = (
    "t057_m05_reminder_settings", "t058_m05_appointment_profile",
    "t059_m05_outbound_schedule", "t060_m05_outbound_render_inputs",
    "t061_m05_outbound_send_tickets", "t062_m05_reminder_decision",
    "t063_m05_directory_v2", "t064_m05_reminder_settings_put",
    "t065_m05_outbound_prepare_rendered", "t066_m05_outbound_claim_v2",
    "t067_m05_outbound_read_v2", "t068_m05_outbound_ready",
    "t069_m05_outbound_render_snapshot", "t070_m05_outbound_examine",
    "t071_m05_outbound_consume", "t072_m05_h2_runtime_closure",
    "t073_m05_reminder_bootstrap",
)
SH2_TARGET: Final = 73
SH2_SUPPORTED: Final = range(SH2_TARGET, SH2_TARGET + 1)
TABLE_UNITS: Final = frozenset(SH2_UNIT_IDS[:5]) | {"t073_m05_reminder_bootstrap"}
CLOSURE_UNIT: Final = "t072_m05_h2_runtime_closure"
RUNTIME_FUNCTIONS: Final = (
    "m05_directory_v2", "m05_outbound_claim_v2", "m05_outbound_read_v2",
    "m05_outbound_ready", "m05_outbound_render_snapshot", "m05_outbound_examine",
    "m05_outbound_consume",
)
OWNER_FUNCTIONS: Final = ("m05_reminder_decision", "m05_outbound_prepare_rendered")
SUPPORT_FUNCTION: Final = "m05_reminder_settings_put"
SUPPORT_ROLE: Final = "haloflow_support"
SETTINGS_CAP: Final = "m05.reminder.settings"
# Closed by t072 (the unchanged H1 / S-A bodies stay installed for owner-internal use).
CLOSED: Final = (("t038_m05_directory_put", "m05_directory_put"),
                 ("t048_m05_outbound_prepare", "m05_outbound_prepare"),
                 ("t049_m05_outbound_claim", "m05_outbound_claim"),
                 ("t055_m05_outbound_read", "m05_outbound_read"))
SKIP_CODES: Final = ("NAME_INVALID", "TZ_UNRESOLVED", "UNROUTABLE_PHONE", "STATUS_UNSUPPORTED",
                     "COMPLETED", "PROFILE_MISSING", "SETTINGS_INCOMPLETE", "CONFIRMED",
                     "OPTED_OUT")
EXAMINE_OUTCOMES: Final = frozenset({"claimed", "deferred", "skipped", "not_ready", "removed"})
CLINIC: Final = "HaloVox Test Clinic"
FIRST_NAME: Final = "Synth"
NY: Final = "America/New_York"
FLOOR: Final = timedelta(hours=26)
RECHECK: Final = timedelta(hours=1)

# The name rule (pins D10r1, Codex d350083 item 4): NFC, 1-40 characters (clinic display
# name 1-60), no leading/trailing whitespace, and no code point whose Unicode general category
# is in FORBIDDEN_CATEGORIES, nor any code point in EMOJI_RANGES. The implementation commits
# one literal range table used by BOTH the SQL and the Python check; U02 verifies it against
# the running unicodedata for every code point (so a missing Cf such as U+0600 fails).
FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})
EMOJI_RANGES: Final = ((0x2600, 0x27BF), (0xFE00, 0xFE0F), (0x1F000, 0x1FAFF),
                       (0xE0100, 0xE01EF))
NAMES_OK: Final = ("Synth", "Jos\u00e9", "Zo\u00eb", "Nguy\u1ec5n", "O'Brien", "Anne-Marie",
                   "\u674e", "\u0645\u062d\u0645\u062f", "Ana Mar\u00eda", "x" * 40)
# Representative refusals for the SQL rows (and the clinic display name): every class above,
# including the Cf characters Codex found missing from the v1 table.
NAMES_BAD: Final = (
    "", " ", " Synth", "Synth ", "x" * 41,
    "Syn\u200bth", "Syn\nth", "Syn\u0007th", "Syn\u0085th", "Synth\U0001f600",
    "Syn\u2764th", "Syn\ufe0fth", "Syn\u202eth", "Syn\u2066th", "Syn\u00adth",
    "Jose\u0301", "Syn\ufeffth", "Syn\ue000th", "Syn\u2028th",
    "Syn\u0600th", "Syn\u06ddth", "Syn\u070fth", "Syn\U000110bdth", "Syn\U000e0001th",
    "Syn\ufff9th", "Syn\U000f0000th",
)


# --- registry, catalogue, install ---------------------------------------------------------


def sh2_units() -> Any:
    from haloflow.m05.reminder_units import SH2_TENANT_MIGRATIONS

    return SH2_TENANT_MIGRATIONS


def sh2_profiles() -> Any:
    from haloflow.m05.reminder_units import SH2_INSTALLED_STATE_PROFILES

    return SH2_INSTALLED_STATE_PROFILES


def sh2_amendments() -> Any:
    from haloflow.m05.reminder_units import SH2_ACL_AMENDMENTS

    return SH2_ACL_AMENDMENTS


def sh2_registry() -> Any:
    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, sa.sa_units(), sb.sb_units(), sc.sc_units(),
        sh.sh_units(), sh2_units(),
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **sa.sa_profiles(),
                                  **sb.sb_profiles(), **sc.sc_profiles(),
                                  **sh.sh_profiles(), **sh2_profiles()},
        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *sh.sh_amendments(), *sh2_amendments()),
    )


def sh2_statements() -> Any:
    from haloflow.m05.reminder_statements import SH2_STATEMENTS

    return SH2_STATEMENTS


def sh2_directory_statements() -> Any:
    from haloflow.m05.reminder_statements import SH2_DIRECTORY_STATEMENTS

    return SH2_DIRECTORY_STATEMENTS


def protected() -> frozenset[str]:
    from haloflow.m05.reminder_statements import SH2_PROTECTED

    return frozenset(sh.protected() | SH2_PROTECTED) - CLOSED_KEYS()


def CLOSED_KEYS() -> frozenset[str]:  # noqa: N802 - a constant computed lazily
    from haloflow.m05.outbound_statements import CLAIM_KEY, PREPARE_KEY, READ_KEY

    return frozenset({PREPARE_KEY, CLAIM_KEY, READ_KEY})


def sh2_catalog() -> Any:
    from types import MappingProxyType

    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import build_statement_catalog
    from haloflow.m05.intake_statements import DIRECTORY_KEY

    sa_part = MappingProxyType({k: v for k, v in sh.sa_statements_without_inbound().items()
                                if k != DIRECTORY_KEY})
    sh_part = MappingProxyType({k: v for k, v in sh.sh_statements().items()
                                if k not in CLOSED_KEYS()})
    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, sa_part, sb.sb_statements(),
                                   sh_part, sh.sh_inbound_statements(), sh2_statements(),
                                   sh2_directory_statements(),
                                   activation_protected=protected())


class SH2Install(sa.SAInstall):
    """The S-A install shape, provisioned through the H2 registry (target 73). After
    install, FIXTURE: the clinic display name is set (the seeded default is NULL)."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, sh2_registry(), self.tenant,
                                           supported=SH2_SUPPORTED, manifest=staff_manifest())
                if self.profile:
                    sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                    settings_raw(self, clinic_display_name=CLINIC)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"S-H H2 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[SH2Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield SH2Install(ids, tenant)
    finally:
        reset(tenant)


@asynccontextmanager
async def runtime(install: SH2Install, *, policy: str = "ENFORCE") -> AsyncIterator[Any]:
    """An H2 TEST runtime (target 73, H2 catalogue, real resolver and gateway)."""

    import service_support as sv

    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=4)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=sh2_registry(),
            catalog=sh2_catalog(),
            supported_schema_versions=frozenset({SH2_TARGET}),
            activation_policy=ActivationPolicy[policy],
        )
    finally:
        await pool.close()


async def dispatch_context(rt: Any, install: SH2Install) -> Any:
    import service_support as sv

    return await sv.context(rt, install, sa.W_DISPATCH, sa.CAPS["dispatch"], kind="workload")


def service(rt: Any) -> Any:
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.reminder import ReminderService

    return ReminderService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER))


# --- clock and time fixtures (D7: integration rows use the REAL database clock) -----------


def db_now(install: sa.SAInstall) -> datetime:
    value: datetime = sa.admin(install, "SELECT clock_timestamp()")[0][0]
    return value


def fixed_zone(offset_hours: int) -> str:
    """The IANA Etc zone whose UTC offset is `offset_hours` (Etc signs are inverted)."""

    if offset_hours == 0:
        return "Etc/GMT"
    return f"Etc/GMT{'-' if offset_hours > 0 else '+'}{abs(offset_hours)}"


def zone_with_local_hour(now: datetime, lo: int, hi: int) -> str:
    """A valid IANA zone in which the local hour of `now` is within [lo, hi]."""

    for target in range(lo, hi + 1):
        base = (target - now.astimezone(UTC).hour) % 24
        for offset in (base, base - 24):
            if -12 <= offset <= 14:
                return fixed_zone(offset)
    raise AssertionError("no Etc zone fits")  # unreachable for a window of >= 3 hours


def at_local(now: datetime, zone: str, days: int, hour: int, minute: int = 0) -> datetime:
    """The UTC instant of local (today + days) at hour:minute in `zone`."""

    local_day: date = now.astimezone(ZoneInfo(zone)).date() + timedelta(days=days)
    return datetime.combine(local_day, dtime(hour, minute), ZoneInfo(zone)).astimezone(UTC)


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def in_window(install: sa.SAInstall) -> tuple[str, datetime]:
    """A zone where local now is 11:00-15:59, and a start two local days ahead at 18:00:
    due (lead 2, 10:00) has passed, now is inside 09:00-19:00, start - now > 26 h."""

    now = db_now(install)
    zone = zone_with_local_hour(now, 11, 15)
    return zone, at_local(now, zone, 2, 18)


def after_window(install: sa.SAInstall) -> tuple[str, datetime]:
    """A zone where local now is 20:00-23:59; start two local days ahead at 23:00."""

    now = db_now(install)
    zone = zone_with_local_hour(now, 20, 23)
    return zone, at_local(now, zone, 2, 23)


# --- settings (FIXTURE raw writes vs the real support path) ---------------------------------


SETTINGS_DEFAULTS: Final = {"lead_days": 2, "send_time": "10:00", "window_start": "09:00",
                            "window_end": "19:00", "clinic_tz": None,
                            "clinic_display_name": CLINIC}


def settings_raw(install: sa.SAInstall, **values: Any) -> None:
    """FIXTURE escape hatch: write settings columns directly (bumps settings_version, does
    NOT re-key the schedule). Rows whose subject is the settings write use settings_put."""

    assert values and set(values) <= set(SETTINGS_DEFAULTS), values
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(k)) for k in values)
    sa.admin(install, sql.SQL("UPDATE {}.m05_reminder_settings SET {},"
                              " settings_version = settings_version + 1").format(
        sql.Identifier(install.schema), assignments), tuple(values.values()))


def reset_settings(install: sa.SAInstall) -> None:
    settings_raw(install, **SETTINGS_DEFAULTS)


def settings_row(install: sa.SAInstall) -> dict[str, Any]:
    rows = sa.admin(install, sa.q(install, "SELECT to_jsonb(s) FROM {s}.m05_reminder_settings"
                                           " AS s"))
    assert len(rows) == 1, rows
    value: dict[str, Any] = rows[0][0]
    return value


def settings_cmd(**values: Any) -> dict[str, Any]:
    body = {**SETTINGS_DEFAULTS, **values}
    return {"schema_version": 1, **body}


def call_as(conninfo: str, install: sa.SAInstall, function: str, command: Any
            ) -> dict[str, Any]:
    """One committed call on an arbitrary login (support fixture logins)."""

    with psycopg.connect(conninfo) as conn:
        result = sa.call_in(conn, install, function, command)
        conn.commit()
    return result


# --- owner-internal calls (FIXTURE / direct decision rows) ----------------------------------


def owner_call(install: sa.SAInstall, function: str, command: Any) -> dict[str, Any]:
    """The admin connection calls an owner-only function with the tenant context set."""

    with psycopg.connect(install.ids.admin) as conn:
        result = sa.call_in(conn, install, function, command)
        conn.commit()
    return result


def decision(install: sa.SAInstall, *, now: datetime, start: datetime, zone: str,
             lead_days: int = 2, send_time: str = "10:00", window_start: str = "09:00",
             window_end: str = "19:00") -> dict[str, Any]:
    return owner_call(install, "m05_reminder_decision", {
        "schema_version": 1, "now": iso_z(now), "start_at": iso_z(start), "zone": zone,
        "lead_days": lead_days, "send_time": send_time, "window_start": window_start,
        "window_end": window_end})


# --- directory v2 ---------------------------------------------------------------------------


def appointment_fields(ref: str, patient: str, start: datetime | str, *,
                       status: str = "booked", first_name: str | None = FIRST_NAME,
                       confirmation: str | None = "not_confirmed",
                       department_tz: str | None = NY,
                       source_status: str | None = "booked") -> dict[str, Any]:
    return {"appointment_ref": ref, "patient_id": patient,
            "start_at": start if isinstance(start, str) else iso_z(start), "status": status,
            "first_name": first_name, "confirmation": confirmation,
            "department_tz": department_tz, "source_status": source_status}


def directory2(install: sa.SAInstall, op: str, **fields: Any) -> dict[str, Any]:
    return sa.call(install, "m05_directory_v2", sa.directory_cmd(op, **fields))


def contact_put(install: sa.SAInstall, phone: str, patient: str) -> None:
    directory2(install, "contact_put", phone=phone, patient_id=patient)


def appointment_put(install: sa.SAInstall, ref: str, patient: str, start: datetime | str,
                    **kw: Any) -> dict[str, Any]:
    return directory2(install, "appointment_put",
                      **appointment_fields(ref, patient, start, **kw))


def seed(install: sa.SAInstall, start: datetime, *, zone: str | None = NY, phones: int = 1,
         phone: str | None = None, **kw: Any) -> tuple[str, str | None, str]:
    """A new patient (0, 1 or 2 phones) with one booked appointment, via directory v2."""

    patient, ref = sa.new_id(), sa.new_id()
    first: str | None = None
    for i in range(phones):
        number = phone if (phone is not None and i == 0) else sa.fresh_phone()
        contact_put(install, number, patient)
        first = first or number
    appointment_put(install, ref, patient, start, department_tz=zone, **kw)
    return patient, first, ref


# --- schedule / inputs / tickets / profile inspection ---------------------------------------


def _one_or_none(install: sa.SAInstall, query: str, key: Any) -> dict[str, Any] | None:
    rows = sa.admin(install, sa.q(install, query), (key,))
    assert len(rows) <= 1, rows
    return None if not rows else rows[0][0]


def schedule_row(install: sa.SAInstall, ref: str) -> dict[str, Any] | None:
    return _one_or_none(install, "SELECT to_jsonb(r) FROM {s}.m05_outbound_schedule AS r"
                                 " WHERE appointment_ref = %s", ref)


def next_check(install: sa.SAInstall, ref: str) -> datetime:
    rows = sa.admin(install, sa.q(install, "SELECT next_check_at FROM {s}.m05_outbound_schedule"
                                           " WHERE appointment_ref = %s"), (ref,))
    assert len(rows) == 1, rows
    value: datetime = rows[0][0]
    return value


def inputs_row(install: sa.SAInstall, op_id: str) -> dict[str, Any] | None:
    return _one_or_none(install, "SELECT to_jsonb(r) FROM {s}.m05_outbound_render_inputs AS r"
                                 " WHERE op_id = %s", op_id)


def ticket_row(install: sa.SAInstall, op_id: str) -> dict[str, Any] | None:
    return _one_or_none(install, "SELECT to_jsonb(t) FROM {s}.m05_outbound_send_tickets AS t"
                                 " WHERE op_id = %s", op_id)


def profile_row(install: sa.SAInstall, ref: str) -> dict[str, Any] | None:
    return _one_or_none(install, "SELECT to_jsonb(p) FROM {s}.m05_appointment_profile AS p"
                                 " WHERE appointment_ref = %s", ref)


def op_for(install: sa.SAInstall, ref: str) -> dict[str, Any] | None:
    return _one_or_none(install, "SELECT to_jsonb(o) FROM {s}.m05_outbound_ops AS o"
                                 " WHERE appointment_ref = %s", ref)


def clear_schedule(install: sa.SAInstall) -> None:
    """FIXTURE: runner rows start from an empty queue (other rows' refs are removed)."""

    sa.admin(install, sa.q(install, "DELETE FROM {s}.m05_outbound_schedule"))


H2_TABLES: Final = ("m05_reminder_settings", "m05_appointment_profile",
                    "m05_outbound_schedule", "m05_outbound_render_inputs",
                    "m05_outbound_send_tickets", "m05_outbound_ops", "m05_reminder_windows",
                    "m05_contact_state", "m05_appointments", "m05_patient_contacts")


def h2_snapshot(install: sa.SAInstall) -> dict[str, list[str]]:
    return {t: sorted(r[0] for r in sa.admin(
        install, sa.q(install, "SELECT to_jsonb(x)::text FROM {s}." + t + " AS x")))
        for t in H2_TABLES}


def refused(install: sa.SAInstall, function: str, command: Any, code: str) -> None:
    """P0001 `code` and zero writes across the H2 tables."""

    before = h2_snapshot(install)
    try:
        sa.call(install, function, command)
    except psycopg.Error as error:
        assert (error.sqlstate, error.diag.message_primary) == ("P0001", code), (
            error.sqlstate, error.diag.message_primary)
    else:
        raise AssertionError(f"expected P0001 {code}")
    assert h2_snapshot(install) == before, "a refused call wrote something"


# --- runtime SQL helpers (dispatch workload) ------------------------------------------------


def _attr() -> dict[str, Any]:
    return sa.attribution(sa.W_DISPATCH)


def snapshot_cmd(ref: str) -> dict[str, Any]:
    return {"schema_version": 1, "appointment_ref": ref, "attribution": _attr()}


def render_snapshot(install: sa.SAInstall, ref: str) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_render_snapshot", snapshot_cmd(ref))


def rendered_hash(snap: dict[str, Any]) -> str | None:
    """The Python rendering of a snapshot (None when any input is missing)."""

    from haloflow.m05.reminder_text import reminder_text

    needed = ("first_name", "clinic_display_name", "zone", "start_at")
    if any(snap.get(k) is None for k in needed):
        return None
    start = datetime.fromisoformat(str(snap["start_at"]).replace("Z", "+00:00"))
    text = reminder_text(snap["first_name"], start, snap["zone"], snap["clinic_display_name"])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def examine_cmd(ref: str, *, op_id: str | None = None, reminder: str | None = None,
                lease_seconds: int = 120, snap: dict[str, Any] | None = None,
                text_sha256: Any = "auto") -> dict[str, Any]:
    snap = snap or {}
    return {"schema_version": 1, "appointment_ref": ref, "op_id": op_id or sa.new_id(),
            "reminder_id": reminder or sa.new_id(), "lease_seconds": lease_seconds,
            "text_sha256": rendered_hash(snap) if text_sha256 == "auto" and snap
            else (None if text_sha256 == "auto" else text_sha256),
            "source_version": snap.get("source_version"),
            "settings_version": snap.get("settings_version"), "attribution": _attr()}


def examine(install: sa.SAInstall, ref: str, **kw: Any) -> dict[str, Any]:
    """Render from a fresh snapshot, then examine (the runner's two steps, synchronous)."""

    snap = kw.pop("snap", None) or render_snapshot(install, ref)
    return sa.call(install, "m05_outbound_examine", examine_cmd(ref, snap=snap, **kw))


def consume_cmd(op_id: str, attempt: str) -> dict[str, Any]:
    return {"schema_version": 1, "op_id": op_id, "attempt_id": attempt, "attribution": _attr()}


def consume(install: sa.SAInstall, op_id: str, attempt: str) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_consume", consume_cmd(op_id, attempt))


def claim_v2(install: sa.SAInstall, op_id: str, fence: int, lease_seconds: int = 120
             ) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_claim_v2", sh.claim_cmd(op_id, fence, lease_seconds))


def read_v2_cmd(op_id: str, attempt: str) -> dict[str, Any]:
    return {"schema_version": 1, "op_id": op_id, "attempt_id": attempt, "attribution": _attr()}


def ready(install: sa.SAInstall, limit: int = 100) -> list[str]:
    result = sa.call(install, "m05_outbound_ready",
                     {"schema_version": 1, "limit": limit, "attribution": _attr()})
    return [str(r) for r in result["refs"]]


def prepare_rendered_cmd(ref: str, snap: dict[str, Any], *, op_id: str | None = None,
                         reminder: str | None = None, digest: str | None = None
                         ) -> dict[str, Any]:
    return {"schema_version": 1, "op_id": op_id or sa.new_id(), "appointment_ref": ref,
            "reminder_id": reminder or sa.new_id(),
            "text_sha256": digest or rendered_hash(snap),
            "source_version": snap["source_version"],
            "settings_version": snap["settings_version"], "attribution": _attr()}


def prepare_rendered(install: sa.SAInstall, ref: str, **kw: Any) -> dict[str, Any]:
    """FIXTURE (owner-internal t065): a prepared H2 op with render inputs, unclaimed."""

    snap = kw.pop("snap", None) or render_snapshot(install, ref)
    return owner_call(install, "m05_outbound_prepare_rendered",
                      prepare_rendered_cmd(ref, snap, **kw))


def claimed(install: sa.SAInstall, ref: str, *, lease_seconds: int = 120) -> tuple[str, str]:
    """examine -> claimed; returns (op_id, attempt_id)."""

    result = examine(install, ref, lease_seconds=lease_seconds)
    assert result["outcome"] == "claimed", result
    return str(result["op_id"]), str(result["attempt_id"])


# --- support fixture (D8) -------------------------------------------------------------------


@contextmanager
def support_login(env: Any, install: sa.SAInstall, key: str, *,
                  caps: tuple[str, ...] | None = (SETTINGS_CAP,), **grant: Any
                  ) -> Iterator[str]:
    """A fixture support login (L-1W harness), allow-listed, with an optional grant on
    this tenant. Yields its conninfo."""

    import l1w_db_support as db

    actor = f"support-{env.token}-{key}"
    if key not in env.logins:  # a login is created and allow-listed once per run
        env.login(key, db.SUPPORT)
        env.allowlist_support(key, actor)
    if caps is not None:
        env.support_grant(actor, install.tenant_id, list(caps), **grant)
    yield env.conninfo(key)


def settings_put(conninfo: str, install: sa.SAInstall, **values: Any) -> dict[str, Any]:
    return call_as(conninfo, install, SUPPORT_FUNCTION, settings_cmd(**values))


# --- held-lock seam for arbitrary logins ----------------------------------------------------


class LoginWaiter:
    """Like sa.Waiter, on an arbitrary conninfo (support logins); it must block."""

    def __init__(self, install: sa.SAInstall, conninfo: str, function: str, command: Any,
                 *, set_role: str | None = None) -> None:
        self.install = install
        self.name = f"sh2-waiter-{uuid.uuid4().hex[:12]}"
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                with psycopg.connect(conninfo, autocommit=True,
                                     application_name=self.name) as conn:
                    if set_role:
                        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(set_role)))
                    with conn.transaction():
                        self.result = sa.call_in(conn, install, function, command)
            except BaseException as error:  # noqa: BLE001 - re-raised in join()
                self.error = error

        self.thread = threading.Thread(target=run, daemon=True)

    def start_and_block(self) -> LoginWaiter:
        self.thread.start()
        sa.wait_until_blocked(self.install, self.name)
        return self

    def join(self) -> dict[str, Any]:
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "waiter did not finish"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def wait_past(install: sa.SAInstall, instant: datetime) -> None:
    """Poll the database clock until it is past `instant` (lease rows; bounded)."""

    deadline = time.monotonic() + 10
    while db_now(install) <= instant:
        assert time.monotonic() < deadline, "database clock did not pass the instant"
        time.sleep(0.05)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def expire_lease(install: sa.SAInstall, op_id: str) -> None:
    """FIXTURE time travel (the H1 P08 pattern): the attempt's lease is in the past."""

    sa.admin(install, sa.q(install, "UPDATE {s}.m05_outbound_ops SET lease_until ="
                                    " clock_timestamp() - interval '1 second'"
                                    " WHERE op_id = %s"), (op_id,))


async def gateway_directory(rt: Any, install: sa.SAInstall, op: str, **fields: Any
                            ) -> dict[str, Any]:
    """The REBOUND gateway key m05.intake.directory (-> m05_directory_v2), executed through
    the real resolver and gateway with the directory workload's capabilities."""

    import service_support as sv

    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.intake import _attribution
    from haloflow.m05.intake_statements import DIRECTORY_KEY

    ctx = await sv.context(rt, install, sa.W_DIRECTORY, sa.CAPS["directory"], kind="workload")
    command = {"schema_version": 1, "op": op, **fields, "attribution": _attribution(ctx)}

    async def body(binding: Any) -> dict[str, Any]:
        rows = await binding.handle.fetch_all(DIRECTORY_KEY, {"command": Jsonb(command)})
        assert len(rows) == 1, rows
        return dict(rows[0][0])

    result: dict[str, Any] = await EventFoundation(
        rt.gateway, producer_service=sa.PRODUCER).run_in_transaction(ctx, body)
    return result


def examine_kwargs(snap: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """ReminderService.examine keyword arguments from a snapshot (the runner's render)."""

    return {"op_id": kw.get("op_id") or sa.new_id(),
            "reminder_id": kw.get("reminder_id") or sa.new_id(),
            "lease_seconds": kw.get("lease_seconds", 120), "text_sha256": rendered_hash(snap),
            "source_version": snap["source_version"],
            "settings_version": snap["settings_version"]}
