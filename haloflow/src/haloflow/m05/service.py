"""B3b M05 staff follow-up service over the M01 gateway (package v2 + v2r1,
Claude/Codex aligned 2026-11-01).

The public commands carry no permissions, attribution, tenant, actor or digest: the
service builds those from the trusted TenantContext only (attribution exactly as
M02; read = m05.followup.read, act = m05.followup.apply, supervise =
m05.followup.supervise), and the SQL additionally requires current directory standing.

RED TRANCHE: the interface is fixed for the red tests; every service method raises
NotImplementedError and the command types do not yet validate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from haloflow.m01.staff.service import (
    CommandMalformed,
    ReadFailed,
    ReadRefused,
    RouteDenied,
)

__all__ = [
    "ActionCommitUnknown",
    "ActionFailed",
    "ActionRefused",
    "ActionResult",
    "CommandMalformed",
    "Cursor",
    "ListQuery",
    "M05Service",
    "ReadFailed",
    "ReadRefused",
    "RouteDenied",
    "StaffAction",
]

SUPERVISE_CAPABILITY = "m05.followup.supervise"


class ActionRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("follow-up action refused")
        self.code = code


class ActionFailed(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__("follow-up action failed")
        self.reason_code = reason_code


class ActionCommitUnknown(Exception):
    """The outcome is unknown after the body completed. Recovery: retry the identical
    StaffAction (same client_action_id); it replays the stored snapshot if the first
    attempt committed and applies once if it did not."""

    def __init__(self, item_id: UUID, client_action_id: UUID) -> None:
        super().__init__("follow-up action outcome unknown")
        self.item_id = item_id
        self.client_action_id = client_action_id


@dataclass(frozen=True)
class StaffAction:
    item_id: UUID
    client_action_id: UUID
    expected_version: int
    type: str
    assignee_id: UUID | None = None


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    item_id: UUID


@dataclass(frozen=True)
class ListQuery:
    limit: int
    states: tuple[str, ...] | None = None
    assignee: UUID | str | None = None
    after: Cursor | None = None


@dataclass(frozen=True)
class ActionResult:
    outcome: str
    snapshot: Mapping[str, Any]


class M05Service:
    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def act(self, context: Any, action: StaffAction) -> ActionResult:
        raise NotImplementedError("B3b action route")

    async def get(self, context: Any, item_id: UUID) -> Mapping[str, Any]:
        raise NotImplementedError("B3b read route")

    async def list_items(self, context: Any, query: ListQuery) -> Mapping[str, Any]:
        raise NotImplementedError("B3b read route")

    async def history(self, context: Any, item_id: UUID) -> Mapping[str, Any]:
        raise NotImplementedError("B3b read route")
