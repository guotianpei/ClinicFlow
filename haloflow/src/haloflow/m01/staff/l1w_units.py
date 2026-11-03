"""L-1W W3a staff units t026-t029 and t032 (contract v1 + Codex 215519Z corrections,
red gate ACCEPT at 6884cb6, Codex 222540Z).

- ``t026_m01_staff_roles`` migrator-owned: the role rows, the tenant-local role guard
  singleton, the bootstrap action keys and the bootstrap log; the admin log's
  operation check is widened to the v2 operations.
- ``t027`` ``m01_staff_self_standing(jsonb)`` (runtime): the caller's own standing.
- ``t028`` ``m01_staff_member_standing(uuid)`` (M05 owner only, pin 2): one member's
  standing under its membership lock (FOR SHARE).
- ``t029`` ``m01_staff_admin_v2(jsonb)`` (runtime): staff administration authorized by
  the actor's ``clinic_admin`` role row, not by a caller flag (the flag only narrows).
- ``t032`` ``m01_staff_bootstrap_admin(jsonb)`` (haloflow_support only): HaloVox support
  bootstraps a clinic's first clinic admin under an active capable grant.

Global lock order (contract §3): role guard -> membership rows (ascending staff_id)
-> identity anchor -> bindings -> sessions -> follow-up item. The guard is taken by
t032 and by every t029 role- or status-changing branch (grant_role, revoke_role,
deactivate, activate), never by M05.

W3a does NOT close v1 (W3b): m01_staff_admin and the v1 M05 actions stay callable,
so W3a alone is not secure production authorization.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinitions
from haloflow.m01.staff.units import STAFF_OWNER_ROLE, _definition, _profile
from haloflow.m02.roles import M05_OWNER_ROLE

SUPPORT_ROLE: Final = "haloflow_support"
BOOTSTRAP_CAPABILITY: Final = "l1w.roles.bootstrap"

T026_MIGRATION_ID: Final = "t026_m01_staff_roles"

_S: Final = "{schema}"
_V1_OPERATIONS: Final = "'add', 'rename', 'deactivate', 'reactivate', 'grant', 'revoke'"
_V2_OPERATIONS: Final = (
    "'activate', 'grant_role', 'revoke_role', 'bind_identity', 'unbind_identity',"
    " 'set_auth_setting'"
)

T026_SQL: Final = f"""
-- t026:step:tables
CREATE TABLE {_S}.m01_staff_roles (
    staff_id uuid NOT NULL REFERENCES {_S}.m01_staff (staff_id),
    role varchar(16) NOT NULL CHECK (role IN ('supervisor', 'clinic_admin')),
    granted_by varchar(136) NOT NULL CHECK (pg_catalog.char_length(granted_by) >= 1),
    granted_at timestamptz NOT NULL,
    PRIMARY KEY (staff_id, role)
);
CREATE TABLE {_S}.m01_staff_role_guard (
    id boolean PRIMARY KEY CHECK (id)
);
INSERT INTO {_S}.m01_staff_role_guard (id) VALUES (true);
CREATE TABLE {_S}.m01_staff_bootstrap_keys (
    client_action_id uuid PRIMARY KEY,
    digest bytea NOT NULL CHECK (pg_catalog.octet_length(digest) = 32),
    actor_id varchar(128) NOT NULL,
    staff_id uuid NOT NULL,
    outcome varchar(32) NOT NULL
        CHECK (outcome IN ('BOOTSTRAPPED', 'ADMIN_EXISTS', 'TARGET_NOT_ACTIVE')),
    created_at timestamptz NOT NULL
);
CREATE TABLE {_S}.m01_staff_bootstrap_log (
    log_id uuid PRIMARY KEY,
    client_action_id uuid NOT NULL UNIQUE
        REFERENCES {_S}.m01_staff_bootstrap_keys (client_action_id),
    staff_id uuid NOT NULL,
    support_actor_id varchar(128) NOT NULL,
    grant_id uuid NOT NULL,
    ticket_reference varchar(128) NOT NULL,
    at timestamptz NOT NULL
);

