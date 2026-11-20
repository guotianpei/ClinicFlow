"""Eligibility E2 — unit rows, no database (RED BY DESIGN).

Every E2 import is deferred into the row body, so the intended red cause is
ModuleNotFoundError for the E2 modules (`haloflow.m05.eligibility_results`,
`haloflow.m05.simulated_clearinghouse`, `haloflow.m05.eligibility`,
`haloflow.m05.eligibility_scheduler`). Rows marked CONTROL use existing code and pass now.

PINNED PURE API (`haloflow.m05.eligibility_results`):
- Constants: CLASSES = ("active", "inactive", "inconclusive"); INCONCLUSIVE_REASONS (the six
  OD-EL8 inconclusive codes); TRANSIENT = ("timeout", "unavailable"); RETRY_DELAY_S = 120;
  LEASE_S = 120; TAKEOVER_MARGIN_S = 30; EXPIRY_DAYS = 30; MAX_ATTEMPTS = 2.
- `normalize(raw) -> dict`: the raw simulated response (below) becomes the closed normalized
  structure (EL-7): {class, reason, observed_at (ISO UTC text), source, payer_name, plan,
  copay, deductible_total, deductible_remaining, coinsurance, referral_required,
  auth_required, txn_ref}; absent or unknown fields stay ABSENT (never guessed, never None).
  `inactive` only from an explicit status "inactive" (positive statement, EL-8); every payer
  error maps to `inconclusive` with its reason; anything malformed (not a mapping, unknown
  status, unknown error, bad observed_at, wrong field types) is `inconclusive` /
  `response_unreadable` with no other field.
- `FreshKey(tenant, appointment_ref, patient_id, payer_id, member_digest, coverage_version,
  service_date)` (frozen) and `is_fresh(result_key, current_key, result_class, observed_at,
  window_start) -> bool`: True iff the keys are equal, the class is `active` and
  observed_at >= window_start (EL-5; M1 uses observed_at, never settled_at).
- `terminate(role, observed_at, window_start, returned) -> "satisfied" | "returned"` (M1):
  role "owner" -> satisfied always; "attached": no observation -> satisfied; observed >=
  window -> satisfied; observed < window and not yet returned -> returned; an attached
  trigger that was already returned -> ValueError (it owns its next verification).
- `retry_ready(failed_at, now)`, `takeover_ready(lease_until, now)`, `expired(service_date,
  today)` (the Python mirrors of the SQL seams).

PINNED PORT (`haloflow.m05.simulated_clearinghouse`): `SimulatedClearinghouse(*, clock)`;
`set_member(member_id, status, *, error=None, benefits=None)` with status in {"active",
"inactive", "error"}; `fail_next(n)` -> the next n submits raise `ClearinghouseUnavailable`;
`malformed_next(n)` -> the next n submits return a malformed body; `async submit(attempt_id,
request) -> dict` (raw: {"attempt_id", "echo": {the request's correlation}, "status",
"error"?, "observed_at": ISO text from the injected clock, "benefits"?, "txn_ref"}); an unknown
member answers status "error" / error "subscriber_not_found"; `calls` = attempt ids received.
No network or HTTP import.

PINNED WORKER (`haloflow.m05.eligibility`): `EligibilityService(gateway)` with async
`due(ctx, limit) -> list[str]`, `claim(ctx, vid)`, `dispatch(ctx, vid, epoch)`,
`record_answer(ctx, attempt_id, epoch, echo, result)`, `timeout(ctx, attempt_id, epoch)`,
`settle(ctx, vid, epoch)`, `expire(ctx, limit) -> int` (each returns the gateway's mapping).
`EligibilityWorker(service, port, *, call_timeout_s=10.0)`; `async run_one(ctx, vid) -> str`
in WORKER_OUTCOMES = ("settled", "retry_pending", "not_claimed", "fenced", "superseded",
"cancelled", "activation_off", "in_flight"). Protocol: claim -> dispatch -> ONE bounded port
call per dispatched attempt -> record_answer (normalized, echo from the response) | timeout
(on any port failure or timeout) -> dispatch again (attempt 2, or `retry_pending` when the
delay is not over) -> settle. An attempt id is never sent twice. Only an `Exception` from the
port (or the call timeout) is a port failure; any other BaseException (cancellation, a process
crash) propagates out of `run_one` with nothing recorded. Logs carry codes and class names only
(EL-14).

PINNED SCHEDULER (`haloflow.m05.eligibility_scheduler`), the S-J pattern:
`EligibilityScheduler(*, contexts, service, worker, tenants, interval_s=60, stop_timeout_s=10,
batch_limit=100, clock, sleep)`; `EligibilitySchedulerRefused(code)`;
`EligibilitySchedulerStopFailed` (code STOP_TIMEOUT); constants DEFAULT_INTERVAL_S=60,
INTERVAL_MIN_S=30, INTERVAL_MAX_S=300, DEFAULT_BATCH_LIMIT=100, BATCH_LIMIT_MAX=1000. Per tenant
per tick: one pass context -> `service.due(ctx, limit)` -> per id (at most limit) one fresh
context -> `worker.run_one(ctx_i, vid)` -> `service.expire(pass ctx, limit)`. One tenant never
stops another; cancellation is never caught; one summary line per tick on logger
`haloflow.m05.eligibility_scheduler` with codes and counts only.

Rows: E2-U01 normalization; E2-U02 inactive never from inconclusive; E2-U03 freshness key;
E2-U04 observed_at vs settled_at; E2-U05 trigger termination table; E2-U06 simulated port
(static + behaviour); E2-U07 seam mirrors at exact instants; E2-W01..W05 worker protocol;
E2-P15 scheduler lifecycle, order, bound, isolation, bounded stop, privacy (unit layer, S-J
pattern); E2-UC0 CONTROL.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import se_support as se

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"
PORT_FILE = SRC / "m05" / "simulated_clearinghouse.py"
WORKER_FILE = SRC / "m05" / "eligibility.py"
SCHED_FILE = SRC / "m05" / "eligibility_scheduler.py"
SCHED_LOGGER = "haloflow.m05.eligibility_scheduler"
WORKER_LOGGER = "haloflow.m05.eligibility"
T0 = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
BOUND_S = 5.0
NETWORK = ("socket", "http", "urllib", "httpx", "aiohttp", "requests", "ssl", "asyncio.streams")
SENTINELS = ("SENTINEL-MEMBER-8841", "Sentinel Patientname", "1922-02-22", "SENTINEL-TXN-77")
INCONCLUSIVE = ("subscriber_not_found", "name_dob_mismatch", "member_id_not_recognized",
                "service_type_unsupported", "payer_unavailable", "response_unreadable")
PAYER_ERRORS = INCONCLUSIVE[:4]
WORKER_OUTCOMES = ("settled", "retry_pending", "not_claimed", "fenced", "superseded",
                   "cancelled", "activation_off", "in_flight")
NORMALIZED_KEYS = {"class", "reason", "observed_at", "source", "payer_name", "plan", "copay",
                   "deductible_total", "deductible_remaining", "coinsurance",
                   "referral_required", "auth_required", "txn_ref"}


def _r() -> Any:
    from haloflow.m05 import eligibility_results

    return eligibility_results


def _port_mod() -> Any:
    from haloflow.m05 import simulated_clearinghouse

    return simulated_clearinghouse


def _raw(status: str = "active", **over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "attempt_id": "00000000-0000-4000-8000-0000000e2001",
        "echo": {"attempt_id": "00000000-0000-4000-8000-0000000e2001"},
        "status": status, "observed_at": T0.isoformat(), "txn_ref": "SYN-TXN-1",
        "benefits": {"payer_name": "Synthetic Payer", "plan": "SYN-PPO", "copay": "25.00",
                     "deductible_total": "1000.00", "deductible_remaining": "400.00",
                     "coinsurance": "20", "referral_required": False, "auth_required": True}}
    raw.update(over)
    return raw


# --- E2-UC0 CONTROL ----------------------------------------------------------------------

def test_el2_uc0_control_e1_rules_present() -> None:
    from haloflow.m05 import eligibility_rules as rules

    assert rules.previsit_due(T0, "America/New_York", 3) == T0 - timedelta(days=3)


# --- E2-U01 normalization ----------------------------------------------------------------

def test_el2_u01_active_is_normalized_with_benefit_fields() -> None:
    got = _r().normalize(_raw())
    assert set(got) <= NORMALIZED_KEYS
    assert got["class"] == "active" and got["reason"] == "coverage_active"
    assert datetime.fromisoformat(got["observed_at"]) == T0
    assert (got["plan"], got["copay"], got["deductible_remaining"], got["coinsurance"],
            got["referral_required"], got["auth_required"], got["txn_ref"]) == (
        "SYN-PPO", "25.00", "400.00", "20", False, True, "SYN-TXN-1")
    assert got["source"] == "simulated"


def test_el2_u01_inactive_requires_a_positive_statement() -> None:
    got = _r().normalize(_raw("inactive"))
    assert (got["class"], got["reason"]) == ("inactive", "coverage_inactive")


@pytest.mark.parametrize("error", PAYER_ERRORS)
def test_el2_u01_each_payer_error_is_inconclusive_with_its_reason(error: str) -> None:
    got = _r().normalize(_raw("error", error=error, benefits=None))
    assert (got["class"], got["reason"]) == ("inconclusive", error)
    assert not {"plan", "copay", "deductible_remaining", "coinsurance"} & set(got)


@pytest.mark.parametrize("raw", [
    None, [], "active", {}, {"status": "active"},                    # no observed_at
    {"status": "maybe", "observed_at": T0.isoformat()},
    {"status": "error", "error": "carrier_pigeon", "observed_at": T0.isoformat()},
    {"status": "active", "observed_at": "yesterday"},
    {"status": "active", "observed_at": "2026-11-20T15:00:00"},       # naive
    {"status": "active", "observed_at": T0.isoformat(), "benefits": "lots"},
    {"status": "active", "observed_at": T0.isoformat(), "benefits": {"copay": 25}},
], ids=["none", "list", "str", "empty", "no-time", "status", "error-code", "bad-time",
        "naive-time", "benefits-type", "field-type"])
def test_el2_u01_malformed_is_inconclusive_response_unreadable(raw: Any) -> None:
    got = _r().normalize(raw)
    assert got == {"class": "inconclusive", "reason": "response_unreadable",
                   "source": "simulated"}


def test_el2_u01_unknown_fields_stay_absent_never_none() -> None:
    raw = _raw(benefits={"plan": "SYN-HMO"}, txn_ref=None)
    raw["surprise"] = "x"
    got = _r().normalize(raw)
    assert got["plan"] == "SYN-HMO"
    assert "surprise" not in got and "copay" not in got and "txn_ref" not in got
    assert None not in got.values()


def test_el2_u01_constants() -> None:
    r = _r()
    assert r.CLASSES == ("active", "inactive", "inconclusive")
    assert r.INCONCLUSIVE_REASONS == INCONCLUSIVE
    assert (r.RETRY_DELAY_S, r.LEASE_S, r.TAKEOVER_MARGIN_S, r.EXPIRY_DAYS, r.MAX_ATTEMPTS) == (
        120, 120, 30, 30, 2)


# --- E2-U02 inactive never from inconclusive input ----------------------------------------

@pytest.mark.parametrize("raw", [
    _raw("error", error="subscriber_not_found"), _raw("error", error="name_dob_mismatch"),
    _raw("error", error="member_id_not_recognized"),
    _raw("error", error="service_type_unsupported"), {"status": "inactive"},
    {"status": "inactive", "observed_at": "bad"}, _raw("INACTIVE"), _raw("terminated")])
def test_el2_u02_inactive_never_produced_from_inconclusive(raw: Any) -> None:
    assert _r().normalize(raw)["class"] == "inconclusive"


# --- E2-U03 freshness key ------------------------------------------------------------------

def _key(**over: Any) -> Any:
    fields = {"tenant": "t-a", "appointment_ref": "a-1", "patient_id": "p-1",
              "payer_id": "SYN-ANTHEM", "member_digest": "d" * 64, "coverage_version": 1,
              "service_date": date(2026, 11, 23)}
    fields.update(over)
    return _r().FreshKey(**fields)


def test_el2_u03_equal_key_active_after_window_is_fresh() -> None:
    assert _r().is_fresh(_key(), _key(), "active", T0, T0) is True


@pytest.mark.parametrize(("field", "value"), [
    ("tenant", "t-b"), ("appointment_ref", "a-2"), ("patient_id", "p-2"),
    ("payer_id", "SYN-UHC"), ("member_digest", "e" * 64), ("coverage_version", 2),
    ("service_date", date(2026, 11, 24))])
def test_el2_u03_each_component_mismatched_alone_is_stale(field: str, value: Any) -> None:
    assert _r().is_fresh(_key(**{field: value}), _key(), "active", T0, T0) is False


@pytest.mark.parametrize("cls", ["inactive", "inconclusive", "manual_required"])
def test_el2_u03_only_active_is_ever_fresh(cls: str) -> None:
    assert _r().is_fresh(_key(), _key(), cls, T0, T0) is False


# --- E2-U04 observed_at, not settled_at -----------------------------------------------------

def test_el2_u04_observed_before_window_is_not_fresh_whenever_settled() -> None:
    window = datetime(2026, 11, 23, 5, 0, tzinfo=UTC)          # 00:00 EST
    observed = window - timedelta(seconds=30)                  # 23:59:30 the day before
    assert _r().is_fresh(_key(), _key(), "active", observed, window) is False
    assert _r().is_fresh(_key(), _key(), "active", window, window) is True
    import inspect

    assert "settled" not in " ".join(inspect.signature(_r().is_fresh).parameters)


# --- E2-U05 trigger termination (M1) --------------------------------------------------------

W = datetime(2026, 11, 23, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(("role", "observed", "returned", "expected"), [
    ("owner", None, False, "satisfied"),
    ("owner", W - timedelta(hours=1), False, "satisfied"),
    ("owner", W - timedelta(hours=1), True, "satisfied"),
    ("attached", W, False, "satisfied"),
    ("attached", W + timedelta(hours=1), False, "satisfied"),
    ("attached", W - timedelta(microseconds=1), False, "returned"),
    ("attached", None, False, "satisfied"),
])
def test_el2_u05_termination_table(role: str, observed: datetime | None, returned: bool,
                                   expected: str) -> None:
    assert _r().terminate(role, observed, W, returned) == expected


def test_el2_u05_attached_twice_returned_is_refused() -> None:
    with pytest.raises(ValueError):
        _r().terminate("attached", W - timedelta(seconds=1), W, True)


# --- E2-U07 seam mirrors at exact instants ---------------------------------------------------

@pytest.mark.parametrize(("delta", "expected"), [
    (timedelta(seconds=119, microseconds=999000), False), (timedelta(seconds=120), True),
    (timedelta(seconds=121), True)])
def test_el2_u07_retry_ready(delta: timedelta, expected: bool) -> None:
    assert _r().retry_ready(T0, T0 + delta) is expected


@pytest.mark.parametrize(("delta", "expected"), [
    (timedelta(seconds=29, microseconds=999999), False), (timedelta(seconds=30), True)])
def test_el2_u07_takeover_ready(delta: timedelta, expected: bool) -> None:
    assert _r().takeover_ready(T0, T0 + delta) is expected


@pytest.mark.parametrize(("days", "expected"), [(29, False), (30, False), (31, True)])
def test_el2_u07_expired_after_service_date_plus_30(days: int, expected: bool) -> None:
    service = date(2026, 11, 23)
    assert _r().expired(service, service + timedelta(days=days)) is expected


# --- E2-U06 simulated clearinghouse -----------------------------------------------------------

def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", [PORT_FILE, WORKER_FILE, SCHED_FILE],
                         ids=["port", "worker", "scheduler"])
def test_el2_u06_no_network_import(path: pathlib.Path) -> None:
    names = _imports(path)
    for bad in NETWORK:
        assert not any(n == bad or n.startswith(bad + ".") for n in names), (path.name, bad)


def _request(member: str = "M-1", attempt: str = "att-1") -> dict[str, Any]:
    return {"attempt_id": attempt, "tenant_id": "t-a", "appointment_ref": "a-1",
            "patient_id": "p-1", "coverage_version": 1, "payer_id": "SYN-ANTHEM",
            "member_id": member, "relationship": "self", "patient_name": "Synthetic Patient",
            "patient_dob": "1970-01-01", "subscriber_name": None, "subscriber_dob": None,
            "subscriber_member_id": None, "service_date": "2026-11-23"}


async def test_el2_u06_port_is_deterministic_and_echoes_the_correlation() -> None:
    m = _port_mod()
    port = m.SimulatedClearinghouse(clock=lambda: T0)
    port.set_member("M-1", "active", benefits={"plan": "SYN-PPO", "copay": "25.00"})
    port.set_member("M-2", "inactive")
    port.set_member("M-3", "error", error="name_dob_mismatch")
    a = await port.submit("att-1", _request("M-1", "att-1"))
    b = await port.submit("att-2", _request("M-2", "att-2"))
    c = await port.submit("att-3", _request("M-3", "att-3"))
    d = await port.submit("att-4", _request("M-404", "att-4"))
    assert (a["status"], b["status"], c["status"], c["error"]) == (
        "active", "inactive", "error", "name_dob_mismatch")
    assert (d["status"], d["error"]) == ("error", "subscriber_not_found")
    assert datetime.fromisoformat(a["observed_at"]) == T0
    assert a["echo"] == {"attempt_id": "att-1", "tenant_id": "t-a", "appointment_ref": "a-1",
                         "patient_id": "p-1", "member_id": "M-1", "coverage_version": 1}
    assert port.calls == ["att-1", "att-2", "att-3", "att-4"]
    again = await port.submit("att-5", _request("M-1", "att-5"))
    assert {k: v for k, v in again.items() if k not in ("attempt_id", "echo", "txn_ref")} == {
        k: v for k, v in a.items() if k not in ("attempt_id", "echo", "txn_ref")}


async def test_el2_u06_fault_injection_and_injected_clock() -> None:
    m = _port_mod()
    now = [T0]
    port = m.SimulatedClearinghouse(clock=lambda: now[0])
    port.set_member("M-1", "active")
    port.fail_next(2)
    for attempt in ("x1", "x2"):
        with pytest.raises(m.ClearinghouseUnavailable):
            await port.submit(attempt, _request("M-1", attempt))
    port.malformed_next(1)
    bad = await port.submit("x3", _request("M-1", "x3"))
    assert _r().normalize(bad)["reason"] == "response_unreadable"
    now[0] = T0 + timedelta(hours=9)
    good = await port.submit("x4", _request("M-1", "x4"))
    assert datetime.fromisoformat(good["observed_at"]) == T0 + timedelta(hours=9)
    assert port.calls == ["x1", "x2", "x3", "x4"]


def test_el2_u06_unavailable_carries_no_patient_data() -> None:
    m = _port_mod()
    assert str(m.ClearinghouseUnavailable("unavailable")) == "unavailable"
    assert "M-1" not in repr(m.SimulatedClearinghouse(clock=lambda: T0))


# --- E2-W01..W05 worker protocol (fake service, real simulated port) -----------------------

class FakeService:
    """Scripted gateway results; records every call (the real EligibilityService subclass)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.dispatches: list[dict[str, Any]] = []
        self.claim_result: dict[str, Any] = {"outcome": "claimed", "epoch": 7,
                                             "takeover": False}
        self.n = 0

    def make(self) -> Any:
        from haloflow.m05.eligibility import EligibilityService

        fake = self

        class Service(EligibilityService):
            def __init__(self) -> None:
                super().__init__(object())

            async def claim(self, ctx: Any, vid: Any) -> Any:
                fake.calls.append(("claim", vid))
                return fake.claim_result

            async def dispatch(self, ctx: Any, vid: Any, epoch: Any) -> Any:
                fake.calls.append(("dispatch", vid, epoch))
                if fake.dispatches:
                    return fake.dispatches.pop(0)
                return {"outcome": "exhausted"}

            async def record_answer(self, ctx: Any, attempt_id: Any, epoch: Any, echo: Any,
                                    result: Any) -> Any:
                fake.calls.append(("record_answer", attempt_id, epoch, echo, result))
                return {"outcome": "recorded"}

            async def timeout(self, ctx: Any, attempt_id: Any, epoch: Any) -> Any:
                fake.calls.append(("timeout", attempt_id, epoch))
                return {"outcome": "timed_out"}

            async def settle(self, ctx: Any, vid: Any, epoch: Any) -> Any:
                fake.calls.append(("settle", vid, epoch))
                return {"outcome": "settled", "class": "active", "reason": "coverage_active"}

        return Service()


