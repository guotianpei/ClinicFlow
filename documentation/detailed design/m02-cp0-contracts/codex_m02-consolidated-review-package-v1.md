# M02 CP0 — consolidated review package v1

2026-09-10 · Active workflow: ChatGPT/Codex lead · Claude independent reviewer · Rachel owner.
Status: READY FOR INDEPENDENT REVIEW OF PROPOSALS, NOT CP0 FREEZE OR IMPLEMENTATION AUTHORIZATION.
Local source branch last inspected: docs/m02-cp0-source-amendments, 79ca482. No Git mutation or remote action performed.

## 1. What the reviewer is deciding

Review this package once against Claude230 C1–C8 and the seven freeze dependencies. The package supplies concrete tenant binding, synthetic policy/evidence/link definitions, read-field manifests, handoff vocabulary, numeric limits and rebuild semantics. It carries owner choices and deployment dependencies honestly. Please distinguish (a) an incorrect proposed contract, (b) an owner decision ready to present, and (c) a later implementation/operational gate. Do not certify CP0 complete while a required freeze field is still unresolved.

Read in order, all in the September 10 session folder:

1. This main document: final proposed routes, precedence, tests and decisions.
2. codex_m02-contract-decisions-and-synthetic-profile-v1.md: explicit policies, tenant binding, signals, bounds, signed manifest.
3. codex_m02-synthetic-descriptors-v1.json: all 13 synthetic descriptors and two correction/five resolution metadata schemas.
4. codex_m02-read-manifest-v1.md and .json: eight read views with exact outputs/paths and computed read-owner column union.

Retained physical basis: September 9 codex_m02-physical-contract-batch-v2.md, hash 0a006066165cbeba9bc88665f945550c3c5a5dcab2e3596a34265d5d8021c9f0. Its unaffected projection columns, source-set serialization/vectors, event/audit INSERT sets and evidence immutability rules remain. Replace conflicting sections by this package and the two current annexes, not by working-230. Working-230 and source-alignment-addendum-231 are history/context, not additional normative layers. No author edits to Claude notes or accepted repository documents.

Retained test authority: September 9 test-leg-entrypoints-and-cases-v3, route-reconciliation-v2 and coverage-ledger-v1, approved under 225/228/229. Retain TC01–29 and EP01–13; only changes explicitly listed here supersede them. Correction to working-230: TC29 read fallback was already part of the baseline and must not disappear. No test code has been written or run.

## 2. Resolved proposed route graph

All role names use haloflow_ prefix and owner names m02_*_owner. SECURITY DEFINER owners are NOLOGIN, with no business caller membership, PUBLIC EXECUTE or grant option. Existing safe search_path and local-row invariants remain.

| Caller | Registered route | SQL function / behavior |
|---|---|---|
| runtime | m02.operation.lock | m02_lock_operation(uuid) |
| correction | m02.correction.lock | Same m02_lock_operation(uuid), direct EXECUTE grant |
| projection_worker | m02.projection.rebuild.lock | Same m02_lock_operation(uuid), direct EXECUTE grant |
| approved service roles | m02.transaction.isolation.read | Registered READ query returning current_setting('transaction_isolation'); orchestration requires read committed before lock/load on operation mutation/rebuild |
| runtime | m02.append.complete | m02_complete_append(uuid,jsonb,jsonb), append owner -> private finalizer |
| correction | m02.correction.complete | m02_complete_correction(uuid,jsonb,jsonb), correction owner -> private finalizer |
| projection_worker | m02.projection.rebuild.apply | m02_apply_rebuild_projection(p_operation_id uuid,p_result jsonb), projection owner, no handoff input |
| handoff_dispatcher | m02.handoff.claim | m02_claim_handoffs(p_batch_limit integer,p_lease_seconds integer,p_dispatcher_id uuid), outbox owner |
| handoff_dispatcher | m02.handoff.finish | m02_finish_handoff(uuid,uuid,text,text), existing fence/outcome/reason shape |

Retain the other 12 physical-v2 public statement keys unchanged. Total proposed registered keys: the original 18, two role-specific lock aliases, one isolation read = 21. The two proposed lock wrappers are removed. A registered catalogue entry does not automatically issue a capability entitlement; composed deployment contexts must include their required set. Missing isolation capability fails before protected work rather than skipping the assertion.

Private finalizer stays m02_finalize_operation(uuid,jsonb,jsonb), callable only by append/correction owners. It calls an owner-private projection-only helper m02_write_projection(uuid,jsonb), then the approved handoff route. Rebuild apply calls only that projection-only helper. The helper is owned by projection owner, has no external EXECUTE grant and no statement registration; it cannot enqueue. The null-operation m02_enqueue_event_handoffs(uuid,jsonb) remains callable only by append/correction owners and creates no projection. Do not use an empty handoff array as the only protection against replay enqueue if the body can derive other work.

Ordinary appends: M01 context/capability check -> RC assertion -> operation lock (new operation first gets its registry identity) -> purpose-specific immutable append -> complete evidence -> pure fold -> generated conflict evidence/reload/refold as required -> completion -> commit -> success. Null-operation paths retain their approved exception. The same evidence snapshot/lock boundary applies to final source comparison.

