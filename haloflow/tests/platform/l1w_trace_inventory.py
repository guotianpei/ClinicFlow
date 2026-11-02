"""The independent L-1W required-id inventory (package v1r1 Q-1: a separate inventory,
wired into the CI trace step; UC1 holds unchanged).

A literal list, never derived from the test files or from scripts/l1w_trace.py. W1 adds
the verifier (V) and key-set (K) rows; W2-W4 extend it.
"""

from __future__ import annotations

from typing import Final

W1_IDS: Final = (
    "V-01", "V-02", "V-03", "V-04", "V-05", "V-06",
    "K-00", "K-01", "K-02", "K-03", "K-04", "K-05",
)