def _dispatched(n: int, member: str = "M-1") -> dict[str, Any]:
    attempt = f"00000000-0000-4000-8000-0000000e2a0{n}"
    return {"outcome": "dispatched", "attempt_id": attempt, "n": n,
            "request": _request(member, attempt)}


def _worker(service: Any, port: Any, **kw: Any) -> Any:
    from haloflow.m05.eligibility import EligibilityWorker

    return EligibilityWorker(service, port, **kw)


async def test_el2_w01_happy_path_order_and_normalized_record() -> None:
    fake = FakeService()
    fake.dispatches = [_dispatched(1)]
    port = _port_mod().SimulatedClearinghouse(clock=lambda: T0)
    port.set_member("M-1", "active")
    outcome = await _worker(fake.make(), port).run_one(object(), "v-1")
    assert outcome == "settled"
    assert [c[0] for c in fake.calls] == ["claim", "dispatch", "record_answer", "settle"]
    rec = fake.calls[2]
    assert rec[1] == fake.calls[2][3]["attempt_id"] == "00000000-0000-4000-8000-0000000e2a01"
    assert rec[2] == 7 and rec[4]["class"] == "active"
    assert set(rec[4]) <= NORMALIZED_KEYS
    assert port.calls == ["00000000-0000-4000-8000-0000000e2a01"]


