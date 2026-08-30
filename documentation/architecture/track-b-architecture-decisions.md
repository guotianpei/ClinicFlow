# HaloVox Track B — Architecture Decision Records

**Project:** HaloVox Track B — PHI / Clinic Automation  
**Date:** 2026-08-02  
**Author:** HaloVox Engineering  
**Status:** Proposed — pending Notifyre capability verification and security/privacy risk-owner sign-off  
**Requirements reference:** track-b-event-tracking-requirements.md v2.3

> **ADR process note:** ADRs are immutable once accepted. A material change to an accepted decision must supersede it with a new ADR referencing the superseded one. ADRs in Proposed status may be revised freely. "Accepted in principle" is not a valid status — use Proposed until acceptance criteria are fully satisfied.

---

## ADR-001: Schema-Per-Tenant with Application-Enforced Isolation and Compensating Controls

**Status:** Proposed — security/privacy risk-owner sign-off required before production  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead + Security/Privacy Risk Owner (sign-off required)  
**Review trigger:** Cross-tenant data access incident; production launch; tenant count > 50  
**Related requirements:** REQ-MT-01 through REQ-MT-22, REQ-NFR-07 (revised per Option A)

### Context

HaloFlow serves multiple clinics on a shared PostgreSQL instance. Tenant data includes patient phone numbers, patient IDs linked to health events, and structured fax fields — all PHI under HIPAA's 18-identifier definition. The database is a PHI system covered by the BAA with GCP.

Requirements REQ-MT-02, REQ-MT-19/20 and REQ-NFR-07 previously stated "database-level isolation" and "data-plane role restricted to exactly one tenant schema at a time." This ADR supersedes those requirements with an honest description of what is actually enforced.

### Decision

**Option A (chosen):** Use PostgreSQL schema-per-tenant for data organisation, with isolation enforced at the application layer. Requirements REQ-MT-02, REQ-MT-19/20 and REQ-NFR-07 are revised to reflect this accurately.

Three distinct concepts are separated:
- **Physical organisation:** schema-per-tenant in PostgreSQL
- **Authorization boundary:** application-enforced via `SET LOCAL search_path` and compensating controls — not PostgreSQL role grants
- **Prohibited behaviour:** no tenantless query paths; no schema-qualified cross-tenant queries in application code

Compensating controls:
- `SET LOCAL search_path` scoped to the resolved tenant schema inside every transaction
- Schema names resolved exclusively from `shared.tenants` — never from external input
- Tenant-aware data access layer that rejects queries missing a resolved tenant context
- Append-only audit log for all control-plane and support access (REQ-MT-21)
- Code review and negative cross-tenant integration tests as mandatory CI gates
- Security/privacy risk-owner sign-off required before production launch

**Acknowledged limitation:** `search_path` is not a PostgreSQL authorization boundary. The data-plane role holds GRANTs on all tenant schemas — application code can issue a schema-qualified cross-tenant query that succeeds. An incorrect `search_path` may silently return another tenant's data without failing loudly. This is a conscious risk decision accepted by the security/privacy risk owner, not an engineering oversight.

**Option B (deferred — future security enhancement):** One PostgreSQL role per tenant assumed via controlled `SET ROLE` on each request. Provides database-enforced isolation; eliminates the acknowledged limitation. Deferred due to provisioning complexity at scale. To be revisited before tenant count exceeds 50 or on any cross-tenant access incident.

### Options Considered

| Dimension | Option A: App-enforced (chosen) | Option B: Per-tenant DB roles | Option C: Row-Level Security | Option D: DB-per-tenant |
|---|---|---|---|---|
| Isolation strength | Application-enforced; acknowledged gap | DB-enforced; no gap | DB-enforced if all tables covered | Highest |
| Operational overhead | Low | Medium — N roles, provisioning on onboarding | High — RLS policy on every table | High — N DB instances |
| Silent cross-tenant leak risk | Low with compensating controls | Very low | Low if all tables covered | Very low |
| Migration complexity | Low | Low | Medium | High |

### Consequences

- REQ-MT-02, REQ-MT-19/20 and REQ-NFR-07 revised to reflect application-enforced isolation with compensating controls.
- All schema names use immutable opaque keys from `shared.tenants`.
- `SET LOCAL search_path` inside transactions mandatory. Connection pools must reset on return.
- Negative cross-tenant tests are required CI gates before any release.
- Security/privacy risk-owner signature required in this ADR before production launch.

---

## ADR-002: Shared Inbound-Number Registry and Outbound Message Routing

**Status:** Proposed — Notifyre client-reference capability to be verified  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead  
**Review trigger:** Notifyre verification items 2 and 5 resolved  
**Related requirements:** REQ-MT-10 through REQ-MT-13, REQ-ROUTER-03, REQ-SMS-01, REQ-FAX-LOG-01, REQ-FAX-LOG-02

### Context

Every inbound communication arrives on a "to" number before tenant identity is known. Outbound delivery callbacks arrive with a provider message ID and may not include the clinic's sending number — the routing mechanism for these callbacks must be defined independently of what Notifyre includes in callbacks.

### Decision

**Inbound routing (patient SMS reply, inbound fax):**  
`shared.tenant_inbound_number_registry` maps every clinic number → `tenant_id + schema_key`. The Inbound Router queries this first, before opening any tenant schema.

**Outbound delivery callback routing — Path B as default:**  
`shared.provider_message_registry` is the primary callback routing source, populated at send time:

```
shared.provider_message_registry
- provider
- provider_account
- external_id          (Notifyre message ID or fax ID; populated when provider confirms)
- operation_id         (our stable ID; available immediately at send time)
- tenant_id
- schema_key
- channel              (sms / fax)
- created_at
```

Lookup key: `(provider, provider_account, external_id)`.  
Secondary lookup: `(provider, provider_account, operation_id)` — used for recovery when `external_id` was not yet committed.  
The clinic's FROM number (from `tenant_inbound_number_registry`) serves as validation/fallback only — not the primary routing path.

**Recovery for missing registry entries:**  
If the sender crashes before committing the registry entry, `external_id` may never appear. Recovery requires at least one of:
- Notifyre echoes a client-supplied reference ID (our `operation_id`) in every callback — enables lookup by `operation_id` without `external_id`
- An operator-assisted unresolved-callback workflow with explicit expiry and alerting

**Action item — Notifyre verification item #2 (extended):** Confirm (a) do delivery callbacks include the clinic's FROM number? (b) can Notifyre accept a client-supplied reference ID on send and echo it in every callback? (c) can the Notifyre status API query by client reference?

If callback arrives before registry is committed: retain in a global `unresolved_callback_queue`; retry resolution on a schedule; alert ops after max attempts.

### Options Considered

| Option | Assessment |
|---|---|
| Tenant-local outbound log only (original) | Cannot route delivery callbacks — tenant unknown at callback time |
| Path A only (FROM number) | Works only if Notifyre guarantees FROM in every callback; two routing implementations with different guarantees |
| Path B as default + FROM as validation (chosen) | One routing implementation; consistent guarantees; FROM adds defence-in-depth |

### Consequences

- `shared.provider_message_registry` provisioned at schema initialisation.
- Registry entry created at send time with `operation_id` immediately; `external_id` updated when provider confirms.
- `unresolved_callback_queue` required with scheduled retry and ops alerting.
- Per-tenant outbound logs (`outbound_sms_log`, `outbound_fax_log`) retain full send records for business purposes; the shared registry is routing infrastructure only.

---

## ADR-003: Three-Level Event Model with Stable Operation ID

**Status:** Proposed  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead  
**Review trigger:** Reconciler false-positive/negative rate exceeds threshold; EMR adapter lookup capability changes  
**Related requirements:** REQ-EVT-01 through REQ-EVT-11

### Context

Previous designs conflated three distinct concepts: (1) intent to invoke an external action, (2) whether the provider accepted the API submission, and (3) whether the action was ultimately delivered or completed. These have different timing, different failure modes, and different recovery paths. Mixing them produces contradictions (e.g., `appointment_reminder_sent` with `status = pending`) and makes reconciliation logic ambiguous.

### Decision

All external side effects (Notifyre SMS, Notifyre fax, EMR API writes) produce events at three distinct levels, all linked by a stable `operation_id`:

**Level 1 — Intent (before external call):**
- Event type: `{action}_requested`
- `operation_id` generated here (UUID v4)
- `idempotency_key` = `operation_id` (passed directly to provider — see ADR-007)
- `external_id` is null at this level

**Level 2 — Submission outcome (after API call returns):**
- `{action}_accepted` — provider accepted the request; `external_id` (provider message ID) now available and populated on this row
- `{action}_rejected` — provider rejected definitively (e.g., invalid number); `external_id` remains null
- `{action}_submission_indeterminate` — call timed out or no response; `external_id` remains null

**Level 3 — Delivery/business outcome (from provider callback or EMR confirmation):**
- `{action}_delivered` — provider confirmed delivery
- `{action}_delivery_failed` — provider confirmed failure
- `{action}_delivery_indeterminate` — conflicting or unresolvable callbacks

**`external_id` population rule:** Null on intent rows, rejection rows, and indeterminate rows. Populated on `{action}_accepted` submission rows only — this is when the provider first returns a message ID.

