"""L-1W W2: sessions and logout at the SQL tier (package v1r1 §2, investigation §1).

Rows: S-01, S-02, S-03, S-04', S-06, S-07, S-08', S-12, LG-01, LG-02, LG-04, X-01.
Each admission is its own transaction, committed before the outcome is read (the
adapter's rule), and persisted state is read afterwards on a separate connection.
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


def test_s01_logout_is_per_device(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=5)
    sid_a, sid_b = db.new_sid(), db.new_sid()
    assert db.admit(runtime, b.ident, sid_a, auth) == ("ADMITTED", b.tenant, b.staff)
    assert db.admit(runtime, b.ident, sid_b, auth) == ("ADMITTED", b.tenant, b.staff)

    assert db.logout(runtime, b.ident, sid_a, auth) == "LOGGED_OUT"

    assert db.admit(runtime, b.ident, sid_a, auth) == ("REVOKED", None, None)
    assert db.admit(runtime, b.ident, sid_b, auth) == ("ADMITTED", b.tenant, b.staff)
    row_a = env.session_row(b.ident.issuer, b.ident.subject, sid_a)
    row_b = env.session_row(b.ident.issuer, b.ident.subject, sid_b)
    assert row_a is not None and row_a[5] is not None and row_a[6] == "logout"
    assert row_b is not None and row_b[5] is None and row_b[6] is None  # LG-03


def test_s02_refreshed_token_cannot_revive_a_revoked_sid(
    runtime: psycopg.Connection[Any], bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=5)
    sid = db.new_sid()
    assert db.admit(runtime, b.ident, sid, auth)[0] == "ADMITTED"
    assert db.logout(runtime, b.ident, sid, auth) == "LOGGED_OUT"
    # A refreshed access token keeps sid and auth_time; assurance does not help.
    assert db.admit(runtime, b.ident, sid, auth, assured=True) == ("REVOKED", None, None)
    assert db.admit(runtime, b.ident, sid, auth, assured=False) == ("REVOKED", None, None)


def test_s03_same_sid_other_auth_time_is_sid_mismatch_and_row_unchanged(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=5)
    sid = db.new_sid()
    assert db.admit(runtime, b.ident, sid, auth)[0] == "ADMITTED"
    before = env.session_row(b.ident.issuer, b.ident.subject, sid)

    assert db.admit(runtime, b.ident, sid, auth + timedelta(seconds=5)) == (
        "SID_MISMATCH", None, None)

    assert env.session_row(b.ident.issuer, b.ident.subject, sid) == before
    # The stored session stays valid for its real token (no revoke on mismatch).
    assert db.admit(runtime, b.ident, sid, auth)[0] == "ADMITTED"


def test_s04_idle_expiry_is_revoked_and_persisted(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    assert db.admin_set(owner, b.tenant, "idle_timeout_minutes", "5", b.actor) == "SET"
    sid, auth = db.new_sid(), ago(hours=1)
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff,
                     auth, ago(minutes=5, seconds=5))

    assert db.admit(runtime, b.ident, sid, auth) == ("IDLE", None, None)

    row = env.session_row(b.ident.issuer, b.ident.subject, sid)  # separate connection
    assert row is not None and row[5] is not None and row[6] == "idle"
    assert db.admit(runtime, b.ident, sid, row[2]) == ("REVOKED", None, None)


def test_s06_activity_is_coalesced_to_one_write_per_60s(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=30)
    fresh, stale = db.new_sid(), db.new_sid()
    fresh_activity = ago(seconds=50)
    stale_activity = ago(seconds=70)
    env.seed_session(b.ident.issuer, b.ident.subject, fresh, b.tenant, b.staff, auth,
                     fresh_activity)
    env.seed_session(b.ident.issuer, b.ident.subject, stale, b.tenant, b.staff, auth,
                     stale_activity)

    assert db.admit(runtime, b.ident, fresh, auth)[0] == "ADMITTED"
    assert db.admit(runtime, b.ident, stale, auth)[0] == "ADMITTED"

    fresh_row = env.session_row(b.ident.issuer, b.ident.subject, fresh)
    stale_row = env.session_row(b.ident.issuer, b.ident.subject, stale)
    assert fresh_row is not None and fresh_row[4] == fresh_activity  # no write inside 60 s
    assert stale_row is not None and stale_row[4] > stale_activity + timedelta(seconds=60)


def test_s07_auth_time_inside_skew_allowed_beyond_skew_refused(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    now: Callable[[], datetime],
) -> None:
    b = bind()
    inside, beyond = db.new_sid(), db.new_sid()
    assert db.admit(runtime, b.ident, inside, now() + timedelta(seconds=45))[0] == "ADMITTED"
    assert db.admit(runtime, b.ident, beyond, now() + timedelta(seconds=75)) == (
        "AUTH_TIME_FUTURE", None, None)
    assert env.session_row(b.ident.issuer, b.ident.subject, beyond) is None  # no insert


def test_s08_absolute_lifetime_existing_row_revoked_absent_row_not_created(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    old_auth = ago(hours=12, minutes=1)
    existing, absent, young = db.new_sid(), db.new_sid(), db.new_sid()
    env.seed_session(b.ident.issuer, b.ident.subject, existing, b.tenant, b.staff, old_auth,
                     ago(minutes=1))

    assert db.admit(runtime, b.ident, existing, old_auth) == ("ABSOLUTE", None, None)
    row = env.session_row(b.ident.issuer, b.ident.subject, existing)
    assert row is not None and row[5] is not None and row[6] == "absolute"

    assert db.admit(runtime, b.ident, absent, old_auth) == ("ABSOLUTE", None, None)
    assert env.session_row(b.ident.issuer, b.ident.subject, absent) is None

    assert db.admit(runtime, b.ident, young, ago(hours=11, minutes=55))[0] == "ADMITTED"


# ---- S-12: the session trigger matrix ---------------------------------------

_FORBIDDEN_UPDATES = [
    ("issuer", "'https://other.invalid'"),
    ("subject", "'other-subject'"),
    ("sid", "'other-sid'"),
    ("tenant_id", "{other_tenant}"),
    ("staff_id", "gen_random_uuid()"),
    ("auth_time", "auth_time - interval '1 second'"),
    ("created_at", "created_at - interval '1 second'"),
    ("revoked_at", "clock_timestamp()"),                 # without a reason
    ("revoke_reason", "'logout'"),                        # without revoked_at
    ("last_activity_at", "last_activity_at - interval '1 second'"),  # decreasing
]


def _active_row(env: db.L1WEnv, bind: Callable[..., Bound],
                ago: Callable[..., datetime]) -> tuple[Bound, str]:
    """Called in the test body (not a fixture), so a red tree fails, not errors."""

    b = bind()
    sid = db.new_sid()
    env.seed_session(b.ident.issuer, b.ident.subject, sid, b.tenant, b.staff, ago(minutes=10),
                     ago(minutes=2))
    return b, sid


def _update(env: db.L1WEnv, b: Bound, sid: str, assignment: str) -> None:
    with env.connect("admin", autocommit=True) as conn:
        conn.execute(
            f"UPDATE shared.staff_sessions SET {assignment}"
            " WHERE issuer = %s AND subject = %s AND sid = %s",
            (b.ident.issuer, b.ident.subject, sid),
        )


@pytest.mark.parametrize(("column", "expression"), _FORBIDDEN_UPDATES,
                         ids=[c for c, _ in _FORBIDDEN_UPDATES])
def test_s12_trigger_refuses_forbidden_session_updates(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime], column: str,
    expression: str,
) -> None:
    b, sid = _active_row(env, bind, ago)
    expression = expression.format(other_tenant=f"'{env.tenant()}'")  # a real tenant: no FK
    before = env.session_row(b.ident.issuer, b.ident.subject, sid)
    with pytest.raises(psycopg.Error):
        _update(env, b, sid, f"{column} = {expression}")
    assert env.session_row(b.ident.issuer, b.ident.subject, sid) == before


def test_s12_trigger_allows_the_single_revocation_and_then_freezes_it(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b, sid = _active_row(env, bind, ago)
    _update(env, b, sid, "revoked_at = clock_timestamp(), revoke_reason = 'idle'")
    revoked = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert revoked is not None and revoked[6] == "idle"
    for assignment in (
        "revoked_at = NULL, revoke_reason = NULL",                # resurrection
        "revoke_reason = 'logout'",                               # reason rewrite
        "revoked_at = clock_timestamp()",                         # timestamp rewrite
        "last_activity_at = clock_timestamp()",                   # activity after revoke
        "revoked_at = clock_timestamp(), revoke_reason = 'bogus'",
    ):
        with pytest.raises(psycopg.Error):
            _update(env, b, sid, assignment)
    assert env.session_row(b.ident.issuer, b.ident.subject, sid) == revoked


def test_s12_trigger_refuses_delete_and_non_logout_revoked_insert(
    env: db.L1WEnv, bind: Callable[..., Bound], ago: Callable[..., datetime]
) -> None:
    b, sid = _active_row(env, bind, ago)
    with pytest.raises(psycopg.Error), env.connect("admin", autocommit=True) as conn:
        conn.execute(
            "DELETE FROM shared.staff_sessions WHERE issuer = %s AND subject = %s AND sid = %s",
            (b.ident.issuer, b.ident.subject, sid),
        )
    assert env.session_row(b.ident.issuer, b.ident.subject, sid) is not None
    with pytest.raises(psycopg.Error), env.connect("admin", autocommit=True) as conn:
        conn.execute(
            "INSERT INTO shared.staff_sessions (issuer, subject, sid, tenant_id, staff_id,"
            " auth_time, created_at, last_activity_at, revoked_at, revoke_reason)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp(), 'idle')",
            (b.ident.issuer, b.ident.subject, db.new_sid(), b.tenant, b.staff, ago(minutes=1),
             ago(minutes=1), ago(minutes=1)),
        )


@pytest.mark.parametrize("table", ["staff_identity_log", "tenant_auth_settings_log"])
def test_s12_logs_are_append_only(
    env: db.L1WEnv, owner: psycopg.Connection[Any], bind: Callable[..., Bound], table: str
) -> None:
    b = bind()
    assert db.admin_set(owner, b.tenant, "mfa_required", "true", b.actor) == "SET"
    for statement in (f"UPDATE shared.{table} SET tenant_id = tenant_id WHERE tenant_id = %s",
                      f"DELETE FROM shared.{table} WHERE tenant_id = %s"):
        with pytest.raises(psycopg.Error), env.connect("admin", autocommit=True) as conn:
            conn.execute(statement, (b.tenant,))


# ---- logout -------------------------------------------------------------------


def test_lg01_logout_of_a_never_admitted_sid_leaves_a_tombstone(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=3)
    sid = db.new_sid()
    assert db.logout(runtime, b.ident, sid, auth) == "LOGGED_OUT"
    row = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert row is not None
    assert (row[0], row[1], row[2], row[6]) == (b.tenant, b.staff, auth, "logout")
    assert row[5] is not None
    # T-4: the later admission finds the tombstone.
    assert db.admit(runtime, b.ident, sid, auth) == ("REVOKED", None, None)


def test_lg02_logout_is_idempotent(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], bind: Callable[..., Bound],
    ago: Callable[..., datetime],
) -> None:
    b = bind()
    auth = ago(minutes=3)
    sid = db.new_sid()
    assert db.admit(runtime, b.ident, sid, auth)[0] == "ADMITTED"
    assert db.logout(runtime, b.ident, sid, auth) == "LOGGED_OUT"
    first = env.session_row(b.ident.issuer, b.ident.subject, sid)
    assert db.logout(runtime, b.ident, sid, auth) == "LOGGED_OUT"
    assert env.session_row(b.ident.issuer, b.ident.subject, sid) == first


def test_lg04_logout_of_an_unbound_identity_writes_nothing(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], ago: Callable[..., datetime],
) -> None:
    before = env.snapshot()
    assert db.logout(runtime, db.identity(env), db.new_sid(), ago(minutes=1)) == "UNBOUND"
    assert env.snapshot() == before


# ---- X-01: no identifiers in refusals or error text --------------------------


def test_x01_invalid_input_is_a_fixed_refusal_and_errors_carry_no_identifiers(
    env: db.L1WEnv, runtime: psycopg.Connection[Any], owner: psycopg.Connection[Any],
    bind: Callable[..., Bound], ago: Callable[..., datetime],
) -> None:
    b = bind()
    long_issuer = db.Identity("https://x.invalid/" + "i" * 600, b.ident.subject)
    long_subject = db.Identity(b.ident.issuer, "s" * 300)
    long_sid = "q" * 300
    auth = ago(minutes=1)
    before = env.snapshot()

    assert db.admit(runtime, long_issuer, db.new_sid(), auth) == ("INVALID_INPUT", None, None)
    assert db.admit(runtime, long_subject, db.new_sid(), auth) == ("INVALID_INPUT", None, None)
    assert db.admit(runtime, b.ident, long_sid, auth) == ("INVALID_INPUT", None, None)
    assert db.logout(runtime, b.ident, long_sid, auth) == "INVALID_INPUT"
    assert db.admin_bind(owner, b.tenant, uuid.uuid4(), long_issuer, b.actor) == (
        "INVALID_INPUT")
    assert env.snapshot() == before

    # A raised integrity refusal (here: the trigger) names no identifier.
    ab, sid = _active_row(env, bind, ago)
    with pytest.raises(psycopg.Error) as caught:
        _update(env, ab, sid, "sid = 'changed'")
    error: Any = caught.value
    text = " ".join(
        str(part) for part in (error, error.diag.message_primary, error.diag.message_detail,
                               error.diag.message_hint, error.diag.context) if part
    )
    for canary in (ab.ident.issuer, ab.ident.subject, sid, ab.tenant, str(ab.staff)):
        assert canary not in text
