"""L-1S package S-H, checkpoint H1: synthetic outbound send/recovery units t047..t056.

Governing: S-H decision table v1r2 (D1-D6), Codex pins 1-6, the H1 packet as corrected
by Codex's packet and red-test reviews, and the versioned-ingress correction (Codex ALIGN
20261108T145700Z, target 56). Synthetic only; nothing here sends a message.

NOT composed into production: `haloflow.composition` names none of this. These units
install only through the S-H TEST registry (tests/m05/sh_support.py, target 56).

- ``t047_m05_outbound_ops``  migrator-owned: the operation table (one op per stable
  appointment_ref, D4) and the M05 owner's exact grants. The runtime holds nothing.
- ``t048``..``t052``  runtime gateways: prepare, claim (fenced CAS + D1 predicate),
  outcome (bookkeeping), expire (lease -> uncertain, never prepared), register (D1
  verify under the frozen phone's contact lock, then t039 in the same transaction).
- ``t053_m05_register_inbound_v2``  t040's body verbatim plus ONE predicate: no window is
  a match candidate while any op on the replying (frozen) phone has correlation_open.
  STOP, replay and quarantine paths are unchanged.
- ``t054``  remove-only ``acl_closure_v1``: runtime EXECUTE leaves the unchanged t040.
- ``t055``/``t056``  read (frozen snapshot for the current, unexpired attempt only) and
  held inspection (no phone, name or text).

Lock order everywhere: the frozen phone's ``m05_contact_state`` row (the SA-TIME-1 lock),
then the op row. Expire takes op rows only, with SKIP LOCKED (it never waits).
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.installed_state import AclAmendment
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import ACL_CLOSURE_KIND, UnitDefinition, UnitDefinitions
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05.intake_units import (
    _ATTRIBUTION,
    _COMMAND,
    _DECLARE_COMMON,
    _INBOUND_BODY,
    _TENANT,
    _contact_lock,
    _definition,
    _profile,
    _string_field,
    _ts,
    _workload,
)

T047_MIGRATION_ID: Final = "t047_m05_outbound_ops"
T054_MIGRATION_ID: Final = "t054_m05_inbound_v1_runtime_closure"
T040_MIGRATION_ID: Final = "t040_m05_register_inbound"
_S: Final = "{schema}"

T047_SQL: Final = r"""
-- t047:step:table
CREATE TABLE {schema}.m05_outbound_ops (
    op_id uuid PRIMARY KEY,
    appointment_ref uuid NOT NULL UNIQUE,
    reminder_id uuid NOT NULL UNIQUE,
    patient_id uuid NOT NULL,
    phone varchar(16) NOT NULL CHECK (phone ~ '^\+[1-9][0-9]{7,14}$'),
    start_at timestamptz NOT NULL,
    text_sha256 bytea NOT NULL CHECK (octet_length(text_sha256) = 32),
    fingerprint bytea NOT NULL CHECK (octet_length(fingerprint) = 32),
    state varchar(24) NOT NULL CHECK (state IN ('prepared', 'attempting', 'accepted',
        'refused', 'uncertain', 'registered', 'accepted_unregistered', 'abandoned')),
    fence integer NOT NULL CHECK (fence >= 0),
    attempt_id uuid,
    lease_until timestamptz,
    provider_ref varchar(128),
    code varchar(32),
    correlation_open boolean NOT NULL,
    registered_result jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT m05_outbound_ops_attempt CHECK (
        (state IN ('prepared', 'abandoned')) OR attempt_id IS NOT NULL),
    CONSTRAINT m05_outbound_ops_registered CHECK (
        (state = 'registered') = (registered_result IS NOT NULL))
);
CREATE INDEX m05_outbound_ops_open_phone ON {schema}.m05_outbound_ops (phone)
    WHERE correlation_open;
CREATE INDEX m05_outbound_ops_lease ON {schema}.m05_outbound_ops (lease_until, op_id)
    WHERE state = 'attempting';

