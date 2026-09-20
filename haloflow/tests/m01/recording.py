"""A recording connection for driving the real `TenantMigrationRunner`.

WHY THIS EXISTS
---------------
`TenantMigrationRunner.__init__` takes `connect: ConnectionFactory`, which is
`Callable[[], Awaitable[AsyncConnection]]`. That parameter is the seam: a test
supplies the factory, so the runner can be driven end to end without a server
and without adding a single production hook. Nothing in this module patches,
monkeypatches or wraps production code.

ONE ORDERED TRACE, AND TRANSACTION IDENTITY
-------------------------------------------
Everything -- statements, transaction boundaries, cursor blocks, autocommit,
close -- goes into ONE ordered list, `trace`. Each transaction block gets a
unique, monotonically increasing id, and every statement records the id of the
innermost block it was issued inside.

That identity is the point. An earlier version of this harness kept statements
and transaction events in two separate lists and asserted "INSERT before DDL,
both at depth 1". Codex showed that proves nothing: both statements could sit in
the FIRST block with the second block empty, and the assertion still passes.
Depth is not identity. With ids, "the INSERT is in transaction 1 and the DDL is
in transaction 2" is a statement the trace can actually answer.

WHAT IT REFUSES
---------------
`UnscriptedQuery` on any read the test did not declare an answer for. A fake
that returns `None` for an unrecognized SELECT will happily let a test pass for
a reason the author never intended; this one stops.

`UnsupportedStatement` on any statement whose shape it cannot classify as a read
or a write. Failing closed matters: an earlier version decided "is this a read?"
by testing whether the text started with `select`, which silently classified a
leading-comment query, a CTE and an `INSERT ... RETURNING` as writes and
answered them with no rows.

WHAT IT IS NOT
--------------
It is not PostgreSQL, and the distinction is not a formality. It records that a
call was MADE. It cannot tell you that the SQL was valid, that a privilege was
enforced, that a transaction durably committed, or what the server would do with
any particular byte sequence. `txn-commit` in the trace means a Python context
manager exited without an exception -- not a commit.

So a test here may assert that the runner issued an execute carrying a given
byte; it may NOT conclude anything about how a server would treat that byte.
Every claim of that kind belongs to the `D` layer on a real PostgreSQL 17 and is
out of scope for this module.

Statement parameters are recorded, and a test may assert on them. Answers do NOT
match on parameters: two reads of the same shape with different parameters get
the same declared answer. A test that needs to distinguish them has to do it
another way.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from psycopg.sql import Composable

__all__ = [
    "MIGRATOR_SAFE",
    "SharedClock",
    "NO_CONTROLLED_EDGES",
    "Answer",
    "RecordingConnection",
    "TraceEntry",
    "UnscriptedQuery",
    "UnsupportedStatement",
    "advisory_lock_taken",
    "advisory_lock_released",
    "connection_factory",
    "ledger_absent",
    "ledger_row",
    "lock_timeout_set",
    "merged_trace",
]


class UnscriptedQuery(AssertionError):
    """A read reached the fake that no test declared an answer for."""


class UnsupportedStatement(AssertionError):
    """A statement the fake cannot classify as a read or a write."""


def _text(query: object) -> str:
    """The statement as text, for both `str` and psycopg `Composable` queries.

    `runner.py` issues both: plain strings for the ledger statements and
    `sql.SQL(...).format(...)` for anything carrying an identifier.
    `as_string(None)` is the documented no-context rendering and quotes
    identifiers exactly as psycopg would send them.
    """

    if isinstance(query, Composable):
        return query.as_string(None)
    if isinstance(query, bytes):
        return query.decode("utf-8", errors="surrogateescape")
    return str(query)


def _fingerprint(text: str) -> str:
    """Whitespace-collapsed, case-folded text, for matching an answer.

    Matching on a fingerprint rather than exact bytes lets a test name WHICH
    read it is answering without pinning the runner's indentation. The trace
    keeps the original text, so assertions about the bytes executed are
    unaffected by this normalization.
    """

    return " ".join(text.split()).casefold()


_COMMENT = re.compile(r"(--[^\n]*\n)|(/\*.*?\*/)", re.S)

# Leading keywords that produce rows the runner may consume.
_READ_VERBS = frozenset({"select", "with", "values", "table", "show", "explain"})

# Leading keywords that produce no rows here. `INSERT`/`UPDATE`/`DELETE` are
# reads when they carry RETURNING, which is checked separately.
_WRITE_VERBS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "set",
        "reset",
        "create",
        "alter",
        "drop",
        "grant",
        "revoke",
        "comment",
        "truncate",
        "lock",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "release",
        "analyze",
        "vacuum",
    }
)

_RETURNING = re.compile(r"\breturning\b")


def _classify(text: str) -> str:
    """`"read"` or `"write"`, or raise rather than guess.

    Comments are stripped first, so a statement that opens with `--` is
    classified by its actual first keyword. `RETURNING` promotes a write to a
    read. An unrecognized leading keyword raises: the alternative is answering
    it with no rows, which is indistinguishable from a real empty result and is
    how a test comes to pass for the wrong reason.
    """

    stripped = _COMMENT.sub(" ", text).strip()
    if not stripped:
        raise UnsupportedStatement("empty statement reached the recording connection")

    verb = stripped.split(None, 1)[0].casefold().lstrip("(")
    if verb in _READ_VERBS:
        return "read"
    if verb in _WRITE_VERBS:
        return "read" if _RETURNING.search(_fingerprint(stripped)) else "write"
    raise UnsupportedStatement(
        "this fake cannot classify the statement below as a read or a write, and will "
        "not guess. Add its leading keyword to _READ_VERBS or _WRITE_VERBS in "
        f"recording.py once you have decided which it is:\n  {stripped[:200]}"
    )


@dataclass(slots=True)
class SharedClock:
    """One monotonic counter shared by every connection in a test.

    `apply` drives TWO connections: the lock is taken on one and the work
    happens on the other. Per-connection traces cannot order events between
    them, so an assertion built from two separate traces would pass a runner
    that released the advisory lock BEFORE doing any work -- the same class of
    defect as comparing transaction depth instead of transaction identity, one
    level up.

    Every trace entry on every connection takes a tick from this counter, so
    `merged_trace` can put them in one true order.
    """

    _issued: int = 0

    def tick(self) -> int:
        self._issued += 1
        return self._issued


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One thing that happened, in order, on the recording connection."""

    kind: str
    """`statement`, `txn-begin`, `txn-commit`, `txn-rollback`, `cursor-open`,
    `cursor-close`, `autocommit`, `close`."""

    seq: int = 0
    """Position in the SHARED order across every connection in the test."""

    connection: str = ""
    """Which connection issued it, for cross-connection ordering assertions."""

    txn: int | None = None
    """The unique id of the innermost open transaction, or `None` outside one.

    Unique across the connection's lifetime, so two sequential blocks are 1 and
    2 rather than both being "depth 1".
    """

    text: str | None = None
    """For a statement: the text as sent, identifiers already quoted."""

    params: tuple[Any, ...] | None = None
    channel: str | None = None
    """`connection` or `cursor` -- which API the runner used."""

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.text) if self.text is not None else ""


