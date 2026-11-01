"""M05 typed gateway units t014..t020: one SECURITY DEFINER function each.

Owner and execution role `haloflow_m05_owner`; EXECUTE to `haloflow_runtime` only;
no PUBLIC; `search_path` pinned to pg_catalog, the tenant schema, pg_temp (package
v1 section 2.3, A-INV decisions v1).

Tranche B1 (gate PASS 20261031T194129Z, item 5): every body is a FAIL-CLOSED STUB.
It checks `app.tenant_id` well-formedness (22023, the part A pattern; not
authentication) and then refuses with P0001 / M05_NOT_IMPLEMENTED, touching no
table. B2..B4 replace bodies one function at a time, each with its behaviour tests;
a changed body changes the unit checksum, which is acceptable because no tenant
outside disposable test databases is at the M05 test target.

Each body's SHA-256 is a reviewed literal, so a changed body fails composition
until the literal is updated.
"""

from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition
from haloflow.m02.roles import M05_OWNER_ROLE

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_VOLATILITY_WORD: Final = {"stable": "STABLE", "volatile": "VOLATILE"}
_VOLATILITY_CODE: Final = {"stable": "s", "volatile": "v"}
_Params = tuple[tuple[str, str], ...]

_STUB_BODY: Final = r"""
DECLARE
    v_setting text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'M05_NOT_IMPLEMENTED';
END;
"""
_STUB_BODY_SHA256: Final = "11cf525d221081fdd4402cac2b8452ce4d371df2960973dd617936c0c58ea280"


