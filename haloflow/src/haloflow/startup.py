"""Trusted production startup (UC1 part B, tranche B5).

The design is B5 package v2 + v2r1, aligned by Codex on 2026-11-02:
- 6abf4d64: v1 REVISE;
- fd6cd794: v2 rulings P-3(a), P-4, this file, and the precedence;
- 4b0a5448: aligned for the red tranche;
- 32e72016: the migrator reads four columns of ``shared.tenants`` (revision 007).

Behaviour:
- ``start_production_services`` validates its inputs exactly, before any I/O. It
  then runs the static composition checks (registry, installed-state profiles,
  catalogue, and the supported-version declaration read from
  ``haloflow.composition`` at call time, which must equal the registry target).
- Then, for every served tenant in the captured order, it opens one migrator
  connection (``dependencies.migrator_connect``), refuses it unless it is idle, and
  runs one ``REPEATABLE READ, READ ONLY`` transaction as ``haloflow_migrator``.
  That transaction reads, in this order, all from one snapshot:
  1. the ``shared.tenants`` row (active, supported version, valid schema key);
  2. the installed-state comparator rows of every typed unit, compared with the
     unit's own frozen declaration rendered for that schema;
  3. the generation (``load_generation_in``), which is then validated for the
     deployment.
  The transaction is rolled back and the connection closed before the next tenant.
  Any refusal aborts startup (P-2(a)). READ ONLY is a transaction scope, not a
  claim of reduced role privilege.
- Only after every tenant has validated and every startup connection is closed is
  the production runtime composed and the services constructed.
- Errors are ``StartupRefused(reason_code, tenant_index)`` from a closed
  vocabulary. They are recorded inside handlers and raised only after every
  handler has exited, so ``__cause__`` and ``__context__`` are both None. They
  carry no tenant id, schema key or driver text; the log line carries only the
  reason code and the index. ``asyncio.CancelledError`` is never converted: it
  propagates after cleanup, and wins over a recorded refusal.
- The returned bundle holds only services built over ``ServedTenantGateway``,
  which admits only the validated ``(tenant_id, schema_key)`` pairs before it
  delegates, plus the narrowed resolver facade, which checks the captured tenant
  ids before any backing-resolver call. It holds no raw resolver, gateway, pool,
  connection or connection factory. The per-transaction schema-version recheck
  stays with the real gateway.

Non-claims:
- L-1W stays open: no application entry point calls this factory yet.
- The validation is a startup snapshot. Drift after startup is not detected; the
  enforcement boundary for that assumption is L-2 plus the gateway's existing
  per-transaction checks.
- Admission is an accidental-bypass boundary, not protection against arbitrary
  trusted Python code.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, TypeVar, cast
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.pq import TransactionStatus

import haloflow.composition as composition
from haloflow.m01.context import CorrelationSource, Principal, TenantContext, TrustedSource
from haloflow.m01.gateway import (
    TenantRepositoryHandle,
    TenantTransactionGateway,
    TransactionOptions,
)
from haloflow.m01.provisioning import installed_state
from haloflow.m01.provisioning.roles import MIGRATOR_ROLE
from haloflow.m01.provisioning.verification import VerificationMismatch
from haloflow.m01.resolver import (
    SCHEMA_KEY_PATTERN,
    TENANT_ID_PATTERN,
    LifecycleState,
    TenantResolver,
)
from haloflow.m01.runtime import TenantRuntimeDependencies
from haloflow.m01.staff.service import StaffService
from haloflow.m02 import registry_validation
from haloflow.m02.observation import EventFoundation
from haloflow.m05.notes import ProductionNoteKeys
from haloflow.m05.service import M05Service

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

_LOG = logging.getLogger(__name__)

T = TypeVar("T")


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

_CONNECTION_FAILED: Final = "STARTUP_CONNECTION_FAILED"
_NOT_ACTIVE: Final = "TENANT_NOT_ACTIVE"
_VERSION_UNSUPPORTED: Final = "SCHEMA_VERSION_UNSUPPORTED"
_DRIFT: Final = "INSTALLED_STATE_DRIFT"
_UNREADABLE: Final = "GENERATION_UNREADABLE"
_COMPOSITION_INVALID: Final = "COMPOSITION_INVALID"
_SCHEMA_PLACEHOLDER: Final = "{schema}"

_TENANT_ROW_QUERY: Final = (
    "SELECT tenant_id, schema_key, lifecycle_state::pg_catalog.text, schema_version"
    " FROM shared.tenants WHERE tenant_id = %s"
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


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _admitted(context: object, validated: frozenset[tuple[str, str]]) -> bool:
    if type(context) is not TenantContext:
        return False
    tenant_id, schema_key = context.tenant_id, context.schema_key
    if type(tenant_id) is not str or type(schema_key) is not str:
        return False
    return (tenant_id, schema_key) in validated


class ServedTenantGateway:
    """Admission on the gateway path. Every returned service runs through this.

    A context whose ``(tenant_id, schema_key)`` is not one of the pairs validated at
    startup is refused ``TenantNotServed`` before the real gateway is reached, so
    no connection is acquired for it. The schema version is rechecked by the real
    gateway on every transaction (``_revalidate_registry``)."""

    __slots__ = ("__gateway", "__validated")

    def __init__(
        self,
        gateway: TenantTransactionGateway,
        *,
        validated: frozenset[tuple[str, str]],
    ) -> None:
        self.__gateway = gateway
        self.__validated = frozenset(validated)

    async def with_tenant_transaction(
        self,
        context: TenantContext,
        callback: Callable[[TenantRepositoryHandle], Awaitable[T]],
        *,
        options: TransactionOptions | None = None,
    ) -> T:
        if not _admitted(context, self.__validated):
            raise TenantNotServed()
        return await self.__gateway.with_tenant_transaction(context, callback, options=options)


class ServedTenantResolver:
    """The narrowed resolver facade. It checks the captured allowed ids before any
    call to the backing resolver, and the validated pair after it."""

    __slots__ = ("__allowed_ids", "__resolver", "__validated")

    def __init__(
        self,
        resolver: TenantResolver,
        *,
        allowed_ids: frozenset[str],
        validated: frozenset[tuple[str, str]],
    ) -> None:
        self.__resolver = resolver
        self.__allowed_ids = frozenset(allowed_ids)
        self.__validated = frozenset(validated)

    async def resolve(
        self,
        *,
        principal: Principal,
        tenant_hint: str,
        purpose: str,
        capabilities: frozenset[str],
        source: TrustedSource,
        execution_id: UUID,
        correlation_id: UUID,
        correlation_source: CorrelationSource,
        request_id: str | None = None,
    ) -> TenantContext:
        if type(tenant_hint) is not str or tenant_hint not in self.__allowed_ids:
            raise TenantNotServed()
        context = await self.__resolver.resolve(
            principal=principal,
            tenant_hint=tenant_hint,
            purpose=purpose,
            capabilities=capabilities,
            source=source,
            execution_id=execution_id,
            correlation_id=correlation_id,
            correlation_source=correlation_source,
            request_id=request_id,
        )
        if not _admitted(context, self.__validated):
            raise TenantNotServed()
        return context


@dataclass(frozen=True, slots=True)
class ProductionServices:
    m05: M05Service
    staff: StaffService
    resolver: ServedTenantResolver


# ---------------------------------------------------------------------------
# Static checks (no I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FunctionCheck:
    """One typed unit's installed-state expectation, with ``{schema}`` unrendered."""

    profile: installed_state.InstalledStateProfile
    config: tuple[str, ...]
    body: str

    def expected(self, schema_key: str) -> tuple[tuple[str, ...], str]:
        # The same rendering as the runner's `_installed_expectations`.
        config = tuple(entry.replace(_SCHEMA_PLACEHOLDER, schema_key) for entry in self.config)
        return config, self.body.replace(_SCHEMA_PLACEHOLDER, schema_key)


