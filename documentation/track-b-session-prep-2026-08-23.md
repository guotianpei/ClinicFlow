# Track B — Ingestion Readout & Design-Session Prep

**Date:** 2026-08-23 (decisions recorded 2026-08-23)
**Prepared by:** Claude (session prep for module-by-module detailed design)
**Purpose:** Confirm full document ingestion, record decisions taken, surface remaining gaps before schema/module design, and propose a design sequence.

---

## 1. Documents Ingested

| Doc | Version / Date | Notes |
|---|---|---|
| `track-b-session-status-2026-08-02.md` | 2026-08-02 | Last formal session record — **3 weeks stale** |
| `architecture/track-b-architecture-decisions.md` | v4, ADR-001–009 | Repo copy still reads **Status: Proposed** |
| `architecture/track-b-design-principle-operational-knowledge-moat.md` | 2026-08-08 | Adopted principle; ERD field discipline |
| `architecture/HaloFlow_Overall_Architecture_v1-revised by me.drawio` | revised | 4 tabs (see §2) |
| `architecture/HaloFlow_Intelligent_Layer_High_Level_Architecture.docx` | v0.1, 2026-08-23 | New |
| `requirements/track-b-event-tracking-requirements.md` | v2.4, 2026-08-02 | Repo copy still reads **Draft — Pending Review** |
| `requirements/HaloFlow_Intelligent_Layer_Requirements.docx` | v0.1, 2026-08-23 | New |
| `requirements/HaloFlow_Tier_UseCase_Roadmap_v3.docx` | 2026-07-29 | |
| `requirements/Autonomous_Eligibility_Verification_Requirements.docx` | 2026-07-31 | |
| `emr-integration-backlog.md` | 2026-07-31 | |
| `track-b-vendor-baa-status.md` | 2026-07-31 | |

---

## 2. Revised Architecture Diagram — What Changed

Four tabs, consistent legend (aqua = deterministic/integration, blue = worker, purple = control/governance, green = human review, red = blocker/sign-off, gray cylinder = shared data, purple cylinder = tenant/PHI):

1. **System Overview** — now includes a first-class **Governed Intelligent Layer** box (Rules • Context • Knowledge • Model Gateway • Decision Orchestrator) alongside Workflow & Integration Services, with `context / recommendation` down and `feedback + outcomes` back up.
2. **Tier & Capability Roadmap** — Tier 3 labeled **"Low-Risk Assisted Intelligence — Voice + text intake"**. Adds a **Permanent Safety Boundary** node and the shared approval pattern (capture → pending → human review → approved action → notification → outcome).
3. **Runtime & Deployment** — GCP: Cloud Run (API/Router, Webhook Worker, Workflow Workers, Queue API, **Intelligence Services**), Cloud Scheduler/Jobs (scheduler, reconciler, reclaimer, watchdog, cleanup), Cloud SQL private IP, GCS opaque prefixes, **Credential Broker** (`tenant_id only` in → `short-lived token` out), Secret Manager/KMS, Cloud Logging (structural metadata only).
4. **Inbound Webhook Data Plane** — the ADR-004 durable-inbox flow, rendered correctly end-to-end.

The diagram is internally consistent with ADRs v4 and with the decisions below.

---

## 3. Decisions Taken — 2026-08-23

| Ref | Decision | Status |
|---|---|---|
| **D1** | **Voice agent is Tier 3.** Tier 4 is a high-level future target; its scope is deliberately not fully defined. | Locked |
| **D2** | **HaloFlow is EMR-agnostic by design; all EMRs are in scope.** Prototyping starts with **athenahealth** (self-serve sandbox already provisioned). All documents replace "Epic" with generic EMR language. | Locked |
| **D3** | **Eligibility runs through the clinic's existing clearinghouse by default**; a dedicated vendor (Stedi) is the fallback only where the clinic has no usable clearinghouse connection. | Locked in principle — needs an ADR |
| **D4** | **Sign-off timestamps use Rachel's actual review/approval date/time**, not the document-generation date. Retroactively: requirements v2.4 and ADRs v4 are **Accepted 2026-08-02**. | Standing rule |
| **D5** | **The Intelligent Layer's decision/outcome capture is not `work_queue`.** Its purpose is continuous workflow improvement: capturing the detailed resolution **and the surrounding context** of human intervention. `work_queue` remains the operational task list. | Locked |
| **D6** | The Intelligent Layer docs' suggested stack (Kafka, Temporal/Camunda, MLflow, AWS-or-Vertex) is **high-level options/ideal**, not a design decision. Locked stack remains GCP + Vertex AI. | Locked |

### D5 — the split, stated precisely

