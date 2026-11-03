"""L-1W W2: shared identity bindings, per-device sessions and per-clinic auth settings.

Revision ID: 008
Revises: 007
Create Date: 2026-11-02

L-1W package v1 + v1r1, consolidated investigation (Codex final disposition ALIGN),
W2 red-test plan v1 (ALIGN 20261102T185918Z).

Roles (created only if absent and never altered, the 004-007 rule), each with one
migrator edge ``INHERIT FALSE, SET TRUE, ADMIN FALSE``:
- ``haloflow_identity_owner`` owns every object below; no other privilege except
  column SELECT on eight ``shared.support_access_grants`` columns;
- ``haloflow_support`` is the NOLOGIN execution role for support operations; its
  membership is granted to a support login out of band, never here.

Every ``shared.l1w_*`` function is SECURITY DEFINER, owned by the identity owner,
``SET search_path = pg_catalog, pg_temp``, fully qualified, READ COMMITTED guarded.
Policy refusals are returned as fixed outcome codes; exceptions are reserved for
integrity and infrastructure failures and carry fixed text only (no issuer, subject,
sid or tenant id). Lock order on every path: anchor -> bindings -> sessions.

``downgrade()`` raises, consistent with ``001``-``007``.
"""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None

IDENTITY_OWNER = "haloflow_identity_owner"
SUPPORT = "haloflow_support"

# Fail-closed preflight (Codex 20261102T191746Z): a pre-existing role must already be
# safe. Refuse before any mutation; never ALTER an existing role (the 004-007 rule).
PREFLIGHT_SQL = f"""
DO $l1w$
DECLARE
    v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['{IDENTITY_OWNER}', '{SUPPORT}'] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = v_role
                   AND (r.rolcanlogin OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
                        OR r.rolreplication OR r.rolbypassrls)) THEN
            RAISE EXCEPTION 'L1W_ROLE_UNSAFE';
        END IF;
        -- no outgoing membership
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members a
                   JOIN pg_catalog.pg_roles m ON m.oid = a.member
                   WHERE m.rolname = v_role) THEN
            RAISE EXCEPTION 'L1W_ROLE_UNSAFE';
        END IF;
        -- a pre-existing migrator edge must have exactly the pinned options
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members a
                   JOIN pg_catalog.pg_roles r ON r.oid = a.roleid
                   JOIN pg_catalog.pg_roles m ON m.oid = a.member
                   WHERE r.rolname = v_role AND m.rolname = 'haloflow_migrator'
                     AND (a.inherit_option OR NOT a.set_option OR a.admin_option)) THEN
            RAISE EXCEPTION 'L1W_ROLE_UNSAFE';
        END IF;
    END LOOP;
    -- identity owner: no incoming member other than the migrator
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members a
               JOIN pg_catalog.pg_roles r ON r.oid = a.roleid
               JOIN pg_catalog.pg_roles m ON m.oid = a.member
               WHERE r.rolname = '{IDENTITY_OWNER}' AND m.rolname <> 'haloflow_migrator') THEN
        RAISE EXCEPTION 'L1W_ROLE_UNSAFE';
    END IF;
    -- support: other incoming members must be LOGIN, non-superuser, without ADMIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members a
               JOIN pg_catalog.pg_roles r ON r.oid = a.roleid
               JOIN pg_catalog.pg_roles m ON m.oid = a.member
               WHERE r.rolname = '{SUPPORT}' AND m.rolname <> 'haloflow_migrator'
                 AND (NOT m.rolcanlogin OR m.rolsuper OR a.admin_option)) THEN
        RAISE EXCEPTION 'L1W_ROLE_UNSAFE';
    END IF;
END
$l1w$;
"""

ROLES_SQL = f"""
DO $l1w$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{IDENTITY_OWNER}') THEN
        CREATE ROLE {IDENTITY_OWNER}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{SUPPORT}') THEN
        CREATE ROLE {SUPPORT}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$l1w$;
GRANT {IDENTITY_OWNER} TO haloflow_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
GRANT {SUPPORT} TO haloflow_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
"""

