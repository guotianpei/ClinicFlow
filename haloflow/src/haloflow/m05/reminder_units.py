"""L-1S package S-H, checkpoint H2: synthetic reminder scheduling and the one-shot send
ticket, units t057..t074.

Governing: H2 package v1 + v1r1 (D1-D6), Codex ALIGN for red tests (seven pins), interface
pins v1 / v1r1 (D7-D10) and the round-1 corrections (D10r1 name rule, D11 consume re-checks
the H1 D1 predicate), owner decisions Q-SH2 and P-1. Synthetic only; nothing here sends a
message. NOT composed into production: these units install only through the H2 TEST
registry (tests/m05/sh2_support.py, target 74).

- t057..t061  migrator-owned tables: settings (singleton, seeded), appointment profile,
  schedule queue, frozen render inputs, one-shot send tickets. The runtime holds nothing.
- t062  ``m05_reminder_decision`` (owner only, STABLE): the pure due / window / 26 h floor
  verdict for an explicit `now` (D7). Every caller passes a ``clock_timestamp()`` read
  after its locks.
- t063  ``m05_directory_v2``: t038's directory writes, then the profile and the schedule.
- t064  ``m05_reminder_settings_put`` (EXECUTE to haloflow_support only, D8): authority
  through t074, re-checked after the settings lock; re-keys the clinic's schedule rows.
- t065  ``m05_outbound_prepare_rendered`` (owner only): H1 prepare + frozen render inputs.
- t066/t067  claim v2 (H1 claim + inputs + confirmation + decision + ticket) and read v2.
- t068/t069  ready (selection) and render_snapshot (the runner's render input).
- t070  ``m05_outbound_examine``: one ref, one transaction (lock order, D2, skips,
  prepare_rendered + claim v2, schedule bookkeeping in the same transaction).
- t071  ``m05_outbound_consume``: spends the ticket; read v2 + D11 + the pre-call gate.
- t072  remove-only ``acl_closure_v1``: runtime EXECUTE leaves the UNCHANGED t038, t048,
  t049 and t055 (owner-internal use stays).
- t073  migrator data unit: bootstrap the schedule (no op, or a prepared op).
- t074  ``m05_reminder_settings_authority`` (staff owner; EXECUTE to the M05 owner only,
  the t022 precedent): the HaloVox support login and an active grant with capability
  ``m05.reminder.settings`` on this schema's tenant. The M05 owner has no read access to
  the shared support tables; the staff owner already has exactly those reads (alembic 009).

Global lock order (pin 1): settings row -> contact rows (sorted phone) -> appointment ->
profile -> schedule row -> op row -> ticket. settings_put never takes contact or
appointment locks; directory v2 never takes the settings lock.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import AclAmendment, InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import (
    ACL_CLOSURE_KIND,
    TYPED_FUNCTION_KIND,
    UnitDefinition,
    UnitDefinitions,
)
from haloflow.m01.staff import units as staff_units
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05.intake_units import (
    _ATTRIBUTION,
    _CONFIG,
    _DECLARE_COMMON,
    _DIRECTORY_BODY,
    _TENANT,
    _VOLATILITY_CODE,
    _VOLATILITY_WORD,
    _contact_lock,
    _string_field,
    _ts,
    _workload,
)
from haloflow.m05.outbound_units import _D1, _OP_RESULT
from haloflow.m05.reminder_text import (
    CLINIC_NAME_MAX,
    FIRST_NAME_MAX,
    sql_forbidden_class,
    sql_whitespace_class,
)

_S: Final = "{schema}"
SUPPORT_ROLE: Final = "haloflow_support"
SETTINGS_CAPABILITY: Final = "m05.reminder.settings"
TEMPLATE_ID: Final = "qb4-v1"
POLICY_VERSION: Final = 1
CLOSURE_ID: Final = "t072_m05_h2_runtime_closure"
CLOSED_UNITS: Final = ("t038_m05_directory_put", "t048_m05_outbound_prepare",
                       "t049_m05_outbound_claim", "t055_m05_outbound_read")

# --- t057..t061 tables ---------------------------------------------------------------------

T057_SQL: Final = r"""
-- t057:step:table
CREATE TABLE {schema}.m05_reminder_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    lead_days smallint NOT NULL CHECK (lead_days IN (2, 3)),
    send_time time(0) NOT NULL,
    window_start time(0) NOT NULL,
    window_end time(0) NOT NULL,
    clinic_tz text CHECK (clinic_tz IS NULL OR char_length(clinic_tz) BETWEEN 1 AND 64),
    clinic_display_name text CHECK (clinic_display_name IS NULL
        OR char_length(clinic_display_name) BETWEEN 1 AND 64),
    settings_version bigint NOT NULL CHECK (settings_version >= 1),
    updated_at timestamptz NOT NULL,
    CONSTRAINT m05_reminder_settings_window CHECK (
        window_start >= time '09:00' AND window_end <= time '19:00'
        AND window_start < window_end
        AND send_time >= window_start AND send_time < window_end)
);
INSERT INTO {schema}.m05_reminder_settings (singleton, lead_days, send_time, window_start,
    window_end, clinic_tz, clinic_display_name, settings_version, updated_at)
VALUES (true, 2, time '10:00', time '09:00', time '19:00', NULL, NULL, 1,
    clock_timestamp());

-- t057:step:runtime_privileges
REVOKE ALL ON {schema}.m05_reminder_settings FROM haloflow_runtime;

-- t057:step:grants
GRANT SELECT, UPDATE (lead_days, send_time, window_start, window_end, clinic_tz,
        clinic_display_name, settings_version, updated_at)
    ON {schema}.m05_reminder_settings TO haloflow_m05_owner;
"""

T058_SQL: Final = r"""
-- t058:step:table
CREATE TABLE {schema}.m05_appointment_profile (
    appointment_ref uuid PRIMARY KEY,
    first_name text CHECK (first_name IS NULL OR char_length(first_name) <= 64),
    confirmation varchar(16) CHECK (confirmation IS NULL
        OR confirmation IN ('confirmed', 'not_confirmed', 'unknown')),
    department_tz text CHECK (department_tz IS NULL OR char_length(department_tz) <= 64),
    source_status text CHECK (source_status IS NULL OR char_length(source_status) <= 64),
    source_version bigint NOT NULL CHECK (source_version >= 1),
    updated_at timestamptz NOT NULL
);

-- t058:step:runtime_privileges
REVOKE ALL ON {schema}.m05_appointment_profile FROM haloflow_runtime;

-- t058:step:grants
GRANT SELECT, INSERT, UPDATE ON {schema}.m05_appointment_profile TO haloflow_m05_owner;
"""

T059_SQL: Final = r"""
-- t059:step:table
CREATE TABLE {schema}.m05_outbound_schedule (
    appointment_ref uuid PRIMARY KEY,
    next_check_at timestamptz NOT NULL,
    last_reason varchar(32),
    examined_count integer NOT NULL DEFAULT 0 CHECK (examined_count >= 0),
    updated_at timestamptz NOT NULL
);
CREATE INDEX m05_outbound_schedule_ready
    ON {schema}.m05_outbound_schedule (next_check_at, appointment_ref);

-- t059:step:runtime_privileges
REVOKE ALL ON {schema}.m05_outbound_schedule FROM haloflow_runtime;

-- t059:step:grants
GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.m05_outbound_schedule TO haloflow_m05_owner;
"""

T060_SQL: Final = r"""
-- t060:step:table
CREATE TABLE {schema}.m05_outbound_render_inputs (
    op_id uuid PRIMARY KEY REFERENCES {schema}.m05_outbound_ops (op_id),
    first_name text NOT NULL,
    clinic_display_name text NOT NULL,
    zone text NOT NULL,
    zone_source varchar(16) NOT NULL CHECK (zone_source IN ('department', 'clinic_fallback')),
    template_id varchar(16) NOT NULL,
    policy_version integer NOT NULL,
    source_version bigint NOT NULL,
    settings_version bigint NOT NULL,
    created_at timestamptz NOT NULL
);

