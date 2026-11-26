"""Incoming fax routing FX-IN, package FX4 — SINGLE-TENANT synthetic install rows (RED BY DESIGN).

Governing: `tests/m05/fx4_support.py` (FX4 interface pins). Install evidence, kept distinct from the
two-clinic harness rows (Codex e9c224a9). One disposable PostgreSQL 17 cluster per row.

- FX4-G04 a fax install reaches target 149 (FX4 units) with the re-pinned installed-state profile;
          default and eligibility targets unchanged (97 / 135)
- FX4-A01 simulator with `sender_name` "Valley Radiology 540-555-0199" -> the staff list shows the
          fax with sender_display {"text": "Valley Radiology •••0199", "source": "as_sent"}
- FX4-A02 GET /synthetic/v1/fax/queues (the install's staff member) lists the seven OD-FX1 default
          queues in order, each with exactly queue_id, name, active, position
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import b3b_support as bb
import el4_support as e4
import pytest

pytestmark = pytest.mark.postgres

FAX = "fax"
SIM_ROUTE = "/synthetic/v1/simulator/fax"
PREFIX = "/synthetic/v1/fax"
DEFAULT_QUEUES = ("Labs/Results", "Imaging", "Referrals & consults", "Records requests",
                  "Insurance/Prior auth", "Pharmacy/Refills", "General")


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


def test_fx4_g04_fax_install_target(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    from haloflow.synthetic import profiles

    assert (profiles.TARGETS["default"], profiles.TARGETS["eligibility"],
            profiles.TARGETS["fax"]) == (97, 135, 149)
    inst = e4.install(pg, tmp_path, FAX)
    assert e4.installed_target(inst) == 149


def test_fx4_a01_simulator_sender_name(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx4-a01") as (_lc, app):
            tok = await e4.token(app)
            sent = await bb.asgi(app, "POST", SIM_ROUTE, headers=bb.post_headers(tok),
                                 body=bb.jbody({"sender": None, "pages": 1,
                                                "sender_name": "Valley Radiology 540-555-0199"}))
            assert (sent.status, sent.json["outcome"]) == (200, "needs_sorting")
            listed = await bb.asgi(app, "GET", PREFIX + "/faxes?queue=needs_sorting",
                                   headers=bb.get_headers())
            assert listed.status == 200
            display: Any = [f["sender_display"] for f in listed.json["faxes"]
                            if f["fax_id"] == sent.json["fax_id"]]
            assert display == [{"text": "Valley Radiology •••0199", "source": "as_sent"}]

    asyncio.run(body())


def test_fx4_a02_queues_route(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx4-a02") as (_lc, app):
            got = await bb.asgi(app, "GET", PREFIX + "/queues", headers=bb.get_headers())
            assert got.status == 200
            assert [q["name"] for q in got.json["queues"]] == list(DEFAULT_QUEUES)
            assert all(set(q) == {"queue_id", "name", "active", "position"}
                       for q in got.json["queues"])

    asyncio.run(body())
