"""Eligibility E3b: the staff service over the v4 action gateways. SYNTHETIC ONLY.

Pins: E3b interface pins v1 section 4 + v1r1 (Codex ALIGN 74643295); red packet v1 + v1r1
(Codex PASS e35a2663).

- `CaseToken`, `EligibilityResolveAction`, `LogContactAction` (frozen).
- `StaffServiceV4(gateway, *, foundation, keys)` extends `StaffServiceV3`: the existing
  actions run unchanged through the re-bound `m05.followup.prepare` / `apply` keys (v4); a
  token-less `ResolveAction` on an eligibility item keeps the E3a refusal
  (ActionFailed ACTION_RESULT_INVALID); `case(context, item_id)` is the t121 case view.

Eligibility actions (Codex D-1 option A, B1-b, B2): ONE gateway transaction - prepare v4 (locks
membership -> appointment key -> the item's own case -> item; replay before projection; the
owner-only helper projects every unapplied case event of the appointment and returns its
evidence; then the token comparison); the M02 append of one `m05_eligibility_case_event`
observation per projected event at contract v2 (producer = this service's foundation,
`m05_followup`; the actor is the staff member; correlation and dedupe key as the timer path,
so each case event has exactly one observation whichever path projects it); then either the
committed `case_changed` result (the refreshed case view) or, for NEW, the resolution
observation at contract v3 and apply v4 (close / log_contact). Any failure rolls the whole
transaction back - projection included; a retry projects again and appends once.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.staff import service as m01staff
from haloflow.m02.observation import AppendFailed, Link, Observation
from haloflow.m05 import notes
from haloflow.m05 import service as staff
from haloflow.m05.eligibility_cases import (
    EligibilityCaseFailed,
    _observation,
    case_view,
)
from haloflow.m05.writeback import StaffServiceV3

_LOG: Final = logging.getLogger(__name__)

EL_KINDS: Final = frozenset({"eligibility_inactive", "eligibility_inconclusive",
                             "eligibility_manual"})
RESOLVE_TYPES: Final = ("resolve", "claim_and_resolve")
RESOLUTION_CONTRACT_V3: Final = 3
CASE_EVIDENCE_CONTRACT_V2: Final = 2
EFFECTS: Final = frozenset({"noted", "none"})  # "opened" is impossible on the staff path
# Integrity / defensive codes the v4 gateways raise (never business refusals).
EL_FAILURES: Final = frozenset({"CASE_CHANGED", "CASE_EVIDENCE_MISSING",
                                "CASE_BINDING_INVALID", "CASE_INVARIANT_FAILED"})
CONTACT_SNAPSHOT_KEYS: Final = staff.SNAPSHOT_KEYS | {"contact_code"}

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
        if type(action) in (EligibilityResolveAction, LogContactAction):
            return await self._el_act(context, action)
        return await super().act(context, action)

    async def _el_act(self, context: Any, action: EligibilityResolveAction | LogContactAction
                      ) -> staff.ActionResult:
        contact = type(action) is LogContactAction
        note = _validate(action)
        # B2: the contact action carries the resolve requirement (it may append evidence).
        m01staff.require_capabilities(context, staff.RESOLVE_ROUTE)
        attribution = m01staff.actor_attribution(context)
        if attribution is None:
            raise staff.ActionRefused("NOT_AUTHORIZED")
        if self._foundation is None:
            raise staff.ActionFailed("ACTION_UNAVAILABLE")
        actor = attribution["principal_id"]
        tenant = context.tenant_id
        caps = context.capabilities
        base: dict[str, Any] = {
            "schema_version": 1,
            "item_id": str(action.item_id),
            "client_action_id": str(action.client_action_id),
            "expected_version": action.expected_version,
            "permissions": {"read": m01staff.READ_CAPABILITY in caps,
                            "act": staff.APPLY_CAPABILITY in caps,
                            "supervise": staff.SUPERVISE_CAPABILITY in caps},
            "attribution": attribution,
        }
        token = None if action.token is None else {
            "case_id": str(action.token.case_id), "item_id": str(action.token.item_id),
            "case_version": action.token.case_version}

        def command(operation: dict[str, Any]) -> Jsonb:
            if isinstance(action, LogContactAction):
                head: dict[str, Any] = {"type": "log_contact",
                                        "contact_code": action.contact_code}
            else:
                head = {"type": action.type, "outcome_code": action.outcome_code}
            tail = {} if token is None else {"case_token": token}
            return Jsonb({**base, "action": {**head, **operation, **tail}})

        async def call(handle: Any, key: str, operation: dict[str, Any]) -> list[Any]:
            rows: list[Any] = await m01staff.fetch_rows(handle, key, {"command": command(
                operation)}, _el_action_error)
            return rows

        async def body(binding: Any, mark: Callable[[], None]) -> staff.ActionResult:
            handle = binding.handle
            first_op: dict[str, Any] = {} if contact else {"note_present": note is not None}
            decision, detail = _el_decision(await call(handle, staff.K_PREPARE, first_op),
                                            action, actor, second=False)
            if decision == "REPLAY_CHALLENGE":
                mac_key = self._key(lambda: self._keys.hmac_key(detail),
                                    lambda: staff.ActionRefused("REPLAY_UNVERIFIABLE"))
                challenge = {"note_hmac": notes.note_hmac(mac_key, tenant, note or ""),
                             "hmac_key_version": detail}
                _el_decision(await call(handle, staff.K_PREPARE, {
                    "note_present": True, "note": challenge}), action, actor, second=True)
                result = _el_applied(await call(handle, staff.K_APPLY, {
                    "note": challenge, "payload": None}), action, actor, "replayed")
            elif decision == "REPLAY":
                result = _el_applied(await call(handle, staff.K_APPLY, {} if contact else {
                    "note": None, "payload": None}), action, actor, "replayed")
            else:
                await self._append_evidence(binding, detail["evidence"])
                if decision == "CASE_CHANGED":
                    _LOG.info("m05 eligibility action case_changed")
                    result = staff.ActionResult("case_changed", detail["case"])
                elif contact:
                    result = _el_applied(await call(handle, staff.K_APPLY, {}), action, actor,
                                         "applied")
                else:
                    assert isinstance(action, EligibilityResolveAction)
                    result = await self._close(binding, call, action, actor, tenant, context,
                                               detail["item"], note)
            mark()
            return result

        state = {"complete": False}

        def mark() -> None:
            state["complete"] = True

        async def inner(binding: Any) -> staff.ActionResult:
            return await body(binding, mark)

        try:
            result: staff.ActionResult = await self._foundation.run_in_transaction(context,
                                                                                   inner)
            return result
        except (Exception, asyncio.CancelledError):
            if not state["complete"]:
                raise
        raise staff.ActionCommitUnknown(action.item_id, action.client_action_id)

    async def _append_evidence(self, binding: Any, evidence: list[Observation]) -> None:
        """B1-b: one case-evidence observation per projected event, contract v2, in the
        action's transaction; a failure fails the action (everything rolls back)."""
        for observation in evidence:
            try:
                appended = await self._foundation.append_observation_in(binding, observation)
            except AppendFailed as error:
                raise staff.ActionFailed(error.reason_code) from None
            if appended.appended_or_existing != "appended" or appended.event_id != \
                    observation.event_id:
                raise staff.ActionFailed("CASE_EVIDENCE_EXISTING")

    async def _close(self, binding: Any, call: Any, action: EligibilityResolveAction,
                     actor: str, tenant: str, context: Any, item: dict[str, Any],
                     note: str | None) -> staff.ActionResult:
        sealed_note: dict[str, Any] | None = None
        note_row: dict[str, Any] | None = None
        if note is not None:
            def unavailable() -> Exception:
                return staff.ActionFailed("KEY_UNAVAILABLE")

            version, mac_key = self._key(lambda: self._keys.current_hmac(), unavailable)
            enc_version, enc_key = self._key(lambda: self._keys.current_encryption(),
                                             unavailable)
            sealed_note = {"note_hmac": notes.note_hmac(mac_key, tenant, note),
                           "hmac_key_version": version}
            sealed = notes.encrypt_note(enc_key, enc_version, tenant, action.item_id,
                                        action.client_action_id, note)
            note_row = {"note_id": str(sealed.note_id), "nonce": sealed.nonce.hex(),
                        "ciphertext": sealed.ciphertext.hex(),
                        "enc_key_version": sealed.enc_key_version}
        event_id = uuid.uuid4()
        observation = Observation(
            event_id=event_id,
            event_type=staff.RESOLUTION_TYPE,
            contract_version=RESOLUTION_CONTRACT_V3,
            correlation_id=context.correlation_id,
            producer_dedupe_key=_resolution_dedupe_key(tenant, action),
            links=(Link(staff.ITEM_LINK_TYPE, action.item_id, "case"),),
            metadata={"outcome_code": action.outcome_code, "assertion_kind": "staff",
                      "item_kind": item["item_kind"]},
            patient_id=UUID(item["patient_id"]),
            causation_event_id=UUID(item["source_event_id"]),
            occurred_at=None,
            source_time_quality="absent",
        )
        try:
            appended = await self._foundation.append_observation_in(binding, observation)
        except AppendFailed as error:
            raise staff.ActionFailed(error.reason_code) from None
        if appended.appended_or_existing != "appended":
            _LOG.warning("m05 eligibility close refused: orphan resolution event")
            raise staff.ActionFailed("ORPHAN_RESOLUTION_EVENT")
        if appended.event_id != event_id:
            raise staff._invalid()
        rows = await call(binding.handle, staff.K_APPLY, {
            "note": sealed_note, "payload": {"event_id": str(event_id), "note_row": note_row}})
        return _el_applied(rows, action, actor, "applied")


