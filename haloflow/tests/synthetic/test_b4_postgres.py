"""Option B B4: the synthetic HTML staff screen over the B3b app, PostgreSQL 17 (per-case
initdb clusters; B2 installs after a B3a first start).

Class A unless stated (b4_support docstring). Fixtures:
- `inst`: a fresh install after one first start, with all five kinds seeded (rows that write);
- `seeded`: MODULE-scoped, read-only rows and rows that refuse before any service call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import b4_support as b4
import pytest

pytestmark = pytest.mark.postgres

STAFF_NAME = "Demo Clinic Admin"  # haloflow.synthetic.provision.STAFF_NAME (B2)


@pytest.fixture(autouse=True)
def _haloflow_loggers_enabled() -> Iterator[None]:
    """The B3b 5a57ced fixture: re-enable `haloflow.*` loggers that an earlier in-process
    alembic `fileConfig` disabled, so log rows cannot pass vacuously; restore on exit."""
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
    with s.cluster(tmp_path) as c:
        yield c
    s.reap()


@pytest.fixture
def inst(pg: s.PgCluster, tmp_path: Path) -> Iterator[tuple[b3.Install, dict[str, Any]]]:
    i = b3.install(pg, tmp_path)
    bb.first_start(i)
    kinds = bb.seed_kinds(i)
    yield i, kinds
    with contextlib.suppress(ModuleNotFoundError):
        s.mod("hooks").FAULT = None


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[b3.Install, dict[str, Any]]]:
    tmp = tmp_path_factory.mktemp("b4-seeded")
    with s.cluster(tmp) as c:
        i = b3.install(c, tmp)
        bb.first_start(i)
        yield i, bb.seed_kinds(i)
    s.reap()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Spies:
    """Records calls to principal resolution and to every M05 service method under the app
    (v1r1 F1.4 / v1r2 C4: refusals before step 8 make zero calls)."""

    M05 = ("get", "list_items", "history", "reason", "act")

    def __init__(self, lc: Any) -> None:
        self.calls: list[str] = []
        targets = [(lc.services.m05, self.M05), (lc.services.resolver, ("resolve",))]
        for target, names in targets:
            for name in names:
                real = getattr(target, name)

                async def wrapper(*a: Any, _n: str = name, _r: Any = real, **k: Any) -> Any:
                    self.calls.append(_n)
                    return await _r(*a, **k)

                setattr(target, name, wrapper)

    @property
    def writes(self) -> list[str]:
        return [c for c in self.calls if c == "act"]


async def get_page(app: Any, target: str) -> tuple[bb.Resp, b4.Page]:
    r = await bb.asgi(app, "GET", target, headers=bb.get_headers())
    assert r.status == 200, (target, r.status, r.raw[:300])
    return r, b4.html_ok(r)


async def view(app: Any, item_id: str, m: str | None = None) -> b4.Page:
    target = b4.ui_path("ui_view", item_id) + (f"?m={m}" if m else "")
    return (await get_page(app, target))[1]


async def post(app: Any, action: str, fields: list[tuple[str, str]], **hk: Any) -> bb.Resp:
    return await bb.asgi(app, "POST", action, headers=b4.form_headers(**hk),
                         body=b4.form_body(fields))


async def close_form(app: Any, item_id: str) -> b4.Form:
    return b4.form_of(await view(app, item_id), b4.ui_path("ui_resolve", item_id))


async def claim_form(app: Any, item_id: str) -> b4.Form:
    return b4.form_of(await view(app, item_id), b4.ui_path("ui_claim", item_id))


def redirected_to_view(r: bb.Resp, item_id: str, m: str | None = None) -> None:
    assert r.status == 303, (r.status, r.raw[:300])
    assert r.header(bb.LABEL_HEADER) == ["true"], r.headers
    want = b4.ui_path("ui_view", item_id) + (f"?m={m}" if m else "")
    assert b4.location(r) == want, (b4.location(r), want)


# =========================================================================== C: controls

def test_b4_c1_installed_w1_table_is_the_owner_list(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """C (W1): the installed m05_outcome_codes holds exactly the six W1 codes, labels and
    note rule. This is the oracle for P-W1."""
    inst, _ = seeded
    got = b4.installed_outcomes(inst)
    want = {code: (label, code == "other") for code, label in b4.OUTCOME_LABELS.items()}
    assert got == want


def test_b4_c2_seed_gives_five_open_unassigned_items(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """C: the seed yields one open, unassigned item of each kind (the B-UI1..5 starting
    point) and the HTML oracle parses a trivial page."""
    inst, kinds = seeded
    assert set(kinds) == set(b4.KIND_LABELS)
    for k in kinds.values():
        row = bb.item_row(inst, k["item_id"])
        assert (row["state"], row["assignee_id"]) == ("open", None)
    pg = b4.page(bb.Resp(200, [], b"<title>SIMULATED x</title><p>a <b>b</b></p>"))
    assert (pg.title, pg.text) == ("SIMULATED x", "a b")


# =========================================================================== P-W1, P-LBL1

def test_b4_p_w1_ui_outcome_map_equals_installed_table(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (v1 Q5, v1r1 §3): the UI constant map equals the installed table exactly, code,
    label and note requirement."""
    inst, _ = seeded
    ui = b4.ui()
    got = {c: (lbl, c in ui.NOTE_REQUIRED) for c, lbl in ui.OUTCOME_LABELS.items()}
    assert got == b4.installed_outcomes(inst)


