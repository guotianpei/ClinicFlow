"""Unit tests for the provisioning package that need no database.

TC-E21 and the trusted-construction controls live here; everything that needs a
real catalogue, a real grant or a real lock is in `test_provisioning_postgres.py`,
because a privilege that is asserted about rather than exercised is exactly the
gap finding F-3 was raised over.
"""

import ast
import json
from pathlib import Path
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
from haloflow.m01.provisioning.codes import (
    LEDGER_ERROR_CODE_MAX_LENGTH,
    PreconditionCode,
)
from haloflow.m01.provisioning.provisioner import ProvisioningRequest, _validate_request
from haloflow.m01.provisioning.roles import PROVISIONING_ROLES
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


# --- the failure vocabulary is closed, and enforced rather than described ---


PROVISIONING_ROOT = Path("src/haloflow/m01/provisioning")


def _raised_reason_codes() -> set[str]:
    """Every literal `reason_code=` value raised in the provisioning package."""

    found: set[str] = set()
    for path in PROVISIONING_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "reason_code":
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(
                    keyword.value.value, str
                ):
                    found.add(keyword.value.value)
    return found


def test_no_reason_code_is_raised_as_a_bare_string() -> None:
    """Review finding: `CONNECTION_NOT_IDLE` belonged to no documented vocabulary.

    It was not alone — sixteen other codes were bare strings in the same position.
    Both vocabularies are now enumerated, so a code that belongs to neither is a
    lint-visible literal rather than a plausible-looking string. The check reads
    the source rather than the enums, because an enum can only be complete about
    codes something actually raises.
    """

    assert _raised_reason_codes() == set(), (
        "raise these through SanitizedErrorCode or PreconditionCode instead of as "
        f"string literals: {sorted(_raised_reason_codes())}"
    )


def test_the_two_failure_vocabularies_do_not_overlap() -> None:
    """A code means one thing: written to the ledger, or raised before one exists."""

    ledger = {code.value for code in SanitizedErrorCode}
    precondition = {code.value for code in PreconditionCode}

    assert ledger & precondition == set()


def test_precondition_codes_carry_no_request_content() -> None:
    for code in PreconditionCode:
        assert code.value.isupper()
        assert code.value.replace("_", "").isalnum()
        assert len(code.value) <= LEDGER_ERROR_CODE_MAX_LENGTH


def test_the_connection_mode_check_raises_neutrally_and_each_caller_translates() -> None:
    """Review finding: a shared helper raised one caller's exception type.

    `require_explicit_transactions` served both the runner and the provisioner but
    always raised `TenantMigrationFailed`, so a provisioning call could fail as a
    migration error. It now raises the neutral `ConnectionModeRejected`, and the
    two entry points each translate it — asserted here from the source, since
    reaching the failure itself needs a database.
    """

    runner_source = (PROVISIONING_ROOT / "runner.py").read_text()
    provisioner_source = (PROVISIONING_ROOT / "provisioner.py").read_text()

    assert "raise ConnectionModeRejected(" in runner_source
    assert "except ConnectionModeRejected as error:" in runner_source
    assert "raise TenantMigrationFailed(reason_code=error.reason_code)" in runner_source

    assert "except ConnectionModeRejected as error:" in provisioner_source
    assert "raise ProvisioningFailed(reason_code=error.reason_code)" in provisioner_source
    # ...and the neutral error is never what a caller of either entry point sees.
    assert "raise ConnectionModeRejected(" not in provisioner_source


# --- CP-1: checksum v2, canonical payload and ordering ---------------------
#
# Traceability: TC-P39 (R-P4.1, R-P4.3), TC-P40 (R-P4.2), TC-P41 (R-P4.4 churn
# asserted, not silently absorbed), TC-P55 (R-P4.5), TC-P56 (R-P4.5), TC-P57
# (R-P4.3, R-P4.5, R-P4.6). Design v7 A6 is authoritative for the payload shape.
#
# The golden vectors below were produced by an oracle written from the A6 text
# alone, independently of `provisioning/checksum.py`, so a digest here disagreeing
# with the module is a real signal rather than the module agreeing with itself.

# The digest checksum v1 produced for the sole production unit. Pinned so the
# v2 churn (R-P4.4) is asserted explicitly rather than absorbed by editing a
# constant to whatever the new code emits.
V1_T001_CHECKSUM = "a2db1ef35b4a2901637ff5d0c057bbbf1bebeee3940b990aa1966eb646b7ff37"
V2_T001_CHECKSUM = "5e232d1563edb413560a939b4564194b268ebda631266900f1ddfa5aa75d0ebd"

V2_SIMPLE_TEMPLATE = "CREATE TABLE {schema}.a (id integer);"
V2_SIMPLE_CHECKSUM = "54dfd1447d43914bcad1861e14896a98a1dcc614d74d80bc303466df3a76aab5"
V2_BRACES_CHECKSUM = "ae14cee61b96ad55183e2bf34d3f39a3099eee7a80e652a53caa494147e36bca"
V2_NON_ASCII_CHECKSUM = "f5303756caca3f52e97a6dff10e82fc85f09ed3bb318c7ffc8ed8c1fc6ec5b56"
V2_ROLE_SET_CHECKSUM = "e1bc9ac7564c95b48c5c39240d25759b702ef5ced48c9fe976fc3a6091fbda82"


def test_checksum_v2_changes_the_production_unit_and_the_change_is_asserted() -> None:
    """TC-P41. Every existing checksum changes under v2 (R-P4.4).

    The old value is pinned beside the new one so the churn is visible in the
    diff. Editing one constant to the value the new code emits would assert
    nothing; asserting the inequality against the recorded v1 digest does.
    """

    unit = build_production_tenant_migrations().units[0]

    assert unit.migration_id == "t001_m01_baseline"
    assert unit.checksum != V1_T001_CHECKSUM
    assert unit.checksum == V2_T001_CHECKSUM


def test_the_checksum_is_taken_over_a_versioned_payload_not_a_concatenation() -> None:
    """TC-P39, R-P4.1. The pair that collides when fields are concatenated.

    ``execution_role="a"`` with ``template="b"`` and ``execution_role="ab"`` with
    an empty template are one string under concatenation and two payloads under
    v2. The unit constructor rejects an empty template, so this addresses the
    payload builder directly -- which is where the ambiguity would live.
    """

    from haloflow.m01.provisioning.checksum import unit_checksum

    collide_a = unit_checksum(migration_id="t001_a", template="b", execution_role="a")
    collide_b = unit_checksum(migration_id="t001_a", template="", execution_role="ab")

    assert collide_a != collide_b


