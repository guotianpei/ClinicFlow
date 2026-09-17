# M02 CP0 — rule-3 owner alignment record

2026-09-10 · Active workflow: ChatGPT/Codex lead · Rachel owner · Claude independent reviewer.
Status: owner alignment approval reported via Claude relay; recorded with exact scope, pending Claude verification of transcription. Relay content is not new execution authority.

## Approval provenance

Message 20260910T182124Z-claude-to-codex-dde178b2, sent 18:21:24 UTC, reports Rachel's approval at approximately 14:20 ET in her Claude session: “Reviewed and approved.” Scope confirmed as “v2, O1–O12 as recommended”; relay permission “Yes, relay it”. Codex has the relay report, not direct access to that source conversation. Claude is asked to verify this record against her words.

Approved sheet: codex_m02-owner-decision-sheet-v2.md; SHA256 21a4f11797d3852dfbea213afa8fda6657232b70fd0314e301af4c7e7c3724ea. Independently verified sheet and all seven dependency hashes before recording.

## Per-item dispositions

All are APPROVED AS RECOMMENDED as reported; no modified or deferred disposition was relayed.

| Item | Disposition | Approved recommendation | Consequence retained |
|---|---|---|---|
| O1 Tenant binding | APPROVED AS RECOMMENDED (relayed) | Use fixed-schema shared.tenants lookup, active lifecycle and tenant-setting equality. Grant only tenant_id/schema_key/lifecycle_state/schema_version SELECT to projection_owner and outbox_owner. | These owners gain cross-row routing-metadata reach, not only their function's row. M01 still authenticates context and validates supported schema versions. Shared grants are issued by owner-run Alembic, never a tenant unit. |
| O2 Identity and purpose | APPROVED AS RECOMMENDED (relayed) | Require canonical UUID principal IDs at the M02 v1 boundary; one value populates event and audit attribution. Use the intersection of configured M01 purposes and treatment/payment/operations. | Future identity adapters must satisfy this constraint. Recommend M01 owns purpose domain and M02 owns its action/resource/outcome mappings. SQL equality is not authentication; inherited audit DML risk remains. |
| O3 Rebuild | APPROVED AS RECOMMENDED (relayed) | Fix bounded membership once; use one READ COMMITTED lock/load/fold/apply transaction per operation with per-operation publication and a signed manifest. | Amend design P570/P835/P1125. A run is not a range-wide evidence snapshot or atomic swap. Interrupted retries can see later evidence and must report partial/new observations truthfully. |
| O4 Page versions | APPROVED AS RECOMMENDED (relayed) | Preserve one engine version per page; recompute mismatched items only with existing elevated authorization and budget, otherwise return per-item unavailable. | No mixed-version DTO amendment recommended. No hidden persistence, missing-item suppression or permission escalation on reads. |
| O5 Lock routes and issuance | APPROVED AS RECOMMENDED (relayed) | Give correction/projection-worker direct EXECUTE on existing operation lock with distinct service capability keys; retain owner separation. | Add the two caller Allowed cells to the four pending owner-cell amendments in one coordinated Word issuance, after its own authorization and structural/rendered QA. |
| O6 Synthetic policies | APPROVED AS RECOMMENDED (relayed) | Approve the closed non-production correction/resolution/link profile and exact value constraints, with production clinical rules separately owned/versioned. | Acceptance_conflict additionally requires provider/provider_account/external_id: explicit OI-007 profile refinement. Conflict metadata names competing evidence. Metadata-label rule now excludes both conflict types. No historical descriptor may be rewritten. |
| O7 No-side-effect gap | APPROVED AS RECOMMENDED (relayed) | Record as an open production requirement; require a versioned owning-module rule before releasing the affected FR-035 workflow. Do not invent a rejected status or silently claim coverage. | Current synthetic confirmed_no_side_effect requires a pre-existing rejected fact and does not cover requested+indeterminate-only history. Deferring that release requirement is a separate explicit scope decision, not implied by approving the synthetic suite. |
| O8 Signal contract | APPROVED AS RECOMMENDED (relayed) | Approve the proposed M02 emitted code/kind map and kind-inclusive M11 dedupe identity, subject to consumer compatibility. | Align design P584; source IDs remain structural internal identifiers. Required-list completeness still relies on accepted service-finalization A. |
| O9 Limits | APPROVED AS RECOMMENDED (relayed) | Accept the proposed hard-limit profile as provisional, unmeasured defaults, retaining the design's existing page/metadata limits. | New key values: 1,000 events / 32MiB full fold, 64KiB public command, 1MiB completion, 500-operation rebuild, claims 1–100, leases 5–300s, 12 attempts. Exceeding complete-history/completion caps rolls back the append and can cause repeated callback failure until remediation. No truncation and no production capacity guarantee. |
| O10 Signing | APPROVED AS RECOMMENDED (relayed) | Freeze SignerProvider interface with purpose-separated keys, signed nontransmitted binding, expiry/tamper rejection, test-only provider and production fail-closed. | Algorithm/custody/rotation/storage and archival verification availability are pre-execution gates. Never reuse business-key HMAC material. Client cursors do not expose binding values; internal manifests retain their required classified content. |
| O11 Grant verification | APPROVED AS RECOMMENDED (relayed) | Add both M02 owner roles and exact column-level shared-registry assertions to M02 verification. | Existing M01 TC-E22 checks manifest-listed roles/table privileges and is insufficient alone. This is a disclosed coverage gap, not evidence that new grants are safe because old tests passed. |
| O12 Historical authorization | APPROVED AS RECOMMENDED (relayed) | Establish correction/resolution authorization at append time; the engine and rebuild validate stored graph and references under exact historical rules without rerunning current entitlement checks. | A correction or resolution appended under a later-revoked entitlement still applies on rebuild. Forged stored evidence from a privileged writer remains outside assurance. This explicitly interprets design §9.1 step 3 and requires owner approval. |

