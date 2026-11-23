"""Option B B5: unit rows (no database). Class per row docstring (ob5_support docstring).

Rows: U-C1..C3 (controls on the oracles), U-ZN0/U-ZN0s (Eastern-only product guard and source
scan), U-ZN1a (every minute of 2026-2027), U-ZN1c (half-open endpoints, subsecond, DST days),
U-ZN1d (test-zone set and the S4 premise), U-F1..F5 (fixtures), U-SS1..6 (scenario state),
U-D1..D5 (display lines), U-MSG (OD-B5-2 wording), U-PATH (form-route union), U-DOC.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import ob5_support as b5
import pytest

YEARS = (2026, 2027)


def _minutes(year: int) -> Any:
    t = datetime(year, 1, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC)
    step = timedelta(minutes=1)
    while t < end:
        yield t
        t += step


# =========================================================================== C: controls

def test_b5_u_c1_eastern_oracle_vectors() -> None:
    """C (v1r4 §1; Codex v1r4 pin 3): the independent oracle `expect_eastern` on the exact
    half-open endpoints, on a standard day and on both DST transition days of 2026 and 2027.
    Every accepted vector is a `send` under the A3 transcription."""
    for d in (date(2026, 11, 17), *b5.DST[2026], *b5.DST[2027]):
        y, m, dd = d.year, d.month, d.day
        refuse = [b5.eastern(y, m, dd, 10, 59, 59, 999999), b5.eastern(y, m, dd, 16)]
        accept = [b5.eastern(y, m, dd, 11), b5.eastern(y, m, dd, 15, 59, 59, 999999)]
        for t in refuse:
            assert b5.expect_eastern(t) is None, t
        for t in accept:
            start = b5.expect_eastern(t)
            assert start is not None, t
            local = start.astimezone(b5.EASTERN)
            assert (local.date(), local.time()) == (d + timedelta(days=2), time(18)), local
            assert b5.a3_verdict(t, start, b5.LOAD_ZONE) == "send", (t, start)
    with pytest.raises(ValueError):
        b5.expect_eastern(datetime(2026, 11, 17, 12))


def test_b5_u_c2_test_zone_set_covers_every_minute() -> None:
    """C (v1r1 E1, kept as U-ZN1b and moved to tests only by v1r4 §2): the five-zone TEST set
    has, at every UTC minute of 2026 and 2027, at least one zone whose local time is in
    [11:00, 16:00); each zone has exactly one UTC offset over both years; the start chosen in
    the first such zone is `send` under A3."""
    tzs = [ZoneInfo(z) for z in b5.TEST_ZONES]
    for z, tz in zip(b5.TEST_ZONES, tzs, strict=True):
        offsets = {datetime(y, m, 15, tzinfo=UTC).astimezone(tz).utcoffset()
                   for y in YEARS for m in range(1, 13)}
        assert len(offsets) == 1, (z, offsets)
    for year in YEARS:
        for t in _minutes(year):
            for z, tz in zip(b5.TEST_ZONES, tzs, strict=True):
                if b5.LOAD_START <= t.astimezone(tz).time() < b5.LOAD_END:
                    start = b5.expect_start(t, z, b5.SEND_LEAD)
                    assert b5.a3_verdict(t, start, z) == "send", (t, z)
                    break
            else:
                raise AssertionError(f"uncovered minute {t}")


def test_b5_u_c3_dst_elapsed_time_is_at_least_49_hours() -> None:
    """C (v1r4 §1; Codex v1r4 verification): a load at 15:59:59 Eastern on each of the two
    days before each transition gives start - now of at least 49 h (spring forward) and at
    most 51 h + 1 s (fall back); never below the 26 h A3 bound."""
    for spring, fall in (b5.DST[2026], b5.DST[2027]):
        for d in (spring - timedelta(days=2), spring - timedelta(days=1),
                  fall - timedelta(days=2), fall - timedelta(days=1)):
            now = b5.eastern(d.year, d.month, d.day, 15, 59, 59)
            start = b5.expect_eastern(now)
            assert start is not None
            hours = (start - now).total_seconds() / 3600
            assert 49 <= hours <= 51.0003, (d, hours)


def test_b5_u_c4_ehr_state_oracle_sees_every_field() -> None:
    """C (Codex 6322924 F1): negative controls for the no-write oracle on a real SimulatedEhr
    (accepted M05 class, no B5 code). A same-status change of start_at, provider_ref,
    department_ref, replaced_by or appointment identity, a write, a closed permit, and a
    pending read or write fault are each detected; an unchanged EHR (reads advance only the
    port time) compares equal."""
    import asyncio
    import dataclasses

    from haloflow.m05.simulated_ehr import EhrRead, SimulatedEhr

    start = datetime(2026, 11, 19, 23, tzinfo=UTC)
    base = EhrRead(status="neutral", appointment_id="a1", start_at=start, provider_ref="p1",
                   department_ref="d1", replaced_by=None)

    def fresh() -> Any:
        e = SimulatedEhr(clock=lambda: datetime.now(UTC))
        e.put(base)
        return e

    ref = b5.ehr_state(fresh())
    e = fresh()
    asyncio.run(e.read("a1"))
    assert b5.ehr_state(e) == ref
    changes = {
        "start_at": {"start_at": start + timedelta(minutes=1)},
        "provider_ref": {"provider_ref": "p2"},
        "department_ref": {"department_ref": "d2"},
        "replaced_by": {"replaced_by": "a9"},
    }
    for field, over in changes.items():
        e = fresh()
        e.put(dataclasses.replace(base, **over))
        assert b5.ehr_state(e) != ref, field
    e = fresh()
    e.put(dataclasses.replace(base, appointment_id="a2"))
    assert b5.ehr_state(e) != ref, "identity"
    e = fresh()
    asyncio.run(e.write_confirmation("a1", permit_id="pm1",
                                     apply_deadline=datetime.now(UTC) + timedelta(hours=1)))
    assert b5.ehr_state(e) != ref, "write"
    e = fresh()
    asyncio.run(e.close_permit("pm2"))
    assert b5.ehr_state(e) != ref, "closed permit"
    for name in ("fail_reads", "fail_writes"):
        e = fresh()
        getattr(e, name)(1)
        assert b5.ehr_state(e) != ref, name


def _side(rows: list[dict[str, Any]], anchor: datetime, *, labels: dict[str, str] | None = None,
          types: dict[str, str] | None = None) -> Any:
    """A synthetic one-table install pair (before empty) for the typed parity oracle."""
    t = "{tenant}.m05_reminder_windows"
    ty = types or {"reminder_id": "uuid", "source_event_id": "uuid", "appointment_ref": "uuid",
                   "start_at": "timestamp with time zone",
                   "deadline": "timestamp with time zone",
                   "created_at": "timestamp with time zone", "text": "text",
                   "state": "character varying"}
    before = b5.Capture({t: []}, {t: ty}, anchor)
    after = b5.Capture({t: rows}, {t: ty}, anchor)
    return b5.Side(before, after, labels or {}, "clinic-syn-x", "tenant_x")


def test_b5_u_c5_typed_parity_oracle_controls() -> None:
    """C (Codex 0052e54 R3 focused investigation; no B5 code). Accepts: random generated ids
    (consistently linked), incidental created_at, and business times shifted together with
    each install's request anchor. Rejects: Codex's reproduced counterexample (start_at,
    deadline and source_event_id all different), a changed start_at or deadline alone, a
    broken link (two rows that shared an id no longer do), a changed uuid-shaped or
    timestamp-shaped TEXT value, and an unclassified timestamp field."""
    import uuid

    t0 = datetime(2026, 11, 17, 15, tzinfo=UTC)

    def rows(anchor: datetime, *, start: str = "2026-11-19T23:00:00Z",
             deadline: timedelta = timedelta(hours=24), text: str = "2",
             linked: bool = True, src: str | None = None) -> list[dict[str, Any]]:
        ev = src or str(uuid.uuid4())
        return [{"reminder_id": str(uuid.uuid4()), "source_event_id": ev,
                 "appointment_ref": "11111111-0000-5000-8000-000000000001",
                 "start_at": start, "deadline": (anchor + deadline).isoformat(),
                 "created_at": (anchor + timedelta(seconds=uuid.uuid4().int % 50)).isoformat(),
                 "text": text, "state": "closed"},
                {"reminder_id": str(uuid.uuid4()),
                 "source_event_id": ev if linked else str(uuid.uuid4()),
                 "appointment_ref": "11111111-0000-5000-8000-000000000001",
                 "start_at": start, "deadline": (anchor + deadline).isoformat(),
                 "created_at": anchor.isoformat(), "text": "ack", "state": "open"}]

    labels = {"11111111-0000-5000-8000-000000000001": "S1.confirm.ref"}
    a_anchor, b_anchor = t0, t0 + timedelta(seconds=17)
    base = _side(rows(a_anchor), a_anchor, labels=labels)
    assert b5.parity_mismatches(base, _side(rows(b_anchor), b_anchor, labels=labels)) == []
    # Codex's counterexample, made exact: A vs B with all three values different.
    a1 = _side([{"reminder_id": str(uuid.uuid4()), "start_at": "2026-11-19T23:00:00Z",
                 "deadline": "2026-11-18T23:00:00Z",
                 "source_event_id": "11111111-1111-4111-8111-111111111111"}], t0)
    b1 = _side([{"reminder_id": str(uuid.uuid4()), "start_at": "2026-11-20T23:00:00Z",
                 "deadline": "2026-11-25T23:00:00Z",
                 "source_event_id": "22222222-2222-4222-8222-222222222222"},
                {"reminder_id": str(uuid.uuid4()), "start_at": "2026-11-20T23:00:00Z",
                 "deadline": "2026-11-25T23:00:00Z",
                 "source_event_id": "22222222-2222-4222-8222-222222222222"}][:1], t0)
    assert b5.parity_mismatches(a1, b1) != []
    bad = {
        "start_at +1h": rows(b_anchor, start="2026-11-20T00:00:00Z"),
        "start_at +1d": rows(b_anchor, start="2026-11-20T23:00:00Z"),
        "deadline +1d": rows(b_anchor, deadline=timedelta(hours=48)),
        "deadline +5min": rows(b_anchor, deadline=timedelta(hours=24, minutes=5)),
        "broken link": rows(b_anchor, linked=False),
        "uuid-shaped text": rows(b_anchor, text=str(uuid.uuid4())),
        "timestamp-shaped text": rows(b_anchor, text="2026-11-20T23:00:00Z"),
        "state": [dict(r, state="open") for r in rows(b_anchor)],
    }
    for name, rs in bad.items():
        got = b5.parity_mismatches(base, _side(rs, b_anchor, labels=labels))
        assert got != [], name
    ut = {"x": "uuid", "when": "timestamp with time zone"}
    with pytest.raises(b5.Unclassified):
        b5.parity_mismatches(_side([{"x": str(uuid.uuid4()), "when": t0.isoformat()}], t0,
                                   types=ut),
                             _side([{"x": str(uuid.uuid4()), "when": t0.isoformat()}], t0,
                                   types=ut))
    # A uuid-shaped text value is compared literally on both sides (equal text -> equal).
    same = str(uuid.uuid4())
    assert b5.parity_mismatches(_side(rows(a_anchor, text=same), a_anchor, labels=labels),
                                _side(rows(b_anchor, text=same), b_anchor, labels=labels)) == []


def test_b5_u_c6_ehr_structural_encoding() -> None:
    """C (Codex 0052e54 R3: EHR serialized structurally, never by repr): two SimulatedEhr with
    the same fixture appointment and a write under different random permit ids compare equal
    through the token maps; a start_at shifted by one hour, a different status (no write on
    either side, since a write sets "positive"), or a write to a different permit
    relationship compares unequal."""
    import asyncio
    import dataclasses
    import uuid

    from haloflow.m05.simulated_ehr import EhrRead, SimulatedEhr

    ref = "11111111-0000-5000-8000-000000000001"
    labels = {ref: "S1.confirm.ref"}
    t0 = datetime(2026, 11, 17, 15, tzinfo=UTC)
    base_read = EhrRead(status="neutral", appointment_id=ref,
                        start_at=datetime(2026, 11, 19, 23, tzinfo=UTC), provider_ref="p1",
                        department_ref="d1", replaced_by=None)

    def side(read: Any, permit: str, tables_permit: str | None = None,
             write: bool = True) -> Any:
        e = SimulatedEhr(clock=lambda: datetime.now(UTC))
        e.put(read)
        if write:
            asyncio.run(e.write_confirmation(
                ref, permit_id=permit, apply_deadline=datetime.now(UTC) + timedelta(hours=1)))
        t = "{tenant}.m05_writeback_permits"
        rows = [{"permit_id": tables_permit or permit}]
        cap = b5.Capture({t: rows}, {t: {"permit_id": "uuid"}}, t0)
        return b5.Side(cap, cap, labels, "clinic-syn-x", "tenant_x",
                       ehr=(b5.ehr_state(e), b5.ehr_state(e)))

    p1, p2 = str(uuid.uuid4()), str(uuid.uuid4())
    assert b5.parity_mismatches(side(base_read, p1), side(base_read, p2)) == []
    shifted = dataclasses.replace(base_read, start_at=base_read.start_at + timedelta(hours=1))
    assert b5.parity_mismatches(side(base_read, p1), side(shifted, p2)) != []
    assert b5.parity_mismatches(side(base_read, p1, write=False),
                                side(base_read, p2, write=False)) == []
    assert b5.parity_mismatches(side(base_read, p1, write=False),
                                side(dataclasses.replace(base_read, status="negative"),
                                     p2, write=False)) != []
    assert b5.parity_mismatches(side(base_read, p1),
                                side(base_read, p2, tables_permit=str(uuid.uuid4()))) != []


def test_b5_u_c7_par_integration_helpers() -> None:
    """C (Codex 07f30e0 fixes 1-2; no B5 code): the two helpers P-PAR's `_side` calls.
    (1) `ref_labels` exists and, given an ids function with the pinned signature, labels
    every fixture patient and appointment of S1..S4 (7 rows, 14 distinct uuids), each with
    its "<scenario>.<key>.patient|ref" role; two tenants give disjoint uuids and equal label
    sets. (2) `is_load_success` is true only for a load with a success result on either
    adapter: a successful tick (JSON 200 / form m=tick), EHR put or reply (JSON 200 or 202,
    form 303 with no m), a load refusal, and a load's 409 are all false."""
    import uuid

    def ids(tenant: str, name: str, key: str) -> tuple[uuid.UUID, uuid.UUID]:
        ns = uuid.uuid5(uuid.NAMESPACE_URL, tenant)
        return uuid.uuid5(ns, f"{name}/{key}/p"), uuid.uuid5(ns, f"{name}/{key}/a")

    la = b5.ref_labels("clinic-syn-aaaaaaaa", ids=ids)
    lb = b5.ref_labels("clinic-syn-bbbbbbbb", ids=ids)
    rows = sum(len(keys) for keys, _, _ in b5.FIXTURE_SHAPE.values())
    assert rows == 7 and len(la) == 14 and len(set(la.values())) == 14
    assert not set(la) & set(lb) and sorted(la.values()) == sorted(lb.values())
    p, a = ids("clinic-syn-aaaaaaaa", "S2", "stop")
    assert (la[str(p)], la[str(a)]) == ("S2.stop.patient", "S2.stop.ref")
    yes = [("load", (200, None)), ("load", (303, "loaded"))]
    no = [("tick", (200, None)), ("tick", (303, "tick")), ("ehr", (200, None)),
          ("ehr", (303, None)), ("reply", (202, None)), ("reply", (303, None)),
          ("load", (409, "SCENARIO_PRESENT")), ("load", (303, "present")),
          ("load", (422, "UNKNOWN_SCENARIO")), ("load", (422, None)),
          ("load", (303, None)), ("load", (303, "hours"))]
    assert all(b5.is_load_success(x, o) for x, o in yes)
    assert not any(b5.is_load_success(x, o) for x, o in no), \
        [(x, o) for x, o in no if b5.is_load_success(x, o)]


