"""Review draft: thin real-serializer dispatch. No expected-file access."""

import json


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate raw JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite JSON value")


def canonicalize(raw_json_bytes, vector_id):
    if type(raw_json_bytes) is not bytes:
        raise ValueError("raw bytes required")
    payload = json.loads(
        raw_json_bytes.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
    )
    if not isinstance(payload, dict):
        raise ValueError("mapping required")
    version = payload.get("checksum_version")
    if version is None:
        if vector_id != "main/ordered-unregistered" or set(payload) != {"specimen"}:
            raise ValueError("unknown unversioned scope")
        from haloflow.m01.provisioning.function_checksum import _canonical_ordering_bytes

        return _canonical_ordering_bytes(payload)
    if type(version) is not int:
        raise ValueError("integer version required")
    if version == 2:
        from haloflow.m01.provisioning import checksum

        if set(payload) != {
            "checksum_version",
            "migration_id",
            "execution_role",
            "template",
            "verification",
        }:
            raise ValueError("v2 key set mismatch")
        fields = {k: v for k, v in payload.items() if k != "checksum_version"}
        return checksum.canonical_json(checksum.unit_payload(**fields)).encode("utf-8")
    if version == 3:
        from haloflow.m01.provisioning.function_checksum import canonical_function_bytes

        return canonical_function_bytes(payload)
    raise ValueError("unsupported checksum version")
