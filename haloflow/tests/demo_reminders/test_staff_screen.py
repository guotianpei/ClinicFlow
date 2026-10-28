"""Staff-screen fixes (package v1 + v1r1, Codex ALIGN 2026-10-28).

G-E3-1  R-S1 Send only on eligible rows; R-S2 Simulate reply only on a current
        reminder in SUBMITTED or NO_RESPONSE. Server-side handling unchanged.
G-E3-2  R-S3 follow-up items can be marked done (fenced open -> done), stay
        listed, open first. Owner decision OD-S1.
G-E3-3  R-S4 "; Opted out — no texts" on every row whose phone key is opted out.
        Owner decision OD-S2.
G-R1-1  R-S5 after a restart a slot's earlier reminder is shown "(before restart)".

Assertions on controls are row-local (the appointment's own <tr>), per Codex F-S2.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from haloflow_demo.ownership import acquire_ownership
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import DemoSettings, validate_demo_database_url
from haloflow_demo.web import CSRF_FIELD, build_staff_app

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # 09:00 EDT; target date 2026-10-28
TEST_NUMBER = "+15550100141"
SENDER = "+15550100100"
SECRET = "test-callback-secret-0123456789"
E1 = "syn-e1-20261028-a-"
E2 = "syn-e2-20261028-a-"
SEND = "Send to this appointment (simulated)"
OPTED = "; Opted out — no texts"
BEFORE = " (before restart)"


# ── helpers ───────────────────────────────────────────────────────────────────


def _env(demo_env: dict[str, str]) -> dict[str, str]:
    return {**demo_env, "DEMO_SMS_TEST_NUMBER": TEST_NUMBER, "DEMO_SMS_SENDER": SENDER,
            "DEMO_CALLBACK_SECRET": SECRET}


async def _start(env: dict[str, str]) -> tuple[Any, DbStaffService]:
    s = DemoSettings.from_env(env)
    own = await acquire_ownership(s, validate_demo_database_url(s))
    return own, DbStaffService(own, s, now=lambda: NOW)


def _q(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _aclient(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
                             base_url="http://127.0.0.1:8080")


def _app(own: Any, svc: DbStaffService) -> Any:
    return build_staff_app(svc, csrf_secret=b"k" * 32, unavailable=lambda: own.owner_lost)


def _token(html: str) -> str:
    m = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', html)
    assert m
    return m.group(1)


def _row(html: str, appt_id: str) -> str:
    m = re.search(rf"<tr><td>{re.escape(appt_id)}</td>.*?</tr>", html, re.S)
    assert m, appt_id
    return m.group(0)


def _items(html: str) -> list[str]:
    section = html.split("<h2>Follow-up items</h2>", 1)[1].split("</ul>", 1)[0]
    return re.findall(r"<li>.*?</li>", section, re.S)


def _replies(row: str) -> int:
    return len(re.findall(r"Simulate reply (?:1|2|3|STOP)<", row))


async def _page(c: httpx.AsyncClient) -> str:
    r = await c.get("/demo/staff")
    assert r.status_code == 200
    return r.text


async def _send(svc: DbStaffService, n: int) -> int:
    sent = await svc.send_to_appointment(E1 + str(n))
    assert sent["state"] == "SUBMITTED", sent
    return int(sent["reminder_id"])


async def _reply(svc: DbStaffService, rid: int, text: str) -> None:
    res = await svc.simulate_reply(rid, text)
    assert res.status == 204
    await svc.process_now()


async def _status(svc: DbStaffService) -> dict[str, str]:
    return {a["emr_appt_id"]: a["status"] for a in (await svc.page_data())["appointments"]}


_SNAPSHOT_TABLES = ("reminder_records", "writeback_tasks", "inbound_messages", "opt_outs",
                    "contact_history", "send_quota")


def _snapshot(url: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every column of each table, rows in a deterministic order (Codex tests r1)."""
    return {t: sorted(_q(url, f"SELECT * FROM {t}"), key=repr) for t in _SNAPSHOT_TABLES}


# ── G-E3-1 / R-S1: Send visibility follows Preview ───────────────────────────


