"""MA-10 staff directory test support (UC1 part B).

Package v1 + v1r1 delta + v1r2 adoption (Claude/Codex aligned 2026-11-01).

The directory is installed through its OWN test registry: production units + M05
units + the directory units t021..t024 (target 24), approving the M05 owner and
the staff owner. The existing M05 harness (target 20) is deliberately unchanged
in the red tranche, so rows that already pass keep their exact pinned unit lists;
the implementation tranche moves it over when M05 starts calling the directory.

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
from m05_harness import m05_manifest_document
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
)
STAFF_TYPED_UNITS: Final = {
    "t022_m01_staff_assignable": "m01_staff_assignable",
    "t023_m01_staff_admin": "m01_staff_admin",
    "t024_m01_staff_display": "m01_staff_display",
}
STAFF_TEST_TARGET: Final = 24
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


# ---------------------------------------------------------------------------
# Registry, manifest, install
# ---------------------------------------------------------------------------


def staff_registry() -> Any:
    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry
    from haloflow.m01.staff.units import STAFF_GATEWAY_PROFILES, STAFF_TENANT_MIGRATIONS
    from haloflow.m05.gateways import M05_GATEWAY_PROFILES
    from haloflow.m05.units import M05_TENANT_MIGRATIONS

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        M05_TENANT_MIGRATIONS,
        STAFF_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES | {M05_OWNER, STAFF_OWNER},
        installed_state_profiles={
            **APPROVED_INSTALLED_STATE_PROFILES,
            **M05_GATEWAY_PROFILES,
            **STAFF_GATEWAY_PROFILES,
        },
    )


def staff_manifest() -> Any:
    """The M05 test manifest plus the staff owner at USAGE+CREATE (it creates its
    three functions; production stays USAGE until B5, as for the M05 owner)."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    document = m05_manifest_document()
    document["execution_role_profiles"][STAFF_OWNER]["tenant_schema_privileges"] = [
        "USAGE",
        "CREATE",
    ]
    return load_provisioning_manifest(document)


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
