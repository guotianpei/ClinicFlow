"""Eligibility E4 / OD-EL12: the synthetic contact-history read key (t127). SYNTHETIC ONLY:
composed into the eligibility-profile catalogue only, never the default or production one.

CONTACT_HISTORY_KEY: a staff READ under the B4 view capability (READ_CAPABILITY, the t095
display pattern). One row whose single column is the function's result.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode
from haloflow.m05.writeback_statements import READ_CAPABILITY

CONTACT_HISTORY_KEY: Final = "m05.synthetic.el_contact_history"

EL4_STATEMENTS: Final[StatementDefinitions] = MappingProxyType({
    CONTACT_HISTORY_KEY: (StatementMode.READ, READ_CAPABILITY,
                          "SELECT m05_el_contact_history(%(item_id)s::uuid)"),
})
