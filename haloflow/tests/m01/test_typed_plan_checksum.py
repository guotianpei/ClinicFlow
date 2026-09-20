"""CP2-1 -- U layer that runs TODAY: fixture controls, TP-24b and TP-24c.

Every case here exercises code that exists at `621cba7`: the frozen CP1 checker
`validate_function_installation` and the function-v3 `function_checksum`. None of
it needs the typed runner path. So these are expected GREEN before any
implementation, and that is recorded honestly as GREEN / BASELINE -- not claimed
as a CP2 achievement.

Two jobs:

1. FIXTURE CONTROLS. Every payload the typed I-layer cases use is asserted here to
   be admitted or refused by the frozen checker with the exact code the case
   relies on. When a typed case is later red, the fixture is already excluded as
   the cause.

2. TP-24b and TP-24c, the two U rows of v5.1 section 5.3. They are about the
   checksum helper on its own. TP-24b-i -- the runner writes this algorithm's
   digest -- is a different claim and lives in the I-layer module.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.checksum import unit_checksum
from haloflow.m01.provisioning.function_checksum import function_checksum
from haloflow.m01.provisioning.function_policy import validate_function_installation

SCHEMA = "tenant_aaaaaaaa"
ALT_SCHEMA = "tenant_bbbbbbbb"
CHECKSUM_FIELDS = ("migration_id", "template", "execution_role", "verification", "policy")


def encode(payload: dict[str, Any]) -> bytes:
    """The encoding `test_function_policy.py` uses for the frozen checker."""

    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def checksum_of(payload: dict[str, Any]) -> str:
    return function_checksum(**{key: payload[key] for key in CHECKSUM_FIELDS})


# ---------------------------------------------------------------------------
# Fixture controls -- the frozen checker's verdict on every derived payload.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["first", "second", "annex"])
@pytest.mark.parametrize("schema_key", [SCHEMA, ALT_SCHEMA])
def test_fixture_admitted(name: str, schema_key: str, typed_payloads: dict[str, Any]) -> None:
    """Admitted, with exactly the bytes and checksum the typed cases expect."""

    payload = typed_payloads[name]
    result = validate_function_installation(encode(payload), schema_key=schema_key)

    assert result.sql_bytes == payload["template"].replace("{schema}", schema_key).encode(
        "utf-8"
    )
    assert result.schema_key == schema_key
    assert result.migration_id == payload["migration_id"]
    assert result.execution_role == payload["execution_role"]
    assert result.policy_version == 1
    assert result.checksum == checksum_of(payload)


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("body_drift", "INSTALL_BODY_MISMATCH"),
        ("second_body_drift", "INSTALL_BODY_MISMATCH"),
        ("table", "INSTALL_TOPLEVEL_FORM_FORBIDDEN"),
        ("unknown_select", "INSTALL_TOPLEVEL_FORM_UNKNOWN"),
        ("payload_nul", "INSTALL_NUL_FORBIDDEN"),
    ],
)
def test_fixture_refused(name: str, code: str, typed_payloads: dict[str, Any]) -> None:
    """Refused by the frozen checker with the exact code the typed case asserts."""

    with pytest.raises(MigrationUnitRejected) as caught:
        validate_function_installation(encode(typed_payloads[name]), schema_key=SCHEMA)
    assert caught.value.reason_code == code


def test_fixture_units_are_distinct(typed_payloads: dict[str, Any]) -> None:
    """`first` and `second` are two units, not one unit twice.

    `B-late-invalid` and `B-late-failure` need two admitted declarations with
    distinct migration identities (Codex, v5 acceptance, correction 1). Distinct
    function names too, so the two DDL statements are distinguishable in a trace.
    """

    first, second = typed_payloads["first"], typed_payloads["second"]
    assert (first["migration_id"], second["migration_id"]) == (
        "t002_annex_probe",
        "t003_annex_probe",
    )
    assert "m02_annex_probe(" in first["template"]
    assert "m02_annex_second(" in second["template"]
    assert "m02_annex_probe(" not in second["template"]
    assert checksum_of(first) != checksum_of(second)


def test_fixture_annex_differs_only_in_role_and_owner(typed_payloads: dict[str, Any]) -> None:
    """TP-24a's alternate: a TWO-FIELD coherent declaration change (Q-5 ruling).

    Template and policy constant; `execution_role` and the verification owner move
    together. Not isolated evidence of role sensitivity.
    """

    first, annex = typed_payloads["first"], typed_payloads["annex"]
    assert annex["template"] == first["template"]
    assert annex["policy"] == first["policy"]
    assert annex["execution_role"] == "haloflow_m02_annex"
    reverted = copy.deepcopy(annex)
    reverted["execution_role"] = first["execution_role"]
    reverted["verification"]["functions"][0]["owner"] = first["verification"]["functions"][0][
        "owner"
    ]
    assert reverted == first


@pytest.mark.parametrize(
    "legacy_role",
    ["declared", None],
    ids=["role-bearing-TP-24b-i", "role-less-TP-25"],
)
def test_typed_and_v2_checksums_differ_for_the_same_unit(
    legacy_role: str | None, typed_payloads: dict[str, Any]
) -> None:
    """The discriminators TP-24b-i and TP-25 rely on -- one per exact legacy fixture.

    If the v3 and v2 algorithms agreed on a unit, "the runner wrote the v3 digest"
    and "the runner wrote the v2 digest" would be one observation. TP-24b-i compares
    against a ROLE-BEARING legacy digest; TP-25's applied v2 row is ROLE-LESS. v12
    checked only the first; Codex's Q-3 ruling requires the premise for the exact
    fixture TP-25 uses, so both are pinned here. Legacy verification is none in both.
    """

    first = typed_payloads["first"]
    legacy = unit_checksum(
        migration_id=first["migration_id"],
        template=first["template"],
        execution_role=first["execution_role"] if legacy_role == "declared" else None,
        verification=None,
    )
    assert legacy != checksum_of(first)


# ---------------------------------------------------------------------------
# TP-24b -- checksum-layer policy sensitivity.
#
# v5.1 section 5.3, as corrected by Codex: "change a policy value ONLY at the pure
# checksum layer and assert `function_checksum` moves. Makes NO claim that the
# changed policy would be admitted." None of the edited payloads below is passed
# to the checker, and several would be refused by it.
# ---------------------------------------------------------------------------


def _set(payload: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    target: Any = payload
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value


POLICY_EDITS = [
    (("policy", "functions", 0, "volatility"), "stable"),
    (("policy", "functions", 0, "parallel"), "safe"),
    (("policy", "functions", 0, "strict"), True),
    (("policy", "functions", 0, "acl", 1, "privileges"), ["USAGE"]),
    (("policy", "semantic_version"), 2),
]


@pytest.mark.parametrize(
    ("path", "value"),
    POLICY_EDITS,
    ids=[".".join(map(str, path[1:])) for path, _ in POLICY_EDITS],
)
def test_tp24b_function_checksum_moves_with_a_policy_value(
    path: tuple[Any, ...], value: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-24b. One policy value changes; template and everything else held constant."""

    baseline = typed_payloads["first"]
    edited = copy.deepcopy(baseline)
    _set(edited, path, value)

    # Single mutation, established rather than assumed.
    assert edited != baseline
    assert edited["template"] == baseline["template"]
    for key in ("migration_id", "execution_role", "verification"):
        assert edited[key] == baseline[key]

    assert checksum_of(edited) != checksum_of(baseline)


# ---------------------------------------------------------------------------
# TP-24c -- ACL reorder equality, asserted as its own equality check.
#
# Overlap, stated: `test_function_checksum.py::
# test_function_and_acl_set_permutations_canonicalize` already reverses ACLs at
# the canonical-bytes layer on its own payloads. TP-24c is the same property on
# the typed-path fixture, at `function_checksum`, which is the digest the ledger
# will hold. It is kept because v5.1 names it; it is not new coverage of the
# normalizer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sections", [("policy",), ("verification",), ("policy", "verification")])
def test_tp24c_acl_order_does_not_move_the_checksum(
    sections: tuple[str, ...], typed_payloads: dict[str, Any]
) -> None:
    baseline = typed_payloads["first"]
    reordered = copy.deepcopy(baseline)
    for section in sections:
        acl = reordered[section]["functions"][0]["acl"]
        assert len(acl) == 2, "a one-entry ACL cannot be reordered; the case would be vacuous"
        acl.reverse()
        assert acl != baseline[section]["functions"][0]["acl"]

    assert checksum_of(reordered) == checksum_of(baseline)
