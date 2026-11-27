"""D1 appointment-verification evaluator: offline, deterministic, model-independent.

Requirements v1.4, architecture v2 and test packet v2r1 (frozen 2026-11-26); call surface
per contract pins v1 and the red-packet addendum. Pure: no database, network, model,
environment, filesystem or ambient clock (enforced by the I1 repository control).
"""

from __future__ import annotations

from .bundle import BundleRejected, validate_bundle
from .canonical import CanonicalError, canonical_bytes, digest, parse_json
from .evaluator import EVALUATOR_VERSION, evaluate
from .expressions import LIMITS, UNKNOWN, eval_expr
from .replay import ReplayError, replay
from .zones import ZoneData

__all__ = [
    "EVALUATOR_VERSION",
    "LIMITS",
    "UNKNOWN",
    "BundleRejected",
    "CanonicalError",
    "ReplayError",
    "ZoneData",
    "canonical_bytes",
    "digest",
    "eval_expr",
    "evaluate",
    "parse_json",
    "replay",
    "validate_bundle",
]
