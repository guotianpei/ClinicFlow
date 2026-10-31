"""M02 role names.

`haloflow_m02_lock_owner` is created by Alembic revision `004` and declared in
M01's provisioning manifest (architecture v4 C-1, C-2). CP2-2b approves it as
the one execution role in `composition.APPROVED_EXECUTION_ROLES` (R-B0): the
typed gateway unit `t003` runs as it.
"""

from typing import Final

LOCK_OWNER_ROLE: Final = "haloflow_m02_lock_owner"

# UC1 part A (delta v1r1 Δ4, decision TB-3). Created by Alembic revision `005`.
APPEND_OWNER_ROLE: Final = "haloflow_m02_append_owner"
PROJECTION_OWNER_ROLE: Final = "haloflow_m02_projection_owner"
READ_OWNER_ROLE: Final = "haloflow_m02_read_owner"
M05_OWNER_ROLE: Final = "haloflow_m05_owner"
