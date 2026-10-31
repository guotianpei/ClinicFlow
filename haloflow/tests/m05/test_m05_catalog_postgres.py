"""UC1 part B, tranche B1 — catalogue rows on PostgreSQL 17.

Package v1 §3.6 ME-01..05 and delta v1r1 MX-02 (catalogue half), plus the B1
install, version and seed rows. Every row calls `m05_install.require()` first, so
until M05 exists each row FAILS in its own body (class-B red).

- ME-01  the seven M05 functions: owner, SECURITY DEFINER, pinned search_path,
         return type, exact ACL (owner + runtime, no PUBLIC); no other m05_* function;
- ME-02  M05 tables owned by the migrator with the package §2.1 columns; the runtime
         holds no table or column privilege; no other m05_* table;
- ME-03  the M05 owner executes exactly one m02_* function, the envelope read;
- ME-04  provisioning before the role revision refuses (EC-3): a fresh database at
         Alembic 004 refuses at t004 with no M05 object; a missing M05 migrator edge
         refuses before any unit (cluster-wide edge snapshotted and restored);
- ME-05  history UPDATE/DELETE refused for the M05 owner and the runtime (no grant),
         and a BEFORE UPDATE OR DELETE rejector trigger exists. The row-level
         rejector behaviour for the table owner is asserted in B2, when an item can
         be created through m05_create_item;
- MX-02  no M05 column names EHR/appointment state; history has `assertion_kind`
         with a CHECK naming 'staff';
- MB1-P01 the tenant reaches test target 20 with every unit applied;
- MB1-P02 the W1 outcome codes, exactly; intake map rows inactive in production;
         the M05 envelope allowlist rows for the resolution contract.
"""

from __future__ import annotations

from typing import Any

import m02_support as m02
import m05_support as m05
import psycopg
import pytest
from m05_harness import M05Install, m05_manifest, m05_registry

pytestmark = pytest.mark.postgres

TABLE_PRIVILEGES = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN",
)
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
INFRASTRUCTURE = ("haloflow_migrator", "haloflow_provisioner", "haloflow_audit_projector")
M02_ROLES = (
    "haloflow_m02_lock_owner",
    "haloflow_m02_append_owner",
    "haloflow_m02_projection_owner",
    "haloflow_m02_read_owner",
)


def _all(install: M05Install, query: str, params: Any = None) -> list[tuple[Any, ...]]:
    return m02.admin_all(install.ids, query, params)


def _signature(schema: str, spec: m05.FunctionSpec) -> str:
    return f"{schema}.{spec.name}({spec.args})"


# --- ME-01 -----------------------------------------------------------------


@pytest.mark.parametrize("spec", m05.FUNCTIONS, ids=[spec.name for spec in m05.FUNCTIONS])
def test_me01_function_owner_definer_search_path_acl(
    m05_install: M05Install, spec: m05.FunctionSpec
) -> None:
    m05_install.require()
    schema = m05_install.schema
    rows = _all(
        m05_install,
        """
        SELECT pg_catalog.pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig,
               pg_catalog.format_type(p.prorettype, NULL), p.proretset
          FROM pg_catalog.pg_proc p
         WHERE p.oid = pg_catalog.to_regprocedure(%s)
        """,
        (_signature(schema, spec),),
    )
    assert rows == [
        (
            m05.M05_OWNER,
            True,
            [m05.SEARCH_PATH_CONFIG.format(schema=schema)],
            spec.returns,
            False,
        )
    ], spec.name
    acl = _all(
        m05_install,
        """
        SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE pg_catalog.pg_get_userbyid(a.grantee) END,
               a.privilege_type, pg_catalog.pg_get_userbyid(a.grantor), a.is_grantable
          FROM pg_catalog.pg_proc p,
               LATERAL pg_catalog.aclexplode(p.proacl) a
         WHERE p.oid = pg_catalog.to_regprocedure(%s)
         ORDER BY 1, 2
        """,
        (_signature(schema, spec),),
    )
    assert sorted(acl) == sorted(m05.FUNCTION_ACL), spec.name
    for role in (*INFRASTRUCTURE, *M02_ROLES):
        assert m02.admin_one(
            m05_install.ids,
            "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
            (role, _signature(schema, spec)),
        ) == (False,), (spec.name, role)


