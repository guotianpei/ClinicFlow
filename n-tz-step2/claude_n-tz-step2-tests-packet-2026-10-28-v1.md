# N-TZ Step 2 — tests packet v1 (for FREEZE)

Branch feat/athena-dept-tz, commit 6d2e922df9debc908ff03446d7bc93274d4b3d8f (base main d2bc92a). Source unchanged.

## Files (sha256)
- tests/demo_reminders/test_athena_tz.py (new, 52 cases) 67a5c5dbd9e72649b0fe838a28837881c04eefd2ee16fe8e0fbf0c08178516e4
- tests/demo_reminders/test_athena_source.py (narrow update) 7613ee43b597c2c9ab2b88555df29981565c3268ec979f7ac5055f8b74b80e95
- tests/demo_reminders/test_athena_startup.py (narrow update) 4c4035c6a73d81a65c9aa6dbd78961f5c98ffb519a4db426d58102275e91a941

## Coverage map (package TZ-1..12 + Codex ALIGN points)
- TZ-1 VERIFIED: snapshot + order token → department → booked; page VERIFIED line, no UNVERIFIED.
- TZ-2/3 MISMATCH (America/Denver): code SOURCE_TZ_MISMATCH, detail America/Denver, zero booked GETs, sticky (no new request on reload/preview/batch), banner with both names, no synthetic fallback.
- TZ-4 alias US/Eastern, TZ-5 display name / bad text, TZ-6 offsets only → UNDETERMINED, proceeds, UNVERIFIED line kept; rejected text never in HTML.
- TZ-7 incl. DST transition dates (Codex): Phoenix/Denver winter vs summer; Santo_Domingo vs New_York 2026-10-31 (equal) vs 2026-11-01 fall-back (mismatch from 06:00Z only) and 2027-03-14 spring-forward vs 03-15. "Never VERIFIED by offsets" check.
- Malformed body vs missing field (Codex): list len≠1, non-dict, string/int/null, conflicting departmentid → SOURCE_INVALID; missing/unrecognized timezonename → None/UNDETERMINED.
- TZ-8 department HTTP 500/404, bad JSON, shape, conflicting id → whole fetch rejected, zero booked GETs, no leak.
- TZ-9 refused before transport (Codex): departments/22, departments list, providers subpath, PUT, other practice.
- TZ-10 bound: truncated path exactly 7 requests, one department GET.
- TZ-11 no leak: sentinels in department name/address/token/body never in HTML/JSON/exceptions/logs.
- TZ-12 synthetic mode: source block unchanged, no department request.

## Interface pinned by the tests (new)
classify_tz(name|None, clinic_tz, target) → "VERIFIED"|"MISMATCH"|"UNDETERMINED"; extract_tz(body, department_id) → str|None (recognized IANA only); SourceInvalid.detail; Snapshot.tz_status / dept_tz; page source keys tz_status / dept_tz.
Banner texts: "Sandbox department timezone matches clinic timezone (TZ) — VERIFIED." / "athenahealth sandbox unavailable — SOURCE_TZ_MISMATCH" + "Sandbox department timezone DEPT differs from clinic timezone TZ."

One refinement beyond package v1 (please check): a present `departmentid` in the detail body that differs from the configured id → SOURCE_INVALID (absent id accepted).

## Narrow existing-test updates (Codex-authorized; full diff: existing-tests-narrow-update.diff)
1. Api fixture answers GET /v1/195900/departments/21 with [{"departmentid": "21", "timezonename": "America/New_York"}].
2. Api.gets() now returns booked-page GETs only (department GET counted by new dept_gets()). Assertions using gets() (offsets, page params, 5-page cap, single flight, per-date starts) are unchanged in text and meaning.
3. test_a2 sequence: department GET inserted between token and booked.
4. test_a2_redirect: request count 2 → 3.
5. test_a9 page: UNVERIFIED assertion → VERIFIED line (fixture supplies matching IANA).
6. test_a8 single flight: adds dept_gets() == 1.
7. test_athena_startup i2 oversized: GET count == 1 → booked-path count == 1.
No send, recovery, no-leak, identity, pagination or refusal assertion was removed or weakened.

## Baselines (PG16 local, source = main d2bc92a)
- Existing suites before test edits: 598 passed.
- New file: collection ImportError (classify_tz missing) — expected red.
- Updated existing files on unchanged source: exactly 4 failed (the 4 changed assertions: a2 sequence, a2 redirect count, a9 VERIFIED line, a8 dept count), 63 passed.
ruff clean on the three test files.
