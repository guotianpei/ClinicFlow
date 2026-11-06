"""UC1 part B (M05) fixtures.

The session database fixtures come from tests/conftest.py. M05 reuses the M02
harness (`m02_support`: identities, provisioning, tenant reset) by putting
tests/m02 on the import path; nothing in tests/m02 is modified.

B1..B4 installed M05 through a TEST registry (part A units + the M05 units) and a
TEST provisioning manifest (production + the M05 owner at USAGE+CREATE) until every
M05 route existed (Codex 20261031T190524Z). B5 composes M05 and the staff directory
into production (target 25, both owners at USAGE+CREATE); the isolated target-20
M05 registry stays as an explicit test fixture (harness migration H2).

Installation is deferred into the test body (`M05Install.require()`), never fixture
setup, so a missing M05 implementation makes each row FAIL in its own body rather
than ERROR in setup (the class-B red rule from part A, Codex packet-v1 review P1).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "m02"))

import m02_support  # noqa: E402
import m05_support  # noqa: E402
import sa_support  # noqa: E402
from m05_harness import M05Install  # noqa: E402
from staff_support import StaffInstall  # noqa: E402


@pytest.fixture
def m05() -> ModuleType:
    return m05_support


@pytest.fixture
def m02() -> ModuleType:
    return m02_support


@pytest.fixture(scope="session")
def m02_ids(migrated_database: str, role_logins: dict[str, str]) -> m02_support.Identities:
    return m02_support.Identities(admin=migrated_database, logins=role_logins)


@pytest.fixture(scope="session")
def m05_reset(
    reset_tenants: Callable[[str, Sequence[str], Sequence[str]], None],
    m02_ids: m02_support.Identities,
) -> Callable[[tuple[str, str]], None]:
    def _reset(tenant: tuple[str, str]) -> None:
        reset_tenants(m02_ids.admin, [tenant[0]], [tenant[1]])

    return _reset


@pytest.fixture(scope="module")
def m05_install(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m05_reset: Callable[[tuple[str, str]], None],
) -> Iterator[M05Install]:
    """One synthetic tenant per module, removed before and after; installed lazily."""

    tenant = m02_support.tenant_for(f"{request.module.__name__}:m05")
    m05_reset(tenant)
    try:
        yield M05Install(m02_ids, tenant)
    finally:
        m05_reset(tenant)


@pytest.fixture(scope="module")
def staff_install(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m05_reset: Callable[[tuple[str, str]], None],
) -> Iterator[StaffInstall]:
    """MA-10: one synthetic tenant per module, installed through the staff test
    registry (target 25 since B3b); removed before and after; installed lazily."""

    tenant = m02_support.tenant_for(f"{request.module.__name__}:staff")
    m05_reset(tenant)
    try:
        yield StaffInstall(m02_ids, tenant)
    finally:
        m05_reset(tenant)


@pytest.fixture(scope="module")
def staff_foreign_install(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m05_reset: Callable[[tuple[str, str]], None],
) -> Iterator[StaffInstall]:
    """A second clinic, for other-tenant ids (DR-01, MA-10d)."""

    tenant = m02_support.tenant_for(f"{request.module.__name__}:staff:foreign")
    m05_reset(tenant)
    try:
        yield StaffInstall(m02_ids, tenant)
    finally:
        m05_reset(tenant)


# L-1S package S-A: one tenant per module, installed through the S-A TEST registry
# (target 42) lazily in the row body; a second tenant for cross-tenant rows.
@pytest.fixture(scope="module")
def sa_install(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m05_reset: Callable[[tuple[str, str]], None],
) -> Iterator[sa_support.SAInstall]:
    yield from sa_support.module_install(request, m02_ids, m05_reset, "sa")


@pytest.fixture(scope="module")
def sa_other_install(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m05_reset: Callable[[tuple[str, str]], None],
) -> Iterator[sa_support.SAInstall]:
    yield from sa_support.module_install(request, m02_ids, m05_reset, "sa:other")


@pytest.fixture(scope="module")
def sa_bare_install(
    request: pytest.FixtureRequest,
    m02_ids: m02_support.Identities,
    m05_reset: Callable[[tuple[str, str]], None],
) -> Iterator[sa_support.SAInstall]:
    """S-A units WITHOUT the test profile: what the units alone install."""

    yield from sa_support.module_install(request, m02_ids, m05_reset, "sa:bare", profile=False)


@pytest.fixture
def sa_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """R-SA-10: active for every S-A PostgreSQL row (pytestmark usefixtures); fails
    the row at teardown on any forbidden use, even one the code swallowed."""

    calls = sa_support.install_guard(monkeypatch)
    yield calls
    assert calls == [], f"S-A side effect attempted: {calls}"