async def test_t_s1a_s1c_send_shown_only_on_eligible_rows(demo_env: dict[str, str]) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        for n in (1, 2, 3, 4, 5):
            assert SEND in _row(html, E1 + str(n)), n
        assert SEND not in _row(html, E1 + "6")      # no phone
        assert SEND not in _row(html, E1 + "7")      # cancelled
    finally:
        await own.close()


async def test_t_s1b_send_hidden_after_send(demo_env: dict[str, str]) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        await _send(svc, 1)
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        assert SEND not in _row(html, E1 + "1")
        assert SEND in _row(html, E1 + "2")
    finally:
        await own.close()


async def test_t_s1d_send_visibility_equals_preview_incl_after_restart(
    demo_env: dict[str, str]
) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        await _send(svc, 1)
        await _send(svc, 4)
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        for p in await svc.preview_batch():
            assert (SEND in _row(html, p["emr_appt_id"])) == (p["decision"] == "eligible"), p
    finally:
        await own.close()
    own, svc = await _start(env)                       # restart: epoch 2
    try:
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        preview = await svc.preview_batch()
        assert {p["emr_appt_id"] for p in preview} == {E2 + str(n) for n in range(1, 8)}
        for p in preview:
            assert (SEND in _row(html, p["emr_appt_id"])) == (p["decision"] == "eligible"), p
        assert SEND not in _row(html, E2 + "1") and SEND not in _row(html, E2 + "4")
    finally:
        await own.close()