def test_the_canonical_encoding_is_stable_across_unicode_form_and_newline_style() -> None:
    """TC-P40, R-P4.2. Asserted through the public property, not the internals."""

    import unicodedata

    nfc = "CREATE TABLE {schema}.café (id integer);"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd

    composed = _registry({"t001_a": nfc}).units[0]
    decomposed = _registry({"t001_a": nfd}).units[0]
    crlf = _registry({"t001_a": "CREATE TABLE {schema}.a\r\n    (id integer);"}).units[0]
    lf = _registry({"t001_a": "CREATE TABLE {schema}.a\n    (id integer);"}).units[0]

    assert composed.checksum == decomposed.checksum
    assert composed.checksum == V2_NON_ASCII_CHECKSUM
    assert crlf.checksum == lf.checksum


def test_the_checksum_payload_carries_the_execution_role_slot_before_it_is_populated() -> None:
    """R-P4.1 and the A6 shape: the payload churns once, at CP-1.

    ``execution_role`` and ``verification`` are in the payload as ``null`` while
    the fields do not yet exist on the unit, so CP-2 and CP-7 populate them
    without changing the shape or moving every checksum a second time.
    """

    from haloflow.m01.provisioning.checksum import CHECKSUM_VERSION, unit_payload

    assert CHECKSUM_VERSION == 2

    payload = unit_payload(migration_id="t001_a", template=V2_SIMPLE_TEMPLATE)

    assert set(payload) == {
        "checksum_version",
        "execution_role",
        "migration_id",
        "template",
        "verification",
    }
    assert payload["checksum_version"] == 2
    assert payload["execution_role"] is None
    assert payload["verification"] is None


def test_reordering_any_collection_leaves_the_checksum_unchanged() -> None:
    """TC-P55, R-P4.5. Every collection, including the nested ones.

    The collections belong to the verification specification, which CP-7 builds.
    The ordering is written here so CP-7 supplies data to an already-ordered
    structure rather than adding ordering to a payload that shipped without it.
    """

    from haloflow.m01.provisioning.checksum import digest, unit_payload

    def spec(functions, acl, config):
        return {"functions": functions, "acl": acl, "config": config}

    functions_a = [
        {"name": "b_fn", "argument_types": ["uuid"]},
        {"name": "a_fn", "argument_types": ["text", "uuid"]},
        {"name": "a_fn", "argument_types": ["text"]},
    ]
    functions_b = [functions_a[2], functions_a[0], functions_a[1]]

    acl_a = [
        {"grantee": "haloflow_runtime", "privileges": ["EXECUTE", "ALL"]},
        {"grantee": "haloflow_audit_projector", "privileges": ["EXECUTE"]},
    ]
    acl_b = [
        {"grantee": "haloflow_audit_projector", "privileges": ["EXECUTE"]},
        {"grantee": "haloflow_runtime", "privileges": ["ALL", "EXECUTE"]},
    ]

    config_a = ["search_path=tenant_x", "role=haloflow_runtime"]
    config_b = ["role=haloflow_runtime", "search_path=tenant_x"]

    first = digest(
        unit_payload(
            migration_id="t001_a",
            template=V2_SIMPLE_TEMPLATE,
            verification=spec(functions_a, acl_a, config_a),
        )
    )
    second = digest(
        unit_payload(
            migration_id="t001_a",
            template=V2_SIMPLE_TEMPLATE,
            verification=spec(functions_b, acl_b, config_b),
        )
    )

    assert first == second


def test_duplicates_and_conflicting_config_keys_are_rejected_at_construction() -> None:
    """TC-P56, R-P4.5. Ordering resolves presentation; it must not hide a contradiction."""

    from haloflow.m01.provisioning.checksum import (
        ordered_acl,
        ordered_config,
        ordered_functions,
    )

    with pytest.raises(MigrationUnitRejected) as duplicate_function:
        ordered_functions(
            [
                {"name": "a_fn", "argument_types": ["uuid"]},
                {"name": "a_fn", "argument_types": ["uuid"]},
            ]
        )
    assert duplicate_function.value.reason_code == (
        PreconditionCode.DUPLICATE_FUNCTION_IDENTITY.value
    )

    with pytest.raises(MigrationUnitRejected) as duplicate_acl:
        ordered_acl(
            [
                {"grantee": "haloflow_runtime", "privileges": ["EXECUTE"]},
                {"grantee": "haloflow_runtime", "privileges": ["ALL"]},
            ]
        )
    assert duplicate_acl.value.reason_code == PreconditionCode.DUPLICATE_ACL_ENTRY.value

    with pytest.raises(MigrationUnitRejected) as conflicting_config:
        ordered_config(["search_path=a", "search_path=b"])
    assert conflicting_config.value.reason_code == PreconditionCode.CONFLICTING_CONFIG_KEY.value

    # A differing argument list is a different identity, not a duplicate.
    assert len(
        ordered_functions(
            [
                {"name": "a_fn", "argument_types": ["uuid"]},
                {"name": "a_fn", "argument_types": ["text"]},
            ]
        )
    ) == 2


def test_config_entries_are_ordered_by_parsed_key_not_by_raw_string() -> None:
    """TC-P55, R-P4.5. ``proconfig`` entries are ``key=value`` text.

    Sorting the raw strings would order by the value whenever two keys share a
    prefix, so equivalent specifications could digest differently.
    """

    from haloflow.m01.provisioning.checksum import ordered_config

    assert ordered_config(["b=1", "a=2"]) == ("a=2", "b=1")
    # `search_path=a` sorts before `role=z` only if the value is doing the work.
    assert ordered_config(["search_path=a", "role=z"]) == ("role=z", "search_path=a")


def test_golden_vectors_pin_the_canonical_encoding() -> None:
    """TC-P57, TC-P39, R-P4.3. Ordinary braces, non-ASCII, CRLF, and a set role.

    A template containing ``{}`` that is not the ``{schema}`` sentinel must pass
    through untouched -- the payload is built by substitution nowhere and by
    ``str.format`` never (A2b).
    """

    from haloflow.m01.provisioning.checksum import unit_checksum

    braces = "CREATE TABLE {schema}.a (v jsonb DEFAULT '{}'::jsonb);"

    assert unit_checksum(migration_id="t001_a", template=V2_SIMPLE_TEMPLATE) == (
        V2_SIMPLE_CHECKSUM
    )
    assert unit_checksum(migration_id="t001_a", template=braces) == V2_BRACES_CHECKSUM
    assert unit_checksum(
        migration_id="t001_a",
        template=V2_SIMPLE_TEMPLATE,
        execution_role="haloflow_m02_migrator",
    ) == V2_ROLE_SET_CHECKSUM