-- t047:step:runtime_privileges
REVOKE ALL ON {schema}.m05_outbound_ops FROM haloflow_runtime;

-- t047:step:grants
GRANT SELECT (op_id, appointment_ref, reminder_id, patient_id, phone, start_at, text_sha256,
        fingerprint, state, fence, attempt_id, lease_until, provider_ref, code,
        correlation_open, registered_result, created_at, updated_at),
    INSERT (op_id, appointment_ref, reminder_id, patient_id, phone, start_at, text_sha256,
        fingerprint, state, fence, attempt_id, lease_until, provider_ref, code,
        correlation_open, registered_result, created_at, updated_at),
    UPDATE (state, fence, attempt_id, lease_until, provider_ref, code, correlation_open,
        registered_result, updated_at)
    ON {schema}.m05_outbound_ops TO haloflow_m05_owner;
"""

_OP_RESULT: Final = (
    "pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', {outcome},"
    " 'op_id', v_o.op_id::text, 'state', v_o.state, 'fence', v_o.fence,"
    " 'appointment_ref', v_o.appointment_ref::text, 'reminder_id', v_o.reminder_id::text,"
    " 'code', v_o.code, 'attempt_id', {attempt})"
)

# Read the op's frozen phone, take the contact lock, then lock the op row.
def _lock_op(missing_code: str) -> str:
    return f"""
    SELECT o.phone::text INTO v_phone FROM {_S}.m05_outbound_ops AS o WHERE o.op_id = v_op;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{missing_code}';
    END IF;
""" + _contact_lock("v_phone", "v_opted") + f"""
    SELECT * INTO v_o FROM {_S}.m05_outbound_ops AS o WHERE o.op_id = v_op FOR UPDATE;
"""


# D1 (pin 3): the directory must equal the frozen snapshot. Caller holds the frozen phone's
# contact lock (v_opted). Sets v_code to NULL or the first failing code.
_D1: Final = r"""
    v_code := NULL;
    SELECT a.patient_id, a.status, a.start_at INTO v_appt
      FROM {schema}.m05_appointments AS a WHERE a.appointment_ref = v_o.appointment_ref
       FOR SHARE;
    IF NOT FOUND THEN
        v_code := 'APPOINTMENT_MISSING';
    ELSE
        SELECT pg_catalog.array_agg(c.phone::text ORDER BY c.phone) INTO v_phones
          FROM {schema}.m05_patient_contacts AS c WHERE c.patient_id = v_appt.patient_id;
        IF v_appt.patient_id <> v_o.patient_id
           OR v_phones IS DISTINCT FROM ARRAY[v_o.phone::text] THEN
            v_code := 'IDENTITY_CHANGED';
        ELSIF v_appt.status <> 'booked' OR v_appt.start_at <> v_o.start_at THEN
            v_code := 'APPOINTMENT_CHANGED';
        ELSIF v_appt.start_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
            v_code := 'START_PASSED';
        ELSIF v_opted THEN
            v_code := 'OPTED_OUT';
        END IF;
    END IF;
    -- R1: a window for this reminder that this op did not create (its own is created in
    -- the same transaction that sets 'registered') is foreign: never send, never register.
    IF v_code IS NULL AND EXISTS (SELECT 1 FROM {schema}.m05_reminder_windows AS w
                                   WHERE w.reminder_id = v_o.reminder_id) THEN
        v_code := 'REMINDER_IN_USE';
    END IF;
"""

_DECLARE_OP: Final = _DECLARE_COMMON + r"""
    v_op uuid;
    v_o record;
    v_appt record;
    v_phones text[];
    v_phone text;
    v_opted boolean;
    v_code text;
    v_now timestamptz;
"""

# --- t048 prepare ---------------------------------------------------------------------------

_PREPARE_BODY: Final = (
    "\nDECLARE" + _DECLARE_OP + r"""
    v_ref uuid;
    v_rem uuid;
    v_hash text;
    v_fp bytea;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'op_id', 'reminder_id',
                                     'schema_version', 'text_sha256']::text[]
