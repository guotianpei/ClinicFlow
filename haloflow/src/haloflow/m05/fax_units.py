"""Incoming fax routing FX-IN, package FX1: per-tenant migration units t136..t139. SYNTHETIC ONLY.

Composed ONLY into the synthetic registry behind `include_fx1` (on top of the eligibility
registry, target 135 -> 139); the production registry {36} and catalogue are unchanged.
Governing: FX-IN requirements v1r1 (Codex ALIGN 8e064b80), architecture v1r2 (Codex ALIGN
469d7802), test cases v1 + the FX1 red packet v1 (Codex 65d1f9db), test cases v1r1 §0 (the
receipt's transaction-level shared lock on K_FX_LINES), owner decisions OD-FX1..FX9.

- t136 (migrator SQL): queues (the seven OD-FX1 defaults), lines, rules, faxes, sorting items,
  history, discrepancies, the fax ingress workload allow-list and the support audit table. No
  runtime or support table privilege; the M05 owner reads and writes through the gateways.
- t137 `m05_fx_settings_authority()` (staff owner; EXECUTE to the M05 owner only; the t074
  pattern): the session login is a support operator with an active, in-window grant on THIS
  tenant holding capability `m05.fax.settings`.
- t138 `m05_fx_settings_put(jsonb)` (EXECUTE to haloflow_support only, OD-FX3): queue_put,
  queue_rename (same ID; OD-FX1, Codex REVISE 7859c910), queue_deactivate, rule_put,
  rule_remove. Authority before any lock or write and re-checked
  after the target row lock; one audit row per applied op.
- t139 `m05_fx_receive(jsonb)` (runtime): one transaction — the K_FX_LINES shared xact lock,
  the tenant-local line re-check, the fax row (or duplicate handling), routing, the sorting row
  and the first history row (architecture 5.1, 5.4).
- t140 `m05_fx_tenant_bound() -> boolean` (staff owner; EXECUTE to the M05 owner only; Codex
  3e5361ff): the session's app.tenant_id equals the tenant registered for THIS schema in
  shared.tenants (the staff owner's alembic 009 read). Every FX runtime gateway refuses otherwise
  with 22023 'tenant context invalid' before any read or write (BINDING below). t139 is created
  before t140; PL/pgSQL resolves the call at execution, and no tenant runs FX code below 140.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinition, UnitDefinitions
from haloflow.m01.staff import units as staff_units
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05.fax_rules import K_FX_LINES, META_TAG
from haloflow.m05.intake_units import _ATTRIBUTION, _DECLARE_COMMON, _TENANT
from haloflow.m05.reminder_units import SUPPORT_ROLE, _definition, _profile

TABLES_ID: Final = "t136_m05_fx_tables"
AUTHORITY_ID: Final = "t137_m05_fx_settings_authority"
SETTINGS_ID: Final = "t138_m05_fx_settings_put"
RECEIVE_ID: Final = "t139_m05_fx_receive"
BOUND_ID: Final = "t140_m05_fx_tenant_bound"
FX1_TARGET: Final = 140

AUTHORITY_NAME: Final = "m05_fx_settings_authority"
SETTINGS_NAME: Final = "m05_fx_settings_put"
RECEIVE_NAME: Final = "m05_fx_receive"
BOUND_NAME: Final = "m05_fx_tenant_bound"
SETTINGS_CAPABILITY: Final = "m05.fax.settings"

DEFAULT_QUEUES: Final = ("Labs/Results", "Imaging", "Referrals & consults", "Records requests",
                         "Insurance/Prior auth", "Pharmacy/Refills", "General")  # OD-FX1
REASONS: Final = ("no_matching_rule", "sender_not_shown", "rules_conflict")  # OD-FX7 codes
HISTORY_ACTIONS: Final = ("received_routed", "received_needs_sorting", "opened", "moved",
                          "filed", "sorted")
_E164: Final = r"'^\+[1-9][0-9]{7,14}$'"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _raise(code: str) -> str:
    return f"RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{code}';"


# --- t136 tables (migrator) ----------------------------------------------------------------

T136_SQL: Final = r"""
-- t136:step:tables
CREATE TABLE {schema}.m05_fx_workloads (
    workload_id uuid PRIMARY KEY
);
CREATE TABLE {schema}.m05_fx_queues (
    queue_id uuid PRIMARY KEY,
    name varchar(64) NOT NULL CHECK (pg_catalog.char_length(name) BETWEEN 1 AND 64),
    active boolean NOT NULL,
    position smallint NOT NULL CHECK (position BETWEEN 1 AND 999),
    created_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX m05_fx_queues_name_key ON {schema}.m05_fx_queues (pg_catalog.lower(name));
CREATE TABLE {schema}.m05_fx_lines (
    line_e164 varchar(16) PRIMARY KEY CHECK (line_e164 OPERATOR(pg_catalog.~) @@E164@@)
);
CREATE TABLE {schema}.m05_fx_rules (
    rule_id uuid PRIMARY KEY,
    sender_e164 varchar(16) CHECK (sender_e164 OPERATOR(pg_catalog.~) @@E164@@),
    line_e164 varchar(16) REFERENCES {schema}.m05_fx_lines (line_e164),
    queue_id uuid NOT NULL REFERENCES {schema}.m05_fx_queues (queue_id),
    CONSTRAINT m05_fx_rules_key CHECK (sender_e164 IS NOT NULL OR line_e164 IS NOT NULL),
    CONSTRAINT m05_fx_rules_unique UNIQUE NULLS NOT DISTINCT (sender_e164, line_e164, queue_id)
);
CREATE TABLE {schema}.m05_fx_faxes (
    fax_id uuid PRIMARY KEY,
    provider varchar(32) NOT NULL,
    provider_fax_id varchar(128) NOT NULL,
    line_e164 varchar(16) NOT NULL,
    sender_e164 varchar(16),
    pages smallint NOT NULL CHECK (pages BETWEEN 1 AND 999),
    received_at timestamptz NOT NULL,
    doc_ref varchar(72) NOT NULL,
    meta_digest bytea NOT NULL CHECK (pg_catalog.octet_length(meta_digest) = 32),
    state varchar(16) NOT NULL CHECK (state IN ('routed', 'needs_sorting', 'filed')),
    queue_id uuid REFERENCES {schema}.m05_fx_queues (queue_id),
    routed_by varchar(8) CHECK (routed_by IN ('rule', 'staff')),
    level smallint CHECK (level BETWEEN 1 AND 3),
    version integer NOT NULL CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CONSTRAINT m05_fx_faxes_identity UNIQUE (provider, provider_fax_id),
    CONSTRAINT m05_fx_faxes_queue CHECK ((state = 'needs_sorting') = (queue_id IS NULL)),
    CONSTRAINT m05_fx_faxes_routed_by CHECK ((state = 'needs_sorting') = (routed_by IS NULL)),
    CONSTRAINT m05_fx_faxes_level CHECK ((level IS NOT NULL) = (routed_by = 'rule')
                                         OR (level IS NULL AND routed_by IS NULL))
);
CREATE TABLE {schema}.m05_fx_sorting (
    fax_id uuid PRIMARY KEY REFERENCES {schema}.m05_fx_faxes (fax_id),
    reason varchar(24) NOT NULL CHECK (reason IN (@@REASONS@@)),
    state varchar(8) NOT NULL CHECK (state IN ('open', 'resolved')),
    resolved_by varchar(64),
    resolved_at timestamptz,
    resolved_queue_id uuid REFERENCES {schema}.m05_fx_queues (queue_id),
    created_at timestamptz NOT NULL,
    CONSTRAINT m05_fx_sorting_resolved CHECK ((state = 'resolved')
        = (resolved_by IS NOT NULL AND resolved_at IS NOT NULL
           AND resolved_queue_id IS NOT NULL))
);
CREATE INDEX m05_fx_sorting_open ON {schema}.m05_fx_sorting (state);
CREATE TABLE {schema}.m05_fx_history (
    fax_id uuid NOT NULL REFERENCES {schema}.m05_fx_faxes (fax_id),
    seq integer NOT NULL CHECK (seq >= 1),
    action varchar(24) NOT NULL CHECK (action IN (@@ACTIONS@@)),
    actor varchar(64),
    from_queue_id uuid,
    to_queue_id uuid,
    at timestamptz NOT NULL,
    PRIMARY KEY (fax_id, seq),
    CONSTRAINT m05_fx_history_actor CHECK ((actor IS NULL)
        = (action IN ('received_routed', 'received_needs_sorting')))
);
CREATE TABLE {schema}.m05_fx_discrepancies (
    fax_id uuid NOT NULL REFERENCES {schema}.m05_fx_faxes (fax_id),
    seq integer NOT NULL CHECK (seq >= 1),
    fields text[] NOT NULL CHECK (pg_catalog.cardinality(fields) >= 1),
    seen_at timestamptz NOT NULL,
    PRIMARY KEY (fax_id, seq)
);
CREATE TABLE {schema}.m05_fx_settings_audit (
    audit_id uuid PRIMARY KEY,
    actor_id varchar(128) NOT NULL,
    grant_id uuid NOT NULL,
    op varchar(24) NOT NULL CHECK (op IN ('queue_put', 'queue_rename', 'queue_deactivate',
                                          'rule_put', 'rule_remove')),
    target_id uuid NOT NULL,
    at timestamptz NOT NULL
);

-- t136:step:default_queues
INSERT INTO {schema}.m05_fx_queues (queue_id, name, active, position, created_at)
SELECT pg_catalog.gen_random_uuid(), q.name, true, q.position, pg_catalog.clock_timestamp()
  FROM (VALUES @@QUEUES@@) AS q (name, position);

-- t136:step:runtime_privileges
REVOKE ALL ON {schema}.m05_fx_workloads, {schema}.m05_fx_queues, {schema}.m05_fx_lines,
    {schema}.m05_fx_rules, {schema}.m05_fx_faxes, {schema}.m05_fx_sorting,
    {schema}.m05_fx_history, {schema}.m05_fx_discrepancies, {schema}.m05_fx_settings_audit
    FROM PUBLIC, haloflow_runtime, haloflow_support;

-- t136:step:grants
GRANT SELECT ON {schema}.m05_fx_workloads, {schema}.m05_fx_lines TO haloflow_m05_owner;
GRANT SELECT, INSERT, UPDATE ON {schema}.m05_fx_queues, {schema}.m05_fx_faxes,
    {schema}.m05_fx_sorting TO haloflow_m05_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.m05_fx_rules TO haloflow_m05_owner;
GRANT SELECT, INSERT ON {schema}.m05_fx_history, {schema}.m05_fx_discrepancies,
    {schema}.m05_fx_settings_audit TO haloflow_m05_owner;
""".replace("@@E164@@", _E164).replace("@@REASONS@@", _in(REASONS)).replace(
    "@@ACTIONS@@", _in(HISTORY_ACTIONS)).replace(
    "@@QUEUES@@", ", ".join(f"('{n}', {i})" for i, n in enumerate(DEFAULT_QUEUES, start=1)))

# --- t137 authority (staff owner; EXECUTE to the M05 owner) --------------------------------

AUTHORITY_BODY: Final = r"""
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
       AND '@@CAP@@' = ANY (g.capabilities)
     ORDER BY g.expires_at DESC, g.grant_id
     LIMIT 1;
    IF v_grant IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'SUPPORT_NOT_AUTHORIZED');
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'authorized',
        'actor_id', v_actor, 'grant_id', v_grant::text);
