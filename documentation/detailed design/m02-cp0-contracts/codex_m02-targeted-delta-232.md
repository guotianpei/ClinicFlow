# M02 CP0 — targeted delta to Claude232

2026-09-10 · ChatGPT/Codex lead · Claude independent reviewer · CP0 incomplete.
Review target: P1/P2/P4 only, P3 owner limitation, F1–F5 folded in. No whole-package re-review requested.

Read authoritative Claude232 SHA256 543f190b0a8e2dafce533054237ea649881864af5c7d512d1f746d78d9ac5736. Both its review and delivery-correction messages are handled using this final copy. C1–C8 remain CLOSED AS PROPOSALS per Claude; no author upgrade to owner approval.

## 1. Changed artifacts and precedence

- codex_m02-contract-decisions-and-synthetic-profile-v2.md replaces annex v1. Its final targeted-delta section governs the explicit refinements.
- codex_m02-synthetic-descriptors-v2.json replaces descriptor JSON v1.
- codex_m02-read-manifest-v2.md/.json replace read-manifest v1; only three engine metadata paths are added. No base-column union change.
- This document adds the signer interface and exact test/owner-sheet delta to consolidated-review-package-v1. Everything outside these changes retains its existing disposition.

P1 value constraints now cover every nonempty frozen “other constraints” cell. Required candidate identity columns and closed conflict metadata preserve both identities/facts. P2 provider lookup accepts the canonical accepted fact for acceptance-conflict resolution, with noncanonical selection explicitly unsupported. P3 no-side-effect indeterminate-only history remains an uncovered production branch, recorded below. Full details appear in annex v2 and the JSON.

## 2. SignerProvider interface contract — P4

This is an implementation-neutral interface specification, including the non-production test provider behavior. No production/test provider code is claimed delivered during document drafting.

`sign(purpose, payload, binding, issued_at, expires_at) -> SignedEnvelope`

`verify(envelope, expected_purpose, expected_binding, now) -> VerifiedPayload`

Provider configuration is injected at service startup; callers never provide a key, algorithm or provider selector. Provider exposes `production_capable` and supported purpose/schema versions; production startup and each protected operation fail closed if the configured provider is not production-capable or unavailable. Verification alone does not grant tenant/operation/capability access. M01 context/entitlement validation remains required independently.

Closed purposes: `m02.cursor.projection/v1`, `m02.cursor.stale/v1`, `m02.cursor.timeline/v1`, `m02.run-manifest/v1`. Cursor purposes are distinct from each other and from manifests. Key material/key namespace is purpose-separated; business-key HMAC material, provider API credentials and tenant dedupe keys are prohibited. A key identifier resolves only through the configured provider's trusted purpose-scoped key registry; envelope data cannot redirect key lookup to a URL or external location.

SignedEnvelope fields exactly: envelope_version=1, purpose, key_id (bounded nonempty identifier <=128 characters), issued_at, expires_at, payload, signature. issued_at is a canonical UTC timestamp. Cursor expires_at is required, later than issued_at, <=900 seconds after it, and subject to the reviewed shorter deployment limit; now >= expires_at is expired. Reject future issued_at (test clock is injected; deployment clock-skew policy must be explicit before execution). Run manifests set expires_at=null because retained evidence is not a bearer pagination authority; any nonnull expiry is enforced. A valid old manifest may prove retained evidence integrity but never authorize rerunning a job. Current run authorization is separate. Test expiry rejection on every cursor purpose and on any envelope with a nonnull expiry.

Payload is a closed typed object for the declared purpose. Signature authenticates a domain-separated canonical byte encoding of every field except signature, including envelope_version, purpose, key_id and timestamps. Canonical encoding v1: compact UTF-8 JSON; object keys sorted by UTF-8 bytes; integers only for numeric fields, no floats/NaN; UTC timestamps as fixed microsecond ISO strings ending Z; UUIDs canonical; arrays retain their purpose-defined order; strings encoded without normalization, with JSON-required escaping. Reject duplicate raw JSON keys, noncanonical encodings and unknown fields before accepting. Signature bytes use canonical unpadded base64url. Key type/algorithm are supplied by trusted provider configuration; no untrusted algorithm header or fallback.

Cursor bindings are derived by the service from the authenticated request, not copied from the unverified envelope:

| Purpose | Exact binding checked |
|---|---|
| projection | tenant_id, selector_digest, sort_definition_version, projection_engine_version, contract_snapshot_digest, view_profile_id, freshness_policy_id |
| stale | tenant_id, cutoff, action_selector_digest, contract_snapshot_digest, sort_definition_version, view_profile_id |
| timeline | tenant_id, subject_type, subject_id, view_profile_id, sort_definition_version, contract_snapshot_digest |
| run manifest | tenant_id, run_id, mode, membership_digest, engine_version, contract_snapshot_digest, limits_profile_id |

Cursor payload adds only the bounded last-page sort key needed by its registered selector. The schema/column name is never caller-supplied. Page size must remain inside the selected profile even with a valid cursor. Signed payloads are not encrypted: no PHI/schema/raw selector body is placed inside. Default/public error paths return controlled invalid/unavailable outcomes, not payload/signature/key details. An invalid token must never become a fresh unbounded query or a regenerated cursor that drops its binding.