# --- validation, binders ----------------------------------------------------------------------

def _validate(action: EligibilityResolveAction | LogContactAction) -> str | None:
    """Exact types before any gateway call; the note D11-normalized (empty after trim is
    absent). Codes and the token's presence are the gateway's (COMMAND_MALFORMED /
    NOTE_REQUIRED are business refusals there)."""
    if not m01staff.is_uuid(action.item_id):
        raise staff.CommandMalformed("item_id")
    if not m01staff.is_uuid(action.client_action_id):
        raise staff.CommandMalformed("client_action_id")
    if (type(action.expected_version) is not int
            or not 1 <= action.expected_version <= staff.MAX_VERSION):
        raise staff.CommandMalformed("expected_version")
    token = action.token
    if token is not None and (
            type(token) is not CaseToken or not m01staff.is_uuid(token.case_id)
            or not m01staff.is_uuid(token.item_id) or type(token.case_version) is not int
            or not 1 <= token.case_version <= staff.MAX_VERSION):
        raise staff.CommandMalformed("token")
    if isinstance(action, LogContactAction):
        if type(action.contact_code) is not str:
            raise staff.CommandMalformed("contact_code")
        return None
    if action.type not in RESOLVE_TYPES:
        raise staff.CommandMalformed("type")
    if type(action.outcome_code) is not str:
        raise staff.CommandMalformed("outcome_code")
    failure: staff.CommandMalformed | None = None
    note: str | None = None
    try:
        note = notes.normalize_note(action.note)
    except notes.NoteInvalid:
        failure = staff.CommandMalformed("note")
    if failure is not None:
        raise failure  # outside the handler: the note never reaches a chain
    return note


