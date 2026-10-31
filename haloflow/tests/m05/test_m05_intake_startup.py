"""UC1 part B, tranche B2 — MC-08: a production generation refuses test intake rows.

Design delta v1 Δ4, ALIGNed by Codex 20261031T233740Z: SQL has no trusted
deployment flag, so the production-generation refusal lives at the D3 startup
validator (`haloflow.m02.registry_validation`). The validator gains intake rows and
one rule: in a PRODUCTION deployment, any intake row with `active_in_production =
false` refuses with `TEST_INTAKE_PRESENT`.

These rows are the PURE validator half and say nothing about loading. The loading
half (a real generation loaded from a PG17 tenant reaching the same refusal) waits
on Codex's ruling on relay 20261031T233833Z: no generation loader exists in src
today (pre-existing part A gap, measured at 3e24500).

Interface pinned here (new): `IntakeRow(source_event_type, source_contract_version,
active_in_production)`; `LoadedGeneration.intake_rows` defaulting to `()` so every
existing part A constructor keeps its meaning (existing validator contracts
preserved; tests/m02/test_m02_part_a_registry_units.py is not edited).
"""

from __future__ import annotations

from typing import Any

import pytest


def _v() -> Any:
    from haloflow.m02 import registry_validation

    return registry_validation


def _generation(v: Any, *intake: Any) -> Any:
    return v.LoadedGeneration(
        contracts=(), resolvers=(), test_targets_present=False, intake_rows=tuple(intake)
    )


def test_mc08_production_with_test_intake_row_is_refused() -> None:
    v = _v()
    row = v.IntakeRow("m02_test_observation_received", 1, False)
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(_generation(v, row), deployment="production")
    assert caught.value.reason_code == "TEST_INTAKE_PRESENT"


def test_mc08_non_production_with_test_intake_row_is_accepted() -> None:
    v = _v()
    row = v.IntakeRow("m02_test_observation_received", 1, False)
    assert v.validate_loaded_generation(_generation(v, row), deployment="non_production") is None


def test_mc08_production_with_empty_or_production_intake_is_accepted() -> None:
    v = _v()
    assert v.validate_loaded_generation(_generation(v), deployment="production") is None
    live = v.IntakeRow("m07_reply_received", 1, True)
    assert v.validate_loaded_generation(_generation(v, live), deployment="production") is None


def test_mc08_one_test_row_among_production_rows_still_refuses() -> None:
    v = _v()
    rows = (
        v.IntakeRow("m07_reply_received", 1, True),
        v.IntakeRow("m02_test_observation_received", 1, False),
    )
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(_generation(v, *rows), deployment="production")
    assert caught.value.reason_code == "TEST_INTAKE_PRESENT"


def test_mc08_existing_constructor_keeps_its_meaning() -> None:
    """The three-argument part A constructor still works and means 'no intake'."""

    v = _v()
    generation = v.LoadedGeneration(contracts=(), resolvers=(), test_targets_present=False)
    assert generation.intake_rows == ()
    assert v.validate_loaded_generation(generation, deployment="production") is None
