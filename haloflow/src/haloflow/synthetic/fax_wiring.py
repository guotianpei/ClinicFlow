"""Incoming fax routing FX-IN, package FX3a: the fax profile's synthetic composition. SYNTHETIC
ONLY.

- `SYN_FAX_LINE`: the install's one synthetic receiving line (a fictional NANP 555-01NN
  number), written by the installer (`fax_lines.install_line`, settings phase).
- `SYN_FAX_WORKLOAD`: the fax ingress workload id, seeded in `m05_fx_workloads` (profile phase).
- `start(...)`: at lifecycle start, AFTER the runtime is composed: the line fence (a dedicated
  runtime-login session holding the shared line lock for the lifetime of the lifecycle) is
  taken FIRST, then the directory is built from the tenant's lines (t144, the trusted startup
  context), then the unmounted ingress app (`fax_ingress.create_fax_ingress_router`) with a
  per-lifespan random secret (memory only). The simulator calls that app in process (no socket).
- `stop(...)`: closes the fence session (releasing the lock).
"""

from __future__ import annotations

import secrets
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Final

from psycopg.conninfo import make_conninfo

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m05 import fax_ingress
from haloflow.m05.fax import INGRESS_CAPABILITIES, LINES_CAPABILITIES, FaxRefused, FaxService
from haloflow.synthetic.wiring import build_fax_service

SYN_FAX_LINE: Final = "+15405550100"
SYN_FAX_WORKLOAD: Final = "00000000-0000-4000-8000-00000005a0f1"
FAX_WORKLOAD_SQL: Final = (
    "INSERT INTO {schema}.m05_fx_workloads (workload_id) VALUES ('" + SYN_FAX_WORKLOAD + "');")
SIM_PROVIDER: Final = "simfax"
PURPOSE: Final = "operations"
# TEST-ONLY fault names (v1r2): taken one per simulator delivery attempt from
# `FaxComposition.faults`. Empty in normal operation: nothing adds to it outside a test.
FAULTS: Final = ("lose_ack", "unavailable", "refuse")


class SimulatedUnavailable(Exception):
    """The `unavailable` fault: the receive step fails before any transaction (-> 503)."""


def build_service(rt: Any) -> FaxService:
    service: FaxService = build_fax_service(rt)   # constructed in `wiring` (B5-K01)
    return service


async def workload_context(resolver: Any, tenant_id: str, caps: frozenset[str],
                           source: TrustedSource) -> Any:
    return await resolver.resolve(
        principal=Principal(kind=PrincipalKind.WORKLOAD, id=SYN_FAX_WORKLOAD,
                            auth_method="synthetic_fax", authorized_tenant_ids=frozenset(
                                {tenant_id}), capabilities=caps),
        tenant_hint=tenant_id, purpose=PURPOSE, capabilities=caps, source=source,
        execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)


def fence_conninfo(runtime: Any, application_name: str) -> str:
    login = runtime.state["logins"]["runtime"]
    return make_conninfo(runtime.endpoint.conninfo(runtime.endpoint.target),
                         user=login["role"], password=login["password"],
                         application_name=f"{application_name}-fax-fence")


@dataclass
class FaxComposition:
    service: FaxService
    fence: fax_ingress.LineFence
    directory: fax_ingress.FaxLineDirectory
    ingress_app: Any
    secret: str
    stack: AsyncExitStack = field(repr=False)
    faults: list[str] = field(default_factory=list)   # TEST-ONLY seam (see FAULTS)
    current_fault: str | None = None

    def __repr__(self) -> str:
        return f"FaxComposition(fence={self.fence!r})"


async def start(runtime: Any, rt: Any, tenant_id: str, application_name: str) -> FaxComposition:
    from fastapi import FastAPI

    service = build_service(rt)
    stack = AsyncExitStack()
    try:
        fence = await stack.enter_async_context(
            fax_ingress.hold_line_fence(fence_conninfo(runtime, application_name)))

        async def lines_for(tenant: str) -> Any:
            return await service.lines(await workload_context(
                rt.resolver, tenant, LINES_CAPABILITIES, TrustedSource.WORKER))

        directory = await fax_ingress.build_directory(fence, (tenant_id,), lines_for)

        async def context_for(tenant: str) -> Any:
            return await workload_context(rt.resolver, tenant, INGRESS_CAPABILITIES,
                                          TrustedSource.HTTP)

        holder: list[FaxComposition] = []

        async def receive(context: Any, fax: Any) -> Any:
            fault = holder[0].current_fault if holder else None
            if fault == "unavailable":
                raise SimulatedUnavailable()
            if fault == "refuse":
                raise FaxRefused("LINE_NOT_REGISTERED")
            return await service.receive(context, fax)

        secret = secrets.token_urlsafe(48)
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(fax_ingress.create_fax_ingress_router(
            fax_ingress.FaxIngressConfig.build(secret), directory, fence, context_for,
            receive))
    except BaseException:
        await stack.aclose()
        raise
    composition = FaxComposition(service=service, fence=fence, directory=directory,
                                 ingress_app=app, secret=secret, stack=stack)
    holder.append(composition)
    return composition


async def stop(composition: FaxComposition | None) -> None:
    if composition is not None:
        await composition.stack.aclose()
