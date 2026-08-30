# M02-OI-007 — Core Seed Catalogue, Extension Ownership, Compatibility, and Startup Validation

**Status:** Accepted / Frozen
**Date:** 2026-08-30
**Decision owner:** Rachel (Product + Engineering, per Requirements v1.0 §13 OI-007 ownership)
**Resolves:** M02-OI-007 (Requirements v1.0 §13; Technical Design v0.3 §19 — "Open content freeze ... Before seed migration")
**Depends on:** ADR-011 (Event and Operation Foundation), specifically D-11.2 (six event levels), D-11.7
(unknown-versus-conflict vocabulary and `status_scope`), D-11.14 (reference-controlled vocabulary, no
PostgreSQL ENUM), D-11.16 (reference-catalogue generation ledger)
**Review history:** drafted 2026-08-30; reviewed by ChatGPT (`chatgpt_review-of-oi-007-seed-catalog-draft.md`),
six findings (F1–F6) confirmed and incorporated; one further field-versus-link modeling correction approved
by Rachel and incorporated in the rebuild; approved and frozen 2026-08-30 with no further open questions.

Scope, per OI-007's own wording: the M02 core seed catalogue, the owning-module extension catalogue, code
ownership, the compatibility process, and startup validation behaviour. This is the content the seed
migration in PR-2 is generated from — no field, code, or rule in that migration should exist that isn't
traceable to a line in this document.

---

## 0. Decisions this catalogue makes, and their disposition after review

| # | Decision | What's seeded | Disposition |
|---|---|---|---|
| S1 | Is `event_level` controlled by a reference table? | `ref_event_levels`, seeded with the six D-11.2 levels | **Approved as a table.** FR-018 lists levels in the same reference-controlled sentence as everything else in the core seed set. |
| S2 | Intent status name | `requested` (not `pending`) | Unchanged. Design §5.1/§7.2 write "requested/pending" as one concept; `requested` matches the `{action}_requested` type name. |
| S3 | Business-outcome statuses | `business_succeeded` / `business_failed`, distinct from `delivered` / `delivery_failed` | **Approved as distinct codes.** A verified EMR write and a verified SMS delivery are different facts at different levels; reusing delivery codes would corrupt per-contract compatibility grouping (see F1/F2 below). |
| S4 | Do correction/observation events carry statuses? | `completed`, `correction_recorded`, `resolution_recorded` | Unchanged. Every row needs a valid status; FR-009 names "completed" for observations. |
| S5 | Explanation codes: same domain as statuses? | No — separate `ref_projection_explanations` | Unchanged. Different cardinality, different lifetime; design §9.2 lists them independently of §7.2's projection statuses. |
| S6 | Namespace ownership model | Exclusive, module-prefixed, registered by migration | Unchanged in principle. **Corrected in scope** (see Prefix Ownership below): OI-007 freezes the *rule*, not specific module grants — my first draft assigned `emr_` to two owners at once, which the rule itself forbids. |
| S7 | How is M02 independently testable? | A reserved, non-production test family | **Renamed and completed.** `m02_foundation` → **`m02_test_`**; the family gains explicit `operation_id`/`correlation_id` requirements and business-outcome coverage it was missing (F3, F4). |
| S8 | Precedence numbering | Sparse, spaced by 100 | **Relocated, not removed.** Now per-contract (F1), not per-status. |
| S9 | What does `compatible_outcome_class` do? | Groups statuses whose co-occurrence at one level tier is not a conflict | **Corrected.** It is declared per contract version, not globally per status code (F1/F2). |

---

## 1. `ref_event_levels` — six levels (D-11.2), unchanged

| code | ordinal | is_terminal_capable | notes |
|---|---|---|---|
| `intent` | 100 | no | Committed decision to attempt an external side effect |
| `submission` | 200 | yes | Immediate normalised call result |
| `delivery` | 300 | yes | Verified provider delivery outcome |
| `business_outcome` | 300 | yes | Verified domain result; same ordinal as `delivery` by design |
| `observation` | — | n/a | Not folded into operation projection unless the contract declares a rule |
| `correction` | — | n/a | Never a business level; applied through the correction graph |

