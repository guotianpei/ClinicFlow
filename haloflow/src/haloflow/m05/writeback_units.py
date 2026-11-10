"""L-1S package S-I: synthetic EHR confirmation write-back, units t076..t093 (TEST registry
only; NOT composed; the production target stays 36; TEST target 94).

Governing: S-I investigation v1 + v1r1 + v1r2, red-test packet v1 (Work Session
2026-11-10), Codex Option A and red-test pins (relay 20261110T134528Z), owner decision C5
(2026-11-10), Codex RED GATE PASS at e9c3fff (relay 20261110T151612Z). Every unit is
additive; t001..t075 are byte-unchanged.

- t076  migrator: ``provider_ref`` / ``department_ref`` on the appointment profile.
- t077  ``m05_directory_v3``: the t063 body with the two identity keys (string or null,
  <= 64), written in the SAME version-bumping upsert.
- t078  remove-only closure: runtime EXECUTE leaves ``m05_directory_v2`` (t063).
- t079  migrator: baselines (INSERT + SELECT only), write-back ops, permits (at most one
  LIVE permit per op; voided permits are kept as history and never spendable).
- t080  ``m05_writeback_capture_baseline`` (T_cap): the t071 lock order, then the ticket.
- t081  ``m05_writeback_discover``: one op per recorded CONFIRM reply of a reminder op.
- t082  ``m05_writeback_due``: check and reconcile phases, read only.
- t083  ``m05_writeback_claim``: the fenced check claim; voids an older live unspent permit;
  a spent permit is ``not_claimable`` / ``PERMIT_SPENT``.
- t084  ``m05_writeback_claim_reconcile``: only a spent, unsettled permit, only after
  apply_deadline + RECONCILE_MARGIN_S in clock_timestamp(); closes the permit.
- t085  ``m05_writeback_decide``: the WB2 matrix against the frozen baseline (precedence:
  no baseline > baseline unknown > identity / schedule / replacement / cancelled >
  provider or department unverifiable > status); R1 failures 60 / 300 / 900 s, then
  ``ehr_unreachable``. An item MINTS AND STORES the op's event id once.
- t086  ``m05_writeback_spend``: one conditional UPDATE; apply_deadline :=
  clock_timestamp() + WRITE_TIMEOUT_S.
- t087  ``m05_writeback_settle``: fenced; observed_positive only for a positive, fully
  matching read; otherwise a staff item.
- t088  migrator: the check event type / contract / allow-list, the five-kind item CHECKs,
  the resolution contract v2 (five kinds) and its allow-list. v1 rows unchanged.
- t089  ``m05_create_item_v2``: the t014 body with the fifth kind; a new
  ``ehr_confirmation_check`` item is recorded on its write-back op (same transaction).
- t090 / t091  ``m05_prepare_action_v3`` / ``m05_apply_action_v3``: the v2 bodies; apply
  accepts a resolution observation at contract v2 for every kind and at v1 for the four
  older kinds.
- t092  ``m05_followup_reason`` (STABLE staff read): the reason of a check item, read
  from its own check event's allow-listed ``reason`` path; anything else ITEM_NOT_FOUND.
- t093  remove-only closure: runtime EXECUTE leaves t014, t030 and t031.
- t094  ``m05_writeback_create_settled_item`` (Codex 20261110T155135Z): the recovery
  create for a SETTLEMENT's staff item, whose statement is not activation-protected (a
  permit spent before the switch went off still reaches staff, SI-R12). Only for the op's
  own settlement: the op locked in state ``item`` at the caller's fence, its live SPENT
  permit locked, the op's stored event which must be its canonical check event, no item
  yet; then the t089 create (rules, digest, op link) in the same transaction. The
  ordinary create key stays protected.

Lock order (extends the H2 global order): settings -> contact -> appointment -> profile ->
outbound op -> ticket (capture); write-back op -> permit (claim / decide / spend / settle).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.installed_state import AclAmendment
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import (
    ACL_CLOSURE_KIND,
    UnitDefinition,
    UnitDefinitions,
)
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05 import gateways as _gw
from haloflow.m05.gateways_v2 import _APPLY_V2_BODY, _PREPARE_V2_BODY
from haloflow.m05.intake_units import (
    _ATTRIBUTION,
    _DECLARE_COMMON,
    _TENANT,
    _contract,
    _string_field,
    _ts,
    _workload,
)
from haloflow.m05.reminder_units import (
    _DECLARE_H2,
    _DIR_KEYS_NEW,
    _DIRECTORY_V2_BODY,
    _LOCK_CHAIN,
    _definition,
    _profile,
)

_S: Final = "{schema}"
WRITE_TIMEOUT_S: Final = 10
RECONCILE_MARGIN_S: Final = 30
RETRY_DELAYS_S: Final = (60, 300, 900)
REASONS: Final = ("status_changed", "declined_or_unknown", "ehr_unreachable",
                  "could_not_verify", "no_baseline")
ITEM_KIND: Final = "ehr_confirmation_check"
T_CHECK: Final = "m05_writeback_check_raised"
ITEM_KINDS: Final = ("no_reply", "reschedule_request", "cancel_request", "unclear_reply",
                     ITEM_KIND)
_KINDS_SQL: Final = ", ".join(f"'{k}'" for k in ITEM_KINDS)
DIRECTORY_CLOSURE_ID: Final = "t078_m05_directory_v2_closure"
RUNTIME_CLOSURE_ID: Final = "t093_m05_si_runtime_closure"


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:  # pragma: no cover - import guard
        raise RuntimeError(f"S-I edit anchor not found exactly once: {old[:60]!r}")
    return text.replace(old, new)


def _refused(code: str) -> str:
    return (f"RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'refused',"
            f" 'code', {code});")


# --- t076 / t079 / t088 migrator units ----------------------------------------------------

T076_SQL: Final = r"""
-- t076:step:columns
ALTER TABLE {schema}.m05_appointment_profile
    ADD COLUMN provider_ref text
        CHECK (provider_ref IS NULL OR char_length(provider_ref) <= 64),
    ADD COLUMN department_ref text
        CHECK (department_ref IS NULL OR char_length(department_ref) <= 64);
