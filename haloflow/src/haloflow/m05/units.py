"""M05 per-tenant units t012..t020 (UC1 part B).

- ``t012_m05_tables``  migrator-owned tables (package v1 section 2.1). The runtime
  holds nothing. In B1 the M05 owner holds exactly INSERT on history; B2..B4 add
  the exact column grants each function needs (Codex gate 20261031T194129Z: no
  broad write privileges, no reads as the migrator). History, actions and notes are
  immutable: a row-level rejector refuses UPDATE and DELETE for every role,
  including the migrator, and a statement-level one refuses TRUNCATE.
- ``t013_m05_seed``    the W1 outcome codes; the M05 resolution contract
  ``m05_followup_resolution_asserted`` v1 and its action family and type, all
  INACTIVE (activation is a B4/B5 decision; inactive contracts need no installed
  route at startup); and the M05 envelope allowlist rows. The intake map is empty
  in B1 (B2 adds test-only rows, never active in production).
- ``t014``..``t020``   typed stub gateways (`haloflow.m05.gateways`).

The rejector function is deliberately not named ``m05_*``: the ``m05_`` function
namespace is exactly the seven reviewed gateways (ME-01).
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinitions
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05.gateways import M05_GATEWAY_UNITS

T012_MIGRATION_ID: Final = "t012_m05_tables"
T013_MIGRATION_ID: Final = "t013_m05_seed"

_S: Final = "{schema}"
_MSG_ROLES: Final = "m05 role verification failed"

_ITEM_KINDS: Final = "'no_reply', 'reschedule_request', 'cancel_request', 'unclear_reply'"
_RESOLVE_ACTIONS: Final = "'resolve', 'claim_and_resolve'"
_ACTIONS: Final = "'claim', 'assign', 'reassign', 'start_progress', 'resolve', 'claim_and_resolve'"
_CLOSED_STATE: Final = (
    "(jsonb_typeof({c}) = 'object'"
    " AND ({c} - 'state' - 'assignee_id' - 'outcome_code') = '{{}}'::jsonb)"
)

T012_SQL: Final = f"""
-- t012:step:guard
DO $guard$
BEGIN
    IF (SELECT count(*)
          FROM pg_catalog.pg_roles AS r
         WHERE r.rolname = '{M05_OWNER_ROLE}'
           AND NOT r.rolcanlogin
           AND NOT r.rolsuper
           AND NOT r.rolcreatedb
           AND NOT r.rolcreaterole
           AND NOT r.rolreplication
           AND NOT r.rolbypassrls) <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_ROLES}';
    END IF;
END
$guard$;

-- t012:step:tables
CREATE TABLE {_S}.m05_outcome_codes (
    outcome_code varchar(32) PRIMARY KEY CHECK (outcome_code ~ '^[a-z][a-z_]{{1,31}}$'),
    display_label varchar(64) NOT NULL,
    note_required boolean NOT NULL
);
CREATE TABLE {_S}.m05_intake_map (
    source_event_type varchar(128) NOT NULL,
    source_contract_version integer NOT NULL,
    discriminator_path varchar(128) NOT NULL
        CHECK (discriminator_path ~ '^[a-z][a-z0-9_]{{0,127}}$'),
    discriminator_value varchar(128) NOT NULL,
    item_kind varchar(32) NOT NULL CHECK (item_kind IN ({_ITEM_KINDS})),
    producer_workload_id uuid NOT NULL,
    active_in_production boolean NOT NULL,
    PRIMARY KEY (source_event_type, source_contract_version, discriminator_path,
                 discriminator_value),
    CONSTRAINT m05_intake_map_contract_fk FOREIGN KEY (source_event_type, source_contract_version)
        REFERENCES {_S}.event_contracts (event_type, version)
);
CREATE TABLE {_S}.m05_followup_items (
    item_id uuid PRIMARY KEY,
    source_event_id uuid NOT NULL,
    item_kind varchar(32) NOT NULL CHECK (item_kind IN ({_ITEM_KINDS})),
    patient_id uuid NOT NULL,
    state varchar(16) NOT NULL CHECK (state IN ('open', 'in_progress', 'resolved')),
    assignee_id uuid,
    outcome_code varchar(32) REFERENCES {_S}.m05_outcome_codes (outcome_code),
    version integer NOT NULL CHECK (version >= 1),
    create_digest bytea NOT NULL CHECK (octet_length(create_digest) = 32),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT m05_followup_items_source_kind_key UNIQUE (source_event_id, item_kind),
    CONSTRAINT m05_followup_items_resolved_outcome
        CHECK ((state = 'resolved') = (outcome_code IS NOT NULL))
);
CREATE TABLE {_S}.m05_followup_history (
    history_id uuid PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES {_S}.m05_followup_items (item_id),
    seq integer NOT NULL CHECK (seq >= 1),
    action varchar(32) NOT NULL CHECK (action IN ('created', {_ACTIONS})),
    actor_id uuid NOT NULL,
    before jsonb CHECK (before IS NULL OR {_CLOSED_STATE.format(c="before")}),
    after jsonb NOT NULL CHECK ({_CLOSED_STATE.format(c="after")}),
    resolution_event_id uuid UNIQUE,
    assertion_kind varchar(16),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT m05_followup_history_item_seq_key UNIQUE (item_id, seq),
    CONSTRAINT m05_followup_history_created_before
        CHECK ((action = 'created') = (before IS NULL)),
    CONSTRAINT m05_followup_history_staff_assertion
        CHECK ((action IN ({_RESOLVE_ACTIONS}) AND assertion_kind = 'staff'
                AND resolution_event_id IS NOT NULL)
            OR (action NOT IN ({_RESOLVE_ACTIONS}) AND assertion_kind IS NULL
                AND resolution_event_id IS NULL))
);
CREATE TABLE {_S}.m05_followup_actions (
    item_id uuid NOT NULL REFERENCES {_S}.m05_followup_items (item_id),
    client_action_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    requested_action varchar(32) NOT NULL CHECK (requested_action IN ({_ACTIONS})),
    digest bytea NOT NULL CHECK (octet_length(digest) = 32),
    hmac_key_version smallint CHECK (hmac_key_version >= 1),
    event_id uuid UNIQUE,
    result_snapshot jsonb NOT NULL CHECK (jsonb_typeof(result_snapshot) = 'object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    PRIMARY KEY (item_id, client_action_id)
);
CREATE TABLE {_S}.m05_followup_notes (
    note_id uuid PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES {_S}.m05_followup_items (item_id),
    client_action_id uuid NOT NULL,
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) >= 16),
    enc_key_version smallint NOT NULL CHECK (enc_key_version >= 1),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT m05_followup_notes_action_key UNIQUE (item_id, client_action_id)
);

