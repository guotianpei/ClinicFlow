"""L-1W W3b R1 (Codex 170024Z): replay integrity of m01_staff_admin_v2 (t029) for the
operations that exist only in v2. The v1-shaped operations are covered by the
staff-directory DA-D7/DA-D8 rows, which now run on t029 as runtime.

The stored log result is the closed FOUR-key result; "outcome": "replayed" is only
appended on return, never stored. Crafted key/log rows are inserted through the admin
connection (FIXTURE: they model a corrupt or a valid stored history; the rejector
triggers forbid UPDATE of real rows).

Rows: RI-01 a real v2-only operation replays its stored result with outcome
"replayed" and writes nothing; RI-02 a constraint-valid stored snapshot with an
impossible timestamp never replays; RI-03 a stored result carrying "outcome" never
replays; RI-04 an activate whose stored snapshot is inactive never replays; RI-05 the
crafted valid control replays.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import l1w_w3a_support as w3
import psycopg
import pytest
import staff_support as st
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres

TS_OK = "2026-10-01T09:30:00.000000Z"


def _setup(install: st.StaffInstall) -> str:
    install.require()
    st.ensure_admin(install)
    staff = st.new_uuid()
    st.seed_staff(install, staff, name="Replay Target")
    return staff


def _digest_v2(tenant: str, key: str, actor: str, operation: str, staff: str, *,
               role: str | None = None) -> bytes:
    """Written out independently of the code under test (t029 preimage)."""

    elements = ["m01.staff_admin/v2", tenant, key, actor, operation, staff, None, None,
                role, None, None, None, None]
    preimage = "[" + ",".join(
        json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in elements) + "]"
    return hashlib.sha256(preimage.encode("utf-8")).digest()


def _command(operation: str, staff: str, key: str, role: str | None) -> dict[str, Any]:
    if role is not None:
        return w3.role_command(operation, staff, role, key=key)
    return st.admin_command(operation, staff, key=key)


def _crafted(install: st.StaffInstall, operation: str, staff: str, key: str, *,
             role: str | None = None, status: str = "active",
             timestamp: str = TS_OK) -> None:
    """FIXTURE: one constraint-valid key row and log row for `operation` on `staff`."""

    digest = _digest_v2(install.tenant[0], key, st.ADMIN, operation, staff, role=role)
    snap = {"schema_version": 1, "staff_id": staff, "status": status,
            "display_name": "Replay Target", "access": ["act", "read"],
            "status_changed_at": timestamp}
    result = {"schema_version": 1, "client_action_id": key, "operation": operation,
              "staff": dict(snap)}
    schema = sql.Identifier(install.schema)
    st.admin_rows(install, sql.SQL(
        "INSERT INTO {}.m01_staff_action_keys VALUES (%s, %s, %s, %s, now())").format(schema),
        (key, digest, st.ADMIN, staff))
    st.admin_rows(install, sql.SQL(
        "INSERT INTO {}.m01_staff_admin_log VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s,"
        " %s, %s, NULL, 'operations', %s, now())").format(schema),
        (key, staff, operation, Jsonb(snap), Jsonb(snap), Jsonb(result), st.ADMIN,
         "00000000-0000-4000-8000-00000000e001", digest))


# --- RI-06/RI-07: identity and setting operations (Codex 181616Z) --------------------

# Shared state the three operations write through 008's admin writers (every row of
# each table; nothing else runs in this session while a row executes).
SHARED_TABLES = ("staff_identity_anchor", "staff_identity_bindings", "staff_identity_log",
                 "staff_sessions", "tenant_auth_settings", "tenant_auth_settings_log")


def _shared(install: st.StaffInstall) -> dict[str, list[Any]]:
    return {t: st.admin_rows(install, sql.SQL(
        "SELECT to_jsonb(x)::text FROM shared.{} AS x ORDER BY 1").format(sql.Identifier(t)))
        for t in SHARED_TABLES}


def _ident() -> dict[str, str]:
    return {"issuer": "https://issuer.l1w-test.invalid/ri",
            "subject": f"sub-ri-{st.new_uuid()[:8]}"}


def _identity_command(operation: str, staff: str, key: str) -> dict[str, Any]:
    if operation == "bind_identity":
        return st.admin_command(operation, staff, key=key, extra=_ident())
    if operation == "set_auth_setting":
        return st.admin_command(operation, st.ADMIN, key=key,
                                extra={"setting": "idle_timeout_minutes", "value": "25"})
    return st.admin_command(operation, staff, key=key)


def _identity_setup(install: st.StaffInstall, operation: str) -> str:
    staff = _setup(install)
    if operation == "unbind_identity":
        bound = st.call(install, w3.ADMIN_V2, st.admin_command("bind_identity", staff,
                                                               extra=_ident()))
        assert bound["outcome"] == "applied"
    return staff


@pytest.mark.parametrize("operation", ["bind_identity", "unbind_identity", "set_auth_setting"])
def test_ri06_identity_and_setting_operations_replay_exactly(staff_install, operation):
    """Through runtime: applied, then the same command replays the stored result with
    outcome "replayed" and writes nothing in the tenant tables or the shared identity,
    session, setting and audit tables."""

    install = staff_install
    staff = _identity_setup(install, operation)
    command = _identity_command(operation, staff, st.new_uuid())
    first = st.call(install, w3.ADMIN_V2, command)
    assert first["outcome"] == "applied"
    before, shared_before = st.state(install), _shared(install)
    replay = st.call(install, w3.ADMIN_V2, command)
    assert replay == {**first, "outcome": "replayed"}
    st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)
    assert _shared(install) == shared_before


@pytest.mark.parametrize("operation", ["bind_identity", "unbind_identity", "set_auth_setting"])
def test_ri07_retry_after_the_actor_lost_authority_is_refused(staff_install, operation):
    """Current authority is decided before replay: once the actor's clinic_admin role
    row is gone, the same command is NOT_AUTHORIZED and writes nothing."""

    install = staff_install
    staff = _identity_setup(install, operation)
    command = _identity_command(operation, staff, st.new_uuid())
    assert st.call(install, w3.ADMIN_V2, command)["outcome"] == "applied"
    w3.drop_role(install, st.ADMIN, "clinic_admin")  # FIXTURE: authority removed
    before, shared_before = st.state(install), _shared(install)
    st.expect_code("NOT_AUTHORIZED", lambda: st.call(install, w3.ADMIN_V2, command))
    st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)
    assert _shared(install) == shared_before


def test_ri03_a_stored_result_with_outcome_is_unrepresentable(staff_install):
    """Codex 170024Z: "outcome" is never stored. The log table's closed-result CHECK
    refuses a five-key result, so the replay path can only ever see four keys."""

    install = staff_install
    staff = _setup(install)
    key = st.new_uuid()
    _crafted(install, "activate", staff, key)
    schema = sql.Identifier(install.schema)
    with pytest.raises(psycopg.errors.CheckViolation):
        st.admin_rows(install, sql.SQL(
            "INSERT INTO {}.m01_staff_admin_log SELECT gen_random_uuid(), %s, staff_id,"
            " operation, before, after, result || '{{\"outcome\": \"applied\"}}'::jsonb,"
            " actor_id, execution_id, request_id, purpose_code, digest, now()"
            " FROM {}.m01_staff_admin_log WHERE client_action_id = %s").format(schema, schema),
            (st.new_uuid(), key))


@pytest.mark.parametrize(("operation", "role"), [
    ("grant_role", "supervisor"), ("revoke_role", "supervisor"), ("activate", None),
])
def test_ri01_real_v2_operation_replays_with_outcome_replayed(staff_install, operation, role):
    install = staff_install
    staff = _setup(install)
    if operation == "revoke_role":
        w3.seed_role(install, staff, "supervisor")  # FIXTURE SETUP
    if operation == "activate":
        st.admin_op(install, st.admin_command("deactivate", staff))
    key = st.new_uuid()
    command = _command(operation, staff, key, role)
    first = st.call(install, w3.ADMIN_V2, command)
    assert first["outcome"] == "applied"
    before = st.state(install)
    replay = st.call(install, w3.ADMIN_V2, command)
    assert replay == {**first, "outcome": "replayed"}
    st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)


@pytest.mark.parametrize(("operation", "role"), [("grant_role", "supervisor"),
                                                  ("activate", None)])
def test_ri02_impossible_timestamp_never_replays(staff_install, operation, role):
    install = staff_install
    staff = _setup(install)
    key = st.new_uuid()
    _crafted(install, operation, staff, key, role=role, timestamp="2026-02-30T09:30:00.000000Z")
    before = st.state(install)
    st.expect_code("ADMIN_LOG_INTEGRITY", lambda: st.call(
        install, w3.ADMIN_V2, _command(operation, staff, key, role)))
    st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)


def test_ri04_activate_with_inactive_snapshot_never_replays(staff_install):
    install = staff_install
    staff = _setup(install)
    key = st.new_uuid()
    _crafted(install, "activate", staff, key, status="inactive")
    before = st.state(install)
    st.expect_code("ADMIN_LOG_INTEGRITY", lambda: st.call(
        install, w3.ADMIN_V2, _command("activate", staff, key, None)))
    st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)


@pytest.mark.parametrize(("operation", "role"), [("grant_role", "supervisor"),
                                                  ("activate", None)])
def test_ri05_crafted_valid_history_replays(staff_install, operation, role):
    install = staff_install
    staff = _setup(install)
    key = st.new_uuid()
    _crafted(install, operation, staff, key, role=role)
    before = st.state(install)
    replay = st.call(install, w3.ADMIN_V2, _command(operation, staff, key, role))
    assert replay["outcome"] == "replayed"
    assert {k: v for k, v in replay.items() if k != "outcome"}["staff"]["status_changed_at"] \
        == TS_OK
    st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)
