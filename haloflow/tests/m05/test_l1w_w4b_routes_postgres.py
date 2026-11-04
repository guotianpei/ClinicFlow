"""L-1W W4b: tenant routes through the GENUINE production bundle (PostgreSQL 17).

Plan v1 R-W4-4..R-W4-10, addendum 1 pins 1-4 and Q-3, interface pins v1 + v1r1
(Codex 5cca1a17). Rows: R-01-HTTP (seven standings x eight routes, written-out
table), R-01-OWN, R-01-SUP, R-01-SUP-NOACT, R-01-REVOKE-PRE-T3, R-Q1, G-01, G-02,
G-03, G-05, AO-03, AO-04, AO-06, AO-08, S-04-HTTP, S-08-HTTP, H-07.

The app is the real ``create_app`` with the real ``PRODUCTION_STEPS`` for the tenant
pool, ``start_production_services`` (B5) and the admission pool, from a real
``load_config``. ONE labelled test seam replaces the key set and verifier: the real
``TokenVerifier`` over the test issuer's in-memory keys (A9 "test issuer"; no vendor,
no network). The services are the genuine bundle (``ProductionNoteKeys``), except
where a row says SEAM below.

TEST SEAMS (labelled):
- ``Hold`` wraps the published ``staff.self_standing`` so a request pauses after its
  self-standing step (T2) and before its route resolve and gateway transaction (T3);
  used only by R-01-REVOKE-PRE-T3 and R-Q1. The wrapper calls the real method.
- G-05 raises ``ActionCommitUnknown`` after a real, committed ``m05.act`` returns (an
  acknowledgement withheld after commit); the retry is genuine.
- FIXTURE SETUP (escape hatch, admin connection): staff rows, access rows and role
  rows are inserted directly, and sessions are seeded backdated (``seed_session``);
  items come from the real create path (B2 test intake profile). Bindings use the
  real ``shared.l1w_admin_bind`` on the staff-owner login.

PINNED (interface pins v1/v1r1; this file adds the request log line for H-07):
- One INFO line per tenant request on logger ``haloflow.platform``:
  ``REQUEST route=<template> status=<int> reason=<code or -> execution_id=<uuid>
  correlation_id=<uuid>``; ``<template>`` is the declared path template (for example
  ``/v1/followup/items/{item_id}``), never the raw path or query.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import l1w_w3a_support as w3a
import m02_support as m02
import psycopg
import pytest
import staff_support as st
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402
import l1w_support as s  # noqa: E402

pytestmark = pytest.mark.postgres

DEADLINE: Final = 60.0
KEY: Final = s.SigningKey.ec("w4b-ec-1")
DUMMY: Final = "00000000-0000-4000-8000-0000000d4b01"  # display/admin target

STANDINGS: Final[dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "INACTIVE": ("inactive", ("act", "read"), ()),
    "READ": ("active", ("read",), ()),
    "ACT": ("active", ("act", "read"), ()),
    "SUP-NO-ACT": ("active", ("read",), ("supervisor",)),
    "SUP-ACT": ("active", ("act", "read"), ("supervisor",)),
    "ADMIN-ONLY": ("active", (), ("clinic_admin",)),
    "ADMIN-ACT": ("active", ("act", "read"), ("clinic_admin",)),
}
ROUTES: Final = ("list", "get", "history", "act", "resolve", "read_note", "display", "admin")
A, D = "allow", "deny"
# Written out (not derived from the module under test): standing -> route -> verdict.
R01_TABLE: Final[dict[str, dict[str, str]]] = {
    "INACTIVE": dict(zip(ROUTES, (D, D, D, D, D, D, D, D), strict=True)),
    "READ": dict(zip(ROUTES, (A, A, A, D, D, A, A, D), strict=True)),
    "ACT": dict(zip(ROUTES, (A, A, A, A, A, A, A, D), strict=True)),
    "SUP-NO-ACT": dict(zip(ROUTES, (A, A, A, D, D, A, A, D), strict=True)),
    "SUP-ACT": dict(zip(ROUTES, (A, A, A, A, A, A, A, D), strict=True)),
    "ADMIN-ONLY": dict(zip(ROUTES, (D, D, D, D, D, D, A, A), strict=True)),
    "ADMIN-ACT": dict(zip(ROUTES, (A, A, A, A, A, A, A, A), strict=True)),
}
# The allowed outcome per route on the fixtures below (a random note id is not found).
ALLOWED_STATUS: Final = {"list": 200, "get": 200, "history": 200, "act": 200,
                         "resolve": 200, "read_note": 404, "display": 200, "admin": 200}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Member:
    staff: str
    ident: db.Identity


@dataclasses.dataclass
class Clinic:
    install: st.StaffInstall
    env: db.L1WEnv
    members: dict[str, Member]

    @property
    def tenant_id(self) -> str:
        return self.install.tenant[0]


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        handle.login("runtime", db.RUNTIME)
        handle.login("staff_owner", db.STAFF_OWNER)
        yield handle


def _schema_sql(install: st.StaffInstall, statement: str, params: Any = None) -> None:
    st.admin_rows(install, sql.SQL(statement).format(sql.Identifier(install.schema)), params)


def _seed_member(install: st.StaffInstall, staff: str, standing: tuple[str, ...] | Any) -> None:
    """FIXTURE SETUP (escape hatch, admin connection): one member with its standing."""

    status, access, roles = standing
    _schema_sql(install, "INSERT INTO {}.m01_staff (staff_id, status, status_changed_at,"
                " display_name) VALUES (%s, %s, clock_timestamp(), %s)",
                (staff, status, f"Synthetic {staff[-4:]}"))
    for value in access:
        _schema_sql(install, "INSERT INTO {}.m01_staff_followup_access (staff_id, access,"
                    " granted_at) VALUES (%s, %s, clock_timestamp())", (staff, value))
    for role in roles:
        w3a.seed_role(install, staff, role)


def _bind(env: db.L1WEnv, tenant_id: str, staff: str) -> db.Identity:
    ident = db.Identity(issuer=s.ISSUER, subject=f"sub-{env.token}-{uuid.uuid4().hex[:12]}")
    with env.connect("staff_owner") as conn:
        assert db.admin_bind(conn, tenant_id, uuid.UUID(staff), ident, uuid.uuid4()) == "BOUND"
    return ident


def _purge_identity(env: db.L1WEnv, tenant_id: str) -> None:
    with psycopg.connect(env.admin, autocommit=True) as conn:
        for table in ("staff_sessions", "staff_identity_bindings", "staff_identity_log",
                      "tenant_auth_settings_log", "tenant_auth_settings"):
            db._with_triggers_disabled(
                conn, table,
                sql.SQL("DELETE FROM shared.{} WHERE tenant_id = %s").format(
                    sql.Identifier(table)), (tenant_id,))


@pytest.fixture(scope="module")
def clinic(request: pytest.FixtureRequest, env: db.L1WEnv, m02_ids: m02.Identities,
           m05_reset: Callable[[tuple[str, str]], None]) -> Iterator[Clinic]:
    tenant = m02.tenant_for(f"l1w:w4b:{request.module.__name__}")
    m05_reset(tenant)
    install = st.StaffInstall(m02_ids, tenant)
    try:
        w3a.ready_with_staff(install)  # installs target 36 and the B2 intake profile
        members: dict[str, Member] = {}
        for name, standing in STANDINGS.items():
            staff = str(uuid.uuid4())
            _seed_member(install, staff, standing)
            members[name] = Member(staff, _bind(env, tenant[0], staff))
        _seed_member(install, DUMMY, ("active", ("act", "read"), ()))
        yield Clinic(install, env, members)
    finally:
        _purge_identity(env, tenant[0])
        m05_reset(tenant)


def _config(clinic: Clinic) -> Any:
    from haloflow.platform.config import load_config

    logins = clinic.install.ids.logins
    return load_config({
        "HALOFLOW_PLATFORM_DEPLOYMENT": "production",
        "HALOFLOW_PLATFORM_SERVED_TENANTS": clinic.tenant_id,
        "HALOFLOW_PLATFORM_RUNTIME_DATABASE_URL": logins["haloflow_runtime"],
        "HALOFLOW_PLATFORM_MIGRATOR_DATABASE_URL": logins["haloflow_migrator"],
        "HALOFLOW_PLATFORM_PROVISIONER_DATABASE_URL": logins["haloflow_provisioner"],
        "HALOFLOW_PLATFORM_AUTH_ISSUER": s.ISSUER,
        "HALOFLOW_PLATFORM_AUTH_AUDIENCE": s.AUDIENCE,
        "HALOFLOW_PLATFORM_AUTH_JWKS_URL": "https://issuer.test.invalid/jwks",
        "HALOFLOW_PLATFORM_AUTH_ALGORITHMS": "ES256",
    })


class Hold:
    """SEAM: pause a request after its real self-standing step, before T3."""

    def __init__(self) -> None:
        self.paused = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = False


class _HeldStaff:
    def __init__(self, real: Any, hold: Hold) -> None:
        self._real = real
        self._hold = hold

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def self_standing(self, context: Any) -> Any:
        standing = await self._real.self_standing(context)
        if self._hold.armed:
            self._hold.paused.set()
            await self._hold.release.wait()
        return standing


def _steps(*, hold: Hold | None = None, wrap_m05: Callable[[Any], Any] | None = None) -> Any:
    from haloflow.platform.app import PRODUCTION_STEPS
    from haloflow.platform.verify import TokenVerifier

    async def open_keyset(_config: Any) -> Any:  # SEAM: test issuer keys
        async def nothing() -> None:
            return None
        return s.FakeKeys({KEY.kid: KEY}), nothing

    async def build_verifier(config: Any, keys: Any) -> Any:
        return TokenVerifier(config.verifier, keys)

    async def start_services(config: Any, pool: Any) -> Any:
        services = await PRODUCTION_STEPS.start_services(config, pool)
        if hold is None and wrap_m05 is None:
            return services
        return dataclasses.replace(
            services,
            staff=_HeldStaff(services.staff, hold) if hold is not None else services.staff,
            m05=wrap_m05(services.m05) if wrap_m05 is not None else services.m05,
        )

    return dataclasses.replace(PRODUCTION_STEPS, open_keyset=open_keyset,
                               build_verifier=build_verifier, start_services=start_services)


@asynccontextmanager
async def _client(clinic: Clinic, **kw: Any) -> AsyncIterator[httpx.AsyncClient]:
    from haloflow.platform.app import create_app

    app = create_app(config=_config(clinic), steps=_steps(**kw))
    transport = httpx.ASGITransport(app=app)
    async with (asyncio.timeout(DEADLINE), app.router.lifespan_context(app),
                httpx.AsyncClient(transport=transport, base_url="http://test") as client):
        yield client


def _whole_seconds(moment: datetime) -> datetime:
    return moment.replace(microsecond=0)


def _headers(member: Member, *, sid: str | None = None,
             auth: datetime | None = None) -> dict[str, str]:
    now = int(time.time())
    auth_time = int(auth.timestamp()) if auth is not None else now - 60
    payload = s.claims(iss=s.ISSUER, sub=member.ident.subject, sid=sid or db.new_sid(),
                       auth_time=auth_time, iat=now - 5, exp=now + 300)
    return {"authorization": f"Bearer {s.token(KEY, payload)}"}


def _item(clinic: Clinic) -> str:
    return w3a.item(clinic.install)  # FIXTURE SETUP: the real create path


def _history_rows(clinic: Clinic, item_id: str) -> list[tuple[Any, ...]]:
    return st.admin_rows(clinic.install, sql.SQL(
        "SELECT action, actor_id::text FROM {}.m05_followup_history"
        " WHERE item_id = %s ORDER BY seq").format(sql.Identifier(clinic.install.schema)),
        (item_id,))


def _request(route: str, *, item: str, key: str | None = None,
             version: int = 1) -> tuple[str, str, Any]:
    k = key or str(uuid.uuid4())
    return {
        "list": ("GET", "/v1/followup/items?limit=100", None),
        "get": ("GET", f"/v1/followup/items/{item}", None),
        "history": ("GET", f"/v1/followup/items/{item}/history", None),
        "act": ("POST", f"/v1/followup/items/{item}/actions",
                {"client_action_id": k, "expected_version": version, "type": "claim"}),
        "resolve": ("POST", f"/v1/followup/items/{item}/resolve",
                    {"client_action_id": k, "expected_version": version,
                     "type": "claim_and_resolve", "outcome_code": "reached_no_change"}),
        "read_note": ("GET", f"/v1/followup/notes/{uuid.uuid4()}", None),
        "display": ("POST", "/v1/staff/display", {"staff_ids": [DUMMY]}),
        "admin": ("POST", "/v1/staff/admin",
                  {"client_action_id": k, "operation": "rename", "staff_id": DUMMY,
                   "display_name": f"Dummy {k[:8]}"}),
    }[route]


async def _send(client: httpx.AsyncClient, headers: dict[str, str],
                req: tuple[str, str, Any]) -> httpx.Response:
    method, path, body = req
    if body is None:
        return await client.request(method, path, headers=headers)
    return await client.request(method, path, headers=headers, json=body)


# ---------------------------------------------------------------------------
# R-01-HTTP: seven standings x eight routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("standing", sorted(STANDINGS))
async def test_r01_http_standing_by_route_table(clinic: Clinic, standing: str) -> None:
    member = clinic.members[standing]
    verdicts: dict[str, tuple[int, Any]] = {}
    async with _client(clinic) as client:
        for route in ROUTES:
            item = _item(clinic)
            before = _history_rows(clinic, item)
            response = await _send(client, _headers(member), _request(route, item=item))
            verdicts[route] = (response.status_code, response.json())
            if R01_TABLE[standing][route] == D:
                assert _history_rows(clinic, item) == before, (standing, route)
    for route in ROUTES:
        status, body = verdicts[route]
        if R01_TABLE[standing][route] == D:
            assert (status, body) == (403, {"code": "FORBIDDEN"}), (standing, route, status, body)
        else:
            assert status == ALLOWED_STATUS[route], (standing, route, status, body)


# ---------------------------------------------------------------------------
# R-01-OWN / R-01-SUP / R-01-SUP-NOACT (pins v1r1 correction 1)
# ---------------------------------------------------------------------------


async def test_r01_own_claim_then_resolve_own_item(clinic: Clinic) -> None:
    member = clinic.members["ACT"]
    item = _item(clinic)
    async with _client(clinic) as client:
        claim = await _send(client, _headers(member), _request("act", item=item))
        assert claim.status_code == 200, claim.content
        version = claim.json()["snapshot"]["version"]
        body = {"client_action_id": str(uuid.uuid4()), "expected_version": version,
                "type": "resolve", "outcome_code": "reached_no_change"}
        resolved = await client.post(f"/v1/followup/items/{item}/resolve",
                                     headers=_headers(member), json=body)
    assert resolved.status_code == 200, resolved.content
    assert resolved.json()["outcome"] == "applied"
    assert resolved.json()["snapshot"]["state"] == "resolved"
    assert _history_rows(clinic, item)[1:] == [("claim", member.staff),
                                               ("resolve", member.staff)]


async def test_r01_sup_reassigns_then_resolves_another_actors_item(clinic: Clinic) -> None:
    owner, sup = clinic.members["ACT"], clinic.members["SUP-ACT"]
    item = _item(clinic)
    async with _client(clinic) as client:
        claim = await _send(client, _headers(owner), _request("act", item=item))
        assert claim.status_code == 200
        v = claim.json()["snapshot"]["version"]
        reassign = await client.post(
            f"/v1/followup/items/{item}/actions", headers=_headers(sup),
            json={"client_action_id": str(uuid.uuid4()), "expected_version": v,
                  "type": "reassign", "assignee_id": DUMMY})
        assert reassign.status_code == 200, reassign.content
        v = reassign.json()["snapshot"]["version"]
        resolved = await client.post(
            f"/v1/followup/items/{item}/resolve", headers=_headers(sup),
            json={"client_action_id": str(uuid.uuid4()), "expected_version": v,
                  "type": "resolve", "outcome_code": "no_contact_no_answer"})
    assert resolved.status_code == 200, resolved.content
    assert [a for a, _ in _history_rows(clinic, item)] == ["created", "claim", "reassign",
                                                           "resolve"]
    assert _history_rows(clinic, item)[2:] == [("reassign", sup.staff), ("resolve", sup.staff)]


async def test_r01_sup_without_act_cannot_act_or_resolve(clinic: Clinic) -> None:
    owner, sup = clinic.members["ACT"], clinic.members["SUP-NO-ACT"]
    item = _item(clinic)
    async with _client(clinic) as client:
        claim = await _send(client, _headers(owner), _request("act", item=item))
        v = claim.json()["snapshot"]["version"]
        before = _history_rows(clinic, item)
        reassign = await client.post(
            f"/v1/followup/items/{item}/actions", headers=_headers(sup),
            json={"client_action_id": str(uuid.uuid4()), "expected_version": v,
                  "type": "reassign", "assignee_id": DUMMY})
        resolve = await client.post(
            f"/v1/followup/items/{item}/resolve", headers=_headers(sup),
            json={"client_action_id": str(uuid.uuid4()), "expected_version": v,
                  "type": "resolve", "outcome_code": "reached_no_change"})
    assert (reassign.status_code, reassign.json()) == (403, {"code": "FORBIDDEN"})
    assert (resolve.status_code, resolve.json()) == (403, {"code": "FORBIDDEN"})
    assert _history_rows(clinic, item) == before


# ---------------------------------------------------------------------------
# R-01-REVOKE-PRE-T3 and R-Q1 (deterministic T2/T3 seam; committed change observed)
# ---------------------------------------------------------------------------


async def _held_request(clinic: Clinic, hold: Hold, client: httpx.AsyncClient,
                        headers: dict[str, str], req: tuple[str, str, Any],
                        change: Callable[[], None]) -> httpx.Response:
    hold.armed = True
    task = asyncio.ensure_future(_send(client, headers, req))
    try:
        await asyncio.wait_for(hold.paused.wait(), DEADLINE)
        await asyncio.to_thread(change)  # commits on its own connection
        hold.release.set()
        return await asyncio.wait_for(task, DEADLINE)
    finally:
        hold.release.set()
        if not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=10)


async def test_r01_role_revoked_before_t3_refuses_the_supervisor_action(clinic: Clinic) -> None:
    owner = clinic.members["ACT"]
    sup_staff = str(uuid.uuid4())
    _seed_member(clinic.install, sup_staff, ("active", ("act", "read"), ("supervisor",)))
    sup = Member(sup_staff, _bind(clinic.env, clinic.tenant_id, sup_staff))
    item = _item(clinic)
    hold = Hold()

    def revoke() -> None:
        w3a.drop_role(clinic.install, sup_staff, "supervisor")  # FIXTURE: committed
        assert (sup_staff, "supervisor") not in w3a.roles(clinic.install)  # observed

    async with _client(clinic, hold=hold) as client:
        claim = await _send(client, _headers(owner), _request("act", item=item))
        v = claim.json()["snapshot"]["version"]
        before = _history_rows(clinic, item)
        response = await _held_request(
            clinic, hold, client, _headers(sup),
            ("POST", f"/v1/followup/items/{item}/actions",
             {"client_action_id": str(uuid.uuid4()), "expected_version": v,
              "type": "reassign", "assignee_id": DUMMY}), revoke)
    assert (response.status_code, response.json()) == (403, {"code": "FORBIDDEN"})
    assert _history_rows(clinic, item) == before


@pytest.mark.parametrize("route", ["list", "get", "history", "read_note"])
async def test_rq1_reader_access_revoked_before_t3_returns_no_data(
    clinic: Clinic, route: str,
) -> None:
    staff = str(uuid.uuid4())
    _seed_member(clinic.install, staff, ("active", ("read",), ()))
    member = Member(staff, _bind(clinic.env, clinic.tenant_id, staff))
    item = _item(clinic)
    hold = Hold()

    def revoke() -> None:  # FIXTURE: committed removal of the read access row
        _schema_sql(clinic.install, "DELETE FROM {}.m01_staff_followup_access"
                    " WHERE staff_id = %s AND access = 'read'", (staff,))

    async with _client(clinic, hold=hold) as client:
        response = await _held_request(clinic, hold, client, _headers(member),
                                       _request(route, item=item), revoke)
    assert (response.status_code, response.json()) == (403, {"code": "FORBIDDEN"})
    assert item.encode() not in response.content


# ---------------------------------------------------------------------------
# G-01 / G-02 / G-03 / G-05 (genuine bundle)
# ---------------------------------------------------------------------------


async def test_g01_list_and_get_return_exactly_the_seeded_items(clinic: Clinic) -> None:
    from haloflow.m05.service import ITEM_KEYS

    member = clinic.members["READ"]
    seeded = {_item(clinic), _item(clinic)}
    listed_ids: list[str] = []
    async with _client(clinic) as client:
        path = "/v1/followup/items?limit=100&states=open"
        while True:  # page through with the cursor
            listed = await client.get(path, headers=_headers(member))
            assert listed.status_code == 200, listed.content
            body = listed.json()
            assert set(body) == {"schema_version", "items", "next"}
            listed_ids += [i["item_id"] for i in body["items"]]
            if body["next"] is None:
                break
            nxt = body["next"]
            path = ("/v1/followup/items?limit=100&states=open"
                    f"&after_created_at={nxt['created_at']}&after_item_id={nxt['item_id']}")
        one = sorted(seeded)[0]
        got = await client.get(f"/v1/followup/items/{one}", headers=_headers(member))
    expected_open = {r[0] for r in st.admin_rows(clinic.install, sql.SQL(
        "SELECT item_id::text FROM {}.m05_followup_items WHERE state = 'open'").format(
        sql.Identifier(clinic.install.schema)))}
    assert len(listed_ids) == len(set(listed_ids))
    assert set(listed_ids) == expected_open and seeded <= expected_open
    assert got.status_code == 200 and got.json()["item_id"] == one
    assert set(got.json()) == set(ITEM_KEYS)


async def test_g02_claim_writes_history_with_the_staff_id(clinic: Clinic) -> None:
    member = clinic.members["ACT"]
    item = _item(clinic)
    async with _client(clinic) as client:
        response = await _send(client, _headers(member), _request("act", item=item))
    assert response.status_code == 200 and response.json()["outcome"] == "applied"
    assert response.json()["snapshot"]["assignee_id"] == member.staff
    assert _history_rows(clinic, item)[-1] == ("claim", member.staff)


async def test_g03_note_bearing_resolve_and_read_note_fail_closed(clinic: Clinic) -> None:
    member = clinic.members["ACT"]
    item = _item(clinic)
    async with _client(clinic) as client:
        before = _history_rows(clinic, item)
        resolve = await client.post(
            f"/v1/followup/items/{item}/resolve", headers=_headers(member),
            json={"client_action_id": str(uuid.uuid4()), "expected_version": 1,
                  "type": "claim_and_resolve", "outcome_code": "other",
                  "note": f"{s.CANARY} synthetic note"})
        note = await client.get(f"/v1/followup/notes/{uuid.uuid4()}", headers=_headers(member))
    assert (resolve.status_code, resolve.json()) == (503, {"code": "KEY_UNAVAILABLE"})
    assert _history_rows(clinic, item) == before
    assert s.CANARY.encode() not in resolve.content
    # A random note id is not found before any key is needed; the genuine
    # NOTE_KEY_UNAVAILABLE mapping needs a stored envelope (G-04 harness stores one,
    # then the genuine bundle reads it: see test_l1w_w4b_note_harness_postgres.py).
    assert note.status_code == 404


async def test_g05_commit_unknown_then_same_id_retry_replays(clinic: Clinic) -> None:
    from haloflow.m05.service import ActionCommitUnknown

    member = clinic.members["ACT"]
    item = _item(clinic)
    fired: list[bool] = []

    def wrap(real: Any) -> Any:
        class _M05:
            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

            async def act(self, context: Any, action: Any) -> Any:
                result = await real.act(context, action)
                if not fired:  # SEAM: acknowledgement withheld after a real commit
                    fired.append(True)
                    raise ActionCommitUnknown(action.item_id, action.client_action_id)
                return result
        return _M05()

    key = str(uuid.uuid4())
    async with _client(clinic, wrap_m05=wrap) as client:
        first = await _send(client, _headers(member), _request("act", item=item, key=key))
        retry = await _send(client, _headers(member), _request("act", item=item, key=key))
    assert (first.status_code, first.json()) == (409, {"code": "COMMIT_UNKNOWN",
                                                       "client_action_id": key})
    assert retry.status_code == 200 and retry.json()["outcome"] == "replayed"
    assert [a for a, _ in _history_rows(clinic, item)].count("claim") == 1


# ---------------------------------------------------------------------------
# AO-03 / AO-04 / AO-06 / AO-08 (admin operations over HTTP)
# ---------------------------------------------------------------------------


def _admin_log(clinic: Clinic, key: str) -> list[tuple[Any, ...]]:
    return st.admin_rows(clinic.install, sql.SQL(
        "SELECT operation, staff_id::text, actor_id FROM {}.m01_staff_admin_log"
        " WHERE client_action_id = %s").format(sql.Identifier(clinic.install.schema)), (key,))


def _settings(clinic: Clinic) -> tuple[Any, ...] | None:
    rows = st.admin_rows(clinic.install, sql.SQL(
        "SELECT idle_timeout_minutes, mfa_required, updated_by_kind, updated_by"
        " FROM shared.tenant_auth_settings WHERE tenant_id = %s"), (clinic.tenant_id,))
    return rows[0] if rows else None


async def test_ao03_roles_and_settings_postconditions(clinic: Clinic) -> None:
    admin = clinic.members["ADMIN-ACT"]
    target = str(uuid.uuid4())
    _seed_member(clinic.install, target, ("active", ("act", "read"), ()))
    keys = [str(uuid.uuid4()) for _ in range(4)]
    async with _client(clinic) as client:
        grant = await client.post("/v1/staff/admin", headers=_headers(admin), json={
            "client_action_id": keys[0], "operation": "grant_role", "staff_id": target,
            "role": "supervisor"})
        assert grant.status_code == 200, grant.content
        assert (target, "supervisor") in w3a.roles(clinic.install)
        revoke = await client.post("/v1/staff/admin", headers=_headers(admin), json={
            "client_action_id": keys[1], "operation": "revoke_role", "staff_id": target,
            "role": "supervisor"})
        idle = await client.post("/v1/staff/admin", headers=_headers(admin), json={
            "client_action_id": keys[2], "operation": "set_auth_setting",
            "setting": "idle_timeout_minutes", "value": "30"})
        mfa = await client.post("/v1/staff/admin", headers=_headers(admin), json={
            "client_action_id": keys[3], "operation": "set_auth_setting",
            "setting": "mfa_required", "value": "false"})
    assert [r.status_code for r in (revoke, idle, mfa)] == [200, 200, 200]
    assert (target, "supervisor") not in w3a.roles(clinic.install)
    settings = _settings(clinic)
    assert settings is not None and settings[:3] == (30, False, "clinic_admin")
    assert settings[3] == admin.staff
    assert _admin_log(clinic, keys[0]) == [("grant_role", target, admin.staff)]
    assert _admin_log(clinic, keys[1]) == [("revoke_role", target, admin.staff)]
    assert _admin_log(clinic, keys[2]) == [("set_auth_setting", admin.staff, admin.staff)]
    assert grant.json()["outcome"] == "applied" and grant.json()["operation"] == "grant_role"
    assert set(grant.json()) == {"schema_version", "client_action_id", "operation", "staff",
                                 "outcome"}


async def test_ao04_same_id_replays_and_changed_field_conflicts(clinic: Clinic) -> None:
    admin = clinic.members["ADMIN-ACT"]
    target = str(uuid.uuid4())
    _seed_member(clinic.install, target, ("active", ("act", "read"), ()))
    key = str(uuid.uuid4())
    body = {"client_action_id": key, "operation": "grant_role", "staff_id": target,
            "role": "supervisor"}
    async with _client(clinic) as client:
        first = await client.post("/v1/staff/admin", headers=_headers(admin), json=body)
        again = await client.post("/v1/staff/admin", headers=_headers(admin), json=body)
        changed = await client.post("/v1/staff/admin", headers=_headers(admin),
                                    json={**body, "role": "clinic_admin"})
    assert first.json()["outcome"] == "applied" and again.status_code == 200
    assert again.json()["outcome"] == "replayed"
    assert (changed.status_code, changed.json()) == (409, {"code": "ACTION_KEY_CONFLICT"})
    assert len(_admin_log(clinic, key)) == 1
    assert (target, "clinic_admin") not in w3a.roles(clinic.install)


@pytest.mark.parametrize("standing", ["READ", "ACT", "SUP-ACT", "INACTIVE"])
async def test_ao06_non_admin_is_403_and_nothing_changes(clinic: Clinic, standing: str) -> None:
    member = clinic.members[standing]
    key = str(uuid.uuid4())
    before = _settings(clinic)
    async with _client(clinic) as client:
        response = await client.post("/v1/staff/admin", headers=_headers(member), json={
            "client_action_id": key, "operation": "set_auth_setting",
            "setting": "idle_timeout_minutes", "value": "6"})
    assert (response.status_code, response.json()) == (403, {"code": "FORBIDDEN"})
    assert _admin_log(clinic, key) == [] and _settings(clinic) == before


async def test_ao08_setting_applies_to_the_next_request_of_an_existing_session(
    clinic: Clinic,
) -> None:
    admin, member = clinic.members["ADMIN-ACT"], clinic.members["READ"]
    now = _whole_seconds(clinic.env.db_now())
    auth = now - timedelta(minutes=30)
    sid_control, sid_cut = db.new_sid(), db.new_sid()
    for sid in (sid_control, sid_cut):  # FIXTURE: active 6 minutes ago
        clinic.env.seed_session(s.ISSUER, member.ident.subject, sid, clinic.tenant_id,
                                uuid.UUID(member.staff), auth, now - timedelta(minutes=6))
    async with _client(clinic) as client:
        set_default = await client.post("/v1/staff/admin", headers=_headers(admin), json={
            "client_action_id": str(uuid.uuid4()), "operation": "set_auth_setting",
            "setting": "idle_timeout_minutes", "value": "15"})
        control = await client.get("/v1/followup/items?limit=1",
                                   headers=_headers(member, sid=sid_control, auth=auth))
        cut = await client.post("/v1/staff/admin", headers=_headers(admin), json={
            "client_action_id": str(uuid.uuid4()), "operation": "set_auth_setting",
            "setting": "idle_timeout_minutes", "value": "5"})
        after = await client.get("/v1/followup/items?limit=1",
                                 headers=_headers(member, sid=sid_cut, auth=auth))
        restore = await client.post("/v1/staff/admin", headers=_headers(admin), json={
            "client_action_id": str(uuid.uuid4()), "operation": "set_auth_setting",
            "setting": "idle_timeout_minutes", "value": "15"})
    assert set_default.status_code == 200 and control.status_code == 200
    assert cut.status_code == 200 and restore.status_code == 200
    assert (after.status_code, after.json()) == (401, {"code": "UNAUTHENTICATED"})
    row = clinic.env.session_row(s.ISSUER, member.ident.subject, sid_cut)
    assert row is not None and row[6] == "idle"


# ---------------------------------------------------------------------------
# S-04-HTTP / S-08-HTTP
# ---------------------------------------------------------------------------


async def test_s04_http_idle_expiry_is_401_and_persisted(clinic: Clinic) -> None:
    member = clinic.members["READ"]
    now = _whole_seconds(clinic.env.db_now())
    auth, sid = now - timedelta(hours=1), db.new_sid()
    clinic.env.seed_session(s.ISSUER, member.ident.subject, sid, clinic.tenant_id,
                            uuid.UUID(member.staff), auth, now - timedelta(minutes=61))
    async with _client(clinic) as client:
        response = await client.get("/v1/followup/items?limit=1",
                                    headers=_headers(member, sid=sid, auth=auth))
    assert (response.status_code, response.json()) == (401, {"code": "UNAUTHENTICATED"})
    row = clinic.env.session_row(s.ISSUER, member.ident.subject, sid)  # separate txn
    assert row is not None and row[5] is not None and row[6] == "idle"


async def test_s08_http_absolute_expiry_is_401_and_persisted(clinic: Clinic) -> None:
    member = clinic.members["READ"]
    now = _whole_seconds(clinic.env.db_now())
    auth, sid = now - timedelta(hours=12, minutes=1), db.new_sid()
    clinic.env.seed_session(s.ISSUER, member.ident.subject, sid, clinic.tenant_id,
                            uuid.UUID(member.staff), auth, now - timedelta(minutes=1))
    async with _client(clinic) as client:
        response = await client.get("/v1/followup/items?limit=1",
                                    headers=_headers(member, sid=sid, auth=auth))
    assert (response.status_code, response.json()) == (401, {"code": "UNAUTHENTICATED"})
    row = clinic.env.session_row(s.ISSUER, member.ident.subject, sid)
    assert row is not None and row[5] is not None and row[6] == "absolute"


async def test_s08_http_no_row_past_twelve_hours_creates_nothing(clinic: Clinic) -> None:
    member = clinic.members["READ"]
    now = _whole_seconds(clinic.env.db_now())
    auth, sid = now - timedelta(hours=12, minutes=1), db.new_sid()
    async with _client(clinic) as client:
        response = await client.get("/v1/followup/items?limit=1",
                                    headers=_headers(member, sid=sid, auth=auth))
    assert response.status_code == 401
    assert clinic.env.session_row(s.ISSUER, member.ident.subject, sid) is None


# ---------------------------------------------------------------------------
# H-07: canaries absent from bodies and logs; request lines carry templates only
# ---------------------------------------------------------------------------

_REQUEST_LINE: Final = re.compile(
    r"^REQUEST route=(/\S*) status=(\d{3}) reason=([A-Z_]+|-) "
    r"execution_id=[0-9a-f-]{36} correlation_id=[0-9a-f-]{36}$")


async def test_h07_canaries_absent_and_request_lines_are_templates(
    clinic: Clinic, caplog: pytest.LogCaptureFixture,
) -> None:
    member = clinic.members["ACT"]
    item = _item(clinic)
    token_headers = _headers(member)
    raw_token = token_headers["authorization"].split(" ", 1)[1]
    canary_note = f"{s.CANARY}-note"
    canary_key = f"{s.CANARY}-key"
    responses: list[httpx.Response] = []
    with caplog.at_level("DEBUG"):
        async with _client(clinic) as client:
            for route in ROUTES:
                responses.append(await _send(client, _headers(member),
                                             _request(route, item=item)))
            responses.append(await client.post(
                f"/v1/followup/items/{item}/resolve", headers=token_headers,
                json={"client_action_id": str(uuid.uuid4()), "expected_version": 2,
                      "type": "resolve", "outcome_code": "other", "note": canary_note}))
            responses.append(await client.post(
                f"/v1/followup/items/{item}/actions", headers=token_headers,
                json={"client_action_id": str(uuid.uuid4()), "expected_version": 2,
                      "type": "claim", canary_key: 1}))
            responses.append(await client.get(
                f"/v1/followup/items?limit=1&{canary_key}=1", headers=token_headers))
    forbidden = [s.CANARY, member.ident.subject, raw_token, clinic.tenant_id,
                 clinic.install.schema]
    for response in responses:
        text = response.text + repr(dict(response.headers))
        for value in forbidden:
            assert value not in text, (response.request.url.path, value)
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    for value in [*forbidden, item]:
        assert value not in log_text, value
    lines = [r.getMessage() for r in caplog.records
             if r.name == "haloflow.platform" and r.getMessage().startswith("REQUEST ")]
    assert len(lines) == len(responses)
    templates = set()
    for line in lines:
        match = _REQUEST_LINE.fullmatch(line)
        assert match is not None, line
        templates.add(match.group(1))
    assert templates <= {
        "/v1/followup/items", "/v1/followup/items/{item_id}",
        "/v1/followup/items/{item_id}/history", "/v1/followup/items/{item_id}/actions",
        "/v1/followup/items/{item_id}/resolve", "/v1/followup/notes/{note_id}",
        "/v1/staff/display", "/v1/staff/admin"}
    assert not any(r.name.startswith("uvicorn.access") for r in caplog.records)
