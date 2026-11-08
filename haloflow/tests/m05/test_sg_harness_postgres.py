"""L-1S package S-G1b — test-side synthetic application, PostgreSQL rows.

Governing: S-G1b plan v1 + v1r1, PG packet spec v1 + v1r1 (Work Session 2026-11-07),
Codex ALIGN for tests 20261107T185959Z / 20261107T191149Z, option A (test-side
harness evidence only: NOT an installable application, NOT production-lifespan
coverage; L-1S stays open). Every new-harness symbol is reached through `_mod()` in
the row body (class-B red rule). Synthetic data only.

PINNED DEFAULT WIRING (`sg_harness`, spec v1 section 2 + v1r1 P1-P4):
- `pg_steps(config, *, keys) -> SyntheticSteps`:
  guard = `read_identity` on runtime_dsn and migrator_dsn (psycopg
  `AsyncConnection.connect(dsn, autocommit=True)`) then `check_identity`;
  open_tenant_pool = `open_tenant_pool(runtime_dsn, min_size=1)`;
  verify_tenants = `startup._verify_served_tenant` per served tenant with
  `build_static_plan(sc.sc_registry())`, migrator connect for verification only and
  `forbid_provisioning()` as the provisioner; a str outcome ->
  `StartupRefused(code, index)`;
  build_services = `compose_tenant_runtime(sc registry, sc catalogue, {46}, ENFORCE)`
  then the production step-4 classes (ServedTenantGateway, EventFoundation
  "m05_followup", M05Service + ProductionNoteKeys, StaffService, ServedTenantResolver)
  plus IntakeService(producer sa.PRODUCER), ReplyIntake, DueWindowRunner(W_TIMER,
  TIMER_CAPS, page 10, cap 50) and `ingress_context`; services expose `gateway` (the
  one ServedTenantGateway), and the runner uses that gateway and `services.resolver`;
  readiness = timer context with exactly {m05.intake.due_windows}, one fetch limit 1
  through the served gateway, `classify_readiness`;
  admission pool on runtime_dsn (no separate admission DSN, v1r1 P2); key set = the
  given test-issuer `keys` (W4b seam) with a no-op close; TokenVerifier(issuer,
  audience, ES256); IdentityAdmission.
- `default_scheduler_factory(*, clock, sleep)` -> unstarted `DueWindowScheduler(runner,
  tenants, interval_s=interval, clock=clock, sleep=sleep)`.
- `parked_scheduler_factory()` -> the same with a sleep that parks on a never-set event:
  zero ticks (`_loop` sleeps before its first tick), still a verified stop (v1r1 P4).

Rows: G-01..G-07, G-08 a-e, G-08b, G-09, G-10, G-10b (spec v1 section 4, v1r1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import m02_support
import psycopg
import pytest
import sa_support as sa
import sc_support as sc
import sd_support as sd
from psycopg import sql
from test_sg_harness_units import _Life

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402
import l1w_support as s  # noqa: E402

pytestmark = pytest.mark.postgres

KEY: Final = s.SigningKey.ec("sg1b-ec-1")
INGRESS: Final = "/v1/synthetic/inbound-sms"
SECRET_A: Final = "a" * 32 + "-sg1b-ingress-A"
SECRET_B: Final = "b" * 32 + "-sg1b-ingress-B"
MARKER: Final = "ZQX-sg1b-raw-marker-5520"
MANIFEST: Final = sql.Identifier("public", "haloflow_synthetic_manifest")
BACKSTOP: Final = 300.0
WATCH: Final = 30.0


def _mod() -> Any:
    import sg_harness as module

    return module


@pytest.fixture(autouse=True)
def _process_backstop() -> Iterator[None]:
    import faulthandler

    faulthandler.dump_traceback_later(BACKSTOP, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


# --- fixtures -----------------------------------------------------------------------------------


@dataclass
class World:
    a: sc.SCInstall
    b: sc.SCInstall
    env: db.L1WEnv
    token: str
    staff_a: db.Identity
    staff_b: db.Identity

    @property
    def admin(self) -> str:
        return str(self.a.ids.admin)


def _bind(env: db.L1WEnv, tenant_id: str) -> db.Identity:
    ident = db.Identity(issuer=s.ISSUER, subject=f"sub-{env.token}-{uuid.uuid4().hex[:12]}")
    with env.connect("staff_owner") as conn:
        assert db.admin_bind(conn, tenant_id, uuid.UUID(sd.STAFF), ident, uuid.uuid4()) == "BOUND"
    return ident


def _purge_identity(env: db.L1WEnv, tenant_id: str) -> None:
    with psycopg.connect(env.admin, autocommit=True) as conn:
        for table in ("staff_sessions", "staff_identity_bindings", "staff_identity_log",
                      "tenant_auth_settings_log", "tenant_auth_settings"):
            db._with_triggers_disabled(
                conn, table,
                sql.SQL("DELETE FROM shared.{} WHERE tenant_id = %s").format(
                    sql.Identifier(table)), (tenant_id,))


def _write_manifest(admin: str, token: str, tenants: list[str]) -> None:
    with psycopg.connect(admin, autocommit=True) as conn, conn.transaction():
        conn.execute(sql.SQL("DELETE FROM {}").format(MANIFEST))
        conn.execute(sql.SQL("INSERT INTO {} (token, tenants) VALUES (%s, %s)").format(MANIFEST),
                     (token, tenants))


def _read_manifest(admin: str) -> list[tuple[str, list[str]]]:
    with psycopg.connect(admin, autocommit=True) as conn:
        rows = conn.execute(sql.SQL("SELECT token::text, tenants FROM {} ORDER BY token").format(
            MANIFEST)).fetchall()
    return [(r[0], list(r[1])) for r in rows]


@pytest.fixture(scope="module")
def world(request: pytest.FixtureRequest, migrated_database: str,
          m02_ids: m02_support.Identities, m05_reset: Any) -> Iterator[World]:
    tenants = [m02_support.tenant_for(f"{request.module.__name__}:{x}") for x in "ab"]
    for tenant in tenants:
        m05_reset(tenant)
    token = str(uuid.uuid4())
    with db.l1w_environment(migrated_database) as env:
        env.login("runtime", db.RUNTIME)
        env.login("staff_owner", db.STAFF_OWNER)
        a, b = (sc.SCInstall(m02_ids, t) for t in tenants)
        owned = False  # this fixture drops ONLY a manifest table it created (review item 1)
        try:
            sd.prepare(a)
            sd.prepare(b)
            owned = _create_manifest(m02_ids.admin)
            _write_manifest(m02_ids.admin, token, [a.tenant_id, b.tenant_id])
            yield World(a, b, env, token, _bind(env, a.tenant_id), _bind(env, b.tenant_id))
        finally:
            if owned:
                with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
                    conn.execute(sql.SQL("DROP TABLE {}").format(MANIFEST))
            for tenant in tenants:
                _purge_identity(env, tenant[0])
                m05_reset(tenant)


class ManifestPreexisting(AssertionError):
    """A manifest table already exists: refuse to adopt or alter it."""


def _create_manifest(admin: str) -> bool:
    """Create and grant the manifest table; True once this caller owns it. An existing
    table (from anything else) is refused untouched, rows included."""

    with psycopg.connect(admin, autocommit=True) as conn:
        found = conn.execute("SELECT to_regclass('public.haloflow_synthetic_manifest')"
                             ).fetchone()
        if found is None or found[0] is not None:
            raise ManifestPreexisting("public.haloflow_synthetic_manifest already exists")
        with conn.transaction():
            conn.execute(sql.SQL("CREATE TABLE {} (token uuid PRIMARY KEY,"
                                 " tenants text[] NOT NULL)").format(MANIFEST))
            for role in ("haloflow_runtime", "haloflow_migrator"):
                conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
                    MANIFEST, sql.Identifier(role)))
    return True


@contextmanager
def _manifest(world: World, token: str, tenants: list[str]) -> Iterator[None]:
    """Replace the single manifest row; restore the module row exactly (v1r1 P3)."""

    original = _read_manifest(world.admin)
    _write_manifest(world.admin, token, tenants)
    try:
        yield
    finally:
        _write_manifest(world.admin, world.token, [world.a.tenant_id, world.b.tenant_id])
        assert _read_manifest(world.admin) == original


@contextmanager
def _manifest_renamed(world: World) -> Iterator[None]:
    with psycopg.connect(world.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER TABLE {} RENAME TO haloflow_synthetic_manifest_aside").format(
            MANIFEST))
    try:
        yield
    finally:
        with psycopg.connect(world.admin, autocommit=True) as conn:
            conn.execute("ALTER TABLE public.haloflow_synthetic_manifest_aside"
                         " RENAME TO haloflow_synthetic_manifest")
        assert _read_manifest(world.admin)[0][0] == world.token


def _config(world: World, *, installs: tuple[Any, ...] | None = None,
            token: str | None = None) -> Any:
    from haloflow.startup import Deployment

    installs = installs or (world.a, world.b)
    logins = world.a.ids.logins
    secrets = dict(zip((inst.tenant_id for inst in installs), (SECRET_A, SECRET_B),
                       strict=False))
    return _mod().SyntheticConfig.build(
        deployment=Deployment.NON_PRODUCTION, runtime_dsn=logins["haloflow_runtime"],
        migrator_dsn=logins["haloflow_migrator"], provisioner_dsn=logins["haloflow_provisioner"],
        manifest_token=token or world.token,
        served_tenants=tuple(inst.tenant_id for inst in installs),
        ingress_secrets=secrets, environ=os.environ)


def _deployment() -> Any:
    from haloflow.startup import Deployment

    return Deployment.NON_PRODUCTION


def _app(config: Any, *, factory: Any = None) -> Any:
    module = _mod()
    return module.create_synthetic_app(
        config, steps=module.pg_steps(config, keys=s.FakeKeys({KEY.kid: KEY})),
        scheduler_factory=factory or module.parked_scheduler_factory())


@asynccontextmanager
async def _running(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    life = _Life(app)
    try:
        assert await life.started(timeout=WATCH), (
            life.task.exception() if life.task.done() else "not up")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            yield client
        life.shut()
        assert await life.settled(timeout=WATCH)
        assert life.task.exception() is None
    finally:
        await life.abandon()


def _staff(ident: db.Identity) -> dict[str, str]:
    now = int(time.time())
    payload = s.claims(iss=s.ISSUER, sub=ident.subject, sid=db.new_sid(),
                       auth_time=now - 60, iat=now - 5, exp=now + 300)
    return {"authorization": f"Bearer {s.token(KEY, payload)}"}


def _ingress(secret: str = SECRET_A) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}", "content-type": "application/json"}


def _body(phone: str, text: str, message_id: str | None = None) -> dict[str, str]:
    return {"channel": "sms", "provider_message_id": message_id or sa.new_id(),
            "phone": phone, "text": text}


async def _listed(client: httpx.AsyncClient, ident: db.Identity) -> set[str]:
    ids: set[str] = set()
    path = "/v1/followup/items?limit=100&states=open"
    while True:
        response = await client.get(path, headers=_staff(ident))
        assert response.status_code == 200, response.content
        body = response.json()
        ids |= {i["item_id"] for i in body["items"]}
        if body["next"] is None:
            return ids
        nxt = body["next"]
        path = ("/v1/followup/items?limit=100&states=open"
                f"&after_created_at={nxt['created_at']}&after_item_id={nxt['item_id']}")


async def _dispatch(install: sc.SCInstall) -> tuple[str, str]:
    """FIXTURE SETUP: one fresh patient and one dispatched window, via the S-D chain."""

    _, phone, (ref,) = sa.fresh_patient(install)
    async with sd.runtime(install) as rt:
        dispatched = await sd.Chain(install, rt).dispatch(ref)
    return phone, str(dispatched.reminder_id)


# --- G-01 .. G-05 -------------------------------------------------------------------------------


CLASSES: Final = [("YES", "confirm", None), ("2", "reschedule", "reschedule_request"),
                  ("CANCEL", "cancel", "cancel_request"), ("maybe", "unclear", "unclear_reply")]


async def test_sg_g01_replies_reach_the_right_clinic_staff(world: World) -> None:
    sd.begin(world.a)
    sd.begin(world.b)
    items: set[str] = set()
    async with _running(_app(_config(world))) as client:
        for text, _cls, kind in CLASSES:
            phone, _ = await _dispatch(world.a)
            before_a, before_b = sd.counts(world.a), sd.counts(world.b)
            response = await client.post(INGRESS, json=_body(phone, text), headers=_ingress())
            assert response.status_code == 202, response.content
            body = response.json()
            assert set(body) == {"outcome", "decision", "inbound_id"}
            uuid.UUID(body["inbound_id"])
            if kind is None:
                assert (body["outcome"], body["decision"]) == ("registered", "recorded")
                assert sd.delta(before_a, sd.counts(world.a)) == sd.CONFIRM
            else:
                assert (body["outcome"], body["decision"]) == ("registered", "item")
                assert sd.delta(before_a, sd.counts(world.a)) == sd.reply_item(kind)
                rows = sa.admin(world.a, sa.q(world.a,
                    "SELECT item_id::text FROM {s}.m05_followup_items WHERE item_kind = %s"
                    " ORDER BY created_at DESC LIMIT 1"), (kind,))
                items.add(rows[0][0])
            assert sd.delta(before_b, sd.counts(world.b)) == {}
        listed_a = await _listed(client, world.staff_a)
        listed_b = await _listed(client, world.staff_b)
    assert len(items) == 3 and items <= listed_a
    assert not items & listed_b


async def test_sg_g02_stop_opts_out_and_blocks_dispatch(world: World) -> None:
    from haloflow.m05.intake import IntakeFailed

    sd.begin(world.a)
    _, phone, (ref1, ref2, ref3) = sa.fresh_patient(world.a, appointments=3)
    async with sd.runtime(world.a) as rt:
        chain = sd.Chain(world.a, rt)
        for ref in (ref1, ref2):
            await chain.dispatch(ref)
    before = sd.counts(world.a)
    async with _running(_app(_config(world))) as client:
        response = await client.post(INGRESS, json=_body(phone, "STOP"), headers=_ingress())
    assert (response.status_code, response.json()["decision"]) == (202, "opt_out")
    assert sd.delta(before, sd.counts(world.a)) == {
        "replies": 1, "opted_out": 1, "windows:awaiting": -2, "windows:opted_out": 2}
    still = sd.counts(world.a)
    async with sd.runtime(world.a) as rt:
        with pytest.raises(IntakeFailed) as caught:
            await sd.Chain(world.a, rt).dispatch(ref3)  # the S-D SD-04 refusal
    assert caught.value.code == "CONTACT_OPTED_OUT"
    assert sd.delta(still, sd.counts(world.a)) == {}


async def test_sg_g03_authentication_and_cross_clinic(world: World) -> None:
    sd.begin(world.a)
    sd.begin(world.b)
    phone, _ = await _dispatch(world.a)
    before_a, before_b = sd.counts(world.a), sd.counts(world.b)
    async with _running(_app(_config(world))) as client:
        for headers in ({"content-type": "application/json"},
                        _ingress("z" * 40), _ingress(SECRET_A + "x")):
            response = await client.post(INGRESS, json=_body(phone, "YES"), headers=headers)
            assert (response.status_code, response.json()) == (
                401, {"code": "INGRESS_UNAUTHENTICATED"})
        staff_jwt = {**_staff(world.staff_a), "content-type": "application/json"}
        response = await client.post(INGRESS, json=_body(phone, "YES"), headers=staff_jwt)
        assert (response.status_code, response.json()) == (
            401, {"code": "INGRESS_UNAUTHENTICATED"})
        response = await client.get("/v1/followup/items",
                                    headers={"authorization": f"Bearer {SECRET_A}"})
        assert (response.status_code, response.json()) == (401, {"code": "UNAUTHENTICATED"})
        assert sd.delta(before_a, sd.counts(world.a)) == {}
        assert sd.delta(before_b, sd.counts(world.b)) == {}
        response = await client.post(INGRESS, json=_body(phone, "YES"),
                                     headers=_ingress(SECRET_B))
    assert response.status_code == 202
    assert (response.json()["outcome"], response.json()["decision"]) == (
        "registered", "quarantined")
    assert sd.delta(before_a, sd.counts(world.a)) == {}
    assert sd.delta(before_b, sd.counts(world.b)) == {"replies": 1}


async def test_sg_g04_redelivery_and_conflict(world: World) -> None:
    sd.begin(world.a)
    phone, _ = await _dispatch(world.a)
    message = sa.new_id()
    async with _running(_app(_config(world))) as client:
        first = await client.post(INGRESS, json=_body(phone, "2", message), headers=_ingress())
        assert first.status_code == 202
        settled = sd.counts(world.a)
        again = await client.post(INGRESS, json=_body(phone, "2", message), headers=_ingress())
        conflict = await client.post(INGRESS, json=_body(phone, "CANCEL", message),
                                     headers=_ingress())
    assert again.status_code == 202
    assert again.json()["outcome"] == "existing"
    assert again.json()["inbound_id"] == first.json()["inbound_id"]
    assert (conflict.status_code, conflict.json()) == (409, {"code": "INBOUND_CONFLICT"})
    assert sd.delta(settled, sd.counts(world.a)) == {}


async def test_sg_g05_clinic_switched_off_then_on(world: World) -> None:
    sd.begin(world.a, activation="revoke")
    sd.begin(world.b)
    phone, _ = await _dispatch_while_inactive(world)
    app = _app(_config(world))
    async with _running(app) as client:
        assert dict(app.state.activation) == {world.a.tenant_id: "inactive",
                                              world.b.tenant_id: "active"}
        before = sd.counts(world.a)
        refused = await client.post(INGRESS, json=_body(phone, "YES"), headers=_ingress())
        assert (refused.status_code, refused.json()) == (409, {"code": "CLINIC_NOT_ACTIVATED"})
        assert sd.delta(before, sd.counts(world.a)) == {}
        sc.change(world.a, "grant")
        accepted = await client.post(INGRESS, json=_body(phone, "YES"), headers=_ingress())
    assert accepted.status_code == 202 and accepted.json()["decision"] == "recorded"


async def _dispatch_while_inactive(world: World) -> tuple[str, str]:
    """FIXTURE: dispatch needs an active clinic; grant, dispatch, revoke again."""

    sc.change(world.a, "grant")
    try:
        return await _dispatch(world.a)
    finally:
        sc.change(world.a, "revoke")


# --- G-06 scheduler ----------------------------------------------------------------------------


async def test_sg_g06_lifespan_scheduler_closes_the_due_window(world: World) -> None:
    import se_support as se

    sd.begin(world.a)
    sd.begin(world.b)
    _, reminder = await _dispatch(world.a)
    sa.shift_window(world.a, reminder, -25)
    before_a, before_b = sd.counts(world.a), sd.counts(world.b)
    clock = se.FakeClock(start=1000.0)
    module = _mod()
    app = _app(_config(world),
               factory=module.default_scheduler_factory(clock=clock, sleep=clock.sleep))
    async with _running(app) as client:
        deadline = time.monotonic() + WATCH
        while sd.delta(before_a, sd.counts(world.a)) != sd.NO_REPLY:
            assert time.monotonic() < deadline, sd.delta(before_a, sd.counts(world.a))
            await asyncio.sleep(0.05)
        listed = await _listed(client, world.staff_a)
    item = sa.admin(world.a, sa.q(world.a, "SELECT item_id::text FROM {s}.m05_followup_items"
                                  " WHERE item_kind = 'no_reply' ORDER BY created_at DESC"
                                  " LIMIT 1"))[0][0]
    assert item in listed
    assert sd.delta(before_a, sd.counts(world.a)) == sd.NO_REPLY
    assert sd.delta(before_b, sd.counts(world.b)) == {}


# --- G-07 restart -------------------------------------------------------------------------------


async def test_sg_g07_restart_keeps_state_and_replays(world: World) -> None:
    sd.begin(world.a)
    phone, _ = await _dispatch(world.a)
    message = sa.new_id()
    async with _running(_app(_config(world))) as client:
        first = await client.post(INGRESS, json=_body(phone, "CANCEL", message),
                                  headers=_ingress())
    assert first.status_code == 202
    kept = sd.counts(world.a)
    async with _running(_app(_config(world))) as client:
        assert sd.counts(world.a) == kept
        replay = await client.post(INGRESS, json=_body(phone, "CANCEL", message),
                                   headers=_ingress())
    assert replay.status_code == 202 and replay.json()["outcome"] == "existing"
    assert replay.json()["inbound_id"] == first.json()["inbound_id"]
    assert sd.counts(world.a) == kept


# --- G-08 refusals before publish ---------------------------------------------------------------


def _forbidden(config_dsns: list[str], tenants: list[tuple[str, str]]) -> set[str]:
    """Every DSN marker (password, user, dbname, the whole DSN) and every tenant id and
    schema key that must never appear in a log record."""

    markers: set[str] = set()
    for dsn in config_dsns:
        parts = psycopg.conninfo.conninfo_to_dict(dsn)
        markers |= {dsn, str(parts["password"]), str(parts["user"]), str(parts["dbname"])}
    for tenant_id, schema_key in tenants:
        markers |= {tenant_id, schema_key}
    return {m for m in markers if m}


async def _refused(app: Any, caplog: pytest.LogCaptureFixture, *, dsns: list[str],
                   tenants: list[tuple[str, str]]) -> Any:
    life = _Life(app)
    try:
        assert await life.settled(timeout=WATCH)
        assert not life.up.is_set() and app.state.platform is None
        error = life.task.exception()
        assert error is not None
        formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
        forbidden = _forbidden(dsns, tenants)
        for record in caplog.records:  # every logger; message, exception and stack
            text = formatter.format(record)
            leaked = sorted(m for m in forbidden if m in text)
            assert not leaked, (record.name, leaked)
        for text in (str(error), repr(error), repr(error.args)):
            assert not [m for m in forbidden if m in text], text
        return error
    finally:
        await life.abandon()


def _dsns(world: World) -> list[str]:
    logins = world.a.ids.logins
    return [logins["haloflow_runtime"], logins["haloflow_migrator"],
            logins["haloflow_provisioner"]]


async def test_sg_g08a_manifest_table_missing(world: World,
                                              caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.startup import StartupRefused

    before = sd.counts(world.a)
    with _manifest_renamed(world):
        error = await _refused(_app(_config(world)), caplog, dsns=_dsns(world),
                               tenants=[world.a.tenant, world.b.tenant])
    assert isinstance(error, StartupRefused)
    assert (error.reason_code, error.tenant_index) == ("SYNTHETIC_DATABASE_UNBOUND", None)
    assert sd.delta(before, sd.counts(world.a)) == {}


async def test_sg_g08b_wrong_token(world: World, caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.startup import StartupRefused

    error = await _refused(_app(_config(world, token=str(uuid.uuid4()))), caplog,
                           dsns=_dsns(world), tenants=[world.a.tenant, world.b.tenant])
    assert isinstance(error, StartupRefused)
    assert error.reason_code == "SYNTHETIC_DATABASE_UNBOUND"


@pytest.fixture
def negative(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
             m05_reset: Any) -> Iterator[Any]:
    """The negative tenant for G-08 c/d/e: unprovisioned, at 36, or at 45."""

    import l1w_w3a_support as w3a
    import staff_support as st

    kind = request.param
    tenant = m02_support.tenant_for(f"{request.module.__name__}:neg:{kind}")
    m05_reset(tenant)
    try:
        if kind == "unprovisioned":
            install: Any = sc.SCInstall(m02_ids, tenant)
        elif kind == "at36":
            install = st.StaffInstall(m02_ids, tenant)
            w3a.ready_with_staff(install)
        else:
            install = sc.SCInstall(m02_ids, tenant, registry="sb")
            install.require()
        yield kind, install
    finally:
        m05_reset(tenant)


@pytest.mark.parametrize(("negative", "code"), [
    ("unprovisioned", "TENANT_NOT_ACTIVE"), ("at36", "SCHEMA_VERSION_UNSUPPORTED"),
    ("at45", "SCHEMA_VERSION_UNSUPPORTED")], indirect=["negative"])
async def test_sg_g08cde_tenant_refusals(world: World, negative: Any, code: str,
                                         caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.startup import StartupRefused

    _kind, install = negative
    tenants = [world.a.tenant_id, install.tenant[0]]
    before = sd.counts(world.a)
    with _manifest(world, world.token, tenants):
        config = _mod().SyntheticConfig.build(
            deployment=_deployment(),
            runtime_dsn=world.a.ids.logins["haloflow_runtime"],
            migrator_dsn=world.a.ids.logins["haloflow_migrator"],
            provisioner_dsn=world.a.ids.logins["haloflow_provisioner"],
            manifest_token=world.token, served_tenants=tuple(tenants),
            ingress_secrets={tenants[0]: SECRET_A, tenants[1]: SECRET_B},
            environ=os.environ)
        error = await _refused(_app(config), caplog, dsns=_dsns(world),
                               tenants=[world.a.tenant, install.tenant])
    assert isinstance(error, StartupRefused)
    assert (error.reason_code, error.tenant_index) == (code, 1)
    assert sd.delta(before, sd.counts(world.a)) == {}


def _install_state(world: World) -> list[Any]:
    with psycopg.connect(world.admin, autocommit=True) as conn:
        out: list[Any] = []
        for inst in (world.a, world.b):
            out.append(conn.execute(
                "SELECT tenant_id, schema_key, lifecycle_state::text, schema_version"
                " FROM shared.tenants WHERE tenant_id = %s", (inst.tenant_id,)).fetchall())
            out.append(conn.execute(
                "SELECT p.proname, pg_catalog.md5(p.prosrc), p.proacl::text FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s"
                " ORDER BY 1, 2", (inst.schema,)).fetchall())
            out.append(conn.execute(
                "SELECT count(*) FROM shared.tenant_state_history WHERE tenant_id = %s",
                (inst.tenant_id,)).fetchall())
        out.append(conn.execute("SELECT * FROM shared.schema_migrations ORDER BY 1").fetchall())
        return out


async def test_sg_g08b2_startup_and_shutdown_change_no_installed_state(world: World) -> None:
    before = _install_state(world)
    async with _running(_app(_config(world))):
        pass
    assert _install_state(world) == before


# --- G-09 privacy -------------------------------------------------------------------------------


def _marker_hits(world: World) -> list[tuple[str, str, str]]:
    from test_sf_reply_postgres import _marker_columns

    with psycopg.connect(world.admin, autocommit=True) as conn:
        return [(schema, *hit) for schema in (world.a.schema, world.b.schema, "shared")
                for hit in _marker_columns(conn, schema)]


async def test_sg_g09_raw_text_never_kept(world: World,
                                          caplog: pytest.LogCaptureFixture) -> None:
    from test_sf_reply_postgres import MARKER as SF_MARKER

    sd.begin(world.a)
    phone, _ = await _dispatch(world.a)
    with caplog.at_level(logging.DEBUG):
        async with _running(_app(_config(world))) as client:
            replies = [
                await client.post(INGRESS, json=_body(phone, f"call me {SF_MARKER}"),
                                  headers=_ingress()),
                await client.post(INGRESS, content=b'{"channel": "sms", "text": "'
                                  + SF_MARKER.encode() + b'"', headers=_ingress()),
                await client.post(INGRESS, content=json.dumps(
                    _body(phone, SF_MARKER + " " * 20000)).encode(), headers=_ingress()),
            ]
    assert [r.status_code for r in replies] == [202, 400, 413]
    for response in replies:
        assert SF_MARKER not in response.text and phone not in response.text
    formatter = logging.Formatter("%(message)s %(exc_info)s")
    assert all(SF_MARKER not in formatter.format(r) for r in caplog.records)
    assert _marker_hits(world) == []
    # positive control: the same scanner finds the marker when it IS stored (rolled back)
    from test_sf_reply_postgres import _marker_columns

    with psycopg.connect(world.admin) as conn:
        probe = sql.Identifier(world.a.schema, "sg1b_scan_probe")
        conn.execute(sql.SQL("CREATE TABLE {} (t text, b bytea)").format(probe))
        conn.execute(sql.SQL("INSERT INTO {} VALUES (%s, %s)").format(probe),
                     (f"x {SF_MARKER} y", b"\x00" + SF_MARKER.encode()))
        assert _marker_columns(conn, world.a.schema) == [
            ("sg1b_scan_probe", "b"), ("sg1b_scan_probe", "t")]
        conn.rollback()


# --- G-10 bundle parity ------------------------------------------------------------------------


async def test_sg_g10_bundle_classes_and_shared_gateway(world: World) -> None:
    from haloflow.m01.staff.service import StaffService
    from haloflow.m05.due_windows import TIMER_CAPS, DueWindowRunner
    from haloflow.m05.reply_classifier import ReplyIntake
    from haloflow.m05.service import M05Service
    from haloflow.startup import ServedTenantGateway, ServedTenantResolver

    app = _app(_config(world))
    async with _running(app):
        services = app.state.platform.services
        assert type(services.m05) is M05Service
        assert type(services.staff) is StaffService
        assert type(services.resolver) is ServedTenantResolver
        assert type(services.gateway) is ServedTenantGateway
        assert type(services.reply) is ReplyIntake
        runner = services.runner
        assert type(runner) is DueWindowRunner
        assert runner._gateway is services.gateway and runner._resolver is services.resolver
        assert runner._principal.capabilities == TIMER_CAPS
        assert runner._principal.id == sa.W_TIMER


async def test_sg_g10b_admission_pool_is_the_guarded_database(world: World) -> None:
    """v1r1 P2 (review item 2): a connection taken from the ACTUAL admission pool the
    harness opened (app.state.pools["admission"]) is on the guarded database, as the
    runtime login, and its backend PID is listed in pg_stat_activity on that database
    with the fixture manifest visible through it."""

    expected = psycopg.conninfo.conninfo_to_dict(world.a.ids.logins["haloflow_runtime"])
    app = _app(_config(world))
    async with _running(app):
        pool = app.state.pools["admission"]
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT pg_catalog.current_database(), CURRENT_USER,"
                " pg_catalog.pg_backend_pid()")
            database, user, pid = await cursor.fetchone()
            token = await (await conn.execute(
                "SELECT token::text FROM public.haloflow_synthetic_manifest")).fetchall()
        with psycopg.connect(world.admin, autocommit=True) as admin:
            listed = admin.execute("SELECT datname, usename FROM pg_stat_activity"
                                   " WHERE pid = %s", (pid,)).fetchall()
    assert (database, user) == (expected["dbname"], expected["user"])
    assert listed == [(expected["dbname"], expected["user"])]
    assert token == [(world.token,)]


def test_sg_f01_preexisting_manifest_is_refused_and_preserved(world: World) -> None:
    """Review item 1 control: with a manifest table already present (the module's own),
    the fixture creator refuses it and changes nothing: table, grants and rows stay."""

    before = _read_manifest(world.admin)
    with pytest.raises(ManifestPreexisting):
        _create_manifest(world.admin)
    assert _read_manifest(world.admin) == before == [
        (world.token, [world.a.tenant_id, world.b.tenant_id])]
