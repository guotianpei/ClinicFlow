# HaloFlow Track B — Session Status & Next Actions
**Date:** 2026-08-02  
**Author:** HaloVox Engineering  
**Status:** Active Development — Pre-Code Phase

---

## 1. What Was Accomplished Today

### Two Sessions Completed
- **Session 1 (morning):** Finalized and signed off `track-b-event-tracking-requirements.md v2.4`
- **Session 2 (afternoon):** Finalized and signed off `track-b-architecture-decisions.md v4` (ADR-001–009)
- Both documents are now **Accepted** — not Proposed. The requirements and architecture phase is complete.

### Design Review & Validation
- Reviewed ChatGPT-generated sequence diagrams (v4, 9 tabs) against requirements v2.4 and ADRs v4
- Identified **two critical issues** and **six moderate/minor issues**
- Rachel updated diagrams to v5 addressing both critical items; v5 approved as baseline for implementation

### Critical Fixes Applied (v4 → v5)
| Tab | Issue | Fix |
|---|---|---|
| Tab 02 | Single "upsert" to `provider_message_registry` after Notifyre call — crash window with no routing entry | Split into: (1) INSERT with `operation_id` + null `external_id` **before** Notifyre call; (2) UPDATE with `external_id` **after** acceptance. Aligns with ADR-002. |
| Tab 03 | Note recommended "random one-time token on every message" — directly contradicts locked ADR-006 | Note replaced with explicit ADR-006 alignment: time-window primary, last-2-digits conflict token on demand only, no token column |

### Architecture Diagrams Produced
- `HaloFlow_TrackB_Architecture_v2.drawio` — 6-tab draw.io (System Overview, Shared Infrastructure, Inbound Router & Inbox, Data Plane & Tenant Schema, Control Plane, Event Lifecycle & Reconciliation)
- `HaloFlow_Overall_Architecture.html` — Clean single-page SVG for stakeholder communication

### Key Decisions Confirmed / Corrected
- **Telnyx is not used.** Notifyre covers both SMS and fax (confirmed from requirements v2.4: `notifyre_message_id` in `outbound_sms_log`, `notifyre_fax_id` in `outbound_fax_log`, REQ-ROUTER-01)
- **GCP confirmed** as cloud platform (not Azure/AWS)
- **GCS Credential Broker** confirmed as required new component (ADR-008) — Cloud Run SA has no direct GCS object access post-broker

---

## 2. Current Artifact Status

| Artifact | Version | Status |
|---|---|---|
| Track B Charter | 2026-07-28 | ✅ Stable |
| HaloFlow Tier Use Case Roadmap | v2 (2026-07-29) | ✅ Stable |
| Event Tracking & Queue Requirements | **v2.4** (2026-08-02) | ✅ **Signed off by Rachel — 2026-08-02** |
| Architecture Decision Records (ADR-001–009) | **v4** (2026-08-02) | ✅ **Signed off by Rachel — 2026-08-02** (ADR-009 conditionally accepted pending Notifyre verification items 1 & 3) |
| Sequence Diagrams | **v5** (2026-08-02) | ✅ Approved — two critical fixes applied; 7 moderate items deferred to design phase |
| Architecture Diagrams (draw.io) | v2 (2026-08-02) | ✅ Reference quality |
| Overall Architecture (HTML/SVG) | v1 (2026-08-02) | ✅ Stakeholder-ready |
| **ERD / Database Schema** | — | ❌ **Not started — next action** |
| API Contracts | — | ❌ Not started |
| Unit Test Cases | — | ❌ Not started (required before coding per process) |
| Code | — | ❌ Not started |

---

## 3. ADR Status — Accepted (signed off by Rachel, 2026-08-02)

Both sessions today produced fully accepted documents. ADRs are no longer Proposed.

| ADR | Decision | Remaining external dependency |
|---|---|---|
| ADR-001 | Schema-per-tenant, application-enforced isolation; Option B (per-tenant DB roles) deferred | None — accepted |
| ADR-002 | `shared.provider_message_registry` as default callback routing; pre-send insert with `operation_id`, update on acceptance | Notifyre item 2 (client reference echo in callbacks) — informs recovery path only |
| ADR-003 | Three-level event model: intent / submission / delivery + stable `operation_id` | None — accepted |
| ADR-004 | Async durable webhook inbox; stateful claim/lease/reclaimer; PHI-encrypted payload | None — accepted |
| ADR-005 | Anti-join reconciler; `reconciliation_cases` table; normalized provider outcomes | None — accepted |
| ADR-006 | Hybrid SMS reply matching: time-window primary; conflict token = last 2 digits of `appointment_id` on demand only | None — accepted |
| ADR-007 | Central watchdog + heartbeat + fencing + `recovering` status + `batch_recipient_operations` + `operation_id` as idempotency key | **Notifyre item 3 (idempotency keys) — drives Path A vs Path B at runtime; both paths are designed and accepted** |
| ADR-008 | Per-tenant GCS SAs + Credential Broker; broker holds `serviceAccountTokenCreator` only | Credential broker must be built before production (pre-production engineering gate, not a sign-off blocker) |
| ADR-009 | Items 1 and 3 are production blockers; items 4 and 6 accepted as known operational constraints | **Items 1 and 3: Notifyre verification required before launch — fallback behaviour designed for both outcomes** |

