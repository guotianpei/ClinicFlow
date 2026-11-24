"""Eligibility E5 (EL-12) composition helpers for the synthetic lifecycle. SYNTHETIC ONLY.

- `build_chart`: the SIMULATED chart (`haloflow.m05.simulated_chart`) over the install's own
  database (schema `sim_chart`, created by the eligibility install profile), connecting as the
  runtime login under its own application name `<app>-chart` (never counted as a pool backend).
- `context_resolver`: issues, synchronously, the two E5 contexts the lifecycle exposes — the
  doc worker's timer-workload context (exactly the doc capability) and an authorized staff
  context (actor, exactly the manual capability). Both are issued by the COMPOSED runtime
  resolver's policy (a shallow copy of `rt.resolver`: same supported versions, purposes and
  TTL; this module constructs no runtime component, L1-A1) whose registry read is answered
  from the record read through the composed control store once at start (the tenant, schema
  key, lifecycle state and schema version cannot change while the runtime holds the install
  lease), so issuing needs no I/O and never suspends.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Coroutine
from typing import Any

from psycopg.conninfo import make_conninfo

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.resolver import TenantRegistryRecord, TenantResolver
from haloflow.m05.eligibility_doc import DOC_CAPABILITIES, STAFF_CAPABILITIES
from haloflow.m05.simulated_chart import SimulatedChart
from haloflow.synthetic import profiles
from haloflow.synthetic.composition import W_TIMER
from haloflow.synthetic.errors import SyntheticRefused

STAFF_AUTH_METHOD = "synthetic_staff"
STAFF_PURPOSE = "operations"


def build_chart(runtime: Any, application_name: str) -> SimulatedChart:
    login = runtime.state["logins"]["runtime"]
    conninfo = make_conninfo(runtime.endpoint.conninfo(runtime.endpoint.target),
                             user=login["role"], password=login["password"],
                             application_name=f"{application_name}-chart")
    return SimulatedChart(conninfo)


class _RecordStore:
    def __init__(self, record: TenantRegistryRecord) -> None:
        self._record = record

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        return self._record if tenant_id == self._record.tenant_id else None


def _issue(coro: Coroutine[Any, Any, TenantContext]) -> TenantContext:
    """Drive a resolve that never suspends (its store answers without I/O) to completion."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value  # type: ignore[no-any-return]
    coro.close()
    raise SyntheticRefused("CONTEXT_UNAVAILABLE")


class ContextResolver:
    def __init__(self, resolver: TenantResolver, tenant_id: str) -> None:
        self._resolver = resolver
        self._tenant = tenant_id

    def _resolve(self, principal: Principal, caps: frozenset[str],
                 source: TrustedSource, corr: CorrelationSource) -> TenantContext:
        return _issue(self._resolver.resolve(
            principal=principal, tenant_hint=self._tenant, purpose=STAFF_PURPOSE,
            capabilities=caps, source=source, execution_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(), correlation_source=corr))

    def doc(self) -> TenantContext:
        caps = frozenset(DOC_CAPABILITIES)
        return self._resolve(
            Principal(kind=PrincipalKind.WORKLOAD, id=W_TIMER, auth_method="worker",
                      authorized_tenant_ids=frozenset({self._tenant}), capabilities=caps),
            caps, TrustedSource.WORKER, CorrelationSource.TRUSTED_INFRASTRUCTURE)

    def staff(self, staff_id: str) -> TenantContext:
        caps = frozenset(STAFF_CAPABILITIES)
        return self._resolve(
            Principal(kind=PrincipalKind.ACTOR, id=staff_id, auth_method=STAFF_AUTH_METHOD,
                      authorized_tenant_ids=frozenset({self._tenant}), capabilities=caps),
            caps, TrustedSource.HTTP, CorrelationSource.ENTRY_POINT_GENERATED)


async def context_resolver(runtime: Any, tenant_id: str) -> ContextResolver:
    composed = runtime.tenant_runtime.resolver
    if not isinstance(composed, TenantResolver):
        raise SyntheticRefused("RESOLVER_INVALID")
    record = await runtime.dependencies.control_store.get_tenant(tenant_id)
    if record is None or record.schema_version != profiles.TARGETS[profiles.ELIGIBILITY]:
        raise SyntheticRefused("TENANT_NOT_FOUND")
    resolver = copy.copy(composed)
    resolver._control_store = _RecordStore(record)  # noqa: SLF001 - answered from the record
    return ContextResolver(resolver, tenant_id)
