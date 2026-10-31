"""UC1 part A tranche 2b — startup registry validation (unit layer, no database).

Rows: PA-13 (every excluded route refused), PA-46 (reserved test families active in
a production-loaded generation), PA-46b (synthetic resolver dispatch / test target
table in production), plus "every active observation contract declares the signal
rule" (decision TB-2, Codex 95ae5bfd).

Frozen interface (`haloflow.m02.registry_validation`):
- `ContractRow(event_type, version, event_level, is_active, projection_rule_id,
  signal_rule, value_constraints)`; `ResolverRow(resolver_id, resolver_kind,
  is_active)`; `LoadedGeneration(contracts, resolvers, test_targets_present)`.
- `validate_loaded_generation(generation, *, deployment)` where deployment is
  "production" or "non_production"; returns None or raises `RegistryRefused`
  with `reason_code`.
Per-test import keeps each row individually visible at the baseline (class S).
"""

from typing import Any

import pytest

_OBS = "m02_test_observation_received"
_SIGNAL = {"handoff_kind": "event_signal", "signal_type": "m02.event.appended"}
_NULL_OP = {"operation_id": {"must_be_null": True}, "synthetic_intent": {"forbidden": True}}


def _v() -> Any:
    import importlib

    return importlib.import_module("haloflow.m02.registry_validation")


def _generation(
    v: Any,
    *,
    contracts: list[Any] | None = None,
    resolvers: list[Any] | None = None,
    targets: bool = True,
) -> Any:
    base = [v.ContractRow(_OBS, 1, "observation", True, None, _SIGNAL, _NULL_OP)]
    res = [
        v.ResolverRow("m02_test_subject_v1", "synthetic_fixture", True),
        v.ResolverRow("m02_test_source_v1", "synthetic_fixture", True),
        v.ResolverRow("m02_test_provider_v1", "synthetic_fixture", False),
    ]
    return v.LoadedGeneration(
        contracts=tuple(base + (contracts or [])),
        resolvers=tuple(res if resolvers is None else resolvers),
        test_targets_present=targets,
    )


def test_valid_non_production_profile_accepted() -> None:
    v = _v()
    assert v.validate_loaded_generation(_generation(v), deployment="non_production") is None


@pytest.mark.parametrize(
    ("label", "row_args", "code"),
    [
        (
            "projection_rule",
            ("x_obs_proj", 1, "observation", True, "rule_1", _SIGNAL, _NULL_OP),
            "ROUTE_NOT_INSTALLED",
        ),
        (
            "m05_handoff",
            (
                "x_obs_m05",
                1,
                "observation",
                True,
                None,
                {"handoff_kind": "ordinary_failure", "signal_type": None},
                _NULL_OP,
            ),
            "ROUTE_NOT_INSTALLED",
        ),
        (
            "m06_handoff",
            (
                "x_obs_m06",
                1,
                "observation",
                True,
                None,
                {"handoff_kind": "reconciliation", "signal_type": None},
                _NULL_OP,
            ),
            "ROUTE_NOT_INSTALLED",
        ),
        (
            "integrity_alert",
            (
                "x_obs_ia",
                1,
                "observation",
                True,
                None,
                {"handoff_kind": "integrity_alert", "signal_type": "m02.integrity.incomplete"},
                _NULL_OP,
            ),
            "ROUTE_NOT_INSTALLED",
        ),
        (
            "unknown_signal",
            (
                "x_obs_sig",
                1,
                "observation",
                True,
                None,
                {"handoff_kind": "event_signal", "signal_type": "m02.other"},
                _NULL_OP,
            ),
            "ROUTE_NOT_INSTALLED",
        ),
        (
            "requires_operation",
            (
                "x_obs_op",
                1,
                "observation",
                True,
                None,
                _SIGNAL,
                {"operation_id": {"must_be_null": False}, "synthetic_intent": {"forbidden": True}},
            ),
            "ROUTE_NOT_INSTALLED",
        ),
        (
            "non_observation_level",
            ("x_req", 1, "intent", True, None, _SIGNAL, _NULL_OP),
            "ROUTE_NOT_INSTALLED",
        ),
        (
            "no_signal_rule",
            ("x_obs_nosig", 1, "observation", True, None, None, _NULL_OP),
            "SIGNAL_RULE_REQUIRED",
        ),
    ],
)
def test_pa13_active_contract_needing_uninstalled_route_refused(
    label: str, row_args: tuple[Any, ...], code: str
) -> None:
    v = _v()
    generation = _generation(v, contracts=[v.ContractRow(*row_args)])
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(generation, deployment="non_production")
    assert caught.value.reason_code == code, label


def test_pa13_inactive_contract_needing_uninstalled_route_is_ignored() -> None:
    v = _v()
    row = v.ContractRow("x_obs_proj", 1, "observation", False, "rule_1", _SIGNAL, _NULL_OP)
    assert (
        v.validate_loaded_generation(_generation(v, contracts=[row]), deployment="non_production")
        is None
    )


@pytest.mark.parametrize("prefix", ["m02_test_", "m05_test_"])
def test_pa46_reserved_test_family_active_in_production_refused(prefix: str) -> None:
    v = _v()
    row = v.ContractRow(f"{prefix}reply_received", 1, "observation", True, None, _SIGNAL, _NULL_OP)
    generation = v.LoadedGeneration(contracts=(row,), resolvers=(), test_targets_present=False)
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(generation, deployment="production")
    assert caught.value.reason_code == "TEST_FAMILY_ACTIVE_IN_PRODUCTION"


def test_pa46b_synthetic_resolver_active_in_production_refused() -> None:
    v = _v()
    generation = v.LoadedGeneration(
        contracts=(),
        resolvers=(v.ResolverRow("m02_test_subject_v1", "synthetic_fixture", True),),
        test_targets_present=False,
    )
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(generation, deployment="production")
    assert caught.value.reason_code == "SYNTHETIC_DISPATCH_IN_PRODUCTION"


def test_pa46b_test_targets_table_in_production_refused() -> None:
    v = _v()
    generation = v.LoadedGeneration(contracts=(), resolvers=(), test_targets_present=True)
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(generation, deployment="production")
    assert caught.value.reason_code == "SYNTHETIC_DISPATCH_IN_PRODUCTION"


def test_pa46_inactive_test_family_in_production_accepted() -> None:
    v = _v()
    row = v.ContractRow(_OBS, 1, "observation", False, None, _SIGNAL, _NULL_OP)
    generation = v.LoadedGeneration(
        contracts=(row,),
        resolvers=(v.ResolverRow("m02_test_subject_v1", "synthetic_fixture", False),),
        test_targets_present=False,
    )
    assert v.validate_loaded_generation(generation, deployment="production") is None


def test_unknown_deployment_label_refused() -> None:
    v = _v()
    with pytest.raises(ValueError):
        v.validate_loaded_generation(_generation(v), deployment="staging")
