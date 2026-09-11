# M02 CP0 — service inventory and finalization test cases v3

2026-09-09 · ChatGPT/Codex lead · Codex drafting; Claude independent review requested.
Status: test-leg draft, not test code, executed evidence, test freeze or completed rule-3 alignment. A selected and omission residual accepted as reported in Claude219. Claude220 sequences this leg before remaining physical-schema drafting and records one alignment over the finished package. No new execution authority inferred.

## Source basis and limits

Read: requirements v1.0 FR-001–035 excerpts; approved design v0.3 §§5–7 public/transaction interfaces and §9.3 rebuild excerpt, using prior extracted text in Documents/Codex/2026-09-07/haloflow-m02/work; remaining-contracts-v3 §§1–7; finalization-and-grant-contract-v1; owner comparison v3; Claude219/220. Later A selection supersedes every B/C deferred-completeness-check proposal. Grant routing from finalization-and-grant-contract-v1 is retained as recorded approved in the handoff, not its superseded mechanism.

This enumerates the public inventory in design §6 and proposed worker inventory in remaining-contracts-v3 §3. It does not certify future implementation routes; the v2 source reconciliation below establishes the current production catalogue contains no M02 registrations, so this is a proposed inventory rather than an existing-call-site audit. CP0 closure requires a crosswalk against design §6 public methods, remaining-contracts-v3 §3 worker routes and each FR-mandated write path, with a named execution owner and case for each. The production statement/call-site crosswalk is a later implementation-verification obligation. Missing signatures/capability names remain architecture dependencies rather than invented facts. Requirement mappings below are draft specification traceability, not links to implemented tests. The earlier extracted text has not been freshly compared against Word sources in this step; source/amendment read-back remains a package obligation.

## Enumerated service inventory

| ID | Public or worker entry point | Proposed SQL route | Named cases | Requirement basis |
|---|---|---|---|---|
| EP01 | EventFoundation.begin_operation | m02_begin_operation | TC01, TC07–11 | FR-001–006, 011, 024; design §§6.2,7.1 |
| EP02 | record_submission_outcome | m02_append_submission | TC02, TC07–11 | FR-007, 011, 016, 022, 029,030 |
| EP03 | record_outcome | m02_append_outcome | TC03, TC07–11 | FR-008, 014,015, 022,030 |
| EP04 | record_observation | m02_append_observation | TC04, TC07–10 | FR-009,011,012,024 |
| EP05 | append_correction | m02_append_correction | TC05, TC07–11 | FR-010,017,024,035; draft correction audit contract |
| EP06 | record_manual_resolution | m02_resolve_operation | TC06, TC07–11 | FR-017,024,035 |
| EP07 | get_operation_projection | m02_get_projection | TC12 | FR-014,024; design §9.3 |
| EP08 | list_operation_projections | m02_list_projections | TC12 | FR-024,033 |
| EP09 | list_stale_intents | m02_list_stale_intents | TC13 | FR-016,024; design §6.1 |
| EP10 | list_subject_timeline | m02_list_timeline | TC12 | FR-010,017,020,024 |
| EP11 | projection worker rebuild (Python name not frozen) | m02_rebuild_projections | TC14 | FR-014; design §9.3 |
| EP12 | handoff dispatcher claim (Python name not frozen) | m02_claim_handoffs | TC15 | FR-028; remaining-contracts-v3 RC09 |
| EP13 | handoff dispatcher finish (Python name not frozen) | m02_finish_handoff | TC15 | FR-028; remaining-contracts-v3 RC09 |

Internal helpers, not additional public entry points: H01 m02_begin_key_scope, H02 m02_lock_operation, H03 m02_finalize_operation. H03 is proposed executable only by append/correction execution owners; worker rebuild is separately routed. Exact orchestration across those execution roles must be resolved by the later signature/grant work. Inventorying the route is not proof that the route is currently callable.

