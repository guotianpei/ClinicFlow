"""L-1S package S-G1 (part a) — synthetic inbound-SMS ingress, unit rows (no database).

Governing: S-G1 package v1 + v1r1 C3/C4 (Work Session 2026-11-07), Codex ALIGN for
tests 20261107T171844Z; placement per Codex ALIGN 20261107T174755Z (option A: m05).
Every S-G import is deferred into the row body (class-B rule).
Synthetic only: no provider, no signature claim, not composed into `platform/app.py`.

Pinned API (`haloflow.m05.synthetic_ingress`):
- `MAX_BODY_BYTES = 16384`, `MAX_AUTH_BYTES = 512`, `MIN_SECRET_BYTES = 32`,
  `MAX_SECRET_BYTES = 505` (`Bearer ` + token must fit 512; F-SG1 secondary),
  `ROUTE = "/v1/synthetic/inbound-sms"`.
- `IngressConfig.build(secrets: Mapping[str, str], served_tenants: tuple[str, ...])`
  -> IngressConfig; refusals `IngressConfigInvalid(code)`: SECRETS_EMPTY,
  SECRET_DUPLICATE, SECRET_TOO_SHORT, SECRET_TOO_LONG, SECRET_INVALID_CHARS, TENANT_NOT_SERVED,
  SECRETS_INVALID (non-mapping / non-str). `repr` shows only the clinic count.
- `authenticate(config, headers) -> str | None`: exactly one `authorization` header,
  <= 512 bytes, `Bearer <token>`; constant-time comparison against EVERY secret (no
  early exit); returns the bound tenant id or None.
- `create_ingress_router(config, *, context_for, receive) -> APIRouter` with
  `POST ROUTE`: auth (401 INGRESS_UNAUTHENTICATED) -> Content-Type must be
  `application/json` (optionally `; charset=utf-8`) (415 INBOUND_UNSUPPORTED_MEDIA) ->
  body streamed with a hard cap regardless of Content-Length; each chunk is checked
  against the remaining capacity before it is copied (F-SG1) (413 INBOUND_TOO_LARGE)
  -> strict UTF-8 + strict JSON object with exactly {channel, provider_message_id,
  phone, text}, all str, no duplicate keys, no NaN/Infinity, and field values as
  `InboundCommand` requires (channel "sms", id 1-128 chars, E.164 phone)
  (400 INBOUND_MALFORMED)
  -> `context = await context_for(tenant_id)` -> `await receive(context,
  InboundMessage(...))`. 202 body exactly {outcome, decision, inbound_id}. Error
  bodies exactly {"code": <fixed code>}: ReplyRefused -> 400 INBOUND_MALFORMED,
  CommandMalformed -> 400 INBOUND_MALFORMED, IntakeFailed INBOUND_CONFLICT -> 409
  INBOUND_CONFLICT, ActivationRefused -> 409 CLINIC_NOT_ACTIVATED, IntakeCommitUnknown
  -> 503 INBOUND_COMMIT_UNKNOWN, anything else -> 500 INTERNAL. Logs carry fixed codes
  only (no secret, phone or text).
"""

from __future__ import annotations

import json
import logging
import tracemalloc
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

TENANT_A = "clinic-sg-a"
TENANT_B = "clinic-sg-b"
SECRET_A = "a" * 31 + "Z" + "-synthetic-ingress-A"
SECRET_B = "b" * 31 + "Y" + "-synthetic-ingress-B"
MARKER = "ZQX-sg-raw-marker-4419"
PHONE = "+15555550123"


def _mod() -> Any:
    import haloflow.m05.synthetic_ingress as module

    return module


def _config(**overrides: Any) -> Any:
    secrets = overrides.pop("secrets", {TENANT_A: SECRET_A, TENANT_B: SECRET_B})
    served = overrides.pop("served", (TENANT_A, TENANT_B))
    return _mod().IngressConfig.build(secrets, served)


# --- SG-U02 config ----------------------------------------------------------------------------

