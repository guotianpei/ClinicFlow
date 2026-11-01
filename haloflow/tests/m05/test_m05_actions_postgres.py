"""UC1 part B, tranche B3a — staff actions in SQL (`m05_prepare_action`,
`m05_apply_action`) on PostgreSQL 17.

Design delta v1 (Δ2–Δ4, Δ6, Δ8), partial ALIGN with refinements by Codex
20261101T010812Z (codex_b3-design-delta-v1-disposition). B3a is not authentication
acceptance: the permission booleans arrive in the SQL command from the service,
which B3b must prove derives them from the trusted M01 context only.

MA-10 (an inactive or other-tenant assignee is refused) is an OPEN owner decision
(Codex asked Rachel; no staff directory exists). It is not tested, not replaced and
not claimed here; only the UUID shape of `assignee_id` is a closed-schema check.

Every call uses the intended path (runtime login shim, SET ROLE runtime,
`app.tenant_id` set transaction-locally). Apply raises P0001 with MESSAGE = code;
prepare RETURNS {decision: NEW | REPLAY | REFUSED}. Every refusal asserts zero writes
by full M05 table contents plus M02 counts (the B2 `_snapshot`).

Rows:
- MA-02a claim then start_progress: two history rows, versions 2 and 3, exact
  before/after, no M02 event, outcome unchanged.
- MA-04  start_progress on another's item: staff NOT_AUTHORIZED; supervisor allowed
  (history actor = supervisor).
- MA-06  assign / reassign need supervise; supervisor take-over by reassign-to-self.
- MB3-PC preconditions -> PRECONDITION_FAILED; assign-to-self -> COMMAND_MALFORMED.
- MA-07  every action on a resolved item -> ITEM_RESOLVED (also with a stale version).
- MA-08  stale expected_version -> STALE_VERSION.
- MA-09  concurrent claim: the second apply observed waiting on the item lock, then
         STALE_VERSION; one assignee, one history row.
- MA-11  unknown and other-tenant item -> identical ITEM_NOT_FOUND (apply, prepare).
- MA-13' read/act missing, supervise without act, workload principal -> NOT_AUTHORIZED.
- MR-01  exact retry after intervening actions and a reassignment -> replayed original
         snapshot, zero writes; also after the item is resolved, while read+act hold.
- MR-01r replay with act or read revoked -> NOT_AUTHORIZED, zero writes.
- MR-02  same key, different payload -> ACTION_KEY_CONFLICT.
- MR-03  same key, different actor -> ACTION_KEY_ACTOR_MISMATCH.
- MR-09  direct apply without prepare == prepare then apply.
- MR-11  closed schema (incl. integer bounds, digest/extra keys) -> COMMAND_MALFORMED.
- MB3-P  prepare classifies NEW / REPLAY / REFUSED{code}, writes nothing, takes the
         item lock, and does not envelope the 22023 tenant-context failure.
- MB3-A  a fault at the history or the action-record insert rolls back the item.
- MB3-T  canonical reserved resolve shapes -> ACTION_NOT_IMPLEMENTED; malformed
         reserved shapes -> COMMAND_MALFORMED first.
- MB3-V  action digest: literal vectors, and the stored digest equals the oracle.
"""

from __future__ import annotations

import threading
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
from test_m05_create_postgres import _admin, _command, _create, _ready, _snapshot, _source

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def foreign_install(
    request: pytest.FixtureRequest, m02_ids: m02.Identities, m05_reset: Any
) -> Iterator[M05Install]:
    tenant = m02.tenant_for(f"{request.module.__name__}:m05:foreign")
    m05_reset(tenant)
    try:
        yield M05Install(m02_ids, tenant)
    finally:
        m05_reset(tenant)


def _uuid() -> str:
    return str(uuid.uuid4())


def _new_item(install: M05Install) -> str:
    _ready(install)
    result = _create(install, _command(_source(install)))
    return str(result["item_id"])


