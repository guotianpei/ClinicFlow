"""Incoming fax routing FX-IN, package FX3b — the fax staff screen, SINGLE-TENANT synthetic install
rows (RED BY DESIGN). Governing: `fx3b_support.py` (interface pin, approved copy, oracle).

One disposable PostgreSQL 17 cluster per row; the real installer, lifecycle and app (fax
profile, target 153); ASGI requests; pages parsed by the support module's own HTMLParser.

- FX3b-C00 CONTROL: the JSON fax routes answer 200 for the act principal (list / get / history /
           queues), as on main
- FX3b-C02 CONTROL: the competing-staff fixture: a second act staff member's move through the
           action gateway commits a `moved` row with its id; `open` leaves the version unchanged
- FX3b-H01 actor-only (OD-FX11): the act principal sees the "Incoming faxes" entry and the
           pages; a read-only principal sees no entry, and GET list / detail (existing and absent
           id) and POST open / move / file / sort WITH a valid csrf token answer 403 with no fax
           data in the body; no fax or history change
- FX3b-H02 csrf: missing and wrong csrf on every authorized POST -> 403; no fax or history
           change
- FX3b-H03 GET is side-effect free; the row's Open form records `opened` by the principal and
           redirects 303 to the detail page
- FX3b-H05 sorting (OD-FX7, OD-FX9): the needs-sorting section precedes every queue section;
           "Fax needs sorting (3)"; each sorting row shows exactly its approved reason; the routed
           fax is in its queue's section; queue sections in t151 order; Sort offers exactly the
           active queue ids and routes the fax to the selected queue
- FX3b-H06 filed and inactive (OD-FX8, OD-FX12): a fax filed by a distinct staff member stays
           listed under its renamed queue (by id, current name); its detail shows the filed
           notice, that filer's name in the filed history event only (the received event shows
           "System"), no Move or Mark filed; a fax filed from the screen shows the principal in
           its filed event; an inactive queue is listed but never selectable
- FX3b-H07 conflicts (OD-FX12 case 0, OD-FX13 cases 1-5; Codex fixtures): the competing action
           commits first; the screen's stale submit answers 409 with exactly one notice = the
           approved lead and case line (name, queue and time from the competing state row; case
           0 ignores a later open by a third staff member; case 5 names nothing); fax and history
           equal the snapshot taken just before the submit, again after a GET refresh and after
           re-submitting the stale form; "Refresh this fax" loads exactly the actions of the
           refreshed state (routed: move, file; needs sorting: sort; filed: none), each bound to
           the current version; case 0 then succeeds from the refreshed form
- FX3b-H08 From column (OD-FX10): saved name, as sent, neither, withheld; the known synthetic
           numbers in every formatted variant are absent from list and detail pages
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import b2_support as s
import b3b_support as bb
import el4_support as e4
import fx3b_support as f
import pytest

pytestmark = pytest.mark.postgres

FAX = f.FAX
SENDER_Z = "+18045550101"     # routed by rule to General (the original queue Z)
X, Y, Z = "Labs/Results", "Imaging", "General"


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


def _no_fax_data(raw: bytes, fax_ids: list[str]) -> None:
    text = raw.decode("utf-8")
    for fid in fax_ids:
        assert fid not in text, fid
    for leaked in (f.SORTING_HEADING, *f.REASON_LABELS.values(), *f.DEFAULT_QUEUES,
                   "ending "):
        assert leaked not in text, leaked


def test_fx3b_c00_json_routes_control(pg: s.PgCluster, tmp_path: Path) -> None:
    """C."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-c00") as (_lc, app):
            tok = await e4.token(app)
            fid = await f.deliver(app, tok, None)
            for target in ("/faxes?queue=needs_sorting", f"/faxes/{fid}",
                           f"/faxes/{fid}/history", "/queues"):
                got = await f.get(app, f.JSON_PREFIX + target)
                assert got.status == 200, target

    asyncio.run(body())


