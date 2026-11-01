"""B3b service test support (package v2 + v2r1, Claude/Codex aligned 2026-11-01).

Every PostgreSQL row reaches SQL only through the composed runtime and the real M01
gateway: the staff test registry (target 25), and a TEST statement catalogue =
production module statements + M05_STATEMENTS + STAFF_STATEMENTS. No direct SET ROLE.

`Recorder` wraps the real gateway to count route calls, trace statement keys and
parameters, and (test-only) substitute the rows a statement returns AFTER the real
statement ran, so corrupted-output rows exercise rollback of real writes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final
from uuid import NAMESPACE_URL, uuid5

import staff_support as st
from psycopg import AsyncConnection

READ: Final = "m05.followup.read"
PREPARE: Final = "m05.followup.prepare"
APPLY: Final = "m05.followup.apply"
SUPERVISE: Final = "m05.followup.supervise"
CREATE: Final = "m05.followup.create"
STAFF_ADMIN: Final = "m01.staff.admin"
STAFF_DISPLAY: Final = "m01.staff.display"

STAFF_CAPS: Final = frozenset({READ, PREPARE, APPLY})
SUPERVISOR_CAPS: Final = STAFF_CAPS | {SUPERVISE}
READ_ONLY_CAPS: Final = frozenset({READ})

# Statement keys (m05/statements.py, m01/statements.py STAFF_STATEMENTS).
K_PREPARE: Final = "m05.followup.prepare"
K_APPLY: Final = "m05.followup.apply"
K_GET: Final = "m05.followup.get"
K_LIST: Final = "m05.followup.list"
K_HISTORY: Final = "m05.followup.history"
K_ADMIN: Final = "m01.staff.admin"
K_DISPLAY: Final = "m01.staff.display"
K_READER: Final = "m01.staff.followup_reader"

SERVICE_TARGET: Final = frozenset({st.STAFF_TEST_TARGET})


def service_catalog() -> Any:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import STAFF_STATEMENTS, build_statement_catalog
    from haloflow.m05.statements import M05_STATEMENTS

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, M05_STATEMENTS, STAFF_STATEMENTS)


def _factory(conninfo: str) -> Callable[[], Awaitable[AsyncConnection[Any]]]:
    async def _connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


@asynccontextmanager
async def runtime(
    install: Any, pool_class: Any = None, login: str | None = None
) -> AsyncIterator[Any]:
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    install.require()
    cls = pool_class or TenantPool
    pool = cls(login or install.ids.logins["haloflow_runtime"], min_size=1, max_size=1)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=_factory(install.ids.logins["haloflow_migrator"]),
                provisioner_connect=_factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=st.staff_registry(),
            catalog=service_catalog(),
            supported_schema_versions=SERVICE_TARGET,
        )
    finally:
        await pool.close()


async def context(
    rt: Any,
    install: Any,
    principal_id: str,
    capabilities: frozenset[str],
    *,
    kind: str = "actor",
    auth_method: str = "test",
    purpose: str = "operations",
    request_id: str | None = None,
) -> Any:
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource

    return await rt.resolver.resolve(
        principal=Principal(
            kind=PrincipalKind(kind),
            id=principal_id,
            auth_method=auth_method,
            authorized_tenant_ids=frozenset({install.tenant[0]}),
            capabilities=capabilities,
        ),
        tenant_hint=install.tenant[0],
        purpose=purpose,
        capabilities=capabilities,
        source=TrustedSource.HTTP,
        execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:b3b:{uuid.uuid4()}"),
        correlation_id=uuid.uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
        request_id=request_id,
    )


class _Handle:
    def __init__(self, handle: Any, recorder: Recorder) -> None:
        self._handle = handle
        self._recorder = recorder

    def _note(self, key: str, params: Any) -> None:
        self._recorder.statements.append(key)
        self._recorder.params.append((key, params))

    async def execute(self, key: str, params: Any = None) -> Any:
        self._note(key, params)
        return await self._handle.execute(key, params)

    async def fetch_one(self, key: str, params: Any = None) -> Any:
        self._note(key, params)
        row = await self._handle.fetch_one(key, params)
        if key in self._recorder.substitute:
            rows = self._recorder.substitute[key]
            return rows[0] if rows else None
        return row

    async def fetch_all(self, key: str, params: Any = None) -> Any:
        self._note(key, params)
        rows = await self._handle.fetch_all(key, params)
        if key in self._recorder.substitute:
            return list(self._recorder.substitute[key])
        return rows


class Recorder:
    """Duck-typed gateway: the services use only `with_tenant_transaction`."""

    def __init__(self, gateway: Any, substitute: dict[str, list[tuple[Any, ...]]] | None = None):
        self._gateway = gateway
        self.calls = 0
        self.statements: list[str] = []
        self.params: list[tuple[str, Any]] = []
        self.substitute = dict(substitute or {})

    async def with_tenant_transaction(self, context: Any, callback: Any, **kwargs: Any) -> Any:
        self.calls += 1

        async def wrapped(handle: Any) -> Any:
            return await callback(_Handle(handle, self))

        return await self._gateway.with_tenant_transaction(context, wrapped, **kwargs)

    def sent(self, key: str) -> list[Any]:
        """The JSON objects sent as %(command)s / %(query)s for `key`, in order."""

        out: list[Any] = []
        for k, params in self.params:
            if k != key:
                continue
            value = next(iter(params.values()))
            out.append(getattr(value, "obj", value))
        return out
