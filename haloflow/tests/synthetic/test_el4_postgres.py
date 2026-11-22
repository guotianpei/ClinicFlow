"""Eligibility E4 PostgreSQL rows (synthetic app, disposable PG17 cluster). RED BY DESIGN.

Governing: `claude_e4-test-packet-draft-v1.md` section 3 + v1r1 R1-R4 (Codex ALIGN) + the
Codex P08b correction (E2-C01) + `Work Session 2026-11-22/codex_e4-retry-timing.md` (R2 as a
fixed-reference seam row R2x plus a real-clock control with margins). Baseline: merged E3b
`main` 3a8b884. Classes C / B / A as `el4_support` states; each row names its class.

Every eligibility row installs INSIDE its body (`e4.install(..., ELIGIBILITY)`), so at the
baseline it fails in its own body on the missing `profile` keyword (class A), never in a
fixture. Database state is read with the admin connection; the clearinghouse through the
row's spy (`clearinghouse_factory`), never through the E4 routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import b4_support as b4
import el4_support as e4
import ob5_support as b5
import pytest

pytestmark = pytest.mark.postgres


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
def test_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    """The B5 five-zone TEST set, so a scenario is loadable at any hour (B5 v1r4 section 2)."""
    monkeypatch.setattr(e4.mod("scenarios"), "LOAD_ZONES", b5.TEST_ZONES)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def el_install(pg: s.PgCluster, tmp_path: Path) -> b3.Install:
    inst = e4.install(pg, tmp_path, e4.ELIGIBILITY)
    e4.first_start(inst, e4.ELIGIBILITY)
    return inst


def loaded(r: bb.Resp, name: str) -> None:
    assert r.status == 200, (r.status, r.raw[:300])
    bb.assert_labelled(r)
    assert r.json == {"simulated": True, "scenario": name, "outcome": "loaded"}, r.json


async def booked(inst: b3.Install, app: Any, tok: str, name: str) -> str:
    """Load scenario `name`, make its booking trigger due (database time); its appointment ref."""
    loaded(await e4.load(app, tok, name), name)
    ref = e4.scenario_ref(inst, name, e4.EL_ROW_KEY)
    e4.make_due(inst, ref, "booking", -1.0)
    return ref


def one_item(inst: b3.Install, kind: str) -> str:
    items = e4.el_items(inst)
    assert [(k, st) for _i, k, st in items] == [(kind, "open")], items
    return str(items[0][0])


def run_total(counts: dict[str, Any]) -> int:
    return sum(v for k, v in counts.items() if k.startswith("run.") and k.count(".") == 1)


# =========================================================================== P01 registration

def test_el4_p01a_init_eligibility_profile(pg: s.PgCluster, tmp_path: Path) -> None:
    """A. `init` with profile eligibility installs to target 126 and records the profile;
    status reports install_profile and target_version."""
    inst = e4.install(pg, tmp_path, e4.ELIGIBILITY)
    assert e4.installed_target(inst) == e4.ELIGIBILITY_TARGET
    assert inst.state.get(e4.STATE_FIELD) == e4.ELIGIBILITY
    st = e4.mod("provision").status(inst.env)
    assert st["install_profile"] == e4.ELIGIBILITY and st["target_version"] == 126, st
    assert st["attested"] is True and st["sealed"] is True, st


def test_el4_p01a_cli_profile_flag(pg: s.PgCluster, tmp_path: Path) -> None:
    """A. `python -m haloflow.synthetic init --profile eligibility` (exit 0, phase ready)."""
    st_dir = s.state_dir(tmp_path)
    env = {**os.environ, **s.environ_for(pg, st_dir)}
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-m", "haloflow.synthetic", "init",
                           "--i-attest-disposable-cluster", "--profile", "eligibility"],
                          cwd=s.HALOFLOW, env=env, capture_output=True, text=True,
                          timeout=600, check=False)
    assert done.returncode == 0, (done.returncode, done.stderr[-500:])
    assert json.loads(done.stdout) == {"phase": "ready"}
    assert s.read_state(st_dir).get(e4.STATE_FIELD) == e4.ELIGIBILITY


def test_el4_p01c_activation_enforced(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. Eligibility startup runs with activation ENFORCE: the E1 flow settles (every
    eligibility key resolved); after a revoke the next eligibility tick is refused for the
    tenant (run phase ACTIVATION_REFUSED), with no new attempt."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()

    async def body() -> None:
        async with e4.running(inst, clearinghouse_factory=factory) as (_lc, app):
            tok = await e4.token(app)
            await e4.member(app, tok, "SYN-E1-001", "active")
            ref = await booked(inst, app, tok, "E1")
            assert e4.tick_counts(await e4.tick(app, tok))["run.settled"] == 1
            _activation_change(inst, "revoke")
            e4.make_due(inst, ref, "previsit", -1.0)
            counts = e4.tick_counts(await e4.tick(app, tok))
            assert counts.get("run.status.ACTIVATION_REFUSED") == 1, counts
            assert len(spies[0].submits) == 1

    run(body())


def _activation_change(inst: b3.Install, kind: str) -> None:
    """FIXTURE: append a grant/revoke through t042 as the M05 owner (sc_support pattern)."""
    import uuid

    from psycopg.types.json import Jsonb

    command = {"schema_version": 1, "change": kind, "actor_id": str(uuid.uuid4()),
               "ticket_reference": "EL4-TEST", "reason": "synthetic E4 row"}
    with inst.c.connect(s.TARGET_DB) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE haloflow_m05_owner")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (inst.state["tenant_id"],))
        conn.execute(f"SELECT {inst.schema}.m05_activation_change(%s)", (Jsonb(command),))


def _shape(value: Any) -> Any:
    """The nested key/type structure of a JSON value (leaf values dropped)."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return ["list"]
    return type(value).__name__


