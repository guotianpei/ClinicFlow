"""The B5 simulator actions: scenario load, manual tick, outbox (Option B B5). SYNTHETIC ONLY.

`load(lifecycle, scenario)` loads one packaged scenario (`scenarios`) as the directory
workload (`composition.W_DIRECTORY`, `LOAD_CAPABILITIES`) in ONE
`EventFoundation.run_in_transaction` (B5 v1r2 §3; the foundation is built by
`wiring.load_foundation`, the one synthetic EventFoundation construction site):

1. `CLAIM_KEY` (t096): a transaction-scoped advisory lock per (tenant, scenario), the count of
   the scenario's refs already present, and the database clock read after the lock
   (`b5.load.clock`). Any ref present -> 409 SCENARIO_PRESENT. The clock outside the load
   hours of every zone in `scenarios.LOAD_ZONES` (read at call time) -> 409
   OUTSIDE_LOAD_HOURS. Both refusals roll back and write nothing.
2. (S1/S3) the simulated EHR entries, synchronously, BEFORE any directory row, so the entry is
   readable from the moment the rows commit (P-RDY1). An entry left by a load that rolled
   back belongs to no present ref and is never shown (`scenarios.visible_ehr`); a later load
   overwrites it.
3. `DIRECTORY_KEY` `contact_put` for every row, then `appointment_put` for every row. A
   result other than applied (contact_put: also existing) raises inside the transaction, so
   it rolls back -> 500 SCENARIO_LOAD_FAILED.
After the body's last statement the commit outcome is the transaction's: an error after that
point (commit or `b5.load.after_commit`) is 500 SCENARIO_LOAD_UNKNOWN (the PA-05 rule, as
`IntakeService._run`). Cancellation always propagates.

`tick(lifecycle, scheduler)` runs one manual tick of a B3a managed scheduler; `outbox`
lists the FakeSender calls (newest first, at most `OUTBOX_CAP`). Audit: one INFO line per
load and per tick, with the status, the closed code and the scenario / scheduler name only.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import FastAPI
from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import Response

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m05.intake_statements import DIRECTORY_KEY
from haloflow.m05.simulated_ehr import EhrRead
from haloflow.synthetic import profiles, scenarios, sim_clearinghouse, wiring
from haloflow.synthetic.composition import W_DIRECTORY
from haloflow.synthetic.display_statements import CLAIM_KEY
from haloflow.synthetic.errors import SyntheticRefused
from haloflow.synthetic.hooks import afire, fire
from haloflow.synthetic.labels import labelled

LOGGER: Final = logging.getLogger("haloflow.synthetic")
LOAD_ROUTE: Final = "/synthetic/simulator/load-scenario"
TICK_ROUTE: Final = "/synthetic/simulator/tick"
OUTBOX_ROUTE: Final = "/synthetic/simulator/outbox"
CLEARINGHOUSE_ROUTE: Final = "/synthetic/simulator/clearinghouse"     # E4, eligibility only
SCHEDULERS: Final = ("due", "reminder", "writeback")
OUTBOX_CAP: Final = 50
LOAD_CAPABILITIES: Final = frozenset({DIRECTORY_KEY, CLAIM_KEY})
# E4 (D-E4-9): the installed t096 claim accepts only S1..S4 and E4 adds no migration unit, so an
# E scenario takes the claim's per-scenario LOCK under a fixed S name; the claim's present
# count is still over the E scenario's own refs (the lock only serialises loads).
CLAIM_LOCK: Final = {"E1": "S1", "E2": "S2", "E3": "S3", "E4": "S4", "E5": "S1"}
MAX_BODY: Final = 1024


@dataclass(frozen=True)
class Result:
    """An action's outcome: the HTTP status, the closed code (None on success) and the JSON
    body (unlabelled; the JSON adapter labels it)."""

    status: int
    code: str | None
    body: dict[str, Any] = field(default_factory=dict)


def _refusal(status: int, code: str) -> Result:
    return Result(status=status, code=code, body={"code": code})


class _Refuse(Exception):
    """A load refusal raised inside the transaction (it rolls back)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _stopping(lifecycle: Any) -> Result | None:
    if lifecycle.admission_open and lifecycle.services is not None:
        return None
    return _refusal(503, str(lifecycle.stop_code))


