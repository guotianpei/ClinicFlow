"""Option B B3b: exposure gate, identity, staff routes, simulator, labels, HTTP drain and
no-network rows, PostgreSQL 17 (per-case initdb clusters; B2 installs after a B3a first start).
Class A unless stated (b3b_support docstring): each row fails in its own body on the missing
B3b interface until it exists.

Fixtures:
- `inst`: a fresh B2 install after one B3a first start (rows that write, restart or change
  the standing; Codex Q-T4 for identity and failure paths).
- `seeded`: MODULE-scoped: one install, first start, one open item of each of the five kinds
  (syn_phone(0..4)). Only rows that read, or that refuse before any service call, use it; each
  row starts and stops its own lifecycle on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    with s.cluster(tmp_path) as c:
        yield c
    s.reap()


@pytest.fixture
def inst(pg: s.PgCluster, tmp_path: Path) -> Iterator[b3.Install]:
    i = b3.install(pg, tmp_path)
    bb.first_start(i)
    yield i
    with contextlib.suppress(ModuleNotFoundError):
        s.mod("hooks").FAULT = None


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[b3.Install, dict[str, Any]]]:
    tmp = tmp_path_factory.mktemp("b3b-seeded")
    with s.cluster(tmp) as c:
        i = b3.install(c, tmp)
        bb.first_start(i)
        kinds = bb.seed_kinds(i)
        yield i, kinds
    s.reap()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Spy:
    """Counts calls to the named async methods of an object (the services under the app)."""

    def __init__(self, target: Any, names: tuple[str, ...]) -> None:
        self.calls: list[str] = []
        for name in names:
            real = getattr(target, name)

            async def wrapper(*a: Any, _n: str = name, _r: Any = real, **k: Any) -> Any:
                self.calls.append(_n)
                return await _r(*a, **k)

            setattr(target, name, wrapper)


M05_CALLS = ("get", "list_items", "history", "reason", "act")


# =========================================================================== C: controls

def test_b3b_c1_seed_gives_every_kind_and_restart_accepts_it(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """C: the seed harness yields one open item of each of the five kinds on canonical phones,
    and a B3a lifecycle start after the seed passes the reduced seal (runtime rows only)."""
    inst, kinds = seeded
    assert set(kinds) == set(bb.ITEM_KINDS)
    assert all(bb.CANONICAL.fullmatch(k["phone"]) for k in kinds.values())
    for k in kinds.values():
        assert bb.item_row(inst, k["item_id"])["state"] == "open"
    bb.first_start(inst)


def test_b3b_c2_direct_ingress_twin_accepts_canonical_numbers(inst: b3.Install) -> None:
    """C (v1r1 §2): through the UNCHANGED S-G1a router (test harness), a reply from a
    canonical number for a sent reminder is registered, with one inbound row."""
    phone = bb.syn_phone(23)
    bb.sent_only(inst, phone)
    status, out = bb.direct_reply(inst, phone, "1")
    assert (status, out["outcome"]) == (202, "registered"), out
    assert len(bb.inbound_rows(inst, phone)) == 1


def test_b3b_c3_standing_override_changes_the_directory(inst: b3.Install) -> None:
    """C: the test-only standing override edits the staff directory as the I5/I6 rows need."""
    st = inst.state
    bb.standing_override(inst, access=("read",))
    rows = inst.admin(f"SELECT access FROM {inst.schema}.m01_staff_followup_access"
                      " WHERE staff_id = %s", (st["staff_id"],))
    assert [r[0] for r in rows] == ["read"]
    bb.standing_override(inst, status="inactive")
    (row,) = inst.admin(f"SELECT status FROM {inst.schema}.m01_staff WHERE staff_id = %s",
                        (st["staff_id"],))
    assert row[0] == "inactive"


def test_b3b_c4_driver_hold_body_parks_the_app() -> None:
    """C: the ASGI driver's held body stream keeps the app call unfinished until released."""
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def body() -> tuple[bool, int]:
        hold = asyncio.Event()
        task = asyncio.create_task(bb.asgi(app, "GET", "/x", hold_body=hold))
        await asyncio.sleep(0.2)
        parked = not task.done()
        hold.set()
        return parked, (await task).status

    assert run(body()) == (True, 200)


# =========================================================================== H: exposure gate

def test_b3b_h2_spoofed_host_refused_on_every_route_while_running(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B3b-4 step 1): on a RUNNING app every route refuses a rebinding-shaped Host with
    421 HOST_REFUSED and calls no service."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> list[tuple[str, int, Any]]:
        async with bb.running(inst) as (lc, app):
            spy = Spy(lc.services.m05, M05_CALLS)
            out = []
            for name, (method, _p) in bb.ROUTES.items():
                r = await bb.asgi(app, method, bb.path(name, iid),
                                  headers=bb.get_headers(host="attacker.test:8765"),
                                  body=b"{}" if method == "POST" else b"")
                out.append((name, r.status, r.json.get("code") if r.json else None))
                bb.assert_labelled(r)
            assert spy.calls == []
            return out

    for name, status, code in run(body()):
        assert (status, code) == (421, "HOST_REFUSED"), name


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null",
                                    "https://127.0.0.1:8765", "http://127.0.0.1:8765/x"])
def test_b3b_h3_post_origin_required_and_exact(seeded: tuple[b3.Install, dict[str, Any]],
                                               origin: str | None) -> None:
    """A (R-B3b-4 step 4): a POST without Origin, or with any Origin other than exactly
    http:// + the request's Host, is 403 ORIGIN_REFUSED with no service call."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> list[tuple[int, Any]]:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            spy = Spy(lc.services.m05, M05_CALLS)
            out = []
            for name in ("act", "resolve", "sim_reply", "sim_ehr"):
                r = await bb.asgi(app, "POST", bb.path(name, iid),
                                  headers=bb.post_headers(token, origin=origin), body=b"{}")
                out.append((r.status, r.json.get("code")))
                bb.assert_labelled(r)
            assert spy.calls == []
            return out

    assert set(run(body())) == {(403, "ORIGIN_REFUSED")}


def test_b3b_h3b_cross_alias_origin_refused(seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (Codex v1r1): Host 127.0.0.1:8765 with Origin http://localhost:8765 (another
    allowlisted alias) is refused; Host localhost:8765 with the matching Origin passes the
    gate (reaches parsing: 422, not 403)."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> tuple[int, int]:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            a = await bb.asgi(app, "POST", bb.path("act", iid), body=b"{}",
                              headers=bb.post_headers(token, origin="http://localhost:8765"))
            b = await bb.asgi(app, "POST", bb.path("act", iid), body=b"{}",
                              headers=bb.post_headers(token, host="localhost:8765",
                                                      origin="http://localhost:8765"))
            return a.status, b.status

    assert run(body()) == (403, 422)


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x", "application/json5"])
def test_b3b_h4_post_content_type_must_be_json(seeded: tuple[b3.Install, dict[str, Any]],
                                               ctype: str | None) -> None:
    """A (R-B3b-4 step 4): a non-JSON POST (the simple-form CSRF shapes) is 415, labelled,
    with no service call."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> set[int]:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            spy = Spy(lc.services.m05, M05_CALLS)
            statuses = set()
            for name in ("act", "resolve", "sim_reply", "sim_ehr"):
                r = await bb.asgi(app, "POST", bb.path(name, iid), body=b"{}",
                                  headers=bb.post_headers(token, ctype=ctype))
                statuses.add(r.status)
                bb.assert_labelled(r)
            assert spy.calls == []
            return statuses

    assert run(body()) == {415}


