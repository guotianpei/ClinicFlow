"""Incoming fax routing FX-IN, package FX3a — two-tenant HARNESS rows (RED BY DESIGN).

HARNESS evidence (Codex e9c224a9: distinguish it from single-tenant install evidence): two FX2
tenants (target 144) in the shared test database, ONE composed runtime (real resolver, gateway,
FX3 catalogue), the real `FaxService`, the real test-side router `fax_ingress`, the real line fence
and directory. The single-tenant synthetic install rows are in
tests/synthetic/test_fx3a_app_postgres.py.
Governing: see `fx3a_support`. Class A unless marked C.

Rows (test-case ids v1r1 §3; trace in brackets):
- FX3-C00 CONTROL: the FX2 registry target is 144
- FX3-I01 no / wrong bearer -> 401 UNAUTHORIZED; no context_for / receive call; A and B ZSE [§3.4]
- FX3-I02 wrong media type -> 415; body over the cap with a lying Content-Length -> 413; ZSE
- FX3-I03 body carrying line / to / to_number / receiving_line -> 400 BODY_FIELD_FORBIDDEN (that
          field); an unknown field -> 400 MALFORMED; ZSE in both tenants [§3.1]
- FX3-I04 header line not in the directory -> 404 LINE_UNKNOWN; no context_for / receive [§3.4]
- FX3-I05 line1 (A) and line2 (B): each fax lands in its own tenant only; POSITIVE: A's staff list
          of the needs-sorting faxes shows A's fax (deliver -> staff read) [FR-1, FR-10]
- FX3-I06 a duplicate line injected (test-only) into A and B -> the directory build refuses
          LINE_DUPLICATE [§3.3]
- FX3-I07 (a) control: an observed 200 needs_sorting, then the same delivery -> 200 duplicate;
          (b) v1r1 (Codex f56c079): the provider LOSES the acknowledgement after commit (transport
          fails at response start) and retries its original identity -> 200 duplicate of the
          committed fax; per provider id one fax, one sorting item, one receipt history [§5.4]
- FX3-I08 IN-FLIGHT (Codex P07): R1 for line1 (owner A) paused by the after_lookup seam; the fence
          connection is terminated; the provisioner reassigns line1 A->B; R1 released -> 409
          LINE_NOT_REGISTERED, NO fax in A or B; the old router refuses the next delivery 503
          FENCE_LOST; a fresh fence + directory + router delivers line1 once, to B [§0]
- FX3-S01 statement authorization with a READ-ONLY staff principal: open applies (history
          `opened`); act (move) -> CapabilityDenied STATEMENT_CAPABILITY_DENIED, ZSE; the same
          move with read+act applies [Codex e9c224a9]
- FX3-S02 the open key cannot be turned into a mutation: OPEN_KEY called with action "move", a
          queue and a version -> COMMAND_MALFORMED (the trusted right-hand `open` wins), no change;
          the same command without those keys -> applied as `opened` only [Codex e9c224a9]
- FX3-S03 the lines and receive keys refuse staff contexts (CapabilityDenied); FaxService.lines /
          receive refuse a context whose capabilities are not exactly theirs (CAPABILITIES_INVALID)
- FX3-H04 v1r1 (Codex f56c079): DISTINCT lines (A line1, B line2); B's fax arrives through the
          real ingress on line2; through the ACTUAL staff routes (fax_staff_routes) B reads it
          (positive control); A's staff -> 404 NOT_FOUND on get / history / open / actions; A and
          B ZSE; B still reads it [FR-10]
- FX3-G01 no sender, line, doc_ref or provider id in any log record of an I05 delivery [FR-11]
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import fx1_support as fx
import fx2_support as f2
import fx3a_support as f3
import m02_support
import psycopg
import pytest
import sa_support as sa

pytestmark = [pytest.mark.postgres]


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Any:
    yield from f3.module_install(request, m02_ids, m05_reset, f"fx3a:{request.node.name}")


@pytest.fixture
def install_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
              m05_reset: Any) -> Any:
    yield from f3.module_install(request, m02_ids, m05_reset, f"fx3ab:{request.node.name}")


class Calls:
    def __init__(self) -> None:
        self.contexts = 0
        self.receives = 0


async def _stack(rt: Any, installs: list[Any], *, calls: Calls | None = None,
                 after_lookup: Any = None, fence: Any = None) -> tuple[Any, Any]:
    """(router app, fence) over the real service, directory and fence for these tenants."""
    from fastapi import FastAPI

    ing = f3.m("fax_ingress")
    svc = f3.service(rt)
    by_tenant = {i.tenant_id: i for i in installs}
    calls = calls or Calls()

    async def lines_for(tenant: str) -> Any:
        return await svc.lines(await f3.lines_ctx(rt, by_tenant[tenant]))

    async def context_for(tenant: str) -> Any:
        calls.contexts += 1
        return await f3.ingress_ctx(rt, by_tenant[tenant])

    async def receive(context: Any, fax: Any) -> Any:
        calls.receives += 1
        return await svc.receive(context, fax)

    directory = await ing.build_directory(fence, list(by_tenant), lines_for)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(ing.create_fax_ingress_router(
        ing.FaxIngressConfig.build(f3.SECRET), directory, fence, context_for, receive,
        after_lookup=after_lookup))
    return app, calls


def _jbytes(value: Any) -> bytes:
    return json.dumps(value).encode()


def _zse(*installs: Any) -> list[Any]:
    return [fx.snapshot(i) for i in installs]


def test_fx3_c00_fx2_target() -> None:
    """C."""
    assert f2.fx2_registry().target_version == f3.FX3_TARGET


def test_fx3_i01_bearer(install: Any, install_b: Any) -> None:
    """A."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE2)

    async def body() -> None:
        async with f3.runtime(install) as rt, \
                f3.m("fax_ingress").hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            calls = Calls()
            app, _ = await _stack(rt, [install, install_b], calls=calls, fence=fence)
            before = _zse(install, install_b)
            for secret in (None, "x" * 40):
                status, out = await f3.request(app, _jbytes(f3.body()), secret=secret)
                assert (status, out.get("code")) == (401, "UNAUTHORIZED")
            assert (calls.contexts, calls.receives) == (0, 0)
            assert _zse(install, install_b) == before

    asyncio.run(body())


