"""L-1S package S-H, checkpoint H3 (ReminderScheduler + t075 accepted selector): red-test
support for the PostgreSQL and harness rows.

Governing: H3 package v1 + v1r1 + v1r2 deltas and interface pins v1 (Work Session
2026-11-09; Codex ALIGN for red-test preparation). Extends `sh2_support` (target 74) with
ONE new typed unit, t075 `m05_outbound_accepted_pending`, giving TEST target 75.
Synthetic data only; FakeSender (or a test double of it) only; TEST registry only; not
composed. Every import of H3 code is deferred into a function body (class-B red rule), so
the expected red cause of every row is ModuleNotFoundError for
`haloflow.m05.reminder_scheduler` / `.reminder_scheduler_units` /
`.reminder_scheduler_statements`, never a collection error.

Interface pins used here (the H3 contract, frozen):
- `haloflow.m05.reminder_scheduler_units`: SH3_TENANT_MIGRATIONS (t075 only) and
  SH3_INSTALLED_STATE_PROFILES. No ACL amendment.
- `haloflow.m05.reminder_scheduler_statements`: ACCEPTED_PENDING_KEY =
  "m05.outbound.accepted_pending", SH3_STATEMENTS, SH3_PROTECTED = frozenset().
- `haloflow.m05.reminder_scheduler`: ReminderSchedulerService(ReminderService) with
  `accepted_pending(context, limit, after)`; ReminderScheduler(*, contexts, service, worker,
  runner, tenants, interval_s, stop_timeout_s, recovery_limit, clock, sleep).
- t075 command keys {attribution, after, limit, schema_version}; limit 1..1000; after null
  or {updated_at, op_id}; result {schema_version: 1, ops: [{op_id, updated_at}]}.

DISCLOSED test-side choices (no source change, no invented API):
- "t052 invocations" are counted at the service seam: `Recording.register` is the one
  method through which OutboundWorker reaches t052 (recover and run_claimed both go
  through `_register` -> `service.register`). pg_stat_user_functions is not used by any
  existing suite, so it is not used here.
- `persisted_accepted_op` reproduces "crash after outcome=accepted, before register" with
  the real runtime statements: examine (claim) -> consume (ticket spent) -> t050 outcome
  accepted, and nothing else. The provider call itself is simulated (no sender exists in
  SQL); the op is left exactly where a worker that died after recording the outcome
  leaves it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

import m02_support
import sa_support as sa
import sc_support as sc
import sh2_support as sh2
import sh_support as sh

SH3_UNIT_IDS: Final = ("t075_m05_outbound_accepted_pending",)
SH3_TARGET: Final = 75
SH3_SUPPORTED: Final = range(SH3_TARGET, SH3_TARGET + 1)
PENDING_FUNCTION: Final = "m05_outbound_accepted_pending"
PENDING_KEY: Final = "m05.outbound.accepted_pending"
HELD_UNIT: Final = "t056_m05_outbound_held"
HELD_FUNCTION: Final = "m05_outbound_held"
RECOVERY_KEYS: Final = ("registered", "existing", "held", "refused_activation",
                        "refused_capability", "refused_not_accepted", "refused_other",
                        "commit_unknown", "failed")
READY_KEYS: Final = ("examined", "claimed", "sent")
BOUND_S: Final = 60.0  # every await on the database is bounded (rows never hang)
LOGGER: Final = "haloflow.m05.reminder_scheduler"


# --- registry, catalogue, install ---------------------------------------------------------


def sh3_units() -> Any:
    from haloflow.m05.reminder_scheduler_units import SH3_TENANT_MIGRATIONS

    return SH3_TENANT_MIGRATIONS


def sh3_profiles() -> Any:
    from haloflow.m05.reminder_scheduler_units import SH3_INSTALLED_STATE_PROFILES

    return SH3_INSTALLED_STATE_PROFILES


def sh3_statements() -> Any:
    from haloflow.m05.reminder_scheduler_statements import SH3_STATEMENTS

    return SH3_STATEMENTS


def sh3_protected() -> frozenset[str]:
    from haloflow.m05.reminder_scheduler_statements import SH3_PROTECTED

    return frozenset(SH3_PROTECTED)


def sh3_registry() -> Any:
    """The H2 registry inputs plus the SH3 unit and profile (no new amendment)."""

    import sb_support as sb

    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, sa.sa_units(), sb.sb_units(), sc.sc_units(),
        sh.sh_units(), sh2.sh2_units(), sh3_units(),
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **sa.sa_profiles(),
                                  **sb.sb_profiles(), **sc.sc_profiles(),
                                  **sh.sh_profiles(), **sh2.sh2_profiles(),
                                  **sh3_profiles()},
        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *sh.sh_amendments(),
                        *sh2.sh2_amendments()),
    )


def sh3_catalog() -> Any:
    """The H2 TEST catalogue plus SH3_STATEMENTS; the protected set is the H2 one united
    with SH3_PROTECTED (empty: t075 is bookkeeping, never activation-protected)."""

    from types import MappingProxyType

    import sb_support as sb

    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import build_statement_catalog
    from haloflow.m05.intake_statements import DIRECTORY_KEY

    sa_part = MappingProxyType({k: v for k, v in sh.sa_statements_without_inbound().items()
                                if k != DIRECTORY_KEY})
    sh_part = MappingProxyType({k: v for k, v in sh.sh_statements().items()
                                if k not in sh2.CLOSED_KEYS()})
    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, sa_part, sb.sb_statements(),
                                   sh_part, sh.sh_inbound_statements(), sh2.sh2_statements(),
                                   sh2.sh2_directory_statements(), sh3_statements(),
                                   activation_protected=sh2.protected() | sh3_protected())


class SH3Install(sh2.SH2Install):
    """The H2 install shape, provisioned through the H3 registry (target 75). After
    install, FIXTURE: the clinic display name is set (the seeded default is NULL)."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, sh3_registry(), self.tenant,
                                           supported=SH3_SUPPORTED, manifest=staff_manifest())
                if self.profile:
                    sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                    sh2.settings_raw(self, clinic_display_name=sh2.CLINIC)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"S-H H3 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[SH3Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield SH3Install(ids, tenant)
    finally:
        reset(tenant)