""" + _string_field("op_id", "c_uuid") + _string_field("appointment_ref", "c_uuid")
    + _string_field("reminder_id", "c_uuid") + _string_field("text_sha256", "'^[0-9a-f]{64}$'")
    + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
    v_rem := (p_command OPERATOR(pg_catalog.->>) 'reminder_id')::uuid;
    v_hash := p_command OPERATOR(pg_catalog.->>) 'text_sha256';
    v_fp := pg_catalog.sha256(pg_catalog.convert_to(
        '["m05.outbound.prepare/v1",' || pg_catalog.to_json(v_setting)::text
        || ',' || pg_catalog.to_json(v_op::text)::text
        || ',' || pg_catalog.to_json(v_ref::text)::text
        || ',' || pg_catalog.to_json(v_rem::text)::text
        || ',' || pg_catalog.to_json(v_hash)::text || ']', 'UTF8'));

    FOR i IN 1..2 LOOP  -- before the lock, then again under it (a concurrent prepare)
        SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.op_id = v_op;
        IF FOUND THEN
            IF v_o.fingerprint <> v_fp THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'OP_CONFLICT';
            END IF;
            RETURN """ + _OP_RESULT.format(outcome="'existing'", attempt="NULL") + r""";
        END IF;
        SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.appointment_ref = v_ref;
        IF FOUND THEN  -- D4: one op per stable appointment_ref; the original wins
            RETURN """ + _OP_RESULT.format(outcome="'existing'", attempt="NULL") + r""";
        END IF;
        IF i = 2 THEN
            EXIT;
        END IF;
        SELECT a.patient_id, a.status, a.start_at INTO v_appt
          FROM {schema}.m05_appointments AS a WHERE a.appointment_ref = v_ref;
        IF NOT FOUND OR v_appt.status <> 'booked'
           OR v_appt.start_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PREPARE_UNROUTABLE';
        END IF;
        SELECT pg_catalog.array_agg(c.phone::text ORDER BY c.phone) INTO v_phones
          FROM {schema}.m05_patient_contacts AS c WHERE c.patient_id = v_appt.patient_id;
        IF v_phones IS NULL OR pg_catalog.cardinality(v_phones) <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PREPARE_UNROUTABLE';
        END IF;
        v_phone := v_phones[1];
""" + _contact_lock("v_phone", "v_opted") + r"""
    END LOOP;

    -- Re-validate the directory under the lock (consistent before-or-after view).
    SELECT a.patient_id, a.status, a.start_at INTO v_appt
      FROM {schema}.m05_appointments AS a WHERE a.appointment_ref = v_ref FOR SHARE;
    IF NOT FOUND OR v_appt.status <> 'booked'
       OR v_appt.start_at OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PREPARE_UNROUTABLE';
    END IF;
    SELECT pg_catalog.array_agg(c.phone::text ORDER BY c.phone) INTO v_phones
      FROM {schema}.m05_patient_contacts AS c WHERE c.patient_id = v_appt.patient_id;
    IF v_phones IS DISTINCT FROM ARRAY[v_phone] THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PREPARE_UNROUTABLE';
    END IF;
    IF v_opted THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTACT_OPTED_OUT';
    END IF;
    -- R1: the reminder id must be unused (no window, no other op), before any send.
    IF EXISTS (SELECT 1 FROM {schema}.m05_reminder_windows AS w WHERE w.reminder_id = v_rem)
       OR EXISTS (SELECT 1 FROM {schema}.m05_outbound_ops AS o WHERE o.reminder_id = v_rem) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'REMINDER_IN_USE';
    END IF;
    v_now := pg_catalog.clock_timestamp();
    INSERT INTO {schema}.m05_outbound_ops (op_id, appointment_ref, reminder_id, patient_id,
        phone, start_at, text_sha256, fingerprint, state, fence, attempt_id, lease_until,
        provider_ref, code, correlation_open, registered_result, created_at, updated_at)
    VALUES (v_op, v_ref, v_rem, v_appt.patient_id, v_phone, v_appt.start_at,
        pg_catalog.decode(v_hash, 'hex'), v_fp, 'prepared', 0, NULL, NULL, NULL, NULL, false,
        NULL, v_now, v_now)
    RETURNING * INTO v_o;
    RETURN """ + _OP_RESULT.format(outcome="'prepared'", attempt="NULL") + r""";
END;
"""
)

