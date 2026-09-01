"""Unit tests for the provisioning package that need no database.

TC-E21 and the trusted-construction controls live here; everything that needs a
real catalogue, a real grant or a real lock is in `test_provisioning_postgres.py`,
because a privilege that is asserted about rather than exercised is exactly the
gap finding F-3 was raised over.
"""

from uuid import UUID, uuid4

import pytest

from haloflow.composition import (
    APPROVED_TENANT_MIGRATIONS,
    build_production_tenant_migrations,
)
from haloflow.m01.errors import MigrationUnitRejected, ProvisioningFailed
from haloflow.m01.provisioning import (
    SanitizedErrorCode,
    TenantMigrationRegistry,
    TenantMigrationUnit,
    tenant_lock_key,
)
from haloflow.m01.provisioning.codes import LEDGER_ERROR_CODE_MAX_LENGTH
from haloflow.m01.provisioning.provisioner import ProvisioningRequest, _validate_request
from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry

VALID_TEMPLATE = "CREATE TABLE {schema}.thing (id integer PRIMARY KEY);"
TEST_UNITS = {"t001_test_probe": VALID_TEMPLATE}


def _registry(*definition_sets: dict[str, str], **kwargs: bool) -> TenantMigrationRegistry:
    return build_tenant_migration_registry(*definition_sets, **kwargs)


# --- trusted construction --------------------------------------------------


def test_a_migration_unit_cannot_be_constructed_outside_the_builder() -> None:
    with pytest.raises(MigrationUnitRejected) as error:
        TenantMigrationUnit("t001_rogue", VALID_TEMPLATE)
    assert error.value.reason_code == "UNTRUSTED_MIGRATION_UNIT"


def test_a_registry_cannot_be_constructed_outside_the_builder() -> None:
    with pytest.raises(MigrationUnitRejected) as error:
        TenantMigrationRegistry(())
    assert error.value.reason_code == "UNTRUSTED_MIGRATION_REGISTRY"


# --- TC-E21: test units cannot enter the production registry ---------------


def test_a_test_only_unit_is_refused_by_the_default_builder() -> None:
    """TC-E21."""

    with pytest.raises(MigrationUnitRejected) as error:
        _registry(TEST_UNITS)
    assert error.value.reason_code == "TEST_MIGRATION_UNIT_REJECTED"


def test_a_test_only_unit_is_accepted_only_when_explicitly_allowed() -> None:
    registry = _registry(TENANT_MIGRATIONS, TEST_UNITS, allow_test_units=True)

    assert registry.migration_ids == ("t001_m01_baseline", "t001_test_probe")


def test_the_production_registry_contains_no_test_units() -> None:
    """TC-E21, over the real composition root rather than a constructed example."""

    registry = build_production_tenant_migrations()

    assert registry.migration_ids == ("t001_m01_baseline",)
    assert not any(unit.is_test_unit for unit in registry)
    assert APPROVED_TENANT_MIGRATIONS == (TENANT_MIGRATIONS,)


def test_the_production_baseline_targets_the_supported_schema_version() -> None:
    """R-E10/R-E11: `t001` means the M01 infrastructure baseline, version 1."""

    assert build_production_tenant_migrations().target_version == 1


# --- unit grammar and rendering -------------------------------------------


@pytest.mark.parametrize(
    "migration_id",
    [
        "baseline",  # no tNNN prefix
        "t1_baseline",  # too few digits
        "t0001_baseline",  # too many
        "t001-baseline",  # wrong separator
        "t001_Baseline",  # uppercase
        "t001_",  # empty tail
    ],
)
def test_a_migration_id_outside_the_grammar_is_refused(migration_id: str) -> None:
    with pytest.raises(MigrationUnitRejected) as error:
        _registry({migration_id: VALID_TEMPLATE})
    assert error.value.reason_code == "MIGRATION_ID_INVALID"


def test_a_template_that_names_no_schema_is_refused() -> None:
    """Unqualified DDL would land wherever search_path happened to point."""

    with pytest.raises(MigrationUnitRejected) as error:
        _registry({"t001_unscoped": "CREATE TABLE thing (id integer);"})
    assert error.value.reason_code == "MIGRATION_TEMPLATE_UNSCOPED"


def test_an_empty_template_is_refused() -> None:
    with pytest.raises(MigrationUnitRejected) as error:
        _registry({"t001_empty": "   \n  "})
    assert error.value.reason_code == "MIGRATION_TEMPLATE_EMPTY"


def test_a_duplicate_migration_id_across_sets_fails_at_composition() -> None:
    with pytest.raises(MigrationUnitRejected) as error:
        _registry({"t001_a": VALID_TEMPLATE}, {"t001_a": VALID_TEMPLATE})
    assert error.value.reason_code == "DUPLICATE_MIGRATION_ID"


