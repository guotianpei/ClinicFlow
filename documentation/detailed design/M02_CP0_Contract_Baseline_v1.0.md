# M02 CP0 Contract Baseline

Owner alignment: Rachel approved decision sheet v2 O1–O12 as recommended on 2026-09-10; recorded by Codex and independently verified by Claude235. This publication becomes issued only when Rachel applies the independently verified issuance package. The recorded decisions govern the requirements, architecture and test-contract baseline; they do not constitute runtime verification or authorize implementation, database, Git or release actions.

## Authority and precedence

The files under m02-cp0-contracts are immutable review snapshots. Their original draft/proposal/review-status labels are retained for provenance; for the exact scopes approved by O1–O12, this index records their subsequent alignment and issuance status. An old “open” label is not permission to invent a resolution; use the explicit later disposition/override below. Unresolved production release and operational gates remain unresolved.

Precedence within this issued package:

1. Owner decision sheet v2 and its independently verified alignment record define the scope and consequences of O1–O12.
2. signer-binding-delta-233 governs signed nontransmitted binding and exclusion of both conflict types from metadata-label targets; targeted-delta-232 supplies the remainder of the SignerProvider interface and targeted test deltas.
3. synthetic-descriptors-v3 is the exact synthetic descriptor/schema set; contract-decisions-and-synthetic-profile-v2 supplies value/semantic/authority rules, with its explicitly labeled final targeted-delta section superseding earlier conflicting passages.
4. read-manifest-v2.md/.json define the proposed-to-issued default/synthetic read outputs, exact paths and declared dependency union, as qualified by the later deltas.
5. consolidated-review-package-v1 provides the integrated route graph, private attribution envelope and test changes; the retained physical-v2 and test/route/coverage baselines supply unchanged detail. Explicit later changes supersede earlier alternatives; there is no implicit approval of discarded wrappers, mixed-version pages, snapshot claims or unsigned binding.

The exact hash table below pins this set. No attachment is silently edited to update a status label. Future changes receive a new revision with an explicit supersession record.

## Decisions preserved

- Fixed-schema tenant routing with exact four-column shared-registry grants to two execution owners, cross-row routing reach disclosed; M01 retains authentication and schema-version compatibility checks. Owner-run Alembic issues shared grants; M02 verifies column-level scope.
- Canonical UUID principal attribution and approved purpose intersection; historical correction authorization is established at append time. Later entitlement revocation does not retroactively suppress valid stored corrections on rebuild. Privileged-writer forgery remains outside assurance.
- Per-operation READ COMMITTED lock/load/fold/apply rebuild with retained membership, no replay enqueue/provider actions and signed truthful run manifests; no range-wide atomic evidence snapshot.
- One version per page, authorized compute-only fallback or explicit unavailable; exact default/synthetic field profiles and minimum necessary engine metadata paths.
- Direct correction/worker lock grants and owner-private finalization; synthetic nested policies, required competing evidence and canonical acceptance resolution.
- The indeterminate-only no-side-effect branch requires a versioned owning-module production rule before release. This suite does not satisfy that branch by fabricating rejection or by implicit scope deferral.
- Provisional unmeasured hard limits can roll back oversized appends and repeatedly fail callbacks; complete-history truncation is forbidden. Signer interface is frozen; production algorithm/custody/rotation/storage and OI-009 measurement remain later gates.
- Service-finalization A and its separately accepted omission residual, runtime audit DML limitation and prior deferred items remain unchanged. No test execution is asserted by document checks.

## Test contract status

EP01–EP13 and TC01–TC29 retain the previously reviewed baseline except the exact integrated and targeted deltas. The suite specifies intended tests and independent review oracles; it is not executable code or evidence of passed implementation tests. Column union arithmetic and descriptor checks establish document consistency only. Required PostgreSQL 17, permissions/body checks, signer implementation tests, consumer compatibility, performance/fairness and release evidence still apply.

## Exact publication manifest

