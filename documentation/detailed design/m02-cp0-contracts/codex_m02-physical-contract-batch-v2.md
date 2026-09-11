# M02 CP0 — physical contract batch v2

2026-09-09 · ChatGPT/Codex lead · Codex drafting for batch review.
Claude229 read in full: test/route track approved with no open conditions. This batch proposes concrete physical contracts; approval of earlier routes does not freeze these types, fields or serializers. No SQL, test code, migration or dependency changes. A retained, B not pursued, C deferred. Read with approved test-leg v3, route v2 and coverage ledger v1.

## 1. Function identities and execution ACL

All names tenant-local. Listed signatures identify input types in order; parameter names for existing declarations are retained. JSONB means a closed versioned envelope, never arbitrary query or unvalidated public input. All mutation/lock functions are VOLATILE/PARALLEL UNSAFE/CALLED ON NULL INPUT, validating nulls explicitly. All are SECURITY DEFINER with pg_catalog, validated tenant schema, pg_temp; owner roles NOLOGIN. PUBLIC EXECUTE absent; no grant option. No caller gains owner membership. Read functions perform no mutation. Proposed names/types below require comparison with final wire schemas before freeze.

| Function(input types) | Result | Owner suffix | Explicit EXECUTE caller suffixes |
|---|---|---|---|
| m02_lock_operation(uuid) | TABLE(operation_id uuid) | m02_lock_owner | runtime, m02_append_owner, m02_correction_owner, m02_projection_owner |
| m02_begin_key_scope(text,text) | TABLE(active_write_version smallint, retained_lookup_versions smallint[], generation bigint) | m02_lock_owner | runtime, m02_append_owner |
| m02_begin_operation(jsonb) | jsonb | m02_append_owner | runtime |
| m02_append_submission(jsonb) | jsonb | m02_append_owner | runtime |
| m02_append_outcome(jsonb) | jsonb | m02_append_owner | runtime |
| m02_append_observation(jsonb) | jsonb | m02_append_owner | runtime |
| m02_append_correction(jsonb) | jsonb | m02_correction_owner | correction |
| m02_resolve_operation(jsonb) | jsonb | m02_correction_owner | correction |
| m02_complete_append(uuid,jsonb,jsonb) | jsonb | m02_append_owner | runtime |
| m02_complete_correction(uuid,jsonb,jsonb) | jsonb | m02_correction_owner | correction |
| m02_finalize_operation(uuid,jsonb,jsonb) | jsonb | m02_projection_owner | m02_append_owner, m02_correction_owner |
| m02_enqueue_event_handoffs(uuid,jsonb) | jsonb | m02_projection_owner | m02_append_owner, m02_correction_owner |
| m02_load_operation_evidence(uuid) | jsonb | m02_read_owner | runtime, correction, projection_worker |
| m02_get_projection(uuid,text) | jsonb | m02_read_owner | runtime |
| m02_list_projections(jsonb,jsonb,integer) | jsonb | m02_read_owner | runtime |
| m02_list_stale_intents(timestamptz,jsonb,jsonb,integer) | jsonb | m02_read_owner | runtime |
| m02_list_timeline(text,uuid,jsonb,integer,text) | jsonb | m02_read_owner | runtime |
| m02_apply_rebuild_projection(uuid,jsonb,jsonb) | jsonb | m02_projection_owner | projection_worker |
| m02_claim_handoffs(integer,integer) | jsonb | m02_outbox_owner | handoff_dispatcher |
| m02_finish_handoff(uuid,uuid,text,text) | boolean | m02_outbox_owner | handoff_dispatcher |

Every suffix above receives haloflow_ prefix. The finalizer's third jsonb argument explicitly carries the reviewed service-derived handoff list, reconciling wrappers' three inputs with the older two-input draft; it is a proposed signature change requiring review, not an unnoticed overload. Install only one reviewed identity per name/signature. Absence of rotation remains mandatory. Claim worker attribution cannot use current_user inside a definer; trusted dispatcher context mapping remains a wire-contract dependency.