# --- t049 claim -----------------------------------------------------------------------------

_CLAIM_BODY: Final = (
    "\nDECLARE" + _DECLARE_OP + r"""
    v_fence integer;
    v_lease integer;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'fence', 'lease_seconds', 'op_id',
                                     'schema_version']::text[]
""" + _string_field("op_id", "c_uuid") + r"""       OR pg_catalog.jsonb_typeof(
              p_command OPERATOR(pg_catalog.->) 'fence')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'fence') OPERATOR(pg_catalog.!~) '^[0-9]{1,9}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'lease_seconds')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'lease_seconds')
          OPERATOR(pg_catalog.!~) '^[0-9]{1,4}$' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_fence := (p_command OPERATOR(pg_catalog.->>) 'fence')::integer;
    v_lease := (p_command OPERATOR(pg_catalog.->>) 'lease_seconds')::integer;
    IF v_lease NOT BETWEEN 30 AND 600 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _lock_op("CLAIM_STALE") + r"""
    IF v_o.state <> 'prepared' OR v_o.fence <> v_fence THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CLAIM_STALE';
    END IF;
""" + _D1 + r"""
    v_now := pg_catalog.clock_timestamp();
    IF v_code IS NOT NULL THEN  -- never sent, never claimed: terminal, no correlation
        UPDATE {schema}.m05_outbound_ops AS o SET state = 'abandoned', code = v_code,
            updated_at = v_now WHERE o.op_id = v_op RETURNING * INTO v_o;
        RETURN """ + _OP_RESULT.format(outcome="'abandoned'", attempt="NULL") + r""";
    END IF;
    UPDATE {schema}.m05_outbound_ops AS o
       SET state = 'attempting', attempt_id = pg_catalog.gen_random_uuid(),
           fence = o.fence + 1,
           lease_until = v_now OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => v_lease),
           correlation_open = true, updated_at = v_now
     WHERE o.op_id = v_op RETURNING * INTO v_o;
    RETURN """ + _OP_RESULT.format(outcome="'claimed'", attempt="v_o.attempt_id::text") + r""";
END;
"""
)

# --- t050 outcome (bookkeeping; not activation-protected) -----------------------------------

_OUTCOME_BODY: Final = (
    "\nDECLARE" + _DECLARE_OP + r"""
    v_attempt uuid;
    v_out text;
    v_ref_in text;
    v_code_in text;
    v_same boolean;
BEGIN""" + _TENANT + r"""
    v_out := p_command OPERATOR(pg_catalog.->>) 'outcome';
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'outcome')
          IS DISTINCT FROM 'string'
       OR v_out NOT IN ('accepted', 'refused', 'uncertain')
       OR (v_out = 'accepted' AND v_keys IS DISTINCT FROM ARRAY['attempt_id', 'attribution',
           'op_id', 'outcome', 'provider_ref', 'schema_version']::text[])
       OR (v_out <> 'accepted' AND v_keys IS DISTINCT FROM ARRAY['attempt_id', 'attribution',
           'code', 'op_id', 'outcome', 'schema_version']::text[])
