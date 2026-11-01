"""MA-10 staff directory units t021..t024 (UC1 part B).

- ``t021_m01_staff_directory`` migrator-owned tables (package v1 section 3.2,
  v1r1 delta 1-3, v1r2 corrections 3-4): the staff membership row, follow-up
  access, admin action keys and the immutable admin log. The runtime role's
  default table privileges (``t001``) are revoked, and PUBLIC holds nothing. The
  staff owner holds exactly the grants its three gateways need; no other role has
  any directory table privilege.
- ``t022``..``t024`` typed gateways owned by ``haloflow_staff_owner``: the
  assignability check (M05 owner only), administration and the display read
  (runtime). Implemented after the Codex red gate PASS on 24e463b.

Trust model (v1r1 delta 3): direct table privilege exists only for the staff owner
(exact grants), the migrator (table owner, SET edge to the staff owner) and the
provisioner (schema owner). The rejectors refuse UPDATE/DELETE/TRUNCATE through
DML for every role; they do not resist a privileged owner changing the
installation (L-2 carried).
"""

from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition, UnitDefinitions
from haloflow.m02.roles import M05_OWNER_ROLE

STAFF_OWNER_ROLE: Final = "haloflow_staff_owner"

T021_MIGRATION_ID: Final = "t021_m01_staff_directory"

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_MSG_ROLES: Final = "staff directory role verification failed"

# v1r2 correction 4: the Unicode White_Space set, enumerated. A display name may
# not start or end with any of these. Explicit class, not btrim.
_WS: Final = (
    r"\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
)
# Anywhere in the name: C0/C1 controls and the two line/paragraph separators.
_FORBIDDEN: Final = r"\u0001-\u001f\u007f-\u009f\u2028\u2029"

DISPLAY_NAME_CHECK: Final = (
    "(display_name IS NFC NORMALIZED"
    " AND pg_catalog.char_length(display_name) BETWEEN 1 AND 80"
    f" AND display_name OPERATOR(pg_catalog.!~) '[{_FORBIDDEN}]'"
    f" AND display_name OPERATOR(pg_catalog.!~) '^[{_WS}]'"
    f" AND display_name OPERATOR(pg_catalog.!~) '[{_WS}]$')"
)

_SNAPSHOT_KEYS: Final = (
    "'schema_version', 'staff_id', 'status', 'display_name', 'access', 'status_changed_at'"
)
_RESULT_KEYS: Final = "'schema_version', 'client_action_id', 'operation', 'staff'"
_OPERATIONS: Final = "'add', 'rename', 'deactivate', 'reactivate', 'grant', 'revoke'"


def _closed(column: str, keys: str) -> str:
    return (
        f"(pg_catalog.jsonb_typeof({column}) = 'object'"
        f" AND {column} OPERATOR(pg_catalog.?&) ARRAY[{keys}]"
        f" AND ({column} OPERATOR(pg_catalog.-) ARRAY[{keys}]) = '{{}}'::jsonb)"
    )