Verification order: bounded parse/shape/canonical checks -> supported purpose/envelope/key lookup -> signature verification -> issued/expiry checks -> exact expected binding comparison -> return typed payload. Every failure returns a controlled denial/unavailable result with no query/apply side effect. Key unavailable is fail-closed, not “skip verification.” Unknown/retired verification key behavior is explicit in provider configuration; do not silently choose the active signing key instead.

### Non-production provider specification

`NonProductionFixtureSigner` implements the same two methods, advertises production_capable=false and accepts an injected deterministic clock plus purpose-separated fixture key IDs. It keeps a test-only token-to-canonical-message map; sign returns a deterministic opaque test signature unique to key/purpose/message, and verify requires an exact stored match before applying time/binding checks. This is a protocol test double, not cryptography, persistence or a production signing algorithm. It rejects unknown tokens, mutated bytes, wrong key/purpose, expired cursors and foreign bindings. Production configuration rejects it regardless of whether a fixture signature verifies. Test keys/tokens are synthetic and never business HMAC material.

The test implementation is to be written with authorized implementation, using this frozen interface. A real provider must pass the same contract suite plus algorithm-specific tamper/key tests and custody review before production execution. Algorithm choice, key custody/rotation, archival verifier availability, storage ACL/retention and cursor transport encoding remain the pre-execution gate specified by Claude232. No production provider existence is asserted.

## 3. Targeted test-contract additions

| Condition / cases | Positive | Negative / exact oracle |
|---|---|---|
| P1 / TC10,18 | Requested/rejected/indeterminate with null external_id; accepted verified identity | Nonnull forbidden external_id or unverified accepted ID rejects before mutation |
| P1 / TC10,18,26 | Acceptance conflict has canonical binding A plus required candidate B columns/namespace/scope | Missing candidate fields, absent binding, same tuple or unverified candidate fails; no overwrite |
| P1 / TC10,18,26 | Delivery conflict names >=2 same-operation verified incompatible terminal facts | Missing/one/duplicate/unsorted/foreign/self/unverified/compatible IDs fail; exact competing_event_ids reaches engine |
| P2 / TC06 | requested + accepted(A) + acceptance_conflict(B), provider case selects binding source A | Conflict resolved by authorized affected scope, canonical binding unchanged, nonterminal accepted result; unresolved later contradictory facts still conflict |
| P2 / TC06 | Canonical matching selected accepted fact | Noncanonical B cannot replace binding; explicit unsupported/INTEGRITY_INCOMPLETE, invalid resolution not committed |
| P4 / TC12,23,29 | Valid cursor for each purpose and exact request binding, including restart with a production-provider contract double | Flip payload/signature/key/purpose/version; expired/future-issued token; wrong tenant/selector/cutoff/subject/view/version/snapshot; missing provider; every case denies without widening query |
| P4 / TC14 | Valid signed start/final manifest for matching retained membership/run | Alter membership/digest/mode/tenant/engine; purpose-confuse cursor/manifest; sign/storage failure after partial commits remains partial, not full success |
| P4 configuration | Non-production profile accepts fixture provider | Production startup/operation rejects fixture provider and any attempted business-HMAC key configuration |
| F1 / TC14,24 | RC service and SQL checks agree | Direct apply/finalizer under RR fails inside SQL even if service guard omitted |
| F2 / TC26,29 | Whole envelope assembled in one SQL statement | Concurrent append cannot create a mixed event/link/binding envelope; verify on project PostgreSQL 17 later |
| F4 / grant evidence | Two explicit owner entries + required four column privileges | Extra registry column, missing allowed column or absent owner from assertion scope fails; table-level TC-E22 alone is insufficient |

These are specification cases, not executed tests. The original document checks only verified name/shape/selected fields and union arithmetic; they did not verify all frozen value constraints or executable schema semantics. The updated verification record explicitly narrows that claim.

## 4. Owner decision sheet additions

1. **No-side-effect limitation:** an indeterminate-only operation cannot use this profile's confirmed_no_side_effect rule without a pre-existing rejected fact. No new rejection/status is invented. TC05/06 do not cover the full FR-035 no-side-effect branch. Decide separately whether a production-owned versioned rule must close it before the relevant module release or whether explicit scope deferral is acceptable. It is not implicitly accepted by this synthetic profile.
2. **Signer gates:** SignerProvider interface is required now for CP0; production algorithm, custody/rotation, verification availability and storage controls remain pre-execution gates. The test provider contract is non-production only.
3. **Shared grant control:** existing M01 TC-E22 does not exhaustively cover new M02 owners or column privileges. Add M02 coverage for both owners and exact columns. Issue shared.tenants grants via the owner-run Alembic revision, not a tenant unit. Four-column grant remains proposed; schema-version compatibility belongs to M01, not a hard-coded M02 body set.

All existing owner choices remain on the consolidated sheet. C1–C8 closure is a reviewer disposition on proposals; P1/P2/P4 require delta verification and Rachel's rule-3 alignment still follows. No Word save, code/test implementation, SQL/database execution or Git mutation occurred.