def test_fx3b_c02_competing_staff_fixture(pg: s.PgCluster, tmp_path: Path) -> None:
    """C."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-c02") as (_lc, app):
            tok = await e4.token(app)
            f.seed_rule(inst, SENDER_Z, Z)
            fid = await f.deliver(app, tok, SENDER_Z)
            dana = f.seed_staff(inst, "Dana Reyes")
            chris = f.seed_staff(inst, "Chris Ode")
            q = f.queue_ids(inst)
            assert f.fax_row(inst, fid) == {"state": "routed", "queue_id": q[Z], "version": 1}
            out = f.gateway_act(inst, dana, "move", fid, expected_version=1, queue_id=q[Y])
            assert (out["outcome"], out["version"]) == ("applied", 2)
            f.gateway_act(inst, chris, "open", fid)
            assert f.fax_row(inst, fid)["version"] == 2      # open never changes the version
            rows = f.history(inst, fid)
            assert [(r["action"], r["actor"]) for r in rows[1:]] == [("moved", dana),
                                                                     ("opened", chris)]
            assert (rows[1]["from"], rows[1]["to"]) == (q[Z], q[Y])

    asyncio.run(body())


def test_fx3b_h01_actor_only(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (OD-FX11)."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-h01") as (lc, app):
            tok = await e4.token(app)
            fid = await f.deliver(app, tok, None)
            q = f.queue_ids(inst)
            # act principal: the entry and the pages
            items = f.fax_page(await f.get(app, f.ITEMS))
            assert (f.UI, f.ENTRY_LABEL) in items.links, items.links
            assert (await f.get(app, f.UI)).status == 200
            assert (await f.get(app, f.detail(fid))).status == 200
            # read-only principal
            bb.standing_override(inst, access=("read",))
            before = (f.fax_row(inst, fid), f.all_history(inst))
            items = f.fax_page(await f.get(app, f.ITEMS))
            assert f.UI not in [h for h, _t in items.links]
            assert f.ENTRY_LABEL not in items.text
            absent = str(uuid.uuid4())
            for target in (f.UI, f.detail(fid), f.detail(absent)):
                got = await f.get(app, target)
                assert got.status == 403, target
                _no_fax_data(got.raw, [fid])
            csrf = lc.csrf_token
            assert csrf
            posts = {"open": [("csrf", csrf)],
                     "move": [("csrf", csrf), ("expected_version", "1"), ("queue_id", q[Y])],
                     "file": [("csrf", csrf), ("expected_version", "1")],
                     "sort": [("csrf", csrf), ("expected_version", "1"), ("queue_id", q[Y])]}
            for target_id in (fid, absent):
                for action, fields in posts.items():
                    got = await f.post_form(app, f.act_path(target_id, action), fields)
                    assert got.status == 403, (action, target_id, got.status)
                    _no_fax_data(got.raw, [fid])
            assert (f.fax_row(inst, fid), f.all_history(inst)) == before

    asyncio.run(body())


def test_fx3b_h02_csrf(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-h02") as (_lc, app):
            tok = await e4.token(app)
            f.seed_rule(inst, SENDER_Z, Z)
            routed = await f.deliver(app, tok, SENDER_Z)
            sorting = await f.deliver(app, tok, None)
            q = f.queue_ids(inst)
            assert (await f.get(app, f.detail(routed))).status == 200     # served (A here)
            before = (f.fax_row(inst, routed), f.fax_row(inst, sorting), f.all_history(inst))
            cases = [(routed, "open", []), (routed, "move", [("expected_version", "1"),
                                                             ("queue_id", q[Y])]),
                     (routed, "file", [("expected_version", "1")]),
                     (sorting, "sort", [("expected_version", "1"), ("queue_id", q[Y])])]
            for fid, action, rest in cases:
                for csrf in (None, "not-the-token"):
                    fields = ([] if csrf is None else [("csrf", csrf)]) + rest
                    got = await f.post_form(app, f.act_path(fid, action), fields)
                    assert got.status == 403, (action, csrf, got.status)
            assert (f.fax_row(inst, routed), f.fax_row(inst, sorting),
                    f.all_history(inst)) == before

    asyncio.run(body())


def test_fx3b_h03_get_is_side_effect_free_open_records(pg: s.PgCluster, tmp_path: Path) -> None:
    """A."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-h03") as (_lc, app):
            tok = await e4.token(app)
            fid = await f.deliver(app, tok, None)
            before = f.history(inst, fid)
            for _ in range(2):
                assert (await f.get(app, f.detail(fid))).status == 200
                assert (await f.get(app, f.UI)).status == 200
            assert f.history(inst, fid) == before
            listing = f.fax_page(await f.get(app, f.UI))
            _text, forms = listing.region(f"data-fax-id={fid}")
            assert f.act_path(fid, "open") in forms
            opened = await f.post_form(app, f.act_path(fid, "open"),
                                       f.submit(listing.form(f.act_path(fid, "open"))))
            assert opened.status == 303
            assert opened.header("location") == [f.detail(fid)]
            rows = f.history(inst, fid)
            assert len(rows) == len(before) + 1
            assert (rows[-1]["action"], rows[-1]["actor"]) == ("opened", inst.state["staff_id"])
            assert f.fax_row(inst, fid)["version"] == 1

    asyncio.run(body())