## 2. operation_projections physical proposal

| Column | Type / constraint | Writer |
|---|---|---|
| operation_id | uuid PRIMARY KEY; tenant registry FK | projection owner INSERT only |
| status | varchar(64) NOT NULL | derived |
| terminal | boolean NOT NULL | derived |
| conflict | boolean NOT NULL | derived |
| indeterminate | boolean NOT NULL | derived |
| controlling_event_ids | uuid[] NOT NULL | derived; distinct, ordered canonical set |
| explanation_code | varchar(64) NOT NULL | derived, approved explanation vocabulary |
| projection_engine_version | integer NOT NULL, positive | derived |
| source_event_count | bigint NOT NULL, nonnegative | database source summary |
| source_max_append_sequence | bigint nullable | NULL exactly when source count is zero |
| source_set_fingerprint | bytea NOT NULL, 32 bytes | internal database-evidence digest |
| refreshed_at | timestamptz NOT NULL, database assigned | projection owner |

No persisted source_event_ids column is added for abandoned B. Finalizer receives IDs transiently to compare against database evidence. Empty controlling set is permitted only when the chosen engine result permits it; nonempty arrays are one-dimensional/one-based, no NULLs/duplicates and ascending UUID bytes. Empty array is the canonical empty PostgreSQL array, not an impossible demand for one nonempty dimension. Controlling IDs must belong to the exact operation evidence set. Booleans/status/explanation combinations are validated against the engine contract, not arbitrary independent flags. No raw metadata or source digest in public projection tiers.

## 3. Compiled engine-input manifest proposal

Manifest version 1 declares column/path, type, purpose, contract applicability and internal consumer. Inputs are assembled from explicit expressions; no wildcard serialization. Base event inputs: event_id uuid (identity), append_sequence bigint (tie ordering), operation_id uuid (scope), event_type varchar(128) and contract_version integer (historical rule lookup), event_level varchar(32) and status varchar(64) (fold), occurred_at timestamptz nullable and source_time_quality varchar(24) (trusted ordering), corrected_event_id uuid nullable, correction_scope varchar(32) nullable, correction_reason varchar(64) nullable, resolution_path_code varchar(64) nullable (correction graph), content_fingerprint bytea (immutable source digest).

Conditional evidence inputs: provider/provider_account varchar(64), external_id varchar(256) and canonical binding identity_namespace/uniqueness_scope/source_event_id are exposed only to acceptance comparison; failure_reason_code varchar(64) only to applicable structural handoff rules. Links expose event_id/link_type/link_id/relationship only where the historical contract's projection/correction rule requires them. Patient, actor, workload, source-provider event IDs, producer dedupe, correlation and received_at are excluded from the engine envelope unless a separately reviewed rule establishes a need; audit provenance uses its own trusted context route. Database scope/authorization checks may use columns without returning them.

Metadata is NOT returned wholesale. For each historical contract, compile exact metadata JSON paths required by its projection/correction rule. An empty declaration means no metadata is returned. A rule that requires metadata but has no reviewed path/type declaration fails unsupported; it must not receive {} and silently fold as success. Frozen seed catalogue names correction semantics but does not supply all correction evidence payload paths: that is an explicit unresolved input-schema dependency. This batch does not invent clinical metadata fields. Engine version and immutable contract snapshot identity accompany the envelope; historical rules must be available even when retired for new appends.

TC26 checks the manifest/expression/envelope equality and field-purpose entitlement, not just response filtering. Engine inputs remain inaccessible via public result serialization, logs and exceptions. Exact correction/resolution evidence schema remains necessary before this manifest can be frozen.

## 4. Exact projection/outbox/audit column grants

