"""The synthetic composition root (Option B increment B1): SYNTHETIC / TEST ONLY.

This is the ONE place outside `haloflow/composition.py` that may compose a statement
catalogue or a tenant migration registry. The allowance in
`tests/m01/test_repository_controls.py` (B1-R1..R3) is bounded to exactly this file:
- `build_statement_catalog` may be called only inside `build_synthetic_catalog`;
- `build_tenant_migration_registry` may be called only inside `build_synthetic_registry`;
- both are imported unaliased, at module level.
Production never imports this module (B1-C1/C1r), and `allow_test_units` is never passed
(R-E12).

What it composes is the S-I TEST assembly (registry target 94) that the test suite
previously assembled in `tests/m05/si_support.py`, plus (Option B B5) exactly the three
synthetic display/scenario units t095..t097 and their three statement keys
(`synthetic.display_units`, `synthetic.display_statements`): target 97. Everything except the
B5 delta is pinned against the frozen pre-move manifest
`tests/synthetic/b1_premove_manifest.json`, generated at bc28c90 (B1-E1..E3); the delta is
pinned by B5-E1/E2. Every other unit, profile, amendment and statement part is an existing,
reviewed `src` constant.

The TEST profile SQL below is a FIXTURE for synthetic installs only. It activates the test
families and event types and maps the synthetic workloads; every intake-map row is
`active_in_production = false`.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, cast

from haloflow.composition import (
    APPROVED_ACL_AMENDMENTS,
    APPROVED_EXECUTION_ROLES,
    APPROVED_INSTALLED_STATE_PROFILES,
    APPROVED_MODULE_STATEMENTS,
    APPROVED_TENANT_MIGRATIONS,
)
from haloflow.m01.provisioning.units import UnitDefinitions, build_tenant_migration_registry
from haloflow.m01.statements import build_statement_catalog
from haloflow.m05.activation_units import SC_INSTALLED_STATE_PROFILES, SC_TENANT_MIGRATIONS
from haloflow.m05.due_statements import SB_PROTECTED, SB_STATEMENTS
from haloflow.m05.due_units import SB_INSTALLED_STATE_PROFILES, SB_TENANT_MIGRATIONS
from haloflow.m05.eligibility_case_units import (
    EL3A_INSTALLED_STATE_PROFILES,
    EL3A_TENANT_MIGRATIONS,
)
from haloflow.m05.eligibility_statements import (
    EL1_REBOUND,
    EL1_STATEMENTS,
    EL2_STATEMENTS,
    EL3A_STATEMENTS,
)
from haloflow.m05.eligibility_units import (
    EL1_ACL_AMENDMENTS,
    EL1_INSTALLED_STATE_PROFILES,
    EL1_TENANT_MIGRATIONS,
)
from haloflow.m05.eligibility_worker_units import (
    EL2_INSTALLED_STATE_PROFILES,
    EL2_TENANT_MIGRATIONS,
)
from haloflow.m05.intake_statements import (
    DIRECTORY_KEY,
    INBOUND_KEY,
    SA_PROTECTED,
    SA_STATEMENTS,
)
from haloflow.m05.intake_units import SA_INSTALLED_STATE_PROFILES, SA_TENANT_MIGRATIONS
from haloflow.m05.outbound_statements import (
    CLAIM_KEY,
    PREPARE_KEY,
    READ_KEY,
    SH_INBOUND_STATEMENTS,
    SH_PROTECTED,
    SH_STATEMENTS,
)
from haloflow.m05.outbound_units import (
    SH_ACL_AMENDMENTS,
    SH_INSTALLED_STATE_PROFILES,
    SH_TENANT_MIGRATIONS,
)
from haloflow.m05.reminder_scheduler_statements import SH3_PROTECTED, SH3_STATEMENTS
from haloflow.m05.reminder_scheduler_units import (
    SH3_INSTALLED_STATE_PROFILES,
    SH3_TENANT_MIGRATIONS,
)
from haloflow.m05.reminder_statements import (
    SH2_DIRECTORY_STATEMENTS,
    SH2_PROTECTED,
    SH2_STATEMENTS,
)
from haloflow.m05.reminder_units import (
    SH2_ACL_AMENDMENTS,
    SH2_INSTALLED_STATE_PROFILES,
    SH2_TENANT_MIGRATIONS,
)
from haloflow.m05.statements import M05_PROTECTED
from haloflow.m05.writeback_statements import SI_PROTECTED, SI_REBOUND, SI_STATEMENTS
from haloflow.m05.writeback_units import (
    SI_ACL_AMENDMENTS,
    SI_INSTALLED_STATE_PROFILES,
    SI_TENANT_MIGRATIONS,
)
from haloflow.synthetic.display_statements import B5_STATEMENTS
from haloflow.synthetic.display_units import B5_INSTALLED_STATE_PROFILES, B5_TENANT_MIGRATIONS

SYNTHETIC_TARGET: Final = 97
S_I_TARGET: Final = 94          # the S-I TEST assembly alone (include_b5=False)

# --- synthetic contract and workload identities (FIXTURE values, unchanged) --------------

FAMILY: Final = "m05_intake"
T_DISPATCHED: Final = "m05_reminder_dispatched"
T_REPLY: Final = "m05_reminder_reply_received"
T_NO_REPLY: Final = "m05_reminder_no_reply"
T_CHECK: Final = "m05_writeback_check_raised"
ITEM_KIND_CHECK: Final = "ehr_confirmation_check"
CHECK_REASONS: Final = ("status_changed", "declined_or_unknown", "ehr_unreachable",
                        "could_not_verify", "no_baseline")
W_DIRECTORY: Final = "00000000-0000-4000-8000-00000005a001"
W_DISPATCH: Final = "00000000-0000-4000-8000-00000005a002"
W_INGRESS: Final = "00000000-0000-4000-8000-00000005a003"
W_TIMER: Final = "00000000-0000-4000-8000-00000005a004"

# Eligibility E3a (synthetic; with `include_el3a`): activates the evidence event type and maps
# its opening observations (discriminator `open_kind`) to the three eligibility item kinds,
# produced by the timer workload. A TEST profile, as the S-I one.
EL3A_EVENT_TYPE: Final = "m05_eligibility_case_event"
EL3A_KINDS: Final = ("eligibility_inactive", "eligibility_inconclusive", "eligibility_manual")
EL3A_TEST_PROFILE_SQL: Final = f"""
UPDATE {{schema}}.event_types SET is_active = true WHERE event_type = '{EL3A_EVENT_TYPE}';
INSERT INTO {{schema}}.m05_intake_map (source_event_type, source_contract_version,
    discriminator_path, discriminator_value, item_kind, producer_workload_id,
    active_in_production) VALUES
