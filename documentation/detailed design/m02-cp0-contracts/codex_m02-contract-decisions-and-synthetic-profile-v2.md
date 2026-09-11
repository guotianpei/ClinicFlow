# M02 CP0 — remaining contract decisions and synthetic profile v2

2026-09-10 · ChatGPT/Codex lead · Claude independent reviewer · Rachel owner.
Status: concrete proposals for one consolidated review; NOT accepted requirements, installed descriptors or a schema freeze.

## 1. Source reconciliation and scope

This annex closes the drafting gaps identified in working-230 with explicit candidates. It supplies a non-production synthetic contract profile and recommended v1 mechanics. Owner approval and independent review remain gates. It does not assign production clinical policies to M02.

Source checks: live design v0.3 XML; frozen OI-007; M01 001_m01_foundation.py, resolver.py, gateway.py, provisioning/units.py and verification.py. Design paragraph indices below include table paragraphs and are zero-based. M01 tenant IDs already match `^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$`; schema_key is unique. Resolver default purposes are treatment/payment/operations. Function verification renders {schema} in both expected body and config. These are source-derived observations, not live catalog checks.

Corrections to earlier working notes: H3/H4 quoted/non-ASCII tenant strings are serializer stress examples, NOT valid M01 tenant fixtures. Preserve them as such and require service/SQL rejection at the tenant boundary. The approved test baseline includes TC29, not only TC01–28. The signed rebuild-manifest obligation remains. Design P835 repeats the consistent-snapshot claim and must join P570/P1125 in source alignment. P584's M11 identity wording also needs alignment if kind is added.

## 2. Tenant binding: recommended fixed-schema registry lookup

Choose candidate A for review: derive tenant identity from shared.tenants using the function's provisioning-rendered schema literal, then require the registry tenant_id to equal app.tenant_id. Require active lifecycle and matching tenant/schema identity. M01 retains supported-schema-version validation in its gateway; do not hard-code supported versions in M02 SQL bodies. Missing, inactive or mismatched records fail closed before outbox/projection mutation. Use the registry value in the key/payload, never the setting as an unverified value. A unique schema_key already exists in M01.

Implement the binding check within the projection-owner finalization/enqueue bodies and outbox-owner claim/finish bodies; do not add a public generic tenant lookup. Exact additional grants: USAGE on shared and SELECT(tenant_id, schema_key, lifecycle_state, schema_version) on shared.tenants to haloflow_m02_projection_owner and haloflow_m02_outbox_owner. No display_reference, timestamps, mutation or grant option. Business callers do not inherit these roles. The same four-column grant already exists for runtime, but that does NOT automatically authorize the two additional owners.

Disclosure: these owner roles can read routing metadata across registry rows through their effective column privilege. Gateway bodies only use the fixed-schema row. This is an explicit additional control-plane read grant requiring owner/reviewer acceptance; no row-level secrecy claim. M01 authenticates principal/tenant entitlement. Equality is routing consistency, not principal authentication, and does not prevent a malicious privileged service from deliberately using another tenant route.

The fixed schema literal is a binding constant, not a caller argument or dynamic SQL identifier. Tenant table references remain unqualified inside the pinned search_path; shared.tenants is explicitly qualified. Do not alter the lock gateway's invariant body or narrow existing lock-owner grant. Existing units and FunctionVerification.render_body can substitute {schema}, so this candidate needs an M02 unit/manifest change and reviewed shared grants, not a new M01 interpolation feature or eighth tenant table. Source-invariant rules for these new bodies must explicitly allow the fixed binding constant; any wider invariant-body rule in the accepted design needs the same owner alignment.

Tests: valid tenant pair; setting mismatch; absent/suspended registry row; M01-rejected unsupported schema version (separate gateway control); attempted search_path/temp shadow; unauthorized direct calls; exact grant-column manifest; deployment to two schema keys produces correct distinct bindings and expected rendered-body verification. No grant is installed by this annex.

## 3. Canonical principal and purpose profile