Projection owner INSERT: all twelve projection columns above; UPDATE: every column except operation_id. refreshed_at and source summaries are computed inside the gateway, not accepted as caller-supplied insert values. Direct business callers receive no table INSERT/UPDATE; append/correction owners invoke the finalizer instead.

Outbox proposed INSERT set for projection owner: handoff_id, tenant_id, operation_id, source_event_id, handoff_kind, dedupe_key, payload, state, attempts, available_at. DB assigns created_at; initial state pending, attempts zero, lease/delivery fields NULL. Dispatcher owner UPDATE only state, attempts, available_at, lease_owner, lease_expires_at, lease_token, delivered_at. No key/payload/source UPDATE. SELECT only those INSERT fields plus created_at and lease/delivery fields. Proposed added lease_token uuid makes fencing explicit; no unfenced finish.

Correction-owner audit INSERT: source_event_id, action_code, resource_class, purpose_code, outcome_code, principal_kind, principal_id, execution_id, request_id. No occurred_at override or projected_at mutation from this grant. Existing runtime audit-table SELECT/INSERT/UPDATE/DELETE remains the separately disclosed M01 limitation; do not describe this narrow added grant as removing it.

Lock-owner exact column grants remain registry SELECT(operation_id), UPDATE(correlation_id) with approved registry rejector, and key-control SELECT(owner_service,action_code,active_write_version,retained_lookup_versions,generation), UPDATE(updated_at) with accepted timestamp-write residual. No broad key-control read. Append/correction evidence INSERT and read-owner SELECT must be compiled from exact query dependencies; final complete manifest is not claimed by this partial column section. No DELETE/TRUNCATE for M02 business owners.

## 5. Source-set fingerprint serialization proposal

Separate this source-set digest from business-key HMAC and per-event content_fingerprint algorithms. Proposed domain tag m02.operation-source-set/v1. SHA-256 input is UTF-8 canonical JSON array [domain_tag, canonical_operation_uuid, events], with no BOM/newline/optional whitespace. Each event element is [canonical_event_uuid, append_sequence_decimal_string, lowercase_content_fingerprint_hex]. Sort events by numeric append_sequence, then UUID bytes. UUIDs are lowercase standard hyphenated form; fingerprints exactly 64 hex characters; decimal strings have no leading zeros, sign or exponent. Empty evidence uses an empty events array. No floats, locale formatting or Unicode free text enter this format.

This detects source membership and stored content-digest drift. It does not independently prove the event digest was correctly computed or that privileged actors have not consistently rewritten both evidence and fingerprints. Links and metadata are covered only insofar as the approved immutable event-content digest includes them; independent integrity verification must recompute that digest from retained evidence. Do not advertise this as a tamper-evident ledger. Engine version is stored separately so source identity is stable across engine versions; output equality additionally compares engine version/results.

Finalizer recomputes from database evidence under operation lock and compares transient source IDs; no trusting submitted source count/max/digest. Rebuild uses the identical serializer and whole declared range. Golden vectors required before freeze: empty, one event, reversed input order, two equal sequence values rejected by schema, changed event ID/sequence/content digest, max bigint decimal, malformed hash/UUID. Computed document vectors are supplied in §10 below. No M02 implementation or implementation tests were executed.

## 6. Seed/canonical-array compatibility

Keep OI-007's exact 13 m02_test_ event types, 15 statuses, six levels and ten explanation codes. requested replaces conceptual requested/pending in concrete fixtures. Terminality, class and precedence remain per contract; no additions to status table to simplify the engine. Foundation observation lacks operation_id; correction follows target scope; resolution requires it. Required fields are not converted into link rows. Test family cannot be active in a production generation.

Set-valued contract arrays (allowed statuses/producers/required fields/required and optional links) must reject duplicates/nulls, with deterministic sorting only after preserving the declared element schema. Do not silently turn the phrase subject-or-source into two conjunctive required links. Its exact machine-readable alternative-group representation remains to be specified from the frozen semantics. Ordered arrays, such as active-first retained key versions, must never use generic alphabetical sorting. Key retained versions keep the approved 1–8 positive distinct smallints, active first and remaining descending; invalid historical/imported state fails closed rather than repaired silently.