def test_the_template_collapses_whitespace_and_a_function_body_does_not() -> None:
    """R-P4.6. Two normalizations that must not be merged.

    A reindented migration is not drift; a reindented function body is, because
    the body is compared against ``prosrc`` where drift is meant to be exact.
    Merging these would let the checksum and the verifier disagree about one
    string, which is the failure A6 exists to prevent.
    """

    from haloflow.m01.provisioning.checksum import normalize_body, normalize_template

    spaced = "CREATE TABLE {schema}.a\n    (id integer);"
    tight = "CREATE TABLE {schema}.a (id integer);"

    assert normalize_template(spaced) == normalize_template(tight)
    assert normalize_body(spaced) != normalize_body(tight)

    # Both share the pinned newline and Unicode normalization.
    assert normalize_body("a\r\nb") == "a\nb"
    assert normalize_template("a\r\nb") == "a b"


# --- CP-1 fix: canonicalization fails closed (Codex note-07) ----------------
#
# Both findings were reachable collisions: two distinct supplied specifications
# receiving one digest. A checksum used as a drift control cannot do that, so
# these are regression tests in the strict sense -- each reproduces the exact
# pre-fix collision and asserts it is now a refusal.


def _verified(spec: object) -> str:
    from haloflow.m01.provisioning.checksum import unit_checksum

    return unit_checksum(
        migration_id="t001_a",
        template=V2_SIMPLE_TEMPLATE,
        verification={"spec": spec} if not isinstance(spec, dict) else spec,
    )


def test_a_malformed_member_of_a_collection_is_refused_not_dropped() -> None:
    """Finding 1. Filtering made a malformed specification digest as a valid one.

    Pre-fix, each pair below produced an identical checksum: the member that
    could not be ordered was silently removed from the payload. The collision is
    the defect -- the malformed input must fail construction instead.
    """

    from haloflow.m01.provisioning.checksum import unit_checksum

    valid_functions = [{"name": "f", "argument_types": ["uuid"]}]
    valid_config = ["a=1"]
    valid_acl = [{"grantee": "haloflow_runtime", "privileges": ["EXECUTE"]}]

    malformed: list[dict[str, object]] = [
        {"functions": [*valid_functions, 42]},
        {"config": [*valid_config, 42]},
        {"acl": [{"grantee": "haloflow_runtime", "privileges": ["EXECUTE", 7]}]},
        {"functions": [{"name": "f", "argument_types": ["uuid", 99]}]},
        {"functions": "not a collection"},
        {"config": {"a": 1}},
    ]
    for spec in malformed:
        with pytest.raises(MigrationUnitRejected) as rejected:
            unit_checksum(migration_id="t001_a", template=V2_SIMPLE_TEMPLATE, verification=spec)
        assert rejected.value.reason_code == PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value

    # The valid forms of the same specifications still digest.
    assert len(
        unit_checksum(
            migration_id="t001_a",
            template=V2_SIMPLE_TEMPLATE,
            verification={"functions": valid_functions, "config": valid_config, "acl": valid_acl},
        )
    ) == 64


def test_two_argument_lists_that_differ_only_past_a_dropped_member_stay_distinct() -> None:
    """Finding 1, the other direction: filtering rejected valid input too.

    ``["uuid", 99]`` and ``["uuid", 100]`` both filtered down to ``("uuid",)``,
    so two distinct identities collided and the pair was refused as duplicates.
    Both are now refused as malformed, for the right reason.
    """

    from haloflow.m01.provisioning.checksum import ordered_functions

    with pytest.raises(MigrationUnitRejected) as rejected:
        ordered_functions(
            [
                {"name": "f", "argument_types": ["uuid", 99]},
                {"name": "f", "argument_types": ["uuid", 100]},
            ]
        )
    assert rejected.value.reason_code == PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value

    # Two genuinely distinct string identities remain two.
    assert len(
        ordered_functions(
            [
                {"name": "f", "argument_types": ["uuid", "text"]},
                {"name": "f", "argument_types": ["uuid", "jsonb"]},
            ]
        )
    ) == 2


def test_mapping_keys_that_collide_under_normalization_are_refused() -> None:
    """Finding 2. Composed and decomposed keys became one, and a value was lost.

    Pre-fix, ``{"é": "FIRST", "é": "SECOND"}`` (composed, then decomposed)
    digested identically to ``{"é": "SECOND"}`` — the first value overwritten
    during key normalization, two specifications reduced to one digest.
    """

    import unicodedata

    from haloflow.m01.provisioning.checksum import unit_checksum

    composed = "é"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed

    with pytest.raises(MigrationUnitRejected) as collision:
        unit_checksum(
            migration_id="t001_a",
            template=V2_SIMPLE_TEMPLATE,
            verification={composed: "FIRST", decomposed: "SECOND"},
        )
    assert collision.value.reason_code == PreconditionCode.DUPLICATE_PAYLOAD_KEY.value

    with pytest.raises(MigrationUnitRejected) as non_string_key:
        unit_checksum(
            migration_id="t001_a",
            template=V2_SIMPLE_TEMPLATE,
            verification={"functions": [{"name": "f", 7: "value"}]},
        )
    assert non_string_key.value.reason_code == (
        PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value
    )

    # Either key alone is fine, and the two still digest differently.
    one = unit_checksum(
        migration_id="t001_a", template=V2_SIMPLE_TEMPLATE, verification={composed: "FIRST"}
    )
    two = unit_checksum(
        migration_id="t001_a", template=V2_SIMPLE_TEMPLATE, verification={composed: "SECOND"}
    )
    assert one != two


def test_an_absent_collection_is_empty_not_malformed() -> None:
    """The fix must not turn optional structure into a refusal.

    ``argument_types`` is absent on a function that takes none, and the payload
    shape is CP-7a's to settle. Absent stays absent; present must be well-formed.
    """

    from haloflow.m01.provisioning.checksum import ordered_functions, unit_checksum

    assert len(ordered_functions([{"name": "f"}])) == 1
    assert len(
        unit_checksum(
            migration_id="t001_a",
            template=V2_SIMPLE_TEMPLATE,
            verification={"functions": [], "acl": [], "config": []},
        )
    ) == 64


