"""L-1S package S-B: due-window selection runner (synthetic only; no scheduler).

`DueWindowRunner.run_once(tenant_id, execution_id)` walks the tenant's due reminder
windows through the t045 selector and closes each through S-A's
`IntakeService.close_window` (t041), one transaction per window. Governing: S-B
requirements v1r1, architecture v1 sections 1-3, interface pins v1 P3/P4 and the
red-package deltas D4/D5/D7 (red gate PASS at 22be664).

- Contexts: one injected trusted timer principal; the resolver issues a selection
  context with EXACTLY {DUE_KEY} per page and a close context with EXACTLY S-A's close
  set per window. No union context, no synthesized context.
- Walk (one circular state machine): phase A takes keys after the stored resume key;
  a short page wraps once to phase B, bounded by `until = start`. The attempt is marked
  (resume := key) immediately before invoking the close, whatever its outcome; at most
  `attempt_cap` attempts, checked before any further selection. COMPLETE resets the
  resume key; CAP, selection failure and cancellation keep the last attempted key.
- Cancellation is never swallowed: the task's pending cancellation count is checked at
  every phase boundary (S-A's `_run` turns a post-marker CancelledError into
  IntakeCommitUnknown without un-cancelling the task), and CancelledError is re-raised.
- One log line per run (D5); counts and codes only, never PHI or exception text.

Nothing here sends a message, touches an EHR or schedules anything. NOT composed into
production.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, NamedTuple
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
from haloflow.m05.due_statements import DUE_KEY
from haloflow.m05.intake import (
    APPEND_CAPABILITY,
    CREATE_KEY,
    CloseResult,
    IntakeCommitUnknown,
    IntakeFailed,
    IntakeRefused,
    IntakeService,
    _attribution,
    _code,
)
from haloflow.m05.intake_statements import CLOSE_KEY

__all__ = ["DueWindowRunner", "Key", "RunResult", "SBRefused"]

LOGGER: Final = logging.getLogger(__name__)

SELECT_CAPS: Final = frozenset({DUE_KEY})
CLOSE_CAPS: Final = frozenset({CLOSE_KEY, APPEND_CAPABILITY, CREATE_KEY})
TIMER_CAPS: Final = SELECT_CAPS | CLOSE_CAPS
PAGE_MAX: Final = 500
CAP_MAX: Final = 10_000
PURPOSE: Final = "operations"
# D7: closed failure vocabulary (S-A refusal codes + three generic codes).
FAILED_CODES: Final = frozenset({
    "COMMAND_MALFORMED", "WORKLOAD_NOT_AUTHORIZED", "DISPATCH_UNROUTABLE",
    "CONTACT_OPTED_OUT", "DISPATCH_CONFLICT", "INBOUND_CONFLICT", "WINDOW_NOT_FOUND",
    "DIRECTORY_CONTENDED", "RESULT_INVALID", "DATABASE_ERROR", "INTERNAL",
})
SELECTOR_CODES: Final = frozenset({"COMMAND_MALFORMED", "BOUNDS_INVALID",
                                   "WORKLOAD_NOT_AUTHORIZED"})
_TS_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"
_TS: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


class SBRefused(Exception):
    """Refused before any I/O."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Key(NamedTuple):
    deadline: datetime
    reminder_id: UUID


@dataclass(frozen=True, slots=True)
class RunResult:
    code: str
    attempted: int
    no_reply: int
    opted_out: int
    not_awaiting: int
    not_due: int
    commit_unknown: int
    failed: Mapping[str, int]
    wrapped: bool


@dataclass(slots=True)
class _Counts:
    attempted: int = 0
    no_reply: int = 0
    opted_out: int = 0
    not_awaiting: int = 0
    not_due: int = 0
    commit_unknown: int = 0
    cancelled: int = 0
    failed: dict[str, int] = field(default_factory=dict)
    wrapped: bool = False

    def fail(self, code: str) -> None:
        code = code if code in FAILED_CODES else "INTERNAL"
        self.failed[code] = self.failed.get(code, 0) + 1

    def result(self, code: str) -> RunResult:
        return RunResult(code, self.attempted, self.no_reply, self.opted_out,
                         self.not_awaiting, self.not_due, self.commit_unknown,
                         dict(sorted(self.failed.items())), self.wrapped)


