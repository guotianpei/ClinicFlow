"""Eligibility E1 — PostgreSQL 17 rows (RED BY DESIGN).

Each row provisions a fresh synthetic tenant at target 101 in its body, so the intended red
cause is the missing E1 source: `build_synthetic_registry(include_el1=...)` does not exist
(TypeError at provisioning, reported as "E1 test installation failed") or, for the parity
row, ModuleNotFoundError for `haloflow.m05.eligibility_rules`. Rows marked CONTROL use only
existing code and must pass now.

Rows (stage-3 ids; trace in brackets):
- E1-P01 legacy appointment_put without a coverage block enrols nothing, enabled or not [M3, W15]
- E1-P02 `present` priority coverage: key row, coverage v1, three pending triggers with due
  times equal to the pure rules [M3, R2, R5]
- E1-P03 `pending`: key row + triggers; no verification, case event or count [M3]
- E1-P04 OD-EL9 not-received deadline: not raised before, raised once after, replay/tick
  idempotent; N = 0 uses the day-of instant; coverage received first -> never raised [OD-EL9, W9]
- E1-P05 non-priority, the same put x3: one manual verification, one case event, count 1 [EL-3, W14]
- E1-P06 none / incomplete buckets, payer_key '-', period = service-date month (local) [R3]
- E1-P06b two distinct appointments, same payer/month/bucket, each x3 -> count 2 [E1-M04 oracle]
- E1-P07 coverage v2: triggers re-pointed to v2 and pending, booking due again [R2]
- E1-P08 enrolled appointment, later put without block: version unchanged; status processed [C2]
- E1-P09 cancelled / replaced: triggers cancelled, note-only event with the next seq [EL-6f, W13]
- E1-P10 forced interleaving: tick A holds the key-row lock open; cancel put B is blocked BY A
  (pg_blocking_pids(B) contains A); after A commits, seq 1 (A) then 2 (B) [M2; kills E1-M06]
- E1-P11 settings disabled: a coverage block enrols nothing [EL-13]
- E1-P12a routed: own tenant succeeds; principal authorized for A asking for B -> TenantDenied
  TENANT_BINDING_MISMATCH, both tenants unchanged [§6a]
- E1-P12b direct SQL, NARROWER: schema-bound only; B's context on A's function writes A only,
  B byte-identical [§6a]
- E1-P13 ACL: runtime EXECUTE on m05_directory_v4 and m05_el_tick, none on m05_directory_v3,
  no runtime privilege on any m05_el_* table [R7]
- E1-P15 SQL due instants equal the Python rules on the pinned DST fixtures [R5, parity]
- E1-P16 malformed coverage blocks -> COMMAND_MALFORMED, nothing written [M3]
- E1-P04s the deadline seam m05_el_deadline_passed at -1 us / equal / +1 us; the tick decides
  only through it [OD-EL9, Codex 5d2877d0 item 3]
- E1-P14 no identifiers (synthetic sentinels) in captured logs (positive control), routed
  results/errors, derived rows, tick output, or the direct SQL error fields [EL-14]
- E1-P17 no usable zone: booking recorded, all date actions deferred, tick diagnostic,
  recovery once after zone repair, no duplicates [Codex D-1 ruling]
- E1-C00 CONTROL: the existing m05_directory_v3 still applies on an S-I install.

Mutant killers (wired at green): M01 due drops time of day -> U01/P15; M02 lead off-by-one ->
U01 each-lead/P15; M03 omission treated as none -> P01/P08; M04 counts key without
appointment -> P06b; M05 pending opens a case at booking -> P03; M06 seq before the lock ->
P10; M07 seam `<` instead of `<=` -> P04s (equal); M08 tick compares outside the seam ->
P04 static row; M09 guessed zone (UTC) on no zone -> P17.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import el1_support as el
import m02_support
import psycopg
import pytest
import sa_support as sa
import si_support as si
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[el.EL1Install]:
    yield from el.module_install(request, m02_ids, m05_reset, f"el1:{request.node.name}")


@pytest.fixture
def install_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
              m05_reset: Any) -> Iterator[el.EL1Install]:
    yield from el.module_install(request, m02_ids, m05_reset, f"el1b:{request.node.name}")


@pytest.fixture
def si_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[si.SIInstall]:
    yield from si.module_install(request, m02_ids, m05_reset, f"el1-si:{request.node.name}")


def _future(days: float) -> datetime:
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0)


def _q(install: el.EL1Install, text: str, params: Any = None) -> list[tuple[Any, ...]]:
    return sa.admin(install, sa.q(install, text), params)


def _triggers(install: el.EL1Install, ref: str) -> dict[str, tuple[Any, ...]]:
    got = _q(install, "SELECT trigger, coverage_version, due_at, status FROM {s}.m05_el_triggers"
             " WHERE appointment_ref = %s", (ref,))
    return {r[0]: r[1:] for r in got}


def _events(install: el.EL1Install, ref: str) -> list[tuple[Any, ...]]:
    return _q(install, "SELECT seq, coverage_version, kind, class, reason"
              " FROM {s}.m05_el_case_events WHERE appointment_ref = %s ORDER BY seq", (ref,))


# --- E1-C00 CONTROL ---------------------------------------------------------------------

def test_el1_c00_control_directory_v3_unchanged_on_si(si_install: si.SIInstall) -> None:
    inst = si.ready_install(si_install)
    ref, patient = sa.new_id(), sa.new_id()
    got = si.directory3(inst, "appointment_put", **si.appointment3(ref, patient, _future(10)))
    assert got == {"schema_version": 1, "outcome": "applied"}


# --- E1-P01 legacy put enrols nothing ---------------------------------------------------

@pytest.mark.parametrize("enabled", [True, False])
def test_el1_p01_legacy_put_enrols_nothing(install: el.EL1Install, enabled: bool) -> None:
    inst = el.ready(install, enabled=enabled)
    ref, patient = sa.new_id(), sa.new_id()
    assert el.put(inst, ref, patient, _future(10)) == {"schema_version": 1, "outcome": "applied"}
    assert el.el_snapshot(inst) == dict.fromkeys(el.EL_TABLES, 0)


# --- E1-P02 present priority ------------------------------------------------------------

def test_el1_p02_present_priority_enrols_with_three_triggers(install: el.EL1Install) -> None:
    from haloflow.m05 import eligibility_rules as rules

    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    before = datetime.now(UTC)
    assert el.put(inst, ref, patient, start, coverage=el.present()) == {
        "schema_version": 1, "outcome": "applied"}
    after = datetime.now(UTC)
    key = _q(inst, "SELECT coverage_state, coverage_version, zone, next_seq, closure_watermark"
             " FROM {s}.m05_el_appointments WHERE appointment_ref = %s", (ref,))
    assert key == [("present", 1, el.NY, 1, 0)]
    cov = _q(inst, "SELECT coverage_version, state, completeness, payer_id"
             " FROM {s}.m05_el_coverage WHERE appointment_ref = %s", (ref,))
    assert cov == [(1, "present", "complete", el.PRIORITY[0])]
    trig = _triggers(inst, ref)
    assert set(trig) == {"booking", "previsit", "dayof"}
    assert all(t[0] == 1 and t[2] == "pending" for t in trig.values())
    assert before - timedelta(seconds=1) <= trig["booking"][1] <= after + timedelta(seconds=1)
    assert trig["previsit"][1] == rules.previsit_due(start, el.NY, 3)
    assert trig["dayof"][1] == rules.dayof_due(start, el.NY)
    assert el.count(inst, "m05_el_case_events") == 0
    assert el.count(inst, "m05_el_counts") == 0


# --- E1-P03 pending ---------------------------------------------------------------------

def test_el1_p03_pending_enrols_without_verification_or_case(install: el.EL1Install) -> None:
    from haloflow.m05 import eligibility_rules as rules

    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage={"state": "pending"})
    key = _q(inst, "SELECT coverage_state, not_received_due_at FROM {s}.m05_el_appointments"
             " WHERE appointment_ref = %s", (ref,))
    assert key == [("pending", rules.not_received_due(start, el.NY, 1))]
    assert set(_triggers(inst, ref)) == {"booking", "previsit", "dayof"}
    for table in ("m05_el_verifications", "m05_el_case_events", "m05_el_counts"):
        assert el.count(inst, table) == 0, table


# --- E1-P04 not-received deadline (OD-EL9) ----------------------------------------------

def _set_deadline(inst: el.EL1Install, ref: str, offset_s: float) -> None:
    """FIXTURE: move the stored deadline relative to database time (the S-I age pattern)."""
    _q(inst, "UPDATE {s}.m05_el_appointments SET not_received_due_at ="
       " clock_timestamp() + make_interval(secs => %s) WHERE appointment_ref = %s",
       (offset_s, ref))


def test_el1_p04_not_received_raised_once_after_deadline(install: el.EL1Install) -> None:
    inst = el.ready(install)
    ref, patient = sa.new_id(), sa.new_id()
    el.put(inst, ref, patient, _future(10), coverage={"state": "pending"})
    _set_deadline(inst, ref, 5.0)
    assert el.tick(inst)["raised"] == 0
    assert _events(inst, ref) == []
    _set_deadline(inst, ref, -0.001)
    assert el.tick(inst)["raised"] == 1
    assert _events(inst, ref) == [(1, 1, "open_or_note", "manual_required",
                                   "coverage_not_received")]
    assert el.tick(inst)["raised"] == 0
    el.put(inst, ref, patient, _future(10), coverage={"state": "pending"})  # replay
    assert el.tick(inst)["raised"] == 0
    assert len(_events(inst, ref)) == 1
    assert _q(inst, "SELECT bucket, payer_key FROM {s}.m05_el_counts"
              " WHERE appointment_ref = %s", (ref,)) == [("not_received", "-")]


def test_el1_p04_n0_deadline_is_the_dayof_instant(install: el.EL1Install) -> None:
    from haloflow.m05 import eligibility_rules as rules

    inst = el.ready(install, not_received_days=0)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage={"state": "pending"})
    assert _q(inst, "SELECT not_received_due_at FROM {s}.m05_el_appointments"
              " WHERE appointment_ref = %s", (ref,)) == [(rules.dayof_due(start, el.NY),)]


def test_el1_p04_coverage_received_first_is_never_raised(install: el.EL1Install) -> None:
    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage={"state": "pending"})
    _set_deadline(inst, ref, 5.0)
    el.put(inst, ref, patient, start, coverage=el.present())
    _set_deadline(inst, ref, -1.0)     # even a stale stored deadline must not raise now
    assert el.tick(inst)["raised"] == 0
    assert _events(inst, ref) == []


@pytest.mark.parametrize(("offset_us", "expected"), [(-1, False), (0, True), (1, True)])
def test_el1_p04_deadline_seam_exact_instants(install: el.EL1Install, offset_us: int,
                                              expected: bool) -> None:
    """The tick's only decision seam: due <= now. Equal instants count as passed."""
    inst = el.ready(install)
    due = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    now = due + timedelta(microseconds=offset_us)
    assert _q(inst, "SELECT {s}.m05_el_deadline_passed(%s, %s)", (due, now)) == [(expected,)]
    assert _q(inst, "SELECT {s}.m05_el_deadline_passed(%s, %s)", (due, now)) == [(expected,)]