async def test_el2_w02_unavailable_then_retry_pending() -> None:
    fake = FakeService()
    fake.dispatches = [_dispatched(1), {"outcome": "retry_not_ready"}]
    port = _port_mod().SimulatedClearinghouse(clock=lambda: T0)
    port.set_member("M-1", "active")
    port.fail_next(1)
    assert await _worker(fake.make(), port).run_one(object(), "v-1") == "retry_pending"
    assert [c[0] for c in fake.calls] == ["claim", "dispatch", "timeout", "dispatch"]


async def test_el2_w03_a_hanging_port_is_bounded_and_timed_out() -> None:
    class Hang:
        calls: list[str] = []

        async def submit(self, attempt_id: str, request: Any) -> Any:
            self.calls.append(attempt_id)
            await asyncio.sleep(3600)

    fake = FakeService()
    fake.dispatches = [_dispatched(1), _dispatched(2)]
    port = Hang()
    got = await asyncio.wait_for(
        _worker(fake.make(), port, call_timeout_s=0.05).run_one(object(), "v-1"), BOUND_S)
    assert got == "settled"
    assert [c[0] for c in fake.calls] == ["claim", "dispatch", "timeout", "dispatch",
                                          "timeout", "dispatch", "settle"]
    assert len(port.calls) == len(set(port.calls)) == 2       # never the same id twice


