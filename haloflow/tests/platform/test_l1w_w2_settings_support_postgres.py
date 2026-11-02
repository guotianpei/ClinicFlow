"""L-1W W2: per-clinic auth settings and support operations (v1r1 §4, investigation §2.1).

Rows: A-01, A-02, A-03, A-04, A-05, A-07, SP-01, SP-02, SP-03, SP-04, SP-08.
Support fixture logins exist only in the disposable cluster (l1w_db_support).
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

IDENTITY_CAP = "l1w.identity.admin"
SETTINGS_CAP = "l1w.settings.admin"


def _support(env: db.L1WEnv, key: str, *, allowlist: bool = True) -> tuple[str, str]:
    """A fixture support login (member of haloflow_support), optionally allowlisted."""

    actor_id = f"support-actor-{env.token}-{key}"
    env.login(key, db.SUPPORT)
    if allowlist:
        env.allowlist_support(key, actor_id)
    return key, actor_id


def _settings(env: db.L1WEnv, tenant: str) -> tuple[Any, ...] | None:
    return env.one(
        "SELECT idle_timeout_minutes, mfa_required FROM shared.tenant_auth_settings"
        " WHERE tenant_id = %s", (tenant,))


def _list(conn: psycopg.Connection[Any], tenant: str) -> tuple[Any, ...] | None:
    row = conn.execute(
        "SELECT outcome, idle_timeout_minutes, mfa_required"
        " FROM shared.l1w_support_list_tenant_settings(%s)", (tenant,)).fetchone()
    conn.commit()
    return row


# ---- A rows ---------------------------------------------------------------------


def test_a01_mfa_on_refuses_unassured_without_revoking(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=3)
    sid = db.new_sid()
    assert db.admit(runtime, b.ident, sid, auth)[0] == "ADMITTED"
    assert db.admin_set(owner, b.tenant, "mfa_required", "true", b.actor) == "SET"

    assert db.admit(runtime, b.ident, sid, auth, assured=False) == ("MFA_REQUIRED", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None and row[5] is None
    assert db.admit(runtime, b.ident, db.new_sid(), auth, assured=True) == (
        "ADMITTED", b.tenant, b.staff)


def test_a02_mfa_off_admits_unassured(
    runtime: psycopg.Connection[Any], bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    assert db.admit(runtime, b.ident, db.new_sid(), ago(minutes=1), assured=False) == (
        "ADMITTED", b.tenant, b.staff)


def test_a03_idle_change_applies_to_existing_sessions(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(hours=1)
    first, second = db.new_sid(), db.new_sid()
    for sid in (first, second):
        env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff, auth,
                         ago(minutes=10))
    assert db.admit(runtime, b.ident, first, auth)[0] == "ADMITTED"  # idle 15 (default)
    assert db.admin_set(owner, b.tenant, "idle_timeout_minutes", "5", b.actor) == "SET"
    assert db.admit(runtime, b.ident, second, auth) == ("IDLE", None, None)


@pytest.mark.parametrize(
    ("setting", "value", "outcome"),
    [
        ("idle_timeout_minutes", "4", "INVALID_VALUE"),
        ("idle_timeout_minutes", "61", "INVALID_VALUE"),
        ("idle_timeout_minutes", "15.5", "INVALID_VALUE"),
        ("idle_timeout_minutes", "abc", "INVALID_VALUE"),
        ("idle_timeout_minutes", "", "INVALID_VALUE"),
        ("mfa_required", "maybe", "INVALID_VALUE"),
        ("mfa_required", "1", "INVALID_VALUE"),
        ("max_session_hours", "8", "INVALID_SETTING"),
        ("", "15", "INVALID_SETTING"),
    ],
)
def test_a04_out_of_bounds_or_unknown_setting_refused_without_writes(
    env: db.L1WEnv, owner: psycopg.Connection[Any], bind: Callable[..., Bound],
    setting: str, value: str, outcome: str,
) -> None:
    b = bind()
    support_key, support_actor = _support(env, f"a04{uuid.uuid4().hex[:6]}")
    env.support_grant(support_actor, b.tenant, [SETTINGS_CAP])
    before = env.snapshot()
    assert db.admin_set(owner, b.tenant, setting, value, b.actor) == outcome
    with env.connect(support_key) as support:
        assert db.support_call(support, "l1w_support_set_setting", b.tenant, setting,
                               value) == outcome
    assert env.snapshot() == before


def test_a04_bounds_are_inclusive(
    env: db.L1WEnv, owner: psycopg.Connection[Any], bind: Callable[..., Bound]
) -> None:
    b = bind()
    assert db.admin_set(owner, b.tenant, "idle_timeout_minutes", "5", b.actor) == "SET"
    assert _settings(env, b.tenant) == (5, False)
    assert db.admin_set(owner, b.tenant, "idle_timeout_minutes", "60", b.actor) == "SET"
    assert _settings(env, b.tenant) == (60, False)


def test_a05_admin_and_support_changes_are_logged_with_attribution(
    env: db.L1WEnv, owner: psycopg.Connection[Any], bind: Callable[..., Bound]
) -> None:
    b = bind()
    support_key, support_actor = _support(env, "a05")
    grant_id, ticket = env.support_grant(support_actor, b.tenant, [SETTINGS_CAP])

    assert db.admin_set(owner, b.tenant, "idle_timeout_minutes", "20", b.actor) == "SET"
    with env.connect(support_key) as support:
        assert db.support_call(support, "l1w_support_set_setting", b.tenant,
                               "idle_timeout_minutes", "30") == "SET"
    assert _settings(env, b.tenant) == (30, False)
    log = env.all(
        "SELECT setting, old_value, new_value, actor_kind, actor_id, support_grant_id,"
        " ticket_reference FROM shared.tenant_auth_settings_log WHERE tenant_id = %s"
        " ORDER BY log_id", (b.tenant,))
    assert log == [
        ("idle_timeout_minutes", "15", "20", "clinic_admin", str(b.actor), None, None),
        ("idle_timeout_minutes", "20", "30", "support", support_actor, grant_id, ticket),
    ]


def test_a07_new_tenant_uses_the_owner_defaults(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    assert _settings(env, b.tenant) is None  # no row: defaults apply
    auth = ago(hours=1)
    inside, outside = db.new_sid(), db.new_sid()
    env.seed_session(b.ident.issuer, b.ident.subject, inside, b.tenant, b.staff, auth,
                     ago(minutes=14))
    env.seed_session(b.ident.issuer, b.ident.subject, outside, b.tenant, b.staff, auth,
                     ago(minutes=15, seconds=5))
    assert db.admit(runtime, b.ident, inside, auth, assured=False)[0] == "ADMITTED"  # MFA off
    assert db.admit(runtime, b.ident, outside, auth) == ("IDLE", None, None)

    support_key, support_actor = _support(env, "a07")
    env.support_grant(support_actor, b.tenant, [SETTINGS_CAP])
    with env.connect(support_key) as support:
        assert _list(support, b.tenant) == ("OK", 15, False)


# ---- SP rows --------------------------------------------------------------------


def _support_attempts(conn: psycopg.Connection[Any], b: Bound, ident: db.Identity) -> list[Any]:
    return [
        db.support_call(conn, "l1w_support_bind", b.tenant, uuid.uuid4(), ident.issuer,
                        ident.subject),
        db.support_call(conn, "l1w_support_unbind", b.tenant, b.staff),
        db.support_call(conn, "l1w_support_set_setting", b.tenant, "mfa_required", "true"),
        _list(conn, b.tenant)[0],  # type: ignore[index]
    ]


def test_sp01_support_login_not_in_the_allowlist_is_refused(
    env: db.L1WEnv, bind: Callable[..., Bound]
) -> None:
    b = bind()
    key, actor = _support(env, "sp01", allowlist=False)
    env.support_grant(actor, b.tenant, [IDENTITY_CAP, SETTINGS_CAP])
    before = env.snapshot()
    with env.connect(key) as conn:
        assert _support_attempts(conn, b, db.identity(env)) == ["SUPPORT_NOT_AUTHORIZED"] * 4
    assert env.snapshot() == before


def test_sp02_renamed_login_no_longer_matches_the_allowlist(
    env: db.L1WEnv, bind: Callable[..., Bound]
) -> None:
    b = bind()
    key, actor = _support(env, "sp02")
    env.support_grant(actor, b.tenant, [IDENTITY_CAP, SETTINGS_CAP])
    renamed = f"haloflow_test_l1w_{env.token}_sp02renamed"
    with env.connect("admin", autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                psycopg.sql.Identifier(env.logins[key]), psycopg.sql.Identifier(renamed)))
        conn.execute(
            psycopg.sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                psycopg.sql.Identifier(renamed), psycopg.sql.Literal(db.FIXTURE_PASSWORD)))
    env.logins[key] = renamed  # same OID, new name
    before = env.snapshot()
    with env.connect(key) as conn:
        assert _support_attempts(conn, b, db.identity(env)) == ["SUPPORT_NOT_AUTHORIZED"] * 4
    assert env.snapshot() == before


@pytest.mark.parametrize(
    "case", ["no-grant", "expired", "not-started", "revoked-state", "other-tenant",
             "missing-capability"])
def test_sp03_grant_must_be_active_current_for_the_tenant_and_capable(
    env: db.L1WEnv, bind: Callable[..., Bound], case: str
) -> None:
    b = bind()
    key, actor = _support(env, f"sp03{case.replace('-', '')}")
    caps = [IDENTITY_CAP, SETTINGS_CAP]
    if case == "expired":
        env.support_grant(actor, b.tenant, caps, starts=timedelta(hours=-2),
                          expires=timedelta(seconds=-1))
    elif case == "not-started":
        env.support_grant(actor, b.tenant, caps, starts=timedelta(minutes=5),
                          expires=timedelta(hours=1))
    elif case == "revoked-state":
        env.support_grant(actor, b.tenant, caps, state="revoked")
    elif case == "other-tenant":
        env.support_grant(actor, env.tenant(), caps)
    elif case == "missing-capability":
        env.support_grant(actor, b.tenant, ["m01.read"])
    before = env.snapshot()
    with env.connect(key) as conn:
        assert _support_attempts(conn, b, db.identity(env)) == ["SUPPORT_NOT_AUTHORIZED"] * 4
    assert env.snapshot() == before


def test_sp03_capabilities_are_separate(env: db.L1WEnv, bind: Callable[..., Bound]) -> None:
    b = bind()
    key, actor = _support(env, "sp03sep")
    env.support_grant(actor, b.tenant, [SETTINGS_CAP])  # settings only
    with env.connect(key) as conn:
        ident = db.identity(env)
        assert db.support_call(conn, "l1w_support_bind", b.tenant, uuid.uuid4(), ident.issuer,
                               ident.subject) == "SUPPORT_NOT_AUTHORIZED"
        assert db.support_call(conn, "l1w_support_set_setting", b.tenant, "mfa_required",
                               "true") == "SET"


def test_sp04_valid_support_bind_and_unbind_are_attributed(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    tenant = env.tenant()
    key, actor = _support(env, "sp04")
    grant_id, ticket = env.support_grant(actor, tenant, [IDENTITY_CAP])
    staff, ident = uuid.uuid4(), db.identity(env)
    with env.connect(key) as conn:
        assert db.support_call(conn, "l1w_support_bind", tenant, staff, ident.issuer,
                               ident.subject) == "BOUND"
        assert db.admit(runtime, ident, db.new_sid(), ago(minutes=1)) == (
            "ADMITTED", tenant, staff)
        assert db.support_call(conn, "l1w_support_unbind", tenant, staff) == "UNBOUND_DONE"
    binding = env.one(
        "SELECT created_by_kind, created_by, revoked_by_kind, revoked_by"
        " FROM shared.staff_identity_bindings WHERE tenant_id = %s AND staff_id = %s",
        (tenant, staff))
    assert binding == ("support", actor, "support", actor)
    log = env.all(
        "SELECT action, actor_kind, actor_id, support_grant_id, ticket_reference"
        " FROM shared.staff_identity_log WHERE tenant_id = %s ORDER BY log_id", (tenant,))
    assert log == [("bind", "support", actor, grant_id, ticket),
                   ("unbind", "support", actor, grant_id, ticket)]


def test_sp08_grant_revoked_while_waiting_on_the_anchor_is_not_used(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b = bind()
    key, actor = _support(env, "sp08")
    grant_id, _ = env.support_grant(actor, b.tenant, [IDENTITY_CAP])
    with env.connect("runtime") as holder_conn, env.connect(key) as support:
        holder, waiter = db.backend_pid(holder_conn), db.backend_pid(support)
        holder_conn.commit()
        support.commit()
        assert db.admit(holder_conn, b.ident, db.new_sid(), ago(minutes=1),
                        commit=False)[0] == "ADMITTED"  # holds the anchor SHARE
        log_before = env.all(
            "SELECT count(*) FROM shared.staff_identity_log WHERE tenant_id = %s", (b.tenant,))
        job = db.Background(
            lambda: db.support_call(support, "l1w_support_unbind", b.tenant, b.staff)).start()
        db.wait_blocked_by(env, waiter, holder)
        with env.connect("admin", autocommit=True) as gate:
            gate.execute("UPDATE shared.support_access_grants SET state = 'revoked'"
                         " WHERE grant_id = %s", (grant_id,))
        holder_conn.commit()
        assert job.join() == "SUPPORT_NOT_AUTHORIZED"
    assert env.all(
        "SELECT count(*) FROM shared.staff_identity_log WHERE tenant_id = %s", (b.tenant,)
    ) == log_before
    assert env.one(
        "SELECT revoked_at FROM shared.staff_identity_bindings WHERE tenant_id = %s"
        " AND staff_id = %s", (b.tenant, b.staff)) == (None,)
