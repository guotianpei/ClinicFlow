"""CP2-2b PostgreSQL rows: 2B-D01 to D16, D18 to D24b, D29, D31, D32.

Test cases v3 section 3.5. Real typed production path; the measured identities are
the non-superuser login shims (R-X2), asserted in `m02_gateway_support.runtime` and
`m02_support.connect_as`. ADMIN is setup, observation and cleanup only.

Status (test cases v3 legend): D04, D15, D17b-style pins and the `active` case of
D29 are EB; every other row is MB before implementation; D32 is DB/MB.
Constructed inputs are labelled where they occur.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import m02_gateway_support as gw
import m02_support as m02
import pytest
from psycopg import AsyncConnection, sql

pytestmark = pytest.mark.postgres

T003 = "t003_m02_lock_operation"
VERIFICATION_FAILED = "VERIFICATION_FAILED"


def config_for(schema: str) -> list[str]:
    return [f"search_path=pg_catalog, {schema}, pg_temp"]


def provision_production(ids: m02.Identities, tenant: tuple[str, str]) -> Any:
    return m02.provision_sync(ids, m02.production_registry(), tenant)


# --- D01 to D03: installed shape and exact ACL ----------------------------------


def test_2b_d01_installed_shape(m02_ids: Any, production_tenant: tuple[str, str]) -> None:
    """R-B1."""

    row = m02.admin_one(
        m02_ids,
        """
        SELECT owner.rolname, p.prosecdef,
               p.prorettype = 'pg_catalog.uuid'::pg_catalog.regtype, p.proretset,
               lang.lanname, p.provolatile, p.proparallel, p.proisstrict, p.prokind,
               p.pronargs, p.proargtypes[0] = 'pg_catalog.uuid'::pg_catalog.regtype,
               p.proargmodes IS NULL, p.proallargtypes IS NULL, p.pronargdefaults
          FROM pg_catalog.pg_proc AS p
          JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
          JOIN pg_catalog.pg_language AS lang ON lang.oid = p.prolang
         WHERE p.oid = pg_catalog.to_regprocedure(%s)
        """,
        (gw.regprocedure(production_tenant[1]),),
    )
    assert row == (m02.LOCK_OWNER, True, True, False, "plpgsql", "v", "u", False, "f",
                   1, True, True, True, 0)


def test_2b_d02_proconfig_is_exactly_one_element_in_order(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B1."""

    schema = production_tenant[1]
    assert m02.admin_one(
        m02_ids,
        "SELECT proconfig, pg_catalog.array_length(proconfig, 1) FROM pg_catalog.pg_proc"
        " WHERE oid = pg_catalog.to_regprocedure(%s)",
        (gw.regprocedure(schema),),
    ) == (config_for(schema), 1)