@pytest.mark.parametrize("case", ["missing", "wrong", "duplicated", "previous_lifespan"])
def test_b3b_h5_csrf_token_required(seeded: tuple[b3.Install, dict[str, Any]],
                                    case: str) -> None:
    """A (R-B3b-4 step 4; mutant 3): a POST with the token missing, wrong, sent twice, or
    taken from the previous lifespan is 403 CSRF_REFUSED with no service call."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> set[tuple[int, Any]]:
        old = ""
        if case == "previous_lifespan":
            async with bb.running(inst) as (_lc, app0):
                old = await bb.session_token(app0)
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            assert token != old
            hdrs = {"missing": [h for h in bb.post_headers(token) if h[0] != bb.CSRF_HEADER],
                    "wrong": bb.post_headers(token[:-2] + ("AA" if token[-2:] != "AA" else "BB")),
                    "duplicated": bb.post_headers(token, extra=[(bb.CSRF_HEADER, token)]),
                    "previous_lifespan": bb.post_headers(old)}[case]
            spy = Spy(lc.services.m05, M05_CALLS)
            out = set()
            for name in ("act", "resolve", "sim_reply", "sim_ehr"):
                r = await bb.asgi(app, "POST", bb.path(name, iid), headers=hdrs, body=b"{}")
                out.add((r.status, r.json.get("code")))
            assert spy.calls == []
            return out

    assert run(body()) == {(403, "CSRF_REFUSED")}


def test_b3b_h6_cross_site_fetch_metadata_refused(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B3b-4 step 2): Sec-Fetch-Site cross-site / same-site on GET and POST is 403
    CROSS_SITE_REFUSED; same-origin and none pass the gate."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> dict[str, tuple[int, int]]:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            out = {}
            for site in ("cross-site", "same-site", "same-origin", "none"):
                g = await bb.asgi(app, "GET", bb.path("get", iid),
                                  headers=bb.get_headers(sec_fetch_site=site))
                p = await bb.asgi(app, "POST", bb.path("act", iid), body=b"{}",
                                  headers=bb.post_headers(token, extra=[("sec-fetch-site",
                                                                         site)]))
                out[site] = (g.status, p.status)
            return out

    out = run(body())
    assert out["cross-site"] == out["same-site"] == (403, 403)
    assert out["same-origin"] == out["none"] == (200, 422)


def test_b3b_h8_no_get_changes_state(seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (no mutating GET): one call to every GET route leaves every table digest and the
    simulated EHR unchanged."""
    inst, kinds = seeded
    iid = kinds["ehr_confirmation_check"]["item_id"]

    async def body() -> tuple[Any, Any]:
        async with bb.running(inst) as (lc, app):
            ehr0 = lc.port.ehr.snapshot()
            before = b3.table_digests(inst)
            for name, (method, _p) in bb.ROUTES.items():
                if method == "GET":
                    r = await bb.asgi(app, "GET", bb.path(name, iid) +
                                      ("?limit=10" if name == "list" else ""),
                                      headers=bb.get_headers())
                    assert r.status == 200, (name, r.status, r.raw[:200])
            after = b3.table_digests(inst)
            return (before, after), (ehr0, lc.port.ehr.snapshot())

    (before, after), (e0, e1) = run(body())
    assert b3.changed(before, after) == set()
    assert e0 == e1


def test_b3b_h9_token_never_leaks(seeded: tuple[b3.Install, dict[str, Any]],
                                  caplog: pytest.LogCaptureFixture) -> None:
    """A (R-B3b-4): the CSRF token appears only in the session response body: not in logs,
    the state file, any other response, or a response header."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]
    caplog.set_level(logging.DEBUG)

    async def body() -> tuple[str, list[bb.Resp]]:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            rs = [await bb.asgi(app, "GET", bb.path(n, iid) + ("?limit=5" if n == "list"
                                                               else ""),
                                headers=bb.get_headers()) for n in ("health", "list", "get")]
            rs.append(await bb.asgi(app, "POST", bb.path("act", iid), body=b"{}",
                                    headers=bb.post_headers(token)))
            rs.append(await bb.asgi(app, "POST", bb.path("act", iid), body=b"{}",
                                    headers=bb.post_headers("x" * 43)))
            return token, rs

    token, rs = run(body())
    assert token not in caplog.text
    assert token not in (inst.st_dir / "state.json").read_text()
    for r in rs:
        assert token.encode() not in r.raw
        assert all(token not in v for _k, v in r.headers)


def test_b3b_h10_ipv6_bind_allowlist(seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A: with bind ("::1", 8765) the Host [::1]:8765 passes and 127.0.0.1:8765 is 421."""
    inst, _ = seeded

    async def body() -> tuple[int, int]:
        async with bb.running(inst, bind=("::1", 8765)) as (_lc, app):
            a = await bb.asgi(app, "GET", "/synthetic/health",
                              headers=bb.get_headers(host="[::1]:8765"))
            b = await bb.asgi(app, "GET", "/synthetic/health",
                              headers=bb.get_headers(host="127.0.0.1:8765"))
            return a.status, b.status

    assert run(body()) == (200, 421)


def test_b3b_h11_session_response_is_no_store(seeded: tuple[b3.Install, dict[str, Any]]
                                              ) -> None:
    """A (Codex v1r1): GET /synthetic/session carries Cache-Control: no-store and Pragma:
    no-cache, and its body holds the token; no other route's body has a `csrf` key."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> tuple[bb.Resp, list[bb.Resp]]:
        async with bb.running(inst) as (_lc, app):
            sess = await bb.asgi(app, "GET", "/synthetic/session", headers=bb.get_headers())
            others = [await bb.asgi(app, "GET", bb.path(n, iid) + ("?limit=5" if n == "list"
                                                                   else ""),
                                    headers=bb.get_headers())
                      for n in ("health", "list", "get", "history", "reason")]
            return sess, others

    sess, others = run(body())
    assert sess.status == 200
    assert "no-store" in ",".join(sess.header("cache-control"))
    assert sess.header("pragma") == ["no-cache"]
    bb.assert_labelled(sess)
    assert type(sess.json["csrf"]) is str
    for r in others:
        assert "csrf" not in (r.json or {}), r.raw[:200]


# =========================================================================== I: identity

def test_b3b_i1_fixed_principal_on_every_staff_route(seeded: tuple[b3.Install, dict[str, Any]]
                                                     ) -> None:
    """A (R-B3b-2; mutant 7): every resolve the staff routes make carries the state's staff
    id, tenant, kind ACTOR and auth_method "synthetic"; two resolves per request (the
    production two-step)."""
    inst, kinds = seeded
    st = inst.state
    iid = kinds["unclear_reply"]["item_id"]
    seen: list[Any] = []

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            real = lc.services.resolver.resolve

            async def spy(**kw: Any) -> Any:
                seen.append(kw["principal"])
                return await real(**kw)

            lc.services.resolver.resolve = spy
            for name in ("list", "get", "history", "reason"):
                r = await bb.asgi(app, "GET", bb.path(name, iid) + ("?limit=5" if name ==
                                                                   "list" else ""),
                                  headers=bb.get_headers())
                assert r.status in (200, 404), (name, r.status)

    run(body())
    assert len(seen) == 8
    for p in seen:
        assert (p.id, p.auth_method, p.authorized_tenant_ids) == (
            st["staff_id"], "synthetic", frozenset({st["tenant_id"]}))
        assert p.kind.name == "ACTOR"


def test_b3b_i2_tenant_input_refused_like_production(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B3b-2): a production tenant header or query key is refused with production's
    TENANT_INPUT_REFUSED response, and nothing is resolved."""
    from haloflow.platform.http import body as prod_body

    inst, _ = seeded
    want = prod_body("TENANT_INPUT_REFUSED")

    async def body() -> list[tuple[int, Any]]:
        async with bb.running(inst) as (_lc, app):
            out = []
            for hdrs, q in ((bb.get_headers(x_tenant_id="other"), ""),
                            (bb.get_headers(tenant="other"), ""),
                            (bb.get_headers(), "&tenant_hint=other"),
                            (bb.get_headers(), "&clinic=other")):
                r = await bb.asgi(app, "GET", bb.path("list") + "?limit=5" + q, headers=hdrs)
                body_ = {k: v for k, v in (r.json or {}).items() if k != "simulated"}
                out.append((r.status, body_))
            return out

    for status, content in run(body()):
        assert (status, content) == (want.status_code, json.loads(bytes(want.body)))


