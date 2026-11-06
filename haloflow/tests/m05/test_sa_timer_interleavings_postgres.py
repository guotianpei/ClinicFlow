"""L-1S package S-A — timer (`m05_close_window`) and SA-TIME-1 interleavings, PG17.

Pins v1 section 4 (t041); SA-TIME-1 sections 3, 4, 6; Codex 2113 items 1, 4, 6 and
2155 item 1. Time is controlled by SEEDING (fixture `shift_window`) and by the
HELD-LOCK SEAM (`sa.held` keeps the first transaction and its locks open while a
second connection is observed blocked in pg_stat_activity); never by sleeping. The
exact instant `received_at = deadline` is covered by the predicate row SA-T04 and
the static rule SA-S06 (every deadline comparison in t040/t041 goes through it);
these rows do not claim equal-instant coverage from timing.

- SA-C01  awaiting, not yet due -> not_due, window unchanged, nothing stored.
          Check order (pins v1r1 C8): stored close -> existing; state <> awaiting ->
          not_awaiting; not due -> not_due; opted out -> opted_out; else no_reply.
- SA-C02  due, awaiting -> closed / no_reply; closed_event_id generated and stored;
          SQL appends nothing (the service does).
- SA-C03  rerun after close -> existing with the STORED decision and event id
          (lost-ack recovery is explicit, never a misleading no-op).
- SA-C04  STOP before the deadline -> window opted_out, timer not_awaiting; STOP
          after the deadline (late, window still awaiting) -> timer closes it
          opted_out, no event id (D-S2 rows 4/5: no no_reply for anyone opted out).
- SA-C03b a window closed opted_out: rerun -> existing, decision opted_out, event
          id null. Persisted terminal values (closed_decision): no_reply (with
          closed_event_id) and opted_out (without); `not_due` and `not_awaiting`
          persist nothing.
- SA-C05  replied / opted_out windows -> not_awaiting, unchanged.
- SA-C06  ambiguous reply left windows awaiting -> each closes no_reply later
          (v1r1 section 1 consequence).
- SA-C07  unknown reminder -> WINDOW_NOT_FOUND; wrong workload / actor ->
          WORKLOAD_NOT_AUTHORIZED; malformed -> COMMAND_MALFORMED.
- SA-C08  clock assumption check (Codex 2113 item 6): every stored received_at and
          closing time is >= the window's sent_at (a stated assumption, not a proof).
- SA-X01  ingress STOP holds the contact lock (timely) -> the timer WAITS; after the
          commit it finds opted_out -> not_awaiting, never no_reply (also after the
          deadline is later seeded past).
- SA-X02  timer holds the lock with the deadline past -> ingress STOP WAITS; its
          received_at > deadline -> late; no_reply stands; contact opted out.
- SA-X03  the same pair as X01/X02 for an actionable reply (reschedule): ingress
          first -> replied, timer not_awaiting; timer first -> no_reply, then the
          reply is late, its own item, window stays no_reply (D-S2 row 6).
- SA-X04  received_at is taken AFTER the lock: a reply that starts while the timer
          holds the lock has received_at > the timer's close time.
"""

from __future__ import annotations

from typing import Any

import pytest
import sa_support as sa

pytestmark = pytest.mark.postgres

CLOSE = "m05_close_window"
INBOUND = "m05_register_inbound"
RESULT_KEYS = ("schema_version", "outcome", "decision", "event_id", "patient_id",
               "appointment_ref")


def _one(install: sa.SAInstall) -> tuple[str, str, str, str]:
    install.require()
    patient, phone, (ref,) = sa.fresh_patient(install)
    reminder = sa.new_id()
    sa.dispatch(install, reminder, ref)
    return patient, phone, ref, reminder


def _events(install: sa.SAInstall) -> Any:
    return sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.patient_events"))[0][0]


def test_sa_c01_not_due(sa_install: sa.SAInstall) -> None:
    _, _, _, reminder = _one(sa_install)
    before = sa.window(sa_install, reminder)
    result = sa.close(sa_install, reminder)
    assert sorted(result) == sorted(RESULT_KEYS)
    assert (result["outcome"], result["decision"], result["event_id"]) == (
        "not_due", None, None)
    assert sa.window(sa_install, reminder) == before


def test_sa_c02_due_closes_no_reply(sa_install: sa.SAInstall) -> None:
    patient, _, ref, reminder = _one(sa_install)
    sa.shift_window(sa_install, reminder, -25)
    events = _events(sa_install)
    result = sa.close(sa_install, reminder)
    assert (result["outcome"], result["decision"], result["patient_id"],
            result["appointment_ref"]) == ("closed", "no_reply", patient, ref)
    w = sa.window(sa_install, reminder)
    assert (w["state"], w["closed_decision"], w["closed_event_id"]) == (
        "no_reply", "no_reply", result["event_id"])
    assert result["event_id"] is not None
    assert _events(sa_install) == events