def ready_install(install: SH3Install) -> SH3Install:
    """Row-body install (class-B red rule), then the clinic explicitly granted."""

    install.require()
    sc.change(install, "grant")
    return install


@asynccontextmanager
async def runtime(install: SH3Install, *, policy: str = "ENFORCE",
                  max_size: int = 4) -> AsyncIterator[Any]:
    """An H3 TEST runtime (target 75, H3 catalogue, real resolver and gateway). Not bound
    to one tenant: the resolver serves every tenant the control store knows."""

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
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=sh3_registry(),
            catalog=sh3_catalog(),
            supported_schema_versions=frozenset({SH3_TARGET}),
            activation_policy=ActivationPolicy[policy],
        )
    finally:
        await pool.close()


# --- contexts -------------------------------------------------------------------------------


async def dispatch_context(rt: Any, install: sa.SAInstall) -> Any:
    import service_support as sv

    return await sv.context(rt, install, sa.W_DISPATCH, sa.CAPS["dispatch"], kind="workload")


Contexts = Callable[[str], Awaitable[Any]]


def dispatch_contexts(rt: Any, installs: Iterable[sa.SAInstall], *,
                      denied: Iterable[str] = ()) -> Contexts:
    """The scheduler's `contexts(tenant)` callable: the dispatch workload's context for a
    served tenant. A tenant in `denied` is resolved by a principal that does NOT hold the
    dispatch capabilities, so the real resolver refuses it with
    TenantDenied(CAPABILITY_DENIED) (capability admission fails in the context phase)."""

    by_id = {inst.tenant_id: inst for inst in installs}
    refused = frozenset(denied)

    async def contexts(tenant: str) -> Any:
        install = by_id[tenant]
        if tenant not in refused:
            return await dispatch_context(rt, install)
        from haloflow.m01.context import (
            CorrelationSource,
            Principal,
            PrincipalKind,
            TrustedSource,
        )

        principal = Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_DISPATCH,
                              auth_method="test", authorized_tenant_ids=frozenset({tenant}),
                              capabilities=frozenset(sa.CAPS["inbound"]))
        return await rt.resolver.resolve(
            principal=principal, tenant_hint=tenant, purpose="operations",
            capabilities=frozenset(sa.CAPS["dispatch"]), source=TrustedSource.WORKER,
            execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

    return contexts


# --- service / worker / sender doubles (subclasses of the real classes) --------------------


Hook = Callable[[dict[str, Any]], Awaitable[None]]


def service(rt: Any, *, after_examine: Hook | None = None,
            register_acks: Iterable[str] | None = None) -> Any:
    """A real ReminderSchedulerService on the runtime gateway that records the calls the
    rows count (t052 via `register`, t075 via `accepted_pending`, t051 via `expire`,
    examine / consume). `after_examine` runs after a committed examine (test barrier)."""

    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.reminder_scheduler import ReminderSchedulerService

    class Recording(ReminderSchedulerService):
        def __init__(self, foundation: Any) -> None:
            super().__init__(foundation)
            self.log: list[tuple[Any, ...]] = []

        async def register(self, context: Any, op_id: Any) -> dict[str, Any]:
            """`register_acks` scripts each call (FIXTURE, lost-acknowledgement seam):
            "ok" = the real register; "lose" = the real register COMMITS, then its
            acknowledgement is lost (OutboundCommitUnknown); "trap" = a further call is a
            test failure. With no script every call is "ok"."""

            from haloflow.m05.outbound import OutboundCommitUnknown

            self.log.append(("register", str(op_id)))
            mode = next(acks, "trap") if acks is not None else "ok"
            if mode == "trap":
                raise AssertionError("register invoked beyond the scripted ceiling")
            result: dict[str, Any] = await super().register(context, op_id)
            self.log.append(("register_result", str(op_id), result.get("outcome")))
            if mode == "lose":
                raise OutboundCommitUnknown()
            return result

        async def accepted_pending(self, context: Any, limit: int, after: Any
                                   ) -> dict[str, Any]:
            self.log.append(("accepted_pending", limit, None if after is None
                             else tuple(after)))
            result: dict[str, Any] = await super().accepted_pending(context, limit, after)
            return result

        async def expire(self, context: Any, limit: int = 100) -> dict[str, Any]:
            self.log.append(("expire", limit))
            result: dict[str, Any] = await super().expire(context, limit)
            return result

        async def examine(self, context: Any, appointment_ref: Any, **kw: Any
                          ) -> dict[str, Any]:
            result: dict[str, Any] = await super().examine(context, appointment_ref, **kw)
            self.log.append(("examine", str(appointment_ref), result.get("outcome")))
            if after_examine is not None:
                await after_examine(result)
            return result

        async def consume(self, context: Any, op_id: Any, attempt_id: Any
                          ) -> dict[str, Any]:
            self.log.append(("consume", str(op_id)))
            result: dict[str, Any] = await super().consume(context, op_id, attempt_id)
            return result

        def calls(self, name: str) -> list[tuple[Any, ...]]:
            return [entry for entry in self.log if entry[0] == name]

    acks = iter(tuple(register_acks)) if register_acks is not None else None
    return Recording(EventFoundation(rt.gateway, producer_service=sa.PRODUCER))


def worker(svc: Any, sender: Any, *, fail_ops: Iterable[str] = ()) -> Any:
    """The real OutboundWorker (render = the H2 `render_inputs`). DISCLOSED double for
    P05: `fail_ops` makes `recover` raise for those op ids BEFORE any statement (there is
    no per-row, persistent, database-side way to make t052 fail for one accepted op: every
    D1 change converts the op to accepted_unregistered, which leaves the selector)."""

    from haloflow.m05.outbound import OutboundWorker
    from haloflow.m05.reminder import render_inputs

    failing = frozenset(str(op) for op in fail_ops)

    class Worker(OutboundWorker):
        async def recover(self, context: Any, op_id: Any) -> Any:
            if str(op_id) in failing:
                raise RuntimeError("synthetic persistent recovery failure")
            return await super().recover(context, op_id)

    return Worker(svc, sender, render_inputs)


@dataclass(eq=False)
class BlockingSender:
    """A sender that blocks inside `send` until released (bounded by the row). Records
    every call like FakeSender; a cancellation propagates (the worker records
    SENDER_CANCELLED and re-raises)."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[tuple[str, str, str]] = field(default_factory=list, repr=False)

    async def send(self, op_id: str, phone: str, text: str) -> Any:
        from haloflow.m05.outbound import Accepted

        self.calls.append((op_id, phone, text))
        self.entered.set()
        await self.release.wait()
        return Accepted("fake-h3-blocking")

    def __repr__(self) -> str:
        return f"BlockingSender(calls={len(self.calls)})"


@dataclass
class Rig:
    scheduler: Any
    service: Any
    worker: Any
    runner: Any
    sender: Any

    async def tick(self) -> Any:
        return await asyncio.wait_for(self.scheduler.tick(), BOUND_S)

    async def stop(self) -> None:
        await asyncio.wait_for(self.scheduler.stop(), BOUND_S)


def rig(rt: Any, installs: Iterable[sa.SAInstall], *, sender: Any = None,
        contexts: Contexts | None = None, fail_ops: Iterable[str] = (),
        after_examine: Hook | None = None, register_acks: Iterable[str] | None = None,
        recovery_limit: int = 100, batch: int = 10,
        interval_s: int = 300, stop_timeout_s: int = 10,
        tenants: tuple[str, ...] | None = None) -> Rig:
    """One ReminderScheduler wired to the real service/worker/runner on `rt` (unstarted;
    default clock and sleep). Rows drive it with `tick()` or `start()`/`stop()`."""

    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.reminder import ReminderRunner
    from haloflow.m05.reminder_scheduler import ReminderScheduler

    installs = tuple(installs)
    sender = sender if sender is not None else FakeSender()
    svc = service(rt, after_examine=after_examine, register_acks=register_acks)
    wrk = worker(svc, sender, fail_ops=fail_ops)
    runner = ReminderRunner(svc, wrk, batch=batch, lease_seconds=120)
    scheduler = ReminderScheduler(
        contexts=contexts or dispatch_contexts(rt, installs), service=svc, worker=wrk,
        runner=runner, tenants=tenants or tuple(i.tenant_id for i in installs),
        interval_s=interval_s, stop_timeout_s=stop_timeout_s, recovery_limit=recovery_limit)
    return Rig(scheduler, svc, wrk, runner, sender)


def sent_ops(sender: Any) -> list[str]:
    return [str(call[0]) for call in sender.calls]


def report(result: Any, install: sa.SAInstall) -> Any:
    (found,) = [t for t in result.tenants if t.tenant == install.tenant_id]
    return found


def zero_recovery(**overrides: int) -> dict[str, int]:
    counts = dict.fromkeys(RECOVERY_KEYS, 0)
    counts.update(overrides)
    return counts


def ready_counts(examined: int = 0, claimed: int = 0, sent: int = 0) -> dict[str, int]:
    return {"examined": examined, "claimed": claimed, "sent": sent}


# --- op states on an H3 tenant (real runtime statements; FIXTURE only where named) ---------


def sendable(install: SH3Install, **kw: Any) -> tuple[str, str | None, str, str]:
    """A due, in-window, sendable reminder (directory v2): (patient, phone, ref, zone)."""

    zone, start = sh2.in_window(install)
    patient, phone, ref = sh2.seed(install, start, zone=zone, **kw)
    return patient, phone, ref, zone


def persisted_accepted_op(install: SH3Install) -> tuple[str, str]:
    """The crash state (DISCLOSED): examine -> consume (ticket spent) -> outcome accepted,
    then the worker "dies" before register. Returns (op_id, ref); the schedule row is gone
    (examine claimed it), so only the recovery phase can reach this op."""

    _p, _ph, ref, _zone = sendable(install)
    op_id, attempt = sh2.claimed(install, ref)
    assert sh2.consume(install, op_id, attempt)["outcome"] == "send"
    sh.outcome(install, op_id, attempt, "accepted",
               provider_ref="fake-h3-" + uuid.uuid4().hex[:12])
    assert sh.op_row(install, op_id)["state"] == "accepted"
    return op_id, ref


def op_in_state(install: SH3Install, state: str) -> tuple[str, str]:
    """An op in `state` on a fresh ref: accepted / registered / accepted_unregistered /
    uncertain / refused / attempting / expired (an attempting op whose lease passed; FIXTURE
    time travel, the H1 P08 pattern) / prepared (owner-internal t065 FIXTURE)."""

    if state == "prepared":
        _p, _ph, ref, _zone = sendable(install)
        return str(sh2.prepare_rendered(install, ref)["op_id"]), ref
    if state in ("accepted", "registered", "accepted_unregistered"):
        op_id, ref = persisted_accepted_op(install)
        if state == "registered":
            assert sh.register(install, op_id)["outcome"] == "registered"
        elif state == "accepted_unregistered":
            row = sh.op_row(install, op_id)
            start = sh.start_of(install, ref)
            sh2.appointment_put(install, ref, row["patient_id"], start + timedelta(days=1),
                                department_tz=sh2.in_window(install)[0])
            assert sh.register(install, op_id)["outcome"] == "held"
        assert sh.op_row(install, op_id)["state"] == state
        return op_id, ref
    _p, _ph, ref, _zone = sendable(install)
    op_id, attempt = sh2.claimed(install, ref)
    if state in ("attempting", "expired"):
        if state == "expired":
            sh2.expire_lease(install, op_id)
        return op_id, ref
    assert sh2.consume(install, op_id, attempt)["outcome"] == "send"
    code = {"uncertain": "FAKE_TIMEOUT", "refused": "FAKE_REFUSED"}[state]
    sh.outcome(install, op_id, attempt, state, code=code)
    assert sh.op_row(install, op_id)["state"] == state
    return op_id, ref


def dispatch_events(install: sa.SAInstall, op_id: str) -> int:
    """M02 dispatch observations for the op's reminder (the H1 P21e counter)."""

    reminder = str(sh.op_row(install, op_id)["reminder_id"])
    rows = sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.patient_events AS e"
                                           " WHERE e.correlation_id = %s"), (reminder,))
    count: int = rows[0][0]
    return count


