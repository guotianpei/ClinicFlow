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

# W2 (plan v1 §3, Codex ALIGN 20261102T185918Z). SQL-tier evidence only: it does not
# imply HTTP acceptance, which stays with the deferred W4 rows below.
W2_IDS: Final = (
    "B-01", "B-02", "B-03", "B-04", "B-07c", "B-07d", "B-08", "S-01",
    "S-02", "S-03", "S-04", "S-05", "S-06", "S-07", "S-08", "S-10",
    "S-12", "S-13", "S-14", "T-1", "T-2", "T-3", "T-4", "T-5",
    "T-6", "T-7", "T-8", "A-01", "A-02", "A-03", "A-04", "A-05",
    "A-07", "SP-01", "SP-02", "SP-03", "SP-04", "SP-05", "SP-06", "SP-07a",
    "SP-07d", "SP-08", "LG-01", "LG-02", "LG-03", "LG-04", "N-01", "X-01",
    "PRIV-01", "PRIV-02", "FX-01", "FX-02",
    "PF-01", "PF-02", "PF-03", "PF-04", "PF-05",
)

# Deferred rows, kept explicit so SQL-tier evidence cannot stand in for them
# (Codex W2 plan disposition). id -> tranche.
DEFERRED: Final = {
    "B-05": "W3", "A-06": "W3", "SP-07b": "W3", "SP-07c": "W3",
    "C-01": "W3", "C-02": "W3", "C-03": "W3", "C-04": "W3", "C-05": "W3",
    "R-01": "W3", "R-02": "W3", "R-03": "W3",
    "L-01": "W3", "L-02": "W3", "L-03": "W3", "L-04": "W3", "L-05": "W3",
    "M05-ROLES-SELECT": "W3",  # Codex final disposition pin 2
    "ADMIT-COMMIT-FAILURE": "W4",  # Codex final disposition pin 3
    "S-04-HTTP": "W4", "S-08-HTTP": "W4",
    "E-L1": "W4", "E-L2": "W4", "E-L3": "W4",
    "H-01": "W4", "H-02": "W4", "H-03": "W4", "H-04": "W4", "H-05": "W4", "H-06": "W4",
    "H-07": "W4",
    "G-01": "W4", "G-02": "W4", "G-03": "W4", "G-04": "W4", "G-05": "W4",
    "S-11": "later",  # pruning deferred (v1r1 §5)
}

# Replaced or superseded package rows: id -> the rows that now carry it.
RETIRED: Final = {
    "S-09": ("T-3", "T-4"),
    "B-07a": ("T-5",),
    "B-07b": ("T-6",),
    "B-06": ("SP-03", "SP-04"),
}
