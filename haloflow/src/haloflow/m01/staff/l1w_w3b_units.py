"""L-1W W3b tenant units: the first-member bootstrap (owner decision OD-FM1,
2026-11-03) and its storage. Plan v1r1 §1-§3 + addendum 1 (Codex 150630Z, 151751Z).

- ``t033`` (ordinary, migrator-owned): adds ``operation`` to the bootstrap keys and
  log, ``role`` and ``created_display_name`` to the log, and widens the key outcomes
  with ``TARGET_EXISTS``. The DEFAULTs keep t032's accepted body valid unchanged: its
  rows record ``bootstrap_admin`` / ``clinic_admin``. t026 bytes are untouched.
- ``t034`` ``m01_staff_bootstrap_first_admin(jsonb)`` (staff owner, EXECUTE to
  haloflow_support only): HaloVox support creates a clinic's FIRST staff member and
  grants clinic_admin in one audited step, only while the clinic has no active
  clinic_admin. No follow-up access is granted (the admin grants it afterwards through
  t029). Never writes m01_staff_admin_log or m01_staff_action_keys: the bootstrap log
  is the audit of record (stated limitation, carried with W3a carry-forward 2).

The closure units t035/t036 (``acl_closure_v1``) live in ``haloflow.composition``
next to the amendments that define them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.units import UnitDefinitions
from haloflow.m01.staff.l1w_units import _ISOLATION_GUARD, _TENANT_GUARD, SUPPORT_ROLE
from haloflow.m01.staff.units import DISPLAY_NAME_CHECK, _definition, _profile

T033_MIGRATION_ID: Final = "t033_m01_staff_bootstrap_first_admin_schema"
T034_MIGRATION_ID: Final = "t034_m01_staff_bootstrap_first_admin"
FIRST_ADMIN_FUNCTION: Final = "m01_staff_bootstrap_first_admin"

_S: Final = "{schema}"
_LOG_NAME_CHECK: Final = DISPLAY_NAME_CHECK.replace("display_name", "created_display_name")

T033_SQL: Final = f"""
-- t033:step:bootstrap_keys
ALTER TABLE {_S}.m01_staff_bootstrap_keys
    ADD COLUMN operation varchar(24) NOT NULL DEFAULT 'bootstrap_admin'
        CONSTRAINT m01_staff_bootstrap_keys_operation_check
        CHECK (operation IN ('bootstrap_admin', 'create_first_admin'));
ALTER TABLE {_S}.m01_staff_bootstrap_keys
    DROP CONSTRAINT m01_staff_bootstrap_keys_outcome_check;
ALTER TABLE {_S}.m01_staff_bootstrap_keys
    ADD CONSTRAINT m01_staff_bootstrap_keys_outcome_check
    CHECK (outcome IN ('BOOTSTRAPPED', 'ADMIN_EXISTS', 'TARGET_NOT_ACTIVE', 'TARGET_EXISTS'));

-- t033:step:bootstrap_log
ALTER TABLE {_S}.m01_staff_bootstrap_log
    ADD COLUMN operation varchar(24) NOT NULL DEFAULT 'bootstrap_admin'
        CONSTRAINT m01_staff_bootstrap_log_operation_check
        CHECK (operation IN ('bootstrap_admin', 'create_first_admin')),
    ADD COLUMN role varchar(16) NOT NULL DEFAULT 'clinic_admin'
        CONSTRAINT m01_staff_bootstrap_log_role_check CHECK (role = 'clinic_admin'),
    ADD COLUMN created_display_name varchar(80),
    ADD CONSTRAINT m01_staff_bootstrap_log_name_iff_create
        CHECK ((created_display_name IS NOT NULL) = (operation = 'create_first_admin')),
    ADD CONSTRAINT m01_staff_bootstrap_log_name_valid
        CHECK (created_display_name IS NULL OR {_LOG_NAME_CHECK});
