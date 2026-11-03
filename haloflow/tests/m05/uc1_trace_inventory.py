"""The independent UC1 required-id inventory for the closeout trace (B5 v2 section 7,
v2r1; Codex 6abf4d64 point 4).

This is a literal list, written here and never derived from the test files or from
scripts/uc1_trace.py. The trace maps every id below to collected node ids, or
declares it a pending hold. The trace may not mark a HOLD as covered.
"""

from __future__ import annotations

from typing import Final


def _range(prefix: str, low: int, high: int) -> tuple[str, ...]:
    return tuple(f"{prefix}-{n:02d}" for n in range(low, high + 1))


REQUIREMENTS: Final = tuple(f"UC1-R{n}" for n in range(1, 12)) + ("UC1-T1", "UC1-T3", "UC1-T4")

AGREED_TESTS: Final = (
    _range("PA", 1, 32)
    + _range("MC", 1, 8)
    + _range("MA", 1, 16)
    + _range("MR", 1, 14)
    + _range("MN", 1, 9)
    + _range("ME", 1, 5)
    + ("MX-01", "MX-02")
)

B5_TESTS: Final = (
    _range("B5-C", 1, 10)
    + _range("B5-I", 1, 8)
    + _range("B5-K", 1, 4)
    + _range("B5-V", 1, 9)
    + _range("B5-S", 1, 5)
    + ("B5-S15",)
    + _range("B5-A", 1, 4)
    + _range("B5-E", 1, 7)
    + ("COMPAT-B5a", "COMPAT-B5b", "P4-1", "P4-2", "P4-3",
       "MR-05a", "MR-05b", "MR-05c", "MR-06a", "MR-06b", "MR-07a", "MR-07b", "MR-RC")
)

# Release holds. They are always pending_hold with evidence_level "none".
HOLDS: Final = (
    "L-1W", "L-1S", "OD-D2", "L-6", "A-1-HUMAN-DEMO", "LIVE-SMS", "REAL-EHR-WRITE",
)

EVIDENCE_LEVELS: Final = frozenset(
    {"unit", "integration_pg17", "process_restart", "simulated", "none"}
)
STATUSES: Final = frozenset({"covered", "pending_hold", "out_of_scope"})

REQUIRED_HEADER: Final = (
    "B5 green does not complete the UC1 milestone; the pending_hold rows remain open."
)