@pytest.mark.parametrize("outcome", ["fenced", "superseded", "cancelled", "activation_off",
                                     "in_flight"])
async def test_el2_w04_refusals_end_the_run_without_a_port_call(outcome: str) -> None:
    fake = FakeService()
    fake.dispatches = [{"outcome": outcome}]
    port = _port_mod().SimulatedClearinghouse(clock=lambda: T0)
    assert await _worker(fake.make(), port).run_one(object(), "v-1") == outcome
    assert port.calls == []
    assert "settle" not in [c[0] for c in fake.calls]


async def test_el2_w04_not_claimed_and_answered_paths() -> None:
    fake = FakeService()
    fake.claim_result = {"outcome": "not_claimable"}
    port = _port_mod().SimulatedClearinghouse(clock=lambda: T0)
    assert await _worker(fake.make(), port).run_one(object(), "v-1") == "not_claimed"
    fake2 = FakeService()
    fake2.dispatches = [{"outcome": "answered"}]
    assert await _worker(fake2.make(), port).run_one(object(), "v-1") == "settled"
    assert [c[0] for c in fake2.calls] == ["claim", "dispatch", "settle"]
    assert port.calls == []


async def test_el2_w05_no_identifiers_in_worker_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger(WORKER_LOGGER).warning("e2 capture control")
    fake = FakeService()
    request = _request(SENTINELS[0], "00000000-0000-4000-8000-0000000e2a01")
    request.update(patient_name=SENTINELS[1], patient_dob=SENTINELS[2])
    fake.dispatches = [{"outcome": "dispatched", "attempt_id": request["attempt_id"], "n": 1,
                        "request": request}, _dispatched(2, SENTINELS[0])]
    port = _port_mod().SimulatedClearinghouse(clock=lambda: T0)
    port.set_member(SENTINELS[0], "active")
    port.fail_next(1)
    await _worker(fake.make(), port).run_one(object(), "v-1")
    text = "\n".join(r.getMessage() + repr(r.args) + str(r.exc_info) for r in caplog.records)
    assert "e2 capture control" in text
    for s in SENTINELS:
        assert s not in text, s


