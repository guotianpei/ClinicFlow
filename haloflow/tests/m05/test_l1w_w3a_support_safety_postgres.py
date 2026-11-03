"""L-1W W3a: live attribute safety of the USAGE-only support profile (Codex 225500Z).

Stage 1 (`assert_execution_roles_safe`) now also checks every manifest profile that
receives tenant-schema privileges, even when no registry unit runs as it. These rows
drift haloflow_support AFTER 008 (registry and controlled edges otherwise valid) and
prove both entry points refuse before any mutation; the safe control provisions.

- SUP-05: the provisioner refuses a new tenant (no ledger row, no schema) when support
  is LOGIN, BYPASSRLS or renamed away (missing).
- SUP-06: the migration runner refuses an installed tenant on the same drift, ledger
  unchanged.
- SUP-07: control: safe support with a legitimate support login member provisions.

The drift is applied to the cluster-global role in the disposable test cluster and
reverted in a `finally`; the test database name guard (haloflow_test*) applies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import m02_support
import pytest
import staff_support as st
from psycopg import sql

pytestmark = pytest.mark.postgres

SUPPORT = "haloflow_support"
DRIFTS = {
    "login": ("ALTER ROLE haloflow_support LOGIN", "ALTER ROLE haloflow_support NOLOGIN"),
    "bypassrls": ("ALTER ROLE haloflow_support BYPASSRLS",
                  "ALTER ROLE haloflow_support NOBYPASSRLS"),
    "missing": ("ALTER ROLE haloflow_support RENAME TO haloflow_test_support_away",
                "ALTER ROLE haloflow_test_support_away RENAME TO haloflow_support"),
}


@contextmanager
def _drifted(ids: Any, case: str) -> Iterator[None]:
    apply, revert = DRIFTS[case]
    with m02_support.connect_admin(ids) as conn:
        conn.autocommit = True
        conn.execute(apply)
    try:
        yield
    finally:
        with m02_support.connect_admin(ids) as conn:
            conn.autocommit = True
            conn.execute(revert)
        assert m02_support.role_attributes(ids, SUPPORT) is not None


def _safe(ids: Any) -> None:
    attributes = m02_support.role_attributes(ids, SUPPORT)
    assert attributes is not None and not any(attributes.values()), attributes


@pytest.mark.parametrize("case", sorted(DRIFTS))
def test_sup05_provisioner_refuses_drifted_support_before_any_mutation(
    m02_ids: Any, reset_tenants: Any, case: str
) -> None:
    from haloflow.m01.errors import ProvisioningFailed

    _safe(m02_ids)
    tenant = m02_support.tenant_for(f"l1w-w3a-sup05:{case}")
    reset_tenants(m02_ids.admin, [tenant[0]], [tenant[1]])
    try:
        with _drifted(m02_ids, case), pytest.raises(ProvisioningFailed) as refused:
            m02_support.provision_sync(m02_ids, st.staff_registry(), tenant,
                                       supported=st.STAFF_TEST_SUPPORTED,
                                       manifest=st.staff_manifest())
        m02_support.assert_stage_one_refusal(m02_ids, refused.value, tenant)
        _safe(m02_ids)
    finally:
        reset_tenants(m02_ids.admin, [tenant[0]], [tenant[1]])


@pytest.mark.parametrize("case", sorted(DRIFTS))
def test_sup06_runner_refuses_drifted_support_ledger_unchanged(
    m02_ids: Any, staff_install: st.StaffInstall, case: str
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed
    from haloflow.m01.provisioning import MIGRATOR_ROLE, TenantMigrationRunner

    staff_install.require()
    tenant_id, schema_key = staff_install.tenant
    ledger = m02_support.ledger(m02_ids, tenant_id)
    runner = TenantMigrationRunner(
        m02_support._connection_factory(m02_ids.logins[MIGRATOR_ROLE]),
        st.staff_registry(), manifest=st.staff_manifest())
    with _drifted(m02_ids, case), pytest.raises(TenantMigrationFailed) as refused:
        asyncio.run(runner.apply(tenant_id=tenant_id, schema_key=schema_key))
    assert refused.value.reason_code == m02_support.EXECUTION_ROLE_UNAVAILABLE
    assert m02_support.ledger(m02_ids, tenant_id) == ledger
    # Control: with support restored, the same runner call succeeds (all units applied).
    asyncio.run(runner.apply(tenant_id=tenant_id, schema_key=schema_key))
    assert m02_support.ledger(m02_ids, tenant_id) == ledger


def test_sup07_safe_support_with_a_login_member_provisions(
    m02_ids: Any, reset_tenants: Any
) -> None:
    tenant = m02_support.tenant_for("l1w-w3a-sup07")
    reset_tenants(m02_ids.admin, [tenant[0]], [tenant[1]])
    login = "haloflow_test_l1w_w3a_sup07_login"
    with m02_support.connect_admin(m02_ids) as conn:
        conn.autocommit = True
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN IN ROLE {}").format(
            sql.Identifier(login), sql.Identifier(SUPPORT)))
    try:
        _safe(m02_ids)
        outcome = m02_support.provision_sync(m02_ids, st.staff_registry(), tenant,
                                             supported=st.STAFF_TEST_SUPPORTED,
                                             manifest=st.staff_manifest())
        assert outcome.schema_version == st.STAFF_TEST_TARGET
    finally:
        reset_tenants(m02_ids.admin, [tenant[0]], [tenant[1]])
        with m02_support.connect_admin(m02_ids) as conn:
            conn.autocommit = True
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))
