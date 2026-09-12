"""Exact review draft. Proposed production API is absent; do not claim red/green."""

import copy
import hashlib
import importlib.util
import json
from enum import Enum
from pathlib import Path

import pytest

from haloflow.m01.provisioning import checksum
from haloflow.m01.provisioning.function_checksum import (
    FUNCTION_CHECKSUM_VERSION,
    _canonical_ordering_bytes,
    canonical_function_bytes,
    function_checksum,
    function_payload,
)

SUPPORT = Path(__file__).resolve().parent / "support"
_adapter_spec = importlib.util.spec_from_file_location(
    "cp1_serializer_adapter", SUPPORT / "serializer_adapter.py"
)
_adapter_module = importlib.util.module_from_spec(_adapter_spec)
_adapter_spec.loader.exec_module(_adapter_module)
canonicalize = _adapter_module.canonicalize


PACKAGE = Path(__file__).resolve().parent / "fixtures/function_checksum"
ROWS = json.loads((PACKAGE / "vector-manifest.json").read_text())


def base():
    return json.loads((PACKAGE / "vectors/main/function-v3-candidate.input.json").read_text())


def test_exact_vector_inventory():
    assert len(ROWS) == 19
    assert len({r["id"] for r in ROWS}) == 19
    assert {
        g: sum(r["id"].startswith(g + "/") for r in ROWS) for g in ("main", "comment", "gateway")
    } == {"main": 13, "comment": 2, "gateway": 4}


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["id"])
def test_published_bytes_and_digests(row):
    raw = (PACKAGE / row["input"]).read_bytes()
    expected = (PACKAGE / row["expected"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row["input_sha256"]
    assert hashlib.sha256(expected).hexdigest() == row["expected_sha256"]
    actual = canonicalize(raw, row["id"])
    assert type(actual) is bytes
    assert actual == expected
    assert hashlib.sha256(actual).hexdigest() == row["expected_sha256"]


def test_v3_shared_normalize_body_contract():
    assert checksum.normalize_body("a\r\nb\rc\n") == "a\nb\nc\n"
    assert checksum.normalize_body("cafe\u0301") == "café"
    assert checksum.normalize_body("café") == "café"
    assert (
        checksum.normalize_body("  BEGIN\n\tRETURN  1;  \nEND;\n")
        == "  BEGIN\n\tRETURN  1;  \nEND;\n"
    )
    assert checksum.normalize_body("RETURN  1;") != checksum.normalize_body("RETURN 1;")


def test_versions_keys_and_fixed_legacy_digests():
    assert checksum.CHECKSUM_VERSION == 2
    assert FUNCTION_CHECKSUM_VERSION == 3
    for name, digest in [
        ("legacy-t001", "5e232d1563edb413560a939b4564194b268ebda631266900f1ddfa5aa75d0ebd"),
        ("legacy-role-set", "e1bc9ac7564c95b48c5c39240d25759b702ef5ced48c9fe976fc3a6091fbda82"),
    ]:
        raw = (PACKAGE / ("vectors/main/" + name + ".input.json")).read_bytes()
        result = canonicalize(raw, "main/" + name)
        assert set(json.loads(result)) == {
            "checksum_version",
            "migration_id",
            "execution_role",
            "template",
            "verification",
        }
        assert hashlib.sha256(result).hexdigest() == digest


def test_public_payload_and_checksum_use_same_bytes_without_mutating_inputs():
    source = base()
    original = copy.deepcopy(source)
    kwargs = {k: v for k, v in source.items() if k != "checksum_version"}
    payload = function_payload(**kwargs)
    assert set(payload) == {
        "checksum_version",
        "migration_id",
        "execution_role",
        "template",
        "verification",
        "policy",
    }
    assert payload["checksum_version"] == 3
    assert canonical_function_bytes(payload) == canonical_function_bytes(source)
    assert (
        function_checksum(**kwargs) == hashlib.sha256(canonical_function_bytes(source)).hexdigest()
    )
    assert source == original
    with pytest.raises(TypeError):
        function_payload(checksum_version=2, **kwargs)


@pytest.mark.parametrize("version", [2, 4, True, "3", None])
def test_v3_entry_rejects_other_versions(version):
    p = base()
    p["checksum_version"] = version
    with pytest.raises(ValueError):
        canonical_function_bytes(p)


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}']
)
def test_adapter_raw_duplicate_and_nonfinite_refusal(raw):
    with pytest.raises(ValueError):
        canonicalize(raw, "main/function-v3-candidate")


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf"), b"bytes", {"a"}, object()]
)
def test_primitive_rejects_unsupported_values(bad):
    with pytest.raises(ValueError):
        _canonical_ordering_bytes({"value": bad})


def test_normalized_key_collision_and_nonstring_key_refusal():
    with pytest.raises(ValueError):
        _canonical_ordering_bytes({"café": 1, "cafe\u0301": 2})
    with pytest.raises(ValueError):
        _canonical_ordering_bytes({1: "one"})