class _SelectionFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ContextUnavailable(Exception):
    pass


def _is_count(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _cancel_pending() -> None:
    """Raise CancelledError when this task has a pending cancellation request (one that
    a lower layer converted into another outcome without un-cancelling the task)."""

    task = asyncio.current_task()
    if task is not None and task.cancelling() > 0:
        raise asyncio.CancelledError()


def _cursor(key: Key | None) -> dict[str, str] | None:
    if key is None:
        return None
    return {"deadline": key.deadline.astimezone(UTC).strftime(_TS_FORMAT),
            "reminder_id": str(key.reminder_id)}


def _keys(rows: Any) -> list[Key]:
    if type(rows) is not list or len(rows) != 1 or len(rows[0]) != 1:
        raise _SelectionFailed("SELECTION_FAILED")
    result = rows[0][0]
    if not isinstance(result, Mapping) or set(result) != {"schema_version", "keys"} \
            or result["schema_version"] != 1 or type(result["keys"]) is not list:
        raise _SelectionFailed("SELECTION_FAILED")
    out: list[Key] = []
    for item in result["keys"]:
        if not isinstance(item, Mapping) or set(item) != {"deadline", "reminder_id"} \
                or type(item["deadline"]) is not str or not _TS.fullmatch(item["deadline"]) \
                or type(item["reminder_id"]) is not str:
            raise _SelectionFailed("SELECTION_FAILED")
        try:
            deadline = datetime.strptime(item["deadline"], _TS_FORMAT).replace(tzinfo=UTC)
            reminder = UUID(item["reminder_id"])
        except ValueError:
            raise _SelectionFailed("SELECTION_FAILED") from None
        out.append(Key(deadline, reminder))
    if out != sorted(out) or len(set(out)) != len(out):
        raise _SelectionFailed("SELECTION_FAILED")
    return out


class DueWindowRunner:
    def __init__(self, *, gateway: Any, resolver: Any, intake: IntakeService,
                 principal: Principal, page_limit: int, attempt_cap: int) -> None:
        if principal.kind is not PrincipalKind.WORKLOAD:
            raise SBRefused("PRINCIPAL_NOT_WORKLOAD")
        if principal.capabilities != TIMER_CAPS:
            raise SBRefused("CAPABILITIES_INVALID")
        if not _is_count(page_limit, 1, PAGE_MAX) \
                or not _is_count(attempt_cap, page_limit, CAP_MAX):
            raise SBRefused("LIMIT_INVALID")
        self._gateway = gateway
        self._resolver = resolver
        self._intake = intake
        self._principal = principal
        self._page = page_limit
        self._cap = attempt_cap
        self._busy: set[str] = set()
        self._resume: dict[str, Key] = {}

    def resume_key(self, tenant_id: str) -> Key | None:
        return self._resume.get(tenant_id)

    async def run_once(self, tenant_id: str, execution_id: UUID) -> RunResult:
        if type(tenant_id) is not str or not tenant_id \
                or tenant_id not in self._principal.authorized_tenant_ids:
            raise SBRefused("TENANT_INVALID")
        if type(execution_id) is not UUID:
            raise SBRefused("EXECUTION_ID_INVALID")
        if tenant_id in self._busy:  # synchronous: no await before this point
            return _Counts().result("RUN_IN_PROGRESS")
        self._busy.add(tenant_id)
        counts = _Counts()
        try:
            code = await self._walk(tenant_id, execution_id, counts)
        except asyncio.CancelledError:
            self._log(tenant_id, "CANCELLED", counts)
            raise
        finally:
            self._busy.discard(tenant_id)
        self._log(tenant_id, code, counts)
        return counts.result(code)

    # -- the walk (pins P4) -----------------------------------------------------------

    async def _walk(self, tenant: str, execution: UUID, counts: _Counts) -> str:
        start = self._resume.get(tenant)
        after_a: Key | None = start
        after_b: Key | None = None
        phase_b = False
        while True:
            if counts.attempted >= self._cap:
                return "CAP"
            if phase_b and start is not None and after_b is not None and after_b >= start:
                self._resume.pop(tenant, None)  # the circuit reached its start: complete
                return "COMPLETE"
            _cancel_pending()
            try:
                page = await self._select(tenant, execution, after_b if phase_b else after_a,
                                          start if phase_b else None)
            except _SelectionFailed as failure:
                return failure.code
            for key in page:
                if counts.attempted >= self._cap:
                    return "CAP"
                _cancel_pending()
                try:
                    context = await self._context(tenant, execution, CLOSE_CAPS)
                except _ContextUnavailable:
                    return "CONTEXT_UNAVAILABLE"
                _cancel_pending()
                counts.attempted += 1
                self._resume[tenant] = key  # checkpoint: marked before the invocation
                if phase_b:
                    after_b = key
                else:
                    after_a = key
                await self._close(context, key, counts)
                _cancel_pending()
            if len(page) < self._page:
                if phase_b or start is None:
                    self._resume.pop(tenant, None)
                    return "COMPLETE"
                phase_b = True
                counts.wrapped = True

    async def _context(self, tenant: str, execution: UUID, caps: frozenset[str]) -> Any:
        try:
            context = await self._resolver.resolve(
                principal=self._principal, tenant_hint=tenant, purpose=PURPOSE,
                capabilities=caps, source=TrustedSource.WORKER, execution_id=execution,
                correlation_id=uuid4(),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
            )
        except Exception:  # noqa: BLE001 - classified by code only, no text kept
            raise _ContextUnavailable() from None
        if frozenset(context.capabilities) != caps:
            raise _ContextUnavailable()
        return context

    async def _select(self, tenant: str, execution: UUID, after: Key | None,
                      until: Key | None) -> list[Key]:
        try:
            context = await self._context(tenant, execution, SELECT_CAPS)
        except _ContextUnavailable:
            raise _SelectionFailed("SELECTION_FAILED") from None
        command = {"schema_version": 1, "after": _cursor(after), "until": _cursor(until),
                   "limit": self._page, "attribution": _attribution(context)}

        async def callback(handle: Any) -> Any:
            return await handle.fetch_all(DUE_KEY, {"command": Jsonb(command)})

        try:
            rows = await self._gateway.with_tenant_transaction(context, callback)
        except psycopg.Error as error:
            code = _code(error)
            raise _SelectionFailed(code if code in SELECTOR_CODES
                                   else "SELECTION_FAILED") from None
        except Exception:  # noqa: BLE001 - classified by code only, no text kept
            raise _SelectionFailed("SELECTION_FAILED") from None
        return _keys(rows)

    async def _close(self, context: Any, key: Key, counts: _Counts) -> None:
        try:
            result = await self._intake.close_window(context, key.reminder_id)
        except asyncio.CancelledError:
            counts.cancelled += 1  # attempted, no CloseResult (cancelled before the marker)
            raise
        except IntakeCommitUnknown:
            counts.commit_unknown += 1
            return
        except (IntakeRefused, IntakeFailed) as error:
            counts.fail(error.code)
            return
        except Exception:  # noqa: BLE001 - classified by code only, no text kept
            counts.fail("INTERNAL")
            return
        _classify(result, counts)

    @staticmethod
    def _log(tenant: str, code: str, counts: _Counts) -> None:
        failed = ",".join(f"{k}:{v}" for k, v in sorted(counts.failed.items()))
        LOGGER.info(
            "due_windows run tenant=%s code=%s attempted=%d no_reply=%d opted_out=%d"
            " not_awaiting=%d not_due=%d commit_unknown=%d cancelled=%d failed=%s wrapped=%s",
            tenant, code, counts.attempted, counts.no_reply, counts.opted_out,
            counts.not_awaiting, counts.not_due, counts.commit_unknown, counts.cancelled,
            failed, counts.wrapped,
        )


def _classify(result: CloseResult, counts: _Counts) -> None:
    if result.decision == "no_reply":
        counts.no_reply += 1
    elif result.decision == "opted_out":
        counts.opted_out += 1
    elif result.outcome == "not_awaiting":
        counts.not_awaiting += 1
    elif result.outcome == "not_due":
        counts.not_due += 1
    else:
        counts.fail("RESULT_INVALID")