BAD_CONFIGS: dict[str, tuple[Any, tuple[str, ...], str]] = {
    "empty": ({}, (TENANT_A,), "SECRETS_EMPTY"),
    "duplicate": ({TENANT_A: SECRET_A, TENANT_B: SECRET_A}, (TENANT_A, TENANT_B),
                  "SECRET_DUPLICATE"),
    "short": ({TENANT_A: "x" * 31}, (TENANT_A,), "SECRET_TOO_SHORT"),
    "long": ({TENANT_A: "x" * 506}, (TENANT_A,), "SECRET_TOO_LONG"),
    "space": ({TENANT_A: "x" * 20 + " " + "y" * 20}, (TENANT_A,), "SECRET_INVALID_CHARS"),
    "control": ({TENANT_A: "x" * 40 + "\x01"}, (TENANT_A,), "SECRET_INVALID_CHARS"),
    "non-ascii": ({TENANT_A: "x" * 40 + "é"}, (TENANT_A,), "SECRET_INVALID_CHARS"),
    "unserved": ({TENANT_A: SECRET_A, TENANT_B: SECRET_B}, (TENANT_A,), "TENANT_NOT_SERVED"),
    "non-mapping": ([(TENANT_A, SECRET_A)], (TENANT_A,), "SECRETS_INVALID"),
    "non-str": ({TENANT_A: 12345}, (TENANT_A,), "SECRETS_INVALID"),
}


@pytest.mark.parametrize("case", sorted(BAD_CONFIGS))
def test_sg_u02_config_refusals(case: str) -> None:
    module = _mod()
    secrets, served, code = BAD_CONFIGS[case]
    with pytest.raises(module.IngressConfigInvalid) as caught:
        module.IngressConfig.build(secrets, served)
    assert caught.value.code == code
    assert SECRET_A not in str(caught.value) and SECRET_A not in repr(caught.value.args)


async def test_sg_u02c_longest_usable_secret() -> None:
    """F-SG1 secondary: `Bearer ` (7 bytes) + token must fit MAX_AUTH_BYTES (512), so
    505 bytes is the longest configurable secret; 506 is refused (SG-U02 "long")."""

    module = _mod()
    assert module.MAX_SECRET_BYTES == module.MAX_AUTH_BYTES - len(b"Bearer ") == 505
    secret = "k" * 505
    config = module.IngressConfig.build({TENANT_A: secret}, (TENANT_A,))
    header = f"Bearer {secret}".encode()
    assert len(header) == module.MAX_AUTH_BYTES
    assert module.authenticate(config, [(b"authorization", header)]) == TENANT_A
    spy = _Spy()
    async with _client(_app(spy, config)) as client:
        response = await client.post(module.ROUTE, content=json.dumps(_body()),
                                     headers={"Authorization": f"Bearer {secret}",
                                              "Content-Type": "application/json"})
    assert response.status_code == 202 and spy.contexts == [TENANT_A]


def test_sg_u02b_config_repr_redacted() -> None:
    config = _config()
    assert repr(config) == "IngressConfig(clinics=2)"
    assert SECRET_A not in repr(config) and SECRET_B not in str(config)


# --- SG-U03 authentication --------------------------------------------------------------------


def _h(*pairs: tuple[str, str]) -> list[tuple[bytes, bytes]]:
    return [(k.encode("latin-1"), v.encode("latin-1")) for k, v in pairs]


AUTH_CASES: dict[str, tuple[list[tuple[bytes, bytes]], str | None]] = {
    "a": (_h(("authorization", f"Bearer {SECRET_A}")), TENANT_A),
    "b": (_h(("authorization", f"Bearer {SECRET_B}")), TENANT_B),
    "missing": ([], None),
    "wrong": (_h(("authorization", "Bearer " + "q" * 40)), None),
    "basic": (_h(("authorization", f"Basic {SECRET_A}")), None),
    "lower-bearer": (_h(("authorization", f"bearer {SECRET_A}")), None),
    "no-space": (_h(("authorization", f"Bearer{SECRET_A}")), None),
    "extra-space": (_h(("authorization", f"Bearer  {SECRET_A}")), None),
    "trailing": (_h(("authorization", f"Bearer {SECRET_A} ")), None),
    "prefix": (_h(("authorization", f"Bearer {SECRET_A[:-1]}")), None),
    "suffix": (_h(("authorization", f"Bearer {SECRET_A}x")), None),
    "two-headers": (_h(("authorization", f"Bearer {SECRET_A}"),
                       ("authorization", f"Bearer {SECRET_A}")), None),
    "oversize": (_h(("authorization", "Bearer " + "z" * 506)), None),
}


