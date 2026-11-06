"""L-1S package S-A: per-tenant units t037..t043 (reply / no-reply intake).

Governing: S-A requirements v1r1, SA-TIME-1 state model v1, architecture v1 + v1r1,
interface pins v1 with corrections C1..C8 and red-package deltas v1r1 (P-1..P-4) and
v1r2 (P-1'), red gate PASS at db07570 (Codex 20261106T003822Z); t042/t043 split
(one function per profiled unit, relay 20261106T012729Z).

NOT composed into production: `haloflow.composition` names none of this. These units
install only through the S-A TEST registry (tests/m05/sa_support.py, target 43);
the production target stays 36 (control row SA-S01).

- ``t037_m05_intake_tables``  migrator-owned: seven tables, the immutability
  rejector, the boundary predicate ``intake_reply_is_timely`` (IMMUTABLE, INVOKER,
  deliberately not ``m05_*``), the ``m05_intake`` family and three contracts (all
  INACTIVE), the m05 envelope allow-list, and the M05 owner's exact grants. The
  runtime holds nothing.
- ``t038``..``t041``  runtime gateways (SECURITY DEFINER, EXECUTE to the runtime).
- ``t042``/``t043``   the D-S1 activation record functions: EXECUTE to no role but
  the owner; no statement key; read by no gateway (D-S1 "enable none").

Locking (SA-TIME-1, pins P-1'): every path that can change a reply window first
locks ``m05_contact_state`` for the phone (insert-if-absent), and directory writes
lock their whole phone set in global sorted order in ONE attempt; a set that grew
while waiting refuses ``DIRECTORY_CONTENDED`` instead of taking out-of-order locks.
Every comparison of a time with a deadline goes through ``intake_reply_is_timely``.
"""

import hashlib
from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition, UnitDefinitions
from haloflow.m02.roles import M05_OWNER_ROLE

T037_MIGRATION_ID: Final = "t037_m05_intake_tables"

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_VOLATILITY_WORD: Final = {"stable": "STABLE", "volatile": "VOLATILE"}
_VOLATILITY_CODE: Final = {"stable": "s", "volatile": "v"}
_Params = tuple[tuple[str, str], ...]

_REPLY_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":["inbound_id","reply_class","triage_kind","appointment_ref","reminder_id"],'
    '"properties":{"inbound_id":{"type":"string"},'
    '"reply_class":{"enum":["confirm","reschedule","cancel","unclear"]},'
    '"triage_kind":{"enum":["none","reschedule_request","cancel_request","unclear_reply"]},'
    '"appointment_ref":{},"reminder_id":{}}}'
)
_DISPATCHED_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":["reminder_id","appointment_ref"],'
    '"properties":{"reminder_id":{"type":"string"},"appointment_ref":{"type":"string"}}}'
)
_NO_REPLY_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":["reminder_id","appointment_ref","triage_kind"],'
    '"properties":{"reminder_id":{"type":"string"},"appointment_ref":{"type":"string"},'
    '"triage_kind":{"const":"no_reply"}}}'
)


def _contract(event_type: str, schema: str) -> str:
    return f"""INSERT INTO {_S}.event_contracts (
    event_type, version, action_family, event_level, allowed_statuses, terminality,
    compatible_outcome_class, precedence_rank, required_fields, required_links,
    optional_links, metadata_json_schema, metadata_max_bytes, authorized_producers,
    required_provenance, correction_policy, projection_rule_id, effective_from,
    retired_at, signal_rule, value_constraints
) VALUES (
    '{event_type}', 1, 'm05_intake', 'observation',
    ARRAY['completed']::varchar(64)[], true, 'observation', 100,
    ARRAY['correlation_id']::varchar(64)[], '[]'::jsonb, '[]'::jsonb,
    '{schema}'::jsonb, 1024, ARRAY['m05_intake']::varchar(64)[],
    ARRAY[]::varchar(64)[], NULL, NULL, '2026-11-05T00:00:00Z', NULL,
    '{{"handoff_kind":"event_signal","signal_type":"m02.event.appended"}}'::jsonb,
    '{{"operation_id":{{"must_be_null":true}}}}'::jsonb
);
"""


