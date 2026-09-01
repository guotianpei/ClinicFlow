"""The fixed database role vocabulary the provisioning path is allowed to use.

Role names reach SQL as identifiers, so they are constants here rather than
configuration. A provisioning session assumes its group role with ``SET ROLE``
before any DDL: the application connects as a LOGIN role that is a member of a
NOLOGIN group role, and without the ``SET ROLE`` every object created would be
owned by the login shim instead of the group role that ``permissions.json``
names.

This is unrelated to R-E6's prohibition. ``SET ROLE`` from a login shim to its
own group role is ordinary PostgreSQL; ``SET ROLE`` *between*
``haloflow_provisioner`` and ``haloflow_migrator`` is refused by the database,
and migration ``003`` deliberately introduces no membership that would change
that.
"""

from typing import Final

PROVISIONER_ROLE: Final = "haloflow_provisioner"
MIGRATOR_ROLE: Final = "haloflow_migrator"
RUNTIME_ROLE: Final = "haloflow_runtime"
AUDIT_PROJECTOR_ROLE: Final = "haloflow_audit_projector"

# Every role name this package may place in an identifier position. Anything
# outside the set is refused before it reaches SQL.
PROVISIONING_ROLES: Final[frozenset[str]] = frozenset(
    {PROVISIONER_ROLE, MIGRATOR_ROLE, RUNTIME_ROLE, AUDIT_PROJECTOR_ROLE}
)
