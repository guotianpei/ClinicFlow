"""PostgreSQL harness for the L-1W W2 shared identity objects (alembic 008).

Plan v1 §2. Everything here is test-only.

Fixture isolation (Codex final disposition, pin 4). Synthetic LOGIN roles are
cluster-global, so a database-name prefix alone is not isolation. They are created
only when BOTH hold:
- ``HALOFLOW_TEST_DISPOSABLE_CLUSTER=1`` (set on the CI job, whose ``postgres:17``
  service is a fresh container per run, or by a throwaway local ``initdb`` cluster);
- the database name starts with ``haloflow_test``.
Otherwise the harness fails; it never skips. Role names carry a per-run random
token, every role is dropped at teardown, and teardown asserts none remain.

Fixtures deliberately touch no W2 object. On a tree without revision 008 every
W2 test therefore fails inside its own body (undefined function or role), so the
red set is reported as FAILED tests rather than fixture errors.

Seeding escape hatch (admin connection, documented, as for ``reset_tenants``):
tenant rows with unique ids, and backdated session rows for the 12 h and idle
boundaries, which no test can wait for. Seeded session rows have exactly the shape
``shared.l1w_admit`` writes.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DISPOSABLE_ENV: Final = "HALOFLOW_TEST_DISPOSABLE_CLUSTER"
DATABASE_PREFIX: Final = "haloflow_test"
FIXTURE_PASSWORD: Final = "l1w-local-test-only"

RUNTIME: Final = "haloflow_runtime"
STAFF_OWNER: Final = "haloflow_staff_owner"
SUPPORT: Final = "haloflow_support"
IDENTITY_OWNER: Final = "haloflow_identity_owner"

SKEW: Final = timedelta(seconds=60)
MAX_SESSION: Final = timedelta(hours=12)
COALESCE: Final = timedelta(seconds=60)

ADMIT_OUTCOMES: Final = frozenset(
    {
        "ADMITTED", "UNBOUND", "BINDING_MISMATCH", "REVOKED", "SID_MISMATCH",
        "AUTH_TIME_FUTURE", "ABSOLUTE", "IDLE", "MFA_REQUIRED",
    }
)

L1W_TABLES: Final = (
    "staff_identity_bindings",
    "staff_identity_anchor",
    "staff_sessions",
    "tenant_auth_settings",
    "tenant_auth_settings_log",
    "staff_identity_log",
    "support_operator_logins",
)


class FixtureIsolationRefused(Exception):
    """The environment is not a disposable test cluster; no role was created."""


def check_fixture_isolation(environ: dict[str, str], database: str) -> None:
    """FX-01. Refuse unless the env flag is exactly "1" and the name has the prefix."""

    if environ.get(DISPOSABLE_ENV) != "1":
        raise FixtureIsolationRefused(f"{DISPOSABLE_ENV}=1 is required")
    if not database.startswith(DATABASE_PREFIX):
        raise FixtureIsolationRefused(f"database name must start with {DATABASE_PREFIX}")


@dataclass
class L1WEnv:
    """Per-run handles: admin conninfo, fixture logins and synthetic tenants."""

    admin: str
    token: str
    logins: dict[str, str] = field(default_factory=dict)  # key -> role name
    tenants: list[str] = field(default_factory=list)

    # ---- connections -------------------------------------------------------

    def conninfo(self, key: str) -> str:
        params = conninfo_to_dict(self.admin)
        params.update(user=self.logins[key], password=FIXTURE_PASSWORD)
        return make_conninfo("", **params)

    def connect(self, key: str, *, autocommit: bool = False) -> psycopg.Connection[Any]:
        if key == "admin":
            return psycopg.connect(self.admin, autocommit=autocommit)
        return psycopg.connect(self.conninfo(key), autocommit=autocommit)

    # ---- logins (created lazily; cluster-global, token-named) ---------------

    def login(self, key: str, group: str) -> str:
        """Create (once) a LOGIN role named for this run, member of ``group``."""

        if key in self.logins:
            return self.logins[key]
        name = f"haloflow_test_l1w_{self.token}_{key}"
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                    sql.Identifier(name), sql.Literal(FIXTURE_PASSWORD), sql.Identifier(group)
                )
            )
            conn.execute(sql.SQL("ALTER ROLE {} SET search_path = ''").format(sql.Identifier(name)))
        self.logins[key] = name
        return name

    def allowlist_support(self, key: str, actor_id: str) -> None:
        """Insert the support_operator_logins row for one fixture login only."""

        name = self.logins[key]
        assert name.startswith(f"haloflow_test_l1w_{self.token}_"), "fixture logins only"
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO shared.support_operator_logins"
                " (login_oid, login_name, actor_id, added_at, decision_ref)"
                " SELECT oid, rolname, %s, clock_timestamp(), %s FROM pg_roles WHERE rolname = %s",
                (actor_id, "0" * 64, name),
            )

    # ---- tenants and grants -------------------------------------------------

    def tenant(self) -> str:
        suffix = secrets.token_hex(4)
        tenant_id = f"l1wt-{self.token}-{suffix}"
        schema_key = f"tenant_{self.token}{suffix}"
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO shared.tenants"
                " (tenant_id, schema_key, lifecycle_state, schema_version)"
                " VALUES (%s, %s, 'active', 1)",
                (tenant_id, schema_key),
            )
        self.tenants.append(tenant_id)
        return tenant_id

    def support_grant(
        self,
        actor_id: str,
        tenant_id: str,
        capabilities: list[str],
        *,
        state: str = "active",
        starts: timedelta = timedelta(hours=-1),
        expires: timedelta = timedelta(hours=1),
    ) -> tuple[uuid.UUID, str]:
        grant_id = uuid.uuid4()
        ticket = f"TICKET-{secrets.token_hex(4)}"
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO shared.support_access_grants (grant_id, actor_id, tenant_id,"
                " ticket_reference, purpose_code, capabilities, approver_id, starts_at,"
                " expires_at, state) VALUES (%s, %s, %s, %s, 'l1w-test', %s, 'approver-test',"
                " clock_timestamp() + %s, clock_timestamp() + %s, %s)",
                (grant_id, actor_id, tenant_id, ticket, capabilities, starts, expires, state),
            )
        return grant_id, ticket

    # ---- reads and seeds (admin) -------------------------------------------

    def one(self, query: str, params: Any = None) -> tuple[Any, ...] | None:
        with psycopg.connect(self.admin, autocommit=True) as conn:
            return conn.execute(query, params).fetchone()

    def all(self, query: str, params: Any = None) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.admin, autocommit=True) as conn:
            return conn.execute(query, params).fetchall()

    def db_now(self) -> datetime:
        row = self.one("SELECT clock_timestamp()")
        assert row is not None
        return row[0]  # type: ignore[no-any-return]

    def session_row(self, issuer: str, subject: str, sid: str) -> tuple[Any, ...] | None:
        return self.one(
            "SELECT tenant_id, staff_id, auth_time, created_at, last_activity_at, revoked_at,"
            " revoke_reason FROM shared.staff_sessions"
            " WHERE issuer = %s AND subject = %s AND sid = %s",
            (issuer, subject, sid),
        )

    def seed_session(
        self,
        issuer: str,
        subject: str,
        sid: str,
        tenant_id: str,
        staff_id: uuid.UUID,
        auth_time: datetime,
        last_activity_at: datetime,
    ) -> None:
        """Escape hatch: a backdated active row shaped as l1w_admit writes it."""

        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO shared.staff_sessions (issuer, subject, sid, tenant_id, staff_id,"
                " auth_time, created_at, last_activity_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (issuer, subject, sid, tenant_id, staff_id, auth_time, last_activity_at,
                 last_activity_at),
            )

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        """Full state of every L-1W table, for no-write oracles."""

        out: dict[str, list[tuple[Any, ...]]] = {}
        with psycopg.connect(self.admin, autocommit=True) as conn:
            for table in L1W_TABLES:
                out[table] = conn.execute(
                    sql.SQL("SELECT * FROM shared.{} ORDER BY 1, 2").format(sql.Identifier(table))
                ).fetchall()
        return out

    # ---- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        with psycopg.connect(self.admin, autocommit=True) as conn:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n"
                    " ON n.oid = c.relnamespace WHERE n.nspname = 'shared' AND c.relkind = 'r'"
                ).fetchall()
            }
            tenant_tables = [
                t for t in ("staff_sessions", "staff_identity_bindings", "staff_identity_log",
                            "tenant_auth_settings_log", "tenant_auth_settings") if t in present
            ]
            if self.tenants:
                for table in tenant_tables:
                    _with_triggers_disabled(
                        conn, table,
                        sql.SQL("DELETE FROM shared.{} WHERE tenant_id = ANY(%s)").format(
                            sql.Identifier(table)),
                        (self.tenants,),
                    )
                if "staff_identity_anchor" in present:
                    conn.execute(
                        "DELETE FROM shared.staff_identity_anchor WHERE subject LIKE %s",
                        (f"sub-{self.token}-%",),
                    )
                conn.execute(
                    "DELETE FROM shared.support_access_grants WHERE tenant_id = ANY(%s)",
                    (self.tenants,),
                )
                conn.execute(
                    "DELETE FROM shared.tenants WHERE tenant_id = ANY(%s)", (self.tenants,))
            if "support_operator_logins" in present:
                conn.execute(
                    "DELETE FROM shared.support_operator_logins WHERE login_name LIKE %s",
                    (f"haloflow_test_l1w_{self.token}_%",),
                )
            for name in self.logins.values():
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
            left = conn.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                (f"haloflow_test\\_l1w\\_{self.token}\\_%",),
            ).fetchall()
        assert left == [], f"fixture roles left behind: {left}"


def _with_triggers_disabled(
    conn: psycopg.Connection[Any], table: str, statement: sql.Composed, params: Any
) -> None:
    ident = sql.Identifier(table)
    conn.execute(sql.SQL("ALTER TABLE shared.{} DISABLE TRIGGER USER").format(ident))
    try:
        conn.execute(statement, params)
    finally:
        conn.execute(sql.SQL("ALTER TABLE shared.{} ENABLE TRIGGER USER").format(ident))


@contextmanager
def l1w_environment(admin_conninfo: str) -> Iterator[L1WEnv]:
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    assert row is not None
    check_fixture_isolation(dict(os.environ), str(row[0]))
    env = L1WEnv(admin=admin_conninfo, token=secrets.token_hex(3))
    try:
        yield env
    finally:
        env.teardown()


# ---- identities ------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str


def identity(env: L1WEnv) -> Identity:
    """Synthetic issuer/subject; the subject carries the run token for teardown."""

    return Identity(
        issuer=f"https://issuer.l1w-test.invalid/{secrets.token_hex(3)}",
        subject=f"sub-{env.token}-{secrets.token_hex(6)}",
    )


@dataclass(frozen=True)
class Bound:
    """A tenant, a staff id and an identity bound to it by a clinic admin."""

    tenant: str
    staff: uuid.UUID
    ident: Identity
    actor: uuid.UUID


def new_sid() -> str:
    return f"sid-{secrets.token_hex(8)}"


# ---- definer calls ---------------------------------------------------------


def admit(
    conn: psycopg.Connection[Any],
    ident: Identity,
    sid: str,
    auth_time: datetime,
    assured: bool = False,
    *,
    commit: bool = True,
) -> tuple[str, str | None, uuid.UUID | None]:
    """The adapter's admission transaction: call, then COMMIT before mapping."""

    row = conn.execute(
        "SELECT outcome, tenant_id, staff_id FROM shared.l1w_admit(%s, %s, %s, %s, %s)",
        (ident.issuer, ident.subject, sid, auth_time, assured),
    ).fetchone()
    if commit:
        conn.commit()
    assert row is not None
    return row[0], row[1], row[2]


