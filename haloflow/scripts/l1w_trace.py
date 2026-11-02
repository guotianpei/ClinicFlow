"""L-1W trace (package v1r1 Q-1): every inventory id maps to collected test nodes.

`TRACE` is a literal: id -> list of node ids. `check(trace, ids, collected)` raises
`TraceRefused(reason)` when an inventory id is missing, an extra id is present, an id
has no nodes, or a mapped node is not in `collected`. Unit tier; no database.

L-1W itself stays a release hold (UC1 trace, `pending_hold`) until selected-provider
validation; these rows are synthetic and unit-level evidence only.
"""

from __future__ import annotations

from collections.abc import Iterable


class TraceRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check(trace: dict[str, list[str]], ids: Iterable[str], collected: set[str]) -> None:
    required = set(ids)
    if set(trace) != required:
        missing = sorted(required - set(trace))
        extra = sorted(set(trace) - required)
        raise TraceRefused(f"id mismatch: missing={missing} extra={extra}")
    for key, nodes in trace.items():
        if not nodes:
            raise TraceRefused(f"{key}: no nodes")
        for node in nodes:
            if node not in collected:
                raise TraceRefused(f"{key}: node not collected: {node}")


TRACE: dict[str, list[str]] = {
    "K-00": [
        "tests/platform/test_l1w_keyset_units.py::test_k00_fetches_once_and_returns_parsed_public_keys",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k00_kid_type_or_alg_mismatch_is_key_mismatch",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k00_requested_alg_outside_allowlist_is_key_mismatch",  # noqa: E501
    ],
    "K-01": [
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[alg-not-allowed]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[duplicate-kid]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[ec-wrong-curve]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[key-ops-without-verify]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[keys-not-array]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[kid-missing]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[kid-not-string]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[kty-oct]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[not-object]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[one-bad-among-good]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[private-params]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[rsa-missing-n]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_invalid_set_rejected_whole_on_first_fetch[use-enc]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_rejected_refresh_keeps_old_set_and_does_not_reset_age",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_valid_refresh_resets_age",
        "tests/platform/test_l1w_keyset_units.py::test_k01_valid_refresh_drops_removed_keys",
        "tests/platform/test_l1w_keyset_units.py::test_k01_alg_curve_incoherence_rejects_whole_set_on_first_fetch",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k01_alg_curve_incoherent_refresh_keeps_old_set_and_age",  # noqa: E501
    ],
    "K-02": [
        "tests/platform/test_l1w_keyset_units.py::test_k02_redirect_not_followed",
        "tests/platform/test_l1w_keyset_units.py::test_k02_non_200_status_rejected[404]",
        "tests/platform/test_l1w_keyset_units.py::test_k02_non_200_status_rejected[500]",
        "tests/platform/test_l1w_keyset_units.py::test_k02_non_200_status_rejected[503]",
    ],
    "K-03": [
        "tests/platform/test_l1w_keyset_units.py::test_k03_oversized_body_rejected",
        "tests/platform/test_l1w_keyset_units.py::test_k03_more_than_max_keys_rejected",
        "tests/platform/test_l1w_keyset_units.py::test_k03_exactly_max_keys_accepted",
        "tests/platform/test_l1w_keyset_units.py::test_k03_transport_failure_with_no_set_is_unavailable[read-timeout]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k03_transport_failure_with_no_set_is_unavailable[connect-error]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k03_transport_failure_with_no_set_is_unavailable[connect-timeout]",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k03_fetch_bounded_by_total_timeout",
        "tests/platform/test_l1w_keyset_units.py::test_k03_fetch_within_timeout_succeeds",
        "tests/platform/test_l1w_keyset_units.py::test_k03_streamed_body_read_stops_at_max_bytes",
        "tests/platform/test_l1w_keyset_units.py::test_k03_non_json_body_rejected",
    ],
    "K-04": [
        "tests/platform/test_l1w_keyset_units.py::test_k04_concurrent_unknown_kids_share_one_refresh",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k04_concurrent_first_fetch_is_single_flight",
        "tests/platform/test_l1w_keyset_units.py::test_k04_failed_initial_fetch_is_rate_limited",
        "tests/platform/test_l1w_keyset_units.py::test_k04_refresh_rate_limited_then_allowed_after_interval",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k04_unknown_kid_right_after_first_install_does_not_refetch",  # noqa: E501
    ],
    "K-05": [
        "tests/platform/test_l1w_keyset_units.py::test_k05_failing_refresh_between_ttl_and_max_age_still_serves",  # noqa: E501
        "tests/platform/test_l1w_keyset_units.py::test_k05_recovery_after_stale_installs_and_serves",  # noqa: E501
    ],
    "V-01": [
        "tests/platform/test_l1w_verifier_units.py::test_v01_valid_token_yields_identity[RS256]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_valid_token_yields_identity[ES256]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_assurance_from_pinned_amr[pwd+mfa]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_assurance_from_pinned_amr[otp]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_assurance_from_pinned_amr[pwd-only]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_assurance_from_pinned_amr[empty]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_typ_variants_accepted[at+jwt]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_typ_variants_accepted[AT+JWT]",
        "tests/platform/test_l1w_verifier_units.py::test_v01_typ_variants_accepted[application/at+jwt]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v01_audience_list_containing_configured_audience",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v01_config_refuses_symmetric_or_none_algorithms[HS256]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v01_config_refuses_symmetric_or_none_algorithms[HS384]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v01_config_refuses_symmetric_or_none_algorithms[HS512]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v01_config_refuses_symmetric_or_none_algorithms[none]",  # noqa: E501
    ],
    "V-02": [
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[alg-hs256-jwk-secret]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[alg-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[alg-none]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[alg-not-allowlisted]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[bad-signature]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[crit]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[jku]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[jwk]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[kid-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[kid-not-string]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[kid-unknown]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[kty-alg-mismatch]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[typ-jwt]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[typ-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[x5c]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_and_key_refusals[x5u]",
        "tests/platform/test_l1w_verifier_units.py::test_v02_key_set_unavailable_is_dependency_not_auth",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v02_header_refusals_make_no_key_lookup",
    ],
    "V-03": [
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[amr-not-list]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[aud-cross-environment]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[auth-time-boolean]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[auth-time-float]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[auth-time-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[client-id-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[exp-at-skew-edge]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[exp-boolean]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[exp-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[exp-past]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[exp-string]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[iat-boolean]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[iat-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[iss-prefix-only]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[jti-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[nbf-boolean]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[nbf-future]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[sid-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[sub-missing]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[sub-number]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[wrong-aud]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_claim_refusals[wrong-iss]",
        "tests/platform/test_l1w_verifier_units.py::test_v03_auth_time_outside_datetime_range_is_claims_invalid[year-10000]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v03_auth_time_outside_datetime_range_is_claims_invalid[googol]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v03_auth_time_outside_datetime_range_is_claims_invalid[minus-googol]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v03_auth_time_outside_datetime_range_is_claims_invalid[before-year-1]",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v03_skew_tolerated_inside_window",
        "tests/platform/test_l1w_verifier_units.py::test_v03_id_token_shape_refused",
    ],
    "V-04": [
        "tests/platform/test_l1w_verifier_units.py::test_v04_sequential_calls_keep_rejecting_invalid_tokens",  # noqa: E501
    ],
    "V-05": [
        "tests/platform/test_l1w_verifier_units.py::test_v05_size_limit",
        "tests/platform/test_l1w_verifier_units.py::test_v05_deep_header_refused_before_signature",
        "tests/platform/test_l1w_verifier_units.py::test_v05_signed_deep_payload_is_a_bounded_refusal",  # noqa: E501
        "tests/platform/test_l1w_verifier_units.py::test_v05_header_at_depth_bound_is_not_refused_for_depth",  # noqa: E501
    ],
    "V-06": [
        "tests/platform/test_l1w_verifier_units.py::test_v06_malformed[bad-base64]",
        "tests/platform/test_l1w_verifier_units.py::test_v06_malformed[empty]",
        "tests/platform/test_l1w_verifier_units.py::test_v06_malformed[empty-signature-segment]",
        "tests/platform/test_l1w_verifier_units.py::test_v06_malformed[four-segments]",
        "tests/platform/test_l1w_verifier_units.py::test_v06_malformed[header-json-array]",
        "tests/platform/test_l1w_verifier_units.py::test_v06_malformed[header-not-json]",
        "tests/platform/test_l1w_verifier_units.py::test_v06_malformed[two-segments]",
        "tests/platform/test_l1w_verifier_units.py::test_v06_duplicate_header_keys_refused",
    ],
    # ---- W2 (alembic 008; SQL tier, PostgreSQL) ----
    "B-01": [
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b01_unbound_identity_is_refused_without_writes",  # noqa: E501
    ],
    "B-02": [
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b02_unbind_then_rebind_creates_a_new_binding_and_keeps_the_anchor",  # noqa: E501
    ],
    "B-03": [
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b03_one_active_binding_per_identity_and_per_staff",  # noqa: E501
    ],
    "B-04": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_b04_no_role_but_the_owner_can_touch_the_identity_tables[runtime]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_b04_no_role_but_the_owner_can_touch_the_identity_tables[staff_owner]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_b04_no_role_but_the_owner_can_touch_the_identity_tables[support]",  # noqa: E501
    ],
    "B-07c": [
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b07c_rebind_to_other_staff_old_sid_revoked_new_sid_admitted",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b07c_unrevoked_row_for_another_binding_is_binding_mismatch_and_revoked",  # noqa: E501
    ],
    "B-07d": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_b07d_rebind_and_admission_serialize_on_the_anchor",  # noqa: E501
    ],
    "B-08": [
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b08_unbind_rereads_after_the_anchor_lock_and_never_revokes_another_binding",  # noqa: E501
    ],
    "S-01": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s01_logout_is_per_device",
    ],
    "S-02": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s02_refreshed_token_cannot_revive_a_revoked_sid",  # noqa: E501
    ],
    "S-03": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s03_same_sid_other_auth_time_is_sid_mismatch_and_row_unchanged",  # noqa: E501
    ],
    "S-04": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s04_idle_expiry_is_revoked_and_persisted",  # noqa: E501
    ],
    "S-05": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t7_waiting_admission_sees_the_refresh_made_by_the_holder",  # noqa: E501
    ],
    "S-06": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s06_activity_is_coalesced_to_one_write_per_60s",  # noqa: E501
    ],
    "S-07": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s07_auth_time_inside_skew_allowed_beyond_skew_refused",  # noqa: E501
    ],
    "S-08": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s08_absolute_lifetime_existing_row_revoked_absent_row_not_created",  # noqa: E501
    ],
    "S-10": [
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_s10_unbind_revokes_every_active_session_of_the_identity",  # noqa: E501
    ],
    "S-12": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[issuer]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[subject]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[sid]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[tenant_id]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[staff_id]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[auth_time]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[created_at]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[revoked_at]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[revoke_reason]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[last_activity_at]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_allows_the_single_revocation_and_then_freezes_it",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_delete_and_non_logout_revoked_insert",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_logs_are_append_only[staff_identity_log]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_logs_are_append_only[tenant_auth_settings_log]",  # noqa: E501
    ],
    "S-13": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s13_t8_idle_decided_with_the_post_wait_clock",  # noqa: E501
    ],
    "S-14": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s14_absolute_decided_with_the_post_wait_clock",  # noqa: E501
    ],
    "T-1": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t1_concurrent_create_other_auth_time_is_sid_mismatch",  # noqa: E501
    ],
    "T-2": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t2_concurrent_create_same_auth_time_both_admitted_one_row",  # noqa: E501
    ],
    "T-3": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t3_logout_waits_for_an_inflight_admission_then_revokes",  # noqa: E501
    ],
    "T-4": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_lg01_logout_of_a_never_admitted_sid_leaves_a_tombstone",  # noqa: E501
    ],
    "T-5": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t5_admission_waits_for_unbind_then_is_unbound_without_a_row",  # noqa: E501
    ],
    "T-6": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t6_unbind_waits_for_an_inflight_admission_then_both_sids_unbound",  # noqa: E501
    ],
    "T-7": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t7_waiting_admission_sees_the_refresh_made_by_the_holder",  # noqa: E501
    ],
    "T-8": [
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s13_t8_idle_decided_with_the_post_wait_clock",  # noqa: E501
    ],
    "A-01": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a01_mfa_on_refuses_unassured_without_revoking",  # noqa: E501
    ],
    "A-02": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a02_mfa_off_admits_unassured",  # noqa: E501
    ],
    "A-03": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a03_idle_change_applies_to_existing_sessions",  # noqa: E501
    ],
    "A-04": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[idle_timeout_minutes-4-INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[idle_timeout_minutes-61-INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[idle_timeout_minutes-15.5-INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[idle_timeout_minutes-abc-INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[idle_timeout_minutes--INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[mfa_required-maybe-INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[mfa_required-1-INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[max_session_hours-8-INVALID_SETTING]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[-15-INVALID_SETTING]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_bounds_are_inclusive",
    ],
    "A-05": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a05_admin_and_support_changes_are_logged_with_attribution",  # noqa: E501
    ],
    "A-07": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a07_new_tenant_uses_the_owner_defaults",  # noqa: E501
    ],
    "SP-01": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp01_support_login_not_in_the_allowlist_is_refused",  # noqa: E501
    ],
    "SP-02": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp02_renamed_login_no_longer_matches_the_allowlist",  # noqa: E501
    ],
    "SP-03": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_grant_must_be_active_current_for_the_tenant_and_capable[no-grant]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_grant_must_be_active_current_for_the_tenant_and_capable[expired]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_grant_must_be_active_current_for_the_tenant_and_capable[not-started]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_grant_must_be_active_current_for_the_tenant_and_capable[revoked-state]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_grant_must_be_active_current_for_the_tenant_and_capable[other-tenant]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_grant_must_be_active_current_for_the_tenant_and_capable[missing-capability]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_capabilities_are_separate",  # noqa: E501
    ],
    "SP-04": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp04_valid_support_bind_and_unbind_are_attributed",  # noqa: E501
    ],
    "SP-05": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[support-l1w_admin_set_setting]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[support-l1w_admit]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[runtime-l1w_support_unbind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[runtime-l1w_admin_bind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[staff_owner-l1w_support_bind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[staff_owner-l1w_admit]",  # noqa: E501
    ],
    "SP-06": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp06_definer_signatures_are_exact",
    ],
    "SP-07a": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[support-l1w_admin_set_setting]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[support-l1w_admit]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[runtime-l1w_support_unbind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[runtime-l1w_admin_bind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[staff_owner-l1w_support_bind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[staff_owner-l1w_admit]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp07a_runtime_has_no_execute_on_admin_or_support_definers",  # noqa: E501
    ],
    "SP-07d": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp07d_staff_owner_cannot_log_in_and_has_no_runtime_edge",  # noqa: E501
    ],
    "SP-08": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp08_grant_revoked_while_waiting_on_the_anchor_is_not_used",  # noqa: E501
    ],
    "LG-01": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_lg01_logout_of_a_never_admitted_sid_leaves_a_tombstone",  # noqa: E501
    ],
    "LG-02": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_lg02_logout_is_idempotent",
    ],
    "LG-03": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s01_logout_is_per_device",
    ],
    "LG-04": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_lg04_logout_of_an_unbound_identity_writes_nothing",  # noqa: E501
    ],
    "N-01": [
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_admission_input_is_invalid_input[issuer]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_admission_input_is_invalid_input[subject]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_admission_input_is_invalid_input[sid]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_admission_input_is_invalid_input[auth_time]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_assured_with_mfa_on_is_mfa_required_and_off_is_admitted",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_bind_input_is_invalid_input[tenant]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_bind_input_is_invalid_input[staff]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_bind_input_is_invalid_input[issuer]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_bind_input_is_invalid_input[subject]",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_bind_input_is_invalid_input[actor]",  # noqa: E501
    ],
    "X-01": [
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_x01_invalid_input_is_a_fixed_refusal_and_errors_carry_no_identifiers",  # noqa: E501
    ],
    "PRIV-01": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_priv01_function_execute_grantees_are_exact",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_priv01_tables_have_no_grant_beyond_the_owner",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_priv01_identity_owner_reads_exactly_eight_grant_columns",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_priv01_schema_usage_and_role_attributes",  # noqa: E501
    ],
    "PRIV-02": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_priv02_every_l1w_function_is_a_pinned_security_definer",  # noqa: E501
    ],
    "FX-01": [
        "tests/platform/test_l1w_w2_fixture_units.py::test_fx01_guard_refuses_outside_a_disposable_test_cluster[no-flag]",  # noqa: E501
        "tests/platform/test_l1w_w2_fixture_units.py::test_fx01_guard_refuses_outside_a_disposable_test_cluster[flag-0]",  # noqa: E501
        "tests/platform/test_l1w_w2_fixture_units.py::test_fx01_guard_refuses_outside_a_disposable_test_cluster[flag-true]",  # noqa: E501
        "tests/platform/test_l1w_w2_fixture_units.py::test_fx01_guard_refuses_outside_a_disposable_test_cluster[dev-db]",  # noqa: E501
        "tests/platform/test_l1w_w2_fixture_units.py::test_fx01_guard_refuses_outside_a_disposable_test_cluster[postgres-db]",  # noqa: E501
        "tests/platform/test_l1w_w2_fixture_units.py::test_fx01_guard_refuses_outside_a_disposable_test_cluster[prefix-not-leading]",  # noqa: E501
        "tests/platform/test_l1w_w2_fixture_units.py::test_fx01_guard_admits_flag_and_prefix",
    ],
    "FX-02": [
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_fx02_cleanup_restores_triggers_even_when_the_statement_fails",  # noqa: E501
    ],
    "PF-01": [
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf01_safe_pre_existing_roles_remain_usable",  # noqa: E501
    ],
    "PF-02": [
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_identity_owner-LOGIN]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_identity_owner-SUPERUSER]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_identity_owner-CREATEDB]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_identity_owner-CREATEROLE]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_identity_owner-REPLICATION]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_identity_owner-BYPASSRLS]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_support-LOGIN]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_support-SUPERUSER]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_support-CREATEDB]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_support-CREATEROLE]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_support-REPLICATION]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_support-BYPASSRLS]",  # noqa: E501
    ],
    "PF-03": [
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_extra_incoming_membership_refused[haloflow_identity_owner-group-role]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_extra_incoming_membership_refused[haloflow_identity_owner-superuser-login]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_extra_incoming_membership_refused[haloflow_support-group-role]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_extra_incoming_membership_refused[haloflow_support-superuser-login]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_identity_owner_refuses_even_a_plain_login_member",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_support_accepts_a_plain_login_member",  # noqa: E501
    ],
    "PF-04": [
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf04_extra_outgoing_membership_refused[haloflow_identity_owner]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf04_extra_outgoing_membership_refused[haloflow_support]",  # noqa: E501
    ],
    "PF-05": [
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf05_migrator_edge_with_other_options_refused[haloflow_identity_owner]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf05_migrator_edge_with_other_options_refused[haloflow_support]",  # noqa: E501
    ],
}
