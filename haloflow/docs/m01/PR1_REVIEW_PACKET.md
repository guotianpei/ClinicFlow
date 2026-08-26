# M01 PR 1 — Foundation review packet

## Review target

- Branch: `feature/m01-tenant-context`
- Scope: M01 control schema, immutable tenant context, resolver, psycopg3 pool,
  transaction gateway, and isolation tests
- Database baseline: PostgreSQL 17 with psycopg3 and `psycopg_pool`
- Excluded from this checkpoint: production identity claims, Cloud SQL/IAM wiring,
  tenant provisioning orchestration, AuditProjector, and business-module migration

> **Deployment blocker:** this checkpoint is not deployable against PHI while
> legacy application modules still use the SQLAlchemy/asyncpg path. CI contains
> an exact, shrinking allowlist for that debt; no new bypass import may be added.

Review a frozen commit from this branch. Do not review or edit the original
`main` worktree.

## Requirements implemented

- Registry-derived schema identifiers matching `^tenant_[a-z0-9]{8,32}$`
- Immutable, short-lived `TenantContext` issued only through `TenantResolver`
- Fail-closed capability, tenant binding, purpose, lifecycle, and schema-version checks
- Registry revalidation inside every tenant transaction
- Hardened transaction-local path: `pg_catalog`, selected tenant, `pg_temp`
- Exact `current_schemas(true)` and `pg_my_temp_schema()` verification
- PostgreSQL 17 transaction-local statement, lock, and transaction timeouts
- Opaque statement keys resolved through an immutable M01-owned SQL catalogue;
  callbacks cannot submit SQL text or session commands
- Capability-derived read-only transactions and write-statement authorization
- Scoped repository handle clears its connection and catalogue after callback completion
- Prohibition of nested gateway transactions and arbitrary caller SQL
- psycopg automatic preparation disabled with `prepare_threshold=None`
- Pool reset with `DISCARD ALL`, baseline verification, and discard on uncertainty
- Empty runtime role search path and TEMPORARY revoked from `PUBLIC`
- Runtime denied direct writes to `shared.access_audit_log`
- Machine-readable shared-schema classification and permissions manifests

## Verification evidence

The M01 suite contains 53 passing unit, static, migration, grant, and PostgreSQL
integration tests. The database tests use one physical pooled connection where
required and cover:

- identical business IDs in two tenant schemas;
- alternating tenants on the same physical connection;
- rollback after callback failure;
- registry lifecycle change after context issuance;
- handle expiry and nested-call denial;
- ungated tenant SQL failure;
- all five independently reported quoted/comment/session-command bypass attempts;
- write denial under a read-only capability;
- real migration-role and column-level grant reconciliation;
- shared schema/table/column reconciliation with the classification manifest;
- TEMP object and global-audit write denial;
- disabled automatic preparation;
- client-task cancellation followed by safe peer-tenant reuse; and
- sub-millisecond context expiry; and
- recoverable PostgreSQL 17 statement timeout followed by reuse of the same clean backend.

## Empirical findings

1. psycopg exposes `pgconn.transaction_status` as an integer-compatible value.
   Equality must be used; Python object-identity comparison incorrectly rejects
   a clean connection.
2. PostgreSQL 17 `transaction_timeout` terminates the physical connection, so it
   is configured above `statement_timeout`; the recoverable timeout wins ordinary
   slow-query cases while transaction timeout remains a backstop.
3. Revoking TEMPORARY from the runtime role alone is insufficient because the
   privilege is inherited from `PUBLIC`; the migration revokes it from `PUBLIC`.

## Requested Claude review focus

Claude's review of commit `a06988a` is complete. The disposition and remediation
evidence are recorded in `CLAUDE_REVIEW_DISPOSITION.md`.

## Known follow-on work

- Freeze the identity-claim and capability vocabulary before HTTP integration.
- Implement checksummed tenant-schema provisioning and the tenant audit outbox.
- Resolve AuditProjector routing, cursor/retention, and decommission drain gates.
- Migrate reminders as the first HTTP plus background-job vertical slice.
- Add Cloud SQL Auth Proxy/IAM configuration and production-like latency evidence.
- Add PHI-safe telemetry, pool metrics, and security/privacy release evidence.
