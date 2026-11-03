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
    "SET-01", "SET-02", "SET-03",
)

# W3a (contract v1; Codex 213713Z/214312Z/214930Z/215519Z). v1 m01_staff_admin and
# m05 prepare/apply stay runtime-callable until W3b; W3a is not closure evidence.
W3A_IDS: Final = (
    "COMP-W3A", "C-03", "C-04", "C-05", "R-02", "R-03", "L-01", "L-02",
    "L-03", "L-04", "L-05", "L-06", "BS-01", "BS-02", "BS-03", "BS-04",
    "BS-05", "BS-06", "BS-07", "BS-08", "B-05", "A-06", "SP-07b", "SP-07c",
    "PIN2-01", "PRIV-W3-01",
    # Implementation-time rows (Codex 224307Z support profile (d); 222540Z service).
    "SUP-01", "SUP-02", "SUP-03", "SUP-04", "SVC-01", "GB-01", "L-07",
    "SUP-05", "SUP-06", "SUP-07", "SUP-08",  # Codex 225500Z stage-1 profile check
)

# W3b (plan v1r1 + addendum 1; Codex 150630Z/151751Z; owner decision OD-FM1): the
# first-member bootstrap (t033/t034), the acl_closure_v1 kind and the closure of the
# v1 runtime paths (t035/t036). HARNESS-MIGRATION stays deferred to W3b
# implementation (existing-test changes are enumerated there).
W3B_IDS: Final = (
    "FM-01", "FM-02", "FM-03", "FM-04", "FM-04b", "FM-05", "FM-05b", "FM-06",
    "FM-06b", "FM-06c", "FM-06d", "FM-07", "FM-07b", "FM-08", "FM-09", "FM-10",
    "ST-01", "ST-02", "ST-03",
    "CLOSURE-UNITS", "AM-01", "AM-02", "AM-03", "AM-04", "AM-05", "AM-06", "AM-07",
    "AM-08", "AM-09", "AM-10", "AM-11", "AM-12", "AM-13",
    "CL-01", "CL-02", "CL-03", "CL-04", "C-01", "C-02", "CG-REV-01",
    "RI-01", "RI-02", "RI-03", "RI-04", "RI-05",
)

# Deferred rows, kept explicit so SQL-tier evidence cannot stand in for them
# (Codex W2 plan disposition). id -> tranche.
DEFERRED: Final = {
    # W3b implementation: existing-test changes for the closed v1 paths (Q-B3 ruling).
    "HARNESS-MIGRATION": "W3b",
    "R-01": "W4",  # the capability matrix literal belongs to the adapter
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
    "M05-ROLES-SELECT": ("PIN2-01",),
    "B-07a": ("T-5",),
    "B-07b": ("T-6",),
    "B-06": ("SP-03", "SP-04"),
}
