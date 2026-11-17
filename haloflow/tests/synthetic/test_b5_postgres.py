"""Option B B5: the simulator, scenario loading and the synthetic display over the B4 app,
PostgreSQL 17 (per-case initdb clusters; B2 installs after a B3a first start).

Class A unless stated (ob5_support docstring). Row map (design reference in each docstring):
- load: P-LD1..P-LD6; readiness: P-RDY1..P-RDY4; restart/state: P-SS1;
- Eastern placement: P-ZN0 (uninjected composed route), P-ZN1 (SQL DST vectors, class C),
  P-ZN2 (out-of-hours refusal writes nothing);
- S4: P-S4a; display: P-DS1..P-DS4, P-ESC1; seal/ACL: P-ACL1;
- adapters and paths: P-PAR1..P-PAR4, P-PATH; ticks: P-TK1..P-TK3; outbox: P-OB1;
- privacy, network, drain: P-PRV1, P-NET1, P-DRN1; end to end: P-E2E.
Rows that need a send at any hour install the five-zone TEST set through the `test_zones`
fixture (v1r4 §2); P-ZN0 and P-ZN2 never use it for the product assertion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import b4_support as b4
import ob5_support as b5
import pytest

pytestmark = pytest.mark.postgres

REPLY = "/synthetic/simulator/patient-reply"
EHR = "/synthetic/simulator/ehr"


@pytest.fixture(autouse=True)
def _haloflow_loggers_enabled() -> Iterator[None]:
    """The B3b 5a57ced fixture (log rows cannot pass vacuously)."""
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
def inst(pg: s.PgCluster, tmp_path: Path) -> Iterator[b3.Install]:
    i = b3.install(pg, tmp_path)
    bb.first_start(i)
    yield i
    for name, attr in (("hooks", "FAULT"), ("hooks", "AFAULT")):
        with contextlib.suppress(ModuleNotFoundError, AttributeError):
            setattr(b5.mod(name), attr, None)


@pytest.fixture
def test_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1r4 §2: the five-zone TEST set, so a scenario is loadable and sendable at any hour
    (monkeypatch restores it). On main the scenarios package is absent: the fixture installs
    nothing and the row fails in its own body on the missing B5 interface (class A)."""
    try:
        sc = b5.mod("scenarios")
    except ModuleNotFoundError:
        return
    monkeypatch.setattr(sc, "LOAD_ZONES", b5.TEST_ZONES)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def tenant(inst: b3.Install) -> str:
    return str(inst.state["tenant_id"])


def refs_of(inst: b3.Install, name: str) -> list[str]:
    return [r for _p, r in b5.scenario_refs(tenant(inst), name).values()]


def rows_of(name: str) -> Any:
    return b5.mod("scenarios").fixtures()[name].rows


def contacts(inst: b3.Install, phones: list[str]) -> list[tuple[str, str]]:
    rows = inst.admin(f"SELECT phone::text, patient_id::text FROM {inst.schema}"
                      ".m05_patient_contacts WHERE phone = ANY(%s) ORDER BY 1",
                      (phones,))
    return [(r[0], r[1]) for r in rows]


class Clock:
    """Captures the b5.load.clock observation (Codex v1r4 pin 2) and chains AFAULT pauses."""

    def __init__(self) -> None:
        self.seen: list[datetime] = []
        self.points: list[str] = []
        self.pause: dict[str, asyncio.Event] = {}
        self.reached: dict[str, asyncio.Event] = {}
        self.raise_at: dict[str, BaseException] = {}

    def install(self) -> None:
        hooks = b5.mod("hooks")

        def fault(point: str, info: Any) -> None:
            if point == "b5.load.clock":
                self.seen.append(datetime.fromisoformat(str(info["now"]).replace("Z", "+00:00")))

        async def afault(point: str, info: Any) -> None:
            self.points.append(point)
            if point in self.reached:
                self.reached[point].set()
            if point in self.pause:
                await self.pause[point].wait()
            if point in self.raise_at:
                raise self.raise_at[point]

        hooks.FAULT = fault
        hooks.AFAULT = afault

    def hold(self, point: str) -> tuple[asyncio.Event, asyncio.Event]:
        self.pause[point] = asyncio.Event()
        self.reached[point] = asyncio.Event()
        return self.reached[point], self.pause[point]


async def reply(app: Any, token: str, phone: str, text: str) -> bb.Resp:
    return await bb.asgi(app, "POST", REPLY, headers=bb.post_headers(token),
                         body=bb.jbody({"phone": phone, "text": text}))


async def reached_or_done(reached: asyncio.Event, task: asyncio.Task[Any],
                          timeout: float = 30) -> None:
    """Wait until the paused load reaches its hook; if the task finishes first (for example
    the B5 route is absent), fail with that response instead of a bare timeout."""
    waiter = asyncio.ensure_future(reached.wait())
    done, _ = await asyncio.wait({waiter, task}, timeout=timeout,
                                 return_when=asyncio.FIRST_COMPLETED)
    if waiter not in done:
        waiter.cancel()
        if task in done:
            r = task.result()
            raise AssertionError(f"load finished before its hook: {getattr(r, 'status', r)}"
                                 f" {getattr(r, 'raw', b'')[:200]!r}")
        raise AssertionError("hook not reached")


async def wait_for(pred: Any, timeout: float = 30) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def items_for(inst: b3.Install, patient: str) -> list[dict[str, Any]]:
    import sa_support as sa

    return list(sa.items_for(b3.as_si_install(inst), patient))


def loaded_ok(r: bb.Resp, name: str) -> None:
    assert r.status == 200, (r.status, r.raw[:300])
    bb.assert_labelled(r)
    assert r.json == {"simulated": True, "scenario": name, "outcome": "loaded"}, r.json


def refused(r: bb.Resp, status: int, code: str) -> None:
    assert (r.status, (r.json or {}).get("code")) == (status, code), (r.status, r.raw[:300])
    bb.assert_labelled(r)


def assert_appointments(inst: b3.Install, name: str, now: datetime, zones: tuple[str, ...]
                        ) -> dict[str, dict[str, Any]]:
    """The scenario's rows exist exactly as the oracle places them for the observed `now`."""
    zone = next(z for z in zones
                if b5.LOAD_START <= now.astimezone(ZoneInfo(z)).time() < b5.LOAD_END)
    refs = b5.scenario_refs(tenant(inst), name)
    got = b5.appointments(inst, [r for _p, r in refs.values()])
    assert set(got) == {r for _p, r in refs.values()}, (name, sorted(got))
    for row in rows_of(name):
        patient, ref = refs[row.key]
        a = got[ref]
        assert a["start_at"] == b5.expect_start(now, zone, row.lead_days), (row.key, a)
        assert (a["status"], a["patient_id"], a["department_tz"], a["first_name"],
                a["provider_ref"]) == ("booked", patient, zone, row.first_name,
                                       row.provider_ref), (row.key, a)
    return got


# =========================================================================== P-ZN1 (C)

def test_b5_p_zn1_sql_decision_dst_and_boundary_vectors(inst: b3.Install) -> None:
    """C (v1r4 §2 P-ZN1'; Codex v1r4 Q2): the REAL t062 `m05_reminder_decision`, called with an
    explicit `now`, on a standard day and on both DST transition days of 2026 and 2027: every
    start the Eastern oracle produces at 11:00 and at 15:59:59.999999 is `send`; the S4 +10
    day start is `not_due`; and a 19:00 Eastern tick gives `outside_window`. Independent of
    the live hour; passes on B4 (it proves the oracle against the installed SQL)."""
    from datetime import date

    def decide(now: datetime, start: datetime) -> str:
        cmd = {"schema_version": 1, "now": now.astimezone(UTC).strftime(
                   "%Y-%m-%dT%H:%M:%S.%fZ"),
               "start_at": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "zone": b5.LOAD_ZONE, "lead_days": 2, "send_time": "10:00",
               "window_start": "09:00", "window_end": "19:00"}
        with inst.c.connect(s.TARGET_DB) as a:
            a.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant(inst),))
            (row,) = a.execute(f"SELECT {inst.schema}.m05_reminder_decision(%s::jsonb)",
                               (json.dumps(cmd),)).fetchall()
            return str(row[0]["verdict"])

    for d in (date(2026, 11, 17), *b5.DST[2026], *b5.DST[2027]):
        for t in (b5.eastern(d.year, d.month, d.day, 11),
                  b5.eastern(d.year, d.month, d.day, 15, 59, 59, 999999)):
            start = b5.expect_eastern(t)
            assert start is not None
            assert decide(t, start) == "send", (t, start)
            assert decide(t, b5.expect_start(t, b5.LOAD_ZONE, 10)) == "not_due", t
            late = b5.eastern(d.year, d.month, d.day, 19)
            assert decide(late, start) == "outside_window", late


