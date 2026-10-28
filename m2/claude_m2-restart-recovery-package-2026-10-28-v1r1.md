# M-2 restart write-back recovery — package v1r1 (architecture revision)

Claude, 2026-10-28. This revises v1 (0f819557…) after Codex's review codex_m2-v1-review.md. The requirements and the synthetic-only scope are aligned and unchanged, so only the deltas are listed.

## Δ1. F-M2-1: async integration, no sync bypass

**Problem, which Codex states correctly:** `tasks._scenario_unavailable` calls `knows()` synchronously, so an async DB-backed `knows` would return a truthy coroutine and bypass the guard. Also, `_ehr_for` is synchronous, and the synthetic `_source` path bypasses the per-date lock.

**Revision:**
- **`knows()` stays synchronous and memory-only.** Its meaning is unchanged: "this router can serve this exact ID now". `tasks.py` is unchanged and is still the only admission and CAS path.
- **Async pre-admission preparation in the service.** All runs go through `DbStaffService.run_writeback` and `reconcile_writeback` (service.py l.491–495; web routes call these). Each first awaits `self._prepare_task(task_id)`, synthetic mode only:
  1. Load the task's reminder row in one read: `writeback_tasks t JOIN reminder_records r ON r.id = t.reminder_id WHERE t.id = :t`.
  2. If `router.knows(r.emr_appt_id)` is true, do nothing (a current-process ID).
  3. Otherwise resolve the scoped identity (Δ2). If exactly one durable slot row exists, register an **alias** in the router: `old_id → DurableSlot(store, slot)`. That handle serves async `read_confirmation` and `write_confirmation_conditional` from the store.
  4. If resolution fails, register nothing. `knows(old_id)` stays False and the existing `_scenario_unavailable` staff-item path runs: no admission, read or write.
- **The order is safe.** `_prepare_task` only adds an alias after a successful read; it never writes. Admission remains the tasks.py CAS. A second concurrent preparation for the same ID is idempotent (dict assignment of an equal handle).
- **Direct callers of `tasks.run_task` with the router bypass preparation.** That is fail-closed: an unknown ID is still a staff item. They are tests only.
- **Seed and publication are serialised.** The synthetic branch of `_source(target)` now takes the existing per-date lock (as athena mode does). Under that lock it: builds the fixtures, `await store.seed(...)`, constructs `SyntheticEhr(fixtures, store=…)`, registers it in the router, and only then publishes it to `self._ehr[target]`.
  - `_ehr_for` becomes `async def _synthetic_for(target)`, called only under the lock. Its sole caller is `_source` (service.py l.181), so there are no other sync callers.
  - A concurrent first load waits on the lock and reuses the published instance.

## Δ2. F-M2-2: scoped, exact old-ID resolution

`slot_for_task(task_id) → Slot | None` (sim_store.py) returns a slot only if all of these hold:
1. The task row exists and joins exactly one `reminder_records` row (FK and unique `reminder_id`; checked, not assumed).
2. `r.tenant_id == TENANT_ID`, the active service tenant.
3. `r.emr_appt_id` fullmatches `syn-e\d+-\d{8}-a-\d+`. That is synthetic source only; `ath-…` and anything else is refused.
4. Exactly one `sim_confirmations` row exists for (`source='synthetic'`, `tenant_id`, `r.emr_patient_id`, `r.appt_start`). It is counted with `SELECT count(*)`, and a count other than 1 is refused.

Otherwise it returns None, and the task becomes a staff item. Nothing is guessed from similar slots, parsed from the ID, or taken from the current target date or fixtures. Athena mode never calls this function.

The router's alias handle also carries the slot's source and tenant. A read or CAS whose slot source is not 'synthetic', or whose tenant is not the active one, raises `ScenarioUnavailable` (defence in depth).

## Δ3. Test revisions and additions

| ID | Change |
|---|---|
| RR-4 | **Explicit crash boundary.** The CAS is committed in the store (version 1 → 2) and the task is left IN_FLIGHT. This is done by making `tasks._finish` raise a test-only `Crash` after `attempt_writeback` returns DONE; the store transaction has already committed and the task has not. Then restart → UNCERTAIN(restart) → reconcile → DONE(reconciled). The assertions are on the **durable** store version (== 2) and value, not on per-instance counters. |
| RR-1/2/5/9 | The "exactly one write" checks now use the durable version (== 2 after one write, unchanged after repeated restarts). Instance counters are kept only as a secondary check within one process. |
| RR-10 | **Genuine v3 schema.** A fresh DB is built with exactly the v3 table set: every current table except `sim_confirmations`, created from current metadata minus that table, plus `demo_meta schema_version='3'`. Opening it with the new code → `demo_schema_mismatch`; afterwards the table list equals the v3 set (no `sim_confirmations`), and the `demo_meta` rows are unchanged. |
| RR-16 (new) | Scoped resolution refusals. Each gives a scenario_unavailable staff item, zero store reads or writes and no admission: a task whose reminder `tenant_id` differs; an `ath-…` reminder in synthetic mode; an ID not matching the synthetic pattern; a slot row with a different source or tenant; no slot row. |
| RR-17 (new) | Router handle defence: a handle with source 'athena' or another tenant raises ScenarioUnavailable on read and CAS. |
| RR-18 (new) | Concurrent first load of one date (gathered page_data ×2) → one seed, one published instance, store rows equal to the fixtures. |
| RR-19 (new) | Bypass is fail-closed: calling `tasks.run_task(own, svc.ehr, tid)` directly for an old-epoch task (no preparation) → scenario_unavailable; no write. |

The other RR cases and the disclosed replacements are unchanged from v1. Out of scope: unchanged.
