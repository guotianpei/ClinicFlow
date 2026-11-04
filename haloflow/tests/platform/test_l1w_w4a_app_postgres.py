"""L-1W W4a: HTTP admission against the real shared identity objects (PostgreSQL 17).

Plan v1 R-W4-3/R-W4-9, addendum 1 pins 1-2, Codex 183528a3. Rows: LO-01 (HTTP),
I2-HTTP (both orders), ACF over HTTP (503 and zero handlers, persisted state read
independently), H-01 (no tenant handler on refusal).

The app is the real ``create_app`` with the real ``TokenVerifier`` (test issuer,
in-memory keys, real clock) and the real ``IdentityAdmission`` over the fixture
runtime login. Tenant services are a sentinel: W4a has no tenant route.

TEST SEAMS (labelled):
- I2-HTTP registers ONE test-only route on the app instance it builds,
  ``GET /test/i2/held``, depending on the pinned dependency
  ``haloflow.platform.app.require_admitted`` (``async def require_admitted(request:
  Request) -> AdmittedSession``; raises the mapped 401/503 responses); the
  handler waits on a barrier. Codex 183528a3 allows this handler seam in W4a;
  genuine-bundle T3 evidence remains a W4b requirement.
- ``HoldSeam`` pauses the admission connection's COMMIT until released, so a
  concurrent logout can be shown blocked behind T1 (``pg_blocking_pids``).
- ``CommitSeam`` (from the adapter rows) fails before or after the real COMMIT.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

import httpx
import l1w_db_support as db
import l1w_support as s
import pytest
from fastapi import Depends, Request
from l1w_db_support import Bound
from psycopg import AsyncConnection
from test_l1w_w4a_admission_postgres import CommitSeam

pytestmark = pytest.mark.postgres

DEADLINE = 20.0


async def _admitted(request: Request) -> Any:
    from haloflow.platform.app import require_admitted

    return await require_admitted(request)


# Module level so FastAPI resolves the (postponed) annotation from module globals.
HeldSession = Annotated[Any, Depends(_admitted)]


class HoldSeam:
    """TEST SEAM: COMMIT waits until ``release`` is set; ``holding`` marks the pause."""

    def __init__(self, conn: AsyncConnection[Any]) -> None:
        self._conn = conn
        self.holding = asyncio.Event()
        self.release = asyncio.Event()
        self.pid: int | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def commit(self) -> None:
        self.pid = self._conn.info.backend_pid
        self.holding.set()
        await self.release.wait()
        await self._conn.commit()


class Harness:
    def __init__(self, env: db.L1WEnv, key: s.SigningKey, seam: str | None = None) -> None:
        self.env = env
        self.key = key
        self.seam = seam
        self.seams: list[Any] = []
        self.handler_calls = 0
        self.held = asyncio.Event()
        self.barrier = asyncio.Event()
        self.arrived = 0

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        conn = await AsyncConnection.connect(self.env.conninfo("runtime"), autocommit=False)
        try:
            if self.seam is None:
                yield conn
            elif self.seam == "hold":
                seam = HoldSeam(conn)
                self.seams.append(seam)
                yield seam
            else:
                cs = CommitSeam(conn, self.seam)
                self.seams.append(cs)
                yield cs
        finally:
            await conn.close()

    def steps(self) -> Any:
        from haloflow.platform.admission import IdentityAdmission
        from haloflow.platform.app import StartupSteps
        from haloflow.platform.verify import TokenVerifier, VerifierConfig

        async def nothing() -> None:
            return None

        async def open_tenant_pool(*_: Any) -> Any:
            return object(), nothing

        async def start_services(*_: Any) -> Any:
            return object()

        async def open_admission_pool(*_: Any) -> Any:
            return object(), nothing

        async def open_keyset(*_: Any) -> Any:
            return s.FakeKeys({self.key.kid: self.key}), nothing

        async def build_verifier(_config: Any, keys: Any) -> Any:
            cfg = VerifierConfig(issuer=s.ISSUER, audience=s.AUDIENCE,
                                 algorithms=frozenset({self.key.alg}))
            return TokenVerifier(cfg, keys)

        async def build_admission(*_: Any) -> Any:
            return IdentityAdmission(self.connect)

        return StartupSteps(open_tenant_pool=open_tenant_pool, start_services=start_services,
                            open_admission_pool=open_admission_pool, open_keyset=open_keyset,
                            build_verifier=build_verifier, build_admission=build_admission)

    def app(self, *, held_route: bool = False) -> Any:
        from haloflow.platform.app import create_app

        app = create_app(config=object(), steps=self.steps())
        if held_route:
            async def held(session: HeldSession) -> dict[str, str]:
                self.handler_calls += 1
                self.arrived += 1
                self.held.set()
                await self.barrier.wait()
                return {"staff": str(session.staff_id)}

            app.add_api_route("/test/i2/held", held, methods=["GET"])
        return app


@asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=transport, base_url="http://test") as client):
        yield client


def _bearer(key: s.SigningKey, b: Bound, sid: str, auth: datetime) -> dict[str, str]:
    import time

    now = int(time.time())
    payload = s.claims(iss=s.ISSUER, sub=b.ident.subject, sid=sid,
                       auth_time=int(auth.timestamp()), iat=now - 5, exp=now + 300)
    return {"authorization": f"Bearer {s.token(key, payload)}"}


@pytest.fixture
def bound_test_issuer(env: db.L1WEnv) -> Callable[..., Bound]:
    """A binding whose issuer is the verifier's test issuer (s.ISSUER)."""

    def make() -> Bound:
        tenant = env.tenant()
        staff, actor = uuid.uuid4(), uuid.uuid4()
        ident = db.Identity(issuer=s.ISSUER, subject=f"sub-{env.token}-{uuid.uuid4().hex[:12]}")
        with env.connect("staff_owner") as conn:
            assert db.admin_bind(conn, tenant, staff, ident, actor) == "BOUND"
        return Bound(tenant, staff, ident, actor)

    return make


