"""L-1S package S-H, checkpoint H2: statement definitions (TEST catalogue only).

Every H2 runtime statement is admitted under the existing dispatch workload's capability
(`m05.intake.dispatch`, as in H1). `SH2_DIRECTORY_STATEMENTS` binds the EXISTING key
`m05.intake.directory` to `m05_directory_v2` (t063), the t053 same-key pattern; the H2
test catalogue drops the S-A directory binding and the closed H1 prepare / claim / read
keys. settings_put has NO runtime statement (D8: support login only). NOT composed.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode
from haloflow.m05.intake_statements import DIRECTORY_KEY, DISPATCH_KEY

EXAMINE_KEY: Final = "m05.outbound.examine"
CONSUME_KEY: Final = "m05.outbound.consume"
CLAIM_V2_KEY: Final = "m05.outbound.claim_v2"
READ_V2_KEY: Final = "m05.outbound.read_v2"
READY_KEY: Final = "m05.outbound.ready"
SNAPSHOT_KEY: Final = "m05.outbound.render_snapshot"

SH2_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        EXAMINE_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                      "SELECT m05_outbound_examine(%(command)s)"),
        CONSUME_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                      "SELECT m05_outbound_consume(%(command)s)"),
        CLAIM_V2_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                       "SELECT m05_outbound_claim_v2(%(command)s)"),
        READ_V2_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                      "SELECT m05_outbound_read_v2(%(command)s)"),
        READY_KEY: (StatementMode.WRITE, DISPATCH_KEY, "SELECT m05_outbound_ready(%(command)s)"),
        SNAPSHOT_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                       "SELECT m05_outbound_render_snapshot(%(command)s)"),
    }
)

SH2_DIRECTORY_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        DIRECTORY_KEY: (StatementMode.WRITE, DIRECTORY_KEY,
                        "SELECT m05_directory_v2(%(command)s)"),
    }
)

# New work is activation-protected (P23); read v2, ready and the snapshot are bookkeeping.
SH2_PROTECTED: Final[frozenset[str]] = frozenset({EXAMINE_KEY, CONSUME_KEY, CLAIM_V2_KEY})
