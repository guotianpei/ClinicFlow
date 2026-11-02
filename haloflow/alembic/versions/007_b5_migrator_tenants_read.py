"""Grant the migrator a four-column read of shared.tenants for the B5 startup check.

Revision ID: 007
Revises: 006
Create Date: 2026-11-01

UC1 part B, tranche B5, P-3 option (a) (Codex startup-grant ruling 32e72016;
package v2 + v2r1 aligned; red gate PASS at 0673dab).

The production startup factory (`haloflow.startup`) validates every served tenant
over one dedicated `haloflow_migrator` connection, in one
`REPEATABLE READ, READ ONLY` transaction: the `shared.tenants` row, the loaded
generation and the installed-state comparison share a single snapshot. Revision
`003` gives the migrator `USAGE` on schema `shared` and its `schema_migrations`
privileges, and explicitly no `shared.tenants` access, so that snapshot could not
read the tenant row.

This revision is additive and grants exactly column `SELECT` on
`tenant_id, schema_key, lifecycle_state, schema_version` of `shared.tenants` to
`haloflow_migrator`: the same four columns `005` grants the projection owner. No
table-level privilege, no other column, no write, no membership or `SET` edge.
Revision `003` is not rewritten. The grant is issued here by the owner-run Alembic
identity that owns `shared.tenants`. READ ONLY in the startup check is a
transaction scope, not a reduced role privilege.

`downgrade()` raises, consistent with `001`-`006`.
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

MIGRATOR_TENANTS_READ_SQL = """
GRANT SELECT (tenant_id, schema_key, lifecycle_state, schema_version) ON shared.tenants TO haloflow_migrator;
"""  # noqa: E501 - one exact grant, kept on one line like 005's registry grant


def upgrade() -> None:
    op.execute(MIGRATOR_TENANTS_READ_SQL)


def downgrade() -> None:
    raise RuntimeError("Revision 007 is not reversible; restore from backup instead.")