# t014 m05_create_item (B2; design delta v1 Δ1–Δ7, Codex ALIGN 20261031T233740Z,
# tests-first GATE PASS 20261101T000802Z). Order: tenant context; closed shape
# (COMMAND_MALFORMED); workload-only; envelope (null / ENVELOPE_UNSUPPORTED);
# completed null-operation observation; exactly one intake match whose producer is
# the caller and the source's workload (else SOURCE_NOT_ELIGIBLE, one shape);
# KIND_MISMATCH; patient present; insert or dedupe by digest. Item and history are
# one transaction; the item's patient comes from the envelope, never the caller.
_CREATE_BODY: Final = r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_attr jsonb;
    v_source uuid;
    v_kind text;
    v_principal uuid;
    v_purpose text;
    v_env jsonb;
    v_paths jsonb;
    v_matches integer;
    v_row record;
    v_patient uuid;
    v_digest bytea;
    v_item uuid;
    v_existing record;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;

    -- 1. Closed shape.
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k)
             FROM pg_catalog.jsonb_object_keys(p_command) AS k)
          IS DISTINCT FROM ARRAY['attribution', 'item_kind', 'schema_version',
                                 'source_event_id']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'source_event_id')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'source_event_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'item_kind')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'item_kind') NOT IN
          ('no_reply', 'reschedule_request', 'cancel_request', 'unclear_reply') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_attr := p_command OPERATOR(pg_catalog.->) 'attribution';
    IF pg_catalog.jsonb_typeof(v_attr) IS DISTINCT FROM 'object'
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
    v_source := (p_command OPERATOR(pg_catalog.->>) 'source_event_id')::uuid;
    v_kind := p_command OPERATOR(pg_catalog.->>) 'item_kind';
    v_principal := (v_attr OPERATOR(pg_catalog.->>) 'principal_id')::uuid;
    v_purpose := v_attr OPERATOR(pg_catalog.->>) 'purpose_code';

    -- 2. Workload-only (D9).
    IF (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'workload' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';
    END IF;

    -- 3. Envelope-only source read (D6): unknown / foreign -> null; unsupported.
    v_env := {schema}.m02_get_observation_envelope(v_source);
    IF v_env IS NULL OR v_env OPERATOR(pg_catalog.?) 'error' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';
    END IF;

    -- 4. A completed, null-operation observation.
    IF (v_env OPERATOR(pg_catalog.->>) 'event_level') IS DISTINCT FROM 'observation'
       OR (v_env OPERATOR(pg_catalog.->>) 'status') IS DISTINCT FROM 'completed'
       OR pg_catalog.jsonb_typeof(v_env OPERATOR(pg_catalog.->) 'operation_id')
          IS DISTINCT FROM 'null' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';
    END IF;

    -- 5. Exactly one intake row matches; never pick one of several.
    v_paths := v_env OPERATOR(pg_catalog.->) 'metadata_paths';
    SELECT pg_catalog.count(*) INTO v_matches
      FROM {schema}.m05_intake_map AS m
     WHERE m.source_event_type = (v_env OPERATOR(pg_catalog.->>) 'event_type')
       AND m.source_contract_version
           = ((v_env OPERATOR(pg_catalog.->>) 'contract_version')::integer)
       AND pg_catalog.jsonb_typeof(v_paths OPERATOR(pg_catalog.->) m.discriminator_path::text)
           = 'string'
       AND (v_paths OPERATOR(pg_catalog.->>) m.discriminator_path::text)
           = m.discriminator_value::text;
    IF v_matches <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';
    END IF;
    SELECT m.item_kind, m.producer_workload_id INTO v_row
      FROM {schema}.m05_intake_map AS m
     WHERE m.source_event_type = (v_env OPERATOR(pg_catalog.->>) 'event_type')
       AND m.source_contract_version
           = ((v_env OPERATOR(pg_catalog.->>) 'contract_version')::integer)
       AND pg_catalog.jsonb_typeof(v_paths OPERATOR(pg_catalog.->) m.discriminator_path::text)
           = 'string'
       AND (v_paths OPERATOR(pg_catalog.->>) m.discriminator_path::text)
           = m.discriminator_value::text;
    IF v_row.producer_workload_id IS DISTINCT FROM v_principal
       OR (v_env OPERATOR(pg_catalog.->>) 'workload_id') IS DISTINCT FROM v_principal::text THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';
    END IF;

    -- 6. Kind, after producer eligibility.
    IF v_row.item_kind <> v_kind THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'KIND_MISMATCH';
    END IF;

    -- 7. Patient from the source only.
    IF pg_catalog.jsonb_typeof(v_env OPERATOR(pg_catalog.->) 'patient_id')
       IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';
    END IF;
    v_patient := (v_env OPERATOR(pg_catalog.->>) 'patient_id')::uuid;

    -- 8. Digest (compact UTF-8 JSON preimage), then insert or dedupe.
    v_digest := pg_catalog.sha256(pg_catalog.convert_to(
        '["m05.create/v1",' || pg_catalog.to_json(v_setting)::text
        || ',' || pg_catalog.to_json(v_source::text)::text
        || ',' || pg_catalog.to_json(v_kind)::text
        || ',' || pg_catalog.to_json(v_principal::text)::text
        || ',' || pg_catalog.to_json(v_purpose)::text || ']', 'UTF8'));
    v_item := pg_catalog.gen_random_uuid();
    INSERT INTO {schema}.m05_followup_items (item_id, source_event_id, item_kind, patient_id,
        state, assignee_id, outcome_code, version, create_digest, created_at)
    VALUES (v_item, v_source, v_kind, v_patient, 'open', NULL, NULL, 1, v_digest,
        pg_catalog.statement_timestamp())
    ON CONFLICT (source_event_id, item_kind) DO NOTHING;
    IF NOT FOUND THEN
        SELECT i.item_id, i.create_digest INTO v_existing
          FROM {schema}.m05_followup_items AS i
         WHERE i.source_event_id = v_source AND i.item_kind = v_kind;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DEDUPE_CONFLICT';
        END IF;
        IF v_existing.create_digest <> v_digest THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DEDUPE_CONFLICT';
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'item_id', v_existing.item_id::text, 'created_or_existing', 'existing');
    END IF;
    INSERT INTO {schema}.m05_followup_history (history_id, item_id, seq, action, actor_id,
        before, after, resolution_event_id, assertion_kind, created_at)
    VALUES (pg_catalog.gen_random_uuid(), v_item, 1, 'created', v_principal, NULL,
        pg_catalog.jsonb_build_object('state', 'open', 'assignee_id', NULL,
                                      'outcome_code', NULL),
        NULL, NULL, pg_catalog.statement_timestamp());
    RETURN pg_catalog.jsonb_build_object('schema_version', 1,
        'item_id', v_item::text, 'created_or_existing', 'created');
END;
"""
_CREATE_BODY_SHA256: Final = "6247e7bf3640029d6660f59425caad953c803eb798a98c7528cae31b4c7447b1"


# B3a read slice (tests-first gate 20261101T013202Z: reads may proceed while MA-10
# is pending; actions stay stubs). STABLE, SELECT-only. Timestamps leave as UTC
# 'YYYY-MM-DDTHH:MM:SS.ffffffZ' whatever the session TimeZone. Unknown and
# other-tenant ids are both ITEM_NOT_FOUND (the lookup is schema-local).
_TENANT_CHECK: Final = """
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
"""
_UTC: Final = (
    "pg_catalog.to_char(pg_catalog.timezone('UTC', {col}),"
    " 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
)
_ITEM_VIEW: Final = (
    "pg_catalog.jsonb_build_object('schema_version', 1, 'item_id', i.item_id::text,"
    " 'source_event_id', i.source_event_id::text, 'item_kind', i.item_kind,"
    " 'patient_id', i.patient_id::text, 'state', i.state,"
    " 'assignee_id', i.assignee_id::text, 'outcome_code', i.outcome_code,"
    " 'version', i.version, 'created_at', " + _UTC.format(col="i.created_at") + ")"
)

_GET_ITEM_BODY: Final = (
    r"""
