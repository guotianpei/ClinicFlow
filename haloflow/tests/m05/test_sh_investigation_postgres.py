"""S-H investigation — reproductions of Codex's S-H v1r1 findings 1-4 on the EXISTING SQL.

INVESTIGATION ONLY (branch investigation/sh-recovery-matching, draft PR, do not merge).
Governing: Codex review `codex_cp1-met-sh-v1r1-investigation.md` (2026-11-08). No source
change. Each row asserts the CURRENT behaviour of t038 (directory), t039 (dispatch) and
t040 (inbound) at main `caf926d`, so a PASS confirms the finding. Synthetic data only.

- SHI-1   t039 registers a window whose 24 h deadline falls after the visit: it only
          requires start_at > now (finding 1: a ">= 24 h at send" floor cannot guarantee
          pre-visit follow-up once registration is delayed).
- SHI-2a  recovery after a directory phone change registers the NEW phone: t039 takes
          patient and phone from the directory at registration (finding 2).
- SHI-2b  ... and a reply from that new phone is matched to the window (a number that
          never received the text).
- SHI-2c  an appointment re-assigned to another patient registers under the new
          patient.
- SHI-3a  early reply, zero current windows for the patient: unclear_reply, no window.
- SHI-3b  early reply while ANOTHER current appointment's window exists: attributed to
          that other appointment (finding 3).
- SHI-3c  early reply with two current windows: unclear_reply (never a guess).
- SHI-4   two reminder ids for the SAME appointment_ref both register; a later reply is
          ambiguous -> unclear_reply (finding 4: one-per-version would create this).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sa_support as sa

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _appointment(install: sa.SAInstall, ref: str, patient: str, start: str,
                 status: str = "booked") -> None:
    sa.directory(install, "appointment_put", appointment_ref=ref, patient_id=patient,
                 start_at=start, status=status)


def _start_of(install: sa.SAInstall, ref: str) -> datetime:
    rows = sa.admin(install, sa.q(install, "SELECT start_at FROM {s}.m05_appointments"
                                           " WHERE appointment_ref = %s"), (ref,))
    value: datetime = rows[0][0]
    return value


def _deadline(install: sa.SAInstall, reminder: str) -> datetime:
    rows = sa.admin(install, sa.q(install, "SELECT deadline FROM {s}.m05_reminder_windows"
                                           " WHERE reminder_id = %s"), (reminder,))
    value: datetime = rows[0][0]
    return value


# --- finding 1 ------------------------------------------------------------------------------


@pytest.mark.parametrize("hours_ahead", (1, 23))
def test_shi1_deadline_after_visit_is_accepted(sa_install: sa.SAInstall,
                                               hours_ahead: int) -> None:
    sa_install.require()
    patient, _phone, _refs = sa.fresh_patient(sa_install, appointments=0)
    ref = sa.new_id()
    _appointment(sa_install, ref, patient,
                 _iso(datetime.now(UTC) + timedelta(hours=hours_ahead)))
    reminder = sa.new_id()
    result = sa.dispatch(sa_install, reminder, ref)
    assert result["outcome"] == "registered"
    assert _deadline(sa_install, reminder) > _start_of(sa_install, ref)


# --- finding 2 ------------------------------------------------------------------------------


def test_shi2a_registration_uses_current_phone(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    patient, phone_a, (ref,) = sa.fresh_patient(sa_install)
    # The text was handed to phone A (outside the database). Before registration the
    # directory moves the patient to phone B.
    phone_b = sa.fresh_phone()
    sa.directory(sa_install, "contact_remove", phone=phone_a, patient_id=patient)
    sa.directory(sa_install, "contact_put", phone=phone_b, patient_id=patient)
    reminder = sa.new_id()
    assert sa.dispatch(sa_install, reminder, ref)["outcome"] == "registered"
    assert sa.window(sa_install, reminder)["phone"] == phone_b


def test_shi2b_reply_from_new_phone_matches(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    patient, phone_a, (ref,) = sa.fresh_patient(sa_install)
    phone_b = sa.fresh_phone()
    sa.directory(sa_install, "contact_remove", phone=phone_a, patient_id=patient)
    sa.directory(sa_install, "contact_put", phone=phone_b, patient_id=patient)
    reminder = sa.new_id()
    sa.dispatch(sa_install, reminder, ref)
    reply = sa.inbound(sa_install, sa.new_id(), phone_b, "reschedule")
    assert (reply["decision"], reply["reminder_id"], reply["triage_kind"]) == (
        "item", reminder, "reschedule_request")
    assert sa.window(sa_install, reminder)["state"] == "replied"


def test_shi2c_reassigned_appointment_registers_new_patient(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    patient_1, _phone_1, (ref,) = sa.fresh_patient(sa_install)
    patient_2, phone_2, _ = sa.fresh_patient(sa_install, appointments=0)
    start = _iso(_start_of(sa_install, ref))
    _appointment(sa_install, ref, patient_2, start)  # same ref, now patient 2
    reminder = sa.new_id()
    result = sa.dispatch(sa_install, reminder, ref)
    assert result["outcome"] == "registered"
    row = sa.window(sa_install, reminder)
    assert (row["patient_id"], row["phone"]) == (patient_2, phone_2)
    assert patient_1 != patient_2


# --- finding 3 ------------------------------------------------------------------------------


def test_shi3a_early_reply_no_window(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    _patient, phone, (_ref,) = sa.fresh_patient(sa_install)
    reply = sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    assert (reply["decision"], reply["triage_kind"], reply["reminder_id"]) == (
        "item", "unclear_reply", None)


def test_shi3b_early_reply_attributed_to_other_appointment(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    _patient, phone, (ref_old, ref_new) = sa.fresh_patient(sa_install, appointments=2)
    older = sa.new_id()
    sa.dispatch(sa_install, older, ref_old)  # an earlier reminder, still current
    # New reminder for ref_new handed off but NOT yet registered; the patient replies.
    reply = sa.inbound(sa_install, sa.new_id(), phone, "cancel")
    assert (reply["decision"], reply["reminder_id"], reply["appointment_ref"],
            reply["triage_kind"]) == ("item", older, ref_old, "cancel_request")
    newer = sa.new_id()
    assert sa.dispatch(sa_install, newer, ref_new)["outcome"] == "registered"
    assert sa.window(sa_install, newer)["state"] == "awaiting"


def test_shi3c_early_reply_two_windows_is_unclear(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    _patient, phone, (ref_1, ref_2) = sa.fresh_patient(sa_install, appointments=2)
    w1, w2 = sa.new_id(), sa.new_id()
    sa.dispatch(sa_install, w1, ref_1)
    sa.dispatch(sa_install, w2, ref_2)
    reply = sa.inbound(sa_install, sa.new_id(), phone, "reschedule")
    assert (reply["decision"], reply["triage_kind"], reply["reminder_id"]) == (
        "item", "unclear_reply", None)
    assert {sa.window(sa_install, w)["state"] for w in (w1, w2)} == {"awaiting"}


# --- finding 4 ------------------------------------------------------------------------------


def test_shi4_two_windows_same_ref_make_replies_ambiguous(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    _patient, phone, (ref,) = sa.fresh_patient(sa_install)
    first, second = sa.new_id(), sa.new_id()
    assert sa.dispatch(sa_install, first, ref)["outcome"] == "registered"
    assert sa.dispatch(sa_install, second, ref)["outcome"] == "registered"
    reply = sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    assert (reply["decision"], reply["triage_kind"], reply["reminder_id"]) == (
        "item", "unclear_reply", None)
