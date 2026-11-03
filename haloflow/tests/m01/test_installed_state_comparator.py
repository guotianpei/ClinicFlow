"""CP2-2b 2B-U30 to U38 (R-B9.2): the separate installed-function comparator.

Test cases v3 section 3.3. Every row below is a CONSTRUCTED observation row. It is
labelled so: nothing here is catalogue or server evidence (test cases v3 section 5).
The D-layer rows in `tests/m02` observe the real catalogue.

Interface bound (packet README, I-B3): `INSTALLED_FUNCTION_QUERY` returns rows of
19 columns in the order of `COLUMNS` below, and
`compare_installed_function(profile, *, schema_key, expected_config, expected_body,
rows)` raises the EXISTING `VerificationMismatch` or returns `None`.

Status before implementation: DB (the names do not exist). After: pass.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

SCHEMA = "tenant_aaaaaaaa"
OWNER = "haloflow_m02_lock_owner"
RUNTIME = "haloflow_runtime"
BODY = "\nBEGIN\n  RETURN NULL;\nEND;\n"
CONFIG = (f"search_path=pg_catalog, {SCHEMA}, pg_temp",)
RUNTIME_OID = 16500
OWNER_OID = 16501

COLUMNS = (
    "oid", "prokind", "pronargs", "arg_is_uuid", "allargtypes_is_null", "argmodes_all_in",
    "nargdefaults_zero", "rettype_is_uuid", "proretset", "lang_is_plpgsql", "owner",
    "prosecdef", "provolatile", "proparallel", "proisstrict", "proconfig", "prosrc",
    "proacl_is_null", "acl",
)
INDEX = {name: position for position, name in enumerate(COLUMNS)}


def profile() -> Any:
    from haloflow.m01.provisioning.installed_state import InstalledStateProfile

    return InstalledStateProfile(
        function_name="m02_lock_operation",
        argument_types=("uuid",),
        owner=OWNER,
        execution_role=OWNER,
        prokind="f",
        return_type="uuid",
        returns_set=False,
        language="plpgsql",
        security_definer=True,
        volatility="v",
        parallel="u",
        strict=False,
        installed_acl=((OWNER, "EXECUTE", OWNER, False), (RUNTIME, "EXECUTE", OWNER, False)),
    )


def good_row(oid: int = 70000) -> list[Any]:
    """A constructed row matching every expectation (the U30 control)."""

    return [
        oid, "f", 1, True, True, True, True, True, False, True, OWNER, True, "v", "u",
        False, list(CONFIG), BODY, False,
        [[OWNER_OID, OWNER, OWNER, "EXECUTE", False],
         [RUNTIME_OID, RUNTIME, OWNER, "EXECUTE", False]],
    ]


def other_identity_row() -> list[Any]:
    """Same name, another signature (`text`): well formed, and not the gateway."""

    row = good_row(oid=70001)
    row[INDEX["arg_is_uuid"]] = False
    return row


def compare(rows: list[list[Any]]) -> None:
    from haloflow.m01.provisioning.installed_state import compare_installed_function

    compare_installed_function(
        profile(),
        schema_key=SCHEMA,
        expected_config=CONFIG,
        expected_body=BODY,
        rows=[tuple(row) for row in rows],
    )


def assert_mismatch(rows: list[list[Any]]) -> None:
    from haloflow.m01.provisioning.verification import VerificationMismatch

    with pytest.raises(VerificationMismatch):
        compare(rows)


def with_(column: str, value: Any, row: list[Any] | None = None) -> list[Any]:
    changed = copy.deepcopy(row if row is not None else good_row())
    changed[INDEX[column]] = value
    return changed


def test_2b_u30_constructed_matching_row_is_accepted() -> None:
    compare([good_row()])


def test_2b_u31_zero_identity_rows_is_a_mismatch() -> None:
    assert_mismatch([])


def test_2b_u31_two_identity_rows_constructed_duplicate_is_a_mismatch() -> None:
    """CONSTRUCTED duplicate: not evidence that the catalogue can hold one."""

    assert_mismatch([good_row(70000), good_row(70002)])


def test_2b_u32_a_well_formed_other_identity_row_is_ignored() -> None:
    compare([other_identity_row(), good_row()])


@pytest.mark.parametrize(
    ("column", "value"),
    [("owner", None), ("prosrc", None), ("provolatile", 3), ("acl", "not-a-list")],
)
def test_2b_u33_a_malformed_other_identity_row_is_a_mismatch(column: str, value: Any) -> None:
    assert_mismatch([with_(column, value, other_identity_row()), good_row()])


def test_2b_u33_a_short_row_is_a_mismatch() -> None:
    assert_mismatch([good_row()[:-1]])


@pytest.mark.parametrize(
    "column",
    ["rettype_is_uuid", "arg_is_uuid", "allargtypes_is_null", "argmodes_all_in",
     "nargdefaults_zero", "lang_is_plpgsql"],
)
def test_2b_u34_each_selected_boolean_false_is_a_mismatch(column: str) -> None:
    assert_mismatch([with_(column, False)])


@pytest.mark.parametrize(
    ("column", "value"),
    [("proretset", True), ("owner", "haloflow_migrator"), ("prosecdef", False),
     ("provolatile", "s"), ("proparallel", "s"), ("proisstrict", True)],
)
def test_2b_u35_each_scalar_property_wrong_is_a_mismatch(column: str, value: Any) -> None:
    assert_mismatch([with_(column, value)])


@pytest.mark.parametrize(
    "config",
    [
        [*CONFIG, "work_mem=64kB"],
        [f"search_path=pg_catalog, {SCHEMA}, public"],
        [f"search_path={SCHEMA}, pg_catalog, pg_temp"],
        [*CONFIG, *CONFIG],
        None,
    ],
    ids=["extra", "substituted", "reordered", "duplicated", "null"],
)
def test_2b_u36_proconfig_not_exact_is_a_mismatch(config: Any) -> None:
    assert_mismatch([with_("proconfig", config)])


def test_2b_u37_body_digest_difference_is_a_mismatch() -> None:
    assert_mismatch([with_("prosrc", BODY.replace("NULL", "NULL "))])


_OWNER_ENTRY = [OWNER_OID, OWNER, OWNER, "EXECUTE", False]
_RUNTIME_ENTRY = [RUNTIME_OID, RUNTIME, OWNER, "EXECUTE", False]


@pytest.mark.parametrize(
    ("proacl_is_null", "acl"),
    [
        (True, []),
        (False, [_OWNER_ENTRY, _RUNTIME_ENTRY, [0, None, OWNER, "EXECUTE", False]]),
        (False, [_OWNER_ENTRY, [RUNTIME_OID, None, OWNER, "EXECUTE", False]]),
        (False, [_OWNER_ENTRY, [RUNTIME_OID, RUNTIME, OWNER, "EXECUTE", True]]),
        (False, [_OWNER_ENTRY, [RUNTIME_OID, RUNTIME, "haloflow_migrator", "EXECUTE", False]]),
        (False, [_RUNTIME_ENTRY]),
        (False, [_OWNER_ENTRY, _RUNTIME_ENTRY, [16502, "haloflow_audit_projector", OWNER,
                                                "EXECUTE", False]]),
        (False, [_OWNER_ENTRY, _RUNTIME_ENTRY, list(_RUNTIME_ENTRY)]),
    ],
    ids=["null", "public", "unresolved", "grantable", "grantor", "owner-missing",
         "third-grantee", "duplicate-constructed"],
)
def test_2b_u38_acl_not_exactly_the_two_tuples_is_a_mismatch(
    proacl_is_null: bool, acl: list[Any]
) -> None:
    row = with_("proacl_is_null", proacl_is_null)
    row[INDEX["acl"]] = acl
    assert_mismatch([row])


# --- F-INT1 (Codex ALIGN 20261031T142215Z): profile-relative identity ---------
#
# Rows U30-U38 above are unchanged and remain the t003 (uuid -> uuid) regression.
# The rows below are CONSTRUCTED observations for a two-argument jsonb profile. In
# the query, `argtypes_match` (position 3, labelled "arg_is_uuid" above) and
# `rettype_matches` (position 7, "rettype_is_uuid") are now relative to the profile.

JSONB_NAME = "m02_append_shape"


def jsonb_profile() -> Any:
    from haloflow.m01.provisioning.installed_state import InstalledStateProfile

    return InstalledStateProfile(
        function_name=JSONB_NAME,
        argument_types=("uuid", "jsonb"),
        owner=OWNER,
        execution_role=OWNER,
        prokind="f",
        return_type="jsonb",
        returns_set=False,
        language="plpgsql",
        security_definer=True,
        volatility="v",
        parallel="u",
        strict=False,
        installed_acl=((OWNER, "EXECUTE", OWNER, False), (RUNTIME, "EXECUTE", OWNER, False)),
    )


def jsonb_row(oid: int = 71000) -> list[Any]:
    row = good_row(oid)
    row[INDEX["pronargs"]] = 2
    return row


def compare_jsonb(rows: list[list[Any]], profile_: Any = None) -> None:
    from haloflow.m01.provisioning.installed_state import compare_installed_function

    compare_installed_function(
        profile_ if profile_ is not None else jsonb_profile(),
        schema_key=SCHEMA,
        expected_config=CONFIG,
        expected_body=BODY,
        rows=[tuple(row) for row in rows],
    )


def assert_jsonb_mismatch(rows: list[list[Any]], profile_: Any = None) -> None:
    from haloflow.m01.provisioning.verification import VerificationMismatch

    with pytest.raises(VerificationMismatch):
        compare_jsonb(rows, profile_)


def test_fint1_u30b_a_matching_two_argument_jsonb_row_is_accepted() -> None:
    compare_jsonb([jsonb_row()])


@pytest.mark.parametrize("pronargs", [0, 1, 3])
def test_fint1_u31b_an_argument_count_other_than_the_profiles_is_no_identity(
    pronargs: int,
) -> None:
    """Even with both type flags true, a row whose `pronargs` differs from the
    profile's argument count is not the identity row, so zero identity rows."""

    assert_jsonb_mismatch([with_("pronargs", pronargs, jsonb_row())])


