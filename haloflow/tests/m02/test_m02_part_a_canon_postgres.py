"""UC1 part A — SQL half of m02.canon/v1 (PA-42 / PA-43 SQL layer), PostgreSQL 17.

`m02_canon_v1(jsonb) -> bytea` is called by each AUTHORIZED owner role (append owner
and projection owner, through the migrator's SET-only memberships) on the frozen
vectors and must return exactly the independently authored bytes (part_a
CANON_VECTORS / TYPE_VECTORS). SQL-layer rejects: out-of-range integers and any
non-integral numeric, including 1.0 (JSONB keeps the scale). Raw lexical cases
(duplicate keys, -0) are a service raw-input concern (PA-43 service rows), and an
unpaired surrogate or U+0000 cannot reach the function because PostgreSQL JSONB
input refuses them; those rows assert that refusal happens before the call.
"""

from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres

_ROLES = ["haloflow_m02_append_owner", "haloflow_m02_projection_owner"]


def _canon(m02_ids: Any, schema: str, role: str, value: Any) -> bytes:
    with psycopg.connect(m02_ids.logins["haloflow_migrator"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_migrator")
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        row = conn.execute(
            sql.SQL("SELECT {}.m02_canon_v1(%s)").format(sql.Identifier(schema)), (Jsonb(value),)
        ).fetchone()
    assert row is not None
    return bytes(row[0])


def _installed(part_a: ModuleType, m02_ids: Any, schema: str) -> None:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(
            "SELECT to_regprocedure(%s) IS NOT NULL", (f"{schema}.m02_canon_v1(jsonb)",)
        ).fetchone()
    assert row == (True,), f"{schema}.m02_canon_v1(jsonb) is not installed"


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("name", ["CV1", "CV2", "CV3", "CV4", "CT1", "CT2"])
def test_pa42_sql_canon_bytes_equal_frozen_vectors(
    part_a: ModuleType,
    m02_ids: Any,
    production_tenant: tuple[str, str],
    role: str,
    name: str,
) -> None:
    _installed(part_a, m02_ids, production_tenant[1])
    vector = next(v for v in (*part_a.CANON_VECTORS, *part_a.TYPE_VECTORS) if v.name == name)
    data = _canon(m02_ids, production_tenant[1], role, vector.logical)
    assert data.hex() == vector.hex
    assert part_a.sha256_hex(data) == vector.sha256


def test_pa44_sql_canon_not_codepoint_ordered(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    _installed(part_a, m02_ids, production_tenant[1])
    cv1 = next(v for v in part_a.CANON_VECTORS if v.name == "CV1")
    data = _canon(m02_ids, production_tenant[1], _ROLES[0], cv1.logical)
    assert data.hex() != part_a.CV0_CODEPOINT_ORDER_HEX


@pytest.mark.parametrize(
    ("label", "literal"),
    [
        ("int_above", '{"x": 9007199254740992}'),
        ("int_below", '{"x": -9007199254740992}'),
        ("fraction", '{"x": 1.5}'),
        ("integral_fraction", '{"x": 1.0}'),
    ],
)
def test_pa43_sql_canon_rejects_numeric_domain(
    part_a: ModuleType,
    m02_ids: Any,
    production_tenant: tuple[str, str],
    label: str,
    literal: str,
) -> None:
    schema = production_tenant[1]
    _installed(part_a, m02_ids, schema)
    with psycopg.connect(m02_ids.logins["haloflow_migrator"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_migrator")
        conn.execute("SET ROLE haloflow_m02_append_owner")
        with pytest.raises(psycopg.Error) as caught, conn.transaction():
            conn.execute(
                sql.SQL("SELECT {}.m02_canon_v1(%s::jsonb)").format(sql.Identifier(schema)),
                (literal,),
            )
    assert caught.value.sqlstate == part_a.REFUSAL_SQLSTATE, label
    assert caught.value.diag.message_primary == "COMMAND_MALFORMED", label


@pytest.mark.parametrize(
    "literal", ['{"x": "' + chr(92) + 'ud800"}', '{"x": "' + chr(92) + 'u0000"}']
)
def test_pa43_unrepresentable_strings_refused_by_jsonb_before_the_call(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], literal: str
) -> None:
    _installed(part_a, m02_ids, production_tenant[1])
    with (
        psycopg.connect(m02_ids.admin, autocommit=True) as conn,
        pytest.raises(psycopg.errors.DataError),
        conn.transaction(),
    ):
        conn.execute("SELECT %s::jsonb", (literal,))


def test_pa43_exponent_is_normalized_by_jsonb_so_rejection_is_raw_layer(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """Codex b2c1ff51: JSONB normalizes 1e2 to the integer 100 (scale 0), so a jsonb
    function cannot tell it from a permitted 100. Exponent rejection belongs to the
    raw-input layer (test_pa43_service_raw_input_rejects[exponent]); at SQL both
    inputs canonicalize to the same bytes."""

    schema = production_tenant[1]
    _installed(part_a, m02_ids, schema)
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(
            "SELECT ('{\"x\":1e2}'::jsonb)::text, ('{\"x\":100}'::jsonb)::text,"
            " scale(('{\"x\":1e2}'::jsonb->>'x')::numeric)"
        ).fetchone()
    assert row == ('{"x": 100}', '{"x": 100}', 0)
    expected = "7b2278223a3130307d"  # {"x":100}
    with psycopg.connect(m02_ids.logins["haloflow_migrator"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_migrator")
        conn.execute("SET ROLE haloflow_m02_append_owner")
        for literal in ('{"x": 1e2}', '{"x": 100}'):
            got = conn.execute(
                sql.SQL("SELECT {}.m02_canon_v1(%s::jsonb)").format(sql.Identifier(schema)),
                (literal,),
            ).fetchone()
            assert got is not None and bytes(got[0]).hex() == expected, literal