def test_el1_p04_tick_decides_only_through_the_seam(install: el.EL1Install) -> None:
    """Static: the tick body calls the seam and compares not_received_due_at nowhere else;
    the seam is IMMUTABLE and not executable by the runtime role."""
    inst = el.ready(install)
    body = _q(inst, "SELECT pg_get_functiondef(format('%%I.m05_el_tick(jsonb)', %s::text)"
              "::regprocedure)", (inst.schema,))[0][0]
    assert "m05_el_deadline_passed(" in body
    for op in ("not_received_due_at <", "not_received_due_at >", "< clock_timestamp",
               "<= clock_timestamp", "> clock_timestamp"):
        assert op not in body.replace("  ", " "), op
    meta = _q(inst, "SELECT p.provolatile, has_function_privilege('haloflow_runtime', p.oid,"
              " 'EXECUTE') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
              " WHERE n.nspname = %s AND p.proname = 'm05_el_deadline_passed'", (inst.schema,))
    assert meta == [("i", False)]


# --- E1-P14 privacy at the E1 intake and error boundary ---------------------------------

SENTINELS = ("SENTINEL-MEMBER-7731", "Sentinel Patientname", "SENTINEL-SUB-7732",
             "Sentinel Subscribername", "1911-11-11")


def _no_sentinel(text: str) -> None:
    for s_ in SENTINELS:
        assert s_ not in text, s_