def op_state(install: sa.SAInstall, op_id: str) -> tuple[str, str | None]:
    row = sh.op_row(install, op_id)
    return str(row["state"]), row["code"]


def ops_with_xmin(install: sa.SAInstall) -> list[tuple[str, str, str]]:
    """Every op row with its xmin: a pure read leaves both byte-identical."""

    rows = sa.admin(install, sa.q(install, "SELECT o.op_id::text, o.xmin::text,"
                                           " to_jsonb(o)::text FROM {s}.m05_outbound_ops AS o"
                                           " ORDER BY o.op_id"))
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def set_updated_at(install: sa.SAInstall, op_ids: Iterable[str], instant: str) -> None:
    """FIXTURE: give ops one identical updated_at (the tie-order discriminator)."""

    sa.admin(install, sa.q(install, "UPDATE {s}.m05_outbound_ops SET updated_at = %s::timestamptz"
                                    " WHERE op_id = ANY(%s::uuid[])"),
             (instant, list(op_ids)))


# --- the t075 command -----------------------------------------------------------------------


def pending_cmd(limit: Any = 100, after: Any = None, *, principal: str = sa.W_DISPATCH,
                **extra: Any) -> dict[str, Any]:
    return {"schema_version": 1, "limit": limit, "after": after,
            "attribution": sa.attribution(principal), **extra}


