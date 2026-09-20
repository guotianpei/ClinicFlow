"""CP2-1 -- U layer: declaration and classification, v5.1 section 5.1.

Pre-change state: RED / INTERFACE for every typed row. The typed vocabulary does
not exist, and each case fails at its own first use of `seam` with
`InterfaceAbsent` naming the capability. TP-02, TP-04 and TP-05 build ordinary
registries that exist today and fail only at `seam.is_typed`.

WHERE A REFUSAL MAY HAPPEN
--------------------------
v5.1 says "at composition" (TP-R13: composition performs pure declaration checks).
Composition here is BOTH steps -- constructing the definition and passing it to
`build_tenant_migration_registry` -- so each refusal case wraps both in one
`pytest.raises`. Which of the two raises is the implementation's choice and is not
asserted. That no registry results IS asserted, and it is the oracle v5.1 names.

CODES
-----
TP-03's `MIGRATION_UNIT_ROLE_REQUIRED` is owner-approved (O-2) and held at the
implementation gate, so it is compared as a string: it is not in `PreconditionCode`
yet.

TP-06/07/08, TP-09 and TP-09a: v5.1 says "refused" and names no code. A test that
accepts ANY refusal passes for the wrong reason -- the defect v11 withdrew
`B-legacy-bypass` for. These rows pin `INSTALL_POLICY_INVALID`, CP1's code for a
malformed declaration. Proposed in v12 as Q-2; ACCEPTED by Codex's v12 review as a
precise contract clarification. O-2's distinct missing-role code is preserved.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.units import build_tenant_migration_registry

ROLES = frozenset({"haloflow_m02_owner", "haloflow_m02_annex"})
ROLE_REQUIRED = "MIGRATION_UNIT_ROLE_REQUIRED"
POLICY_INVALID = "INSTALL_POLICY_INVALID"

# TP-04: an ordinary template that merely CONTAINS function DDL.
ORDINARY_FUNCTION_SQL = (
    "CREATE FUNCTION {schema}.cp2_probe_fn() RETURNS int "
    "LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$;"
)

# TP-05: OD-04's authorized shape -- an ORDINARY, migrator-owned (no execution
# role), SECURITY INVOKER trigger function, installed outside CP1 by owner
# decision. Classifying this as typed would route it into a policy that refuses
# `RETURNS trigger` and breaks the authorized route.
OD04_REJECTOR_SQL = (
    "CREATE FUNCTION {schema}.operation_registry_reject() RETURNS trigger "
    "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp "
    "AS $body$BEGIN RAISE EXCEPTION USING ERRCODE = '0A000', "
    "MESSAGE = 'operation_registry rows are immutable'; END;$body$;"
)


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def compose(definitions: dict[str, Any]) -> Any:
    """The public builder with the sanctioned test flag (R-E12), as v11 does."""

    return build_tenant_migration_registry(
        definitions, approved_execution_roles=ROLES, allow_test_units=True
    )


def refused_at_composition(make: Callable[[], dict[str, Any]]) -> str:
    """Build the definitions AND compose them; return the refusal code.

    Both steps inside one `raises`, so the placement within composition is not
    constrained. No registry is returned, because none must exist.
    """

    with pytest.raises(MigrationUnitRejected) as caught:
        compose(make())
    return caught.value.reason_code


# ---------------------------------------------------------------------------
# Controls.
# ---------------------------------------------------------------------------


def test_tp01_a_valid_typed_definition_builds_and_reports_typed(
    seam: Any, typed_payloads: dict[str, Any], call_spy: Any, monkeypatch: Any
) -> None:
    """TP-01, CONTROL. Every refusal below is meaningful only if this builds.

    Also TP-R13 (offered in v12, required by the v12 review): composition performs
    pure declaration checks only, so it makes ZERO checker calls. The spy is
    installed on the checker seam before composing, and proven observable in this
    same test by one direct call through that seam afterwards. (TP-13 separately
    proves the RUNNER reaches the checker through the same seam.)

    Owed at the binding review (Codex, v13 review): the bound `checker_seam` must
    be a location that COMPOSITION would also go through if it called the
    checker. A seam only the runner uses would make this zero vacuous.
    """

    payload = typed_payloads["first"]
    owner, name = seam.checker_seam()
    checker = call_spy()
    monkeypatch.setattr(owner, name, checker.wrap(getattr(owner, name)))

    registry = compose({payload["migration_id"]: seam.typed_definition(payload)})

    assert checker.calls == [], "composition called the actual-schema checker"
    assert registry.migration_ids == (payload["migration_id"],)
    (unit,) = registry.units
    assert seam.is_typed(unit) is True
    assert unit.execution_role == payload["execution_role"]

    # Observability control for the zero above.
    getattr(owner, name)(encode(payload), schema_key="tenant_aaaaaaaa")
    assert len(checker.calls) == 1 and checker.calls[0].raised is None


def test_tp02_a_valid_ordinary_definition_builds_and_is_not_typed(seam: Any) -> None:
    """TP-02, CONTROL. The ordinary route is unchanged and not typed."""

    registry = compose({"t001_test_cp2": seam.ordinary_definition(
        "CREATE TABLE {schema}.cp2_probe (id int);"
    )})

    (unit,) = registry.units
    assert seam.is_typed(unit) is False


# ---------------------------------------------------------------------------
# Classification is declared, never inferred -- TP-R2, OD-04.
# ---------------------------------------------------------------------------


def test_tp04_an_ordinary_template_containing_create_function_is_not_typed(seam: Any) -> None:
    """TP-04. Template content does not classify a unit."""

    registry = compose({"t001_test_cp2_fn": seam.ordinary_definition(ORDINARY_FUNCTION_SQL)})

    (unit,) = registry.units
    assert seam.is_typed(unit) is False


def test_tp05_the_od04_rejector_shape_builds_and_is_not_typed(seam: Any) -> None:
    """TP-05. OD-04's authorized ordinary route is preserved."""

    registry = compose({"t004_operation_registry_reject": seam.ordinary_definition(
        OD04_REJECTOR_SQL
    )})

    (unit,) = registry.units
    assert unit.execution_role is None  # migrator-owned
    assert seam.is_typed(unit) is False