def test_fint1_u31b_control_the_same_row_at_the_profiles_count_is_accepted() -> None:
    compare_jsonb([with_("pronargs", 2, jsonb_row())])


@pytest.mark.parametrize("column", ["arg_is_uuid", "rettype_is_uuid"])
def test_fint1_u34b_a_false_argument_or_return_flag_is_a_mismatch(column: str) -> None:
    assert_jsonb_mismatch([with_(column, False, jsonb_row())])


def test_fint1_u32b_a_well_formed_other_signature_overload_is_ignored() -> None:
    other = with_("arg_is_uuid", False, jsonb_row(oid=71001))
    compare_jsonb([other, jsonb_row()])


def test_fint1_u30c_the_uuid_profile_rejects_the_two_argument_row() -> None:
    """The t003 profile never accepts a row of another arity."""

    assert_mismatch([jsonb_row()])


_OUTSIDE_VOCABULARY = [
    "public.t", "pg_catalog.uuid", "bytea", "json", "uuid[]", "void", "record",
    "UUID", " uuid", "anyelement",
]


@pytest.mark.parametrize("type_name", _OUTSIDE_VOCABULARY)
@pytest.mark.parametrize("position", ["argument", "return"])
def test_fint1_u39_a_type_outside_the_closed_vocabulary_is_refused_at_construction(
    type_name: str, position: str
) -> None:
    """Refused by the profile itself, before any name can reach `to_regtype`."""

    import dataclasses

    from haloflow.m01.errors import MigrationUnitRejected

    change: dict[str, Any] = (
        {"argument_types": ("uuid", type_name)}
        if position == "argument"
        else {"return_type": type_name}
    )
    with pytest.raises(MigrationUnitRejected) as caught:
        dataclasses.replace(jsonb_profile(), **change)
    assert caught.value.reason_code == "INSTALLED_STATE_PROFILE_INVALID"


@pytest.mark.parametrize(
    "type_name", ["text", "uuid", "jsonb", "bool", "int2", "int4", "int8", "timestamptz"]
)
def test_fint1_u39_control_every_vocabulary_type_constructs(type_name: str) -> None:
    import dataclasses

    dataclasses.replace(jsonb_profile(), argument_types=(type_name,), return_type=type_name)


@pytest.mark.parametrize(
    ("field", "value"),
    [("return_type", "bytea"), ("argument_types", ("uuid", "public.t")),
     ("language", "sql"), ("prokind", "p")],
)
def test_fint1_u39b_the_comparator_stays_defensive(field: str, value: Any) -> None:
    """A profile altered after construction (bypassing `__post_init__`, which no
    production path can do) still fails closed in the comparator."""

    altered = jsonb_profile()
    object.__setattr__(altered, field, value)
    assert_jsonb_mismatch([jsonb_row()], altered)
