"""Eligibility E3a — unit rows, no database (RED BY DESIGN).

Every E3a import is deferred into the row body, so the intended red cause is
ModuleNotFoundError for the E3a modules (`haloflow.m05.eligibility_labels`,
`haloflow.m05.eligibility_cases`) or TypeError for the new composition flag. Rows marked
CONTROL use existing code and pass now. Interface pins: the el3a_support docstring.

Rows:
- E3a-UC0 CONTROL: the E2 registry and catalogue build; the production target is 36.
- E3a-U01 the eleven reason labels, byte-exact OD-EL8; no label for coverage_active or a
  note-only reason; every label <= 64 characters [OD-EL8]
- E3a-U02 composition: include_el3a requires include_el2 (ValueError); the E3a registry adds
  exactly t118..t121 over E2 (target 121); the catalogue adds exactly the three statement
  keys with their capabilities; the production registry and catalogue are unchanged; the
  E3a test profile activates the event type and maps the three kinds by open_kind [R4]
- E3a-U03 service admission: a non-workload principal and any capability set other than
  CASE_CAPABILITIES are refused before any I/O [pins]
- E3a-U04 the evidence observation the service builds: type, contract version, dedupe key,
  correlation, metadata keys exactly, no patient data in metadata [obligation 1, EL-14]
- E3a-U05 the service's call order in one transaction (apply -> append -> create for
  opened; apply -> append for noted / none; apply only for idle) and a lost acknowledgement
  after the commit marker -> EligibilityCaseCommitUnknown [pins]
- E3a-U06 scheduler: cases and case_contexts both or neither (CASES_INVALID); apply runs
  after expire; without cases, apply is NOT_RUN with zero counts [pins]
- E3a-U07 no network import in the E3a modules [EL-6]
- E3a-U08 scheduler apply phase: a case context whose tenant differs from the scheduled
  tenant is refused before drain (TENANT_MISMATCH, zero counts, DEGRADED); matched control
  drains [Codex E3a green review 2026-11-20]
"""

from __future__ import annotations

import asyncio
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any

import el3a_support as e3
import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow" / "m05"


# --- E3a-UC0 CONTROL ----------------------------------------------------------------------

