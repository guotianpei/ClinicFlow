"""L-1S package S-H, checkpoint H1 — worker unit rows (no database; RED first).

Governing: see `sh_support`. The worker under test is
`haloflow.m05.outbound.OutboundWorker(service, sender, render)`; the service here is an
in-memory double with the OutboundService method names (claim, read, record_outcome,
register), so these rows pin the worker's ordering, never the SQL.

- H1-U01  payload: the sender receives exactly the frozen snapshot's phone and the text
          rendered from it; its hash equals the prepared text_sha256.
- H1-U01b render mismatch (hash differs) -> no send; recorded refused RENDER_CHANGED.
- H1-U02  claim lost or read refused (stale/expired) -> zero sender calls.
- H1-U03  sender raises / times out -> recorded uncertain; run_one again -> zero
          further sends.
- H1-U03b sender cancelled (CancelledError) -> uncertain recorded, cancellation re-raised.
- H1-U03c malformed or oversized ACCEPTED evidence -> uncertain ACCEPTED_EVIDENCE_INVALID.
- H1-U04  outcome commit-unknown -> the identical outcome is retried once; one send.
- H1-U05  register commit-unknown -> register retried; one send.
- H1-U05b recover(accepted op) registers only: no claim, no read, no send.
- H1-U06  privacy, adversarial: the fake raises a chained exception whose text contains
          the phone, name and message text; formatted logs (with exception chains), any
          escaped traceback and all reprs are scanned and must be clean.
- H1-U06b positive control: the same scanner finds all three in a careless
          logger.exception of that error.
- H1-U07  limited static guard: no provider/network import in haloflow.m05.outbound.
- H1-U07b runtime tripwire: no socket connection attempt during run_one (fake only).
"""

from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sh_support as sh

PHONE = "+15550001234"
START = datetime(2099, 1, 1, 14, 0, tzinfo=UTC)


class Svc:
    """In-memory double of OutboundService (method names and result shapes only)."""

    def __init__(self, *, claim_ok: bool = True, read_ok: bool = True,
                 digest: str | None = None) -> None:
        self.calls: list[str] = []
        self.claim_ok, self.read_ok = claim_ok, read_ok
        self.digest = digest or sh.text_hash(sh.render(sh.FIRST_NAME, START))
        self.attempt = str(uuid.uuid4())
        self.outcomes: list[tuple[str, Any]] = []
        self.fail_outcome_once = False
        self.fail_register_once = False
        self.state = "prepared"

    async def claim(self, context: Any, op_id: str, fence: int) -> dict[str, Any]:
        self.calls.append("claim")
        if not self.claim_ok:
            from haloflow.m05.outbound import OutboundRefused

            raise OutboundRefused("CLAIM_STALE")
        self.state = "attempting"
        return {"outcome": "claimed", "attempt_id": self.attempt, "fence": fence + 1}

    async def read(self, context: Any, op_id: str, attempt: str) -> dict[str, Any]:
        self.calls.append("read")
        if not self.read_ok:
            from haloflow.m05.outbound import OutboundRefused

            raise OutboundRefused("CLAIM_STALE")
        return {"op_id": op_id, "attempt_id": attempt, "phone": PHONE,
                "patient_id": str(uuid.uuid4()), "start_at": START.isoformat(),
                "text_sha256": self.digest}

    async def record_outcome(self, context: Any, op_id: str, attempt: str, outcome: str,
                             **evidence: Any) -> dict[str, Any]:
        self.calls.append("outcome:" + outcome)
        if self.fail_outcome_once:
            from haloflow.m05.outbound import OutboundCommitUnknown

            self.fail_outcome_once = False
            self.outcomes.append((outcome, evidence))
            raise OutboundCommitUnknown()
        self.outcomes.append((outcome, evidence))
        self.state = outcome
        return {"outcome": "recorded", "state": outcome}

    async def register(self, context: Any, op_id: str) -> dict[str, Any]:
        self.calls.append("register")
        if self.fail_register_once:
            from haloflow.m05.outbound import OutboundCommitUnknown

            self.fail_register_once = False
            raise OutboundCommitUnknown()
        self.state = "registered"
        return {"outcome": "registered", "state": "registered"}


def _render(snapshot: dict[str, Any]) -> str:
    """H1 fixed template: the first name is the fixture constant (no directory field until
    H2); every other input comes from the frozen snapshot."""

    return sh.render(sh.FIRST_NAME, datetime.fromisoformat(snapshot["start_at"]))


def _worker(svc: Svc, sender: Any) -> Any:
    from haloflow.m05.outbound import OutboundWorker

    return OutboundWorker(svc, sender, _render)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_h1_u01_payload_bound_to_snapshot() -> None:
    from haloflow.m05.outbound import FakeSender

    sender, svc = FakeSender(), Svc()
    _run(_worker(svc, sender).run_one(None, "op-1", 0))
    ((op_id, phone, text),) = sender.calls
    assert (op_id, phone) == ("op-1", PHONE)
    assert sh.text_hash(text) == svc.digest
    assert svc.calls == ["claim", "read", "outcome:accepted", "register"]


def test_h1_u01b_render_mismatch_never_sends() -> None:
    from haloflow.m05.outbound import FakeSender

    sender, svc = FakeSender(), Svc(digest="00" * 32)
    _run(_worker(svc, sender).run_one(None, "op-1", 0))
    assert sender.calls == []
    assert svc.outcomes == [("refused", {"code": "RENDER_CHANGED"})]