def test_a_bare_string_is_not_a_collection_of_config_entries() -> None:
    """Codex note-09. The container is validated before it is iterated.

    A string is iterable and yields strings, so materializing first turned
    ``ordered_config("ab")`` into ``("a", "b")`` — a malformed container quietly
    becoming two well-formed entries. `unit_checksum` rejected this earlier on
    its own path, but the public helper's contract was inconsistent, and CP-7a
    is expected to reuse it.
    """

    from haloflow.m01.provisioning.checksum import ordered_acl, ordered_config, ordered_functions

    for malformed in ("ab", b"ab", {"a": "1"}, 7):
        with pytest.raises(MigrationUnitRejected) as rejected:
            ordered_config(malformed)  # type: ignore[arg-type]
        assert rejected.value.reason_code == PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value

    for helper in (ordered_functions, ordered_acl):
        with pytest.raises(MigrationUnitRejected) as refused:
            helper("ab")  # type: ignore[arg-type]
        assert refused.value.reason_code == PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value

    # Legitimate iterables still work, generators included — validation
    # materializes once rather than consuming the iterator to check it.
    assert ordered_config(["b=2", "a=1"]) == ("a=1", "b=2")
    assert ordered_config(("b=2", "a=1")) == ("a=1", "b=2")
    assert ordered_config(entry for entry in ["b=2", "a=1"]) == ("a=1", "b=2")


# --- CP-2: the execution role on a unit ------------------------------------
#
# Traceability: TC-P3 (R-P1.2), TC-P4 (R-P1.3), TC-P5 (C, R-P1.2), the checksum
# half of TC-P6 (R-P1.4), TC-P14 (C, R-P1B.5), TC-P78 (R-P1B.22, D23).
# Architecture A1. Design v7 is authoritative.
#
# Two controls, deliberately independent (R-P1.3): the identifier pattern and
# membership of an approved set supplied at composition time. Neither
# substitutes for the other, which is what TC-P4 exists to prove.

APPROVED_M02 = frozenset({"haloflow_m02_migrator"})


def test_a_role_outside_the_approved_set_is_refused_at_composition() -> None:
    """TC-P3, R-P1.2. The allow-list arrives from composition; M01 embeds none."""

    from haloflow.m01.provisioning.units import UnitDefinition

    definitions = {
        "t002_a": UnitDefinition(VALID_TEMPLATE, execution_role="haloflow_m02_migrator")
    }

    with pytest.raises(MigrationUnitRejected) as refused:
        build_tenant_migration_registry(definitions, approved_execution_roles=frozenset())
    assert refused.value.reason_code == PreconditionCode.EXECUTION_ROLE_NOT_APPROVED.value

    # The same unit composes once the role is approved.
    registry = build_tenant_migration_registry(
        definitions, approved_execution_roles=APPROVED_M02
    )
    assert registry.units[0].execution_role == "haloflow_m02_migrator"


def test_a_role_failing_the_identifier_pattern_is_refused_even_when_approved() -> None:
    """TC-P4, R-P1.3. Two controls, not one.

    Each malformed name below is placed *in the approved set*, so the allow-list
    cannot be what refuses it. A single combined control would pass every one of
    these, which is why the requirement asks for two.
    """

    from haloflow.m01.provisioning.units import UnitDefinition

    malformed = [
        "haloflow_m02; DROP SCHEMA public",
        "haloflow_M02",
        "haloflow_m02-migrator",
        "m02_migrator",
        "haloflow_",
        "haloflow_" + "x" * 49,
        'haloflow_m02"',
    ]
    for name in malformed:
        with pytest.raises(MigrationUnitRejected) as refused:
            build_tenant_migration_registry(
                {"t002_a": UnitDefinition(VALID_TEMPLATE, execution_role=name)},
                approved_execution_roles=frozenset({name}),
            )
        assert refused.value.reason_code == PreconditionCode.EXECUTION_ROLE_INVALID.value, name

    # The boundary case the length bound allows.
    longest = "haloflow_" + "x" * 48
    registry = build_tenant_migration_registry(
        {"t002_a": UnitDefinition(VALID_TEMPLATE, execution_role=longest)},
        approved_execution_roles=frozenset({longest}),
    )
    assert registry.units[0].execution_role == longest


def test_no_infrastructure_role_may_be_an_execution_role() -> None:
    """TC-P78, R-P1B.22(a), D23. Refused at composition, before any database access.

    `haloflow_provisioner` is the load-bearing case: it owns every tenant schema,
    and V29 measured that an execution role which owns the schema *can* mutate
    `nspacl` during stage 4 — which would break R-P1B.20 outright. The preflight
    would catch this too late; it has to fail before anything is provisioned.

    Each role is placed in the approved set, so approval cannot be what saves us.
    """

    from haloflow.m01.provisioning.units import UnitDefinition

    assert "haloflow_provisioner" in PROVISIONING_ROLES

    for role in sorted(PROVISIONING_ROLES):
        with pytest.raises(MigrationUnitRejected) as refused:
            build_tenant_migration_registry(
                {"t002_a": UnitDefinition(VALID_TEMPLATE, execution_role=role)},
                approved_execution_roles=frozenset({role}),
            )
        assert refused.value.reason_code == (
            PreconditionCode.EXECUTION_ROLE_IS_INFRASTRUCTURE.value
        ), role


def test_the_infrastructure_refusal_reads_the_role_vocabulary_rather_than_a_copy() -> None:
    """TC-P78, second half. A hand-listed set would drift from `roles.py`.

    Asserted from the source: the refusal must test membership of
    `PROVISIONING_ROLES`, so a role added to that frozenset is covered without
    anyone remembering to update a second list here.
    """

    source = (PROVISIONING_ROOT / "units.py").read_text()

    assert "PROVISIONING_ROLES" in source
    for role in PROVISIONING_ROLES:
        assert f'"{role}"' not in source, (
            f"{role} is written as a literal in units.py; the control must read "
            "PROVISIONING_ROLES so it cannot drift from roles.py"
        )


