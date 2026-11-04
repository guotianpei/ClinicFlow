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
        # Only the first ``hold_limit`` arrivals wait at the barrier; any later one is
        # answered at once, so a wrongly admitted late request shows as a behavioural
        # 200 (asserted 401), not as a timeout (Codex 366f7e94).
        self.hold_limit: int | None = None

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
                if self.hold_limit is None or self.arrived <= self.hold_limit:
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


@pytest.fixture
def at(ago: Callable[..., datetime]) -> Callable[..., datetime]:
    """``ago`` truncated to whole seconds: JWT ``auth_time`` is an integer, so every SQL
    seed and every token in this module uses the same value (Codex ff2ef544 #1)."""

    def make(**delta: float) -> datetime:
        return ago(**delta).replace(microsecond=0)

    return make


SETTLE_S = 10.0
LOCK_TIMEOUT_MS = 15_000  # longer than wait_blocked_by's 10 s detection window
STATEMENT_TIMEOUT_MS = 20_000
BOUNDED_APP = "haloflow-l1w-w4a-bounded"


async def _settle(*tasks: asyncio.Future[Any]) -> None:
    """Failure-path cleanup: cancel and await anything still running, itself bounded
    (Codex ff2ef544 #2, 94a45a4f)."""

    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=SETTLE_S)


def _bounded_conninfo(env: db.L1WEnv, tag: str) -> str:
    """The fixture runtime login with a connect timeout and server-side lock and
    statement timeouts, so a retained lock can never block a test forever."""

    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(env.conninfo("runtime"))
    params.update(
        connect_timeout="5", application_name=tag,
        options=f"-c lock_timeout={LOCK_TIMEOUT_MS} -c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )
    return make_conninfo("", **params)


def _bounded_logout(env: db.L1WEnv, b: Bound, sid: str, auth: datetime, tag: str,
                    pid_out: dict[str, Any] | None = None) -> str:
    """Synchronous logout on its own bounded connection. Always run OFF the event
    loop (``asyncio.to_thread``), never inline (Codex 94a45a4f)."""

    import psycopg

    with psycopg.connect(_bounded_conninfo(env, tag)) as conn:
        if pid_out is not None:
            pid_out["pid"] = db.backend_pid(conn)
        return db.logout(conn, b.ident, sid, auth)


def _tagged_backends(env: db.L1WEnv, tag: str) -> int:
    row = env.one("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (tag,))
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# LO-01 over HTTP
# ---------------------------------------------------------------------------


async def test_lo01_http_logout_is_per_device(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], at: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key)
    b = bound_test_issuer()
    auth = at(minutes=3)
    sid_a, sid_b = db.new_sid(), db.new_sid()
    h.barrier.set()
    async with asyncio.timeout(DEADLINE), _client(h.app(held_route=True)) as client:
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
    assert row is not None and row[2] == auth and row[6] == "logout"
    assert h.handler_calls == 3


# ---------------------------------------------------------------------------
# I2-HTTP (addendum 1 pin 1): no numeric bound; admitted-before work completes
# ---------------------------------------------------------------------------


async def test_i2_http_requests_admitted_before_logout_complete_later_ones_refused(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], at: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key)
    b = bound_test_issuer()
    sid, auth = db.new_sid(), at(minutes=3)
    headers = _bearer(key, b, sid, auth)
    n = 3
    h.hold_limit = n
    held: list[asyncio.Future[Any]] = []
    late_task: asyncio.Future[Any] | None = None
    async with _client(h.app(held_route=True)) as client:
        try:
            async with asyncio.timeout(DEADLINE):
                held = [asyncio.ensure_future(client.get("/test/i2/held", headers=headers))
                        for _ in range(n)]
                while h.arrived < n:  # every request passed T1 and is inside its handler
                    await asyncio.sleep(0.01)

                # Logout commits while all n are held: off the loop, bounded connection.
                outcome = await asyncio.to_thread(_bounded_logout, env, b, sid, auth,
                                                  f"{BOUNDED_APP}-i2a-{env.token}")
                assert outcome == "LOGGED_OUT"

                # A regression that admits the late request would park it at the
                # barrier: it runs as a task and is bounded by the deadline.
                late_task = asyncio.ensure_future(client.get("/test/i2/held", headers=headers))
                late = await late_task
                h.barrier.set()
                results = await asyncio.gather(*held)
        finally:
            h.barrier.set()
            await _settle(*held, *([late_task] if late_task else []))
    assert [r.status_code for r in results] == [200] * n
    assert late.status_code == 401
    assert h.handler_calls == n  # the late request never reached the handler


