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
}