-- t060:step:runtime_privileges
REVOKE ALL ON {schema}.m05_outbound_render_inputs FROM haloflow_runtime;

-- t060:step:grants
GRANT SELECT, INSERT ON {schema}.m05_outbound_render_inputs TO haloflow_m05_owner;
"""

T061_SQL: Final = r"""
-- t061:step:table
CREATE TABLE {schema}.m05_outbound_send_tickets (
    op_id uuid PRIMARY KEY REFERENCES {schema}.m05_outbound_ops (op_id),
    attempt_id uuid NOT NULL,
    issued_at timestamptz NOT NULL,
    consumed_at timestamptz
);

-- t061:step:runtime_privileges
REVOKE ALL ON {schema}.m05_outbound_send_tickets FROM haloflow_runtime;

-- t061:step:grants
GRANT SELECT, INSERT, UPDATE (consumed_at) ON {schema}.m05_outbound_send_tickets
    TO haloflow_m05_owner;
"""

# t073: bootstrap. Every booked future appointment with no op, or a prepared op, is
# enqueued now; examine re-keys it (R-H2-12 as amended by D4).
T073_SQL: Final = r"""
-- t073:step:bootstrap
INSERT INTO {schema}.m05_outbound_schedule (appointment_ref, next_check_at, last_reason,
    examined_count, updated_at)
SELECT a.appointment_ref, clock_timestamp(), NULL, 0, clock_timestamp()
  FROM {schema}.m05_appointments AS a
 WHERE a.status = 'booked' AND a.start_at > clock_timestamp()
   AND NOT EXISTS (SELECT 1 FROM {schema}.m05_outbound_ops AS o
                    WHERE o.appointment_ref = a.appointment_ref AND o.state <> 'prepared')
ON CONFLICT (appointment_ref) DO NOTHING;
"""

# --- shared fragments ---------------------------------------------------------------------

_C_NAME: Final = (
    f"    c_forbidden constant text := '{sql_forbidden_class()}';\n"
    f"    c_ws constant text := '{sql_whitespace_class()}';\n"
)


def _name_ok(expr: str, maximum: int) -> str:
    """The D10r1 rule as a NULL-safe boolean expression."""

    return (f"COALESCE(({expr}) IS NFC NORMALIZED"
            f" AND pg_catalog.char_length({expr}) BETWEEN 1 AND {maximum}"
            f" AND ({expr}) OPERATOR(pg_catalog.!~) c_forbidden"
            f" AND ({expr}) OPERATOR(pg_catalog.!~) ('^' || c_ws)"
            f" AND ({expr}) OPERATOR(pg_catalog.!~) (c_ws || '$'), false)")


def _zone_ok(expr: str) -> str:
    return (f"(({expr}) IS NOT NULL AND EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names"
            f" AS z WHERE z.name = ({expr})))")


_HM: Final = "'HH24:MI'"


def _decision_call(now: str, start: str, zone: str, into: str) -> str:
    """Call t062 with the CURRENT settings record v_s (read after the caller's locks)."""

    return f"""
    {into} := {_S}.m05_reminder_decision(pg_catalog.jsonb_build_object(
        'schema_version', 1, 'now', {_ts(now)}, 'start_at', {_ts(start)}, 'zone', {zone},
        'lead_days', v_s.lead_days::integer,
        'send_time', pg_catalog.to_char(v_s.send_time, {_HM}),
        'window_start', pg_catalog.to_char(v_s.window_start, {_HM}),
        'window_end', pg_catalog.to_char(v_s.window_end, {_HM})));
"""


def _rekey(filter_sql: str, *, create: bool) -> str:
    """Set next_check_at := greatest(due, clock_timestamp()) for the matching schedulable
    appointments (booked, future, no op or a prepared op). With `create`, rows are inserted
    and non-schedulable matches are deleted (directory v2); without it, only existing rows
    are re-keyed (settings_put). An unresolvable zone keys to clock_timestamp()."""

    target = f"""
      WITH s AS (SELECT r.lead_days, r.send_time, r.clinic_tz
                   FROM {_S}.m05_reminder_settings AS r),
           tz AS MATERIALIZED (SELECT z.name FROM pg_catalog.pg_timezone_names AS z),
           t AS (
        SELECT a.appointment_ref, a.start_at,
               COALESCE((SELECT tz.name FROM tz WHERE tz.name = p.department_tz),
                        (SELECT tz.name FROM tz, s WHERE tz.name = s.clinic_tz)) AS zone
          FROM {_S}.m05_appointments AS a
          LEFT JOIN {_S}.m05_appointment_profile AS p
                 ON p.appointment_ref = a.appointment_ref
         WHERE ({filter_sql})
           AND a.status = 'booked'
           AND a.start_at OPERATOR(pg_catalog.>) pg_catalog.clock_timestamp()
           AND NOT EXISTS (SELECT 1 FROM {_S}.m05_outbound_ops AS o
                            WHERE o.appointment_ref = a.appointment_ref
                              AND o.state <> 'prepared'))"""
    key = ("CASE WHEN t.zone IS NULL THEN pg_catalog.clock_timestamp() ELSE"
           " GREATEST(pg_catalog.timezone(t.zone, ((pg_catalog.timezone(t.zone, t.start_at))"
           "::date OPERATOR(pg_catalog.-) s.lead_days::integer)"
           " OPERATOR(pg_catalog.+) s.send_time), pg_catalog.clock_timestamp()) END")
    if not create:
        return f"""
    {target}
    UPDATE {_S}.m05_outbound_schedule AS q
       SET next_check_at = {key}, updated_at = pg_catalog.clock_timestamp()
      FROM t, s WHERE q.appointment_ref = t.appointment_ref;
"""
    return f"""
    {target}
    INSERT INTO {_S}.m05_outbound_schedule (appointment_ref, next_check_at, last_reason,
        examined_count, updated_at)
    SELECT t.appointment_ref, {key}, NULL, 0, pg_catalog.clock_timestamp() FROM t, s
        ORDER BY t.appointment_ref
    ON CONFLICT (appointment_ref) DO UPDATE
       SET next_check_at = EXCLUDED.next_check_at, updated_at = EXCLUDED.updated_at;
    {target}
    DELETE FROM {_S}.m05_outbound_schedule AS q
     USING {_S}.m05_appointments AS a
     WHERE q.appointment_ref = a.appointment_ref AND ({filter_sql})
       AND NOT EXISTS (SELECT 1 FROM t WHERE t.appointment_ref = q.appointment_ref);
"""


_INPUTS_JSON: Final = (
    "pg_catalog.jsonb_build_object('first_name', v_i.first_name,"
    " 'clinic_display_name', v_i.clinic_display_name, 'zone', v_i.zone,"
    " 'zone_source', v_i.zone_source, 'template_id', v_i.template_id,"
    " 'policy_version', v_i.policy_version, 'source_version', v_i.source_version,"
    " 'settings_version', v_i.settings_version)"
)


def _refusal(code: str) -> str:
    return (f"RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'refused',"
            f" 'code', {code});")


def _not_ready(code: str) -> str:
    return (f"RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'not_ready',"
            f" 'code', '{code}');")


# --- t062 decision (owner only, STABLE) -----------------------------------------------------

_DECISION_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    c_ts constant text := '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$';
    c_hm constant text := '^([01][0-9]|2[0-3]):[0-5][0-9]$';
    v_now timestamptz;
    v_start timestamptz;
    v_zone text;
    v_lead integer;
    v_send time;
    v_ws time;
    v_we time;
    v_due timestamptz;
    v_local timestamp;
    v_verdict text;
    v_next timestamptz;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['lead_days', 'now', 'schema_version', 'send_time',
                                     'start_at', 'window_end', 'window_start', 'zone']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
""" + _string_field("now", "c_ts") + _string_field("start_at", "c_ts")
    + _string_field("zone", r"'^[A-Za-z0-9_+./-]{1,64}$'")
    + _string_field("send_time", "c_hm") + _string_field("window_start", "c_hm")
    + _string_field("window_end", "c_hm") + r"""       OR pg_catalog.jsonb_typeof(
              p_command OPERATOR(pg_catalog.->) 'lead_days') IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'lead_days') OPERATOR(pg_catalog.!~) '^[0-9]{1,2}$'
    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_zone := p_command OPERATOR(pg_catalog.->>) 'zone';
    v_lead := (p_command OPERATOR(pg_catalog.->>) 'lead_days')::integer;
    v_send := (p_command OPERATOR(pg_catalog.->>) 'send_time')::time;
    v_ws := (p_command OPERATOR(pg_catalog.->>) 'window_start')::time;
    v_we := (p_command OPERATOR(pg_catalog.->>) 'window_end')::time;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names AS z WHERE z.name = v_zone)
       OR v_lead NOT BETWEEN 1 AND 14 OR v_ws >= v_we OR v_send < v_ws OR v_send >= v_we THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_now := (p_command OPERATOR(pg_catalog.->>) 'now')::timestamptz;
    v_start := (p_command OPERATOR(pg_catalog.->>) 'start_at')::timestamptz;
    v_due := pg_catalog.timezone(v_zone, ((pg_catalog.timezone(v_zone, v_start))::date
             OPERATOR(pg_catalog.-) v_lead) OPERATOR(pg_catalog.+) v_send);
    v_local := pg_catalog.timezone(v_zone, v_now);
    IF (v_start OPERATOR(pg_catalog.-) v_now) OPERATOR(pg_catalog.<) interval '26 hours' THEN
        v_verdict := 'too_late';
        v_next := NULL;
    ELSIF v_now OPERATOR(pg_catalog.<) v_due THEN
        v_verdict := 'not_due';
        v_next := v_due;
    ELSIF v_local::time >= v_ws AND v_local::time < v_we THEN
        v_verdict := 'send';
        v_next := NULL;
    ELSIF v_local::time < v_ws THEN
        v_verdict := 'outside_window';
        v_next := pg_catalog.timezone(v_zone, v_local::date OPERATOR(pg_catalog.+) v_ws);
    ELSE
        v_verdict := 'outside_window';
        v_next := pg_catalog.timezone(v_zone, (v_local::date OPERATOR(pg_catalog.+) 1)
                  OPERATOR(pg_catalog.+) v_ws);
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'verdict', v_verdict,
        'due_at', """ + _ts("v_due") + r""",
        'next_eligible_at', CASE WHEN v_next IS NULL THEN NULL ELSE """ + _ts("v_next")
    + r""" END);
