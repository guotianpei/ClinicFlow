"""Create the M02 lock owner role and the migrator's one membership edge.

Revision ID: 004
Revises: 003
Create Date: 2026-09-27

CP2-2a, architecture v4 §3 (C-1), requirement R-A1.

`haloflow_m02_lock_owner` is a NOLOGIN execution role. In CP2-2a it receives,
per tenant, exactly column `SELECT (operation_id)` and `UPDATE (correlation_id)`
on `operation_registry`, granted by the `t002` unit after its guard passes.

**An existing role is never altered or "normalized".** If the role already
exists, this revision leaves its attributes as they are. A pre-existing unsafe
role is refused later, fail-closed and without mutation, by the `t002` guard
(G-1) before the unit's column grants. That protection does not reach back over
this revision's own membership edge or the provisioner's schema `USAGE` grant;
architecture v4 §8 L-5 records that boundary.

The edge is exactly `SET TRUE, INHERIT FALSE, ADMIN FALSE` into a declared
NOLOGIN execution role: the owner-sanctioned narrow exception to the migrator
deny token (requirements erratum 1 E2 = N1). `INHERIT FALSE` keeps the
migrator's ordinary statements free of the role's privileges; only an explicit
`SET ROLE` carries them. `ADMIN FALSE` stops the migrator granting the role on.

No password and no `search_path` setting; the role is never a session user.
`downgrade()` raises, consistent with `001`-`003`.
"""

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


LOCK_OWNER_ROLE_SQL = """
DO $m02$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'haloflow_m02_lock_owner') THEN
        CREATE ROLE haloflow_m02_lock_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$m02$;
GRANT haloflow_m02_lock_owner TO haloflow_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
"""


def upgrade() -> None:
    op.execute(LOCK_OWNER_ROLE_SQL)


def downgrade() -> None:
    raise RuntimeError("Revision 004 is not reversible; restore from backup instead.")
