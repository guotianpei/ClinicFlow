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
    "SET-01": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_set01_concurrent_first_setters_of_different_settings_keep_both_values",  # noqa: E501
    ],
    "SET-02": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_set02_concurrent_first_setters_of_one_setting_log_an_accurate_chain",  # noqa: E501
    ],
    "SET-03": [
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_set03_support_grant_revoked_while_waiting_on_the_first_row_is_refused",  # noqa: E501
    ],
    # ---- W3a (tenant units t026-t032; v1 still callable until W3b) ----
    "COMP-W3A": [
        "tests/m05/test_l1w_w3a_composition_units.py::test_w3a_u01_production_unit_ids_and_target",
        "tests/m05/test_l1w_w3a_composition_units.py::test_w3a_u02_unit_kinds_and_execution_roles",
        "tests/m05/test_l1w_w3a_composition_units.py::test_w3a_u03_profiles_exist_for_every_typed_w3a_unit_with_exact_acl",  # noqa: E501
        "tests/m05/test_l1w_w3a_composition_units.py::test_w3a_u04_supported_version_literal_is_32",
        "tests/m05/test_l1w_w3a_composition_units.py::test_w3a_u06_v1_functions_stay_installed_in_w3a",  # noqa: E501
    ],
    "C-03": [
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_c03_forged_supervise_without_role_row_is_refused_state_unchanged",  # noqa: E501
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_c03p_real_supervisor_assign_and_reassign_are_applied",  # noqa: E501
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_c03p_real_supervisor_acts_on_another_members_item",  # noqa: E501
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_c03_caller_flag_only_narrows",
    ],
    "C-04": [
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_c04_forged_staff_admin_without_role_row_is_refused",  # noqa: E501
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_c04p_real_clinic_admin_grants_a_role",
    ],
    "C-05": [
        "tests/m05/test_l1w_w3a_composition_units.py::test_w3a_u05_repointed_and_new_statements",
    ],
    "R-02": [
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_r02_supervisor_removed_is_refused_at_the_next_call",  # noqa: E501
    ],
    "R-03": [
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_r03_self_change_of_clinic_admin_is_refused[grant_role]",  # noqa: E501
        "tests/m05/test_l1w_w3a_authority_postgres.py::test_r03_self_change_of_clinic_admin_is_refused[revoke_role]",  # noqa: E501
    ],
    "L-01": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l01_l02_reciprocal_admin_revoke_serializes_on_the_guard[a-first]",  # noqa: E501
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l01_l02_reciprocal_admin_revoke_serializes_on_the_guard[b-first]",  # noqa: E501
    ],
    "L-02": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l01_l02_reciprocal_admin_revoke_serializes_on_the_guard[a-first]",  # noqa: E501
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l01_l02_reciprocal_admin_revoke_serializes_on_the_guard[b-first]",  # noqa: E501
    ],
    "L-03": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l03_m05_assign_and_deactivate_of_the_assignee_serialize",  # noqa: E501
    ],
    "L-04": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l04_self_claim_takes_the_actor_row_once",  # noqa: E501
    ],
    "L-05": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l05_v2_bodies_take_every_membership_lock_before_the_item",  # noqa: E501
    ],
    "L-06": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l06_bootstrap_waits_on_an_admin_role_change_then_sees_the_admin",  # noqa: E501
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l06_bootstrap_waits_on_a_status_change_of_an_admin[deactivate]",  # noqa: E501
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_l06_bootstrap_waits_on_a_status_change_of_an_admin[activate]",  # noqa: E501
    ],
    "BS-01": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs01_real_bootstrap_path",
    ],
    "BS-02": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs02_identity_and_grant_refusals_write_nothing[unlisted]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs02_identity_and_grant_refusals_write_nothing[renamed]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs02_identity_and_grant_refusals_write_nothing[no-grant]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs02_identity_and_grant_refusals_write_nothing[other-tenant]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs02_identity_and_grant_refusals_write_nothing[no-capability]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs02_identity_and_grant_refusals_write_nothing[expired]",  # noqa: E501
    ],
    "BS-03": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs03_admin_exists_and_inactive_target",
    ],
    "BS-04": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs04_concurrent_bootstraps_for_different_targets_serialize",  # noqa: E501
    ],
    "BS-05": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs05_grant_revoked_while_waiting_on_the_guard",  # noqa: E501
    ],
    "BS-06": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs06_replay_returns_the_stored_outcome_after_reauthorization",  # noqa: E501
    ],
    "BS-07": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs07_bootstrap_privileges_are_exact",
    ],
    "BS-08": [
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[grant_role-True]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[revoke_role-True]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[deactivate-True]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[activate-True]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[rename-False]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[grant-False]",  # noqa: E501
        "tests/m05/test_l1w_w3a_bootstrap_postgres.py::test_bs08_bootstrap_blocks_on_the_guard",
    ],
    "B-05": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_b05_self_bind_and_foreign_target_are_refused",  # noqa: E501
    ],
    "A-06": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_a06_sp07c_writes_land_on_this_schemas_tenant_only",  # noqa: E501
    ],
    "SP-07b": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_sp07b_admin_v2_takes_no_tenant_argument",  # noqa: E501
    ],
    "SP-07c": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_a06_sp07c_writes_land_on_this_schemas_tenant_only",  # noqa: E501
    ],
    "PIN2-01": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_pin201_m05_owner_reads_staff_standing_only_through_two_gateways",  # noqa: E501
    ],
    "PRIV-W3-01": [
        "tests/m05/test_l1w_w3a_locks_grants_postgres.py::test_privw301_staff_owner_shared_column_grants_are_exact",  # noqa: E501
    ],    "SUP-01": [
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup01_support_is_a_usage_only_profile_with_the_008_edge_and_not_approved",  # noqa: E501
    ],
    "SUP-02": [
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup02_a_unit_run_as_support_is_refused_by_the_production_registry",  # noqa: E501
    ],
    "SUP-03": [
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup03_the_controlled_graph_refuses_unsafe_support_edges[undeclared-support-to-staff-owner]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup03_the_controlled_graph_refuses_unsafe_support_edges[undeclared-runtime-to-support]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup03_the_controlled_graph_refuses_unsafe_support_edges[missing]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup03_the_controlled_graph_refuses_unsafe_support_edges[inherit]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup03_the_controlled_graph_refuses_unsafe_support_edges[admin]",  # noqa: E501
    ],
    "SUP-04": [
        "tests/m05/test_l1w_w3a_impl_postgres.py::test_sup04_support_login_membership_survives_provisioning",  # noqa: E501
    ],
    "SVC-01": [
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_reactivate_is_sent_as_activate_and_bound_back[applied]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_reactivate_is_sent_as_activate_and_bound_back[replayed]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_deactivate_binds_to_the_v1_shape",
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_result_validation_stays_strict[reactivate-missing-outcome]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_result_validation_stays_strict[reactivate-bad-outcome]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_result_validation_stays_strict[reactivate-list-outcome]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_result_validation_stays_strict[reactivate-extra-key]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_result_validation_stays_strict[reactivate-v1-name-returned]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_result_validation_stays_strict[deactivate-wrong-operation]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_result_validation_stays_strict[deactivate-v1-four-keys-only]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_svc01_the_public_operation_set_is_unchanged",
    ],
    "SUP-05": [
        "tests/m05/test_l1w_w3a_support_safety_postgres.py::test_sup05_provisioner_refuses_drifted_support_before_any_mutation[bypassrls]",  # noqa: E501
        "tests/m05/test_l1w_w3a_support_safety_postgres.py::test_sup05_provisioner_refuses_drifted_support_before_any_mutation[login]",  # noqa: E501
        "tests/m05/test_l1w_w3a_support_safety_postgres.py::test_sup05_provisioner_refuses_drifted_support_before_any_mutation[missing]",  # noqa: E501
    ],
    "SUP-06": [
        "tests/m05/test_l1w_w3a_support_safety_postgres.py::test_sup06_runner_refuses_drifted_support_ledger_unchanged[bypassrls]",  # noqa: E501
        "tests/m05/test_l1w_w3a_support_safety_postgres.py::test_sup06_runner_refuses_drifted_support_ledger_unchanged[login]",  # noqa: E501
        "tests/m05/test_l1w_w3a_support_safety_postgres.py::test_sup06_runner_refuses_drifted_support_ledger_unchanged[missing]",  # noqa: E501
    ],
    "SUP-07": [
        "tests/m05/test_l1w_w3a_support_safety_postgres.py::test_sup07_safe_support_with_a_login_member_provisions",  # noqa: E501
    ],
    "SUP-08": [
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup08_every_schema_privileged_profile_is_read_after_registry_roles",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup08_unsafe_or_missing_support_refuses_even_for_an_empty_registry[login]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup08_unsafe_or_missing_support_refuses_even_for_an_empty_registry[bypassrls]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup08_unsafe_or_missing_support_refuses_even_for_an_empty_registry[missing]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_sup08_a_registry_role_without_a_profile_is_still_refused",  # noqa: E501
    ],
    "L-07": [
        "tests/m05/test_l1w_w3a_impl_units.py::test_l07_guard_precedes_every_membership_lock[t029_m01_staff_admin_v2]",  # noqa: E501
        "tests/m05/test_l1w_w3a_impl_units.py::test_l07_guard_precedes_every_membership_lock[t032_m01_staff_bootstrap_admin]",  # noqa: E501
    ],
    "GB-01": [
        "tests/m05/test_l1w_w3a_impl_postgres.py::test_gb01_granted_by_length_boundary[136-True]",
        "tests/m05/test_l1w_w3a_impl_postgres.py::test_gb01_granted_by_length_boundary[137-False]",
        "tests/m05/test_l1w_w3a_impl_postgres.py::test_gb01_granted_by_length_boundary[0-False]",
    ],
    # L-1W W3b (plan v1r1 + addendum 1; Codex 150630Z/151751Z/154906Z): red tranche.
    "FM-01": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm01_empty_clinic_gets_member_and_admin_in_one_step",  # noqa: E501
    ],
    "FM-02": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm02_members_without_an_active_admin_do_not_block[active-member]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm02_members_without_an_active_admin_do_not_block[inactive-member]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm02_members_without_an_active_admin_do_not_block[inactive-admin]",  # noqa: E501
    ],
    "FM-03": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm03_active_admin_exists_writes_only_the_key",  # noqa: E501
    ],
    "FM-04": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm04_existing_staff_id_is_target_exists[active]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm04_existing_staff_id_is_target_exists[inactive]",  # noqa: E501
    ],
    "FM-04b": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm04b_staff_row_inserted_while_waiting_is_target_exists_no_partial_rows",  # noqa: E501
    ],
    "FM-05": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05_identity_and_grant_refusals_write_nothing[unlisted]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05_identity_and_grant_refusals_write_nothing[renamed]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05_identity_and_grant_refusals_write_nothing[no-grant]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05_identity_and_grant_refusals_write_nothing[other-tenant]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05_identity_and_grant_refusals_write_nothing[no-capability]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05_identity_and_grant_refusals_write_nothing[expired]",  # noqa: E501
    ],
    "FM-05b": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05b_authorization_lost_while_waiting_on_the_guard[revoked]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm05b_authorization_lost_while_waiting_on_the_guard[renamed]",  # noqa: E501
    ],
    "FM-06": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm06_replay_conflict_and_actor_mismatch",  # noqa: E501
    ],
    "FM-06b": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm06b_bootstrap_keys_are_shared_with_t032_directional_codes",  # noqa: E501
    ],
    "FM-06c": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm06c_staff_action_keys_are_an_independent_namespace",  # noqa: E501
    ],
    "FM-06d": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm06d_replay_under_a_renewed_grant_returns_the_original_outcome",  # noqa: E501
    ],
    "FM-07": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm07_concurrent_first_members_one_wins",  # noqa: E501
    ],
    "FM-07b": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm07b_first_member_and_t032_serialize_on_the_guard",  # noqa: E501
    ],
    "FM-08": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm08_first_member_privileges_are_exact",  # noqa: E501
    ],
    "FM-09": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm09_closed_shape_refusals_write_nothing[access-field]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm09_closed_shape_refusals_write_nothing[extra-key]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm09_closed_shape_refusals_write_nothing[missing-name]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm09_closed_shape_refusals_write_nothing[blank-name]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm09_closed_shape_refusals_write_nothing[long-name]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm09_closed_shape_refusals_write_nothing[bad-uuid]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm09_closed_shape_refusals_write_nothing[schema-version]",  # noqa: E501
    ],
    "FM-10": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_fm10_fresh_production_tenant_onboards_through_production_units_only",  # noqa: E501
    ],
    "ST-01": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_st01_t033_columns_and_defaults",
    ],
    "ST-02": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_st02_t032_rows_after_t033_record_bootstrap_admin",  # noqa: E501
    ],
    "ST-03": [
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_st03_display_name_present_iff_create[create_first_admin-None-False]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_st03_display_name_present_iff_create[bootstrap_admin-Has A Name-False]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_st03_display_name_present_iff_create[create_first_admin-Has A Name-True]",  # noqa: E501
        "tests/m05/test_l1w_w3b_first_member_postgres.py::test_st03_display_name_present_iff_create[bootstrap_admin-None-True]",  # noqa: E501
    ],
    "CLOSURE-UNITS": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_cl00_w3b_units_kinds_and_declared_amendments",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_cl00b_closure_statements_are_rendered_from_the_declaration_only",  # noqa: E501
    ],
    "AM-01": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am01_unknown_target_is_refused",
    ],
    "AM-02": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am02_removal_outside_the_target_profile_acl_is_refused",  # noqa: E501
    ],
    "AM-03": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am03_owner_entry_removal_is_refused",
    ],
    "AM-04": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am04_a_closure_unit_carrying_a_template_is_refused",  # noqa: E501
    ],
    "AM-05": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am05_activator_must_be_a_closure_unit_run_as_the_target_owner[t034_m01_staff_bootstrap_first_admin]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am05_activator_must_be_a_closure_unit_run_as_the_target_owner[t033_m01_staff_bootstrap_first_admin_schema]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am05_activator_must_be_a_closure_unit_run_as_the_target_owner[t036_m01_staff_v1_runtime_closure]",  # noqa: E501
    ],
    "AM-06": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am06_two_amendments_on_one_target_are_refused",  # noqa: E501
    ],
    "AM-07": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am07_activator_version_not_above_the_target_is_refused",  # noqa: E501
    ],
    "AM-08": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am08_empty_duplicate_and_orphan_closures_are_refused[empty-removes]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am08_empty_duplicate_and_orphan_closures_are_refused[duplicate-removes]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am08_empty_duplicate_and_orphan_closures_are_refused[closure-without-amendment]",  # noqa: E501
    ],
    "AM-09": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am09_before_activation_the_accepted_profile_applies_unchanged",  # noqa: E501
    ],
    "AM-10": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_am10b_non_acl_drift_on_a_closed_function_is_still_refused_at_startup[m05_prepare_action(jsonb)-SET work_mem = '64kB'-RESET work_mem]",  # noqa: E501
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_am10b_non_acl_drift_on_a_closed_function_is_still_refused_at_startup[m01_staff_admin(jsonb)-STABLE-VOLATILE]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am10_every_non_acl_field_is_unchanged_after_activation",  # noqa: E501
    ],
    "AM-11": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am11_grantee_outside_the_role_vocabulary_is_refused",  # noqa: E501
    ],
    "AM-12": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am12_closure_checksum_payload_is_exact[t035_m05_v1_runtime_closure]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am12_closure_checksum_payload_is_exact[t036_m01_staff_v1_runtime_closure]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am12b_checksum_is_the_pinned_digest_and_every_field_is_bound[t035_m05_v1_runtime_closure]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am12b_checksum_is_the_pinned_digest_and_every_field_is_bound[t036_m01_staff_v1_runtime_closure]",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am12c_amendment_declaration_order_is_normalized",  # noqa: E501
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_am12d_target_set_change_changes_only_that_units_checksum",  # noqa: E501
    ],
    "AM-13": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_am13_changed_closure_declaration_after_install_is_ledger_drift",  # noqa: E501
    ],
    "CL-01": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_cl01_exact_acl_after_provisioning_and_startup_is_green",  # noqa: E501
    ],
    "CL-02": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_cl02_restored_runtime_grant_is_drift_at_startup",  # noqa: E501
    ],
    "CL-03": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_cl03_partial_closure_fails_verification_and_is_not_applied",  # noqa: E501
    ],
    "CL-04": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_cl04_a_registry_below_36_is_refused_by_the_supported_set",  # noqa: E501
    ],
    "C-01": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c01_runtime_execute_inventory_is_exact",
    ],
    "C-02": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c02_runtime_calls_to_v1_raise_42501[m05_prepare_action(jsonb)]",  # noqa: E501
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c02_runtime_calls_to_v1_raise_42501[m05_apply_action(jsonb)]",  # noqa: E501
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c02_runtime_calls_to_v1_raise_42501[m01_staff_admin(jsonb)]",  # noqa: E501
    ],
    "CG-REV-01": [
        "tests/m01/test_l1w_w3b_acl_closure_units.py::test_cg_rev01_ordinary_role_bearing_revoke_is_still_refused",  # noqa: E501
    ],
    # A: closed to runtime; B: historical v1-contract rows run as the closed function's
    # owner (HARNESS_EXTERNAL_FILES); E: replay controls on the migrated v2 paths. C (v2
    # mutation equivalents) lives in scripts/l1w_w3b_mutants.py; D is the full test step.
    "HARNESS-MIGRATION": [
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c01_runtime_execute_inventory_is_exact",  # noqa: E501
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c02_runtime_calls_to_v1_raise_42501[m05_prepare_action(jsonb)]",  # noqa: E501
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c02_runtime_calls_to_v1_raise_42501[m05_apply_action(jsonb)]",  # noqa: E501
        "tests/m05/test_l1w_w3b_closure_postgres.py::test_c02_runtime_calls_to_v1_raise_42501[m01_staff_admin(jsonb)]",  # noqa: E501
        "tests/m05/test_m05_staff_directory_postgres.py::test_dad1b_literal_vectors_through_sql",  # noqa: E501
        "tests/m05/test_m05_staff_directory_postgres.py::test_am1_guard_after_lock_is_caught",  # noqa: E501
        "tests/m05/test_m05_staff_directory_postgres.py::test_am2_admission_without_on_conflict_is_caught",  # noqa: E501
        "tests/m05/test_m05_staff_directory_postgres.py::test_am3_weak_replay_check_is_caught",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_rm1_key_share_alone_is_equivalent_here",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_rm1b_key_share_without_writer_lock_is_caught",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_rm2_revoke_without_membership_lock_is_caught",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_rm3_merged_statements_are_caught",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_gm1_isolation_guard_after_item_lock_is_caught",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_gm2_composition_guard_after_item_lock_is_caught",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_sm1_actor_check_removed_is_caught",  # noqa: E501
        "tests/m05/test_m05_assignee_eligibility_postgres.py::test_sm2_target_check_skipped_is_caught",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri03_a_stored_result_with_outcome_is_unrepresentable",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri01_real_v2_operation_replays_with_outcome_replayed[grant_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri01_real_v2_operation_replays_with_outcome_replayed[revoke_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri01_real_v2_operation_replays_with_outcome_replayed[activate-None]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri02_impossible_timestamp_never_replays[grant_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri02_impossible_timestamp_never_replays[activate-None]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri04_activate_with_inactive_snapshot_never_replays",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri05_crafted_valid_history_replays[grant_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri05_crafted_valid_history_replays[activate-None]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri06_identity_and_setting_operations_replay_exactly[bind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri06_identity_and_setting_operations_replay_exactly[unbind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri06_identity_and_setting_operations_replay_exactly[set_auth_setting]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri07_retry_after_the_actor_lost_authority_is_refused[bind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri07_retry_after_the_actor_lost_authority_is_refused[unbind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri07_retry_after_the_actor_lost_authority_is_refused[set_auth_setting]",  # noqa: E501
    ],
    "RI-06": [
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri06_identity_and_setting_operations_replay_exactly[bind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri06_identity_and_setting_operations_replay_exactly[unbind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri06_identity_and_setting_operations_replay_exactly[set_auth_setting]",  # noqa: E501
    ],
    "RI-07": [
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri07_retry_after_the_actor_lost_authority_is_refused[bind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri07_retry_after_the_actor_lost_authority_is_refused[unbind_identity]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri07_retry_after_the_actor_lost_authority_is_refused[set_auth_setting]",  # noqa: E501
    ],
    "RI-01": [
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri01_real_v2_operation_replays_with_outcome_replayed[grant_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri01_real_v2_operation_replays_with_outcome_replayed[revoke_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri01_real_v2_operation_replays_with_outcome_replayed[activate-None]",  # noqa: E501
    ],
    "RI-02": [
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri02_impossible_timestamp_never_replays[grant_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri02_impossible_timestamp_never_replays[activate-None]",  # noqa: E501
    ],
    "RI-03": [
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri03_a_stored_result_with_outcome_is_unrepresentable",  # noqa: E501
    ],
    "RI-04": [
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri04_activate_with_inactive_snapshot_never_replays",  # noqa: E501
    ],
    "RI-05": [
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri05_crafted_valid_history_replays[grant_role-supervisor]",  # noqa: E501
        "tests/m05/test_l1w_w3b_replay_integrity_postgres.py::test_ri05_crafted_valid_history_replays[activate-None]",  # noqa: E501
    ],
    # L-1W W4a (plan v1 + addendum 1; Codex eeaddf94 / 183528a3 / ff2ef544 / 94a45a4f / 413421dd).
    "E-L1": [
        "tests/m05/test_l1w_w4a_launcher_postgres.py::test_el1a_unservable_tenant_exits_nonzero_without_accepting",  # noqa: E501
        "tests/m05/test_l1w_w4a_launcher_postgres.py::test_el1b_missing_configuration_exits_nonzero_without_accepting[HALOFLOW_PLATFORM_SERVED_TENANTS]",  # noqa: E501
        "tests/m05/test_l1w_w4a_launcher_postgres.py::test_el1b_missing_configuration_exits_nonzero_without_accepting[HALOFLOW_PLATFORM_AUTH_ISSUER]",  # noqa: E501
        "tests/m05/test_l1w_w4a_launcher_postgres.py::test_el1b_missing_configuration_exits_nonzero_without_accepting[HALOFLOW_PLATFORM_RUNTIME_DATABASE_URL]",  # noqa: E501
        "tests/m05/test_l1w_w4a_launcher_postgres.py::test_el1c_good_configuration_serves_after_startup_and_exits_cleanly",  # noqa: E501
    ],
    "E-L2": [
        "tests/platform/test_l1w_w4a_app_units.py::test_el2_without_lifespan_every_request_is_503[/healthz-GET]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el2_without_lifespan_every_request_is_503[/v1/session/logout-POST]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el2_without_lifespan_every_request_is_503[/anything-GET]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el2_shutdown_unpublishes_before_closing",
        "tests/platform/test_l1w_w4a_app_units.py::test_el2_startup_order_is_pinned",
    ],
    "E-L3": [
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[tenant_pool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[services]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[admission_pool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[keyset]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[verifier]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_fault_after_each_step_closes_what_was_opened_and_reraises[admission]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_close_failure_never_masks_the_original[keyset]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_close_failure_never_masks_the_original[verifier]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_close_failure_never_masks_the_original[admission]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_cleanup_failure_alone_fails_startup",
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_cancellation_during_startup_wins_and_cleans_up",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_el3_cancellation_during_cleanup_wins",
        "tests/platform/test_l1w_w4a_app_postgres.py::test_el3_real_pools_partial_startup_leaves_no_backend[fault-keyset]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_el3_real_pools_partial_startup_leaves_no_backend[fault-verifier]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_el3_real_pools_partial_startup_leaves_no_backend[fault-admission]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_el3_real_pools_partial_startup_leaves_no_backend[close-failure]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_el3_real_pools_partial_startup_leaves_no_backend[cancel-keyset]",  # noqa: E501
    ],
    "H-01-W4A": [
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_missing_or_malformed_bearer_is_401_and_admission_never_runs[None-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_missing_or_malformed_bearer_is_401_and_admission_never_runs[Bearer-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_missing_or_malformed_bearer_is_401_and_admission_never_runs[Bearer -POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_missing_or_malformed_bearer_is_401_and_admission_never_runs[Basic abc-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_missing_or_malformed_bearer_is_401_and_admission_never_runs[bearer x.y.z-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_missing_or_malformed_bearer_is_401_and_admission_never_runs[Bearer not-a-jwt-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_verifier_refusal_is_401_and_admission_never_runs[overrides0-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_verifier_refusal_is_401_and_admission_never_runs[overrides1-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_verifier_refusal_is_401_and_admission_never_runs[overrides2-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_verifier_refusal_is_401_and_admission_never_runs[overrides3-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_verifier_refusal_is_401_and_admission_never_runs[overrides4-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h01_unknown_kid_is_401",
        "tests/platform/test_l1w_w4a_app_postgres.py::test_h01_w4a_refused_admission_never_reaches_a_handler",  # noqa: E501
    ],
    "H-03-W4A": [
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-X-Tenant-Id-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-X-Tenant-Id-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-x-haloflow-tenant-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-x-haloflow-tenant-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-X-TENANT-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-X-TENANT-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-tenant-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[header-tenant-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-tenant-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-tenant-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-tenant_id-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-tenant_id-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-tenant_hint-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-tenant_hint-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-clinic-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-clinic-POST-/v1/session/logout]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-CLINIC_ID-GET-/healthz]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h03_tenant_input_is_400_before_authentication[query-CLINIC_ID-POST-/v1/session/logout]",  # noqa: E501
    ],
    "H-05": [
        "tests/platform/test_l1w_w4a_app_units.py::test_h05_docs_and_openapi_are_404[/docs]",
        "tests/platform/test_l1w_w4a_app_units.py::test_h05_docs_and_openapi_are_404[/redoc]",
        "tests/platform/test_l1w_w4a_app_units.py::test_h05_docs_and_openapi_are_404[/openapi.json]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_h05_docs_and_openapi_are_404[/docs/oauth2-redirect]",  # noqa: E501
    ],
    "H-06": [
        "tests/platform/test_l1w_w4a_app_units.py::test_h06_healthz_is_exact_when_ready",
    ],
    "W4A-SURFACE": [
        "tests/platform/test_l1w_w4a_app_units.py::test_w4a_route_surface_is_exactly_pinned",
        "tests/platform/test_l1w_w4a_app_units.py::test_bodies_literal_is_pinned",
    ],
    "KEYSET-503": [
        "tests/platform/test_l1w_w4a_app_units.py::test_keyset_unavailable_is_503_and_admission_never_runs",  # noqa: E501
    ],
    "ADMIT-MAP": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_refusal_set_is_pinned",
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_admitted_returns_binding_and_commits_once",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[UNBOUND-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[BINDING_MISMATCH-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[REVOKED-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[SID_MISMATCH-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[AUTH_TIME_FUTURE-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[ABSOLUTE-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[IDLE-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[INVALID_INPUT-401-UNAUTHENTICATED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_refusal_status[MFA_REQUIRED-401-REAUTHENTICATION_REQUIRED]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_unavailable_is_503_and_no_handler[ADMISSION_COMMIT_UNKNOWN]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_admit_map_http_unavailable_is_503_and_no_handler[ADMISSION_UNAVAILABLE]",  # noqa: E501
    ],
    "ADMIT-PERSIST": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_persist_confirmed_refusal_revocation_is_durable[IDLE]",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_persist_confirmed_refusal_revocation_is_durable[ABSOLUTE]",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_persist_confirmed_refusal_revocation_is_durable[BINDING_MISMATCH]",  # noqa: E501
    ],
    "ADMIT-COMMIT-FAILURE": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf1_known_rollback_on_admitted_is_unavailable_and_nothing_persists",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf2_known_rollback_on_idle_is_unavailable_and_revocation_not_persisted",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf3_committed_but_ack_lost_on_admitted_is_unavailable_session_persisted",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_acf_http_commit_failure_is_503_and_no_handler[before]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_acf_http_commit_failure_is_503_and_no_handler[after]",  # noqa: E501
    ],
    "ACF-1": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf1_known_rollback_on_admitted_is_unavailable_and_nothing_persists",  # noqa: E501
    ],
    "ACF-2": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf2_known_rollback_on_idle_is_unavailable_and_revocation_not_persisted",  # noqa: E501
    ],
    "ACF-3": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf3_committed_but_ack_lost_on_admitted_is_unavailable_session_persisted",  # noqa: E501
    ],
    "ACF-4": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted",  # noqa: E501
    ],
    "ACF-ACQ": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf_connection_acquisition_failure_is_unavailable",  # noqa: E501
    ],
    "ACF-HTTP": [
        "tests/platform/test_l1w_w4a_app_postgres.py::test_acf_http_commit_failure_is_503_and_no_handler[before]",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_acf_http_commit_failure_is_503_and_no_handler[after]",  # noqa: E501
    ],
    "LO-01": [
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_lo01_adapter_logout_revokes_only_that_session",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_lo01_adapter_logout_commit_unknown_is_unavailable",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_units.py::test_logout_route_admits_then_logs_out_and_returns_204",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_lo01_http_logout_is_per_device",
    ],
    "I2-HTTP": [
        "tests/platform/test_l1w_w4a_app_postgres.py::test_i2_http_requests_admitted_before_logout_complete_later_ones_refused",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_i2_http_logout_waits_behind_inflight_admission_then_applies",  # noqa: E501
    ],
    "I2-CONTROL": [
        "tests/platform/test_l1w_w4a_app_postgres.py::test_i2_control_retained_lock_blocks_logout_only_until_its_bound",  # noqa: E501
    ],
    "LIFE-1": [
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_open_failure_closes_the_pool_and_reraises_the_original[open_tenant_pool-TenantPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_open_failure_closes_the_pool_and_reraises_the_original[open_admission_pool-AdmissionPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_close_failure_never_replaces_the_open_failure[open_tenant_pool-TenantPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_close_failure_never_replaces_the_open_failure[open_admission_pool-AdmissionPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_cancellation_while_opening_closes_and_propagates[open_tenant_pool-TenantPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_cancellation_while_opening_closes_and_propagates[open_admission_pool-AdmissionPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_cancellation_during_the_close_wins[open_tenant_pool-TenantPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_cancellation_during_the_close_wins[open_admission_pool-AdmissionPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_successful_open_returns_the_pool_and_its_close[open_tenant_pool-TenantPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_successful_open_returns_the_pool_and_its_close[open_admission_pool-AdmissionPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_error_in_lifetime_is_reraised_not_cleanup_failed[None]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_error_in_lifetime_is_reraised_not_cleanup_failed[close_error1]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_cancellation_in_lifetime_is_reraised_despite_close_failure",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_cancellation_during_lifetime_close_wins",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_units.py::test_life1_lifetime_error_unpublishes_before_closing",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_postgres.py::test_life1_real_pool_fault_inside_acquisition_window_leaves_no_backend[error-open_tenant_pool-TenantPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_postgres.py::test_life1_real_pool_fault_inside_acquisition_window_leaves_no_backend[error-open_admission_pool-AdmissionPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_postgres.py::test_life1_real_pool_fault_inside_acquisition_window_leaves_no_backend[cancel-open_tenant_pool-TenantPool]",  # noqa: E501
        "tests/platform/test_l1w_w4a_life1_postgres.py::test_life1_real_pool_fault_inside_acquisition_window_leaves_no_backend[cancel-open_admission_pool-AdmissionPool]",  # noqa: E501
    ],
    # W4b (plan v1 + addendum 1; interface pins v1 + v1r1, Codex 5cca1a17).
    "R-01": [
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_self_standing_capability_literal",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_matrix_is_exactly_the_a52_literal",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_matrix_is_read_only",
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_requirements_are_exactly_the_literal",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_optional_is_exactly_the_literal",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_requirements_are_read_only",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_no_matrix_row_grants_self_standing",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[act-without-read]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[active-nothing]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[admin-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[admin-only]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[everything]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[inactive-with-everything]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[read]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[sup-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[sup-no-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[sup-only]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_capabilities_for_each_standing[unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_supervisor_without_act_cannot_act_or_resolve",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_admin_only_standing_is_route_specific",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_display_prefers_the_read_alternative_when_both_are_held",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_capabilities_with_everything_held[resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_write_routes_carry_read_and_held_supervise[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_write_routes_carry_read_and_held_supervise[admin-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_write_routes_carry_read_and_held_supervise[admin-only]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_write_routes_carry_read_and_held_supervise[everything]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_write_routes_carry_read_and_held_supervise[read]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_write_routes_carry_read_and_held_supervise[sup-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_write_routes_carry_read_and_held_supervise[sup-no-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_optional_never_adds_an_unheld_capability",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_route_denied_when_one_capability_is_missing[resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_unknown_route_is_refused",
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_standing_outside_the_closed_vocabulary_is_refused[bad0]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_standing_outside_the_closed_vocabulary_is_refused[bad1]",  # noqa: E501
        "tests/platform/test_l1w_w4b_capabilities_units.py::test_r01_standing_outside_the_closed_vocabulary_is_refused[bad2]",  # noqa: E501
    ],
    "R-01-U": [
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_two_step_context_and_exact_route_request[resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_route_context_is_bounded_by_the_standing[standing0-act-expected0]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_route_context_is_bounded_by_the_standing[standing1-resolve-expected1]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_route_context_is_bounded_by_the_standing[standing2-act-expected2]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_route_context_is_bounded_by_the_standing[standing3-get-expected3]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_route_context_is_bounded_by_the_standing[standing4-display-expected4]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_route_context_is_bounded_by_the_standing[standing5-display-expected5]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_each_request_gets_a_fresh_execution_and_correlation_id",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_client_correlation_header_is_ignored_and_not_echoed[x-correlation-id]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_client_correlation_header_is_ignored_and_not_echoed[x-request-id]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_client_correlation_header_is_ignored_and_not_echoed[traceparent]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[act-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[act-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[admin-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[admin-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[display-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[display-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[get-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[get-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[history-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[history-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[list-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[list-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[read_note-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[read_note-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[resolve-inactive]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_inactive_or_unknown_standing_is_403_on_every_route[resolve-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_r01u_logout_needs_no_standing",
    ],
    "R-01-HTTP": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_http_standing_by_route_table[ACT]",
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_http_standing_by_route_table[ADMIN-ACT]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_http_standing_by_route_table[ADMIN-ONLY]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_http_standing_by_route_table[INACTIVE]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_http_standing_by_route_table[READ]",
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_http_standing_by_route_table[SUP-ACT]",
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_http_standing_by_route_table[SUP-NO-ACT]",  # noqa: E501
    ],
    "R-01-OWN": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_own_claim_then_resolve_own_item",
    ],
    "R-01-SUP": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_sup_reassigns_then_resolves_another_actors_item",  # noqa: E501
    ],
    "R-01-SUP-NOACT": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_sup_without_act_cannot_act_or_resolve",
    ],
    "R-01-REVOKE-PRE-T3": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_r01_role_revoked_before_t3_refuses_the_supervisor_action",  # noqa: E501
    ],
    "R-Q1": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_rq1_reader_access_revoked_before_t3_returns_no_data[list]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_rq1_reader_access_revoked_before_t3_returns_no_data[get]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_rq1_reader_access_revoked_before_t3_returns_no_data[history]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_rq1_reader_access_revoked_before_t3_returns_no_data[read_note]",  # noqa: E501
    ],
    "H-01": [
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-hs256-jwk-secret-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-none-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-alg-not-allowlisted-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-bad-signature-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-crit-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jku-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-jwk-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-not-string-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kid-unknown-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-kty-alg-mismatch-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-jwt-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-typ-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5c-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v02-x5u-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-amr-not-list-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-aud-cross-environment-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-boolean-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-float-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-auth-time-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-client-id-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-at-skew-edge-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-boolean-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-past-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-exp-string-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-boolean-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iat-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-iss-prefix-only-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-jti-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-boolean-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-nbf-future-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sid-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-missing-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-sub-number-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-aud-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v03-wrong-iss-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-deep-header-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v05-too-large-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-bad-base64-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-empty-signature-segment-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-four-segments-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-json-array-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-header-not-json-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_the_w1_corpus_before_admission[v06-two-segments-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[None-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer -resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Basic abc-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_every_route_refuses_a_missing_or_malformed_bearer[Bearer a b-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h01_key_set_unavailable_is_503_on_every_route[resolve]",  # noqa: E501
    ],
    "H-02": [
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-item-id-equal]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-item-id-other]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-key-not-uuid]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-missing-version]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-type-bad]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-unknown-field]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-version-bool]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[act-version-string]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-idle-4]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-idle-61]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-mfa-yes]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-role-bad]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-set-with-staff-id]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-unknown-field]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-unknown-op]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[display-bad-uuid]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[display-empty]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[display-not-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[resolve-item-id-equal]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[resolve-note-not-str]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[resolve-outcome-bad]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unknown_key_is_never_reflected[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unknown_key_is_never_reflected[resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unknown_key_is_never_reflected[display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unknown_key_is_never_reflected[admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unknown_query_key_is_never_reflected",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_over_16_kib_is_422_before_parsing[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_over_16_kib_is_422_before_parsing[resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_over_16_kib_is_422_before_parsing[display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_over_16_kib_is_422_before_parsing[admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_at_16_kib_is_not_refused_for_size[act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_at_16_kib_is_not_refused_for_size[resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_at_16_kib_is_not_refused_for_size[display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_body_at_16_kib_is_not_refused_for_size[admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[truncated-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[truncated-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[truncated-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[truncated-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[array-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[array-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[array-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[array-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[null-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[null-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[null-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[null-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[not-utf8-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[not-utf8-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[not-utf8-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[not-utf8-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[empty-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[empty-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[empty-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_unparseable_or_non_object_body_is_422[empty-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_nine_query_parameters_is_422",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[-limit]",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=0-limit]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=101-limit]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=x-limit]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=10&states=done-states]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=10&states=open,open-states]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=10&assignee=nobody-assignee]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=10&after_item_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa-after_created_at]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=10&after_created_at=2026-11-04T21:00:00.000000Z-after_item_id]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=10&unknown=1-query]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_validation[limit=10&limit=11-query]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_list_query_builds_the_service_query",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[not-a-uuid-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[not-a-uuid-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[not-a-uuid-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[not-a-uuid-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_path_uuid_is_422_without_echo[aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_bad_note_id_is_422",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_item_id_comes_from_the_path_only",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_set_auth_setting_staff_id_is_the_principal",  # noqa: E501
    ],
    "H-03": [
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-X-Tenant-Id-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[header-tenant-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-tenant_id-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-get]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-history]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-list]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-read_note]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_input_on_every_route_is_400[query-Clinic-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_id-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_id-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_id-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_id-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_hint-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_hint-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_hint-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[tenant_hint-display]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[clinic_id-act]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[clinic_id-resolve]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[clinic_id-admin]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h03_tenant_in_the_body_is_an_unknown_field[clinic_id-display]",  # noqa: E501
    ],
    "H-04": [
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_case_list_is_complete",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[route-denied]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[read-not-authorized]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[action-not-authorized]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[admin-not-authorized]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[read-not-found]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[action-not-found]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[read-malformed]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[stale]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[resolved]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[note-required]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[key-conflict]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[admin-precondition]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[action-refusal-unlisted]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[command-malformed]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[action-key-unavailable]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[read-note-key-unavailable]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[read-result-invalid]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[admin-result-invalid]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[action-unavailable]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[lock-timeout]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[failure-unlisted]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[action-commit-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[resolve-commit-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[admin-commit-unknown]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[m01-capability-denied]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[m01-context-invalid]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[m01-tenant-denied]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[not-served]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[unmapped]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[unmapped-keyerror]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_resolve_failure_at_either_step_is_503[not-served-1]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_resolve_failure_at_either_step_is_503[not-served-2]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_resolve_failure_at_either_step_is_503[m01-tenant-denied-1]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_resolve_failure_at_either_step_is_503[m01-tenant-denied-2]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_resolve_failure_at_either_step_is_503[m01-context-invalid-1]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_resolve_failure_at_either_step_is_503[m01-context-invalid-2]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_self_standing_failure_never_reaches_the_service[read-result-invalid]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_self_standing_failure_never_reaches_the_service[read-not-authorized]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_self_standing_failure_never_reaches_the_service[m01-context-invalid]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_self_standing_failure_never_reaches_the_service[unmapped]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_success_bodies",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_admin_replayed_outcome_is_returned",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_bodies_literal_extends_w4a",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_no_other_route_or_method_exists[/v1/followup/items-DELETE]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_no_other_route_or_method_exists[/v1/followup/items/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa-POST]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_no_other_route_or_method_exists[/v1/staff/admin-GET]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_no_other_route_or_method_exists[/v1/support/bootstrap-POST]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_no_other_route_or_method_exists[/v1/admin-POST]",  # noqa: E501
    ],
    "H-07": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_h07_canaries_absent_and_request_lines_are_templates",  # noqa: E501
    ],
    "S-04-HTTP": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_s04_http_idle_expiry_is_401_and_persisted",
    ],
    "S-08-HTTP": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_s08_http_absolute_expiry_is_401_and_persisted",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_s08_http_no_row_past_twelve_hours_creates_nothing",  # noqa: E501
    ],
    "G-01": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_g01_list_and_get_return_exactly_the_seeded_items",  # noqa: E501
    ],
    "G-02": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_g02_claim_writes_history_with_the_staff_id",  # noqa: E501
    ],
    "G-03": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_g03_note_bearing_resolve_and_read_note_fail_closed",  # noqa: E501
        "tests/m05/test_l1w_w4b_note_harness_postgres.py::test_g03b_genuine_bundle_cannot_read_a_stored_note",  # noqa: E501
    ],
    "G-04": [
        "tests/m05/test_l1w_w4b_note_harness_postgres.py::test_g04a_authorized_reader_gets_the_note_text",  # noqa: E501
        "tests/m05/test_l1w_w4b_note_harness_postgres.py::test_g04b_unauthorized_reader_gets_no_text[ADMIN-ONLY]",  # noqa: E501
        "tests/m05/test_l1w_w4b_note_harness_postgres.py::test_g04b_unauthorized_reader_gets_no_text[INACTIVE]",  # noqa: E501
    ],
    "G-05": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_g05_commit_unknown_then_same_id_retry_replays",  # noqa: E501
    ],
    "AO-01": [
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_parser_reads_every_t029_operation",
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[add]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[deactivate]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[grant]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[grant_role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[reactivate]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[rename]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[revoke]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[revoke_role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_emitted_keys_equal_t029_v_expected[set_auth_setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao01_set_auth_setting_values_are_strings",
    ],
    "AO-02": [
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[activate-sql-name-not-offered]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[add-with-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[bind_identity-not-offered]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[deactivate-with-value]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant-with-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-bad-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-no-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-role-not-str]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-with-access]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-with-name]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-with-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-4]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-61]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-arabic-indic]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-empty]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-sign]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-space]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-three-digits]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[mfa-1]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[mfa-True]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[mfa-yes]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[revoke_role-with-value]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-bad-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-no-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-no-value]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-value-not-str]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-with-access]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-with-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[unbind_identity-not-offered]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_operations_literal",
    ],
    "AO-03": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_ao03_roles_and_settings_postconditions",
    ],
    "AO-04": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_ao04_same_id_replays_and_changed_field_conflicts",  # noqa: E501
    ],
    "AO-05": [
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao05_set_auth_setting_for_another_staff_id_refused_before_io",  # noqa: E501
    ],
    "AO-06": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_ao06_non_admin_is_403_and_nothing_changes[READ]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_ao06_non_admin_is_403_and_nothing_changes[ACT]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_ao06_non_admin_is_403_and_nothing_changes[SUP-ACT]",  # noqa: E501
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_ao06_non_admin_is_403_and_nothing_changes[INACTIVE]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h04_service_exception_maps_to_its_literal[admin-not-authorized]",  # noqa: E501
    ],
    "AO-07": [
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[activate-sql-name-not-offered]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[add-with-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[bind_identity-not-offered]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[deactivate-with-value]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant-with-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-bad-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-no-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-role-not-str]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-with-access]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-with-name]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[grant_role-with-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-4]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-61]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-arabic-indic]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-empty]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-sign]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-space]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[idle-three-digits]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[mfa-1]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[mfa-True]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[mfa-yes]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[revoke_role-with-value]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-bad-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-no-setting]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-no-value]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-value-not-str]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-with-access]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[set-with-role]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao02_ao07_malformed_command_refused_at_construction[unbind_identity-not-offered]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao07_idle_values_inside_the_sql_rule_are_accepted[5]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao07_idle_values_inside_the_sql_rule_are_accepted[05]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao07_idle_values_inside_the_sql_rule_are_accepted[15]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao07_idle_values_inside_the_sql_rule_are_accepted[60]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao07_idle_values_inside_the_sql_rule_are_accepted[59]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao07_mfa_values_accepted[true]",
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_ao07_mfa_values_accepted[false]",
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-idle-4]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-idle-61]",  # noqa: E501
        "tests/platform/test_l1w_w4b_boundary_units.py::test_h02_malformed_body_is_422_without_echo[admin-mfa-yes]",  # noqa: E501
    ],
    "AO-08": [
        "tests/m05/test_l1w_w4b_routes_postgres.py::test_ao08_setting_applies_to_the_next_request_of_an_existing_session",  # noqa: E501
    ],
    "AO-RESULT": [
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_admin_result_carries_the_outcome[applied]",
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_admin_result_carries_the_outcome[replayed]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_admin_result_for_role_operation_is_bound[other-staff]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_admin_result_for_role_operation_is_bound[other-operation]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_admin_result_for_role_operation_is_bound[bad-outcome]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_admin_result_for_role_operation_is_bound[extra-key]",  # noqa: E501
    ],
    "SELF-STANDING": [
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_binds_the_caller_standing",
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_unknown_is_a_standing_not_an_error",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_requires_its_capability[caps0]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_requires_its_capability[caps1]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_requires_its_capability[caps2]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[access-bad]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[access-dup]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[access-not-list]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[access-unsorted]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[bool-schema]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[extra-key]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[missing-key]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[no-rows]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[not-object]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[other-staff]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[roles-bad]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[roles-unsorted]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[schema-2]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[status-bad]",  # noqa: E501
        "tests/m01/test_l1w_w4b_admin_ops_units.py::test_self_standing_invalid_result_fails_closed[two-rows]",  # noqa: E501
    ],
}
