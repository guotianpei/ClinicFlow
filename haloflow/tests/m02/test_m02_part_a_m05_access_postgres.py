"""The M05 owner's tenant access on a PRODUCTION tenant (UC1 part B, B5), PostgreSQL 17.

Rebaselined in B5 per Codex ruling 2812e580 (option (a): an explicit rebaseline, not a
retirement). Part A amendment A4 (Codex ALIGN 20261031T132829Z) gave the M05 owner
tenant-schema USAGE only, no ownership and EXECUTE on the envelope alone. B5 composed
M05 (t012..t020) and the staff directory (t021..t025) into production (target 25), so
Part A's historical A4 semantics are SUPERSEDED for production tenants: the M05 owner
now owns the seven M05 gateways and holds the reviewed B4 table/column grants. Part A's
own coverage (explicit Part A registry) lives elsewhere and is unchanged.

Old -> new node identities (same module):
- test_a4_m05_owner_has_schema_usage_only
    -> test_b5_m05_owner_schema_usage_and_create_per_profile
- test_a4_m05_owner_has_no_table_or_sequence_privilege
    -> test_b5_m05_owner_exact_table_and_column_acl
- test_a4_m05_owner_executes_only_the_envelope
    -> test_b5_m05_owner_exact_ownership_and_effective_execute
- test_a4_m05_owner_actual_envelope_call_and_create_denied
    -> test_b5_m05_owner_envelope_call_works_direct_event_read_denied

Semantic expectations (every set below is an independent literal, never read from src
or from the catalogue under test; each comparison is EXACT over an exhaustive catalogue
query, so an unexpected object or privilege fails):
- schema: USAGE and CREATE, both non-grantable, granted by the provisioner. CREATE is
  the approved B5 schema profile (manifest execution-role profile ["USAGE", "CREATE"]),
  an intentional allowance that supersedes A4's CREATE denial; it is schema-wide
  CREATE, not a privilege restricted to creating functions.
- tables: one table-level grant (history INSERT) plus the B4 column grants; every other
  table and sequence privilege in the tenant schema is denied.
- ownership: exactly the seven M05 functions, and no relation or type.
- functions: the owner's own ACL entries on its seven functions are inherent owner
  authority (implicit grant option included); the explicit grants FROM OTHER ROLES are
  exactly envelope EXECUTE (read owner) and m01_staff_assignable EXECUTE (staff owner),
  neither grantable. Effective EXECUTE is exactly those nine functions.
- behaviour: an actual envelope call completes; a direct patient_events read is denied.
"""

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.postgres

M05 = "haloflow_m05_owner"
MIGRATOR = "haloflow_migrator"
PROVISIONER = "haloflow_provisioner"
READ_OWNER = "haloflow_m02_read_owner"
STAFF_OWNER = "haloflow_staff_owner"

TABLE_PRIVILEGES = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN",
)
SEQUENCE_PRIVILEGES = ("SELECT", "USAGE", "UPDATE")
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")

# Schema ACL: manifest execution-role profile (src/haloflow/m01/manifests/provisioning.json
# "haloflow_m05_owner".tenant_schema_privileges) expanded non-grantable with grantor the
# provisioner (src/haloflow/m01/provisioning/manifest.py, execution-role entries).
SCHEMA_ACL = {("CREATE", False, PROVISIONER), ("USAGE", False, PROVISIONER)}

# Column names (tests/m05/m05_support.py COLUMNS / OUTCOME_CODE_COLUMNS, package v1 2.1).
ITEM_COLUMNS = (
    "item_id", "source_event_id", "item_kind", "patient_id", "state", "assignee_id",
    "outcome_code", "version", "create_digest", "created_at",
)
HISTORY_COLUMNS = (
    "history_id", "item_id", "seq", "action", "actor_id", "before", "after",
    "resolution_event_id", "assertion_kind", "created_at",
)
ACTION_COLUMNS = (
    "item_id", "client_action_id", "actor_id", "requested_action", "digest",
    "hmac_key_version", "event_id", "result_snapshot", "created_at",
)
NOTE_COLUMNS = (
    "note_id", "item_id", "client_action_id", "nonce", "ciphertext", "enc_key_version",
    "created_at",
)
INTAKE_COLUMNS = (
    "source_event_type", "source_contract_version", "discriminator_path",
    "discriminator_value", "item_kind", "producer_workload_id", "active_in_production",
)

