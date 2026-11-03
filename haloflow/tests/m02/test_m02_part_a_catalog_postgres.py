"""UC1 part A — catalogue rows (PostgreSQL 17).

Rows: PA-14 (function ownership, definer, search_path, EXECUTE ACL), PA-15 /
PA-28 / PA-47 (exact column grants), PA-27 (roles and memberships), PA-39 /
PA-41 (shared registry grants issued by revision 005), plus the frozen
physical shape of the reference tables and the two contract extension columns
(Codex 95ae5bfd: names frozen in the manifest, not inferred from t004).

Every assertion reads the catalogue as ADMIN. At the pre-change baseline the
roles, tables and functions do not exist, so each row fails inside its body on
its first existence assertion (class B where the row's own assertion is the
absence; never a fixture ERROR).
"""

import secrets
from collections.abc import Callable
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

pytestmark = pytest.mark.postgres


def _admin(m02_ids: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(m02_ids.admin, autocommit=True)


# --- PA-27: roles and memberships ------------------------------------------


@pytest.mark.parametrize(
    "role",
    [
        "haloflow_m02_append_owner",
        "haloflow_m02_projection_owner",
        "haloflow_m02_read_owner",
        "haloflow_m05_owner",
    ],
)
def test_pa27_role_is_nologin_with_exact_migrator_edge(
    part_a: ModuleType, m02_ids: Any, role: str
) -> None:
    assert role in part_a.PART_A_ROLES
    with _admin(m02_ids) as conn:
        assert part_a.role_exists(conn, role), f"{role} does not exist"
        row = conn.execute(
            # D1 (Codex 20261031T015048Z): rolconfig lives on pg_roles, not pg_authid.
            "SELECT a.rolcanlogin, a.rolsuper, a.rolcreatedb, a.rolcreaterole,"
            " a.rolreplication, a.rolbypassrls, a.rolpassword IS NULL, r.rolconfig IS NULL"
            " FROM pg_authid AS a JOIN pg_roles AS r ON r.oid = a.oid WHERE a.rolname = %s",
            (role,),
        ).fetchone()
        assert row == (False, False, False, False, False, False, True, True)
        edges = conn.execute(
            "SELECT m.rolname, a.inherit_option, a.set_option, a.admin_option"
            " FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid"
            " JOIN pg_roles m ON m.oid = a.member WHERE r.rolname = %s",
            (role,),
        ).fetchall()
        expected = part_a.MEMBERSHIP
        assert edges == [(part_a.MIGRATOR, expected["inherit"], expected["set"], expected["admin"])]


def test_pa27_m05_owner_owns_nothing_in_part_a(part_a: ModuleType, m02_ids: Any) -> None:
    with _admin(m02_ids) as conn:
        assert part_a.role_exists(conn, part_a.M05_OWNER), "haloflow_m05_owner does not exist"
        owned = conn.execute(
            "SELECT count(*) FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid"
            " WHERE r.rolname = %s AND d.deptype = 'o'",
            (part_a.M05_OWNER,),
        ).fetchone()
        assert owned == (0,)


# --- PA-39 / PA-41: shared registry grants ----------------------------------


def test_pa39_projection_owner_shared_tenants_exact_columns(
    part_a: ModuleType, m02_ids: Any
) -> None:
    with _admin(m02_ids) as conn:
        assert part_a.role_exists(conn, part_a.PROJECTION_OWNER), "projection owner missing"
        for column in part_a.SHARED_TENANTS_COLUMNS_ALL:
            granted = column in part_a.SHARED_TENANTS_GRANTED
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                expected = granted and privilege == "SELECT"
                assert (
                    part_a.column_privilege(
                        conn, part_a.PROJECTION_OWNER, "shared.tenants", column, privilege
                    )
                    is expected
                ), (column, privilege)
            assert (
                part_a.column_privilege(
                    conn,
                    part_a.PROJECTION_OWNER,
                    "shared.tenants",
                    column,
                    "SELECT WITH GRANT OPTION",
                )
                is False
            ), column
        schema = conn.execute(
            "SELECT has_schema_privilege(%s, 'shared', 'USAGE'),"
            " has_schema_privilege(%s, 'shared', 'CREATE')",
            (part_a.PROJECTION_OWNER, part_a.PROJECTION_OWNER),
        ).fetchone()
        assert schema == (True, False)
        # No privilege on any other shared table (O11).
        others = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'shared' AND c.relkind IN ('r','v','m','p')"
            " AND c.relname <> 'tenants'"
            " AND (has_table_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
            "REFERENCES,TRIGGER')"
            "  OR has_any_column_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,REFERENCES'))",
            (part_a.PROJECTION_OWNER, part_a.PROJECTION_OWNER),
        ).fetchall()
        assert others == []


