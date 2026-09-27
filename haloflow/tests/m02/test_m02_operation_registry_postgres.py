"""CP2-2a table, runtime-privilege and rejector rows on a production install.

2A-T01–T06 (T04a/b/c), 2A-P01–P04, 2A-J01–J07, 2A-H02. The tenant is
provisioned once per module through `build_production_tenant_migrations()`.
2A-P05 is a U row and lives in `test_m02_registry_units.py`.
"""

from types import ModuleType
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.postgres

T04B_MAX_STATEMENTS = 10_000


@pytest.fixture(scope="module")
def seeded(production_tenant: tuple[str, str], m02_ids: Any) -> dict[str, dict[str, Any]]:
    """Two rows seeded by MIG with known literals, committed (§2.5)."""

    import m02_support as m02

    rows = {"a": m02.seeded_row("a"), "b": m02.seeded_row("b")}
    # Missing-table-safe (Codex P1): with no table (pre-change baseline) nothing is
    # seeded, and each J row's first assertion, `assert_installed`, goes red.
    present = m02.admin_one(
        m02_ids,
        "SELECT pg_catalog.to_regclass(%s) IS NOT NULL",
        (m02.qualified(production_tenant[1]),),
    )
    if present == (True,):
        with m02.connect_as(m02_ids, "MIG") as conn, conn.transaction():
            for row in rows.values():
                conn.execute(m02.insert_statement(production_tenant[1]), row)
    return rows


# --- 2A-T01 .. T06 ---------------------------------------------------------


