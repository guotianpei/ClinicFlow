"""F-INT1 Q1-Q5: `INSTALLED_FUNCTION_QUERY` against the REAL catalogue.

Codex ALIGN 20261031T142215Z. The comparator rows in
`test_installed_state_comparator.py` are constructed; these rows observe what the
query itself resolves in PostgreSQL. ADMIN creates probe functions in a dedicated
probe schema and drops it afterwards. The query runs exactly as the runner runs it:
inside a transaction, after `SET LOCAL search_path = pg_catalog, pg_temp`, with
bound parameters `(argument_types, return_type, schema_key, function_name)`.

Column positions: 2 pronargs, 3 argtypes_match, 7 rettype_matches.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.postgres

PROBE_SCHEMA = "m01_installed_state_probe"
PRONARGS, ARGTYPES_MATCH, RETTYPE_MATCHES = 2, 3, 7

_FUNCTIONS = (
    "CREATE FUNCTION {s}.q_uuid(p uuid) RETURNS uuid LANGUAGE plpgsql"
    " AS $$BEGIN RETURN p; END$$",
    "CREATE FUNCTION {s}.q_pair(p uuid, q jsonb) RETURNS jsonb LANGUAGE plpgsql"
    " AS $$BEGIN RETURN q; END$$",
    "CREATE FUNCTION {s}.q_over(p text) RETURNS uuid LANGUAGE plpgsql"
    " AS $$BEGIN RETURN NULL; END$$",
    "CREATE FUNCTION {s}.q_over(p uuid) RETURNS uuid LANGUAGE plpgsql"
    " AS $$BEGIN RETURN p; END$$",
)


@pytest.fixture
def probe(migrated_database: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        drop = sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(PROBE_SCHEMA))
        conn.execute(drop)
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(PROBE_SCHEMA)))
        try:
            for statement in _FUNCTIONS:
                conn.execute(sql.SQL(statement).format(s=sql.Identifier(PROBE_SCHEMA)))
            yield conn
        finally:
            conn.execute(drop)


def observe(
    conn: psycopg.Connection[Any], name: str, argument_types: list[str], return_type: str
) -> list[tuple[Any, ...]]:
    from haloflow.m01.provisioning.installed_state import INSTALLED_FUNCTION_QUERY

    with conn.transaction():
        conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")
        return conn.execute(
            INSTALLED_FUNCTION_QUERY, (argument_types, return_type, PROBE_SCHEMA, name)
        ).fetchall()


def flags(rows: list[tuple[Any, ...]]) -> list[tuple[int, bool, bool]]:
    return [(row[PRONARGS], row[ARGTYPES_MATCH], row[RETTYPE_MATCHES]) for row in rows]


def test_fint1_q1_one_uuid_argument_returning_uuid_matches(
    probe: psycopg.Connection[Any],
) -> None:
    """The oidvector lower-bound regression: a direct `proargtypes::oid[]` comparison
    never equals a 1-based expectation; the re-aggregated comparison must."""

    assert flags(observe(probe, "q_uuid", ["uuid"], "uuid")) == [(1, True, True)]


@pytest.mark.parametrize(
    ("argument_types", "return_type", "expected"),
    [
        (["uuid", "jsonb"], "jsonb", (2, True, True)),
        (["jsonb", "uuid"], "jsonb", (2, False, True)),
        (["uuid"], "jsonb", (2, False, True)),
        (["uuid", "jsonb", "text"], "jsonb", (2, False, True)),
        ([], "jsonb", (2, False, True)),
        (["uuid", "jsonb"], "text", (2, True, False)),
        (["uuid", "json"], "jsonb", (2, False, True)),
    ],
    ids=["exact", "reordered", "shorter", "longer", "empty", "return-text", "json-not-jsonb"],
)
def test_fint1_q2_ordered_exact_argument_and_return_identity(
    probe: psycopg.Connection[Any],
    argument_types: list[str],
    return_type: str,
    expected: tuple[int, bool, bool],
) -> None:
    assert flags(observe(probe, "q_pair", argument_types, return_type)) == [expected]


@pytest.mark.parametrize(
    ("argument_types", "return_type", "expected"),
    [
        (["uuid", "no_such_type"], "jsonb", (2, False, True)),
        (["uuid", "jsonb"], "no_such_type", (2, True, False)),
    ],
    ids=["argument", "return"],
)
def test_fint1_q3_an_unresolvable_type_name_is_false_not_an_error(
    probe: psycopg.Connection[Any],
    argument_types: list[str],
    return_type: str,
    expected: tuple[int, bool, bool],
) -> None:
    """Independent of profile construction, which refuses such names earlier."""

    assert flags(observe(probe, "q_pair", argument_types, return_type)) == [expected]


def test_fint1_q4_overloads_leave_exactly_one_identity_row(
    probe: psycopg.Connection[Any],
) -> None:
    rows = flags(observe(probe, "q_over", ["uuid"], "uuid"))
    assert sorted(rows) == [(1, False, True), (1, True, True)]
    assert [row for row in rows if row[1]] == [(1, True, True)]


def test_fint1_q5_a_temp_type_shadow_does_not_change_resolution(
    probe: psycopg.Connection[Any],
) -> None:
    probe.execute("CREATE TYPE pg_temp.jsonb AS (x int)")
    try:
        # Positive control: the shadow is real, and wins on a temp-first path.
        with probe.transaction():
            probe.execute("SET LOCAL search_path = pg_temp, pg_catalog")
            shadowed = probe.execute(
                "SELECT pg_catalog.to_regtype('jsonb') <> 'pg_catalog.jsonb'::pg_catalog.regtype"
            ).fetchone()
        assert shadowed == (True,)
        # Under the runner's pinned pg_catalog-first path it does not.
        assert flags(observe(probe, "q_pair", ["uuid", "jsonb"], "jsonb")) == [(2, True, True)]
    finally:
        probe.execute("DROP TYPE pg_temp.jsonb")
