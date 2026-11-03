"""M02 production statement definitions.

UC1 part A (Codex ALIGN 20261031T142215Z item 6, the approved amendment to the
module-scope list): exactly one statement, the observation append, called by
`haloflow.m02.observation` only. Production definition sets live only in
`haloflow.mNN.statements` (repository control); the composition root composes this
set and `statement_catalog.json` pins it.

The query names the tenant function unqualified: the M01 gateway pins the
transaction's search_path to `pg_catalog, <tenant schema>, pg_temp`, and statement
definitions may not carry a schema qualifier.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode

OBSERVATION_APPEND_KEY: Final = "m02.observation.append"
OBSERVATION_APPEND_CAPABILITY: Final = "m02.observation.append"

M02_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        OBSERVATION_APPEND_KEY: (
            StatementMode.WRITE,
            OBSERVATION_APPEND_CAPABILITY,
            "SELECT m02_append_observation(%(command)s)",
        ),
    }
)