T037_SQL: Final = (
    r"""
-- t037:step:tables
CREATE TABLE {schema}.m05_intake_workloads (
    workload_role varchar(16) PRIMARY KEY
        CHECK (workload_role IN ('directory', 'dispatch', 'ingress', 'timer')),
    workload_id uuid NOT NULL UNIQUE
);
CREATE TABLE {schema}.m05_patient_contacts (
    phone varchar(16) NOT NULL CHECK (phone ~ '^\+[1-9][0-9]{7,14}$'),
    patient_id uuid NOT NULL,
    PRIMARY KEY (phone, patient_id)
);
CREATE TABLE {schema}.m05_appointments (
    appointment_ref uuid PRIMARY KEY,
    patient_id uuid NOT NULL,
    start_at timestamptz NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('booked', 'cancelled', 'replaced'))
);
CREATE TABLE {schema}.m05_contact_state (
    phone varchar(16) PRIMARY KEY CHECK (phone ~ '^\+[1-9][0-9]{7,14}$'),
    opted_out boolean NOT NULL,
    opted_out_at timestamptz,
    CONSTRAINT m05_contact_state_opt_out_at CHECK (opted_out = (opted_out_at IS NOT NULL))
);
CREATE TABLE {schema}.m05_reminder_windows (
    reminder_id uuid PRIMARY KEY,
    appointment_ref uuid NOT NULL,
    patient_id uuid NOT NULL,
    phone varchar(16) NOT NULL,
    sent_at timestamptz NOT NULL,
    deadline timestamptz NOT NULL,
    state varchar(16) NOT NULL
        CHECK (state IN ('awaiting', 'replied', 'opted_out', 'no_reply')),
    dispatch_fingerprint bytea NOT NULL CHECK (octet_length(dispatch_fingerprint) = 32),
    dispatch_event_id uuid NOT NULL UNIQUE,
    closed_decision varchar(16) CHECK (closed_decision IN ('no_reply', 'opted_out')),
    closed_event_id uuid UNIQUE,
    CONSTRAINT m05_reminder_windows_deadline CHECK (deadline = sent_at + interval '24 hours'),
    CONSTRAINT m05_reminder_windows_closed_event
        CHECK ((closed_event_id IS NOT NULL) = (closed_decision IS NOT DISTINCT FROM 'no_reply'))
);
CREATE INDEX m05_reminder_windows_phone ON {schema}.m05_reminder_windows (phone);
CREATE TABLE {schema}.m05_inbound_replies (
    inbound_id uuid PRIMARY KEY,
    channel varchar(8) NOT NULL CHECK (channel IN ('sms')),
    provider_message_id varchar(128) NOT NULL CHECK (char_length(provider_message_id) >= 1),
    phone varchar(16) NOT NULL,
    reply_class varchar(16) NOT NULL
        CHECK (reply_class IN ('confirm', 'reschedule', 'cancel', 'unclear', 'stop')),
    received_at timestamptz NOT NULL,
    fingerprint bytea NOT NULL CHECK (octet_length(fingerprint) = 32),
    decision varchar(16) NOT NULL
        CHECK (decision IN ('opt_out', 'quarantined', 'recorded', 'item')),
    triage_kind varchar(32)
        CHECK (triage_kind IN ('reschedule_request', 'cancel_request', 'unclear_reply')),
    patient_id uuid,
    appointment_ref uuid,
    reminder_id uuid,
    matched_reminders uuid[] NOT NULL,
    event_id uuid UNIQUE,
    CONSTRAINT m05_inbound_replies_message_key UNIQUE (channel, provider_message_id),
    CONSTRAINT m05_inbound_replies_event
        CHECK ((event_id IS NOT NULL) = (decision IN ('recorded', 'item'))),
    CONSTRAINT m05_inbound_replies_kind
        CHECK ((triage_kind IS NOT NULL) = (decision = 'item'))
);
CREATE TABLE {schema}.m05_activation_records (
    record_id uuid PRIMARY KEY,
    change varchar(8) NOT NULL CHECK (change IN ('grant', 'revoke')),
    actor_id uuid NOT NULL,
    ticket_reference varchar(64) NOT NULL CHECK (char_length(ticket_reference) >= 1),
    reason varchar(256) NOT NULL CHECK (char_length(reason) >= 1),
    created_at timestamptz NOT NULL
);

-- t037:step:integrity
CREATE FUNCTION {schema}.intake_record_reject() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
    SET search_path = pg_catalog, {schema}, pg_temp
    AS $body$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '0A000', MESSAGE = 'intake records are immutable';
END;
$body$;
REVOKE ALL ON FUNCTION {schema}.intake_record_reject() FROM PUBLIC;
CREATE TRIGGER m05_inbound_replies_immutable
    BEFORE UPDATE OR DELETE ON {schema}.m05_inbound_replies
    FOR EACH ROW EXECUTE FUNCTION {schema}.intake_record_reject();
CREATE TRIGGER m05_inbound_replies_no_truncate
    BEFORE TRUNCATE ON {schema}.m05_inbound_replies
    FOR EACH STATEMENT EXECUTE FUNCTION {schema}.intake_record_reject();
CREATE TRIGGER m05_activation_records_immutable
    BEFORE UPDATE OR DELETE ON {schema}.m05_activation_records
    FOR EACH ROW EXECUTE FUNCTION {schema}.intake_record_reject();
CREATE TRIGGER m05_activation_records_no_truncate
    BEFORE TRUNCATE ON {schema}.m05_activation_records
    FOR EACH STATEMENT EXECUTE FUNCTION {schema}.intake_record_reject();

-- t037:step:predicate
-- The ONE boundary rule (SA-TIME-1, Codex 2155 item 1): a time counts before a
-- deadline only if strictly earlier; the deadline instant itself is late. NULL in,
-- NULL out (never true).
CREATE FUNCTION {schema}.intake_reply_is_timely(timestamptz, timestamptz)
    RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SECURITY INVOKER
    SET search_path = pg_catalog, {schema}, pg_temp
    AS $body$ SELECT $1 OPERATOR(pg_catalog.<) $2 $body$;
REVOKE ALL ON FUNCTION {schema}.intake_reply_is_timely(timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.intake_reply_is_timely(timestamptz, timestamptz)
    TO haloflow_m05_owner;

-- t037:step:runtime_privileges
REVOKE ALL ON {schema}.m05_intake_workloads, {schema}.m05_patient_contacts,
    {schema}.m05_appointments, {schema}.m05_contact_state, {schema}.m05_reminder_windows,
    {schema}.m05_inbound_replies, {schema}.m05_activation_records FROM haloflow_runtime;

-- t037:step:grants
-- Exactly what t038..t043 need, column-level where a write is narrow. FOR UPDATE /
-- FOR SHARE need an UPDATE column privilege besides SELECT.
GRANT SELECT (workload_role, workload_id) ON {schema}.m05_intake_workloads
    TO haloflow_m05_owner;
GRANT SELECT (phone, patient_id), INSERT (phone, patient_id)
    ON {schema}.m05_patient_contacts TO haloflow_m05_owner;
GRANT DELETE ON {schema}.m05_patient_contacts TO haloflow_m05_owner;
GRANT SELECT (appointment_ref, patient_id, start_at, status),
    INSERT (appointment_ref, patient_id, start_at, status),
    UPDATE (patient_id, start_at, status)
    ON {schema}.m05_appointments TO haloflow_m05_owner;
GRANT SELECT (phone, opted_out, opted_out_at), INSERT (phone, opted_out, opted_out_at),
    UPDATE (opted_out, opted_out_at)
    ON {schema}.m05_contact_state TO haloflow_m05_owner;
GRANT SELECT (reminder_id, appointment_ref, patient_id, phone, sent_at, deadline, state,
        dispatch_fingerprint, dispatch_event_id, closed_decision, closed_event_id),
    INSERT (reminder_id, appointment_ref, patient_id, phone, sent_at, deadline, state,
        dispatch_fingerprint, dispatch_event_id, closed_decision, closed_event_id),
    UPDATE (state, closed_decision, closed_event_id)
    ON {schema}.m05_reminder_windows TO haloflow_m05_owner;
GRANT SELECT (inbound_id, channel, provider_message_id, phone, reply_class, received_at,
        fingerprint, decision, triage_kind, patient_id, appointment_ref, reminder_id,
        matched_reminders, event_id),
    INSERT (inbound_id, channel, provider_message_id, phone, reply_class, received_at,
        fingerprint, decision, triage_kind, patient_id, appointment_ref, reminder_id,
        matched_reminders, event_id)
    ON {schema}.m05_inbound_replies TO haloflow_m05_owner;
GRANT SELECT (record_id, change, actor_id, ticket_reference, reason, created_at),
    INSERT (record_id, change, actor_id, ticket_reference, reason, created_at)
    ON {schema}.m05_activation_records TO haloflow_m05_owner;

-- t037:step:contracts
-- Family, types and contracts INACTIVE (S-A test profiles activate them).
INSERT INTO {schema}.ref_action_families (action_family, owner_module, is_active) VALUES
    ('m05_intake', 'm05', false);
INSERT INTO {schema}.event_types (event_type, action_family, is_active) VALUES
    ('m05_reminder_dispatched', 'm05_intake', false),
    ('m05_reminder_reply_received', 'm05_intake', false),
    ('m05_reminder_no_reply', 'm05_intake', false);
"""
    + _contract("m05_reminder_dispatched", _DISPATCHED_SCHEMA)
    + _contract("m05_reminder_reply_received", _REPLY_SCHEMA)
    + _contract("m05_reminder_no_reply", _NO_REPLY_SCHEMA)
    + r"""
-- t037:step:envelope_allowlist
INSERT INTO {schema}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
    ('m05', 'm05_reminder_dispatched', 1, 'appointment_ref'),
    ('m05', 'm05_reminder_dispatched', 1, 'reminder_id'),
    ('m05', 'm05_reminder_reply_received', 1, 'appointment_ref'),
    ('m05', 'm05_reminder_reply_received', 1, 'inbound_id'),
    ('m05', 'm05_reminder_reply_received', 1, 'reminder_id'),
    ('m05', 'm05_reminder_reply_received', 1, 'reply_class'),
    ('m05', 'm05_reminder_reply_received', 1, 'triage_kind'),
    ('m05', 'm05_reminder_no_reply', 1, 'appointment_ref'),
    ('m05', 'm05_reminder_no_reply', 1, 'reminder_id'),
    ('m05', 'm05_reminder_no_reply', 1, 'triage_kind');
"""
)

