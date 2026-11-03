"""L-1W W2: bindings, unbind and fail-closed inputs (package v1r1 §1, plan v1 §1).

Rows: B-01, B-02, B-03, B-07c, B-08 (unbind post-lock re-read), S-10, N-01 (NULL
inputs fail closed; Codex W2 plan disposition).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import l1w_db_support as db
import psycopg
import pytest
from l1w_db_support import Bound

pytestmark = pytest.mark.postgres


def test_b01_unbound_identity_is_refused_without_writes(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], ago: Callable[..., datetime]
) -> None:
    ident = db.identity(env)
    before = env.snapshot()
    assert db.admit(runtime, ident, db.new_sid(), ago(minutes=1)) == ("UNBOUND", None, None)
    assert env.snapshot() == before


def test_b02_unbind_then_rebind_creates_a_new_binding_and_keeps_the_anchor(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=2)
    old_sid = db.new_sid()
    assert db.admit(runtime, b.ident, old_sid, auth)[0] == "ADMITTED"
    anchor_before = env.all(
        "SELECT issuer, subject, created_at FROM shared.staff_identity_anchor"
        " WHERE issuer = %s AND subject = %s", (b.ident.issuer, b.ident.subject))
    first = env.one(
        "SELECT binding_id FROM shared.staff_identity_bindings WHERE issuer = %s"
        " AND subject = %s AND revoked_at IS NULL", (b.ident.issuer, b.ident.subject))

    assert db.admin_unbind(owner, b.tenant, b.staff, b.actor) == "UNBOUND_DONE"
    assert db.admit(runtime, b.ident, db.new_sid(), auth) == ("UNBOUND", None, None)
    assert db.admin_unbind(owner, b.tenant, b.staff, b.actor) == "NOT_BOUND"

    assert db.admin_bind(owner, b.tenant, b.staff, b.ident, b.actor) == "BOUND"
    second = env.one(
        "SELECT binding_id FROM shared.staff_identity_bindings WHERE issuer = %s"
        " AND subject = %s AND revoked_at IS NULL", (b.ident.issuer, b.ident.subject))
    assert first is not None and second is not None and first != second
    assert env.all(
        "SELECT issuer, subject, created_at FROM shared.staff_identity_anchor"
        " WHERE issuer = %s AND subject = %s", (b.ident.issuer, b.ident.subject)
    ) == anchor_before
    assert env.all(
        "SELECT count(*) FROM shared.staff_identity_bindings WHERE issuer = %s"
        " AND subject = %s", (b.ident.issuer, b.ident.subject)) == [(2,)]
    # The old sid was revoked by the unbind and stays revoked after the rebind.
    assert db.admit(runtime, b.ident, old_sid, auth) == ("REVOKED", None, None)
    assert db.admit(runtime, b.ident, db.new_sid(), auth) == ("ADMITTED", b.tenant, b.staff)
    log = env.all(
        "SELECT action, actor_kind, actor_id FROM shared.staff_identity_log"
        " WHERE tenant_id = %s AND staff_id = %s ORDER BY log_id", (b.tenant, b.staff))
    assert log == [("bind", "clinic_admin", str(b.actor)),
                   ("unbind", "clinic_admin", str(b.actor)),
                   ("bind", "clinic_admin", str(b.actor))]


def test_b03_one_active_binding_per_identity_and_per_staff(
    env: db.L1WEnv, owner: psycopg.Connection[Any], bind: Callable[..., Bound]
) -> None:
    b = bind()
    other_tenant = env.tenant()
    before = env.snapshot()
    assert db.admin_bind(owner, b.tenant, uuid.uuid4(), b.ident, b.actor) == (
        "IDENTITY_ALREADY_BOUND")
    assert db.admin_bind(owner, other_tenant, uuid.uuid4(), b.ident, b.actor) == (
        "IDENTITY_ALREADY_BOUND")
    assert db.admin_bind(owner, b.tenant, b.staff, db.identity(env), b.actor) == (
        "STAFF_ALREADY_BOUND")
    assert env.snapshot() == before


def test_b07c_rebind_to_other_staff_old_sid_revoked_new_sid_admitted(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=2)
    old_sid = db.new_sid()
    assert db.admit(runtime, b.ident, old_sid, auth)[0] == "ADMITTED"
    new_staff = uuid.uuid4()
    assert db.admin_unbind(owner, b.tenant, b.staff, b.actor) == "UNBOUND_DONE"
    assert db.admin_bind(owner, b.tenant, new_staff, b.ident, b.actor) == "BOUND"

    assert db.admit(runtime, b.ident, old_sid, auth) == ("REVOKED", None, None)
    assert db.admit(runtime, b.ident, db.new_sid(), auth) == ("ADMITTED", b.tenant, new_staff)


def test_b07c_unrevoked_row_for_another_binding_is_binding_mismatch_and_revoked(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    """Defence in depth (investigation step 5(b)): a surviving active row whose
    tenant/staff differ from the current binding. Unbind revokes rows, so this is
    seeded through the escape hatch."""

    b = bind()
    auth = ago(minutes=2)
    sid = db.new_sid()
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, uuid.uuid4(), auth,
                     ago(minutes=1))
    assert db.admit(runtime, b.ident, sid, auth) == ("BINDING_MISMATCH", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is not None and row[6] == "binding_mismatch"


def test_s10_unbind_revokes_every_active_session_of_the_identity(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=2)
    sids = [db.new_sid(), db.new_sid()]
    for sid in sids:
        assert db.admit(runtime, b.ident, sid, auth)[0] == "ADMITTED"
    unrelated = bind(b.tenant)
    unrelated_sid = db.new_sid()
    assert db.admit(runtime, unrelated.ident, unrelated_sid, auth)[0] == "ADMITTED"

    assert db.admin_unbind(owner, b.tenant, b.staff, b.actor) == "UNBOUND_DONE"

    for sid in sids:
        row = env.session_row(b.ident.issuer, b.ident.subject, sid)
        assert row is not None and row[5] is not None and row[6] == "binding_revoked"
        assert db.admit(runtime, b.ident, sid, auth) == ("UNBOUND", None, None)
    other = env.session_row(unrelated.ident.issuer, unrelated.ident.subject, unrelated_sid)
    assert other is not None and other[5] is None


def test_b08_unbind_rereads_after_the_anchor_lock_and_never_revokes_another_binding(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    """Codex W2 plan disposition. B waits on identity X's anchor to unbind staff S.
    Before B gets the lock, A unbinds S and rebinds S to identity Y in one
    transaction. B's post-lock re-read finds S now bound to Y, not to the locked
    anchor X: B must refuse and leave Y's binding intact (no out-of-order anchor)."""

    b = bind()
    other = db.identity(env)
    auth = ago(minutes=2)
    with (env.connect("runtime") as holder_conn,
          env.connect("staff_owner") as a_conn,
          env.connect("staff_owner") as b_conn):
        holder, a_pid, b_pid = (db.backend_pid(c) for c in (holder_conn, a_conn, b_conn))
        for conn in (holder_conn, a_conn, b_conn):
            conn.commit()
        assert db.admit(holder_conn, b.ident, db.new_sid(), auth, commit=False)[0] == "ADMITTED"

        def a_unbind_and_rebind() -> tuple[str, str]:
            first = db.admin_unbind(a_conn, b.tenant, b.staff, b.actor, commit=False)
            second = db.admin_bind(a_conn, b.tenant, b.staff, other, b.actor, commit=False)
            a_conn.commit()
            return first, second

        a_job = db.Background(a_unbind_and_rebind).start()
        db.wait_blocked_by(env, a_pid, holder)
        b_job = db.Background(
            lambda: db.admin_unbind(b_conn, b.tenant, b.staff, b.actor)).start()
        deadline_ok = False
        for _ in range(200):
            row = env.one("SELECT pg_blocking_pids(%s)", (b_pid,))
            if row and row[0]:
                deadline_ok = True
                break
            time.sleep(0.05)
        assert deadline_ok, "B never waited on the anchor"
        holder_conn.commit()
        assert a_job.join() == ("UNBOUND_DONE", "BOUND")
        assert b_job.join() == "NOT_BOUND"

    active = env.all(
        "SELECT issuer, subject FROM shared.staff_identity_bindings WHERE tenant_id = %s"
        " AND staff_id = %s AND revoked_at IS NULL", (b.tenant, b.staff))
    assert active == [(other.issuer, other.subject)]
    log = env.all(
        "SELECT action FROM shared.staff_identity_log WHERE tenant_id = %s AND staff_id = %s"
        " ORDER BY log_id", (b.tenant, b.staff))
    assert log == [("bind",), ("unbind",), ("bind",)]


