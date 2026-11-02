"""UC1 part B, tranche B5 — Alembic revision 007: the migrator reads exactly four
columns of shared.tenants (PostgreSQL 17).

Codex ruling 32e72016 (codex_b5-startup-grant-ruling.md) chose option (a) for the
P-3 gap that Claude reported in 0be43eba. At f9d90e1 the migrator holds no read on
shared.tenants, so the coherent REPEATABLE READ, READ ONLY startup snapshot could
not read the tenants row.

The delta is additive:
- revision 007, the next in the verified chain (down_revision "006");
- it grants only SELECT(tenant_id, schema_key, lifecycle_state, schema_version)
  ON shared.tenants TO haloflow_migrator;
- revision 003 is not rewritten.

Rows:
- G01: the exact column ACL after 007; no table-level entry; no broader privilege.
- G02: the effective read as the migrator login. The four columns are readable; a
  fifth column and every write are refused.
- G03: every other grant and membership is unchanged across 006 -> 007 (full
  before/after diff of the shared ACLs and pg_auth_members).
- G04: the permission contract (permissions.json) declares the read.

Each case runs on a new disposable database built through the real revision chain,
dropped on every exit path. No retained or production database is migrated.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import m02_support as m02
import psycopg
import pytest
from test_m02_part_a_prerequisites_postgres import fresh_database

pytestmark = pytest.mark.postgres

MIGRATOR = "haloflow_migrator"
COLUMNS = ("lifecycle_state", "schema_key", "schema_version", "tenant_id")
MIGRATOR_READ_TOKEN = (
    "shared.tenants:select(tenant_id,schema_key,lifecycle_state,schema_version)"
)

SHARED_ACL = """
SELECT c.relname, NULL::text AS column_name, x.grantee::regrole::text, x.privilege_type,
       x.is_grantable
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
       LATERAL aclexplode(c.relacl) x
 WHERE n.nspname = 'shared'
UNION ALL
SELECT c.relname, a.attname::text, x.grantee::regrole::text, x.privilege_type, x.is_grantable
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped,
       LATERAL aclexplode(a.attacl) x
 WHERE n.nspname = 'shared'
 ORDER BY 1, 2 NULLS FIRST, 3, 4
"""
MEMBERSHIPS = """
SELECT r.rolname, m.rolname, a.set_option, a.inherit_option, a.admin_option
  FROM pg_auth_members a
  JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member
 ORDER BY 1, 2
"""


@pytest.fixture
def at_006(
    m02_ids: Any, database_url: Any, apply_migrations: Any
) -> Iterator[tuple[m02.Identities, Any]]:
    with fresh_database(m02_ids, database_url, apply_migrations, "006") as pair:
        yield pair


def _rows(ids: m02.Identities, query: str, params: Any = None) -> list[tuple[Any, ...]]:
    return m02.admin_all(ids, query, params)


def test_b5_g01_exact_column_acl_after_007(at_006: Any) -> None:
    ids, upgrade = at_006
    upgrade("007")
    entries = [row for row in _rows(ids, SHARED_ACL) if row[2] == MIGRATOR and row[0] == "tenants"]
    assert entries == [("tenants", column, MIGRATOR, "SELECT", False) for column in COLUMNS]
    (privileges,) = _rows(
        ids,
        "SELECT has_table_privilege(%s, 'shared.tenants', 'SELECT'),"
        " has_table_privilege(%s, 'shared.tenants', 'INSERT'),"
        " has_table_privilege(%s, 'shared.tenants', 'UPDATE'),"
        " has_table_privilege(%s, 'shared.tenants', 'DELETE'),"
        " has_table_privilege(%s, 'shared.tenants', 'TRUNCATE'),"
        " has_table_privilege(%s, 'shared.tenants', 'REFERENCES'),"
        " has_table_privilege(%s, 'shared.tenants', 'TRIGGER'),"
        " has_any_column_privilege(%s, 'shared.tenants', 'INSERT'),"
        " has_any_column_privilege(%s, 'shared.tenants', 'UPDATE'),"
        " has_column_privilege(%s, 'shared.tenants', 'display_reference', 'SELECT')",
        (MIGRATOR,) * 10,
    )
    assert privileges == (False,) * 10


def test_b5_g02_effective_read_as_the_migrator_login(at_006: Any) -> None:
    ids, upgrade = at_006
    upgrade("007")
    with m02.connect_as(ids, "MIG") as conn:
        conn.execute("SELECT tenant_id, schema_key, lifecycle_state, schema_version"
                     " FROM shared.tenants").fetchall()
        for statement in (
            "SELECT display_reference FROM shared.tenants",
            "UPDATE shared.tenants SET schema_version = schema_version",
            "DELETE FROM shared.tenants",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(statement)


def test_b5_g03_nothing_else_changes_across_006_to_007(at_006: Any) -> None:
    ids, upgrade = at_006
    acl_before = _rows(ids, SHARED_ACL)
    members_before = _rows(ids, MEMBERSHIPS)
    assert not [row for row in acl_before if row[2] == MIGRATOR and row[0] == "tenants"]
    upgrade("007")
    acl_after = _rows(ids, SHARED_ACL)
    added = [row for row in acl_after if row not in acl_before]
    removed = [row for row in acl_before if row not in acl_after]
    assert added == [("tenants", column, MIGRATOR, "SELECT", False) for column in COLUMNS]
    assert removed == []
    assert _rows(ids, MEMBERSHIPS) == members_before


def test_b5_g04_permission_contract_declares_the_read() -> None:
    document = json.loads(Path("src/haloflow/m01/manifests/permissions.json").read_text())
    assert MIGRATOR_READ_TOKEN in document[MIGRATOR]["allow"]
    assert "shared.tenants:write" in document[MIGRATOR]["deny"]  # unchanged