def test_b5_u_c8_outbound_provider_ref_classification_is_narrow() -> None:
    """C (green classification, disclosed): only m05_outbound_ops.provider_ref in the exact
    FakeSender shape `fake-<16 hex>` is incidental; another shape, another table, or another
    column compares literally."""
    fake = "fake-0123456789abcdef"
    got = b5._scrub("{tenant}.m05_outbound_ops", {"provider_ref": fake, "phone": fake})
    assert got == {"provider_ref": "<inc:provider_ref>", "phone": fake}
    for table, row in (("{tenant}.m05_outbound_ops", {"provider_ref": "fake-0123"}),
                       ("{tenant}.m05_outbound_ops", {"provider_ref": "prov-syn-1"}),
                       ("{tenant}.m05_appointment_profile", {"provider_ref": fake})):
        assert b5._scrub(table, row) == row, (table, row)


# =========================================================================== A: placement

def test_b5_u_zn0_product_zone_is_eastern_only() -> None:
    """A (v1r4 §2 U-ZN0; OD-B5-3): the packaged product constants."""
    sc = b5.mod("scenarios")
    assert sc.LOAD_ZONE == "America/New_York"
    assert sc.LOAD_ZONES == ("America/New_York",)
    hours = (time(11, 0), time(16, 0))
    assert hours == (sc.LOAD_START, sc.LOAD_END)
    assert (sc.START_HOUR, sc.SEND_LEAD_DAYS, sc.FAR_LEAD_DAYS) == (18, 2, 10)
    # Eligibility E4 (el4_support pins; declared pin move): E1..E5 follow the four B5 names.
    assert sc.NAMES[:4] == ("S1", "S2", "S3", "S4")