# --- shared body fragments ---------------------------------------------------------

_DECLARE_COMMON: Final = r"""
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    c_phone constant text := '^\+[1-9][0-9]{7,14}$';
    v_setting text;
    v_attr jsonb;
    v_keys text[];
"""

_TENANT: Final = r"""
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(p_command) AS k;
"""

# Closed attribution shape (the t014 rules), then schema_version = 1.
_ATTRIBUTION: Final = r"""
    v_attr := p_command OPERATOR(pg_catalog.->) 'attribution';
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(v_attr) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_attr) AS k)
          IS DISTINCT FROM ARRAY['auth_method', 'execution_id', 'principal_id', 'principal_kind',
                                 'purpose_code', 'request_id']::text[]
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'principal_kind') <> 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') NOT IN ('actor', 'workload')
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'principal_id') <> 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'execution_id') <> 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'execution_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'auth_method') <> 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'auth_method')
           OPERATOR(pg_catalog.!~) '^[a-z][a-z0-9_]{0,31}$'
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'purpose_code') <> 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'purpose_code') NOT IN ('treatment', 'payment',
           'operations')
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'request_id') NOT IN ('null',
           'string')
       OR (pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'request_id') = 'string'
           AND pg_catalog.char_length(v_attr
               OPERATOR(pg_catalog.->>) 'request_id') NOT BETWEEN 1 AND 128) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
"""