# ---------------------------------------------------------------------------
# LO-01 over HTTP
# ---------------------------------------------------------------------------


async def test_lo01_http_logout_is_per_device(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key)
    b = bound_test_issuer()
    auth = ago(minutes=3)
    sid_a, sid_b = db.new_sid(), db.new_sid()
    async with _client(h.app(held_route=True)) as client:
        h.barrier.set()
        assert (await client.get("/test/i2/held", headers=_bearer(key, b, sid_a, auth))
                ).status_code == 200
        assert (await client.get("/test/i2/held", headers=_bearer(key, b, sid_b, auth))
                ).status_code == 200

        out = await client.post("/v1/session/logout", headers=_bearer(key, b, sid_a, auth))
        assert out.status_code == 204 and out.content == b""

        again = await client.get("/test/i2/held", headers=_bearer(key, b, sid_a, auth))
        other = await client.get("/test/i2/held", headers=_bearer(key, b, sid_b, auth))
    assert again.status_code == 401 and again.json() == {"code": "UNAUTHENTICATED"}
    assert other.status_code == 200
    row = env.session_row(b.ident.issuer, b.ident.subject, sid_a)
    assert row is not None and row[6] == "logout"
    assert h.handler_calls == 3


# ---------------------------------------------------------------------------
# I2-HTTP (addendum 1 pin 1): no numeric bound; admitted-before work completes
# ---------------------------------------------------------------------------


async def test_i2_http_requests_admitted_before_logout_complete_later_ones_refused(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key)
    b = bound_test_issuer()
    sid, auth = db.new_sid(), ago(minutes=3)
    headers = _bearer(key, b, sid, auth)
    n = 3
    async with _client(h.app(held_route=True)) as client:
        held = [asyncio.ensure_future(client.get("/test/i2/held", headers=headers))
                for _ in range(n)]
        async with asyncio.timeout(DEADLINE):
            while h.arrived < n:  # every request passed T1 and is inside its handler
                await asyncio.sleep(0.01)

        with env.connect("runtime") as conn:  # logout commits while all n are held
            assert db.logout(conn, b.ident, sid, auth) == "LOGGED_OUT"

        late = await client.get("/test/i2/held", headers=headers)
        h.barrier.set()
        async with asyncio.timeout(DEADLINE):
            results = await asyncio.gather(*held)
    assert [r.status_code for r in results] == [200] * n
    assert late.status_code == 401
    assert h.handler_calls == n  # the late request never reached the handler


async def test_i2_http_logout_waits_behind_inflight_admission_then_applies(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    b = bound_test_issuer()
    sid, auth = db.new_sid(), ago(minutes=3)
    with env.connect("runtime") as conn:  # the session exists before the race
        assert db.admit(conn, b.ident, sid, auth)[0] == "ADMITTED"
    h = Harness(env, key, seam="hold")
    headers = _bearer(key, b, sid, auth)
    async with _client(h.app(held_route=True)) as client:
        h.barrier.set()
        request = asyncio.ensure_future(client.get("/test/i2/held", headers=headers))
        async with asyncio.timeout(DEADLINE):
            while not h.seams or not h.seams[0].holding.is_set():
                await asyncio.sleep(0.01)
        holder = h.seams[0].pid
        assert holder is not None

        result: dict[str, Any] = {}

        def do_logout() -> None:
            with env.connect("runtime") as conn:
                result["pid"] = db.backend_pid(conn)
                result["outcome"] = db.logout(conn, b.ident, sid, auth)

        thread = threading.Thread(target=do_logout, daemon=True)
        thread.start()
        async with asyncio.timeout(DEADLINE):
            while "pid" not in result:
                await asyncio.sleep(0.01)
        await asyncio.to_thread(db.wait_blocked_by, env, result["pid"], holder)

        h.seams[0].release.set()  # T1 commits: the in-flight request is admitted
        first = await request
        await asyncio.to_thread(thread.join, DEADLINE)
        assert result["outcome"] == "LOGGED_OUT"
        h.seam = None
        after = await client.get("/test/i2/held", headers=headers)
    assert first.status_code == 200
    assert after.status_code == 401


# ---------------------------------------------------------------------------
# ACF over HTTP: 503, zero handlers, persisted state independent of the status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["before", "after"])
async def test_acf_http_commit_failure_is_503_and_no_handler(
    mode: str, env: db.L1WEnv, bound_test_issuer: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key, seam=mode)
    b = bound_test_issuer()
    sid, auth = db.new_sid(), ago(minutes=3)
    async with _client(h.app(held_route=True)) as client:
        h.barrier.set()
        response = await client.get("/test/i2/held", headers=_bearer(key, b, sid, auth))
    assert response.status_code == 503 and response.json() == {"code": "UNAVAILABLE"}
    assert h.handler_calls == 0
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    if mode == "after":
        assert h.seams[0].server_committed and row is not None  # durable, still 503
    else:
        assert row is None


async def test_h01_refused_admission_never_reaches_a_handler(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key)
    stranger = Bound(env.tenant(), uuid.uuid4(),
                     db.Identity(issuer=s.ISSUER, subject=f"sub-{env.token}-unbound"),
                     uuid.uuid4())
    async with _client(h.app(held_route=True)) as client:
        h.barrier.set()
        response = await client.get("/test/i2/held",
                                    headers=_bearer(key, stranger, db.new_sid(), ago(minutes=1)))
    assert response.status_code == 401 and response.json() == {"code": "UNAUTHENTICATED"}
    assert h.handler_calls == 0