def test_me01_no_other_m05_function(m05_install: M05Install) -> None:
    m05_install.require()
    rows = _all(
        m05_install,
        """
        SELECT p.oid::pg_catalog.regprocedure::text
          FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = %s AND p.proname LIKE 'm05\\_%%'
        """,
        (m05_install.schema,),
    )
    # regprocedure text is schema-qualified only when not on search_path; compare names.
    names = sorted(text.rsplit(".", 1)[-1] for (text,) in rows)
    assert names == sorted(f"{spec.name}({spec.args})" for spec in m05.FUNCTIONS)


# --- ME-02 -----------------------------------------------------------------


@pytest.mark.parametrize("table", m05.TABLES)
def test_me02_table_owner_and_no_runtime_privilege(m05_install: M05Install, table: str) -> None:
    m05_install.require()
    qualified = f"{m05_install.schema}.{table}"
    assert m02.admin_one(
        m05_install.ids,
        "SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c"
        " WHERE c.oid = pg_catalog.to_regclass(%s)",
        (qualified,),
    ) == (m05.MIGRATOR,), table
    for privilege in TABLE_PRIVILEGES:
        assert m02.admin_one(
            m05_install.ids,
            "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
            (m05.RUNTIME, qualified, privilege),
        ) == (False,), (table, privilege)
    for privilege in COLUMN_PRIVILEGES:
        assert m02.admin_one(
            m05_install.ids,
            "SELECT pg_catalog.has_any_column_privilege(%s, %s, %s)",
            (m05.RUNTIME, qualified, privilege),
        ) == (False,), (table, privilege)


@pytest.mark.parametrize("table", sorted(m05.COLUMNS))
def test_me02_columns_exact(m05_install: M05Install, table: str) -> None:
    m05_install.require()
    rows = _all(
        m05_install,
        """
        SELECT a.attname FROM pg_catalog.pg_attribute a
         WHERE a.attrelid = pg_catalog.to_regclass(%s) AND a.attnum > 0
           AND NOT a.attisdropped
        """,
        (f"{m05_install.schema}.{table}",),
    )
    assert sorted(name for (name,) in rows) == sorted(m05.COLUMNS[table]), table


def test_me02_no_other_m05_table(m05_install: M05Install) -> None:
    m05_install.require()
    rows = _all(
        m05_install,
        """
        SELECT c.relname FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname LIKE 'm05\\_%%' AND c.relkind IN ('r', 'p', 'v', 'm')
        """,
        (m05_install.schema,),
    )
    assert sorted(name for (name,) in rows) == sorted(m05.TABLES)


# --- ME-03 -----------------------------------------------------------------


def test_me03_m05_owner_executes_exactly_the_envelope(m05_install: M05Install) -> None:
    m05_install.require()
    executable = []
    for signature in m05.M02_FUNCTIONS:
        (allowed,) = m02.admin_one(
            m05_install.ids,
            "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
            (m05.M05_OWNER, f"{m05_install.schema}.{signature}"),
        )
        if allowed:
            executable.append(signature)
    assert tuple(executable) == m05.M05_EXECUTABLE_M02


# --- ME-04 -----------------------------------------------------------------


def _m05_provision(ids: m02.Identities, tenant: tuple[str, str]) -> Any:
    return m02.provision_sync(
        ids,
        m05_registry(),
        tenant,
        supported=m05.M05_TEST_SUPPORTED,
        manifest=m05_manifest(),
    )