def _workload(role: str) -> str:
    return f"""
    IF (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'workload'
       OR NOT EXISTS (
           SELECT 1 FROM {_S}.m05_intake_workloads AS w
            WHERE w.workload_role = '{role}'
              AND w.workload_id = (v_attr OPERATOR(pg_catalog.->>) 'principal_id')::uuid) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'WORKLOAD_NOT_AUTHORIZED';
    END IF;
"""


def _string_field(name: str, pattern: str) -> str:
    return (
        f"       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) '{name}')"
        " IS DISTINCT FROM 'string'\n"
        f"       OR (p_command OPERATOR(pg_catalog.->>) '{name}')"
        f" OPERATOR(pg_catalog.!~) {pattern}\n"
    )


_UTC: Final = "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'"


def _ts(expr: str) -> str:
    return f"pg_catalog.to_char(pg_catalog.timezone('UTC', {expr}), {_UTC})"


# Insert-if-absent, then lock ONE phone's contact row (the SA-TIME-1 lock).
def _contact_lock(phone: str, into: str) -> str:
    return f"""
    INSERT INTO {_S}.m05_contact_state (phone, opted_out, opted_out_at)
    VALUES ({phone}, false, NULL) ON CONFLICT (phone) DO NOTHING;
    SELECT c.opted_out INTO {into} FROM {_S}.m05_contact_state AS c
     WHERE c.phone = {phone} FOR UPDATE;
"""


# --- t038 directory -------------------------------------------------------------------

_DIRECTORY_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_op text;
    v_phone text;
    v_patient uuid;
    v_ref uuid;
    v_start timestamptz;
    v_status text;
    v_patients uuid[];
    v_locked text[];
    v_after text[];
BEGIN""" + _TENANT + r"""
    v_op := p_command OPERATOR(pg_catalog.->>) 'op';
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'op') IS DISTINCT FROM 'string'
       OR v_op NOT IN ('contact_put', 'contact_remove', 'appointment_put') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF v_op IN ('contact_put', 'contact_remove') THEN
        IF v_keys IS DISTINCT FROM ARRAY['attribution', 'op', 'patient_id', 'phone',
                                         'schema_version']::text[]
""" + _string_field("phone", "c_phone") + _string_field("patient_id", "c_uuid") + r"""        THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_phone := p_command OPERATOR(pg_catalog.->>) 'phone';
    ELSE
        IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'op', 'patient_id',
                                         'schema_version', 'start_at', 'status']::text[]
""" + _string_field("appointment_ref", "c_uuid") + _string_field("patient_id", "c_uuid")
    + _string_field(
        "start_at", r"'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'"
    ) + r"""           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'status')
              IS DISTINCT FROM 'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'status') NOT IN ('booked', 'cancelled',
              'replaced') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
        v_start := (p_command OPERATOR(pg_catalog.->>) 'start_at')::timestamptz;
        v_status := p_command OPERATOR(pg_catalog.->>) 'status';
    END IF;
    v_patient := (p_command OPERATOR(pg_catalog.->>) 'patient_id')::uuid;
