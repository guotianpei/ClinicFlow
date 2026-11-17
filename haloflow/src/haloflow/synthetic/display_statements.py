"""Option B B5: statement keys for the synthetic display and scenario functions (t095..t097).
SYNTHETIC ONLY: composed into the synthetic catalogue only, never production's.

- DISPLAY_KEY, PRESENT_KEY: staff READs under the B4 view capability (READ_CAPABILITY,
  `m05.followup.read`, the t092 pattern). Ingress and timer workloads do not hold it.
- CLAIM_KEY: the load's WRITE claim under its own capability, held only by the scenario
  load context (`sim_actions.LOAD_CAPABILITIES`). Not activation-protected.
Each statement returns one row whose single column is the function's result.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode
from haloflow.m05.writeback_statements import READ_CAPABILITY

DISPLAY_KEY: Final = "m05.synthetic.display"
CLAIM_KEY: Final = "m05.synthetic.scenario_claim"
PRESENT_KEY: Final = "m05.synthetic.scenario_present"
CLAIM_CAPABILITY: Final = "m05.synthetic.scenario_claim"

B5_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    DISPLAY_KEY: (StatementMode.READ, READ_CAPABILITY,
                  "SELECT m05_synthetic_display(%(item_id)s::uuid)"),
    CLAIM_KEY: (StatementMode.WRITE, CLAIM_CAPABILITY,
                "SELECT m05_synthetic_scenario_claim(%(scenario)s::text, %(refs)s::jsonb)"),
    PRESENT_KEY: (StatementMode.READ, READ_CAPABILITY,
                  "SELECT m05_synthetic_scenario_present(%(refs)s::jsonb)"),
})
