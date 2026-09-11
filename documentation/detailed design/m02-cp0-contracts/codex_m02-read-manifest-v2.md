# M02 CP0 — proposed read view and column manifest v2

2026-09-10 · ChatGPT/Codex lead · Independent review requested with consolidated package.
Status: document-level manifest, not executed SQL, a pg_depend dump, or certified live grants.

The accompanying JSON is the canonical proposed field inventory for the default/synthetic profile. No wildcard serialization is permitted. Each view below denotes explicit expressions and dependencies; implementation must prove catalog parity, permissions and body checksums before replacing inherited runtime SELECT. View access is only through purpose-specific registered functions (except explicitly reviewed direct tier views); no generic arbitrary-view selector.

## Output construction and semantics

Engine projection metadata is dispatched by exact historical event_type/version and rule_id/version. Construct only the keys required for that rule from the paths listed below: metadata label corrections contribute no label data to the business fold; exclusion contributes effect/evidence_refs; each resolution contributes its own closed fields. Unknown fields are rejected on append, never forwarded on read. For a contract without declared paths the metadata projection is empty; a rule requiring an undeclared/missing path is unsupported. The array listing possible paths is a union, not permission to return every path on every event.

Base envelope types/nullability are inherited from physical v2 and the exact descriptor shapes in the synthetic annex. Link and evidence arrays are canonical unique typed objects. Bytes such as content_fingerprint use 64 lowercase hex in the service envelope; never binary-to-public serialization. Full history is bounded by explicit failure, not pagination truncation.

ProjectionResult adds service-derived freshness: generated_at, policy identifier, cache/compute origin, and explicit available/unavailable disposition; source digests/counts remain internal. One page version is selected and bound into its cursor. Stale/missing/wrong-version cache recomputes only with already authorized internal-evidence capability and budget, otherwise returns per-item unavailable. Do not persist on fallback. An evidence load for compute-only fallback must produce a coherent operation envelope in one statement/snapshot or validate matching source membership across loads; independent RC queries may not silently splice different event/link sets.

Timeline principal_kind is derived actor/workload, not an actor UUID, workload UUID, auth_method or free-text identity. Display chronology uses occurred_at only for trusted quality, otherwise created_at; append_sequence is the final internal tie key. Cursor is opaque/signed; no raw sequence used as rebuild resume watermark. Unavailable/denied resources use M01 non-enumerable behavior.

Stale candidates use authoritative events, not projections. The query must exclude qualifying submissions and apply the same historical correction/resolution validity rules to resolved/superseded candidates. Metadata dependencies below reflect that obligation; a simpler anti-join that ignores corrections is not equivalent. Provider hints can be null and never license automatic resend; M06 owns lookup decisions. This elevated service view is not the default clinic timeline.

Synthetic label view is non-production only and requires an explicit view capability. Production support/clinical field sets are intentionally not invented: an unregistered profile fails unavailable. This is a proposed extension boundary for owner review, not proof that every future tier has been implemented.

## Per-view inventory

### engine_events

Consumer: authorized internal fold only.

Exact output: event_id, append_sequence, operation_id, event_type, contract_version, event_level, status, occurred_at, source_time_quality, corrected_event_id, correction_scope, correction_reason, resolution_path_code, content_fingerprint, provider, provider_account, external_id, failure_reason_code, metadata_projection.

patient_events dependencies: append_sequence, content_fingerprint, contract_version, corrected_event_id, correction_reason, correction_scope, event_id, event_level, event_type, external_id, failure_reason_code, metadata, occurred_at, operation_id, provider, provider_account, resolution_path_code, source_time_quality, status.

Metadata paths: policy.rule_id, policy.rule_version, policy.target_event_type, policy.target_contract_version, data.effect, data.evidence_refs, data.case_ref, data.affected_event_ids, data.selected_event_id, data.disposition_code, data.new_operation_id.

### engine_links

Consumer: authorized internal fold only.

Exact output: event_id, link_type, link_id, relationship.

patient_event_links dependencies: event_id, link_id, link_type, relationship.

patient_events dependencies: event_id, operation_id.

Metadata paths: none.

### engine_binding

Consumer: authorized acceptance comparison only.

Exact output: operation_id, provider, provider_account, identity_namespace, external_id, uniqueness_scope, source_event_id.

operation_acceptance_bindings dependencies: external_id, identity_namespace, operation_id, provider, provider_account, source_event_id, uniqueness_scope.

Metadata paths: none.

### projection_default

Consumer: authorized default public projection.

Exact output: operation_id, status, terminal, conflict, indeterminate, controlling_event_ids, explanation_code, projection_engine_version, refreshed_at.

operation_projections dependencies: conflict, controlling_event_ids, explanation_code, indeterminate, operation_id, projection_engine_version, refreshed_at, status, terminal.

operation_registry dependencies: operation_id, subject_id, subject_type.

Metadata paths: none.

