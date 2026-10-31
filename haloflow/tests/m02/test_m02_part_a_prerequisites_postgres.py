"""UC1 part A — PA-32: provisioning before revision 005's prerequisites refuses (PG17).

Codex ALIGN 20261031T165210Z / 20261031T170447Z. Measured before this change:
production provisioning SUCCEEDED on a database at Alembic revision 004, because the
four part-A roles and their migrator edges are CLUSTER-wide (revision 005 had created
them for the main test database) while the projection owner's shared grants are
DATABASE-local and nothing checked them. The t004 prerequisite guard now verifies
the installed prerequisites (not the Alembic revision marker; that distinction is
asserted below) before any part-A object exists.

Every provisioning attempt runs in a FRESH, disposable database
(`haloflow_test_pa32_<hex>`), created and dropped here; the protected test
database is never provisioned into or downgraded. Rows:
- staged 004: the roles exist cluster-wide (so the cause is the database-local
  grant, not an incidental missing role); t004 is refused, t001..t003 stay applied
  (existing per-unit transactions), no part-A object exists, t004 is recorded
  failed, the tenant is not active;
- after upgrading the SAME database to 005: a different fresh tenant provisions to
  version 11 (positive control);
- missing required grant (005 database, one column grant revoked): refused;
- missing M05 migrator edge (cluster-wide, so snapshotted and restored exactly):
  refused; the edge rows are byte-identical afterwards;
- the guard verifies installed state: a 004 database whose grants were installed
  by hand passes the guard, though its Alembic marker still says 004.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import m02_support as m02
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

pytestmark = pytest.mark.postgres

PROJECTION = "haloflow_m02_projection_owner"
M05 = "haloflow_m05_owner"
PART_A_ROLES = (
    "haloflow_m02_append_owner",
    PROJECTION,
    "haloflow_m02_read_owner",
    M05,
)
PART_A_TABLES = (
    "ref_event_levels", "ref_event_statuses", "event_types", "event_contracts",
    "patient_events", "patient_event_links", "event_handoff_outbox",
    "m02_envelope_path_allowlist", "m02_link_resolvers",
)
BEFORE = ("t001_m01_baseline", "t002_m02_operation_registry", "t003_m02_lock_operation")
AFTER = (
    "t005_m02_events", "t006_m02_outbox", "t007_m02_envelope_allowlist",
    "t008_m02_canon", "t009_m02_append", "t010_m02_enqueue", "t011_m02_envelope_read",
)
T004 = "t004_m02_reference"
GRANT_005 = (
    f"GRANT USAGE ON SCHEMA shared TO {PROJECTION}",
    "GRANT SELECT (tenant_id, schema_key, lifecycle_state, schema_version)"
    f" ON shared.tenants TO {PROJECTION}",
)


@contextmanager
def fresh_database(
    m02_ids: Any,
    database_url: Callable[[dict[str, object], str], str],
    apply_migrations: Callable[..., None],
    revision: str,
) -> Iterator[tuple[m02.Identities, Callable[[str], None]]]:
    """A disposable database at `revision`; yields re-pointed identities and an
    upgrade function. Dropped on every exit path."""

    dbname = f"haloflow_test_pa32_{secrets.token_hex(4)}"
    admin_params: dict[str, object] = dict(conninfo_to_dict(m02_ids.admin))
    created = False
    try:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
            created = True
        apply_migrations(database_url(admin_params, dbname), revision)
        ids = m02.Identities(
            admin=make_conninfo(m02_ids.admin, dbname=dbname),
            logins={k: make_conninfo(v, dbname=dbname) for k, v in m02_ids.logins.items()},
        )

        def upgrade(target: str) -> None:
            apply_migrations(database_url(admin_params, dbname), target)

        yield ids, upgrade
    finally:
        if created:
            with m02.connect_admin(m02_ids) as conn:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(dbname)
                    )
                )


def _one(ids: m02.Identities, query: str, params: Any = None) -> Any:
    with psycopg.connect(ids.admin, autocommit=True) as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0] if len(row) == 1 else tuple(row)


def _alembic_revision(ids: m02.Identities) -> str:
    revision: str = _one(ids, "SELECT version_num FROM public.alembic_version")
    return revision


def _provision(ids: m02.Identities, tenant: tuple[str, str]) -> Any:
    return m02.provision_sync(
        ids, m02.production_registry(), tenant, supported=m02.PRODUCTION_SUPPORTED
    )


def _guard(ids: m02.Identities) -> tuple[str, str] | None:
    """Run t004's prerequisite guard directly as the migrator, the identity t004
    runs as. Returns (sqlstate, message) on refusal, None when it passes."""

    from haloflow.m02.part_a_units import _PREREQUISITE_GUARD

    with psycopg.connect(ids.logins["haloflow_migrator"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_migrator")
        try:
            conn.execute(_PREREQUISITE_GUARD)
        except psycopg.Error as error:
            return error.sqlstate or "", error.diag.message_primary or ""
    return None


REFUSED = ("P0001", "PART_A_PREREQUISITE_MISSING")


def _assert_refused_at_t004(ids: m02.Identities, tenant: tuple[str, str]) -> None:
    """The measured external outcome (runner sanitization, pinned): t004 fails with
    MIGRATION_DDL_FAILED. That code alone does not name the cause, so the guard is
    also run directly in the same database and must refuse with its own message."""

    from haloflow.m01.errors import TenantMigrationFailed

    with pytest.raises(TenantMigrationFailed) as caught:
        _provision(ids, tenant)
    assert caught.value.reason_code == "MIGRATION_DDL_FAILED"
    assert _guard(ids) == REFUSED
    rows = m02.ledger(ids, tenant[0])
    for unit in BEFORE:
        assert rows[unit][0] == "applied", unit
    assert rows[T004] == ("failed", "MIGRATION_DDL_FAILED", True)
    for unit in AFTER:
        assert unit not in rows, unit
    present = _one(
        ids,
        "SELECT count(*) FROM pg_catalog.pg_class AS c"
        " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = ANY(%s)",
        (tenant[1], list(PART_A_TABLES)),
    )
    assert present == 0
    functions = _one(
        ids,
        "SELECT count(*) FROM pg_catalog.pg_proc AS p"
        " JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND p.proname LIKE 'm02\\_%%' AND p.proname <> 'm02_lock_operation'",
        (tenant[1],),
    )
    assert functions == 0
    state = _one(
        ids, "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s", (tenant[0],)
    )
    assert state != "active"


def _edges(admin: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(admin, autocommit=True) as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT g.rolname, u.rolname, gr.rolname, m.admin_option, m.inherit_option,"
                " m.set_option FROM pg_catalog.pg_auth_members AS m"
                " JOIN pg_catalog.pg_roles AS g ON g.oid = m.roleid"
                " JOIN pg_catalog.pg_roles AS u ON u.oid = m.member"
                " JOIN pg_catalog.pg_roles AS gr ON gr.oid = m.grantor"
                " WHERE g.rolname = ANY(%s) AND u.rolname = 'haloflow_migrator'"
                " ORDER BY 1, 2, 3",
                (list(PART_A_ROLES),),
            ).fetchall()
        ]


def test_pa32_staged_004_refused_then_005_provisions(
    m02_ids: Any, database_url: Any, apply_migrations: Any
) -> None:
    with fresh_database(m02_ids, database_url, apply_migrations, "004") as (ids, upgrade):
        assert _alembic_revision(ids) == "004"
        # The cause under test is database-local: the roles and edges exist
        # cluster-wide, and only this database lacks the shared grants.
        assert len(_edges(ids.admin)) >= 4
        assert _one(ids, "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
                    (list(PART_A_ROLES),)) == 4
        assert _one(ids, "SELECT has_schema_privilege(%s, 'shared', 'USAGE')",
                    (PROJECTION,)) is False

        _assert_refused_at_t004(ids, m02.tenant_for("pa32-at-004"))

        upgrade("005")
        assert _alembic_revision(ids) == "005"
        assert _guard(ids) is None  # positive control: the same guard now passes
        outcome = _provision(ids, m02.tenant_for("pa32-after-005"))
        assert outcome.schema_version == 11


def test_pa32_a_missing_required_column_grant_is_refused(
    m02_ids: Any, database_url: Any, apply_migrations: Any
) -> None:
    with fresh_database(m02_ids, database_url, apply_migrations, "005") as (ids, _):
        with psycopg.connect(ids.admin, autocommit=True) as conn:
            conn.execute(f"REVOKE SELECT (schema_version) ON shared.tenants FROM {PROJECTION}")
        _assert_refused_at_t004(ids, m02.tenant_for("pa32-missing-grant"))


def test_pa32_a_table_wide_grant_is_refused_as_not_exact(
    m02_ids: Any, database_url: Any, apply_migrations: Any
) -> None:
    """Extra access is refused too: table-wide SELECT adds the other columns."""

    with fresh_database(m02_ids, database_url, apply_migrations, "005") as (ids, _):
        with psycopg.connect(ids.admin, autocommit=True) as conn:
            conn.execute(f"GRANT SELECT ON shared.tenants TO {PROJECTION}")
        _assert_refused_at_t004(ids, m02.tenant_for("pa32-table-wide"))


def test_pa32_a_missing_m05_migrator_edge_is_refused_and_restored_exactly(
    m02_ids: Any, database_url: Any, apply_migrations: Any
) -> None:
    """The edge is cluster-wide: snapshot, revoke, attempt in a fresh database,
    then restore exactly as revision 005 grants it and compare the snapshot."""

    before = _edges(m02_ids.admin)
    assert any(row[0] == M05 for row in before)
    try:
        with fresh_database(m02_ids, database_url, apply_migrations, "005") as (ids, _):
            with m02.connect_admin(m02_ids) as conn:
                conn.execute(f"REVOKE {M05} FROM haloflow_migrator")
            assert not any(row[0] == M05 for row in _edges(ids.admin))
            assert _guard(ids) == REFUSED  # the guard itself refuses the missing edge
            # Through provisioning, M01's earlier preflight already refuses it (the
            # manifest declares the edge), before any unit: nothing is applied.
            from haloflow.m01.errors import ProvisioningFailed

            tenant = m02.tenant_for("pa32-missing-m05-edge")
            with pytest.raises(ProvisioningFailed) as caught:
                _provision(ids, tenant)
            assert caught.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE"
            assert m02.ledger(ids, tenant[0]) == {}
    finally:
        if not any(row[0] == M05 for row in _edges(m02_ids.admin)):
            with m02.connect_admin(m02_ids) as conn:
                conn.execute(
                    f"GRANT {M05} TO haloflow_migrator"
                    " WITH INHERIT FALSE, SET TRUE, ADMIN FALSE"
                )
    assert _edges(m02_ids.admin) == before


def test_pa32_the_guard_verifies_installed_state_not_the_revision_marker(
    m02_ids: Any, database_url: Any, apply_migrations: Any
) -> None:
    """Distinction Codex asked to show: with the 005 grants installed by hand into a
    004 database, provisioning passes the guard though the marker still says 004."""

    with fresh_database(m02_ids, database_url, apply_migrations, "004") as (ids, _):
        with psycopg.connect(ids.admin, autocommit=True) as conn:
            for statement in GRANT_005:
                conn.execute(statement)
        assert _alembic_revision(ids) == "004"
        assert _guard(ids) is None
        outcome = _provision(ids, m02.tenant_for("pa32-hand-installed"))
        assert outcome.schema_version == 11