# ---- N-01: SQL NULL inputs fail closed --------------------------------------


@pytest.mark.parametrize("field", ["issuer", "subject", "sid", "auth_time"])
def test_n01_null_admission_input_is_invalid_input(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime], field: str,
) -> None:
    b = bind()
    args: dict[str, Any] = {"issuer": b.ident.issuer, "subject": b.ident.subject,
                            "sid": db.new_sid(), "auth_time": ago(minutes=1)}
    args[field] = None
    before = env.snapshot()
    row = runtime.execute(
        "SELECT outcome, tenant_id, staff_id FROM shared.l1w_admit(%s, %s, %s, %s, false)",
        (args["issuer"], args["subject"], args["sid"], args["auth_time"]),
    ).fetchone()
    runtime.commit()
    assert row == ("INVALID_INPUT", None, None)
    assert env.snapshot() == before


def test_n01_null_assured_with_mfa_on_is_mfa_required_and_off_is_admitted(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=1)

    def call(sid: str) -> Any:
        row = runtime.execute(
            "SELECT outcome, tenant_id, staff_id FROM shared.l1w_admit(%s, %s, %s, %s, NULL)",
            (b.ident.issuer, b.ident.subject, sid, auth),
        ).fetchone()
        runtime.commit()
        return row

    assert call(db.new_sid()) == ("ADMITTED", b.tenant, b.staff)  # MFA off (default)
    assert db.admin_set(owner, b.tenant, "mfa_required", "true", b.actor) == "SET"
    assert call(db.new_sid()) == ("MFA_REQUIRED", None, None)


@pytest.mark.parametrize("field", ["tenant", "staff", "issuer", "subject", "actor"])
def test_n01_null_bind_input_is_invalid_input(
    env: db.L1WEnv, owner: psycopg.Connection[Any], field: str
) -> None:
    args: dict[str, Any] = {"tenant": env.tenant(), "staff": uuid.uuid4(),
                            "issuer": db.identity(env).issuer, "subject": db.identity(env).subject,
                            "actor": uuid.uuid4()}
    args[field] = None
    before = env.snapshot()
    row = owner.execute(
        "SELECT shared.l1w_admin_bind(%s, %s, %s, %s, %s)",
        (args["tenant"], args["staff"], args["issuer"], args["subject"], args["actor"]),
    ).fetchone()
    owner.commit()
    assert row == ("INVALID_INPUT",)
    assert env.snapshot() == before
