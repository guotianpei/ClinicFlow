"""M01-owned catalogue of fixed tenant SQL statements.

Application callbacks submit opaque statement keys and bound values. They never
submit SQL text, identifiers, session commands, or search-path changes.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, field
from enum import StrEnum
from types import MappingProxyType

from haloflow.m01.errors import RepositoryStatementRejected

_STATEMENT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{2,127}$")
_TENANT_SCHEMA_QUALIFIER = re.compile(r"\btenant_[a-z0-9]{8,32}\s*\.", re.IGNORECASE)
_CATALOG_ISSUER = object()


class StatementMode(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class TenantStatement:
    """A fixed, security-reviewed statement stored inside M01."""

    key: str
    mode: StatementMode
    query: str = field(repr=False)
    _issuer: InitVar[object | None] = None

    def __post_init__(self, _issuer: object | None) -> None:
        if _issuer is not _CATALOG_ISSUER:
            raise RepositoryStatementRejected(reason_code="UNTRUSTED_STATEMENT")
        if not _STATEMENT_KEY_PATTERN.fullmatch(self.key) or not self.query.strip():
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
            or "set_config(" in normalized_query
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


def _build_statement_catalog(
    definitions: Mapping[str, tuple[StatementMode, str]],
) -> TenantStatementCatalog:
    """Internal construction hook used by M01-owned repositories and tests."""

    statements = tuple(
        TenantStatement(key, mode, query, _issuer=_CATALOG_ISSUER)
        for key, (mode, query) in definitions.items()
    )
    return TenantStatementCatalog(statements, _issuer=_CATALOG_ISSUER)