def test_the_production_registry_declares_no_execution_role() -> None:
    """TC-P5 (C). Characterizes today's production baseline.

    `t001` runs as `haloflow_migrator` by absence, not by declaration, and no
    module role exists yet. When M02 adds one this test is the thing that makes
    the change visible rather than silent.
    """

    registry = build_production_tenant_migrations()

    assert [unit.execution_role for unit in registry.units] == [None]
    assert APPROVED_TENANT_MIGRATIONS == (TENANT_MIGRATIONS,)


def test_changing_the_execution_role_changes_the_checksum() -> None:
    """TC-P6, checksum half (R-P1.4). The postgres half of TC-P6 is CP-6's.

    Re-applying a unit whose role changed against an `applied` ledger row is
    drift, and that only works if the role is inside the digest.
    """

    from haloflow.m01.provisioning.units import UnitDefinition

    without = build_tenant_migration_registry(
        {"t002_a": VALID_TEMPLATE}, approved_execution_roles=APPROVED_M02
    ).units[0]
    with_role = build_tenant_migration_registry(
        {"t002_a": UnitDefinition(VALID_TEMPLATE, execution_role="haloflow_m02_migrator")},
        approved_execution_roles=APPROVED_M02,
    ).units[0]

    assert without.execution_role is None
    assert without.checksum != with_role.checksum

    # A bare template and an explicit `execution_role=None` are the same unit.
    explicit_none = build_tenant_migration_registry(
        {"t002_a": UnitDefinition(VALID_TEMPLATE, execution_role=None)},
        approved_execution_roles=APPROVED_M02,
    ).units[0]
    assert explicit_none.checksum == without.checksum


def test_composition_performs_no_database_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-P14 (C), R-P1B.5. Composition is the static half only.

    Asserted with every database setting removed from the environment: if
    composition read one, this would fail rather than quietly connect. The
    single-composition-path control depends on this staying pure.
    """

    for variable in (
        "DATABASE_URL",
        "HALOFLOW_MIGRATION_DATABASE_URL",
        "HALOFLOW_TEST_DATABASE_URL",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
    ):
        monkeypatch.delenv(variable, raising=False)

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("composition opened a database connection")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _refuse)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _refuse)

    registry = build_production_tenant_migrations()

    assert registry.migration_ids == ("t001_m01_baseline",)
    assert len(registry.units[0].checksum) == 64


# --- CP-3: the provisioning manifest ---------------------------------------
#
# Traceability: TC-P31 (R-P3.2), TC-P34 (R-P3.4, A5), TC-P68 loader half
# (R-P1B.12, A7). Architecture A5 and A7.
#
# Design variance, approved: the four structured blocks live in
# `m01/manifests/provisioning.json`, not in `permissions.json`. v7 A5/A7 say
# `permissions.json` "gains a sibling block", but four consumers iterate its
# top-level keys as roles and index `policy["allow"]` -- one of them a prohibited
# file. Measured and recorded in claude_note-14; approved in chatgpt_note-15.
#
# Every refusal below is asserted by code, never by message, and the loader takes
# an injected document so a refusal can be exercised without writing a file.

VALID_PROFILE = {
    "login": False,
    "superuser": False,
    "createdb": False,
    "createrole": False,
    "replication": False,
    "bypassrls": False,
    "tenant_schema_privileges": ["CREATE"],
}

INFRASTRUCTURE_SCHEMA_PRIVILEGES = {
    "haloflow_provisioner": {
        "privileges": ["USAGE", "CREATE"],
        "is_grantable": False,
        "grantor": "haloflow_provisioner",
        "role_class": "owner",
    },
    "haloflow_migrator": {
        "privileges": ["USAGE", "CREATE"],
        "is_grantable": False,
        "grantor": "haloflow_provisioner",
        "role_class": "infrastructure",
    },
    "haloflow_runtime": {
        "privileges": ["USAGE"],
        "is_grantable": False,
        "grantor": "haloflow_provisioner",
        "role_class": "infrastructure",
    },
    "haloflow_audit_projector": {
        "privileges": ["USAGE"],
        "is_grantable": False,
        "grantor": "haloflow_provisioner",
        "role_class": "infrastructure",
    },
}


def _manifest_document(**overrides: object) -> dict[str, object]:
    """A minimal well-formed provisioning manifest, with targeted damage applied."""

    document: dict[str, object] = {
        "execution_role_profiles": {},
        "role_memberships": [],
        "tenant_schema_role_privileges": {
            role: dict(entry) for role, entry in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()
        },
        "tenant_table_overrides": [],
    }
    document.update(overrides)
    return document


def test_the_shipped_provisioning_manifest_loads() -> None:
    """R-P1B.12. The file in the repository is itself valid, not merely the fixtures."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    manifest = load_provisioning_manifest()

    assert set(manifest.tenant_schema_role_privileges) >= PROVISIONING_ROLES
    assert manifest.tenant_schema_role_privileges["haloflow_runtime"].privileges == ("USAGE",)
    for entry in manifest.tenant_schema_role_privileges.values():
        assert entry.is_grantable is False
        assert entry.grantor == "haloflow_provisioner"


def test_permissions_json_is_not_read_for_the_provisioning_blocks() -> None:
    """Codex note-15, condition 2. One authoritative source, no fallback.

    `permissions.json` is still consulted to resolve which capability tokens a
    role holds -- A5 requires an override's `narrows` token to be one the role
    actually has -- but never for the four blocks themselves.
    """

    from haloflow.m01.provisioning import manifest as manifest_module

    source = Path(manifest_module.__file__).read_text()

    assert "provisioning.json" in source
    for block in (
        "execution_role_profiles",
        "role_memberships",
        "tenant_schema_role_privileges",
        "tenant_table_overrides",
    ):
        assert block in source
    # No merge or fallback: the blocks are never sought in the permissions file.
    assert ".get(" not in source.split("PERMISSIONS")[-1].split("\n")[0]