"""

T079_SQL: Final = r"""
-- t079:step:tables
CREATE TABLE {schema}.m05_writeback_baselines (
    op_id uuid PRIMARY KEY REFERENCES {schema}.m05_outbound_ops (op_id),
    attempt_id uuid NOT NULL,
    appointment_ref uuid NOT NULL,
    start_at timestamptz NOT NULL,
    confirmation varchar(16),
    provider_ref text,
    department_ref text,
    source_version bigint NOT NULL,
    captured_at timestamptz NOT NULL
);
CREATE TABLE {schema}.m05_writeback_ops (
    wb_op_id uuid PRIMARY KEY,
    inbound_id uuid NOT NULL UNIQUE,
    op_id uuid NOT NULL UNIQUE REFERENCES {schema}.m05_outbound_ops (op_id),
    appointment_ref uuid NOT NULL,
    patient_id uuid NOT NULL,
    state varchar(24) NOT NULL CHECK (state IN ('pending', 'eligible_check', 'permitted',
        'spent', 'observed_positive', 'item', 'skipped')),
    fence integer NOT NULL CHECK (fence >= 0),
    lease_until timestamptz,
    r1_failures integer NOT NULL DEFAULT 0 CHECK (r1_failures >= 0),
    next_at timestamptz,
    reason varchar(32) CHECK (reason IS NULL OR reason IN ('status_changed',
        'declined_or_unknown', 'ehr_unreachable', 'could_not_verify', 'no_baseline')),
    event_id uuid UNIQUE,
    item_id uuid,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT m05_writeback_ops_item CHECK ((state = 'item') = (reason IS NOT NULL)
        AND (state = 'item') = (event_id IS NOT NULL))
);
CREATE INDEX m05_writeback_ops_due ON {schema}.m05_writeback_ops (created_at, wb_op_id)
    WHERE state IN ('pending', 'eligible_check', 'permitted', 'spent');
CREATE TABLE {schema}.m05_writeback_permits (
    permit_id uuid PRIMARY KEY,
    wb_op_id uuid NOT NULL REFERENCES {schema}.m05_writeback_ops (wb_op_id),
    issued_fence integer NOT NULL CHECK (issued_fence >= 1),
    r1_digest bytea NOT NULL CHECK (octet_length(r1_digest) = 32),
    apply_deadline timestamptz,
    spent_at timestamptz,
    spent_fence integer,
    voided_at timestamptz,
    closed_at timestamptz,
    created_at timestamptz NOT NULL,
    CONSTRAINT m05_writeback_permits_spent CHECK ((spent_at IS NULL) = (apply_deadline IS NULL)
        AND (spent_at IS NULL) = (spent_fence IS NULL)),
    CONSTRAINT m05_writeback_permits_void CHECK (voided_at IS NULL OR spent_at IS NULL)
);
CREATE UNIQUE INDEX m05_writeback_permits_live ON {schema}.m05_writeback_permits (wb_op_id)
    WHERE voided_at IS NULL;

-- t079:step:runtime_privileges
REVOKE ALL ON {schema}.m05_writeback_baselines, {schema}.m05_writeback_ops,
    {schema}.m05_writeback_permits FROM haloflow_runtime;

-- t079:step:grants
GRANT SELECT, INSERT ON {schema}.m05_writeback_baselines TO haloflow_m05_owner;
GRANT SELECT, INSERT,
    UPDATE (state, fence, lease_until, r1_failures, next_at, reason, event_id, item_id,
        updated_at)
    ON {schema}.m05_writeback_ops TO haloflow_m05_owner;
GRANT SELECT, INSERT,
    UPDATE (apply_deadline, spent_at, spent_fence, voided_at, closed_at)
    ON {schema}.m05_writeback_permits TO haloflow_m05_owner;
"""

_CHECK_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":["wb_op_id","appointment_ref","reason"],'
    '"properties":{"wb_op_id":{"type":"string"},"appointment_ref":{"type":"string"},'
    '"reason":{"enum":[' + ",".join(f'"{r}"' for r in REASONS) + "]}}}"
)
_RESOLUTION_V2_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":["outcome_code","assertion_kind","item_kind"],'
    '"properties":{'
    '"outcome_code":{"enum":["rescheduled","cancelled_in_ehr","reached_no_change",'
    '"no_contact_left_message","no_contact_no_answer","other"]},'
    '"assertion_kind":{"enum":["staff"]},'
    '"item_kind":{"enum":[' + ",".join(f'"{k}"' for k in ITEM_KINDS) + "]}"
    "}}"
)

T088_SQL: Final = (
    r"""
-- t088:step:check_contract
-- The check event type, INACTIVE (the S-I test profile activates it).
INSERT INTO {schema}.event_types (event_type, action_family, is_active) VALUES
    ('m05_writeback_check_raised', 'm05_intake', false);
"""
    + _contract(T_CHECK, _CHECK_SCHEMA)
    + r"""
-- t088:step:check_allowlist
INSERT INTO {schema}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
    ('m05', 'm05_writeback_check_raised', 1, 'appointment_ref'),
    ('m05', 'm05_writeback_check_raised', 1, 'reason'),
    ('m05', 'm05_writeback_check_raised', 1, 'wb_op_id');

-- t088:step:item_kinds
ALTER TABLE {schema}.m05_intake_map DROP CONSTRAINT m05_intake_map_item_kind_check,
    ADD CONSTRAINT m05_intake_map_item_kind_check CHECK (item_kind IN (@@KINDS@@));
ALTER TABLE {schema}.m05_followup_items DROP CONSTRAINT m05_followup_items_item_kind_check,
    ADD CONSTRAINT m05_followup_items_item_kind_check CHECK (item_kind IN (@@KINDS@@));

-- t088:step:resolution_contract_v2
INSERT INTO {schema}.event_contracts (
    event_type, version, action_family, event_level, allowed_statuses, terminality,
    compatible_outcome_class, precedence_rank, required_fields, required_links,
    optional_links, metadata_json_schema, metadata_max_bytes, authorized_producers,
    required_provenance, correction_policy, projection_rule_id, effective_from,
    retired_at, signal_rule, value_constraints
) VALUES (
    'm05_followup_resolution_asserted', 2, 'm05_followup', 'observation',
    ARRAY['completed']::varchar(64)[], true, 'observation', 100,
    ARRAY['correlation_id']::varchar(64)[],
    '[["m05_followup_item_v1"]]'::jsonb,
    '[]'::jsonb,
    '@@RESOLUTION_SCHEMA@@'::jsonb,
    1024,
    ARRAY['m05_followup']::varchar(64)[],
    ARRAY[]::varchar(64)[],
    NULL, NULL, '2026-11-10T00:00:00Z', NULL,
    '{"handoff_kind":"event_signal","signal_type":"m02.event.appended"}'::jsonb,
    '{"operation_id":{"must_be_null":true}}'::jsonb
);

-- t088:step:resolution_allowlist_v2
INSERT INTO {schema}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
    ('m05', 'm05_followup_resolution_asserted', 2, 'outcome_code'),
    ('m05', 'm05_followup_resolution_asserted', 2, 'assertion_kind'),
    ('m05', 'm05_followup_resolution_asserted', 2, 'item_kind');
