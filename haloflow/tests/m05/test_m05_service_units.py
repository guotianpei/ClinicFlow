"""B3b service rows that need no database (package v2 + v2r1).

- SV-01  the public command types carry no trusted field.
- SV-02  every malformed public value raises CommandMalformed at construction
         (before any gateway can be reached); exact types (bool is not int, str is
         not UUID).
- ST-3   no m01.staff.* key in production composition, catalogue or manifest.
- ST-4   m01_staff_assignable has no statement.
- LS-1   no M05 body takes a non-shared lock on a directory relation; directory
         bodies other than the reader are unchanged in the red tranche.

RED TRANCHE: SV-02 fails until the implementation validates commands.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from importlib import resources
from typing import Any

import pytest

TRUSTED_FIELDS = {"permissions", "attribution", "tenant", "tenant_id", "actor", "actor_id",
                  "principal", "principal_id", "digest", "supervise", "staff_admin"}
U1, U2 = uuid.uuid4(), uuid.uuid4()


def test_sv01_public_commands_have_no_trusted_fields() -> None:
    from haloflow.m01.staff.service import StaffAdminCommand
    from haloflow.m05.service import Cursor, ListQuery, StaffAction

    for cls in (StaffAction, ListQuery, Cursor, StaffAdminCommand):
        names = {f.name for f in fields(cls)}
        assert not names & TRUSTED_FIELDS, (cls.__name__, names & TRUSTED_FIELDS)
    assert {f.name for f in fields(StaffAction)} == {
        "item_id", "client_action_id", "expected_version", "type", "assignee_id",
    }


def _action(**overrides: Any) -> Any:
    from haloflow.m05.service import StaffAction

    values: dict[str, Any] = {"item_id": U1, "client_action_id": U2, "expected_version": 1,
                              "type": "claim", "assignee_id": None}
    values.update(overrides)
    return StaffAction(**values)


ACTION_CASES = {
    "item-str": {"item_id": str(U1)},
    "key-str": {"client_action_id": str(U2)},
    "item-none": {"item_id": None},
    "version-zero": {"expected_version": 0},
    "version-negative": {"expected_version": -1},
    "version-too-large": {"expected_version": 2147483648},
    "version-bool": {"expected_version": True},
    "version-float": {"expected_version": 1.0},
    "version-str": {"expected_version": "1"},
    "type-unknown": {"type": "close"},
    "type-resolve-reserved": {"type": "resolve"},
    "type-not-str": {"type": 1},
    "claim-with-assignee": {"assignee_id": U1},
    "start-with-assignee": {"type": "start_progress", "assignee_id": U1},
    "assign-without-assignee": {"type": "assign"},
    "reassign-without-assignee": {"type": "reassign"},
    "assignee-str": {"type": "assign", "assignee_id": str(U1)},
}


@pytest.mark.parametrize("case", sorted(ACTION_CASES))
def test_sv02_staff_action_malformed(case: str) -> None:
    from haloflow.m05.service import CommandMalformed

    with pytest.raises(CommandMalformed):
        _action(**ACTION_CASES[case])


def test_sv02_staff_action_controls() -> None:
    _action()
    _action(type="start_progress")
    _action(type="assign", assignee_id=U1)
    _action(type="reassign", assignee_id=U1, expected_version=2147483647)


def _query(**overrides: Any) -> Any:
    from haloflow.m05.service import ListQuery

    values: dict[str, Any] = {"limit": 10}
    values.update(overrides)
    return ListQuery(**values)


def _cursor(**overrides: Any) -> Any:
    from haloflow.m05.service import Cursor

    values: dict[str, Any] = {"created_at": datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
                              "item_id": U1}
    values.update(overrides)
    return Cursor(**values)


QUERY_CASES = {
    "limit-zero": {"limit": 0},
    "limit-101": {"limit": 101},
    "limit-bool": {"limit": True},
    "limit-str": {"limit": "10"},
    "states-empty": {"states": ()},
    "states-list": {"states": ["open"]},
    "states-unknown": {"states": ("closed",)},
    "states-duplicate": {"states": ("open", "open")},
    "assignee-str-uuid": {"assignee": str(U1)},
    "assignee-other-word": {"assignee": "nobody"},
    "after-not-cursor": {"after": {"created_at": "x", "item_id": str(U1)}},
}


@pytest.mark.parametrize("case", sorted(QUERY_CASES))
def test_sv02_list_query_malformed(case: str) -> None:
    from haloflow.m05.service import CommandMalformed

    with pytest.raises(CommandMalformed):
        _query(**QUERY_CASES[case])


CURSOR_CASES = {
    "naive": {"created_at": datetime(2026, 11, 1, 12, 0)},
    "non-utc": {"created_at": datetime(2026, 11, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))},
    "created-str": {"created_at": "2026-11-01T12:00:00.000000Z"},
    "item-str": {"item_id": str(U1)},
}


@pytest.mark.parametrize("case", sorted(CURSOR_CASES))
def test_sv02_cursor_malformed(case: str) -> None:
    from haloflow.m05.service import CommandMalformed

    with pytest.raises(CommandMalformed):
        _cursor(**CURSOR_CASES[case])


def test_sv02_query_controls() -> None:
    _query()
    _query(limit=100, states=("open", "in_progress"), assignee="unassigned", after=_cursor())
    _query(limit=1, assignee=U1)


def _admin(**overrides: Any) -> Any:
    from haloflow.m01.staff.service import StaffAdminCommand

    values: dict[str, Any] = {"client_action_id": U1, "operation": "add", "staff_id": U2,
                              "display_name": "Dana Ortiz", "access": None}
    values.update(overrides)
    return StaffAdminCommand(**values)


ADMIN_CASES = {
    "key-str": {"client_action_id": str(U1)},
    "staff-str": {"staff_id": str(U2)},
    "operation-unknown": {"operation": "delete"},
    "add-without-name": {"display_name": None},
    "add-name-not-str": {"display_name": 5},
    "add-with-access": {"access": "read"},
    "grant-without-access": {"operation": "grant", "display_name": None},
    "grant-access-unknown": {"operation": "grant", "display_name": None, "access": "admin"},
    "grant-with-name": {"operation": "grant", "access": "read"},
    "deactivate-with-name": {"operation": "deactivate"},
}


@pytest.mark.parametrize("case", sorted(ADMIN_CASES))
def test_sv02_admin_command_malformed(case: str) -> None:
    from haloflow.m01.staff.service import CommandMalformed

    with pytest.raises(CommandMalformed):
        _admin(**ADMIN_CASES[case])


def test_sv02_admin_controls() -> None:
    _admin()
    _admin(operation="rename", display_name="Dana O.")
    _admin(operation="grant", display_name=None, access="act")
    _admin(operation="deactivate", display_name=None)


def test_st3_no_staff_statement_in_production() -> None:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS, build_production_catalog
    from haloflow.m01.provisioning.manifest import MANIFEST_PACKAGE

    for definitions in APPROVED_MODULE_STATEMENTS:
        assert not [k for k in definitions if k.startswith("m01.staff.")]
    compiled = build_production_catalog()
    assert not [s.key for s in compiled.catalog.statements() if s.key.startswith("m01.staff.")]
    text = resources.files(MANIFEST_PACKAGE).joinpath("statement_catalog.json").read_text()
    assert "m01.staff." not in json.dumps(json.loads(text))


def test_st4_staff_statements_exact_and_no_assignable_statement() -> None:
    from haloflow.m01.statements import M01_STATEMENTS, STAFF_STATEMENTS, StatementMode

    assert dict(M01_STATEMENTS) == {}
    assert {k: (m, c) for k, (m, c, _) in STAFF_STATEMENTS.items()} == {
        "m01.staff.admin": (StatementMode.WRITE, "m01.staff.admin"),
        "m01.staff.display": (StatementMode.READ, "m01.staff.display"),
        "m01.staff.followup_reader": (StatementMode.READ, "m05.followup.read"),
    }
    assert not [q for _, _, q in STAFF_STATEMENTS.values() if "m01_staff_assignable" in q]


def test_ls1_m05_bodies_take_no_exclusive_directory_lock() -> None:
    from haloflow.m05.gateways import M05_GATEWAY_UNITS

    for migration_id, unit in M05_GATEWAY_UNITS.items():
        text = unit.template
        assert not re.search(r"m01_staff\w*[^;]*FOR\s+(NO\s+KEY\s+)?UPDATE", text), migration_id
        assert "m01_staff_followup_access" not in text and " m01_staff " not in text