def test_fx3_i02_media_and_size(install: Any, install_b: Any) -> None:
    """A."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE2)

    async def body() -> None:
        async with f3.runtime(install) as rt, \
                f3.m("fax_ingress").hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            app, calls = await _stack(rt, [install, install_b], fence=fence)
            before = _zse(install, install_b)
            status, _ = await f3.request(app, _jbytes(f3.body()), content_type="text/plain")
            assert status == 415
            big = b'{"x": "' + b"a" * 5000 + b'"}'
            status, _ = await f3.request(app, big, content_length="10")
            assert status == 413
            assert (calls.contexts, calls.receives) == (0, 0)
            assert _zse(install, install_b) == before

    asyncio.run(body())


@pytest.mark.parametrize(("field", "code"), [
    ("line", "BODY_FIELD_FORBIDDEN"), ("to", "BODY_FIELD_FORBIDDEN"),
    ("to_number", "BODY_FIELD_FORBIDDEN"), ("receiving_line", "BODY_FIELD_FORBIDDEN"),
    ("extra", "MALFORMED"),
])
def test_fx3_i03_body_fields(install: Any, install_b: Any, field: str, code: str) -> None:
    """A (§3.1: the line never comes from the body)."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE2)

    async def body() -> None:
        async with f3.runtime(install) as rt, \
                f3.m("fax_ingress").hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            app, calls = await _stack(rt, [install, install_b], fence=fence)
            before = _zse(install, install_b)
            status, out = await f3.request(app, _jbytes(f3.body(**{field: fx.LINE2})))
            assert (status, out) == (400, {"code": code, "field": field})
            assert (calls.contexts, calls.receives) == (0, 0)
            assert _zse(install, install_b) == before

    asyncio.run(body())


