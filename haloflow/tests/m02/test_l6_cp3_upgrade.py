"""L-6 CP-3: pure capability validation and step dispatch (tier U).

Tier U: no database. Never R-L6.X2 evidence (test cases v4 R1).

Traceability:
- TC-F03a (test cases v4 §2): capability self-check, RC-04 before any connection.
  Variants per owner decision Q2 (record f3c8b5e9…03ed): one per carried field.
- TC-A03-b (IP-15/16 v2 `2068f441…de48`): direct validation opens no connection
  and appends nothing.
- TC-G01b (owner records f3c8b5e9…03ed, de5024f1…508c): component; the pure
  dispatch offers only CLc then FN at K11, and after CLc every step except FN is
  refused with RC-08 / claim. Not database evidence; the real-K11 path stays
  with TC-F10 (CP-5).

Interface: B5.1 v3 `9494f6e0…272e` §4. Imports are inside each test so a missing
symbol fails that node only (plan v4 §6.1, D-sym); the exact pre-change site per node
is in the manifest. Every assertion a mutant must break carries a unique message
("TC-…: …"), including the missing-refusal message "<case>: did not refuse", so the
mutation map names the exact failing assertion.
"""

from __future__ import annotations

import dataclasses
from uuid import UUID

import pytest

TENANT = "l6cp3-u-tenant"
OPERATION = UUID("00000000-0000-4000-8000-0000000000a1")
ATTEMPT = UUID("00000000-0000-4000-8000-0000000000b1")
CARRIED_FIELDS = ("tenant_id", "operation_id", "attempt_id", "key", "k_pid", "database")
ALL_STEPS = (
    "S0",
    "S1",
    "S2",
    "S3",
    "C1",
    "CL",
    "CLc",
    "C2",
    "C3",
    "AB",
    "NB",
    "N0",
    "ST",
    "NR",
    "N2",
    "NX",
    "DR",
    "XE",
    "LO",
    "A2r",
    "A2i",
    "A2f",
    "C2b",
    "V",
    "AC",
    "RL1",
    "RL2",
    "FN",
)


def _valid_capability():  # type: ignore[no-untyped-def]
    from haloflow.m01.provisioning.runner import tenant_lock_key
    from haloflow.m01.provisioning.upgrade import MaintenanceCapability

    return MaintenanceCapability(
        tenant_id=TENANT,
        operation_id=OPERATION,
        attempt_id=ATTEMPT,
        key=tenant_lock_key(TENANT),
        k_pid=4242,
        database="haloflow_test",
    )


@pytest.fixture
def no_connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any attempt to open a database connection during the call under test."""

    import psycopg

    opened: list[str] = []

    def _refuse(*_args: object, **_kwargs: object) -> object:
        opened.append("connect")
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _refuse)
    monkeypatch.setattr(psycopg.Connection, "connect", _refuse)
    return opened


def _refused(call, case: str):  # type: ignore[no-untyped-def]
    """Return the MaintenanceRefused raised by `call`. A normal return fails here with the
    mapped message `<case>: did not refuse`; any other exception propagates unchanged and
    is never accepted as the refusal (Codex B5.2 v1 finding 4)."""

    from haloflow.m01.provisioning.upgrade import MaintenanceRefused

    try:
        call()
    except MaintenanceRefused as refused:
        return refused
    raise AssertionError(f"{case}: did not refuse")


def _assert_token_invalid(error: BaseException, case: str) -> None:
    assert (getattr(error, "reason_code", None), getattr(error, "phase", None)) == (
        "MAINTENANCE_TOKEN_INVALID",
        "capability",
    ), f"{case}: code/phase"


# --- TC-F03a ----------------------------------------------------------------


def test_tc_f03a_key_mismatch_refused_before_any_connection(no_connections: list[str]) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    cap = dataclasses.replace(_valid_capability(), key=_valid_capability().key ^ 1)
    error = _refused(lambda: validate_capability(cap), "TC-F03a key_mismatch")
    _assert_token_invalid(error, "TC-F03a key_mismatch")
    assert no_connections == [], "TC-F03a key_mismatch: no connection"


@pytest.mark.parametrize("field", CARRIED_FIELDS)
def test_tc_f03a_missing_field_refused_before_any_connection(
    field: str, no_connections: list[str]
) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    cap = dataclasses.replace(_valid_capability(), **{field: None})
    error = _refused(lambda: validate_capability(cap), f"TC-F03a missing_{field}")
    _assert_token_invalid(error, f"TC-F03a missing_{field}")
    assert no_connections == [], f"TC-F03a missing_{field}: no connection"


def test_tc_f03a_control_valid_capability_passes(no_connections: list[str]) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    validate_capability(_valid_capability())
    assert no_connections == [], "TC-F03a control: no connection"


# --- TC-A03-b ---------------------------------------------------------------


def test_tc_a03_b_direct_validation_opens_nothing_and_appends_nothing(
    no_connections: list[str],
) -> None:
    """Direct validation is pure: no connection, therefore no evidence append (IP-14 A1)."""

    from haloflow.m01.provisioning.upgrade import validate_capability

    cap = dataclasses.replace(_valid_capability(), attempt_id=None)
    error = _refused(lambda: validate_capability(cap), "TC-A03-b")
    _assert_token_invalid(error, "TC-A03-b")
    assert no_connections == [], "TC-A03-b: no connection, so nothing can be appended"


# --- TC-G01b (component, U) -------------------------------------------------


def test_tc_g01b_k11_offers_only_clc_then_fn() -> None:
    from haloflow.m01.provisioning.upgrade import Classification, KState, next_steps

    k11 = Classification(state=KState.K11, generation=None, reason=None)
    assert next_steps(k11, live=False) == ("CLc", "FN"), "TC-G01b: K11 new run"


@pytest.mark.parametrize("step", [s for s in ALL_STEPS if s != "FN"])
def test_tc_g01b_after_clc_every_other_step_refused(step: str) -> None:
    from haloflow.m01.provisioning.upgrade import require_step_allowed

    error = _refused(lambda: require_step_allowed("CLc", step), f"TC-G01b {step}")
    assert (error.reason_code, error.phase) == ("MAINTENANCE_CLAIM_REFUSED", "claim"), (
        f"TC-G01b {step}: code/phase"
    )


def test_tc_g01b_after_clc_fn_allowed() -> None:
    from haloflow.m01.provisioning.upgrade import require_step_allowed

    require_step_allowed("CLc", "FN")