All three levels share the same `operation_id`. Current state is a **derived projection** from all events for that `operation_id` — the append-only source events are never mutated.

**Scope:** Intent/outcome pattern applies to **external side effects only.** Inbound observations (`fax_received`, `patient_reply_received`, `sms_stop_received`) require a single append-only completed event written transactionally with the corresponding state change — no intent record.

**EMR operations:** EMR write outcomes cannot be reconciled via Notifyre. An EMR `submission_indeterminate` event must trigger a staff alert with an explicit "EMR outcome unknown — manual verification required" work queue item.

**Constraints:**
- At most one provider acceptance identity per `operation_id` (DB constraint)
- Webhook events are deduplicated by provider event ID (ADR-004); conflicting callbacks are retained and flagged, not rejected

### Options Considered

| Option | Assessment |
|---|---|
| Single event type with status flag | `appointment_reminder_sent` + `status = pending` is logically contradictory |
| Intent + outcome, no operation_id | Cannot reliably pair records across crash/recovery |
| Three-level intent/submission/delivery + operation_id (chosen) | Precise semantics; stable linkage; maps to actual provider lifecycle |

### Consequences

- `patient_events` gains `operation_id` (indexed), `idempotency_key`, `submission_level` (intent / submission / delivery).
- All event type codes updated to `{action}_requested` / `{action}_accepted` / `{action}_rejected` / `{action}_submission_indeterminate` / `{action}_delivered` / `{action}_delivery_failed` convention.
- Current-status projection logic defined per `operation_id` — not stored redundantly.
- `ref_event_statuses` is a separate reference domain from `ref_queue_statuses` (see ADR-005).

---

## ADR-004: Async Durable Webhook Inbox with Stateful Processing Claims

**Status:** Proposed  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead  
**Review trigger:** Duplicate event processing or webhook loss detected in production  
**Related requirements:** REQ-ROUTER-02, REQ-ROUTER-09

### Context

Notifyre guarantees at-least-once webhook delivery. Two failure modes must be addressed: (1) a process crash after beginning tenant processing leaves the webhook unprocessed but a deduplication row exists, so provider retries are incorrectly silenced with HTTP 200; (2) concurrent duplicate deliveries may both proceed simultaneously if not claim-fenced.

### Decision

Use an **async durable inbox** pattern:

**Step 1 — Durable ingestion (synchronous, in the HTTP handler):**
1. Verify HMAC signature — reject immediately on failure (REQ-ROUTER-08)
2. Check timestamp tolerance
3. Persist the full webhook envelope/payload to `shared.webhook_inbox` (see PHI controls below)
4. Return HTTP 2xx immediately — this means "durably accepted," not "business processing completed"

**Duplicate handling:** A valid duplicate (same `(provider, provider_account, event_id)`, HMAC passes) receives HTTP 2xx without a second insert, regardless of current inbox status (`received` / `processing` / `retryable_failed` / `completed`). The scheduled reclaimer ensures recovery for any non-completed row. Only failed HMAC receives a rejection response.

**Step 2 — Async processing (background worker with claim lifecycle):**

```
shared.webhook_inbox
- provider
- provider_account
- event_id            (unique constraint on (provider, provider_account, event_id))
- payload             (application-layer encrypted column)
- status              (received / processing / completed / retryable_failed)
- claim_owner         (worker instance ID)
- claim_expires_at
- attempt_count
- created_at
- completed_at
- last_failure
```

Worker claims a `received` or reclaimable row atomically, processes it, then marks `completed`. If processing fails, marks `retryable_failed` so a later worker can reclaim.

**Reclaim conditions:**
- `status = retryable_failed`
- `status = processing` AND `claim_expires_at` has passed (abandoned claim)

**A `completed` row is never reprocessed** — this is the only state that silences future provider retries.

**Independent scheduled reclaimer:** Scans for expired/failed claims and requeues them. Does not rely solely on provider retries for recovery.

### PHI Controls for `webhook_inbox`

Raw webhook payloads may contain PHI (patient phone numbers, fax metadata) and sit in the shared schema before tenant routing. Required controls:

- **Encryption:** Payload column encrypted at the application layer — not relying solely on GCP at-rest encryption. HaloVox owns and rotates the encryption key independently of GCP defaults.
- **Plaintext metadata:** Event type, provider, event_id, timestamps stored in unencrypted columns for indexing and querying. Raw payload in encrypted column only.
- **Access:** Only the webhook processor and scheduled reclaimer decrypt payload. Control-plane/support role sees metadata columns only — not raw payload.
- **Retention:** On `status = completed`, raw payload purged after 7 days. `retryable_failed` and unroutable payloads retained 30 days for ops investigation, then purged.
- **Audit:** Any access to the encrypted payload column logged in `access_audit_log`.

### Consequences

- HTTP handler is minimal: HMAC verify → persist (or acknowledge duplicate) → 2xx. No tenant lookup, no business logic.
- Business processing happens asynchronously; webhook processing latency increases slightly but reliability improves significantly.
- Downstream business operations retain their own idempotency constraints — the inbox guarantees safe retry delivery, not at-most-once business execution.
- Scheduled reclaimer is a required operational component.
- REQ-ROUTER-02 and REQ-ROUTER-09 updated to reflect durable-inbox 2xx semantics.

---

## ADR-005: Indeterminate State, Reconciliation Lifecycle, and Append-Only Event Projection

**Status:** Proposed  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead  
**Review trigger:** Reconciler false-positive/negative rate; staff alert volume threshold  
**Related requirements:** REQ-EVT-01, REQ-EVT-10, REQ-EVT-11

### Context

A process crash after an external call but before writing a submission outcome leaves the intent row (`{action}_requested`) as the only record. The reconciler detects stale intents via an anti-join — but once it writes the first `{action}_submission_indeterminate`, that row has submission level and is excluded from all future anti-join scans. `patient_events` is append-only and holds no mutable retry state. Without a separate operational store, the documented retry/escalation lifecycle cannot function.

Additionally, `indeterminate` is an event status, not a queue status — it must not be added to `ref_queue_statuses`.

### Decision

**Event status domain:** A separate `ref_event_statuses` reference table defines valid status codes for `patient_events`. This domain is independent of `ref_queue_statuses` (work queue lifecycle) and `ref_job_run_statuses` (batch run lifecycle).

**`reconciliation_cases` table — mutable retry lifecycle:**

```
reconciliation_cases (shared schema, control-plane write)
- operation_id          (unique)
- status                (open / retry_scheduled / resolved / escalated)
- attempt_count
- next_attempt_at
- last_attempt_at
- lease_owner
- lease_expires_at
- resolution_event_id   (FK to patient_events — populated when resolved)
- work_queue_id         (FK — populated when escalated to staff)
- created_at
- updated_at
```

`patient_events` remains append-only and is the audit record. `reconciliation_cases` owns retry scheduling and mutable operational state.

**First-time reconciliation — anti-join on stale intents:**
```sql
SELECT intent.*
FROM patient_events intent
WHERE intent.submission_level = 'intent'
  AND intent.created_at < (NOW() - staleness_threshold)
  AND NOT EXISTS (
    SELECT 1 FROM patient_events outcome
    WHERE outcome.operation_id = intent.operation_id
      AND outcome.submission_level IN ('submission', 'delivery')
  )
  AND NOT EXISTS (
    SELECT 1 FROM reconciliation_cases rc
    WHERE rc.operation_id = intent.operation_id
  )
```

This fires only once per operation — when no submission event and no reconciliation case exist yet.

**Subsequent retries:** Reconciler queries `reconciliation_cases` where `status = open` AND `next_attempt_at <= NOW()`, using lease for concurrency safety.

**Normalized provider outcomes — reconciler uses adapter-mapped results:**

Each provider adapter (Notifyre SMS, Notifyre fax, EMR) maps its raw status codes to one of these normalized outcomes before the reconciler acts:

| Normalized outcome | Events to write | Case action |
|---|---|---|
| Not found | `{action}_submission_indeterminate` | Open case; schedule retry |
| Submission rejected | `{action}_rejected` | Resolve case |
| Accepted, delivery pending | `{action}_accepted` (if not written) | Resolve case; delivery events expected via callback |
| Accepted, delivered | `{action}_accepted` + `{action}_delivered` | Resolve case |
| Accepted, delivery failed | `{action}_accepted` + `{action}_delivery_failed` | Resolve case |
| Conflicting / unknown | `{action}_submission_indeterminate` | Open case; schedule retry or escalate |

**Note:** "Provider confirms failure" does NOT always mean writing an accepted event first. A submission rejection produces `{action}_rejected` only — no acceptance event. Accepted + delivery failed produces both events. The adapter is responsible for normalizing correctly.

**Note on EMR:** EMR adapters have different lookup guarantees. An EMR "no record" does not mean the same thing as Notifyre "no record." Each adapter documents its normalized outcome mapping.

**Reconciler flow per case:**
1. Claim case with lease
2. Query provider via adapter → get normalized outcome
3. Write appropriate patient_events (append-only)
4. Update case: increment `attempt_count`, set `next_attempt_at`, update status
5. Provider does not support lookup → write `{action}_submission_indeterminate`; create idempotent staff work item immediately; mark case `escalated`
6. Max attempts exceeded → mark `escalated`; create staff work item