-- t026:step:admin_log_operations
ALTER TABLE {_S}.m01_staff_admin_log
    DROP CONSTRAINT m01_staff_admin_log_operation_check;
ALTER TABLE {_S}.m01_staff_admin_log
    ADD CONSTRAINT m01_staff_admin_log_operation_check
    CHECK (operation IN ({_V1_OPERATIONS}, {_V2_OPERATIONS}));

-- t026:step:rejectors
CREATE TRIGGER m01_staff_roles_no_update
    BEFORE UPDATE ON {_S}.m01_staff_roles
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_roles_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_roles
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_role_guard_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m01_staff_role_guard
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_role_guard_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_role_guard
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_bootstrap_keys_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m01_staff_bootstrap_keys
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_bootstrap_keys_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_bootstrap_keys
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_bootstrap_log_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m01_staff_bootstrap_log
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_bootstrap_log_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_bootstrap_log
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();

-- t026:step:runtime_privileges
REVOKE ALL ON {_S}.m01_staff_roles, {_S}.m01_staff_role_guard,
    {_S}.m01_staff_bootstrap_keys, {_S}.m01_staff_bootstrap_log
    FROM {RUNTIME_ROLE}, PUBLIC;

-- t026:step:grants
GRANT SELECT, INSERT, DELETE ON {_S}.m01_staff_roles TO {STAFF_OWNER_ROLE};
GRANT SELECT, UPDATE ON {_S}.m01_staff_role_guard TO {STAFF_OWNER_ROLE};
GRANT SELECT, INSERT ON {_S}.m01_staff_bootstrap_keys TO {STAFF_OWNER_ROLE};
GRANT SELECT, INSERT ON {_S}.m01_staff_bootstrap_log TO {STAFF_OWNER_ROLE};
"""

# Shared fragments of the typed bodies (kept literal inside each body so its SHA-256
# pins exactly what is installed).
_TENANT_GUARD: Final = r"""
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
"""
_ISOLATION_GUARD: Final = r"""
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ISOLATION_UNSUPPORTED';
    END IF;
"""
_ATTRIBUTION_INVALID: Final = r"""pg_catalog.jsonb_typeof(v_attr) IS DISTINCT FROM 'object'
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
               AND 128)"""
_SNAPSHOT_SQL: Final = r"""SELECT pg_catalog.jsonb_build_object(
        'schema_version', 1, 'staff_id', s.staff_id::text, 'status', s.status,
        'display_name', s.display_name,
        'access', COALESCE((SELECT pg_catalog.jsonb_agg(a.access ORDER BY a.access)
                              FROM {schema}.m01_staff_followup_access AS a
                             WHERE a.staff_id = s.staff_id), '[]'::jsonb),
        'status_changed_at', pg_catalog.to_char(s.status_changed_at AT TIME ZONE 'UTC',
                                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))"""


# t027 m01_staff_self_standing (contract §1): the caller's OWN standing, for the entry
# point and UI. Tenant context; closed attribution; the membership row FOR SHARE
# (contract §3: reads take membership FOR SHARE only). Unknown -> status 'unknown'.
_SELF_STANDING_BODY: Final = (
    r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_attr jsonb;
    v_actor uuid;
    v_status text;
    v_access jsonb;
    v_roles jsonb;
BEGIN"""
    + _TENANT_GUARD
    + _ISOLATION_GUARD
    + r"""
    v_attr := p_attribution;
    IF """
    + _ATTRIBUTION_INVALID
    + r""" THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'actor' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NOT_AUTHORIZED';
    END IF;
    v_actor := (v_attr OPERATOR(pg_catalog.->>) 'principal_id')::uuid;
    SELECT s.status INTO v_status
      FROM {schema}.m01_staff AS s
     WHERE s.staff_id = v_actor
       FOR SHARE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'staff_id', v_actor::text,
            'status', 'unknown', 'access', '[]'::jsonb, 'roles', '[]'::jsonb);
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(a.access ORDER BY a.access), '[]'::jsonb)
      INTO v_access
      FROM {schema}.m01_staff_followup_access AS a
     WHERE a.staff_id = v_actor;
    SELECT COALESCE(pg_catalog.jsonb_agg(r.role ORDER BY r.role), '[]'::jsonb)
      INTO v_roles
      FROM {schema}.m01_staff_roles AS r
     WHERE r.staff_id = v_actor;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'staff_id', v_actor::text,
        'status', v_status, 'access', v_access, 'roles', v_roles);
END;
"""
)