async def test_i2_http_logout_waits_behind_inflight_admission_then_applies(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], at: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    b = bound_test_issuer()
    sid, auth = db.new_sid(), at(minutes=3)
    with env.connect("runtime") as conn:  # the session exists before the race
        assert db.admit(conn, b.ident, sid, auth)[0] == "ADMITTED"
    h = Harness(env, key, seam="hold")
    headers = _bearer(key, b, sid, auth)
    result: dict[str, Any] = {}

    tag = f"{BOUNDED_APP}-i2b-{env.token}"

    def do_logout() -> None:
        try:
            result["outcome"] = _bounded_logout(env, b, sid, auth, tag, result)
        except Exception as error:  # noqa: BLE001 - surfaced by the assertions below
            result["error"] = type(error).__name__

    thread = threading.Thread(target=do_logout, daemon=True)
    request: asyncio.Future[Any] | None = None
    h.barrier.set()
    async with _client(h.app(held_route=True)) as client:
        try:
            async with asyncio.timeout(DEADLINE):
                request = asyncio.ensure_future(client.get("/test/i2/held", headers=headers))
                while not h.seams or not h.seams[0].holding.is_set():
                    await asyncio.sleep(0.01)
                holder = h.seams[0].pid
                assert holder is not None

                thread.start()
                while "pid" not in result:
                    await asyncio.sleep(0.01)
                await asyncio.to_thread(db.wait_blocked_by, env, result["pid"], holder)

                h.seams[0].release.set()  # T1 commits: the in-flight request is admitted
                first = await request
                await asyncio.to_thread(thread.join, DEADLINE)
                assert not thread.is_alive()
                assert result["outcome"] == "LOGGED_OUT"
                h.seam = None
                after = await client.get("/test/i2/held", headers=headers)
        finally:
            for seam in h.seams:  # never strand a held COMMIT (and its locks)
                if isinstance(seam, HoldSeam):
                    seam.release.set()
            await _settle(*([request] if request else []))
            if thread.is_alive():
                await asyncio.to_thread(thread.join, DEADLINE)
    assert not thread.is_alive()
    assert "error" not in result, result
    assert first.status_code == 200
    assert after.status_code == 401
    assert _tagged_backends(env, tag) == 0


async def test_i2_control_retained_lock_blocks_logout_only_until_its_bound(
    env: db.L1WEnv, bound_test_issuer: Callable[..., Bound], at: Callable[..., datetime],
) -> None:
    """HARNESS CONTROL (Codex 94a45a4f; passes at red by design, it imports nothing
    from haloflow.platform). A retained session-row lock, as a regression would leave
    it, blocks the off-loop bounded logout: the test still finishes within its
    deadline, the logout fails with the server-side lock timeout, the event loop
    stayed responsive meanwhile, and no tagged backend or task is left behind."""

    import psycopg

    b = bound_test_issuer()
    sid, auth = db.new_sid(), at(minutes=3)
    with env.connect("runtime") as conn:
        assert db.admit(conn, b.ident, sid, auth)[0] == "ADMITTED"
    tag = f"{BOUNDED_APP}-ctl-{env.token}"
    holder = psycopg.connect(env.admin)  # retains the row lock (the regression)
    try:
        holder.execute("SELECT 1 FROM shared.staff_sessions WHERE issuer = %s AND subject = %s"
                       " AND sid = %s FOR UPDATE", (b.ident.issuer, b.ident.subject, sid))
        pids: dict[str, Any] = {}
        start = asyncio.get_running_loop().time()
        task = asyncio.ensure_future(asyncio.to_thread(_bounded_logout, env, b, sid, auth,
                                                       tag, pids))
        ticks = 0
        try:
            async with asyncio.timeout(DEADLINE):
                while not task.done():  # the loop keeps running while logout is blocked
                    ticks += 1
                    await asyncio.sleep(0.05)
        finally:
            await _settle(task)
        elapsed = asyncio.get_running_loop().time() - start
        assert task.done()
        error = task.exception()
        assert isinstance(error, psycopg.errors.LockNotAvailable), repr(error)
        assert LOCK_TIMEOUT_MS / 1000 - 1 <= elapsed < DEADLINE
        assert ticks > 10
    finally:
        holder.rollback()
        holder.close()
    assert _tagged_backends(env, tag) == 0
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is None  # the blocked logout changed nothing


# ---------------------------------------------------------------------------
# ACF over HTTP: 503, zero handlers, persisted state independent of the status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["before", "after"])
async def test_acf_http_commit_failure_is_503_and_no_handler(
    mode: str, env: db.L1WEnv, bound_test_issuer: Callable[..., Bound],
    at: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key, seam=mode)
    b = bound_test_issuer()
    sid, auth = db.new_sid(), at(minutes=3)
    h.barrier.set()
    async with asyncio.timeout(DEADLINE), _client(h.app(held_route=True)) as client:
        response = await client.get("/test/i2/held", headers=_bearer(key, b, sid, auth))
    assert response.status_code == 503 and response.json() == {"code": "UNAVAILABLE"}
    assert h.handler_calls == 0
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    if mode == "after":
        assert h.seams[0].server_committed and row is not None  # durable, still 503
    else:
        assert row is None


