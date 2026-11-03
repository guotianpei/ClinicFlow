"""L-1W W3a implementation-time rows required by Codex 224307Z (support profile (d))
and 222540Z (service result mapping). Unit tier, no database.

Rows:
- SUP-01: haloflow_support is a USAGE-only, all-attributes-false profile with exactly
  the 008 migrator edge (SET true, INHERIT false, ADMIN false); it is a controlled
  role and is NOT an approved execution role.
- SUP-02: a typed unit whose execution role is haloflow_support is refused by the
  production registry build (the approved-role gate), not merely absent from a set.
- SUP-03: the controlled membership graph refuses an undeclared edge touching
  support, a missing support edge and a support edge with different options; the
  exact shipped graph passes.
- SUP-08: stage 1 reads the live attributes of every grant-only (USAGE-only,
  never a unit's role) profile -- support -- AFTER the registry roles,
  with the existing reads; an unsafe or missing support refuses with
  EXECUTION_ROLE_UNAVAILABLE even for an empty registry; a registry role without a
  profile is still refused; the migrator check still runs (Codex 225500Z).
- L-07: source order: in t029 and t032 the role guard is referenced before every
  membership row lock (FROM m01_staff ... FOR UPDATE / FOR SHARE); the static half of
  guard-before-membership (the dynamic half is BS-08 and L-06).
- SVC-01: StaffService binds the v2 admin result strictly: exactly the v1 keys plus
  "outcome" in {applied, replayed}; the public `reactivate` is sent as `activate` and
  bound back; the public AdminResult keeps the v1 four-key shape.

Every expected value is written out here, never derived from the code under test.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from typing import Any, Final

import pytest

SUPPORT: Final = "haloflow_support"
MIGRATOR: Final = "haloflow_migrator"
SHIPPED_PROFILE_EDGES: Final = (
    "haloflow_m02_append_owner",
    "haloflow_m02_lock_owner",
    "haloflow_m02_projection_owner",
    "haloflow_m02_read_owner",
    "haloflow_m05_owner",
    "haloflow_staff_owner",
    "haloflow_support",
)


# ---- SUP-01 -------------------------------------------------------------------------


def test_sup01_support_is_a_usage_only_profile_with_the_008_edge_and_not_approved() -> None:
    from haloflow.composition import APPROVED_EXECUTION_ROLES
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    manifest = load_provisioning_manifest()
    profile = manifest.execution_role_profiles[SUPPORT]
    assert (profile.login, profile.superuser, profile.createdb, profile.createrole,
            profile.replication, profile.bypassrls) == (False,) * 6
    assert tuple(profile.tenant_schema_privileges) == ("USAGE",)
    assert tuple(manifest.tenant_schema_role_privileges[SUPPORT].privileges) == ("USAGE",)
    edges = [(e.role, e.member, e.set, e.inherit, e.admin) for e in manifest.role_memberships]
    assert (SUPPORT, MIGRATOR, True, False, False) in edges
    assert sorted(e[0] for e in edges) == list(SHIPPED_PROFILE_EDGES)
    assert SUPPORT in manifest.controlled_roles
    assert SUPPORT not in APPROVED_EXECUTION_ROLES


# ---- SUP-02 -------------------------------------------------------------------------


def test_sup02_a_unit_run_as_support_is_refused_by_the_production_registry() -> None:
    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.errors import MigrationUnitRejected
    from haloflow.m01.provisioning.units import build_tenant_migration_registry
    from haloflow.m01.staff.l1w_units import L1W_STAFF_UNITS

    as_support = dataclasses.replace(
        L1W_STAFF_UNITS["t032_m01_staff_bootstrap_admin"], execution_role=SUPPORT)
    rogue = {"t033_m01_staff_rogue": as_support}
    with pytest.raises(MigrationUnitRejected) as refused:
        build_tenant_migration_registry(
            *APPROVED_TENANT_MIGRATIONS, rogue,
            approved_execution_roles=APPROVED_EXECUTION_ROLES,
            installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
        )
    assert refused.value.reason_code == "EXECUTION_ROLE_NOT_APPROVED"
    # Control: the identical registry composes once support is (hypothetically)
    # approved, so the approved-role gate is exactly what refuses it.
    build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, rogue,
        approved_execution_roles=APPROVED_EXECUTION_ROLES | {SUPPORT},
        installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
    )


# ---- SUP-03 -------------------------------------------------------------------------


def _edge(role: str, member: str, *, set_: bool = True, inherit: bool = False,
          admin: bool = False) -> Any:
    from haloflow.m01.provisioning.role_safety import MembershipEdge

    return MembershipEdge(role=role, member=member, set=set_, inherit=inherit, admin=admin)


def _shipped() -> set[Any]:
    return {_edge(role, MIGRATOR) for role in SHIPPED_PROFILE_EDGES}


@pytest.mark.parametrize(
    "case",
    ["undeclared-support-to-staff-owner", "undeclared-runtime-to-support", "missing",
     "inherit", "admin"],
)
def test_sup03_the_controlled_graph_refuses_unsafe_support_edges(case: str) -> None:
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest
    from haloflow.m01.provisioning.role_safety import assess_membership_graph

    declared = load_provisioning_manifest().role_memberships
    assert assess_membership_graph(observed=frozenset(_shipped()), declared=declared) is None
    observed = _shipped()
    if case == "undeclared-support-to-staff-owner":
        observed.add(_edge("haloflow_staff_owner", SUPPORT))
    elif case == "undeclared-runtime-to-support":
        observed.add(_edge(SUPPORT, "haloflow_runtime"))
    else:
        observed.discard(_edge(SUPPORT, MIGRATOR))
        if case == "inherit":
            observed.add(_edge(SUPPORT, MIGRATOR, inherit=True))
        elif case == "admin":
            observed.add(_edge(SUPPORT, MIGRATOR, admin=True))
    assert assess_membership_graph(observed=frozenset(observed), declared=declared) == (
        "EXECUTION_ROLE_UNAVAILABLE")


# ---- SUP-08 --------------------------------------------------------------------------


class _Cursor:
    def __init__(self, log: list[tuple[str, Any]], rows: dict[str, Any]) -> None:
        self.log, self.rows, self.last = log, rows, ("", None)

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self, statement: str, params: Any) -> None:
        kind = ("attributes" if "rolcanlogin" in statement else "set"
                if "pg_has_role" in statement else "migrator"
                if "rolcreaterole" in statement else "graph")
        self.last = (kind, params)
        self.log.append(self.last)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return [(role, MIGRATOR, True, False, False) for role in SHIPPED_PROFILE_EDGES]

    async def fetchone(self) -> Any:
        kind, params = self.last
        if kind == "attributes":
            return self.rows.get(params[0], (False,) * 6)
        if kind == "set":
            return (True,)
        return (False,)


class _Connection:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.log: list[tuple[str, Any]] = []
        self.rows = rows or {}

    def cursor(self) -> _Cursor:
        return _Cursor(self.log, self.rows)


def _unit(role: str | None) -> Any:
    return type("Unit", (), {"execution_role": role})()


def test_sup08_every_schema_privileged_profile_is_read_after_registry_roles() -> None:
    import asyncio

    from haloflow.m01.provisioning.role_safety import assert_execution_roles_safe

    conn = _Connection()
    asyncio.run(assert_execution_roles_safe(conn, [_unit("haloflow_staff_owner"), _unit(None)]))
    attribute_reads = [p[0] for k, p in conn.log if k == "attributes"]
    # The registry role first, then exactly the grant-only (USAGE-only) profiles:
    # support. Unused execution profiles (USAGE+CREATE) are not read here; their
    # pre-unit drift stays the in-SQL guards' case (Codex ruling on CI #226).
    assert attribute_reads == ["haloflow_staff_owner", SUPPORT]
    assert conn.log[0][0] == "graph" and conn.log[-1] == ("migrator", (MIGRATOR,))


@pytest.mark.parametrize("row", [(True, False, False, False, False, False),
                                 (False, False, False, False, False, True), None],
                         ids=["login", "bypassrls", "missing"])
def test_sup08_unsafe_or_missing_support_refuses_even_for_an_empty_registry(row: Any) -> None:
    import asyncio

    from haloflow.m01.errors import ExecutionRoleUnavailable
    from haloflow.m01.provisioning.role_safety import assert_execution_roles_safe

    conn = _Connection({SUPPORT: row})
    with pytest.raises(ExecutionRoleUnavailable) as refused:
        asyncio.run(assert_execution_roles_safe(conn, []))
    assert refused.value.reason_code == "EXECUTION_ROLE_UNAVAILABLE"
    assert ("migrator", (MIGRATOR,)) not in conn.log  # refused before the migrator read


def test_sup08_a_registry_role_without_a_profile_is_still_refused() -> None:
    import asyncio

    from haloflow.m01.errors import ExecutionRoleUnavailable
    from haloflow.m01.provisioning.role_safety import assert_execution_roles_safe

    conn = _Connection()
    with pytest.raises(ExecutionRoleUnavailable):
        asyncio.run(assert_execution_roles_safe(conn, [_unit("haloflow_unprofiled_owner")]))
    assert not [p for k, p in conn.log if k == "attributes"]


# ---- L-07 ---------------------------------------------------------------------------


@pytest.mark.parametrize("migration_id",
                         ["t029_m01_staff_admin_v2", "t032_m01_staff_bootstrap_admin"])
def test_l07_guard_precedes_every_membership_lock(migration_id: str) -> None:
    from haloflow.m01.staff.l1w_units import L1W_STAFF_UNITS

    body = L1W_STAFF_UNITS[migration_id].template
    guard = body.find("m01_staff_role_guard AS g WHERE g.id FOR UPDATE")
    locks = [m.start() for m in re.finditer(
        r"FROM \{schema\}\.m01_staff AS s\s+WHERE[^;]*FOR (?:UPDATE|SHARE)", body)]
    assert guard != -1, migration_id
    assert locks, migration_id
    assert all(guard < start for start in locks), migration_id


# ---- SVC-01 -------------------------------------------------------------------------

_KEY: Final = "00000000-0000-4000-8000-00000000a001"
_STAFF: Final = "00000000-0000-4000-8000-00000000a002"


def _snapshot(status: str = "active") -> dict[str, Any]:
    return {"schema_version": 1, "staff_id": _STAFF, "status": status,
            "display_name": "Dana O.", "access": [],
            "status_changed_at": "2026-11-02T22:00:00.000000Z"}


def _command(operation: str) -> Any:
    from haloflow.m01.staff.service import StaffAdminCommand

    return StaffAdminCommand(client_action_id=uuid.UUID(_KEY), operation=operation,
                             staff_id=uuid.UUID(_STAFF))


def _v2(operation: str, outcome: str = "applied", **extra: Any) -> dict[str, Any]:
    status = "active" if operation in ("activate", "reactivate") else "inactive"
    return {"schema_version": 1, "client_action_id": _KEY, "operation": operation,
            "staff": _snapshot(status), "outcome": outcome, **extra}


@pytest.mark.parametrize("outcome", ["applied", "replayed"])
def test_svc01_reactivate_is_sent_as_activate_and_bound_back(outcome: str) -> None:
    from haloflow.m01.staff.service import _bind_admin

    bound = _bind_admin([(_v2("activate", outcome),)], _command("reactivate"))
    assert bound == {"schema_version": 1, "client_action_id": _KEY,
                     "operation": "reactivate", "staff": _snapshot("active")}


def test_svc01_deactivate_binds_to_the_v1_shape() -> None:
    from haloflow.m01.staff.service import _bind_admin

    bound = _bind_admin([(_v2("deactivate"),)], _command("deactivate"))
    assert set(bound) == {"schema_version", "client_action_id", "operation", "staff"}
    assert bound["operation"] == "deactivate"


@pytest.mark.parametrize(
    ("operation", "value"),
    [
        ("reactivate", "missing-outcome"),
        ("reactivate", "bad-outcome"),
        ("reactivate", "list-outcome"),
        ("reactivate", "extra-key"),
        ("reactivate", "v1-name-returned"),
        ("deactivate", "wrong-operation"),
        ("deactivate", "v1-four-keys-only"),
    ],
)
def test_svc01_result_validation_stays_strict(operation: str, value: str) -> None:
    from haloflow.m01.staff.service import AdminFailed, _bind_admin

    sent = "activate" if operation == "reactivate" else operation
    result = _v2(sent)
    if value == "missing-outcome" or value == "v1-four-keys-only":
        del result["outcome"]
    elif value == "bad-outcome":
        result["outcome"] = "done"
    elif value == "list-outcome":
        result["outcome"] = ["applied"]
    elif value == "extra-key":
        result["note"] = "x"
    elif value == "v1-name-returned":
        result["operation"] = "reactivate"
    elif value == "wrong-operation":
        result["operation"] = "activate"
    with pytest.raises(AdminFailed) as refused:
        _bind_admin([(result,)], _command(operation))
    assert refused.value.reason_code == "ADMIN_RESULT_INVALID"


def test_svc01_the_public_operation_set_is_unchanged() -> None:
    from haloflow.m01.staff.service import OPERATIONS

    assert OPERATIONS == ("add", "rename", "deactivate", "reactivate", "grant", "revoke")
