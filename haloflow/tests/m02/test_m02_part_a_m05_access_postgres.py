"""UC1 part A — the M05 owner's bounded tenant access (amendment A4), PostgreSQL 17.

Codex ALIGN 20261031T132829Z: the M05 owner gets tenant-schema USAGE only (an
execution-role profile with ["USAGE"]), so it can call the one function granted to
it, `m02_get_observation_envelope`, and nothing else. This module measures both
sides on a production-provisioned tenant:
- allowed: schema USAGE, and an actual envelope call that completes;
- denied: schema CREATE, every privilege on every table and sequence of the tenant
  schema, EXECUTE on every other function there (append, enqueue, canon, the lock
  gateway), any grant option, and ownership of anything.
Expected values come from the amendment, never from the catalogue under test.
"""

import uuid
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.postgres

M05 = "haloflow_m05_owner"
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _admin(m02_ids: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(m02_ids.admin, autocommit=True)


def test_a4_m05_owner_has_schema_usage_only(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        usage, create = conn.execute(
            "SELECT has_schema_privilege(%s, %s, 'USAGE'), has_schema_privilege(%s, %s, 'CREATE')",
            (M05, schema, M05, schema),
        ).fetchone()  # type: ignore[misc]
        acl = conn.execute(
            "SELECT a.privilege_type, a.is_grantable"
            " FROM pg_catalog.pg_namespace AS n,"
            " LATERAL pg_catalog.aclexplode(n.nspacl) AS a"
            " WHERE n.nspname = %s AND a.grantee = %s::regrole",
            (schema, M05),
        ).fetchall()
    assert (usage, create) == (True, False)
    assert acl == [("USAGE", False)]


def test_a4_m05_owner_has_no_table_or_sequence_privilege(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        relations = conn.execute(
            "SELECT c.oid::regclass::text, c.relkind FROM pg_catalog.pg_class AS c"
            " WHERE c.relnamespace = %s::regnamespace AND c.relkind IN ('r', 'p', 'v', 'S')",
            (schema,),
        ).fetchall()
        assert any(name.endswith(".patient_events") for name, _ in relations)
        for name, kind in relations:
            privileges = ("SELECT", "USAGE", "UPDATE") if kind == "S" else TABLE_PRIVILEGES
            for privilege in privileges:
                check = "has_sequence_privilege" if kind == "S" else "has_table_privilege"
                held = conn.execute(
                    sql.SQL("SELECT {}(%s, %s, %s)").format(sql.Identifier(check)),
                    (M05, name, privilege),
                ).fetchone()
                assert held == (False,), (name, privilege)
        owned = conn.execute(
            "SELECT count(*) FROM pg_catalog.pg_class WHERE relowner = %s::regrole", (M05,)
        ).fetchone()
    assert owned == (0,)


def test_a4_m05_owner_executes_only_the_envelope(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        functions = conn.execute(
            "SELECT p.oid::regprocedure::text FROM pg_catalog.pg_proc AS p"
            " WHERE p.pronamespace = %s::regnamespace",
            (schema,),
        ).fetchall()
        executable = sorted(
            name
            for (name,) in functions
            if conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (M05, name)
            ).fetchone()
            == (True,)
        )
        grantable = conn.execute(
            "SELECT count(*) FROM pg_catalog.pg_proc AS p,"
            " LATERAL pg_catalog.aclexplode(p.proacl) AS a"
            " WHERE p.pronamespace = %s::regnamespace AND a.grantee = %s::regrole"
            " AND a.is_grantable",
            (schema, M05),
        ).fetchone()
    assert executable == [f"{schema}.m02_get_observation_envelope(uuid)"]
    assert grantable == (0,)


def test_a4_m05_owner_actual_envelope_call_and_create_denied(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    tenant_id, schema = production_tenant
    ident = sql.Identifier(schema)
    with psycopg.connect(m02_ids.logins["haloflow_migrator"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_migrator")
        conn.execute("SET ROLE haloflow_m05_owner")
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            row = conn.execute(
                sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(ident),
                (str(uuid.uuid4()),),
            ).fetchone()
        assert row == (None,)  # the call itself completes; an unknown id is null
        for statement in (
            sql.SQL("CREATE TABLE {}.m05_probe (x int)").format(ident),
            sql.SQL("SELECT event_id FROM {}.patient_events LIMIT 1").format(ident),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(statement)