def test_b3b_i3_request_chosen_actor_headers_ignored(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (mutant 7): the S-J test header names (actor, actor tenant, caps) change nothing:
    the principal is still B2's staff member."""
    import sj_harness as sj

    inst, kinds = seeded
    st = inst.state
    seen: list[Any] = []

    async def body() -> int:
        async with bb.running(inst) as (lc, app):
            real = lc.services.resolver.resolve

            async def spy(**kw: Any) -> Any:
                seen.append(kw["principal"])
                return await real(**kw)

            lc.services.resolver.resolve = spy
            r = await bb.asgi(app, "GET", bb.path("get", kinds["cancel_request"]["item_id"]),
                              headers=bb.get_headers() + [
                                  (sj.ACTOR_HEADER, str(uuid.uuid4())),
                                  (sj.ACTOR_TENANT_HEADER, "other"), (sj.CAPS_HEADER, "none")])
            return r.status

    assert run(body()) == 200
    assert {p.id for p in seen} == {st["staff_id"]}


def test_b3b_i4_capabilities_come_from_the_standing(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B3b-2; mutant 8): the second resolve of each route requests exactly
    route_capabilities(route, capabilities_for(standing)) for B2's standing (read + act)."""
    from haloflow.platform.capabilities import capabilities_for, route_capabilities

    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]
    seen: list[frozenset[str]] = []

    async def body() -> Any:
        async with bb.running(inst) as (lc, app):
            ctx = await lc.services.resolver.resolve(**_self_standing_kw(inst))
            standing = await lc.services.staff.self_standing(ctx)
            real = lc.services.resolver.resolve

            async def spy(**kw: Any) -> Any:
                seen.append(frozenset(kw["capabilities"]))
                return await real(**kw)

            lc.services.resolver.resolve = spy
            await bb.asgi(app, "GET", bb.path("get", iid), headers=bb.get_headers())
            # act (claim) with a stale version: resolved with its capability set, then refused
            # 409 by the service, so the shared item is not changed
            token = await bb.session_token(app)
            r = await bb.asgi(app, "POST", bb.path("act", iid), headers=bb.post_headers(token),
                              body=bb.jbody({"client_action_id": bb.new_uuid(),
                                             "expected_version": 7, "type": "claim"}))
            assert r.status == 409, r.raw[:200]
            return standing

    standing = run(body())
    held = capabilities_for(standing)
    assert seen[1] == route_capabilities("get", held)
    assert seen[3] == route_capabilities("act", held)   # implementation: act carries optional
    # read/supervise only when HELD by the standing (a fixed or widened set is detected here)
    # Measured at implementation: B2's first staff member is the bootstrapped clinic admin
    # (role clinic_admin), so `held` includes m01.staff.admin; the admin and display routes
    # are simply not served (R7 / R7u). No supervisor role.
    assert "m05.followup.supervise" not in held


def _self_standing_kw(inst: b3.Install) -> dict[str, Any]:
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.platform.capabilities import SELF_STANDING

    st = inst.state
    own = frozenset({SELF_STANDING})
    return {"principal": Principal(kind=PrincipalKind.ACTOR, id=st["staff_id"],
                                   auth_method="synthetic",
                                   authorized_tenant_ids=frozenset({st["tenant_id"]}),
                                   capabilities=own),
            "tenant_hint": st["tenant_id"], "purpose": "operations", "capabilities": own,
            "source": TrustedSource.HTTP, "execution_id": uuid.uuid4(),
            "correlation_id": uuid.uuid4(),
            "correlation_source": CorrelationSource.ENTRY_POINT_GENERATED}


def test_b3b_i5_read_only_standing_denies_act_and_resolve(inst: b3.Install) -> None:
    """A (J13-J15 analogue; mutant 8): with the test-only override to read-only access, the
    reads answer and act / resolve are 403 FORBIDDEN (production map) with no write."""
    kinds = bb.seed_kinds(inst, base=10)
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> tuple[int, int, int]:
        async with bb.running(inst) as (_lc, app):
            # after the start: the override edits install-sealed rows, which the next start's
            # reduced seal would refuse; the standing is read per request
            bb.standing_override(inst, access=("read",))
            token = await bb.session_token(app)
            g = await bb.asgi(app, "GET", bb.path("get", iid), headers=bb.get_headers())
            a = await bb.asgi(app, "POST", bb.path("act", iid), headers=bb.post_headers(token),
                              body=bb.jbody({"client_action_id": bb.new_uuid(),
                                             "expected_version": 1, "type": "claim"}))
            r = await bb.asgi(app, "POST", bb.path("resolve", iid),
                              headers=bb.post_headers(token),
                              body=bb.jbody({"client_action_id": bb.new_uuid(),
                                             "expected_version": 1, "type": "resolve",
                                             "outcome_code": "reached_no_change"}))
            assert a.json["code"] == r.json["code"] == "FORBIDDEN"
            return g.status, a.status, r.status

    assert run(body()) == (200, 403, 403)
    assert bb.item_row(inst, iid)["state"] == "open"


def test_b3b_i6_inactive_standing_denies_every_staff_route(inst: b3.Install) -> None:
    """A: with the override to inactive, every staff route is 403."""
    kinds = bb.seed_kinds(inst, base=20)
    iid = kinds["cancel_request"]["item_id"]

    async def body() -> set[int]:
        async with bb.running(inst) as (_lc, app):
            bb.standing_override(inst, status="inactive")   # after the start (see I5)
            token = await bb.session_token(app)
            out = set()
            for name in bb.STAFF_ROUTES:
                method = bb.ROUTES[name][0]
                r = await bb.asgi(app, method, bb.path(name, iid) +
                                  ("?limit=5" if name == "list" else ""),
                                  headers=bb.post_headers(token) if method == "POST"
                                  else bb.get_headers(),
                                  body=bb.jbody({"client_action_id": bb.new_uuid(),
                                                 "expected_version": 1, "type": "claim"})
                                  if name == "act" else
                                  bb.jbody({"client_action_id": bb.new_uuid(),
                                            "expected_version": 1, "type": "resolve",
                                            "outcome_code": "reached_no_change"})
                                  if name == "resolve" else b"")
                out.add(r.status)
            return out

    assert run(body()) == {403}


# =========================================================================== R: staff routes

def test_b3b_r1_r4_reads_answer_for_every_kind(seeded: tuple[b3.Install, dict[str, Any]]
                                               ) -> None:
    """A (R-B3b-1): list returns all five seeded items; get and history answer for each kind;
    reason answers for ehr_confirmation_check. Payloads equal the service's own (plus the
    label)."""
    inst, kinds = seeded

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            r = await bb.asgi(app, "GET", bb.path("list") + "?limit=100",
                              headers=bb.get_headers())
            assert r.status == 200, r.raw[:300]
            listed = {str(i["item_id"]) for i in r.json["items"]}
            assert {k["item_id"] for k in kinds.values()} <= listed
            for kind, k in kinds.items():
                for name in ("get", "history"):
                    r = await bb.asgi(app, "GET", bb.path(name, k["item_id"]),
                                      headers=bb.get_headers())
                    assert r.status == 200, (kind, name, r.raw[:300])
                    bb.assert_labelled(r)
            k = kinds["ehr_confirmation_check"]
            r = await bb.asgi(app, "GET", bb.path("reason", k["item_id"]),
                              headers=bb.get_headers())
            assert r.status == 200, r.raw[:300]
            bb.assert_labelled(r)

    run(body())


