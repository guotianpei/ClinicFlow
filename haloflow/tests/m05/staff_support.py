"""MA-10 staff directory test support (UC1 part B).

Package v1 + v1r1 delta + v1r2 adoption (Claude/Codex aligned 2026-11-01).

The directory was installed through its OWN test registry: part A units + M05
units + the directory units t021..t025 (target 25), approving the M05 owner and
the staff owner. B5 composes exactly that into production, so (harness migration
H1, Codex 319b5f3) `staff_registry()` and `staff_manifest()` ARE the production
registry and manifest, never a second composition on top of them. The M05
harness keeps its isolated target-20 registry (H2).

Every gateway call takes the intended path: runtime login shim, SET ROLE runtime,
`app.tenant_id` set transaction-locally. Snapshots and seeding faults use the
test admin connection.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Final

import m02_support
import m05_support as m05
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

STAFF_OWNER: Final = "haloflow_staff_owner"
RUNTIME: Final = m05.RUNTIME
MIGRATOR: Final = m05.MIGRATOR
M05_OWNER: Final = m05.M05_OWNER

STAFF_UNIT_IDS: Final = (
    "t021_m01_staff_directory",
    "t022_m01_staff_assignable",
    "t023_m01_staff_admin",
    "t024_m01_staff_display",
    "t025_m01_staff_followup_reader",
)
STAFF_TYPED_UNITS: Final = {
    "t022_m01_staff_assignable": "m01_staff_assignable",
    "t023_m01_staff_admin": "m01_staff_admin",
    "t024_m01_staff_display": "m01_staff_display",
    "t025_m01_staff_followup_reader": "m01_staff_followup_reader",
}
STAFF_TEST_TARGET: Final = 32  # B3b adds t025; L-1W W3a composes t026-t032
STAFF_TEST_SUPPORTED: Final = range(STAFF_TEST_TARGET, STAFF_TEST_TARGET + 1)

TABLES: Final = (
    "m01_staff",
    "m01_staff_followup_access",
    "m01_staff_action_keys",
    "m01_staff_admin_log",
)
M05_CONTENT_TABLES: Final = (
    "m05_followup_items",
    "m05_followup_history",
    "m05_followup_actions",
    "m05_followup_notes",
)

# v1 3.3: (function, argument types, return type, volatility, EXECUTE grantee).
FUNCTIONS: Final = {
    "m01_staff_assignable": (("uuid",), "bool", "v", M05_OWNER),
    "m01_staff_admin": (("jsonb",), "jsonb", "v", RUNTIME),
    "m01_staff_display": (("jsonb",), "jsonb", "s", RUNTIME),
    # B3b t025 (package v2 1.2): STABLE, no lock, EXECUTE to the runtime only.
    "m01_staff_followup_reader": (("uuid",), "bool", "s", RUNTIME),
}

# v1 3.2 + v1r2 correction 3: the staff owner's exact table grants.
OWNER_TABLE_ACL: Final = {
    "m01_staff": ("INSERT", "SELECT"),
    "m01_staff_followup_access": ("DELETE", "INSERT", "SELECT"),
    "m01_staff_action_keys": ("INSERT", "SELECT"),
    "m01_staff_admin_log": ("INSERT",),
}
OWNER_COLUMN_ACL: Final = {
    ("m01_staff", "UPDATE"): ("display_name", "status", "status_changed_at"),
    ("m01_staff_admin_log", "SELECT"): (
        "actor_id", "after", "client_action_id", "digest", "operation", "result", "staff_id",
    ),
}

# v1r1 delta 3: the trusted identities and every ordinary role.
TRUSTED: Final = (STAFF_OWNER, MIGRATOR, "haloflow_provisioner")
ORDINARY_ROLES: Final = (
    RUNTIME,
    M05_OWNER,
    "haloflow_m02_lock_owner",
    "haloflow_m02_append_owner",
    "haloflow_m02_projection_owner",
    "haloflow_m02_read_owner",
    "haloflow_support_ro",
    "haloflow_breakglass_ro",
    "haloflow_breakglass_rw",
    "haloflow_audit_projector",
    "haloflow_control_audit_writer",
    "haloflow_owner",
)

OPERATIONS: Final = ("add", "rename", "deactivate", "reactivate", "grant", "revoke")
ADMIN_ERROR_CODES: Final = (
    "COMMAND_MALFORMED",
    "NOT_AUTHORIZED",
    "PRECONDITION_FAILED",
    "ACTION_KEY_CONFLICT",
    "ACTION_KEY_ACTOR_MISMATCH",
    "ISOLATION_UNSUPPORTED",
    "COMPOSITION_UNSUPPORTED",
    "ADMIN_LOG_INTEGRITY",
)
SNAPSHOT_KEYS: Final = (
    "access", "display_name", "schema_version", "staff_id", "status", "status_changed_at",
)
RESULT_KEYS: Final = ("client_action_id", "operation", "schema_version", "staff")
DISPLAY_ENTRY_KEYS: Final = ("display_name", "staff_id", "status")
DISPLAY_MAX_IDS: Final = 100
TIMESTAMP_PATTERN: Final = m05.TIMESTAMP_PATTERN

ADMIN: Final = "00000000-0000-4000-8000-0000000a0001"
ADMIN2: Final = "00000000-0000-4000-8000-0000000a0002"

# v1r2 correction 4: the Unicode White_Space set, enumerated.
WHITE_SPACE: Final = (
    [chr(c) for c in range(0x09, 0x0E)]
    + [" ", "\u0085", " ", " "]
    + [chr(c) for c in range(0x2000, 0x200B)]
    + [" ", " ", " ", " ", "　"]
)

# v1r1 delta 2: literal digest vectors (Python and PG16 agree; Codex reproduced).
DIGEST_VECTORS: Final = (
    (("clinic-demo-01", "3f2b8c1e-0000-4000-8000-000000000001",
      "7a1d0c2e-0000-4000-8000-0000000000a1", "add",
      "5c9e4b7a-0000-4000-8000-0000000000b1", "Dana Ortiz", None),
     "15c7c9176a09179d81bf281a15ae18bb4f1eb8b2945c63b690943dc019325376"),
    (("clinic-demo-01", "3f2b8c1e-0000-4000-8000-000000000002",
      "7a1d0c2e-0000-4000-8000-0000000000a1", "grant",
      "5c9e4b7a-0000-4000-8000-0000000000b1", None, "act"),
     "71c01da474ba665fbdee41b521e78db6fff074794c3c359c0cb38f08b6a84b85"),
    (("clinic-demo-01", "3f2b8c1e-0000-4000-8000-000000000003",
      "7a1d0c2e-0000-4000-8000-0000000000a1", "rename",
      "5c9e4b7a-0000-4000-8000-0000000000b1", 'Zoë "Z" Ortiz', None),
     "b6096287ac8c8525faed3993d1769d9873d3b1fcce09e633d2c0f22e6277be02"),
)


def admin_digest_preimage(
    tenant: str, key: str, actor: str, operation: str, staff: str,
    display_name: str | None, access: str | None,
) -> str:
    elements = ["m01.staff_admin/v1", tenant, key, actor, operation, staff, display_name, access]
    return "[" + ",".join(
        json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in elements
    ) + "]"


def admin_digest(*parts: Any) -> bytes:
    return hashlib.sha256(admin_digest_preimage(*parts).encode("utf-8")).digest()


def admin_digest_v2(
    tenant: str, key: str, actor: str, operation: str, staff: str,
    display_name: str | None, access: str | None,
) -> bytes:
    """L-1W W3a m01_staff_admin_v2 key digest (written out independently): the v1
    elements plus role, issuer, subject, setting and value, which a v1-operation
    command never carries (null)."""

    elements = ["m01.staff_admin/v2", tenant, key, actor, operation, staff, display_name, access,
                None, None, None, None, None]
    preimage = "[" + ",".join(
        json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in elements) + "]"
    return hashlib.sha256(preimage.encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# Registry, manifest, install
# ---------------------------------------------------------------------------


def staff_registry() -> Any:
    """The production registry (B5 H1): part A + M05 + staff (+ L-1W W3a), target 32."""

    from haloflow.composition import build_production_tenant_migrations

    return build_production_tenant_migrations()


def staff_manifest() -> Any:
    """The production manifest (B5 H1). B3b's test manifest gave the M05 and staff
    owners USAGE+CREATE (they create their functions); B5 makes exactly those two
    changes in production, so the two documents are equal (measured)."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    return load_provisioning_manifest()