END;
""".replace("@@CAP@@", SETTINGS_CAPABILITY)

# --- t138 settings_put (support only) ------------------------------------------------------

_AUTHORIZE: Final = r"""
    v_auth := {schema}.m05_fx_settings_authority();
    IF (v_auth OPERATOR(pg_catalog.->>) 'outcome') IS DISTINCT FROM 'authorized' THEN
        """ + _raise("SUPPORT_NOT_AUTHORIZED") + r"""
    END IF;
"""

SETTINGS_BODY: Final = (r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    c_phone constant text := @@E164@@;
    v_setting text;
    v_keys text[];
    v_op text;
    v_auth jsonb;
    v_queue uuid;
    v_rule uuid;
    v_target uuid;
    v_name text;
    v_position integer;
    v_sender text;
    v_line text;
    v_found boolean;
BEGIN""" + _TENANT + r"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'op')
          IS DISTINCT FROM 'string' THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_op := p_command OPERATOR(pg_catalog.->>) 'op';
    IF v_op = 'queue_put' THEN
        IF v_keys IS DISTINCT FROM ARRAY['name', 'op', 'position', 'schema_version']::text[]
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'name')
              IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'name')
              NOT BETWEEN 1 AND 64
           OR (p_command OPERATOR(pg_catalog.->>) 'name') <> pg_catalog.btrim(
              p_command OPERATOR(pg_catalog.->>) 'name')
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'position')
              IS DISTINCT FROM 'number'
           OR (p_command OPERATOR(pg_catalog.->>) 'position')
              OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,2}$' THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_op = 'queue_rename' THEN
        IF v_keys IS DISTINCT FROM ARRAY['name', 'op', 'queue_id', 'schema_version']::text[]
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'queue_id')
              IS DISTINCT FROM 'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'queue_id') OPERATOR(pg_catalog.!~) c_uuid
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'name')
              IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'name')
              NOT BETWEEN 1 AND 64
           OR (p_command OPERATOR(pg_catalog.->>) 'name') <> pg_catalog.btrim(
              p_command OPERATOR(pg_catalog.->>) 'name') THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_op = 'queue_deactivate' THEN
        IF v_keys IS DISTINCT FROM ARRAY['op', 'queue_id', 'schema_version']::text[]
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'queue_id')
              IS DISTINCT FROM 'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'queue_id') OPERATOR(pg_catalog.!~) c_uuid THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_op = 'rule_put' THEN
        IF v_keys IS DISTINCT FROM ARRAY['line', 'op', 'queue_id', 'schema_version',
                                         'sender']::text[]
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'queue_id')
              IS DISTINCT FROM 'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'queue_id') OPERATOR(pg_catalog.!~) c_uuid
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'sender')
              NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'line')
              NOT IN ('string', 'null')
           OR (p_command OPERATOR(pg_catalog.->>) 'sender') OPERATOR(pg_catalog.!~) c_phone
           OR (p_command OPERATOR(pg_catalog.->>) 'line') OPERATOR(pg_catalog.!~) c_phone
           OR (pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'sender') = 'null'
               AND pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'line') = 'null') THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_op = 'rule_remove' THEN
        IF v_keys IS DISTINCT FROM ARRAY['op', 'rule_id', 'schema_version']::text[]
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'rule_id')
              IS DISTINCT FROM 'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'rule_id') OPERATOR(pg_catalog.!~) c_uuid THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSE
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    -- OD-FX3: support authority before any lock or write ...""" + _AUTHORIZE + r"""
    IF v_op = 'queue_put' THEN
        v_name := p_command OPERATOR(pg_catalog.->>) 'name';
        v_position := (p_command OPERATOR(pg_catalog.->>) 'position')::integer;
        -- serialise queue creation on the queue table (no row exists yet to lock)
        LOCK TABLE {schema}.m05_fx_queues IN SHARE ROW EXCLUSIVE MODE;""" + _AUTHORIZE + r"""
        IF EXISTS (SELECT 1 FROM {schema}.m05_fx_queues AS q
                    WHERE pg_catalog.lower(q.name) = pg_catalog.lower(v_name)) THEN
            """ + _raise("QUEUE_NAME_TAKEN") + r"""
        END IF;
        v_target := pg_catalog.gen_random_uuid();
        INSERT INTO {schema}.m05_fx_queues (queue_id, name, active, position, created_at)
        VALUES (v_target, v_name, true, v_position, pg_catalog.clock_timestamp());
        v_queue := v_target;
    ELSIF v_op = 'queue_rename' THEN
        -- OD-FX1 rename (Codex REVISE 7859c910): the SAME queue ID keeps its rules and faxes.
        v_target := (p_command OPERATOR(pg_catalog.->>) 'queue_id')::uuid;
        v_name := p_command OPERATOR(pg_catalog.->>) 'name';
        LOCK TABLE {schema}.m05_fx_queues IN SHARE ROW EXCLUSIVE MODE;
        PERFORM 1 FROM {schema}.m05_fx_queues AS q
          WHERE q.queue_id = v_target AND q.active FOR UPDATE;
        v_found := FOUND;
        -- ... and re-checked after the rename lock.""" + _AUTHORIZE + r"""
        IF NOT v_found THEN
            """ + _raise("QUEUE_NOT_FOUND") + r"""
        END IF;
        IF EXISTS (SELECT 1 FROM {schema}.m05_fx_queues AS q
                    WHERE pg_catalog.lower(q.name) = pg_catalog.lower(v_name)
                      AND q.queue_id <> v_target) THEN
            """ + _raise("QUEUE_NAME_TAKEN") + r"""
        END IF;
        UPDATE {schema}.m05_fx_queues SET name = v_name WHERE queue_id = v_target;
        v_queue := v_target;
    ELSIF v_op = 'queue_deactivate' THEN
        v_target := (p_command OPERATOR(pg_catalog.->>) 'queue_id')::uuid;
        PERFORM 1 FROM {schema}.m05_fx_queues AS q
          WHERE q.queue_id = v_target AND q.active FOR UPDATE;
        v_found := FOUND;
        -- ... and re-checked after the target row lock.""" + _AUTHORIZE + r"""
        IF NOT v_found THEN
            """ + _raise("QUEUE_NOT_FOUND") + r"""
        END IF;
        UPDATE {schema}.m05_fx_queues SET active = false WHERE queue_id = v_target;
    ELSIF v_op = 'rule_put' THEN
        v_queue := (p_command OPERATOR(pg_catalog.->>) 'queue_id')::uuid;
        v_sender := p_command OPERATOR(pg_catalog.->>) 'sender';
        v_line := p_command OPERATOR(pg_catalog.->>) 'line';
        PERFORM 1 FROM {schema}.m05_fx_queues AS q
          WHERE q.queue_id = v_queue AND q.active FOR SHARE;
        v_found := FOUND;""" + _AUTHORIZE + r"""
        IF NOT v_found THEN
            """ + _raise("QUEUE_NOT_FOUND") + r"""
        END IF;
        IF v_line IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM {schema}.m05_fx_lines AS l WHERE l.line_e164 = v_line) THEN
            """ + _raise("LINE_NOT_REGISTERED") + r"""
        END IF;
        v_target := pg_catalog.gen_random_uuid();
        INSERT INTO {schema}.m05_fx_rules (rule_id, sender_e164, line_e164, queue_id)
        VALUES (v_target, v_sender, v_line, v_queue)
        ON CONFLICT ON CONSTRAINT m05_fx_rules_unique DO NOTHING;
        IF NOT FOUND THEN
            """ + _raise("RULE_DUPLICATE") + r"""
        END IF;
        v_rule := v_target;
    ELSE
        v_target := (p_command OPERATOR(pg_catalog.->>) 'rule_id')::uuid;
        PERFORM 1 FROM {schema}.m05_fx_rules AS r WHERE r.rule_id = v_target FOR UPDATE;
        v_found := FOUND;""" + _AUTHORIZE + r"""
        IF NOT v_found THEN
            """ + _raise("RULE_NOT_FOUND") + r"""
        END IF;
        DELETE FROM {schema}.m05_fx_rules WHERE rule_id = v_target;
    END IF;
    INSERT INTO {schema}.m05_fx_settings_audit (audit_id, actor_id, grant_id, op, target_id, at)
    VALUES (pg_catalog.gen_random_uuid(), v_auth OPERATOR(pg_catalog.->>) 'actor_id',
            (v_auth OPERATOR(pg_catalog.->>) 'grant_id')::uuid, v_op, v_target,
            pg_catalog.clock_timestamp());
    RETURN pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object('schema_version', 1,
        'outcome', 'applied', 'queue_id', v_queue::text, 'rule_id', v_rule::text));
