"""UC1 part A (M02 minimal observation append): the four typed gateway units.

One SECURITY DEFINER function per typed unit, each with its own execution role and
installed-state profile (Codex ALIGN 20261031T131639Z: C1, O1, B1):

- ``t008_m02_canon``          ``m02_canon_v1(jsonb) -> text``, append owner; EXECUTE
                              projection owner. The exact m02.canon/v1 characters;
                              the canonical BYTES are ``convert_to(result, 'UTF8')``.
                              Pure: no table access, no dynamic SQL, no role or
                              setting change; the recursion is schema-qualified.
- ``t009_m02_append``         ``m02_append_observation(jsonb) -> jsonb``, append owner;
                              EXECUTE runtime. Package v1 section 1.1 with delta v1r1
                              and decisions TB, CN, AL, RG and G1. Its single enqueue
                              call follows the frozen marker ``-- t008:enqueue-call``
                              (PA-05(a) seam; the marker text predates the C1 split).
- ``t010_m02_enqueue``        ``m02_enqueue_event_handoffs(uuid, jsonb) -> jsonb``,
                              projection owner; EXECUTE append owner. TB-1 registry
                              binding before any outbox access; kind-inclusive M11 key;
                              HANDOFF_KEY_COLLISION on an incompatible existing row.
- ``t011_m02_envelope_read``  ``m02_get_observation_envelope(uuid) -> jsonb``, read
                              owner; EXECUTE M05 owner. Allowlisted paths only; an
                              inactive type, or a type/version with no allowlist row,
                              is ENVELOPE_UNSUPPORTED; unknown or foreign ids are null.

Every body checks ``app.tenant_id`` well-formedness first (22023, the t003 pattern;
not authentication). Refusals are P0001 with MESSAGE exactly the code.

Forward reference, deliberate: t009's body calls t010's function, installed later
in the same provisioning run. PL/pgSQL resolves a name when the statement first
executes, not at CREATE. The PG17 install and call chain are proven by the
postgres rows, not assumed here.

Each body is reviewed text and its SHA-256 (of the normalized body) is a reviewed
literal, so a changed body fails composition until the literal is updated.
"""

from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition
from haloflow.m02.roles import (
    APPEND_OWNER_ROLE,
    M05_OWNER_ROLE,
    PROJECTION_OWNER_ROLE,
    READ_OWNER_ROLE,
)

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_VOLATILITY_WORD: Final = {"immutable": "IMMUTABLE", "stable": "STABLE", "volatile": "VOLATILE"}
_VOLATILITY_CODE: Final = {"immutable": "i", "stable": "s", "volatile": "v"}
_PARALLEL_CODE: Final = {"safe": "s", "restricted": "r", "unsafe": "u"}
_Params = tuple[tuple[str, str], ...]


def _template(
    name: str,
    params: _Params,
    returns: str,
    volatility: str,
    parallel: str,
    body: str,
    grantee: str,
) -> str:
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    return (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS {returns}\n"
        f"    LANGUAGE plpgsql {_VOLATILITY_WORD[volatility]} PARALLEL {parallel.upper()}"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
        f"GRANT EXECUTE ON FUNCTION {_S}.{name}({argtypes}) TO {grantee};\n"
    )


def _definition(
    *,
    name: str,
    params: _Params,
    returns: str,
    volatility: str,
    parallel: str,
    body: str,
    body_sha256: str,
    owner: str,
    grantee: str,
) -> UnitDefinition:
    def acl() -> list[dict[str, Any]]:
        return [{"grantee": grantee, "privileges": ["EXECUTE"]}]

    return UnitDefinition(
        template=_template(name, params, returns, volatility, parallel, body, grantee),
        execution_role=owner,
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
                    "return_type": returns,
                    "language": "plpgsql",
                    "is_procedure": False,
                    "replace": False,
                    "security_definer": True,
                    "volatility": volatility,
                    "parallel": parallel,
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
                    "owner": owner,
                    "security_definer": True,
                    "config": [_CONFIG],
                    "acl": acl(),
                    "body": body,
                }
            ],
        },
    )


def _profile(
    *,
    name: str,
    params: _Params,
    returns: str,
    volatility: str,
    parallel: str,
    owner: str,
    grantee: str,
) -> InstalledStateProfile:
    return InstalledStateProfile(
        function_name=name,
        argument_types=tuple(t for _, t in params),
        owner=owner,
        execution_role=owner,
        prokind="f",
        return_type=returns,
        returns_set=False,
        language="plpgsql",
        security_definer=True,
        volatility=_VOLATILITY_CODE[volatility],
        parallel=_PARALLEL_CODE[parallel],
        strict=False,
        installed_acl=(
            (owner, "EXECUTE", owner, False),
            (grantee, "EXECUTE", owner, False),
        ),
    )


# ---------------------------------------------------------------------------
# t008_m02_canon: m02_canon_v1
# ---------------------------------------------------------------------------

