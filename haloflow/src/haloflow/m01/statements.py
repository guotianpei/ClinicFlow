"""M01-owned catalogue of fixed tenant SQL statements.

Application callbacks submit opaque statement keys and bound values. They never
submit SQL text, identifiers, session commands, or search-path changes.

Composition is startup-only (ADR-011 D-11.18, B2). Each module owns its fixed
definitions in its own ``haloflow.mNN.statements``; ``build_statement_catalog``
composes approved sets into one immutable catalogue and derives the
write-capability set from the composed WRITE statements, so the two cannot
drift. There is no runtime registration path.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from haloflow.m01.errors import RepositoryStatementRejected

_STATEMENT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{2,127}$")
# B2.3: keys are module-prefixed. `m01_test.` is the reserved non-production
# family, deliberately parallel to ADR-011 D-11.14's `m02_test_` event types but
# spelled with the dotted separator this key grammar uses.
_MODULE_PREFIX_PATTERN = re.compile(r"^m\d{2}(_test)?\.")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_TENANT_SCHEMA_QUALIFIER = re.compile(r"\btenant_[a-z0-9]{8,32}\s*\.", re.IGNORECASE)
_SET_CONFIG_CALL = re.compile(r"\bset_config\s*\(", re.IGNORECASE)
_CATALOG_ISSUER = object()

StatementDefinitions = Mapping[str, tuple["StatementMode", str, str]]


class StatementMode(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class TenantStatement:
    """A fixed, security-reviewed statement stored inside M01."""

    key: str
    mode: StatementMode
    required_capability: str
    query: str = field(repr=False)
    _issuer: InitVar[object | None] = None

    def __post_init__(self, _issuer: object | None) -> None:
        if _issuer is not _CATALOG_ISSUER:
            raise RepositoryStatementRejected(reason_code="UNTRUSTED_STATEMENT")
        if (
            not _STATEMENT_KEY_PATTERN.fullmatch(self.key)
            or not _CAPABILITY_PATTERN.fullmatch(self.required_capability)
            or not self.query.strip()
        ):
            raise RepositoryStatementRejected(reason_code="STATEMENT_DEFINITION_INVALID")
        if ";" in self.query:
            raise RepositoryStatementRejected(reason_code="MULTI_STATEMENT_PROHIBITED")
        normalized_query = self.query.casefold()
        if (
            '"' in self.query
            or "--" in self.query
            or "/*" in self.query
            or "shared." in normalized_query
            or "public." in normalized_query
            or _SET_CONFIG_CALL.search(self.query)
            or re.match(r"^\s*(?:set|reset|prepare|deallocate)\b", normalized_query)
            or _TENANT_SCHEMA_QUALIFIER.search(self.query)
        ):
            raise RepositoryStatementRejected(reason_code="UNSAFE_STATEMENT_DEFINITION")


class TenantStatementCatalog:
    """Immutable statement lookup; construction is restricted to M01."""

    __slots__ = ("__statements",)

    def __init__(
        self,
        statements: Iterable[TenantStatement],
        *,
        _issuer: object | None = None,
    ) -> None:
        if _issuer is not _CATALOG_ISSUER:
            raise RepositoryStatementRejected(reason_code="UNTRUSTED_CATALOG")
        statement_items = tuple(statements)
        by_key = {statement.key: statement for statement in statement_items}
        if len(by_key) != len(statement_items):
            raise RepositoryStatementRejected(reason_code="DUPLICATE_STATEMENT_KEY")
        self.__statements = MappingProxyType(by_key)

    def resolve(self, key: str) -> TenantStatement:
        try:
            return self.__statements[key]
        except KeyError:
            raise RepositoryStatementRejected(reason_code="STATEMENT_NOT_REGISTERED") from None

    def keys(self) -> tuple[str, ...]:
        """Registered keys, sorted. Used by the CI manifest check."""

        return tuple(sorted(self.__statements))

    def statements(self) -> tuple[TenantStatement, ...]:
        """Every registered statement, ordered by key. Used by the manifest check."""

        return tuple(self.__statements[key] for key in sorted(self.__statements))


@dataclass(frozen=True, slots=True)
class CompiledCatalog:
    """A composed, frozen catalogue whose write capabilities are derived from it.

    ``write_capabilities`` is a property computed from the catalogue's WRITE
    statements, not a stored field. B2.9 requires that the two cannot drift; a
    stored field could be supplied inconsistently by any caller, so there is
    nothing to supply.
    """

    catalog: TenantStatementCatalog

    @property
    def write_capabilities(self) -> frozenset[str]:
        return frozenset(
            statement.required_capability
            for statement in self.catalog.statements()
            if statement.mode is StatementMode.WRITE
        )


def _build_statement_catalog(definitions: StatementDefinitions) -> TenantStatementCatalog:
    """Internal construction hook used by M01-owned code and by tests."""

    statements = tuple(
        TenantStatement(key, mode, capability, query, _issuer=_CATALOG_ISSUER)
        for key, (mode, capability, query) in definitions.items()
    )
    return TenantStatementCatalog(statements, _issuer=_CATALOG_ISSUER)


def build_statement_catalog(*definition_sets: StatementDefinitions) -> CompiledCatalog:
    """Compose approved module definition sets into one immutable catalogue.

    Startup-only. Duplicate keys across sets fail here rather than at first use,
    and every key must carry a module prefix. This is the only supported entry
    point; ``_build_statement_catalog`` and ``_CATALOG_ISSUER`` remain private.
    """

    merged: dict[str, tuple[StatementMode, str, str]] = {}
    for definitions in definition_sets:
        for key, definition in definitions.items():
            if not isinstance(key, str) or not _MODULE_PREFIX_PATTERN.match(key):
                raise RepositoryStatementRejected(reason_code="STATEMENT_KEY_NOT_MODULE_PREFIXED")
            if key in merged:
                raise RepositoryStatementRejected(reason_code="DUPLICATE_STATEMENT_KEY")
            merged[key] = definition

    return CompiledCatalog(catalog=_build_statement_catalog(merged))


# M01 owns no tenant-schema SQL of its own: its only shared-control read runs on
# the control connection in `control_store`, outside this catalogue. The empty
# set exists so the composition root has an M01 entry to compose and so the
# convention is visible where a future M01 statement would be added.
#
# Production definition sets are named `M<NN>_STATEMENTS` and live only in
# `haloflow.mNN.statements`. Both facts are enforced by repository-control tests,
# because the statement-catalogue manifest is only a review gate if there is
# exactly one composition path it can pin.
M01_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({})
