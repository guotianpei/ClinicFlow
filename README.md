# Evidence for PR #38 (pglast 7.21, D-1a) — NEVER MERGE

Captured by Claude in the cloud container on 2026-10-26 (PostgreSQL 16.15 is installed there but not used by these files; Python 3.12). It is evidence only: no code is on this branch.

- `10_tests_prechange.txt`: the frozen tests at 24800a0, run against main's source with pglast 7.21 installed.
- `pglast_transform.py`: produces `20_transform-report.json`. It changes the 84 declarations and recomputes every derived literal to a fixpoint.
- `pglast_proof.py`: produces `22_semantic_diff_proof.json`. Its result: 0 violations; the only changes are the 84 version leaves, 86 hash leaves and 48 Python hash lines; the checksum vectors are untouched; 111 values were re-derived independently.
- `21_m01_unit_after_transform.txt`, `23_lint_type_unit.txt`, `24_verified_install_linux_cold.txt`: post-change runs.

`SHA256SUMS` covers every file.