def _el_action_error(error: psycopg.Error) -> Exception:
    sqlstate, message = m01staff.sqlstate_and_message(error)
    if sqlstate == "P0001" and message in EL_FAILURES:
        return staff.ActionFailed(message)
    return staff._action_error(error)


def _resolution_dedupe_key(tenant: str, action: EligibilityResolveAction) -> str:
    """The resolution dedupe key, as the existing staff path (MK-V1)."""
    preimage = json.dumps(["m05.resolve/v1", tenant, str(action.item_id),
                           str(action.client_action_id)], separators=(",", ":"),
                          ensure_ascii=False)
    return "m05r1:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def _evidence(value: Any) -> list[Observation]:
    """The projected events -> contract v2 observations (closed shape, bound effect)."""
    if type(value) is not list:
        raise staff._invalid()
    out: list[Observation] = []
    for entry in value:
        if (type(entry) is not dict or set(entry) != {"case_event_id", "effect", "evidence"}
                or entry["effect"] not in EFFECTS):
            raise staff._invalid()
        try:
            observation = _observation({"case_event_id": entry["case_event_id"],
                                        "evidence": entry["evidence"]})
        except EligibilityCaseFailed:
            raise staff._invalid() from None
        if observation.metadata.get("effect") != entry["effect"]:
            raise staff._invalid()
        out.append(dataclasses.replace(observation,
                                       contract_version=CASE_EVIDENCE_CONTRACT_V2))
    return out