Require canonical UUID principal text at the M02 v1 boundary. ACTOR maps solely to actor_id; WORKLOAD solely to workload_id. Audit principal_id is the same UUID's canonical text and principal_kind matches it; SQL rejects mismatched supplied forms and retry evidence. This does not authenticate service-supplied identity or remove existing audit DML risk.

Purpose proposal: accept only the intersection of the deployed M01 resolver allowlist and {treatment,payment,operations}; map each value identically to purpose_code. No automatic mapping of arbitrary custom purposes to operations. Synthetic tests use operations. Purpose alone never grants a correction capability. Proposed governance: M01 owns the canonical purpose domain; M02 owns action/resource/outcome mappings for its audit rows, reviewed with the audit consumer. Rachel must approve ownership rather than infer it from resolver defaults.

Action mappings: append_correction -> m02.correction.append; record_manual_resolution -> m02.resolution.append; resource_class patient_event; outcome_code recorded, only for a committed new/equivalent correction record. No success audit on a rolled-back act. These codes remain proposed local mappings. request_id <=128 characters, auth_method <=32, purpose <=64; reject before database conversion, never truncate. execution_id remains caller-labelled execution provenance.

## 4. Descriptor framework and storage

The following is the proposed non-production `m02_test_` profile version 1. It refines nested correction policies without changing OI-007's 13 type names, statuses, levels, classes or ranks. Before installing, apply OI-007 compatibility: if these type/version schemas have already been installed, create new versions; never rewrite historical descriptors. Production generations keep this namespace inactive.

All objects below are closed: unknown keys, duplicate raw-JSON keys, null required values and type coercion are rejected. UUID means canonical lowercase text. Integer excludes boolean. Rule IDs use the registered m02_test_ namespace; rule version is exactly 1. Every stored correction includes its rule identity and exact historical target contract identity inside allowlisted metadata, so replay does not use current policy implicitly.

The correction command's existing outer event_id, corrected_event_id, correction_scope, correction_reason and schema_version remain. Its `replacement` is one of the exact objects below; `evidence_refs` is a unique sorted array of 1–8 objects `{resolver_id, ref_id}`, where resolver_id is m02_test_evidence_v1 and ref_id is UUID. Evidence resolution is tenant-local and authorizes the requested act; arbitrary strings/URLs are not evidence. `approval_ref` is forbidden in this synthetic no-dual-approval profile, not silently ignored.

Stored correction metadata has exactly `policy` and `data`: policy is `{rule_id, rule_version, target_event_type, target_contract_version}`; data is the rule-specific replacement plus evidence_refs. Resolved evidence facts needed by the engine must be immutable and captured as the typed references/outcomes specified below, not fetched over the network by the pure fold. Original evidence and source identity remain immutable. No new standalone correction table is proposed.

### 4.1 Metadata-only rule

Rule `m02_test_metadata_label_v1`, version 1. Targets: any of the 13 synthetic types except correction/resolution events. Replacement exactly `{label_code}` with label_code in {reviewed,unreviewed}. The source synthetic event metadata schema explicitly permits optional `label_code` with that same enum. The correction's metadata.data is exactly `{label_code,evidence_refs}`. All six OI-007 correction reasons are permitted subject to authorized evidence; the reason itself does not determine effect.

Effect: display-only label overlay on the target; no change to status, level, terminality, acceptance identity, controlling business fact or original row. Chained corrections of correction events are not supported by this synthetic profile. Multiple independent label corrections to the same target select the greatest append_sequence (unique tenant sequence) as the display overlay; all remain in history. This ordering affects display only. Base public views do not return label_code; a separately authorized synthetic view can return it for TC26/metadata-only fixtures.

### 4.2 Asserted-fact rule

Rule `m02_test_exclude_assertion_v1`, version 1. Targets: synthetic submission/delivery/business-outcome/conflict facts, never requested/observation/correction/resolution. Replacement exactly `{effect:"exclude"}`; stored data exactly `{effect,evidence_refs}`. All six reasons remain valid controlled reasons; enhanced asserted-fact capability is required in addition to correction capability. The synthetic profile explicitly declares dual approval not required.

