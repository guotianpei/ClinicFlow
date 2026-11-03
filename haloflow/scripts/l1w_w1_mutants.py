"""L-1W W1 security mutation controls (Codex e0323cac: real mutation evidence).

Each mutant removes or weakens one security check in ``haloflow.platform.verify`` or
``haloflow.platform.keys`` and is run against that module's unit tests by the shared
part-A checker (``scripts/m02_observation_mutants.py``): the control run must pass
completely; a mutant is KILLED only by a completed run whose every failure is in its
PREDECLARED kill set (no error, no skip, the control's node set); the target is
restored after every run and its SHA-256 re-checked. No judging logic is added here.

Kill sets were measured locally on the implementation head before CI (declared, then
confirmed by CI); a CI difference is reported, never edited away silently.

Known limitation (library note v1r1; Codex e0e045eb): a mutant that reuses one options
dict across decode calls (the GHSA-gvp8 class) is NOT included: PyJWT 2.15.1 copies
the options mapping before applying defaults, so the mutant is inert; V-04 keeps the
behavioural sequential-rejection oracle instead.

Usage (from haloflow/):
    python3 -I scripts/l1w_w1_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/l1w-w1-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VERIFY = Path("src/haloflow/platform/verify.py")
KEYS = Path("src/haloflow/platform/keys.py")
VERIFY_TESTS = "tests/platform/test_l1w_verifier_units.py"
KEYS_TESTS = "tests/platform/test_l1w_keyset_units.py"
TITLE_FROM = "m02-observation-mutants"


def _nodes(module: str, *names: str) -> frozenset[str]:
    return frozenset(f"{module}::{name}" for name in names)


# (id, description, target, old, new, predeclared kills)
SPECS: tuple[tuple[str, str, Path, str, str, frozenset[str]], ...] = (
    ("VM-ALG", "alg allowlist dropped", VERIFY,
     "        if type(alg) is not str or alg not in cfg.algorithms:\n",
     "        if type(alg) is not str:\n",
     _nodes(VERIFY_TESTS,
            "test_v02_header_and_key_refusals[alg-hs256-jwk-secret]",
            "test_v02_header_and_key_refusals[alg-none]",
            "test_v02_header_and_key_refusals[alg-not-allowlisted]",
            "test_v02_header_refusals_make_no_key_lookup",
     )),
    ("VM-CRIT", "crit refusal removed", VERIFY,
     "        if \"crit\" in header:\n            _refuse(\"CRIT_PRESENT\")\n",
     "",
     _nodes(VERIFY_TESTS,
            "test_v02_header_and_key_refusals[crit]",
            "test_v02_header_refusals_make_no_key_lookup",
     )),
    ("VM-EMBED", "embedded-key refusal removed", VERIFY,
     "        if _EMBEDDED_KEY_HEADERS & set(header):\n            _refuse(\"EMBEDDED_KEY\")\n",
     "",
     _nodes(VERIFY_TESTS,
            "test_v02_header_and_key_refusals[jku]",
            "test_v02_header_and_key_refusals[jwk]",
            "test_v02_header_and_key_refusals[x5c]",
            "test_v02_header_and_key_refusals[x5u]",
     )),
    ("VM-TYP", "access-token typ check removed", VERIFY,
     "        if type(typ) is not str or typ.lower() not in _ACCESS_TOKEN_TYPES:\n            _refuse(\"WRONG_TYPE\")\n",  # noqa: E501
     "",
     _nodes(VERIFY_TESTS,
            "test_v02_header_and_key_refusals[typ-jwt]",
            "test_v02_header_and_key_refusals[typ-missing]",
     )),
    ("VM-SIG", "signature not verified", VERIFY,
     "options={\"verify_signature\": True}",
     "options={\"verify_signature\": False}",
     _nodes(VERIFY_TESTS,
            "test_v02_header_and_key_refusals[bad-signature]",
            "test_v04_sequential_calls_keep_rejecting_invalid_tokens",
     )),
    ("VM-ISS", "issuer check removed", VERIFY,
     "        if claims[\"iss\"] != cfg.issuer:\n            _refuse(\"WRONG_ISSUER\")\n",
     "",
     _nodes(VERIFY_TESTS,
            "test_v03_claim_refusals[iss-prefix-only]",
            "test_v03_claim_refusals[wrong-iss]",
            "test_v04_sequential_calls_keep_rejecting_invalid_tokens",
     )),
    ("VM-AUD", "audience check removed", VERIFY,
     "        if cfg.audience not in audiences:\n            _refuse(\"WRONG_AUDIENCE\")\n",
     "",
     _nodes(VERIFY_TESTS,
            "test_v03_claim_refusals[aud-cross-environment]",
            "test_v03_claim_refusals[wrong-aud]",
            "test_v04_sequential_calls_keep_rejecting_invalid_tokens",
     )),
    ("VM-EXP", "expiry check removed", VERIFY,
     "        if claims[\"exp\"] <= now - cfg.skew_s:\n            _refuse(\"EXPIRED\")\n",
     "",
     _nodes(VERIFY_TESTS,
            "test_v03_claim_refusals[exp-at-skew-edge]",
            "test_v03_claim_refusals[exp-past]",
            "test_v04_sequential_calls_keep_rejecting_invalid_tokens",
     )),
    ("VM-NBF", "not-before check removed", VERIFY,
     "        if \"nbf\" in claims and claims[\"nbf\"] > now + cfg.skew_s:\n            _refuse(\"NOT_YET_VALID\")\n",  # noqa: E501
     "",
     _nodes(VERIFY_TESTS,
            "test_v03_claim_refusals[nbf-future]",
     )),
    ("VM-BOOL", "JSON booleans accepted as integers", VERIFY,
     "    return type(value) is int  # JSON true/false are bool, never accepted as numbers\n",
     "    return isinstance(value, int)\n",
     _nodes(VERIFY_TESTS,
            "test_v03_claim_refusals[auth-time-boolean]",
            "test_v03_claim_refusals[exp-boolean]",
            "test_v03_claim_refusals[iat-boolean]",
            "test_v03_claim_refusals[nbf-boolean]",
     )),
    ("VM-DEPTH", "header/payload depth bound disabled", VERIFY,
     "    if _depth_exceeds(text, max_depth):\n",
     "    if False:\n",
     _nodes(VERIFY_TESTS,
            "test_v05_deep_header_refused_before_signature",
            "test_v05_signed_deep_payload_is_a_bounded_refusal",
     )),
    ("VM-REQ", "required claims not enforced (sid)", VERIFY,
     "_REQUIRED_STR: Final = (\"iss\", \"sub\", \"jti\", \"client_id\", \"sid\")",
     "_REQUIRED_STR: Final = (\"iss\", \"sub\", \"jti\", \"client_id\")",
     _nodes(VERIFY_TESTS,
            "test_v03_claim_refusals[sid-missing]",
     )),
    ("VM-AMR", "assurance granted regardless of amr", VERIFY,
     "            assured=bool(set(amr) & cfg.assurance_amr),\n",
     "            assured=True,\n",
     _nodes(VERIFY_TESTS,
            "test_v01_assurance_from_pinned_amr[empty]",
            "test_v01_assurance_from_pinned_amr[pwd-only]",
            "test_v01_valid_token_yields_identity[ES256]",
            "test_v01_valid_token_yields_identity[RS256]",
     )),
    ("VM-AUTHTIME", "out-of-range auth_time not guarded", VERIFY,
     "        except (OverflowError, ValueError, OSError):\n            _refuse(\"CLAIMS_INVALID\")  # outside the representable range (Codex 039eff80)\n",  # noqa: E501
     "        except ZeroDivisionError:\n            _refuse(\"CLAIMS_INVALID\")\n",
     _nodes(VERIFY_TESTS,
            "test_v03_auth_time_outside_datetime_range_is_claims_invalid[before-year-1]",
            "test_v03_auth_time_outside_datetime_range_is_claims_invalid[googol]",
            "test_v03_auth_time_outside_datetime_range_is_claims_invalid[minus-googol]",
            "test_v03_auth_time_outside_datetime_range_is_claims_invalid[year-10000]",
     )),
    ("VM-INIT", "symmetric algorithms accepted at construction", VERIFY,
     "        if not config.algorithms or not config.algorithms <= set(SUPPORTED_ALGORITHMS):\n",
     "        if not config.algorithms:\n",
     _nodes(VERIFY_TESTS,
            "test_v01_config_refuses_symmetric_or_none_algorithms[HS256]",
            "test_v01_config_refuses_symmetric_or_none_algorithms[HS384]",
            "test_v01_config_refuses_symmetric_or_none_algorithms[HS512]",
            "test_v01_config_refuses_symmetric_or_none_algorithms[none]",
     )),
    ("KM-DUP", "duplicate kid accepted", KEYS,
     "            if kid in keys:\n                raise _Rejected()  # duplicate kid: the whole set is refused\n",  # noqa: E501
     "",
     _nodes(KEYS_TESTS,
            "test_k01_invalid_set_rejected_whole_on_first_fetch[duplicate-kid]",
            "test_k01_rejected_refresh_keeps_old_set_and_does_not_reset_age",
     )),
    ("KM-AGE", "rejected refresh resets age", KEYS,
     "        except _Rejected:\n            return\n",
     "        except _Rejected:\n            self._installed_at = started\n            return\n",
     _nodes(KEYS_TESTS,
            "test_k01_alg_curve_incoherent_refresh_keeps_old_set_and_age",
            "test_k01_rejected_refresh_keeps_old_set_and_does_not_reset_age",
            "test_k01_valid_refresh_resets_age",
            "test_k05_failing_refresh_between_ttl_and_max_age_still_serves",
            "test_k05_recovery_after_stale_installs_and_serves",
     )),
    ("KM-REDIRECT", "redirects followed", KEYS,
     "follow_redirects=False",
     "follow_redirects=True",
     _nodes(KEYS_TESTS,
            "test_k02_redirect_not_followed",
     )),
    ("KM-DEADLINE", "whole-fetch deadline removed", KEYS,
     "            async with asyncio.timeout(cfg.timeout_s):\n",
     "            async with asyncio.timeout(None):\n",
     _nodes(KEYS_TESTS,
            "test_k03_fetch_bounded_by_total_timeout",
     )),
    ("KM-BUFFER", "body buffered whole before the size check", KEYS,
     "                    async for chunk in response.aiter_bytes():\n                        buffer += chunk\n                        if len(buffer) > cfg.max_bytes:\n                            raise _Rejected()\n",  # noqa: E501
     "                    buffer += await response.aread()\n                    if len(buffer) > cfg.max_bytes:\n                        raise _Rejected()\n",  # noqa: E501
     _nodes(KEYS_TESTS,
            "test_k03_streamed_body_read_stops_at_max_bytes",
     )),
    ("KM-RATE", "refresh rate limit removed", KEYS,
     "            self._last_attempt is not None\n            and now - self._last_attempt < self._config.refresh_min_interval_s\n",  # noqa: E501
     "            False\n",
     _nodes(KEYS_TESTS,
            "test_k01_valid_refresh_drops_removed_keys",
            "test_k04_failed_initial_fetch_is_rate_limited",
            "test_k04_refresh_rate_limited_then_allowed_after_interval",
            "test_k04_unknown_kid_right_after_first_install_does_not_refetch",
     )),
    ("KM-INITRATE", "failed initial fetch retried on every request", KEYS,
     "        if (\n            self._last_attempt is not None\n",
     "        if (\n            self._keys is not None\n            and self._last_attempt is not None\n",  # noqa: E501
     _nodes(KEYS_TESTS,
            "test_k04_failed_initial_fetch_is_rate_limited",
     )),
    ("KM-CURVE", "declared alg checked by family only, not curve", KEYS,
     "            if family_kty != kty or (family_crv is not None and family_crv != crv):\n",
     "            if family_kty != kty:\n",
     _nodes(KEYS_TESTS,
            "test_k01_alg_curve_incoherence_rejects_whole_set_on_first_fetch",
            "test_k01_alg_curve_incoherent_refresh_keeps_old_set_and_age",
     )),
    ("KM-SINGLE", "single-flight removed", KEYS,
     "        if self._inflight is not None and not self._inflight.done():\n            await asyncio.shield(self._inflight)\n            return\n        if (",  # noqa: E501
     "        if (",
     _nodes(KEYS_TESTS,
            "test_k04_concurrent_first_fetch_is_single_flight",
     )),
    ("KM-USE", "use=enc keys accepted", KEYS,
     "        if use is not None and use != \"sig\":\n            raise _Rejected()\n",
     "",
     _nodes(KEYS_TESTS,
            "test_k01_invalid_set_rejected_whole_on_first_fetch[one-bad-among-good]",
            "test_k01_invalid_set_rejected_whole_on_first_fetch[use-enc]",
     )),
    ("KM-PRIV", "private key parameters accepted", KEYS,
     "        if _PRIVATE_PARAMS & set(entry):\n            raise _Rejected()\n",
     "",
     _nodes(KEYS_TESTS,
            "test_k01_invalid_set_rejected_whole_on_first_fetch[private-params]",
     )),
    ("KM-STALE", "max_age never enforced", KEYS,
     "                if self._clock() - self._installed_at >= self._config.max_age_s:\n                    raise KeySetUnavailable(\"KEYSET_STALE\")\n",  # noqa: E501
     "",
     _nodes(KEYS_TESTS,
            "test_k01_alg_curve_incoherent_refresh_keeps_old_set_and_age",
            "test_k01_rejected_refresh_keeps_old_set_and_does_not_reset_age",
            "test_k01_valid_refresh_resets_age",
            "test_k05_failing_refresh_between_ttl_and_max_age_still_serves",
            "test_k05_recovery_after_stale_installs_and_serves",
     )),
    ("KM-COHERE", "declared key alg not compared", KEYS,
     "        if key.alg is not None and key.alg != alg:\n            raise KeyMismatch()\n",
     "",
     _nodes(KEYS_TESTS,
            "test_k00_requested_alg_outside_allowlist_is_key_mismatch",
     )),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("verify", VERIFY, VERIFY_TESTS),
    ("keys", KEYS, KEYS_TESTS),
)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_mutants(checker: Any, target: Path) -> tuple[Any, ...]:
    return tuple(
        checker.Mutant(mid, desc, old, new, kills)
        for mid, desc, spec_target, old, new, kills in SPECS
        if spec_target == target
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = build_mutants(checker, target)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=l1w-w1-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
