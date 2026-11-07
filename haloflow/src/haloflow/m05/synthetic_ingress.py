"""L-1S package S-G1: the synthetic inbound-SMS ingress (trust contract T1-T6).

Used only by the test-side synthetic application (Codex ALIGN 20261107T174755Z, option
A: the synthetic registry and application composition stay test-side); never installed
by ``haloflow.platform.app``, ``startup`` or ``composition``. A trusted ingress caller
presents a per-clinic secret as ``Authorization: Bearer <secret>``; the secret alone
selects the clinic. The tenant is never taken from the body, the phone number or another
header. Order per request:
authenticate (401) -> media type (415) -> body read with a hard cap regardless of
Content-Length (413) -> strict UTF-8 / JSON / field validation (400) -> intake.

The raw reply text is held only in memory for classification; it is never logged,
stored, echoed or put in an error body. No provider payload integrity or signature is
claimed: a synthetic secret proves nothing about any SMS provider.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from haloflow.m01.errors import ActivationRefused
from haloflow.m05.intake import CommandMalformed, IntakeCommitUnknown, IntakeFailed
from haloflow.m05.reply_classifier import InboundMessage, ReplyRefused

LOGGER: Final = logging.getLogger(__name__)

ROUTE: Final = "/v1/synthetic/inbound-sms"
MAX_BODY_BYTES: Final = 16384
MAX_AUTH_BYTES: Final = 512
MIN_SECRET_BYTES: Final = 32
MAX_SECRET_BYTES: Final = MAX_AUTH_BYTES - len(b"Bearer ")  # 505: the header must fit
_SECRET_CHARS: Final = re.compile(r"[\x21-\x7e]+")
_PHONE: Final = re.compile(r"\+[1-9][0-9]{7,14}")
_KEYS: Final = frozenset({"channel", "provider_message_id", "phone", "text"})
_MEDIA: Final = frozenset({"application/json", "application/json; charset=utf-8"})


class IngressConfigInvalid(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IngressConfig:
    """Per-clinic ingress secrets. Construct with `build`; the repr is redacted."""

    __slots__ = ("_bindings",)

    def __init__(self, bindings: tuple[tuple[str, bytes], ...]) -> None:
        self._bindings = bindings

    @classmethod
    def build(cls, secrets: Mapping[str, str], served_tenants: tuple[str, ...]) -> IngressConfig:
        if not isinstance(secrets, Mapping) or not all(
                type(k) is str and type(v) is str for k, v in secrets.items()):
            raise IngressConfigInvalid("SECRETS_INVALID")
        if not secrets:
            raise IngressConfigInvalid("SECRETS_EMPTY")
        bindings: list[tuple[str, bytes]] = []
        for tenant_id, secret in secrets.items():
            if tenant_id not in served_tenants:
                raise IngressConfigInvalid("TENANT_NOT_SERVED")
            if not _SECRET_CHARS.fullmatch(secret):
                raise IngressConfigInvalid("SECRET_INVALID_CHARS")
            if len(secret.encode("ascii")) < MIN_SECRET_BYTES:
                raise IngressConfigInvalid("SECRET_TOO_SHORT")
            if len(secret.encode("ascii")) > MAX_SECRET_BYTES:
                raise IngressConfigInvalid("SECRET_TOO_LONG")
            bindings.append((tenant_id, secret.encode("ascii")))
        if len({secret for _, secret in bindings}) != len(bindings):
            raise IngressConfigInvalid("SECRET_DUPLICATE")
        return cls(tuple(bindings))

    def __repr__(self) -> str:
        return f"IngressConfig(clinics={len(self._bindings)})"

    __str__ = __repr__

    def bindings(self) -> tuple[tuple[str, bytes], ...]:
        return self._bindings


def authenticate(config: IngressConfig, headers: list[tuple[bytes, bytes]]) -> str | None:
    values = [value for name, value in headers if name.lower() == b"authorization"]
    if len(values) != 1 or len(values[0]) > MAX_AUTH_BYTES:
        return None
    value = values[0]
    if not value.startswith(b"Bearer ") or len(value) <= 7:
        return None
    token = value[7:]
    matched: str | None = None
    for tenant_id, secret in config.bindings():  # every secret is compared
        if hmac.compare_digest(token, secret):
            matched = tenant_id
    return matched


def _error(status: int, code: str) -> Response:
    LOGGER.info("synthetic_ingress refused status=%d code=%s", status, code)
    return JSONResponse({"code": code}, status_code=status)


async def _bounded_body(request: Request) -> bytes | None:
    """Read the body into a buffer that never exceeds MAX_BODY_BYTES: each chunk is
    compared with the remaining capacity BEFORE it is copied, so an oversized chunk is
    refused without copying it. A chunk the server has already received is
    transport-owned memory; this bounds the application buffer only, not server or
    proxy memory."""

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


def _message(raw: bytes) -> InboundMessage | None:
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(decoded, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except ValueError:
        return None
    if type(value) is not dict or set(value) != _KEYS:
        return None
    if not all(type(value[key]) is str for key in _KEYS):
        return None
    if (value["channel"] != "sms" or not 1 <= len(value["provider_message_id"]) <= 128
            or not _PHONE.fullmatch(value["phone"])):
        return None
    return InboundMessage(channel=value["channel"],
                          provider_message_id=value["provider_message_id"],
                          phone=value["phone"], text=value["text"])


ContextFor = Callable[[str], Awaitable[Any]]
Receive = Callable[[Any, InboundMessage], Awaitable[Any]]


def create_ingress_router(config: IngressConfig, *, context_for: ContextFor,
                          receive: Receive) -> APIRouter:
    router = APIRouter()

    async def inbound(request: Request) -> Response:
        tenant_id = authenticate(config, list(request.scope.get("headers", [])))
        if tenant_id is None:
            return _error(401, "INGRESS_UNAUTHENTICATED")
        media = request.headers.getlist("content-type")
        if len(media) != 1 or media[0].strip().lower() not in _MEDIA:
            return _error(415, "INBOUND_UNSUPPORTED_MEDIA")
        raw = await _bounded_body(request)
        if raw is None:
            return _error(413, "INBOUND_TOO_LARGE")
        message = _message(raw)
        if message is None:
            return _error(400, "INBOUND_MALFORMED")
        try:
            context = await context_for(tenant_id)
            result = await receive(context, message)
        except (ReplyRefused, CommandMalformed):
            return _error(400, "INBOUND_MALFORMED")
        except IntakeFailed as failed:
            if failed.code == "INBOUND_CONFLICT":
                return _error(409, "INBOUND_CONFLICT")
            return _error(500, "INTERNAL")
        except ActivationRefused:
            return _error(409, "CLINIC_NOT_ACTIVATED")
        except IntakeCommitUnknown:
            return _error(503, "INBOUND_COMMIT_UNKNOWN")
        except Exception:  # noqa: BLE001 - fixed code only; no exception text kept
            return _error(500, "INTERNAL")
        return JSONResponse({"outcome": result.outcome, "decision": result.decision,
                             "inbound_id": str(result.inbound_id)}, status_code=202)

    router.add_api_route(ROUTE, inbound, methods=["POST"])
    return router
