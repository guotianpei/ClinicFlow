"""L-6 tenant schema upgrade, CP-3 slice (architecture v6 r3; plan v4 §2).

CP-3 adds the maintenance capability and its fence (F, §2 statements 1-3), the two
claim forms (CL, CLc), the committed-state classifier (§8), the establishment steps
C1, C2 and C3 (§7), the owner-authorized abandon AB (§7), and the outer-attempt
``attempt_refused`` evidence (IP-14 r3, A1-A7). Every later step (NB onward) stops
at the CP-3 boundary with ``CheckpointBoundaryReached``, before any transaction and
without writing anything.

Actors (§1): P is ``haloflow_provisioner`` and M is ``haloflow_migrator``, each
reached through its login shim and ``SET ROLE``; K is a dedicated migrator session
holding the runner's per-tenant session advisory lock (Q11: same factory, identity,
namespace and key as ``TenantMigrationRunner.tenant_lock``).

Evidence (IP-14): an outer attempt that refuses appends exactly one
``attempt_refused{code, phase}`` on a separate autocommit P connection, after the
refused step's transaction has ended, and does not retry. The operation is named
only when this attempt established an authoritative match with it: a committed claim,
or a fence whose statement 2 returned the row and whose binding checks passed (IP-14
r3 A3, with the proposed W5 R-b clarification); otherwise it is NULL. Direct calls
(``validate_capability``, ``fence``, ``claim``, ``classify``) append nothing. An append
that fails is reported as ``MAINTENANCE_EVIDENCE_WRITE_FAILED`` with the real driver
error as ``__cause__``.

Test seams (``UpgradeTestHooks``) are wired only by a test composition; the
production builder passes ``hooks=None``. No seam changes SQL text, fabricates a
result or skips a check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection, sql
from psycopg import Error as PsycopgError

from haloflow.m01.errors import M01Error
from haloflow.m01.provisioning.codes import MaintenanceCode, SanitizedErrorCode
from haloflow.m01.provisioning.roles import (
    AUDIT_PROJECTOR_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    RUNTIME_ROLE,
)
from haloflow.m01.provisioning.runner import (
    MIGRATION_LOCK_NAMESPACE,
    ConnectionFactory,
    require_explicit_transactions,
    tenant_lock_key,
)
from haloflow.m01.provisioning.units import TenantMigrationRegistry

OPERATIONS: Final = "shared.tenant_maintenance_operations"
WITHHELD: Final = "shared.tenant_maintenance_withheld"
ATTEMPTS: Final = "shared.tenant_maintenance_attempts"

FROM_VERSION: Final = 2
TO_VERSION: Final = 3

# Tenant objects named by architecture v6 r3 §4 (created by t001 and t002).
OUTBOX_TABLE: Final = "access_audit_outbox"
REGISTRY_TABLE: Final = "operation_registry"
REJECTOR_FUNCTION: Final = "operation_registry_reject"
REJECTOR_ARGUMENT_TYPES: Final = ""  # a trigger function takes no input arguments
REJECTOR_PROKIND: Final = "f"

ENTRY_REASON: Final = "l6_maintenance_entry"
RESUME_REASON: Final = "l6_maintenance_resume"
UPGRADE_ACTOR_KIND: Final = "workload"
UPGRADE_ACTOR_ID: Final = "haloflow-l6-tenant-upgrade"

_DECISION_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_OPEN_STATES: Final = ("establishing", "entered", "activated")


# --- refusal and stop types ---------------------------------------------------


class MaintenanceRefused(M01Error):
    """An L-6 refusal: ``reason_code`` (a ``MaintenanceCode`` value, or
    ``LOCK_UNAVAILABLE``) and ``phase`` (an ``attempt_refused`` phase value)."""

    code = "MAINTENANCE_REFUSED"
    public_message = "Tenant maintenance refused"

    def __init__(self, reason_code: str, phase: str) -> None:
        super().__init__(reason_code=reason_code)
        self.phase = phase
        # IP-14 A3: the operation is associated only when the refusing fence matched
        # it. Set by the fence; never read from a capability.
        self.matched_operation: UUID | None = None


class CheckpointBoundaryReached(Exception):  # noqa: N818 - name fixed by the interface
    """Q10: the next step is not implemented at CP-3. Raised before any transaction
    of that step; nothing is written and nothing is reported as completed."""

    def __init__(self, step: str) -> None:
        super().__init__(f"CP-3 boundary before step {step}")
        self.step = step


class InjectedFault(Exception):  # noqa: N818 - name fixed by the interface
    """Raised only by a test seam (``step_fault`` crash, ``k_session_fault``)."""


@dataclass(frozen=True, slots=True)
class InterferenceObservation:
    """Addendum 4 r3 D4: the committed state the stopping step observed, unrepaired."""

    lifecycle_state: str
    schema_version: int
    operation_state: str
    t003_state: str | None
    t003_attempt: int | None
    denial_status: str
    safe_denial_claimed: bool = False


class MaintenanceInterferenceStopped(Exception):  # noqa: N818 - name fixed by Q-P6
    """Q-P6 (a): a T-2 check found interference; the attempt appended
    ``interference_detected{point, kind}`` and stopped. Not a refusal."""

    def __init__(
        self, point: str, kind: str, phase: str, observed: InterferenceObservation
    ) -> None:
        super().__init__(f"maintenance interference at point {point} ({kind}) in {phase}")
        self.point = point
        self.kind = kind
        self.phase = phase
        self.observed = observed


def _refused(code: str, phase: str) -> MaintenanceRefused:
    return MaintenanceRefused(code, phase)


# --- values -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MaintenanceCapability:
    """The in-process fence capability (§2.4). A token value alone is never accepted."""

    tenant_id: str
    operation_id: UUID
    attempt_id: UUID
    key: int
    k_pid: int
    database: str


def validate_capability(cap: object) -> None:
    """Pure capability self-check (TC-F03a, TC-F04). Opens nothing, appends nothing.

    Refuses ``MAINTENANCE_TOKEN_INVALID`` / ``capability`` for anything that is not a
    ``MaintenanceCapability``, a missing or mistyped field, or a key that is not
    ``tenant_lock_key(tenant_id)``.
    """

    invalid = _refused(MaintenanceCode.MAINTENANCE_TOKEN_INVALID.value, "capability")
    if not isinstance(cap, MaintenanceCapability):
        raise invalid
    if not isinstance(cap.tenant_id, str) or not cap.tenant_id:
        raise invalid
    if not isinstance(cap.operation_id, UUID) or not isinstance(cap.attempt_id, UUID):
        raise invalid
    if not isinstance(cap.database, str) or not cap.database:
        raise invalid
    for number in (cap.key, cap.k_pid):
        if type(number) is not int:
            raise invalid
    if cap.k_pid <= 0 or cap.key != tenant_lock_key(cap.tenant_id):
        raise invalid


@dataclass(frozen=True, slots=True)
class OwnerAuthorization:
    """Q6: Rachel's recorded decision for an owner-only action, by attestation hash."""

    decision_sha256: str
    tenant_id: str
    operation_id: UUID
    action: str


@dataclass(frozen=True, slots=True)
class UpgradeRequest:
    tenant_id: str
    operation_id: UUID | None


class KState(StrEnum):
    K0 = "K0"
    K1 = "K1"
    K2 = "K2"
    K3 = "K3"
    K3b = "K3b"
    K3n = "K3n"
    K3x = "K3x"
    K3z = "K3z"
    K3d = "K3d"
    K4 = "K4"
    K5 = "K5"
    K5f = "K5f"
    K6 = "K6"
    K7 = "K7"
    K7b = "K7b"
    K8 = "K8"
    K9 = "K9"
    K10 = "K10"
    K10b = "K10b"
    K11 = "K11"
    K12 = "K12"
    X = "X"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Classification:
    state: KState
    generation: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class KHandle:
    """The trusted K session identity. ``lock_timeout_seconds`` is the composition's
    T_lock, used by statement 1 of every fence and claim taken under this K."""

    pid: int
    database: str
    key: int
    lock_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class OperationRow:
    operation_id: UUID
    tenant_id: str
    state: str
    current_attempt: UUID | None
    neutralization_generation: int


@dataclass(frozen=True, slots=True)
class UpgradeOutcome:
    attempt_id: UUID
    operation_id: UUID | None
    final_state: KState


# --- test seams (B5.1 v3 §4.4; owner records for capability_fault) -------------


class AppenderProbe(Protocol):
    """Q3 seam on the outer ``attempt_refused`` appender only (Q-P7)."""

    def on_invocation(self) -> None: ...

    def wrap(self, conn: AsyncConnection[Any]) -> Any: ...


StepFault = Literal["crash", "lost_ack"]
KSessionFault = Literal["raise_after_lock", "cancel_after_lock"]
CapabilityFault = Literal["missing_attempt", "tenant", "k_pid", "database"]


