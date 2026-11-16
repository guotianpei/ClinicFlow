"""The synthetic simulator routes (Option B B3b, R-B3b-6/7). SYNTHETIC ONLY.

`POST /synthetic/simulator/patient-reply` {"phone", "text"}: the reply goes through the
UNCHANGED S-G1a ingress router (`create_ingress_router`), held in a separate, UNMOUNTED ASGI app
with a per-lifespan random secret (memory only). The simulator builds the exact S-G1a body
(server-generated `provider_message_id`) and calls that app in process (a plain ASGI call: no
httpx, no socket), so authentication, media type, body cap, strict validation, the `W_INGRESS`
workload context and `ReplyIntake.receive` all run as in S-G1a. The ingress status and body are
returned, labelled. The phone must be the canonical fictional NANP number: E.164
`+1 555 555-01NN` (NN 00..99), i.e. `^\\+155555501[0-9]{2}$` (v1r1 §2).

`POST /synthetic/simulator/ehr`: `put_appointment` (SimulatedEhr.put) or `fail_next`
(fail_reads / fail_writes), on the SAME SimulatedEhr the write-back port wraps. It touches no
database row. The simulated EHR lives in process memory (Codex Q2 = a): a restart starts empty.

Audit: one INFO line per action on `haloflow.synthetic` with the action, status and code only:
never the phone, the text or the appointment id.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Final

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m05.intake_statements import INBOUND_KEY
from haloflow.m05.simulated_ehr import STATUSES, EhrRead
from haloflow.m05.synthetic_ingress import ROUTE as INGRESS_ROUTE
from haloflow.m05.synthetic_ingress import IngressConfig, create_ingress_router
from haloflow.synthetic import wiring
from haloflow.synthetic.composition import W_INGRESS
from haloflow.synthetic.labels import labelled, refusal

LOGGER: Final = logging.getLogger("haloflow.synthetic")
PHONE: Final = re.compile(r"\+155555501[0-9]{2}")
REPLY_ROUTE: Final = "/synthetic/simulator/patient-reply"
EHR_ROUTE: Final = "/synthetic/simulator/ehr"
MAX_BODY: Final = 16 * 1024
MAX_TEXT: Final = 1600
FAIL_MAX: Final = 10
INGRESS_CAPS: Final = frozenset({INBOUND_KEY, "m02.observation.append", "m05.followup.create"})
_PUT_REQUIRED: Final = frozenset({"operation", "appointment_id", "status", "start_at"})
_PUT_OPTIONAL: Final = frozenset({"provider_ref", "department_ref", "replaced_by"})
_FAIL_KEYS: Final = frozenset({"operation", "reads", "writes"})


class _Bad(Exception):
    def __init__(self, field: str) -> None:
        super().__init__("malformed")
        self.field = field


async def _json(request: Request) -> Any:
    raw = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_BODY - len(raw):
            raise _Bad("body")
        raw += chunk
    try:
        return json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Bad("body") from None


def _malformed(field: str) -> Response:
    return refusal(422, "MALFORMED", field=field)


# ---------------------------------------------------------------------------- patient reply

def _ingress_app(lifecycle: Any) -> Any:
    """The unmounted S-G1a app for this lifespan (built once, on first use)."""
    cached = getattr(lifecycle, "_b3b_ingress", None)
    if cached is not None:
        return cached
    services = lifecycle.services
    tenant = services.tenant_id
    config = IngressConfig.build({tenant: lifecycle.ingress_secret}, (tenant,))
    reply = wiring.reply_intake(services.gateway)

    async def context_for(tenant_id: str) -> Any:
        return await services.resolver.resolve(
            principal=Principal(kind=PrincipalKind.WORKLOAD, id=W_INGRESS,
                                auth_method="synthetic_ingress",
                                authorized_tenant_ids=frozenset({tenant_id}),
                                capabilities=INGRESS_CAPS),
            tenant_hint=tenant_id, purpose="operations", capabilities=INGRESS_CAPS,
            source=TrustedSource.HTTP, execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

    async def receive(context: Any, message: Any) -> Any:
        return await reply.receive(context, message)       # looked up at call time

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_ingress_router(config, context_for=context_for, receive=receive))
    lifecycle._b3b_ingress = app
    return app


async def _deliver(app: Any, secret: str, payload: bytes) -> tuple[int, Any]:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": INGRESS_ROUTE,
             "raw_path": INGRESS_ROUTE.encode("ascii"), "root_path": "", "query_string": b"",
             "headers": [(b"authorization", b"Bearer " + secret.encode("ascii")),
                         (b"content-type", b"application/json"),
                         (b"content-length", str(len(payload)).encode("ascii"))],
             "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0)}
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    status = next(int(m["status"]) for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, (json.loads(raw) if raw else {})


async def patient_reply(lifecycle: Any, request: Request) -> Response:
    try:
        value = await _json(request)
        if type(value) is not dict:
            raise _Bad("body")
        if set(value) != {"phone", "text"}:
            raise _Bad("unknown_field" if set(value) - {"phone", "text"} else
                       ("phone" if "phone" not in value else "text"))
        phone, text = value["phone"], value["text"]
        if type(phone) is not str or not PHONE.fullmatch(phone):
            raise _Bad("phone")
        if type(text) is not str or not 1 <= len(text) <= MAX_TEXT:
            raise _Bad("text")
    except _Bad as bad:
        LOGGER.info("SIMULATOR action=patient_reply status=422 code=MALFORMED")
        return _malformed(bad.field)
    payload = json.dumps({"channel": "sms", "provider_message_id": str(uuid.uuid4()),
                          "phone": phone, "text": text}).encode("utf-8")
    status, out = await _deliver(_ingress_app(lifecycle), lifecycle.ingress_secret, payload)
    code = out.get("code") if isinstance(out, dict) else None
    LOGGER.info("SIMULATOR action=patient_reply status=%d code=%s", status,
                code if isinstance(code, str) and re.fullmatch(r"[A-Z_]+", code) else "-")
    return labelled(out if isinstance(out, dict) else {}, status)


# ---------------------------------------------------------------------------- EHR

def _optional(value: dict[str, Any], key: str) -> str | None:
    v = value.get(key)
    if v is not None and (type(v) is not str or not 1 <= len(v) <= 128):
        raise _Bad(key)
    return v


def _count(value: dict[str, Any], key: str) -> int:
    v = value[key]
    if type(v) is not int or not 0 <= v <= FAIL_MAX:
        raise _Bad(key)
    return v


async def ehr(lifecycle: Any, request: Request) -> Response:
    operation = "malformed"
    try:
        value = await _json(request)
        if type(value) is not dict:
            raise _Bad("body")
        raw_op = value.get("operation")
        operation = raw_op if isinstance(raw_op, str) else ""
        if operation == "put_appointment":
            keys = set(value)
            if not keys >= _PUT_REQUIRED or keys - _PUT_REQUIRED - _PUT_OPTIONAL:
                raise _Bad("unknown_field" if keys - _PUT_REQUIRED - _PUT_OPTIONAL
                           else sorted(_PUT_REQUIRED - keys)[0])
            appointment_id = value["appointment_id"]
            if type(appointment_id) is not str or not 1 <= len(appointment_id) <= 128:
                raise _Bad("appointment_id")
            status = value["status"]
            if type(status) is not str or status not in STATUSES:
                raise _Bad("status")
            raw = value["start_at"]
            try:
                start_at = datetime.fromisoformat(raw) if type(raw) is str else None
            except ValueError:
                start_at = None
            if start_at is None or start_at.tzinfo is None or start_at.utcoffset() is None:
                raise _Bad("start_at")
            read = EhrRead(status=status, appointment_id=appointment_id, start_at=start_at,
                           provider_ref=_optional(value, "provider_ref"),
                           department_ref=_optional(value, "department_ref"),
                           replaced_by=_optional(value, "replaced_by"))
            lifecycle.port.ehr.put(read)
        elif operation == "fail_next":
            if set(value) != _FAIL_KEYS:
                raise _Bad("unknown_field")
            reads, writes = _count(value, "reads"), _count(value, "writes")
            lifecycle.port.ehr.fail_reads(reads)
            lifecycle.port.ehr.fail_writes(writes)
        else:
            operation = "malformed"
            raise _Bad("operation")
    except _Bad as bad:
        LOGGER.info("SIMULATOR action=ehr_%s status=422", "malformed")
        return _malformed(bad.field)
    LOGGER.info("SIMULATOR action=ehr_%s status=200", operation)
    return labelled({"operation": operation, "ehr_state": "process_memory"})


def install_simulator(app: FastAPI, lifecycle: Any) -> None:
    async def reply_endpoint(request: Request) -> Response:
        return await patient_reply(lifecycle, request)

    async def ehr_endpoint(request: Request) -> Response:
        return await ehr(lifecycle, request)

    app.add_api_route(REPLY_ROUTE, reply_endpoint, methods=["POST"], response_class=Response,
                      include_in_schema=False)
    app.add_api_route(EHR_ROUTE, ehr_endpoint, methods=["POST"], response_class=Response,
                      include_in_schema=False)