Effect: exclude the named original assertion from the active fold, retain it in immutable history and superseded_event_ids, then recompute from all remaining facts. It does not install a different status or modify the canonical acceptance binding. If removing acceptance evidence makes a retained binding unsupported, produce the existing integrity/incomplete outcome instead of silently replacing that binding or declaring success. Repeated exclusions of the same target are idempotent in the set fold; they remain distinct audit/evidence acts if their event identities differ. A future restoring/superseding rule requires a new descriptor; this profile does not infer one.

### 4.3 Manual-resolution envelope and five path rules

Outer shape: schema_version=1, event_id, operation_id, corrected_event_id, correction_reason, resolution_path_code, case_ref, evidence_refs, resolution_data. case_ref exactly `{resolver_id:"m02_test_case_v1",ref_id:UUID}`. Require ACTOR, authorized case/operation relationship, resolution capability, and evidence permission. correction_scope is derived asserted_fact, not a user switch. Targets are original operation evidence, not another correction/resolution. Every referenced fact is same-operation and cannot be excluded under the current correction graph.

Stored resolution metadata exactly `{policy,data}`. policy has rule_id/rule_version/target_event_type/target_contract_version. data has case_ref/evidence_refs/affected_event_ids plus the path-specific field below. affected_event_ids is a unique sorted nonempty UUID set of at most 16 original same-operation facts; includes corrected_event_id. An authorized case/evidence resolver validates this exact scope. Every required competing fact to be disposed must be included; unrelated or future facts are not implicitly covered.

| resolution_path_code | rule_id | Exact resolution_data beyond affected_event_ids | Required evidence and result |
|---|---|---|---|
| verified_via_provider_lookup | m02_test_resolve_provider_v1 | selected_event_id UUID | Select one retained delivered/delivery_failed fact, or the canonical accepted fact when affected scope contains an acceptance_conflict; evidence resolver attests the selected provider result for this case. For acceptance, selected fact identity must equal the complete canonical binding; noncanonical choice is unsupported with INTEGRITY_INCOMPLETE and cannot replace the binding. Exclude only authorized affected competing facts other than selected, then fold; remaining incompatible facts still conflict. |
| verified_via_domain_confirmation | m02_test_resolve_domain_v1 | selected_event_id UUID | Same, selecting business_succeeded/business_failed backed by domain confirmation. |
| confirmed_no_side_effect | m02_test_resolve_none_v1 | selected_event_id UUID | Select a retained rejected submission fact backed by explicit no-side-effect evidence. A verified terminal fact outside affected scope prevents a false no-effect conclusion. No fabricated rejection row. |
| accepted_unresolvable | m02_test_resolve_unknown_v1 | disposition_code exactly unresolved | Records case disposition but does not suppress facts or assert success/failure. Existing unknown/conflict projection persists; no verified result is manufactured. |
| authorised_resend_issued | m02_test_resolve_resend_v1 | new_operation_id UUID | Reference an already independently authorized/committed operation whose resend_of_operation_id is this operation. No provider call, new operation creation or suppression of original uncertainty. |

For the first three paths, selected_event_id must belong to affected_event_ids. The selected existing fact supplies outcome semantics; the resolution event is also recorded as controlling provenance under the versioned rule, with CORRECTION_APPLIED only if no higher-priority unresolved conflict/integrity condition remains. Unknown/resend dispositions do not become controlling business outcomes. Multiple resolutions with overlapping scopes and incompatible selected facts remain conflict; do not resolve by latest timestamp. Compatible duplicate choices converge. New later contradictory evidence is evaluated normally and can reopen conflict. These are proposed explicit synthetic semantics for review, not established production policy.

Historical authorization interpretation: authentication, entitlement and approval decisions are checked on append and recorded through immutable rule/evidence references. Replay validates stored graph and reference shape/scope under historical rules; it does not rerun current IAM or trust a newly added policy. A rule needing historical evidence absent from its manifest is unsupported. The application-trust/privileged-writer residual remains.

## 5. Typed link/evidence resolvers and seed alternatives

Resolver IDs below are synthetic instances, never inferred production module permissions. Inputs are canonical UUIDs or bounded closed provider descriptors as indicated; results are tenant-scoped structural validity/authorization, not metadata payloads. A non-production fixture registry supplies typed objects; this is not a proposal to create real M05/provider tables inside M02.