def test_el1_p14_no_identifiers_in_logs_results_errors_or_derived_rows(
        install: el.EL1Install, caplog: pytest.LogCaptureFixture) -> None:
    """Evidence boundary: Python logging (root and every `haloflow*` logger, DEBUG) during a
    routed success and a routed malformed intake, plus results, derived rows, tick output and
    the error fields. A positive control proves the capture sees a haloflow M05 record. The
    PostgreSQL server log is outside this row (statement parameters are not logged on error by
    default: log_parameter_max_length_on_error = 0); disclosed in the packet."""
    import asyncio
    import logging

    inst = el.ready(install)
    cov = el.present(el.OTHER_PAYER, relationship="dependent", member_id=SENTINELS[0],
                     patient_name=SENTINELS[1], subscriber_member_id=SENTINELS[2],
                     subscriber_name=SENTINELS[3], patient_dob=SENTINELS[4])
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    ok = si.appointment3(ref, patient, start)
    ok["coverage"] = cov
    bad = si.appointment3(sa.new_id(), sa.new_id(), start)
    bad["coverage"] = {**cov, "subscriber_dob": "11/11/1911"}
    caplog.set_level(logging.DEBUG)
    control = "EL1-P14-CAPTURE-CONTROL"
    logging.getLogger("haloflow.m05.eligibility").warning(control)

    async def run() -> tuple[dict[str, Any], BaseException]:
        async with el.runtime(inst) as rt:
            result = await el.routed_put(rt, inst, ok)
            try:
                await el.routed_put(rt, inst, bad)
            except Exception as error:  # noqa: BLE001 - examined below
                return result, error
            raise AssertionError("malformed coverage was accepted")

    result, error = asyncio.run(run())
    logged = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert control in logged, "log capture is not working"
    _no_sentinel(logged)
    _no_sentinel(str(result))
    _no_sentinel(f"{type(error).__name__} {error!s} {error!r}")
    for table in el.EL_TABLES:
        if table != "m05_el_coverage":
            _no_sentinel(str(_q(inst, "SELECT to_jsonb(x)::text FROM {s}." + table + " AS x")))
    _no_sentinel(str(el.tick(inst)))
    # Direct SQL error fields (the layer below the gateway) also echo nothing.
    with pytest.raises(psycopg.errors.RaiseException) as caught:
        sa.call(inst, "m05_directory_v4", {"schema_version": 1, "op": "appointment_put",
                                           **bad,
                                           "attribution": sa.attribution(sa.W_DIRECTORY)})
    diag = caught.value.diag
    _no_sentinel(" ".join(str(x) for x in (diag.message_primary, diag.message_detail,
                                          diag.message_hint, diag.context, str(caught.value))))
    assert diag.message_primary == "COMMAND_MALFORMED"