"""
).replace("@@KINDS@@", _KINDS_SQL).replace("@@RESOLUTION_SCHEMA@@", _RESOLUTION_V2_SCHEMA)

# --- t077 directory v3 --------------------------------------------------------------------

_DIR_KEYS_V3: Final = (
    "        IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'confirmation',\n"
    "                                         'department_ref', 'department_tz', 'first_name',\n"
    "                                         'op', 'patient_id', 'provider_ref',\n"
    "                                         'schema_version', 'source_status', 'start_at',\n"
    "                                         'status']::text[]\n"
    + "".join(
        f"           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) '{f}')"
        " NOT IN ('string', 'null')\n"
        f"           OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) '{f}') > 64\n"
        for f in ("first_name", "confirmation", "department_tz", "source_status",
                  "provider_ref", "department_ref"))
    + "           OR (p_command OPERATOR(pg_catalog.->>) 'confirmation')\n"
    "              NOT IN ('confirmed', 'not_confirmed', 'unknown')\n"
)
_DIRECTORY_V3_BODY: Final = _replace_once(_replace_once(_replace_once(_replace_once(
    _DIRECTORY_V2_BODY, _DIR_KEYS_NEW, _DIR_KEYS_V3),
    "confirmation, department_tz, source_status, source_version, updated_at)",
    "confirmation, department_tz, source_status, provider_ref, department_ref,\n"
    "            source_version, updated_at)"),
    "p_command OPERATOR(pg_catalog.->>) 'source_status', 1,",
    "p_command OPERATOR(pg_catalog.->>) 'source_status',\n"
    "            p_command OPERATOR(pg_catalog.->>) 'provider_ref',\n"
    "            p_command OPERATOR(pg_catalog.->>) 'department_ref', 1,"),
    "source_status = EXCLUDED.source_status,",
    "source_status = EXCLUDED.source_status,\n"
    "               provider_ref = EXCLUDED.provider_ref,\n"
    "               department_ref = EXCLUDED.department_ref,")

# --- shared fragments ---------------------------------------------------------------------

_C_TS: Final = r"""
    c_ts constant text := '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$';
"""


def _uuid_field(name: str) -> str:
    return _string_field(name, "c_uuid")


def _int_field(name: str) -> str:
    return (
        f"       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) '{name}')"
        " IS DISTINCT FROM 'number'\n"
        f"       OR (p_command OPERATOR(pg_catalog.->>) '{name}')"
        " OPERATOR(pg_catalog.!~) '^[0-9]{1,9}$'\n"
    )


def _keys(*names: str) -> str:
    listed = ", ".join(f"'{n}'" for n in sorted((*names, "attribution", "schema_version")))
    return f"    IF v_keys IS DISTINCT FROM ARRAY[{listed}]::text[]\n"


_MALFORMED: Final = r"""    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
"""

# Validates the typed read object in v_read (JSON object or null); sets v_r_start.
_READ_SHAPE: Final = r"""
    IF v_read IS NOT NULL THEN
        IF pg_catalog.jsonb_typeof(v_read) IS DISTINCT FROM 'object'
           OR (SELECT pg_catalog.array_agg(k ORDER BY k)
                 FROM pg_catalog.jsonb_object_keys(v_read) AS k)
              IS DISTINCT FROM ARRAY['appointment_id', 'department_ref', 'provider_ref',
                                     'replaced_by', 'start_at', 'status']::text[]
           OR pg_catalog.jsonb_typeof(v_read OPERATOR(pg_catalog.->) 'status')
              IS DISTINCT FROM 'string'
           OR (v_read OPERATOR(pg_catalog.->>) 'status') NOT IN ('positive', 'neutral',
              'negative', 'unknown', 'empty', 'checked_in', 'checked_out', 'cancelled')
           OR pg_catalog.jsonb_typeof(v_read OPERATOR(pg_catalog.->) 'appointment_id')
              IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_read OPERATOR(pg_catalog.->>) 'appointment_id')
              NOT BETWEEN 1 AND 128
           OR pg_catalog.jsonb_typeof(v_read OPERATOR(pg_catalog.->) 'start_at')
              IS DISTINCT FROM 'string'
           OR (v_read OPERATOR(pg_catalog.->>) 'start_at') OPERATOR(pg_catalog.!~) c_ts
           OR pg_catalog.jsonb_typeof(v_read OPERATOR(pg_catalog.->) 'provider_ref')
              NOT IN ('string', 'null')
           OR pg_catalog.char_length(v_read OPERATOR(pg_catalog.->>) 'provider_ref') > 128
           OR pg_catalog.jsonb_typeof(v_read OPERATOR(pg_catalog.->) 'department_ref')
              NOT IN ('string', 'null')
           OR pg_catalog.char_length(v_read OPERATOR(pg_catalog.->>) 'department_ref') > 128
           OR pg_catalog.jsonb_typeof(v_read OPERATOR(pg_catalog.->) 'replaced_by')
              NOT IN ('string', 'null')
           OR pg_catalog.char_length(v_read OPERATOR(pg_catalog.->>) 'replaced_by') > 128 THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        BEGIN
            v_r_start := pg_catalog.timezone('UTC',
                (pg_catalog.rtrim(v_read OPERATOR(pg_catalog.->>) 'start_at', 'Z'))::timestamp);
        EXCEPTION WHEN data_exception THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END;
    END IF;
"""

# The identity part of WB2 against the baseline v_b and the read v_read (not null).
# Sets v_reason to 'status_changed' or 'could_not_verify', or leaves it NULL.
_IDENTITY: Final = r"""
    v_status := v_read OPERATOR(pg_catalog.->>) 'status';
    v_r_provider := v_read OPERATOR(pg_catalog.->>) 'provider_ref';
    v_r_department := v_read OPERATOR(pg_catalog.->>) 'department_ref';
    IF (v_read OPERATOR(pg_catalog.->>) 'appointment_id')
           IS DISTINCT FROM v_b.appointment_ref::text
       OR (v_read OPERATOR(pg_catalog.->>) 'replaced_by') IS NOT NULL
       OR v_r_start IS DISTINCT FROM v_b.start_at
       OR v_status = 'cancelled'
       OR (v_b.provider_ref IS NOT NULL AND v_r_provider IS NOT NULL
           AND v_r_provider IS DISTINCT FROM v_b.provider_ref)
       OR (v_b.department_ref IS NOT NULL AND v_r_department IS NOT NULL
           AND v_r_department IS DISTINCT FROM v_b.department_ref) THEN
        v_reason := 'status_changed';
    ELSIF v_b.provider_ref IS NULL OR v_b.department_ref IS NULL
          OR v_r_provider IS NULL OR v_r_department IS NULL THEN
        v_reason := 'could_not_verify';
    END IF;
