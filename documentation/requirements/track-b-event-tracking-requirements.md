# HaloVox Track B — Event Tracking & Queue Management
## Requirements Document

**Version:** 2.4  
**Date:** 2026-08-02  
**Author:** HaloVox Engineering  
**Status:** Draft — Pending Review

**Change Summary v2.4:** Applied v3 ADR review decisions. Key changes: REQ-MT-02/20 language corrected to application-enforced isolation (ADR-001); REQ-ROUTER-02 duplicate handling updated — valid duplicates receive 2xx, not rejection; REQ-ROUTER-09 revised to durable inbox 2xx semantics; Section 4.2 event catalog updated to three-level naming convention; REQ-EVT-04 cross-reference fixed to REQ-EVT-10; REQ-EVT-09 event names updated; REQ-EVT-11 references reconciliation_cases table; REQ-FAX-LOG-01 `unknown_outcome` → `indeterminate`; batch model updated with `recovering` status, `batch_recipient_operations` table spec, partial unique indexes; REQ-SMS-01–04 updated to hybrid token approach (last 2 digits of appointment_id on conflict); REQ-NFR-15 updated (Cloud Run SA has no direct GCS access post-broker); shared infrastructure table inventory added; v3 reconciliation notes added.

**Change Summary v2.3:** Reconciled with ADR decisions (post second-round ADR review). Key changes: DB isolation revised from DB-enforced to application-enforced with compensating controls (ADR-001 Option A); intent-before-action scoped to external side effects only — inbound observations excluded (ADR-003); `unknown_outcome` replaced with non-terminal `indeterminate` state requiring reconciliation (ADR-005); REQ-ROUTER-09 revised to stateful dedup lifecycle with claim/lease/reclaimer (ADR-004); STOP opt-out inference from failed-send status removed — verified suppression signal required (ADR-009); outbound fax callback routing updated to reference shared registry (ADR-002); GCS checksum moved from object key to object metadata (ADR-008); reply token removed entirely — time-window matching is primary, ambiguous → work queue (ADR-006); ref_event_statuses domain added to reference tables.

**Change Summary v2.2:** Applied second-round external review findings (1–16) and editorial corrections. Key changes: shared inbound-number registry, intent-before-action event pattern, FIFO reply matching removed in favor of appointment tokens, sms_consent replaced with append-only sms_consent_events, outbound_fax_log table added, two-tier DB role model, search_path safety requirements, watchdog fencing and heartbeat, batch summary clarified as job-level aggregates, GCS tenant isolation and fax deletion requirements, access auditing requirements, Cloud SQL vs Cloud Logging payload separation.

---

## 1. Overview

Track B automates clinic communications across five use cases: appointment reminders, patient reply processing, outbound fax, inbound fax routing, and care gap outreach. The system is multi-tenant — each clinic (tenant) operates independently with its own configuration, EMR system, payer list, and phone/fax numbers. Every action taken by the system on behalf of a patient or the clinic must be recorded, and failures must be surfaced to the appropriate party for resolution.

This document covers cross-cutting infrastructure concerns: event tracking, queue management, multi-tenant architecture, batch job management, SMS consent, and reply matching.

Flow-specific requirements for Flows 1 (Appointment Reminders), 3 (Outbound Fax), and 5 (Care Gap Outreach) are maintained in separate documents. This document references them where relevant but does not duplicate their content.

This document defines requirements for:
- Multi-tenant architecture and tenant configuration
- Inbound request routing and dispatch
- Event tracking across all channels (SMS and fax)
- Inbound fax classification and routing
- Work queue for front office staff
- Error queue for failed automated processing
- Batch job management and execution tracking
- System failure alerting
- SMS consent and suppression
- Outbound SMS log and reply matching
- Outbound fax log

Deferred features are noted in the Backlog section. The current design must support those features when built.

---

## 2. Multi-Tenant Architecture

### 2.1 Purpose

HaloFlow serves multiple clinics. Each clinic is a **tenant**. Tenants are completely isolated — one tenant's data, configuration, jobs, and events must never be visible to or affect another tenant.

### 2.2 Tenant Registry

**REQ-MT-01:** The system must maintain a `tenants` table in the shared schema as the top-level tenant registry. Each row represents one clinic with a unique `tenant_id` and an immutable opaque `schema_key` used to construct the PostgreSQL schema name.

**REQ-MT-02:** Tenant data is organised using **schema-per-tenant** in PostgreSQL. Each tenant is provisioned its own PostgreSQL schema (e.g., `tenant_a1b2c3d4`). Schema names are derived from the immutable opaque `schema_key` — not from clinic names. All operational tables for that tenant reside in their schema. Isolation is enforced at the **application layer** via `SET LOCAL search_path` and compensating controls (see REQ-MT-19 and ADR-001) — not by PostgreSQL role grants. Application code must not issue tenantless queries or schema-qualified cross-tenant queries. Access to approved shared control-plane tables in the `shared` schema is permitted (see REQ-MT-04).

**REQ-MT-03:** Tenant onboarding must provision a new schema and run Alembic migrations against it. No code deploy is required to onboard a new clinic.

**REQ-MT-04:** A `shared` schema must hold all global reference tables: `tenants`, `tenant_inbound_number_registry`, `ref_event_types`, `ref_queue_types`, `ref_queue_statuses`, `ref_error_types`, `ref_fax_directions`, `ref_job_types`, `ref_job_run_statuses`, `ref_payers`, `ref_emr_systems`, `fax_types`. These are read by all tenant schemas and by control-plane processes but never written to by tenant operations.

**REQ-MT-05:** The application must set the PostgreSQL `search_path` to the resolved tenant's schema at the start of every inbound request using `SET LOCAL search_path`. All downstream queries for that request automatically target the correct tenant schema without additional filtering. See REQ-MT-22 for search_path safety requirements.

**REQ-MT-06:** A tenant-aware data access layer must reject any query that is missing a resolved tenant context. Schema names used in `search_path` must be selected only from the trusted `shared.tenants` registry — never derived from webhook payloads, request parameters, or any external input.

### 2.3 Tenant Configuration

**REQ-MT-07:** Each tenant schema must include a `tenant_config` table specifying:
- The EMR system they use (name/code + API credentials stored in GCP Secret Manager)
- The EMR practice/facility ID for that tenant
- Contact information and timezone

**REQ-MT-08:** EMR credentials must never be stored in plaintext in the database. All credentials must be stored as GCP Secret Manager references (path/name only stored in `tenant_config`).

**REQ-MT-09:** Each tenant must have one and only one EMR system configured. Multi-EMR per tenant is not supported in this version.

### 2.4 Shared Inbound Number Registry

**REQ-MT-10:** A `shared.tenant_inbound_number_registry` table must exist in the shared schema. This table maps every inbound phone/fax number to its owning tenant. It is the authoritative source for inbound routing and must be queried by the Inbound Router before any tenant schema is opened.

**REQ-MT-11:** Each record in `shared.tenant_inbound_number_registry` must include: `to_number` (E.164), `number_type` (`sms` / `fax` / `voice`), `tenant_id` (FK to `tenants`), `schema_key`, `is_active`, `activated_at`, `deactivated_at`. A number may only belong to one active tenant at a time. Numbers must be validated in E.164 format on insert.

**REQ-MT-12:** The `schema_key` in `shared.tenant_inbound_number_registry` is the sole input to the Inbound Router for constructing the tenant schema name. It must be an immutable opaque identifier — never a clinic name or user-supplied string.

**REQ-MT-13:** Inbound number records must carry activation and deactivation timestamps to support auditing of number reassignment history.

### 2.5 Tenant Payer Configuration

**REQ-MT-14:** The shared schema must maintain a `ref_payers` table containing configuration for every payer (insurance) the platform supports.

**REQ-MT-15:** Each payer in `ref_payers` must specify:
- A unique code and display name
- Eligibility check options: portal URL and/or EDI 270/271 endpoint URL
- PA request endpoint (future — field reserved now)

**REQ-MT-16:** Each tenant schema must include a `tenant_payers` join table declaring which payers the clinic accepts.

**REQ-MT-17:** A patient's specific payer(s) are stored in the EMR — HaloFlow does not replicate payer-to-patient assignments locally.

### 2.6 Universal EMR Systems Table (Reserved)

**REQ-MT-18:** The shared schema must include a `ref_emr_systems` table as a global registry of known EMR systems. Reserved for future multi-EMR routing.

### 2.7 Database Roles

**REQ-MT-19:** Two database roles must be defined at the PostgreSQL level:

| Role | Access | Usage |
|---|---|---|
| **Tenant data-plane role** | Application-enforced access to one tenant schema at a time (via `SET LOCAL search_path`), plus approved shared schema reads | All tenant-facing application requests |
| **Privileged control-plane/support role** | Can enumerate tenants via shared registry; accesses one tenant schema at a time with explicit selection; all access produces audited records | Scheduler, watchdog, schema provisioning, technical support debugging |

