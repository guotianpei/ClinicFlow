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

Cancellation: a caller cancelled while waiting on a shared refresh leaves the refresh
running for the other waiters (``asyncio.shield``); ``aclose`` cancels any refresh in
flight and closes the HTTP client.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, rsa

KEYSET_REASON_CODES: Final[frozenset[str]] = frozenset(
    {"KEYSET_UNAVAILABLE", "KEYSET_STALE"}
)

# Asymmetric signature algorithms the client can serve, by key family.
SUPPORTED_ALGORITHMS: Final[dict[str, tuple[str, str | None]]] = {
    "RS256": ("RSA", None), "RS384": ("RSA", None), "RS512": ("RSA", None),
    "PS256": ("RSA", None), "PS384": ("RSA", None), "PS512": ("RSA", None),
    "ES256": ("EC", "P-256"), "ES384": ("EC", "P-384"), "ES512": ("EC", "P-521"),
}
_CURVES: Final[dict[str, tuple[ec.EllipticCurve, int]]] = {
    "P-256": (ec.SECP256R1(), 32), "P-384": (ec.SECP384R1(), 48), "P-521": (ec.SECP521R1(), 66),
}
_PRIVATE_PARAMS: Final = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
_B64URL: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_MIN_RSA_BITS: Final = 2048


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


@dataclass(frozen=True, slots=True)
class _Key:
    kty: str
    crv: str | None
    alg: str | None
    public_key: Any


class _Rejected(Exception):
    """Internal: the response or key set is not acceptable (never escapes)."""


def _b64url_bytes(value: object) -> bytes:
    if type(value) is not str or not _B64URL.fullmatch(value):
        raise _Rejected()
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise _Rejected() from None


class KeySetClient:
    """Fetches, validates and caches the issuer's key set."""

    def __init__(
        self,
        config: KeySetConfig,
        *,
        transport: httpx.AsyncBaseTransport,
        clock: Callable[[], float],
    ) -> None:
        if not config.allowed_algorithms or not config.allowed_algorithms <= set(
            SUPPORTED_ALGORITHMS
        ):
            raise ValueError("allowed_algorithms must be a non-empty set of asymmetric algorithms")
        self._config = config
        self._clock = clock
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self._keys: dict[str, _Key] | None = None
        self._installed_at: float | None = None
        self._last_attempt: float | None = None
        self._inflight: asyncio.Task[None] | None = None

    async def key_for(self, kid: str, alg: str) -> Any:
        now = self._clock()
        if self._keys is None:
            await self._refresh(now, force=True)
            if self._keys is None:
                raise KeySetUnavailable("KEYSET_UNAVAILABLE")
        else:
            assert self._installed_at is not None
            age = now - self._installed_at
            if age >= self._config.ttl_s:
                await self._refresh(now, force=False)
                assert self._installed_at is not None
                if self._clock() - self._installed_at >= self._config.max_age_s:
                    raise KeySetUnavailable("KEYSET_STALE")
        keys = self._keys
        assert keys is not None
        key = keys.get(kid)
        if key is None:
            await self._refresh(self._clock(), force=False)
            assert self._keys is not None
            key = self._keys.get(kid)
            if key is None:
                raise UnknownKey()
        return self._coherent(key, alg)

    def _coherent(self, key: _Key, alg: str) -> Any:
        family = SUPPORTED_ALGORITHMS.get(alg)
        if family is None or alg not in self._config.allowed_algorithms:
            raise KeyMismatch()
        kty, crv = family
        if key.kty != kty or (crv is not None and key.crv != crv):
            raise KeyMismatch()
        if key.alg is not None and key.alg != alg:
            raise KeyMismatch()
        return key.public_key

    async def _refresh(self, now: float, *, force: bool) -> None:
        """Single-flight, rate-limited refresh. Never raises for fetch problems."""

        if self._inflight is not None and not self._inflight.done():
            await asyncio.shield(self._inflight)
            return
        if (
            not force
            and self._last_attempt is not None
            and now - self._last_attempt < self._config.refresh_min_interval_s
        ):
            return
        self._last_attempt = now
        self._inflight = asyncio.ensure_future(self._fetch_and_install(now))
        await asyncio.shield(self._inflight)

    async def _fetch_and_install(self, started: float) -> None:
        try:
            body = await self._fetch()
            keys = self._parse(body)
        except _Rejected:
            return
        self._keys = keys
        self._installed_at = started

    async def _fetch(self) -> bytes:
        cfg = self._config
        try:
            async with asyncio.timeout(cfg.timeout_s):
                async with self._http.stream("GET", cfg.jwks_url) as response:
                    if response.status_code != 200:
                        raise _Rejected()
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer += chunk
                        if len(buffer) > cfg.max_bytes:
                            raise _Rejected()
                    return bytes(buffer)
        except (TimeoutError, httpx.HTTPError, OSError):
            raise _Rejected() from None

    def _parse(self, body: bytes) -> dict[str, _Key]:
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _Rejected() from None
        if type(document) is not dict or type(document.get("keys")) is not list:
            raise _Rejected()
        entries = document["keys"]
        if len(entries) > self._config.max_keys:
            raise _Rejected()
        keys: dict[str, _Key] = {}
        for entry in entries:
            kid, key = self._parse_key(entry)
            if kid in keys:
                raise _Rejected()  # duplicate kid: the whole set is refused
            keys[kid] = key
        return keys

    def _parse_key(self, entry: object) -> tuple[str, _Key]:
        if type(entry) is not dict:
            raise _Rejected()
        kid = entry.get("kid")
        if type(kid) is not str or not kid:
            raise _Rejected()
        if _PRIVATE_PARAMS & set(entry):
            raise _Rejected()
        use = entry.get("use")
        if use is not None and use != "sig":
            raise _Rejected()
        ops = entry.get("key_ops")
        if ops is not None and (type(ops) is not list or "verify" not in ops):
            raise _Rejected()
        alg = entry.get("alg")
        if alg is not None and (type(alg) is not str or alg not in self._config.allowed_algorithms):
            raise _Rejected()
        kty = entry.get("kty")
        try:
            if kty == "RSA":
                n = int.from_bytes(_b64url_bytes(entry.get("n")), "big")
                e = int.from_bytes(_b64url_bytes(entry.get("e")), "big")
                if n.bit_length() < _MIN_RSA_BITS:
                    raise _Rejected()
                public_key: Any = rsa.RSAPublicNumbers(e, n).public_key()
                crv = None
            elif kty == "EC":
                crv = entry.get("crv")
                if type(crv) is not str or crv not in _CURVES:
                    raise _Rejected()
                if not any(c == crv for a, (_, c) in SUPPORTED_ALGORITHMS.items()
                           if a in self._config.allowed_algorithms):
                    raise _Rejected()
                curve, size = _CURVES[crv]
                x, y = _b64url_bytes(entry.get("x")), _b64url_bytes(entry.get("y"))
                if len(x) != size or len(y) != size:
                    raise _Rejected()
                public_key = ec.EllipticCurvePublicNumbers(
                    int.from_bytes(x, "big"), int.from_bytes(y, "big"), curve
                ).public_key()
            else:
                raise _Rejected()
        except ValueError:
            raise _Rejected() from None
        if alg is not None and SUPPORTED_ALGORITHMS[alg][0] != kty:
            raise _Rejected()
        return kid, _Key(kty=kty, crv=crv, alg=alg, public_key=public_key)

    async def aclose(self) -> None:
        if self._inflight is not None and not self._inflight.done():
            self._inflight.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # shutting down
                await self._inflight
        await self._http.aclose()