### projection_freshness_internal

Consumer: service freshness comparison; no direct public serialization.

Exact output: operation_id, source_event_count, source_max_append_sequence, source_set_fingerprint.

operation_projections dependencies: operation_id, source_event_count, source_max_append_sequence, source_set_fingerprint.

patient_events dependencies: append_sequence, content_fingerprint, event_id, operation_id.

Metadata paths: none.

### timeline_default

Consumer: authorized default timeline.

Exact output: event_id, operation_id, event_type, event_level, status, occurred_at, created_at, source_time_quality, corrected_event_id, correction_scope, correction_reason, principal_kind.

patient_events dependencies: actor_id, append_sequence, corrected_event_id, correction_reason, correction_scope, created_at, event_id, event_level, event_type, occurred_at, operation_id, patient_id, source_time_quality, status, workload_id.

patient_event_links dependencies: event_id, link_id, link_type, relationship.

Metadata paths: none.

### stale_intent

Consumer: authorized M06 detector service only.

Exact output: requested_event_id, operation_id, owner_service, action_code, created_at, provider, provider_account, external_id.

patient_events dependencies: append_sequence, contract_version, corrected_event_id, correction_scope, created_at, event_id, event_level, event_type, metadata, operation_id, resolution_path_code, status.

operation_registry dependencies: action_code, operation_id, owner_service, subject_id, subject_type.

operation_acceptance_bindings dependencies: external_id, operation_id, provider, provider_account.

patient_event_links dependencies: event_id, link_id, link_type, relationship.

Metadata paths: policy.rule_id, policy.rule_version, data.effect, data.affected_event_ids, data.selected_event_id, data.disposition_code, data.new_operation_id.

### synthetic_label_view

Consumer: non-production explicitly authorized metadata test view only.

Exact output: event_id, label_code.

patient_events dependencies: append_sequence, contract_version, corrected_event_id, correction_scope, event_id, event_type, metadata, operation_id.

Metadata paths: label_code, policy.rule_id, policy.rule_version, data.label_code.

## Exact proposed read-owner base-column SELECT union

This is the computed union of the view-dependency declarations above, not a claim that pg_depend has verified it. No UPDATE/INSERT/DELETE/TRUNCATE is granted to read owner. No read of operation_key_versions, access_audit_outbox or event_handoff_outbox is included. A new dependency must update the declaration and receive review; a view that needs absent columns must fail verification rather than acquire a broad table grant.

- operation_acceptance_bindings: external_id, identity_namespace, operation_id, provider, provider_account, source_event_id, uniqueness_scope.

- operation_projections: conflict, controlling_event_ids, explanation_code, indeterminate, operation_id, projection_engine_version, refreshed_at, source_event_count, source_max_append_sequence, source_set_fingerprint, status, terminal.

- operation_registry: action_code, operation_id, owner_service, subject_id, subject_type.

- patient_event_links: event_id, link_id, link_type, relationship.

- patient_events: actor_id, append_sequence, content_fingerprint, contract_version, corrected_event_id, correction_reason, correction_scope, created_at, event_id, event_level, event_type, external_id, failure_reason_code, metadata, occurred_at, operation_id, patient_id, provider, provider_account, resolution_path_code, source_time_quality, status, workload_id.

Whole metadata privilege is unavoidable for these JSON path expressions and is explicitly disclosed. The owner can read whole values through its column privilege; returned paths are restricted by view/function bodies. Views owned by the same read owner are not an independent security barrier against a compromised owner. Business callers receive neither owner membership nor base-table access.

## Required verification, no execution claim

Compare transitive view pg_depend column sets with this manifest, then compare actual column privileges, effective role memberships and function bodies. Include predicates/join/sort columns, not only returned columns. Verify missing grant, extra grant, whole-row view, unlisted nested path, altered view owner, direct view access and default-privilege leakage separately. Run valid and denied paths for runtime/correction/projection_worker; test TC26/TC29 plus public TC12/TC23. A JSON manifest matching itself is not evidence of correct SQL.

Read-owner SELECT closure does not certify all append/correction/projection owner query grants. Retain their reviewed purpose-specific mutation sets and compile any new internal dependencies during implementation review. The tenant-binding registry grants are separately listed in the decision annex, not hidden in this public read union.

## Targeted delta 232 — conflict paths

For m02_test_acceptance_conflict only, include metadata.candidate_identity_namespace and metadata.candidate_uniqueness_scope with provider/provider_account/external_id and the canonical binding envelope. For m02_test_delivery_conflict only, include metadata.competing_event_ids. All three are closed paths and require historical descriptor validation. No base-column grant changes: metadata is already in engine_events' declared dependencies. JSON v2 adds exactly these three paths; all eight outputs and the base-column union remain unchanged. Missing IDs, foreign/self references, duplicate/unsorted IDs, unverified or compatible alleged conflicts fail semantic validation; the engine never receives an unrestricted metadata body.