TABLES_SQL = """
CREATE TABLE shared.staff_identity_anchor (
    issuer text NOT NULL CHECK (length(issuer) BETWEEN 1 AND 512),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, subject)
);

CREATE TABLE shared.staff_identity_bindings (
    binding_id uuid PRIMARY KEY,
    issuer text NOT NULL,
    subject text NOT NULL,
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    staff_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    created_by_kind text NOT NULL CHECK (created_by_kind IN ('clinic_admin', 'support')),
    created_by varchar(128) NOT NULL,
    revoked_at timestamptz,
    revoked_by_kind text CHECK (revoked_by_kind IN ('clinic_admin', 'support')),
    revoked_by varchar(128),
    FOREIGN KEY (issuer, subject) REFERENCES shared.staff_identity_anchor (issuer, subject),
    CONSTRAINT staff_identity_bindings_revocation CHECK (
        (revoked_at IS NULL AND revoked_by_kind IS NULL AND revoked_by IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by_kind IS NOT NULL AND revoked_by IS NOT NULL))
);
CREATE UNIQUE INDEX staff_identity_bindings_active_identity
    ON shared.staff_identity_bindings (issuer, subject) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX staff_identity_bindings_active_staff
    ON shared.staff_identity_bindings (tenant_id, staff_id) WHERE revoked_at IS NULL;

CREATE TABLE shared.staff_sessions (
    issuer text NOT NULL,
    subject text NOT NULL,
    sid text NOT NULL CHECK (length(sid) BETWEEN 1 AND 255),
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    staff_id uuid NOT NULL,
    auth_time timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    last_activity_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoke_reason text CHECK (revoke_reason IN
        ('logout', 'idle', 'absolute', 'binding_revoked', 'binding_mismatch')),
    PRIMARY KEY (issuer, subject, sid),
    CONSTRAINT staff_sessions_revocation CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL))
);

CREATE TABLE shared.tenant_auth_settings (
    tenant_id varchar(64) PRIMARY KEY REFERENCES shared.tenants(tenant_id),
    idle_timeout_minutes integer NOT NULL DEFAULT 15
        CHECK (idle_timeout_minutes BETWEEN 5 AND 60),
    mfa_required boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL,
    updated_by_kind text NOT NULL CHECK (updated_by_kind IN ('clinic_admin', 'support')),
    updated_by varchar(128) NOT NULL
);

CREATE TABLE shared.tenant_auth_settings_log (
    log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    setting text NOT NULL CHECK (setting IN ('idle_timeout_minutes', 'mfa_required')),
    old_value text NOT NULL,
    new_value text NOT NULL,
    actor_kind text NOT NULL CHECK (actor_kind IN ('clinic_admin', 'support')),
    actor_id varchar(128) NOT NULL,
    support_grant_id uuid,
    ticket_reference varchar(128),
    at timestamptz NOT NULL,
    CONSTRAINT tenant_auth_settings_log_support CHECK (
        (actor_kind = 'support') = (support_grant_id IS NOT NULL AND ticket_reference IS NOT NULL))
);

CREATE TABLE shared.staff_identity_log (
    log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    staff_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('bind', 'unbind')),
    actor_kind text NOT NULL CHECK (actor_kind IN ('clinic_admin', 'support')),
    actor_id varchar(128) NOT NULL,
    support_grant_id uuid,
    ticket_reference varchar(128),
    at timestamptz NOT NULL,
    CONSTRAINT staff_identity_log_support CHECK (
        (actor_kind = 'support') = (support_grant_id IS NOT NULL AND ticket_reference IS NOT NULL))
);

CREATE TABLE shared.support_operator_logins (
    login_oid oid PRIMARY KEY,
    login_name name NOT NULL UNIQUE,
    actor_id varchar(128) NOT NULL,
    added_at timestamptz NOT NULL,
    decision_ref text NOT NULL CHECK (decision_ref ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE shared.staff_identity_anchor IS
    'M01 classification: pseudonymous-id; PHI prohibited';
COMMENT ON TABLE shared.staff_identity_bindings IS
    'M01 classification: pseudonymous-id; PHI prohibited';
COMMENT ON TABLE shared.staff_sessions IS
    'M01 classification: pseudonymous-id; PHI prohibited';
COMMENT ON TABLE shared.tenant_auth_settings IS
    'M01 classification: control-plane; PHI prohibited';
COMMENT ON TABLE shared.tenant_auth_settings_log IS
    'M01 classification: control-plane; PHI prohibited';
COMMENT ON TABLE shared.staff_identity_log IS
    'M01 classification: pseudonymous-id; PHI prohibited';
COMMENT ON TABLE shared.support_operator_logins IS
    'M01 classification: pseudonymous-id; PHI prohibited';
"""

