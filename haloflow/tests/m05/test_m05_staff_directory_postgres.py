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


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_dt08_isolation_guard_takes_no_lock_first(staff_install, isolation):
    """Dynamic evidence (red review F-MA10-RED-03): a competing holder owns the
    membership row; the guarded admin call must refuse at once, never wait."""

    _ready(staff_install)
    staff = st.new_uuid()
    st.seed_staff(staff_install, staff)
    before = st.state(staff_install)
    with (
        st.holder(staff_install, [("m01_staff", "staff_id", staff)]),
        st.runtime_connection(staff_install) as conn,
    ):
        conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
        st.set_tenant(conn, staff_install)
        st.expect_immediate_code(
            conn, "ISOLATION_UNSUPPORTED",
            lambda: st.call_on(conn, staff_install, "m01_staff_admin",
                               st.admin_command("rename", staff, display_name="Z")),
        )
        conn.rollback()
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


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


def _table_acl(install: st.StaffInstall, table: str) -> set[tuple[str, str, str, bool]]:
    rows = st.admin_rows(
        install,
        "SELECT a.grantee::regrole::text, a.privilege_type, a.grantor::regrole::text, "
        "a.is_grantable FROM pg_catalog.pg_class c, "
        "LATERAL pg_catalog.aclexplode(c.relacl) a "
        "WHERE c.oid = pg_catalog.to_regclass(%s) AND a.grantee <> c.relowner",
        (f"{install.schema}.{table}",),
    )
    return {(str(g), str(p), str(r), bool(o)) for g, p, r, o in rows}


def _column_acl(install: st.StaffInstall, table: str) -> set[tuple[str, str, str, str, bool]]:
    rows = st.admin_rows(
        install,
        "SELECT a.attname, x.grantee::regrole::text, x.privilege_type, "
        "x.grantor::regrole::text, x.is_grantable "
        "FROM pg_catalog.pg_attribute a, LATERAL pg_catalog.aclexplode(a.attacl) x "
        "WHERE a.attrelid = pg_catalog.to_regclass(%s) AND a.attacl IS NOT NULL",
        (f"{install.schema}.{table}",),
    )
    return {(str(c), str(g), str(p), str(r), bool(o)) for c, g, p, r, o in rows}


TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                    "TRIGGER", "MAINTAIN")
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")


def _columns(install: st.StaffInstall, table: str) -> list[str]:
    return [
        str(r[0]) for r in st.admin_rows(
            install,
            "SELECT attname FROM pg_catalog.pg_attribute WHERE attrelid = "
            "pg_catalog.to_regclass(%s) AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (f"{install.schema}.{table}",),
        )
    ]


