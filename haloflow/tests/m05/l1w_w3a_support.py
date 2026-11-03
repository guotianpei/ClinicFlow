"""L-1W W3a test support (contract v1, Codex 215519Z). Test-only.

Installs through the production registry (staff_support.StaffInstall) and calls the
W3a v2 gateways through the runtime login shim. Role rows can be seeded through the
admin connection: this is FIXTURE SETUP (labelled at each call site), used only where
a test needs a role holder before the real paths exist; the real bootstrap and the
real admin grant path are proven separately (BS-01, C-04+).
"""

from __future__ import annotations

from typing import Any, Final

import m05_support as m05
import staff_support as st
from psycopg import sql
from test_m05_actions_postgres import _cmd
from test_m05_create_postgres import _command, _create, _ready, _source

ADMIN_V2: Final = "m01_staff_admin_v2"
PREPARE_V2: Final = "m05_prepare_action_v2"
APPLY_V2: Final = "m05_apply_action_v2"
SELF_STANDING: Final = "m01_staff_self_standing"
BOOTSTRAP: Final = "m01_staff_bootstrap_admin"


def ready_with_staff(install: st.StaffInstall) -> None:
    """Install, then seed S1, S2, SUP and ADMIN as active members with read+act
    (through the still-callable v1 admin gateway; W3a does not close v1)."""

    _ready(install)
    rows = st.admin_rows(
        install,
        sql.SQL("SELECT count(*) FROM {}.m01_staff WHERE staff_id = %s").format(
            sql.Identifier(install.schema)), (m05.SUP,),
    )
    if rows == [(0,)]:
        for actor in (m05.S1, m05.S2, m05.SUP):
            st.seed_staff(install, actor, name=f"Actor {actor[-4:]}")


def ensure_staff(install: st.StaffInstall, staff_id: str) -> None:
    rows = st.admin_rows(
        install,
        sql.SQL("SELECT count(*) FROM {}.m01_staff WHERE staff_id = %s").format(
            sql.Identifier(install.schema)), (staff_id,),
    )
    if rows == [(0,)]:
        st.seed_staff(install, staff_id, name=f"Staff {staff_id[-4:]}")


def normalize_staff(install: st.StaffInstall, staff_id: str) -> None:
    """FIXTURE SETUP (through the still-callable v1 admin gateway): an existing member
    is made active with read and act and holds no role row, so module-scoped rows that
    change it do not leak into the next row."""

    status = st.admin_rows(
        install,
        sql.SQL("SELECT status FROM {}.m01_staff WHERE staff_id = %s").format(
            sql.Identifier(install.schema)), (staff_id,),
    )
    if status == [("inactive",)]:
        st.admin_op(install, st.admin_command("reactivate", staff_id))
    held = {a for (a,) in st.admin_rows(
        install,
        sql.SQL("SELECT access FROM {}.m01_staff_followup_access WHERE staff_id = %s").format(
            sql.Identifier(install.schema)), (staff_id,))}
    for access in ("read", "act"):
        if access not in held:
            st.admin_op(install, st.admin_command("grant", staff_id, access=access))
    for role in ("supervisor", "clinic_admin"):
        drop_role(install, staff_id, role)


def seed_role(install: st.StaffInstall, staff_id: str, role: str) -> None:
    """FIXTURE SETUP (escape hatch, admin connection): insert one role row."""

    st.admin_rows(
        install,
        sql.SQL(
            "INSERT INTO {}.m01_staff_roles (staff_id, role, granted_by, granted_at)"
            " VALUES (%s, %s, 'fixture', clock_timestamp()) ON CONFLICT DO NOTHING"
        ).format(sql.Identifier(install.schema)),
        (staff_id, role),
    )


def drop_role(install: st.StaffInstall, staff_id: str, role: str) -> None:
    """FIXTURE SETUP (escape hatch): remove one role row."""

    st.admin_rows(
        install,
        sql.SQL("DELETE FROM {}.m01_staff_roles WHERE staff_id = %s AND role = %s").format(
            sql.Identifier(install.schema)),
        (staff_id, role),
    )


def seed_service_roles(install: st.StaffInstall) -> None:
    """FIXTURE SETUP for the service suites after statement repointing (red packet
    section 3): StaffService.admin reaches m01_staff_admin_v2, so the admin actor
    st.ADMIN is a member holding clinic_admin; FollowupService supervise reaches the v2
    actions, so m05.SUP holds supervisor. No scenario changes otherwise."""

    ensure_staff(install, st.ADMIN)
    seed_role(install, st.ADMIN, "clinic_admin")
    seed_role(install, m05.SUP, "supervisor")


def roles(install: st.StaffInstall) -> list[tuple[Any, ...]]:
    return st.admin_rows(
        install,
        sql.SQL("SELECT staff_id::text, role FROM {}.m01_staff_roles ORDER BY 1, 2").format(
            sql.Identifier(install.schema)),
    )


def item(install: st.StaffInstall) -> str:
    return str(_create(install, _command(_source(install)))["item_id"])


def action(item_id: str, version: int, action_type: str, *, actor: str,
           assignee: str | None = None, supervise: bool = True,
           key: str | None = None) -> dict[str, Any]:
    permissions = {"read": True, "act": True, "supervise": supervise}
    return _cmd(item_id, version, action_type, key=key, actor=actor, assignee=assignee,
                permissions=permissions)


def apply_v2(install: st.StaffInstall, command: Any) -> dict[str, Any]:
    result: dict[str, Any] = st.call(install, APPLY_V2, command)
    return result


def prepare_v2(install: st.StaffInstall, command: Any) -> dict[str, Any]:
    result: dict[str, Any] = st.call(install, PREPARE_V2, command)
    return result


def admin_v2(install: st.StaffInstall, command: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = st.call(install, ADMIN_V2, command)
    return result


def role_command(operation: str, target: str, role: str, *, actor: str = st.ADMIN,
                 staff_admin: bool = True, key: str | None = None) -> dict[str, Any]:
    return st.admin_command(operation, target, actor=actor, staff_admin=staff_admin, key=key,
                            extra={"role": role})