**Current-status projection rule:**
- Collect all events for an `operation_id` ordered by `occurred_at` (provider timestamp)
- If `occurred_at` unavailable: use `created_at` for ordering non-conflicting sequential events only
- `created_at` must NOT decide precedence between two conflicting terminal outcomes — those go to `delivery_conflict_requires_reconciliation`
- Delivery-level events take precedence over submission-level; submission-level over intent-level
- If two terminal delivery events conflict: retain both; set projection to `delivery_conflict_requires_reconciliation`; create one idempotent staff work item
- Source events are never discarded or mutated

### Consequences

- `ref_event_statuses` provisioned as a new shared-schema reference table.
- `reconciliation_cases` provisioned as a new shared-schema control-plane table.
- `patient_events` gains `submission_level` column (`intent` / `submission` / `delivery`).
- Reconciler is a required scheduled job; must be idempotent; uses lease per case.
- `indeterminate` is an event status code in `ref_event_statuses` — not added to `ref_queue_statuses` or `ref_job_run_statuses`.

---

## ADR-006: Hybrid Reply Matching — Time-Window Primary, Conflict Token on Demand

**Status:** Proposed  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead + Clinical Risk Owner (patient safety acceptance)  
**Review trigger:** Ambiguous or wrong-match rate in work queue exceeds acceptable threshold  
**Related requirements:** REQ-SMS-01 through REQ-SMS-08

### Context

Two SMS message types exist with different matching requirements:

**Reminders** (informational — sent 3 days before appointment): "Your appointment is Thursday 2pm. Questions? Call the office." No patient action expected, no reply matching needed.

**Appointment confirmation requests** (action required — patient replies YES/NO): These require reply matching because a YES or NO drives an EMR write. The challenge: shared household phones may have multiple confirmation requests open simultaneously (e.g., two children with separate appointments in the same week).

Original designs tried FIFO ordering (wrong-appointment risk) then mandatory tokens for all messages (patient friction). The correct design is: token-free for most cases, token on demand when a conflict is detected.

### Decision

**Reminders:** One-way informational messages. No `outbound_sms_log` reply-matching record needed. No reply matching logic applies.

**Appointment confirmation requests — hybrid matching:**

**Step 1 — Consent keywords always first:**  
STOP / START / HELP are processed before any appointment matching. No confirmation context needed.

**Step 2 — Normal case (single open confirmation for this phone):**  
Time-window matching: `from_number` (inbound) matches `to_number` in `outbound_sms_log` where `reply_status = pending` and within reply window. If exactly one match → auto-confirm atomically. No token involved.

**Step 3 — Conflict case (new confirmation sent while another is already open):**  
At INSERT time of a new `outbound_sms_log` record, the system checks whether any other record for the same `to_number` has `reply_status = pending`. If yes → a conflict exists. The **new message** includes a disambiguation token = **last 2 digits of `appointment_id`** (e.g., "Reply YES 42 to confirm your appointment").

**Step 4 — Reply matching when conflict token is present:**
- Reply contains 2-digit suffix (e.g., "YES 42") → match against open records where `appointment_id LIKE '%42'` for that phone
- Exactly one match → confirm atomically
- Zero or multiple matches (token collision, ~1% chance) → route to `inbound_sms_ambiguous_match` work queue
- Reply contains no token with 2+ open records → route to `inbound_sms_ambiguous_match` work queue

**Confirmation expiry:** Confirmation records expire at the **appointment date/time** — not a fixed-hour reply window. This eliminates the gap between natural expiry and new confirmation arrival.

**FIFO ordering is explicitly prohibited.** Selecting the oldest record risks confirming the wrong family member's appointment.

### Why Token-Free is Safe for Single-Open Case

When exactly one confirmation is pending for a phone number, there is no ambiguity about which appointment the reply refers to. The auto-match is safe. The "delayed reply to wrong appointment" risk (B1) only materialises when two confirmations coexist — which is precisely when the conflict token fires.

### Options Considered

| Option | Assessment |
|---|---|
| FIFO by `sent_at` | Patient safety risk — wrong appointment in shared household |
| Mandatory token in every message | Patient friction; unnecessary for 99% of single-open cases |
| Time-window only (no token) | Delayed reply can confirm wrong appointment when sequential confirmations overlap |
| Hybrid: time-window primary + last-2-digits-of-appointment_id token on conflict (chosen) | Minimal friction; safe; deterministic; no token storage needed |
| Epic-style short link with embedded token | Better UX; deferred to future enhancement |

### Token Design Detail

- **Token value:** Last 2 digits of `appointment_id` (EMR-assigned numeric ID). No separate token field needed — derived at match time.
- **Token collision:** Two open confirmations where `appointment_id` ends in same 2 digits (~1% probability) → work queue fallback. Safe.
- **No token storage:** Token is derived from existing `appointment_id` field on the `outbound_sms_log` record. No new column required.
- **EMR ID format assumption:** Token approach assumes numeric appointment IDs. Confirm with target EMR systems (Epic typically uses numeric IDs).

### Consequences

- `outbound_sms_log` does not require a dedicated `reply_token` column.
- At INSERT time: check for open pending confirmations on same phone; include 2-digit token in message body if conflict detected.
- Confirmation records expire at appointment date/time.
- `inbound_sms_ambiguous_match` work queue handles zero-match, multi-match, and token-collision cases.
- FIFO match is prohibited by code review and test enforcement.
- REQ-SMS-01 through REQ-SMS-08 updated to reflect hybrid matching.

---

## ADR-007: Central Watchdog with Heartbeat, Fencing, and Persisted Recipient Operations

**Status:** Proposed — Notifyre idempotency is a production blocker for automatic takeover  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead  
**Review trigger:** Duplicate patient communication confirmed; watchdog false-positive; Notifyre idempotency verified  
**Related requirements:** REQ-BATCH-08, REQ-BATCH-16 through REQ-BATCH-18

### Context

A batch job crashing mid-run leaves `batch_job_runs` stuck in `running`. A watchdog using only elapsed time may mark a slow-but-healthy job failed. Fencing via `run_attempt` reduces the duplicate-send risk during takeover but cannot fully close the race window between a worker passing its fence check and Notifyre accepting the in-flight HTTP request. Provider-side idempotency is the primary defence against duplicates in that window.

### Decision

**Watchdog mechanism:** Central watchdog job (control-plane role) using heartbeat + lease + fencing token. Before each send, the batch worker verifies: run status = `running` (or `recovering` during takeover), lease belongs to this worker, lease unexpired, `run_attempt` matches. Any mismatch → abort immediately.

**Heartbeat cadence:** Updated on a timer (not only at major steps) and at each recipient send, because one processing step may run for a long time.

**Persisted recipient-operation rows:**  
A `batch_recipient_operations` table (tenant schema) records one row per logical send before the first send attempt:

```
batch_recipient_operations
- operation_id         (UUID; generated once; reused across all retries/takeovers)
- batch_job_run_id     (FK to batch_job_runs)
- send_type            (appointment_reminder / care_gap_outreach)
- patient_id
- appointment_id       (nullable — appointment reminders only)
- campaign_id          (nullable — care-gap outreach only)
- scheduled_date       (nullable — care-gap outreach only)
- status               (pending / submitted / delivered / failed / indeterminate)
- created_at
- updated_at
```

**Unique constraints (two partial indexes — no nullable ambiguity):**
```sql
-- Appointment reminders
UNIQUE (batch_job_run_id, patient_id, appointment_id)
  WHERE appointment_id IS NOT NULL

-- Care-gap outreach
UNIQUE (batch_job_run_id, patient_id, campaign_id, scheduled_date)
  WHERE campaign_id IS NOT NULL
```

**Idempotency key:** `operation_id` (UUID) is passed **directly** to Notifyre as the idempotency key. No HMAC derivation needed — `operation_id` is already persisted, stable across retries and takeovers, and unique by construction. The prose describing HMAC derivation is superseded by this decision.

**Key rotation note:** If the idempotency key mechanism ever changes, the persisted `operation_id` remains stable — rotation of secrets or algorithms does not change the key for an existing in-flight operation.

**Logical communication identity for care-gap:** patient + campaign + scheduled_date. A new outreach (new batch run, same patient/campaign/date) creates a new `batch_recipient_operations` row with a new `operation_id`. Retries within the same run reuse the existing row and `operation_id`.

**Watchdog takeover state machine:**

**Path A — Notifyre idempotency available (verified):**
1. Watchdog detects stale run (heartbeat expired + grace period elapsed)
2. Atomic conditional update: `running` → `recovering` + increment `run_attempt` + transfer lease to replacement worker identity
3. Replacement worker sees `status = recovering` → resets its own lease → transitions `recovering` → `running`
4. Replacement worker reuses persisted `operation_id` per recipient; Notifyre deduplicates any in-flight duplicate from old worker

**Path B — Notifyre idempotency unavailable:**
1. Watchdog detects stale run
2. Atomic conditional update: `running` → `failed` + increment `run_attempt`
3. Mark uncertain `batch_recipient_operations` rows (those with `status = pending` or `submitted`) as `indeterminate`
4. Do NOT start replacement worker
5. Create staff work queue item: "Batch run interrupted — manual verification required before resending"

**`recovering` is a valid non-terminal `batch_job_runs` status** — added to `ref_job_run_statuses`. Replacement worker must accept `recovering` as a valid state to proceed.

