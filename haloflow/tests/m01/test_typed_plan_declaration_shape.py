"""CP2-1 correction -- SHAPE: malformed declarations must fail at composition.

NEW FILE, a clearly identified supplement. Every v14 file stays byte-identical.

Responds to the v15 review, finding 3. `_declaration_checks` tests
`required <= set(block)` -- key PRESENCE and nothing else -- so unknown keys,
null values, wrong types and malformed nested shapes all compose successfully
today. Reproduced before these tests were written:

    ACCEPTED  <-- EXTRA unknown policy key
    ACCEPTED  <-- ALL-NULL policy values
    ACCEPTED  <-- NULL verification function list
    ACCEPTED  <-- functions is a string, not a list
    ACCEPTED  <-- policy_verification kind is WRONG

The last case is not in Codex's list; it was found while reproducing the other
four, and it is the worst of them: a `policy_verification` block declaring the
WRONG `kind` passes the gate whose whole purpose is to check that.

WHERE THE SCHEMA COMES FROM
---------------------------
Not from here. `function_policy._closed_shape` is the authoritative, frozen,
recursive shape validator and it already raises `INSTALL_POLICY_INVALID`. These
tests pin the BEHAVIOUR (refused at composition, with that code); they
deliberately do not restate the schema, because a second copy of a schema that
can drift from the first is the exact defect CP2-1 exists to close. Owner
decision B2: composition calls the frozen path rather than mirroring it.

TWO THINGS THE FROZEN HELPER DOES NOT DO, so neither is claimed of it:

* It checks `verification.kind` is a STRING, never its value. SHAPE-12 covers
  the value and pins `VERIFICATION_KIND_UNKNOWN`, the code `verification.py`
  already raises for it. My v17 prescription named the wrong constant here and
  Codex caught it; the case carries the full correction.
* It runs on a payload. If the correction freezes or thaws BEFORE validating,
  a caller's malformed tuple in a list-required field is silently normalized
  into an accepted list. SHAPE-13..15 pin the ORIGINAL input's type so a
  normalize-then-validate implementation fails.

Pre-change state: RED. Every refusal case below composes successfully today.
Four cases are GREEN today and must stay green: the three controls, and
SHAPE-04 — a missing required key IS already refused, so the current check is
insufficient rather than useless.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.units import build_tenant_migration_registry

ROLES = frozenset({"haloflow_m02_owner", "haloflow_m02_annex"})
POLICY_INVALID = "INSTALL_POLICY_INVALID"
# The authoritative code for an unsupported verification kind, already raised by
# `verification.py` for this exact condition. See SHAPE-12.
KIND_UNKNOWN = "VERIFICATION_KIND_UNKNOWN"


def compose(definitions: dict[str, Any]) -> Any:
    """The public builder with the sanctioned test flag (R-E12), as v11 does."""

    return build_tenant_migration_registry(
        definitions, approved_execution_roles=ROLES, allow_test_units=True
    )


def refused_at_composition(make: Callable[[], dict[str, Any]]) -> str:
    """Build AND compose inside one `raises`; return the refusal code.

    Which of the two steps raises is the implementation's choice and is not
    asserted. That NO registry results is asserted, and it is the oracle.
    """

    with pytest.raises(MigrationUnitRejected) as caught:
        compose(make())
    return caught.value.reason_code


def typed_unit(seam: Any, payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {payload["migration_id"]: seam.typed_definition(payload, **overrides)}


def edited(payload: dict[str, Any], edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """A deep copy of `payload` with ONE edit applied, so each case is single."""

    result = copy.deepcopy(payload)
    edit(result)
    return result


# ---------------------------------------------------------------------------
# Controls. Every refusal below is meaningful only if these pass.
# ---------------------------------------------------------------------------


def test_shape_control_a_well_formed_typed_unit_still_composes(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """CONTROL, green today and after. Stricter validation must not reject valid work."""

    payload = typed_payloads["first"]
    registry = compose(typed_unit(seam, payload))

    (unit,) = registry.units
    assert seam.is_typed(unit) is True


def test_shape_control_an_ordinary_unit_is_untouched(seam: Any) -> None:
    """CONTROL, EX-01. The ordinary route sees no new validation at all."""

    registry = compose({"t001_test_cp2": seam.ordinary_definition(
        "CREATE TABLE {schema}.cp2_probe (id int);"
    )})

    (unit,) = registry.units
    assert seam.is_typed(unit) is False


def test_shape_control_a_valid_policy_with_a_wrong_body_is_not_refused_here(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """CONTROL, the boundary. A well-SHAPED declaration composes even when its
    body is wrong; catching that is the actual-schema checker's job at
    pending-install, and it must not migrate into composition (TP-R13).
    """

    payload = edited(
        typed_payloads["first"],
        lambda p: p["policy"]["functions"][0].__setitem__("body_sha256", "0" * 64),
    )

    registry = compose(typed_unit(seam, payload))
    assert len(registry.units) == 1


# ---------------------------------------------------------------------------
# SHAPE-01..04  Closed key sets.
# ---------------------------------------------------------------------------


def test_shape01_an_unknown_policy_key_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    payload = edited(typed_payloads["first"],
                     lambda p: p["policy"].__setitem__("surprise", "anything"))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape02_an_unknown_verification_key_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    payload = edited(typed_payloads["first"],
                     lambda p: p["verification"].__setitem__("surprise", "anything"))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape03_an_unknown_function_key_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """Recursion, one level down: the closed key set applies to each function."""

    payload = edited(typed_payloads["first"],
                     lambda p: p["policy"]["functions"][0].__setitem__("surprise", 1))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape04_a_missing_policy_key_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    payload = edited(typed_payloads["first"], lambda p: p["policy"].pop("parser_version"))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


# ---------------------------------------------------------------------------
# SHAPE-05  Null values. Codex reproduced the all-null case; each key is pinned
# separately so a partial fix cannot pass.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["policy_format", "semantic_version", "parser_package",
     "parser_version", "grammar_major", "functions"],
)
def test_shape05_a_null_policy_value_is_refused(
    key: str, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    payload = edited(typed_payloads["first"], lambda p: p["policy"].__setitem__(key, None))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape06_a_null_verification_function_list_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """Codex reproduced this one directly."""

    payload = edited(typed_payloads["first"],
                     lambda p: p["verification"].__setitem__("functions", None))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


# ---------------------------------------------------------------------------
# SHAPE-07..10  Wrong types. `grammar_major` carries the bool trap: `True` is an
# instance of `int`, so an `isinstance` check would admit it. The frozen
# validator uses `type(x) is int`, which does not. Pinned so a reimplementation
# cannot regress to `isinstance`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("grammar_major", True),
        ("grammar_major", "17"),
        ("grammar_major", 17.0),
        ("policy_format", "1"),
        ("parser_package", 1),
        ("functions", "not-a-list"),
        ("functions", {}),
        ("functions", []),
    ],
    ids=["grammar-major-bool-trap", "grammar-major-str", "grammar-major-float",
         "policy-format-str", "parser-package-int", "functions-str",
         "functions-dict", "functions-empty"],
)
def test_shape07_a_wrongly_typed_policy_value_is_refused(
    key: str, value: Any, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    payload = edited(typed_payloads["first"], lambda p: p["policy"].__setitem__(key, value))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape08_a_non_mapping_function_entry_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    payload = edited(typed_payloads["first"],
                     lambda p: p["policy"].__setitem__("functions", ["not-a-mapping"]))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


@pytest.mark.parametrize(
    ("key", "value"),
    [("name", 1), ("is_procedure", "yes"), ("comment", 1), ("config", "not-a-list"),
     ("inputs", "not-a-list"), ("acl", "not-a-list")],
    ids=["name-int", "is-procedure-str", "comment-int", "config-str",
         "inputs-str", "acl-str"],
)
def test_shape09_a_wrongly_typed_function_field_is_refused(
    key: str, value: Any, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """Recursion proper: wrong types INSIDE a function entry, not just beside it."""

    payload = edited(typed_payloads["first"],
                     lambda p: p["policy"]["functions"][0].__setitem__(key, value))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape10_a_malformed_parameter_entry_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """Two levels down: inside a function's `inputs`."""

    payload = edited(
        typed_payloads["first"],
        lambda p: p["policy"]["functions"][0].__setitem__("inputs", [{"type": "int"}]),
    )
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape11_a_malformed_acl_entry_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """Two levels down, the other branch: inside a function's `acl`."""

    payload = edited(
        typed_payloads["first"],
        lambda p: p["policy"]["functions"][0].__setitem__(
            "acl", [{"grantee": "r", "privileges": "EXECUTE"}]
        ),
    )
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