def test_b5_u_zn0s_source_scan_no_zone_override() -> None:
    """A (v1r4 §2 source-scan row; Codex v1r4 Q1): in src/haloflow/synthetic, `LOAD_ZONES` is
    assigned exactly once (its definition in the scenarios package); no src file names any of
    the five TEST zones; no src call passes a `zones=` keyword; no src module reads an
    environment variable, header, query or form value into the zone set."""
    trees = b5.src_modules()
    defs = [(p, ln) for p, t in trees.items() for ln in b5.assigns_name(t, "LOAD_ZONES")]
    assert len(defs) == 1, f"LOAD_ZONES assignments: {defs}"
    (path, _), = defs
    assert path.parent.name == "scenarios", path
    for p, t in trees.items():
        text = p.read_text(encoding="utf-8")
        for z in b5.TEST_ZONES:
            assert z not in text, (p, z)
        for node in ast.walk(t):
            if isinstance(node, ast.Call):
                assert all(k.arg != "zones" for k in node.keywords), (p, node.lineno)


def test_b5_u_h1_async_hook_seam() -> None:
    """A (ob5_support INTERFACE hooks; the B2-U-H1 rule): `hooks.AFAULT` is None, `afire` is a
    coroutine function that awaits AFAULT when set, and no src module assigns AFAULT after its
    definition in hooks.py."""
    import inspect

    hooks = b5.mod("hooks")
    assert hooks.AFAULT is None
    assert inspect.iscoroutinefunction(hooks.afire)
    seen: list[str] = []

    async def probe(point: str, info: Any) -> None:
        seen.append(point)

    hooks.AFAULT = probe
    try:
        import asyncio

        asyncio.run(hooks.afire("b5.load.after_claim", {}))
    finally:
        hooks.AFAULT = None
    assert seen == ["b5.load.after_claim"]
    defs = [(p.name, ln) for p, t in b5.src_modules().items()
            for ln in b5.assigns_name(t, "AFAULT")]
    assert [n for n, _ in defs] == ["hooks.py"], defs


