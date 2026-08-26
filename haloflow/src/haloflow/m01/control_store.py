"""Explicitly qualified access to the non-PHI shared tenant registry."""

from haloflow.m01.pool import TenantPool
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord


class PsycopgControlStore:
    """Minimal shared-control reader used during tenant resolution."""

    def __init__(self, pool: TenantPool) -> None:
        self._pool = pool

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        async with self._pool._connection_for_control() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT tenant_id, schema_key, lifecycle_state, schema_version
                    FROM shared.tenants
                    WHERE tenant_id = %s
                    """,
                    (tenant_id,),
                )
            ).fetchone()

        if row is None:
            return None
        try:
            lifecycle_state = LifecycleState(row[2])
        except ValueError:
            return None
        return TenantRegistryRecord(
            tenant_id=row[0],
            schema_key=row[1],
            lifecycle_state=lifecycle_state,
            schema_version=row[3],
        )
