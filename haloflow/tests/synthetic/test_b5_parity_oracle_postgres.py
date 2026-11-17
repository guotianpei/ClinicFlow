"""Option B B5: the typed parity oracle on real installs (Codex 0052e54 R3 focused
investigation). Class C: runs on `main` b358637 with NO B5 code.

The compared action is a TEST-HARNESS action, labelled as such: the accepted B3b JSON route
POST /synthetic/simulator/patient-reply on two independent fresh installs (two clusters),
each seeded with one sent reminder for the same canonical phone. It exercises the oracle's
capture/lifecycle mechanics; it is NOT B5 runtime evidence.

Every database read happens while its cluster is alive; the comparison runs on immutable
captures afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import ob5_support as b5
import pytest

pytestmark = pytest.mark.postgres

PHONE = bb.syn_phone(70)
U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())
TS1, TS2 = "2026-11-19T23:00:00Z", "2026-11-20T23:00:00Z"


def _forge(inst: b3.Install, q: str, params: tuple[Any, ...]) -> None:
    """Admin write, triggers off for this statement's session: an adversarial fixture."""
    with inst.c.connect(s.TARGET_DB) as a:
        a.execute("SET session_replication_role = replica")
        assert a.execute(q, params).rowcount >= 1, q


class Pair:
    """Two installs with the same seed; captures and tampering while both are alive."""

    def __init__(self, a: b3.Install, b: b3.Install) -> None:
        self.inst = {"A": a, "B": b}
        self.flow: dict[str, dict[str, str]] = {}
        self.before: dict[str, b5.Capture] = {}
        self.sends: dict[str, tuple[tuple[str, str, str], ...]] = {}

    def seed_and_act(self, text: str) -> None:
        for k, inst in self.inst.items():
            self.flow[k] = bb.sent_only(inst, PHONE)

            async def act(inst: b3.Install = inst, k: str = k) -> None:
                async with bb.running(inst) as (lc, app):
                    token = await bb.session_token(app)
                    self.before[k] = b5.capture(inst, b5.db_now(inst))
                    sent0 = len(lc.sender.calls)
                    r = await bb.asgi(app, "POST", "/synthetic/simulator/patient-reply",
                                      headers=bb.post_headers(token),
                                      body=bb.jbody({"phone": PHONE, "text": text}))
                    assert r.status == 202, (r.status, r.raw[:200])
                    self.sends[k] = tuple(lc.sender.calls[sent0:])

            asyncio.run(act())

    def side(self, k: str) -> b5.Side:
        inst = self.inst[k]
        f = self.flow[k]
        return b5.Side(self.before[k], b5.capture(inst, self.before[k].anchor),
                       {f["patient"]: "seed.patient", f["ref"]: "seed.ref"},
                       str(inst.state["tenant_id"]), inst.schema, sends=self.sends[k])

    def item(self, k: str) -> dict[str, Any]:
        inst = self.inst[k]
        (row,) = inst.admin(f"SELECT to_jsonb(i) FROM {inst.schema}.m05_followup_items AS i"
                            " WHERE patient_id = %s::uuid", (self.flow[k]["patient"],))
        return dict(row[0])


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[Pair]:
    for d in ("a", "b", "ia", "ib"):
        (tmp_path / d).mkdir()
    with s.cluster(tmp_path / "a") as ca, s.cluster(tmp_path / "b") as cb:
        a, b = b3.install(ca, tmp_path / "ia"), b3.install(cb, tmp_path / "ib")
        bb.first_start(a)
        bb.first_start(b)
        yield Pair(a, b)
    s.reap()