@dataclass(frozen=True, slots=True)
class _StaticPlan:
    supported: frozenset[int]
    functions: tuple[_FunctionCheck, ...]


class _CompositionInvalid(Exception):
    pass


def _function_check(unit: Any, profile: installed_state.InstalledStateProfile) -> _FunctionCheck:
    """From the unit's OWN frozen declaration, as the runner reads it at install."""

    functions = (unit.policy_verification or {}).get("functions", ())
    matches = [
        function
        for function in functions
        if function["name"] == profile.function_name
        and tuple(function["argument_types"]) == profile.argument_types
    ]
    if len(matches) != 1:
        raise _CompositionInvalid
    function = matches[0]
    config = tuple(function["config"])
    body = function["body"]
    if not all(type(entry) is str for entry in config) or type(body) is not str:
        raise _CompositionInvalid
    return _FunctionCheck(profile=profile, config=config, body=body)


def _valid_supported(value: object, target: int) -> bool:
    if type(value) is not frozenset or not value:
        return False
    if not all(type(version) is int and version > 0 for version in value):
        return False
    return value == frozenset({target})


def _build_static_plan() -> _StaticPlan:
    """Raises on any failure; the caller maps every failure to COMPOSITION_INVALID."""

    registry = composition.build_production_tenant_migrations()
    composition.require_installed_state_profiles(registry)
    composition.build_production_catalog()
    supported: object = composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS
    if not _valid_supported(supported, registry.target_version):
        raise _CompositionInvalid
    checks: list[_FunctionCheck] = []
    for unit in registry:
        if not unit.is_typed:
            continue
        requirement = registry.installed_state_requirement(unit.migration_id)
        if not requirement.required or requirement.profile is None:
            raise _CompositionInvalid
        # L-1W W3b: a served tenant is at the target version, so a profile amended by
        # an `acl_closure_v1` unit is compared in its amended form (exactly the
        # declared removals leave the ACL; every other field stays the accepted one).
        amended = registry.acl_amendment(unit.migration_id)
        profile = requirement.profile
        if amended is not None:
            amendment, activator_version = amended
            profile = installed_state.effective_profile(
                profile, amendment,
                activator_version=activator_version,
                checked_version=registry.target_version,
            )
        checks.append(_function_check(unit, profile))
    return _StaticPlan(supported=cast("frozenset[int]", supported), functions=tuple(checks))


