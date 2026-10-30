"""Startup validation of the loaded contract generation for UC1 part A.

Decisions D3, AL and G1 (Codex-aligned): part A installs only the null-operation
observation route with the ``event_signal`` / ``m02.event.appended`` handoff.
Startup refuses any ACTIVE contract that needs a route part A does not install,
any active observation contract without that signal rule, and, in a production
deployment, any active reserved test family (``m02_test_``, ``m05_test_``), any
active synthetic fixture resolver, or the presence of the test link-target table.

The function is pure over a snapshot so that it can be tested without a database;
the caller reads the snapshot from the loaded generation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "ContractRow",
    "LoadedGeneration",
    "RegistryRefused",
    "ResolverRow",
    "validate_loaded_generation",
]

_DEPLOYMENTS: Final = frozenset({"production", "non_production"})
_TEST_FAMILIES: Final = ("m02_test_", "m05_test_")
_SUPPORTED_SIGNAL: Final = ("event_signal", "m02.event.appended")
_SYNTHETIC_KIND: Final = "synthetic_fixture"


class RegistryRefused(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ContractRow:
    event_type: str
    version: int
    event_level: str
    is_active: bool
    projection_rule_id: str | None
    signal_rule: Mapping[str, Any] | None
    value_constraints: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ResolverRow:
    resolver_id: str
    resolver_kind: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class LoadedGeneration:
    contracts: tuple[ContractRow, ...]
    resolvers: tuple[ResolverRow, ...]
    test_targets_present: bool


def _operation_must_be_null(constraints: Mapping[str, Any]) -> bool:
    operation = constraints.get("operation_id")
    return isinstance(operation, Mapping) and operation.get("must_be_null") is True


def _check_route(contract: ContractRow) -> None:
    if contract.event_level != "observation":
        raise RegistryRefused("ROUTE_NOT_INSTALLED")
    if contract.projection_rule_id is not None:
        raise RegistryRefused("ROUTE_NOT_INSTALLED")
    if not _operation_must_be_null(contract.value_constraints):
        raise RegistryRefused("ROUTE_NOT_INSTALLED")
    if contract.signal_rule is None:
        raise RegistryRefused("SIGNAL_RULE_REQUIRED")
    pair = (contract.signal_rule.get("handoff_kind"), contract.signal_rule.get("signal_type"))
    if pair != _SUPPORTED_SIGNAL:
        raise RegistryRefused("ROUTE_NOT_INSTALLED")


def validate_loaded_generation(generation: LoadedGeneration, *, deployment: str) -> None:
    """Return None, or raise ``RegistryRefused``; ``ValueError`` for an unknown deployment."""

    if deployment not in _DEPLOYMENTS:
        raise ValueError("unknown deployment label")
    active = [contract for contract in generation.contracts if contract.is_active]
    if deployment == "production":
        if any(contract.event_type.startswith(_TEST_FAMILIES) for contract in active):
            raise RegistryRefused("TEST_FAMILY_ACTIVE_IN_PRODUCTION")
        if generation.test_targets_present or any(
            resolver.is_active and resolver.resolver_kind == _SYNTHETIC_KIND
            for resolver in generation.resolvers
        ):
            raise RegistryRefused("SYNTHETIC_DISPATCH_IN_PRODUCTION")
    for contract in active:
        _check_route(contract)