# ---------------------------------------------------------------------------
# SHAPE-12  The one I found while verifying Codex's report.
# ---------------------------------------------------------------------------


def test_shape12_a_wrong_policy_verification_kind_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """NOT in the v15 review's list -- found while reproducing the others.

    A `policy_verification` block whose `kind` is not the supported verification
    kind is accepted today. This is the gate whose entire purpose is to check
    `kind`, and it does not.

    THE FROZEN HELPER DOES NOT COVER THIS. `_closed_shape` checks only that
    `verification.kind` IS A STRING, never its value. B2 alone does not make this
    case pass, and it would be wrong to claim otherwise.

    MY v17 PRESCRIPTION WAS WRONG, and Codex caught it. I wrote that the fix
    should pin `units.TYPED_FUNCTION_KIND`. These are two different vocabularies:

        units.TYPED_FUNCTION_KIND      == "typed_function_v3"   the UNIT's kind
        policy_verification["kind"]    == "function_metadata"   the BLOCK's kind

    Pinning the first would have rejected every valid typed unit. The authority
    for the second is `verification.FunctionMetadataVerification`, whose `kind`
    is `Literal["function_metadata"]` and which already refuses a wrong value
    with `VERIFICATION_KIND_UNKNOWN`.

    CODE CHOICE, AND IT IS MINE TO DEFEND. This case pins
    `VERIFICATION_KIND_UNKNOWN`, not `INSTALL_POLICY_INVALID`. Codex asked for the
    authoritative vocabulary through a narrow boundary but did not name a code.
    `verification.py` already raises `VERIFICATION_KIND_UNKNOWN` for exactly this
    condition, so reusing it keeps one meaning for one failure instead of minting
    a second. A test that accepted EITHER code would pass for the wrong reason --
    the defect v11 withdrew `B-legacy-bypass` for -- so one is pinned. If the
    contract wants `INSTALL_POLICY_INVALID` here instead, say so and I change the
    pin rather than widening it.

    A vocabulary check only. No other semantic policy check moves earlier.
    """

    payload = edited(typed_payloads["first"],
                     lambda p: p["verification"].__setitem__("kind", "something_else"))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == KIND_UNKNOWN