def test_b5_u_zn1a_every_minute_matches_the_owner_rule() -> None:
    """A (v1r4 §2 U-ZN1a): for every UTC minute of 2026 and 2027, `place(now, (LOAD_ZONE,))`
    refuses exactly when the independent oracle refuses; otherwise it returns America/New_York
    and the Eastern local date, and `start_at(zone, date, 2)` equals the oracle start, which
    is `send` under A3."""
    sc = b5.mod("scenarios")
    zones = (b5.LOAD_ZONE,)
    for year in YEARS:
        for t in _minutes(year):
            want = b5.expect_eastern(t)
            if want is None:
                with pytest.raises(sc.OutsideLoadHours):
                    sc.place(t, zones)
                continue
            zone, local_date = sc.place(t, zones)
            assert (zone, local_date) == (b5.LOAD_ZONE, t.astimezone(b5.EASTERN).date()), t
            got = sc.start_at(zone, local_date, sc.SEND_LEAD_DAYS)
            assert got == want and got.tzinfo is not None, (t, got, want)


@pytest.mark.parametrize("day", [date(2026, 11, 17), *b5.DST[2026], *b5.DST[2027]],
                         ids=str)
def test_b5_u_zn1c_half_open_endpoints(day: date) -> None:
    """A (Codex v1r4 pin 3): 10:59:59.999999 and 16:00:00 refuse; 11:00:00 and
    15:59:59.999999 load; on a standard day and on each DST transition day. A naive `now`
    is a ValueError."""
    sc = b5.mod("scenarios")
    y, m, d = day.year, day.month, day.day
    for t in (b5.eastern(y, m, d, 10, 59, 59, 999999), b5.eastern(y, m, d, 16)):
        with pytest.raises(sc.OutsideLoadHours):
            sc.place(t, (b5.LOAD_ZONE,))
    for t in (b5.eastern(y, m, d, 11), b5.eastern(y, m, d, 15, 59, 59, 999999)):
        assert sc.place(t, (b5.LOAD_ZONE,)) == (b5.LOAD_ZONE, day), t
        assert sc.start_at(b5.LOAD_ZONE, day, 2) == b5.expect_eastern(t)
    with pytest.raises(ValueError):
        sc.place(datetime(y, m, d, 12), (b5.LOAD_ZONE,))