# t028 m01_staff_member_standing (contract §1, §4; pin 2): EXECUTE to the M05 owner
# only. Statement 1 takes the membership row FOR SHARE (conflicts with every writer's
# FOR UPDATE); statement 2, separate, reads access and roles on a fresh snapshot.
# Never raises for unknown, inactive or missing standing.
_MEMBER_STANDING_BODY: Final = (
    r"""
DECLARE
    v_setting text;
    v_status text;
    v_access integer;
    v_supervisor boolean;
BEGIN"""
    + _TENANT_GUARD
    + _ISOLATION_GUARD
    + r"""
    IF p_staff_id IS NOT NULL THEN
        SELECT s.status INTO v_status
          FROM {schema}.m01_staff AS s
         WHERE s.staff_id = p_staff_id
           FOR SHARE;
    END IF;
    IF p_staff_id IS NULL OR NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'known', false,
            'active', false, 'assignable', false, 'supervisor', false);
    END IF;
    SELECT pg_catalog.count(*) INTO v_access
      FROM {schema}.m01_staff_followup_access AS a
     WHERE a.staff_id = p_staff_id
       AND a.access IN ('read', 'act');
    SELECT EXISTS (SELECT 1 FROM {schema}.m01_staff_roles AS r
                    WHERE r.staff_id = p_staff_id AND r.role = 'supervisor')
      INTO v_supervisor;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'known', true,
        'active', v_status = 'active',
        'assignable', v_status = 'active' AND v_access = 2,
        'supervisor', v_status = 'active' AND v_access = 2 AND v_supervisor);
END;
"""
)


