"""L-1W W4a: the platform ASGI app — lifespan, readiness, boundary (unit tier, no DB).

Plan v1 R-W4-1/2/3/7, addendum 1 pins 1-4, architecture v2 A1/A8 and addendum 1
D3. Rows: E-L2, E-L3, H-01 (unit), H-03, H-05, H-06, ADMIT-MAP (HTTP mapping).

PINNED INTERFACE (W4a contract; red-gate reviewed):
- ``haloflow.platform.app.create_app(*, config=None, steps=None) -> FastAPI``.
  ``config`` None means ``haloflow.platform.config.load_config(os.environ)`` inside
  lifespan startup (a bad environment fails startup, never import). ``steps`` None
  means ``haloflow.platform.app.PRODUCTION_STEPS``.
- ``haloflow.platform.app.StartupSteps`` (frozen dataclass), called in this order,
  each awaited once; every ``open_*`` returns ``(resource, close)`` where ``close``
  is an async no-argument callable:
    1. ``open_tenant_pool(config)``
    2. ``start_services(config, tenant_pool) -> ProductionServices`` (no close)
    3. ``open_admission_pool(config)``
    4. ``open_keyset(config)``
    5. ``build_verifier(config, keyset) -> TokenVerifier`` (no close)
    6. ``build_admission(admission_pool) -> IdentityAdmission`` (no close)
  Then publish ``PlatformServices(services, admission, verifier)`` at
  ``app.state.platform``. Shutdown unpublishes first, then closes in reverse order.
- ``haloflow.platform.app.PRODUCTION_STEPS`` (a ``StartupSteps``): the tenant and
  admission pools are opened with ``wait=True`` and ``min_size >= 1`` on
  ``config.runtime_database_url`` (so each holds a backend once opened; E-L3 real-pool
  rows count them by ``application_name``); ``open_keyset`` performs no fetch.
- Cleanup (addendum 1 D3): on a failure every resource opened so far is closed in
  reverse order; a close failure is recorded, never raised over the original; the
  original exception propagates with its type and code; ``CancelledError`` wins; a
  close failure with no prior failure fails startup with
  ``StartupCleanupFailed``.
- Fixed bodies (closed literal ``haloflow.platform.http.BODIES``):
  not ready 503 ``{"code":"NOT_READY"}``; unauthenticated 401
  ``{"code":"UNAUTHENTICATED"}`` with ``WWW-Authenticate: Bearer``; MFA 401
  ``{"code":"REAUTHENTICATION_REQUIRED"}`` with the same header; unavailable 503
  ``{"code":"UNAVAILABLE"}``; tenant input 400 ``{"code":"TENANT_INPUT_REFUSED"}``;
  unmapped 500 ``{"code":"INTERNAL"}``; ``/healthz`` 200 ``{"status":"ok"}``.
- Order per request: readiness gate (outermost; also /healthz) -> tenant-input
  refusal -> bearer verification -> admission -> handler.
- Tenant-input names (closed, case-insensitive): headers ``x-tenant-id``,
  ``x-haloflow-tenant``, ``x-tenant``, ``tenant``; query keys ``tenant``,
  ``tenant_id``, ``tenant_hint``, ``clinic``, ``clinic_id``.
- W4a routes: ``GET /healthz`` and ``POST /v1/session/logout`` (204, empty body).
  FastAPI ``docs_url``/``redoc_url``/``openapi_url`` are None.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import l1w_support as s
import pytest

BEARER = {"www-authenticate": "Bearer"}


# ---------------------------------------------------------------------------
# Fakes (no database)
# ---------------------------------------------------------------------------


@dataclass
class Journal:
    events: list[str] = field(default_factory=list)


class FakeResource:
    def __init__(self, name: str, journal: Journal, *, close_error: Exception | None = None,
                 close_gate: asyncio.Event | None = None) -> None:
        self.name = name
        self.journal = journal
        self.close_error = close_error
        self.close_gate = close_gate
        self.closed = False

    async def close(self) -> None:
        self.journal.events.append(f"close:{self.name}")
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class SpyAdmission:
    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome
        self.admits: list[Any] = []
        self.logouts: list[Any] = []

    async def admit(self, identity: Any) -> Any:
        from haloflow.platform.admission import AdmittedSession

        self.admits.append(identity)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return AdmittedSession(tenant_id="clinic-unit", staff_id=uuid.UUID(int=7),
                               identity=identity)

    async def logout(self, identity: Any) -> None:
        self.logouts.append(identity)
        if isinstance(self.outcome, BaseException):
            raise self.outcome


@dataclass
class Rig:
    journal: Journal
    resources: dict[str, FakeResource]
    admission: SpyAdmission
    key: s.SigningKey
    steps: Any


def _rig(*, fail_at: str | None = None, error: BaseException | None = None,
         close_errors: dict[str, Exception] | None = None,
         close_gate: asyncio.Event | None = None, admission: SpyAdmission | None = None,
         keys_unavailable: bool = False) -> Rig:
    from haloflow.platform.app import StartupSteps
    from haloflow.platform.verify import TokenVerifier, VerifierConfig

    journal = Journal()
    resources: dict[str, FakeResource] = {}
    spy = admission or SpyAdmission()
    key = s.SigningKey.ec()
    errors = close_errors or {}

    def opener(name: str) -> Callable[..., Any]:
        async def open_(*_: Any) -> tuple[Any, Callable[[], Any]]:
            journal.events.append(f"open:{name}")
            if fail_at == name:
                raise error or RuntimeError(f"fault-{name}")
            res = FakeResource(name, journal, close_error=errors.get(name),
                               close_gate=close_gate if name == "tenant_pool" else None)
            resources[name] = res
            return res, res.close
        return open_

    async def start_services(*_: Any) -> Any:
        journal.events.append("open:services")
        if fail_at == "services":
            raise error or RuntimeError("fault-services")
        return object()

    async def build_verifier(*_: Any) -> Any:
        journal.events.append("open:verifier")
        if fail_at == "verifier":
            raise error or RuntimeError("fault-verifier")
        cfg = VerifierConfig(issuer=s.ISSUER, audience=s.AUDIENCE, algorithms=frozenset({"ES256"}))
        keys = s.FakeKeys({key.kid: key}, unavailable=keys_unavailable)
        return TokenVerifier(cfg, keys, clock=s.Clock())

    async def build_admission(*_: Any) -> Any:
        journal.events.append("open:admission")
        if fail_at == "admission":
            raise error or RuntimeError("fault-admission")
        return spy

    steps = StartupSteps(
        open_tenant_pool=opener("tenant_pool"),
        start_services=start_services,
        open_admission_pool=opener("admission_pool"),
        open_keyset=opener("keyset"),
        build_verifier=build_verifier,
        build_admission=build_admission,
    )
    return Rig(journal, resources, spy, key, steps)


def _config() -> Any:
    return object()  # the fake steps never read it


@asynccontextmanager
async def _running(rig: Rig) -> AsyncIterator[httpx.AsyncClient]:
    from haloflow.platform.app import create_app

    app = create_app(config=_config(), steps=rig.steps)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _bearer(rig: Rig, **overrides: Any) -> dict[str, str]:
    return {"authorization": f"Bearer {s.token(rig.key, s.claims(**overrides))}"}


# ---------------------------------------------------------------------------
# H-05 / H-06 and the route surface
# ---------------------------------------------------------------------------


async def test_h06_healthz_is_exact_when_ready() -> None:
    async with _running(_rig()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert response.content == b'{"status":"ok"}'


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
async def test_h05_docs_and_openapi_are_404(path: str) -> None:
    async with _running(_rig()) as client:
        response = await client.get(path)
    assert response.status_code == 404


def test_w4a_route_surface_is_exactly_pinned() -> None:
    from fastapi.routing import APIRoute

    from haloflow.platform.app import create_app

    app = create_app(config=_config(), steps=_rig().steps)
    routes = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert routes == {("GET", "/healthz"), ("POST", "/v1/session/logout")}
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


def test_bodies_literal_is_pinned() -> None:
    from haloflow.platform.http import BODIES

    assert dict(BODIES) == {
        "NOT_READY": (503, {"code": "NOT_READY"}),
        "UNAUTHENTICATED": (401, {"code": "UNAUTHENTICATED"}),
        "REAUTHENTICATION_REQUIRED": (401, {"code": "REAUTHENTICATION_REQUIRED"}),
        "UNAVAILABLE": (503, {"code": "UNAVAILABLE"}),
        "TENANT_INPUT_REFUSED": (400, {"code": "TENANT_INPUT_REFUSED"}),
        "INTERNAL": (500, {"code": "INTERNAL"}),
    }


# ---------------------------------------------------------------------------
# E-L2: readiness gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,method", [("/healthz", "GET"), ("/v1/session/logout", "POST"),
                                         ("/anything", "GET")])
async def test_el2_without_lifespan_every_request_is_503(path: str, method: str) -> None:
    from haloflow.platform.app import create_app

    rig = _rig()
    app = create_app(config=_config(), steps=rig.steps)  # lifespan never run (--lifespan off)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 503 and response.json() == {"code": "NOT_READY"}
    assert rig.journal.events == [] and rig.admission.admits == []


async def test_el2_shutdown_unpublishes_before_closing() -> None:
    from haloflow.platform.app import create_app

    gate = asyncio.Event()
    rig = _rig(close_gate=gate)
    app = create_app(config=_config(), steps=rig.steps)
    transport = httpx.ASGITransport(app=app)
    exiting: asyncio.Future[Any] | None = None
    entered = False
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        lifespan = app.router.lifespan_context(app)
        try:
            async with asyncio.timeout(10):
                await lifespan.__aenter__()
                entered = True
                assert (await client.get("/healthz")).status_code == 200
                exiting = asyncio.ensure_future(lifespan.__aexit__(None, None, None))
                while "close:tenant_pool" not in rig.journal.events:  # gated close reached
                    await asyncio.sleep(0.01)
                during = await client.get("/healthz")
                gate.set()
                await exiting
                after = await client.get("/healthz")
        finally:  # failure path: never leave the shutdown task, the gate or the stack open
            gate.set()
            if entered and exiting is None:  # failed before shutdown was started
                exiting = asyncio.ensure_future(lifespan.__aexit__(None, None, None))
            if exiting is not None:
                await asyncio.wait([exiting], timeout=10)
                if not exiting.done():
                    exiting.cancel()
                    await asyncio.wait([exiting], timeout=10)
    assert during.status_code == 503 and during.json() == {"code": "NOT_READY"}
    assert after.status_code == 503
    assert rig.journal.events[-3:] == ["close:keyset", "close:admission_pool",
                                       "close:tenant_pool"]


async def test_el2_startup_order_is_pinned() -> None:
    rig = _rig()
    async with _running(rig):
        pass
    assert rig.journal.events == [
        "open:tenant_pool", "open:services", "open:admission_pool", "open:keyset",
        "open:verifier", "open:admission",
        "close:keyset", "close:admission_pool", "close:tenant_pool",
    ]


# ---------------------------------------------------------------------------
# E-L3: partial-startup cleanup (addendum 1 D3)
# ---------------------------------------------------------------------------

STEPS = ["tenant_pool", "services", "admission_pool", "keyset", "verifier", "admission"]
OPENED_BEFORE = {
    "tenant_pool": [], "services": ["tenant_pool"], "admission_pool": ["tenant_pool"],
    "keyset": ["admission_pool", "tenant_pool"], "verifier": ["keyset", "admission_pool",
                                                             "tenant_pool"],
    "admission": ["keyset", "admission_pool", "tenant_pool"],
}


class FaultCode(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.reason_code = code


async def _start(rig: Rig) -> None:
    from haloflow.platform.app import create_app

    app = create_app(config=_config(), steps=rig.steps)
    async with app.router.lifespan_context(app):
        pytest.fail("startup should have failed")


@pytest.mark.parametrize("step", STEPS)
async def test_el3_fault_after_each_step_closes_what_was_opened_and_reraises(step: str) -> None:
    rig = _rig(fail_at=step, error=FaultCode(f"FAULT_{step.upper()}"))
    with pytest.raises(FaultCode) as caught:
        await _start(rig)
    assert caught.value.reason_code == f"FAULT_{step.upper()}"
    closes = [e.split(":", 1)[1] for e in rig.journal.events if e.startswith("close:")]
    assert closes == OPENED_BEFORE[step]
    assert all(r.closed for r in rig.resources.values())


@pytest.mark.parametrize("step", ["keyset", "verifier", "admission"])
async def test_el3_close_failure_never_masks_the_original(step: str) -> None:
    rig = _rig(fail_at=step, error=FaultCode("ORIGINAL"),
               close_errors={"admission_pool": RuntimeError("close-failed")})
    with pytest.raises(FaultCode) as caught:
        await _start(rig)
    assert caught.value.reason_code == "ORIGINAL"
    assert rig.resources["tenant_pool"].closed  # later closes still ran


async def test_el3_cleanup_failure_alone_fails_startup() -> None:
    from haloflow.platform.app import StartupCleanupFailed

    # The failure is in the shutdown of a successful start: reported, never swallowed.
    rig = _rig(close_errors={"keyset": RuntimeError("close-failed")})
    from haloflow.platform.app import create_app

    app = create_app(config=_config(), steps=rig.steps)
    with pytest.raises(StartupCleanupFailed):
        async with app.router.lifespan_context(app):
            pass
    assert rig.resources["admission_pool"].closed and rig.resources["tenant_pool"].closed


async def test_el3_cancellation_during_startup_wins_and_cleans_up() -> None:
    rig = _rig(fail_at="keyset", error=asyncio.CancelledError(),
               close_errors={"admission_pool": RuntimeError("close-failed")})
    with pytest.raises(asyncio.CancelledError):
        await _start(rig)
    assert rig.resources["tenant_pool"].closed and rig.resources["admission_pool"].closed


async def test_el3_cancellation_during_cleanup_wins() -> None:
    rig = _rig(fail_at="keyset", error=FaultCode("ORIGINAL"),
               close_errors={"admission_pool": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await _start(rig)
    assert rig.resources["tenant_pool"].closed


# ---------------------------------------------------------------------------
# H-01 / H-03 (unit tier) and the admission status mapping
# ---------------------------------------------------------------------------

PROTECTED = [("POST", "/v1/session/logout")]


@pytest.mark.parametrize("method,path", PROTECTED)
@pytest.mark.parametrize("auth", [
    None, "Bearer", "Bearer ", "Basic abc", "bearer x.y.z", "Bearer not-a-jwt",
])
async def test_h01_missing_or_malformed_bearer_is_401_and_admission_never_runs(
    method: str, path: str, auth: str | None,
) -> None:
    rig = _rig()
    headers = {} if auth is None else {"authorization": auth}
    async with _running(rig) as client:
        response = await client.request(method, path, headers=headers)
    assert response.status_code == 401 and response.json() == {"code": "UNAUTHENTICATED"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert rig.admission.admits == []


@pytest.mark.parametrize("method,path", PROTECTED)
@pytest.mark.parametrize("overrides", [
    {"iss": "https://other.invalid/"}, {"aud": "https://other.invalid"},
    {"exp": s.NOW - 120}, {"sid": s.DROP}, {"auth_time": s.DROP},
])
async def test_h01_verifier_refusal_is_401_and_admission_never_runs(
    method: str, path: str, overrides: dict[str, Any],
) -> None:
    rig = _rig()
    async with _running(rig) as client:
        response = await client.request(method, path, headers=_bearer(rig, **overrides))
    assert response.status_code == 401 and response.json() == {"code": "UNAUTHENTICATED"}
    assert rig.admission.admits == []


@pytest.mark.parametrize("method,path", [("GET", "/healthz"), *PROTECTED])
@pytest.mark.parametrize("where,name", [
    ("header", "X-Tenant-Id"), ("header", "x-haloflow-tenant"), ("header", "X-TENANT"),
    ("header", "tenant"), ("query", "tenant"), ("query", "tenant_id"),
    ("query", "tenant_hint"), ("query", "clinic"), ("query", "CLINIC_ID"),
])
async def test_h03_tenant_input_is_400_before_authentication(
    method: str, path: str, where: str, name: str,
) -> None:
    rig = _rig()
    async with _running(rig) as client:
        if where == "header":
            response = await client.request(method, path, headers={name: "clinic-x",
                                                                     **_bearer(rig)})
        else:
            response = await client.request(method, f"{path}?{name}=clinic-x",
                                            headers=_bearer(rig))
    assert response.status_code == 400 and response.json() == {"code": "TENANT_INPUT_REFUSED"}
    assert rig.admission.admits == []
    assert b"clinic-x" not in response.content


@pytest.mark.parametrize("code,status,body", [
    ("UNBOUND", 401, "UNAUTHENTICATED"), ("BINDING_MISMATCH", 401, "UNAUTHENTICATED"),
    ("REVOKED", 401, "UNAUTHENTICATED"), ("SID_MISMATCH", 401, "UNAUTHENTICATED"),
    ("AUTH_TIME_FUTURE", 401, "UNAUTHENTICATED"), ("ABSOLUTE", 401, "UNAUTHENTICATED"),
    ("IDLE", 401, "UNAUTHENTICATED"), ("INVALID_INPUT", 401, "UNAUTHENTICATED"),
    ("MFA_REQUIRED", 401, "REAUTHENTICATION_REQUIRED"),
])
async def test_admit_map_http_refusal_status(code: str, status: int, body: str) -> None:
    from haloflow.platform.admission import AdmissionRefused

    rig = _rig(admission=SpyAdmission(AdmissionRefused(code)))
    async with _running(rig) as client:
        response = await client.post("/v1/session/logout", headers=_bearer(rig))
    assert response.status_code == status and response.json() == {"code": body}
    assert response.headers["www-authenticate"] == "Bearer"
    assert rig.admission.logouts == []
    assert response.content == f'{{"code":"{body}"}}'.encode()


@pytest.mark.parametrize("reason", ["ADMISSION_COMMIT_UNKNOWN", "ADMISSION_UNAVAILABLE"])
async def test_admit_map_http_unavailable_is_503_and_no_handler(reason: str) -> None:
    from haloflow.platform.admission import AdmissionUnavailable

    rig = _rig(admission=SpyAdmission(AdmissionUnavailable(reason)))
    async with _running(rig) as client:
        response = await client.post("/v1/session/logout", headers=_bearer(rig))
    assert response.status_code == 503 and response.json() == {"code": "UNAVAILABLE"}
    assert rig.admission.logouts == []


async def test_h01_unknown_kid_is_401() -> None:
    rig = _rig()
    async with _running(rig) as client:
        token = s.token(s.SigningKey.ec("other-kid"), s.claims())
        refused = await client.post("/v1/session/logout",
                                    headers={"authorization": f"Bearer {token}"})
    assert refused.status_code == 401 and refused.json() == {"code": "UNAUTHENTICATED"}
    assert rig.admission.admits == []


async def test_keyset_unavailable_is_503_and_admission_never_runs() -> None:
    rig = _rig(keys_unavailable=True)
    async with _running(rig) as client:
        response = await client.post("/v1/session/logout", headers=_bearer(rig))
    assert response.status_code == 503 and response.json() == {"code": "UNAVAILABLE"}
    assert rig.admission.admits == []


async def test_logout_route_admits_then_logs_out_and_returns_204() -> None:
    rig = _rig()
    async with _running(rig) as client:
        response = await client.post("/v1/session/logout", headers=_bearer(rig))
    assert response.status_code == 204 and response.content == b""
    assert len(rig.admission.admits) == 1 and len(rig.admission.logouts) == 1
    assert rig.admission.admits[0].sid == s.SID == rig.admission.logouts[0].sid

