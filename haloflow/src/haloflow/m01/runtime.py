"""Tenancy runtime composition: one supported-version set for every consumer (L-1).

The generic M01 builder. It knows no schema version number: the set arrives from
the composition root (`haloflow.composition`), which is the only production
caller (repository control L1-A2). It lives inside `haloflow.m01` because it
types and constructs against `TenantPool`, a module production code outside M01
may not import.

Order (architecture v1 addendum 1 C2):

1. validate the declared set;
2. check the registry's target version is in it;
3. construct the migration runner first, so the provisioning manifest loads
   before any other component exists;
4. construct the resolver, the gateway (set passed explicitly) and the
   provisioner (with that runner), all from the same set object;
5. return the bundle, and only here.

Any failure propagates unchanged; no component is returned or stored anywhere a
caller can reach. Nothing on this path opens a connection or uses the pool, and
the builder never opens or closes the pool: its lifecycle belongs to the caller
(L-1W). Each call returns new instances; there is no singleton.
"""

from dataclasses import dataclass

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.gateway import TenantTransactionGateway
from haloflow.m01.pool import TenantPool
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.provisioner import TenantProvisioner
from haloflow.m01.provisioning.runner import ConnectionFactory, TenantMigrationRunner
from haloflow.m01.provisioning.units import TenantMigrationRegistry
from haloflow.m01.provisioning.upgrade import TenantSchemaUpgrade, UpgradeTestHooks
from haloflow.m01.resolver import ControlStore, TenantResolver
from haloflow.m01.statements import CompiledCatalog


@dataclass(frozen=True, slots=True)
class TenantRuntimeDependencies:
    """Deployment inputs, injected. Creating them is application wiring (L-1W)."""

    pool: TenantPool
    control_store: ControlStore
    migrator_connect: ConnectionFactory
    provisioner_connect: ConnectionFactory


@dataclass(frozen=True, slots=True)
class TenantRuntime:
    """The composed runtime. All three components hold the same supported set."""

    resolver: TenantResolver
    gateway: TenantTransactionGateway
    provisioner: TenantProvisioner
    supported_schema_versions: frozenset[int]


def _unsupported() -> MigrationUnitRejected:
    return MigrationUnitRejected(reason_code=PreconditionCode.SCHEMA_VERSION_UNSUPPORTED.value)


def _require_valid_declaration(supported_schema_versions: frozenset[int]) -> None:
    """Step 1. A non-empty frozenset of positive ints; `bool` is not an int here."""

    if not isinstance(supported_schema_versions, frozenset) or not supported_schema_versions:
        raise _unsupported()
    for version in supported_schema_versions:
        # `type(...) is int` rejects bool explicitly: True == 1 in Python.
        if type(version) is not int or version <= 0:
            raise _unsupported()


def compose_tenant_runtime(
    dependencies: TenantRuntimeDependencies,
    *,
    registry: TenantMigrationRegistry,
    catalog: CompiledCatalog,
    supported_schema_versions: frozenset[int],
) -> TenantRuntime:
    """Compose resolver, gateway and provisioner from one supported-version set."""

    _require_valid_declaration(supported_schema_versions)
    # Step 2. An empty registry raises its own MIGRATION_REGISTRY_EMPTY here,
    # unchanged; it is not re-coded as SCHEMA_VERSION_UNSUPPORTED.
    if registry.target_version not in supported_schema_versions:
        raise _unsupported()

    runner = TenantMigrationRunner(dependencies.migrator_connect, registry)
    resolver = TenantResolver(
        dependencies.control_store,
        supported_schema_versions=supported_schema_versions,
    )
    gateway = TenantTransactionGateway(
        dependencies.pool,
        catalog,
        supported_schema_versions=supported_schema_versions,
    )
    provisioner = TenantProvisioner(
        dependencies.provisioner_connect,
        runner,
        supported_schema_versions=supported_schema_versions,
    )
    return TenantRuntime(
        resolver=resolver,
        gateway=gateway,
        provisioner=provisioner,
        supported_schema_versions=supported_schema_versions,
    )


@dataclass(frozen=True, slots=True)
class TenantUpgradeDependencies:
    """Deployment inputs for the L-6 upgrade (architecture v6 r3 §1): the P and M
    connection factories and T_lock. Creating them is application wiring."""

    provisioner_connect: ConnectionFactory
    migrator_connect: ConnectionFactory
    lock_timeout_seconds: float = 30.0


def compose_tenant_upgrade(
    dependencies: TenantUpgradeDependencies,
    *,
    registry: TenantMigrationRegistry,
    hooks: UpgradeTestHooks | None = None,
) -> TenantSchemaUpgrade:
    """The only construction site of ``TenantSchemaUpgrade`` (§6a). Opens nothing.

    ``hooks`` are the reviewed L-6 test seams; the production builder (CP-6) passes
    ``None``.
    """

    return TenantSchemaUpgrade(
        provisioner_connect=dependencies.provisioner_connect,
        migrator_connect=dependencies.migrator_connect,
        lock_timeout_seconds=dependencies.lock_timeout_seconds,
        registry=registry,
        hooks=hooks,
    )
