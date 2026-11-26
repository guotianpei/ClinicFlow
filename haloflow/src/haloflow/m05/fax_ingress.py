"""Incoming fax routing FX-IN, package FX3a: the synthetic inbound-fax ingress (trusted route,
receiving-line directory and the lifespan line fence).

Used only by test-side and synthetic composition (the S-G1 placement): never installed by
``haloflow.platform.app``, ``startup`` or ``composition``. No provider signature or payload
integrity is claimed: a synthetic bearer secret proves nothing about any fax provider.

Trust contract:
- the clinic is chosen ONLY by the receiving line in the trusted header ``LINE_HEADER``, looked up
  in a directory built from every served clinic's lines (t144) while the line fence is held; the
  body can never name a line (FORBIDDEN_FIELDS -> 400 BODY_FIELD_FORBIDDEN);
- the line fence is a dedicated session holding ``pg_advisory_lock_shared(K_FX_LINES)`` for the
  life of the router: the line provisioner takes the same key exclusively, so lines cannot change
  while a directory built under the fence is in use. A lost fence is final for that router
  (503 FENCE_LOST); the gateway also re-checks the line under a shared transaction lock, so a
  delivery admitted just before a reassignment is refused 409 LINE_NOT_REGISTERED.

Order per request: fence alive (503) -> bearer (401) -> media type (415) -> capped body read
regardless of Content-Length (413) -> strict JSON body (400) -> header line canonical E.164
(400 MALFORMED line) -> directory lookup (404 LINE_UNKNOWN; no context or receive call) ->
after_lookup (test seam) -> context_for(tenant) -> receive -> 200 {"outcome", "fax_id"}.
No sender, line, doc_ref or provider id is logged or echoed.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

import psycopg
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from haloflow.m05.fax import FaxRefused, InboundFax
from haloflow.m05.fax_rules import E164, K_FX_LINES, SENDER_NAME_RAW_MAX

LOGGER: Final = logging.getLogger(__name__)

ROUTE: Final = "/v1/synthetic/inbound-fax"
LINE_HEADER: Final = "x-synthetic-receiving-line"
MAX_BODY_BYTES: Final = 4096
MAX_AUTH_BYTES: Final = 512
MIN_SECRET_BYTES: Final = 32
MAX_SECRET_BYTES: Final = MAX_AUTH_BYTES - len(b"Bearer ")  # 505: the header must fit
FORBIDDEN_FIELDS: Final = ("line", "to", "to_number", "receiving_line")
OPTIONAL_FIELDS: Final = ("sender_name",)  # FX4: the provider's name as sent (OD-FX10)
BODY_FIELDS: Final = ("provider", "provider_fax_id", "sender", "pages", "received_at",
                      "doc_ref")
_SECRET_CHARS: Final = re.compile(r"[\x21-\x7e]+")
_MEDIA: Final = frozenset({"application/json", "application/json; charset=utf-8"})


class FaxIngressConfigInvalid(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LineDirectoryInvalid(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FaxIngressConfig:
    """The ingress bearer secret. Construct with `build`; the repr is redacted."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    @classmethod
    def build(cls, secret: Any) -> FaxIngressConfig:
        if type(secret) is not str or not _SECRET_CHARS.fullmatch(secret):
            raise FaxIngressConfigInvalid("SECRET_INVALID")
        raw = secret.encode("ascii")
        if not MIN_SECRET_BYTES <= len(raw) <= MAX_SECRET_BYTES:
            raise FaxIngressConfigInvalid("SECRET_LENGTH")
        return cls(raw)

    def __repr__(self) -> str:
        return "FaxIngressConfig()"

    __str__ = __repr__

    def matches(self, headers: list[tuple[bytes, bytes]]) -> bool:
        values = [value for name, value in headers if name.lower() == b"authorization"]
        if len(values) != 1 or len(values[0]) > MAX_AUTH_BYTES:
            return False
        value = values[0]
        if not value.startswith(b"Bearer ") or len(value) <= 7:
            return False
        return hmac.compare_digest(value[7:], self._secret)


class FaxLineDirectory:
    """Receiving line -> tenant, immutable once built. The repr names counts only."""

    __slots__ = ("_by_line",)

    def __init__(self, by_line: Mapping[str, str]) -> None:
        self._by_line = dict(by_line)

    @classmethod
    def build(cls, lines_by_tenant: Mapping[str, Sequence[str]]) -> FaxLineDirectory:
        by_line: dict[str, str] = {}
        for tenant, lines in lines_by_tenant.items():
            for line in lines:
                if type(line) is not str or not E164.fullmatch(line):
                    raise LineDirectoryInvalid("LINE_INVALID")
                if line in by_line:
                    raise LineDirectoryInvalid("LINE_DUPLICATE")
                by_line[line] = tenant
        return cls(by_line)

    def lookup(self, line: str) -> str | None:
        return self._by_line.get(line)

    def __repr__(self) -> str:
        return f"FaxLineDirectory(lines={len(self._by_line)})"

    __str__ = __repr__


class LineFence:
    """A dedicated session holding the shared line lock. `ensure()` probes it; False is final."""

    def __init__(self, connection: psycopg.AsyncConnection[Any]) -> None:
        self._connection = connection
        self.alive = True

    async def ensure(self) -> bool:
        if not self.alive:
            return False
        try:
            async with self._connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
        except Exception:  # noqa: BLE001 - any failure means the lock may be gone
            self.alive = False
            LOGGER.warning("fax ingress line fence lost")
        return self.alive

    def __repr__(self) -> str:
        return f"LineFence(alive={self.alive})"