"""

_DECLARE_WB: Final = _DECLARE_COMMON + _C_TS + r"""
    v_wb uuid;
    v_w record;
    v_b record;
    v_p record;
    v_fence integer;
    v_permit uuid;
    v_read jsonb;
    v_r_start timestamptz;
    v_r_provider text;
    v_r_department text;
    v_status text;
    v_reason text;
    v_event uuid;
    v_now timestamptz;
"""

# Load the op FOR UPDATE into v_w (NOT FOUND: the caller decides).
_LOCK_WB: Final = r"""
    v_wb := (p_command OPERATOR(pg_catalog.->>) 'wb_op_id')::uuid;
    SELECT * INTO v_w FROM {schema}.m05_writeback_ops AS w WHERE w.wb_op_id = v_wb
       FOR UPDATE;
"""

# The item terminal: mint and store the event id ONCE, return the decision.
_ITEM_RESULT: Final = r"""
        v_now := pg_catalog.clock_timestamp();
        v_event := COALESCE(v_w.event_id, pg_catalog.gen_random_uuid());
        UPDATE {schema}.m05_writeback_ops AS w
           SET state = 'item', reason = v_reason, event_id = v_event, lease_until = NULL,
               next_at = NULL, updated_at = v_now
         WHERE w.wb_op_id = v_wb;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'item',
            'permit_id', NULL, 'reason', v_reason, 'event_id', v_event::text,
            'patient_id', v_w.patient_id::text, 'wb_op_id', v_wb::text,
            'appointment_ref', v_w.appointment_ref::text,
            'occurred_at', """ + _ts("v_now") + r""");
"""


def _stale() -> str:
    return ("RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'stale',"
            " 'permit_id', NULL, 'reason', NULL, 'event_id', NULL, 'patient_id', NULL);")


# --- t080 capture -------------------------------------------------------------------------

_CAPTURE_BODY: Final = (
    "\nDECLARE" + _DECLARE_H2 + r"""
    v_attempt uuid;
    v_t record;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attempt_id', 'attribution', 'op_id',
                                     'schema_version']::text[]
""" + _uuid_field("op_id") + _uuid_field("attempt_id") + _MALFORMED + _ATTRIBUTION
    + _workload("dispatch") + r"""
    v_op := (p_command OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    v_attempt := (p_command OPERATOR(pg_catalog.->>) 'attempt_id')::uuid;
    SELECT * INTO v_i FROM {schema}.m05_outbound_render_inputs AS i WHERE i.op_id = v_op;
    IF NOT FOUND THEN  -- unknown here (another tenant's op included): before any lock
        """ + _refused("'OP_NOT_FOUND'") + r"""
    END IF;
""" + _LOCK_CHAIN + r"""
    SELECT * INTO v_t FROM {schema}.m05_outbound_send_tickets AS t WHERE t.op_id = v_op
       FOR UPDATE;
    IF NOT FOUND OR v_t.attempt_id IS DISTINCT FROM v_attempt THEN
        """ + _refused("'CLAIM_STALE'") + r"""
    END IF;
    IF v_t.consumed_at IS NOT NULL THEN
        """ + _refused("'TICKET_SPENT'") + r"""
    END IF;
    IF v_o.state IS DISTINCT FROM 'attempting' OR v_o.attempt_id IS DISTINCT FROM v_attempt THEN
        """ + _refused("'CLAIM_STALE'") + r"""
    END IF;
    IF EXISTS (SELECT 1 FROM {schema}.m05_writeback_baselines AS b WHERE b.op_id = v_op) THEN
        """ + _refused("'ALREADY_CAPTURED'") + r"""
    END IF;
    IF v_p.source_version IS NULL OR v_p.source_version IS DISTINCT FROM v_i.source_version THEN
        """ + _refused("'SOURCE_MOVED'") + r"""
    END IF;
    INSERT INTO {schema}.m05_writeback_baselines (op_id, attempt_id, appointment_ref, start_at,
        confirmation, provider_ref, department_ref, source_version, captured_at)
    VALUES (v_op, v_attempt, v_o.appointment_ref, v_o.start_at, v_p.confirmation,
        v_p.provider_ref, v_p.department_ref, v_p.source_version, pg_catalog.clock_timestamp());
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'captured',
        'code', NULL);
END;
"""
)

# --- t081 discover ------------------------------------------------------------------------

_DISCOVER_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_limit integer;
    v_count integer;
BEGIN""" + _TENANT + _keys("limit") + _int_field("limit") + _MALFORMED + _ATTRIBUTION
    + _workload("dispatch") + r"""
    v_limit := (p_command OPERATOR(pg_catalog.->>) 'limit')::integer;
    IF v_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    INSERT INTO {schema}.m05_writeback_ops (wb_op_id, inbound_id, op_id, appointment_ref,
        patient_id, state, fence, lease_until, r1_failures, next_at, reason, event_id,
        item_id, created_at, updated_at)
    SELECT pg_catalog.gen_random_uuid(), r.inbound_id, o.op_id, o.appointment_ref,
           o.patient_id, 'pending', 0, NULL, 0, NULL, NULL, NULL, NULL,
           pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp()
      FROM (SELECT r.inbound_id, r.reminder_id, r.received_at
              FROM {schema}.m05_inbound_replies AS r
             WHERE r.reply_class = 'confirm' AND r.decision = 'recorded'
               AND r.reminder_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM {schema}.m05_writeback_ops AS w
                                WHERE w.inbound_id = r.inbound_id)
             ORDER BY r.received_at, r.inbound_id LIMIT v_limit) AS r
      JOIN {schema}.m05_outbound_ops AS o ON o.reminder_id = r.reminder_id
     ORDER BY r.received_at, r.inbound_id
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'discovered', v_count);
END;
"""
)

# --- t082 due -----------------------------------------------------------------------------

_DUE_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_limit integer;
    v_now timestamptz;
    v_ops jsonb;