No generic execute-operation endpoint. Stale-intent discovery is read-only; materializing an indeterminate fact routes through EP02 with its normalized outcome contract. Resend uses EP01 under the authorized owning-module resend rule, never a hidden resend path. Adapter provider calls, M05/M06 consumer lifecycles, key rotation and privileged archival are not extra M02 service methods. Their boundary obligations remain in the full package; their exclusion here is not deletion of requirements.

## Test contract and cases

All fixtures are synthetic. Every case begins from a valid context, registered producer and supported contract with known expected evidence. For each negative variant, alter one cause and retain a positive control. Assertions inspect exact event identity, links, projection result, expected deterministic handoff keys, transaction outcome and ordering; row counts alone are insufficient. IDs below are proposed test-case IDs, not code filenames.

**TC01 — committed intent before external action (EP01).** Start with no business identity; expect one registry identity, requested event, links, requested projection and required structural signal/outbox work in one transaction. Do not expose OperationStart or invoke the adapter before commit. Failure before commit leaves none of the newly written records and no provider call. Existing identity branch returns the same operation and preserves existing history. Resend variant requires distinct identity, authorized linkage and the full intent-before-call sequence. Basis FR-001–006/035 and design §7.1.

**TC02 — submission branches (EP02).** Separate accepted, rejected and indeterminate fixtures. Accepted creates or reuses canonical binding; competing identity preserves the original binding, adds conflict evidence, projects acceptance conflict and creates the deterministic M05 handoff. Rejected stays a submission rejection and produces the contract-required failure handoff; indeterminate stays nonterminal and produces the M06 reconciliation trigger. Never represent rejection as accepted plus delivery failure. Basis FR-007/016/029/030.

**TC03 — verified outcome branches (EP03).** Verified delivery/business success and failure; out-of-order compatible fact; contradictory verified terminal facts. Preserve all immutable evidence; recompute from the complete set; assert conflict precedence and required M05 handoff for conflict or actionable failure. Unverified evidence fails before mutation. Basis FR-008/014/015/030.

**TC04 — observation plus domain state (EP04).** Null-operation observation with authorized subject/source and callback: event, links, accepted domain state and any contract-required durable work commit together; no synthetic intent or operation projection. Inject callback failure and assert full rollback. Reject nested tenant gateways and tenant switching. An operation-linked observation, if an approved event contract allows it, must have a separately enumerated fixture and projection obligations; do not silently infer it from the nullable SQL field. Basis FR-009/012, design §6.2.

**TC05 — correction (EP05).** Separate metadata-only and asserted-fact variants. Original remains queryable; validate same-operation target, reason, evidence and correction authority. Audit append and correction transaction succeed together; audit failure rolls everything back. Recompute applicable projection and required handoffs from corrected evidence. Null-operation correction applicability and exact existing audit callable must be bound to source contracts before this case can close. Basis FR-017/024/035 and remaining-contracts-v3 §4.

**TC06 — manual resolution (EP06).** Authorized evidence-bearing resolution and closing without authoritative outcome are separate variants; the latter must not falsely project failure. Stale/foreign targets fail; original evidence remains immutable. Resend authorization is routed to TC01, not performed by this method. Basis FR-035.

**TC07 — failure matrix (each EP01–06 independently).** For every applicable branch above, inject failure after first evidence write, after links/binding where present, during fold, during finalizer, during required handoff/audit/domain write, and at definite commit failure. Assert no successful result escapes and all new transactional writes disappear while prior committed evidence remains. Mark an inapplicable stage with a reason, not an unexecuted success. Unit cases prove orchestration with a transaction double; later PostgreSQL integration cases separately prove rollback and permission behavior. No database execution is performed by this document.

TC07 requirement basis: NFR-001 defines success as committed authoritative transaction; FR-002 requires committed intent before external execution; FR-009 requires observation and accepted domain state transactionally together; FR-028 requires a crash-durable handoff trigger, with design §10.2 selecting the atomic outbox path (FR-028 also permits a scheduled-detector alternative, which is not silently substituted here). FR-014/017/035 define projection and correction/resolution results. Design §5.3's final validation step and §§6.2/9.3 specify common transaction and projection completion. Rollback of those writes on definite failure follows this transaction design; no FR is misquoted as mandating a database completeness trigger. Correction audit-route details remain separately under review.