DECLARE
    v_setting text;
    v_view jsonb;
BEGIN"""
    + _TENANT_CHECK
    + r"""
    SELECT """
    + _ITEM_VIEW
    + r""" INTO v_view
      FROM {schema}.m05_followup_items AS i
     WHERE i.item_id = p_item_id;
    IF v_view IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ITEM_NOT_FOUND';
    END IF;
    RETURN v_view;
END;
"""
)

_GET_HISTORY_BODY: Final = (
    r"""
DECLARE
    v_setting text;
    v_entries jsonb;
BEGIN"""
    + _TENANT_CHECK
    + r"""
    IF p_item_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM {schema}.m05_followup_items AS i WHERE i.item_id = p_item_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ITEM_NOT_FOUND';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'seq', h.seq, 'action', h.action, 'actor_id', h.actor_id::text,
               'before', h.before, 'after', h.after, 'assertion_kind', h.assertion_kind,
               'created_at', """
    + _UTC.format(col="h.created_at")
    + r""") ORDER BY h.seq), '[]'::jsonb)
      INTO v_entries
      FROM {schema}.m05_followup_history AS h
     WHERE h.item_id = p_item_id;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1,
        'item_id', p_item_id::text, 'entries', v_entries);
END;
"""
)

_LIST_ITEMS_BODY: Final = (
    r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    c_ts constant text := '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$';
    v_setting text;
    v_key text;
    v_limit integer;
    v_states text[];
    v_assignee_mode text := 'any';
    v_assignee uuid;
    v_after jsonb;
    v_after_ts timestamptz;
    v_after_id uuid;
    v_rows jsonb;
    v_count integer;
    v_next jsonb := NULL;
BEGIN"""
    + _TENANT_CHECK
    + r"""
    IF pg_catalog.jsonb_typeof(p_query) IS DISTINCT FROM 'object'
       OR NOT (p_query OPERATOR(pg_catalog.?) 'schema_version')
       OR NOT (p_query OPERATOR(pg_catalog.?) 'limit')
       OR pg_catalog.jsonb_typeof(p_query OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_query OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_query OPERATOR(pg_catalog.->) 'limit')
          IS DISTINCT FROM 'number'
       OR (p_query OPERATOR(pg_catalog.->) 'limit')::text
          OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,2}$' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    FOR v_key IN SELECT k FROM pg_catalog.jsonb_object_keys(p_query) AS k LOOP
        IF v_key NOT IN ('schema_version', 'limit', 'states', 'assignee', 'after') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END LOOP;
    v_limit := ((p_query OPERATOR(pg_catalog.->) 'limit')::text)::integer;
    IF v_limit > 100 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;

    IF p_query OPERATOR(pg_catalog.?) 'states' THEN
        IF pg_catalog.jsonb_typeof(p_query OPERATOR(pg_catalog.->) 'states')
               IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_query OPERATOR(pg_catalog.->) 'states') = 0
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_array_elements(
                   p_query OPERATOR(pg_catalog.->) 'states') AS e(v)
                WHERE pg_catalog.jsonb_typeof(e.v) IS DISTINCT FROM 'string'
                   OR (e.v OPERATOR(pg_catalog.#>>) '{}'::text[])
                      NOT IN ('open', 'in_progress', 'resolved'))
           OR (SELECT pg_catalog.count(DISTINCT e.v) FROM pg_catalog.jsonb_array_elements(
                   p_query OPERATOR(pg_catalog.->) 'states') AS e(v))
              <> pg_catalog.jsonb_array_length(p_query OPERATOR(pg_catalog.->) 'states') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        SELECT pg_catalog.array_agg(e.v) INTO v_states
          FROM pg_catalog.jsonb_array_elements_text(
              p_query OPERATOR(pg_catalog.->) 'states') AS e(v);
    END IF;

    IF p_query OPERATOR(pg_catalog.?) 'assignee' THEN
        IF pg_catalog.jsonb_typeof(p_query OPERATOR(pg_catalog.->) 'assignee')
               IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        IF (p_query OPERATOR(pg_catalog.->>) 'assignee') = 'unassigned' THEN
            v_assignee_mode := 'unassigned';
        ELSIF (p_query OPERATOR(pg_catalog.->>) 'assignee') OPERATOR(pg_catalog.~) c_uuid THEN
            v_assignee_mode := 'id';
            v_assignee := (p_query OPERATOR(pg_catalog.->>) 'assignee')::uuid;
        ELSE
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END IF;

    IF p_query OPERATOR(pg_catalog.?) 'after' THEN
        v_after := p_query OPERATOR(pg_catalog.->) 'after';
        IF pg_catalog.jsonb_typeof(v_after) IS DISTINCT FROM 'object'
           OR (SELECT pg_catalog.array_agg(k ORDER BY k)
                 FROM pg_catalog.jsonb_object_keys(v_after) AS k)
              IS DISTINCT FROM ARRAY['created_at', 'item_id']::text[]
           OR pg_catalog.jsonb_typeof(v_after OPERATOR(pg_catalog.->) 'created_at')
              IS DISTINCT FROM 'string'
           OR (v_after OPERATOR(pg_catalog.->>) 'created_at') OPERATOR(pg_catalog.!~) c_ts
           OR pg_catalog.jsonb_typeof(v_after OPERATOR(pg_catalog.->) 'item_id')
              IS DISTINCT FROM 'string'
           OR (v_after OPERATOR(pg_catalog.->>) 'item_id') OPERATOR(pg_catalog.!~) c_uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        BEGIN
            v_after_ts := pg_catalog.timezone('UTC', pg_catalog.replace(pg_catalog.replace(
                v_after OPERATOR(pg_catalog.->>) 'created_at', 'T', ' '), 'Z', '')::timestamp);
        EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END;
        -- Canonical form only: re-rendering must give back exactly the input.
        IF """
    + _UTC.format(col="v_after_ts")
    + r""" <> (v_after OPERATOR(pg_catalog.->>) 'created_at') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_after_id := (v_after OPERATOR(pg_catalog.->>) 'item_id')::uuid;
    END IF;

    SELECT pg_catalog.count(*), COALESCE(pg_catalog.jsonb_agg(r.view ORDER BY r.created_at,
               r.item_id) FILTER (WHERE r.n <= v_limit), '[]'::jsonb)
      INTO v_count, v_rows
      FROM (
          SELECT """
    + _ITEM_VIEW
    + r""" AS view, i.created_at, i.item_id,
                 pg_catalog.row_number() OVER (ORDER BY i.created_at, i.item_id) AS n
            FROM {schema}.m05_followup_items AS i
           WHERE (v_states IS NULL OR i.state = ANY (v_states))
             AND (v_assignee_mode = 'any'
                  OR (v_assignee_mode = 'unassigned' AND i.assignee_id IS NULL)
                  OR (v_assignee_mode = 'id' AND i.assignee_id = v_assignee))
             AND (v_after_ts IS NULL OR (i.created_at, i.item_id) > (v_after_ts, v_after_id))
           ORDER BY i.created_at, i.item_id
           LIMIT v_limit + 1
      ) AS r;
    IF v_count > v_limit THEN
        v_next := pg_catalog.jsonb_build_object(
            'created_at', v_rows OPERATOR(pg_catalog.->) (v_limit - 1)
                OPERATOR(pg_catalog.->) 'created_at',
            'item_id', v_rows OPERATOR(pg_catalog.->) (v_limit - 1)
                OPERATOR(pg_catalog.->) 'item_id');
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'items', v_rows,
        'next', v_next);
END;
"""
)