TRIGGERS_SQL = """
CREATE FUNCTION shared.staff_sessions_guard_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $l1w$
BEGIN
    IF NEW.issuer IS DISTINCT FROM OLD.issuer OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.sid IS DISTINCT FROM OLD.sid OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.staff_id IS DISTINCT FROM OLD.staff_id
       OR NEW.auth_time IS DISTINCT FROM OLD.auth_time
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'staff session identity is immutable';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'revoked staff session is final';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.revoked_at IS NULL) <> (NEW.revoke_reason IS NULL) THEN
        RAISE EXCEPTION 'staff session revocation needs a time and a reason together';
    END IF;
    IF NEW.revoked_at IS NOT NULL
       AND NEW.last_activity_at IS DISTINCT FROM OLD.last_activity_at THEN
        RAISE EXCEPTION 'staff session revocation cannot change activity';
    END IF;
    IF NEW.last_activity_at < OLD.last_activity_at THEN
        RAISE EXCEPTION 'staff session activity cannot decrease';
    END IF;
    RETURN NEW;
END
$l1w$;

CREATE FUNCTION shared.staff_sessions_guard_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $l1w$
BEGIN
    IF NEW.revoked_at IS NOT NULL AND NEW.revoke_reason IS DISTINCT FROM 'logout' THEN
        RAISE EXCEPTION 'only a logout tombstone may be created revoked';
    END IF;
    RETURN NEW;
END
$l1w$;

CREATE FUNCTION shared.staff_sessions_guard_delete()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $l1w$
BEGIN
    RAISE EXCEPTION 'staff sessions are retained';
END
$l1w$;

CREATE TRIGGER staff_sessions_guard_update
BEFORE UPDATE ON shared.staff_sessions
FOR EACH ROW EXECUTE FUNCTION shared.staff_sessions_guard_update();
CREATE TRIGGER staff_sessions_guard_insert
BEFORE INSERT ON shared.staff_sessions
FOR EACH ROW EXECUTE FUNCTION shared.staff_sessions_guard_insert();
CREATE TRIGGER staff_sessions_guard_delete
BEFORE DELETE ON shared.staff_sessions
FOR EACH ROW EXECUTE FUNCTION shared.staff_sessions_guard_delete();

CREATE TRIGGER tenant_auth_settings_log_append_only
BEFORE UPDATE OR DELETE ON shared.tenant_auth_settings_log
FOR EACH ROW EXECUTE FUNCTION shared.reject_append_only_change();
CREATE TRIGGER staff_identity_log_append_only
BEFORE UPDATE OR DELETE ON shared.staff_identity_log
FOR EACH ROW EXECUTE FUNCTION shared.reject_append_only_change();
"""

_GUARD = """
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'l1w definer requires READ COMMITTED';
    END IF;"""

_IDENTITY_INVALID = (
    "p_issuer IS NULL OR p_subject IS NULL OR length(p_issuer) NOT BETWEEN 1 AND 512"
    " OR length(p_subject) NOT BETWEEN 1 AND 255"
)

