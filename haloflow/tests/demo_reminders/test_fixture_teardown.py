"""PG16 teardown package (Codex-aligned F1): the ``demo_db_url`` teardown helper.

Test harness only. Requirements TD-R1..TD-R5 are in Work Session 2026-10-29,
claude_pg16-teardown-package-2026-10-29-v1.md; Codex corrections are in
codex_pg16-teardown-package-v1-review.md.

Environment:
- ``DEMO_TEST_ADMIN_URL`` (existing): the role the fixture uses.
- ``DEMO_TEST_MONITOR_URL`` (optional, TD-T1): a role that can read other roles'
  ``backend_type`` (``pg_read_all_stats`` or superuser). The admin role cannot,
  as measured on PG16 (f1-proto-evidence.log).
- ``DEMO_TEST_OTHER_URL`` (optional, TD-T4): a distinct role that can CONNECT.

Missing capabilities give explicit skips. A TD-T1 worker that is not observed
after every prerequisite passed is an ENVIRONMENT failure, not evidence about the
helper. No test changes server settings or role privileges.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from .conftest import _admin_url, _drop_fixture_db, _new_fixture_db_name, _with_dbname

OBSERVE_SECONDS = 90
HALOFLOW_DIR = Path(__file__).resolve().parents[2]


def _log(msg: str) -> None:
    print(f"[teardown-test] {msg}", flush=True)


def _one(conn: psycopg.Connection[Any], sql: str, params: Any = None) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _exists(admin: str, name: str) -> bool:
    with psycopg.connect(admin, autocommit=True) as c:
        return bool(_one(c, "SELECT count(*) FROM pg_database WHERE datname = %s", (name,)))


def _cleanup(admin: str, name: str, created: bool) -> None:
    """Drop only the database this test created, as its owner, with a plain DROP.
    Retries while an autovacuum worker or a closing session is still attached."""
    if not created:
        return
    deadline = time.monotonic() + 120
    while _exists(admin, name):
        try:
            with psycopg.connect(admin, autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{name}"')
        except psycopg.errors.ObjectInUse:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def _admin_caps(admin: str) -> dict[str, Any]:
    with psycopg.connect(admin, autocommit=True) as c:
        row = c.execute(
            "SELECT current_user, rolsuper, rolcreatedb,"
            " pg_has_role(current_user, 'pg_signal_backend', 'MEMBER'),"
            " current_setting('server_version'), current_setting('autovacuum'),"
            " current_setting('autovacuum_naptime')"
            " FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row is not None
    keys = ("user", "super", "createdb", "signal", "version", "autovacuum", "naptime")
    caps = dict(zip(keys, row, strict=True))
    _log("admin capabilities: " + ", ".join(f"{k}={v}" for k, v in caps.items()))
    return caps


# --- TD-T3: name guard (no database; recording connection) ---------------------

class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: Any, params: Any = None) -> Any:
        self.calls.append((str(query), params))
        return self


BAD_NAMES = [
    "haloflow_thu1029_demo",        # the 10/29 demo database
    "haloflow_final1105_demo",
    "postgres",
    "haloflow_tABCDEF12_demo",      # upper-case hex
    "haloflow_t1234567_demo",       # 7 hex digits
    'haloflow_t12345678_demo" ; DROP DATABASE "x',
    "haloflow_t12345678_demo\n",
    "",
]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_td_t3_guard_rejects_before_any_statement(bad: str) -> None:
    rec = _RecordingConn()
    with pytest.raises(ValueError):
        _drop_fixture_db(rec, bad)  # type: ignore[arg-type]
    assert rec.calls == []


def test_td_t3_guard_is_unconditional_under_python_O() -> None:
    """TD-R3 must hold with assertions stripped (python -O)."""
    code = (
        "from tests.demo_reminders.conftest import _drop_fixture_db\n"
        "class R:\n"
        "    calls = []\n"
        "    def execute(self, q, p=None):\n"
        "        R.calls.append(q)\n"
        "try:\n"
        "    _drop_fixture_db(R(), 'haloflow_thu1029_demo')\n"
        "    print('NOT_REJECTED', len(R.calls))\n"
        "except ValueError:\n"
        "    print('REJECTED', len(R.calls))\n"
    )
    out = subprocess.run([sys.executable, "-O", "-c", code], cwd=HALOFLOW_DIR,
                         capture_output=True, text=True, timeout=60)
    _log(f"python -O guard probe: rc={out.returncode} stdout={out.stdout.strip()!r}")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "REJECTED 0"


def test_td_t3_valid_name_terminates_own_role_then_plain_drop() -> None:
    """TD-R2 / TD-R1 shape: own-role sessions only, then DROP without FORCE."""
    name = _new_fixture_db_name()
    rec = _RecordingConn()
    _drop_fixture_db(rec, name)  # type: ignore[arg-type]
    assert len(rec.calls) == 2, rec.calls
    term_sql, term_params = rec.calls[0]
    assert "pg_terminate_backend" in term_sql
    assert "usename = current_user" in term_sql
    assert "pid <> pg_backend_pid()" in term_sql
    assert term_params == (name,)
    drop_sql, drop_params = rec.calls[1]
    assert drop_sql == f'DROP DATABASE IF EXISTS "{name}"'
    assert "FORCE" not in drop_sql.upper()
    assert drop_params is None


# --- TD-T2: own-role leftover session -----------------------------------------

def test_td_t2_own_role_session_is_ended_and_db_dropped() -> None:
    admin = _admin_url()
    name, created = _new_fixture_db_name(), False
    leftover: psycopg.Connection[Any] | None = None
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{name}"')
        created = True
        leftover = psycopg.connect(_with_dbname(admin, name), autocommit=True)
        pid = _one(leftover, "SELECT pg_backend_pid()")
        with psycopg.connect(admin, autocommit=True) as c:
            _drop_fixture_db(c, name)
        assert not _exists(admin, name)
        with psycopg.connect(admin, autocommit=True) as c:
            assert _one(c, "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)) == 0
    finally:
        if leftover is not None:
            leftover.close()
        _cleanup(admin, name, created)


# --- TD-T1: autovacuum worker connected at teardown (the PG16 regression) -----

def test_td_t1_teardown_succeeds_with_autovacuum_worker_connected() -> None:
    admin = _admin_url()
    monitor = os.environ.get("DEMO_TEST_MONITOR_URL", "")
    if not monitor:
        pytest.skip("DEMO_TEST_MONITOR_URL not set: cannot observe an autovacuum worker")
    caps = _admin_caps(admin)
    if caps["super"] or caps["signal"]:
        pytest.skip("admin role is superuser or has pg_signal_backend:"
                    " regression precondition unmet")
    if not caps["createdb"]:
        pytest.skip("admin role lacks CREATEDB")
    if caps["autovacuum"] != "on":
        pytest.skip("autovacuum is off on this server")
    with psycopg.connect(monitor, autocommit=True) as m:
        can_see = _one(m, "SELECT rolsuper OR pg_has_role(current_user, 'pg_read_all_stats',"
                          " 'MEMBER') FROM pg_roles WHERE rolname = current_user")
        _log(f"monitor user={_one(m, 'SELECT current_user')} can_read_all_stats={can_see}")
    if not can_see:
        pytest.skip("monitor role cannot read other roles' backend_type")

    name, created = _new_fixture_db_name(), False
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{name}"')
            admin_oid = _one(c, "SELECT oid FROM pg_database WHERE datname = %s", (name,))
        created = True
        with psycopg.connect(monitor, autocommit=True) as m:
            mon_oid = m.execute("SELECT oid FROM pg_database WHERE datname = %s",
                                (name,)).fetchone()
        if mon_oid is None or mon_oid[0] != admin_oid:
            pytest.fail("ENVIRONMENT: monitor and admin URLs are not the same server")
        with psycopg.connect(_with_dbname(admin, name), autocommit=True) as d:
            d.execute("CREATE TABLE t (i int, pad text)")
            d.execute("ALTER TABLE t SET (autovacuum_vacuum_cost_delay = 100,"
                      " autovacuum_vacuum_cost_limit = 1, autovacuum_vacuum_threshold = 0,"
                      " autovacuum_vacuum_scale_factor = 0)")
            d.execute("INSERT INTO t SELECT g, repeat('x', 200) FROM generate_series(1, 200000) g")
            d.execute("DELETE FROM t WHERE i % 2 = 0")
        worker = None
        deadline = time.monotonic() + OBSERVE_SECONDS
        with psycopg.connect(monitor, autocommit=True) as m:
            while time.monotonic() < deadline:
                worker = m.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datname = %s"
                    " AND backend_type = 'autovacuum worker'", (name,)).fetchone()
                if worker is not None:
                    break
                time.sleep(0.5)
        if worker is None:
            pytest.fail(f"ENVIRONMENT: no autovacuum worker observed on the test database within"
                        f" {OBSERVE_SECONDS}s (naptime={caps['naptime']}); not evidence about"
                        " the teardown helper")
        _log(f"autovacuum worker pid={worker[0]} observed on {name}; calling the teardown helper")
        with psycopg.connect(admin, autocommit=True) as c:
            _drop_fixture_db(c, name)
        assert not _exists(admin, name)
    finally:
        _cleanup(admin, name, created)


# --- TD-T4: another role's session is never terminated -------------------------

def test_td_t4_other_role_session_surfaces_object_in_use() -> None:
    admin = _admin_url()
    other_url = os.environ.get("DEMO_TEST_OTHER_URL", "")
    if not other_url:
        pytest.skip("DEMO_TEST_OTHER_URL not set: no distinct role available")
    caps = _admin_caps(admin)
    name, created = _new_fixture_db_name(), False
    other: psycopg.Connection[Any] | None = None
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{name}"')
        created = True
        try:
            other = psycopg.connect(_with_dbname(other_url, name), autocommit=True)
        except psycopg.OperationalError as exc:
            pytest.skip(f"other role cannot CONNECT to the test database: {type(exc).__name__}")
        o_user = _one(other, "SELECT current_user")
        o_pid = _one(other, "SELECT pg_backend_pid()")
        with psycopg.connect(admin, autocommit=True) as c:
            o_super = _one(c, "SELECT rolsuper FROM pg_roles WHERE rolname = %s", (o_user,))
            member = _one(c, "SELECT pg_has_role(current_user, %s, 'USAGE')", (o_user,))
        _log(f"other role={o_user} super={o_super} admin_has_its_privileges={member}")
        if o_user == caps["user"] or o_super or member or caps["super"]:
            pytest.skip("other role is not distinct from the admin role's privileges")
        with (psycopg.connect(admin, autocommit=True) as c,
              pytest.raises(psycopg.errors.ObjectInUse)):
            _drop_fixture_db(c, name)
        assert _one(other, "SELECT pg_backend_pid()") == o_pid  # not terminated
        assert _exists(admin, name)
    finally:
        if other is not None:
            other.close()   # its own client closes it; no termination by the admin
        _cleanup(admin, name, created)