def test_an_unrecognized_tenant_schema_token_fails_the_control() -> None:
    """TC-P31, R-P3.2.

    The token vocabulary is enumerated rather than inferred, so a token nobody
    has classified cannot pass through the control unnoticed -- which is the
    shape of finding F-3, where `permissions.json` had been verified only against
    itself.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        classify_tenant_schema_token,
    )

    assert classify_tenant_schema_token("tenant_schema:business_dml") is not None

    for unrecognized in (
        "tenant_schema:invented_capability",
        "tenant_schema:",
        "tenant_schema:business_dml_extra",
        "tenant_schema.unknown_table:select",
    ):
        with pytest.raises(MigrationManifestRejected) as refused:
            classify_tenant_schema_token(unrecognized)
        assert refused.value.reason_code == (
            PreconditionCode.MANIFEST_TOKEN_UNRECOGNIZED.value
        ), unrecognized


def test_every_tenant_schema_token_in_the_permissions_manifest_is_classified() -> None:
    """TC-P31, the other direction. The vocabulary must cover what ships.

    An enumerated vocabulary that has drifted behind `permissions.json` would
    pass the test above while failing on the real file.
    """

    from haloflow.m01.provisioning.manifest import classify_tenant_schema_token

    permissions = json.loads(
        (Path("src/haloflow/m01/manifests/permissions.json")).read_text()
    )
    for role, policy in permissions.items():
        for token in list(policy.get("allow", ())) + list(policy.get("deny", ())):
            if token.startswith("tenant_schema"):
                assert classify_tenant_schema_token(token) is not None, (role, token)


def test_override_validation_refuses_every_way_an_override_can_be_wrong() -> None:
    """TC-P34, R-P3.4, A5. An override may only ever reduce."""

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    valid = {
        "role": "haloflow_runtime",
        "table": "operation_registry",
        "narrows": "tenant_schema:business_dml",
        "privileges": ["SELECT", "INSERT"],
    }
    # The valid form loads, so each refusal below is about the damage and not
    # about the shape being unacceptable in general.
    load_provisioning_manifest(_manifest_document(tenant_table_overrides=[valid]))

    cases: list[tuple[list[object], PreconditionCode]] = [
        ([{**valid, "role": "haloflow_nonexistent"}], PreconditionCode.MANIFEST_ROLE_UNKNOWN),
        ([{**valid, "table": "no_such_table"}], PreconditionCode.MANIFEST_TABLE_UNKNOWN),
        (
            [{**valid, "privileges": ["SELECT", "TELEPORT"]}],
            PreconditionCode.MANIFEST_PRIVILEGE_UNKNOWN,
        ),
        ([valid, dict(valid)], PreconditionCode.MANIFEST_DUPLICATE_DECLARATION),
        (
            [valid, {**valid, "privileges": ["SELECT"]}],
            PreconditionCode.MANIFEST_DUPLICATE_DECLARATION,
        ),
        (
            [{**valid, "narrows": "tenant_schema:checksummed_ddl"}],
            PreconditionCode.MANIFEST_OVERRIDE_INVALID,
        ),
        (
            [{**valid, "privileges": ["SELECT", "INSERT", "UPDATE", "DELETE"]}],
            PreconditionCode.MANIFEST_OVERRIDE_INVALID,
        ),
    ]
    for overrides, expected in cases:
        with pytest.raises(MigrationManifestRejected) as refused:
            load_provisioning_manifest(_manifest_document(tenant_table_overrides=overrides))
        assert refused.value.reason_code == expected.value, overrides


def test_the_loader_refuses_a_runtime_create_declaration() -> None:
    """TC-P68, loader half. R-P1B.12, A7.

    The runtime role holds `USAGE` and never `CREATE`. Stage 3 enforces that
    against a live catalogue at CP-5; this is the half that stops the invariant
    being relaxed by editing data, which is the cheaper attack.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    damaged = {
        role: dict(entry) for role, entry in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()
    }
    damaged["haloflow_runtime"]["privileges"] = ["USAGE", "CREATE"]

    with pytest.raises(MigrationManifestRejected) as refused:
        load_provisioning_manifest(
            _manifest_document(tenant_schema_role_privileges=damaged)
        )
    assert refused.value.reason_code == (
        PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID.value
    )


def test_the_schema_privilege_block_refuses_every_declared_way_of_being_wrong() -> None:
    """A7, D22. Absence is an error, not an empty set.

    A silently missing entry is how the audit-projector gap arose in the first
    place, so a missing infrastructure role is a refusal rather than a default.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    def damaged(**changes: object) -> dict[str, object]:
        block = {r: dict(e) for r, e in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()}
        for role, patch in changes.items():
            block[role].update(patch)  # type: ignore[arg-type]
        return block

    cases: list[tuple[object, PreconditionCode]] = [
        (damaged(haloflow_migrator={"is_grantable": True}),
         PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID),
        (damaged(haloflow_migrator={"grantor": "haloflow_migrator"}),
         PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID),
        (damaged(haloflow_migrator={"privileges": ["USAGE", "SELECT"]}),
         PreconditionCode.MANIFEST_PRIVILEGE_UNKNOWN),
    ]
    for block, expected in cases:
        with pytest.raises(MigrationManifestRejected) as refused:
            load_provisioning_manifest(_manifest_document(tenant_schema_role_privileges=block))
        assert refused.value.reason_code == expected.value

    # A missing infrastructure role is a refusal, not a default.
    for absent in sorted(PROVISIONING_ROLES):
        block = {r: dict(e) for r, e in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()}
        del block[absent]
        with pytest.raises(MigrationManifestRejected) as refused:
            load_provisioning_manifest(_manifest_document(tenant_schema_role_privileges=block))
        assert refused.value.reason_code == (
            PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_MISSING.value
        ), absent

    # An unknown role name in the block is refused too.
    block = {r: dict(e) for r, e in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()}
    block["haloflow_not_a_role"] = dict(INFRASTRUCTURE_SCHEMA_PRIVILEGES["haloflow_runtime"])
    with pytest.raises(MigrationManifestRejected) as refused:
        load_provisioning_manifest(_manifest_document(tenant_schema_role_privileges=block))
    assert refused.value.reason_code == PreconditionCode.MANIFEST_ROLE_UNKNOWN.value


def test_the_top_level_shape_is_validated_strictly_not_heuristically() -> None:
    """Codex note-15, condition 3. Blocks are named, not sniffed by child keys."""

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    unknown = _manifest_document()
    unknown["tenant_table_overides"] = []  # a plausible typo
    with pytest.raises(MigrationManifestRejected) as extra:
        load_provisioning_manifest(unknown)
    assert extra.value.reason_code == PreconditionCode.PROVISIONING_MANIFEST_MALFORMED.value

    for block in (
        "execution_role_profiles",
        "role_memberships",
        "tenant_schema_role_privileges",
        "tenant_table_overrides",
    ):
        missing = _manifest_document()
        del missing[block]
        with pytest.raises(MigrationManifestRejected) as absent:
            load_provisioning_manifest(missing)
        assert absent.value.reason_code == (
            PreconditionCode.PROVISIONING_MANIFEST_MALFORMED.value
        ), block

    # An empty list is a *valid* value -- the shipped manifest declares no
    # membership edge today -- so the wrong-type cases must not include it.
    for wrong in ("text", 7, None, {"role": "haloflow_m02_migrator"}):
        with pytest.raises(MigrationManifestRejected) as mistyped:
            load_provisioning_manifest(_manifest_document(role_memberships=wrong))
        assert mistyped.value.reason_code == (
            PreconditionCode.PROVISIONING_MANIFEST_MALFORMED.value
        ), wrong


def test_an_execution_role_profile_contributes_its_schema_privilege_entry() -> None:
    """A7. All five grantee classes reach the builder in one shape."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    manifest = load_provisioning_manifest(
        _manifest_document(
            execution_role_profiles={"haloflow_m02_migrator": dict(VALID_PROFILE)},
            role_memberships=[
                {
                    "role": "haloflow_m02_migrator",
                    "member": "haloflow_migrator",
                    "set": True,
                    "inherit": False,
                    "admin": False,
                }
            ],
        )
    )

    entry = manifest.tenant_schema_role_privileges["haloflow_m02_migrator"]
    assert entry.privileges == ("CREATE",)
    assert entry.is_grantable is False
    assert entry.grantor == "haloflow_provisioner"
    assert entry.role_class == "execution"
    assert len(manifest.tenant_schema_role_privileges) == 5


