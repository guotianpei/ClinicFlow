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
previously assembled in `tests/m05/si_support.py`. Its output is pinned against the frozen
pre-move manifest `tests/synthetic/b1_premove_manifest.json`, which was generated at bc28c90
before this module existed (B1-E1..E3). Every unit, profile, amendment and statement part
here is an existing, reviewed `src` constant; nothing new is defined.

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

SYNTHETIC_TARGET: Final = 94

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


def build_synthetic_catalog() -> Any:
    """The S-I TEST statement catalogue (synthetic only). Pinned by B1-E2."""

    return build_statement_catalog(*_parts(), SI_REBOUND, SI_STATEMENTS,
                                   activation_protected=_protected())


def build_synthetic_registry() -> Any:
    """The S-I TEST per-tenant migration registry, target 94 (synthetic only). Pinned by
    B1-E1."""

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, SA_TENANT_MIGRATIONS,
        # SB/SC sets are un-annotated MappingProxyType literals in their modules; the
        # cast only states their declared role (no conversion, no copy).
        cast("UnitDefinitions", SB_TENANT_MIGRATIONS),
        cast("UnitDefinitions", SC_TENANT_MIGRATIONS), SH_TENANT_MIGRATIONS, SH2_TENANT_MIGRATIONS,
        SH3_TENANT_MIGRATIONS, SI_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES,
                                  **SA_INSTALLED_STATE_PROFILES,
                                  **SB_INSTALLED_STATE_PROFILES,
                                  **SC_INSTALLED_STATE_PROFILES,
                                  **SH_INSTALLED_STATE_PROFILES,
                                  **SH2_INSTALLED_STATE_PROFILES,
                                  **SH3_INSTALLED_STATE_PROFILES,
                                  **SI_INSTALLED_STATE_PROFILES},
        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *SH_ACL_AMENDMENTS, *SH2_ACL_AMENDMENTS,
                        *SI_ACL_AMENDMENTS),
    )
