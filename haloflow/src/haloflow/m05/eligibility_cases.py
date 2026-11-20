"""Eligibility checks E3a: the case applier service. SYNTHETIC ONLY.

`EligibilityCaseService.apply_one` runs ONE gateway transaction: the t120 applier
(`m05.eligibility.apply`) takes the lowest unapplied case event of one appointment; then the
M02 append of exactly one `m05_eligibility_case_event` v1 observation for it (event id minted
and stored by the gateway; producer_dedupe_key "elcase:<case_event_id hex>"; the `record`
discriminator tells a verification settlement from a finding or a note); then, when the event
opened a case, `m05.eligibility.create_item` (t119) with that observation as the item's
source. A failure anywhere rolls all of it back and the event stays unapplied. Governing: E3a
red packet v1r1 (Codex red gate PASS e4e7aadd); pins in `tests/m05/el3a_support.py`.

Admission happens before any I/O: a WORKLOAD principal whose context capabilities EQUAL
CASE_CAPABILITIES. Logs carry effects and codes only (EL-14).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import PrincipalKind, TenantContext
from haloflow.m02.observation import AppendFailed, EventFoundation, Observation
from haloflow.m05.eligibility_statements import (
    APPLY_KEY,
    CASE_VIEW_KEY,
    CASES_CAPABILITY,
    CREATE_ITEM_KEY,
)
from haloflow.m05.intake import _attribution, _code

_LOG: Final = logging.getLogger(__name__)

EVENT_TYPE: Final = "m05_eligibility_case_event"
APPEND_CAPABILITY: Final = "m02.observation.append"
CASE_CAPABILITIES: Final = frozenset({CASES_CAPABILITY, APPEND_CAPABILITY})
EFFECTS: Final = ("opened", "noted", "none")
EL_KINDS: Final = ("eligibility_inactive", "eligibility_inconclusive", "eligibility_manual")
METADATA_KEYS: Final = frozenset({"appointment_ref", "case_event_id", "class",
                                  "coverage_version", "effect", "open_kind", "reason",
                                  "record", "seq", "verification_id"})


class EligibilityCaseRefused(Exception):
    """Refused before any I/O (admission)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EligibilityCaseFailed(Exception):
    """A database refusal or an append failure, by code. The transaction rolled back."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class EligibilityCaseCommitUnknown(Exception):
    """The body completed; the acknowledgement of the commit was lost."""


class EligibilityCaseService:
    __slots__ = ("__foundation",)

    def __init__(self, foundation: Any) -> None:
        self.__foundation = foundation

    def __repr__(self) -> str:
        return "EligibilityCaseService()"

    @staticmethod
    def _admit(context: Any) -> None:
        if context.principal.kind is not PrincipalKind.WORKLOAD:
            raise EligibilityCaseRefused("PRINCIPAL_NOT_WORKLOAD")
        if context.capabilities != CASE_CAPABILITIES:
            raise EligibilityCaseRefused("CAPABILITIES_INVALID")

    async def apply_one(self, context: TenantContext) -> str:
        self._admit(context)
        complete = False

        async def body(binding: Any) -> str:
            nonlocal complete
            row = await self._call(binding, context, APPLY_KEY, {})
            outcome = row.get("outcome")
            if outcome == "idle":
                complete = True
                return "idle"
            if outcome != "applied" or row.get("effect") not in EFFECTS:
                raise EligibilityCaseFailed("RESULT_INVALID")
            effect = str(row["effect"])
            observation = _observation(row)
            try:
                await self.__foundation.append_observation_in(binding, observation)
            except AppendFailed as error:
                raise EligibilityCaseFailed(error.reason_code) from None
            if effect == "opened":
                kind = row.get("item_kind")
                if kind not in EL_KINDS:
                    raise EligibilityCaseFailed("RESULT_INVALID")
                created = await self._call(binding, context, CREATE_ITEM_KEY, {
                    "source_event_id": str(observation.event_id), "item_kind": kind})
                try:
                    UUID(str(created["item_id"]))
                except (KeyError, TypeError, ValueError):
                    raise EligibilityCaseFailed("RESULT_INVALID") from None
            complete = True  # the final action of the body
            return effect

        try:
            result: str = await self.__foundation.run_in_transaction(context, body)
        except (Exception, asyncio.CancelledError):
            if not complete:
                raise
            raise EligibilityCaseCommitUnknown() from None
        _LOG.info("eligibility case applied effect=%s", result)
        return result

    async def drain(self, context: TenantContext, limit: int = 100) -> dict[str, int]:
        if type(limit) is not int or limit < 1:
            raise EligibilityCaseRefused("LIMIT_INVALID")
        counts: dict[str, int] = {}
        for _ in range(limit):
            effect = await self.apply_one(context)
            if effect == "idle":
                break
            counts[effect] = counts.get(effect, 0) + 1
        return counts

    async def _call(self, binding: Any, context: Any, key: str,
                    command: Mapping[str, Any]) -> dict[str, Any]:
        payload = {"schema_version": 1, **command, "attribution": _attribution(context)}
        failure: str | None = None
        try:
            rows = await binding.handle.fetch_all(key, {"command": Jsonb(payload)})
        except psycopg.Error as error:
            failure = _code(error)
            rows = []
        if failure is not None:
            raise EligibilityCaseFailed(failure)
        if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
            raise EligibilityCaseFailed("RESULT_INVALID")
        return dict(rows[0][0])


def _observation(row: Mapping[str, Any]) -> Observation:
    try:
        evidence = row["evidence"]
        metadata = dict(evidence["metadata"])
        case_event = UUID(str(row["case_event_id"]))
        observation = Observation(
            event_id=UUID(str(evidence["event_id"])),
            event_type=EVENT_TYPE,
            contract_version=1,
            correlation_id=case_event,
            producer_dedupe_key=f"elcase:{case_event.hex}",
            links=(),
            metadata=metadata,
            patient_id=UUID(str(evidence["patient_id"])),
            occurred_at=datetime.fromisoformat(str(evidence["occurred_at"])),
            source_time_quality="trusted",
        )
    except (KeyError, TypeError, ValueError):
        raise EligibilityCaseFailed("RESULT_INVALID") from None
    if set(metadata) != METADATA_KEYS:
        raise EligibilityCaseFailed("RESULT_INVALID")
    return observation


async def case_view(foundation: EventFoundation, context: TenantContext,
                    item_id: UUID) -> dict[str, Any]:
    """The staff case view (t121) through `m05.eligibility.case_view` (read capability)."""

    async def body(binding: Any) -> dict[str, Any]:
        rows = await binding.handle.fetch_all(CASE_VIEW_KEY, {"item_id": item_id})
        if len(rows) != 1 or not isinstance(rows[0][0], Mapping):
            raise EligibilityCaseFailed("RESULT_INVALID")
        return dict(rows[0][0])

    result: dict[str, Any] = await foundation.run_in_transaction(context, body)
    return result
