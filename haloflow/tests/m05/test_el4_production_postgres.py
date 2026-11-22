"""Eligibility E4-P02b: production startup refuses a tenant installed with the eligibility
registry (target 126). CONTROL (B1-C3 pattern).

Governing: `claude_e4-test-packet-draft-v1.md` E4-P02b. The tenant is installed with the
EXISTING E3b harness (`el3b_support.EL3BInstall`: the synthetic registry with
include_el1..include_el3b, target 126 - the same registry the E4 eligibility profile
installs). Expected to pass before and after E4 (no production file changes); a failure is a
production-guard finding, not a reason to change `startup.py` inside E4.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import el3b_support as e3b
import m02_support
import pytest
from test_b1_synthetic_startup_postgres import (
    EXPECTED_REASON,
    Spies,
    _dependencies,
    _installed_state,
)

pytestmark = pytest.mark.postgres

ELIGIBILITY_CLASSES = (
    ("haloflow.m05.eligibility_scheduler", "EligibilityScheduler"),
    ("haloflow.m05.simulated_clearinghouse", "SimulatedClearinghouse"),
    ("haloflow.m05.due_scheduler", "DueWindowScheduler"),
    ("haloflow.m05.reminder_scheduler", "ReminderScheduler"),
    ("haloflow.m05.writeback_scheduler", "WritebackScheduler"),
    ("haloflow.m05.outbound", "FakeSender"),
    ("haloflow.m05.simulated_ehr", "SimulatedEhr"),
)


@pytest.fixture
def el_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Callable[[tuple[str, str]], None]) -> Iterator[e3b.EL3BInstall]:
    yield from e3b.module_install(request, m02_ids, m05_reset, f"el4p02b:{request.node.name}")


@pytest.mark.parametrize("deployment", ["PRODUCTION", "NON_PRODUCTION"])
async def test_el4_p02b_production_startup_refuses_eligibility_tenant(
        el_install: e3b.EL3BInstall, monkeypatch: pytest.MonkeyPatch, deployment: str) -> None:
    """C. Refused with the B1-C3 reason; nothing production or synthetic (eligibility
    scheduler, simulated clearinghouse, B-series producers) constructed; the tenant's
    installed state unchanged."""
    import asyncio

    from haloflow.startup import Deployment, StartupRefused, start_production_services

    await asyncio.to_thread(el_install.require)
    before = _installed_state(el_install)
    ((version, _row),) = before[0]
    assert version == e3b.EL3B_TARGET == 126
    spies = Spies(monkeypatch, ELIGIBILITY_CLASSES)

    async with _dependencies(el_install) as dependencies:
        with pytest.raises(StartupRefused) as refused:
            await start_production_services(dependencies, deployment=Deployment[deployment],
                                            served_tenants=[el_install.tenant_id])

    assert refused.value.reason_code == EXPECTED_REASON
    assert refused.value.tenant_index == 0
    spies.assert_none()
    assert _installed_state(el_install) == before