@pytest.mark.parametrize("case", sorted(AUTH_CASES))
def test_sg_u03_authenticate(case: str) -> None:
    module = _mod()
    headers, expected = AUTH_CASES[case]
    assert module.authenticate(_config(), headers) == expected


def test_sg_u03b_comparison_visits_every_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constant-time path: hmac.compare_digest is called once per configured secret,
    whether the presented token matches the first, the last or none."""

    module = _mod()
    calls: list[int] = []
    real = module.hmac.compare_digest

    def counting(a: Any, b: Any) -> bool:
        calls.append(1)
        return bool(real(a, b))

    monkeypatch.setattr(module.hmac, "compare_digest", counting)
    config = _config()
    for token in (SECRET_A, SECRET_B, "q" * 40):
        calls.clear()
        module.authenticate(config, _h(("authorization", f"Bearer {token}")))
        assert len(calls) == 2, token


# --- route harness -----------------------------------------------------------------------------


class _Spy:
    def __init__(self, error: BaseException | None = None,
                 context_error: BaseException | None = None) -> None:
        self.contexts: list[str] = []
        self.received: list[tuple[Any, Any]] = []
        self.error = error
        self.context_error = context_error

    async def context_for(self, tenant_id: str) -> Any:
        self.contexts.append(tenant_id)
        if self.context_error is not None:
            raise self.context_error
        return ("ctx", tenant_id)

    async def receive(self, context: Any, message: Any) -> Any:
        self.received.append((context, message))
        if self.error is not None:
            raise self.error
        from haloflow.m05.intake import InboundResult

        return InboundResult(outcome="registered", inbound_id=uuid.UUID(int=7),
                             decision="item", triage_kind="unclear_reply",
                             patient_id=uuid.UUID(int=8), event_id=uuid.UUID(int=9),
                             item_id=uuid.UUID(int=10))


def _app(spy: _Spy, config: Any = None) -> Any:
    from fastapi import FastAPI

    module = _mod()
    app = FastAPI()
    app.include_router(module.create_ingress_router(
        config if config is not None else _config(), context_for=spy.context_for,
        receive=spy.receive))
    return app


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _body(**overrides: Any) -> dict[str, Any]:
    body = {"channel": "sms", "provider_message_id": "pm-1", "phone": PHONE,
            "text": f"maybe {MARKER}"}
    body.update(overrides)
    return body


def _auth(secret: str = SECRET_A) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}", "content-type": "application/json"}


async def test_sg_u04_happy_path_binds_tenant_from_credential() -> None:
    module = _mod()
    spy = _Spy()
    async with _client(_app(spy)) as client:
        response = await client.post(module.ROUTE, headers=_auth(SECRET_B),
                                     content=json.dumps(_body()))
    assert response.status_code == 202
    assert response.json() == {"outcome": "registered", "decision": "item",
                               "inbound_id": str(uuid.UUID(int=7))}
    assert spy.contexts == [TENANT_B]
    ((context, message),) = spy.received
    assert context == ("ctx", TENANT_B)
    assert (message.channel, message.provider_message_id, message.phone, message.text) == (
        "sms", "pm-1", PHONE, f"maybe {MARKER}")
    assert MARKER not in response.text


class _Stream:
    """A request body that records whether it was read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        for chunk in self.chunks:
            yield chunk