# --- E1-P05 non-priority replay ---------------------------------------------------------

def test_el1_p05_nonpriority_replay_is_one_case_one_count(install: el.EL1Install) -> None:
    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    for _ in range(3):
        el.put(inst, ref, patient, start, coverage=el.present(el.OTHER_PAYER))
    ver = _q(inst, "SELECT owner_trigger, state, result_class, reason"
             " FROM {s}.m05_el_verifications WHERE appointment_ref = %s", (ref,))
    assert ver == [("booking", "settled", "manual_required", "nonpriority")]
    assert _events(inst, ref) == [(1, 1, "open_or_note", "manual_required", "nonpriority")]
    assert _q(inst, "SELECT bucket, payer_key FROM {s}.m05_el_counts"
              " WHERE appointment_ref = %s", (ref,)) == [("nonpriority", el.OTHER_PAYER)]
    trig = _triggers(inst, ref)
    assert trig["booking"][2] == "satisfied"
    assert trig["previsit"][2] == trig["dayof"][2] == "not_applicable"


# --- E1-P06 buckets ---------------------------------------------------------------------

@pytest.mark.parametrize(("coverage", "bucket", "reason"), [
    ({"state": "none"}, "no_coverage", "no_coverage"),
    (el.present(member_id=None), "incomplete", "incomplete"),
    (el.present(relationship="dependent", subscriber_dob=None), "incomplete", "incomplete"),
])
def test_el1_p06_manual_buckets(install: el.EL1Install, coverage: dict[str, Any], bucket: str,
                                reason: str) -> None:
    inst = el.ready(install)
    ref, patient = sa.new_id(), sa.new_id()
    # 2027-01-01 04:30Z is 2026-12-31 23:30 in New York: the period is the LOCAL month.
    start = datetime(2027, 1, 1, 4, 30, tzinfo=UTC)
    el.put(inst, ref, patient, start, coverage=coverage)
    assert _q(inst, "SELECT period, bucket, payer_key FROM {s}.m05_el_counts"
              " WHERE appointment_ref = %s", (ref,)) == [("2026-12", bucket, "-")]
    assert _events(inst, ref) == [(1, 1, "open_or_note", "manual_required", reason)]