""" + ",\n".join(
    f"    ('{EL3A_EVENT_TYPE}', 1, 'open_kind', '{kind}', '{kind}', '{W_TIMER}', false)"
    for kind in EL3A_KINDS) + ";\n"

SA_TEST_PROFILE_SQL: Final = f"""
UPDATE {{schema}}.ref_action_families SET is_active = true WHERE action_family = '{FAMILY}';
UPDATE {{schema}}.event_types SET is_active = true
 WHERE event_type IN ('{T_DISPATCHED}', '{T_REPLY}', '{T_NO_REPLY}');
INSERT INTO {{schema}}.m05_intake_workloads (workload_role, workload_id) VALUES
    ('directory', '{W_DIRECTORY}'), ('dispatch', '{W_DISPATCH}'),
    ('ingress', '{W_INGRESS}'), ('timer', '{W_TIMER}');
INSERT INTO {{schema}}.m05_intake_map (source_event_type, source_contract_version,
    discriminator_path, discriminator_value, item_kind, producer_workload_id,
    active_in_production) VALUES
    ('{T_REPLY}', 1, 'triage_kind', 'reschedule_request', 'reschedule_request',
     '{W_INGRESS}', false),
    ('{T_REPLY}', 1, 'triage_kind', 'cancel_request', 'cancel_request', '{W_INGRESS}', false),
    ('{T_REPLY}', 1, 'triage_kind', 'unclear_reply', 'unclear_reply', '{W_INGRESS}', false),
    ('{T_NO_REPLY}', 1, 'triage_kind', 'no_reply', 'no_reply', '{W_TIMER}', false);