END;
"""
)

# --- t063 directory v2 ----------------------------------------------------------------------

_DIR_KEYS_OLD: Final = (
    "        IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'op',"
    " 'patient_id',\n"
    "                                         'schema_version', 'start_at', 'status']::text[]\n"
)
_DIR_KEYS_NEW: Final = (
    "        IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'confirmation',\n"
    "                                         'department_tz', 'first_name', 'op', 'patient_id',\n"
    "                                         'schema_version', 'source_status', 'start_at',\n"
    "                                         'status']::text[]\n"
    + "".join(
        f"           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) '{f}')"
        " NOT IN ('string', 'null')\n"
        f"           OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) '{f}') > 64\n"
        for f in ("first_name", "confirmation", "department_tz", "source_status"))
    + "           OR (p_command OPERATOR(pg_catalog.->>) 'confirmation')\n"
    "              NOT IN ('confirmed', 'not_confirmed', 'unknown')\n"
)
_DIR_RETURN: Final = (
    "    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied');\n"
)
_DIR_TAIL: Final = r"""
    -- H2: the profile (version bumped in the same statement) and the schedule, in the
    -- directory transaction, after the t038 writes (lock order: contact -> appointment ->
    -- profile -> schedule; never settings).
    IF v_op = 'appointment_put' THEN
        INSERT INTO {schema}.m05_appointment_profile AS p (appointment_ref, first_name,
            confirmation, department_tz, source_status, source_version, updated_at)
        VALUES (v_ref, p_command OPERATOR(pg_catalog.->>) 'first_name',
            p_command OPERATOR(pg_catalog.->>) 'confirmation',
            p_command OPERATOR(pg_catalog.->>) 'department_tz',
            p_command OPERATOR(pg_catalog.->>) 'source_status', 1,
            pg_catalog.clock_timestamp())
        ON CONFLICT (appointment_ref) DO UPDATE
           SET first_name = EXCLUDED.first_name, confirmation = EXCLUDED.confirmation,
               department_tz = EXCLUDED.department_tz,
               source_status = EXCLUDED.source_status,
               source_version = p.source_version OPERATOR(pg_catalog.+) 1,
               updated_at = EXCLUDED.updated_at;
""" + _rekey("a.appointment_ref = v_ref", create=True) + r"""
    ELSE
""" + _rekey("a.patient_id = v_patient", create=True) + r"""
    END IF;
"""
if _DIRECTORY_BODY.count(_DIR_KEYS_OLD) != 1 or _DIRECTORY_BODY.count(_DIR_RETURN) != 1:
    # pragma: no cover - import guard
    raise RuntimeError("t038 directory body anchors not found exactly once")
_DIRECTORY_V2_BODY: Final = (
    _DIRECTORY_BODY.replace(_DIR_KEYS_OLD, _DIR_KEYS_NEW)
    .replace(_DIR_RETURN, _DIR_TAIL + _DIR_RETURN)
)

# --- t074 settings authority (staff owner; EXECUTE to the M05 owner) ------------------------

_AUTHORITY_BODY: Final = r"""
DECLARE
    v_setting text;
    v_actor text;
    v_tenant text;
    v_grant uuid;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    SELECT l.actor_id INTO v_actor
      FROM shared.support_operator_logins AS l
     WHERE l.login_name = session_user
       AND l.login_oid = (SELECT r.oid FROM pg_catalog.pg_roles AS r
                           WHERE r.rolname = session_user);
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'SUPPORT_NOT_AUTHORIZED');
    END IF;
    SELECT t.tenant_id INTO v_tenant FROM shared.tenants AS t WHERE t.schema_key = '{schema}';
    IF NOT FOUND OR v_tenant IS DISTINCT FROM v_setting THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    SELECT g.grant_id INTO v_grant
      FROM shared.support_access_grants AS g
     WHERE g.actor_id = v_actor AND g.tenant_id = v_tenant AND g.state = 'active'
       AND g.starts_at <= pg_catalog.clock_timestamp()
       AND pg_catalog.clock_timestamp() < g.expires_at
       AND 'm05.reminder.settings' = ANY (g.capabilities)
     ORDER BY g.expires_at DESC, g.grant_id
     LIMIT 1;
    IF v_grant IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'SUPPORT_NOT_AUTHORIZED');
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'authorized',
        'actor_id', v_actor, 'grant_id', v_grant::text);
