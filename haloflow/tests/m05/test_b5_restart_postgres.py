"""B5 real process-restart evidence for M05 staff actions (UC1 part B, tranche B5;
mechanism aligned Claude/Codex).

Method. Each row runs the action in a separate OS process (`restart_child.py`,
started with `<python> -I`, `start_new_session=True`, cwd = haloflow/), composed
through the TEST support exactly as the in-process B4 rows are (service_support
runtime/context, EventFoundation(producer m05_followup) + M05Service with
TestNoteKeys). The child reports READY (os pid, backend pid of the action
transaction), APPEND (resolution event id), IN_TX (xid inside the open transaction,
after the service's statements and before COMMIT), COMMITTED (after COMMIT, before
the service returns) and RESULT. The parent waits for each exact marker in order
(30 s each, 60 s per case), kills with os.killpg(SIGKILL), and before ANY row read
proves the killed backend gone from pg_stat_activity on a fresh admin connection; an
IN_TX kill additionally proves pg_xact_status(xid) = 'aborted'. Every case gets a
NEW disposable tenant (unique label, reset before and after) and new items. The
oracle is a scoped baseline read after setup and before child 1 (item row, its
history, the action row for (item, key), events linked to the item with their links
and outbox rows, the item's source event, the note row for the key); assertions are
deltas against it, and every baseline row must still be present unchanged. Event
identity: m05_followup_actions.event_id for (item, key), read fresh, compared with
the child's APPEND id. Finally no pg_stat_activity row may carry the case tag or any
recorded backend pid.

Non-claims:
- CI PostgreSQL 17 only (the harness fails below 170000 by design); not run locally.
- Disposable fixtures only: no retained database, tenant or item is reused.
- The ActionResult API is unchanged: (outcome, snapshot), no event id; event
  identity is read from the database.
- These rows prove restart safety of the B4 implementation; they may pass at red.

Timing notes: the gateway's lock_timeout is 5 s, so in MR-07 the parent releases or
kills child 1 as soon as `b1 = ANY(pg_blocking_pids(b2))` is observed; a paused
transaction is also bounded by the gateway's transaction_timeout (about 32 s).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import m02_support
import m05_support as m05
import pytest
import resolve_support as rs
import service_support as sv
import staff_support as st
from psycopg import sql
from test_m05_actions_postgres import _cmd
from test_m05_service_postgres import _item

pytestmark = pytest.mark.postgres

CHILD = Path(__file__).resolve().with_name("restart_child.py")
HALOFLOW_ROOT = Path(__file__).resolve().parents[2]
ROLES = ("haloflow_runtime", "haloflow_migrator", "haloflow_provisioner")
MARKER_SECONDS = 30.0
CASE_SECONDS = 60.0
CLEANUP_SECONDS = 30.0
POLL_SECONDS = 0.02
NOTE = "Synthetic B5 restart note; no patient data."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
          m05_reset: Any) -> Iterator[st.StaffInstall]:
    """A NEW disposable tenant per case (unique label), reset before and after."""

    tenant = m02_support.tenant_for(f"{request.node.nodeid}:b5:{uuid4()}")
    m05_reset(tenant)
    try:
        yield st.StaffInstall(m02_ids, tenant)
    finally:
        m05_reset(tenant)


def _setup(install: st.StaffInstall, action: str) -> tuple[UUID, UUID, int]:
    """A new item: open (version 1) for claim; claimed by S1 (version 2) for resolve."""

    item = _item(install)
    version = 1
    if action != "claim":
        st.call(install, "m05_apply_action_v2", _cmd(str(item), 1, "claim", actor=m05.S1))
        version = 2
    return item, uuid4(), version


# ---------------------------------------------------------------------------
# Child process control
# ---------------------------------------------------------------------------


class Child:
    def __init__(self, case: Case, spec: dict[str, Any]) -> None:
        self.case = case
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.seen: list[str] = []
        self.stderr: list[str] = []
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(CHILD)], cwd=HALOFLOW_ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._threads = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines),
                             daemon=True),
            threading.Thread(target=self._drain, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self.proc.stdin.write(json.dumps(spec) + "\n")
        self.proc.stdin.flush()

    @staticmethod
    def _pump(stream: Any, sink: queue.Queue[str | None]) -> None:
        for line in stream:
            sink.put(line.rstrip("\n"))
        sink.put(None)

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip("\n"))

    def _diagnostic(self) -> str:
        return f"seen={self.seen} rc={self.proc.poll()} stderr={self.stderr[-20:]}"

    def expect(self, marker: str) -> dict[str, Any]:
        """The NEXT stdout line must be `marker` (strict order), within the bounds."""

        try:
            line = self.lines.get(timeout=self.case.remaining())
        except queue.Empty:
            pytest.fail(f"timeout waiting for {marker}: {self._diagnostic()}")
        if line is None:
            pytest.fail(f"child ended before {marker}: {self._diagnostic()}")
        self.seen.append(line)
        if line != marker and not line.startswith(marker + " "):
            pytest.fail(f"expected {marker}, got {line!r}: {self._diagnostic()}")
        return _parse(line)

    def go(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write("go\n")
        self.proc.stdin.flush()

    def kill(self) -> None:
        os.killpg(self.proc.pid, signal.SIGKILL)  # pgid == pid (start_new_session)
        self.proc.wait(timeout=self.case.remaining())
        assert self.proc.returncode == -signal.SIGKILL, self._diagnostic()

    def finish(self) -> None:
        try:
            self.proc.wait(timeout=self.case.remaining())
        except subprocess.TimeoutExpired:
            pytest.fail(f"child did not exit: {self._diagnostic()}")
        assert self.proc.returncode == 0, self._diagnostic()

    def reap(self) -> None:
        if self.proc.poll() is None:  # alive, or exited but not reaped: pgid still ours
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        try:
            self.proc.wait(timeout=CLEANUP_SECONDS)
        finally:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            for thread in self._threads:
                thread.join(CLEANUP_SECONDS)


def _parse(line: str) -> dict[str, Any]:
    head, found, snapshot = line.partition(" snapshot=")
    fields: dict[str, Any] = {}
    for token in head.split(" ")[1:]:
        key, _, value = token.partition("=")
        fields[key] = value
    if found:
        fields["snapshot"] = json.loads(snapshot)
    return fields


class Case:
    """One row: a 60 s budget, a unique application_name tag, its children and the
    backends they reported; `close` kills, reaps and proves no residue."""

    def __init__(self, install: st.StaffInstall, item: UUID, key: UUID, version: int,
                 action: str) -> None:
        self.install = install
        self.item, self.key, self.version, self.action = item, key, version, action
        self.tag = f"haloflow-b5-{uuid4().hex[:16]}"
        self.deadline = time.monotonic() + CASE_SECONDS
        self.children: list[Child] = []
        self.backends: list[int] = []

    def remaining(self, bound: float = MARKER_SECONDS) -> float:
        left = min(bound, self.deadline - time.monotonic())
        if left <= 0:
            pytest.fail("case exceeded its 60 s bound")
        return left

    def spawn(self, pause: str) -> Child:
        spec = {
            "logins": {role: self.install.ids.logins[role] for role in ROLES},
            "tenant": list(self.install.tenant),
            "item_id": str(self.item), "client_action_id": str(self.key),
            "expected_version": self.version, "action": self.action,
            "note": NOTE if self.action == "resolve_note" else None,
            "pause": pause, "tag": self.tag,
        }
        child = Child(self, spec)
        self.children.append(child)
        return child

    def ready(self, child: Child) -> int:
        backend = int(child.expect("READY")["backend"])
        self.backends.append(backend)
        return backend

    def backend_gone(self, backend: int) -> None:
        deadline = time.monotonic() + self.remaining()
        while time.monotonic() < deadline:
            if not st.admin_rows(self.install, "SELECT 1 FROM pg_catalog.pg_stat_activity"
                                 " WHERE pid = %s", (backend,)):
                return
            time.sleep(POLL_SECONDS)
        pytest.fail(f"backend {backend} still present")

    def blocked(self, waiter: int, blocker: int) -> None:
        deadline = time.monotonic() + self.remaining()
        while time.monotonic() < deadline:
            row = st.admin_rows(self.install, "SELECT %s = ANY(pg_catalog.pg_blocking_pids(%s))",
                                (blocker, waiter))
            if row and row[0][0] is True:
                return
            time.sleep(POLL_SECONDS)
        pytest.fail(f"backend {waiter} never blocked by {blocker}")

    def close(self) -> None:
        for child in self.children:
            child.reap()
        deadline = time.monotonic() + CLEANUP_SECONDS
        residue: list[tuple[Any, ...]] = []
        while time.monotonic() < deadline:
            residue = st.admin_rows(
                self.install, "SELECT pid, application_name FROM pg_catalog.pg_stat_activity"
                " WHERE application_name = %s OR pid = ANY(%s::integer[])",
                (self.tag, self.backends))
            if not residue:
                return
            time.sleep(POLL_SECONDS)
        raise AssertionError(f"connections left behind: {residue}")


# ---------------------------------------------------------------------------
# Scoped state, deltas, identity
# ---------------------------------------------------------------------------

_LINKED = "(SELECT k.event_id FROM {s}.patient_event_links k WHERE k.link_id = %(item)s)"
SCOPED: dict[str, str] = {
    "item": "SELECT to_jsonb(t)::text FROM {s}.m05_followup_items t WHERE t.item_id = %(item)s",
    "history": "SELECT to_jsonb(t)::text FROM {s}.m05_followup_history t"
               " WHERE t.item_id = %(item)s",
    "actions": "SELECT to_jsonb(t)::text FROM {s}.m05_followup_actions t"
               " WHERE t.item_id = %(item)s AND t.client_action_id = %(key)s",
    "notes": "SELECT to_jsonb(t)::text FROM {s}.m05_followup_notes t"
             " WHERE t.item_id = %(item)s AND t.client_action_id = %(key)s",
    "events": "SELECT to_jsonb(e)::text FROM {s}.patient_events e WHERE e.event_id IN " + _LINKED,
    "links": "SELECT to_jsonb(l)::text FROM {s}.patient_event_links l"
             " WHERE l.event_id IN " + _LINKED,
    "outbox": "SELECT to_jsonb(o)::text FROM {s}.event_handoff_outbox o"
              " WHERE o.source_event_id IN " + _LINKED,
    "source": "SELECT to_jsonb(e)::text FROM {s}.patient_events e WHERE e.event_id ="
              " (SELECT i.source_event_id FROM {s}.m05_followup_items i"
              " WHERE i.item_id = %(item)s)",
}


def scoped(case: Case) -> dict[str, list[str]]:
    """Every scoped row as sorted JSON text, each table on a fresh admin connection."""

    params = {"item": str(case.item), "key": str(case.key)}
    out: dict[str, list[str]] = {}
    for table, text in SCOPED.items():
        query = sql.SQL(text).format(s=sql.Identifier(case.install.schema))
        out[table] = sorted(r[0] for r in st.admin_rows(case.install, query, params))
    return out


def action_event_id(case: Case) -> str | None:
    rows = st.admin_rows(case.install, sql.SQL(
        "SELECT event_id::text FROM {}.m05_followup_actions"
        " WHERE item_id = %s AND client_action_id = %s").format(
        sql.Identifier(case.install.schema)), (str(case.item), str(case.key)))
    assert len(rows) == 1, rows
    value: str | None = rows[0][0]
    return value


def fresh_snapshot(case: Case) -> dict[str, Any]:
    rows = st.admin_rows(case.install, sql.SQL(
        "SELECT result_snapshot FROM {}.m05_followup_actions"
        " WHERE item_id = %s AND client_action_id = %s").format(
        sql.Identifier(case.install.schema)), (str(case.item), str(case.key)))
    assert len(rows) == 1, rows
    snapshot: dict[str, Any] = rows[0][0]
    return snapshot


def event_absent(case: Case, event_id: str) -> None:
    """No event, link or outbox row with this id anywhere in the tenant."""

    s = sql.Identifier(case.install.schema)
    for text in ("SELECT count(*) FROM {s}.patient_events WHERE event_id = %s",
                 "SELECT count(*) FROM {s}.patient_event_links WHERE event_id = %s",
                 "SELECT count(*) FROM {s}.event_handoff_outbox WHERE source_event_id = %s"):
        assert st.admin_rows(case.install, sql.SQL(text).format(s=s), (event_id,)) == [(0,)], (
            text, event_id)


def xact_status(case: Case, xid: str) -> str | None:
    rows = st.admin_rows(case.install, "SELECT pg_catalog.pg_xact_status(%s::xid8)", (xid,))
    status: str | None = rows[0][0]
    return status


def check_delta(case: Case, base: dict[str, list[str]], after: dict[str, list[str]],
                event_id: str | None = None) -> dict[str, Any]:
    """Exactly one applied action of `case.action` relative to the baseline; every
    baseline row other than the item is still present unchanged. Returns the action row."""

    added: dict[str, list[dict[str, Any]]] = {}
    for table in SCOPED:
        if table == "item":
            continue
        missing = [r for r in base[table] if r not in after[table]]
        assert not missing, (table, "baseline row changed or removed", missing)
        added[table] = [json.loads(r) for r in after[table] if r not in base[table]]
    assert added["source"] == []
    (old,) = [json.loads(r) for r in base["item"]]
    (new,) = [json.loads(r) for r in after["item"]]
    assert new["item_id"] == old["item_id"] == str(case.item)
    for field in ("source_event_id", "item_kind", "patient_id", "create_digest", "created_at"):
        assert new[field] == old[field], field
    assert new["version"] == old["version"] + 1 == case.version + 1
    assert new["assignee_id"] == m05.S1
    prior = max(json.loads(r)["seq"] for r in base["history"])
    (history,) = added["history"]
    (action,) = added["actions"]
    assert (history["item_id"], history["seq"], history["actor_id"]) == (
        str(case.item), prior + 1, m05.S1)
    assert (action["item_id"], action["client_action_id"], action["actor_id"]) == (
        str(case.item), str(case.key), m05.S1)
    if case.action == "claim":
        assert (history["action"], history["resolution_event_id"]) == ("claim", None)
        assert (action["requested_action"], action["event_id"]) == ("claim", None)
        assert (new["state"], new["outcome_code"]) == ("open", None)
        for table in ("events", "links", "outbox", "notes"):
            assert added[table] == [], (table, added[table])
        return action
    assert event_id is not None
    (event,) = added["events"]
    assert (event["event_id"], event["event_type"]) == (event_id, sv.RESOLUTION_TYPE)
    assert added["links"] == [{"event_id": event_id, "link_type": rs.ITEM_LINK,
                               "link_id": str(case.item), "relationship": rs.RELATIONSHIP}]
    (outbox,) = added["outbox"]
    assert outbox["source_event_id"] == event_id
    assert (history["action"], history["resolution_event_id"]) == ("resolve", event_id)
    assert (action["requested_action"], action["event_id"]) == ("resolve", event_id)
    assert (new["state"], new["outcome_code"]) == ("resolved", "rescheduled")
    notes = added["notes"]
    if case.action == "resolve_note":
        (note,) = notes
        assert (note["item_id"], note["client_action_id"]) == (str(case.item), str(case.key))
    else:
        assert notes == []
    return action


def expected_snapshot(case: Case) -> dict[str, Any]:
    """The in-process B4 expectation (test_ma01 form) for an applied action."""

    resolved = case.action != "claim"
    return {"schema_version": 1, "item_id": str(case.item),
            "client_action_id": str(case.key), "action": "resolve" if resolved else "claim",
            "version": case.version + 1, "state": "resolved" if resolved else "open",
            "assignee_id": m05.S1, "outcome_code": "rescheduled" if resolved else None}


def _case(install: st.StaffInstall, action: str) -> Case:
    item, key, version = _setup(install, action)
    return Case(install, item, key, version, action)


# ---------------------------------------------------------------------------
# MR-05: killed after COMMIT, retry replays
# ---------------------------------------------------------------------------


def _killed_after_commit_replays(install: st.StaffInstall, action: str) -> None:
    case = _case(install, action)
    try:
        base = scoped(case)
        first = case.spawn("after_commit")
        b1 = case.ready(first)
        e1 = first.expect("APPEND")["event_id"] if action != "claim" else None
        first.expect("COMMITTED")
        first.kill()
        case.backend_gone(b1)
        after_kill = scoped(case)
        recorded = fresh_snapshot(case)
        row = check_delta(case, base, after_kill, e1)
        assert row["result_snapshot"] == recorded == expected_snapshot(case)
        assert action_event_id(case) == e1

        second = case.spawn("none")
        b2 = case.ready(second)
        second.expect("COMMITTED")
        result = second.expect("RESULT")
        second.finish()
        case.backend_gone(b2)
        assert (result["outcome"], result["snapshot"]) == ("replayed", recorded)
        assert scoped(case) == after_kill  # +1 exactly once across both children
        assert action_event_id(case) == e1
    finally:
        case.close()


def test_mr05a_claim_killed_after_commit_replays(fresh):
    _killed_after_commit_replays(fresh, "claim")


def test_mr05b_noted_resolve_killed_after_commit_replays(fresh):
    _killed_after_commit_replays(fresh, "resolve_note")


def test_mr05c_plain_resolve_killed_after_commit_replays(fresh):
    _killed_after_commit_replays(fresh, "resolve_plain")


# ---------------------------------------------------------------------------
# MR-06: killed inside the transaction, nothing persists, retry applies anew
# ---------------------------------------------------------------------------


def _killed_in_tx_then_new(install: st.StaffInstall, action: str) -> None:
    case = _case(install, action)
    try:
        base = scoped(case)
        first = case.spawn("in_tx")
        b1 = case.ready(first)
        e1 = first.expect("APPEND")["event_id"] if action != "claim" else None
        x1 = first.expect("IN_TX")["xid"]
        first.kill()
        case.backend_gone(b1)
        assert xact_status(case, x1) == "aborted"
        assert scoped(case) == base  # zero rows in every coupled table for the key
        if e1 is not None:
            event_absent(case, e1)

        second = case.spawn("none")
        b2 = case.ready(second)
        e2 = second.expect("APPEND")["event_id"] if action != "claim" else None
        second.expect("COMMITTED")
        result = second.expect("RESULT")
        second.finish()
        case.backend_gone(b2)
        assert result["outcome"] == "applied"
        after = scoped(case)
        row = check_delta(case, base, after, e2)
        assert result["snapshot"] == row["result_snapshot"] == expected_snapshot(case)
        assert action_event_id(case) == e2
        if e1 is not None:
            assert e2 != e1
            event_absent(case, e1)
    finally:
        case.close()


def test_mr06a_claim_killed_in_tx_leaves_nothing_then_new(fresh):
    _killed_in_tx_then_new(fresh, "claim")


def test_mr06b_noted_resolve_killed_in_tx_then_new(fresh):
    _killed_in_tx_then_new(fresh, "resolve_note")


# ---------------------------------------------------------------------------
# MR-07: a second process waits on the first one's item lock
# ---------------------------------------------------------------------------


def test_mr07a_waiter_blocked_by_child1_then_replays(fresh):
    case = _case(fresh, "resolve_note")
    try:
        base = scoped(case)
        first = case.spawn("in_tx")
        b1 = case.ready(first)
        e1 = first.expect("APPEND")["event_id"]
        first.expect("IN_TX")
        waiter = case.spawn("none")
        b2 = case.ready(waiter)
        case.blocked(b2, b1)
        first.go()
        first.expect("COMMITTED")
        applied = first.expect("RESULT")
        first.finish()
        waiter.expect("COMMITTED")
        replayed = waiter.expect("RESULT")
        waiter.finish()
        case.backend_gone(b1)
        case.backend_gone(b2)
        assert applied["outcome"] == "applied"
        assert (replayed["outcome"], replayed["snapshot"]) == ("replayed", applied["snapshot"])
        row = check_delta(case, base, scoped(case), e1)
        assert row["result_snapshot"] == applied["snapshot"] == expected_snapshot(case)
        assert action_event_id(case) == e1
    finally:
        case.close()


def test_mr07b_child1_killed_while_waiter_blocked_then_new(fresh):
    case = _case(fresh, "resolve_note")
    try:
        base = scoped(case)
        first = case.spawn("in_tx")
        b1 = case.ready(first)
        e1 = first.expect("APPEND")["event_id"]
        x1 = first.expect("IN_TX")["xid"]
        waiter = case.spawn("none")
        b2 = case.ready(waiter)
        case.blocked(b2, b1)
        first.kill()
        e2 = waiter.expect("APPEND")["event_id"]
        waiter.expect("COMMITTED")
        result = waiter.expect("RESULT")
        waiter.finish()
        case.backend_gone(b1)
        case.backend_gone(b2)
        # Zero-rollback state proven with first-attempt-specific ids afterwards.
        assert xact_status(case, x1) == "aborted"
        event_absent(case, e1)
        assert result["outcome"] == "applied" and e2 != e1
        row = check_delta(case, base, scoped(case), e2)
        assert result["snapshot"] == row["result_snapshot"] == expected_snapshot(case)
        assert action_event_id(case) == e2
    finally:
        case.close()


# ---------------------------------------------------------------------------
# Control: the unpaused child behaves like the in-process service
# ---------------------------------------------------------------------------


def test_mr_rc_unpaused_child_applies_like_in_process(fresh):
    case = _case(fresh, "resolve_note")
    try:
        base = scoped(case)
        child = case.spawn("none")
        backend = case.ready(child)
        event_id = child.expect("APPEND")["event_id"]
        child.expect("COMMITTED")
        result = child.expect("RESULT")
        child.finish()
        case.backend_gone(backend)
        assert result["outcome"] == "applied"
        assert result["snapshot"] == expected_snapshot(case)
        row = check_delta(case, base, scoped(case), event_id)
        assert row["result_snapshot"] == result["snapshot"]
        assert action_event_id(case) == event_id
    finally:
        case.close()