@pytest.mark.parametrize(
    "role",
    ["haloflow_m02_append_owner", "haloflow_m02_read_owner", "haloflow_m05_owner"],
)
def test_pa39_other_part_a_roles_have_no_shared_access(
    part_a: ModuleType, m02_ids: Any, role: str
) -> None:
    with _admin(m02_ids) as conn:
        assert part_a.role_exists(conn, role), f"{role} does not exist"
        row = conn.execute(
            "SELECT has_schema_privilege(%s, 'shared', 'USAGE'),"
            " has_any_column_privilege(%s, 'shared.tenants', 'SELECT,INSERT,UPDATE,REFERENCES')",
            (role, role),
        ).fetchone()
        assert row == (False, False)


def test_pa41_shared_grants_grantor_is_registry_owner(part_a: ModuleType, m02_ids: Any) -> None:
    """Catalogue proof of origin: the shared.tenants column grants to the
    projection owner were made by the table's owner (the owner-run Alembic
    identity), not by the migrator that runs tenant units."""

    with _admin(m02_ids) as conn:
        assert part_a.role_exists(conn, part_a.PROJECTION_OWNER), "projection owner missing"
        rows = conn.execute(
            "SELECT a.attname, g.rolname, o.rolname"
            " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_roles o ON o.oid = c.relowner,"
            " LATERAL aclexplode(a.attacl) x JOIN pg_roles g ON g.oid = x.grantor"
            " JOIN pg_roles e ON e.oid = x.grantee"
            " WHERE c.oid = 'shared.tenants'::regclass AND e.rolname = %s",
            (part_a.PROJECTION_OWNER,),
        ).fetchall()
        assert sorted(r[0] for r in rows) == sorted(part_a.SHARED_TENANTS_GRANTED)
        for column, grantor, table_owner in rows:
            assert grantor == table_owner, (column, grantor)
            assert grantor != part_a.MIGRATOR, column


def _shared_grant_state(conninfo: str, role: str) -> tuple[Any, ...]:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        row = conn.execute(
            "SELECT has_schema_privilege(%s, 'shared', 'USAGE'),"
            " has_any_column_privilege(%s, 'shared.tenants', 'SELECT'),"
            " (SELECT count(*) FROM shared.tenants),"
            " (SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'tenant\\_%%')",
            (role, role),
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_pa41_staged_fresh_database_grants_before_any_tenant(
    part_a: ModuleType,
    m02: ModuleType,
    m02_ids: Any,
    database_url: Callable[[dict[str, object], str], str],
    apply_migrations: Callable[..., None],
) -> None:
    """Codex ee98edee: a fresh, isolated database migrated in stages. At 004 the
    projection owner has no shared access; at 005, with zero tenants and no
    tenant schema provisioned, the grants exist. Origin is the shared revision."""

    dbname = f"haloflow_test_pa41_{secrets.token_hex(4)}"
    admin_params: dict[str, object] = dict(conninfo_to_dict(m02_ids.admin))
    created = False
    try:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
            created = True
        apply_migrations(database_url(admin_params, dbname), "004")
        fresh = make_conninfo(m02_ids.admin, dbname=dbname)
        with psycopg.connect(fresh, autocommit=True) as conn:
            exists = part_a.role_exists(conn, part_a.PROJECTION_OWNER)
        if exists:
            assert _shared_grant_state(fresh, part_a.PROJECTION_OWNER)[:2] == (False, False)
        apply_migrations(database_url(admin_params, dbname), "005")
        with psycopg.connect(fresh, autocommit=True) as conn:
            assert part_a.role_exists(conn, part_a.PROJECTION_OWNER), "projection owner missing"
        assert _shared_grant_state(fresh, part_a.PROJECTION_OWNER) == (True, True, 0, 0)
    finally:
        if created:
            with m02.connect_admin(m02_ids) as conn:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(dbname)
                    )
                )


def test_pa41_supplementary_revision_text(part_a: ModuleType) -> None:
    """Supplementary only (Codex 2a872210 point 4): the revision text names the
    grants and no M02 unit source does. Not proof of runtime origin."""

    from pathlib import Path

    revision = Path("alembic/versions") / f"{part_a.REVISION_005}.py"
    assert revision.is_file(), f"{revision} does not exist"
    text = revision.read_text(encoding="utf-8")
    assert f"GRANT USAGE ON SCHEMA shared TO {part_a.PROJECTION_OWNER}" in text
    assert (
        "GRANT SELECT (tenant_id, schema_key, lifecycle_state, schema_version)"
        f" ON shared.tenants TO {part_a.PROJECTION_OWNER}"
    ) in text
    units = Path("src/haloflow/m02")
    for path in sorted(units.rglob("*.py")):
        assert "shared.tenants TO" not in path.read_text(encoding="utf-8"), path


