"""L-1S package S-A statement definitions (TEST catalogue only).

One WRITE statement per runtime intake gateway (t038..t041). NOT composed into
production: `haloflow.composition.APPROVED_MODULE_STATEMENTS` does not name this set
(control row SA-S01); the S-A test catalogue composes it beside the production sets.
The activation functions (t042/t043) have no statement key (D-S1 "enable none").
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode

DIRECTORY_KEY: Final = "m05.intake.directory"
DISPATCH_KEY: Final = "m05.intake.dispatch"
INBOUND_KEY: Final = "m05.intake.inbound"
CLOSE_KEY: Final = "m05.intake.close"

SA_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        DIRECTORY_KEY: (StatementMode.WRITE, DIRECTORY_KEY,
                        "SELECT m05_directory_put(%(command)s)"),
        DISPATCH_KEY: (StatementMode.WRITE, DISPATCH_KEY,
                       "SELECT m05_register_dispatch(%(command)s)"),
        INBOUND_KEY: (StatementMode.WRITE, INBOUND_KEY,
                      "SELECT m05_register_inbound(%(command)s)"),
        CLOSE_KEY: (StatementMode.WRITE, CLOSE_KEY, "SELECT m05_close_window(%(command)s)"),
    }
)

# L-1S S-C (D-S1): every statement above is gated by the activation probe (the module
# owns this declaration; the catalogue never infers it).
SA_PROTECTED: Final[frozenset[str]] = frozenset(SA_STATEMENTS)