END;
""").replace("@@E164@@", _E164)

# --- t140 tenant binding (staff owner; EXECUTE to the M05 owner) -------------------------

BOUND_BODY: Final = r"""
DECLARE
    v_setting text;
    v_tenant text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    SELECT t.tenant_id INTO v_tenant FROM shared.tenants AS t WHERE t.schema_key = '{schema}';
    RETURN FOUND AND v_setting IS NOT NULL AND v_tenant = v_setting;
END;
"""

# Used by every FX runtime gateway right after the context-syntax check (_TENANT).
BINDING: Final = r"""
    IF NOT {schema}.m05_fx_tenant_bound() THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
"""

# --- t139 receive (runtime) ----------------------------------------------------------------

_DIGEST: Final = (
    "pg_catalog.sha256(pg_catalog.convert_to(pg_catalog.concat_ws(pg_catalog.chr(31), '"
    + META_TAG + "', v_line, COALESCE(v_sender, ''), v_pages::text, v_received_text,"
    " v_doc), 'UTF8'))")

RECEIVE_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    c_ts constant text := '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$';
    v_provider text;
    v_provider_id text;
    v_line text;
    v_sender text;
    v_pages integer;
    v_received_text text;
    v_received timestamptz;
    v_doc text;
    v_digest bytea;
    v_fax uuid;
    v_existing record;
    v_level integer;
    v_queues uuid[];
    v_queue uuid;
    v_reason text;
    v_fields text[];
    v_seq integer;
    v_now timestamptz;
BEGIN""" + _TENANT + BINDING + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'doc_ref', 'line', 'pages', 'provider',
                                     'provider_fax_id', 'received_at', 'schema_version',
                                     'sender']::text[] THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;""" + _ATTRIBUTION + r"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'provider')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'provider')
          OPERATOR(pg_catalog.!~) '^[a-z][a-z0-9_]{0,31}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'provider_fax_id')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'provider_fax_id')
          OPERATOR(pg_catalog.!~) '^[\x21-\x7e]{1,128}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'line')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'line') OPERATOR(pg_catalog.!~) c_phone
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'sender')
          NOT IN ('string', 'null')
       OR (p_command OPERATOR(pg_catalog.->>) 'sender') OPERATOR(pg_catalog.!~) c_phone
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'pages')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'pages')
          OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,2}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'received_at')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'received_at') OPERATOR(pg_catalog.!~) c_ts
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'doc_ref')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'doc_ref')
          OPERATOR(pg_catalog.!~) '^syn-doc-[A-Za-z0-9-]{1,64}$' THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    IF (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'workload'
       OR NOT EXISTS (
           SELECT 1 FROM {schema}.m05_fx_workloads AS w
            WHERE w.workload_id = (v_attr OPERATOR(pg_catalog.->>) 'principal_id')::uuid) THEN
        """ + _raise("WORKLOAD_NOT_AUTHORIZED") + r"""
    END IF;
    BEGIN
        v_received := (p_command OPERATOR(pg_catalog.->>) 'received_at')::timestamptz;
    EXCEPTION WHEN OTHERS THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END;
    v_provider := p_command OPERATOR(pg_catalog.->>) 'provider';
    v_provider_id := p_command OPERATOR(pg_catalog.->>) 'provider_fax_id';
    v_line := p_command OPERATOR(pg_catalog.->>) 'line';
    v_sender := p_command OPERATOR(pg_catalog.->>) 'sender';
    v_pages := (p_command OPERATOR(pg_catalog.->>) 'pages')::integer;
    v_received_text := p_command OPERATOR(pg_catalog.->>) 'received_at';
    v_doc := p_command OPERATOR(pg_catalog.->>) 'doc_ref';
    v_digest := """ + _DIGEST + r""";
    -- Test cases v1r1 §0: the install-wide line fence, held to commit, BEFORE the line re-check.
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(@@KEY@@::bigint);
    IF NOT EXISTS (SELECT 1 FROM {schema}.m05_fx_lines AS l WHERE l.line_e164 = v_line) THEN
        """ + _raise("LINE_NOT_REGISTERED") + r"""
    END IF;
    v_now := pg_catalog.clock_timestamp();
    v_fax := pg_catalog.gen_random_uuid();
    INSERT INTO {schema}.m05_fx_faxes (fax_id, provider, provider_fax_id, line_e164, sender_e164,
        pages, received_at, doc_ref, meta_digest, state, queue_id, routed_by, level, version,
        created_at)
    VALUES (v_fax, v_provider, v_provider_id, v_line, v_sender, v_pages, v_received, v_doc,
        v_digest, 'needs_sorting', NULL, NULL, NULL, 1, v_now)
    ON CONFLICT ON CONSTRAINT m05_fx_faxes_identity DO NOTHING;
    IF NOT FOUND THEN
        -- FR-7: same identity. Lock the existing row (serialises discrepancy sequence numbers).
        SELECT f.* INTO v_existing FROM {schema}.m05_fx_faxes AS f
         WHERE f.provider = v_provider AND f.provider_fax_id = v_provider_id FOR UPDATE;
        IF v_existing.meta_digest = v_digest THEN
            RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'duplicate',
                'fax_id', v_existing.fax_id::text, 'queue_id', NULL, 'level', NULL,
                'reason', NULL);
        END IF;
        v_fields := ARRAY[]::text[];
        IF v_existing.line_e164 IS DISTINCT FROM v_line THEN
            v_fields := v_fields OPERATOR(pg_catalog.||) 'line'::text;
        END IF;
        IF v_existing.sender_e164 IS DISTINCT FROM v_sender THEN
            v_fields := v_fields OPERATOR(pg_catalog.||) 'sender'::text;
        END IF;
        IF v_existing.pages IS DISTINCT FROM v_pages THEN
            v_fields := v_fields OPERATOR(pg_catalog.||) 'pages'::text;
        END IF;
        IF v_existing.received_at IS DISTINCT FROM v_received THEN
            v_fields := v_fields OPERATOR(pg_catalog.||) 'received_at'::text;
        END IF;
        IF v_existing.doc_ref IS DISTINCT FROM v_doc THEN
            v_fields := v_fields OPERATOR(pg_catalog.||) 'doc_ref'::text;
        END IF;
        SELECT COALESCE(pg_catalog.max(d.seq), 0) OPERATOR(pg_catalog.+) 1 INTO v_seq
          FROM {schema}.m05_fx_discrepancies AS d WHERE d.fax_id = v_existing.fax_id;
        INSERT INTO {schema}.m05_fx_discrepancies (fax_id, seq, fields, seen_at)
        VALUES (v_existing.fax_id, v_seq, v_fields, v_now);
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'duplicate_mismatch', 'fax_id', v_existing.fax_id::text,
            'queue_id', NULL, 'level', NULL, 'reason', NULL);
    END IF;
    -- Routing (FR-4..6, OD-FX5): the highest level with any rule on an ACTIVE queue decides;
    -- its DISTINCT destinations decide.
    SELECT m.lvl, pg_catalog.array_agg(DISTINCT m.queue_id) INTO v_level, v_queues
      FROM (SELECT CASE WHEN r.sender_e164 IS NOT NULL AND r.line_e164 IS NOT NULL THEN 1
                        WHEN r.sender_e164 IS NOT NULL THEN 2 ELSE 3 END AS lvl, r.queue_id
              FROM {schema}.m05_fx_rules AS r
              JOIN {schema}.m05_fx_queues AS q ON q.queue_id = r.queue_id AND q.active
             WHERE (r.sender_e164 IS NOT NULL AND v_sender IS NOT NULL
                    AND r.sender_e164 = v_sender
                    AND (r.line_e164 IS NULL OR r.line_e164 = v_line))
                OR (r.sender_e164 IS NULL AND r.line_e164 = v_line)) AS m
     GROUP BY m.lvl ORDER BY m.lvl LIMIT 1;
    IF v_level IS NOT NULL AND pg_catalog.cardinality(v_queues) = 1 THEN
        v_queue := v_queues[1];
        UPDATE {schema}.m05_fx_faxes SET state = 'routed', queue_id = v_queue,
               routed_by = 'rule', level = v_level WHERE fax_id = v_fax;
        INSERT INTO {schema}.m05_fx_history (fax_id, seq, action, actor, from_queue_id,
            to_queue_id, at) VALUES (v_fax, 1, 'received_routed', NULL, NULL, v_queue, v_now);
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'routed',
            'fax_id', v_fax::text, 'queue_id', v_queue::text, 'level', v_level,
            'reason', NULL);
    END IF;
    v_reason := CASE WHEN v_level IS NOT NULL THEN 'rules_conflict'
                     WHEN v_sender IS NULL THEN 'sender_not_shown'
                     ELSE 'no_matching_rule' END;
    INSERT INTO {schema}.m05_fx_sorting (fax_id, reason, state, created_at)
    VALUES (v_fax, v_reason, 'open', v_now);
    INSERT INTO {schema}.m05_fx_history (fax_id, seq, action, actor, from_queue_id, to_queue_id,
        at) VALUES (v_fax, 1, 'received_needs_sorting', NULL, NULL, NULL, v_now);
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'needs_sorting',
        'fax_id', v_fax::text, 'queue_id', NULL, 'level', NULL, 'reason', v_reason);
END;
""").replace("@@KEY@@", str(K_FX_LINES))

