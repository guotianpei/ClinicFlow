"""L-1W W1 key-set rows K-01..K-05 (unit tier, no network: httpx.MockTransport).

Package v1 section 5.1 + addendum 1 D1 (only a validated, installed set resets age).
The contract is the module docstring of ``haloflow.platform.keys``.

RED: ``KeySetClient.key_for`` raises NotImplementedError.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import l1w_support as ls
import pytest

from haloflow.platform.keys import (
    KeyMismatch,
    KeySetClient,
    KeySetConfig,
    KeySetUnavailable,
    UnknownKey,
)

RSA = ls.SigningKey.rsa("rsa-1")
EC = ls.SigningKey.ec("ec-1")
RSA2 = ls.SigningKey.rsa("rsa-2")


def _client(server: ls.JwksServer, clock: ls.Clock, **config: Any) -> KeySetClient:
    return KeySetClient(KeySetConfig(jwks_url=ls.JWKS_URL, **config),
                        transport=server.transport(), clock=clock)


def _served(*keys: dict[str, Any]) -> tuple[ls.JwksServer, ls.Clock]:
    server = ls.JwksServer()
    server.set_keys(*keys)
    return server, ls.Clock()


# --- basic contract ----------------------------------------------------------------


async def test_k00_fetches_once_and_returns_parsed_public_keys() -> None:
    server, clock = _served(RSA.jwk(), EC.jwk())
    client = _client(server, clock)
    rsa_key = await client.key_for("rsa-1", "RS256")
    ec_key = await client.key_for("ec-1", "ES256")
    assert rsa_key.public_numbers() == RSA.private.public_key().public_numbers()
    assert ec_key.public_numbers() == EC.private.public_key().public_numbers()
    assert len(server.requests) == 1
    (request,) = server.requests
    assert (request.method, str(request.url)) == ("GET", ls.JWKS_URL)


async def test_k00_kid_type_or_alg_mismatch_is_key_mismatch() -> None:
    server, clock = _served(RSA.jwk(), EC.jwk())
    client = _client(server, clock)
    with pytest.raises(KeyMismatch):
        await client.key_for("rsa-1", "ES256")
    with pytest.raises(KeyMismatch):
        await client.key_for("ec-1", "RS256")


async def test_k00_requested_alg_outside_allowlist_is_key_mismatch() -> None:
    server, clock = _served(RSA.jwk(alg="RS384"))
    client = _client(server, clock, allowed_algorithms=frozenset({"RS256", "RS384"}))
    with pytest.raises(KeyMismatch):
        await client.key_for("rsa-1", "RS256")


# --- K-01 whole-set validation; rejected sets do not reset age (D1) ----------------

INVALID_SETS: dict[str, Any] = {
    "duplicate-kid": lambda: {"keys": [RSA.jwk(), RSA.jwk()]},
    "kid-missing": lambda: {"keys": [{k: v for k, v in RSA.jwk().items() if k != "kid"}]},
    "kid-not-string": lambda: {"keys": [RSA.jwk(kid=7)]},
    "use-enc": lambda: {"keys": [RSA.jwk(use="enc")]},
    "key-ops-without-verify": lambda: {"keys": [RSA.jwk(key_ops=["encrypt"])]},
    "alg-not-allowed": lambda: {"keys": [RSA.jwk(alg="HS256")]},
    "kty-oct": lambda: {"keys": [{"kid": "o", "kty": "oct", "k": "c2VjcmV0"}]},
    "private-params": lambda: {"keys": [RSA.jwk(d="AQAB")]},
    "rsa-missing-n": lambda: {"keys": [{k: v for k, v in RSA.jwk().items() if k != "n"}]},
    "ec-wrong-curve": lambda: {"keys": [EC.jwk(crv="P-384")]},
    "one-bad-among-good": lambda: {"keys": [RSA.jwk(), EC.jwk(use="enc")]},
    "keys-not-array": lambda: {"keys": RSA.jwk()},
    "not-object": lambda: [RSA.jwk()],
}


@pytest.mark.parametrize("case", sorted(INVALID_SETS))
async def test_k01_invalid_set_rejected_whole_on_first_fetch(case: str) -> None:
    server = ls.JwksServer(body=INVALID_SETS[case]())
    client = _client(server, ls.Clock())
    with pytest.raises(KeySetUnavailable):
        await client.key_for("rsa-1", "RS256")


async def test_k01_rejected_refresh_keeps_old_set_and_does_not_reset_age() -> None:
    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-1", "RS256")  # installed at t0
    server.body = {"keys": [RSA.jwk(), RSA.jwk()]}  # duplicate kid from now on
    clock.now += 700  # past TTL (600): refresh attempted, rejected
    assert await client.key_for("rsa-1", "RS256") is not None  # old set still served
    clock.now += 1099  # t0 + 1799 < max_age (1800)
    assert await client.key_for("rsa-1", "RS256") is not None
    clock.now += 1  # t0 + 1800: age measured from the last INSTALLED set
    with pytest.raises(KeySetUnavailable) as caught:
        await client.key_for("rsa-1", "RS256")
    assert caught.value.reason_code == "KEYSET_STALE"


async def test_k01_valid_refresh_resets_age() -> None:
    """Codex 42cd14a5: after the intended install no fetch can succeed, so only the
    age reset can keep the set served past the OLD boundary."""

    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-1", "RS256")  # install 1 at t0
    clock.now += 700  # past TTL: refresh succeeds -> install 2 at t0 + 700
    await client.key_for("rsa-1", "RS256")
    assert len(server.requests) == 2
    server.fail = httpx.ConnectError("down")  # no successful fetch from here on
    clock.now += 1150  # t0 + 1850: past the OLD boundary (t0 + 1800)
    assert await client.key_for("rsa-1", "RS256") is not None
    clock.now += 649  # t0 + 2499: last second before the NEW boundary
    assert await client.key_for("rsa-1", "RS256") is not None
    clock.now += 1  # t0 + 2500 = install 2 + max_age
    with pytest.raises(KeySetUnavailable) as caught:
        await client.key_for("rsa-1", "RS256")
    assert caught.value.reason_code == "KEYSET_STALE"


async def test_k01_valid_refresh_drops_removed_keys() -> None:
    """The removed kid is unknown in the newly installed set itself: checked inside the
    refresh interval, so no further fetch can be what removes it."""

    server, clock = _served(RSA.jwk(), RSA2.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-2", "RS256")
    server.set_keys(RSA.jwk())  # rsa-2 rotated out
    clock.now += 700
    await client.key_for("rsa-1", "RS256")  # TTL refresh installs the rotated set
    fetches = len(server.requests)
    with pytest.raises(UnknownKey):
        await client.key_for("rsa-2", "RS256")  # same instant: rate-limited, no fetch
    assert len(server.requests) == fetches


# --- K-02 redirects are never followed ----------------------------------------------


async def test_k02_redirect_not_followed() -> None:
    server = ls.JwksServer(redirect_to="https://evil.invalid/jwks")
    client = _client(server, ls.Clock())
    with pytest.raises(KeySetUnavailable):
        await client.key_for("rsa-1", "RS256")
    assert [str(r.url) for r in server.requests] == [ls.JWKS_URL]


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_k02_non_200_status_rejected(status: int) -> None:
    server = ls.JwksServer(status=status)
    server.set_keys(RSA.jwk())
    with pytest.raises(KeySetUnavailable):
        await _client(server, ls.Clock()).key_for("rsa-1", "RS256")


# --- K-03 size, key count, timeout, body shape ---------------------------------------


async def test_k03_oversized_body_rejected() -> None:
    server = ls.JwksServer(raw=b'{"keys": []' + b" " * 70_000 + b"}")
    with pytest.raises(KeySetUnavailable):
        await _client(server, ls.Clock()).key_for("rsa-1", "RS256")


async def test_k03_more_than_max_keys_rejected() -> None:
    keys = [ls.SigningKey.ec(f"ec-{i}").jwk() for i in range(21)]
    server, clock = _served(*keys)
    with pytest.raises(KeySetUnavailable):
        await _client(server, clock).key_for("ec-0", "ES256")


async def test_k03_exactly_max_keys_accepted() -> None:
    keys = [ls.SigningKey.ec(f"ec-{i}").jwk() for i in range(20)]
    server, clock = _served(*keys)
    assert await _client(server, clock).key_for("ec-19", "ES256") is not None


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("t"), httpx.ConnectError("c"), httpx.ConnectTimeout("t")],
    ids=["read-timeout", "connect-error", "connect-timeout"],
)
async def test_k03_transport_failure_with_no_set_is_unavailable(failure: Exception) -> None:
    server = ls.JwksServer(fail=failure)
    with pytest.raises(KeySetUnavailable) as caught:
        await _client(server, ls.Clock()).key_for("rsa-1", "RS256")
    assert caught.value.reason_code == "KEYSET_UNAVAILABLE"


async def test_k03_fetch_bounded_by_total_timeout() -> None:
    """A hanging endpoint is abandoned at timeout_s (whole fetch: connect, headers and
    body), not merely when the transport raises its own timeout (Codex 42cd14a5)."""

    import time

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"keys": [RSA.jwk()]})

    server = ls.JwksServer(respond=hang)
    client = _client(server, ls.Clock(), timeout_s=0.2)
    started = time.monotonic()
    with pytest.raises(KeySetUnavailable) as caught:
        await client.key_for("rsa-1", "RS256")
    assert time.monotonic() - started < 1.5
    assert caught.value.reason_code == "KEYSET_UNAVAILABLE"


async def test_k03_fetch_within_timeout_succeeds() -> None:
    async def slowish(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"keys": [RSA.jwk()]})

    server = ls.JwksServer(respond=slowish)
    assert await _client(server, ls.Clock(), timeout_s=1.0).key_for("rsa-1", "RS256")


async def test_k03_streamed_body_read_stops_at_max_bytes() -> None:
    """The body is read as a stream and abandoned once max_bytes is exceeded; a client
    that buffers the whole response first would consume every chunk."""

    yielded: list[int] = []

    async def chunks() -> Any:
        yield b'{"keys": ['
        for i in range(20):
            yielded.append(i)
            yield b" " * 8192

    async def stream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=chunks())

    server = ls.JwksServer(respond=stream)
    with pytest.raises(KeySetUnavailable):
        await _client(server, ls.Clock()).key_for("rsa-1", "RS256")
    assert len(yielded) <= 9  # 65536 / 8192 = 8 chunks, +1 crossing the limit


async def test_k03_non_json_body_rejected() -> None:
    server = ls.JwksServer(raw=b"<html>nope</html>")
    with pytest.raises(KeySetUnavailable):
        await _client(server, ls.Clock()).key_for("rsa-1", "RS256")


# --- K-04 unknown kid: single-flight, rate-limited refresh ----------------------------


async def test_k04_concurrent_unknown_kids_share_one_refresh() -> None:
    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-1", "RS256")
    clock.now += 61
    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()

    server.on_request = slow
    tasks = [asyncio.create_task(client.key_for(f"unknown-{i}", "RS256")) for i in range(50)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, UnknownKey) for r in results)
    assert len(server.requests) == 2  # the initial fetch + exactly one refresh


async def test_k04_concurrent_first_fetch_is_single_flight() -> None:
    """Declared W1-implementation addition (mutation evidence: KM-SINGLE survived the
    unknown-kid row, where the rate limit masks it). With no set installed, concurrent
    first lookups share ONE fetch."""

    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()

    server.on_request = slow
    tasks = [asyncio.create_task(client.key_for("rsa-1", "RS256")) for _ in range(20)]
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r is not None for r in results)
    assert len(server.requests) == 1


async def test_k04_refresh_rate_limited_then_allowed_after_interval() -> None:
    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-1", "RS256")  # fetch 1
    clock.now += 61
    with pytest.raises(UnknownKey):
        await client.key_for("new-kid", "RS256")  # fetch 2 (refresh)
    clock.now += 30
    with pytest.raises(UnknownKey):
        await client.key_for("new-kid", "RS256")  # inside the interval: no fetch
    assert len(server.requests) == 2
    server.set_keys(RSA.jwk(), RSA2.jwk(kid="new-kid"))
    clock.now += 31  # 61 s after the last refresh
    assert await client.key_for("new-kid", "RS256") is not None  # fetch 3
    assert len(server.requests) == 3


async def test_k04_unknown_kid_right_after_first_install_does_not_refetch() -> None:
    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-1", "RS256")
    with pytest.raises(UnknownKey):
        await client.key_for("other", "RS256")
    assert len(server.requests) == 1


# --- K-05 stale window --------------------------------------------------------------


async def test_k05_failing_refresh_between_ttl_and_max_age_still_serves() -> None:
    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-1", "RS256")
    server.fail = httpx.ConnectError("down")
    clock.now += 601
    assert await client.key_for("rsa-1", "RS256") is not None
    clock.now += 1198  # 1799
    assert await client.key_for("rsa-1", "RS256") is not None
    clock.now += 1  # 1800
    with pytest.raises(KeySetUnavailable) as caught:
        await client.key_for("rsa-1", "RS256")
    assert caught.value.reason_code == "KEYSET_STALE"


async def test_k05_recovery_after_stale_installs_and_serves() -> None:
    server, clock = _served(RSA.jwk())
    client = _client(server, clock)
    await client.key_for("rsa-1", "RS256")
    server.fail = httpx.ConnectError("down")
    clock.now += 1900
    with pytest.raises(KeySetUnavailable):
        await client.key_for("rsa-1", "RS256")
    server.fail = None
    clock.now += 61
    assert await client.key_for("rsa-1", "RS256") is not None