BEGIN""" + _TENANT + _keys("limit") + _int_field("limit") + _MALFORMED + _ATTRIBUTION
    + _workload("dispatch") + r"""
    v_limit := (p_command OPERATOR(pg_catalog.->>) 'limit')::integer;
    IF v_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_now := pg_catalog.clock_timestamp();
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'wb_op_id', d.wb_op_id::text, 'phase', d.phase)
               ORDER BY d.created_at, d.wb_op_id), '[]'::jsonb)
      INTO v_ops
      FROM (SELECT w.wb_op_id, w.created_at,
                   CASE WHEN w.state = 'spent' THEN 'reconcile' ELSE 'check' END AS phase
              FROM {schema}.m05_writeback_ops AS w
              LEFT JOIN {schema}.m05_writeback_permits AS p
                     ON p.wb_op_id = w.wb_op_id AND p.voided_at IS NULL
             WHERE (w.state = 'pending'
                    AND (w.next_at IS NULL OR w.next_at OPERATOR(pg_catalog.<=) v_now))
                OR (w.state IN ('eligible_check', 'permitted')
                    AND (w.lease_until IS NULL OR w.lease_until OPERATOR(pg_catalog.<=) v_now)
                    AND (w.next_at IS NULL OR w.next_at OPERATOR(pg_catalog.<=) v_now))
                OR (w.state = 'spent' AND p.spent_at IS NOT NULL
                    AND v_now OPERATOR(pg_catalog.>) (p.apply_deadline
                        OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => @@MARGIN@@))
                    AND (p.closed_at IS NULL OR w.lease_until IS NULL
                         OR w.lease_until OPERATOR(pg_catalog.<=) v_now))
             ORDER BY w.created_at, w.wb_op_id LIMIT v_limit) AS d;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'ops', v_ops);
END;
"""
).replace("@@MARGIN@@", str(RECONCILE_MARGIN_S))

# --- t083 claim (check phase) -------------------------------------------------------------


def _claim_result(outcome: str, code: str, phase: str, permit: str | None) -> str:
    extra = "" if permit is None else f", 'permit_id', {permit}"
    return (f"RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', '{outcome}',"
            f" 'code', {code}, 'fence', {'v_fence' if outcome == 'claimed' else 'NULL'},"
            f" 'phase', '{phase}', 'appointment_ref',"
            f" {'v_w.appointment_ref::text' if outcome == 'claimed' else 'NULL'}{extra});")


_CLAIM_SHAPE: Final = (
    _TENANT + _keys("wb_op_id", "lease_seconds") + _uuid_field("wb_op_id")
    + _int_field("lease_seconds") + _MALFORMED + _ATTRIBUTION + _workload("dispatch") + r"""
    v_lease := (p_command OPERATOR(pg_catalog.->>) 'lease_seconds')::integer;
    IF v_lease NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _LOCK_WB
)

_CLAIM_BODY: Final = (
    "\nDECLARE" + _DECLARE_WB + r"""
    v_lease integer;
BEGIN""" + _CLAIM_SHAPE + r"""
    IF NOT FOUND THEN
        """ + _claim_result("not_claimable", "'OP_NOT_FOUND'", "check", None) + r"""
    END IF;
    v_now := pg_catalog.clock_timestamp();
    IF v_w.state = 'spent' THEN
        """ + _claim_result("not_claimable", "'PERMIT_SPENT'", "check", None) + r"""
    END IF;
    IF v_w.state NOT IN ('pending', 'eligible_check', 'permitted') THEN
        """ + _claim_result("not_claimable", "'TERMINAL'", "check", None) + r"""
    END IF;
    IF v_w.next_at IS NOT NULL AND v_w.next_at OPERATOR(pg_catalog.>) v_now THEN
        """ + _claim_result("not_due", "'NOT_DUE'", "check", None) + r"""
    END IF;
    IF v_w.state IN ('eligible_check', 'permitted') AND v_w.lease_until IS NOT NULL
       AND v_w.lease_until OPERATOR(pg_catalog.>) v_now THEN
        """ + _claim_result("not_due", "'LEASED'", "check", None) + r"""
    END IF;
    -- A live UNSPENT permit from an older fence is voided (kept as history).
    UPDATE {schema}.m05_writeback_permits AS p SET voided_at = v_now
     WHERE p.wb_op_id = v_wb AND p.voided_at IS NULL AND p.spent_at IS NULL;
    v_fence := v_w.fence OPERATOR(pg_catalog.+) 1;
    UPDATE {schema}.m05_writeback_ops AS w
       SET state = 'eligible_check', fence = v_fence,
           lease_until = v_now OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => v_lease),
           next_at = NULL, updated_at = v_now
     WHERE w.wb_op_id = v_wb;
    """ + _claim_result("claimed", "NULL", "check", None) + r"""
END;
"""
)

# --- t084 claim (reconcile phase; NOT activation-protected) -------------------------------

_CLAIM_RECONCILE_BODY: Final = (
    "\nDECLARE" + _DECLARE_WB + r"""
    v_lease integer;
    v_pr record;
BEGIN""" + _CLAIM_SHAPE + r"""
    IF NOT FOUND THEN
        """ + _claim_result("not_claimable", "'OP_NOT_FOUND'", "reconcile", "NULL") + r"""
    END IF;
    IF v_w.state IS DISTINCT FROM 'spent' THEN
        """ + _claim_result("not_claimable", "'NOT_SPENT'", "reconcile", "NULL") + r"""
    END IF;
    SELECT * INTO v_pr FROM {schema}.m05_writeback_permits AS p
     WHERE p.wb_op_id = v_wb AND p.voided_at IS NULL AND p.spent_at IS NOT NULL
       FOR UPDATE;
    IF NOT FOUND THEN
        """ + _claim_result("not_claimable", "'NOT_SPENT'", "reconcile", "NULL") + r"""
    END IF;
    v_now := pg_catalog.clock_timestamp();  -- database wall time, never now()
    IF NOT v_now OPERATOR(pg_catalog.>) (v_pr.apply_deadline
           OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => @@MARGIN@@)) THEN
        """ + _claim_result("not_claimable", "'RECONCILE_NOT_DUE'", "reconcile", "NULL") + r"""
    END IF;
    IF v_pr.closed_at IS NOT NULL AND v_w.lease_until IS NOT NULL
       AND v_w.lease_until OPERATOR(pg_catalog.>) v_now THEN
        """ + _claim_result("not_due", "'LEASED'", "reconcile", "NULL") + r"""
    END IF;
    UPDATE {schema}.m05_writeback_permits AS p SET closed_at = COALESCE(p.closed_at, v_now)
     WHERE p.permit_id = v_pr.permit_id;
    v_fence := v_w.fence OPERATOR(pg_catalog.+) 1;
    UPDATE {schema}.m05_writeback_ops AS w
       SET fence = v_fence,
           lease_until = v_now OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => v_lease),
           updated_at = v_now
     WHERE w.wb_op_id = v_wb;
    """ + _claim_result("claimed", "NULL", "reconcile", "v_pr.permit_id::text") + r"""
END;
"""
).replace("@@MARGIN@@", str(RECONCILE_MARGIN_S))

# --- t085 decide --------------------------------------------------------------------------