END;
"""

# --- t064 settings_put (support only, D8/D9) ------------------------------------------------

_SETTINGS_PUT_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + _C_NAME + r"""
    c_hm constant text := '^([01][0-9]|2[0-3]):[0-5][0-9]$';
    v_auth jsonb;
    v_lead integer;
    v_send time;
    v_ws time;
    v_we time;
    v_tz text;
    v_name text;
    v_version bigint;
    v_n integer;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['clinic_display_name', 'clinic_tz', 'lead_days',
                                     'schema_version', 'send_time', 'window_end',
                                     'window_start']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'lead_days')
          IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'send_time')
          IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'window_start')
          IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'window_end')
          IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'clinic_tz')
          NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'clinic_display_name')
          NOT IN ('string', 'null') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    -- D8: support authority (t074), before any lock or write ...
    v_auth := {schema}.m05_reminder_settings_authority();
    IF (v_auth OPERATOR(pg_catalog.->>) 'outcome') IS DISTINCT FROM 'authorized' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SUPPORT_NOT_AUTHORIZED';
    END IF;
    PERFORM 1 FROM {schema}.m05_reminder_settings AS s FOR UPDATE;
    -- ... and re-checked with a fresh clock after the settings lock.
    v_auth := {schema}.m05_reminder_settings_authority();
    IF (v_auth OPERATOR(pg_catalog.->>) 'outcome') IS DISTINCT FROM 'authorized' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SUPPORT_NOT_AUTHORIZED';
    END IF;
    IF (p_command OPERATOR(pg_catalog.->>) 'lead_days') NOT IN ('2', '3')
       OR (p_command OPERATOR(pg_catalog.->>) 'send_time') OPERATOR(pg_catalog.!~) c_hm
       OR (p_command OPERATOR(pg_catalog.->>) 'window_start') OPERATOR(pg_catalog.!~) c_hm
       OR (p_command OPERATOR(pg_catalog.->>) 'window_end') OPERATOR(pg_catalog.!~) c_hm THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SETTINGS_INVALID';
    END IF;
    v_lead := (p_command OPERATOR(pg_catalog.->>) 'lead_days')::integer;
    v_send := (p_command OPERATOR(pg_catalog.->>) 'send_time')::time;
    v_ws := (p_command OPERATOR(pg_catalog.->>) 'window_start')::time;
    v_we := (p_command OPERATOR(pg_catalog.->>) 'window_end')::time;
    v_tz := p_command OPERATOR(pg_catalog.->>) 'clinic_tz';
    v_name := p_command OPERATOR(pg_catalog.->>) 'clinic_display_name';
    IF v_ws < time '09:00' OR v_we > time '19:00' OR v_ws >= v_we
       OR v_send < v_ws OR v_send >= v_we
       OR (v_tz IS NOT NULL AND NOT """ + _zone_ok("v_tz") + r""")
       OR NOT """ + _name_ok("v_name", CLINIC_NAME_MAX) + r""" THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SETTINGS_INVALID';
    END IF;
    UPDATE {schema}.m05_reminder_settings AS s
       SET lead_days = v_lead, send_time = v_send, window_start = v_ws, window_end = v_we,
           clinic_tz = v_tz, clinic_display_name = v_name,
           settings_version = s.settings_version OPERATOR(pg_catalog.+) 1,
           updated_at = pg_catalog.clock_timestamp()
    RETURNING s.settings_version INTO v_version;
    -- D9: re-key only; no op, ticket or send is created here.
""" + _rekey("true", create=False) + r"""
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied',
        'settings_version', v_version, 'rekeyed', v_n);
END;
"""
)

# --- resolution shared by snapshot / prepare_rendered / examine -----------------------------

# Requires v_s (settings), v_p (profile, may be NULL-valued), v_appt; sets v_zone, v_source.
_RESOLVE_ZONE: Final = r"""
    v_zone := NULL;
    v_source := NULL;
    IF """ + _zone_ok("v_p.department_tz") + r""" THEN
        v_zone := v_p.department_tz;
        v_source := 'department';
    ELSIF """ + _zone_ok("v_s.clinic_tz") + r""" THEN
        v_zone := v_s.clinic_tz;
        v_source := 'clinic_fallback';
    END IF;
"""

_DECLARE_H2: Final = _DECLARE_COMMON + _C_NAME + r"""
    v_ref uuid;
    v_op uuid;
    v_s record;
    v_p record;
    v_appt record;
    v_o record;
    v_i record;
    v_q record;
    v_d jsonb;
    v_r jsonb;
    v_zone text;
    v_source text;
    v_phones text[];
    v_phone text;
    v_opted boolean;
    v_code text;
    v_now timestamptz;
"""

# --- t069 render snapshot (read only) -------------------------------------------------------

_SNAPSHOT_BODY: Final = (
    "\nDECLARE" + _DECLARE_H2 + r"""
    v_sset bigint;
    v_sname text;
    v_stz text;
    v_sstart timestamptz;
    v_pfirst text;
    v_ptz text;
    v_psv bigint;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution',
                                     'schema_version']::text[]
""" + _string_field("appointment_ref", "c_uuid") + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
    -- Codex 26edf09 I2: ONE statement (one READ COMMITTED snapshot) reads settings,
    -- appointment and profile, so a concurrent directory commit is seen wholly or not at
    -- all (start_at and source_version always belong together).
    SELECT s.settings_version, s.clinic_display_name, s.clinic_tz, a.start_at,
           p.first_name, p.department_tz, p.source_version
      INTO v_sset, v_sname, v_stz, v_sstart, v_pfirst, v_ptz, v_psv
      FROM {schema}.m05_reminder_settings AS s
      LEFT JOIN {schema}.m05_appointments AS a ON a.appointment_ref = v_ref
      LEFT JOIN {schema}.m05_appointment_profile AS p ON p.appointment_ref = v_ref;
    v_zone := NULL;
    v_source := NULL;
    IF """ + _zone_ok("v_ptz") + r""" THEN
        v_zone := v_ptz;
        v_source := 'department';
    ELSIF """ + _zone_ok("v_stz") + r""" THEN
        v_zone := v_stz;
        v_source := 'clinic_fallback';
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1,
        'appointment_ref', v_ref::text, 'source_version', v_psv,
        'settings_version', v_sset, 'first_name', v_pfirst,
        'clinic_display_name', v_sname, 'zone', v_zone, 'zone_source', v_source,
        'start_at', CASE WHEN v_sstart IS NULL THEN NULL ELSE """
    + _ts("v_sstart") + r""" END);
END;
"""
)

# --- t068 ready (read only) -----------------------------------------------------------------

_READY_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_limit integer;
    v_refs jsonb;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'limit', 'schema_version']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'limit')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'limit') OPERATOR(pg_catalog.!~) '^[0-9]{1,4}$' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_limit := (p_command OPERATOR(pg_catalog.->>) 'limit')::integer;
    IF v_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(r.appointment_ref::text
                    ORDER BY r.next_check_at, r.appointment_ref), '[]'::jsonb)
      INTO v_refs
      FROM (SELECT q.appointment_ref, q.next_check_at FROM {schema}.m05_outbound_schedule AS q
             WHERE q.next_check_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp()
             ORDER BY q.next_check_at, q.appointment_ref LIMIT v_limit) AS r;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'refs', v_refs);
END;
"""
)

# --- t065 prepare_rendered (owner only) -----------------------------------------------------

_PREPARE_RENDERED_BODY: Final = (
    "\nDECLARE" + _DECLARE_H2 + r"""
    v_rem uuid;
    v_hash text;
    v_sv bigint;
    v_tv bigint;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'op_id', 'reminder_id',
                                     'schema_version', 'settings_version', 'source_version',
                                     'text_sha256']::text[]