def _static_plan_or_code() -> _StaticPlan | str:
    try:
        return _build_static_plan()
    except Exception:  # noqa: BLE001 - every static failure is the one fixed code
        return _COMPOSITION_INVALID


# ---------------------------------------------------------------------------
# Per-tenant verification: one connection, one snapshot
# ---------------------------------------------------------------------------


def _transaction_status(connection: Any) -> TransactionStatus | None:
    try:
        status = connection.info.transaction_status
    except Exception:  # noqa: BLE001 - an unreadable status is not an idle connection
        return None
    return status if isinstance(status, TransactionStatus) else None


def _registry_code(error: registry_validation.RegistryRefused) -> str:
    code = getattr(error, "reason_code", None)
    if type(code) is str and code in STARTUP_REASON_CODES:
        return code
    return _UNREADABLE


def _tenant_row_code(
    rows: Sequence[Sequence[Any]], tenant_id: str, supported: frozenset[int]
) -> tuple[str, int] | str:
    """The validated ``(schema_key, schema_version)``, or a refusal code."""

    if len(rows) != 1:
        return _NOT_ACTIVE
    row = rows[0]
    if len(row) != 4:
        return _UNREADABLE
    row_tenant, schema_key, lifecycle_state, schema_version = row
    if row_tenant != tenant_id or lifecycle_state != LifecycleState.ACTIVE.value:
        return _NOT_ACTIVE
    if type(schema_version) is not int or schema_version not in supported:
        return _VERSION_UNSUPPORTED
    if type(schema_key) is not str or not SCHEMA_KEY_PATTERN.fullmatch(schema_key):
        return _UNREADABLE
    return schema_key, schema_version


