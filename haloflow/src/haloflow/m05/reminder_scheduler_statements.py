"""L-1S package S-H, checkpoint H3: statement definition for t075 (TEST catalogue only).

`m05.outbound.accepted_pending` binds t075 `m05_outbound_accepted_pending` under the
existing dispatch workload capability, with the held statement's (t056) mode. It is
read-only bookkeeping and NOT activation-protected: registration of what it returns stays
protected through t052. NOT composed.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode
from haloflow.m05.intake_statements import DISPATCH_KEY

ACCEPTED_PENDING_KEY: Final = "m05.outbound.accepted_pending"

SH3_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        ACCEPTED_PENDING_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                               "SELECT m05_outbound_accepted_pending(%(command)s)"),
    }
)

SH3_PROTECTED: Final[frozenset[str]] = frozenset()
