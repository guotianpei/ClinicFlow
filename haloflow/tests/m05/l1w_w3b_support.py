"""L-1W W3b test support (plan v1r1 + addendum 1, Codex 151751Z). Test-only.

First-member bootstrap (owner decision OD-FM1, 2026-11-03): HaloVox support creates a
clinic's first staff member and grants clinic_admin in one audited step, only while the
clinic has no active clinic_admin.

t034 ``m01_staff_bootstrap_first_admin(p_command jsonb) -> jsonb``
    command  {"schema_version": 1, "client_action_id": uuid, "staff_id": uuid,
              "display_name": text}  (closed shape; any other key is COMMAND_MALFORMED)
    result   {"schema_version": 1, "outcome": <code>[, "replayed": true]}
    codes    BOOTSTRAPPED, ADMIN_EXISTS, TARGET_EXISTS, SUPPORT_NOT_AUTHORIZED,
             COMMAND_MALFORMED, ACTION_KEY_CONFLICT, ACTION_KEY_ACTOR_MISMATCH
    (t032's code names are reused for the key cases; t032 itself is unchanged.)

Each row gets a FRESH tenant (no staff at all), because m01_staff rows cannot be
deleted. Members that must pre-exist are inserted through the admin connection:
FIXTURE SETUP (escape hatch), labelled at each call site, never a production path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Final

import psycopg
import staff_support as st
from psycopg import sql
from psycopg.types.json import Jsonb

FIRST_ADMIN: Final = "m01_staff_bootstrap_first_admin"
BOOTSTRAP: Final = "m01_staff_bootstrap_admin"
CAP: Final = "l1w.roles.bootstrap"
DIGEST_NAMESPACE: Final = "m01.staff_bootstrap_first_admin/v1"

# Every table a first-member call may touch, for full-state refusal comparisons.
FM_TABLES: Final = (
    *st.TABLES,
    "m01_staff_roles",
    "m01_staff_bootstrap_log",
    "m01_staff_bootstrap_keys",
)


def command(staff_id: str, display_name: str = "First Admin", *, key: str | None = None,
            **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"schema_version": 1, "client_action_id": key or str(uuid.uuid4()),
                           "staff_id": staff_id, "display_name": display_name}
    out.update(extra)
    return out


def call_on(conn: psycopg.Connection[Any], install: st.StaffInstall, function: str,
            arg: dict[str, Any]) -> dict[str, Any]:
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
    row = conn.execute(
        sql.SQL("SELECT {}.{}(%s)").format(sql.Identifier(install.schema),
                                           sql.Identifier(function)),
        (Jsonb(arg),),
    ).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def snapshot(install: st.StaffInstall) -> dict[str, list[str]]:
    return {table: st.table_content(install, table) for table in FM_TABLES}


def rows(install: st.StaffInstall, table: str) -> list[dict[str, Any]]:
    return [json.loads(r) for r in st.table_content(install, table)]


def seed_member_raw(install: st.StaffInstall, staff_id: str, *, name: str = "Existing Staff",
                    status: str = "active") -> None:
    """FIXTURE SETUP (escape hatch, admin connection): one m01_staff row, no access, no
    role. Used only where a row needs a member to exist before the first admin."""

    st.admin_rows(
        install,
        sql.SQL("INSERT INTO {}.m01_staff (staff_id, status, status_changed_at, display_name)"
                " VALUES (%s, %s, clock_timestamp(), %s)").format(
            sql.Identifier(install.schema)),
        (staff_id, status, name),
    )


def seed_role_raw(install: st.StaffInstall, staff_id: str, role: str) -> None:
    """FIXTURE SETUP (escape hatch, admin connection): one role row."""

    st.admin_rows(
        install,
        sql.SQL("INSERT INTO {}.m01_staff_roles (staff_id, role, granted_by, granted_at)"
                " VALUES (%s, %s, 'fixture', clock_timestamp())").format(
            sql.Identifier(install.schema)),
        (staff_id, role),
    )


def expected_digest(tenant: str, key: str, actor: str, staff_id: str, display_name: str
                    ) -> bytes:
    """Pinned preimage (addendum 1, pin 1): grant_id and ticket are NOT bound."""

    elements = [DIGEST_NAMESPACE, tenant, key, actor, "create_first_admin", staff_id,
                display_name]
    preimage = "[" + ",".join(
        json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in elements) + "]"
    return hashlib.sha256(preimage.encode("utf-8")).digest()