**TC08 — omission regression (each EP01–06 independently).** A later test-only altered service implementation skips required completion or required durable work, or returns success before commit. The expected test must fail against that altered implementation by comparing the complete required result and ordering; restored implementation must pass. This is evidence that the service regression test detects the omission, NOT an assertion that A's database rejects an incomplete transaction. Required handoff expectations come from the reviewed event contract fixture, not copied from the implementation output.

TC08 requirement basis: the same required-result obligations as TC07 (FR-002/009/014/017/028/035, NFR-001; design §§5.3/6.2/9.3/10.2), exercised negatively by deliberately omitting one required step. Mutation/omission testing is the proposed verification technique from comparison v3's mitigation, not an independently stated FR or a database-enforcement requirement. Thus the requirement is complete correct service behavior, while the accepted residual concerns how completely review/tests enforce that behavior across erroneous paths.

**TC09 — ambiguous commit (EP01–06).** Simulate commit acknowledgement loss separately from definite rollback. Return APPEND_COMMIT_UNKNOWN with the original lookup identity; do not claim rollback or return ordinary success. Lookup resolves whether the same event/dedupe identity committed before retry; retry never fabricates another logical identity. Basis design §6.2, FR-003/005/011. Provider reconciliation behavior outside M02 remains separately owned.

**TC10 — validation and duplicate paths (EP01–06).** Invalid/expired context, wrong capability/producer, foreign subject/operation and malformed contract/metadata fail before mutation. Equivalent duplicate returns original identity without a new fact or duplicate handoff; changed immutable content under the same dedupe identity fails with controlled conflict. Exercise tenant separation. Basis FR-005/011/018–020/024. Exact per-entry capability mapping remains open.

**TC11 — concurrent operation changes (EP01–03,05–06).** Later integration tests use barriers for same-operation competing facts and an unrelated-operation control. Assert complete-set fold after serialization, preserved evidence, canonical binding and deterministic handoffs; locks release on commit and rollback. For multiple-operation paths use the reviewed lock order. Separate unit fold permutations from database lock evidence. Basis FR-011/014/015/029, existing RC04. Include EP04 only if an approved operation-linked contract establishes applicability.

**TC12 — projection/timeline reads (EP07,08,10).** Validate tenant, bounded selectors, cursor binding, view capability and field filtering; missing/stale projection follows explicit consistency mode. Read results must not trigger providers or new handoffs. If a mode persists a refreshed projection, that write route must be explicitly added to the inventory and tested; it is not assumed here. Basis FR-014/020/024/033 and design §9.3. Exact modes and field inventories are separate closure dependencies.

**TC13 — stale discovery (EP09).** Authoritative anti-join returns eligible intent with no qualifying submission, with stable bounded pagination. Discovery neither appends indeterminate evidence nor mutates a projection; any later materialization uses EP02. Basis FR-016 and design §6.1.

**TC14 — rebuild (EP11).** Complete bounded range, supported engine and consistent snapshot; compare full-range result with live fold. Interrupted rebuild restarts the declared range. No provider call or new downstream delivery; replay/dry-run compares deterministic handoff expectations without multiplying them. Derived state only is changed. Basis FR-014 and design §9.3.

**TC15 — handoff lifecycle (EP12–13).** Claim, expiry, reclaim and stale-token finish; stale worker changes zero records and current worker finishes once. Consumer acknowledgement loss and retry converge to one effective consumer record by deterministic tenant key. Unit state-machine/fencing cases and later transaction/concurrency/consumer contract evidence are distinct. Basis FR-028 and RC09; exact bounds/identity source remain physical-contract dependencies.

## Superseded test claims and closure checklist