def test_fx3b_h05_sorting(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (OD-FX7, OD-FX9)."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-h05") as (_lc, app):
            tok = await e4.token(app)
            f.seed_rule(inst, SENDER_Z, Z)
            f.seed_rule(inst, "+18045550102", Y)
            f.seed_rule(inst, "+18045550102", X)                 # conflicting rules
            routed = await f.deliver(app, tok, SENDER_Z)
            sorting = {"rules_conflict": await f.deliver(app, tok, "+18045550102"),
                       "no_matching_rule": await f.deliver(app, tok, "+18045550199"),
                       "sender_not_shown": await f.deliver(app, tok, None)}
            q = f.queue_ids(inst)
            pg_ = f.fax_page(await f.get(app, f.UI))
            keys = pg_.region_keys()
            queue_keys = [k for k in keys if k.startswith("data-queue-id=")]
            assert keys.index("id=needs-sorting") < keys.index(queue_keys[0])
            assert queue_keys == [f"data-queue-id={q[n]}" for n in f.DEFAULT_QUEUES]
            section, _forms = pg_.region("id=needs-sorting")
            assert f"{f.SORTING_HEADING} (3)" in section
            for reason, fid in sorting.items():
                row, forms = pg_.region(f"data-fax-id={fid}")
                shown = [code for code, label in f.REASON_LABELS.items() if label in row]
                assert shown == [reason], (fid, row)
                assert f.act_path(fid, "open") in forms
            row, _forms = pg_.region(f"data-fax-id={routed}")
            assert not any(label in row for label in f.REASON_LABELS.values())
            general, _forms = pg_.region(f"data-queue-id={q[Z]}")
            assert "ending 0101" in general and "ending 0101" not in section
            # Sort offers exactly the active queues and routes to the selected one
            target = sorting["no_matching_rule"]
            dpage = f.fax_page(await f.get(app, f.detail(target)))
            sort = dpage.form(f.act_path(target, "sort"))
            assert sorted(sort.selects["queue_id"].options) == sorted(q.values())
            assert sort.hidden("expected_version") == "1"
            assert not dpage.has_form(f.act_path(target, "move"))
            got = await f.post_form(app, sort.action, f.submit(sort, queue_id=q[Y]))
            assert got.status == 303 and got.header("location") == [f.detail(target)]
            assert f.fax_row(inst, target) == {"state": "routed", "queue_id": q[Y], "version": 2}
            last = f.history(inst, target)[-1]
            assert (last["action"], last["actor"], last["to"]) == ("sorted",
                                                                   inst.state["staff_id"], q[Y])

    asyncio.run(body())


