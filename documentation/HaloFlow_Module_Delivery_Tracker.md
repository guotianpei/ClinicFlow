# HaloFlow Module Delivery Tracker

Last updated: 2026-08-30

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
| M01 | 🟢 | 🟢 | 🟡 | 🟢 | 🟢 | 🟡 | 🟡 | ⬜ | ⬜ | 🟡 Foundation merged; debt PR required before M02; production readiness remains |
| M02 | 🟢 | 🟢 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | 🟠 Design v0.3, ADR-011, and OI-007 all accepted; implementation gated on the M01 debt PR |
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

## Delivery notes

### M01 — Tenant Context and Data Access

- Foundation merged to `main` through pull request #1 on 2026-08-26.
- Merged implementation includes PostgreSQL 17 shared control schema and roles,
  immutable resolver-issued tenant context, transaction gateway, M01-owned SQL
  catalogue, exact per-statement capability checks, pool reset/baseline controls,
  migration safety, and schema/permission manifests.
- Independent review closed the critical cross-tenant SQL finding and recommended
  merge after two remediation commits (`2f4b4e1` and `25e4154`).
- Verification: 57 M01 tests passed against local PostgreSQL 17, with Ruff and
  strict mypy checks passing.
- Remaining before PHI deployment: migrate legacy SQLAlchemy/asyncpg business
  modules behind M01; integrate production identity claims and provisioning;
  complete Cloud SQL reliability/performance evidence, E2E acceptance, PHI-safe
  telemetry, operational runbooks, and lifecycle/decommission coordination.

- **M01 debt PR required before M02 implementation** (identified 2026-08-30 while reviewing M02
  against the merged code, and approved as decisions B1–B8/F1):
  - Tenant-schema provisioner and per-tenant migration runner. Migration `001` creates only the `shared`
    schema; there is no code that creates a tenant schema, and the M01 tests build them by hand.
  - Public, startup-only `build_statement_catalog(...)`; explicit catalogue argument on the gateway with no
    silent empty default; CI manifest pinning statement keys and query digests.
  - Multi-capability context issuance. `TenantResolver` currently issues `frozenset({capability})`, a
    singleton, which would force every statement in one atomic M02 flow to share a single coarse capability.
  - `execution_id` rename (contextual `operation_id`), plus forward migration `002` renaming and retyping
    `operation_id` to `execution_id uuid` in `shared.tenant_state_history`, `shared.access_audit_log`, and
    `shared.isolation_alerts`.
  - Required `correlation_id: UUID` and `correlation_source` on `TenantContext`.
- Ruff reports 32 pre-existing findings, all in the legacy pre-M01 modules (`modules/`, `integrations/`,
  `ehr/`, `main.py`) and none in `m01/`, `tests/`, or `alembic/`. They belong to the legacy-migration item
  above rather than to any M02 change set.

### M02 — Event and Operation Foundation

- Detailed Requirements v1.0 approved 2026-08-28. Technical Design v0.3 approved as the implementation
  baseline 2026-08-30, closing review findings T1–T8 and V1–V5.
- **ADR-011 accepted 2026-08-30**, superseding portions of ADR-003 and ADR-005: `idempotency_key` removed;
  `submission_level` → `event_level` with six levels; `append_sequence` as total-order tiebreaker only;
  capability-scoped canonical acceptance binding; tenant-bound verified references replacing unenforceable
  shared-to-tenant foreign keys; mandatory `tenant_id` and one reconciliation case per tenant operation;
  unknown-versus-conflict vocabulary with `status_scope`; fingerprint canonicalisation; bounded full-range
  replay; reference-catalogue generation ledger; and the four-identifier model.
- Shared Infrastructure Table Inventory updated with six new tables and a correction to the
  `shared.access_audit_log` write-role entry, which had read "all roles write" against an M01 migration that
  grants INSERT to only two roles and revokes it from `haloflow_runtime`.
- **OI-007 accepted and frozen 2026-08-30** — the authoritative event type, status, and contract seed
  catalogue, at `documentation/detailed design/M02_OI-007_Seed_Catalog_v1.0.md`. Restructures
  `ref_event_statuses` to hold only vocabulary (code, `status_scope`, description), moving terminality,
  compatible-outcome class, and precedence rank onto the versioned `event_contracts` row per design §5.2's
  `EventContract` descriptor; adds a `ref_action_families` table enforcing exclusive namespace ownership in
  the database rather than by convention; reserves `m02_test_` as the non-production self-test family
  covering all six event levels. Specific module `action_family` prefixes for M07, M08, M09 and M12 are
  **not** granted here — each module registers its own prefix at its own design/content freeze.
- **Next gate: the M01 debt PR.** No M02 migration is written until it lands.
- Debt-PR review completed and **signed off 2026-08-31** (Requirements, Architecture, Unit Test Cases),
  satisfying the project's pre-coding review rule. Package:
  `Shared Workspace/ClinicFlow/Work Session 2026-08-31/claude_m01-debt-pr-review-package.md` (v5).
  Reviewed by ChatGPT; 12 decisions (D1-D12) resolved; 67 test cases. Delivered as **two PRs**:
  PR-1 = `execution_id` rename, correlation contract, multi-capability issuance, public statement
  catalogue, migration `002`. PR-2 = tenant provisioner, per-tenant migration runner, migration `003`.
- Four findings against the merged `001`/M01 code were raised during the review and are scheduled into
  those PRs:
  - **F-1** `haloflow_provisioner` and `haloflow_migrator` are created by `001` with **no grants at
    all**, though `permissions.json` already specifies the intended split. Closed by `003` (PR-2).
  - **F-2** the private-API ownership check substring-matches a dotted name and so misses
    `from ... import ...` forms. Closed by an AST check (PR-1).
  - **F-3** `permissions.json` is never verified against actual database grants - both manifest tests
    assert about the JSON itself - so M01-FR-013's acceptance criterion is not met, which is why F-1
    went unnoticed. Closed by a catalogue-vs-manifest test (PR-2).
  - **F-4** `001` grants `USAGE, SELECT` on `shared.access_audit_log_audit_id_seq` to two audit roles
    that need neither (the column is `GENERATED ALWAYS AS IDENTITY`); the `SELECT` lets a role denied
    SELECT on the table read `last_value`, i.e. the global audit row count across all tenants.
    Verified on PostgreSQL 17.11. Revoked by `003` (PR-2).
- Migration `002` data risk cleared 2026-08-31: all three `operation_id` columns verified empty on the
  only environment where `001` has been applied. A PHI-safe preflight castability guard ships anyway.
- Remaining production gates: OI-005 (provider capability evidence), OI-006 (retention, legal hold,
  disposition, correction authority), OI-009 (measured objectives, now including the Cloud KMS and
  reference-catalogue cold-start dependencies), and OI-010 revalidation before pilot.
- Applying the D4 acceptance stamps to ADR-003, ADR-005 and ADR-007 remains an open Module 0 action; those
  ADRs still carry `Proposed` in the canonical record.

## Recommended implementation order

`M01 → M02 → M03 → M04 → M05 → M06 → M07 → M08 → M09 → M10 → M12`

M11 is cross-cutting and should be implemented incrementally beginning with M01 rather than deferred until the end.