def test_el2_w00_worker_outcomes_closed() -> None:
    from haloflow.m05 import eligibility

    assert eligibility.WORKER_OUTCOMES == WORKER_OUTCOMES


# --- E2-P15 scheduler (unit layer; S-J pattern) -----------------------------------------------

T_A, T_B = se.TENANT_A, se.TENANT_B


def _sched_mod() -> Any:
    from haloflow.m05 import eligibility_scheduler

    return eligibility_scheduler


class Ctx:
    def __init__(self, tenant: str, n: int) -> None:
        self.tenant, self.n = tenant, n
        # The real TenantContext's field; the scheduler compares it with the scheduled
        # tenant before using the context (E3a correction, Codex dcd06e8c).
        self.tenant_id = tenant


class World:
    def __init__(self, clock: se.FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[Any, ...]] = []
        self.built = 0
        self.ids: dict[str, list[str]] = {}
        self.run_fn: Callable[[Any, str], Awaitable[str]] | None = None
        self.due_error: dict[str, Exception] = {}

    def contexts(self) -> Callable[[str], Awaitable[Any]]:
        async def contexts(tenant: str) -> Any:
            self.built += 1
            ctx = Ctx(tenant, self.built)
            self.calls.append(("context", tenant, ctx))
            return ctx

        return contexts

    def service(self) -> Any:
        from haloflow.m05.eligibility import EligibilityService

        world = self

        class Service(EligibilityService):
            def __init__(self) -> None:
                super().__init__(object())

            async def due(self, ctx: Any, limit: int = 100) -> list[str]:
                world.calls.append(("due", ctx, limit))
                if ctx.tenant in world.due_error:
                    raise world.due_error[ctx.tenant]
                return list(world.ids.get(ctx.tenant, []))

            async def expire(self, ctx: Any, limit: int = 100) -> int:
                world.calls.append(("expire", ctx, limit))
                return 0

            def __getattribute__(self, name: str) -> Any:
                if name in ("claim", "dispatch", "record_answer", "timeout", "settle"):
                    raise AssertionError(f"scheduler touched service.{name}")
                return super().__getattribute__(name)

        return Service()

    def worker(self) -> Any:
        from haloflow.m05.eligibility import EligibilityWorker

        world = self

        class Worker(EligibilityWorker):
            def __init__(self) -> None:
                super().__init__(object(), object())

            async def run_one(self, ctx: Any, vid: Any) -> str:
                world.calls.append(("run_one", ctx, str(vid)))
                if world.run_fn is not None:
                    return await world.run_fn(ctx, str(vid))
                await asyncio.sleep(0)
                return "settled"

        return Worker()

    def scheduler(self, tenants: tuple[str, ...] = (T_A,), **kw: Any) -> Any:
        kw.setdefault("interval_s", 60)
        kw.setdefault("stop_timeout_s", 1)
        kw.setdefault("batch_limit", 3)
        return _sched_mod().EligibilityScheduler(
            contexts=self.contexts(), service=self.service(), worker=self.worker(),
            tenants=tenants, clock=self.clock, sleep=self.clock.sleep, **kw)