""" + _string_field("op_id", "c_uuid") + _string_field("attempt_id", "c_uuid") + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF v_out = 'accepted' THEN
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'provider_ref')
              IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'provider_ref')
              NOT BETWEEN 1 AND 128 THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_ref_in := p_command OPERATOR(pg_catalog.->>) 'provider_ref';
    ELSE
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'code')
              IS DISTINCT FROM 'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'code')
              OPERATOR(pg_catalog.!~) '^[A-Za-z0-9_-]{1,32}$' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_code_in := p_command OPERATOR(pg_catalog.->>) 'code';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_attempt := (p_command OPERATOR(pg_catalog.->>) 'attempt_id')::uuid;
""" + _lock_op("STALE_ATTEMPT") + r"""
    IF v_o.attempt_id IS DISTINCT FROM v_attempt THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STALE_ATTEMPT';
    END IF;
    v_same := CASE WHEN v_out = 'accepted' THEN v_o.provider_ref IS NOT DISTINCT FROM v_ref_in
                   ELSE v_o.code IS NOT DISTINCT FROM v_code_in END;
    v_now := pg_catalog.clock_timestamp();
    IF v_o.state = 'attempting'
       OR (v_o.state = 'uncertain' AND v_out = 'accepted') THEN  -- late acceptance kept
        UPDATE {schema}.m05_outbound_ops AS o
           SET state = v_out,
               provider_ref = CASE WHEN v_out = 'accepted' THEN v_ref_in ELSE o.provider_ref END,
               code = CASE WHEN v_out = 'accepted' THEN o.code ELSE v_code_in END,
               correlation_open = (v_out <> 'refused'),
               updated_at = v_now
         WHERE o.op_id = v_op RETURNING * INTO v_o;
        RETURN """ + _OP_RESULT.format(outcome="'recorded'", attempt="v_o.attempt_id::text") + r""";
    END IF;
    IF v_o.state = v_out AND v_same THEN
        RETURN """ + _OP_RESULT.format(outcome="'existing'", attempt="v_o.attempt_id::text") + r""";
    END IF;
    IF v_o.state IN ('registered', 'accepted_unregistered') AND v_out = 'accepted' AND v_same THEN
        RETURN """ + _OP_RESULT.format(outcome="'existing'", attempt="v_o.attempt_id::text") + r""";
    END IF;
    IF v_o.state IN ('registered', 'refused', 'abandoned', 'accepted_unregistered') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STATE_TERMINAL';
    END IF;
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'OUTCOME_CONFLICT';
END;
"""
)

# --- t051 expire (bookkeeping; never waits, never grants another attempt) -------------------

_EXPIRE_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_limit integer;
    v_n integer;
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
    WITH due AS (
        SELECT o.op_id FROM {schema}.m05_outbound_ops AS o
         WHERE o.state = 'attempting'
           AND o.lease_until OPERATOR(pg_catalog.<) pg_catalog.clock_timestamp()
         ORDER BY o.lease_until, o.op_id LIMIT v_limit
           FOR UPDATE SKIP LOCKED)
    UPDATE {schema}.m05_outbound_ops AS o
       SET state = 'uncertain', code = 'LEASE_EXPIRED', updated_at = pg_catalog.clock_timestamp()
      FROM due WHERE o.op_id = due.op_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'expired', v_n);
END;
"""
)

# --- t052 register (D1 verify, then t039 in the same transaction) ---------------------------

_REGISTER_BODY: Final = (
    "\nDECLARE" + _DECLARE_OP + r"""
    v_w jsonb;
    v_result jsonb;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'op_id', 'schema_version']::text[]
