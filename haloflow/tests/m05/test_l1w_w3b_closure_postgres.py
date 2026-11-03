"""L-1W W3b: closure of the v1 runtime paths (plan v1r1 §4; final scope C-01/C-02 from
W3 plan v1; Codex 151751Z pin 5). PostgreSQL 17, production registry (target 36).

After t035/t036 (``acl_closure_v1``) haloflow_runtime holds no EXECUTE on
m05_prepare_action(jsonb), m05_apply_action(jsonb) or m01_staff_admin(jsonb). The
functions stay installed, owned as before; only the runtime grant is gone.

Rows: CL-01 exact ACL after provisioning to 36 and startup green; C-01 the runtime
EXECUTE inventory, written out; C-02 each v1 function executed AS haloflow_runtime
raises 42501; CL-02 a restored runtime grant is INSTALLED_STATE_DRIFT at startup;
CL-03 a closure whose execution skips one removal fails VERIFICATION_FAILED and the
unit is not recorded as applied (seam: ``TenantMigrationRegistry.closure_statements``
looked up at call time by the runner); CL-04 a registry below 36 is refused by the
supported set {36}; AM-13 a closure declaration changed after install is drift at the
ledger comparison (immutable stored checksum vs computed), not a patched expectation.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import m02_support as m02
import psycopg
import pytest
from psycopg import errors, sql
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))

pytestmark = pytest.mark.postgres

TARGET: Final = 36
RUNTIME: Final = "haloflow_runtime"
V1: Final = {
    "m05_prepare_action(jsonb)": "haloflow_m05_owner",
    "m05_apply_action(jsonb)": "haloflow_m05_owner",
    "m01_staff_admin(jsonb)": "haloflow_staff_owner",
}
# Measured at fda1cbe (target 32) on PG17.11, minus the three closed v1 functions.
RUNTIME_EXECUTABLE_AFTER_CLOSURE: Final = [
    "m01_staff_admin_v2(p_command jsonb)",
    "m01_staff_display(p_query jsonb)",
    "m01_staff_followup_reader(p_staff_id uuid)",
    "m01_staff_self_standing(p_attribution jsonb)",
    "m02_append_observation(p_command jsonb)",
    "m02_lock_operation(p_operation_id uuid)",
    "m05_apply_action_v2(p_command jsonb)",
    "m05_create_item(p_command jsonb)",
    "m05_get_history(p_item_id uuid)",
    "m05_get_item(p_item_id uuid)",
    "m05_list_items(p_query jsonb)",
    "m05_prepare_action_v2(p_command jsonb)",
    "m05_read_note(p_note_id uuid)",
]


@pytest.fixture
def tenant(
    request: pytest.FixtureRequest, m05_reset: Callable[[tuple[str, str]], None]
) -> Iterator[tuple[str, str]]:
    t = m02.tenant_for(f"w3b-closure:{request.node.nodeid}")
    m05_reset(t)
    try:
        yield t
    finally:
        m05_reset(t)


def _provision(ids: m02.Identities, tenant: tuple[str, str], registry: Any = None) -> Any:
    registry = registry if registry is not None else m02.production_registry()
    return m02.provision_sync(ids, registry, tenant, supported=range(TARGET, TARGET + 1))


def _acl(ids: m02.Identities, schema: str, signature: str) -> list[tuple[Any, ...]]:
    return sorted(m02.admin_all(ids, (
        "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
        " x.privilege_type, pg_get_userbyid(x.grantor), x.is_grantable"
        " FROM pg_proc p, LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x"
        " WHERE p.oid = to_regprocedure(%s)"), (f'"{schema}".{signature}',)))


def _start(ids: m02.Identities, served: tuple[str, ...]) -> Any:
    """Production startup for the served tenants; returns the services or the
    StartupRefused instance."""

    from test_b5_startup_postgres import _deps

    from haloflow.startup import Deployment, StartupRefused, start_production_services

    async def run() -> Any:
        async with _deps(ids) as (d, _counting, _store):
            try:
                return await start_production_services(
                    d, deployment=Deployment.PRODUCTION, served_tenants=served)
            except StartupRefused as refused:
                return refused

    return asyncio.run(run())


def test_cl01_exact_acl_after_provisioning_and_startup_is_green(
    m02_ids: m02.Identities, tenant: tuple[str, str]
) -> None:
    assert m02.production_registry().target_version == TARGET
    outcome = _provision(m02_ids, tenant)
    assert outcome.schema_version == TARGET
    for signature, owner in V1.items():
        assert _acl(m02_ids, tenant[1], signature) == [(owner, "EXECUTE", owner, False)], (
            signature)
    from haloflow.startup import ProductionServices

    assert type(_start(m02_ids, (tenant[0],))) is ProductionServices


def test_c01_runtime_execute_inventory_is_exact(
    m02_ids: m02.Identities, tenant: tuple[str, str]
) -> None:
    _provision(m02_ids, tenant)
    rows = m02.admin_all(m02_ids, (
        "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND has_function_privilege(%s, p.oid, 'EXECUTE')"
        " ORDER BY 1"), (tenant[1], RUNTIME))
    assert [r[0] for r in rows] == RUNTIME_EXECUTABLE_AFTER_CLOSURE


@pytest.mark.parametrize("signature", list(V1))
def test_c02_runtime_calls_to_v1_raise_42501(
    m02_ids: m02.Identities, tenant: tuple[str, str], signature: str
) -> None:
    _provision(m02_ids, tenant)
    name = signature.split("(")[0]
    with psycopg.connect(m02_ids.logins[RUNTIME]) as conn:
        conn.execute(f"SET ROLE {RUNTIME}")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant[0],))
        with pytest.raises(errors.InsufficientPrivilege) as caught:
            conn.execute(sql.SQL("SELECT {}.{}(%s)").format(
                sql.Identifier(tenant[1]), sql.Identifier(name)), (Jsonb({}),))
        assert caught.value.sqlstate == "42501"


def test_cl02_restored_runtime_grant_is_drift_at_startup(
    m02_ids: m02.Identities, tenant: tuple[str, str]
) -> None:
    _provision(m02_ids, tenant)
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:  # the break
        conn.execute(sql.SQL("SET ROLE haloflow_m05_owner"))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {}.m05_prepare_action(jsonb) TO {}")
                     .format(sql.Identifier(tenant[1]), sql.Identifier(RUNTIME)))
    assert (RUNTIME, "EXECUTE", "haloflow_m05_owner", False) in _acl(
        m02_ids, tenant[1], "m05_prepare_action(jsonb)")  # positive control
    refused = _start(m02_ids, (tenant[0],))
    assert getattr(refused, "reason_code", None) == "INSTALLED_STATE_DRIFT"
    assert refused.tenant_index == 0


def test_cl03_partial_closure_fails_verification_and_is_not_applied(
    m02_ids: m02.Identities, tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from haloflow.m01.provisioning.units import TenantMigrationRegistry

    original = TenantMigrationRegistry.closure_statements

    def skip_one(self: Any, migration_id: str, schema_key: str) -> tuple[str, ...]:
        statements = original(self, migration_id, schema_key)
        return statements[:-1] if migration_id.startswith("t035_") else statements

    monkeypatch.setattr(TenantMigrationRegistry, "closure_statements", skip_one)
    with pytest.raises(Exception) as caught:
        _provision(m02_ids, tenant)
    assert getattr(caught.value, "reason_code", None) == "VERIFICATION_FAILED"
    ledger = dict(m02.admin_all(m02_ids, (
        "SELECT migration_id, state FROM shared.schema_migrations WHERE tenant_id = %s"),
        (tenant[0],)))
    assert ledger.get("t034_m01_staff_bootstrap_first_admin") == "applied"
    assert ledger.get("t035_m05_v1_runtime_closure") != "applied"
    # Nothing was revoked by the failed unit: both runtime grants are still present.
    for signature in ("m05_prepare_action(jsonb)", "m05_apply_action(jsonb)"):
        assert (RUNTIME, "EXECUTE", "haloflow_m05_owner", False) in _acl(
            m02_ids, tenant[1], signature)


def test_cl04_a_registry_below_36_is_refused_by_the_supported_set(
    m02_ids: m02.Identities, tenant: tuple[str, str]
) -> None:
    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        APPROVED_TENANT_MIGRATIONS,
        L1W_W3B_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    assert frozenset({TARGET}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    without = build_tenant_migration_registry(
        *[s for s in APPROVED_TENANT_MIGRATIONS if s is not L1W_W3B_TENANT_MIGRATIONS],
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
    )
    assert without.target_version == 32
    with pytest.raises(Exception) as caught:
        _provision(m02_ids, tenant, without)
    assert getattr(caught.value, "reason_code", None) == "SCHEMA_VERSION_UNSUPPORTED"


def test_am13_changed_closure_declaration_after_install_is_ledger_drift(
    m02_ids: m02.Identities, tenant: tuple[str, str]
) -> None:
    """The stored checksum is the one computed at install; a later registry whose t035
    declaration differs (t016 amendment dropped) computes another checksum, and the
    runner re-run against the installed tenant refuses as drift (TC-E6 pattern)."""

    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    _provision(m02_ids, tenant)
    stored = m02.admin_all(m02_ids, (
        "SELECT checksum FROM shared.schema_migrations"
        " WHERE tenant_id = %s AND migration_id = %s"),
        (tenant[0], "t035_m05_v1_runtime_closure"))
    production = m02.production_registry()
    computed = next(u for u in production if u.migration_id.startswith("t035_")).checksum
    assert stored == [(computed,)]
    changed = build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
        acl_amendments=tuple(a for a in APPROVED_ACL_AMENDMENTS
                             if a.target_unit != "t016_m05_apply_action"),
    )
    assert next(u for u in changed if u.migration_id.startswith("t035_")).checksum != computed
    from psycopg import AsyncConnection

    from haloflow.m01.provisioning import MIGRATOR_ROLE, TenantMigrationRunner

    async def connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(m02_ids.logins[MIGRATOR_ROLE], autocommit=True)

    async def rerun() -> None:
        await TenantMigrationRunner(connect, changed).apply(
            tenant_id=tenant[0], schema_key=tenant[1])

    with pytest.raises(Exception) as caught:
        asyncio.run(rerun())
    assert getattr(caught.value, "reason_code", None) == "MIGRATION_CHECKSUM_DRIFT"
    assert m02.admin_all(m02_ids, (  # the evidence is not rewritten
        "SELECT checksum FROM shared.schema_migrations"
        " WHERE tenant_id = %s AND migration_id = %s"),
        (tenant[0], "t035_m05_v1_runtime_closure")) == stored