def _m05_objects(ids: m02.Identities, schema: str) -> list[tuple[Any, ...]]:
    return m02.admin_all(
        ids,
        """
        SELECT c.relname FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname LIKE 'm05\\_%%'
        UNION ALL
        SELECT p.proname FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = %s AND p.proname LIKE 'm05\\_%%'
        """,
        (schema, schema),
    )


def test_me04_provisioning_before_role_revision_refuses(
    m02_ids: m02.Identities, database_url: Any, apply_migrations: Any
) -> None:
    """A fresh database at Alembic 004: the M05 registry is refused at t004 (the
    part A prerequisite guard), so no t012+ unit runs and no M05 object exists."""

    from test_m02_part_a_prerequisites_postgres import fresh_database

    from haloflow.m01.errors import ProvisioningFailed

    registry_error: Exception | None = None
    try:
        m05_registry()
    except Exception as error:  # noqa: BLE001 - the red cause is reported below
        registry_error = error
    assert registry_error is None, f"M05 registry unavailable: {type(registry_error).__name__}"

    with fresh_database(m02_ids, database_url, apply_migrations, "004") as (ids, _):
        tenant = m02.tenant_for("me04-before-005")
        with pytest.raises(ProvisioningFailed) as caught:
            _m05_provision(ids, tenant)
        assert caught.value.reason_code == "MIGRATION_DDL_FAILED"
        ledger = m02.ledger(ids, tenant[0])
        assert not any(migration_id in ledger for migration_id in m05.M05_UNIT_IDS)
        assert _m05_objects(ids, tenant[1]) == []


def test_me04_missing_m05_migrator_edge_refuses_before_any_unit(
    m02_ids: m02.Identities, database_url: Any, apply_migrations: Any
) -> None:
    """The M05 owner's migrator edge is cluster-wide: snapshot, revoke, attempt in a
    fresh 005 database, restore exactly as revision 005 grants it, compare."""

    from test_m02_part_a_prerequisites_postgres import _edges, fresh_database

    from haloflow.m01.errors import ProvisioningFailed

    registry_error: Exception | None = None
    try:
        m05_registry()
    except Exception as error:  # noqa: BLE001
        registry_error = error
    assert registry_error is None, f"M05 registry unavailable: {type(registry_error).__name__}"

    before = _edges(m02_ids.admin)
    assert any(row[0] == m05.M05_OWNER for row in before)
    try:
        with fresh_database(m02_ids, database_url, apply_migrations, "005") as (ids, _):
            with m02.connect_admin(m02_ids) as conn:
                conn.execute(f"REVOKE {m05.M05_OWNER} FROM {m05.MIGRATOR}")
            tenant = m02.tenant_for("me04-missing-m05-edge")
            with pytest.raises(ProvisioningFailed) as caught:
                _m05_provision(ids, tenant)
            assert caught.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE"
            assert m02.ledger(ids, tenant[0]) == {}
    finally:
        if not any(row[0] == m05.M05_OWNER for row in _edges(m02_ids.admin)):
            with m02.connect_admin(m02_ids) as conn:
                conn.execute(
                    f"GRANT {m05.M05_OWNER} TO {m05.MIGRATOR}"
                    " WITH INHERIT FALSE, SET TRUE, ADMIN FALSE"
                )
    assert _edges(m02_ids.admin) == before


# --- ME-05 -----------------------------------------------------------------


@pytest.mark.parametrize("role", [m05.M05_OWNER, m05.RUNTIME])
@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
def test_me05_history_update_delete_refused_without_grant(
    m05_install: M05Install, role: str, statement: str
) -> None:
    m05_install.require()
    table = f"{m05_install.schema}.m05_followup_history"
    text = (
        f"UPDATE {table} SET seq = seq WHERE false"
        if statement == "UPDATE"
        else f"DELETE FROM {table} WHERE false"
    )
    with m02.connect_admin(m05_install.ids) as conn:
        conn.execute(f"SET ROLE {role}")
        with pytest.raises(psycopg.Error) as caught:
            conn.execute(text)
        assert caught.value.sqlstate == "42501", (role, statement)