""" + _string_field("op_id", "c_uuid") + _string_field("appointment_ref", "c_uuid")
    + _string_field("reminder_id", "c_uuid") + _string_field("text_sha256", "'^[0-9a-f]{64}$'")
    + r"""       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'source_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'source_version')
          OPERATOR(pg_catalog.!~) '^[0-9]{1,18}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'settings_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'settings_version')
          OPERATOR(pg_catalog.!~) '^[0-9]{1,18}$' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
    v_rem := (p_command OPERATOR(pg_catalog.->>) 'reminder_id')::uuid;
    v_hash := p_command OPERATOR(pg_catalog.->>) 'text_sha256';
    v_sv := (p_command OPERATOR(pg_catalog.->>) 'source_version')::bigint;
    v_tv := (p_command OPERATOR(pg_catalog.->>) 'settings_version')::bigint;
    -- Replay order (pins v1r1 item 3): op_id exists -> hash -> versions -> existing.
    SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.op_id = v_op;
    IF FOUND THEN
        SELECT * INTO v_i FROM {schema}.m05_outbound_render_inputs AS i WHERE i.op_id = v_op;
        IF pg_catalog.encode(v_o.text_sha256, 'hex') <> v_hash
           OR v_o.appointment_ref <> v_ref OR v_o.reminder_id <> v_rem THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'OP_CONFLICT';
        END IF;
        IF NOT FOUND OR v_i.source_version <> v_sv OR v_i.settings_version <> v_tv THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INPUTS_CONFLICT';
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'existing',
            'op_id', v_o.op_id::text, 'fence', v_o.fence, 'inputs', """ + _INPUTS_JSON + r""");
    END IF;
    SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.appointment_ref = v_ref;
    IF FOUND THEN  -- H1 D4: the original op (and its inputs, if any) wins
        SELECT * INTO v_i FROM {schema}.m05_outbound_render_inputs AS i
         WHERE i.op_id = v_o.op_id;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'existing',
            'op_id', v_o.op_id::text, 'fence', v_o.fence,
            'inputs', CASE WHEN v_i.op_id IS NULL THEN NULL ELSE """ + _INPUTS_JSON + r""" END);
    END IF;
    -- Lock order: settings -> (contact, inside H1 prepare) -> appointment/profile -> op.
    SELECT * INTO v_s FROM {schema}.m05_reminder_settings AS s FOR SHARE;
    SELECT p.* INTO v_p FROM {schema}.m05_appointment_profile AS p
     WHERE p.appointment_ref = v_ref FOR SHARE;
    IF NOT FOUND OR v_p.source_version <> v_sv OR v_s.settings_version <> v_tv THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STALE_SNAPSHOT';
    END IF;
    IF NOT """ + _name_ok("v_p.first_name", FIRST_NAME_MAX) + r""" THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NAME_INVALID';
    END IF;
    IF NOT """ + _name_ok("v_s.clinic_display_name", CLINIC_NAME_MAX) + r""" THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SETTINGS_INCOMPLETE';
    END IF;
""" + _RESOLVE_ZONE + r"""
    IF v_zone IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'TZ_UNRESOLVED';
    END IF;
    v_r := {schema}.m05_outbound_prepare(pg_catalog.jsonb_build_object('schema_version', 1,
        'op_id', v_op::text, 'appointment_ref', v_ref::text, 'reminder_id', v_rem::text,
        'text_sha256', v_hash, 'attribution', v_attr));
    IF (v_r OPERATOR(pg_catalog.->>) 'op_id') IS DISTINCT FROM v_op::text THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'OP_CONFLICT';  -- defensive
    END IF;
    INSERT INTO {schema}.m05_outbound_render_inputs (op_id, first_name, clinic_display_name,
        zone, zone_source, template_id, policy_version, source_version, settings_version,
        created_at)
    VALUES (v_op, v_p.first_name, v_s.clinic_display_name, v_zone, v_source, '""" + TEMPLATE_ID
    + "', " + str(POLICY_VERSION) + r""", v_sv, v_tv, pg_catalog.clock_timestamp())
    RETURNING * INTO v_i;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'prepared',
        'op_id', v_op::text, 'fence', (v_r OPERATOR(pg_catalog.->>) 'fence')::integer,
        'inputs', """ + _INPUTS_JSON + r""");
END;
"""
)

# --- t066 claim v2 --------------------------------------------------------------------------

# Requires v_op; takes settings FOR SHARE -> contact -> appointment/profile FOR SHARE -> op.
_LOCK_CHAIN: Final = r"""
    SELECT * INTO v_s FROM {schema}.m05_reminder_settings AS s FOR SHARE;
    SELECT o.phone::text INTO v_phone FROM {schema}.m05_outbound_ops AS o WHERE o.op_id = v_op;
""" + _contact_lock("v_phone", "v_opted") + r"""
    PERFORM 1 FROM {schema}.m05_appointments AS a
      JOIN {schema}.m05_outbound_ops AS o ON o.appointment_ref = a.appointment_ref
     WHERE o.op_id = v_op FOR SHARE OF a;
    SELECT p.* INTO v_p FROM {schema}.m05_appointment_profile AS p
      JOIN {schema}.m05_outbound_ops AS o ON o.appointment_ref = p.appointment_ref
     WHERE o.op_id = v_op FOR SHARE OF p;
    SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.op_id = v_op FOR UPDATE;
"""

_CLAIM_V2_BODY: Final = (
    "\nDECLARE" + _DECLARE_H2 + r"""
    v_fence integer;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'fence', 'lease_seconds', 'op_id',
                                     'schema_version']::text[]
""" + _string_field("op_id", "c_uuid") + r"""       OR pg_catalog.jsonb_typeof(
              p_command OPERATOR(pg_catalog.->) 'fence') IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'fence') OPERATOR(pg_catalog.!~) '^[0-9]{1,9}$'
    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_fence := (p_command OPERATOR(pg_catalog.->>) 'fence')::integer;
    SELECT * INTO v_i FROM {schema}.m05_outbound_render_inputs AS i WHERE i.op_id = v_op;
    IF NOT FOUND THEN  -- D5: no claim without frozen render inputs (zero writes)
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NO_RENDER_INPUTS';
    END IF;
""" + _LOCK_CHAIN + r"""
    IF v_o.state <> 'prepared' OR v_o.fence <> v_fence THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CLAIM_STALE';
    END IF;
    v_now := pg_catalog.clock_timestamp();
    -- X3: a confirmation recorded after prepare abandons the op (OD-RM1).
    IF v_p.confirmation = 'confirmed' THEN
        UPDATE {schema}.m05_outbound_ops AS o SET state = 'abandoned', code = 'CONFIRMED',
            updated_at = v_now WHERE o.op_id = v_op RETURNING * INTO v_o;
        RETURN """ + _OP_RESULT.format(outcome="'abandoned'", attempt="NULL") + r""";
    END IF;
    -- Codex 26edf09 I1: the CURRENT source status still makes the appointment eligible.
    IF v_p.source_status IS DISTINCT FROM 'booked' THEN
        UPDATE {schema}.m05_outbound_ops AS o
           SET state = 'abandoned',
               code = CASE WHEN v_p.source_status = 'completed' THEN 'COMPLETED'
                           ELSE 'STATUS_UNSUPPORTED' END,
               updated_at = v_now WHERE o.op_id = v_op RETURNING * INTO v_o;
        RETURN """ + _OP_RESULT.format(outcome="'abandoned'", attempt="NULL") + r""";
    END IF;
    -- R-H2-02 with a fresh clock read after the locks; frozen zone and start, current
    -- settings (a newly narrowed window applies to prepared work).
""" + _decision_call("pg_catalog.clock_timestamp()", "v_o.start_at", "v_i.zone", "v_d") + r"""
    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') = 'too_late' THEN
        UPDATE {schema}.m05_outbound_ops AS o SET state = 'abandoned', code = 'TOO_LATE_26H',
            updated_at = pg_catalog.clock_timestamp() WHERE o.op_id = v_op RETURNING * INTO v_o;
        RETURN """ + _OP_RESULT.format(outcome="'abandoned'", attempt="NULL") + r""";
    END IF;
    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') <> 'send' THEN  -- R-H2-07: defer, no attempt
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'deferred',
            'code', CASE WHEN (v_d OPERATOR(pg_catalog.->>) 'verdict') = 'not_due'
                         THEN 'NOT_DUE' ELSE 'OUTSIDE_WINDOW' END,
            'op_id', v_o.op_id::text, 'state', v_o.state, 'fence', v_o.fence,
            'next_eligible_at', v_d OPERATOR(pg_catalog.->>) 'next_eligible_at');
    END IF;
    v_r := {schema}.m05_outbound_claim(p_command);  -- H1: CAS + the D1 predicate
    IF (v_r OPERATOR(pg_catalog.->>) 'outcome') = 'claimed' THEN
        INSERT INTO {schema}.m05_outbound_send_tickets (op_id, attempt_id, issued_at,
            consumed_at)
        VALUES (v_op, (v_r OPERATOR(pg_catalog.->>) 'attempt_id')::uuid,
            pg_catalog.clock_timestamp(), NULL);
    END IF;
    RETURN v_r;
END;
"""
)

# --- t067 read v2 ---------------------------------------------------------------------------

