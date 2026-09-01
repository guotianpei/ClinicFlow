"""Version and checksum drift across active tenant schemas (R-E5).

The query runs on a provisioner-role connection, which holds SELECT on
``shared.tenants`` and on ``shared.schema_migrations`` and write access to
neither ledger row -- reporting drift must not be able to alter it.

``schema_migrations_drift_idx`` on ``(state, migration_id, tenant_id)`` already
exists in migration ``001``; this is the reader it was indexed for.
"""

from dataclasses import dataclass

from psycopg import AsyncConnection

from haloflow.m01.provisioning.runner import ConnectionFactory
from haloflow.m01.provisioning.units import TenantMigrationRegistry


@dataclass(frozen=True, slots=True)
class TenantDrift:
    """One tenant's distance from the registry, all fields ordered for stable output."""

    tenant_id: str
    lifecycle_state: str
    schema_version: int
    missing_migrations: tuple[str, ...]
    """Registry units with no `applied` ledger row for this tenant."""
    failed_migrations: tuple[str, ...]
    drifted_migrations: tuple[str, ...]
    """Applied units whose recorded checksum no longer matches the registry."""

    @property
    def is_current(self) -> bool:
        return not (self.missing_migrations or self.failed_migrations or self.drifted_migrations)


async def report_drift(
    connect: ConnectionFactory,
    registry: TenantMigrationRegistry,
    *,
    include_current: bool = False,
) -> tuple[TenantDrift, ...]:
    """Report every tenant that is not at the registry's target state."""

    connection = await connect()
    try:
        return await report_drift_on(connection, registry, include_current=include_current)
    finally:
        await connection.close()


async def report_drift_on(
    connection: AsyncConnection,
    registry: TenantMigrationRegistry,
    *,
    include_current: bool = False,
) -> tuple[TenantDrift, ...]:
    """Report drift over an already-open provisioner-role connection."""

    expected = {unit.migration_id: unit.checksum for unit in registry}

    tenant_rows = await (
        await connection.execute(
            """
            SELECT tenant_id, lifecycle_state, schema_version
            FROM shared.tenants
            ORDER BY tenant_id
            """
        )
    ).fetchall()
    ledger_rows = await (
        await connection.execute(
            """
            SELECT tenant_id, migration_id, state, checksum
            FROM shared.schema_migrations
            """
        )
    ).fetchall()

    by_tenant: dict[str, dict[str, tuple[str, str]]] = {}
    for tenant_id, migration_id, state, checksum in ledger_rows:
        by_tenant.setdefault(tenant_id, {})[migration_id] = (state, checksum)

    reports: list[TenantDrift] = []
    for tenant_id, lifecycle_state, schema_version in tenant_rows:
        ledger = by_tenant.get(tenant_id, {})
        missing: list[str] = []
        failed: list[str] = []
        drifted: list[str] = []
        for migration_id, checksum in expected.items():
            recorded = ledger.get(migration_id)
            if recorded is None:
                missing.append(migration_id)
                continue
            state, recorded_checksum = recorded
            if state == "failed":
                failed.append(migration_id)
            elif state != "applied":
                missing.append(migration_id)
            elif recorded_checksum != checksum:
                drifted.append(migration_id)

        report = TenantDrift(
            tenant_id=tenant_id,
            lifecycle_state=lifecycle_state,
            schema_version=schema_version,
            missing_migrations=tuple(sorted(missing)),
            failed_migrations=tuple(sorted(failed)),
            drifted_migrations=tuple(sorted(drifted)),
        )
        if include_current or not report.is_current:
            reports.append(report)

    return tuple(reports)