ADMIT_SQL = f"""
CREATE FUNCTION shared.l1w_admit(
    p_issuer text, p_subject text, p_sid text, p_auth_time timestamptz, p_assured boolean)
RETURNS TABLE (outcome text, tenant_id varchar(64), staff_id uuid)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $l1w$
#variable_conflict use_column
DECLARE
    c_skew CONSTANT interval := interval '60 seconds';
    c_max_session CONSTANT interval := interval '12 hours';
    c_coalesce CONSTANT interval := interval '60 seconds';
    v_tenant varchar(64);
    v_staff uuid;
    v_idle integer;
    v_mfa boolean;
    v_t timestamptz;
    s shared.staff_sessions%ROWTYPE;
BEGIN{_GUARD}
    IF {_IDENTITY_INVALID} OR p_sid IS NULL OR length(p_sid) NOT BETWEEN 1 AND 255
       OR p_auth_time IS NULL THEN
        RETURN QUERY SELECT 'INVALID_INPUT'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    -- 1. anchor SHARE; 2. active binding read after the lock.
    PERFORM 1 FROM shared.staff_identity_anchor a
        WHERE a.issuer = p_issuer AND a.subject = p_subject FOR SHARE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'UNBOUND'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    SELECT b.tenant_id, b.staff_id INTO v_tenant, v_staff
        FROM shared.staff_identity_bindings b
        WHERE b.issuer = p_issuer AND b.subject = p_subject AND b.revoked_at IS NULL;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'UNBOUND'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    -- 3. pre-insert gate (creation only); 4. insert unless past the 12 h limit.
    v_t := clock_timestamp();
    IF p_auth_time > v_t + c_skew THEN
        RETURN QUERY SELECT 'AUTH_TIME_FUTURE'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    IF v_t < p_auth_time + c_max_session THEN
        INSERT INTO shared.staff_sessions AS ss (issuer, subject, sid, tenant_id, staff_id,
            auth_time, created_at, last_activity_at)
        VALUES (p_issuer, p_subject, p_sid, v_tenant, v_staff, p_auth_time, v_t, v_t)
        ON CONFLICT (issuer, subject, sid) DO NOTHING;
    END IF;
    -- 5. locked full validation, clock sampled after the lock.
    SELECT * INTO s FROM shared.staff_sessions x
        WHERE x.issuer = p_issuer AND x.subject = p_subject AND x.sid = p_sid FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'ABSOLUTE'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    v_t := clock_timestamp();
    IF s.revoked_at IS NOT NULL THEN
        RETURN QUERY SELECT 'REVOKED'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    IF s.tenant_id <> v_tenant OR s.staff_id <> v_staff THEN
        UPDATE shared.staff_sessions x SET revoked_at = v_t, revoke_reason = 'binding_mismatch'
            WHERE x.issuer = p_issuer AND x.subject = p_subject AND x.sid = p_sid;
        RETURN QUERY SELECT 'BINDING_MISMATCH'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    IF s.auth_time <> p_auth_time THEN
        RETURN QUERY SELECT 'SID_MISMATCH'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    IF v_t >= s.auth_time + c_max_session THEN
        UPDATE shared.staff_sessions x SET revoked_at = v_t, revoke_reason = 'absolute'
            WHERE x.issuer = p_issuer AND x.subject = p_subject AND x.sid = p_sid;
        RETURN QUERY SELECT 'ABSOLUTE'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    IF s.auth_time > v_t + c_skew THEN
        RETURN QUERY SELECT 'AUTH_TIME_FUTURE'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    SELECT st.idle_timeout_minutes, st.mfa_required INTO v_idle, v_mfa
        FROM shared.tenant_auth_settings st WHERE st.tenant_id = v_tenant;
    v_idle := coalesce(v_idle, 15);
    v_mfa := coalesce(v_mfa, false);
    IF v_t - s.last_activity_at >= make_interval(mins => v_idle) THEN
        UPDATE shared.staff_sessions x SET revoked_at = v_t, revoke_reason = 'idle'
            WHERE x.issuer = p_issuer AND x.subject = p_subject AND x.sid = p_sid;
        RETURN QUERY SELECT 'IDLE'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    IF v_mfa AND p_assured IS NOT TRUE THEN
        RETURN QUERY SELECT 'MFA_REQUIRED'::text, NULL::varchar(64), NULL::uuid;
        RETURN;
    END IF;
    IF v_t - s.last_activity_at >= c_coalesce THEN
        UPDATE shared.staff_sessions x SET last_activity_at = v_t
            WHERE x.issuer = p_issuer AND x.subject = p_subject AND x.sid = p_sid;
    END IF;
    RETURN QUERY SELECT 'ADMITTED'::text, v_tenant, v_staff;
END
$l1w$;

CREATE FUNCTION shared.l1w_logout(
    p_issuer text, p_subject text, p_sid text, p_auth_time timestamptz)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $l1w$
DECLARE
    v_tenant varchar(64);
    v_staff uuid;
    v_revoked timestamptz;
    v_t timestamptz;
BEGIN{_GUARD}
    IF {_IDENTITY_INVALID} OR p_sid IS NULL OR length(p_sid) NOT BETWEEN 1 AND 255
       OR p_auth_time IS NULL THEN
        RETURN 'INVALID_INPUT';
    END IF;
    PERFORM 1 FROM shared.staff_identity_anchor a
        WHERE a.issuer = p_issuer AND a.subject = p_subject FOR SHARE;
    IF NOT FOUND THEN
        RETURN 'UNBOUND';
    END IF;
    SELECT b.tenant_id, b.staff_id INTO v_tenant, v_staff
        FROM shared.staff_identity_bindings b
        WHERE b.issuer = p_issuer AND b.subject = p_subject AND b.revoked_at IS NULL;
    IF NOT FOUND THEN
        RETURN 'UNBOUND';
    END IF;
    v_t := clock_timestamp();
    INSERT INTO shared.staff_sessions (issuer, subject, sid, tenant_id, staff_id, auth_time,
        created_at, last_activity_at, revoked_at, revoke_reason)
    VALUES (p_issuer, p_subject, p_sid, v_tenant, v_staff, p_auth_time, v_t, v_t, v_t, 'logout')
    ON CONFLICT (issuer, subject, sid) DO NOTHING;
    SELECT x.revoked_at INTO v_revoked FROM shared.staff_sessions x
        WHERE x.issuer = p_issuer AND x.subject = p_subject AND x.sid = p_sid FOR UPDATE;
    IF v_revoked IS NULL THEN
        UPDATE shared.staff_sessions x
            SET revoked_at = clock_timestamp(), revoke_reason = 'logout'
            WHERE x.issuer = p_issuer AND x.subject = p_subject AND x.sid = p_sid;
    END IF;
    RETURN 'LOGGED_OUT';
END
$l1w$;
"""