| | `work_queue` | Intelligent Layer decision/outcome store |
|---|---|---|
| **Question it answers** | What must staff do right now? | What was recommended, in what context, what did the human actually do, and what happened? |
| **Write pattern** | Mutable current state + `work_queue_history` transitions | Append-only |
| **Optimized for** | Fast querying, assignment, RBAC, SLA | Reconstruction and evaluation |
| **Holds context snapshot?** | No | **Yes** — inputs, evidence, provenance, data-quality flags, rule/model/prompt versions at decision time |
| **Retention driver** | Operational (resolve and move on) | Learning (must outlive the task) |
| **Consumer** | Clinic staff | Evaluation / improvement loop |

They link, they don't merge: a recommendation may spawn a `work_queue` item; resolving that item emits a feedback/outcome record back to the decision store. The reason this cannot be columns on `work_queue` is the **context snapshot** — IL-FR-003 provenance and quality indicators, IL-FR-008 evidence and versions — which is a point-in-time capture, not current state.

**Sequencing question for tomorrow (D5 vs. the Aug 8 moat principle):** the moat doc says capture failure reason + resolution path as *fields on existing tables* now, and don't build the knowledge layer until multi-clinic. D5 argues the context capture needs its own home. These reconcile if we build **one append-only per-tenant table now** (`agent_decision_log` / decision + outcome records) and still **defer** everything the moat doc actually deferred — cross-tenant aggregation, PayerProfile/ExceptionPattern objects, the model registry. A single per-tenant append-only table is not cross-clinic aggregation and triggers no new compliance gate.

---

## 4. New Consequence of D2 — ADR-006 Needs Superseding

**This is the top item for tomorrow.** Going EMR-agnostic does not soften the ADR-006 problem; it makes it structural.

ADR-006 sets the SMS conflict-disambiguation token as the **last 2 digits of `appointment_id`**, with the explicit caveat: *"Token approach assumes numeric appointment IDs. Confirm with target EMR systems (Epic typically uses numeric IDs)."*

- athenahealth appointment IDs are numeric — the token works there.
- **FHIR resource IDs are strings by specification** (`id` type, `[A-Za-z0-9\-\.]{1,64}`), not guaranteed numeric. Any FHIR-based adapter — which is the only general-purpose API surface CGM eMDs exposes — can return IDs where "last 2 digits" is undefined.
- Under D2, "confirm with the target EMR" is no longer answerable, because there is no single target EMR.

