"""Option B B1-C3: production startup refuses a synthetic (S-I TEST, target 94) tenant.

Governing: `claude_option-b-package-v1-2026-11-11.md` B-C3 and the Codex review pin
(20261111T132851Z): the gate must not depend on the future B2 provisioning command, so the
tenant is installed with the EXISTING S-I test fixture (`si_support.module_install` +
`ready_install`: registry 94, the TEST profile, activation granted). It then proves,
independently of the refusal code:
- `start_production_services` raises `StartupRefused` with the pinned reason for BOTH
  deployments (PRODUCTION and NON_PRODUCTION);
- nothing was constructed: no production tenant runtime, no M05Service / StaffService /
  EventFoundation, and none of the synthetic producers or ports (the three schedulers,
  FakeSender, SimulatedEhr);
- the synthetic tenant's installed state is unchanged by the refused startup (schema
  version and ledger row count).

This row is a CONTROL: it is expected to pass before and after B1 (no production file
changes). If it ever fails, that is a production-guard finding to report, not a reason to
change `startup.py` inside B1.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, Final

import m02_support
import pytest
import si_support as si

pytestmark = pytest.mark.postgres

# Measured on the red candidate (see the B1 evidence note); pinned so a change in WHICH
# guard fires is visible in review.
EXPECTED_REASON: Final = "SCHEMA_VERSION_UNSUPPORTED"

SYNTHETIC_CLASSES: Final = (
    ("haloflow.m05.due_scheduler", "DueWindowScheduler"),
    ("haloflow.m05.reminder_scheduler", "ReminderScheduler"),
    ("haloflow.m05.writeback_scheduler", "WritebackScheduler"),
    ("haloflow.m05.outbound", "FakeSender"),
    ("haloflow.m05.simulated_ehr", "SimulatedEhr"),
)
PRODUCTION_NAMES: Final = ("M05Service", "StaffService", "EventFoundation")


@pytest.fixture
def si_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Callable[[tuple[str, str]], None]) -> Iterator[si.SIInstall]:
    yield from si.module_install(request, m02_ids, m05_reset, f"b1c3:{request.node.name}")


class Spies:
    """Counts constructions without changing behaviour."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        import haloflow.startup as startup
        from haloflow import composition

        self.calls: dict[str, int] = {}

        def counting(name: str, real: Any) -> Any:
            self.calls[name] = 0

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                self.calls[name] += 1
                return real(*args, **kwargs)

            return wrapper

        monkeypatch.setattr(composition, "build_production_tenant_runtime",
                            counting("build_production_tenant_runtime",
                                     composition.build_production_tenant_runtime))
        for name in PRODUCTION_NAMES:
            monkeypatch.setattr(startup, name, counting(name, getattr(startup, name)))
        for module_name, class_name in SYNTHETIC_CLASSES:
            cls = getattr(importlib.import_module(module_name), class_name)
            real_init = cls.__init__
            key = class_name
            self.calls[key] = 0

            def init(obj: Any, *args: Any, _real: Any = real_init, _key: str = key,
                     **kwargs: Any) -> None:
                self.calls[_key] += 1
                _real(obj, *args, **kwargs)

            monkeypatch.setattr(cls, "__init__", init)

    def assert_none(self) -> None:
        assert self.calls and all(count == 0 for count in self.calls.values()), self.calls


@asynccontextmanager
async def _dependencies(install: si.SIInstall) -> AsyncIterator[Any]:
    from psycopg import AsyncConnection

    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies

    logins = install.ids.logins
    pool = TenantPool(logins["haloflow_runtime"], min_size=1, max_size=2)
    await pool.open()

    def factory(conninfo: str) -> Any:
        async def connect() -> AsyncConnection[Any]:
            return await AsyncConnection.connect(conninfo, autocommit=True)

        return connect

    try:
        yield TenantRuntimeDependencies(
            pool=pool, control_store=PsycopgControlStore(pool),
            migrator_connect=factory(logins["haloflow_migrator"]),
            provisioner_connect=factory(logins["haloflow_provisioner"]))
    finally:
        await pool.close()


def _installed_state(install: si.SIInstall) -> tuple[Any, ...]:
    """The tenant row and its full migration ledger, as JSON text (exact before/after)."""

    tenant = m02_support.admin_all(
        install.ids, "SELECT schema_version, to_jsonb(t)::text FROM shared.tenants t"
        " WHERE t.tenant_id = %s", (install.tenant_id,))
    ledger = m02_support.admin_all(
        install.ids, "SELECT to_jsonb(m)::text FROM shared.schema_migrations m"
        " WHERE m.tenant_id = %s ORDER BY m.migration_id", (install.tenant_id,))
    return (tuple(tenant), tuple(ledger))


@pytest.mark.parametrize("deployment", ["PRODUCTION", "NON_PRODUCTION"])
async def test_b1_c3_production_startup_refuses_a_synthetic_tenant(
        si_install: si.SIInstall, monkeypatch: pytest.MonkeyPatch, deployment: str) -> None:
    from haloflow.startup import Deployment, StartupRefused, start_production_services

    install = await asyncio.to_thread(si.ready_install, si_install)
    before = _installed_state(install)
    ((version, _row),) = before[0]
    assert version == si.SI_TARGET
    assert len(before[1]) == si.SI_TARGET
    spies = Spies(monkeypatch)

    async with _dependencies(install) as dependencies:
        with pytest.raises(StartupRefused) as refused:
            await start_production_services(dependencies, deployment=Deployment[deployment],
                                            served_tenants=[install.tenant_id])

    assert refused.value.reason_code == EXPECTED_REASON
    assert refused.value.tenant_index == 0
    spies.assert_none()
    assert _installed_state(install) == before


def test_b1_c3s_spies_count_real_constructions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Self-test: the spies above really observe a construction (so `assert_none` in
    B1-C3 is not vacuous), and they leave the real behaviour in place."""

    from datetime import UTC, datetime

    import haloflow.startup as startup
    from haloflow import composition
    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.simulated_ehr import SimulatedEhr

    spies = Spies(monkeypatch)
    FakeSender()
    SimulatedEhr(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    assert spies.calls["FakeSender"] == 1
    assert spies.calls["SimulatedEhr"] == 1
    with pytest.raises(AssertionError):
        spies.assert_none()
    assert startup.M05Service is not None and composition.build_production_tenant_runtime
