"""Incoming fax routing FX-IN, package FX3a: the synthetic fax simulator route. SYNTHETIC ONLY.

`POST /synthetic/v1/simulator/fax` {"sender": E.164 | null, "pages": 1..999}: the simulator is
the synthetic fax PROVIDER. It builds the exact ingress body (provider `simfax`, a fresh
server-generated `provider_fax_id`, `received_at` = now, a synthetic `doc_ref`) and delivers it
ONCE, in process (a plain ASGI call: no socket), to the lifecycle's UNMOUNTED fax ingress app
(`fax_wiring`) with the per-lifespan bearer secret and the install's receiving line
(`SYN_FAX_LINE`) in the trusted line header. So bearer, media type, body cap, strict body,
line lookup, the fax workload context and t139 all run as for a provider. The ingress status
and body are returned, labelled. The simulator does NOT retry: a lost acknowledgement and the
provider's retry with the same identity are exercised by the ingress rows (FX3-I07), not
claimed here.

Audit: one INFO line per delivery on `haloflow.synthetic` with the status and code only:
never the sender, the line, the provider id or the doc_ref.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from haloflow.m05 import fax_ingress
from haloflow.m05.fax_rules import E164
from haloflow.synthetic.fax_wiring import SIM_PROVIDER, SYN_FAX_LINE
from haloflow.synthetic.labels import labelled

LOGGER: Final = logging.getLogger("haloflow.synthetic")
SIM_ROUTE: Final = "/synthetic/v1/simulator/fax"
MAX_BODY: Final = 1024
MAX_PAGES: Final = 999


async def _deliver(composition: Any, payload: bytes) -> tuple[int, Any]:
    route = fax_ingress.ROUTE
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": route,
             "raw_path": route.encode("ascii"), "root_path": "", "query_string": b"",
             "headers": [(b"authorization", b"Bearer " + composition.secret.encode("ascii")),
                         (b"content-type", b"application/json"),
                         (fax_ingress.LINE_HEADER.encode("ascii"),
                          SYN_FAX_LINE.encode("ascii")),
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

    await composition.ingress_app(scope, receive, send)
    status = next(int(m["status"]) for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, (json.loads(raw) if raw else {})


def _refused(field: str) -> Response:
    LOGGER.info("SIMULATOR action=fax status=422 code=MALFORMED")
    return labelled({"code": "MALFORMED", "field": field}, 422)


async def _value(request: Request) -> Any:
    raw = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_BODY - len(raw):
            return None
        raw += chunk
    try:
        return json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


async def deliver_fax(lifecycle: Any, request: Request) -> Response:
    composition = getattr(lifecycle, "fax", None)
    if composition is None:
        return labelled({"code": "UNAVAILABLE"}, 503)
    value = await _value(request)
    if type(value) is not dict:
        return _refused("body")
    if set(value) != {"sender", "pages"}:
        return _refused("unknown_field" if set(value) - {"sender", "pages"} else
                        ("sender" if "sender" not in value else "pages"))
    sender, pages = value["sender"], value["pages"]
    if sender is not None and (type(sender) is not str or not E164.fullmatch(sender)):
        return _refused("sender")
    if type(pages) is not int or not 1 <= pages <= MAX_PAGES:
        return _refused("pages")
    provider_fax_id = "SIM-" + uuid.uuid4().hex
    payload = json.dumps({
        "provider": SIM_PROVIDER, "provider_fax_id": provider_fax_id, "sender": sender,
        "pages": pages,
        "received_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "doc_ref": "syn-doc-" + provider_fax_id[-12:]}).encode("utf-8")
    status, out = await _deliver(composition, payload)
    code = out.get("code") if isinstance(out, dict) else None
    shown = code if isinstance(code, str) and re.fullmatch(r"[A-Z_]+", code) else None
    LOGGER.info("SIMULATOR action=fax status=%d code=%s", status, shown or "-")
    return labelled(out if isinstance(out, dict) else {}, status)


def install_fax_simulator(app: FastAPI, lifecycle: Any) -> None:
    async def endpoint(request: Request) -> Response:
        return await deliver_fax(lifecycle, request)

    app.add_api_route(SIM_ROUTE, endpoint, methods=["POST"], response_class=Response,
                      include_in_schema=False)