# ---- admin and support writers, generated from one template per operation -----

_SUPPORT_RESOLVE = """
    SELECT l.actor_id INTO v_actor FROM shared.support_operator_logins l
        WHERE l.login_name = session_user
          AND l.login_oid = (SELECT r.oid FROM pg_catalog.pg_roles r
                             WHERE r.rolname = session_user);
    IF NOT FOUND THEN
        RETURN {refuse};
    END IF;"""

_SUPPORT_GRANT = """
    v_grant := NULL;
    SELECT g.grant_id, g.ticket_reference INTO v_grant, v_ticket
        FROM shared.support_access_grants g
        WHERE g.actor_id = v_actor AND g.tenant_id = p_tenant AND g.state = 'active'
          AND g.starts_at <= clock_timestamp() AND clock_timestamp() < g.expires_at
          AND '{capability}' = ANY (g.capabilities)
        ORDER BY g.expires_at DESC, g.grant_id
        LIMIT 1;
    IF v_grant IS NULL THEN
        RETURN {refuse};
    END IF;"""


def _support_check(capability: str, refuse: str = "'SUPPORT_NOT_AUTHORIZED'") -> str:
    return _SUPPORT_RESOLVE.format(refuse=refuse) + _SUPPORT_GRANT.format(
        capability=capability, refuse=refuse)


def _writer(kind: str, name: str, params: str, body: str, capability: str) -> str:
    """One admin or support definer. ``body`` holds {pre} and {post} markers."""

    if kind == "admin":
        declare_actor = "v_actor varchar(128) := p_actor::text;"
        pre = """
    IF p_actor IS NULL THEN
        RETURN 'INVALID_INPUT';
    END IF;"""
        post = ""
        actor_kind = "clinic_admin"
    else:
        declare_actor = "v_actor varchar(128);"
        pre = _support_check(capability)
        # The authoritative re-check, last before the write (investigation §2.1). Inside
        # an exception block it raises L1W03 so the block's writes roll back.
        raising = "unbind" not in name
        post = _SUPPORT_GRANT.format(capability=capability, refuse="'SUPPORT_NOT_AUTHORIZED'")
        if raising:
            post = post.replace(
                "RETURN 'SUPPORT_NOT_AUTHORIZED';", "RAISE EXCEPTION USING ERRCODE = 'L1W03';")
        actor_kind = "support"
    return f"""
CREATE FUNCTION shared.{name}({params})
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $l1w$
#variable_conflict use_column
DECLARE
    {declare_actor}
    v_grant uuid;
    v_ticket varchar(128);
    v_issuer text;
    v_subject text;
    v_binding uuid;
    v_old_idle integer;
    v_old_mfa boolean;
    v_new_idle integer;
    v_new_mfa boolean;
    v_old text;
    v_new text;
    c_kind CONSTANT text := '{actor_kind}';
BEGIN{_GUARD}
""" + body.replace("{pre}", pre).replace("{post}", post) + """
END
$l1w$;
"""


