"""L-1S package S-E (due-window scheduler) — test support.

Governing: S-E package v1 + v1r1 (Work Session 2026-11-07), Codex ALIGN for tests
20261107T125936Z (`codex_se-v1r1-review.md`). Every import of S-E code is deferred into
the row body (class-B red rule).

`FakeClock` is the injected monotonic clock and sleep: sleeping advances fake time and
yields once, so no row ever sleeps in real time. `fake_runner()` builds the documented
controlled double: a real `DueWindowRunner` subclass constructed through the real
`__init__` (valid timer principal; inert gateway/resolver/intake objects, the constructor
does no I/O) that overrides ONLY `run_once` with a scripted behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

TENANT_A: Final = "sched-tenant-a"
TENANT_B: Final = "sched-tenant-b"
TENANT_C: Final = "sched-tenant-c"
LOGGER: Final = "haloflow.m05.due_scheduler"
TARGET_S: Final = 900


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


def run_result(code: str = "COMPLETE", *, attempted: int = 0, no_reply: int = 0,
               opted_out: int = 0) -> Any:
    from haloflow.m05.due_windows import RunResult

    return RunResult(code, attempted, no_reply, opted_out, 0, 0, 0, {}, False)


Behaviour = Callable[[str, uuid.UUID], Awaitable[Any]]


def fake_runner(behaviour: Behaviour, tenants: tuple[str, ...] = (TENANT_A,)) -> Any:
    from haloflow.m01.context import Principal, PrincipalKind
    from haloflow.m05.due_windows import TIMER_CAPS, DueWindowRunner

    class FakeRunner(DueWindowRunner):
        """Controlled double: real constructor, scripted `run_once` only."""

        def __init__(self) -> None:
            super().__init__(
                gateway=object(), resolver=object(), intake=object(),  # type: ignore[arg-type]
                principal=Principal(kind=PrincipalKind.WORKLOAD,
                                    id="00000000-0000-4000-8000-00000005a004",
                                    auth_method="worker",
                                    authorized_tenant_ids=frozenset(tenants),
                                    capabilities=TIMER_CAPS),
                page_limit=10, attempt_cap=50)
            self.calls: list[tuple[str, uuid.UUID]] = []

        async def run_once(self, tenant_id: str, execution_id: uuid.UUID) -> Any:
            self.calls.append((tenant_id, execution_id))
            return await behaviour(tenant_id, execution_id)

    return FakeRunner()


def timed(clock: FakeClock, durations: dict[str, float],
          results: dict[str, Any] | None = None, *,
          log: list[tuple[str, float, float]] | None = None) -> Behaviour:
    """Each tenant's run takes `durations[tenant]` fake seconds; returns `results`."""

    async def behaviour(tenant: str, _execution: uuid.UUID) -> Any:
        start = clock.now
        clock.now += durations.get(tenant, 0.0)
        if log is not None:
            log.append((tenant, start, clock.now))
        await asyncio.sleep(0)
        value = (results or {}).get(tenant)
        if isinstance(value, BaseException):
            raise value
        return value if value is not None else run_result()

    return behaviour


def scheduler(runner: Any, tenants: tuple[str, ...], clock: FakeClock, *,
              interval_s: Any = 300, stop_timeout_s: Any = 5) -> Any:
    from haloflow.m05.due_scheduler import DueWindowScheduler

    return DueWindowScheduler(runner, tenants, interval_s=interval_s,
                              stop_timeout_s=stop_timeout_s, clock=clock, sleep=clock.sleep)


def summaries(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.name == LOGGER]


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