def test_b5_u_zn1d_test_zones_and_the_s4_premise() -> None:
    """A (v1r4 §2; Codex v1r4 pin 1): with the TEST zone set, `place` returns the FIRST
    qualifying zone in order; S4's +10 local days start is `not_due` under A3 at every hour of
    a sample week (it never becomes a +2-day sendable appointment), in Eastern and in every
    test zone."""
    sc = b5.mod("scenarios")
    t0 = datetime(2026, 11, 16, tzinfo=UTC)
    for h in range(24 * 7):
        t = t0 + timedelta(hours=h, minutes=7)
        first = next(z for z in b5.TEST_ZONES
                     if b5.LOAD_START <= t.astimezone(ZoneInfo(z)).time() < b5.LOAD_END)
        zone, local_date = sc.place(t, b5.TEST_ZONES)
        assert (zone, local_date) == (first, t.astimezone(ZoneInfo(first)).date()), t
        far = sc.start_at(zone, local_date, sc.FAR_LEAD_DAYS)
        assert far == b5.expect_start(t, zone, 10)
        assert b5.a3_verdict(t, far, zone) == "not_due", (t, zone)
        east = b5.expect_eastern(t)
        if east is not None:
            far_e = sc.start_at(b5.LOAD_ZONE, t.astimezone(b5.EASTERN).date(), 10)
            assert b5.a3_verdict(t, far_e, b5.LOAD_ZONE) == "not_due", t