# ---------------------------------------------------------------------------- load

def _audit_load(result: Result, scenario: str | None) -> Result:
    LOGGER.info("SIMULATOR action=load_scenario status=%d code=%s scenario=%s", result.status,
                result.code or "-", scenario or "-")
    return result


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def _context(lifecycle: Any) -> Any:
    services = lifecycle.services
    tenant = services.tenant_id
    return await services.resolver.resolve(
        principal=Principal(kind=PrincipalKind.WORKLOAD, id=W_DIRECTORY,
                            auth_method="synthetic_simulator",
                            authorized_tenant_ids=frozenset({tenant}),
                            capabilities=LOAD_CAPABILITIES),
        tenant_hint=tenant, purpose="operations", capabilities=LOAD_CAPABILITIES,
        source=TrustedSource.HTTP, execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)


def _single(rows: list[Any]) -> Mapping[str, Any]:
    if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
        raise RuntimeError("RESULT_INVALID")
    value: Mapping[str, Any] = rows[0][0]
    return value


async def _directory(binding: Any, context: Any, op: str, fields: Mapping[str, Any],
                     accepted: frozenset[str]) -> None:
    principal = context.principal
    command = {"schema_version": 1, "op": op, **fields, "attribution": {
        "principal_kind": principal.kind.value, "principal_id": principal.id,
        "auth_method": principal.auth_method, "execution_id": str(context.execution_id),
        "request_id": context.request_id, "purpose_code": context.purpose}}
    result = _single(await binding.handle.fetch_all(DIRECTORY_KEY, {"command": Jsonb(command)}))
    if set(result) != {"schema_version", "outcome"} or result["schema_version"] != 1 \
            or result["outcome"] not in accepted:
        raise RuntimeError("directory result not applied")


async def load(lifecycle: Any, scenario: str) -> Result:
    """Load one packaged scenario (module docstring). Never retries."""
    fixtures = scenarios.fixtures()
    if type(scenario) is not str or scenario not in fixtures \
            or (scenario in scenarios.EL_NAMES
                and not profiles.has_eligibility(_profile(lifecycle))):
        return _audit_load(_refusal(422, "UNKNOWN_SCENARIO"), None)
    stopping = _stopping(lifecycle)
    if stopping is not None:
        return _audit_load(stopping, scenario)
    spec = fixtures[scenario]
    tenant = str(lifecycle.services.tenant_id)
    keyed = [(row, *scenarios.ids(tenant, scenario, row.key)) for row in spec.rows]
    refs = [str(ref) for _row, _patient, ref in keyed]
    ehr = lifecycle.port.ehr
    complete = False

    async def body(binding: Any) -> None:
        nonlocal complete
        claim = _single(await binding.handle.fetch_all(
            CLAIM_KEY, {"scenario": CLAIM_LOCK.get(scenario, scenario), "refs": Jsonb(refs)}))
        now = datetime.fromisoformat(str(claim["now"]).replace("Z", "+00:00"))
        fire("b5.load.clock", {"now": _iso_z(now)})
        if claim["present"] != 0:
            raise _Refuse(409, "SCENARIO_PRESENT")
        try:
            zone, local_date = scenarios.place(now, scenarios.LOAD_ZONES)
        except scenarios.OutsideLoadHours:
            raise _Refuse(409, "OUTSIDE_LOAD_HOURS") from None
        await afire("b5.load.after_claim", {})
        starts = {row.key: scenarios.start_at(zone, local_date, row.lead_days)
                  for row in spec.rows}
        for row, _patient, ref in keyed:
            if row.ehr:
                ehr.put(EhrRead(status="neutral", appointment_id=str(ref),
                                start_at=starts[row.key], provider_ref=row.provider_ref,
                                department_ref=row.department_ref, replaced_by=None))
        await afire("b5.load.after_ehr", {})
        for row, patient, _ref in keyed:
            await _directory(binding, context, "contact_put",
                             {"phone": row.phone, "patient_id": str(patient)},
                             frozenset({"applied", "existing"}))
        for row, patient, ref in keyed:
            await _directory(binding, context, "appointment_put", {
                "appointment_ref": str(ref), "patient_id": str(patient),
                "start_at": _iso_z(starts[row.key]), "status": "booked",
                "first_name": row.first_name, "confirmation": "not_confirmed",
                "department_tz": zone, "source_status": "booked",
                "provider_ref": row.provider_ref, "department_ref": row.department_ref,
                **({} if row.coverage is None else {"coverage": dict(row.coverage)})},
                frozenset({"applied"}))
        await afire("b5.load.before_commit", {})
        complete = True                     # the body's final action

    try:
        context = await _context(lifecycle)
        await wiring.load_foundation(lifecycle.services.gateway).run_in_transaction(context, body)
        await afire("b5.load.after_commit", {})
    except asyncio.CancelledError:
        raise
    except _Refuse as refused:
        return _audit_load(_refusal(refused.status, refused.code), scenario)
    except Exception:  # noqa: BLE001 - every other failure maps to a closed code
        stopping = _stopping(lifecycle)
        if stopping is not None and not complete:
            return _audit_load(stopping, scenario)
        return _audit_load(_refusal(500, "SCENARIO_LOAD_UNKNOWN" if complete
                                    else "SCENARIO_LOAD_FAILED"), scenario)
    return _audit_load(Result(status=200, code=None,
                              body={"scenario": scenario, "outcome": "loaded"}), scenario)


