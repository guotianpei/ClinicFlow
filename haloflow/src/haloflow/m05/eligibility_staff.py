"""Eligibility E3b: the staff service over the v4 action gateways. SYNTHETIC ONLY.

Pins: E3b interface pins v1 section 4 + v1r1 (Codex ALIGN 74643295); red packet v1 + v1r1
(Codex PASS e35a2663).

- `CaseToken`, `EligibilityResolveAction`, `LogContactAction` (frozen).
- `StaffServiceV4(gateway, *, foundation, keys)` extends `StaffServiceV3`: the existing
  actions run unchanged through the re-bound `m05.followup.prepare` / `apply` keys (v4); a
  token-less `ResolveAction` on an eligibility item keeps the E3a refusal
  (ActionFailed ACTION_RESULT_INVALID); `case(context, item_id)` is the t121 case view.

IMPLEMENTATION IN PROGRESS: the eligibility actions (`EligibilityResolveAction`,
`LogContactAction`) await the v4 eligibility branch (design point D-1 with Codex) and refuse
with ActionFailed("ACTION_UNAVAILABLE") until then.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from haloflow.m01.staff import service as m01staff
from haloflow.m05 import service as staff
from haloflow.m05.eligibility_cases import case_view
from haloflow.m05.writeback import StaffServiceV3

__all__ = ["CaseToken", "EligibilityResolveAction", "LogContactAction", "StaffServiceV4"]


@dataclass(frozen=True)
class CaseToken:
    case_id: UUID
    item_id: UUID
    case_version: int


@dataclass(frozen=True)
class EligibilityResolveAction:
    item_id: UUID
    client_action_id: UUID
    expected_version: int
    type: str
    outcome_code: str
    note: str | None
    token: CaseToken | None


@dataclass(frozen=True)
class LogContactAction:
    item_id: UUID
    client_action_id: UUID
    expected_version: int
    contact_code: str
    token: CaseToken | None


class StaffServiceV4(StaffServiceV3):
    """StaffServiceV3 over the v4 gateways, plus the eligibility case view and actions."""

    async def case(self, context: Any, item_id: UUID) -> dict[str, Any]:
        if not m01staff.is_uuid(item_id):
            raise staff.CommandMalformed("item_id")
        if self._foundation is None:
            raise m01staff.ReadFailed("READ_UNAVAILABLE")
        view = await case_view(self._foundation, context, item_id)
        if view.get("error") == "ITEM_NOT_FOUND":
            raise m01staff.ReadFailed("ITEM_NOT_FOUND")
        return view

    async def act(self, context: Any, action: Any) -> staff.ActionResult:
        if isinstance(action, (EligibilityResolveAction, LogContactAction)):
            raise staff.ActionFailed("ACTION_UNAVAILABLE")  # D-1 pending
        return await super().act(context, action)