**Notifyre idempotency (verification item 3) is a production blocker for Path A (automatic takeover).** Path B (manual reconciliation) is acceptable for initial launch only if clinic batch volumes are small enough for staff to manage.

**Acknowledged race window:** The window between a worker passing its fence check and Notifyre accepting the in-flight HTTP request cannot be fully closed without a distributed lock per send. The per-recipient `operation_id` idempotency key is the best available mitigation. Residual duplicate-send rate must be monitored as a patient-safety metric.

Watchdog itself is monitored — alert fires if watchdog has not run within its expected schedule window.

### Consequences

- `batch_job_runs` gains: `last_heartbeat_at`, `expected_completion_at`, `lease_owner`, `lease_expires_at`, `run_attempt`.
- `batch_recipient_operations` provisioned in tenant schema with two partial unique indexes.
- `ref_job_run_statuses` gains `recovering` as a valid non-terminal state.
- `ref_recipient_operation_statuses` provisioned as a separate reference domain for `batch_recipient_operations.status`.
- `operation_id` passed directly to Notifyre as idempotency key.
- Watchdog runs under control-plane role; produces audited access records.
- Duplicate send rate monitored as a key production safety metric.
- Notifyre verification item 3 drives the go/no-go decision on automatic takeover.

---

## ADR-008: Per-Tenant GCS Service Accounts with Credential Broker

**Status:** Proposed — credential broker design required before production  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead + Security/Privacy Risk Owner  
**Review trigger:** Cross-tenant GCS access incident; production launch  
**Related requirements:** REQ-NFR-13 (revised), REQ-NFR-15 (revised), REQ-NFR-18, REQ-FAX-06

### Context

GCS fax documents are the highest-sensitivity PHI in the system. A single shared service account with access to all tenant prefixes provides no runtime enforcement. Per-tenant service accounts reduce blast radius but do not by themselves prevent wrong-tenant impersonation — if the base Cloud Run SA can impersonate all tenant SAs, selecting the wrong SA still succeeds silently.

### Threat Model

**Threat in scope: accidental cross-tenant selection** — a code bug or wrong variable selects the wrong tenant's service account. The credential broker prevents this by maintaining an authoritative `tenant_id → service_account` mapping and rejecting mismatched requests.

**Threat out of scope (separate security problem): compromised application runtime** — an attacker with code execution on the Cloud Run instance can request any tenant's credentials regardless of broker controls, since the application is the trusted caller. Mitigations for this threat are layered separately (see below) and do not change the broker architecture.

### Decision

**Target architecture (pre-production requirement):** A thin **credential broker** enforces the `tenant_id → service_account` mapping:

1. Application resolves tenant context and requests a GCS credential from the broker, supplying only `tenant_id` (not the target service account — the broker performs the mapping internally)
2. Broker looks up the registered service account for that `tenant_id` in its allowlist
3. Broker calls `generateAccessToken` internally and returns a short-lived, prefix-scoped token
4. Wrong `tenant_id` (not in registry) or broker unavailable → rejection + audit log entry

Only the credential broker holds `roles/iam.serviceAccountTokenCreator`. Application code has no direct impersonation rights. Token caches partitioned by `(tenant_id, scope)` — no cross-tenant cache sharing.

**Honest boundary statement:** The broker prevents accidental wrong-SA selection (code bug path) and centralises token audit/issuance. It does not prevent a fully compromised application runtime from requesting a different `tenant_id` — that is a separate threat handled by runtime controls below.

**Mitigations for compromised runtime (layered separately):**
- All broker requests audit-logged with `tenant_id`, timestamp, Cloud Run instance ID — anomaly detection alerts on unusual cross-tenant credential request patterns
- Short-lived tokens (15–30 min TTL) limit exposure window
- Per-tenant SAs limit blast radius to one tenant's data per stolen token
- Cloud Run workload identity (OIDC) verification at broker — confirms caller is the legitimate Cloud Run service, not an external process impersonating it
- Full runtime compromise treated as an incident response problem; per-tenant Cloud Run workloads deferred as a future enhancement if threat model escalates

**REQ-NFR-15 update:** The ordinary Cloud Run service account must not have direct GCS object creator/viewer access. Only the credential broker holds storage access permissions. The Cloud Run SA's GCS permissions are removed once the broker is in place.

**Interim controls (before broker is built):**
- Per-tenant service accounts provisioned (reduces blast radius)
- Strict code review on all GCS path construction
- Negative cross-tenant integration tests in CI
- Audit logging on all impersonation calls

**Tenant deprovisioning lifecycle:**
- On offboarding: revoke tenant SA IAM access immediately
- Data deletion: only after all of the following — approved retention window elapsed, no active legal holds, no quarantine, verified durable EMR attachment for all processed faxes, backup obligations met, contractual disposition requirements satisfied

**Object naming:** `{schema_key}/{uuid}.{ext}` — no PHI in key.  
**Checksum:** Stored as immutable GCS object metadata (`x-goog-meta-sha256`) — not in the object key name.

### Options Considered

| Option | Assessment |
|---|---|
| Single shared SA + per-tenant prefix | No runtime enforcement — wrong prefix access silently succeeds |
| Per-tenant SAs, no broker | Reduces blast radius; does not prevent wrong-SA selection |
| Per-tenant SAs + credential broker (chosen) | Prevents accidental wrong-SA selection; audited; centralised token issuance |
| Per-tenant GCS buckets | Strongest isolation; prohibitive at tenant scale |

### Consequences

- Credential broker is a required pre-production component.
- Broker performs `tenant_id → service_account` mapping internally; caller supplies only `tenant_id`.
- Only broker holds `serviceAccountTokenCreator`.
- Cloud Run SA's direct GCS access removed when broker is live.
- Tenant provisioning creates SA + registers mapping in broker allowlist.
- All broker requests logged in global audit log.
- Object metadata carries `sha256` checksum.
- REQ-NFR-13 revised: checksum in metadata, not in key name.
- REQ-NFR-15 revised: Cloud Run SA has no direct GCS access post-broker.

---

## ADR-009: Notifyre Capability Verification and Fallback Behaviour

**Status:** Proposed — pending Notifyre verification; items 1 and 3 are production blockers  
**Date:** 2026-08-02  
**Decision owner:** HaloVox Engineering Lead + Compliance/Clinical Ops (consent items)  
**Review trigger:** Any Notifyre verification item resolved; production launch  
**Related requirements:** REQ-CONSENT-02, REQ-CONSENT-07, REQ-EVT-10, REQ-EVT-11, REQ-ROUTER-02

### Context

Six Notifyre capabilities are architecturally significant but unverified. Items 1 and 3 are production blockers. Items 4 and 6 have been assessed and accepted as a known constraint with compliance sign-off (see below).

### Verification Items and Fallbacks

| # | Question | Blocker? | If supported | If not supported |
|---|---|---|---|---|
| 1 | Do STOP/START replies trigger "SMS Received" webhook? | **Yes — consent blocker** | Inbound router catches STOP/START; records consent immediately | Requires documented Notifyre suppression-query API or verified suppression status code. Do **not** infer opt-out from generic delivery failure. Without verified suppression signal, STOP handling is a **production blocker** |
| 2 | Do callbacks include `occurred_at` timestamp and/or client-supplied reference ID echoed? | No | Use provider timestamp for ordering; client reference ID for registry recovery | Enforce terminal-state precedence only; conflicting terminals → `delivery_conflict_requires_reconciliation` + staff work item |
| 3 | Does send API support idempotency keys? | **Yes — watchdog takeover blocker** | Pass `operation_id` directly; reconciler queries by key; watchdog Path A (automatic takeover) safe | Watchdog Path B: fence old worker, mark in-flight recipients `indeterminate`, no automatic replacement send, manual staff reconciliation required |
| 4 | What is the scope of STOP account-wide suppression? | **No — accepted constraint** | Design explicit cross-tenant handling if needed | Account-wide suppression across all HaloVox clinics on same Notifyre account is an accepted known constraint. `sms_consent_events` remains authoritative for HaloFlow sending decisions. Clinics informed operationally. Compliance sign-off required. |
| 5 | Do callbacks include the clinic's FROM number? | No | FROM number as additional validation in registry lookup | Path B (`provider_message_registry`) is already the default — no fallback needed |
| 6 | Does Notifyre support subaccounts? | **No — accepted constraint** | Adopt per-tenant subaccounts (preferred) | Account-level HMAC and suppression accepted as constraint (see item 4). Compliance sign-off required. |

**On consent inference:** Do not create an `sms_consent_events` opt-out record from an ambiguous delivery failure. A generic send failure is not consent withdrawal. A consent record requires a specific, documented Notifyre suppression status code or a verified suppression-query API response.

**On items 4 and 6 (account-wide suppression):** If Notifyre suppresses a phone number account-wide when any clinic's patient sends STOP, and subaccounts/sender-scoped suppression are unavailable, HaloFlow cannot independently deliver SMS to that number for other clinics. This is an accepted business constraint — clinics are informed operationally that a patient STOP with any HaloVox clinic suppresses SMS delivery account-wide. Compliance sign-off must be documented before production launch.

**On missing `occurred_at`:** Use `created_at` for ordering non-conflicting sequential events. Two conflicting terminal events without reliable timestamps → `delivery_conflict_requires_reconciliation` + one idempotent staff work item. Source events preserved append-only.

