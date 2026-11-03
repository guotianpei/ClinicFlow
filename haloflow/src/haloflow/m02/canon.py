"""m02.canon/v1 canonical serialization and the service raw-input boundary (UC1 part A).

Decision CN-1..CN-6 (tenant-binding/canonicalization decision v1, Codex-aligned):

- Value domain: null, true, false, strings, integers in [-(2**53-1), 2**53-1],
  arrays and objects. Floats, fractions, exponents, negative zero, U+0000,
  unpaired surrogates and duplicate object keys are refused.
- Serialization: no insignificant whitespace, UTF-8, shortest integer decimal,
  strings unchanged (no Unicode normalization), array order kept, object keys
  sorted by UTF-16 code-unit sequence.
- Escaping: ``"`` and ``\\`` escaped; U+0008/09/0A/0C/0D as ``\\b \\t \\n \\f \\r``;
  other U+0001..U+001F as lowercase ``\\u00xx``; everything else raw UTF-8.

This profile is NOT claimed to be RFC 8785 (JCS) compliant. ``load_command`` is the
raw-input layer: it rejects the lexical forms JSONB conversion would erase
(duplicate keys, -0, any fraction or exponent) before a command reaches SQL.
"""

import hashlib
import json
import re
from typing import Any, Final, NoReturn

__all__ = [
    "MAX_SAFE_INTEGER",
    "RawInputRejected",
    "canon_bytes",
    "load_command",
    "m11_dedupe_key",
]

MAX_SAFE_INTEGER: Final = 2**53 - 1
_REASON: Final = "COMMAND_MALFORMED"
_TENANT_ID: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
_SHORT_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class RawInputRejected(ValueError):
    """A value or raw command outside the m02.canon/v1 domain.

    Carries only a controlled reason code; the offending value is never echoed.
    """

    def __init__(self, reason_code: str = _REASON) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _reject() -> NoReturn:
    raise RawInputRejected()


def _string(text: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        if code == 0 or 0xD800 <= code <= 0xDFFF:
            _reject()
        escaped = _SHORT_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _utf16_order(key: str) -> bytes:
    return key.encode("utf-16-be", errors="surrogatepass")


def _value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            _reject()
        return str(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            _reject()
        keys = sorted(value, key=_utf16_order)
        return "{" + ",".join(_string(key) + ":" + _value(value[key]) for key in keys) + "}"
    _reject()


def canon_bytes(value: Any) -> bytes:
    """The m02.canon/v1 bytes of ``value``; raises ``RawInputRejected`` outside the domain."""

    return _value(value).encode("utf-8")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            _reject()
        result[key] = item
    return result


def _integer(text: str) -> int:
    if text.startswith("-") and text.lstrip("-") == "0":
        _reject()
    return int(text)


def _no_float(text: str) -> NoReturn:
    _reject()


def load_command(text: str) -> Any:
    """Parse a raw JSON command, refusing what JSONB conversion would lose.

    Duplicate keys, negative zero, any fraction or exponent, and NaN/Infinity are
    refused; the parsed value must then canonicalize (range, U+0000, surrogates).
    """

    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_float=_no_float,
            parse_constant=_no_float,
        )
    except RawInputRejected:
        raise RawInputRejected() from None
    except (ValueError, RecursionError):
        raise RawInputRejected() from None
    canon_bytes(value)
    return value


def m11_dedupe_key(
    tenant_id: str, source_event_id: str, handoff_kind: str, signal_type: str
) -> str:
    """Kind-inclusive M11 handoff key (delta v1r1 Delta1-S, design P584, owner O8)."""

    if not isinstance(tenant_id, str) or not _TENANT_ID.fullmatch(tenant_id):
        _reject()
    preimage = ["m02.handoff/v1", "M11", tenant_id, source_event_id, handoff_kind, signal_type]
    return "m02h1:" + hashlib.sha256(canon_bytes(preimage)).hexdigest()