# t015 m05_prepare_action / t016 m05_apply_action (B3a non-resolve actions; design
# delta v1 Δ2-Δ4 with Codex refinements 20261101T010812Z; MA-10 package v1 3.5 +
# v1r1 + v1r2 c5). One template, two renderings: prepare RETURNS a classification
# (REFUSED{code} for domain refusals) and writes nothing; apply RAISES P0001 with
# MESSAGE = code and is self-sufficient. Environment faults (22023 tenant context,
# ISOLATION_UNSUPPORTED, COMPOSITION_UNSUPPORTED) RAISE in both.
# Order: 1 tenant; 1b READ COMMITTED; 1c composition scope (item, key, type,
# assignee, actor) before any lock; 2 closed shape, then reserved resolve types ->
# ACTION_NOT_IMPLEMENTED; 3 item FOR UPDATE; 4 read+act, actor principal; 5 key
# branch (replay / actor mismatch / conflict); 6 NEW: resolved, version,
# precondition, supervise; 6e target eligibility through m01_staff_assignable
# (membership FOR SHARE, after the item lock); 7 writes; 8 snapshot.
_ACTION_TEMPLATE: Final = r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_scope text;
    v_prior text;
    v_keys text[];
    v_action jsonb;
    v_type text;
    v_attr jsonb;
    v_perm jsonb;
    v_item uuid;
    v_key uuid;
    v_version integer;
    v_assignee uuid;
    v_actor uuid;
    v_target uuid;
    v_row record;
    v_stored record;
    v_digest bytea;
    v_state text;
    v_new_assignee uuid;
    v_seq integer;
    v_snapshot jsonb;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;

    -- 1b. READ COMMITTED only (MA-10 v1 3.5): an environment fault, raised.
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ISOLATION_UNSUPPORTED';
    END IF;

    -- 1c. Composition (v1r2 c5): one M05 action on one item per transaction, bound
    -- to item, key, type, assignee and actor; checked before any lock. Diagnostic
    -- guard; the service route is the primary control.
    v_scope := 'm05:' || pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        '["m05.scope/v1",' || pg_catalog.to_json(v_setting)::text
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>)
            'item_id')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>)
            'client_action_id')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.#>>)
            '{action,type}')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.#>>)
            '{action,assignee_id}')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.#>>)
            '{attribution,principal_id}')::text, 'null')
        || ']', 'UTF8')), 'hex');
    v_prior := pg_catalog.current_setting('haloflow.txn_scope', true);
    IF v_prior IS NOT NULL AND v_prior <> '' AND v_prior <> v_scope THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMPOSITION_UNSUPPORTED';
    END IF;
    PERFORM pg_catalog.set_config('haloflow.txn_scope', v_scope, true);

    -- 2. Closed shape.
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        @@REFUSE:COMMAND_MALFORMED@@
    END IF;
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(p_command) AS k;
    IF v_keys IS DISTINCT FROM ARRAY['action', 'attribution', 'client_action_id',
            'expected_version', 'item_id', 'permissions', 'schema_version']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'item_id')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'item_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'client_action_id')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'client_action_id')
          OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'expected_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'expected_version')::text
          OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,9}$'
       OR ((p_command OPERATOR(pg_catalog.->) 'expected_version')::text)::bigint
          > 2147483647 THEN
        @@REFUSE:COMMAND_MALFORMED@@
    END IF;
    v_action := p_command OPERATOR(pg_catalog.->) 'action';
    IF pg_catalog.jsonb_typeof(v_action) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_action OPERATOR(pg_catalog.->) 'type')
          IS DISTINCT FROM 'string' THEN
        @@REFUSE:COMMAND_MALFORMED@@
    END IF;
    v_type := v_action OPERATOR(pg_catalog.->>) 'type';
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(v_action) AS k;
    IF v_type IN ('claim', 'start_progress') THEN
        IF v_keys IS DISTINCT FROM ARRAY['type']::text[] THEN
            @@REFUSE:COMMAND_MALFORMED@@
        END IF;
    ELSIF v_type IN ('assign', 'reassign') THEN
        IF v_keys IS DISTINCT FROM ARRAY['assignee_id', 'type']::text[]
           OR pg_catalog.jsonb_typeof(v_action OPERATOR(pg_catalog.->) 'assignee_id')
              IS DISTINCT FROM 'string'
           OR (v_action OPERATOR(pg_catalog.->>) 'assignee_id')
              OPERATOR(pg_catalog.!~) c_uuid THEN
            @@REFUSE:COMMAND_MALFORMED@@
        END IF;
        v_assignee := (v_action OPERATOR(pg_catalog.->>) 'assignee_id')::uuid;
    ELSIF v_type IN ('resolve', 'claim_and_resolve') THEN
        IF v_keys IS DISTINCT FROM ARRAY['outcome_code', 'type']::text[]
           OR pg_catalog.jsonb_typeof(v_action OPERATOR(pg_catalog.->) 'outcome_code')
              IS DISTINCT FROM 'string'
           OR (v_action OPERATOR(pg_catalog.->>) 'outcome_code') NOT IN ('rescheduled',
              'cancelled_in_ehr', 'reached_no_change', 'no_contact_left_message',
              'no_contact_no_answer', 'other') THEN
            @@REFUSE:COMMAND_MALFORMED@@
        END IF;
    ELSE
        @@REFUSE:COMMAND_MALFORMED@@
    END IF;
    v_perm := p_command OPERATOR(pg_catalog.->) 'permissions';
    IF pg_catalog.jsonb_typeof(v_perm) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k)
             FROM pg_catalog.jsonb_object_keys(v_perm) AS k)
          IS DISTINCT FROM ARRAY['act', 'read', 'supervise']::text[]
       OR pg_catalog.jsonb_typeof(v_perm OPERATOR(pg_catalog.->) 'read')
          IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_perm OPERATOR(pg_catalog.->) 'act')
          IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_perm OPERATOR(pg_catalog.->) 'supervise')
          IS DISTINCT FROM 'boolean' THEN
        @@REFUSE:COMMAND_MALFORMED@@
    END IF;
    v_attr := p_command OPERATOR(pg_catalog.->) 'attribution';
    IF pg_catalog.jsonb_typeof(v_attr) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k)
             FROM pg_catalog.jsonb_object_keys(v_attr) AS k)
          IS DISTINCT FROM ARRAY['auth_method', 'execution_id', 'principal_id',
                                 'principal_kind', 'purpose_code', 'request_id']::text[]
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'principal_kind')
          IS DISTINCT FROM 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') NOT IN ('actor', 'workload')
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'principal_id')
          IS DISTINCT FROM 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'execution_id')
          IS DISTINCT FROM 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'execution_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'auth_method')
          IS DISTINCT FROM 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'auth_method')
          OPERATOR(pg_catalog.!~) '^[a-z][a-z0-9_]{0,31}$'
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'purpose_code')
          IS DISTINCT FROM 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'purpose_code') NOT IN ('treatment', 'payment',
          'operations')
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'request_id')
          NOT IN ('null', 'string')
       OR (pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'request_id') = 'string'
           AND pg_catalog.char_length(v_attr OPERATOR(pg_catalog.->>) 'request_id')
               NOT BETWEEN 1 AND 128) THEN
        @@REFUSE:COMMAND_MALFORMED@@
    END IF;
    v_item := (p_command OPERATOR(pg_catalog.->>) 'item_id')::uuid;
    v_key := (p_command OPERATOR(pg_catalog.->>) 'client_action_id')::uuid;
    v_version := ((p_command OPERATOR(pg_catalog.->) 'expected_version')::text)::integer;
    v_actor := (v_attr OPERATOR(pg_catalog.->>) 'principal_id')::uuid;
    -- Self-assignment is claim (delta 2).
    IF v_type = 'assign' AND v_assignee = v_actor THEN
        @@REFUSE:COMMAND_MALFORMED@@
    END IF;
    -- Reserved for B4 (canonical shape only, after every malformed check).
    IF v_type IN ('resolve', 'claim_and_resolve') THEN
        @@REFUSE:ACTION_NOT_IMPLEMENTED@@
    END IF;

    -- 3. The item lock; unknown and other-tenant ids are identical (schema-local).
    SELECT i.state, i.assignee_id, i.outcome_code, i.version INTO v_row
      FROM {schema}.m05_followup_items AS i
     WHERE i.item_id = v_item
       FOR UPDATE;
    IF NOT FOUND THEN
        @@REFUSE:ITEM_NOT_FOUND@@
    END IF;

    -- 4. Current read+act, actor principals only (defence in depth; not
    -- authentication, D7a).
    IF (v_perm OPERATOR(pg_catalog.->) 'read') <> 'true'::jsonb
       OR (v_perm OPERATOR(pg_catalog.->) 'act') <> 'true'::jsonb
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'actor' THEN
        @@REFUSE:NOT_AUTHORIZED@@
    END IF;
    -- 4b. B3b (package v2 1.3): the actor's CURRENT directory standing (active, read
    -- and act), decided under its membership lock (FOR SHARE, after the item lock),
    -- before the key branch: a revoked or deactivated actor cannot replay either.
    IF NOT {schema}.m01_staff_assignable(v_actor) THEN
        @@REFUSE:NOT_AUTHORIZED@@
    END IF;

    -- 5. Action-key branch: replay returns the stored snapshot, rechecking nothing
    -- else (MR-01, MA-10j).
    v_digest := pg_catalog.sha256(pg_catalog.convert_to(
        '["m05.action/v1",' || pg_catalog.to_json(v_setting)::text
        || ',' || pg_catalog.to_json(v_item::text)::text
        || ',' || pg_catalog.to_json(v_key::text)::text
        || ',' || pg_catalog.to_json(v_actor::text)::text
        || ',' || pg_catalog.to_json(v_type)::text
        || ',' || COALESCE(pg_catalog.to_json(v_assignee::text)::text, 'null')
        || ',' || v_version::text || ']', 'UTF8'));
    SELECT a.actor_id, a.digest, a.result_snapshot INTO v_stored
      FROM {schema}.m05_followup_actions AS a
     WHERE a.item_id = v_item AND a.client_action_id = v_key;
    IF FOUND THEN
        IF v_stored.actor_id <> v_actor THEN
            @@REFUSE:ACTION_KEY_ACTOR_MISMATCH@@
        END IF;
        IF v_stored.digest <> v_digest THEN
            @@REFUSE:ACTION_KEY_CONFLICT@@
        END IF;
        @@REPLAY@@
    END IF;

    -- 6. NEW only.
    IF v_row.state = 'resolved' THEN
        @@REFUSE:ITEM_RESOLVED@@
    END IF;
    IF v_version <> v_row.version THEN
        @@REFUSE:STALE_VERSION@@
    END IF;
    v_state := v_row.state;
    v_new_assignee := v_row.assignee_id;
    IF v_type = 'claim' THEN
        IF v_row.assignee_id IS NOT NULL THEN
            @@REFUSE:PRECONDITION_FAILED@@
        END IF;
        v_target := v_actor;
        v_new_assignee := v_actor;
    ELSIF v_type = 'start_progress' THEN
        IF v_row.state <> 'open' OR v_row.assignee_id IS NULL THEN
            @@REFUSE:PRECONDITION_FAILED@@
        END IF;
        IF v_row.assignee_id <> v_actor
           AND (v_perm OPERATOR(pg_catalog.->) 'supervise') <> 'true'::jsonb THEN
            @@REFUSE:NOT_AUTHORIZED@@
        END IF;
        v_state := 'in_progress';
    ELSIF v_type = 'assign' THEN
        IF v_row.assignee_id IS NOT NULL THEN
            @@REFUSE:PRECONDITION_FAILED@@
        END IF;
        IF (v_perm OPERATOR(pg_catalog.->) 'supervise') <> 'true'::jsonb THEN
            @@REFUSE:NOT_AUTHORIZED@@
        END IF;
        v_target := v_assignee;
        v_new_assignee := v_assignee;
    ELSE
        IF v_row.assignee_id IS NULL OR v_row.assignee_id = v_assignee THEN
            @@REFUSE:PRECONDITION_FAILED@@
        END IF;
        IF (v_perm OPERATOR(pg_catalog.->) 'supervise') <> 'true'::jsonb THEN
            @@REFUSE:NOT_AUTHORIZED@@
        END IF;
        v_target := v_assignee;
        v_new_assignee := v_assignee;
    END IF;

    -- 6e. MA-10: the target is an active member of this clinic with read AND act,
    -- decided under the membership lock (FOR SHARE, after the item lock). A target
    -- equal to the actor (claim; a supervisor reassigning to self) was already locked
    -- and checked at step 4b.
    IF v_target IS NOT NULL AND v_target <> v_actor
       AND NOT {schema}.m01_staff_assignable(v_target) THEN
        @@REFUSE:ASSIGNEE_NOT_ELIGIBLE@@
    END IF;