@pytest.mark.parametrize("table", st.TABLES)
def test_dt11_exact_table_and_column_acl(staff_install, table):
    """Exact grants including grantor (the migrator, which runs t021) and the
    grant option (never granted)."""

    _ready(staff_install)
    assert _table_acl(staff_install, table) == {
        (st.STAFF_OWNER, p, st.MIGRATOR, False) for p in st.OWNER_TABLE_ACL[table]
    }
    expected_cols = {
        (col, st.STAFF_OWNER, priv, st.MIGRATOR, False)
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


@pytest.mark.parametrize("table", st.TABLES)
def test_dt11_staff_owner_exact_effective_privileges(staff_install, table):
    """Effective privileges equal the grants: every table privilege (PG17 MAINTAIN
    included) and every column privilege, with no grant option anywhere."""

    _ready(staff_install)
    qualified = f"{staff_install.schema}.{table}"
    granted = set(st.OWNER_TABLE_ACL[table])
    for privilege in TABLE_PRIVILEGES:
        (row,) = st.admin_rows(
            staff_install,
            "SELECT pg_catalog.has_table_privilege(%s, %s, %s), "
            "pg_catalog.has_table_privilege(%s, %s, %s)",
            (st.STAFF_OWNER, qualified, privilege,
             st.STAFF_OWNER, qualified, f"{privilege} WITH GRANT OPTION"),
        )
        assert row == (privilege in granted, False), (table, privilege)
    for column in _columns(staff_install, table):
        for privilege in COLUMN_PRIVILEGES:
            expected = privilege in granted or column in st.OWNER_COLUMN_ACL.get(
                (table, privilege), ()
            )
            (row,) = st.admin_rows(
                staff_install,
                "SELECT pg_catalog.has_column_privilege(%s, %s, %s, %s), "
                "pg_catalog.has_column_privilege(%s, %s, %s, %s)",
                (st.STAFF_OWNER, qualified, column, privilege,
                 st.STAFF_OWNER, qualified, column, f"{privilege} WITH GRANT OPTION"),
            )
            assert row == (expected, False), (table, column, privilege)


@pytest.mark.parametrize("role", st.ORDINARY_ROLES)
def test_dt11_ordinary_roles_have_no_directory_privilege(staff_install, role):
    _ready(staff_install)
    for table in st.TABLES:
        qualified = f"{staff_install.schema}.{table}"
        for privilege in TABLE_PRIVILEGES:
            (row,) = st.admin_rows(
                staff_install, "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
                (role, qualified, privilege),
            )
            assert row[0] is False, (role, table, privilege)
        for privilege in COLUMN_PRIVILEGES:
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
    # B5 declared change: production composes the staff gateways, so the owner holds
    # USAGE+CREATE (it creates its functions); USAGE only until B5.
    assert profile["tenant_schema_privileges"] == ["USAGE", "CREATE"]
    assert {
        "role": st.STAFF_OWNER, "member": st.MIGRATOR,
        "set": True, "inherit": False, "admin": False,
    } in document["role_memberships"]


_ROLE_ATTRS = (
    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
    "FROM pg_catalog.pg_roles WHERE rolname = %s"
)


@pytest.mark.parametrize("attribute", ["LOGIN", "BYPASSRLS"])
def test_dt13_pre_existing_unsafe_role_refused_without_mutation(
    m02_ids, database_url, apply_migrations, attribute
):
    """A pre-existing unsafe staff owner (roles are cluster-wide) is neither
    "normalized" by Alembic 006 nor used: provisioning refuses before any unit and
    leaves the role exactly as it found it. State is restored and compared."""

    import m02_support as m02
    from test_m02_part_a_prerequisites_postgres import fresh_database

    from haloflow.m01.errors import ProvisioningFailed

    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        original = conn.execute(_ROLE_ATTRS, (st.STAFF_OWNER,)).fetchall()
    assert original == [(False, False, False, False, False, False)]
    try:
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute(f"ALTER ROLE {st.STAFF_OWNER} {attribute}")
            unsafe = conn.execute(_ROLE_ATTRS, (st.STAFF_OWNER,)).fetchall()
        assert unsafe != original
        with fresh_database(m02_ids, database_url, apply_migrations, "005") as (ids, upgrade):
            upgrade("006")  # the role exists: 006 must not alter it
            with psycopg.connect(ids.admin, autocommit=True) as conn:
                assert conn.execute(_ROLE_ATTRS, (st.STAFF_OWNER,)).fetchall() == unsafe
            tenant = m02.tenant_for(f"dt13-unsafe-{attribute.lower()}")
            with pytest.raises(ProvisioningFailed) as caught:
                m02.provision_sync(ids, st.staff_registry(), tenant,
                                   supported=st.STAFF_TEST_SUPPORTED,
                                   manifest=st.staff_manifest())
            assert caught.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE"
            assert m02.ledger(ids, tenant[0]) == {}
            with psycopg.connect(ids.admin, autocommit=True) as conn:
                assert conn.execute(_ROLE_ATTRS, (st.STAFF_OWNER,)).fetchall() == unsafe
    finally:
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute(f"ALTER ROLE {st.STAFF_OWNER} NO{attribute}")
            restored = conn.execute(_ROLE_ATTRS, (st.STAFF_OWNER,)).fetchall()
    assert restored == original


def test_dt13_t021_guard_refuses_unsafe_role_directly(staff_install):
    """The t021 guard itself (run as the migrator, as the unit runs) refuses an
    unsafe staff owner; positive control first."""

    from haloflow.m01.staff.units import T021_SQL

    _ready(staff_install)
    guard = T021_SQL.split("-- t021:step:tables")[0]
    with psycopg.connect(staff_install.ids.logins[st.MIGRATOR], autocommit=True) as conn:
        conn.execute(f"SET ROLE {st.MIGRATOR}")
        conn.execute(guard)  # passes while the role is safe
    try:
        st.admin_rows(staff_install, f"ALTER ROLE {st.STAFF_OWNER} LOGIN")
        with psycopg.connect(staff_install.ids.logins[st.MIGRATOR], autocommit=True) as conn:
            conn.execute(f"SET ROLE {st.MIGRATOR}")
            with pytest.raises(psycopg.Error) as refused:
                conn.execute(guard)
        assert refused.value.diag.message_primary == "staff directory role verification failed"
    finally:
        st.admin_rows(staff_install, f"ALTER ROLE {st.STAFF_OWNER} NOLOGIN")


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


def _only_admin_delta(
    install: st.StaffInstall, before: st.State, command: dict[str, Any], result: dict[str, Any]
) -> None:
    delta = st.Delta(before, st.state(install))
    st.check_admin_delta(delta, before, install, command, result)
    delta.untouched(st.M05_CONTENT_TABLES)


def _contend(
    install: st.StaffInstall, first: dict[str, Any], second: dict[str, Any], *, commit: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A runs `first` and holds its transaction; B runs `second`, observed waiting
    on A exactly; A commits or rolls back; B commits. Returns (A result, B box)."""

    conn_a, result_a = _admin_in_txn(install, first)
    conn_b = st.runtime_connection(install)
    try:
        st.set_tenant(conn_b, install)
        with st.background(lambda: st.call_on(conn_b, install, "m01_staff_admin", second)) as box:
            st.wait_until_blocked_by(install, conn_b.info.backend_pid, conn_a.info.backend_pid)
            (conn_a.commit if commit else conn_a.rollback)()
        if "error" in box:
            conn_b.rollback()
        else:
            conn_b.commit()
    finally:
        conn_a.close()
        conn_b.close()
    return result_a, box


def test_dak1_concurrent_identical_requests_replay(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    command = st.admin_command("add", staff, key=key, display_name="K1")
    before = st.state(staff_install)
    result_a, box = _contend(staff_install, command, command, commit=True)
    assert "error" not in box, box.get("error")
    assert box["result"] == result_a
    _only_admin_delta(staff_install, before, command, result_a)  # one effect only


@pytest.mark.parametrize("variant", ["operation", "display_name", "staff_id"])
def test_dak2_dak3_concurrent_conflicting_requests(staff_install, variant):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    first = st.admin_command("add", staff, key=key, display_name="K2")
    second = {
        "operation": st.admin_command("grant", staff, key=key, access="read"),
        "display_name": st.admin_command("add", staff, key=key, display_name="Other"),
        "staff_id": st.admin_command("add", st.new_uuid(), key=key, display_name="K2"),
    }[variant]
    before = st.state(staff_install)
    result_a, box = _contend(staff_install, first, second, commit=True)
    error = box.get("error")
    assert isinstance(error, psycopg.Error) and error.sqlstate == "P0001"
    assert error.diag.message_primary == "ACTION_KEY_CONFLICT"
    _only_admin_delta(staff_install, before, first, result_a)


def test_dak4_first_contender_rolls_back(staff_install):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    command = st.admin_command("add", staff, key=key, display_name="K4")
    before = st.state(staff_install)
    _, box = _contend(staff_install, command, command, commit=False)
    assert "error" not in box, box.get("error")
    _assert_result(box["result"], "add", key)
    _only_admin_delta(staff_install, before, command, box["result"])  # B's effect only


@pytest.mark.parametrize("concurrent", [False, True])
def test_dak5_different_actor_same_key(staff_install, concurrent):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    first = st.admin_command("add", staff, key=key, display_name="K5")
    second = st.admin_command("add", staff, key=key, display_name="K5", actor=st.ADMIN2)
    before = st.state(staff_install)
    if not concurrent:
        result_a = st.admin_op(staff_install, first)
        st.expect_code("ACTION_KEY_ACTOR_MISMATCH", lambda: st.admin_op(staff_install, second))
    else:
        result_a, box = _contend(staff_install, first, second, commit=True)
        error = box.get("error")
        assert isinstance(error, psycopg.Error)
        assert error.diag.message_primary == "ACTION_KEY_ACTOR_MISMATCH"
    _only_admin_delta(staff_install, before, first, result_a)


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
    first = st.admin_command("grant", staff, access="read")
    second = st.admin_command("grant", staff, access="act")
    before = st.state(staff_install)
    result_a, box = _contend(staff_install, first, second, commit=True)
    assert "error" not in box, box.get("error")
    assert result_a["staff"]["access"] == ["read"]
    assert box["result"]["staff"]["access"] == ["act", "read"]
    delta = st.Delta(before, st.state(staff_install))
    assert sorted((r["staff_id"], r["access"]) for r in delta.added["m01_staff_followup_access"]) \
        == [(staff, "act"), (staff, "read")]
    assert sorted(r["client_action_id"] for r in delta.added["m01_staff_action_keys"]) == sorted(
        [first["client_action_id"], second["client_action_id"]]
    )
    assert sorted(r["client_action_id"] for r in delta.added["m01_staff_admin_log"]) == sorted(
        [first["client_action_id"], second["client_action_id"]]
    )
    for table in st.ALL_TABLES:
        assert not delta.removed[table] and not delta.changed[table], table
    delta.untouched(("m01_staff",) + st.M05_CONTENT_TABLES)


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
     {"purpose_code": "payment"}, {"auth_method": "other"}],
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


def test_dts2_directory_sql_is_ascii_with_escapes():
    """Every directory SQL text (t021 and the three bodies) is pure ASCII: Unicode
    classes are written as \\uXXXX escapes, never literal characters, so no
    normalization (NFC folds U+2000 into U+2002) can change a class or a body hash."""

    from haloflow.m01.staff import units

    texts = {"t021": units.T021_SQL}
    texts.update({mid: unit.template for mid, unit in units.STAFF_GATEWAY_UNITS.items()})
    for label, text in texts.items():
        assert text.isascii(), (label, [c for c in text if not c.isascii()][:5])


# ---------------------------------------------------------------------------
# DA-D7 / DA-D8: persisted replay integrity (Codex F-MA10-IMPL-01)
# ---------------------------------------------------------------------------


def _valid_rows(
    install: st.StaffInstall, staff: str, key: str, name: str, *, operation: str = "add",
    timestamp: str = "2026-10-01T09:30:00.000000Z",
) -> tuple[Any, Any]:
    """A constraint-valid, semantically valid key row and log row for `add` or
    `deactivate` (a non-add/rename operation: the name is not in its command)."""

    command_name = name if operation == "add" else None
    digest = st.admin_digest(install.tenant[0], key, st.ADMIN, operation, staff,
                             command_name, None)
    status = "active" if operation == "add" else "inactive"
    snap = {"schema_version": 1, "staff_id": staff, "status": status, "display_name": name,
            "access": [], "status_changed_at": timestamp}
    key_row = {"client_action_id": key, "digest": digest, "actor_id": st.ADMIN,
               "staff_id": staff}
    log_row = {"client_action_id": key, "staff_id": staff, "operation": operation,
               "before": None if operation == "add" else {**snap, "status": "active"},
               "after": snap,
               "result": {"schema_version": 1, "client_action_id": key, "operation": operation,
                          "staff": dict(snap)},
               "actor_id": st.ADMIN, "digest": digest}
    return key_row, log_row


def _replay_command(staff: str, key: str, name: str, operation: str) -> dict[str, Any]:
    if operation == "add":
        return st.admin_command("add", staff, key=key, display_name=name)
    return st.admin_command(operation, staff, key=key)


def _insert_rows(install: st.StaffInstall, key_row: dict[str, Any],
                 log_row: dict[str, Any]) -> None:
    schema = sql.Identifier(install.schema)
    st.admin_rows(install, sql.SQL(
        "INSERT INTO {}.m01_staff_action_keys VALUES (%s, %s, %s, %s, now())").format(schema),
        (key_row["client_action_id"], key_row["digest"], key_row["actor_id"],
         key_row["staff_id"]))
    st.admin_rows(install, sql.SQL(
        "INSERT INTO {}.m01_staff_admin_log VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, "
        "%s, %s, NULL, 'operations', %s, now())").format(schema),
        (log_row["client_action_id"], log_row["staff_id"], log_row["operation"],
         None if log_row["before"] is None else Jsonb(log_row["before"]),
         Jsonb(log_row["after"]), Jsonb(log_row["result"]), log_row["actor_id"],
         "00000000-0000-4000-8000-00000000e001", log_row["digest"]))


def _set(path: tuple[str, ...], value: Any) -> Any:
    def corrupt(key_row: dict[str, Any], log_row: dict[str, Any]) -> None:
        target: Any = {"key": key_row, "log": log_row}
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
    return corrupt


def _both_snapshots(field: str, value: Any) -> Any:
    """Corrupt the after snapshot AND result.staff identically, so only the
    snapshot's own validity (not their agreement) can catch it."""

    def corrupt(key_row: dict[str, Any], log_row: dict[str, Any]) -> None:
        log_row["after"][field] = value
        log_row["result"]["staff"][field] = value
    return corrupt


def _log_operation_rename(key_row: dict[str, Any], log_row: dict[str, Any]) -> None:
    log_row["operation"] = "rename"
    log_row["before"] = dict(log_row["after"])  # table CHECK: before iff not add


_OTHER = "00000000-0000-4000-8000-0000000c0fff"
_CORRUPTIONS = {
    "result_schema_version_999": _set(("log", "result", "schema_version"), 999),
    "codex_counterexample": lambda k, lg: lg.__setitem__("result", {
        "schema_version": 999, "client_action_id": lg["client_action_id"],
        "operation": "wrong", "staff": None}),
    "result_operation_wrong": _set(("log", "result", "operation"), "rename"),
    "result_key_other": _set(("log", "result", "client_action_id"), _OTHER),
    "result_staff_null": _set(("log", "result", "staff"), None),
    "result_staff_not_object": _set(("log", "result", "staff"), "staff"),
    "result_staff_differs_from_after": _set(("log", "result", "staff", "display_name"), "X"),
    "snapshot_schema_version_2": _both_snapshots("schema_version", 2),
    "snapshot_staff_id_other": _both_snapshots("staff_id", _OTHER),
    "snapshot_status_bogus": _both_snapshots("status", "bogus"),
    "snapshot_status_inactive_for_add": _both_snapshots("status", "inactive"),
    "snapshot_access_invalid": _both_snapshots("access", ["write"]),
    "snapshot_access_granted_on_add": _both_snapshots("access", ["read"]),
    "snapshot_name_not_command": _both_snapshots("display_name", "Someone Else"),
    "snapshot_name_null": _both_snapshots("display_name", None),
    "snapshot_timestamp_format": _both_snapshots("status_changed_at", "2026-10-01 09:30:00"),
    "log_operation_rename": _log_operation_rename,
    "log_staff_other": _set(("log", "staff_id"), _OTHER),
    "log_actor_other": _set(("log", "actor_id"), st.ADMIN2),
    "log_digest_other": _set(("log", "digest"), b"\x00" * 32),
    "key_staff_other": _set(("key", "staff_id"), _OTHER),
}


@pytest.mark.parametrize("corruption", sorted(_CORRUPTIONS))
def test_dad7_constraint_valid_corrupt_log_never_replays(staff_install, corruption):
    _ready(staff_install)
    staff, key, name = st.new_uuid(), st.new_uuid(), "Stored Name"
    key_row, log_row = _valid_rows(staff_install, staff, key, name)
    _CORRUPTIONS[corruption](key_row, log_row)
    _insert_rows(staff_install, key_row, log_row)  # passes every table constraint
    before = st.state(staff_install)
    st.expect_code(
        "ADMIN_LOG_INTEGRITY",
        lambda: st.admin_op(staff_install,
                            st.admin_command("add", staff, key=key, display_name=name)),
    )
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


def test_dad8_valid_historical_replay_control(staff_install):
    """Positive control for DA-D7: the same rows, uncorrupted, replay exactly. The
    staff member does not even exist now: replay never consults current state."""

    _ready(staff_install)
    staff, key, name = st.new_uuid(), st.new_uuid(), "Stored Name"
    key_row, log_row = _valid_rows(staff_install, staff, key, name)
    _insert_rows(staff_install, key_row, log_row)
    before = st.state(staff_install)
    assert st.admin_op(staff_install,
                       st.admin_command("add", staff, key=key, display_name=name)) \
        == log_row["result"]
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


def test_dad1b_literal_vectors_through_sql(staff_install):
    """The three literal digest vectors executed by the SQL itself: tenant
    `clinic-demo-01` set as the transaction tenant context, then each stored key
    digest compared with its literal hash."""

    _ready(staff_install)
    with st.runtime_connection(staff_install) as conn:
        for (tenant, key, actor, operation, staff, name, access), _ in st.DIGEST_VECTORS:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            st.call_on(conn, staff_install, "m01_staff_admin", st.admin_command(
                operation, staff, key=key, actor=actor, display_name=name, access=access))
            conn.commit()
    for (_tenant, key, *_rest), expected in st.DIGEST_VECTORS:
        rows = st.admin_rows(staff_install, sql.SQL(
            "SELECT pg_catalog.encode(digest, 'hex') FROM {}.m01_staff_action_keys "
            "WHERE client_action_id = %s").format(sql.Identifier(staff_install.schema)), (key,))
        assert rows == [(expected,)], key


# ---------------------------------------------------------------------------
# AM-1..AM-3: directory mutants (each must make its discriminating test fail)
# ---------------------------------------------------------------------------


@pytest.fixture
def mutant_install(request, m02_ids, m05_reset):
    created: list[tuple[str, str]] = []

    def make(label: str, replacements: tuple[tuple[str, str], ...]) -> st.MutantInstall:
        import m02_support as m02

        tenant = m02.tenant_for(f"{request.node.name}:{label}")
        m05_reset(tenant)
        created.append(tenant)
        install = st.MutantInstall(m02_ids, tenant, replacements)
        install.require()
        return install

    yield make
    for tenant in created:
        m05_reset(tenant)


def _rename_under_rr_with_holder(install: st.StaffInstall) -> psycopg.Error | None:
    """The DT-08 scenario: a competing holder owns the membership row; the admin
    call runs under REPEATABLE READ with the safety lock_timeout. Returns the error
    raised (or None) and how long it took is checked by the caller."""

    staff = st.new_uuid()
    st.seed_staff(install, staff)
    with (
        st.holder(install, [("m01_staff", "staff_id", staff)]),
        st.runtime_connection(install) as conn,
    ):
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        st.set_tenant(conn, install)
        conn.execute(f"SET LOCAL lock_timeout = '{st.SAFETY_LOCK_TIMEOUT}'")
        try:
            st.call_on(conn, install, "m01_staff_admin",
                       st.admin_command("rename", staff, display_name="Z"))
        except psycopg.Error as error:
            conn.rollback()
            return error
        conn.rollback()
    return None


def test_am1_guard_after_lock_is_caught(mutant_install, staff_install):
    """AM-1: guard moved after the membership lock. Pinned observation: the
    mutant WAITS on the held row and fails with 55P03 (lock_timeout), where the
    clean implementation refuses at once with ISOLATION_UNSUPPORTED."""

    _ready(staff_install)
    control = _rename_under_rr_with_holder(staff_install)
    assert control is not None and control.sqlstate == "P0001"
    assert control.diag.message_primary == "ISOLATION_UNSUPPORTED"

    install = mutant_install("am1", st.MUTANT_GUARD_AFTER_LOCK)
    mutant = _rename_under_rr_with_holder(install)
    assert mutant is not None and mutant.sqlstate == "55P03", mutant


def test_am2_admission_without_on_conflict_is_caught(mutant_install):
    """Without ON CONFLICT, the DA-K1 contender fails on the unique key instead of
    replaying: the DA-K1 assertion ('no error, same result') must fail."""

    install = mutant_install("am2", st.MUTANT_NO_ON_CONFLICT)
    staff, key = st.new_uuid(), st.new_uuid()
    command = st.admin_command("add", staff, key=key, display_name="K1")
    _, box = _contend(install, command, command, commit=True)
    error = box.get("error")
    assert isinstance(error, psycopg.Error), "mutant replayed: AM-2 not discriminated"
    assert error.sqlstate == "23505"


def test_am3_weak_replay_check_is_caught(mutant_install, staff_install):
    """AM-3: the fe092ed replay check. Pinned observation: with Codex's
    counterexample stored, the mutant RETURNS that corrupt result as success; the
    clean implementation refuses with ADMIN_LOG_INTEGRITY. Both leave state
    unchanged."""

    def scenario(install: st.StaffInstall) -> Any:
        staff, key, name = st.new_uuid(), st.new_uuid(), "Stored Name"
        key_row, log_row = _valid_rows(install, staff, key, name)
        _CORRUPTIONS["codex_counterexample"](key_row, log_row)
        _insert_rows(install, key_row, log_row)
        before = st.state(install)
        try:
            outcome: Any = st.admin_op(install, st.admin_command("add", staff, key=key,
                                                                 display_name=name))
        except psycopg.Error as error:
            outcome = error
        st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)
        return outcome, log_row["result"]

    _ready(staff_install)
    control, _ = scenario(staff_install)
    assert isinstance(control, psycopg.Error)
    assert control.diag.message_primary == "ADMIN_LOG_INTEGRITY"

    install = mutant_install("am3", st.mutant_weak_replay())
    mutant, stored = scenario(install)
    assert mutant == stored  # the corrupt row came back as a success
    assert mutant["schema_version"] == 999 and mutant["staff"] is None


# Codex 063f4ac review: impossible calendar timestamps, and the full name rule on
# every stored snapshot (here a deactivate, whose command carries no name).
_TIMESTAMP_CORRUPTIONS = {
    "feb_30": "2026-02-30T09:30:00.000000Z",
    "feb_29_non_leap": "2027-02-29T09:30:00.000000Z",
    "month_13": "2026-13-01T09:30:00.000000Z",
    "hour_25": "2026-01-01T25:00:00.000000Z",
    "minute_60": "2026-01-01T09:60:00.000000Z",
}
_NON_ADD_NAME_CORRUPTIONS = {
    "leading_space": " Dana",
    "trailing_nbsp": "Dana\u00a0",
    "not_nfc": "Zoe\u0308",
    "control_inside": "Da\u0001na",
    "line_separator": "Da\u2028na",
    "81_code_points": "x" * 81,
}


@pytest.mark.parametrize("label", sorted(_TIMESTAMP_CORRUPTIONS))
@pytest.mark.parametrize("operation", ["add", "deactivate"])
def test_dad7_impossible_timestamp_never_replays(staff_install, operation, label):
    _ready(staff_install)
    staff, key, name = st.new_uuid(), st.new_uuid(), "Stored Name"
    key_row, log_row = _valid_rows(staff_install, staff, key, name, operation=operation,
                                   timestamp=_TIMESTAMP_CORRUPTIONS[label])
    _insert_rows(staff_install, key_row, log_row)
    before = st.state(staff_install)
    st.expect_code("ADMIN_LOG_INTEGRITY", lambda: st.admin_op(
        staff_install, _replay_command(staff, key, name, operation)))
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


@pytest.mark.parametrize("label", sorted(_NON_ADD_NAME_CORRUPTIONS))
def test_dad7_non_add_snapshot_name_rule(staff_install, label):
    _ready(staff_install)
    staff, key = st.new_uuid(), st.new_uuid()
    name = _NON_ADD_NAME_CORRUPTIONS[label]
    key_row, log_row = _valid_rows(staff_install, staff, key, name, operation="deactivate")
    _insert_rows(staff_install, key_row, log_row)
    before = st.state(staff_install)
    st.expect_code("ADMIN_LOG_INTEGRITY", lambda: st.admin_op(
        staff_install, _replay_command(staff, key, name, "deactivate")))
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)


@pytest.mark.parametrize(
    "operation, timestamp",
    [("add", "2028-02-29T09:30:00.000000Z"), ("deactivate", "2028-02-29T23:59:59.999999Z"),
     ("deactivate", "2026-10-01T09:30:00.000000Z")],
    ids=["add_leap_day", "deactivate_leap_day_end", "deactivate_ordinary"],
)
def test_dad8_valid_controls_leap_day_and_non_add(staff_install, operation, timestamp):
    _ready(staff_install)
    staff, key, name = st.new_uuid(), st.new_uuid(), "Stored Name"
    key_row, log_row = _valid_rows(staff_install, staff, key, name, operation=operation,
                                   timestamp=timestamp)
    _insert_rows(staff_install, key_row, log_row)
    before = st.state(staff_install)
    assert st.admin_op(staff_install, _replay_command(staff, key, name, operation)) \
        == log_row["result"]
    st.Delta(before, st.state(staff_install)).untouched(st.ALL_TABLES)