def test_sa_c03_rerun_returns_stored(sa_install: sa.SAInstall) -> None:
    _, _, _, reminder = _one(sa_install)
    sa.shift_window(sa_install, reminder, -25)
    first = sa.close(sa_install, reminder)
    before = sa.snapshot(sa_install)
    again = sa.close(sa_install, reminder)
    assert again == {**first, "outcome": "existing"}
    assert sa.snapshot(sa_install) == before


def test_sa_c03b_opted_out_close_is_stored(sa_install: sa.SAInstall) -> None:
    _, phone, _, reminder = _one(sa_install)
    sa.shift_window(sa_install, reminder, -25)
    sa.inbound(sa_install, sa.new_id(), phone, "stop")  # late: window stays awaiting
    first = sa.close(sa_install, reminder)
    assert (first["outcome"], first["decision"], first["event_id"]) == (
        "closed", "opted_out", None)
    w = sa.window(sa_install, reminder)
    assert (w["closed_decision"], w["closed_event_id"]) == ("opted_out", None)
    again = sa.close(sa_install, reminder)
    assert again == {**first, "outcome": "existing"}


@pytest.mark.parametrize("stop_when", ("before_deadline", "after_deadline"))
def test_sa_c04_opted_out(sa_install: sa.SAInstall, stop_when: str) -> None:
    _, phone, _, reminder = _one(sa_install)
    if stop_when == "after_deadline":
        sa.shift_window(sa_install, reminder, -25)
        sa.inbound(sa_install, sa.new_id(), phone, "stop")  # late STOP: window stays awaiting
        assert sa.window(sa_install, reminder)["state"] == "awaiting"
    else:
        sa.inbound(sa_install, sa.new_id(), phone, "stop")
        assert sa.window(sa_install, reminder)["state"] == "opted_out"
        sa.shift_window(sa_install, reminder, -25)
    result = sa.close(sa_install, reminder)
    expected_outcome = "closed" if stop_when == "after_deadline" else "not_awaiting"
    assert (result["outcome"], result["event_id"]) == (expected_outcome, None)
    assert sa.window(sa_install, reminder)["state"] == "opted_out"
    if stop_when == "after_deadline":
        assert result["decision"] == "opted_out"
        assert sa.window(sa_install, reminder)["closed_decision"] == "opted_out"


def test_sa_c05_not_awaiting(sa_install: sa.SAInstall) -> None:
    _, phone, _, reminder = _one(sa_install)
    sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    sa.shift_window(sa_install, reminder, -25)
    stored = sa.window(sa_install, reminder)
    result = sa.close(sa_install, reminder)
    assert (result["outcome"], result["decision"], result["event_id"]) == (
        "not_awaiting", None, None)
    assert sa.window(sa_install, reminder) == stored