@dataclass(frozen=True, slots=True)
class UpgradeTestHooks:
    pause_after: Mapping[str, Callable[[], Awaitable[None]]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    step_fault: Mapping[str, StepFault] = field(default_factory=lambda: MappingProxyType({}))
    appender_probe: AppenderProbe | None = None
    k_session_fault: KSessionFault | None = None
    capability_fault: CapabilityFault | None = None


_NO_HOOKS: Final = UpgradeTestHooks()


# --- connections ----------------------------------------------------------------


async def _open_as(connect: ConnectionFactory, role: str) -> AsyncConnection[Any]:
    connection = await connect()
    try:
        await require_explicit_transactions(connection)
        await connection.execute("SET search_path = pg_catalog")
        await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    except BaseException:
        await connection.close()
        raise
    return connection


@asynccontextmanager
async def _session(connect: ConnectionFactory, role: str) -> AsyncIterator[AsyncConnection[Any]]:
    connection = await _open_as(connect, role)
    try:
        yield connection
    finally:
        await connection.close()


# --- K (Q11) --------------------------------------------------------------------


@asynccontextmanager
async def _hold_k(
    connect: ConnectionFactory,
    tenant_id: str,
    lock_timeout_seconds: float,
    fault: KSessionFault | None,
) -> AsyncIterator[KHandle]:
    key = tenant_lock_key(tenant_id)
    connection = await connect()
    try:
        await require_explicit_transactions(connection)
        await connection.execute("SET search_path = pg_catalog")
        await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(MIGRATOR_ROLE)))
        await connection.execute(
            "SELECT set_config('lock_timeout', %s, false)",
            (f"{int(lock_timeout_seconds * 1000)}ms",),
        )
        try:
            await connection.execute(
                "SELECT pg_advisory_lock(%s, %s)", (MIGRATION_LOCK_NAMESPACE, key)
            )
        except PsycopgError as error:
            raise _refused(SanitizedErrorCode.LOCK_UNAVAILABLE.value, "lock_acquire") from (
                _sanitized(error)
            )
        try:
            row = await (
                await connection.execute("SELECT pg_backend_pid(), current_database()")
            ).fetchone()
            if row is None:
                raise _refused(SanitizedErrorCode.LOCK_UNAVAILABLE.value, "lock_acquire")
            handle = KHandle(int(row[0]), str(row[1]), key, lock_timeout_seconds)
            if fault == "raise_after_lock":
                raise InjectedFault("k_session_fault: raise_after_lock")
            if fault == "cancel_after_lock":
                raise asyncio.CancelledError()
            yield handle
        finally:
            if not connection.broken and not connection.closed:
                # The lock is session-scoped: if the unlock cannot run, closing the
                # session releases it.
                with contextlib.suppress(PsycopgError):
                    await connection.execute(
                        "SELECT pg_advisory_unlock(%s, %s)", (MIGRATION_LOCK_NAMESPACE, key)
                    )
    finally:
        await connection.close()


@asynccontextmanager
async def hold_k_session(
    connect: ConnectionFactory, tenant_id: str, *, lock_timeout_seconds: float
) -> AsyncIterator[KHandle]:
    """Hold the runner's per-tenant session advisory lock on a dedicated migrator
    session (Q11). Refuses ``LOCK_UNAVAILABLE`` / ``lock_acquire``. Unlocks and
    closes on success, error and cancellation."""

    async with _hold_k(connect, tenant_id, lock_timeout_seconds, None) as handle:
        yield handle


# --- F (§2) ---------------------------------------------------------------------


async def _set_lock_timeout(conn: AsyncConnection[Any], seconds: float) -> None:
    await conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{int(seconds * 1000)}ms",))


async def _statement_3(conn: AsyncConnection[Any], key: int, pid: int) -> bool:
    row = await (
        await conn.execute(
            """
            SELECT 1 FROM pg_catalog.pg_locks
             WHERE locktype = 'advisory'
               AND database = (SELECT oid FROM pg_catalog.pg_database
                                WHERE datname = current_database())
               AND classid = %s::oid AND objid = %s::oid AND objsubid = 2
               AND pid = %s AND mode = 'ExclusiveLock' AND granted
            """,
            (MIGRATION_LOCK_NAMESPACE, key & 0xFFFFFFFF, pid),
        )
    ).fetchone()
    return row is not None


async def fence(conn: AsyncConnection[Any], cap: MaintenanceCapability, k: KHandle) -> OperationRow:
    """Statements 1-3 of F inside the caller's open transaction (B5.1 v3 §4.3 steps
    3-6). Writes nothing and appends nothing. The caller validated ``cap`` first."""

    await _set_lock_timeout(conn, k.lock_timeout_seconds)
    try:
        row = await (
            await conn.execute(
                f"""
                SELECT maintenance_operation_id, tenant_id, state, current_attempt,
                       neutralization_generation
                  FROM {OPERATIONS}
                 WHERE maintenance_operation_id = %s AND current_attempt = %s
                   FOR UPDATE
                """,
                (cap.operation_id, cap.attempt_id),
            )
        ).fetchone()
    except psycopg.errors.LockNotAvailable as error:
        raise _refused(SanitizedErrorCode.LOCK_UNAVAILABLE.value, "fence") from _sanitized(error)
    if row is None:
        raise _refused(MaintenanceCode.MAINTENANCE_FENCE_LOST.value, "fence")
    database = await (await conn.execute("SELECT current_database()")).fetchone()
    if (
        str(row[1]) != cap.tenant_id
        or cap.k_pid != k.pid
        or cap.key != k.key
        or database is None
        or str(database[0]) != cap.database
    ):
        raise _refused(MaintenanceCode.MAINTENANCE_TOKEN_INVALID.value, "fence")
    operation = OperationRow(
        operation_id=UUID(str(row[0])),
        tenant_id=str(row[1]),
        state=str(row[2]),
        current_attempt=None if row[3] is None else UUID(str(row[3])),
        neutralization_generation=int(row[4]),
    )
    if not await _statement_3(conn, k.key, k.pid):  # Q-P4d: the trusted handle
        lost = _refused(MaintenanceCode.MAINTENANCE_LOCK_LOST.value, "fence")
        lost.matched_operation = operation.operation_id
        raise lost
    return operation


# --- CL / CLc (§2) --------------------------------------------------------------


async def _event_exists(
    conn: AsyncConnection[Any], operation_id: UUID, event: str, gen: int | None = None
) -> bool:
    query = (
        f"SELECT 1 FROM {ATTEMPTS} WHERE maintenance_operation_id = %s AND event = %s"
        + (" AND detail ->> 'gen' = %s" if gen is not None else "")
        + " LIMIT 1"
    )
    params: tuple[Any, ...] = (operation_id, event) + ((str(gen),) if gen is not None else ())
    return (await (await conn.execute(query, params)).fetchone()) is not None


async def _insert_event(
    conn: AsyncConnection[Any],
    *,
    attempt_id: UUID,
    operation_id: UUID | None,
    tenant_id: str,
    event: str,
    detail: Mapping[str, Any],
) -> None:
    await conn.execute(
        f"INSERT INTO {ATTEMPTS} (attempt_id, maintenance_operation_id, tenant_id, event, detail) "
        "VALUES (%s, %s, %s, %s, %s::jsonb)",
        (attempt_id, operation_id, tenant_id, event, json.dumps(dict(detail))),
    )


async def claim(
    conn: AsyncConnection[Any],
    *,
    form: Literal["CL", "CLc"],
    tenant_id: str,
    operation_id: UUID,
    new_attempt_id: UUID,
    k: KHandle,
    registry: TenantMigrationRegistry | None = None,
) -> UUID:
    """One P transaction (the caller's, Q-P4b): name (tenant, operation), check the
    form's predicate, run statement 3 for the new attempt with the trusted K pid and
    key (Q-P4d), then write ``current_attempt`` and ``claimed{claim, prior_attempt}``
    only. Refuses RC-08 / ``claim``.

    The §8 classified-state predicate is applied on every invocation: CL only in
    K1-K10b except K3b, K3n and K3x; CLc only in K11 (G5/G6, Φ7, no ``completed``).
    It needs the registry (production checksums, the t003 profile), so a call
    without ``registry`` is refused RC-08 (fail closed) for both forms.

    Returns the matched operation id, read from the locked row (IP-14 r3 A3)."""

    refused = _refused(MaintenanceCode.MAINTENANCE_CLAIM_REFUSED.value, "claim")
    await _set_lock_timeout(conn, k.lock_timeout_seconds)
    try:
        row = await (
            await conn.execute(
                f"""
                SELECT state, current_attempt, neutralization_generation,
                       maintenance_operation_id
                  FROM {OPERATIONS}
                 WHERE maintenance_operation_id = %s AND tenant_id = %s
                   FOR UPDATE
                """,
                (operation_id, tenant_id),
            )
        ).fetchone()
    except psycopg.errors.LockNotAvailable as error:
        raise _refused(SanitizedErrorCode.LOCK_UNAVAILABLE.value, "claim") from _sanitized(error)
    if row is None:
        raise refused
    state, prior, generation = str(row[0]), row[1], int(row[2])
    matched = UUID(str(row[3]))
    if form == "CL":
        if state not in _OPEN_STATES:
            raise refused
        # [r2] CL is refused at K3b(g), K3n(g) and K3x(g): any N evidence for the
        # current generation without `neutralized` for the current generation.
        if state == "entered" and not await _event_exists(
            conn, operation_id, "neutralized", generation
        ):
            for event in (
                "neutralization_bootstrap_started",
                "neutralization_started",
                "neutralization_stopped",
            ):
                if await _event_exists(conn, operation_id, event, generation):
                    raise refused
    elif state != "released":
        raise refused
    if registry is None:
        raise refused
    snapshot = await _classify_in(conn, tenant_id, operation_id, registry)
    allowed = _CL_STATES if form == "CL" else frozenset({KState.K11})
    if snapshot.classification.state not in allowed:
        raise refused
    if k.key != tenant_lock_key(tenant_id):
        raise _refused(MaintenanceCode.MAINTENANCE_TOKEN_INVALID.value, "claim")
    if not await _statement_3(conn, k.key, k.pid):
        raise _refused(MaintenanceCode.MAINTENANCE_LOCK_LOST.value, "claim")
    await conn.execute(
        f"UPDATE {OPERATIONS} SET current_attempt = %s, updated_at = statement_timestamp() "
        "WHERE maintenance_operation_id = %s",
        (new_attempt_id, operation_id),
    )
    await _insert_event(
        conn,
        attempt_id=new_attempt_id,
        operation_id=operation_id,
        tenant_id=tenant_id,
        event="claimed",
        detail={"claim": form, "prior_attempt": None if prior is None else str(prior)},
    )
    return matched


