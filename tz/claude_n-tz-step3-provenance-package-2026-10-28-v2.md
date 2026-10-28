# N-TZ Step 3 — interpretation provenance contract (package v2, consolidated)

Claude, 2026-10-28. Base: main 36c8c72.

This replaces Δ2 of v1r1 entirely. It answers codex_n-tz-step3-provenance-investigation.md as one contract, not a patch.

Unchanged from v1 and v1r1:
- OD-TZ1 (convert and send) and OD-TZ2 (Q-B4 text, no label).
- The eff_tz rules R1–R8.
- The Δ3 consumer audit.
- C-1..C-10, C-12 and the exact C-6 quota boundary.

## 1. The invariant

**A retained athena reminder must have been claimed under the same interpretation context that every later snapshot of this database uses.**

Duplicate prevention for sandbox appointments is the F-R1-1 slot key (tenant, patient, start instant). The epoch-qualified id changes on every restart. The slot key is stable exactly when the same booked record maps to the same UTC instant, and that holds when this tuple is unchanged:

`context = (department_id, effective_zone, basis, clinic_tz)`

- `basis` is DEPARTMENT when eff_tz comes from a recognized department zone, or FALLBACK when UNDETERMINED metadata fell back to CLINIC_TZ.
- `clinic_tz` is included conservatively. With DEPARTMENT basis it does not move instants, but it does move the target and quota day. With FALLBACK basis it is the effective zone.
- `department_id` is included because a different department means a different appointment set and a different zone authority.

Every path Codex identified is a violation of this invariant:

| Path | Before | After | Why it is a violation |
|---|---|---|---|
| Legacy | pre-conversion code: no record, NY 13Z | Denver 15Z | no record of the old context |
| Marked fallback → recognized | (21, NY, FALLBACK, NY) 13Z | (21, Denver, DEPARTMENT, NY) 15Z | context differs |
| Recognized zone change | Denver | Chicago | context differs |
| Config change | clinic_tz changed | — | context differs |

One rule covers all of them.

## 2. Contract

The record is a single `demo_meta` row with key `athena_tz_interpretation`. There is no schema change.

Its value has one exact format:

```
v1;dept=<ID_RE>;zone=<recognized IANA>;basis=<DEPARTMENT|FALLBACK>;clinic=<recognized IANA>
```

The value is parsed with a full-match regex and both zones are checked against `available_timezones()`. Anything else, including v1r1's bare `department_tz_v1`, is unsupported.

The rules run in athena_preview mode only. All DB work happens in one fenced transaction holding `pg_advisory_xact_lock(PROVENANCE_LOCK_CLASS, 0)`. "Ath reminders" means `reminder_records` rows with `emr_appt_id LIKE 'ath-%'`, in any state and any epoch.

**P0 — before any athena request** (on the first `_source()` of the process):

| Condition | Result |
|---|---|
| Value present but unsupported | refuse `TZ_PROVENANCE_UNSUPPORTED`, process-sticky, zero requests |
| Value absent and ath reminders exist | refuse `LEGACY_ATHENA_REMINDERS`, process-sticky, zero requests |
| Otherwise | continue |

**P1 — after the department GET, before any booked GET** (every snapshot). `fetch_snapshot` gains a `check_context` callback, which the service supplies.

The context is computed from the department response and the settings.

- **No process context established yet** (first validation in this process):

  | Stored value | Result |
  |---|---|
  | equal to the context | accept, and pin it as the process context |
  | absent, or different with zero ath reminders retained | write the context, accept, pin |
  | different with ath reminders retained | refuse `TZ_CONTEXT_CHANGED` for this date (sticky per date); booked GETs 0 |

  "Different with zero retained" covers a fresh DB, or one after reset-synthetic emptied `reminder_records`.

- **Process context already pinned:** a snapshot whose context differs is refused `TZ_CONTEXT_CHANGED` (sticky per date). A process never re-establishes. Sends in one process therefore happen under exactly one context, which closes the race between a re-establish and an in-flight claim. No other writer exists, because ownership is exclusive and fenced.

**Concurrency:** the per-process asyncio lock plus the advisory lock serialise first validation. Concurrent first use for two dates converges on one write, and the second validation compares against it.

**Never:**
- deletes, resets or rewrites reminders, timestamps or contact history;
- changes CLINIC_TZ;
- runs any of this in synthetic mode (no read, no write).

