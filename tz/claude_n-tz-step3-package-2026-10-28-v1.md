# N-TZ Step 3 — convert to the EHR department's timezone (package v1)

Claude, 2026-10-28. For Codex ALIGN before any test or code. Base: main 36c8c72 (PR #46 merged 19:24:14Z).

## 0. Owner decisions (Rachel, 2026-10-28 ~15:25 ET, via clickable options)

- **OD-TZ1 — "Convert and send."** If the EHR says the department is in a different timezone from the clinic's HaloFlow setting, HaloFlow trusts the EHR's timezone and sends reminders with times in the department's local time.
  - Claude had recommended "Hold and alert staff"; Rachel chose otherwise.
  - This replaces PR #46's fail-closed `SOURCE_TZ_MISMATCH` behaviour.
- **OD-TZ2 — "Keep text, no label."** The approved Q-B4 patient text is unchanged and carries no zone suffix. The time shown is the department's local time.

## 1. Requirements

- R1. Effective zone per snapshot:
  - `eff_tz = dept_tz` when `extract_tz` returns a recognized IANA name (VERIFIED, MISMATCH, or an alias with identical offsets);
  - otherwise `eff_tz = CLINIC_TZ` (UNDETERMINED: display name, missing or rejected value).
- R2. Booked records' `date` + `starttime` are read as local times in `eff_tz`. NONEXISTENT/AMBIGUOUS exclusions are computed in `eff_tz`.
- R3. Everything that reasons about the appointment's local date or time uses `eff_tz`:
  - the date filter (`get_appointments_by_date`, `select_eligible`);
  - the staff screen "when" column (shows the zone abbreviation, e.g. MDT);
  - the patient reminder text (exact Q-B4 wording, no suffix, per OD-TZ2).
- R4. These stay in CLINIC_TZ:
  - the target clinic date (`clinic_target_date`);
  - the daily send-quota day;
  - inbound "received" times;
  - "snapshot taken".
- R5. `SOURCE_TZ_MISMATCH` is no longer raised. A malformed or failed department response still rejects the whole fetch, with the same codes as PR #46.
- R6. Staff banners:
  - VERIFIED → unchanged.
  - Converted (recognized IANA, different from CLINIC_TZ) → "Sandbox department timezone DEPT differs from clinic timezone TZ — appointment times are read and texted in DEPT."
  - UNDETERMINED → the existing UNVERIFIED line.
- R7. Synthetic mode is byte-for-byte unchanged (no display zone set, so CLINIC_TZ is used everywhere).
- R8. Everything else from PR #46 still holds:
  - the 7-request bound;
  - the department GET before booked pages;
  - the allowlist;
  - no-leak behaviour;
  - sticky failures.

## 2. Architecture

- `synthetic_ehr.SyntheticAppointment` gains `display_tz: str | None = None`. It is the zone the appointment's local date/time belong to; None means CLINIC_TZ.
- `athena_source`:
  - `fetch_snapshot` computes `eff_tz` and passes it to `_map`.
  - Each mapped appointment gets `display_tz = eff_tz` when it differs from CLINIC_TZ.
  - `Snapshot.tz_status` takes the values VERIFIED | CONVERTED | UNDETERMINED. The `MISMATCH` raise is removed. `classify_tz` stays, and its MISMATCH result now maps to CONVERTED.
  - Aliases with identical offsets also map to CONVERTED: they are recognized IANA names, and converting with them is exact.
- `selection.select_eligible` / `SyntheticEhr.get_appointments_by_date`:
  - The date check uses `appt.display_tz or tz` per appointment.
  - Signatures are unchanged.
- `sending.reminder_text(first_name, start_utc, tz)` is unchanged. Its caller in `send_reminder` passes `appt.display_tz or settings.clinic_tz`. The quota-day `tz` stays `settings.clinic_tz`.
- `service.page_data` "when" uses `a.display_tz or tz`. `_source_block` exposes `tz_status` and `dept_tz`.
- `web._source_html` gains the CONVERTED line, and the mismatch-unavailable branch is removed.
- No DB, migration or settings changes. Stored `appt_start` is UTC, so restart and dedupe are unaffected.
- Out of scope:
  - multiple departments with different zones (the demo reads one configured department);
  - real SMS sends (OD-A3 still holds: simulated only).

## 3. Unit tests (tests first; new `test_athena_tz_convert.py` plus updates to `test_athena_tz.py`)

| ID | Case | Expected |
|---|---|---|
| C-1 | dept America/Denver, CLINIC_TZ NY, record 10/28 09:00 | start_utc 15:00Z; status CONVERTED; booked GET made |
| C-2 | same, staff page | when "Wednesday, Oct 28 at 9:00 AM MDT"; CONVERTED banner with both names; no UNVERIFIED |
| C-3 | same, send to appointment (stub gateway) | SMS body exactly the Q-B4 text with "on Wednesday, Oct 28 at 9:00 AM." (no suffix) |
| C-4 | Denver record 10/28 23:30 (= 10/29 01:30 NY) | kept for target 10/28 (department-local date), eligible |
| C-5 | DST in eff_tz: Denver 11/01 01:30 ambiguous; 03/14 02:30 nonexistent | excluded with AMBIGUOUS_TIME / NONEXISTENT_TIME (computed in Denver) |
| C-6 | quota day | still the clinic (NY) day: cap counted under the NY date |
| C-7 | dept America/New_York | VERIFIED; behaviour identical to PR #46 |
| C-8 | alias US/Eastern | CONVERTED with eff_tz US/Eastern; times identical to NY |
| C-9 | display name / missing / rejected | UNDETERMINED; times in CLINIC_TZ; UNVERIFIED line |
| C-10 | department HTTP 500 / bad shape / conflicting id | whole fetch rejected (unchanged) |
| C-11 | restart (epoch 2) with converted snapshot | slot dedupe still SKIPPED; prior labels intact |
| C-12 | synthetic mode | reminder text, when-column and selection unchanged (frozen suites green) |

Test changes to PR #46 tests (OD-TZ1 reverses their expectation; for Codex to authorise):
- `test_athena_tz.py` TZ-2 and TZ-3 (mismatch fail-closed, banner, sticky, no send) are replaced by C-1/C-2/C-3.
- In TZ-1/TZ-4/TZ-5, `tz_status` expectations change from UNDETERMINED to CONVERTED for US/Eastern.
- `classify_tz` unit cases are unchanged (the function stays).

## 4. Open follow-up

- O-TZ-1, the isolated live Denver smoke, is no longer needed as a separate profile: with OD-TZ1, the default NY-configured sandbox run will exercise CONVERTED. Any live run still needs a revised runner (allowlist + department GET, budget 9) and Codex review before operation.