@pytest.mark.parametrize("value", [123, None, ["function_metadata"], {}],
                         ids=["int", "none", "list", "dict"])
def test_shape12b_a_non_string_verification_kind_is_a_STRUCTURAL_failure(
    value: Any, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """SHAPE-12b. The other half of Codex's v18 ruling, pinned separately.

    The ruling draws a line that SHAPE-12 alone does not express:

        unsupported STRING kind, block otherwise well-shaped
            -> VERIFICATION_KIND_UNKNOWN   (a vocabulary failure)
        NON-STRING kind, or any other structural fault
            -> INSTALL_POLICY_INVALID      (a shape failure)

    That ordering is the contract: validate the original STRUCTURE first, then
    apply the narrow vocabulary check. An implementation that ran the vocabulary
    check first would report `VERIFICATION_KIND_UNKNOWN` for an integer kind,
    passing SHAPE-12 while getting this backwards. Both cases exist so the
    boundary between them is pinned rather than implied.
    """

    payload = edited(typed_payloads["first"],
                     lambda p: p["verification"].__setitem__("kind", value))
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


# ---------------------------------------------------------------------------
# SHAPE-13..15  Validation must precede normalization.
#
# Codex, v17 review, point 3. If the correction freezes/thaws FIRST and validates
# the result, a caller's malformed tuple in a list-required field is silently
# normalized into an accepted list and every case above still passes. These pin
# the original input's type, so a normalize-then-validate implementation fails.
# ---------------------------------------------------------------------------


def test_shape13_a_tuple_where_a_list_is_required_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """`policy.functions` supplied as a tuple. Structurally identical once
    normalized, and therefore exactly the case a freeze-first fix would miss."""

    payload = edited(
        typed_payloads["first"],
        lambda p: p["policy"].__setitem__("functions", tuple(p["policy"]["functions"])),
    )
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape14_a_tuple_in_a_nested_verification_field_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """The same, one branch over and one level down: inside `verification`."""

    payload = edited(
        typed_payloads["first"],
        lambda p: p["verification"].__setitem__(
            "functions", tuple(p["verification"]["functions"])
        ),
    )
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape15_a_tuple_inside_a_verification_function_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """Deepest: a tuple inside a verification function's own list field."""

    payload = edited(
        typed_payloads["first"],
        lambda p: p["verification"]["functions"][0].__setitem__(
            "argument_types", tuple(p["verification"]["functions"][0]["argument_types"])
        ),
    )
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


@pytest.mark.parametrize(
    ("key", "value"),
    [("name", 1), ("owner", None), ("security_definer", "yes"),
     ("body", 1), ("config", "not-a-list"), ("acl", "not-a-list")],
    ids=["name-int", "owner-none", "security-definer-str", "body-int",
         "config-str", "acl-str"],
)
def test_shape16_a_malformed_verification_function_record_is_refused(
    key: str, value: Any, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """The OTHER recursive branch. Codex, v17 review, point 3: the shape cases
    exercised `policy.functions` thoroughly and `verification.functions` barely.
    """

    payload = edited(
        typed_payloads["first"],
        lambda p: p["verification"]["functions"][0].__setitem__(key, value),
    )
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID


def test_shape17_an_unknown_verification_function_key_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    payload = edited(
        typed_payloads["first"],
        lambda p: p["verification"]["functions"][0].__setitem__("surprise", 1),
    )
    assert refused_at_composition(lambda: typed_unit(seam, payload)) == POLICY_INVALID