**Implementation note (ADR-001):** Isolation is enforced at the application layer, not by PostgreSQL authorization boundaries. `SET LOCAL search_path` provides routing convenience but is not a database-level access restriction — if the data-plane role has grants on multiple schemas, schema-qualified cross-tenant queries would succeed. The following compensating controls are required: schema names resolved exclusively from `shared.tenants`, tenant-aware data access layer that rejects queries without a resolved tenant context, negative cross-tenant integration tests as mandatory CI gates, and append-only audit log for all control-plane access. Security/privacy risk-owner sign-off is required before production launch. Per-tenant database roles (Option B) are deferred as a future security enhancement.

**REQ-MT-20:** Application code must not issue tenantless queries or schema-qualified cross-tenant queries. The data-plane role may hold grants on multiple schemas — application controls are the enforcement boundary, not database permissions. The control-plane role must still access one schema at a time with explicit selection from the tenant registry.

**REQ-MT-21:** Every use of the control-plane/support role must produce an audited access record: timestamp, actor (scheduler process / support user ID), tenant accessed, operation type. These records are stored in the global audit log outside any tenant schema.

### 2.8 search_path Safety

**REQ-MT-22:** The following `search_path` safety requirements apply to all application code:

- Schema names must be resolved exclusively from the `shared.tenants` registry — never from webhook payloads, URL parameters, or any external input
- Schema names must use proper PostgreSQL identifier quoting to prevent injection
- `SET LOCAL search_path` must be used inside a transaction so the path resets automatically on transaction end
- The approved shared schema must be explicitly included in the `search_path` alongside the tenant schema
- Connections must be fully reset (search_path cleared, transaction rolled back) before returning to the connection pool
- The test suite must include tests that alternate two different tenants on the same pooled connection and verify no context leakage

---

## 3. Inbound Router / Dispatcher

### 3.1 Purpose

Every inbound communication arrives on a specific "to" number — the clinic's Notifyre number (SMS or fax; voice calls are handled by Twilio, a separate future integration). The Inbound Router uses this number to identify the tenant, load the tenant's EMR configuration, and dispatch the request to the correct flow handler.

### 3.2 Requirements

**REQ-ROUTER-01:** Every inbound webhook (Notifyre SMS, Notifyre fax) must pass through an Inbound Router before any business logic executes.

**REQ-ROUTER-02:** HMAC signature verification must occur as the first step, before any payload inspection or tenant lookup. Requirements:
- Use the Notifyre account-level HMAC signing secret (one secret for the HaloFlow Notifyre account, configured at the webhook level)
- Perform constant-time byte comparison to prevent timing attacks
- Enforce a timestamp tolerance window (e.g., ±5 minutes) to reject stale replays
- Enforce event ID uniqueness via `shared.webhook_inbox`: a valid duplicate (same `(provider, provider_account, event_id)`, HMAC passes) receives HTTP 2xx without reprocessing — not a rejection response. Only failed HMAC verification is rejected.
- Any request failing HMAC verification must be rejected immediately and recorded in the global security incident log (see REQ-ROUTER-08)
- After rejection, the "to" field may be read from the payload for security logging purposes only — this limited post-rejection inspection is explicitly permitted

**REQ-ROUTER-03:** After HMAC verification, the Inbound Router must resolve tenant identity by querying `shared.tenant_inbound_number_registry` using the "to" field. If no active tenant record is found for the "to" number, the request must be rejected and logged as an unroutable inbound event.

**REQ-ROUTER-04:** Once tenant is resolved, the Inbound Router must set the PostgreSQL `search_path` using `SET LOCAL` to the tenant's schema, load `tenant_config`, and instantiate the correct EMR adapter for that tenant.

**REQ-ROUTER-05:** The resolved tenant context and EMR adapter must be stored in request context (alongside `correlation_id`) so all downstream modules can access them without additional DB lookups.

**REQ-ROUTER-06:** The Inbound Router must dispatch the request to the correct flow handler based on the `number_type` of the matched `shared.tenant_inbound_number_registry` record:
- `number_type = sms` → Patient Reply handler (Flow 2)
- `number_type = fax` → Inbound Fax handler (Flow 4)
- `number_type = voice` → Voice handler *(future — requires separate Twilio integration; not handled by Notifyre)*

**REQ-ROUTER-07:** For outbound flows (batch jobs) and scheduled jobs, tenant identity is established by the scheduler before the job begins — it iterates tenants via the shared registry using the control-plane role. No inbound routing is needed for these flows.

**REQ-ROUTER-08:** Invalid webhook requests (failed HMAC verification) must be recorded in a **global security incident log** outside any tenant schema. This log must capture: timestamp, source IP, last 4 digits of "to" field only (best-effort, post-rejection), rejection reason, and request hash. No PHI. No tenant assignment from an unverified payload field.

**REQ-ROUTER-09:** Duplicate webhook delivery must be handled via a **stateful dedup lifecycle** (ADR-004). The system must not simply check event IDs and return 200 — a crash-after-insert scenario would permanently silence that webhook's retries. Requirements:
- Full webhook payload must be persisted durably to `shared.webhook_inbox` before HTTP 200 is returned; 200 means "durably accepted," not "business processing completed"
- Each inbox record carries a processing claim: `status` (`received` / `processing` / `completed` / `retryable_failed`), `claim_owner`, `claim_expires_at`, `attempt_count`
- HTTP 200 without business processing is only safe for an already-`completed` record
- Failed or abandoned claims (expired `processing` status) must be reclaimable by a scheduled reclaimer — not solely dependent on provider retries
- Dedup records must be retained for the provider's maximum retry horizon (not only the signature replay window)

---

## 4. Event Tracking

### 4.1 Purpose

Every action the system takes must be recorded per patient per event. This serves three purposes:
1. **Audit trail** — HIPAA-compliant record of all PHI-touching communications
2. **Operational visibility** — staff and tech support can see what happened and when
3. **Deduplication** — system can check whether an action was already taken before repeating it

### 4.2 Scope

The following actions must generate an event record:

External side effects use the three-level intent/submission/delivery naming convention (ADR-003). Inbound observations use a single completed event type.

| Category | Direction | Level | Event Types |
|---|---|---|---|
| SMS | Outbound | Intent | `appointment_reminder_requested`, `care_gap_outreach_requested` |
| SMS | Outbound | Submission | `appointment_reminder_accepted`, `appointment_reminder_rejected`, `appointment_reminder_submission_indeterminate` |
| SMS | Outbound | Submission | `care_gap_outreach_accepted`, `care_gap_outreach_rejected`, `care_gap_outreach_submission_indeterminate` |
| SMS | Outbound | Delivery | `appointment_reminder_delivered`, `appointment_reminder_delivery_failed` |
| SMS | Outbound | Delivery | `care_gap_outreach_delivered`, `care_gap_outreach_delivery_failed` |
| SMS | Inbound | Observation | `patient_reply_received`, `sms_stop_received`, `sms_start_received` |
| Fax | Outbound | Intent | `fax_requested` |
| Fax | Outbound | Submission | `fax_accepted`, `fax_rejected`, `fax_submission_indeterminate` |
| Fax | Outbound | Delivery | `fax_delivered`, `fax_delivery_failed` |
| Fax | Inbound | Observation | `fax_received`, `fax_classified`, `fax_patient_matched`, `fax_chart_attached`, `fax_queue_routed` |
| EMR | Outbound | Intent | `emr_appointment_update_requested`, `emr_document_attach_requested` |
| EMR | Outbound | Submission | `emr_appointment_update_accepted`, `emr_appointment_update_rejected`, `emr_appointment_update_submission_indeterminate` |
| EMR | Outbound | Submission | `emr_document_attach_accepted`, `emr_document_attach_rejected`, `emr_document_attach_submission_indeterminate` |
| EMR | Outbound | Delivery | `emr_appointment_confirmed`, `emr_appointment_cancelled`, `emr_appointment_rebook_flagged`, `emr_appointment_update_failed` |
| EMR | Outbound | Delivery | `emr_document_attached`, `emr_document_attach_failed` |

Note: `patient_reply_received`, `sms_stop_received`, and `sms_start_received` are distinct event types with separate entries in `ref_event_types`.

### 4.3 Requirements

**REQ-EVT-01:** External side effects must follow the **intent-before-action** pattern (ADR-003):
1. Write a `patient_events` record with event type `{action}_requested` (the intent) **before** making any external call (Notifyre API, EMR API). Generate a stable `operation_id` (UUID v4) at this point.
2. Pass a deterministic idempotency key (derived from `operation_id`) to the external provider on the call.
3. After the call returns, write a **submission outcome** event: `{action}_accepted`, `{action}_rejected`, or `{action}_submission_indeterminate`.
4. Delivery/business outcome events (`{action}_delivered`, `{action}_delivery_failed`) are written on provider callbacks or EMR confirmation.

This ensures an audit record exists even if the Cloud Run instance is killed after the external call but before the outcome is recorded. All events for one `operation_id` are linked by that ID; current status is a derived projection — never a mutated field.

**Scope:** The intent-before-action pattern applies to **external side effects only** (Notifyre API calls, EMR API writes). Inbound observations (`fax_received`, `patient_reply_received`, `sms_stop_received`, `sms_start_received`) require a **single append-only completed event** written transactionally with the corresponding state change — no intent record is created for inbound observations.