def load_value(lifecycle: Any, value: object) -> Result | None:
    """The JSON body shape of a load (both adapters): exactly {"scenario": <str>}. Returns the
    MALFORMED refusal (audited), or None when the shape is valid."""
    if type(value) is not dict or set(value) != {"scenario"} or type(value["scenario"]) is not str:
        return _audit_load(_refusal(422, "MALFORMED"), None)
    return None


# ---------------------------------------------------------------------------- tick

def _audit_tick(result: Result, scheduler: str | None) -> Result:
    LOGGER.info("SIMULATOR action=tick status=%d code=%s scheduler=%s", result.status,
                result.code or "-", scheduler or "-")
    return result


def _counts(outcome: Any) -> dict[str, int]:
    flat = getattr(outcome, "flat_counts", None)        # E4: the eligibility tick body
    if isinstance(flat, Mapping):
        return {str(k): int(v) for k, v in flat.items() if type(v) is int and v >= 0}
    counts: dict[str, int] = {}
    codes = getattr(outcome, "codes", None)
    if isinstance(codes, Mapping):
        counts.update({str(k): int(v) for k, v in codes.items() if type(v) is int})
    for name in ("attempted", "no_reply", "opted_out"):
        v = getattr(outcome, name, None)
        if type(v) is int:
            counts[name] = v
    return {k: v for k, v in counts.items() if v >= 0}


def _profile(lifecycle: Any) -> str:
    """The lifecycle's install profile (the recorded one once started, else the request)."""
    installed = getattr(lifecycle, "install_profile", None)
    return str(installed or getattr(lifecycle, "profile", profiles.DEFAULT))


def _schedulers(lifecycle: Any) -> tuple[str, ...]:
    """E4: the eligibility profile adds `eligibility`; the default set is SCHEDULERS."""
    if profiles.has_eligibility(_profile(lifecycle)):          # FX3a: fax composes E4 too
        return profiles.schedulers(_profile(lifecycle))
    return SCHEDULERS


async def tick(lifecycle: Any, scheduler: str) -> Result:
    """One manual tick of the named B3a managed scheduler (serialised with its loop)."""
    if type(scheduler) is not str or scheduler not in _schedulers(lifecycle):
        return _audit_tick(_refusal(422, "UNKNOWN_SCHEDULER"), None)
    stopping = _stopping(lifecycle)
    if stopping is not None:
        return _audit_tick(stopping, scheduler)
    try:
        outcome = await lifecycle.schedulers[scheduler].tick()
    except asyncio.CancelledError:
        raise
    except SyntheticRefused as refused:
        return _audit_tick(_refusal(503, refused.code), scheduler)
    except Exception:  # noqa: BLE001 - a scheduler refusal while running is closed
        stopping = _stopping(lifecycle)
        return _audit_tick(stopping or _refusal(500, "TICK_FAILED"), scheduler)
    return _audit_tick(Result(status=200, code=None,
                              body={"scheduler": scheduler, "counts": _counts(outcome)}),
                       scheduler)


