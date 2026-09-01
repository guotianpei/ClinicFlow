# HaloFlow Module Delivery Tracker

Last updated: 2026-09-01

## Current position — read this first

**PR-1 merged 2026-08-31** (PR #5, `5eccdb7`). **PR-2 — the tenant provisioner and per-tenant
migration runner — is written, verified, and awaiting independent code review.** It is committed on
`feat/m01-debt-pr2-provisioner` (`595593b`, 16 files, +2944 / -124) and **not yet pushed**. It remains
the single gate in front of all M02 implementation until it merges.

**PR-2 gate result**, on PostgreSQL 17.10, run against a database created fresh from `001` → `003` and
again as an upgrade over an existing `001`/`002` database: ruff clean, strict mypy clean, **182 tests
pass**, up from the 125 baseline. Run twice, identical. `.github/workflows/m01.yml` needed no change —
the new package sits under `src/haloflow/m01`, which the ruff and mypy lines already cover, and
`test_ci_workflow_covers_every_checked_production_path` still passes.

**One decision changed during implementation: D13** (2026-09-01). The signed-off design had the
provisioner run `CREATE SCHEMA ... AUTHORIZATION haloflow_owner`, which PostgreSQL refuses without
membership in that role — and that membership would give the provisioner INSERT, DELETE and DROP over
`shared.access_audit_log`. Tenant schemas are therefore owned by `haloflow_provisioner`, and
`permissions.json` moves the `tenant_schema:ownership` token accordingly. Evidence:
`Shared Workspace/ClinicFlow/Work Session 2026-08-31/claude_d13-tenant-schema-ownership-finding.md`.

**CI is green on `main`**, confirming PR-1's workflow edit works on GitHub's runner and not only
locally. Merged branches have been deleted.

**Session handoffs:** `Work Session 2026-08-31/claude_session-handoff-next-session.md` carries the
environment traps and the test loop; `Work Session 2026-09-01/claude_pr2-for-chatgpt-code-review.md` is
the PR-2 review package.

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
| M01 | 🟢 | 🟢 | 🟡 | 🟢 | 🟢 | 🟡 | 🟡 | ⬜ | ⬜ | 🟡 Foundation and debt PR-1 merged; debt PR-2 written and verified, in review, not pushed; production readiness remains |
| M02 | 🟢 | 🟢 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | 🟠 Design v0.3, ADR-011, and OI-007 all accepted; implementation gated on M01 debt PR-2, which is now in review |
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

- **M01 debt PR — split into two, per the review signed off 2026-08-31.** Identified 2026-08-30 while
  reviewing M02 against the merged code; approved as decisions B1–B8/F1 and detailed in
  `Shared Workspace/ClinicFlow/Work Session 2026-08-31/claude_m01-debt-pr-review-package.md` (v5).

- **PR-1 — MERGED 2026-08-31** (pull request #5, merge commit `5eccdb7`; commits `8d4f51c` + `bbb9efc`).
  17 files, +1648 / -271. Verified on PostgreSQL 17.10 before merge: ruff clean, strict mypy clean,
  **125 tests pass**, up from the 57 baseline. Contents:
  - `execution_id` rename of the contextual `operation_id`, **typed `UUID`** at the resolver boundary
    (decision D1: canonical identity now holds by type rather than string-format validation), plus forward
    migration `002` renaming and retyping the column in `shared.tenant_state_history`,
    `shared.access_audit_log` and `shared.isolation_alerts`. `002` carries a PHI-safe preflight
    castability guard and takes `ACCESS EXCLUSIVE` on all three tables **before** scanning, closing a
    check/use race a concurrent writer could otherwise exploit.
  - Required `correlation_id: UUID` and `correlation_source` on `TenantContext`. Per FR-031 the resolver
    validates and preserves both and contains no UUID-generating code path at all; a test asserts the
    absence.
  - Multi-capability context issuance, replacing `frozenset({capability})`. An empty requested set is a
    distinct `CAPABILITIES_EMPTY` request-shape error, and request shape and runtime types are validated
    before any authorization comparison.
  - Public, startup-only `build_statement_catalog(...)`, a single production composition root at
    `haloflow/src/haloflow/composition.py`, write capabilities **derived** from the catalogue's WRITE
    statements, and `manifests/statement_catalog.json` pinning keys and query digests through that root.
    The gateway now requires an explicit catalogue; the previous silent empty default failed at first use
    rather than at construction.
  - **F-2 closed**: the private-API ownership check is now AST-based over eight import forms.
  - `.github/workflows/m01.yml` extended to lint and type-check `src/haloflow/composition.py`, with a test
    asserting CI coverage from the workflow file.

- **PR-2 — WRITTEN AND VERIFIED, IN REVIEW, NOT PUSHED** (branch `feat/m01-debt-pr2-provisioner`,
  commit `595593b`). Delivered exactly the scope the signed-off package fixed (TC-E1 through TC-E26),
  with one departure recorded as D13 below. Contents:
  - New package `haloflow.m01.provisioning`: `units.py` (ordered, checksummed migration units and the
    trusted startup-only registry builder; checksums over the template, not the rendered text, so one
    migration has one checksum across every tenant), `runner.py` (the ledger state machine),
    `provisioner.py` (the FR-017 sequence and the R-E7 installer hook), `drift.py`, `codes.py`
    (the closed sanitized-error vocabulary), `roles.py`.
  - The advisory lock is session-level on a dedicated connection, so it survives the `running` commit
    that opens the DDL window. TC-E7/TC-E16 prove it: a slow unit commits `running` and then sleeps
    inside the DDL, and a second runner is refused during that window.
  - On failure the DDL is rolled back **before** `failed` is committed, asserted by observing that the
    table the failing unit created is absent at the moment `failed` is visible.
  - Migration `003` implements the split `permissions.json` had specified since `001`: **F-1** closed,
    **D12** (provisioner INSERT-only on `shared.tenant_state_history`, no sequence privilege) and
    **F-4** (audit sequence revocations) applied. Both re-confirmed on 17.10.
  - **F-3** closed: `permissions.json` is now verified against actual database grants, exhaustively
    over every role × every shared table × all seven table privileges, failing on unauthorized grants
    as loudly as on missing ones.
  - **D11** applied: the gateway isolation suite runs against provisioner-built schemas, with one
    deliberately minimal hand-built control (`clinic-c`) retained.
  - **R-E12** enforced by three new repository controls, each with a negative case: test-only migration
    units cannot enter the production registry, only the composition root composes one, and no
    production module passes `allow_test_units`.

- **D13 — tenant-schema ownership (decided 2026-09-01, Rachel).** Tenant schemas are owned by
  `haloflow_provisioner`; `permissions.json` moves the `tenant_schema:ownership` token from
  `haloflow_owner` to `haloflow_provisioner`. The signed-off Part 2E step 2 —
  `CREATE SCHEMA ... AUTHORIZATION haloflow_owner` run as the provisioner — does not execute:
  PostgreSQL 17.10 answers `must be able to SET ROLE "haloflow_owner"`, and the membership that would
  allow it lets the provisioner INSERT into, DELETE from and DROP the shared audit table.
  `WITH SET FALSE, INHERIT FALSE` does not rescue it. A consequence: the runtime's default privileges
  moved into the `t001` baseline, which runs as the migrator, because default privileges apply to
  their creating role's future objects and the provisioner could only set the migrator's by being a
  member of it — which R-E6 forbids. Full probe evidence in
  `Shared Workspace/ClinicFlow/Work Session 2026-08-31/claude_d13-tenant-schema-ownership-finding.md`.

- PR-2's original scope, for reference, as the signed-off package fixed it:
  - Tenant-schema provisioner and per-tenant migration runner writing the full lifecycle to
    `shared.schema_migrations`. Migration `001` creates only the `shared` schema; there is still no code
    that creates a tenant schema, and the M01 tests build them by hand.
  - The runner's per-tenant advisory lock must be **session-level** on a dedicated connection, held across
    its intermediate commits. A transaction-scoped lock releases at the `running` commit and leaves the
    DDL window unprotected.
  - Migration `003`: the provisioner/migrator grants (F-1), `shared.tenant_state_history` INSERT-only for
    the provisioner (D12 — no sequence privilege is required; verified on PostgreSQL 17.11), and the F-4
    sequence revocations.
  - The manifest-versus-database grant control (F-3).
  - Tenant baseline `t001` means "M01 infrastructure baseline", not "M02-ready".
  - Test fixture goes hybrid: the main isolation suite on provisioner-built schemas, plus a small
    independent hand-built control so a consistently-wrong provisioner cannot become the test oracle for
    the gateway behaviour it validates.

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
- **Next gate: M01 debt PR-2** (tenant provisioner and per-tenant migration runner). PR-1 merged
  2026-08-31; PR-2 written and verified 2026-09-01 and now in code review. No M02 migration is
  written until PR-2 merges.
- Debt-PR review completed and **signed off 2026-08-31** (Requirements, Architecture, Unit Test Cases),
  satisfying the project's pre-coding review rule. Package:
  `Shared Workspace/ClinicFlow/Work Session 2026-08-31/claude_m01-debt-pr-review-package.md` (v5).
  Reviewed by ChatGPT; 12 decisions (D1-D12) resolved; 67 test cases. Delivered as **two PRs**:
  PR-1 = `execution_id` rename, correlation contract, multi-capability issuance, public statement
  catalogue, migration `002`. PR-2 = tenant provisioner, per-tenant migration runner, migration `003`.
- Four findings against the merged `001`/M01 code were raised during the review and are scheduled into
  those PRs:
  - **F-1** `haloflow_provisioner` and `haloflow_migrator` are created by `001` with **no grants at
    all**, though `permissions.json` already specifies the intended split. **CLOSED in PR-2** by migration `003`.
  - **F-2 — CLOSED in PR-1.** The private-API ownership check substring-matched a dotted name and so
    missed `from ... import ...` forms entirely. Replaced with an AST check covering eight import forms,
    including submodule-via-parent-package and wildcard imports.
  - **F-3** `permissions.json` is never verified against actual database grants - both manifest tests
    assert about the JSON itself - so M01-FR-013's acceptance criterion is not met, which is why F-1
    went unnoticed. **CLOSED in PR-2** by an exhaustive manifest-versus-database grant test.
  - **F-4** `001` grants `USAGE, SELECT` on `shared.access_audit_log_audit_id_seq` to two audit roles
    that need neither (the column is `GENERATED ALWAYS AS IDENTITY`); the `SELECT` lets a role denied
    SELECT on the table read `last_value`, i.e. the global audit row count across all tenants.
    Verified on PostgreSQL 17.11 and re-confirmed on 17.10. **CLOSED in PR-2** — revoked by `003`.
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