| Artifact | SHA256 |
|---|---|
| [codex_m02-consolidated-review-package-v1.md](m02-cp0-contracts/codex_m02-consolidated-review-package-v1.md) | `64e727081eb6d3b80cb6d59fa6cb73b20398cdabf9e81f5b6cdb839c75883044` |
| [codex_m02-contract-decisions-and-synthetic-profile-v2.md](m02-cp0-contracts/codex_m02-contract-decisions-and-synthetic-profile-v2.md) | `cfa8c3ad9e21cb5b119d381224fb244b276f2d84a804e0a9de6bc6df330a8025` |
| [codex_m02-synthetic-descriptors-v3.json](m02-cp0-contracts/codex_m02-synthetic-descriptors-v3.json) | `903f13713cdaffeefb7b223a39028d4aa1aa40f69faead64fa25aea1b927bf3d` |
| [codex_m02-read-manifest-v2.md](m02-cp0-contracts/codex_m02-read-manifest-v2.md) | `79122935fee8280ac30003eeca07c2a1407ef9d8585eb175793f39267964aa23` |
| [codex_m02-read-manifest-v2.json](m02-cp0-contracts/codex_m02-read-manifest-v2.json) | `bc4e329326b8b61f63c364d09e0ee63aafc167d59bc250c51c9cc9f42f7149fd` |
| [codex_m02-targeted-delta-232.md](m02-cp0-contracts/codex_m02-targeted-delta-232.md) | `4ca8ec6a3a90bbb64c58be8d6ce33cc18f7b1b1cb4bd008c49a4efddb7a9f480` |
| [codex_m02-signer-binding-delta-233.md](m02-cp0-contracts/codex_m02-signer-binding-delta-233.md) | `7782d3cd7f7ad859dfaf940694419f3dfbea7373c9ad5c27ca9c15b3d91cca21` |
| [codex_m02-owner-decision-sheet-v2.md](m02-cp0-contracts/codex_m02-owner-decision-sheet-v2.md) | `21a4f11797d3852dfbea213afa8fda6657232b70fd0314e301af4c7e7c3724ea` |
| [codex_m02-owner-alignment-record-2026-09-10.md](m02-cp0-contracts/codex_m02-owner-alignment-record-2026-09-10.md) | `0c17ebd638f55defdbaf998c2c66ef5455df2f017d78326a35a125add6e95f7d` |
| [codex_m02-physical-contract-batch-v2.md](m02-cp0-contracts/codex_m02-physical-contract-batch-v2.md) | `0a006066165cbeba9bc88665f945550c3c5a5dcab2e3596a34265d5d8021c9f0` |
| [codex_m02-test-leg-entrypoints-and-cases-v3.md](m02-cp0-contracts/codex_m02-test-leg-entrypoints-and-cases-v3.md) | `dcc049e7b83fe42e79240f78c5e074f9f729d016427c461e1a1e1d72c56ae878` |
| [codex_m02-execution-route-reconciliation-v2.md](m02-cp0-contracts/codex_m02-execution-route-reconciliation-v2.md) | `31115e5c108724be2431853f3a79f0a3163d619ef6dc4039429aab662dbd61b1` |
| [codex_m02-test-leg-coverage-ledger-v1.md](m02-cp0-contracts/codex_m02-test-leg-coverage-ledger-v1.md) | `51457220bbd1b2e76779a80532ff984931e73d88a98d8e6d37f0d69af47e6faf` |

## Issuance and change control

Apply only the reviewed exact replacement manifest to the matching live sources, plus the byte-identical files above. Revalidate uniqueness and hashes immediately before any owner save. Do not edit requirements to weaken O7 or collapse O9/O12 consequences. ADR-013 (accepted 2026-09-10, including schema USAGE) and its three ADR-011 supersession notes are issued in the same coordinated change.

After the single Word save, compare numbering definitions and list assignments, table geometry, styles, ToC fields and other non-target parts against the approved baseline and any owner changes. Render and visually inspect the complete saved document. Record the actual resulting hashes and QA disposition; this publication index alone is not proof of successful issuance.
