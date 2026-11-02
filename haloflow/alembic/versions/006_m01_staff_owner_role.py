"""Create the MA-10 staff-directory execution role and its migrator edge.

Revision ID: 006
Revises: 005
Create Date: 2026-11-01

UC1 part B, MA-10 staff directory (package v1 + v1r1 delta + v1r2 adoption,
Claude/Codex aligned 2026-11-01; owner choices recorded 2026-11-01).

One NOLOGIN execution role, created only if absent:

- `haloflow_staff_owner` owns the three directory gateways
  (`m01_staff_assignable`, `m01_staff_admin`, `m01_staff_display`) and nothing else.

**An existing role is never altered or "normalized"** (the 004/005 rule). A
pre-existing unsafe role is refused later, fail-closed and without mutation, by
the tenant-unit guard before any per-tenant grant.

One membership edge, to the migrator, `INHERIT FALSE, SET TRUE, ADMIN FALSE`
(the 004/005 pattern). No shared-schema privilege. No password, no
`search_path`; never a session user. `downgrade()` raises, consistent with
`001`-`005`.
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

STAFF_OWNER_ROLE = "haloflow_staff_owner"

STAFF_OWNER_ROLE_SQL = f"""
DO $m01staff$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{STAFF_OWNER_ROLE}') THEN
        CREATE ROLE {STAFF_OWNER_ROLE}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$m01staff$;
GRANT {STAFF_OWNER_ROLE} TO haloflow_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
"""


def upgrade() -> None:
    op.execute(STAFF_OWNER_ROLE_SQL)


def downgrade() -> None:
    raise RuntimeError("Revision 006 is not reversible; restore from backup instead.")
