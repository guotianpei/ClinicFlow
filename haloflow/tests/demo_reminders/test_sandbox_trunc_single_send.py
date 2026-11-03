"""Characterization: a NEW single send under a TRUNCATED sandbox snapshot (assessment
claude_m2s-trunc-single-send-assessment-2026-10-29-v1.md; Codex O1 ALIGN for the demonstrator).

Current, accepted behaviour (A-3c + M-2S), pinned so a later change is deliberate:
batch send is refused; a single send is allowed; truncated data is never seeded, so the
reply's baseline read and the write-back both fail closed (staff items, zero store I/O, no
durable row). This is a demonstrator limit, not an authorization of manual real-EHR work.
Offline: in-process mock transport and stub gateway only.
"""

from __future__ import annotations

from typing import Any

import pytest

from .test_sandbox_restart_recovery import (
    FILLER,
    MDY,
    Api,
    Spy,
    _env,
    kinds,
    proc,
    rec,
    rows,
    sql,
    stop,
    tables,
)


async def test_trunc_new_single_send_fails_closed(
    demo_env: dict[str, str], demo_db_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(demo_env)
    await tables(env)
    api = Api(by_date={MDY: [rec("1001"), *FILLER]}, total={MDY: 501})
    own, svc = await proc(env, api)
    try:
        data = await svc.page_data()
        batch = await svc.send_batch()
        sent = await svc.send_to_appointment("ath-e2-1001")
        assert sent["state"] == "SUBMITTED"
        await svc.simulate_reply(int(sent["reminder_id"]), "1")
        for _ in range(6):                         # baseline retries until exhausted
            await svc.process_now()
        task_rows: list[Any] = sql(demo_db_url, "SELECT id, state, observed_before, attempts"
                                                " FROM writeback_tasks")
        spy = Spy(monkeypatch)
        out = await svc.run_writeback(int(task_rows[0][0]))
    finally:
        await stop(own, svc)
    assert data["source"]["truncated"] is True and len(data["appointments"]) == 500
    assert batch == []                                                    # A-3c
    assert sql(demo_db_url, "SELECT state, baseline_ok FROM inbound_messages") == [
        ("PROCESSED", False)]
    assert sql(demo_db_url, "SELECT state FROM reminder_records") == [("CONFIRMED",)]
    assert [r[1:] for r in task_rows] == [("PENDING", None, 0)]
    assert out == "scenario_unavailable"
    assert (spy.reads, spy.writes) == (0, 0)
    assert kinds(demo_db_url) == ["writeback_no_baseline", "writeback_scenario_unavailable"]
    assert rows(demo_db_url) == []                                       # never seeded