_READ_V2_BODY: Final = (
    "\nDECLARE" + _DECLARE_H2 + r"""
    v_attempt uuid;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attempt_id', 'attribution', 'op_id',
                                     'schema_version']::text[]
""" + _string_field("op_id", "c_uuid") + _string_field("attempt_id", "c_uuid") + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_attempt := (p_command OPERATOR(pg_catalog.->>) 'attempt_id')::uuid;
    SELECT * INTO v_i FROM {schema}.m05_outbound_render_inputs AS i WHERE i.op_id = v_op;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NO_RENDER_INPUTS';
    END IF;
    SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.op_id = v_op FOR SHARE;
    v_now := pg_catalog.clock_timestamp();  -- X2: after the op row lock, never now()
    IF v_o.state <> 'attempting' OR v_o.attempt_id IS DISTINCT FROM v_attempt
       OR v_o.lease_until OPERATOR(pg_catalog.<=) v_now THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CLAIM_STALE';
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'op_id', v_o.op_id::text,
        'attempt_id', v_o.attempt_id::text, 'patient_id', v_o.patient_id::text,
        'phone', v_o.phone::text, 'start_at', """ + _ts("v_o.start_at") + r""",
        'reminder_id', v_o.reminder_id::text,
        'text_sha256', pg_catalog.encode(v_o.text_sha256, 'hex'),
        'lease_until', """ + _ts("v_o.lease_until") + r""",
        'first_name', v_i.first_name, 'clinic_display_name', v_i.clinic_display_name,
        'zone', v_i.zone, 'zone_source', v_i.zone_source,
        'db_now', """ + _ts("v_now") + r""");
END;
"""
)

# --- t070 examine ---------------------------------------------------------------------------

def _skip(code: str) -> str:
    """Data skip: last_reason, examined_count + 1, re-check in 1 h (a source write re-keys
    sooner)."""

    return f"""
        UPDATE {_S}.m05_outbound_schedule AS q
           SET last_reason = {code}, examined_count = q.examined_count OPERATOR(pg_catalog.+) 1,
               next_check_at = pg_catalog.clock_timestamp() OPERATOR(pg_catalog.+)
                   interval '1 hour',
               updated_at = pg_catalog.clock_timestamp()
         WHERE q.appointment_ref = v_ref;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'skipped',
            'code', {code});
"""


def _remove(code: str) -> str:
    return f"""
        DELETE FROM {_S}.m05_outbound_schedule AS q WHERE q.appointment_ref = v_ref;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'removed',
            'code', {code});
"""


# Handle a claim v2 result v_r (claimed / abandoned / deferred) for the locked row.
_AFTER_CLAIM: Final = r"""
    IF (v_r OPERATOR(pg_catalog.->>) 'outcome') = 'claimed' THEN
        DELETE FROM {schema}.m05_outbound_schedule AS q WHERE q.appointment_ref = v_ref;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'claimed',
            'code', NULL, 'op_id', v_r OPERATOR(pg_catalog.->>) 'op_id',
            'attempt_id', v_r OPERATOR(pg_catalog.->>) 'attempt_id',
            'fence', (v_r OPERATOR(pg_catalog.->>) 'fence')::integer);
    END IF;
    IF (v_r OPERATOR(pg_catalog.->>) 'outcome') = 'deferred' THEN
        UPDATE {schema}.m05_outbound_schedule AS q
           SET next_check_at = GREATEST(
                   (v_r OPERATOR(pg_catalog.->>) 'next_eligible_at')::timestamptz,
                   pg_catalog.clock_timestamp()),
               last_reason = v_r OPERATOR(pg_catalog.->>) 'code',
               examined_count = q.examined_count OPERATOR(pg_catalog.+) 1,
               updated_at = pg_catalog.clock_timestamp()
         WHERE q.appointment_ref = v_ref;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'deferred',
            'code', v_r OPERATOR(pg_catalog.->>) 'code',
            'op_id', v_r OPERATOR(pg_catalog.->>) 'op_id');
    END IF;
""" + _remove("v_r OPERATOR(pg_catalog.->>) 'code'")

_EXAMINE_BODY: Final = (
    "\nDECLARE" + _DECLARE_H2 + r"""
    v_rem uuid;
    v_hash text;
    v_lease integer;
    v_sv bigint;
    v_tv bigint;
    v_locked text[];
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'lease_seconds',
                                     'op_id', 'reminder_id', 'schema_version',
                                     'settings_version', 'source_version',
                                     'text_sha256']::text[]
""" + _string_field("op_id", "c_uuid") + _string_field("appointment_ref", "c_uuid")
    + _string_field("reminder_id", "c_uuid") + r"""       OR pg_catalog.jsonb_typeof(
              p_command OPERATOR(pg_catalog.->) 'text_sha256') NOT IN ('string', 'null')
       OR (p_command OPERATOR(pg_catalog.->>) 'text_sha256') OPERATOR(pg_catalog.!~)
              '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'source_version')
          NOT IN ('number', 'null')
       OR (p_command OPERATOR(pg_catalog.->>) 'source_version')
          OPERATOR(pg_catalog.!~) '^[0-9]{1,18}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'settings_version')
          NOT IN ('number', 'null')
       OR (p_command OPERATOR(pg_catalog.->>) 'settings_version')
          OPERATOR(pg_catalog.!~) '^[0-9]{1,18}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'lease_seconds')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'lease_seconds')
          OPERATOR(pg_catalog.!~) '^[0-9]{1,4}$' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_rem := (p_command OPERATOR(pg_catalog.->>) 'reminder_id')::uuid;
    v_hash := p_command OPERATOR(pg_catalog.->>) 'text_sha256';
    v_sv := (p_command OPERATOR(pg_catalog.->>) 'source_version')::bigint;
    v_tv := (p_command OPERATOR(pg_catalog.->>) 'settings_version')::bigint;
    v_lease := (p_command OPERATOR(pg_catalog.->>) 'lease_seconds')::integer;
    IF v_lease NOT BETWEEN 30 AND 600 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM {schema}.m05_outbound_schedule AS q
                    WHERE q.appointment_ref = v_ref) THEN
        """ + _not_ready("NOT_SCHEDULED") + r"""
    END IF;
    -- Pin 1 lock order: settings -> contacts (sorted) -> appointment -> profile -> schedule
    -- -> op. The contact set: the patient's phones plus any op's frozen phone.
    SELECT * INTO v_s FROM {schema}.m05_reminder_settings AS s FOR SHARE;
    SELECT COALESCE(pg_catalog.array_agg(DISTINCT x.phone ORDER BY x.phone), ARRAY[]::text[])
      INTO v_locked
      FROM (SELECT c.phone::text AS phone FROM {schema}.m05_patient_contacts AS c
              JOIN {schema}.m05_appointments AS a ON a.patient_id = c.patient_id
             WHERE a.appointment_ref = v_ref
            UNION SELECT o.phone::text FROM {schema}.m05_outbound_ops AS o
             WHERE o.appointment_ref = v_ref) AS x;
    INSERT INTO {schema}.m05_contact_state (phone, opted_out, opted_out_at)
    SELECT p, false, NULL FROM pg_catalog.unnest(v_locked) AS p ORDER BY p
        ON CONFLICT (phone) DO NOTHING;
    PERFORM 1 FROM {schema}.m05_contact_state AS c
      WHERE c.phone = ANY (v_locked) ORDER BY c.phone FOR UPDATE;
    SELECT a.patient_id, a.status, a.start_at INTO v_appt
      FROM {schema}.m05_appointments AS a WHERE a.appointment_ref = v_ref FOR SHARE;
    SELECT p.* INTO v_p FROM {schema}.m05_appointment_profile AS p
     WHERE p.appointment_ref = v_ref FOR SHARE;
    SELECT * INTO v_q FROM {schema}.m05_outbound_schedule AS q
     WHERE q.appointment_ref = v_ref FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM {schema}.m05_outbound_schedule AS q
                    WHERE q.appointment_ref = v_ref) THEN
            """ + _not_ready("LOCKED") + r"""
        END IF;
        """ + _not_ready("NOT_SCHEDULED") + r"""
    END IF;
    -- D2: re-keyed since selection -> nothing to do (zero writes).
    IF v_q.next_check_at OPERATOR(pg_catalog.>) pg_catalog.clock_timestamp() THEN
        """ + _not_ready("REKEYED") + r"""
    END IF;
    SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.appointment_ref = v_ref
       FOR UPDATE;
    IF FOUND AND v_o.state <> 'prepared' THEN  -- H1 owns it from here