@@TAIL@@END;
"""

_REFUSE_APPLY: Final = "RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{code}';"
_REFUSE_PREPARE: Final = (
    "RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'decision', 'REFUSED',"
    " 'code', '{code}');"
)
_REPLAY_APPLY: Final = (
    "RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'replayed',\n"
    "            'snapshot', v_stored.result_snapshot);"
)
_REPLAY_PREPARE: Final = (
    "RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'decision', 'REPLAY',\n"
    "            'snapshot', v_stored.result_snapshot);"
)
_TAIL_PREPARE: Final = """
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'decision', 'NEW');
"""
_TAIL_APPLY: Final = r"""
    -- 7. Writes: item, history (seq under the item lock), action record.
    UPDATE {schema}.m05_followup_items AS i
       SET state = v_state, assignee_id = v_new_assignee, version = v_row.version + 1
     WHERE i.item_id = v_item;
    SELECT pg_catalog.max(h.seq) INTO v_seq
      FROM {schema}.m05_followup_history AS h
     WHERE h.item_id = v_item;
    INSERT INTO {schema}.m05_followup_history (history_id, item_id, seq, action, actor_id,
        before, after, resolution_event_id, assertion_kind, created_at)
    VALUES (pg_catalog.gen_random_uuid(), v_item, COALESCE(v_seq, 0) + 1, v_type, v_actor,
        pg_catalog.jsonb_build_object('state', v_row.state,
            'assignee_id', v_row.assignee_id::text, 'outcome_code', v_row.outcome_code),
        pg_catalog.jsonb_build_object('state', v_state,
            'assignee_id', v_new_assignee::text, 'outcome_code', v_row.outcome_code),
        NULL, NULL, pg_catalog.statement_timestamp());
    -- 8. The snapshot: the item after the action, stored and returned verbatim.
    v_snapshot := pg_catalog.jsonb_build_object('schema_version', 1,
        'item_id', v_item::text, 'client_action_id', v_key::text, 'action', v_type,
        'version', v_row.version + 1, 'state', v_state,
        'assignee_id', v_new_assignee::text, 'outcome_code', v_row.outcome_code);
    INSERT INTO {schema}.m05_followup_actions (item_id, client_action_id, actor_id,
        requested_action, digest, hmac_key_version, event_id, result_snapshot, created_at)
    VALUES (v_item, v_key, v_actor, v_type, v_digest, NULL, NULL, v_snapshot,
        pg_catalog.statement_timestamp());
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied',
        'snapshot', v_snapshot);