_DECIDE_BODY: Final = (
    "\nDECLARE" + _DECLARE_WB + r"""
    v_failed boolean;
    v_delay integer;
BEGIN""" + _TENANT + _keys("wb_op_id", "fence", "read", "read_failed")
    + _uuid_field("wb_op_id") + _int_field("fence") + r"""
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'read_failed')
          IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'read')
          NOT IN ('object', 'null')
""" + _MALFORMED + _ATTRIBUTION + _workload("dispatch") + r"""
    v_fence := (p_command OPERATOR(pg_catalog.->>) 'fence')::integer;
    v_failed := (p_command OPERATOR(pg_catalog.->) 'read_failed') = 'true'::jsonb;
    v_read := p_command OPERATOR(pg_catalog.->) 'read';
    IF pg_catalog.jsonb_typeof(v_read) = 'null' THEN
        v_read := NULL;
    END IF;
    IF v_failed = (v_read IS NOT NULL) THEN  -- exactly one of: a read, or a failed read
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _READ_SHAPE + _LOCK_WB + r"""
    IF NOT FOUND OR v_w.fence IS DISTINCT FROM v_fence OR v_w.state <> 'eligible_check'
       OR v_w.lease_until IS NULL
       OR v_w.lease_until OPERATOR(pg_catalog.<=) pg_catalog.clock_timestamp() THEN
        """ + _stale() + r"""
    END IF;
    SELECT * INTO v_b FROM {schema}.m05_writeback_baselines AS b WHERE b.op_id = v_w.op_id;
    IF NOT FOUND THEN
        v_reason := 'no_baseline';
    ELSIF v_failed THEN
        v_delay := CASE v_w.r1_failures WHEN 0 THEN @@D1@@ WHEN 1 THEN @@D2@@
                                        WHEN 2 THEN @@D3@@ ELSE NULL END;
        IF v_delay IS NOT NULL THEN
            v_now := pg_catalog.clock_timestamp();
            UPDATE {schema}.m05_writeback_ops AS w
               SET state = 'pending', r1_failures = w.r1_failures OPERATOR(pg_catalog.+) 1,
                   next_at = v_now OPERATOR(pg_catalog.+)
                       pg_catalog.make_interval(secs => v_delay),
                   lease_until = NULL, updated_at = v_now
             WHERE w.wb_op_id = v_wb;
            RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'deferred',
                'permit_id', NULL, 'reason', NULL, 'event_id', NULL, 'patient_id', NULL);
        END IF;
        UPDATE {schema}.m05_writeback_ops AS w
           SET r1_failures = w.r1_failures OPERATOR(pg_catalog.+) 1
         WHERE w.wb_op_id = v_wb;
        v_reason := 'ehr_unreachable';
    ELSIF v_b.confirmation IS NULL OR v_b.confirmation = 'unknown' THEN
        v_reason := 'declined_or_unknown';
    ELSE
""" + _IDENTITY.replace("\n    ", "\n        ") + r"""
        IF v_reason IS NULL THEN
            IF v_status IN ('positive', 'checked_in', 'checked_out') THEN
                v_now := pg_catalog.clock_timestamp();
                UPDATE {schema}.m05_writeback_ops AS w
                   SET state = 'skipped', lease_until = NULL, updated_at = v_now
                 WHERE w.wb_op_id = v_wb;
                RETURN pg_catalog.jsonb_build_object('schema_version', 1,
                    'outcome', 'skipped', 'permit_id', NULL, 'reason', NULL,
                    'event_id', NULL, 'patient_id', NULL);
            ELSIF v_status IN ('negative', 'unknown', 'empty') THEN
                v_reason := 'declined_or_unknown';
            END IF;
        END IF;
    END IF;
    IF v_reason IS NOT NULL THEN
""" + _ITEM_RESULT + r"""
    END IF;
    -- neutral, fully matching: ONE new permit bound to this fence and this read.
    v_now := pg_catalog.clock_timestamp();
    v_permit := pg_catalog.gen_random_uuid();
    INSERT INTO {schema}.m05_writeback_permits (permit_id, wb_op_id, issued_fence, r1_digest,
        apply_deadline, spent_at, spent_fence, voided_at, closed_at, created_at)
    VALUES (v_permit, v_wb, v_fence,
        pg_catalog.sha256(pg_catalog.convert_to(v_read::text, 'UTF8')),
        NULL, NULL, NULL, NULL, NULL, v_now);
    UPDATE {schema}.m05_writeback_ops AS w SET state = 'permitted', updated_at = v_now
     WHERE w.wb_op_id = v_wb;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'permitted',
        'permit_id', v_permit::text, 'reason', NULL, 'event_id', NULL, 'patient_id', NULL);
END;
"""
).replace("@@D1@@", str(RETRY_DELAYS_S[0])).replace("@@D2@@", str(RETRY_DELAYS_S[1])).replace(
    "@@D3@@", str(RETRY_DELAYS_S[2]))

# --- t086 spend ---------------------------------------------------------------------------

_SPEND_BODY: Final = (
    "\nDECLARE" + _DECLARE_WB + r"""
    v_deadline timestamptz;
BEGIN""" + _TENANT + _keys("wb_op_id", "fence", "permit_id") + _uuid_field("wb_op_id")
    + _int_field("fence") + _uuid_field("permit_id") + _MALFORMED + _ATTRIBUTION
    + _workload("dispatch") + r"""
    v_fence := (p_command OPERATOR(pg_catalog.->>) 'fence')::integer;
    v_permit := (p_command OPERATOR(pg_catalog.->>) 'permit_id')::uuid;
""" + _LOCK_WB + r"""
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'refused',
            'code', 'OP_NOT_FOUND', 'apply_deadline', NULL);
    END IF;
    v_now := pg_catalog.clock_timestamp();
    v_deadline := v_now OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => @@TIMEOUT@@);
    -- ONE conditional UPDATE: the permit, its fence, the op's fence, state and lease.
    UPDATE {schema}.m05_writeback_permits AS p
       SET spent_at = v_now, spent_fence = v_fence, apply_deadline = v_deadline
     WHERE p.permit_id = v_permit AND p.wb_op_id = v_wb
       AND p.spent_at IS NULL AND p.voided_at IS NULL AND p.issued_fence = v_fence
       AND v_w.fence = v_fence AND v_w.state = 'permitted'
       AND v_w.lease_until IS NOT NULL AND v_w.lease_until OPERATOR(pg_catalog.>) v_now;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'refused',
            'code', 'PERMIT_NOT_SPENDABLE', 'apply_deadline', NULL);
    END IF;
    UPDATE {schema}.m05_writeback_ops AS w SET state = 'spent', updated_at = v_now
     WHERE w.wb_op_id = v_wb;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'spent',
        'code', NULL, 'apply_deadline', """ + _ts("v_deadline") + r""");