# =========================================================================== load

def test_b5_p_ld1_atomic_load_and_its_rows(inst: b3.Install, test_zones: None,
                                           caplog: pytest.LogCaptureFixture) -> None:
    """A (v1r1 B1, v1r2 §3, P-LD1): load S2 and S1. Every row is present exactly as placed for
    the observed load clock; contacts bind each fixture phone to its patient; the S1 EHR
    entry holds the database start before the response; S2 puts nothing in the EHR; one audit
    line per load."""
    caplog.set_level(logging.INFO, logger="haloflow.synthetic")
    clock = Clock()

    async def body() -> None:
        clock.install()
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            loaded_ok(await b5.load_json(app, token, "S2"), "S2")
            ehr_after_s2 = dict(lc.port.ehr.snapshot().appointments)
            loaded_ok(await b5.load_json(app, token, "S1"), "S1")
            assert ehr_after_s2 == {}, "S2 is not EHR-backed"
            got = assert_appointments(inst, "S1", clock.seen[1], b5.TEST_ZONES)
            ehr = lc.port.ehr.snapshot().appointments
            (ref,) = got
            assert set(ehr) == {ref}
            row = rows_of("S1")[0]
            e = ehr[ref]
            assert (e.status, e.start_at, e.provider_ref, e.department_ref, e.replaced_by) == (
                "neutral", got[ref]["start_at"], row.provider_ref, row.department_ref, None)
            assert lc.sender.calls == []

    run(body())
    assert len(clock.seen) == 2
    assert_appointments(inst, "S2", clock.seen[0], b5.TEST_ZONES)
    for name in ("S1", "S2"):
        refs = b5.scenario_refs(tenant(inst), name)
        want = sorted((r.phone, refs[r.key][0]) for r in rows_of(name))
        assert contacts(inst, [r.phone for r in rows_of(name)]) == want
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(
        "SIMULATOR action=load_scenario")]
    assert lines == ["SIMULATOR action=load_scenario status=200 code=- scenario=S2",
                     "SIMULATOR action=load_scenario status=200 code=- scenario=S1"], lines
    assert clock.points == [p for _ in range(2) for p in b5.HOOKS[1:]], clock.points


def test_b5_p_ld2_concurrent_same_scenario(inst: b3.Install, test_zones: None) -> None:
    """A (v1r2 §5 P-LD2; Codex v1r1/v1r2 pins): load A pauses INSIDE its transaction after the
    claim (advisory lock held); load B starts and is observed waiting on the advisory lock;
    A is released and commits; B then sees A's committed rows and refuses 409
    SCENARIO_PRESENT. A's EHR entries are unchanged by B (compared by value)."""
    clock = Clock()

    async def body() -> None:
        clock.install()
        reached, go = clock.hold("b5.load.after_claim")
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            a = asyncio.create_task(b5.load_json(app, token, "S1"))
            await reached_or_done(reached, a)
            clock.pause.pop("b5.load.after_claim")          # B must not pause here
            b = asyncio.create_task(b5.load_json(app, token, "S1"))
            await wait_for(lambda: len(s.waiting_advisory(inst.c)) == 1)
            go.set()
            ra = await a
            loaded_ok(ra, "S1")
            ehr_a = dict(lc.port.ehr.snapshot().appointments)
            rb = await b
            refused(rb, 409, "SCENARIO_PRESENT")
            assert dict(lc.port.ehr.snapshot().appointments) == ehr_a
            assert len(b5.appointments(inst, refs_of(inst, "S1"))) == 1

    run(body())


def test_b5_p_ld3_concurrent_different_scenarios(inst: b3.Install, test_zones: None) -> None:
    """A (v1r1 B1, P-LD3): S1..S4 loaded concurrently all succeed; every scenario is present
    and the EHR holds exactly the S1/S3 refs."""
    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            rs = await asyncio.gather(*(b5.load_json(app, token, n) for n in b5.NAMES))
            for n, r in zip(b5.NAMES, rs, strict=True):
                loaded_ok(r, n)
            backed = b5.ehr_backed(tenant(inst), "S1") | b5.ehr_backed(tenant(inst), "S3")
            assert set(lc.port.ehr.snapshot().appointments) == backed

    run(body())
    for n in b5.NAMES:
        assert len(b5.appointments(inst, refs_of(inst, n))) == len(rows_of(n)), n


class RefuseKth:
    """v1r2 §5 P-LD4: wraps EventFoundation.run_in_transaction so that the k-th directory
    statement inside it RETURNS a non-applied result (after really executing) or raises."""

    def __init__(self, k: int, mode: str) -> None:
        self.k, self.mode, self.n = k, mode, 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from haloflow.m02.observation import EventFoundation
        from haloflow.m05.intake_statements import DIRECTORY_KEY

        real = EventFoundation.run_in_transaction
        outer = self

        class Handle:
            def __init__(self, h: Any) -> None:
                self._h = h

            def __getattr__(self, name: str) -> Any:
                return getattr(self._h, name)

            async def fetch_all(self, key: str, params: Any, *a: Any, **k: Any) -> Any:
                rows = await self._h.fetch_all(key, params, *a, **k)
                if key == DIRECTORY_KEY:
                    outer.n += 1
                    if outer.n == outer.k:
                        if outer.mode == "raise":
                            raise RuntimeError("injected directory failure")
                        return [({"schema_version": 1, "outcome": "refused_by_test"},)]
                return rows

        class Binding:
            def __init__(self, b: Any) -> None:
                self._b = b
                self.handle = Handle(b.handle)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._b, name)

        async def wrapped(fself: Any, context: Any, body: Any, *a: Any, **k: Any) -> Any:
            async def body2(binding: Any) -> Any:
                return await body(Binding(binding))

            return await real(fself, context, body2, *a, **k)

        monkeypatch.setattr(EventFoundation, "run_in_transaction", wrapped)