# ---------------------------------------------------------------------------
# Refusals at composition -- TP-R2a/b/c, TP-R3.
# ---------------------------------------------------------------------------


def test_tp03_a_typed_definition_without_an_execution_role_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-03. Owner-approved code (O-2), at composition, no registry."""

    payload = typed_payloads["first"]
    code = refused_at_composition(
        lambda: {payload["migration_id"]: seam.typed_definition(payload, execution_role=None)}
    )
    assert code == ROLE_REQUIRED


@pytest.mark.parametrize(
    "policy",
    ["missing", "null", "malformed"],
    ids=["TP-06-missing", "TP-07-null", "TP-08-malformed"],
)
def test_tp06_07_08_typed_without_a_valid_policy_is_refused_never_downgraded(
    policy: str, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-06/07/08. Refused -- NOT silently built as an ordinary unit.

    "Malformed" is the policy block with one required key removed, which CP1's
    closed-shape check refuses as `INSTALL_POLICY_INVALID`. A downgrade would
    build a registry, so the `raises` is itself the never-downgraded oracle.
    """

    payload = typed_payloads["first"]
    malformed = {key: value for key, value in payload["policy"].items() if key != "functions"}
    value = {"missing": seam.ABSENT, "null": None, "malformed": malformed}[policy]

    code = refused_at_composition(
        lambda: {payload["migration_id"]: seam.typed_definition(payload, policy=value)}
    )
    assert code == POLICY_INVALID


@pytest.mark.parametrize(
    "kind", ["typed_v2", 1, None], ids=["unknown-name", "wrong-type", "null"]
)
def test_tp09_an_unknown_or_invalid_explicit_kind_is_refused_not_defaulted(
    kind: object, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-09. An explicit kind the vocabulary does not define is refused.

    Defaulting it to either typed or ordinary would be inference, which TP-R2
    forbids.
    """

    payload = typed_payloads["first"]
    code = refused_at_composition(
        lambda: {payload["migration_id"]: seam.typed_definition(payload, kind=kind)}
    )
    assert code == POLICY_INVALID


def test_tp09a_an_ordinary_definition_carrying_a_typed_only_field_is_refused(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """TP-09a. A `policy` block on an ordinary definition is refused, not ignored.

    Ignoring it would install policy-shaped SQL on the ordinary route with the
    policy silently unenforced.
    """

    payload = typed_payloads["first"]
    code = refused_at_composition(
        lambda: {
            payload["migration_id"]: seam.ordinary_definition(
                payload["template"], policy=payload["policy"]
            )
        }
    )
    assert code == POLICY_INVALID