@pytest.mark.parametrize(
    "area", ["root", "policy", "function", "verification", "verification_function"]
)
def test_closed_schema_unknown_fields(area):
    p = base()
    target = {
        "root": p,
        "policy": p["policy"],
        "function": p["policy"]["functions"][0],
        "verification": p["verification"],
        "verification_function": p["verification"]["functions"][0],
    }[area]
    target["unknown_field"] = 1
    with pytest.raises(ValueError):
        canonical_function_bytes(p)


@pytest.mark.parametrize("area", ["policy", "verification"])
@pytest.mark.parametrize("kind", ["function", "acl", "privilege", "config"])
def test_duplicates_refused_before_checksum(area, kind):
    p = base()
    group = p[area]["functions"]
    f = group[0]
    if kind == "function":
        group.append(copy.deepcopy(f))
    elif kind == "acl":
        f["acl"].append(copy.deepcopy(f["acl"][0]))
    elif kind == "privilege":
        f["acl"][0]["privileges"].append(f["acl"][0]["privileges"][0])
    else:
        f["config"].append(f["config"][0])
    with pytest.raises(ValueError):
        canonical_function_bytes(p)


@pytest.mark.parametrize("area", ["policy", "verification"])
@pytest.mark.parametrize("kind", ["functions", "acl", "privileges", "config"])
def test_malformed_set_members_are_not_dropped(area, kind):
    p = base()
    f = p[area]["functions"][0]
    if kind == "functions":
        p[area]["functions"].append(42)
    elif kind == "privileges":
        f["acl"][0]["privileges"].append(42)
    else:
        f[kind].append(42)
    with pytest.raises(ValueError):
        canonical_function_bytes(p)


def test_unregistered_paths_never_sort_by_key_name():
    payload = {"specimen": {"privileges": ["z", "a"], "config": ["z=1", "a=2"]}}
    assert (
        _canonical_ordering_bytes(payload)
        == b'{"specimen":{"config":["z=1","a=2"],"privileges":["z","a"]}}'
    )
    with pytest.raises(ValueError):
        canonicalize(b'{"specimen":{}}', "not-the-primitive-scope")


def test_policy_and_verification_changes_affect_digest_independently():
    original = base()
    for section in ("policy", "verification"):
        changed = copy.deepcopy(original)
        changed[section]["functions"][0]["security_definer"] = False
        assert canonical_function_bytes(changed) != canonical_function_bytes(original)
    # Serialization captures drift; full SQL/security-policy approval remains separate.


def test_enum_fallback_and_unsupported_evidence_value():
    spec = importlib.util.spec_from_file_location(
        "reviewed_parser_probe", SUPPORT / "cp1_parser_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Sample(Enum):
        TABLE = "t"

    assert module.encode_ast(Sample.TABLE) == {
        "enum_class": "Sample",
        "name": "TABLE",
        "value": "t",
    }
    with pytest.raises(TypeError):
        module.encode_ast(object())
    with pytest.raises(ValueError):
        json.dumps({"value": float("nan")}, default=module.encode_ast, allow_nan=False)


@pytest.mark.parametrize("area", ["policy", "verification"])
def test_function_and_acl_set_permutations_canonicalize(area):
    p = base()
    second = copy.deepcopy(p[area]["functions"][0])
    second["name"] = "z_second_probe"
    p[area]["functions"].append(second)
    reordered = copy.deepcopy(p)
    reordered[area]["functions"].reverse()
    for f in reordered[area]["functions"]:
        f["acl"].reverse()
    assert canonical_function_bytes(reordered) == canonical_function_bytes(p)


@pytest.mark.parametrize(
    "path", [("policy", "inputs"), ("policy", "outputs"), ("verification", "argument_types")]
)
def test_ordered_signature_arrays_remain_ordered(path):
    area, key = path
    p = base()
    if key == "outputs":
        p = json.loads((PACKAGE / "vectors/gateway/begin-key-scope-shape.input.json").read_text())
    q = copy.deepcopy(p)
    q[area]["functions"][0][key].reverse()
    assert canonical_function_bytes(q) != canonical_function_bytes(p)


def test_primitive_registered_config_order_and_duplicate_refusal():
    # Ordering primitive only: extra configuration keys are not production-policy permission.
    for area in ("policy", "verification"):
        p = base()
        p[area]["functions"][0]["config"] = ["z=1", "a=2"]
        q = copy.deepcopy(p)
        q[area]["functions"][0]["config"].reverse()
        assert _canonical_ordering_bytes(p) == _canonical_ordering_bytes(q)
        p[area]["functions"][0]["config"] = ["a=1", "a=2"]
        with pytest.raises(ValueError):
            _canonical_ordering_bytes(p)


def test_literal_v3_candidate_anchor():
    raw = (PACKAGE / "vectors/main/function-v3-candidate.input.json").read_bytes()
    actual = canonicalize(raw, "main/function-v3-candidate")
    assert (
        hashlib.sha256(actual).hexdigest()
        == "a5b907e508b32cd8e956387eff30407339b8ad46255913e4860907e224987083"
    )