def test_b4_p_lbl1_every_ui_page_and_error_page_is_labelled(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (B-L1, R-B4-7, R-B4-8; M67): the list, every item view, an unknown item (404) and an
    unknown filter (422) are HTML with the banner once, a SIMULATED title, the label header
    and the security headers; the CSS route is labelled and is text/css."""
    inst, kinds = seeded

    async def body() -> None:
        async with bb.running(inst) as (_, app):
            await get_page(app, b4.UI)
            for k in kinds.values():
                await view(app, k["item_id"])
            r = await bb.asgi(app, "GET", b4.ui_path("ui_view", uuid.uuid4()),
                              headers=bb.get_headers())
            assert r.status == 404, r.status
            b4.html_ok(r)
            r = await bb.asgi(app, "GET", b4.UI + "?state=bogus", headers=bb.get_headers())
            assert r.status == 422, r.status
            b4.html_ok(r)
            r = await bb.asgi(app, "GET", b4.CSS, headers=bb.get_headers())
            assert r.status == 200 and r.header(bb.LABEL_HEADER) == ["true"], r.status
            assert r.header("content-type")[0].startswith("text/css")

    run(body())


# =========================================================================== P-UI1..5

W1_BY_KIND = {"no_reply": "no_contact_left_message", "reschedule_request": "rescheduled",
              "cancel_request": "cancelled_in_ehr", "unclear_reply": "reached_no_change",
              "ehr_confirmation_check": "no_contact_no_answer"}


@pytest.mark.parametrize("kind", list(b4.KIND_LABELS))
def test_b4_p_ui_kind_listed_viewed_claimed_and_resolved(
        inst: tuple[b3.Install, dict[str, Any]], kind: str) -> None:
    """A (B-UI1..5, R-B4-1..4, W1-W4): the item is listed with its exact kind label; its view
    shows the label, state, assignee, synthetic patient ref and the Claim and Close
    (claim_and_resolve) forms; Claim redirects and assigns it to the principal; Close
    (resolve) with a W1 outcome resolves it; history shows the exact W2 line; no forms
    remain (W4)."""
    i, kinds = inst
    item = kinds[kind]
    item_id = item["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            _, lst = await get_page(app, b4.UI)
            assert b4.ui_path("ui_view", item_id) in lst.links
            assert b4.KIND_LABELS[kind] in lst.text
            v = await view(app, item_id)
            assert b4.KIND_LABELS[kind] in v.text
            assert "Synthetic patient ref: " + item["patient"].replace("-", "")[:8] in v.text
            cf = await claim_form(app, item_id)
            assert cf.hidden("type") == "claim"
            assert (await close_form(app, item_id)).hidden("type") == "claim_and_resolve"
            redirected_to_view(await post(app, cf.action, b4.submit_fields(cf)), item_id)
            row = bb.item_row(i, item_id)
            assert row["assignee_id"] == i.state["staff_id"], row
            assert STAFF_NAME in (await view(app, item_id)).text
            rf = await close_form(app, item_id)
            assert rf.hidden("type") == "resolve"
            assert sorted(rf.radios["outcome_code"]) == sorted(b4.OUTCOME_LABELS)
            r = await post(app, rf.action, b4.submit_fields(rf, outcome_code=W1_BY_KIND[kind]))
            redirected_to_view(r, item_id)
            assert bb.item_row(i, item_id)["state"] == "resolved"
            done = await view(app, item_id)
            line = (f"Staff reported: {b4.OUTCOME_LABELS[W1_BY_KIND[kind]]} — by {STAFF_NAME},")
            assert line in done.text and "(not verified in EHR)" in done.text, done.text
            assert done.forms == []
            assert [h["action"] for h in b4.history_rows(i, item_id)][-2:] == ["claim", "resolve"]

    run(body())


def test_b4_p_ui6_claim_and_close_is_one_entry(
        inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (W3, R-B4-3): Close on an unassigned item is one claim_and_resolve entry."""
    i, kinds = inst
    item_id = kinds["cancel_request"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            before = len(b4.history_rows(i, item_id))
            f = await close_form(app, item_id)
            redirected_to_view(await post(app, f.action,
                                          b4.submit_fields(f, outcome_code="cancelled_in_ehr")),
                               item_id)
            hist = b4.history_rows(i, item_id)
            assert len(hist) == before + 1 and hist[-1]["action"] == "claim_and_resolve"
            assert hist[-1]["assertion_kind"] == "staff"

    run(body())


# =========================================================================== P-UI7 replay

def test_b4_p_ui7a_claim_replay_is_one_write(inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (v1r2 C5, P-UI7a): the identical claim body, resubmitted after the item is
    assigned, is the service's replay: one history entry; both responses redirect alike."""
    i, kinds = inst
    item_id = kinds["no_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            before = len(b4.history_rows(i, item_id))
            f = await claim_form(app, item_id)
            fields = b4.submit_fields(f)
            redirected_to_view(await post(app, f.action, fields), item_id)
            redirected_to_view(await post(app, f.action, fields), item_id)
            hist = b4.history_rows(i, item_id)
            assert [h["action"] for h in hist[before:]] == ["claim"]

    run(body())


@pytest.mark.parametrize("variant", ["claim_and_resolve", "resolve"])
def test_b4_p_ui7bc_close_replay_keeps_its_type(
        inst: tuple[b3.Install, dict[str, Any]], variant: str) -> None:
    """A (v1r2 C5, P-UI7b/c; M72): the identical Close body, resubmitted after the item is
    resolved, replays as the same production type: one entry of that type, and the second
    response is the replay (no `resolved` message)."""
    i, kinds = inst
    item_id = kinds["reschedule_request"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            if variant == "resolve":
                cf = await claim_form(app, item_id)
                redirected_to_view(await post(app, cf.action, b4.submit_fields(cf)), item_id)
            before = len(b4.history_rows(i, item_id))
            f = await close_form(app, item_id)
            assert f.hidden("type") == variant
            fields = b4.submit_fields(f, outcome_code="rescheduled")
            redirected_to_view(await post(app, f.action, fields), item_id)
            redirected_to_view(await post(app, f.action, fields), item_id)
            hist = b4.history_rows(i, item_id)
            assert [h["action"] for h in hist[before:]] == [variant]

    run(body())


def test_b4_p_ui7d_stale_pre_change_form_with_fresh_key_conflicts(
        inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (v1r2 P-UI7d, R-B4-6 `conflict`): a Close form rendered at version v, submitted with a
    NEW action key after the item moved to v+1 (claimed), is a stale-version conflict: no
    write, the item unchanged, and the conflict message on the view."""
    i, kinds = inst
    item_id = kinds["unclear_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            stale = await close_form(app, item_id)
            cf = await claim_form(app, item_id)
            redirected_to_view(await post(app, cf.action, b4.submit_fields(cf)), item_id)
            before = (bb.item_row(i, item_id), len(b4.history_rows(i, item_id)))
            fields = b4.submit_fields(stale, client_action_id=str(uuid.uuid4()),
                                      outcome_code="reached_no_change")
            redirected_to_view(await post(app, stale.action, fields), item_id, "conflict")
            assert (bb.item_row(i, item_id), len(b4.history_rows(i, item_id))) == before
            assert b4.MESSAGES["conflict"] in (await view(app, item_id, "conflict")).text

    run(body())


def test_b4_p_ui7e_same_key_other_payload_is_refused(
        inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (v1r2 C5, P-UI7e): reusing a Close form's key with a different outcome is the
    service's ACTION_KEY_CONFLICT: `refused`, no write."""
    i, kinds = inst
    item_id = kinds["ehr_confirmation_check"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            f = await close_form(app, item_id)
            redirected_to_view(await post(app, f.action,
                                          b4.submit_fields(f, outcome_code="rescheduled")),
                               item_id)
            n = len(b4.history_rows(i, item_id))
            r = await post(app, f.action, b4.submit_fields(f, outcome_code="other",
                                                           note="changed"))
            redirected_to_view(r, item_id, "refused")
            assert len(b4.history_rows(i, item_id)) == n

    run(body())


# =========================================================================== P-RES, P-CON

def test_b4_p_res1_note_is_recorded_but_never_shown(
        inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B4-3/4, OD-D2 hold): a note on a non-Other outcome is stored; the history shows
    'note recorded' and the page never contains the note text."""
    i, kinds = inst
    item_id = kinds["no_reply"]["item_id"]
    secret = "note-text-b4-res1-must-not-render"

    async def body() -> None:
        async with bb.running(i) as (_, app):
            f = await close_form(app, item_id)
            redirected_to_view(await post(app, f.action, b4.submit_fields(
                f, outcome_code="no_contact_no_answer", note=secret)), item_id)
            v = await view(app, item_id)
            assert "note recorded" in v.text and secret not in v.raw
            assert b4.history_rows(i, item_id)[-1]["note_id"] is not None

    run(body())


def test_b4_p_res2_other_with_note_resolves(inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (W1): Other with a non-empty note resolves the item."""
    i, kinds = inst
    item_id = kinds["unclear_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            f = await close_form(app, item_id)
            redirected_to_view(await post(app, f.action, b4.submit_fields(
                f, outcome_code="other", note="spoke to family")), item_id)
            assert bb.item_row(i, item_id)["outcome_code"] == "other"

    run(body())


@pytest.mark.parametrize("note", [None, "", "   "])
def test_b4_p_res3_other_without_note_refused_before_service(
        inst: tuple[b3.Install, dict[str, Any]], note: str | None) -> None:
    """A (R-B4-4, v1r2 C4 step 8; M66): Other with no or a blank note is refused by the
    production ResolveAction at construction: the `note` message, the item unchanged and
    zero M05 service calls."""
    i, kinds = inst
    item_id = kinds["cancel_request"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (lc, app):
            f = await close_form(app, item_id)
            spy = Spies(lc)
            before = bb.item_row(i, item_id)
            r = await post(app, f.action, b4.submit_fields(f, outcome_code="other", note=note))
            redirected_to_view(r, item_id, "note")
            assert bb.item_row(i, item_id) == before
            assert spy.writes == []
            assert b4.MESSAGES["note"] in (await view(app, item_id, "note")).text

    run(body())


def test_b4_p_con2_resolved_item_has_no_forms_and_refuses_close(
        inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (W4, R-B4-6 `resolved`): a resolved item shows no forms; an earlier Close form with a
    fresh key gets the `resolved` message and no write."""
    i, kinds = inst
    item_id = kinds["reschedule_request"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            f = await close_form(app, item_id)
            redirected_to_view(await post(app, f.action,
                                          b4.submit_fields(f, outcome_code="rescheduled")),
                               item_id)
            assert (await view(app, item_id)).forms == []
            n = len(b4.history_rows(i, item_id))
            r = await post(app, f.action, b4.submit_fields(
                f, client_action_id=str(uuid.uuid4()), outcome_code="rescheduled"))
            assert r.status == 303 and b4.location(r).endswith(("?m=resolved", "?m=conflict"))
            assert len(b4.history_rows(i, item_id)) == n

    run(body())


# =========================================================================== P-RSN, P-LST

def test_b4_p_rsn1_reason_shown_only_for_ehr_check(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B4-3, C5(a)): the ehr_confirmation_check view shows 'Reason: ' and one exact C5(a)
    label; no other kind shows a reason line."""
    inst, kinds = seeded

    async def body() -> None:
        async with bb.running(inst) as (_, app):
            for kind, k in kinds.items():
                v = await view(app, k["item_id"])
                shown = [lbl for lbl in b4.REASON_LABELS.values() if f"Reason: {lbl}" in v.text]
                if kind == "ehr_confirmation_check":
                    assert len(shown) == 1, v.text
                else:
                    assert "Reason:" not in v.text and shown == []

    run(body())


def test_b4_p_lst1_filter_is_closed_and_default_is_active(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B4-2): the default list shows open and in-progress items; state=resolved shows
    none of the seeded (open) items; the page carries the page-local line."""
    inst, kinds = seeded
    ids = {k["item_id"] for k in kinds.values()}

    async def body() -> None:
        async with bb.running(inst) as (_, app):
            _, dflt = await get_page(app, b4.UI)
            linked = {lnk.rsplit("/", 1)[-1] for lnk in dflt.links if lnk.startswith(b4.UI + "/")}
            assert ids <= linked
            assert b4.PAGE_LOCAL_LINE in dflt.text
            _, res = await get_page(app, b4.UI + "?state=resolved")
            assert not ids & {lnk.rsplit("/", 1)[-1] for lnk in res.links}

    run(body())


def test_b4_p_lst2_pages_walk_the_service_order_without_gaps(
        pg: s.PgCluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A (v1r1 F2.1/F2.3, P-LST2): with UI_PAGE_SIZE = 3 and ten items in mixed states, three
    sharing one created_at, following Next over state=all visits every item exactly once;
    each Next carries the last row in SERVICE order (created_at, item_id); within a page
    the presentation order is the grouping."""
    i = b3.install(pg, tmp_path)
    bb.first_start(i)
    kinds = {**bb.seed_kinds(i), **{f"b{k}": v for k, v in bb.seed_kinds(i, base=5).items()}}
    all_ids = [k["item_id"] for k in kinds.values()]
    tie = sorted(all_ids)[:3]
    i.admin(f"UPDATE {i.schema}.m05_followup_items SET created_at ="
            " '2026-11-16T12:00:00+00' WHERE item_id = ANY(%s::uuid[])", (tie,))
    ui = b4.ui()
    monkeypatch.setattr(ui, "UI_PAGE_SIZE", 3)
    service_order = [r[0] for r in i.admin(
        f"SELECT item_id::text FROM {i.schema}.m05_followup_items"
        " ORDER BY created_at, item_id")]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            for item_id in all_ids[:2]:
                cf = await claim_form(app, item_id)
                redirected_to_view(await post(app, cf.action, b4.submit_fields(cf)), item_id)
            f = await close_form(app, all_ids[2])
            redirected_to_view(await post(app, f.action, b4.submit_fields(
                f, outcome_code="rescheduled")), all_ids[2])
            seen: list[str] = []
            target: str | None = b4.UI + "?state=all"
            while target is not None:
                _, p = await get_page(app, target)
                rows = [lnk.rsplit("/", 1)[-1] for lnk in p.links
                        if lnk.startswith(b4.UI + "/")]
                assert len(rows) <= 3
                page_ids = sorted(rows, key=service_order.index)
                nxt = [lnk for lnk in p.links if "after_item_id=" in lnk]
                if nxt:
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(nxt[0]).query)
                    assert q["after_item_id"] == [page_ids[-1]], (q, page_ids)
                states = {r[0]: (r[1], r[2] is None) for r in i.admin(
                    f"SELECT item_id::text, state, assignee_id FROM {i.schema}"
                    ".m05_followup_items WHERE item_id = ANY(%s::uuid[])", (rows,))}
                rank = {("open", True): 0, ("open", False): 1, ("in_progress", False): 2,
                        ("in_progress", True): 2, ("resolved", False): 3, ("resolved", True): 3}
                assert rows == sorted(page_ids, key=lambda x: rank[states[x]]), (rows, states)
                seen.extend(rows)
                target = nxt[0] if nxt else None
            assert sorted(seen) == sorted(service_order) and len(seen) == len(set(seen))

    run(body())


# =========================================================================== P-FRM, P-SEC

async def _asgi_chunks(app: Any, target: str, headers: list[tuple[str, str]],
                       chunks: list[bytes]) -> bb.Resp:
    """The raw driver, but the body arrives in several http.request messages."""
    hdrs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": target, "raw_path": target.encode(),
             "root_path": "", "query_string": b"", "headers": hdrs,
             "client": ("127.0.0.1", 50000), "server": bb.BIND}
    queue = list(chunks)

    async def receive() -> dict[str, Any]:
        if queue:
            part = queue.pop(0)
            return {"type": "http.request", "body": part, "more_body": bool(queue)}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    msgs: list[dict[str, Any]] = []

    async def send(m: dict[str, Any]) -> None:
        msgs.append(m)

    await asyncio.wait_for(app(scope, receive, send), 60)
    start = next(m for m in msgs if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body")
    hs = [(bytes(k).decode().lower(), bytes(v).decode()) for k, v in start["headers"]]
    return bb.Resp(int(start["status"]), hs, raw, msgs)


def test_b4_p_frm1_byte_cap_before_decoding(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (v1r1 F1.2, P-FRM1; M69): 8193 bytes streamed in chunks without Content-Length, a
    declared Content-Length of 8193, and a body longer than its declared length are each
    refused 413 (HTML, labelled) with zero calls."""
    inst, kinds = seeded
    item_id = kinds["no_reply"]["item_id"]
    action = b4.ui_path("ui_resolve", item_id)
    big = b"note=" + b"a" * 8188

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            spy = Spies(lc)
            r = await _asgi_chunks(app, action, b4.form_headers(), [big[:4000], big[4000:]])
            assert r.status == 413, r.status
            b4.html_ok(r)
            r = await bb.asgi(app, "POST", action, body=b"note=a",
                              headers=b4.form_headers(extra=[("content-length", "8193")]))
            assert r.status == 413, r.status
            r = await _asgi_chunks(app, action,
                                   b4.form_headers(extra=[("content-length", "10")]),
                                   [b"note=aaaa", b"aaaaaaaa"])
            assert r.status in (400, 413), r.status
            assert spy.calls == []

    run(body())


def _valid_resolve(f: b4.Form) -> list[tuple[str, str]]:
    return b4.submit_fields(f, outcome_code="rescheduled")


@pytest.mark.parametrize("case", ["seven", "unknown", "dup_csrf", "dup_type", "dup_key",
                                  "dup_version", "dup_outcome", "dup_note", "miss_type",
                                  "miss_key", "miss_version", "miss_outcome"])
def test_b4_p_frm2_structure_refused_400_before_service(
        seeded: tuple[b3.Install, dict[str, Any]], case: str) -> None:
    """A (v1r2 C4 steps 2-3 and 6, P-FRM2; M68): with a valid csrf present, more than six
    fields, an unknown field, any duplicate and any missing non-csrf field are 400 (HTML,
    labelled), zero calls."""
    inst, kinds = seeded
    item_id = kinds["reschedule_request"]["item_id"]

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            f = await close_form(app, item_id)
            base = _valid_resolve(f)
            dup = {"dup_csrf": "csrf", "dup_type": "type", "dup_key": "client_action_id",
                   "dup_version": "expected_version", "dup_outcome": "outcome_code"}
            miss = {"miss_type": "type", "miss_key": "client_action_id",
                    "miss_version": "expected_version", "miss_outcome": "outcome_code"}
            if case == "seven":
                fields = [*base, ("note", "x"), ("extra", "y")]
            elif case == "unknown":
                fields = [*base, ("assignee_id", str(uuid.uuid4()))]
            elif case == "dup_note":
                fields = [*base, ("note", "a"), ("note", "b")]
            elif case in dup:
                fields = [*base, next(p for p in base if p[0] == dup[case])]
            else:
                fields = [p for p in base if p[0] != miss[case]]
            spy = Spies(lc)
            r = await post(app, f.action, fields)
            assert r.status == 400, (case, r.status)
            b4.html_ok(r)
            assert spy.calls == []

    run(body())


_FRM3 = {
    "lone_pct": lambda g: g + b"&note=%",
    "bad_hex": lambda g: g + b"&note=%z1",
    "short_pct": lambda g: g + b"&note=%1",
    "space": lambda g: g + b"&note=a b",
    "semicolon": lambda g: g + b"&note=a;b",
    "quote": lambda g: g + b'&note=a"b',
    "high_byte": lambda g: g + b"&note=\x80",
    "empty_pair": lambda g: g.replace(b"&", b"&&", 1),
    "leading_amp": lambda g: b"&" + g,
    "trailing_amp": lambda g: g + b"&",
    "no_equals": lambda g: g + b"&note",
    "bad_utf8": lambda g: g + b"&note=%C3%28",
}


@pytest.mark.parametrize("case", list(_FRM3))
def test_b4_p_frm3_encoding_refused_400_before_service(
        seeded: tuple[b3.Install, dict[str, Any]], case: str) -> None:
    """A (v1r2 C3, P-FRM3; M70): raw-byte, percent-triplet, structure and UTF-8 refusals are
    400 with zero calls, although every other field is valid."""
    inst, kinds = seeded
    item_id = kinds["reschedule_request"]["item_id"]

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            f = await close_form(app, item_id)
            raw = _FRM3[case](b4.form_body(_valid_resolve(f)))
            spy = Spies(lc)
            r = await bb.asgi(app, "POST", f.action, headers=b4.form_headers(), body=raw)
            assert r.status == 400, (case, r.status)
            assert spy.calls == []

    run(body())


@pytest.mark.parametrize("case", ["absent", "absent_and_type_absent", "wrong", "control"])
def test_b4_p_frm4_csrf_403_precedes_missing_fields(
        inst: tuple[b3.Install, dict[str, Any]], case: str) -> None:
    """A (v1r2 C4 steps 4-6, P-FRM4; M61, M62, M71): an absent csrf is 403 even when `type` is
    also absent; a wrong csrf is 403; both with zero calls, principal resolution included.
    The control (correct csrf) succeeds with exactly one write."""
    i, kinds = inst
    item_id = kinds["no_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (lc, app):
            f = await close_form(app, item_id)
            fields = _valid_resolve(f)
            if case == "absent":
                fields = [p for p in fields if p[0] != "csrf"]
            elif case == "absent_and_type_absent":
                fields = [p for p in fields if p[0] not in ("csrf", "type")]
            elif case == "wrong":
                fields = [(k, ("x" * len(v) if k == "csrf" else v)) for k, v in fields]
            spy = Spies(lc)
            r = await post(app, f.action, fields)
            if case == "control":
                redirected_to_view(r, item_id)
                assert spy.writes == ["act"]
            else:
                assert r.status == 403, (case, r.status)
                b4.html_ok(r)
                assert spy.calls == []

    run(body())


def test_b4_p_frm6_form_media_never_widens_beyond_the_registered_routes(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """B-or-A (v1r1 F1.1, P-FRM6; M63): the form media type on every JSON POST route, on an
    unregistered /synthetic/ui path and on case, trailing-slash and %2F variants of the
    registered routes is refused 415 by the gate, with zero calls. On B3b these JSON-route
    and variant refusals already hold; the row also requires the registered route itself to
    accept the form (A)."""
    inst, kinds = seeded
    item_id = kinds["reschedule_request"]["item_id"]
    reg = b4.ui_path("ui_claim", item_id)
    targets = [bb.path("act", item_id), bb.path("resolve", item_id), bb.path("sim_reply"),
               bb.path("sim_ehr"), "/synthetic/ui/x", reg.replace(item_id, item_id.upper()),
               reg + "/", reg.replace("/claim", "%2Fclaim")]

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            spy = Spies(lc)
            for t in targets:
                r = await bb.asgi(app, "POST", t, headers=b4.form_headers(), body=b"a=1")
                assert r.status == 415, (t, r.status)
                bb.assert_labelled(r)
            assert spy.calls == []
            f = await claim_form(app, item_id)
            assert f.action == reg

    run(body())


@pytest.mark.parametrize("endpoint,type_", [("ui_claim", "resolve"), ("ui_resolve", "claim"),
                                            ("ui_resolve", "assign")])
def test_b4_p_frm7_type_must_match_endpoint(
        seeded: tuple[b3.Install, dict[str, Any]], endpoint: str, type_: str) -> None:
    """A (v1r2 C1/C4 step 7, P-FRM7): a `type` not allowed for the endpoint is 400, zero
    calls."""
    inst, kinds = seeded
    item_id = kinds["unclear_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            f = await close_form(app, item_id)
            fields = b4.submit_fields(f, type=type_, outcome_code="rescheduled")
            if endpoint == "ui_claim":
                fields = [p for p in fields if p[0] != "outcome_code"]
            spy = Spies(lc)
            r = await post(app, b4.ui_path(endpoint, item_id), fields)
            assert r.status == 400, r.status
            assert spy.calls == []

    run(body())


@pytest.mark.parametrize("case", ["cross_origin", "no_origin", "cross_site", "host_spoof"])
def test_b4_p_sec4_ui_post_keeps_the_b3b_gate(
        seeded: tuple[b3.Install, dict[str, Any]], case: str) -> None:
    """A (R-B4-7, C-3; M64): a valid form body to a registered UI route with a cross-site
    Origin, no Origin, Sec-Fetch-Site cross-site or a spoofed Host is refused by the gate
    (403/403/403/421), labelled, zero calls."""
    inst, kinds = seeded
    item_id = kinds["unclear_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            f = await claim_form(app, item_id)
            kw: dict[str, Any] = {}
            want = 403
            if case == "cross_origin":
                kw["origin"] = "http://evil.example"
            elif case == "no_origin":
                kw["origin"] = None
            elif case == "cross_site":
                kw["extra"] = [("sec-fetch-site", "cross-site")]
            else:
                kw["host"], want = "evil.example", 421
            spy = Spies(lc)
            r = await post(app, f.action, b4.submit_fields(f), **kw)
            assert r.status == want, (case, r.status)
            bb.assert_labelled(r)
            assert spy.calls == []

    run(body())


def test_b4_p_sec5_no_mutating_get_and_no_cors(
        inst: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B4-1, C-3): GETs carrying action parameters change nothing; OPTIONS on a UI route
    is 405 without any CORS header."""
    i, kinds = inst
    item_id = kinds["no_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(i) as (_, app):
            before = (bb.item_row(i, item_id), len(b4.history_rows(i, item_id)))
            for t in (b4.ui_path("ui_view", item_id) + "?type=claim",
                      b4.ui_path("ui_claim", item_id), b4.ui_path("ui_resolve", item_id)
                      + "?type=claim_and_resolve&outcome_code=rescheduled"):
                r = await bb.asgi(app, "GET", t, headers=bb.get_headers())
                assert r.status in (200, 405), (t, r.status)
            assert (bb.item_row(i, item_id), len(b4.history_rows(i, item_id))) == before
            r = await bb.asgi(app, "OPTIONS", b4.UI, headers=bb.get_headers(
                origin="http://evil.example"))
            assert r.status == 405
            assert not [k for k, _ in r.headers if k.startswith("access-control-")]
            await get_page(app, b4.UI)

    run(body())


# =========================================================================== P-ESC, P-LOG, P-DRN

def test_b4_p_esc1_injected_labels_render_inert(
        seeded: tuple[b3.Install, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A (B-UI6, R-B4-7; M65): markup placed in a kind label, a reason label and a message
    renders as text: no element is created and the literal text is visible."""
    inst, kinds = seeded
    ui = b4.ui()
    evil = '<script>alert(1)</script><img src=x onerror="1">'
    monkeypatch.setitem(ui.KIND_LABELS, "ehr_confirmation_check", "K" + evil)
    for code in list(ui.REASON_LABELS):
        monkeypatch.setitem(ui.REASON_LABELS, code, "R" + evil)
    monkeypatch.setitem(ui.MESSAGES, "conflict", "M" + evil)
    item_id = kinds["ehr_confirmation_check"]["item_id"]

    async def body() -> None:
        async with bb.running(inst) as (_, app):
            for target in (b4.UI, b4.ui_path("ui_view", item_id) + "?m=conflict"):
                _, p = await get_page(app, target)
                assert "img" not in p.tags and p.scripts == 0
            assert all(x + evil in p.text for x in ("K", "R", "M")), p.text

    run(body())


def test_b4_p_log1_flow_logs_no_identifiers(
        inst: tuple[b3.Install, dict[str, Any]], caplog: pytest.LogCaptureFixture) -> None:
    """A (R-B4-9): a list, view, claim and close with a note log no item id, patient id,
    phone or note text; positive control: the haloflow.synthetic logger is live and its
    records reach the capture."""
    i, kinds = inst
    k = kinds["cancel_request"]
    secret = "note-b4-log1-secret"

    async def body() -> None:
        async with bb.running(i) as (_, app):
            await get_page(app, b4.UI)
            cf = await claim_form(app, k["item_id"])
            redirected_to_view(await post(app, cf.action, b4.submit_fields(cf)), k["item_id"])
            rf = await close_form(app, k["item_id"])
            redirected_to_view(await post(app, rf.action, b4.submit_fields(
                rf, outcome_code="cancelled_in_ehr", note=secret)), k["item_id"])

    caplog.set_level(logging.DEBUG)
    run(body())
    logging.getLogger("haloflow.synthetic").info("B4-LOG1-CONTROL")
    assert logging.getLogger("haloflow.synthetic").disabled is False
    assert "B4-LOG1-CONTROL" in caplog.text
    for needle in (k["item_id"], k["patient"], k["phone"], secret):
        assert needle not in caplog.text, needle


def test_b4_p_drn1_ui_refused_during_drain(
        seeded: tuple[b3.Install, dict[str, Any]]) -> None:
    """A (R-B4-10, C-6): once a stop is requested, a UI GET and a UI POST get the gate's
    labelled 503, and no M05 call is made. The UI GET answers 200 before the stop (A)."""
    inst, kinds = seeded
    item_id = kinds["no_reply"]["item_id"]

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            f = await claim_form(app, item_id)
            spy = Spies(lc)
            lc.request_stop("TEST")
            r = await bb.asgi(app, "GET", b4.UI, headers=bb.get_headers())
            assert r.status == 503
            bb.assert_labelled(r)
            r = await post(app, f.action, b4.submit_fields(f))
            assert r.status == 503
            assert spy.calls == []

    run(body())