async def _check_tenant(
    connection: AsyncConnection[Any],
    *,
    tenant_id: str,
    deployment: Deployment,
    after_tenant_row: Callable[[], Awaitable[None]] | None,
    plan: _StaticPlan,
) -> tuple[str, str, int] | str:
    """The checker body: a validated triple, or a reason code. It never raises a
    mapped failure; only CancelledError (and a test hook's own error) propagate.
    The transaction is left for the caller to roll back only on a refusal."""

    if _transaction_status(connection) is not TransactionStatus.IDLE:
        return _CONNECTION_FAILED
    try:
        await connection.set_autocommit(True)
        # Session settings, each its own committed statement, before the snapshot
        # transaction: the migrator login reaches its privileges only through SET.
        await connection.execute("SET search_path = pg_catalog, pg_temp")
        await connection.execute(
            sql.SQL("SET ROLE {}").format(sql.Identifier(MIGRATOR_ROLE))
        )
        await connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    except Exception:  # noqa: BLE001 - driver text is never kept
        return _CONNECTION_FAILED

    # (1) The tenants row. Its SELECT is the first statement of the transaction, so
    # it fixes the snapshot that (2) and (3) share.
    try:
        rows = await (await connection.execute(_TENANT_ROW_QUERY, (tenant_id,))).fetchall()
    except Exception:  # noqa: BLE001
        return _UNREADABLE
    bound = _tenant_row_code(rows, tenant_id, plan.supported)
    if isinstance(bound, str):
        return bound
    schema_key, schema_version = bound

    if after_tenant_row is not None:
        await after_tenant_row()

    # (2) Installed state of every typed unit.
    for check in plan.functions:
        profile = check.profile
        expected_config, expected_body = check.expected(schema_key)
        try:
            function_rows = await (
                await connection.execute(
                    installed_state.INSTALLED_FUNCTION_QUERY,
                    (list(profile.argument_types), profile.return_type,
                     schema_key, profile.function_name),
                )
            ).fetchall()
        except Exception:  # noqa: BLE001
            return _UNREADABLE
        try:
            installed_state.compare_installed_function(
                profile,
                schema_key=schema_key,
                expected_config=expected_config,
                expected_body=expected_body,
                rows=function_rows,
            )
        except VerificationMismatch:
            return _DRIFT
        except Exception:  # noqa: BLE001
            return _UNREADABLE

    # (3) The generation, in the same transaction, then validated.
    try:
        generation = await registry_validation.load_generation_in(connection, schema_key)
    except registry_validation.RegistryRefused as refused:
        return _registry_code(refused)
    except Exception:  # noqa: BLE001
        return _UNREADABLE
    try:
        registry_validation.validate_loaded_generation(generation, deployment=deployment.value)
    except registry_validation.RegistryRefused as refused:
        return _registry_code(refused)
    except Exception:  # noqa: BLE001
        return _UNREADABLE

    try:
        await connection.rollback()
    except Exception:  # noqa: BLE001
        return _CONNECTION_FAILED
    return tenant_id, schema_key, schema_version


async def _verify_tenant(
    connection: Any,
    *,
    tenant_id: str,
    deployment: Deployment,
    after_tenant_row: Any = None,
    plan: _StaticPlan | None = None,
) -> tuple[str, str, int]:
    """The private per-tenant checker. ``after_tenant_row`` is a test-only hook,
    awaited after read (1); B5-S15 uses it as the SU-SNAPSHOT barrier. Returns the
    validated ``(tenant_id, schema_key, schema_version)``, or raises
    ``StartupRefused(reason_code)`` with no tenant index and no chained error.
    Without ``plan`` the static plan is built here from the composition."""

    resolved = _static_plan_or_code() if plan is None else plan
    outcome: tuple[str, str, int] | str
    if isinstance(resolved, str):
        outcome = resolved
    else:
        outcome = await _check_tenant(
            connection,
            tenant_id=tenant_id,
            deployment=deployment,
            after_tenant_row=after_tenant_row,
            plan=resolved,
        )
    if isinstance(outcome, str):
        raise StartupRefused(outcome)
    return outcome