def test_me05_history_privileges_and_rejector_trigger(m05_install: M05Install) -> None:
    m05_install.require()
    table = f"{m05_install.schema}.m05_followup_history"
    owner = {
        privilege: m02.admin_one(
            m05_install.ids,
            "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
            (m05.M05_OWNER, table, privilege),
        )[0]
        for privilege in TABLE_PRIVILEGES
    }
    # Package §2.1: the owner gets INSERT only on history.
    assert owner == {privilege: privilege == "INSERT" for privilege in TABLE_PRIVILEGES}
    # tgtype bits: ROW=1, BEFORE=2, INSERT=4, DELETE=8, UPDATE=16.
    rows = _all(
        m05_install,
        """
        SELECT t.tgtype::int, t.tgenabled
          FROM pg_catalog.pg_trigger t
         WHERE t.tgrelid = pg_catalog.to_regclass(%s) AND NOT t.tgisinternal
        """,
        (table,),
    )
    assert any(
        tgtype & 2 and tgtype & 8 and tgtype & 16 and enabled == "O"
        for tgtype, enabled in rows
    ), rows


# --- MX-02 (catalogue half) --------------------------------------------------


def test_mx02_no_ehr_state_column_and_staff_assertion_check(m05_install: M05Install) -> None:
    m05_install.require()
    rows = _all(
        m05_install,
        """
        SELECT c.relname, a.attname FROM pg_catalog.pg_attribute a
          JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname LIKE 'm05\\_%%' AND c.relkind = 'r'
           AND a.attnum > 0 AND NOT a.attisdropped
        """,
        (m05_install.schema,),
    )
    assert rows, "no M05 columns found"
    offending = [
        (table, column)
        for table, column in rows
        if any(fragment in column.lower() for fragment in m05.FORBIDDEN_COLUMN_FRAGMENTS)
    ]
    assert offending == []
    checks = _all(
        m05_install,
        """
        SELECT pg_catalog.pg_get_constraintdef(k.oid)
          FROM pg_catalog.pg_constraint k
         WHERE k.conrelid = pg_catalog.to_regclass(%s) AND k.contype = 'c'
        """,
        (f"{m05_install.schema}.m05_followup_history",),
    )
    assert any(
        "assertion_kind" in definition and "'staff'" in definition for (definition,) in checks
    ), checks


# --- MB1-P01 / P02 -------------------------------------------------------------


def test_mb1_p01_tenant_reaches_test_target_with_every_unit(m05_install: M05Install) -> None:
    result = m05_install.require()
    assert result.schema_version == m05.M05_TEST_TARGET
    ledger = m02.ledger(m05_install.ids, m05_install.tenant[0])
    assert {migration_id: row[0] for migration_id, row in ledger.items()} == {
        migration_id: "applied" for migration_id in m05.PART_A_UNIT_IDS + m05.M05_UNIT_IDS
    }


def test_mb1_p02_seed_rows(m05_install: M05Install) -> None:
    m05_install.require()
    schema = m05_install.schema
    columns = ", ".join(m05.OUTCOME_CODE_COLUMNS)
    rows = _all(m05_install, f"SELECT {columns} FROM {schema}.m05_outcome_codes")
    assert sorted(rows) == sorted(m05.OUTCOME_CODES)
    assert _all(
        m05_install,
        f"SELECT count(*) FROM {schema}.m05_intake_map WHERE active_in_production",
    ) == [(0,)]
    allowlist = _all(
        m05_install,
        f"""
        SELECT json_path FROM {schema}.m02_envelope_path_allowlist
         WHERE consumer_module = 'm05'
           AND event_type = 'm05_followup_resolution_asserted' AND contract_version = 1
        """,
    )
    assert sorted(path for (path,) in allowlist) == sorted(
        ["outcome_code", "assertion_kind", "item_kind"]
    )