async def test_sg_u05_auth_before_media_and_body() -> None:
    module = _mod()
    spy = _Spy()
    stream = _Stream([json.dumps(_body()).encode()])
    async with _client(_app(spy)) as client:
        response = await client.post(module.ROUTE, content=stream,
                                     headers={"authorization": "Bearer " + "q" * 40,
                                              "content-type": "text/plain"})
    assert (response.status_code, response.json()) == (401, {"code": "INGRESS_UNAUTHENTICATED"})
    assert stream.read is False and spy.contexts == [] and spy.received == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                          "application/json; charset=latin-1", ""])
async def test_sg_u05b_media_type_refused_before_body(content_type: str) -> None:
    module = _mod()
    spy = _Spy()
    stream = _Stream([json.dumps(_body()).encode()])
    headers = {"authorization": f"Bearer {SECRET_A}"}
    if content_type:
        headers["content-type"] = content_type
    async with _client(_app(spy)) as client:
        response = await client.post(module.ROUTE, content=stream, headers=headers)
    assert (response.status_code, response.json()) == (
        415, {"code": "INBOUND_UNSUPPORTED_MEDIA"})
    assert stream.read is False and spy.contexts == [] and spy.received == []


async def test_sg_u05c_charset_utf8_accepted() -> None:
    module = _mod()
    spy = _Spy()
    async with _client(_app(spy)) as client:
        response = await client.post(
            module.ROUTE, content=json.dumps(_body()),
            headers={"authorization": f"Bearer {SECRET_A}",
                     "content-type": "application/json; charset=utf-8"})
    assert response.status_code == 202


async def _raw(app: Any, headers: list[tuple[bytes, bytes]],
               chunks: list[bytes]) -> tuple[int, bytes, int]:
    """Drive the ASGI app directly so headers (incl. a false or absent Content-Length)
    and body chunks are exactly as given. Returns (status, body, chunks consumed)."""

    module = _mod()
    pending = list(chunks)
    consumed = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        if pending:
            consumed += 1
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": module.ROUTE, "raw_path":
             module.ROUTE.encode(), "query_string": b"", "headers": headers,
             "client": ("127.0.0.1", 1), "server": ("t", 80), "root_path": ""}
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, consumed


@pytest.mark.parametrize("declared", [None, b"10", b"100000"],
                         ids=["absent", "understated", "overstated"])