def _vid(i: int) -> str:
    return f"00000000-0000-4000-8000-00000e2b{i:04d}"


@pytest.mark.parametrize(("kw", "code"), [
    ({"interval_s": 29}, "INTERVAL_INVALID"), ({"interval_s": 301}, "INTERVAL_INVALID"),
    ({"batch_limit": 0}, "BATCH_LIMIT_INVALID"), ({"batch_limit": 1001}, "BATCH_LIMIT_INVALID"),
    ({"tenants": ()}, "TENANTS_INVALID")])
def test_el2_p15_inputs_refused(kw: dict[str, Any], code: str) -> None:
    world = World(se.FakeClock())
    tenants = kw.pop("tenants", (T_A,))
    with pytest.raises(_sched_mod().EligibilitySchedulerRefused) as refused:
        world.scheduler(tenants, **kw)
    assert refused.value.code == code


def test_el2_p15_constants() -> None:
    m = _sched_mod()
    assert (m.DEFAULT_INTERVAL_S, m.INTERVAL_MIN_S, m.INTERVAL_MAX_S, m.DEFAULT_BATCH_LIMIT,
            m.BATCH_LIMIT_MAX) == (60, 30, 300, 100, 1000)


async def test_el2_p15_pass_order_fresh_contexts_and_batch_bound() -> None:
    world = World(se.FakeClock())
    world.ids = {T_A: [_vid(i) for i in range(5)], T_B: []}
    s = world.scheduler((T_A, T_B), batch_limit=2)
    await asyncio.wait_for(s.tick(), BOUND_S)
    shape = [(c[0], c[1] if isinstance(c[1], str) else c[1].tenant) for c in world.calls]
    assert shape == [("context", T_A), ("due", T_A), ("context", T_A), ("run_one", T_A),
                     ("context", T_A), ("run_one", T_A), ("expire", T_A),
                     ("context", T_B), ("due", T_B), ("expire", T_B)]
    assert [c[2] for c in world.calls if c[0] == "run_one"] == [_vid(0), _vid(1)]
    contexts = [c[2] for c in world.calls if c[0] == "context"]
    assert len({id(c) for c in contexts}) == len(contexts)
    await asyncio.wait_for(s.stop(), BOUND_S)


