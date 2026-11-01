"""MA-10 staff directory, administration and display read on PostgreSQL 17.

Package v1 (a953d905) + v1r1 delta (0bc7ad98) + v1r2 adoption (62b09d5b),
Claude/Codex aligned 2026-11-01 (codex_ma10-package-v1r1-alignment). Owner choices
(2026-11-01): HaloFlow-kept per-clinic list; named clinic administrator; deactivated
staff keep visible items; assignable = active with read AND act; names stored.

RED TRANCHE. t021 (tables, rejectors, grants) is real; t022..t024 are fail-closed
stubs. Rows that test the schema or privileges may already pass and are reported
as such; behaviour rows fail in their own bodies.

Not claimed here (v1r1 delta 4): authorization of the administrator or of the
display caller. The SQL `staff_admin` flag is defence in depth; B3b proves that
capabilities come from the trusted M01 context. SQL tests prove shape and privilege
only.

Rows:
- DT-01  add -> active, name stored, access [], one key + one log row; replay.
- DT-02  add on an existing id -> PRECONDITION_FAILED, zero writes, no key.
- DT-03  rename / deactivate / reactivate / grant / revoke happy paths.
- DT-04  every precondition failure -> PRECONDITION_FAILED, zero writes.
- DT-05  staff_admin false; workload principal -> NOT_AUTHORIZED.
- DT-06  closed schema at every level; display-name rule (v1r2 c4) -> COMMAND_MALFORMED.
- DT-07  same key, different digest -> ACTION_KEY_CONFLICT.
- DT-08  REPEATABLE READ / SERIALIZABLE -> ISOLATION_UNSUPPORTED, no lock, zero writes.
- DT-09  rejectors: log and key UPDATE/DELETE/TRUNCATE; staff DELETE/TRUNCATE refused.
- DT-10  display read: shape; unknown/foreign omitted alike; bounds; never access.
- DT-11  privileges: exact ACLs; no ordinary-role privilege or role path (v1r1 d3).
- DT-12  installed state: owner, definer, path, volatility, parallel.
- DT-13  Alembic 006 role and edge.
- DT-S1  static call graph.
- DA-K1..K7  key admission (v1r1 delta 1).
- DA-D1..D6, DA-D2b  digest, snapshots, replay, atomicity (v1r1 delta 2, v1r2 c1/c3).
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest
import staff_support as st
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


def _ready(install: st.StaffInstall) -> None:
    install.require()


def _add(
    install: st.StaffInstall, staff: str, name: str = "Dana Ortiz", **kw: Any
) -> dict[str, Any]:
    return st.admin_op(install, st.admin_command("add", staff, display_name=name, **kw))


def _assert_result(result: dict[str, Any], operation: str, key: str) -> dict[str, Any]:
    assert sorted(result) == sorted(st.RESULT_KEYS), result
    assert result["schema_version"] == 1
    assert result["operation"] == operation
    assert result["client_action_id"] == key
    staff = result["staff"]
    assert sorted(staff) == sorted(st.SNAPSHOT_KEYS), staff
    assert staff["schema_version"] == 1
    assert re.fullmatch(st.TIMESTAMP_PATTERN, staff["status_changed_at"])
    assert staff["access"] == sorted(staff["access"])
    assert set(staff["access"]) <= {"read", "act"}
    return dict(staff)


# ---------------------------------------------------------------------------
# DT-01..DT-08: administration behaviour
# ---------------------------------------------------------------------------


def test_dt01_add_creates_active_member_without_access_and_replays(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    before = st.directory_snapshot(staff_install)
    result = st.admin_op(
        staff_install, st.admin_command("add", staff, key=key, display_name="Dana Ortiz")
    )
    snap = _assert_result(result, "add", key)
    assert snap["staff_id"] == staff
    assert snap["status"] == "active"
    assert snap["display_name"] == "Dana Ortiz"
    assert snap["access"] == []  # v1r1 delta 2: add grants NO access
    after = st.directory_snapshot(staff_install)
    assert len(st.added_rows(before["m01_staff"], after["m01_staff"])) == 1
    assert st.added_rows(before["m01_staff_followup_access"],
                         after["m01_staff_followup_access"]) == []
    assert len(st.added_rows(before["m01_staff_action_keys"],
                             after["m01_staff_action_keys"])) == 1
    (log,) = st.added_rows(before["m01_staff_admin_log"], after["m01_staff_admin_log"])
    assert log["operation"] == "add" and log["before"] is None
    assert log["after"] == snap and log["result"] == result
    # Exact replay: same result, nothing written.
    replay = st.admin_op(
        staff_install, st.admin_command("add", staff, key=key, display_name="Dana Ortiz")
    )
    assert replay == result
    assert st.directory_snapshot(staff_install) == after


def test_dt02_add_existing_id_refused_without_writes(staff_install):
    _ready(staff_install)
    staff = st.new_uuid()
    _add(staff_install, staff)
    before = st.directory_snapshot(staff_install)
    st.expect_code(
        "PRECONDITION_FAILED",
        lambda: st.admin_op(staff_install, st.admin_command("add", staff, display_name="Other")),
    )
    assert st.directory_snapshot(staff_install) == before


@pytest.mark.parametrize(
    "operation, payload, expect",
    [
        ("rename", {"display_name": "Dana O."}, {"display_name": "Dana O."}),
        ("grant", {"access": "read"}, {"access": ["read"]}),
        ("deactivate", {}, {"status": "inactive"}),
    ],
)
def test_dt03_operation_snapshots(staff_install, operation, payload, expect):
    _ready(staff_install)
    staff = st.new_uuid()
    added = _assert_result(_add(staff_install, staff), "add", _last_key(staff_install, staff))
    before = st.directory_snapshot(staff_install)
    key = st.new_uuid()
    result = st.admin_op(staff_install, st.admin_command(operation, staff, key=key, **payload))
    snap = _assert_result(result, operation, key)
    for field, value in expect.items():
        assert snap[field] == value
    unchanged = {"rename": ("status", "status_changed_at", "access"),
                 "grant": ("status", "status_changed_at", "display_name"),
                 "deactivate": ("display_name", "access")}[operation]
    for field in unchanged:
        assert snap[field] == added[field], field
    if operation == "deactivate":
        assert snap["status_changed_at"] >= added["status_changed_at"]
    after = st.directory_snapshot(staff_install)
    (log,) = st.added_rows(before["m01_staff_admin_log"], after["m01_staff_admin_log"])
    assert log["before"] == added and log["after"] == snap and log["result"] == result


def test_dt03_reactivate_and_revoke(staff_install):
    _ready(staff_install)
    staff = st.new_uuid()
    st.seed_staff(staff_install, staff, access=("read", "act"), active=False)
    re_snap = _assert_result(
        st.admin_op(staff_install, st.admin_command("reactivate", staff)), "reactivate",
        _last_key(staff_install, staff),
    )
    assert re_snap["status"] == "active" and re_snap["access"] == ["act", "read"]
    rv = _assert_result(
        st.admin_op(staff_install, st.admin_command("revoke", staff, access="act")), "revoke",
        _last_key(staff_install, staff),
    )
    assert rv["access"] == ["read"] and rv["status"] == "active"
    assert rv["status_changed_at"] == re_snap["status_changed_at"]


def _last_key(install: st.StaffInstall, staff: str) -> str:
    rows = st.admin_rows(
        install,
        sql.SQL(
            "SELECT client_action_id::text FROM {}.m01_staff_admin_log "
            "WHERE staff_id = %s ORDER BY created_at DESC, log_id DESC LIMIT 1"
        ).format(sql.Identifier(install.schema)),
        (staff,),
    )
    assert rows, "no log row"
    return str(rows[0][0])


@pytest.mark.parametrize(
    "setup, operation, payload",
    [
        ("absent", "rename", {"display_name": "X"}),
        ("absent", "deactivate", {}),
        ("absent", "reactivate", {}),
        ("absent", "grant", {"access": "read"}),
        ("absent", "revoke", {"access": "read"}),
        ("inactive", "deactivate", {}),
        ("active", "reactivate", {}),
        ("granted_read", "grant", {"access": "read"}),
        ("no_access", "revoke", {"access": "act"}),
    ],
)
def test_dt04_preconditions(staff_install, setup, operation, payload):
    _ready(staff_install)
    staff = st.new_uuid()
    if setup == "inactive":
        st.seed_staff(staff_install, staff, access=(), active=False)
    elif setup == "active" or setup == "no_access":
        st.seed_staff(staff_install, staff, access=())
    elif setup == "granted_read":
        st.seed_staff(staff_install, staff, access=("read",))
    before = st.directory_snapshot(staff_install)
    key = st.new_uuid()
    st.expect_code(
        "PRECONDITION_FAILED",
        lambda: st.admin_op(staff_install, st.admin_command(operation, staff, key=key, **payload)),
    )
    assert st.directory_snapshot(staff_install) == before  # incl. no key row (DA-K6)


@pytest.mark.parametrize(
    "overrides", [{"staff_admin": False}, {"kind": "workload"}], ids=["flag_false", "workload"]
)
def test_dt05_not_authorized(staff_install, overrides):
    _ready(staff_install)
    before = st.directory_snapshot(staff_install)
    st.expect_code(
        "NOT_AUTHORIZED",
        lambda: st.admin_op(
            staff_install, st.admin_command("add", st.new_uuid(), display_name="X", **overrides)
        ),
    )
    assert st.directory_snapshot(staff_install) == before


_STAFF = "00000000-0000-4000-8000-0000000c0001"


def _malformed_commands() -> list[tuple[str, Any]]:
    base = st.admin_command("add", _STAFF, key="00000000-0000-4000-8000-0000000c0f01",
                            display_name="Valid Name")
    cases: list[tuple[str, Any]] = [("not_object", [base]), ("null", None)]
    for key in list(base):
        missing = dict(base)
        del missing[key]
        cases.append((f"missing_{key}", missing))
        nulled = dict(base)
        nulled[key] = None
        cases.append((f"null_{key}", nulled))
    cases.append(("extra_top", {**base, "digest": "00"}))
    cases.append(("extra_permissions", {**base, "permissions": {"staff_admin": True, "x": 1}}))
    cases.append(("extra_attribution", {**base, "attribution": {**base["attribution"], "x": 1}}))
    cases.append(("bad_version", {**base, "schema_version": 2}))
    cases.append(("bad_uuid", {**base, "staff_id": "not-a-uuid"}))
    cases.append(("upper_uuid", {**base, "staff_id": _STAFF.upper()}))
    cases.append(("bad_operation", {**base, "operation": "delete"}))
    cases.append(("add_with_access", {**base, "access": "read"}))
    cases.append(("grant_with_name",
                  {**st.admin_command("grant", _STAFF, access="read"), "display_name": "X"}))
    cases.append(("grant_bad_access", st.admin_command("grant", _STAFF, access="write")))
    cases.append(("grant_missing_access", st.admin_command("grant", _STAFF)))
    cases.append(("rename_missing_name", st.admin_command("rename", _STAFF)))
    cases.append(("deactivate_with_name",
                  st.admin_command("deactivate", _STAFF, display_name="X")))
    cases.append(("flag_not_bool", {**base, "permissions": {"staff_admin": "yes"}}))
    return cases


@pytest.mark.parametrize("label, command", _malformed_commands(),
                         ids=[c[0] for c in _malformed_commands()])
def test_dt06_closed_schema(staff_install, label, command):
    _ready(staff_install)
    before = st.directory_snapshot(staff_install)
    st.expect_code("COMMAND_MALFORMED", lambda: st.admin_op(staff_install, command))
    assert st.directory_snapshot(staff_install) == before


_BAD_NAMES = (
    [("empty", ""), ("81_code_points", "x" * 81), ("not_nfc", "Zoë"),
     ("control_inside", "Da\u0001na"), ("tab_inside", "Da\tna"), ("c1_inside", "Da\u0085na"),
     ("line_separator", "Da na"), ("paragraph_separator", "Da na")]
    + [(f"leading_U+{ord(c):04X}", c + "Dana") for c in st.WHITE_SPACE]
    + [(f"trailing_U+{ord(c):04X}", "Dana" + c) for c in st.WHITE_SPACE]
)


@pytest.mark.parametrize("label, name", _BAD_NAMES, ids=[n[0] for n in _BAD_NAMES])
def test_dt06_display_name_rule_rejects(staff_install, label, name):
    _ready(staff_install)
    before = st.directory_snapshot(staff_install)
    st.expect_code(
        "COMMAND_MALFORMED",
        lambda: st.admin_op(staff_install,
                            st.admin_command("add", st.new_uuid(), display_name=name)),
    )
    assert st.directory_snapshot(staff_install) == before


@pytest.mark.parametrize(
    "name", ["D", "x" * 80, "Zoë", "Da na", "O'Brien-Søren 李"],
    ids=["one", "80_code_points", "nfc_composed", "inner_nbsp", "mixed_script"],
)
def test_dt06_display_name_rule_accepts(staff_install, name):
    _ready(staff_install)
    snap = _add(staff_install, st.new_uuid(), name)["staff"]
    assert snap["display_name"] == name


def test_dt07_same_key_different_digest_conflict(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    st.admin_op(staff_install, st.admin_command("add", staff, key=key, display_name="A"))
    before = st.directory_snapshot(staff_install)
    st.expect_code(
        "ACTION_KEY_CONFLICT",
        lambda: st.admin_op(staff_install,
                            st.admin_command("add", staff, key=key, display_name="B")),
    )
    assert st.directory_snapshot(staff_install) == before


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
@pytest.mark.parametrize("function", ["m01_staff_admin", "m01_staff_display"])
def test_dt08_isolation_guard(staff_install, isolation, function):
    _ready(staff_install)
    staff = st.new_uuid()
    st.seed_staff(staff_install, staff)
    before = st.directory_snapshot(staff_install)
    arg = (st.admin_command("rename", staff, display_name="Z")
           if function == "m01_staff_admin" else {"schema_version": 1, "staff_ids": [staff]})
    if function == "m01_staff_display":
        # The display read takes no lock and is STABLE; it is not isolation-guarded.
        result = st.call(staff_install, function, arg, isolation=isolation)
        assert [e["staff_id"] for e in result["staff"]] == [staff]
        return
    with st.runtime_connection(staff_install) as conn:
        conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
        st.set_tenant(conn, staff_install)
        st.expect_code("ISOLATION_UNSUPPORTED",
                       lambda: st.call_on(conn, staff_install, function, arg))
        conn.rollback()
    assert st.directory_snapshot(staff_install) == before


def test_dt08_isolation_guard_precedes_shape(staff_install):
    _ready(staff_install)
    st.expect_code(
        "ISOLATION_UNSUPPORTED",
        lambda: st.admin_op(staff_install, {"schema_version": 9}, isolation="REPEATABLE READ"),
    )


def test_dt08_tenant_context_precedes_everything(staff_install):
    _ready(staff_install)
    with (
        st.runtime_connection(staff_install, autocommit=True) as conn,
        conn.transaction(),
        pytest.raises(psycopg.Error) as refused,
    ):
        st.call_on(conn, staff_install, "m01_staff_admin", {"schema_version": 9})
    assert refused.value.sqlstate == "22023"


# ---------------------------------------------------------------------------
# DT-09: rejectors (through DML; v1r1 delta 3 qualifies the limit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table, statement",
    [
        ("m01_staff_admin_log", "UPDATE {t} SET purpose_code = purpose_code"),
        ("m01_staff_admin_log", "DELETE FROM {t}"),
        ("m01_staff_admin_log", "TRUNCATE {t} CASCADE"),
        ("m01_staff_action_keys", "UPDATE {t} SET actor_id = actor_id"),
        ("m01_staff_action_keys", "DELETE FROM {t}"),
        ("m01_staff_action_keys", "TRUNCATE {t} CASCADE"),
        ("m01_staff", "DELETE FROM {t}"),
        ("m01_staff", "TRUNCATE {t} CASCADE"),
    ],
)
def test_dt09_rejectors_refuse_for_table_owner(staff_install, table, statement):
    _ready(staff_install)
    st.seed_staff(staff_install, st.new_uuid())
    target = f'"{staff_install.schema}"."{table}"'
    with psycopg.connect(staff_install.ids.logins[st.MIGRATOR]) as conn:
        conn.execute(f"SET ROLE {st.MIGRATOR}")
        with pytest.raises(psycopg.Error) as refused:
            conn.execute(statement.format(t=target))
        conn.rollback()
    assert refused.value.sqlstate == "P0001"
    assert refused.value.diag.message_primary == "STAFF_RECORD_IMMUTABLE"


# ---------------------------------------------------------------------------
# DT-10: display read (least-privilege read contract)
# ---------------------------------------------------------------------------


def test_dt10_display_shape_and_omission(staff_install, staff_foreign_install):
    _ready(staff_install)
    staff_foreign_install.require()
    a, b, foreign, unknown = (st.new_uuid() for _ in range(4))
    st.seed_staff(staff_install, a, name="Alpha", access=("read",))
    st.seed_staff(staff_install, b, name="Beta", access=(), active=False)
    st.seed_staff(staff_foreign_install, foreign, name="Foreign")
    result = st.display(staff_install, [a, b, foreign, unknown])
    assert sorted(result) == ["schema_version", "staff"]
    assert result["schema_version"] == 1
    entries = result["staff"]
    assert [e["staff_id"] for e in entries] == sorted([a, b])
    for entry in entries:
        assert sorted(entry) == sorted(st.DISPLAY_ENTRY_KEYS)  # never access
    by_id = {e["staff_id"]: e for e in entries}
    assert by_id[a] == {"staff_id": a, "display_name": "Alpha", "status": "active"}
    assert by_id[b] == {"staff_id": b, "display_name": "Beta", "status": "inactive"}


@pytest.mark.parametrize(
    "query",
    [
        {"schema_version": 1, "staff_ids": []},
        {"schema_version": 1, "staff_ids": [_STAFF, _STAFF]},
        {"schema_version": 1, "staff_ids": ["x"]},
        {"schema_version": 1, "staff_ids": [_STAFF.upper()]},
        {"schema_version": 1, "staff_ids": [None]},
        {"schema_version": 1},
        {"schema_version": 1, "staff_ids": [_STAFF], "extra": 1},
        {"schema_version": 2, "staff_ids": [_STAFF]},
        {"schema_version": 1, "staff_ids": _STAFF},
    ],
    ids=["empty", "duplicate", "not_uuid", "upper", "null_id", "missing", "extra",
         "version", "not_array"],
)
def test_dt10_display_malformed(staff_install, query):
    _ready(staff_install)
    st.expect_code("COMMAND_MALFORMED", lambda: st.call(staff_install, "m01_staff_display", query))


def test_dt10_display_bounds(staff_install):
    _ready(staff_install)
    ids = [st.new_uuid() for _ in range(st.DISPLAY_MAX_IDS)]
    assert st.display(staff_install, ids)["staff"] == []
    st.expect_code("COMMAND_MALFORMED",
                   lambda: st.display(staff_install, ids + [st.new_uuid()]))


# ---------------------------------------------------------------------------
# DT-11: privileges and trust model (v1r1 delta 3)
# ---------------------------------------------------------------------------


def _table_acl(install: st.StaffInstall, table: str) -> set[tuple[str, str]]:
    rows = st.admin_rows(
        install,
        "SELECT grantee::regrole::text, privilege_type FROM pg_catalog.pg_class c, "
        "LATERAL pg_catalog.aclexplode(c.relacl) a "
        "WHERE c.oid = pg_catalog.to_regclass(%s) AND a.grantee <> c.relowner",
        (f"{install.schema}.{table}",),
    )
    return {(str(g), str(p)) for g, p in rows}


def _column_acl(install: st.StaffInstall, table: str) -> set[tuple[str, str, str]]:
    rows = st.admin_rows(
        install,
        "SELECT a.attname, x.grantee::regrole::text, x.privilege_type "
        "FROM pg_catalog.pg_attribute a, LATERAL pg_catalog.aclexplode(a.attacl) x "
        "WHERE a.attrelid = pg_catalog.to_regclass(%s) AND a.attacl IS NOT NULL",
        (f"{install.schema}.{table}",),
    )
    return {(str(c), str(g), str(p)) for c, g, p in rows}


@pytest.mark.parametrize("table", st.TABLES)
def test_dt11_exact_table_and_column_acl(staff_install, table):
    _ready(staff_install)
    assert _table_acl(staff_install, table) == {
        (st.STAFF_OWNER, p) for p in st.OWNER_TABLE_ACL[table]
    }
    expected_cols = {
        (col, st.STAFF_OWNER, priv)
        for (t, priv), cols in st.OWNER_COLUMN_ACL.items() if t == table
        for col in cols
    }
    assert _column_acl(staff_install, table) == expected_cols
    owner = st.admin_rows(
        staff_install,
        "SELECT relowner::regrole::text FROM pg_catalog.pg_class WHERE oid = "
        "pg_catalog.to_regclass(%s)", (f"{staff_install.schema}.{table}",),
    )
    assert owner == [(st.MIGRATOR,)]


@pytest.mark.parametrize("role", st.ORDINARY_ROLES)
def test_dt11_ordinary_roles_have_no_directory_privilege(staff_install, role):
    _ready(staff_install)
    for table in st.TABLES:
        qualified = f"{staff_install.schema}.{table}"
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                          "TRIGGER"):
            (row,) = st.admin_rows(
                staff_install, "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
                (role, qualified, privilege),
            )
            assert row[0] is False, (role, table, privilege)
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            (row,) = st.admin_rows(
                staff_install, "SELECT pg_catalog.has_any_column_privilege(%s, %s, %s)",
                (role, qualified, privilege),
            )
            assert row[0] is False, (role, table, privilege, "column")
    for trusted in st.TRUSTED:
        for mode in ("USAGE", "MEMBER", "SET"):
            (row,) = st.admin_rows(
                staff_install, "SELECT pg_catalog.pg_has_role(%s, %s, %s)",
                (role, trusted, mode),
            )
            assert row[0] is False, (role, trusted, mode)


def test_dt11_public_holds_nothing(staff_install):
    _ready(staff_install)
    for table in st.TABLES:
        rows = st.admin_rows(
            staff_install,
            "SELECT count(*) FROM pg_catalog.pg_class c, "
            "LATERAL pg_catalog.aclexplode(c.relacl) a "
            "WHERE c.oid = pg_catalog.to_regclass(%s) AND a.grantee = 0",
            (f"{staff_install.schema}.{table}",),
        )
        assert rows == [(0,)], table


@pytest.mark.parametrize("function", sorted(st.FUNCTIONS))
def test_dt11_exact_function_acl(staff_install, function):
    _ready(staff_install)
    args, _, _, grantee = st.FUNCTIONS[function]
    signature = f"{staff_install.schema}.{function}({', '.join(args)})"
    rows = st.admin_rows(
        staff_install,
        "SELECT a.grantee::regrole::text, a.privilege_type, a.grantor::regrole::text, "
        "a.is_grantable FROM pg_catalog.pg_proc p, LATERAL pg_catalog.aclexplode(p.proacl) a "
        "WHERE p.oid = pg_catalog.to_regprocedure(%s)",
        (signature,),
    )
    assert {tuple(r) for r in rows} == {
        (st.STAFF_OWNER, "EXECUTE", st.STAFF_OWNER, False),
        (grantee, "EXECUTE", st.STAFF_OWNER, False),
    }


# ---------------------------------------------------------------------------
# DT-12, DT-13, DT-S1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("function", sorted(st.FUNCTIONS))
def test_dt12_installed_state(staff_install, function):
    _ready(staff_install)
    args, returns, volatility, _ = st.FUNCTIONS[function]
    rows = st.admin_rows(
        staff_install,
        "SELECT p.proowner::regrole::text, p.prosecdef, p.provolatile::text, "
        "p.proparallel::text, p.proisstrict, p.proretset, p.prorettype::regtype::text, "
        "p.proconfig FROM pg_catalog.pg_proc p WHERE p.oid = pg_catalog.to_regprocedure(%s)",
        (f"{staff_install.schema}.{function}({', '.join(args)})",),
    )
    assert rows, function
    owner, definer, vol, parallel, strict, retset, rettype, config = rows[0]
    assert owner == st.STAFF_OWNER and definer is True
    assert vol == volatility and parallel == "u" and strict is False and retset is False
    assert rettype == {"bool": "boolean", "jsonb": "jsonb"}[returns]
    assert config == [f"search_path=pg_catalog, {staff_install.schema}, pg_temp"]


def test_dt13_alembic_006_role_and_single_edge(staff_install):
    _ready(staff_install)
    rows = st.admin_rows(
        staff_install,
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = %s",
        (st.STAFF_OWNER,),
    )
    assert rows == [(False, False, False, False, False, False)]
    edges = st.admin_rows(
        staff_install,
        "SELECT m.rolname, a.set_option, a.inherit_option, a.admin_option "
        "FROM pg_catalog.pg_auth_members a "
        "JOIN pg_catalog.pg_roles r ON r.oid = a.roleid "
        "JOIN pg_catalog.pg_roles m ON m.oid = a.member WHERE r.rolname = %s",
        (st.STAFF_OWNER,),
    )
    assert edges == [(st.MIGRATOR, True, False, False)]


def test_dt13_manifest_declares_role_and_edge():
    from m05_harness import production_manifest_document

    document = production_manifest_document()
    profile = document["execution_role_profiles"][st.STAFF_OWNER]
    assert profile["tenant_schema_privileges"] == ["USAGE"]  # production: USAGE until B5
    assert {
        "role": st.STAFF_OWNER, "member": st.MIGRATOR,
        "set": True, "inherit": False, "admin": False,
    } in document["role_memberships"]


def test_dts1_static_call_graph():
    """Admin and display bodies reference no m05_ relation or function (lock order,
    v1 3.6). Applies to implemented bodies; stubs pass trivially."""

    from haloflow.m01.staff.units import STAFF_GATEWAY_UNITS

    for migration_id, unit in STAFF_GATEWAY_UNITS.items():
        body = unit.policy["functions"][0]  # type: ignore[index]
        text = unit.template
        if migration_id in ("t023_m01_staff_admin", "t024_m01_staff_display"):
            assert "m05_" not in text, migration_id
        assert body["name"] == st.STAFF_TYPED_UNITS[migration_id]


def test_dts1_m05_bodies_call_exactly_one_directory_function():
    from haloflow.m05.gateways import M05_GATEWAY_UNITS

    for migration_id, unit in M05_GATEWAY_UNITS.items():
        calls = set(re.findall(r"m01_staff_[a-z]+", unit.template))
        if migration_id in ("t015_m05_prepare_action", "t016_m05_apply_action"):
            assert calls == {"m01_staff_assignable"}, (migration_id, calls)
        else:
            assert calls == set(), (migration_id, calls)


# ---------------------------------------------------------------------------
# DA-K: key admission (v1r1 delta 1)
# ---------------------------------------------------------------------------


def _admin_in_txn(install: st.StaffInstall, command: dict[str, Any]) -> tuple[Any, Any]:
    """Open a runtime transaction, call admin, return (conn, result) without commit."""

    conn = st.runtime_connection(install)
    st.set_tenant(conn, install)
    result = st.call_on(conn, install, "m01_staff_admin", command)
    return conn, result


def test_dak1_concurrent_identical_requests_replay(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    command = st.admin_command("add", staff, key=key, display_name="K1")
    before = st.directory_snapshot(staff_install)
    conn_a, result_a = _admin_in_txn(staff_install, command)
    conn_b = st.runtime_connection(staff_install)
    try:
        st.set_tenant(conn_b, staff_install)
        with st.background(lambda: st.call_on(conn_b, staff_install, "m01_staff_admin",
                                              command)) as box:
            st.wait_until_blocked_by(staff_install, conn_b.info.backend_pid,
                                     conn_a.info.backend_pid)
            conn_a.commit()
        conn_b.commit()
        assert "error" not in box, box.get("error")
        assert box["result"] == result_a
    finally:
        conn_a.close()
        conn_b.close()
    after = st.directory_snapshot(staff_install)
    for table in st.TABLES[:1] + st.TABLES[2:]:
        assert len(st.added_rows(before[table], after[table])) == 1, table


@pytest.mark.parametrize(
    "variant", ["operation", "display_name", "staff_id"],
)
def test_dak2_dak3_concurrent_conflicting_requests(staff_install, variant):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    first = st.admin_command("add", staff, key=key, display_name="K2")
    second = {
        "operation": st.admin_command("grant", staff, key=key, access="read"),
        "display_name": st.admin_command("add", staff, key=key, display_name="Other"),
        "staff_id": st.admin_command("add", st.new_uuid(), key=key, display_name="K2"),
    }[variant]
    before = st.directory_snapshot(staff_install)
    conn_a, result_a = _admin_in_txn(staff_install, first)
    conn_b = st.runtime_connection(staff_install)
    try:
        st.set_tenant(conn_b, staff_install)
        with st.background(lambda: st.call_on(conn_b, staff_install, "m01_staff_admin",
                                              second)) as box:
            st.wait_until_blocked_by(staff_install, conn_b.info.backend_pid,
                                     conn_a.info.backend_pid)
            conn_a.commit()
        conn_b.rollback()
        error = box.get("error")
        assert isinstance(error, psycopg.Error) and error.sqlstate == "P0001"
        assert error.diag.message_primary == "ACTION_KEY_CONFLICT"
    finally:
        conn_a.close()
        conn_b.close()
    after = st.directory_snapshot(staff_install)
    (log,) = st.added_rows(before["m01_staff_admin_log"], after["m01_staff_admin_log"])
    assert log["result"] == result_a


def test_dak4_first_contender_rolls_back(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    command = st.admin_command("add", staff, key=key, display_name="K4")
    before = st.directory_snapshot(staff_install)
    conn_a, _ = _admin_in_txn(staff_install, command)
    conn_b = st.runtime_connection(staff_install)
    try:
        st.set_tenant(conn_b, staff_install)
        with st.background(lambda: st.call_on(conn_b, staff_install, "m01_staff_admin",
                                              command)) as box:
            st.wait_until_blocked_by(staff_install, conn_b.info.backend_pid,
                                     conn_a.info.backend_pid)
            conn_a.rollback()
        conn_b.commit()
        assert "error" not in box, box.get("error")
        _assert_result(box["result"], "add", key)
    finally:
        conn_a.close()
        conn_b.close()
    after = st.directory_snapshot(staff_install)
    keys = st.added_rows(before["m01_staff_action_keys"], after["m01_staff_action_keys"])
    logs = st.added_rows(before["m01_staff_admin_log"], after["m01_staff_admin_log"])
    assert len(keys) == 1 and len(logs) == 1 and logs[0]["result"] == box["result"]


@pytest.mark.parametrize("concurrent", [False, True])
def test_dak5_different_actor_same_key(staff_install, concurrent):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    first = st.admin_command("add", staff, key=key, display_name="K5")
    second = st.admin_command("add", staff, key=key, display_name="K5", actor=st.ADMIN2)
    if not concurrent:
        st.admin_op(staff_install, first)
        before = st.directory_snapshot(staff_install)
        st.expect_code("ACTION_KEY_ACTOR_MISMATCH", lambda: st.admin_op(staff_install, second))
        assert st.directory_snapshot(staff_install) == before
        return
    conn_a, _ = _admin_in_txn(staff_install, first)
    conn_b = st.runtime_connection(staff_install)
    try:
        st.set_tenant(conn_b, staff_install)
        with st.background(lambda: st.call_on(conn_b, staff_install, "m01_staff_admin",
                                              second)) as box:
            st.wait_until_blocked_by(staff_install, conn_b.info.backend_pid,
                                     conn_a.info.backend_pid)
            conn_a.commit()
        conn_b.rollback()
        error = box.get("error")
        assert isinstance(error, psycopg.Error)
        assert error.diag.message_primary == "ACTION_KEY_ACTOR_MISMATCH"
    finally:
        conn_a.close()
        conn_b.close()


def test_dak6_failed_precondition_leaves_no_key_and_key_is_reusable(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    grant = st.admin_command("grant", staff, key=key, access="read")
    st.expect_code("PRECONDITION_FAILED", lambda: st.admin_op(staff_install, grant))
    assert st.admin_rows(
        staff_install,
        sql.SQL("SELECT count(*) FROM {}.m01_staff_action_keys WHERE client_action_id = %s")
        .format(sql.Identifier(staff_install.schema)), (key,),
    ) == [(0,)]
    _add(staff_install, staff)
    result = st.admin_op(staff_install, grant)
    assert _assert_result(result, "grant", key)["access"] == ["read"]


def test_dak7_different_keys_same_staff_serialize(staff_install):
    _ready(staff_install)
    staff = st.new_uuid()
    st.seed_staff(staff_install, staff, access=())
    conn_a, _ = _admin_in_txn(staff_install, st.admin_command("grant", staff, access="read"))
    conn_b = st.runtime_connection(staff_install)
    try:
        st.set_tenant(conn_b, staff_install)
        command_b = st.admin_command("grant", staff, access="act")
        with st.background(lambda: st.call_on(conn_b, staff_install, "m01_staff_admin",
                                              command_b)) as box:
            st.wait_until_blocked_by(staff_install, conn_b.info.backend_pid,
                                     conn_a.info.backend_pid)
            conn_a.commit()
        conn_b.commit()
        assert "error" not in box, box.get("error")
        assert box["result"]["staff"]["access"] == ["act", "read"]
    finally:
        conn_a.close()
        conn_b.close()


# ---------------------------------------------------------------------------
# DA-D: digest, snapshots, replay, atomicity
# ---------------------------------------------------------------------------


def test_dad1_digest_vectors_and_stored_digest():
    for parts, expected in st.DIGEST_VECTORS:
        assert st.admin_digest(*parts).hex() == expected


def test_dad1_stored_digest_equals_oracle(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    st.admin_op(staff_install, st.admin_command("add", staff, key=key, display_name='Zoë "Z"'))
    rows = st.admin_rows(
        staff_install,
        sql.SQL("SELECT digest, actor_id, staff_id::text FROM {}.m01_staff_action_keys "
                "WHERE client_action_id = %s").format(sql.Identifier(staff_install.schema)),
        (key,),
    )
    assert rows == [(
        st.admin_digest(staff_install.tenant[0], key, st.ADMIN, "add", staff, 'Zoë "Z"', None),
        st.ADMIN, staff,
    )]


@pytest.mark.parametrize(
    "transport",
    [{"request_id": "req-2"}, {"execution_id": "00000000-0000-4000-8000-00000000e002"},
     {"purpose_code": "audit"}, {"auth_method": "other"}],
    ids=["request_id", "execution_id", "purpose_code", "auth_method"],
)
def test_dad2_transport_change_replays(staff_install, transport):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    first = st.admin_op(staff_install, st.admin_command("add", staff, key=key, display_name="T"))
    before = st.directory_snapshot(staff_install)
    retry = st.admin_command("add", staff, key=key, display_name="T", attribution_extra=transport)
    assert st.admin_op(staff_install, retry) == first
    assert st.directory_snapshot(staff_install) == before


@pytest.mark.parametrize(
    "overrides", [{"staff_admin": False}, {"kind": "workload"}], ids=["flag_false", "workload"]
)
def test_dad2b_authorization_precedes_replay(staff_install, overrides):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    st.admin_op(staff_install, st.admin_command("add", staff, key=key, display_name="T"))
    before = st.directory_snapshot(staff_install)
    st.expect_code(
        "NOT_AUTHORIZED",
        lambda: st.admin_op(
            staff_install,
            st.admin_command("add", staff, key=key, display_name="T", **overrides),
        ),
    )
    assert st.directory_snapshot(staff_install) == before


def test_dad3_every_operation_snapshot(staff_install):
    _ready(staff_install)
    staff = st.new_uuid()
    seq = [
        ("add", {"display_name": "Six Ops"}, {"status": "active", "access": []}),
        ("grant", {"access": "act"}, {"access": ["act"]}),
        ("grant", {"access": "read"}, {"access": ["act", "read"]}),
        ("rename", {"display_name": "Six Ops 2"}, {"display_name": "Six Ops 2"}),
        ("deactivate", {}, {"status": "inactive", "access": ["act", "read"]}),
        ("reactivate", {}, {"status": "active"}),
        ("revoke", {"access": "act"}, {"access": ["read"]}),
    ]
    previous: dict[str, Any] | None = None
    for operation, payload, expect in seq:
        key = st.new_uuid()
        snap = _assert_result(
            st.admin_op(staff_install, st.admin_command(operation, staff, key=key, **payload)),
            operation, key,
        )
        for field, value in expect.items():
            assert snap[field] == value, (operation, field)
        if previous is not None and operation in ("rename", "grant", "revoke"):
            assert snap["status_changed_at"] == previous["status_changed_at"], operation
        previous = snap


def test_dad4_replay_after_intervening_changes_returns_original(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    st.seed_staff(staff_install, staff, access=("read", "act"))
    command = st.admin_command("rename", staff, key=key, display_name="Original")
    original = st.admin_op(staff_install, command)
    st.admin_op(staff_install, st.admin_command("rename", staff, display_name="Later"))
    st.admin_op(staff_install, st.admin_command("revoke", staff, access="act"))
    st.admin_op(staff_install, st.admin_command("deactivate", staff))
    before = st.directory_snapshot(staff_install)
    assert st.admin_op(staff_install, command) == original
    assert st.directory_snapshot(staff_install) == before


def test_dad5_log_insert_fault_rolls_back_everything(staff_install):
    _ready(staff_install)
    schema = staff_install.schema
    trigger = sql.SQL(
        "CREATE FUNCTION {s}.test_fault_log() RETURNS trigger LANGUAGE plpgsql AS "
        "$$BEGIN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'TEST_FAULT'; END$$; "
        "CREATE TRIGGER test_fault_log BEFORE INSERT ON {s}.m01_staff_admin_log "
        "FOR EACH ROW EXECUTE FUNCTION {s}.test_fault_log()"
    ).format(s=sql.Identifier(schema))
    st.admin_rows(staff_install, trigger)
    try:
        before = st.directory_snapshot(staff_install)
        with pytest.raises(psycopg.Error) as fault:
            st.admin_op(staff_install,
                        st.admin_command("add", st.new_uuid(), display_name="Fault"))
        assert fault.value.diag.message_primary == "TEST_FAULT"
        assert st.directory_snapshot(staff_install) == before
    finally:
        st.admin_rows(staff_install, sql.SQL(
            "DROP TRIGGER test_fault_log ON {s}.m01_staff_admin_log; "
            "DROP FUNCTION {s}.test_fault_log()").format(s=sql.Identifier(schema)))


@pytest.mark.parametrize("column", ["before", "after", "result"])
def test_dad6_log_closed_shapes(staff_install, column):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    st.admin_op(staff_install, st.admin_command("add", staff, key=key, display_name="S"))
    st.admin_op(staff_install, st.admin_command("rename", staff, display_name="S2"))
    schema = sql.Identifier(staff_install.schema)
    template = st.admin_rows(
        staff_install,
        sql.SQL("SELECT before, after, result, digest FROM {}.m01_staff_admin_log "
                "WHERE staff_id = %s AND operation = 'rename'").format(schema), (staff,),
    )[0]
    values = dict(zip(("before", "after", "result"), template[:3], strict=True))
    for mutation in ("extra", "missing"):
        bad = dict(values)
        doc = dict(bad[column])
        if mutation == "extra":
            doc["extra"] = 1
        else:
            doc.pop(sorted(doc)[0])
        bad[column] = doc
        new_key = st.new_uuid()
        with psycopg.connect(staff_install.ids.admin) as conn:
            conn.execute(
                sql.SQL("INSERT INTO {}.m01_staff_action_keys VALUES "
                        "(%s, %s, %s, %s, now())").format(schema),
                (new_key, template[3], st.ADMIN, staff),
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    sql.SQL(
                        "INSERT INTO {}.m01_staff_admin_log VALUES (gen_random_uuid(), %s, %s, "
                        "'rename', %s, %s, %s, %s, gen_random_uuid(), NULL, 'operations', %s, "
                        "now())"
                    ).format(schema),
                    (new_key, staff, Jsonb(bad["before"]), Jsonb(bad["after"]),
                     Jsonb(bad["result"]), st.ADMIN, template[3]),
                )
            conn.rollback()


def test_dad3_integrity_key_without_log_fails_closed(staff_install):
    """v1r2 c3: an admitted key with no log row never replays as success."""

    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    command = st.admin_command("add", staff, key=key, display_name="I")
    digest = st.admin_digest(staff_install.tenant[0], key, st.ADMIN, "add", staff, "I", None)
    st.admin_rows(
        staff_install,
        sql.SQL("INSERT INTO {}.m01_staff_action_keys VALUES (%s, %s, %s, %s, now())")
        .format(sql.Identifier(staff_install.schema)),
        (key, digest, st.ADMIN, staff),
    )
    before = st.directory_snapshot(staff_install)
    st.expect_code("ADMIN_LOG_INTEGRITY", lambda: st.admin_op(staff_install, command))
    assert st.directory_snapshot(staff_install) == before