def test_b3b_r5_r6_claim_then_resolve_every_kind(inst: b3.Install) -> None:
    """A (R-B3b-1): claim (act) then resolve each of the five kinds through the routes; each
    returns production's {"outcome", "snapshot"} plus the label; the item rows end resolved."""
    kinds = bb.seed_kinds(inst, base=30)

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            for kind, k in kinds.items():
                a = await bb.asgi(app, "POST", bb.path("act", k["item_id"]),
                                  headers=bb.post_headers(token),
                                  body=bb.jbody({"client_action_id": bb.new_uuid(),
                                                 "expected_version": 1, "type": "claim"}))
                assert (a.status, a.json["outcome"]) == (200, "applied"), (kind, a.raw[:300])
                assert set(a.json) == {"outcome", "snapshot", "simulated"}
                r = await bb.asgi(app, "POST", bb.path("resolve", k["item_id"]),
                                  headers=bb.post_headers(token),
                                  body=bb.jbody({"client_action_id": bb.new_uuid(),
                                                 "expected_version": 2, "type": "resolve",
                                                 "outcome_code": "reached_no_change"}))
                assert (r.status, r.json["outcome"]) == (200, "applied"), (kind, r.raw[:300])

    run(body())
    for k in kinds.values():
        assert bb.item_row(inst, k["item_id"])["state"] == "resolved"


def test_b3b_r7_not_served_paths_are_404(seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B3b-1): read_note, display, admin, the public ingress path and the production
    paths are not served (404, labelled), for GET and POST."""
    inst, _ = seeded

    async def body() -> set[int]:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            out = set()
            for p in bb.NOT_SERVED:
                for method in ("GET", "POST"):
                    r = await bb.asgi(app, method, p, body=b"{}" if method == "POST" else b"",
                                      headers=bb.post_headers(token) if method == "POST"
                                      else bb.get_headers())
                    bb.assert_labelled(r)
                    out.add(r.status)
            return out

    assert run(body()) == {404}


# The malformed corpus (R8): (route, query, body).
CORPUS: list[tuple[str, str, bytes]] = [
    ("list", "", b""), ("list", "?limit=0", b""), ("list", "?limit=101", b""),
    ("list", "?limit=5&states=nope", b""), ("list", "?limit=5&limit=6", b""),
    ("list", "?limit=5&x=1", b""), ("list", "?limit=5&after_created_at=2026-01-01T00:00:00Z",
                                    b""),
    ("get", "?x=1", b""), ("history", "?x=1", b""), ("reason", "?x=1", b""),
    ("act", "", b"[]"), ("act", "", b"{\"type\": \"claim\"}"), ("act", "", b"not json"),
    ("act", "", b"{\"client_action_id\": \"x\", \"expected_version\": 1, \"type\": \"claim\"}"),
    ("act", "", b"{\"client_action_id\": \"00000000-0000-4000-8000-000000000001\","
                b" \"expected_version\": 1, \"type\": \"claim\", \"extra\": 1}"),
    ("resolve", "", b"{\"client_action_id\": \"00000000-0000-4000-8000-000000000001\","
                    b" \"expected_version\": 1, \"type\": \"resolve\"}"),
    ("resolve", "", b"{\"client_action_id\": \"00000000-0000-4000-8000-000000000001\","
                    b" \"expected_version\": 1, \"type\": \"resolve\", \"outcome_code\": \"x\","
                    b" \"note\": 5}"),
    ("act", "", b"x" * (16 * 1024 + 1)),
]


def _production_expectation(name: str, item_id: str, query: str, raw: bytes) -> tuple[int, Any]:
    """What the PRODUCTION parser for the route's analogue answers for this request."""
    from starlette.requests import Request

    from haloflow.platform import routes as pr
    from haloflow.platform.http import malformed

    scope = {"type": "http", "method": bb.ROUTES[name][0], "path": "/x", "headers": [],
             "query_string": query.lstrip("?").encode(), "path_params": {"item_id": item_id}}
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def parse() -> None:
        req = Request(scope, receive)
        if name == "list":
            pr._list_query(req)
        elif name in ("get", "history", "reason"):
            pr._path_uuid(item_id, "item_id")
            pr._no_query(req)
        elif name == "act":
            pr._staff_action(uuid.UUID(item_id), await pr._json_object(req, pr.ACT_FIELDS))
        else:
            pr._resolve_action(uuid.UUID(item_id), await pr._json_object(req, pr.RESOLVE_FIELDS))

    try:
        asyncio.run(parse())
    except pr._Malformed as e:
        resp = malformed(e.field)
        return resp.status_code, json.loads(bytes(resp.body))
    return 0, None