# --- dispatch (§7, §7a, §8) -----------------------------------------------------

_NEW_RUN: Final[Mapping[KState, tuple[str, ...]]] = MappingProxyType(
    {
        KState.K0: ("C1",),
        KState.K1: ("CL", "C2"),
        KState.K2: ("CL", "C3"),
        KState.K3: ("CL", "NB", "N0"),
        KState.K3b: ("NX",),
        KState.K3n: ("NX",),
        KState.K3x: ("NX",),
        KState.K3z: ("CL", "DR"),
        KState.K3d: ("CL", "XE"),
        KState.K4: ("CL", "LO"),
        KState.K5: ("CL", "A2r"),
        KState.K5f: ("CL", "A2r"),
        KState.K6: ("CL", "A2r"),
        KState.K7: ("CL", "C2b"),
        KState.K7b: ("CL", "V"),
        KState.K8: ("CL", "AC"),
        KState.K9: ("CL", "RL1"),
        KState.K10: ("CL", "RL2"),
        KState.K10b: ("CL", "RL2"),
        KState.K11: ("CLc", "FN"),
    }
)
_LIVE: Final[Mapping[KState, tuple[str, ...]]] = MappingProxyType(
    {
        KState.K0: ("C1",),
        KState.K1: ("C2",),
        KState.K2: ("C3",),
        KState.K3: ("NB", "N0"),
        KState.K3b: ("N0",),
        KState.K3n: ("N2",),
        KState.K3x: ("NX",),
        KState.K3z: ("DR",),
        KState.K3d: ("XE",),
        KState.K4: ("LO",),
        KState.K5: ("A2r",),
        KState.K5f: ("A2r",),
        KState.K6: ("A2r",),
        KState.K7: ("C2b",),
        KState.K7b: ("V",),
        KState.K8: ("AC",),
        KState.K9: ("RL1",),
        KState.K10: ("RL2",),
        KState.K10b: ("RL2",),
        KState.K11: ("FN",),
    }
)


def next_steps(c: Classification, *, live: bool) -> tuple[str, ...]:
    """Pure dispatch (§7, §7a, §8). X, Unknown and K12 offer nothing."""

    return (_LIVE if live else _NEW_RUN).get(c.state, ())


def require_step_allowed(claim_form: Literal["CL", "CLc"], step: str) -> None:
    """After CLc only FN may run (§2); anything else refuses RC-08 / ``claim``."""

    if claim_form == "CLc" and step != "FN":
        raise _refused(MaintenanceCode.MAINTENANCE_CLAIM_REFUSED.value, "claim")


# --- classification (§4, §8) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Layout:
    """Role, unit and t003 facts the classifier needs, taken from the registry."""

    lock_owner: str | None
    baseline_ids: tuple[str, ...]
    t003_id: str
    checksums: Mapping[str, str]
    t003_function: str | None
    t003_acl: tuple[tuple[str, str, str, bool], ...]
    # (input argument types as `oidvectortypes` renders them, prokind)
    t003_signature: tuple[str, str] | None = None


def _layout(registry: TenantMigrationRegistry) -> _Layout:
    units = registry.units
    last = units[-1]
    profile = registry.installed_state_requirement(last.migration_id).profile
    return _Layout(
        lock_owner=last.execution_role,
        baseline_ids=tuple(u.migration_id for u in units[:-1]),
        t003_id=last.migration_id,
        checksums=MappingProxyType({u.migration_id: u.checksum for u in units}),
        t003_function=None if profile is None else profile.function_name,
        t003_acl=() if profile is None else tuple(profile.installed_acl),
        t003_signature=None
        if profile is None
        else (", ".join(profile.argument_types), profile.prokind),
    )


async def _rows(conn: AsyncConnection[Any], query: Any, params: Any = None) -> list[Any]:
    return list(await (await conn.execute(query, params)).fetchall())


async def _owner_default(conn: AsyncConnection[Any], objtype: str, owner: str) -> set[str]:
    return {
        str(p)
        for (p,) in await _rows(
            conn,
            'SELECT a.privilege_type FROM aclexplode(acldefault(%s::"char", %s::regrole)) a '
            "WHERE a.grantee = %s::regrole",
            (objtype, owner, owner),
        )
    }


async def _phi_actual(conn: AsyncConnection[Any], schema_key: str) -> dict[str, Any]:
    s = schema_key
    owner = await _rows(
        conn, "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s", (s,)
    )
    return {
        "schema_owner": str(owner[0][0]) if owner else None,
        "schema": {
            (str(g), str(p), str(gr), bool(o))
            for g, p, gr, o in await _rows(
                conn,
                "SELECT COALESCE(g.rolname, 'PUBLIC'), a.privilege_type, gr.rolname, "
                "a.is_grantable FROM pg_namespace n CROSS JOIN LATERAL "
                "aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) a "
                "LEFT JOIN pg_roles g ON g.oid = a.grantee "
                "LEFT JOIN pg_roles gr ON gr.oid = a.grantor WHERE n.nspname = %s",
                (s,),
            )
        },
        "relations": {
            (str(n), str(k), str(o))
            for n, k, o in await _rows(
                conn,
                "SELECT c.relname, c.relkind::text, c.relowner::regrole::text FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','S','f')",
                (s,),
            )
        },
        "tables": {
            (str(t), str(g), str(p), str(gr), bool(o))
            for t, g, p, gr, o in await _rows(
                conn,
                "SELECT c.relname, COALESCE(g.rolname, 'PUBLIC'), a.privilege_type, "
                "gr.rolname, a.is_grantable FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace CROSS JOIN LATERAL "
                "aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a "
                "LEFT JOIN pg_roles g ON g.oid = a.grantee "
                "LEFT JOIN pg_roles gr ON gr.oid = a.grantor "
                "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','S','f')",
                (s,),
            )
        },
        "columns": {
            (str(t), str(col), str(g), str(p), str(gr), bool(o))
            for t, col, g, p, gr, o in await _rows(
                conn,
                "SELECT c.relname, a.attname, COALESCE(g.rolname, 'PUBLIC'), "
                "x.privilege_type, gr.rolname, x.is_grantable FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL aclexplode(a.attacl) x "
                "LEFT JOIN pg_roles g ON g.oid = x.grantee "
                "LEFT JOIN pg_roles gr ON gr.oid = x.grantor "
                "WHERE n.nspname = %s AND a.attacl IS NOT NULL",
                (s,),
            )
        },
        # W5 finding 2: every routine is inventoried on its own, independent of its
        # ACL (a routine whose ACL explodes to no rows still appears), by identity
        # (name, input argument types, kind, owner). Overloads are distinct entries.
        "routines": {
            (str(f), str(args), str(kind), str(ow))
            for f, args, kind, ow in await _rows(
                conn,
                "SELECT p.proname, pg_catalog.oidvectortypes(p.proargtypes), "
                "p.prokind::text, p.proowner::regrole::text FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s",
                (s,),
            )
        },
        # ACL tuples bound to the routine identity (name and input argument types).
        "functions": {
            (str(f), str(args), str(ow), str(g), str(p), str(gr), bool(o))
            for f, args, ow, g, p, gr, o in await _rows(
                conn,
                "SELECT p.proname, pg_catalog.oidvectortypes(p.proargtypes), "
                "p.proowner::regrole::text, COALESCE(g.rolname, 'PUBLIC'), "
                "a.privilege_type, gr.rolname, a.is_grantable FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace CROSS JOIN LATERAL "
                "aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a "
                "LEFT JOIN pg_roles g ON g.oid = a.grantee "
                "LEFT JOIN pg_roles gr ON gr.oid = a.grantor WHERE n.nspname = %s",
                (s,),
            )
        },
        "default_acl": {
            (str(r), str(t), str(g), str(p), str(gr), bool(o))
            for r, t, g, p, gr, o in await _rows(
                conn,
                "SELECT d.defaclrole::regrole::text, d.defaclobjtype::text, "
                "COALESCE(g.rolname, 'PUBLIC'), a.privilege_type, gr.rolname, "
                "a.is_grantable FROM pg_default_acl d "
                "JOIN pg_namespace n ON n.oid = d.defaclnamespace "
                "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
                "LEFT JOIN pg_roles g ON g.oid = a.grantee "
                "LEFT JOIN pg_roles gr ON gr.oid = a.grantor WHERE n.nspname = %s",
                (s,),
            )
        },
    }


