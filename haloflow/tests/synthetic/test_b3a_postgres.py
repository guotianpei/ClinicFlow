"""Option B B3a: lifecycle, seal-scope, guard, lease, drain and recovery rows, PostgreSQL 17
(per-case initdb clusters, fresh B2 installs). Class A unless stated (b3a_support docstring):
each row fails in its own body on the missing B3 interface until it exists.

Order oracles combine the lifecycle's test-only hook points (`b3.start.*`, `b3.stop.*`) with
INDEPENDENT observations taken at each point: live scheduler tasks (asyncio), scheduler
states, runtime backends by application_name (pg_stat_activity from an admin connection) and
the exclusive K_S probe (pg_locks). No row trusts the lifecycle's own result alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import b3a_support as b3
import pytest

pytestmark = pytest.mark.postgres

APP = "b3a-row"


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    with s.cluster(tmp_path) as c:
        yield c
    s.reap()


@pytest.fixture
def inst(pg: s.PgCluster, tmp_path: Path) -> Iterator[b3.Install]:
    yield b3.install(pg, tmp_path)
    with contextlib.suppress(ModuleNotFoundError):
        s.mod("hooks").FAULT = None


def lc_mod() -> Any:
    return b3.mod("lifecycle")


def refused_code(fn: Callable[[], Any]) -> str:
    errors = s.mod("errors")
    try:
        fn()
    except errors.SyntheticRefused as e:
        return e.code
    raise AssertionError("not refused")


class Hooks:
    """Test-only FAULT: records every b3.* point with the independent observations taken AT
    that point; optional actions per point (raise / callable)."""

    def __init__(self, inst: b3.Install, lc: Any = None) -> None:
        self.inst, self.lc = inst, lc
        self.seen: list[dict[str, Any]] = []
        self.actions: dict[str, Callable[[dict[str, Any]], None]] = {}
        s.mod("hooks").FAULT = self

    def __call__(self, point: str, info: Any) -> None:
        if not point.startswith("b3."):
            return
        rec = b3.Recorder()
        rec.note(point)
        obs: dict[str, Any] = {"point": point, "live_tasks": rec.events[0][1]}
        if self.lc is not None:
            obs["schedulers"] = {k: str(getattr(v.state, "name", v.state))
                                 for k, v in self.lc.schedulers.items()} \
                if getattr(self.lc, "schedulers", None) else {}
            obs["admission_open"] = bool(getattr(self.lc, "admission_open", False))
        obs["backends"] = len(b3.backends(self.inst, APP))
        self.seen.append(obs)
        act = self.actions.get(point)
        if act is not None:
            act(obs)

    def points(self, prefix: str) -> list[str]:
        return [o["point"] for o in self.seen if o["point"].startswith(prefix)]

    def at(self, point: str) -> dict[str, Any]:
        (o,) = [o for o in self.seen if o["point"] == point]
        return o


def make_lc(inst: b3.Install, **kw: Any) -> Any:
    kw.setdefault("application_name", APP)
    return lc_mod().SyntheticLifecycle(inst.env, **kw)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def start_stop(lc: Any) -> Any:
    await lc.start()
    lc.request_stop("TEST")
    return await lc.wait_stopped()


# =========================================================================== SC: seal scope

def test_b3a_sc1_composed_paths_change_exactly_the_runtime_mutable_tables(
        inst: b3.Install) -> None:
    """A (SC1 on a real B2 install): through the B3a lifecycle (first start, guarded runtime),
    the composed write paths change exactly RUNTIME_MUTABLE_TABLES; nothing install-sealed,
    nothing in `shared`."""
    sc = b3.mod("seal_scope")
    before = b3.table_digests(inst)

    async def body() -> None:
        lc = make_lc(inst)
        await lc.start()
        try:
            await b3_drive(inst, lc)
        finally:
            lc.request_stop("TEST")
            await lc.wait_stopped()

    run(body())
    diff = b3.changed(before, b3.table_digests(inst))
    assert diff == set(sc.RUNTIME_MUTABLE_TABLES), (sorted(diff - sc.RUNTIME_MUTABLE_TABLES),
                                                    sorted(sc.RUNTIME_MUTABLE_TABLES - diff))
    assert not diff & sc.INSTALL_SEALED_TABLES


class _StackView:
    """The S-J flow helpers' `stack` interface over a RUNNING B3a lifecycle: its tenant
    runtime, its sender, its scheduler ticks (test-side ingress and staff calls only)."""

    def __init__(self, inst: b3.Install, lc: Any) -> None:
        self.install = b3.as_si_install(inst)
        self.rt = lc.runtime.tenant_runtime
        self.sender = lc.sender
        self._s = lc.schedulers

    async def tick_reminder(self) -> Any:
        return await asyncio.wait_for(self._s["reminder"].tick(), 60)

    async def tick_writeback(self) -> Any:
        return await asyncio.wait_for(self._s["writeback"].tick(), 60)

    async def tick_all(self) -> dict[str, Any]:
        return {"due": await asyncio.wait_for(self._s["due"].tick(), 60),
                "reminder": await self.tick_reminder(),
                "writeback": await self.tick_writeback()}


async def b3_drive(inst: b3.Install, lc: Any) -> dict[str, Any]:
    """Every composed write path on the RUNNING lifecycle (b3a_support.sj_flow's steps, bound
    to the lifecycle's runtime, sender, EHR port and schedulers): J01, J02 + claim/resolve,
    J03 replies 2/3/NO/STOP, one not-yet-due appointment."""
    import uuid

    import sa_support as sa
    import service_support as sv
    import sh2_support as sh2
    import si_support as si
    import test_sj_postgres as tsj

    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.service import ResolveAction, StaffAction
    from haloflow.m05.writeback import StaffServiceV3

    stack = _StackView(inst, lc)
    ehr = lc.port.ehr
    rec: dict[str, Any] = {}
    f1 = await tsj._sent_and_replied(stack)  # type: ignore[arg-type]
    tsj._put(stack.install, ehr, f1)
    rec["j01"] = tsj._wb_counts(await stack.tick_writeback())
    f2 = await tsj._sent_and_replied(stack)  # type: ignore[arg-type]
    tsj._put(stack.install, ehr, f2, provider_ref="prov-other")
    rec["j02"] = tsj._wb_counts(await stack.tick_writeback())
    (item,) = si.check_items(stack.install)
    iid = uuid.UUID(str(item["item_id"]))
    staff = StaffServiceV3(stack.rt.gateway, foundation=EventFoundation(
        stack.rt.gateway, producer_service=sv.PRODUCER), keys=lc.note_keys)
    st = inst.state
    rec["claim"] = (await staff.act(await b3._staff_ctx(stack.rt, stack.install.tenant_id,
                                                        st["staff_id"]),
                                    StaffAction(item_id=iid, client_action_id=uuid.uuid4(),
                                                expected_version=1, type="claim"))).outcome
    rec["resolve"] = (await staff.act(await b3._staff_ctx(stack.rt, stack.install.tenant_id,
                                                          st["staff_id"]),
                                      ResolveAction(item_id=iid, client_action_id=uuid.uuid4(),
                                                    expected_version=2, type="resolve",
                                                    outcome_code="reached_no_change"))).outcome
    for text in ("2", "3", "NO", "STOP"):
        f = await tsj._sent_and_replied(stack, text)  # type: ignore[arg-type]
        tsj._put(stack.install, ehr, f)
        await stack.tick_all()
    (unclear,) = [r for r in sa.admin(stack.install, sa.q(
        stack.install, "SELECT item_id::text, item_kind FROM {s}.m05_followup_items"
        " WHERE item_kind = 'unclear_reply'"))]
    uid = uuid.UUID(unclear[0])
    await staff.act(await b3._staff_ctx(stack.rt, stack.install.tenant_id, st["staff_id"]),
                    StaffAction(item_id=uid, client_action_id=uuid.uuid4(),
                                expected_version=1, type="claim"))
    rec["other_note"] = (await staff.act(
        await b3._staff_ctx(stack.rt, stack.install.tenant_id, st["staff_id"]),
        ResolveAction(item_id=uid, client_action_id=uuid.uuid4(), expected_version=2,
                      type="resolve", outcome_code="other", note="synthetic note"))).outcome
    now = sh2.db_now(stack.install)
    zone = sh2.zone_with_local_hour(now, 11, 15)
    patient, phone, ref = sa.new_id(), sa.fresh_phone(), sa.new_id()
    await sh2.gateway_directory(stack.rt, stack.install, "contact_put", phone=phone,
                                patient_id=patient)
    later = await sh2.gateway_directory(stack.rt, stack.install, "appointment_put",
                                        **si.appointment3(ref, patient,
                                                          sh2.at_local(now, zone, 10, 18),
                                                          department_tz=zone))
    rec["not_yet_due"] = later["outcome"]
    assert (rec["j01"]["observed_positive"], rec["j02"]["item_raised"], rec["claim"],
            rec["resolve"], rec["other_note"], rec["not_yet_due"]) == (
        1, 1, "applied", "applied", "applied", "applied"), rec
    return rec


def refused_detail(fn: Callable[[], Any]) -> tuple[str, tuple[str, ...]]:
    errors = s.mod("errors")
    try:
        fn()
    except errors.SyntheticRefused as e:
        return e.code, tuple(e.detail)
    raise AssertionError("not refused")


@pytest.mark.parametrize("where", ["{schema}", "shared"])
def test_b3a_sc2b_post_extra_table_refused_by_the_inventory_check(inst: b3.Install,
                                                                  where: str) -> None:
    """A (v1r2 Item 3; Codex red gate 0719bcd item 3; mutant 27): AFTER first start (reduced
    mode) an extra table is refused at app start and at `init` with STATE_DIVERGED detail
    exactly ("inventory",). The inventory equality runs BEFORE the sealed-digest comparison,
    so dropping it (mutant 27) changes the observable detail to the digest's ("profile",)
    even though the table is still refused: the row isolates the inventory check from the
    overlapping digest protection."""
    run(start_stop(make_lc(inst)))
    assert "runtime" in inst.state
    inst.admin(f"CREATE TABLE {where.format(schema=chr(34) + inst.schema + chr(34))}.zz_extra"
               " (x int)")
    want = ("STATE_DIVERGED", ("inventory",))
    assert refused_detail(lambda: run(make_lc(inst).start())) == want
    assert refused_detail(lambda: s.mod("provision").init(inst.env, attest_flag=True)) == want


def test_b3a_sc10_wrong_pinned_digest_enforced_after_first_start(
        inst: b3.Install, monkeypatch: pytest.MonkeyPatch) -> None:
    """A (added at implementation: Codex red gate bb62696, green condition 3). After a VALID
    first start (reduced mode), the reduced seal compares the live sealed digest with the
    PINNED `installed_profile.SEALED_READY_DIGEST`: with the pin altered (one hex digit), app
    start and `init` refuse STATE_DIVERGED ("profile",) on an untampered install; with the
    pin restored both pass again (the refusal comes from the pin, not from the install)."""
    run(start_stop(make_lc(inst)))
    assert "runtime" in inst.state
    prof = s.mod("installed_profile")
    true = prof.SEALED_READY_DIGEST
    wrong = ("0" if true[0] != "0" else "1") + true[1:]
    monkeypatch.setattr(prof, "SEALED_READY_DIGEST", wrong)
    want = ("STATE_DIVERGED", ("profile",))
    assert refused_detail(lambda: run(make_lc(inst).start())) == want
    assert refused_detail(lambda: s.mod("provision").init(inst.env, attest_flag=True)) == want
    monkeypatch.setattr(prof, "SEALED_READY_DIGEST", true)
    assert run(start_stop(make_lc(inst))).disposition == "drained"
    assert s.mod("provision").init(inst.env, attest_flag=True).phase == "ready"


def test_b3a_sc3_installer_accepts_runtime_rows_after_first_start(inst: b3.Install) -> None:
    """A: after first start and legitimate runtime activity, `status` attests and an `init`
    re-run returns `ready`; the marker is unchanged."""
    lc_mod()  # absence first (red rule): the row fails in its own body
    run(start_stop(make_lc(inst)))
    marker = inst.state["runtime"]
    b3.sj_flow(inst)
    st = s.mod("provision").status(inst.env)
    assert (st["sealed"], st["attested"]) == (True, True)
    assert s.mod("provision").init(inst.env, attest_flag=True).phase == "ready"
    assert inst.state["runtime"] == marker


def test_b3a_sc3b_first_start_of_the_new_profile_writes_marker_before_pool(
        inst: b3.Install) -> None:
    """A (mutants 12/29; renamed per Codex red gate 0719bcd item 1): a ready install made by
    the CURRENT (amended) installer has no marker; its first start checks the full seal and
    writes the marker BEFORE the pool opens (no runtime backend exists at marker.after). A
    genuine e05bc51 install is NOT startable at all (FR-L1b)."""
    assert "runtime" not in inst.state
    lc = make_lc(inst)
    h = Hooks(inst, lc)
    run(start_stop(lc))
    order = h.points("b3.start.")
    assert order.index("b3.start.a1") < order.index("b3.start.marker.after") \
        < order.index("b3.start.pool")
    assert h.at("b3.start.marker.after")["backends"] == 0
    assert inst.state["runtime"]["first_epoch"]


TAMPER: dict[str, str] = {
    "staff": 'UPDATE "{s}".m01_staff SET display_name = \'Someone Else\'',
    "settings": 'UPDATE "{s}".m05_reminder_settings SET clinic_display_name = \'Other\'',
    "function": ('CREATE OR REPLACE FUNCTION "{s}".m05_followup_reason(p_command jsonb)'
                 " RETURNS jsonb LANGUAGE sql AS $$ SELECT '{{}}'::jsonb $$"),
    "runtime_row": ('INSERT INTO "{s}".m05_patient_contacts SELECT * FROM'
                    ' "{s}".m05_patient_contacts LIMIT 0'),  # replaced at implementation
}


@pytest.mark.parametrize("kind", ["staff", "settings", "function"])
def test_b3a_sc4abc_pre_first_start_sealed_tamper_refused(inst: b3.Install, kind: str) -> None:
    """A: before first start (no marker) a sealed tamper refuses app start with STATE_DIVERGED;
    no pool opened (no runtime backend ever seen), no marker written, no change."""
    inst.admin(TAMPER[kind].format(s=inst.schema))
    before = s.snapshot(inst.c)
    lc = make_lc(inst)
    h = Hooks(inst, lc)
    assert refused_code(lambda: run(lc.start())) == "STATE_DIVERGED"
    assert "b3.start.pool" not in h.points("b3.start.")
    assert "runtime" not in inst.state
    assert s.diff(before, s.snapshot(inst.c)) == {}


def test_b3a_sc4d_pre_first_start_runtime_row_tamper_refused(inst: b3.Install) -> None:
    """A: before first start the FULL seal governs, so even a runtime-mutable row change
    (an admin-inserted contact) refuses start."""
    lc_mod()  # absence first (red rule): the row fails in its own body
    b3.sj_flow(inst)  # rows in runtime-mutable tables, no marker (full mode)
    assert "runtime" not in inst.state
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_DIVERGED"


@pytest.mark.parametrize("kind", ["staff", "settings", "function"])
def test_b3a_sc4efg_post_first_start_sealed_tamper_refused(inst: b3.Install, kind: str) -> None:
    """A: after first start, the same sealed tampers refuse app start and `init`. For the
    function, the OID is unchanged (C6), so the semantic digest must catch it."""
    run(start_stop(make_lc(inst)))
    inst.admin(TAMPER[kind].format(s=inst.schema))
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_DIVERGED"
    assert refused_code(lambda: s.mod("provision").init(inst.env, attest_flag=True)) \
        == "STATE_DIVERGED"


def test_b3a_sc5_runtime_table_definition_change_refused(inst: b3.Install) -> None:
    """A (mutant 15): after first start, adding a column to a runtime-mutable table diverges."""
    run(start_stop(make_lc(inst)))
    inst.admin(f'ALTER TABLE "{inst.schema}".m05_outbound_ops ADD COLUMN zz int')
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_DIVERGED"


def test_b3a_sc6_sequence_values_ignored_ownership_checked(inst: b3.Install) -> None:
    """A: advanced sequence values are not divergence; dropping the sequence's ownership is.
    Corrected at implementation (measured; Claude B3a green packet): the tenant schema's only
    owned sequence is the IDENTITY sequence of the runtime-mutable `patient_events`
    (deptype 'i'), which the runtime advances; `ALTER SEQUENCE ... OWNED BY NONE` is refused
    by PostgreSQL for identity sequences (FeatureNotSupported), so the ownership leg drops the
    identity (`ALTER COLUMN ... DROP IDENTITY`) for 'i' and keeps OWNED BY NONE for 'a'."""
    run(start_stop(make_lc(inst)))
    seqs = inst.admin("SELECT n.nspname, c.relname, t.relname, a.attname, d.deptype"
                      " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                      " JOIN pg_depend d ON d.objid = c.oid AND d.deptype IN ('a', 'i')"
                      " JOIN pg_class t ON t.oid = d.refobjid JOIN pg_attribute a"
                      " ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid"
                      " WHERE c.relkind = 'S' AND n.nspname = %s", (inst.schema,))
    assert seqs, "no owned sequence in the tenant schema to exercise"
    sch, seq, table, column, deptype = seqs[0]
    inst.admin(f'SELECT nextval(\'"{sch}"."{seq}"\')')
    run(start_stop(make_lc(inst)))
    if deptype == "a":
        inst.admin(f'ALTER SEQUENCE "{sch}"."{seq}" OWNED BY NONE')
    else:
        inst.admin(f'ALTER TABLE "{sch}"."{table}" ALTER COLUMN "{column}" DROP IDENTITY')
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_DIVERGED"


def test_b3a_sc7_isolation_alert_persists_stops_and_refuses_next_start(
        inst: b3.Install) -> None:
    """A (Q-T1): an isolation alert row written while the app runs persists, the runtime
    stops through the coordinator, and the next start refuses without erasing it."""
    async def body() -> Any:
        lc = make_lc(inst)
        await lc.start()
        inst.admin("INSERT INTO shared.isolation_alerts (alert_id, source_code, alert_type,"
                   " severity) VALUES (pg_catalog.gen_random_uuid(), 'b3a_sc7',"
                   " 'synthetic_fault', 1)")  # fault fixture (NOT NULL columns: corrected)
        lc.request_stop("ISOLATION_ALERT")
        return await lc.wait_stopped()

    result = run(body())
    assert result.disposition in ("drained", "terminal")
    count = inst.admin("SELECT count(*) FROM shared.isolation_alerts")[0][0]
    assert count >= 1
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_DIVERGED"
    assert inst.admin("SELECT count(*) FROM shared.isolation_alerts")[0][0] == count


@pytest.mark.parametrize("bad", [{"first_epoch": 5}, {"first_epoch": ""}, {"x": "y"}])
def test_b3a_sc8_malformed_marker_corrupt(inst: b3.Install, bad: dict[str, Any]) -> None:
    """A: a malformed `runtime` field is STATE_CORRUPT (app start and installer)."""
    import json

    path = inst.st_dir / "state.json"
    st = json.loads(path.read_text())
    st["runtime"] = bad
    path.write_text(json.dumps(st))
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_CORRUPT"


# =========================================================================== SC9: marker

def test_b3a_sc9a_failure_before_marker_leaves_state(inst: b3.Install) -> None:
    """A: a fault at b3.start.a1 leaves the state byte-identical; next start succeeds."""
    raw = (inst.st_dir / "state.json").read_bytes()
    lc = make_lc(inst)
    h = Hooks(inst, lc)

    def boom(_o: dict[str, Any]) -> None:
        raise RuntimeError("b3a fault")

    h.actions["b3.start.a1"] = boom
    with pytest.raises(Exception):  # noqa: B017,PT011 - the start fails; class name only
        run(lc.start())
    assert (inst.st_dir / "state.json").read_bytes() == raw
    s.mod("hooks").FAULT = None
    assert run(start_stop(make_lc(inst))).disposition == "drained"


def test_b3a_sc9b_crash_inside_marker_write_keeps_old_state(inst: b3.Install,
                                                            tmp_path: Path) -> None:
    """A: os._exit at state.after_temp_fsync during the marker write: the state parses, has no
    marker, is not corrupt; the next start uses the full seal and succeeds."""
    lc_mod()  # absence first (red rule): the row fails in its own body
    child = b3.spawn(inst, tmp_path / "bar", "lifecycle", app=APP,
                     crash="state.after_temp_fsync")
    rc, out = child.wait(120)
    assert rc == 97, out[-1500:]
    b3.assert_gone(inst, child)
    assert "runtime" not in inst.state
    assert run(start_stop(make_lc(inst))).disposition == "drained"


@pytest.mark.parametrize("point", ["b3.start.marker.after", "b3.start.schedulers"])
def test_b3a_sc9cd_crash_after_marker_reduced_mode_next(inst: b3.Install, tmp_path: Path,
                                                         point: str) -> None:
    """A (mutant 28): a crash after the marker (before or after the pool opens, before
    admission) keeps the marker; the next start uses the reduced seal: a runtime-row change is
    tolerated, a sealed change is refused."""
    lc_mod()  # absence first (red rule): the row fails in its own body
    child = b3.spawn(inst, tmp_path / "bar", "lifecycle", app=APP, crash=point)
    rc, out = child.wait(120)
    assert rc == 97, out[-1500:]
    b3.assert_gone(inst, child)
    assert inst.state["runtime"]["first_epoch"]
    b3.sj_flow(inst)
    assert run(start_stop(make_lc(inst))).disposition == "drained"
    inst.admin(TAMPER["staff"].format(s=inst.schema))
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_DIVERGED"


def test_b3a_sc9e_hand_removed_marker_fails_closed(inst: b3.Install) -> None:
    """A: marker removed by hand after runtime use -> full seal -> STATE_DIVERGED (no silent
    downgrade)."""
    lc_mod()  # absence first (red rule): the row fails in its own body
    import json

    run(start_stop(make_lc(inst)))
    b3.sj_flow(inst)
    path = inst.st_dir / "state.json"
    st = json.loads(path.read_text())
    del st["runtime"]
    path.write_text(json.dumps(st))
    assert refused_code(lambda: run(make_lc(inst).start())) == "STATE_DIVERGED"


# =========================================================================== S: startup

@pytest.mark.parametrize("case", ["production", "no_disposable", "pg_env", "dsn_key",
                                  "state_missing", "phase_not_ready"])
def test_b3a_s1_a0_refusals_open_no_connection(inst: b3.Install, case: str,
                                               tmp_path: Path) -> None:
    """A: each A0 refusal happens before any connection (no backend with the app name ever
    appears), with the B2 closed code, and changes nothing."""
    env = dict(inst.env)
    expect = {"production": "DEPLOYMENT_PRODUCTION", "no_disposable": "ATTESTATION_MISSING",
              "pg_env": "DSN_ENV_OVERRIDE", "dsn_key": "DSN_KEY_FORBIDDEN",
              "state_missing": "STATE_MISSING", "phase_not_ready": "STATE_CORRUPT"}[case]
    if case == "production":
        env["HALOFLOW_PLATFORM_DEPLOYMENT"] = "production"
    elif case == "no_disposable":
        env.pop("HALOFLOW_SYNTHETIC_DISPOSABLE")
    elif case == "pg_env":
        env["PGOPTIONS"] = "-c x=y"
    elif case == "dsn_key":
        env["HALOFLOW_SYNTHETIC_ADMIN_DSN"] += " options=-cx=y"
    elif case == "state_missing":
        env["HALOFLOW_SYNTHETIC_STATE"] = str(tmp_path / "nowhere")
    elif case == "phase_not_ready":
        import json

        p = inst.st_dir / "state.json"
        st = json.loads(p.read_text())
        st["phase"] = "staff"
        p.write_text(json.dumps(st))
    before = s.snapshot(inst.c)
    lc = lc_mod().SyntheticLifecycle(env, application_name=APP)
    assert refused_code(lambda: run(lc.start())) == expect
    assert b3.backends(inst, APP) == []
    assert s.diff(before, s.snapshot(inst.c)) == {}


def test_b3a_s2_installer_holding_ks_refuses_start(inst: b3.Install) -> None:
    """A: INSTALL_LOCKED while an installer holds K_S; no pool."""
    with b3.held_install_lock(inst):
        lc = make_lc(inst)
        h = Hooks(inst, lc)
        assert refused_code(lambda: run(lc.start())) == "INSTALL_LOCKED"
        assert "b3.start.pool" not in h.points("b3.start.")


def test_b3a_s3_epoch_superseded_refuses_before_pool(inst: b3.Install) -> None:
    """A: the reservation epoch changed by a completed installer run after the state read ->
    EPOCH_SUPERSEDED; no pool; the lease released."""
    lc = make_lc(inst)
    h = Hooks(inst, lc)
    h.actions["b3.start.lease"] = lambda _o: s.mod("provision").init  # noqa: ARG005
    # a real installer run cannot interleave (K_S); the takeover helper bumps the epoch:
    def bump(_o: dict[str, Any]) -> None:
        st = inst.state
        guard = s.mod("guard")
        ep = s.mod("dsn").parse_admin_dsn(inst.env["HALOFLOW_SYNTHETIC_ADMIN_DSN"], {})
        guard._takeover(ep, st["nonce"], guard.new_epoch())

    h.actions["b3.start.lease"] = bump
    assert refused_code(lambda: run(lc.start())) == "EPOCH_SUPERSEDED"
    assert "b3.start.pool" not in h.points("b3.start.")
    assert b3.ks_free(inst)


def test_b3a_s3b_state_of_another_cluster_refused(inst: b3.Install, tmp_path: Path) -> None:
    """A: a state file copied to another ready cluster -> CLUSTER_MISMATCH / TARGET_MISMATCH."""
    lc_mod()  # absence first (red rule): the row fails in its own body
    (tmp_path / "o").mkdir()
    with s.cluster(tmp_path / "other") as c2:
        other = b3.install(c2, tmp_path / "o")
        env = dict(other.env)
        env["HALOFLOW_SYNTHETIC_STATE"] = str(inst.st_dir)
        lc = lc_mod().SyntheticLifecycle(env, application_name=APP)
        assert refused_code(lambda: run(lc.start())) in ("CLUSTER_MISMATCH", "TARGET_MISMATCH")


def test_b3a_s4_s5_admits_at_94_and_starts_in_order(inst: b3.Install) -> None:
    """A: the tenant is admitted at 94; schedulers start due -> reminder -> write-back, then
    the watcher; health is 200 RUNNING with simulated true."""
    async def body() -> tuple[Any, Any]:
        lc = make_lc(inst)
        h = Hooks(inst, lc)
        await lc.start()
        app = b3.mod("app").create_synthetic_app(lifecycle=lc)
        import sj_harness as sj

        status, out = await sj.http(app, "GET", "/synthetic/health")
        lc.request_stop("TEST")
        await lc.wait_stopped()
        return h, (status, out)

    h, (status, out) = run(body())
    order = h.points("b3.start.")
    assert order[-2:] == ["b3.start.schedulers", "b3.start.watcher"]
    assert (status, out) == (200, {"simulated": True, "state": "RUNNING"})


@pytest.mark.parametrize("step", ["lease", "a1", "marker.after", "pool", "runtime", "wiring",
                                  "schedulers", "watcher"])
def test_b3a_s6_startup_failure_reverse_close(inst: b3.Install, step: str) -> None:
    """A: a fault at each b3.start.<step> closes only what was opened, in reverse order; state
    FAILED; class-name-only error; lease released (K_S free); no runtime backend left.
    Strengthened at implementation (v1r2 D1.3c, mutant 28): a failure AFTER the marker write
    keeps the marker (one-way authorization); a failure before it leaves no marker."""
    lc = make_lc(inst)
    h = Hooks(inst, lc)

    def boom(_o: dict[str, Any]) -> None:
        raise RuntimeError("b3a fault with secret-looking text CANARY")

    h.actions[f"b3.start.{step}"] = boom
    with pytest.raises(Exception) as ei:  # noqa: PT011
        run(lc.start())
    assert "CANARY" not in str(ei.value)
    assert lc.state == "FAILED"
    b3.wait_until(lambda: b3.backends(inst, APP) == [], 10)
    assert b3.ks_free(inst)
    assert ("runtime" in inst.state) == (step not in ("lease", "a1"))


@pytest.mark.parametrize("release", ["within_bound", "after_start_failed"])
def test_b3a_s8_cancel_during_lease_acquisition_strands_no_lease(inst: b3.Install,
                                                                  release: str) -> None:
    """A (added: Codex review 85d836a, blocking). Startup is cancelled while the lease thread
    HOLDS K_S but has not yet published the lease (barrier at `b3.lease.acquired`). The
    acquisition is released (a) within the abort bound, or (b) only after `start` has
    already failed. Either way, with the host process still alive, the lease backend goes
    away and the EXCLUSIVE K_S becomes available; `start` re-raises the cancellation."""
    import threading

    reached, gate = threading.Event(), threading.Event()

    def fault(point: str, _info: Any) -> None:
        if point == "b3.lease.acquired":
            reached.set()
            gate.wait(60)

    s.mod("hooks").FAULT = fault

    async def body() -> str:
        lc = make_lc(inst)
        task = asyncio.create_task(lc.start())
        assert await asyncio.to_thread(reached.wait, 60)
        assert b3.backends(inst, f"{APP}/lease") and not b3.ks_free(inst)
        if release == "within_bound":
            threading.Timer(0.5, gate.set).start()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.to_thread(
            b3.wait_until, lambda: b3.backends(inst, f"{APP}/lease") == [] and b3.ks_free(inst),
            30)
        return str(lc.state)

    try:
        assert run(body()) == "FAILED"
    finally:
        gate.set()
        s.mod("hooks").FAULT = None


def test_b3a_s7_second_start_refused_without_effect(inst: b3.Install) -> None:
    """A: start twice on one lifecycle -> the second start has no effect."""
    async def body() -> None:
        lc = make_lc(inst)
        await lc.start()
        n = len(b3.backends(inst, APP))
        with pytest.raises(Exception):  # noqa: B017,PT011
            await lc.start()
        assert len(b3.backends(inst, APP)) == n
        lc.request_stop("TEST")
        await lc.wait_stopped()

    run(body())


# =========================================================================== G: guard

def test_b3a_g1_every_runtime_transaction_is_guarded(inst: b3.Install) -> None:
    """A (mutants 3/4/8): over a lifecycle run with work and a forced abort, every runtime
    transaction that runs a non-allowlisted statement starts with the five F2 statements (shared
    lock), every statement outside a transaction is allowlisted, and no runtime backend ever
    takes the exclusive K_X."""
    lc_mod()  # absence first (red rule): the row fails in its own body
    b3.trace_on(inst)
    guard = s.mod("guard")
    async def body() -> None:
        lc = make_lc(inst)
        await lc.start()
        for name in ("due", "reminder", "writeback"):
            await asyncio.wait_for(lc.schedulers[name].tick(), 60)
        lc.request_stop("TEST")
        await lc.wait_stopped()

    # Corrected at implementation: runtime rows before the FIRST start are refused by the full
    # seal (SC4d), so the install is first started once (reduced mode), then given work.
    run(start_stop(make_lc(inst)))
    b3.sj_flow(inst)  # pending rows for the lifecycle's ticks
    run(body())
    # Corrected at implementation: the prefix trace also matches the lease connection
    # (`<app>/lease`, an ADMIN session holding K_S, not a runtime pool connection); it is
    # excluded here, and it must not run any guarded transaction.
    lease = b3.trace(inst, f"{APP}/lease")
    per = {pid: stmts for pid, stmts in b3.trace(inst, APP).items() if pid not in lease}
    assert per, "no runtime backend traced"
    assert lease and not any(b3.transactions(st)[0] for st in lease.values()), lease
    # Corrected at implementation: the server log shows the SERVER form of a statement
    # (whitespace-joined, `$n` placeholders); the allowlist holds the client text. Compare
    # both normalised.
    import re

    def server_form(q: str) -> str:
        n = iter(range(1, 100))
        return re.sub(r"\s+", " ", re.sub(r"%s", lambda _m: f"${next(n)}", q)).strip()

    allowed = {server_form(x) for x in guard.ALLOWLIST}
    for pid, stmts in per.items():
        txs, outside = b3.transactions(stmts)
        bad_outside = [x for x in outside if server_form(x) not in allowed]
        assert bad_outside == [], (pid, bad_outside[:3])
        for tx in txs:
            if all(server_form(x) in allowed for x in tx):
                continue
            assert all(rx.search(st) for rx, st in zip(b3.F2_PREFIX, tx[:5], strict=False)) \
                and len(tx) >= 5, (pid, tx[:6])
        assert not any("pg_advisory_xact_lock(" in x for x in stmts), pid


@pytest.mark.parametrize("case", ["execute_insert", "begin", "cursor", "executemany", "copy",
                                  "set_off"])
def test_b3a_g2_unguarded_statements_refused_client_side(inst: b3.Install, case: str) -> None:
    """A (mutants 5/6): through a runtime pool connection, each unguarded form is refused with
    UNGUARDED_STATEMENT and never reaches the server (server log)."""
    b3.trace_on(inst)
    guard = s.mod("guard")
    errors = s.mod("errors")

    async def body() -> str:
        from haloflow.m01.pool import TenantPool

        pool = TenantPool(b3.runtime_conninfo(inst, application_name=APP),
                          connection_class=guard.GuardedRuntimeConnection)
        await pool.open()
        try:
            async with pool._connection_for_control() as conn:
                conn.expected = s.slot(inst.c)
                marker = f"zz_g2_{case}"
                try:
                    if case == "execute_insert":
                        await conn.execute(f"SELECT 1 AS {marker}")
                    elif case == "begin":
                        await conn.execute(f"BEGIN /* {marker} */")
                    elif case == "cursor":
                        await conn.cursor().execute(f"SELECT 1 AS {marker}")
                    elif case == "executemany":
                        await conn.cursor().executemany(f"SELECT %s AS {marker}", [(1,)])
                    elif case == "copy":
                        async with conn.cursor().copy(f"COPY (SELECT 1 AS {marker}) TO STDOUT"):
                            pass
                    else:
                        await conn.execute(
                            f"SET default_transaction_read_only = off /* {marker} */")
                    return "EXECUTED"
                except errors.SyntheticRefused as e:
                    return e.code
        finally:
            await pool.close()

    assert run(body()) == "UNGUARDED_STATEMENT"
    time.sleep(0.3)
    assert not any(f"zz_g2_{case}" in x for st in b3.trace(inst, APP).values() for x in st)


def test_b3a_g3_forced_epoch_bump_fences_and_stops(inst: b3.Install) -> None:
    """A (Q-T3 synthetic fault): an epoch bump while running fences the next runtime commit
    (EPOCH_SUPERSEDED) and the coordinator stops the app (lease_lost or guard reason)."""
    async def body() -> Any:
        lc = make_lc(inst)
        await lc.start()
        guard = s.mod("guard")
        ep = s.mod("dsn").parse_admin_dsn(inst.env["HALOFLOW_SYNTHETIC_ADMIN_DSN"], {})
        await asyncio.to_thread(guard._takeover, ep, inst.state["nonce"], guard.new_epoch())
        await asyncio.wait_for(lc.schedulers["reminder"].tick(), 60)
        return await asyncio.wait_for(lc.wait_stopped(), 60)

    result = run(body())
    assert result.reason in ("GUARD_REFUSED", "EPOCH_SUPERSEDED")


def test_b3a_g5_read_only_reapplied_after_reset(inst: b3.Install) -> None:
    """A (mutant 7): after a pool return (`DISCARD ALL` reset) the connection is read-only
    again; with the cursor fence disabled (test subclass) the server still refuses a write."""
    guard = s.mod("guard")

    async def body() -> tuple[str, str]:
        from haloflow.m01.pool import TenantPool

        class NoFence(guard.GuardedRuntimeConnection):  # type: ignore[misc,name-defined]
            def _fenced(self, *_a: Any) -> bool:   # the fence's single decision point
                return False

        pool = TenantPool(b3.runtime_conninfo(inst, application_name=APP), min_size=1,
                          max_size=1, connection_class=NoFence)
        await pool.open()
        try:
            async with pool._connection_for_control() as conn:
                conn.expected = s.slot(inst.c)
            async with pool._connection_for_control() as conn:
                ro = (await (await conn.execute("SHOW default_transaction_read_only"))
                      .fetchone())[0]
                try:
                    await conn.execute("CREATE TABLE zz_g5 (x int)")
                    out = "EXECUTED"
                except Exception as e:  # noqa: BLE001
                    out = type(e).__name__
            return ro, out
        finally:
            await pool.close()

    ro, out = run(body())
    assert ro == "on" and out != "EXECUTED", (ro, out)


def test_b3a_g6_runtime_never_migrates_or_provisions(inst: b3.Install) -> None:
    """A (mutant 9): the runtime's migrator/provisioner factories refuse RUNTIME_NO_DDL and
    open no connection."""
    async def body() -> tuple[str, str]:
        lc = make_lc(inst)
        await lc.start()
        try:
            deps = lc.runtime.dependencies
            codes = []
            for f in (deps.migrator_connect, deps.provisioner_connect):
                try:
                    await f()
                    codes.append("CONNECTED")
                except s.mod("errors").SyntheticRefused as e:
                    codes.append(e.code)
            return tuple(codes)  # type: ignore[return-value]
        finally:
            lc.request_stop("TEST")
            await lc.wait_stopped()

    assert run(body()) == ("RUNTIME_NO_DDL", "RUNTIME_NO_DDL")


def test_b3a_g7_real_lease_loss_takeover_race(inst: b3.Install, tmp_path: Path) -> None:
    """A (Q-T3 real race; Codex finding 2). The lifecycle runs in a child with the watcher
    slowed (lease_check_s = 30) and two reminder ticks scripted.
    (1) The first runtime write transaction after start pauses right after its guard (file
    barrier at `guarded_txn`, armed only after start). (2) The parent terminates the lease
    backend (application_name "<app>/lease"). (3) A real `init` re-run in a second child takes
    K_S and reaches its takeover. (4) The takeover WAITS on the advisory lock (pg_locks
    granted = false) while the runtime transaction is paused. (5) Released, the runtime
    transaction commits. (6) The takeover commits (`init` returns `ready`). (7) The second
    tick's runtime transaction is refused EPOCH_SUPERSEDED, and no watcher check ran."""
    lc_mod()
    run(start_stop(make_lc(inst)))  # first start (corrected at implementation: see G1)
    b3.sj_flow(inst)  # pending reminder work so the first tick writes
    bar = tmp_path / "bar"
    g = b3.spawn(inst, bar, "lifecycle", app=APP,
                 extra={"pause_after_start": "guarded_txn", "lease_check_s": 30,
                        "ticks": ["reminder", "reminder"], "tick_codes": True})
    g.reached("guarded_txn", 120)                                                   # (1)
    (lease_pid,) = b3.backends(inst, f"{APP}/lease")
    assert s.terminate_backend(inst.c, lease_pid)                                  # (2)
    init_child = s.spawn(tmp_path / "bar-init", "init", environ=inst.env)          # (3)
    b3.wait_until(lambda: s.waiting_advisory(inst.c), 60)                          # (4)
    g.go("guarded_txn")                                                            # (5)
    assert init_child.result(120) == {"ok": True, "phase": "ready"}                # (6)
    (bar / "stop").touch()
    rc, out = g.wait(120)
    line = [ln for ln in out.splitlines() if ln.startswith("B3CHILD ")][-1]
    import json

    got = json.loads(line[len("B3CHILD "):])
    assert got["tick_codes"][0] is None, got                    # the paused one committed
    assert got["tick_codes"][1] == "EPOCH_SUPERSEDED", got      # (7)
    assert got["watcher_checks_delta"] == 0, got


# =========================================================================== L: lease

def test_b3a_l1_lease_backend_killed_stops_and_503(inst: b3.Install) -> None:
    """A: pg_terminate_backend(lease pid) -> within lease_check_s + 2 s every producer is
    stopped, port/pool closed, health 503 RUNTIME_LEASE_LOST; no further runtime commit."""
    async def body() -> tuple[float, Any, Any]:
        cfg = lc_mod().LifecycleConfig(lease_check_s=1)
        lc = make_lc(inst, config=cfg)
        await lc.start()
        app = b3.mod("app").create_synthetic_app(lifecycle=lc)
        t0 = time.monotonic()
        assert s.terminate_backend(inst.c, lc.runtime.lease.pid)
        result = await asyncio.wait_for(lc.wait_stopped(), cfg.lease_check_s + cfg.t_total)
        import sj_harness as sj

        return time.monotonic() - t0, result, await sj.http(app, "GET", "/synthetic/health")

    elapsed, result, (status, out) = run(body())
    assert result.disposition in ("lease_lost_drained", "terminal")
    assert result.reason == "RUNTIME_LEASE_LOST"
    assert (status, out.get("refused")) == (503, "RUNTIME_LEASE_LOST")
    assert b3.backends(inst, APP) == []


def test_b3a_l2_guard_refusal_in_tick_requests_one_stop(inst: b3.Install) -> None:
    """A (mutant 9 of v1): a guard refusal inside a tick requests exactly one stop, no retry."""
    test_b3a_g3_forced_epoch_bump_fences_and_stops(inst)


# =========================================================================== D: drain

def test_b3a_d1_normal_stop_order_quiescence_and_admission(inst: b3.Install) -> None:
    """A (mutants 17/22/23/33): order watcher, producers, port, pool, quiesce, lease; at the
    port and pool steps no scheduler task is live and all are STOPPED; at the lease step no
    runtime backend exists; admission closed (503) from request_stop onward; drained."""
    async def body() -> tuple[Hooks, Any, list[int]]:
        lc = make_lc(inst)
        h = Hooks(inst, lc)
        await lc.start()
        app = b3.mod("app").create_synthetic_app(lifecycle=lc)
        import sj_harness as sj

        probes: list[int] = []

        async def probe() -> None:
            while lc.state not in ("STOPPED", "STOP_FAILED", "FAILED"):
                probes.append((await sj.http(app, "GET", "/synthetic/health"))[0])
                await asyncio.sleep(0.01)

        task = asyncio.create_task(probe())
        await asyncio.sleep(0.05)
        lc.request_stop("TEST")
        assert lc.admission_open is False  # synchronous close at the request
        result = await lc.wait_stopped()
        await task
        return h, result, probes

    h, result, probes = run(body())
    assert h.points("b3.stop.") == ["b3.stop.watcher", "b3.stop.producers", "b3.stop.port",
                                    "b3.stop.pool", "b3.stop.quiesce", "b3.stop.lease"]
    for p in ("b3.stop.port", "b3.stop.pool"):
        o = h.at(p)
        assert o["live_tasks"] == 0 and set(o["schedulers"].values()) == {"STOPPED"}, o
    assert h.at("b3.stop.lease")["backends"] == 0
    assert result.disposition == "drained"
    first_503 = next((i for i, v in enumerate(probes) if v == 503), None)
    assert first_503 is not None and all(v == 503 for v in probes[first_503:])


def test_b3a_d3_terminal_stop_failure_in_process(inst: b3.Install) -> None:
    """A (mutant 19): the write-back stop never completes -> fatal_exit(3, RUNTIME_STOP_FAILED)
    exactly once, within the attempt-2 end and the deadline + 0.5 s; port/pool/lease untouched
    (no explicit lease release on the terminal path: K_S still held)."""
    calls: list[tuple[Any, ...]] = []

    async def body() -> tuple[float, Any]:
        lc = make_lc(inst, fatal_exit=lambda *a: calls.append(a))
        await lc.start()

        async def never() -> None:
            await asyncio.sleep(3600)

        lc.schedulers["writeback"].stop = never  # type: ignore[method-assign]
        t0 = time.monotonic()
        lc.request_stop("TEST")
        await lc.wait_stopped()
        return time.monotonic() - t0, lc

    elapsed, lc = run(body())
    assert len(calls) == 1 and calls[0][0] == 3 and "RUNTIME_STOP_FAILED" in calls[0], calls
    assert elapsed <= lc.config.t_total + 0.5
    assert not b3.ks_free(inst), "the terminal path must not release the lease explicitly"


def test_b3a_d3s_terminal_failure_through_real_serve(inst: b3.Install, tmp_path: Path) -> None:
    """A (Codex finding 3): through `python -m haloflow.synthetic serve` in a child, a stop that
    never completes ends with process exit 3 within T_total + 1.5 s of SIGTERM, one fixed
    stderr line, then (eventually) no backend and K_S free."""
    lc_mod()
    child = b3.spawn(inst, tmp_path / "bar", "serve", app=APP,
                     extra={"args": ["--port", "0"], "hang_stop": "writeback"})
    child.seen("b3.start.watcher", 120)
    t0 = time.monotonic()
    child.proc.send_signal(signal.SIGTERM)
    rc, out = child.wait(120)
    elapsed = time.monotonic() - t0
    assert rc == 3, out[-1500:]
    assert elapsed <= lc_mod().LifecycleConfig().t_total + 1.5
    assert [ln for ln in out.splitlines() if ln.startswith("fatal: ")] == \
        ["fatal: RUNTIME_STOP_FAILED writeback"]
    b3.assert_gone(inst, child)


@pytest.mark.parametrize("mode", ["hang", "resist"])
def test_b3a_d7_pool_close_unconfirmed_goes_terminal_lease_kept(inst: b3.Install,
                                                               mode: str) -> None:
    """A (mutants 33/34): the pool close hangs or resists cancellation -> terminal
    POOL_NOT_QUIESCENT or DRAIN_DEADLINE within its budget + 0.5 s; the coordinator does not
    release the lease (K_S still held)."""
    calls: list[tuple[Any, ...]] = []

    async def body() -> tuple[float, Any]:
        lc = make_lc(inst, fatal_exit=lambda *a: calls.append(a))
        await lc.start()
        pool = lc.runtime.pool

        async def bad_close() -> None:
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if mode == "hang":
                        raise
        pool.close = bad_close  # type: ignore[method-assign]
        t0 = time.monotonic()
        lc.request_stop("TEST")
        await lc.wait_stopped()
        return time.monotonic() - t0, lc

    elapsed, lc = run(body())
    assert len(calls) == 1 and set(calls[0]) & {"POOL_NOT_QUIESCENT", "DRAIN_DEADLINE"}, calls
    assert elapsed <= lc.config.t_total + 0.5
    assert not b3.ks_free(inst)


def test_b3a_d10_blocked_event_loop_watchdog_exit(inst: b3.Install, tmp_path: Path) -> None:
    """A (mutant 31): a step blocking the event loop (synchronous sleep in the port close) in a
    child -> the watchdog thread exits the process with 3 / DRAIN_DEADLINE by T_total + 2 s."""
    lc_mod()
    child = b3.spawn(inst, tmp_path / "bar", "lifecycle", app=APP,
                     extra={"block_at": "b3.stop.port", "block_s": 3600})
    child.seen("b3.start.watcher", 120)
    (tmp_path / "bar" / "stop").touch()
    t0 = time.monotonic()
    rc, out = child.wait(120)
    assert rc == 3 and "fatal: DRAIN_DEADLINE" in out, out[-1500:]
    assert time.monotonic() - t0 <= lc_mod().LifecycleConfig().t_total + 2.0
    b3.assert_gone(inst, child)


def test_b3a_d11_watcher_stop_hang_terminal(inst: b3.Install) -> None:
    """A (mutant 32): a watcher that does not stop -> WATCHER_STOP_FAILED terminal within
    t_watcher + 0.5 s; no producer, port, pool or lease step after it."""
    calls: list[tuple[Any, ...]] = []

    async def body() -> tuple[float, Hooks, Any]:
        lc = make_lc(inst, fatal_exit=lambda *a: calls.append(a))
        h = Hooks(inst, lc)
        await lc.start()
        lc._watcher_stop = lambda: asyncio.sleep(3600)  # type: ignore[attr-defined]
        t0 = time.monotonic()
        lc.request_stop("TEST")
        await lc.wait_stopped()
        return time.monotonic() - t0, h, lc

    elapsed, h, lc = run(body())
    assert len(calls) == 1 and "WATCHER_STOP_FAILED" in calls[0], calls
    assert elapsed <= lc.config.t_watcher + 0.5
    assert h.points("b3.stop.") == ["b3.stop.watcher"]


def test_b3a_d12_non_quiescent_backend_terminal(inst: b3.Install) -> None:
    """A (mutant 33): a backend still carrying the lifecycle's application_name at the
    quiescence check -> POOL_NOT_QUIESCENT terminal; lease not released."""
    import psycopg

    calls: list[tuple[Any, ...]] = []

    async def body() -> None:
        lc = make_lc(inst, fatal_exit=lambda *a: calls.append(a))
        await lc.start()
        extra = psycopg.connect(b3.runtime_conninfo(inst, application_name=APP),
                                autocommit=True)
        try:
            lc.request_stop("TEST")
            await lc.wait_stopped()
        finally:
            extra.close()

    run(body())
    assert len(calls) == 1 and "POOL_NOT_QUIESCENT" in calls[0], calls


def test_b3a_d4_simultaneous_requests_one_sequence(inst: b3.Install) -> None:
    """A: lease kill + guard refusal + lifespan exit in one loop turn -> one drain (each stop
    point seen once), the first reason, one deadline in every requester's view."""
    async def body() -> tuple[Hooks, Any, list[float]]:
        lc = make_lc(inst)
        h = Hooks(inst, lc)
        await lc.start()
        lc.request_stop("RUNTIME_LEASE_LOST")
        d1 = lc.deadline
        lc.request_stop("GUARD_REFUSED")
        lc.request_stop("LIFESPAN")
        result = await lc.wait_stopped()
        return h, result, [d1, lc.deadline, result.deadline]

    h, result, deadlines = run(body())
    pts = h.points("b3.stop.")
    assert len(pts) == len(set(pts)), pts
    assert result.reason == "RUNTIME_LEASE_LOST"
    assert len(set(deadlines)) == 1


def test_b3a_d5_partial_startup_stops_only_started(inst: b3.Install) -> None:
    """A: fault at b3.start.schedulers after due and reminder started -> write-back never
    stopped (never started), reminder and due stopped, then port/pool/lease; FAILED."""
    lc = make_lc(inst)
    h = Hooks(inst, lc)

    def boom(_o: dict[str, Any]) -> None:
        raise RuntimeError("b3a fault")

    h.actions["b3.start.schedulers"] = boom
    with pytest.raises(Exception):  # noqa: B017,PT011
        run(lc.start())
    assert lc.state == "FAILED"
    assert lc.schedulers["writeback"].state.name == "NEW"
    assert b3.ks_free(inst)


def test_b3a_d6_lease_backend_left_alive_is_terminated(inst: b3.Install) -> None:
    """A (mutant 20): the liveness check fails while the backend stays -> cleanup terminates
    the recorded pid; result lease_lost_drained / terminated; K_S free afterwards."""
    async def body() -> Any:
        lc = make_lc(inst, config=lc_mod().LifecycleConfig(lease_check_s=1))
        await lc.start()
        lease = lc.runtime.lease

        def failing_check() -> None:
            raise s.mod("errors").SyntheticRefused("RUNTIME_LEASE_LOST")

        lease.check = failing_check  # type: ignore[method-assign]
        return await asyncio.wait_for(lc.wait_stopped(), 60)

    result = run(body())
    assert (result.disposition, result.cleanup) == ("lease_lost_drained", "terminated")
    assert b3.ks_free(inst)


def test_b3a_d8_asgi_lifespan_path(inst: b3.Install) -> None:
    """A: entering and leaving the ASGI lifespan runs start and the D1 stop order."""
    async def body() -> Hooks:
        app = b3.mod("app").create_synthetic_app(inst.env)
        h = Hooks(inst)
        async with app.router.lifespan_context(app):
            pass
        return h

    h = run(body())
    assert h.points("b3.stop.")[-1] == "b3.stop.lease"


# =========================================================================== R: recovery

@pytest.mark.parametrize("case", ["r1a", "r1b", "r2", "r3", "r4", "r5"])
def test_b3a_r_restart_recovery_across_generations(inst: b3.Install, tmp_path: Path,
                                                   case: str) -> None:
    """A (v1r1 D4.2): generation 1 runs in a child and is killed (os._exit) at the J08 point
    or stopped; the row proves it is gone (no process, no backend, K_S free); generation 2 (a
    new child) completes; no-duplicate oracles count the fsync'd journal across BOTH
    generations (J08a1/a2/b/c/d/e analogues).
    Corrected at implementation: (1) the install is first started once (runtime rows before
    the first start are refused by the full seal, SC4d); (2) each case SEEDS its own J08
    boundary test-side (`b3.seed_recovery`: a due appointment for the send cases, a sent
    reminder with reply "1" plus the simulated EHR appointment for the write-back cases),
    because the shared S-J flow leaves no pending send or write-back for generation 1."""
    lc_mod()
    crash = {"r1a": "b3.r.after_accept", "r1b": "b3.r.after_consume",
             "r2": "b3.r.after_reply", "r3": "b3.r.after_spend",
             "r4": "b3.r.after_write_issued", "r5": "b3.r.after_settle_commit"}[case]
    bar = tmp_path / "bar"
    run(start_stop(make_lc(inst)))
    seed = b3.seed_recovery(inst, case)
    g1 = b3.spawn(inst, bar, "lifecycle", app=f"{APP}-g1", crash=crash,
                  extra={"ticks": ["due", "reminder", "writeback"], "ehr_seed": seed})
    rc, out = g1.wait(180)
    assert rc == 97, out[-1500:]
    b3.assert_gone(inst, g1)
    g2 = b3.spawn(inst, bar, "lifecycle", app=f"{APP}-g2",
                  extra={"ticks": ["due", "reminder", "writeback", "writeback"],
                         "ehr_seed": seed})
    g2.seen("b3.start.watcher", 120)
    (bar / "stop").touch()
    rc2, out2 = g2.wait(180)
    assert rc2 == 0, out2[-1500:]
    j = b3.Journal(bar / "journal.jsonl").lines()
    sends = [x for x in j if x["effect"] == "send"]
    writes = [x for x in j if x["effect"] == "write_confirmation"]
    assert len({x["op_id"] for x in sends}) == len(sends), "a send was repeated"
    assert len({x["appointment_id"] for x in writes}) == len(writes), "a write was repeated"


# =========================================================================== N / F2

def test_b3a_n1_no_network_client_constructed(inst: b3.Install,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A: a full start -> one tick each -> stop with AF_INET/AF_INET6 sockets and the httpx /
    requests clients patched to raise: zero calls; STOPPED."""
    import socket

    real = socket.socket.__init__
    calls: list[Any] = []

    def guarded(self: Any, family: int = socket.AF_INET, *a: Any, **k: Any) -> None:
        if family != socket.AF_UNIX:
            calls.append(family)
            raise AssertionError("network socket created")
        real(self, family, *a, **k)

    monkeypatch.setattr(socket.socket, "__init__", guarded)
    for mod_name, attr in (("httpx", "Client"), ("httpx", "AsyncClient"),
                           ("requests", "Session")):
        try:
            m = __import__(mod_name)
            monkeypatch.setattr(m, attr, lambda *a, _n=attr, **k: calls.append(_n))
        except ImportError:
            pass

    async def body() -> str:
        lc = make_lc(inst)
        await lc.start()
        for n in ("due", "reminder", "writeback"):
            await asyncio.wait_for(lc.schedulers[n].tick(), 60)
        lc.request_stop("TEST")
        await lc.wait_stopped()
        return str(lc.state)

    assert run(body()) == "STOPPED" and calls == []


def test_b3a_n2_default_implementations_are_fake_and_simulated(inst: b3.Install) -> None:
    """A: with the default factories the sender is exactly FakeSender and the port wraps
    exactly SimulatedEhr."""
    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.simulated_ehr import SimulatedEhr

    async def body() -> tuple[type, type]:
        lc = make_lc(inst)
        await lc.start()
        try:
            return type(lc.sender), type(lc.port.ehr)
        finally:
            lc.request_stop("TEST")
            await lc.wait_stopped()

    assert run(body()) == (FakeSender, SimulatedEhr)


def test_b3a_f2_production_startup_still_refuses_a_used_install(inst: b3.Install) -> None:
    """A: after B3a use, production startup (both deployments) still refuses the synthetic
    tenant with SCHEMA_VERSION_UNSUPPORTED, and the tenant row and ledger are unchanged
    (the B1-C3 probe on a B3a-used B2 install)."""
    run(start_stop(make_lc(inst)))
    from psycopg import AsyncConnection

    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies
    from haloflow.startup import Deployment, StartupRefused, start_production_services

    lg = inst.state["logins"]
    tenant = inst.state["tenant_id"]

    def dsn(k: str) -> str:
        return inst.c.dsn(user=lg[k]["role"], password=lg[k]["password"])

    def state() -> Any:
        return inst.admin("SELECT to_jsonb(t)::text FROM shared.tenants t"), inst.admin(
            "SELECT to_jsonb(m)::text FROM shared.schema_migrations m ORDER BY m.migration_id")

    before = state()

    async def body(dep: str) -> str:
        pool = TenantPool(dsn("runtime"), min_size=1, max_size=2)
        await pool.open()

        def factory(conninfo: str) -> Any:
            async def connect() -> AsyncConnection[Any]:
                return await AsyncConnection.connect(conninfo, autocommit=True)
            return connect

        try:
            deps = TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=factory(dsn("migrator")),
                provisioner_connect=factory(dsn("provisioner")))
            with pytest.raises(StartupRefused) as ei:
                await start_production_services(deps, deployment=Deployment[dep],
                                                served_tenants=[tenant])
            return str(ei.value.reason_code)
        finally:
            await pool.close()

    for dep in ("PRODUCTION", "NON_PRODUCTION"):
        assert run(body(dep)) == "SCHEMA_VERSION_UNSUPPORTED"
    assert state() == before


def _env_unused() -> None:  # keeps os imported for children helpers
    os.getpid()
