"""B3b staff-directory service over the M01 gateway (package v2 + v2r1, Claude/Codex
aligned 2026-11-01).

RED TRANCHE: the public interface is fixed here for the red tests; every service
method raises NotImplementedError and the command types do not yet validate. The
implementation tranche adds validation, trusted derivation and result binding.

Errors shared with the M05 service live here (M05 depends on M01, never the
reverse). Every error carries a fixed message and a code; none carries driver text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class RouteDenied(Exception):
    """The context lacks a capability the route needs; no gateway call was made."""

    def __init__(self, reason_code: str = "CAPABILITY_DENIED") -> None:
        super().__init__("route denied")
        self.reason_code = reason_code


class CommandMalformed(ValueError):
    """A public command value is malformed; raised before any gateway call."""

    def __init__(self, field: str) -> None:
        super().__init__("command malformed")
        self.field = field


class ReadRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("read refused")
        self.code = code


class ReadFailed(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__("read failed")
        self.reason_code = reason_code


class AdminRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("staff administration refused")
        self.code = code


class AdminFailed(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__("staff administration failed")
        self.reason_code = reason_code


class AdminCommitUnknown(Exception):
    """The outcome is unknown after the body completed. Recovery: retry the identical
    StaffAdminCommand (same client_action_id); it replays if the first attempt
    committed and applies once if it did not."""

    def __init__(self, staff_id: UUID, client_action_id: UUID, operation: str) -> None:
        super().__init__("staff administration outcome unknown")
        self.staff_id = staff_id
        self.client_action_id = client_action_id
        self.operation = operation


@dataclass(frozen=True)
class StaffAdminCommand:
    client_action_id: UUID
    operation: str
    staff_id: UUID
    display_name: str | None = None
    access: str | None = None


@dataclass(frozen=True)
class AdminResult:
    """The bound admin result: {schema_version, client_action_id, operation, staff}."""

    result: Mapping[str, Any]


class StaffService:
    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def admin(self, context: Any, command: StaffAdminCommand) -> AdminResult:
        raise NotImplementedError("B3b staff admin service")

    async def display(self, context: Any, staff_ids: tuple[UUID, ...]) -> Mapping[str, Any]:
        raise NotImplementedError("B3b staff display service")
