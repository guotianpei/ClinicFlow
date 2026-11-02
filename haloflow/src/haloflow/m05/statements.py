"""M05 statement definitions (UC1 part B; gate ruling 1, 20261031T194129Z).

One statement per M05 gateway function. NOT composed into production until B5,
together with the units, execution role, profiles and target (gate ruling 4); the
module-scope test (MB1-S05) enforces that. The queries name the tenant function
unqualified: the M01 gateway pins the transaction's search_path.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.statements import StatementDefinitions, StatementMode

M05_STATEMENTS: Final[StatementDefinitions] = MappingProxyType(
    {
        "m05.followup.create": (
            StatementMode.WRITE,
            "m05.followup.create",
            "SELECT m05_create_item(%(command)s)",
        ),
        "m05.followup.prepare": (
            StatementMode.WRITE,
            "m05.followup.prepare",
            "SELECT m05_prepare_action(%(command)s)",
        ),
        "m05.followup.apply": (
            StatementMode.WRITE,
            "m05.followup.apply",
            "SELECT m05_apply_action(%(command)s)",
        ),
        "m05.followup.get": (
            StatementMode.READ,
            "m05.followup.read",
            "SELECT m05_get_item(%(item_id)s)",
        ),
        "m05.followup.list": (
            StatementMode.READ,
            "m05.followup.read",
            "SELECT m05_list_items(%(query)s)",
        ),
        "m05.followup.history": (
            StatementMode.READ,
            "m05.followup.read",
            "SELECT m05_get_history(%(item_id)s)",
        ),
        "m05.followup.note.read": (
            StatementMode.READ,
            "m05.followup.note.read",
            "SELECT m05_read_note(%(note_id)s)",
        ),
    }
)