## Batch review and honest completeness

This is the first physical batch, not the final schema freeze. Concrete review choices: finalizer signature adds handoff argument; projection types and empty-array representation; engine-input minimum set; source-set digest format/domain. Remaining work: closed correction/hand-off wire schemas, complete table/query/column grant manifest, exact capability mappings, seed alternative-group encoding and golden vectors, then clean test-contract consolidation. No request for owner blanket approval of unresolved items. NF10 carries the audit-outbox limitation from note228. Review as a batch; improvements alone need not trigger piecemeal revisions.


## 7. V2 reconciliation: application engine, context and completion envelope

V2 supersedes v1 as the review target. The proposed SQL rebuild function taking only a range/version/flag could not invoke the shared Python engine. Replace it with m02_apply_rebuild_projection(uuid,jsonb,jsonb), a worker-only application-of-result gateway owned by projection owner. The Python worker selects its declared bounded range in the approved consistent snapshot, loads full evidence, runs the same engine and supplies one result per operation. It never calls an adapter. The SQL gateway validates current source correspondence and writes through the same private finalizer. Dry-run performs comparison only and never invokes this write gateway. A concurrent-source mismatch aborts the run; restart the entire declared range with a new snapshot. Range-level publication/transaction boundaries must be explicitly reviewed before implementation; this proposal does not claim a collection of individually committed rows is an atomic range swap.

Completion p_result is proposed as a closed object with exactly: schema_version (integer 1), projection_engine_version (positive integer), contract_snapshot_digest (64 lowercase hex), source_event_ids (canonical UUID array, unique, sorted UUID bytes), status (reference code), terminal/conflict/indeterminate (booleans), controlling_event_ids and superseded_event_ids (canonical UUID sets), explanation_code (reference code), contract_versions (unique pairs of event_type string and positive version, ordered by type then numeric version). No caller source count/max/digest or refreshed_at fields. SQL compares source IDs with the full database set and validates both result ID sets belong to it; selected and superseded sets must be disjoint. Contract version pairs must equal the distinct versions actually referenced. Engine/snapshot recognition must use the reviewed service snapshot protocol; byte shape alone does not prove the snapshot authentic. Full engine recomputation is service-side under A, so valid-looking false semantics from a malicious trusted service are not excluded by these shape checks. This is a proposed envelope, not an implementation claim.

All completion input JSON must be validated before conversion to JSONB where duplicate JSON keys would otherwise be lost. Reject extra keys, invalid/null required values, noncanonical UUIDs, noninteger versions, unrecognized versions and oversized command envelopes. Numeric command-size limits must be derived from approved complete-history bounds; never truncate source IDs/evidence to satisfy a limit. Until bound approval, oversize is explicit unavailable/failure, not partial success.

M01 context source check: context.py defines principal.kind/id/auth_method, tenant_id, purpose, execution_id, correlation_id/correlation_source and request_id. TenantContext documents execution_id as caller-labelled execution scope. Principal is identity-policy output; its external identity adapter remains an integration item. Thus copying execution_id or request_id from a resolver-issued context preserves provenance but is not independent authentication of those labels. No claim that M01 minted or independently verified them. The public M02 command cannot override context fields. Map principal.kind.value/id directly to audit kind/id; preserve execution_id and bounded request_id; map purpose only through a reviewed controlled-purpose allowlist, never arbitrary text. M01 principal.id is a string whereas design actor_id/workload_id are UUID: a canonical UUID identity mapping is a real integration dependency, not a safe cast for all principals. Reject unsupported identities until the adapter mapping is approved. No truncation of IDs or silent replacement with a generated UUID.

