"""The platform ASGI app (L-1W W4a): lifespan, readiness, admission, logout.

Launch only as ``uvicorn haloflow.platform.app:app --lifespan on`` (architecture v2
A1). Startup order (``StartupSteps``): tenant pool -> production services (B5
``start_production_services``) -> admission pool -> key-set client -> verifier ->
admission adapter; then ``PlatformServices`` is published on ``app.state.platform``.
Shutdown unpublishes first (the gate refuses from that moment), then closes in reverse.

Cleanup (addendum 1 D3): every resource opened so far is closed in reverse order; a
close failure is recorded and never raised over an original failure; cancellation
wins; a close failure with no other failure raises ``StartupCleanupFailed``.

A refused startup logs one ERROR line on the ``haloflow.platform`` logger:
``STARTUP_REFUSED reason_code=<code>`` (``... field=<VARIABLE>`` for configuration),
with no tenant id, conninfo or driver text.

W4a routes: ``GET /healthz`` and ``POST /v1/session/logout``. The W4b tenant routes
are installed from ``haloflow.platform.routes``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Final

import httpx
from fastapi import Depends, FastAPI, Request
from psycopg import AsyncConnection
from starlette.responses import JSONResponse, Response

from haloflow.platform.admission import (
    AdmissionRefused,
    AdmissionUnavailable,
    AdmittedSession,
    IdentityAdmission,
)
from haloflow.platform.config import ConfigInvalid, PlatformConfig, load_config
from haloflow.platform.http import Gate, body
from haloflow.platform.keys import KeySetClient
from haloflow.platform.routes import install as install_routes
from haloflow.platform.verify import AuthRefused, DependencyUnavailable, TokenVerifier

LOGGER: Final = logging.getLogger("haloflow.platform")

Close = Callable[[], Awaitable[None]]


class StartupCleanupFailed(Exception):
    """A resource failed to close when no other failure was in flight."""

    def __init__(self) -> None:
        super().__init__("STARTUP_CLEANUP_FAILED")
        self.reason_code = "STARTUP_CLEANUP_FAILED"


@dataclass(frozen=True, slots=True)
class StartupSteps:
    open_tenant_pool: Callable[[Any], Awaitable[tuple[Any, Close]]]
    start_services: Callable[[Any, Any], Awaitable[Any]]
    open_admission_pool: Callable[[Any], Awaitable[tuple[Any, Close]]]
    open_keyset: Callable[[Any], Awaitable[tuple[Any, Close]]]
    build_verifier: Callable[[Any, Any], Awaitable[Any]]
    build_admission: Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PlatformServices:
    services: Any
    admission: Any
    verifier: Any


# ---------------------------------------------------------------------------
# Production steps
# ---------------------------------------------------------------------------


async def _open_tenant_pool(config: PlatformConfig) -> tuple[Any, Close]:
    from haloflow.m01.connections import open_tenant_pool

    return await open_tenant_pool(config.runtime_database_url, min_size=1)


def _connector(conninfo: str) -> Callable[[], Awaitable[AsyncConnection[Any]]]:
    async def connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return connect


async def _start_services(config: PlatformConfig, tenant_pool: Any) -> Any:
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.runtime import TenantRuntimeDependencies
    from haloflow.startup import start_production_services

    deps = TenantRuntimeDependencies(
        pool=tenant_pool,
        control_store=PsycopgControlStore(tenant_pool),
        migrator_connect=_connector(config.migrator_database_url),
        provisioner_connect=_connector(config.provisioner_database_url),
    )
    return await start_production_services(
        deps, deployment=config.deployment, served_tenants=config.served_tenants
    )


async def _open_admission_pool(config: PlatformConfig) -> tuple[Any, Close]:
    from haloflow.m01.connections import open_admission_pool

    return await open_admission_pool(config.runtime_database_url, min_size=1)


async def _open_keyset(config: PlatformConfig) -> tuple[Any, Close]:
    # No fetch here: the key set is fetched on first use.
    client = KeySetClient(config.keyset, transport=httpx.AsyncHTTPTransport(), clock=time.time)
    return client, client.aclose


async def _build_verifier(config: PlatformConfig, keyset: Any) -> Any:
    return TokenVerifier(config.verifier, keyset)


async def _build_admission(admission_pool: Any) -> Any:
    return IdentityAdmission(admission_pool.connection)


PRODUCTION_STEPS: Final = StartupSteps(
    open_tenant_pool=_open_tenant_pool,
    start_services=_start_services,
    open_admission_pool=_open_admission_pool,
    open_keyset=_open_keyset,
    build_verifier=_build_verifier,
    build_admission=_build_admission,
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


async def _close_all(closers: list[Close]) -> bool:
    """Close in reverse order. Returns True if any close failed; a cancellation is
    re-raised after every closer has been attempted."""

    failed = False
    cancelled = False
    for close in reversed(closers):
        try:
            await close()
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 - recorded, never raised over an original
            failed = True
    closers.clear()
    if cancelled:
        raise asyncio.CancelledError()
    return failed


def _log_refusal(error: BaseException) -> None:
    if isinstance(error, ConfigInvalid):
        LOGGER.error("STARTUP_REFUSED reason_code=CONFIG_INVALID field=%s", error.field)
        return
    code = getattr(error, "reason_code", None)
    from haloflow.startup import STARTUP_REASON_CODES

    if type(code) is str and code in STARTUP_REASON_CODES:
        LOGGER.error("STARTUP_REFUSED reason_code=%s", code)


def _lifespan(config: PlatformConfig | None, steps: StartupSteps) -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        closers: list[Close] = []
        try:
            cfg = config if config is not None else load_config(os.environ)
            tenant_pool, close = await steps.open_tenant_pool(cfg)
            closers.append(close)
            services = await steps.start_services(cfg, tenant_pool)
            admission_pool, close = await steps.open_admission_pool(cfg)
            closers.append(close)
            keyset, close = await steps.open_keyset(cfg)
            closers.append(close)
            verifier = await steps.build_verifier(cfg, keyset)
            admission = await steps.build_admission(admission_pool)
        except BaseException as error:
            _log_refusal(error)
            await _close_all(closers)  # a cancellation here wins over the original
            raise
        app.state.platform = PlatformServices(services=services, admission=admission,
                                              verifier=verifier)
        try:
            yield
        except BaseException:
            # An error or cancellation thrown into the app's lifetime: unpublish, close
            # everything, and re-raise THAT error (D3; Codex W4A-LIFE-1). A close
            # failure is recorded only; a cancellation during the close wins.
            app.state.platform = None
            await _close_all(closers)
            raise
        app.state.platform = None  # the gate refuses from here on
        if await _close_all(closers):
            raise StartupCleanupFailed()  # shutdown-only close failure stays visible

    return lifespan


# ---------------------------------------------------------------------------
# Request admission
# ---------------------------------------------------------------------------


class _Refusal(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or token != token.strip() or " " in token:
        return None
    return token


async def require_admitted(request: Request) -> AdmittedSession:
    platform = getattr(request.app.state, "platform", None)
    if platform is None:
        raise _Refusal("NOT_READY")
    token = _bearer_token(request)
    if token is None:
        raise _Refusal("UNAUTHENTICATED")
    try:
        identity = await platform.verifier.verify(token)
    except AuthRefused:
        raise _Refusal("UNAUTHENTICATED") from None
    except DependencyUnavailable:
        raise _Refusal("UNAVAILABLE") from None
    try:
        session: AdmittedSession = await platform.admission.admit(identity)
    except AdmissionRefused as refused:
        key = "REAUTHENTICATION_REQUIRED" if refused.code == "MFA_REQUIRED" else "UNAUTHENTICATED"
        raise _Refusal(key) from None
    except AdmissionUnavailable:
        raise _Refusal("UNAVAILABLE") from None
    return session


Admitted = Annotated[AdmittedSession, Depends(require_admitted)]


async def _refusal_handler(_request: Request, error: Exception) -> Response:
    assert isinstance(error, _Refusal)
    return body(error.key)


async def _internal_handler(_request: Request, _error: Exception) -> Response:
    return body("INTERNAL")


async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def logout(request: Request, session: Admitted) -> Response:
    try:
        await request.app.state.platform.admission.logout(session.identity)
    except AdmissionRefused:
        return body("UNAUTHENTICATED")
    except AdmissionUnavailable:
        return body("UNAVAILABLE")
    return Response(status_code=204)


def create_app(*, config: PlatformConfig | None = None,
               steps: StartupSteps | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=_lifespan(config, steps or PRODUCTION_STEPS))
    app.state.platform = None
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/v1/session/logout", logout, methods=["POST"], status_code=204,
                      response_class=Response)
    install_routes(app)
    app.add_exception_handler(_Refusal, _refusal_handler)
    app.add_exception_handler(Exception, _internal_handler)
    app.add_middleware(Gate)  # last added = outermost
    return app


app = create_app()