def test_2a_t01_columns_types_nullability_in_order(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    table = m02.qualified(production_tenant[1])
    assert m02.admin_one(m02_ids, "SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (table,)) == (
        True,
    )
    columns = m02.admin_all(
        m02_ids,
        """
        SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = pg_catalog.to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        (table,),
    )
    assert tuple(columns) == m02.EXPECTED_COLUMNS


def test_2a_t02_constraints(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    table = m02.qualified(production_tenant[1])
    rows = m02.admin_all(
        m02_ids,
        """
        SELECT c.contype,
               ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(n, o)
                     JOIN pg_catalog.pg_attribute AS a
                       ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.o)::text[],
               c.confrelid = c.conrelid,
               ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY AS k(n, o)
                     JOIN pg_catalog.pg_attribute AS a
                       ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.o)::text[]
          FROM pg_catalog.pg_constraint AS c
         WHERE c.conrelid = pg_catalog.to_regclass(%s)
        """,
        (table,),
    )
    by_type: dict[str, list[tuple[Any, ...]]] = {}
    for contype, keys, self_ref, fkeys in rows:
        by_type.setdefault(contype, []).append((list(keys), self_ref, list(fkeys)))
    assert [keys for keys, _, _ in by_type["p"]] == [["operation_id"]]
    assert [keys for keys, _, _ in by_type["u"]] == [list(m02.UNIQUE_COLUMNS)]
    assert by_type["f"] == [(["resend_of_operation_id"], True, ["operation_id"])]
    assert len(by_type.get("c", [])) >= 1


def test_2a_t03_self_reference_is_refused(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    row = m02.seeded_row("t03")
    row["resend_of_operation_id"] = row["operation_id"]
    with m02.connect_as(m02_ids, "MIG") as conn:
        m02.expect_sqlstate(conn, m02.insert_statement(production_tenant[1]), row, "23514")
        m02.reassert_role(conn, "MIG")


def _default_expression(m02: ModuleType, ids: Any, schema: str) -> str:
    (expression,) = m02.admin_one(
        ids,
        """
        SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid)
          FROM pg_catalog.pg_attrdef AS d
          JOIN pg_catalog.pg_attribute AS a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
         WHERE d.adrelid = pg_catalog.to_regclass(%s) AND a.attname = 'created_at'
        """,
        (m02.qualified(schema),),
    )
    return str(expression)


def _statement_time_probe(m02: ModuleType, ids: Any, schema: str) -> tuple[Any, Any, Any]:
    """T04b procedure. Returns (created_at, statement_ts, transaction_ts).

    Bounded, no sleep: statements repeat until statement time has advanced past
    transaction time. Hitting the bound is INCONCLUSIVE, which fails the row; it
    never passes.
    """

    row = m02.seeded_row(f"t04b-{schema}")
    with m02.connect_as(ids, "MIG") as conn:
        try:
            with conn.transaction():
                advanced = False
                for _ in range(T04B_MAX_STATEMENTS):
                    probe = conn.execute(
                        "SELECT transaction_timestamp(), statement_timestamp()"
                    ).fetchone()
                    assert probe is not None
                    if probe[1] > probe[0]:
                        advanced = True
                        break
                if not advanced:
                    pytest.fail("2A-T04b INCONCLUSIVE: statement time never advanced")
                returned = conn.execute(
                    m02.insert_statement(schema)
                    + sql.SQL(
                        " RETURNING created_at, statement_timestamp(), transaction_timestamp()"
                    ),
                    row,
                ).fetchone()
                assert returned is not None
                raise psycopg.Rollback()
        finally:
            m02.reassert_role(conn, "MIG")
    return returned[0], returned[1], returned[2]


def test_2a_t04a_default_is_statement_timestamp(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    assert _default_expression(m02, m02_ids, production_tenant[1]) == "statement_timestamp()"


def test_2a_t04b_created_at_is_the_inserting_statements_time(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    created_at, statement_ts, transaction_ts = _statement_time_probe(
        m02, m02_ids, production_tenant[1]
    )
    assert created_at == statement_ts
    assert created_at != transaction_ts


def test_2a_t04c_transaction_timestamp_mutant_is_distinguished(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """The oracle tells the two defaults apart (T04a and T04b both flip)."""

    m02.provision_sync(m02_ids, m02.mutant_registry("t04c"), m02_tenant)
    schema = m02_tenant[1]
    assert _default_expression(m02, m02_ids, schema) == "transaction_timestamp()"
    created_at, statement_ts, transaction_ts = _statement_time_probe(m02, m02_ids, schema)
    assert created_at == transaction_ts
    assert created_at != statement_ts


def test_2a_t05_table_owner_is_the_migrator(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    assert m02.admin_one(
        m02_ids,
        "SELECT pg_catalog.pg_get_userbyid(relowner) FROM pg_catalog.pg_class "
        "WHERE oid = pg_catalog.to_regclass(%s)",
        (m02.qualified(production_tenant[1]),),
    ) == (m02.MIGRATOR,)


def test_2a_t06_schema_version_is_two(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    assert m02.admin_one(
        m02_ids,
        "SELECT schema_version FROM shared.tenants WHERE tenant_id = %s",
        (production_tenant[0],),
    ) == (2,)


# --- 2A-P01 .. P04 ---------------------------------------------------------


def test_2a_p01_runtime_table_privileges_are_select_only(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    schema = production_tenant[1]
    assert m02.admin_one(
        m02_ids, "SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (m02.qualified(schema),)
    ) == (True,)
    held = {p for p in m02.P8 if m02.table_privilege(m02_ids, m02.RUNTIME, schema, p)}
    assert held == {"SELECT"}


def test_2a_p02_runtime_has_no_column_write_or_reference(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    for privilege in ("INSERT", "UPDATE", "REFERENCES"):
        assert (
            m02.any_column_privilege(m02_ids, m02.RUNTIME, production_tenant[1], privilege) is False
        )


def test_2a_p03_runtime_holds_no_grant_option(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema = production_tenant[1]
    assert m02.table_privilege(m02_ids, m02.RUNTIME, schema, "SELECT WITH GRANT OPTION") is False
    for column, _, _ in m02.EXPECTED_COLUMNS:
        assert (
            m02.column_privilege(m02_ids, m02.RUNTIME, schema, column, "SELECT WITH GRANT OPTION")
            is False
        ), column


def test_2a_p04_runtime_behaviour(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema = production_tenant[1]
    table = sql.Identifier(schema, m02.TABLE)
    with m02.connect_as(m02_ids, "RT") as conn:
        with conn.transaction():
            conn.execute(sql.SQL("SELECT operation_id FROM {}").format(table)).fetchall()
        m02.expect_sqlstate(conn, m02.insert_statement(schema), m02.seeded_row("p04"), "42501")
        m02.reassert_role(conn, "RT")
        m02.expect_sqlstate(
            conn,
            sql.SQL("UPDATE {} SET correlation_id = correlation_id").format(table),
            None,
            "42501",
        )
        m02.reassert_role(conn, "RT")
        m02.expect_sqlstate(
            conn, sql.SQL("SELECT operation_id FROM {} FOR UPDATE").format(table), None, "42501"
        )
        m02.reassert_role(conn, "RT")


# --- 2A-J01 .. J07 ---------------------------------------------------------


def test_2a_j01_exactly_one_enabled_non_internal_trigger(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    rows = m02.admin_all(
        m02_ids,
        "SELECT tgenabled FROM pg_catalog.pg_trigger "
        "WHERE tgrelid = pg_catalog.to_regclass(%s) AND NOT tgisinternal",
        (m02.qualified(production_tenant[1]),),
    )
    assert rows == [("O",)]


def _update(schema: str, assignment: str, where: str = "operation_id = %s") -> sql.Composed:
    return sql.SQL("UPDATE {} SET " + assignment + " WHERE " + where).format(
        sql.Identifier(schema, "operation_registry")
    )


def test_2a_j02_lock_owner_update_is_rejected_and_row_unchanged(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], seeded: dict[str, Any]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema, row = production_tenant[1], seeded["a"]
    before = m02.full_row(m02_ids, schema, row["operation_id"])
    assert before is not None
    new_value = uuid5(NAMESPACE_URL, "haloflow-test:m02:j02:new")
    with m02.connect_as(m02_ids, "LOCK") as conn:
        error = m02.expect_sqlstate(
            conn,
            _update(schema, "correlation_id = %s", "operation_id = %s"),
            (new_value, row["operation_id"]),
            "0A000",
        )
        assert error.diag.message_primary == m02.IMMUTABLE_MESSAGE
        m02.reassert_role(conn, "LOCK")
    assert m02.full_row(m02_ids, schema, row["operation_id"]) == before


def test_2a_j03_lock_owner_select_for_update_succeeds(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], seeded: dict[str, Any]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema, row = production_tenant[1], seeded["a"]
    with m02.connect_as(m02_ids, "LOCK") as conn, conn.transaction():
        locked = conn.execute(
            sql.SQL("SELECT operation_id FROM {} WHERE operation_id = %s FOR UPDATE").format(
                sql.Identifier(schema, m02.TABLE)
            ),
            (row["operation_id"],),
        ).fetchall()
    assert locked == [(row["operation_id"],)]


def test_2a_j04_matching_literal_update_is_rejected(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], seeded: dict[str, Any]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema, row = production_tenant[1], seeded["a"]
    before = m02.full_row(m02_ids, schema, row["operation_id"])
    with m02.connect_as(m02_ids, "LOCK") as conn:
        m02.expect_sqlstate(
            conn,
            _update(schema, "correlation_id = %s"),
            (row["correlation_id"], row["operation_id"]),
            "0A000",
        )
        m02.reassert_role(conn, "LOCK")
    assert m02.full_row(m02_ids, schema, row["operation_id"]) == before


def test_2a_j04b_owner_expression_noop_is_rejected(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], seeded: dict[str, Any]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema, row = production_tenant[1], seeded["a"]
    before = m02.full_row(m02_ids, schema, row["operation_id"])
    with m02.connect_as(m02_ids, "MIG") as conn:
        m02.expect_sqlstate(
            conn,
            _update(schema, "correlation_id = correlation_id"),
            (row["operation_id"],),
            "0A000",
        )
        m02.reassert_role(conn, "MIG")
    assert m02.full_row(m02_ids, schema, row["operation_id"]) == before


def test_2a_j04c_lock_owner_expression_form_is_a_permission_refusal(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], seeded: dict[str, Any]
) -> None:
    """Recorded so the permission check is not mistaken for the trigger."""

    m02.assert_installed(m02_ids, production_tenant[1])
    schema, row = production_tenant[1], seeded["a"]
    with m02.connect_as(m02_ids, "LOCK") as conn:
        m02.expect_sqlstate(
            conn,
            _update(schema, "correlation_id = correlation_id"),
            (row["operation_id"],),
            "42501",
        )
        m02.reassert_role(conn, "LOCK")


def test_2a_j05_zero_row_update_does_not_fire_and_is_not_a_denial(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], seeded: dict[str, Any]
) -> None:
    """CP2-D24: documented as not firing; never recorded as a denial."""

    m02.assert_installed(m02_ids, production_tenant[1])
    schema = production_tenant[1]
    absent = uuid5(NAMESPACE_URL, "haloflow-test:m02:j05:absent")
    before = {k: m02.full_row(m02_ids, schema, r["operation_id"]) for k, r in seeded.items()}
    with m02.connect_as(m02_ids, "LOCK") as conn, conn.transaction():
        cursor = conn.execute(_update(schema, "correlation_id = %s"), (absent, absent))
        assert cursor.rowcount == 0
    after = {k: m02.full_row(m02_ids, schema, r["operation_id"]) for k, r in seeded.items()}
    assert after == before


def test_2a_j06_owner_multi_row_update_is_rejected(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], seeded: dict[str, Any]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema = production_tenant[1]
    ids = [seeded["a"]["operation_id"], seeded["b"]["operation_id"]]
    before = [m02.full_row(m02_ids, schema, i) for i in ids]
    with m02.connect_as(m02_ids, "MIG") as conn:
        m02.expect_sqlstate(
            conn,
            _update(schema, "producer_version = 'changed'", "operation_id = ANY(%s)"),
            (ids,),
            "0A000",
        )
        m02.reassert_role(conn, "MIG")
    assert [m02.full_row(m02_ids, schema, i) for i in ids] == before


def test_2a_j07_function_catalogue(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    schema = production_tenant[1]
    signature = f"{schema}.{m02.REJECTOR_FUNCTION}()"
    prosecdef, proconfig, acl_present = m02.admin_one(
        m02_ids,
        "SELECT prosecdef, proconfig, proacl IS NOT NULL FROM pg_catalog.pg_proc "
        "WHERE oid = pg_catalog.to_regprocedure(%s)",
        (signature,),
    )
    assert prosecdef is False
    assert proconfig == [f"search_path=pg_catalog, {schema}, pg_temp"]
    assert acl_present is True
    acl = m02.admin_all(
        m02_ids,
        """
        SELECT pg_catalog.pg_get_userbyid(e.grantee), e.privilege_type, e.is_grantable,
               pg_catalog.pg_get_userbyid(e.grantor)
          FROM pg_catalog.pg_proc AS p, LATERAL pg_catalog.aclexplode(p.proacl) AS e
         WHERE p.oid = pg_catalog.to_regprocedure(%s)
        """,
        (signature,),
    )
    assert set(acl) == {(m02.MIGRATOR, "EXECUTE", False, m02.MIGRATOR)}
    assert len(acl) == 1


# --- 2A-H02 ----------------------------------------------------------------


def test_2a_h02_installed_body_digest_matches_the_test_literal(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    m02.assert_installed(m02_ids, production_tenant[1])
    (digest,) = m02.admin_one(
        m02_ids,
        "SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(prosrc, 'UTF8')), 'hex') "
        "FROM pg_catalog.pg_proc WHERE oid = pg_catalog.to_regprocedure(%s)",
        (f"{production_tenant[1]}.{m02.REJECTOR_FUNCTION}()",),
    )
    assert digest == m02.REJECTOR_BODY_SHA256