@dataclass(frozen=True, slots=True)
class Answer:
    """Rows to return for a read whose fingerprint contains every marker.

    Markers rather than a whole-statement match: the runner's catalogue reads
    are long and a test should name the part that identifies them, not restate
    them.

    The duplicate check below rejects the case where SEVERAL declared answers
    match ONE query -- that is, answers whose markers overlap. It does NOT
    detect one loose answer matching several different queries; that answer
    simply serves all of them. Markers should therefore be chosen to identify a
    read, not merely to appear in it.

    Parameters are not part of matching. Two reads of the same shape with
    different parameters receive the same answer.
    """

    markers: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    def matches(self, fingerprint: str) -> bool:
        return all(marker.casefold() in fingerprint for marker in self.markers)


# The shipped manifest declares no role memberships, so the controlled-edge read
# must come back empty for `assess_membership_graph`'s set equality to hold.
NO_CONTROLLED_EDGES = Answer(markers=("from pg_auth_members",), rows=())

# `assert_execution_roles_safe` ends by requiring the migrator to exist and to
# lack CREATEROLE. One row, `rolcreaterole` false.
MIGRATOR_SAFE = Answer(markers=("select rolcreaterole", "pg_roles"), rows=((False,),))

# `tenant_lock` issues these three through `connection.execute`. All are reads:
# `set_config`, `pg_advisory_lock` and `pg_advisory_unlock` are function calls
# in a SELECT list, not SET statements.
lock_timeout_set = Answer(markers=("set_config", "lock_timeout"), rows=(("30000ms",),))
advisory_lock_taken = Answer(markers=("pg_advisory_lock(",), rows=((None,),))
advisory_lock_released = Answer(markers=("pg_advisory_unlock(",), rows=((True,),))


