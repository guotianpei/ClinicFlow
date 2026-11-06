"""L-1S package S-A — inbound registration (`m05_register_inbound`) on PostgreSQL 17.

Pins v1 section 4 (t040); SA-TIME-1 section 3; architecture v1 section 2 with the
v1r1 delta (ambiguity changes no window; authoritative directory identity); owner
decision D-S2 rows 1-7. SQL decides and records; observations and items are the
service's (test_sa_service_postgres.py), so these rows assert the DECISION, the
stored inbound row and the window transitions, and that SQL wrote no event/item.

- SA-I01  shape and codes; result keys; stored row equals the result; fingerprint
          equals the independent oracle; received_at UTC-Z.
- SA-I02  redelivery, same content -> existing, identical result, nothing new
          (R-SA-2); same id, other class -> INBOUND_CONFLICT, original immutable,
          zero writes; same id other phone -> INBOUND_CONFLICT.
- SA-I03  D-S2 row 1: STOP -> opt_out on the number, every awaiting timely window of
          the phone (all patients' windows on that phone) -> opted_out, decision
          opt_out, no event id; a second STOP is a new message, still opt_out.
- SA-I04  D-S2 rows 2/3: actionable reply then STOP, and STOP then actionable reply
          -> the actionable one is decision item with its kind, correlated to the
          (now opted_out) window (Codex 2db40c9d item 3: any state).
- SA-I05  kinds (R-SA-9) with exactly one current window: confirm -> recorded,
          window replied; reschedule/cancel/unclear -> item + kind, window replied.
- SA-I06  repeated reply after a prior reply: correlates to the replied window, item
          for actionable, window unchanged.
- SA-I07  D-S2 row 7 + v1r1 section 1: two current windows (two same-day
          appointments texted) -> unclear_reply for ANY class, appointment_ref and
          reminder_id null, BOTH windows stay awaiting; zero current windows (no
          window, or only past/cancelled appointments) -> unclear_reply.
- SA-I08  identity (R-SA-6, v1r1 section 2): unknown number -> quarantined; number
          shared by two patients where only one has a window -> quarantined (the
          directory counts, not windows); windows untouched; quarantined rows kept and
          countable.
- SA-I09  late reply (received_at >= deadline) registered BEFORE the timer: decision
          item, window stays awaiting (the timer will close it, SA-C rows).
- SA-I10  reply after the window closed no_reply (D-S2 row 6): item; window stays
          no_reply.
- SA-I11  wrong workload / actor -> WORKLOAD_NOT_AUTHORIZED; malformed ->
          COMMAND_MALFORMED; cross-tenant: a phone known only in another tenant is
          quarantined here (never looked up there).
- SA-I13  stale correlation (Codex 1913 item 2): reminder for patient A, appointment
          then reassigned to B; A replies -> unclear_reply, no appointment or reminder
          attributed, A's window unchanged, nothing bound to B's appointment.
- SA-I12  SQL appends no event and creates no item (the service does, atomically).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import sa_support as sa

pytestmark = pytest.mark.postgres

INBOUND = "m05_register_inbound"
RESULT_KEYS = ("schema_version", "outcome", "inbound_id", "decision", "triage_kind",
               "patient_id", "appointment_ref", "reminder_id", "received_at", "event_id")


def _setup(install: sa.SAInstall, appointments: int = 1) -> tuple[str, str, list[str], list[str]]:
    """A patient with `appointments` booked appointments, each dispatched."""

    install.require()
    patient, phone, refs = sa.fresh_patient(install, appointments=appointments)
    reminders = [sa.new_id() for _ in refs]
    for reminder, ref in zip(reminders, refs, strict=True):
        assert sa.dispatch(install, reminder, ref)["outcome"] == "registered"
    return patient, phone, refs, reminders


def _stored(install: sa.SAInstall, inbound_id: str) -> dict[str, Any]:
    rows = sa.admin(install, sa.q(install, "SELECT to_jsonb(r) FROM {s}.m05_inbound_replies r"
                                           " WHERE inbound_id = %s"), (inbound_id,))
    assert len(rows) == 1, rows
    value: dict[str, Any] = rows[0][0]
    return value


def _counts(install: sa.SAInstall) -> tuple[Any, ...]:
    return (sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.patient_events"))[0][0],
            sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.m05_followup_items"))[0][0])


def test_sa_i01_shape(sa_install: sa.SAInstall) -> None:
    patient, phone, (ref,), (reminder,) = _setup(sa_install)
    message = sa.new_id()
    result = sa.inbound(sa_install, message, phone, "reschedule")
    assert sorted(result) == sorted(RESULT_KEYS)
    assert result["schema_version"] == 1 and result["outcome"] == "registered"
    assert (result["decision"], result["triage_kind"], result["patient_id"],
            result["appointment_ref"], result["reminder_id"]) == (
        "item", "reschedule_request", patient, ref, reminder)
    assert re.fullmatch(sa.TIMESTAMP_PATTERN, result["received_at"])
    assert result["event_id"] is not None
    stored = _stored(sa_install, result["inbound_id"])
    assert (stored["channel"], stored["provider_message_id"], stored["phone"],
            stored["reply_class"], stored["decision"], stored["triage_kind"],
            stored["event_id"], stored["matched_reminders"]) == (
        "sms", message, phone, "reschedule", "item", "reschedule_request",
        result["event_id"], [reminder])
    fp = sa.admin(sa_install, sa.q(sa_install, "SELECT encode(fingerprint, 'hex') FROM"
                                               " {s}.m05_inbound_replies WHERE inbound_id = %s"),
                  (result["inbound_id"],))
    assert fp == [(sa.inbound_fingerprint(sa_install.tenant_id, "sms", message, phone,
                                          "reschedule").hex(),)]


def test_sa_i02_redelivery_and_conflict(sa_install: sa.SAInstall) -> None:
    _, phone, _, _ = _setup(sa_install)
    message = sa.new_id()
    first = sa.inbound(sa_install, message, phone, "cancel")
    before = sa.snapshot(sa_install)
    again = sa.inbound(sa_install, message, phone, "cancel")
    assert again == {**first, "outcome": "existing"}
    assert sa.snapshot(sa_install) == before
    sa.refused(sa_install, INBOUND, sa.inbound_cmd(message, phone, "confirm"),
               "INBOUND_CONFLICT")
    sa.refused(sa_install, INBOUND, sa.inbound_cmd(message, "+15550199999", "cancel"),
               "INBOUND_CONFLICT")
    assert _stored(sa_install, first["inbound_id"])["reply_class"] == "cancel"


def test_sa_i03_stop_is_phone_wide(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    phone = sa.fresh_phone()
    patients = (sa.new_id(), sa.new_id())
    reminders = []
    for patient in patients:
        sa.directory(sa_install, "contact_put", phone=phone, patient_id=patient)
        ref = sa.new_id()
        sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=patient,
                     start_at=sa.FUTURE.format(n=1), status="booked")
        reminder = sa.new_id()
        sa.dispatch(sa_install, reminder, ref)
        reminders.append(reminder)
    events = _counts(sa_install)
    result = sa.inbound(sa_install, sa.new_id(), phone, "stop")
    assert (result["decision"], result["triage_kind"], result["event_id"]) == (
        "opt_out", None, None)
    assert [sa.window(sa_install, r)["state"] for r in reminders] == ["opted_out", "opted_out"]
    c = sa.contact(sa_install, phone)
    assert c is not None and c["opted_out"] is True and c["opted_out_at"] is not None
    assert _counts(sa_install) == events
    assert sa.inbound(sa_install, sa.new_id(), phone, "stop")["decision"] == "opt_out"


def test_sa_i04_actionable_with_stop_both_orders(sa_install: sa.SAInstall) -> None:
    # Row 2: reply, then STOP.
    _, phone, (ref,), (reminder,) = _setup(sa_install)
    first = sa.inbound(sa_install, sa.new_id(), phone, "reschedule")
    assert first["decision"] == "item"
    assert sa.inbound(sa_install, sa.new_id(), phone, "stop")["decision"] == "opt_out"
    assert sa.window(sa_install, reminder)["state"] == "replied"
    # Row 3: STOP, then reply: still its own item, correlated to the opted_out window.
    _, phone2, (ref2,), (reminder2,) = _setup(sa_install)
    sa.inbound(sa_install, sa.new_id(), phone2, "stop")
    late = sa.inbound(sa_install, sa.new_id(), phone2, "cancel")
    assert (late["decision"], late["triage_kind"], late["appointment_ref"],
            late["reminder_id"]) == ("item", "cancel_request", ref2, reminder2)
    assert sa.window(sa_install, reminder2)["state"] == "opted_out"


@pytest.mark.parametrize(("reply_class", "decision", "kind"), (
    ("confirm", "recorded", None),
    ("reschedule", "item", "reschedule_request"),
    ("cancel", "item", "cancel_request"),
    ("unclear", "item", "unclear_reply"),
))
def test_sa_i05_kinds(sa_install: sa.SAInstall, reply_class: str, decision: str,
                      kind: str | None) -> None:
    _, phone, (ref,), (reminder,) = _setup(sa_install)
    result = sa.inbound(sa_install, sa.new_id(), phone, reply_class)
    assert (result["decision"], result["triage_kind"], result["appointment_ref"],
            result["reminder_id"]) == (decision, kind, ref, reminder)
    assert result["event_id"] is not None
    assert sa.window(sa_install, reminder)["state"] == "replied"


def test_sa_i06_repeated_reply(sa_install: sa.SAInstall) -> None:
    _, phone, (ref,), (reminder,) = _setup(sa_install)
    sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    stored = sa.window(sa_install, reminder)
    again = sa.inbound(sa_install, sa.new_id(), phone, "reschedule")
    assert (again["decision"], again["triage_kind"], again["reminder_id"]) == (
        "item", "reschedule_request", reminder)
    assert sa.window(sa_install, reminder) == stored


@pytest.mark.parametrize("reply_class", ("confirm", "reschedule", "cancel", "unclear"))
def test_sa_i07_ambiguous_changes_no_window(sa_install: sa.SAInstall, reply_class: str) -> None:
    patient, phone, refs, reminders = _setup(sa_install, appointments=2)
    result = sa.inbound(sa_install, sa.new_id(), phone, reply_class)
    assert (result["decision"], result["triage_kind"], result["patient_id"],
            result["appointment_ref"], result["reminder_id"]) == (
        "item", "unclear_reply", patient, None, None)
    assert [sa.window(sa_install, r)["state"] for r in reminders] == ["awaiting", "awaiting"]
    assert sorted(_stored(sa_install, result["inbound_id"])["matched_reminders"]) \
        == sorted(reminders)


def test_sa_i07b_no_current_window(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    patient, phone, _ = sa.fresh_patient(sa_install, appointments=0)
    result = sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    assert (result["decision"], result["triage_kind"], result["patient_id"],
            result["appointment_ref"]) == ("item", "unclear_reply", patient, None)
    # Window exists but its appointment was cancelled since dispatch: not current.
    p2, phone2, (ref2,), (reminder2,) = _setup(sa_install)
    sa.directory(sa_install, "appointment_put", appointment_ref=ref2, patient_id=p2,
                 start_at=sa.FUTURE.format(n=1), status="cancelled")
    result2 = sa.inbound(sa_install, sa.new_id(), phone2, "reschedule")
    assert (result2["triage_kind"], result2["reminder_id"]) == ("unclear_reply", None)
    assert sa.window(sa_install, reminder2)["state"] == "awaiting"


def test_sa_i08_identity_quarantine(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    events = _counts(sa_install)
    unknown = sa.inbound(sa_install, sa.new_id(), sa.PHONE_UNKNOWN, "reschedule")
    assert (unknown["decision"], unknown["triage_kind"], unknown["patient_id"],
            unknown["event_id"]) == ("quarantined", None, None, None)
    # Shared phone: P2 has a window, P3 has none -> still two patients -> quarantined.
    sa.directory(sa_install, "contact_put", phone=sa.PHONE_SHARED, patient_id=sa.P2)
    sa.directory(sa_install, "contact_put", phone=sa.PHONE_SHARED, patient_id=sa.P3)
    ref = sa.new_id()
    sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=sa.P2,
                 start_at=sa.FUTURE.format(n=1), status="booked")
    sa.dispatch(sa_install, sa.new_id(), ref)
    shared = sa.inbound(sa_install, sa.new_id(), sa.PHONE_SHARED, "cancel")
    assert shared["decision"] == "quarantined"
    assert _counts(sa_install) == events
    kept = sa.admin(sa_install, sa.q(sa_install, "SELECT count(*) FROM {s}.m05_inbound_replies"
                                                 " WHERE decision = 'quarantined'"))
    assert kept[0][0] >= 2


def test_sa_i08b_shared_phone_window_untouched(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    phone = sa.fresh_phone()
    owner, other = sa.new_id(), sa.new_id()
    sa.directory(sa_install, "contact_put", phone=phone, patient_id=owner)
    ref = sa.new_id()
    sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=owner,
                 start_at=sa.FUTURE.format(n=1), status="booked")
    reminder = sa.new_id()
    sa.dispatch(sa_install, reminder, ref)
    sa.directory(sa_install, "contact_put", phone=phone, patient_id=other)  # no window
    result = sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    assert result["decision"] == "quarantined"
    assert sa.window(sa_install, reminder)["state"] == "awaiting"


def test_sa_i09_late_reply_before_timer(sa_install: sa.SAInstall) -> None:
    _, phone, (ref,), (reminder,) = _setup(sa_install)
    sa.shift_window(sa_install, reminder, -25)
    result = sa.inbound(sa_install, sa.new_id(), phone, "reschedule")
    assert (result["decision"], result["triage_kind"], result["reminder_id"]) == (
        "item", "reschedule_request", reminder)
    assert sa.window(sa_install, reminder)["state"] == "awaiting"


def test_sa_i10_reply_after_no_reply(sa_install: sa.SAInstall) -> None:
    _, phone, (ref,), (reminder,) = _setup(sa_install)
    sa.shift_window(sa_install, reminder, -25)
    assert sa.close(sa_install, reminder)["decision"] == "no_reply"
    result = sa.inbound(sa_install, sa.new_id(), phone, "cancel")
    assert (result["decision"], result["triage_kind"], result["reminder_id"]) == (
        "item", "cancel_request", reminder)
    assert sa.window(sa_install, reminder)["state"] == "no_reply"


MALFORMED: dict[str, dict[str, Any]] = {
    "channel-voice": {"channel": "voice"},
    "class-unknown": {"reply_class": "yes"},
    "phone-bad": {"phone": "15550100001"},
    "message-long": {"provider_message_id": "x" * 129},
    "extra-received-at": {"received_at": "2026-01-01T00:00:00Z"},
    "extra-patient": {"patient_id": sa.P1},
    "extra-appointment": {"appointment_ref": sa.P1},
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_sa_i11_malformed(sa_install: sa.SAInstall, case: str) -> None:
    sa_install.require()
    command = {**sa.inbound_cmd(sa.new_id(), sa.PHONE_1, "confirm"), **MALFORMED[case]}
    sa.refused(sa_install, INBOUND, command, "COMMAND_MALFORMED")


def test_sa_i11_workload_and_tenant(sa_install: sa.SAInstall,
                                    sa_other_install: sa.SAInstall) -> None:
    sa_install.require()
    sa_other_install.require()
    for principal in (sa.W_DISPATCH, sa.W_TIMER, sa.W_DIRECTORY, sa.W_OTHER):
        sa.refused(sa_install, INBOUND,
                   sa.inbound_cmd(sa.new_id(), sa.PHONE_1, "confirm", principal=principal),
                   "WORKLOAD_NOT_AUTHORIZED")
    actor = sa.inbound_cmd(sa.new_id(), sa.PHONE_1, "confirm")
    actor["attribution"]["principal_kind"] = "actor"
    sa.refused(sa_install, INBOUND, actor, "WORKLOAD_NOT_AUTHORIZED")
    _, foreign_phone, _ = sa.fresh_patient(sa_other_install)
    result = sa.inbound(sa_install, sa.new_id(), foreign_phone, "reschedule")
    assert result["decision"] == "quarantined"


def test_sa_i12_sql_writes_no_event_or_item(sa_install: sa.SAInstall) -> None:
    _, phone, _, _ = _setup(sa_install)
    before = _counts(sa_install)
    sa.inbound(sa_install, sa.new_id(), phone, "reschedule")
    assert _counts(sa_install) == before


def test_sa_i13_reassigned_appointment_is_stale(sa_install: sa.SAInstall) -> None:
    patient_a, phone_a, (ref,), (reminder,) = _setup(sa_install)
    patient_b, _, _ = sa.fresh_patient(sa_install, appointments=0)
    sa.directory(sa_install, "appointment_put", appointment_ref=ref, patient_id=patient_b,
                 start_at=sa.FUTURE.format(n=1), status="booked")
    stored = sa.window(sa_install, reminder)
    result = sa.inbound(sa_install, sa.new_id(), phone_a, "reschedule")
    assert (result["decision"], result["triage_kind"], result["patient_id"],
            result["appointment_ref"], result["reminder_id"]) == (
        "item", "unclear_reply", patient_a, None, None)
    assert sa.window(sa_install, reminder) == stored
    assert _stored(sa_install, result["inbound_id"])["matched_reminders"] == []