**The token must be derived from an identifier HaloFlow owns, not one the EMR assigns.** Natural candidate: a 2-digit derivation from `outbound_sms_log_id` or `operation_id` — both already exist, both are ours, both are stable across retries, and neither requires a new column (consistent with ADR-006's "no token storage" property). Collision behaviour and the `inbound_sms_ambiguous_match` fallback are unchanged.

Per the ADR process note (immutable once accepted, and D4 stamps ADR-006 as Accepted 2026-08-02), this requires **ADR-010 superseding ADR-006**, not an edit. Recommend drafting it as the first item of the SMS module, or ahead of it.

---

## 5. Remaining Gaps

### 🟠 Needs an ADR before its module

**G3a. Eligibility adapter (from D3).** D3 is an architecture decision, not just a vendor pick — it implies an `EligibilityAdapter` abstraction mirroring `EHRAdapter`, plus a per-tenant eligibility-provider field in `tenant_config` resolved at provisioning. Open sub-items: programmatic API access for clinic clearinghouses (CGM eMEDIX unconfirmed); Stedi BAA still worth closing since it is now the fallback path, not the default.

**G5a. Decision/outcome store scope (from D5).** Needs the sequencing call in §3 recorded as an ADR: one append-only per-tenant table now; cross-tenant knowledge layer still deferred behind the moat doc's compliance gate.

### 🟠 Design-phase — resolve within the relevant module

**G6. Moat fields absent from requirements v2.4.** The Aug 8 principle asks for **failure classification, resolution path taken, and outcome** on `error_queue`, `reconciliation_cases`, `work_queue`, `batch_recipient_operations`, and the eligibility/fax tables. v2.4 field lists omit these. Additive and near-free at ERD time. Also carry forward: explicit `requires_approval` flag on action types.

**G7. Outbound fax missing the ADR-002 pre-send registry insert.** REQ-FAX-LOG-01/02 describe callback resolution via `shared.provider_message_registry` but never state the **pre-send INSERT with `operation_id` before the Notifyre call, UPDATE with `external_id` after acceptance** — the two-step fix already applied to SMS (Tab 02). Sequence diagram Tab 04 has the same gap.

**G8. No escalation path when a replacement worker fails to claim a `recovering` run.** REQ-BATCH-16 / ADR-007 define the transition into `recovering` but not what happens if nobody picks it up. Needs a terminal timeout + staff work queue item.

**G9. Remaining sequence-diagram v5 corrections.** Tab 01 actor split (`shared.webhook_inbox` vs `shared.tenant_inbound_number_registry`); Tab 02 tenant-DB label; Tab 09 shared-schema PHI misclassification; Tab 09 missing eligibility vendor; Tab 01 HMAC → security incident log path. Fold into whichever module touches each.

**G13. `ref_emr_systems` is promoted by D2.** REQ-MT-18 currently calls it "reserved for future multi-EMR routing" and the Backlog lists Multi-EMR Routing as deferred. Under D2, **multi-EMR across tenants is v1** and `ref_emr_systems` + the adapter interface are first-class. REQ-MT-09 (one EMR per tenant) still holds. Only **per-patient** multi-EMR routing stays in the backlog.

### 🟡 Document edits to push (mechanical)

| File | Edit |
|---|---|
| `track-b-vendor-baa-status.md` | Move Retell AI from "Tier 4 — Voice" to **Tier 3** (D1) |
| `Autonomous_Eligibility_Verification_Requirements.docx` §6 | "already scoped as Tier 4 (voice, via Retell)" → **Tier 3** (D1); resolve Q2 with D3 |
| `HaloFlow_Tier_UseCase_Roadmap_v3.docx` | Replace all "Epic" with generic EMR (D2); Tier 1 open item "Confirm Epic FHIR app credentials" → generic |
| `track-b-session-status-2026-08-02.md` | Same Epic→generic edit in §4 Tier 1; supersede with a current session status doc |
| `track-b-architecture-decisions.md` | Stamp **Accepted 2026-08-02** on header + all nine ADRs (D4); fix cross-reference v2.3 → **v2.4**; ADR-006 gets a superseded-by pointer once ADR-010 lands |
| `track-b-event-tracking-requirements.md` | Header **Draft — Pending Review** → **Accepted 2026-08-02** (D4) |
| `emr-integration-backlog.md` | Fix duplicate row numbers in Open Items (two #3, two #4) |

### 🟡 Carried-forward engineering items (not blocking design)

- **Rotate the athenahealth Client Secret** — it appeared in screenshots in a prior session, and D2 just made athenahealth the primary prototyping target.
- Merge `integrations/telnyx.py` + `integrations/fax.py` into a single `integrations/notifyre.py`.
- Verify Cloud SQL instance `haloflow-db` is Running; create the real `.env`.
- **Notifyre verification items 1 (STOP/START webhook) and 3 (idempotency keys)** remain open production blockers. Neither blocks the ERD — both paths are designed — but item 3 decides watchdog Path A vs Path B at runtime.

---

## 6. Proposed Design Sequence

Process per module: **Requirements review → Architecture design → Unit test cases → alignment → code.**

| # | Module | Covers | Notes |
|---|---|---|---|
| 0 | **Reconciliation pass** | Push the §5 document edits; stamp sign-offs per D4 | Mostly mechanical now that D1–D6 are locked |
| 1 | **Shared control schema** | `tenants`, `tenant_inbound_number_registry`, `provider_message_registry`, `webhook_inbox`, `unresolved_callback_queue`, `reconciliation_cases`, all `ref_*`, `access_audit_log`, global security incident log | |
| 2 | **Tenant core & event model** | `patient_events` (three-level, `operation_id`, `submission_level`), `tenant_config`, `tenant_payers`, correlation propagation, **`ref_emr_systems` + EMR adapter interface as first-class (G13)** | |
| 3 | **Queues** | `work_queue` + history, `error_queue` + history — **with G6 moat fields** | |
| 4 | **Batch & watchdog** | `tenant_jobs`, `batch_job_runs`, `batch_job_run_steps`, `batch_recipient_operations`, partial unique indexes, `recovering` lifecycle — **closes G8** | |
| 5 | **Comms logs & consent** | `outbound_sms_log`, `outbound_fax_log`, `inbound_fax_log`, `sms_consent_events` — **closes G7**; **opens with ADR-010 (§4)** | |
| 6 | **Eligibility** | Worklist, dual-touchpoint orchestration, response normalization, exception taxonomy, retry rules — **opens with the G3a adapter ADR** | Unblocked by D3 |
| 7 | **Intelligence Plane foundation** | Decision + outcome records, context snapshot, `requires_approval`, linkage to `work_queue` — **per D5 and the G5a ADR** | Build the table, defer the layer |

Modules 1–5 are the ERD. Modules 6–7 are what keeps the schema migration-free later.

**Recommended start:** Module 0 (short), then Module 1 — with ADR-010 drafted early since it is the one decision that changes a table's contents rather than adding to them.

---

*Prepared for the 2026-08-24 detailed design session.*