def _case_view_bound(view: Any, action: EligibilityResolveAction | LogContactAction) -> bool:
    return (type(view) is dict and view.get("schema_version") == 1
            and view.get("item_id") == str(action.item_id)
            and m01staff.uuid_text(view.get("case_id"))
            and type(view.get("case_version")) is int and view["case_version"] >= 1
            and view.get("item_kind") in EL_KINDS)


def _el_decision(rows: list[Any], action: EligibilityResolveAction | LogContactAction,
                 actor: str, *, second: bool) -> tuple[str, Any]:
    """Prepare v4 on a bound action: REFUSED | REPLAY | REPLAY_CHALLENGE (note only) |
    CASE_CHANGED{case, evidence} | NEW{[item,] evidence}; the second prepare only a bound
    REPLAY or a known REFUSED."""
    value = m01staff.single_json(rows)
    if type(value) is not dict or value.get("schema_version") != 1:
        raise staff._invalid()
    decision = value.get("decision")
    contact = type(action) is LogContactAction
    if (decision == "REFUSED" and set(value) == {"schema_version", "decision", "code"}
            and value["code"] in staff.ACTION_REFUSALS):
        raise staff.ActionRefused(value["code"])
    if decision == "REPLAY" and set(value) == {"schema_version", "decision", "snapshot"}:
        return "REPLAY", _bound_snapshot(value["snapshot"], action, actor)
    if second:
        raise staff._invalid()
    if (decision == "REPLAY_CHALLENGE" and not contact
            and set(value) == {"schema_version", "decision", "hmac_key_version"}
            and type(value["hmac_key_version"]) is int):
        return "REPLAY_CHALLENGE", value["hmac_key_version"]
    if (decision == "CASE_CHANGED"
            and set(value) == {"schema_version", "decision", "case", "evidence"}
            and _case_view_bound(value["case"], action)):
        return "CASE_CHANGED", {"case": value["case"], "evidence": _evidence(value["evidence"])}
    if decision == "NEW" and contact and set(value) == {"schema_version", "decision",
                                                        "evidence"}:
        return "NEW", {"evidence": _evidence(value["evidence"])}
    if decision == "NEW" and not contact and set(value) == {"schema_version", "decision",
                                                            "item", "evidence"}:
        item = value["item"]
        if (type(item) is dict
                and set(item) == {"item_id", "item_kind", "patient_id", "source_event_id"}
                and item["item_id"] == str(action.item_id) and item["item_kind"] in EL_KINDS
                and m01staff.uuid_text(item["patient_id"])
                and m01staff.uuid_text(item["source_event_id"])):
            return "NEW", {"item": item, "evidence": _evidence(value["evidence"])}
    raise staff._invalid()


def _bound_snapshot(snap: Any, action: EligibilityResolveAction | LogContactAction,
                    actor: str) -> dict[str, Any]:
    common = (type(snap) is dict and snap.get("schema_version") == 1
              and snap.get("item_id") == str(action.item_id)
              and snap.get("client_action_id") == str(action.client_action_id))
    if not common:
        raise staff._invalid()
    if isinstance(action, LogContactAction):
        ok = (set(snap) == CONTACT_SNAPSHOT_KEYS and snap["action"] == "log_contact"
              and snap["version"] == action.expected_version
              and snap["contact_code"] == action.contact_code
              and snap["state"] in ("open", "in_progress") and snap["outcome_code"] is None)
    else:
        assignee = snap.get("assignee_id")
        ok = (set(snap) == staff.SNAPSHOT_KEYS and snap["action"] == action.type
              and snap["version"] == action.expected_version + 1
              and snap["state"] == "resolved" and snap["outcome_code"] == action.outcome_code
              and m01staff.uuid_text(assignee)
              and (action.type != "claim_and_resolve" or assignee == actor))
    if not ok:
        raise staff._invalid()
    bound: dict[str, Any] = snap
    return bound


def _el_applied(rows: list[Any], action: EligibilityResolveAction | LogContactAction,
                actor: str, expected: str) -> staff.ActionResult:
    value = m01staff.single_json(rows)
    if (type(value) is not dict or set(value) != {"schema_version", "outcome", "snapshot"}
            or value["schema_version"] != 1 or value["outcome"] != expected):
        raise staff._invalid()
    return staff.ActionResult(expected, _bound_snapshot(value["snapshot"], action, actor))
