"""L-1S package S-H, checkpoint H2 — text, name and worker unit rows (no database; RED first).

Governing: see `sh2_support`. The worker under test is
`haloflow.m05.outbound.OutboundWorker(service, sender, render).run_claimed(context, op_id,
attempt_id)`; the service here is an in-memory double with the method names consume,
record_outcome and register, so these rows pin the worker's ordering, never the SQL.

- H2-U01  Q-B4 golden strings (independent literals): single-digit hour, midnight, noon,
          DST day, clinic-name variants, a fixed-offset Etc zone; the zero-padded day
          ("Nov 02") is the demonstrator's established format and is kept.
- H2-U01b the H1 fixed test template (sh_support.render) and reminder_text agree when the
          clinic name is the H1 one (the shared case).
- H2-U02  first_name_valid: every NAMES_OK accepted, every NAMES_BAD refused (emoji,
          format, control, bidi, BOM, private use, non-NFC, 41 characters, empty,
          surrounding whitespace); refused values never raise.
- H2-U03  run_claimed: consume -> render(inputs) -> hash check -> exactly one send ->
          outcome accepted -> register. No claim and no H1 read are called.
- H2-U03b consume refused (TICKET_SPENT / CLAIM_STALE / PRECALL_WINDOW_CLOSED) -> zero
          sends, no outcome recorded by the worker (the gate refusal is recorded by consume
          itself), result "not_sent".
- H2-U03c consume commit-unknown -> zero sends, consume NOT retried, result
          "consume_unknown" (Codex pin 4).
- H2-U03d render mismatch -> zero sends, recorded refused RENDER_CHANGED.
- H2-U03e sender timeout after a committed send authorisation -> uncertain; a second
          run_claimed for the same attempt -> consume TICKET_SPENT -> zero further sends.
- H2-U03f run_one (H1) is unchanged: still claim -> read, never consume.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sh2_support as sh2
import sh_support as sh

PHONE = "+15550004321"
START = datetime(2099, 1, 5, 15, 0, tzinfo=UTC)
TAIL = " Reply 1 to confirm, 2 to reschedule, or 3 to cancel. Reply STOP to opt out."

GOLDEN = (
    ("Synth", datetime(2026, 11, 2, 14, 0, tzinfo=UTC), sh2.NY, "HaloVox Test Clinic",
     "Hi Synth, this is HaloVox Test Clinic. You have an appointment on Monday, Nov 02 at"
     " 9:00 AM." + TAIL),
    ("Synth", datetime(2026, 11, 10, 5, 0, tzinfo=UTC), sh2.NY, "HaloVox Test Clinic",
     "Hi Synth, this is HaloVox Test Clinic. You have an appointment on Tuesday, Nov 10 at"
     " 12:00 AM." + TAIL),
    ("Synth", datetime(2026, 11, 10, 17, 0, tzinfo=UTC), sh2.NY, "HaloVox Test Clinic",
     "Hi Synth, this is HaloVox Test Clinic. You have an appointment on Tuesday, Nov 10 at"
     " 12:00 PM." + TAIL),
    ("Zoë", datetime(2027, 3, 14, 7, 30, tzinfo=UTC), sh2.NY, "Dr. Ana Núñez Family Practice",
     "Hi Zoë, this is Dr. Ana Núñez Family Practice. You have an appointment on Sunday,"
     " Mar 14 at 3:30 AM." + TAIL),
    ("Synth", datetime(2026, 10, 28, 13, 5, tzinfo=UTC), "America/Chicago",
     "HaloVox Demo Clinic",
     "Hi Synth, this is HaloVox Demo Clinic. You have an appointment on Wednesday, Oct 28 at"
     " 8:05 AM." + TAIL),
    ("Synth", datetime(2026, 11, 8, 3, 45, tzinfo=UTC), "Etc/GMT-3", "HaloVox Test Clinic",
     "Hi Synth, this is HaloVox Test Clinic. You have an appointment on Sunday, Nov 08 at"
     " 6:45 AM." + TAIL),
)


@pytest.mark.parametrize(("first", "start", "zone", "clinic", "expected"), GOLDEN,
                         ids=[f"g{i}" for i in range(len(GOLDEN))])
def test_h2_u01_golden_text(first: str, start: datetime, zone: str, clinic: str,
                            expected: str) -> None:
    from haloflow.m05.reminder_text import reminder_text

    assert reminder_text(first, start, zone, clinic) == expected


def test_h2_u01b_matches_h1_template_for_shared_case() -> None:
    from haloflow.m05.reminder_text import reminder_text

    for start in (START, datetime(2099, 1, 1, 9, 0, tzinfo=UTC)):
        assert reminder_text(sh.FIRST_NAME, start, sh.TZ, "HaloVox Test Clinic") == sh.render(
            sh.FIRST_NAME, start)


def test_h2_u02_first_name_rule() -> None:
    from haloflow.m05.reminder_text import first_name_valid

    for name in sh2.NAMES_OK:
        assert first_name_valid(name) is True, name
    for name in sh2.NAMES_BAD:
        assert first_name_valid(name) is False, repr(name)
    for lo, hi in sh2.FORBIDDEN_RANGES:  # every forbidden range, both ends
        for cp in {lo, hi}:
            assert first_name_valid(f"A{chr(cp)}B") is False, hex(cp)


# --- worker double ----------------------------------------------------------------------------


def _text(first: str = sh.FIRST_NAME) -> str:
    from haloflow.m05.reminder_text import reminder_text

    return reminder_text(first, START, sh2.NY, sh2.CLINIC)


def _render(inputs: Any) -> str:
    from haloflow.m05.reminder_text import reminder_text

    start = datetime.fromisoformat(str(inputs["start_at"]).replace("Z", "+00:00"))
    return reminder_text(inputs["first_name"], start, inputs["zone"],
                         inputs["clinic_display_name"])


class Svc:
    """In-memory double: consume / record_outcome / register (plus claim/read tripwires)."""

    def __init__(self, *, consume: str = "send", digest: str | None = None) -> None:
        self.calls: list[str] = []
        self.mode = consume
        self.spent = False
        self.digest = digest or hashlib.sha256(_text().encode("utf-8")).hexdigest()

    async def consume(self, context: Any, op_id: str, attempt: str) -> dict[str, Any]:
        self.calls.append("consume")
        if self.mode == "unknown":
            from haloflow.m05.outbound import OutboundCommitUnknown

            raise OutboundCommitUnknown()
        if self.mode != "send":
            return {"outcome": "refused", "code": self.mode}
        if self.spent:
            return {"outcome": "refused", "code": "TICKET_SPENT"}
        self.spent = True
        return {"outcome": "send", "inputs": {
            "phone": PHONE, "first_name": sh.FIRST_NAME, "clinic_display_name": sh2.CLINIC,
            "zone": sh2.NY, "zone_source": "department", "start_at": sh2.iso_z(START),
            "text_sha256": self.digest}}

    async def record_outcome(self, context: Any, op_id: str, attempt: str, outcome: str,
                             **evidence: Any) -> dict[str, Any]:
        self.calls.append(f"outcome:{outcome}:{evidence.get('code')}")
        return {"state": outcome}

    async def register(self, context: Any, op_id: str) -> dict[str, Any]:
        self.calls.append("register")
        return {"outcome": "registered"}

    async def claim(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.calls.append("claim")
        return {"outcome": "claimed", "attempt_id": str(uuid.uuid4()), "fence": 1}

    async def read(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.calls.append("read")
        raise AssertionError("run_claimed must not call the H1 read")


def _worker(svc: Svc, sender: Any) -> Any:
    from haloflow.m05.outbound import OutboundWorker

    return OutboundWorker(svc, sender, _render)


def test_h2_u03_run_claimed_sends_once() -> None:
    from haloflow.m05.outbound import FakeSender

    svc, sender = Svc(), FakeSender()
    result = asyncio.run(_worker(svc, sender).run_claimed(object(), "op-1", "att-1"))
    assert result == "accepted"
    assert sender.calls == [("op-1", PHONE, _text())]
    assert svc.calls == ["consume", "outcome:accepted:None", "register"]


@pytest.mark.parametrize("code", ["TICKET_SPENT", "CLAIM_STALE", "PRECALL_WINDOW_CLOSED"])
def test_h2_u03b_consume_refused_no_send(code: str) -> None:
    from haloflow.m05.outbound import FakeSender

    svc, sender = Svc(consume=code), FakeSender()
    assert asyncio.run(_worker(svc, sender).run_claimed(object(), "op", "att")) == "not_sent"
    assert sender.calls == []
    assert svc.calls == ["consume"]


def test_h2_u03c_consume_commit_unknown_never_sends() -> None:
    from haloflow.m05.outbound import FakeSender

    svc, sender = Svc(consume="unknown"), FakeSender()
    result = asyncio.run(_worker(svc, sender).run_claimed(object(), "op", "att"))
    assert result == "consume_unknown"
    assert sender.calls == []
    assert svc.calls == ["consume"]  # never retried


def test_h2_u03d_render_mismatch_refused() -> None:
    from haloflow.m05.outbound import FakeSender

    svc, sender = Svc(digest="0" * 64), FakeSender()
    assert asyncio.run(_worker(svc, sender).run_claimed(object(), "op", "att")) == "refused"
    assert sender.calls == []
    assert svc.calls == ["consume", "outcome:refused:RENDER_CHANGED"]


def test_h2_u03e_timeout_then_ticket_spent() -> None:
    from haloflow.m05.outbound import FakeSender

    svc, sender = Svc(), FakeSender(fail="timeout")
    worker = _worker(svc, sender)
    assert asyncio.run(worker.run_claimed(object(), "op", "att")) == "uncertain"
    assert asyncio.run(worker.run_claimed(object(), "op", "att")) == "not_sent"
    assert len(sender.calls) == 1
    assert svc.calls == ["consume", "outcome:uncertain:SENDER_TIMEOUT", "consume"]


def test_h2_u03f_run_one_unchanged() -> None:
    from haloflow.m05.outbound import FakeSender, OutboundWorker

    class H1Svc(Svc):
        async def read(self, context: Any, op_id: str, attempt: str) -> dict[str, Any]:
            self.calls.append("read")
            return {"phone": PHONE, "start_at": START.isoformat(),
                    "text_sha256": hashlib.sha256(sh.render(sh.FIRST_NAME, START).encode(
                        "utf-8")).hexdigest()}

    svc, sender = H1Svc(digest="unused"), FakeSender()  # control: H1 code only, green now
    worker = OutboundWorker(svc, sender, lambda snap: sh.render(sh.FIRST_NAME, START))
    assert asyncio.run(worker.run_one(object(), "op", 0)) == "accepted"
    assert svc.calls[:2] == ["claim", "read"]
    assert "consume" not in svc.calls
