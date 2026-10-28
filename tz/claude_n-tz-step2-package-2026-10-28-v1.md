# N-TZ Step 2 — sandbox department timezone check in the athena source (package v1)

Claude, 2026-10-28. For Codex ALIGN before any code. Code base: main d2bc92a.

## 1. Facts this rests on (verified)

- Live read (Codex, 18:34:36Z, `codex_n-tz-live/department-tz.json`, SHA256SUMS OK — re-checked by Claude):
  - First-listed sandbox department: `timezonename` America/Denver, `timezone` -6, `timezoneoffset` -7, `doesnotobservedst` false.
  - `department_source` = first_listed (the Mac `.env` did not set DEMO_ATHENA_DEPARTMENT_ID). Department identity across runs is not proven.
- Offsets (Claude, zoneinfo): Denver is UTC-6 and New York is UTC-4 on 2026-10-28 and 10-30. On 11-02 and 2027-01-15 they are UTC-7 and UTC-5. The gap is a constant 2 h.
- Merged code (`athena_source._local_start`) reads `date` + `starttime` in CLINIC_TZ. With CLINIC_TZ=America/New_York, a Denver 09:00 slot becomes 13:00Z instead of 15:00Z, i.e. 2 h early.
  - Currently mitigated only by the "UNVERIFIED" banner.
  - Sends stay simulated (OD-A3).
  - Thursday demo uses the synthetic source and is unaffected.

## 2. Requirements

- R1. In athena_preview mode, before any booked GET of a snapshot, read the configured department's detail (`DEMO_ATHENA_DEPARTMENT_ID`). Only its four timezone fields are used, with the Step 1 bounds.
- R2. Classify against CLINIC_TZ, evaluated over the target clinic date:
  - VERIFIED: `timezonename` is a recognized IANA zone exactly equal to CLINIC_TZ.
  - MISMATCH (proven): `timezonename` is a recognized IANA zone, and its UTC offset differs from CLINIC_TZ's at some local time on the target date (all 48 half-hours checked, fold=0).
    - This is a comparison of zone rules, not of spelling.
  - UNDETERMINED: everything else. That covers:
    - a recognized IANA alias with identical offsets all day (for example US/Eastern);
    - a non-IANA or display name;
    - missing, null or rejected values;
    - offset/DST fields only.
- R3. On MISMATCH, fail closed: the whole snapshot is rejected with fixed code `SOURCE_TZ_MISMATCH`.
  - No booked GET is made.
  - The rejection is sticky per (process, date), like the existing failures.
  - Send batch has nothing to send.
  - CLINIC_TZ is never changed silently, and times are never re-interpreted in the department zone.
- R4. On VERIFIED, the banner replaces "Times assume … UNVERIFIED." with "Sandbox department timezone matches clinic timezone (TZ) — VERIFIED."
- R5. On UNDETERMINED, current behaviour is kept, including the existing UNVERIFIED banner.
- R6. If the department detail request fails (HTTP, network, JSON or shape), the whole fetch is rejected with the existing fixed codes, and no booked GET is made.
- R7. The MISMATCH banner (staff-facing only): "athenahealth sandbox unavailable — SOURCE_TZ_MISMATCH" plus "Sandbox department timezone DEPT differs from clinic timezone TZ."
  - DEPT is shown only when it is a recognized IANA name. A rejected value is never displayed.
- R8. Request bound per snapshot: token (if not cached) + 1 department GET + at most 5 booked GETs = 7.
  - The allowlist gains exactly `GET /v1/195900/departments/{configured id}`. No other department id is allowed.
- R9. Nothing from a response body, id or exception is logged or raised as text. Fixed codes only.

## 3. Architecture

- `athena_source.py`:
  - Add `DEPT_PATH` to the client allowlist, bound to `cfg.department_id`.
  - Add `client.department(token)`.
  - Add a pure function `extract_tz(body) -> TzFields` (reusing the Step 1 bounds).
  - Add a pure function `classify_tz(fields, clinic_tz, target) -> ("VERIFIED"|"MISMATCH"|"UNDETERMINED", dept_name|None)`.
  - `fetch_snapshot` calls these after the token and before page 1. It raises `SourceInvalid("SOURCE_TZ_MISMATCH")` on a mismatch.
  - `Snapshot` gains `tz_status` and `dept_tz` (str|None).
- `web._source_html`:
  - Renders the VERIFIED line, or the existing UNVERIFIED line, from `tz_status`.
  - Adds `SOURCE_TZ_MISMATCH` to the unavailable-banner path, with the R7 second line.
  - The mismatch needs `dept_tz` even though there is no snapshot: `SourceInvalid` gains an optional, already-validated `detail` (IANA name or None), which is stored with the sticky failure.
- No schema, migration or settings changes. Synthetic mode is untouched.

## 4. Unit tests (tests-first, new file `tests/demo_reminders/test_athena_tz.py`, mocked transport)

| ID | Case | Expected |
|---|---|---|
| TZ-1 | dept America/New_York | snapshot OK, `tz_status` VERIFIED, page shows VERIFIED line, no UNVERIFIED |
| TZ-2 | dept America/Denver | `SOURCE_TZ_MISMATCH`; booked GETs = 0; page banner + both names; Send batch returns [] |
| TZ-3 | mismatch is sticky | second page load: no new request |
| TZ-4 | US/Eastern | UNDETERMINED, proceeds, UNVERIFIED banner |
| TZ-5 | "Eastern Standard Time" / missing / null / 65-char / int / `<script>` | UNDETERMINED, proceeds; rejected text never in HTML |
| TZ-6 | offset -5 + doesnotobservedst false, no name | UNDETERMINED (offset never verifies) |
| TZ-7 | America/Phoenix vs CLINIC_TZ America/Denver on 2027-01-15 (same offsets all day) | UNDETERMINED; on 2026-07-15 → MISMATCH |
| TZ-8 | dept detail HTTP 500 / bad JSON / shape list-of-2 | whole fetch rejected with existing code; booked GETs = 0 |
| TZ-9 | allowlist | GET departments/{other id}, GET departments list, and PUT are DISALLOWED |
| TZ-10 | request count, 1 page | token 1 + dept 1 + booked 1; truncated path ≤ 7 |
| TZ-11 | no leak | sentinels in other dept fields never appear in HTML, events or exceptions |
| TZ-12 | synthetic mode | no department request, no tz line change |

Existing frozen suites must stay green. Some existing N-ATH tests use mocks that answer only token + booked, so they will now see a department GET. Each such mock will gain one department response (America/New_York) as a disclosed fixture-only change. No assertion changes; any assertion change would come back to you first.

## 5. Out of scope / follow-ups

- O-TZ-1: an isolated sandbox run with CLINIC_TZ=America/Denver to show VERIFIED end-to-end. It would use its own DB and never touch Thursday assets. Proposed after Step 2; needs your ALIGN.
- O-TZ-2 (business, for Rachel later; not blocking): for real clinics, if the EHR department zone differs from the clinic setting, should we refuse (Step 2 behaviour) or convert using the department zone? This affects patient-facing reminder times.
