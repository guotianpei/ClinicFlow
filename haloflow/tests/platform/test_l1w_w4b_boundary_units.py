"""L-1W W4b: tenant routes over HTTP with fake services (unit tier, no database).

Plan v1 R-W4-4..R-W4-7, addendum 1 pins 3-4, architecture v2 A5/A8, interface pins
v1 1.4-1.6. Rows: H-01 (every W4b route x the W1 refusal corpus through HTTP),
H-03 (every W4b route), H-02, H-04 (the closed status map), R-01-U (the route asks
for exactly its requirement), the two-step context, and the no-echo rule.

PINNED INTERFACE (interface pins v1; in addition to the W4a contract):
- The tenant services published by ``start_services`` expose ``resolver.resolve(...)``
  (keyword arguments as ``ServedTenantResolver.resolve``), ``staff.self_standing(ctx)``,
  ``staff.display(ctx, staff_ids)``, ``staff.admin(ctx, command)``,
  ``m05.list_items(ctx, query)``, ``m05.get(ctx, item_id)``,
  ``m05.history(ctx, item_id)``, ``m05.act(ctx, action)`` and
  ``m05.read_note(ctx, note_id)``.
- Per tenant request, after admission: resolve #1 with ``capabilities ==
  {SELF_STANDING}`` and a principal holding only that; ``staff.self_standing``; then
  (only if the route is allowed) resolve #2 with ``capabilities ==`` the route's chosen
  alternative and a principal holding ``capabilities_for(standing)``; then exactly one
  service call. Both resolves: ``tenant_hint`` and ``authorized_tenant_ids`` from
  admission only; ``principal.id == str(staff_id)``; ``auth_method == "bearer"``;
  ``purpose == "operations"``; ``source == TrustedSource.HTTP``; one fresh
  ``execution_id`` shared by both; ``correlation_source == ENTRY_POINT_GENERATED``.
- Status map: see ``STATUS_CASES`` below (interface pins v1 1.6).
- Request models are closed; a validation error is 422 ``{"code":"MALFORMED",
  "field":<label>}`` and never echoes input. ``<label>`` is a declared field name, or
  a constant: ``unknown_field`` for any undeclared body key (the key itself is input
  and is never reflected; Codex 264b3e82 #2), ``body`` for an oversized, unparseable
  or non-object body, ``query`` for an undeclared, repeated or excess query
  parameter.

The fake services are labelled unit fakes; the genuine-bundle rows live in
``test_l1w_w4b_routes_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import l1w_support as s
import pytest
import test_l1w_verifier_units as vu

ITEM = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOTE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
KEY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
STAFF = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
TENANT = "clinic-unit"
SELF_STANDING = "m01.staff.self_standing"

READ = "m05.followup.read"
NOTE_READ = "m05.followup.note.read"
DISPLAY = "m01.staff.display"
PREPARE = "m05.followup.prepare"
APPLY = "m05.followup.apply"
APPEND = "m02.observation.append"
SUPERVISE = "m05.followup.supervise"
ADMIN = "m01.staff.admin"

ROUTES: dict[str, tuple[str, str, Any]] = {
    "list": ("GET", "/v1/followup/items?limit=10", None),
    "get": ("GET", f"/v1/followup/items/{ITEM}", None),
    "history": ("GET", f"/v1/followup/items/{ITEM}/history", None),
    "act": ("POST", f"/v1/followup/items/{ITEM}/actions",
            {"client_action_id": KEY, "expected_version": 1, "type": "claim"}),
    "resolve": ("POST", f"/v1/followup/items/{ITEM}/resolve",
                {"client_action_id": KEY, "expected_version": 1, "type": "resolve",
                 "outcome_code": "reached_no_change"}),
    "read_note": ("GET", f"/v1/followup/notes/{NOTE}", None),
    "display": ("POST", "/v1/staff/display", {"staff_ids": [OTHER]}),
    "admin": ("POST", "/v1/staff/admin",
              {"client_action_id": KEY, "operation": "deactivate", "staff_id": OTHER}),
}
SERVICE_METHOD = {
    "list": ("m05", "list_items"), "get": ("m05", "get"), "history": ("m05", "history"),
    "act": ("m05", "act"), "resolve": ("m05", "act"), "read_note": ("m05", "read_note"),
    "display": ("staff", "display"), "admin": ("staff", "admin"),
}
REQUIRED = {
    "list": frozenset({READ}), "get": frozenset({READ}), "history": frozenset({READ}),
    "act": frozenset({PREPARE, APPLY}), "resolve": frozenset({PREPARE, APPLY, APPEND}),
    "read_note": frozenset({READ, NOTE_READ}), "display": frozenset({DISPLAY, READ}),
    "admin": frozenset({ADMIN}),
}
# The bounded context each route asks for when the standing holds everything
# (pins v1r1: minimum alternative plus ROUTE_OPTIONAL & held; Codex 264b3e82 #1).
WITH_EVERYTHING = {**REQUIRED, "act": frozenset({PREPARE, APPLY, READ, SUPERVISE}),
                   "resolve": frozenset({PREPARE, APPLY, APPEND, READ, SUPERVISE})}
EVERYTHING = ("active", ("act", "read"), ("clinic_admin", "supervisor"))
GRANTED_EVERYTHING = frozenset({READ, NOTE_READ, DISPLAY, PREPARE, APPLY, APPEND, SUPERVISE,
                                ADMIN})


# ---------------------------------------------------------------------------
# Unit fakes (labelled)
# ---------------------------------------------------------------------------


@dataclass
class FakeContext:
    kwargs: dict[str, Any]

    @property
    def capabilities(self) -> frozenset[str]:
        caps: frozenset[str] = self.kwargs["capabilities"]
        return caps


@dataclass
class Journal:
    resolves: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str, Any, tuple[Any, ...]]] = field(default_factory=list)


class FakeResolver:
    def __init__(self, journal: Journal, fail: BaseException | None = None,
                 fail_at: int = 1) -> None:
        self.journal = journal
        self.fail = fail
        self.fail_at = fail_at

    async def resolve(self, **kwargs: Any) -> FakeContext:
        self.journal.resolves.append(kwargs)
        if self.fail is not None and len(self.journal.resolves) == self.fail_at:
            raise self.fail
        return FakeContext(kwargs)


class FakeService:
    def __init__(self, name: str, journal: Journal, outcomes: dict[str, Any]) -> None:
        self._name = name
        self._journal = journal
        self._outcomes = outcomes

    def __getattr__(self, method: str) -> Callable[..., Any]:
        async def call(ctx: Any, *args: Any) -> Any:
            self._journal.calls.append((self._name, method, ctx, args))
            outcome = self._outcomes.get(f"{self._name}.{method}")
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                return outcome(*args)
            return outcome
        return call


@dataclass
class FakeServices:
    resolver: FakeResolver
    staff: FakeService
    m05: FakeService


class SpyAdmission:
    def __init__(self) -> None:
        self.admits: list[Any] = []

    async def admit(self, identity: Any) -> Any:
        from haloflow.platform.admission import AdmittedSession

        self.admits.append(identity)
        return AdmittedSession(tenant_id=TENANT, staff_id=STAFF, identity=identity)

    async def logout(self, identity: Any) -> None:
        return None


def _standing(status: str, access: tuple[str, ...], roles: tuple[str, ...]) -> Any:
    from haloflow.m01.staff.service import Standing

    return Standing(status=status, access=access, roles=roles)


def _defaults() -> dict[str, Any]:
    from haloflow.m05.service import ActionResult

    snapshot = {"schema_version": 1, "item_id": ITEM, "client_action_id": KEY,
                "action": "claim", "version": 2, "state": "in_progress",
                "assignee_id": str(STAFF), "outcome_code": None}
    return {
        "m05.list_items": {"schema_version": 1, "items": [], "next": None},
        "m05.get": {"schema_version": 1, "item_id": ITEM},
        "m05.history": {"schema_version": 1, "item_id": ITEM, "entries": []},
        "m05.act": ActionResult(outcome="applied", snapshot=snapshot),
        "m05.read_note": {"note_id": NOTE, "item_id": ITEM, "text": "synthetic"},
        "staff.display": {"schema_version": 1, "staff": []},
        "staff.admin": lambda *_: _admin_result(),  # built on call (red: per route)
    }


def _admin_result(outcome: str = "applied") -> Any:
    from haloflow.m01.staff.service import AdminResult

    return AdminResult(result={"schema_version": 1, "client_action_id": KEY,
                               "operation": "deactivate",
                               "staff": {"schema_version": 1, "staff_id": OTHER}},
                       outcome=outcome)


@dataclass
class Rig:
    journal: Journal
    admission: SpyAdmission
    key: s.SigningKey
    steps: Any


def _rig(*, standing: Any = EVERYTHING, outcomes: dict[str, Any] | None = None,
         resolve_fail: BaseException | None = None, resolve_fail_at: int = 1,
         key: s.SigningKey | None = None) -> Rig:
    from haloflow.platform.app import StartupSteps
    from haloflow.platform.verify import TokenVerifier, VerifierConfig

    journal = Journal()
    admission = SpyAdmission()
    signing = key or vu.RSA
    merged = _defaults()
    if isinstance(standing, BaseException):
        merged["staff.self_standing"] = standing
    else:
        merged["staff.self_standing"] = lambda *_, st=standing: _standing(*st)
    merged.update(outcomes or {})
    services = FakeServices(
        resolver=FakeResolver(journal, resolve_fail, resolve_fail_at),
        staff=FakeService("staff", journal, merged),
        m05=FakeService("m05", journal, merged),
    )

    async def nothing() -> None:
        return None

    async def open_(*_: Any) -> Any:
        return object(), nothing

    async def start_services(*_: Any) -> Any:
        return services

    async def build_verifier(*_: Any) -> Any:
        cfg = VerifierConfig(issuer=s.ISSUER, audience=s.AUDIENCE,
                             algorithms=frozenset({"RS256", "ES256"}),
                             assurance_amr=frozenset({"mfa", "otp"}))
        keys = s.FakeKeys({vu.RSA.kid: vu.RSA, vu.EC.kid: vu.EC})
        return TokenVerifier(cfg, keys, clock=s.Clock())

    async def build_admission(*_: Any) -> Any:
        return admission

    steps = StartupSteps(open_tenant_pool=open_, start_services=start_services,
                         open_admission_pool=open_, open_keyset=open_,
                         build_verifier=build_verifier, build_admission=build_admission)
    return Rig(journal, admission, signing, steps)


@asynccontextmanager
async def _running(rig: Rig) -> AsyncIterator[httpx.AsyncClient]:
    from haloflow.platform.app import create_app

    app = create_app(config=object(), steps=rig.steps)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _auth(rig: Rig, raw: str | None = None) -> dict[str, str]:
    return {"authorization": f"Bearer {raw if raw is not None else s.token(rig.key)}"}


async def _call(client: httpx.AsyncClient, route: str, rig: Rig, *,
                headers: dict[str, str] | None = None, body: Any = None,
                path: str | None = None, raw: bytes | None = None) -> httpx.Response:
    method, default_path, default_body = ROUTES[route]
    h = headers if headers is not None else _auth(rig)
    payload = default_body if body is None else body
    if raw is not None:
        return await client.request(method, path or default_path,
                                     headers={**h, "content-type": "application/json"},
                                     content=raw)
    if method == "GET":
        return await client.request(method, path or default_path, headers=h)
    return await client.request(method, path or default_path, headers=h, json=payload)


def _service_calls(rig: Rig) -> list[tuple[str, str]]:
    return [(svc, m) for svc, m, _ctx, _args in rig.journal.calls if m != "self_standing"]


# ---------------------------------------------------------------------------
# H-01: every W4b route x the W1 refusal corpus, through HTTP
# ---------------------------------------------------------------------------

CORPUS: dict[str, Callable[[], str]] = {
    **{f"v02-{k}": v[0] for k, v in vu.V02_CASES.items()},
    **{f"v03-{k}": (lambda o=v[0]: s.token(vu.RSA, s.claims(**o)))
       for k, v in vu.V03_CASES.items()},
    **{f"v06-{k}": (lambda raw=v: raw) for k, v in vu.V06_CASES.items()},
    "v05-too-large": lambda: s.token(vu.RSA, s.claims(pad="x" * 9000)),
    "v05-deep-header": lambda: s.token(vu.RSA, header={"x": vu._nested(2000)}),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("case", sorted(CORPUS))
async def test_h01_every_route_refuses_the_w1_corpus_before_admission(
    route: str, case: str,
) -> None:
    rig = _rig()
    raw = CORPUS[case]()
    async with _running(rig) as client:
        response = await _call(client, route, rig, headers=_auth(rig, raw) if raw else {})
    assert response.status_code == 401, (route, case, response.status_code)
    assert response.json() == {"code": "UNAUTHENTICATED"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert rig.admission.admits == [] and rig.journal.resolves == []
    assert rig.journal.calls == []


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("auth", [None, "Bearer", "Bearer ", "Basic abc", "Bearer a b"])
async def test_h01_every_route_refuses_a_missing_or_malformed_bearer(
    route: str, auth: str | None,
) -> None:
    rig = _rig()
    async with _running(rig) as client:
        response = await _call(client, route, rig,
                               headers={} if auth is None else {"authorization": auth})
    assert response.status_code == 401 and response.json() == {"code": "UNAUTHENTICATED"}
    assert rig.admission.admits == [] and rig.journal.calls == []


@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_h01_key_set_unavailable_is_503_on_every_route(route: str) -> None:
    rig = _rig()
    from haloflow.platform.app import StartupSteps
    from haloflow.platform.verify import TokenVerifier, VerifierConfig

    async def unavailable_verifier(*_: Any) -> Any:
        cfg = VerifierConfig(issuer=s.ISSUER, audience=s.AUDIENCE,
                             algorithms=frozenset({"RS256"}))
        return TokenVerifier(cfg, s.FakeKeys({vu.RSA.kid: vu.RSA}, unavailable=True),
                             clock=s.Clock())

    st = rig.steps
    rig.steps = StartupSteps(open_tenant_pool=st.open_tenant_pool,
                             start_services=st.start_services,
                             open_admission_pool=st.open_admission_pool,
                             open_keyset=st.open_keyset, build_verifier=unavailable_verifier,
                             build_admission=st.build_admission)
    async with _running(rig) as client:
        response = await _call(client, route, rig)
    assert response.status_code == 503 and response.json() == {"code": "UNAVAILABLE"}
    assert rig.journal.calls == []


# ---------------------------------------------------------------------------
# H-03: tenant input on every W4b route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("where,name", [("header", "X-Tenant-Id"), ("header", "tenant"),
                                        ("query", "tenant_id"), ("query", "Clinic")])
async def test_h03_tenant_input_on_every_route_is_400(route: str, where: str, name: str) -> None:
    rig = _rig()
    method, path, _ = ROUTES[route]
    async with _running(rig) as client:
        if where == "header":
            response = await _call(client, route, rig,
                                   headers={name: "clinic-other", **_auth(rig)})
        else:
            sep = "&" if "?" in path else "?"
            response = await _call(client, route, rig, path=f"{path}{sep}{name}=clinic-other")
    assert response.status_code == 400 and response.json() == {"code": "TENANT_INPUT_REFUSED"}
    assert rig.admission.admits == [] and rig.journal.calls == []
    assert b"clinic-other" not in response.content


@pytest.mark.parametrize("route", ["act", "resolve", "admin", "display"])
@pytest.mark.parametrize("name", ["tenant_id", "tenant", "tenant_hint", "clinic_id"])
async def test_h03_tenant_in_the_body_is_an_unknown_field(route: str, name: str) -> None:
    rig = _rig()
    body = {**ROUTES[route][2], name: "clinic-other"}
    async with _running(rig) as client:
        response = await _call(client, route, rig, body=body)
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "unknown_field"}
    assert _service_calls(rig) == []
    assert all(r["tenant_hint"] == TENANT for r in rig.journal.resolves)


# ---------------------------------------------------------------------------
# The two-step context (A5.1, A5.3; interface pins 1.4) and R-01-U
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_r01u_two_step_context_and_exact_route_request(route: str) -> None:
    from haloflow.m01.context import CorrelationSource, PrincipalKind, TrustedSource

    rig = _rig()
    async with _running(rig) as client:
        response = await _call(client, route, rig)
    assert response.status_code == 200, response.content
    first, second = rig.journal.resolves
    for r in (first, second):
        assert r["tenant_hint"] == TENANT
        assert r["principal"].authorized_tenant_ids == frozenset({TENANT})
        assert r["principal"].kind is PrincipalKind.ACTOR
        assert r["principal"].id == str(STAFF)
        assert r["principal"].auth_method == "bearer"
        assert r["purpose"] == "operations" and r["source"] is TrustedSource.HTTP
        assert r["correlation_source"] is CorrelationSource.ENTRY_POINT_GENERATED
        assert isinstance(r["correlation_id"], uuid.UUID)
        assert isinstance(r["execution_id"], uuid.UUID)
    assert first["capabilities"] == frozenset({SELF_STANDING})
    assert first["principal"].capabilities == frozenset({SELF_STANDING})
    assert second["capabilities"] == WITH_EVERYTHING[route]
    assert second["principal"].capabilities == GRANTED_EVERYTHING
    assert first["execution_id"] == second["execution_id"]
    assert first["correlation_id"] == second["correlation_id"]
    calls = rig.journal.calls
    assert [(c[0], c[1]) for c in calls] == [("staff", "self_standing"), SERVICE_METHOD[route]]
    assert calls[0][2].kwargs is first and calls[1][2].kwargs is second


@pytest.mark.parametrize("standing,route,expected", [
    (("active", ("act", "read"), ()), "act", frozenset({PREPARE, APPLY, READ})),
    (("active", ("act", "read"), ()), "resolve", frozenset({PREPARE, APPLY, APPEND, READ})),
    (("active", ("act", "read"), ("supervisor",)), "act",
     frozenset({PREPARE, APPLY, READ, SUPERVISE})),
    (("active", ("read",), ("supervisor",)), "get", frozenset({READ})),
    (("active", ("act", "read"), ("clinic_admin",)), "display", frozenset({DISPLAY, READ})),
    (("active", (), ("clinic_admin",)), "display", frozenset({DISPLAY, ADMIN})),
])
async def test_r01u_route_context_is_bounded_by_the_standing(
    standing: Any, route: str, expected: frozenset[str],
) -> None:
    rig = _rig(standing=standing)
    async with _running(rig) as client:
        response = await _call(client, route, rig)
    assert response.status_code == 200
    assert rig.journal.resolves[1]["capabilities"] == expected


async def test_r01u_each_request_gets_a_fresh_execution_and_correlation_id() -> None:
    rig = _rig()
    async with _running(rig) as client:
        await _call(client, "get", rig)
        await _call(client, "get", rig)
    ids = {r["execution_id"] for r in rig.journal.resolves}
    cids = {r["correlation_id"] for r in rig.journal.resolves}
    assert len(ids) == 2 and len(cids) == 2


@pytest.mark.parametrize("header", ["x-correlation-id", "x-request-id", "traceparent"])
async def test_r01u_client_correlation_header_is_ignored_and_not_echoed(header: str) -> None:
    supplied = "f0f0f0f0-f0f0-4f0f-8f0f-f0f0f0f0f0f0"
    rig = _rig()
    async with _running(rig) as client:
        response = await _call(client, "get", rig, headers={header: supplied, **_auth(rig)})
    assert response.status_code == 200
    assert all(str(r["correlation_id"]) != supplied for r in rig.journal.resolves)
    assert supplied.encode() not in response.content
    assert supplied not in json.dumps(dict(response.headers))


@pytest.mark.parametrize("status", ["inactive", "unknown"])
@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_r01u_inactive_or_unknown_standing_is_403_on_every_route(
    status: str, route: str,
) -> None:
    rig = _rig(standing=(status, ("act", "read"), ("clinic_admin", "supervisor")))
    async with _running(rig) as client:
        response = await _call(client, route, rig)
    assert response.status_code == 403 and response.json() == {"code": "FORBIDDEN"}
    assert len(rig.journal.resolves) == 1 and _service_calls(rig) == []


async def test_r01u_logout_needs_no_standing() -> None:
    rig = _rig(standing=("inactive", (), ()))
    async with _running(rig) as client:
        response = await client.post("/v1/session/logout", headers=_auth(rig))
    assert response.status_code == 204
    assert rig.journal.resolves == [] and rig.journal.calls == []


# ---------------------------------------------------------------------------
# H-02: the request boundary (A8, addendum 1 pin 4)
# ---------------------------------------------------------------------------

H02_CASES: dict[str, tuple[str, dict[str, Any], str]] = {
    "act-item-id-equal": ("act", {"item_id": ITEM}, "unknown_field"),
    "act-item-id-other": ("act", {"item_id": OTHER}, "unknown_field"),
    "resolve-item-id-equal": ("resolve", {"item_id": ITEM}, "unknown_field"),
    "act-unknown-field": ("act", {"extra": 1}, "unknown_field"),
    "act-missing-version": ("act", {"expected_version": None}, "expected_version"),
    "act-version-string": ("act", {"expected_version": "1"}, "expected_version"),
    "act-version-bool": ("act", {"expected_version": True}, "expected_version"),
    "act-key-not-uuid": ("act", {"client_action_id": "not-a-uuid"}, "client_action_id"),
    "act-type-bad": ("act", {"type": "delete"}, "type"),
    "resolve-outcome-bad": ("resolve", {"outcome_code": "fixed"}, "outcome_code"),
    "resolve-note-not-str": ("resolve", {"note": 7}, "note"),
    "display-not-list": ("display", {"staff_ids": OTHER}, "staff_ids"),
    "display-empty": ("display", {"staff_ids": []}, "staff_ids"),
    "display-bad-uuid": ("display", {"staff_ids": ["x"]}, "staff_ids"),
    "admin-unknown-op": ("admin", {"operation": "bind_identity"}, "operation"),
    "admin-unknown-field": ("admin", {"issuer": "https://x.invalid/"}, "unknown_field"),
    "admin-set-with-staff-id": ("admin", {"operation": "set_auth_setting",
                                          "setting": "mfa_required", "value": "true"},
                                "staff_id"),
    "admin-idle-4": ("admin", {"operation": "set_auth_setting", "staff_id": None,
                               "setting": "idle_timeout_minutes", "value": "4"}, "value"),
    "admin-idle-61": ("admin", {"operation": "set_auth_setting", "staff_id": None,
                                "setting": "idle_timeout_minutes", "value": "61"}, "value"),
    "admin-mfa-yes": ("admin", {"operation": "set_auth_setting", "staff_id": None,
                                "setting": "mfa_required", "value": "yes"}, "value"),
    "admin-role-bad": ("admin", {"operation": "grant_role", "role": "owner"}, "role"),
}


def _h02_body(route: str, change: dict[str, Any]) -> dict[str, Any]:
    body = dict(ROUTES[route][2])
    for k, v in change.items():
        if v is None:
            body.pop(k, None)
        else:
            body[k] = v
    return body


@pytest.mark.parametrize("case", sorted(H02_CASES))
async def test_h02_malformed_body_is_422_without_echo(case: str) -> None:
    route, change, fld = H02_CASES[case]
    rig = _rig()
    body = _h02_body(route, change)
    async with _running(rig) as client:
        response = await _call(client, route, rig, body=body)
    assert response.status_code == 422, (case, response.content)
    assert response.json() == {"code": "MALFORMED", "field": fld}
    assert _service_calls(rig) == []
    for value in change.values():
        if isinstance(value, str) and len(value) > 3:
            assert value.encode() not in response.content


@pytest.mark.parametrize("route", ["act", "resolve", "display", "admin"])
async def test_h02_unknown_key_is_never_reflected(
    route: str, caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _rig()
    canary_key = f"{s.CANARY}-note-text-as-key"
    body = {**ROUTES[route][2], canary_key: "v"}
    with caplog.at_level("DEBUG"):
        async with _running(rig) as client:
            response = await _call(client, route, rig, body=body)
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "unknown_field"}
    assert s.CANARY.encode() not in response.content
    assert s.CANARY not in json.dumps(dict(response.headers))
    assert s.CANARY not in caplog.text
    assert _service_calls(rig) == []


async def test_h02_unknown_query_key_is_never_reflected(caplog: pytest.LogCaptureFixture) -> None:
    rig = _rig()
    with caplog.at_level("DEBUG"):
        async with _running(rig) as client:
            response = await _call(client, "list", rig,
                                   path=f"/v1/followup/items?limit=10&{s.CANARY}=1")
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "query"}
    assert s.CANARY.encode() not in response.content and s.CANARY not in caplog.text


@pytest.mark.parametrize("route", ["act", "resolve", "display", "admin"])
async def test_h02_body_over_16_kib_is_422_before_parsing(route: str) -> None:
    rig = _rig()
    raw = json.dumps({**ROUTES[route][2], "pad": "y" * (16 * 1024)}).encode()
    assert len(raw) > 16 * 1024
    async with _running(rig) as client:
        response = await _call(client, route, rig, raw=raw)
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "body"}
    assert _service_calls(rig) == [] and b"yyyy" not in response.content


@pytest.mark.parametrize("route", ["act", "resolve", "display", "admin"])
async def test_h02_body_at_16_kib_is_not_refused_for_size(route: str) -> None:
    rig = _rig()
    base = ROUTES[route][2]
    raw = json.dumps(base).encode()
    raw = raw[:-1] + b" " * (16 * 1024 - len(raw)) + b"}"
    assert len(raw) == 16 * 1024
    async with _running(rig) as client:
        response = await _call(client, route, rig, raw=raw)
    assert response.status_code == 200, response.content


@pytest.mark.parametrize("route", ["act", "resolve", "display", "admin"])
@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b"\xff\xfe", b""],
                         ids=["truncated", "array", "null", "not-utf8", "empty"])
async def test_h02_unparseable_or_non_object_body_is_422(route: str, raw: bytes) -> None:
    rig = _rig()
    async with _running(rig) as client:
        response = await _call(client, route, rig, raw=raw)
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "body"}
    assert _service_calls(rig) == []


async def test_h02_nine_query_parameters_is_422() -> None:
    rig = _rig()
    query = "&".join(["limit=10"] + [f"p{i}=1" for i in range(8)])
    async with _running(rig) as client:
        response = await _call(client, "list", rig, path=f"/v1/followup/items?{query}")
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "query"}
    assert _service_calls(rig) == []


@pytest.mark.parametrize("query,fld", [
    ("", "limit"), ("limit=0", "limit"), ("limit=101", "limit"), ("limit=x", "limit"),
    ("limit=10&states=done", "states"), ("limit=10&states=open,open", "states"),
    ("limit=10&assignee=nobody", "assignee"),
    (f"limit=10&after_item_id={ITEM}", "after_created_at"),
    ("limit=10&after_created_at=2026-11-04T21:00:00.000000Z", "after_item_id"),
    ("limit=10&unknown=1", "query"),
    ("limit=10&limit=11", "query"),
])
async def test_h02_list_query_validation(query: str, fld: str) -> None:
    rig = _rig()
    async with _running(rig) as client:
        response = await _call(client, "list", rig, path=f"/v1/followup/items?{query}")
    assert response.status_code == 422, (query, response.content)
    assert response.json() == {"code": "MALFORMED", "field": fld}
    assert _service_calls(rig) == []


async def test_h02_list_query_builds_the_service_query() -> None:
    from haloflow.m05.service import Cursor, ListQuery

    rig = _rig()
    created = "2026-11-04T21:00:00.000000Z"
    path = (f"/v1/followup/items?limit=25&states=open,in_progress&assignee=unassigned"
            f"&after_created_at={created}&after_item_id={ITEM}")
    async with _running(rig) as client:
        response = await _call(client, "list", rig, path=path)
    assert response.status_code == 200
    [(_, _, _, (query,))] = [c for c in rig.journal.calls if c[1] == "list_items"]
    from datetime import UTC, datetime

    assert query == ListQuery(limit=25, states=("open", "in_progress"), assignee="unassigned",
                              after=Cursor(created_at=datetime(2026, 11, 4, 21, tzinfo=UTC),
                                           item_id=uuid.UUID(ITEM)))


@pytest.mark.parametrize("route", ["get", "history", "act", "resolve"])
@pytest.mark.parametrize("bad", ["not-a-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                                 "a" * 300])
async def test_h02_bad_path_uuid_is_422_without_echo(route: str, bad: str) -> None:
    rig = _rig()
    method, path, _ = ROUTES[route]
    async with _running(rig) as client:
        response = await _call(client, route, rig, path=path.replace(ITEM, bad))
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "item_id"}
    assert bad.encode() not in response.content and _service_calls(rig) == []


async def test_h02_bad_note_id_is_422() -> None:
    rig = _rig()
    async with _running(rig) as client:
        response = await _call(client, "read_note", rig, path="/v1/followup/notes/xyz")
    assert response.status_code == 422
    assert response.json() == {"code": "MALFORMED", "field": "note_id"}


async def test_h02_item_id_comes_from_the_path_only() -> None:
    from haloflow.m05.service import StaffAction

    rig = _rig()
    async with _running(rig) as client:
        response = await _call(client, "act", rig)
    assert response.status_code == 200
    [(_, _, _, (action,))] = [c for c in rig.journal.calls if c[1] == "act"]
    assert action == StaffAction(item_id=uuid.UUID(ITEM), client_action_id=uuid.UUID(KEY),
                                 expected_version=1, type="claim")


async def test_h02_set_auth_setting_staff_id_is_the_principal() -> None:
    from haloflow.m01.staff.service import StaffAdminCommand

    rig = _rig()
    body = {"client_action_id": KEY, "operation": "set_auth_setting",
            "setting": "idle_timeout_minutes", "value": "05"}
    async with _running(rig) as client:
        response = await _call(client, "admin", rig, body=body)
    assert response.status_code == 200, response.content
    [(_, _, _, (command,))] = [c for c in rig.journal.calls if c[1] == "admin"]
    assert command == StaffAdminCommand(client_action_id=uuid.UUID(KEY),
                                        operation="set_auth_setting", staff_id=STAFF,
                                        setting="idle_timeout_minutes", value="05")


# ---------------------------------------------------------------------------
# H-04: the closed status map (interface pins 1.6)
# ---------------------------------------------------------------------------


def _status_cases() -> dict[str, tuple[str, Callable[[], BaseException], int, dict[str, Any]]]:
    from haloflow.m01.errors import CapabilityDenied, ContextInvalid, TenantDenied
    from haloflow.m01.staff.service import (
        AdminCommitUnknown,
        AdminFailed,
        AdminRefused,
        CommandMalformed,
        ReadFailed,
        ReadRefused,
        RouteDenied,
    )
    from haloflow.m05.service import ActionCommitUnknown, ActionFailed, ActionRefused
    from haloflow.startup import TenantNotServed

    k = uuid.UUID(KEY)
    cases: dict[str, tuple[str, Callable[[], BaseException], int, dict[str, Any]]] = {
        "route-denied": ("get", lambda: RouteDenied(), 403, {"code": "FORBIDDEN"}),
        "read-not-authorized": ("get", lambda: ReadRefused("NOT_AUTHORIZED"), 403,
                                {"code": "FORBIDDEN"}),
        "action-not-authorized": ("act", lambda: ActionRefused("NOT_AUTHORIZED"), 403,
                                  {"code": "FORBIDDEN"}),
        "admin-not-authorized": ("admin", lambda: AdminRefused("NOT_AUTHORIZED"), 403,
                                 {"code": "FORBIDDEN"}),
        "read-not-found": ("get", lambda: ReadRefused("ITEM_NOT_FOUND"), 404,
                           {"code": "NOT_FOUND"}),
        "action-not-found": ("act", lambda: ActionRefused("ITEM_NOT_FOUND"), 404,
                             {"code": "NOT_FOUND"}),
        "read-malformed": ("history", lambda: ReadRefused("COMMAND_MALFORMED"), 409,
                           {"code": "COMMAND_MALFORMED"}),
        "stale": ("act", lambda: ActionRefused("STALE_VERSION"), 409,
                  {"code": "STALE_VERSION"}),
        "resolved": ("resolve", lambda: ActionRefused("ITEM_RESOLVED"), 409,
                     {"code": "ITEM_RESOLVED"}),
        "note-required": ("resolve", lambda: ActionRefused("NOTE_REQUIRED"), 409,
                          {"code": "NOTE_REQUIRED"}),
        "key-conflict": ("act", lambda: ActionRefused("ACTION_KEY_CONFLICT"), 409,
                         {"code": "ACTION_KEY_CONFLICT"}),
        "admin-precondition": ("admin", lambda: AdminRefused("PRECONDITION_FAILED"), 409,
                               {"code": "PRECONDITION_FAILED"}),
        "action-refusal-unlisted": ("act", lambda: ActionRefused("SOMETHING_ELSE"), 500,
                                    {"code": "INTERNAL"}),
        "command-malformed": ("act", lambda: CommandMalformed("assignee_id"), 422,
                              {"code": "MALFORMED", "field": "assignee_id"}),
        "action-key-unavailable": ("resolve", lambda: ActionFailed("KEY_UNAVAILABLE"), 503,
                                   {"code": "KEY_UNAVAILABLE"}),
        "read-note-key-unavailable": ("read_note",
                                      lambda: ReadFailed("NOTE_KEY_UNAVAILABLE"), 503,
                                      {"code": "NOTE_KEY_UNAVAILABLE"}),
        "read-result-invalid": ("list", lambda: ReadFailed("READ_RESULT_INVALID"), 503,
                                {"code": "READ_RESULT_INVALID"}),
        "admin-result-invalid": ("admin", lambda: AdminFailed("ADMIN_RESULT_INVALID"), 503,
                                 {"code": "ADMIN_RESULT_INVALID"}),
        "action-unavailable": ("resolve", lambda: ActionFailed("ACTION_UNAVAILABLE"), 503,
                               {"code": "ACTION_UNAVAILABLE"}),
        "lock-timeout": ("admin", lambda: AdminFailed("LOCK_TIMEOUT"), 503,
                         {"code": "LOCK_TIMEOUT"}),
        "failure-unlisted": ("get", lambda: ReadFailed("SECRET_DETAIL_X"), 503,
                             {"code": "UNAVAILABLE"}),
        "action-commit-unknown": ("act", lambda: ActionCommitUnknown(uuid.UUID(ITEM), k), 409,
                                  {"code": "COMMIT_UNKNOWN", "client_action_id": KEY}),
        "resolve-commit-unknown": ("resolve",
                                   lambda: ActionCommitUnknown(uuid.UUID(ITEM), k), 409,
                                   {"code": "COMMIT_UNKNOWN", "client_action_id": KEY}),
        "admin-commit-unknown": ("admin",
                                 lambda: AdminCommitUnknown(uuid.UUID(OTHER), k, "deactivate"),
                                 409, {"code": "COMMIT_UNKNOWN", "client_action_id": KEY}),
        "m01-capability-denied": ("get", lambda: CapabilityDenied(), 503,
                                  {"code": "UNAVAILABLE"}),
        "m01-context-invalid": ("act", lambda: ContextInvalid(), 503, {"code": "UNAVAILABLE"}),
        "m01-tenant-denied": ("display", lambda: TenantDenied(), 503, {"code": "UNAVAILABLE"}),
        "not-served": ("list", lambda: TenantNotServed(), 503, {"code": "UNAVAILABLE"}),
        "unmapped": ("get", lambda: RuntimeError("secret CANARY-l1w-7f3e"), 500,
                     {"code": "INTERNAL"}),
        "unmapped-keyerror": ("admin", lambda: KeyError("CANARY-l1w-7f3e"), 500,
                              {"code": "INTERNAL"}),
    }
    return cases


STATUS_CASE_NAMES = [
    "route-denied", "read-not-authorized", "action-not-authorized", "admin-not-authorized",
    "read-not-found", "action-not-found", "read-malformed", "stale", "resolved",
    "note-required", "key-conflict", "admin-precondition", "action-refusal-unlisted",
    "command-malformed", "action-key-unavailable", "read-note-key-unavailable",
    "read-result-invalid", "admin-result-invalid", "action-unavailable", "lock-timeout",
    "failure-unlisted", "action-commit-unknown", "resolve-commit-unknown",
    "admin-commit-unknown", "m01-capability-denied", "m01-context-invalid",
    "m01-tenant-denied", "not-served", "unmapped", "unmapped-keyerror",
]


def test_h04_case_list_is_complete() -> None:
    assert sorted(_status_cases()) == sorted(STATUS_CASE_NAMES)


@pytest.mark.parametrize("case", STATUS_CASE_NAMES)
async def test_h04_service_exception_maps_to_its_literal(case: str) -> None:
    route, build, status, body = _status_cases()[case]
    svc, method = SERVICE_METHOD[route]
    rig = _rig(outcomes={f"{svc}.{method}": build()})
    async with _running(rig) as client:
        response = await _call(client, route, rig)
    assert response.status_code == status, (case, response.content)
    assert response.json() == body
    assert b"CANARY" not in response.content and b"SECRET" not in response.content
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("at", [1, 2])
@pytest.mark.parametrize("error", ["not-served", "m01-tenant-denied", "m01-context-invalid"])
async def test_h04_resolve_failure_at_either_step_is_503(at: int, error: str) -> None:
    _, build, _, _ = _status_cases()[error]
    rig = _rig(resolve_fail=build(), resolve_fail_at=at)
    async with _running(rig) as client:
        response = await _call(client, "get", rig)
    assert response.status_code == 503 and response.json() == {"code": "UNAVAILABLE"}
    assert _service_calls(rig) == []


@pytest.mark.parametrize("error", ["read-result-invalid", "read-not-authorized",
                                   "m01-context-invalid", "unmapped"])
async def test_h04_self_standing_failure_never_reaches_the_service(error: str) -> None:
    _, build, status, body = _status_cases()[error]
    rig = _rig(standing=build())
    async with _running(rig) as client:
        response = await _call(client, "get", rig)
    assert response.status_code == status and response.json() == body
    assert len(rig.journal.resolves) == 1 and _service_calls(rig) == []


async def test_h04_success_bodies() -> None:
    rig = _rig()
    async with _running(rig) as client:
        act = await _call(client, "act", rig)
        admin = await _call(client, "admin", rig)
        note = await _call(client, "read_note", rig)
    assert act.status_code == 200 and set(act.json()) == {"outcome", "snapshot"}
    assert act.json()["outcome"] == "applied"
    assert admin.status_code == 200
    assert admin.json() == {"schema_version": 1, "client_action_id": KEY,
                            "operation": "deactivate",
                            "staff": {"schema_version": 1, "staff_id": OTHER},
                            "outcome": "applied"}
    assert note.json() == {"note_id": NOTE, "item_id": ITEM, "text": "synthetic"}


async def test_h04_admin_replayed_outcome_is_returned() -> None:
    rig = _rig(outcomes={"staff.admin": lambda *_: _admin_result("replayed")})
    async with _running(rig) as client:
        response = await _call(client, "admin", rig)
    assert response.status_code == 200 and response.json()["outcome"] == "replayed"


def test_h04_bodies_literal_extends_w4a() -> None:
    from haloflow.platform.http import BODIES

    assert dict(BODIES) == {
        "NOT_READY": (503, {"code": "NOT_READY"}),
        "UNAUTHENTICATED": (401, {"code": "UNAUTHENTICATED"}),
        "REAUTHENTICATION_REQUIRED": (401, {"code": "REAUTHENTICATION_REQUIRED"}),
        "UNAVAILABLE": (503, {"code": "UNAVAILABLE"}),
        "TENANT_INPUT_REFUSED": (400, {"code": "TENANT_INPUT_REFUSED"}),
        "INTERNAL": (500, {"code": "INTERNAL"}),
        "FORBIDDEN": (403, {"code": "FORBIDDEN"}),
        "NOT_FOUND": (404, {"code": "NOT_FOUND"}),
    }


@pytest.mark.parametrize("path,method", [
    ("/v1/followup/items", "DELETE"), ("/v1/followup/items/" + ITEM, "POST"),
    ("/v1/staff/admin", "GET"), ("/v1/support/bootstrap", "POST"), ("/v1/admin", "POST"),
])
async def test_h04_no_other_route_or_method_exists(path: str, method: str) -> None:
    rig = _rig()
    async with _running(rig) as client:
        response = await client.request(method, path, headers=_auth(rig))
    assert response.status_code in (404, 405)
    assert rig.journal.calls == []