_PHASES: Final = ("Φ0", "Φ1", "Φ2", "Φ3", "Φ4", "Φ5", "Φ6", "Φ7")


async def _phi_expected(conn: AsyncConnection[Any], phase: str, layout: _Layout) -> dict[str, Any]:
    """Architecture v6 r3 §4, Φ0-Φ7. Owner entries are owner-default (``acldefault``
    restricted to the owner's own tuple). The t003 entries come from the registry's
    installed-state profile of the t003 unit (its exact installed ACL)."""

    m, p, rt, pj = MIGRATOR_ROLE, PROVISIONER_ROLE, RUNTIME_ROLE, AUDIT_PROJECTOR_ROLE
    lo = layout.lock_owner
    assert lo is not None and phase in _PHASES
    owner_table = await _owner_default(conn, "r", m)
    schema = {(p, priv, p, False) for priv in await _owner_default(conn, "n", p)} | {
        (m, "USAGE", p, False),
        (m, "CREATE", p, False),
    }
    if phase in ("Φ0", "Φ1", "Φ6", "Φ7"):
        schema |= {(rt, "USAGE", p, False), (pj, "USAGE", p, False)}
    if phase in ("Φ3", "Φ4", "Φ5", "Φ6", "Φ7"):
        schema |= {(lo, "USAGE", p, False), (lo, "CREATE", p, False)}
    tables = {
        (t, m, priv, m, False) for t in (OUTBOX_TABLE, REGISTRY_TABLE) for priv in owner_table
    }
    if phase in ("Φ0", "Φ7"):
        tables |= {
            (OUTBOX_TABLE, rt, priv, m, False) for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        tables |= {(OUTBOX_TABLE, pj, "SELECT", m, False), (REGISTRY_TABLE, rt, "SELECT", m, False)}
    # The rejector is a trigger function with no input arguments; t003's identity
    # (name, input argument types, kind) comes from its installed-state profile.
    rej = (REJECTOR_FUNCTION, REJECTOR_ARGUMENT_TYPES)
    routines = {(*rej, REJECTOR_PROKIND, m)}
    functions = {(*rej, m, m, "EXECUTE", m, False)}
    if phase in ("Φ4", "Φ5", "Φ6", "Φ7"):
        if layout.t003_function is None or layout.t003_signature is None:
            raise _LayoutUnusable()
        fn = (layout.t003_function, layout.t003_signature[0])
        routines |= {(*fn, layout.t003_signature[1], lo)}
        functions |= {
            (*fn, lo, lo, priv, lo, False) for priv in await _owner_default(conn, "f", lo)
        }
        if phase in ("Φ4", "Φ7"):
            functions |= {
                (*fn, lo, grantee, priv, grantor, grantable)
                for grantee, priv, grantor, grantable in layout.t003_acl
                if grantee != lo
            }
    return {
        "schema_owner": p,
        "schema": schema,
        "relations": {(OUTBOX_TABLE, "r", m), (REGISTRY_TABLE, "r", m)},
        "tables": tables,
        "columns": {
            (REGISTRY_TABLE, "operation_id", lo, "SELECT", m, False),
            (REGISTRY_TABLE, "correlation_id", lo, "UPDATE", m, False),
        },
        "routines": routines,
        "functions": functions,
        "default_acl": {
            (m, "r", rt, priv, m, False) for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        | {(m, "S", rt, priv, m, False) for priv in ("USAGE", "SELECT")},
    }


class _LayoutUnusable(Exception):
    """The registry lacks a fact the classifier needs (e.g. the t003 profile)."""


async def _phi_matches(
    conn: AsyncConnection[Any], schema_key: str, phase: str, layout: _Layout
) -> bool:
    actual = await _phi_actual(conn, schema_key)
    expected = await _phi_expected(conn, phase, layout)
    return all(actual[key] == expected[key] for key in expected)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """What the controller learns from a classification, beyond the state."""

    classification: Classification
    schema_key: str | None = None
    operation_id: UUID | None = None
    t003_baseline: str | None = None
    lifecycle_state: str | None = None
    schema_version: int | None = None
    operation_state: str | None = None
    t003_ledger: tuple[str, int] | None = None


def _x(reason: str, generation: int | None = None) -> _Snapshot:
    return _Snapshot(Classification(KState.X, generation, reason))


# Milestone events after neutralization, in protocol order (§7), with the state
# each one, as the latest present milestone, implies (K5 to K6 are refined by the
# t003 ledger and its markers).
_ENTERED_MILESTONES: Final = (
    ("drained", KState.K3d),
    ("exclusion_established", KState.K4),
    ("lo_granted", KState.K5),
    ("apply_applied", KState.K7),
    ("l2b_withheld", KState.K7b),
    ("verified", KState.K8),
)
# W5 finding 1 (§5, §8 "any other combination is X"): the events an operation may
# carry in each operation state. An event outside the state's set is X, whatever the
# milestone or layer reading says; e.g. a premature `completed`, `released`,
# `abandoned`, `activated` or restoration event never classifies as a working state.
# Historical N generations stay legal (N events are keyed by generation, not refused).
_COMMON_EVENTS: Final = frozenset(
    {"op_created", "claimed", "attempt_refused", "interference_detected", "reconciled_by_owner"}
)
_ESTABLISHING_EVENTS: Final = _COMMON_EVENTS | {"l2_withheld"}
_N_EVENTS: Final = frozenset(
    {
        "neutralization_bootstrap_started",
        "neutralization_started",
        "neutralization_round_started",
        "neutralization_round_outcome",
        "neutralized",
        "neutralization_restarted",
        "neutralization_stopped",
    }
)
_APPLY_EVENTS: Final = frozenset(
    {"apply_running", "apply_applied", "apply_failed", "apply_outcome_unrecorded"}
)
_ENTERED_EVENTS: Final = (
    _ESTABLISHING_EVENTS
    | {"l1_withheld", "drained", "exclusion_established", "lo_granted", "l2b_withheld", "verified"}
    | _N_EVENTS
    | _APPLY_EVENTS
)
_ACTIVATED_EVENTS: Final = _ENTERED_EVENTS | {"activated", "l1_restored", "l2_l2b_restored"}
_RELEASED_EVENTS: Final = _ACTIVATED_EVENTS | {"released", "completed"}
_ALLOWED_EVENTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "establishing": _ESTABLISHING_EVENTS,
        "entered": _ENTERED_EVENTS,
        "activated": _ACTIVATED_EVENTS,
        "released": _RELEASED_EVENTS,
    }
)
_PHI_OF: Final[Mapping[KState, str]] = MappingProxyType(
    {
        KState.K0: "Φ0",
        KState.K1: "Φ0",
        KState.K2: "Φ1",
        KState.K3: "Φ2",
        KState.K3b: "Φ2",
        KState.K3n: "Φ2",
        KState.K3x: "Φ2",
        KState.K3z: "Φ2",
        KState.K3d: "Φ2",
        KState.K4: "Φ2",
        KState.K5: "Φ3",
        KState.K5f: "Φ3",
        KState.K6: "Φ3",
        KState.K7: "Φ4",
        KState.K7b: "Φ5",
        KState.K8: "Φ5",
        KState.K9: "Φ5",
        KState.K10: "Φ6",
        KState.K10b: "Φ7",
        KState.K11: "Φ7",
        KState.K12: "Φ7",
    }
)
_OP_COLUMNS: Final = (
    "maintenance_operation_id, tenant_id, from_version, to_version, state, pre_entry_event_id, "
    "entry_event_id, resume_event_id, t003_baseline, neutralization_generation"
)