def test_manifest_collections_are_canonically_ordered() -> None:
    """Codex note-15, condition 7. Deterministic ordering where it feeds a digest."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    first = load_provisioning_manifest(_manifest_document())
    shuffled = _manifest_document(
        tenant_schema_role_privileges={
            role: dict(INFRASTRUCTURE_SCHEMA_PRIVILEGES[role])
            for role in reversed(list(INFRASTRUCTURE_SCHEMA_PRIVILEGES))
        }
    )
    second = load_provisioning_manifest(shuffled)

    assert list(first.tenant_schema_role_privileges) == list(
        second.tenant_schema_role_privileges
    )
    assert list(first.tenant_schema_role_privileges) == sorted(
        first.tenant_schema_role_privileges
    )
    assert first.tenant_schema_role_privileges["haloflow_provisioner"].privileges == (
        "CREATE",
        "USAGE",
    )


# --- CP-3 correction: the manifest declares fixed invariants, not options ---
#
# Codex note-17, three blocking findings. The loader validated the *shape* of
# declarations that v7 fixes by *value*. Because stages 1-3 compare the live
# catalogue against this manifest, a manifest able to declare unsafe state makes
# those controls agree with the danger instead of catching it. Reproduced before
# fixing: a profile with all six attributes true, a membership to `attacker` with
# `admin: true`, and every infrastructure ACL baseline emptied -- all accepted.


def _profile(**changes: object) -> dict[str, object]:
    profile = dict(VALID_PROFILE)
    profile.update(changes)
    return profile


def _safe_edge(**changes: object) -> dict[str, object]:
    edge: dict[str, object] = {
        "role": "haloflow_m02_migrator",
        "member": "haloflow_migrator",
        "set": True,
        "inherit": False,
        "admin": False,
    }
    edge.update(changes)
    return edge


def _with_execution_role(**changes: object) -> dict[str, object]:
    return _manifest_document(
        execution_role_profiles={"haloflow_m02_migrator": _profile()},
        role_memberships=[_safe_edge(**changes)],
    )


def test_an_execution_role_profile_cannot_declare_an_unsafe_attribute() -> None:
    """Codex note-17 finding 1. R-P1B.4(b) fixes the safe profile.

    NOLOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS.
    Stage 1 compares the database role against this declaration, so a
    declaration that may say `true` lets a dangerous role match dangerous data
    and pass. Each attribute is exercised on its own, so no single check can
    stand in for the other five.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    for attribute in ("login", "superuser", "createdb", "createrole", "replication", "bypassrls"):
        with pytest.raises(MigrationManifestRejected) as refused:
            load_provisioning_manifest(
                _manifest_document(
                    execution_role_profiles={
                        "haloflow_m02_migrator": _profile(**{attribute: True})
                    },
                    role_memberships=[_safe_edge()],
                )
            )
        assert refused.value.reason_code == (
            PreconditionCode.EXECUTION_ROLE_PROFILE_UNSAFE.value
        ), attribute

    # All six false is the only accepted profile.
    load_provisioning_manifest(_with_execution_role())


def test_a_membership_declaration_cannot_describe_an_unsafe_edge() -> None:
    """Codex note-17 finding 2. R-P1B.3, R-P1B.4(c) and A7 fix the edge.

    `member = haloflow_migrator`, `SET true`, `INHERIT false`, `ADMIN false`.
    `INHERIT FALSE` is deliberate and measured (V15): the migrator's ordinary
    statements must not carry the execution role's privileges -- only an explicit
    `SET ROLE` does. `ADMIN FALSE` stops the migrator granting the role onward.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    unsafe: list[dict[str, object]] = [
        {"member": "attacker"},
        {"member": "haloflow_provisioner"},
        {"set": False},
        {"inherit": True},
        {"admin": True},
    ]
    for change in unsafe:
        with pytest.raises(MigrationManifestRejected) as refused:
            load_provisioning_manifest(_with_execution_role(**change))
        assert refused.value.reason_code == (
            PreconditionCode.ROLE_MEMBERSHIP_DECLARATION_UNSAFE.value
        ), change


def test_every_declared_execution_role_must_declare_its_membership_edge() -> None:
    """Codex note-17 finding 2, completeness half.

    A profile with no edge would leave stage 1 with nothing to compare, and an
    absent expectation is how a grantee escapes a completeness check -- the same
    shape as the audit-projector gap D22 exists to close.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    with pytest.raises(MigrationManifestRejected) as missing:
        load_provisioning_manifest(
            _manifest_document(
                execution_role_profiles={"haloflow_m02_migrator": _profile()}
            )
        )
    assert missing.value.reason_code == (
        PreconditionCode.ROLE_MEMBERSHIP_DECLARATION_MISSING.value
    )

    # And an edge for a role with no profile is refused from the other side.
    with pytest.raises(MigrationManifestRejected) as orphan:
        load_provisioning_manifest(_manifest_document(role_memberships=[_safe_edge()]))
    assert orphan.value.reason_code == PreconditionCode.MANIFEST_ROLE_UNKNOWN.value