"""

SI_TEST_PROFILE_SQL: Final = f"""
UPDATE {{schema}}.event_types SET is_active = true WHERE event_type = '{T_CHECK}';
INSERT INTO {{schema}}.m05_intake_map (source_event_type, source_contract_version,
    discriminator_path, discriminator_value, item_kind, producer_workload_id,
    active_in_production) VALUES
""" + ",\n".join(
    f"    ('{T_CHECK}', 1, 'reason', '{reason}', '{ITEM_KIND_CHECK}', '{W_DISPATCH}', false)"
    for reason in CHECK_REASONS) + ";\n"

# F-R1 (Option B B3a; Codex `codex_b3a-fr1-review.md`, relay 20261115T023713Z): the staff
# resolution activation. t013 installs the m05_followup family, the
# m05_followup_resolution_asserted event type and the m02 resolver m05_followup_item_v1
# INACTIVE; without this the synthetic install refuses every staff resolve
# (CONTRACT_UNSUPPORTED). The same statements as the accepted test fixture
# `tests/m05/m05_support.B4_RESOLUTION_PROFILE_SQL` (FR-E1 pins the equality). Executed in
# the installer's `profile` phase transaction.
RESOLUTION_PROFILE_SQL: Final = """
UPDATE {schema}.ref_action_families SET is_active = true WHERE action_family = 'm05_followup';
UPDATE {schema}.event_types SET is_active = true
 WHERE event_type = 'm05_followup_resolution_asserted';
UPDATE {schema}.m02_link_resolvers SET is_active = true
 WHERE resolver_id = 'm05_followup_item_v1';
