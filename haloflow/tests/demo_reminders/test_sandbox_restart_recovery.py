"""M-2S — sandbox-mode (athena_preview) restart recovery for SIMULATED write-back.

Package claude_m2s-sandbox-recovery-package-2026-10-29-v1r2.md (Codex ALIGN of the
binding proposal, codex_m2s-binding-proposal-alignment.md). Tests-first: written
before the implementation; the baseline run at main 2a37700 is expected to fail.

SIMULATED evidence only. Nothing here describes athenahealth write semantics (F-E1).
No test touches the network: the athena transport is an in-process MockTransport
whose ``requests`` list records every request, and the SMS gateway is StubGateway.

Return values are the existing ``tasks.py`` strings (``Outcome`` values such as
"DONE", "DONE(reconciled)", "writeback_conflict"; "scenario_unavailable").

API under test (to be implemented):

- ``models.SCHEMA_VERSION`` = 5. ``sim_confirmations.provider_appt_id`` (String(16),
  NULL for synthetic; exact ``[1-9][0-9]{0,11}`` and NOT NULL for athena_preview), with
  CHECK ``ck_sim_confirmations_provider_appt`` and UNIQUE ``uq_sim_confirmations_paid``
  (source, tenant_id, provider_appt_id).
- ``sim_store.SimStore(own, tenant, source="synthetic")``; ``source`` must be
  "synthetic" or "athena_preview" (``ValueError`` otherwise).
  ``SimStore.seed_bound(entries, hook=None) -> dict[str, str]``: entries are
  ``(paid, emr_patient_id, appt_start)``; one fenced transaction; insert-if-absent;
  returns per PAID one of BOUND / SLOT_TAKEN / PAID_ELSEWHERE. ``hook(stage)`` is
  awaited with "inserted" after the inserts and before the commit.
- Snapshot publication: records excluded as DUPLICATE_SLOT, SLOT_TAKEN or
  PAID_ELSEWHERE are absent from ``page_data()["appointments"]`` and counted in
  ``page_data()["source"]["counts"]``. ``DbStaffService.seed_hook`` (default None) is
  passed to ``seed_bound``.
- Scope pin ``demo_meta['athena_m2s_scope']`` = ``v1;practice=195900;dept=<id>``:
  insert-if-absent by its first writer, never rewritten; a differing or malformed
  value refuses (fail closed, no request).
- Binding (run / reconcile / baseline), with zero network requests: local provenance
  gate → exact id formats → PAID → one durable row with slot AND PAID → if this
  process loaded a snapshot for the stored-zone date, the PAID must be there at the
  same patient/start with status scheduled or confirmed. Outcomes: DONE etc. as M-2;
  ``"scenario_unavailable"`` (existing item) when no binding exists or a gate fails;
  ``"source_changed"`` with follow-up kind ``writeback_source_changed`` when a loaded
  snapshot disagrees; failed / TRUNCATED snapshot for that date → refused.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo import models
from haloflow_demo.athena_source import check_athena_mode
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.sending import StubGateway
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url
from haloflow_demo.sim_store import SimStore

NY, DENVER = "America/New_York", "America/Denver"
DEPT = "21"
DEPT_PATH = f"/v1/195900/departments/{DEPT}"
BOOKED = "/v1/195900/appointments/booked"
SCOPE_KEY = "athena_m2s_scope"
TZ_KEY = "athena_tz_interpretation"
TEST_NUMBER = "+15550100141"
TENANT = "demo-clinic"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)          # NY target date 2026-10-28
D1 = date(2026, 10, 28)
SOURCE_CHANGED_TEXT = ("The saved appointment could not be validated against the loaded"
                       " sandbox snapshot. Review it before continuing. No real EHR update"
                       " was made.")
ATHENA_ENV = {"DEMO_EHR_SOURCE": "athena_preview", "DEMO_ATHENA_DEPARTMENT_ID": DEPT,
              "ATHENA_BASE_URL": "https://api.preview.platform.athenahealth.com",
              "ATHENA_PRACTICE_ID": "195900", "ATHENA_CLIENT_ID": "SENTINEL-CID",
              "ATHENA_CLIENT_SECRET": "SENTINEL-SECRET"}


def rec(aid: str, pid: str = "60178", start: str = "09:00", status: str = "f") -> dict[str, str]:
    return {"appointmentid": aid, "patientid": pid, "starttime": start,
            "appointmentstatus": status}


class Api:
    """Department zone fixed; booked records per MM/DD/YYYY date (default: none).
    ``fail`` holds dates whose booked GET returns 500 (whole fetch rejected)."""

    def __init__(self, zone: str | None = NY, by_date: dict[str, list[dict[str, str]]]
                 | None = None, fail: set[str] | None = None) -> None:
        self.zone, self.by_date, self.fail = zone, by_date or {}, fail or set()
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path == "/oauth2/v1/token":
            return httpx.Response(200, json={"access_token": "SENTINEL-TOKEN",
                                             "expires_in": 3600})
        if req.url.path == DEPT_PATH:
            body: dict[str, Any] = {"departmentid": DEPT, "name": "SENTINEL-dept"}
            if self.zone is not None:
                body["timezonename"] = self.zone
            return httpx.Response(200, json=[body])
        if req.url.path == BOOKED:
            d = req.url.params["startdate"]
            if d in self.fail:
                return httpx.Response(500)
            recs = [{**r, "date": d, "departmentid": DEPT,
                     "patient": {"firstname": "SENTINEL-Donna"}}
                    for r in self.by_date.get(d, [])]
            return httpx.Response(200, json={"appointments": recs, "totalcount": len(recs)})
        return httpx.Response(404)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def for_target(self, d: date) -> None:
        self.now = datetime(d.year, d.month, d.day, 13, 0, tzinfo=UTC) - timedelta(days=2)


def _env(demo_env: dict[str, str], **over: str) -> dict[str, str]:
    return {**demo_env, **ATHENA_ENV, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER,
            "DEMO_SMS_SENDER": "+15550100100",
            "DEMO_CALLBACK_SECRET": "test-callback-secret-0123456789", **over}


async def proc(env: dict[str, str], api: Api, clock: Clock | None = None,
               gw: StubGateway | None = None) -> tuple[Any, DbStaffService]:
    clock = clock or Clock()
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    svc = DbStaffService(own, s, now=lambda: clock.now,
                         gateway=gw or StubGateway(TEST_NUMBER),
                         athena=check_athena_mode(s, env),
                         athena_transport=httpx.MockTransport(api))
    return own, svc


async def stop(own: Any, svc: DbStaffService) -> None:
    await svc.aclose()
    await own.close()


async def tables(env: dict[str, str]) -> None:
    s = DemoSettings.from_env({k: v for k, v in env.items() if not k.startswith(
        ("DEMO_EHR_SOURCE", "DEMO_ATHENA", "ATHENA_"))})
    own = await acquire_ownership(s, validate_demo_database_url(s))
    await own.close()


def sql(url: str, q: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(q, params)
        return cur.fetchall() if cur.description else []


def rows(url: str) -> list[tuple[Any, ...]]:
    return sql(url, "SELECT provider_appt_id, emr_patient_id, appt_start, value, version"
                    " FROM sim_confirmations WHERE source = 'athena_preview'"
                    " ORDER BY appt_start, provider_appt_id")


def task(url: str, tid: int) -> tuple[Any, ...]:
    return sql(url, "SELECT state, reason, attempts FROM writeback_tasks WHERE id = %s",
               (tid,))[0]


def kinds(url: str) -> list[str]:
    return sorted(r[0] for r in sql(url, "SELECT kind FROM follow_up_items"))


def meta(url: str, key: str) -> str | None:
    r = sql(url, "SELECT value FROM demo_meta WHERE key = %s", (key,))
    return r[0][0] if r else None


def appt_ids(data: dict[str, Any]) -> list[str]:
    return [a["emr_appt_id"] for a in data["appointments"]]


T0900 = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)        # 09:00 New York
MDY = "10/28/2026"


async def confirm(svc: DbStaffService, url: str, appt: str) -> int:
    """Send to ``appt``, simulate reply '1', process it; returns the task id."""
    sent = await svc.send_to_appointment(appt)
    assert sent["state"] == "SUBMITTED", sent
    res = await svc.simulate_reply(int(sent["reminder_id"]), "1")
    assert res.status == 204
    await svc.process_now()
    r = sql(url, "SELECT t.id FROM writeback_tasks t JOIN reminder_records m"
                 " ON m.id = t.reminder_id WHERE m.emr_appt_id = %s", (appt,))
    assert len(r) == 1
    return int(r[0][0])


async def fresh(demo_env: dict[str, str], demo_db_url: str,
                by_date: dict[str, list[dict[str, str]]] | None = None
                ) -> tuple[dict[str, str], int]:
    """Tables + one sandbox process that confirms appointment 1001 at 09:00 (task
    PENDING, not run). Returns (env, task id); the process is stopped."""
    env = _env(demo_env)
    await tables(env)
    own, svc = await proc(env, Api(by_date=by_date or {MDY: [rec("1001")]}))
    try:
        data = await svc.page_data()
        assert appt_ids(data) == ["ath-e2-1001"]
        tid = await confirm(svc, demo_db_url, "ath-e2-1001")
    finally:
        await stop(own, svc)
    return env, tid


# ── schema (R-MS-8, T-MS-29/30) ──────────────────────────────────────────────


def test_t_ms_30a_schema_version_is_5() -> None:
    assert models.SCHEMA_VERSION == 5


async def test_t_ms_29_constraints_reject_bad_rows(demo_env: dict[str, str],
                                                   demo_db_url: str) -> None:
    await tables(_env(demo_env))
    ins = ("INSERT INTO sim_confirmations(source, tenant_id, emr_patient_id, appt_start,"
           " value, version, provider_appt_id) VALUES (%s, 'demo-clinic', %s, %s, '1', 1, %s)")
    bad = [("synthetic", "syn-p-a", T0900, "1001"),          # synthetic with a PAID
           ("athena_preview", "athp-1", T0900, None),         # athena without (NULL)
           ("athena_preview", "athp-2", T0900, "0123"),       # malformed
           ("athena_preview", "athp-3", T0900, "1001x")]
    for row in bad:
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(demo_db_url, ins, row)
    sql(demo_db_url, ins, ("athena_preview", "athp-4", T0900, "1001"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql(demo_db_url, ins, ("athena_preview", "athp-5", T0900, "1001"))
    sql(demo_db_url, ins, ("synthetic", "syn-p-b", T0900, None))
    sql(demo_db_url, ins, ("synthetic", "syn-p-c", T0900, None))   # NULLs distinct


async def test_t_ms_30b_schema4_database_refused_untouched(demo_env: dict[str, str],
                                                           demo_db_url: str) -> None:
    env = _env(demo_env)
    await tables(env)
    sql(demo_db_url, "UPDATE demo_meta SET value = '4' WHERE key = 'schema_version'")
    before = sql(demo_db_url, "SELECT key, value FROM demo_meta ORDER BY key")
    s = DemoSettings.from_env(env)
    with pytest.raises(StartupRefused):
        own = await acquire_ownership(s, validate_demo_database_url(s))
        await own.close()
    assert sql(demo_db_url, "SELECT key, value FROM demo_meta ORDER BY key") == before


# ── store (T-MS-19, T-MS-1) ──────────────────────────────────────────────────


async def test_t_ms_19_store_source_validation(demo_env: dict[str, str],
                                               demo_db_url: str) -> None:
    env = _env(demo_env)
    await tables(env)
    s = DemoSettings.from_env({k: v for k, v in env.items() if not k.startswith(
        ("DEMO_EHR_SOURCE", "DEMO_ATHENA", "ATHENA_"))})
    own = await acquire_ownership(s, validate_demo_database_url(s))
    try:
        SimStore(own, TENANT)
        SimStore(own, TENANT, "athena_preview")
        with pytest.raises(ValueError):
            SimStore(own, TENANT, "athena")
        with pytest.raises(ValueError):
            SimStore(own, TENANT, "")
    finally:
        await own.close()


async def test_t_ms_1_seed_insert_if_absent_keeps_state(demo_env: dict[str, str],
                                                       demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1001")]}))
    try:
        assert await svc.run_writeback(tid) == "DONE"
        await svc.page_data()                          # reseed after the write
    finally:
        await stop(own, svc)
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "2", 2)]


# ── recovery (T-MS-2/4/5/6/9/24) ─────────────────────────────────────────────


async def test_t_ms_2_9_24_restart_no_snapshot_durable_decides(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    api = Api(by_date={MDY: [rec("1001")]})
    own, svc = await proc(env, api)
    try:
        out = await svc.run_writeback(tid)               # no page load in this process
    finally:
        await stop(own, svc)
    assert out == "DONE"
    assert task(demo_db_url, tid)[0] == "DONE"
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "2", 2)]
    assert api.requests == []                            # OQ-1: no implicit request


async def test_t_ms_4_after_write_crash_reconciles_once(demo_env: dict[str, str],
                                                       demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    own, svc = await proc(env, Api())
    try:
        sql(demo_db_url, "UPDATE writeback_tasks SET state = 'IN_FLIGHT',"
                         " attempts = 1 WHERE id = %s", (tid,))
        sql(demo_db_url, "UPDATE sim_confirmations SET value = '2', version = 2"
                         " WHERE source = 'athena_preview'")          # write committed
    finally:
        await stop(own, svc)                               # crash before completion
    api = Api()
    own, svc = await proc(env, api)
    try:
        assert task(demo_db_url, tid)[:2] == ("UNCERTAIN", "restart")
        assert await svc.reconcile_writeback(tid) == "DONE(reconciled)"
    finally:
        await stop(own, svc)
    assert task(demo_db_url, tid)[:2] == ("DONE", "reconciled")
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "2", 2)]
    assert api.requests == []


async def test_t_ms_5_before_write_crash_stays_uncertain(demo_env: dict[str, str],
                                                        demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    sql(demo_db_url, "UPDATE writeback_tasks SET state = 'IN_FLIGHT', attempts = 1"
                     " WHERE id = %s", (tid,))
    own, svc = await proc(env, Api())
    try:
        await svc.reconcile_writeback(tid)
        with pytest.raises(ValueError):
            await svc.run_writeback(tid)
    finally:
        await stop(own, svc)
    assert task(demo_db_url, tid)[:2] == ("UNCERTAIN", "restart")
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "1", 1)]


async def test_t_ms_6_clinic_edit_conflict(demo_env: dict[str, str],
                                           demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    sql(demo_db_url, "UPDATE sim_confirmations SET value = '1', version = 2"
                     " WHERE source = 'athena_preview'")
    own, svc = await proc(env, Api())
    try:
        assert await svc.run_writeback(tid) == "writeback_conflict"
    finally:
        await stop(own, svc)
    assert task(demo_db_url, tid)[0] == "CONFLICT"
    assert "writeback_conflict" in kinds(demo_db_url)


async def test_t_ms_3_restart_unprocessed_reply_completes(demo_env: dict[str, str],
                                                          demo_db_url: str) -> None:
    env = _env(demo_env)
    await tables(env)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1001")]}))
    try:
        await svc.page_data()
        sent = await svc.send_to_appointment("ath-e2-1001")
        assert (await svc.simulate_reply(int(sent["reminder_id"]), "1")).status == 204
    finally:
        await stop(own, svc)                               # reply not processed
    api = Api()
    own, svc = await proc(env, api)
    try:
        await svc.process_now()
        tid = int(sql(demo_db_url, "SELECT id FROM writeback_tasks")[0][0])
        assert sql(demo_db_url, "SELECT observed_before FROM writeback_tasks")[0][0] == "1"
        assert await svc.run_writeback(tid) == "DONE"
    finally:
        await stop(own, svc)
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "2", 2)]
    assert api.requests == []


# ── snapshot validation (T-MS-7a..e, 8, 23, 25) ──────────────────────────────


@pytest.mark.parametrize("later", [
    pytest.param([], id="7a-paid-absent"),
    pytest.param([rec("1002")], id="7b-replacement-same-slot"),
    pytest.param([rec("1001", start="10:00")], id="7c-moved-time"),
    pytest.param([rec("1001", status="x")], id="7d-cancelled"),
])
async def test_t_ms_7_loaded_snapshot_disagrees(demo_env: dict[str, str], demo_db_url: str,
                                                later: list[dict[str, str]]) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    own, svc = await proc(env, Api(by_date={MDY: later}))
    try:
        await svc.page_data()
        out = await svc.run_writeback(tid)
        again = await svc.reconcile_writeback(tid) if task(demo_db_url, tid)[0] in (
            "UNCERTAIN",) else None
    finally:
        await stop(own, svc)
    assert out == "source_changed" and again is None
    assert task(demo_db_url, tid)[:3] == ("PENDING", None, 0)
    assert "writeback_source_changed" in kinds(demo_db_url)
    assert sql(demo_db_url, "SELECT detail FROM follow_up_items WHERE kind ="
                            " 'writeback_source_changed'")[0][0] == SOURCE_CHANGED_TEXT
    assert ("1001", "athp-60178", T0900, "1", 1) in rows(demo_db_url)


async def test_t_ms_7e_retained_task_validated_by_paid(demo_env: dict[str, str],
                                                      demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1001")]}))
    try:
        data = await svc.page_data()
        assert appt_ids(data) == ["ath-e3-1001"]
        assert await svc.run_writeback(tid) == "DONE"
    finally:
        await stop(own, svc)
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "2", 2)]


async def test_t_ms_8_failed_snapshot_refuses(demo_env: dict[str, str],
                                              demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    own, svc = await proc(env, Api(fail={MDY}))
    try:
        data = await svc.page_data()
        assert data["source"]["error"] is not None
        out = await svc.run_writeback(tid)
    finally:
        await stop(own, svc)
    assert out in ("scenario_unavailable", "source_changed")
    assert task(demo_db_url, tid)[:3] == ("PENDING", None, 0)
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "1", 1)]


async def test_t_ms_21_replacement_after_unsent_seed_not_attached(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env = _env(demo_env)
    await tables(env)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1001")]}))
    try:
        await svc.page_data()                                  # A seeded, never sent
    finally:
        await stop(own, svc)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1002")]}))
    try:
        data = await svc.page_data()
        sent = await svc.send_to_appointment("ath-e3-1002")
    finally:
        await stop(own, svc)
    assert appt_ids(data) == [] and sent["state"] == "SKIPPED"
    assert data["source"]["counts"].get("SLOT_TAKEN") == 1
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "1", 1)]


async def test_t_ms_22_replacement_after_write_not_attached(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    own, svc = await proc(env, Api())
    try:
        assert await svc.run_writeback(tid) == "DONE"
    finally:
        await stop(own, svc)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1002")]}))
    try:
        data = await svc.page_data()
    finally:
        await stop(own, svc)
    assert appt_ids(data) == []
    assert data["source"]["counts"].get("SLOT_TAKEN") == 1
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "2", 2)]


async def test_t_ms_25_moved_paid_elsewhere(demo_env: dict[str, str],
                                            demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1001", start="11:00")]}))
    try:
        data = await svc.page_data()
        out = await svc.run_writeback(tid)
    finally:
        await stop(own, svc)
    assert appt_ids(data) == [] and data["source"]["counts"].get("PAID_ELSEWHERE") == 1
    assert out == "source_changed"
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "1", 1)]


async def test_t_ms_18_26_duplicate_slot_in_snapshot(demo_env: dict[str, str],
                                                     demo_db_url: str) -> None:
    env = _env(demo_env)
    await tables(env)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1001"), rec("1002")]}))
    try:
        data = await svc.page_data()
        batch = await svc.send_batch()
    finally:
        await stop(own, svc)
    assert appt_ids(data) == [] and batch == []
    assert data["source"]["counts"].get("DUPLICATE_SLOT") == 2
    assert rows(demo_db_url) == []


# ── binding gates (T-MS-10/11/14/15/31) ──────────────────────────────────────


@pytest.mark.parametrize("change", [
    pytest.param({"DEMO_ATHENA_DEPARTMENT_ID": "22"}, id="dept"),
    pytest.param({"CLINIC_TZ": "America/Chicago"}, id="clinic"),
])
async def test_t_ms_15_config_change_refused_no_request(
    demo_env: dict[str, str], demo_db_url: str, change: dict[str, str],
) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    api = Api()
    own, svc = await proc({**env, **change}, api)
    try:
        out = await svc.run_writeback(tid)
    finally:
        await stop(own, svc)
    assert out == "scenario_unavailable" and api.requests == []
    assert task(demo_db_url, tid)[:3] == ("PENDING", None, 0)
    assert rows(demo_db_url)[0][3:] == ("1", 1)


@pytest.mark.parametrize("bad", [None, "v1;dept=21;zone=Mars/Base;basis=DEPARTMENT;clinic=X",
                                 "v1;dept=21;zone=America/New_York;basis=DEPARTMENT;"
                                 "clinic=America/New_York\n"])
async def test_t_ms_14_interpretation_record_absent_or_malformed(
    demo_env: dict[str, str], demo_db_url: str, bad: str | None,
) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    if bad is None:
        sql(demo_db_url, "DELETE FROM demo_meta WHERE key = %s", (TZ_KEY,))
    else:
        sql(demo_db_url, "UPDATE demo_meta SET value = %s WHERE key = %s", (bad, TZ_KEY))
    api = Api()
    own, svc = await proc(env, api)
    try:
        out = await svc.run_writeback(tid)
    finally:
        await stop(own, svc)
    assert out == "scenario_unavailable" and api.requests == []
    assert rows(demo_db_url)[0][3:] == ("1", 1)


async def test_t_ms_31_scope_pin_written_once_and_enforced(demo_env: dict[str, str],
                                                          demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    assert meta(demo_db_url, SCOPE_KEY) == "v1;practice=195900;dept=21"
    for bad in ("v1;practice=195901;dept=21", "v1;practice=195900;dept=21\n", "v0"):
        sql(demo_db_url, "UPDATE demo_meta SET value = %s WHERE key = %s", (bad, SCOPE_KEY))
        api = Api(by_date={MDY: [rec("1001")]})
        own, svc = await proc(env, api)
        try:
            out = await svc.run_writeback(tid)
            data = await svc.page_data()
        finally:
            await stop(own, svc)
        assert out == "scenario_unavailable"
        assert appt_ids(data) == []
        assert meta(demo_db_url, SCOPE_KEY) == bad          # never rewritten
    assert rows(demo_db_url)[0][3:] == ("1", 1)


async def test_t_ms_11_malformed_bindings_refused(demo_env: dict[str, str],
                                                  demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    for appt, pid in (("ath-e2-01001", "athp-60178"), ("ath-e2-1001", "syn-p-a"),
                      ("ath-e2-1003", "athp-60178")):        # last: no durable row
        sql(demo_db_url, "UPDATE reminder_records SET emr_appt_id = %s, emr_patient_id = %s",
            (appt, pid))
        own, svc = await proc(env, Api())
        try:
            assert await svc.run_writeback(tid) == "scenario_unavailable"
        finally:
            await stop(own, svc)
    assert rows(demo_db_url)[0][3:] == ("1", 1)


async def test_t_ms_10_sources_never_cross(demo_env: dict[str, str],
                                           demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    syn_env = {k: v for k, v in env.items() if not k.startswith(
        ("DEMO_EHR_SOURCE", "DEMO_ATHENA", "ATHENA_"))}
    s = DemoSettings.from_env(syn_env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    svc = DbStaffService(own, s, now=lambda: NOW, gateway=StubGateway(TEST_NUMBER))
    try:
        assert await svc.run_writeback(tid) == "scenario_unavailable"
    finally:
        await stop(own, svc)
    sql(demo_db_url, "UPDATE reminder_records SET tenant_id = 'other-clinic'")
    own, svc = await proc(env, Api())
    try:
        assert await svc.run_writeback(tid) == "scenario_unavailable"
    finally:
        await stop(own, svc)
    assert rows(demo_db_url)[0][3:] == ("1", 1)


# ── stored-zone snapshot date (T-MS-16/17) ───────────────────────────────────

LATE = rec("1001", start="23:30")                         # Denver 23:30 = NY 01:30 next day


async def _late_task(demo_env: dict[str, str], url: str) -> tuple[dict[str, str], int]:
    env = _env(demo_env)
    await tables(env)
    own, svc = await proc(env, Api(zone=DENVER, by_date={MDY: [LATE]}))
    try:
        await svc.page_data()
        tid = await confirm(svc, url, "ath-e2-1001")
    finally:
        await stop(own, svc)
    start = sql(url, "SELECT appt_start FROM reminder_records")[0][0]
    assert start.astimezone(UTC) == datetime(2026, 10, 29, 5, 30, tzinfo=UTC)
    return env, tid


async def test_t_ms_16_stored_zone_date_failed_snapshot_refuses(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env, tid = await _late_task(demo_env, demo_db_url)
    clock = Clock()
    own, svc = await proc(env, Api(zone=DENVER, by_date={"10/29/2026": []}, fail={MDY}),
                          clock)
    try:
        await svc.page_data()                             # 10/28 (Denver date) fails
        clock.for_target(date(2026, 10, 29))
        await svc.page_data()                             # 10/29 (NY date) loads
        out = await svc.run_writeback(tid)
    finally:
        await stop(own, svc)
    assert out in ("scenario_unavailable", "source_changed")
    assert rows(demo_db_url)[0][3:] == ("1", 1)


async def test_t_ms_17_stored_zone_date_loaded_snapshot_admits(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    env, tid = await _late_task(demo_env, demo_db_url)
    clock = Clock()
    own, svc = await proc(env, Api(zone=DENVER, by_date={MDY: [LATE]},
                                   fail={"10/29/2026"}), clock)
    try:
        await svc.page_data()                             # 10/28 loads with the PAID
        clock.for_target(date(2026, 10, 29))
        await svc.page_data()                             # 10/29 fails (not consulted)
        out = await svc.run_writeback(tid)
    finally:
        await stop(own, svc)
    assert out == "DONE"
    assert rows(demo_db_url)[0][3:] == ("2", 2)


# ── duplicates and concurrency (T-MS-13/27/28) ───────────────────────────────


async def test_t_ms_13_repeats_across_restart(demo_env: dict[str, str],
                                              demo_db_url: str) -> None:
    env, tid = await fresh(demo_env, demo_db_url)
    gw = StubGateway(TEST_NUMBER)
    own, svc = await proc(env, Api(by_date={MDY: [rec("1001")]}), gw=gw)
    try:
        assert await svc.run_writeback(tid) == "DONE"
        with pytest.raises(ValueError):
            await svc.run_writeback(tid)
        await svc.page_data()
        await svc.send_batch()
        await svc.send_to_appointment("ath-e3-1001")
    finally:
        await stop(own, svc)
    assert gw.calls == []
    assert len(sql(demo_db_url, "SELECT 1 FROM reminder_records")) == 1
    assert len(sql(demo_db_url, "SELECT 1 FROM contact_history")) == 1
    assert rows(demo_db_url) == [("1001", "athp-60178", T0900, "2", 2)]


class Barrier:
    """Two hooks that must both be inside their seed transactions (observed overlap)
    before either commits; bounded by a timeout."""

    def __init__(self, n: int = 2) -> None:
        self.n, self.inside, self.go = n, 0, asyncio.Event()
        self.overlap = False

    async def __call__(self, stage: str) -> None:
        if stage != "inserted":
            return
        self.inside += 1
        if self.inside == self.n:
            self.overlap = True
            self.go.set()
        await asyncio.wait_for(self.go.wait(), timeout=10)


async def test_t_ms_27_concurrent_two_date_seed_one_bound(demo_env: dict[str, str],
                                                         demo_db_url: str) -> None:
    env = _env(demo_env)
    await tables(env)
    D2s = "10/29/2026"
    api = Api(by_date={MDY: [rec("1001")], D2s: [rec("1001", pid="60179")]})
    clock = Clock()
    own, svc = await proc(env, api, clock)
    svc.seed_hook = Barrier()
    try:
        await asyncio.gather(svc._source(D1), svc._source(date(2026, 10, 29)))  # noqa: SLF001
        assert svc.seed_hook.overlap
        clock.for_target(D1)
        d1 = await svc.page_data()
        clock.for_target(date(2026, 10, 29))
        d2 = await svc.page_data()
    finally:
        await stop(own, svc)
    bound = rows(demo_db_url)
    assert [r[0] for r in bound] == ["1001"]               # exactly one binding
    published = appt_ids(d1) + appt_ids(d2)
    assert len(published) == 1
    loser = d2 if appt_ids(d1) else d1
    assert loser["source"]["counts"].get("PAID_ELSEWHERE") == 1


async def test_t_ms_28_seed_failure_rolls_back_nothing_published(
    demo_env: dict[str, str], demo_db_url: str,
) -> None:
    """D2's seed is held open inside its transaction (observed) while a D1 send
    creates a reminder; then D2's seed fails and must roll back with nothing
    published for D2."""
    env = _env(demo_env)
    await tables(env)
    d2 = date(2026, 10, 29)
    api = Api(by_date={MDY: [rec("1001")], "10/29/2026": [rec("2001", pid="60180")]})
    clock = Clock()
    own, svc = await proc(env, api, clock)
    inside, release = asyncio.Event(), asyncio.Event()

    async def held_then_fail(stage: str) -> None:
        if stage == "inserted":
            inside.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            raise RuntimeError("injected seed failure")

    try:
        await svc.page_data()                              # D1 published normally
        svc.seed_hook = held_then_fail
        clock.for_target(d2)
        d2_page = asyncio.create_task(svc.page_data())
        await asyncio.wait_for(inside.wait(), timeout=10)   # D2 seed txn is open
        clock.for_target(D1)
        sent = await svc.send_to_appointment("ath-e2-1001")  # overlaps the D2 seed
        release.set()
        res = await asyncio.gather(d2_page, return_exceptions=True)
        clock.for_target(d2)
        svc.seed_hook = None
        after = await svc.page_data()
    finally:
        await stop(own, svc)
    assert sent["state"] == "SUBMITTED"
    assert isinstance(res[0], BaseException) or appt_ids(res[0]) == []
    assert appt_ids(after) == [] or after["source"]["error"] is not None
    assert [r[0] for r in rows(demo_db_url)] == ["1001"]   # D2 seed rolled back
    assert [r[0] for r in sql(demo_db_url, "SELECT emr_appt_id FROM reminder_records")] == [
        "ath-e2-1001"]