def pending(install: sa.SAInstall, limit: int = 1000, after: Any = None) -> list[dict[str, Any]]:
    result = sa.call(install, PENDING_FUNCTION, pending_cmd(limit, after))
    assert set(result) == {"schema_version", "ops"} and result["schema_version"] == 1, result
    ops: list[dict[str, Any]] = result["ops"]
    return ops


def cursor(row: dict[str, Any]) -> dict[str, Any]:
    return {"updated_at": row["updated_at"], "op_id": row["op_id"]}


# --- S-G1a synthetic HTTP ingress, in process (harness rows) --------------------------------


INGRESS_ROUTE: Final = "/v1/synthetic/inbound-sms"


def ingress_app(rt: Any, secrets: dict[str, str]) -> Any:
    """The UNCHANGED S-G1a router (`create_ingress_router`) on a bare FastAPI app bound to
    the H3 runtime: secret -> clinic; ingress workload context; ReplyIntake (classifier)
    -> IntakeService (v2 inbound via the same-key binding). DISCLOSED: the S-G1b
    `create_synthetic_app` composes the S-C registry (target 46) and cannot serve an H3
    tenant without a harness change, so only its ingress route is reused."""

    from fastapi import FastAPI

    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.intake import IntakeService
    from haloflow.m05.reply_classifier import ReplyIntake
    from haloflow.m05.synthetic_ingress import IngressConfig, create_ingress_router

    config = IngressConfig.build(secrets, tuple(secrets))
    reply = ReplyIntake(IntakeService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER)))
    caps = frozenset(sa.CAPS["inbound"])

    async def context_for(tenant_id: str) -> Any:
        return await rt.resolver.resolve(
            principal=Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_INGRESS,
                                auth_method="synthetic_ingress",
                                authorized_tenant_ids=frozenset({tenant_id}), capabilities=caps),
            tenant_hint=tenant_id, purpose="operations", capabilities=caps,
            source=TrustedSource.HTTP, execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

    app = FastAPI()
    app.include_router(create_ingress_router(config, context_for=context_for,
                                             receive=reply.receive))
    return app