""" + _remove("'OP_' || pg_catalog.upper(v_o.state)") + r"""
    END IF;
    IF v_appt.status IS DISTINCT FROM 'booked' THEN
""" + _remove("'NOT_BOOKED'") + r"""
    END IF;
    IF v_appt.start_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
""" + _remove("'START_PASSED'") + r"""
    END IF;
    IF v_o.op_id IS NOT NULL THEN  -- a prepared op: claim v2 decides (frozen inputs)
        IF NOT EXISTS (SELECT 1 FROM {schema}.m05_outbound_render_inputs AS i
                        WHERE i.op_id = v_o.op_id) THEN  -- D4: an H1-era prepared op
            UPDATE {schema}.m05_outbound_ops AS o SET state = 'abandoned',
                code = 'NO_RENDER_INPUTS', updated_at = pg_catalog.clock_timestamp()
             WHERE o.op_id = v_o.op_id;
""" + _remove("'NO_RENDER_INPUTS'") + r"""
        END IF;
        v_r := {schema}.m05_outbound_claim_v2(pg_catalog.jsonb_build_object(
            'schema_version', 1, 'op_id', v_o.op_id::text, 'fence', v_o.fence,
            'lease_seconds', v_lease, 'attribution', v_attr));
""" + _AFTER_CLAIM + r"""
    END IF;
    -- No op: data checks under the locks (R-H2-05, P-1c, R-H2-03).
    IF v_p.appointment_ref IS NULL THEN
""" + _skip("'PROFILE_MISSING'") + r"""
    END IF;
    IF v_p.source_status IS NOT DISTINCT FROM 'completed' THEN
""" + _skip("'COMPLETED'") + r"""
    END IF;
    IF v_p.source_status IS DISTINCT FROM 'booked' THEN
""" + _skip("'STATUS_UNSUPPORTED'") + r"""
    END IF;
    IF NOT """ + _name_ok("v_p.first_name", FIRST_NAME_MAX) + r""" THEN
""" + _skip("'NAME_INVALID'") + r"""
    END IF;
    IF NOT """ + _name_ok("v_s.clinic_display_name", CLINIC_NAME_MAX) + r""" THEN
""" + _skip("'SETTINGS_INCOMPLETE'") + r"""
    END IF;
""" + _RESOLVE_ZONE + r"""
    IF v_zone IS NULL THEN
""" + _skip("'TZ_UNRESOLVED'") + r"""
    END IF;
    SELECT pg_catalog.array_agg(c.phone::text ORDER BY c.phone) INTO v_phones
      FROM {schema}.m05_patient_contacts AS c WHERE c.patient_id = v_appt.patient_id;
    IF v_phones IS NULL OR pg_catalog.cardinality(v_phones) <> 1 THEN
""" + _skip("'UNROUTABLE_PHONE'") + r"""
    END IF;
    IF v_p.confirmation = 'confirmed' THEN
""" + _skip("'CONFIRMED'") + r"""
    END IF;
    SELECT c.opted_out INTO v_opted FROM {schema}.m05_contact_state AS c
     WHERE c.phone = v_phones[1];
    IF v_opted THEN
""" + _skip("'OPTED_OUT'") + r"""
    END IF;
    -- R-H2-02 with a fresh clock read after the locks.
""" + _decision_call("pg_catalog.clock_timestamp()", "v_appt.start_at", "v_zone", "v_d") + r"""
    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') = 'too_late' THEN
""" + _remove("'TOO_LATE_26H'") + r"""
    END IF;
    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') <> 'send' THEN
        v_r := pg_catalog.jsonb_build_object('outcome', 'deferred',
            'code', CASE WHEN (v_d OPERATOR(pg_catalog.->>) 'verdict') = 'not_due'
                         THEN 'NOT_DUE' ELSE 'OUTSIDE_WINDOW' END,
            'op_id', NULL, 'next_eligible_at', v_d OPERATOR(pg_catalog.->>) 'next_eligible_at');
""" + _AFTER_CLAIM + r"""
    END IF;
    -- The render the caller supplied must match the versions read under the locks.
    IF v_hash IS NULL OR v_sv IS DISTINCT FROM v_p.source_version
       OR v_tv IS DISTINCT FROM v_s.settings_version THEN
        UPDATE {schema}.m05_outbound_schedule AS q
           SET next_check_at = pg_catalog.clock_timestamp(), last_reason = 'STALE_SNAPSHOT',
               examined_count = q.examined_count OPERATOR(pg_catalog.+) 1,
               updated_at = pg_catalog.clock_timestamp()
         WHERE q.appointment_ref = v_ref;
        """ + _not_ready("STALE_SNAPSHOT") + r"""
    END IF;
    -- prepare_rendered and claim v2 in this transaction (Codex (a)): an unknown commit
    -- never leads to a send; a failure rolls everything back.
    v_r := {schema}.m05_outbound_prepare_rendered(pg_catalog.jsonb_build_object(
        'schema_version', 1, 'op_id', v_op::text, 'appointment_ref', v_ref::text,
        'reminder_id', v_rem::text, 'text_sha256', v_hash, 'source_version', v_sv,
        'settings_version', v_tv, 'attribution', v_attr));
    v_r := {schema}.m05_outbound_claim_v2(pg_catalog.jsonb_build_object(
        'schema_version', 1, 'op_id', v_r OPERATOR(pg_catalog.->>) 'op_id',
        'fence', (v_r OPERATOR(pg_catalog.->>) 'fence')::integer,
        'lease_seconds', v_lease, 'attribution', v_attr));
""" + _AFTER_CLAIM + r"""
END;
"""
)

# --- t071 consume ---------------------------------------------------------------------------

_CONSUME_BODY: Final = (
    "\nDECLARE" + _DECLARE_H2 + r"""
    v_attempt uuid;
    v_t record;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attempt_id', 'attribution', 'op_id',
                                     'schema_version']::text[]