def test_el1_p06b_counts_are_per_appointment(install: el.EL1Install) -> None:
    inst = el.ready(install)
    start = datetime(2027, 2, 10, 15, 0, tzinfo=UTC)
    refs = [sa.new_id(), sa.new_id()]
    for ref in refs:
        patient = sa.new_id()
        for _ in range(3):
            el.put(inst, ref, patient, start, coverage=el.present(el.OTHER_PAYER))
    assert _q(inst, "SELECT count(*) FROM {s}.m05_el_counts WHERE period = '2027-02'"
              " AND bucket = 'nonpriority' AND payer_key = %s", (el.OTHER_PAYER,)) == [(2,)]


# --- E1-P07 coverage change -------------------------------------------------------------

def test_el1_p07_coverage_v2_repoints_triggers(install: el.EL1Install) -> None:
    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage=el.present(el.OTHER_PAYER))
    el.put(inst, ref, patient, start, coverage=el.present(el.PRIORITY[1]))
    assert _q(inst, "SELECT coverage_state, coverage_version FROM {s}.m05_el_appointments"
              " WHERE appointment_ref = %s", (ref,)) == [("present", 2)]
    trig = _triggers(inst, ref)
    assert all(t[0] == 2 and t[2] == "pending" for t in trig.values()), trig
    # v1's manual verification stays settled history; it is not re-opened or re-counted.
    assert _q(inst, "SELECT coverage_version, state FROM {s}.m05_el_verifications"
              " WHERE appointment_ref = %s", (ref,)) == [(1, "settled")]
    # An identical re-put is a replay, not a third version.
    el.put(inst, ref, patient, start, coverage=el.present(el.PRIORITY[1]))
    assert _q(inst, "SELECT coverage_version FROM {s}.m05_el_appointments"
              " WHERE appointment_ref = %s", (ref,)) == [(2,)]


# --- E1-P08 omission on a later put -----------------------------------------------------

def test_el1_p08_later_put_without_block_keeps_enrolment(install: el.EL1Install) -> None:
    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage=el.present())
    el.put(inst, ref, patient, start)                      # no block
    assert _q(inst, "SELECT coverage_state, coverage_version FROM {s}.m05_el_appointments"
              " WHERE appointment_ref = %s", (ref,)) == [("present", 1)]
    el.put(inst, ref, patient, start, status="cancelled")  # no block, status change
    assert {t[2] for t in _triggers(inst, ref).values()} == {"cancelled"}


# --- E1-P09 cancel / replace ------------------------------------------------------------

@pytest.mark.parametrize(("status", "reason"), [("cancelled", "appointment_cancelled"),
                                                ("replaced", "appointment_rescheduled")])
def test_el1_p09_status_change_cancels_and_notes(install: el.EL1Install, status: str,
                                                 reason: str) -> None:
    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage={"state": "none"})       # seq 1: manual case
    el.put(inst, ref, patient, start, coverage={"state": "none"}, status=status)
    assert _events(inst, ref) == [
        (1, 1, "open_or_note", "manual_required", "no_coverage"),
        (2, 1, "note_only", None, reason)]
    assert all(t[2] in ("satisfied", "not_applicable", "cancelled")
               for t in _triggers(inst, ref).values())
    # Replay of the status change adds nothing.
    el.put(inst, ref, patient, start, coverage={"state": "none"}, status=status)
    assert len(_events(inst, ref)) == 2


def test_el1_p09_replacement_enrols_as_new_booking(install: el.EL1Install) -> None:
    inst = el.ready(install)
    old, new, patient, start = sa.new_id(), sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, old, patient, start, coverage=el.present())
    el.put(inst, old, patient, start, coverage=el.present(), status="replaced")
    el.put(inst, new, patient, start + timedelta(days=1), coverage=el.present())
    assert {t[2] for t in _triggers(inst, new).values()} == {"pending"}