# =========================================================================== A: fixtures

def test_b5_u_f1_fixture_shape() -> None:
    """A (v1 R-B5-1, v1r1 E2): exactly S1..S4, with the pinned row keys, lead days and
    EHR-backed flags (S1/S3 EHR-backed; S4 +10 days)."""
    sc = b5.mod("scenarios")
    fx = sc.fixtures()
    assert tuple(fx)[:4] == b5.NAMES      # E4 (declared pin move): E1..E5 follow
    for name, (keys, leads, flags) in b5.FIXTURE_SHAPE.items():
        scenario = fx[name]
        assert scenario.name == name
        assert tuple(r.key for r in scenario.rows) == keys, name
        assert tuple(r.lead_days for r in scenario.rows) == leads, name
        assert tuple(r.ehr for r in scenario.rows) == flags, name


def test_b5_u_f2_fixture_content_is_synthetic() -> None:
    """A (v1 R-B5-1): canonical fictional phones, first names from the fixed list, provider and
    department refs from the packaged maps; the provider labels are non-empty strings."""
    sc = b5.mod("scenarios")
    for scenario in sc.fixtures().values():
        for r in scenario.rows:
            assert b5.CANONICAL.fullmatch(r.phone), r.phone
            assert r.first_name in sc.FIRST_NAMES, r.first_name
            assert r.provider_ref in sc.PROVIDERS, r.provider_ref
            assert r.department_ref in sc.DEPARTMENTS, r.department_ref
    assert all(type(v) is str and v for v in sc.PROVIDERS.values())


def test_b5_u_f3_scenarios_are_pairwise_disjoint() -> None:
    """A (v1r1 B1 / U-F7): phones, patient ids and appointment refs are pairwise disjoint
    across and within the four scenarios."""
    sc = b5.mod("scenarios")
    tenant = "clinic-syn-00000000"
    phones: list[str] = []
    ids: list[str] = []
    for scenario in sc.fixtures().values():
        for r in scenario.rows:
            phones.append(r.phone)
            ids.extend(str(x) for x in sc.ids(tenant, scenario.name, r.key))
    assert len(phones) == len(set(phones)), phones
    assert len(ids) == len(set(ids)), ids


def _raw_s1() -> dict[str, Any]:
    sc = b5.mod("scenarios")
    s1 = sc.fixtures()["S1"]
    (r,) = s1.rows
    return {"name": "S1", "rows": [{"key": r.key, "first_name": r.first_name, "phone": r.phone,
                                    "provider_ref": r.provider_ref,
                                    "department_ref": r.department_ref,
                                    "lead_days": r.lead_days, "ehr": r.ehr}]}


_BAD: list[tuple[str, str, Any]] = [
    # (case id, row field or "top", replacement; None removes the field)
    ("unknown_top", "top", {"extra": 1}),
    ("unknown_row", "row", {"extra": 1}),
    ("phone_real", "phone", "+14155550123"),
    ("phone_e164_bad", "phone", "+1555555010"),
    ("name_not_listed", "first_name", "Zelda-Not-Listed"),
    ("provider_unknown", "provider_ref", "prov-unknown"),
    ("department_unknown", "department_ref", "dept-unknown"),
    ("lead_bad", "lead_days", 3),
    ("lead_bool", "lead_days", True),
    ("ehr_not_bool", "ehr", 1),
    ("missing_phone", "phone", None),
    ("duplicate_key", "dup", None),
]