async def _classify(
    conn: AsyncConnection[Any],
    tenant_id: str,
    operation_id: UUID | None,
    layout: _Layout,
    target_version: int,
) -> _Snapshot:
    # G1
    if target_version != TO_VERSION:
        return _x("g1_target")
    if layout.lock_owner is None:
        return _x("layout")
    tenant = await _rows(
        conn,
        "SELECT schema_key, lifecycle_state, schema_version FROM shared.tenants "
        "WHERE tenant_id = %s",
        (tenant_id,),
    )
    if len(tenant) != 1:
        return _x("tenant_absent")
    schema_key, lifecycle, version = str(tenant[0][0]), str(tenant[0][1]), int(tenant[0][2])
    ledger = {
        str(m): (str(c), str(s), int(a))
        for m, c, s, a in await _rows(
            conn,
            "SELECT migration_id, checksum, state, attempt FROM shared.schema_migrations "
            "WHERE tenant_id = %s",
            (tenant_id,),
        )
    }
    events = [
        (UUID(str(o)) if o is not None else None, str(e), d)
        for o, e, d in await _rows(
            conn,
            f"SELECT maintenance_operation_id, event, detail FROM {ATTEMPTS} "
            "WHERE tenant_id = %s ORDER BY occurred_at, event_id",
            (tenant_id,),
        )
    ]
    open_ops = await _rows(
        conn,
        f"SELECT {_OP_COLUMNS} FROM {OPERATIONS} "
        "WHERE tenant_id = %s AND state NOT IN ('released', 'abandoned')",
        (tenant_id,),
    )
    named = None
    if operation_id is not None:
        named_rows = await _rows(
            conn,
            f"SELECT {_OP_COLUMNS} FROM {OPERATIONS} WHERE maintenance_operation_id = %s",
            (operation_id,),
        )
        named = named_rows[0] if named_rows else None
    history = await _rows(
        conn,
        "SELECT event_id, new_state, reason_code, execution_id FROM shared.tenant_state_history "
        "WHERE tenant_id = %s ORDER BY event_id DESC LIMIT 1",
        (tenant_id,),
    )

    # G2: t001/t002 applied at prod; every t003 row, in any state, at prod.
    for migration_id in layout.baseline_ids:
        recorded = ledger.get(migration_id)
        if recorded is None or recorded[1] != "applied":
            return _x("g2_baseline")
        if recorded[0] != layout.checksums[migration_id]:
            return _x("g2_checksum")
    t003 = ledger.get(layout.t003_id)
    if t003 is not None and t003[0] != layout.checksums[layout.t003_id]:
        return _x("g2_t003_checksum")
    if set(ledger) - set(layout.baseline_ids) - {layout.t003_id}:
        return _x("ledger_unknown_unit")
    t003_ledger = None if t003 is None else (t003[1], t003[2])

    if operation_id is None:
        if open_ops:
            return _x("g3_operation")
        # K0. Interference evidence not bound to any operation also blocks it.
        unbound = [e for o, e, _d in events if o is None]
        if (
            "interference_detected" in unbound
            and "reconciled_by_owner"
            not in unbound[max(i for i, e in enumerate(unbound) if e == "interference_detected") :]
        ):
            return _x("interference")
        if lifecycle != "active" or version != FROM_VERSION:
            return _x("k0_registry")
        if t003 is not None and t003[1] != "failed":
            return _x("g7_ledger")
        if not await _phi_matches(conn, schema_key, "Φ0", layout):
            return _x("phi")
        return _Snapshot(
            Classification(KState.K0, None, None),
            schema_key=schema_key,
            t003_baseline="absent" if t003 is None else f"failed:{t003[2]}",
            lifecycle_state=lifecycle,
            schema_version=version,
            t003_ledger=t003_ledger,
        )

    # G3: the named operation is this tenant's, 2 -> 3, and no other is non-final.
    if named is None:
        return _x("g3_operation")
    op_id = UUID(str(named[0]))
    if str(named[1]) != tenant_id or int(named[2]) != FROM_VERSION or int(named[3]) != TO_VERSION:
        return _x("g3_identity")
    state = str(named[4])
    if state == "abandoned":
        return _x("g3_closed")
    if any(UUID(str(o[0])) != op_id for o in open_ops):
        return _x("g3_several")
    pre_entry, entry, resume = int(named[5]), named[6], named[7]
    baseline, generation = str(named[8]), int(named[9])
    op_events = [(e, d) for o, e, d in events if o == op_id]
    names = [e for e, _d in op_events]

    def _last(event: str) -> int:
        return max((i for i, e in enumerate(names) if e == event), default=-1)

    def _last_first(event: str) -> int:
        return min((i for i, e in enumerate(names) if e == event), default=-1)

    if _last("interference_detected") >= 0 and _last("reconciled_by_owner") < _last(
        "interference_detected"
    ):
        return _x("interference", generation)

    def _gen(event: str) -> list[int]:
        found = []
        for e, d in op_events:
            if e == event and isinstance(d, dict) and type(d.get("gen")) is int:
                found.append(int(d["gen"]))
        return found

    def _marker(event: str, n: int) -> bool:
        return any(
            e == event and isinstance(d, dict) and d.get("ledger_attempt") == n
            for e, d in op_events
        )

    def _g7() -> bool:  # K1 to K5: the baseline, or failed(n) with apply_failed{n}
        if t003 is None:
            return baseline == "absent"
        if t003[1] != "failed":
            return False
        return baseline == f"failed:{t003[2]}" or _marker("apply_failed", t003[2])

    if not history:
        return _x("history", generation)
    latest_id, latest_state, latest_reason, latest_execution = history[0]

    def _history_is(event_id: object, new_state: str, reason: str) -> bool:
        return (
            event_id is not None
            and int(latest_id) == int(str(event_id))
            and str(latest_execution) == str(op_id)
            and str(latest_state) == new_state
            and str(latest_reason) == reason
        )

    common: dict[str, Any] = {
        "schema_key": schema_key,
        "operation_id": op_id,
        "t003_baseline": baseline,
        "lifecycle_state": lifecycle,
        "schema_version": version,
        "operation_state": state,
        "t003_ledger": t003_ledger,
    }

    async def _done(k: KState) -> _Snapshot:
        if not await _phi_matches(conn, schema_key, _PHI_OF[k], layout):
            return _x("phi", generation)
        return _Snapshot(Classification(k, generation, None), **common)

    neutral_events = (
        "neutralization_bootstrap_started",
        "neutralization_started",
        "neutralization_stopped",
        "neutralized",
    )
    milestone_names = [m for m, _k in _ENTERED_MILESTONES]

    if state == "establishing":
        if lifecycle != "active" or version != FROM_VERSION:
            return _x("registry", generation)
        if int(latest_id) != pre_entry:  # G4
            return _x("g4", generation)
        if not _g7():
            return _x("g7", generation)
        if any(e in names for e in ("l1_withheld", *neutral_events, *milestone_names)):
            return _x("layers", generation)
        if set(names) - _ALLOWED_EVENTS["establishing"]:
            return _x("events", generation)
        return await _done(KState.K2 if "l2_withheld" in names else KState.K1)

    # From here on every state needs both establishment layers.
    if "l1_withheld" not in names or "l2_withheld" not in names:
        return _x("layers", generation)
    if _gen("neutralized") and _gen("neutralized") != [generation]:
        return _x("neutralized_generation", generation)
    present = [m in names for m in milestone_names]
    reached = max((i for i, p in enumerate(present) if p), default=-1)
    if not all(present[: reached + 1]):  # a later milestone without an earlier one
        return _x("milestones", generation)
    neutralized = generation in _gen("neutralized")
    if reached >= 0 and not neutralized:
        return _x("milestones", generation)

    if state == "entered":
        if lifecycle != "suspended" or version != FROM_VERSION:
            return _x("registry", generation)
        if not _history_is(entry, "suspended", ENTRY_REASON):  # G5
            return _x("g5", generation)
        if any(e in names for e in ("activated", "l1_restored", "l2_l2b_restored", "released")):
            return _x("layers", generation)
        if set(names) - _ALLOWED_EVENTS["entered"]:
            return _x("events", generation)
        # Ledger-step evidence exists only after LO (§7 A2r: K5 or K6 precondition).
        applies = [i for i, e in enumerate(names) if e in _APPLY_EVENTS]
        if applies and not (0 <= _last_first("lo_granted") < min(applies)):
            return _x("events", generation)
        if not neutralized:
            if generation in _gen("neutralization_stopped"):
                k = KState.K3x
            elif generation in _gen("neutralization_started"):
                k = KState.K3n
            elif generation in _gen("neutralization_bootstrap_started"):
                k = KState.K3b
            else:
                k = KState.K3
            return await _done(k) if _g7() else _x("g7", generation)
        if reached < 0:
            return await _done(KState.K3z) if _g7() else _x("g7", generation)
        k = _ENTERED_MILESTONES[reached][1]
        if k in (KState.K3d, KState.K4):
            return await _done(k) if _g7() else _x("g7", generation)
        if k == KState.K5:
            if t003 is not None and t003[1] == "running":
                if not _marker("apply_running", t003[2]):
                    return _x("unauthorized_running", generation)
                return await _done(KState.K6)
            if t003 is not None and t003[1] == "failed" and _marker("apply_failed", t003[2]):
                return await _done(KState.K5f)
            return await _done(KState.K5) if _g7() else _x("g7", generation)
        if t003 is None or t003[1] != "applied":  # K7, K7b, K8
            return _x("ledger", generation)
        return await _done(k)

    # activated or released: K9 to K12.
    if lifecycle != "active" or version != TO_VERSION:
        return _x("registry", generation)
    if reached != len(milestone_names) - 1 or "activated" not in names:
        return _x("milestones", generation)
    if t003 is None or t003[1] != "applied":
        return _x("ledger", generation)
    # G6: resume history, and a `verified` after the last apply_applied and
    # l2b_withheld and before activated.
    if not _history_is(resume, "active", RESUME_REASON):
        return _x("g6", generation)
    verified, activated = _last("verified"), _last("activated")
    if not (max(_last("apply_applied"), _last("l2b_withheld")) < verified < activated):
        return _x("g6", generation)
    restored_l1, restored_l2 = "l1_restored" in names, "l2_l2b_restored" in names
    allowed = _ALLOWED_EVENTS.get(state)
    if allowed is None or set(names) - allowed:
        return _x("events", generation)
    if state == "activated":
        if "released" in names or "completed" in names:
            return _x("layers", generation)
        if restored_l2:
            return await _done(KState.K10b) if restored_l1 else _x("layers", generation)
        return await _done(KState.K10 if restored_l1 else KState.K9)
    if state == "released":
        if not (restored_l1 and restored_l2 and "released" in names):
            return _x("layers", generation)
        return await _done(KState.K12 if "completed" in names else KState.K11)
    return _x("operation_state", generation)