# --- E1-P10 sequence under the appointment lock (forced interleaving) -------------------

def _blockers(inst: el.EL1Install, pid: int) -> list[int]:
    got = sa.admin(inst, "SELECT pg_blocking_pids(%s)", (pid,))
    return list(got[0][0] or [])


def test_el1_p10_second_writer_waits_and_commits_after_first(install: el.EL1Install) -> None:
    """Writer A is the timer tick, in an OPEN transaction, raising the not-received event
    (seq 1) and so holding the appointment KEY row (m05_el_appointments) lock. Writer B is a
    cancel put: it takes the m05_appointments row (free, A never touched it) and must then
    WAIT on the key row, not read next_seq early. (E1-M06 = seq taken before the key lock: B
    would read next_seq = 1, then collide or reorder.) Two writers that both go through
    m05_appointments would be serialized by that row lock and could not detect M06, so the
    tick is the first writer here. After A commits, B completes; durable effects are exactly
    seq 1 (A) then seq 2 (B)."""
    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage={"state": "pending"})
    _set_deadline(inst, ref, -1.0)
    tick_cmd = {"schema_version": 1, "attribution": sa.attribution(sa.W_TIMER)}
    done: dict[str, Any] = {}

    b_fields = si.appointment3(ref, patient, start, status="cancelled")
    b_fields["coverage"] = {"state": "pending"}
    b_cmd = {"schema_version": 1, "op": "appointment_put", **b_fields,
             "attribution": sa.attribution(sa.W_DIRECTORY)}

    with sa.runtime_conn(inst) as conn_a, sa.runtime_conn(inst) as conn_b:
        a_pid = conn_a.execute("SELECT pg_backend_pid()").fetchone()[0]  # type: ignore[index]
        b_pid = conn_b.execute("SELECT pg_backend_pid()").fetchone()[0]  # type: ignore[index]

        def writer_b() -> None:
            try:
                with conn_b.transaction():
                    done["b"] = sa.call_in(conn_b, inst, "m05_directory_v4", b_cmd)
            except BaseException as error:  # noqa: BLE001 - surfaced below
                done["b_error"] = error

        with conn_a.transaction():
            done["a"] = sa.call_in(conn_a, inst, "m05_el_tick", tick_cmd)
            assert done["a"]["raised"] == 1
            b = threading.Thread(target=writer_b)
            b.start()
            deadline = time.monotonic() + 10
            while (a_pid not in _blockers(inst, b_pid)) and time.monotonic() < deadline:
                time.sleep(0.05)
            # B is blocked BY A specifically (not by any other waiter in the database).
            assert a_pid in _blockers(inst, b_pid), "writer B never waited on A"
            assert "b" not in done and "b_error" not in done
        # A committed here.
        b.join(30)
    assert not b.is_alive(), "writer B did not complete"
    assert "b_error" not in done, done.get("b_error")
    assert done["b"] == {"schema_version": 1, "outcome": "applied"}
    assert _events(inst, ref) == [
        (1, 1, "open_or_note", "manual_required", "coverage_not_received"),
        (2, 1, "note_only", None, "appointment_cancelled")]
    assert _q(inst, "SELECT next_seq FROM {s}.m05_el_appointments"
              " WHERE appointment_ref = %s", (ref,)) == [(3,)]


# --- E1-P11 disabled --------------------------------------------------------------------

def test_el1_p11_disabled_enrols_nothing(install: el.EL1Install) -> None:
    inst = el.ready(install, enabled=False)
    el.put(inst, sa.new_id(), sa.new_id(), _future(10), coverage=el.present())
    assert el.el_snapshot(inst) == dict.fromkeys(el.EL_TABLES, 0)


# --- E1-P12 tenant isolation against existing foreign data ----------------------------

def _el_rows(inst: el.EL1Install) -> dict[str, list[tuple[Any, ...]]]:
    return {t: _q(inst, "SELECT to_jsonb(x)::text FROM {s}." + t + " AS x ORDER BY 1")
            for t in el.EL_TABLES}


def _seed_both(a: el.EL1Install, b: el.EL1Install) -> tuple[str, str, datetime]:
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(a, ref, patient, start, coverage={"state": "none"})
    el.put(b, ref, patient, start, coverage=el.present(el.OTHER_PAYER))
    return ref, patient, start