END;
"""
).replace("@@TIMEOUT@@", str(WRITE_TIMEOUT_S))

# --- t087 settle (fenced; NOT activation-protected) ---------------------------------------

_SETTLE_BODY: Final = (
    "\nDECLARE" + _DECLARE_WB + r"""
    v_write text;
BEGIN""" + _TENANT + _keys("wb_op_id", "fence", "permit_id", "write_outcome", "read")
    + _uuid_field("wb_op_id") + _int_field("fence") + _uuid_field("permit_id") + r"""
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'write_outcome')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'write_outcome') NOT IN ('applied',
          'deadline_passed', 'permit_closed', 'not_found', 'unknown')
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'read')
          NOT IN ('object', 'null')
""" + _MALFORMED + _ATTRIBUTION + _workload("dispatch") + r"""
    v_fence := (p_command OPERATOR(pg_catalog.->>) 'fence')::integer;
    v_permit := (p_command OPERATOR(pg_catalog.->>) 'permit_id')::uuid;
    v_write := p_command OPERATOR(pg_catalog.->>) 'write_outcome';
    v_read := p_command OPERATOR(pg_catalog.->) 'read';
    IF pg_catalog.jsonb_typeof(v_read) = 'null' THEN
        v_read := NULL;
    END IF;
""" + _READ_SHAPE + _LOCK_WB + r"""
    IF NOT FOUND OR v_w.fence IS DISTINCT FROM v_fence OR v_w.state <> 'spent'
       OR NOT EXISTS (SELECT 1 FROM {schema}.m05_writeback_permits AS p
                       WHERE p.permit_id = v_permit AND p.wb_op_id = v_wb
                         AND p.voided_at IS NULL AND p.spent_at IS NOT NULL) THEN
        """ + _stale() + r"""
    END IF;
    SELECT * INTO v_b FROM {schema}.m05_writeback_baselines AS b WHERE b.op_id = v_w.op_id;
    IF NOT FOUND THEN
        v_reason := 'no_baseline';
    ELSIF v_read IS NULL THEN
        v_reason := 'could_not_verify';
    ELSE
""" + _IDENTITY.replace("\n    ", "\n        ") + r"""
        IF v_reason IS NULL AND v_status = 'positive' THEN
            v_now := pg_catalog.clock_timestamp();
            UPDATE {schema}.m05_writeback_ops AS w
               SET state = 'observed_positive', lease_until = NULL, updated_at = v_now
             WHERE w.wb_op_id = v_wb;
            RETURN pg_catalog.jsonb_build_object('schema_version', 1,
                'outcome', 'observed_positive', 'permit_id', NULL, 'reason', NULL,
                'event_id', NULL, 'patient_id', NULL);
        END IF;
        v_reason := COALESCE(v_reason, 'could_not_verify');
    END IF;
""" + _ITEM_RESULT + r"""
END;
"""
)

# --- t089 create v2 / t090, t091 actions v3 / t092 reason ---------------------------------

_V1_KINDS: Final = "('no_reply', 'reschedule_request', 'cancel_request', 'unclear_reply')"
_CREATE_CREATED_RETURN: Final = (
    "    RETURN pg_catalog.jsonb_build_object('schema_version', 1,\n"
    "        'item_id', v_item::text, 'created_or_existing', 'created');"
)
_CREATE_V2_BODY: Final = _replace_once(
    _replace_once(_gw._CREATE_BODY, _V1_KINDS, f"({_KINDS_SQL})"),
    _CREATE_CREATED_RETURN,
    "    -- S-I: a new check item is recorded on its write-back op (same transaction).\n"
    "    IF v_kind = '" + ITEM_KIND + "' THEN\n"
    "        UPDATE {schema}.m05_writeback_ops AS w SET item_id = v_item\n"
    "         WHERE w.event_id = v_source AND w.item_id IS NULL;\n"
    "    END IF;\n" + _CREATE_CREATED_RETURN)

_PREPARE_V3_BODY: Final = _PREPARE_V2_BODY
_APPLY_V3_BODY: Final = _replace_once(
    _APPLY_V2_BODY,
    "(v_env OPERATOR(pg_catalog.->) 'contract_version') IS DISTINCT FROM '1'::jsonb",
    "NOT ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '2'::jsonb\n"
    "                OR ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '1'::jsonb\n"
    "                    AND v_row.item_kind <> '" + ITEM_KIND + "'))")

_REASON_BODY: Final = r"""
DECLARE
    v_setting text;
    v_item record;
    v_env jsonb;
    v_reason text;
BEGIN""" + _gw._TENANT_CHECK + r"""
    SELECT i.item_id, i.item_kind, i.source_event_id INTO v_item
      FROM {schema}.m05_followup_items AS i
     WHERE i.item_id = p_item_id AND i.item_kind = '""" + ITEM_KIND + r"""';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ITEM_NOT_FOUND';
    END IF;
    -- The item's OWN check event: bound through its write-back op, then the envelope.
    IF NOT EXISTS (SELECT 1 FROM {schema}.m05_writeback_ops AS w
                    WHERE w.event_id = v_item.source_event_id
                      AND w.item_id = v_item.item_id) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ITEM_NOT_FOUND';
    END IF;
    v_env := {schema}.m02_get_observation_envelope(v_item.source_event_id);
    IF v_env IS NULL OR v_env OPERATOR(pg_catalog.?) 'error'
       OR (v_env OPERATOR(pg_catalog.->>) 'event_type') IS DISTINCT FROM '""" + T_CHECK + r"""'
       OR (v_env OPERATOR(pg_catalog.->) 'contract_version') IS DISTINCT FROM '1'::jsonb
       OR pg_catalog.jsonb_typeof(v_env OPERATOR(pg_catalog.->) 'metadata_paths'
              OPERATOR(pg_catalog.->) 'reason') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ITEM_NOT_FOUND';
    END IF;
    v_reason := v_env OPERATOR(pg_catalog.->) 'metadata_paths' OPERATOR(pg_catalog.->>) 'reason';
    IF v_reason NOT IN ('status_changed', 'declined_or_unknown', 'ehr_unreachable',
                        'could_not_verify', 'no_baseline') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ITEM_NOT_FOUND';
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'item_id', v_item.item_id::text,
        'reason', v_reason);
END;
"""

# --- t094 create settled item (recovery create; NOT activation-protected) ------------------

_CREATE_SETTLED_BODY: Final = (
    "\nDECLARE" + _DECLARE_WB + r"""
    v_env jsonb;