@pytest.mark.parametrize("where", ("claim", "read"))
def test_h1_u02_lost_claim_zero_sends(where: str) -> None:
    from haloflow.m05.outbound import FakeSender

    sender = FakeSender()
    svc = Svc(claim_ok=where != "claim", read_ok=where != "read")
    _run(_worker(svc, sender).run_one(None, "op-1", 0))
    assert sender.calls == []
    assert not any(c.startswith("outcome") for c in svc.calls)


@pytest.mark.parametrize("failure", ("error", "timeout"))
def test_h1_u03_sender_failure_uncertain_no_resend(failure: str) -> None:
    from haloflow.m05.outbound import FakeSender

    sender = FakeSender(fail=failure)
    svc = Svc()
    worker = _worker(svc, sender)
    _run(worker.run_one(None, "op-1", 0))
    assert [o for o, _ in svc.outcomes] == ["uncertain"]
    svc.claim_ok = False  # the op is no longer prepared
    _run(worker.run_one(None, "op-1", 1))
    assert len(sender.calls) == 1


def test_h1_u03b_cancellation_records_uncertain_and_reraises() -> None:
    from haloflow.m05.outbound import FakeSender

    sender, svc = FakeSender(fail="cancel"), Svc()
    with pytest.raises(asyncio.CancelledError):
        _run(_worker(svc, sender).run_one(None, "op-1", 0))
    assert [o for o, _ in svc.outcomes] == ["uncertain"]


@pytest.mark.parametrize("provider_ref", ("", "x" * 129, None, 7))
def test_h1_u03c_bad_accepted_evidence_is_uncertain(provider_ref: Any) -> None:
    from haloflow.m05.outbound import FakeSender

    sender, svc = FakeSender(accepted_ref=provider_ref), Svc()
    _run(_worker(svc, sender).run_one(None, "op-1", 0))
    assert svc.outcomes == [("uncertain", {"code": "ACCEPTED_EVIDENCE_INVALID"})]
    assert "register" not in svc.calls


def test_h1_u04_outcome_commit_unknown_retried_once() -> None:
    from haloflow.m05.outbound import FakeSender

    sender, svc = FakeSender(), Svc()
    svc.fail_outcome_once = True
    _run(_worker(svc, sender).run_one(None, "op-1", 0))
    assert [o for o, _ in svc.outcomes] == ["accepted", "accepted"]
    assert svc.outcomes[0] == svc.outcomes[1]
    assert len(sender.calls) == 1


def test_h1_u05_register_commit_unknown_retried() -> None:
    from haloflow.m05.outbound import FakeSender

    sender, svc = FakeSender(), Svc()
    svc.fail_register_once = True
    _run(_worker(svc, sender).run_one(None, "op-1", 0))
    assert svc.calls.count("register") == 2
    assert len(sender.calls) == 1


def test_h1_u05b_recover_registers_only() -> None:
    from haloflow.m05.outbound import FakeSender

    sender, svc = FakeSender(), Svc()
    svc.state = "accepted"
    _run(_worker(svc, sender).recover(None, "op-1"))
    assert svc.calls == ["register"]
    assert sender.calls == []


SECRETS = (PHONE, sh.FIRST_NAME, "appointment on")


def _scan(text: str) -> list[str]:
    """One scanner for both the oracle and its positive control."""

    return [secret for secret in SECRETS if secret in text]


def _formatted(records: list[logging.LogRecord]) -> str:
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(fmt.format(r) for r in records)  # includes exc_info chains


def test_h1_u06_privacy_adversarial_sender_error(caplog: pytest.LogCaptureFixture) -> None:
    """The fake raises an exception (with a chained cause) whose text CONTAINS the phone,
    name and message text; nothing outward may carry them."""

    from haloflow.m05.outbound import FakeSender

    caplog.set_level(logging.DEBUG)
    sender, svc = FakeSender(fail="leaky"), Svc()
    worker = _worker(svc, sender)
    escaped = ""
    try:
        _run(worker.run_one(None, "op-1", 0))
    except BaseException as error:  # noqa: BLE001 - any escape is scanned too
        escaped = "".join(traceback.format_exception(error))
    assert [o for o, _ in svc.outcomes] == ["uncertain"]
    outward = (_formatted(caplog.records) + escaped + repr(worker) + repr(sender)
               + repr(svc.outcomes))
    assert _scan(outward) == [], "phone, name or text leaked"


def test_h1_u06b_scanner_positive_control(caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.m05.outbound import FakeSender

    caplog.set_level(logging.DEBUG)
    leak_log = logging.getLogger("haloflow.m05.outbound")
    try:
        _run(FakeSender(fail="leaky").send("op-1", PHONE, sh.render(sh.FIRST_NAME, START)))
    except Exception:  # noqa: BLE001
        leak_log.exception("deliberate leak")  # the exact path a careless worker would take
    assert set(_scan(_formatted(caplog.records))) == set(SECRETS)


def test_h1_u07_no_provider_or_network_import() -> None:
    """Limited guard only: a static scan of the module's own imports."""

    import haloflow.m05.outbound as outbound

    banned = ("httpx", "requests", "haloflow.integrations", "haloflow.modules", "notifyre")
    source = open(outbound.__file__, encoding="utf-8").read()  # noqa: SIM115
    assert not any(f"import {b}" in source or f"from {b}" in source for b in banned)


def test_h1_u07b_runtime_network_tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake-only execution: any socket connection attempt during run_one is recorded."""

    import socket

    from haloflow.m05.outbound import FakeSender

    attempts: list[Any] = []

    def trip(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args[:2])
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket.socket, "connect", trip)
    monkeypatch.setattr(socket, "create_connection", trip)
    sender, svc = FakeSender(), Svc()
    _run(_worker(svc, sender).run_one(None, "op-1", 0))
    assert attempts == []
    assert len(sender.calls) == 1