async def post_reply(app: Any, secret: str, phone: str, text: str,
                     message_id: str | None = None) -> tuple[int, dict[str, Any]]:
    """One HTTP POST through the ASGI interface, in process. DISCLOSED: the S-A side-effect
    guard (sa_guard) blocks httpx.AsyncClient.send, so the request is delivered to the
    ASGI app directly (same route, headers, body and status as S-G1b's ASGITransport)."""

    body = json.dumps({"channel": "sms", "provider_message_id": message_id or sa.new_id(),
                       "phone": phone, "text": text}).encode("utf-8")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": INGRESS_ROUTE,
             "raw_path": INGRESS_ROUTE.encode("ascii"), "root_path": "", "query_string": b"",
             "headers": [(b"authorization", f"Bearer {secret}".encode("ascii")),
                         (b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode("ascii"))],
             "client": ("synthetic", 1), "server": ("synthetic", 80)}
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), BOUND_S)
    status = next(int(m["status"]) for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, (json.loads(raw) if raw else {})


async def ingest(rt: Any, install: sa.SAInstall, *, patient: str | None = None,
                 phone: str | None = None, contact: bool = True,
                 first_name: str = sh2.FIRST_NAME) -> tuple[str, str, str]:
    """Synthetic directory ingestion through the REBOUND gateway key (m05_directory_v2):
    contact (optional) + one booked, due, in-window appointment. (patient, phone, ref)."""

    zone, start = sh2.in_window(install)
    patient = patient or sa.new_id()
    phone = phone or sa.fresh_phone()
    ref = sa.new_id()
    if contact:
        result = await sh2.gateway_directory(rt, install, "contact_put", phone=phone,
                                             patient_id=patient)
        assert result["outcome"] in ("applied", "existing"), result
    result = await sh2.gateway_directory(rt, install, "appointment_put",
                                         **sh2.appointment_fields(ref, patient, start,
                                                                  department_tz=zone,
                                                                  first_name=first_name))
    assert result["outcome"] == "applied", result
    return patient, phone, ref


def window_of(install: sa.SAInstall, ref: str) -> dict[str, Any]:
    windows = sh.windows_for(install, ref)
    assert len(windows) == 1, windows
    return windows[0]


def reply_row(install: sa.SAInstall, message_id: str) -> dict[str, Any]:
    rows = sa.admin(install, sa.q(install, "SELECT to_jsonb(r) FROM {s}.m05_inbound_replies AS r"
                                           " WHERE provider_message_id = %s"), (message_id,))
    assert len(rows) == 1, rows
    value: dict[str, Any] = rows[0][0]
    return value


def appointment_rows(install: sa.SAInstall, ref: str) -> tuple[Any, Any]:
    appt = sa.admin(install, sa.q(install, "SELECT to_jsonb(a) FROM {s}.m05_appointments AS a"
                                           " WHERE appointment_ref = %s"), (ref,))
    return appt, sh2.profile_row(install, ref)