T008_MIGRATION_ID: Final = "t008_m02_canon"
_T008_NAME: Final = "m02_canon_v1"
_T008_PARAMS: Final[_Params] = (("p_value", "jsonb"),)
_T008_BODY: Final = r"""
DECLARE
    v_kind text;
    v_num numeric;
    v_text text;
    v_code integer;
BEGIN
    IF p_value IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_kind := pg_catalog.jsonb_typeof(p_value);
    IF v_kind = 'null' THEN
        RETURN 'null';
    ELSIF v_kind = 'boolean' THEN
        RETURN p_value OPERATOR(pg_catalog.#>>) '{}'::text[];
    ELSIF v_kind = 'number' THEN
        v_num := (p_value OPERATOR(pg_catalog.#>>) '{}'::text[])::numeric;
        IF pg_catalog.scale(v_num) <> 0
           OR pg_catalog.abs(v_num) > 9007199254740991::numeric THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        RETURN v_num::text;
    ELSIF v_kind = 'string' THEN
        v_text := p_value OPERATOR(pg_catalog.#>>) '{}'::text[];
        v_text := pg_catalog.replace(v_text, pg_catalog.chr(92), pg_catalog.chr(92)
            || pg_catalog.chr(92));
        v_text := pg_catalog.replace(v_text, pg_catalog.chr(34), pg_catalog.chr(92)
            || pg_catalog.chr(34));
        v_text := pg_catalog.replace(v_text, pg_catalog.chr(8), pg_catalog.chr(92) || 'b');
        v_text := pg_catalog.replace(v_text, pg_catalog.chr(9), pg_catalog.chr(92) || 't');
        v_text := pg_catalog.replace(v_text, pg_catalog.chr(10), pg_catalog.chr(92) || 'n');
        v_text := pg_catalog.replace(v_text, pg_catalog.chr(12), pg_catalog.chr(92) || 'f');
        v_text := pg_catalog.replace(v_text, pg_catalog.chr(13), pg_catalog.chr(92) || 'r');
        FOR v_code IN 1..31 LOOP
            IF v_code NOT IN (8, 9, 10, 12, 13) THEN
                v_text := pg_catalog.replace(v_text, pg_catalog.chr(v_code),
                    pg_catalog.chr(92) || 'u' || pg_catalog.lpad(pg_catalog.to_hex(v_code), 4,
                        '0'));
            END IF;
        END LOOP;
        RETURN pg_catalog.chr(34) || v_text || pg_catalog.chr(34);
    ELSIF v_kind = 'array' THEN
        SELECT pg_catalog.string_agg({schema}.m02_canon_v1(e.value), ',' ORDER BY e.ordinality)
          INTO v_text
          FROM pg_catalog.jsonb_array_elements(p_value) WITH ORDINALITY AS e(value, ordinality);
        RETURN '[' || COALESCE(v_text, '') || ']';
    ELSIF v_kind = 'object' THEN
        SELECT pg_catalog.string_agg(
                   {schema}.m02_canon_v1(pg_catalog.to_jsonb(o.key)) || ':'
                       || {schema}.m02_canon_v1(o.value),
                   ',' ORDER BY k.utf16 COLLATE pg_catalog."C")
          INTO v_text
          FROM pg_catalog.jsonb_each(p_value) AS o(key, value)
          CROSS JOIN LATERAL (
              SELECT COALESCE(pg_catalog.string_agg(
                         CASE WHEN c.cp > 65535 THEN
                             pg_catalog.lpad(pg_catalog.to_hex(55296 + ((c.cp - 65536) >> 10)), 4,
                                 '0')
                             || pg_catalog.lpad(pg_catalog.to_hex(56320 + ((c.cp - 65536) & 1023)),
                                 4, '0')
                         ELSE pg_catalog.lpad(pg_catalog.to_hex(c.cp), 4, '0') END,
                         '' ORDER BY c.n), '') AS utf16
                FROM (SELECT pg_catalog.ascii(ch.ch) AS cp, ch.n
                        FROM pg_catalog.regexp_split_to_table(o.key, '') WITH ORDINALITY
                             AS ch(ch, n)) AS c
          ) AS k;
        RETURN '{' || COALESCE(v_text, '') || '}';
    END IF;
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
END;
"""
_T008_BODY_SHA256: Final = "477dedd21fd4ed131c1f0d757861db7414539fa494b0b4a46be29bd975c91296"
T008_DEFINITION: Final = _definition(
    name=_T008_NAME,
    params=_T008_PARAMS,
    returns="text",
    volatility="immutable",
    parallel="safe",
    body=_T008_BODY,
    body_sha256=_T008_BODY_SHA256,
    owner=APPEND_OWNER_ROLE,
    grantee=PROJECTION_OWNER_ROLE,
)
T008_PROFILE: Final = _profile(
    name=_T008_NAME,
    params=_T008_PARAMS,
    returns="text",
    volatility="immutable",
    parallel="safe",
    owner=APPEND_OWNER_ROLE,
    grantee=PROJECTION_OWNER_ROLE,
)


# ---------------------------------------------------------------------------
# t009_m02_append: m02_append_observation
# ---------------------------------------------------------------------------

T009_MIGRATION_ID: Final = "t009_m02_append"
_T009_NAME: Final = "m02_append_observation"
_T009_PARAMS: Final[_Params] = (("p_command", "jsonb"),)
_T009_BODY: Final = r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_event_id uuid;
    v_event_type text;
    v_contract_version integer;
    v_operation_id uuid;
    v_patient_id uuid;
    v_correlation_id uuid;
    v_causation_id uuid;
    v_dedupe_key text;
    v_producer text;
    v_occurred_at timestamptz;
    v_quality text;
    v_links jsonb;
    v_metadata jsonb;
    v_attr jsonb;
    v_contract record;
    v_level_active boolean;
    v_status_ok boolean;
    v_principal_kind text;
    v_principal_id uuid;
    v_actor_id uuid;
    v_workload_id uuid;
    v_auth_method text;
    v_schema jsonb;
    v_prop jsonb;
    v_key text;
    v_value jsonb;
    v_link jsonb;
    v_resolver record;
    v_target record;
    v_group jsonb;
    v_declared text[];
    v_link_types text[] := ARRAY[]::text[];
    v_fingerprint bytea;
    v_existing record;
    v_inserted integer;
    v_result_event_id uuid;
    v_outcome text;
    v_items jsonb;
    v_handoff_state text := 'none';
