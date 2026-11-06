"""L-1S package S-A (reply/no-reply -> follow-up item producer): frozen test manifest.

Sources (aligned; nothing here is inferred from source under test):
- S-A requirements v1r1, SA-TIME-1 state model v1, architecture v1 + v1r1 delta
  (Codex ALIGN for red-test preparation, codex_sa-architecture-v1r1-align-2155.md);
- S-A interface pins v1 (claude_sa-interface-pins-v1-2026-11-05.md), which fixes the
  names, shapes and codes asserted here, with corrections C1..C7;
- owner decisions D-S1..D-S4 (2026-11-05), D-S2 STOP table rows 1..7.

S-A installs ONLY through the S-A test registry below (target 42). The production
registry, target {36}, catalogue and manifests are unchanged (static row SA-S01).
Synthetic data only. Expected values are literals: a disagreement with the
implementation is a finding for review, never something to edit to match.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Final

import m02_support
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

M05_OWNER: Final = "haloflow_m05_owner"
MIGRATOR: Final = "haloflow_migrator"
RUNTIME: Final = "haloflow_runtime"

# --- units, functions, statements (pins section 2, section 6) -----------------

SA_UNIT_IDS: Final = (
    "t037_m05_intake_tables",
    "t038_m05_directory_put",
    "t039_m05_register_dispatch",
    "t040_m05_register_inbound",
    "t041_m05_close_window",
    "t042_m05_activation",
)
SA_TARGET: Final = 42
SA_SUPPORTED: Final = range(SA_TARGET, SA_TARGET + 1)
PRODUCTION_TARGET: Final = 36  # unchanged by S-A

# Runtime gateways: unit -> (function, statement key, capability). All WRITE.
RUNTIME_FUNCTIONS: Final = {
    "t038_m05_directory_put": ("m05_directory_put", "m05.intake.directory",
                               "m05.intake.directory"),
    "t039_m05_register_dispatch": ("m05_register_dispatch", "m05.intake.dispatch",
                                   "m05.intake.dispatch"),
    "t040_m05_register_inbound": ("m05_register_inbound", "m05.intake.inbound",
                                  "m05.intake.inbound"),
    "t041_m05_close_window": ("m05_close_window", "m05.intake.close", "m05.intake.close"),
}
# t042: owner-only (no runtime, no PUBLIC, no statement key).
ACTIVATION_FUNCTIONS: Final = (
    ("m05_activation_change", ("jsonb",), "v"),
    ("m05_activation_state", (), "s"),
)
HELPER_FUNCTION: Final = "intake_reply_is_timely"  # (timestamptz, timestamptz), IMMUTABLE

# ME-01 extended: the m05_ function namespace in an S-A tenant is exactly these.
M05_NAMESPACE_SA: Final = frozenset({
    "m05_create_item", "m05_prepare_action", "m05_apply_action", "m05_get_item",
    "m05_list_items", "m05_get_history", "m05_read_note", "m05_prepare_action_v2",
    "m05_apply_action_v2",
    "m05_directory_put", "m05_register_dispatch", "m05_register_inbound",
    "m05_close_window", "m05_activation_change", "m05_activation_state",
})

FUNCTION_ACL_RUNTIME: Final = (
    (M05_OWNER, "EXECUTE", M05_OWNER, False),
    (RUNTIME, "EXECUTE", M05_OWNER, False),
)
FUNCTION_ACL_OWNER_ONLY: Final = ((M05_OWNER, "EXECUTE", M05_OWNER, False),)

STATEMENTS: Final = {
    "m05.intake.directory": ("write", "m05.intake.directory",
                             "SELECT m05_directory_put(%(command)s)"),
    "m05.intake.dispatch": ("write", "m05.intake.dispatch",
                            "SELECT m05_register_dispatch(%(command)s)"),
    "m05.intake.inbound": ("write", "m05.intake.inbound",
                           "SELECT m05_register_inbound(%(command)s)"),
    "m05.intake.close": ("write", "m05.intake.close", "SELECT m05_close_window(%(command)s)"),
}
APPEND_CAP: Final = "m02.observation.append"
CREATE_CAP: Final = "m05.followup.create"
CAPS: Final = {
    "directory": frozenset({"m05.intake.directory"}),
    "dispatch": frozenset({"m05.intake.dispatch", APPEND_CAP}),
    "inbound": frozenset({"m05.intake.inbound", APPEND_CAP, CREATE_CAP}),
    "close": frozenset({"m05.intake.close", APPEND_CAP, CREATE_CAP}),
}
PRODUCER: Final = "m05_intake"  # C1

# --- tables (pins section 3) ---------------------------------------------------

TABLES: Final = (
    "m05_intake_workloads",
    "m05_patient_contacts",
    "m05_appointments",
    "m05_contact_state",
    "m05_reminder_windows",
    "m05_inbound_replies",
    "m05_activation_records",
)
COLUMNS: Final = {
    "m05_intake_workloads": ("workload_role", "workload_id"),
    "m05_patient_contacts": ("phone", "patient_id"),
    "m05_appointments": ("appointment_ref", "patient_id", "start_at", "status"),
    "m05_contact_state": ("phone", "opted_out", "opted_out_at"),
    "m05_reminder_windows": (
        "reminder_id", "appointment_ref", "patient_id", "phone", "sent_at", "deadline",
        "state", "dispatch_fingerprint", "dispatch_event_id", "closed_decision",
        "closed_event_id",
    ),
    "m05_inbound_replies": (
        "inbound_id", "channel", "provider_message_id", "phone", "reply_class", "received_at",
        "fingerprint", "decision", "triage_kind", "patient_id", "appointment_ref",
        "reminder_id", "matched_reminders", "event_id",
    ),
    "m05_activation_records": (
        "record_id", "change", "actor_id", "ticket_reference", "reason", "created_at",
    ),
}
IMMUTABLE_TABLES: Final = ("m05_inbound_replies", "m05_activation_records")
WINDOW_STATES: Final = ("awaiting", "replied", "opted_out", "no_reply")
REPLY_CLASSES: Final = ("confirm", "reschedule", "cancel", "unclear", "stop")
DECISIONS: Final = ("opt_out", "quarantined", "recorded", "item")

# --- contracts (pins section 5) --------------------------------------------------

FAMILY: Final = "m05_intake"
T_DISPATCHED: Final = "m05_reminder_dispatched"
T_REPLY: Final = "m05_reminder_reply_received"
T_NO_REPLY: Final = "m05_reminder_no_reply"
CONTRACT_TYPES: Final = (T_DISPATCHED, T_REPLY, T_NO_REPLY)
ALLOWLIST: Final = {
    T_DISPATCHED: ("appointment_ref", "reminder_id"),
    T_REPLY: ("appointment_ref", "inbound_id", "reminder_id", "reply_class", "triage_kind"),
    T_NO_REPLY: ("appointment_ref", "reminder_id", "triage_kind"),
}

# Timestamps in results leave as UTC with microseconds and a literal Z (the M05 rule).
TIMESTAMP_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"

# --- error codes -----------------------------------------------------------------

CODES: Final = (
    "COMMAND_MALFORMED", "WORKLOAD_NOT_AUTHORIZED", "DISPATCH_UNROUTABLE",
    "CONTACT_OPTED_OUT", "DISPATCH_CONFLICT", "INBOUND_CONFLICT", "WINDOW_NOT_FOUND",
)

# --- synthetic principals and directory -------------------------------------------

W_DIRECTORY: Final = "00000000-0000-4000-8000-00000005a001"
W_DISPATCH: Final = "00000000-0000-4000-8000-00000005a002"
W_INGRESS: Final = "00000000-0000-4000-8000-00000005a003"
W_TIMER: Final = "00000000-0000-4000-8000-00000005a004"
W_OTHER: Final = "00000000-0000-4000-8000-00000005a0ff"
ACTOR: Final = "00000000-0000-4000-8000-00000005aa01"
WORKLOADS: Final = {
    "directory": W_DIRECTORY, "dispatch": W_DISPATCH, "ingress": W_INGRESS, "timer": W_TIMER,
}

P1: Final = "00000000-0000-4000-8000-00000005b001"
P2: Final = "00000000-0000-4000-8000-00000005b002"
P3: Final = "00000000-0000-4000-8000-00000005b003"
PHONE_1: Final = "+15550100001"       # P1 only
PHONE_SHARED: Final = "+15550100002"  # P2 and P3 (P3 has no window)
PHONE_UNKNOWN: Final = "+15550100009"

SA_TEST_PROFILE_SQL: Final = f"""
UPDATE {{schema}}.ref_action_families SET is_active = true WHERE action_family = '{FAMILY}';
UPDATE {{schema}}.event_types SET is_active = true
 WHERE event_type IN ('{T_DISPATCHED}', '{T_REPLY}', '{T_NO_REPLY}');