@pytest.mark.parametrize("case,field,value", _BAD, ids=[c[0] for c in _BAD])
def test_b5_u_f4_schema_refuses(case: str, field: str, value: Any) -> None:
    """A (v1 U-F1..F6): the strict schema refuses each malformed fixture with ScenarioInvalid
    naming a field; the valid S1 round-trips."""
    sc = b5.mod("scenarios")
    raw = _raw_s1()
    assert sc.validate(json.loads(json.dumps(raw))) == sc.fixtures()["S1"]
    if field == "top":
        raw.update(value)
    elif field == "row":
        raw["rows"][0].update(value)
    elif field == "dup":
        raw["rows"].append(dict(raw["rows"][0]))
    elif value is None:
        del raw["rows"][0][field]
    else:
        raw["rows"][0][field] = value
    with pytest.raises(sc.ScenarioInvalid) as e:
        sc.validate(raw)
    assert type(e.value.field) is str and e.value.field, case


def test_b5_u_f5_ids_are_deterministic_uuid5() -> None:
    """A (v1 R-B5-1): ids are uuid5, deterministic, and differ by tenant, scenario and key."""
    sc = b5.mod("scenarios")
    a = sc.ids("clinic-syn-aaaaaaaa", "S1", "confirm")
    assert a == sc.ids("clinic-syn-aaaaaaaa", "S1", "confirm")
    assert all(x.version == 5 for x in a) and a[0] != a[1]
    assert sc.ids("clinic-syn-bbbbbbbb", "S1", "confirm") != a
    assert sc.ids("clinic-syn-aaaaaaaa", "S3", "check") != a


# =========================================================================== A: state

R = frozenset({"r1", "r2", "r3"})
E = frozenset({"r1", "r2"})          # EHR-backed refs of the fixture


@pytest.mark.parametrize("case,present,ehr,want", [
    ("not_loaded", frozenset(), frozenset(), "not_loaded"),
    ("not_loaded_orphans", frozenset(), frozenset({"r1", "r2"}), "not_loaded"),
    ("loaded", R, E, "loaded"),
    ("loaded_extra_ehr", R, E | {"x9"}, "loaded"),
    ("ehr_lost_all", R, frozenset(), "ehr_lost"),
    ("ehr_lost_one", R, frozenset({"r1"}), "ehr_lost"),
    ("partial", frozenset({"r1"}), E, "partial"),
    ("partial_no_ehr", frozenset({"r3"}), frozenset(), "partial"),
])
def test_b5_u_ss_scenario_state(case: str, present: frozenset[str], ehr: frozenset[str],
                                want: str) -> None:
    """A (v1r3 §2 U-SS1..6): the four cases plus mixed EHR loss and orphans."""
    sc = b5.mod("scenarios")
    assert sc.scenario_state(present, ehr, R, E) == want, case


def test_b5_u_ss6_orphans_are_never_visible() -> None:
    """A (v1r3 §2 U-SS6; M-B5-13): the EHR section lists only refs present in the database;
    an orphan from a rolled-back load is invisible; a non-EHR-backed scenario (S2/S4 shape)
    never shows EHR loss."""
    sc = b5.mod("scenarios")
    assert sc.visible_ehr(frozenset({"r1"}), frozenset({"r1", "o1", "o2"})) == {"r1"}
    assert sc.visible_ehr(frozenset(), frozenset({"o1"})) == frozenset()
    assert sc.scenario_state(R, frozenset(), R, frozenset()) == "loaded"


# =========================================================================== A: display

LINKED = {"linked": True, "first_name": "Ada", "start_at": "2026-11-19T23:00:00Z",
          "department_tz": "America/New_York", "provider_ref": None}


def test_b5_u_d1_linked_with_known_provider() -> None:
    """A (v1r1 C2/C3): name line, then the local time in department_tz and the provider
    label from the packaged map."""
    sc, ui = b5.mod("scenarios"), b5.mod("ui")
    ref, label = sorted(sc.PROVIDERS.items())[0]
    got = ui.display_lines({**LINKED, "provider_ref": ref}, clinic_tz="America/New_York")
    assert got == ("Patient: Ada", f"Appointment: 2026-11-19 18:00 America/New_York — {label}")


@pytest.mark.parametrize("provider", [None, "prov-unknown"])
def test_b5_u_d2_unknown_provider_shows_time_only(provider: str | None) -> None:
    """A (v1 R-B5-5, v1r1 C3): a null or unknown provider_ref shows the time only."""
    got = b5.mod("ui").display_lines({**LINKED, "provider_ref": provider},
                                     clinic_tz="America/New_York")
    assert got == ("Patient: Ada", "Appointment: 2026-11-19 18:00 America/New_York")


def test_b5_u_d3_invalid_zone_falls_back_to_clinic_tz() -> None:
    """A (v1r1 C3, the A3 fallback): an invalid department_tz renders in clinic_tz."""
    got = b5.mod("ui").display_lines({**LINKED, "department_tz": "Not/AZone"},
                                     clinic_tz="America/New_York")
    assert got[1] == "Appointment: 2026-11-19 18:00 America/New_York"


