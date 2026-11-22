"""Eligibility E3b - routed rows through the REAL synthetic ASGI app (RED BY DESIGN).

Codex obligation 2 (routed closure) with Q-T2a option A (Codex d2fb92d9): the actual
`create_synthetic_app` (request guards: Host, Origin, media type, CSRF), the actual
`staff_routes` parsers and production error mapping, over a TEST lifecycle whose services are
the real E3b tenant runtime on PostgreSQL: the composed resolver, the M01 StaffService (self
standing) and `StaffServiceV4`. Labelled exactly so: the synthetic app's own composition of the
eligibility packages is NOT claimed here (that is E4).

Additional pins (staff_routes, synthetic only, F5):
- `case` uses the `get` route's capability requirement (as `reason` does); `contact` uses the
  `act` route's; `resolve` keeps its own.
- request bodies: `resolve` adds an optional `case_token` {case_id, item_id, case_version};
  `contact` = {client_action_id, expected_version, contact_code, case_token} (all required);
  a malformed `case_token` -> 422 {"code": "MALFORMED", "field": "case_token"}.
- `case` -> 200 the case view; ITEM_NOT_FOUND -> 404 NOT_FOUND (as `reason`).
- actions -> 200 {"outcome", "snapshot"} (outcome in applied / replayed / case_changed).

Rows:
- E3b-RC0 CONTROL: the same harness (test lifecycle, real app, gate, parsers) over an E3a install
  with StaffServiceV3: claim through the `act` route and read through `get` -> 200 (passes now;
  proves the harness, the CSRF/Origin/Host headers and the fixed principal's standing).
- E3b-R01 [resolve | claim_and_resolve] durable closure through the routes: case closed with
  its closure_outcome, item resolved, watermark = next_seq - 1, one resolution event (v3).
- E3b-R02 `case` route: the view's exact keys for an eligibility item; 404 NOT_FOUND for an
  unknown id.
- E3b-R03 `contact` route: matching token -> applied and one entry; stale token (an unapplied
  finding) -> case_changed and no entry; missing case_token -> 422 MALFORMED case_token.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import el2_support as e2
import el3a_support as e3
import el3b_support as e4
import m02_support
import pytest

_SYNTHETIC = Path(__file__).resolve().parents[1] / "synthetic"
if str(_SYNTHETIC) not in sys.path:
    sys.path.insert(0, str(_SYNTHETIC))

import b3b_support as bb  # noqa: E402

TOKEN = "t" * 48


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[e4.EL3BInstall]:
    yield from e4.module_install(request, m02_ids, m05_reset, f"el3b-r:{request.node.name}")


class _Lifecycle:
    """The attributes the gate and the staff routes read (gate.py, staff_routes.py)."""

    state = "RUNNING"
    admission_open = True
    stop_code = None
    csrf_token = TOKEN

    def __init__(self, services: Any) -> None:
        self.services = services

    def http_enter(self) -> bool:
        return True

    def http_leave(self) -> None:
        return None


def _begin(inst: Any) -> Any:
    import sd_support as sd

    e3.ready(inst)
    sd.begin(inst)
    return inst


def _el_item(inst: Any) -> tuple[str, uuid.UUID]:
    ref, _, _ = e3.payer(inst, e2.future(20), "inactive", "coverage_inactive")
    e3.drain(inst)
    (case,) = e3.cases(inst, ref)
    return ref, uuid.UUID(case["item_id"])


async def _app(inst: Any, rt: Any, *, v4: bool = True) -> Any:
    import m05_support as m05
    import sd_support as sd

    from haloflow.m01.staff.service import StaffService
    from haloflow.synthetic.app import create_synthetic_app
    from haloflow.synthetic.wiring import SyntheticServices

    staff = e4.Staff(inst, rt, v4=v4)  # StaffServiceV4 (or V3) with the chain's note keys
    services = SyntheticServices(resolver=rt.resolver, staff=StaffService(rt.gateway),
                                 m05=staff.chain.staff, gateway=rt.gateway,
                                 tenant_id=inst.tenant_id, staff_id=m05.S1)
    assert sd.STAFF == m05.S1
    return create_synthetic_app(lifecycle=_Lifecycle(services), bind=bb.BIND)  # type: ignore[arg-type]


def _path(name: str, item: uuid.UUID) -> str:
    template = {**bb.ROUTES, **bb.E3B_ROUTES}[name][1]
    return template.replace("{item_id}", str(item))


async def _post(app: Any, name: str, item: uuid.UUID, body: dict[str, Any]) -> Any:
    return await bb.asgi(app, "POST", _path(name, item), headers=bb.post_headers(TOKEN),
                         body=json.dumps(body).encode())


async def _case(app: Any, item: uuid.UUID) -> Any:
    return await bb.asgi(app, "GET", _path("case", item), headers=bb.get_headers())


def _token(view: dict[str, Any], item: uuid.UUID) -> dict[str, Any]:
    return {"case_id": view["case_id"], "item_id": str(item),
            "case_version": view["case_version"]}


@pytest.fixture
def el3a(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
         m05_reset: Any) -> Iterator[e3.EL3AInstall]:
    yield from e3.module_install(request, m02_ids, m05_reset, f"el3b-rc:{request.node.name}")


# --- E3b-RC0 CONTROL ----------------------------------------------------------------------

def test_el3b_rc0_control_harness_serves_existing_routes_on_e3a(el3a: e3.EL3AInstall) -> None:
    inst = _begin(el3a)

    async def body() -> tuple[Any, Any]:
        async with e4.staff_runtime(inst, el3b=False) as rt:
            item = await e3.legacy_check_item(inst, rt)
            app = await _app(inst, rt, v4=False)
            claimed = await _post(app, "act", item, {"client_action_id": str(uuid.uuid4()),
                                                      "expected_version": 1, "type": "claim"})
            got = await bb.asgi(app, "GET", _path("get", item), headers=bb.get_headers())
            return claimed, got

    claimed, got = asyncio.run(body())
    assert claimed.status == 200 and claimed.json["outcome"] == "applied", \
        (claimed.status, claimed.raw[:300])
    import m05_support as m05

    assert got.status == 200, (got.status, got.raw[:300])
    assert (got.json["assignee_id"], got.json["version"]) == (m05.S1, 2), got.json


# --- E3b-R01 routed closure ---------------------------------------------------------------

@pytest.mark.parametrize("kind", ["resolve", "claim_and_resolve"])
def test_el3b_r01_routed_closure_is_durable(install: e4.EL3BInstall, kind: str) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)

    async def body() -> Any:
        async with e4.staff_runtime(inst) as rt:
            app = await _app(inst, rt)
            version = 1
            if kind == "resolve":
                claimed = await _post(app, "act", item, {"client_action_id": str(uuid.uuid4()),
                                                          "expected_version": 1, "type": "claim"})
                assert claimed.status == 200, (claimed.status, claimed.raw[:300])
                version = 2
            view = await _case(app, item)
            assert view.status == 200, (view.status, view.raw[:300])
            return await _post(app, "resolve", item, {
                "client_action_id": str(uuid.uuid4()), "expected_version": version,
                "type": kind, "outcome_code": "el_insurance_updated",
                "case_token": _token(view.json, item)})

    r = asyncio.run(body())
    assert r.status == 200, (r.status, r.raw[:300])
    assert r.json["outcome"] == "applied" and r.json["snapshot"]["state"] == "resolved"
    (case,) = e3.cases(inst, ref)
    assert (case["state"], case["closure_outcome"]) == ("closed", "el_insurance_updated")
    key = e4.key_row(inst, ref)
    assert key["closure_watermark"] == key["next_seq"] - 1
    assert e4.item(inst, str(item))["state"] == "resolved"
    (event,) = e3.rows(inst, "patient_events",
                       "event_type = 'm05_followup_resolution_asserted'")
    assert event["contract_version"] == 3


# --- E3b-R02 case route -------------------------------------------------------------------

def test_el3b_r02_case_route_view_and_not_found(install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    _ref, item = _el_item(inst)

    async def body() -> tuple[Any, Any]:
        async with e4.staff_runtime(inst) as rt:
            app = await _app(inst, rt)
            return await _case(app, item), await _case(app, uuid.uuid4())

    found, missing = asyncio.run(body())
    assert found.status == 200, (found.status, found.raw[:300])
    assert set(found.json) - {"simulated"} == set(e3.VIEW_KEYS)
    assert found.json["item_id"] == str(item)
    assert missing.status == 404 and missing.json["code"] == "NOT_FOUND"


# --- E3b-R03 contact route ----------------------------------------------------------------

def test_el3b_r03_contact_route_matching_stale_missing(install: e4.EL3BInstall) -> None:
    inst = _begin(install)
    ref, item = _el_item(inst)

    async def first() -> tuple[Any, dict[str, Any]]:
        async with e4.staff_runtime(inst) as rt:
            app = await _app(inst, rt)
            view = (await _case(app, item)).json
            ok = await _post(app, "contact", item, {
                "client_action_id": str(uuid.uuid4()), "expected_version": 1,
                "contact_code": "el_payer_awaiting", "case_token": _token(view, item)})
            missing = await _post(app, "contact", item, {
                "client_action_id": str(uuid.uuid4()), "expected_version": 1,
                "contact_code": "el_payer_awaiting"})
            return (ok, missing), view

    (ok, missing), view = asyncio.run(first())
    assert ok.status == 200 and ok.json["outcome"] == "applied", (ok.status, ok.raw[:300])
    assert missing.status == 422 and missing.json["code"] == "MALFORMED" \
        and missing.json["field"] == "case_token"
    assert e4.contact_entries(inst, str(item)) == ["el_payer_awaiting"]
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")  # unapplied finding

    async def stale() -> Any:
        async with e4.staff_runtime(inst) as rt:
            app = await _app(inst, rt)
            return await _post(app, "contact", item, {
                "client_action_id": str(uuid.uuid4()), "expected_version": 1,
                "contact_code": "el_left_message", "case_token": _token(view, item)})

    changed = asyncio.run(stale())
    assert changed.status == 200 and changed.json["outcome"] == "case_changed"
    assert e4.contact_entries(inst, str(item)) == ["el_payer_awaiting"]
