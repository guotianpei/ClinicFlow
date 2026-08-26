"""M01 shared control-plane foundation.

Revision ID: 001
Revises:
Create Date: 2026-08-26

The previous prototype migration was never applied to a database containing
data, so revision 001 is intentionally replaced rather than converted.
"""

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


ROLE_SQL = """
DO $m01$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_owner') THEN
        CREATE ROLE haloflow_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_runtime') THEN
        CREATE ROLE haloflow_runtime NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_migrator') THEN
        CREATE ROLE haloflow_migrator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_provisioner') THEN
        CREATE ROLE haloflow_provisioner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_audit_projector') THEN
        CREATE ROLE haloflow_audit_projector NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_control_audit_writer') THEN
        CREATE ROLE haloflow_control_audit_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_support_ro') THEN
        CREATE ROLE haloflow_support_ro NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_breakglass_ro') THEN
        CREATE ROLE haloflow_breakglass_ro NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haloflow_breakglass_rw') THEN
        CREATE ROLE haloflow_breakglass_rw NOLOGIN;
    END IF;
END
$m01$;

ALTER ROLE haloflow_runtime SET search_path = '';
ALTER ROLE haloflow_audit_projector SET search_path = '';
ALTER ROLE haloflow_support_ro SET search_path = '';
ALTER ROLE haloflow_breakglass_ro SET search_path = '';
ALTER ROLE haloflow_breakglass_rw SET search_path = '';

DO $m01$
BEGIN
    EXECUTE format(
        'REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC',
        current_database()
    );
END
$m01$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
"""


CONTROL_SCHEMA_SQL = """
CREATE SCHEMA shared AUTHORIZATION haloflow_owner;
REVOKE ALL ON SCHEMA shared FROM PUBLIC;

CREATE TABLE shared.tenants (
    tenant_id varchar(64) PRIMARY KEY,
    schema_key varchar(64) NOT NULL UNIQUE,
    lifecycle_state varchar(32) NOT NULL,
    schema_version integer NOT NULL,
    display_reference varchar(128),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT tenants_tenant_id_format
        CHECK (tenant_id ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'),
    CONSTRAINT tenants_schema_key_format
        CHECK (schema_key ~ '^tenant_[a-z0-9]{8,32}$'),
    CONSTRAINT tenants_lifecycle_state
        CHECK (lifecycle_state IN (
            'provisioning', 'active', 'suspended', 'archival_pending',
            'archived', 'decommissioned'
        )),
    CONSTRAINT tenants_schema_version_positive CHECK (schema_version > 0)
);

CREATE TABLE shared.tenant_state_history (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    prior_state varchar(32),
    new_state varchar(32) NOT NULL,
    reason_code varchar(64) NOT NULL,
    actor_kind varchar(16) NOT NULL,
    actor_id varchar(128) NOT NULL,
    operation_id varchar(128) NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT tenant_state_history_state
        CHECK (new_state IN (
            'provisioning', 'active', 'suspended', 'archival_pending',
            'archived', 'decommissioned'
        )),
    CONSTRAINT tenant_state_history_actor_kind
        CHECK (actor_kind IN ('actor', 'workload'))
);

CREATE INDEX tenant_state_history_tenant_time_idx
    ON shared.tenant_state_history (tenant_id, occurred_at DESC);

CREATE TABLE shared.schema_migrations (
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    migration_id varchar(128) NOT NULL,
    checksum varchar(128) NOT NULL,
    state varchar(24) NOT NULL,
    attempt integer NOT NULL DEFAULT 1,
    started_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    completed_at timestamptz,
    sanitized_error_code varchar(64),
    PRIMARY KEY (tenant_id, migration_id),
    CONSTRAINT schema_migrations_state
        CHECK (state IN ('pending', 'running', 'applied', 'failed')),
    CONSTRAINT schema_migrations_attempt_positive CHECK (attempt > 0)
);

CREATE INDEX schema_migrations_drift_idx
    ON shared.schema_migrations (state, migration_id, tenant_id);

CREATE TABLE shared.support_access_grants (
    grant_id uuid PRIMARY KEY,
    actor_id varchar(128) NOT NULL,
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    ticket_reference varchar(128) NOT NULL,
    purpose_code varchar(64) NOT NULL,
    capabilities text[] NOT NULL,
    approver_id varchar(128) NOT NULL,
    starts_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    state varchar(24) NOT NULL,
    break_glass boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT support_access_grants_window CHECK (expires_at > starts_at),
    CONSTRAINT support_access_grants_state
        CHECK (state IN ('requested', 'approved', 'active', 'revoked', 'expired'))
);

CREATE INDEX support_access_grants_lookup_idx
    ON shared.support_access_grants (actor_id, tenant_id, state, expires_at);

CREATE TABLE shared.access_audit_log (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_event_id uuid NOT NULL UNIQUE,
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    principal_kind varchar(16) NOT NULL,
    principal_id varchar(128) NOT NULL,
    action_code varchar(64) NOT NULL,
    resource_class varchar(64) NOT NULL,
    purpose_code varchar(64) NOT NULL,
    outcome_code varchar(64) NOT NULL,
    request_id varchar(128),
    operation_id varchar(128) NOT NULL,
    occurred_at timestamptz NOT NULL,
    projected_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT access_audit_log_principal_kind
        CHECK (principal_kind IN ('actor', 'workload'))
);

CREATE INDEX access_audit_log_investigation_idx
    ON shared.access_audit_log (tenant_id, principal_id, occurred_at DESC);

CREATE TABLE shared.isolation_alerts (
    alert_id uuid PRIMARY KEY,
    tenant_id varchar(64) REFERENCES shared.tenants(tenant_id),
    source_code varchar(64) NOT NULL,
    alert_type varchar(64) NOT NULL,
    severity smallint NOT NULL,
    operation_id varchar(128),
    state varchar(24) NOT NULL DEFAULT 'open',
    detected_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    closed_at timestamptz,
    CONSTRAINT isolation_alerts_severity CHECK (severity BETWEEN 1 AND 4),
    CONSTRAINT isolation_alerts_state
        CHECK (state IN ('open', 'investigating', 'contained', 'closed'))
);

CREATE INDEX isolation_alerts_workflow_idx
    ON shared.isolation_alerts (state, severity, detected_at);
"""


