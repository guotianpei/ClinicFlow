"""L-1S package S-I: statement definitions (TEST catalogue only; NOT composed).

- `SI_STATEMENTS`: the new keys. The baseline capture runs under the existing dispatch
  workload capability (`m05.intake.dispatch`); every other write-back statement under the
  new capability `m05.writeback`; the staff reason read under `m05.followup.read`.
- `SI_REBOUND`: the EXISTING keys rebound to their S-I versions (the t053 same-key
  pattern): directory -> v3 (t077), follow-up create -> v2 (t089), prepare / apply -> v3
  (t090 / t091). The S-I test catalogue drops every rebound key from its source part.
- `SI_PROTECTED`: new work is activation-protected (discover, the check claim, decide,
  spend). The reconcile claim, settlement, due, capture and the reason read are not: a
  permit spent before the switch went off still completes, and staff still see the item.
- `CREATE_SETTLED_KEY` (t094): the settlement's recovery create, under the existing create
  capability and NOT protected; the ordinary create key stays protected as inherited.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode
from haloflow.m05.intake_statements import DIRECTORY_KEY, DISPATCH_KEY

WRITEBACK_CAPABILITY: Final = "m05.writeback"
READ_CAPABILITY: Final = "m05.followup.read"

CAPTURE_KEY: Final = "m05.writeback.capture"
DISCOVER_KEY: Final = "m05.writeback.discover"
DUE_KEY: Final = "m05.writeback.due"
CLAIM_KEY: Final = "m05.writeback.claim"
CLAIM_RECONCILE_KEY: Final = "m05.writeback.claim_reconcile"
DECIDE_KEY: Final = "m05.writeback.decide"
SPEND_KEY: Final = "m05.writeback.spend"
SETTLE_KEY: Final = "m05.writeback.settle"
REASON_KEY: Final = "m05.followup.reason"
CREATE_SETTLED_KEY: Final = "m05.followup.create_settled"

CREATE_KEY: Final = "m05.followup.create"
PREPARE_KEY: Final = "m05.followup.prepare"
APPLY_KEY: Final = "m05.followup.apply"


def _write(capability: str, function: str) -> tuple[StatementMode, str, str]:
    return (StatementMode.WRITE, capability, f"SELECT {function}(%(command)s)")


SI_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        CAPTURE_KEY: _write(DISPATCH_KEY, "m05_writeback_capture_baseline"),
        DISCOVER_KEY: _write(WRITEBACK_CAPABILITY, "m05_writeback_discover"),
        DUE_KEY: _write(WRITEBACK_CAPABILITY, "m05_writeback_due"),
        CLAIM_KEY: _write(WRITEBACK_CAPABILITY, "m05_writeback_claim"),
        CLAIM_RECONCILE_KEY: _write(WRITEBACK_CAPABILITY, "m05_writeback_claim_reconcile"),
        DECIDE_KEY: _write(WRITEBACK_CAPABILITY, "m05_writeback_decide"),
        SPEND_KEY: _write(WRITEBACK_CAPABILITY, "m05_writeback_spend"),
        SETTLE_KEY: _write(WRITEBACK_CAPABILITY, "m05_writeback_settle"),
        CREATE_SETTLED_KEY: (StatementMode.WRITE, "m05.followup.create",
                             "SELECT m05_writeback_create_settled_item(%(command)s)"),
        REASON_KEY: (StatementMode.READ, READ_CAPABILITY,
                     "SELECT m05_followup_reason(%(item_id)s)"),
    }
)

SI_REBOUND: Final[StatementDefinitions] = MappingProxyType(
    {
        DIRECTORY_KEY: _write(DIRECTORY_KEY, "m05_directory_v3"),
        CREATE_KEY: _write(CREATE_KEY, "m05_create_item_v2"),
        PREPARE_KEY: _write(PREPARE_KEY, "m05_prepare_action_v3"),
        APPLY_KEY: _write(APPLY_KEY, "m05_apply_action_v3"),
    }
)

SI_PROTECTED: Final[frozenset[str]] = frozenset({DISCOVER_KEY, CLAIM_KEY, DECIDE_KEY,
                                                SPEND_KEY})

# No statement key is closed by S-I: the closed functions' keys are rebound instead.
SI_CLOSED_KEYS: Final[frozenset[str]] = frozenset()
