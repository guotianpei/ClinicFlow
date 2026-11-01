"""UC1 part B, tranche B3a — read paths (`m05_get_item`, `m05_get_history`,
`m05_list_items`) on PostgreSQL 17.

Design delta v1 Δ5, partial ALIGN with refinements by Codex 20261101T010812Z:
exact shapes; ITEM_NOT_FOUND identical for unknown and other-tenant ids; closed
list query with integer bounds and strict cursor validation; deterministic order for
equal created_at (keyset (created_at, item_id)); no duplicate rows across pages. No
claim is made of a snapshot ACROSS separate list requests under concurrent inserts.

The list rows use their own tenant (`reads_install`) so the item set is fully
controlled. Timestamps leave M05 as UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`
(m05_support.TIMESTAMP_PATTERN), independent of the session TimeZone.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import m02_support as m02
import m05_support as m05
import psycopg
import pytest
from m05_harness import M05Install
from psycopg import sql
from psycopg.types.json import Jsonb
from test_m05_actions_postgres import _apply, _cmd, _new_item
from test_m05_create_postgres import _admin, _snapshot

pytestmark = pytest.mark.postgres


def _tenant(label: str) -> Any:
    @pytest.fixture(scope="module")
    def fixture(
        request: pytest.FixtureRequest, m02_ids: m02.Identities, m05_reset: Any
    ) -> Iterator[M05Install]:
        tenant = m02.tenant_for(f"{request.module.__name__}:m05:{label}")
        m05_reset(tenant)
        try:
            yield M05Install(m02_ids, tenant)
        finally:
            m05_reset(tenant)

    return fixture


reads_install = _tenant("reads")
foreign_install = _tenant("foreign")


def _read(install: M05Install, function: str, argument: Any, *, timezone: str = "UTC") -> Any:
    with psycopg.connect(install.ids.logins[m05.RUNTIME], autocommit=True) as conn:
        conn.execute(f"SET ROLE {m05.RUNTIME}")
        conn.execute(sql.SQL("SET TIME ZONE {}").format(sql.Literal(timezone)))
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
            row = conn.execute(
                sql.SQL("SELECT {}.{}(%s)").format(
                    sql.Identifier(install.schema), sql.Identifier(function)
                ),
                (Jsonb(argument) if function == "m05_list_items" else argument,),
            ).fetchone()
    assert row is not None
    return row[0]


def _read_refused(install: M05Install, function: str, argument: Any, code: str) -> None:
    before = _snapshot(install)
    with pytest.raises(psycopg.Error) as caught:
        _read(install, function, argument)
    assert (caught.value.sqlstate, caught.value.diag.message_primary) == ("P0001", code)
    assert _snapshot(install) == before


def _ts(value: Any) -> str:
    assert isinstance(value, str) and re.fullmatch(m05.TIMESTAMP_PATTERN, value), value
    return value


# --- get_item / get_history ------------------------------------------------------


def test_mb3_r_get_item_exact_shape(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    _apply(m05_install, _cmd(item, 1, "claim"))
    view = _read(m05_install, "m05_get_item", item)
    assert sorted(view) == sorted(m05.ITEM_VIEW_KEYS)
    row = _admin(
        m05_install,
        sql.SQL(
            "SELECT source_event_id::text, item_kind, patient_id::text FROM {}.m05_followup_items"
            " WHERE item_id = %s"
        ).format(sql.Identifier(m05_install.schema)),
        (item,),
    )[0]
    assert {k: view[k] for k in view if k != "created_at"} == {
        "schema_version": 1,
        "item_id": item,
        "source_event_id": row[0],
        "item_kind": row[1],
        "patient_id": row[2],
        "state": "open",
        "assignee_id": m05.S1,
        "outcome_code": None,
        "version": 2,
    }
    _ts(view["created_at"])
    # The same instant whatever the session time zone.
    assert _read(m05_install, "m05_get_item", item, timezone="America/New_York") == view


def test_mb3_r_get_history_exact_shape(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    _apply(m05_install, _cmd(item, 1, "claim"))
    history = _read(m05_install, "m05_get_history", item)
    assert sorted(history) == ["entries", "item_id", "schema_version"]
    assert (history["schema_version"], history["item_id"]) == (1, item)
    entries = history["entries"]
    assert all(sorted(e) == sorted(m05.HISTORY_ENTRY_KEYS) for e in entries)
    assert [
        (e["seq"], e["action"], e["actor_id"], e["before"], e["after"], e["assertion_kind"])
        for e in entries
    ] == [
        (1, "created", m05.W1, None,
         {"state": "open", "assignee_id": None, "outcome_code": None}, None),
        (2, "claim", m05.S1, {"state": "open", "assignee_id": None, "outcome_code": None},
         {"state": "open", "assignee_id": m05.S1, "outcome_code": None}, None),
    ]
    for entry in entries:
        _ts(entry["created_at"])
    assert "resolution_event_id" not in str(history)


def test_mb3_r_unknown_and_foreign_ids_share_item_not_found(
    m05_install: M05Install, foreign_install: M05Install
) -> None:
    _new_item(m05_install)
    foreign = _new_item(foreign_install)
    for item in (str(uuid.uuid4()), foreign):
        for function in ("m05_get_item", "m05_get_history"):
            _read_refused(m05_install, function, item, "ITEM_NOT_FOUND")


# --- list_items ------------------------------------------------------------------


def _list(install: M05Install, **query: Any) -> dict[str, Any]:
    result: dict[str, Any] = _read(install, "m05_list_items", {"schema_version": 1, **query})
    assert sorted(result) == sorted(m05.LIST_RESULT_KEYS), result
    assert result["schema_version"] == 1
    for view in result["items"]:
        assert sorted(view) == sorted(m05.ITEM_VIEW_KEYS)
    return result


def _ids(result: dict[str, Any]) -> list[str]:
    return [view["item_id"] for view in result["items"]]


def test_mb3_r_list_filters_order_and_paging(reads_install: M05Install) -> None:
    items = [_new_item(reads_install) for _ in range(5)]
    # Ties: give three items the SAME created_at; order must fall back to item_id.
    _admin(
        reads_install,
        sql.SQL(
            "UPDATE {}.m05_followup_items SET created_at = '2026-11-01 00:00:00.000001+00'"
            " WHERE item_id = ANY(%s::uuid[])"
        ).format(sql.Identifier(reads_install.schema)),
        (items[:3],),
    )
    _apply(reads_install, _cmd(items[3], 1, "claim"))
    _apply(reads_install, _cmd(items[4], 1, "claim", actor=m05.S2))
    _apply(reads_install, _cmd(items[4], 2, "start_progress", actor=m05.S2))
    rows = _admin(
        reads_install,
        sql.SQL(
            "SELECT item_id::text FROM {}.m05_followup_items ORDER BY created_at, item_id"
        ).format(sql.Identifier(reads_install.schema)),
    )
    expected = [row[0] for row in rows]
    assert _ids(_list(reads_install, limit=100)) == expected
    assert _list(reads_install, limit=100)["next"] is None
    # Keyset paging with limit 2: every item exactly once, in order.
    seen: list[str] = []
    after: Any = None
    for _ in range(10):
        page = _list(reads_install, limit=2, **({"after": after} if after else {}))
        seen.extend(_ids(page))
        after = page["next"]
        if after is None:
            break
        assert sorted(after) == ["created_at", "item_id"]
        _ts(after["created_at"])
    assert seen == expected and len(set(seen)) == len(seen)
    # Filters.
    assert _ids(_list(reads_install, limit=100, states=["in_progress"])) == [items[4]]
    assert _ids(_list(reads_install, limit=100, assignee=m05.S1)) == [items[3]]
    assert set(_ids(_list(reads_install, limit=100, assignee="unassigned"))) == set(items[:3])
    assert set(_ids(_list(reads_install, limit=100, states=["open", "in_progress"]))) == set(items)
    assert _ids(_list(reads_install, limit=100, states=["resolved"])) == []


def test_mb3_r_list_query_is_closed(reads_install: M05Install) -> None:
    _new_item(reads_install)
    ok_after = {"created_at": "2026-11-01T00:00:00.000001Z", "item_id": str(uuid.uuid4())}
    for name, query in {
        "not-object": [1],
        "missing-schema-version": {"limit": 10},
        "schema-version-2": {"schema_version": 2, "limit": 10},
        "missing-limit": {"schema_version": 1},
        "limit-zero": {"schema_version": 1, "limit": 0},
        "limit-over": {"schema_version": 1, "limit": m05.LIST_LIMIT_MAX + 1},
        "limit-float": {"schema_version": 1, "limit": 10.0},
        "limit-string": {"schema_version": 1, "limit": "10"},
        "limit-bool": {"schema_version": 1, "limit": True},
        "extra-key": {"schema_version": 1, "limit": 10, "patient_id": str(uuid.uuid4())},
        "states-empty": {"schema_version": 1, "limit": 10, "states": []},
        "states-duplicate": {"schema_version": 1, "limit": 10, "states": ["open", "open"]},
        "states-unknown": {"schema_version": 1, "limit": 10, "states": ["closed"]},
        "states-not-array": {"schema_version": 1, "limit": 10, "states": "open"},
        "assignee-not-uuid": {"schema_version": 1, "limit": 10, "assignee": "s1"},
        "assignee-null": {"schema_version": 1, "limit": 10, "assignee": None},
        "after-not-object": {"schema_version": 1, "limit": 10, "after": "x"},
        "after-missing-id": {"schema_version": 1, "limit": 10,
                             "after": {"created_at": ok_after["created_at"]}},
        "after-extra": {"schema_version": 1, "limit": 10, "after": {**ok_after, "x": 1}},
        "after-bad-timestamp": {"schema_version": 1, "limit": 10,
                                "after": {**ok_after, "created_at": "2026-11-01"}},
        "after-offset-timestamp": {"schema_version": 1, "limit": 10,
                                   "after": {**ok_after,
                                             "created_at": "2026-11-01T00:00:00.000001+00:00"}},
        "after-impossible-date": {"schema_version": 1, "limit": 10,
                                  "after": {**ok_after,
                                            "created_at": "2026-02-30T00:00:00.000001Z"}},
        "after-id-uppercase": {"schema_version": 1, "limit": 10,
                               "after": {**ok_after, "item_id": ok_after["item_id"].upper()}},
    }.items():
        try:
            _read_refused(reads_install, "m05_list_items", query, "COMMAND_MALFORMED")
        except AssertionError as error:
            raise AssertionError(f"case {name}: {error}") from error
    # Controls: the well-formed bounds and cursor are accepted.
    _list(reads_install, limit=1)
    _list(reads_install, limit=m05.LIST_LIMIT_MAX, after=ok_after)
