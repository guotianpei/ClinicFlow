"""CP2-1 typed-plan harness -- an EXTENSION of `recording.py`, which it does not edit.

`recording.py` is part of the frozen v11 baseline (sha256 `14e3bd3b...`). Nothing
here changes it. Everything the typed cases need beyond it lives in this module
and is reached through `conftest.py` fixtures, per that file's convention.

WHAT IT ADDS, AND WHY EACH ONE IS NEEDED
----------------------------------------
`TypedConnection` keeps everything `RecordingConnection` records and adds:

  raw      The query object exactly as the runner passed it, beside the trace
           sequence number. TP-11 is about BYTES. The v11 trace stores text, and
           text decoded from bytes with `surrogateescape` can hide a difference
           in encoding. `executed_bytes` answers from `raw`, not from the text.

  faults   Raise a real psycopg error on a matching statement, AFTER it has been
           recorded. `B-late-failure` needs the first unit's DDL to fail after
           its `running` row committed. The raised error carries a genuine
           SQLSTATE, so `_sanitize` sees what it would see from a server.

  hooks    Run a callback when a matching statement is issued. `B-after-await`
           mutates the source envelope at a real `await` point after the
           snapshot, rather than at a point the test merely hopes comes later.

`CallSpy` wraps a callable, records each call's position on the SHARED clock and
any exception it raised, and delegates. Because it ticks the same `SharedClock`
as the connections, "the checker was called before the first `running` write"
is a comparison of two sequence numbers in one true order (TP-10), not an
inference from two separate lists.

WHAT IT IS NOT
--------------
Everything `recording.py` says about itself applies unchanged: it records that a
call was made. It is not PostgreSQL. A fault here is a raised Python exception
of a psycopg class; it is not evidence of how a server fails.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import recording
from psycopg.sql import Composable

__all__ = [
    "CallSpy",
    "Fault",
    "Hook",
    "SpyCall",
    "TypedConnection",
    "executed_bytes",
    "intercept",
]


@dataclass(slots=True)
class Fault:
    """Raise `error()` once, on the first statement whose fingerprint holds every marker."""

    markers: tuple[str, ...]
    error: Callable[[], BaseException]
    fired: bool = False

    def matches(self, fingerprint: str) -> bool:
        return all(marker.casefold() in fingerprint for marker in self.markers)


@dataclass(slots=True)
class Hook:
    """Run `action()` once, when the first matching statement is issued."""

    markers: tuple[str, ...]
    action: Callable[[], None]
    fired: bool = False

    def matches(self, fingerprint: str) -> bool:
        return all(marker.casefold() in fingerprint for marker in self.markers)


@dataclass(slots=True)
class TypedConnection(recording.RecordingConnection):
    """`RecordingConnection` plus raw capture, fault injection and hooks."""

    raw: list[tuple[int, object]] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    hooks: list[Hook] = field(default_factory=list)

    def _issue(
        self, channel: str, query: object, params: Sequence[Any] | None
    ) -> tuple[tuple[Any, ...], ...]:
        # Explicit base call: zero-argument `super()` is unreliable in a
        # `slots=True` dataclass, which is rebuilt as a new class.
        rows = recording.RecordingConnection._issue(self, channel, query, params)
        entry = self.trace[-1]
        self.raw.append((entry.seq, query))
        for hook in self.hooks:
            if not hook.fired and hook.matches(entry.fingerprint):
                hook.fired = True
                hook.action()
        for fault in self.faults:
            if not fault.fired and fault.matches(entry.fingerprint):
                fault.fired = True
                raise fault.error()
        return rows


def executed_bytes(connection: TypedConnection) -> tuple[bytes, ...]:
    """Every executed query as bytes, from the RAW object, in issue order.

    `str` is encoded strictly as UTF-8 -- the encoding the checker bound with
    (`_render_exact_sql`). A `Composable` is not a candidate for bound bytes and
    is excluded. Anything else raises rather than being guessed at.
    """

    result: list[bytes] = []
    for _, query in connection.raw:
        if isinstance(query, bytes):
            result.append(query)
        elif isinstance(query, str):
            result.append(query.encode("utf-8", "strict"))
        elif isinstance(query, Composable):
            continue
        else:
            raise AssertionError(f"unrecognised query object {type(query).__name__}")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SpyCall:
    seq: int
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    raised: BaseException | None


@dataclass(slots=True)
class CallSpy:
    """Records calls on the shared clock and delegates to the real callable."""

    clock: recording.SharedClock
    calls: list[SpyCall] = field(default_factory=list)

    def wrap(self, real: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(real):

            async def spied_async(*args: Any, **kwargs: Any) -> Any:
                seq = self.clock.tick()
                try:
                    result = await real(*args, **kwargs)
                except BaseException as error:
                    self.calls.append(SpyCall(seq, args, kwargs, error))
                    raise
                self.calls.append(SpyCall(seq, args, kwargs, None))
                return result

            return spied_async

        def spied(*args: Any, **kwargs: Any) -> Any:
            seq = self.clock.tick()
            try:
                result = real(*args, **kwargs)
            except BaseException as error:
                self.calls.append(SpyCall(seq, args, kwargs, error))
                raise
            self.calls.append(SpyCall(seq, args, kwargs, None))
            return result

        return spied

    @property
    def raised(self) -> tuple[BaseException, ...]:
        return tuple(call.raised for call in self.calls if call.raised is not None)


def intercept(
    monkeypatch: Any,
    seam: tuple[Any, str],
    *,
    before: Callable[..., None] | None = None,
    after: Callable[[Any], Any] | None = None,
) -> None:
    """Replace `seam` with a wrapper: `before(*args, **kwargs)`, then the real
    callable, then `after(result)`, whose return value is passed on.

    Sync or async is taken from the real callable, so a case does not depend on
    which the implementation chooses.
    """

    owner, name = seam
    real = getattr(owner, name)

    if inspect.iscoroutinefunction(real):

        async def wrapped_async(*args: Any, **kwargs: Any) -> Any:
            if before is not None:
                before(*args, **kwargs)
            result = await real(*args, **kwargs)
            return after(result) if after is not None else result

        monkeypatch.setattr(owner, name, wrapped_async)
        return

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if before is not None:
            before(*args, **kwargs)
        result = real(*args, **kwargs)
        return after(result) if after is not None else result

    monkeypatch.setattr(owner, name, wrapped)