| ID | Resolves / allowed relationship | Semantic checks |
|---|---|---|
| m02_test_subject_v1 | UUID subject / subject | Same tenant, declared subject kind and authorized caller |
| m02_test_source_v1 | UUID source / source | Same tenant and approved source class |
| m02_test_provider_v1 | UUID provider-evidence object / provider | Same tenant; object's provider/account/source match fact |
| m02_test_business_v1 | UUID business-evidence object / business_resource | Same tenant and declared business-outcome source |
| m02_test_evidence_v1 | UUID evidence object / evidence | Immutable evidence kind, case/operation/target scope and authorized act; selected result where required |
| m02_test_case_v1 | UUID case / case | Same tenant/operation, actor case permission |

For links, link_type is the listed synthetic resolver ID; link_id is the UUID; relationship is the listed exact code. Any production link_type mapping needs its owner registration. Evidence/case refs in metadata are validated typed references, not automatically duplicated link rows.

Required-links encoding: array of nonempty disjunction groups, all groups required; sort IDs bytewise within groups and groups by compact canonical JSON bytes, reject duplicates. Requested and observation types: [[m02_test_source_v1,m02_test_subject_v1]]. Delivery types/conflicts requiring provider: [[m02_test_provider_v1]]. Business outcomes: [[m02_test_business_v1]]. Submission accepted/rejected/indeterminate: [] beyond the table-level patient-or-approved-link invariant. Acceptance conflict: [[m02_test_provider_v1]]. Correction/resolution: [] for the corrected-event requirement, which remains corrected_event_id FK; they still satisfy the table-level subject/link invariant by validated inherited subject linkage, not an invented corrected-event link type.

No optional links by default. Where a type needs an approved source link to satisfy the table invariant despite an empty required-links list, declare m02_test_subject_v1 and m02_test_source_v1 as optional explicitly. For correction/resolution, permitted inherited links are limited to those valid for the target and declared in the correction descriptor; no unrestricted copying. All 13 descriptors must enumerate these lists before seed generation. Null-operation observations/corrections do not gain a synthetic operation. Case/evidence references never establish tenant permission by their UUID shape alone.

## 6. Closed structural signal mapping

Proposed core M02-to-M11 codes, subject to consumer compatibility review: m02.event.appended -> event_signal; m02.integrity.incomplete -> integrity_alert. These are M02-owned emitted code proposals, not a grant over M11's entire vocabulary. Mapping is closed and generation-pinned; unknown combinations fail before enqueue. M05 kinds remain ordinary_failure/acceptance_conflict/delivery_conflict, M06 reconciliation, and M11 the two kinds above.

Synthetic rule: each newly committed synthetic event requires event_signal/m02.event.appended with that source event ID; equivalent retries converge on the same key. An incomplete projection may require integrity_alert/m02.integrity.incomplete anchored to the source event whose completion detects it. A detection without a source-event anchor belongs to a separate diagnostic path, not a fabricated outbox source FK. A rebuild never emits either as new downstream work. Required-list completeness still relies on service correctness under A.

M11 preimage includes kind as proposed in working-230. Align design P584 explicitly. Store schema_version=1, tenant_id, operation_id (nullable), source_event_id, handoff_kind and signal_type (M11 only). No arbitrary metadata. Logical-key collision comparison remains mandatory. Production-valid tenant vectors use M01 grammar; invalid quoted/non-ASCII strings test rejection, even though serializer escaping is independently specified.

## 7. Numeric profile: source defaults versus new proposals

All new numbers below are conservative review candidates, not benchmark-derived production targets. They are configuration profile `m02_limits_v1`, pinned per transaction and identified in run/diagnostic records. SQL-side hard maxima are constants in reviewed versioned gateway bodies; caller values can only narrow them. Service and SQL manifests must contain equal values. No mutable session setting can raise a limit. A deployment config can narrow service limits; raising hard maxima needs a new reviewed profile/unit and OI-009 reassessment.