""" + _string_field("op_id", "c_uuid") + _string_field("attempt_id", "c_uuid") + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_attempt := (p_command OPERATOR(pg_catalog.->>) 'attempt_id')::uuid;
    SELECT * INTO v_i FROM {schema}.m05_outbound_render_inputs AS i WHERE i.op_id = v_op;
    IF NOT FOUND THEN  -- pin 3: refused before any lock or write
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NO_RENDER_INPUTS';
    END IF;
""" + _LOCK_CHAIN + r"""
    SELECT * INTO v_t FROM {schema}.m05_outbound_send_tickets AS t WHERE t.op_id = v_op
       FOR UPDATE;
    IF NOT FOUND OR v_t.attempt_id IS DISTINCT FROM v_attempt THEN
        -- another attempt's ticket is never touched (zero writes)
        """ + _refusal("'CLAIM_STALE'") + r"""
    END IF;
    IF v_t.consumed_at IS NOT NULL THEN
        """ + _refusal("'TICKET_SPENT'") + r"""
    END IF;
    UPDATE {schema}.m05_outbound_send_tickets AS t SET consumed_at = pg_catalog.clock_timestamp()
     WHERE t.op_id = v_op AND t.attempt_id = v_attempt AND t.consumed_at IS NULL;
    -- read v2 (fresh clock after the locks)
    IF v_o.state <> 'attempting' OR v_o.attempt_id IS DISTINCT FROM v_attempt
       OR v_o.lease_until OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
        """ + _refusal("'CLAIM_STALE'") + r"""
    END IF;
    -- D11: the H1 D1 predicate (recipient, appointment, start, STOP, reminder id) again.
""" + _D1 + r"""
    IF v_code IS NOT NULL THEN
        UPDATE {schema}.m05_outbound_ops AS o
           SET state = 'refused', code = v_code, correlation_open = false,
               updated_at = pg_catalog.clock_timestamp()
         WHERE o.op_id = v_op;
        """ + _refusal("v_code") + r"""
    END IF;
    -- Codex 26edf09 I1: the current source status, under the profile lock.
    IF v_p.source_status IS DISTINCT FROM 'booked' THEN
        v_code := CASE WHEN v_p.source_status = 'completed' THEN 'COMPLETED'
                       ELSE 'STATUS_UNSUPPORTED' END;
        UPDATE {schema}.m05_outbound_ops AS o
           SET state = 'refused', code = v_code, correlation_open = false,
               updated_at = pg_catalog.clock_timestamp()
         WHERE o.op_id = v_op;
        """ + _refusal("v_code") + r"""
    END IF;
    -- The pre-call gate (R-H2-08): frozen zone and start, current settings, fresh clock.
""" + _decision_call("pg_catalog.clock_timestamp()", "v_o.start_at", "v_i.zone", "v_d") + r"""
    IF (v_d OPERATOR(pg_catalog.->>) 'verdict') <> 'send' THEN
        UPDATE {schema}.m05_outbound_ops AS o
           SET state = 'refused', code = 'PRECALL_WINDOW_CLOSED', correlation_open = false,
               updated_at = pg_catalog.clock_timestamp()
         WHERE o.op_id = v_op;
        """ + _refusal("'PRECALL_WINDOW_CLOSED'") + r"""
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'send',
        'inputs', pg_catalog.jsonb_build_object('phone', v_o.phone::text,
            'first_name', v_i.first_name, 'clinic_display_name', v_i.clinic_display_name,
            'zone', v_i.zone, 'zone_source', v_i.zone_source,
            'start_at', """ + _ts("v_o.start_at") + r""",
            'text_sha256', pg_catalog.encode(v_o.text_sha256, 'hex')));
END;
"""
)

# --- unit construction ----------------------------------------------------------------------

_COMMAND: Final = (("p_command", "jsonb"),)


def _template(name: str, volatility: str, body: str, grantee: str | None) -> str:
    text = (
        f"CREATE FUNCTION {_S}.{name}(p_command jsonb) RETURNS jsonb\n"
        f"    LANGUAGE plpgsql {_VOLATILITY_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}(jsonb) FROM PUBLIC;\n"
    )
    if grantee is not None:
        text += f"GRANT EXECUTE ON FUNCTION {_S}.{name}(jsonb) TO {grantee};\n"
    return text


def _definition(name: str, volatility: str, body: str, grantee: str | None) -> UnitDefinition:
    acl: list[dict[str, Any]] = (
        [] if grantee is None else [{"grantee": grantee, "privileges": ["EXECUTE"]}])
    return UnitDefinition(
        template=_template(name, volatility, body, grantee),
        execution_role=M05_OWNER_ROLE,
        kind=TYPED_FUNCTION_KIND,
        policy={
            "policy_format": 1,
            "semantic_version": 1,
            "parser_package": "pglast",
            "parser_version": "7.21",
            "grammar_major": 17,
            "functions": [{
                "schema": _S, "name": name,
                "inputs": [{"name": "p_command", "type": "jsonb"}], "outputs": [],
                "return_type": "jsonb", "language": "plpgsql", "is_procedure": False,
                "replace": False, "security_definer": True, "volatility": volatility,
                "parallel": "unsafe", "strict": False, "config": [_CONFIG],
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "acl": acl, "comment": None,
            }],
        },
        policy_verification={
            "kind": "function_metadata",
            "functions": [{
                "name": name, "argument_types": ["jsonb"], "owner": M05_OWNER_ROLE,
                "security_definer": True, "config": [_CONFIG], "acl": acl, "body": body,
            }],
        },
    )


def _profile(name: str, volatility: str, grantee: str | None) -> InstalledStateProfile:
    acl: tuple[tuple[str, str, str, bool], ...] = (
        (M05_OWNER_ROLE, "EXECUTE", M05_OWNER_ROLE, False),)
    if grantee is not None:
        acl += ((grantee, "EXECUTE", M05_OWNER_ROLE, False),)
    return InstalledStateProfile(
        function_name=name, argument_types=("jsonb",), owner=M05_OWNER_ROLE,
        execution_role=M05_OWNER_ROLE, prokind="f", return_type="jsonb", returns_set=False,
        language="plpgsql", security_definer=True, volatility=_VOLATILITY_CODE[volatility],
        parallel="u", strict=False, installed_acl=acl,
    )


# (migration id, function, volatility, body, EXECUTE grantee or None for owner only)
_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    ("t062_m05_reminder_decision", "m05_reminder_decision", "stable", _DECISION_BODY, None),
    ("t063_m05_directory_v2", "m05_directory_v2", "volatile", _DIRECTORY_V2_BODY,
     RUNTIME_ROLE),
    ("t064_m05_reminder_settings_put", "m05_reminder_settings_put", "volatile",
     _SETTINGS_PUT_BODY, SUPPORT_ROLE),
    ("t065_m05_outbound_prepare_rendered", "m05_outbound_prepare_rendered", "volatile",
     _PREPARE_RENDERED_BODY, None),
    ("t066_m05_outbound_claim_v2", "m05_outbound_claim_v2", "volatile", _CLAIM_V2_BODY,
     RUNTIME_ROLE),
    ("t067_m05_outbound_read_v2", "m05_outbound_read_v2", "volatile", _READ_V2_BODY,
     RUNTIME_ROLE),
    ("t068_m05_outbound_ready", "m05_outbound_ready", "volatile", _READY_BODY, RUNTIME_ROLE),
    ("t069_m05_outbound_render_snapshot", "m05_outbound_render_snapshot", "volatile",
     _SNAPSHOT_BODY, RUNTIME_ROLE),
    ("t070_m05_outbound_examine", "m05_outbound_examine", "volatile", _EXAMINE_BODY,
     RUNTIME_ROLE),
    ("t071_m05_outbound_consume", "m05_outbound_consume", "volatile", _CONSUME_BODY,
     RUNTIME_ROLE),
)

_AUTHORITY_ID: Final = "t074_m05_reminder_settings_authority"
_AUTHORITY_NAME: Final = "m05_reminder_settings_authority"

_ALL: Final[dict[str, str | UnitDefinition]] = {
    "t057_m05_reminder_settings": T057_SQL,
    "t058_m05_appointment_profile": T058_SQL,
    "t059_m05_outbound_schedule": T059_SQL,
    "t060_m05_outbound_render_inputs": T060_SQL,
    "t061_m05_outbound_send_tickets": T061_SQL,
    **{mid: _definition(name, vol, body, grantee)
       for mid, name, vol, body, grantee in _FUNCTIONS},
    CLOSURE_ID: UnitDefinition(template="", kind=ACL_CLOSURE_KIND,
                               execution_role=M05_OWNER_ROLE),
    "t073_m05_reminder_bootstrap": T073_SQL,
    _AUTHORITY_ID: staff_units._definition(
        _AUTHORITY_NAME, (), "jsonb", "volatile", M05_OWNER_ROLE, _AUTHORITY_BODY,
        hashlib.sha256(_AUTHORITY_BODY.encode("utf-8")).hexdigest()),
}
SH2_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
SH2_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    **{mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _FUNCTIONS},
    _AUTHORITY_ID: staff_units._profile(_AUTHORITY_NAME, (), "jsonb", "volatile",
                                        M05_OWNER_ROLE),
})
SH2_ACL_AMENDMENTS: Final[tuple[AclAmendment, ...]] = tuple(
    AclAmendment(target_unit=unit, activated_by=CLOSURE_ID,
                 removes=((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),))
    for unit in CLOSED_UNITS
)
