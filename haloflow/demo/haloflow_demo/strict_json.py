"""Strict JSON decoding shared by the send-response and inbound-event decoders.

Duplicate keys, and keys equal case-insensitively, are rejected: last key never
wins. Lookups are case-insensitive over the documented names only.
"""

from __future__ import annotations

import json
from typing import Any


class StrictJsonError(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for k, v in pairs:
        folded = k.casefold()
        if folded in seen:
            raise StrictJsonError(f"duplicate or conflicting key {k!r}")
        seen.add(folded)
        out[k] = v
    return out


def loads_strict(raw: bytes | str) -> Any:
    """Raises :class:`StrictJsonError` for duplicates, ``ValueError`` for bad JSON."""
    return json.loads(raw, object_pairs_hook=_object)


def ci_get(obj: dict[str, Any], name: str) -> Any:
    """Case-insensitive get. Collisions were already rejected by :func:`loads_strict`."""
    folded = name.casefold()
    for k, v in obj.items():
        if k.casefold() == folded:
            return v
    return None