""" + _ATTRIBUTION + _workload("directory") + r"""
    -- P-1': the whole lock set, computed once, locked in global sorted order.
    v_patients := ARRAY[v_patient];
    IF v_op = 'appointment_put' THEN
        v_patients := v_patients || ARRAY(
            SELECT a.patient_id FROM {schema}.m05_appointments AS a
             WHERE a.appointment_ref = v_ref);
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(DISTINCT x.phone ORDER BY x.phone), ARRAY[]::text[])
      INTO v_locked
      FROM (SELECT c.phone::text AS phone FROM {schema}.m05_patient_contacts AS c
             WHERE c.patient_id = ANY (v_patients)
            UNION SELECT v_phone WHERE v_phone IS NOT NULL) AS x;
    INSERT INTO {schema}.m05_contact_state (phone, opted_out, opted_out_at)
    SELECT p, false, NULL FROM pg_catalog.unnest(v_locked) AS p ORDER BY p
        ON CONFLICT (phone) DO NOTHING;
    PERFORM 1 FROM {schema}.m05_contact_state AS c
      WHERE c.phone = ANY (v_locked) ORDER BY c.phone FOR UPDATE;
    -- Re-read under the locks; a grown set refuses (no out-of-order lock is taken).
    IF v_op = 'appointment_put' THEN
        v_patients := ARRAY[v_patient] || ARRAY(
            SELECT a.patient_id FROM {schema}.m05_appointments AS a
             WHERE a.appointment_ref = v_ref);
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(DISTINCT c.phone::text), ARRAY[]::text[])
      INTO v_after
      FROM {schema}.m05_patient_contacts AS c WHERE c.patient_id = ANY (v_patients);
    IF NOT (v_after OPERATOR(pg_catalog.<@) v_locked) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DIRECTORY_CONTENDED';
    END IF;
    IF v_op = 'contact_put' THEN
        INSERT INTO {schema}.m05_patient_contacts (phone, patient_id)
        VALUES (v_phone, v_patient) ON CONFLICT (phone, patient_id) DO NOTHING;
    ELSIF v_op = 'contact_remove' THEN
        DELETE FROM {schema}.m05_patient_contacts AS c
         WHERE c.phone = v_phone AND c.patient_id = v_patient;
    ELSE
        INSERT INTO {schema}.m05_appointments (appointment_ref, patient_id, start_at, status)
        VALUES (v_ref, v_patient, v_start, v_status)
        ON CONFLICT (appointment_ref) DO UPDATE
           SET patient_id = EXCLUDED.patient_id, start_at = EXCLUDED.start_at,
               status = EXCLUDED.status;
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied');
END;
"""
)

# --- t039 dispatch --------------------------------------------------------------------

_WINDOW_RESULT: Final = (
    "pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', {outcome},"
    " 'reminder_id', v_w.reminder_id::text, 'appointment_ref', v_w.appointment_ref::text,"
    " 'patient_id', v_w.patient_id::text, 'phone', v_w.phone::text,"
    " 'sent_at', " + _ts("v_w.sent_at") + ", 'deadline', " + _ts("v_w.deadline") + ","
    " 'event_id', v_w.dispatch_event_id::text)"
)

_DISPATCH_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_reminder uuid;
    v_ref uuid;
    v_fp bytea;
    v_w record;
    v_appt record;
    v_phones text[];
    v_phone text;
    v_opted boolean;
    v_sent timestamptz;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'reminder_id',
                                     'schema_version']::text[]
""" + _string_field("reminder_id", "c_uuid")
    + _string_field("appointment_ref", "c_uuid")
    + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_reminder := (p_command OPERATOR(pg_catalog.->>) 'reminder_id')::uuid;
    v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
    -- C4: command-level fingerprint (a retry after a directory change still matches).
    v_fp := pg_catalog.sha256(pg_catalog.convert_to(
        '["m05.dispatch/v1",' || pg_catalog.to_json(v_setting)::text
        || ',' || pg_catalog.to_json(v_reminder::text)::text
        || ',' || pg_catalog.to_json(v_ref::text)::text || ']', 'UTF8'));

    -- Existing reminder FIRST (M11): its stored original wins, opted out or not.
    SELECT * INTO v_w FROM {schema}.m05_reminder_windows AS w WHERE w.reminder_id = v_reminder;
    IF FOUND THEN
        IF v_w.dispatch_fingerprint <> v_fp THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DISPATCH_CONFLICT';
        END IF;
        RETURN """ + _WINDOW_RESULT.format(outcome="'existing'") + r""";
    END IF;

    -- Route through the directory (patient and phone never from the caller).
    SELECT a.patient_id, a.status, a.start_at INTO v_appt
      FROM {schema}.m05_appointments AS a WHERE a.appointment_ref = v_ref;
    IF NOT FOUND OR v_appt.status <> 'booked'
       OR v_appt.start_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DISPATCH_UNROUTABLE';
    END IF;
    SELECT pg_catalog.array_agg(c.phone::text ORDER BY c.phone) INTO v_phones
      FROM {schema}.m05_patient_contacts AS c WHERE c.patient_id = v_appt.patient_id;
    IF v_phones IS NULL OR pg_catalog.cardinality(v_phones) <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DISPATCH_UNROUTABLE';
    END IF;
    v_phone := v_phones[1];
""" + _contact_lock("v_phone", "v_opted") + r"""
    -- A concurrent same-reminder dispatch may have committed while we waited.
    SELECT * INTO v_w FROM {schema}.m05_reminder_windows AS w WHERE w.reminder_id = v_reminder;
    IF FOUND THEN
        IF v_w.dispatch_fingerprint <> v_fp THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DISPATCH_CONFLICT';
        END IF;
        RETURN """ + _WINDOW_RESULT.format(outcome="'existing'") + r""";
    END IF;
    -- Re-validate the directory under the lock (consistent before-or-after view).
    SELECT a.patient_id, a.status, a.start_at INTO v_appt
      FROM {schema}.m05_appointments AS a WHERE a.appointment_ref = v_ref FOR SHARE;
    IF NOT FOUND OR v_appt.status <> 'booked'
       OR v_appt.start_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DISPATCH_UNROUTABLE';
    END IF;
    SELECT pg_catalog.array_agg(c.phone::text ORDER BY c.phone) INTO v_phones
      FROM {schema}.m05_patient_contacts AS c WHERE c.patient_id = v_appt.patient_id;
    IF v_phones IS DISTINCT FROM ARRAY[v_phone] THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DISPATCH_UNROUTABLE';
    END IF;
    IF v_opted THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTACT_OPTED_OUT';
    END IF;
    v_sent := pg_catalog.clock_timestamp();  -- after the lock
    INSERT INTO {schema}.m05_reminder_windows (reminder_id, appointment_ref, patient_id, phone,
        sent_at, deadline, state, dispatch_fingerprint, dispatch_event_id, closed_decision,
        closed_event_id)
    VALUES (v_reminder, v_ref, v_appt.patient_id, v_phone, v_sent,
        v_sent OPERATOR(pg_catalog.+) interval '24 hours', 'awaiting', v_fp,
        pg_catalog.gen_random_uuid(), NULL, NULL)
    RETURNING * INTO v_w;
    RETURN """ + _WINDOW_RESULT.format(outcome="'registered'") + r""";
