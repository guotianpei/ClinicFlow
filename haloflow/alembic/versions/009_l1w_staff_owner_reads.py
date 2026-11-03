"""L-1W W3a: the staff owner's column reads on three shared tables.

Revision ID: 009
Revises: 008
Create Date: 2026-11-02

W3a contract v1 section 2 (Codex 215519Z; red gate ACCEPT at 6884cb6). The W3a
tenant gateways run as ``haloflow_staff_owner``:

- ``m01_staff_admin_v2`` derives the tenant of identity and settings writes from
  ``shared.tenants`` by this schema's key, never from the caller;
- ``m01_staff_bootstrap_admin`` resolves the support login by OID AND name from
  ``shared.support_operator_logins`` and requires an active capable grant in
  ``shared.support_access_grants`` for this tenant, re-checked after its locks.

Exactly these column SELECT privileges, nothing table-level, nothing else
(PRIV-W3-01). ``downgrade()`` raises, consistent with ``001``-``008``.
"""

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

STAFF_OWNER = "haloflow_staff_owner"

GRANTS_SQL = f"""
GRANT SELECT (tenant_id, schema_key) ON shared.tenants TO {STAFF_OWNER};
GRANT SELECT (login_oid, login_name, actor_id) ON shared.support_operator_logins
    TO {STAFF_OWNER};
GRANT SELECT (grant_id, actor_id, tenant_id, ticket_reference, capabilities, state,
    starts_at, expires_at) ON shared.support_access_grants TO {STAFF_OWNER};
"""


def upgrade() -> None:
    op.execute(GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("Revision 009 is not reversible; restore from backup instead.")