# t029 m01_staff_admin_v2 (contract §1, §3; mechanism note; Codex 215519Z).
# Order: 1 tenant; 1b READ COMMITTED; 1c composition (v2 digest) before any lock;
# 2 closed shape; 3 the caller flag and principal kind can only NARROW; 4 role guard
# FOR UPDATE for grant_role/revoke_role/deactivate/activate only; 5 membership rows
# in ascending staff_id order (actor FOR SHARE, target FOR UPDATE; actor = target
# once, FOR UPDATE); 6 authorization from the actor's CURRENT standing (active and a
# clinic_admin role row), after the locks and before replay; 7 self rules (no self
# grant/revoke of clinic_admin, no self bind); 8 key admission, replay, conflict;
# 9 precondition and change (identity and settings through the shared l1w_admin_*
# definers, tenant derived from this schema); 10 immutable log row.
_ADMIN_V2_BODY: Final = (
    r"""
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
    v_actor_id uuid;
    v_op text;
    v_name text;
    v_access text;
    v_role text;
    v_issuer text;
    v_subject text;
    v_auth_setting text;
    v_auth_value text;
    v_actor text;
    v_digest bytea;
    v_scope text;
    v_prior text;
    v_keys text[];
    v_expected text[];
    v_admitted uuid;
    v_stored record;
    v_log record;
    v_result jsonb;
    v_snap jsonb;
    v_snap_name text;
    v_name_ok boolean;
    v_ts timestamptz;
    v_ts_ok boolean;
    v_row record;
    v_found boolean := false;
    v_actor_status text;
    v_tenant varchar(64);
    v_outcome text;
    v_before jsonb;
    v_after jsonb;
    v_now timestamptz := pg_catalog.now();
BEGIN"""
    + _TENANT_GUARD
    + _ISOLATION_GUARD
    + r"""
    -- 1c. Composition: one canonical semantic request per transaction.
    v_digest := pg_catalog.sha256(pg_catalog.convert_to(
        '["m01.staff_admin/v2",' || pg_catalog.to_json(v_setting)::text
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
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>) 'role')::text,
            'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>) 'issuer')::text,
            'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>) 'subject')::text,
            'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>) 'setting')::text,
            'null')
        || ',' || COALESCE(pg_catalog.to_json(p_command OPERATOR(pg_catalog.->>) 'value')::text,
            'null')
        || ']', 'UTF8'));
    v_scope := 'admin:' || pg_catalog.encode(v_digest, 'hex');
    v_prior := pg_catalog.current_setting('haloflow.txn_scope', true);
    IF v_prior IS NOT NULL AND v_prior <> '' AND v_prior <> v_scope THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMPOSITION_UNSUPPORTED';
    END IF;

    -- 2. Closed shape.
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_op := CASE WHEN pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'operation') =
        'string'
                 THEN p_command OPERATOR(pg_catalog.->>) 'operation' END;
    v_expected := CASE
        WHEN v_op IN ('add', 'rename') THEN ARRAY['attribution', 'client_action_id',
            'display_name', 'operation', 'permissions', 'schema_version', 'staff_id']
        WHEN v_op IN ('grant', 'revoke') THEN ARRAY['access', 'attribution', 'client_action_id',
            'operation', 'permissions', 'schema_version', 'staff_id']
        WHEN v_op IN ('grant_role', 'revoke_role') THEN ARRAY['attribution', 'client_action_id',
            'operation', 'permissions', 'role', 'schema_version', 'staff_id']
        WHEN v_op = 'bind_identity' THEN ARRAY['attribution', 'client_action_id', 'issuer',
            'operation', 'permissions', 'schema_version', 'staff_id', 'subject']
        WHEN v_op = 'set_auth_setting' THEN ARRAY['attribution', 'client_action_id',
            'operation', 'permissions', 'schema_version', 'setting', 'staff_id', 'value']
        WHEN v_op IN ('deactivate', 'activate', 'unbind_identity') THEN ARRAY['attribution',
            'client_action_id', 'operation', 'permissions', 'schema_version', 'staff_id']
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
    IF v_op IN ('grant_role', 'revoke_role') THEN
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'role') IS DISTINCT FROM
            'string'
           OR (p_command OPERATOR(pg_catalog.->>) 'role') NOT IN ('supervisor', 'clinic_admin')
            THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_role := p_command OPERATOR(pg_catalog.->>) 'role';
    END IF;
    IF v_op = 'bind_identity' THEN
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'issuer') IS DISTINCT FROM
            'string'
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'subject') IS DISTINCT
               FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_issuer := p_command OPERATOR(pg_catalog.->>) 'issuer';
        v_subject := p_command OPERATOR(pg_catalog.->>) 'subject';
    END IF;
    IF v_op = 'set_auth_setting' THEN
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'setting') IS DISTINCT FROM
            'string'
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'value') IS DISTINCT FROM
               'string' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_auth_setting := p_command OPERATOR(pg_catalog.->>) 'setting';
        v_auth_value := p_command OPERATOR(pg_catalog.->>) 'value';
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
    IF """
    + _ATTRIBUTION_INVALID
    + r""" THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_key := (p_command OPERATOR(pg_catalog.->>) 'client_action_id')::uuid;
    v_staff := (p_command OPERATOR(pg_catalog.->>) 'staff_id')::uuid;
    v_actor := v_attr OPERATOR(pg_catalog.->>) 'principal_id';
    v_actor_id := v_actor::uuid;
    -- The clinic setting names the acting admin as staff_id.
    IF v_op = 'set_auth_setting' AND v_staff <> v_actor_id THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;

    -- 3. The caller's flag and principal kind can only narrow.
    IF (v_perm OPERATOR(pg_catalog.->) 'staff_admin') <> 'true'::jsonb
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'actor' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NOT_AUTHORIZED';
    END IF;
    PERFORM pg_catalog.set_config('haloflow.txn_scope', v_scope, true);

    -- 4. The tenant-local role guard, first, on every role- or status-changing branch.
    IF v_op IN ('grant_role', 'revoke_role', 'deactivate', 'activate') THEN
        PERFORM 1 FROM {schema}.m01_staff_role_guard AS g WHERE g.id FOR UPDATE;
    END IF;

    -- 5. Membership rows in ascending staff_id order: actor FOR SHARE, target FOR UPDATE.
    IF v_actor_id = v_staff THEN
        SELECT s.staff_id, s.status INTO v_row
          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_staff FOR UPDATE;
        v_found := FOUND;
        v_actor_status := v_row.status;
    ELSIF v_actor_id < v_staff THEN
        SELECT s.status INTO v_actor_status
          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_actor_id FOR SHARE;
        SELECT s.staff_id, s.status INTO v_row
          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_staff FOR UPDATE;
        v_found := FOUND;
    ELSE
        SELECT s.staff_id, s.status INTO v_row
          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_staff FOR UPDATE;
        v_found := FOUND;
        SELECT s.status INTO v_actor_status
          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_actor_id FOR SHARE;
    END IF;

    -- 6. Authority from the actor's current standing and role row (fresh statement,
    -- after the locks, before replay: a removed admin cannot replay either).
    IF v_actor_status IS DISTINCT FROM 'active'
       OR NOT EXISTS (SELECT 1 FROM {schema}.m01_staff_roles AS r
                       WHERE r.staff_id = v_actor_id AND r.role = 'clinic_admin') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NOT_AUTHORIZED';
    END IF;

    -- 7. Self rules.
    IF v_staff = v_actor_id
       AND (v_op = 'bind_identity'
            OR (v_op IN ('grant_role', 'revoke_role') AND v_role = 'clinic_admin')) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NOT_AUTHORIZED';
    END IF;

    -- 8. Admission; waits on an uncommitted holder of the same key.
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
        -- Replay integrity (L-1W W3b R1, Codex 170024Z; ported from t023, v1r2 c3 and
        -- F-MA10-IMPL-01): the stored result is returned only if the full closed
        -- FOUR-key result and its nested staff snapshot are well-typed and bound to
        -- this request, the key row and the log row; "outcome" is never stored, only
        -- appended on return. The historical snapshot is never compared with current
        -- (mutable) staff state.
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
        -- parse) and an exact round trip.
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
           -- operation-implied facts of the after snapshot (v2 operations: activate is
           -- v1's reactivate; role, identity and setting operations change no
           -- snapshot field)
           OR (v_op IN ('add', 'activate')
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
        RETURN v_result OPERATOR(pg_catalog.||)
            pg_catalog.jsonb_build_object('outcome', 'replayed');
    END IF;

    -- 9. Precondition and change.
    IF v_op = 'add' THEN
        IF v_found THEN
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
        IF NOT v_found THEN
            IF v_op IN ('bind_identity', 'unbind_identity') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STAFF_NOT_FOUND';
            END IF;
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
        END IF;
        """
    + _SNAPSHOT_SQL
    + r"""
          INTO v_before
          FROM {schema}.m01_staff AS s
         WHERE s.staff_id = v_staff;
        IF v_op IN ('bind_identity', 'unbind_identity', 'set_auth_setting') THEN
            -- The tenant comes from THIS schema, never from the caller.
            SELECT t.tenant_id INTO v_tenant
              FROM shared.tenants AS t
             WHERE t.schema_key = '{schema}';
            IF NOT FOUND OR v_tenant IS DISTINCT FROM v_setting THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
            END IF;
        END IF;
        IF v_op = 'rename' THEN
            UPDATE {schema}.m01_staff SET display_name = v_name WHERE staff_id = v_staff;
        ELSIF v_op = 'deactivate' THEN
            IF v_row.status <> 'active' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END IF;
            UPDATE {schema}.m01_staff SET status = 'inactive', status_changed_at = v_now
             WHERE staff_id = v_staff;
        ELSIF v_op = 'activate' THEN
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
        ELSIF v_op = 'revoke' THEN
            DELETE FROM {schema}.m01_staff_followup_access
             WHERE staff_id = v_staff AND access = v_access;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END IF;
        ELSIF v_op = 'grant_role' THEN
            IF v_row.status <> 'active' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END IF;
            BEGIN
                INSERT INTO {schema}.m01_staff_roles (staff_id, role, granted_by, granted_at)
                VALUES (v_staff, v_role, v_actor, v_now);
            EXCEPTION WHEN unique_violation THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END;
        ELSIF v_op = 'revoke_role' THEN
            DELETE FROM {schema}.m01_staff_roles
             WHERE staff_id = v_staff AND role = v_role;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'PRECONDITION_FAILED';
            END IF;
        ELSIF v_op = 'bind_identity' THEN
            v_outcome := shared.l1w_admin_bind(v_tenant, v_staff, v_issuer, v_subject,
                                               v_actor_id);
            IF v_outcome = 'INVALID_INPUT' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
            ELSIF v_outcome IS DISTINCT FROM 'BOUND' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = COALESCE(v_outcome, 'ADMIN_UNAVAILABLE');
            END IF;
        ELSIF v_op = 'unbind_identity' THEN
            v_outcome := shared.l1w_admin_unbind(v_tenant, v_staff, v_actor_id);
            IF v_outcome IS DISTINCT FROM 'UNBOUND_DONE' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = COALESCE(v_outcome, 'ADMIN_UNAVAILABLE');
            END IF;
        ELSE  -- set_auth_setting
            v_outcome := shared.l1w_admin_set_setting(v_tenant, v_auth_setting, v_auth_value,
                                                      v_actor_id);
            IF v_outcome IN ('INVALID_INPUT', 'INVALID_SETTING', 'INVALID_VALUE') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
            ELSIF v_outcome IS DISTINCT FROM 'SET' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = COALESCE(v_outcome, 'ADMIN_UNAVAILABLE');
            END IF;
        END IF;
    END IF;
    """
    + _SNAPSHOT_SQL
    + r"""
          INTO v_after
          FROM {schema}.m01_staff AS s
         WHERE s.staff_id = v_staff;
    v_result := pg_catalog.jsonb_build_object(
        'schema_version', 1, 'client_action_id', v_key::text, 'operation', v_op, 'staff', v_after);
    -- 10. The immutable log row (role changes included).
    INSERT INTO {schema}.m01_staff_admin_log (
        log_id, client_action_id, staff_id, operation, before, after, result, actor_id,
        execution_id, request_id, purpose_code, digest, created_at)
    VALUES (pg_catalog.gen_random_uuid(), v_key, v_staff, v_op, v_before, v_after, v_result,
            v_actor, (v_attr OPERATOR(pg_catalog.->>) 'execution_id')::uuid,
            v_attr OPERATOR(pg_catalog.->>) 'request_id', v_attr OPERATOR(pg_catalog.->>)
                'purpose_code',
            v_digest, v_now);
    RETURN v_result OPERATOR(pg_catalog.||) pg_catalog.jsonb_build_object('outcome', 'applied');
END;
"""
)


