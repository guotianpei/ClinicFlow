"""CP2-2a fixtures (architecture v4 §6.3).

This file holds M02 fixtures only. The session database fixtures
(`migrated_database`, `role_logins`, `reset_tenants`, ...) come from
`tests/conftest.py`, so both packages share one database setup per session. The
M02 harness is built from those fixtures and from the production composition.
It does not import M01's `ProvisioningHarness` from its test module.

`m02_support` is imported here and nowhere else. Test modules reach it through
the `m02` fixture, which follows the M01 convention for `recording`.
"""

from collections.abc import Callable, Iterator, Sequence
from types import ModuleType

import m02_support
import pytest


@pytest.fixture
def m02() -> ModuleType:
    return m02_support


@pytest.fixture(scope="session")
def m02_ids(migrated_database: str, role_logins: dict[str, str]) -> m02_support.Identities:
    """ADMIN plus the MIG/RT login shims; setup only, never a measured superuser."""

    return m02_support.Identities(admin=migrated_database, logins=role_logins)


@pytest.fixture(scope="session")
def m02_reset(
    reset_tenants: Callable[[str, Sequence[str], Sequence[str]], None],
    m02_ids: m02_support.Identities,
) -> Callable[[tuple[str, str]], None]:
    def _reset(tenant: tuple[str, str]) -> None:
        reset_tenants(m02_ids.admin, [tenant[0]], [tenant[1]])

    return _reset


@pytest.fixture
def m02_tenant(
    request: pytest.FixtureRequest, m02_reset: Callable[[tuple[str, str]], None]
) -> Iterator[tuple[str, str]]:
    """A synthetic tenant unique to one test, removed before and after."""

    tenant = m02_support.tenant_for(request.node.nodeid)
    m02_reset(tenant)
    yield tenant
    m02_reset(tenant)


@pytest.fixture(scope="module")
def production_tenant(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m02_reset: Callable[[tuple[str, str]], None],
) -> Iterator[tuple[str, str]]:
    """One tenant per module, provisioned through `build_production_tenant_migrations()`."""

    tenant = m02_support.tenant_for(f"{request.module.__name__}:production")
    m02_reset(tenant)
    try:
        # No feature assertion here (Codex P1): rows assert installation and
        # version in their own bodies, so the baseline is behaviour-red, not ERROR.
        m02_support.provision_sync(m02_ids, m02_support.production_registry(), tenant)
        yield tenant
    finally:
        # Codex P2: cleanup runs even when provisioning fails before `yield`.
        m02_reset(tenant)


@pytest.fixture(scope="module")
def m01_only_tenant(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m02_reset: Callable[[tuple[str, str]], None],
) -> Iterator[tuple[str, str]]:
    """A tenant at `t001` only: the migrator has CREATE, and no `t002` object exists."""

    tenant = m02_support.tenant_for(f"{request.module.__name__}:m01-only")
    m02_reset(tenant)
    try:
        m02_support.provision_sync(
            m02_ids, m02_support.m01_only_registry(), tenant, supported=range(1, 2)
        )
        yield tenant
    finally:
        m02_reset(tenant)