async def _classify_snapshot(
    conn: AsyncConnection[Any],
    tenant_id: str,
    operation_id: UUID | None,
    registry: TenantMigrationRegistry,
) -> _Snapshot:
    try:
        layout = _layout(registry)
        target = registry.target_version
    except Exception:  # an unusable registry is not a readable classification
        return _Snapshot(Classification(KState.UNKNOWN, None, "registry"))
    if layout.lock_owner is None:
        return _Snapshot(Classification(KState.X, None, "layout"))
    try:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return await _classify(conn, tenant_id, operation_id, layout, target)
    except _LayoutUnusable:
        return _Snapshot(Classification(KState.X, None, "layout"))
    except PsycopgError:
        return _Snapshot(Classification(KState.UNKNOWN, None, "unreadable"))


async def classify(
    conn: AsyncConnection[Any],
    tenant_id: str,
    operation_id: UUID | None,
    *,
    registry: TenantMigrationRegistry,
) -> Classification:
    """Read-only classification from committed state (§8). Unknown when unreadable."""

    return (await _classify_snapshot(conn, tenant_id, operation_id, registry)).classification


async def _classify_in(
    conn: AsyncConnection[Any],
    tenant_id: str,
    operation_id: UUID | None,
    registry: TenantMigrationRegistry,
) -> _Snapshot:
    """The classifier inside the caller's open transaction (the claim's)."""

    try:
        layout = _layout(registry)
        target = registry.target_version
    except Exception:
        return _Snapshot(Classification(KState.UNKNOWN, None, "registry"))
    try:
        return await _classify(conn, tenant_id, operation_id, layout, target)
    except _LayoutUnusable:
        return _Snapshot(Classification(KState.X, None, "layout"))
    except PsycopgError:  # the caller's transaction is aborted; it refuses and rolls back
        return _Snapshot(Classification(KState.UNKNOWN, None, "unreadable"))


_CL_STATES: Final = frozenset(
    {
        KState.K1,
        KState.K2,
        KState.K3,
        KState.K3z,
        KState.K3d,
        KState.K4,
        KState.K5,
        KState.K5f,
        KState.K6,
        KState.K7,
        KState.K7b,
        KState.K8,
        KState.K9,
        KState.K10,
        KState.K10b,
    }
)


# --- the controller -------------------------------------------------------------

_DENIAL: Final[Mapping[KState, str]] = MappingProxyType(
    {
        KState.K0: "served",
        KState.K1: "served",
        KState.K2: "establishment_incomplete",
        KState.K3: "establishment_incomplete",
    }
)
_IMPLEMENTED_STEPS: Final = frozenset({"C1", "CL", "CLc", "C2", "C3", "AB"})
_FENCED_STEPS: Final = frozenset({"C2", "C3", "AB"})


def _sanitized(error: PsycopgError) -> Exception:
    sqlstate = getattr(error, "sqlstate", None) or "unknown"
    return RuntimeError(f"database error, sqlstate {sqlstate}")


@dataclass
class _Attempt:
    request: UpgradeRequest
    attempt_id: UUID
    snapshot: _Snapshot
    operation_id: UUID | None = None
    capability: MaintenanceCapability | None = None
    k: KHandle | None = None
    fault_applied: bool = False
    # W5 finding 3 (IP-14 r3 A3): the operation this attempt has ESTABLISHED an
    # authoritative match with: set only from the row a committed claim, or a
    # passing fence (statement 2 row + binding + statement 3), returned. Never copied
    # from the request or a capability.
    matched_operation: UUID | None = None