@asynccontextmanager
async def hold_line_fence(conninfo: str) -> AsyncIterator[LineFence]:
    connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT pg_catalog.pg_advisory_lock_shared(%s)", (K_FX_LINES,))
        fence = LineFence(connection)
        yield fence
    finally:
        with contextlib.suppress(Exception):
            await connection.close()  # closing the session releases the lock


LinesFor = Callable[[str], Awaitable[Sequence[str]]]


async def build_directory(fence: LineFence, tenants: Sequence[str],
                          lines_for: LinesFor) -> FaxLineDirectory:
    """The directory of every served tenant's lines, read while the fence is held."""
    if not await fence.ensure():
        raise LineDirectoryInvalid("FENCE_LOST")
    collected = {tenant: tuple(await lines_for(tenant)) for tenant in tenants}
    directory = FaxLineDirectory.build(collected)
    if not await fence.ensure():
        raise LineDirectoryInvalid("FENCE_LOST")
    return directory


def _error(status: int, code: str, field: str | None = None) -> Response:
    LOGGER.info("fax_ingress refused status=%d code=%s", status, code)
    content: dict[str, str] = {"code": code}
    if field is not None:
        content["field"] = field
    return JSONResponse(content, status_code=status)


async def _bounded_body(request: Request) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_BODY_BYTES - len(body):
            return None
        body += chunk
    return bytes(body)


def _reject_constant(_value: str) -> Any:
    raise ValueError("constant")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate")
    return dict(pairs)


class _BodyRefused(Exception):
    def __init__(self, code: str, field: str) -> None:
        super().__init__(code)
        self.code = code
        self.field = field


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                           parse_constant=_reject_constant)
    except ValueError:
        raise _BodyRefused("MALFORMED", "body") from None
    if type(value) is not dict:
        raise _BodyRefused("MALFORMED", "body")
    for field in FORBIDDEN_FIELDS:
        if field in value:
            raise _BodyRefused("BODY_FIELD_FORBIDDEN", field)
    for field in sorted(value):
        if field not in BODY_FIELDS and field not in OPTIONAL_FIELDS:
            raise _BodyRefused("MALFORMED", field)
    for field in BODY_FIELDS:
        if field not in value:
            raise _BodyRefused("MALFORMED", field)
    for field in ("provider", "provider_fax_id", "received_at", "doc_ref"):
        if type(value[field]) is not str:
            raise _BodyRefused("MALFORMED", field)
    if value["sender"] is not None and type(value["sender"]) is not str:
        raise _BodyRefused("MALFORMED", "sender")
    if type(value["pages"]) is not int:
        raise _BodyRefused("MALFORMED", "pages")
    name = value.get("sender_name")  # FX4 (OD-FX10): optional, untrusted, never logged
    if name is not None and (type(name) is not str or len(name) > SENDER_NAME_RAW_MAX):
        raise _BodyRefused("MALFORMED", "sender_name")
    return value


ContextFor = Callable[[str], Awaitable[Any]]
Receive = Callable[[Any, InboundFax], Awaitable[Mapping[str, Any]]]
AfterLookup = Callable[[], Awaitable[None]]


def create_fax_ingress_router(config: FaxIngressConfig, directory: FaxLineDirectory,
                              fence: LineFence, context_for: ContextFor, receive: Receive, *,
                              after_lookup: AfterLookup | None = None) -> APIRouter:
    router = APIRouter()

    async def inbound(request: Request) -> Response:
        if not await fence.ensure():
            return _error(503, "FENCE_LOST")
        if not config.matches(list(request.scope.get("headers", []))):
            return _error(401, "UNAUTHORIZED")
        media = request.headers.getlist("content-type")
        if len(media) != 1 or media[0].strip().lower() not in _MEDIA:
            return _error(415, "UNSUPPORTED_MEDIA")
        raw = await _bounded_body(request)
        if raw is None:
            return _error(413, "TOO_LARGE")
        try:
            body = _parse(raw)
        except _BodyRefused as refused:
            return _error(400, refused.code, refused.field)
        lines = request.headers.getlist(LINE_HEADER)
        if len(lines) != 1 or not E164.fullmatch(lines[0]):
            return _error(400, "MALFORMED", "line")
        line = lines[0]
        tenant = directory.lookup(line)
        if tenant is None:
            return _error(404, "LINE_UNKNOWN")
        if after_lookup is not None:
            await after_lookup()
        fax = InboundFax(provider=body["provider"], provider_fax_id=body["provider_fax_id"],
                         line=line, sender=body["sender"], pages=body["pages"],
                         received_at=body["received_at"], doc_ref=body["doc_ref"],
                         sender_name=body.get("sender_name"))
        try:
            context = await context_for(tenant)
            result = await receive(context, fax)
        except FaxRefused as refused:
            if refused.code == "LINE_NOT_REGISTERED":
                return _error(409, "LINE_NOT_REGISTERED")
            if refused.code == "COMMAND_MALFORMED":
                return _error(400, "MALFORMED", "body")
            return _error(503, "UNAVAILABLE")
        except Exception:  # noqa: BLE001 - fixed code only; no exception text kept
            return _error(503, "UNAVAILABLE")
        return JSONResponse({"outcome": result.get("outcome"),
                             "fax_id": result.get("fax_id")}, status_code=200)

    router.add_api_route(ROUTE, inbound, methods=["POST"])
    return router