Proposed local audit codes for review: action_code m02.correction.append or m02.resolution.append; resource_class patient_event; outcome_code recorded. Purpose codes and policy-specific enhanced/dual approval are intentionally not invented. These proposed codes need vocabulary ownership confirmation. Event and audit writes remain atomic and retry-equivalent; runtime's existing audit DML limitation remains in force.

## 8. Closed handoff wire proposal and deterministic identity

p_handoffs is an array of closed items: schema_version=1, source_event_id (UUID), handoff_kind (one of six design values), signal_type (controlled string only for event_signal/integrity_alert). Other fields are prohibited. The gateway derives trusted tenant_id, source operation_id, deterministic key, storage UUID and payload; callers cannot supply tenant, dedupe key, state, attempts, leases or metadata. Reject repeated logical keys within the list rather than accept conflicting commands. Structural signal codes require their own reviewed finite rule mapping; arbitrary strings are invalid.

Stored payload v1 contains exactly schema_version, tenant_id, operation_id (UUID or null), source_event_id, handoff_kind, and signal_type when applicable. No copied event metadata, provider external identity, free-text reason, auth token or request body. These internal identifiers require classification and downstream authorization; calling them structural does not make them anonymous.

Versioned dedupe preimage is compact UTF-8 JSON (no whitespace/BOM/newline):
- M05 ordinary_failure/acceptance_conflict/delivery_conflict: ["m02.handoff/v1","M05",tenant_id,operation_uuid,kind].
- M06 reconciliation: ["m02.handoff/v1","M06",tenant_id,operation_uuid].
- M11 event_signal/integrity_alert: ["m02.handoff/v1","M11",tenant_id,source_event_uuid,signal_type].
Stored key proposal: m02h1: followed by lowercase SHA-256 hex of the preimage. Tenant ID is the canonical registry string, preserved exactly with standard JSON escaping; no case folding or normalization. Kind is bound to a finite destination mapping. M11 signal mapping must distinguish integrity alerts from other signals that share a source. On unique-key collision, compare logical key fields, not the hash alone. Payload changes for the same effective M05/M06 key do not create a second work identity. Preserve first durable source anchor; consumer loads current authorized evidence by operation as needed. Same-key incompatible logical meaning is an integrity error, not overwrite. handoff_id is generated once and reused through stored row retrieval on retries.

Null-operation event_signal/integrity_alert may use source-event identity. M05/M06 null-operation commands fail unsupported absent a separately approved alternative. This preserves the approved TC28 synthetic event_signal route. No assertion of required-list completeness under A.

Outbox physical details proposed in addition to §4: handoff_id uuid PK; tenant_id text NOT NULL matching trusted context; operation_id uuid nullable local FK; source_event_id uuid NOT NULL local event FK; handoff_kind varchar(32) NOT NULL; dedupe_key varchar(70) UNIQUE NOT NULL; payload jsonb NOT NULL; state varchar(16) NOT NULL; attempts integer NOT NULL >=0; available_at/created_at timestamptz NOT NULL; lease_owner varchar(128), lease_token uuid, lease_expires_at/delivered_at timestamptz nullable. Leased state requires all three lease fields; other states clear them. Delivered state requires delivered_at; other states omit it. Claim increments attempts, assigns a fresh unguessable token and DB-clock expiry. Finish requires matching handoff_id/token, leased state and unexpired lease; stale token returns false with no mutation. Retry/dead-letter clears lease fields; immutable key/payload/anchors never updated. Allowed terminal outcome inputs are delivered, retry, dead_letter. The fourth finish text argument is a controlled reason code, never raw failure text; reason persistence is not granted by this schema and must not be implied. Dispatcher workload attribution uses trusted principal context, not SECURITY DEFINER current_user. Lease duration, retry ceilings and fairness bounds remain OI-009 configuration objectives; enforce configured positive bounds without inventing production targets.

## 9. Capability catalogue proposal and grant accounting