async def test_el2_p15_one_tenant_failure_never_stops_another() -> None:
    world = World(se.FakeClock())
    world.ids = {T_A: [_vid(1)], T_B: [_vid(2)]}
    world.due_error[T_A] = RuntimeError("SENTINEL-MEMBER-8841")
    s = world.scheduler((T_A, T_B))
    result = await asyncio.wait_for(s.tick(), BOUND_S)
    assert [c[2] for c in world.calls if c[0] == "run_one"] == [_vid(2)]
    assert [t.tenant for t in result.tenants] == [T_A, T_B]
    await asyncio.wait_for(s.stop(), BOUND_S)


async def test_el2_p15_lifecycle_and_bounded_stop_failed() -> None:
    m = _sched_mod()
    from haloflow.m05.due_scheduler import SchedulerState

    world = World(se.FakeClock())
    world.ids = {T_A: [_vid(1)]}
    release = asyncio.Event()

    async def hang(ctx: Any, vid: str) -> str:
        while True:
            try:
                await release.wait()
                return "settled"
            except asyncio.CancelledError:
                if release.is_set():
                    raise
                continue                                      # ignores cancel until released

    world.run_fn = hang
    s = world.scheduler(stop_timeout_s=0.05)
    assert s.state is SchedulerState.NEW
    s.start()
    assert s.state is SchedulerState.RUNNING
    with pytest.raises(m.EligibilitySchedulerRefused):
        await s.tick()
    for _ in range(2000):
        if any(c[0] == "run_one" for c in world.calls):
            break
        await asyncio.sleep(0)
    stopping = asyncio.ensure_future(s.stop())
    done, _ = await asyncio.wait({stopping}, timeout=BOUND_S)
    assert stopping in done
    with pytest.raises(m.EligibilitySchedulerStopFailed) as failed:
        stopping.result()
    assert failed.value.code == "STOP_TIMEOUT"
    assert s.state is SchedulerState.STOP_FAILED
    release.set()
    later = asyncio.ensure_future(s.stop())
    done, _ = await asyncio.wait({later}, timeout=BOUND_S)
    assert later in done and later.exception() is None
    assert s.state is SchedulerState.STOPPED


async def test_el2_p15_summary_line_codes_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=SCHED_LOGGER)
    world = World(se.FakeClock())
    world.ids = {T_A: [_vid(1)]}
    world.due_error[T_B] = RuntimeError(SENTINELS[0])
    s = world.scheduler((T_A, T_B))
    await asyncio.wait_for(s.tick(), BOUND_S)
    lines = [r.getMessage() for r in caplog.records if r.name == SCHED_LOGGER]
    assert len(lines) == 1 and lines[0].startswith("eligibility_scheduler tick=0 ")
    for s_ in SENTINELS:
        assert s_ not in "\n".join(r.getMessage() + repr(r.args) for r in caplog.records)
    await asyncio.wait_for(s.stop(), BOUND_S)


def test_el2_p15_scheduler_calls_only_due_run_one_expire_and_is_not_composed() -> None:
    text = SCHED_FILE.read_text(encoding="utf-8")
    for name in ("claim(", "dispatch(", "record_answer(", "timeout(", "settle(", "submit("):
        assert f".{name}" not in text, name
    for path in (SRC / "composition.py", SRC / "startup.py"):
        assert "eligibility" not in path.read_text(encoding="utf-8"), path.name
