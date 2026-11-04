"""L-1W W4a: the admission adapter over PostgreSQL 17 (plan v1 R-W4-3; addendum 1
pins 1-2; Codex 183528a3 construction pins).

PINNED INTERFACE (W4a contract; red-gate reviewed):
- ``haloflow.platform.admission.IdentityAdmission(connect)``. ``connect()`` returns an
  async context manager yielding one psycopg ``AsyncConnection`` in NON-autocommit
  mode, logged in as a member of ``haloflow_runtime``. Production passes
  ``AdmissionPool.connection``; these rows pass the fixture runtime login.
- ``await admission.admit(identity: VerifiedIdentity) -> AdmittedSession`` with
  fields ``tenant_id: str``, ``staff_id: UUID``, ``identity: VerifiedIdentity``.
  One ``shared.l1w_admit(iss, sub, sid, auth_time, assured)`` call, then exactly
  one ``await connection.commit()``. The outcome is acted on only after that
  commit RETURNS.
- A committed non-ADMITTED outcome raises ``AdmissionRefused(code)``, ``code`` the
  l1w_admit outcome, one of ``ADMISSION_REFUSALS``.
- ANY exception from ``commit()`` (or from the call, or from acquiring the
  connection) raises ``AdmissionUnavailable(reason_code)``, ``reason_code`` in
  {"ADMISSION_COMMIT_UNKNOWN", "ADMISSION_UNAVAILABLE"}; never AdmissionRefused,
  never an AdmittedSession. The adapter never infers rollback from an exception.
  ``__cause__``/``__context__`` are None; str/repr carry no identifier.
- ``await admission.logout(identity)`` runs ``shared.l1w_logout`` + commit;
  "LOGGED_OUT" returns None; any other outcome raises AdmissionRefused; commit
  failure raises AdmissionUnavailable("ADMISSION_COMMIT_UNKNOWN").

TEST SEAM (labelled): ``CommitSeam`` wraps the real connection and replaces only
``commit``. Mode "count" passes through and counts. Mode "before" rolls back and
raises without sending COMMIT (known rollback). Mode "after" sends the REAL COMMIT,
waits for it to complete on the server, then raises instead of returning
(acknowledgement withheld: Codex pin, ACF-3/4). Persisted state is always read on a
separate admin connection, and for "after" the committed effect is observed BEFORE
the adapter's result is asserted.

Rows: ADMIT-MAP, ADMIT-PERSIST, ACF-1..4, LO-01 (adapter tier; HTTP tier in
test_l1w_w4a_app_postgres.py).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import l1w_db_support as db
import psycopg
import pytest
from l1w_db_support import Bound
from psycopg import AsyncConnection

pytestmark = pytest.mark.postgres

REFUSALS = frozenset({
    "UNBOUND", "BINDING_MISMATCH", "REVOKED", "SID_MISMATCH", "AUTH_TIME_FUTURE",
    "ABSOLUTE", "IDLE", "MFA_REQUIRED", "INVALID_INPUT",
})


def _identity(ident: db.Identity, sid: str, auth_time: datetime, assured: bool = False) -> Any:
    from haloflow.platform.verify import VerifiedIdentity

    return VerifiedIdentity(issuer=ident.issuer, subject=ident.subject, sid=sid,
                            auth_time=auth_time, assured=assured, client_id="test-client",
                            jti=f"jti-{uuid.uuid4()}")


class CommitSeam:
    """TEST SEAM: a real connection whose commit fails before or after the server commit."""

    def __init__(self, conn: AsyncConnection[Any], mode: str) -> None:
        self._conn = conn
        self.mode = mode
        self.commits = 0
        self.server_committed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def commit(self) -> None:
        self.commits += 1
        if self.mode == "count":  # passthrough: count only
            await self._conn.commit()
            self.server_committed = True
            return
        if self.mode == "before":
            await self._conn.rollback()
            raise psycopg.OperationalError("seam: connection lost before COMMIT was sent")
        await self._conn.commit()
        self.server_committed = True
        raise psycopg.OperationalError("seam: COMMIT acknowledgement lost")


def _source(env: db.L1WEnv, mode: str | None = None) -> tuple[Callable[[], Any], list[Any]]:
    seams: list[Any] = []

    @asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        conn = await AsyncConnection.connect(env.conninfo("runtime"), autocommit=False)
        try:
            if mode is None:
                yield conn
            else:
                seam = CommitSeam(conn, mode)
                seams.append(seam)
                yield seam
        finally:
            await conn.close()

    return connect, seams


def _admission(env: db.L1WEnv, mode: str | None = None) -> tuple[Any, list[Any]]:
    from haloflow.platform.admission import IdentityAdmission

    connect, seams = _source(env, mode)
    return IdentityAdmission(connect), seams


def _assert_sanitized(error: BaseException, *secrets: str) -> None:
    assert error.__cause__ is None and error.__context__ is None
    for text in (str(error), repr(error), *map(str, error.args)):
        for secret in secrets:
            assert secret not in text


# ---------------------------------------------------------------------------
# ADMIT-MAP: the adapter surface itself
# ---------------------------------------------------------------------------


def test_admit_map_refusal_set_is_pinned() -> None:
    from haloflow.platform.admission import ADMISSION_REFUSALS

    assert frozenset(ADMISSION_REFUSALS) == REFUSALS


async def test_admit_map_admitted_returns_binding_and_commits_once(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    sid, auth = db.new_sid(), ago(minutes=3)
    admission, seams = _admission(env, "count")
    session = await admission.admit(_identity(b.ident, sid, auth))
    assert [seam.commits for seam in seams] == [1]  # one connection, exactly one COMMIT
    assert (session.tenant_id, session.staff_id) == (b.tenant, b.staff)
    assert session.identity.sid == sid
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is None  # committed, active


async def _refusal(admission: Any, identity: Any) -> Any:
    from haloflow.platform.admission import AdmissionRefused

    with pytest.raises(AdmissionRefused) as caught:
        await admission.admit(identity)
    return caught.value


async def test_admit_map_every_refusal_outcome(
    env: db.L1WEnv, owner: psycopg.Connection[Any], runtime: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime], now: Callable[[], datetime],
) -> None:
    admission, _ = _admission(env)
    cases: dict[str, Any] = {}

    stranger = db.identity(env)
    cases["UNBOUND"] = _identity(stranger, db.new_sid(), ago(minutes=1))

    b = bind()
    sid = db.new_sid()
    auth = ago(minutes=4)
    assert db.admit(runtime, b.ident, sid, auth)[0] == "ADMITTED"
    assert db.logout(runtime, b.ident, sid, auth) == "LOGGED_OUT"
    cases["REVOKED"] = _identity(b.ident, sid, auth)

    sid2 = db.new_sid()
    assert db.admit(runtime, b.ident, sid2, auth)[0] == "ADMITTED"
    cases["SID_MISMATCH"] = _identity(b.ident, sid2, auth - timedelta(seconds=30))

    cases["AUTH_TIME_FUTURE"] = _identity(b.ident, db.new_sid(), now() + timedelta(minutes=5))

    sid3, auth3 = db.new_sid(), ago(hours=12, minutes=1)  # captured once (Codex ff2ef544)
    env.seed_session(b.ident.issuer, b.ident.subject, sid3, b.tenant, b.staff,
                     auth3, ago(minutes=1))
    cases["ABSOLUTE"] = _identity(b.ident, sid3, auth3)

    sid4, auth4 = db.new_sid(), ago(hours=1)
    env.seed_session(b.ident.issuer, b.ident.subject, sid4, b.tenant, b.staff,
                     auth4, ago(minutes=16))
    cases["IDLE"] = _identity(b.ident, sid4, auth4)

    sid5 = db.new_sid()
    env.seed_session(b.ident.issuer, b.ident.subject, sid5, b.tenant, uuid.uuid4(), auth,
                     ago(minutes=1))
    cases["BINDING_MISMATCH"] = _identity(b.ident, sid5, auth)

    cases["INVALID_INPUT"] = _identity(b.ident, "x" * 256, auth)

    m = bind()
    assert db.admin_set(owner, m.tenant, "mfa_required", "true", m.actor) == "SET"
    cases["MFA_REQUIRED"] = _identity(m.ident, db.new_sid(), ago(minutes=1), assured=False)

    assert set(cases) == REFUSALS
    from haloflow.platform.admission import AdmissionRefused, AdmissionUnavailable

    # Every case is observed, then compared at once, so a failure names exactly which
    # outcome changed (Codex 366f7e94: map each failed assertion to its SQL change).
    observed: dict[str, str] = {}
    for code, identity in cases.items():
        try:
            await admission.admit(identity)
        except AdmissionRefused as error:
            observed[code] = error.code
            _assert_sanitized(error, identity.issuer, identity.subject, identity.sid)
        except AdmissionUnavailable as error:
            observed[code] = f"UNAVAILABLE:{error.reason_code}"
        else:
            observed[code] = "ADMITTED"
    assert observed == {code: code for code in cases}


# ---------------------------------------------------------------------------
# ADMIT-PERSIST: confirmed-commit refusals persist their revocation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["IDLE", "ABSOLUTE", "BINDING_MISMATCH"])
async def test_admit_persist_confirmed_refusal_revocation_is_durable(
    kind: str, env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    sid = db.new_sid()
    if kind == "IDLE":
        auth, staff, last = ago(hours=1), b.staff, ago(minutes=16)
    elif kind == "ABSOLUTE":
        auth, staff, last = ago(hours=12, minutes=1), b.staff, ago(minutes=1)
    else:
        auth, staff, last = ago(minutes=5), uuid.uuid4(), ago(minutes=1)
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, staff, auth, last)
    admission, _ = _admission(env)

    error = await _refusal(admission, _identity(b.ident, sid, auth))

    assert error.code == kind
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is not None
    assert row[6] == {"IDLE": "idle", "ABSOLUTE": "absolute",
                      "BINDING_MISMATCH": "binding_mismatch"}[kind]


# ---------------------------------------------------------------------------
# ACF-1..4: failed or unknown COMMIT -> AdmissionUnavailable, whatever persisted
# ---------------------------------------------------------------------------


async def _unavailable(admission: Any, identity: Any) -> Any:
    from haloflow.platform.admission import AdmissionUnavailable

    with pytest.raises(AdmissionUnavailable) as caught:
        await admission.admit(identity)
    error = caught.value
    assert error.reason_code == "ADMISSION_COMMIT_UNKNOWN"
    _assert_sanitized(error, identity.issuer, identity.subject, identity.sid)
    return error


async def test_acf1_known_rollback_on_admitted_is_unavailable_and_nothing_persists(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    sid, auth = db.new_sid(), ago(minutes=2)
    before = env.snapshot()
    admission, seams = _admission(env, "before")

    await _unavailable(admission, _identity(b.ident, sid, auth))

    assert [s.commits for s in seams] == [1] and not seams[0].server_committed
    assert env.session_row(b.ident.issuer, b.ident.subject, sid) is None
    assert env.snapshot() == before


async def test_acf2_known_rollback_on_idle_is_unavailable_and_revocation_not_persisted(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    sid, auth = db.new_sid(), ago(hours=1)
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff, auth,
                     ago(minutes=16))
    admission, seams = _admission(env, "before")

    await _unavailable(admission, _identity(b.ident, sid, auth))

    assert [s.commits for s in seams] == [1]
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is None and row[6] is None


async def test_acf3_committed_but_ack_lost_on_admitted_is_unavailable_session_persisted(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    from haloflow.platform.admission import AdmissionUnavailable

    b = bind()
    sid, auth = db.new_sid(), ago(minutes=2)
    admission, seams = _admission(env, "after")
    identity = _identity(b.ident, sid, auth)

    with pytest.raises(AdmissionUnavailable) as caught:
        await admission.admit(identity)

    # The committed effect is observed independently first, then the result.
    assert seams[0].server_committed
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is None  # durable active session exists
    assert caught.value.reason_code == "ADMISSION_COMMIT_UNKNOWN"
    _assert_sanitized(caught.value, b.ident.issuer, b.ident.subject, sid)


async def test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    from haloflow.platform.admission import AdmissionUnavailable

    b = bind()
    sid, auth = db.new_sid(), ago(hours=1)
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff, auth,
                     ago(minutes=16))
    admission, seams = _admission(env, "after")

    with pytest.raises(AdmissionUnavailable) as caught:
        await admission.admit(_identity(b.ident, sid, auth))

    assert seams[0].server_committed
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is not None and row[6] == "idle"
    assert caught.value.reason_code == "ADMISSION_COMMIT_UNKNOWN"


async def test_acf_connection_acquisition_failure_is_unavailable(
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    from haloflow.platform.admission import AdmissionUnavailable, IdentityAdmission

    @asynccontextmanager
    async def broken() -> AsyncIterator[Any]:
        raise psycopg.OperationalError("seam: pool exhausted")
        yield  # pragma: no cover

    b = bind()
    with pytest.raises(AdmissionUnavailable) as caught:
        await IdentityAdmission(broken).admit(_identity(b.ident, db.new_sid(), ago(minutes=1)))
    assert caught.value.reason_code == "ADMISSION_UNAVAILABLE"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


# ---------------------------------------------------------------------------
# LO-01 (adapter tier): logout is per device; commit uncertainty is unavailable
# ---------------------------------------------------------------------------


async def test_lo01_adapter_logout_revokes_only_that_session(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    from haloflow.platform.admission import AdmissionRefused

    b = bind()
    auth = ago(minutes=3)
    a, c = _identity(b.ident, db.new_sid(), auth), _identity(b.ident, db.new_sid(), auth)
    admission, _ = _admission(env)
    await admission.admit(a)
    await admission.admit(c)

    assert await admission.logout(a) is None

    with pytest.raises(AdmissionRefused) as caught:
        await admission.admit(a)
    assert caught.value.code == "REVOKED"
    assert (await admission.admit(c)).staff_id == b.staff
    row = env.session_row(b.ident.issuer, b.ident.subject, a.sid)
    assert row is not None and row[6] == "logout"


async def test_lo01_adapter_logout_commit_unknown_is_unavailable(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    from haloflow.platform.admission import AdmissionUnavailable

    b = bind()
    identity = _identity(b.ident, db.new_sid(), ago(minutes=3))
    plain, _ = _admission(env)
    await plain.admit(identity)
    admission, seams = _admission(env, "after")

    with pytest.raises(AdmissionUnavailable) as caught:
        await admission.logout(identity)

    assert seams[0].server_committed
    row = env.session_row(b.ident.issuer, b.ident.subject, identity.sid)
    assert row is not None and row[6] == "logout"
    assert caught.value.reason_code == "ADMISSION_COMMIT_UNKNOWN"
