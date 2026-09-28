"""CP2-2b 2B-D25 to D27 (R-B11): the CI ownership inventory.

D25a to D25i are CONSTRUCTED classifier rows (no database; never server evidence).
D25j, D25k, D25l, D26 and D27 run on PostgreSQL 17 through `ServerSource`, with
ADMIN as the observer; the extra objects are created by the lock owner itself
(`connect_as(..., "LOCK")`: migrator login -> SET ROLE lock owner, no superuser).

Status: D25a to D25i DB (they import only test code, but bind the approved
classifier contract; they pass as soon as this module collects). D25j to D27 MB.
"""

from __future__ import annotations

from typing import Any

import m02_inventory as inv
import m02_support as m02
import pytest
from psycopg import sql

TARGET = "tenant_target"
OTHER = "tenant_other"
GATEWAY = inv.Address("pg_proc", 1000)
NS_TARGET, NS_OTHER = 11, 12
NAMES = {NS_TARGET: TARGET, NS_OTHER: OTHER}


def constructed(**overrides: Any) -> inv.ConstructedSource:
    """A CONSTRUCTED baseline: the gateway alone, in A and B, no dependents."""

    source = inv.ConstructedSource(
        spine_rows=[GATEWAY],
        direct_rows=[GATEWAY],
        namespaces={GATEWAY: NS_TARGET},
        names=dict(NAMES),
    )
    for name, value in overrides.items():
        setattr(source, name, value)
    return source


def run(source: inv.ConstructedSource) -> inv.Result:
    return inv.classify(source, target_schema=TARGET, gateway=GATEWAY)


# --- constructed classifier rows (labelled CONSTRUCTED) --------------------------------


def test_2b_d25_constructed_baseline_passes_control() -> None:
    assert run(constructed()).passes(GATEWAY)


def test_2b_d25a_an_object_missing_from_the_spine_is_found_by_b_and_fails() -> None:
    extra = inv.Address("pg_class", 2000)
    result = run(constructed(
        direct_rows=[GATEWAY, extra], namespaces={GATEWAY: NS_TARGET, extra: NS_TARGET}
    ))
    assert extra in result.b_not_in_a and extra in result.target_objects
    assert not result.passes(GATEWAY)


def test_2b_d25b_a_mapped_spine_object_absent_from_b_is_spine_only() -> None:
    extra = inv.Address("pg_class", 2001)
    result = run(constructed(
        spine_rows=[GATEWAY, extra], namespaces={GATEWAY: NS_TARGET, extra: NS_TARGET}
    ))
    assert f"spine-only:{extra}" in result.failures


def test_2b_d25c_an_unresolved_namespace_fails() -> None:
    extra = inv.Address("pg_type", 2002)
    result = run(constructed(
        spine_rows=[GATEWAY, extra], direct_rows=[GATEWAY, extra],
        namespaces={GATEWAY: NS_TARGET, extra: 99},
    ))
    assert f"unresolved:{extra}" in result.failures


def test_2b_d25d_an_unmapped_dependent_fails() -> None:
    table = inv.Address("pg_class", 2003)
    rule = inv.Address("pg_rewrite", 2004)
    result = run(constructed(
        spine_rows=[GATEWAY, table], direct_rows=[GATEWAY, table],
        namespaces={GATEWAY: NS_TARGET, table: NS_OTHER},
        edges={table: [(rule, "i")]},
    ))
    assert f"unmapped:{rule}" in result.failures


def test_2b_d25e_a_dependency_cycle_terminates_and_each_address_appears_once() -> None:
    x, y = inv.Address("pg_class", 2005), inv.Address("pg_type", 2006)
    result = run(constructed(
        spine_rows=[GATEWAY, x], direct_rows=[GATEWAY, x, y],
        namespaces={GATEWAY: NS_TARGET, x: NS_OTHER, y: NS_OTHER},
        edges={x: [(y, "i")], y: [(x, "i")]},
    ))
    assert sorted(result.walked) == sorted(set(result.walked))
    assert {x, y} <= set(result.walked)


def test_2b_d25f_duplicates_across_a_b_and_the_walk_count_once() -> None:
    x = inv.Address("pg_class", 2007)
    result = run(constructed(
        spine_rows=[GATEWAY, x, x], direct_rows=[GATEWAY, x, x],
        namespaces={GATEWAY: NS_TARGET, x: NS_OTHER},
        edges={GATEWAY: [], x: [(x, "a")]},
    ))
    assert result.walked.count(x) == 1


def test_2b_d25g_a_column_dependent_is_classified_through_its_parent() -> None:
    table = inv.Address("pg_class", 2008)
    column = inv.Address("pg_class", 2008, 3)
    result = run(constructed(
        spine_rows=[GATEWAY, table], direct_rows=[GATEWAY, table],
        namespaces={GATEWAY: NS_TARGET, table: NS_TARGET},
        edges={table: [(column, "a")]},
    ))
    assert column in result.walked
    assert table in result.target_objects


def test_2b_d25h_an_object_in_another_tenant_schema_is_outside_the_target() -> None:
    other_gateway = inv.Address("pg_proc", 2009)
    result = run(constructed(
        spine_rows=[GATEWAY, other_gateway], direct_rows=[GATEWAY, other_gateway],
        namespaces={GATEWAY: NS_TARGET, other_gateway: NS_OTHER},
    ))
    assert result.passes(GATEWAY)


def test_2b_d25i_an_unmapped_spine_class_fails() -> None:
    """CONSTRUCTED unmapped class (formerly D28). Never counted as server evidence."""

    unmapped = inv.Address("pg_event_trigger", 2010)
    result = run(constructed(spine_rows=[GATEWAY, unmapped]))
    assert f"unmapped:{unmapped}" in result.failures


