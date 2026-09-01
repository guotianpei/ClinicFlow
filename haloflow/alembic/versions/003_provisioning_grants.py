"""Install the provisioning role grants permissions.json already specified.

Revision ID: 003
Revises: 002
Create Date: 2026-08-31

Three findings land here.

**F-1.** Migration `001` creates `haloflow_provisioner` and `haloflow_migrator`
and grants them nothing at all, while `permissions.json` has specified the split
between them since `001` merged. Neither role could do its job, and nothing
caught it because both manifest tests assert about the JSON rather than about the
database (F-3). This revision implements the manifest.

Neither role may assume the other. `001` issues no `IN ROLE`, no
`GRANT <role> TO <role>` and no `SET ROLE`, so there is no membership to unwind;
this revision must simply not introduce one, and TC-E19 asserts that `SET ROLE`
between them still fails in both directions.

**D12.** `haloflow_provisioner` gets `INSERT` on `shared.tenant_state_history` and
nothing else there. The table is append-only by trigger and the provisioner owns
lifecycle transitions, so INSERT is the whole requirement. No sequence privilege
is granted: `event_id` is `GENERATED ALWAYS AS IDENTITY`, and an identity column
inserts without one -- re-confirmed on PostgreSQL 17.10, 2026-08-31.

**F-4.** `001` grants `USAGE, SELECT ON SEQUENCE
shared.access_audit_log_audit_id_seq` to both audit-writing roles. Neither needs
it, for the same identity-column reason, and the `SELECT` half lets a role that
is denied `SELECT` on `shared.access_audit_log` read `last_value` -- the global
audit row count across every tenant. Reproduced on 17.10 and revoked here.

**D13** (2026-08-31): tenant schemas are owned by `haloflow_provisioner`. The
provisioner therefore needs `CREATE` on the database and no membership anywhere.
The alternative -- `CREATE SCHEMA ... AUTHORIZATION haloflow_owner` -- requires
`GRANT haloflow_owner TO haloflow_provisioner`, which PostgreSQL demands for that
statement and which would give the provisioner INSERT, DELETE and DROP over
`shared.access_audit_log`.

`downgrade()` raises, consistent with `001` and `002`.
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


# The migrator owns per-tenant DDL and every write to the migration ledger. It
# receives nothing on shared.tenants: referential integrity checks on
# schema_migrations.tenant_id run with the referencing table's owner privileges,
# so the FK does not require the inserting role to read the parent.
MIGRATOR_GRANTS_SQL = """
GRANT USAGE ON SCHEMA shared TO haloflow_migrator;
GRANT SELECT, INSERT, UPDATE ON shared.schema_migrations TO haloflow_migrator;
"""


# The provisioner owns allocation, schema creation, grants, verification and
# activation. It reads the ledger to decide whether a run resumes, and cannot
# write it: "who may alter a tenant's migration history" stays answerable by role.
PROVISIONER_GRANTS_SQL = """
GRANT USAGE ON SCHEMA shared TO haloflow_provisioner;
GRANT SELECT, INSERT, UPDATE ON shared.tenants TO haloflow_provisioner;
GRANT SELECT ON shared.schema_migrations TO haloflow_provisioner;
GRANT INSERT ON shared.tenant_state_history TO haloflow_provisioner;

DO $m01_003$
BEGIN
    EXECUTE format(
        'GRANT CREATE ON DATABASE %I TO haloflow_provisioner',
        current_database()
    );
END
$m01_003$;
"""


# F-4. Both roles insert into an identity-column table and need no sequence
# privilege; the SELECT half is a cross-tenant volume leak.
AUDIT_SEQUENCE_REVOKE_SQL = """
REVOKE USAGE, SELECT ON SEQUENCE shared.access_audit_log_audit_id_seq
    FROM haloflow_audit_projector;
REVOKE USAGE, SELECT ON SEQUENCE shared.access_audit_log_audit_id_seq
    FROM haloflow_control_audit_writer;
"""


COMMENT_SQL = """
COMMENT ON SCHEMA shared IS
    'M01 control plane. Tenant schemas are owned by haloflow_provisioner (D13); '
    'haloflow_owner owns this schema only.';
"""


def upgrade() -> None:
    op.execute(MIGRATOR_GRANTS_SQL)
    op.execute(PROVISIONER_GRANTS_SQL)
    op.execute(AUDIT_SEQUENCE_REVOKE_SQL)
    op.execute(COMMENT_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "M01 downgrade is intentionally unsupported because the shared schema "
        "contains audit and tenant-lifecycle evidence. Use an operator-approved "
        "export, retention, and decommission procedure instead."
    )