def tick_value(value: object) -> Result | None:
    if type(value) is not dict or set(value) != {"scheduler"} \
            or type(value["scheduler"]) is not str:
        return _audit_tick(_refusal(422, "MALFORMED"), None)
    return None


# ---------------------------------------------------------------------------- outbox

def outbox(lifecycle: Any) -> list[dict[str, str]]:
    """The FakeSender calls, newest first, at most OUTBOX_CAP (process memory: a restart
    empties it). Nothing was sent."""
    sender = lifecycle.sender
    calls = list(getattr(sender, "calls", ()) or ())
    return [{"op_id": str(c[0]), "phone": str(c[1]), "text": str(c[2])}
            for c in reversed(calls[-OUTBOX_CAP:])]


# ---------------------------------------------------------------------------- JSON routes

async def _json(request: Request) -> object:
    import json

    raw = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_BODY - len(raw):
            return _BAD
        raw += chunk
    try:
        value: object = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _BAD
    return value


_BAD: Final = object()


def _response(result: Result) -> Response:
    return labelled(result.body, result.status)


def install_sim_actions(app: FastAPI, lifecycle: Any) -> None:
    async def load_endpoint(request: Request) -> Response:
        value = await _json(request)
        refused = load_value(lifecycle, value)
        if refused is not None:
            return _response(refused)
        assert isinstance(value, dict)
        return _response(await load(lifecycle, value["scenario"]))

    async def tick_endpoint(request: Request) -> Response:
        value = await _json(request)
        refused = tick_value(value)
        if refused is not None:
            return _response(refused)
        assert isinstance(value, dict)
        return _response(await tick(lifecycle, value["scheduler"]))

    async def outbox_endpoint() -> Response:
        return labelled({"outbox": outbox(lifecycle)}, headers={"Cache-Control": "no-store"})

    app.add_api_route(LOAD_ROUTE, load_endpoint, methods=["POST"], response_class=Response,
                      include_in_schema=False)
    app.add_api_route(TICK_ROUTE, tick_endpoint, methods=["POST"], response_class=Response,
                      include_in_schema=False)
    app.add_api_route(OUTBOX_ROUTE, outbox_endpoint, methods=["GET"], response_class=Response,
                      include_in_schema=False)
    if profiles.has_eligibility(getattr(lifecycle, "profile", profiles.DEFAULT)):
        async def clearinghouse_endpoint(request: Request) -> Response:
            return _response(clearinghouse(lifecycle, await _json(request)))

        app.add_api_route(CLEARINGHOUSE_ROUTE, clearinghouse_endpoint, methods=["POST"],
                          response_class=Response, include_in_schema=False)


# ---------------------------------------------------------------------------- clearinghouse

def _audit_control(result: Result, op: str | None) -> Result:
    LOGGER.info("SIMULATOR action=clearinghouse status=%d code=%s op=%s", result.status,
                result.code or "-", op or "-")
    return result


def clearinghouse(lifecycle: Any, value: object) -> Result:
    """E4 (D-E4-4): one control of the composed SYNTHETIC clearinghouse (eligibility only)."""
    try:
        control = sim_clearinghouse.parse(value)
    except sim_clearinghouse.ControlMalformed:
        return _audit_control(_refusal(422, "MALFORMED"), None)
    stopping = _stopping(lifecycle)
    if stopping is not None:
        return _audit_control(stopping, control[0])
    if lifecycle.clearinghouse is None:
        return _audit_control(_refusal(503, "RUNTIME_STOPPED"), control[0])
    sim_clearinghouse.apply(lifecycle.clearinghouse, control)
    return _audit_control(Result(status=200, code=None, body={"outcome": "applied"}),
                          control[0])
