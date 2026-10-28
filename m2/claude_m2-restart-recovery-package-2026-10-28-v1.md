# M-2 restart write-back recovery — package v1 (requirements, architecture, tests)

Claude, 2026-10-28, for Codex review before any test. Base: main 425146d. Direction: codex_restart-recovery-direction.md.

**Scope:** synthetic mode only. Sandbox (athena_preview) write-back behaviour is unchanged by this package: still simulated, in memory, and `scenario_unavailable` after a restart. Its durable store is a possible later package.

This is simulated recovery evidence only. It proves nothing about a real EHR's write or conflict semantics (F-E1 is still open).

## 1. Today (LIMITED, register M-2)

- The simulated confirmation value and version live in `SyntheticEhr._conf`, in process memory.
- Appointment IDs are qualified by owner epoch and date (`syn-e<epoch>-<yyyymmdd>-a-N`). After a restart the fixtures are regenerated, and `_EhrRouter.knows(old_id)` is False.
- So a task left PENDING, NOT_APPLIED or UNCERTAIN, or a reply processed after a restart, becomes a `writeback_scenario_unavailable` staff item, and nothing is read or written.
- Restart already turns IN_FLIGHT tasks into UNCERTAIN(restart) (ownership.py).

## 2. Requirements

- **R-M2-1, durable simulated state.** The synthetic confirmation value and version persist in the demo database, keyed by a stable slot identity: (`source`='synthetic', `tenant_id`, `emr_patient_id`, `appt_start`). The key never uses the epoch-qualified ID. It is independent of the current target date and of regenerated fixtures.
- **R-M2-2, seed once.** When a date's fixtures load, a slot row is inserted only if absent (`ON CONFLICT DO NOTHING`), with the fixture's initial value and version 1. Loading fixtures never resets or overwrites an existing row.
- **R-M2-3, atomic CAS.** A conditional write is one fenced statement: `UPDATE … SET value=:target, version=version+1 WHERE <slot> AND version=:expected RETURNING value, version`. A read is a fenced SELECT. `clinic_change` (the test hook) increments the version atomically.
- **R-M2-4, old-ID lookup.** A task or reply for any appointment ID resolves to its slot through its own `reminder_records` row (`emr_patient_id`, `appt_start`, `tenant_id`), never by parsing the ID or matching similar slots.
  - If the reminder row is missing, or no store row exists for that slot, the result is a `writeback_scenario_unavailable` staff item. Nothing is read or written. The item text stays "This confirmation belongs to an earlier demo run. Nothing was written. Update the EHR manually."
  - Ambiguity is impossible by key: the slot key is unique.
- **R-M2-5, recovery behaviour, all under the existing write-back rules** (no blind retry; conflict produces a staff item):
  - A PENDING or NOT_APPLIED task after a restart runs through the conditional write. The expected result is DONE, with exactly one applied write.
  - UNCERTAIN(restart) is reconcile-only:
    - store value == target → DONE(reconciled), with `caused_by_us` false;
    - store value == observed_before → stays UNCERTAIN (and "Run" is not offered, as today);
    - anything else → CONFLICT plus a staff item.
  - A pending reply (inbound RECEIVED, not yet processed) after a restart is processed as today, and its task completes as above.
  - The date-rollover behaviour (the original appointment is targeted) is preserved.
- **R-M2-6, crash points.** Recovery must be correct for each crash point, with no duplicate state transition across repeated restarts:
  1. Before the write (task IN_FLIGHT, store unchanged) → UNCERTAIN(restart) → reconcile → UNCERTAIN → staff decides. No automatic write.
  2. After the committed simulated write, before task completion → UNCERTAIN(restart) → reconcile → DONE(reconciled).
  3. After reconciliation → DONE is terminal, and a further restart changes nothing.
- **R-M2-7, schema.** `SCHEMA_VERSION` goes 3 → 4 with a new table `sim_confirmations`.
  - An existing v3 demo DB is refused with `demo_schema_mismatch` before any effect. No table is left behind: create_all runs in the same transaction, which rolls back on refusal.
  - No migration, reset, deletion or automatic re-creation.
  - Operator next step: create a fresh `*_demo` database.
  - The Thursday DB `haloflow_thu1029_demo` must never be opened with the new code; it stays on pinned e1da39c. This goes into the runbook note and the PR text.
- **R-M2-8, reset.** `reset-synthetic` also truncates `sim_confirmations`, because it is synthetic demo state. Quota, opt-outs, contact history and meta are preserved, as today.
- **R-M2-9, unchanged:**
  - the sandbox source and PR #47 provenance guard;
  - sender controls, the daily cap and pause;
  - F-R1-1 duplicate-send prevention;
  - no-response semantics;
  - the "(before restart)" labels.
  The only change to the staff page is that a recovered task shows DONE.

## 3. Architecture

- **`models.py`:**
  - `SimConfirmation`: `source` String(16), `tenant_id` String(64), `emr_patient_id` String(128), `appt_start` timestamptz, `value` String(32) nullable, `version` Integer NOT NULL CHECK ≥ 1, `updated_at`.
  - PRIMARY KEY (`source`, `tenant_id`, `emr_patient_id`, `appt_start`).
  - `SCHEMA_VERSION = 4`. Add the table to `SYNTHETIC_TABLES`.