def test_the_infrastructure_acl_baselines_are_fixed_not_declared() -> None:
    """Codex note-17 finding 3. R-P1B.13, A7, D22 fix all four sets exactly.

    Provisioner and migrator hold exactly `USAGE, CREATE`; runtime and the audit
    projector exactly `USAGE`. Stage 2 installs and stage 3 verifies whatever
    this file says, so a weakened declaration is faithfully installed as policy.
    Both directions are refused: narrower and broader.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    expected = {
        "haloflow_provisioner": ["USAGE", "CREATE"],
        "haloflow_migrator": ["USAGE", "CREATE"],
        "haloflow_runtime": ["USAGE"],
        "haloflow_audit_projector": ["USAGE"],
    }

    def block(role: str, privileges: list[str]) -> dict[str, object]:
        damaged = {r: dict(e) for r, e in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()}
        damaged[role]["privileges"] = privileges
        return damaged

    for role, correct in expected.items():
        narrower = [] if len(correct) == 1 else ["USAGE"]
        broader = ["USAGE", "CREATE"] if len(correct) == 1 else ["USAGE", "CREATE", "USAGE"]

        with pytest.raises(MigrationManifestRejected) as narrowed:
            load_provisioning_manifest(
                _manifest_document(tenant_schema_role_privileges=block(role, narrower))
            )
        assert narrowed.value.reason_code in {
            PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID.value,
            PreconditionCode.MANIFEST_DUPLICATE_DECLARATION.value,
        }, (role, narrower)

        with pytest.raises(MigrationManifestRejected) as widened:
            load_provisioning_manifest(
                _manifest_document(tenant_schema_role_privileges=block(role, broader))
            )
        assert widened.value.reason_code in {
            PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID.value,
            PreconditionCode.MANIFEST_DUPLICATE_DECLARATION.value,
        }, (role, broader)

    # Emptying any baseline is refused -- the case Codex reproduced.
    for role in expected:
        with pytest.raises(MigrationManifestRejected) as emptied:
            load_provisioning_manifest(
                _manifest_document(tenant_schema_role_privileges=block(role, []))
            )
        assert emptied.value.reason_code == (
            PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID.value
        ), role


def test_the_role_class_is_derived_from_the_role_not_taken_on_trust() -> None:
    """Beyond note-17's three findings, and flagged as such.

    `role_class` was the fourth field in the same function accepting any value.
    It is the same defect class, so leaving it would have meant fixing three
    instances of a bug and leaving the fourth in the code Codex had just
    reviewed. The owner is the provisioner, the other three fixed roles are
    infrastructure, and a declared execution role is execution.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    mislabelled = {r: dict(e) for r, e in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()}
    mislabelled["haloflow_runtime"]["role_class"] = "owner"

    with pytest.raises(MigrationManifestRejected) as refused:
        load_provisioning_manifest(
            _manifest_document(tenant_schema_role_privileges=mislabelled)
        )
    assert refused.value.reason_code == (
        PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID.value
    )

    manifest = load_provisioning_manifest(_with_execution_role())
    entries = manifest.tenant_schema_role_privileges
    assert entries["haloflow_provisioner"].role_class == "owner"
    assert entries["haloflow_migrator"].role_class == "infrastructure"
    assert entries["haloflow_m02_migrator"].role_class == "execution"


def test_an_unknown_key_in_any_nested_entry_is_refused() -> None:
    """Codex note-19. Strictness applies one level down, not only at the top.

    Note-15 condition 3 required the *top-level* block shape to be strict and
    named. The nested entries were not: every one of the four blocks silently
    ignored an unknown key, so `"bypassrIs"` — a capital I where an l belongs,
    indistinguishable in most fonts — read as accepted configuration.

    The narrower risk Codex named is the durable one: a future option added to
    this file but not to the loader would be ignored rather than refused, and the
    manifest would pass while the database carried an option nobody checked.

    Fixing the code rather than narrowing the comment, because silently ignoring
    an unknown key in a security declaration is the same fail-open shape as the
    three note-17 findings.
    """

    from haloflow.m01.provisioning.manifest import (
        MigrationManifestRejected,
        load_provisioning_manifest,
    )

    malformed = PreconditionCode.PROVISIONING_MANIFEST_MALFORMED.value
    valid_override = {
        "role": "haloflow_runtime",
        "table": "operation_registry",
        "narrows": "tenant_schema:business_dml",
        "privileges": ["SELECT", "INSERT"],
    }

    cases: list[tuple[str, dict[str, object]]] = [
        (
            "membership entry",
            _manifest_document(
                execution_role_profiles={"haloflow_m02_migrator": _profile()},
                role_memberships=[_safe_edge(adnim=True)],
            ),
        ),
        (
            "execution-role profile",
            _manifest_document(
                execution_role_profiles={
                    "haloflow_m02_migrator": _profile(bypassrIs=True)
                },
                role_memberships=[_safe_edge()],
            ),
        ),
        (
            "schema-privilege entry",
            _manifest_document(
                tenant_schema_role_privileges={
                    **{r: dict(e) for r, e in INFRASTRUCTURE_SCHEMA_PRIVILEGES.items()},
                    "haloflow_runtime": {
                        **INFRASTRUCTURE_SCHEMA_PRIVILEGES["haloflow_runtime"],
                        "is_grantible": True,
                    },
                }
            ),
        ),
        (
            "table override",
            _manifest_document(
                tenant_table_overrides=[{**valid_override, "privilages": ["UPDATE"]}]
            ),
        ),
    ]
    for label, document in cases:
        with pytest.raises(MigrationManifestRejected) as refused:
            load_provisioning_manifest(document)
        assert refused.value.reason_code == malformed, label

    # A missing required key stays a refusal too, from the other direction.
    incomplete = dict(_safe_edge())
    del incomplete["admin"]
    with pytest.raises(MigrationManifestRejected):
        load_provisioning_manifest(
            _manifest_document(
                execution_role_profiles={"haloflow_m02_migrator": _profile()},
                role_memberships=[incomplete],
            )
        )

    # And the shipped manifest, which has no stray keys, still loads.
    assert load_provisioning_manifest() is not None
