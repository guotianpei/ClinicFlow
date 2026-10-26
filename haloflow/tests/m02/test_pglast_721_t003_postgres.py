"""pglast 7.21 repair (D-1a), PostgreSQL rows for T003. Tests written before the change.

PGT-7: a fresh tenant installs T003 and the ledger records the NEW (7.21) checksum.
PGT-8: a tenant whose ledger records the OLD (7.17-era) T003 checksum is refused as
       drift, and nothing changes: ledger row, installed function body and ACL.
       The old record is the real old identity: T003_OLD_CHECKSUM is main 0367601's
       recorded value, re-derived from the old declaration in test_pglast_721.py; the
       function body is the same in both declarations (only the policy version moved).
       ARRANGEMENT (labelled): the old ledger value is written by ADMIN, because the
       current code cannot install a 7.17 declaration.
PGT-10: an unsupported parser at install time is refused in the whole-plan preflight with
       INSTALL_PARSER_VERSION_MISMATCH: ledger rows and tenant-schema inventory unchanged.
No upgrade path is claimed: PGT-8 records the limit for existing 7.17-era installs.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

import m02_gateway_support as gw
import m02_support as m02
import pytest

pytestmark = pytest.mark.postgres

T003 = "t003_m02_lock_operation"
T003_OLD_CHECKSUM = "fff7b9cebb6cf85d8327535d27934163ed3b7375a2064f82857dc9e9af5e2a6d"
T003_NEW_CHECKSUM = "6adea880c1cd8ee54028e520348427ec19b173a0ce6d36854ac2008488289843"


def _ledger_checksum(ids: Any, tenant_id: str) -> tuple[Any, ...]:
    return m02.admin_one(
        ids,
        "SELECT state, checksum, attempt, completed_at IS NOT NULL"
        " FROM shared.schema_migrations WHERE tenant_id = %s AND migration_id = %s",
        (tenant_id, T003),
    )


def _ledger_rows(ids: Any, tenant_id: str) -> list[tuple[Any, ...]]:
    """Every column of every ledger row for the tenant, actual values."""
    return m02.admin_all(
        ids,
        "SELECT migration_id, checksum, state, attempt, started_at, completed_at,"
        " sanitized_error_code FROM shared.schema_migrations WHERE tenant_id = %s"
        " ORDER BY migration_id",
        (tenant_id,),
    )


def _schema_inventory(ids: Any, schema: str) -> list[tuple[Any, ...]]:
    """Every relation and routine in the tenant schema, with owner and ACL."""
    return m02.admin_all(
        ids,
        "SELECT 'rel', c.relname, c.relkind::text, c.relowner::regrole::text,"
        " c.relacl::text FROM pg_catalog.pg_class c"
        " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s"
        " UNION ALL"
        " SELECT 'proc', p.oid::regprocedure::text, p.prokind::text,"
        " p.proowner::regrole::text, p.proacl::text FROM pg_catalog.pg_proc p"
        " JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s"
        " ORDER BY 1, 2",
        (schema, schema),
    )


def _function_def(ids: Any, schema: str) -> tuple[Any, ...]:
    return m02.admin_one(
        ids,
        "SELECT pg_catalog.pg_get_functiondef(pg_catalog.to_regprocedure(%s))",
        (gw.regprocedure(schema),),
    )


def test_pgt7_fresh_install_records_the_new_checksum(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    outcomes = gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    assert {o.migration_id: o.applied for o in outcomes}[T003] is True
    state, checksum, _attempt, completed = _ledger_checksum(m02_ids, m02_tenant[0])
    assert (state, checksum, completed) == ("applied", T003_NEW_CHECKSUM, True)


def test_pgt8_a_7_17_era_ledger_record_is_refused_as_drift_with_no_change(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed

    tenant_id, schema = m02_tenant
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    with m02.connect_admin(m02_ids) as conn:  # ARRANGEMENT: the 7.17-era record
        conn.execute(
            "UPDATE shared.schema_migrations SET checksum = %s"
            " WHERE tenant_id = %s AND migration_id = %s",
            (T003_OLD_CHECKSUM, tenant_id, T003),
        )
    before_ledger = _ledger_rows(m02_ids, tenant_id)
    before_body = _function_def(m02_ids, schema)
    before_acl = gw.gateway_acl(m02_ids, schema)
    t003_row = [r for r in before_ledger if r[0] == T003]
    assert len(t003_row) == 1 and t003_row[0][1:3] == (T003_OLD_CHECKSUM, "applied")

    with pytest.raises(TenantMigrationFailed) as caught:
        gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    assert caught.value.reason_code == "MIGRATION_CHECKSUM_DRIFT"

    assert _ledger_rows(m02_ids, tenant_id) == before_ledger  # every column, actual values
    assert _function_def(m02_ids, schema) == before_body
    assert gw.gateway_acl(m02_ids, schema) == before_acl


@pytest.mark.parametrize("version", ["7.17", "7.21.1"])
def test_pgt10_an_unsupported_parser_at_install_is_refused_before_any_statement(
    version: str, m02_ids: Any, m02_tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed

    real = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version",
                        lambda name: version if name == "pglast" else real(name))
    tenant_id, schema = m02_tenant
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    before_ledger = _ledger_rows(m02_ids, tenant_id)
    before_schema = _schema_inventory(m02_ids, schema)
    with pytest.raises(TenantMigrationFailed) as caught:
        gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    assert caught.value.reason_code == "INSTALL_PARSER_VERSION_MISMATCH"
    # Refused in the whole-plan preflight: no ledger row written or changed and no
    # object created in the tenant schema, for any unit, not only T003.
    assert _ledger_rows(m02_ids, tenant_id) == before_ledger
    assert _schema_inventory(m02_ids, schema) == before_schema
    assert gw.gateway_absent(m02_ids, schema)
