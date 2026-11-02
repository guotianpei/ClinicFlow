"""L-1W W2: concurrency timelines (investigation §1.1 T-1..T-8; v1r1 S-13, S-14, B-07d).

Each row proves the block with ``pg_blocking_pids(waiter) = {holder}`` before the
holder is released; the oracle is the outcome tuple plus the persisted state read
afterwards on a new connection. A timeout is a failure, never a pass.

Holds are open transactions on a holder connection: an uncommitted admission (which
keeps its anchor SHARE and session-row locks), an uncommitted admin call, or, for
the artificial boundary holds (S-13/T-8, S-14), an admin ``SELECT ... FOR UPDATE``
on the session row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import l1w_db_support as db
import psycopg
import pytest
from l1w_db_support import Bound

pytestmark = pytest.mark.postgres


def _admit_in_background(env: db.L1WEnv, b: Bound, sid: str, auth: datetime) -> tuple[
        db.Background, psycopg.Connection[Any], int]:
    conn = env.connect("runtime")
    pid = db.backend_pid(conn)
    conn.commit()
    job = db.Background(lambda: db.admit(conn, b.ident, sid, auth)).start()
    return job, conn, pid


def _hold_row(env: db.L1WEnv, b: Bound, sid: str) -> tuple[psycopg.Connection[Any], int]:
    holder = env.connect("admin")
    pid = db.backend_pid(holder)
    row = holder.execute(
        "SELECT 1 FROM shared.staff_sessions WHERE issuer = %s AND subject = %s AND sid = %s"
        " FOR UPDATE", (b.ident.issuer, b.ident.subject, sid),
    ).fetchone()
    assert row is not None
    return holder, pid


def test_t1_concurrent_create_other_auth_time_is_sid_mismatch(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    sid, a1 = db.new_sid(), ago(minutes=2)
    with env.connect("runtime") as first:
        holder = db.backend_pid(first)
        first.commit()
        assert db.admit(first, b.ident, sid, a1, commit=False)[0] == "ADMITTED"  # uncommitted
        job, second, waiter = _admit_in_background(env, b, sid, a1 + timedelta(seconds=1))
        with second:
            db.wait_blocked_by(env, waiter, holder)
            first.commit()
            assert job.join() == ("SID_MISMATCH", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[2] == a1 and row[5] is None


def test_t2_concurrent_create_same_auth_time_both_admitted_one_row(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    sid, a1 = db.new_sid(), ago(minutes=2)
    with env.connect("runtime") as first:
        holder = db.backend_pid(first)
        first.commit()
        assert db.admit(first, b.ident, sid, a1, commit=False)[0] == "ADMITTED"
        job, second, waiter = _admit_in_background(env, b, sid, a1)
        with second:
            db.wait_blocked_by(env, waiter, holder)
            first.commit()
            assert job.join() == ("ADMITTED", b.tenant, b.staff)
    rows = env.all(
        "SELECT count(*) FROM shared.staff_sessions WHERE issuer = %s AND subject = %s"
        " AND sid = %s", (b.ident.issuer, b.ident.subject, sid))
    assert rows == [(1,)]


def test_t3_logout_waits_for_an_inflight_admission_then_revokes(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    sid, auth = db.new_sid(), ago(minutes=2)
    with env.connect("runtime") as first, env.connect("runtime") as second:
        holder, waiter = db.backend_pid(first), db.backend_pid(second)
        first.commit()
        second.commit()
        assert db.admit(first, b.ident, sid, auth, commit=False)[0] == "ADMITTED"
        job = db.Background(lambda: db.logout(second, b.ident, sid, auth)).start()
        db.wait_blocked_by(env, waiter, holder)
        first.commit()
        assert job.join() == "LOGGED_OUT"
        assert db.admit(first, b.ident, sid, auth) == ("REVOKED", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[6] == "logout"


def test_t5_admission_waits_for_unbind_then_is_unbound_without_a_row(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    sid, auth = db.new_sid(), ago(minutes=2)
    with env.connect("staff_owner") as admin_conn:
        holder = db.backend_pid(admin_conn)
        admin_conn.commit()
        assert db.admin_unbind(admin_conn, b.tenant, b.staff, b.actor, commit=False) == (
            "UNBOUND_DONE")
        job, waiter_conn, waiter = _admit_in_background(env, b, sid, auth)
        with waiter_conn:
            db.wait_blocked_by(env, waiter, holder)
            admin_conn.commit()
            assert job.join() == ("UNBOUND", None, None)
    assert env.session_row(b.ident.issuer, b.ident.subject, sid) is None


def test_t6_unbind_waits_for_an_inflight_admission_then_both_sids_unbound(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    """Codex pin 1: binding is checked before session, so after U commits both the
    existing sid and a new sid are UNBOUND; the existing row is revoked independently."""

    b = bind()
    sid, auth = db.new_sid(), ago(minutes=2)
    with env.connect("runtime") as first, env.connect("staff_owner") as admin_conn:
        holder, waiter = db.backend_pid(first), db.backend_pid(admin_conn)
        first.commit()
        admin_conn.commit()
        assert db.admit(first, b.ident, sid, auth, commit=False)[0] == "ADMITTED"
        job = db.Background(lambda: db.admin_unbind(admin_conn, b.tenant, b.staff, b.actor)).start()
        db.wait_blocked_by(env, waiter, holder)
        first.commit()
        assert job.join() == "UNBOUND_DONE"
        assert db.admit(first, b.ident, sid, auth) == ("UNBOUND", None, None)
        assert db.admit(first, b.ident, db.new_sid(), auth) == ("UNBOUND", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is not None and row[6] == "binding_revoked"


def test_t7_waiting_admission_sees_the_refresh_made_by_the_holder(
    env: db.L1WEnv, owner: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    """Also S-05: expiry versus a concurrent refresh."""

    b = bind()
    assert db.admin_set(owner, b.tenant, "idle_timeout_minutes", "5", b.actor) == "SET"
    sid, auth = db.new_sid(), ago(hours=1)
    last = ago(minutes=5) + timedelta(seconds=3)
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff, auth, last)
    with env.connect("runtime") as first:
        holder = db.backend_pid(first)
        first.commit()
        assert db.admit(first, b.ident, sid, auth, commit=False)[0] == "ADMITTED"  # refreshes
        job, second, waiter = _admit_in_background(env, b, sid, auth)
        with second:
            db.wait_blocked_by(env, waiter, holder)
            db.sleep_until(env, last + timedelta(minutes=5, seconds=1))  # old boundary passed
            first.commit()
            assert job.join() == ("ADMITTED", b.tenant, b.staff)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is None and row[4] > last


def test_s13_t8_idle_decided_with_the_post_wait_clock(
    env: db.L1WEnv, owner: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    assert db.admin_set(owner, b.tenant, "idle_timeout_minutes", "5", b.actor) == "SET"
    sid, auth = db.new_sid(), ago(hours=1)
    last = ago(minutes=5) + timedelta(seconds=2)  # 2 s inside the window when it starts
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff, auth, last)
    holder_conn, holder = _hold_row(env, b, sid)
    with holder_conn:
        job, waiter_conn, waiter = _admit_in_background(env, b, sid, auth)
        with waiter_conn:
            db.wait_blocked_by(env, waiter, holder)
            db.sleep_until(env, last + timedelta(minutes=5, seconds=1))
            holder_conn.commit()
            assert job.join() == ("IDLE", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[6] == "idle"


def test_s14_absolute_decided_with_the_post_wait_clock(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    sid = db.new_sid()
    auth = ago(hours=12) + timedelta(seconds=2)
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff, auth,
                     ago(seconds=10))
    holder_conn, holder = _hold_row(env, b, sid)
    with holder_conn:
        job, waiter_conn, waiter = _admit_in_background(env, b, sid, auth)
        with waiter_conn:
            db.wait_blocked_by(env, waiter, holder)
            db.sleep_until(env, auth + timedelta(hours=12, seconds=1))
            holder_conn.commit()
            assert job.join() == ("ABSOLUTE", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[6] == "absolute"


def test_b07d_rebind_and_admission_serialize_on_the_anchor(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    auth = ago(minutes=2)
    with env.connect("staff_owner") as admin_conn:
        assert db.admin_unbind(admin_conn, b.tenant, b.staff, b.actor) == "UNBOUND_DONE"
        new_staff = uuid.uuid4()

        # Order 1: rebind holds the anchor; a new-sid admission waits, then is admitted
        # under the new binding.
        holder = db.backend_pid(admin_conn)
        admin_conn.commit()
        assert db.admin_bind(admin_conn, b.tenant, new_staff, b.ident, b.actor,
                             commit=False) == "BOUND"
        job, waiter_conn, waiter = _admit_in_background(env, b, db.new_sid(), auth)
        with waiter_conn:
            db.wait_blocked_by(env, waiter, holder)
            admin_conn.commit()
            assert job.join() == ("ADMITTED", b.tenant, new_staff)

        # Order 2: an in-flight admission holds the anchor SHARE; unbind waits.
        assert db.admin_unbind(admin_conn, b.tenant, new_staff, b.actor) == "UNBOUND_DONE"
        assert db.admin_bind(admin_conn, b.tenant, b.staff, b.ident, b.actor) == "BOUND"
        with env.connect("runtime") as first:
            holder = db.backend_pid(first)
            waiter = db.backend_pid(admin_conn)
            first.commit()
            admin_conn.commit()
            sid = db.new_sid()
            assert db.admit(first, b.ident, sid, auth, commit=False) == (
                "ADMITTED", b.tenant, b.staff)
            job = db.Background(
                lambda: db.admin_unbind(admin_conn, b.tenant, b.staff, b.actor)).start()
            db.wait_blocked_by(env, waiter, holder)
            first.commit()
            assert job.join() == "UNBOUND_DONE"
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[6] == "binding_revoked"