# --- PA-14: functions -------------------------------------------------------


def _function_row(conn: Any, schema: str, name: str, args: str) -> tuple[Any, ...] | None:
    """Resolve by type identity (to_regprocedure), never by argument-name text."""

    row = conn.execute(
        "SELECT r.rolname, p.prosecdef, p.proconfig, p.proacl::text[]"
        " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
        " WHERE p.oid = to_regprocedure(%s)",
        (f"{schema}.{name}({args})",),
    ).fetchone()
    return None if row is None else tuple(row)


@pytest.mark.parametrize(
    "name",
    [
        "m02_append_observation",
        "m02_enqueue_event_handoffs",
        "m02_get_observation_envelope",
        "m02_canon_v1",
    ],
)
def test_pa14_function_owner_definer_path_acl(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], name: str
) -> None:
    spec = next(f for f in part_a.FUNCTIONS if f.name == name)
    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        row = _function_row(conn, schema, spec.name, spec.args)
        assert row is not None, f"{schema}.{spec.name}({spec.args}) is not installed"
        owner, secdef, config, acl = row
        assert owner == spec.owner
        assert secdef is spec.security_definer
        assert config == [part_a.SEARCH_PATH_CONFIG.format(schema=schema)]
        assert part_a.function_acl_problems(acl, spec.owner, spec.executors) == []
        signature = f"{schema}.{spec.name}({spec.args})"
        # Effective permissions, owner and executors positive; others negative.
        for role in (spec.owner, *sorted(spec.executors)):
            assert conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, signature)
            ).fetchone() == (True,), role
        others = set(part_a.PART_A_ROLES) | {part_a.RUNTIME, part_a.MIGRATOR}
        for role in sorted(others - set(spec.executors) - {spec.owner}):
            assert conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, signature)
            ).fetchone() == (False,), role


@pytest.mark.parametrize("role", ["haloflow_runtime", "haloflow_m05_owner"])
def test_pa14_canon_serializer_not_executable_by_others(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], role: str
) -> None:
    schema = production_tenant[1]
    with _admin(m02_ids) as conn:
        row = _function_row(conn, schema, "m02_canon_v1", "jsonb")
        assert row is not None, "m02_canon_v1(jsonb) is not installed"
        allowed = conn.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            (role, f"{schema}.m02_canon_v1(jsonb)"),
        ).fetchone()
        assert allowed == (False,)


# --- PA-15 / PA-28 / PA-47: table column grants ----------------------------


def _assert_exact_select(
    part_a: ModuleType, conn: Any, role: str, table: str, granted: tuple[str, ...]
) -> None:
    assert part_a.regclass_exists(conn, table), f"{table} is not installed"
    columns = [
        r[0]
        for r in conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass"
            " AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (table,),
        ).fetchall()
    ]
    missing = set(granted) - set(columns)
    assert not missing, (table, missing)
    for column in columns:
        assert part_a.column_privilege(conn, role, table, column, "SELECT") is (
            column in granted
        ), (role, table, column)


@pytest.mark.parametrize(
    ("role_attr", "grants_attr"),
    [("APPEND_OWNER", "APPEND_OWNER_SELECT"), ("READ_OWNER", "READ_OWNER_SELECT")],
)
def test_pa15_pa28_pa47_exact_select_grants(
    part_a: ModuleType,
    m02_ids: Any,
    production_tenant: tuple[str, str],
    role_attr: str,
    grants_attr: str,
) -> None:
    schema = production_tenant[1]
    role = getattr(part_a, role_attr)
    with _admin(m02_ids) as conn:
        for table, granted in getattr(part_a, grants_attr).items():
            _assert_exact_select(part_a, conn, role, f"{schema}.{table}", granted)


def _assert_exact_column_privilege(
    part_a: ModuleType, conn: Any, role: str, table: str, granted: tuple[str, ...], privilege: str
) -> None:
    assert part_a.regclass_exists(conn, table), f"{table} is not installed"
    columns = [
        r[0]
        for r in conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass"
            " AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (table,),
        ).fetchall()
    ]
    assert not set(granted) - set(columns), (table, set(granted) - set(columns))
    for column in columns:
        assert part_a.column_privilege(conn, role, table, column, privilege) is (
            column in granted
        ), (role, table, column, privilege)