_BIND_BODY = f"""
    IF p_tenant IS NULL OR p_staff IS NULL OR {_IDENTITY_INVALID} THEN
        RETURN 'INVALID_INPUT';
    END IF;{{pre}}
    BEGIN
        INSERT INTO shared.staff_identity_anchor (issuer, subject, created_at)
            VALUES (p_issuer, p_subject, clock_timestamp())
            ON CONFLICT (issuer, subject) DO NOTHING;
        PERFORM 1 FROM shared.staff_identity_anchor a
            WHERE a.issuer = p_issuer AND a.subject = p_subject FOR UPDATE;{{post}}
        IF EXISTS (SELECT 1 FROM shared.staff_identity_bindings b WHERE b.issuer = p_issuer
                   AND b.subject = p_subject AND b.revoked_at IS NULL) THEN
            RAISE EXCEPTION USING ERRCODE = 'L1W01';
        END IF;
        IF EXISTS (SELECT 1 FROM shared.staff_identity_bindings b WHERE b.tenant_id = p_tenant
                   AND b.staff_id = p_staff AND b.revoked_at IS NULL) THEN
            RAISE EXCEPTION USING ERRCODE = 'L1W02';
        END IF;
        INSERT INTO shared.staff_identity_bindings (binding_id, issuer, subject, tenant_id,
            staff_id, created_at, created_by_kind, created_by)
        VALUES (gen_random_uuid(), p_issuer, p_subject, p_tenant, p_staff, clock_timestamp(),
            c_kind, v_actor);
        INSERT INTO shared.staff_identity_log (tenant_id, staff_id, action, actor_kind,
            actor_id, support_grant_id, ticket_reference, at)
        VALUES (p_tenant, p_staff, 'bind', c_kind, v_actor, v_grant, v_ticket,
            clock_timestamp());
    EXCEPTION
        WHEN SQLSTATE 'L1W01' THEN RETURN 'IDENTITY_ALREADY_BOUND';
        WHEN SQLSTATE 'L1W02' THEN RETURN 'STAFF_ALREADY_BOUND';
        WHEN SQLSTATE 'L1W03' THEN RETURN 'SUPPORT_NOT_AUTHORIZED';
        WHEN unique_violation THEN RETURN 'STAFF_ALREADY_BOUND';
        WHEN foreign_key_violation THEN RETURN 'INVALID_INPUT';
    END;
    RETURN 'BOUND';"""

_UNBIND_BODY = """
    IF p_tenant IS NULL OR p_staff IS NULL THEN
        RETURN 'INVALID_INPUT';
    END IF;{pre}
    SELECT b.issuer, b.subject INTO v_issuer, v_subject FROM shared.staff_identity_bindings b
        WHERE b.tenant_id = p_tenant AND b.staff_id = p_staff AND b.revoked_at IS NULL;
    IF NOT FOUND THEN
        RETURN 'NOT_BOUND';
    END IF;
    PERFORM 1 FROM shared.staff_identity_anchor a
        WHERE a.issuer = v_issuer AND a.subject = v_subject FOR UPDATE;
    -- Post-lock re-read: the same active (tenant, staff) binding must still belong
    -- to the locked anchor; otherwise refuse without touching another anchor.
    SELECT b.binding_id INTO v_binding FROM shared.staff_identity_bindings b
        WHERE b.tenant_id = p_tenant AND b.staff_id = p_staff AND b.revoked_at IS NULL
          AND b.issuer = v_issuer AND b.subject = v_subject;
    IF NOT FOUND THEN
        RETURN 'NOT_BOUND';
    END IF;{post}
    UPDATE shared.staff_identity_bindings b
        SET revoked_at = clock_timestamp(), revoked_by_kind = c_kind, revoked_by = v_actor
        WHERE b.binding_id = v_binding;
    UPDATE shared.staff_sessions x
        SET revoked_at = clock_timestamp(), revoke_reason = 'binding_revoked'
        WHERE x.issuer = v_issuer AND x.subject = v_subject AND x.revoked_at IS NULL;
    INSERT INTO shared.staff_identity_log (tenant_id, staff_id, action, actor_kind, actor_id,
        support_grant_id, ticket_reference, at)
    VALUES (p_tenant, p_staff, 'unbind', c_kind, v_actor, v_grant, v_ticket, clock_timestamp());
    RETURN 'UNBOUND_DONE';"""