def test_el1_p12a_routed_own_tenant_succeeds_and_wrong_tenant_is_denied(
        install: el.EL1Install, install_b: el.EL1Install) -> None:
    """The authorized routing boundary (resolver + gateway, target 101, directory key rebound
    to m05_directory_v4). Own tenant: success, only A changes. A principal authorized for A
    asking for tenant B: TenantDenied TENANT_BINDING_MISMATCH before any SQL; both unchanged."""
    import asyncio

    from haloflow.m01.errors import TenantDenied

    a, b = el.ready(install), el.ready(install_b)
    ref, patient, start = _seed_both(a, b)
    a0, b0 = _el_rows(a), _el_rows(b)
    fields = si.appointment3(ref, patient, start, status="cancelled")
    fields["coverage"] = {"state": "none"}

    async def run() -> None:
        async with el.runtime(a) as rt:
            with pytest.raises(TenantDenied) as denied:
                await el.routed_put(rt, b, fields, authorized_for=a)
            assert denied.value.reason_code == "TENANT_BINDING_MISMATCH"
            assert _el_rows(a) == a0 and _el_rows(b) == b0
            got = await el.routed_put(rt, a, fields)
            assert got == {"schema_version": 1, "outcome": "applied"}

    asyncio.run(run())
    assert _el_rows(b) == b0
    assert _events(a, ref)[-1] == (2, 1, "note_only", None, "appointment_cancelled")


def test_el1_p12b_direct_sql_is_schema_bound_only(install: el.EL1Install,
                                                  install_b: el.EL1Install) -> None:
    """NARROWER guarantee, labelled as such: direct SQL below the routing boundary is confined
    to the function's own schema. Own context: success (no error tolerated). B's tenant context
    on A's function: the gateway checks only the context's form, so the write still lands in
    A's schema; B's rows stay byte-identical. Tenant authorization is the routing boundary's
    job (P12a), not this layer's."""
    a, b = el.ready(install), el.ready(install_b)
    ref, patient, start = _seed_both(a, b)
    b0 = _el_rows(b)
    fields = si.appointment3(ref, patient, start, status="cancelled")
    fields["coverage"] = {"state": "none"}
    cmd = {"schema_version": 1, "op": "appointment_put", **fields,
           "attribution": sa.attribution(sa.W_DIRECTORY)}
    with sa.runtime_conn(a) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (b.tenant_id,))
        got = conn.execute(sql.SQL("SELECT {}.{}(%s)").format(
            sql.Identifier(a.schema), sql.Identifier("m05_directory_v4")),
            (Jsonb(cmd),)).fetchone()
    assert got is not None and got[0] == {"schema_version": 1, "outcome": "applied"}
    assert _el_rows(b) == b0
    assert _events(a, ref)[-1] == (2, 1, "note_only", None, "appointment_cancelled")


# --- E1-P13 ACL -------------------------------------------------------------------------

def test_el1_p13_runtime_privileges(install: el.EL1Install) -> None:
    inst = el.ready(install)

    def can_execute(fn: str) -> bool:
        got = _q(inst, "SELECT has_function_privilege('haloflow_runtime',"
                 " format('%%I.%%I(jsonb)', %s::text, %s::text), 'EXECUTE')",
                 (inst.schema, fn))
        return bool(got[0][0])

    assert can_execute("m05_directory_v4")
    assert can_execute("m05_el_tick")
    assert not can_execute("m05_directory_v3")
    for table in (*el.EL_TABLES, "m05_el_settings", "m05_el_priority_payers"):
        got = _q(inst, "SELECT has_table_privilege('haloflow_runtime',"
                 " format('%%I.%%I', %s::text, %s::text),"
                 " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
                 (inst.schema, table))
        assert got == [(False,)], table


# --- E1-P15 SQL / Python parity on the pinned DST fixtures ------------------------------

