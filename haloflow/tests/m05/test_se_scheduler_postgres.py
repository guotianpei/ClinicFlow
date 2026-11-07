"""L-1S package S-E — DueWindowScheduler eligibility/closure rows (PostgreSQL).

Governing: S-E package v1 + v1r1 C1 (Codex ALIGN for tests 20261107T125936Z). The
database decides "due" with clock_timestamp(); these rows move stored deadlines with the
existing S-B fixture `sb.set_deadline` and drive MANUAL ticks. They are eligibility and
closure evidence only; nothing here measures elapsed latency (that is SE-U08, fake
clock). S-C TEST runtime (target 46), ENFORCE, real DueWindowRunner and IntakeService;
the S-A side-effect guard is active; synthetic only.

- SE-P01 future deadline -> tick closes nothing (zero deltas); past deadline -> next tick
         closes it `no_reply` with one item (S-D NO_REPLY deltas).
- SE-P02 two tenants in one scheduler, one revoked: SELECTION_FAILED there with zero
         deltas; the granted tenant's due window is closed in the same tick.
- SE-P03 a repeated tick attempts nothing and writes nothing.
- NC-E1 only the future-deadline tick ran -> the closure oracle fails.
- NC-E2 tenant revoked before the due tick -> the closure oracle fails.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import m02_support
import pytest
import sa_support as sa
import sb_support as sb
import sc_support as sc
import sd_support as sd

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


@pytest.fixture(scope="module")
def se_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[sc.SCInstall]:
    yield from sc.module_install(request, m02_ids, m05_reset, "se")


@pytest.fixture(scope="module")
def se_other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
             m05_reset: Any) -> Iterator[sc.SCInstall]:
    yield from sc.module_install(request, m02_ids, m05_reset, "se:other")


def _runner(rt: Any, tenants: tuple[str, ...]) -> Any:
    from haloflow.m01.context import Principal, PrincipalKind
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.due_windows import TIMER_CAPS, DueWindowRunner
    from haloflow.m05.intake import IntakeService

    return DueWindowRunner(
        gateway=rt.gateway, resolver=rt.resolver,
        intake=IntakeService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER)),
        principal=Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_TIMER, auth_method="worker",
                            authorized_tenant_ids=frozenset(tenants),
                            capabilities=TIMER_CAPS),
        page_limit=10, attempt_cap=50)


def _scheduler(runner: Any, tenants: tuple[str, ...]) -> Any:
    from haloflow.m05.due_scheduler import DueWindowScheduler

    return DueWindowScheduler(runner, tenants, interval_s=300, stop_timeout_s=10)


def _deadline(install: sc.SCInstall, reminder: str, offset: timedelta) -> None:
    sb.set_deadline(install, reminder, datetime.now(UTC) + offset)  # type: ignore[arg-type]


def _closed(install: sc.SCInstall, reminder: str, before: Counter[str]) -> None:
    """The closure oracle (SE-P01; NC-E1/NC-E2 drive it to fail)."""

    window = sa.window(install, reminder)
    assert (window["state"], window["closed_decision"]) == ("no_reply", "no_reply"), window
    assert sd.delta(before, sd.counts(install)) == sd.NO_REPLY


async def _due_reminder(install: sc.SCInstall, rt: Any) -> str:
    _, _, (ref,) = sa.fresh_patient(install)
    dispatched = await sd.Chain(install, rt).dispatch(ref)
    return str(dispatched.reminder_id)


def test_se_p01_p03_eligibility_closure_and_repeat(se_install: sc.SCInstall) -> None:
    sd.begin(se_install)

    async def body() -> None:
        async with sd.runtime(se_install) as rt:
            reminder = await _due_reminder(se_install, rt)
            tenants = (se_install.tenant_id,)
            scheduler = _scheduler(_runner(rt, tenants), tenants)
            _deadline(se_install, reminder, timedelta(hours=1))
            before = sd.counts(se_install)
            first = await scheduler.tick()
            assert (dict(first.codes), first.attempted) == ({"COMPLETE": 1}, 0)
            sd.assert_no_change(sd.delta(before, sd.counts(se_install)))
            assert sa.window(se_install, reminder)["state"] == "awaiting"
            _deadline(se_install, reminder, -timedelta(seconds=1))
            second = await scheduler.tick()
            assert (dict(second.codes), second.attempted, second.no_reply) == (
                {"COMPLETE": 1}, 1, 1)
            _closed(se_install, reminder, before)
            mark = sd.counts(se_install)
            third = await scheduler.tick()  # SE-P03
            assert (dict(third.codes), third.attempted) == ({"COMPLETE": 1}, 0)
            sd.assert_no_change(sd.delta(mark, sd.counts(se_install)))
            await scheduler.stop()

    asyncio.run(body())


def test_se_p02_two_tenants_one_revoked(se_install: sc.SCInstall,
                                         se_other: sc.SCInstall) -> None:
    sd.begin(se_other)
    sd.begin(se_install)

    async def body() -> None:
        async with sd.runtime(se_install) as rt:
            granted = await _due_reminder(se_install, rt)
            revoked = await _due_reminder(se_other, rt)
            for install, reminder in ((se_install, granted), (se_other, revoked)):
                _deadline(install, reminder, -timedelta(seconds=1))
            sc.change(se_other, "revoke")
            tenants = (se_install.tenant_id, se_other.tenant_id)
            scheduler = _scheduler(_runner(rt, tenants), tenants)
            before_a, before_b = sd.counts(se_install), sd.counts(se_other)
            result = await scheduler.tick()
            assert dict(result.codes) == {"COMPLETE": 1, "SELECTION_FAILED": 1}
            assert (result.attempted, result.no_reply) == (1, 1)
            _closed(se_install, granted, before_a)
            sd.assert_no_change(sd.delta(before_b, sd.counts(se_other)))
            assert sa.window(se_other, revoked)["state"] == "awaiting"
            await scheduler.stop()

    asyncio.run(body())


def test_se_nc1_future_deadline_only_fails_the_closure_oracle(se_install: sc.SCInstall) -> None:
    sd.begin(se_install)

    async def body() -> None:
        async with sd.runtime(se_install) as rt:
            reminder = await _due_reminder(se_install, rt)
            tenants = (se_install.tenant_id,)
            scheduler = _scheduler(_runner(rt, tenants), tenants)
            _deadline(se_install, reminder, timedelta(hours=1))
            before = sd.counts(se_install)
            await scheduler.tick()
            with pytest.raises(AssertionError):
                _closed(se_install, reminder, before)
            await scheduler.stop()

    asyncio.run(body())


def test_se_nc2_revoked_before_due_tick_fails_the_closure_oracle(
        se_install: sc.SCInstall) -> None:
    sd.begin(se_install)

    async def body() -> None:
        async with sd.runtime(se_install) as rt:
            reminder = await _due_reminder(se_install, rt)
            tenants = (se_install.tenant_id,)
            scheduler = _scheduler(_runner(rt, tenants), tenants)
            _deadline(se_install, reminder, -timedelta(seconds=1))
            sc.change(se_install, "revoke")
            before = sd.counts(se_install)
            result = await scheduler.tick()
            assert dict(result.codes) == {"SELECTION_FAILED": 1}
            with pytest.raises(AssertionError):
                _closed(se_install, reminder, before)
            sd.assert_no_change(sd.delta(before, sd.counts(se_install)))
            await scheduler.stop()

    asyncio.run(body())