async def _verify_served_tenant(
    dependencies: TenantRuntimeDependencies,
    *,
    tenant_id: str,
    deployment: Deployment,
    plan: _StaticPlan,
) -> tuple[str, str, int] | str:
    """One startup connection: acquire, refuse unless idle, verify, then roll back
    and close, each guarded. A cleanup failure with no prior failure refuses
    STARTUP_CONNECTION_FAILED; after a prior failure the original is kept.
    CancelledError is never converted and propagates after cleanup."""

    try:
        connection = await dependencies.migrator_connect()
    except Exception:  # noqa: BLE001
        return _CONNECTION_FAILED

    code: str | None = None
    validated: tuple[str, str, int] | None = None
    cleanup_failed = False
    try:
        if _transaction_status(connection) is not TransactionStatus.IDLE:
            code = _CONNECTION_FAILED  # never adopted (runner rule, runner.py:546)
        else:
            try:
                validated = await _verify_tenant(
                    connection, tenant_id=tenant_id, deployment=deployment, plan=plan
                )
            except StartupRefused as refused:
                code = refused.reason_code
            except Exception:  # noqa: BLE001
                code = _UNREADABLE
    finally:
        try:
            try:
                if _transaction_status(connection) is not TransactionStatus.IDLE:
                    await connection.rollback()
            except Exception:  # noqa: BLE001
                cleanup_failed = True
        finally:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001
                cleanup_failed = True

    if code is not None:
        return code
    if cleanup_failed or validated is None:
        return _CONNECTION_FAILED
    return validated


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


def _refusal(reason_code: str, tenant_index: int | None) -> StartupRefused:
    """A fixed log line: reason code and index only."""

    _LOG.warning("startup refused: reason=%s tenant_index=%s", reason_code, tenant_index)
    return StartupRefused(reason_code, tenant_index)


def _capture_served(served_tenants: object) -> tuple[str, ...]:
    if type(served_tenants) is not list and type(served_tenants) is not tuple:
        raise TypeError("served_tenants must be a list or tuple of str")
    captured = tuple(cast("Sequence[object]", served_tenants))
    for tenant_id in captured:
        if type(tenant_id) is not str:
            raise TypeError("served_tenants must be a list or tuple of str")
        if not TENANT_ID_PATTERN.fullmatch(tenant_id):
            raise ValueError("served tenant id is outside the tenant-id grammar")
    return cast("tuple[str, ...]", captured)


async def start_production_services(
    dependencies: TenantRuntimeDependencies,
    *,
    deployment: Deployment,
    served_tenants: Sequence[str],
) -> ProductionServices:
    # 1. Exact inputs, before any I/O.
    if type(dependencies) is not TenantRuntimeDependencies:
        raise TypeError("dependencies must be TenantRuntimeDependencies")
    if type(deployment) is not Deployment:
        raise TypeError("deployment must be a Deployment")
    captured = _capture_served(served_tenants)
    if not captured:
        raise StartupRefused("STARTUP_NO_TENANTS")
    seen: set[str] = set()
    for index, tenant_id in enumerate(captured):
        if tenant_id in seen:
            raise StartupRefused("STARTUP_TENANT_DUPLICATE", index)
        seen.add(tenant_id)

    # 2. Static checks (no I/O).
    plan = _static_plan_or_code()
    if isinstance(plan, str):
        raise _refusal(plan, None)

    # 3. Every tenant, in order; the first refusal aborts. Each startup connection
    # is closed before the next tenant and before anything is constructed.
    validated: list[tuple[str, str, int]] = []
    for index, tenant_id in enumerate(captured):
        outcome = await _verify_served_tenant(
            dependencies, tenant_id=tenant_id, deployment=deployment, plan=plan
        )
        if isinstance(outcome, str):
            raise _refusal(outcome, index)
        validated.append(outcome)

    # 4. Construction, only now.
    pairs = frozenset((tenant_id, schema_key) for tenant_id, schema_key, _ in validated)
    services: ProductionServices | None = None
    try:
        runtime = composition.build_production_tenant_runtime(dependencies)
        gateway = ServedTenantGateway(runtime.gateway, validated=pairs)
        # The foundation is typed against the real gateway; the served wrapper
        # mirrors the one method it uses (`with_tenant_transaction`).
        foundation = EventFoundation(
            cast("TenantTransactionGateway", gateway), producer_service="m05_followup"
        )
        services = ProductionServices(
            m05=M05Service(gateway, foundation=foundation, keys=ProductionNoteKeys()),
            staff=StaffService(gateway),
            resolver=ServedTenantResolver(
                runtime.resolver, allowed_ids=frozenset(captured), validated=pairs
            ),
        )
    except Exception:  # noqa: BLE001
        services = None
    if services is None:
        raise _refusal(_COMPOSITION_INVALID, None)
    return services