def test_units_are_ordered_by_id_regardless_of_composition_order() -> None:
    registry = _registry(
        {"t003_c": VALID_TEMPLATE},
        {"t001_a": VALID_TEMPLATE},
        {"t002_b": VALID_TEMPLATE},
    )

    assert registry.migration_ids == ("t001_a", "t002_b", "t003_c")
    assert registry.target_version == 3


def test_render_refuses_a_schema_key_outside_the_pattern() -> None:
    """The identifier cannot be a bound parameter, so the pattern is the control."""

    unit = _registry({"t001_a": VALID_TEMPLATE}).units[0]

    for hostile in ("tenant_aaaaaaaa; DROP SCHEMA shared", "shared", "public", "tenant_SHORT"):
        with pytest.raises(MigrationUnitRejected) as error:
            unit.render(hostile)
        assert error.value.reason_code == "SCHEMA_KEY_INVALID"


def test_render_substitutes_every_occurrence_of_the_placeholder() -> None:
    unit = _registry(
        {"t001_a": "CREATE TABLE {schema}.a (id int); CREATE INDEX i ON {schema}.a (id);"}
    ).units[0]

    rendered = unit.render("tenant_aaaaaaaa")

    assert "{schema}" not in rendered
    assert rendered.count("tenant_aaaaaaaa") == 2


# --- checksums -------------------------------------------------------------


def test_the_checksum_is_stable_under_reindentation_but_not_under_edits() -> None:
    spaced = _registry({"t001_a": "CREATE TABLE {schema}.a\n    (id integer);"}).units[0]
    tight = _registry({"t001_a": "CREATE TABLE {schema}.a (id integer);"}).units[0]
    edited = _registry({"t001_a": "CREATE TABLE {schema}.a (id bigint);"}).units[0]

    assert spaced.checksum == tight.checksum
    assert edited.checksum != tight.checksum


def test_the_checksum_is_taken_over_the_template_not_the_rendered_text() -> None:
    """One migration has one checksum across every tenant, so drift is comparable."""

    unit = _registry({"t001_a": VALID_TEMPLATE}).units[0]
    before = unit.checksum

    unit.render("tenant_aaaaaaaa")
    unit.render("tenant_bbbbbbbb")

    assert unit.checksum == before
    assert len(before) == 64


# --- sanitized failure vocabulary -----------------------------------------


def test_every_sanitized_code_fits_the_ledger_column() -> None:
    """The column is varchar(64); a longer code would fail against a real tenant."""

    assert all(len(code.value) <= LEDGER_ERROR_CODE_MAX_LENGTH for code in SanitizedErrorCode)


def test_sanitized_codes_are_uppercase_identifiers_carrying_no_detail() -> None:
    for code in SanitizedErrorCode:
        assert code.value.isupper()
        assert code.value.replace("_", "").isalnum()


# --- lock keys -------------------------------------------------------------


def test_the_tenant_lock_key_is_deterministic_and_fits_a_postgres_integer() -> None:
    first = tenant_lock_key("clinic-a")
    second = tenant_lock_key("clinic-a")

    assert first == second
    assert tenant_lock_key("clinic-b") != first
    for tenant_id in ("clinic-a", "clinic-b", "a-very-long-tenant-identifier-x"):
        assert -(2**31) <= tenant_lock_key(tenant_id) < 2**31


# --- request validation ----------------------------------------------------


def _request(**overrides: object) -> ProvisioningRequest:
    fields: dict[str, object] = {
        "tenant_id": "clinic-a",
        "schema_key": "tenant_aaaaaaaa",
        "actor_id": "provisioning-worker",
        "execution_id": uuid4(),
    }
    fields.update(overrides)
    return ProvisioningRequest(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"tenant_id": "A"}, "TENANT_ID_INVALID"),
        ({"tenant_id": "clinic_a"}, "TENANT_ID_INVALID"),
        ({"schema_key": "shared"}, "SCHEMA_KEY_INVALID"),
        ({"schema_key": "tenant_aaaaaaaa; DROP SCHEMA shared"}, "SCHEMA_KEY_INVALID"),
        ({"actor_kind": "robot"}, "ACTOR_KIND_INVALID"),
        ({"actor_id": ""}, "ACTOR_ID_REQUIRED"),
        ({"execution_id": "not-a-uuid"}, "EXECUTION_ID_INVALID"),
    ],
)
def test_a_malformed_provisioning_request_is_refused_before_any_ddl(
    overrides: dict[str, object], reason_code: str
) -> None:
    with pytest.raises(ProvisioningFailed) as error:
        _validate_request(_request(**overrides))
    assert error.value.reason_code == reason_code


def test_a_well_formed_request_passes_validation() -> None:
    _validate_request(_request())


def test_the_execution_id_guard_does_not_rely_on_static_typing_alone() -> None:
    """Same reasoning as TC-A1: an untyped caller is not bound by an annotation."""

    with pytest.raises(ProvisioningFailed):
        _validate_request(_request(execution_id=str(UUID(int=1))))