`delivery` and `business_outcome` share ordinal 300 deliberately, matching design §9.1's "delivery/business
outcome over submission over intent" — one precedence tier. Two verified terminal facts at ordinal 300 are
compared by the rule in §3 below, which is now contract-scoped rather than status-scoped.

## 2. `ref_event_statuses` — core seed, **restructured per F1**

**F1 correction.** The first draft put `level`, `terminality`, `compatible_outcome_class`, and
`precedence_rank` on this table. Design §5.2 assigns all four to the versioned `EventContract` descriptor,
keyed by `(event_type, version)` — not to the shared status row. Putting them here would make a status's
terminality universal across every contract that ever uses it, which contradicts FR-019's requirement that a
contract version can define terminality behaviour before that version is retired. This table now carries
only what FR-018 explicitly assigns to a status: the code, its `status_scope`, and its lifecycle.

| code | status_scope | description |
|---|---|---|
| `requested` | stored_event | Durable intent committed; no submission fact yet |
| `accepted` | stored_event | Provider accepted; delivery still expected |
| `rejected` | stored_event | Definitive submission rejection |
| `indeterminate` | stored_event | Outcome not yet known; M06 reconciliation; never auto-resend |
| `delivered` | stored_event | Verified delivery |
| `delivery_failed` | stored_event | Verified delivery failure |
| `business_succeeded` | stored_event | Verified domain completion |
| `business_failed` | stored_event | Verified domain failure |
| `conflict` | stored_event | Stored status of a `*_conflict` event; retains contradictory evidence |
| `completed` | stored_event | Accepted inbound or material internal fact |
| `correction_recorded` | stored_event | Authorised correction or supersession |
| `resolution_recorded` | stored_event | Authorised manual resolution (FR-035) |
| `acceptance_conflict_requires_reconciliation` | **projection_only** | Competing provider acceptance identity; non-terminal |
| `delivery_conflict_requires_reconciliation` | **projection_only** | Contradictory verified terminal outcomes; non-terminal |
| `projection_unsupported` | **projection_only** | Unknown or incompatible contract/status; success never claimed |

Same fifteen codes as before — F1 changes the table's *shape*, not the vocabulary. The three
`projection_only` codes are rejected by the database on event insert (D-11.7); which levels and which
contracts may use `conflict` is now declared per contract (§3), not implied by a level list on this row.

## 3. `event_contracts` — where terminality, class, and precedence actually live

Each contract row (`event_type`, `version`) carries, per design §5.2:

    event_type, version, action_family, event_level,
    allowed_statuses[], terminality, compatible_outcome_class, precedence_rank,
    required_fields[], required_links[], optional_links[],
    metadata_json_schema, metadata_max_bytes,
    authorized_producers[], required_provenance[], correction_policy,
    projection_rule_id, effective_from, retired_at

`required_fields[]` is new relative to the first draft — it did not distinguish fields from links, which is
exactly the correction approved this round. See §5 for why.

**Conflict comparison (F2 correction).** "Two terminal facts conflict" is not evaluated from a global status
property. The ProjectionEngine compares the **exact stored contract versions** of the two competing facts:
if both are at the same `event_level` ordinal (§1) and their contracts declare different
`compatible_outcome_class` values — or the applicable `projection_rule_id` otherwise declares them
incompatible — the pair is a conflict. A status code by itself decides nothing; the contract it was recorded
under does.

## 4. `ref_projection_explanations` — the ten codes of design §9.2, unchanged

`INTENT_ONLY`, `SUBMISSION_ACCEPTED`, `SUBMISSION_REJECTED`, `SUBMISSION_INDETERMINATE`, `VERIFIED_OUTCOME`,
`ACCEPTANCE_CONFLICT`, `TERMINAL_OUTCOME_CONFLICT`, `CORRECTION_APPLIED`, `CONTRACT_UNSUPPORTED`,
`INTEGRITY_INCOMPLETE`. Seeded verbatim as a distinct domain (S5). Every `ProjectionResult` carries exactly
one.

