"""Incoming fax routing FX-IN, package FX3a — two-tenant harness support (TEST ONLY). RED BY DESIGN.

Governing (Shared Workspace `Work Session 2026-11-25/`): test cases v1r1 §0/§3 (Codex ALIGN
19629e35),
FX3 split and harness scope (Codex ALIGN e9c224a9, `codex_fx3a-scope-review.md`), FX1 at 750dc5f
(target 140), FX2 at 1349b8d (target 144), OD-FX1..FX9. Synthetic only; no provider signature is
claimed (the S-G1 disclaimer). The HTML screen is FX3b (after Rachel's mock review).

Red classes: C control; A absence-red (the FX3a modules do not exist: ModuleNotFoundError /
TypeError for `include_fx3`).

FX3a interface pinned here (red packet v1, section 2)
==========================================================================================
`haloflow.m05.fax_statements` (new):
  CAPABILITY_INGRESS = "m05.fax.ingress"     held ONLY by the fax ingress workload context
  CAPABILITY_LINES   = "m05.fax.lines"       held ONLY by the trusted startup context
  RECEIVE_KEY = "m05.fax.receive"  WRITE  CAPABILITY_INGRESS  SELECT m05_fx_receive(%(command)s)
  LINES_KEY   = "m05.fax.lines"    READ   CAPABILITY_LINES
                  SELECT m05_fx_lines_snapshot(%(command)s)
  READ_KEY    = "m05.fax.read"     READ   m05.followup.read   SELECT m05_fx_staff_read(%(command)s)
  OPEN_KEY    = "m05.fax.open"     WRITE  m05.followup.read   SELECT m05_fx_staff_action(
                  %(command)s OPERATOR(pg_catalog.||) '{"action": "open"}'::jsonb)
                  -- the operation is FIXED to open on the SQL side; a caller cannot override it
                  (Codex e9c224a9)
  ACT_KEY     = "m05.fax.act"      WRITE  m05.followup.apply
                  SELECT m05_fx_staff_action(%(command)s)
                  -- held only with route `act` (prepare+apply), so only staff with read AND act
  FX3_STATEMENTS: those five keys. Catalogue: `build_synthetic_catalog(<EL flags>, include_fx1,
  include_fx2, include_fx3=True)`; include_fx3 without include_fx2 -> ValueError("...FX3...").
  The registry is FX2's (FX3a adds NO unit; target stays 144).
`haloflow.m05.fax` (new): `FaxService(foundation: EventFoundation)`:
  INGRESS_CAPABILITIES = frozenset({CAPABILITY_INGRESS}); LINES_CAPABILITIES = {CAPABILITY_LINES}
  async receive(ctx, fax: InboundFax) -> dict   workload ctx with EXACTLY INGRESS_CAPABILITIES
  async lines(ctx) -> tuple[str, ...]           workload ctx with EXACTLY LINES_CAPABILITIES
  async list(ctx, queue_id: UUID | None) / get(ctx, fax_id) / history(ctx, fax_id) -> dict
  async open(ctx, fax_id) -> dict               (OPEN_KEY)
  async act(ctx, action: FaxAction) -> dict      (ACT_KEY; FaxAction.action in move|file|sort)
  InboundFax(provider, provider_fax_id, line, sender, pages, received_at, doc_ref) frozen;
  FaxAction(action, fax_id, expected_version, queue_id) frozen.
  Refusals: FaxRefused(code) for the gateway's P0001 codes (FAX_NOT_FOUND, ACTOR_INACTIVE, ...);
  FaxRefused("CAPABILITIES_INVALID") / ("PRINCIPAL_NOT_WORKLOAD") from the admit checks;
  the runtime's CapabilityDenied passes through unchanged.
`haloflow.m05.fax_ingress` (new; test-side router, NOT mounted by any app — S-G1 placement):
  ROUTE = "/v1/synthetic/inbound-fax"; LINE_HEADER = "x-synthetic-receiving-line";
  MAX_BODY_BYTES = 4096; FORBIDDEN_FIELDS = ("line", "to", "to_number", "receiving_line")
  FaxIngressConfig.build(secret: str) (32..505 printable bytes, else FaxIngressConfigInvalid)
  FaxLineDirectory.build(lines_by_tenant: Mapping[str, Sequence[str]]) -> FaxLineDirectory;
    a line under two tenants -> LineDirectoryInvalid("LINE_DUPLICATE"); .lookup(line) -> str|None
  hold_line_fence(conninfo) -> async context manager yielding LineFence: a dedicated connection
    holding pg_advisory_lock_shared(K_FX_LINES); `await fence.ensure()` probes that connection
    (SELECT 1) and returns False once it is lost; `.alive` is the last probe's result (False is
    final for that fence)
  async build_directory(fence, tenants, lines_for) -> FaxLineDirectory   (directory built only
    while the fence is alive; lines_for(tenant) = FaxService.lines through a LINES context)
  create_fax_ingress_router(config, directory, fence, context_for, receive, *, after_lookup=None)
    order: `await fence.ensure()` at admission (False -> 503 FENCE_LOST) -> bearer (401
    UNAUTHORIZED) -> media type
    (415) -> capped body read regardless of Content-Length (413) -> strict JSON body: exactly
    provider, provider_fax_id, sender, pages, received_at, doc_ref; any FORBIDDEN_FIELDS member
    -> 400 BODY_FIELD_FORBIDDEN (field), other unknown/missing/invalid -> 400 MALFORMED (field)
    -> header line canonical E.164 (400 MALFORMED line) -> directory lookup (404 LINE_UNKNOWN; NO
    context_for/receive call) -> `after_lookup()` (test seam, I08) -> context_for(tenant) ->
    receive(ctx, InboundFax) -> 200 {"outcome", "fax_id"}; FaxRefused LINE_NOT_REGISTERED -> 409
    {"code": "LINE_NOT_REGISTERED"}. No sender, line, doc_ref or provider id in any response or
    log record.
`haloflow.synthetic.fax_staff_routes` (new; red v1r1, Codex f56c079 review H04): the staff JSON
  routes the synthetic app mounts, in the `staff_routes` pattern: PREFIX = "/synthetic/v1/fax";
  `install_fax_staff_routes(app, lifecycle)` adds GET {PREFIX}/faxes (list, ?queue=), GET
  {PREFIX}/faxes/{id} (get), GET {PREFIX}/faxes/{id}/history, POST {PREFIX}/faxes/{id}/open (get
  requirement) and POST {PREFIX}/faxes/{id}/actions (act requirement); every request uses
  `lifecycle.services` = the clinic's fixed principal (tenant_id, staff_id) with the production
  two-step resolution (self standing -> capabilities_for -> route_capabilities) and exactly one
  `services.fax` (FaxService) call; refusals through production's map plus FaxRefused
  FAX_NOT_FOUND / QUEUE_NOT_FOUND -> 404 NOT_FOUND, state codes -> 409 CONFLICT {"code"};
  labelled bodies. CSRF stays the app's (install row FX3-H02).
`haloflow.synthetic.wiring.SyntheticServices` gains `fax: Any = None` (the fax profile's
  FaxService; other profiles leave it None).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, Final

import fx1_support as fx
import fx2_support as f2
import m02_support
import sa_support as sa

FX3_TARGET: Final = 144
FX3_SUPPORTED: Final = range(FX3_TARGET, FX3_TARGET + 1)
SECRET: Final = "s" * 40
READ_CAPS: Final = frozenset({"m05.followup.read", "m05.followup.note.read", "m01.staff.display"})
ACT_CAPS: Final = READ_CAPS | frozenset({"m05.followup.prepare", "m05.followup.apply",
                                         "m02.observation.append"})


def m(name: str, *, package: str = "haloflow.m05") -> Any:
    """Lazy `<package>.<name>` import inside a row body (class-A red rule)."""
    import importlib

    return importlib.import_module(f"{package}.{name}")


def fx3_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True, include_fx2=True,
                                   include_fx3=True)  # type: ignore[call-arg]


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str
                   ) -> Iterator[f2.FX2Install]:
    yield from f2.module_install(request, ids, reset, label)


def ready(install: f2.FX2Install, *lines: str) -> f2.FX2Install:
    install.require()
    for line in lines:
        fx.seed_line(install, line)
    return install


@asynccontextmanager
async def runtime(install: f2.FX2Install, *, max_size: int = 6) -> AsyncIterator[Any]:
    """A composed TEST runtime over the FX2 registry (144) and the FX3 catalogue (real resolver
    and gateway); one runtime serves every tenant of the test database."""
    import service_support as sv

    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=max_size)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"])),
            registry=f2.fx2_registry(), catalog=fx3_catalog(),
            supported_schema_versions=frozenset({FX3_TARGET}),
            activation_policy=ActivationPolicy["ENFORCE"])
    finally:
        await pool.close()


async def ctx(rt: Any, install: sa.SAInstall, principal: str, caps: frozenset[str], *,
              kind: str = "actor") -> Any:
    import service_support as sv

    return await sv.context(rt, install, principal, caps, kind=kind)


def service(rt: Any) -> Any:
    from haloflow.m02.observation import EventFoundation

    return m("fax").FaxService(EventFoundation(rt.gateway, producer_service="m05.fax"))


async def ingress_ctx(rt: Any, install: sa.SAInstall) -> Any:
    return await ctx(rt, install, fx.W_FAX, m("fax").INGRESS_CAPABILITIES, kind="workload")


async def lines_ctx(rt: Any, install: sa.SAInstall) -> Any:
    return await ctx(rt, install, fx.W_FAX, m("fax").LINES_CAPABILITIES, kind="workload")


def body(**override: Any) -> dict[str, Any]:
    fid = override.pop("provider_fax_id", f"FX-{uuid.uuid4().hex[:16]}")
    out = {"provider": fx.PROVIDER, "provider_fax_id": fid, "sender": None, "pages": 1,
           "received_at": fx.received_at(), "doc_ref": f"syn-doc-{fid[-8:]}"}
    out.update(override)
    return out


def request(app: Any, payload: bytes, *, line: str | None = fx.LINE1,
            secret: str | None = SECRET, content_type: str = "application/json",
            content_length: str | None = None, lose_ack: bool = False) -> Any:
    """One ASGI POST to the fax ingress route (returns (status, json)).

    lose_ack=True simulates the provider losing the acknowledgement AFTER the receipt committed:
    the transport fails when the app sends its response start (the gateway transaction has
    already committed by then); returns (None, None) and the provider must retry (I07)."""
    import json

    route = m("fax_ingress").ROUTE
    headers: list[tuple[bytes, bytes]] = [(b"content-type", content_type.encode())]
    if secret is not None:
        headers.append((b"authorization", b"Bearer " + secret.encode()))
    if line is not None:
        headers.append((m("fax_ingress").LINE_HEADER.encode(), line.encode()))
    headers.append((b"content-length",
                    (content_length or str(len(payload))).encode()))

    async def run() -> tuple[int | None, Any]:
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": route,
                 "raw_path": route.encode(), "root_path": "", "query_string": b"",
                 "headers": headers, "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0)}
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        messages: list[dict[str, Any]] = []

        class AckLost(ConnectionResetError):
            pass

        async def send(message: dict[str, Any]) -> None:
            if lose_ack and message["type"] == "http.response.start":
                raise AckLost("synthetic provider lost the acknowledgement")
            messages.append(message)

        try:
            await app(scope, receive, send)
        except AckLost:
            return None, None
        except BaseExceptionGroup as group:  # anyio task groups may wrap the transport failure
            if group.subgroup(AckLost) is None:
                raise
            return None, None
        status = next(int(x["status"]) for x in messages if x["type"] == "http.response.start")
        raw = b"".join(x.get("body", b"") for x in messages if x["type"] == "http.response.body")
        return status, (json.loads(raw) if raw else {})

    return run()


def staff_request(app: Any, method: str, target: str, value: Any = None) -> Any:
    """One ASGI request to a staff route (returns (status, json)); POSTs carry a JSON body."""
    import json

    path, _, query = target.partition("?")
    payload = b"" if value is None else json.dumps(value).encode()
    headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1")]
    if method == "POST":
        headers += [(b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode())]

    async def run() -> tuple[int, Any]:
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path,
                 "raw_path": path.encode(), "root_path": "", "query_string": query.encode(),
                 "headers": headers, "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0)}
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await app(scope, receive, send)
        status = next(int(x["status"]) for x in messages if x["type"] == "http.response.start")
        raw = b"".join(x.get("body", b"") for x in messages if x["type"] == "http.response.body")
        return status, (json.loads(raw) if raw else {})

    return run()