def test_fx3b_h06_filed_renamed_inactive(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (OD-FX8, OD-FX12)."""
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-h06") as (_lc, app):
            tok = await e4.token(app)
            f.seed_rule(inst, SENDER_Z, Z)
            filed = await f.deliver(app, tok, SENDER_Z)        # filed by a DISTINCT staff member
            mine = await f.deliver(app, tok, SENDER_Z)         # filed from the screen
            routed = await f.deliver(app, tok, SENDER_Z)
            sorting = await f.deliver(app, tok, None)
            q = f.queue_ids(inst)
            dana = f.seed_staff(inst, "Dana Reyes")
            assert f.gateway_act(inst, dana, "file", filed, expected_version=1)["outcome"] == (
                "applied")
            # the screen's own file action (positive path)
            dpage = f.fax_page(await f.get(app, f.detail(mine)))
            file_form = dpage.form(f.act_path(mine, "file"))
            got = await f.post_form(app, file_form.action, f.submit(file_form))
            assert got.status == 303 and got.header("location") == [f.detail(mine)]
            assert f.fax_row(inst, mine)["state"] == "filed"
            f.rename_queue(inst, Z, "General correspondence")
            f.deactivate_queue(inst, "Pharmacy/Refills")
            listing = f.fax_page(await f.get(app, f.UI))
            general, _forms = listing.region(f"data-queue-id={q[Z]}")
            assert "General correspondence" in general
            row, forms = listing.region(f"data-fax-id={filed}")
            assert row and row in general
            assert f.act_path(filed, "open") in forms
            pharmacy, _forms = listing.region(f"data-queue-id={q['Pharmacy/Refills']}")
            assert "Pharmacy/Refills" in pharmacy                      # listed, identifiable
            # detail: the filed notice; the filer bound to the filed history event (Codex REVISE 2)
            dpage = f.fax_page(await f.get(app, f.detail(filed)))
            assert dpage.alerts == [f.FILED_NOTICE]
            filed_seq = [r["seq"] for r in f.history(inst, filed) if r["action"] == "filed"]
            assert len(filed_seq) == 1
            event, _forms = dpage.region(f"data-history-seq={filed_seq[0]}")
            assert "Dana Reyes" in event and "Demo Clinic Admin" not in event, event
            received, _forms = dpage.region("data-history-seq=1")
            assert f.SYSTEM_ACTOR in received and "Dana Reyes" not in received, received
            assert not dpage.has_form(f.act_path(filed, "move"))
            assert not dpage.has_form(f.act_path(filed, "file"))
            mine_page = f.fax_page(await f.get(app, f.detail(mine)))
            mine_seq = [r["seq"] for r in f.history(inst, mine) if r["action"] == "filed"]
            event, _forms = mine_page.region(f"data-history-seq={mine_seq[0]}")
            assert "Demo Clinic Admin" in event and "Dana Reyes" not in event, event
            active = {v for k, v in q.items() if k != "Pharmacy/Refills"}
            move = f.fax_page(await f.get(app, f.detail(routed))).form(f.act_path(routed, "move"))
            assert set(move.selects["queue_id"].options) <= active
            assert q["Pharmacy/Refills"] not in move.selects["queue_id"].options
            sort = f.fax_page(await f.get(app, f.detail(sorting))).form(
                f.act_path(sorting, "sort"))
            assert set(sort.selects["queue_id"].options) == active

    asyncio.run(body())


# case -> (setup kind, the competing actions, the screen's attempt, its queue)
CONFLICTS: dict[int, tuple[str, list[tuple[str, str | None]], str, str | None]] = {
    0: ("routed", [("move", Y)], "move", X),
    1: ("routed", [("file", None)], "move", X),
    2: ("routed", [("move", Y)], "file", None),
    3: ("routed", [("file", None)], "file", None),
    4: ("sorting", [("sort", Y)], "sort", X),
    5: ("sorting", [("sort", Y), ("move", Z)], "sort", X),
}


@pytest.mark.parametrize("case", sorted(CONFLICTS))
def test_fx3b_h07_conflict(pg: s.PgCluster, tmp_path: Path, case: int) -> None:
    """A (OD-FX12 case 0, OD-FX13 cases 1-5; Codex conflict fixtures)."""
    kind, competing, attempt, attempted = CONFLICTS[case]
    inst = e4.install(pg, tmp_path, FAX)

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name=f"fx3b-h07-{case}") as (_lc, app):
            tok = await e4.token(app)
            f.seed_rule(inst, SENDER_Z, Z)
            fid = await f.deliver(app, tok, SENDER_Z if kind == "routed" else None)
            q = f.queue_ids(inst)
            dana = f.seed_staff(inst, "Dana Reyes")
            chris = f.seed_staff(inst, "Chris Ode")
            # A (the screen's principal) holds the original form
            stale = f.fax_page(await f.get(app, f.detail(fid))).form(f.act_path(fid, attempt))
            assert stale.hidden("expected_version") == "1"
            fields = f.submit(stale, **({"queue_id": q[attempted]} if attempted else {}))
            # B commits first (through the action gateway)
            for action, queue in competing:
                version = f.fax_row(inst, fid)["version"]
                out = f.gateway_act(inst, dana, action, fid, expected_version=version,
                                    queue_id=None if queue is None else q[queue])
                assert out["outcome"] == "applied", out
            change = [r for r in f.history(inst, fid) if r["action"] in ("moved", "sorted",
                                                                         "filed")][-1]
            if case == 0:
                f.gateway_act(inst, chris, "open", fid)                # a later open by C
            snap = f.snapshot(inst, fid)
            got = await f.post_form(app, stale.action, fields)
            assert got.status == 409, (case, got.status, got.raw[:300])
            page = f.fax_page(got)
            assert len(page.alerts) == 1, page.alerts
            (notice,) = page.alerts
            values = {"x": attempted or "", "y": Y, "name": "Dana Reyes",
                      "time": f.clinic_time(change["at"])}
            assert notice.startswith(f.CONFLICT_LEAD), notice
            assert f.case_text(case, **values) in notice, notice
            if case == 5:
                for leaked in ("Dana Reyes", "Chris Ode", X, Y, Z, " AM", " PM"):
                    assert leaked not in notice, leaked
            if case == 0:
                assert "Chris Ode" not in notice
            assert (f.detail(fid), f.REFRESH_LABEL) in page.links
            assert f.snapshot(inst, fid) == snap
            # the stale form cannot resubmit; GET refresh changes nothing
            again = await f.post_form(app, stale.action, fields)
            assert again.status == 409
            fresh = f.fax_page(await f.get(app, f.detail(fid)))
            assert f.snapshot(inst, fid) == snap
            # Codex REVISE 3: the exact action set for the refreshed state, then the versions
            current = str(snap[0]["version"])
            expected_actions = {"routed": {"move", "file"}, "needs_sorting": {"sort"},
                                "filed": set()}[snap[0]["state"]]
            shown = {a for a in ("move", "file", "sort") if fresh.has_form(f.act_path(fid, a))}
            assert shown == expected_actions, (case, snap[0]["state"], shown)
            for action in sorted(expected_actions):
                assert fresh.form(f.act_path(fid, action)).hidden("expected_version") == current
            if case == 0:
                move = fresh.form(f.act_path(fid, "move"))
                done = await f.post_form(app, move.action, f.submit(move, queue_id=q[X]))
                assert done.status == 303
                assert f.fax_row(inst, fid)["queue_id"] == q[X]

    asyncio.run(body())


def _variants(e164: str) -> list[str]:
    ten = e164[-10:]
    a, b, c = ten[:3], ten[3:6], ten[6:]
    return [e164, e164[1:], ten, f"{a}-{b}-{c}", f"({a}) {b}-{c}", f"{a}.{b}.{c}",
            f"{a} {b} {c}", f"{b}-{c}", f"{b}{c}"]


def test_fx3b_h08_from_column(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (OD-FX10)."""
    inst = e4.install(pg, tmp_path, FAX)
    saved, sent, neither = "+18045550111", "+18045550122", "+18045550133"

    async def body() -> None:
        async with e4.running(inst, profile=FAX, app_name="fx3b-h08") as (_lc, app):
            tok = await e4.token(app)
            f.save_sender(inst, saved, "Valley Radiology")
            ids = {
                "saved": await f.deliver(app, tok, saved),
                "as_sent": await f.deliver(app, tok, sent,
                                           sender_name="Northside Labs 804-555-0122"),
                "neither": await f.deliver(app, tok, neither),
                "withheld": await f.deliver(app, tok, None),
            }
            expected = {
                "saved": "Valley Radiology · ending 0111",
                "as_sent": "Northside Labs •••0122 (as sent) · ending 0122",
                "neither": "Unknown sender · ending 0133",
                "withheld": f.SENDER_HIDDEN,
            }
            listing = await f.get(app, f.UI)
            lpage = f.fax_page(listing)
            raws = [listing.raw.decode("utf-8")]
            for key, fid in ids.items():
                row, _forms = lpage.region(f"data-fax-id={fid}")
                assert expected[key] in row, (key, row)
                d = await f.get(app, f.detail(fid))
                assert expected[key] in f.fax_page(d).text, key
                raws.append(d.raw.decode("utf-8"))
            for raw in raws:
                for number in (saved, sent, neither):
                    for variant in _variants(number):
                        assert variant not in raw, variant

    asyncio.run(body())
