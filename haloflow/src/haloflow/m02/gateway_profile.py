"""The fixed installed-state profile of the M02 lock gateway (CP2-2b, C-4).

Architecture v3 section 5.3, the "profile" column. These are the facts about the
installed `m02_lock_operation(uuid)` that the typed declaration does not express:
the function kind, the result shape, the owner, and the grantor and grantability of
every ACL entry, including the owner's own entry, which the Form A declaration
does not declare (E6).

The composition root binds this profile to `t003_m02_lock_operation` by migration
id. Composition checks every agreement cell against the unit's declaration
statically (M01 `installed_state.check_profile_agreement`); neither overrides the
other, and no expected value here comes from observed state (R-B9.3).
"""

from typing import Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m02.roles import LOCK_OWNER_ROLE

LOCK_OPERATION_PROFILE: Final = InstalledStateProfile(
    function_name="m02_lock_operation",
    argument_types=("uuid",),
    owner=LOCK_OWNER_ROLE,
    execution_role=LOCK_OWNER_ROLE,
    prokind="f",
    return_type="uuid",
    returns_set=False,
    language="plpgsql",
    security_definer=True,
    volatility="v",
    parallel="u",
    strict=False,
    installed_acl=(
        (LOCK_OWNER_ROLE, "EXECUTE", LOCK_OWNER_ROLE, False),
        (RUNTIME_ROLE, "EXECUTE", LOCK_OWNER_ROLE, False),
    ),
)