| Item | Limit / behavior | Basis |
|---|---|---|
| Event metadata | <=8,192 compact UTF-8 bytes; per-contract lower cap allowed | Accepted design P371 |
| Operation-ID selector | <=200 IDs | Accepted P610 |
| Batch/run page | <=500 items | Accepted P613 |
| Queue/stale page | <=200 items | Accepted P616/P624 |
| Timeline page | 1–200 items | New proposed default |
| Raw public event command | <=65,536 UTF-8 bytes before parsing | New proposed hard cap, includes outer fields and links |
| Links per event | <=32, each closed and typed | New proposed hard cap |
| Complete operation fold | <=1,000 events and <=33,554,432 encoded input bytes; both enforced | New proposed safety envelope, unmeasured |
| Completion JSON | <=1,048,576 encoded bytes | New proposed cap; source/controlling/superseded arrays each <=1,000 UUIDs and contract pairs <=1,000 |
| Handoff list per completion | <=2,000 closed items and <=1,048,576 encoded bytes | New proposed cap, independent of total old outbox rows |
| Rebuild run membership | <=500 operation IDs, one active operation transaction per tenant worker | New proposed default |
| Dispatcher claim | p_batch_limit 1–100; p_lease_seconds 5–300, default 30 | New proposed hard bounds |
| Delivery attempts | <=12 claims; attempts increment on claim, capped rows terminalized without another delivery | New proposed ceiling, must surface operational handling |
| Retry delay | min(300,2^(attempts-1)) seconds after retry; DB clock | New proposed bounded deterministic schedule |
| Cursor lifetime | <=900 seconds, default 300; page version/tenant/selector/snapshot bound | New proposed default; signer deployment remains a gate |

Complete-history cap means detect N+1 or byte overrun and fail, not fold a first page. Existing deep histories over the cap become unavailable until a reviewed larger profile or equivalent optimization is validated. Never prune authoritative history to fit. A completion capacity error rolls back the event/domain/audit/outbox changes in that transaction. A callback retry may repeat the same failure; retain the upstream durable inbox/reconciliation record and escalate capacity remediation. This is an owner-visible availability tradeoff, not evidence that 1,000 events meet performance targets.

Dispatcher exhausted leased rows: once their final lease expires, the owner route marks dead_letter without incrementing beyond 12 or returning them for delivery. Explicit retry at attempt 12 also terminalizes. Current valid token may still finish delivered within its lease. Restart/repair of a dead letter is separately authorized; never reset attempts automatically. The owned operational alert path must surface the terminal state even if M11 delivery itself is impaired.

## 8. Signed run manifest contract

Closed content: schema_version=1, run_id UUID, tenant_id, mode apply/dry_run, membership (ordered UUID list <=500), selector_digest, membership_digest, engine_version, contract_snapshot_digest, limits_profile_id, started_at/finished_at UTC, per_operation array, aggregate counts, result complete/partial/failed, signer_key_id and signature envelope. Per-operation item: operation_id, disposition applied/compared/failed/unattempted, source_count and source_fingerprint when observed, projection_digest when computed, controlled error_code when failed. No event metadata or raw error text.

Snapshot semantics are explicit: pinned contract snapshot plus per-operation source observations under RC locks, not a range-wide database snapshot. Digests cover canonical document bytes under a separately versioned manifest serialization specification. Membership is retained, not only hashed. A verified signed membership/run-start artifact must exist before apply; final signed results must be written before reporting full success. If signing/persistence fails after any operation commit, report partial/manifest-incomplete and retain membership plus recovery evidence. A signature cannot make an already-committed partial run atomic.

Signer implementation/key custody, exact signature envelope/algorithm, storage ACL/retention and cursor signer integration are named implementation/operational dependencies. No existing M01 signer was found in this source check. Do not invent a provisioned production signer or use a business HMAC key. This package asks review of the manifest semantics and carries signer deployment as a pre-rebuild gate; whether CP0 may defer algorithm/interface closure is an explicit reviewer/owner gate, not presumed.

## 9. Review status and owner decisions