def test_el4_p01d0_pre_e4_writer_state_shape(pg: s.PgCluster, tmp_path: Path) -> None:
    """C (binds the committed fixture to the GENUINE pre-E4 writer). The frozen 3a8b884
    installer writes a state file with no profile field, the fixture's top-level key set, and
    the fixture's nested structure for logins, note_keys and support."""
    inst = e4.pre_e4_install(pg, tmp_path)
    real = json.loads(e4.state_path(inst).read_bytes())
    fixture = json.loads(e4.PRE_E4_STATE.read_bytes())
    assert set(real) == set(fixture), sorted(set(real) ^ set(fixture))
    assert e4.STATE_FIELD not in real
    for key in ("logins", "note_keys", "support"):
        assert _shape(real[key]) == _shape(fixture[key]), key
    assert set(real["seal"]) == {"profile", "point", "nondet", "identities", "ledger"}


def test_el4_p01d1_pre_e4_state_default_startup(pg: s.PgCluster, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A (Codex finding 1). State and database written by the frozen PRE-E4 writer: the E4
    reader's `status` attests it, and a default startup on it succeeds unchanged (one pool
    constructed and opened - the counter is not vacuous), target 97, the B-series schedulers
    only, eligibility routes 404 / UNKNOWN_SCHEDULER; the state file is not rewritten
    except by the existing runtime marker."""
    inst = e4.pre_e4_install(pg, tmp_path)
    assert e4.STATE_FIELD not in inst.state
    st = e4.mod("provision").status(inst.env)
    assert st["install_profile"] == e4.DEFAULT and st["target_version"] == e4.DEFAULT_TARGET, st
    assert st["attested"] is True and st["sealed"] is True, st
    before = json.loads(e4.state_path(inst).read_bytes())
    pools = e4.PoolCounter(monkeypatch)

    async def body() -> None:
        async with e4.running(inst, profile=e4.DEFAULT) as (lc, app):
            assert (pools.constructed, pools.opened) == (1, 1)
            assert tuple(sorted(lc.schedulers)) == tuple(sorted(e4.B5_SCHEDULERS))
            tok = await e4.token(app)
            r = await e4.control(app, tok, {"fail_next": 1})
            assert r.status == 404, (r.status, r.raw[:200])
            r = await e4.tick(app, tok, "eligibility")
            assert (r.status, r.json.get("code")) == (422, "UNKNOWN_SCHEDULER"), r.raw[:200]

    run(body())
    after = json.loads(e4.state_path(inst).read_bytes())
    assert {k: v for k, v in after.items() if k != "runtime"} == before
    assert e4.STATE_FIELD not in after


def test_el4_p01d1c_current_default_install_startup(pg: s.PgCluster, tmp_path: Path) -> None:
    """A. A default-profile install by the E4 writer keeps the pre-E4 key set (no profile
    field) and starts with the B-series schedulers only."""
    inst = e4.install(pg, tmp_path, e4.DEFAULT)
    assert e4.STATE_FIELD not in inst.state
    assert e4.installed_target(inst) == e4.DEFAULT_TARGET

    async def body() -> None:
        async with e4.running(inst, profile=e4.DEFAULT) as (lc, _app):
            assert tuple(sorted(lc.schedulers)) == tuple(sorted(e4.B5_SCHEDULERS))

    run(body())


def _refused_before_pool(inst: b3.Install, profile: str, code: str,
                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex finding 2: the runtime pool construction point is instrumented; a refusal must
    construct and open NO pool (not merely clean one up), write no marker, and leave the
    state file's bytes and mtime unchanged."""
    before = e4.state_stat(inst)
    app_name = f"el4-refuse-{profile}"
    pools = e4.PoolCounter(monkeypatch)

    async def body() -> None:
        lc = e4.lifecycle(inst, profile=profile, app_name=app_name)
        with pytest.raises(e4.mod("errors").SyntheticRefused) as err:
            await lc.start()
        assert err.value.code == code, err.value.code

    run(body())
    assert (pools.constructed, pools.opened) == (0, 0)
    assert e4.pool_backends(inst, app_name) == 0
    assert e4.state_stat(inst) == before