def _assert_no_table_privileges(
    conn: Any, role: str, table: str, privileges: tuple[str, ...]
) -> None:
    for privilege in privileges:
        assert conn.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
        ).fetchone() == (False,), (role, table, privilege)


PROTECTED_TABLE_PRIVILEGES = ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def test_pa15_append_owner_insert_sets_and_no_mutation(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    schema = production_tenant[1]
    role = part_a.APPEND_OWNER
    with _admin(m02_ids) as conn:
        for table, inserts in (
            (f"{schema}.patient_events", part_a.EVENT_INSERT_SET_E),
            (f"{schema}.patient_event_links", part_a.LINK_COLUMNS),
        ):
            _assert_exact_column_privilege(part_a, conn, role, table, inserts, "INSERT")
            for privilege in ("UPDATE", "REFERENCES"):
                _assert_exact_column_privilege(part_a, conn, role, table, (), privilege)
            _assert_no_table_privileges(conn, role, table, PROTECTED_TABLE_PRIVILEGES)


def test_pa15_projection_owner_outbox_grants(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    schema = production_tenant[1]
    table = f"{schema}.event_handoff_outbox"
    role = part_a.PROJECTION_OWNER
    with _admin(m02_ids) as conn:
        _assert_exact_select(part_a, conn, role, table, part_a.PROJECTION_OWNER_OUTBOX_SELECT)
        _assert_exact_column_privilege(
            part_a, conn, role, table, part_a.OUTBOX_INSERT_SET, "INSERT"
        )
        for privilege in ("UPDATE", "REFERENCES"):
            _assert_exact_column_privilege(part_a, conn, role, table, (), privilege)
        _assert_no_table_privileges(conn, role, table, PROTECTED_TABLE_PRIVILEGES)
        # The append owner reaches the outbox only through enqueue: no table and
        # no column privilege of any kind (Codex ee98edee).
        _assert_no_table_privileges(
            conn, part_a.APPEND_OWNER, table, ("SELECT", "INSERT", *PROTECTED_TABLE_PRIVILEGES)
        )
        assert conn.execute(
            "SELECT has_any_column_privilege(%s, %s, 'SELECT,INSERT,UPDATE,REFERENCES')",
            (part_a.APPEND_OWNER, table),
        ).fetchone() == (False,)


@pytest.mark.parametrize(
    "table",
    [
        "patient_events",
        "patient_event_links",
        "event_handoff_outbox",
        "m02_envelope_path_allowlist",
        "event_contracts",
        "event_types",
    ],
)
def test_pa28_runtime_has_no_table_privileges(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], table: str
) -> None:
    qualified = f"{production_tenant[1]}.{table}"
    with _admin(m02_ids) as conn:
        assert part_a.regclass_exists(conn, qualified), f"{qualified} is not installed"
        row = conn.execute(
            "SELECT has_table_privilege(%s, %s,"
            " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'),"
            " has_any_column_privilege(%s, %s, 'SELECT,INSERT,UPDATE,REFERENCES')",
            (part_a.RUNTIME, qualified, part_a.RUNTIME, qualified),
        ).fetchone()
        assert row == (False, False)


# --- frozen physical names (Codex 95ae5bfd) --------------------------------


@pytest.mark.parametrize("table", ["ref_event_levels", "ref_event_statuses", "event_types"])
def test_reference_table_shape_is_frozen(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str], table: str
) -> None:
    qualified = f"{production_tenant[1]}.{table}"
    with _admin(m02_ids) as conn:
        assert part_a.regclass_exists(conn, qualified), f"{qualified} is not installed"
        rows = conn.execute(
            "SELECT attname, format_type(atttypid, atttypmod), attnotnull FROM pg_attribute"
            " WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (qualified,),
        ).fetchall()
        assert [tuple(r) for r in rows] == list(part_a.REFERENCE_TABLES[table])


def test_event_contracts_columns_and_extensions_are_frozen(
    part_a: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    qualified = f"{production_tenant[1]}.event_contracts"
    with _admin(m02_ids) as conn:
        assert part_a.regclass_exists(conn, qualified), f"{qualified} is not installed"
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT attname, format_type(atttypid, atttypmod), attnotnull"
                " FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0"
                " AND NOT attisdropped",
                (qualified,),
            ).fetchall()
        }
        assert set(rows) == set(part_a.EVENT_CONTRACT_COLUMNS)
        for column, expected in part_a.FROZEN_CONTRACT_COLUMN_TYPES.items():
            assert rows[column] == expected, column
