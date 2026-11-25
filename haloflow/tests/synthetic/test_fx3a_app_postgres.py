"""Incoming fax routing FX-IN, package FX3a — SINGLE-TENANT synthetic install rows (RED BY DESIGN).

INSTALL evidence (Codex e9c224a9: kept distinct from the two-tenant harness rows in
tests/m05/test_fx3a_ingress_postgres.py). One disposable PostgreSQL 17 cluster per row, the real
installer (`provision.init(..., profile="fax")`), the real lifecycle and app. The simulator delivers
through the SAME unmounted fax router (`haloflow.m05.fax_ingress`) with the lifecycle's real fence,
directory, context resolution and FaxService — as `patient_reply` uses the S-G1a app.

FX3a install interface pinned here (red packet v1, section 3):
- `haloflow.synthetic.profiles`: FAX = "fax"; PROFILES gains it; TARGETS["fax"] == 144; registry /
  catalog("fax") = the FX2 registry / FX3 catalogue; schedulers("fax") == schedulers("eligibility").
- `provision.init(env, attest_flag=True, profile="fax")`: as eligibility, plus the fax workload id
  in m05_fx_workloads and ONE synthetic receiving line `SYN_FAX_LINE = "+15405550100"`
  (`haloflow.synthetic.fax_wiring`) written by `fax_lines.provision_lines` before any startup.
- lifecycle (profile fax): holds the line fence for its lifetime (taken before the directory is
  built); `lc.fax_service`; the per-lifespan simulator secret; a lost fence makes deliveries 503.
- simulator: `POST /synthetic/v1/simulator/fax` JSON {"sender": E.164|null, "pages": 1..999}
  (CSRF as every POST) -> the router with header line SYN_FAX_LINE -> {"outcome", "fax_id"}.
- staff JSON (prefix `/synthetic/v1/fax`), the B2 staff member, production route keys:
  GET  /faxes?queue=needs_sorting|<queue uuid>   (list)      GET /faxes/{id}  (get)
  GET  /faxes/{id}/history                        (history)   POST /faxes/{id}/open (get)
  POST /faxes/{id}/actions {"action", "expected_version", "queue_id"?}  (act)
  refusals: 403 FORBIDDEN (route capability), 404 NOT_FOUND (FAX_NOT_FOUND / QUEUE_NOT_FOUND),
  409 CONFLICT {"code"} (VERSION_CONFLICT, FAX_FILED, NOT_ROUTED, ALREADY_SORTED,
  NOT_NEEDS_SORTING), 422 MALFORMED, missing CSRF as the existing POST routes.
No route reaches the support settings gateway or the lines snapshot.

Rows:
- FX3-G02 CONTROL: the default and eligibility installs keep targets 97 / 135
- FX3-G03 a fax install reaches 144, records install_profile "fax", holds SYN_FAX_LINE; starting it
          as "eligibility" is refused before any pool
- FX3-A01 POSITIVE end to end: simulator delivery (sender hidden) -> 200 needs_sorting -> the staff
          needs-sorting list shows it with reason sender_not_shown -> get shows no sender digits
- FX3-H01 read-only staff: list / get / history 200; open 200 with history actor = the staff id;
          actions -> 403, ZSE
- FX3-H02 act staff: sort -> 200 routed; a POST without the CSRF token is refused, ZSE
- FX3-H03 the route table has no settings or lines route
- FX3-H04 (single tenant) an unknown fax id -> 404 NOT_FOUND on get / history / open / actions, ZSE
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import el4_support as e4
import pytest

pytestmark = pytest.mark.postgres

FAX = "fax"
SIM_ROUTE = "/synthetic/v1/simulator/fax"
PREFIX = "/synthetic/v1/fax"


@pytest.fixture(autouse=True)
def _haloflow_loggers_enabled() -> Iterator[None]:
    manager = logging.Logger.manager
    names = [n for n, lg in list(manager.loggerDict.items())
             if (n == "haloflow" or n.startswith("haloflow."))
             and isinstance(lg, logging.Logger) and lg.disabled]
    for n in names:
        logging.getLogger(n).disabled = False
    try:
        yield
    finally:
        for n in names:
            logging.getLogger(n).disabled = True


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    import shutil

    with s.cluster(tmp_path) as c:
        yield c
    s.reap()
    shutil.rmtree(tmp_path / "pg", ignore_errors=True)


def _fx_rows(inst: b3.Install, table: str) -> list[Any]:
    return [r[0] for r in inst.admin(f"SELECT to_jsonb(t) FROM {inst.schema}.{table} AS t")]


def _zse(inst: b3.Install) -> dict[str, list[Any]]:
    return {t: sorted(str(r) for r in _fx_rows(inst, t))
            for t in ("m05_fx_faxes", "m05_fx_sorting", "m05_fx_history")}


async def _deliver(app: Any, tok: str, sender: str | None = None) -> bb.Resp:
    return await bb.asgi(app, "POST", SIM_ROUTE, headers=bb.post_headers(tok),
                         body=bb.jbody({"sender": sender, "pages": 1}))


async def _get(app: Any, target: str) -> bb.Resp:
    return await bb.asgi(app, "GET", PREFIX + target, headers=bb.get_headers())


async def _post(app: Any, tok: str | None, target: str, value: Any) -> bb.Resp:
    headers = bb.post_headers(tok) if tok is not None else bb.get_headers() + [
        ("content-type", "application/json")]
    return await bb.asgi(app, "POST", PREFIX + target, headers=headers, body=bb.jbody(value))


def test_fx3_g02_other_profiles_unchanged() -> None:
    """C."""
    from haloflow.synthetic import profiles

    assert (profiles.TARGETS["default"], profiles.TARGETS["eligibility"]) == (97, 135)


def test_fx3_g03_fax_install(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    from haloflow.synthetic import fax_wiring

    inst = e4.install(pg, tmp_path, FAX)
    assert e4.installed_target(inst) == 144
    assert inst.state["install_profile"] == FAX
    assert [r["line_e164"] for r in _fx_rows(inst, "m05_fx_lines")] == [fax_wiring.SYN_FAX_LINE]

    async def body() -> None:
        with pytest.raises(Exception) as refused:  # noqa: PT011 - the lifecycle's own refusal
            async with e4.running(inst, profile="eligibility", app_name="fx3-g03"):
                pass
        assert "PROFILE" in repr(refused.value)

    asyncio.run(body())


def test_fx3_a01_simulator_to_staff_read(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (Codex e9c224a9: a successful simulator -> staff read through normal entry points)."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3-a01") as (_lc, app):
            tok = await e4.token(app)
            delivered = await _deliver(app, tok)
            assert (delivered.status, delivered.json["outcome"]) == (200, "needs_sorting")
            fid = delivered.json["fax_id"]
            listed = await _get(app, "/faxes?queue=needs_sorting")
            assert listed.status == 200
            assert [(f["fax_id"], f["reason"]) for f in listed.json["faxes"]] == [
                (fid, "sender_not_shown")]
            got = await _get(app, f"/faxes/{fid}")
            assert (got.status, got.json["fax"]["sender_last4"]) == (200, None)

    asyncio.run(body())


def test_fx3_h01_read_only_staff(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3-h01") as (_lc, app):
            tok = await e4.token(app)
            fid = (await _deliver(app, tok)).json["fax_id"]
            bb.standing_override(inst, access=("read",))
            for target in ("/faxes?queue=needs_sorting", f"/faxes/{fid}",
                           f"/faxes/{fid}/history"):
                assert (await _get(app, target)).status == 200, target
            opened = await _post(app, tok, f"/faxes/{fid}/open", {})
            assert opened.status == 200
            history = await _get(app, f"/faxes/{fid}/history")
            assert history.json["history"][-1]["action"] == "opened"
            assert history.json["history"][-1]["actor"] == inst.state["staff_id"]
            before = _zse(inst)
            queue = _fx_rows(inst, "m05_fx_queues")[0]["queue_id"]
            denied = await _post(app, tok, f"/faxes/{fid}/actions",
                                 {"action": "sort", "expected_version": 1, "queue_id": queue})
            assert denied.status == 403
            assert _zse(inst) == before

    asyncio.run(body())


def test_fx3_h02_act_staff_and_csrf(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3-h02") as (_lc, app):
            tok = await e4.token(app)
            fid = (await _deliver(app, tok)).json["fax_id"]
            queue = _fx_rows(inst, "m05_fx_queues")[0]["queue_id"]
            before = _zse(inst)
            no_csrf = await _post(app, None, f"/faxes/{fid}/actions",
                                  {"action": "sort", "expected_version": 1, "queue_id": queue})
            assert no_csrf.status in (400, 403)
            assert _zse(inst) == before
            sorted_ = await _post(app, tok, f"/faxes/{fid}/actions",
                                  {"action": "sort", "expected_version": 1, "queue_id": queue})
            assert (sorted_.status, sorted_.json["state"], sorted_.json["queue_id"]) == (
                200, "routed", queue)

    asyncio.run(body())


def test_fx3_h03_no_support_or_lines_route(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3-h03") as (_lc, app):
            paths = [getattr(r, "path", "") for r in app.routes]
            assert any(p.startswith(PREFIX) for p in paths)
            assert not [p for p in paths if "settings" in p or "lines" in p]

    asyncio.run(body())


def test_fx3_h04_unknown_fax(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (single tenant; the cross-tenant case is the harness row FX3-H04)."""
    import uuid

    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3-h04") as (_lc, app):
            tok = await e4.token(app)
            await _deliver(app, tok)
            unknown = str(uuid.uuid4())
            before = _zse(inst)
            for response in (await _get(app, f"/faxes/{unknown}"),
                             await _get(app, f"/faxes/{unknown}/history"),
                             await _post(app, tok, f"/faxes/{unknown}/open", {}),
                             await _post(app, tok, f"/faxes/{unknown}/actions",
                                         {"action": "file", "expected_version": 1})):
                assert response.status == 404
            assert _zse(inst) == before

    asyncio.run(body())