# Explicit relacl / attacl entries to the M05 owner: the reviewed B4 sets
# (tests/m05/m05_support.py B4_TABLE_ACL / B4_COLUMN_ACL; src/haloflow/m05/units.py
# t012:step:grants). The tables are migrator-owned, so the migrator is the grantor.
TABLE_ACL = {("m05_followup_history", "INSERT")}
COLUMN_ACL: dict[str, dict[str, tuple[str, ...]]] = {
    "m05_outcome_codes": {"SELECT": ("outcome_code", "note_required")},
    "m05_intake_map": {"SELECT": INTAKE_COLUMNS},
    "m05_followup_items": {
        "INSERT": ITEM_COLUMNS,
        "SELECT": ITEM_COLUMNS,
        "UPDATE": ("state", "assignee_id", "version", "outcome_code"),
    },
    "m05_followup_history": {"SELECT": HISTORY_COLUMNS},
    "m05_followup_actions": {
        "INSERT": ACTION_COLUMNS,
        "SELECT": (
            "item_id", "client_action_id", "actor_id", "digest", "result_snapshot",
            "event_id", "hmac_key_version",
        ),
    },
    "m05_followup_notes": {"INSERT": NOTE_COLUMNS, "SELECT": NOTE_COLUMNS},
}

# The seven M05 gateways, owned by the M05 owner (tests/m05/m05_support.py FUNCTIONS /
# M05_TYPED_UNITS; src/haloflow/m05/gateways.py typed units t014..t020).
OWNED_FUNCTIONS = (
    "m05_create_item(jsonb)",
    "m05_prepare_action(jsonb)",
    "m05_apply_action(jsonb)",
    "m05_get_item(uuid)",
    "m05_list_items(jsonb)",
    "m05_get_history(uuid)",
    "m05_read_note(uuid)",
)
# Explicit EXECUTE grants FROM OTHER ROLES (t011 envelope: src/haloflow/m02/
# part_a_gateways.py T011 grantee; t022 assignability: src/haloflow/m01/staff/units.py
# _FUNCTIONS grantee), as (function, privilege, grantor, grantable).
GRANTED_FUNCTIONS = {
    ("m02_get_observation_envelope(uuid)", "EXECUTE", READ_OWNER, False),
    ("m01_staff_assignable(uuid)", "EXECUTE", STAFF_OWNER, False),
}

_FUNCTION_KEY = "p.proname || '(' || pg_catalog.oidvectortypes(p.proargtypes) || ')'"