def logout(conn: psycopg.Connection[Any], ident: Identity, sid: str, auth_time: datetime) -> str:
    row = conn.execute(
        "SELECT shared.l1w_logout(%s, %s, %s, %s)", (ident.issuer, ident.subject, sid, auth_time)
    ).fetchone()
    conn.commit()
    assert row is not None
    return str(row[0])


def admin_bind(
    conn: psycopg.Connection[Any], tenant: str, staff: uuid.UUID, ident: Identity, actor: uuid.UUID,
    *, commit: bool = True,
) -> str:
    row = conn.execute(
        "SELECT shared.l1w_admin_bind(%s, %s, %s, %s, %s)",
        (tenant, staff, ident.issuer, ident.subject, actor),
    ).fetchone()
    if commit:
        conn.commit()
    assert row is not None
    return str(row[0])


def admin_unbind(
    conn: psycopg.Connection[Any], tenant: str, staff: uuid.UUID, actor: uuid.UUID,
    *, commit: bool = True,
) -> str:
    row = conn.execute(
        "SELECT shared.l1w_admin_unbind(%s, %s, %s)", (tenant, staff, actor)
    ).fetchone()
    if commit:
        conn.commit()
    assert row is not None
    return str(row[0])


def admin_set(
    conn: psycopg.Connection[Any], tenant: str, setting: str, value: str, actor: uuid.UUID,
    *, commit: bool = True,
) -> str:
    row = conn.execute(
        "SELECT shared.l1w_admin_set_setting(%s, %s, %s, %s)", (tenant, setting, value, actor)
    ).fetchone()
    if commit:
        conn.commit()
    assert row is not None
    return str(row[0])


