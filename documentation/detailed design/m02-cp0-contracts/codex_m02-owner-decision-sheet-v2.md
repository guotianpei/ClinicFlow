# M02 CP0 — single owner decision sheet

2026-09-10 · ChatGPT/Codex lead · Rachel owner · Claude independent reviewer.
Status: READY FOR RULE-3 ALIGNMENT; Claude234 closed D1 and requested O12, now included. No reviewer condition remains open. No approval inferred. This sheet consolidates decisions from package v1, annex v2 and deltas 232/233. It does not request code, database, source issuance or Git authorization.

## Review status and exact decision scope

Claude232 closed C1–C8 as proposals. Claude233 closed P1/P2, recorded P3, and approved P4 except D1. Claude234 verified D1 and the two fold-ins; O12 below records his sole decision-sheet omission, with no further review requested. The proposal package is reviewer-cleared for owner alignment. CP0 remains incomplete until the owner alignment and applicable freeze gates are recorded.

Owner approval here means agreement on the following requirements/architecture/test-contract decisions and explicit limitations. Separate issuance and implementation gates still apply. The test contracts are specifications; no M02 runtime, database or production performance verification has run.

## Decisions

| ID | Recommended decision | Consequence / qualification |
|---|---|---|
| O1 Tenant binding | Use fixed-schema shared.tenants lookup, active lifecycle and tenant-setting equality. Grant only tenant_id/schema_key/lifecycle_state/schema_version SELECT to projection_owner and outbox_owner. | These owners gain cross-row routing-metadata reach, not only their function's row. M01 still authenticates context and validates supported schema versions. Shared grants are issued by owner-run Alembic, never a tenant unit. |
| O2 Identity and purpose | Require canonical UUID principal IDs at the M02 v1 boundary; one value populates event and audit attribution. Use the intersection of configured M01 purposes and treatment/payment/operations. | Future identity adapters must satisfy this constraint. Recommend M01 owns purpose domain and M02 owns its action/resource/outcome mappings. SQL equality is not authentication; inherited audit DML risk remains. |
| O3 Rebuild | Fix bounded membership once; use one READ COMMITTED lock/load/fold/apply transaction per operation with per-operation publication and a signed manifest. | Amend design P570/P835/P1125. A run is not a range-wide evidence snapshot or atomic swap. Interrupted retries can see later evidence and must report partial/new observations truthfully. |
| O4 Page versions | Preserve one engine version per page; recompute mismatched items only with existing elevated authorization and budget, otherwise return per-item unavailable. | No mixed-version DTO amendment recommended. No hidden persistence, missing-item suppression or permission escalation on reads. |
| O5 Lock routes and issuance | Give correction/projection-worker direct EXECUTE on existing operation lock with distinct service capability keys; retain owner separation. | Add the two caller Allowed cells to the four pending owner-cell amendments in one coordinated Word issuance, after its own authorization and structural/rendered QA. |
| O6 Synthetic policies | Approve the closed non-production correction/resolution/link profile and exact value constraints, with production clinical rules separately owned/versioned. | Acceptance_conflict additionally requires provider/provider_account/external_id: explicit OI-007 profile refinement. Conflict metadata names competing evidence. Metadata-label rule now excludes both conflict types. No historical descriptor may be rewritten. |
| O7 No-side-effect gap | Record as an open production requirement; require a versioned owning-module rule before releasing the affected FR-035 workflow. Do not invent a rejected status or silently claim coverage. | Current synthetic confirmed_no_side_effect requires a pre-existing rejected fact and does not cover requested+indeterminate-only history. Deferring that release requirement is a separate explicit scope decision, not implied by approving the synthetic suite. |
| O8 Signal contract | Approve the proposed M02 emitted code/kind map and kind-inclusive M11 dedupe identity, subject to consumer compatibility. | Align design P584; source IDs remain structural internal identifiers. Required-list completeness still relies on accepted service-finalization A. |
| O9 Limits | Accept the proposed hard-limit profile as provisional, unmeasured defaults, retaining the design's existing page/metadata limits. | New key values: 1,000 events / 32MiB full fold, 64KiB public command, 1MiB completion, 500-operation rebuild, claims 1–100, leases 5–300s, 12 attempts. Exceeding complete-history/completion caps rolls back the append and can cause repeated callback failure until remediation. No truncation and no production capacity guarantee. |
| O10 Signing | Freeze SignerProvider interface with purpose-separated keys, signed nontransmitted binding, expiry/tamper rejection, test-only provider and production fail-closed. | Algorithm/custody/rotation/storage and archival verification availability are pre-execution gates. Never reuse business-key HMAC material. Client cursors do not expose binding values; internal manifests retain their required classified content. |
| O11 Grant verification | Add both M02 owner roles and exact column-level shared-registry assertions to M02 verification. | Existing M01 TC-E22 checks manifest-listed roles/table privileges and is insufficient alone. This is a disclosed coverage gap, not evidence that new grants are safe because old tests passed. |
| O12 Historical authorization | Establish correction/resolution authorization at append time; the engine and rebuild validate stored graph and references under exact historical rules without rerunning current entitlement checks. | A correction or resolution appended under a later-revoked entitlement still applies on rebuild. Forged stored evidence from a privileged writer remains outside assurance. This explicitly interprets design §9.1 step 3 and requires owner approval. |