""" + _string_field("op_id", "c_uuid") + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
""" + _lock_op("NOT_ACCEPTED") + r"""
    IF v_o.state = 'registered' THEN  -- pin 4: a stored success wins on retry
        RETURN v_o.registered_result
            OPERATOR(pg_catalog.||) pg_catalog.jsonb_build_object('outcome', 'existing');
    END IF;
    IF v_o.state = 'accepted_unregistered' THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'held',
            'op_id', v_o.op_id::text, 'state', v_o.state, 'code', v_o.code);
    END IF;
    IF v_o.state <> 'accepted' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NOT_ACCEPTED';
    END IF;
""" + _D1 + r"""
    v_now := pg_catalog.clock_timestamp();
    IF v_code IS NOT NULL THEN  -- never remap: hold with the code; correlation stays open
        UPDATE {schema}.m05_outbound_ops AS o
           SET state = 'accepted_unregistered', code = v_code, updated_at = v_now
         WHERE o.op_id = v_op RETURNING * INTO v_o;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'held',
            'op_id', v_o.op_id::text, 'state', v_o.state, 'code', v_o.code);
    END IF;
    v_w := {schema}.m05_register_dispatch(pg_catalog.jsonb_build_object(
        'schema_version', 1, 'reminder_id', v_o.reminder_id::text,
        'appointment_ref', v_o.appointment_ref::text, 'attribution', v_attr));
    -- R1 defensive: only a window created now, on the frozen identity, registers the op.
    IF (v_w OPERATOR(pg_catalog.->>) 'outcome') IS DISTINCT FROM 'registered'
       OR (v_w OPERATOR(pg_catalog.->>) 'reminder_id') IS DISTINCT FROM v_o.reminder_id::text
       OR (v_w OPERATOR(pg_catalog.->>) 'appointment_ref')
          IS DISTINCT FROM v_o.appointment_ref::text
       OR (v_w OPERATOR(pg_catalog.->>) 'patient_id') IS DISTINCT FROM v_o.patient_id::text
       OR (v_w OPERATOR(pg_catalog.->>) 'phone') IS DISTINCT FROM v_o.phone::text THEN
        UPDATE {schema}.m05_outbound_ops AS o
           SET state = 'accepted_unregistered', code = 'WINDOW_MISMATCH', updated_at = v_now
         WHERE o.op_id = v_op RETURNING * INTO v_o;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'held',
            'op_id', v_o.op_id::text, 'state', v_o.state, 'code', v_o.code);
    END IF;
    v_result := pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'registered',
        'op_id', v_o.op_id::text, 'state', 'registered', 'code', NULL,
        'reminder_id', v_w OPERATOR(pg_catalog.->>) 'reminder_id',
        'appointment_ref', v_w OPERATOR(pg_catalog.->>) 'appointment_ref',
        'patient_id', v_w OPERATOR(pg_catalog.->>) 'patient_id',
        'sent_at', v_w OPERATOR(pg_catalog.->>) 'sent_at',
        'event_id', v_w OPERATOR(pg_catalog.->>) 'event_id',
        'window_outcome', v_w OPERATOR(pg_catalog.->>) 'outcome');
    UPDATE {schema}.m05_outbound_ops AS o
       SET state = 'registered', correlation_open = false, registered_result = v_result,
           updated_at = v_now
     WHERE o.op_id = v_op;
    RETURN v_result;
END;
"""
)

# --- t053 inbound v2: t040 verbatim + the D3 guard ------------------------------------------

_CANDIDATE_TAIL: Final = (
    "               AND a.start_at OPERATOR(pg_catalog.>) v_received;\n"
)
_GUARDED_TAIL: Final = (
    "               AND a.start_at OPERATOR(pg_catalog.>) v_received\n"
    "               -- D3 (pin 1): no candidate while a potentially-sent op on this frozen\n"
    "               -- phone is unresolved; the reply then takes t040's own unclear path.\n"
    "               AND NOT EXISTS (SELECT 1 FROM {schema}.m05_outbound_ops AS o\n"
    "                                WHERE o.phone = v_phone AND o.correlation_open);\n"
)
if _INBOUND_BODY.count(_CANDIDATE_TAIL) != 1:  # pragma: no cover - import-time guard
    raise RuntimeError("t040 candidate query not found exactly once")
_INBOUND_V2_BODY: Final = _INBOUND_BODY.replace(_CANDIDATE_TAIL, _GUARDED_TAIL)