def _cmd(
    item_id: str,
    version: int,
    action_type: str,
    *,
    key: str | None = None,
    actor: str = m05.S1,
    assignee: str | None = None,
    permissions: dict[str, bool] | None = None,
    kind: str = "actor",
    action_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from test_m05_create_postgres import _attribution

    action: dict[str, Any] = {"type": action_type}
    if assignee is not None:
        action["assignee_id"] = assignee
    action.update(action_extra or {})
    return {
        "schema_version": 1,
        "item_id": item_id,
        "client_action_id": key or _uuid(),
        "expected_version": version,
        "action": action,
        "permissions": dict(permissions or m05.STAFF_PERMISSIONS),
        "attribution": _attribution(actor, kind=kind),
    }


def _call(install: M05Install, function: str, command: Any) -> dict[str, Any]:
    with psycopg.connect(install.ids.logins[m05.RUNTIME], autocommit=True) as conn:
        conn.execute(f"SET ROLE {m05.RUNTIME}")
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
            row = conn.execute(
                sql.SQL("SELECT {}.{}(%s)").format(
                    sql.Identifier(install.schema), sql.Identifier(function)
                ),
                (Jsonb(command),),
            ).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def _apply(install: M05Install, command: Any) -> dict[str, Any]:
    result = _call(install, "m05_apply_action", command)
    assert sorted(result) == sorted(m05.APPLY_KEYS), result
    assert result["schema_version"] == 1
    assert sorted(result["snapshot"]) == sorted(m05.SNAPSHOT_KEYS), result
    return result


def _prepare(install: M05Install, command: Any) -> dict[str, Any]:
    before = _snapshot(install)
    result = _call(install, "m05_prepare_action", command)
    assert _snapshot(install) == before, "prepare wrote something"
    assert sorted(result) == sorted(m05.PREPARE_KEYS[result["decision"]]), result
    assert result["schema_version"] == 1
    return result


def _refused(install: M05Install, command: Any, code: str) -> None:
    """Apply refuses with `code` and writes nothing; prepare classifies the same."""

    assert _prepare(install, command) == {
        "schema_version": 1,
        "decision": "REFUSED",
        "code": code,
    }
    before = _snapshot(install)
    with pytest.raises(psycopg.Error) as caught:
        _call(install, "m05_apply_action", command)
    assert (caught.value.sqlstate, caught.value.diag.message_primary) == ("P0001", code)
    assert _snapshot(install) == before, "a refused apply wrote something"


def _row(install: M05Install, item_id: str) -> dict[str, Any]:
    rows = _admin(
        install,
        sql.SQL("SELECT to_jsonb(t) FROM {}.m05_followup_items t WHERE item_id = %s").format(
            sql.Identifier(install.schema)
        ),
        (item_id,),
    )
    item: dict[str, Any] = rows[0][0]
    return item


def _history(install: M05Install, item_id: str) -> list[dict[str, Any]]:
    rows = _admin(
        install,
        sql.SQL(
            "SELECT to_jsonb(t) FROM {}.m05_followup_history t WHERE item_id = %s ORDER BY seq"
        ).format(sql.Identifier(install.schema)),
        (item_id,),
    )
    return [row[0] for row in rows]


def _resolve_by_admin(install: M05Install, item_id: str) -> None:
    """Resolution itself is B4; tests reach `resolved` by a direct admin fixture."""

    _admin(
        install,
        sql.SQL(
            "UPDATE {}.m05_followup_items SET state = 'resolved',"
            " outcome_code = 'reached_no_change', version = version + 1 WHERE item_id = %s"
        ).format(sql.Identifier(install.schema)),
        (item_id,),
    )


def _state(state: str, assignee: str | None) -> dict[str, Any]:
    return {"state": state, "assignee_id": assignee, "outcome_code": None}


def _expected_snapshot(
    item_id: str, key: str, action: str, version: int, state: str, assignee: str | None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "item_id": item_id,
        "client_action_id": key,
        "action": action,
        "version": version,
        "state": state,
        "assignee_id": assignee,
        "outcome_code": None,
    }


# --- MB3-V ---------------------------------------------------------------------


def test_mb3_v_action_digest_vectors() -> None:
    for parts, preimage, digest in m05.ACTION_DIGEST_VECTORS:
        assert m05.action_digest_preimage(*parts) == preimage
        assert m05.action_digest(*parts).hex() == digest


def test_mb3_v_stored_digest_equals_the_oracle(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    key = _uuid()
    _apply(m05_install, _cmd(item, 1, "claim", key=key))
    rows = _admin(
        m05_install,
        sql.SQL(
            "SELECT encode(digest, 'hex'), hmac_key_version, event_id, actor_id::text"
            " FROM {}.m05_followup_actions WHERE item_id = %s AND client_action_id = %s"
        ).format(sql.Identifier(m05_install.schema)),
        (item, key),
    )
    expected = m05.action_digest(m05_install.tenant[0], item, key, m05.S1, "claim", None, 1)
    assert rows == [(expected.hex(), None, None, m05.S1)]


# --- MA-02a / MA-04 / MA-06 / preconditions -------------------------------------


def test_ma02a_claim_then_start_progress(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    m02_before = {k: v for k, v in _snapshot(m05_install).items() if not k.startswith("m05_")}
    k1, k2 = _uuid(), _uuid()
    first = _apply(m05_install, _cmd(item, 1, "claim", key=k1))
    assert first == {
        "schema_version": 1,
        "outcome": "applied",
        "snapshot": _expected_snapshot(item, k1, "claim", 2, "open", m05.S1),
    }
    second = _apply(m05_install, _cmd(item, 2, "start_progress", key=k2))
    assert second["snapshot"] == _expected_snapshot(
        item, k2, "start_progress", 3, "in_progress", m05.S1
    )
    history = _history(m05_install, item)
    assert [(h["seq"], h["action"], h["actor_id"]) for h in history] == [
        (1, "created", m05.W1),
        (2, "claim", m05.S1),
        (3, "start_progress", m05.S1),
    ]
    assert (history[1]["before"], history[1]["after"]) == (
        _state("open", None),
        _state("open", m05.S1),
    )
    assert (history[2]["before"], history[2]["after"]) == (
        _state("open", m05.S1),
        _state("in_progress", m05.S1),
    )
    assert all(h["assertion_kind"] is None and h["resolution_event_id"] is None for h in history)
    row = _row(m05_install, item)
    assert (row["version"], row["state"], row["assignee_id"], row["outcome_code"]) == (
        3, "in_progress", m05.S1, None,
    )
    m02_after = {k: v for k, v in _snapshot(m05_install).items() if not k.startswith("m05_")}
    assert m02_after == m02_before  # non-resolve actions never call M02


def test_ma04_start_progress_on_anothers_item(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    _apply(m05_install, _cmd(item, 1, "claim"))
    _refused(m05_install, _cmd(item, 2, "start_progress", actor=m05.S2), "NOT_AUTHORIZED")
    result = _apply(
        m05_install,
        _cmd(item, 2, "start_progress", actor=m05.SUP, permissions=m05.SUPERVISOR_PERMISSIONS),
    )
    assert (result["snapshot"]["state"], result["snapshot"]["assignee_id"]) == (
        "in_progress", m05.S1,
    )
    assert _history(m05_install, item)[-1]["actor_id"] == m05.SUP


def test_ma06_assign_and_reassign_require_supervise(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    _refused(m05_install, _cmd(item, 1, "assign", assignee=m05.S2), "NOT_AUTHORIZED")
    sup = m05.SUPERVISOR_PERMISSIONS
    assigned = _apply(
        m05_install, _cmd(item, 1, "assign", assignee=m05.S2, actor=m05.SUP, permissions=sup)
    )
    assert assigned["snapshot"]["assignee_id"] == m05.S2
    _refused(m05_install, _cmd(item, 2, "reassign", assignee=m05.S1), "NOT_AUTHORIZED")
    _refused(
        m05_install, _cmd(item, 2, "reassign", assignee=m05.S1, actor=m05.S2), "NOT_AUTHORIZED"
    )
    taken = _apply(
        m05_install, _cmd(item, 2, "reassign", assignee=m05.SUP, actor=m05.SUP, permissions=sup)
    )
    assert (taken["snapshot"]["assignee_id"], taken["snapshot"]["version"]) == (m05.SUP, 3)


def test_mb3_pc_preconditions(m05_install: M05Install) -> None:
    sup = m05.SUPERVISOR_PERMISSIONS
    unassigned = _new_item(m05_install)
    _refused(m05_install, _cmd(unassigned, 1, "start_progress"), "PRECONDITION_FAILED")
    _refused(
        m05_install,
        _cmd(unassigned, 1, "reassign", assignee=m05.S2, actor=m05.SUP, permissions=sup),
        "PRECONDITION_FAILED",
    )
    _refused(m05_install, _cmd(unassigned, 1, "assign", assignee=m05.S1), "COMMAND_MALFORMED")
    claimed = _new_item(m05_install)
    _apply(m05_install, _cmd(claimed, 1, "claim"))
    _refused(m05_install, _cmd(claimed, 2, "claim", actor=m05.S2), "PRECONDITION_FAILED")
    _refused(
        m05_install,
        _cmd(claimed, 2, "assign", assignee=m05.S2, actor=m05.SUP, permissions=sup),
        "PRECONDITION_FAILED",
    )
    _refused(
        m05_install,
        _cmd(claimed, 2, "reassign", assignee=m05.S1, actor=m05.SUP, permissions=sup),
        "PRECONDITION_FAILED",
    )
    _apply(m05_install, _cmd(claimed, 2, "start_progress"))
    _refused(m05_install, _cmd(claimed, 3, "start_progress"), "PRECONDITION_FAILED")


# --- MA-07 / MA-08 / MA-09 / MA-11 / MA-13' ---------------------------------------


def test_ma07_every_action_on_a_resolved_item(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    _apply(m05_install, _cmd(item, 1, "claim"))
    _resolve_by_admin(m05_install, item)
    sup = m05.SUPERVISOR_PERMISSIONS
    for version in (3, 1):  # current and stale: W4 is reported first
        for command in (
            _cmd(item, version, "claim", actor=m05.S2),
            _cmd(item, version, "start_progress"),
            _cmd(item, version, "assign", assignee=m05.S2, actor=m05.SUP, permissions=sup),
            _cmd(item, version, "reassign", assignee=m05.S2, actor=m05.SUP, permissions=sup),
        ):
            _refused(m05_install, command, "ITEM_RESOLVED")


def test_ma08_stale_version(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    _apply(m05_install, _cmd(item, 1, "claim"))
    _refused(m05_install, _cmd(item, 1, "start_progress"), "STALE_VERSION")
    _refused(m05_install, _cmd(item, 3, "start_progress"), "STALE_VERSION")


def test_ma09_concurrent_claim(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    call = sql.SQL("SELECT {}.m05_apply_action(%s)").format(sql.Identifier(m05_install.schema))
    first, second = _cmd(item, 1, "claim"), _cmd(item, 1, "claim", actor=m05.S2)
    outcome: list[Any] = []
    holder = psycopg.connect(m05_install.ids.logins[m05.RUNTIME])
    waiter = psycopg.connect(m05_install.ids.logins[m05.RUNTIME])
    thread: threading.Thread | None = None
    try:
        for conn in (holder, waiter):
            conn.execute(f"SET ROLE {m05.RUNTIME}")
            conn.execute("SET lock_timeout = '15s'")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (m05_install.tenant[0],))
        holder.execute(call, (Jsonb(first),))  # open, uncommitted
        pid = waiter.execute("SELECT pg_backend_pid()").fetchone()[0]  # type: ignore[index]

        def run() -> None:
            try:
                waiter.execute(call, (Jsonb(second),))
                waiter.commit()
                outcome.append("applied")
            except psycopg.Error as error:
                outcome.append((error.sqlstate, error.diag.message_primary))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        blocked = False
        for _ in range(100):
            if _admin(
                m05_install, "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            ) == [("Lock",)]:
                blocked = True
                break
            threading.Event().wait(0.05)
        assert blocked, "the second claim was not observed waiting on the item lock"
        holder.commit()
        thread.join(timeout=20)
        assert outcome == [("P0001", "STALE_VERSION")], outcome
    finally:
        for conn in (holder, waiter):
            try:
                conn.rollback()
            finally:
                conn.close()
        if thread is not None:
            thread.join(timeout=5)
    assert _row(m05_install, item)["assignee_id"] == m05.S1
    assert [h["action"] for h in _history(m05_install, item)] == ["created", "claim"]


def test_ma11_unknown_and_foreign_items_share_item_not_found(
    m05_install: M05Install, foreign_install: M05Install
) -> None:
    _ready(m05_install)
    foreign = _new_item(foreign_install)
    for item in (_uuid(), foreign):
        _refused(m05_install, _cmd(item, 1, "claim"), "ITEM_NOT_FOUND")


def test_ma13_permission_shapes_not_authorized(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    for permissions in (
        {"read": True, "act": False, "supervise": False},
        {"read": False, "act": True, "supervise": False},
        {"read": True, "act": False, "supervise": True},
    ):
        _refused(m05_install, _cmd(item, 1, "claim", permissions=permissions), "NOT_AUTHORIZED")
    _refused(m05_install, _cmd(item, 1, "claim", actor=m05.W1, kind="workload"), "NOT_AUTHORIZED")


# --- MR-01..03 -----------------------------------------------------------------


def test_mr01_replay_after_intervening_actions_and_reassignment(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    k1 = _uuid()
    original = _apply(m05_install, _cmd(item, 1, "claim", key=k1))
    _apply(m05_install, _cmd(item, 2, "start_progress"))
    _apply(
        m05_install,
        _cmd(item, 3, "reassign", assignee=m05.S2, actor=m05.SUP,
             permissions=m05.SUPERVISOR_PERMISSIONS),
    )
    retry = _cmd(item, 1, "claim", key=k1)
    assert _prepare(m05_install, retry) == {
        "schema_version": 1, "decision": "REPLAY", "snapshot": original["snapshot"],
    }
    before = _snapshot(m05_install)
    assert _apply(m05_install, retry) == {
        "schema_version": 1, "outcome": "replayed", "snapshot": original["snapshot"],
    }
    assert _snapshot(m05_install) == before
    # After resolution: still the original snapshot while read+act hold now, and the
    # replay writes NOTHING (F-B3-REPLAY01: a replay that wrote the historical item
    # state back would silently reopen the item). Fresh full snapshot AFTER the
    # admin resolution; unchanged after prepare REPLAY and after apply replay.
    _resolve_by_admin(m05_install, item)
    resolved = _snapshot(m05_install)
    resolved_row = _row(m05_install, item)
    assert (resolved_row["state"], resolved_row["version"]) == ("resolved", 5)
    assert _prepare(m05_install, retry) == {
        "schema_version": 1, "decision": "REPLAY", "snapshot": original["snapshot"],
    }
    assert _snapshot(m05_install) == resolved
    assert _apply(m05_install, retry) == {
        "schema_version": 1, "outcome": "replayed", "snapshot": original["snapshot"],
    }
    assert _snapshot(m05_install) == resolved
    assert _row(m05_install, item) == resolved_row


def test_mr01r_replay_refused_when_read_or_act_revoked(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    k1 = _uuid()
    _apply(m05_install, _cmd(item, 1, "claim", key=k1))
    _resolve_by_admin(m05_install, item)
    for permissions in (
        {"read": True, "act": False, "supervise": False},
        {"read": False, "act": True, "supervise": False},
    ):
        _refused(
            m05_install, _cmd(item, 1, "claim", key=k1, permissions=permissions), "NOT_AUTHORIZED"
        )


def test_mr02_same_key_different_payload_conflicts(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    k1 = _uuid()
    _apply(m05_install, _cmd(item, 1, "claim", key=k1))
    _refused(m05_install, _cmd(item, 2, "claim", key=k1), "ACTION_KEY_CONFLICT")
    _refused(m05_install, _cmd(item, 1, "start_progress", key=k1), "ACTION_KEY_CONFLICT")


def test_mr03_same_key_different_actor(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    k1 = _uuid()
    _apply(m05_install, _cmd(item, 1, "claim", key=k1))
    _refused(m05_install, _cmd(item, 1, "claim", key=k1, actor=m05.S2), "ACTION_KEY_ACTOR_MISMATCH")


# --- MR-09 / MR-11 -----------------------------------------------------------------


def test_mr09_direct_apply_equals_prepare_then_apply(m05_install: M05Install) -> None:
    routed, direct = _new_item(m05_install), _new_item(m05_install)
    routed_cmd, direct_cmd = _cmd(routed, 1, "claim"), _cmd(direct, 1, "claim")
    assert _prepare(m05_install, routed_cmd)["decision"] == "NEW"
    a, b = _apply(m05_install, routed_cmd), _apply(m05_install, direct_cmd)

    def strip(result: dict[str, Any]) -> dict[str, Any]:
        return {**result, "snapshot": {k: v for k, v in result["snapshot"].items()
                                       if k not in ("item_id", "client_action_id")}}

    assert strip(a) == strip(b)
    ha, hb = _history(m05_install, routed), _history(m05_install, direct)
    keys = ("seq", "action", "actor_id", "before", "after")
    assert [{k: h[k] for k in keys} for h in ha] == [{k: h[k] for k in keys} for h in hb]


def _malformed(item: str) -> dict[str, Any]:
    base = _cmd(item, 1, "claim")

    def top(**changes: Any) -> dict[str, Any]:
        return {**base, **changes}

    def without(key: str) -> dict[str, Any]:
        return {k: v for k, v in base.items() if k != key}

    def act(**changes: Any) -> dict[str, Any]:
        return {**base, "action": {**base["action"], **changes}}

    def perms(value: Any) -> dict[str, Any]:
        return {**base, "permissions": value}

    cases: dict[str, Any] = {
        "not-object": [base],
        "extra-digest": top(digest="00" * 32),
        "extra-key": top(note="x"),
        "schema-version-2": top(schema_version=2),
        "schema-version-bool": top(schema_version=True),
        "item-uppercase": top(item_id=item.upper()),
        "key-not-uuid": top(client_action_id="k1"),
        "version-zero": top(expected_version=0),
        "version-negative": top(expected_version=-1),
        "version-float": top(expected_version=1.5),
        "version-integral-float": top(expected_version=1.0),
        "version-string": top(expected_version="1"),
        "version-bool": top(expected_version=True),
        "version-too-large": top(expected_version=m05.MAX_VERSION + 1),
        "action-not-object": top(action="claim"),
        "action-type-unknown": act(type="close"),
        "claim-with-assignee": act(assignee_id=m05.S2),
        "action-extra": act(state="open"),
        "assign-without-assignee": {**base, "action": {"type": "assign"}},
        "assignee-not-uuid": {**base, "action": {"type": "assign", "assignee_id": "s2"}},
        "assignee-uppercase": {**base, "action": {"type": "assign", "assignee_id": m05.S2.upper()}},
        "permissions-not-object": perms("all"),
        "permissions-missing-key": perms({"read": True, "act": True}),
        "permissions-extra-key": perms({**m05.STAFF_PERMISSIONS, "admin": True}),
        "permissions-not-bool": perms({"read": 1, "act": True, "supervise": False}),
        "attribution-extra": top(attribution={**base["attribution"], "actor_id": m05.S1}),
        "attribution-purpose": top(attribution={**base["attribution"], "purpose_code": "x"}),
    }
    for key in ("schema_version", "item_id", "client_action_id", "expected_version", "action",
                "permissions", "attribution"):
        cases[f"missing-{key}"] = without(key)
    return cases


def test_mr11_closed_schema_refused_as_malformed(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    for name, command in _malformed(item).items():
        try:
            _refused(m05_install, command, "COMMAND_MALFORMED")
        except AssertionError as error:
            raise AssertionError(f"case {name}: {error}") from error
    _apply(m05_install, _cmd(item, 1, "claim"))  # well-formed control
    # Upper bound itself is well-formed (stale, not malformed).
    _refused(m05_install, _cmd(item, m05.MAX_VERSION, "start_progress"), "STALE_VERSION")


# --- MB3-P: prepare -----------------------------------------------------------------


def test_mb3_p_prepare_classifies_and_holds_the_lock(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    assert _prepare(m05_install, _cmd(item, 1, "claim")) == {
        "schema_version": 1, "decision": "NEW",
    }
    # Prepare takes the item lock (FOR UPDATE) inside the caller's transaction.
    with psycopg.connect(m05_install.ids.logins[m05.RUNTIME]) as holder:
        holder.execute(f"SET ROLE {m05.RUNTIME}")
        holder.execute("SELECT set_config('app.tenant_id', %s, true)", (m05_install.tenant[0],))
        holder.execute(
            sql.SQL("SELECT {}.m05_prepare_action(%s)").format(sql.Identifier(m05_install.schema)),
            (Jsonb(_cmd(item, 1, "claim")),),
        )
        with (
            m02.connect_admin(m05_install.ids) as probe,
            pytest.raises(psycopg.errors.LockNotAvailable),
        ):
            probe.execute(
                sql.SQL(
                    "SELECT 1 FROM {}.m05_followup_items WHERE item_id = %s FOR UPDATE NOWAIT"
                ).format(sql.Identifier(m05_install.schema)),
                (item,),
            )
        holder.rollback()


def test_mb3_p_prepare_does_not_envelope_tenant_context_failure(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    with psycopg.connect(m05_install.ids.logins[m05.RUNTIME], autocommit=True) as conn:
        conn.execute(f"SET ROLE {m05.RUNTIME}")
        for function in ("m05_prepare_action", "m05_apply_action"):
            with pytest.raises(psycopg.Error) as caught, conn.transaction():
                conn.execute(
                    sql.SQL("SELECT {}.{}(%s)").format(
                        sql.Identifier(m05_install.schema), sql.Identifier(function)
                    ),
                    (Jsonb(_cmd(item, 1, "claim")),),
                )
            assert caught.value.sqlstate == "22023", function


# --- MB3-A: atomicity --------------------------------------------------------------


@pytest.mark.parametrize("table", ["m05_followup_history", "m05_followup_actions"])
def test_mb3_a_fault_at_record_insert_rolls_back_the_item(
    m05_install: M05Install, table: str
) -> None:
    item = _new_item(m05_install)
    schema = sql.Identifier(m05_install.schema)
    _admin(
        m05_install,
        sql.SQL(
            "CREATE FUNCTION {}.mb3_fault() RETURNS trigger LANGUAGE plpgsql AS"
            " $f$BEGIN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'MB3_FAULT'; END$f$"
        ).format(schema),
    )
    try:
        _admin(
            m05_install,
            sql.SQL(
                "CREATE TRIGGER mb3_fault BEFORE INSERT ON {}.{} FOR EACH ROW"
                " EXECUTE FUNCTION {}.mb3_fault()"
            ).format(schema, sql.Identifier(table), schema),
        )
        before = _snapshot(m05_install)
        with pytest.raises(psycopg.Error) as caught:
            _call(m05_install, "m05_apply_action", _cmd(item, 1, "claim"))
        assert (caught.value.sqlstate, caught.value.diag.message_primary) == (
            "P0001", "MB3_FAULT",
        )
        assert _snapshot(m05_install) == before
    finally:
        _admin(
            m05_install,
            sql.SQL("DROP TRIGGER IF EXISTS mb3_fault ON {}.{}").format(
                schema, sql.Identifier(table)
            ),
        )
        _admin(m05_install, sql.SQL("DROP FUNCTION IF EXISTS {}.mb3_fault()").format(schema))
    _apply(m05_install, _cmd(item, 1, "claim"))  # control after removal


# --- MB3-T: retired by B4 -----------------------------------------------------------
# The reserved resolve shapes (ACTION_NOT_IMPLEMENTED until B4) are replaced by the B4
# behaviour rows, per this row's own rule: MA-01/03/OR, RP-1..13 and the MR-10 matrix
# in tests/m05/test_m05_resolve_postgres.py and test_m05_resolve_binding_postgres.py,
# plus MB3-T4 below (the B3 command shape {type, outcome_code} is now malformed).


def test_mb3_t4_b3_resolve_shape_is_malformed(m05_install: M05Install) -> None:
    item = _new_item(m05_install)
    for action_type in m05.RESERVED_ACTIONS:
        _refused(m05_install, _cmd(item, 1, action_type,
                                   action_extra={"outcome_code": "rescheduled"}),
                 "COMMAND_MALFORMED")
