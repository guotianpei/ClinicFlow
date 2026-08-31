"""Rename the contextual operation_id to execution_id and retype it to uuid.

Revision ID: 002
Revises: 001
Create Date: 2026-08-31

ADR-011 D-11.18 (approved decision B5). M01's contextual `operation_id` becomes
`execution_id`. Renaming only the Python field would leave
`shared.access_audit_log.operation_id` holding an execution identifier while
M02's `patient_events.operation_id` holds a business-operation identifier: one
column name, two meanings, in one database.

Migration `001` is not edited, and this revision's `downgrade()` raises for the
same reason `001`'s does.

The columns are retyped from varchar(128) to uuid. A preflight guard counts
values that would not cast and aborts before any schema change, so an
unanticipated value produces one named, PHI-safe error rather than a cast
failure partway through a deploy. Verified 2026-08-31 that all three columns are
empty in every environment where `001` has been applied; the guard is for the
environments that do not exist yet.

A note the guard's message carries, found by exercising it against a seeded bad
value: `tenant_state_history` and `access_audit_log` are append-only, enforced by
`001`'s `reject_append_only_change` trigger. A bad value in either cannot be
fixed with an UPDATE, so "remediate and re-run" is not advice an operator could
actually follow there. Only `isolation_alerts` is directly correctable.
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


# Castability must be tested per value with exception handling, because
# PostgreSQL accepts several valid uuid spellings (uppercase, braced,
# unhyphenated) that a canonical-format regex would wrongly reject. This is done
# inline rather than through a helper function so the migration creates nothing:
# an unqualified CREATE FUNCTION would land in whichever schema the migrating
# role's search_path happens to name.
#
# The per-row loop is O(rows). These are control-plane tables, and all three are
# empty in every environment where 001 has been applied.
PREFLIGHT_SQL = """
DO $m01_002$
DECLARE
    target    record;
    candidate text;
    offenders bigint := 0;
BEGIN
    FOR target IN
        SELECT * FROM (VALUES
            ('shared', 'tenant_state_history'),
            ('shared', 'access_audit_log'),
            ('shared', 'isolation_alerts')
        ) AS t(schema_name, table_name)
    LOOP
        FOR candidate IN EXECUTE format(
            'SELECT operation_id FROM %I.%I WHERE operation_id IS NOT NULL',
            target.schema_name, target.table_name
        )
        LOOP
            BEGIN
                PERFORM candidate::uuid;
            EXCEPTION WHEN others THEN
                offenders := offenders + 1;
            END;
        END LOOP;
    END LOOP;

    IF offenders > 0 THEN
        RAISE EXCEPTION
            'M01 migration 002 preflight failed: % value(s) in shared.*.operation_id '
            'cannot be cast to uuid. No schema change has been made. Values are '
            'deliberately not reported. Note that tenant_state_history and '
            'access_audit_log are append-only: their rows cannot be corrected by '
            'UPDATE or DELETE, so remediation there requires the same '
            'operator-approved procedure as an M01 downgrade. isolation_alerts '
            'carries no append-only trigger and can be corrected directly.',
            offenders
        USING ERRCODE = 'data_exception';
    END IF;
END
$m01_002$;
"""


RENAME_SQL = """
ALTER TABLE shared.tenant_state_history RENAME COLUMN operation_id TO execution_id;
ALTER TABLE shared.access_audit_log     RENAME COLUMN operation_id TO execution_id;
ALTER TABLE shared.isolation_alerts     RENAME COLUMN operation_id TO execution_id;

ALTER TABLE shared.tenant_state_history
    ALTER COLUMN execution_id TYPE uuid USING execution_id::uuid;
ALTER TABLE shared.access_audit_log
    ALTER COLUMN execution_id TYPE uuid USING execution_id::uuid;
ALTER TABLE shared.isolation_alerts
    ALTER COLUMN execution_id TYPE uuid USING execution_id::uuid;

COMMENT ON COLUMN shared.tenant_state_history.execution_id IS
    'ADR-011 D-11.18 caller-labelled execution scope; not M02 operation_id';
COMMENT ON COLUMN shared.access_audit_log.execution_id IS
    'ADR-011 D-11.18 caller-labelled execution scope; not M02 operation_id';
COMMENT ON COLUMN shared.isolation_alerts.execution_id IS
    'ADR-011 D-11.18 caller-labelled execution scope; not M02 operation_id';
"""


def upgrade() -> None:
    op.execute(PREFLIGHT_SQL)
    op.execute(RENAME_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "M01 downgrade is intentionally unsupported because the shared schema "
        "contains audit and tenant-lifecycle evidence. Use an operator-approved "
        "export, retention, and decommission procedure instead."
    )