def test_sa_c06_ambiguous_then_no_reply(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    _, phone, refs = sa.fresh_patient(sa_install, appointments=2)
    reminders = [sa.new_id(), sa.new_id()]
    for reminder, ref in zip(reminders, refs, strict=True):
        sa.dispatch(sa_install, reminder, ref)
    assert sa.inbound(sa_install, sa.new_id(), phone, "reschedule")["triage_kind"] \
        == "unclear_reply"
    for reminder in reminders:
        sa.shift_window(sa_install, reminder, -25)
        assert sa.close(sa_install, reminder)["decision"] == "no_reply"


def test_sa_c07_refusals(sa_install: sa.SAInstall) -> None:
    _, _, _, reminder = _one(sa_install)
    sa.refused(sa_install, CLOSE, sa.close_cmd(sa.new_id()), "WINDOW_NOT_FOUND")
    for principal in (sa.W_DISPATCH, sa.W_INGRESS, sa.W_DIRECTORY, sa.W_OTHER):
        sa.refused(sa_install, CLOSE, sa.close_cmd(reminder, principal),
                   "WORKLOAD_NOT_AUTHORIZED")
    actor = sa.close_cmd(reminder)
    actor["attribution"]["principal_kind"] = "actor"
    sa.refused(sa_install, CLOSE, actor, "WORKLOAD_NOT_AUTHORIZED")
    for bad in ({"reminder_id": "x"}, {"now": "2099-01-01T00:00:00Z"},
                {"schema_version": 2}):
        sa.refused(sa_install, CLOSE, {**sa.close_cmd(reminder), **bad}, "COMMAND_MALFORMED")


def test_sa_c08_clock_assumption(sa_install: sa.SAInstall) -> None:
    _, phone, _, reminder = _one(sa_install)
    sa.inbound(sa_install, sa.new_id(), phone, "unclear")
    rows = sa.admin(sa_install, sa.q(sa_install,
                                     "SELECT bool_and(r.received_at >= w.sent_at)"
                                     " FROM {s}.m05_inbound_replies r"
                                     " JOIN {s}.m05_reminder_windows w"
                                     " ON w.reminder_id = ANY (r.matched_reminders)"))
    assert rows == [(True,)]


# --- interleavings ------------------------------------------------------------------


def test_sa_x01_stop_first_timer_waits(sa_install: sa.SAInstall) -> None:
    _, phone, _, reminder = _one(sa_install)
    # Timing-free: the STOP is timely (deadline 24 h ahead). The timer checks
    # `state <> awaiting` BEFORE due-ness (pins v1r1 C8), so a timer that waited on the
    # contact lock observes the committed transition whatever its clock says.
    with sa.held(sa_install, INBOUND, sa.inbound_cmd(sa.new_id(), phone, "stop")) as stop:
        assert stop["decision"] == "opt_out"
        waiter = sa.Waiter(sa_install, CLOSE, sa.close_cmd(reminder)).start_and_block()
    result = waiter.join()
    assert (result["outcome"], result["event_id"]) == ("not_awaiting", None)
    sa.shift_window(sa_install, reminder, -25)
    assert sa.close(sa_install, reminder)["outcome"] == "not_awaiting"
    assert sa.window(sa_install, reminder)["state"] == "opted_out"


def test_sa_x02_timer_first_stop_waits(sa_install: sa.SAInstall) -> None:
    _, phone, _, reminder = _one(sa_install)
    sa.shift_window(sa_install, reminder, -25)
    with sa.held(sa_install, CLOSE, sa.close_cmd(reminder)) as closed:
        assert closed["decision"] == "no_reply"
        waiter = sa.Waiter(sa_install, INBOUND,
                           sa.inbound_cmd(sa.new_id(), phone, "stop")).start_and_block()
    stop = waiter.join()
    assert stop["decision"] == "opt_out"
    w = sa.window(sa_install, reminder)
    assert (w["state"], w["closed_decision"]) == ("no_reply", "no_reply")
    late = sa.admin(sa_install, sa.q(sa_install,
                                     "SELECT r.received_at >= w.deadline FROM"
                                     " {s}.m05_inbound_replies r, {s}.m05_reminder_windows w"
                                     " WHERE r.inbound_id = %s AND w.reminder_id = %s"),
                    (stop["inbound_id"], reminder))
    assert late == [(True,)]
    c = sa.contact(sa_install, phone)
    assert c is not None and c["opted_out"] is True


def test_sa_x03_actionable_both_orders(sa_install: sa.SAInstall) -> None:
    # Ingress first.
    _, phone, _, reminder = _one(sa_install)
    with sa.held(sa_install, INBOUND, sa.inbound_cmd(sa.new_id(), phone, "reschedule")) as r:
        assert r["decision"] == "item"
        waiter = sa.Waiter(sa_install, CLOSE, sa.close_cmd(reminder)).start_and_block()
    assert waiter.join()["outcome"] == "not_awaiting"
    assert sa.window(sa_install, reminder)["state"] == "replied"
    # Timer first.
    _, phone2, ref2, reminder2 = _one(sa_install)
    sa.shift_window(sa_install, reminder2, -25)
    with sa.held(sa_install, CLOSE, sa.close_cmd(reminder2)):
        waiter = sa.Waiter(sa_install, INBOUND,
                           sa.inbound_cmd(sa.new_id(), phone2, "reschedule")).start_and_block()
    reply = waiter.join()
    assert (reply["decision"], reply["triage_kind"], reply["reminder_id"]) == (
        "item", "reschedule_request", reminder2)
    assert sa.window(sa_install, reminder2)["state"] == "no_reply"
    assert ref2 == reply["appointment_ref"]


def test_sa_x04_received_at_after_lock(sa_install: sa.SAInstall) -> None:
    _, phone, _, reminder = _one(sa_install)
    sa.shift_window(sa_install, reminder, -25)
    with sa.runtime_conn(sa_install) as conn, conn.transaction():
        sa.call_in(conn, sa_install, CLOSE, sa.close_cmd(reminder))
        held_at = conn.execute("SELECT clock_timestamp()").fetchone()
        waiter = sa.Waiter(sa_install, INBOUND,
                           sa.inbound_cmd(sa.new_id(), phone, "confirm")).start_and_block()
    reply = waiter.join()
    later = sa.admin(sa_install, sa.q(sa_install, "SELECT received_at > %s FROM"
                                                  " {s}.m05_inbound_replies WHERE inbound_id"
                                                  " = %s"),
                     (held_at[0] if held_at else None, reply["inbound_id"]))
    assert later == [(True,)]