def test_b5_po1_equivalent_installs_compare_equal_and_tampering_is_rejected(
        pair: Pair) -> None:
    """C (Codex 0052e54 R3 items 1-3). A reply "2" on each install creates the same business
    result with different generated ids, keyed fingerprints and incidental times: the typed
    oracle finds NO mismatch. Then, on B only, each tampering is REJECTED and reverting it is
    accepted again: the window deadline +1 day and +1 hour (with sent_at: a schema CHECK
    pins deadline = sent_at + 24 h); the item's
    source_event_id pointing at another event (broken link); the appointment start_at
    +1 hour. Text: the profile first name set to the same uuid-shaped, then the same
    timestamp-shaped value on both installs is accepted; a different uuid-shaped or
    timestamp-shaped value on B is rejected. An incidental created_at shift is accepted."""
    pair.seed_and_act("2")
    assert b5.parity_mismatches(pair.side("A"), pair.side("B")) == []
    b = pair.inst["B"]
    sch = b.schema
    ref, patient = pair.flow["B"]["ref"], pair.flow["B"]["patient"]
    item = pair.item("B")

    def rejected(name: str, q: str, params: tuple[Any, ...], undo: str,
                 undo_params: tuple[Any, ...]) -> None:
        _forge(b, q, params)
        got = b5.parity_mismatches(pair.side("A"), pair.side("B"))
        assert got != [], f"accepted tampering: {name}"
        _forge(b, undo, undo_params)
        assert b5.parity_mismatches(pair.side("A"), pair.side("B")) == [], f"undo {name}"

    w = f"{sch}.m05_reminder_windows"
    # The schema CHECK m05_reminder_windows_deadline pins deadline = sent_at + 24 h, so a
    # deadline can only move together with sent_at.
    rejected("deadline (and sent_at) +1 day",
             f"UPDATE {w} SET sent_at = sent_at + interval '1 day', deadline = deadline"
             " + interval '1 day' WHERE appointment_ref = %s", (ref,),
             f"UPDATE {w} SET sent_at = sent_at - interval '1 day', deadline = deadline"
             " - interval '1 day' WHERE appointment_ref = %s", (ref,))
    rejected("deadline (and sent_at) +1 hour",
             f"UPDATE {w} SET sent_at = sent_at + interval '1 hour', deadline = deadline"
             " + interval '1 hour' WHERE appointment_ref = %s", (ref,),
             f"UPDATE {w} SET sent_at = sent_at - interval '1 hour', deadline = deadline"
             " - interval '1 hour' WHERE appointment_ref = %s", (ref,))
    rejected("item source_event_id -> other event",
             f"UPDATE {sch}.m05_followup_items SET source_event_id = %s::uuid"
             " WHERE item_id = %s::uuid", (str(uuid.uuid4()), item["item_id"]),
             f"UPDATE {sch}.m05_followup_items SET source_event_id = %s::uuid"
             " WHERE item_id = %s::uuid", (item["source_event_id"], item["item_id"]))
    rejected("appointment start_at +1 hour",
             f"UPDATE {sch}.m05_appointments SET start_at = start_at + interval '1 hour'"
             " WHERE appointment_ref = %s", (ref,),
             f"UPDATE {sch}.m05_appointments SET start_at = start_at - interval '1 hour'"
             " WHERE appointment_ref = %s", (ref,))
    _forge(b, f"UPDATE {sch}.m05_followup_items SET created_at = created_at - interval"
              " '30 seconds' WHERE item_id = %s::uuid", (item["item_id"],))
    assert b5.parity_mismatches(pair.side("A"), pair.side("B")) == [], "incidental time"
    prof = f"{sch}.m05_appointment_profile"
    q = f"UPDATE {prof} SET first_name = %s WHERE appointment_ref = %s"
    for same, other in ((U1, U2), (TS1, TS2)):
        for k in ("A", "B"):
            _forge(pair.inst[k], q.replace(sch, pair.inst[k].schema),
                   (same, pair.flow[k]["ref"]))
        assert b5.parity_mismatches(pair.side("A"), pair.side("B")) == [], same
        _forge(b, q, (other, ref))
        assert b5.parity_mismatches(pair.side("A"), pair.side("B")) != [], other
        _forge(b, q, (same, ref))
    assert patient


def test_b5_po2_captures_need_a_live_cluster(tmp_path: Path) -> None:
    """C (Codex 0052e54 R3 lifecycle): a capture after the cluster context closed fails
    loudly (so P-PAR's ordering matters and is enforced), while captures taken inside the
    context remain comparable after it closes."""
    (tmp_path / "c").mkdir()
    (tmp_path / "i").mkdir()
    with s.cluster(tmp_path / "c") as c:
        inst = b3.install(c, tmp_path / "i")
        cap = b5.capture(inst, b5.db_now(inst))
    s.reap()
    side = b5.Side(cap, cap, {}, str(inst.state["tenant_id"]), inst.schema)
    assert b5.parity_mismatches(side, side) == []
    import psycopg

    with pytest.raises(psycopg.OperationalError):
        b5.capture(inst, cap.anchor)