Remaining-contracts-v3 RC06 and finalization-and-grant-contract-v1's expectation that omitted finalization rejects commit are superseded by A. Replace them in the assembled test contract with TC07/08/09, without editing their historical files. Explicitly preserve the accepted residual: erroneous authorized paths outside tested coverage can still commit incomplete business work.

Before declaring the CP0 entry-point set closed: reconcile the ten §6 methods, three worker routes and FR-mandated write paths with named execution owners and cases; retain registered-statement/producer-code equality as later implementation verification; resolve EP04/05 null-operation applicability, EP07/08 cache-write behavior, exact audit call and H03 execution-role route; enumerate each contract branch and required handoff fixture; map each row to exact amended-source anchors and eventual test evidence IDs. These are visible gaps, not a claim of complete traceability. Continue the test leg before physical-schema design.

One owner alignment remains planned over the complete requirements/design/test-case package. Issuance before or alongside alignment is reasonable once exact source comparison/read-back and rendered QA are ready and Rachel authorizes issuance; this draft does not issue sources. The relay's mention of deleting _to_delete supplies no deletion authorization here. CP0 incomplete; no code, tests, database, dependencies, accepted sources, Git or deployment changed.


## V2 source reconciliation and closed applicability

Fresh Word XML read (zero-based paragraph indices include paragraphs inside tables): requirements SHA-256 0aaf8f8053d146f5a57b6bc0451e4a86e5d0fb5b9ee3472b844bbab6db88a562; design SHA-256 0b4b547d556364a213a75224ec09789570278f80ee240b3a69f46c4c63c5018e. Design p386 confirms all ten public methods, p417 the callback/transaction boundary, p418 the commit-unknown contract, p501 cache fallback, p502 bounded rebuild. Requirements FR-NNN maps to paragraph 119 + 2*(NNN-1), through FR-035 p187. These are text provenance anchors, not rendered-QA evidence or source issuance.

Frozen OI-007 §§1,5,6 resolves EP04/05: m02_test_observation_received has correlation_id only, no operation_id; m02_test_correction requires operation_id exactly when its target is operation-scoped. Therefore add null-operation correction as a mandatory EP05 branch: original observation and its correction remain queryable, same-subject/tenant validation and audit are required, and no operation projection or invented operation is produced. Operation-linked observation is not in the frozen foundation fixture set. A module extension may declare an observation projection rule under OI-007 §1; it must extend this inventory and fixtures before producer enablement. This is an explicit extension boundary, not silent omission.

For EP07/08, propose the read fallback computes and returns without persisting cache state; otherwise explicit unavailable. Persisted refresh stays with EP11. This uses the alternative permitted by design p501 and the read owner's no-mutation contract, but is a concrete policy proposal for independent review, not an already frozen consistency enum. Test both paths with stale/missing cache: no derived row change, no new handoff, no provider call. Complete-evidence retrieval for the in-memory fold still needs a reviewed internal read route; tier views omit data needed by the engine.

Static repository evidence: composition.py composes only M01_STATEMENTS; statements.py declares that mapping empty. There is no implemented M02 EventFoundation or registered M02 production statement in src. units.py t001 defines access_audit_outbox but no registered audit-append callable exists in the inspected production tree. Do not describe the table as an existing callable interface. Exact audit route must be designed and reviewed; no direct shared audit write is proposed. Existing gateway executes registered SQL under its connection role; returning from a SECURITY DEFINER call does not leave its owner active for the next Python-issued statement. A finalizer callable only by append/correction owners consequently needs an explicit owner-wrapper route after the pure service fold. That route is currently missing from the proposed inventory.

## Concrete branch fixtures

All event fixtures use frozen OI-007 §6 m02_test_ contracts in a non-production generation. Case IDs below expand TC01–06 and are mandatory independently, not optional examples. Source links and provider namespaces use distinct synthetic values; exact link-type declarations must follow the fixture contract, never real patient data.