"""


def _action_body(mode: str) -> str:
    import re

    refuse = _REFUSE_APPLY if mode == "apply" else _REFUSE_PREPARE
    body = re.sub(
        r"@@REFUSE:([A-Z_]+)@@", lambda m: refuse.format(code=m.group(1)), _ACTION_TEMPLATE
    )
    body = body.replace("@@REPLAY@@", _REPLAY_APPLY if mode == "apply" else _REPLAY_PREPARE)
    body = body.replace("@@TAIL@@", _TAIL_APPLY if mode == "apply" else _TAIL_PREPARE)
    assert "@@" not in body and body.isascii()
    return body


_PREPARE_BODY: Final = _action_body("prepare")
_APPLY_BODY: Final = _action_body("apply")


def _template(name: str, params: _Params, volatility: str, body: str) -> str:
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    return (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS jsonb\n"
        f"    LANGUAGE plpgsql {_VOLATILITY_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
        f"GRANT EXECUTE ON FUNCTION {_S}.{name}({argtypes}) TO {RUNTIME_ROLE};\n"
    )


def _definition(
    name: str, params: _Params, volatility: str, body: str, body_sha256: str
) -> UnitDefinition:
    def acl() -> list[dict[str, Any]]:
        return [{"grantee": RUNTIME_ROLE, "privileges": ["EXECUTE"]}]

    return UnitDefinition(
        template=_template(name, params, volatility, body),
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


def _profile(name: str, params: _Params, volatility: str) -> InstalledStateProfile:
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
        installed_acl=(
            (M05_OWNER_ROLE, "EXECUTE", M05_OWNER_ROLE, False),
            (RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),
        ),
    )


# (migration id, function, parameters, volatility). Writes VOLATILE, reads STABLE,
# all PARALLEL UNSAFE (gate ruling 2).
_FUNCTIONS: Final[tuple[tuple[str, str, _Params, str], ...]] = (
    ("t014_m05_create_item", "m05_create_item", (("p_command", "jsonb"),), "volatile"),
    ("t015_m05_prepare_action", "m05_prepare_action", (("p_command", "jsonb"),), "volatile"),
    ("t016_m05_apply_action", "m05_apply_action", (("p_command", "jsonb"),), "volatile"),
    ("t017_m05_get_item", "m05_get_item", (("p_item_id", "uuid"),), "stable"),
    ("t018_m05_list_items", "m05_list_items", (("p_query", "jsonb"),), "stable"),
    ("t019_m05_get_history", "m05_get_history", (("p_item_id", "uuid"),), "stable"),
    ("t020_m05_read_note", "m05_read_note", (("p_note_id", "uuid"),), "stable"),
)

_GET_ITEM_BODY_SHA256: Final = "9f592e2cbda518f631d7e634ac71461f329b35c7d17e457ecc8b2a7f53f15bf6"
_GET_HISTORY_BODY_SHA256: Final = "8982c03dade3192fc79e22bd86f820d7664dfbf859fa1e12f394d7052104df4d"
_LIST_ITEMS_BODY_SHA256: Final = "fca320809dc1d6d44249c3ce927ae3701bf83dc4b933b3ad4ef4dee7ef7d871d"
_PREPARE_BODY_SHA256: Final = "ddc80f19b64c29974d5ae9ee825632dbc2f34e1a16b89997e47ad458d2d0a5ee"
_APPLY_BODY_SHA256: Final = "ba1515e12d9de04e2eb20fb4a4127125791a5c0e6aed0e508600abf5a77be24f"

# Bodies by function. A function absent here is still the B1 fail-closed stub; each
# tranche moves exactly the functions its gated tests cover.
_BODIES: Final = {
    "m05_create_item": (_CREATE_BODY, _CREATE_BODY_SHA256),
    "m05_prepare_action": (_PREPARE_BODY, _PREPARE_BODY_SHA256),
    "m05_apply_action": (_APPLY_BODY, _APPLY_BODY_SHA256),
    "m05_get_item": (_GET_ITEM_BODY, _GET_ITEM_BODY_SHA256),
    "m05_get_history": (_GET_HISTORY_BODY, _GET_HISTORY_BODY_SHA256),
    "m05_list_items": (_LIST_ITEMS_BODY, _LIST_ITEMS_BODY_SHA256),
}

M05_GATEWAY_UNITS: Final = MappingProxyType(
    {
        migration_id: _definition(
            name, params, volatility, *_BODIES.get(name, (_STUB_BODY, _STUB_BODY_SHA256))
        )
        for migration_id, name, params, volatility in _FUNCTIONS
    }
)
M05_GATEWAY_PROFILES: Final = MappingProxyType(
    {
        migration_id: _profile(name, params, volatility)
        for migration_id, name, params, volatility in _FUNCTIONS
    }
)