Each row specifies statement key = required capability, except no public statement is registered for the private finalizer/enqueue functions. This is a proposed catalogue, not installed M01 entries. READ/WRITE are semantic modes matching M01's lowercase enum values. Capability names fit the existing grammar; naming does not issue policy entitlements.

| Statement key / capability | Mode | Function suffix after m02_ | Caller |
|---|---|---|---|
| m02.operation.lock | WRITE | lock_operation | runtime/correction/projection_worker through approved route only; see ACL reconciliation below |
| m02.key_scope.begin | WRITE | begin_key_scope | runtime |
| m02.operation.begin | WRITE | begin_operation | runtime |
| m02.submission.append | WRITE | append_submission | runtime |
| m02.outcome.append | WRITE | append_outcome | runtime |
| m02.observation.append | WRITE | append_observation | runtime |
| m02.correction.append | WRITE | append_correction | correction |
| m02.resolution.append | WRITE | resolve_operation | correction |
| m02.append.complete | WRITE | complete_append | runtime |
| m02.correction.complete | WRITE | complete_correction | correction |
| m02.operation.evidence.read | READ | load_operation_evidence | runtime/correction/projection_worker |
| m02.projection.read | READ | get_projection | runtime |
| m02.projection.list | READ | list_projections | runtime |
| m02.stale_intent.list | READ | list_stale_intents | runtime |
| m02.timeline.read | READ | list_timeline | runtime |
| m02.projection.rebuild.apply | WRITE | apply_rebuild_projection | projection_worker |
| m02.handoff.claim | WRITE | claim_handoffs | handoff_dispatcher |
| m02.handoff.finish | WRITE | finish_handoff | handoff_dispatcher |

ACL reconciliation requiring review: §1 currently grants direct lock EXECUTE only runtime plus internal owner roles. Correction/projection worker cannot simply use the public m02.operation.lock statement with those SQL ACLs. Preferred proposal is distinct correction/worker lock wrappers owned by their respective owners, calling the existing lock gateway, with keys m02.correction.lock and m02.projection.rebuild.lock and no new direct lock grants. This adds two explicit identities m02_lock_correction_operation(uuid) and m02_lock_rebuild_operation(uuid), returns TABLE(operation_id uuid), WRITE; restrict respective EXECUTE to correction and projection_worker. The first catalogue row then applies only to runtime. This keeps locks before evidence loading and retains no owner membership. A SQL mutation must not silently acquire its first lock after the Python fold. Treat this as an actual route gap for batch review, not an already-approved additional gateway.

Per-statement M01 checks cover one required capability; orchestration checks the complete required set before starting. Typed correction scope additionally requires enhanced asserted-fact permission where policy says so; resolution requires actor/case/evidence authorization. A READ evidence capability does not confer public clinical/support field access. Role entitlement, operation/subject entitlement, context expiry and tenant validation remain separate checks.

Exact draft evidence INSERT column set E: event_id, operation_id, patient_id, event_type, contract_version, event_level, status, channel, direction, correlation_id, causation_event_id, provider, provider_account, external_id, provider_event_id, source_namespace, source_event_id, producer_dedupe_key, content_fingerprint, producer_service, producer_version, actor_id, workload_id, auth_method, occurred_at, source_time_quality, received_at, corrected_event_id, correction_reason, correction_scope, failure_reason_code, resolution_path_code, metadata. Append/correction owner INSERT E; append_sequence identity and created_at default omitted. Column grants do not constrain event kinds; gateway bodies enforce ordinary versus privileged kinds and immutable-policy rules. No UPDATE/DELETE grants on events/links/binding. Link INSERT(event_id,link_type,link_id,relationship). Binding append-owner INSERT(operation_id,provider,provider_account,identity_namespace,external_id,uniqueness_scope,source_event_id); correction cannot replace a canonical binding.

