"""Access-token verifier (L-1W W1; architecture v2 A3.1, library note v1r1).

Contract (pinned by tests/platform/test_l1w_verifier_units.py):

Pre-decode (before any signature work, on the raw text):
- more than ``max_token_bytes`` UTF-8 bytes: ``TOO_LARGE``;
- not exactly three non-empty base64url segments, or a header that is not a JSON
  object: ``MALFORMED``; duplicate JSON keys in the header: ``MALFORMED``;
- header JSON nesting deeper than ``header_max_depth``: ``HEADER_TOO_DEEP``;
- ``crit`` present: ``CRIT_PRESENT``; any of ``jku``, ``x5u``, ``jwk``, ``x5c``:
  ``EMBEDDED_KEY``; ``alg`` missing or not in ``algorithms`` (``none`` and every
  ``HS*`` always refused): ``ALG_NOT_ALLOWED``; ``kid`` missing or not a string:
  ``KID_MISSING``; ``typ`` other than ``at+jwt`` or ``application/at+jwt``
  (case-insensitive, per RFC 9068): ``WRONG_TYPE``;
- construction refuses (``ValueError``) a configuration whose ``algorithms`` include
  ``none`` or any ``HS*`` algorithm.

Key and signature (the key comes only from the injected key source):
- unknown kid after the permitted refresh: ``KID_UNKNOWN``; key/alg incoherent:
  ``KEY_MISMATCH``; bad signature: ``BAD_SIGNATURE``; key set unavailable:
  ``DependencyUnavailable`` (503, never 401).

Claims (RFC 9068 profile plus the session claims L-1W needs), exact JSON types:
- required: ``iss`` (str), ``sub`` (str), ``aud`` (str or list of str), ``exp`` (int),
  ``iat`` (int), ``jti`` (str), ``client_id`` (str), ``sid`` (str), ``auth_time`` (int);
  missing or wrong type: ``CLAIMS_INVALID``;
- ``iss`` must equal the configured issuer: ``WRONG_ISSUER``; ``aud`` must contain the
  configured audience: ``WRONG_AUDIENCE``; ``exp`` <= now - skew: ``EXPIRED``;
  ``nbf`` (optional int) > now + skew: ``NOT_YET_VALID``;
- deeply nested payload JSON never escapes as RecursionError: ``CLAIMS_INVALID`` or
  ``MALFORMED``, never an uncaught exception.

Result: ``VerifiedIdentity``; ``assured`` is true only if the token's ``amr`` (a list of
strings) intersects the configured ``assurance_amr`` set (owner RQ-B3c; enforced per
clinic by the admission step, not here).

Every call builds its own decode options; nothing is shared across calls (GHSA-gvp8
defence in depth). Refusal messages and exception text never contain the token or
any claim value.

Signature verification uses PyJWT's ``PyJWS.decode_complete`` with the single
algorithm from the (already allow-listed) header and the parsed key object from the
key source; PyJWT never sees a raw JWK, a URL or a symmetric algorithm. Claims are
parsed and checked here, strictly, after the signature.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, NoReturn, Protocol

import jwt

from haloflow.platform.keys import (
    SUPPORTED_ALGORITHMS,
    KeyMismatch,
    KeySetUnavailable,
    UnknownKey,
)

AUTH_REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {
        "TOO_LARGE",
        "MALFORMED",
        "HEADER_TOO_DEEP",
        "CRIT_PRESENT",
        "EMBEDDED_KEY",
        "ALG_NOT_ALLOWED",
        "KID_MISSING",
        "KID_UNKNOWN",
        "KEY_MISMATCH",
        "WRONG_TYPE",
        "BAD_SIGNATURE",
        "CLAIMS_INVALID",
        "WRONG_ISSUER",
        "WRONG_AUDIENCE",
        "EXPIRED",
        "NOT_YET_VALID",
    }
)

_SEGMENT: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_EMBEDDED_KEY_HEADERS: Final = frozenset({"jku", "x5u", "jwk", "x5c"})
_ACCESS_TOKEN_TYPES: Final = frozenset({"at+jwt", "application/at+jwt"})
_PAYLOAD_MAX_DEPTH: Final = 32
_REQUIRED_STR: Final = ("iss", "sub", "jti", "client_id", "sid")
_REQUIRED_INT: Final = ("exp", "iat", "auth_time")


class AuthRefused(Exception):
    """The credential is not accepted (401). ``code`` is from AUTH_REFUSAL_CODES."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DependencyUnavailable(Exception):
    """Verification cannot be performed (503): the key set is unavailable."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class KeySource(Protocol):
    async def key_for(self, kid: str, alg: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class VerifierConfig:
    issuer: str
    audience: str
    algorithms: frozenset[str]
    assurance_amr: frozenset[str] = frozenset()
    skew_s: int = 60
    max_token_bytes: int = 8192
    header_max_depth: int = 32


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    sid: str
    auth_time: datetime
    assured: bool
    client_id: str
    jti: str


def _refuse(code: str) -> NoReturn:
    raise AuthRefused(code) from None


def _depth_exceeds(text: str, limit: int) -> bool:
    """Non-recursive JSON nesting depth check (strings are skipped)."""

    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode_segment(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        _refuse("MALFORMED")


def _strict_object(
    raw: bytes, max_depth: int, depth_code: str, shape_code: str
) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _refuse(shape_code)
    if _depth_exceeds(text, max_depth):
        _refuse(depth_code)
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError):
        _refuse(shape_code)
    if type(value) is not dict:
        _refuse(shape_code)
    return value


def _is_int(value: object) -> bool:
    return type(value) is int  # JSON true/false are bool, never accepted as numbers


class TokenVerifier:
    def __init__(
        self,
        config: VerifierConfig,
        keys: KeySource,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not config.algorithms or not config.algorithms <= set(SUPPORTED_ALGORITHMS):
            raise ValueError("algorithms must be a non-empty set of asymmetric algorithms")
        self._config = config
        self._keys = keys
        self._clock = clock

    async def verify(self, raw: str) -> VerifiedIdentity:
        cfg = self._config
        if type(raw) is not str:
            _refuse("MALFORMED")
        if len(raw.encode("utf-8")) > cfg.max_token_bytes:
            _refuse("TOO_LARGE")
        segments = raw.split(".")
        if len(segments) != 3 or not all(_SEGMENT.fullmatch(part) for part in segments):
            _refuse("MALFORMED")
        header = _strict_object(
            _decode_segment(segments[0]), cfg.header_max_depth, "HEADER_TOO_DEEP", "MALFORMED"
        )

        if "crit" in header:
            _refuse("CRIT_PRESENT")
        if _EMBEDDED_KEY_HEADERS & set(header):
            _refuse("EMBEDDED_KEY")
        alg = header.get("alg")
        if type(alg) is not str or alg not in cfg.algorithms:
            _refuse("ALG_NOT_ALLOWED")
        kid = header.get("kid")
        if type(kid) is not str or not kid:
            _refuse("KID_MISSING")
        typ = header.get("typ")
        if type(typ) is not str or typ.lower() not in _ACCESS_TOKEN_TYPES:
            _refuse("WRONG_TYPE")

        try:
            key = await self._keys.key_for(kid, alg)
        except UnknownKey:
            _refuse("KID_UNKNOWN")
        except KeyMismatch:
            _refuse("KEY_MISMATCH")
        except KeySetUnavailable as error:
            raise DependencyUnavailable(error.reason_code) from None

        try:
            decoded = jwt.PyJWS().decode_complete(
                raw, key=key, algorithms=[alg], options={"verify_signature": True}
            )
        except jwt.InvalidSignatureError:
            _refuse("BAD_SIGNATURE")
        except jwt.PyJWTError:
            _refuse("MALFORMED")
        payload_bytes = decoded["payload"]
        if type(payload_bytes) is not bytes:
            _refuse("MALFORMED")
        claims = _strict_object(
            payload_bytes, _PAYLOAD_MAX_DEPTH, "CLAIMS_INVALID", "CLAIMS_INVALID"
        )
        return self._check_claims(claims)

    def _check_claims(self, claims: dict[str, Any]) -> VerifiedIdentity:
        cfg = self._config
        for name in _REQUIRED_STR:
            if type(claims.get(name)) is not str or not claims[name]:
                _refuse("CLAIMS_INVALID")
        for name in _REQUIRED_INT:
            if not _is_int(claims.get(name)):
                _refuse("CLAIMS_INVALID")
        aud = claims.get("aud")
        if type(aud) is str:
            audiences = [aud]
        elif type(aud) is list and aud and all(type(a) is str for a in aud):
            audiences = list(aud)
        else:
            _refuse("CLAIMS_INVALID")
        if "nbf" in claims and not _is_int(claims["nbf"]):
            _refuse("CLAIMS_INVALID")
        amr = claims.get("amr", [])
        if type(amr) is not list or not all(type(m) is str for m in amr):
            _refuse("CLAIMS_INVALID")

        if claims["iss"] != cfg.issuer:
            _refuse("WRONG_ISSUER")
        if cfg.audience not in audiences:
            _refuse("WRONG_AUDIENCE")
        now = self._clock()
        if claims["exp"] <= now - cfg.skew_s:
            _refuse("EXPIRED")
        if "nbf" in claims and claims["nbf"] > now + cfg.skew_s:
            _refuse("NOT_YET_VALID")

        return VerifiedIdentity(
            issuer=claims["iss"],
            subject=claims["sub"],
            sid=claims["sid"],
            auth_time=datetime.fromtimestamp(claims["auth_time"], tz=UTC),
            assured=bool(set(amr) & cfg.assurance_amr),
            client_id=claims["client_id"],
            jti=claims["jti"],
        )
