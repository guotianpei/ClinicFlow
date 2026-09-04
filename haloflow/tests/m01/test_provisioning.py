"""Unit tests for the provisioning package that need no database.

TC-E21 and the trusted-construction controls live here; everything that needs a
real catalogue, a real grant or a real lock is in `test_provisioning_postgres.py`,
because a privilege that is asserted about rather than exercised is exactly the
gap finding F-3 was raised over.
"""

import ast
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