| Branch | Entry | Event fixture / prior state | Expected completion |
|---|---|---|---|
| F01 | EP01 | m02_test_requested, new business key | requested projection; committed OperationStart |
| F02 | EP01 | same business key and existing requested | same operation, no duplicate equivalent fact |
| F03 | EP01 | authorized resend of indeterminate operation | new operation and resend link, original unchanged |
| F04 | EP02 | m02_test_accepted, verified identity | original canonical binding retained/created; accepted projection |
| F05 | EP02 | equivalent acceptance retry | same effective fact/binding; no duplicate work |
| F06 | EP02 | competing identity | acceptance_conflict evidence, nonterminal conflict, M05 durable key |
| F07 | EP02 | m02_test_rejected with actionable failure rule | rejected projection and M05 durable key |
| F08 | EP02 | m02_test_submission_indeterminate | indeterminate projection, M06 durable key, no resend |
| F09 | EP03 | m02_test_delivered | verified terminal success |
| F10 | EP03 | m02_test_delivery_failed with actionable rule | terminal failure and M05 durable key |
| F11 | EP03 | m02_test_business_succeeded | verified business success |
| F12 | EP03 | m02_test_business_failed with actionable rule | business failure and M05 durable key |
| F13 | EP03 | delivered plus delivery_failed | delivery_conflict evidence, nonterminal conflict and M05 key |
| F14 | EP03 | delivered plus business_failed | same-tier incompatible-contract conflict, M05 key |
| F15 | EP03 | compatible late evidence after terminal outcome | deterministic fold; receipt order does not regress state |
| F16 | EP04 | m02_test_observation_received plus accepted callback state | event and domain state together, no projection |
| F17 | EP05 | m02_test_correction metadata-only, operation target | original intact, authorized metadata supersession and audit |
| F18 | EP05 | m02_test_correction asserted-fact, operation target | corrected fold, audit, contract-required work |
| F19 | EP05 | m02_test_correction targeting F16 | correction and audit, no operation/projection |
| F20 | EP06 | m02_test_resolution with authoritative evidence | attributable resolution and recomputed state |
| F21 | EP06 | close case without authoritative outcome | no invented failure or terminal outcome |

Each F branch receives its own validation/duplicate/definite-failure/ambiguous-commit cases, with identical identifiers reused after uncertainty. F02/F05 may have no new evidence; test that duplicate paths cannot bypass validation and cannot falsely claim previously missing work exists. Any repair behavior requires an explicit service contract, not automatic extra insertion inferred by the test.

Failure stages for each branch: S1 after new event, S2 after links/binding, S3 during applicable fold, S4 during applicable projection persistence, S5 during contract-required handoff/audit/domain mutation, S6 definite commit failure, S7 lost commit acknowledgement. F16/F19 mark S3/S4 not applicable because they lack an operation; duplicate-only F02/F05 mark absent write stages not applicable but still exercise S6/S7. Split S5 by each required participant. Omission variants independently skip projection, each required durable work item, audit, domain callback, or commit wait wherever applicable; the corresponding oracle must fail. Never assert SQL rejection of omission under A.

For fixtures requiring M05/M06 work, the event contract/rule fixture defines a named destination, triggering evidence IDs, controlled reason and deterministic key expectation. The exact key serialization is not invented here; until pinned it is an open oracle dependency. Repeated dispatch may execute more than once, but the consumer's effective record must converge to one. A test double proves local orchestration only, not shared consumer integration.

## Requirements not discharged by TC01–15