**ADR-009 remains Proposed** until all six verification items are resolved and production design is confirmed.

---

## ADR-011: Event and Operation Foundation — Schema, Identity, Projection, and Fingerprint Semantics

**Status:** Accepted
**Date:** 2026-08-30
**Decision owner:** HaloVox Engineering Lead
**Supersedes:** portions of ADR-003 and ADR-005 (see Supersession Record)
**Review trigger:** Provider capability evidence (OI-005) changes; fingerprint or canonicalisation defect; projection live-vs-rebuild divergence; tenant-sweep or replay bound breach (OI-009)
**Related requirements:** M02-FR-001 through M02-FR-035; M02-NFR-001 through M02-NFR-010

### Context

M02 refines two accepted decisions materially enough that they cannot stand unamended. ADR-003 defined a
three-level external-operation model with a stable `operation_id`; ADR-005 defined indeterminate state,
the reconciliation lifecycle, and append-only projection. Both were written before the M02 requirements
worked through inbound observations, corrections, conflicting terminal evidence, multi-tenant control-row
binding, and deterministic replay. M02 requirements §1 therefore made requirements approval conditional on
drafting this ADR, and design §19 records ADR-011 as an open external gate before schema freeze.

A status note, because it affects how this ADR is framed. The canonical ADR file still stamps ADR-003,
ADR-005 and ADR-007 as **Proposed**, while decision D4 records ADRs v4 as **Accepted** on 2 August 2026.
The ADR process note in that file permits free revision of a Proposed ADR, which would make supersession
unnecessary — but the M02 requirements treat those ADRs as accepted and explicitly require supersession.
This ADR takes the conservative path and supersedes rather than edits. Applying the D4 acceptance stamps
to the canonical record remains a separate Module 0 action and is **not** discharged here.

Three further constraints shaped these decisions:

- The merged M01 implementation is stricter than the M02 design assumed. Its statement catalogue rejects any
  SQL naming `shared.`, its gateway pins `search_path` to `pg_catalog, <tenant_schema>` and asserts it before
  and after every callback, and a `TenantContext` today carries exactly one capability.
- No tenant-schema provisioner exists yet; migration `001` creates only the `shared` schema.
- Cloud KMS is not wired into the repository, and OI-009 has not set its objectives.

### Decision

#### D-11.1 — The redundant `idempotency_key` column is removed

`patient_events` does not carry an `idempotency_key` column. ADR-007 already establishes that the persisted
`operation_id` UUID is passed **directly** to Notifyre as the idempotency key with no derivation. A second
column holding the same value can drift from the value actually transmitted, and FR-004 requires the
transmitted key to equal the stored `operation_id` byte for byte across retry, restart, deployment and worker
takeover. One value, one column, one source.

#### D-11.2 — `submission_level` becomes `event_level`, with six levels

The column is named `event_level`, and the controlled set is:

`intent`, `submission`, `delivery`, `business_outcome`, `observation`, `correction`

ADR-003's three levels could not represent facts that ADR-003 itself carved out of the intent/outcome
pattern. Inbound observations were defined as "a single append-only completed event ... no intent record",
which left them with no level to occupy; corrections and verified domain outcomes had the same problem. The
name `submission_level` also described only one of the levels it enumerated.

Consequence for ADR-005: its first-time reconciliation anti-join predicates on
`submission_level IN ('submission', 'delivery')` and is restated in D-11.11 against `event_level` and the
`listStaleIntents` contract.

#### D-11.3 — `append_sequence`: a total-order tiebreaker, and nothing else

`patient_events.append_sequence` is `bigint GENERATED ALWAYS AS IDENTITY`, unique within the tenant schema,
database-assigned.

It **is** the final total-order tiebreaker when projecting a complete visible event set, after trustworthy
`occurred_at` and contract status precedence.

It is **not** commit order, **not** a global cursor, **not** a resumable replay watermark, and **not**
tamper evidence. Two facts make those uses unsound: transactions commit out of allocation order, so a scan
up to a high-water mark can miss a lower sequence that committed later; and rolled-back transactions consume
sequence values, so gaps are normal and a gap alone is never evidence of tampering (NFR-010).

This is the direct reason replay is bounded and full-range (D-11.9).

#### D-11.4 — Canonical acceptance binding, with competing identities retained

ADR-003 stated "at most one provider acceptance identity per `operation_id` (DB constraint)". That is
refined, because it assumed provider identities are globally unique and they are not.

- Canonical binding lives in its own tenant-local table, `operation_acceptance_bindings`, with
  `PK(operation_id)`. This constraint **always** applies: one canonical acceptance identity per operation.
- Uniqueness of a provider identity **across** operations is enforced only when the versioned
  `provider_capabilities` row declares `uniqueness_scope = account_namespace`. Only then is the partial
  unique index on `(provider, provider_account, identity_namespace, external_id)` active. Absent that
  declaration, no cross-operation uniqueness is inferred.
- A second, distinct verified identity never replaces the canonical binding and is never discarded. It is
  appended as `{action}_acceptance_conflict` with stored status `conflict`, projects the non-terminal
  `acceptance_conflict_requires_reconciliation`, and enqueues a crash-durable M05 handoff.

#### D-11.5 — Shared-to-tenant references are verified application references, never foreign keys

ADR-005 declared `reconciliation_cases.resolution_event_id` as "FK to patient_events" and `work_queue_id` as
"FK". Neither is expressible: a row in the single `shared` schema cannot carry a real foreign key into one of
N tenant schemas. Declaring it invites a false sense of integrity that the database never enforces.

Every shared control row derived from tenant evidence therefore stores `tenant_id` **plus** the target
identifier, and the application resolver verifies the pair against the resolved tenant schema at write time
and again at resolution time. No cross-schema foreign key is declared.

#### D-11.6 — Mandatory `tenant_id`; one reconciliation case per tenant operation

ADR-005 keyed `reconciliation_cases` on `operation_id (unique)` with no tenant column. That is unsafe in a
schema-per-tenant deployment: an `operation_id` collision or a forged identifier crosses a tenant boundary in
a shared table.

`shared.reconciliation_cases` carries mandatory `tenant_id`, written from the trusted M01 tenant context and
**never** from caller input, with `UNIQUE (tenant_id, operation_id)` — one case per tenant operation.
M05 handoff dedupe keys are `tenant_id + operation_id + handoff_kind`, so distinct conflict and failure work
can coexist while each kind converges to at most one effective item.

#### D-11.7 — Unknown and conflict are different things, and status has a scope

ADR-003 defined `{action}_delivery_indeterminate` for "conflicting or unresolvable callbacks", conflating two
states with opposite handling: an outcome nobody knows yet, and two verified outcomes that disagree. The
first may resolve itself through a provider lookup; the second requires human adjudication and must never be
silently collapsed.

- **Unknown:** event type `{action}_submission_indeterminate`, stored status `indeterminate`, non-terminal.
  Routes to M06 reconciliation. M02 never resends automatically.
- **Conflict:** event types `{action}_acceptance_conflict` and `{action}_delivery_conflict`, both carrying
  stored status `conflict`. Both source facts are retained.
- **Projection-only statuses:** `acceptance_conflict_requires_reconciliation` and
  `delivery_conflict_requires_reconciliation`. Explicitly non-terminal; resolvable only by authorised
  correction or manual resolution under FR-035.

Every status declares `status_scope ∈ {stored_event, projection_only, both}`. Event inserts accept only
`stored_event` or `both`; a projection-only status in an insert is rejected by the database.

`ADR-003`'s `{action}_delivery_indeterminate` is retired. Reference codes are retired by `is_active = false`,
never deleted (D-11.14).

#### D-11.8 — Fingerprint canonicalisation (new; closes readiness-review C1)

Two digests exist, and both are worthless unless the canonical byte string is pinned. Without this, two
releases disagree and every equivalent retry becomes a spurious `DEDUPE_CONFLICT`.

- `business_key_fingerprint` = HMAC-SHA-256, keyed per tenant and key version, over the canonical
  serialisation of the owning module's business key.
- `content_fingerprint` = SHA-256 over the canonical serialisation of the event's immutable fields, its
  typed links, and its metadata.

**Canonical serialisation, version `v1`:**

1. **Base encoding** is RFC 8785 JSON Canonicalization Scheme (JCS): UTF-8, object keys sorted by UTF-16 code
   unit, no insignificant whitespace.
2. **Digest input is the validated, normalised representation** — computed *after* contract validation and
   metadata allowlisting, never over the wire payload. A field the contract does not allow cannot influence
   a digest.
3. **Absent and null are identical**, applied *after* contract validation and normalisation. A key whose
   value is null is omitted, exactly as an absent key is. Preserving the distinction invites encoder drift.
   Because this collapses the two forms irreversibly, an event contract **must not assign different
   semantics to null and absent** under fingerprint version `v1`. A contract needing that distinction must
   model it explicitly — for example a controlled enum value such as `"unknown"` — rather than relying on
   the shape of the JSON. CI enforces this: a contract metadata schema that admits an explicit null for a
   field it also permits to be absent is rejected at startup and in the contract-lint check.