T021_SQL: Final = f"""
-- t021:step:guard
DO $guard$
BEGIN
    IF (SELECT count(*)
          FROM pg_catalog.pg_roles AS r
         WHERE r.rolname = '{STAFF_OWNER_ROLE}'
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

-- t021:step:tables
CREATE TABLE {_S}.m01_staff (
    staff_id uuid PRIMARY KEY,
    status varchar(8) NOT NULL CHECK (status IN ('active', 'inactive')),
    status_changed_at timestamptz NOT NULL,
    display_name varchar(80) NOT NULL CHECK {DISPLAY_NAME_CHECK}
);
CREATE TABLE {_S}.m01_staff_followup_access (
    staff_id uuid NOT NULL REFERENCES {_S}.m01_staff (staff_id),
    access varchar(4) NOT NULL CHECK (access IN ('read', 'act')),
    granted_at timestamptz NOT NULL,
    PRIMARY KEY (staff_id, access)
);
CREATE TABLE {_S}.m01_staff_action_keys (
    client_action_id uuid PRIMARY KEY,
    digest bytea NOT NULL CHECK (pg_catalog.octet_length(digest) = 32),
    actor_id varchar(128) NOT NULL,
    staff_id uuid NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE {_S}.m01_staff_admin_log (
    log_id uuid PRIMARY KEY,
    client_action_id uuid NOT NULL UNIQUE
        REFERENCES {_S}.m01_staff_action_keys (client_action_id),
    staff_id uuid NOT NULL,
    operation varchar(16) NOT NULL CHECK (operation IN ({_OPERATIONS})),
    before jsonb,
    after jsonb NOT NULL,
    result jsonb NOT NULL,
    actor_id varchar(128) NOT NULL,
    execution_id uuid NOT NULL,
    request_id varchar(128),
    purpose_code varchar(64) NOT NULL,
    digest bytea NOT NULL CHECK (pg_catalog.octet_length(digest) = 32),
    created_at timestamptz NOT NULL,
    CONSTRAINT m01_staff_admin_log_before_iff_not_add
        CHECK ((before IS NULL) = (operation = 'add')),
    CONSTRAINT m01_staff_admin_log_before_closed
        CHECK (before IS NULL OR {_closed("before", _SNAPSHOT_KEYS)}),
    CONSTRAINT m01_staff_admin_log_after_closed CHECK {_closed("after", _SNAPSHOT_KEYS)},
    CONSTRAINT m01_staff_admin_log_result_closed CHECK {_closed("result", _RESULT_KEYS)}
);

-- t021:step:rejectors
CREATE FUNCTION {_S}.staff_record_reject() RETURNS trigger
    LANGUAGE plpgsql AS $reject$
BEGIN
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STAFF_RECORD_IMMUTABLE';
END
$reject$;
REVOKE ALL ON FUNCTION {_S}.staff_record_reject() FROM PUBLIC;
CREATE TRIGGER m01_staff_no_delete
    BEFORE DELETE ON {_S}.m01_staff
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_action_keys_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m01_staff_action_keys
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_action_keys_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_action_keys
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_admin_log_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m01_staff_admin_log
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_admin_log_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_admin_log
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();

-- t021:step:runtime_privileges
REVOKE ALL ON {_S}.m01_staff, {_S}.m01_staff_followup_access,
    {_S}.m01_staff_action_keys, {_S}.m01_staff_admin_log
    FROM {RUNTIME_ROLE}, PUBLIC;

-- t021:step:grants
GRANT SELECT, INSERT ON {_S}.m01_staff TO {STAFF_OWNER_ROLE};
GRANT UPDATE (status, status_changed_at, display_name) ON {_S}.m01_staff
    TO {STAFF_OWNER_ROLE};
GRANT SELECT, INSERT, DELETE ON {_S}.m01_staff_followup_access TO {STAFF_OWNER_ROLE};
GRANT SELECT, INSERT ON {_S}.m01_staff_action_keys TO {STAFF_OWNER_ROLE};
GRANT INSERT ON {_S}.m01_staff_admin_log TO {STAFF_OWNER_ROLE};
GRANT SELECT (client_action_id, staff_id, operation, actor_id, digest, after, result)
    ON {_S}.m01_staff_admin_log TO {STAFF_OWNER_ROLE};
"""

# ---------------------------------------------------------------------------
# Typed gateways t022..t024 (implementation tranche after the Codex red gate PASS
# on 24e463b, 20261101T140311Z). Each body's SHA-256 is a reviewed literal.
# ---------------------------------------------------------------------------

# t022 m01_staff_assignable (v2 5.2-5.4, v1 3.3): tenant context; READ COMMITTED
# guard; statement 1 takes the membership row FOR SHARE; statement 2, separate, counts
# read+act. False for unknown, inactive or missing access; never raises for those.
_ASSIGNABLE_BODY: Final = r"""
DECLARE
    v_setting text;
    v_status text;
    v_access integer;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;

    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ISOLATION_UNSUPPORTED';
    END IF;

    IF p_staff_id IS NULL THEN
        RETURN false;
    END IF;
    -- Statement 1: the membership row, FOR SHARE (conflicts with every writer's
    -- FOR UPDATE; FOR KEY SHARE would not). Under READ COMMITTED a waiting lock
    -- re-reads the newly committed row.
    SELECT s.status INTO v_status
      FROM {schema}.m01_staff AS s
     WHERE s.staff_id = p_staff_id
       FOR SHARE;
    IF NOT FOUND OR v_status <> 'active' THEN
        RETURN false;
    END IF;
    -- Statement 2, separate: a fresh snapshot taken after the lock was granted.
    SELECT pg_catalog.count(*) INTO v_access
      FROM {schema}.m01_staff_followup_access AS a
     WHERE a.staff_id = p_staff_id
       AND a.access IN ('read', 'act');
    RETURN v_access = 2;
END;
"""
_ASSIGNABLE_BODY_SHA256: Final = "24ae3aa3aea919ae31e97c670ba1a449ffe10cbff3335a4155ec287ebcad6d29"