Registry append-owner INSERT(operation_id,owner_service,action_code,business_key_fingerprint,business_key_version,subject_type,subject_id,resend_of_operation_id,correlation_id,producer_version); created_at default omitted. Key-control INSERT/UPDATE remains migration-owned except the accepted lock-owner timestamp privilege. Registry correlation UPDATE privilege remains the lock-owner rejector exception, not an append service mutation route.

SELECT accounting is intentionally a query dependency matrix rather than a claim that all stored event columns may be exposed to the engine: append needs duplicate identities/content fingerprint, operation validation and existing binding fields; correction additionally needs target contract/scope/correction chain and audit retry fields; read owner needs §3 engine fields plus distinct authorized public-tier dependencies; projection owner needs source IDs/sequences/fingerprints, applicable contracts, projections and outbox dedupe fields; outbox owner only §4 lease/delivery set. PostgreSQL column SELECT on metadata cannot restrict JSON paths: body expressions and reviewed views must enforce paths, while the definer's effective column privilege still includes the whole metadata value. That broader owner-level reach must be disclosed. Exact SELECT union cannot be certified until every public-tier field and contract metadata path is frozen. SQL/query checksums and actual catalog grants are later implementation evidence, not available today.

## 10. Source digest document vectors

These synthetic vectors were computed while drafting this document with the Python standard library; they validate the proposed serialization examples, not an M02 runtime. The operation UUID is 00000000-0000-0000-0000-000000000001. Positive bigint sequences are decimal strings; malformed inputs have no digest. Reversed input order is canonicalized before hashing. Equal sequences across two events are rejected because patient_events append_sequence is UNIQUE, even though UUID sorting defines a stable serializer ordering.

### empty

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[]]`

SHA-256: `35a079dc31c35d61dab055ea3a3293c889be853714fc7d0e62f3d4b3bc4af38a`

### one

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[["00000000-0000-0000-0000-000000000010","1","0000000000000000000000000000000000000000000000000000000000000000"]]]`

SHA-256: `6f0d0d7253c5e27277f63ef437bcf3ea9b64b87e6fae29f4784a6fe74807e3ae`

### two

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[["00000000-0000-0000-0000-000000000010","1","0000000000000000000000000000000000000000000000000000000000000000"],["00000000-0000-0000-0000-000000000020","2","1111111111111111111111111111111111111111111111111111111111111111"]]]`

SHA-256: `ae53797214201be13e4cb3f4cb8425d23ec3e84cd91272d6ecd9e5d731d4643f`

### reversed

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[["00000000-0000-0000-0000-000000000010","1","0000000000000000000000000000000000000000000000000000000000000000"],["00000000-0000-0000-0000-000000000020","2","1111111111111111111111111111111111111111111111111111111111111111"]]]`

SHA-256: `ae53797214201be13e4cb3f4cb8425d23ec3e84cd91272d6ecd9e5d731d4643f`

### changed_id

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[["00000000-0000-0000-0000-000000000020","1","0000000000000000000000000000000000000000000000000000000000000000"]]]`

SHA-256: `2332be9c29c42ccbfca04cee21a5e99276c106f220815dda9c390fb6757d4a01`

### changed_sequence

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[["00000000-0000-0000-0000-000000000010","2","0000000000000000000000000000000000000000000000000000000000000000"]]]`

SHA-256: `a7e1dc7beb671985f087485c25c40dcf226ebad54891ad4df7a260338f016a36`