| Requirement group | Additional named case | Required oracle / ownership |
|---|---|---|
| FR-004 | TC16 provider key boundary | Notifyre adapter receives exact operation UUID on first call/retry/takeover; M02 provides committed identity, adapter owner supplies integration fixture |
| FR-013/014 | TC17 temporal and fold permutations | UTC/source quality distinctions; equal-time tie handling, arrival/commit permutations and sequence gaps do not alter set fold |
| FR-018/019/032 | TC18 seed and catalogue compatibility | all 13 frozen test contracts, all startup failure classes, manifest equality and no duplicated ref_event_types infrastructure row; retire blocks append without losing history |
| FR-020/025/034 | TC19 telemetry isolation | prohibited synthetic sentinels absent from signal/errors; post-commit signal failure cannot undo commit or repeat action; latency evidence remains separately unmeasured |
| FR-021/031 | TC20 correlation provenance | same operation across changed requests; changed operation under same request; arbitrary public header not promoted to trusted provenance; M03/M11 boundary fixtures required |
| FR-026 | TC21 disposition boundary | ordinary-role evidence deletion denied; legal-hold/export/disposition evidence belongs to approved privileged lifecycle, not invented as an M02 public method |
| FR-027/028 | TC22 consumer binding | tenant identity comes from trusted context; foreign target denied; crash/retry converges to one effective M05/M06 record; requires consumer-owner integration evidence |
| FR-033 | TC23 bulk read shape | bounded selector/page sizes, one engine version, per-item failure and no N+1 query behavior; no production performance claim |

TC01–23 plus earlier installation/privilege/gateway/key/parser cases form the proposed test leg, not a full passed acceptance suite. NFR coverage and exact reference anchors for earlier B/P/G/S/RC cases still need reconciliation. Unresolved audit, finalizer-wrapper and complete-evidence routes must each gain a named authorization-negative and authorized-positive test before architecture closure. They are not owner implementation requests.

## Reviewer questions and next dependency work

Please review F01–21 completeness for the frozen foundation family, null-operation correction, the proposed non-persisting read fallback, and the actual absent audit callable. Next drafting must resolve the internal execution routes and evidence/key oracles so the test leg becomes checkable. Then finish exact signatures/column grants/projection schema/fingerprint/seed serialization. No claims that a numbered list alone closes coverage.


## V3 — Claude221 conditions and CP0 owner crosswalk

Read Claude221 in full. TC07/08 now name their requirement and design basis, distinguishing required behavior from the proposed omission-testing technique. No new requirement or enforcement guarantee is invented.

CP0 closure is a finite document reconciliation, not a comparison to nonexistent code. EP01–04 execute ordinary mutation gateways owned by haloflow_m02_append_owner; EP05–06 use haloflow_m02_correction_owner; EP07–10 use haloflow_m02_read_owner; EP11 uses haloflow_m02_projection_owner; EP12–13 use haloflow_m02_outbox_owner. Internal lock/key helpers use haloflow_m02_lock_owner. These are execution owners, not service caller identities or permission to assume their roles.

FR-mandated write paths: intent/retry/resend FR-001–006/035 map EP01; submission/indeterminate/acceptance FR-007/016/029 map EP02; verified outcomes/conflicts FR-008/015/030 map EP03; accepted observation/state FR-009 maps EP04; correction FR-017 maps EP05; manual evidence FR-035 maps EP06; projection FR-014 maps operation-scoped EP01–06 completion plus EP11 rebuild; handoff FR-028 maps source-owning entry completion and EP12–13 dispatch. Signal FR-025/034 maps the same source completion and dispatcher boundary. Shared consumer mutation FR-027/028 belongs to M05/M06 and is represented by TC22's external contract test, not an invented M02 method. Reference publication FR-018/019/032 belongs to reviewed migrations/control-plane publication; privileged disposition FR-026 belongs to separately authorized lifecycle, both represented in the coverage ledger.

Completion and null-event handoff mutation ownership is projection owner through the proposed internal routes in execution-route reconciliation v1; audit insertion proposal is correction owner against the existing tenant outbox. These route proposals remain subject to review. Named owners alone do not establish that unresolved routes are approved or callable.

Claude221 endorses compute-only EP07/08 fallback; adopt it in the proposed package. Its elevated evidence authorization dependency remains explicit. Later implementation must prove equality among the reviewed design inventory, registered statements/capabilities and actual caller routes, with no unlisted write path. That future code check does not prevent closing CP0's document inventory once all proposed routes and cases are reviewed.
