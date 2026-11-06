"""L-1S package S-A — tables, grants, contracts, directory and dispatch on PostgreSQL 17.

Pins v1 sections 2-5; architecture v1r1 sections 2, 3; Codex 2155 items 2, 4, 5, 6.
Calls go through the runtime login, SET ROLE runtime, `app.tenant_id` set
transaction-locally, the reviewed signature. Refusals are P0001 with MESSAGE = code
and ZERO writes (full S-A table contents + item/history + M02 counts compared).

- SA-T01  install: the six units at target 42; the seven tables migrator-owned; the
          runtime holds NO privilege on any S-A table; m05_ namespace exactly the
          pinned set (ME-01 extended); helper not m05_-named, IMMUTABLE, INVOKER.
- SA-T02  immutability: m05_inbound_replies and m05_activation_records refuse
          UPDATE/DELETE/TRUNCATE for every role, migrator included (0A000).
- SA-T03  contracts: the three types exist, family `m05_intake`, producer
          `m05_intake`, no links, allow-list exactly the pinned paths; inactive in
          the UNIT (a fresh install before the test profile has them inactive).
- SA-T04  exact predicate: intake_reply_is_timely(r, d) is r < d at d-1us (true),
          d (false), d+1us (false), NULL inputs (NULL, never true).
- SA-D01  directory: wrong workload / actor -> WORKLOAD_NOT_AUTHORIZED; malformed ->
          COMMAND_MALFORMED; contact_put/remove, appointment_put applied.
- SA-D02  directory lock policy: a directory write on a phone WAITS while a
          registration holds that phone's contact lock (held-lock seam), and the
          reverse; appointment_put that moves a patient locks old and new phones.
- SA-D03  first contact insertion vs inbound (Codex 1913 item 1): contact_put locks
          the PROSPECTIVE phone too. Inbound first (unknown phone, holds the lock) ->
          contact_put waits; inbound saw "before" (quarantined). contact_put first ->
          inbound waits and sees "after" (the patient; no window -> unclear_reply).
- SA-D04  concurrent contact change vs dispatch: a second phone added for the patient
          while dispatch waits -> DISPATCH_UNROUTABLE (after view); dispatch first ->
          contact_put waits, the window keeps the original phone (before view).
- SA-P01  dispatch registers: window awaiting, deadline = sent_at + 24 h, sent_at
          after the lock (>= transaction start), fingerprint = independent oracle,
          dispatched observation appended BY THE SERVICE only (none from SQL).
- SA-P02  retry same command -> existing, identical stored row (deadline never
          reset); retry after a later STOP -> existing (not CONTACT_OPTED_OUT);
          retry after the directory moved the appointment to another patient or
          phone -> existing with the ORIGINAL bindings (Codex 2155 item 2).
- SA-P03  same reminder id, other appointment -> DISPATCH_CONFLICT, zero writes.
- SA-P04  unroutable: unknown appointment, cancelled, replaced, past, patient with
          no phone, patient with two phones, other tenant's appointment ->
          DISPATCH_UNROUTABLE.
- SA-P05  opted out before dispatch -> CONTACT_OPTED_OUT, no window (D-S2 row 4).
- SA-P06  wrong workload (ingress, timer, other), actor -> WORKLOAD_NOT_AUTHORIZED.
- SA-P07  malformed command shapes -> COMMAND_MALFORMED.
- SA-P08  concurrent dispatch vs STOP, both orders (held-lock seam): STOP first ->
          CONTACT_OPTED_OUT; dispatch first -> STOP sets the new window opted_out.
- SA-P09  concurrent same-reminder dispatch: second waits, then existing.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest
import sa_support as sa

pytestmark = pytest.mark.postgres

DIRECTORY = "m05_directory_put"
DISPATCH = "m05_register_dispatch"
INBOUND = "m05_register_inbound"


def _ready(install: sa.SAInstall) -> None:
    install.require()


# --- SA-T01 .. SA-T04 -----------------------------------------------------------------


def test_sa_t01_install_shape(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    applied = sa.admin(sa_install, "SELECT schema_version FROM shared.tenants WHERE tenant_id = %s",
                       (sa_install.tenant_id,))
    assert applied == [(sa.SA_TARGET,)]
    for table in sa.TABLES:
        rows = sa.admin(sa_install, "SELECT tableowner FROM pg_tables WHERE schemaname = %s"
                                    " AND tablename = %s", (sa_install.schema, table))
        assert rows == [(sa.MIGRATOR,)], table
        columns = sa.admin(sa_install, "SELECT column_name FROM information_schema.columns"
                                       " WHERE table_schema = %s AND table_name = %s",
                           (sa_install.schema, table))
        assert sorted(c for (c,) in columns) == sorted(sa.COLUMNS[table]), table
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            has = sa.admin(sa_install, "SELECT has_table_privilege(%s, %s, %s)",
                           (sa.RUNTIME, f"{sa_install.schema}.{table}", privilege))
            assert has == [(False,)], (table, privilege)
    names = sa.admin(sa_install, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n"
                                 " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                                 " AND p.proname LIKE 'm05\\_%%'", (sa_install.schema,))
    assert {n for (n,) in names} == sa.M05_NAMESPACE_SA
    helper = sa.admin(sa_install, "SELECT p.provolatile, p.prosecdef,"
                                  " pg_get_function_identity_arguments(p.oid)"
                                  " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                                  " WHERE n.nspname = %s AND p.proname = %s",
                      (sa_install.schema, sa.HELPER_FUNCTION))
    assert helper == [("i", False, "timestamp with time zone, timestamp with time zone")]


@pytest.mark.parametrize("table", sa.IMMUTABLE_TABLES)
def test_sa_t02_immutable_tables(sa_install: sa.SAInstall, table: str) -> None:
    _ready(sa_install)
    if table == "m05_inbound_replies":
        _, phone, _ = sa.fresh_patient(sa_install)
        sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    for statement in (f"UPDATE {{s}}.{table} SET {sa.COLUMNS[table][0]} ="
                      f" {sa.COLUMNS[table][0]}",
                      f"DELETE FROM {{s}}.{table}", f"TRUNCATE {{s}}.{table}"):
        with psycopg.connect(sa_install.ids.logins[sa.MIGRATOR], autocommit=True) as conn:
            if table == "m05_activation_records":
                conn.execute(sa.q(sa_install, "INSERT INTO {s}.m05_activation_records VALUES"
                                              " (gen_random_uuid(), 'revoke', %s, 'T-1', 'r',"
                                              " now())"), (sa.ACTOR,))
            with pytest.raises(psycopg.Error) as caught:
                conn.execute(sa.q(sa_install, statement))
            assert caught.value.sqlstate == "0A000", statement


def test_sa_t03_contracts(sa_bare_install: sa.SAInstall) -> None:
    install = sa_bare_install  # the units alone, no test profile
    install.require()
    family = sa.admin(install, sa.q(install, "SELECT owner_module, is_active FROM"
                                             " {s}.ref_action_families WHERE action_family = %s"),
                      (sa.FAMILY,))
    assert family == [("m05", False)]
    for event_type in sa.CONTRACT_TYPES:
        active = sa.admin(install, sa.q(install, "SELECT is_active FROM {s}.event_types"
                                                 " WHERE event_type = %s"), (event_type,))
        assert active == [(False,)], event_type
        rows = sa.admin(install, sa.q(install, "SELECT c.action_family, c.event_level,"
                                               " c.authorized_producers::text[],"
                                               " c.required_links, c.optional_links"
                                               " FROM {s}.event_contracts c"
                                               " WHERE c.event_type = %s AND c.version = 1"),
                        (event_type,))
        assert rows == [(sa.FAMILY, "observation", [sa.PRODUCER], [], [])], event_type
        paths = sa.admin(install, sa.q(install, "SELECT json_path FROM"
                                                " {s}.m02_envelope_path_allowlist WHERE"
                                                " consumer_module = 'm05' AND event_type = %s"
                                                " AND contract_version = 1 ORDER BY 1"),
                         (event_type,))
        assert tuple(p for (p,) in paths) == sa.ALLOWLIST[event_type]
    for table in ("m05_intake_workloads", "m05_intake_map"):
        rows = sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}." + table))
        assert rows == [(0,)], table  # no workload, no intake row from the units


def test_sa_t04_exact_boundary_predicate(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    d = "2026-11-06 12:00:00.000000+00"
    cases = (
        ("2026-11-06 11:59:59.999999+00", True),
        (d, False),
        ("2026-11-06 12:00:00.000001+00", False),
    )
    for received, expected in cases:
        got = sa.admin(sa_install, sa.q(sa_install, "SELECT {s}." + sa.HELPER_FUNCTION
                                                    + "(%s::timestamptz, %s::timestamptz)"),
                       (received, d))
        assert got == [(expected,)], received
    nulls = sa.admin(sa_install, sa.q(sa_install, "SELECT {s}." + sa.HELPER_FUNCTION
                                                  + "(NULL, %s::timestamptz), {s}."
                                                  + sa.HELPER_FUNCTION
                                                  + "(%s::timestamptz, NULL)"), (d, d))
    assert nulls == [(None, None)]


# --- SA-D01 / SA-D02 ---------------------------------------------------------------------


def test_sa_d01_directory_refusals_and_apply(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    patient, phone = sa.new_id(), "+15550177001"
    for principal in (sa.W_DISPATCH, sa.W_INGRESS, sa.W_TIMER, sa.W_OTHER):
        sa.refused(sa_install, DIRECTORY,
                   sa.directory_cmd("contact_put", principal, phone=phone, patient_id=patient),
                   "WORKLOAD_NOT_AUTHORIZED")
    actor = sa.directory_cmd("contact_put", phone=phone, patient_id=patient)
    actor["attribution"]["principal_kind"] = "actor"
    sa.refused(sa_install, DIRECTORY, actor, "WORKLOAD_NOT_AUTHORIZED")
    for bad in (
        {"op": "contact_put", "phone": "5550177001", "patient_id": patient},
        {"op": "contact_put", "phone": phone},
        {"op": "drop_all"},
        {"op": "appointment_put", "appointment_ref": sa.new_id(), "patient_id": patient,
         "start_at": sa.PAST, "status": "pending"},
        {"op": "contact_put", "phone": phone, "patient_id": patient, "tenant_id": "x"},
    ):
        command = {"schema_version": 1, **bad, "attribution": sa.attribution(sa.W_DIRECTORY)}
        sa.refused(sa_install, DIRECTORY, command, "COMMAND_MALFORMED")
    assert sa.directory(sa_install, "contact_put", phone=phone, patient_id=patient) == {
        "schema_version": 1, "outcome": "applied"}
    ref = sa.new_id()
    sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=patient,
                 start_at=sa.FUTURE.format(n=1), status="booked")
    rows = sa.admin(sa_install, sa.q(sa_install, "SELECT patient_id::text, status FROM"
                                                 " {s}.m05_appointments WHERE appointment_ref"
                                                 " = %s"), (ref,))
    assert rows == [(patient, "booked")]
    sa.directory(sa_install, "contact_remove", phone=phone, patient_id=patient)
    gone = sa.admin(sa_install, sa.q(sa_install, "SELECT count(*) FROM {s}.m05_patient_contacts"
                                                 " WHERE phone = %s"), (phone,))
    assert gone == [(0,)]


def test_sa_d02_directory_waits_for_registration_lock(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    patient, phone, (ref,) = sa.fresh_patient(sa_install)
    reminder = sa.new_id()
    # Registration holds the contact lock; a directory change for that phone waits.
    with sa.held(sa_install, DISPATCH, sa.dispatch_cmd(reminder, ref)) as result:
        assert result["outcome"] == "registered"
        waiter = sa.Waiter(sa_install, DIRECTORY, sa.directory_cmd(
            "appointment_put", appointment_ref=ref, patient_id=patient,
            start_at=sa.FUTURE.format(n=1), status="cancelled")).start_and_block()
    assert waiter.join()["outcome"] == "applied"
    # Reverse: a directory change holds the lock; dispatch waits, then sees it.
    other_patient, other_phone, (other_ref,) = sa.fresh_patient(sa_install)
    with sa.held(sa_install, DIRECTORY, sa.directory_cmd(
            "appointment_put", appointment_ref=other_ref, patient_id=other_patient,
            start_at=sa.FUTURE.format(n=1), status="cancelled")):
        waiter = sa.Waiter(sa_install, DISPATCH,
                           sa.dispatch_cmd(sa.new_id(), other_ref)).start_and_block()
    with pytest.raises(psycopg.Error) as caught:
        waiter.join()
    assert caught.value.diag.message_primary == "DISPATCH_UNROUTABLE"
    # Moving an appointment to another patient locks both patients' phones.
    third, third_phone, _ = sa.fresh_patient(sa_install, appointments=0)
    with sa.held(sa_install, INBOUND, sa.inbound_cmd(sa.new_id(), third_phone, "confirm")):
        waiter = sa.Waiter(sa_install, DIRECTORY, sa.directory_cmd(
            "appointment_put", appointment_ref=other_ref, patient_id=third,
            start_at=sa.FUTURE.format(n=2), status="booked")).start_and_block()
    assert waiter.join()["outcome"] == "applied"
    assert other_phone != third_phone


def test_sa_d03_first_contact_insertion_vs_inbound(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    # Inbound first: the phone has no contact row and no directory entry yet.
    patient, phone = sa.new_id(), sa.fresh_phone()
    with sa.held(sa_install, INBOUND, sa.inbound_cmd(sa.new_id(), phone, "confirm")) as first:
        assert first["decision"] == "quarantined"
        waiter = sa.Waiter(sa_install, DIRECTORY, sa.directory_cmd(
            "contact_put", phone=phone, patient_id=patient)).start_and_block()
    assert waiter.join()["outcome"] == "applied"
    # contact_put first (first contact for a new patient): inbound waits, sees it.
    patient2, phone2 = sa.new_id(), sa.fresh_phone()
    with sa.held(sa_install, DIRECTORY, sa.directory_cmd(
            "contact_put", phone=phone2, patient_id=patient2)):
        waiter = sa.Waiter(sa_install, INBOUND,
                           sa.inbound_cmd(sa.new_id(), phone2, "confirm")).start_and_block()
    after = waiter.join()
    assert (after["decision"], after["triage_kind"], after["patient_id"]) == (
        "item", "unclear_reply", patient2)


def test_sa_d04_contact_change_vs_dispatch(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    # Directory first: a second phone for the patient -> dispatch (after view) refuses.
    patient, _, (ref,) = sa.fresh_patient(sa_install)
    with sa.held(sa_install, DIRECTORY, sa.directory_cmd(
            "contact_put", phone=sa.fresh_phone(), patient_id=patient)):
        waiter = sa.Waiter(sa_install, DISPATCH,
                           sa.dispatch_cmd(sa.new_id(), ref)).start_and_block()
    with pytest.raises(psycopg.Error) as caught:
        waiter.join()
    assert caught.value.diag.message_primary == "DISPATCH_UNROUTABLE"
    # Dispatch first: the contact change waits; the window keeps the original phone.
    patient2, phone2, (ref2,) = sa.fresh_patient(sa_install)
    reminder = sa.new_id()
    with sa.held(sa_install, DISPATCH, sa.dispatch_cmd(reminder, ref2)):
        waiter = sa.Waiter(sa_install, DIRECTORY, sa.directory_cmd(
            "contact_remove", phone=phone2, patient_id=patient2)).start_and_block()
    assert waiter.join()["outcome"] == "applied"
    assert sa.window(sa_install, reminder)["phone"] == phone2


# --- SA-P01 .. SA-P09 ----------------------------------------------------------------------


def _registered(result: dict[str, Any], reminder: str, ref: str, patient: str,
                phone: str) -> None:
    assert sorted(result) == sorted((
        "schema_version", "outcome", "reminder_id", "appointment_ref", "patient_id", "phone",
        "sent_at", "deadline", "event_id"))
    assert result["schema_version"] == 1
    for key in ("sent_at", "deadline"):
        assert re.fullmatch(sa.TIMESTAMP_PATTERN, result[key]), result[key]
    assert (result["reminder_id"], result["appointment_ref"], result["patient_id"],
            result["phone"]) == (reminder, ref, patient, phone)


def test_sa_p01_dispatch_registers(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    patient, phone, (ref,) = sa.fresh_patient(sa_install)
    reminder = sa.new_id()
    events_before = sa.admin(sa_install, sa.q(sa_install, "SELECT count(*) FROM"
                                                          " {s}.patient_events"))
    with sa.runtime_conn(sa_install) as conn, conn.transaction():
        start = conn.execute("SELECT transaction_timestamp()").fetchone()
        result = sa.call_in(conn, sa_install, DISPATCH, sa.dispatch_cmd(reminder, ref))
    assert result["outcome"] == "registered"
    _registered(result, reminder, ref, patient, phone)
    w = sa.window(sa_install, reminder)
    assert w["state"] == "awaiting" and w["closed_decision"] is None
    check = sa.admin(sa_install, sa.q(sa_install,
                                      "SELECT deadline = sent_at + interval '24 hours',"
                                      " sent_at >= %s, encode(dispatch_fingerprint, 'hex')"
                                      " FROM {s}.m05_reminder_windows WHERE reminder_id = %s"),
                     (start[0] if start else None, reminder))
    expected = sa.dispatch_fingerprint(sa_install.tenant_id, reminder, ref).hex()
    assert check == [(True, True, expected)]
    assert result["event_id"] == w["dispatch_event_id"]
    # SQL registration appends nothing; the observation is the service's (SA-V rows).
    assert sa.admin(sa_install, sa.q(sa_install, "SELECT count(*) FROM {s}.patient_events")) \
        == events_before


def test_sa_p02_retry_returns_original(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    patient, phone, (ref,) = sa.fresh_patient(sa_install)
    reminder = sa.new_id()
    first = sa.dispatch(sa_install, reminder, ref)
    stored = sa.window(sa_install, reminder)
    second = sa.dispatch(sa_install, reminder, ref)
    assert second == {**first, "outcome": "existing"}
    assert sa.window(sa_install, reminder) == stored
    # A later STOP: the retry still returns the stored original (not opted out).
    sa.inbound(sa_install, sa.new_id(), phone, "stop")
    after_stop = sa.dispatch(sa_install, reminder, ref)
    assert after_stop == {**first, "outcome": "existing"}
    # Directory moves the appointment to another patient on another phone.
    moved_to, _, _ = sa.fresh_patient(sa_install, appointments=0)
    sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=moved_to,
                 start_at=sa.FUTURE.format(n=3), status="booked")
    after_move = sa.dispatch(sa_install, reminder, ref)
    assert after_move == {**first, "outcome": "existing"}
    w = sa.window(sa_install, reminder)
    assert (w["patient_id"], w["phone"], w["sent_at"], w["deadline"]) == (
        patient, phone, stored["sent_at"], stored["deadline"])


def test_sa_p03_conflict(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    _, _, (ref_a, ref_b) = sa.fresh_patient(sa_install, appointments=2)
    reminder = sa.new_id()
    sa.dispatch(sa_install, reminder, ref_a)
    sa.refused(sa_install, DISPATCH, sa.dispatch_cmd(reminder, ref_b), "DISPATCH_CONFLICT")


def test_sa_p04_unroutable(sa_install: sa.SAInstall, sa_other_install: sa.SAInstall) -> None:
    _ready(sa_install)
    sa_other_install.require()
    cases: dict[str, str] = {"unknown": sa.new_id()}
    for status in ("cancelled", "replaced"):
        patient, _, (ref,) = sa.fresh_patient(sa_install)
        sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=patient,
                     start_at=sa.FUTURE.format(n=1), status=status)
        cases[status] = ref
    patient, _, (past_ref,) = sa.fresh_patient(sa_install)
    sa.directory(sa_install, "appointment_put", appointment_ref=past_ref, patient_id=patient,
                 start_at=sa.PAST, status="booked")
    cases["past"] = past_ref
    no_phone = sa.new_id()
    ref = sa.new_id()
    sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=no_phone,
                 start_at=sa.FUTURE.format(n=1), status="booked")
    cases["no-phone"] = ref
    two_phones, _, (two_ref,) = sa.fresh_patient(sa_install)
    sa.directory(sa_install, "contact_put", phone="+15550188002", patient_id=two_phones)
    cases["two-phones"] = two_ref
    _, _, (foreign_ref,) = sa.fresh_patient(sa_other_install)
    cases["other-tenant"] = foreign_ref
    for name, appointment in cases.items():
        try:
            sa.refused(sa_install, DISPATCH, sa.dispatch_cmd(sa.new_id(), appointment),
                       "DISPATCH_UNROUTABLE")
        except AssertionError as error:
            raise AssertionError(name) from error


def test_sa_p05_opted_out_before_dispatch(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    _, phone, (ref,) = sa.fresh_patient(sa_install)
    assert sa.inbound(sa_install, sa.new_id(), phone, "stop")["decision"] == "opt_out"
    sa.refused(sa_install, DISPATCH, sa.dispatch_cmd(sa.new_id(), ref), "CONTACT_OPTED_OUT")


def test_sa_p06_wrong_workload(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    _, _, (ref,) = sa.fresh_patient(sa_install)
    for principal in (sa.W_INGRESS, sa.W_TIMER, sa.W_DIRECTORY, sa.W_OTHER):
        sa.refused(sa_install, DISPATCH, sa.dispatch_cmd(sa.new_id(), ref, principal),
                   "WORKLOAD_NOT_AUTHORIZED")
    actor = sa.dispatch_cmd(sa.new_id(), ref, sa.W_DISPATCH)
    actor["attribution"]["principal_kind"] = "actor"
    sa.refused(sa_install, DISPATCH, actor, "WORKLOAD_NOT_AUTHORIZED")


MALFORMED: dict[str, Any] = {
    "not-object": [],
    "extra-key": {"patient_id": "00000000-0000-4000-8000-00000005b001"},
    "missing-appointment": {"appointment_ref": None},
    "reminder-not-uuid": {"reminder_id": "r-1"},
    "schema-2": {"schema_version": 2},
    "attribution-missing-key": {"attribution": {"principal_kind": "workload"}},
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_sa_p07_malformed(sa_install: sa.SAInstall, case: str) -> None:
    _ready(sa_install)
    _, _, (ref,) = sa.fresh_patient(sa_install)
    change = MALFORMED[case]
    if isinstance(change, list):
        command: Any = change
    else:
        command = {**sa.dispatch_cmd(sa.new_id(), ref), **change}
        if change.get("appointment_ref", 0) is None:
            del command["appointment_ref"]
    sa.refused(sa_install, DISPATCH, command, "COMMAND_MALFORMED")


def test_sa_p08_dispatch_vs_stop_both_orders(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    # STOP first holds the contact lock -> dispatch waits -> CONTACT_OPTED_OUT.
    _, phone, (ref,) = sa.fresh_patient(sa_install)
    with sa.held(sa_install, INBOUND, sa.inbound_cmd(sa.new_id(), phone, "stop")) as stop:
        assert stop["decision"] == "opt_out"
        waiter = sa.Waiter(sa_install, DISPATCH, sa.dispatch_cmd(sa.new_id(), ref))
        waiter.start_and_block()
    with pytest.raises(psycopg.Error) as caught:
        waiter.join()
    assert caught.value.diag.message_primary == "CONTACT_OPTED_OUT"
    # Dispatch first holds the lock -> STOP waits -> the new window becomes opted_out.
    _, phone2, (ref2,) = sa.fresh_patient(sa_install)
    reminder = sa.new_id()
    with sa.held(sa_install, DISPATCH, sa.dispatch_cmd(reminder, ref2)):
        waiter = sa.Waiter(sa_install, INBOUND, sa.inbound_cmd(sa.new_id(), phone2, "stop"))
        waiter.start_and_block()
    assert waiter.join()["decision"] == "opt_out"
    assert sa.window(sa_install, reminder)["state"] == "opted_out"


def test_sa_p09_concurrent_same_reminder(sa_install: sa.SAInstall) -> None:
    _ready(sa_install)
    _, _, (ref,) = sa.fresh_patient(sa_install)
    reminder = sa.new_id()
    with sa.held(sa_install, DISPATCH, sa.dispatch_cmd(reminder, ref)) as first:
        waiter = sa.Waiter(sa_install, DISPATCH, sa.dispatch_cmd(reminder, ref))
        waiter.start_and_block()
    assert waiter.join() == {**first, "outcome": "existing"}
    count = sa.admin(sa_install, sa.q(sa_install, "SELECT count(*) FROM {s}.m05_reminder_windows"
                                                  " WHERE reminder_id = %s"), (reminder,))
    assert count == [(1,)]