def test_b5_u_d4_unlinked_is_the_approved_text() -> None:
    """A (T-B5-2, OD-B4-1): unlinked gives exactly "No appointment linked" and no name."""
    got = b5.mod("ui").display_lines({"linked": False}, clinic_tz="America/New_York")
    assert got == (b5.NO_APPOINTMENT,)


def test_b5_u_d5_lines_are_unescaped_text() -> None:
    """A (B-UI6 split): display_lines returns raw text (the page escapes once, P-ESC rows);
    an injection-shaped first name comes back verbatim, not pre-escaped."""
    name = "<b>&amp;\"x'"
    got = b5.mod("ui").display_lines({**LINKED, "first_name": name},
                                     clinic_tz="America/New_York")
    assert got[0] == f"Patient: {name}"


# =========================================================================== A: wording

def test_b5_u_msg_owner_wording() -> None:
    """A (OD-B5-2, message 6 replaced): the simulator page's texts are exactly the approved
    ones."""
    ui = b5.mod("sim_ui")
    assert ui.MESSAGES == b5.MESSAGES
    assert ui.PARTIAL_TEXT == b5.PARTIAL_TEXT
    assert ui.RESTART_TEXT == b5.RESTART_TEXT
    assert ui.RESTART_TEXT.format(scenario="S1") == (
        "The simulated EHR for S1 was cleared by a restart. Reset to run scenarios again.")
    assert ui.LOADED_LABEL == b5.LOADED_LABEL
    assert ui.HEADINGS == b5.HEADINGS
    assert ui.TICK_BUTTONS == b5.TICK_BUTTONS
    assert ui.LOAD_BUTTON == b5.LOAD_BUTTON
    assert "No time zone" not in json.dumps(ui.MESSAGES)


def test_b5_u_msg2_action_codes() -> None:
    """A (v1r2 §6, v1r4 §1): the load refusal codes and their statuses."""
    sa = b5.mod("sim_actions")
    assert sa.SCHEDULERS == ("due", "reminder", "writeback")
    assert sa.OUTBOX_CAP == 50
    caps = frozenset({"m05.intake.directory", "m05.synthetic.scenario_claim"})
    assert caps == sa.LOAD_CAPABILITIES
    assert (sa.LOAD_ROUTE, sa.TICK_ROUTE, sa.OUTBOX_ROUTE) == (
        b5.LOAD_ROUTE, b5.TICK_ROUTE, b5.OUTBOX_ROUTE)
    r = sa.Result(status=409, code="OUTSIDE_LOAD_HOURS", body={})
    assert (r.status, r.code) == (409, "OUTSIDE_LOAD_HOURS")


# =========================================================================== A/B: routes

@pytest.mark.parametrize("path,ok", [
    *[(p, True) for p in b5.FORM_ROUTES.values()],
    ("/synthetic/ui/items/00000000-0000-4000-8000-000000000001/claim", True),
    ("/synthetic/simulator/ui/LOAD", False),
    ("/synthetic/simulator/ui/load/", False),
    ("/synthetic/simulator/ui/load/x", False),
    ("/synthetic/simulator/ui/other", False),
    ("/synthetic/simulator/ui/", False),
    ("/synthetic/simulator/ui", False),
    ("/synthetic/simulator/load-scenario", False),
    ("/synthetic/simulator/ui/%6Coad", False),
    ("/x/synthetic/simulator/ui/load", False),
])
def test_b5_u_path_form_route_union(path: str, ok: bool) -> None:
    """B (v1r1 D2): `ui.UI_FORM_ROUTE` fully matches exactly the four simulator form paths in
    addition to the B4 staff form paths. On B4 the four simulator paths fail
    `simulator form path not matched`."""
    from haloflow.synthetic import ui

    got = ui.UI_FORM_ROUTE.fullmatch(path) is not None
    assert got == ok, ("simulator form path not matched" if ok else "matched", path)


def test_b5_u_doc_synthetic_app_md() -> None:
    """A (v1 R-B5-7; v1r3 §3; v1r4 §3; OD-B5-1): documentation/synthetic-app.md exists and
    states the load hours (message 6 verbatim), the Eastern display, the reminder-tick cutoff,
    the restart behaviour, the no-reply delay, and the documentation-only reset."""
    text = b5.doc_text()
    for needle in (b5.MESSAGES["hours"], "America/New_York", "7:00 PM Eastern",
                   "outside_window", "24 hours", "no on-demand", "No reset command",
                   "about 21 minutes", "couldn't reach EHR", "SIMULATED"):
        assert needle in text, needle