**REQ-EVT-02:** Each event record must include: `patient_id` (nullable — see below), `event_type`, `channel`, `direction`, `status`, `external_id` (Notifyre message or fax ID where applicable), `correlation_id`, `occurred_at`, `received_at`, and any relevant sanitized metadata. `patient_id` is nullable: valid events exist with no known patient (unclassified faxes, match failures, STOP/HELP before patient identification). When `patient_id` is null, at least one subject/source reference must be present (`appointment_id`, `inbound_fax_log_id`, `outbound_sms_log_id`, or `outbound_fax_log_id`).

**REQ-EVT-03:** Events that are part of a batch job must be linked to that batch job via `batch_job_run_id`.

**REQ-EVT-04:** Event records are append-only. Status updates (e.g., delivered confirmation from webhook) write a new event rather than mutating the original. Append-only tables do not have a meaningful `updated_at` — `created_at` is set at insert time and no update is ever issued. Current status is derived by evaluating event records in `occurred_at` order, with terminal statuses taking precedence over non-terminal ones regardless of `received_at` order (see REQ-EVT-10 for out-of-order handling).

**REQ-EVT-05:** The `patient_events` DB write (intent record) must be **synchronous** — it completes before the external call is made. Cloud Logging delivery is non-blocking and may be offloaded asynchronously via the internal event bus with a defined target latency. If Cloud Logging delivery fails, the failure must be logged and swallowed — the DB record is the authoritative audit trail.

**REQ-EVT-06:** All events must be published asynchronously to GCP Cloud Logging as structured JSON within a defined target latency after the DB write. Cloud Logging entries must contain only structural fields (event type, status code, tenant ID, correlation ID, timestamp) — no error bodies and no PHI. Full sanitized operational detail (including PHI-adjacent error messages) is stored in Cloud SQL only.

**REQ-EVT-07:** Every call made to the EMR system API that modifies patient data must generate an event record — both on success and on failure. EMR action events must capture the action type, the target resource (appointment ID, document ID, etc.), the outcome, and any sanitized error detail from the EMR API response.

**REQ-EVT-08:** EMR action events must carry the same `correlation_id` as the inbound request that triggered them (see Section 15). `correlation_id` is per-request only. Business workflow linkage across separate requests (reminder sent → patient reply → EMR write) is achieved via `outbound_sms_log_id` for SMS flows or `appointment_id` from the EMR — not via a shared `correlation_id`.

**REQ-EVT-09:** For the appointment confirmation flow, the following event sequence must be visible to front office per patient: `patient_reply_received` → `emr_appointment_update_requested` → `emr_appointment_confirmed` (delivery-level, or `emr_appointment_update_failed`). These are linked via `appointment_id` and `outbound_sms_log_id`.

**REQ-EVT-10:** Provider callbacks (Notifyre delivery status webhooks) may arrive out of order. The system must use `occurred_at` (provider event timestamp) rather than `received_at` (webhook receipt time) to determine event ordering. Terminal statuses (`delivered`, `failed`) must not be overwritten by subsequently received non-terminal statuses (`queued`, `sent`). If a provider webhook does not include an `occurred_at` timestamp, this requirement is subject to the Notifyre verification action item (Section 17).

**REQ-EVT-11:** `indeterminate` is a **non-terminal** event status for events where the external call was made but the outcome could not be determined. It is **not** a terminal state — a background reconciler must periodically attempt to resolve it.

**Reconciler behaviour (ADR-005):**
- First pass: anti-join scans `{action}_requested` (intent) events that have no qualifying submission or delivery outcome AND no existing `reconciliation_cases` row for the same `operation_id`, and have exceeded the staleness threshold. On first detection: write `{action}_submission_indeterminate` and open a `reconciliation_cases` row.
- Subsequent passes: reconciler queries `reconciliation_cases` where `status = open` AND `next_attempt_at <= NOW()`. Queries provider via normalized adapter outcomes (see ADR-005).
- Provider confirms outcome → write appropriate events; mark case `resolved`
- Provider has no record → keep case `open`; increment `attempt_count`; schedule `next_attempt_at`
- Provider does not support lookup → create staff work queue item immediately; mark case `escalated`
- Max attempts exceeded → mark case `escalated`; create staff alert
- **Do not automatically resend** when outcome is indeterminate

`indeterminate` is an event status code in `ref_event_statuses` (separate domain from `ref_queue_statuses`). It must not be added to `ref_queue_statuses` or `ref_job_run_statuses`.

---

## 5. Inbound Fax Classification & Routing

### 5.1 Purpose

Inbound faxes arrive as raw PDFs. They must be classified by type, have relevant fields extracted via OCR, optionally matched to a patient, and routed to the correct destination (patient chart, work queue, or error queue).

### 5.2 Fax Type Configuration

**REQ-FAX-01:** The system must maintain a `fax_types` configuration table in the **shared schema** defining all known fax categories applicable across all tenants.

**REQ-FAX-02:** Each fax type configuration must specify:
- **Direction** — inbound, outbound, or both (`lab_result` is inbound-only; `prior_auth_request` is outbound-only)
- Whether a patient match is required (inbound only)
- Which fields must be extracted by the OCR classifier for that type
- Which fields are required to attempt a patient match
- What to do on successful patient match (auto-attach to chart or route to work queue)
- What to do on patient match failure (route to error queue or work queue)
- What to do if patient match is not required (route to work queue or discard)
- Default priority (`normal` or `urgent`)
- SLA in hours before the item escalates to `urgent`

**REQ-FAX-03:** Initial fax types must include at minimum: `referral`, `lab_result`, `prior_auth_response`, `insurance_correspondence`, `admin`, `vendor`, `unclassified`.

**REQ-FAX-04:** The classifier must run in two passes:
1. **Type identification pass** — determine fax category
2. **Field extraction pass** — extract only the fields required by that fax type's configuration

**REQ-FAX-05:** If the classifier cannot determine the fax type with sufficient confidence, it must classify the fax as `unclassified` and route to the **work queue** with `urgent` priority. This is a normal workflow outcome, not a system error — it must not be recorded in the error queue.

**REQ-FAX-06:** OCR extracted text must not be persisted in the database. Raw PDF and OCR output must be stored in GCS under the tenant's designated prefix (see REQ-NFR-18). Only structured extracted fields are stored in the database. Raw fax files must be deleted from GCS after successful OCR processing and EMR write confirmation. Files must be retained in GCS while the associated work queue or error queue item is unresolved.

**REQ-FAX-07:** Webhook signature (HMAC-SHA256) must be verified before any fax download or processing begins per REQ-ROUTER-02.

### 5.3 Patient Matching

**REQ-FAX-08:** Patient matching is required only for fax types where `requires_patient_match = true`.

**REQ-FAX-09:** The system must attempt patient match using the fields specified in `required_match_fields` for the given fax type.

**REQ-FAX-10:** A match confidence score must be recorded for every match attempt.

**REQ-FAX-11:** If a required patient match fails (no match found or confidence below threshold), the fax must be routed to the error queue with error type `inbound_fax_patient_match_failed`.

**REQ-FAX-12:** For fax types that do not require patient matching, the fax must be routed per the `no_match_required_route` field in the fax type configuration.

**REQ-FAX-13:** If patient matching returns **more than one candidate above the confidence threshold** (ambiguous match), the fax must be routed to the **work queue** — never auto-attached to any patient chart. The work queue item must include all OCR-extracted fields and all matching patient IDs with their confidence scores so staff can select the correct patient.

### 5.4 Routing Outcomes

| Condition | Destination |
|---|---|
| Patient match required + exactly one match above threshold + type = auto_attach | Patient chart (via EMR API) |
| Patient match required + exactly one match above threshold + type = work_queue | Work queue |
| Patient match required + multiple matches above threshold (ambiguous) | Work queue (with all candidates listed) |
| Patient match required + match failed | Error queue |
| Patient match not required | Work queue (or discard per config) |
| Classifier below confidence threshold | Work queue (`urgent`, `unclassified`) |
| Webhook signature invalid | Global security incident log, request rejected |

---

## 6. Work Queue

### 6.1 Purpose

The work queue holds items that require a front office staff member to take action. These are not system errors — they are normal workflow items that cannot be fully automated.

### 6.2 Requirements

**REQ-WQ-01:** The work queue must support the following item types:

| Queue Type | Trigger |
|---|---|
| `inbound_fax_needs_routing` | Fax classified but routing requires human decision |
| `inbound_fax_needs_review` | Fax is unclassified or low-confidence, needs manual review |
| `inbound_fax_ambiguous_match` | Fax matched multiple patients above threshold; staff to select |
| `outbound_fax_failed_retry` | Outbound fax delivery failed after retries; staff to retry or contact recipient |
| `batch_sms_patient_invalid_number` | Per-patient item: patient's number is invalid; staff to update contact info |
| `sms_opt_out_batch_summary` | Batch-level item: one per job run listing all opted-out appointments; staff to call patients |
| `prior_auth_response_received` | PA response fax received and classified; staff to review decision |
| `referral_received` | Referral fax received and matched to patient; staff to schedule |
| `inbound_sms_help_received` | Patient replied HELP; staff to call back with clinic contact information |
| `inbound_sms_no_match` | Inbound SMS reply could not be matched to any open reminder; staff to review |
| `inbound_sms_ambiguous_match` | Inbound SMS matched multiple open reminders; staff to select correct appointment |
| `inbound_sms_opt_out_received` | Patient replied STOP; staff to update contact preference and switch to phone call reminders |

