"""Startup validation of the loaded contract generation for UC1 part A.

Decisions D3, AL and G1 (Codex-aligned): part A installs only the null-operation
observation route with the ``event_signal`` / ``m02.event.appended`` handoff.
Startup refuses any ACTIVE contract that needs a route part A does not install,
any active observation contract without that signal rule, and, in a production
deployment, any active reserved test family (``m02_test_``, ``m05_test_``), any
active synthetic fixture resolver, or the presence of the test link-target table.

The validator is pure over a snapshot so that it can be tested without a database.

UC1 part B, B2 (Codex ALIGN 20261031T233740Z and 20261031T234337Z):
- The generation gains the M05 intake rows; a PRODUCTION deployment with any intake
  row not active in production refuses ``TEST_INTAKE_PRESENT`` (MC-08). SQL has no
  trusted deployment flag, so this is enforced here, not in ``m05_create_item``.
- ``load_generation`` reads a generation from one tenant schema over a
  caller-supplied connection: one READ ONLY, REPEATABLE READ transaction (a single
  consistent snapshot), schema-qualified identifiers, strict row decoding. Any
  missing required relation, permission failure or malformed row refuses
  ``GENERATION_UNREADABLE``; it never returns an empty accepted generation.

What this is NOT: startup enforcement. No production caller loads and validates a
generation before serving tenant traffic yet; that trusted caller and its
refusal-before-traffic proof are a mandatory B5 obligation (D3 end-to-end open).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.pq import TransactionStatus

from haloflow.m01.resolver import SCHEMA_KEY_PATTERN

__all__ = [
    "ContractRow",
    "IntakeRow",
    "LoadedGeneration",
    "RegistryRefused",
    "ResolverRow",
    "load_generation",
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
class IntakeRow:
    source_event_type: str
    source_contract_version: int
    active_in_production: bool


@dataclass(frozen=True, slots=True)
class LoadedGeneration:
    contracts: tuple[ContractRow, ...]
    resolvers: tuple[ResolverRow, ...]
    test_targets_present: bool
    # Default empty: every part A constructor keeps its meaning (no intake).
    intake_rows: tuple[IntakeRow, ...] = ()


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
        if any(not row.active_in_production for row in generation.intake_rows):
            raise RegistryRefused("TEST_INTAKE_PRESENT")
    for contract in active:
        _check_route(contract)


# ---------------------------------------------------------------------------
# Loader (B2). Read-only; the caller owns the connection and its credentials.
# ---------------------------------------------------------------------------

_REQUIRED_RELATIONS: Final = ("event_types", "event_contracts", "m02_link_resolvers")
_TEST_TARGETS: Final = "m02_test_link_targets"
_INTAKE: Final = "m05_intake_map"
_UNREADABLE: Final = "GENERATION_UNREADABLE"


def _unreadable() -> RegistryRefused:
    return RegistryRefused(_UNREADABLE)


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise _unreadable()
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise _unreadable()
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise _unreadable()
    return value


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        raise _unreadable()
    return value


def _signal_rule(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    rule = _object(value)
    if set(rule) != {"handoff_kind", "signal_type"}:
        raise _unreadable()
    _text(rule["handoff_kind"])
    _text(rule["signal_type"])
    return rule


def _contract(row: tuple[Any, ...]) -> ContractRow:
    if len(row) != 7:
        raise _unreadable()
    return ContractRow(
        event_type=_text(row[0]),
        version=_int(row[1]),
        event_level=_text(row[2]),
        is_active=_bool(row[3]),
        projection_rule_id=_optional_text(row[4]),
        signal_rule=_signal_rule(row[5]),
        value_constraints=_object(row[6]),
    )


def _resolver(row: tuple[Any, ...]) -> ResolverRow:
    if len(row) != 3:
        raise _unreadable()
    return ResolverRow(
        resolver_id=_text(row[0]), resolver_kind=_text(row[1]), is_active=_bool(row[2])
    )


def _intake(row: tuple[Any, ...]) -> IntakeRow:
    if len(row) != 3:
        raise _unreadable()
    return IntakeRow(
        source_event_type=_text(row[0]),
        source_contract_version=_int(row[1]),
        active_in_production=_bool(row[2]),
    )


async def load_generation(connection: AsyncConnection[Any], schema_key: str) -> LoadedGeneration:
    """Read one tenant's loaded generation in a single READ ONLY snapshot.

    The caller supplies an existing, idle connection; nothing here sources
    credentials, changes role or infers the deployment. ``SCHEMA_KEY_INVALID`` for a
    key outside the tenant schema grammar; ``GENERATION_UNREADABLE`` for anything
    that cannot be read completely and decoded strictly. The intake table may be
    absent only when the schema has no ``m05_*`` relation at all (a pre-M05
    generation).
    """

    if type(schema_key) is not str or not SCHEMA_KEY_PATTERN.fullmatch(schema_key):
        raise RegistryRefused("SCHEMA_KEY_INVALID")
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise _unreadable()
    schema = sql.Identifier(schema_key)
    try:
        async with connection.transaction():
            # Must be the transaction's first statement; the snapshot is taken by the
            # next one, and every read below shares it.
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            relations = {
                str(name)
                for (name,) in await (
                    await connection.execute(
                        "SELECT c.relname FROM pg_catalog.pg_class AS c"
                        " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v')",
                        (schema_key,),
                    )
                ).fetchall()
            }
            if not set(_REQUIRED_RELATIONS) <= relations:
                raise _unreadable()
            m05_present = any(name.startswith("m05_") for name in relations)
            if m05_present and _INTAKE not in relations:
                raise _unreadable()
            contract_rows = await (
                await connection.execute(
                    sql.SQL(
                        "SELECT c.event_type, c.version, c.event_level, t.is_active,"
                        " c.projection_rule_id, c.signal_rule, c.value_constraints"
                        " FROM {}.event_contracts AS c JOIN {}.event_types AS t"
                        " ON t.event_type = c.event_type ORDER BY c.event_type, c.version"
                    ).format(schema, schema)
                )
            ).fetchall()
            resolver_rows = await (
                await connection.execute(
                    sql.SQL(
                        "SELECT resolver_id, resolver_kind, is_active"
                        " FROM {}.m02_link_resolvers ORDER BY resolver_id"
                    ).format(schema)
                )
            ).fetchall()
            intake_rows: list[tuple[Any, ...]] = []
            if _INTAKE in relations:
                intake_rows = await (
                    await connection.execute(
                        sql.SQL(
                            "SELECT source_event_type, source_contract_version,"
                            " active_in_production FROM {}.m05_intake_map"
                            " ORDER BY source_event_type, source_contract_version,"
                            " active_in_production"
                        ).format(schema)
                    )
                ).fetchall()
    except psycopg.Error:
        raise _unreadable() from None
    return LoadedGeneration(
        contracts=tuple(_contract(tuple(row)) for row in contract_rows),
        resolvers=tuple(_resolver(tuple(row)) for row in resolver_rows),
        test_targets_present=_TEST_TARGETS in relations,
        intake_rows=tuple(_intake(tuple(row)) for row in intake_rows),
    )
