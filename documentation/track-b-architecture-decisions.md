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
| `shared.access_audit_log` | Append-only; all roles write | Minimal (structured fields only) | PHI access audit trail |
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

---

*End of Architecture Decision Records — v4 (post third review)*