**Banners (staff-facing, fixed text, IANA names and basis only):**
- LEGACY: "athenahealth sandbox unavailable — LEGACY_ATHENA_REMINDERS" + "This demo database holds sandbox reminders from before timezone conversion; use a fresh demo database."
- UNSUPPORTED: "athenahealth sandbox unavailable — TZ_PROVENANCE_UNSUPPORTED" + "This demo database's timezone record is not recognized; use a fresh demo database."
- CONTEXT_CHANGED: "athenahealth sandbox unavailable — TZ_CONTEXT_CHANGED" + "Sandbox appointment times would now be read in ZONE (BASIS) but earlier sandbox reminders in this database used OLDZONE (OLDBASIS); use a fresh demo database."
  - Zone names are rendered only after IANA validation.

Unknown metadata stays visible. A FALLBACK context still shows the existing UNVERIFIED line, and still sends, per OD-TZ1 scope and the bounded sandbox policy. Codex: "unknown visible". The only change is that a later change of context is refused, not silently accepted.

The request bound is unchanged: P0 and P1 are DB-only, so a snapshot is still at most 7 requests.

## 3. Alternative assessed: stable provider-identity dedupe (not chosen)

The alternative is to dedupe on (tenant, provider appointmentid) in addition to the slot.

- Pros: it does not depend on interpretation at all.
- Cons:
  - It needs a new `reminder_records` column, which means a SCHEMA_VERSION bump. Every existing demo DB would then be refused, including Thursday's `haloflow_thu1029_demo` if it were ever started with new code. Thursday runs pinned e1da39c, but the bump is still a wider change.
  - It changes core `reminders.claim_reminder` semantics, which frozen F-R1-1 suites cover.
  - It raises a product question: an appointment rescheduled in the EHR under the same id would never get a reminder at its new time. That would need an owner decision.
  - The interpretation-provenance contract solves the same risk inside the athena layer, with no schema or core change.

Recommendation: the provenance contract. Provider-identity dedupe stays a candidate for real (non-sandbox) integration later.

## 4. Tests (consolidated; new `tests/demo_reminders/test_athena_tz_provenance.py`; stub gateway counts every external call)

| ID | Case | Expected |
|---|---|---|
| P-1 | exact value validation: `department_tz_v1`, `v2;…`, missing field, non-IANA zone, basis `OTHER`, trailing text | `TZ_PROVENANCE_UNSUPPORTED`; zero athena requests; no send; value unchanged |
| P-2 | unmarked legacy: ath reminder 13Z, no value | `LEGACY_ATHENA_REMINDERS`; zero requests; row untouched; no value written |
| P-3 | marked fallback → recognized: process 1, dept tz missing → FALLBACK NY, sends Denver 09:00 as 13Z; restart with dept Denver | `TZ_CONTEXT_CHANGED`; requests token + dept only; one reminder row still at 13Z; gateway calls stay 1; value unchanged |
| P-4 | recognized change Denver → Chicago with retained reminders | `TZ_CONTEXT_CHANGED`; no second send |
| P-5 | stable Denver → Denver restart | proceeds; epoch-2 same slot SKIPPED (15Z); one row; gateway calls 1 |
| P-6 | clinic_tz change (NY → Chicago), DEPARTMENT basis, retained reminders | `TZ_CONTEXT_CHANGED` |
| P-7 | stored value differs, zero ath reminders retained (e.g. after reset-synthetic) | re-established to the new context; proceeds |
| P-8 | in-process change: date A Denver established + sent; date B department returns Chicago | date B `TZ_CONTEXT_CHANGED` (no re-establish in-process); date A still served |
| P-9 | concurrent first use: two dates' first page loads gathered on a fresh DB | exactly one value written; both proceed; equal context |
| P-10 | synthetic mode on a DB with ath reminders and a bad value | runs normally; value neither read nor written |
| P-11 | only `syn-…` reminders, athena mode | value written; proceeds |
| P-12 | banners | exact texts above; no rejected or raw department text; no leak sentinels |
| P-13 | fallback context displayed | UNVERIFIED line still shown, and send allowed (OD-TZ1, unknown visible) |

C-13, C-13b, C-14 and C-15 from v1r1 are superseded by P-2, P-10, P-5 and P-11.

The authorised narrow replacements of PR #46's mismatch and alias expectations in `test_athena_tz.py` are as approved in Codex's review v1.
