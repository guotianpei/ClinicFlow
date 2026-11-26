"""Incoming fax routing FX-IN, package FX5 — SINGLE-TENANT synthetic install rows (RED BY DESIGN).
Governing: `tests/m05/fx5_support.py` (package v1, Codex ALIGN 273a6baf, OD-FX11).

The fax profile's install target becomes 153 (FX5). One disposable PostgreSQL 17 cluster per row,
the real installer, lifecycle and app (as FX3a).

- FX5-G01 the fax install reaches 153 and records install_profile "fax"
- FX5-H01 a read-only principal: 403 FORBIDDEN on all five fax read / open routes, for an
          EXISTING and an ABSENT fax id alike (no existence disclosure); the FaxService is never
          called (the route refuses first); no history row
- FX5-H02 an act principal: 200 on list / get / history / queues; open 200 records `opened` with
          the staff id
- FX5-H03 CONTROL (R3, Codex 0a62cb3f): a read-only principal keeps the follow-up list AND a
          seeded unrelated follow-up item (GET 200) — passes before and after FX5
"""

from __future__ import annotations

import asyncio
import logging
import uuid
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
# the exact refusal body (every synthetic response carries the simulated label); identical for an
# existing and an absent fax id
DENIED = {"code": "FORBIDDEN", "simulated": True}


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


def _history(inst: b3.Install) -> list[Any]:
    return sorted(str(r[0]) for r in inst.admin(
        f"SELECT to_jsonb(t) FROM {inst.schema}.m05_fx_history AS t"))


async def _deliver(app: Any, tok: str) -> bb.Resp:
    return await bb.asgi(app, "POST", SIM_ROUTE, headers=bb.post_headers(tok),
                         body=bb.jbody({"sender": None, "pages": 1}))


async def _get(app: Any, target: str) -> bb.Resp:
    return await bb.asgi(app, "GET", PREFIX + target, headers=bb.get_headers())


async def _post(app: Any, tok: str, target: str) -> bb.Resp:
    return await bb.asgi(app, "POST", PREFIX + target, headers=bb.post_headers(tok),
                         body=bb.jbody({}))


def test_fx5_g01_fax_install_target(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    from haloflow.synthetic import profiles

    assert profiles.TARGETS["fax"] == 153
    inst = e4.install(pg, tmp_path, FAX)
    applied = {r[0] for r in inst.admin("SELECT migration_id FROM shared.schema_migrations"
                                        " WHERE state = 'applied'")}
    assert {"t150_m05_fx_staff_read_v3", "t153_m05_fx_revoke_superseded"} <= applied


def test_fx5_h01_read_only_principal(pg: s.PgCluster, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A."""
    from haloflow.m05 import fax as fax_mod

    inst = e4.install(pg, tmp_path, FAX)
    called: list[str] = []
    for name in ("list", "get", "history", "open", "queues"):
        original = getattr(fax_mod.FaxService, name)

        def spy(self: Any, *args: Any, _n: str = name, _o: Any = original, **kw: Any) -> Any:
            called.append(_n)
            return _o(self, *args, **kw)

        monkeypatch.setattr(fax_mod.FaxService, name, spy)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx5-h01") as (_lc, app):
            tok = await e4.token(app)
            fid = (await _deliver(app, tok)).json["fax_id"]
            bb.standing_override(inst, access=("read",))
            called.clear()
            before = _history(inst)
            for target_id in (fid, str(uuid.uuid4())):
                for target in ("/faxes?queue=needs_sorting", f"/faxes/{target_id}",
                               f"/faxes/{target_id}/history", "/queues"):
                    got = await _get(app, target)
                    assert (got.status, got.json) == (403, DENIED), target
                opened = await _post(app, tok, f"/faxes/{target_id}/open")
                assert (opened.status, opened.json) == (403, DENIED)
            assert called == []
            assert _history(inst) == before

    asyncio.run(body())


def test_fx5_h03_follow_up_kept(pg: s.PgCluster, tmp_path: Path) -> None:
    """C (R3: unrelated roles unchanged; a control before and after FX5)."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx5-h03") as (_lc, app):
            tok = await e4.token(app)
            # an unrelated follow-up item (eligibility E2 -> "Insurance needs manual check")
            assert (await e4.load(app, tok, "E2")).status == 200
            e4.make_due(inst, e4.scenario_ref(inst, "E2", e4.EL_ROW_KEY), "booking", -1.0)
            await e4.tick(app, tok)
            items = [i for i, k, st in e4.el_items(inst) if st == "open"]
            assert len(items) == 1, items
            iid = items[0]
            bb.standing_override(inst, access=("read",))
            listed = await bb.asgi(app, "GET", bb.path("list") + "?limit=100",
                                   headers=bb.get_headers())
            assert listed.status == 200
            assert iid in [i["item_id"] for i in listed.json["items"]], listed.json
            got = await bb.asgi(app, "GET", bb.path("get", iid), headers=bb.get_headers())
            assert got.status == 200, got.raw[:200]

    asyncio.run(body())


def test_fx5_h02_act_principal(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (positive)."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx5-h02") as (_lc, app):
            tok = await e4.token(app)
            fid = (await _deliver(app, tok)).json["fax_id"]
            for target in ("/faxes?queue=needs_sorting", f"/faxes/{fid}",
                           f"/faxes/{fid}/history", "/queues"):
                assert (await _get(app, target)).status == 200, target
            assert (await _post(app, tok, f"/faxes/{fid}/open")).status == 200
            history = (await _get(app, f"/faxes/{fid}/history")).json["history"]
            assert (history[-1]["action"], history[-1]["actor"]) == ("opened",
                                                                      inst.state["staff_id"])

    asyncio.run(body())
