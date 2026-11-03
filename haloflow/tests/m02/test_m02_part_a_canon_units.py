"""UC1 part A — m02.canon/v1, key and fingerprint vectors (unit layer, no database).

Rows: PA-19 (M11 key, service half), PA-26 (FP-V1, service half), PA-42 (CV1-CV4
byte equality), PA-43 (reject vectors at the oracle and service raw-input
layers), PA-44 (CV0 contrast). The SQL halves of PA-19/26/42/43 live in
`test_m02_part_a_canon_postgres.py`.

Frozen service interface these tests encode (decision CN-5, CN-6):
- `haloflow.m02.canon.canon_bytes(value) -> bytes`
- `haloflow.m02.canon.load_command(text: str) -> Any`, raising
  `haloflow.m02.canon.RawInputRejected` (attribute `reason_code`) for the raw
  rejects; it is the raw-input layer that runs before JSONB conversion.
- `haloflow.m02.canon.m11_dedupe_key(tenant_id, source_event_id, handoff_kind,
  signal_type) -> str`
- `haloflow.m02.canon.content_fingerprint_preimage(...)` is NOT frozen here; the
  SQL body is authoritative for fingerprints (Δ2-F) and PA-26 checks the SQL value.

Imports of `haloflow.m02.canon` are per test, so each row is individually
visible at the baseline (class S, structural) instead of one collection error.
Oracle-only rows are class G (they check the frozen oracle against the frozen
bytes, which is a property of the test material, not of the implementation).
"""

from types import ModuleType
from typing import Any

import pytest


def _service() -> Any:
    import importlib

    return importlib.import_module("haloflow.m02.canon")


# --- oracle self-consistency (class G) -------------------------------------


def test_pa42_oracle_matches_frozen_bytes(part_a: ModuleType) -> None:
    for vector in (*part_a.CANON_VECTORS, *part_a.TYPE_VECTORS):
        data = part_a.canon_bytes(vector.logical)
        assert data.hex() == vector.hex, vector.name
        assert part_a.sha256_hex(data) == vector.sha256, vector.name


def test_pa44_cv0_codepoint_order_differs_from_cv1(part_a: ModuleType) -> None:
    cv1 = next(v for v in part_a.CANON_VECTORS if v.name == "CV1")
    assert cv1.hex != part_a.CV0_CODEPOINT_ORDER_HEX
    # Same members, different order: the contrast is about ordering only.
    assert sorted(bytes.fromhex(part_a.CV0_CODEPOINT_ORDER_HEX)) == sorted(bytes.fromhex(cv1.hex))


@pytest.mark.parametrize(
    "label", ["int_above", "int_below", "fraction", "integral_float", "surrogate", "nul"]
)
def test_pa43_oracle_rejects(part_a: ModuleType, label: str) -> None:
    value = dict(part_a.CANON_REJECTS)[label]
    with pytest.raises(part_a.CanonReject):
        part_a.canon_bytes({"x": value})


def test_pa19_pa26_oracle_vectors(part_a: ModuleType) -> None:
    assert part_a.m11_key(*part_a.M11_V1_PREIMAGE[2:]) == part_a.M11_V1_KEY
    superseded = ["m02.handoff/v1", "M11", *part_a.M11_V1_PREIMAGE[2:4], "m02.event.appended"]
    assert "m02h1:" + part_a.sha256_hex(part_a.canon_bytes(superseded)) == (
        part_a.M11_V0_KEY_SUPERSEDED
    )
    assert "m05r1:" + part_a.sha256_hex(part_a.canon_bytes(list(part_a.MK_V1_PREIMAGE))) == (
        part_a.MK_V1_KEY
    )
    assert part_a.sha256_hex(part_a.canon_bytes(list(part_a.FP_V1_ARRAY))) == part_a.FP_V1_SHA256


# --- service implementation (class S at baseline) ---------------------------