def _production_registry_with(
    m05_units: Any, staff_units: Any
) -> Any:
    """The production unit sets, roles and profiles, with the M05 and staff unit
    sets replaced (mutant registries). The profiles are keyed by migration id, so a
    mutated body keeps its unit's profile."""

    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        L1W_W3A_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry
    from haloflow.m02.units import M02_TENANT_MIGRATIONS

    # L-1W W3a: the production W3a set follows unchanged, so a mutant registry keeps
    # the production target (32).
    return build_tenant_migration_registry(
        TENANT_MIGRATIONS,
        M02_TENANT_MIGRATIONS,
        m05_units,
        staff_units,
        L1W_W3A_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
    )


class StaffInstall:
    """One tenant installed through the staff test registry on first `require()`.
    Deferred into the test body so a missing implementation fails each row in its
    own body (the class-B red rule)."""

    def __init__(self, ids: m02_support.Identities, tenant: tuple[str, str]) -> None:
        self.ids = ids
        self.tenant = tenant
        self._attempted = False
        self._error: Exception | None = None

    @property
    def schema(self) -> str:
        return self.tenant[1]

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                m02_support.provision_sync(
                    self.ids,
                    staff_registry(),
                    self.tenant,
                    supported=STAFF_TEST_SUPPORTED,
                    manifest=staff_manifest(),
                )
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"staff test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


