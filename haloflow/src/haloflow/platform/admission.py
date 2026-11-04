"""Per-request admission (L-1W W4a, plan v1 R-W4-3, addendum 1 pins 1-2).

One ``shared.l1w_admit`` call in its own transaction, then exactly one COMMIT. The
outcome is acted on only after that COMMIT returns. Any failure to acquire, call or
commit is ``AdmissionUnavailable`` (503); rollback is never inferred from an
exception, so a failed or unknown COMMIT is ``ADMISSION_COMMIT_UNKNOWN`` whatever
persisted. Exceptions carry fixed codes only, with no cause or context.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from haloflow.platform.verify import VerifiedIdentity

ADMITTED: Final = "ADMITTED"
ADMISSION_REFUSALS: Final = frozenset({
    "UNBOUND", "BINDING_MISMATCH", "REVOKED", "SID_MISMATCH", "AUTH_TIME_FUTURE",
    "ABSOLUTE", "IDLE", "MFA_REQUIRED", "INVALID_INPUT",
})
COMMIT_UNKNOWN: Final = "ADMISSION_COMMIT_UNKNOWN"
UNAVAILABLE: Final = "ADMISSION_UNAVAILABLE"
LOGGED_OUT: Final = "LOGGED_OUT"
_LOGOUT_REFUSALS: Final = frozenset({"UNBOUND", "INVALID_INPUT"})

_ADMIT_SQL: Final = (
    "SELECT outcome, tenant_id, staff_id FROM shared.l1w_admit(%s, %s, %s, %s, %s)"
)
_LOGOUT_SQL: Final = "SELECT shared.l1w_logout(%s, %s, %s, %s)"

ConnectionSource = Callable[[], AbstractAsyncContextManager[Any]]


class AdmissionRefused(Exception):
    """A committed policy refusal (401). ``code`` is the l1w_admit/l1w_logout outcome."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AdmissionUnavailable(Exception):
    """Admission could not be decided (503)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class AdmittedSession:
    tenant_id: str
    staff_id: uuid.UUID
    identity: VerifiedIdentity


class IdentityAdmission:
    __slots__ = ("_connect",)

    def __init__(self, connect: ConnectionSource) -> None:
        self._connect = connect

    async def _one(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...]:
        stage = "acquire"
        row: Any = None
        failed: str | None
        try:
            async with self._connect() as conn:
                stage = "call"
                cursor = await conn.execute(sql, params)
                row = await cursor.fetchone()
                stage = "commit"
                await conn.commit()
                stage = "committed"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - classified below, outside the handler
            failed = stage
        else:
            failed = None
        if failed == "committed":  # the COMMIT returned; only releasing failed
            failed = None
        if failed is not None:
            raise AdmissionUnavailable(COMMIT_UNKNOWN if failed == "commit" else UNAVAILABLE)
        if type(row) is not tuple:
            raise AdmissionUnavailable(UNAVAILABLE)
        return row

    async def admit(self, identity: VerifiedIdentity) -> AdmittedSession:
        row = await self._one(_ADMIT_SQL, (identity.issuer, identity.subject, identity.sid,
                                           identity.auth_time, identity.assured))
        if len(row) != 3:
            raise AdmissionUnavailable(UNAVAILABLE)
        outcome, tenant_id, staff_id = row
        if outcome == ADMITTED and type(tenant_id) is str and tenant_id \
                and type(staff_id) is uuid.UUID:
            return AdmittedSession(tenant_id=tenant_id, staff_id=staff_id, identity=identity)
        if type(outcome) is str and outcome in ADMISSION_REFUSALS:
            raise AdmissionRefused(outcome)
        raise AdmissionUnavailable(UNAVAILABLE)

    async def logout(self, identity: VerifiedIdentity) -> None:
        row = await self._one(_LOGOUT_SQL, (identity.issuer, identity.subject, identity.sid,
                                            identity.auth_time))
        outcome = row[0] if len(row) == 1 else None
        if outcome == LOGGED_OUT:
            return None
        if type(outcome) is str and outcome in _LOGOUT_REFUSALS:
            raise AdmissionRefused(outcome)
        raise AdmissionUnavailable(UNAVAILABLE)


class AdmissionPool:
    """A small runtime-role pool for admission transactions only (Q-W4-1). Its
    connections are non-autocommit; a connection returned inside a transaction is
    rolled back by the pool."""

    def __init__(self, conninfo: str, *, min_size: int = 1, max_size: int = 4,
                 timeout: float = 10.0) -> None:
        self._pool: AsyncConnectionPool[AsyncConnection[Any]] = AsyncConnectionPool(
            conninfo, min_size=min_size, max_size=max_size, timeout=timeout,
            name="haloflow-platform-admission", open=False,
            kwargs={"autocommit": False, "prepare_threshold": None},
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[Any]]:
        async with self._pool.connection() as conn:
            yield conn
