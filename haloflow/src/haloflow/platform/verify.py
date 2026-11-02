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

RED: ``TokenVerifier.verify`` raises NotImplementedError (W1 red tranche).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

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


class TokenVerifier:
    def __init__(
        self,
        config: VerifierConfig,
        keys: KeySource,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._keys = keys
        self._clock = clock

    async def verify(self, raw: str) -> VerifiedIdentity:
        raise NotImplementedError("L-1W W1 red: TokenVerifier.verify")