def test_fx3_i04_line_unknown(install: Any, install_b: Any) -> None:
    """A."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE2)

    async def body() -> None:
        async with f3.runtime(install) as rt, \
                f3.m("fax_ingress").hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            app, calls = await _stack(rt, [install, install_b], fence=fence)
            before = _zse(install, install_b)
            status, out = await f3.request(app, _jbytes(f3.body()), line="+15405559999")
            assert (status, out) == (404, {"code": "LINE_UNKNOWN"})
            assert (calls.contexts, calls.receives) == (0, 0)
            assert _zse(install, install_b) == before

    asyncio.run(body())


def test_fx3_i05_lands_in_its_tenant_and_staff_reads_it(
        install: Any, install_b: Any, caplog: pytest.LogCaptureFixture) -> None:
    """A (I05 + G01)."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE2)
    staff = f2.seed_staff(install, access=("read",))
    sent_a = f3.body(sender="+18045551234")
    sent_b = f3.body()

    async def body() -> None:
        async with f3.runtime(install) as rt, \
                f3.m("fax_ingress").hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            app, _ = await _stack(rt, [install, install_b], fence=fence)
            with caplog.at_level(logging.DEBUG):
                status_a, out_a = await f3.request(app, _jbytes(sent_a), line=fx.LINE1)
                status_b, out_b = await f3.request(app, _jbytes(sent_b), line=fx.LINE2)
            assert (status_a, out_a["outcome"], status_b, out_b["outcome"]) == (
                200, "needs_sorting", 200, "needs_sorting")
            assert [r["fax_id"] for r in fx.rows(install, "m05_fx_faxes")] == [out_a["fax_id"]]
            assert [r["fax_id"] for r in fx.rows(install_b, "m05_fx_faxes")] == [out_b["fax_id"]]
            svc = f3.service(rt)
            listed = await svc.list(await f3.ctx(rt, install, staff, f3.READ_CAPS), None)
            assert [f["fax_id"] for f in listed["faxes"]] == [out_a["fax_id"]]
            text = "\n".join(r.getMessage() for r in caplog.records)
            for secret in ("+18045551234", fx.LINE1, fx.LINE2, sent_a["doc_ref"],
                           sent_a["provider_fax_id"], sent_b["provider_fax_id"]):
                assert secret not in text, secret

    asyncio.run(body())


def test_fx3_i06_duplicate_line_refuses_build(install: Any, install_b: Any) -> None:
    """A (a test-only injected duplicate; the provisioner can never write one)."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE1)

    async def body() -> None:
        ing = f3.m("fax_ingress")
        async with f3.runtime(install) as rt, \
                ing.hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            with pytest.raises(ing.LineDirectoryInvalid) as refused:
                await _stack(rt, [install, install_b], fence=fence)
            assert refused.value.code == "LINE_DUPLICATE"

    asyncio.run(body())


def test_fx3_i07_retry_is_duplicate(install: Any, install_b: Any) -> None:
    """A (Codex f56c079 review): (a) control - an observed 200 then a replay is a duplicate;
    (b) the provider LOSES the first acknowledgement after commit and retries with its original
    identity -> duplicate of the committed fax; one fax, one sorting item, one receipt history
    entry per provider id."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE2)

    def receipt_rows(fid: str) -> tuple[int, int, int]:
        return (len(fx.rows(install, "m05_fx_faxes", "fax_id = %s::uuid", (fid,))),
                len(fx.rows(install, "m05_fx_sorting", "fax_id = %s::uuid", (fid,))),
                len([h for h in fx.rows(install, "m05_fx_history", "fax_id = %s::uuid", (fid,))
                     if h["action"].startswith("received_")]))

    async def body() -> None:
        async with f3.runtime(install) as rt, \
                f3.m("fax_ingress").hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            app, calls = await _stack(rt, [install, install_b], fence=fence)
            # (a) control: observed first reply, manual replay
            payload = _jbytes(f3.body())
            first = await f3.request(app, payload)
            second = await f3.request(app, payload)
            assert (first[0], first[1]["outcome"]) == (200, "needs_sorting")
            assert (second[0], second[1]["outcome"], second[1]["fax_id"]) == (
                200, "duplicate", first[1]["fax_id"])
            assert receipt_rows(first[1]["fax_id"]) == (1, 1, 1)
            # (b) acknowledgement lost after commit, provider retries the same identity
            lost_payload = _jbytes(f3.body(provider_fax_id="FX-LOSTACK0000001"))
            lost = await f3.request(app, lost_payload, lose_ack=True)
            assert lost == (None, None)
            assert calls.receives == 3  # the lost-ack request did reach receive and commit
            committed = fx.rows(install, "m05_fx_faxes", "provider_fax_id = %s",
                                ("FX-LOSTACK0000001",))
            assert len(committed) == 1
            retry = await f3.request(app, lost_payload)
            assert (retry[0], retry[1]["outcome"], retry[1]["fax_id"]) == (
                200, "duplicate", committed[0]["fax_id"])
            assert receipt_rows(committed[0]["fax_id"]) == (1, 1, 1)
            assert len(fx.rows(install, "m05_fx_faxes")) == 2
            assert fx.rows(install_b, "m05_fx_faxes") == []

    asyncio.run(body())