-- t012:step:integrity
CREATE FUNCTION {_S}.followup_record_reject() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
    SET search_path = pg_catalog, {_S}, pg_temp
    AS $body$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '0A000', MESSAGE = 'follow-up records are immutable';
END;
$body$;
REVOKE ALL ON FUNCTION {_S}.followup_record_reject() FROM PUBLIC;
CREATE TRIGGER m05_followup_history_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m05_followup_history
    FOR EACH ROW EXECUTE FUNCTION {_S}.followup_record_reject();
CREATE TRIGGER m05_followup_actions_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m05_followup_actions
    FOR EACH ROW EXECUTE FUNCTION {_S}.followup_record_reject();
CREATE TRIGGER m05_followup_notes_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m05_followup_notes
    FOR EACH ROW EXECUTE FUNCTION {_S}.followup_record_reject();
CREATE TRIGGER m05_followup_history_no_truncate
    BEFORE TRUNCATE ON {_S}.m05_followup_history
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.followup_record_reject();
CREATE TRIGGER m05_followup_actions_no_truncate
    BEFORE TRUNCATE ON {_S}.m05_followup_actions
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.followup_record_reject();
CREATE TRIGGER m05_followup_notes_no_truncate
    BEFORE TRUNCATE ON {_S}.m05_followup_notes
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.followup_record_reject();

-- t012:step:runtime_privileges
REVOKE ALL ON {_S}.m05_outcome_codes, {_S}.m05_intake_map, {_S}.m05_followup_items,
    {_S}.m05_followup_history, {_S}.m05_followup_actions, {_S}.m05_followup_notes
    FROM {RUNTIME_ROLE};

-- t012:step:grants
GRANT INSERT ON {_S}.m05_followup_history TO {M05_OWNER_ROLE};
"""

_RESOLUTION_METADATA_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":["outcome_code","assertion_kind","item_kind"],'
    '"properties":{'
    '"outcome_code":{"enum":["rescheduled","cancelled_in_ehr","reached_no_change",'
    '"no_contact_left_message","no_contact_no_answer","other"]},'
    '"assertion_kind":{"enum":["staff"]},'
    '"item_kind":{"enum":["no_reply","reschedule_request","cancel_request","unclear_reply"]}'
    "}}"
)

T013_SQL: Final = f"""
-- t013:step:outcome_codes
INSERT INTO {_S}.m05_outcome_codes (outcome_code, display_label, note_required) VALUES
    ('rescheduled', 'Rescheduled', false),
    ('cancelled_in_ehr', 'Cancelled in EHR', false),
    ('reached_no_change', 'Reached – no change', false),
    ('no_contact_left_message', 'Couldn''t reach – left message', false),
    ('no_contact_no_answer', 'Couldn''t reach – no answer', false),
    ('other', 'Other', true);

-- t013:step:resolution_contract
INSERT INTO {_S}.ref_action_families (action_family, owner_module, is_active) VALUES
    ('m05_followup', 'm05', false);
INSERT INTO {_S}.event_types (event_type, action_family, is_active) VALUES
    ('m05_followup_resolution_asserted', 'm05_followup', false);
INSERT INTO {_S}.event_contracts (
    event_type, version, action_family, event_level, allowed_statuses, terminality,
    compatible_outcome_class, precedence_rank, required_fields, required_links,
    optional_links, metadata_json_schema, metadata_max_bytes, authorized_producers,
    required_provenance, correction_policy, projection_rule_id, effective_from,
    retired_at, signal_rule, value_constraints
) VALUES (
    'm05_followup_resolution_asserted', 1, 'm05_followup', 'observation',
    ARRAY['completed']::varchar(64)[], true, 'observation', 100,
    ARRAY['correlation_id']::varchar(64)[],
    '[["m05_followup_item_v1"]]'::jsonb,
    '[]'::jsonb,
    '{_RESOLUTION_METADATA_SCHEMA}'::jsonb,
    1024,
    ARRAY['m05_followup']::varchar(64)[],
    ARRAY[]::varchar(64)[],
    NULL, NULL, '2026-10-31T00:00:00Z', NULL,
    '{{"handoff_kind":"event_signal","signal_type":"m02.event.appended"}}'::jsonb,
    '{{"operation_id":{{"must_be_null":true}}}}'::jsonb
);

-- t013:step:envelope_allowlist
INSERT INTO {_S}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
    ('m05', 'm05_followup_resolution_asserted', 1, 'outcome_code'),
    ('m05', 'm05_followup_resolution_asserted', 1, 'assertion_kind'),
    ('m05', 'm05_followup_resolution_asserted', 1, 'item_kind');
"""

M05_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {
        T012_MIGRATION_ID: T012_SQL,
        T013_MIGRATION_ID: T013_SQL,
        **M05_GATEWAY_UNITS,
    }
)
