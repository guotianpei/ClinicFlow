"""Eligibility E5 (EL-12): the SIMULATED chart store. SYNTHETIC ONLY.

The only chart E5 writes to is this simulation (no real EHR, network or patient data). It is
persisted in a separate schema `sim_chart` of the synthetic install's own database (created by
the eligibility install profile only; `SIM_CHART_SQL`), never in a tenant schema, and it is
not reachable through the M01/M05 statement gateways. A fresh `SimulatedChart` over the same
database sees the same state (R4.3, R5.5).

Governing: E5 architecture v1r1 A3 (Codex ALIGN 3671b547) with the context-bound port
clarification of the test cases v1r1 (Codex ALIGN 9e6974f2).

The production entry point is the context-bound port `SimulatedChart.for_tenant(ctx)` ->
`TenantChart`, where `ctx` is the authorized `TenantContext` of the worker or the staff
action: the tenant is always taken from that context, never from caller data.

- `issue_permit(permit_id, event_id, appointment_ref)`: binds the permit to (tenant, event,
  appointment) at issuance. Idempotent for the same id and binding. Refused: a row for the id
  with another binding or a closed (tombstoned) permit -> PERMIT_CLOSED; another OPEN permit for
  (tenant, event) -> PERMIT_CONFLICT; an entry for (tenant, event) with ANOTHER appointment ->
  REFUSED_MISMATCH (a mismatched first write is stopped before any entry exists).
- `append(permit_id, body)`: tenant / event / appointment come only from the permit row. open
  -> entry inserted, permit used -> APPENDED; an entry already exists for the binding with the
  same appointment -> EXISTS (permit used, no second row, the stored entry untouched); with
  another appointment -> REFUSED_MISMATCH; used -> ALREADY_USED (a prior append by this permit
  committed: NOT a non-write); closed or unknown -> PERMIT_CLOSED (a definitive non-write); a
  permit bound to another tenant -> TENANT_MISMATCH (no state change).
- `close_permit(permit_id)`: open -> closed (None); used stays used -> ALREADY_USED; closed ->
  None; unknown id -> a closed tombstone (None), so a delayed issue for it can never open it;
  another tenant's permit -> TENANT_MISMATCH.
- `read(event_id)`: {"appointment_ref", "permit_id", "body"} of the tenant's entry, or ABSENT.
  Another tenant's key is never visible.
No time-based permit expiry: a permit ends only by use or explicit close.

Test controls (synthetic only; per instance): `fail_next(n)` the next n appends are refused
before any write (raise `ChartRefused`); `unknown_next(n)` the next n appends commit, then the
acknowledgement is dropped (raise `ChartUnknown`); `hold_commits()` / `release_commits()` a
held append waits before its transaction (it holds no lock while waiting) and, when released,
runs normally — so a permit closed meanwhile gives PERMIT_CLOSED and nothing is written;
`reads_unavailable(n)` the next n reads fail (raise `ChartUnavailable`);
`port_timeout_override` (seconds or None) the bound the doc worker applies to one port call;
`calls` (operation, permit_id or None) in call order.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import TenantContext
from haloflow.m01.resolver import TENANT_ID_PATTERN

SCHEMA: Final = "sim_chart"

ISSUED: Final = "issued"
APPENDED: Final = "appended"
EXISTS: Final = "exists"
ALREADY_USED: Final = "already_used"
PERMIT_CLOSED: Final = "permit_closed"
PERMIT_CONFLICT: Final = "permit_conflict"
REFUSED_MISMATCH: Final = "refused_mismatch"
TENANT_MISMATCH: Final = "tenant_mismatch"
ABSENT: Final = "absent"

# Created by the eligibility install profile only (provision `profile` phase, admin
# connection). The runtime group gets exactly the DML the port needs; nothing else.
SIM_CHART_SQL: Final = """
CREATE SCHEMA sim_chart;
REVOKE ALL ON SCHEMA sim_chart FROM PUBLIC;
CREATE TABLE sim_chart.permits (
    permit_id uuid PRIMARY KEY,
    tenant text NOT NULL,
    event_id uuid,
    appointment_ref uuid,
    state varchar(8) NOT NULL CHECK (state IN ('open', 'used', 'closed')),
    issued_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    ended_at timestamptz,
    CONSTRAINT permits_binding CHECK ((event_id IS NULL) = (appointment_ref IS NULL)),
    CONSTRAINT permits_tombstone CHECK (event_id IS NOT NULL OR state = 'closed')
);
CREATE UNIQUE INDEX permits_one_open ON sim_chart.permits (tenant, event_id)
    WHERE state = 'open';