def test_el3a_uc0_control_e2_composition_builds() -> None:
    from haloflow.composition import APPROVED_SUPPORTED_SCHEMA_VERSIONS
    from haloflow.synthetic.composition import (
        build_synthetic_catalog,
        build_synthetic_registry,
    )

    build_synthetic_registry(include_el1=True, include_el2=True)
    build_synthetic_catalog(include_el1=True, include_el2=True)
    assert frozenset({36}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS


# --- E3a-U01 ------------------------------------------------------------------------------

def test_el3a_u01_reason_labels_are_byte_exact() -> None:
    from haloflow.m05.eligibility_labels import REASON_LABELS

    assert dict(REASON_LABELS) == e3.REASON_LABELS
    assert "coverage_active" not in REASON_LABELS
    for reason in e3.NOTE_ONLY:
        assert reason not in REASON_LABELS
    assert all(len(label) <= 64 for label in REASON_LABELS.values())


# --- E3a-U02 ------------------------------------------------------------------------------

def test_el3a_u02_include_el3a_requires_include_el2() -> None:
    from haloflow.synthetic.composition import (
        build_synthetic_catalog,
        build_synthetic_registry,
    )

    with pytest.raises(ValueError):
        build_synthetic_registry(include_el1=True, include_el3a=True)
    with pytest.raises(ValueError):
        build_synthetic_catalog(include_el1=True, include_el3a=True)


def _unit_ids(registry: Any) -> set[str]:
    return set(registry.migration_ids)


def test_el3a_u02_registry_adds_exactly_t118_to_t121() -> None:
    from haloflow.synthetic.composition import build_synthetic_registry

    e2_ids = _unit_ids(build_synthetic_registry(include_el1=True, include_el2=True))
    e3_ids = _unit_ids(e3.el3a_registry())
    added = sorted(e3_ids - e2_ids)
    assert [a[:4] for a in added] == ["t118", "t119", "t120", "t121"]
    assert added == ["t118_m05_el_case_tables", "t119_m05_create_item_v3",
                     "t120_m05_el_apply", "t121_m05_el_case_view"]
    assert e2_ids <= e3_ids
    assert e3.el3a_registry().target_version == e3.EL3A_TARGET


def test_el3a_u02_catalogue_adds_exactly_the_three_keys() -> None:
    from haloflow.synthetic.composition import build_synthetic_catalog

    before = set(build_synthetic_catalog(include_el1=True, include_el2=True).catalog.keys())
    after = e3.el3a_catalog().catalog
    assert set(after.keys()) - before == set(e3.STATEMENT_KEYS)
    caps = {key: after.resolve(key).required_capability for key in e3.STATEMENT_KEYS}
    assert caps == {"m05.eligibility.apply": "m05.eligibility.cases",
                    "m05.eligibility.create_item": "m05.eligibility.cases",
                    "m05.eligibility.case_view": "m05.followup.read"}


def test_el3a_u02_test_profile_activates_and_maps_three_kinds() -> None:
    import sa_support as sa

    text = e3.el3a_profile_sql()
    assert f"event_type = '{e3.EVENT_TYPE}'" in text and "is_active = true" in text
    for kind in e3.EL_KINDS:
        assert (f"('{e3.EVENT_TYPE}', 1, 'open_kind', '{kind}', '{kind}', '{sa.W_TIMER}',"
                " false)") in text


# --- E3a-U03 service admission ------------------------------------------------------------

class _Ctx:
    """The TenantContext fields the service reads (admission and the E2 attribution)."""

    def __init__(self, kind: str, caps: frozenset[str]) -> None:
        from haloflow.m01.context import PrincipalKind

        self.principal = type("P", (), {"kind": PrincipalKind[kind],
                                        "id": "00000000-0000-4000-8000-00000005a004",
                                        "auth_method": "test"})()
        self.capabilities = caps
        self.execution_id = uuid.uuid4()
        self.request_id = None
        self.purpose = "operations"


class _NoIo:
    """A foundation that fails the row if touched."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"I/O before admission: {name}")


@pytest.mark.parametrize(("kind", "caps", "code"), [
    ("ACTOR", e3.CASE_CAPABILITIES, "PRINCIPAL_NOT_WORKLOAD"),
    ("WORKLOAD", frozenset({"m05.eligibility.cases"}), "CAPABILITIES_INVALID"),
    ("WORKLOAD", e3.CASE_CAPABILITIES | {"m05.eligibility.worker"}, "CAPABILITIES_INVALID"),
])
def test_el3a_u03_admission_before_io(kind: str, caps: frozenset[str], code: str) -> None:
    from haloflow.m05.eligibility_cases import EligibilityCaseRefused, EligibilityCaseService

    service = EligibilityCaseService(_NoIo())
    with pytest.raises(EligibilityCaseRefused) as raised:
        asyncio.run(service.apply_one(_Ctx(kind, caps)))
    assert raised.value.code == code


# --- E3a-U04 / U05 the transaction body (fake gateway, recorded calls) --------------------

def _applied(effect: str) -> dict[str, Any]:
    event = str(uuid.uuid4())
    meta = {"appointment_ref": str(uuid.uuid4()), "case_event_id": event,
            "verification_id": None, "seq": 1, "coverage_version": 1,
            "class": "manual_required" if effect != "none" else None,
            "reason": "nonpriority" if effect != "none" else "appointment_cancelled",
            "effect": effect, "open_kind": "eligibility_manual" if effect == "opened" else "none",
            "record": "note" if effect == "none" else "settlement"}
    if effect != "none":
        meta["verification_id"] = str(uuid.uuid4())
    return {"schema_version": 1, "outcome": "applied", "effect": effect,
            "case_event_id": event, "appointment_ref": meta["appointment_ref"], "seq": 1,
            "case_id": str(uuid.uuid4()) if effect != "none" else None,
            "item_kind": "eligibility_manual" if effect == "opened" else None,
            "evidence": {"event_id": str(uuid.uuid4()), "patient_id": str(uuid.uuid4()),
                         "occurred_at": datetime.now(UTC).isoformat(), "metadata": meta}}


class _Recorder:
    """A foundation stand-in (pins: `run_in_transaction(context, body)` passes a binding
    whose `handle.fetch_all(key, {"command": Jsonb(...)})` returns [(mapping,)], the E2
    pattern; `append_observation_in(binding, observation)`). Records the order of calls.
    `lose_ack` raises ConnectionError after the body completed (commit marker set)."""

    def __init__(self, apply_result: dict[str, Any], *, lose_ack: bool = False) -> None:
        self.apply_result = apply_result
        self.calls: list[str] = []
        self.appended: list[Any] = []
        self.lose_ack = lose_ack
        self.handle = self

    async def run_in_transaction(self, context: Any, body: Any) -> Any:
        result = await body(self)
        if self.lose_ack:
            raise ConnectionError("acknowledgement lost after commit")
        return result

    async def append_observation_in(self, binding: Any, observation: Any) -> None:
        self.calls.append("append")
        self.appended.append(observation)

    async def fetch_all(self, key: str, params: Any = None) -> list[tuple[Any, ...]]:
        self.calls.append(key)
        if key == "m05.eligibility.apply":
            return [(self.apply_result,)]
        return [({"schema_version": 1, "item_id": str(uuid.uuid4()),
                  "created_or_existing": "created"},)]


def _ok_ctx() -> _Ctx:
    return _Ctx("WORKLOAD", e3.CASE_CAPABILITIES)


@pytest.mark.parametrize("effect", ["opened", "noted", "none"])
def test_el3a_u04_evidence_observation_shape(effect: str) -> None:
    from haloflow.m05.eligibility_cases import EligibilityCaseService

    result = _applied(effect)
    rec = _Recorder(result)
    assert asyncio.run(EligibilityCaseService(rec).apply_one(_ok_ctx())) == effect
    (obs,) = rec.appended
    case_event = uuid.UUID(result["case_event_id"])
    assert (obs.event_type, obs.contract_version) == (e3.EVENT_TYPE, 1)
    assert str(obs.event_id) == result["evidence"]["event_id"]
    assert obs.correlation_id == case_event
    assert obs.producer_dedupe_key == f"elcase:{case_event.hex}"
    assert sorted(obs.metadata) == sorted(e3.METADATA_KEYS)
    assert str(obs.patient_id) == result["evidence"]["patient_id"]
    assert obs.source_time_quality == "trusted" and tuple(obs.links) == ()
    for value in obs.metadata.values():
        assert value is None or isinstance(value, (str, int))


@pytest.mark.parametrize(("effect", "order"), [
    ("opened", ["m05.eligibility.apply", "append", "m05.eligibility.create_item"]),
    ("noted", ["m05.eligibility.apply", "append"]),
    ("none", ["m05.eligibility.apply", "append"]),
])
def test_el3a_u05_call_order_in_one_transaction(effect: str, order: list[str]) -> None:
    from haloflow.m05.eligibility_cases import EligibilityCaseService

    rec = _Recorder(_applied(effect))
    asyncio.run(EligibilityCaseService(rec).apply_one(_ok_ctx()))
    assert rec.calls == order


def test_el3a_u05_idle_calls_apply_only() -> None:
    from haloflow.m05.eligibility_cases import EligibilityCaseService

    rec = _Recorder({"schema_version": 1, "outcome": "idle"})
    assert asyncio.run(EligibilityCaseService(rec).apply_one(_ok_ctx())) == "idle"
    assert rec.calls == ["m05.eligibility.apply"] and rec.appended == []


def test_el3a_u05_lost_acknowledgement_is_commit_unknown() -> None:
    from haloflow.m05.eligibility_cases import (
        EligibilityCaseCommitUnknown,
        EligibilityCaseService,
    )

    rec = _Recorder(_applied("opened"), lose_ack=True)
    with pytest.raises(EligibilityCaseCommitUnknown):
        asyncio.run(EligibilityCaseService(rec).apply_one(_ok_ctx()))


# --- E3a-U06 scheduler --------------------------------------------------------------------

def test_el3a_u06_cases_and_case_contexts_both_or_neither() -> None:
    from haloflow.m05.eligibility import EligibilityService, EligibilityWorker
    from haloflow.m05.eligibility_cases import EligibilityCaseService
    from haloflow.m05.eligibility_scheduler import (
        EligibilityScheduler,
        EligibilitySchedulerRefused,
    )
    from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

    service = EligibilityService(_NoIo())
    worker = EligibilityWorker(service, SimulatedClearinghouse(clock=lambda: datetime.now(UTC)))

    async def ctx(_tenant: str) -> Any:
        return None

    cases = EligibilityCaseService(_NoIo())
    for kwargs in ({"cases": cases}, {"case_contexts": ctx},
                   {"cases": object(), "case_contexts": ctx}):
        with pytest.raises(EligibilitySchedulerRefused) as raised:
            EligibilityScheduler(contexts=ctx, service=service, worker=worker,
                                 tenants=("tenant-one",), **kwargs)
        assert raised.value.code == "CASES_INVALID"
    EligibilityScheduler(contexts=ctx, service=service, worker=worker,
                         tenants=("tenant-one",), cases=cases, case_contexts=ctx)


def test_el3a_u06_tenant_report_has_an_apply_phase() -> None:
    from dataclasses import fields

    from haloflow.m05.eligibility_scheduler import TenantReport

    assert [f.name for f in fields(TenantReport)][-3:] == ["run", "expire", "apply"]


# --- E3a-U08 scheduler apply-phase tenant identity (Codex green review, 2026-11-20) -------

def _u08_run(case_tenant: str) -> tuple[Any, list[tuple[str, int]]]:
    from types import SimpleNamespace

    from haloflow.m05.eligibility import EligibilityService, EligibilityWorker
    from haloflow.m05.eligibility_cases import EligibilityCaseService
    from haloflow.m05.eligibility_scheduler import EligibilityScheduler
    from haloflow.m05.simulated_clearinghouse import SimulatedClearinghouse

    drained: list[tuple[str, int]] = []

    class Service(EligibilityService):
        async def due(self, ctx: Any, limit: int = 100) -> list[str]:
            return []

        async def expire(self, ctx: Any, limit: int = 100) -> int:
            return 0

    class Cases(EligibilityCaseService):
        async def drain(self, ctx: Any, limit: int = 100) -> dict[str, int]:
            drained.append((ctx.tenant_id, limit))
            return {"opened": 1, "noted": 0, "none": 0}

    async def contexts(tenant: str) -> Any:
        return SimpleNamespace(tenant_id=tenant)

    async def case_contexts(_tenant: str) -> Any:
        return SimpleNamespace(tenant_id=case_tenant)

    service = Service(_NoIo())
    worker = EligibilityWorker(service, SimulatedClearinghouse(clock=lambda: datetime.now(UTC)))
    scheduler = EligibilityScheduler(contexts=contexts, service=service, worker=worker,
                                     tenants=("tenant-one",), cases=Cases(_NoIo()),
                                     case_contexts=case_contexts)
    (report,) = asyncio.run(scheduler.tick()).tenants
    return report, drained


def test_el3a_u08_apply_refuses_a_case_context_for_another_tenant() -> None:
    """A valid context for tenant B returned while scheduling tenant A is refused before
    drain: nothing is drained, A reports zero apply counts, TENANT_MISMATCH, DEGRADED."""
    report, drained = _u08_run("tenant-two")
    assert drained == []
    assert report.tenant == "tenant-one"
    assert report.apply.status == "TENANT_MISMATCH"
    assert report.apply.counts == {"opened": 0, "noted": 0, "none": 0}
    assert report.code == "DEGRADED"


def test_el3a_u08_control_matched_case_context_drains() -> None:
    report, drained = _u08_run("tenant-one")
    assert drained == [("tenant-one", 100)]
    assert report.apply.status == "ok"
    assert report.apply.counts == {"opened": 1, "noted": 0, "none": 0}
    assert report.code == "COMPLETE"


# --- E3a-U07 ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["eligibility_cases.py", "eligibility_labels.py"])
def test_el3a_u07_no_network_import(name: str) -> None:
    text = (SRC / name).read_text(encoding="utf-8")
    for banned in ("import httpx", "import requests", "import aiohttp", "urllib.request",
                   "import socket", "http.client"):
        assert banned not in text, banned