# t023 m01_staff_admin (v1 3.4, v1r1 deltas 1-2, v1r2 c1/c3/c4/c5): tenant context;
# READ COMMITTED guard; composition scope (key + digest) before shape; closed shape;
# authorization before replay; key admission (INSERT ... ON CONFLICT DO NOTHING waits
# on an uncommitted holder); replay / actor mismatch / conflict / log integrity;
# membership FOR UPDATE; precondition; change; immutable log row. Touches no M05
# relation (lock order, DT-S1).
_ADMIN_BODY: Final = r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    c_ws constant text := '[\u0009-\u000d\u0020\u0085\u00a0\u1680'
        || '\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]';
    c_forbidden constant text := '[\u0001-\u001f\u007f-\u009f\u2028\u2029]';
    v_setting text;
    v_attr jsonb;
    v_perm jsonb;
    v_key uuid;
    v_staff uuid;
    v_op text;
    v_name text;
    v_access text;
    v_actor text;
    v_digest bytea;
    v_scope text;
    v_prior text;
    v_keys text[];
    v_expected text[];
    v_admitted uuid;
    v_stored record;
    v_log record;
    v_snap jsonb;
    v_snap_name text;
    v_name_ok boolean;
    v_ts timestamptz;
    v_ts_ok boolean;
    v_result jsonb;
    v_row record;
    v_before jsonb;
    v_after jsonb;
    v_now timestamptz := pg_catalog.now();
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;

    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ISOLATION_UNSUPPORTED';
    END IF;

    -- Composition (v1r2 c5): one canonical semantic request per transaction.
    -- Diagnostic guard; the service route is the primary control.
    v_digest := pg_catalog.sha256(pg_catalog.convert_to(
        '["m01.staff_admin/v1",' || pg_catalog.to_json(v_setting)::text
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>)
            'client_action_id')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.#>>)
            '{attribution,principal_id}')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>)
            'operation')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>)
            'staff_id')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>)
            'display_name')::text, 'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>) 'access')::text,
            'null')
        || ']', 'UTF8'));
    v_scope := 'admin:' || pg_catalog.encode(v_digest, 'hex');
    v_prior := pg_catalog.current_setting('haloflow.txn_scope', true);
    IF v_prior IS NOT NULL AND v_prior <> '' AND v_prior <> v_scope THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMPOSITION_UNSUPPORTED';
    END IF;

    -- Closed shape.
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_op := CASE WHEN pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'operation') =
        'string'
                 THEN p_command OPERATOR(pg_catalog.->>) 'operation' END;
    v_expected := CASE
        WHEN v_op IN ('add', 'rename') THEN ARRAY['attribution', 'client_action_id',
            'display_name',
            'operation', 'permissions', 'schema_version', 'staff_id']
        WHEN v_op IN ('grant', 'revoke') THEN ARRAY['access', 'attribution', 'client_action_id',
            'operation', 'permissions', 'schema_version', 'staff_id']
        WHEN v_op IN ('deactivate', 'reactivate') THEN ARRAY['attribution', 'client_action_id',
            'operation', 'permissions', 'schema_version', 'staff_id']
    END;
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(p_command) AS k;
    IF v_expected IS NULL OR v_keys IS DISTINCT FROM v_expected
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version') IS DISTINCT
           FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'client_action_id') IS
           DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'client_action_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'staff_id') IS DISTINCT FROM
           'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'staff_id') OPERATOR(pg_catalog.!~) c_uuid THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF v_op IN ('add', 'rename') THEN
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'display_name') IS DISTINCT
            FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_name := p_command OPERATOR(pg_catalog.->>) 'display_name';
        -- v1r1 delta 4 + v1r2 c4: NFC (never normalized), 1-80 code points, no
        -- control or line-separator code point, no Unicode White_Space at either end.
        IF NOT (v_name IS NFC NORMALIZED)
           OR pg_catalog.char_length(v_name) NOT BETWEEN 1 AND 80
           OR v_name OPERATOR(pg_catalog.~) c_forbidden
           OR v_name OPERATOR(pg_catalog.~) ('^' || c_ws)
           OR v_name OPERATOR(pg_catalog.~) (c_ws || '$') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END IF;
    IF v_op IN ('grant', 'revoke') THEN
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'access') IS DISTINCT FROM
            'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'access') NOT IN ('read', 'act') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_access := p_command OPERATOR(pg_catalog.->>) 'access';
    END IF;
    v_perm := p_command OPERATOR(pg_catalog.->) 'permissions';
    IF pg_catalog.jsonb_typeof(v_perm) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_perm) AS
           k)
          IS DISTINCT FROM ARRAY['staff_admin']::text[]
       OR pg_catalog.jsonb_typeof(v_perm OPERATOR(pg_catalog.->) 'staff_admin') IS DISTINCT FROM
           'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_attr := p_command OPERATOR(pg_catalog.->) 'attribution';
    IF pg_catalog.jsonb_typeof(v_attr) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_attr) AS
           k)
          IS DISTINCT FROM ARRAY['auth_method', 'execution_id', 'principal_id', 'principal_kind',
                                 'purpose_code', 'request_id']::text[]
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'principal_kind') IS DISTINCT
           FROM 'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') NOT IN ('actor', 'workload')
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'principal_id') IS DISTINCT FROM
           'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'execution_id') IS DISTINCT FROM
           'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'execution_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'auth_method') IS DISTINCT FROM
           'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'auth_method') OPERATOR(pg_catalog.!~)
           '^[a-z][a-z0-9_]{0,31}$'
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'purpose_code') IS DISTINCT FROM
           'string'
       OR (v_attr OPERATOR(pg_catalog.->>) 'purpose_code') NOT IN ('treatment', 'payment',
           'operations')
       OR pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'request_id') NOT IN ('null',
           'string')
       OR (pg_catalog.jsonb_typeof(v_attr OPERATOR(pg_catalog.->) 'request_id') = 'string'
           AND pg_catalog.char_length(v_attr OPERATOR(pg_catalog.->>) 'request_id') NOT BETWEEN 1
               AND 128) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_key := (p_command OPERATOR(pg_catalog.->>) 'client_action_id')::uuid;
    v_staff := (p_command OPERATOR(pg_catalog.->>) 'staff_id')::uuid;
    v_actor := v_attr OPERATOR(pg_catalog.->>) 'principal_id';

    -- Authorization precedes replay (v1r2 c1). Defence in depth only (B3b).
    IF (v_perm OPERATOR(pg_catalog.->) 'staff_admin') <> 'true'::jsonb
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'actor' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NOT_AUTHORIZED';
    END IF;
    PERFORM pg_catalog.set_config('haloflow.txn_scope', v_scope, true);

    -- Admission (v1r1 delta 1): waits on an uncommitted holder of the same key.
    INSERT INTO {schema}.m01_staff_action_keys (client_action_id, digest, actor_id, staff_id,
                                                created_at)
    VALUES (v_key, v_digest, v_actor, v_staff, v_now)
    ON CONFLICT (client_action_id) DO NOTHING
    RETURNING client_action_id INTO v_admitted;
    IF v_admitted IS NULL THEN
        SELECT k.actor_id, k.digest, k.staff_id INTO v_stored
          FROM {schema}.m01_staff_action_keys AS k
         WHERE k.client_action_id = v_key;
        IF v_stored.actor_id IS DISTINCT FROM v_actor THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ACTION_KEY_ACTOR_MISMATCH';
        END IF;
        IF v_stored.digest IS DISTINCT FROM v_digest THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ACTION_KEY_CONFLICT';
        END IF;
        -- Replay integrity (v1r2 c3; Codex F-MA10-IMPL-01): the stored result is
        -- returned only if the full closed result and its nested staff snapshot are
        -- well-typed and bound to this request, the key row and the log row. The
        -- historical snapshot is never compared with current (mutable) staff state.
        SELECT l.operation, l.staff_id, l.actor_id, l.digest, l.after, l.result
          INTO v_log
          FROM {schema}.m01_staff_admin_log AS l
         WHERE l.client_action_id = v_key;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ADMIN_LOG_INTEGRITY';
        END IF;
        v_result := v_log.result;
        v_snap := v_log.after;
        -- Canonical UTC timestamp: shape, a real calendar date (narrowly caught
        -- parse) and an exact round trip (Codex 063f4ac review).
        v_ts_ok := false;
        IF pg_catalog.jsonb_typeof(v_snap OPERATOR(pg_catalog.->) 'status_changed_at') = 'string'
           AND (v_snap OPERATOR(pg_catalog.->>) 'status_changed_at') OPERATOR(pg_catalog.~)
               '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{6}Z$' THEN
            BEGIN
                v_ts := pg_catalog.timezone('UTC', pg_catalog.replace(pg_catalog.replace(
                    v_snap OPERATOR(pg_catalog.->>) 'status_changed_at', 'T', ' '), 'Z', '')
                    ::timestamp);
                v_ts_ok := pg_catalog.to_char(v_ts AT TIME ZONE 'UTC',
                                              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                           = (v_snap OPERATOR(pg_catalog.->>) 'status_changed_at');
            EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN
                v_ts_ok := false;
            END;
        END IF;
        -- The full display-name rule applies to every stored snapshot.
        v_snap_name := CASE
            WHEN pg_catalog.jsonb_typeof(v_snap OPERATOR(pg_catalog.->) 'display_name') = 'string'
            THEN v_snap OPERATOR(pg_catalog.->>) 'display_name' END;
        v_name_ok := COALESCE(v_snap_name IS NFC NORMALIZED
            AND pg_catalog.char_length(v_snap_name) BETWEEN 1 AND 80
            AND v_snap_name OPERATOR(pg_catalog.!~) c_forbidden
            AND v_snap_name OPERATOR(pg_catalog.!~) ('^' || c_ws)
            AND v_snap_name OPERATOR(pg_catalog.!~) (c_ws || '$'), false);
        -- Any unknown (NULL) predicate counts as corrupt.
        IF COALESCE(v_stored.staff_id IS DISTINCT FROM v_staff
           OR v_log.operation IS DISTINCT FROM v_op
           OR v_log.staff_id IS DISTINCT FROM v_staff
           OR v_log.actor_id IS DISTINCT FROM v_actor
           OR v_log.digest IS DISTINCT FROM v_digest
           OR pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
           OR (SELECT pg_catalog.array_agg(k ORDER BY k)
                 FROM pg_catalog.jsonb_object_keys(v_result) AS k)
              IS DISTINCT FROM ARRAY['client_action_id', 'operation', 'schema_version',
                                     'staff']::text[]
           OR (v_result OPERATOR(pg_catalog.->) 'schema_version') IS DISTINCT FROM '1'::jsonb
           OR (v_result OPERATOR(pg_catalog.->) 'client_action_id')
              IS DISTINCT FROM pg_catalog.to_jsonb(v_key::text)
           OR (v_result OPERATOR(pg_catalog.->) 'operation')
              IS DISTINCT FROM pg_catalog.to_jsonb(v_op)
           OR (v_result OPERATOR(pg_catalog.->) 'staff') IS DISTINCT FROM v_snap
           OR pg_catalog.jsonb_typeof(v_snap) IS DISTINCT FROM 'object'
           OR (SELECT pg_catalog.array_agg(k ORDER BY k)
                 FROM pg_catalog.jsonb_object_keys(v_snap) AS k)
              IS DISTINCT FROM ARRAY['access', 'display_name', 'schema_version', 'staff_id',
                                     'status', 'status_changed_at']::text[]
           OR (v_snap OPERATOR(pg_catalog.->) 'schema_version') IS DISTINCT FROM '1'::jsonb
           OR (v_snap OPERATOR(pg_catalog.->) 'staff_id')
              IS DISTINCT FROM pg_catalog.to_jsonb(v_staff::text)
           OR NOT COALESCE((v_snap OPERATOR(pg_catalog.->) 'status')
                           IN ('"active"'::jsonb, '"inactive"'::jsonb), false)
           OR NOT v_name_ok
           OR NOT COALESCE((v_snap OPERATOR(pg_catalog.->) 'access')
                           IN ('[]'::jsonb, '["act"]'::jsonb, '["read"]'::jsonb,
                               '["act", "read"]'::jsonb), false)
           OR NOT v_ts_ok
           -- operation-implied facts of the after snapshot
           OR (v_op IN ('add', 'reactivate')
               AND (v_snap OPERATOR(pg_catalog.->>) 'status') IS DISTINCT FROM 'active')
           OR (v_op = 'deactivate'
               AND (v_snap OPERATOR(pg_catalog.->>) 'status') IS DISTINCT FROM 'inactive')
           OR (v_op IN ('add', 'rename')
               AND (v_snap OPERATOR(pg_catalog.->>) 'display_name') IS DISTINCT FROM v_name)
           OR (v_op = 'add'
               AND (v_snap OPERATOR(pg_catalog.->) 'access') IS DISTINCT FROM '[]'::jsonb)
           OR (v_op = 'grant'
               AND NOT COALESCE((v_snap OPERATOR(pg_catalog.->) 'access')
                                OPERATOR(pg_catalog.?) v_access, false))
           OR (v_op = 'revoke'
               AND COALESCE((v_snap OPERATOR(pg_catalog.->) 'access')
                            OPERATOR(pg_catalog.?) v_access, true)), true) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ADMIN_LOG_INTEGRITY';
        END IF;
        RETURN v_result;
    END IF;

    -- Admitted: membership row FOR UPDATE first (the serialization point).
    SELECT s.staff_id, s.status, s.status_changed_at, s.display_name INTO v_row
      FROM {schema}.m01_staff AS s
     WHERE s.staff_id = v_staff
       FOR UPDATE;
    IF v_op = 'add' THEN
        IF FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
        END IF;
        BEGIN
            INSERT INTO {schema}.m01_staff (staff_id, status, status_changed_at, display_name)
            VALUES (v_staff, 'active', v_now, v_name);
        EXCEPTION WHEN unique_violation THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
        END;
        v_before := NULL;
    ELSE
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
        END IF;
        SELECT pg_catalog.jsonb_build_object(
        'schema_version', 1, 'staff_id', s.staff_id::text, 'status', s.status,
        'display_name', s.display_name,
        'access', COALESCE((SELECT pg_catalog.jsonb_agg(a.access ORDER BY a.access)
                              FROM {schema}.m01_staff_followup_access AS a
                             WHERE a.staff_id = s.staff_id), '[]'::jsonb),
        'status_changed_at', pg_catalog.to_char(s.status_changed_at AT TIME ZONE 'UTC',
                                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
          INTO v_before
          FROM {schema}.m01_staff AS s
         WHERE s.staff_id = v_staff;
        IF v_op = 'rename' THEN
            UPDATE {schema}.m01_staff SET display_name = v_name WHERE staff_id = v_staff;
        ELSIF v_op = 'deactivate' THEN
            IF v_row.status <> 'active' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END IF;
            UPDATE {schema}.m01_staff SET status = 'inactive', status_changed_at = v_now
             WHERE staff_id = v_staff;
        ELSIF v_op = 'reactivate' THEN
            IF v_row.status <> 'inactive' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END IF;
            UPDATE {schema}.m01_staff SET status = 'active', status_changed_at = v_now
             WHERE staff_id = v_staff;
        ELSIF v_op = 'grant' THEN
            BEGIN
                INSERT INTO {schema}.m01_staff_followup_access (staff_id, access, granted_at)
                VALUES (v_staff, v_access, v_now);
            EXCEPTION WHEN unique_violation THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END;
        ELSE  -- revoke
            DELETE FROM {schema}.m01_staff_followup_access
             WHERE staff_id = v_staff AND access = v_access;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END IF;
        END IF;
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'schema_version', 1, 'staff_id', s.staff_id::text, 'status', s.status,
        'display_name', s.display_name,
        'access', COALESCE((SELECT pg_catalog.jsonb_agg(a.access ORDER BY a.access)
                              FROM {schema}.m01_staff_followup_access AS a
                             WHERE a.staff_id = s.staff_id), '[]'::jsonb),
        'status_changed_at', pg_catalog.to_char(s.status_changed_at AT TIME ZONE 'UTC',
                                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
          INTO v_after
          FROM {schema}.m01_staff AS s
         WHERE s.staff_id = v_staff;
    v_result := pg_catalog.jsonb_build_object(
        'schema_version', 1, 'client_action_id', v_key::text, 'operation', v_op, 'staff', v_after);
    INSERT INTO {schema}.m01_staff_admin_log (
        log_id, client_action_id, staff_id, operation, before, after, result, actor_id,
        execution_id, request_id, purpose_code, digest, created_at)
    VALUES (pg_catalog.gen_random_uuid(), v_key, v_staff, v_op, v_before, v_after, v_result,
            v_actor, (v_attr OPERATOR(pg_catalog.->>) 'execution_id')::uuid,
            v_attr OPERATOR(pg_catalog.->>) 'request_id', v_attr OPERATOR(pg_catalog.->>)
                'purpose_code',
            v_digest, v_now);
    RETURN v_result;
END;
"""
_ADMIN_BODY_SHA256: Final = "c4352882071aca5b8ada639a2f0d0facd76996ff40131a5f0ef906e837ed1356"


# t024 m01_staff_display (v1 3.3 DR-07): STABLE, no lock, no isolation guard; 1-100
# distinct canonical ids; unknown and other-clinic ids omitted alike; never access.
_DISPLAY_BODY: Final = r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_ids jsonb;
    v_bad integer;
    v_count integer;
    v_distinct integer;
    v_staff jsonb;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;

    IF pg_catalog.jsonb_typeof(p_query) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(p_query) AS
           k)
          IS DISTINCT FROM ARRAY['schema_version', 'staff_ids']::text[]
       OR pg_catalog.jsonb_typeof(p_query OPERATOR(pg_catalog.->) 'schema_version') IS DISTINCT
           FROM 'number'
       OR (p_query OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_query OPERATOR(pg_catalog.->) 'staff_ids') IS DISTINCT FROM
           'array' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_ids := p_query OPERATOR(pg_catalog.->) 'staff_ids';
    SELECT pg_catalog.count(*),
           pg_catalog.count(*) FILTER (WHERE pg_catalog.jsonb_typeof(e) IS DISTINCT FROM 'string'
                                         OR (e OPERATOR(pg_catalog.#>>) '{}')
                                             OPERATOR(pg_catalog.!~) c_uuid),
           pg_catalog.count(DISTINCT e)
      INTO v_count, v_bad, v_distinct
      FROM pg_catalog.jsonb_array_elements(v_ids) AS e;
    IF v_count NOT BETWEEN 1 AND 100 OR v_bad <> 0 OR v_distinct <> v_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'staff_id', s.staff_id::text, 'display_name', s.display_name, 'status', s.status)
               ORDER BY s.staff_id), '[]'::jsonb)
      INTO v_staff
      FROM {schema}.m01_staff AS s
     WHERE s.staff_id IN (SELECT (e OPERATOR(pg_catalog.#>>) '{}')::uuid
                            FROM pg_catalog.jsonb_array_elements(v_ids) AS e);
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'staff', v_staff);
END;
"""
_DISPLAY_BODY_SHA256: Final = "d21407820b3cfe429685298f7a392bbd80f6fb9b35801df6e3dd2720c3491a83"

# t025 m01_staff_followup_reader (B3b package v2 1.2 + v2r1 A2/A3): STABLE, no lock;
# true only for an active member holding `read`. RED TRANCHE: fail-closed stub (tenant
# context, then refuse) until the implementation gate.
_READER_BODY: Final = r"""
DECLARE
    v_setting text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'M01_STAFF_NOT_IMPLEMENTED';
END;
"""
_READER_BODY_SHA256: Final = "48323b0f0e15a37fff1d9cacf0f43ab3b42406199fdb20a26898d4a59900fcde"

_VOLATILITY_WORD: Final = {"stable": "STABLE", "volatile": "VOLATILE"}
_VOLATILITY_CODE: Final = {"stable": "s", "volatile": "v"}
_RETURN_WORD: Final = {"bool": "bool", "jsonb": "jsonb"}
_Params = tuple[tuple[str, str], ...]


def _template(
    name: str, params: _Params, returns: str, volatility: str, grantee: str, body: str
) -> str:
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    return (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS {_RETURN_WORD[returns]}\n"
        f"    LANGUAGE plpgsql {_VOLATILITY_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
        f"GRANT EXECUTE ON FUNCTION {_S}.{name}({argtypes}) TO {grantee};\n"
    )


def _definition(
    name: str, params: _Params, returns: str, volatility: str, grantee: str,
    body: str, body_sha256: str,
) -> UnitDefinition:
    def acl() -> list[dict[str, Any]]:
        return [{"grantee": grantee, "privileges": ["EXECUTE"]}]

    return UnitDefinition(
        template=_template(name, params, returns, volatility, grantee, body),
        execution_role=STAFF_OWNER_ROLE,
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
                    "owner": STAFF_OWNER_ROLE,
                    "security_definer": True,
                    "config": [_CONFIG],
                    "acl": acl(),
                    "body": body,
                }
            ],
        },
    )


def _profile(
    name: str, params: _Params, returns: str, volatility: str, grantee: str
) -> InstalledStateProfile:
    return InstalledStateProfile(
        function_name=name,
        argument_types=tuple(t for _, t in params),
        owner=STAFF_OWNER_ROLE,
        execution_role=STAFF_OWNER_ROLE,
        prokind="f",
        return_type=returns,
        returns_set=False,
        language="plpgsql",
        security_definer=True,
        volatility=_VOLATILITY_CODE[volatility],
        parallel="u",
        strict=False,
        installed_acl=(
            (STAFF_OWNER_ROLE, "EXECUTE", STAFF_OWNER_ROLE, False),
            (grantee, "EXECUTE", STAFF_OWNER_ROLE, False),
        ),
    )


# (migration id, function, parameters, return type, volatility, EXECUTE grantee).
# Assignable is explicitly VOLATILE (v2 review): its lock statement and its access
# read each take a fresh READ COMMITTED snapshot. EXECUTE on it goes to the M05
# owner only; admin and display go to the runtime.
_FUNCTIONS: Final[tuple[tuple[str, str, _Params, str, str, str], ...]] = (
    ("t022_m01_staff_assignable", "m01_staff_assignable", (("p_staff_id", "uuid"),),
     "bool", "volatile", M05_OWNER_ROLE),
    ("t023_m01_staff_admin", "m01_staff_admin", (("p_command", "jsonb"),),
     "jsonb", "volatile", RUNTIME_ROLE),
    ("t024_m01_staff_display", "m01_staff_display", (("p_query", "jsonb"),),
     "jsonb", "stable", RUNTIME_ROLE),
    ("t025_m01_staff_followup_reader", "m01_staff_followup_reader", (("p_staff_id", "uuid"),),
     "bool", "stable", RUNTIME_ROLE),
)
_BODIES: Final = {
    "m01_staff_assignable": (_ASSIGNABLE_BODY, _ASSIGNABLE_BODY_SHA256),
    "m01_staff_admin": (_ADMIN_BODY, _ADMIN_BODY_SHA256),
    "m01_staff_display": (_DISPLAY_BODY, _DISPLAY_BODY_SHA256),
    "m01_staff_followup_reader": (_READER_BODY, _READER_BODY_SHA256),
}

STAFF_GATEWAY_UNITS: Final = MappingProxyType(
    {mid: _definition(name, params, ret, vol, grantee, *_BODIES[name])
     for mid, name, params, ret, vol, grantee in _FUNCTIONS}
)
STAFF_GATEWAY_PROFILES: Final = MappingProxyType(
    {mid: _profile(name, params, ret, vol, grantee)
     for mid, name, params, ret, vol, grantee in _FUNCTIONS}
)

STAFF_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {T021_MIGRATION_ID: T021_SQL, **STAFF_GATEWAY_UNITS}
)
