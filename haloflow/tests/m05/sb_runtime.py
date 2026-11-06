"""S-B runner harness: the real M01 runtime (S-B TEST registry/catalogue, target 45), the
real `IntakeService`, and a gateway wrapper that observes and injects at named points.

The wrapper is duck-typed like `service_support.Recorder` (the services use only
`with_tenant_transaction`). Injection points:
- `fail_select_at`: the n-th DUE_KEY statement (1-based, counted per wrapper) raises.
- `park_select_at`: the n-th DUE_KEY statement parks on `select_parked`/`select_release`
  inside the open selection transaction (cancellation seam).
- `fail_close`: reminder ids whose CLOSE_KEY statement raises (before COMMIT: rollback).
- `lose_ack`: reminder ids whose close transaction COMMITS, then the wrapper raises
  (acknowledgement lost after commit).
- `park_after_body`: reminder id whose close callback, after the S-A body returned
  (body-complete marker set), parks on `parked`/`release` INSIDE the open transaction.
- `before_close`: reminder id -> sync callable run (in a thread) before that close's
  statement, inside the close transaction (e.g. deliver a reply from another connection).
`InjectedFault` is the only exception the wrapper raises.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import sa_support as sa
import sb_support as sb
import service_support as sv

LOGGER = "haloflow.m05.due_windows"


class InjectedFault(Exception):
    """The ONLY exception this harness raises."""


class _Handle:
    def __init__(self, real: Any, gate: Gate, txn: dict[str, Any]) -> None:
        self._real = real
        self._gate = gate
        self._txn = txn

    async def fetch_all(self, key: str, params: Any = None, *args: Any, **kwargs: Any) -> Any:
        gate = self._gate
        if key == sb.DUE_KEY:
            gate.selects += 1
            gate.select_commands.append(dict(params["command"].obj))
            if gate.fail_select_at == gate.selects:
                raise InjectedFault("selection failure (test)")
            if gate.park_select_at == gate.selects:
                gate.select_parked.set()
                await gate.select_release.wait()
        if key == sb.CLOSE_KEY:
            reminder = params["command"].obj["reminder_id"]
            self._txn["close"] = reminder
            gate.closes.append(reminder)
            hook = gate.before_close.pop(reminder, None)
            if hook is not None:
                await asyncio.to_thread(hook)
            if reminder in gate.fail_close:
                raise InjectedFault("close failure (test)")
        return await self._real.fetch_all(key, params, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class Gate:
    def __init__(self, real: Any) -> None:
        self.real = real
        self.selects = 0
        self.select_commands: list[dict[str, Any]] = []
        self.closes: list[str] = []
        self.fail_select_at: int | None = None
        self.park_select_at: int | None = None
        self.select_parked = asyncio.Event()
        self.select_release = asyncio.Event()
        self.fail_close: set[str] = set()
        self.lose_ack: set[str] = set()
        self.park_after_body: str | None = None
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self.before_close: dict[str, Any] = {}
        self.contexts: list[Any] = []

    async def with_tenant_transaction(self, context: Any, callback: Any, **kwargs: Any) -> Any:
        self.contexts.append(context)
        txn: dict[str, Any] = {}

        async def wrapped(handle: Any) -> Any:
            result = await callback(_Handle(handle, self, txn))
            if txn.get("close") is not None and txn["close"] == self.park_after_body:
                self.parked.set()
                await self.release.wait()
            return result

        result = await self.real.with_tenant_transaction(context, wrapped, **kwargs)
        if txn.get("close") in self.lose_ack:
            raise InjectedFault("acknowledgement lost after commit (test)")
        return result


class FlakyResolver:
    """Wraps the real resolver; `fail_close_resolve` > 0 makes that many close-context
    resolutions raise InjectedFault; `park_select` parks the next selection resolve."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.fail_close_resolve = 0
        self.park_select = False
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self.requested: list[frozenset[str]] = []

    async def resolve(self, **kwargs: Any) -> Any:
        caps = frozenset(kwargs["capabilities"])
        self.requested.append(caps)
        if caps == sb.CLOSE_CAPS and self.fail_close_resolve > 0:
            self.fail_close_resolve -= 1
            raise InjectedFault("resolver unavailable (test)")
        if caps == sb.SELECT_CAPS and self.park_select:
            self.park_select = False
            self.parked.set()
            await self.release.wait()
        return await self.real.resolve(**kwargs)


@asynccontextmanager
async def runtime(install: sb.SBInstall, *, max_size: int = 3) -> AsyncIterator[Any]:
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=max_size)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=sb.sb_registry(),
            catalog=sb.sb_catalog(),
            supported_schema_versions=frozenset({sb.SB_TARGET}),
        )
    finally:
        await pool.close()


def principal(install: sb.SBInstall) -> Any:
    from haloflow.m01.context import Principal, PrincipalKind

    return Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_TIMER, auth_method="worker",
                     authorized_tenant_ids=frozenset({install.tenant_id}),
                     capabilities=sb.TIMER_CAPS)


def runner(install: sb.SBInstall, rt: Any, *, page: int, cap: int,
           gate: Gate | None = None, resolver: Any = None) -> tuple[Any, Gate]:
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.due_windows import DueWindowRunner
    from haloflow.m05.intake import IntakeService

    gate = gate or Gate(rt.gateway)
    intake = IntakeService(EventFoundation(gate, producer_service=sa.PRODUCER))
    return DueWindowRunner(gateway=gate, resolver=resolver or rt.resolver, intake=intake,
                           principal=principal(install), page_limit=page,
                           attempt_cap=cap), gate


async def run(r: Any, install: sb.SBInstall) -> Any:
    return await r.run_once(install.tenant_id, uuid.uuid4())


def outcome_sum(result: Any) -> int:
    return (result.no_reply + result.opted_out + result.not_awaiting + result.not_due
            + result.commit_unknown + sum(result.failed.values()))


def run_lines(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.name == LOGGER
            and r.getMessage().startswith("due_windows run ")]


def ordered_due(install: sb.SBInstall, reminders: list[str]) -> list[str]:
    """The given reminders in (deadline, reminder_id) order."""

    order = [r for _, r in sb.awaiting_due(install)]
    return sorted(reminders, key=order.index)