INSERT INTO {{schema}}.m05_intake_workloads (workload_role, workload_id) VALUES
    ('directory', '{W_DIRECTORY}'), ('dispatch', '{W_DISPATCH}'),
    ('ingress', '{W_INGRESS}'), ('timer', '{W_TIMER}');
INSERT INTO {{schema}}.m05_intake_map (source_event_type, source_contract_version,
    discriminator_path, discriminator_value, item_kind, producer_workload_id,
    active_in_production) VALUES
    ('{T_REPLY}', 1, 'triage_kind', 'reschedule_request', 'reschedule_request',
     '{W_INGRESS}', false),
    ('{T_REPLY}', 1, 'triage_kind', 'cancel_request', 'cancel_request', '{W_INGRESS}', false),
    ('{T_REPLY}', 1, 'triage_kind', 'unclear_reply', 'unclear_reply', '{W_INGRESS}', false),
    ('{T_NO_REPLY}', 1, 'triage_kind', 'no_reply', 'no_reply', '{W_TIMER}', false);
"""

# --- digest vectors (independent literals, computed 2026-11-05 with
# json.dumps(..., separators=(",", ":"), ensure_ascii=False) + hashlib.sha256) ------

DISPATCH_VECTOR: Final = (
    ("pilot-clinic-01", "00000000-0000-4000-8000-00000005a101",
     "00000000-0000-4000-8000-0000000a0101"),
    '["m05.dispatch/v1","pilot-clinic-01","00000000-0000-4000-8000-00000005a101",'
    '"00000000-0000-4000-8000-0000000a0101"]',
    "62e9c76586cda43b63d34a99abda4cf77e695f29d1ba91fb5d50760a482e89de",
)
INBOUND_VECTORS: Final = (
    (
        ("pilot-clinic-01", "sms", "SM-0001", "+15550100001", "reschedule"),
        '["m05.inbound/v1","pilot-clinic-01","sms","SM-0001","+15550100001","reschedule"]',
        "7d532c5ec87b3691a22d9ab56da1bc8bb94f4e7ef8a77370f2afba0f9c5b8e58",
    ),
    (
        ("pilot-clinic-01", "sms", "SM-0001", "+15550100001", "stop"),
        '["m05.inbound/v1","pilot-clinic-01","sms","SM-0001","+15550100001","stop"]',
        "71811f142aea50780985b730af625401922438672fcada6a2f74525fd9afd3fb",
    ),
)


def _compact(parts: list[Any]) -> str:
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=False)


def dispatch_fingerprint(tenant: str, reminder_id: str, appointment_ref: str) -> bytes:
    return hashlib.sha256(
        _compact(["m05.dispatch/v1", tenant, reminder_id, appointment_ref]).encode()
    ).digest()


def inbound_fingerprint(tenant: str, channel: str, message_id: str, phone: str,
                        reply_class: str) -> bytes:
    return hashlib.sha256(
        _compact(["m05.inbound/v1", tenant, channel, message_id, phone, reply_class]).encode()
    ).digest()


# --- registry and install ---------------------------------------------------------


def sa_units() -> Any:
    """The S-A unit set (deferred import: absent before implementation)."""

    from haloflow.m05.intake_units import SA_TENANT_MIGRATIONS

    return SA_TENANT_MIGRATIONS


def sa_profiles() -> Any:
    from haloflow.m05.intake_units import SA_INSTALLED_STATE_PROFILES

    return SA_INSTALLED_STATE_PROFILES


def sa_registry() -> Any:
    """Production unit sets, roles, profiles and ACL amendments + the S-A units and
    their profiles. No `allow_test_units`: the S-A units are module units installed
    only through this TEST registry (pins section 1)."""

    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        sa_units(),
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **sa_profiles()},
        acl_amendments=APPROVED_ACL_AMENDMENTS,
    )


def sa_statements() -> Any:
    from haloflow.m05.intake_statements import SA_STATEMENTS

    return SA_STATEMENTS


def sa_catalog() -> Any:
    """Production module statements + the S-A statements (test catalogue only)."""

    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import build_statement_catalog

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, sa_statements())


class SAInstall:
    """One synthetic tenant, installed through the S-A registry on first `require()`
    (in the row body, never fixture setup: class-B red rule), then the S-A test
    profile applied once. The outcome is memoised per module."""

    def __init__(self, ids: m02_support.Identities, tenant: tuple[str, str], *,
                 profile: bool = True) -> None:
        self.ids = ids
        self.tenant = tenant
        self.profile = profile
        self._attempted = False
        self._error: Exception | None = None

    @property
    def schema(self) -> str:
        return self.tenant[1]

    @property
    def tenant_id(self) -> str:
        return self.tenant[0]

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(
                    self.ids, sa_registry(), self.tenant, supported=SA_SUPPORTED,
                    manifest=staff_manifest(),
                )
                if self.profile:
                    admin(self, SA_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"S-A test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


# --- SQL helpers --------------------------------------------------------------------


def admin(install: SAInstall, statement: Any, params: Any = None) -> list[tuple[Any, ...]]:
    """FIXTURE escape hatch (admin connection): seeding and observation only."""

    with psycopg.connect(install.ids.admin, autocommit=True) as conn:
        cursor = conn.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def q(install: SAInstall, text: str) -> sql.Composed:
    """`{s}` in text is replaced by the quoted tenant schema."""

    return sql.SQL(text).format(s=sql.Identifier(install.schema))


def attribution(principal: str, *, kind: str = "workload") -> dict[str, Any]:
    return {
        "principal_kind": kind,
        "principal_id": principal,
        "auth_method": "test",
        "execution_id": "00000000-0000-4000-8000-00000005ae01",
        "request_id": None,
        "purpose_code": "operations",
    }


def directory_cmd(op: str, principal: str = W_DIRECTORY, **fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, "op": op, **fields, "attribution": attribution(principal)}


def dispatch_cmd(reminder_id: str, appointment_ref: str,
                 principal: str = W_DISPATCH) -> dict[str, Any]:
    return {"schema_version": 1, "reminder_id": reminder_id, "appointment_ref": appointment_ref,
            "attribution": attribution(principal)}


def inbound_cmd(message_id: str, phone: str, reply_class: str, *, channel: str = "sms",
                principal: str = W_INGRESS) -> dict[str, Any]:
    return {"schema_version": 1, "channel": channel, "provider_message_id": message_id,
            "phone": phone, "reply_class": reply_class, "attribution": attribution(principal)}


def close_cmd(reminder_id: str, principal: str = W_TIMER) -> dict[str, Any]:
    return {"schema_version": 1, "reminder_id": reminder_id,
            "attribution": attribution(principal)}


def runtime_conn(install: SAInstall) -> psycopg.Connection[Any]:
    conn = psycopg.connect(install.ids.logins[RUNTIME], autocommit=True)
    conn.execute(f"SET ROLE {RUNTIME}")
    return conn


def call_in(conn: psycopg.Connection[Any], install: SAInstall, function: str,
            command: Any) -> dict[str, Any]:
    """Inside an OPEN transaction on `conn` (the held-lock seam)."""

    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant_id,))
    row = conn.execute(
        sql.SQL("SELECT {}.{}(%s)").format(sql.Identifier(install.schema),
                                          sql.Identifier(function)),
        (Jsonb(command),),
    ).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def call(install: SAInstall, function: str, command: Any) -> dict[str, Any]:
    with runtime_conn(install) as conn, conn.transaction():
        return call_in(conn, install, function, command)


def directory(install: SAInstall, op: str, **fields: Any) -> dict[str, Any]:
    return call(install, "m05_directory_put", directory_cmd(op, **fields))


def dispatch(install: SAInstall, reminder_id: str, appointment_ref: str) -> dict[str, Any]:
    return call(install, "m05_register_dispatch", dispatch_cmd(reminder_id, appointment_ref))


def inbound(install: SAInstall, message_id: str, phone: str, reply_class: str) -> dict[str, Any]:
    return call(install, "m05_register_inbound", inbound_cmd(message_id, phone, reply_class))


def close(install: SAInstall, reminder_id: str) -> dict[str, Any]:
    return call(install, "m05_close_window", close_cmd(reminder_id))


def snapshot(install: SAInstall) -> dict[str, Any]:
    """Full contents of the S-A tables and M05 items/history, plus M02 counts."""

    out: dict[str, Any] = {}
    for table in (*TABLES, "m05_followup_items", "m05_followup_history"):
        out[table] = sorted(
            row[0] for row in admin(install, q(install, "SELECT to_jsonb(t)::text FROM {s}."
                                                         + table + " AS t"))
        )
    for table in ("patient_events", "event_handoff_outbox"):
        out[table] = admin(install, q(install, "SELECT count(*) FROM {s}." + table))[0][0]
    return out


def refused(install: SAInstall, function: str, command: Any, code: str) -> None:
    before = snapshot(install)
    try:
        call(install, function, command)
    except psycopg.Error as error:
        assert (error.sqlstate, error.diag.message_primary) == ("P0001", code), (
            error.sqlstate, error.diag.message_primary)
    else:
        raise AssertionError(f"expected P0001 {code}")
    assert snapshot(install) == before, "a refused call wrote something"


def new_id() -> str:
    return str(uuid.uuid4())


def window(install: SAInstall, reminder_id: str) -> dict[str, Any]:
    rows = admin(install, q(install, "SELECT to_jsonb(w) FROM {s}.m05_reminder_windows AS w"
                                     " WHERE reminder_id = %s"), (reminder_id,))
    assert len(rows) == 1, rows
    value: dict[str, Any] = rows[0][0]
    return value


def contact(install: SAInstall, phone: str) -> dict[str, Any] | None:
    rows = admin(install, q(install, "SELECT to_jsonb(c) FROM {s}.m05_contact_state AS c"
                                     " WHERE phone = %s"), (phone,))
    return None if not rows else rows[0][0]


def items_for(install: SAInstall, patient_id: str) -> list[dict[str, Any]]:
    rows = admin(install, q(install, "SELECT to_jsonb(i) FROM {s}.m05_followup_items AS i"
                                     " WHERE patient_id = %s ORDER BY created_at, item_id"),
                 (patient_id,))
    return [r[0] for r in rows]


def event(install: SAInstall, event_id: str) -> dict[str, Any]:
    rows = admin(install, q(install, "SELECT to_jsonb(e) FROM {s}.patient_events AS e"
                                     " WHERE event_id = %s"), (event_id,))
    assert len(rows) == 1, rows
    value: dict[str, Any] = rows[0][0]
    return value


def shift_window(install: SAInstall, reminder_id: str, hours: float) -> None:
    """FIXTURE: move a window's sent_at and deadline by `hours` (negative = into the
    past) so timer rows never sleep. Keeps deadline = sent_at + 24 h."""

    admin(install, q(install, "UPDATE {s}.m05_reminder_windows"
                              " SET sent_at = sent_at + make_interval(secs => %s),"
                              " deadline = deadline + make_interval(secs => %s)"
                              " WHERE reminder_id = %s"),
          (hours * 3600, hours * 3600, reminder_id))


FUTURE: Final = "2099-01-0{n}T09:00:00Z"
PAST: Final = "2000-01-01T09:00:00Z"


def seed_patient(install: SAInstall, patient: str, phone: str, *,
                 appointments: int = 1) -> list[str]:
    """Directory through the real directory gateway: contact + booked future
    appointments (one per day from 2099-01-01)."""

    directory(install, "contact_put", phone=phone, patient_id=patient)
    refs = []
    for i in range(appointments):
        ref = new_id()
        directory(install, "appointment_put", appointment_ref=ref, patient_id=patient,
                  start_at=FUTURE.format(n=1 + i), status="booked")
        refs.append(ref)
    return refs


def fresh_phone() -> str:
    return "+1555" + str(uuid.uuid4().int)[:7]


def fresh_patient(install: SAInstall, *, appointments: int = 1) -> tuple[str, str, list[str]]:
    """A new patient on a new unique phone (rows stay independent within a module)."""

    patient = new_id()
    phone = fresh_phone()
    return patient, phone, seed_patient(install, patient, phone, appointments=appointments)


# --- held-lock seam (SA-TIME-1 section 6; no sleeps for correctness) ------------------


def wait_until_blocked(install: SAInstall, application_name: str, *,
                       timeout_s: float = 20.0) -> None:
    """Poll until the backend with `application_name` waits on a heavyweight lock.
    Polling observes a state; the assertions never depend on timing."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = admin(install, "SELECT wait_event_type FROM pg_stat_activity"
                              " WHERE application_name = %s", (application_name,))
        if rows and rows[0][0] == "Lock":
            return
        time.sleep(0.02)
    raise AssertionError(f"{application_name} never blocked on a lock")


