"""B5 expected values (package v2 + v2r1, aligned by Codex 4b0a5448).

Every value here is written out independently. None is derived from the code under
test.
"""

from __future__ import annotations

from typing import Final

import m05_support as m05
import staff_support as st

PRODUCTION_TARGET: Final = 25
PRODUCTION_SUPPORTED: Final = frozenset({PRODUCTION_TARGET})

PRODUCTION_UNIT_IDS: Final = m05.PART_A_UNIT_IDS + m05.M05_UNIT_IDS + st.STAFF_UNIT_IDS

M02_ROLES: Final = frozenset(
    {
        "haloflow_m02_lock_owner",
        "haloflow_m02_append_owner",
        "haloflow_m02_projection_owner",
        "haloflow_m02_read_owner",
    }
)
EXECUTION_ROLES: Final = M02_ROLES | {"haloflow_m05_owner", "haloflow_staff_owner"}

# Every production statement after B5: key -> (mode, required capability).
STATEMENTS: Final = {
    "m02.observation.append": ("write", "m02.observation.append"),
    "m01.staff.admin": ("write", "m01.staff.admin"),
    "m01.staff.display": ("read", "m01.staff.display"),
    "m01.staff.followup_reader": ("read", "m05.followup.read"),
    "m05.followup.create": ("write", "m05.followup.create"),
    "m05.followup.prepare": ("write", "m05.followup.prepare"),
    "m05.followup.apply": ("write", "m05.followup.apply"),
    "m05.followup.get": ("read", "m05.followup.read"),
    "m05.followup.list": ("read", "m05.followup.read"),
    "m05.followup.history": ("read", "m05.followup.read"),
    "m05.followup.note.read": ("read", "m05.followup.note.read"),
}
M01_STATEMENT_KEYS: Final = frozenset(
    {"m01.staff.admin", "m01.staff.display", "m01.staff.followup_reader"}
)

# provisioning.json tenant_schema_privileges after B5. At f9d90e1 the two
# owners held ["USAGE"]; every other profile is unchanged (measured 2026-11-02).
TENANT_SCHEMA_PRIVILEGES: Final = {
    "haloflow_m02_append_owner": ["USAGE", "CREATE"],
    "haloflow_m02_lock_owner": ["USAGE", "CREATE"],
    "haloflow_m02_projection_owner": ["USAGE", "CREATE"],
    "haloflow_m02_read_owner": ["USAGE", "CREATE"],
    "haloflow_m05_owner": ["USAGE", "CREATE"],
    "haloflow_staff_owner": ["USAGE", "CREATE"],
}

PRODUCER: Final = "m05_followup"
STARTUP_PATH: Final = "src/haloflow/startup.py"

# Activation flags (v2 section 4).
M05_FAMILY: Final = "m05_followup"
M05_TYPE: Final = "m05_followup_resolution_asserted"
M05_RESOLVER: Final = "m05_followup_item_v1"

REASONS: Final = frozenset(
    {
        "STARTUP_NO_TENANTS",
        "STARTUP_TENANT_DUPLICATE",
        "STARTUP_CONNECTION_FAILED",
        "TENANT_NOT_ACTIVE",
        "SCHEMA_VERSION_UNSUPPORTED",
        "INSTALLED_STATE_DRIFT",
        "GENERATION_UNREADABLE",
        "ROUTE_NOT_INSTALLED",
        "SIGNAL_RULE_REQUIRED",
        "M05_ACTIVATION_NOT_AUTHORIZED",
        "TEST_FAMILY_ACTIVE_IN_PRODUCTION",
        "SYNTHETIC_DISPATCH_IN_PRODUCTION",
        "TEST_INTAKE_PRESENT",
        "COMPOSITION_INVALID",
    }
)
