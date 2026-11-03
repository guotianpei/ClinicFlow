"""L-1W W3b: the remove-only closure unit kind ``acl_closure_v1`` and its ACL
amendments (plan v1r1 §4 + addendum 1 pin 5; Codex 150630Z, 151751Z). Unit tier, no
database. Every expected value is written out here, never derived from the code under
test. Imports sit in the test bodies so a missing implementation fails each row in its
own body (class-B red rule).

Interface pinned by these rows (red packet §2):

* ``haloflow.m01.provisioning.units.ACL_CLOSURE_KIND == "acl_closure_v1"``; a closure
  unit is ``UnitDefinition(template="", kind=ACL_CLOSURE_KIND, execution_role=<owner>)``
  -- it carries NO authored template.
* ``haloflow.m01.provisioning.installed_state.AclAmendment(target_unit, activated_by,
  removes)`` with ``removes`` a tuple of ``(grantee, privilege, grantor, is_grantable)``.
* ``build_tenant_migration_registry(..., acl_amendments=<tuple of AclAmendment>)``
  validates the amendments; any violation raises ``MigrationUnitRejected`` with
  ``INSTALLED_STATE_PROFILE_INVALID``.
* ``TenantMigrationRegistry.closure_statements(migration_id, schema_key)`` returns the
  exact statements the runner executes for a closure unit, rendered from the
  declaration only.
* ``installed_state.effective_profile(profile, amendment, activator_version=…,
  checked_version=…)``.
* composition: ``L1W_W3B_TENANT_MIGRATIONS`` (t033-t036), ``APPROVED_ACL_AMENDMENTS``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Final

import pytest

CLOSURE_KIND: Final = "acl_closure_v1"
RUNTIME: Final = "haloflow_runtime"
M05_OWNER: Final = "haloflow_m05_owner"
STAFF_OWNER: Final = "haloflow_staff_owner"
CODE: Final = "INSTALLED_STATE_PROFILE_INVALID"

T015: Final = "t015_m05_prepare_action"
T016: Final = "t016_m05_apply_action"
T023: Final = "t023_m01_staff_admin"
T035: Final = "t035_m05_v1_runtime_closure"
T036: Final = "t036_m01_staff_v1_runtime_closure"

W3B_UNITS: Final = (
    "t033_m01_staff_bootstrap_first_admin_schema",
    "t034_m01_staff_bootstrap_first_admin",
    T035,
    T036,
)
# The declared amendments, written out (plan v1r1 §1).
AMENDMENTS: Final = {
    T015: (T035, ((RUNTIME, "EXECUTE", M05_OWNER, False),)),
    T016: (T035, ((RUNTIME, "EXECUTE", M05_OWNER, False),)),
    T023: (T036, ((RUNTIME, "EXECUTE", STAFF_OWNER, False),)),
}
CLOSURE_STATEMENTS: Final = {
    T035: (
        'REVOKE EXECUTE ON FUNCTION "tenant_x".m05_prepare_action(jsonb) FROM haloflow_runtime;',
        'REVOKE EXECUTE ON FUNCTION "tenant_x".m05_apply_action(jsonb) FROM haloflow_runtime;',
    ),
    T036: (
        'REVOKE EXECUTE ON FUNCTION "tenant_x".m01_staff_admin(jsonb) FROM haloflow_runtime;',
    ),
}


def _compose(amendments: Any, *extra: Any, allow_test_units: bool = False) -> Any:
    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        *extra,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles=APPROVED_INSTALLED_STATE_PROFILES,
        acl_amendments=amendments,
        allow_test_units=allow_test_units,
    )


def _amendment(target: str, activator: str, removes: Any) -> Any:
    from haloflow.m01.provisioning.installed_state import AclAmendment

    return AclAmendment(target_unit=target, activated_by=activator, removes=tuple(removes))


def _approved() -> tuple[Any, ...]:
    from haloflow.composition import APPROVED_ACL_AMENDMENTS

    return tuple(APPROVED_ACL_AMENDMENTS)


def _refused(amendments: Any, *extra: Any, allow_test_units: bool = False) -> None:
    from haloflow.m01.errors import MigrationUnitRejected

    with pytest.raises(MigrationUnitRejected) as caught:
        _compose(amendments, *extra, allow_test_units=allow_test_units)
    assert caught.value.reason_code == CODE


# --- composition of the declared set -------------------------------------------------


def test_cl00_w3b_units_kinds_and_declared_amendments() -> None:
    from haloflow.composition import L1W_W3B_TENANT_MIGRATIONS
    from haloflow.m01.provisioning.units import ACL_CLOSURE_KIND

    assert ACL_CLOSURE_KIND == CLOSURE_KIND
    assert tuple(L1W_W3B_TENANT_MIGRATIONS) == W3B_UNITS
    closure = {k: v for k, v in L1W_W3B_TENANT_MIGRATIONS.items()
               if getattr(v, "kind", None) == CLOSURE_KIND}
    assert {k: (v.execution_role, v.template) for k, v in closure.items()} == {
        T035: (M05_OWNER, ""), T036: (STAFF_OWNER, "")}
    assert {a.target_unit: (a.activated_by, a.removes) for a in _approved()} == AMENDMENTS
    registry = _compose(_approved())
    assert registry.target_version == 36


def test_cl00b_closure_statements_are_rendered_from_the_declaration_only() -> None:
    registry = _compose(_approved())
    for unit, expected in CLOSURE_STATEMENTS.items():
        assert registry.closure_statements(unit, "tenant_x") == expected


# --- AM-01..AM-08, AM-11: refusals ------------------------------------------------------


def test_am01_unknown_target_is_refused() -> None:
    _refused((*_approved(), _amendment("t099_unknown_unit", T035,
                                       [(RUNTIME, "EXECUTE", M05_OWNER, False)])))


def test_am02_removal_outside_the_target_profile_acl_is_refused() -> None:
    bad = [_amendment(T015, T035, [("haloflow_m02_read_owner", "EXECUTE", M05_OWNER, False)])]
    _refused((*[a for a in _approved() if a.target_unit != T015], *bad))


def test_am03_owner_entry_removal_is_refused() -> None:
    bad = [_amendment(T015, T035, [(M05_OWNER, "EXECUTE", M05_OWNER, False)])]
    _refused((*[a for a in _approved() if a.target_unit != T015], *bad))


def test_am04_a_closure_unit_carrying_a_template_is_refused() -> None:
    from haloflow.m01.errors import MigrationUnitRejected
    from haloflow.m01.provisioning.units import ACL_CLOSURE_KIND, UnitDefinition

    with pytest.raises(MigrationUnitRejected):
        UnitDefinition(
            template='REVOKE EXECUTE ON FUNCTION {schema}.m05_prepare_action(jsonb)'
                     ' FROM haloflow_runtime;',
            kind=ACL_CLOSURE_KIND, execution_role=M05_OWNER)


@pytest.mark.parametrize("activator", ["t034_m01_staff_bootstrap_first_admin",
                                       "t033_m01_staff_bootstrap_first_admin_schema", T036])
def test_am05_activator_must_be_a_closure_unit_run_as_the_target_owner(activator: str
                                                                         ) -> None:
    """t034 is typed, t033 ordinary, t036 a closure unit run as the STAFF owner while
    t015 is owned by the M05 owner."""

    others = [a for a in _approved() if a.target_unit != T015]
    _refused((*others, _amendment(T015, activator, [(RUNTIME, "EXECUTE", M05_OWNER, False)])))


def test_am06_two_amendments_on_one_target_are_refused() -> None:
    _refused((*_approved(), _amendment(T015, T035, [(RUNTIME, "EXECUTE", M05_OWNER, False)])))


def test_am07_activator_version_not_above_the_target_is_refused() -> None:
    from types import MappingProxyType

    from haloflow.m01.provisioning.units import ACL_CLOSURE_KIND, UnitDefinition

    early = MappingProxyType({"t010_test_early_closure": UnitDefinition(
        template="", kind=ACL_CLOSURE_KIND, execution_role=M05_OWNER)})
    others = [a for a in _approved() if a.target_unit != T015]
    _refused((*others, _amendment(T015, "t010_test_early_closure",
                                  [(RUNTIME, "EXECUTE", M05_OWNER, False)])),
             early, allow_test_units=True)


@pytest.mark.parametrize("case", ["empty-removes", "duplicate-removes",
                                  "closure-without-amendment"])
def test_am08_empty_duplicate_and_orphan_closures_are_refused(case: str) -> None:
    others = [a for a in _approved() if a.target_unit != T015]
    if case == "empty-removes":
        _refused((*others, _amendment(T015, T035, [])))
    elif case == "duplicate-removes":
        entry = (RUNTIME, "EXECUTE", M05_OWNER, False)
        _refused((*others, _amendment(T015, T035, [entry, entry])))
    else:
        # t036 loses its only amendment: a closure unit with nothing to remove.
        _refused(tuple(a for a in _approved() if a.activated_by != T036))


def test_am11_grantee_outside_the_role_vocabulary_is_refused() -> None:
    others = [a for a in _approved() if a.target_unit != T015]
    _refused((*others, _amendment(T015, T035, [("PUBLIC", "EXECUTE", M05_OWNER, False)])))
    _refused((*others, _amendment(T015, T035,
                                  [('haloflow_runtime"; DROP', "EXECUTE", M05_OWNER, False)])))


# --- AM-09 / AM-10: effective profile ----------------------------------------------------


def test_am09_before_activation_the_accepted_profile_applies_unchanged() -> None:
    from haloflow.composition import APPROVED_INSTALLED_STATE_PROFILES
    from haloflow.m01.provisioning.installed_state import effective_profile

    profile = APPROVED_INSTALLED_STATE_PROFILES[T015]
    amendment = next(a for a in _approved() if a.target_unit == T015)
    assert effective_profile(profile, amendment, activator_version=35,
                             checked_version=34) == profile
    after = effective_profile(profile, amendment, activator_version=35, checked_version=35)
    assert (RUNTIME, "EXECUTE", M05_OWNER, False) in profile.installed_acl
    assert after.installed_acl == tuple(
        e for e in profile.installed_acl if e != (RUNTIME, "EXECUTE", M05_OWNER, False))


def test_am10_every_non_acl_field_is_unchanged_after_activation() -> None:
    from haloflow.composition import APPROVED_INSTALLED_STATE_PROFILES
    from haloflow.m01.provisioning.installed_state import effective_profile

    profile = APPROVED_INSTALLED_STATE_PROFILES[T023]
    amendment = next(a for a in _approved() if a.target_unit == T023)
    after = effective_profile(profile, amendment, activator_version=36, checked_version=36)
    assert replace(after, installed_acl=profile.installed_acl) == profile


# --- AM-12 / AM-13: checksum --------------------------------------------------------------


def _checksum(registry: Any, migration_id: str) -> str:
    return str(next(u for u in registry if u.migration_id == migration_id).checksum)


def test_am12_the_closure_checksum_covers_the_declared_targets() -> None:
    """The only composable variation of t035's declaration is its target set (every
    removal must lie inside the target profile's ACL): dropping the t016 amendment
    leaves t035 amending t015 alone, and its checksum must change. An identical
    declaration composed twice gives the identical checksum (control)."""

    base = _checksum(_compose(_approved()), T035)
    assert len(base) == 64
    assert _checksum(_compose(_approved()), T035) == base
    narrowed = tuple(a for a in _approved() if a.target_unit != T016)
    assert _checksum(_compose(narrowed), T035) != base
    # t036 is untouched by a t035 change.
    assert _checksum(_compose(narrowed), T036) == _checksum(_compose(_approved()), T036)


def test_am12b_the_closure_checksum_is_stable_and_differs_per_unit() -> None:
    first, second = _compose(_approved()), _compose(_approved())
    assert _checksum(first, T035) == _checksum(second, T035)
    assert _checksum(first, T035) != _checksum(first, T036)


def test_cg_rev01_ordinary_role_bearing_revoke_is_still_refused() -> None:
    """CG4 is unchanged: an ordinary (non-closure) unit run as an owner that carries a
    REVOKE is refused with the prohibited-content code (ordinary_content unchanged)."""

    from haloflow.m01.errors import MigrationUnitRejected
    from haloflow.m01.provisioning.ordinary_content import check_rendered
    from haloflow.m01.provisioning.units import UnitDefinition, build_tenant_migration_registry

    template = ("REVOKE EXECUTE ON FUNCTION {schema}.m05_prepare_action(jsonb)"
                " FROM haloflow_runtime;")
    registry = build_tenant_migration_registry(
        {"t001_test_w3b_revoke": UnitDefinition(template, execution_role=M05_OWNER)},
        approved_execution_roles=frozenset({M05_OWNER}),
        allow_test_units=True,
    )
    unit = registry.units[0]
    with pytest.raises(MigrationUnitRejected) as caught:
        check_rendered(unit=unit, rendered=template.replace("{schema}", '"tenant_x"'))
    assert caught.value.reason_code == "ORDINARY_ROLE_CONTENT_PROHIBITED"
