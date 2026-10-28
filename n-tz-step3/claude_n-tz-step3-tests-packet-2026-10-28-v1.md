# N-TZ Step 3 — tests packet v1 (for FREEZE)

Branch feat/athena-tz-convert, commit bd70f7c478fd344ec145251e6634e8e622ff6ea4. Base: main 36c8c72. Source is unchanged.

Design: package v1 + v1r1 (Δ3 audit, C-6 exact) + provenance package v2 (ac85ccad…), Codex ALIGN codex_n-tz-step3-v2-alignment.md. Owner decisions OD-TZ1 / OD-TZ2.

## Files (sha256)
- tests/demo_reminders/test_athena_tz_convert.py (new, 13 cases): 3853b3b3c962408e7f95060d6265dfd3f4ad064f7a43e90de4724367fb0b6f8f
- tests/demo_reminders/test_athena_tz_provenance.py (new, 20 cases): 9e641a4e8b4ddd54c103b4f1ac545d9fb39f3d97111fd7fc0ff5cf0907913bab
- tests/demo_reminders/test_athena_tz.py (authorised replacement): 067c18430d8258c93cb3c206f04d76bcd100b130fe446657b47c65f839c0ea78
  - Full diff: test_athena_tz-replacement.diff.

## Conversion cases (test_athena_tz_convert.py)

- C-1: Denver 09:00 → 15:00Z. CONVERTED, eff_tz Denver, basis DEPARTMENT, display_tz Denver.
- C-5: DST computed in Denver.
  - 11/01 01:30 is AMBIGUOUS; 09:00 → 16:00Z.
  - 03/14/2027 02:30 is NONEXISTENT; 03:30 → 09:30Z.
- C-7: New York → VERIFIED, display_tz None, 13:00Z.
- C-8: US/Eastern → CONVERTED, 13:00Z.
- C-9 (snapshot, ×3: missing / display name / non-IANA): UNDETERMINED, FALLBACK, NY times.
- C-9 (page): EDT times, UNVERIFIED line, send allowed.
- C-2: page "when" shows "Wednesday, Oct 28 at 9:00 AM MDT", plus the CONVERTED banner, and no UNVERIFIED line.
- C-3: the SMS body is exactly the Q-B4 text "…on Wednesday, Oct 28 at 9:00 AM." with no zone label (OD-TZ2). It equals reminder_text(…, 15Z, Denver).
- C-4: Denver 23:30 on 10/28 (NY 10/29) stays on target 10/28 and is eligible. "when" shows 11:30 PM MDT.
- C-6: now = 10-27 04:30Z gives target 10-29. The quota row is (2026-10-27, 1), the New York day.
- C-12: synthetic display_tz is None and the text is unchanged.
- C-10 (department failures) stays covered by TZ-8 in test_athena_tz.py.

## Provenance cases (test_athena_tz_provenance.py)

The StubGateway's calls list counts every external send.

- P-1 (×7 values: v1r1 bare form, v2, missing field, non-IANA, bad basis, trailing field, bad dept): TZ_PROVENANCE_UNSUPPORTED, sticky. Zero athena requests, no send, value unchanged.
- P-2: an unmarked ath reminder at 13Z → LEGACY_ATHENA_REMINDERS. Zero requests, the row is untouched, no value written.
- P-3: process 1 has metadata missing (FALLBACK NY) and sends at 13Z. After restart the department reports Denver → TZ_CONTEXT_CHANGED.
  - Requests: token + department only, 0 booked.
  - Gateway calls stay 1; the row is still at 13Z; the value is unchanged.
- P-4: Denver → Chicago → CHANGED, no second send.
- P-5: stable Denver restart → epoch-2 same slot is SKIPPED. One row, one send.
- P-6: CLINIC_TZ NY → Chicago with retained reminders → CHANGED.
- P-7: a different stored value with zero ath reminders → re-established; sends.
- P-8: within one process, date B reports Chicago → CHANGED with no booked GET. Date A is still served.
- P-9: concurrent first use with equal contexts → exactly one row, both proceed.
- P-9b (Codex): concurrent first use with different contexts. Exactly one date succeeds; the loser has 0 booked GETs and sends nothing. The winner is still usable (it sends); the stored value is the winner's; the loser stays refused.
- P-10: synthetic mode on a DB with an ath row and the value "garbage" → runs normally; the value is untouched.
- P-11: syn-only reminders → value written, proceeds.
- P-12: exact CONTEXT_CHANGED, LEGACY and UNSUPPORTED banner texts. No SENTINEL or "<script" on the page.
- P-13: FALLBACK shows the UNVERIFIED line and sends; the stored value has basis FALLBACK.

## Interface pinned (new)
- Snapshot.eff_tz, Snapshot.basis (DEPARTMENT|FALLBACK); tz_status VERIFIED|CONVERTED|UNDETERMINED.
- SyntheticAppointment.display_tz.
- demo_meta key athena_tz_interpretation, with the exact value format v1;dept=..;zone=..;basis=..;clinic=..
- Error codes LEGACY_ATHENA_REMINDERS / TZ_PROVENANCE_UNSUPPORTED / TZ_CONTEXT_CHANGED.
- The P-9/P-9b tests call service._source(date) and _source_block(date) directly (noqa SLF001), to force concurrent first use for two dates.

## test_athena_tz.py replacement (authorised in Codex review v1)
- TZ-2 snapshot: mismatch-raises → CONVERTED, request order token → department → booked, 08:00 Denver → 14:00Z.
- TZ-4/5: US/Eastern → CONVERTED. The display name and None stay UNDETERMINED.
- TZ-2/3 page: mismatch banner and no-send → CONVERTED banner; appointments listed; snapshot reused (3 requests); no UNVERIFIED and no SOURCE_TZ_MISMATCH on the page.
- The docstring is updated. Malformed-data, allowlist, request-bound, no-leak and classify/extract cases are unchanged.

## Baselines (PG16 local; source = main 36c8c72): baseline-red.txt
- test_athena_tz_convert.py: 12 failed, 1 passed. The pass is C-9 page, where fallback is identical to main by design.
- test_athena_tz_provenance.py: 19 failed, 1 passed. The pass is P-10, where synthetic is unchanged by design.
- test_athena_tz.py: 3 failed (exactly the 3 replaced tests), 49 passed.
- ruff clean on all three files. No other test file was changed.
