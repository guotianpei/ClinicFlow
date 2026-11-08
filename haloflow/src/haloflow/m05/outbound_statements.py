"""L-1S package S-H, checkpoint H1: statement definitions (TEST catalogue only).

Every outbound statement is admitted under the existing dispatch workload's capability
(`m05.intake.dispatch`): no new workload role (disclosed in tests/m05/sh_support.py).
`SH_INBOUND_STATEMENTS` binds the EXISTING key `m05.intake.inbound` to the v2 gateway
(t053); the S-H test catalogue drops only that key from the S-A input mapping, so
`IntakeService` is an unchanged caller. NOT composed into production.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode
from haloflow.m05.intake_statements import DISPATCH_KEY, INBOUND_KEY

PREPARE_KEY: Final = "m05.outbound.prepare"
CLAIM_KEY: Final = "m05.outbound.claim"
OUTCOME_KEY: Final = "m05.outbound.outcome"
EXPIRE_KEY: Final = "m05.outbound.expire"
REGISTER_KEY: Final = "m05.outbound.register"
READ_KEY: Final = "m05.outbound.read"
HELD_KEY: Final = "m05.outbound.held"

SH_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        PREPARE_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                      "SELECT m05_outbound_prepare(%(command)s)"),
        CLAIM_KEY: (StatementMode.WRITE, DISPATCH_KEY, "SELECT m05_outbound_claim(%(command)s)"),
        OUTCOME_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                      "SELECT m05_outbound_outcome(%(command)s)"),
        EXPIRE_KEY: (StatementMode.WRITE, DISPATCH_KEY, "SELECT m05_outbound_expire(%(command)s)"),
        REGISTER_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                       "SELECT m05_register_outbound(%(command)s)"),
        READ_KEY: (StatementMode.WRITE, DISPATCH_KEY, "SELECT m05_outbound_read(%(command)s)"),
        HELD_KEY: (StatementMode.WRITE, DISPATCH_KEY, "SELECT m05_outbound_held(%(command)s)"),
    }
)

SH_INBOUND_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        INBOUND_KEY: (StatementMode.WRITE, INBOUND_KEY,
                      "SELECT m05_register_inbound_v2(%(command)s)"),
    }
)

# D5: new work is activation-protected; outcome, expire, read and held are bookkeeping
# (authorization still applies: capability and workload checks are unchanged).
SH_PROTECTED: Final[frozenset[str]] = frozenset({PREPARE_KEY, CLAIM_KEY, REGISTER_KEY})