async def test_sg_u06_stream_cap_regardless_of_content_length(declared: bytes | None) -> None:
    spy = _Spy()
    chunks = [b"{" + b" " * 8191] * 3 + [b"}"] * 50  # 24 KiB+ streamed in pieces
    headers = [(b"authorization", f"Bearer {SECRET_A}".encode()),
               (b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", declared))
    status, body, consumed = await _raw(_app(spy), headers, chunks)
    assert (status, json.loads(body)) == (413, {"code": "INBOUND_TOO_LARGE"})
    assert consumed <= 3  # stopped at the cap: the remaining chunks were never read
    assert spy.contexts == [] and spy.received == []


async def test_sg_u06b_exact_cap_is_accepted() -> None:
    module = _mod()
    spy = _Spy()
    raw = json.dumps(_body(text="y")).encode()
    padded = raw[:-1] + b" " * (module.MAX_BODY_BYTES - len(raw)) + b"}"
    assert len(padded) == module.MAX_BODY_BYTES
    async with _client(_app(spy)) as client:
        response = await client.post(module.ROUTE, content=padded, headers=_auth())
    assert response.status_code == 202
    assert spy.contexts == [TENANT_A] and len(spy.received) == 1
    over_spy = _Spy()
    async with _client(_app(over_spy)) as client:
        over = await client.post(module.ROUTE, content=padded + b" ", headers=_auth())
    assert (over.status_code, over.json()) == (413, {"code": "INBOUND_TOO_LARGE"})
    assert over_spy.contexts == [] and over_spy.received == []


async def test_sg_u06c_single_oversized_chunk_is_not_copied() -> None:
    """F-SG1: one ASGI chunk far larger than the cap is refused (413) without the
    helper copying it: the chunk is compared with the remaining capacity BEFORE the
    buffer is extended. The chunk object itself is transport-owned memory (allocated
    here, before tracing) and is outside what this helper bounds."""

    module = _mod()
    spy = _Spy()
    huge = b"{" + b" " * (module.MAX_BODY_BYTES * 512)  # ~8 MiB in a single chunk
    headers = [(b"authorization", f"Bearer {SECRET_A}".encode()),
               (b"content-type", b"application/json")]
    app = _app(spy)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        status, body, consumed = await _raw(app, headers, [huge])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (status, json.loads(body)) == (413, {"code": "INBOUND_TOO_LARGE"})
    assert consumed == 1 and spy.contexts == [] and spy.received == []
    assert peak < len(huge) // 8, peak  # a copy would add ~len(huge) traced bytes


@pytest.mark.parametrize(("sizes", "status"), [
    ((16383, 1), 202), ((16383, 2), 413), ((1, 16383), 202), ((1, 16384), 413),
    ((16384,), 202), ((16385,), 413), ((8192, 8192), 202), ((8192, 8192, 1), 413),
], ids=["n-1+1", "n-1+2", "1+n-1", "1+n", "n", "n+1", "half+half", "half+half+1"])
async def test_sg_u06d_multi_chunk_boundaries(sizes: tuple[int, ...], status: int) -> None:
    module = _mod()
    assert module.MAX_BODY_BYTES == 16384
    raw = json.dumps(_body(text="y")).encode()
    total = sum(sizes)
    padded = raw[:-1] + b" " * (total - len(raw)) + b"}"
    chunks, start = [], 0
    for size in sizes:
        chunks.append(padded[start:start + size])
        start += size
    spy = _Spy()
    headers = [(b"authorization", f"Bearer {SECRET_A}".encode()),
               (b"content-type", b"application/json")]
    got, _body_bytes, _consumed = await _raw(_app(spy), headers, chunks)
    assert got == status
    assert (spy.contexts == [TENANT_A]) is (status == 202)
    assert (spy.received == []) is (status == 413)


MALFORMED: dict[str, bytes] = {
    "not-utf8": b'{"channel": "sms", "provider_message_id": "p", "phone": "+15555550123",'
                b' "text": "\xff"}',
    "not-json": b"channel=sms",
    "array": b"[]",
    "nan": b'{"channel": "sms", "provider_message_id": "p", "phone": "+15555550123",'
           b' "text": NaN}',
    "duplicate-key": b'{"channel": "sms", "channel": "sms", "provider_message_id": "p",'
                     b' "phone": "+15555550123", "text": "yes"}',
    "missing-key": json.dumps({"channel": "sms", "phone": PHONE, "text": "yes"}).encode(),
    "extra-key": json.dumps({**_body(), "tenant": TENANT_B}).encode(),
    "wrong-type": json.dumps({**_body(), "text": 1}).encode(),
    "bad-phone": json.dumps({**_body(), "phone": "5555550123"}).encode(),
    "bad-channel": json.dumps({**_body(), "channel": "email"}).encode(),
    "empty-id": json.dumps({**_body(), "provider_message_id": ""}).encode(),
    "id-129": json.dumps({**_body(), "provider_message_id": "p" * 129}).encode(),
    "id-int": json.dumps({**_body(), "provider_message_id": 12345}).encode(),
    "phone-null": json.dumps({**_body(), "phone": None}).encode(),
    "channel-list": json.dumps({**_body(), "channel": ["sms"]}).encode(),
    "text-object": json.dumps({**_body(), "text": {"t": "yes"}}).encode(),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
async def test_sg_u07_malformed_bodies(case: str) -> None:
    module = _mod()
    spy = _Spy()
    async with _client(_app(spy)) as client:
        response = await client.post(module.ROUTE, content=MALFORMED[case], headers=_auth())
    assert (response.status_code, response.json()) == (400, {"code": "INBOUND_MALFORMED"})
    assert MARKER not in response.text
    assert spy.contexts == [] and spy.received == []  # refused before any service effect


async def test_sg_u07b_id_at_limit_accepted() -> None:
    module = _mod()
    spy = _Spy()
    async with _client(_app(spy)) as client:
        response = await client.post(module.ROUTE, headers=_auth(), content=json.dumps(
            {**_body(), "provider_message_id": "p" * 128}))
    assert response.status_code == 202
    assert spy.received[0][1].provider_message_id == "p" * 128


def _error(name: str) -> BaseException:
    from haloflow.m01.errors import ActivationRefused
    from haloflow.m05.intake import CommandMalformed, IntakeCommitUnknown, IntakeFailed

    errors: dict[str, BaseException] = {
        "conflict": IntakeFailed("INBOUND_CONFLICT"),
        "inactive": ActivationRefused(reason_code="M05_NOT_ACTIVATED"),
        "unknown": IntakeCommitUnknown(),
        "command": CommandMalformed("inbound command"),
        "other-failed": IntakeFailed("CONTACT_OPTED_OUT"),
        "runtime": RuntimeError(f"secret {MARKER} {PHONE}"),
    }
    return errors[name]


_FORMATTER = logging.Formatter("%(name)s %(levelname)s %(message)s")


def _formatted(records: list[logging.LogRecord]) -> list[str]:
    """Each record as a handler would emit it, INCLUDING exc_info tracebacks and
    stack_info (getMessage alone would miss them)."""

    return [_FORMATTER.format(record) for record in records]


def _no_leak(texts: list[str]) -> None:
    for text in texts:
        assert MARKER not in text and PHONE not in text and SECRET_A not in text, text


def test_sg_u08n_log_oracle_sees_tracebacks() -> None:
    """Negative control: a fixed message with a sensitive exception attached is caught
    by the same oracle the mapping rows use."""

    try:
        raise RuntimeError(f"secret {MARKER}")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "fixed message", None,
                                   sys.exc_info())
    with pytest.raises(AssertionError):
        _no_leak(_formatted([record]))


MAPPING = {"conflict": (409, "INBOUND_CONFLICT"), "inactive": (409, "CLINIC_NOT_ACTIVATED"),
           "unknown": (503, "INBOUND_COMMIT_UNKNOWN"),
           "command": (400, "INBOUND_MALFORMED"), "other-failed": (500, "INTERNAL"),
           "runtime": (500, "INTERNAL")}


@pytest.mark.parametrize("name", sorted(MAPPING))
async def test_sg_u08_error_mapping(name: str, caplog: pytest.LogCaptureFixture) -> None:
    module = _mod()
    spy = _Spy(error=_error(name))
    with caplog.at_level(logging.DEBUG):
        async with _client(_app(spy)) as client:
            response = await client.post(module.ROUTE, content=json.dumps(_body()),
                                         headers=_auth())
    status, code = MAPPING[name]
    assert (response.status_code, response.json()) == (status, {"code": code})
    _no_leak([response.text, *_formatted(caplog.records)])


@pytest.mark.parametrize("name", ["runtime", "inactive"])
async def test_sg_u08c_context_for_failure(name: str, caplog: pytest.LogCaptureFixture) -> None:
    module = _mod()
    spy = _Spy(context_error=_error(name))
    with caplog.at_level(logging.DEBUG):
        async with _client(_app(spy)) as client:
            response = await client.post(module.ROUTE, content=json.dumps(_body()),
                                         headers=_auth())
    status, code = MAPPING[name]
    assert (response.status_code, response.json()) == (status, {"code": code})
    assert spy.contexts == [TENANT_A] and spy.received == []
    _no_leak([response.text, *_formatted(caplog.records)])


async def test_sg_u08b_reply_refused_is_malformed() -> None:
    from haloflow.m05.reply_classifier import ReplyRefused

    module = _mod()
    spy = _Spy(error=ReplyRefused("TEXT_INVALID"))
    async with _client(_app(spy)) as client:
        response = await client.post(module.ROUTE, content=json.dumps(_body()),
                                     headers=_auth())
    assert (response.status_code, response.json()) == (400, {"code": "INBOUND_MALFORMED"})


def test_sg_s01_not_in_production_app() -> None:
    from haloflow.platform.app import create_app

    module = _mod()
    paths = {getattr(route, "path", None) for route in create_app().routes}
    assert module.ROUTE not in paths
