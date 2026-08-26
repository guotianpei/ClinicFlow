# M01 PR 1 — Claude review disposition

## Review baseline

- Reviewed commit: `a06988a8430ff20801428082f34a460d5a8a30c8`
- Source review: `M01_PR1_Claude_Review_a06988a.md`
- Disposition: critical and high findings accepted and remediated in this branch

## Findings

| Finding | Disposition | Resolution |
|---|---|---|
| C1 bypassable SQL blocklist | Accepted | Removed arbitrary SQL from callbacks. Repository handles accept opaque keys resolved through an immutable M01-owned statement catalogue. Unsafe catalogue definitions are rejected, values remain bound parameters, and the effective path is reverified after the callback. Regression tests cover every reported bypass. |
| C1 per-tenant role recommendation | Not adopted | ADR-001 intentionally selects application-enforced isolation. `SET LOCAL ROLE` would still be changeable by hostile callback code. The structural statement catalogue closes the callback SQL surface without changing the accepted role model. |
| H1 raw pool methods | Accepted | Gateway/control checkout methods are private M01 APIs. Application-source checks prohibit importing the pool or catalogue issuer. Integration tests use independent test connections for negative raw-SQL assertions. |
| H2 fixture roles instead of migration roles | Accepted | The test login inherits the actual `haloflow_runtime` group role. Tests reconcile all roles in `permissions.json`, audit INSERT writers, runtime TEMP denial, and column-scoped tenant-registry reads. |
| H3 capability does not constrain writes | Accepted | Transaction read-only mode and statement authorization are derived from the context capability. Unknown/read capabilities default to read-only; callers cannot override the mode. |
| H4 live legacy SQLAlchemy path | Accepted deployment blocker | PR 1 remains non-deployable against PHI. A static exact allowlist prevents the legacy `haloflow.database`, SQLAlchemy, or asyncpg import surface from growing and must shrink during business-module migration. |
| M1 zero timeout edge | Accepted | A sub-millisecond remaining lifetime fails with `ContextExpired` before checkout; configured timeouts never become zero. |
| M2 equal statement/transaction timeout | Accepted | Transaction timeout has a positive margin above statement timeout, subject to context expiry. A slow statement is cancelled while the same verified backend remains reusable. |
| M3 fixed round trips | Partially accepted | Five `set_config` operations were collapsed into one round trip. Broader latency optimization is deferred until Cloud SQL measurements are available. |
| M4 manifest not reconciled | Accepted | Classifications are table- and column-complete. Integration tests require exact equality with `information_schema`; database comments record the no-PHI table policy and constrain `display_reference` intent. |
| M5 narrow CI paths | Accepted | Pull requests touching any `haloflow/**` path run the M01 workflow. |
| M6 destructive downgrade | Accepted | Revision 001 refuses downgrade. Audit/lifecycle evidence requires an operator-approved export, retention, and decommission procedure. |
| L1 invalid handle retains connection | Accepted | Invalidation clears both connection and catalogue references; the regression test inspects the cleared slots. |
| L2 unconstrained operation ID | Accepted | Resolver requires canonical UUID operation IDs before registry access. |
| L3/L5 raw SQL false positives and unsanitized error | Superseded | Raw SQL is no longer accepted. Unknown keys raise the sanitized `RepositoryStatementRejected` taxonomy without retaining or logging query text. |
| L4 unlocked registry read | Deferred and tracked | Transaction-start revalidation remains READ COMMITTED. `FOR SHARE` is incompatible with read-only transactions in the current design; lifecycle drain/decommission coordination is follow-on work. Effective-path and context-expiry checks still run before callback return. |
| L6 role/session commands | Accepted | Callbacks cannot issue SQL; catalogue construction rejects role/session commands and `set_config`. |

## Additional decisions

The upgrade deliberately does not use `CREATE SCHEMA IF NOT EXISTS`. A partially
present `shared` schema is drift and must fail visibly rather than be treated as
a successful migration. Alembic revision state provides normal idempotency.

The defensive runtime audit-table `REVOKE` remains even though current grants do
not give runtime INSERT. Effective privilege tests, rather than that statement,
are the evidence for the security property.

## Follow-up review of `2f4b4e1`

Claude's follow-up recommended merge and confirmed that C1 was closed. The
remaining actionable items were resolved as follows:

| Finding | Disposition | Resolution |
|---|---|---|
| R1 gateway-wide write authorization | Accepted | Every catalogue statement declares one required capability. The handle checks that exact capability in addition to transaction-level read/write mode. A regression test denies a peer-module write statement even when the context has a different write capability. |
| R2 timeout margin near context expiry | Accepted | Effective statement timeout is capped at remaining context lifetime minus the full configured margin. Transactions fail closed before checkout when the margin cannot be preserved. |
| R3 whitespace-defeatable `set_config` check | Accepted | Definition validation uses a case-insensitive `set_config\s*\(` pattern; space/newline and `pg_catalog`-qualified variants are regression-tested. This is defence-in-depth for M01-authored catalogue entries, not the application security boundary. |
| R4 quoted identifiers | Retained intentionally | Blanket rejection is conservative and fail-closed. M01 will revisit it only if a reviewed schema requires quoted identifiers. |
| R5 Python ownership assumption | Accepted | The review packet now states that private ownership is convention enforced by CI import discipline and catalogue security review. |
| R6 exact legacy allowlist | Retained intentionally | Equality is a deliberate forcing function: both growth and shrinkage require an explicit review of the migration boundary. |
| R7 success-path verification | No change | Callback SQL is catalogue-restricted, and exceptions roll back the transaction. Post-callback path verification is an additional success-path invariant, not the primary boundary. |
