"""M02 role names.

`haloflow_m02_lock_owner` is created by Alembic revision `004` and declared in
M01's provisioning manifest (architecture v4 C-1, C-2). CP2-2a approves no
execution role: `composition.APPROVED_EXECUTION_ROLES` stays empty, and the
role approval lands with CP2-2b (R-B0).
"""

from typing import Final

LOCK_OWNER_ROLE: Final = "haloflow_m02_lock_owner"