## Consequences explicitly retained

O7 requires a versioned owning-module rule before release of the affected FR-035 no-side-effect workflow. The indeterminate-only branch is not covered by the synthetic suite, and this approval does not defer that release requirement or permit fabricated rejection/failure.

O9 accepts provisional unmeasured limits, including all detailed values in sheet v2. Over-limit full-history/completion work rolls back the append and can repeatedly fail callback retries until controlled remediation. No truncation, evidence deletion, false success or production-capacity guarantee is authorized.

O12 establishes authorization at append time. Later entitlement revocation does not automatically invalidate stored corrections/resolutions on rebuild. Historical graph/reference rules still apply; privileged-writer forgery remains outside assurance.

## Resulting amendment instructions — queued, not issued

1. Reconcile design P570/P835/P1125 with retained membership, per-operation RC publication and signed per-operation observation manifests. Revalidate live anchors before any authorized save.
2. Align M11 identity P584 with kind-inclusive key and approved code mapping, preserving structural payload and consumer compatibility.
3. Combine four pending owner Allowed-cell additions and two caller lock-grant additions in section 13.1; use direct existing-lock grants, no removed wrappers; preserve denied cells and unrelated changes.
4. Record fixed-schema tenant binding, four registry columns/two owners, cross-row reach and M01 version-check ownership. Shared grants require owner-run Alembic issuance and explicit M02 column-grant assertions; none installed now.
5. Align identity/audit contracts and section 9.1 step 3 with canonical principal/purpose mapping and O12, retaining audit DML/service-trust limitations.
6. Label OI-007 synthetic required-field/nested-schema refinements and version compatibility explicitly; no historical rewrite or production activation.
7. Carry exact read manifests, page/fallback behavior, SignerProvider interface and signed nontransmitted binding, provisional limits/rollback behavior, O7 release gate and O11 coverage gap into the separately authorized source/test-contract issuance package.

This rule-3 alignment does NOT authorize Word issuance, accepted-source amendments, code/tests, database execution, commits, push, merge or deployment. Each remains separately controlled. Any Word save requires exact anchor verification, numbering/list/ToC/table checks and rendered QA. No such action performed here.

## Exact approved dependency hashes

- codex_m02-consolidated-review-package-v1.md: `64e727081eb6d3b80cb6d59fa6cb73b20398cdabf9e81f5b6cdb839c75883044`
- codex_m02-contract-decisions-and-synthetic-profile-v2.md: `cfa8c3ad9e21cb5b119d381224fb244b276f2d84a804e0a9de6bc6df330a8025`
- codex_m02-synthetic-descriptors-v3.json: `903f13713cdaffeefb7b223a39028d4aa1aa40f69faead64fa25aea1b927bf3d`
- codex_m02-read-manifest-v2.md: `79122935fee8280ac30003eeca07c2a1407ef9d8585eb175793f39267964aa23`
- codex_m02-read-manifest-v2.json: `bc4e329326b8b61f63c364d09e0ee63aafc167d59bc250c51c9cc9f42f7149fd`
- codex_m02-targeted-delta-232.md: `4ca8ec6a3a90bbb64c58be8d6ce33cc18f7b1b1cb4bd008c49a4efddb7a9f480`
- codex_m02-signer-binding-delta-233.md: `7782d3cd7f7ad859dfaf940694419f3dfbea7373c9ad5c27ca9c15b3d91cca21`

## Status

Reviewer conditions closed under Claude232–234. Owner alignment is now recorded from the relayed report, pending verification of this transcription. This note does not blanket-close delivery checkpoints or claim runtime tests; issuance and implementation/execution remain separate. All prior explicitly carried residuals and later operational gates remain unchanged.