**REQ-WQ-02:** Each work queue item must link to the originating event (patient event or inbound fax log record).

**REQ-WQ-03:** Work queue item status must be configurable per queue type — not a single fixed lifecycle. Status codes must be stable lowercase values with separate display labels. Examples:

| Queue Type Group | Status Codes |
|---|---|
| Fax review types | `pending_review` → `in_review` → `completed` |
| Approval types | `pending_review` → `pending_approval` → `approved` / `rejected` |
| Scheduling types | `pending_review` → `pending_scheduling` → `scheduled` / `declined` |
| Action types | `pending_action` → `in_progress` → `resolved` / `escalated` |

The default initial status for a new work queue item must be determined by its queue type configuration.

**REQ-WQ-04:** Work queue items must support priority: `normal` and `urgent`. Priority must escalate to `urgent` automatically if the item exceeds its SLA deadline.

**REQ-WQ-05:** `due_at` must be computed and stored on the work queue item at creation time based on the queue type's SLA configuration. It must not be derived at query time — a later SLA config change must not retroactively alter existing deadlines.

**REQ-WQ-06:** Work queue items must carry the following lifecycle fields: `assigned_to`, `assigned_at`, `started_at`, `due_at`, `resolved_at`, `escalated_at`, `resolution_code`, `staff_note`, `actor_type`, `actor_id`, `optimistic_lock_version`. These fields support RBAC enforcement and full audit history without schema changes.

**REQ-WQ-06a:** An append-only `work_queue_history` table must record every state transition, actor change, and note update on a work queue item. The main `work_queue` row retains current state for fast querying. The history table records: `work_queue_id`, `changed_at`, `changed_by` (actor type + ID), `field_changed`, `old_value`, `new_value`.

**REQ-WQ-07:** For batch SMS failures, work queue items must be created only for the appropriate failure type:
- `batch_sms_patient_invalid_number` → one item per patient with invalid/unsupported number
- `sms_opt_out_batch_summary` → one batch-level item per job run listing all opted-out patients (not a per-patient item)
- Provider rate limits, timeouts, and outages → system-level error handling, not work queue

**REQ-WQ-08:** Authorization must be enforced at the backend API layer before any UI exposes queue data. UI enforcement of role restrictions is deferred to the UI build phase. Access model: clinic staff have full access to their tenant's work queue. HaloVox technical support has read-only access via the control-plane role, which produces audited access records per REQ-MT-21.

---

## 7. Error Queue

### 7.1 Purpose

The error queue holds items where automated processing failed and requires investigation or manual remediation.

### 7.2 Requirements

**REQ-EQ-01:** The error queue must support the following error types:

| Error Type | Trigger |
|---|---|
| `inbound_fax_patient_match_failed` | Fax required patient match but no match found |
| `inbound_fax_ocr_failed` | Cloud Vision AI returned no usable text |
| `outbound_fax_delivery_failed` | Fax failed after all retries exhausted |
| `emr_attachment_failed` | Patient match succeeded but EMR API rejected the attachment |
| `batch_job_system_failure` | Entire batch job failed at system level |
| `emr_write_failed` | EMR API write failed and cannot be retried |

Note: Invalid webhook signatures are recorded in the global security incident log (REQ-ROUTER-08), not in the tenant error queue. Low-confidence fax classification routes to the work queue (REQ-FAX-05), not the error queue.

**REQ-EQ-02:** Error queue items must record: error type, source (which table and record ID), `patient_id` (nullable), error message (sanitized — no raw PHI), retry count, `assigned_to`, `assigned_at`, `actor_type`, `actor_id`, and timestamps (`created_at`, `resolved_at`).

**REQ-EQ-02a:** An append-only `error_queue_history` table must record every status transition and actor change on an error queue item, consistent with REQ-WQ-06a.

**REQ-EQ-03:** Error queue items must support a retry mechanism with retry count tracking. Max retries must be configurable per error type.

**REQ-EQ-04:** `batch_job_system_failure` errors must trigger a notification to Technical Support (see Section 9). They must also create a work queue item so front office is aware.

**REQ-EQ-05:** Error queue item status lifecycle uses stable lowercase codes: `open` → `retrying` → `resolved` / `escalated`.

---

## 8. Batch Job Management

### 8.1 Purpose

Batch jobs send communications to many patients in a single run. The system must support scheduling batch jobs per tenant, enforcing concurrency controls, tracking each execution step, and providing a per-run summary for front office review.

### 8.2 Three-Table Design

| Table | Scope | Purpose |
|---|---|---|
| `ref_job_types` | Global (shared schema) | Catalog of all available batch job types |
| `tenant_jobs` | Per-tenant schema | Scheduled jobs for a tenant — one row per job type |
| `batch_job_runs` | Per-tenant schema | Execution history — one row per actual job run |

### 8.3 Requirements

#### ref_job_types — Global Job Type Catalog

**REQ-BATCH-01:** A global `ref_job_types` table (shared schema) must define all available batch job types. Each record must specify: job type code, name, description, default cron expression, whether per-tenant config overrides are supported, and `is_active` flag.

**REQ-BATCH-02:** Initial job types must include at minimum: `appointment_reminder`, `care_gap_outreach`, and `batch_watchdog`.

#### tenant_jobs — Per-Tenant Job Scheduling

**REQ-BATCH-03:** A `tenant_jobs` table in each tenant schema must hold one record per batch job type per tenant.

**REQ-BATCH-04:** Each `tenant_jobs` record must specify:
- `job_type_code` (FK to `ref_job_types`)
- `cron_expression` — tenant-specific schedule
- `timezone` — IANA timezone string for cron evaluation
- `is_enabled`
- `concurrency_lock` — if true, a new run will not start if a prior run is still in `running` state. Enforced by atomic status check and update, not by the boolean alone.
- `notification_config` — JSONB specifying notification recipients per event type (system failure, partial failure)
- `expected_duration_minutes` — used by the watchdog to compute `expected_completion_at`
- `watchdog_grace_period_minutes` — how long after `expected_completion_at` the watchdog waits before marking a run failed
- `created_at`, `updated_at`

**REQ-BATCH-05:** Unique constraint on `(job_type_code)` per tenant schema (one config per job type per tenant).

#### batch_job_runs — Execution History

**REQ-BATCH-06:** Every batch job execution must create a `batch_job_runs` record before processing begins.

**REQ-BATCH-07:** Each `batch_job_runs` record must capture:
- `tenant_job_id` (FK to `tenant_jobs`)
- `job_type_code` (denormalized)
- `correlation_id`
- `status` — job execution status (see REQ-BATCH-08)
- `triggered_by` — `scheduled` or `manual`
- `started_at`, `completed_at`, `expected_completion_at`
- `last_heartbeat_at` — updated by the running job at regular intervals
- `lease_owner` — identifier of the process holding the run lease
- `lease_expires_at` — expiry time of the process lease
- `run_attempt` — monotonically incremented fencing token; incremented by the watchdog on each takeover
- `total_targeted` — patients targeted after consent filtering
- `sent_count`, `failed_count`, `skipped_count`
- `failure_category_counts` — JSONB summary of failure counts by category
- `failure_reason` — populated on system-level failure

**REQ-BATCH-08:** `batch_job_runs` must use its own status domain (`ref_job_run_statuses`), separate from `ref_queue_statuses`. Lifecycle: `pending` → `running` → `recovering` (watchdog takeover with idempotency) → `running` again, or `failed` / `completed` / `completed_with_errors`. `recovering` is a valid non-terminal state that the replacement worker accepts before transitioning back to `running`.

**REQ-BATCH-09:** Each individual patient communication within a batch must be recorded as a `patient_events` row linked via `batch_job_run_id`.

#### batch_recipient_operations — Persisted Recipient Identity

**REQ-BATCH-09a:** A `batch_recipient_operations` table must be maintained in each tenant schema. One row is created per logical send **before** the first send attempt. This table owns the stable `operation_id` used as the Notifyre idempotency key and enables watchdog takeover recovery.

Fields:
- `operation_id` (UUID; generated once; reused across all retries and watchdog takeovers for the same logical send)
- `batch_job_run_id` (FK to `batch_job_runs`)
- `send_type` (`appointment_reminder` / `care_gap_outreach`)
- `patient_id`
- `appointment_id` (nullable — appointment reminders only)
- `campaign_id` (nullable — care-gap outreach only)
- `scheduled_date` (nullable — care-gap outreach only)
- `status` (`pending` / `submitted` / `delivered` / `failed` / `indeterminate`) — from `ref_recipient_operation_statuses`
- `created_at`, `updated_at`

Unique constraints (partial indexes — no nullable ambiguity):
```sql
UNIQUE (batch_job_run_id, patient_id, appointment_id) WHERE appointment_id IS NOT NULL
UNIQUE (batch_job_run_id, patient_id, campaign_id, scheduled_date) WHERE campaign_id IS NOT NULL
```

