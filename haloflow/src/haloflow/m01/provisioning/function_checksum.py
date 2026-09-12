"""Function-v3 canonical serialization, separate from SQL-policy validation.

These APIs validate the serialization domain and capture policy/verification drift.
Issuers must separately validate SQL, policy safety and their mutual consistency.
Legacy version-2 serialization remains in :mod:`checksum`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from typing import Any, Final

from .checksum import normalize_body

FUNCTION_CHECKSUM_VERSION: Final = 3

_ROOT = frozenset(
    {"checksum_version", "migration_id", "execution_role", "template", "verification", "policy"}
)
_POLICY = frozenset(
    {
        "policy_format",
        "semantic_version",
        "parser_package",
        "parser_version",
        "grammar_major",
        "functions",
    }
)
_FUNCTION = frozenset(
    {
        "schema",
        "name",
        "inputs",
        "outputs",
        "return_type",
        "language",
        "is_procedure",
        "replace",
        "security_definer",
        "volatility",
        "parallel",
        "strict",
        "config",
        "body_sha256",
        "acl",
        "comment",
    }
)
_VERIFICATION_FUNCTION = frozenset(
    {"name", "argument_types", "owner", "security_definer", "config", "acl", "body"}
)


def _normalize(value: Any) -> Any:
    """Copy strict JSON values, normalizing every string without losing whitespace."""
    if isinstance(value, str):
        return normalize_body(value)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            key = normalize_body(key)
            if key in result:
                raise ValueError("Duplicate normalized JSON key")
            result[key] = _normalize(item)
        return result
    raise ValueError("Unsupported JSON value")


def _record(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Missing or unknown record fields")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected string")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Expected array")
    return value


def _signature(value: Any) -> tuple[str, ...]:
    types = []
    for item in _array(value):
        record = _record(item, frozenset({"name", "type"}))
        if record["name"] is not None:
            _string(record["name"])
        types.append(_string(record["type"]))
    return tuple(types)


def _policy_identity(value: Any) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError("Expected function record")
    return (
        _string(value.get("schema")),
        _string(value.get("name")),
        _signature(value.get("inputs")),
    )


def _verification_identity(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError("Expected verification function record")
    return (
        _string(value.get("name")),
        tuple(_string(item) for item in _array(value.get("argument_types"))),
    )


def _grantee(value: Any) -> str:
    record = _record(value, frozenset({"grantee", "privileges"}))
    return _string(record["grantee"])


def _config_key(value: Any) -> str:
    text = _string(value)
    key, separator, _ = text.partition("=")
    if not key or not separator:
        raise ValueError("Configuration must have a key and equals separator")
    return key


# Wildcards represent array membership only, never arbitrary object keys.
_SET_PATHS: Final[dict[tuple[str, ...], Callable[[Any], Any]]] = {
    ("policy", "functions"): _policy_identity,
    ("verification", "functions"): _verification_identity,
    **{(section, "functions", "*", "acl"): _grantee for section in ("policy", "verification")},
    **{
        (section, "functions", "*", "acl", "*", "privileges"): _string
        for section in ("policy", "verification")
    },
    **{
        (section, "functions", "*", "config"): _config_key for section in ("policy", "verification")
    },
}


def _order(value: Any, path: tuple[str, ...] = ()) -> Any:
    if path in _SET_PATHS:
        items = _array(value)
        key = _SET_PATHS[path]
        identities = [key(item) for item in items]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate set identity")
        value = sorted(items, key=key)
    if isinstance(value, dict):
        return {key: _order(item, (*path, key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_order(item, (*path, "*")) for item in value]
    return value


def _acl_and_config(function: dict[str, Any]) -> None:
    for item in _array(function["acl"]):
        record = _record(item, frozenset({"grantee", "privileges"}))
        _string(record["grantee"])
        for privilege in _array(record["privileges"]):
            _string(privilege)
    for item in _array(function["config"]):
        _config_key(item)


def _validate(payload: Any) -> None:
    root = _record(payload, _ROOT)
    if type(root["checksum_version"]) is not int or root["checksum_version"] != 3:
        raise ValueError("Expected function checksum version 3")
    for field in ("migration_id", "execution_role", "template"):
        _string(root[field])
    policy = _record(root["policy"], _POLICY)
    for field in ("policy_format", "semantic_version", "grammar_major"):
        if type(policy[field]) is not int:
            raise ValueError("Expected integer policy metadata")
    for field in ("parser_package", "parser_version"):
        _string(policy[field])
    for item in _array(policy["functions"]):
        function = _record(item, _FUNCTION)
        for field in (
            "schema",
            "name",
            "return_type",
            "language",
            "volatility",
            "parallel",
            "body_sha256",
        ):
            _string(function[field])
        for field in ("is_procedure", "replace", "security_definer", "strict"):
            if type(function[field]) is not bool:
                raise ValueError("Expected boolean function flag")
        if function["comment"] is not None:
            _string(function["comment"])
        _signature(function["inputs"])
        _signature(function["outputs"])
        _acl_and_config(function)
    verification = _record(root["verification"], frozenset({"kind", "functions"}))
    _string(verification["kind"])
    for item in _array(verification["functions"]):
        function = _record(item, _VERIFICATION_FUNCTION)
        for field in ("name", "owner", "body"):
            _string(function[field])
        if type(function["security_definer"]) is not bool:
            raise ValueError("Expected boolean security_definer")
        for argument in _array(function["argument_types"]):
            _string(argument)
        _acl_and_config(function)


def _encode(payload: Any) -> bytes:
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (UnicodeError, TypeError) as exc:
        raise ValueError("Payload cannot be encoded as strict UTF-8 JSON") from exc


def _canonical_ordering_bytes(payload: Any) -> bytes:
    """Test-only ordering seam; acceptance does not imply a valid function payload."""
    return _encode(_order(_normalize(payload)))


def canonical_function_bytes(payload: Any) -> bytes:
    """Validate the v3 serialization domain and return its canonical UTF-8 bytes."""
    normalized = _normalize(payload)
    _validate(normalized)
    return _encode(_order(normalized))


def function_payload(
    *,
    migration_id: str,
    template: str,
    execution_role: str,
    verification: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Build a normalized v3 payload, without mutating caller-owned values."""
    payload = _normalize(
        {
            "checksum_version": FUNCTION_CHECKSUM_VERSION,
            "migration_id": migration_id,
            "execution_role": execution_role,
            "template": template,
            "verification": verification,
            "policy": policy,
        }
    )
    _validate(payload)
    result: dict[str, Any] = _order(payload)
    return result


def function_checksum(
    *,
    migration_id: str,
    template: str,
    execution_role: str,
    verification: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    """Hash the same canonical bytes exposed by canonical_function_bytes."""
    payload = function_payload(
        migration_id=migration_id,
        template=template,
        execution_role=execution_role,
        verification=verification,
        policy=policy,
    )
    return hashlib.sha256(canonical_function_bytes(payload)).hexdigest()