def _terminate_fence(install: Any) -> None:
    from haloflow.m05.fax_rules import K_FX_LINES

    pids = sa.admin(install, "SELECT pid FROM pg_locks WHERE locktype = 'advisory'"
                             " AND mode = 'ShareLock' AND granted"
                             " AND (classid::bigint << 32 | objid::bigint) = %s",
                    (K_FX_LINES,))
    assert pids, "no fence session holds the shared lock"
    for (pid,) in pids:
        sa.admin(install, "SELECT pg_terminate_backend(%s)", (pid,))


def test_fx3_i08_in_flight_reassignment(install: Any, install_b: Any) -> None:
    """A (Codex P07 / test cases v1r1 I08)."""
    from haloflow.synthetic import fax_lines

    install.require()
    install_b.require()
    fax_lines.provision_lines(install.ids.admin,
                              [fax_lines.LineOp("add", install.tenant_id, fx.LINE1)])

    async def body() -> None:
        ing = f3.m("fax_ingress")
        paused, release = asyncio.Event(), asyncio.Event()

        async def after_lookup() -> None:
            paused.set()
            await release.wait()

        async with f3.runtime(install) as rt:
            async with ing.hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
                app, _ = await _stack(rt, [install, install_b], fence=fence,
                                      after_lookup=after_lookup)
                r1 = asyncio.ensure_future(f3.request(app, _jbytes(f3.body())))
                await asyncio.wait_for(paused.wait(), 20)
                await asyncio.to_thread(_terminate_fence, install)
                await asyncio.to_thread(
                    fax_lines.provision_lines, install.ids.admin,
                    [fax_lines.LineOp("remove", install.tenant_id, fx.LINE1),
                     fax_lines.LineOp("add", install_b.tenant_id, fx.LINE1)])
                release.set()
                status, out = await asyncio.wait_for(r1, 20)
                assert (status, out) == (409, {"code": "LINE_NOT_REGISTERED"})
                assert fx.rows(install, "m05_fx_faxes") == fx.rows(install_b,
                                                                   "m05_fx_faxes") == []
                status, out = await f3.request(app, _jbytes(f3.body()))
                assert (status, out) == (503, {"code": "FENCE_LOST"})
                assert not fence.alive
            async with ing.hold_line_fence(install.ids.logins[sa.RUNTIME]) as fresh:
                app2, _ = await _stack(rt, [install, install_b], fence=fresh)
                status, out = await f3.request(app2, _jbytes(f3.body()))
                assert (status, out["outcome"]) == (200, "needs_sorting")
                assert fx.rows(install, "m05_fx_faxes") == []
                assert [r["fax_id"] for r in fx.rows(install_b, "m05_fx_faxes")] == [
                    out["fax_id"]]

    asyncio.run(body())