CREATE TABLE sim_chart.entries (
    tenant text NOT NULL,
    event_id uuid NOT NULL,
    appointment_ref uuid NOT NULL,
    permit_id uuid NOT NULL,
    body jsonb NOT NULL,
    committed_at timestamptz NOT NULL,
    PRIMARY KEY (tenant, event_id)
);
GRANT USAGE ON SCHEMA sim_chart TO haloflow_runtime;
GRANT SELECT, INSERT, UPDATE ON sim_chart.permits TO haloflow_runtime;
GRANT SELECT, INSERT ON sim_chart.entries TO haloflow_runtime;
"""


class ChartRefused(Exception):
    """Refused before any write (connection refused / `fail_next`): a definitive non-write."""


class ChartUnknown(Exception):
    """The outcome is unknown (the acknowledgement was lost after a possible commit)."""


class ChartUnavailable(Exception):
    """A read could not be answered (timeout / unavailable)."""


@dataclass(frozen=True, slots=True)
class ChartTenant:
    """A synthetic second tenant for the store-level isolation rows ONLY (`context_for`)."""

    tenant_id: str


def context_for(tenant_id: str) -> ChartTenant:
    """The tenant binding of a second SYNTHETIC tenant (store-level isolation rows only; the
    synthetic install has one tenant)."""
    if type(tenant_id) is not str or not TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise ValueError("TENANT_INVALID")
    return ChartTenant(tenant_id)


def _tenant_of(ctx: object) -> str:
    if isinstance(ctx, (TenantContext, ChartTenant)):
        return ctx.tenant_id
    raise TypeError("CONTEXT_INVALID")


class SimulatedChart:
    """The simulated chart store over the install database (schema `sim_chart`)."""

    def __init__(self, conninfo: str) -> None:
        if type(conninfo) is not str or not conninfo:
            raise ValueError("CONNINFO_INVALID")
        self._conninfo = conninfo
        self.calls: list[tuple[str, str | None]] = []
        self.port_timeout_override: float | None = None
        self._fail = 0
        self._unknown = 0
        self._reads_unavailable = 0
        self._release: asyncio.Event | None = None
        self._pending: set[asyncio.Task[Any]] = set()

    def __repr__(self) -> str:
        return "SimulatedChart()"

    @property
    def conninfo(self) -> str:
        return self._conninfo

    def for_tenant(self, ctx: object) -> TenantChart:
        return TenantChart(self, _tenant_of(ctx))

    # -- test controls (synthetic only) ------------------------------------------------------

    def fail_next(self, n: int) -> None:
        self._fail = int(n)

    def unknown_next(self, n: int) -> None:
        self._unknown = int(n)

    def reads_unavailable(self, n: int) -> None:
        self._reads_unavailable = int(n)

    def hold_commits(self) -> None:
        self._release = asyncio.Event()

    def release_commits(self) -> None:
        if self._release is not None:
            self._release.set()
        self._release = None

    def track(self, task: asyncio.Task[Any]) -> None:
        """Keep a port call that outlived its caller's bound (held append) until it ends."""
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def aclose(self) -> None:
        """Cancel port calls still pending (a never-released held append)."""
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=2)

    async def _connect(self) -> psycopg.AsyncConnection[Any]:
        return await psycopg.AsyncConnection.connect(self._conninfo, autocommit=False)


def _lock_key(tenant: str, event_id: str) -> str:
    return f"sim_chart:{tenant}:{event_id}"


