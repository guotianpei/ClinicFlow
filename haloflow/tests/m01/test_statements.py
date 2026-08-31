import pytest

from haloflow.m01.errors import RepositoryStatementRejected
from haloflow.m01.statements import (
    StatementMode,
    TenantStatement,
    TenantStatementCatalog,
    _build_statement_catalog,
    build_statement_catalog,
)


def test_application_cannot_construct_statement_or_catalog() -> None:
    with pytest.raises(RepositoryStatementRejected) as statement_error:
        TenantStatement("m01_test.read", StatementMode.READ, "probe:read", "SELECT 1")
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
        _build_statement_catalog({"m01_test.unsafe": (StatementMode.READ, "probe:read", query)})


def test_public_builder_applies_the_same_safety_validation() -> None:
    """TC-D4. The regression runs over the supported entry point, not only the private one."""

    with pytest.raises(RepositoryStatementRejected):
        build_statement_catalog(
            {"m01_test.unsafe": (StatementMode.READ, "probe:read", "SELECT * FROM shared.tenants")}
        )


def test_catalog_is_immutable_and_rejects_unknown_keys() -> None:
    catalog = _build_statement_catalog(
        {
            "m01_test.safe": (
                StatementMode.READ,
                "probe:read",
                "SELECT value FROM probe WHERE id = %s",
            )
        }
    )

    assert catalog.resolve("m01_test.safe").mode is StatementMode.READ
    with pytest.raises(RepositoryStatementRejected) as error:
        catalog.resolve("SELECT * FROM tenant_bbbbbbbb.probe")
    assert error.value.reason_code == "STATEMENT_NOT_REGISTERED"


def test_public_builder_composes_several_module_definition_sets() -> None:
    """TC-D1."""

    compiled = build_statement_catalog(
        {"m01_test.read": (StatementMode.READ, "probe:read", "SELECT 1")},
        {"m02_test.write": (StatementMode.WRITE, "probe:write", "INSERT INTO probe VALUES (%s)")},
    )

    assert compiled.catalog.keys() == ("m01_test.read", "m02_test.write")


def test_duplicate_key_across_module_sets_fails_at_composition() -> None:
    """TC-D2. Startup, not first use."""

    with pytest.raises(RepositoryStatementRejected) as error:
        build_statement_catalog(
            {"m01_test.read": (StatementMode.READ, "probe:read", "SELECT 1")},
            {"m01_test.read": (StatementMode.READ, "probe:read", "SELECT 2")},
        )
    assert error.value.reason_code == "DUPLICATE_STATEMENT_KEY"


@pytest.mark.parametrize("key", ["probe.read", "read", "x01.read", "m1.read", "m011.read"])
def test_keys_without_a_module_prefix_are_rejected(key: str) -> None:
    """TC-D3."""

    with pytest.raises(RepositoryStatementRejected) as error:
        build_statement_catalog({key: (StatementMode.READ, "probe:read", "SELECT 1")})
    assert error.value.reason_code == "STATEMENT_KEY_NOT_MODULE_PREFIXED"


@pytest.mark.parametrize("key", ["m01.read", "m02.read", "m01_test.read", "m12_test.read"])
def test_module_and_test_namespaces_are_both_accepted(key: str) -> None:
    """TC-D3 / D10. `m01_test.` is the reserved non-production family."""

    compiled = build_statement_catalog({key: (StatementMode.READ, "probe:read", "SELECT 1")})
    assert compiled.catalog.keys() == (key,)


def test_write_capabilities_are_derived_from_write_statements() -> None:
    """TC-D6, including the read-only case that contributes none."""

    compiled = build_statement_catalog(
        {
            "m01_test.read": (StatementMode.READ, "probe:read", "SELECT 1"),
            "m01_test.write": (StatementMode.WRITE, "probe:write", "INSERT INTO probe VALUES (%s)"),
            "m01_test.write_two": (
                StatementMode.WRITE,
                "other:write",
                "UPDATE probe SET marker = %s",
            ),
        }
    )
    assert compiled.write_capabilities == frozenset({"probe:write", "other:write"})

    read_only = build_statement_catalog(
        {"m01_test.read": (StatementMode.READ, "probe:read", "SELECT 1")}
    )
    assert read_only.write_capabilities == frozenset()


def test_catalog_exposes_no_runtime_registration_path() -> None:
    """TC-D7."""

    compiled = build_statement_catalog(
        {"m01_test.read": (StatementMode.READ, "probe:read", "SELECT 1")}
    )

    assert not hasattr(compiled.catalog, "register")
    assert not hasattr(compiled.catalog, "add")
    with pytest.raises(AttributeError):
        compiled.catalog.statements = ()  # type: ignore[misc]
