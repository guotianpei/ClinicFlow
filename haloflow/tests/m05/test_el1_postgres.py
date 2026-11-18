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
- E1-P10 two concurrent writers on one appointment: seq strictly increasing, no duplicate [M2]
- E1-P11 settings disabled: a coverage block enrols nothing [EL-13]
- E1-P12 tenant isolation: enrolment in tenant A leaves tenant B's eligibility tables empty [§6a]
- E1-P13 ACL: runtime EXECUTE on m05_directory_v4 and m05_el_tick, none on m05_directory_v3,
  no runtime privilege on any m05_el_* table [R7]
- E1-P15 SQL due instants equal the Python rules on the pinned DST fixtures [R5, parity]
- E1-P16 malformed coverage blocks -> COMMAND_MALFORMED, nothing written [M3]
- E1-P17 PROVISIONAL (Codex ruling pending): no valid zone -> booking trigger due, time-based
  triggers pending with due_at NULL, no case event [open point D-1]
- E1-C00 CONTROL: the existing m05_directory_v3 still applies on an S-I install.

E1-P14 (no identifiers in logs) moves to E2: E1 has no Python service path that logs.
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


# --- E1-P10 sequence under the appointment lock -----------------------------------------

def test_el1_p10_concurrent_writers_get_distinct_increasing_seq(install: el.EL1Install) -> None:
    inst = el.ready(install)
    ref, patient, start = sa.new_id(), sa.new_id(), _future(10)
    el.put(inst, ref, patient, start, coverage={"state": "none"})       # seq 1
    errors: list[BaseException] = []

    def writer(cov: dict[str, Any], status: str) -> None:
        try:
            el.put(inst, ref, patient, start, coverage=cov, status=status)
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(el.present(el.OTHER_PAYER), "booked")),
               threading.Thread(target=writer, args=({"state": "none"}, "cancelled"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    seqs = [e[0] for e in _events(inst, ref)]
    assert seqs == sorted(set(seqs)) and seqs[0] == 1 and len(seqs) >= 2
    assert _q(inst, "SELECT next_seq FROM {s}.m05_el_appointments"
              " WHERE appointment_ref = %s", (ref,)) == [(seqs[-1] + 1,)]


# --- E1-P11 disabled --------------------------------------------------------------------

def test_el1_p11_disabled_enrols_nothing(install: el.EL1Install) -> None:
    inst = el.ready(install, enabled=False)
    el.put(inst, sa.new_id(), sa.new_id(), _future(10), coverage=el.present())
    assert el.el_snapshot(inst) == dict.fromkeys(el.EL_TABLES, 0)


# --- E1-P12 tenant isolation ------------------------------------------------------------

def test_el1_p12_enrolment_stays_in_its_tenant(install: el.EL1Install,
                                               install_b: el.EL1Install) -> None:
    a, b = el.ready(install), el.ready(install_b)
    ref, patient = sa.new_id(), sa.new_id()
    el.put(a, ref, patient, _future(10), coverage={"state": "none"})
    assert el.count(a, "m05_el_appointments") == 1
    assert el.el_snapshot(b) == dict.fromkeys(el.EL_TABLES, 0)


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


# --- E1-P17 PROVISIONAL: no valid zone --------------------------------------------------

def test_el1_p17_provisional_no_zone(install: el.EL1Install) -> None:
    import sh2_support as sh2

    inst = el.ready(install)
    sh2.settings_raw(inst, clinic_tz=None)
    ref = sa.new_id()
    el.put(inst, ref, sa.new_id(), _future(10), coverage=el.present(), department_tz="Bad/Zone")
    trig = _triggers(inst, ref)
    assert trig["booking"][1] is not None and trig["booking"][2] == "pending"
    assert trig["previsit"][1] is None and trig["dayof"][1] is None
    assert el.count(inst, "m05_el_case_events") == 0
    time.sleep(0)  # no clock dependency
