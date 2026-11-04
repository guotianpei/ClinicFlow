"""L-1W W4a W4A-LIFE-1 regressions (Codex 413421dd / e531b6a8), unit tier.

1. Acquisition window: ``haloflow.m01.connections.open_tenant_pool`` and
   ``open_admission_pool`` own the pool from construction to return. A failure or
   cancellation inside ``open()`` closes the pool before it leaves; a close failure
   never replaces the original; a cancellation (original or during close) wins.
   PINNED SEAM: the openers construct through the module-level names ``TenantPool`` and
   ``AdmissionPool`` of ``haloflow.m01.connections``, looked up at call time.
2. Yielded lifespan: an error or cancellation thrown into the app's lifetime is
   re-raised as itself after unpublish and close, even if a close fails
   (``StartupCleanupFailed`` is only for a shutdown whose sole failure is a close);
   a cancellation during that close wins.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_l1w_w4a_app_units import FaultCode, _rig


class FakePool:
    instances: list[FakePool] = []

    def __init__(self, conninfo: str, *, min_size: int = 1, open_error: BaseException | None,
                 close_error: BaseException | None) -> None:
        self.conninfo = conninfo
        self.min_size = min_size
        self.open_error = open_error
        self.close_error = close_error
        self.opened = False
        self.closed = 0
        FakePool.instances.append(self)

    async def open(self) -> None:
        self.opened = True  # the "real acquisition" happened ...
        if self.open_error is not None:
            raise self.open_error  # ... then opening fails or is cancelled

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def _patch(monkeypatch: pytest.MonkeyPatch, name: str, open_error: BaseException | None,
           close_error: BaseException | None = None) -> None:
    import haloflow.m01.connections as connections

    FakePool.instances = []

    def factory(conninfo: str, *, min_size: int = 1) -> FakePool:
        return FakePool(conninfo, min_size=min_size, open_error=open_error,
                        close_error=close_error)

    monkeypatch.setattr(connections, name, factory)


OPENERS = [("open_tenant_pool", "TenantPool"), ("open_admission_pool", "AdmissionPool")]


@pytest.mark.parametrize("opener,cls", OPENERS)
async def test_life1_open_failure_closes_the_pool_and_reraises_the_original(
    monkeypatch: pytest.MonkeyPatch, opener: str, cls: str,
) -> None:
    import haloflow.m01.connections as connections

    _patch(monkeypatch, cls, FaultCode("OPEN_FAILED"))
    with pytest.raises(FaultCode) as caught:
        await getattr(connections, opener)("conninfo-unused")
    assert caught.value.reason_code == "OPEN_FAILED"
    (pool,) = FakePool.instances
    assert pool.opened and pool.closed == 1


@pytest.mark.parametrize("opener,cls", OPENERS)
async def test_life1_close_failure_never_replaces_the_open_failure(
    monkeypatch: pytest.MonkeyPatch, opener: str, cls: str,
) -> None:
    import haloflow.m01.connections as connections

    _patch(monkeypatch, cls, FaultCode("OPEN_FAILED"), RuntimeError("close-failed"))
    with pytest.raises(FaultCode) as caught:
        await getattr(connections, opener)("conninfo-unused")
    assert caught.value.reason_code == "OPEN_FAILED"
    assert FakePool.instances[0].closed == 1


@pytest.mark.parametrize("opener,cls", OPENERS)
async def test_life1_cancellation_while_opening_closes_and_propagates(
    monkeypatch: pytest.MonkeyPatch, opener: str, cls: str,
) -> None:
    import haloflow.m01.connections as connections

    _patch(monkeypatch, cls, asyncio.CancelledError(), RuntimeError("close-failed"))
    with pytest.raises(asyncio.CancelledError):
        await getattr(connections, opener)("conninfo-unused")
    assert FakePool.instances[0].closed == 1


@pytest.mark.parametrize("opener,cls", OPENERS)
async def test_life1_cancellation_during_the_close_wins(
    monkeypatch: pytest.MonkeyPatch, opener: str, cls: str,
) -> None:
    import haloflow.m01.connections as connections

    _patch(monkeypatch, cls, FaultCode("OPEN_FAILED"), asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await getattr(connections, opener)("conninfo-unused")
    assert FakePool.instances[0].closed == 1


@pytest.mark.parametrize("opener,cls", OPENERS)
async def test_life1_successful_open_returns_the_pool_and_its_close(
    monkeypatch: pytest.MonkeyPatch, opener: str, cls: str,
) -> None:
    import haloflow.m01.connections as connections

    _patch(monkeypatch, cls, None)
    resource, close = await getattr(connections, opener)("conninfo-x", min_size=1)
    (pool,) = FakePool.instances
    assert resource is pool and pool.closed == 0 and pool.conninfo == "conninfo-x"
    await close()
    assert pool.closed == 1


# ---------------------------------------------------------------------------
# Yielded lifespan: the in-flight error is preserved
# ---------------------------------------------------------------------------


async def _lifetime_raises(rig: Any, error: BaseException) -> Any:
    from haloflow.platform.app import create_app

    app = create_app(config=object(), steps=rig.steps)
    async with asyncio.timeout(10), app.router.lifespan_context(app):
        assert app.state.platform is not None
        raise error
    return app  # pragma: no cover


@pytest.mark.parametrize("close_error", [None, RuntimeError("close-failed")])
async def test_life1_error_in_lifetime_is_reraised_not_cleanup_failed(
    close_error: Exception | None,
) -> None:
    from haloflow.platform.app import StartupCleanupFailed

    errors = {"keyset": close_error} if close_error else {}
    rig = _rig(close_errors=errors)
    with pytest.raises(FaultCode) as caught:
        await _lifetime_raises(rig, FaultCode("BODY_FAILED"))
    assert caught.value.reason_code == "BODY_FAILED"
    assert not isinstance(caught.value, StartupCleanupFailed)
    assert all(r.closed for r in rig.resources.values())
    assert rig.journal.events[-3:] == ["close:keyset", "close:admission_pool",
                                       "close:tenant_pool"]


async def test_life1_cancellation_in_lifetime_is_reraised_despite_close_failure() -> None:
    rig = _rig(close_errors={"admission_pool": RuntimeError("close-failed")})
    with pytest.raises(asyncio.CancelledError):
        await _lifetime_raises(rig, asyncio.CancelledError())
    assert all(r.closed for r in rig.resources.values())


async def test_life1_cancellation_during_lifetime_close_wins() -> None:
    rig = _rig(close_errors={"admission_pool": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await _lifetime_raises(rig, FaultCode("BODY_FAILED"))
    assert rig.resources["tenant_pool"].closed


async def test_life1_lifetime_error_unpublishes_before_closing() -> None:
    from haloflow.platform.app import create_app

    gate = asyncio.Event()
    rig = _rig(close_gate=gate)  # the tenant-pool close waits on the gate
    app = create_app(config=object(), steps=rig.steps)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            raise FaultCode("BODY_FAILED")

    task = asyncio.ensure_future(run())
    try:
        async with asyncio.timeout(10):
            while "close:tenant_pool" not in rig.journal.events:
                await asyncio.sleep(0.01)
            during = app.state.platform
    finally:
        gate.set()
        await asyncio.wait([task], timeout=10)
    assert during is None  # unpublished before the (gated) close
    assert isinstance(task.exception(), FaultCode)
