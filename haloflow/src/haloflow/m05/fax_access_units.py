"""Incoming fax routing FX-IN, package FX5: fax access for staff who can act only (OD-FX11).
Per-tenant migration units t150..t153. SYNTHETIC ONLY (`include_fx5`, on top of FX4, target 153).

Governing: package v1 (`claude_fx5-actor-only-package-v1-2026-11-26.md`), Codex ALIGN for red
tests 273a6baf, red gate accepted 84f0a9a3 (daa459f). OD-FX11 (Rachel 2026-11-26): only staff who
can act see the fax screen; view-only staff have no fax access.

- t150 `m05_fx_staff_read_v3(jsonb)` (runtime, STABLE): t149 with the staff check 'act'.
- t151 `m05_fx_staff_queues_v2(jsonb)` (runtime, STABLE): t145 with the staff check 'act'.
- t152 `m05_fx_staff_action_v2(jsonb)` (runtime): t142 with the staff check 'act' for every
  action, open included. The check stays first: a refused open writes nothing.
- t153 `acl_closure_v1` (the L-1W W3b mechanism; precedent t035 / t036 / t054): removes the
  runtime role's EXECUTE from the superseded staff fax functions t142, t143, t145 and t149
  (FX5_ACL_AMENDMENTS), so no earlier version stays callable.

A failed staff check keeps the internal code ACTOR_INACTIVE (t141 is one boolean; Codex 273a6baf);
the HTTP refusal is the route's uniform 403 FORBIDDEN (platform requirement `fax_read`).
The unit framework never replaces a function, so each change is a new versioned function built
from its predecessor's body by an anchored replacement asserted unique at import.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.installed_state import AclAmendment
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import ACL_CLOSURE_KIND, UnitDefinition, UnitDefinitions
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05.fax_display_units import QUEUES_BODY, QUEUES_ID, READ_V2_BODY, READ_V2_ID
from haloflow.m05.fax_staff_units import ACTION_BODY, ACTION_ID, READ_ID
from haloflow.m05.reminder_units import _definition, _profile

READ_V3_ID: Final = "t150_m05_fx_staff_read_v3"
QUEUES_V2_ID: Final = "t151_m05_fx_staff_queues_v2"
ACTION_V2_ID: Final = "t152_m05_fx_staff_action_v2"
CLOSURE_ID: Final = "t153_m05_fx_revoke_superseded"
FX5_TARGET: Final = 153

READ_V3_NAME: Final = "m05_fx_staff_read_v3"
QUEUES_V2_NAME: Final = "m05_fx_staff_queues_v2"
ACTION_V2_NAME: Final = "m05_fx_staff_action_v2"

# the staff fax functions t153 closes for the runtime role
SUPERSEDED_IDS: Final = (ACTION_ID, READ_ID, QUEUES_ID, READ_V2_ID)


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"FX5 anchor not unique: {old[:48]!r}")
    return text.replace(old, new)


_READ_CHECK: Final = "{schema}.m05_fx_staff_check(v_staff, 'read')"
_ACT_CHECK: Final = "{schema}.m05_fx_staff_check(v_staff, 'act')"
_OPEN_CHECK: Final = ("{schema}.m05_fx_staff_check(v_staff,\n"
                      "            CASE WHEN v_action = 'open' THEN 'read' ELSE 'act' END)")

READ_V3_BODY: Final = _replace_once(READ_V2_BODY, _READ_CHECK, _ACT_CHECK)
QUEUES_V2_BODY: Final = _replace_once(QUEUES_BODY, _READ_CHECK, _ACT_CHECK)
ACTION_V2_BODY: Final = _replace_once(
    _replace_once(ACTION_BODY, "-- 1. the acting staff member (before any fax lock): read to open,"
                               " act to change.",
                  "-- 1. the acting staff member (before any fax lock): act for every action,"
                  " open included (OD-FX11)."),
    _OPEN_CHECK, _ACT_CHECK)

_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    (READ_V3_ID, READ_V3_NAME, "stable", READ_V3_BODY, RUNTIME_ROLE),
    (QUEUES_V2_ID, QUEUES_V2_NAME, "stable", QUEUES_V2_BODY, RUNTIME_ROLE),
    (ACTION_V2_ID, ACTION_V2_NAME, "volatile", ACTION_V2_BODY, RUNTIME_ROLE),
)

_ALL: Final[dict[str, str | UnitDefinition]] = {
    **{mid: _definition(name, vol, body, grantee) for mid, name, vol, body, grantee in _FUNCTIONS},
    CLOSURE_ID: UnitDefinition(template="", kind=ACL_CLOSURE_KIND, execution_role=M05_OWNER_ROLE),
}

FX5_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
FX5_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _FUNCTIONS})
FX5_ACL_AMENDMENTS: Final[tuple[AclAmendment, ...]] = tuple(
    AclAmendment(target_unit=unit, activated_by=CLOSURE_ID,
                 removes=((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),))
    for unit in SUPERSEDED_IDS)