def test_2b_d03_the_acl_is_exactly_the_two_four_field_tuples(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B4, E6. PUBLIC is compared as grantee OID 0 (the first field)."""

    schema = production_tenant[1]
    assert m02.admin_one(
        m02_ids,
        "SELECT proacl IS NOT NULL FROM pg_catalog.pg_proc"
        " WHERE oid = pg_catalog.to_regprocedure(%s)",
        (gw.regprocedure(schema),),
    ) == (True,)
    assert gw.gateway_acl(m02_ids, schema) == gw.EXPECTED_ACL


# --- D04, D05: the privilege boundary and the value -----------------------------


def test_2b_d04_runtime_direct_for_update_is_a_privilege_refusal(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B4. EB: already true under 2a's SELECT-only (RQ-2); listed for traceability."""

    schema = production_tenant[1]
    operation_id = gw.seed(m02_ids, schema, "d04")
    table = sql.Identifier(schema, m02.TABLE)
    with gw.runtime(m02_ids) as conn:
        control = conn.execute(
            sql.SQL("SELECT operation_id FROM {} WHERE operation_id = %s").format(table),
            (operation_id,),
        ).fetchone()
        assert control == (operation_id,)
        m02.expect_sqlstate(
            conn,
            sql.SQL("SELECT operation_id FROM {} WHERE operation_id = %s FOR UPDATE").format(
                table
            ),
            (operation_id,),
            "42501",
        )


def test_2b_d05_a_valid_call_returns_the_rows_id(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B1, R-B6. Paired with D06/D07; ME mutants: `RETURN p_operation_id;` and a
    SELECT without FOR UPDATE must each fail the pair (D06 catches both)."""

    tenant_id, schema = production_tenant
    operation_id = gw.seed(m02_ids, schema, "d05")
    independent = m02.full_row(m02_ids, schema, operation_id)
    assert independent is not None
    with gw.runtime(m02_ids) as conn, conn.transaction():
        gw.set_tenant(conn, tenant_id)
        assert gw.call_gateway(conn, schema, operation_id) == independent[0]


# --- D06 to D08: lock semantics ----------------------------------------------------


def test_2b_d06_same_row_blocks_then_the_release_control_succeeds(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B6. The waiter shows `Lock` and times out with 57014; after the holder rolls
    back, a FRESH transaction's identical call succeeds (the release control)."""

    tenant_id, schema = production_tenant
    operation_id = gw.seed(m02_ids, schema, "d06")
    with gw.holding_lock(m02_ids, schema, tenant_id, operation_id, end="rollback"):
        blocked = gw.probe_call(m02_ids, schema, tenant_id, operation_id)
    assert blocked.sqlstate == "57014"
    assert blocked.lock_wait_seen is True
    released = gw.probe_call(m02_ids, schema, tenant_id, operation_id)
    assert (released.sqlstate, released.value) == (None, operation_id)


def test_2b_d07_different_rows_and_other_tenants_do_not_block(
    m02_ids: Any, production_tenant: tuple[str, str], m02_tenant: tuple[str, str]
) -> None:
    """R-B6 anti-vacuity: a whole-table lock or an always-failing call would fail here."""

    tenant_id, schema = production_tenant
    held = gw.seed(m02_ids, schema, "d07-held")
    other_row = gw.seed(m02_ids, schema, "d07-other")
    provision_production(m02_ids, m02_tenant)
    same_value = gw.seed(m02_ids, m02_tenant[1], "d07-held")
    assert same_value == held
    with gw.holding_lock(m02_ids, schema, tenant_id, held):
        different = gw.probe_call(m02_ids, schema, tenant_id, other_row)
        other_tenant = gw.probe_call(m02_ids, m02_tenant[1], m02_tenant[0], same_value)
    assert (different.sqlstate, different.value) == (None, other_row)
    assert (other_tenant.sqlstate, other_tenant.value) == (None, same_value)


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_2b_d08_the_lock_is_held_until_commit_or_rollback(
    m02_ids: Any, production_tenant: tuple[str, str], end: str
) -> None:
    """R-B6: the observer blocks before the end, and succeeds after it."""

    tenant_id, schema = production_tenant
    operation_id = gw.seed(m02_ids, schema, f"d08-{end}")
    with gw.holding_lock(m02_ids, schema, tenant_id, operation_id, end=end):
        before = gw.probe_call(m02_ids, schema, tenant_id, operation_id)
    after = gw.probe_call(m02_ids, schema, tenant_id, operation_id)
    assert before.sqlstate == "57014"
    assert (after.sqlstate, after.value) == (None, operation_id)


# --- D09 to D13: refusals, precedence, isolation --------------------------------------


async def _lock_operation_as_runtime(
    ids: Any, schema: str, tenant: str | None, operation_id: Any, isolation: str | None = None
) -> Any:
    from haloflow.m02.lock import lock_operation

    async with await AsyncConnection.connect(ids.logins[m02.RUNTIME], autocommit=True) as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(m02.RUNTIME)))
        async with conn.transaction():
            if isolation is not None:
                await conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            if tenant is not None:
                await conn.execute(
                    "SELECT pg_catalog.set_config('app.tenant_id', %s, true)", (tenant,)
                )
            try:
                return await lock_operation(conn, schema_key=schema, operation_id=operation_id)
            except Exception as error:  # returned for the caller's assertions
                return error