- **`sim_store.py` (new)**, an async store over `Ownership`:
  - `seed(fixtures, tenant)`: insert if absent;
  - `read(slot) → Observation | None`;
  - `cas(slot, target, expected) → WriteResult`, one fenced UPDATE…RETURNING; on a miss it re-reads;
  - `bump(slot, value)` for the clinic_change hook;
  - `slot_for_reminder(appt_id) → Slot | None`, via `reminder_records`.
- **`synthetic_ehr.SyntheticEhr` gains an optional `store`** plus a `tenant`.
  - With a store, `read_confirmation` and `write_confirmation_conditional` go to the store, using the appointment's slot from its own fixtures.
  - The failure injections are kept for tests: `connect` raises before the CAS; `lost_response` commits the CAS, then raises `EhrResponseLost`.
  - The counters `write_attempts`, `applied_writes` and `lost_updates` stay per instance.
  - Without a store (unit tests), behaviour is unchanged.
- **`service._EhrRouter`:**
  - A known ID routes as today.
  - An unknown ID in synthetic mode resolves `slot_for_reminder(id)`. If a store row exists, the router serves read and CAS from the store directly; otherwise it raises `ScenarioUnavailable`, as today.
  - `knows(id)` becomes "known here, or resolvable to an existing store slot", so `tasks._scenario_unavailable` needs no change.
  - Athena mode keeps the current router path unchanged (no store).
- **`DbStaffService._ehr_for`** creates `SyntheticEhr(fixtures, store=…)` and awaits `store.seed`, under the existing per-date lock.
- **Unchanged:** `tasks.py` admission CAS, `writeback.py` decision rules, `inbound.py`, `ownership.py` restart recovery.

## 4. Unit tests (tests-first; new `tests/demo_reminders/test_restart_recovery.py`, real PG, stub gateway)

| ID | Case | Expected |
|---|---|---|
| RR-1 | A confirmed reply creates a task; restart (epoch 2) before running it; run the task in epoch 2 | DONE; store value is the target and version 2; exactly one applied write; one staff-free path |
| RR-2 | A reply received but not processed before restart; process in epoch 2 | Task is created and DONE; one write |
| RR-3 | Crash point 1: IN_FLIGHT, store unchanged; restart | UNCERTAIN(restart); reconcile → UNCERTAIN; Run not offered; no write; store unchanged |
| RR-4 | Crash point 2: committed CAS (lost_response hook), task IN_FLIGHT; restart | UNCERTAIN(restart); reconcile → DONE(reconciled); applied_writes total 1 |
| RR-5 | Crash point 3: after RR-4, restart again and reconcile or run attempts | No state change; no write; DONE stays |
| RR-6 | The clinic changes the value between reply and recovery (bump) | CONFLICT plus a writeback_conflict staff item; no write |
| RR-7 | Missing identity: the store row is absent for the slot (a reset-synthetic that kept the reminder is impossible, so this is simulated by deleting the store row in the test) | scenario_unavailable staff item; no read or write |
| RR-8 | Seed once: load the same date in epoch 1, write; restart; load the same date in epoch 2 | Store row not reset (value stays the target) |
| RR-9 | Double restart with a PENDING task, then run | exactly one write; version 2 |
| RR-10 | Old-schema refusal: a v3 demo DB (created by pinning SCHEMA_VERSION=3 tables) is opened by the new code | `demo_schema_mismatch`; `sim_confirmations` absent afterwards; no other change |
| RR-11 | reset-synthetic | sim_confirmations emptied; quota, opt-outs and contact history kept |
| RR-12 | Sandbox mode, restart with a sandbox task | unchanged: scenario_unavailable (no store use) |
| RR-13 | Date rollover (existing case) with a store | the original appointment is written; the new date's slot is untouched |
| RR-14 | Concurrency: two CAS calls with the same expected version | exactly one applied, the other returns applied=False |
| RR-15 | Tenant/source separation: same patient and start under another tenant or source | distinct rows; no cross-read |

Existing tests whose LIMITED expectation changes (disclosed, narrow):
- `test_inbound.py::test_writeback_after_restart_fails_closed` (≈ l.1799–1829): the synthetic restart now gives DONE instead of `scenario_unavailable`. It becomes RR-1 style, keeping the "(before restart)" label and the regenerated-ID assertions.
- `test_athena_source.py::test_a9_restart_prior_tasks_fail_closed_and_no_resend`: unchanged, because sandbox mode is out of scope (RR-12).
- `test_staff_feedback.py` code-map rows for `scenario_unavailable`: unchanged (the code still exists).
- `test_db.py` `EXPECTED_TABLES` (l.44, `set(DEMO_TABLES) == EXPECTED_TABLES`) gains `sim_confirmations`. No test asserts SCHEMA_VERSION == 3 literally (grep at 425146d). The final list will be in the tests packet.

## 5. Out of scope

- Sandbox durable store.
- Real EHR write.
- Restart of in-flight SMS sends (unchanged: UNCERTAIN, never resent).
- Multi-department.
- A migration tool.