## Limits detail for O9

Existing accepted defaults: event metadata 8KiB; operation-ID selector 200; batch/run page 500; queue/stale page 200. New proposals: timeline page 200; 32 links/event; 1,000 complete-fold events and 32MiB encoded envelope (both caps); 1MiB completion; 2,000/1MiB handoff list; 500 operation IDs/run with one active operation per tenant worker; dispatcher batch 100 max, lease default 30 seconds within 5–300; 12 total claims; retry delay min(300,2^(attempts-1)) seconds; cursor <=900 seconds/default300. Evidence refs 1–8; resolution affected IDs and conflict competing IDs at most16 per rule payload. Source history remains complete even when one conflict record identifies a concrete pair/cluster. All new values require later representative OI-009 validation and controlled revision if unsuitable.

## Read/field profile decision boundary

Default/synthetic read profiles expose exactly the eight reviewed view inventories in read-manifest v2. Production clinical/support extensions require separately registered exact field/path profiles and owner entitlements; an unregistered profile returns unavailable. Whole metadata column reach at the definer owner is disclosed even when returned paths are restricted. Document union arithmetic is not a catalog/grant proof. Actual bodies, views, transitive dependencies and effective privileges must match in implementation verification.

## Decisions already made — do not reopen implicitly

Service finalization A selected; its omitted-finalization/handoff residual separately accepted. B membership trigger not pursued; C broader enforcement deferred. Existing runtime audit DML and privileged service/writer trust limits remain visible. Unmeasured OI-009, deferred key rotation/producer heartbeat, CP1–CP9 and note168 limitations carry. This sheet grants no additional risk acceptance by omission.

## Record after owner alignment

Record each O1–O12 disposition (approved / modified / deferred with scope and release gate), the exact current artifact hashes, and any resulting source amendment instructions. Do not reduce O7 or O9 to generic approval text that hides the unresolved requirement or availability consequence. If any decision changes a reviewed contract, request only the necessary targeted re-review. Source issuance, code/tests, database execution, commit/push/merge and release remain separately controlled.

## Current artifact set

- codex_m02-consolidated-review-package-v1.md (retained basis)
- codex_m02-contract-decisions-and-synthetic-profile-v2.md
- codex_m02-synthetic-descriptors-v3.json (v2 plus two label-target removals)
- codex_m02-read-manifest-v2.md and .json
- codex_m02-targeted-delta-232.md with codex_m02-signer-binding-delta-233.md taking precedence for D1 and label targets
- Claude232, Claude233 and Claude234 (D1 closed; O12 addition requested without re-review)

These are review/decision documents, not issued sources or executed release evidence.

## Artifact fingerprint record for alignment

- codex_m02-consolidated-review-package-v1.md: `64e727081eb6d3b80cb6d59fa6cb73b20398cdabf9e81f5b6cdb839c75883044`
- codex_m02-contract-decisions-and-synthetic-profile-v2.md: `cfa8c3ad9e21cb5b119d381224fb244b276f2d84a804e0a9de6bc6df330a8025`
- codex_m02-synthetic-descriptors-v3.json: `903f13713cdaffeefb7b223a39028d4aa1aa40f69faead64fa25aea1b927bf3d`
- codex_m02-read-manifest-v2.md: `79122935fee8280ac30003eeca07c2a1407ef9d8585eb175793f39267964aa23`
- codex_m02-read-manifest-v2.json: `bc4e329326b8b61f63c364d09e0ee63aafc167d59bc250c51c9cc9f42f7149fd`
- codex_m02-targeted-delta-232.md: `4ca8ec6a3a90bbb64c58be8d6ce33cc18f7b1b1cb4bd008c49a4efddb7a9f480`
- codex_m02-signer-binding-delta-233.md: `7782d3cd7f7ad859dfaf940694419f3dfbea7373c9ad5c27ca9c15b3d91cca21`
