"""L-1S package S-B statement definitions (TEST catalogue only).

One READ statement for the t045 selector. NOT composed into production:
`haloflow.composition.APPROVED_MODULE_STATEMENTS` does not name this set (SB-S01);
the S-B test catalogue composes it beside the production sets and S-A's
`SA_STATEMENTS`, which this package does not change (SB-S02).
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode

DUE_KEY: Final = "m05.intake.due_windows"

SB_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        DUE_KEY: (StatementMode.READ, DUE_KEY, "SELECT m05_due_windows(%(command)s)"),
    }
)