def test_el4_p01d2_pre_e4_state_eligibility_request_refused(
        pg: s.PgCluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A. Pre-E4 writer state + an eligibility startup request -> PROFILE_MISMATCH, zero pool
    constructions."""
    inst = e4.pre_e4_install(pg, tmp_path)
    _refused_before_pool(inst, e4.ELIGIBILITY, "PROFILE_MISMATCH", monkeypatch)


def test_el4_p01d3_eligibility_file_default_request_refused(
        pg: s.PgCluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A. The reverse: an eligibility state file (target 126) + a default startup request ->
    PROFILE_MISMATCH, zero pool constructions."""
    inst = e4.install(pg, tmp_path, e4.ELIGIBILITY)
    _refused_before_pool(inst, e4.DEFAULT, "PROFILE_MISMATCH", monkeypatch)


def test_el4_p01d4_eligibility_file_wrong_target_refused(
        pg: s.PgCluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A. A pre-E4 writer install (target 97) whose state file claims eligibility ->
    PROFILE_TARGET_MISMATCH, zero pool constructions."""
    inst = e4.pre_e4_install(pg, tmp_path)
    e4.rewrite_state(inst, install_profile=e4.ELIGIBILITY)
    _refused_before_pool(inst, e4.ELIGIBILITY, "PROFILE_TARGET_MISMATCH", monkeypatch)


def test_el4_p01d5_pool_counter_observes_control(pg: s.PgCluster, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """C (proves `PoolCounter` is not vacuous on the baseline): a pre-E4 writer install and a
    baseline startup (no profile keyword) construct and open exactly one runtime pool."""
    inst = e4.pre_e4_install(pg, tmp_path)
    pools = e4.PoolCounter(monkeypatch)

    async def body() -> None:
        async with bb.running(inst, app_name="el4-pool-control"):
            assert (pools.constructed, pools.opened) == (1, 1)

    run(body())


def test_el4_p01e_default_install_target_control(pg: s.PgCluster, tmp_path: Path) -> None:
    """C. The baseline install (no keyword) is at target 97 (with B1-E2 / B5-E2)."""
    inst = e4.install(pg, tmp_path, None)
    assert e4.installed_target(inst) == e4.DEFAULT_TARGET


def test_el4_p01f_eligibility_profile_pin_equals_measurement() -> None:
    """C (added at green, Codex codex_e4-profile-control-review.md; the B2-P1 pattern): the
    committed eligibility installed-state profile (installed_profile_eligibility.py, target
    126) equals a fresh measurement (two independent eligibility installs, identical traces).
    A unit, migration, profile-SQL, settings or view change without a re-pin fails here."""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, str(s.HALOFLOW / "scripts" / "b2_measure_profile.py"),
                        "--check", "--profile", "eligibility"], capture_output=True, text=True,
                       timeout=1500, env={"PATH": os.environ.get("PATH", ""),
                                          "HALOFLOW_SYNTHETIC_PG_BIN": str(s.pg_bin())})
    assert (r.returncode, r.stdout.strip().splitlines()[-1:]) == (0, ["profile pin MATCHES"]), \
        r.stdout[-2000:] + r.stderr[-2000:]


# =========================================================================== P03 no network

def test_el4_p03a_no_network(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A. Eligibility app start -> every scenario loaded and ticked -> stop: no non-AF_UNIX
    socket, no httpx Client/AsyncClient, no StediEligibilityClient constructed."""
    import socket
    import sys

    stedi_loaded_before = "haloflow.integrations.stedi" in sys.modules
    inst = el_install(pg, tmp_path)
    real = socket.socket.__init__
    calls: list[Any] = []

    def guarded(self: Any, family: int = socket.AF_INET, *a: Any, **k: Any) -> None:
        if family != socket.AF_UNIX:
            calls.append(family)
            raise AssertionError("network socket created")
        real(self, family, *a, **k)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            monkeypatch.setattr(socket.socket, "__init__", guarded)
            # Importing the Stedi client loads app Settings, so it is never imported here:
            # if already loaded its constructor is counted; otherwise the proof is that the
            # flow never loaded it (the B1-C3s spy rule).
            if stedi_loaded_before:
                monkeypatch.setattr(sys.modules["haloflow.integrations.stedi"]
                                    .StediEligibilityClient, "__init__",
                                    lambda *a, **k: calls.append("stedi"))
            for mod_name, attr in (("httpx", "Client"), ("httpx", "AsyncClient")):
                with contextlib.suppress(ImportError):
                    m = __import__(mod_name)
                    monkeypatch.setattr(m, attr, lambda *a, _n=attr, **k: calls.append(_n))
            tok = await e4.token(app)
            await e4.member(app, tok, "SYN-E1-001", "active")
            await e4.member(app, tok, "SYN-E5-001", "active")
            for name in e4.EL_SCENARIOS:
                loaded(await e4.load(app, tok, name), name)
                e4.make_due(inst, e4.scenario_ref(inst, name, e4.EL_ROW_KEY), "booking")
                e4.tick_counts(await e4.tick(app, tok))

    run(body())
    assert calls == []
    assert stedi_loaded_before or "haloflow.integrations.stedi" not in sys.modules


# =========================================================================== P04 simulator

def test_el4_p04a_positive_verification(pg: s.PgCluster, tmp_path: Path,
                                        test_zones: None) -> None:
    """A (v1r1 R1). E1 (present, priority, member active): one tick completes ONE verified
    attempt for the intended appointment, then nothing is raised."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()

    async def body() -> None:
        async with e4.running(inst, clearinghouse_factory=factory) as (_lc, app):
            tok = await e4.token(app)
            await e4.member(app, tok, "SYN-E1-001", "active")
            ref = await booked(inst, app, tok, "E1")
            counts = e4.tick_counts(await e4.tick(app, tok))
            assert run_total(counts) >= 1 and counts["run.settled"] == 1, counts
            assert counts["run.context_failed"] == 0, counts
            (vid, owner, state, cls, _reason), = e4.verifications(inst, ref)
            assert (owner, state, cls) == ("booking", "settled", "active")
            (attempt, n, a_state), = e4.attempts(inst, vid)
            assert (n, a_state) == (1, "answered")
            spy = spies[0]
            assert spy.submits == [attempt]
            (req,) = spy.requests
            assert (req.get("appointment_ref"), req.get("member_id")) == (ref, "SYN-E1-001"), req
            assert req.get("tenant_id") == str(inst.state["tenant_id"]), req
            assert e4.triggers(inst, ref)["booking"][0] == "satisfied"
            assert e4.case_events(inst, ref) == [] and e4.el_items(inst) == []
            assert e4.outbox_count(inst) == 0

    run(body())


@pytest.mark.parametrize("name", ["E2", "E4", "E5"])
def test_el4_p04b_manual_routing(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                 name: str) -> None:
    """A. none / incomplete / non-priority -> one manual case and item with the exact reason;
    the clearinghouse is never called."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()
    kind, reason = e4.EL_SCENARIO_OUTCOME[name]

    async def body() -> None:
        async with e4.running(inst, clearinghouse_factory=factory) as (_lc, app):
            tok = await e4.token(app)
            ref = await booked(inst, app, tok, name)
            counts = e4.tick_counts(await e4.tick(app, tok))
            assert counts["apply.opened"] == 1, counts
            assert kind is not None
            item = one_item(inst, kind)
            assert e4.case_row(inst, item)[3] == reason
            assert [r[3] for r in e4.case_events(inst, ref)] == [reason]
            assert spies[0].submits == []

    run(body())


def test_el4_p04c_pending_until_boundary(pg: s.PgCluster, tmp_path: Path,
                                         test_zones: None) -> None:
    """A. E3 (pending): nothing before the OD-EL9 deadline (deadline 60 s ahead); after it
    (deadline 1 s past, database time) one coverage_not_received case. Requires the E1
    not-received tick to run inside the eligibility managed tick (v1r2 D-E4-8)."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            loaded(await e4.load(app, tok, "E3"), "E3")
            ref = e4.scenario_ref(inst, "E3", e4.EL_ROW_KEY)
            e4.tick_counts(await e4.tick(app, tok))
            e4.age_coverage_deadline(inst, ref, 60.0)
            e4.tick_counts(await e4.tick(app, tok))
            assert e4.case_events(inst, ref) == [] and e4.el_items(inst) == []
            e4.age_coverage_deadline(inst, ref, -1.0)
            counts = e4.tick_counts(await e4.tick(app, tok))
            assert counts.get("e1.raised") == 1, counts
            item = one_item(inst, "eligibility_manual")
            assert e4.case_row(inst, item)[3] == "coverage_not_received"

    run(body())


@pytest.mark.parametrize(("setup", "kind", "reason"), [
    ("inactive", "eligibility_inactive", "coverage_inactive"),
    ("malformed", "eligibility_inconclusive", "response_unreadable")])
def test_el4_p04d_single_attempt_outcomes(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                          setup: str, kind: str, reason: str) -> None:
    """A. member inactive -> eligibility_inactive / coverage_inactive; malformed_next 1 ->
    eligibility_inconclusive / response_unreadable. One attempt each, no retry (E2-P17)."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()

    async def body() -> None:
        async with e4.running(inst, clearinghouse_factory=factory) as (_lc, app):
            tok = await e4.token(app)
            if setup == "inactive":
                await e4.member(app, tok, "SYN-E1-001", "inactive")
            else:
                await e4.member(app, tok, "SYN-E1-001", "active")
                r = await e4.control(app, tok, {"malformed_next": 1})
                assert r.status == 200, r.raw[:200]
            ref = await booked(inst, app, tok, "E1")
            e4.tick_counts(await e4.tick(app, tok))
            e4.tick_counts(await e4.tick(app, tok))
            (vid, _o, _st, _c, _r), = e4.verifications(inst, ref)
            assert len(e4.attempts(inst, vid)) == 1 and len(spies[0].submits) == 1
            item = one_item(inst, kind)
            assert e4.case_row(inst, item)[3] == reason

    run(body())


def test_el4_p04d_r2x_retry_seam_exact_boundary(pg: s.PgCluster, tmp_path: Path) -> None:
    """A (codex_e4-retry-timing.md: deterministic exact boundary). On the eligibility
    install, the worker's own seam m05_el_retry_ready with ONE fixed database reference
    instant R: R+119.999 s false; R+120 s true (inclusive); R+120.001 s true."""
    inst = e4.install(pg, tmp_path, e4.ELIGIBILITY)
    got = e4.q(inst, "WITH r AS (SELECT clock_timestamp() AS t) SELECT"
               " {s}.m05_el_retry_ready(r.t, r.t + interval '119.999 seconds'),"
               " {s}.m05_el_retry_ready(r.t, r.t + interval '120 seconds'),"
               " {s}.m05_el_retry_ready(r.t, r.t + interval '120.001 seconds') FROM r")
    assert got == [(False, True, True)], got


def test_el4_p04d_r2_retry_real_clock(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A (v1r1 R2 with Codex margins). fail_next 2: attempt 1 times out; aged 110 s -> no
    new attempt; aged 120 s -> attempt 2 times out and the verification settles
    inconclusive / payer_unavailable; no third attempt at 240 s or 1 h; the item shows the
    OD-EL11 title and the OD-EL8 reason label."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()

    async def body() -> None:
        async with e4.running(inst, clearinghouse_factory=factory) as (_lc, app):
            tok = await e4.token(app)
            await e4.member(app, tok, "SYN-E1-001", "active")
            r = await e4.control(app, tok, {"fail_next": 2})
            assert r.status == 200, r.raw[:200]
            ref = await booked(inst, app, tok, "E1")
            e4.tick_counts(await e4.tick(app, tok))
            (vid, _o, state, _c, _r), = e4.verifications(inst, ref)
            (a1, n1, s1), = e4.attempts(inst, vid)
            assert (n1, s1) == (1, "timed_out") and state != "settled"
            assert e4.case_events(inst, ref) == []
            e4.age_failure(inst, a1, -110.0)
            e4.tick_counts(await e4.tick(app, tok))
            assert len(e4.attempts(inst, vid)) == 1 and len(spies[0].submits) == 1
            e4.age_failure(inst, a1, -e4.RETRY_DELAY_S)
            e4.tick_counts(await e4.tick(app, tok))
            got = e4.attempts(inst, vid)
            assert [(n, st) for _a, n, st in got] == [(1, "timed_out"), (2, "timed_out")], got
            assert len(spies[0].submits) == 2
            (_v, _o, state, cls, reason), = e4.verifications(inst, ref)
            assert (state, cls, reason) == ("settled", "inconclusive", "payer_unavailable")
            for offset in (-240.0, -3600.0):
                e4.age_failure(inst, got[1][0], offset)
                e4.tick_counts(await e4.tick(app, tok))
            assert len(e4.attempts(inst, vid)) == 2 and len(spies[0].submits) == 2
            item = one_item(inst, "eligibility_inconclusive")
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert e4.EL_TITLES["eligibility_inconclusive"] in pg_.text
            assert e4.EL_REASONS["payer_unavailable"] in pg_.text

    run(body())


def test_el4_p04e_every_e4_response_labelled(pg: s.PgCluster, tmp_path: Path,
                                             test_zones: None) -> None:
    """A. B-L1: every E4 JSON response carries the label header and simulated: true; the
    form twin redirects with the label header."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            for body_ in ({"fail_next": 0}, {"malformed_next": 0},
                          {"member": {"member_id": "SYN-E1-001", "status": "active"}},
                          {"fail_next": 99}):
                bb.assert_labelled(await e4.control(app, tok, body_))
            csrf = await b5.page_csrf(app)
            r = await e4.post_form(app, e4.CLEARINGHOUSE_FORM,
                                   [("csrf", csrf), ("op", "fail_next"), ("n", "1")])
            assert r.status == 303 and r.header(bb.LABEL_HEADER) == ["true"], r.raw[:200]
            bb.assert_labelled(await e4.tick(app, tok))

    run(body())


@pytest.mark.parametrize("bad", [
    {"member": {"member_id": "SYN-E1-001", "status": "lapsed"}},
    {"member": {"member_id": "M000001", "status": "active"}},
    {"fail_next": 11}, {"fail_next": 1, "extra": 1}, {}])
def test_el4_p04f_malformed_controls(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                     bad: Any) -> None:
    """A. Malformed control bodies -> 422 MALFORMED and no state change (a following E1
    flow with the member active still settles active on the first attempt)."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()

    async def body() -> None:
        async with e4.running(inst, clearinghouse_factory=factory) as (_lc, app):
            tok = await e4.token(app)
            await e4.member(app, tok, "SYN-E1-001", "active")
            r = await e4.control(app, tok, bad)
            assert (r.status, (r.json or {}).get("code")) == (422, "MALFORMED"), r.raw[:200]
            bb.assert_labelled(r)
            ref = await booked(inst, app, tok, "E1")
            e4.tick_counts(await e4.tick(app, tok))
            (_v, _o, state, cls, _r), = e4.verifications(inst, ref)
            assert (state, cls) == ("settled", "active") and len(spies[0].submits) == 1

    run(body())


def test_el4_p04g_default_profile_inventory(pg: s.PgCluster, tmp_path: Path) -> None:
    """B/C. Baseline app (no profile keyword): the eligibility scheduler is unknown and the
    clearinghouse routes are not served (control at baseline; stays true for the default
    profile after E4 via P01d-1)."""
    inst = e4.install(pg, tmp_path, None)
    bb.first_start(inst)

    async def body() -> None:
        async with bb.running(inst) as (_lc, app):
            tok = await bb.session_token(app)
            r = await e4.tick(app, tok, "eligibility")
            assert (r.status, r.json.get("code")) == (422, "UNKNOWN_SCHEDULER"), r.raw[:200]
            r = await e4.control(app, tok, {"fail_next": 1})
            assert r.status == 404, (r.status, r.raw[:200])

    run(body())


# =========================================================================== P05 E2 -> E3 -> screen

async def _make_kind(inst: b3.Install, app: Any, tok: str, kind: str) -> str:
    """One eligibility item of `kind` through the app: inactive (member inactive), inconclusive
    (malformed_next 1), manual (E2, no coverage). Returns the item id."""
    if kind == "eligibility_manual":
        await booked(inst, app, tok, "E2")
    else:
        await e4.member(app, tok, "SYN-E1-001",
                        "inactive" if kind == "eligibility_inactive" else "active")
        if kind == "eligibility_inconclusive":
            r = await e4.control(app, tok, {"malformed_next": 1})
            assert r.status == 200, r.raw[:200]
        await booked(inst, app, tok, "E1")
    e4.tick_counts(await e4.tick(app, tok))
    return one_item(inst, kind)


KIND_REASON = {"eligibility_inactive": "coverage_inactive",
               "eligibility_inconclusive": "response_unreadable",
               "eligibility_manual": "no_coverage"}


@pytest.mark.parametrize("kind", list(e4.EL_TITLES))
def test_el4_p05a_screen_shows_kind(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                    kind: str) -> None:
    """A (Codex trace row). Fixture -> tick (E2 event, E3a projection) -> the list shows the
    OD-EL11 title; the view shows the title, the OD-EL8 reason, the view token as hidden
    fields of its forms, and no identity, raw response or transaction reference."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            item = await _make_kind(inst, app, tok, kind)
            r, pg_ = await e4.page(app, e4.UI_PREFIX)
            b4.html_ok(r)
            assert e4.EL_TITLES[kind] in pg_.text and "Unrecognized" not in pg_.text
            r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            b4.html_ok(r)
            assert e4.EL_TITLES[kind] in pg_.text
            assert e4.EL_REASONS[KIND_REASON[kind]] in pg_.text
            case_id, _st, version, _reason = e4.case_row(inst, item)
            for action in ("contact", "close"):
                f = b4.form_of(pg_, f"{e4.UI_PREFIX}/{item}/{action}")
                assert (f.hidden("case_id"), f.hidden("item_id"), f.hidden("case_version")) \
                    == (case_id, item, str(version))
            for leak in ("SYN-E1-001", "1970-01-01", "coinsurance", "deductible_total",
                         "txn_ref", "Synthetic Patient"):
                assert leak not in pg_.raw, leak

    run(body())


@pytest.mark.parametrize("code", list(e4.EL_CONTACTS))
def test_el4_p05b_contact_form(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                               code: str) -> None:
    """A. The contact form with each OD-EL7 entry: one history entry, the item stays open,
    case_version unchanged; the label shows in the item's history."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            item = await _make_kind(inst, app, tok, "eligibility_manual")
            before = e4.case_row(inst, item)
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = b4.form_of(pg_, f"{e4.UI_PREFIX}/{item}/contact")
            assert code in f.radios.get("contact_code", []), f.radios
            r = await e4.post_form(app, f.action, b4.submit_fields(f, contact_code=code))
            assert r.status == 303, (r.status, r.raw[:200])
            assert e4.contact_codes(inst, item) == [code]
            assert e4.item_state(inst, item)[0] in ("open", "in_progress")
            assert e4.case_row(inst, item)[2] == before[2]
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert e4.EL_CONTACTS[code] in pg_.text

    run(body())


@pytest.mark.parametrize("outcome", list(e4.EL_OUTCOMES))
def test_el4_p05c_close_form(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                             outcome: str) -> None:
    """A. Close with each OD-EL6 outcome: el_other without a note is refused before the
    service (A note is required for Other., zero effects); with a note it resolves and the
    note text is never shown."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            item = await _make_kind(inst, app, tok, "eligibility_manual")
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = b4.form_of(pg_, f"{e4.UI_PREFIX}/{item}/close")
            assert set(f.radios.get("outcome_code", [])) == set(e4.EL_OUTCOMES), f.radios
            if outcome == "el_other":
                before = e4.item_state(inst, item)
                r = await e4.post_form(app, f.action, b4.submit_fields(f, outcome_code=outcome))
                assert r.status in (303, 422), (r.status, r.raw[:200])
                assert e4.item_state(inst, item) == before
                r = await e4.post_form(app, f.action, b4.submit_fields(
                    f, outcome_code=outcome, note=e4.CANARY["note"]))
            else:
                r = await e4.post_form(app, f.action, b4.submit_fields(f, outcome_code=outcome))
            assert r.status == 303, (r.status, r.raw[:200])
            assert e4.item_state(inst, item)[:2] == ("resolved", outcome)
            assert e4.case_row(inst, item)[1] == "closed"
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert e4.EL_OUTCOMES[outcome] in pg_.text
            assert e4.CANARY["note"] not in pg_.raw

    run(body())


def test_el4_p05d_stale_form_case_changed(pg: s.PgCluster, tmp_path: Path,
                                          test_zones: None) -> None:
    """A. The case_version advances (a later note-only event) between view and post ->
    CASE_CHANGED: the reload message, zero effects."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            await e4.member(app, tok, "SYN-E1-001", "inactive")
            ref = await booked(inst, app, tok, "E1")
            e4.tick_counts(await e4.tick(app, tok))
            item = one_item(inst, "eligibility_inactive")
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = b4.form_of(pg_, f"{e4.UI_PREFIX}/{item}/close")
            v0 = e4.case_row(inst, item)[2]
            e4.make_due(inst, ref, "previsit", -1.0)          # a later unresolved finding
            e4.tick_counts(await e4.tick(app, tok))
            assert e4.case_row(inst, item)[2] == v0 + 1
            before = (e4.item_state(inst, item), e4.contact_codes(inst, item))
            r = await e4.post_form(app, f.action,
                                   b4.submit_fields(f, outcome_code="el_confirmed_inactive"))
            assert r.status == 409, (r.status, r.raw[:200])
            assert e4.CASE_CHANGED_MESSAGE in b4.page(r).text
            assert (e4.item_state(inst, item), e4.contact_codes(inst, item)) == before

    run(body())


def test_el4_p05e_later_active_note(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A (OD-EL3). A later active finding adds a note to the open case; the item stays open
    until staff close it."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            await e4.member(app, tok, "SYN-E1-001", "inactive")
            ref = await booked(inst, app, tok, "E1")
            e4.tick_counts(await e4.tick(app, tok))
            item = one_item(inst, "eligibility_inactive")
            await e4.member(app, tok, "SYN-E1-001", "active")
            e4.make_due(inst, ref, "previsit", -1.0)
            e4.tick_counts(await e4.tick(app, tok))
            assert e4.item_state(inst, item)[0] in ("open", "in_progress")
            assert e4.case_row(inst, item)[1] == "open"
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            assert pg_.text.count(e4.EL_TITLES["eligibility_inactive"]) >= 1

    run(body())


def test_el4_p05f_cross_tenant_ui(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A (UI layer; JSON layer is the E3-P17 control). An item id that is not this tenant's
    eligibility item -> the item view is not found and both forms are refused, zero effects."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            item = await _make_kind(inst, app, tok, "eligibility_manual")
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = b4.form_of(pg_, f"{e4.UI_PREFIX}/{item}/contact")
            foreign = "0b8e7f3c-1d2a-4c5b-8e9f-0a1b2c3d4e5f"
            r, _ = await e4.page(app, f"{e4.UI_PREFIX}/{foreign}")
            assert r.status == 404, r.status
            for action in ("contact", "close"):
                fields = [(k, foreign if k == "item_id" else v)
                          for k, v in b4.submit_fields(f, contact_code="el_left_message",
                                                       outcome_code="el_self_pay")]
                r = await e4.post_form(app, f"{e4.UI_PREFIX}/{foreign}/{action}", fields)
                assert r.status in (303, 404, 409, 422) and r.status != 200, r.status
            assert e4.contact_codes(inst, item) == []
            assert e4.item_state(inst, item)[0] in ("open", "in_progress")

    run(body())


# =========================================================================== P06 escaping, logs

def test_el4_p06b_pages_csp_and_banner(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. The eligibility item view carries the B4 CSP / security headers, the SIMULATED
    banner once, and no script (b4.html_ok)."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            item = await _make_kind(inst, app, tok, "eligibility_inactive")
            r, _ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            b4.html_ok(r)

    run(body())


def test_el4_p06a_injected_markup_inert(pg: s.PgCluster, tmp_path: Path, test_zones: None) -> None:
    """A. Markup injected into an eligibility value source renders inert: the case reason is
    overridden (FIXTURE, admin) with markup -> the view shows "Unrecognized", never the tag."""
    inst = el_install(pg, tmp_path)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            item = await _make_kind(inst, app, tok, "eligibility_manual")
            e4.q(inst, "ALTER TABLE {s}.m05_el_cases DISABLE TRIGGER USER")
            with contextlib.suppress(Exception):
                e4.q(inst, "UPDATE {s}.m05_el_cases SET current_reason = %s"
                     " WHERE item_id = %s::uuid", ("<b>x</b>", item))
            r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            b4.html_ok(r)
            assert "<b>x</b>" not in pg_.raw and "b" not in pg_.tags

    run(body())


def test_el4_p06c_canary_never_logged(pg: s.PgCluster, tmp_path: Path, test_zones: None,
                                      caplog: pytest.LogCaptureFixture) -> None:
    """A. Over the whole P04/P05 flow (controls with the canary member, load, ticks, view,
    contact, close with the canary note), no canary value appears in any log record."""
    inst = el_install(pg, tmp_path)
    caplog.set_level(logging.DEBUG)

    async def body() -> None:
        async with e4.running(inst) as (_lc, app):
            tok = await e4.token(app)
            await e4.member(app, tok, e4.CANARY["member_id"], "inactive")
            await e4.control(app, tok, {"member": {"member_id": e4.CANARY["member_id"],
                                                   "status": e4.CANARY["payer_free_text"]}})
            item = await _make_kind(inst, app, tok, "eligibility_manual")
            _r, pg_ = await e4.page(app, f"{e4.UI_PREFIX}/{item}")
            f = b4.form_of(pg_, f"{e4.UI_PREFIX}/{item}/close")
            await e4.post_form(app, f.action, b4.submit_fields(
                f, outcome_code="el_other", note=e4.CANARY["note"]))

    run(body())
    text = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    assert caplog.records, "no log records captured"
    for value in e4.CANARY.values():
        assert value not in text, value


# =========================================================================== P08 drain order

def test_el4_p08_drain_before_pool_close(pg: s.PgCluster, tmp_path: Path,
                                         test_zones: None) -> None:
    """A (v1r1 R4; kills M03). A tick held inside submit; shutdown requested: STOPPING, new
    tick / load / staff action refused with the stopping code, pool still open; release:
    the held attempt settles as expected (active), the eligibility drain completes, THEN
    the pool closes; final STOPPED. Order oracle over recorded events."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()
    order = e4.Order([])
    app_name = "el4-p08"

    async def body() -> None:
        lc = e4.lifecycle(inst, profile=e4.ELIGIBILITY, app_name=app_name,
                          clearinghouse_factory=factory)
        await lc.start()
        app = e4.mod("app").create_synthetic_app(lifecycle=lc, bind=bb.BIND)
        tok = await e4.token(app)
        await e4.member(app, tok, "SYN-E1-001", "active")
        ref = await booked(inst, app, tok, "E1")
        spy = spies[0]
        spy.hold = asyncio.Event()
        held_tick = asyncio.ensure_future(e4.tick(app, tok))
        await asyncio.wait_for(spy.held.wait(), 30)
        sched = lc.schedulers["eligibility"]
        real_stop = sched.stop

        async def stop() -> None:
            await real_stop()
            order.mark("eligibility_drained")

        sched.stop = stop
        pool = lc.runtime.pool
        real_close = pool.close

        async def close(*a: Any, **k: Any) -> Any:
            order.mark("pool_closed")
            return await real_close(*a, **k)

        pool.close = close
        lc.request_stop("TEST")
        order.mark("stop_requested")
        assert lc.state == "STOPPING"
        for r in (await e4.tick(app, tok), await e4.load(app, tok, "E2")):
            assert r.status == 503 and r.json.get("code") == lc.stop_code, (r.status, r.raw[:200])
        order.mark("refusal_observed")
        assert e4.pool_backends(inst, app_name) >= 1          # resources not closed yet
        order.mark("tick_released")
        spy.hold.set()
        await held_tick
        result = await lc.wait_stopped()
        assert result.disposition == "drained", result
        assert lc.state == "STOPPED"
        (_v, _o, state, cls, _r), = e4.verifications(inst, ref)
        assert (state, cls) == ("settled", "active")           # expected settlement

    run(body())
    assert order.events == ["stop_requested", "refusal_observed", "tick_released",
                            "eligibility_drained", "pool_closed"], order.events


def test_el4_p08b_stop_timeout_keeps_durable_attempt(pg: s.PgCluster, tmp_path: Path,
                                                     test_zones: None) -> None:
    """A (v1r1 R4 P08b + Codex E2-C01 correction). The held submit outlives the eligibility
    stop budget -> the existing B3a producer-stop terminal path: fatal_exit(3,
    RUNTIME_STOP_FAILED, eligibility) exactly once (as test_b3a_d3 for write-back). Dispatch
    committed before submit, so exactly ONE durable attempt row remains, with the spy's
    attempt id, in state `dispatched` (the E2 recovery state: after its lease a later run may
    take it over); the verification is not settled and no case event was raised."""
    inst = el_install(pg, tmp_path)
    spies, factory = e4.spy_factory()
    calls: list[tuple[Any, ...]] = []

    async def body() -> None:
        cfg = dataclasses.replace(e4.mod("lifecycle").LifecycleConfig(),
                                  eligibility_stop_timeout_s=0.5)
        lc = e4.lifecycle(inst, profile=e4.ELIGIBILITY, app_name="el4-p08b",
                          clearinghouse_factory=factory, config=cfg,
                          fatal_exit=lambda *a: calls.append(a))
        await lc.start()
        app = e4.mod("app").create_synthetic_app(lifecycle=lc, bind=bb.BIND)
        tok = await e4.token(app)
        await e4.member(app, tok, "SYN-E1-001", "active")
        ref = await booked(inst, app, tok, "E1")
        spy = spies[0]
        spy.hold = asyncio.Event()
        held_tick = asyncio.ensure_future(e4.tick(app, tok))
        await asyncio.wait_for(spy.held.wait(), 30)
        lc.request_stop("TEST")
        await lc.wait_stopped()
        assert len(calls) == 1 and calls[0][0] == 3 and "RUNTIME_STOP_FAILED" in calls[0] \
            and "eligibility" in calls[0], calls
        (vid, _o, state, _c, _r), = e4.verifications(inst, ref)
        assert state != "settled"
        (attempt, n, a_state), = e4.attempts(inst, vid)
        assert (attempt, n, a_state) == (spy.submits[0], 1, "dispatched")
        assert e4.case_events(inst, ref) == []
        spy.hold.set()
        held_tick.cancel()
        with contextlib.suppress(BaseException):
            await held_tick

    run(body())
