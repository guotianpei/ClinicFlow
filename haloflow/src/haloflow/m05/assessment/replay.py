"""Side-effect-free replay of a recorded assessment (architecture v2 "Replay record")."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import CanonicalError, digest
from .evaluator import Json, evaluate
from .zones import ZoneData


class ReplayError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def replay(record: Mapping[str, Any], *, zones: ZoneData) -> Json:
    """Re-evaluate a result's `replay_record` with its original clock and snapshots.

    Missing content, a digest that does not match the supplied bundle, or zone data that
    differs from the recorded release and digest are errors raised before any decision.
    """
    request = record.get("request") if isinstance(record, Mapping) else None
    if not isinstance(request, Mapping):
        raise ReplayError("missing_content")
    snapshots = request.get("snapshots")
    if not isinstance(snapshots, Mapping) or any(
            name not in snapshots for name in ("policy", "routes", "permissions", "reference")):
        raise ReplayError("missing_content")
    if "evaluation_instant" not in request or "context" not in request:
        raise ReplayError("missing_content")
    recorded_tz = record.get("tzdata")
    if not isinstance(recorded_tz, Mapping):
        raise ReplayError("missing_content")
    if recorded_tz.get("release") != zones.release or recorded_tz.get("digest") != zones.digest:
        raise ReplayError("tzdata_mismatch")
    try:
        if digest(request) != record.get("input_digest") or \
                digest(snapshots["policy"]) != record.get("bundle_digest"):
            raise ReplayError("digest_mismatch")
    except CanonicalError as exc:
        raise ReplayError("digest_mismatch") from exc
    return evaluate(request, zones=zones)