_SET_BODY = """
    IF p_tenant IS NULL THEN
        RETURN 'INVALID_INPUT';
    END IF;
    IF p_setting IS NULL OR p_setting NOT IN ('idle_timeout_minutes', 'mfa_required') THEN
        RETURN 'INVALID_SETTING';
    END IF;
    IF p_value IS NULL THEN
        RETURN 'INVALID_VALUE';
    END IF;
    IF p_setting = 'idle_timeout_minutes' THEN
        IF p_value !~ '^[0-9]{1,2}$' THEN
            RETURN 'INVALID_VALUE';
        END IF;
        IF p_value::integer NOT BETWEEN 5 AND 60 THEN
            RETURN 'INVALID_VALUE';
        END IF;
        v_new := p_value::integer::text;
    ELSE
        IF p_value NOT IN ('true', 'false') THEN
            RETURN 'INVALID_VALUE';
        END IF;
        v_new := p_value;
    END IF;{pre}
    BEGIN
        -- Serialize first setters (Codex 20261102T194326Z): materialize the default
        -- row, so a concurrent first writer waits on it and every writer locks and
        -- re-reads an existing row. A refusal below rolls this provisional row back.
        INSERT INTO shared.tenant_auth_settings AS st (tenant_id, idle_timeout_minutes,
            mfa_required, updated_at, updated_by_kind, updated_by)
        VALUES (p_tenant, 15, false, clock_timestamp(), c_kind, v_actor)
        ON CONFLICT (tenant_id) DO NOTHING;
        SELECT st.idle_timeout_minutes, st.mfa_required INTO v_old_idle, v_old_mfa
            FROM shared.tenant_auth_settings st WHERE st.tenant_id = p_tenant FOR UPDATE;
        v_new_idle := v_old_idle;
        v_new_mfa := v_old_mfa;
        IF p_setting = 'idle_timeout_minutes' THEN
            v_old := v_old_idle::text;
            v_new_idle := v_new::integer;
        ELSE
            v_old := v_old_mfa::text;
            v_new_mfa := v_new::boolean;
        END IF;{post}
        INSERT INTO shared.tenant_auth_settings AS st (tenant_id, idle_timeout_minutes,
            mfa_required, updated_at, updated_by_kind, updated_by)
        VALUES (p_tenant, v_new_idle, v_new_mfa, clock_timestamp(), c_kind, v_actor)
        ON CONFLICT (tenant_id) DO UPDATE SET idle_timeout_minutes = EXCLUDED.idle_timeout_minutes,
            mfa_required = EXCLUDED.mfa_required, updated_at = EXCLUDED.updated_at,
            updated_by_kind = EXCLUDED.updated_by_kind, updated_by = EXCLUDED.updated_by;
        INSERT INTO shared.tenant_auth_settings_log (tenant_id, setting, old_value, new_value,
            actor_kind, actor_id, support_grant_id, ticket_reference, at)
        VALUES (p_tenant, p_setting, v_old, v_new, c_kind, v_actor, v_grant, v_ticket,
            clock_timestamp());
    EXCEPTION
        WHEN SQLSTATE 'L1W03' THEN RETURN 'SUPPORT_NOT_AUTHORIZED';
        WHEN foreign_key_violation THEN RETURN 'INVALID_INPUT';
    END;
    RETURN 'SET';"""

_TENANT = "p_tenant character varying"
WRITERS_SQL = "".join(
    [
        _writer("admin", "l1w_admin_bind",
                f"{_TENANT}, p_staff uuid, p_issuer text, p_subject text, p_actor uuid",
                _BIND_BODY, "l1w.identity.admin"),
        _writer("admin", "l1w_admin_unbind", f"{_TENANT}, p_staff uuid, p_actor uuid",
                _UNBIND_BODY, "l1w.identity.admin"),
        _writer("admin", "l1w_admin_set_setting",
                f"{_TENANT}, p_setting text, p_value text, p_actor uuid",
                _SET_BODY, "l1w.settings.admin"),
        _writer("support", "l1w_support_bind",
                f"{_TENANT}, p_staff uuid, p_issuer text, p_subject text",
                _BIND_BODY, "l1w.identity.admin"),
        _writer("support", "l1w_support_unbind", f"{_TENANT}, p_staff uuid",
                _UNBIND_BODY, "l1w.identity.admin"),
        _writer("support", "l1w_support_set_setting",
                f"{_TENANT}, p_setting text, p_value text",
                _SET_BODY, "l1w.settings.admin"),
    ]
)