def new_uuid() -> str:
    return str(uuid.uuid4())


def attribution(actor: str = ADMIN, *, kind: str = "actor", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "principal_kind": kind,
        "principal_id": actor,
        "auth_method": "test",
        "execution_id": "00000000-0000-4000-8000-00000000e001",
        "request_id": None,
        "purpose_code": "operations",
    }
    out.update(extra)
    return out


def admin_command(
    operation: str,
    staff_id: str,
    *,
    key: str | None = None,
    actor: str = ADMIN,
    display_name: str | None = None,
    access: str | None = None,
    staff_admin: bool = True,
    kind: str = "actor",
    extra: dict[str, Any] | None = None,
    attribution_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "schema_version": 1,
        "client_action_id": key or new_uuid(),
        "staff_id": staff_id,
        "operation": operation,
        "permissions": {"staff_admin": staff_admin},
        "attribution": attribution(actor, kind=kind, **(attribution_extra or {})),
    }
    if display_name is not None:
        command["display_name"] = display_name
    if access is not None:
        command["access"] = access
    command.update(extra or {})
    return command


def runtime_connection(
    install: StaffInstall, *, autocommit: bool = False
) -> psycopg.Connection[Any]:
    conn = psycopg.connect(install.ids.logins[RUNTIME], autocommit=autocommit)
    conn.execute(f"SET ROLE {RUNTIME}")
    if not autocommit:
        conn.commit()
    return conn


def set_tenant(conn: psycopg.Connection[Any], install: StaffInstall) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))


def call_on(conn: psycopg.Connection[Any], install: StaffInstall, function: str, arg: Any) -> Any:
    row = conn.execute(
        sql.SQL("SELECT {}.{}(%s)").format(
            sql.Identifier(install.schema), sql.Identifier(function)
        ),
        (Jsonb(arg) if not isinstance(arg, str) else arg,),
    ).fetchone()
    assert row is not None
    return row[0]


def call(install: StaffInstall, function: str, arg: Any, *, isolation: str | None = None) -> Any:
    with psycopg.connect(install.ids.logins[RUNTIME], autocommit=True) as conn:
        conn.execute(f"SET ROLE {RUNTIME}")
        with conn.transaction():
            if isolation is not None:
                conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            set_tenant(conn, install)
            return call_on(conn, install, function, arg)