**REQ-BATCH-09b:** On watchdog takeover without Notifyre idempotency: all `batch_recipient_operations` rows with `status = pending` or `submitted` for the affected run must be marked `indeterminate`. No automatic replacement send. Staff work queue item created.

**REQ-BATCH-09c:** `ref_recipient_operation_statuses` is provisioned as a separate reference domain for `batch_recipient_operations.status` — distinct from `ref_queue_statuses`, `ref_event_statuses`, and `ref_job_run_statuses`.

**REQ-BATCH-10:** On batch job completion, the `batch_job_runs` record must contain a queryable summary: `total_targeted` (count after consent filtering), `sent_count`, `failed_count`, `skipped_count`, `failure_category_counts` (JSONB breakdown by failure type). `total_targeted` reflects the send list **after** opted-out patients have been excluded. Individual patient-level detail is queryable by joining `patient_events` and `outbound_sms_log` on `batch_job_run_id`. A separate per-patient recipient results table is not required.

**REQ-BATCH-11:** Patient-level SMS failure handling must distinguish failure types:

| Failure Type | Handling |
|---|---|
| Invalid or unsupported number | `batch_sms_patient_invalid_number` work queue item (one per patient) |
| Patient opted out | Pre-filtered by `sms_consent_events` check; `sms_opt_out_batch_summary` work queue item (batch-level) |
| Provider rate limit | Automatic retry with backoff |
| Provider timeout | Retry or reconcile using provider message ID |
| Provider outage | System-level error; operational alert |
| Unknown outcome | Reconciliation before resending |
| Permanent delivery rejection | Configurable — staff action or suppress |

**REQ-BATCH-12:** If the entire batch job fails at system level, the `batch_job_runs` record must be updated to `failed` status and notifications must fire per `tenant_jobs.notification_config`.

**REQ-BATCH-13:** Batch job run history is retained in `batch_job_runs`. Individual communications are linked via `patient_events.batch_job_run_id`.

#### batch_job_run_steps — Execution Step Log

**REQ-BATCH-14:** An append-only `batch_job_run_steps` table must record the progress of each batch job run through its processing steps. Each row captures: `batch_job_run_id`, `step_code` (logical step identifier — stable across code refactoring; not a Python function name), `status` (`starting` / `in_progress` / `sending` / `success` / `failed`), `step_sequence`, and `recorded_at`. Append-only tables have no `updated_at` — `recorded_at` is set at insert time only.

**REQ-BATCH-15:** The batch job must write a step record at the start and end of each major processing step. A successful run must produce a terminal `success` step for each step.

#### Watchdog Job

**REQ-BATCH-16:** A dedicated `batch_watchdog` job must run after each batch job's `expected_completion_at` window has elapsed. The watchdog must:
1. Check for a terminal `success` step in `batch_job_run_steps` for the expected steps
2. If absent, wait `watchdog_grace_period_minutes` before acting (to allow slow-but-healthy jobs to finish)
3. After the grace period, if `last_heartbeat_at` has also expired, execute one of two atomic transitions:
   - **If Notifyre idempotency verified:** `running` → `recovering` + increment `run_attempt` + transfer lease to replacement worker. Replacement worker sees `recovering`, resets lease, transitions to `running`.
   - **If Notifyre idempotency unavailable:** `running` → `failed` + increment `run_attempt`. Mark uncertain `batch_recipient_operations` rows `indeterminate`. Do NOT start replacement worker. Create staff work queue item.
4. Fire notifications per `notification_config`

**REQ-BATCH-17:** Before each external send (Notifyre API call), the batch process must verify that its local `run_attempt` value matches the current value in `batch_job_runs`. If the watchdog has incremented `run_attempt`, the process is fenced and must abort all remaining sends immediately without retrying. A crashed batch process cannot update its own status — the watchdog is the independent component responsible for detecting and marking stale runs.

**REQ-BATCH-18:** The watchdog itself must be monitored. An alert must fire if the watchdog job has not run within its expected schedule window. The watchdog runs under the control-plane role (REQ-MT-19) and must produce audited access records.

---

## 9. System Failure Notification

### 9.1 Requirements

**REQ-NOTIF-01:** System-level batch job failures must notify Technical Support. Application code reads `tenant_jobs.notification_config` and delivers the notification. Initial default recipients are configured in `notification_config` seed data — not embedded in business logic.

**REQ-NOTIF-02:** System-level batch job failures must also notify front office via an `error_queue` record; future push notification when Message Center UI is built.

**REQ-NOTIF-03:** GCP Cloud Monitoring is used for infrastructure-level alerts only (Cloud Run instance down, Cloud SQL unreachable, disk/memory thresholds). It is not used for batch job business failures — application code owns that path to avoid duplicate notifications.

**REQ-NOTIF-04:** For fax success/failure (inbound and outbound), the system must push notifications to the front office via work queue or error queue records; future push via Message Center UI.

**REQ-NOTIF-05:** The notification mechanism must be decoupled from the job processing logic. Notification failures must not cause job processing to fail.

---

## 10. SMS Consent & Suppression

### 10.1 Purpose

HaloFlow must maintain per-tenant SMS consent records independently of Notifyre's account-level suppression. Notifyre's STOP/START handling suppresses a number account-wide — which could silently affect other tenants sharing the same Notifyre account if the same patient phone number appears across clinics. HaloFlow's `sms_consent_events` table ensures each tenant has an explicit, auditable, immutable consent history.

### 10.2 Requirements

**REQ-CONSENT-01:** Each tenant schema must include an `sms_consent_events` table as an **append-only** consent history log. Each row represents one consent state change. Fields: `sms_consent_event_id`, `to_number` (E.164), `consent_status` (`opted_in` / `opted_out`), `source` (`patient_reply` / `staff_action`), `source_reference` (webhook event ID for patient reply; staff user ID for staff action), `appointment_id` (optional — populated if the STOP was matched to a specific reminder), `recorded_at`. No rows are updated or deleted. Current consent status per number is derived from the most recent record.

**REQ-CONSENT-02:** When a patient replies STOP, the inbound router must:
1. Insert an `opted_out` record into `sms_consent_events`
2. Attempt to match the reply to an open `outbound_sms_log` record using time-window matching (for appointment linkage context)
3. Create a work queue item of type `inbound_sms_opt_out_received` notifying front office to update contact preference and switch to phone call reminders
4. Write a `sms_stop_received` patient event

**REQ-CONSENT-03:** When a patient replies START, insert an `opted_in` record into `sms_consent_events` and write a `sms_start_received` patient event. No work queue item required unless the patient has pending appointments that were skipped due to opt-out.

**REQ-CONSENT-04:** When a patient replies HELP, create a work queue item of type `inbound_sms_help_received` with the patient's phone number so staff can call back with clinic contact information. Write a `patient_reply_received` patient event.

**REQ-CONSENT-05:** Every outbound SMS batch job must derive the current consent status per number from `sms_consent_events` (most recent record) before building the send list. Opted-out numbers must be excluded. After filtering, if any patients were excluded, a single `sms_opt_out_batch_summary` work queue item must be created listing all excluded `appointment_id` values and phone numbers (last 4 digits only).

**REQ-CONSENT-06:** Notifyre handles STOP/START at the platform level (account-wide suppression). HaloFlow must not rely on Notifyre's suppression as the authoritative record — it is a secondary enforcement layer. HaloFlow's `sms_consent_events` table is authoritative for HaloFlow's sending decisions.

**REQ-CONSENT-07 [ACTION ITEMS]:**
1. Verify with Notifyre whether STOP and START replies trigger the "SMS Received" webhook. If yes, the inbound router catches them and REQ-CONSENT-02/03 apply. If no, a documented Notifyre suppression-query API or specific verified suppression status code is required — **do not infer patient opt-out from a generic delivery failure**. A generic send failure is not consent withdrawal. Without a verified suppression signal, STOP handling is a production blocker (ADR-009, verification item 1).
2. Verify the scope of Notifyre's account-level suppression — confirm whether a STOP on one tenant's number suppresses that phone number across all tenants on the same Notifyre account. If yes, define cross-tenant suppression handling (subaccounts, verified sender-scoped suppression, or global suppression registry).

**REQ-CONSENT-08:** Staff may not unilaterally override a patient-initiated STOP. A staff `opted_in` record in `sms_consent_events` is only permitted when the patient has affirmatively re-consented (e.g., called the clinic and verbally opted back in). The `source_reference` field must always contain a staff user ID for staff-initiated records, enabling audit of who made the change and when.

---

## 11. Outbound SMS Log & Reply Matching

### 11.1 Purpose

SMS does not have a native reply-to threading mechanism. When a patient replies, the carrier delivers it as a new inbound message with no reference to the original outbound message. HaloFlow must maintain its own outbound SMS log to enable reply-to-appointment matching.

### 11.2 Outbound SMS Log Requirements

**REQ-SMS-01:** Two distinct SMS message types are tracked differently:

- **Reminders** (informational — sent ~3 days before appointment): No reply matching needed. No `outbound_sms_log` reply-tracking record required. Message content: appointment date/time and clinic contact. No patient action expected.
- **Appointment confirmation requests** (action required — patient replies YES/NO): Must create an `outbound_sms_log` record for reply matching.