# t032 m01_staff_bootstrap_admin (contract §1 + Codex 215519Z): EXECUTE to
# haloflow_support only. Returned codes, never raised (environment faults excepted).
# Order: tenant; READ COMMITTED; closed shape; support login by OID AND name; this
# schema's tenant; an active capable grant; GUARD FOR UPDATE; target membership FOR
# UPDATE; the grant RE-CHECKED in a fresh statement after the locks; the action key
# (replay re-authorized above, then the stored outcome; actor mismatch; conflict);
# target active; no active clinic_admin; role row, log row, key row.
_BOOTSTRAP_BODY: Final = (
    r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_keys text[];
    v_key uuid;
    v_staff uuid;
    v_actor varchar(128);
    v_tenant varchar(64);
    v_grant uuid;
    v_ticket varchar(128);
    v_digest bytea;
    v_stored record;
    v_status text;
    v_outcome text;
    v_now timestamptz := pg_catalog.now();
BEGIN"""
    + _TENANT_GUARD
    + _ISOLATION_GUARD
    + r"""
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'COMMAND_MALFORMED');
    END IF;
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(p_command) AS k;
    IF v_keys IS DISTINCT FROM ARRAY['client_action_id', 'schema_version', 'staff_id']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version') IS DISTINCT
           FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'client_action_id') IS
           DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'client_action_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'staff_id') IS DISTINCT FROM
           'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'staff_id') OPERATOR(pg_catalog.!~) c_uuid THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'COMMAND_MALFORMED');
    END IF;
    v_key := (p_command OPERATOR(pg_catalog.->>) 'client_action_id')::uuid;
    v_staff := (p_command OPERATOR(pg_catalog.->>) 'staff_id')::uuid;

    -- The support operator: session_user by OID AND name (a renamed login fails).
    SELECT l.actor_id INTO v_actor
      FROM shared.support_operator_logins AS l
     WHERE l.login_name = session_user
       AND l.login_oid = (SELECT r.oid FROM pg_catalog.pg_roles AS r
                           WHERE r.rolname = session_user);
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'SUPPORT_NOT_AUTHORIZED');
    END IF;
    -- This schema's tenant, never the caller's.
    SELECT t.tenant_id INTO v_tenant
      FROM shared.tenants AS t
     WHERE t.schema_key = '{schema}';
    IF NOT FOUND OR v_tenant IS DISTINCT FROM v_setting THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    SELECT g.grant_id INTO v_grant
      FROM shared.support_access_grants AS g
     WHERE g.actor_id = v_actor AND g.tenant_id = v_tenant AND g.state = 'active'
       AND g.starts_at <= pg_catalog.clock_timestamp()
       AND pg_catalog.clock_timestamp() < g.expires_at
       AND 'l1w.roles.bootstrap' = ANY (g.capabilities)
     LIMIT 1;
    IF v_grant IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'SUPPORT_NOT_AUTHORIZED');
    END IF;

    -- Locks: the tenant-local guard, then the target's membership row.
    PERFORM 1 FROM {schema}.m01_staff_role_guard AS g WHERE g.id FOR UPDATE;
    SELECT s.status INTO v_status
      FROM {schema}.m01_staff AS s
     WHERE s.staff_id = v_staff
       FOR UPDATE;

    -- The authoritative grant re-check, a fresh statement after the locks.
    v_grant := NULL;
    SELECT g.grant_id, g.ticket_reference INTO v_grant, v_ticket
      FROM shared.support_access_grants AS g
     WHERE g.actor_id = v_actor AND g.tenant_id = v_tenant AND g.state = 'active'
       AND g.starts_at <= pg_catalog.clock_timestamp()
       AND pg_catalog.clock_timestamp() < g.expires_at
       AND 'l1w.roles.bootstrap' = ANY (g.capabilities)
     ORDER BY g.expires_at DESC, g.grant_id
     LIMIT 1;
    IF v_grant IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'SUPPORT_NOT_AUTHORIZED');
    END IF;

    -- The action key (serialized by the guard).
    v_digest := pg_catalog.sha256(pg_catalog.convert_to(
        '["m01.staff_bootstrap/v1",' || pg_catalog.to_json(v_tenant)::text
        || ',' || pg_catalog.to_json(v_key::text)::text
        || ',' || pg_catalog.to_json(v_staff::text)::text || ']', 'UTF8'));
    SELECT k.actor_id, k.digest, k.outcome INTO v_stored
      FROM {schema}.m01_staff_bootstrap_keys AS k
     WHERE k.client_action_id = v_key;
    IF FOUND THEN
        IF v_stored.actor_id IS DISTINCT FROM v_actor THEN
            RETURN pg_catalog.jsonb_build_object('schema_version', 1,
                'outcome', 'ACTION_KEY_ACTOR_MISMATCH');
        END IF;
        IF v_stored.digest IS DISTINCT FROM v_digest THEN
            RETURN pg_catalog.jsonb_build_object('schema_version', 1,
                'outcome', 'ACTION_KEY_CONFLICT');
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', v_stored.outcome, 'replayed', true);
    END IF;

    IF v_status IS DISTINCT FROM 'active' THEN
        v_outcome := 'TARGET_NOT_ACTIVE';
    ELSIF EXISTS (SELECT 1
                    FROM {schema}.m01_staff_roles AS r
                    JOIN {schema}.m01_staff AS s ON s.staff_id = r.staff_id
                   WHERE r.role = 'clinic_admin' AND s.status = 'active') THEN
        v_outcome := 'ADMIN_EXISTS';
    ELSE
        v_outcome := 'BOOTSTRAPPED';
    END IF;
    INSERT INTO {schema}.m01_staff_bootstrap_keys (client_action_id, digest, actor_id,
        staff_id, outcome, created_at)
    VALUES (v_key, v_digest, v_actor, v_staff, v_outcome, v_now);
    IF v_outcome = 'BOOTSTRAPPED' THEN
        INSERT INTO {schema}.m01_staff_roles (staff_id, role, granted_by, granted_at)
        VALUES (v_staff, 'clinic_admin', 'support:' || v_actor, v_now);
        INSERT INTO {schema}.m01_staff_bootstrap_log (log_id, client_action_id, staff_id,
            support_actor_id, grant_id, ticket_reference, at)
        VALUES (pg_catalog.gen_random_uuid(), v_key, v_staff, v_actor, v_grant, v_ticket,
            v_now);
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', v_outcome);
END;
"""
)

_T027_SHA256: Final = "68f1ce2ceda6eb5ad4cbd12309c20cc5b63a246c4d5a7586ae2179c1ba437920"
_T028_SHA256: Final = "4a4c735e911ea2f95f9ed64488b2b84c086aa7647cc000a6e3bd9610516372f1"
_T029_SHA256: Final = "482e248532e62c2bc3c36f2526174da4a475fa2f6539d21adfc7a78760a81141"
_T032_SHA256: Final = "190a4298c6d8939f2f9115f362d13ef959b2f2b3711faacd72ac36bdc0ec999c"

_Params = tuple[tuple[str, str], ...]
# (migration id, function, parameters, return type, volatility, EXECUTE grantee, body,
# reviewed body SHA-256). Every W3a staff function is VOLATILE: each takes row locks.
_FUNCTIONS: Final[tuple[tuple[str, str, _Params, str, str, str, str, str], ...]] = (
    ("t027_m01_staff_self_standing", "m01_staff_self_standing",
     (("p_attribution", "jsonb"),), "jsonb", "volatile", RUNTIME_ROLE,
     _SELF_STANDING_BODY, _T027_SHA256),
    ("t028_m01_staff_member_standing", "m01_staff_member_standing",
     (("p_staff_id", "uuid"),), "jsonb", "volatile", M05_OWNER_ROLE,
     _MEMBER_STANDING_BODY, _T028_SHA256),
    ("t029_m01_staff_admin_v2", "m01_staff_admin_v2",
     (("p_command", "jsonb"),), "jsonb", "volatile", RUNTIME_ROLE,
     _ADMIN_V2_BODY, _T029_SHA256),
    ("t032_m01_staff_bootstrap_admin", "m01_staff_bootstrap_admin",
     (("p_command", "jsonb"),), "jsonb", "volatile", SUPPORT_ROLE,
     _BOOTSTRAP_BODY, _T032_SHA256),
)

L1W_STAFF_UNITS: Final = MappingProxyType(
    {mid: _definition(name, params, ret, vol, grantee, body, sha)
     for mid, name, params, ret, vol, grantee, body, sha in _FUNCTIONS}
)
L1W_STAFF_PROFILES: Final = MappingProxyType(
    {mid: _profile(name, params, ret, vol, grantee)
     for mid, name, params, ret, vol, grantee, _body, _sha in _FUNCTIONS}
)
L1W_STAFF_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {T026_MIGRATION_ID: T026_SQL, **L1W_STAFF_UNITS}
)