async def test_h01_w4a_refused_admission_never_reaches_a_handler(
    env: db.L1WEnv, at: Callable[..., datetime],
) -> None:
    key = s.SigningKey.ec()
    h = Harness(env, key)
    stranger = Bound(env.tenant(), uuid.uuid4(),
                     db.Identity(issuer=s.ISSUER, subject=f"sub-{env.token}-unbound"),
                     uuid.uuid4())
    h.barrier.set()
    async with asyncio.timeout(DEADLINE), _client(h.app(held_route=True)) as client:
        response = await client.get("/test/i2/held",
                                    headers=_bearer(key, stranger, db.new_sid(), at(minutes=1)))
    assert response.status_code == 401 and response.json() == {"code": "UNAUTHENTICATED"}
    assert h.handler_calls == 0


# ---------------------------------------------------------------------------
# E-L3 with real pools (Codex ff2ef544 #3): pg_stat_activity is the oracle
# ---------------------------------------------------------------------------

PROBE_APP = "haloflow-l1w-w4a-el3"


def _platform_environ(env: db.L1WEnv, tag: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(env.conninfo("runtime"))
    params["application_name"] = tag
    runtime = make_conninfo("", **params)
    return {
        "HALOFLOW_PLATFORM_DEPLOYMENT": "non_production",
        "HALOFLOW_PLATFORM_SERVED_TENANTS": "clinic-el3-unused",
        "HALOFLOW_PLATFORM_RUNTIME_DATABASE_URL": runtime,
        "HALOFLOW_PLATFORM_MIGRATOR_DATABASE_URL": runtime,
        "HALOFLOW_PLATFORM_PROVISIONER_DATABASE_URL": runtime,
        "HALOFLOW_PLATFORM_AUTH_ISSUER": s.ISSUER,
        "HALOFLOW_PLATFORM_AUTH_AUDIENCE": s.AUDIENCE,
        "HALOFLOW_PLATFORM_AUTH_JWKS_URL": s.JWKS_URL,
        "HALOFLOW_PLATFORM_AUTH_ALGORITHMS": "ES256",
    }


def _backends(env: db.L1WEnv, tag: str) -> int:
    row = env.one("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (tag,))
    assert row is not None
    return int(row[0])


class StepFault(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.reason_code = code


@pytest.mark.parametrize("case", ["fault-keyset", "fault-verifier", "fault-admission",
                                  "close-failure", "cancel-keyset"])
async def test_el3_real_pools_partial_startup_leaves_no_backend(
    case: str, env: db.L1WEnv,
) -> None:
    """Real ``PRODUCTION_STEPS`` pools (tenant + admission) on the fixture runtime login,
    tagged by application_name. ``start_services`` is replaced (no production tenant in
    this suite; B5 startup closes its own connections, B5-E rows). A fault is injected
    after the real acquisitions; the positive control counts the tagged backends while
    the fault fires; afterwards none remain."""

    import dataclasses

    from haloflow.platform.app import PRODUCTION_STEPS, create_app
    from haloflow.platform.config import load_config

    tag = f"{PROBE_APP}-{case}-{env.token}"
    config = load_config(_platform_environ(env, tag))
    seen: dict[str, int] = {}

    async def fake_services(*_: Any) -> Any:
        return object()

    def faulty(step: str) -> Callable[..., Any]:
        async def fire(*_: Any) -> Any:
            seen["during"] = await asyncio.to_thread(_backends, env, tag)
            if case == "cancel-keyset":
                raise asyncio.CancelledError()
            raise StepFault(f"FAULT_{step.upper()}")
        return fire

    replace: dict[str, Any] = {"start_services": fake_services}
    if case in ("fault-keyset", "cancel-keyset", "close-failure"):
        replace["open_keyset"] = faulty("keyset")
    elif case == "fault-verifier":
        replace["build_verifier"] = faulty("verifier")
    else:
        replace["build_admission"] = faulty("admission")
    if case == "close-failure":
        real_open = PRODUCTION_STEPS.open_admission_pool

        async def open_then_fail_close(cfg: Any) -> Any:
            resource, close = await real_open(cfg)

            async def close_then_raise() -> None:
                await close()  # the real pool really closes ...
                raise RuntimeError("seam: close reported failure")  # ... then reports one

            return resource, close_then_raise

        replace["open_admission_pool"] = open_then_fail_close
    steps = dataclasses.replace(PRODUCTION_STEPS, **replace)
    app = create_app(config=config, steps=steps)

    expected: type[BaseException] = (asyncio.CancelledError if case == "cancel-keyset"
                                      else StepFault)
    async with asyncio.timeout(DEADLINE):
        with pytest.raises(expected) as caught:
            async with app.router.lifespan_context(app):
                pytest.fail("startup should have failed")
    if expected is StepFault:
        assert caught.value.reason_code.startswith("FAULT_")  # the original, not the close
    assert seen["during"] >= 2  # positive control: both real pools held a backend
    deadline = asyncio.get_running_loop().time() + DEADLINE
    while (count := await asyncio.to_thread(_backends, env, tag)) and \
            asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert count == 0