`outbound_sms_log` fields for confirmation requests: `outbound_sms_log_id`, `patient_id` (EMR), `appointment_id` (EMR), `send_type` (`appointment_reminder_confirmation`), `to_number` (E.164), `notifyre_message_id`, `sent_at`, `reply_status` (`pending` / `matched` / `expired`), `batch_job_run_id`. Confirmation records expire when `appointment date/time` passes — not a fixed-hour reply window.

**Conflict token:** No dedicated token column. At INSERT time, if another `outbound_sms_log` record exists for the same `to_number` with `reply_status = pending`, the new outbound message must include a conflict token = last 2 digits of `appointment_id` (e.g., "Reply YES 42 to confirm your appointment"). The token is derived at match time from the existing `appointment_id` field — no separate storage needed.

**REQ-SMS-02:** When an inbound SMS arrives (after HMAC verification and tenant resolution): first check for STOP / START / HELP keywords — process as consent or help event before any appointment matching (see Section 10). For all other replies:

1. If reply contains a 2-digit suffix (conflict token case): match against open pending confirmation records for that phone where `appointment_id LIKE '%{token}'`.
   - Exactly one match → confirm atomically (REQ-SMS-05)
   - Zero or multiple matches → `inbound_sms_ambiguous_match` work queue
2. If reply contains no token suffix: time-window match against open pending confirmation records for that phone (`reply_status = pending`, appointment not yet passed).

**REQ-SMS-03:** If time-window matching (no token) finds **exactly one match**, link the reply atomically and proceed with the appointment action. This is the normal case — one open confirmation per phone.

**REQ-SMS-04:** If time-window matching returns **multiple open records** (same phone, multiple pending confirmations — e.g., two children), route to `inbound_sms_ambiguous_match` work queue with all candidate `outbound_sms_log_id` and `appointment_id` values. FIFO ordering must **not** be used — selecting the wrong record poses a patient safety risk. No automatic resolution; staff selects the correct appointment.

**REQ-SMS-05:** Reply matching must be atomic. Once a reply is matched to an `outbound_sms_log` record, that record must be marked `matched` in the same transaction as the `patient_events` insert, preventing duplicate matching of the same reply.

**REQ-SMS-06:** If no open `outbound_sms_log` record is found for the inbound phone number within the reply window, route to work queue of type `inbound_sms_no_match`.

**REQ-SMS-07:** `outbound_sms_log` records must be marked `expired` by a scheduled cleanup job after the reply window closes with no reply received.

**REQ-SMS-08:** The `outbound_sms_log_id` is the primary workflow linker for SMS-based business flows. The reply (`patient_reply_received` event), the original send (`appointment_reminder_requested` / `appointment_reminder_accepted` events), and any downstream EMR write are all linked via `outbound_sms_log_id`. This supersedes `appointment_id` as the cross-request linkage mechanism for SMS flows; `appointment_id` is retained as a convenience reference. Event type naming follows the three-level intent/submission/delivery convention (ADR-003).

### 11.3 Outbound Fax Log