### changed_content

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[["00000000-0000-0000-0000-000000000010","1","1111111111111111111111111111111111111111111111111111111111111111"]]]`

SHA-256: `38327531645f0ab3984298b9699f2133b12859d5f0a953261b364be11850c1cd`

### max_bigint

Preimage: `["m02.operation-source-set/v1","00000000-0000-0000-0000-000000000001",[["00000000-0000-0000-0000-000000000010","9223372036854775807","0000000000000000000000000000000000000000000000000000000000000000"]]]`

SHA-256: `c787b770daf5ef86eb6bb6d033d303d12e1e594b6ff3dd3230a1b4afc6cbfb02`


Reject vectors: uppercase/noncanonical UUID; non-64-character or nonhex hash; sequence 0, negative, leading-zero, exponent or greater than 9223372036854775807; duplicate event ID; duplicate sequence. Empty evidence does not by itself authorize a successful projection for a registered operation with missing intent; integrity outcome remains engine-defined. No binary digest is accepted from a public caller as proof of correctness.

## 11. Seed alternatives and correction schema boundary

Proposed canonical required-links representation is an array of disjunction groups: every group must be satisfied, and each group contains a nonempty unique list of approved link-rule identifiers, at least one of which must match. Sort rule identifiers lexically within each group; sort groups by their compact serialized form; reject duplicate groups. Thus subject-or-source is ONE group with two alternatives, not two required groups. Exact link-rule identifiers resolve through a reviewed typed resolver registry; the literal prose phrases are not production IDs. Fields operation_id/correlation_id/corrected_event_id remain direct fields/conditional requirements, never invented link rows. OI-007's correction row says corrected event under links while §5 locates the FK on patient_events: mapping must explicitly preserve that FK requirement and must not create a redundant unapproved relationship. This representation is a proposal for compatibility review, not a silent change to the frozen catalogue.

Correction command outer shape proposal: schema_version=1, event_id, corrected_event_id, correction_scope (metadata_only/asserted_fact), correction_reason (six OI-007 codes), replacement (contract-specific closed object), evidence_refs (approved typed references), and approval_ref only where policy requires. No tenant/principal/execution/audit fields; operation scope is derived from the target. The exact historical correction_policy supplies the allowable replacement paths and types. Arbitrary JSON Patch, unrestricted metadata, free-text evidence and replacement of immutable source identity are prohibited. Metadata-only correction must not change the asserted business outcome. Asserted-fact replacement and graph semantics require named versioned rule definitions; they cannot be inferred from correction_scope alone.

Resolution outer shape proposal: schema_version=1, event_id, operation_id, corrected_event_id, resolution_path_code (five OI-007 codes), correction_reason, case_ref, evidence_refs, and rule-specific closed resolution data. An authorized actor and case ownership are required. authorised_resend_issued documents authorized resolution evidence but never invokes a provider or creates an implicit resend; the separately authorized new operation still follows intent-before-action. Accepted-unresolvable must not be fabricated into verified success. Exact rule outcomes/affected competing facts/dual approval remain contract and policy definitions requiring review.

These are closed OUTER envelopes only. The replacement/evidence/case identifiers, nested path schemas, rule IDs and per-path outcomes are not fully specified in the accepted seed document. They are explicit CP0 freeze dependencies. It would be inaccurate to label an unconstrained nested object a completed closed wire schema. A seed-generation fixture may use a separately reviewed synthetic schema; no clinical schema or owning-module capability is inferred.

## 12. Review disposition and alignment gate

Test track: Claude229 approved; no new test-track approval loop requested. Physical v2 is the batch review target and includes actual route corrections, not just editorial changes. Review first: rebuild application wrapper, correction/worker lock wrappers, completion semantics and trust limits, physical projection/outbox fields, source/key serializers, capability/grant map, and compatibility of seed group representation. Refine remaining policy/contract dependencies in this same batch; do not claim physical freeze or CP0 complete.

Owner alignment remains one complete requirements + architecture + tests package. Pending freeze items are precise: nested correction/resolution and evidence schemas/rules; UUID principal mapping and audit purpose vocabulary; public tier/path SELECT union; rebuild range transaction publication; numeric envelope bounds under complete-history behavior; closed signal types and link resolver IDs. No SQL, migrations, tests, DB, dependencies, accepted source documents or Git were changed. Four earlier role Allowed-cell amendments remain approved for issuance but issuance is unconfirmed. A's omission residual and runtime audit DML limitation remain visible.
