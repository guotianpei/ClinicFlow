"""Create the UC1 part-A roles, their migrator edges and the shared registry grants.

Revision ID: 005
Revises: 004
Create Date: 2026-10-30

UC1 part A (M02 minimal observation append), test-interface package v1 with
delta v1r1 Δ4 and decision TB-3 (Claude/Codex aligned 2026-10-30).

Four NOLOGIN execution roles, created only if absent:

- `haloflow_m02_append_owner`     owns `m02_append_observation` and `m02_canon_v1`;
- `haloflow_m02_projection_owner` owns `m02_enqueue_event_handoffs`;
- `haloflow_m02_read_owner`       owns `m02_get_observation_envelope`;
- `haloflow_m05_owner`            holds EXECUTE on the envelope read only; owns
                                  nothing in part A.

**An existing role is never altered or "normalized"** (the 004 rule). A
pre-existing unsafe role is refused later, fail-closed and without mutation,
by the tenant-unit guards before any per-tenant grant.

Each role gets exactly one membership edge, to the migrator,
`INHERIT FALSE, SET TRUE, ADMIN FALSE` (the 004 pattern): only an explicit
`SET ROLE` carries the role's privileges and the migrator cannot grant it on.

The projection owner gets `USAGE` on schema `shared` and column `SELECT` on
exactly `tenant_id, schema_key, lifecycle_state, schema_version` of
`shared.tenants`. These shared grants are issued HERE, by the owner-run Alembic
identity that owns `shared.tenants`, never by a tenant unit run as the
migrator (PA-41). No other role receives any shared privilege.

No password and no `search_path` setting; no role is ever a session user.
`downgrade()` raises, consistent with `001`-`004`.
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


PART_A_ROLES = (
    "haloflow_m02_append_owner",
    "haloflow_m02_projection_owner",
    "haloflow_m02_read_owner",
    "haloflow_m05_owner",
)


def _role_sql(role: str) -> str:
    return f"""
DO $m02$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE {role}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$m02$;
GRANT {role} TO haloflow_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
"""


PART_A_ROLES_SQL = "".join(_role_sql(role) for role in PART_A_ROLES)

SHARED_REGISTRY_GRANTS_SQL = """
GRANT USAGE ON SCHEMA shared TO haloflow_m02_projection_owner;
GRANT SELECT (tenant_id, schema_key, lifecycle_state, schema_version) ON shared.tenants TO haloflow_m02_projection_owner;
"""  # noqa: E501 - the exact one-line grant text is asserted by PA-41 (supplementary)


def upgrade() -> None:
    op.execute(PART_A_ROLES_SQL)
    op.execute(SHARED_REGISTRY_GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("Revision 005 is not reversible; restore from backup instead.")