4. **Strings** are Unicode NFC.
5. **Timestamps** are RFC 3339, UTC, `Z` suffix, exactly six fractional digits, **truncated** never rounded —
   matching PostgreSQL `timestamptz` microsecond resolution, so a value survives a database round trip
   unchanged.
6. **UUIDs** are canonical lowercase hyphenated form.
7. **Numbers** in fingerprinted fields are integers only. Floating-point values are prohibited: JCS number
   formatting is ECMAScript-derived and a known source of cross-language disagreement. Any decimal quantity
   is carried as a **controlled canonical decimal string**, whose form is pinned here — otherwise the drift
   problem simply moves down one level:
   - optional single leading `-` for negatives; never `+`; zero is `"0"`, never `"-0"`;
   - at least one integer digit, with no leading zeros other than a single `0`;
   - the fractional part carries **exactly** the scale declared by the event contract for that field, zero
     padded if needed, so `1.50` at scale 2 is preserved and never normalised to `1.5`;
   - no exponent notation, no thousands separators, no surrounding whitespace;
   - a value that cannot be represented at the contract's declared scale without loss is rejected at
     validation rather than silently rounded.
8. **Enumerated codes and booleans** use their controlled lowercase forms.
9. **Links** are serialised as an array sorted by `(link_type, link_id, relationship)`, each element a
   canonical object of exactly those three fields.
10. **Domain separation.** The digest input is prefixed with a versioned ASCII label and a newline —
    `haloflow.m02.content-fingerprint.v1\n` or `haloflow.m02.business-key.v1\n` — so a content digest and a
    business-key digest over identical material can never collide.
11. **Versioning.** `fingerprint_algorithm_version` is pinned by the event contract version. Changing any
    rule above requires a new version; historical rows retain the version they were written under and are
    never re-digested.

**Required evidence:** a committed set of golden vectors — input object, canonical byte string, hex digest —
exercised by a unit test. A canonicalisation change that does not also change the version fails that test.

#### D-11.9 — Pilot replay is bounded, full-range, and restart-from-the-top

A replay or projection rebuild declares one `tenant_id`, one projection version, and one bounded
operation/range selector. It takes a consistent database snapshot, recomputes every operation in the range,
and validates counts and source fingerprints. **An interrupted run restarts the entire declared range**;
`append_sequence` is not used to resume (D-11.3). Replay runs under an identity with no provider-adapter
capability and compares handoffs in dry-run form, so it can neither call a provider nor multiply downstream
work. Architecture review is triggered when a tenant rebuild exceeds the OI-009 wall-clock objective or
event-count bound.

#### D-11.10 — Operation identity and business-key version control (design D-02; approved B7 scope)

`operation_registry` is insert-only, one row per logical operation, with
`UNIQUE (owner_service, action_code, business_key_version, business_key_fingerprint)`.

Exactly one `active_write_version` accepts new rows; retained versions are lookup-only and must each have a
recoverable key envelope. `m02_begin_key_scope` holds `FOR KEY SHARE` on the control row while returning the
active and retained version identifiers; `m02_rotate_key_version` holds `FOR UPDATE`, so a version flip waits
for in-flight writers and no old-version write races a new-version write.

**Scope for v1, per the approved B7 decision:** the `KeyProvider` abstraction, HMAC fingerprinting,
`operation_key_versions`, and persisted key-version provenance ship in v1, together with an explicitly
non-production `LocalDevKeyProvider`. Production configuration **fails closed** unless a production-capable
provider is configured. Cloud KMS envelope integration, rotation orchestration, `m02_rotate_key_version`
execution, and producer heartbeat machinery are deferred to OI-009 follow-up work. The v1 schema carries the
version and provenance columns so that deferral needs no breaking migration. An unkeyed deterministic hash is
**not** an acceptable substitute for the HMAC.

#### D-11.11 — Hybrid projection with a per-operation critical section (design D-04)

Current state is produced by one pure, versioned `ProjectionEngine` folding a complete visible event set. A
derived `operation_projections` cache is refreshed in the same tenant transaction as the append and is
rebuildable; it is never evidence and never authorises external replay.

Every operation-scoped append calls `m02_lock_operation(operation_id)` before dedupe resolution, insertion,
conflict detection or projection work. The lock targets the `operation_registry` row because that row always
exists. Transactions touching several operations acquire locks in ascending `operation_id` byte order.

An append with a null `operation_id` is **exempt**: there is no registry row and no cross-fact operation
conflict to serialise. Such observations remain authoritative in `patient_events` and `patient_event_links`,
appear in the subject timeline, and create no `operation_projections` row.

The stale-intent evidence contract (`listStaleIntents`) replaces ADR-005's inline anti-join. It reads
authoritative events, not cache state, anti-joins committed intents against every qualifying submission fact,
and is served by the named partial index `idx_patient_events_stale_intent_scan`. M02 owns the evidence query;
M06 owns detector scheduling and execution.

#### D-11.12 — Tenant-local transactional outbox for control handoffs (design D-05)

`event_handoff_outbox` is tenant-local and commits atomically with its source event. In-process state alone
never satisfies at-least-once delivery. Deterministic dedupe keys: M05 `tenant + operation + kind`;
M06 `tenant + operation`; M11 `tenant + source event + signal type`. Dispatchers enumerate tenants in a paged,
fair round-robin sweep with bounded per-tenant claims. Consumers persist or return the effective control
record before acknowledgement; a crash before the delivered mark repeats delivery and converges at the
consumer.

#### D-11.13 — Two producer dedupe forms plus event-id retry (design D-06)

- Partial `UNIQUE (producer_service, source_namespace, source_event_id)` where `source_event_id` is not null.
- Partial `UNIQUE (producer_service, producer_dedupe_key)` where `producer_dedupe_key` is not null.
- `event_id` reuse covers commit-ambiguity retry.

A duplicate key is success **only** when `content_fingerprint` matches. A mismatch raises `DEDUPE_CONFLICT`,
retains structural attempt evidence outside patient data, and is investigated. This is what lets equivalent
retries collapse while independently sourced conflicting provider facts both survive.

#### D-11.14 — Reference-controlled vocabulary; no PostgreSQL ENUM (design D-08)

Event types, statuses, contracts and provider capabilities are shared reference/control data, stored as
stable lowercase `varchar` codes validated against reference tables. Native PostgreSQL `ENUM` types are not
used, because additive rollout, retirement and zero-downtime compatibility all require adding and retiring
codes without a type-altering migration. Codes are retired by `is_active = false`, never deleted.

#### D-11.15 — No cryptographic hash chain in v1 (design D-09)

Evidence integrity in v1 rests on append-only grants, separated privileged identities, database auditing,
point-in-time recovery, retention-locked signed exports, and automated integrity reconciliation. A
cryptographic event hash chain is **not** implemented.

This is approved for the v1 baseline only, and is revalidated before pilot if policy changes. If a chain is
later required, FR-026 lawful archival and destruction must still be reconcilable with verifiable chain
continuity or tombstone evidence — that interaction is unresolved and is why the chain is not being adopted
speculatively.

#### D-11.16 — Reference data is read on the control plane and carried as an immutable snapshot

M01's tenant SQL boundary is preserved: **no `shared.*` SQL executes on a tenant connection.** The
ContractRegistry loads reference and contract data over the M01 control-plane connection **before** the
tenant transaction opens, and the resulting immutable, versioned snapshot is carried in the operation context
for the life of that transaction.

For reproducibility and audit, the snapshot identity in force at append time is recorded. This requires a
field that design v0.3 §4.3 does not have: **`patient_events.reference_snapshot_generation bigint NOT NULL`**,
a monotonic generation of the shared reference/contract control set. It is structural and PHI-free, and it
participates in no business identity, dedupe, or projection semantics.

Extending M01's search path to make shared data readable inside the tenant transaction is reconsidered only
if M02 later demonstrates a hard requirement for database-level atomic consistency between shared and tenant
state. Reading a snapshot slightly ahead of the transaction is an accepted trade for keeping the isolation
boundary intact.

**Snapshot acquisition protocol (approved 2026-08-30).**

*Ledger and validity.* An append-only `shared.reference_catalog_generations` ledger carries
`generation bigint PRIMARY KEY`, the canonical catalogue digest, activation timestamp, controlled actor, and
compatibility metadata. One locked control row identifies `current_generation`. Every snapshot-governed
reference version carries `valid_from_generation` and a nullable, exclusive `valid_to_generation`. The
validity predicate is pinned exactly, to remove any off-by-one reading:

    valid_from_generation <= G AND (valid_to_generation IS NULL OR G < valid_to_generation)

*Publication.* A controlled publisher runs one transaction: lock the current-generation row; allocate the next
generation; write new versions and retirement boundaries; validate the complete candidate catalogue; compute
and record its canonical digest; advance `current_generation`; commit atomically. The publisher identity is
`haloflow_migrator` (or a dedicated `haloflow_reference_publisher`); `haloflow_runtime` holds SELECT only on
the ledger and the reference tables, consistent with design §4.5 assigning these to migrations/provisioning.

*Acquisition.* ContractRegistry uses one read-only `REPEATABLE READ` control-plane transaction: read
`current_generation`; load all rows valid at that generation; recompute and verify the ledger digest;
construct the immutable snapshot; **fail closed** on a missing row, digest mismatch, or unsupported content.

