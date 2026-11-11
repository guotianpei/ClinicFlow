# Option B B1 — red-candidate evidence (Claude, 2026-11-11)

Branch `feat/option-b-b1-red`. Candidate head **fdf00a8213192cc6ae24490f564d08c8abadbcb2**
(fixes c04be9e04…; c04be9e35f08a19d38d1fb81ddfe1bca50023555). Base main
bc28c90c239c9a22e5c8ad54d808eee23ba88d86. Cloud container, synthetic data only; see
versions.txt for Python, PostgreSQL and pytest versions and the invocations.

## Runs (each run alone, sequential, same environment and same database)

| Log | Tree | Result | Exit |
|---|---|---|---|
| main-baseline.log | main bc28c90 (worktree) | 5682 passed | 0 |
| candidate-rerun.log (+ junit-rerun.xml) | c04be9e, alone | 30 failed / 5692 passed | 1 |
| candidate-fix.log (+ junit-fix.xml) | **fdf00a8**, alone | **10 failed / 5712 passed** | 1 |
| pytest.log (+ junit.xml) | c04be9e, first run (overlapped with my isolation runs) | 30 failed / 5692 passed | 1 |

`pytest.log` is kept for completeness, but it is not clean: I ran isolation checks against
the same database while it was running. `candidate-rerun.log` reproduces the same 30 with
nothing else running.

## Classification of fdf00a8 (junit-fix.xml)

10 failures, all intended red, each for the intended cause:

| Row | Cause |
|---|---|
| R1 × 2 (catalog, registry) | the current checker refuses the second root (implementation pending) |
| R3 × 2 | `src/haloflow/synthetic/composition.py` does not exist yet |
| E1, E2, E3, E4 | ModuleNotFoundError: haloflow.synthetic |
| C1b static | haloflow.synthetic does not exist yet |
| C1b runtime | ModuleNotFoundError: haloflow.synthetic (subprocess) |

There are 40 new rows; the 30 that pass are controls:
- E0;
- C1, C1r, C1f;
- C2;
- R2 × 22;
- C3 × 2 (SCHEMA_VERSION_UNSUPPORTED, tenant_index 0, PRODUCTION and NON_PRODUCTION);
- C3s.

The 5682 pre-existing rows all pass, which equals main.

## The c04be9e regression: root cause and fix

**Root cause.** `tests/conftest.py` runs `alembic command.upgrade` repeatedly. `alembic/env.py`
then calls `fileConfig` (disable_existing_loggers=True), which disables every logger that
already exists. On c04be9e, the B1-C3 spies imported `haloflow.m05.{due_scheduler,
reminder_scheduler, writeback_scheduler, outbound, simulated_ehr}` early in tests/m05. A later
migration therefore disabled their loggers, and 20 log-asserting rows failed:

- sb_cancellation c01/c03/c04;
- sb_runner e04;
- se u03/u03b/u09/u09b/u09d × 2;
- sh_h1 u06b;
- sh_h3 p10/u04/u11/u24;
- si uw06_08 × 5.

Importing `haloflow.startup` loads none of those five modules; it creates only the
haloflow, haloflow.m05 and haloflow.startup loggers.

**Fix (fdf00a8, test-only).** The spies wrap only classes whose modules are already loaded.
For the others, they assert that the refused startup newly loads none of them. The C3s
self-test uses stand-in modules: a counted construction is detected, and so is a newly
loaded module. No other suite was edited, and no positive log or privacy assertion was
touched.

## Disclosures

- B1-C3: production startup's version gate fires first, so the TEST-family and intake guards
  are not reached for a target-94 tenant.
- The 15 PytestWarnings in the candidate runs are "record_property is incompatible with
  junit_family xunit2" from `tests/m01/phase_signature`. They come from my local
  `--junitxml` flag with the default junit family; CI uses xunit1. main-baseline was run
  without `--junitxml` and shows none.
- This is local evidence, not CI. No PR exists yet, so no CI has run.
