"""L-1S package S-B — t045 `m05_due_windows` on PostgreSQL 17 (runtime role, real units).

Governing: requirements v1r1 R-SB-1a/1b/2/3/8a; architecture v1 sections 3-5; pins P2;
red-package delta D1 (jsonb scalar result `{"schema_version": 1, "keys": [{deadline,
reminder_id}, ...]}`), D2 (exact boundary through the source-tied predicate at a FIXED
cutoff; no caller-controlled cutoff reaches the selector), D3 (plan evidence on the
source-tied `DUE_QUERIES` with the selector's own parameters, custom and generic plans).

- SB-T01  boundary: DUE_PREDICATE at a fixed cutoff equals NOT intake_reply_is_timely for
          deadline = cutoff - 1 us / = cutoff / + 1 us (the instant is due), and is not
          TRUE for a NULL deadline; and the real t045 returns a 1 h-overdue window and not a
          window due in 1 h.
- SB-T02  result shape exactly {schema_version, keys}; each key exactly {deadline,
          reminder_id}; deadline in the M05 timestamp format.
- SB-T03  replied / opted_out / no_reply windows are never returned.
- SB-T04  order (deadline, reminder_id) and limit.
- SB-T05  tie-heavy keyset paging (12 windows on 3 identical deadlines, page 5): the
          concatenated pages equal the full ordered due set — no skip, no repeat.
- SB-T06  until is inclusive; after is exclusive; after + until bound both sides.
- SB-T07  malformed commands -> COMMAND_MALFORMED (missing/unknown keys, schema_version,
          limit 0/501/bool/float/string, cursor not an object, cursor extra/missing key,
          naive or non-canonical timestamp, non-UUID or upper-case UUID); after >= until ->
          BOUNDS_INVALID; nothing written in any case.
- SB-A02  a workload that is not the tenant's timer, and an actor -> WORKLOAD_NOT_AUTHORIZED.
- SB-A03  tenant isolation: the other tenant's due windows are never returned.
- SB-R01  read-only: a selection holds no row lock (a concurrent t041 on a returned window
          does not block while the selecting transaction is still open) and writes nothing.
- SB-P01..P03  plan rows live in test_sb_plan_postgres.py (own tenant: its backlog must
          not leak into these rows).
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

import pytest
import sa_support as sa
import sb_support as sb

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


def _fresh(install: sb.SBInstall, n: int, *, hours_past: float = 25.0) -> list[str]:
    return [sb.due_window(install, hours_past=hours_past)[2] for _ in range(n)]


# --- SB-T01 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("offset_us", (-1, 0, 1))
def test_sb_t01_boundary_fixed_cutoff(sb_install: sb.SBInstall, offset_us: int) -> None:
    sb_install.require()
    from haloflow.m05.due_units import DUE_PREDICATE

    cutoff = sb.utc(2026, 11, 6, 12, 0, 0)
    deadline = cutoff + offset_us * sb.MICRO
    due = sb.predicate_holds(sb_install, DUE_PREDICATE, deadline, cutoff)
    assert due is (not sb.timely(sb_install, cutoff, deadline))
    assert due is (offset_us <= 0)


def test_sb_t01_null_and_real_selector(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    from haloflow.m05.due_units import DUE_PREDICATE

    assert sb.predicate_holds(sb_install, DUE_PREDICATE, None, sb.utc(2026, 1, 1)) is not True
    sb.close_all_awaiting(sb_install)
    overdue = _fresh(sb_install, 1, hours_past=25)[0]      # deadline 1 h ago
    not_yet = _fresh(sb_install, 1, hours_past=23)[0]      # deadline in 1 h
    ids = [r for _, r in sb.select(sb_install, sb.due_cmd())]
    assert overdue in ids and not_yet not in ids


# --- SB-T02..T06 --------------------------------------------------------------------------------


def test_sb_t02_result_shape(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    _fresh(sb_install, 2)
    rows = sb.select(sb_install, sb.due_cmd())
    assert len(rows) == 2
    for deadline, reminder in rows:
        assert re.fullmatch(sa.TIMESTAMP_PATTERN, deadline), deadline
        assert str(uuid.UUID(reminder)) == reminder


def test_sb_t03_states_excluded(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    replied, opted, closed, live = _fresh(sb_install, 4)
    sa.admin(sb_install, sa.q(sb_install, "UPDATE {s}.m05_reminder_windows SET state = 'replied'"
                                          " WHERE reminder_id = %s"), (replied,))
    sa.admin(sb_install, sa.q(sb_install, "UPDATE {s}.m05_reminder_windows"
                                          " SET state = 'opted_out', closed_decision = 'opted_out'"
                                          " WHERE reminder_id = %s"), (opted,))
    assert sa.close(sb_install, closed)["decision"] == "no_reply"
    assert [r for _, r in sb.select(sb_install, sb.due_cmd())] == [live]


def test_sb_t04_order_and_limit(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    reminders = _fresh(sb_install, 5)
    base = sb.utc(2026, 1, 1, 9)
    for i, reminder in enumerate(reversed(reminders)):
        sb.set_deadline(sb_install, reminder, base + timedelta(minutes=i))
    expected = [r for _, r in sb.awaiting_due(sb_install)]
    assert expected == list(reversed(reminders))
    assert [r for _, r in sb.select(sb_install, sb.due_cmd(limit=3))] == expected[:3]
    assert [r for _, r in sb.select(sb_install, sb.due_cmd(limit=500))] == expected


def test_sb_t05_tie_heavy_paging(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    reminders = _fresh(sb_install, 12)
    base = sb.utc(2026, 2, 1, 9)
    for i, reminder in enumerate(reminders):
        sb.set_deadline(sb_install, reminder, base + timedelta(hours=i % 3))
    expected = [(sb.iso(d), r) for d, r in sb.awaiting_due(sb_install)]
    pages: list[tuple[str, str]] = []
    after = None
    while True:
        page = sb.select(sb_install, sb.due_cmd(after=after, limit=5))
        pages += page
        if len(page) < 5:
            break
        after = sb.key(*page[-1])
    assert pages == expected
    assert len(set(pages)) == len(pages) == 12


def test_sb_t06_after_exclusive_until_inclusive(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    _fresh(sb_install, 6)
    full = [(sb.iso(d), r) for d, r in sb.awaiting_due(sb_install)]
    k1, k4 = sb.key(*full[1]), sb.key(*full[4])
    assert sb.select(sb_install, sb.due_cmd(after=k1)) == full[2:]
    assert sb.select(sb_install, sb.due_cmd(until=k4)) == full[:5]
    assert sb.select(sb_install, sb.due_cmd(after=k1, until=k4)) == full[2:5]


# --- SB-T07 ---------------------------------------------------------------------------------


def _malformed() -> list[tuple[str, Any, str]]:
    good_key = sb.key("2026-01-01T09:00:00.000000Z", str(uuid.uuid4()))
    base = sb.due_cmd()
    cases: list[tuple[str, Any, str]] = []

    def variant(name: str, code: str = "COMMAND_MALFORMED", **changes: Any) -> None:
        command = {**base, **changes}
        cases.append((name, command, code))

    missing = dict(base)
    del missing["limit"]
    cases.append(("missing-limit", missing, "COMMAND_MALFORMED"))
    variant("unknown-key", extra=1)
    variant("schema-2", schema_version=2)
    for name, value in (("limit-0", 0), ("limit-501", 501), ("limit-bool", True),
                        ("limit-float", 10.5), ("limit-str", "10")):
        variant(name, limit=value)
    variant("after-list", after=[good_key["deadline"], good_key["reminder_id"]])
    variant("after-extra", after={**good_key, "x": 1})
    variant("after-missing", after={"deadline": good_key["deadline"]})
    variant("naive-ts", after={**good_key, "deadline": "2026-01-01T09:00:00.000000"})
    variant("offset-ts", after={**good_key, "deadline": "2026-01-01T09:00:00.000000+00:00"})
    variant("no-micro-ts", after={**good_key, "deadline": "2026-01-01T09:00:00Z"})
    variant("bad-uuid", until={**good_key, "reminder_id": "not-a-uuid"})
    variant("upper-uuid", until={**good_key, "reminder_id": good_key["reminder_id"].upper()})
    variant("reversed", "BOUNDS_INVALID", after=good_key, until=good_key)
    later = sb.key("2026-01-01T10:00:00.000000Z", good_key["reminder_id"])
    variant("reversed-2", "BOUNDS_INVALID", after=later, until=good_key)
    return cases


@pytest.mark.parametrize(("name", "command", "code"), _malformed(),
                         ids=[c[0] for c in _malformed()])
def test_sb_t07_malformed(sb_install: sb.SBInstall, name: str, command: Any, code: str) -> None:
    sb_install.require()
    sb.refused(sb_install, command, code)


# --- SB-A02 / SB-A03 ---------------------------------------------------------------------------


@pytest.mark.parametrize(("principal", "kind"), ((sa.W_DISPATCH, "workload"),
                                                 (sa.W_OTHER, "workload"),
                                                 (sa.ACTOR, "actor")))
def test_sb_a02_not_timer(sb_install: sb.SBInstall, principal: str, kind: str) -> None:
    sb_install.require()
    sb.refused(sb_install, sb.due_cmd(principal=principal, kind=kind), "WORKLOAD_NOT_AUTHORIZED")


def test_sb_a03_tenant_isolation(sb_install: sb.SBInstall,
                                 sb_other_install: sb.SBInstall) -> None:
    sb_install.require()
    sb_other_install.require()
    sb.close_all_awaiting(sb_install)
    sb.close_all_awaiting(sb_other_install)
    mine = _fresh(sb_install, 1)
    theirs = _fresh(sb_other_install, 1)
    assert [r for _, r in sb.select(sb_install, sb.due_cmd())] == mine
    assert [r for _, r in sb.select(sb_other_install, sb.due_cmd())] == theirs


# --- SB-R01 -----------------------------------------------------------------------------------


def test_sb_r01_read_only_no_row_lock(sb_install: sb.SBInstall) -> None:
    sb_install.require()
    sb.close_all_awaiting(sb_install)
    (reminder,) = _fresh(sb_install, 1)
    before = sa.snapshot(sb_install)
    with sa.held(sb_install, sb.SELECTOR, sb.due_cmd()) as result:
        assert [k["reminder_id"] for k in result["keys"]] == [reminder]
        assert sa.snapshot(sb_install) == before
        # t041 on the returned window completes while the selection is still open
        # (lock_timeout: a row lock taken by the selector fails this row, never hangs it)
        with sa.runtime_conn(sb_install) as conn, conn.transaction():
            conn.execute("SET LOCAL lock_timeout = '3s'")
            closed = sa.call_in(conn, sb_install, "m05_close_window", sa.close_cmd(reminder))
        assert closed["decision"] == "no_reply"