class TenantSchemaUpgrade:
    """The L-6 controller. Constructed only by ``compose_tenant_upgrade``."""

    def __init__(
        self,
        *,
        provisioner_connect: ConnectionFactory,
        migrator_connect: ConnectionFactory,
        lock_timeout_seconds: float,
        registry: TenantMigrationRegistry,
        hooks: UpgradeTestHooks | None,
    ) -> None:
        self._p = provisioner_connect
        self._m = migrator_connect
        self._timeout = lock_timeout_seconds
        self._registry = registry
        self._hooks = hooks if hooks is not None else _NO_HOOKS

    # -- public entry points ------------------------------------------------------

    async def run(self, request: UpgradeRequest) -> UpgradeOutcome:
        """S0-S3, then the CP-3 steps; refuses after one ``attempt_refused`` append,
        stops on interference, or stops at the CP-3 boundary."""

        attempt = await self._begin(request)
        try:
            return await self._run(attempt)
        except MaintenanceRefused as refused:
            raise await self._record_refusal(attempt, refused)  # noqa: B904 - cause kept
        except _Interference as interference:
            await self._record_interference(attempt, interference)
            raise MaintenanceInterferenceStopped(
                interference.point, interference.kind, interference.phase, interference.observed
            ) from None

    async def abandon(
        self, request: UpgradeRequest, authorization: OwnerAuthorization | None
    ) -> UpgradeOutcome:
        """AB at K1 only, with Rachel's recorded authorization (Q6)."""

        attempt = await self._begin(request)
        try:
            return await self._abandon(attempt, authorization)
        except MaintenanceRefused as refused:
            raise await self._record_refusal(attempt, refused)  # noqa: B904 - cause kept

    # -- S0, S1 -------------------------------------------------------------------

    async def _begin(self, request: UpgradeRequest) -> _Attempt:
        """S0 (read-only classification), then S1 (``started``). Q-P9 (a): S1 runs even
        when S0's result will be refused. An S1 failure stops the attempt under the
        evidence-failure rule (IP-14 A6, Q-P10): RC-22 with the driver error as
        ``__cause__``, no refusal append, no retry and no protected step. Its phase is
        ``classify``, the phase of the step that precedes it (an open point recorded
        in the packet README; the 005 vocabulary has no S1 phase)."""

        async with _session(self._p, PROVISIONER_ROLE) as conn:
            snapshot = await _classify_snapshot(
                conn, request.tenant_id, request.operation_id, self._registry
            )
        attempt = _Attempt(request, uuid4(), snapshot)
        try:
            async with _session(self._p, PROVISIONER_ROLE) as conn:
                await _insert_event(
                    conn,
                    attempt_id=attempt.attempt_id,
                    operation_id=None,
                    tenant_id=request.tenant_id,
                    event="started",
                    detail={},
                )
        except PsycopgError as error:
            failed = _refused(MaintenanceCode.MAINTENANCE_EVIDENCE_WRITE_FAILED.value, "classify")
            raise failed from error
        return attempt

    @staticmethod
    def _require_classified(snapshot: _Snapshot) -> None:
        state = snapshot.classification.state
        if state == KState.UNKNOWN:
            raise _refused(MaintenanceCode.MAINTENANCE_STATE_UNKNOWN.value, "classify")
        if state == KState.X:
            raise _refused(MaintenanceCode.MAINTENANCE_CASE_REFUSED.value, "classify")

    async def _pause(self, step: str) -> None:
        pause = self._hooks.pause_after.get(step)
        if pause is not None:
            await pause()

    async def _reclassify(self, attempt: _Attempt) -> None:
        """S3: the state is unchanged since S0, now under K."""

        async with _session(self._p, PROVISIONER_ROLE) as conn:
            snapshot = await _classify_snapshot(
                conn, attempt.request.tenant_id, attempt.request.operation_id, self._registry
            )
        self._require_classified(snapshot)
        if snapshot.classification.state != attempt.snapshot.classification.state:
            raise _refused(MaintenanceCode.MAINTENANCE_CASE_REFUSED.value, "classify")
        attempt.snapshot = snapshot

    # -- run ------------------------------------------------------------------------

    async def _run(self, attempt: _Attempt) -> UpgradeOutcome:
        self._require_classified(attempt.snapshot)
        request = attempt.request
        async with _hold_k(
            self._m, request.tenant_id, self._timeout, self._hooks.k_session_fault
        ) as k:
            attempt.k = k
            await self._pause("S2")
            await self._reclassify(attempt)
            await self._pause("S3")
            state = attempt.snapshot.classification.state
            steps = list(next_steps(attempt.snapshot.classification, live=False))
            if state == KState.K0:
                attempt.operation_id = uuid4()
            else:
                attempt.operation_id = attempt.snapshot.operation_id
            claim_form: Literal["CL", "CLc"] | None = None
            while steps:
                step = steps.pop(0)
                if claim_form is not None:
                    require_step_allowed(claim_form, step)
                if step not in _IMPLEMENTED_STEPS:
                    raise CheckpointBoundaryReached(step)
                if step in ("CL", "CLc"):
                    claim_form = "CL" if step == "CL" else "CLc"
                    await self._claim(attempt, claim_form)
                    continue
                await self._step(attempt, step)
                after = {"C1": KState.K1, "C2": KState.K2, "C3": KState.K3}[step]
                if not steps:
                    steps = list(_LIVE.get(after, ()))
                    claim_form = None
        raise CheckpointBoundaryReached("none")

    # -- abandon ----------------------------------------------------------------

    async def _abandon(
        self, attempt: _Attempt, authorization: OwnerAuthorization | None
    ) -> UpgradeOutcome:
        self._require_classified(attempt.snapshot)
        request = attempt.request
        invalid = _refused(MaintenanceCode.MAINTENANCE_TOKEN_INVALID.value, "capability")
        if (
            not isinstance(authorization, OwnerAuthorization)
            or not isinstance(authorization.decision_sha256, str)
            or not _DECISION_SHA256.fullmatch(authorization.decision_sha256)
            or authorization.tenant_id != request.tenant_id
            or request.operation_id is None
            or authorization.operation_id != request.operation_id
            or authorization.action != "abandon"
        ):
            raise invalid
        if attempt.snapshot.classification.state != KState.K1:
            raise _refused(MaintenanceCode.MAINTENANCE_CLAIM_REFUSED.value, "claim")
        async with _hold_k(
            self._m, request.tenant_id, self._timeout, self._hooks.k_session_fault
        ) as k:
            attempt.k = k
            await self._reclassify(attempt)
            attempt.operation_id = attempt.snapshot.operation_id
            await self._claim(attempt, "CL")
            await self._step(attempt, "AB")
        return UpgradeOutcome(attempt.attempt_id, attempt.operation_id, KState.K0)

    # -- the capability ---------------------------------------------------------

    def _mint(self, attempt: _Attempt) -> MaintenanceCapability:
        if attempt.capability is None:
            k = attempt.k
            assert k is not None and attempt.operation_id is not None
            attempt.capability = MaintenanceCapability(
                tenant_id=attempt.request.tenant_id,
                operation_id=attempt.operation_id,
                attempt_id=attempt.attempt_id,
                key=k.key,
                k_pid=k.pid,
                database=k.database,
            )
        return attempt.capability

    def _capability_for(self, attempt: _Attempt, step: str) -> Any:
        cap = self._mint(attempt)
        fault = self._hooks.capability_fault
        if fault is None or attempt.fault_applied or step not in _FENCED_STEPS:
            return cap
        attempt.fault_applied = True
        if fault == "missing_attempt":
            return replace(cap, attempt_id=None)  # type: ignore[arg-type]
        if fault == "tenant":
            other = f"{cap.tenant_id[:60]}-zz"
            return replace(cap, tenant_id=other, key=tenant_lock_key(other))
        if fault == "k_pid":
            return replace(cap, k_pid=cap.k_pid + 1)
        return replace(cap, database=f"{cap.database}_not_this_one")

    # -- steps ------------------------------------------------------------------

    async def _claim(self, attempt: _Attempt, form: Literal["CL", "CLc"]) -> None:
        k = attempt.k
        assert k is not None and attempt.operation_id is not None
        async with _session(self._p, PROVISIONER_ROLE) as conn, conn.transaction():
            matched = await claim(
                conn,
                form=form,
                tenant_id=attempt.request.tenant_id,
                operation_id=attempt.operation_id,
                new_attempt_id=attempt.attempt_id,
                k=k,
                registry=self._registry,
            )
        attempt.matched_operation = matched  # only after the claim committed
        await self._pause(form)  # W5-3 seam: after the claim committed, before any fence

    async def _step(self, attempt: _Attempt, step: str) -> None:
        k = attempt.k
        assert k is not None
        cap = self._capability_for(attempt, step)
        validate_capability(cap)  # before any connection of the step
        fault = self._hooks.step_fault.get(step)
        role = MIGRATOR_ROLE if step == "C2" else PROVISIONER_ROLE
        connect = self._m if step == "C2" else self._p
        body = {"C1": self._c1, "C2": self._c2, "C3": self._c3, "AB": self._ab}[step]
        async with _session(connect, role) as conn:
            async with conn.transaction():
                await body(conn, attempt, cap, k)
                if fault == "crash":
                    raise InjectedFault(f"step_fault: {step} crash")
            if fault == "lost_ack":
                raise psycopg.OperationalError(f"injected: {step} acknowledgement lost")
        await self._pause(step)

    @staticmethod
    async def _fenced(
        conn: AsyncConnection[Any], attempt: _Attempt, cap: MaintenanceCapability, k: KHandle
    ) -> OperationRow:
        """F, then record the authoritative match it established (IP-14 r3 A3): the
        statement-2 row matched the capability's tenant and operation, the binding
        check passed and statement 3 held. Recorded from the returned row only; the
        match stands even if the step's transaction later rolls back."""

        operation = await fence(conn, cap, k)
        if operation.operation_id == cap.operation_id and operation.tenant_id == cap.tenant_id:
            attempt.matched_operation = operation.operation_id
        return operation

    async def _c1(
        self, conn: AsyncConnection[Any], attempt: _Attempt, cap: MaintenanceCapability, k: KHandle
    ) -> None:
        """K0 → K1. Creates the row; runs statements 3-4 of F (§7 C1)."""

        await _set_lock_timeout(conn, k.lock_timeout_seconds)
        if cap.k_pid != k.pid or cap.key != k.key or cap.tenant_id != attempt.request.tenant_id:
            raise _refused(MaintenanceCode.MAINTENANCE_TOKEN_INVALID.value, "fence")
        database = await (await conn.execute("SELECT current_database()")).fetchone()
        if database is None or str(database[0]) != cap.database:
            raise _refused(MaintenanceCode.MAINTENANCE_TOKEN_INVALID.value, "fence")
        if not await _statement_3(conn, k.key, k.pid):
            raise _refused(MaintenanceCode.MAINTENANCE_LOCK_LOST.value, "fence")
        latest = await (
            await conn.execute(
                "SELECT max(event_id) FROM shared.tenant_state_history WHERE tenant_id = %s",
                (cap.tenant_id,),
            )
        ).fetchone()
        if latest is None or latest[0] is None:
            raise _refused(MaintenanceCode.MAINTENANCE_CASE_REFUSED.value, "establish")
        await conn.execute(
            f"""
            INSERT INTO {OPERATIONS}
                (maintenance_operation_id, tenant_id, from_version, to_version, state,
                 pre_entry_event_id, t003_baseline, current_attempt)
            VALUES (%s, %s, %s, %s, 'establishing', %s, %s, %s)
            """,
            (
                cap.operation_id,
                cap.tenant_id,
                FROM_VERSION,
                TO_VERSION,
                int(latest[0]),
                attempt.snapshot.t003_baseline,
                cap.attempt_id,
            ),
        )
        await _insert_event(
            conn,
            attempt_id=cap.attempt_id,
            operation_id=cap.operation_id,
            tenant_id=cap.tenant_id,
            event="op_created",
            detail={},
        )

    async def _withheld_rows(
        self,
        conn: AsyncConnection[Any],
        operation_id: UUID,
        layer: str,
        tuples: list[tuple[str, str, str, str | None, str, str, bool, str]],
    ) -> None:
        for kind, schema, obj, column, grantee, privilege, grantable, grantor in tuples:
            await conn.execute(
                f"""
                INSERT INTO {WITHHELD}
                    (maintenance_operation_id, layer, object_kind, schema_name, object_name,
                     column_name, grantee, privilege, grantable, grantor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    operation_id,
                    layer,
                    kind,
                    schema,
                    obj,
                    column,
                    grantee,
                    privilege,
                    grantable,
                    grantor,
                ),
            )

    async def _c2(
        self, conn: AsyncConnection[Any], attempt: _Attempt, cap: MaintenanceCapability, k: KHandle
    ) -> None:
        """K1 → K2 (M): withhold the class O table tuples (L2)."""

        await self._fenced(conn, attempt, cap, k)
        if await _event_exists(conn, cap.operation_id, "l2_withheld"):
            raise _refused(MaintenanceCode.MAINTENANCE_CASE_REFUSED.value, "establish")
        schema_key = attempt.snapshot.schema_key
        assert schema_key is not None
        class_o = (RUNTIME_ROLE, AUDIT_PROJECTOR_ROLE)
        rows = await _rows(
            conn,
            "SELECT c.relname, g.rolname, a.privilege_type, a.is_grantable, gr.rolname "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) a "
            "JOIN pg_roles g ON g.oid = a.grantee JOIN pg_roles gr ON gr.oid = a.grantor "
            "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','S','f') "
            "AND g.rolname = ANY(%s) ORDER BY 1, 2, 3",
            (schema_key, list(class_o)),
        )
        tuples: list[tuple[str, str, str, str | None, str, str, bool, str]] = []
        for table, grantee, privilege, grantable, grantor in rows:
            if str(grantor) != MIGRATOR_ROLE:
                raise _refused(MaintenanceCode.MAINTENANCE_CASE_REFUSED.value, "establish")
            await conn.execute(
                sql.SQL("REVOKE {} ON TABLE {}.{} FROM {}").format(
                    sql.SQL(str(privilege)),
                    sql.Identifier(schema_key),
                    sql.Identifier(str(table)),
                    sql.Identifier(str(grantee)),
                )
            )
            tuples.append(
                (
                    "table",
                    schema_key,
                    str(table),
                    None,
                    str(grantee),
                    str(privilege),
                    bool(grantable),
                    MIGRATOR_ROLE,
                )
            )
        await self._withheld_rows(conn, cap.operation_id, "L2", tuples)
        await _insert_event(
            conn,
            attempt_id=cap.attempt_id,
            operation_id=cap.operation_id,
            tenant_id=cap.tenant_id,
            event="l2_withheld",
            detail={"tuple_count": len(tuples)},
        )

    async def _c3(
        self, conn: AsyncConnection[Any], attempt: _Attempt, cap: MaintenanceCapability, k: KHandle
    ) -> None:
        """K2 → K3 (P): suspend, history entry, withhold class O schema USAGE (L1)."""

        operation = await self._fenced(conn, attempt, cap, k)
        registry = await (
            await conn.execute(
                "SELECT schema_key, lifecycle_state, schema_version FROM shared.tenants "
                "WHERE tenant_id = %s FOR UPDATE",
                (cap.tenant_id,),
            )
        ).fetchone()
        schema_key = attempt.snapshot.schema_key
        assert registry is not None and schema_key is not None
        layout = _layout(self._registry)
        assert layout.lock_owner is not None
        t003_rows = await _rows(
            conn,
            "SELECT state, attempt FROM shared.schema_migrations "
            "WHERE tenant_id = %s AND migration_id = %s",
            (cap.tenant_id, layout.t003_id),
        )
        t003 = (str(t003_rows[0][0]), int(t003_rows[0][1])) if t003_rows else None
        baseline = attempt.snapshot.t003_baseline
        # T-2 at point b (addendum 4 r3): registry, ACL and ledger as recorded.
        kind: str | None = None
        if str(registry[1]) != "active" or int(registry[2]) != FROM_VERSION:
            kind = "registry"
        elif not await _phi_matches(conn, schema_key, "Φ1", layout):
            kind = "acl"
        elif (t003 is None and baseline != "absent") or (
            t003 is not None and (t003[0] != "failed" or baseline != f"failed:{t003[1]}")
        ):
            kind = "ledger"
        if kind is not None:
            observed = InterferenceObservation(
                lifecycle_state=str(registry[1]),
                schema_version=int(registry[2]),
                operation_state=operation.state,
                t003_state=None if t003 is None else t003[0],
                t003_attempt=None if t003 is None else t003[1],
                denial_status=_DENIAL[KState.K2],
            )
            raise _Interference("b", kind, "C3", observed)
        await conn.execute(
            "UPDATE shared.tenants SET lifecycle_state = 'suspended', "
            "updated_at = statement_timestamp() WHERE tenant_id = %s",
            (cap.tenant_id,),
        )
        history = await (
            await conn.execute(
                """
                INSERT INTO shared.tenant_state_history
                    (tenant_id, prior_state, new_state, reason_code,
                     actor_kind, actor_id, execution_id)
                VALUES (%s, 'active', 'suspended', %s, %s, %s, %s)
                RETURNING event_id
                """,
                (
                    cap.tenant_id,
                    ENTRY_REASON,
                    UPGRADE_ACTOR_KIND,
                    UPGRADE_ACTOR_ID,
                    str(cap.operation_id),
                ),
            )
        ).fetchone()
        assert history is not None
        tuples: list[tuple[str, str, str, str | None, str, str, bool, str]] = []
        for grantee in (RUNTIME_ROLE, AUDIT_PROJECTOR_ROLE):
            await conn.execute(
                sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(
                    sql.Identifier(schema_key), sql.Identifier(grantee)
                )
            )
            tuples.append(
                ("schema", schema_key, schema_key, None, grantee, "USAGE", False, PROVISIONER_ROLE)
            )
        await self._withheld_rows(conn, cap.operation_id, "L1", tuples)
        await conn.execute(
            f"UPDATE {OPERATIONS} SET state = 'entered', entry_event_id = %s, "
            "updated_at = statement_timestamp() WHERE maintenance_operation_id = %s",
            (int(history[0]), cap.operation_id),
        )
        await _insert_event(
            conn,
            attempt_id=cap.attempt_id,
            operation_id=cap.operation_id,
            tenant_id=cap.tenant_id,
            event="l1_withheld",
            detail={"tuple_count": len(tuples)},
        )

    async def _ab(
        self, conn: AsyncConnection[Any], attempt: _Attempt, cap: MaintenanceCapability, k: KHandle
    ) -> None:
        """K1 → K0 (P): close the operation (nothing was withheld)."""

        operation = await self._fenced(conn, attempt, cap, k)
        if operation.state != "establishing" or await _event_exists(
            conn, cap.operation_id, "l2_withheld"
        ):
            raise _refused(MaintenanceCode.MAINTENANCE_CLAIM_REFUSED.value, "claim")
        await conn.execute(
            f"UPDATE {OPERATIONS} SET state = 'abandoned', updated_at = statement_timestamp() "
            "WHERE maintenance_operation_id = %s",
            (cap.operation_id,),
        )
        await _insert_event(
            conn,
            attempt_id=cap.attempt_id,
            operation_id=cap.operation_id,
            tenant_id=cap.tenant_id,
            event="abandoned",
            detail={},
        )

    # -- evidence (IP-14) ---------------------------------------------------------

    async def _record_refusal(
        self, attempt: _Attempt, refused: MaintenanceRefused
    ) -> MaintenanceRefused:
        """Append one ``attempt_refused`` on a separate autocommit P connection; never
        retried. Returns the error the caller raises."""

        probe = self._hooks.appender_probe
        if probe is not None:
            probe.on_invocation()
        detail = {"code": refused.reason_code, "phase": refused.phase}
        # IP-14 r3 A3 (with the proposed A3 clarification, W5 R-b): associate the
        # operation only if this attempt ESTABLISHED a match: the refusing fence's own
        # statement-2 match (statement-3 loss), or else an earlier committed claim /
        # passing fence of this same attempt, whatever the refusal code. Never taken
        # from the refused capability, the request or a mismatched row: a token or
        # binding refusal (RC-05) with no earlier match stays NULL.
        operation = refused.matched_operation
        if operation is None:
            operation = attempt.matched_operation
        try:
            raw = await _open_as(self._p, PROVISIONER_ROLE)
        except PsycopgError as error:
            return self._write_failed(refused, error)
        conn: Any = probe.wrap(raw) if probe is not None else raw
        try:
            await conn.execute(
                f"INSERT INTO {ATTEMPTS} "
                "(attempt_id, maintenance_operation_id, tenant_id, event, detail) "
                "VALUES (%s, %s, %s, 'attempt_refused', %s::jsonb)",
                (
                    attempt.attempt_id,
                    operation,
                    attempt.request.tenant_id,
                    json.dumps(detail),
                ),
            )
        except PsycopgError as error:
            return self._write_failed(refused, error)
        finally:
            with contextlib.suppress(PsycopgError):
                await raw.close()
        return refused

    async def _record_interference(self, attempt: _Attempt, interference: _Interference) -> None:
        """Append ``interference_detected{point, kind}`` once; nothing else is written."""

        async with _session(self._p, PROVISIONER_ROLE) as conn:
            await _insert_event(
                conn,
                attempt_id=attempt.attempt_id,
                operation_id=attempt.operation_id,
                tenant_id=attempt.request.tenant_id,
                event="interference_detected",
                detail={"point": interference.point, "kind": interference.kind},
            )

    @staticmethod
    def _write_failed(refused: MaintenanceRefused, error: PsycopgError) -> MaintenanceRefused:
        failed = _refused(MaintenanceCode.MAINTENANCE_EVIDENCE_WRITE_FAILED.value, refused.phase)
        failed.__cause__ = error
        return failed


class _Interference(Exception):  # noqa: N818 - internal carrier, never surfaced
    """Internal: a T-2 mismatch found inside a step; the step's transaction rolls back
    before the controller appends ``interference_detected`` and stops."""

    def __init__(
        self, point: str, kind: str, phase: str, observed: InterferenceObservation
    ) -> None:
        super().__init__(point)
        self.point = point
        self.kind = kind
        self.phase = phase
        self.observed = observed