"""

# The H2 keys that S-H2 closed and S-I re-bound (formerly tests/m05/sh2_support.CLOSED_KEYS).
_CLOSED_KEYS: Final = frozenset({PREPARE_KEY, CLAIM_KEY, READ_KEY})


def _protected() -> frozenset[str]:
    """The activation-protected set: the H2 set (closed keys removed), plus H3, plus S-I."""

    h1 = frozenset(M05_PROTECTED | SA_PROTECTED | SB_PROTECTED) | SH_PROTECTED
    h2 = frozenset(h1 | SH2_PROTECTED) - _CLOSED_KEYS
    return h2 | frozenset(SH3_PROTECTED) | frozenset(SI_PROTECTED)


def _parts() -> tuple[Any, ...]:
    """The statement parts, in the order the test-side assembly composed them."""

    sa_without_inbound = {k: v for k, v in SA_STATEMENTS.items() if k != INBOUND_KEY}
    sa_part = MappingProxyType({k: v for k, v in sa_without_inbound.items()
                                if k != DIRECTORY_KEY})
    sh_part = MappingProxyType({k: v for k, v in SH_STATEMENTS.items()
                                if k not in _CLOSED_KEYS})
    rebound = frozenset(SI_REBOUND)
    parts = (*APPROVED_MODULE_STATEMENTS, sa_part, SB_STATEMENTS, sh_part,
             SH_INBOUND_STATEMENTS, SH2_STATEMENTS, SH2_DIRECTORY_STATEMENTS, SH3_STATEMENTS)
    return tuple(MappingProxyType({k: v for k, v in part.items() if k not in rebound})
                 for part in parts)


def build_synthetic_catalog(*, include_b5: bool = True, include_el1: bool = False,
                            include_el2: bool = False, include_el3a: bool = False) -> Any:
    """The synthetic statement catalogue: the S-I TEST catalogue plus (by default) the three
    B5 keys. `include_b5=False` is the S-I TEST catalogue alone, as the M05 S-I/S-J test
    harness uses it (`tests/m05/si_support.si_catalog`). Pinned by B1-E2 / B5-E2.
    `include_el1=True` (eligibility E1; default off, so every existing pin is unchanged)
    rebinds the directory key to `m05_directory_v4` and adds the eligibility tick key.
    `include_el2=True` (requires `include_el1`) adds the seven eligibility worker keys.
    `include_el3a=True` (requires `include_el2`) adds the case apply / create / view keys."""

    if include_el2 and not include_el1:
        raise ValueError("eligibility E2 composes on E1")
    if include_el3a and not include_el2:
        raise ValueError("eligibility E3a composes on E2")
    extra = (B5_STATEMENTS,) if include_b5 else ()
    rebound = SI_REBOUND
    if include_el1:
        rebound = MappingProxyType({k: v for k, v in SI_REBOUND.items() if k not in EL1_REBOUND})
        extra = (*extra, EL1_REBOUND, EL1_STATEMENTS)
    if include_el2:
        extra = (*extra, EL2_STATEMENTS)
    if include_el3a:
        extra = (*extra, EL3A_STATEMENTS)
    return build_statement_catalog(*_parts(), rebound, SI_STATEMENTS, *extra,
                                   activation_protected=_protected())


def build_synthetic_registry(*, include_b5: bool = True, include_el1: bool = False,
                             include_el2: bool = False, include_el3a: bool = False) -> Any:
    """The synthetic per-tenant migration registry: the S-I TEST units plus (by default) the
    B5 units, target 97 (synthetic only). `include_b5=False` is the S-I TEST registry alone,
    target 94, as the M05 S-I/S-J test harness uses it (`tests/m05/si_support.si_registry`).
    Pinned by B1-E1 (all but the B5 delta) and B5-E1 (the delta)."""

    if include_el1 and not include_b5:
        raise ValueError("eligibility E1 composes on the synthetic (B5) registry")
    if include_el2 and not include_el1:
        raise ValueError("eligibility E2 composes on E1")
    if include_el3a and not include_el2:
        raise ValueError("eligibility E3a composes on E2")
    b5_units = (B5_TENANT_MIGRATIONS,) if include_b5 else ()
    b5_profiles = dict(B5_INSTALLED_STATE_PROFILES) if include_b5 else {}
    el1_units = (EL1_TENANT_MIGRATIONS,) if include_el1 else ()
    el1_profiles = dict(EL1_INSTALLED_STATE_PROFILES) if include_el1 else {}
    el1_amendments = EL1_ACL_AMENDMENTS if include_el1 else ()
    el2_units = (EL2_TENANT_MIGRATIONS,) if include_el2 else ()
    el2_profiles = dict(EL2_INSTALLED_STATE_PROFILES) if include_el2 else {}
    el3a_units = (EL3A_TENANT_MIGRATIONS,) if include_el3a else ()
    el3a_profiles = dict(EL3A_INSTALLED_STATE_PROFILES) if include_el3a else {}
    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, SA_TENANT_MIGRATIONS,
        # SB/SC sets are un-annotated MappingProxyType literals in their modules; the
        # cast only states their declared role (no conversion, no copy).
        cast("UnitDefinitions", SB_TENANT_MIGRATIONS),
        cast("UnitDefinitions", SC_TENANT_MIGRATIONS), SH_TENANT_MIGRATIONS, SH2_TENANT_MIGRATIONS,
        SH3_TENANT_MIGRATIONS, SI_TENANT_MIGRATIONS, *b5_units, *el1_units, *el2_units, *el3a_units,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES,
                                  **SA_INSTALLED_STATE_PROFILES,
                                  **SB_INSTALLED_STATE_PROFILES,
                                  **SC_INSTALLED_STATE_PROFILES,
                                  **SH_INSTALLED_STATE_PROFILES,
                                  **SH2_INSTALLED_STATE_PROFILES,
                                  **SH3_INSTALLED_STATE_PROFILES,
                                  **SI_INSTALLED_STATE_PROFILES,
                                  **b5_profiles, **el1_profiles, **el2_profiles,
                                  **el3a_profiles},
        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *SH_ACL_AMENDMENTS, *SH2_ACL_AMENDMENTS,
                        *SI_ACL_AMENDMENTS, *el1_amendments),
    )