@pytest.mark.parametrize("name", ["CV1", "CV2", "CV3", "CV4", "CT1", "CT2"])
def test_pa42_service_canon_bytes(part_a: ModuleType, name: str) -> None:
    vector = next(v for v in (*part_a.CANON_VECTORS, *part_a.TYPE_VECTORS) if v.name == name)
    assert _service().canon_bytes(vector.logical).hex() == vector.hex


def test_pa44_service_is_not_codepoint_ordered(part_a: ModuleType) -> None:
    cv1 = next(v for v in part_a.CANON_VECTORS if v.name == "CV1")
    assert _service().canon_bytes(cv1.logical).hex() != part_a.CV0_CODEPOINT_ORDER_HEX


@pytest.mark.parametrize(
    "label", ["int_above", "int_below", "fraction", "integral_float", "surrogate", "nul"]
)
def test_pa43_service_canon_rejects(part_a: ModuleType, label: str) -> None:
    service = _service()
    value = dict(part_a.CANON_REJECTS)[label]
    with pytest.raises(service.RawInputRejected):
        service.canon_bytes({"x": value})


@pytest.mark.parametrize(
    "label",
    [
        "negative_zero",
        "negative_zero_fraction",
        "exponent",
        "fraction",
        "duplicate_key",
        "integral_fraction",
        "surrogate_escape",
        "nul_escape",
        "duplicate_nested_key",
    ],
)
def test_pa43_service_raw_input_rejects(part_a: ModuleType, label: str) -> None:
    service = _service()
    text = dict(part_a.RAW_INPUT_REJECTS)[label]
    with pytest.raises(service.RawInputRejected) as caught:
        service.load_command(text)
    assert caught.value.reason_code == "COMMAND_MALFORMED"


def test_pa43_service_raw_input_accepts_zero(part_a: ModuleType) -> None:
    assert _service().load_command('{"x":0}') == {"x": 0}


def test_pa19_service_m11_key(part_a: ModuleType) -> None:
    key = _service().m11_dedupe_key(*part_a.M11_V1_PREIMAGE[2:])
    assert key == part_a.M11_V1_KEY
    assert key != part_a.M11_V0_KEY_SUPERSEDED


def test_pa21_service_m11_key_rejects_bad_tenant(part_a: ModuleType) -> None:
    service = _service()
    for tenant in ('pilot"clinic', "pilot-clinic-\u00e9", "Pilot-Clinic", "a"):
        with pytest.raises(service.RawInputRejected):
            service.m11_dedupe_key(tenant, *part_a.M11_V1_PREIMAGE[3:])


# --- PA-14 ACL oracle controls (class G; Codex 2a872210 point 1) -----------

_OWNER = "haloflow_m02_append_owner"
_EXEC = frozenset({"haloflow_m02_append_owner", "haloflow_m02_projection_owner"})


def test_pa14_acl_oracle_positive_control(part_a: ModuleType) -> None:
    acl = [f"{_OWNER}=X/{_OWNER}", f"haloflow_m02_projection_owner=X/{_OWNER}"]
    assert part_a.function_acl_problems(acl, _OWNER, _EXEC) == []


@pytest.mark.parametrize(
    "acl",
    [
        None,
        [f"{_OWNER}=X/{_OWNER}", f"haloflow_m02_projection_owner=X/{_OWNER}", f"=X/{_OWNER}"],
        [
            f"{_OWNER}=X/{_OWNER}",
            f"haloflow_m02_projection_owner=X/{_OWNER}",
            f"haloflow_runtime=X/{_OWNER}",
        ],
        [f"{_OWNER}=X/{_OWNER}", f"haloflow_m02_projection_owner=X*/{_OWNER}"],
        [f"{_OWNER}=X/{_OWNER}"],
    ],
    ids=["null_acl", "public", "extra_role", "grant_option", "missing_executor"],
)
def test_pa14_acl_oracle_negative_controls(part_a: ModuleType, acl: list[str] | None) -> None:
    assert part_a.function_acl_problems(acl, _OWNER, _EXEC) != []