def admin_op(install: StaffInstall, command: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = call(install, "m01_staff_admin", command, **kwargs)
    return result


def display(install: StaffInstall, staff_ids: list[str], **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = call(
        install, "m01_staff_display", {"schema_version": 1, "staff_ids": staff_ids}, **kwargs
    )
    return result


def raises_code(code: str) -> Callable[[Callable[[], Any]], None]:
    def check(thunk: Callable[[], Any]) -> None:
        try:
            thunk()
        except psycopg.Error as error:
            assert error.sqlstate == "P0001", (error.sqlstate, str(error))
            assert error.diag.message_primary == code, error.diag.message_primary
            return
        raise AssertionError(f"expected P0001 {code}, call succeeded")

    return check


def expect_code(code: str, thunk: Callable[[], Any]) -> None:
    raises_code(code)(thunk)


def seed_staff(
    install: StaffInstall,
    staff_id: str,
    *,
    name: str = "Test Staff",
    access: tuple[str, ...] = ("read", "act"),
    active: bool = True,
) -> None:
    """Seed one staff member THROUGH THE ADMIN GATEWAY (the definer boundary)."""

    admin_op(install, admin_command("add", staff_id, display_name=name))
    for value in access:
        admin_op(install, admin_command("grant", staff_id, access=value))
    if not active:
        admin_op(install, admin_command("deactivate", staff_id))


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def admin_rows(install: StaffInstall, statement: Any, params: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(install.ids.admin, autocommit=True) as conn:
        cursor = conn.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def table_content(install: StaffInstall, table: str) -> list[str]:
    return sorted(
        row[0]
        for row in admin_rows(
            install,
            sql.SQL("SELECT to_jsonb(t)::text FROM {}.{} AS t").format(
                sql.Identifier(install.schema), sql.Identifier(table)
            ),
        )
    )


def directory_snapshot(install: StaffInstall) -> dict[str, list[str]]:
    return {table: table_content(install, table) for table in TABLES}


def m05_snapshot(install: StaffInstall) -> dict[str, list[str]]:
    return {table: table_content(install, table) for table in M05_CONTENT_TABLES}


def full_snapshot(install: StaffInstall) -> dict[str, list[str]]:
    return {**directory_snapshot(install), **m05_snapshot(install)}


def added_rows(before: list[str], after: list[str]) -> list[dict[str, Any]]:
    remaining = list(before)
    added: list[dict[str, Any]] = []
    for row in after:
        if row in remaining:
            remaining.remove(row)
        else:
            added.append(json.loads(row))
    assert not remaining, f"rows removed: {remaining}"
    return added


# ---------------------------------------------------------------------------
# Concurrency (blocker identity, bounded; never sleep as proof)
# ---------------------------------------------------------------------------

BLOCK_BOUND_SECONDS: Final = 5.0
POLL_SECONDS: Final = 0.02


def wait_until_blocked_by(install: StaffInstall, waiter_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + BLOCK_BOUND_SECONDS
    with psycopg.connect(install.ids.admin, autocommit=True) as monitor:
        while time.monotonic() < deadline:
            row = monitor.execute(
                "SELECT pg_catalog.pg_blocking_pids(%s)", (waiter_pid,)
            ).fetchone()
            if row is not None and list(row[0]) == [blocker_pid]:
                return
            time.sleep(POLL_SECONDS)
    raise AssertionError(f"pid {waiter_pid} not blocked by exactly {blocker_pid} within bound")


@contextmanager
def background(fn: Callable[[], Any]) -> Iterator[dict[str, Any]]:
    """Run fn in a thread; the box gets 'result' or 'error'. Joined on exit."""

    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except Exception as error:  # noqa: BLE001 - asserted by the caller
            box["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield box
    finally:
        thread.join(BLOCK_BOUND_SECONDS * 2)
        assert not thread.is_alive(), "background call did not finish"


# ---------------------------------------------------------------------------
# Exact state and intended-delta checks (red review F-MA10-RED-02)
# ---------------------------------------------------------------------------

PRIMARY_KEYS: Final = {
    "m01_staff": ("staff_id",),
    "m01_staff_followup_access": ("staff_id", "access"),
    "m01_staff_action_keys": ("client_action_id",),
    "m01_staff_admin_log": ("log_id",),
    "m05_followup_items": ("item_id",),
    "m05_followup_history": ("history_id",),
    "m05_followup_actions": ("item_id", "client_action_id"),
    "m05_followup_notes": ("note_id",),
}
ALL_TABLES: Final = TABLES + M05_CONTENT_TABLES
State = dict[str, dict[tuple[Any, ...], dict[str, Any]]]


def state(install: StaffInstall) -> State:
    """Every row of all eight tables, keyed by primary key."""

    out: State = {}
    for table in ALL_TABLES:
        rows = [json.loads(r) for r in table_content(install, table)]
        out[table] = {tuple(r[c] for c in PRIMARY_KEYS[table]): r for r in rows}
    return out


class Delta:
    def __init__(self, before: State, after: State) -> None:
        self.added: dict[str, list[dict[str, Any]]] = {}
        self.removed: dict[str, list[dict[str, Any]]] = {}
        self.changed: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for table in ALL_TABLES:
            b, a = before[table], after[table]
            self.added[table] = [a[k] for k in a if k not in b]
            self.removed[table] = [b[k] for k in b if k not in a]
            self.changed[table] = [(b[k], a[k]) for k in a if k in b and a[k] != b[k]]

    def untouched(self, tables: tuple[str, ...]) -> None:
        for table in tables:
            assert not self.added[table], (table, "added", self.added[table])
            assert not self.removed[table], (table, "removed", self.removed[table])
            assert not self.changed[table], (table, "changed", self.changed[table])


def parse_ts(value: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def bytea_hex(value: bytes) -> str:
    return "\\x" + value.hex()


def staff_snapshot_from(st_before: State, staff_id: str) -> dict[str, Any] | None:
    """The closed staff snapshot as it stood in a captured state."""

    row = st_before["m01_staff"].get((staff_id,))
    if row is None:
        return None
    access = sorted(
        r["access"] for r in st_before["m01_staff_followup_access"].values()
        if r["staff_id"] == staff_id
    )
    return {
        "schema_version": 1, "staff_id": staff_id, "status": row["status"],
        "display_name": row["display_name"], "access": access,
        "status_changed_at": row["status_changed_at"],
    }


def assert_snapshot_equal(actual: dict[str, Any] | None, expected: dict[str, Any] | None) -> None:
    if expected is None:
        assert actual is None
        return
    assert actual is not None
    assert {k: v for k, v in actual.items() if k != "status_changed_at"} == {
        k: v for k, v in expected.items() if k != "status_changed_at"
    }
    assert parse_ts(actual["status_changed_at"]) == parse_ts(expected["status_changed_at"])


def check_admin_delta(
    delta: Delta,
    before: State,
    install: StaffInstall,
    command: dict[str, Any],
    result: dict[str, Any],
    *,
    v2: bool = False,
) -> None:
    """Exactly the intended directory change for one admitted admin command; the
    caller asserts the M05 tables separately. ``v2`` (L-1W W3a): the command went
    through m01_staff_admin_v2 (the service route), whose key digest is v2 and whose
    SQL result adds "outcome" to the logged four-key result."""

    staff, operation = command["staff_id"], command["operation"]
    key, actor = command["client_action_id"], command["attribution"]["principal_id"]
    snap = result["staff"]
    # m01_staff
    if operation == "add":
        assert not delta.changed["m01_staff"] and not delta.removed["m01_staff"]
        (row,) = delta.added["m01_staff"]
    else:
        assert not delta.added["m01_staff"] and not delta.removed["m01_staff"]
        changed = delta.changed["m01_staff"]
        if operation in ("grant", "revoke"):
            assert changed == []
            row = before["m01_staff"][(staff,)]
        else:
            ((old, row),) = changed
            assert old["staff_id"] == staff
            for field in ("display_name",) if operation != "rename" else ():
                assert row[field] == old[field]
            if operation == "rename":
                assert row["status"] == old["status"]
                assert row["status_changed_at"] == old["status_changed_at"]
    assert row["staff_id"] == staff
    # Expected values derived from the command and the pre-state, not only from the
    # returned snapshot (Codex gate 24e463b, non-blocking hardening).
    pre = staff_snapshot_from(before, staff)
    expected_status = {"add": "active", "deactivate": "inactive", "reactivate": "active"}.get(
        operation, pre["status"] if pre else None
    )
    expected_name = (
        command["display_name"] if operation in ("add", "rename")
        else (pre["display_name"] if pre else None)
    )
    assert row["status"] == expected_status == snap["status"]
    assert row["display_name"] == expected_name == snap["display_name"]
    assert parse_ts(row["status_changed_at"]) == parse_ts(snap["status_changed_at"])
    # m01_staff_followup_access
    access_added = delta.added["m01_staff_followup_access"]
    access_removed = delta.removed["m01_staff_followup_access"]
    assert not delta.changed["m01_staff_followup_access"]
    if operation == "grant":
        assert [(r["staff_id"], r["access"]) for r in access_added] == [(staff, command["access"])]
        assert access_removed == []
    elif operation == "revoke":
        assert [(r["staff_id"], r["access"]) for r in access_removed] == [
            (staff, command["access"])
        ]
        assert access_added == []
    else:
        assert access_added == [] and access_removed == []
    # snapshot access agrees with the persisted rows
    after_access = sorted(
        [r["access"] for r in before["m01_staff_followup_access"].values()
         if r["staff_id"] == staff and r not in access_removed]
        + [r["access"] for r in access_added]
    )
    assert snap["access"] == after_access
    # keys and log
    digest = (admin_digest_v2 if v2 else admin_digest)(
        install.tenant[0], key, actor, operation, staff,
        command.get("display_name"), command.get("access"),
    )
    if v2:
        assert result.get("outcome") in ("applied", "replayed")
        result = {k: v for k, v in result.items() if k != "outcome"}
    assert not delta.changed["m01_staff_action_keys"] and not delta.removed["m01_staff_action_keys"]
    (key_row,) = delta.added["m01_staff_action_keys"]
    assert (key_row["client_action_id"], key_row["staff_id"], key_row["actor_id"],
            key_row["digest"]) == (key, staff, actor, bytea_hex(digest))
    assert not delta.changed["m01_staff_admin_log"] and not delta.removed["m01_staff_admin_log"]
    (log,) = delta.added["m01_staff_admin_log"]
    attribution_in = command["attribution"]
    assert (log["client_action_id"], log["staff_id"], log["operation"], log["actor_id"],
            log["execution_id"], log["request_id"], log["purpose_code"], log["digest"]) == (
        key, staff, operation, actor, attribution_in["execution_id"],
        attribution_in["request_id"], attribution_in["purpose_code"], bytea_hex(digest),
    )
    assert_snapshot_equal(log["before"], staff_snapshot_from(before, staff))
    assert log["after"] == snap and log["result"] == result


def check_assignment_delta(
    delta: Delta,
    before: State,
    install: StaffInstall,
    command: dict[str, Any],
    result: dict[str, Any],
    *,
    new_assignee: str,
) -> None:
    """Exactly the intended M05 change for one applied assign/reassign/claim; the
    caller asserts the directory tables separately."""

    item, key = command["item_id"], command["client_action_id"]
    actor = command["attribution"]["principal_id"]
    action = command["action"]["type"]
    assert result["outcome"] == "applied"
    ((old, new),) = delta.changed["m05_followup_items"]
    assert not delta.added["m05_followup_items"] and not delta.removed["m05_followup_items"]
    assert old["item_id"] == item
    assert new == {**old, "assignee_id": new_assignee, "version": old["version"] + 1}
    assert result["snapshot"]["version"] == new["version"]
    assert result["snapshot"]["assignee_id"] == new_assignee
    (history,) = delta.added["m05_followup_history"]
    assert not delta.changed["m05_followup_history"] and not delta.removed["m05_followup_history"]
    prior_seq = max(
        (h["seq"] for h in before["m05_followup_history"].values() if h["item_id"] == item),
        default=0,
    )
    closed = ("state", "assignee_id", "outcome_code")
    assert (history["item_id"], history["seq"], history["action"], history["actor_id"],
            history["resolution_event_id"], history["assertion_kind"]) == (
        item, prior_seq + 1, action, actor, None, None,
    )
    assert history["before"] == {c: old[c] for c in closed}
    assert history["after"] == {c: new[c] for c in closed}
    (action_row,) = delta.added["m05_followup_actions"]
    assert not delta.changed["m05_followup_actions"] and not delta.removed["m05_followup_actions"]
    digest = m05.action_digest(
        install.tenant[0], item, key, actor, action, command["action"].get("assignee_id"),
        command["expected_version"],
    )
    assert (action_row["item_id"], action_row["client_action_id"], action_row["actor_id"],
            action_row["requested_action"], action_row["digest"], action_row["hmac_key_version"],
            action_row["event_id"], action_row["result_snapshot"]) == (
        item, key, actor, action, bytea_hex(digest), None, None, result["snapshot"],
    )
    delta.untouched(("m05_followup_notes",))


# ---------------------------------------------------------------------------
# Competing lock holder (red review F-MA10-RED-03)
# ---------------------------------------------------------------------------

IMMEDIATE_SECONDS: Final = 1.0
SAFETY_LOCK_TIMEOUT: Final = "2s"


@contextmanager
def holder(install: StaffInstall, rows: list[tuple[str, str, Any]]) -> Iterator[int]:
    """A separate admin transaction holding FOR UPDATE on each (table, column,
    value) row until exit; yields its backend pid. Rolled back on every exit."""

    conn = psycopg.connect(install.ids.admin)
    try:
        for table, column, value in rows:
            locked = conn.execute(
                sql.SQL("SELECT 1 FROM {}.{} WHERE {} = %s FOR UPDATE").format(
                    sql.Identifier(install.schema), sql.Identifier(table),
                    sql.Identifier(column),
                ),
                (value,),
            ).fetchall()
            assert locked == [(1,)], (table, value)
        yield conn.info.backend_pid
    finally:
        conn.rollback()
        conn.close()


def row_locked_elsewhere(install: StaffInstall, table: str, column: str, value: Any) -> bool:
    """True when another transaction holds a conflicting lock on the row now."""

    with psycopg.connect(install.ids.admin) as conn:
        try:
            conn.execute(
                sql.SQL("SELECT 1 FROM {}.{} WHERE {} = %s FOR UPDATE NOWAIT").format(
                    sql.Identifier(install.schema), sql.Identifier(table),
                    sql.Identifier(column),
                ),
                (value,),
            )
            return False
        except psycopg.errors.LockNotAvailable:
            return True
        finally:
            conn.rollback()


def expect_immediate_code(
    conn: psycopg.Connection[Any], code: str, thunk: Callable[[], Any]
) -> None:
    """The call must refuse with `code` at once while a competing holder blocks its
    targets. A late rejection that first waited on the held lock hits the safety
    lock_timeout (55P03) or the elapsed bound and fails."""

    conn.execute(f"SET LOCAL lock_timeout = '{SAFETY_LOCK_TIMEOUT}'")
    started = time.monotonic()
    try:
        thunk()
    except psycopg.Error as error:
        elapsed = time.monotonic() - started
        assert error.sqlstate == "P0001", (error.sqlstate, str(error))
        assert error.diag.message_primary == code, error.diag.message_primary
        assert elapsed < IMMEDIATE_SECONDS, f"refusal waited {elapsed:.2f}s"
        return
    raise AssertionError(f"expected immediate P0001 {code}, call succeeded")


# ---------------------------------------------------------------------------
# Directory mutants (tranche-1 review): a mutated t023 body composed into its own
# registry and tenant, to show that the corresponding tests discriminate.
# ---------------------------------------------------------------------------


def mutant_registry(replacements: tuple[tuple[str, str], ...]) -> Any:
    from haloflow.m01.staff import units
    from haloflow.m05.units import M05_TENANT_MIGRATIONS

    body = units._ADMIN_BODY
    for old, new in replacements:
        assert body.count(old) == 1, ("mutation anchor not unique", old[:60])
        body = body.replace(old, new)
    assert body != units._ADMIN_BODY
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    mutated = dict(units.STAFF_TENANT_MIGRATIONS)
    mutated["t023_m01_staff_admin"] = units._definition(
        "m01_staff_admin", (("p_command", "jsonb"),), "jsonb", "volatile", RUNTIME, body, sha
    )
    return _production_registry_with(M05_TENANT_MIGRATIONS, mutated)


class MutantInstall(StaffInstall):
    def __init__(
        self,
        ids: m02_support.Identities,
        tenant: tuple[str, str],
        replacements: tuple[tuple[str, str], ...],
    ) -> None:
        super().__init__(ids, tenant)
        self.replacements = replacements

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            m02_support.provision_sync(
                self.ids,
                mutant_registry(self.replacements),
                self.tenant,
                supported=STAFF_TEST_SUPPORTED,
                manifest=staff_manifest(),
            )


ISOLATION_BLOCK: Final = (
    "\n    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN\n"
    "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ISOLATION_UNSUPPORTED';\n"
    "    END IF;\n"
)
MEMBERSHIP_LOCK: Final = "     WHERE s.staff_id = v_staff\n       FOR UPDATE;\n"
# AM-1: the isolation guard moved after the membership lock.
MUTANT_GUARD_AFTER_LOCK: Final = (
    (ISOLATION_BLOCK, "\n"),
    (MEMBERSHIP_LOCK, MEMBERSHIP_LOCK + ISOLATION_BLOCK),
)
# AM-2: key admission without ON CONFLICT (a concurrent identical request then
# fails on the unique key instead of replaying).
MUTANT_NO_ON_CONFLICT: Final = (
    ("    ON CONFLICT (client_action_id) DO NOTHING\n", ""),
)
REPLAY_STRICT_START: Final = "        v_result := v_log.result;\n        v_snap := v_log.after;\n"


def mutant_weak_replay() -> tuple[tuple[str, str], ...]:
    """AM-3: the replay-integrity validation reduced to the fe092ed check (object
    with the matching key only), the defect Codex found (F-MA10-IMPL-01)."""

    from haloflow.m01.staff import units

    body = units._ADMIN_BODY
    start = body.index(REPLAY_STRICT_START)
    end = body.index("        RETURN v_result;\n    END IF;\n\n    -- Admitted")
    weak = (
        "        v_result := v_log.result;\n"
        "        IF v_result IS NULL\n"
        "           OR pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'\n"
        "           OR (v_result OPERATOR(pg_catalog.->>) 'client_action_id')"
        " IS DISTINCT FROM v_key::text THEN\n"
        "            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ADMIN_LOG_INTEGRITY';\n"
        "        END IF;\n"
    )
    return ((body[start:end], weak),)


# ---------------------------------------------------------------------------
# Tranche-2 mutants (package v1 section 4 R-M1..R-M3, plus the M05 guard-moved
# mutants): any staff or M05 gateway body mutated by exact, unique anchors and
# composed into its own registry and tenant; production source is never modified.
# ---------------------------------------------------------------------------


def _mutated(body: str, replacements: tuple[tuple[str, str], ...]) -> str:
    out = body
    for old, new in replacements:
        assert out.count(old) == 1, ("mutation anchor not unique", old[:60])
        out = out.replace(old, new)
    assert out != body
    return out


def unit_mutant_registry(mutations: dict[str, tuple[tuple[str, str], ...]]) -> Any:
    """The staff test registry with each named unit's body mutated."""

    from haloflow.m01.staff import units as staff_units
    from haloflow.m05 import gateways
    from haloflow.m05.units import M05_TENANT_MIGRATIONS

    staff = dict(staff_units.STAFF_TENANT_MIGRATIONS)
    m05_units = dict(M05_TENANT_MIGRATIONS)
    staff_specs = {mid: spec for mid, *spec in staff_units._FUNCTIONS}
    m05_specs = {mid: spec for mid, *spec in gateways._FUNCTIONS}
    for migration_id, replacements in mutations.items():
        if migration_id in staff_specs:
            name, params, returns, volatility, grantee = staff_specs[migration_id]
            body = _mutated(staff_units._BODIES[name][0], replacements)
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            staff[migration_id] = staff_units._definition(
                name, params, returns, volatility, grantee, body, sha
            )
        else:
            name, params, volatility = m05_specs[migration_id]
            body = _mutated(gateways._BODIES[name][0], replacements)
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            m05_units[migration_id] = gateways._definition(name, params, volatility, body, sha)
    return _production_registry_with(m05_units, staff)


class UnitMutantInstall(StaffInstall):
    def __init__(
        self,
        ids: m02_support.Identities,
        tenant: tuple[str, str],
        mutations: dict[str, tuple[tuple[str, str], ...]],
    ) -> None:
        super().__init__(ids, tenant)
        self.mutations = mutations

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            m02_support.provision_sync(
                self.ids,
                unit_mutant_registry(self.mutations),
                self.tenant,
                supported=STAFF_TEST_SUPPORTED,
                manifest=staff_manifest(),
            )


ASSIGNABLE: Final = "t022_m01_staff_assignable"
ADMIN_UNIT: Final = "t023_m01_staff_admin"
APPLY_UNIT: Final = "t016_m05_apply_action"

# R-M1: the reader takes FOR KEY SHARE instead of FOR SHARE.
R_M1: Final = {ASSIGNABLE: (("       FOR SHARE;\n", "       FOR KEY SHARE;\n"),)}
# R-M2: the admin's membership lock removed (the row is still read; the status
# UPDATE then takes only its implicit FOR NO KEY UPDATE, and revoke touches no
# membership row at all).
R_M2: Final = {ADMIN_UNIT: ((MEMBERSHIP_LOCK, "     WHERE s.staff_id = v_staff;\n"),)}
# R-M1b: R-M1 and R-M2 together (the reader-side guarantee without the writer's
# explicit lock).
R_M1B: Final = {**R_M1, **R_M2}
# R-M3: the two assignable statements merged into one query whose access count is
# read under the snapshot taken before the lock wait.
_TWO_STATEMENTS: Final = (
    "    SELECT s.status INTO v_status\n"
    "      FROM {schema}.m01_staff AS s\n"
    "     WHERE s.staff_id = p_staff_id\n"
    "       FOR SHARE;\n"
)
_MERGED: Final = (
    "    SELECT s.status, (SELECT pg_catalog.count(*)::integer\n"
    "                        FROM {schema}.m01_staff_followup_access AS a\n"
    "                       WHERE a.staff_id = p_staff_id\n"
    "                         AND a.access IN ('read', 'act'))\n"
    "      INTO v_status, v_access\n"
    "      FROM {schema}.m01_staff AS s\n"
    "     WHERE s.staff_id = p_staff_id\n"
    "       FOR SHARE;\n"
)
_SECOND_STATEMENT: Final = (
    "    SELECT pg_catalog.count(*) INTO v_access\n"
    "      FROM {schema}.m01_staff_followup_access AS a\n"
    "     WHERE a.staff_id = p_staff_id\n"
    "       AND a.access IN ('read', 'act');\n"
)
R_M3: Final = {ASSIGNABLE: ((_TWO_STATEMENTS, _MERGED), (_SECOND_STATEMENT, ""))}

# GM-1: the M05 isolation guard moved after the item lock (apply).
ITEM_LOCK: Final = "     WHERE i.item_id = v_item\n       FOR UPDATE;\n"
GM_1: Final = {APPLY_UNIT: ((ISOLATION_BLOCK, "\n"), (ITEM_LOCK, ITEM_LOCK + ISOLATION_BLOCK))}
# GM-2: the M05 composition check (and its scope record) moved after the item lock.
COMPOSITION_BLOCK: Final = (
    "    v_prior := pg_catalog.current_setting('haloflow.txn_scope', true);\n"
    "    IF v_prior IS NOT NULL AND v_prior <> '' AND v_prior <> v_scope THEN\n"
    "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMPOSITION_UNSUPPORTED';\n"
    "    END IF;\n"
    "    PERFORM pg_catalog.set_config('haloflow.txn_scope', v_scope, true);\n"
)
GM_2: Final = {APPLY_UNIT: ((COMPOSITION_BLOCK, ""), (ITEM_LOCK, ITEM_LOCK + COMPOSITION_BLOCK))}


# B3b SQL mutants (package v2 7, SM-1/SM-2), applied to BOTH prepare and apply.
PREPARE_UNIT: Final = "t015_m05_prepare_action"
ACTOR_CHECK_APPLY: Final = (
    "    IF NOT {schema}.m01_staff_assignable(v_actor) THEN\n"
    "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NOT_AUTHORIZED';\n"
    "    END IF;\n"
)
ACTOR_CHECK_PREPARE: Final = (
    "    IF NOT {schema}.m01_staff_assignable(v_actor) THEN\n"
    "        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'decision', 'REFUSED',"
    " 'code', 'NOT_AUTHORIZED');\n"
    "    END IF;\n"
)
# SM-1: step 4b (the actor's directory check) removed.
SM_1: Final = {APPLY_UNIT: ((ACTOR_CHECK_APPLY, ""),),
               PREPARE_UNIT: ((ACTOR_CHECK_PREPARE, ""),)}
# SM-2: step 6e skipped for every target, not only target == actor.
TARGET_GUARD: Final = "    IF v_target IS NOT NULL AND v_target <> v_actor\n"
SM_2: Final = {APPLY_UNIT: ((TARGET_GUARD, "    IF false AND v_target <> v_actor\n"),),
               PREPARE_UNIT: ((TARGET_GUARD, "    IF false AND v_target <> v_actor\n"),)}