Rebuild: retain exact bounded membership and pinned engine/contract profile; one RC lock/load/fold/apply transaction per operation; no adapter or enqueue capability. Mismatch under the held lock is protocol/integrity failure, not an expected race. Stop/report partial state; restart repeats retained membership with fresh per-operation evidence. Dry-run locks/loads/folds/compares without applying. Signed manifests describe per-operation observations, not an atomic range-wide evidence snapshot.

## 3. Service-only attribution envelope

Public correction/resolution command shapes are in the policy annex and cannot include tenant/principal/execution/request overrides. The service constructs the private single-JSON SQL input as exactly `{schema_version:1, command:<validated public command>, attribution:{principal_kind,principal_id,auth_method,execution_id,request_id,purpose_code}}`. request_id may be null; all other attribution fields are required and bounded. principal_kind is actor/workload; principal_id canonical UUID text; execution_id canonical UUID text preserving caller-labelled provenance; purpose_code from the closed approved intersection. Unknown keys rejected. Raw JSON duplicate keys are rejected before parsing into JSONB.

The service derives attribution exclusively from the resolver-issued context and approved purpose mapping. SQL writes the corresponding actor_id/workload_id and audit principal_id/kind from one canonical value and checks any conflicting supplied evidence. It validates source_event_id = correction event_id, action/resource/outcome codes from entry route and retry equivalence. No standalone arbitrary audit append endpoint is added. SQL equality is consistency, not independent authentication. Existing runtime audit DML, malicious trusted-service and privileged-writer limitations remain.

## 4. Result, input and serialization contracts

Retain physical-v2's closed p_result fields: schema_version, projection_engine_version, contract_snapshot_digest, source_event_ids, status, terminal, conflict, indeterminate, controlling_event_ids, superseded_event_ids, explanation_code, contract_versions. IDs are unique canonical UUID sets; source IDs match the whole operation; controlling/superseded sets are disjoint subsets. Contract pairs match stored historical references. SQL computes source summaries/digest and refreshed_at. Shape validation does not prove the trusted engine's semantics.

Read-manifest annex supplies the exact permitted internal/public fields. Metadata paths are selected by rule, not blindly unioned into every envelope. Raw identity/auth fields are excluded from pure-engine input under the proposed append-time authorization interpretation; required historical semantic evidence remains. A missing declared policy/evidence path fails unsupported.

Public pages preserve accepted one-engine-version semantics. Bind the selected version and immutable contract snapshot into the cursor. An item with a different persisted version is compute-only if permitted and affordable; otherwise unavailable. Do not return mixed versions as valid page items. Mixed-version DTO remains an owner alternative, not the recommended package change. Compute-only loads must be coherent and do not mutate projection/outbox state.

UTF-8 byte ordering/COLLATE C replaces ambiguous lexical ordering for strings; UUID bytes and integer ordering remain distinct. Retain source-set digest vectors unchanged. M11 preimage gains handoff_kind and uses only approved code pairs. H3/H4 are escaping stress examples with invalid M01 tenant strings, not successful tenant-binding fixtures; the production path rejects them. Valid-tenant examples and semantic fixture table accompany the package verification record.

## 5. Field and privilege accounting

Projection columns/UPDATE set remain physical-v2. Remove state/attempts/available_at from projection-owner outbox INSERT; their defaults are pending/0/DB clock. Claims/finalization cannot override immutable outbox key/payload/source fields. Lease token remains the fence; canonical service-derived dispatcher identity is attribution only.

Keep physical-v2 event INSERT set E and correction audit INSERT set. Keep exact narrow lock-owner privileges and accepted key timestamp residual. New registry lookup grants are explicitly confined to the two owner roles and four routing columns in the annex, with cross-row routing-metadata reach disclosed. No additional tenant table or principal session-setting authentication is introduced.

The read-owner union is explicit in JSON/Markdown and must be verified against transitive view dependencies and actual grants later. Base-table runtime SELECT is removed only when the complete approved replacement profile is implemented/verified, never merely because this proposal exists. Append/correction/projection-owner SELECT query dependencies still require implementation-level verification against their pinned bodies; this document does not claim a live complete ACL proof.

## 6. Integrated test deltas

Retain baseline valid controls, exact expected identities/results, rollback and tenant separation for every case. No row-count-only substitute. Add these source-traceable deltas to the already approved cases:

