"""L-1W W4b: staff admin role and setting operations (unit tier, no database).

Plan v1 addendum 1 Q-3 and interface pins v1 1.2-1.3. Rows: AO-01 (emitted keys equal
t029's ``v_expected``, parsed from the unit body), AO-02 (malformed payloads refused
before I/O), AO-05 (service-level staff_id mismatch for ``set_auth_setting``), AO-07
(command-level value bounds), plus the self-standing result binding (1.2) and the
admin result's ``outcome`` (1.3).

PINNED INTERFACE (interface pins v1 1.2-1.3):
- ``haloflow.m01.staff.service.OPERATIONS`` == ("add", "rename", "deactivate",
  "reactivate", "grant", "revoke", "grant_role", "revoke_role", "set_auth_setting").
- ``StaffAdminCommand(client_action_id, operation, staff_id, display_name=None,
  access=None, role=None, setting=None, value=None)``; every field not used by the
  operation must be None; ``CommandMalformed(<field>)`` on the first bad field.
- ``AdminResult(result, outcome)``: ``result`` unchanged, ``outcome`` in
  {"applied", "replayed"}.
- ``Standing(status, access, roles)`` (frozen; closed vocabularies, else
  ``ReadFailed("READ_RESULT_INVALID")``) and ``StaffService.self_standing(context)
  -> Standing``: requires exactly the ``m01.staff.self_standing`` capability (else
  ``RouteDenied`` with no gateway call), one gateway transaction with statement key
  ``m01.staff.self_standing`` and params ``{"attribution": Jsonb(<attribution>)}``.

The fake gateway records every statement; nothing here touches a database.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pytest

ACTOR = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
KEY = uuid.UUID("33333333-3333-4333-8333-333333333333")
SELF_STANDING = "m01.staff.self_standing"
ADMIN = "m01.staff.admin"
TS = "2026-11-04T21:00:00.000000Z"


class _Kind(Enum):
    ACTOR = "actor"


@dataclass(frozen=True)
class _Principal:
    id: str = ACTOR
    auth_method: str = "bearer"
    kind: _Kind = _Kind.ACTOR


@dataclass(frozen=True)
class _Context:
    capabilities: frozenset[str]
    principal: _Principal = field(default_factory=_Principal)
    execution_id: uuid.UUID = uuid.UUID("44444444-4444-4444-8444-444444444444")
    request_id: str | None = None
    purpose: str = "operations"
    tenant_id: str = "clinic-unit"


class _Handle:
    def __init__(self, gateway: _Gateway) -> None:
        self.gateway = gateway

    async def fetch_all(self, key: str, params: dict[str, Any]) -> list[Any]:
        self.gateway.calls.append((key, params))
        return self.gateway.rows


class _Gateway:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows: list[Any] = rows if rows is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.transactions = 0

    async def with_tenant_transaction(self, context: Any, callback: Any) -> Any:
        self.transactions += 1
        return await callback(_Handle(self))


def _command(**kw: Any) -> Any:
    from haloflow.m01.staff.service import StaffAdminCommand

    return StaffAdminCommand(**kw)


def _snapshot(staff_id: str = ACTOR) -> dict[str, Any]:
    return {"schema_version": 1, "staff_id": staff_id, "display_name": "Synthetic Admin",
            "status": "active", "status_changed_at": TS, "access": []}


def _v2_rows(operation: str, staff_id: str, outcome: str = "applied") -> list[Any]:
    return [({"schema_version": 1, "client_action_id": str(KEY), "operation": operation,
              "staff": _snapshot(staff_id), "outcome": outcome},)]


# ---------------------------------------------------------------------------
# AO-01: emitted keys equal t029's v_expected (parsed from the unit body)
# ---------------------------------------------------------------------------


def _t029_expected() -> dict[str, list[str]]:
    from haloflow.m01.staff import l1w_units

    body = l1w_units._ADMIN_V2_BODY
    start = body.index("v_expected := CASE")
    block = body[start:body.index("END;", start)]
    expected: dict[str, list[str]] = {}
    pattern = r"WHEN v_op (?:IN \(([^)]*)\)|= ('[a-z_]+')) THEN ARRAY\[([^\]]*)\]"
    for in_list, single, keys in re.findall(pattern, " ".join(block.split())):
        ops = in_list or single
        names = re.findall(r"'([a-z_]+)'", keys)
        for op in re.findall(r"'([a-z_]+)'", ops):
            expected[op] = names
    return expected


def test_ao01_parser_reads_every_t029_operation() -> None:
    assert set(_t029_expected()) == {
        "add", "rename", "grant", "revoke", "grant_role", "revoke_role", "bind_identity",
        "set_auth_setting", "deactivate", "activate", "unbind_identity",
    }


AO_COMMANDS: dict[str, dict[str, Any]] = {
    "grant_role": {"operation": "grant_role", "staff_id": uuid.UUID(OTHER), "role": "supervisor"},
    "revoke_role": {"operation": "revoke_role", "staff_id": uuid.UUID(OTHER),
                    "role": "clinic_admin"},
    "set_auth_setting": {"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                         "setting": "idle_timeout_minutes", "value": "30"},
    "add": {"operation": "add", "staff_id": uuid.UUID(OTHER), "display_name": "Synthetic B"},
    "rename": {"operation": "rename", "staff_id": uuid.UUID(OTHER), "display_name": "Synth C"},
    "grant": {"operation": "grant", "staff_id": uuid.UUID(OTHER), "access": "read"},
    "revoke": {"operation": "revoke", "staff_id": uuid.UUID(OTHER), "access": "act"},
    "deactivate": {"operation": "deactivate", "staff_id": uuid.UUID(OTHER)},
    "reactivate": {"operation": "reactivate", "staff_id": uuid.UUID(OTHER)},
}
SQL_OPERATION = {"reactivate": "activate"}


@pytest.mark.parametrize("name", sorted(AO_COMMANDS))
async def test_ao01_emitted_keys_equal_t029_v_expected(name: str) -> None:
    from haloflow.m01.staff.service import StaffService

    kw = AO_COMMANDS[name]
    sql_op = SQL_OPERATION.get(kw["operation"], kw["operation"])
    gateway = _Gateway(_v2_rows(sql_op, str(kw["staff_id"])))
    if name in ("add",):
        gateway.rows[0][0]["staff"]["display_name"] = kw["display_name"]
    if name == "rename":
        gateway.rows[0][0]["staff"]["display_name"] = kw["display_name"]
    if name == "deactivate":
        gateway.rows[0][0]["staff"]["status"] = "inactive"
    if name == "grant":
        gateway.rows[0][0]["staff"]["access"] = ["read"]
    await StaffService(gateway).admin(_Context(frozenset({ADMIN})),
                                      _command(client_action_id=KEY, **kw))
    [(key, params)] = gateway.calls
    assert key == "m01.staff.admin"
    sent = params["command"].obj
    assert sorted(sent) == _t029_expected()[sql_op]
    assert sent["schema_version"] == 1 and sent["operation"] == sql_op
    assert sent["permissions"] == {"staff_admin": True}


def test_ao01_set_auth_setting_values_are_strings() -> None:
    cmd = _command(client_action_id=KEY, operation="set_auth_setting",
                   staff_id=uuid.UUID(ACTOR), setting="mfa_required", value="true")
    assert type(cmd.value) is str


# ---------------------------------------------------------------------------
# AO-02 / AO-07: malformed payloads are refused at construction (before any I/O)
# ---------------------------------------------------------------------------

AO02_CASES: dict[str, tuple[dict[str, Any], str]] = {
    "grant_role-no-role": ({"operation": "grant_role", "staff_id": uuid.UUID(OTHER)}, "role"),
    "grant_role-bad-role": ({"operation": "grant_role", "staff_id": uuid.UUID(OTHER),
                             "role": "owner"}, "role"),
    "grant_role-role-not-str": ({"operation": "grant_role", "staff_id": uuid.UUID(OTHER),
                                 "role": ["supervisor"]}, "role"),
    "grant_role-with-access": ({"operation": "grant_role", "staff_id": uuid.UUID(OTHER),
                                "role": "supervisor", "access": "read"}, "access"),
    "grant_role-with-name": ({"operation": "grant_role", "staff_id": uuid.UUID(OTHER),
                              "role": "supervisor", "display_name": "X"}, "display_name"),
    "grant_role-with-setting": ({"operation": "grant_role", "staff_id": uuid.UUID(OTHER),
                                 "role": "supervisor", "setting": "mfa_required"}, "setting"),
    "revoke_role-with-value": ({"operation": "revoke_role", "staff_id": uuid.UUID(OTHER),
                                "role": "supervisor", "value": "true"}, "value"),
    "grant-with-role": ({"operation": "grant", "staff_id": uuid.UUID(OTHER), "access": "read",
                         "role": "supervisor"}, "role"),
    "add-with-setting": ({"operation": "add", "staff_id": uuid.UUID(OTHER),
                          "display_name": "X", "setting": "mfa_required"}, "setting"),
    "deactivate-with-value": ({"operation": "deactivate", "staff_id": uuid.UUID(OTHER),
                               "value": "x"}, "value"),
    "set-no-setting": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                        "value": "30"}, "setting"),
    "set-bad-setting": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                         "setting": "max_session_hours", "value": "12"}, "setting"),
    "set-no-value": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                      "setting": "mfa_required"}, "value"),
    "set-value-not-str": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                           "setting": "idle_timeout_minutes", "value": 30}, "value"),
    "set-with-role": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                       "setting": "mfa_required", "value": "true", "role": "supervisor"}, "role"),
    "set-with-access": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                         "setting": "mfa_required", "value": "true", "access": "read"}, "access"),
    # AO-07 at the command level
    "idle-4": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                "setting": "idle_timeout_minutes", "value": "4"}, "value"),
    "idle-61": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                 "setting": "idle_timeout_minutes", "value": "61"}, "value"),
    "idle-three-digits": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                           "setting": "idle_timeout_minutes", "value": "030"}, "value"),
    "idle-sign": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                   "setting": "idle_timeout_minutes", "value": "+30"}, "value"),
    "idle-space": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                    "setting": "idle_timeout_minutes", "value": " 30"}, "value"),
    "idle-arabic-indic": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                           "setting": "idle_timeout_minutes", "value": "٣٠"}, "value"),
    "idle-empty": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                    "setting": "idle_timeout_minutes", "value": ""}, "value"),
    "mfa-yes": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                 "setting": "mfa_required", "value": "yes"}, "value"),
    "mfa-True": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
                  "setting": "mfa_required", "value": "True"}, "value"),
    "mfa-1": ({"operation": "set_auth_setting", "staff_id": uuid.UUID(ACTOR),
               "setting": "mfa_required", "value": "1"}, "value"),
    "bind_identity-not-offered": ({"operation": "bind_identity", "staff_id": uuid.UUID(OTHER)},
                                  "operation"),
    "unbind_identity-not-offered": ({"operation": "unbind_identity",
                                     "staff_id": uuid.UUID(OTHER)}, "operation"),
    "activate-sql-name-not-offered": ({"operation": "activate", "staff_id": uuid.UUID(OTHER)},
                                      "operation"),
}


@pytest.mark.parametrize("case", sorted(AO02_CASES))
def test_ao02_ao07_malformed_command_refused_at_construction(case: str) -> None:
    from haloflow.m01.staff.service import CommandMalformed

    kw, fld = AO02_CASES[case]
    with pytest.raises(CommandMalformed) as caught:
        _command(client_action_id=KEY, **kw)
    assert caught.value.field == fld


@pytest.mark.parametrize("value", ["5", "05", "15", "60", "59"])
def test_ao07_idle_values_inside_the_sql_rule_are_accepted(value: str) -> None:
    cmd = _command(client_action_id=KEY, operation="set_auth_setting", staff_id=uuid.UUID(ACTOR),
                   setting="idle_timeout_minutes", value=value)
    assert cmd.value == value


@pytest.mark.parametrize("value", ["true", "false"])
def test_ao07_mfa_values_accepted(value: str) -> None:
    cmd = _command(client_action_id=KEY, operation="set_auth_setting", staff_id=uuid.UUID(ACTOR),
                   setting="mfa_required", value=value)
    assert cmd.value == value


def test_ao02_operations_literal() -> None:
    from haloflow.m01.staff.service import OPERATIONS

    assert OPERATIONS == ("add", "rename", "deactivate", "reactivate", "grant", "revoke",
                          "grant_role", "revoke_role", "set_auth_setting")


# ---------------------------------------------------------------------------
# AO-05: set_auth_setting names the acting admin (service level)
# ---------------------------------------------------------------------------


async def test_ao05_set_auth_setting_for_another_staff_id_refused_before_io() -> None:
    from haloflow.m01.staff.service import AdminRefused, CommandMalformed, StaffService

    gateway = _Gateway()
    cmd = _command(client_action_id=KEY, operation="set_auth_setting",
                   staff_id=uuid.UUID(OTHER), setting="mfa_required", value="true")
    with pytest.raises((CommandMalformed, AdminRefused)):
        await StaffService(gateway).admin(_Context(frozenset({ADMIN})), cmd)
    assert gateway.calls == [] and gateway.transactions == 0


# ---------------------------------------------------------------------------
# Result binding: outcome carried; role/setting snapshots bound on shape and identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["applied", "replayed"])
async def test_admin_result_carries_the_outcome(outcome: str) -> None:
    from haloflow.m01.staff.service import StaffService

    gateway = _Gateway(_v2_rows("grant_role", OTHER, outcome))
    result = await StaffService(gateway).admin(
        _Context(frozenset({ADMIN})),
        _command(client_action_id=KEY, **AO_COMMANDS["grant_role"]))
    assert result.outcome == outcome
    assert dict(result.result) == {"schema_version": 1, "client_action_id": str(KEY),
                                   "operation": "grant_role", "staff": _snapshot(OTHER)}


@pytest.mark.parametrize("mutate", ["other-staff", "other-operation", "bad-outcome",
                                    "extra-key"])
async def test_admin_result_for_role_operation_is_bound(mutate: str) -> None:
    from haloflow.m01.staff.service import AdminFailed, StaffService

    rows = _v2_rows("grant_role", OTHER)
    value = rows[0][0]
    if mutate == "other-staff":
        value["staff"] = _snapshot(ACTOR)
    elif mutate == "other-operation":
        value["operation"] = "revoke_role"
    elif mutate == "bad-outcome":
        value["outcome"] = "noop"
    else:
        value["role"] = "supervisor"
    with pytest.raises(AdminFailed) as caught:
        await StaffService(_Gateway(rows)).admin(
            _Context(frozenset({ADMIN})),
            _command(client_action_id=KEY, **AO_COMMANDS["grant_role"]))
    assert caught.value.reason_code == "ADMIN_RESULT_INVALID"


# ---------------------------------------------------------------------------
# Self-standing (interface pins 1.2)
# ---------------------------------------------------------------------------


def _standing_row(**over: Any) -> list[Any]:
    value: dict[str, Any] = {"schema_version": 1, "staff_id": ACTOR, "status": "active",
                             "access": ["act", "read"], "roles": ["supervisor"]}
    value.update(over)
    return [(value,)]


async def test_self_standing_binds_the_caller_standing() -> None:
    from haloflow.m01.staff.service import StaffService, Standing

    gateway = _Gateway(_standing_row())
    standing = await StaffService(gateway).self_standing(_Context(frozenset({SELF_STANDING})))
    assert standing == Standing(status="active", access=("act", "read"), roles=("supervisor",))
    [(key, params)] = gateway.calls
    assert key == "m01.staff.self_standing" and set(params) == {"attribution"}
    assert params["attribution"].obj == {
        "principal_kind": "actor", "principal_id": ACTOR, "auth_method": "bearer",
        "execution_id": "44444444-4444-4444-8444-444444444444", "request_id": None,
        "purpose_code": "operations"}
    assert gateway.transactions == 1


async def test_self_standing_unknown_is_a_standing_not_an_error() -> None:
    from haloflow.m01.staff.service import StaffService, Standing

    gateway = _Gateway(_standing_row(status="unknown", access=[], roles=[]))
    standing = await StaffService(gateway).self_standing(_Context(frozenset({SELF_STANDING})))
    assert standing == Standing(status="unknown", access=(), roles=())


@pytest.mark.parametrize("caps", [frozenset(), frozenset({"m05.followup.read"}),
                                  frozenset({ADMIN})])
async def test_self_standing_requires_its_capability(caps: frozenset[str]) -> None:
    from haloflow.m01.staff.service import RouteDenied, StaffService

    gateway = _Gateway(_standing_row())
    with pytest.raises(RouteDenied):
        await StaffService(gateway).self_standing(_Context(caps))
    assert gateway.transactions == 0


SELF_STANDING_INVALID: dict[str, list[Any]] = {
    "other-staff": _standing_row(staff_id=OTHER),
    "schema-2": _standing_row(schema_version=2),
    "status-bad": _standing_row(status="enabled"),
    "access-unsorted": _standing_row(access=["read", "act"]),
    "access-dup": _standing_row(access=["read", "read"]),
    "access-bad": _standing_row(access=["write"]),
    "roles-bad": _standing_row(roles=["owner"]),
    "roles-unsorted": _standing_row(roles=["supervisor", "clinic_admin"]),
    "extra-key": _standing_row(tenant_id="clinic-x"),
    "missing-key": [({"schema_version": 1, "staff_id": ACTOR, "status": "active",
                      "access": []},)],
    "two-rows": _standing_row() + _standing_row(),
    "no-rows": [],
    "not-object": [(["active"],)],
    "access-not-list": _standing_row(access="read"),
    "bool-schema": _standing_row(schema_version=True),
}


@pytest.mark.parametrize("case", sorted(SELF_STANDING_INVALID))
async def test_self_standing_invalid_result_fails_closed(case: str) -> None:
    from haloflow.m01.staff.service import ReadFailed, StaffService

    gateway = _Gateway(SELF_STANDING_INVALID[case])
    with pytest.raises(ReadFailed) as caught:
        await StaffService(gateway).self_standing(_Context(frozenset({SELF_STANDING})))
    assert caught.value.reason_code == "READ_RESULT_INVALID"
