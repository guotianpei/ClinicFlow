import pytest

from haloflow.m01.errors import RepositoryStatementRejected
from haloflow.m01.statements import (
    StatementMode,
    TenantStatement,
    TenantStatementCatalog,
    _build_statement_catalog,
)


def test_application_cannot_construct_statement_or_catalog() -> None:
    with pytest.raises(RepositoryStatementRejected) as statement_error:
        TenantStatement("probe.read", StatementMode.READ, "probe:read", "SELECT 1")
    assert statement_error.value.reason_code == "UNTRUSTED_STATEMENT"

    with pytest.raises(RepositoryStatementRejected) as catalog_error:
        TenantStatementCatalog([])
    assert catalog_error.value.reason_code == "UNTRUSTED_CATALOG"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM shared.tenants",
        "SELECT * FROM public.example",
        "SELECT * FROM tenant_aaaaaaaa.example",
        'SELECT * FROM "tenant_aaaaaaaa".example',
        "SELECT set_config('search_path', 'tenant_bbbbbbbb', true)",
        "SELECT set_config ('search_path', 'tenant_bbbbbbbb', true)",
        "SELECT pg_catalog.set_config\n('search_path', 'tenant_bbbbbbbb', true)",
        "RESET search_path",
        "SET ROLE haloflow_owner",
        "SET SESSION AUTHORIZATION haloflow_owner",
        "SELECT 1 /* hidden command */",
        "SELECT 1; RESET search_path",
    ],
)
def test_catalog_rejects_unsafe_statement_definitions(query: str) -> None:
    with pytest.raises(RepositoryStatementRejected):
        _build_statement_catalog({"probe.unsafe": (StatementMode.READ, "probe:read", query)})


def test_catalog_is_immutable_and_rejects_unknown_keys() -> None:
    catalog = _build_statement_catalog(
        {
            "probe.safe": (
                StatementMode.READ,
                "probe:read",
                "SELECT value FROM probe WHERE id = %s",
            )
        }
    )

    assert catalog.resolve("probe.safe").mode is StatementMode.READ
    with pytest.raises(RepositoryStatementRejected) as error:
        catalog.resolve("SELECT * FROM tenant_bbbbbbbb.probe")
    assert error.value.reason_code == "STATEMENT_NOT_REGISTERED"
