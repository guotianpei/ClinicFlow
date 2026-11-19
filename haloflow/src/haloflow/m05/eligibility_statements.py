"""Eligibility checks E1: statement keys. SYNTHETIC ONLY: composed into the synthetic catalogue
behind `include_el1`, never production's.

- `EL1_REBOUND`: the existing directory key `m05.intake.directory` rebound to
  `m05_directory_v4` (t103), the t053 same-key pattern; it replaces S-I's binding (v3) when
  E1 is composed. Same capability (`m05.intake.directory`).
- `EL1_STATEMENTS`: `m05.eligibility.tick` -> `m05_el_tick` (t104) under its own capability,
  held only by the timer workload's eligibility context. Not activation-protected (the
  eligibility switch is `m05_el_settings.enabled`).
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode
from haloflow.m05.intake_statements import DIRECTORY_KEY

TICK_KEY: Final = "m05.eligibility.tick"
TICK_CAPABILITY: Final = "m05.eligibility.tick"

EL1_REBOUND: Final[StatementDefinitions] = MappingProxyType({
    DIRECTORY_KEY: (StatementMode.WRITE, DIRECTORY_KEY, "SELECT m05_directory_v4(%(command)s)"),
})
EL1_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    TICK_KEY: (StatementMode.WRITE, TICK_CAPABILITY, "SELECT m05_el_tick(%(command)s)"),
})
