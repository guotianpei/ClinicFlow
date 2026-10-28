# N-TZ Step 3 — convert to the EHR department's timezone (package v1r1)

Claude, 2026-10-28. This revises v1 (670095d4…) after Codex's review, codex_n-tz-step3-v1-review.md. Base: main 36c8c72.

It covers owner decisions OD-TZ1 ("Convert and send") and OD-TZ2 ("Keep text, no label").

Only the changes from v1 are listed here. Everything else in v1 stands.

## Δ1. Correction: the v1 claim "stored UTC, so dedupe unaffected" is withdrawn

Codex is right that the v1 claim is false.

A reminder claimed before conversion stored the slot at the wrong instant. For example, Denver 09:00 read as New York was stored as 13:00Z; after conversion the same appointment is 15:00Z.

Two things change across that upgrade:
- The (tenant, patient, start) slot key, which the F-R1-1 slot dedupe uses.
- The epoch-qualified appointment id (`ath-e{epoch}-…`), which changes on every restart.

So a converted process started on a database that holds pre-conversion sandbox reminders could send a second reminder for the same appointment.

## Δ2. Legacy-database policy (R9, new) — refuse, never rewrite

The policy uses a marker row in `demo_meta`:
- Key: `athena_tz_interpretation`.
- Value: `department_tz_v1`.
- `demo_meta` is an existing key/value table, so there is no schema change and no `SCHEMA_VERSION` bump.

It runs in athena_preview mode only, once per process, on the first `_source()` call. That is before any athena request, and before any selection or send. It is one fenced transaction:
1. If the marker is present, proceed.
2. If the marker is absent and any `reminder_records` row has `emr_appt_id LIKE 'ath-%'`, refuse.
   - The refusal is sticky for the whole process: every date's source has error `LEGACY_ATHENA_REMINDERS`.
   - The page shows no appointments, so preview, batch and single sends do nothing.
   - No athena request is made.
3. If the marker is absent and there are no athena reminders, insert the marker (`ON CONFLICT DO NOTHING`) and proceed.

What the policy never does:
- It never deletes, resets or rewrites reminder rows or timestamps.
- It never writes the marker in synthetic mode.
- Synthetic reminders (`syn-…`) never trigger a refusal.

Banner (staff-facing only):
- "athenahealth sandbox unavailable — LEGACY_ATHENA_REMINDERS"
- followed by "This demo database holds sandbox reminders from before timezone conversion; use a fresh demo database."

Scope, stated explicitly:

| Case | Supported? |
|---|---|
| Fresh DB → converted | Yes |
| Converted → converted restart | Yes (marker present; slot dedupe works because the instants are now stable) |
| Pre-conversion → converted upgrade | No. Refused as above. |

Ownership is exclusive, so no pre-conversion process can write between the check and the marker insert.

## Δ3. Other local-date consumers (audit)

- `noresponse.py` and `inbound.py` compare UTC instants only (`appt_start > :t`, `c.appt_start <= received_at`). After conversion those instants are correct, so no change is needed.
- `writeback.py` and `tasks.py` have no local-date logic.
- `livetest.py` refuses athena mode already (`live_test_athena_mode`).
- Remaining local-date consumers, all listed in v1:
  - stay in clinic time: the target date, the quota day, inbound received times, and snapshot-taken;
  - use eff_tz: the date filter, selection, the staff "when" column, and the reminder text.

## Δ4. Tests added or changed (on top of v1 C-1..C-12)

| ID | Case | Expected |
|---|---|---|
| C-6 (exact) | now = 2026-10-27 04:30Z: New York 10-27 00:30, Denver 10-26 22:30. Department Denver, days_before 2 | target 10-29 (NY + 2); after one send, `send_quota.clinic_day` = 2026-10-27 (NY), not 10-26 |
| C-13 | legacy: a DB without the marker holding a reminder `ath-e1-1001`, patient athp-60178, appt_start 13:00Z (a pre-conversion Denver 09:00); start converted with dept Denver | source error LEGACY_ATHENA_REMINDERS; zero athena requests; `send_to_appointment` returns not_eligible; `send_batch` returns []; reminder_records count stays 1 with appt_start still 13:00Z; banner text; marker still absent |
| C-13b | the same legacy DB, synthetic mode | runs normally; marker not written |
| C-14 | fresh DB, converted: send for Denver 09:00 (stored 15:00Z); restart (epoch 2) | marker present; the epoch-2 send for the same slot is SKIPPED (slot dedupe at 15:00Z); one reminder row |
| C-15 | DB with only `syn-…` reminders, athena mode | marker written; proceeds (synthetic rows are not legacy athena rows) |
| C-11 | replaced by C-14 (which states the restart case precisely) | — |

Authorised by Codex (review v1): narrow replacement of the PR #46 mismatch and alias expectations with conversion assertions. All malformed-data, request-restriction, no-leak, recovery and quota assertions are preserved.