async def test_t_s1e_stale_send_post_still_skipped_server_side(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        async with _aclient(_app(own, svc)) as c:
            tok = _token(await _page(c))
            await _send(svc, 1)
            r = await c.post("/demo/appointments/send",
                             data={CSRF_FIELD: tok, "emr_appt_id": E1 + "1"})
            assert r.status_code == 303
        assert _q(demo_db_url, "SELECT count(*) FROM reminder_records") == [(1,)]
    finally:
        await own.close()


# ── G-E3-1 / R-S2: Simulate reply visibility ─────────────────────────────────


async def test_t_s2a_submitted_row_has_four_reply_controls(demo_env: dict[str, str]) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        await _send(svc, 1)
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        assert _replies(_row(html, E1 + "1")) == 4
        assert _replies(_row(html, E1 + "2")) == 0    # no reminder
    finally:
        await own.close()


@pytest.mark.parametrize("reply,state", [("1", "CONFIRMED"), ("2", "RESCHEDULE_REQUESTED"),
                                         ("3", "CANCEL_REQUESTED")])
async def test_t_s2b_answered_rows_have_no_reply_controls(
    demo_env: dict[str, str], demo_db_url: str, reply: str, state: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        rid = await _send(svc, 1)
        await _send(svc, 2)
        await _reply(svc, rid, reply)
        assert _q(demo_db_url, "SELECT state FROM reminder_records WHERE id = %s",
                  (rid,)) == [(state,)]
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        assert _replies(_row(html, E1 + "1")) == 0
        assert _replies(_row(html, E1 + "2")) == 4    # row-local: neighbour unaffected
    finally:
        await own.close()


async def test_t_s2c_not_sent_row_has_no_reply_controls(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        rid = await _send(svc, 4)
        await _reply(svc, rid, "STOP")
        sent = await svc.send_to_appointment(E1 + "5")
        assert sent["state"] == "NOT_SENT", sent
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        assert _replies(_row(html, E1 + "5")) == 0
    finally:
        await own.close()


async def test_t_s2d_no_response_row_keeps_reply_controls(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        rid = await _send(svc, 1)
        _q(demo_db_url, "UPDATE reminder_records SET state = 'NO_RESPONSE' WHERE id = %s", (rid,))
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        assert _replies(_row(html, E1 + "1")) == 4
    finally:
        await own.close()


async def test_t_s2e_before_restart_row_has_no_reply_controls(demo_env: dict[str, str]) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        await _send(svc, 1)
    finally:
        await own.close()
    own, svc = await _start(env)
    try:
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        row = _row(html, E2 + "1")
        assert "reminder: Submitted" + BEFORE in row
        assert _replies(row) == 0
    finally:
        await own.close()


@pytest.mark.parametrize("state", ["CLAIMED", "REJECTED", "UNCERTAIN"])
async def test_t_s2f_claimed_rejected_uncertain_rows_have_no_reply_controls(
    demo_env: dict[str, str], demo_db_url: str, state: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        rid = await _send(svc, 1)
        await _send(svc, 2)
        _q(demo_db_url, "UPDATE reminder_records SET state = %s WHERE id = %s", (state, rid))
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        assert _replies(_row(html, E1 + "1")) == 0
        assert _replies(_row(html, E1 + "2")) == 4
    finally:
        await own.close()


# ── G-E3-2 / R-S3: Mark follow-up items done ─────────────────────────────────


async def _two_items(svc: DbStaffService, url: str) -> tuple[int, int]:
    await _reply(svc, await _send(svc, 1), "2")   # reschedule item
    await _reply(svc, await _send(svc, 2), "3")   # cancel item
    ids = [r[0] for r in _q(url, "SELECT id FROM follow_up_items ORDER BY id")]
    assert len(ids) == 2, ids
    return ids[0], ids[1]


def _item_with(items: list[str], item_id: int) -> str:
    hits = [i for i in items if f'name="item_id" value="{item_id}"' in i]
    assert len(hits) == 1, items
    return hits[0]


async def test_t_s3a_l_mark_done_one_item_other_stays_open(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        first, second = await _two_items(svc, demo_db_url)
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
            items = _items(html)
            assert all("[open]" in i and "Mark done" in i for i in items)
            r = await c.post("/demo/items/done", data={CSRF_FIELD: _token(html),
                                                       "item_id": str(first)})
            assert r.status_code == 303
            items = _items(await _page(c))
        assert _q(demo_db_url, "SELECT id, status FROM follow_up_items ORDER BY id") == [
            (first, "done"), (second, "open")]
        other = _item_with(items, second)
        assert "[open]" in other and "Mark done" in other
        done = [i for i in items if "[done]" in i]
        assert len(done) == 1 and "Mark done" not in done[0]
        assert "reschedule" in done[0].lower()
    finally:
        await own.close()


async def test_t_s3b_c_repeat_unknown_and_malformed_ids(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        first, second = await _two_items(svc, demo_db_url)
        async with _aclient(_app(own, svc)) as c:
            tok = _token(await _page(c))
            assert (await c.post("/demo/items/done", data={CSRF_FIELD: tok,
                    "item_id": str(first)})).status_code == 303
            assert (await c.post("/demo/items/done", data={CSRF_FIELD: tok,
                    "item_id": str(first)})).status_code == 303          # already done
            assert (await c.post("/demo/items/done", data={CSRF_FIELD: tok,
                    "item_id": "99999"})).status_code == 303             # unknown
            assert (await c.post("/demo/items/done", data={CSRF_FIELD: tok,
                    "item_id": "x1"})).status_code == 400                # malformed
            assert (await c.post("/demo/items/done",
                                 data={CSRF_FIELD: tok})).status_code == 400
        assert _q(demo_db_url, "SELECT id, status FROM follow_up_items ORDER BY id") == [
            (first, "done"), (second, "open")]
    finally:
        await own.close()


async def test_t_s3d_e_csrf_and_origin_enforced(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        first, _ = await _two_items(svc, demo_db_url)
        async with _aclient(_app(own, svc)) as c:
            tok = _token(await _page(c))
            assert (await c.post("/demo/items/done",
                                 data={"item_id": str(first)})).status_code == 403
            assert (await c.post("/demo/items/done", data={CSRF_FIELD: "wrong",
                    "item_id": str(first)})).status_code == 403
            r = await c.post("/demo/items/done", data={CSRF_FIELD: tok, "item_id": str(first)},
                             headers={"Origin": "http://evil.example"})
            assert r.status_code in (403, 400)
        assert _q(demo_db_url, "SELECT status FROM follow_up_items WHERE id = %s",
                  (first,)) == [("open",)]
    finally:
        await own.close()


async def test_t_s3f_get_does_not_mutate_items(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        await _two_items(svc, demo_db_url)
        before = _q(demo_db_url, "SELECT id, status FROM follow_up_items ORDER BY id")
        async with _aclient(_app(own, svc)) as c:
            await _page(c)
            r = await c.get("/demo/items/done")
            assert r.status_code in (404, 405)
        assert _q(demo_db_url, "SELECT id, status FROM follow_up_items ORDER BY id") == before
    finally:
        await own.close()


async def test_t_s3g_owner_lost_503_and_unchanged(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        first, _ = await _two_items(svc, demo_db_url)
        async with _aclient(_app(own, svc)) as c:
            tok = _token(await _page(c))
            _q(demo_db_url, "UPDATE demo_settings SET owner_epoch = owner_epoch + 1")
            r = await c.post("/demo/items/done", data={CSRF_FIELD: tok, "item_id": str(first)})
            assert r.status_code == 503
        assert own.owner_lost is True
        assert _q(demo_db_url, "SELECT status FROM follow_up_items WHERE id = %s",
                  (first,)) == [("open",)]
    finally:
        await own.close()


async def test_t_s3h_mark_done_changes_only_item_status(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        first, _ = await _two_items(svc, demo_db_url)
        before = _snapshot(demo_db_url)
        item_before = _q(demo_db_url, "SELECT id, source_key, kind, inbound_id, reminder_id,"
                         " detail FROM follow_up_items ORDER BY id")
        assert await svc.mark_item_done(first) is True
        assert _snapshot(demo_db_url) == before
        assert _q(demo_db_url, "SELECT id, source_key, kind, inbound_id, reminder_id,"
                  " detail FROM follow_up_items ORDER BY id") == item_before
        assert await svc.mark_item_done(first) is False          # no-op the second time
    finally:
        await own.close()


async def test_t_s3i_open_items_listed_first(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        first, second = await _two_items(svc, demo_db_url)
        # same status (both open): id ascending (Codex tests r1, optional point)
        assert [i["id"] for i in (await svc.page_data())["items"]] == [first, second]
        await svc.mark_item_done(first)
        async with _aclient(_app(own, svc)) as c:
            items = _items(await _page(c))
        assert len(items) == 2
        assert f'value="{second}"' in items[0] and "[open]" in items[0]
        assert "[done]" in items[1]
        data = await svc.page_data()
        assert [i["id"] for i in data["items"]] == [second, first]
        assert [i["status"] for i in data["items"]] == ["open", "done"]
    finally:
        await own.close()


async def test_t_s3j_item_detail_is_escaped(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        first, _ = await _two_items(svc, demo_db_url)
        _q(demo_db_url, "UPDATE follow_up_items SET detail = %s WHERE id = %s",
           ("<script>x</script>", first))
        async with _aclient(_app(own, svc)) as c:
            html = await _page(c)
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;x&lt;/script&gt;" in html
    finally:
        await own.close()


async def test_t_s3k_done_persists_across_restart(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        first, second = await _two_items(svc, demo_db_url)
        assert await svc.mark_item_done(first) is True
    finally:
        await own.close()
    own, svc = await _start(env)
    try:
        async with _aclient(_app(own, svc)) as c:
            items = _items(await _page(c))
        done = [i for i in items if "[done]" in i]
        assert len(done) == 1 and "Mark done" not in done[0]
        assert "[open]" in _item_with(items, second)
    finally:
        await own.close()


# ── G-E3-3 / R-S4: opt-out marker ────────────────────────────────────────────


async def test_t_s4a_b_c_marker_after_stop_on_rows_with_a_phone(
    demo_env: dict[str, str]
) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        rid = await _send(svc, 4)
        assert not any(OPTED in s for s in (await _status(svc)).values())   # T-S4a
        await _reply(svc, rid, "STOP")
        status = await _status(svc)
        assert status[E1 + "4"].endswith("reminder: Submitted" + OPTED)    # G-E3-3 row
        for n in (1, 2, 3, 5, 7):                                          # phone alias rows
            assert status[E1 + str(n)].endswith("reminder: none" + OPTED), n
        assert OPTED not in status[E1 + "6"]                               # no phone
    finally:
        await own.close()


async def test_t_s4d_marker_persists_after_restart(demo_env: dict[str, str]) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        await _reply(svc, await _send(svc, 4), "STOP")
    finally:
        await own.close()
    own, svc = await _start(env)
    try:
        status = await _status(svc)
        assert status[E2 + "4"].endswith("reminder: Submitted" + BEFORE + OPTED)
        assert status[E2 + "1"].endswith("reminder: none" + OPTED)
        assert OPTED not in status[E2 + "6"]
    finally:
        await own.close()


async def test_t_s4e_no_test_number_no_marker_no_error(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    own, svc = await _start(demo_env)             # DEMO_SMS_TEST_NUMBER unset
    try:
        _q(demo_db_url, "INSERT INTO opt_outs(phone_key) VALUES ('some-key')")
        status = await _status(svc)
        assert all(s.endswith("reminder: none") for s in status.values())
    finally:
        await own.close()


# ── G-R1-1 / R-S5: earlier reminder shown after restart ──────────────────────


async def test_t_s5a_restart_shows_before_restart_labels(demo_env: dict[str, str]) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        await _reply(svc, await _send(svc, 1), "1")
        await _send(svc, 2)
    finally:
        await own.close()
    own, svc = await _start(env)
    try:
        status = await _status(svc)
        assert status[E2 + "1"].endswith("reminder: Confirmed by patient" + BEFORE)
        assert status[E2 + "2"].endswith("reminder: Submitted" + BEFORE)
        for n in (3, 4, 5, 6, 7):
            assert status[E2 + str(n)].endswith("reminder: none"), n
    finally:
        await own.close()


async def test_t_s5b_exact_id_takes_precedence(demo_env: dict[str, str], demo_db_url: str) -> None:
    own, svc = await _start(_env(demo_env))
    try:
        await _send(svc, 1)
        # an older reminder for the same slot (another epoch's id), lower id than the exact one
        _q(demo_db_url, "INSERT INTO reminder_records(id, tenant_id, emr_appt_id, emr_patient_id,"
           " appt_start, state) SELECT -1, tenant_id, 'syn-e0-20261028-a-1', emr_patient_id,"
           " appt_start, 'CONFIRMED' FROM reminder_records LIMIT 1")
        _q(demo_db_url, "INSERT INTO reminder_records(id, tenant_id, emr_appt_id, emr_patient_id,"
           " appt_start, state) SELECT 1000, tenant_id, 'syn-e9-20261028-a-1', emr_patient_id,"
           " appt_start, 'CONFIRMED' FROM reminder_records WHERE id > 0 LIMIT 1")
        status = await _status(svc)
        assert status[E1 + "1"].endswith("reminder: Submitted")
    finally:
        await own.close()


async def test_t_s5c_fixture_d_rows_show_only_their_own_slot(demo_env: dict[str, str]) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        await _send(svc, 4)                       # patient D, 10:30; a-5 is D at 15:00
    finally:
        await own.close()
    own, svc = await _start(env)
    try:
        status = await _status(svc)
        assert status[E2 + "4"].endswith("reminder: Submitted" + BEFORE)
        assert status[E2 + "5"].endswith("reminder: none")
    finally:
        await own.close()


async def test_t_s5d_highest_id_slot_reminder_chosen(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        await _send(svc, 1)
    finally:
        await own.close()
    # pre-F-R1-1 data: a second reminder for the same slot with a higher id
    _q(demo_db_url, "INSERT INTO reminder_records(id, tenant_id, emr_appt_id, emr_patient_id,"
       " appt_start, state) SELECT 1000, tenant_id, 'syn-e0-20261028-a-1x', emr_patient_id,"
       " appt_start, 'CANCEL_REQUESTED' FROM reminder_records LIMIT 1")
    own, svc = await _start(env)
    try:
        status = await _status(svc)
        assert status[E2 + "1"].endswith(
            "reminder: Cancel requested — staff to cancel in EHR" + BEFORE)
    finally:
        await own.close()


async def test_t_s5e_other_tenant_reminder_never_shown(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    env = _env(demo_env)
    own, svc = await _start(env)
    try:
        await _send(svc, 1)
    finally:
        await own.close()
    _q(demo_db_url, "UPDATE reminder_records SET tenant_id = 'other-tenant'")
    own, svc = await _start(env)
    try:
        status = await _status(svc)
        assert status[E2 + "1"].endswith("reminder: none")
    finally:
        await own.close()