@pytest.mark.parametrize(("local_start", "lead"), [
    (datetime(2026, 11, 4, 1, 30), 3),      # -> 2026-11-01 01:30 EDT (ambiguous, earlier)
    (datetime(2026, 3, 11, 2, 30), 3),      # -> 2026-03-08 03:00 EDT (gap, first valid)
    (datetime(2026, 11, 20, 15, 0), 5),
])
def test_el1_p15_sql_due_matches_rules(install: el.EL1Install, local_start: datetime,
                                       lead: int) -> None:
    from haloflow.m05 import eligibility_rules as rules

    inst = el.ready(install, lead_days=lead, not_received_days=lead)
    start = local_start.replace(tzinfo=ZoneInfo(el.NY)).astimezone(UTC)
    ref = sa.new_id()
    el.put(inst, ref, sa.new_id(), start, coverage={"state": "pending"})
    trig = _triggers(inst, ref)
    assert trig["previsit"][1] == rules.previsit_due(start, el.NY, lead)
    assert trig["dayof"][1] == rules.dayof_due(start, el.NY)
    assert _q(inst, "SELECT not_received_due_at FROM {s}.m05_el_appointments"
              " WHERE appointment_ref = %s", (ref,)) == [(rules.not_received_due(
                  start, el.NY, lead),)]


# --- E1-P16 malformed blocks ------------------------------------------------------------

@pytest.mark.parametrize("coverage", [
    {"state": "unknown"},
    {"state": "none", "payer_id": "X"},
    {k: v for k, v in el.present().items() if k != "subscriber_dob"},
    el.present(patient_dob="01/01/1970"),
    el.present(member_id="x" * 65),
    el.present(payer_id=7),
    "none",
    None,          # explicit JSON null (the key present) is malformed, unlike omission
])
def test_el1_p16_malformed_block_refused(install: el.EL1Install, coverage: Any) -> None:
    inst = el.ready(install)
    fields = si.appointment3(sa.new_id(), sa.new_id(), _future(10))
    fields["coverage"] = coverage
    with pytest.raises(psycopg.errors.RaiseException, match="COMMAND_MALFORMED"):
        sa.call(inst, "m05_directory_v4", {"schema_version": 1, "op": "appointment_put",
                                           **fields,
                                           "attribution": sa.attribution(sa.W_DIRECTORY)})
    assert el.el_snapshot(inst) == dict.fromkeys(el.EL_TABLES, 0)


# --- E1-P17 no usable zone: record, defer, diagnose, recover once (Codex D-1) ----------

@pytest.mark.parametrize("coverage", [el.present(), el.present(el.OTHER_PAYER)])
def test_el1_p17_no_zone_defers_then_recovers_once(install: el.EL1Install,
                                                   coverage: dict[str, Any]) -> None:
    import sh2_support as sh2

    from haloflow.m05 import eligibility_rules as rules

    inst = el.ready(install)
    sh2.settings_raw(inst, clinic_tz=None)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage=coverage, department_tz="Bad/Zone")
    assert _q(inst, "SELECT coverage_state, coverage_version, zone, deferred_reason"
              " FROM {s}.m05_el_appointments WHERE appointment_ref = %s", (ref,)) == [
        ("present", 1, None, "no_zone")]
    trig = _triggers(inst, ref)
    assert {k: (v[1], v[2]) for k, v in trig.items()} == {
        "booking": (None, "deferred"), "previsit": (None, "deferred"),
        "dayof": (None, "deferred")}
    for table in ("m05_el_verifications", "m05_el_case_events", "m05_el_counts"):
        assert el.count(inst, table) == 0, table
    first = el.tick(inst)
    assert (first["replanned"], first["deferred_no_zone"]) == (0, 1)
    # Repair the clinic zone: the next tick re-plans exactly once.
    sh2.settings_raw(inst, clinic_tz=el.NY)
    second = el.tick(inst)
    assert (second["replanned"], second["deferred_no_zone"]) == (1, 0)
    trig = _triggers(inst, ref)
    if coverage["payer_id"] in el.PRIORITY:
        assert trig["previsit"][1] == rules.previsit_due(start, el.NY, 3)
        assert {v[2] for v in trig.values()} == {"pending"}
        assert el.count(inst, "m05_el_case_events") == 0
    else:
        assert _events(inst, ref) == [(1, 1, "open_or_note", "manual_required", "nonpriority")]
        assert el.count(inst, "m05_el_counts") == 1
    third = el.tick(inst)
    assert (third["replanned"], third["deferred_no_zone"]) == (0, 0)
    assert el.count(inst, "m05_el_case_events") in (0, 1)
    assert el.count(inst, "m05_el_counts") in (0, 1)
