"""EVIDENCE ONLY (not part of the frozen suite; removed after the run).

Codex follow-through on P-9b: bind the persisted record to the actual winner's
context, and exercise compare/write/pin serialisation under many interleavings.
Each iteration: fresh demo DB; two first uses for two dates run concurrently;
the mocked department reads return Denver/Chicago in a random order, and every
mocked request yields to the event loop a random number of times first.
"""

from __future__ import annotations

import asyncio
import random

import httpx
import pytest
from haloflow_demo.sending import StubGateway

from demo_reminders import test_athena_tz_provenance as tp  # type: ignore[import-not-found]

ITER = 40


@pytest.mark.parametrize("seed", range(ITER))
async def test_race_probe(demo_env: dict[str, str], demo_db_url: str, seed: int) -> None:
    rnd = random.Random(seed)
    zones = [tp.DENVER, tp.CHICAGO]
    rnd.shuffle(zones)
    api = tp.Api(*zones)

    async def handler(req: httpx.Request) -> httpx.Response:
        for _ in range(rnd.randint(0, 6)):
            await asyncio.sleep(0)
        return api(req)

    env = tp._env(demo_env)                                            # noqa: SLF001
    from haloflow_demo.athena_source import check_athena_mode
    from haloflow_demo.ownership import acquire_ownership
    from haloflow_demo.service import DbStaffService
    from haloflow_demo.settings import DemoSettings, validate_demo_database_url
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    svc = DbStaffService(own, s, now=lambda: tp.NOW, gateway=StubGateway(tp.TEST_NUMBER),
                         athena=check_athena_mode(s, env),
                         athena_transport=httpx.MockTransport(handler))
    order = [tp.D1, tp.D2] if rnd.random() < 0.5 else [tp.D2, tp.D1]
    try:
        res = await asyncio.gather(*(svc._source(d) for d in order))  # noqa: SLF001
        blocks = {d: svc._source_block(d) for d in order}             # noqa: SLF001
        pinned = svc._pinned                                           # noqa: SLF001
    finally:
        await svc.aclose()
        await own.close()
    winners = [d for d, r in zip(order, res, strict=True) if r is not None]
    assert len(winners) == 1
    w = winners[0]
    loser = next(d for d in order if d != w)
    assert blocks[w]["error"] is None and blocks[loser]["error"] == "TZ_CONTEXT_CHANGED"
    stored = tp.marker(demo_db_url)
    assert stored == tp.ctx(blocks[w]["eff_tz"], "DEPARTMENT")      # bound to the winner
    assert pinned is not None and pinned.value() == stored
    c = blocks[loser]["conflict"]
    assert c["old_zone"] == blocks[w]["eff_tz"] and c["zone"] != c["old_zone"]
    booked = [r.url.params["startdate"] for r in api.requests if r.url.path == tp.BOOKED]
    assert booked == [w.strftime("%m/%d/%Y")]
    print(f"EVIDENCE seed={seed} first={order[0]} winner={w} zone={blocks[w]['eff_tz']}"
          f" loser_saw={c['zone']} stored_ok=1")