def _admin(m02_ids: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(m02_ids.admin, autocommit=True)


def test_b5_m05_owner_schema_usage_and_create_per_profile(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """USAGE and CREATE per the approved B5 schema profile; supersedes A4's CREATE denial."""

    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        held = conn.execute(
            "SELECT pg_catalog.has_schema_privilege(%s, %s, 'USAGE'),"
            " pg_catalog.has_schema_privilege(%s, %s, 'CREATE'),"
            " pg_catalog.has_schema_privilege(%s, %s, 'USAGE WITH GRANT OPTION'),"
            " pg_catalog.has_schema_privilege(%s, %s, 'CREATE WITH GRANT OPTION')",
            (M05, schema) * 4,
        ).fetchone()
        acl = conn.execute(
            "SELECT a.privilege_type, a.is_grantable, pg_catalog.pg_get_userbyid(a.grantor)"
            " FROM pg_catalog.pg_namespace AS n,"
            " LATERAL pg_catalog.aclexplode(n.nspacl) AS a"
            " WHERE n.nspname = %s AND a.grantee = %s::regrole",
            (schema, M05),
        ).fetchall()
    assert held == (True, True, False, False)
    assert len(acl) == len(SCHEMA_ACL)
    assert set(acl) == SCHEMA_ACL


def test_b5_m05_owner_exact_table_and_column_acl(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """Exact explicit table/column ACL; exact effective privileges on every relation."""

    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        table_acl = conn.execute(
            "SELECT c.relname, a.privilege_type, pg_catalog.pg_get_userbyid(a.grantor),"
            " a.is_grantable FROM pg_catalog.pg_class AS c,"
            " LATERAL pg_catalog.aclexplode(c.relacl) AS a"
            " WHERE c.relnamespace = %s::regnamespace AND a.grantee = %s::regrole",
            (schema, M05),
        ).fetchall()
        column_acl = conn.execute(
            "SELECT c.relname, att.attname, a.privilege_type,"
            " pg_catalog.pg_get_userbyid(a.grantor), a.is_grantable"
            " FROM pg_catalog.pg_class AS c"
            " JOIN pg_catalog.pg_attribute AS att ON att.attrelid = c.oid,"
            " LATERAL pg_catalog.aclexplode(att.attacl) AS a"
            " WHERE c.relnamespace = %s::regnamespace AND a.grantee = %s::regrole",
            (schema, M05),
        ).fetchall()
        relations = conn.execute(
            "SELECT c.relname, c.relkind FROM pg_catalog.pg_class AS c"
            " WHERE c.relnamespace = %s::regnamespace"
            " AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')",
            (schema,),
        ).fetchall()
        effective_table = conn.execute(
            "SELECT c.relname, p.privilege FROM pg_catalog.pg_class AS c"
            " CROSS JOIN pg_catalog.unnest(%s::text[]) AS p(privilege)"
            " WHERE c.relnamespace = %s::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
            " AND pg_catalog.has_table_privilege(%s, c.oid, p.privilege)",
            (list(TABLE_PRIVILEGES), schema, M05),
        ).fetchall()
        effective_sequence = conn.execute(
            "SELECT c.relname, p.privilege FROM pg_catalog.pg_class AS c"
            " CROSS JOIN pg_catalog.unnest(%s::text[]) AS p(privilege)"
            " WHERE c.relnamespace = %s::regnamespace AND c.relkind = 'S'"
            " AND pg_catalog.has_sequence_privilege(%s, c.oid, p.privilege)",
            (list(SEQUENCE_PRIVILEGES), schema, M05),
        ).fetchall()
        grant_option = conn.execute(
            "SELECT pg_catalog.has_table_privilege(%s, %s, 'INSERT WITH GRANT OPTION')",
            (M05, f"{schema}.m05_followup_history"),
        ).fetchone()
        effective_column = conn.execute(
            "SELECT c.relname, att.attname, p.privilege FROM pg_catalog.pg_class AS c"
            " JOIN pg_catalog.pg_attribute AS att ON att.attrelid = c.oid"
            " CROSS JOIN pg_catalog.unnest(%s::text[]) AS p(privilege)"
            " WHERE c.relnamespace = %s::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
            " AND att.attnum > 0 AND NOT att.attisdropped"
            " AND pg_catalog.has_column_privilege(%s, c.oid, att.attnum, p.privilege)",
            (list(COLUMN_PRIVILEGES), schema, M05),
        ).fetchall()

    expected_columns = {
        (table, column, privilege, MIGRATOR, False)
        for table, by_privilege in COLUMN_ACL.items()
        for privilege, columns in by_privilege.items()
        for column in columns
    }
    assert sorted(table_acl) == sorted(
        (table, privilege, MIGRATOR, False) for table, privilege in TABLE_ACL
    )
    assert len(column_acl) == len(expected_columns)
    assert set(column_acl) == expected_columns

    # The catalogue walk is real (patient_events is in scope), and every table and
    # sequence privilege other than history INSERT is denied.
    assert ("patient_events", "r") in relations
    assert sorted(effective_table) == sorted(TABLE_ACL)
    assert effective_sequence == []
    assert grant_option == (False,)

    # Effective column privileges: the explicit column grants, plus every history
    # column for INSERT through the table-level grant. Nothing else in the schema.
    expected_effective = {
        (table, column, privilege) for table, column, privilege, _, _ in expected_columns
    } | {("m05_followup_history", column, "INSERT") for column in HISTORY_COLUMNS}
    assert len(effective_column) == len(expected_effective)
    assert set(effective_column) == expected_effective


def test_b5_m05_owner_exact_ownership_and_effective_execute(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """Owns exactly the seven M05 functions; explicit grants from others are exactly the
    envelope and the assignability helper; effective EXECUTE is exactly those nine."""

    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        owned_functions = conn.execute(
            f"SELECT {_FUNCTION_KEY} FROM pg_catalog.pg_proc AS p"
            " WHERE p.pronamespace = %s::regnamespace AND p.proowner = %s::regrole",
            (schema, M05),
        ).fetchall()
        owned_relations = conn.execute(
            "SELECT c.relname FROM pg_catalog.pg_class AS c"
            " WHERE c.relnamespace = %s::regnamespace AND c.relowner = %s::regrole",
            (schema, M05),
        ).fetchall()
        owned_types = conn.execute(
            "SELECT t.typname FROM pg_catalog.pg_type AS t"
            " WHERE t.typnamespace = %s::regnamespace AND t.typowner = %s::regrole",
            (schema, M05),
        ).fetchall()
        owned_schemas = conn.execute(
            "SELECT n.nspname FROM pg_catalog.pg_namespace AS n WHERE n.nspowner = %s::regrole",
            (M05,),
        ).fetchall()
        function_acl = conn.execute(
            f"SELECT {_FUNCTION_KEY}, a.privilege_type, pg_catalog.pg_get_userbyid(a.grantor),"
            " a.is_grantable FROM pg_catalog.pg_proc AS p,"
            " LATERAL pg_catalog.aclexplode(p.proacl) AS a"
            " WHERE p.pronamespace = %s::regnamespace AND a.grantee = %s::regrole",
            (schema, M05),
        ).fetchall()
        executable = conn.execute(
            f"SELECT {_FUNCTION_KEY} FROM pg_catalog.pg_proc AS p"
            " WHERE p.pronamespace = %s::regnamespace"
            " AND pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE')",
            (schema, M05),
        ).fetchall()
        with_grant_option = conn.execute(
            f"SELECT {_FUNCTION_KEY} FROM pg_catalog.pg_proc AS p"
            " WHERE p.pronamespace = %s::regnamespace"
            " AND pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE WITH GRANT OPTION')",
            (schema, M05),
        ).fetchall()

    # Ownership: the seven gateways, and no relation, type or schema.
    assert sorted(name for (name,) in owned_functions) == sorted(OWNED_FUNCTIONS)
    assert owned_relations == []
    assert owned_types == []
    assert owned_schemas == []

    # proacl entries naming the M05 owner. Its own entries on the functions it owns
    # (grantor = grantee) record inherent owner authority; the explicit grants from
    # other roles are exactly the two below. No entry is grantable.
    self_entries = {row for row in function_acl if row[2] == M05}
    granted = {row for row in function_acl if row[2] != M05}
    assert len(function_acl) == len(OWNED_FUNCTIONS) + len(GRANTED_FUNCTIONS)
    assert self_entries == {(name, "EXECUTE", M05, False) for name in OWNED_FUNCTIONS}
    assert granted == GRANTED_FUNCTIONS

    # Effective EXECUTE over every function in the schema: owned plus granted.
    assert sorted(name for (name,) in executable) == sorted(
        (*OWNED_FUNCTIONS, *(name for name, _, _, _ in GRANTED_FUNCTIONS))
    )
    # The grant option is the owner's inherent one only: none on the granted pair.
    assert sorted(name for (name,) in with_grant_option) == sorted(OWNED_FUNCTIONS)


def test_b5_m05_owner_envelope_call_works_direct_event_read_denied(
    m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """As the M05 owner (SET ROLE through the migrator's edge): the envelope call
    completes and a direct patient_events read is refused."""

    tenant_id, schema = production_tenant
    ident = sql.Identifier(schema)
    with psycopg.connect(m02_ids.logins["haloflow_migrator"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_migrator")
        conn.execute("SET ROLE haloflow_m05_owner")
        assert conn.execute("SELECT current_user").fetchone() == (M05,)
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            row = conn.execute(
                sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(ident),
                (str(uuid.uuid4()),),
            ).fetchone()
        assert row == (None,)  # the call itself completes; an unknown id is null
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                sql.SQL("SELECT event_id FROM {}.patient_events LIMIT 1").format(ident)
            )
