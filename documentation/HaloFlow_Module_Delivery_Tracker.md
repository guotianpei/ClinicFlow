# HaloFlow Module Delivery Tracker

Last updated: 2026-09-01 (PR-2 merged)

## Current position — read this first

**The M01 debt PR is complete. PR-2 merged 2026-09-01** (pull request #6, merge commit `a3210e3`),
following PR-1 on 2026-08-31 (PR #5, `5eccdb7`). The merged tree is **byte-identical** to `95c507f`,
the commit the full gate was run against — `git diff 95c507f a3210e3` is empty. `main` has been
fast-forwarded locally and the merged branch deleted, locally and on the remote.

**M02 implementation is no longer gated on the M01 debt PR.** One precondition remains before M02
implementation begins, and it is not a merge: **R-E7 is deliberately unsatisfied** — see the M01
delivery notes. M02 must settle its own per-tenant object-installation mechanism first.

**PR-2 gate result**, on PostgreSQL 17.10 against a database created fresh from `001` → `003` and
again as an upgrade over an existing `001`/`002` database: ruff clean, strict mypy clean, **192 tests
pass**, up from the 125 baseline. Run twice, identical. 130 of those need no database, which is the
subset reviewable on the Mac without a server running. `.github/workflows/m01.yml` needed no change —
the new package sits under `src/haloflow/m01`, which the ruff and mypy lines already cover, and
`test_ci_workflow_covers_every_checked_production_path` still passes.

**CI on `a3210e3` is NOT yet confirmed.** Workflow run "M01 tenant isolation #21" was triggered on the
merge commit, but its conclusion could not be read from outside the repository, so it is recorded here
as unverified rather than assumed. **Check the Actions tab and update this line.** CI was green on
`main` as of PR-1, so the gate itself is known healthy; what is unconfirmed is this specific run.

**PR-2 was independently reviewed twice.** ChatGPT raised four findings at `29d6599` (two high) and two
more at `53d6425` (both low); all six were addressed. Two of the high findings were real
transaction-correctness defects: an unrecoverable crash window between the DDL commit and the `applied`
ledger write, and a silent dependency on `autocommit=True` that only the test factory supplied. Full
dispositions, including where a remedy differs from the one suggested, are in
`Shared Workspace/ClinicFlow/Work Session 2026-09-01/claude_pr2-review-corrections.md`.

**One decision changed during implementation: D13** (2026-09-01). The signed-off design had the
provisioner run `CREATE SCHEMA ... AUTHORIZATION haloflow_owner`, which PostgreSQL refuses without
membership in that role — and that membership would give the provisioner INSERT, DELETE and DROP over
`shared.access_audit_log`. Tenant schemas are therefore owned by `haloflow_provisioner`, and
`permissions.json` moves the `tenant_schema:ownership` token accordingly. Evidence:
`Shared Workspace/ClinicFlow/Work Session 2026-08-31/claude_d13-tenant-schema-ownership-finding.md`.

**Session handoffs:** `Work Session 2026-08-31/claude_session-handoff-next-session.md` carries the
environment traps and the test loop; `Work Session 2026-09-01/claude_pr2-review-corrections.md` carries
the review dispositions and the pre-push verification.

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
| M01 | 🟢 | 🟢 | 🟡 | 🟢 | 🟢 | 🟡 | 🟡 | ⬜ | ⬜ | 🟡 Foundation and the whole debt PR merged (PR-1 and PR-2). Implementation stays 🟡: the legacy SQLAlchemy/asyncpg modules are not yet behind M01 and the production identity adapter is open. Security/privacy stays 🟡 pending PHI-safe telemetry; reliability stays 🟡 pending Cloud SQL evidence |
| M02 | 🟢 | 🟢 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | 🟠 Design v0.3, ADR-011, and OI-007 all accepted; the M01 debt PR is merged, so implementation is unblocked except for one precondition: R-E7, the per-tenant object-installation mechanism, is M02's to settle |
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

- **PR-2 — MERGED 2026-09-01** (pull request #6, merge commit `a3210e3`; commits `595593b`,
  `29d6599`, `53d6425`, `827a942`, `95c507f`). 17 files, +3597 / -144. Verified on PostgreSQL 17.10
  before merge: ruff clean, strict mypy clean, **192 tests pass**, up from the 125 baseline, run twice
  identically against a freshly created database. Every changed file was SHA-256 compared between the
  Mac working tree and the container tree the gate ran on before the branch was pushed, and the merged
  tree is byte-identical to `95c507f`. Delivered the scope the signed-off package fixed (TC-E1 through
  TC-E26), with one departure recorded as D13 below and one requirement deliberately unmet (R-E7).
  Contents:
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

- **R-E7 IS NOT SATISFIED BY PR-2 — carried forward to M02 as an explicit gate.** The provisioner
  originally shipped a `TenantObjectInstaller` hook, and ChatGPT's review found that it handed a
  module-supplied callback the live provisioner-role connection: a role that owns every tenant schema
  and can write the tenant registry, with nothing in the interface confining an installer to the schema
  it was given. Rachel's disposition on 2026-09-01 was to **remove it rather than narrow it**, because
  M02 has no current consumer and PR-2 should not freeze a privileged contract against guessed
  requirements. Ordinary per-tenant objects are contributed as migration units through the registry,
  which the runner already takes as an argument; that extension point is retained and tested.
  **M02 must settle its own per-tenant installation mechanism — with the function owner, ACL and pinned
  `search_path` its SECURITY DEFINER functions actually require — before its implementation begins.**
  A repository control now fails on any Protocol method taking a connection, or any constructor
  parameter named like a module callback, anywhere in the provisioning package.

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

- PR-2's scope as the signed-off package fixed it, for reference:
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
- **The M01 debt PR is merged** — PR-1 on 2026-08-31, PR-2 on 2026-09-01 — so no M01 work blocks M02
  any longer. **One precondition remains, added by the PR-2 review: R-E7 is not satisfied. M02 must
  settle its own per-tenant object-installation mechanism — with the function owner, ACL and pinned
  `search_path` its SECURITY DEFINER functions require — before implementation begins.** M01 ships the
  migration-registry extension point, which covers ordinary per-tenant objects but not objects needing
  a different owner.

- **M01 PR-3 — R-E7's answer — is in rule-3 review, not yet coded.** PR-3 *is* the mechanism the R-E7
  precondition above asks for: the allow-listed execution role and its bootstrap contract, the typed
  verifier replacing v1's postcondition, the tenant-schema grant control, and checksum v2. It is
  deliberately **one PR** (D17) — the role mechanism, typed verification, checksum change and grant
  controls are one security contract over shared implementation surfaces.
  - Package at **v7** (2026-09-03), 79 test cases, in
    `Shared Workspace/ClinicFlow/Work Session 2026-09-03/claude_m01-pr3-review-package-v7.md`, with the
    handoff beside it, now **two artifacts** per the revised `COLLABORATION_WORKFLOW.md` §5: the
    **implementation packet (v5)**, which Aider reads and which carries specification only, and the
    **pre-implementation operator runbook (v1)**, which is Rachel's and is never loaded into Aider. The
    split is a safety boundary rather than filing: operator procedure inside the packet leaves the
    executor holding its own approval procedure, which positions it to run those steps rather than wait
    for them. **v7's architecture and
    requirements are approved by review; the remaining correction was packet-only.** The CP-5b/CP-5c
    seam is drawn at the **activation** boundary rather than the code boundary: CP-5b builds the
    consolidated grant component and leaves the live provisioning path unchanged, and CP-5c switches the
    path atomically — old grant locations removed, stage 2 invoked, stage 3 enforcing in the same
    transaction, runner only after. A seam at the code boundary would have left a committable state in
    which provisioning grants every schema privilege and activates a tenant with no exact ACL
    enforcement, `_verify`'s presence checks being no substitute (V20, V24). If the seam proves
    unworkable the sanctioned fallback is to recombine the two checkpoints, never to ship the
    intermediate state. **v5 and v6 are both superseded
    and both known to be wrong**: v5's stage 3 asserted a complete ACL before three of five grantee
    classes existed (D22), and v6's R-P1B.14 claimed a residue guarantee the committed sequence cannot
    provide (see below). Neither may be used as the design of record.
  - **ChatGPT's v6 review returned CHANGES REQUIRED and found a false invariant.** R-P1B.14 claimed a
    stage-1/2/3 failure leaves "no ledger row" and that a re-run starts "as if never attempted". Both
    are false: `_register_tenant` and `_create_schema` each commit before stage 2, so a stage-2/3
    failure leaves a committed `provisioning` row and schema, and a resumed tenant may hold grants and
    ledger history from an earlier attempt. **v7 states the invariant differentially** — the failed
    attempt adds no new ACL entry and no new or modified ledger row, both byte-identical to pre-attempt;
    the tenant does not activate; a repaired re-run resumes and converges. The strong claim survives
    only for a stage-1 failure on a fresh tenant. Five resume tests added (TC-P73–TC-P77).
  - **R-P1B.21 (new): the control is a gate, not a repair tool.** Stage 2 issues no `REVOKE` and stage 3
    repairs nothing; pre-existing committed drift fails stage 3 and **survives the rollback** (V28b,
    measured), leaving the tenant inactive until an operator intervenes. Silently revoking would destroy
    the evidence of how the grant arrived.
  - **D23 (2026-09-03): no infrastructure role may be an execution role.** `haloflow_provisioner` owns
    every tenant schema, and **V29 measured that an execution role owning the schema can mutate `nspacl`
    during stage 4** — which would break R-P1B.20 and invalidate stage 3's placement. The approved set
    excludes all of `PROVISIONING_ROLES` at composition; the tenant schema is owned by the provisioner
    alone; neither the migrator nor an execution role holds grant option. A measured nuance shapes the
    enforcement: a non-owner without grant option that issues a schema `GRANT` does **not** raise, it
    warns and changes nothing, so ownership is asserted from the catalogue rather than inferred from an
    error. A module needing gateway-owner behaviour declares its own role in its own Alembic revision.
  - **D22 (2026-09-03) — the expected schema ACL is entirely declared, and the grants consolidate.**
    Two findings. First, a provisioned tenant schema has **five** ACL grantee classes, not the four
    R-P1B.13 named: `_apply_grants` also grants schema `USAGE` to `haloflow_audit_projector`, declared
    in `permissions.json` only at object level. Second, and more serious, **v5's stage 3 could not have
    asserted a complete ACL at all** — `_apply_grants` runs *after* `apply_within_lock` and
    `_create_schema` granted the migrator inline, so three of five classes were absent or undeclared at
    the moment of assertion; v5 would have failed every correct provisioning and had no test that would
    have caught it. Resolution: all five classes are declared in a new `tenant_schema_role_privileges`
    manifest block; the expected-set builder contains **no role-name literal**, so
    `haloflow_audit_projector` is not a hard-coded exception; **stage 2 installs every declared schema
    grant and is the only place any is installed**; stage 3 asserts the complete set symmetrically
    before the runner; and the placement is justified by **V27**, which measured that the runner's
    `t001` work leaves `nspacl` byte-identical.
  - Review history: v2 and v3 each drew findings; the **v4 checklist review returned "conditionally
    aligned, no further design round required"** with one required correction — the stage-3 grant
    postcondition compared privilege *names*, which cannot distinguish `CREATE WITH GRANT OPTION` from
    `CREATE` and does not exclude `PUBLIC`. v5 applies it: set equality over
    `(grantee, privilege_type, is_grantable, grantor)` tuples, expected set built explicitly, plus
    TC-P62/63/64.
  - The central design correction, carried from v3→v4 and unchanged since: **validation precedes every
    privileged mutation.** Four stages — role-safety preflight (read-only, no ledger) → provisioner
    grant transaction → grant postcondition rolling back on mismatch → runner, which re-runs the
    preflight. A failure in any of the first three leaves no schema grant and no ledger row.
  - **D13's consequence, measured:** the migrator neither owns the tenant schema nor holds grant option,
    so a tenant unit cannot install its own prerequisite. The execution role's schema `CREATE` is
    installed by the **provisioner as schema owner**, driven from manifest data so M01 names no module
    role. Table-level grants stay inside the module's unit.
  - **Decisions D14–D21 are all settled. No PR-3 design decision remains open.** D14–D20 settled
    2026-09-01; **D21 settled 2026-09-03: `grantor` joins the stage-3 tuple equality and is pinned to
    `haloflow_provisioner`**, making the comparison four-dimensional. The deciding evidence is V25 — a
    declared execution role holding exactly its declared privileges, granted by someone other than the
    provisioner, matches on grantee, privilege and `is_grantable`, so `grantor` is the only remaining
    signal; the undeclared-grantee rule catches that case only when the recipient happens to be
    undeclared. The cost is unusually low because under D13 the provisioner owns the tenant schema and
    makes every grant, and its own baseline ACL entry also carries `grantor = haloflow_provisioner`, so
    one uniform rule covers every expected entry class with no exception list. A future grant installed
    by anything other than the provisioner — a Module-0 bootstrap, a break-glass repair, a
    managed-service operator — is a change to R-P1B.13 arriving with its own review, not a carve-out at
    implementation time.
  - **Repo hygiene defect, found 2026-09-03, NOT part of PR-3.** `make check` and `CLAUDE.md` are both
  narrower than CI: the Makefile's `LINT_PATHS`/`TYPE_PATHS` and `CLAUDE.md`'s "this is exactly what CI
  runs" block both omit `src/haloflow/composition.py`, which `.github/workflows/m01.yml` lints **and**
  type-checks. So `make check` can report clean on a file CI will reject — and PR-3's CP-2 edits
  `composition.py`. The workflow file is the authority. Fix separately: bring `LINT_PATHS`,
  `TYPE_PATHS` and `CLAUDE.md` back into line with it. Deliberately excluded from PR-3, whose packet
  makes both files prohibited and uses the CI commands verbatim.

- **Rule-3 sign-off given by Rachel 2026-09-03 on design package v7** (Requirements, Architecture, Unit
  Test Cases). D14–D23 all settled; ChatGPT approved the architecture and requirements. PR-3 is cleared
  to enter implementation, subject to the release gates below.

- **Repository prepared 2026-09-03.** The stale `.git/index.lock` was identified — its holder was macOS
  `Virtualization.framework`, the Cowork VM's folder share, holding a **read-only** descriptor rather
  than a writer — and removed; `main` fast-forwarded; branch
  `feat/m01-pr3-execution-role-typed-verification` created with the tracker change carried onto it.

- **Release Gate 2 (R-P4.4) — PARTIAL, not yet closed.** `shared.schema_migrations` returned **0 rows**
  on `haloflow_test_m01` (the local test database) on 2026-09-03. The gate requires **every** environment
  where migration `001` has been applied. Still to check: **`haloflow_dev`**, which
  `HALOFLOW_MIGRATION_DATABASE_URL` targets and which `make migrate` upgrades; and any cloud PostgreSQL
  17 instance created from `Work Session 2026-08-30/claude_setup-cloud-pg17.sh`. The 2026-08-31 note
  describing "the only environment where `001` has been applied" predates this check and should be
  treated as unverified until those are covered.

- **CP-0 baseline captured 2026-09-03, green.** On branch
  `feat/m01-pr3-execution-role-typed-verification`, with the tracker as the only modified file:
  `ruff check src/haloflow/m01 src/haloflow/composition.py tests/m01 alembic` → all checks passed;
  `mypy src/haloflow/m01 src/haloflow/composition.py` → no issues in 16 source files;
  `pytest tests/m01 -q` → **192 passed, 0 skipped**, 7.85s. This is the reference the CP-9 evidence
  compares against, and the 0-skipped figure is the starting point for TC-P41.

- **CP-1 pilot outcome 2026-09-03 — the plan gate is not load-bearing, and the role boundary moved.**
  Three `/ask` rounds produced a correct plan; the code that followed it invented a SQL-parsing contract
  (M01 never parses SQL), asserted the **v1 flat digest** instead of the v2 canonical payload, invented
  three reason codes outside both vocabularies, invented API, and omitted two of six requested tests.
  Separately, Aider has no test runner configured and **fabricated test output** — "25 passed" for a
  192-test suite. All edits reverted, nothing committed.

  **Rachel's decision, final form: Claude is the implementation lead** — Claude writes the tests, writes
  the production code, and runs the verification commands it can actually run. Aider/Qwen is no longer
  the default coding executor for PR-3; the handoff and supervision costs are too high for
  security-sensitive work, and it remains available only for a narrowly bounded mechanical task Rachel
  approves. (An interim decision on 2026-09-03 split the work — Claude authoring tests, Aider writing
  the production code — and was superseded the same day.) The governing finding is that **the plan gate
  cannot catch this class of error**: an abstract plan line reads identically whether the model
  understands the concept or not. See
  `Work Session 2026-09-03/claude_note-05-cp1-pilot-finding-role-boundary-change.md`. Design v7, the
  thirteen checkpoints and all 79 test cases are unchanged.

- **CP-1 COMMITTED 2026-09-04 as `6ee11b4` — checksum v2.** Codex approved with no remaining findings (note-11); Rachel authorized. Four files, 816 insertions, 8 deletions, on `feat/m01-pr3-execution-role-typed-verification` over `d12c788`. The delivery tracker was deliberately excluded from that commit and still needs separate documentation authorization.
  `provisioning/checksum.py` created (versioned canonical JSON payload; sorted keys, `(",",":")`
  separators, NFC, `\n`, SHA-256; two normalizations kept apart — the migration template collapses
  whitespace, a function body does not; every collection ordered by a stable semantic key, with
  duplicate identities refused rather than sorted). `TenantMigrationUnit.checksum` delegates to it.
  **The production unit's checksum moved `a2db1ef3…` → `5e232d15…`**, which TC-P41 asserts explicitly
  against the pinned v1 value rather than absorbing.

  Nine tests, all failing genuinely before the change (two as behavioural assertions against the
  existing property, seven because the deliverable module did not exist) and passing after. Claude-run
  gates: `pytest tests/m01 -m "not postgres"` **139 passed** (130 + 9, none lost); `ruff` clean; `mypy`
  clean, 17 files. **Rachel ran the authoritative `pytest tests/m01 -q` on her Mac against PostgreSQL
  17.10: 201 passed, 0 skipped, 7.76s** — the 192 baseline plus the 9 new tests, reconciling exactly.
  The 0-skipped figure is the load-bearing part: all 62 Postgres-marked tests genuinely executed against
  a live server, so checksum v2 is measured, not merely argued, not to have disturbed any
  database-backed behaviour.

  **CP-1's approved file list was extended by one file, with Rachel's approval:** `codes.py` gains three
  additive `PreconditionCode` members — `DUPLICATE_FUNCTION_IDENTITY`, `DUPLICATE_ACL_ENTRY`,
  `CONFLICTING_CONFIG_KEY` — because TC-P56 requires construction refusals and an existing repository
  control fails on any bare-string reason code. No existing member changed.

  Evidence: `Work Session 2026-09-03/claude_m01-pr3-checkpoint-evidence.md`.

  **Codex review 2026-09-04 returned CHANGES REQUIRED, and found two real defects Claude did not.** Both
  were reachable checksum collisions inside the canonicalizer: (1) a member of a recognized collection
  that could not be ordered was *filtered out* rather than refused, so `config: ["a=1", 42]` and
  `config: ["a=1"]` digested identically; (2) mapping keys were NFC-normalized without a collision check,
  so a composed and a decomposed key became one and a value was silently overwritten. A checksum used as
  a drift control cannot do either. Canonicalization now fails closed throughout, with two further
  additive `PreconditionCode` members Rachel approved — `CHECKSUM_PAYLOAD_MALFORMED` and
  `DUPLICATE_PAYLOAD_KEY` (five added by CP-1 in total).

  **This is the independence weakness in plan v4 §11 doing exactly what it was recorded to do.** Claude
  wrote both the filtering and the tests over it, and both encoded the same wrong assumption — that
  dropping an unorderable member was tidying rather than data loss. No amount of Claude-authored testing
  would have found it. Independent review did, on the first pass.

  **A second review round (note-09) found a third defect:** `ordered_config("ab")` returned
  `("a", "b")` — a bare string is iterable and yields strings, so the container was materialized before
  it could be refused, turning a malformed argument into two well-formed entries. It did not reopen
  either collision through `unit_checksum`, which rejects the case earlier, but the public helper
  contract was wrong and CP-7a is expected to reuse these helpers. Fixed one level below the finding, in
  the shared `_text_sequence`, because `ordered_config` was only the reachable instance of a pattern
  three callers shared. No new reason code.

  **All three findings shared a premise with the code Claude wrote** — filtering is tidying, key
  normalization is safe, a container that iterates is a collection — and Claude's tests encoded the same
  premises. No amount of Claude-authored testing would have found any of them. This is the concrete
  instance of the independence weakness recorded in plan v4 §11, and the argument for keeping the CP-5c
  pre-implementation test freeze exactly where it is.

  Rachel ran the authoritative gate after each fix, on PG 17.10: **201 → 205 → 206 passed, 0 skipped
  every time.** Zero skips matters: the 62 Postgres-backed tests genuinely executed after each change
  rather than skipping into a green result. Codex's closing inspection of the note-09 correction is
  outstanding — `claude_note-10-cp1-note09-correction.md`, focused diff 79 lines.

  **Open documentation action:** R-P4.5's wording moves from "conflicting" to "repeated, whether
  identical or conflicting" at the next approved documentation update. Design v7 is deliberately
  unchanged for now, so the code is knowingly stricter than the design text rather than silently so.

  **Open obligation:** the decision to order recognized collections by key name at any depth is accepted
  for CP-1 only, and must be reassessed at **CP-7a** against the concrete typed verification structure.

  **R-P4.4 — CLOSED 2026-09-04.** `shared.schema_migrations` holds **zero rows total** in `haloflow_dev`
  and zero in `haloflow_test_m01`, so no ledger anywhere holds a v1 checksum and **checksum v2 needs no
  compatibility or migration plan**. `haloflow/.env` declares exactly two Postgres URLs, both
  `localhost:5432`; every other Postgres URL in the repository is documentation boilerplate, so there is
  no cloud PostgreSQL 17 environment in configuration — subject to Rachel's confirmation that none exists
  outside `.env`. The first attempt used a `NOT LIKE … ESCAPE` predicate that emitted a shell-quoting
  `SyntaxWarning`; it was re-run without pattern matching so the captured evidence carries no asterisk.
  This gate had been open since before implementation began.

- **CP-2 COMMITTED 2026-09-04 as `555393f` — the execution role on a unit.** Codex approved with no code-review findings (note-13); Rachel authorized. Six files, 354 insertions, 9 deletions, over `6ee11b4`. The delivery tracker was again excluded and still needs separate documentation authorization.
  `TenantMigrationUnit` gains `execution_role: str | None`; a new `UnitDefinition` record lets a
  definition set declare one; `build_tenant_migration_registry` takes the approved set from the
  composition root, which declares it **empty** for production. Three additive `PreconditionCode`
  members. Seven tests plus two repository controls.

  **Two independent controls, and the tests prove the independence rather than asserting it** (R-P1.3).
  TC-P4 places each of seven malformed role names *inside* the approved set, so the allow-list cannot be
  what refuses them — a single merged control would pass all seven. TC-P78 does the same with all four
  infrastructure roles: each is approved and still refused, because the unit checks `PROVISIONING_ROLES`
  itself. So **a permissive approved set cannot reach `haloflow_provisioner`** — the case V29 measured as
  able to mutate `nspacl` during stage 4 and break R-P1B.20 outright (R-P1B.22(a), D23).

  **No production definition changed.** `TENANT_MIGRATIONS` is byte-identical and **`t001`'s checksum is
  unchanged at `5e232d15…`**, the value committed at CP-1. That is what putting `execution_role` in the
  A6 payload as `null` at CP-1 bought: the churn R-P4.4 gates happened once, and R-P4.4 does not need
  re-running.

  Gates: 153 non-Postgres passed (147 + 6), ruff and mypy clean; **Rachel's authoritative run 215 passed,
  0 skipped, 7.58s** on PG 17.10 — required at CP-2 rather than optional, because
  `test_provisioning_postgres.py` imports from `composition.py` at module scope and a composition change
  can take out collection of the whole file. It collected and ran.

  Evidence: `Work Session 2026-09-03/claude_m01-pr3-checkpoint-evidence.md`. Codex handoff:
  `claude_note-12-cp2-review-handoff.md`.

  **A design decision v7 did not make:** A1 gives the unit's shape but not the definition's. Rachel chose
  a definition value that stays a bare template string or becomes a frozen `UnitDefinition` record;
  CP-7a inherits it and adds `verification` to the same record. Codex accepted it as preferable to a
  parallel role mapping, because template, execution role and the future verification contract stay one
  definition and cannot drift by migration identifier.

  **Open action against the execution plan (Codex note-13, ruling 2):** plan v4 §3's checkpoint file
  table should name `codes.py` wherever a checkpoint's approved requirements demand new refusal codes —
  at least CP-3 and CP-7a — rather than treating each as an exception. Recorded for the next plan
  revision, not edited in silently.

- **Gates before PR-3 ships**, all evidence or authorization rather than design: Rachel's rule-3
    sign-off (given); **R-P4.4, the v1-checksum ledger check — CLOSED 2026-09-04, zero rows in every
    configured environment**; and re-running `claude_probe-pr3-acl-exactness-v2.sql` on the 17.10
    baseline (R-P1B.17) — V21–V26 were measured on 16.13, and this remains **open**. Both gates require
    captured command output, not an assertion that they passed.
  - **PR-3 is the first task implemented under the collaboration workflow adopted 2026-09-04**
    (`Shared Workspace/ClinicFlow/COLLABORATION_WORKFLOW.md`, ChatGPT's v5). Per checkpoint: Claude
    confirms the requirements, writes the requirement-traceable tests, and stops; both parties run the
    gates the measured verification split assigns them and confirm each failure is missing behaviour;
    Claude implements the scoped change, re-runs its gates, reads the complete diff and records the
    evidence; **Rachel reviews that evidence and authorizes the commit — nothing is committed before
    that**; ChatGPT/Codex then reviews the diff independently. Push, PR, merge and deployment each
    require separate authorization. Claude never commits, pushes, branches, merges, deploys, or touches
    `.git`.
  - **CP-5c is the one checkpoint whose tests Codex reviews and freezes *before* implementation** — the
    stage-3 exact-ACL security core, nineteen tests. Every other checkpoint is reviewed after.
  - The execution document is
    `Work Session 2026-09-03/claude_m01-pr3-implementation-plan-v4.md` (approved 2026-09-04); the
    evidence record is `claude_m01-pr3-checkpoint-evidence.md` in the same folder.
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