---

## 4. Open Items

### 🔴 Production Blockers — Contact Notifyre Now
| # | Question | Impact if unsupported |
|---|---|---|
| 1 | Do STOP/START replies trigger the "SMS Received" webhook? | STOP handling becomes a production blocker; cannot infer opt-out from generic delivery failure |
| 3 | Does the send API support idempotency keys? | Watchdog must use Path B (manual reconciliation, no automatic takeover) |

### 🟡 ADR Engineering Gates (sign-offs done — these are build/verify tasks)
- **ADR-008:** Credential broker must be built and live before any production PHI hits GCS — engineering pre-production gate
- **ADR-009:** Notifyre items 1 and 3 must be verified before launch — contact Notifyre now (see §4 production blockers above)

### 🟠 Design-Phase Corrections (Sequence Diagrams — address during ERD/design)
| Ref | Item |
|---|---|
| Tab 04 | Outbound fax: pre-send `provider_message_registry` insert missing — same two-step fix as Tab 02 (insert `operation_id` before send, update `external_id` after acceptance) |
| Tab 01 | "Shared Inbox" actor conflates `shared.webhook_inbox` and `shared.tenant_inbound_number_registry` — separate actors needed |
| Tab 02 | "Tenant DB (logs only)" label understates content — tenant schema holds `batch_recipient_operations`, `tenant_config`, `sms_consent_events`, `work_queue`, `error_queue` etc. |
| Tab 09 | Shared schema incorrectly labeled PHI-containing — it is a Non-PHI control schema; `webhook_inbox` payload is transiently encrypted PHI but the schema classification is Non-PHI |
| Tab 09 | Stedi missing from external systems in C4 diagram |
| Tab 01 | HMAC failure path to `global_security_incident_log` not shown |
| Tab 07 | No escalation path defined if replacement worker fails to claim a `recovering` run — requirements gap to add |

### 🔵 Tier 1 Business/Infra (No Code Dependency — Can Start Now)
- Execute BAA with pilot clinic
- Set up GCP project, IAM, Cloud SQL, Secret Manager structure
- Confirm Notifyre BAA status (signed or self-serve?)
- Confirm Stedi BAA status
- Confirm Epic FHIR app credentials provisioning path with clinic

---

## 5. Next Actions (Priority Order)

### Immediate — Next Session
1. **Produce ERD / Database Schema** (shared schema + tenant schema)
   - Bridge between requirements and code; everything else depends on this
   - Covers: all `shared.*` tables, all `tenant.*` tables, reference table seed data, indexes, partial unique indexes, Alembic migration structure
   - Source of truth: requirements v2.4 + ADRs v4

2. **Contact Notifyre** (parallel — do not wait for ERD)
   - Verification items 1 and 3 are production blockers
   - Draft questions based on Section 17.6 of requirements v2.4

### After ERD
3. **API Contracts** — webhook ingress, provisioning API, staff-facing endpoints
4. **Unit Test Cases** — per engineering process: test cases required before any code
5. **Formally accept ADRs** — record sign-offs in the ADR document

### After Test Cases
6. **Begin coding** — starting with Tier 1 infrastructure, then Tier 2 batch workers

---

## 6. Engineering Process (Standing Rule)

```
Requirements → Architecture → Unit Test Cases → Code

No code is written until:
  ✅ Requirements reviewed and stable
  ✅ Architecture design reviewed and aligned
  ✅ Unit test cases defined and agreed
```

---

## 7. Documents to Push to GitHub Repo

Push these to `github.com/guotianpei/ClinicFlow/tree/main/documentation` before next session:

```
/documentation/track-b-event-tracking-requirements.md       ← v2.4
/documentation/track-b-architecture-decisions.md            ← v4 (ADR-001–009)
/documentation/halovox-track-b-critical-sequence-diagrams-v5.drawio
/documentation/track-b-session-status-2026-08-02.md         ← this file
```

At session start, Claude will read these files to restore full context before any design work begins.

---

*End of session status — HaloVox Track B — 2026-08-02*
