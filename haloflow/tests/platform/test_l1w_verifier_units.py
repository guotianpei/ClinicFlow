"""L-1W W1 verifier rows V-01..V-06 (unit tier, no database).

Package v1 section 5.1 + v1r1 section 5 (V-05) + library note v1r1 test pins
(Codex 6e73e350, clarification e0e045eb). The verifier contract is the module
docstring of ``haloflow.platform.verify``.

RED: ``TokenVerifier.verify`` raises NotImplementedError, so every row that calls it
fails; the construction rows fail because the stub accepts any configuration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import l1w_support as ls
import pytest

from haloflow.platform.verify import (
    AUTH_REFUSAL_CODES,
    AuthRefused,
    DependencyUnavailable,
    TokenVerifier,
    VerifiedIdentity,
    VerifierConfig,
)

RSA = ls.SigningKey.rsa("rsa-1")
EC = ls.SigningKey.ec("ec-1")
OTHER_RSA = ls.SigningKey.rsa("rsa-1")  # same kid, different key material


def _config(**overrides: Any) -> VerifierConfig:
    base: dict[str, Any] = {
        "issuer": ls.ISSUER, "audience": ls.AUDIENCE,
        "algorithms": frozenset({"RS256", "ES256"}),
        "assurance_amr": frozenset({"mfa", "otp"}),
    }
    base.update(overrides)
    return VerifierConfig(**base)


def _verifier(keys: ls.FakeKeys | None = None, **config: Any) -> TokenVerifier:
    return TokenVerifier(
        _config(**config), keys or ls.FakeKeys({RSA.kid: RSA, EC.kid: EC}), clock=ls.Clock()
    )


async def _refused(verifier: TokenVerifier, raw: str, code: str) -> None:
    assert code in AUTH_REFUSAL_CODES
    with pytest.raises(AuthRefused) as caught:
        await verifier.verify(raw)
    assert caught.value.code == code
    text = f"{caught.value!r} {caught.value!s} {caught.value.args!r}"
    # declared test fix (W1 implementation): "" is a substring of every string, so the
    # raw-token absence check applies only to a non-empty token
    assert ls.CANARY not in text and (not raw or raw not in text)


# --- V-01 valid tokens ----------------------------------------------------------


@pytest.mark.parametrize("key", [RSA, EC], ids=["RS256", "ES256"])
async def test_v01_valid_token_yields_identity(key: ls.SigningKey) -> None:
    identity = await _verifier().verify(ls.token(key))
    assert identity == VerifiedIdentity(
        issuer=ls.ISSUER, subject=ls.SUBJECT, sid=ls.SID,
        auth_time=datetime.fromtimestamp(ls.NOW - 60, tz=UTC), assured=False,
        client_id="synthetic-client", jti="synthetic-jti-0001",
    )


@pytest.mark.parametrize(
    ("amr", "assured"),
    [(["pwd", "mfa"], True), (["otp"], True), (["pwd"], False), ([], False)],
    ids=["pwd+mfa", "otp", "pwd-only", "empty"],
)
async def test_v01_assurance_from_pinned_amr(amr: list[str], assured: bool) -> None:
    identity = await _verifier().verify(ls.token(RSA, ls.claims(amr=amr)))
    assert identity.assured is assured


@pytest.mark.parametrize("typ", ["at+jwt", "AT+JWT", "application/at+jwt"])
async def test_v01_typ_variants_accepted(typ: str) -> None:
    identity = await _verifier().verify(ls.token(RSA, header={"typ": typ}))
    assert identity.subject == ls.SUBJECT


async def test_v01_audience_list_containing_configured_audience() -> None:
    raw = ls.token(RSA, ls.claims(aud=["https://other.invalid", ls.AUDIENCE]))
    assert (await _verifier().verify(raw)).subject == ls.SUBJECT


@pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512", "none"])
def test_v01_config_refuses_symmetric_or_none_algorithms(alg: str) -> None:
    with pytest.raises(ValueError):
        TokenVerifier(_config(algorithms=frozenset({"RS256", alg})), ls.FakeKeys(),
                      clock=ls.Clock())


# --- V-02 header / key refusal corpus ---------------------------------------------


def _forged_hs256() -> str:
    import hashlib
    import hmac

    header = ls.b64url_json({"alg": "HS256", "typ": "at+jwt", "kid": RSA.kid})
    payload = ls.b64url_json(ls.claims())
    secret = json.dumps(RSA.jwk()).encode()  # CVE-2026-48526 shape: JWK text as secret
    sig = hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{ls.b64url(sig)}"


V02_CASES: dict[str, tuple[Any, str]] = {
    # non-empty signature segment: an empty one is MALFORMED first (Codex 42cd14a5)
    "alg-none": (lambda: ls.token(RSA, header={"alg": "none"}, signature=b"x"), "ALG_NOT_ALLOWED"),
    "alg-hs256-jwk-secret": (_forged_hs256, "ALG_NOT_ALLOWED"),
    "alg-not-allowlisted": (lambda: ls.token(RSA, header={"alg": "RS384"}), "ALG_NOT_ALLOWED"),
    "alg-missing": (lambda: ls.token(RSA, header={"alg": ls.DROP}), "ALG_NOT_ALLOWED"),
    "crit": (lambda: ls.token(RSA, header={"crit": ["exp"]}), "CRIT_PRESENT"),
    "jku": (lambda: ls.token(RSA, header={"jku": "https://evil.invalid/jwks"}), "EMBEDDED_KEY"),
    "x5u": (lambda: ls.token(RSA, header={"x5u": "https://evil.invalid/x5"}), "EMBEDDED_KEY"),
    "jwk": (lambda: ls.token(RSA, header={"jwk": RSA.jwk()}), "EMBEDDED_KEY"),
    "x5c": (lambda: ls.token(RSA, header={"x5c": ["MIIB"]}), "EMBEDDED_KEY"),
    "kid-missing": (lambda: ls.token(RSA, header={"kid": ls.DROP}), "KID_MISSING"),
    "kid-not-string": (lambda: ls.token(RSA, header={"kid": 7}), "KID_MISSING"),
    "kid-unknown": (lambda: ls.token(RSA, header={"kid": "nope"}), "KID_UNKNOWN"),
    "kty-alg-mismatch": (lambda: ls.token(RSA, header={"alg": "ES256"}), "KEY_MISMATCH"),
    "typ-missing": (lambda: ls.token(RSA, header={"typ": ls.DROP}), "WRONG_TYPE"),
    "typ-jwt": (lambda: ls.token(RSA, header={"typ": "JWT"}), "WRONG_TYPE"),
    "bad-signature": (lambda: ls.token(OTHER_RSA), "BAD_SIGNATURE"),
}


@pytest.mark.parametrize("case", sorted(V02_CASES))
async def test_v02_header_and_key_refusals(case: str) -> None:
    build, code = V02_CASES[case]
    await _refused(_verifier(), build(), code)


async def test_v02_key_set_unavailable_is_dependency_not_auth() -> None:
    verifier = _verifier(ls.FakeKeys({RSA.kid: RSA}, unavailable=True))
    with pytest.raises(DependencyUnavailable):
        await verifier.verify(ls.token(RSA))


async def test_v02_header_refusals_make_no_key_lookup() -> None:
    keys = ls.FakeKeys({RSA.kid: RSA})
    verifier = _verifier(keys)
    for build in (lambda: ls.token(RSA, header={"crit": ["x"]}),
                  lambda: ls.token(RSA, header={"alg": "none"}, signature=b"x")):
        with pytest.raises(AuthRefused):
            await verifier.verify(build())
    assert keys.calls == []


# --- V-03 claims -------------------------------------------------------------------

V03_CASES: dict[str, tuple[dict[str, Any], str]] = {
    "exp-past": ({"exp": ls.NOW - 61}, "EXPIRED"),
    "exp-at-skew-edge": ({"exp": ls.NOW - 60}, "EXPIRED"),
    "nbf-future": ({"nbf": ls.NOW + 61}, "NOT_YET_VALID"),
    "wrong-iss": ({"iss": "https://other-issuer.invalid/"}, "WRONG_ISSUER"),
    "iss-prefix-only": ({"iss": ls.ISSUER + "x"}, "WRONG_ISSUER"),
    "wrong-aud": ({"aud": "https://other-api.invalid"}, "WRONG_AUDIENCE"),
    "aud-cross-environment": ({"aud": ls.AUDIENCE.replace("test", "prod")}, "WRONG_AUDIENCE"),
    "iat-missing": ({"iat": ls.DROP}, "CLAIMS_INVALID"),
    "sub-missing": ({"sub": ls.DROP}, "CLAIMS_INVALID"),
    "client-id-missing": ({"client_id": ls.DROP}, "CLAIMS_INVALID"),
    "jti-missing": ({"jti": ls.DROP}, "CLAIMS_INVALID"),
    "sid-missing": ({"sid": ls.DROP}, "CLAIMS_INVALID"),
    "auth-time-missing": ({"auth_time": ls.DROP}, "CLAIMS_INVALID"),
    "exp-missing": ({"exp": ls.DROP}, "CLAIMS_INVALID"),
    "exp-string": ({"exp": str(ls.NOW + 300)}, "CLAIMS_INVALID"),
    # JSON true/false are not integers even though Python bool subclasses int
    "exp-boolean": ({"exp": True}, "CLAIMS_INVALID"),
    "iat-boolean": ({"iat": True}, "CLAIMS_INVALID"),
    "nbf-boolean": ({"nbf": False}, "CLAIMS_INVALID"),
    "auth-time-boolean": ({"auth_time": True}, "CLAIMS_INVALID"),
    "auth-time-float": ({"auth_time": ls.NOW - 60.5}, "CLAIMS_INVALID"),
    "sub-number": ({"sub": 123}, "CLAIMS_INVALID"),
    "amr-not-list": ({"amr": "mfa"}, "CLAIMS_INVALID"),
}


@pytest.mark.parametrize("case", sorted(V03_CASES))
async def test_v03_claim_refusals(case: str) -> None:
    overrides, code = V03_CASES[case]
    raw = ls.token(RSA, ls.claims(x_canary=ls.CANARY, **overrides))
    await _refused(_verifier(), raw, code)


@pytest.mark.parametrize(
    "auth_time",
    [253_402_300_800, 10**100, -(10**100), -62_135_596_801],
    ids=["year-10000", "googol", "minus-googol", "before-year-1"],
)
async def test_v03_auth_time_outside_datetime_range_is_claims_invalid(auth_time: int) -> None:
    """Declared W1-implementation addition (Codex 039eff80): an integer auth_time that
    cannot be represented is a fixed, redacted refusal, never an uncaught error."""

    raw = ls.token(RSA, ls.claims(auth_time=auth_time, x_canary=ls.CANARY))
    await _refused(_verifier(), raw, "CLAIMS_INVALID")


async def test_v03_skew_tolerated_inside_window() -> None:
    raw = ls.token(RSA, ls.claims(exp=ls.NOW - 59, nbf=ls.NOW + 59))
    assert (await _verifier().verify(raw)).subject == ls.SUBJECT


async def test_v03_id_token_shape_refused() -> None:
    """An OIDC ID token (typ JWT, nonce, aud = client id, no client_id) is refused
    even though its signature and issuer are valid (positive access-token contract)."""

    raw = ls.token(RSA, ls.claims(aud="synthetic-client", nonce="n-1", client_id=ls.DROP),
                   header={"typ": "JWT"})
    with pytest.raises(AuthRefused):
        await _verifier().verify(raw)


# --- V-04 sequential-call oracle (library note v1r1; Codex e0e045eb) ----------------


async def test_v04_sequential_calls_keep_rejecting_invalid_tokens() -> None:
    verifier = _verifier()
    sequence: list[tuple[str, str | None]] = [
        (ls.token(RSA), None),
        (ls.token(OTHER_RSA), "BAD_SIGNATURE"),
        (ls.token(RSA, ls.claims(exp=ls.NOW - 120)), "EXPIRED"),
        (ls.token(RSA, ls.claims(aud="https://other-api.invalid")), "WRONG_AUDIENCE"),
        (ls.token(RSA, ls.claims(iss="https://other-issuer.invalid/")), "WRONG_ISSUER"),
        (ls.token(EC), None),
        (ls.token(RSA, ls.claims(exp=ls.NOW - 120)), "EXPIRED"),
    ]
    for raw, code in sequence:
        if code is None:
            assert (await verifier.verify(raw)).subject == ls.SUBJECT
        else:
            await _refused(verifier, raw, code)


# --- V-05 size and nesting (v1r1 section 5) -----------------------------------------


async def test_v05_size_limit() -> None:
    padding_claim = "x" * 9000
    raw = ls.token(RSA, ls.claims(pad=padding_claim))
    assert len(raw.encode()) > 8192
    await _refused(_verifier(), raw, "TOO_LARGE")


def _nested(depth: int) -> Any:
    value: Any = []
    for _ in range(depth - 1):
        value = [value]
    return value


async def test_v05_deep_header_refused_before_signature() -> None:
    keys = ls.FakeKeys({RSA.kid: RSA})
    raw = ls.token(RSA, header={"x": _nested(2000)})
    assert len(raw.encode()) <= 8192
    await _refused(_verifier(keys), raw, "HEADER_TOO_DEEP")
    assert keys.calls == []


async def test_v05_signed_deep_payload_is_a_bounded_refusal() -> None:
    raw = ls.token(RSA, ls.claims(n=_nested(2000)))
    assert len(raw.encode()) <= 8192
    with pytest.raises(AuthRefused) as caught:
        await _verifier().verify(raw)
    assert caught.value.code in {"CLAIMS_INVALID", "MALFORMED"}


async def test_v05_header_at_depth_bound_is_not_refused_for_depth() -> None:
    # depth 32 including the header object itself is allowed (x adds 31 levels)
    raw = ls.token(RSA, header={"x": _nested(31)})
    assert (await _verifier().verify(raw)).subject == ls.SUBJECT


# --- V-06 malformed -------------------------------------------------------------------

V06_CASES: dict[str, str] = {
    "two-segments": "abc.def",
    "four-segments": "a.b.c.d",
    "empty-signature-segment": f"{ls.b64url_json({'alg': 'RS256'})}.{ls.b64url_json({})}.",
    "bad-base64": "@@@.###.$$$",
    "header-not-json": f"{ls.b64url(b'not json')}.{ls.b64url_json({})}.c2ln",
    "header-json-array": f"{ls.b64url_json([1, 2])}.{ls.b64url_json({})}.c2ln",
    "empty": "",
}


@pytest.mark.parametrize("case", sorted(V06_CASES))
async def test_v06_malformed(case: str) -> None:
    await _refused(_verifier(), V06_CASES[case], "MALFORMED")


async def test_v06_duplicate_header_keys_refused() -> None:
    raw_header = ls.b64url(
        b'{"alg":"RS256","typ":"at+jwt","kid":"rsa-1","alg":"RS256"}'
    )
    raw = ls.token(RSA, raw_header=raw_header)
    await _refused(_verifier(), raw, "MALFORMED")
