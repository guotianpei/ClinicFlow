"""B5 restart child: one M05 staff action in its own OS process (test-only).

Run by tests/m05/test_b5_restart_postgres.py as

    <python> -I tests/m05/restart_child.py        (cwd = haloflow/)

`-I` (isolated mode) is used deliberately: PYTHONPATH, PYTHON* variables, the user
site and the script directory are ignored, so the child imports exactly the venv's
packages (haloflow is an editable install in site-packages) plus the two test
support directories this file inserts itself (tests/m05, then tests/m02, the same
paths tests/m05/conftest.py and pytest's prepend mode provide in-process).

Protocol. The first stdin line is one JSON object:
    logins   {"haloflow_runtime", "haloflow_migrator", "haloflow_provisioner": conninfo}
    tenant   [tenant_id, schema_key]
    item_id, client_action_id, expected_version
    action   "claim" | "resolve_note" | "resolve_plain"
    note     the synthetic note text (resolve_note only)
    pause    "none" | "in_tx" | "after_commit"
    tag      application_name for every connection this process opens
Credentials travel on stdin, never argv. Markers on stdout, one per line, flushed:
    READY pid=<os pid> backend=<pg_backend_pid of the action transaction's connection>
    APPEND event_id=<uuid>       when the resolution append returns (resolve NEW only)
    IN_TX xid=<xid8>             (pause in_tx) inside the open transaction after the
                                 service's statements (prepare/append/apply) returned
                                 and before COMMIT; then blocks for "go" on stdin
    COMMITTED                    after the gateway transaction committed, before the
                                 service returns; (pause after_commit) then blocks for "go"
    RESULT outcome=<o> snapshot=<compact sorted JSON>     then exit 0
Failures print one `ERROR type=<class> code=<code>` line and exit non-zero; a
pause released by anything but "go" exits 3 without committing (in_tx: the
SystemExit unwinds the gateway transaction, which rolls back).

How the evidence is obtained (no src change; test seams in THIS process only):
- backend pid: a TenantPool subclass (the `_faulting_pool` pattern in
  test_m05_service_postgres.py) records `connection.info.backend_pid` of the
  connection `_connection_for_gateway` hands to the gateway; READY is printed on the
  first acquisition, before any statement of the action transaction. With
  min_size = max_size = 1 every later acquisition must be the same backend (checked).
- xid / pause: a duck-typed gateway wrapper (the service_support.Recorder pattern)
  wraps the callback passed to `with_tenant_transaction`; after the service's callback
  returns it runs `SELECT pg_backend_pid(), pg_current_xact_id()` directly on that
  recorded psycopg connection (inside the same transaction; the catalogued handle is
  not used for this probe), checks the pid equals READY's, prints IN_TX and waits.
- APPEND: an EventFoundation subclass whose `append_observation_in` prints the
  returned AppendResult.event_id (the binding owner check still holds: the binding
  is issued by this same instance).
- application_name: `make_conninfo(conninfo, application_name=tag)` for the pool and
  the migrator/provisioner factories. NOTE the M01 gateway sets application_name
  transaction-locally to `haloflow:<execution_id>`, so while a gateway transaction is
  open pg_stat_activity shows that value, not the tag; the parent therefore tracks
  backends by pid and uses the tag only for idle/session-level residue.

Guard: refuses (exit 64) unless every conninfo's dbname and the server's
current_database() start with "haloflow_test".
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent / "m02")]

import m02_support  # noqa: E402
import m05_support as m05  # noqa: E402
import resolve_support as rs  # noqa: E402
import service_support as sv  # noqa: E402
import staff_support as st  # noqa: E402

DB_PREFIX = "haloflow_test"
ROLES = ("haloflow_runtime", "haloflow_migrator", "haloflow_provisioner")
ACTIONS = ("claim", "resolve_note", "resolve_plain")
PAUSES = ("none", "in_tx", "after_commit")


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class _Protocol(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _State:
    def __init__(self, pause: str) -> None:
        self.pause = pause
        self.backend: int | None = None
        self.connection: Any = None
        self.transactions = 0


async def _wait_go() -> None:
    line = await asyncio.to_thread(sys.stdin.readline)
    if line.strip() != "go":
        emit("ABORT released-without-go")
        raise SystemExit(3)


def _pool_class(state: _State) -> Any:
    from haloflow.m01.pool import TenantPool

    class _Pool(TenantPool):
        @asynccontextmanager
        async def _connection_for_gateway(self) -> AsyncIterator[Any]:
            async with super()._connection_for_gateway() as connection:
                pid = int(connection.info.backend_pid)
                if state.backend is None:
                    state.backend = pid
                    emit(f"READY pid={os.getpid()} backend={pid}")
                elif pid != state.backend:
                    raise _Protocol("BACKEND_CHANGED")
                state.connection = connection
                try:
                    yield connection
                finally:
                    state.connection = None

    return _Pool


class _Gateway:
    """Duck-typed gateway: the services and EventFoundation call only
    `with_tenant_transaction`."""

    def __init__(self, gateway: Any, state: _State) -> None:
        self._gateway = gateway
        self._state = state

    async def with_tenant_transaction(self, context: Any, callback: Any, **kwargs: Any) -> Any:
        state = self._state
        state.transactions += 1

        async def wrapped(handle: Any) -> Any:
            result = await callback(handle)
            if state.pause == "in_tx":
                connection = state.connection
                if connection is None:
                    raise _Protocol("NO_CONNECTION")
                cursor = await connection.execute(
                    "SELECT pg_catalog.pg_backend_pid(), pg_catalog.pg_current_xact_id()::text")
                row = await cursor.fetchone()
                if row is None or int(row[0]) != state.backend:
                    raise _Protocol("PROBE_BACKEND_MISMATCH")
                emit(f"IN_TX xid={row[1]}")
                await _wait_go()
            return result

        result = await self._gateway.with_tenant_transaction(context, wrapped, **kwargs)
        emit("COMMITTED")
        if state.pause == "after_commit":
            await _wait_go()
        return result


def _foundation_class() -> Any:
    from haloflow.m02.observation import EventFoundation

    class _CapturingFoundation(EventFoundation):
        async def append_observation_in(self, binding: Any, observation: Any) -> Any:
            appended = await super().append_observation_in(binding, observation)
            emit(f"APPEND event_id={appended.event_id}")
            return appended

    return _CapturingFoundation


def _guard(logins: dict[str, str], tag: str) -> None:
    for role in ROLES:
        dbname = conninfo_to_dict(logins[role]).get("dbname")
        if not isinstance(dbname, str) or not dbname.startswith(DB_PREFIX):
            emit("REFUSED database-name")
            raise SystemExit(64)
    with psycopg.connect(logins["haloflow_runtime"], application_name=tag,
                         autocommit=True) as conn:
        row = conn.execute("SELECT pg_catalog.current_database()").fetchone()
    if row is None or not str(row[0]).startswith(DB_PREFIX):
        emit("REFUSED database-name")
        raise SystemExit(64)


def _action(spec: dict[str, Any]) -> tuple[Any, frozenset[str]]:
    item, key = UUID(spec["item_id"]), UUID(spec["client_action_id"])
    version = int(spec["expected_version"])
    if spec["action"] == "claim":
        action = rs.svc().StaffAction(item_id=item, client_action_id=key,
                                      expected_version=version, type="claim",
                                      assignee_id=None)
        return action, sv.STAFF_CAPS
    note = spec["note"] if spec["action"] == "resolve_note" else None
    return rs.resolve_action(item, version, note=note, key=key), sv.RESOLVE_CAPS


async def _run(spec: dict[str, Any]) -> None:
    state = _State(spec["pause"])
    tag = spec["tag"]
    tagged = {role: make_conninfo(spec["logins"][role], application_name=tag) for role in ROLES}
    install = st.StaffInstall(m02_support.Identities(admin="", logins=tagged),
                              (spec["tenant"][0], spec["tenant"][1]))
    action, caps = _action(spec)
    async with sv.runtime(install, pool_class=_pool_class(state),
                          login=tagged["haloflow_runtime"]) as rt:
        ctx = await sv.context(rt, install, m05.S1, caps)
        gateway = _Gateway(rt.gateway, state)
        foundation = _foundation_class()(gateway, producer_service=sv.PRODUCER)
        service = rs.svc().M05Service(gateway, foundation=foundation, keys=sv.TestNoteKeys())
        result = await service.act(ctx, action)
        if state.transactions != 1:
            raise _Protocol("TRANSACTION_COUNT")
        snapshot = json.dumps(dict(result.snapshot), sort_keys=True, separators=(",", ":"),
                              default=str)
        emit(f"RESULT outcome={result.outcome} snapshot={snapshot}")


def main() -> int:
    spec = json.loads(sys.stdin.readline())
    if spec.get("action") not in ACTIONS or spec.get("pause") not in PAUSES:
        emit("ERROR type=Spec code=SPEC_INVALID")
        return 2
    _guard(spec["logins"], spec["tag"])
    try:
        asyncio.run(_run(spec))
    except Exception as error:  # noqa: BLE001 - reported to the parent as one marker
        code = getattr(error, "code", None) or getattr(error, "reason_code", None)
        emit(f"ERROR type={type(error).__name__} code={code}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