BEGIN
    -- 1. Tenant setting: well-formedness only (not authentication); t003 pattern.
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;

    -- 2. Closed command shape (package v1 section 1.1).
    IF p_command IS NULL OR pg_catalog.jsonb_typeof(p_command) <> 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k)
             FROM pg_catalog.jsonb_object_keys(p_command) AS k)
          IS DISTINCT FROM ARRAY['attribution', 'causation_event_id', 'contract_version',
              'correlation_id', 'event_id', 'event_type', 'links', 'metadata',
              'occurred_at', 'operation_id', 'patient_id', 'producer_dedupe_key',
              'producer_service', 'schema_version', 'source_time_quality']::text[]
       OR p_command OPERATOR(pg_catalog.->) 'schema_version' IS DISTINCT FROM '1'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'event_id') <> 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'event_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'event_type') <> 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'event_type')
          OPERATOR(pg_catalog.!~) '^[a-z][a-z0-9_]{1,127}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'contract_version') <> 'number'
       OR pg_catalog.scale((p_command OPERATOR(pg_catalog.->>) 'contract_version')::numeric) <> 0
       OR (p_command OPERATOR(pg_catalog.->>) 'contract_version')::numeric NOT BETWEEN 1
           AND 2147483647
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'producer_dedupe_key')
           <> 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'producer_dedupe_key')
          OPERATOR(pg_catalog.!~) '^[a-z0-9:]+$'
       OR pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'producer_dedupe_key') > 256
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'producer_service') <> 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'producer_service')
          OPERATOR(pg_catalog.!~) '^[a-z][a-z0-9_]{0,63}$'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'source_time_quality')
           <> 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'source_time_quality')
          NOT IN ('trusted', 'estimated', 'absent', 'invalid')
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'links') <> 'array'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'metadata') <> 'object' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    FOREACH v_key IN ARRAY ARRAY['operation_id', 'patient_id', 'correlation_id',
                                 'causation_event_id']::text[] LOOP
        v_value := p_command OPERATOR(pg_catalog.->) v_key;
        IF pg_catalog.jsonb_typeof(v_value) NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(v_value) = 'string'
               AND (v_value OPERATOR(pg_catalog.#>>) '{}'::text[])
                   OPERATOR(pg_catalog.!~) c_uuid) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END LOOP;
    v_value := p_command OPERATOR(pg_catalog.->) 'occurred_at';
    IF pg_catalog.jsonb_typeof(v_value) = 'string' THEN
        IF (v_value OPERATOR(pg_catalog.#>>) '{}'::text[]) OPERATOR(pg_catalog.!~)
           ('^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
            || '(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        BEGIN
            v_occurred_at := (v_value OPERATOR(pg_catalog.#>>) '{}'::text[])::timestamptz;
        EXCEPTION WHEN data_exception THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END;
    ELSIF pg_catalog.jsonb_typeof(v_value) <> 'null' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_event_id := (p_command OPERATOR(pg_catalog.->>) 'event_id')::uuid;
    v_event_type := p_command OPERATOR(pg_catalog.->>) 'event_type';
    v_contract_version := (p_command OPERATOR(pg_catalog.->>) 'contract_version')::integer;
    v_operation_id := (p_command OPERATOR(pg_catalog.->>) 'operation_id')::uuid;
    v_patient_id := (p_command OPERATOR(pg_catalog.->>) 'patient_id')::uuid;
    v_correlation_id := (p_command OPERATOR(pg_catalog.->>) 'correlation_id')::uuid;
    v_causation_id := (p_command OPERATOR(pg_catalog.->>) 'causation_event_id')::uuid;
    v_dedupe_key := p_command OPERATOR(pg_catalog.->>) 'producer_dedupe_key';
    v_producer := p_command OPERATOR(pg_catalog.->>) 'producer_service';
    v_quality := p_command OPERATOR(pg_catalog.->>) 'source_time_quality';
    v_links := p_command OPERATOR(pg_catalog.->) 'links';
    v_metadata := p_command OPERATOR(pg_catalog.->) 'metadata';
    v_attr := p_command OPERATOR(pg_catalog.->) 'attribution';

    -- 3. Contract: active type, effective, observation/completed, part-A routes only.
    SELECT c.event_type, c.version, c.event_level, c.allowed_statuses, c.required_fields,
           c.required_links, c.optional_links, c.metadata_json_schema, c.metadata_max_bytes,
           c.authorized_producers, c.projection_rule_id, c.effective_from, c.retired_at,
           c.signal_rule, c.value_constraints, t.is_active
      INTO v_contract
      FROM {schema}.event_contracts AS c
      JOIN {schema}.event_types AS t ON t.event_type = c.event_type
     WHERE c.event_type = v_event_type AND c.version = v_contract_version;
    IF NOT FOUND OR v_contract.is_active IS DISTINCT FROM true
       OR v_contract.effective_from > pg_catalog.statement_timestamp()
       OR (v_contract.retired_at IS NOT NULL
           AND v_contract.retired_at <= pg_catalog.statement_timestamp())
       OR v_contract.event_level IS DISTINCT FROM 'observation'
       OR v_contract.allowed_statuses IS DISTINCT FROM ARRAY['completed']::varchar(64)[]
       OR v_contract.projection_rule_id IS NOT NULL
       OR (v_contract.signal_rule IS NOT NULL AND v_contract.signal_rule IS DISTINCT FROM
           '{"handoff_kind":"event_signal","signal_type":"m02.event.appended"}'::jsonb)
       OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_contract.value_constraints) AS k
                   WHERE k NOT IN ('operation_id', 'synthetic_intent'))
       OR (v_contract.value_constraints OPERATOR(pg_catalog.?) 'operation_id'
           AND (pg_catalog.jsonb_typeof(v_contract.value_constraints
               OPERATOR(pg_catalog.->) 'operation_id') <> 'object'
                OR (SELECT pg_catalog.array_agg(k) FROM pg_catalog.jsonb_object_keys(
                        v_contract.value_constraints OPERATOR(pg_catalog.->) 'operation_id') AS k)
                   IS DISTINCT FROM ARRAY['must_be_null']::text[]
                OR pg_catalog.jsonb_typeof(v_contract.value_constraints
                       OPERATOR(pg_catalog.#>) '{operation_id,must_be_null}'::text[]) <> 'boolean'))
       OR (v_contract.value_constraints OPERATOR(pg_catalog.?) 'synthetic_intent'
           AND (pg_catalog.jsonb_typeof(v_contract.value_constraints
               OPERATOR(pg_catalog.->) 'synthetic_intent') <> 'object'
                OR (SELECT pg_catalog.array_agg(k) FROM pg_catalog.jsonb_object_keys(
                        v_contract.value_constraints OPERATOR(pg_catalog.->) 'synthetic_intent')
                            AS k)
                   IS DISTINCT FROM ARRAY['forbidden']::text[]
                OR pg_catalog.jsonb_typeof(v_contract.value_constraints
                       OPERATOR(pg_catalog.#>) '{synthetic_intent,forbidden}'::text[])
                           <> 'boolean')) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTRACT_UNSUPPORTED';
    END IF;
    SELECT l.is_active INTO v_level_active
      FROM {schema}.ref_event_levels AS l WHERE l.code = 'observation';
    SELECT s.is_active AND s.status_scope IN ('stored_event', 'both') INTO v_status_ok
      FROM {schema}.ref_event_statuses AS s WHERE s.code = 'completed';
    IF v_level_active IS DISTINCT FROM true OR v_status_ok IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTRACT_UNSUPPORTED';
    END IF;

    -- 4. Producer: from trusted service context, and authorized by the contract.
    IF NOT (v_producer = ANY (v_contract.authorized_producers)) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRODUCER_NOT_AUTHORIZED';
    END IF;

    -- 5. Attribution (consolidated section 3 shape).
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
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ATTRIBUTION_INVALID';
    END IF;
    v_principal_kind := v_attr OPERATOR(pg_catalog.->>) 'principal_kind';
    v_principal_id := (v_attr OPERATOR(pg_catalog.->>) 'principal_id')::uuid;
    v_auth_method := v_attr OPERATOR(pg_catalog.->>) 'auth_method';
    IF v_principal_kind = 'actor' THEN
        v_actor_id := v_principal_id;
    ELSE
        v_workload_id := v_principal_id;
    END IF;

    -- 6. Field rules from the contract.
    IF v_correlation_id IS NULL
       OR (v_occurred_at IS NULL) <> (v_quality = 'absent')
       OR (v_contract.value_constraints
           OPERATOR(pg_catalog.#>>) '{operation_id,must_be_null}'::text[] = 'true'
           AND v_operation_id IS NOT NULL) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    FOREACH v_key IN ARRAY v_contract.required_fields LOOP
        IF v_key NOT IN ('operation_id', 'patient_id', 'correlation_id', 'causation_event_id',
                         'occurred_at') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTRACT_UNSUPPORTED';
        END IF;
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) v_key) = 'null' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END LOOP;

    -- 7. Metadata: the contract's closed schema (supported keyword subset; any other
    -- keyword fails closed as CONTRACT_UNSUPPORTED) and the size bound.
    v_schema := v_contract.metadata_json_schema;
    IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_schema) AS k
                WHERE k NOT IN ('type', 'additionalProperties', 'properties', 'required'))
       OR v_schema OPERATOR(pg_catalog.->) 'type' IS DISTINCT FROM '"object"'::jsonb
       OR v_schema OPERATOR(pg_catalog.->) 'additionalProperties' IS DISTINCT FROM 'false'::jsonb
       OR pg_catalog.jsonb_typeof(COALESCE(v_schema OPERATOR(pg_catalog.->) 'properties',
           '{}'::jsonb)) <> 'object'
       OR pg_catalog.jsonb_typeof(COALESCE(v_schema OPERATOR(pg_catalog.->) 'required',
           '[]'::jsonb)) <> 'array' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTRACT_UNSUPPORTED';
    END IF;
    FOR v_key, v_prop IN
        SELECT p.key, p.value
          FROM pg_catalog.jsonb_each(COALESCE(v_schema OPERATOR(pg_catalog.->) 'properties',
              '{}'::jsonb)) AS p
    LOOP
        IF pg_catalog.jsonb_typeof(v_prop) <> 'object'
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_prop) AS k
                       WHERE k NOT IN ('enum', 'const', 'type'))
           OR (v_prop OPERATOR(pg_catalog.?) 'enum'
               AND pg_catalog.jsonb_typeof(v_prop OPERATOR(pg_catalog.->) 'enum') <> 'array')
           OR (v_prop OPERATOR(pg_catalog.?) 'type'
               AND (v_prop OPERATOR(pg_catalog.->>) 'type') NOT IN ('string', 'integer',
                   'boolean')) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTRACT_UNSUPPORTED';
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_metadata) AS k
                WHERE NOT (COALESCE(v_schema OPERATOR(pg_catalog.->) 'properties', '{}'::jsonb)
                           OPERATOR(pg_catalog.?) k))
       OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(
                      COALESCE(v_schema OPERATOR(pg_catalog.->) 'required', '[]'::jsonb)) AS r(name)
                   WHERE pg_catalog.jsonb_typeof(r.name) <> 'string'
                      OR NOT (v_metadata OPERATOR(pg_catalog.?) (r.name
                          OPERATOR(pg_catalog.#>>) '{}'::text[]))) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    FOR v_key, v_value IN SELECT m.key, m.value FROM pg_catalog.jsonb_each(v_metadata) AS m LOOP
        v_prop := v_schema OPERATOR(pg_catalog.#>) ARRAY['properties', v_key]::text[];
        IF (v_prop OPERATOR(pg_catalog.?) 'enum' AND NOT EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_prop
                    OPERATOR(pg_catalog.->) 'enum') AS e(value)
                 WHERE e.value = v_value))
           OR (v_prop OPERATOR(pg_catalog.?) 'const'
               AND v_value IS DISTINCT FROM v_prop OPERATOR(pg_catalog.->) 'const')
           OR ((v_prop OPERATOR(pg_catalog.->>) 'type') = 'string'
               AND pg_catalog.jsonb_typeof(v_value) <> 'string')
           OR ((v_prop OPERATOR(pg_catalog.->>) 'type') = 'boolean'
               AND pg_catalog.jsonb_typeof(v_value) <> 'boolean')
           OR ((v_prop OPERATOR(pg_catalog.->>) 'type') = 'integer'
               AND (pg_catalog.jsonb_typeof(v_value) <> 'number'
                    OR pg_catalog.scale((v_value
                        OPERATOR(pg_catalog.#>>) '{}'::text[])::numeric) <> 0)) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END LOOP;
    IF pg_catalog.octet_length(pg_catalog.convert_to({schema}.m02_canon_v1(v_metadata), 'UTF8'))
       > v_contract.metadata_max_bytes THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;

    -- 8. Causation must be an existing event of this tenant (schema-local).
    IF v_causation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM {schema}.patient_events AS e WHERE e.event_id = v_causation_id) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;

    -- 9. Links: shape, declared and active resolvers, typed G1 targets, then groups.
    FOR v_link IN SELECT e.value FROM pg_catalog.jsonb_array_elements(v_links) AS e(value) LOOP
        IF pg_catalog.jsonb_typeof(v_link) <> 'object'
           OR (SELECT pg_catalog.array_agg(k ORDER BY k)
               FROM pg_catalog.jsonb_object_keys(v_link) AS k)
              IS DISTINCT FROM ARRAY['link_id', 'link_type', 'relationship']::text[]
           OR pg_catalog.jsonb_typeof(v_link OPERATOR(pg_catalog.->) 'link_type') <> 'string'
           OR pg_catalog.jsonb_typeof(v_link OPERATOR(pg_catalog.->) 'relationship') <> 'string'
           OR pg_catalog.jsonb_typeof(v_link OPERATOR(pg_catalog.->) 'link_id') <> 'string'
           OR (v_link OPERATOR(pg_catalog.->>) 'link_id') OPERATOR(pg_catalog.!~) c_uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END LOOP;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_array_elements(v_links))
       <> (SELECT pg_catalog.count(DISTINCT e.value)
           FROM pg_catalog.jsonb_array_elements(v_links) AS e(value)) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(d.name), ARRAY[]::text[]) INTO v_declared
      FROM (SELECT g.member OPERATOR(pg_catalog.#>>) '{}'::text[] AS name
              FROM pg_catalog.jsonb_array_elements(v_contract.required_links) AS r(grp)
              CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(r.grp) AS g(member)
            UNION
            SELECT o.member OPERATOR(pg_catalog.#>>) '{}'::text[]
              FROM pg_catalog.jsonb_array_elements(v_contract.optional_links) AS o(member)) AS d;
    FOR v_link IN SELECT e.value FROM pg_catalog.jsonb_array_elements(v_links) AS e(value) LOOP
        SELECT r.relationship, r.resolver_kind, r.is_active INTO v_resolver
          FROM {schema}.m02_link_resolvers AS r
         WHERE r.resolver_id = v_link OPERATOR(pg_catalog.->>) 'link_type';
        IF NOT FOUND OR v_resolver.is_active IS DISTINCT FROM true
           OR v_resolver.resolver_kind IS DISTINCT FROM 'synthetic_fixture'
           OR v_resolver.relationship IS DISTINCT FROM v_link
               OPERATOR(pg_catalog.->>) 'relationship'
           OR NOT ((v_link OPERATOR(pg_catalog.->>) 'link_type') = ANY (v_declared)) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'LINK_UNSUPPORTED';
        END IF;
        v_link_types := v_link_types || (v_link OPERATOR(pg_catalog.->>) 'link_type');
    END LOOP;
    FOR v_link IN SELECT e.value FROM pg_catalog.jsonb_array_elements(v_links) AS e(value) LOOP
        IF pg_catalog.to_regclass('{schema}.m02_test_link_targets') IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'LINK_INVALID';
        END IF;
        SELECT t.subject_kind, t.authorized_principal, t.source_class INTO v_target
          FROM {schema}.m02_test_link_targets AS t
         WHERE t.resolver_id = v_link OPERATOR(pg_catalog.->>) 'link_type'
           AND t.link_id = (v_link OPERATOR(pg_catalog.->>) 'link_id')::uuid;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'LINK_INVALID';
        END IF;
        IF (v_link OPERATOR(pg_catalog.->>) 'link_type') = 'm02_test_subject_v1' THEN
            IF v_target.subject_kind IS DISTINCT FROM 'patient'
               OR (v_target.authorized_principal IS NOT NULL
                   AND v_target.authorized_principal <> v_principal_id) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'LINK_INVALID';
            END IF;
        ELSIF (v_link OPERATOR(pg_catalog.->>) 'link_type') = 'm02_test_source_v1' THEN
            IF v_target.source_class IS DISTINCT FROM 'synthetic_inbound' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'LINK_INVALID';
            END IF;
        ELSE
            -- No other resolver has part-A semantics (G1); fail closed.
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'LINK_INVALID';
        END IF;
    END LOOP;
    FOR v_group IN SELECT r.grp FROM pg_catalog.jsonb_array_elements(v_contract.required_links)
        AS r(grp) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(v_group) AS g(member)
                        WHERE (g.member OPERATOR(pg_catalog.#>>) '{}'::text[])
                            = ANY (v_link_types)) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'REQUIRED_LINK_MISSING';
        END IF;
    END LOOP;

    -- 10. Content fingerprint (delta v1r1 Delta2-F; m02.canon/v1).
    v_fingerprint := pg_catalog.sha256(pg_catalog.convert_to({schema}.m02_canon_v1(
        pg_catalog.jsonb_build_array(
            'm02.event-content/v1',
            v_event_type,
            v_contract_version::text,
            'observation',
            'completed',
            v_operation_id::text,
            v_patient_id::text,
            v_causation_id::text,
            v_producer,
            v_actor_id::text,
            v_workload_id::text,
            pg_catalog.to_char(pg_catalog.timezone('UTC', v_occurred_at),
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            v_quality,
            (SELECT COALESCE(pg_catalog.jsonb_agg(x.item ORDER BY
                        pg_catalog.convert_to({schema}.m02_canon_v1(x.item), 'UTF8')), '[]'::jsonb)
               FROM (SELECT pg_catalog.jsonb_build_array(
                                e.value OPERATOR(pg_catalog.->>) 'link_type',
                                e.value OPERATOR(pg_catalog.->>) 'link_id',
                                e.value OPERATOR(pg_catalog.->>) 'relationship') AS item
                       FROM pg_catalog.jsonb_array_elements(v_links) AS e(value)) AS x),
            v_metadata)), 'UTF8'));

    -- 11. Dedupe per producer (design 4.6 item 3), then append.
    SELECT e.producer_service, e.producer_dedupe_key INTO v_existing
      FROM {schema}.patient_events AS e WHERE e.event_id = v_event_id;
    IF FOUND AND (v_existing.producer_service IS DISTINCT FROM v_producer
                  OR v_existing.producer_dedupe_key IS DISTINCT FROM v_dedupe_key) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DEDUPE_CONFLICT';
    END IF;
    INSERT INTO {schema}.patient_events (
        event_id, operation_id, patient_id, event_type, contract_version, event_level,
        status, correlation_id, causation_event_id, producer_dedupe_key,
        content_fingerprint, producer_service, actor_id, workload_id, auth_method,
        occurred_at, source_time_quality, received_at, metadata)
    VALUES (
        v_event_id, v_operation_id, v_patient_id, v_event_type, v_contract_version,
        'observation', 'completed', v_correlation_id, v_causation_id, v_dedupe_key,
        v_fingerprint, v_producer, v_actor_id, v_workload_id, v_auth_method,
        v_occurred_at, v_quality, pg_catalog.statement_timestamp(), v_metadata)
    ON CONFLICT (producer_service, producer_dedupe_key)
        WHERE producer_dedupe_key IS NOT NULL DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    IF v_inserted = 1 THEN
        INSERT INTO {schema}.patient_event_links (event_id, link_type, link_id, relationship)
        SELECT v_event_id, e.value OPERATOR(pg_catalog.->>) 'link_type',
               (e.value OPERATOR(pg_catalog.->>) 'link_id')::uuid,
               e.value OPERATOR(pg_catalog.->>) 'relationship'
          FROM pg_catalog.jsonb_array_elements(v_links) AS e(value);
        v_result_event_id := v_event_id;
        v_outcome := 'appended';
    ELSE
        SELECT e.event_id, e.content_fingerprint INTO v_existing
          FROM {schema}.patient_events AS e
         WHERE e.producer_service = v_producer AND e.producer_dedupe_key = v_dedupe_key;
        IF NOT FOUND OR v_existing.content_fingerprint IS DISTINCT FROM v_fingerprint THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'DEDUPE_CONFLICT';
        END IF;
        v_result_event_id := v_existing.event_id;
        v_outcome := 'existing';
    END IF;

    -- 12. Handoffs derived from the contract, never from caller input. Both branches
    -- pass through the registry-bound enqueue (TB-2; Codex XB-1).
    IF v_contract.signal_rule IS NOT NULL THEN
        v_items := pg_catalog.jsonb_build_array(v_contract.signal_rule);
        -- t008:enqueue-call
        PERFORM {schema}.m02_enqueue_event_handoffs(v_result_event_id, v_items);
        v_handoff_state := 'enqueued';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema_version', 1,
        'event_id', v_result_event_id::text,
        'appended_or_existing', v_outcome,
        'handoff_state', v_handoff_state);
END;
"""
_T009_BODY_SHA256: Final = "5358f2e2ba26419dcad64b89a4fd9d52abf76ba609802e6f06bd0c5c019633f3"
T009_DEFINITION: Final = _definition(
    name=_T009_NAME,
    params=_T009_PARAMS,
    returns="jsonb",
    volatility="volatile",
    parallel="unsafe",
    body=_T009_BODY,
    body_sha256=_T009_BODY_SHA256,
    owner=APPEND_OWNER_ROLE,
    grantee=RUNTIME_ROLE,
)
T009_PROFILE: Final = _profile(
    name=_T009_NAME,
    params=_T009_PARAMS,
    returns="jsonb",
    volatility="volatile",
    parallel="unsafe",
    owner=APPEND_OWNER_ROLE,
    grantee=RUNTIME_ROLE,
)


# ---------------------------------------------------------------------------
# t010_m02_enqueue: m02_enqueue_event_handoffs
# ---------------------------------------------------------------------------

T010_MIGRATION_ID: Final = "t010_m02_enqueue"
_T010_NAME: Final = "m02_enqueue_event_handoffs"
_T010_PARAMS: Final[_Params] = (("p_source_event_id", "uuid"), ("p_items", "jsonb"))
_T010_BODY: Final = r"""
DECLARE
    v_setting text;
    v_tenant text;
    v_state text;
    v_item jsonb;
    v_kind text;
    v_signal text;
    v_key text;
    v_payload jsonb;
    v_inserted integer;
    v_found record;
    v_count integer := 0;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    SELECT t.tenant_id, t.lifecycle_state INTO v_tenant, v_state
      FROM shared.tenants AS t
     WHERE t.schema_key = '{schema}';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'tenant binding absent';
    END IF;
    IF v_state <> 'active' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'tenant binding inactive';
    END IF;
    IF v_tenant <> v_setting THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'tenant binding mismatch';
    END IF;
    IF p_source_event_id IS NULL OR p_items IS NULL
       OR pg_catalog.jsonb_typeof(p_items) <> 'array'
       OR pg_catalog.jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    FOR v_item IN SELECT e.value FROM pg_catalog.jsonb_array_elements(p_items) AS e(value) LOOP
        IF pg_catalog.jsonb_typeof(v_item) <> 'object'
           OR (SELECT pg_catalog.array_agg(k ORDER BY k)
                 FROM pg_catalog.jsonb_object_keys(v_item) AS k)
              IS DISTINCT FROM ARRAY['handoff_kind', 'signal_type']::text[] THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_kind := v_item OPERATOR(pg_catalog.->>) 'handoff_kind';
        v_signal := v_item OPERATOR(pg_catalog.->>) 'signal_type';
        -- The closed part-A map: one kind, one signal type.
        IF v_kind IS DISTINCT FROM 'event_signal'
           OR v_signal IS DISTINCT FROM 'm02.event.appended' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_key := 'm02h1:' || pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            {schema}.m02_canon_v1(pg_catalog.jsonb_build_array(
                'm02.handoff/v1', 'M11', v_tenant, p_source_event_id::text, v_kind, v_signal)),
            'UTF8')), 'hex');
        v_payload := pg_catalog.jsonb_build_object(
            'schema_version', 1,
            'tenant_id', v_tenant,
            'operation_id', NULL,
            'source_event_id', p_source_event_id::text,
            'handoff_kind', v_kind,
            'signal_type', v_signal);
        INSERT INTO {schema}.event_handoff_outbox (
            handoff_id, tenant_id, operation_id, source_event_id, handoff_kind,
            dedupe_key, payload, state, attempts, available_at)
        VALUES (
            pg_catalog.gen_random_uuid(), v_tenant, NULL, p_source_event_id, v_kind,
            v_key, v_payload, 'pending', 0, pg_catalog.statement_timestamp())
        ON CONFLICT (dedupe_key) DO NOTHING;
        GET DIAGNOSTICS v_inserted = ROW_COUNT;
        IF v_inserted = 0 THEN
            SELECT o.tenant_id, o.operation_id, o.source_event_id, o.handoff_kind, o.payload
              INTO v_found
              FROM {schema}.event_handoff_outbox AS o
             WHERE o.dedupe_key = v_key;
            IF NOT FOUND
               OR v_found.tenant_id IS DISTINCT FROM v_tenant
               OR v_found.operation_id IS NOT NULL
               OR v_found.source_event_id IS DISTINCT FROM p_source_event_id
               OR v_found.handoff_kind IS DISTINCT FROM v_kind
               OR v_found.payload IS DISTINCT FROM v_payload THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'HANDOFF_KEY_COLLISION';
            END IF;
        END IF;
        v_count := v_count + 1;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'handoff_state', 'enqueued',
                                         'items', v_count);
END;
"""
_T010_BODY_SHA256: Final = "4c15e02bef2dc8589ea5eb8c1c984bf512af846e5b21e864b19e654f81efe58d"
T010_DEFINITION: Final = _definition(
    name=_T010_NAME,
    params=_T010_PARAMS,
    returns="jsonb",
    volatility="volatile",
    parallel="unsafe",
    body=_T010_BODY,
    body_sha256=_T010_BODY_SHA256,
    owner=PROJECTION_OWNER_ROLE,
    grantee=APPEND_OWNER_ROLE,
)
T010_PROFILE: Final = _profile(
    name=_T010_NAME,
    params=_T010_PARAMS,
    returns="jsonb",
    volatility="volatile",
    parallel="unsafe",
    owner=PROJECTION_OWNER_ROLE,
    grantee=APPEND_OWNER_ROLE,
)


# ---------------------------------------------------------------------------
# t011_m02_envelope_read: m02_get_observation_envelope
# ---------------------------------------------------------------------------

T011_MIGRATION_ID: Final = "t011_m02_envelope_read"
_T011_NAME: Final = "m02_get_observation_envelope"
_T011_PARAMS: Final[_Params] = (("p_event_id", "uuid"),)
_T011_BODY: Final = r"""
DECLARE
    v_setting text;
    v_event record;
    v_active boolean;
    v_paths jsonb;
    v_links jsonb;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    IF p_event_id IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT e.event_id, e.event_type, e.contract_version, e.event_level, e.status,
           e.operation_id, e.patient_id, e.actor_id, e.workload_id,
           e.causation_event_id, e.producer_dedupe_key, e.metadata
      INTO v_event
      FROM {schema}.patient_events AS e
     WHERE e.event_id = p_event_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    -- A type inactive in the loaded generation, or a type/version with no allowlist
    -- row for this consumer, fails closed; whole metadata is never returned.
    SELECT t.is_active INTO v_active
      FROM {schema}.event_types AS t
     WHERE t.event_type = v_event.event_type;
    IF v_active IS DISTINCT FROM true OR NOT EXISTS (
        SELECT 1 FROM {schema}.m02_envelope_path_allowlist AS a
         WHERE a.consumer_module = 'm05'
           AND a.event_type = v_event.event_type
           AND a.contract_version = v_event.contract_version
    ) THEN
        RETURN pg_catalog.jsonb_build_object('error', 'ENVELOPE_UNSUPPORTED');
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_object_agg(a.json_path,
                        v_event.metadata OPERATOR(pg_catalog.->) a.json_path::text),
                    '{}'::jsonb)
      INTO v_paths
      FROM {schema}.m02_envelope_path_allowlist AS a
     WHERE a.consumer_module = 'm05'
       AND a.event_type = v_event.event_type
       AND a.contract_version = v_event.contract_version
       AND v_event.metadata OPERATOR(pg_catalog.?) a.json_path::text;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'link_type', l.link_type,
                        'link_id', l.link_id::text,
                        'relationship', l.relationship)
                    ORDER BY l.link_type, l.link_id, l.relationship), '[]'::jsonb)
      INTO v_links
      FROM {schema}.patient_event_links AS l
     WHERE l.event_id = v_event.event_id;
    RETURN pg_catalog.jsonb_build_object(
        'schema_version', 1,
        'event_id', v_event.event_id::text,
        'event_type', v_event.event_type,
        'contract_version', v_event.contract_version,
        'event_level', v_event.event_level,
        'status', v_event.status,
        'operation_id', v_event.operation_id::text,
        'patient_id', v_event.patient_id::text,
        'actor_id', v_event.actor_id::text,
        'workload_id', v_event.workload_id::text,
        'causation_event_id', v_event.causation_event_id::text,
        'producer_dedupe_key', v_event.producer_dedupe_key,
        'links', v_links,
        'metadata_paths', v_paths);
END;
"""
_T011_BODY_SHA256: Final = "1364e2a7222afe9664a14db72408b9386d87d075fd7f08dadb18912b137324ea"
T011_DEFINITION: Final = _definition(
    name=_T011_NAME,
    params=_T011_PARAMS,
    returns="jsonb",
    volatility="stable",
    parallel="unsafe",
    body=_T011_BODY,
    body_sha256=_T011_BODY_SHA256,
    owner=READ_OWNER_ROLE,
    grantee=M05_OWNER_ROLE,
)
T011_PROFILE: Final = _profile(
    name=_T011_NAME,
    params=_T011_PARAMS,
    returns="jsonb",
    volatility="stable",
    parallel="unsafe",
    owner=READ_OWNER_ROLE,
    grantee=M05_OWNER_ROLE,
)


PART_A_GATEWAY_UNITS: Final = MappingProxyType(
    {
        T008_MIGRATION_ID: T008_DEFINITION,
        T009_MIGRATION_ID: T009_DEFINITION,
        T010_MIGRATION_ID: T010_DEFINITION,
        T011_MIGRATION_ID: T011_DEFINITION,
    }
)
PART_A_GATEWAY_PROFILES: Final = MappingProxyType(
    {
        T008_MIGRATION_ID: T008_PROFILE,
        T009_MIGRATION_ID: T009_PROFILE,
        T010_MIGRATION_ID: T010_PROFILE,
        T011_MIGRATION_ID: T011_PROFILE,
    }
)