def ledger_absent() -> Answer:
    """No ledger row for this unit: the first-attempt path."""

    return Answer(markers=("from shared.schema_migrations",), rows=())


def ledger_row(state: str, checksum: str) -> Answer:
    """An existing ledger row, for the already-applied and drift paths."""

    return Answer(markers=("from shared.schema_migrations",), rows=((state, checksum),))


@dataclass(slots=True)
class _Result:
    """What `await connection.execute(...)` yields: a cursor-like result."""

    _rows: tuple[tuple[Any, ...], ...]

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


@dataclass(slots=True)
class _Cursor:
    _connection: RecordingConnection

    async def execute(self, query: object, params: Sequence[Any] | None = None) -> _Cursor:
        self._connection._rows = self._connection._issue("cursor", query, params)
        return self

    async def fetchone(self) -> tuple[Any, ...] | None:
        rows = self._connection._rows
        return rows[0] if rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._connection._rows)


@dataclass(slots=True)
class RecordingConnection:
    """Stands in for `AsyncConnection` across every call `runner.py` makes.

    The method surface is exactly what the runner touches and nothing more:
    `set_autocommit`, `execute`, `cursor`, `transaction`, `close`. A runner
    change that reaches for a sixth method fails loudly with `AttributeError`
    rather than being silently absorbed.
    """

    answers: list[Answer] = field(default_factory=list)
    name: str = "connection"
    clock: SharedClock = field(default_factory=SharedClock)
    trace: list[TraceEntry] = field(default_factory=list)
    autocommit: bool | None = None
    closed: bool = False
    _open: list[int] = field(default_factory=list)
    _transactions: int = 0
    _rows: tuple[tuple[Any, ...], ...] = ()

    # -- the surface the runner uses -------------------------------------

    async def set_autocommit(self, value: bool) -> None:
        self.autocommit = value
        self.trace.append(self._entry("autocommit"))

    async def execute(self, query: object, params: Sequence[Any] | None = None) -> _Result:
        return _Result(self._issue("connection", query, params))

    def cursor(self) -> Any:
        return self._cursor_block()

    @asynccontextmanager
    async def _cursor_block(self) -> Any:
        self.trace.append(self._entry("cursor-open"))
        try:
            yield _Cursor(self)
        finally:
            self.trace.append(self._entry("cursor-close"))

    def transaction(self) -> Any:
        return self._transaction_block()

    @asynccontextmanager
    async def _transaction_block(self) -> Any:
        self._transactions += 1
        identifier = self._transactions
        self._open.append(identifier)
        self.trace.append(self._entry("txn-begin", txn=identifier))
        try:
            yield None
        except BaseException:
            self._open.pop()
            self.trace.append(self._entry("txn-rollback", txn=identifier))
            raise
        self._open.pop()
        self.trace.append(self._entry("txn-commit", txn=identifier))

    async def close(self) -> None:
        self.closed = True
        self.trace.append(self._entry("close"))

    # -- recording -------------------------------------------------------

    @property
    def _current(self) -> int | None:
        return self._open[-1] if self._open else None

    def _entry(self, kind: str, **extra: Any) -> TraceEntry:
        extra.setdefault("txn", self._current)
        return TraceEntry(kind=kind, seq=self.clock.tick(), connection=self.name, **extra)

    def _issue(
        self, channel: str, query: object, params: Sequence[Any] | None
    ) -> tuple[tuple[Any, ...], ...]:
        text = _text(query)
        entry = self._entry(
            "statement",
            text=text,
            params=tuple(params) if params is not None else None,
            channel=channel,
        )
        self.trace.append(entry)
        if _classify(text) == "write":
            return ()
        return self._answer(entry)

    def _answer(self, entry: TraceEntry) -> tuple[tuple[Any, ...], ...]:
        matched = [answer for answer in self.answers if answer.matches(entry.fingerprint)]
        if not matched:
            raise UnscriptedQuery(
                "no declared answer for this read, so the fake would have invented one:\n"
                f"  {(entry.text or '').strip()}"
            )
        if len(matched) > 1:
            raise UnscriptedQuery(
                f"{len(matched)} declared answers match this read; their markers overlap:\n"
                f"  {(entry.text or '').strip()}"
            )
        return matched[0].rows

    # -- what tests assert against ---------------------------------------

    @property
    def statements(self) -> tuple[TraceEntry, ...]:
        return tuple(entry for entry in self.trace if entry.kind == "statement")

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(entry.text or "" for entry in self.statements)

    @property
    def kinds(self) -> tuple[str, ...]:
        """Every trace entry's kind, in order. For asserting shape."""

        return tuple(entry.kind for entry in self.trace)

    def index_of(self, predicate: Any) -> int:
        """The single trace position matching `predicate`, or raise.

        Single, deliberately. An ordering assertion built on "the first match"
        is silently wrong the moment a second match appears.
        """

        found = [index for index, entry in enumerate(self.trace) if predicate(entry)]
        if len(found) != 1:
            raise AssertionError(f"expected exactly one matching trace entry, found {len(found)}")
        return found[0]

    def issued(self, *markers: str) -> tuple[TraceEntry, ...]:
        """Every statement whose fingerprint contains all the markers."""

        wanted = [marker.casefold() for marker in markers]
        return tuple(
            entry
            for entry in self.statements
            if all(marker in entry.fingerprint for marker in wanted)
        )


def merged_trace(*connections: RecordingConnection) -> tuple[TraceEntry, ...]:
    """Every connection's entries in ONE true order, by the shared clock.

    The connections must share a `SharedClock`; otherwise their sequence
    numbers collide and the merge is meaningless, so that is checked rather
    than assumed.
    """

    clocks = {id(connection.clock) for connection in connections}
    if len(clocks) != 1:
        raise AssertionError(
            "these connections do not share a SharedClock, so their entries cannot be "
            "ordered against each other; build them from one clock"
        )
    return tuple(sorted((e for c in connections for e in c.trace), key=lambda e: e.seq))


def connection_factory(*connections: RecordingConnection) -> Any:
    """A `ConnectionFactory` handing out the given connections in order.

    `apply` acquires two: one for the lock, one for the work. `apply_within_lock`
    acquires one. Exhausting the supply raises rather than reusing the last, so
    a test that gave one connection to a path that takes two finds out.
    """

    remaining = list(connections)

    async def factory() -> RecordingConnection:
        if not remaining:
            raise AssertionError(
                "the runner asked for more connections than this test supplied; "
                "`apply` takes two (lock, work) and `apply_within_lock` takes one"
            )
        return remaining.pop(0)

    return factory
