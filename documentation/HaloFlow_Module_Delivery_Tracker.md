# HaloFlow Module Delivery Tracker

Last updated: 2026-08-23

## Status legend

| Symbol | Meaning |
|---|---|
| ⬜ | Not started |
| 🔵 | Next / ready to start |
| 🟡 | In progress |
| 🟠 | Blocked or decision required |
| 🟢 | Complete and accepted |
| ➖ | Not applicable |

## Module scope and dependency register

| ID | Module | Scope | Primary dependencies |
|---|---|---|---|
| M01 | Tenant Context and Data Access | Shared and tenant schemas; trusted tenant/schema registry; tenant-context propagation; transaction-scoped `SET LOCAL search_path`; connection-pool reset; support-access audit; negative cross-tenant controls. | Cloud SQL foundation; identity model |
| M02 | Event and Operation Foundation | Stable `operation_id`; intent, submission, delivery, and business-outcome events; append-only storage; idempotent inserts; state projections; event-correction conventions. | M01 |
| M03 | Webhook Inbox and Worker Runtime | HMAC and timestamp verification; encrypted durable inbox; duplicate receipt handling; atomic claiming; leases; heartbeat; attempts; retry states; scheduled reclaimer. | M01, M02 |
| M04 | Tenant and Provider Routing | Inbound-number registry; provider-message registry; tenant resolution; provider callback routing; unresolved callback queue; provisioning and registry lifecycle. | M01, M03 |
| M05 | Work and Error Queues | Queue states; assignment; priority; SLA; history; retry and escalation; staff versus support permissions; evidence and resolution capture. | M01, M02 |
| M06 | Outbound Operation and Batch Runtime | Tenant scheduler; batch runs; recipient operations; intent-before-send; watchdog; heartbeat; leases; fencing; provider idempotency; indeterminate outcomes; reconciliation. | M01, M02, M05 |
| M07 | SMS Messaging and Consent | Reminder and confirmation workflows; STOP/START/HELP; append-only consent history; reply-to-appointment matching; send and delivery callbacks; suppression behavior. | M03, M04, M05, M06 |
| M08 | Fax Processing | Inbound and outbound fax; per-tenant GCS access; credential broker; document validation; classification/OCR; patient matching; EMR attachment; retention/deletion gates; delivery tracking. | M03, M04, M05, M06 |
| M09 | Eligibility and Visit Readiness | Eligibility worklist and triggers; clearinghouse adapter; X12 270/271 normalization; deterministic exceptions; one safe retry; EHR write-back; staff exception handling. | M01, M02, M05, M06 |
| M10 | Clinic Workspace and Operational APIs | Work/error queue interfaces; fax and eligibility review; approval workflows; operational status views; role-based access; clinic-facing API contracts. | M05, M07, M08, M09 |
| M11 | Observability and Support Operations | PHI-safe structural telemetry; correlation IDs; metrics and alerts; reconciliation dashboards; audited support tools; runbooks; operational readiness. | Cross-cutting; begins with M01 |
| M12 | Governed Intelligent Layer | Context service; rules engine; approved knowledge; model gateway; decision orchestrator; structured decision object; human review; feedback; evaluation; monitoring; rollback. | M01, M02, M05, M10, M11 |

## Delivery tracking matrix

| ID | Detailed design | ADR / decisions | Implementation | Unit tests | Integration tests | Security / privacy tests | Reliability / performance tests | E2E / acceptance | Runbook / operations | Overall |
|---|---|---|---|---|---|---|---|---|---|---|
| M01 | 🔵 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | 🔵 Next |
| M02 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M03 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M04 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M05 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M06 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M07 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M08 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M09 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M10 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M11 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| M12 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |

## Completion criteria for each stage

| Stage | Completion criteria |
|---|---|
| Detailed design | Scope and non-goals approved; APIs/events, schemas, state machines, sequence diagrams, failure behavior, security controls, observability, rollout, and acceptance criteria documented. |
| ADR / decisions | Material alternatives recorded; decision owners and required risk acceptances identified; blocking questions resolved or explicitly tracked. |
| Implementation | Production code and migrations complete; feature flags/configuration included; code review complete; no unresolved critical defects. |
| Unit tests | Domain rules, state transitions, validation, retries, and negative paths covered; agreed coverage and mutation-quality expectations met. |
| Integration tests | Database, provider adapter, queue/worker, transaction, migration, and contract behavior verified using production-like dependencies. |
| Security / privacy tests | Tenant-isolation negatives, authorization, secrets, encryption, PHI-safe telemetry, audit, and abuse cases pass. |
| Reliability / performance tests | Idempotency, concurrency, lease expiry, crash recovery, duplicate callbacks, throughput, backpressure, and timeout behavior pass. |
| E2E / acceptance | Primary and exception workflows satisfy documented acceptance criteria with representative tenant configurations. |
| Runbook / operations | Dashboards, alerts, support procedures, reconciliation steps, deployment/rollback instructions, and ownership are documented and exercised. |

## Recommended implementation order

`M01 → M02 → M03 → M04 → M05 → M06 → M07 → M08 → M09 → M10 → M12`

M11 is cross-cutting and should be implemented incrementally beginning with M01 rather than deferred until the end.