def test_2b_d09_null_id_is_22004_and_maps_to_its_code(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B1 (G-3), R-B5. No row changes."""

    from haloflow.m02.lock import LockOperationRefused

    tenant_id, schema = production_tenant
    operation_id = gw.seed(m02_ids, schema, "d09")
    before = m02.full_row(m02_ids, schema, operation_id)
    with gw.runtime(m02_ids) as conn:
        gw.expect_call_sqlstate(conn, schema, tenant_id, None, "22004")
    error = asyncio.run(_lock_operation_as_runtime(m02_ids, schema, tenant_id, None))
    assert type(error) is LockOperationRefused
    assert error.code.value == "LOCK_OPERATION_ID_REQUIRED"
    assert m02.full_row(m02_ids, schema, operation_id) == before


@pytest.mark.parametrize("context", [None, "", "A", "-x-"], ids=["unset", "empty", "A", "-x-"])
def test_2b_d10_an_invalid_tenant_context_is_22023(
    m02_ids: Any, production_tenant: tuple[str, str], context: str | None
) -> None:
    """R-B2: unset, empty and malformed are each refused (not authentication)."""

    from haloflow.m02.lock import LockOperationRefused

    _, schema = production_tenant
    operation_id = gw.seed(m02_ids, schema, f"d10-{context!r}")
    with gw.runtime(m02_ids) as conn:
        gw.expect_call_sqlstate(conn, schema, context, operation_id, "22023")
    error = asyncio.run(_lock_operation_as_runtime(m02_ids, schema, context, operation_id))
    assert type(error) is LockOperationRefused
    assert error.code.value == "LOCK_TENANT_CONTEXT_INVALID"


def test_2b_d10_control_a_well_formed_other_tenant_id_is_not_refused(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B2 control: well-formedness only; the value is not checked against the schema."""

    _, schema = production_tenant
    operation_id = gw.seed(m02_ids, schema, "d10-control")
    with gw.runtime(m02_ids) as conn, conn.transaction():
        gw.set_tenant(conn, "clinic-some-other-tenant")
        assert gw.call_gateway(conn, schema, operation_id) == operation_id


def test_2b_d11_null_id_and_invalid_context_gives_22004(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B2 precedence (architecture v3 section 2)."""

    with gw.runtime(m02_ids) as conn:
        gw.expect_call_sqlstate(conn, production_tenant[1], "A", None, "22004")


def test_2b_d12_a_missing_id_is_p0002_never_null(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B1 (G-2), R-B5; control: an existing id in the same shape succeeds."""

    from haloflow.m02.lock import LockOperationRefused

    tenant_id, schema = production_tenant
    missing = uuid5(NAMESPACE_URL, "haloflow-test:m02:d12:missing")
    existing = gw.seed(m02_ids, schema, "d12")
    with gw.runtime(m02_ids) as conn:
        gw.expect_call_sqlstate(conn, schema, tenant_id, missing, "P0002")
        with conn.transaction():
            gw.set_tenant(conn, tenant_id)
            assert gw.call_gateway(conn, schema, existing) == existing
    error = asyncio.run(_lock_operation_as_runtime(m02_ids, schema, tenant_id, missing))
    assert type(error) is LockOperationRefused
    assert error.code.value == "LOCK_OPERATION_NOT_FOUND"


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_2b_d13_the_caller_enforces_read_committed_the_gateway_is_compatible(
    m02_ids: Any, production_tenant: tuple[str, str], isolation: str
) -> None:
    """R-B6. The direct call's success is COMPATIBILITY ONLY (D34b), not a claim about
    the gateway's internals."""

    from haloflow.m02.lock import LockOperationPreconditionError

    tenant_id, schema = production_tenant
    operation_id = gw.seed(m02_ids, schema, f"d13-{isolation}")
    error = asyncio.run(
        _lock_operation_as_runtime(m02_ids, schema, tenant_id, operation_id, isolation)
    )
    assert type(error) is LockOperationPreconditionError
    with gw.runtime(m02_ids) as conn, conn.transaction():
        conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")  # type: ignore[arg-type]
        gw.set_tenant(conn, tenant_id)
        assert gw.call_gateway(conn, schema, operation_id) == operation_id


# --- D14 to D16: isolation, absence, owners -----------------------------------------


def test_2b_d14_a_tenant_gateway_never_reaches_another_tenants_row(
    m02_ids: Any, production_tenant: tuple[str, str], m02_tenant: tuple[str, str]
) -> None:
    """R-B7: B's id through A's gateway is NOT_FOUND."""

    tenant_a, schema_a = production_tenant
    provision_production(m02_ids, m02_tenant)
    only_in_b = gw.seed(m02_ids, m02_tenant[1], "d14-only-b")
    with gw.runtime(m02_ids) as conn:
        gw.expect_call_sqlstate(conn, schema_a, tenant_a, only_in_b, "P0002")


def test_2b_d15_m02_rotate_key_version_is_absent_with_a_positive_control(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B7. EB. The detection query must find the name where it DOES exist."""

    query = (
        "SELECT count(*) FROM pg_catalog.pg_proc AS p"
        " JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND p.proname = 'm02_rotate_key_version'"
    )
    assert m02.admin_one(m02_ids, query, (production_tenant[1],)) == (0,)
    scratch = "m02_d15_scratch"
    with m02.connect_admin(m02_ids) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(scratch)))
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(scratch)))
        conn.execute(sql.SQL(
            "CREATE FUNCTION {}.m02_rotate_key_version() RETURNS void LANGUAGE sql AS 'SELECT'"
        ).format(sql.Identifier(scratch)))
    try:
        assert m02.admin_one(m02_ids, query, (scratch,)) == (1,)
    finally:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(scratch)))


def test_2b_d16_table_and_function_owners_are_separated(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """R-B8 (CP2-D07)."""

    schema = production_tenant[1]
    assert m02.admin_one(
        m02_ids,
        "SELECT (SELECT r.rolname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles r"
        "         ON r.oid = c.relowner WHERE c.oid = pg_catalog.to_regclass(%s)),"
        "       (SELECT r.rolname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles r"
        "         ON r.oid = p.proowner WHERE p.oid = pg_catalog.to_regprocedure(%s))",
        (m02.qualified(schema), gw.regprocedure(schema)),
    ) == (m02.MIGRATOR, m02.LOCK_OWNER)


# --- D18: the lock owner's schema privileges (R-B10) ---------------------------------


def test_2b_d18_the_lock_owner_schema_acl_is_exactly_usage_and_create(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    rows = m02.admin_all(
        m02_ids,
        """
        SELECT e.privilege_type, e.is_grantable, grantor.rolname
          FROM pg_catalog.pg_namespace AS n,
               LATERAL pg_catalog.aclexplode(n.nspacl) AS e
          JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = e.grantor
         WHERE n.nspname = %s AND e.grantee = %s::pg_catalog.regrole
        """,
        (production_tenant[1], m02.LOCK_OWNER),
    )
    assert sorted(rows) == [("CREATE", False, "haloflow_provisioner"),
                            ("USAGE", False, "haloflow_provisioner")]


# --- D19 to D24b: atomic post-install verification (R-B9) ----------------------------


def test_2b_d19_a_clean_install_is_applied_and_the_adapter_ran(
    m02_ids: Any, m02_tenant: tuple[str, str], monkeypatch: Any
) -> None:
    with gw.adapter_calls(monkeypatch) as calls:
        outcome = provision_production(m02_ids, m02_tenant)
    assert outcome.schema_version == 3
    assert calls == [m02_tenant[1]]
    assert m02.ledger(m02_ids, m02_tenant[0])[T003] == ("applied", None, True)


def _plant(case: str, schema: str) -> tuple[str, str]:
    """(grant, revert) ADMIN setup statements for each corrupted-default shape."""

    lo, ap = m02.LOCK_OWNER, gw.AUDIT_PROJECTOR
    if case == "d20-schema-extra-grantee":
        return (f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} IN SCHEMA {schema}"
                f" GRANT EXECUTE ON FUNCTIONS TO {ap}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} IN SCHEMA {schema}"
                f" REVOKE EXECUTE ON FUNCTIONS FROM {ap}")
    if case == "d21-grantable-extra-grantee":
        return (f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} IN SCHEMA {schema}"
                f" GRANT EXECUTE ON FUNCTIONS TO {ap} WITH GRANT OPTION",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} IN SCHEMA {schema}"
                f" REVOKE EXECUTE ON FUNCTIONS FROM {ap}")
    if case == "d22-global-owner-removed":
        return (f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} REVOKE EXECUTE ON FUNCTIONS FROM {lo}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} GRANT EXECUTE ON FUNCTIONS TO {lo}")
    if case == "d23-global-added-grantee":
        return (f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} GRANT EXECUTE ON FUNCTIONS TO {ap}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} REVOKE EXECUTE ON FUNCTIONS FROM {ap}")
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    ["d20-schema-extra-grantee", "d21-grantable-extra-grantee", "d22-global-owner-removed",
     "d23-global-added-grantee"],
)
def test_2b_d20_to_d23_a_corrupted_default_is_refused_and_rolled_back(
    m02_ids: Any, m02_tenant: tuple[str, str], case: str
) -> None:
    """R-B9. The defaults are planted by an ADMIN fixture BEFORE the install (labelled
    setup), through a bare schema so that a schema-scoped row can exist first."""

    from haloflow.m01.errors import TenantMigrationFailed

    tenant_id, schema = m02_tenant
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    grant, revert = _plant(case, schema)
    with (
        gw.planted_default(m02_ids, grant, revert),
        pytest.raises(TenantMigrationFailed) as caught,
    ):
        gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    assert caught.value.reason_code == VERIFICATION_FAILED
    surfaced = f"{caught.value} {caught.value.reason_code}"
    for leaked in (schema, gw.AUDIT_PROJECTOR, "m02_lock_operation", "proacl"):
        assert leaked not in surfaced
    assert gw.gateway_absent(m02_ids, schema)
    rows = m02.ledger(m02_ids, tenant_id)
    assert rows["t001_m01_baseline"][0] == "applied"
    assert rows["t002_m02_operation_registry"][0] == "applied"
    assert rows[T003] == ("failed", VERIFICATION_FAILED, True)


def test_2b_d20_control_the_same_bare_schema_route_installs_on_clean_defaults(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    outcomes = gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    assert [o.migration_id for o in outcomes if o.applied] == [
        "t001_m01_baseline", "t002_m02_operation_registry", T003,
    ]
    assert gw.gateway_acl(m02_ids, m02_tenant[1]) == gw.EXPECTED_ACL


def test_2b_d24_an_equal_checksum_rerun_skips_with_no_adapter_call(
    m02_ids: Any, m02_tenant: tuple[str, str], monkeypatch: Any
) -> None:
    """R-B9.6. LABELLED: planting a new default before the rerun does NOT alter the
    installed function (defaults apply at creation), so this row proves only that a
    skip makes no adapter call."""

    gw.prepare_bare_schema(m02_ids, m02_tenant)
    gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    lo, ap, schema = m02.LOCK_OWNER, gw.AUDIT_PROJECTOR, m02_tenant[1]
    grant = (f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} IN SCHEMA {schema}"
             f" GRANT EXECUTE ON FUNCTIONS TO {ap}")
    revert = (f"ALTER DEFAULT PRIVILEGES FOR ROLE {lo} IN SCHEMA {schema}"
              f" REVOKE EXECUTE ON FUNCTIONS FROM {ap}")
    with gw.planted_default(m02_ids, grant, revert), gw.adapter_calls(monkeypatch) as calls:
        outcomes = gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    assert {o.migration_id: o.applied for o in outcomes}[T003] is False
    assert calls == []


def test_2b_d24b_installed_state_drift_is_not_detected_on_skip(
    m02_ids: Any, m02_tenant: tuple[str, str], monkeypatch: Any
) -> None:
    """R-B9.7, L-2 documented: a real ACL change after install persists through a skip.
    Cleanup restores the grant and then re-checks D03's exact ACL."""

    gw.prepare_bare_schema(m02_ids, m02_tenant)
    gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    schema = m02_tenant[1]
    target = sql.SQL("FUNCTION {}(uuid)").format(sql.Identifier(schema, gw.GATEWAY))
    with m02.connect_as(m02_ids, "LOCK") as conn:
        conn.execute(sql.SQL("REVOKE EXECUTE ON {} FROM haloflow_runtime").format(target))
    try:
        with gw.adapter_calls(monkeypatch) as calls:
            outcomes = gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
        assert {o.migration_id: o.applied for o in outcomes}[T003] is False
        assert calls == []
        assert all(entry[1] != m02.RUNTIME for entry in gw.gateway_acl(m02_ids, schema))
    finally:
        with m02.connect_as(m02_ids, "LOCK") as conn:
            conn.execute(sql.SQL("GRANT EXECUTE ON {} TO haloflow_runtime").format(target))
    assert gw.gateway_acl(m02_ids, schema) == gw.EXPECTED_ACL


# --- D29: rollout population (O-6) -------------------------------------------------------


def test_2b_d29_a_fresh_tenant_reaches_version_three(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    assert provision_production(m02_ids, m02_tenant).schema_version == 3


def test_2b_d29_a_resumed_provisioning_tenant_reaches_version_three(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    with m02.connect_admin(m02_ids) as conn:
        conn.execute(
            "INSERT INTO shared.tenants (tenant_id, schema_key, lifecycle_state, schema_version)"
            " VALUES (%s, %s, 'provisioning', 1)",
            m02_tenant,
        )
    assert provision_production(m02_ids, m02_tenant).schema_version == 3


def test_2b_d29_an_active_version_two_tenant_is_not_resumable(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """EB: existing behaviour, pinned for L-6."""

    from haloflow.m01.errors import ProvisioningFailed

    with m02.connect_admin(m02_ids) as conn:
        conn.execute(
            "INSERT INTO shared.tenants (tenant_id, schema_key, lifecycle_state, schema_version)"
            " VALUES (%s, %s, 'active', 2)",
            m02_tenant,
        )
    with pytest.raises(ProvisioningFailed) as caught:
        provision_production(m02_ids, m02_tenant)
    assert caught.value.reason_code == "TENANT_NOT_RESUMABLE"


# --- D31: the login chain during the t003 install (R-X2) ----------------------------------


def test_2b_d31_the_gateway_is_created_as_the_lock_owner_through_a_non_superuser_login(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    gw.ChainObservingConnection.observed = []
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    gw.run_migrations(
        m02_ids, m02.production_registry(), m02_tenant,
        connection_class=gw.ChainObservingConnection,
    )
    assert len(gw.ChainObservingConnection.observed) == 1
    session_user, current, superuser = gw.ChainObservingConnection.observed[0]
    assert session_user not in (m02.MIGRATOR, m02.LOCK_OWNER)
    assert current == m02.LOCK_OWNER and superuser is False


# --- D32: immutable expectations on the real path (R-B9.3, R-B9.6) -------------------------


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value


def test_2b_d32_mutating_caller_inputs_after_composition_changes_nothing_installed(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    from haloflow.m01.errors import MigrationUnitRejected
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
    from haloflow.m02.units import (
        T002_MIGRATION_ID,
        T002_SQL,
        T003_DEFINITION,
        T003_MIGRATION_ID,
    )

    policy = _thaw(T003_DEFINITION.policy)
    verification = _thaw(T003_DEFINITION.policy_verification)
    caller_owned = dataclasses.replace(
        T003_DEFINITION, policy=policy, policy_verification=verification
    )

    def compose(definition: Any) -> Any:
        return build_tenant_migration_registry(
            TENANT_MIGRATIONS,
            {T002_MIGRATION_ID: T002_SQL, T003_MIGRATION_ID: definition},
            approved_execution_roles=frozenset({m02.LOCK_OWNER}),
            installed_state_profiles={T003_MIGRATION_ID: LOCK_OPERATION_PROFILE},
        )

    registry = compose(caller_owned)
    for block in (policy, verification):
        block["functions"][0]["config"] = ["search_path=pg_catalog, {schema}"]
        block["functions"][0]["acl"][0]["grantee"] = gw.AUDIT_PROJECTOR
    with pytest.raises(dataclasses.FrozenInstanceError):
        LOCK_OPERATION_PROFILE.owner = gw.AUDIT_PROJECTOR  # type: ignore[misc]

    outcome = m02.provision_sync(m02_ids, registry, m02_tenant)
    schema = m02_tenant[1]
    assert outcome.schema_version == 3
    assert m02.admin_one(
        m02_ids, "SELECT proconfig FROM pg_catalog.pg_proc WHERE oid ="
        " pg_catalog.to_regprocedure(%s)", (gw.regprocedure(schema),),
    ) == (config_for(schema),)
    assert gw.gateway_acl(m02_ids, schema) == gw.EXPECTED_ACL

    with pytest.raises(MigrationUnitRejected) as caught:
        compose(dataclasses.replace(
            T003_DEFINITION, policy=policy, policy_verification=verification
        ))
    assert caught.value.reason_code in ("INSTALLED_STATE_PROFILE_INVALID", "INSTALL_POLICY_INVALID")