# --- unit construction ---------------------------------------------------------------------

_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    (SETTINGS_ID, SETTINGS_NAME, "volatile", SETTINGS_BODY, SUPPORT_ROLE),
    (RECEIVE_ID, RECEIVE_NAME, "volatile", RECEIVE_BODY, RUNTIME_ROLE),
)

_ALL: Final[dict[str, str | UnitDefinition]] = {
    TABLES_ID: T136_SQL,
    BOUND_ID: staff_units._definition(
        BOUND_NAME, (), "bool", "stable", M05_OWNER_ROLE, BOUND_BODY,
        hashlib.sha256(BOUND_BODY.encode("utf-8")).hexdigest()),
    AUTHORITY_ID: staff_units._definition(
        AUTHORITY_NAME, (), "jsonb", "volatile", M05_OWNER_ROLE, AUTHORITY_BODY,
        hashlib.sha256(AUTHORITY_BODY.encode("utf-8")).hexdigest()),
    **{mid: _definition(name, vol, body, grantee) for mid, name, vol, body, grantee in _FUNCTIONS},
}

FX1_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
FX1_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    **{mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _FUNCTIONS},
    AUTHORITY_ID: staff_units._profile(AUTHORITY_NAME, (), "jsonb", "volatile", M05_OWNER_ROLE),
    BOUND_ID: staff_units._profile(BOUND_NAME, (), "bool", "stable", M05_OWNER_ROLE),
})