BEGIN""" + _TENANT + _keys("wb_op_id", "fence", "permit_id", "source_event_id", "item_kind")
    + _uuid_field("wb_op_id") + _int_field("fence") + _uuid_field("permit_id")
    + _uuid_field("source_event_id") + r"""
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'item_kind')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'item_kind') IS DISTINCT FROM '""" + ITEM_KIND
    + "'\n" + _MALFORMED + _ATTRIBUTION + _workload("dispatch") + r"""
    v_fence := (p_command OPERATOR(pg_catalog.->>) 'fence')::integer;
    v_permit := (p_command OPERATOR(pg_catalog.->>) 'permit_id')::uuid;
    v_event := (p_command OPERATOR(pg_catalog.->>) 'source_event_id')::uuid;
""" + _LOCK_WB + r"""
    -- Only the settlement that just raised this op's item: same op, same fence, its
    -- live SPENT permit (locked), its stored event, no item yet. Anything else (a
    -- decide-path item, a foreign permit or event, a repeat) is refused.
    IF NOT FOUND OR v_w.state IS DISTINCT FROM 'item' OR v_w.fence IS DISTINCT FROM v_fence
       OR v_w.event_id IS DISTINCT FROM v_event OR v_w.item_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SETTLED_ITEM_NOT_ELIGIBLE';
    END IF;
    PERFORM 1 FROM {schema}.m05_writeback_permits AS p
     WHERE p.permit_id = v_permit AND p.wb_op_id = v_wb AND p.voided_at IS NULL
       AND p.spent_at IS NOT NULL
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SETTLED_ITEM_NOT_ELIGIBLE';
    END IF;
    -- The canonical check event of THIS op (appended earlier in this transaction).
    v_env := {schema}.m02_get_observation_envelope(v_event);
    IF v_env IS NULL OR v_env OPERATOR(pg_catalog.?) 'error'
       OR (v_env OPERATOR(pg_catalog.->>) 'event_type') IS DISTINCT FROM '""" + T_CHECK + r"""'
       OR (v_env OPERATOR(pg_catalog.->) 'contract_version') IS DISTINCT FROM '1'::jsonb
       OR (v_env OPERATOR(pg_catalog.->) 'metadata_paths' OPERATOR(pg_catalog.->>) 'wb_op_id')
          IS DISTINCT FROM v_wb::text
       OR (v_env OPERATOR(pg_catalog.->) 'metadata_paths' OPERATOR(pg_catalog.->>) 'reason')
          IS DISTINCT FROM v_w.reason THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SETTLED_ITEM_NOT_ELIGIBLE';
    END IF;
    -- The ordinary create rules, digest and the op link live in t089 (one place).
    RETURN {schema}.m05_create_item_v2(pg_catalog.jsonb_build_object('schema_version', 1,
        'source_event_id', v_event::text, 'item_kind', '""" + ITEM_KIND + r"""',
        'attribution', v_attr));
END;
"""
)

# --- unit construction --------------------------------------------------------------------

# (migration id, function, volatility, body, EXECUTE grantee)
_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    ("t077_m05_directory_v3", "m05_directory_v3", "volatile", _DIRECTORY_V3_BODY,
     RUNTIME_ROLE),
    ("t080_m05_writeback_capture_baseline", "m05_writeback_capture_baseline", "volatile",
     _CAPTURE_BODY, RUNTIME_ROLE),
    ("t081_m05_writeback_discover", "m05_writeback_discover", "volatile", _DISCOVER_BODY,
     RUNTIME_ROLE),
    ("t082_m05_writeback_due", "m05_writeback_due", "volatile", _DUE_BODY, RUNTIME_ROLE),
    ("t083_m05_writeback_claim", "m05_writeback_claim", "volatile", _CLAIM_BODY,
     RUNTIME_ROLE),
    ("t084_m05_writeback_claim_reconcile", "m05_writeback_claim_reconcile", "volatile",
     _CLAIM_RECONCILE_BODY, RUNTIME_ROLE),
    ("t085_m05_writeback_decide", "m05_writeback_decide", "volatile", _DECIDE_BODY,
     RUNTIME_ROLE),
    ("t086_m05_writeback_spend", "m05_writeback_spend", "volatile", _SPEND_BODY,
     RUNTIME_ROLE),
    ("t087_m05_writeback_settle", "m05_writeback_settle", "volatile", _SETTLE_BODY,
     RUNTIME_ROLE),
    ("t089_m05_create_item_v2", "m05_create_item_v2", "volatile", _CREATE_V2_BODY,
     RUNTIME_ROLE),
    ("t090_m05_prepare_action_v3", "m05_prepare_action_v3", "volatile", _PREPARE_V3_BODY,
     RUNTIME_ROLE),
    ("t091_m05_apply_action_v3", "m05_apply_action_v3", "volatile", _APPLY_V3_BODY,
     RUNTIME_ROLE),
    ("t094_m05_writeback_create_settled_item", "m05_writeback_create_settled_item",
     "volatile", _CREATE_SETTLED_BODY, RUNTIME_ROLE),
)
_REASON_ID: Final = "t092_m05_followup_reason"
_REASON_NAME: Final = "m05_followup_reason"
_REASON_PARAMS: Final = (("p_item_id", "uuid"),)


def _sha(body: str) -> str:
    import hashlib

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _closure() -> UnitDefinition:
    return UnitDefinition(template="", kind=ACL_CLOSURE_KIND, execution_role=M05_OWNER_ROLE)


_ALL: Final[dict[str, str | UnitDefinition]] = {
    "t076_m05_profile_identity_columns": T076_SQL,
    DIRECTORY_CLOSURE_ID: _closure(),
    "t079_m05_writeback_tables": T079_SQL,
    "t088_m05_writeback_check_contract": T088_SQL,
    **{mid: _definition(name, vol, body, grantee)
       for mid, name, vol, body, grantee in _FUNCTIONS},
    _REASON_ID: _gw._definition(_REASON_NAME, _REASON_PARAMS, "stable", _REASON_BODY,
                                _sha(_REASON_BODY)),
    RUNTIME_CLOSURE_ID: _closure(),
}

SI_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
SI_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    **{mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _FUNCTIONS},
    _REASON_ID: _gw._profile(_REASON_NAME, _REASON_PARAMS, "stable"),
})
_RUNTIME_EXECUTE: Final = ((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),)
SI_ACL_AMENDMENTS: Final[tuple[AclAmendment, ...]] = (
    AclAmendment(target_unit="t063_m05_directory_v2", activated_by=DIRECTORY_CLOSURE_ID,
                 removes=_RUNTIME_EXECUTE),
    *(AclAmendment(target_unit=unit, activated_by=RUNTIME_CLOSURE_ID, removes=_RUNTIME_EXECUTE)
      for unit in ("t014_m05_create_item", "t030_m05_prepare_action_v2",
                   "t031_m05_apply_action_v2")),
)