END;
"""
)

# --- t040 inbound ------------------------------------------------------------------------

_INBOUND_RESULT: Final = (
    "pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', {outcome},"
    " 'inbound_id', v_r.inbound_id::text, 'decision', v_r.decision,"
    " 'triage_kind', v_r.triage_kind, 'patient_id', v_r.patient_id::text,"
    " 'appointment_ref', v_r.appointment_ref::text, 'reminder_id', v_r.reminder_id::text,"
    " 'received_at', " + _ts("v_r.received_at") + ", 'event_id', v_r.event_id::text)"
)

_INBOUND_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_channel text;
    v_message text;
    v_phone text;
    v_class text;
    v_opted boolean;
    v_received timestamptz;
    v_fp bytea;
    v_r record;
    v_patients uuid[];
    v_patient uuid;
    v_candidates uuid[];
    v_w record;
    v_decision text;
    v_kind text;
    v_ref uuid;
    v_reminder uuid;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'channel', 'phone', 'provider_message_id',
                                     'reply_class', 'schema_version']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'channel')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'channel') <> 'sms'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'provider_message_id')
          IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'provider_message_id')
          NOT BETWEEN 1 AND 128
""" + _string_field("phone", "c_phone")
    + r"""       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'reply_class')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'reply_class') NOT IN ('confirm', 'reschedule',
          'cancel', 'unclear', 'stop') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("ingress") + r"""
    v_channel := p_command OPERATOR(pg_catalog.->>) 'channel';
    v_message := p_command OPERATOR(pg_catalog.->>) 'provider_message_id';
    v_phone := p_command OPERATOR(pg_catalog.->>) 'phone';
    v_class := p_command OPERATOR(pg_catalog.->>) 'reply_class';
""" + _contact_lock("v_phone", "v_opted") + r"""
    v_received := pg_catalog.clock_timestamp();  -- AFTER the lock (SA-TIME-1)

    -- Dedupe on the semantic fingerprint (never on received_at).
    v_fp := pg_catalog.sha256(pg_catalog.convert_to(
        '["m05.inbound/v1",' || pg_catalog.to_json(v_setting)::text
        || ',' || pg_catalog.to_json(v_channel)::text
        || ',' || pg_catalog.to_json(v_message)::text
        || ',' || pg_catalog.to_json(v_phone)::text
        || ',' || pg_catalog.to_json(v_class)::text || ']', 'UTF8'));
    SELECT * INTO v_r FROM {schema}.m05_inbound_replies AS r
     WHERE r.channel = v_channel AND r.provider_message_id = v_message;
    IF FOUND THEN
        IF v_r.fingerprint <> v_fp THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INBOUND_CONFLICT';
        END IF;
        RETURN """ + _INBOUND_RESULT.format(outcome="'existing'") + r""";
    END IF;

    IF v_class = 'stop' THEN
        -- D-S2 row 1: the opt-out is on the number, across every patient's windows.
        UPDATE {schema}.m05_contact_state AS c
           SET opted_out = true, opted_out_at = COALESCE(c.opted_out_at, v_received)
         WHERE c.phone = v_phone;
        UPDATE {schema}.m05_reminder_windows AS w SET state = 'opted_out'
         WHERE w.phone = v_phone AND w.state = 'awaiting'
           AND {schema}.intake_reply_is_timely(v_received, w.deadline);
        v_decision := 'opt_out';
        v_candidates := ARRAY[]::uuid[];
    ELSE
        -- Identity from the authoritative directory, BEFORE any window change.
        SELECT pg_catalog.array_agg(c.patient_id ORDER BY c.patient_id) INTO v_patients
          FROM {schema}.m05_patient_contacts AS c WHERE c.phone = v_phone;
        IF v_patients IS NULL OR pg_catalog.cardinality(v_patients) <> 1 THEN
            v_decision := 'quarantined';
            v_candidates := ARRAY[]::uuid[];
        ELSE
            v_patient := v_patients[1];
            -- P-2: current appointment, still this patient's, window in any state.
            SELECT COALESCE(pg_catalog.array_agg(w.reminder_id ORDER BY w.reminder_id),
                            ARRAY[]::uuid[])
              INTO v_candidates
              FROM {schema}.m05_reminder_windows AS w
              JOIN {schema}.m05_appointments AS a ON a.appointment_ref = w.appointment_ref
             WHERE w.phone = v_phone AND w.patient_id = v_patient
               AND a.patient_id = w.patient_id AND a.status = 'booked'
               AND a.start_at OPERATOR(pg_catalog.>) v_received;
            IF pg_catalog.cardinality(v_candidates) = 1 THEN
                SELECT * INTO v_w FROM {schema}.m05_reminder_windows AS w
                 WHERE w.reminder_id = v_candidates[1] FOR UPDATE;
                IF v_w.state = 'awaiting'
                   AND {schema}.intake_reply_is_timely(v_received, v_w.deadline) THEN
                    UPDATE {schema}.m05_reminder_windows AS w SET state = 'replied'
                     WHERE w.reminder_id = v_w.reminder_id;
                END IF;
                v_ref := v_w.appointment_ref;
                v_reminder := v_w.reminder_id;
                v_kind := CASE v_class WHEN 'reschedule' THEN 'reschedule_request'
                                       WHEN 'cancel' THEN 'cancel_request'
                                       WHEN 'unclear' THEN 'unclear_reply' END;
            ELSE
                -- D-S2 row 7 / v1r1 section 1: never a guess, no window change.
                v_kind := 'unclear_reply';
            END IF;
            v_decision := CASE WHEN v_kind IS NULL THEN 'recorded' ELSE 'item' END;
        END IF;
    END IF;

    INSERT INTO {schema}.m05_inbound_replies (inbound_id, channel, provider_message_id, phone,
        reply_class, received_at, fingerprint, decision, triage_kind, patient_id,
        appointment_ref, reminder_id, matched_reminders, event_id)
    VALUES (pg_catalog.gen_random_uuid(), v_channel, v_message, v_phone, v_class, v_received,
        v_fp, v_decision, v_kind, v_patient, v_ref, v_reminder, v_candidates,
        CASE WHEN v_decision IN ('recorded', 'item') THEN pg_catalog.gen_random_uuid() END)
    RETURNING * INTO v_r;
    RETURN """ + _INBOUND_RESULT.format(outcome="'registered'") + r""";