def test_2b_d25m_a_gateway_absent_from_both_ownership_sources_fails() -> None:
    """Codex packet-v1 hardening: a function address alone cannot satisfy the row."""

    other = inv.Address("pg_proc", 2012)
    result = run(constructed(
        spine_rows=[other], direct_rows=[other],
        namespaces={GATEWAY: NS_TARGET, other: NS_OTHER},
    ))
    assert f"gateway-not-owned:{GATEWAY}" in result.failures
    assert not result.passes(GATEWAY)


def test_2b_d25_a_gateway_dependent_under_an_included_kind_fails() -> None:
    child = inv.Address("pg_type", 2011)
    result = run(constructed(
        direct_rows=[GATEWAY, child], namespaces={GATEWAY: NS_TARGET, child: NS_OTHER},
        edges={GATEWAY: [(child, "i")]},
    ))
    assert result.gateway_dependents == [child]
    assert not result.passes(GATEWAY)


# --- server rows (PostgreSQL 17) -------------------------------------------------------


def _classify_live(ids: Any, schema: str) -> tuple[inv.Result, inv.Address]:
    with m02.connect_admin(ids) as conn:
        gateway = inv.gateway_address(conn, schema)
        result = inv.classify(
            inv.ServerSource(conn, m02.LOCK_OWNER), target_schema=schema, gateway=gateway
        )
    return result, gateway


def _provision(ids: Any, tenant: tuple[str, str]) -> None:
    m02.provision_sync(ids, m02.production_registry(), tenant)


@pytest.mark.postgres
def test_2b_d25j_the_live_inventory_of_a_production_tenant_is_exactly_the_gateway(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    result, gateway = _classify_live(m02_ids, production_tenant[1])
    assert result.failures == []
    assert result.target_objects == {gateway}


@pytest.mark.postgres
def test_2b_d25k_the_live_gateway_has_no_dependents_under_included_kinds(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    result, _ = _classify_live(m02_ids, production_tenant[1])
    assert result.gateway_dependents == []


@pytest.mark.postgres
def test_2b_d25l_a_normal_dependent_view_is_found_by_the_direct_scan(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """Concrete coverage evidence for the n-exclusion rationale, not universal proof."""

    _provision(m02_ids, m02_tenant)
    schema = m02_tenant[1]
    with m02.connect_as(m02_ids, "LOCK") as conn:
        conn.execute(sql.SQL("CREATE TABLE {} (x int)").format(sql.Identifier(schema, "d25l_t")))
        conn.execute(sql.SQL("CREATE VIEW {} AS SELECT x FROM {}").format(
            sql.Identifier(schema, "d25l_v"), sql.Identifier(schema, "d25l_t")))
    with m02.connect_admin(m02_ids) as conn:
        view_oid = conn.execute(
            "SELECT pg_catalog.to_regclass(%s)::oid::bigint", (f"{schema}.d25l_v",)
        ).fetchone()
    assert view_oid is not None
    result, gateway = _classify_live(m02_ids, schema)
    view = inv.Address("pg_class", int(view_oid[0]))
    assert view in result.target_objects
    assert not result.passes(gateway)
    type_rows = sorted(a for a in result.target_objects if a.catalog == "pg_type")
    print(f"D25l report: pg_type objects reached in target: {type_rows}")


@pytest.mark.postgres
@pytest.mark.parametrize("kind", ["table", "function", "collation"])
def test_2b_d26_a_real_extra_object_owned_by_the_lock_owner_is_detected(
    m02_ids: Any, m02_tenant: tuple[str, str], kind: str
) -> None:
    """R-B11.2. The collation is a FEASIBILITY GATE: if the lock owner cannot create it,
    a reviewed real alternative outside pg_class/pg_proc replaces it; never removed."""

    _provision(m02_ids, m02_tenant)
    schema = m02_tenant[1]
    statements = {
        "table": sql.SQL("CREATE TABLE {} (x int)").format(sql.Identifier(schema, "d26_t")),
        "function": sql.SQL(
            "CREATE FUNCTION {}() RETURNS int LANGUAGE sql AS 'SELECT 1'"
        ).format(sql.Identifier(schema, "d26_f")),
        "collation": sql.SQL('CREATE COLLATION {} FROM "C"').format(
            sql.Identifier(schema, "d26_c")),
    }
    with m02.connect_as(m02_ids, "LOCK") as conn:
        conn.execute(statements[kind])
    result, gateway = _classify_live(m02_ids, schema)
    expected_catalog = {"table": "pg_class", "function": "pg_proc", "collation": "pg_collation"}
    assert any(
        a.catalog == expected_catalog[kind] and a != gateway for a in result.target_objects
    )
    assert not result.passes(gateway)


@pytest.mark.postgres
def test_2b_d27_another_tenants_gateway_is_outside_and_an_overload_is_detected(
    m02_ids: Any, production_tenant: tuple[str, str], m02_tenant: tuple[str, str]
) -> None:
    _provision(m02_ids, m02_tenant)
    result, gateway = _classify_live(m02_ids, production_tenant[1])
    assert result.passes(gateway), result
    schema = m02_tenant[1]
    with m02.connect_as(m02_ids, "LOCK") as conn:
        conn.execute(sql.SQL(
            "CREATE FUNCTION {}(p text) RETURNS text LANGUAGE sql AS 'SELECT p'"
        ).format(sql.Identifier(schema, "m02_lock_operation")))
    overloaded, own_gateway = _classify_live(m02_ids, schema)
    assert len([a for a in overloaded.target_objects if a.catalog == "pg_proc"]) == 2
    assert not overloaded.passes(own_gateway)
