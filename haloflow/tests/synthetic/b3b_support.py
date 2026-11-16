"""Option B increment B3b: shared test support (TEST ONLY; never imported by `src`).

Governing records (Shared Workspace, Work Session 2026-11-15), Codex ALIGN for red preparation
20261116T011830Z (`codex_b3b-v1r1-review.md`):
- `claude_b3b-package-v1-2026-11-15.md` (package) and `claude_b3b-v1r1-2026-11-15.md` (focused
  revision; the later governs the earlier);
- Codex reviews `codex_b3b-v1-and-pr95-review.md` and `codex_b3b-v1r1-review.md`.

Red classes (as B3a, v1r1 D4.1): every row states its class in its docstring.
- C  control: passes now, without any B3b code; proves a fixture or an oracle.
- B  behaviour-red on accepted B3a (`main` a8d6f40): runs now and fails with a NAMED assertion
     about B3a behaviour (for example the spoofed Host reaching the health route).
- A  absence-red: fails in its own body on the missing B3b interface (ModuleNotFoundError for
     `haloflow.synthetic.{gate,labels,staff_routes,simulator}`, AttributeError/TypeError for
     the new names on existing modules).

Independence of the oracles from the code under test:
- `asgi()` is a raw ASGI driver (no httpx: B-N1 patches the httpx constructors, and Starlette's
  TestClient IS an httpx.Client). It records every message the app sends, so labels, headers
  and the body are read off the wire.
- Item seeds and the "direct S-G1a" twin use the accepted test-side S-J harness on the install
  (unguarded, after the first start: the reduced seal accepts runtime-mutable rows).
- View oracles use `b3a_support.table_digests` (admin connection), never the app.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import b2_support as s
import b3a_support as b3

INTERFACE: Final = """
haloflow.synthetic.app
    create_synthetic_app(environ=None, *, lifecycle=None, bind=("127.0.0.1", 8765)) -> FastAPI
    routes exactly ROUTES below (R-B3b-1); docs/openapi None; the S-G1a ingress NOT mounted
haloflow.synthetic.gate
    allowed_hosts(bind: tuple[str, int]) -> frozenset[str]
    CSRF_HEADER = "x-haloflow-synthetic-csrf" ; LABEL_HEADER = "x-haloflow-simulated"
haloflow.synthetic.labels      labelled JSON + JSON 404/405 handlers (R-B3b-5)
haloflow.synthetic.staff_routes   install_staff_routes(app, ...) ; SyntheticServices
haloflow.synthetic.simulator   PHONE = re.compile(r"\\+155555501[0-9]{2}")  (fullmatch)
haloflow.synthetic.lifecycle
    LifecycleConfig(..., t_http=2)  .t_total = 28 + t_http ; t_http bound [1, 10]
    SyntheticLifecycle: .services (resolver, staff, m05) while RUNNING ; .http_in_flight (int)
    DrainCoordinator.for_test: a resource named "http" is the http step (budget t_http,
        code HTTP_NOT_DRAINED), after the producers and before the port
    hook b3.stop.http before the http step ; b3b.gate.enter at the start of the gate (every
        HTTP request; a raise there is a middleware fault, L4)
haloflow.synthetic.errors  + HTTP_NOT_DRAINED HOST_REFUSED CROSS_SITE_REFUSED ORIGIN_REFUSED
                             CSRF_REFUSED
haloflow.synthetic.__main__  serve: port 0 refused (BIND_NOT_LOOPBACK);
    uvicorn.Config(..., timeout_graceful_shutdown=LifecycleConfig().t_total)
responses: header x-haloflow-simulated: true on every response; every JSON body an object
    with "simulated": true; sim_ehr bodies also "ehr_state": "process_memory";
    GET /synthetic/session -> {"simulated": true, "csrf": <token>} with Cache-Control:
    no-store and Pragma: no-cache