Request one consolidated review with main package and read-manifest annex. Conditions C1–C8 have concrete proposed resolutions; they are not marked closed by the author. Owner decision sheet must include: four-column registry grant; canonical UUID principal constraint and purpose ownership; RC per-operation rebuild amendments and signed-manifest treatment; new unmeasured hard-limit profile with rollback availability consequences; synthetic correction policy semantics and deferred production ownership; M02/M11 code mapping and kind-inclusive dedupe; direct lock grants and combined source issuance.

Known residual gaps for reviewer disposition: exact production support/clinical fields require owning-module descriptors; signer/cursor deployment and cryptographic interface are not installed; production policy/evidence resolvers are not invented; performance remains unmeasured. The synthetic profile is concrete but may require further semantic changes under independent review. No SQL/database, code/tests, accepted-source or Git changes were performed.

## Targeted delta 232 — conditions and source fold-ins

P1: descriptor JSON v2 carries every nonempty OI-007 other-constraints rule. Requested/rejected/indeterminate external_id is null; accepted non-null ID requires verified identity. Acceptance conflict candidate identity uses required event provider/provider_account/external_id plus closed metadata candidate_identity_namespace=m02_test_provider and candidate_uniqueness_scope=account_namespace. Canonical identity and its source fact remain in the immutable binding; both complete tuples are retained and must differ. These constants scope this synthetic profile only, not production providers. Delivery conflict metadata is exactly competing_event_ids, a unique ascending UUID-byte set of 2–16 same-operation retained verified terminal facts containing a genuinely incompatible pair. This identifies the concrete competing pair/cluster, not a prefix substitute for full-history folding. No self/correction/resolution reference, silently truncated set or missing IDs. Metadata cap remains 8KiB. The two conflict types no longer use label-only metadata schemas. Later label correction is a display overlay and cannot change their identity/evidence fields.

P2: provider-lookup resolution can choose a retained canonical accepted fact with an acceptance_conflict in affected_event_ids. Match provider/account/namespace/external_id/scope and the canonical binding's source-event identity; require case evidence approving that exact choice. Exclude only authorized affected competing facts, preserve original/binding and refold. Expected requested+accepted(A)+conflict(B) with canonical A -> submission accepted, nonterminal, CORRECTION_APPLIED, canonical acceptance plus resolution controlling provenance. Any unsuppressed later conflict remains a conflict. Noncanonical B -> unsupported/INTEGRITY_INCOMPLETE, no binding mutation and no ordinary successful resolution result; reject before committing the invalid resolution.

P3 OWNER LIMITATION: confirmed_no_side_effect only supports the existing-rejected-fact synthetic branch. It does not support requested+submission_indeterminate with staff confirmation of no effect. No new rejected fact/status may be fabricated; no-side-effect is not provider rejection. TC05/06 coverage excludes this FR-035 production branch. Rachel must choose whether a separately owned/versioned production rule or explicit scope deferral is acceptable; the synthetic suite cannot certify this requirement covered.

P4: SignerProvider contract is in codex_m02-targeted-delta-232.md and is a CP0 interface requirement for EP08–10 and rebuild. Provider algorithm, key custody/rotation and storage deployment remain pre-execution gates, not an excuse to defer the interface. No signer code has been implemented under this document-only scope.

F1: apply and finalizer themselves reject transaction_isolation other than read committed before source comparison/mutation. Service isolation query is an early check, not the only guard. F2: m02_load_operation_evidence is a single SQL statement building the entire closed envelope; no VOLATILE multi-query stitching. Verify PostgreSQL 17 snapshot behavior later. F3: M02 SQL binding checks fixed schema, active lifecycle and tenant equality; schema-version compatibility stays with M01. F4: issue shared.tenants grants from an owner-run Alembic revision, never a tenant migration unit. Add both M02 owner roles and explicit column-privilege checks to M02 shared-grant evidence; existing M01 TC-E22 table-level/manifest-role control alone misses them. F5: remove observation_received from every resolution target enum.

Decision-sheet additions: no-side-effect production gap; signer interface now required versus later deployment; M01 shared-grant coverage gap and correct owner-run issuance. All are pending owner alignment.