# --- t055 read, t056 held -------------------------------------------------------------------

_READ_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_op uuid;
    v_attempt uuid;
    v_o record;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attempt_id', 'attribution', 'op_id',
                                     'schema_version']::text[]
""" + _string_field("op_id", "c_uuid") + _string_field("attempt_id", "c_uuid") + r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_attempt := (p_command OPERATOR(pg_catalog.->>) 'attempt_id')::uuid;
    SELECT * INTO v_o FROM {schema}.m05_outbound_ops AS o WHERE o.op_id = v_op;
    IF NOT FOUND OR v_o.state <> 'attempting' OR v_o.attempt_id IS DISTINCT FROM v_attempt
       OR v_o.lease_until OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CLAIM_STALE';
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'op_id', v_o.op_id::text,
        'attempt_id', v_o.attempt_id::text, 'patient_id', v_o.patient_id::text,
        'phone', v_o.phone::text, 'start_at', """ + _ts("v_o.start_at") + r""",
        'reminder_id', v_o.reminder_id::text,
        'text_sha256', pg_catalog.encode(v_o.text_sha256, 'hex'),
        'lease_until', """ + _ts("v_o.lease_until") + r""");
END;
"""
)

_HELD_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_limit integer;
    v_ops jsonb;
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
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'op_id', h.op_id::text, 'appointment_ref', h.appointment_ref::text,
               'state', h.state, 'code', h.code,
               'created_at', """ + _ts("h.created_at") + r""",
               'updated_at', """ + _ts("h.updated_at") + r""")
               ORDER BY h.updated_at DESC, h.op_id), '[]'::jsonb)
      INTO v_ops
      FROM (SELECT o.op_id, o.appointment_ref, o.state, o.code, o.created_at, o.updated_at
              FROM {schema}.m05_outbound_ops AS o
             WHERE o.state IN ('uncertain', 'accepted_unregistered', 'refused')
             ORDER BY o.updated_at DESC, o.op_id LIMIT v_limit) AS h;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'ops', v_ops);
END;
"""
)

# (migration id, function, volatility, body); every one is runtime-executable.
_FUNCTIONS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("t048_m05_outbound_prepare", "m05_outbound_prepare", "volatile", _PREPARE_BODY),
    ("t049_m05_outbound_claim", "m05_outbound_claim", "volatile", _CLAIM_BODY),
    ("t050_m05_outbound_outcome", "m05_outbound_outcome", "volatile", _OUTCOME_BODY),
    ("t051_m05_outbound_expire", "m05_outbound_expire", "volatile", _EXPIRE_BODY),
    ("t052_m05_register_outbound", "m05_register_outbound", "volatile", _REGISTER_BODY),
    ("t053_m05_register_inbound_v2", "m05_register_inbound_v2", "volatile", _INBOUND_V2_BODY),
    ("t055_m05_outbound_read", "m05_outbound_read", "volatile", _READ_BODY),
    ("t056_m05_outbound_held", "m05_outbound_held", "volatile", _HELD_BODY),
)

_TYPED: Final = {mid: _definition(name, _COMMAND, vol, body, True)
                 for mid, name, vol, body in _FUNCTIONS}

_ALL: Final[dict[str, str | UnitDefinition]] = {
    T047_MIGRATION_ID: T047_SQL,
    **_TYPED,
    T054_MIGRATION_ID: UnitDefinition(template="", kind=ACL_CLOSURE_KIND,
                                      execution_role=M05_OWNER_ROLE),
}
SH_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
SH_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    mid: _profile(name, _COMMAND, vol, True) for mid, name, vol, _body in _FUNCTIONS
})
SH_ACL_AMENDMENTS: Final[tuple[AclAmendment, ...]] = (
    AclAmendment(
        target_unit=T040_MIGRATION_ID,
        activated_by=T054_MIGRATION_ID,
        removes=((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),),
    ),
)