def test_b3b_r8_malformed_corpus_parity_with_production(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (Q1 = a; R8 parity): each corpus request gets the same status and body (minus the
    label) on the synthetic route as the production parser gives."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]
    expected = [_production_expectation(n, iid, q, raw) for n, q, raw in CORPUS]
    assert all(e[0] == 422 for e in expected), expected  # every corpus entry is malformed

    async def body() -> list[tuple[int, Any]]:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            out = []
            for n, q, raw in CORPUS:
                method = bb.ROUTES[n][0]
                r = await bb.asgi(app, method, bb.path(n, iid) + q, body=raw,
                                  headers=bb.post_headers(token) if method == "POST"
                                  else bb.get_headers())
                bb.assert_labelled(r)
                out.append((r.status, {k: v for k, v in r.json.items() if k != "simulated"}))
            return out

    assert run(body()) == expected


def test_b3b_r9_r10_error_map_parity(inst: b3.Install) -> None:
    """A (R-B3b-3): an unknown item is production's 404 NOT_FOUND on get/history/reason/act;
    a stale expected_version on act is production's 409 code; both via error_response."""
    from haloflow.m05.service import ActionRefused
    from haloflow.platform.http import error_response

    kinds = bb.seed_kinds(inst, base=40)
    iid = kinds["reschedule_request"]["item_id"]
    unknown = bb.new_uuid()

    async def body() -> tuple[list[tuple[int, Any]], tuple[int, Any]]:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            nf = []
            for name in ("get", "history", "reason"):
                r = await bb.asgi(app, "GET", bb.path(name, unknown), headers=bb.get_headers())
                nf.append((r.status, r.json.get("code")))
            r = await bb.asgi(app, "POST", bb.path("act", unknown),
                              headers=bb.post_headers(token),
                              body=bb.jbody({"client_action_id": bb.new_uuid(),
                                             "expected_version": 1, "type": "claim"}))
            nf.append((r.status, r.json.get("code")))
            stale = await bb.asgi(app, "POST", bb.path("act", iid),
                                  headers=bb.post_headers(token),
                                  body=bb.jbody({"client_action_id": bb.new_uuid(),
                                                 "expected_version": 7, "type": "claim"}))
            return nf, (stale.status, stale.json.get("code"))

    nf, stale = run(body())
    assert set(nf) == {(404, "NOT_FOUND")}, nf
    assert stale[0] == 409
    want = error_response(ActionRefused(stale[1]))
    assert want.status_code == 409 and json.loads(bytes(want.body))["code"] == stale[1]


def test_b3b_r11_resolution_in_history_and_restart_passes_seal(inst: b3.Install) -> None:
    """A: after claim + resolve through the routes, history shows the resolution; a new
    lifecycle generation then starts (reduced seal accepts the rows the routes wrote)."""
    kinds = bb.seed_kinds(inst, base=50)
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> Any:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            for v, payload in ((1, {"type": "claim"}),
                               (2, {"type": "resolve", "outcome_code": "reached_no_change"})):
                name = "act" if payload["type"] == "claim" else "resolve"
                r = await bb.asgi(app, "POST", bb.path(name, iid),
                                  headers=bb.post_headers(token),
                                  body=bb.jbody({"client_action_id": bb.new_uuid(),
                                                 "expected_version": v, **payload}))
                assert r.status == 200, r.raw[:300]
            h = await bb.asgi(app, "GET", bb.path("history", iid), headers=bb.get_headers())
            return h.json

    hist = run(body())
    assert "reached_no_change" in json.dumps(hist)
    bb.first_start(inst)


# =========================================================================== S: simulator

def _reply_body(phone: str, text: str) -> bytes:
    return bb.jbody({"phone": phone, "text": text})


@pytest.mark.parametrize("n", [0, 23, 99])
def test_b3b_s1_patient_reply_matches_the_direct_ingress_twin(pg: s.PgCluster, tmp_path: Path,
                                                              n: int) -> None:
    """A (R-B3b-6; v1r1 §2): for a reminder sent to syn_phone(n), a reply through the
    simulator gives the same status, outcome and decision, and an equal inbound row (minus
    ids and times), as the same text posted directly to the S-G1a router on a TWIN install
    seeded identically."""
    (tmp_path / "a").mkdir()
    a = b3.install(pg, tmp_path / "a")
    bb.first_start(a)
    phone = bb.syn_phone(n)
    bb.sent_only(a, phone)
    with s.cluster(tmp_path / "twin") as c2:
        (tmp_path / "b").mkdir()
        b = b3.install(c2, tmp_path / "b")
        bb.first_start(b)
        bb.sent_only(b, phone)
        d_status, d_out = bb.direct_reply(b, phone, "2")
        twin_rows = bb.inbound_rows(b, phone)

    async def body() -> bb.Resp:
        async with bb.running(a) as (_lc, app):
            token = await bb.session_token(app)
            return await bb.asgi(app, "POST", bb.path("sim_reply"),
                                 headers=bb.post_headers(token), body=_reply_body(phone, "2"))

    r = run(body())
    bb.assert_labelled(r)
    assert (r.status, r.json["outcome"], r.json["decision"]) == (
        d_status, d_out["outcome"], d_out["decision"])
    drop = {"inbound_id", "provider_message_id", "received_at", "fingerprint", "patient_id",
            "appointment_ref", "item_id", "event_id", "created_at"}
    def norm(row: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in row.items() if k not in drop}
        # install-specific identifiers: compare their shape, not their value
        out["reminder_id"] = row.get("reminder_id") is not None
        matched = row.get("matched_reminders")
        out["matched_reminders"] = len(matched) if isinstance(matched, list) else matched
        return out

    mine = [norm(row) for row in bb.inbound_rows(a, phone)]
    twin = [norm(row) for row in twin_rows]
    assert len(mine) == len(twin) == 1
    assert mine == twin, {k for k in mine[0] if mine[0].get(k) != twin[0].get(k)}


def test_b3b_s2_reply_goes_through_the_ingress_handler(inst: b3.Install,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """A (R-B3b-6; mutant 10): one simulator reply calls the ingress `authenticate` once and
    ReplyIntake.receive once, in that order (no direct call to receive)."""
    from haloflow.m05 import reply_classifier, synthetic_ingress

    phone = bb.syn_phone(31)
    bb.sent_only(inst, phone)
    order: list[str] = []
    real_auth = synthetic_ingress.authenticate
    real_recv = reply_classifier.ReplyIntake.receive

    def auth(*a: Any, **k: Any) -> Any:
        order.append("authenticate")
        return real_auth(*a, **k)

    async def recv(self: Any, *a: Any, **k: Any) -> Any:
        order.append("receive")
        return await real_recv(self, *a, **k)

    monkeypatch.setattr(synthetic_ingress, "authenticate", auth)
    monkeypatch.setattr(reply_classifier.ReplyIntake, "receive", recv)

    async def body() -> int:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            r = await bb.asgi(app, "POST", bb.path("sim_reply"),
                              headers=bb.post_headers(token), body=_reply_body(phone, "1"))
            return r.status

    assert run(body()) == 202
    assert order == ["authenticate", "receive"]


@pytest.mark.parametrize("phone", ["+15555550200", "+15555550099", "+15550100001",
                                   "+12125550123", "5555550123", "+1 555 555 0123",
                                   "+155555501234", 5555550123, None])
def test_b3b_s3_non_canonical_phone_refused_before_ingress(
        seeded: tuple[b3.Install, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
        phone: Any) -> None:
    """A (v1r1 §2; mutant 12): 422 MALFORMED field=phone, labelled, and the ingress is never
    reached (authenticate not called)."""
    from haloflow.m05 import synthetic_ingress

    inst, _ = seeded
    calls: list[int] = []
    monkeypatch.setattr(synthetic_ingress, "authenticate",
                        lambda *a, **k: calls.append(1))

    async def body() -> bb.Resp:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            return await bb.asgi(app, "POST", bb.path("sim_reply"),
                                 headers=bb.post_headers(token),
                                 body=bb.jbody({"phone": phone, "text": "1"}))

    r = run(body())
    bb.assert_labelled(r)
    assert (r.status, r.json["code"], r.json["field"]) == (422, "MALFORMED", "phone")
    assert calls == []


def test_b3b_s4_ingress_secret_never_leaks_and_rotates(
        seeded: tuple[b3.Install, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """A (R-B3b-6): the per-lifespan ingress secret (captured where IngressConfig.build
    receives it) is >= 48 bytes, differs between two lifespans, and appears in no log line,
    response or the state file."""
    from haloflow.m05 import synthetic_ingress

    inst, kinds = seeded
    caplog.set_level(logging.DEBUG)
    secrets: list[str] = []
    real = synthetic_ingress.IngressConfig.build.__func__  # type: ignore[attr-defined]

    def build(cls: Any, mapping: Any, served: Any) -> Any:
        secrets.extend(mapping.values())
        return real(cls, mapping, served)

    monkeypatch.setattr(synthetic_ingress.IngressConfig, "build", classmethod(build))
    raws: list[bytes] = []

    async def one() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            for p in (kinds["unclear_reply"]["phone"], "+15555550200"):
                r = await bb.asgi(app, "POST", bb.path("sim_reply"),
                                  headers=bb.post_headers(token), body=_reply_body(p, "1"))
                raws.append(r.raw + json.dumps(r.headers).encode())

    run(one())
    run(one())
    assert len(secrets) == 2 and secrets[0] != secrets[1]
    assert all(len(x.encode()) >= 48 for x in secrets)
    state = (inst.st_dir / "state.json").read_text()
    for x in secrets:
        assert x not in caplog.text and x not in state
        assert all(x.encode() not in raw for raw in raws)


def test_b3b_s5_ingress_route_not_on_the_public_app(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (Q3; mutant 11): POST /v1/synthetic/inbound-sms with a Bearer header is 404 on the
    public app (the ingress app is unmounted)."""
    inst, kinds = seeded

    async def body() -> int:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            r = await bb.asgi(app, "POST", "/v1/synthetic/inbound-sms",
                              headers=bb.post_headers(token, extra=[
                                  ("authorization", "Bearer " + "x" * 48)]),
                              body=bb.jbody({"channel": "sms", "provider_message_id": "m1",
                                             "phone": kinds["unclear_reply"]["phone"],
                                             "text": "1"}))
            bb.assert_labelled(r)
            return r.status

    assert run(body()) == 404


def test_b3b_s6_ehr_put_is_read_through_the_port(inst: b3.Install) -> None:
    """A (R-B3b-7): put_appointment changes the SAME SimulatedEhr object the port wraps (by
    identity), and the next port read returns that appointment; the body says process_memory."""
    async def body() -> tuple[bb.Resp, Any, bool]:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            ehr = lc.port.ehr
            r = await bb.asgi(app, "POST", bb.path("sim_ehr"), headers=bb.post_headers(token),
                              body=bb.jbody({"operation": "put_appointment",
                                             "appointment_id": "appt-s6", "status": "neutral",
                                             "start_at": "2026-12-01T15:00:00+00:00"}))
            read = await lc.port.read("appt-s6")
            return r, read, lc.port.ehr is ehr

    r, read, same = run(body())
    bb.assert_labelled(r)
    assert r.status == 200 and r.json["ehr_state"] == "process_memory"
    assert same and read.status == "neutral" and read.appointment_id == "appt-s6"


def test_b3b_s7_fail_next_read(inst: b3.Install) -> None:
    """A (R-B3b-7): fail_next reads=1 makes exactly the next port read EhrUnavailable."""
    from haloflow.m05.simulated_ehr import EhrRead, EhrUnavailable

    async def body() -> list[str]:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            from datetime import UTC, datetime
            lc.port.ehr.put(EhrRead("neutral", "appt-s7", datetime(2026, 12, 1, tzinfo=UTC),
                                    None, None, None))
            r = await bb.asgi(app, "POST", bb.path("sim_ehr"), headers=bb.post_headers(token),
                              body=bb.jbody({"operation": "fail_next", "reads": 1,
                                             "writes": 0}))
            assert r.status == 200
            out = []
            for _ in range(2):
                try:
                    await lc.port.read("appt-s7")
                    out.append("ok")
                except EhrUnavailable:
                    out.append("unavailable")
            return out

    assert run(body()) == ["unavailable", "ok"]


@pytest.mark.parametrize("payload", [
    {"operation": "put_appointment", "appointment_id": "a", "status": "neutral",
     "start_at": "2026-12-01T15:00:00"},                                   # naive datetime
    {"operation": "put_appointment", "appointment_id": "a", "status": "maybe",
     "start_at": "2026-12-01T15:00:00+00:00"},                             # unknown status
    {"operation": "put_appointment", "appointment_id": "a", "status": "neutral",
     "start_at": "2026-12-01T15:00:00+00:00", "extra": 1},                 # unknown key
    {"operation": "fail_next", "reads": 11, "writes": 0},                  # out of range
    {"operation": "fail_next", "reads": -1, "writes": 0},
    {"operation": "fail_next", "reads": True, "writes": 0},                # bool is not int
    {"operation": "delete", "appointment_id": "a"},                        # unknown operation
    [],
])
def test_b3b_s8_malformed_ehr_body_changes_nothing(seeded: tuple[b3.Install, dict[str, Any]],
                                                   payload: Any) -> None:
    """A (R-B3b-7): a malformed EHR body is 422 MALFORMED, labelled, and the simulated EHR
    snapshot is unchanged."""
    inst, _ = seeded

    async def body() -> tuple[bb.Resp, bool]:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            before = lc.port.ehr.snapshot()
            r = await bb.asgi(app, "POST", bb.path("sim_ehr"), headers=bb.post_headers(token),
                              body=bb.jbody(payload))
            return r, lc.port.ehr.snapshot() == before

    r, unchanged = run(body())
    bb.assert_labelled(r)
    assert (r.status, r.json["code"]) == (422, "MALFORMED") and unchanged


def test_b3b_s9_reply_writes_only_the_ingress_set_and_ehr_writes_nothing(
        pg: s.PgCluster, tmp_path: Path) -> None:
    """A (Codex v1r1 S9, two legs; mutant 16): (1) the view diff of one simulator reply
    equals the diff of the same reply posted directly to S-G1a on an identically seeded twin
    (the expected ingress writes only), and both leave sealed tables, permits, leases and
    deadline rows unchanged; (2) put_appointment and fail_next change no table."""
    sealed_watch = {"{t}.m05_writeback_permits", "{t}.m05_writeback_ops",
                    "{t}.m05_outbound_send_tickets", "{t}.m05_reminder_settings",
                    "{t}.m01_staff", "{t}.m05_activation_records"}

    def norm(inst: b3.Install, names: set[str]) -> set[str]:
        return {n.replace(inst.schema, "{t}") for n in names}

    phone = bb.syn_phone(44)
    (tmp_path / "a").mkdir()
    a = b3.install(pg, tmp_path / "a")
    bb.first_start(a)
    bb.sent_only(a, phone)
    with s.cluster(tmp_path / "twin") as c2:
        (tmp_path / "b").mkdir()
        b = b3.install(c2, tmp_path / "b")
        bb.first_start(b)
        bb.sent_only(b, phone)
        t0 = b3.table_digests(b)
        bb.direct_reply(b, phone, "3")
        twin_diff = norm(b, b3.changed(t0, b3.table_digests(b)))

    async def body() -> tuple[set[str], set[str]]:
        async with bb.running(a) as (_lc, app):
            token = await bb.session_token(app)
            v0 = b3.table_digests(a)
            r = await bb.asgi(app, "POST", bb.path("sim_reply"),
                              headers=bb.post_headers(token), body=_reply_body(phone, "3"))
            assert r.status == 202, r.raw[:300]
            reply_diff = b3.changed(v0, b3.table_digests(a))
            v1 = b3.table_digests(a)
            for payload in ({"operation": "put_appointment", "appointment_id": "appt-s9",
                             "status": "neutral", "start_at": "2026-12-01T15:00:00+00:00"},
                            {"operation": "fail_next", "reads": 2, "writes": 2}):
                r = await bb.asgi(app, "POST", bb.path("sim_ehr"),
                                  headers=bb.post_headers(token), body=bb.jbody(payload))
                assert r.status == 200
            return reply_diff, b3.changed(v1, b3.table_digests(a))

    reply_diff, ehr_diff = run(body())
    assert norm(a, reply_diff) == twin_diff and twin_diff
    assert not (twin_diff & sealed_watch)
    assert ehr_diff == set()


def test_b3b_s10_simulator_audit_lines(inst: b3.Install,
                                       caplog: pytest.LogCaptureFixture) -> None:
    """A (R-B3b-6/7): one INFO line per simulator action on haloflow.synthetic, carrying the
    action, status and code only: never the phone, the text or the appointment id."""
    phone = bb.syn_phone(55)
    bb.sent_only(inst, phone)
    caplog.set_level(logging.INFO, logger="haloflow.synthetic")

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            await bb.asgi(app, "POST", bb.path("sim_reply"), headers=bb.post_headers(token),
                          body=_reply_body(phone, "unique-text-s10"))
            await bb.asgi(app, "POST", bb.path("sim_ehr"), headers=bb.post_headers(token),
                          body=bb.jbody({"operation": "put_appointment",
                                         "appointment_id": "appt-secret-s10",
                                         "status": "neutral",
                                         "start_at": "2026-12-01T15:00:00+00:00"}))

    run(body())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIMULATOR ")]
    assert lines == ["SIMULATOR action=patient_reply status=202 code=-",
                     "SIMULATOR action=ehr_put_appointment status=200"], lines
    for secret in (phone, "unique-text-s10", "appt-secret-s10"):
        assert secret not in caplog.text


def _gen2_tick(inst: b3.Install, *, app_name: str, b3b: bool,
               noop: bool = False) -> tuple[int, Any, list[tuple[str, str]], int]:
    """Start a generation (through the B3b app when `b3b`, else the B3a lifecycle alone), spy
    every simulated-EHR read the port makes, run ONE write-back tick, stop. Returns (EHR
    appointments at start, tick result, reads, EHR writes). `noop`: the tick is replaced by a
    no-op (the negative control: the oracle must reject it)."""
    async def body() -> tuple[int, Any, list[tuple[str, str]], int]:
        lc = b3.mod("lifecycle").SyntheticLifecycle(inst.env, application_name=app_name)
        await lc.start()
        try:
            if b3b:
                b3.mod("app").create_synthetic_app(lifecycle=lc, bind=bb.BIND)
            empty = len(lc.port.ehr.snapshot().appointments)
            reads: list[tuple[str, str]] = []
            real = lc.port.ehr.read

            async def spy(appointment_id: str) -> Any:
                try:
                    found = await real(appointment_id)
                except Exception as e:
                    reads.append((appointment_id, type(e).__name__))
                    raise
                reads.append((appointment_id, "ok"))
                return found

            lc.port.ehr.read = spy
            if noop:
                async def nothing() -> Any:
                    return None

                lc.schedulers["writeback"].tick = nothing
            result = await asyncio.wait_for(lc.schedulers["writeback"].tick(), 120)
            return empty, result, reads, len(lc.port.ehr.writes)
        finally:
            lc.request_stop("TEST")
            await lc.wait_stopped()

    return run(body())


def _s11_oracle(inst: b3.Install, seed: dict[str, Any], empty: int, result: Any,
                reads: list[tuple[str, str]], writes: int) -> None:
    """Codex red gate 55c17fc: an OBSERVED read of the seeded appointment yielding
    EhrUnavailable, and the op-specific first step of the accepted retry policy (R1 failure 1:
    deferred, state pending, r1_failures 1, next_at = now + RETRY_DELAYS_S[0]); never
    observed_positive; no write. A no-op tick fails here."""
    from haloflow.m05.writeback import RETRY_DELAYS_S

    assert empty == 0, "generation 2 inherited simulated EHR state"
    assert reads == [(seed["appointment_id"], "EhrUnavailable")], reads
    assert result is not None, "no tick ran"
    (report,) = result.tenants
    assert report.discover.counts.get("discovered") == 1, report
    assert report.run.counts.get("deferred") == 1, report
    assert report.run.counts.get("observed_positive") == 0, report
    ops = inst.admin(f"SELECT state, r1_failures, reason,"
                     f" extract(epoch from next_at - updated_at)::int"
                     f" FROM {inst.schema}.m05_writeback_ops")
    assert ops == [("pending", 1, None, RETRY_DELAYS_S[0])], ops
    assert writes == 0


def test_b3b_s11c_oracle_holds_on_the_b3a_lifecycle(inst: b3.Install) -> None:
    """C (red gate 55c17fc): the S11 oracle passes for the accepted worker on the B3a
    lifecycle alone (premise measured: discovered 1, deferred 1, the seeded read raised
    EhrUnavailable, op pending r1_failures 1, next_at + 60 s)."""
    seed = b3.seed_recovery(inst, "r2")[0]
    _s11_oracle(inst, seed, *_gen2_tick(inst, app_name="b3b-s11c", b3b=False))


def test_b3b_s11n_noop_tick_fails_the_oracle(inst: b3.Install) -> None:
    """C (negative control, red gate 55c17fc): with the write-back tick replaced by a no-op,
    the S11 oracle rejects the run (no read, no tick result, no deferral)."""
    seed = b3.seed_recovery(inst, "r2")[0]
    observed = _gen2_tick(inst, app_name="b3b-s11n", b3b=False, noop=True)
    with pytest.raises(AssertionError):
        _s11_oracle(inst, seed, *observed)


def test_b3b_s11_ehr_state_is_process_memory_across_restart(inst: b3.Install) -> None:
    """A (Codex Q2 = a, explicit restart behaviour; red gate 55c17fc): generation 1 puts the
    seeded appointment into the simulated EHR through the simulator and stops; generation 2,
    composed through the B3b app, starts with a NEW, empty SimulatedEhr; its write-back tick
    reads that appointment, gets EhrUnavailable, and takes the accepted retry step (deferred;
    pending, r1_failures 1, next_at + RETRY_DELAYS_S[0]): never observed_positive, no write."""
    seed = b3.seed_recovery(inst, "r2")[0]

    async def gen1() -> tuple[int, int]:
        async with bb.running(inst, app_name="b3b-g1") as (lc, app):
            token = await bb.session_token(app)
            r = await bb.asgi(app, "POST", bb.path("sim_ehr"), headers=bb.post_headers(token),
                              body=bb.jbody({"operation": "put_appointment", **seed}))
            return r.status, len(lc.port.ehr.snapshot().appointments)

    assert run(gen1()) == (200, 1)
    _s11_oracle(inst, seed, *_gen2_tick(inst, app_name="b3b-g2", b3b=True))


# =========================================================================== L: labels

def test_b3b_l1_every_route_labelled_on_success_and_refusal(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (B-L1; mutant 13): every route, on success and on each refusal reachable for it (gate,
    malformed, not found), carries the label header and simulated: true."""
    inst, kinds = seeded
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> int:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            n = 0
            for name, (method, _p) in bb.ROUTES.items():
                cases: list[tuple[str, list[tuple[str, str]], bytes]] = [
                    (bb.path(name, iid) + ("?limit=5" if name == "list" else ""),
                     bb.post_headers(token) if method == "POST" else bb.get_headers(), b"{}"
                     if method == "POST" else b""),
                    (bb.path(name, bb.new_uuid()), bb.get_headers(host="evil:1"), b""),
                ]
                if method == "POST":
                    cases.append((bb.path(name, iid), bb.post_headers("bad"), b"{}"))
                    cases.append((bb.path(name, iid), bb.post_headers(token), b"[]"))
                for target, hdrs, raw in cases:
                    r = await bb.asgi(app, method, target, headers=hdrs, body=raw)
                    bb.assert_labelled(r)
                    n += 1
            return n

    assert run(body()) == 28   # 10 routes x 2 cases + 4 POST routes x 2 more


def test_b3b_l3_payload_with_simulated_key_is_500(seeded: tuple[b3.Install, dict[str, Any]]
                                                  ) -> None:
    """A (R-B3b-5): a service payload that already holds `simulated` is a defect: 500
    INTERNAL, labelled (the wrapper never overwrites)."""
    inst, kinds = seeded

    async def body() -> bb.Resp:
        async with bb.running(inst) as (lc, app):
            async def bad(*_a: Any, **_k: Any) -> dict[str, Any]:
                return {"simulated": False, "item": {}}

            lc.services.m05.get = bad
            return await bb.asgi(app, "GET", bb.path("get", kinds["unclear_reply"]["item_id"]),
                                 headers=bb.get_headers())

    r = run(body())
    assert (r.status, r.json["code"]) == (500, "INTERNAL") and r.json["simulated"] is True


@pytest.mark.parametrize("where", ["gate", "encode"])
def test_b3b_l4_middleware_fault_is_labelled_500(seeded: tuple[b3.Install, dict[str, Any]],
                                                 where: str) -> None:
    """A (Codex v1r1; mutant 13b): a fault raised inside the gate itself (the test-only hook
    `b3b.gate.enter`) and one inside the label wrapper's JSON encoding (a payload holding a
    non-encodable value) each give 500 {"code":"INTERNAL","simulated":true} with the label
    header; the fault text never appears."""
    inst, kinds = seeded

    async def body() -> bb.Resp:
        async with bb.running(inst) as (lc, app):
            if where == "gate":
                def fault(point: str, _info: Any) -> None:
                    if point == "b3b.gate.enter":
                        raise RuntimeError("FAULT-TEXT-L4")
                s.mod("hooks").FAULT = fault
            else:
                async def bad(*_a: Any, **_k: Any) -> dict[str, Any]:
                    return {"item": {"x": object()}, "note": "FAULT-TEXT-L4"}

                lc.services.m05.get = bad
            try:
                return await bb.asgi(app, "GET",
                                     bb.path("get", kinds["unclear_reply"]["item_id"]),
                                     headers=bb.get_headers())
            finally:
                s.mod("hooks").FAULT = None

    r = run(body())
    assert (r.status, r.json) == (500, {"code": "INTERNAL", "simulated": True})
    assert r.header(bb.LABEL_HEADER) == ["true"] and b"FAULT-TEXT-L4" not in r.raw


# =========================================================================== D: HTTP drain

class HoldHooks:
    """b3.stop.* order with the in-flight count observed at each point."""

    def __init__(self, lc: Any) -> None:
        self.lc = lc
        self.seen: list[tuple[str, int]] = []
        s.mod("hooks").FAULT = self

    def __call__(self, point: str, _info: Any) -> None:
        if point.startswith("b3.stop."):
            self.seen.append((point, int(self.lc.http_in_flight)))


def test_b3b_d1_held_request_is_awaited_before_the_port(inst: b3.Install) -> None:
    """A (R-B3b-8; mutant 14): a request parked inside the service call when the stop is
    requested, and released within t_http, completes (200) before b3.stop.port; the order is
    producers, http, port, ...; in-flight is 0 at the port; the drain ends drained."""
    kinds = bb.seed_kinds(inst, base=60)
    iid = kinds["unclear_reply"]["item_id"]

    async def body() -> tuple[list[tuple[str, int]], int, Any]:
        async with bb.running(inst) as (lc, app):
            gate = asyncio.Event()
            real = lc.services.m05.get

            async def held(*a: Any, **k: Any) -> Any:
                await gate.wait()
                return await real(*a, **k)

            lc.services.m05.get = held
            h = HoldHooks(lc)
            req = asyncio.create_task(bb.asgi(app, "GET", bb.path("get", iid),
                                              headers=bb.get_headers()))
            await bb_wait(lambda: lc.http_in_flight == 1)
            lc.request_stop("TEST")
            await asyncio.sleep(0.3)
            gate.set()
            r = await req
            result = await lc.wait_stopped()
            return h.seen, r.status, result

    seen, status, result = run(body())
    points = [p for p, _ in seen]
    assert points[:3] == ["b3.stop.watcher", "b3.stop.producers", "b3.stop.http"], points
    assert points.index("b3.stop.http") < points.index("b3.stop.port")
    assert dict(seen)["b3.stop.port"] == 0
    assert status == 200 and result.disposition == "drained"


def test_b3b_d1b_held_body_stream_is_counted(inst: b3.Install) -> None:
    """A (Codex v1r1; mutant 14c): a request whose response body is held by the client
    (send parked) keeps the in-flight count at 1; the http step waits for it; released within
    t_http, the stop drains normally."""
    async def body() -> tuple[int, int, Any]:
        async with bb.running(inst) as (lc, app):
            hold = asyncio.Event()
            req = asyncio.create_task(bb.asgi(app, "GET", "/synthetic/session",
                                              headers=bb.get_headers(), hold_body=hold))
            await bb_wait(lambda: lc.http_in_flight == 1)
            lc.request_stop("TEST")
            await asyncio.sleep(0.5)
            during = int(lc.http_in_flight)
            hold.set()
            r = await req
            result = await lc.wait_stopped()
            return during, r.status, result

    during, status, result = run(body())
    assert during == 1 and status == 200 and result.disposition == "drained"


def test_b3b_d3_after_stop_request_new_requests_are_503(inst: b3.Install) -> None:
    """A (C-6): from the stop request on, every route except health answers 503 with the
    lifecycle's closed code at once (no service call); health keeps its B3a behaviour."""
    async def body() -> list[tuple[str, int, Any]]:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            spy = Spy(lc.services.m05, M05_CALLS)
            lc.request_stop("TEST")
            out = []
            for name, (method, _p) in bb.ROUTES.items():
                r = await bb.asgi(app, method, bb.path(name, bb.new_uuid()) +
                                  ("?limit=5" if name == "list" else ""),
                                  headers=bb.post_headers(token) if method == "POST"
                                  else bb.get_headers(), body=b"{}" if method == "POST" else b"")
                out.append((name, r.status, (r.json or {}).get("refused") or
                            (r.json or {}).get("code")))
            assert spy.calls == []
            await lc.wait_stopped()
            return out

    for name, status, code in run(body()):
        assert status == 503 and code == "RUNTIME_STOPPED", (name, status, code)


@pytest.mark.parametrize("mode", ["no_db", "in_txn"])
def test_b3b_d2_unconfirmed_http_drain_terminal_through_a_child(inst: b3.Install,
                                                                tmp_path: Path,
                                                                mode: str) -> None:
    """A (v1r1 §1 D2 / D2b; Codex: K_S probed before and after child exit, rollback from a
    fresh session; mutant 14b): a child process runs the app with a request held past t_http
    (no_db: parked before any DB checkout; in_txn: parked inside the gateway transaction
    after the claim statements, before commit). The child prints exactly one line
    `fatal: HTTP_NOT_DRAINED http` and exits 3; it never reaches b3.stop.port/pool/quiesce/
    lease; while it is paused at b3.stop.http the exclusive K_S is NOT available from a
    separate session; after it exits K_S becomes available (process exit freed it). in_txn:
    a fresh admin session sees the item unclaimed (version 1, no action row)."""
    kinds = bb.seed_kinds(inst, base=70)
    iid = kinds["unclear_reply"]["item_id"]
    child = bb_spawn(inst, tmp_path / "bar", mode=mode, item_id=iid)
    child.reached("b3.stop.http", 180)
    assert not b3.ks_free(inst), "lease released before the http step resolved"
    child.go("b3.stop.http")
    rc, out = child.wait(120)
    assert rc == 3, out[-1500:]
    assert [ln for ln in out.splitlines() if ln.startswith("fatal: ")] == \
        ["fatal: HTTP_NOT_DRAINED http"]
    for p in ("b3.stop.port", "b3.stop.pool", "b3.stop.quiesce", "b3.stop.lease"):
        assert not (child.bar / f"{p}.seen").exists(), p
    b3.assert_gone(inst, child)
    if mode == "in_txn":
        row = bb.item_row(inst, iid)
        assert (row["state"], row["version"]) == ("open", 1), row
        acts = inst.admin(f"SELECT count(*) FROM {inst.schema}.m05_followup_actions"
                          " WHERE item_id = %s::uuid", (iid,))
        assert acts[0][0] == 0


# =========================================================================== N: no network

def test_b3b_n1_no_network_with_every_route_exercised(inst: b3.Install,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A (R-B3b-10): a full lifespan, one request to every route through the raw ASGI driver,
    then a stop, with non-AF_UNIX sockets and the httpx / requests constructors patched to
    raise: zero calls; STOPPED."""
    import socket

    kinds = bb.seed_kinds(inst, base=80)
    iid = kinds["unclear_reply"]["item_id"]
    real = socket.socket.__init__
    calls: list[Any] = []

    def guarded(self: Any, family: int = socket.AF_INET, *a: Any, **k: Any) -> None:
        if family != socket.AF_UNIX:
            calls.append(family)
            raise AssertionError("network socket created")
        real(self, family, *a, **k)

    monkeypatch.setattr(socket.socket, "__init__", guarded)
    for mod_name, attr in (("httpx", "Client"), ("httpx", "AsyncClient"),
                           ("requests", "Session")):
        with contextlib.suppress(ImportError):
            m = __import__(mod_name)
            monkeypatch.setattr(m, attr, lambda *a, _n=attr, **k: calls.append(_n))

    async def body() -> str:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            for name, (method, _p) in bb.ROUTES.items():
                await bb.asgi(app, method, bb.path(name, iid) +
                              ("?limit=5" if name == "list" else ""),
                              headers=bb.post_headers(token) if method == "POST"
                              else bb.get_headers(),
                              body=_reply_body(kinds["unclear_reply"]["phone"], "1")
                              if name == "sim_reply" else b"{}" if method == "POST" else b"")
            lc.request_stop("TEST")
            await lc.wait_stopped()
            return str(lc.state)

    assert run(body()) == "STOPPED" and calls == []


# =========================================================================== helpers

async def bb_wait(pred: Any, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def bb_spawn(inst: b3.Install, bar: Path, *, mode: str, item_id: str) -> b3.B3Child:
    """Run `b3b_child.py` (fresh `python -I`): the app over a lifecycle with t_http=1, one
    request held (mode), then request_stop with the DEFAULT fatal_exit (real process exit);
    paused at b3.stop.http until <bar>/b3.stop.http.go."""
    import os
    import subprocess
    import sys

    bar.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"bar": str(bar), "environ": inst.env, "app": "b3b-child",
                          "mode": mode, "item_id": item_id})
    env = {"PATH": os.environ.get("PATH", ""),
           "PYTHONPATH": f"{s.SRC}{os.pathsep}{b3.HERE}{os.pathsep}{b3.M05}"}
    proc = subprocess.Popen([sys.executable, "-I", str(b3.HERE / "b3b_child.py"), payload],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    s.CHILDREN.append(proc)
    return b3.B3Child(proc, bar, "b3b-child")
