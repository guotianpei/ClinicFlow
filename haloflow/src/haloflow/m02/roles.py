"""M02 role names.

`haloflow_m02_lock_owner` is created by Alembic revision `004` and declared in
M01's provisioning manifest (architecture v4 C-1, C-2). CP2-2b approves it as
the one execution role in `composition.APPROVED_EXECUTION_ROLES` (R-B0): the
typed gateway unit `t003` runs as it.
"""

from typing import Final

LOCK_OWNER_ROLE: Final = "haloflow_m02_lock_owner"
