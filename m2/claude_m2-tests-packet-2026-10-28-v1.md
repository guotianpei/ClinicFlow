# M-2 restart recovery — tests packet v1 (for FREEZE)

Branch feat/m2-restart-recovery, commit 9a0b9085e89d2ac4bbf90a2e02ca11308a33d315. Base: main e1bea32. Source is unchanged.
Design: package v1 + v1r1. Codex ALIGN: codex_m2-v1r1-review.md, including the known-ID task-isolation consolidation.

## Task isolation (Codex consolidation), carried into the tests
Chosen solution: a **task-scoped EHR view per run**. For every `run_writeback` and `reconcile_writeback`, synthetic mode, the service:
1. Loads the task's reminder.
2. Validates it before any admission:
   - the reminder tenant is the active tenant;
   - the id fullmatches the synthetic format;
   - the slot is bound. A current-process id must have the same patient and start as the reminder. An old id needs exactly one durable slot row.
3. Passes `tasks.run_task` / `reconcile_task` a view that `knows` and serves **only** that validated id. It serves the current instance or a durable-slot handle. The view is per call, so there is no shared alias to invalidate.
4. If validation fails, the view knows nothing, and the existing `_scenario_unavailable` path runs: a staff item, with no admission, EHR read or write.
`tasks.py` is unchanged. Athena mode is unchanged (router as today).

## Files (sha256)
- tests/demo_reminders/test_restart_recovery.py (new, 24 cases): 949a3bc4b5189a81346221c785143573fb61616fdacbb9a453981f6ae5c47b72
- tests/demo_reminders/test_inbound.py (one test replaced): e56aaa5efd02c41e67b3add56748d196792c87a3b53c6318195a9e8e0023b0f7
- tests/demo_reminders/test_db.py (EXPECTED_TABLES + sim_confirmations): 6adb0b50bfbde41ba7042bd7fba7e577f5eecfd08311902b4abeee0639274e9e
- Replacement diff: replacements.diff, 69283eb9…b3b

## Coverage

All "one write" checks use the **durable** `sim_confirmations` value and version.

**Recovery and crash points**
- RR-1: PENDING after restart → DONE; durable ("2", 2); the regenerated fixture has write_attempts 0; "(before restart)" label kept.
- RR-2: pending reply processed after restart → DONE.
- RR-3: crash point 1 (IN_FLIGHT before write) → UNCERTAIN(restart); reconcile → UNCERTAIN; Run raises; store unchanged.
- RR-4: crash point 2, explicit. `tasks._finish` raises Crash after the committed CAS; store ("2", 2), task IN_FLIGHT. Restart → UNCERTAIN(restart); reconcile → DONE(reconciled); store still ("2", 2).
- RR-5: crash point 3, then two more restarts. Run and reconcile raise; nothing changes.
- RR-6: clinic change → writeback_conflict and a staff item; store ("3", 2).
- RR-7: store row missing → scenario_unavailable; task (PENDING, None, 0); staff item.
- RR-8: seed once; 7 rows.
- RR-9: double restart, then exactly one write.

**Schema and reset**
- RR-10: genuine v3 table set (current metadata minus sim_confirmations, schema_version '3') → demo_schema_mismatch. Tables and meta are unchanged; no sim_confirmations.
- RR-11: reset-synthetic empties the store; quota and contacts kept.

**Identity and isolation**
- RR-12: covered by the existing test_athena_source a9 (sandbox unchanged).
- RR-13: date rollover writes only the original slot.
- RR-14: concurrent CAS, exactly one applied.
- RR-15: tenant and source separation in the store.
- RR-16a (×3), known current id: another tenant with the same slot; another tenant with another slot; own tenant, current id of a different slot. Each refused with no admission, and the legit task still DONE.
- RR-16b, prepared old id: cross-tenant forged task with the same old id, run both before and after the legit task's resolution. Refused both times; legit DONE.
- RR-16c (×3): `ath-…` id, unqualified id, bad-format id → refused; store untouched.
- RR-17: SimStore refuses a foreign source or tenant slot (SlotRefused) on read and cas.
- RR-18: concurrent first load → one instance, 7 rows, all ("1", 1).
- RR-19: direct `tasks.run_task(own, svc.ehr, old_tid)` bypass → scenario_unavailable; no admission; store unchanged.

## Interface pinned
- `haloflow_demo.sim_store`: `Slot(source, tenant_id, emr_patient_id, appt_start)`, `SimStore(own, tenant)` with `.read` / `.cas`, and `SlotRefused`.
- Table `sim_confirmations`, with columns source, tenant_id, emr_patient_id, appt_start, value, version.
- `reset_synthetic` clears it. `DemoBase.metadata` includes it.

## Note on the uniqueness constraint
`uq_reminder_appt` is unique per (tenant_id, emr_appt_id), so a same-tenant collision on the same id cannot exist. The RR-16 cases cover every collision the schema allows.

## Baselines (PG16, source = main e1bea32): baseline-red.txt
- New file: collection error (`haloflow_demo.sim_store` missing), as expected.
- Changed files: exactly 2 failed (test_db t13 and the replaced restart test); 115 passed.
- Full suite excluding the new file: 2 failed, 689 passed (= 691 existing).
- ruff is clean.