*Caching and lifetime.* Process caching is keyed by `(generation, digest)`; each new operation checks the
current-generation pointer and may reuse the immutable body only when both match. A generation superseded
after acquisition remains valid for the in-flight tenant transaction and is never swapped mid-transaction;
the event records the acquired generation. Publication preserves a compatibility window of at least the
maximum context lifetime plus transaction duration — with M01's current defaults that is the 5-minute
`context_ttl` plus the 30-second statement timeout, so changing either changes the required window.
Incompatible producers fail closed rather than silently using a different generation.

*Retention.* Historical versions and ledger entries are not deleted while any retained event or replay
evidence can reference them.

**Four clarifications added in review, because the protocol as stated leaves them ambiguous:**

1. **Published version rows are immutable.** A generation adds or retires versions; it never edits one. This
   invariant is what makes projection determinism work: a projection resolves each event's own retained
   `contract_version` and therefore gets the same answer regardless of which generation is current. Without
   it, folding fifty events spanning twelve generations would require twelve historical catalogue loads.
2. **`reference_snapshot_generation` is provenance, not a projection input.** It records what the *producer*
   saw at append time, for audit and replay reconstruction (D-11.9). It does not force the projection engine
   to load that generation.
3. **`is_active` and `valid_to_generation` are orthogonal and must not be conflated.**
   `valid_to_generation` governs *snapshot membership* — whether a row is in generation G at all.
   `is_active` (D-11.14) governs whether a code present in the snapshot accepts *new appends*. A retired-but-
   still-valid code remains readable and projectable while refusing new events. Two retirement mechanisms
   with one meaning each is workable; two with overlapping meanings is not.
4. **The catalogue digest reuses the D-11.8 canonicalisation** with its own domain-separation prefix,
   `haloflow.m02.reference-catalog.v1\n`. Introducing a second, separately-specified canonical form would
   recreate exactly the drift problem D-11.8 exists to close.

**Cost recorded for OI-009.** Every operation now performs a control-plane current-generation read, and a
cold process performs a full catalogue load plus digest verification. Together with the Cloud KMS dependency
in D-11.10, cold-start acquisition has two hard control-plane dependencies. Both belong in the OI-009
measurement set.

#### D-11.17 — SECURITY DEFINER gateways are deployed per tenant schema

`m02_lock_operation` and `m02_begin_key_scope` are installed into **every tenant schema** by the tenant
provisioner. This preserves M01's `pg_catalog, <tenant_schema>` search-path invariant and permits unqualified
runtime calls without introducing a shared executable schema.

Controls, all mandatory:

- One canonical function definition in source control; identical copies deployed.
- Ownership assigned to the dedicated NOLOGIN `haloflow_m02_lock_owner`.
- `PUBLIC` EXECUTE revoked; EXECUTE granted only where required.
- Fixed safe `search_path`, schema-qualified statements, exact-row targeting, minimum structural return.
- The integrity suite detects **definition and security-metadata drift** across tenants, comparing per tenant:
  `prosrc` digest, `proowner`, `proacl`, `prosecdef`, and `proconfig`. A function with the correct body but
  the wrong owner or a `PUBLIC` grant is still a security defect, so a body checksum alone is insufficient.

The shared-schema alternative is rejected: reducing deployment duplication is not a sufficient reason to
weaken the tenant execution boundary. Shared **reference data** is centrally owned and read through the
control plane (D-11.16); tenant **security gateways** are a tenant execution boundary deployed inside the
tenant schema. Centralising the first does not imply centralising the second.

#### D-11.18 — The four-identifier model, and the M01 changes it requires

| Identifier | Meaning | Lifetime / scope |
|---|---|---|
| `execution_id` | Caller-labelled execution scope | One execution; may span multiple tenant contexts |
| `correlation_id` | Trusted request or job-trigger chain | One request/job trigger; not durable workflow identity |
| `operation_id` | Durable business side-effect identity | Entire logical operation, across retries and takeover |
| `request_id` | Optional external/source diagnostic identifier | Source-defined |

**Rename (B5).** M01's contextual `operation_id` becomes `execution_id` — resolver input, context field,
validation and error terminology, `application_name`, tests and documentation. Canonical UUID validation and
immutable issuance are preserved. The rename extends to SQL in forward migration `002` (migration `001` is
not edited; its `downgrade()` deliberately raises):
`shared.tenant_state_history.operation_id`, `shared.access_audit_log.operation_id`, and
`shared.isolation_alerts.operation_id` are renamed to `execution_id` and retyped from `varchar(128)` to
`uuid`. Renaming only the Python field would leave `shared.access_audit_log.operation_id` holding an
execution identifier while `patient_events.operation_id` holds a business operation identifier — one column
name, two meanings, in one database. `execution_id` is caller-supplied; callers should mint one per
execution, but one execution may deliberately span several tenant contexts, as in an M06 batch fan-out.

**Correlation (B6, with the FR-031 correction).** `TenantContext` gains required `correlation_id: UUID` and
required `correlation_source ∈ {trusted_infrastructure, entry_point_generated}`. Per FR-031 the
**entry-point owner**, not `TenantResolver`, generates a cryptographically strong UUID when trusted
infrastructure has not supplied an approved one; the resolver validates and preserves the value and its
provenance and does not mint a fallback. Arbitrary public correlation strings are rejected or replaced at the
entry-point boundary and are never coerced or hashed into trusted UUIDs. `request_id` is not correlation
identity. M11 retains governance of propagation, telemetry and trusted-infrastructure policy.

**Multi-capability context (F1).** `TenantResolver.resolve(...)` accepts a requested `frozenset[str]` of
capabilities; every requested capability must be contained in `principal.capabilities`; the issued context
carries only the validated subset. This replaces singleton-capability issuance, which would have forced every
statement in an atomic M02 flow — registry insert, event insert, link insert, projection upsert, outbox
insert, lock gateway — to share one coarse capability and would have hollowed out the least-privilege claims
in FR-024 and design §13.1. Per-statement enforcement remains active inside a multi-statement flow, and a
negative test proves an unauthorised capability cannot be injected or inherited.

**Statement catalogue (B2).** M01 exposes one supported, startup-only `build_statement_catalog(...)` while
retaining the issuer sentinel internally as the mechanism. Each module owns fixed definitions in its own
`haloflow.mNN.statements`; keys are module-prefixed; bootstrap composes approved sets into one immutable
catalogue; duplicate keys fail startup; the gateway requires an explicit catalogue argument with no silent
empty default; there is no runtime registration. A module definition set contributes both its statements and
its write-capability codes, with the write-capability set preferably **derived** from WRITE statements in the
composed catalogue so the two cannot drift. CI pins the exact registered key set and a SHA-256 digest of each
normalised query; no runtime checksum. The security property is that SQL is fixed, reviewed, validated and
frozen at startup — not that a Python sentinel stays secret.

**Execution provenance in M02.** `patient_events.execution_id uuid NOT NULL` — structural, PHI-free forensic
provenance tying each append to the execution scope that wrote it. It participates in no business identity,
dedupe, or projection semantics. `operation_registry` needs no separate column: its creating execution is
represented by the associated intent event. This is an addition to design v0.3 §4.3.

`NOT NULL` is safe on every append path — M03 callback workers, the M06 reconciler and stale-intent detector,
replay tooling, and privileged correction alike — because every append executes inside a `TenantContext`, and
`execution_id` is a required field of that context (D-11.18). There is no append path that lacks one.

#### D-11.19 — Access-audit source mapping

`shared.access_audit_log.source_event_id` and `patient_events.source_event_id` are different things and must
never be joined by name.

- When a privileged audit record originates from an M02 event, `access_audit_log.source_event_id` is that M02
  `event_id` (satisfying the existing `NOT NULL UNIQUE` constraint).
- When privileged export, disposition or other activity has no patient event, the controlled writer generates
  a dedicated privileged-action UUID.
- `patient_events.source_event_id` remains a bounded provider/source deduplication identifier and is not
  mapped by name to the shared audit field.

### Options Considered

| Decision | Alternative rejected | Why |
|---|---|---|
| D-11.2 six levels | Keep three levels; model observations as intent+outcome pairs | Manufactures a synthetic intent for a fact the system never attempted; FR-009 forbids it |
| D-11.3 tiebreaker only | Use `append_sequence` as a replay watermark | Out-of-order commits make a high-water scan lossy; gaps from rollbacks would read as data loss |
| D-11.4 capability-scoped uniqueness | Global unique index on provider external ID | Two providers or accounts legitimately reuse a raw external ID; a global index would reject valid facts |
| D-11.5 verified references | Declare shared→tenant foreign keys | Not expressible across N schemas; declaring one asserts integrity the database never enforces |
| D-11.8 JCS + pinned rules | "SHA-256 over the JSON" without a canonical form | Key order, timestamp precision and float formatting all drift across releases and languages; every drift is a false `DEDUPE_CONFLICT` |
| D-11.9 full-range restart | Incremental resume from last processed sequence | Same out-of-order-commit flaw as D-11.3; a resumed rebuild can silently omit committed rows |
| D-11.15 no hash chain | Hash-chain events in v1 | Unresolved interaction with lawful destruction under FR-026; compensating controls are adequate for v1 |
| D-11.16 control-plane snapshot | Add a reference schema to M01's search path | Reopens M01's reviewed isolation invariant for a read-consistency benefit M02 does not yet need |
| D-11.17 per-tenant gateways | One copy in a shared executable schema | Requires the same search-path change; deployment convenience is not a reason to weaken the tenant execution boundary |
| D-11.18 multi-capability context | One coarse capability per M02 flow | Makes per-statement capability checks carry no information and overstates least privilege in §13.1 |