| Cases | New concrete assertions |
|---|---|
| TC05/06/17 | Each of two correction rules and five resolution paths; metadata overlay never changes business outcome; exclusion retains original; unknown/resend never fabricate success; incompatible overlapping resolutions remain conflict; invalid scope/evidence/target rejected |
| TC07/08/25 | Complete-history and completion-byte failures roll back all new writes; no silent prefix fold; omitted finalization still detected by service oracle, not falsely rejected by a nonexistent DB membership trigger |
| TC10/18 | All 13 descriptors preserve frozen status/level/class/rank and conditional operation scope; every nested object rejects extra fields; profile version change cannot rewrite historical schema; production rejects active m02_test_ |
| TC11/14/24 | RC assertion before lock/load; correction/worker direct lock ACL and distinct capability; same-operation append commits before rebuilding reader sees evidence; wrong isolation fails before mutation; no wrapper identities remain |
| TC14 | Per-operation apply; retained exact membership on interruption; partial result after later failure; no enqueue through helper/finalizer; signer/run-manifest failure reported truthfully; pinned contract snapshot distinguished from evidence observations |
| TC12/23/29 | One version per page across mixed persisted versions; authorized compute-only or unavailable, no hidden item loss; exact limits/cursor binding; no public escalation to internal evidence |
| TC15 | Bounds 1/100 and 5/300 plus outside values; canonical workload identity; fresh lease token; expired/stale token rejected; attempt 12 terminal handling; prior valid lease may finish; no reset/replay of dead letters |
| TC19/22/28 | Closed M02/M11 mapping and kind-inclusive dedupe; same key retries converge; unknown signal rejected; quoted/non-ASCII tenant rejected; no metadata in payload/errors/logs |
| TC26 | Exact per-rule paths, eight view output/dependency sets, no whole metadata output, whole-column owner exposure disclosed; fixed-schema registry lookup negative matrix; missing tier profile unavailable |
| TC27 | Canonical UUID roundtrip, actor/workload exclusive columns, event/audit equality, arbitrary public attribution denied, purpose intersection and bounded auth/request fields; privileged audit forgery limitation remains characterized |

Keep TC01–04,09,13,16,20–21 and unaffected parts of every other case. TC13 stale anti-join explicitly incorporates corrected/resolved/superseded candidate semantics from the same historical rules, not simply existence of any submission. TC16 provider key and TC21 disposition ownership remain external integration/lifecycle evidence. No implication that drafting these assertions executes them.

## 7. Source issuance and owner decision sheet

Present after consolidated independent review. Every item is a proposal; none has been owner-selected this session.

| Decision | Recommendation and consequence | Source scope |
|---|---|---|
| Tenant binding | Fixed-schema registry lookup; add four-column SELECT to two owner roles, disclose cross-row routing metadata | Design routing/owner/read-grant text; M02 unit/manifests and shared-grant issuance |
| Identity/purpose | Canonical UUID principal v1; M01-controlled purpose domain with M02 action mappings | Identity adapter contract; correction/audit source text |
| Rebuild | RC per-operation publication; signed retained membership and per-operation observations | Design P570, P835, P1125 plus any cross-references; not an atomic range swap |
| Page versions | Preserve existing single page version with compute-only/unavailable items | No mixed-version DTO amendment needed for recommendation; exact fallback contract reviewed |
| Direct locks | Correction/worker EXECUTE on existing lock, distinct statement capabilities | Add two caller Allowed cells to four pending owner-cell amendments; one coordinated issuance |
| Policy profile | Synthetic closed rules and resolver instances; clinical policies remain owner-supplied | OI-007 version/compatibility discipline; no production activation |
| Signals | Two M02-owned code proposals and kind-inclusive M11 identity | Design P584/consumer contract compatibility |
| Limits | Source page/8KiB limits plus proposed unmeasured hard-cap profile | OI-009 and capacity-error rollback/recovery consequence |
| Manifest signer | Preserve signed manifest; finish signer/cursor interface and provisioning before execution | Existing signing requirement; no imaginary deployed signer |

For Word issuance, re-read every exact anchor and amend the combined approved scope only after Rachel authorizes. Verify numbering definitions/list assignments, tables/ToC and rendered pages after any save. Neither this package nor review approval authorizes issuance, implementation, SQL execution, Git actions or production work.

## 8. Honest freeze ledger

| Dependency | Drafting output | Gate still required |
|---|---|---|
| Nested correction/resolution/evidence | Two correction + five resolution schemas, effects, evidence/case refs and graph behavior | Independent semantic review; owner approval; production descriptors separately |
| Principal/purpose | Canonical equality envelope and concrete controlled mapping | Owner identity constraint/governance |
| Public tier/path SELECT | Eight view contracts, exact default/synthetic fields and computed union | Review restricted profile completeness; future clinical/support registration; live query/grant evidence later |
| Rebuild | Exact RC order, membership, apply/helper, publication and signed manifest semantics | Source alignment; signer/interface closure decision |
| Numeric limits | Explicit source defaults and proposed profile with failure behavior | Owner acceptance and later production-like measurement |
| M11 mapping | Two code/kind mappings, destinations and key encoding | Consumer/owner compatibility review |
| Link/seed groups | Six typed resolvers and all 13 descriptor link arrays | Review FK-vs-link semantics and synthetic extension profile |

C1–C8 are proposed addressed, not author-approved closed. C4 now has a selected review candidate and its exact privilege footprint. Signed-manifest and cursor cryptographic interfaces are still unresolved; reviewer must state whether they block CP0 or can be tracked at a later pre-execution gate. Do not mark CP0 complete on an optimistic interpretation. All prior residuals (A omission, audit DML, privileged writer trust, unmeasured OI-009, deferred rotation/heartbeat, CP1–CP9/note168) carry.