@pytest.mark.parametrize("k", [1, 4, 8])
def test_b5_p_ld4_returned_refusal_rolls_back(inst: b3.Install, test_zones: None, k: int,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A (v1r2 §3 step 3, P-LD4; M-B5-11): the k-th directory result of an S2 load (k=1 the
    first contact_put, k=4 the last contact_put, k=8 the last appointment_put) is a returned
    non-applied outcome: 500 SCENARIO_LOAD_FAILED, zero scenario rows, zero contacts; a retry
    without the fault loads."""
    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            fault = RefuseKth(k, "return")
            with monkeypatch.context() as m:
                fault.install(m)
                refused(await b5.load_json(app, token, "S2"), 500, "SCENARIO_LOAD_FAILED")
            assert fault.n == k
            assert b5.appointments(inst, refs_of(inst, "S2")) == {}
            assert contacts(inst, [r.phone for r in rows_of("S2")]) == []
            loaded_ok(await b5.load_json(app, token, "S2"), "S2")

    run(body())


def test_b5_p_ld5_present_after_restart_never_redates(inst: b3.Install,
                                                      test_zones: None) -> None:
    """A (v1r1 A2/B3, P-LD5): load S1, stop, start a new generation: loading S1 again is 409
    SCENARIO_PRESENT; `start_at` is unchanged; the new generation's EHR stays empty."""
    async def first() -> None:
        async with bb.running(inst, app_name="b5-gen1") as (_lc, app):
            loaded_ok(await b5.load_json(app, await bb.session_token(app), "S1"), "S1")

    run(first())
    before = b5.appointments(inst, refs_of(inst, "S1"))

    async def second() -> None:
        async with bb.running(inst, app_name="b5-gen2") as (lc, app):
            refused(await b5.load_json(app, await bb.session_token(app), "S1"), 409,
                    "SCENARIO_PRESENT")
            assert lc.port.ehr.snapshot().appointments == {}

    run(second())
    assert b5.appointments(inst, refs_of(inst, "S1")) == before


@pytest.mark.parametrize("case,body,status,code", [
    ("unknown", {"scenario": "S9"}, 422, "UNKNOWN_SCENARIO"),
    ("lower", {"scenario": "s1"}, 422, "UNKNOWN_SCENARIO"),
    ("not_str", {"scenario": 1}, 422, "MALFORMED"),
    ("extra", {"scenario": "S1", "tenant": "x"}, 422, "MALFORMED"),
    ("missing", {}, 422, "MALFORMED"),
    ("array", ["S1"], 422, "MALFORMED"),
])
def test_b5_p_ld6_malformed_load_writes_nothing(inst: b3.Install, test_zones: None, case: str,
                                                body: Any, status: int, code: str) -> None:
    """A (v1r1 B1, P-LD6; M-B5-5): a malformed or non-allowlisted scenario is refused before
    any database call; database, simulated EHR and FakeSender unchanged; an audit line with
    scenario=-."""
    async def run_() -> None:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            before = b5.snap(inst, lc)
            r = await bb.asgi(app, "POST", b5.LOAD_ROUTE, headers=bb.post_headers(token),
                              body=bb.jbody(body))
            refused(r, status, code)
            b5.unchanged(before, b5.snap(inst, lc))

    run(run_())


@pytest.mark.parametrize("case", ["csrf", "origin", "host", "media"])
def test_b5_p_ld6g_gate_refusals_write_nothing(inst: b3.Install, test_zones: None,
                                               case: str) -> None:
    """A (C-3; v1 R-B5-2): the B3b gate refuses an unauthenticated load (wrong CSRF, cross
    origin, foreign host, form media on the JSON route) with its own code; nothing written.
    The row ends with a valid load so it cannot pass on B4 (404)."""
    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            hdr = {"csrf": bb.post_headers("x" * 43),
                   "origin": bb.post_headers(token, origin="http://evil.example"),
                   "host": bb.post_headers(token, host="evil.example:8765"),
                   "media": bb.post_headers(token, ctype=b4.FORM_CTYPE)}[case]
            want = {"csrf": (403, "CSRF_REFUSED"), "origin": (403, "ORIGIN_REFUSED"),
                    "host": (421, "HOST_REFUSED"), "media": (415, "UNSUPPORTED_MEDIA")}[case]
            before = b5.snap(inst, lc)
            r = await bb.asgi(app, "POST", b5.LOAD_ROUTE, headers=hdr,
                              body=bb.jbody({"scenario": "S1"}))
            refused(r, *want)
            b5.unchanged(before, b5.snap(inst, lc))
            loaded_ok(await b5.load_json(app, token, "S1"), "S1")

    run(body())


# =========================================================================== Eastern

def test_b5_p_zn0_uninjected_route_follows_the_owner_rule(inst: b3.Install) -> None:
    """A (v1r4 §2 P-ZN0; Codex v1r4 Q1 and pin 2): the composed app WITHOUT the test zone set.
    The expectation is computed by the independent oracle from the load clock the loader
    actually observed (b5.load.clock), bracketed by database clock reads before and after.
    Loaded: S1's start is the Eastern oracle start, department_tz America/New_York, and the
    real t062 decision at the observed instant is `send`. Refused: 409 OUTSIDE_LOAD_HOURS,
    nothing written, and the page (form adapter) shows message 6. Never skips."""
    clock = Clock()
    out: dict[str, Any] = {}

    async def body() -> None:
        clock.install()
        assert b5.mod("scenarios").LOAD_ZONES == (b5.LOAD_ZONE,)
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            before = b5.snap(inst, lc)
            lo = b5.db_now(inst)
            r = await b5.load_json(app, token, "S1")
            hi = b5.db_now(inst)
            (seen,) = clock.seen
            assert lo <= seen <= hi, (lo, seen, hi)
            want = b5.expect_eastern(seen)
            out["want"], out["seen"] = want, seen
            if want is None:
                refused(r, 409, "OUTSIDE_LOAD_HOURS")
                b5.unchanged(before, b5.snap(inst, lc))
                csrf = await b5.page_csrf(app)
                fr = await b5.post_form(app, "load", [("csrf", csrf), ("scenario", "S1")])
                b5.page_redirect(fr, "hours")
                _, pg = await b5.sim_page(app, "hours")
                assert b5.MESSAGES["hours"] in pg.text
                b5.unchanged(before, b5.snap(inst, lc))
            else:
                loaded_ok(r, "S1")

    run(body())
    if out["want"] is not None:
        (ref,) = refs_of(inst, "S1")
        a = b5.appointments(inst, [ref])[ref]
        assert (a["start_at"], a["department_tz"]) == (out["want"], b5.LOAD_ZONE)
        assert b5.a3_verdict(out["seen"], a["start_at"], b5.LOAD_ZONE) == "send"


def test_b5_p_zn2_out_of_hours_refusal_writes_nothing(inst: b3.Install,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A (v1r4 §1 step 3; Codex v1r4 pin 3): with LOAD_ZONES set to a zone that is outside its
    load hours right now (chosen from the TEST set by the oracle), every scenario is refused
    409 OUTSIDE_LOAD_HOURS by both adapters; database, simulated EHR and FakeSender unchanged;
    no directory call."""
    now = datetime.now(UTC)
    closed = next(z for z in b5.TEST_ZONES + ("Asia/Tokyo",)
                  if not (b5.LOAD_START <= (now + timedelta(minutes=5)).astimezone(
                      ZoneInfo(z)).time() < b5.LOAD_END)
                  and not (b5.LOAD_START <= now.astimezone(ZoneInfo(z)).time() < b5.LOAD_END))
    monkeypatch.setattr(b5.mod("scenarios"), "LOAD_ZONES", (closed,))
    fault = RefuseKth(10**6, "return")
    fault.install(monkeypatch)

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            before = b5.snap(inst, lc)
            for n in b5.NAMES:
                refused(await b5.load_json(app, token, n), 409, "OUTSIDE_LOAD_HOURS")
            csrf = await b5.page_csrf(app)
            b5.page_redirect(await b5.post_form(app, "load", [("csrf", csrf),
                                                              ("scenario", "S2")]), "hours")
            b5.unchanged(before, b5.snap(inst, lc))

    run(body())
    assert fault.n == 0, "directory called"


# =========================================================================== readiness

def _intervals(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Make the background loops tick every `seconds` (test-only) so they run during a pause.
    The accepted schedulers refuse an interval below 60/30 s (INTERVAL_MIN_S), so the B3a
    loop sleep (`_Managed._b3_sleep`, bound at build time) is replaced on the class before
    the lifecycle starts; the schedulers' own intervals are unchanged."""
    from haloflow.synthetic import wiring

    async def fast(self: Any, _seconds: float) -> None:
        await asyncio.sleep(seconds)

    monkeypatch.setattr(wiring._Managed, "_b3_sleep", fast)


def _baselines_match(inst: b3.Install, ref: str) -> None:
    """Codex v1r3 qualification: the send's baseline was captured successfully and holds the
    known database values (start, confirmation, provider, department)."""
    (b,) = b5.baselines(inst, [ref])
    a = b5.appointments(inst, [ref])[ref]
    row = inst.admin(f"SELECT confirmation, provider_ref, department_ref FROM {inst.schema}"
                     ".m05_appointment_profile WHERE appointment_ref = %s::uuid", (ref,))[0]
    got_start = datetime.fromisoformat(str(b["start_at"]).replace("Z", "+00:00"))
    assert got_start == a["start_at"], (b, a)
    assert (b.get("confirmation"), b.get("provider_ref"), b.get("department_ref")) == row, b


@pytest.mark.parametrize("point", ["b5.load.after_commit", "b5.load.before_commit"])
def test_b5_p_rdy1_2_ticks_during_a_paused_load(inst: b3.Install, test_zones: None,
                                                point: str,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A (v1r2 §5 P-RDY1/P-RDY2, v1r3 §4; M-B5-10): S1 and S3 load paused at `point` while a
    manual reminder tick runs and the background loops tick (intervals lowered).
    before_commit: zero sends while paused. after_commit: any send has a baseline captured
    from the database. After release: every S1/S3 send has its baseline; reply "1" to S1 and
    write-back ticks: R1 finds the EHR entry (one EHR write, no EhrUnavailable, no
    no_baseline or ehr_unreachable reason)."""
    _intervals(monkeypatch, 0.2)
    clock = Clock()

    async def body() -> None:
        clock.install()
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            loaded_ok(await b5.load_json(app, token, "S3"), "S3")
            reached, go = clock.hold(point)
            task = asyncio.create_task(b5.load_json(app, token, "S1"))
            await reached_or_done(reached, task)
            sent_before = len(lc.sender.calls)
            await lc.schedulers["reminder"].tick()
            await asyncio.sleep(1.0)                       # background loops tick meanwhile
            s1 = refs_of(inst, "S1")
            if point == "b5.load.before_commit":
                assert [c for c in lc.sender.calls[sent_before:]
                        if c[1] == rows_of("S1")[0].phone] == []
            go.set()
            loaded_ok(await task, "S1")
            await lc.schedulers["reminder"].tick()
            phone = rows_of("S1")[0].phone
            assert [c[1] for c in lc.sender.calls].count(phone) == 1
            _baselines_match(inst, s1[0])
            for ref in refs_of(inst, "S3"):
                _baselines_match(inst, ref)
            assert (await reply(app, token, phone, "1")).status == 202
            for _ in range(3):
                await lc.schedulers["writeback"].tick()
            assert [w[0] for w in lc.port.ehr.snapshot().writes] == [s1[0]]
            reasons = {w.get("reason") for w in b5.wb_ops(inst, s1)}
            assert reasons <= {None}, reasons

    run(body())


@pytest.mark.parametrize("point", ["b5.load.after_claim", "b5.load.after_ehr",
                                   "b5.load.before_commit", "put:2", "put:5"])
@pytest.mark.parametrize("how", ["fault", "cancel"])
def test_b5_p_rdy3_failure_or_cancel_leaves_nothing(inst: b3.Install, test_zones: None,
                                                    point: str, how: str,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A (v1r2 §5 P-RDY3a/b, v1r3 §4): a fault at each load hook or at the k-th directory put,
    or a cancellation of the handler task there: zero S1 rows, zero contacts, zero sends after
    a reminder tick; the page shows S1 not loaded and no orphan EHR entry. A retry loads and
    the send has its baseline."""
    clock = Clock()

    async def body() -> None:
        clock.install()
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            sa = b5.mod("sim_actions")
            with monkeypatch.context() as m:
                if point.startswith("put:"):
                    k = int(point.split(":")[1])
                    if how == "fault":
                        RefuseKth(k, "raise").install(m)
                        result = await sa.load(lc, "S1")
                        assert (result.status, result.code) == (500, "SCENARIO_LOAD_FAILED")
                    else:
                        gate = asyncio.Event()
                        cut = RefuseKth(10**6, "return")
                        cut.install(m)
                        task = asyncio.create_task(sa.load(lc, "S1"))
                        await wait_for(lambda: cut.n >= k - 1)
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                        gate.set()
                elif how == "fault":
                    clock.raise_at[point] = RuntimeError("injected")
                    result = await sa.load(lc, "S1")
                    assert (result.status, result.code) == (500, "SCENARIO_LOAD_FAILED")
                    clock.raise_at.clear()
                else:
                    reached, _go = clock.hold(point)
                    task = asyncio.create_task(sa.load(lc, "S1"))
                    await reached_or_done(reached, task)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    clock.pause.clear()
            assert b5.appointments(inst, refs_of(inst, "S1")) == {}
            assert contacts(inst, [rows_of("S1")[0].phone]) == []
            await lc.schedulers["reminder"].tick()
            assert lc.sender.calls == []
            _, pg = await b5.sim_page(app)
            (ref,) = refs_of(inst, "S1")
            assert ref not in pg.raw, "orphan EHR entry or S1 shown as present"
            loaded_ok(await b5.load_json(app, token, "S1"), "S1")
            await lc.schedulers["reminder"].tick()
            assert len(lc.sender.calls) == 1
            _baselines_match(inst, ref)

    run(body())


def test_b5_p_rdy3c_commit_outcome_unknown(inst: b3.Install, test_zones: None) -> None:
    """A (v1r2 §3 outcomes, §5 P-RDY3c): a fault raised at b5.load.after_commit (the commit
    was acknowledged, the handler cannot report it) gives 500 SCENARIO_LOAD_UNKNOWN; a retry
    gives 409 SCENARIO_PRESENT; the rows and the EHR entry are present; the page shows S1
    Loaded; the reminder send has its baseline."""
    clock = Clock()

    async def body() -> None:
        clock.install()
        clock.raise_at["b5.load.after_commit"] = RuntimeError("after ack")
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            refused(await b5.load_json(app, token, "S1"), 500, "SCENARIO_LOAD_UNKNOWN")
            clock.raise_at.clear()
            refused(await b5.load_json(app, token, "S1"), 409, "SCENARIO_PRESENT")
            (ref,) = refs_of(inst, "S1")
            assert set(lc.port.ehr.snapshot().appointments) == {ref}
            _, pg = await b5.sim_page(app)
            assert b5.LOADED_LABEL in pg.text
            await lc.schedulers["reminder"].tick()
            assert len(lc.sender.calls) == 1
            _baselines_match(inst, ref)

    run(body())


def test_b5_p_rdy4_crash_after_commit_then_restart(inst: b3.Install, test_zones: None,
                                                   tmp_path: Path) -> None:
    """A (v1r3 §3 P-RDY4, with the Codex v1r3 qualification): a child loads S1 and is killed at
    b5.load.after_commit (os._exit). After a restart on the same database (EHR empty):
    1. load S1 is 409 SCENARIO_PRESENT and the page shows the S1 restart notice;
    2. a reminder tick SENDS once and the baseline is captured successfully with the known
       database values (a fixture assertion, not an S-I guarantee);
    3. reply "1", then write-back ticks: R1 cannot read the EHR, the op defers
       (r1_failures 1, next_at in the future); aging next_at three more times gives a check
       item with reason ehr_unreachable, shown SIMULATED on the item view;
    4. no EHR write, no spent permit, one FakeSender call throughout."""
    import sa_support as sa
    import si_support as si

    bar = tmp_path / "bar"
    child = spawn_b5(inst, bar, crash="b5.load.after_commit", scenario="S1")
    rc, out = child.wait(180)
    assert rc == 97, (rc, out[-500:])
    b3.assert_gone(inst, child)
    (ref,) = refs_of(inst, "S1")
    assert set(b5.appointments(inst, [ref])) == {ref}
    phone = rows_of("S1")[0].phone
    si_inst = b3.as_si_install(inst)

    async def body() -> None:
        async with bb.running(inst, app_name="b5-after-crash") as (lc, app):
            token = await bb.session_token(app)
            refused(await b5.load_json(app, token, "S1"), 409, "SCENARIO_PRESENT")
            _, pg = await b5.sim_page(app)
            assert b5.RESTART_TEXT.format(scenario="S1") in pg.text
            await lc.schedulers["reminder"].tick()
            assert [c[1] for c in lc.sender.calls] == [phone]
            _baselines_match(inst, ref)
            assert (await reply(app, token, phone, "1")).status == 202
            await lc.schedulers["writeback"].tick()
            await lc.schedulers["writeback"].tick()
            (w,) = b5.wb_ops(inst, [ref])
            assert w["r1_failures"] == 1 and w["next_at"] is not None, w
            for _ in range(3):
                si.age(si_inst, w["wb_op_id"], next_at=3600)
                await lc.schedulers["writeback"].tick()
            (w,) = b5.wb_ops(inst, [ref])
            assert w["reason"] == "ehr_unreachable", w
            patient = b5.appointments(inst, [ref])[ref]["patient_id"]
            items = [i for i in items_for(inst, patient)
                     if i["item_kind"] == "ehr_confirmation_check"]
            assert len(items) == 1, items
            pg2 = await view(app, str(items[0]["item_id"]))
            assert "couldn't reach EHR" in pg2.text and "SIMULATED" in pg2.text
            assert lc.port.ehr.snapshot().writes == ()
            assert len(lc.sender.calls) == 1

    run(body())
    spent = sa.admin(si_inst, sa.q(si_inst, "SELECT count(*) FROM {s}.m05_writeback_permits"
                                            " WHERE spent_at IS NOT NULL"))
    assert spent == [(0,)], spent


# =========================================================================== state

def test_b5_p_ss1_exact_present_subset_and_page_states(inst: b3.Install,
                                                       test_zones: None) -> None:
    """A (v1r3 §2 P-SS1; M-B5-12): S1 loaded through the app, S2 PARTIAL (one row put
    test-side through the directory), S3 absent. The real t097 returns exactly the sorted
    present subset of the given refs; the page shows S1 Loaded, S2 the partial notice, S3 the
    Load button; more than 64 refs or a NULL array is COMMAND_MALFORMED."""
    import sh2_support as sh2
    import si_support as si

    t = tenant(inst)

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            loaded_ok(await b5.load_json(app, token, "S1"), "S1")
            row = rows_of("S2")[0]
            patient, ref = b5.scenario_refs(t, "S2")[row.key]
            si_inst = b3.as_si_install(inst)
            zone, start = sh2.in_window(si_inst)
            assert si.directory3(si_inst, "contact_put", phone=row.phone,
                                 patient_id=patient)["outcome"] in ("applied", "existing")
            assert si.directory3(si_inst, "appointment_put", **si.appointment3(
                ref, patient, start, department_tz=zone))["outcome"] == "applied"
            _, pg = await b5.sim_page(app)
            text = pg.text
            assert b5.LOADED_LABEL in text and b5.PARTIAL_TEXT in text
            load_forms = [f for f in pg.forms if f.action == b5.FORM_ROUTES["load"]]
            assert {f.fields["scenario"][0] for f in load_forms} == {"S3", "S4"}

    run(body())
    every = [r for n in ("S1", "S2", "S3") for r in refs_of(inst, n)]
    want = sorted(refs_of(inst, "S1") + [b5.scenario_refs(t, "S2")[rows_of("S2")[0].key][1]])

    def present(arr: Any) -> Any:
        with inst.c.connect(s.TARGET_DB, user=inst.state["logins"]["runtime"]["role"],
                            password=inst.state["logins"]["runtime"]["password"]) as a:
            a.execute("SELECT set_config('app.tenant_id', %s, false)", (t,))
            (r,) = a.execute(f"SELECT {inst.schema}.m05_synthetic_scenario_present(%s::uuid[])",
                             (arr,)).fetchall()
            return r[0]

    assert [str(x) for x in present(every)] == want
    import psycopg

    for bad in ([b5.new_id() for _ in range(65)], None):
        with pytest.raises(psycopg.errors.RaiseException, match="COMMAND_MALFORMED"):
            present(bad)


# =========================================================================== S4, display

def test_b5_p_s4a_far_appointment_has_no_window(inst: b3.Install, test_zones: None) -> None:
    """A (v1r1 E2 P-S4a; Codex v1r4 pin 1): after loading S4 and a reminder tick, there is no
    outbound op and no window for the S4 appointment; its start is +10 local days."""
    clock = Clock()

    async def body() -> None:
        clock.install()
        async with bb.running(inst) as (lc, app):
            loaded_ok(await b5.load_json(app, await bb.session_token(app), "S4"), "S4")
            await lc.schedulers["reminder"].tick()
            assert lc.sender.calls == []

    run(body())
    refs = refs_of(inst, "S4")
    assert b5.windows(inst, refs) == []
    assert [o for o in b5.outbound_ops(inst, refs) if o.get("state") != "prepared"] == []
    assert_appointments(inst, "S4", clock.seen[0], b5.TEST_ZONES)


async def view(app: Any, item_id: str) -> b4.Page:
    r = await bb.asgi(app, "GET", b4.ui_path("ui_view", item_id), headers=bb.get_headers())
    assert r.status == 200, (r.status, r.raw[:300])
    return b4.html_ok(r)


def expected_lines(inst: b3.Install, ref: str) -> tuple[str, str]:
    """The display oracle: admin values, zoneinfo formatting, the packaged provider label."""
    a = b5.appointments(inst, [ref])[ref]
    local = a["start_at"].astimezone(ZoneInfo(a["department_tz"]))
    label = b5.mod("scenarios").PROVIDERS.get(a["provider_ref"])
    when = f"Appointment: {local:%Y-%m-%d %H:%M} {a['department_tz']}"
    return f"Patient: {a['first_name']}", when + (f" — {label}" if label else "")


def test_b5_p_ds1_every_correlated_kind_shows_name_and_appointment(
        inst: b3.Install, test_zones: None) -> None:
    """A (T-B5-1; v1r1 C2): items of all five kinds produced through the app from S1/S2/S3
    (no_reply by test-side window aging) show exactly the oracle's Patient and Appointment
    lines; STOP produces no item."""
    import sa_support as sa

    t = tenant(inst)
    si_inst = b3.as_si_install(inst)

    async def body() -> dict[str, str]:
        found: dict[str, str] = {}
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            for n in ("S1", "S2", "S3"):
                loaded_ok(await b5.load_json(app, token, n), n)
            await lc.schedulers["reminder"].tick()
            s2 = {r.key: r for r in rows_of("S2")}
            for key, text in (("reschedule", "2"), ("cancel", "3"),
                              ("unclear", "maybe later"), ("stop", "STOP")):
                assert (await reply(app, token, s2[key].phone, text)).status == 202
            await lc.schedulers["due"].tick()
            s3 = rows_of("S3")[0]
            (ref3,) = refs_of(inst, "S3")
            assert (await reply(app, token, s3.phone, "1")).status == 202
            e = lc.port.ehr.snapshot().appointments[ref3]
            r = await bb.asgi(app, "POST", EHR, headers=bb.post_headers(token),
                              body=bb.jbody({"operation": "put_appointment",
                                             "appointment_id": ref3, "status": "neutral",
                                             "start_at": e.start_at.isoformat(),
                                             "provider_ref": "prov-changed",
                                             "department_ref": e.department_ref}))
            assert r.status == 200
            for _ in range(3):
                await lc.schedulers["writeback"].tick()
            (ref1,) = refs_of(inst, "S1")
            (win,) = b5.windows(inst, [ref1])
            sa.shift_window(si_inst, str(win["reminder_id"]), -25)
            await lc.schedulers["due"].tick()
            refs = {n: b5.scenario_refs(t, n) for n in ("S1", "S2", "S3")}
            for n, by_key in refs.items():
                for _key, (patient, ref) in by_key.items():
                    for i in items_for(inst, patient):
                        found[i["item_kind"]] = str(i["item_id"])
                        found[i["item_kind"] + ":ref"] = ref
                        found[i["item_kind"] + ":scenario"] = n
            stop_patient = refs["S2"]["stop"][0]
            assert items_for(inst, stop_patient) == []
            for kind in bb.ITEM_KINDS:
                pg = await view(app, found[kind])
                name_line, when_line = expected_lines(inst, found[kind + ":ref"])
                assert name_line in pg.text and when_line in pg.text, (kind, pg.text[:600])
                assert b5.NO_APPOINTMENT not in pg.text, kind
        return found

    found = run(body())
    assert set(bb.ITEM_KINDS) <= set(found), sorted(found)


def test_b5_p_ds2_uncorrelated_reply_shows_no_appointment(inst: b3.Install,
                                                          test_zones: None) -> None:
    """A (T-B5-2; v1r1 E2 P-DS2): a reply from the S4 phone (the patient has an appointment,
    but no window) is an unclear_reply with a NULL appointment_ref; its view shows exactly
    "No appointment linked" and no Patient line (never by patient_id)."""
    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            loaded_ok(await b5.load_json(app, token, "S4"), "S4")
            row = rows_of("S4")[0]
            assert (await reply(app, token, row.phone, "1")).status == 202
            (inbound,) = bb.inbound_rows(inst, row.phone)
            assert inbound["appointment_ref"] is None, inbound
            patient = b5.scenario_refs(tenant(inst), "S4")["far"][0]
            (item,) = items_for(inst, patient)
            assert item["item_kind"] == "unclear_reply"
            pg = await view(app, str(item["item_id"]))
            assert b5.NO_APPOINTMENT in pg.text and "Patient:" not in pg.text, pg.text[:600]

    run(body())


def test_b5_p_ds3_bui_rows_with_the_display(inst: b3.Install) -> None:
    """A (T-B5-3): the B4 seed (five kinds, test-side harness; profiles with first names)
    renders the display lines for every kind, and the B4 forms are still rendered and work
    (claim one item)."""
    kinds = bb.seed_kinds(inst, base=20)

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            for kind, k in kinds.items():
                pg = await view(app, k["item_id"])
                name_line, when_line = expected_lines(inst, k["ref"])
                assert name_line in pg.text and when_line in pg.text, (kind, pg.text[:600])
                assert any(f.action == b4.ui_path("ui_claim", k["item_id"]) for f in pg.forms)
            iid = kinds["cancel_request"]["item_id"]
            f = b4.form_of(await view(app, iid), b4.ui_path("ui_claim", iid))
            r = await bb.asgi(app, "POST", f.action, headers=b4.form_headers(),
                              body=b4.form_body(b4.submit_fields(f)))
            assert r.status == 303, r.status

    run(body())


def test_b5_p_esc1_injection_in_every_display_slot_is_inert(inst: b3.Install) -> None:
    """A (B-UI6 extended, v1r1 C4): an appointment whose first name is injection-shaped,
    correlated to an unclear reply (test-side), renders escaped: no element is created from
    it, the text appears verbatim, and the page passes the HTML oracle."""
    import sa_support as sa
    import sh2_support as sh2
    import si_support as si
    import sj_harness as sj
    import test_sj_postgres as tsj

    name = "<script>x</script><b>&amp;\"'"
    si_inst = b3.as_si_install(inst)
    phone = bb.syn_phone(40)
    out: dict[str, str] = {}

    async def seed() -> None:
        ehr, _ = await tsj._flow_ehr(si_inst)
        stack = await sj.build(si_inst, ehr)
        try:
            zone, start = sh2.in_window(si_inst)
            patient, ref = sa.new_id(), sa.new_id()
            await sh2.gateway_directory(stack.rt, si_inst, "contact_put", phone=phone,
                                        patient_id=patient)
            res = await sh2.gateway_directory(stack.rt, si_inst, "appointment_put",
                                              **si.appointment3(ref, patient, start,
                                                                department_tz=zone,
                                                                first_name=name))
            assert res["outcome"] == "applied", res
            await stack.tick_reminder()
            status, o = await sj.reply(stack, {"phone": phone}, "maybe later",
                                       message=sa.new_id())
            assert status == 202, o
            await stack.tick_all()
            (item,) = [i for i in sa.items_for(si_inst, patient)]
            out["item"], out["ref"] = str(item["item_id"]), ref
        finally:
            assert await stack.shutdown() == {}

    run(seed())

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            pg = await view(app, out["item"])
            assert f"Patient: {name}" in pg.text, pg.text[:600]
            assert pg.scripts == 0 and "b" not in pg.tags

    run(body())


def test_b5_p_ds4_display_is_tenant_scoped_and_staff_only(inst: b3.Install) -> None:
    """A (v1r1 C1/C2, P-DS4): t095 under another tenant setting raises; an unknown item id is
    ITEM_NOT_FOUND; the ingress and timer workload capability sets do not include the
    display's READ capability (the gateway refuses the key)."""
    import psycopg

    from haloflow.synthetic import simulator, wiring

    kinds = bb.seed_kinds(inst, base=30)
    lg = inst.state["logins"]["runtime"]

    def call(tenant_id: str, item: str) -> Any:
        with inst.c.connect(s.TARGET_DB, user=lg["role"], password=lg["password"]) as a:
            a.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
            return a.execute(f"SELECT {inst.schema}.m05_synthetic_display(%s::uuid)",
                             (item,)).fetchall()[0][0]

    iid = kinds["unclear_reply"]["item_id"]
    assert call(tenant(inst), iid)["linked"] is True
    with pytest.raises(psycopg.errors.RaiseException, match="ITEM_NOT_FOUND"):
        call(tenant(inst), b5.new_id())
    with pytest.raises(psycopg.Error):
        call("clinic-other-0000", iid)
    for caps in (simulator.INGRESS_CAPS, wiring.TIMER_CAPS):
        assert "m05.followup.read" not in caps
    st = b5.mod("display_statements")
    assert st.DISPLAY_KEY == "m05.synthetic.display"


def test_b5_p_acl1_three_functions_installed_as_pinned(inst: b3.Install) -> None:
    """A (v1r1 C1/C4 drift row; v1r2 §6; v1r3 §2): t095..t097 exist with the pinned signature,
    return type and volatility; SECURITY DEFINER; owner the M05 owner role; EXECUTE for
    haloflow_runtime only (no PUBLIC); their three ledger rows are recorded."""
    rows = inst.admin(
        "SELECT p.proname, pg_get_function_identity_arguments(p.oid),"
        " pg_get_function_result(p.oid), p.provolatile, p.prosecdef,"
        " pg_get_userbyid(p.proowner), p.proacl::text[]"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND p.proname LIKE 'm05\\_synthetic\\_%%'", (inst.schema,))
    got = {r[0]: r[1:] for r in rows}
    assert set(got) == set(b5.B5_FUNCTIONS), sorted(got)
    for name, (args, result, vol) in b5.B5_FUNCTIONS.items():
        a, res, v, secdef, owner, acl = got[name]
        assert (a, res, v, secdef, owner) == (args, result, vol, True, "haloflow_m05_owner"), \
            (name, got[name])
        grantees = {re.split("=", e)[0] for e in acl}
        assert grantees == {"haloflow_runtime", "haloflow_m05_owner"}, (name, acl)
        assert all(not e.startswith("=") for e in acl), (name, acl)
    ids = {r[0] for r in inst.admin("SELECT migration_id FROM shared.schema_migrations")}
    assert set(b5.B5_UNITS) <= ids, sorted(set(b5.B5_UNITS) - ids)


# =========================================================================== adapters, paths

PARITY: list[tuple[str, str, dict[str, str]]] = [
    ("load", "unknown", {"scenario": "S9"}),
    ("load", "lower", {"scenario": "s2"}),
    ("tick", "valid", {"scheduler": "due"}),
    ("tick", "unknown", {"scheduler": "weekly"}),
    ("reply", "no_contact", {"phone": "+15555550199", "text": "1"}),
    ("reply", "bad_phone", {"phone": "+14155550123", "text": "1"}),
    ("reply", "empty_text", {"phone": "+15555550199", "text": ""}),
    ("ehr", "fail_zero", {"operation": "fail_next", "reads": "0", "writes": "0"}),
    ("ehr", "bad_op", {"operation": "drop_all"}),
    ("ehr", "bad_status", {"operation": "put_appointment", "appointment_id": "a1",
                           "status": "booked", "start_at": "2026-11-19T23:00:00+00:00"}),
]
JSON_ROUTE = {"load": b5.LOAD_ROUTE, "tick": b5.TICK_ROUTE, "reply": REPLY, "ehr": EHR}


def _json_value(action: str, fields: dict[str, str]) -> dict[str, Any]:
    if action == "ehr" and fields.get("operation") == "fail_next":
        return {**fields, "reads": int(fields["reads"]), "writes": int(fields["writes"])}
    return dict(fields)


@pytest.mark.parametrize("action,case,fields", PARITY,
                         ids=[f"{a}-{c}" for a, c, _ in PARITY])
def test_b5_p_par_json_and_form_adapters_agree(inst: b3.Install, test_zones: None, action: str,
                                               case: str, fields: dict[str, str],
                                               caplog: pytest.LogCaptureFixture) -> None:
    """A (v1r1 D3 P-PAR1..4): the same input through the JSON route and the form route gives
    the same result (JSON 422 <-> form 422 HTML error page; a load refusal code <-> its
    message key; success <-> 303 with m=loaded/tick, or no m for reply and EHR), the same
    database, simulated-EHR and FakeSender delta, and the same audit line."""
    caplog.set_level(logging.INFO, logger="haloflow.synthetic")

    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            csrf = await b5.page_csrf(app)
            s0 = b5.snap(inst, lc)
            caplog.clear()
            rj = await bb.asgi(app, "POST", JSON_ROUTE[action], headers=bb.post_headers(token),
                               body=bb.jbody(_json_value(action, fields)))
            json_lines = [r.getMessage() for r in caplog.records]
            s1 = b5.snap(inst, lc)
            caplog.clear()
            rf = await b5.post_form(app, action, [("csrf", csrf), *fields.items()])
            form_lines = [r.getMessage() for r in caplog.records]
            s2 = b5.snap(inst, lc)
            code = (rj.json or {}).get("code")
            if rj.status == 422:
                assert rf.status == 422, (rf.status, rf.raw[:300])
                b4.html_ok(rf)
            else:
                assert rf.status == 303, (rf.status, rf.raw[:300])
                loc = b4.location(rf)
                key = loc.split("?m=")[1] if "?m=" in loc else None
                want = b5.CODE_MESSAGE[code] if code in b5.CODE_MESSAGE else (
                    {"load": "loaded", "tick": "tick"}.get(action) if rj.status < 300
                    else None)
                assert key == want, (rj.status, code, loc)
            assert b3.changed(s0.tables, s1.tables) == b3.changed(s1.tables, s2.tables)
            assert (s1.sends[len(s0.sends):], s1.ehr_writes - s0.ehr_writes) == (
                s2.sends[len(s1.sends):], s2.ehr_writes - s1.ehr_writes)
            assert (s0.ehr == s1.ehr) == (s1.ehr == s2.ehr), "EHR delta differs"
            assert json_lines == form_lines, (json_lines, form_lines)

    run(body())


@pytest.mark.parametrize("method,path,ctype,want", [
    ("POST", "/synthetic/simulator/ui/LOAD", b4.FORM_CTYPE, 415),
    ("POST", "/synthetic/simulator/ui/load/", b4.FORM_CTYPE, 415),
    ("POST", "/synthetic/simulator/ui/%6Coad", b4.FORM_CTYPE, 415),
    ("POST", "/synthetic/simulator/ui/other", b4.FORM_CTYPE, 415),
    ("GET", "/synthetic/simulator/ui/load", None, 405),
    ("GET", "/synthetic/simulator/ui/tick", None, 405),
    ("POST", b5.LOAD_ROUTE, b4.FORM_CTYPE, 415),
    ("POST", "/synthetic/simulator/ui/load", "application/json", 415),
    ("POST", b5.OUTBOX_ROUTE, "application/json", 405),
])
def test_b5_p_path_noncanonical_and_wrong_media_refused(inst: b3.Install, test_zones: None,
                                                        method: str, path: str,
                                                        ctype: str | None, want: int) -> None:
    """A (v1r1 D4 P-PATH; M63 analogue): each variant is refused with its existing code and
    writes nothing; the row ends with the canonical form load being served (303), so the
    exception did not widen and the row cannot pass on B4."""
    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            csrf = await b5.page_csrf(app)
            before = b5.snap(inst, lc)
            hdr = (b4.form_headers(ctype=ctype) if ctype == b4.FORM_CTYPE else
                   bb.post_headers(token, ctype=ctype)) if method == "POST" else bb.get_headers()
            r = await bb.asgi(app, method, path, headers=hdr,
                              body=b4.form_body([("csrf", csrf), ("scenario", "S1")])
                              if method == "POST" else b"")
            assert r.status == want, (r.status, r.raw[:200])
            assert r.header(bb.LABEL_HEADER) == ["true"]
            b5.unchanged(before, b5.snap(inst, lc))
            ok = await b5.post_form(app, "load", [("csrf", csrf), ("scenario", "S1")])
            b5.page_redirect(ok, "loaded")

    run(body())


@pytest.mark.parametrize("case", ["missing", "wrong", "duplicate"])
def test_b5_p_csrf_form_token_checked_first(inst: b3.Install, test_zones: None,
                                            case: str) -> None:
    """A (C-3; v1r1 D2; M-B5-4): the form CSRF field is checked before any action: missing or
    wrong is a 403 HTML error page, a duplicate field a 400; nothing written."""
    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            csrf = await b5.page_csrf(app)
            fields = {"missing": [("scenario", "S1")],
                      "wrong": [("csrf", "x" * len(csrf)), ("scenario", "S1")],
                      "duplicate": [("csrf", csrf), ("csrf", csrf), ("scenario", "S1")]}[case]
            before = b5.snap(inst, lc)
            r = await b5.post_form(app, "load", fields)
            assert r.status == (400 if case == "duplicate" else 403), r.status
            b4.html_ok(r)
            b5.unchanged(before, b5.snap(inst, lc))

    run(body())


# =========================================================================== ticks, outbox

def test_b5_p_tk1_ticks_report_counts(inst: b3.Install) -> None:
    """A (v1 R-B5-3, P-TK1): each scheduler tick returns 200 labelled
    {"scheduler", "counts": {str: int}}; an unknown scheduler is 422 UNKNOWN_SCHEDULER."""
    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            for name in ("due", "reminder", "writeback"):
                r = await b5.tick_json(app, token, name)
                assert r.status == 200, (name, r.raw[:300])
                bb.assert_labelled(r)
                assert r.json["scheduler"] == name
                counts = r.json["counts"]
                assert type(counts) is dict and all(
                    type(k) is str and type(v) is int and v >= 0 for k, v in counts.items())
            refused(await b5.tick_json(app, token, "weekly"), 422, "UNKNOWN_SCHEDULER")

    run(body())


def test_b5_p_tk4_tick_with_nothing_due_moves_no_deadline(inst: b3.Install) -> None:
    """A (C-4, P-TK4; M-B5-6): with seeded items and nothing due, the three ticks leave
    windows, outbound ops, write-back ops and permits byte-identical."""
    bb.seed_kinds(inst, base=50)
    tables = ("m05_reminder_windows", "m05_outbound_ops", "m05_writeback_ops",
              "m05_writeback_permits")

    def digest() -> dict[str, Any]:
        d = b3.table_digests(inst)
        return {t: d["{tenant}." + t] for t in tables}

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            before = digest()
            for name in ("due", "reminder", "writeback"):
                assert (await b5.tick_json(app, token, name)).status == 200
            assert digest() == before

    run(body())


def test_b5_p_tk2_tick_during_drain_is_503(inst: b3.Install) -> None:
    """A (C-6/C-7, P-TK2): after the stop is requested, a tick and a load get 503 with the
    lifecycle's closed code."""
    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            token = await bb.session_token(app)
            b5.mod("sim_actions")
            lc.request_stop("TEST")
            for r in (await b5.tick_json(app, token, "due"),
                      await b5.load_json(app, token, "S1")):
                assert r.status == 503, (r.status, r.raw[:200])
                bb.assert_labelled(r)
            await lc.wait_stopped()

    run(body())


def test_b5_p_ob1_outbox_view(inst: b3.Install) -> None:
    """A (v1 R-B5-4, P-OB1): the outbox lists the last 50 FakeSender calls newest first,
    labelled; POST is 405; the page lists them escaped and shows the empty text when there
    are none."""
    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            _, pg = await b5.sim_page(app)
            assert b5.MESSAGES["outbox_empty"] in pg.text
            for n in range(55):
                lc.sender.calls.append((f"op-{n}", bb.syn_phone(n % 100),
                                        f"<i>text {n}</i>"))
            r = await bb.asgi(app, "GET", b5.OUTBOX_ROUTE, headers=bb.get_headers())
            assert r.status == 200
            bb.assert_labelled(r)
            got = r.json["outbox"]
            assert [o["op_id"] for o in got] == [f"op-{n}" for n in range(54, 4, -1)]
            assert set(got[0]) == {"op_id", "phone", "text"}
            token = await bb.session_token(app)
            r2 = await bb.asgi(app, "POST", b5.OUTBOX_ROUTE, headers=bb.post_headers(token),
                               body=b"{}")
            assert r2.status == 405
            _, pg2 = await b5.sim_page(app)
            assert "<i>text 54</i>" in pg2.text and "<i>" not in pg2.tags

    run(body())


# =========================================================================== privacy, network

def test_b5_p_prv1_no_phone_text_or_name_in_logs(inst: b3.Install, test_zones: None,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    """A (v1 R-B5-4 privacy, P-PRV; M-B5-7): across load, ticks, a reply, an EHR change, the
    page and the outbox, no log record carries a fixture phone, a first name or a reply text;
    a live-logger positive control proves capture works."""
    caplog.set_level(logging.DEBUG)
    logging.getLogger("haloflow.synthetic").info("CONTROL b5-live")

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            token = await bb.session_token(app)
            for n in b5.NAMES:
                loaded_ok(await b5.load_json(app, token, n), n)
            for name in ("due", "reminder", "writeback"):
                await b5.tick_json(app, token, name)
            await reply(app, token, rows_of("S2")[2].phone, "secret words here")
            await b5.sim_page(app)
            await bb.asgi(app, "GET", b5.OUTBOX_ROUTE, headers=bb.get_headers())

    run(body())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "CONTROL b5-live" in text
    sc = b5.mod("scenarios")
    for scenario in sc.fixtures().values():
        for r in scenario.rows:
            assert r.phone not in text and r.first_name not in text, r.key
    assert "secret words here" not in text
    assert re.search(r"SIMULATOR action=tick status=200 code=- scheduler=reminder", text)


def test_b5_p_net1_no_network(inst: b3.Install, test_zones: None,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A (B-N1 re-run, P-NET): every B5 route exercised with non-AF_UNIX sockets and the
    httpx/requests constructors patched to raise: zero calls."""
    import socket

    real = socket.socket.__init__
    calls: list[Any] = []

    def guarded(self: Any, family: int = socket.AF_INET, *a: Any, **k: Any) -> None:
        if family != socket.AF_UNIX:
            calls.append(family)
            raise AssertionError("network socket created")
        real(self, family, *a, **k)

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            monkeypatch.setattr(socket.socket, "__init__", guarded)
            for mod_name, attr in (("httpx", "Client"), ("httpx", "AsyncClient"),
                                   ("requests", "Session")):
                with contextlib.suppress(ImportError):
                    m = __import__(mod_name)
                    monkeypatch.setattr(m, attr, lambda *a, _n=attr, **k: calls.append(_n))
            token = await bb.session_token(app)
            csrf = await b5.page_csrf(app)
            loaded_ok(await b5.load_json(app, token, "S1"), "S1")
            await b5.tick_json(app, token, "reminder")
            await bb.asgi(app, "GET", b5.OUTBOX_ROUTE, headers=bb.get_headers())
            await b5.post_form(app, "tick", [("csrf", csrf), ("scheduler", "writeback")])
            await b5.post_form(app, "reply", [("csrf", csrf),
                                              ("phone", rows_of("S1")[0].phone),
                                              ("text", "1")])
            await b5.sim_page(app)

    run(body())
    assert calls == []


# =========================================================================== end to end

def test_b5_p_e2e_s1_through_the_page_forms_only(inst: b3.Install, test_zones: None) -> None:
    """A (v1r1 E3 P-E2E): S1 through the page forms only: load, reminder tick (no due-window
    tick), reply "1", write-back ticks. Independent verification: the simulated EHR holds the
    positive confirmation and exactly one write; the admin connection shows the op
    observed_positive with a spent permit; the outbox and FakeSender show exactly one send."""
    async def body() -> None:
        async with bb.running(inst) as (lc, app):
            csrf = await b5.page_csrf(app)
            ticked: list[str] = []
            for name in ("due", "reminder", "writeback"):
                sched = lc.schedulers[name]
                real = sched.tick

                async def spy(_r: Any = real, _n: str = name) -> Any:
                    ticked.append(_n)
                    return await _r()

                sched.tick = spy
            b5.page_redirect(await b5.post_form(app, "load", [("csrf", csrf),
                                                              ("scenario", "S1")]), "loaded")
            b5.page_redirect(await b5.post_form(app, "tick", [("csrf", csrf),
                                                              ("scheduler", "reminder")]),
                             "tick")
            row = rows_of("S1")[0]
            rr = await b5.post_form(app, "reply", [("csrf", csrf), ("phone", row.phone),
                                                   ("text", "1")])
            assert rr.status == 303, rr.status
            for _ in range(2):
                b5.page_redirect(await b5.post_form(app, "tick", [("csrf", csrf),
                                                                  ("scheduler", "writeback")]),
                                 "tick")
            assert "due" not in ticked, ticked
            (ref,) = refs_of(inst, "S1")
            snap = lc.port.ehr.snapshot()
            assert snap.appointments[ref].status == "positive"
            assert [w[0] for w in snap.writes] == [ref]
            assert [c[1] for c in lc.sender.calls] == [row.phone]
            r = await bb.asgi(app, "GET", b5.OUTBOX_ROUTE, headers=bb.get_headers())
            assert [o["phone"] for o in r.json["outbox"]] == [row.phone]

    run(body())
    (ref,) = refs_of(inst, "S1")
    (w,) = b5.wb_ops(inst, [ref])
    assert w["state"] == "observed_positive", w
    spent = inst.admin(f"SELECT count(*) FROM {inst.schema}.m05_writeback_permits"
                       " WHERE wb_op_id = %s::uuid AND spent_at IS NOT NULL", (w["wb_op_id"],))
    assert spent == [(1,)], spent


# =========================================================================== child

def spawn_b5(inst: b3.Install, bar: Path, *, crash: str, scenario: str) -> b3.B3Child:
    """Run `b5_child.py` (fresh `python -I`, explicit environment): the app with the TEST zone
    set, one load of `scenario`, os._exit(97) at the `crash` hook point."""
    bar.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"bar": str(bar), "environ": inst.env, "app": "b5-child",
                          "crash": crash, "scenario": scenario})
    env = {"PATH": os.environ.get("PATH", ""),
           "PYTHONPATH": f"{s.SRC}{os.pathsep}{b3.HERE}{os.pathsep}{b3.M05}"}
    proc = subprocess.Popen([sys.executable, "-I", str(b3.HERE / "b5_child.py"), payload],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    s.CHILDREN.append(proc)
    return b3.B3Child(proc, bar, "b5-child")
