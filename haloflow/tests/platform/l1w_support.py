"""Test-only issuer, JWKS and HTTP transport helpers for the L-1W W1 unit tier.

Tokens are signed here with the locked ``cryptography`` package only (no JWT library),
so the tests do not depend on the library under test to build their inputs. Every
key, issuer, subject and sid is synthetic.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ISSUER = "https://issuer.test.invalid/"
AUDIENCE = "https://api.haloflow.test.invalid"
JWKS_URL = "https://issuer.test.invalid/jwks"
NOW = 1_800_000_000  # fixed test clock (seconds)
SUBJECT = "synthetic-subject-0001"
SID = "synthetic-sid-0001"
CANARY = "CANARY-l1w-7f3e"  # appears in no error, message or log


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_json(obj: Any) -> str:
    return b64url(json.dumps(obj, separators=(",", ":")).encode())


def _int_b64(value: int) -> str:
    return b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


@dataclass
class SigningKey:
    kid: str
    alg: str  # "RS256" | "ES256"
    private: Any

    @classmethod
    def rsa(cls, kid: str = "rsa-1") -> SigningKey:
        return cls(kid, "RS256", rsa.generate_private_key(public_exponent=65537, key_size=2048))

    @classmethod
    def ec(cls, kid: str = "ec-1") -> SigningKey:
        return cls(kid, "ES256", ec.generate_private_key(ec.SECP256R1()))

    def jwk(self, **extra: Any) -> dict[str, Any]:
        pub = self.private.public_key()
        if self.alg == "RS256":
            nums = pub.public_numbers()
            base = {"kty": "RSA", "n": _int_b64(nums.n), "e": _int_b64(nums.e)}
        else:
            nums = pub.public_numbers()
            base = {"kty": "EC", "crv": "P-256",
                    "x": b64url(nums.x.to_bytes(32, "big")),
                    "y": b64url(nums.y.to_bytes(32, "big"))}
        return {"kid": self.kid, "use": "sig", "alg": self.alg, **base, **extra}

    def sign(self, signing_input: bytes) -> bytes:
        if self.alg == "RS256":
            return bytes(self.private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
        der = self.private.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def claims(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iss": ISSUER, "sub": SUBJECT, "aud": AUDIENCE, "exp": NOW + 300, "iat": NOW - 10,
        "jti": "synthetic-jti-0001", "client_id": "synthetic-client", "sid": SID,
        "auth_time": NOW - 60,
    }
    for key, value in overrides.items():
        if value is DROP:
            base.pop(key, None)
        else:
            base[key] = value
    return base


class _Drop:
    def __repr__(self) -> str:
        return "DROP"


DROP: Any = _Drop()


def token(key: SigningKey, payload: dict[str, Any] | None = None, *,
          header: dict[str, Any] | None = None, raw_header: str | None = None,
          raw_payload: str | None = None, signature: bytes | None = None) -> str:
    """Compact JWS. ``header`` overrides merge into the default header; DROP removes."""

    hdr: dict[str, Any] = {"alg": key.alg, "typ": "at+jwt", "kid": key.kid}
    for k, v in (header or {}).items():
        if v is DROP:
            hdr.pop(k, None)
        else:
            hdr[k] = v
    h = raw_header if raw_header is not None else b64url_json(hdr)
    p = raw_payload if raw_payload is not None else b64url_json(payload or claims())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = signature if signature is not None else key.sign(signing_input)
    return f"{h}.{p}.{b64url(sig)}"


@dataclass
class FakeKeys:
    """In-memory KeySource for verifier tests (the real client is tested separately)."""

    keys: dict[str, SigningKey] = field(default_factory=dict)
    unavailable: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def key_for(self, kid: str, alg: str) -> Any:
        from haloflow.platform.keys import KeyMismatch, KeySetUnavailable, UnknownKey

        self.calls.append((kid, alg))
        if self.unavailable:
            raise KeySetUnavailable("KEYSET_STALE")
        if kid not in self.keys:
            raise UnknownKey()
        key = self.keys[kid]
        if key.alg != alg:
            raise KeyMismatch()
        return key.private.public_key()


@dataclass
class Clock:
    now: float = float(NOW)

    def __call__(self) -> float:
        return self.now


@dataclass
class JwksServer:
    """httpx.MockTransport handler serving a mutable JWKS response."""

    body: Any = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "application/json"})
    raw: bytes | None = None
    fail: Exception | None = None
    redirect_to: str | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    on_request: Callable[[], Any] | None = None

    def set_keys(self, *keys: dict[str, Any]) -> None:
        self.body = {"keys": list(keys)}
        self.raw = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            result = self.on_request()
            if hasattr(result, "__await__"):
                await result
        if self.fail is not None:
            raise self.fail
        if self.redirect_to is not None:
            return httpx.Response(302, headers={"location": self.redirect_to})
        content = self.raw if self.raw is not None else json.dumps(self.body).encode()
        return httpx.Response(self.status, headers=self.headers, content=content)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