@contextmanager
def held(install: SAInstall, function: str, command: Any) -> Iterator[dict[str, Any]]:
    """Run `function` in an open runtime transaction; yield its result while the
    transaction (and every lock it took) is still held; commit on exit."""

    with runtime_conn(install) as conn, conn.transaction():
        yield call_in(conn, install, function, command)


class Waiter:
    """A second runtime connection running `function` in a thread; it must block."""

    def __init__(self, install: SAInstall, function: str, command: Any) -> None:
        self.install = install
        self.name = f"sa-waiter-{uuid.uuid4().hex[:12]}"
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                with psycopg.connect(install.ids.logins[RUNTIME], autocommit=True,
                                     application_name=self.name) as conn:
                    conn.execute(f"SET ROLE {RUNTIME}")
                    with conn.transaction():
                        self.result = call_in(conn, install, function, command)
            except BaseException as error:  # noqa: BLE001 - re-raised in join()
                self.error = error

        self.thread = threading.Thread(target=run, daemon=True)

    def start_and_block(self) -> Waiter:
        self.thread.start()
        wait_until_blocked(self.install, self.name)
        return self

    def join(self) -> dict[str, Any]:
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "waiter did not finish"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str, *,
                   profile: bool = True) -> Iterator[SAInstall]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield SAInstall(ids, tenant, profile=profile)
    finally:
        reset(tenant)
