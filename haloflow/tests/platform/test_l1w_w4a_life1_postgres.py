"""L-1W W4a W4A-LIFE-1 with real pools (PostgreSQL 17; Codex 413421dd / e531b6a8).

The REAL ``TenantPool`` / ``AdmissionPool`` open on the fixture runtime login, tagged by
``application_name``. TEST SEAM (labelled): the class's ``open`` is wrapped so the real
open completes (the backend exists: positive control) and then a fault or a
cancellation is raised INSIDE the opener's acquisition window, before it returns. The
oracle is independent: no tagged backend remains in ``pg_stat_activity`` afterwards.
The test holds a strong reference to the pool object, so garbage collection cannot
close an abandoned pool and pass the oracle by accident; only the opener's explicit
close can. Any pool still open at the end is closed in ``finally`` (failure-path
cleanup only).
"""

from __future__ import annotations

import asyncio
from typing import Any

import l1w_db_support as db
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_l1w_w4a_app_units import FaultCode

pytestmark = pytest.mark.postgres

DEADLINE = 20.0


def _tagged(env: db.L1WEnv, tag: str) -> str:
    params = conninfo_to_dict(env.conninfo("runtime"))
    params.update(application_name=tag, connect_timeout="5")
    return make_conninfo("", **params)


def _count(env: db.L1WEnv, tag: str) -> int:
    row = env.one("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (tag,))
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("opener,cls", [("open_tenant_pool", "TenantPool"),
                                        ("open_admission_pool", "AdmissionPool")])
@pytest.mark.parametrize("fault", ["error", "cancel"])
async def test_life1_real_pool_fault_inside_acquisition_window_leaves_no_backend(
    env: db.L1WEnv, monkeypatch: pytest.MonkeyPatch, opener: str, cls: str, fault: str,
) -> None:
    import haloflow.m01.connections as connections
    import haloflow.m01.pool as pool_module

    tag = f"haloflow-l1w-life1-{cls.lower()}-{fault}-{env.token}"
    pool_class = getattr(pool_module, cls)
    real_open = pool_class.open
    seen: dict[str, int] = {}
    held: list[Any] = []  # strong references: no GC rescue

    async def open_then_fault(self: Any) -> None:
        held.append(self)
        await real_open(self)
        seen["during"] = await asyncio.to_thread(_count, env, tag)
        if fault == "cancel":
            raise asyncio.CancelledError()
        raise FaultCode("FAULT_IN_OPEN")

    monkeypatch.setattr(pool_class, "open", open_then_fault)
    expected: type[BaseException] = asyncio.CancelledError if fault == "cancel" else FaultCode
    try:
        async with asyncio.timeout(DEADLINE):
            with pytest.raises(expected):
                await getattr(connections, opener)(_tagged(env, tag), min_size=1)
        assert len(held) == 1
        assert seen["during"] >= 1  # positive control: the real acquisition happened
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while (count := await asyncio.to_thread(_count, env, tag)) and loop.time() < deadline:
            await asyncio.sleep(0.05)
        assert count == 0, f"{count} tagged backend(s) left open by the opener"
    finally:
        for pool in held:  # failure-path cleanup only; a closed pool closes again quietly
            await asyncio.wait_for(pool.close(), DEADLINE)
