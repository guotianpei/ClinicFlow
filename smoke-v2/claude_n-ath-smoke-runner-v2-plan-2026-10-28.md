# Integrated sandbox smoke — runner v2 (live check of main 425146d)

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
5. **Evidence additions:** `tz_status`, `effective_zone` (an IANA name) and `provenance_basis`. The department id stays in memory only.

**Offline checks:** `test_athena_sandbox_smoke.py`, 13 cases, all passed against main 425146d on PG16. They use a mocked athenahealth and a real local PG. They cover:
- CONVERTED happy path, with exact request order token / departments / token / department / booked, and 5 requests;
- VERIFIED and UNVERIFIED outcomes;
- truncation at exactly 9 requests;
- the no-eligible path;
- source failure with no leak;
- an existing DB or a bad DB name refused before any request;
- the gate refused before any effect;
- the allowlist, including selected-department-only;
- vetting;
- body and when patterns, including the DST-day abbreviation set.

ruff and mypy --strict are clean.

**Operation (Codex, after review):** from `haloflow/` at main 425146d, with the haloflow venv and PYTHONPATH=demo:
```
python <runner> --env-file .env --admin-url <admin URL, PG17 local> --db haloflow_smoke_tz_demo --out <evidence dir>
```
Exit codes: 0 completed, 1 stopped on a check (fixed code), 2 refused before any effect. One run only; no retries.