### Consequences

**Schema**

- `patient_events`: no `idempotency_key`; `event_level` replaces `submission_level`; adds `append_sequence`,
  `content_fingerprint`, `execution_id`, and `reference_snapshot_generation`.
- New tenant-local: `operation_registry`, `operation_key_versions`, `patient_event_links`,
  `operation_acceptance_bindings`, `operation_projections`, `event_handoff_outbox`.
- New shared: `ref_event_types`, `event_contracts`, `provider_capabilities`, `producer_key_capabilities`.
  `shared.reconciliation_cases` is re-specified with mandatory `tenant_id` and `UNIQUE (tenant_id, operation_id)`.
- M01 forward migration `002` renames and retypes three `shared.*` `operation_id` columns to `execution_id uuid`.

**Required corrections to the ADR file's Shared Infrastructure Table Inventory** (FR-032 requires every new
shared table to be confirmed against it before migration):

- Add rows for `ref_event_types`, `event_contracts`, `provider_capabilities`, `producer_key_capabilities`,
  and `reference_catalog_generations` (the D-11.16 ledger), each with a classification-manifest entry.
- The existing `shared.access_audit_log` row reads "Append-only; **all roles write**". That is wrong against
  the merged M01 migration, which grants INSERT only to `haloflow_audit_projector` and
  `haloflow_control_audit_writer` and explicitly revokes INSERT/UPDATE/DELETE/TRUNCATE from
  `haloflow_runtime`. **Corrected in this change set** to name those two writers, per FR-032 and because the
  inventory is the document the M01/M02 audit boundary is reviewed against.

**Code and process**

- M01 debt PR carries: tenant-schema provisioner and per-tenant migration runner; public
  `build_statement_catalog`; required catalogue argument; multi-capability issuance; the `execution_id`
  rename and migration `002`; `correlation_id` and `correlation_source` on the context.
- New Python dependency for JSON Schema validation of event metadata (FR-020), version pinned.
- CI gains: the statement manifest (keys + query digests), fingerprint golden vectors, the shared
  classification manifest entries for every new shared column, and the per-tenant definer-function drift check.

**Not discharged by this ADR**

Applying the D4 acceptance stamps to ADR-003, ADR-005 and ADR-007 remains a Module 0 action.

### Still-open items gating M02 after this ADR

| ID | Gate | Required by |
|---|---|---|
| M02-OI-005 | Notifyre idempotency, echoed client reference, callback `occurred_at`, provider lookup capability evidence | Before automated takeover |
| M02-OI-006 | Retention, legal hold, archival/export, correction and destruction authority policy | Before production pilot |
| M02-OI-007 | Core event type/status/contract seed catalogue and compatibility process | Before seed migration — **next work item** |
| M02-OI-009 | Measured append, projection, replay, backlog-age, fairness and Cloud KMS objectives | Before production readiness |
| M02-OI-010 | Revalidation of D-11.15 before pilot if policy changes | Before pilot |

### Supersession Record

| Superseded | Where | Superseded by | Nature |
|---|---|---|---|
| `idempotency_key` on `patient_events` | ADR-003 Decision (Level 1) and Consequences | D-11.1 | Removed as redundant with ADR-007 |
| `submission_level` (intent/submission/delivery) | ADR-003 Consequences; ADR-005 Consequences | D-11.2 | Renamed to `event_level`; extended to six levels |
| `{action}_delivery_indeterminate` for "conflicting or unresolvable callbacks" | ADR-003 Decision (Level 3) | D-11.7 | Split into unknown vs conflict vocabulary; type retired |
| "At most one provider acceptance identity per `operation_id` (DB constraint)" | ADR-003 Constraints | D-11.4 | Refined: `PK(operation_id)` always; cross-operation uniqueness only under declared provider capability scope |
| `reconciliation_cases` keyed on `operation_id (unique)`, no tenant column | ADR-005 Decision | D-11.6 | Mandatory `tenant_id`; `UNIQUE (tenant_id, operation_id)` |
| `resolution_event_id` / `work_queue_id` declared as foreign keys | ADR-005 Decision | D-11.5 | Replaced by tenant-bound verified application references |
| First-time reconciliation inline anti-join on `submission_level` | ADR-005 Decision | D-11.2, D-11.11 | Restated as the M02-owned `listStaleIntents` evidence contract over `event_level` |
| Projection ordering rule | ADR-005 "Current-status projection rule" | D-11.3, D-11.11 | Retained in substance; extended with `append_sequence` tiebreaker, correction graph, acceptance conflicts, and explanation codes |

**Not superseded.** ADR-007's decision that `operation_id` is passed directly to Notifyre as the idempotency
key remains controlling and is reinforced by D-11.1 and FR-004. ADR-003's three-level model for external side
effects, its `external_id` population rule, and its exclusion of inbound observations from the intent pattern
all remain correct and are extended rather than replaced.

---

## Shared Infrastructure Table Inventory

The following shared-schema tables are control-plane infrastructure — not tenant operational tables. All are in the `shared` schema unless noted.

| Table | Write role | PHI classification | Notes |
|---|---|---|---|
| `shared.tenants` | Control-plane only | No | Top-level tenant registry |
| `shared.tenant_inbound_number_registry` | Control-plane only | Low (E.164 numbers) | Inbound routing source of truth |
| `shared.provider_message_registry` | Data-plane (at send time) | No | Outbound callback routing; `operation_id` + `external_id` |
| `shared.webhook_inbox` | Webhook processor only | Yes — payload encrypted | Raw webhook payloads; application-layer encryption |
| `shared.unresolved_callback_queue` | Webhook processor only | No | Callbacks arriving before registry committed |
| `shared.reconciliation_cases` | Reconciler only | No | Mutable retry lifecycle for indeterminate operations |
| `shared.ref_event_statuses` + all other ref tables | Control-plane / migrations only | No | Read by all; written only by provisioning |
| `shared.ref_event_types` | Control-plane / migrations only | No | ADR-011 D-11.14; event type catalogue |
| `shared.ref_event_levels` | Control-plane / migrations only | No | ADR-011 D-11.2; six event levels (subject to OI-007 decision S1) |
| `shared.event_contracts` | Control-plane / migrations only | No | ADR-011 D-11.14; versioned event contracts, old versions retained |
| `shared.provider_capabilities` | Controlled integration migration | No | ADR-011 D-11.4; declares external-ID namespace and uniqueness scope |
| `shared.producer_key_capabilities` | Producer startup/heartbeat identity | No | ADR-011 D-11.10; PHI-free live producer registration |
| `shared.reference_catalog_generations` | `haloflow_migrator` / reference publisher only | No | ADR-011 D-11.16; append-only generation ledger with canonical catalogue digest |
| `shared.access_audit_log` | Append-only; `haloflow_audit_projector` and `haloflow_control_audit_writer` only | Minimal (structured fields only) | PHI access audit trail. Corrected per ADR-011: `haloflow_runtime` is explicitly revoked INSERT/UPDATE/DELETE/TRUNCATE in M01 migration 001 |
| Global security incident log | Append-only; webhook handler writes | No PHI | Failed HMAC, unroutable events |

---

## Summary Table

| ADR | Decision | Status | Production blocker? |
|---|---|---|---|
| ADR-001 | Schema-per-tenant + application-enforced isolation (Option A); Option B deferred | Proposed | Security/privacy risk-owner sign-off |
| ADR-002 | Path B (`provider_message_registry`) as default callback routing; FROM as validation | Proposed | Notifyre item 2 (client reference echo) |
| ADR-003 | Three-level event model (intent / submission / delivery) + operation_id | Proposed | — |
| ADR-004 | Async durable webhook inbox; stateful claim lifecycle; PHI-encrypted payload | Proposed | — |
| ADR-005 | Anti-join reconciler; `reconciliation_cases` table; normalized provider outcomes; projection rule | Proposed | — |
| ADR-006 | Time-window primary; conflict token = last 2 digits of appointment_id on demand; confirmations expire at appointment date/time | Proposed | Clinical risk-owner acceptance of token-free single-open path |
| ADR-007 | Central watchdog + heartbeat + fencing + `recovering` status + `batch_recipient_operations` + `operation_id` as idempotency key | Proposed | Notifyre idempotency (item 3) for automatic takeover |
| ADR-008 | Per-tenant GCS SAs + credential broker; broker holds only `tenant_id`; honest boundary documented | Proposed | Credential broker before production |
| ADR-009 | Items 1 and 3 are production blockers; items 4 and 6 accepted as known constraint with compliance sign-off | Proposed — pending verification | Items 1 and 3 |
| ADR-011 | M02 event and operation foundation: `event_level`, `append_sequence`, canonical acceptance binding, unknown-vs-conflict vocabulary, fingerprint canonicalisation, reference-catalogue generations, four-identifier model | **Accepted 2026-08-30** | OI-005, OI-006, OI-007, OI-009 gate M02 delivery |

---

*End of Architecture Decision Records — v4 (post third review)*