IMMUTABILITY_SQL = """
CREATE FUNCTION shared.reject_tenant_identity_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $m01$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION 'tenant identity is immutable';
    END IF;
    IF OLD.lifecycle_state <> 'provisioning'
       AND NEW.schema_key IS DISTINCT FROM OLD.schema_key THEN
        RAISE EXCEPTION 'active tenant schema identity is immutable';
    END IF;
    RETURN NEW;
END
$m01$;

CREATE TRIGGER tenants_identity_immutable
BEFORE UPDATE ON shared.tenants
FOR EACH ROW EXECUTE FUNCTION shared.reject_tenant_identity_change();

CREATE FUNCTION shared.reject_append_only_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $m01$
BEGIN
    RAISE EXCEPTION 'append-only record cannot be changed';
END
$m01$;

CREATE TRIGGER tenant_state_history_append_only
BEFORE UPDATE OR DELETE ON shared.tenant_state_history
FOR EACH ROW EXECUTE FUNCTION shared.reject_append_only_change();

CREATE TRIGGER access_audit_log_append_only
BEFORE UPDATE OR DELETE ON shared.access_audit_log
FOR EACH ROW EXECUTE FUNCTION shared.reject_append_only_change();
"""


OWNERSHIP_AND_GRANTS_SQL = """
ALTER FUNCTION shared.reject_tenant_identity_change() OWNER TO haloflow_owner;
ALTER FUNCTION shared.reject_append_only_change() OWNER TO haloflow_owner;
ALTER TABLE shared.tenants OWNER TO haloflow_owner;
ALTER TABLE shared.tenant_state_history OWNER TO haloflow_owner;
ALTER TABLE shared.schema_migrations OWNER TO haloflow_owner;
ALTER TABLE shared.support_access_grants OWNER TO haloflow_owner;
ALTER TABLE shared.access_audit_log OWNER TO haloflow_owner;
ALTER TABLE shared.isolation_alerts OWNER TO haloflow_owner;

GRANT USAGE ON SCHEMA shared TO haloflow_runtime;
GRANT SELECT (tenant_id, schema_key, lifecycle_state, schema_version)
    ON shared.tenants TO haloflow_runtime;

GRANT USAGE ON SCHEMA shared TO haloflow_audit_projector;
GRANT INSERT ON shared.access_audit_log TO haloflow_audit_projector;
GRANT USAGE, SELECT ON SEQUENCE shared.access_audit_log_audit_id_seq
    TO haloflow_audit_projector;

GRANT USAGE ON SCHEMA shared TO haloflow_control_audit_writer;
GRANT INSERT ON shared.access_audit_log TO haloflow_control_audit_writer;
GRANT USAGE, SELECT ON SEQUENCE shared.access_audit_log_audit_id_seq
    TO haloflow_control_audit_writer;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON shared.access_audit_log FROM haloflow_runtime;
"""


def upgrade() -> None:
    op.execute(ROLE_SQL)
    op.execute(CONTROL_SCHEMA_SQL)
    op.execute(IMMUTABILITY_SQL)
    op.execute(OWNERSHIP_AND_GRANTS_SQL)


def downgrade() -> None:
    # Roles are cluster-scoped and deliberately retained. Removing them safely
    # requires an operator-reviewed dependency check outside a schema migration.
    op.execute("DROP SCHEMA IF EXISTS shared CASCADE")