def support_call(conn: psycopg.Connection[Any], function: str, *args: Any) -> Any:
    placeholders = ", ".join(["%s"] * len(args))
    row = conn.execute(
        sql.SQL("SELECT shared.{}(" + placeholders + ")").format(sql.Identifier(function)), args
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


# ---- concurrency -----------------------------------------------------------


def backend_pid(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT pg_backend_pid()").fetchone()
    assert row is not None
    return int(row[0])


def wait_blocked_by(env: L1WEnv, waiter: int, holder: int, timeout_s: float = 10.0) -> None:
    """Prove the block: pg_blocking_pids(waiter) = {holder}. A timeout is a failure."""

    deadline = time.monotonic() + timeout_s
    last: list[int] = []
    while time.monotonic() < deadline:
        row = env.one("SELECT pg_blocking_pids(%s)", (waiter,))
        last = list(row[0]) if row and row[0] else []
        if set(last) == {holder}:
            return
        time.sleep(0.05)
    raise AssertionError(f"waiter {waiter} not blocked by {holder} (saw {last})")


class Background:
    """Run one callable on a thread with its own connection; join with a deadline."""

    def __init__(self, target: Callable[[], Any]) -> None:
        self._target = target
        self.result: Any = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.result = self._target()
        except BaseException as error:  # noqa: BLE001 - re-raised by join()
            self.error = error

    def start(self) -> Background:
        self._thread.start()
        return self

    def join(self, timeout_s: float = 30.0) -> Any:
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            raise AssertionError("background call did not finish (a timeout is a failure)")
        if self.error is not None:
            raise self.error
        return self.result


def sleep_until(env: L1WEnv, moment: datetime) -> None:
    """Wait (by the database clock) until ``moment`` has passed."""

    while True:
        remaining = (moment - env.db_now()).total_seconds()
        if remaining < 0:
            return
        time.sleep(min(remaining + 0.05, 1.0))