def test_fx3_s01_statement_authorization(install: Any) -> None:
    """A (Codex e9c224a9)."""
    from haloflow.m01.errors import CapabilityDenied

    f3.ready(install, fx.LINE1, fx.LINE2)
    reader = f2.seed_staff(install, access=("read",))
    both = f2.seed_staff(install, access=("read", "act"))
    fid = f2.routed_fax(install)
    general = uuid.UUID(fx.queue_id(install, "General"))
    fax = f3.m("fax")

    async def body() -> None:
        async with f3.runtime(install) as rt:
            svc = f3.service(rt)
            read_ctx = await f3.ctx(rt, install, reader, f3.READ_CAPS)
            opened = await svc.open(read_ctx, uuid.UUID(fid))
            assert opened["outcome"] == "applied"
            before = fx.snapshot(install)
            with pytest.raises(CapabilityDenied) as denied:
                await svc.act(read_ctx, fax.FaxAction("move", uuid.UUID(fid), 1, general))
            assert denied.value.reason_code == "STATEMENT_CAPABILITY_DENIED"
            assert fx.snapshot(install) == before
            act_ctx = await f3.ctx(rt, install, both, f3.ACT_CAPS)
            moved = await svc.act(act_ctx, fax.FaxAction("move", uuid.UUID(fid), 1, general))
            assert (moved["outcome"], moved["queue_id"]) == ("applied", str(general))

    asyncio.run(body())


def test_fx3_s02_open_key_cannot_mutate(install: Any) -> None:
    """A: the trusted side fixes the operation to open (Codex e9c224a9)."""
    from psycopg.types.json import Jsonb

    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.intake import _attribution

    f3.ready(install, fx.LINE1, fx.LINE2)
    reader = f2.seed_staff(install, access=("read",))
    fid = f2.routed_fax(install, "Imaging")
    stmts = f3.m("fax_statements")

    async def body() -> None:
        async with f3.runtime(install) as rt:
            context = await f3.ctx(rt, install, reader, f3.READ_CAPS)
            foundation = EventFoundation(rt.gateway, producer_service="m05.fax")
            command = {"schema_version": 1, "action": "move", "fax_id": fid,
                       "staff_id": reader, "expected_version": 1,
                       "queue_id": fx.queue_id(install, "General"),
                       "attribution": _attribution(context)}

            async def run(binding: Any) -> Any:
                return await binding.handle.fetch_all(stmts.OPEN_KEY,
                                                      {"command": Jsonb(command)})

            with pytest.raises(psycopg.Error) as refused:  # move-shaped keys are malformed
                await foundation.run_in_transaction(context, run)
            assert refused.value.diag.message_primary == "COMMAND_MALFORMED"
            del command["expected_version"], command["queue_id"]
            rows = await foundation.run_in_transaction(context, run)
            assert rows[0][0]["outcome"] == "applied"
            row = fx.fax(install, fid)
            assert (row["queue_id"], row["version"]) == (fx.queue_id(install, "Imaging"), 1)
            assert [h["action"] for h in fx.rows(install, "m05_fx_history",
                                                 "fax_id = %s::uuid", (fid,))
                    if h["action"] != "received_routed"] == ["opened"]

    asyncio.run(body())


def test_fx3_s03_lines_and_receive_restricted(install: Any) -> None:
    """A."""
    from haloflow.m01.errors import CapabilityDenied

    f3.ready(install, fx.LINE1)
    staff = f2.seed_staff(install)
    fax = f3.m("fax")

    async def body() -> None:
        async with f3.runtime(install) as rt:
            svc = f3.service(rt)
            staff_ctx = await f3.ctx(rt, install, staff, f3.ACT_CAPS)
            for call in (lambda: svc.lines(staff_ctx),
                         lambda: svc.receive(staff_ctx, fax.InboundFax(**{
                             **f3.body(), "line": fx.LINE1}))):
                with pytest.raises((fax.FaxRefused, CapabilityDenied)):
                    await call()
            wrong = await f3.ctx(rt, install, fx.W_FAX, fax.INGRESS_CAPABILITIES,
                                 kind="workload")
            with pytest.raises(fax.FaxRefused) as refused:
                await svc.lines(wrong)
            assert refused.value.code == "CAPABILITIES_INVALID"
            assert await svc.lines(await f3.lines_ctx(rt, install)) == (fx.LINE1,)

    asyncio.run(body())


