"""Canonical bytes, digests and the strict JSON reader (architecture v2, A3).

UTF-8 JSON, object keys sorted by Unicode scalar value, literal non-ASCII, standard JSON
escaping, no insignificant whitespace. Floats, duplicate keys and malformed Unicode are
rejected. Arrays held under a schema-declared set key are sorted by their canonical element
bytes with exact duplicates removed; every other array keeps its order (a sequence).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

# Request/bundle keys whose arrays are sets (architecture v2: "the schema declares which
# collections are sets"). Expression operands, ranked orders and zone transitions are
# sequences and keep their order.
SET_KEYS: Final = frozenset({
    "evidence", "coverages", "services", "suggestions", "obligations", "dispositions",
    "permissions", "templates", "questions", "rules", "question_types", "depends_on",
    "closes", "codes",
})


class CanonicalError(ValueError):
    """A value that has no canonical encoding."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check_text(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalError("malformed_unicode") from exc
    return text


def normalize(value: Any, *, key: str | None = None) -> Any:
    """Return the canonical form of `value` (plain dicts, lists, str, int, bool, None)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        raise CanonicalError("float_value")
    if isinstance(value, str):
        return _check_text(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError("unsupported_type")
            out[_check_text(k)] = normalize(v, key=k)
        return dict(sorted(out.items()))
    if isinstance(value, (list, tuple)):
        items = [normalize(v) for v in value]
        if key in SET_KEYS:
            unique: dict[bytes, Any] = {}
            for item in items:
                unique.setdefault(_dump(item), item)
            items = [unique[b] for b in sorted(unique)]
        return items
    raise CanonicalError("unsupported_type")


def _dump(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_bytes(value: object) -> bytes:
    return _dump(normalize(value))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _reject_float(_text: str) -> Any:
    raise CanonicalError("float_value")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise CanonicalError("duplicate_key")
        out[k] = v
    return out


def parse_json(text: bytes) -> object:
    """Strict reader: duplicate keys, floats and malformed UTF-8 are rejected."""
    try:
        decoded = text.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CanonicalError("malformed_unicode") from exc
    try:
        value = json.loads(decoded, object_pairs_hook=_pairs, parse_float=_reject_float,
                           parse_constant=_reject_float)
    except CanonicalError:
        raise
    except json.JSONDecodeError as exc:
        raise CanonicalError("malformed_json") from exc
    normalize(value)  # surrogates and other non-canonical content
    return value
