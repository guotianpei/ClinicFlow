"""Incoming fax routing FX-IN, package FX3a: the fax service over the FX1/FX2 gateways.
SYNTHETIC composition only (the FX3 catalogue keys, `fax_statements`).

`FaxService(foundation)` makes exactly one gateway call per method, in one transaction of the
caller's tenant context:
- receive(ctx, InboundFax)   the ingress workload context, capabilities exactly
                             INGRESS_CAPABILITIES (RECEIVE_KEY, t139)
- lines(ctx)                 the trusted startup context, capabilities exactly
                             LINES_CAPABILITIES (LINES_KEY, t144) -> the tenant's lines
- list / get / history       a staff context (READ_KEY, t143)
- open                       a staff context (OPEN_KEY: t142 fixed to `open` on the SQL side)
- act(ctx, FaxAction)        a staff context holding apply (ACT_KEY, t142: move / file / sort)
Staff commands carry `staff_id` = the context principal (the gateway requires it to equal the
attribution's actor). Gateway refusals (P0001 codes) raise FaxRefused(code); the runtime's
CapabilityDenied passes through unchanged. Nothing is logged here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import PrincipalKind, TenantContext
from haloflow.m05.fax_statements import (
    ACT_KEY,
    CAPABILITY_INGRESS,
    CAPABILITY_LINES,
    LINES_KEY,
    OPEN_KEY,
    READ_KEY,
    RECEIVE_KEY,
)
from haloflow.m05.intake import _attribution, _code

INGRESS_CAPABILITIES: Final = frozenset({CAPABILITY_INGRESS})
LINES_CAPABILITIES: Final = frozenset({CAPABILITY_LINES})
ACTIONS: Final = ("move", "file", "sort")


class FaxRefused(Exception):
    """A gateway refusal or an admission failure, by code (the transaction rolled back)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class InboundFax:
    provider: str
    provider_fax_id: str
    line: str
    sender: str | None
    pages: int
    received_at: str
    doc_ref: str


@dataclass(frozen=True, slots=True)
class FaxAction:
    action: str
    fax_id: UUID
    expected_version: int
    queue_id: UUID | None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError("fax action must be move, file or sort")


class FaxService:
    def __init__(self, foundation: Any) -> None:
        self._foundation = foundation

    def __repr__(self) -> str:
        return "FaxService()"

    async def _call(self, context: Any, key: str, command: Mapping[str, Any], *,
                    attributed: bool = True) -> dict[str, Any]:
        payload = {"schema_version": 1, **command}
        if attributed:  # t144 (the lines snapshot) takes exactly {"schema_version": 1}
            payload["attribution"] = _attribution(context)

        async def body(binding: Any) -> dict[str, Any]:
            failure: str | None = None
            try:
                rows = await binding.handle.fetch_all(key, {"command": Jsonb(payload)})
            except psycopg.Error as error:
                failure = _code(error)
                rows = []
            if failure is not None:
                raise FaxRefused(failure)
            if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
                raise FaxRefused("RESULT_INVALID")
            return dict(rows[0][0])

        result: dict[str, Any] = await self._foundation.run_in_transaction(context, body)
        return result

    @staticmethod
    def _workload(context: Any, capabilities: frozenset[str]) -> None:
        if not isinstance(context, TenantContext):
            raise FaxRefused("CONTEXT_INVALID")
        if context.principal.kind is not PrincipalKind.WORKLOAD:
            raise FaxRefused("PRINCIPAL_NOT_WORKLOAD")
        if context.capabilities != capabilities:
            raise FaxRefused("CAPABILITIES_INVALID")

    @staticmethod
    def _staff(context: Any) -> str:
        if not isinstance(context, TenantContext):
            raise FaxRefused("CONTEXT_INVALID")
        return str(context.principal.id)

    # -- ingress / startup -------------------------------------------------------------------

    async def receive(self, context: Any, fax: InboundFax) -> dict[str, Any]:
        self._workload(context, INGRESS_CAPABILITIES)
        if not isinstance(fax, InboundFax):
            raise FaxRefused("COMMAND_MALFORMED")
        return await self._call(context, RECEIVE_KEY, {
            "provider": fax.provider, "provider_fax_id": fax.provider_fax_id, "line": fax.line,
            "sender": fax.sender, "pages": fax.pages, "received_at": fax.received_at,
            "doc_ref": fax.doc_ref})

    async def lines(self, context: Any) -> tuple[str, ...]:
        self._workload(context, LINES_CAPABILITIES)
        out = await self._call(context, LINES_KEY, {}, attributed=False)
        lines = out.get("lines")
        if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
            raise FaxRefused("RESULT_INVALID")
        return tuple(lines)

    # -- staff -------------------------------------------------------------------------------

    async def list(self, context: Any, queue_id: UUID | None) -> dict[str, Any]:
        staff = self._staff(context)
        return await self._call(context, READ_KEY, {
            "view": "list", "staff_id": staff,
            "queue_id": None if queue_id is None else str(queue_id)})

    async def get(self, context: Any, fax_id: UUID) -> dict[str, Any]:
        staff = self._staff(context)
        return await self._call(context, READ_KEY, {"view": "get", "staff_id": staff,
                                                    "fax_id": str(fax_id)})

    async def history(self, context: Any, fax_id: UUID) -> dict[str, Any]:
        staff = self._staff(context)
        return await self._call(context, READ_KEY, {"view": "history", "staff_id": staff,
                                                    "fax_id": str(fax_id)})

    async def open(self, context: Any, fax_id: UUID) -> dict[str, Any]:
        staff = self._staff(context)
        # no "action" here: OPEN_KEY supplies {"action": "open"} on the SQL side
        return await self._call(context, OPEN_KEY, {"staff_id": staff, "fax_id": str(fax_id)})

    async def act(self, context: Any, action: FaxAction) -> dict[str, Any]:
        staff = self._staff(context)
        if not isinstance(action, FaxAction):
            raise FaxRefused("COMMAND_MALFORMED")
        command: dict[str, Any] = {"action": action.action, "staff_id": staff,
                                   "fax_id": str(action.fax_id),
                                   "expected_version": action.expected_version}
        if action.action != "file":
            if action.queue_id is None:
                raise FaxRefused("COMMAND_MALFORMED")
            command["queue_id"] = str(action.queue_id)
        return await self._call(context, ACT_KEY, command)