def _staff_app(rt: Any, install: Any, staff_id: str) -> Any:
    """The ACTUAL synthetic fax staff routes (`haloflow.synthetic.fax_staff_routes`) for ONE
    clinic: the same `install_fax_staff_routes(app, lifecycle)` the synthetic app calls, over a
    lifecycle whose `services` is that clinic's fixed session principal (tenant + staff member)
    with the real resolver, the real M01 staff service (production two-step resolution) and the
    real FaxService. As in the install, no request input selects tenant, principal or capability."""
    from types import SimpleNamespace

    from fastapi import FastAPI

    from haloflow.m01.staff.service import StaffService
    from haloflow.synthetic.wiring import SyntheticServices

    routes = f3.m("fax_staff_routes", package="haloflow.synthetic")
    services = SyntheticServices(resolver=rt.resolver, staff=StaffService(rt.gateway), m05=None,
                                 gateway=rt.gateway, tenant_id=install.tenant_id,
                                 staff_id=staff_id, fax=f3.service(rt))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    routes.install_fax_staff_routes(app, SimpleNamespace(services=services))
    return app


def test_fx3_h04_foreign_fax(install: Any, install_b: Any) -> None:
    """A (Codex f56c079 review): two clinics with DISTINCT lines; B's fax arrives through the real
    ingress on B's line; B's staff reads it through the actual staff routes (positive control);
    A's staff gets 404 NOT_FOUND on get / history / open / actions for B's id; neither clinic
    changes. The single-install unknown-id row (tests/synthetic FX3-H04) stays a separate
    control."""
    f3.ready(install, fx.LINE1)
    f3.ready(install_b, fx.LINE2)
    staff_a = f2.seed_staff(install)
    staff_b = f2.seed_staff(install_b)
    sender = "+18045550142"
    fx.seed_rule(install_b, sender=sender, line=None, queue="Imaging")
    prefix = f3.m("fax_staff_routes", package="haloflow.synthetic").PREFIX

    async def body() -> None:
        async with f3.runtime(install) as rt, \
                f3.m("fax_ingress").hold_line_fence(install.ids.logins[sa.RUNTIME]) as fence:
            ingress, _ = await _stack(rt, [install, install_b], fence=fence)
            landed = await f3.request(ingress, _jbytes(f3.body(sender=sender)), line=fx.LINE2)
            assert (landed[0], landed[1]["outcome"]) == (200, "routed")
            fid_b = landed[1]["fax_id"]
            assert fx.rows(install, "m05_fx_faxes") == []
            app_a, app_b = _staff_app(rt, install, staff_a), _staff_app(rt, install_b, staff_b)
            own = await f3.staff_request(app_b, "GET", f"{prefix}/faxes/{fid_b}")
            assert (own[0], own[1]["fax"]["fax_id"]) == (200, fid_b)  # B positive control
            before = _zse(install, install_b)
            for method, target, payload in (
                    ("GET", f"{prefix}/faxes/{fid_b}", None),
                    ("GET", f"{prefix}/faxes/{fid_b}/history", None),
                    ("POST", f"{prefix}/faxes/{fid_b}/open", {}),
                    ("POST", f"{prefix}/faxes/{fid_b}/actions",
                     {"action": "file", "expected_version": 1})):
                refused = await f3.staff_request(app_a, method, target, payload)
                assert (refused[0], refused[1].get("code")) == (404, "NOT_FOUND"), (
                    method, target, refused)
            assert _zse(install, install_b) == before
            again = await f3.staff_request(app_b, "GET", f"{prefix}/faxes/{fid_b}/history")
            assert again[0] == 200  # B still reads its own, unchanged

    asyncio.run(body())
