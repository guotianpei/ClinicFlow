"""L-1S package S-G1b: the TEST-SIDE synthetic application harness.

Option A (Codex 20261107T174755Z, `codex_sg1-placement-resolution.md`): the synthetic
registry, catalogue and application composition stay in the test tree. This module is
test-harness evidence only; it is NOT an installable application and NOT coverage of
the production lifespan. L-1S stays open. Interface pinned by
`tests/m05/test_sg_harness_units.py` (S-G1b plan v1 + v1r1).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from psycopg.conninfo import conninfo_to_dict

from haloflow import composition, startup
from haloflow.m01.errors import ActivationRefused
from haloflow.m01.provisioning import installed_state
from haloflow.m05.due_scheduler import SchedulerState, SchedulerStopFailed
from haloflow.m05.synthetic_ingress import (
    IngressConfig,
    IngressConfigInvalid,
    create_ingress_router,
)
from haloflow.startup import Deployment, StartupRefused

LOGGER: Final = logging.getLogger("haloflow.synthetic")

ROUTING_ENV: Final = frozenset({
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE",
    "PGOPTIONS", "PGTARGETSESSIONATTRS", "PGLOADBALANCEHOSTS"})
_DSN_KEYS: Final = frozenset({"host", "port", "dbname", "user", "password"})
_REQUIRED: Final = ("host", "port", "dbname", "user")
_DB_PREFIX: Final = "haloflow_test"
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UNBOUND: Final = "SYNTHETIC_DATABASE_UNBOUND"
IDENTITY_SQL: Final = (
    "SELECT pg_catalog.current_database(), pg_catalog.inet_server_port(),"
    " m.token::pg_catalog.text, m.tenants"
    " FROM (SELECT 1) AS one LEFT JOIN public.haloflow_synthetic_manifest AS m ON true")

Close = Callable[[], Awaitable[None]]


class SyntheticConfigInvalid(Exception):
    def __init__(self, field: str) -> None:
        super().__init__("SYNTHETIC_CONFIG_INVALID", field)
        self.code = "SYNTHETIC_CONFIG_INVALID"
        self.field = field


class ShutdownFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _invalid(field: str) -> SyntheticConfigInvalid:
    return SyntheticConfigInvalid(field)


def _routing(dsn: object) -> tuple[str, str, str]:
    if type(dsn) is not str:
        raise _invalid("database")
    try:
        params = conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - the DSN text is never echoed
        raise _invalid("database") from None
    if not set(params) <= _DSN_KEYS:
        raise _invalid("database")
    if not all(type(params.get(key)) is str and params[key] for key in _REQUIRED):
        raise _invalid("database")
    host, port, dbname = str(params["host"]), str(params["port"]), str(params["dbname"])
    if "," in host or not re.fullmatch(r"[0-9]{1,5}", port):
        raise _invalid("database")
    return host, port, dbname


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticConfig:
    deployment: Deployment
    runtime_dsn: str
    migrator_dsn: str
    provisioner_dsn: str
    host: str
    port: str
    dbname: str
    manifest_token: str
    served_tenants: tuple[str, ...]
    ingress: IngressConfig
    interval_s: int

    @classmethod
    def build(cls, *, deployment: Any, runtime_dsn: Any, migrator_dsn: Any,
              provisioner_dsn: Any, manifest_token: Any, served_tenants: Any,
              ingress_secrets: Any, interval_s: Any = 300,
              environ: Mapping[str, str] | None = None) -> SyntheticConfig:
        if type(deployment) is Deployment and deployment is Deployment.PRODUCTION:
            raise StartupRefused("SYNTHETIC_IN_PRODUCTION")
        if type(deployment) is not Deployment:
            raise _invalid("deployment")
        env = os.environ if environ is None else environ
        if any(name in env for name in ROUTING_ENV):
            raise _invalid("environment")
        routes = {_routing(dsn) for dsn in (runtime_dsn, migrator_dsn, provisioner_dsn)}
        if len(routes) != 1:
            raise _invalid("database")
        ((host, port, dbname),) = routes
        if not dbname.startswith(_DB_PREFIX):
            raise _invalid("database")
        if type(manifest_token) is not str or not _UUID.fullmatch(manifest_token):
            raise _invalid("manifest_token")
        if (type(served_tenants) is not tuple or not served_tenants
                or not all(type(t) is str for t in served_tenants)
                or len(set(served_tenants)) != len(served_tenants)):
            raise _invalid("served_tenants")
        try:
            ingress = IngressConfig.build(ingress_secrets, served_tenants)
        except IngressConfigInvalid:
            raise _invalid("ingress_secrets") from None
        if type(interval_s) is not int or not 60 <= interval_s <= 450:
            raise _invalid("interval_s")
        return cls(deployment, runtime_dsn, migrator_dsn, provisioner_dsn, host, port, dbname,
                   manifest_token, served_tenants, ingress, interval_s)

    def __repr__(self) -> str:
        return f"SyntheticConfig(clinics={len(self.served_tenants)})"

    __str__ = __repr__


# --- database identity guard ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    database: str
    port: int | None
    manifest: tuple[tuple[str, tuple[str, ...]], ...]


def check_identity(config: SyntheticConfig, runtime: Identity, migrator: Identity) -> None:
    for identity in (runtime, migrator):
        if identity.database != config.dbname:
            raise StartupRefused(_UNBOUND)
        if not config.host.startswith("/") and identity.port != int(config.port):
            raise StartupRefused(_UNBOUND)
        if len(identity.manifest) != 1:
            raise StartupRefused(_UNBOUND)
        ((token, tenants),) = identity.manifest
        if token != config.manifest_token:
            raise StartupRefused(_UNBOUND)
        if len(set(tenants)) != len(tenants) or set(tenants) != set(config.served_tenants):
            raise StartupRefused(_UNBOUND)


def _identity_from(rows: Any) -> Identity:
    if type(rows) is not list or not rows:
        raise ValueError
    manifest: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[Any, Any]] = set()
    for row in rows:
        if type(row) is not tuple or len(row) != 4:
            raise ValueError
        database, port, token, tenants = row
        if type(database) is not str or (port is not None and type(port) is not int):
            raise ValueError
        seen.add((database, port))
        if token is None and tenants is None:
            continue
        if type(token) is not str or type(tenants) is not list or not all(
                type(t) is str for t in tenants):
            raise ValueError
        manifest.append((token, tuple(tenants)))
    if len(seen) != 1:
        raise ValueError
    ((database, port),) = seen
    return Identity(database=database, port=port, manifest=tuple(manifest))


async def read_identity(dsn: str, *, connect: Callable[[str], Awaitable[Any]]) -> Identity:
    try:
        connection = await connect(dsn)
    except Exception:  # noqa: BLE001
        raise StartupRefused(_UNBOUND) from None
    try:
        try:
            cursor = await connection.execute(IDENTITY_SQL)
            return _identity_from(await cursor.fetchall())
        except Exception:  # noqa: BLE001 - no query text, DSN or server message kept
            raise StartupRefused(_UNBOUND) from None
    finally:
        await connection.close()


def forbid_provisioning() -> Callable[[], Awaitable[Any]]:
    async def connect() -> Any:
        raise StartupRefused("SYNTHETIC_PROVISIONING_FORBIDDEN")

    return connect


def classify_readiness(error: BaseException | None) -> str:
    if error is None:
        return "active"
    if isinstance(error, ActivationRefused) and type(error) is ActivationRefused and (
            error.reason_code == "M05_NOT_ACTIVATED"):
        return "inactive"
    raise StartupRefused("SYNTHETIC_TENANT_NOT_READY")


def build_static_plan(registry: Any) -> Any:
    """A mirror of `startup._build_static_plan` over any registry."""

    composition.require_installed_state_profiles(registry)
    checks = []
    for unit in registry:
        if not unit.is_typed:
            continue
        requirement = registry.installed_state_requirement(unit.migration_id)
        if not requirement.required or requirement.profile is None:
            raise ValueError("profile-less typed unit")
        profile = requirement.profile
        amended = registry.acl_amendment(unit.migration_id)
        if amended is not None:
            amendment, activator_version = amended
            profile = installed_state.effective_profile(
                profile, amendment, activator_version=activator_version,
                checked_version=registry.target_version)
        checks.append(startup._function_check(unit, profile))
    return startup._StaticPlan(supported=frozenset({registry.target_version}),
                               functions=tuple(checks))


async def close_all(closers: list[Close]) -> bool:
    """`platform.app._close_all` semantics (parity row SG-U14)."""

    failed = False
    cancelled = False
    for close in reversed(closers):
        try:
            await close()
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001
            failed = True
    closers.clear()
    if cancelled:
        raise asyncio.CancelledError()
    return failed


# --- the application -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyntheticSteps:
    guard: Callable[[Any], Awaitable[None]]
    open_tenant_pool: Callable[[Any], Awaitable[tuple[Any, Close]]]
    verify_tenants: Callable[[Any, Any], Awaitable[Any]]
    build_services: Callable[[Any, Any, Any], Awaitable[Any]]
    readiness: Callable[[Any, Any], Awaitable[Mapping[str, str]]]
    open_admission_pool: Callable[[Any], Awaitable[tuple[Any, Close]]]
    open_keyset: Callable[[Any], Awaitable[tuple[Any, Close]]]
    build_verifier: Callable[[Any, Any], Awaitable[Any]]
    build_admission: Callable[[Any], Awaitable[Any]]


SchedulerFactory = Callable[[Any, tuple[str, ...], int], Any]


async def _stop_outcome(scheduler: Any) -> str | None:
    try:
        await scheduler.stop()
    except SchedulerStopFailed:
        return "SCHEDULER_STOP_TIMEOUT"
    except (Exception, asyncio.CancelledError):  # noqa: BLE001 - never a verified stop
        return "SCHEDULER_STOP_UNVERIFIED"
    if scheduler.state is not SchedulerState.STOPPED:
        return "SCHEDULER_STOP_UNVERIFIED"
    return None


def _fail(app: Any, closers: list[Close], code: str, cause: BaseException | None) -> None:
    app.state.retained = tuple(closers)
    LOGGER.error("SHUTDOWN_FAILED reason_code=%s", code)
    raise ShutdownFailed(code) from cause


async def _shutdown(app: Any, scheduler: Any, closers: list[Close],
                    original: BaseException | None) -> None:
    """The one quiescence rule. Always unpublishes first; closes only after a verified
    stop; otherwise retains everything and raises ShutdownFailed."""

    app.state.platform = None
    stop = asyncio.ensure_future(_stop_outcome(scheduler))
    cancelled = False
    while not stop.done():
        try:
            await asyncio.shield(stop)
        except asyncio.CancelledError:
            cancelled = True  # recorded FIRST: a coincident completion must not hide it
            if stop.done():
                break
    code = "SCHEDULER_STOP_UNVERIFIED" if stop.cancelled() else stop.result()
    cause = original if original is not None else (
        asyncio.CancelledError() if cancelled else None)
    if code is not None:
        _fail(app, closers, code, cause)
    failed = await close_all(closers)
    if original is not None:
        raise original
    if cancelled:
        raise asyncio.CancelledError()
    if failed:
        from haloflow.platform.app import StartupCleanupFailed

        raise StartupCleanupFailed()


def _lifespan(config: SyntheticConfig, steps: SyntheticSteps,
              factory: SchedulerFactory) -> Callable[[Any], Any]:
    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        from haloflow.platform.app import PlatformServices

        closers: list[Close] = []
        try:
            await steps.guard(config)
            pool, close = await steps.open_tenant_pool(config)
            closers.append(close)
            validated = await steps.verify_tenants(config, pool)
            services = await steps.build_services(config, pool, validated)
            activation = await steps.readiness(config, services)
            admission_pool, close = await steps.open_admission_pool(config)
            closers.append(close)
            keyset, close = await steps.open_keyset(config)
            closers.append(close)
            verifier = await steps.build_verifier(config, keyset)
            admission = await steps.build_admission(admission_pool)
        except BaseException:
            await close_all(closers)
            raise
        app.state.activation = MappingProxyType(dict(activation))
        app.state.platform = PlatformServices(services=services, admission=admission,
                                              verifier=verifier)
        before = asyncio.all_tasks()
        try:
            scheduler = factory(services.runner, config.served_tenants, config.interval_s)
        except BaseException as error:
            app.state.platform = None
            spawned = asyncio.all_tasks() - before
            if spawned:  # F-H1: live work survives the raise
                app.state.retained_tasks = tuple(spawned)  # strong ownership, not just ids
                _fail(app, closers, "SCHEDULER_FACTORY_UNVERIFIED", error)
            await close_all(closers)
            raise
        spawned = asyncio.all_tasks() - before
        if spawned or getattr(scheduler, "state", None) is not SchedulerState.NEW:
            app.state.platform = None
            app.state.retained_tasks = tuple(spawned)
            _fail(app, closers, "SCHEDULER_FACTORY_UNVERIFIED", None)
        try:
            scheduler.start()
        except BaseException as error:
            await _shutdown(app, scheduler, closers, error)
            raise
        try:
            yield
        except BaseException as error:
            await _shutdown(app, scheduler, closers, error)
            raise
        await _shutdown(app, scheduler, closers, None)

    return lifespan


def create_synthetic_app(config: SyntheticConfig, *, steps: SyntheticSteps,
                         scheduler_factory: SchedulerFactory) -> Any:
    from haloflow.platform.app import create_app

    app = create_app()

    async def context_for(tenant_id: str) -> Any:
        return await app.state.platform.services.ingress_context(tenant_id)

    async def receive(context: Any, message: Any) -> Any:
        return await app.state.platform.services.reply.receive(context, message)

    app.include_router(create_ingress_router(config.ingress, context_for=context_for,
                                             receive=receive))
    app.state.retained = ()
    app.state.retained_tasks = ()
    app.state.activation = None
    app.router.lifespan_context = _lifespan(config, steps, scheduler_factory)
    return app