## 5. Fields versus links — the correction approved this round

Design §4.3 lists `operation_id`, `correlation_id`, and `corrected_event_id` as **columns on `patient_events`
itself**. Design §4.4 defines `patient_event_links` as a separate mechanism for **typed references to
patient, appointment, log, batch/run, message, document, or an approved subject/source**. These are two
different tables with two different integrity mechanisms — a link is a row in `patient_event_links`;
`operation_id` is not.

My first draft's foundation intent contract listed only "subject or source" under required links, silently
treating `operation_id` as if it belonged in the same bucket. It doesn't, and FR-006 names both explicitly:
*"carry operation_id, correlation_id, subject or source linkage."* The corrected shape, applied to every
foundation contract below:

- **Required fields** — direct `patient_events` columns: `operation_id` (for every level tied to an
  operation), `correlation_id` (universal — §4.3 makes it `required` on every row, not just operation-scoped
  ones).
- **Required links** — rows in `patient_event_links`: subject or an approved source link.
- **Other constraints** — e.g. "`external_id` forbidden," which is a value constraint on a field that is
  present in the schema but must be null under this contract, not a link at all.

`operation_id` itself is **nullable** at the schema level (§4.3: *"Required for external side effects,
failure/conflict, correction of operation facts, and manual resolution"*) — it is required by *contract*,
conditionally, not required by every row in the table. The observation contract below is the case where it's
correctly absent.

## 6. `event_types` / `event_contracts` — the `m02_test_` family (F3, F4, F6)

Renamed from `m02_foundation` (F6): the prohibition on production use is now visible in the type name itself,
and the fix is enforceable — see §9 condition 8.

| event_type | level | status | terminality | class | rank | required fields | required links | other constraints |
|---|---|---|---|---|---|---|---|---|
| `m02_test_requested` | intent | `requested` | no | — | 100 | `operation_id`, `correlation_id` | subject or source | `external_id` forbidden |
| `m02_test_accepted` | submission | `accepted` | no | `submission_positive` | 100 | `operation_id`, `correlation_id` | — | `external_id` present only if verified |
| `m02_test_rejected` | submission | `rejected` | **yes** | `submission_negative` | 200 | `operation_id`, `correlation_id` | — | `external_id` forbidden |
| `m02_test_submission_indeterminate` | submission | `indeterminate` | no | `submission_unknown` | 150 | `operation_id`, `correlation_id` | — | `external_id` forbidden |
| `m02_test_delivered` | delivery | `delivered` | **yes** | `outcome_positive` | 100 | `operation_id`, `correlation_id` | provider | — |
| `m02_test_delivery_failed` | delivery | `delivery_failed` | **yes** | `outcome_negative` | 200 | `operation_id`, `correlation_id` | provider | — |
| `m02_test_business_succeeded` | business_outcome | `business_succeeded` | **yes** | `outcome_positive` | 100 | `operation_id`, `correlation_id` | approved business-resource link | — |
| `m02_test_business_failed` | business_outcome | `business_failed` | **yes** | `outcome_negative` | 200 | `operation_id`, `correlation_id` | approved business-resource link | — |
| `m02_test_acceptance_conflict` | submission | `conflict` | no | `conflict_evidence` | 900 | `operation_id`, `correlation_id` | provider | retains both competing identities |
| `m02_test_delivery_conflict` | delivery | `conflict` | no | `conflict_evidence` | 900 | `operation_id`, `correlation_id` | provider | retains both competing facts |
| `m02_test_observation_received` | observation | `completed` | **yes** | `observation` | 100 | `correlation_id` **only** — no `operation_id` | subject or source | matches FR-009: no synthetic intent |
| `m02_test_correction` | correction | `correction_recorded` | no | `correction` | 100 | `correlation_id`; `operation_id` only if the corrected event was operation-scoped | corrected event | never a business level |
| `m02_test_resolution` | correction | `resolution_recorded` | no | `correction` | 200 | `operation_id`, `correlation_id` | corrected event | FR-035 |

Thirteen contracts (two more than the first draft: `business_succeeded`, `business_failed`), now exercising
all six levels defined in §1, including a delivery-vs-business-outcome conflict pair at the shared ordinal-300
tier — closing F4. `m02_test_observation_received` correctly has **no** `operation_id` requirement: real
inbound observations (`fax_received`, `patient_reply_received`) are null-operation per D-11.11, and FR-009
forbids manufacturing a synthetic intent to give them one. Getting this contract's fields right is itself
part of what F4 was checking for — a foundation family that got every contract's shape identical to the
operation-scoped ones wouldn't actually test the null-operation path design §9.1 and D-11.11 both depend on.

**Production disposition (F6).** `m02_test_*` is seeded `is_active = false` in any catalogue generation
published toward a production deployment (see D-11.16's generation ledger — activation state is part of what
a generation publishes). It is activated only in a separate, explicitly non-production seed profile used for
synthetic tenants and CI. Startup validation condition 8 (§9) rejects an *active* contract in the `m02_test_`
namespace in any generation a production deployment loads — a check against the namespace and the loaded
generation's activation state, not against a per-tenant flag that this shared, control-plane table has no way
to carry.

## 7. Controlled reason-code domains — approved as originally drafted

**`ref_correction_reasons`** — M02-owned and closed (correction authority is a security boundary):
`metadata_error`, `misattributed_fact`, `duplicate_assertion`, `provider_data_correction`,
`operator_error`, `policy_required_supersession`.

**`ref_resolution_paths`** (FR-035) — M02-owned and closed:
`verified_via_provider_lookup`, `verified_via_domain_confirmation`, `confirmed_no_side_effect`,
`accepted_unresolvable`, `authorised_resend_issued`.

**`ref_failure_reasons`** — M02 seeds only the foundation codes; module-extensible, since only the owning
module knows why its action failed (FR-030): `transport_timeout`, `transport_error`, `provider_rejected`,
`provider_unavailable`, `unsupported_provider_mapping` (FR-023), `internal_precondition_failed`.

## 8. Namespace ownership — rules frozen here; grants deferred to each module

**Do not freeze specific module prefix assignments in OI-007.** The first draft's table assigned
`sms_`→M07, `fax_`→M08, `elig_`/`emr_`→M09, `emr_`/`intel_`→M12 — inferred from the delivery tracker's module
register, not approved by any of those owners, and internally contradictory: `emr_` was assigned to two
modules at once, which the exclusivity rule below exists specifically to prevent. OI-007 governs the
*mechanism*; it should not also be silently deciding module boundaries no module owner has signed off on.

**Frozen here — the mechanism:**

- A new shared reference table, `ref_action_families` — `prefix` (PK, enforcing exclusivity in the
  database, not just by convention), `owner_module`, `registered_at`, `registered_by`.
- Prefix syntax is lowercase, `^[a-z][a-z0-9_]*_$` (trailing underscore, so `sms_` cannot collide with a
  future `smsx_`).
- Registration happens only through a reviewed migration — never at runtime, matching FR-018's "owning
  business module shall install its event-type extensions before enabling its producer."
- No rename or reassignment of a prefix once any event type under it has a stored row. Changing meaning
  requires a new prefix and new event types, per the same principle as D-11.14's "retire, never edit."
- `m02_test_` is seeded here as the one reserved, non-production entry.

**Deferred to each module's own design/content freeze:** M07, M08, M09, and M12 each register their exact
prefix(es) in their own reviewed migration, at their own freeze gate — not in this document.

## 9. Compatibility process (FR-019) — corrected per F5

**No new contract version required:**
- adding a completely new event type in an already-registered namespace;
- adding a new status that no existing contract admits;
- a documentation-only clarification that changes neither validation nor projection behaviour.

**New contract version required:**
- adding, removing, or changing any metadata field or JSON Schema rule — **including adding an optional
  field.** FR-020 requires metadata to be *allowlisted per event contract*; an allowlist is a closed
  enumeration, so admitting a new field changes what a given input validates against today. That is a
  validation-behaviour change under design §5.2's own rule ("a new contract version only when validation or
  projection behavior changes"), regardless of whether the field is optional.
- adding, removing, or changing a required or **optional** link in `required_links[]` / `optional_links[]` —
  for the same reason: §5.3's validation order checks links against the contract's declared list, so adding
  one to that list changes what passes validation today.
- changing allowed statuses, terminality, `compatible_outcome_class`, precedence rank, required fields,
  provenance, correction policy, producer authorisation, or projection rule.

**New event type required (not a version bump):** any change that would reinterpret the meaning of rows
already stored under the current type.

**Retirement:** `is_active = false`, never deletion (D-11.14). A retired type remains readable and
projectable forever; retirement only blocks new appends.

**Fingerprint interaction:** any change to canonicalisation is an ADR-011 D-11.8
`fingerprint_algorithm_version` bump, never a silent contract edit.

## 10. Shared reference inventory — standing rule, not case-by-case

Every new physical reference domain introduced by this catalogue or by ADR-011 — `ref_event_levels`,
`ref_event_statuses`, `event_contracts`, `ref_projection_explanations`, `ref_correction_reasons`,
`ref_resolution_paths`, `ref_failure_reasons`, `ref_action_families`, and D-11.16's
`reference_catalog_generations` ledger — gets, before migration: an ownership decision (who writes it,
per §13.1's role model), a classification-manifest entry, and a Shared Infrastructure Table Inventory row,
per FR-032. This is now a standing rule for any *future* reference domain a module adds under its own
namespace, not a one-time list.

## 11. Startup validation behaviour — two-tier (F6, plus the clarification)

Validation splits into two tiers, so a deployment running a subset of modules doesn't fail startup over a
contract it will never execute:

**Tier 1 — global catalogue integrity, checked everywhere:**

1. A required core code from §§1–4 or §7 is absent or inactive in the loaded generation.
2. A status's `status_scope` disagrees with an insert, or a contract admits a `projection_only` status.
3. An `event_contracts` row references an absent type, status, level, or projection rule id.
4. A `provider_capabilities` row is active without a declared `uniqueness_scope` (ADR-011 D-11.4).
5. An active event type's prefix has no row in `ref_action_families` (§8).
6. **`m02_test_*` is active in a generation loaded by a production deployment** (F6 — corrected from the
   first draft's unenforceable "active in a production tenant").
7. The reference/contract snapshot cannot be loaded over the control plane (ADR-011 D-11.16), or its digest
   fails to verify.

**Tier 2 — this deployment's declared subset, checked locally:**

8. An active event type this deployment must run names a producer the deployment cannot execute.
9. The composed statement catalogue's key set or query digests differ from the CI manifest (ADR-011 D-11.18)
   for the statements this deployment actually registers.

Each failure is a distinct sanitised reason code and a structural alert. None degrades to a warning: an
unknown or mis-scoped code must never reach a projection, because the projection would then have to guess.

---

## Disposition of prior open questions — all resolved this round

Per Rachel's approval: *"No additional architectural decision is needed."* Every question the first draft
left open is now settled: S1 (table), S3 (distinct codes), reason-code ownership (mixed
extensible/closed, unchanged), prefix ownership (mechanism frozen, grants deferred), and the reserved family
(approved as `m02_test_`, subject to F3/F4/F6, all now incorporated above).

## Freeze status

**Frozen and accepted 2026-08-30.** This resolves M02-OI-007 (Requirements v1.0 §13; Technical Design v0.3
§19), which both documents gate as "before seed migration." The machine-readable seed source — the single
versioned file that migrations, generated fixtures, startup compatibility validation, and the
ContractRegistry snapshot (ADR-011 D-11.16) all consume — is generated from this specification and ships
with PR-2. A change to any seed code, contract shape, or namespace-registration rule in this document after
freeze follows the compatibility process in §9 of this same document, and — where it touches an ADR-011
decision rather than catalogue content — requires a superseding ADR, consistent with that file's immutability
rule.
