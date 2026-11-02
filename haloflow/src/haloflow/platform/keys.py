"""Key-set client for the pinned issuer (L-1W W1; architecture v2 A3.2, addendum 1 D1).

Contract (pinned by tests/platform/test_l1w_keyset_units.py):
- keys are fetched only from the configured ``jwks_url``, over the injected httpx
  transport; redirects are never followed; a response is refused if it exceeds
  ``max_bytes``, holds more than ``max_keys`` keys, is not a JSON object with a
  ``keys`` array, or the fetch takes longer than ``timeout_s``;
- ``timeout_s`` bounds the WHOLE fetch (connect, headers and body) by the client's own
  deadline, independent of any transport timeout; the body is read as a stream and
  abandoned as soon as more than ``max_bytes`` have arrived (it is never buffered whole
  first);
- the whole set is validated before it is installed: every key needs a string
  ``kid``, a ``kty`` coherent with an allowed algorithm, ``use`` absent or ``sig``,
  ``key_ops`` absent or containing ``verify``, ``alg`` absent or allowed, and only
  public parameters; a duplicate ``kid`` or any invalid key rejects the WHOLE set and
  keeps the previous one;
- only a validated, installed set resets the cache age (D1); a rejected response or a
  failed fetch never does;
- age < ``ttl_s``: served from cache; ``ttl_s`` <= age < ``max_age_s``: served and a
  refresh is attempted; age >= ``max_age_s`` (or no set ever installed and the fetch
  fails): ``KeySetUnavailable`` (mapped to 503 by the entry point);
- an unknown ``kid`` triggers at most one refresh per ``refresh_min_interval_s``,
  single-flight across concurrent callers; still unknown afterwards: ``UnknownKey``
  (mapped to 401);
- ``key_for`` returns a parsed public-key object for the requested algorithm; a key
  whose type or declared ``alg`` does not match the requested algorithm is
  ``KeyMismatch`` (401).

RED: every entry point raises NotImplementedError (W1 red tranche).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

KEYSET_REASON_CODES: Final[frozenset[str]] = frozenset(
    {"KEYSET_UNAVAILABLE", "KEYSET_STALE"}
)


class KeySetUnavailable(Exception):
    """No usable key set (never fetched successfully, or older than max_age)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class UnknownKey(Exception):
    """The kid is not in the installed set, even after the permitted refresh."""


class KeyMismatch(Exception):
    """The kid exists but its type or declared alg does not fit the requested alg."""


@dataclass(frozen=True, slots=True)
class KeySetConfig:
    jwks_url: str
    allowed_algorithms: frozenset[str] = field(
        default_factory=lambda: frozenset({"RS256", "ES256"})
    )
    ttl_s: float = 600.0
    max_age_s: float = 1800.0
    refresh_min_interval_s: float = 60.0
    timeout_s: float = 5.0
    max_bytes: int = 65536
    max_keys: int = 20


class KeySetClient:
    """Fetches, validates and caches the issuer's key set."""

    def __init__(
        self,
        config: KeySetConfig,
        *,
        transport: httpx.AsyncBaseTransport,
        clock: Callable[[], float],
    ) -> None:
        self._config = config
        self._transport = transport
        self._clock = clock

    async def key_for(self, kid: str, alg: str) -> Any:
        raise NotImplementedError("L-1W W1 red: KeySetClient.key_for")

    async def aclose(self) -> None:
        raise NotImplementedError("L-1W W1 red: KeySetClient.aclose")