**REQ-FAX-LOG-01:** An `outbound_fax_log` table must be maintained in each tenant schema. Every outbound fax sent must create a record regardless of whether a patient is associated: `outbound_fax_log_id`, `fax_type` (FK to `shared.fax_types`), `to_number` (E.164), `notifyre_fax_id` (Notifyre's fax transmission ID), `subject_type` (`patient` / `payer` / `admin` / `vendor`), `subject_id` (optional — patient_id or other identifier depending on subject_type), `sent_at`, `delivery_status` (`pending` / `delivered` / `failed` / `indeterminate`), `delivery_callback_at`, `batch_job_run_id` (nullable).

**REQ-FAX-LOG-02:** When Notifyre delivers a fax delivery status callback, the router must resolve tenant context via `shared.provider_message_registry` (lookup by `(provider, provider_account, external_id)`) before opening any tenant schema — the callback arrives without inherent tenant identity (ADR-002). Once tenant is resolved, look up `outbound_fax_log` by `notifyre_fax_id` to link the callback to the original send, regardless of whether a patient is associated with the fax. If the registry entry is not found (e.g., sender crashed before committing it), retain in `unresolved_callback_queue` and retry on schedule.

**REQ-FAX-LOG-03:** If a patient is associated (`subject_type = patient`), a `patient_events` row must also be written for the delivery outcome, linking to `outbound_fax_log_id`.

**REQ-FAX-LOG-04:** The `outbound_fax_log_id` is the workflow linker for fax delivery chains (send → delivery callback). `patient_events.external_id` (Notifyre fax ID) provides a secondary link for patient-associated faxes.

---

## 12. Backlog (Deferred — Design Must Support)

The following features are deferred to a later sprint. The current data model and architecture must be designed to support them without schema changes. Additive migrations for new features are acceptable and expected — the goal is backward-compatible, non-destructive migrations.

| Feature | Description | Supported By |
|---|---|---|
| **Patient Communication Dashboard** | UI for clinic staff to view per-patient communication history | `patient_events` queryable by `patient_id` within tenant schema |
| **Message Center UI** | Front office push notifications for fax success/failure | `work_queue` + `error_queue` tables; `assigned_to` field reserved |
| **Work Queue Management UI** | Assign, prioritize, resolve work queue items; enforce RBAC | `work_queue` lifecycle fields reserved (REQ-WQ-06) |
| **Batch Job Report UI** | View batch job history and per-patient failure details | `batch_job_runs` + `patient_events.batch_job_run_id` + `batch_job_run_steps` |
| **Manual Batch Job Trigger** | Staff trigger batch job on demand outside schedule | `batch_job_runs.triggered_by = 'manual'` already supported |
| **SLA Escalation Automation** | Auto-escalate work queue items past SLA to `urgent` | `work_queue.due_at` (set at creation) |
| **Cold Storage Archival** | Move operational data older than 3–6 months to archive | Cloud Logging routing policy + GCS lifecycle rules; configurable, no schema change |
| **Retention Policy Enforcement** | Operational data: 3–6 months hot storage, then archive. Policy configurable, no design impact today. | `created_at` on all tables; partitioning strategy TBD |
| **Multi-EMR Routing** | Route patient writes to different EMR systems per patient | `ref_emr_systems` reserved; EMR adapter interface designed for this |
| **Access Audit UI** | View audit trail of staff and support access to PHI records | `access_audit_log` table (REQ-NFR-20); implementation deferred |
| **Flow-Specific Requirements** | Flows 1, 3, 5 | Separate flow-level requirement documents |

---

## 13. Configurable Reference Data

### 13.1 Purpose

Type values, status values, and category codes must be stored in reference/lookup tables — not as hard-coded constants or PostgreSQL native enums. New types and statuses can be added without a code deploy or schema migration.

### 13.2 Requirements

**REQ-REF-01:** The following reference tables must exist in the shared schema:

| Reference Table | Controls |
|---|---|
| `ref_event_types` | Valid values for `patient_events.event_type` |
| `ref_event_statuses` | Valid status codes for `patient_events` (e.g., `pending`, `accepted`, `rejected`, `indeterminate`, `delivered`, `delivery_failed`, `delivery_conflict_requires_reconciliation`) — **separate domain from `ref_queue_statuses`** (ADR-005) |
| `ref_queue_types` | Valid values for `work_queue.queue_type`; defines default initial status and SLA per type |
| `ref_queue_statuses` | Valid status codes per queue type; defines allowed transitions and terminal states — must not include patient-event outcome codes |
| `ref_error_types` | Valid values for `error_queue.error_type`; defines max retry count per type |
| `ref_fax_directions` | Valid direction values for `fax_types.direction` |
| `ref_job_types` | Available batch job type codes and definitions |
| `ref_job_run_statuses` | Valid status codes for `batch_job_runs` — separate domain from `ref_queue_statuses` and `ref_event_statuses` |

**REQ-REF-02:** All operational tables must use `VARCHAR` for type and status fields validated against the corresponding reference table — not PostgreSQL native `ENUM` types.

**REQ-REF-03:** Each reference table must include an `is_active` flag. Setting `is_active = false` retires a type without deleting historical records.

**REQ-REF-04:** Reference table seed data must be maintained as version-controlled SQL seed scripts applied via Alembic.

**REQ-REF-05:** Application startup must validate that all reference codes used in business logic exist in the database. A missing reference code is a startup error.

**REQ-REF-06:** New reference data values that require new application behavior must have a corresponding supported handler in application code. A handler registry mapping controlled codes to application handlers will be defined in the design document.

---

## 14. EMR Action Event Logging

### 14.1 Purpose

The system makes write calls to the EMR API on behalf of patients. Front office staff must have visibility into whether those EMR writes succeeded or failed.

### 14.2 Requirements

**REQ-EMR-01:** Every EMR API write call must result in an event record regardless of outcome.

**REQ-EMR-02:** EMR event types must include at minimum:

| Event Type | Trigger |
|---|---|
| `emr_appointment_confirmed` | EMR API call to confirm appointment succeeded |
| `emr_appointment_cancelled` | EMR API call to cancel appointment succeeded |
| `emr_appointment_rebook_flagged` | EMR API call to flag appointment for rebook succeeded |
| `emr_appointment_update_failed` | EMR API call for any appointment update failed |
| `emr_document_attached` | EMR API document attachment succeeded |
| `emr_document_attach_failed` | EMR API document attachment failed |

**REQ-EMR-03:** EMR event records must include the EMR resource identifier (appointment ID, document ID) and the sanitized HTTP status code / error message returned by the EMR API on failure.

**REQ-EMR-04:** If an EMR write fails, the failure must be recorded in the event log AND routed to the error queue with an appropriate error type.

**REQ-EMR-05:** EMR action events must carry the same `correlation_id` as the inbound request that triggered the workflow.

---

## 15. Correlation ID

### 15.1 Purpose

Every inbound request generates a unique `correlation_id` at the entry point, carried through all downstream actions for that request. For internal engineering and debug use only — never exposed to patients or clinic staff.

### 15.2 Requirements

**REQ-COR-01:** A `correlation_id` (UUID v4) must be generated at the entry point of every inbound request before any processing begins.

Entry points that generate a correlation ID:
- Notifyre inbound webhook (SMS reply, fax received)
- Batch job scheduler trigger (one ID per job run)
- Any internal API endpoint that initiates patient-facing actions

**REQ-COR-02:** The `correlation_id` must be propagated to all downstream systems for the duration of that request:
- Included in every `patient_events`, `inbound_fax_log`, `batch_job_runs`, `outbound_sms_log`, `outbound_fax_log`, `work_queue`, and `error_queue` row written as part of that request
- Included as `X-Correlation-ID` header on all outgoing HTTP calls (EMR API, Notifyre API)
- Included as a structured field in every Cloud Logging entry for that request

**REQ-COR-03:** The `correlation_id` must be stored in Python's `contextvars.ContextVar` and set by middleware at the start of each request, accessible to all modules without explicit argument passing.

**REQ-COR-04:** For batch jobs, all `patient_events` rows generated within one job run share the job-level `correlation_id` stored in `batch_job_runs.correlation_id`.

**REQ-COR-05:** `correlation_id` is **per-request only** — it does not span multiple inbound requests. Business workflow linkage across separate requests is achieved via:
- **SMS flows**: `outbound_sms_log_id` links the original send, the patient reply, and the downstream EMR write
- **Fax flows**: `outbound_fax_log_id` (outbound) or `inbound_fax_log_id` (inbound reclassification) links related events
- **EMR flows**: `appointment_id` from the EMR system

**REQ-COR-06:** If an inbound request already carries an `X-Correlation-ID` header from trusted internal infrastructure, the system must use that value. Public webhooks (Notifyre) must never be allowed to inject an arbitrary correlation ID — the header must only be accepted from a trusted infrastructure allowlist.

**REQ-COR-07:** Correlation IDs must appear in all Cloud Logging entries for backend tracing. They must never appear in user-facing UI, error messages shown to clinic staff, or SMS/fax content.

---

## 16. Non-Functional Requirements

**REQ-NFR-01:** Event DB writes (`patient_events`) must be synchronous (intent record before external call). Cloud Logging delivery is non-blocking and must not block or fail primary processing.

**REQ-NFR-02:** All tables storing PHI must reside in Cloud SQL (PostgreSQL) within the HaloVox GCP project. PHI must not appear in plain text in Cloud Logging entries.

**REQ-NFR-03:** OCR text and raw PDFs must be stored in GCS, not in the database.

**REQ-NFR-04:** All mutable tables must include `created_at` and `updated_at` timestamps. Append-only tables (`patient_events`, `batch_job_run_steps`, `sms_consent_events`, `work_queue_history`, `error_queue_history`) have only `created_at` (or equivalent insert-time timestamp) — no `updated_at` field is defined on these tables.

**REQ-NFR-05:** Type and status values must be defined in reference tables in the database schema, not as application-layer constants only. All status codes must use stable lowercase format with separate display labels.

**REQ-NFR-06:** `correlation_id` must be indexed on all tables that carry it.

**REQ-NFR-07:** Tenant isolation is enforced via schema-per-tenant with application-enforced access controls and compensating controls (see REQ-MT-19, ADR-001). No unrestricted cross-schema connection or schema-qualified cross-tenant query is permitted in any application path. Security/privacy risk-owner sign-off is required before production launch.

**REQ-NFR-08:** New tenant onboarding requires only provisioning a new schema and running Alembic migrations. No code deploy is required.

**REQ-NFR-09:** Batch job concurrency control (`tenant_jobs.concurrency_lock`) must be enforced via an atomic status check and update at the database level — not by the boolean flag alone.

**REQ-NFR-10:** A signed Business Associate Agreement (BAA) must be in place with every vendor that creates, receives, maintains, or transmits ePHI before PHI is transmitted. Required vendors include at minimum: Notifyre (SMS/fax), GCP (Cloud SQL, GCS, Cloud Run, Cloud Logging), and any OCR/AI service.

**REQ-NFR-11:** All data in transit must use TLS 1.2 or higher. No plaintext HTTP connections are permitted for any API calls (EMR, Notifyre, internal).

**REQ-NFR-12:** All data at rest in Cloud SQL and GCS must be encrypted. GCP default encryption is acceptable; CMEK may be required per clinic contract.

**REQ-NFR-13:** GCS storage for fax documents and OCR output must enforce per-tenant isolation (ADR-008):
- Each tenant's files must be stored under an immutable, opaque, tenant-specific prefix (e.g., `gs://halovox-fax/{schema_key}/`)
- A credential broker must authorize the `tenant_id → service_account` mapping before issuing GCS tokens; only the broker holds `serviceAccountTokenCreator` — application code cannot call `generateAccessToken` directly (pre-production requirement)
- Object keys must contain no PHI (no patient names, DOBs, phone numbers, appointment details); format: `{schema_key}/{uuid}.{ext}`
- Object integrity checksum (SHA-256) must be stored as **immutable GCS object metadata** (`x-goog-meta-sha256`) — not embedded in the object key name
- File size, MIME type, and page-count limits must be enforced before processing
- Only approved internal services may access fax objects — no public URLs or redirect to third-party hosts without explicit allowlist
- Malformed or malware-suspected documents must be quarantined, not processed
- Fax file deletion must satisfy all of: resolved work queue, verified EMR attachment, no legal hold, no quarantine, approved retention window elapsed, contractual data-disposition requirements met

**REQ-NFR-14:** Cloud SQL must run on a private IP within a VPC. No public IP exposure. Cloud Run must connect via Cloud SQL Auth Proxy.

**REQ-NFR-15:** Cloud Run service account must have minimum required GCP permissions (Cloud SQL client, Secret Manager accessor, Cloud Logging writer). No project-owner or editor roles. **GCS access:** Once the credential broker (ADR-008) is in production, the Cloud Run service account must have no direct GCS object creator/viewer permissions — all GCS access goes through the broker, which holds `serviceAccountTokenCreator` and issues short-lived per-tenant tokens. Before the broker is live, per-tenant service accounts with prefix-scoped access are the interim control.

**REQ-NFR-16:** PHI minimum necessary — only the minimum fields required for the use case may be stored in `patient_events.metadata`. Phone numbers in logs as last 4 digits only. No full names, DOBs, diagnosis codes, or raw provider payloads in Cloud Logging entries.

**REQ-NFR-17:** Cloud Logging entries must contain structural/observability fields only: event type, status code, tenant ID, correlation ID, timestamp. Full sanitized operational error detail (including PHI-adjacent error messages) must be stored in Cloud SQL only. "All events published to Cloud Logging" means metadata signals — not a copy of the database record.

**REQ-NFR-18:** Raw fax files (inbound PDFs, OCR outputs) must be deleted from GCS after the associated processing is fully complete (OCR done + EMR write confirmed, or work queue item resolved). Files must be retained in GCS while any associated work queue or error queue item remains unresolved. Temporary processing files must be cleaned up within a defined window after processing completes.

**REQ-NFR-19:** The system must maintain sufficient audit trail data to determine the scope of a potential PHI breach — which records were accessed/transmitted, by whom, timestamps, correlation IDs — supporting HIPAA breach notification obligations.

**REQ-NFR-20 [REQUIREMENT — IMPLEMENTATION DEFERRED]:** The system must produce access audit records for all PHI-touching operations by authenticated actors. Audit records are required for:
- Staff viewing patient events or communication history
- Opening or downloading fax documents
- Technical support cross-tenant access (covered by REQ-MT-21)
- Work queue item assignment and resolution
- Export or report operations covering patient data
- Administrative configuration changes (tenant config, fax types, notification config)

These records must be stored in an append-only `access_audit_log` outside tenant schemas. Implementation is deferred; the table must be provisioned with the initial schema.

---

## 17. Review Notes — Deferred or Declined Feedback

This section documents items raised in external requirements reviews that were deliberately deferred or declined, with rationale.

### 17.1 First-Round Review (2026-08-01)

| Review Item | Decision | Rationale |
|---|---|---|
| **Separate `request_id` from `correlation_id`** | Declined | Business workflow correlation handled via `outbound_sms_log_id` / `outbound_fax_log_id` / `appointment_id`. No `request_id` distinct from `correlation_id` needed. REQ-COR-05 clarifies semantics. |
| **Measurable NFR targets** (webhook latency, RPO/RTO, batch completion time) | Deferred | SLA numbers defined once system is operational and baseline is established. |
| **Acceptance and failure test cases** | Deferred to test plan | Defined in separate test plan during unit test alignment phase per HaloVox engineering process. |
| **Additional event envelope fields**: `schema_version`, `causation_event_id`, `parent_event_id` | Deferred | Over-engineered for current scale. `actor_type` and `actor_id` accepted on queue tables. |
| **Missing flow specs for Flows 1, 3, 5** | Intentional scope exclusion | Flow-specific requirements maintained as separate documents. |
| **Polymorphic source link model** | Deferred | Explicit nullable FKs sufficient for known source types. |
| **`batch_recipient_results` table** | Declined | `patient_events` + `batch_job_run_steps` + `outbound_sms_log` provide equivalent per-recipient records. Separate table is redundant. |
| **"No schema changes for future features" goal** | Revised | Changed to: favor backward-compatible, additive migrations. |
| **Full RBAC authorization rules for queue transitions** | Partially deferred | Lifecycle fields reserved (REQ-WQ-06). Backend authorization required; UI enforcement deferred. |
| **Centralized PHI-safe logging interface** | Deferred to design phase | PHI exclusion requirement captured in REQ-NFR-16/17. Interface defined in design document. |
| **`inbound_fax_classifier_below_threshold` in error queue** | Resolved — removed from error queue | Low-confidence classification is a normal workflow outcome. Routes to work queue only (REQ-FAX-05). |
| **`webhook_signature_invalid` in tenant error queue** | Resolved — moved to global security log | Invalid signatures cannot be safely assigned to a tenant from an unverified payload. Global security incident log added (REQ-ROUTER-08). |

### 17.2 Second-Round Review (2026-08-02)

| Review Item | Decision | Rationale |
|---|---|---|
| **Finding 5: Per-tenant consent doesn't solve account-wide Notifyre suppression** | Action item added (REQ-CONSENT-07 item 2) | Notifyre suppression scope not yet verified. Deferred until confirmed. HaloFlow's `sms_consent_events` remains authoritative for HaloFlow sending decisions regardless. |
| **Finding 14: Separate per-patient batch recipient results table** | Declined | `patient_events` joined to `outbound_sms_log` on `batch_job_run_id` provides equivalent per-recipient detail. Batch-level summary (failure category counts) stored on `batch_job_runs`. |
| **Finding 15: Out-of-order delivery callback handling** | Deferred pending Notifyre verification | `occurred_at` field requirement depends on whether Notifyre includes a provider-event timestamp in delivery callbacks. Verification action item added. Terminal status protection (REQ-EVT-10) is required regardless. |
| **Finding 15 + Finding 4: Notifyre idempotency key support** | Action item added | Must verify whether Notifyre supports idempotency keys on send API calls before finalizing REQ-EVT-01 implementation. |

### 17.3 Shared Infrastructure Table Inventory (M3)

The following shared-schema tables are control-plane infrastructure. All reside in the `shared` schema unless noted.

| Table | Write role | PHI? | Notes |
|---|---|---|---|
| `shared.tenants` | Control-plane only | No | Top-level tenant registry |
| `shared.tenant_inbound_number_registry` | Control-plane only | Low (E.164) | Inbound routing source of truth |
| `shared.provider_message_registry` | Data-plane (at send time) | No | Outbound callback routing |
| `shared.webhook_inbox` | Webhook processor only | Yes — encrypted payload | Application-layer encryption required |
| `shared.unresolved_callback_queue` | Webhook processor only | No | Callbacks arriving before registry committed |
| `shared.reconciliation_cases` | Reconciler only | No | Mutable retry lifecycle for indeterminate operations |
| `shared.ref_event_statuses` + all ref tables | Control-plane / migrations only | No | Read by all; written only by provisioning |
| `shared.access_audit_log` | Append-only; all roles write | Minimal — structured fields only | PHI access audit trail |
| Global security incident log | Append-only; webhook handler writes | No PHI | Failed HMAC, unroutable events |

### 17.4 ADR Reconciliation (v2.3 — 2026-08-02)

Changes applied to align requirements with finalized ADR decisions:

| Requirement | Change | ADR Reference |
|---|---|---|
| REQ-MT-19/20 | DB isolation revised from "database-level enforcement" to application-enforced isolation with compensating controls; Option B (per-tenant DB roles) deferred | ADR-001 |
| REQ-NFR-07 | Revised to match application-enforced isolation language | ADR-001 |
| REQ-EVT-01 | Intent-before-action scoped to external side effects only; inbound observations use single completed event; event naming updated to three-level convention (`_requested` / `_accepted` / `_delivered`) | ADR-003 |
| REQ-EVT-11 | `unknown_outcome` (terminal) replaced with `indeterminate` (non-terminal); reconciler uses anti-join against stale intents; no automatic resend on indeterminate | ADR-005 |
| REQ-ROUTER-09 | Revised from replay-window dedup to stateful dedup lifecycle with claim, lease, scheduled reclaimer | ADR-004 |
| REQ-CONSENT-07 | Removed opt-out inference from generic delivery failure; verified suppression signal required; STOP without verified signal = production blocker | ADR-009 |
| REQ-FAX-LOG-02 | Callback routing updated to require `shared.provider_message_registry` lookup before tenant schema access | ADR-002 |
| REQ-NFR-13 | Checksum moved from object key name to GCS object metadata (`x-goog-meta-sha256`); credential broker requirement added; fax deletion conditions made explicit | ADR-008 |
| REQ-SMS-01 | Removed `appointment_token` field from `outbound_sms_log` | ADR-006 |
| REQ-SMS-02/03/04 | Removed token-first matching; time-window matching is the only mechanism; ambiguous → work queue | ADR-006 |
| REQ-SMS-08 | Event type names updated to three-level convention | ADR-003 |
| REQ-REF-01 | Added `ref_event_statuses` as separate domain from `ref_queue_statuses` | ADR-005 |

### 17.5 ADR Reconciliation (v2.4 — 2026-08-02)

| Requirement | Change | ADR Reference |
|---|---|---|
| REQ-MT-02 | "Database level" isolation replaced with application-enforced + three-concept separation | ADR-001 |
| REQ-MT-20 | "No unrestricted connection" replaced with "no cross-tenant queries; data-plane role may hold grants" | ADR-001 |
| REQ-ROUTER-02 | Valid duplicate webhooks receive 2xx — not rejection | ADR-004 |
| REQ-ROUTER-09 | Full durable inbox 2xx semantics; safe for any recoverable inbox state | ADR-004 |
| Section 4.2 | Event catalog updated to three-level naming (intent/submission/delivery) | ADR-003 |
| REQ-EVT-04 | Cross-reference fixed to REQ-EVT-10 | — |
| REQ-EVT-09 | Event names updated to three-level convention | ADR-003 |
| REQ-EVT-11 | Reconciler uses `reconciliation_cases` table; normalized provider outcomes | ADR-005 |
| REQ-FAX-LOG-01 | `unknown_outcome` → `indeterminate` | ADR-005 |
| REQ-BATCH-08 | Added `recovering` as valid non-terminal status | ADR-007 |
| REQ-BATCH-09a/b/c | New `batch_recipient_operations` table specified | ADR-007 |
| REQ-BATCH-16 | Watchdog takeover state machine updated (recovering path vs failed path) | ADR-007 |
| REQ-SMS-01–04 | Hybrid token approach: reminders informational; confirmations expire at appointment date/time; conflict token = last 2 digits of appointment_id | ADR-006 |
| REQ-NFR-15 | Cloud Run SA has no direct GCS access post-broker | ADR-008 |
| Section 17.3 | Shared infrastructure table inventory added | M3 |
| REQ-CONSENT-07 | Items 4+6 accepted as known constraint; compliance sign-off required | ADR-009 |

### 17.6 Open Action Items (Notifyre Verification)

The following must be verified with Notifyre before production implementation begins. Items 1 and 3 are production blockers:

1. **STOP/START webhook** *(Production blocker)*: Do patient STOP and START replies trigger the "SMS Received" webhook? If not, a documented suppression-query API or verified suppression status code is required — consent must not be inferred from a generic delivery failure.
2. **Delivery callback fields**: Do outbound SMS and fax delivery callbacks include (a) an `occurred_at` provider event timestamp distinct from webhook delivery time, and (b) a client-supplied reference ID (our `operation_id`) that Notifyre echoes in every callback?
3. **Idempotency keys** *(Production blocker for automatic watchdog takeover)*: Does the Notifyre send API support idempotency keys for duplicate send prevention? Without this, watchdog must fence and hold for manual reconciliation — no automatic replacement sends.
4. **Account-wide suppression scope**: Does a patient STOP on one tenant's number suppress that phone number across all tenants on the same Notifyre account?
5. **Delivery callback sender number**: Do delivery callbacks include the clinic's FROM number?
6. **Subaccounts**: Does Notifyre support subaccounts for per-tenant scoped suppression, numbers, HMAC secrets, and billing?

---

*End of Requirements Document v2.4*