LIST_SQL = f"""
CREATE FUNCTION shared.l1w_support_list_tenant_settings(p_tenant character varying)
RETURNS TABLE (outcome text, idle_timeout_minutes integer, mfa_required boolean)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $l1w$
#variable_conflict use_column
DECLARE
    v_actor varchar(128);
    v_grant uuid;
    v_ticket varchar(128);
    v_idle integer;
    v_mfa boolean;
BEGIN{_GUARD}
    IF p_tenant IS NULL THEN
        RETURN QUERY SELECT 'INVALID_INPUT'::text, NULL::integer, NULL::boolean;
        RETURN;
    END IF;
    SELECT l.actor_id INTO v_actor FROM shared.support_operator_logins l
        WHERE l.login_name = session_user
          AND l.login_oid = (SELECT r.oid FROM pg_catalog.pg_roles r
                             WHERE r.rolname = session_user);
    IF FOUND THEN
        SELECT g.grant_id INTO v_grant FROM shared.support_access_grants g
            WHERE g.actor_id = v_actor AND g.tenant_id = p_tenant AND g.state = 'active'
              AND g.starts_at <= clock_timestamp() AND clock_timestamp() < g.expires_at
              AND 'l1w.settings.admin' = ANY (g.capabilities)
            LIMIT 1;
    END IF;
    IF v_grant IS NULL THEN
        RETURN QUERY SELECT 'SUPPORT_NOT_AUTHORIZED'::text, NULL::integer, NULL::boolean;
        RETURN;
    END IF;
    SELECT st.idle_timeout_minutes, st.mfa_required INTO v_idle, v_mfa
        FROM shared.tenant_auth_settings st WHERE st.tenant_id = p_tenant;
    RETURN QUERY SELECT 'OK'::text, coalesce(v_idle, 15), coalesce(v_mfa, false);
END
$l1w$;
"""

_FUNCTIONS = {
    "shared.l1w_admit(text, text, text, timestamptz, boolean)": "haloflow_runtime",
    "shared.l1w_logout(text, text, text, timestamptz)": "haloflow_runtime",
    "shared.l1w_admin_bind(varchar, uuid, text, text, uuid)": "haloflow_staff_owner",
    "shared.l1w_admin_unbind(varchar, uuid, uuid)": "haloflow_staff_owner",
    "shared.l1w_admin_set_setting(varchar, text, text, uuid)": "haloflow_staff_owner",
    "shared.l1w_support_bind(varchar, uuid, text, text)": SUPPORT,
    "shared.l1w_support_unbind(varchar, uuid)": SUPPORT,
    "shared.l1w_support_set_setting(varchar, text, text)": SUPPORT,
    "shared.l1w_support_list_tenant_settings(varchar)": SUPPORT,
}
_TRIGGER_FUNCTIONS = (
    "shared.staff_sessions_guard_update()",
    "shared.staff_sessions_guard_insert()",
    "shared.staff_sessions_guard_delete()",
)
_TABLES = (
    "staff_identity_anchor", "staff_identity_bindings", "staff_sessions",
    "tenant_auth_settings", "tenant_auth_settings_log", "staff_identity_log",
    "support_operator_logins",
)

OWNERSHIP_AND_GRANTS_SQL = "\n".join(
    [f"ALTER TABLE shared.{t} OWNER TO {IDENTITY_OWNER};" for t in _TABLES]
    + [f"REVOKE ALL ON shared.{t} FROM PUBLIC;" for t in _TABLES]
    + [f"ALTER FUNCTION {f} OWNER TO {IDENTITY_OWNER};" for f in _TRIGGER_FUNCTIONS]
    + [f"REVOKE ALL ON FUNCTION {f} FROM PUBLIC;" for f in _TRIGGER_FUNCTIONS]
    + [f"ALTER FUNCTION {f} OWNER TO {IDENTITY_OWNER};" for f in _FUNCTIONS]
    + [f"REVOKE ALL ON FUNCTION {f} FROM PUBLIC;" for f in _FUNCTIONS]
    + [f"GRANT EXECUTE ON FUNCTION {f} TO {role};" for f, role in _FUNCTIONS.items()]
    + [
        f"GRANT USAGE ON SCHEMA shared TO {IDENTITY_OWNER};",
        "GRANT USAGE ON SCHEMA shared TO haloflow_staff_owner;",
        f"GRANT USAGE ON SCHEMA shared TO {SUPPORT};",
        "GRANT SELECT (grant_id, actor_id, tenant_id, ticket_reference, capabilities, state,"
        f" starts_at, expires_at) ON shared.support_access_grants TO {IDENTITY_OWNER};",
    ]
)


def upgrade() -> None:
    op.execute(PREFLIGHT_SQL)
    op.execute(ROLES_SQL)
    op.execute(TABLES_SQL)
    op.execute(TRIGGERS_SQL)
    op.execute(ADMIT_SQL)
    op.execute(WRITERS_SQL)
    op.execute(LIST_SQL)
    op.execute(OWNERSHIP_AND_GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("Revision 008 is not reversible; restore from backup instead.")
