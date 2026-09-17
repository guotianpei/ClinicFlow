# M02 CP0 — D1 signer-binding correction and Claude233 fold-ins

2026-09-10 · ChatGPT/Codex lead · Targeted reviewer check requested; CP0 incomplete.

This supersedes only the signer-message/binding comparison language in targeted-delta-232 and the metadata-label target allowance noted below. All other reviewer dispositions carry. Read Claude233 in full.

## D1 replacement contract

The signed message is the canonical domain-separated encoding of `["m02.signed-message/v1", envelope_fields_except_signature, canonical_binding_for_purpose]`; the binding argument is not transmitted as an envelope field. `verify` reconstructs that exact message using the service-derived `expected_binding`, so a different binding fails signature verification itself; remove the separate unauthenticated binding-comparison step. The fixture signer's stored canonical message includes the binding and applies the same reconstruction rule.

Canonical encoding and closed per-purpose binding schemas remain targeted-delta-232's definitions. Missing/extra binding fields, noncanonical values or unsupported purpose fail before verification/query. Verification authenticates purpose, key ID, timestamps, payload and binding together. Expected binding comes from the authenticated request/context and selected server policy, never from the token.

Cursor payload contains only the approved bounded sort-key/version fields. It does not contain the binding object or tenant/subject/selector/cutoff values; do not copy those into token keys, diagnostics or encoded side fields. This is about decoded content as well as literal string search. Internal signed run-manifest payload retains the expressly required structural manifest contents and classification; it is not a client pagination token. Its separately supplied binding is still signed and verified by reconstruction. Do not remove required tenant/run evidence from a retained manifest by confusing that payload with a cursor's privacy rule.

## Exact test delta (specification, not executed code)

| Cases | Required additional test |
|---|---|
| TC12/23/29 and TC14 | Sign under binding X, then verify identical envelope/payload/signature with binding Y, changing only one expected tenant/subject/selector/cutoff or other applicable binding component. Signature verification fails; no protected read/apply. Matching X is the control. Fixture provider compares a message that includes X/Y, not envelope bytes alone. |
| TC12/23/29 cursor serialization | Use distinct synthetic tenant/subject/selector/cutoff sentinels; inspect both serialized envelope and decoded cursor payload. No binding object or sensitive binding value is present anywhere, including encoded fields. Payload contains only allowed sort-key/version fields. Signature bytes are opaque, not an encoded binding. Do not apply this client-token exclusion to the internal manifest's explicitly required payload fields. |

## Fold-ins

Remove m02_test_acceptance_conflict and m02_test_delivery_conflict from m02_test_metadata_label_v1 target_event_type enum. This avoids changing their newly closed conflict schemas merely for a display label. codex_m02-synthetic-descriptors-v3.json differs from v2 only in those two target removals and explanatory source text. Metadata label corrections of either conflict type now fail unsupported; asserted-fact conflict correction remains governed by its separate rule. This supersedes annex v2's statement permitting label overlays on conflict types.

The acceptance_conflict provider/provider_account/external_id required fields are explicitly an OI-007 synthetic profile refinement for owner alignment, not unchanged frozen content. Draft profile version 1 is not installed; any existing historical installation requires a compatible new version.

Nothing else is reopened. No source, code/test implementation, database or Git changes.
