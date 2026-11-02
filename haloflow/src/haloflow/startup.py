"""Trusted production startup (UC1 part B, tranche B5).

The design is B5 package v2 + v2r1, aligned by Codex on 2026-11-02:
- 6abf4d64: v1 REVISE;
- fd6cd794: v2 rulings P-3(a), P-4, this file, and the precedence;
- 4b0a5448: aligned for the red tranche.

RED TRANCHE: this module pins the interface only. Every entry point fails closed
with ``NotImplementedError``; no behaviour is implemented yet.

Aligned behaviour, implemented after the tests-first gate:
- ``start_production_services`` validates its inputs exactly, before any I/O. It
  then runs the static composition checks, and then, for every served tenant, one
  migrator connection with one REPEATABLE READ, READ ONLY transaction. That
  transaction reads, in this order:
  1. the ``shared.tenants`` row;
  2. the installed-state comparator rows;
  3. the generation, which is then validated.
  Any refusal aborts startup (P-2(a)). The connection is closed before anything is
  constructed.
- Errors are ``StartupRefused(reason_code, tenant_index)``, raised outside every
  handler, so ``__cause__`` and ``__context__`` are both None. They carry no
  tenant id, schema key or driver text.
- The returned bundle holds only services built over ``ServedTenantGateway``,
  which admits only the validated tenants, plus the narrowed resolver facade. It
  holds no raw resolver, gateway, pool or connection factory.

Non-claims:
- L-1W stays open: no application entry point calls this factory yet.
- The validation is a startup snapshot. Drift after startup is not detected.
- Admission is an accidental-bypass boundary, not protection against arbitrary
  trusted Python code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

__all__ = [
    "STARTUP_REASON_CODES",
    "Deployment",
    "ProductionServices",
    "ServedTenantGateway",
    "ServedTenantResolver",
    "StartupRefused",
    "TenantNotServed",
    "start_production_services",
]


class Deployment(Enum):
    PRODUCTION = "production"
    NON_PRODUCTION = "non_production"


# The closed refusal vocabulary (v2 section 2.4, v2r1). The red tests pin this set.
STARTUP_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "STARTUP_NO_TENANTS",
        "STARTUP_TENANT_DUPLICATE",
        "STARTUP_CONNECTION_FAILED",
        "TENANT_NOT_ACTIVE",
        "SCHEMA_VERSION_UNSUPPORTED",
        "INSTALLED_STATE_DRIFT",
        "GENERATION_UNREADABLE",
        "ROUTE_NOT_INSTALLED",
        "SIGNAL_RULE_REQUIRED",
        "M05_ACTIVATION_NOT_AUTHORIZED",
        "TEST_FAMILY_ACTIVE_IN_PRODUCTION",
        "SYNTHETIC_DISPATCH_IN_PRODUCTION",
        "TEST_INTAKE_PRESENT",
        "COMPOSITION_INVALID",
    }
)


class StartupRefused(Exception):
    """A fixed reason code plus the refused tenant's position in the captured tuple
    (``None`` for a refusal that concerns no single tenant)."""

    def __init__(self, reason_code: str, tenant_index: int | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.tenant_index = tenant_index


class TenantNotServed(Exception):
    """Admission refusal. The tenant is not in the validated set captured at startup."""

    def __init__(self) -> None:
        super().__init__("TENANT_NOT_SERVED")
        self.reason_code = "TENANT_NOT_SERVED"


class ServedTenantGateway:
    """Admission on the gateway path. Every returned service runs through this."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("B5 served-tenant gateway")


class ServedTenantResolver:
    """The narrowed resolver facade. It checks the captured allowed ids before any
    call to the backing resolver."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("B5 served-tenant resolver")


@dataclass(frozen=True, slots=True)
class ProductionServices:
    m05: Any
    staff: Any
    resolver: ServedTenantResolver


async def start_production_services(
    dependencies: Any,
    *,
    deployment: Deployment,
    served_tenants: Sequence[str],
) -> ProductionServices:
    raise NotImplementedError("B5 startup")


async def _verify_tenant(
    connection: Any,
    *,
    tenant_id: str,
    deployment: Deployment,
    after_tenant_row: Any = None,
) -> tuple[str, str, int]:
    """The private per-tenant checker. ``after_tenant_row`` is a test-only hook,
    awaited after read (1); B5-S15 uses it as the SU-SNAPSHOT barrier. Returns the
    validated ``(tenant_id, schema_key, schema_version)``."""

    raise NotImplementedError("B5 tenant verification")