"""

# t034 (plan v1r1 §3 + addendum 1). Returned codes, never raised (environment faults
# excepted). Order: tenant; READ COMMITTED; closed shape (exactly client_action_id,
# display_name, schema_version, staff_id); support login by OID AND name; this
# schema's tenant; an active capable grant; GUARD FOR UPDATE; login and grant
# RE-CHECKED in fresh statements after the guard; the action key (actor mismatch;
# another operation or another digest -> conflict; otherwise the stored outcome);
# ADMIN_EXISTS; TARGET_EXISTS; then member + role + key + log, with a unique
# violation on the member insert (a racing raw insert the guard does not cover)
# caught in a sub-block (savepoint) and returned as TARGET_EXISTS. The digest binds
# operation, tenant, key, actor, staff id and display name -- NOT the grant or ticket
# (addendum 1 pin 1); the winning grant and ticket are stored in the log row.
_FIRST_ADMIN_BODY: Final = (
    r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    c_ws constant text := '[\u0009-\u000d \u0085  '
        || ' -     　]';
    c_forbidden constant text := '[\u0001-\u001f\u007f-\u009f  ]';
    v_setting text;
    v_keys text[];
    v_key uuid;
    v_staff uuid;
    v_name text;
    v_actor varchar(128);
    v_tenant varchar(64);
    v_grant uuid;
    v_ticket varchar(128);
    v_digest bytea;
    v_stored record;
    v_outcome text;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN"""
    + _TENANT_GUARD
    + _ISOLATION_GUARD
    + r"""
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'COMMAND_MALFORMED');
    END IF;
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(p_command) AS k;
    IF v_keys IS DISTINCT FROM
           ARRAY['client_action_id', 'display_name', 'schema_version', 'staff_id']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version') IS DISTINCT
           FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'client_action_id') IS
           DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'client_action_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'staff_id') IS DISTINCT FROM
           'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'staff_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'display_name') IS DISTINCT
           FROM 'string' THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'COMMAND_MALFORMED');
    END IF;
    v_name := p_command OPERATOR(pg_catalog.->>) 'display_name';
    IF NOT (v_name IS NFC NORMALIZED)
       OR pg_catalog.char_length(v_name) NOT BETWEEN 1 AND 80
       OR v_name OPERATOR(pg_catalog.~) c_forbidden
       OR v_name OPERATOR(pg_catalog.~) ('^' || c_ws)
       OR v_name OPERATOR(pg_catalog.~) (c_ws || '$') THEN
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

    -- The tenant-local guard: serializes with t029 role/status changes and t032.
    PERFORM 1 FROM {schema}.m01_staff_role_guard AS g WHERE g.id FOR UPDATE;

    -- Re-authorization after the wait, in fresh statements: the login identity ...
    PERFORM 1
       FROM shared.support_operator_logins AS l
      WHERE l.actor_id = v_actor
        AND l.login_name = session_user
        AND l.login_oid = (SELECT r.oid FROM pg_catalog.pg_roles AS r
                            WHERE r.rolname = session_user);
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', 'SUPPORT_NOT_AUTHORIZED');
    END IF;
    -- ... and a currently capable grant (deterministic choice; may differ from the
    -- original on a replay, which is why neither is digest-bound).
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
        '["m01.staff_bootstrap_first_admin/v1",' || pg_catalog.to_json(v_tenant)::text
        || ',' || pg_catalog.to_json(v_key::text)::text
        || ',' || pg_catalog.to_json(v_actor)::text
        || ',"create_first_admin"'
        || ',' || pg_catalog.to_json(v_staff::text)::text
        || ',' || pg_catalog.to_json(v_name)::text || ']', 'UTF8'));
    SELECT k.actor_id, k.digest, k.outcome, k.operation INTO v_stored
      FROM {schema}.m01_staff_bootstrap_keys AS k
     WHERE k.client_action_id = v_key;
    IF FOUND THEN
        IF v_stored.actor_id IS DISTINCT FROM v_actor THEN
            RETURN pg_catalog.jsonb_build_object('schema_version', 1,
                'outcome', 'ACTION_KEY_ACTOR_MISMATCH');
        END IF;
        IF v_stored.operation IS DISTINCT FROM 'create_first_admin'
           OR v_stored.digest IS DISTINCT FROM v_digest THEN
            RETURN pg_catalog.jsonb_build_object('schema_version', 1,
                'outcome', 'ACTION_KEY_CONFLICT');
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1,
            'outcome', v_stored.outcome, 'replayed', true);
    END IF;

    IF EXISTS (SELECT 1
                 FROM {schema}.m01_staff_roles AS r
                 JOIN {schema}.m01_staff AS s ON s.staff_id = r.staff_id
                WHERE r.role = 'clinic_admin' AND s.status = 'active') THEN
        v_outcome := 'ADMIN_EXISTS';
    ELSIF EXISTS (SELECT 1 FROM {schema}.m01_staff AS s WHERE s.staff_id = v_staff) THEN
        v_outcome := 'TARGET_EXISTS';
    ELSE
        BEGIN
            INSERT INTO {schema}.m01_staff (staff_id, status, status_changed_at, display_name)
            VALUES (v_staff, 'active', v_now, v_name);
            INSERT INTO {schema}.m01_staff_roles (staff_id, role, granted_by, granted_at)
            VALUES (v_staff, 'clinic_admin', 'support:' || v_actor, v_now);
            v_outcome := 'BOOTSTRAPPED';
        EXCEPTION WHEN unique_violation THEN
            -- A member with this id committed while this call passed the existence
            -- check (a write the guard does not cover): nothing of this call survives.
            v_outcome := 'TARGET_EXISTS';
        END;
    END IF;
    INSERT INTO {schema}.m01_staff_bootstrap_keys (client_action_id, digest, actor_id,
        staff_id, outcome, created_at, operation)
    VALUES (v_key, v_digest, v_actor, v_staff, v_outcome, v_now, 'create_first_admin');
    IF v_outcome = 'BOOTSTRAPPED' THEN
        INSERT INTO {schema}.m01_staff_bootstrap_log (log_id, client_action_id, staff_id,
            support_actor_id, grant_id, ticket_reference, at, operation, role,
            created_display_name)
        VALUES (pg_catalog.gen_random_uuid(), v_key, v_staff, v_actor, v_grant, v_ticket,
            v_now, 'create_first_admin', 'clinic_admin', v_name);
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', v_outcome);
END;
"""
)

_T034_SHA256: Final = "ea2e30dc2bd07417352befc5484e33090622e3974e1201f6b271d14c6c021f0f"

_PARAMS: Final = (("p_command", "jsonb"),)

L1W_W3B_STAFF_UNITS: Final = MappingProxyType({
    T034_MIGRATION_ID: _definition(FIRST_ADMIN_FUNCTION, _PARAMS, "jsonb", "volatile",
                                   SUPPORT_ROLE, _FIRST_ADMIN_BODY, _T034_SHA256),
})
L1W_W3B_STAFF_PROFILES: Final = MappingProxyType({
    T034_MIGRATION_ID: _profile(FIRST_ADMIN_FUNCTION, _PARAMS, "jsonb", "volatile",
                                SUPPORT_ROLE),
})
L1W_W3B_STAFF_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {T033_MIGRATION_ID: T033_SQL, **L1W_W3B_STAFF_UNITS}
)