END;
"""
)

# --- t041 close ---------------------------------------------------------------------------

_CLOSE_RESULT: Final = (
    "pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', {outcome},"
    " 'decision', {decision}, 'event_id', {event},"
    " 'patient_id', v_w.patient_id::text, 'appointment_ref', v_w.appointment_ref::text)"
)

_CLOSE_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_reminder uuid;
    v_phone text;
    v_opted boolean;
    v_w record;
    v_now timestamptz;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'reminder_id', 'schema_version']::text[]
""" + _string_field("reminder_id", "c_uuid") + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("timer") + r"""
    v_reminder := (p_command OPERATOR(pg_catalog.->>) 'reminder_id')::uuid;
    SELECT w.phone INTO v_phone FROM {schema}.m05_reminder_windows AS w
     WHERE w.reminder_id = v_reminder;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'WINDOW_NOT_FOUND';
    END IF;
""" + _contact_lock("v_phone", "v_opted") + r"""
    SELECT * INTO v_w FROM {schema}.m05_reminder_windows AS w
     WHERE w.reminder_id = v_reminder FOR UPDATE;
    -- C8 order: stored close; not awaiting; not due; opted out; no_reply.
    IF v_w.closed_decision IS NOT NULL THEN
        RETURN """ + _CLOSE_RESULT.format(
            outcome="'existing'", decision="v_w.closed_decision",
            event="v_w.closed_event_id::text") + r""";
    END IF;
    IF v_w.state <> 'awaiting' THEN
        RETURN """ + _CLOSE_RESULT.format(
            outcome="'not_awaiting'", decision="NULL::text", event="NULL::text") + r""";
    END IF;
    v_now := pg_catalog.clock_timestamp();  -- after the lock
    IF {schema}.intake_reply_is_timely(v_now, v_w.deadline) THEN
        RETURN """ + _CLOSE_RESULT.format(
            outcome="'not_due'", decision="NULL::text", event="NULL::text") + r""";
    END IF;
    IF v_opted THEN
        UPDATE {schema}.m05_reminder_windows AS w
           SET state = 'opted_out', closed_decision = 'opted_out'
         WHERE w.reminder_id = v_reminder
        RETURNING * INTO v_w;
    ELSE
        UPDATE {schema}.m05_reminder_windows AS w
           SET state = 'no_reply', closed_decision = 'no_reply',
               closed_event_id = pg_catalog.gen_random_uuid()
         WHERE w.reminder_id = v_reminder
        RETURNING * INTO v_w;
    END IF;
    RETURN """ + _CLOSE_RESULT.format(
        outcome="'closed'", decision="v_w.closed_decision",
        event="v_w.closed_event_id::text") + r""";
END;
"""
)

# --- t042 / t043 activation (D-S1, isolated) ---------------------------------------------

_ACTIVATION_CHANGE_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_record uuid;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['actor_id', 'change', 'reason', 'schema_version',
                                     'ticket_reference']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'change')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'change') NOT IN ('grant', 'revoke')
""" + _string_field("actor_id", "c_uuid")
    + r"""       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'ticket_reference')
          IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'ticket_reference')
          NOT BETWEEN 1 AND 64
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'reason')
          IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'reason')
          NOT BETWEEN 1 AND 256 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_record := pg_catalog.gen_random_uuid();
    INSERT INTO {schema}.m05_activation_records (record_id, change, actor_id, ticket_reference,
        reason, created_at)
    VALUES (v_record, p_command OPERATOR(pg_catalog.->>) 'change',
        (p_command OPERATOR(pg_catalog.->>) 'actor_id')::uuid,
        p_command OPERATOR(pg_catalog.->>) 'ticket_reference',
        p_command OPERATOR(pg_catalog.->>) 'reason', pg_catalog.clock_timestamp());
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'record_id', v_record::text,
        'change', p_command OPERATOR(pg_catalog.->>) 'change');
