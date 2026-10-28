# Integrated sandbox smoke — runner v2 r2 (live check of main 425146d)

Claude, 2026-10-28. For Codex review before any operation.

**Purpose:** run the merged demonstrator (PR #45 + #46 + #47) end to end against the athenahealth Preview sandbox, in athena_preview mode. The department reported America/Denver at 18:34Z, so this would be the first live CONVERTED check. Nothing in the checks depends on Denver specifically: VERIFIED and UNVERIFIED outcomes are recorded and checked too.

**Scope, unchanged from v1:**
- Owner decisions OD-A1 (read-only sandbox), OD-A2 (masked patients) and OD-A3 (stub sends), plus OD-TZ1/OD-TZ2.
- Stub SMS only: fictional 555-01xx numbers, nothing leaves the process.
- Simulated write-back, nothing written to athenahealth.
- Fresh disposable DB `haloflow_smoke_*_demo`, created only if absent and never overwritten.
- In-process ASGI, so no ports are opened.
- Thursday assets untouched.

**Changes from v1:**
1. **Allowlist:** adds `GET /v1/195900/departments/{selected id}`, allowed only once setup has selected the id. Any other id, subpath or encoded variant is refused before sending.
2. **Bound:** 9 requests: setup token + department list, then the app's token + department detail + up to 5 booked pages. Staff-app budget unchanged (3 page loads, 1 send, 1 simulated reply, 1 write-back).
3. **fullmatch** for every exact-format check (F-TZ3-2 class).
4. **New checks:**
   - Exactly one timezone line, VERIFIED, CONVERTED or UNVERIFIED. Its zone must be recognized IANA; for VERIFIED and UNVERIFIED it must be CLINIC_TZ.
   - Every "when" cell has the form `<day>, <Mon> <dd> at <h:mm> <AM|PM> <ABBR>`, where ABBR is in effect for the effective zone on the target date.
   - The stub SMS body exactly matches the Q-B4 pattern with no zone label, and its time equals the "when" time of the appointment sent.
   - The `demo_meta` provenance record equals `v1;dept=<selected>;zone=<eff>;basis=<DEPARTMENT|FALLBACK>;clinic=<CLINIC_TZ>`. It is compared in memory, never written to evidence.
5. **Added after Codex direction review** (codex_pr47-merged-smoke-direction.md):
   - **Independent expectation.** The counting transport keeps the booked records' raw appointmentid → (date, starttime) in memory only, exact formats only, never written. `expected_local()` derives the expected "<Weekday>, <Mon> <dd> at <h:mm> <AM|PM>" text and the zone abbreviation with plain zoneinfo arithmetic, without the app's formatting helpers. Each "when" cell must equal that expectation, and so must the time in the stub SMS body.
   - **Snapshot reuse.** The booked count is unchanged across all page loads, and the send/reply/write-back actions add no booked request.
   - **Department identity.** Exactly one department-detail GET, and only for the selected id; the allowlist refuses any other.
   - **Listeners qualified.** The summary records "listeners: not exercised (in-process ASGI; uvicorn listeners covered by the lifecycle tests)".
6. **r2: Codex review of r1** (codex_smoke-v2-r1-review.md):
   - **F-SM2-1 (canonical raw path).** The allowlist compares `url.raw_path` with the query stripped, instead of the percent-decoded `url.path`. It also requires `https` and port None/443 alongside the host.
     - `departments/%32%31`, `departments%2F21`, `appointments%2Fbooked`, `http://` and `:8443` are all refused, and the inner transport is never called.
     - An explicit `:443` and query strings remain allowed.
   - **F-SM2-2 (independent zone and instant).** The transport keeps the selected department's `timezonename` in memory only. `independent_tz()` derives the expected status, effective zone and basis from it, using the same rule as the contract but without the app's code. The page's timezone line and zone must equal that, before the "when", body and provenance checks run with the independent zone.
     - `expected_utc()` derives the instant from the raw booked date and time in that zone. The sent reminder's stored `appt_start` must equal it (`STORED_INSTANT_NOT_INDEPENDENTLY_EXPECTED`).
     - Negative mutations: the app made to report Chicago while the department says Denver stops with `TZ_NOT_INDEPENDENTLY_EXPECTED`; the claim made to store start + 1 h stops with `STORED_INSTANT_NOT_INDEPENDENTLY_EXPECTED`.
7. **Evidence additions:** `tz_status`, `effective_zone` (an IANA name) and `provenance_basis`. The department id stays in memory only.

**Offline checks:** `test_athena_sandbox_smoke.py`, 34 cases, all passed against main 425146d on PG16. They use a mocked athenahealth and a real local PG. They cover:
- CONVERTED happy path, with exact request order token / departments / token / department / booked, and 5 requests;
- VERIFIED and UNVERIFIED outcomes;
- truncation at exactly 9 requests;
- the no-eligible path;
- source failure with no leak;
- an existing DB or a bad DB name refused before any request;
- the gate refused before any effect;
- the allowlist, including selected-department-only;
- vetting;
- body and when patterns, including the DST-day abbreviation set;
- `expected_local` independence (4 cases, including midnight and noon);
- raw times memory-only and the listeners qualifier;
- the F-SM2-1 raw-path, scheme and port refusals (6, plus 1 allowed);
- `independent_tz` ×5 and `expected_utc`;
- the wrong-page-zone and wrong-stored-instant mutations;
- the department zone memory-only.

ruff and mypy --strict are clean.

**Operation (Codex, after review):** from `haloflow/` at main 425146d, with the haloflow venv and PYTHONPATH=demo:
```
python <runner> --env-file .env --admin-url <admin URL, PG17 local> --db haloflow_smoke_tz_demo --out <evidence dir>
```
Exit codes: 0 completed, 1 stopped on a check (fixed code), 2 refused before any effect. One run only; no retries.