class TenantChart:
    """The chart port bound to one authorized tenant."""

    __slots__ = ("_chart", "_tenant")

    def __init__(self, chart: SimulatedChart, tenant: str) -> None:
        self._chart = chart
        self._tenant = tenant

    def __repr__(self) -> str:
        return "TenantChart()"

    @property
    def chart(self) -> SimulatedChart:
        return self._chart

    @property
    def tenant(self) -> str:
        return self._tenant

    async def issue_permit(self, permit_id: str, event_id: str, appointment_ref: str) -> str:
        self._chart.calls.append(("issue_permit", str(permit_id)))
        conn = await self._chart._connect()
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_catalog.pg_advisory_xact_lock("
                                   "pg_catalog.hashtextextended(%s, 0))",
                                   (_lock_key(self._tenant, str(event_id)),))
                cur = await conn.execute(
                    "SELECT tenant, event_id::text, appointment_ref::text, state"
                    " FROM sim_chart.permits WHERE permit_id = %s FOR UPDATE", (permit_id,))
                row = await cur.fetchone()
                if row is not None:
                    same = (row[0], row[1], row[2]) == (self._tenant, str(event_id),
                                                        str(appointment_ref))
                    if row[3] == "closed" or not same:
                        return PERMIT_CLOSED
                cur = await conn.execute(
                    "SELECT appointment_ref::text FROM sim_chart.entries"
                    " WHERE tenant = %s AND event_id = %s", (self._tenant, event_id))
                entry = await cur.fetchone()
                if entry is not None and entry[0] != str(appointment_ref):
                    return REFUSED_MISMATCH
                if row is not None:
                    return ISSUED                         # idempotent: same id and binding
                cur = await conn.execute(
                    "SELECT 1 FROM sim_chart.permits WHERE tenant = %s AND event_id = %s"
                    " AND state = 'open'", (self._tenant, event_id))
                if await cur.fetchone() is not None:
                    return PERMIT_CONFLICT
                await conn.execute(
                    "INSERT INTO sim_chart.permits (permit_id, tenant, event_id,"
                    " appointment_ref, state, issued_at) VALUES (%s, %s, %s, %s, 'open',"
                    " pg_catalog.clock_timestamp())",
                    (permit_id, self._tenant, event_id, appointment_ref))
                return ISSUED
        finally:
            await conn.close()

    async def append(self, permit_id: str, body: Mapping[str, Any]) -> str:
        chart = self._chart
        chart.calls.append(("append", str(permit_id)))
        if chart._fail > 0:
            chart._fail -= 1
            raise ChartRefused()
        release = chart._release
        if release is not None:
            await release.wait()          # held: no transaction, no lock while waiting
        drop_ack = chart._unknown > 0
        if drop_ack:
            chart._unknown -= 1
        conn = await chart._connect()
        try:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT tenant, event_id::text, appointment_ref::text, state"
                    " FROM sim_chart.permits WHERE permit_id = %s FOR UPDATE", (permit_id,))
                row = await cur.fetchone()
                if row is None:
                    outcome = PERMIT_CLOSED
                elif row[0] != self._tenant:
                    outcome = TENANT_MISMATCH
                elif row[3] == "closed":
                    outcome = PERMIT_CLOSED
                elif row[3] == "used":
                    outcome = ALREADY_USED
                else:
                    await conn.execute("SELECT pg_catalog.pg_advisory_xact_lock("
                                       "pg_catalog.hashtextextended(%s, 0))",
                                       (_lock_key(row[0], row[1]),))
                    cur = await conn.execute(
                        "SELECT appointment_ref::text FROM sim_chart.entries"
                        " WHERE tenant = %s AND event_id = %s", (row[0], row[1]))
                    entry = await cur.fetchone()
                    if entry is not None and entry[0] != row[2]:
                        outcome = REFUSED_MISMATCH
                    else:
                        if entry is None:
                            await conn.execute(
                                "INSERT INTO sim_chart.entries (tenant, event_id,"
                                " appointment_ref, permit_id, body, committed_at) VALUES"
                                " (%s, %s, %s, %s, %s, pg_catalog.clock_timestamp())",
                                (row[0], row[1], row[2], permit_id,
                                 Jsonb(json.loads(json.dumps(dict(body))))))
                            outcome = APPENDED
                        else:
                            outcome = EXISTS
                        await conn.execute(
                            "UPDATE sim_chart.permits SET state = 'used',"
                            " ended_at = pg_catalog.clock_timestamp() WHERE permit_id = %s",
                            (permit_id,))
        finally:
            await conn.close()
        if drop_ack:
            raise ChartUnknown()
        return outcome

    async def close_permit(self, permit_id: str) -> str | None:
        self._chart.calls.append(("close_permit", str(permit_id)))
        conn = await self._chart._connect()
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO sim_chart.permits (permit_id, tenant, event_id,"
                    " appointment_ref, state, issued_at, ended_at) VALUES (%s, %s, NULL, NULL,"
                    " 'closed', pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp())"
                    " ON CONFLICT (permit_id) DO NOTHING", (permit_id, self._tenant))
                cur = await conn.execute(
                    "SELECT tenant, state FROM sim_chart.permits WHERE permit_id = %s"
                    " FOR UPDATE", (permit_id,))
                row = await cur.fetchone()
                if row is None:                      # pragma: no cover - inserted above
                    return None
                if row[0] != self._tenant:
                    return TENANT_MISMATCH
                if row[1] == "used":
                    return ALREADY_USED
                if row[1] == "open":
                    await conn.execute(
                        "UPDATE sim_chart.permits SET state = 'closed',"
                        " ended_at = pg_catalog.clock_timestamp() WHERE permit_id = %s",
                        (permit_id,))
                return None
        finally:
            await conn.close()

    async def entries_for(self, appointment_ref: str) -> list[dict[str, Any]]:
        """Read-only: the tenant's entries for one appointment, oldest first (staff view)."""
        conn = await self._chart._connect()
        try:
            cur = await conn.execute(
                "SELECT event_id::text, body,"
                " pg_catalog.to_char(pg_catalog.timezone('UTC', committed_at),"
                " 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
                " FROM sim_chart.entries WHERE tenant = %s AND appointment_ref = %s"
                " ORDER BY committed_at, event_id", (self._tenant, appointment_ref))
            rows = await cur.fetchall()
            await conn.rollback()
        finally:
            await conn.close()
        return [{"event_id": r[0], "body": r[1], "committed_at": r[2]} for r in rows]

    async def read(self, event_id: str) -> dict[str, Any] | str:
        chart = self._chart
        chart.calls.append(("read", None))
        if chart._reads_unavailable > 0:
            chart._reads_unavailable -= 1
            raise ChartUnavailable()
        conn = await chart._connect()
        try:
            cur = await conn.execute(
                "SELECT appointment_ref::text, permit_id::text, body FROM sim_chart.entries"
                " WHERE tenant = %s AND event_id = %s", (self._tenant, event_id))
            row = await cur.fetchone()
            await conn.rollback()
        finally:
            await conn.close()
        if row is None:
            return ABSENT
        return {"appointment_ref": row[0], "permit_id": row[1], "body": row[2]}