END;
"""
)

_ACTIVATION_STATE_BODY: Final = r"""
DECLARE
    v_last record;
BEGIN
    SELECT r.record_id, r.change INTO v_last FROM {schema}.m05_activation_records AS r
     ORDER BY r.created_at DESC, r.record_id DESC LIMIT 1;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('active', false, 'last_record_id', NULL);
    END IF;
    RETURN pg_catalog.jsonb_build_object('active', v_last.change = 'grant',
        'last_record_id', v_last.record_id::text);
END;
"""


# --- unit construction (the t014 Form A declaration, with an owner-only option) --------


def _template(name: str, params: _Params, volatility: str, body: str, runtime: bool) -> str:
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    text = (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS jsonb\n"
        f"    LANGUAGE plpgsql {_VOLATILITY_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
    )
    if runtime:
        text += f"GRANT EXECUTE ON FUNCTION {_S}.{name}({argtypes}) TO {RUNTIME_ROLE};\n"
    return text


def _definition(name: str, params: _Params, volatility: str, body: str,
                runtime: bool) -> UnitDefinition:
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

    def acl() -> list[dict[str, Any]]:
        return [{"grantee": RUNTIME_ROLE, "privileges": ["EXECUTE"]}] if runtime else []

    return UnitDefinition(
        template=_template(name, params, volatility, body, runtime),
        execution_role=M05_OWNER_ROLE,
        kind=TYPED_FUNCTION_KIND,
        policy={
            "policy_format": 1,
            "semantic_version": 1,
            "parser_package": "pglast",
            "parser_version": "7.21",
            "grammar_major": 17,
            "functions": [
                {
                    "schema": _S,
                    "name": name,
                    "inputs": [{"name": p, "type": t} for p, t in params],
                    "outputs": [],
                    "return_type": "jsonb",
                    "language": "plpgsql",
                    "is_procedure": False,
                    "replace": False,
                    "security_definer": True,
                    "volatility": volatility,
                    "parallel": "unsafe",
                    "strict": False,
                    "config": [_CONFIG],
                    "body_sha256": body_sha256,
                    "acl": acl(),
                    "comment": None,
                }
            ],
        },
        policy_verification={
            "kind": "function_metadata",
            "functions": [
                {
                    "name": name,
                    "argument_types": [t for _, t in params],
                    "owner": M05_OWNER_ROLE,
                    "security_definer": True,
                    "config": [_CONFIG],
                    "acl": acl(),
                    "body": body,
                }
            ],
        },
    )


def _profile(name: str, params: _Params, volatility: str, runtime: bool) -> InstalledStateProfile:
    acl: tuple[tuple[str, str, str, bool], ...] = ((M05_OWNER_ROLE, "EXECUTE", M05_OWNER_ROLE,
                                                     False),)
    if runtime:
        acl += ((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),)
    return InstalledStateProfile(
        function_name=name,
        argument_types=tuple(t for _, t in params),
        owner=M05_OWNER_ROLE,
        execution_role=M05_OWNER_ROLE,
        prokind="f",
        return_type="jsonb",
        returns_set=False,
        language="plpgsql",
        security_definer=True,
        volatility=_VOLATILITY_CODE[volatility],
        parallel="u",
        strict=False,
        installed_acl=acl,
    )


_COMMAND: Final[_Params] = (("p_command", "jsonb"),)

# (migration id, function, parameters, volatility, body, runtime EXECUTE)
_FUNCTIONS: Final[tuple[tuple[str, str, _Params, str, str, bool], ...]] = (
    ("t038_m05_directory_put", "m05_directory_put", _COMMAND, "volatile", _DIRECTORY_BODY,
     True),
    ("t039_m05_register_dispatch", "m05_register_dispatch", _COMMAND, "volatile",
     _DISPATCH_BODY, True),
    ("t040_m05_register_inbound", "m05_register_inbound", _COMMAND, "volatile",
     _INBOUND_BODY, True),
    ("t041_m05_close_window", "m05_close_window", _COMMAND, "volatile", _CLOSE_BODY, True),
    ("t042_m05_activation_change", "m05_activation_change", _COMMAND, "volatile",
     _ACTIVATION_CHANGE_BODY, False),
    ("t043_m05_activation_state", "m05_activation_state", (), "stable",
     _ACTIVATION_STATE_BODY, False),
)

SA_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {
        T037_MIGRATION_ID: T037_SQL,
        **{
            migration_id: _definition(name, params, volatility, body, runtime)
            for migration_id, name, params, volatility, body, runtime in _FUNCTIONS
        },
    }
)
SA_INSTALLED_STATE_PROFILES: Final = MappingProxyType(
    {
        migration_id: _profile(name, params, volatility, runtime)
        for migration_id, name, params, volatility, _body, runtime in _FUNCTIONS
    }
)