"""

BIND: Final = ("127.0.0.1", 8765)
HOST: Final = "127.0.0.1:8765"
ORIGIN: Final = "http://127.0.0.1:8765"
CSRF_HEADER: Final = "x-haloflow-synthetic-csrf"
LABEL_HEADER: Final = "x-haloflow-simulated"
P: Final = "/synthetic/v1/followup/items"

# R-B3b-1: name -> (method, path template). Exactly these, plus nothing.
ROUTES: Final[dict[str, tuple[str, str]]] = {
    "health": ("GET", "/synthetic/health"),
    "session": ("GET", "/synthetic/session"),
    "list": ("GET", P),
    "get": ("GET", P + "/{item_id}"),
    "history": ("GET", P + "/{item_id}/history"),
    "reason": ("GET", P + "/{item_id}/reason"),
    "act": ("POST", P + "/{item_id}/actions"),
    "resolve": ("POST", P + "/{item_id}/resolve"),
    "sim_reply": ("POST", "/synthetic/simulator/patient-reply"),
    "sim_ehr": ("POST", "/synthetic/simulator/ehr"),
}
STAFF_ROUTES: Final = ("list", "get", "history", "reason", "act", "resolve")
NOT_SERVED: Final = ("/synthetic/v1/followup/notes/" + "0" * 8 + "-0000-4000-8000-" + "0" * 12,
                     "/synthetic/v1/staff/display", "/synthetic/v1/staff/admin",
                     "/v1/synthetic/inbound-sms", "/v1/followup/items")

# v1r1 §2: the canonical fictional NANP number (area 555 + 555-0100..0199), E.164.
CANONICAL: Final = re.compile(r"\+155555501[0-9]{2}")
ITEM_KINDS: Final = ("no_reply", "reschedule_request", "cancel_request", "unclear_reply",
                     "ehr_confirmation_check")


def syn_phone(n: int) -> str:
    """The single source of B3b phone numbers (v1r1 §2)."""
    if type(n) is not int or not 0 <= n <= 99:
        raise ValueError("n")
    return f"+155555501{n:02d}"


def path(name: str, item_id: Any = None) -> str:
    template = ROUTES[name][1]
    return template.replace("{item_id}", str(item_id)) if item_id is not None else template


# ---------------------------------------------------------------------------- ASGI driver

@dataclass
class Resp:
    status: int
    headers: list[tuple[str, str]]
    raw: bytes
    messages: list[dict[str, Any]] = field(default_factory=list)

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]

    @property
    def json(self) -> Any:
        return json.loads(self.raw) if self.raw else None


async def asgi(app: Any, method: str, target: str, *,
               headers: list[tuple[str, str]] | None = None, body: bytes = b"",
               hold_body: asyncio.Event | None = None, timeout: float = 60) -> Resp:
    """One in-process ASGI request with exact header control (duplicates allowed, nothing
    added). `hold_body`: the client's `send` parks on the first http.response.body message
    until the event is set (a held body stream, v1r1 §1 mutant 14c)."""
    path_part, _, query = target.partition("?")
    hdrs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or [])]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path_part,
             "raw_path": path_part.encode("ascii"), "root_path": "",
             "query_string": query.encode("ascii"), "headers": hdrs,
             "client": ("127.0.0.1", 50000), "server": BIND}
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and hold_body is not None:
            await hold_body.wait()
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout)
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    hs = [(bytes(k).decode("latin-1").lower(), bytes(v).decode("latin-1"))
          for k, v in start.get("headers", [])]
    return Resp(int(start["status"]), hs, raw, messages)


def get_headers(host: str = HOST, **extra: str) -> list[tuple[str, str]]:
    return [("host", host), *[(k.replace("_", "-"), v) for k, v in extra.items()]]


def post_headers(token: str, *, host: str = HOST, origin: str | None = ORIGIN,
                 ctype: str | None = "application/json",
                 extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    h = [("host", host)]
    if origin is not None:
        h.append(("origin", origin))
    if ctype is not None:
        h.append(("content-type", ctype))
    h.append((CSRF_HEADER, token))
    return h + (extra or [])


def jbody(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


async def session_token(app: Any) -> str:
    r = await asgi(app, "GET", "/synthetic/session", headers=get_headers())
    assert r.status == 200, (r.status, r.raw[:200])
    token = r.json["csrf"]
    assert type(token) is str and len(token) >= 40
    return token


def assert_labelled(r: Resp) -> None:
    """B-L1 (R-B3b-5): the label header, and a JSON object body holding simulated: true."""
    assert r.header(LABEL_HEADER) == ["true"], (r.status, r.headers)
    assert type(r.json) is dict and r.json.get("simulated") is True, (r.status, r.raw[:300])


# ---------------------------------------------------------------------------- lifecycle

def first_start(inst: b3.Install) -> None:
    """One full start/stop of the B3a lifecycle: writes the runtime marker, so the install is
    in reduced-seal mode and accepts runtime-mutable rows from the seed harness."""
    async def body() -> None:
        lc = b3.mod("lifecycle").SyntheticLifecycle(inst.env, application_name="b3b-first")
        await lc.start()
        lc.request_stop("TEST")
        result = await lc.wait_stopped()
        assert result.disposition == "drained", result

    asyncio.run(body())


@asynccontextmanager
async def running(inst: b3.Install, *, app_name: str = "b3b-row", bind: tuple[str, int] = BIND,
                  **lc_kw: Any) -> AsyncIterator[tuple[Any, Any]]:
    """A started lifecycle and the B3b app over it (the app's lifespan is not used: the row
    owns start and stop). Stops on exit unless the row already stopped it."""
    lc = b3.mod("lifecycle").SyntheticLifecycle(inst.env, application_name=app_name, **lc_kw)
    await lc.start()
    app = b3.mod("app").create_synthetic_app(lifecycle=lc, bind=bind)
    try:
        yield lc, app
    finally:
        if lc.state == "RUNNING":
            lc.request_stop("TEST")
        if lc.state in ("STOPPING",):
            await lc.wait_stopped()


# ---------------------------------------------------------------------------- seeds

async def _ingest(stack: Any, phone: str) -> dict[str, str]:
    """`sj_harness.ingest` with an explicit canonical phone (v1r1 §2 seed normalisation)."""
    import sa_support as sa
    import sh2_support as sh2
    import si_support as si

    install, rt = stack.install, stack.rt
    zone, start = sh2.in_window(install)
    patient, ref = sa.new_id(), sa.new_id()
    contact = await sh2.gateway_directory(rt, install, "contact_put", phone=phone,
                                          patient_id=patient)
    assert contact["outcome"] in ("applied", "existing"), contact
    result = await sh2.gateway_directory(rt, install, "appointment_put",
                                         **si.appointment3(ref, patient, start,
                                                           department_tz=zone))
    assert result["outcome"] == "applied", result
    return {"patient": patient, "phone": phone, "ref": ref}


async def _sent(stack: Any, phone: str) -> dict[str, str]:
    flow = await _ingest(stack, phone)
    before = len(stack.sender.calls)
    await stack.tick_reminder()
    assert [c[1] for c in stack.sender.calls[before:]] == [phone]
    return flow


async def _replied(stack: Any, phone: str, text: str) -> dict[str, str]:
    import sa_support as sa
    import sj_harness as sj

    flow = await _sent(stack, phone)
    status, out = await sj.reply(stack, flow, text, message=sa.new_id())
    assert (status, out.get("outcome")) == (202, "registered"), out
    return flow


def seed_kinds(inst: b3.Install, *, base: int = 0) -> dict[str, dict[str, str]]:
    """One open follow-up item of each of the five kinds, seeded test-side (the accepted S-J
    harness, unguarded) AFTER the first start. Phones syn_phone(base .. base+4).
    Returns kind -> {"item_id", "patient", "phone", "ref"}."""
    import sa_support as sa
    import sj_harness as sj
    import test_sj_postgres as tsj

    si_inst = b3.as_si_install(inst)
    out: dict[str, dict[str, str]] = {}

    def item_of(flow: dict[str, str], kind: str) -> None:
        found = [i for i in sa.items_for(si_inst, flow["patient"]) if i["item_kind"] == kind]
        assert len(found) == 1, (kind, found)
        out[kind] = {**flow, "item_id": str(found[0]["item_id"])}

    async def body() -> None:
        ehr, _ = await tsj._flow_ehr(si_inst)
        stack = await sj.build(si_inst, ehr)
        try:
            for n, (text, kind) in enumerate((("2", "reschedule_request"),
                                              ("3", "cancel_request"),
                                              ("maybe later", "unclear_reply"))):
                flow = await _replied(stack, syn_phone(base + n), text)
                await stack.tick_all()
                item_of(flow, kind)
            flow = await _replied(stack, syn_phone(base + 3), "1")
            tsj._put(si_inst, ehr, flow, provider_ref="prov-other")
            await stack.tick_writeback()
            item_of(flow, "ehr_confirmation_check")
            flow = await _sent(stack, syn_phone(base + 4))
            (window,) = sa.admin(si_inst, sa.q(si_inst, "SELECT reminder_id::text FROM"
                                               " {s}.m05_reminder_windows WHERE patient_id = %s"),
                                 (flow["patient"],))
            sa.shift_window(si_inst, window[0], -25)
            await stack.tick_all()
            item_of(flow, "no_reply")
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())
    return out


def sent_only(inst: b3.Install, phone: str) -> dict[str, str]:
    """One reminder sent to `phone` (awaiting a reply), test-side, after the first start."""
    import sj_harness as sj
    import test_sj_postgres as tsj

    si_inst = b3.as_si_install(inst)
    out: dict[str, str] = {}

    async def body() -> None:
        ehr, _ = await tsj._flow_ehr(si_inst)
        stack = await sj.build(si_inst, ehr)
        try:
            out.update(await _sent(stack, phone))
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())
    return out


def direct_reply(inst: b3.Install, phone: str, text: str) -> tuple[int, dict[str, Any]]:
    """The S-G1a twin: the same body posted to the UNCHANGED ingress router on the test-side
    runtime (sh3.ingress_app + post_reply), as the accepted S-J rows do."""
    import sa_support as sa
    import sj_harness as sj
    import test_sj_postgres as tsj

    si_inst = b3.as_si_install(inst)
    got: list[tuple[int, dict[str, Any]]] = []

    async def body() -> None:
        ehr, _ = await tsj._flow_ehr(si_inst)
        stack = await sj.build(si_inst, ehr)
        try:
            got.append(await sj.reply(stack, {"phone": phone}, text, message=sa.new_id()))
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(body())
    return got[0]


def inbound_rows(inst: b3.Install, phone: str) -> list[dict[str, Any]]:
    rows = inst.admin(f"SELECT to_jsonb(r) FROM {inst.schema}.m05_inbound_replies AS r"
                      " WHERE phone = %s ORDER BY 1::text", (phone,))
    return [dict(r[0]) for r in rows]


def item_row(inst: b3.Install, item_id: str) -> dict[str, Any]:
    (row,) = inst.admin(f"SELECT to_jsonb(i) FROM {inst.schema}.m05_followup_items AS i"
                        " WHERE item_id = %s::uuid", (item_id,))
    return dict(row[0])


def new_uuid() -> str:
    return str(uuid.uuid4())


def standing_override(inst: b3.Install, *, access: tuple[str, ...] | None = None,
                      status: str | None = None) -> None:
    """TEST-ONLY (R-B3b-2, J13-J15 denials): edit B2's staff standing on a disposable install
    with an admin connection. The app has no override; this changes the directory itself.
    This changes install-sealed rows, so a row applies it only AFTER its lifecycle started
    and never starts another generation afterwards (the reduced seal would refuse it)."""
    st = inst.state
    if access is not None:
        inst.admin(f"DELETE FROM {inst.schema}.m01_staff_followup_access WHERE staff_id = %s"
                   " AND NOT (access = ANY(%s))", (st["staff_id"], list(access)))
    if status is not None:
        inst.admin(f"UPDATE {inst.schema}.m01_staff SET status = %s WHERE staff_id = %s",
                   (status, st["staff_id"]))


__all__ = ["s"]
