# M01 PR 1 — Foundation review packet

## Review target

- Branch: `feature/m01-tenant-context`
- Scope: M01 control schema, immutable tenant context, resolver, psycopg3 pool,
  transaction gateway, and isolation tests
- Database baseline: PostgreSQL 17 with psycopg3 and `psycopg_pool`
- Excluded from this checkpoint: production identity claims, Cloud SQL/IAM wiring,
  tenant provisioning orchestration, AuditProjector, and business-module migration

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
- Scoped repository handle invalidated after callback completion
- Prohibition of nested gateway transactions and caller-qualified schemas
- psycopg automatic preparation disabled with `prepare_threshold=None`
- Pool reset with `DISCARD ALL`, baseline verification, and discard on uncertainty
- Empty runtime role search path and TEMPORARY revoked from `PUBLIC`
- Runtime denied direct writes to `shared.access_audit_log`
- Machine-readable shared-schema classification and permissions manifests

## Verification evidence

The M01 suite contains 30 passing unit, static, migration, grant, and PostgreSQL
integration tests. The database tests use one physical pooled connection where
required and cover:

- identical business IDs in two tenant schemas;
- alternating tenants on the same physical connection;
- rollback after callback failure;
- registry lifecycle change after context issuance;
- handle expiry and nested-call denial;
- ungated tenant SQL failure;
- cross-tenant qualification denial;
- TEMP object and global-audit write denial;
- disabled automatic preparation;
- client-task cancellation followed by safe peer-tenant reuse; and
- PostgreSQL 17 `transaction_timeout` followed by pool replacement and safe reuse.

## Empirical findings

1. psycopg exposes `pgconn.transaction_status` as an integer-compatible value.
   Equality must be used; Python object-identity comparison incorrectly rejects
   a clean connection.
2. PostgreSQL 17 `transaction_timeout` terminates the physical connection. The
   safe behavior is to let the pool discard it and create a verified replacement.
3. Revoking TEMPORARY from the runtime role alone is insufficient because the
   privilege is inherited from `PUBLIC`; the migration revokes it from `PUBLIC`.

## Requested Claude review focus

1. Attempt to find a path from application code to a raw pool or connection.
2. Challenge context construction, tenant-binding, registry-revalidation, and
   schema-identifier trust assumptions.
3. Review reset/discard behavior for exception, timeout, cancellation, and pool
   shutdown edge cases.
4. Review the migration's ownership and effective grants, especially every
   writer to `shared.access_audit_log`.
5. Look for SQL identifier interpolation, search-path fallback, shared/public
   resolution, prepared-state leakage, or repository-handle escape.
6. Check that logs, errors, manifests, and audit fields do not introduce PHI.

## Known follow-on work

- Freeze the identity-claim and capability vocabulary before HTTP integration.
- Implement checksummed tenant-schema provisioning and the tenant audit outbox.
- Resolve AuditProjector routing, cursor/retention, and decommission drain gates.
- Migrate reminders as the first HTTP plus background-job vertical slice.
- Add Cloud SQL Auth Proxy/IAM configuration and production-like latency evidence.
- Add PHI-safe telemetry, pool metrics, and security/privacy release evidence.
