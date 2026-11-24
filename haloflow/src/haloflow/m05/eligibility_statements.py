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

# --- E2 (synthetic; composed behind `include_el2`) ---------------------------------------
# The worker gateways (t111..t117), all under one capability held only by the eligibility
# worker's timer-workload context. Not activation-protected: the eligibility switch is
# `m05_el_settings.enabled`, revalidated inside dispatch and settle (R2).

WORKER_CAPABILITY: Final = "m05.eligibility.worker"
DUE_KEY: Final = "m05.eligibility.due"
CLAIM_KEY: Final = "m05.eligibility.claim"
DISPATCH_KEY: Final = "m05.eligibility.dispatch"
ANSWER_KEY: Final = "m05.eligibility.answer"
TIMEOUT_KEY: Final = "m05.eligibility.timeout"
SETTLE_KEY: Final = "m05.eligibility.settle"
EXPIRE_KEY: Final = "m05.eligibility.expire"

EL2_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    key: (StatementMode.WRITE, WORKER_CAPABILITY, f"SELECT {function}(%(command)s)")
    for key, function in (
        (DUE_KEY, "m05_el_due"), (CLAIM_KEY, "m05_el_claim"),
        (DISPATCH_KEY, "m05_el_dispatch"), (ANSWER_KEY, "m05_el_record_answer"),
        (TIMEOUT_KEY, "m05_el_timeout"), (SETTLE_KEY, "m05_el_settle"),
        (EXPIRE_KEY, "m05_el_expire"))
})

# --- E3a (synthetic; composed behind `include_el3a`) -------------------------------------
# The case applier (t120) and the eligibility item create (t119) under one capability held
# only by the case service's timer-workload context (with the M02 append capability); the
# case view (t121) is a staff read under the existing read capability. Not
# activation-protected (D-R2, Codex agreed): settled findings reach staff after switch-off.

CASES_CAPABILITY: Final = "m05.eligibility.cases"
APPLY_KEY: Final = "m05.eligibility.apply"
CREATE_ITEM_KEY: Final = "m05.eligibility.create_item"
CASE_VIEW_KEY: Final = "m05.eligibility.case_view"
READ_CAPABILITY: Final = "m05.followup.read"

EL3A_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    APPLY_KEY: (StatementMode.WRITE, CASES_CAPABILITY, "SELECT m05_el_apply(%(command)s)"),
    CREATE_ITEM_KEY: (StatementMode.WRITE, CASES_CAPABILITY,
                      "SELECT m05_create_item_v3(%(command)s)"),
    CASE_VIEW_KEY: (StatementMode.READ, READ_CAPABILITY,
                    "SELECT m05_el_case_view(%(item_id)s)"),
})

# Eligibility E3b (correction C-1): no new key; the existing staff action keys (capability =
# key) are re-bound to the v4 gateways, as S-I re-bound them to v3. The synthetic catalogue
# drops the S-I bindings of these two keys when `include_el3b` is set.
# (imported under other names: this module's own APPLY_KEY is the E3a case applier key)
from haloflow.m05.writeback_statements import (  # noqa: E402
    APPLY_KEY as STAFF_APPLY_KEY,
)
from haloflow.m05.writeback_statements import (  # noqa: E402
    PREPARE_KEY as STAFF_PREPARE_KEY,
)

EL3B_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    STAFF_PREPARE_KEY: (StatementMode.WRITE, STAFF_PREPARE_KEY,
                        "SELECT m05_prepare_action_v4(%(command)s)"),
    STAFF_APPLY_KEY: (StatementMode.WRITE, STAFF_APPLY_KEY,
                      "SELECT m05_apply_action_v4(%(command)s)"),
})

# --- E5 (synthetic; composed behind `include_el5`) ---------------------------------------
# The documentation worker gateways (t129..t132, t134 resumption) under one capability held
# only by the doc service's timer-workload context; the staff "Recorded in chart manually"
# action (t134) under its own capability held only by an authorized staff context. Not
# activation-protected (architecture v1r1 A7: settled events are documented after a
# switch-off).

DOC_CAPABILITY: Final = "m05.eligibility.doc"
DOC_MANUAL_CAPABILITY: Final = "m05.eligibility.doc_manual"
DOC_ENQUEUE_KEY: Final = "m05.eligibility.doc_enqueue"
DOC_ENTRY_KEY: Final = "m05.eligibility.doc_entry"
DOC_CLAIM_KEY: Final = "m05.eligibility.doc_claim"
DOC_SETTLE_KEY: Final = "m05.eligibility.doc_settle"
DOC_RESUME_KEY: Final = "m05.eligibility.doc_manual_resume"
DOC_MANUAL_KEY: Final = "m05.eligibility.doc_manual"

EL5_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    DOC_ENQUEUE_KEY: (StatementMode.WRITE, DOC_CAPABILITY,
                      "SELECT m05_el_doc_enqueue(%(command)s)"),
    DOC_ENTRY_KEY: (StatementMode.READ, DOC_CAPABILITY, "SELECT m05_el_doc_entry(%(command)s)"),
    DOC_CLAIM_KEY: (StatementMode.WRITE, DOC_CAPABILITY, "SELECT m05_el_doc_claim(%(command)s)"),
    DOC_SETTLE_KEY: (StatementMode.WRITE, DOC_CAPABILITY,
                     "SELECT m05_el_doc_settle(%(command)s)"),
    DOC_RESUME_KEY: (StatementMode.WRITE, DOC_CAPABILITY,
                     "SELECT m05_el_doc_manual(%(command)s)"),
    DOC_MANUAL_KEY: (StatementMode.WRITE, DOC_MANUAL_CAPABILITY,
                     "SELECT m05_el_doc_manual(%(command)s)"),
})
