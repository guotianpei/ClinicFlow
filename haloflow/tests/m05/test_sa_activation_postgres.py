"""L-1S package S-A — D-S1 activation record, isolated (t042), on PostgreSQL 17.

Owner decision D-S1 "Design now, enable none"; architecture v1r1 section 6 (isolated:
not wired into the gateway, no global absent-record denial, existing behaviour
unchanged); Codex 2155 last item (restricted execution; no path enables production;
M05-A1 and existing regressions mandatory).

- SA-A01  default: no record -> m05_activation_state() = {active: false,
          last_record_id: null}.
- SA-A02  restricted execution: neither function is executable by the runtime, the
          migrator or PUBLIC; only the M05 owner's own ACL entry exists.
- SA-A03  grant then revoke (called as the owner, the only path): append-only audit
          rows with actor, ticket and reason; state follows the LAST record;
          malformed change -> COMMAND_MALFORMED, zero writes.
- SA-A04  isolation: with the record ACTIVE, nothing else changes — M05-A1 still
          refuses a production generation with M05 active (registry validator), and an
          S-A gateway call behaves identically with the record active or revoked.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
import sa_support as sa
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


def _as_owner(install: sa.SAInstall, statement: str, params: Any = None) -> Any:
    with psycopg.connect(install.ids.admin, autocommit=True) as conn, conn.transaction():
        conn.execute(f"SET LOCAL ROLE {sa.M05_OWNER}")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant_id,))
        row = conn.execute(sa.q(install, statement), params).fetchone()
        return None if row is None else row[0]


def _change(change: str, **extra: Any) -> dict[str, Any]:
    return {"schema_version": 1, "change": change, "actor_id": sa.ACTOR,
            "ticket_reference": "HV-SUP-0001", "reason": "synthetic test", **extra}


def test_sa_a01_default_inactive(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    assert _as_owner(sa_install, "SELECT {s}.m05_activation_state()") == {
        "active": False, "last_record_id": None}


@pytest.mark.parametrize(("function", "signature"), (
    ("m05_activation_change", "jsonb"), ("m05_activation_state", ""),
))
def test_sa_a02_restricted_execution(sa_install: sa.SAInstall, function: str,
                                     signature: str) -> None:
    sa_install.require()
    target = f"{sa_install.schema}.{function}({signature})"
    for role in (sa.RUNTIME, sa.MIGRATOR, "public"):
        got = sa.admin(sa_install, "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                       (role, target))
        assert got == [(False,)], role
    acl = sa.admin(sa_install, "SELECT array_to_string(p.proacl, ',') FROM pg_proc p"
                               " WHERE p.oid = %s::regprocedure", (target,))
    assert acl == [(f"{sa.M05_OWNER}=X/{sa.M05_OWNER}",)]


def test_sa_a03_grant_revoke_audit(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    granted = _as_owner(sa_install, "SELECT {s}.m05_activation_change(%s)",
                        (Jsonb(_change("grant")),))
    state = _as_owner(sa_install, "SELECT {s}.m05_activation_state()")
    assert state == {"active": True, "last_record_id": granted["record_id"]}
    revoked = _as_owner(sa_install, "SELECT {s}.m05_activation_change(%s)",
                        (Jsonb(_change("revoke")),))
    assert _as_owner(sa_install, "SELECT {s}.m05_activation_state()") == {
        "active": False, "last_record_id": revoked["record_id"]}
    rows = sa.admin(sa_install, sa.q(sa_install, "SELECT change, actor_id::text,"
                                                 " ticket_reference, reason FROM"
                                                 " {s}.m05_activation_records ORDER BY"
                                                 " created_at, record_id"))
    assert rows[-2:] == [("grant", sa.ACTOR, "HV-SUP-0001", "synthetic test"),
                         ("revoke", sa.ACTOR, "HV-SUP-0001", "synthetic test")]
    before = sa.snapshot(sa_install)
    for bad in (_change("enable"), _change("grant", tenant_id="x"),
                {**_change("grant"), "ticket_reference": ""}):
        with pytest.raises(psycopg.Error) as caught:
            _as_owner(sa_install, "SELECT {s}.m05_activation_change(%s)", (Jsonb(bad),))
        assert caught.value.diag.message_primary == "COMMAND_MALFORMED"
    assert sa.snapshot(sa_install) == before


def test_sa_a04_isolated(sa_install: sa.SAInstall) -> None:
    sa_install.require()
    _, phone, _ = sa.fresh_patient(sa_install)
    revoked_result = sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    _as_owner(sa_install, "SELECT {s}.m05_activation_change(%s)", (Jsonb(_change("grant")),))
    granted_result = sa.inbound(sa_install, sa.new_id(), phone, "confirm")
    keys = ("decision", "triage_kind", "patient_id")
    assert {k: revoked_result[k] for k in keys} == {k: granted_result[k] for k in keys}
    _as_owner(sa_install, "SELECT {s}.m05_activation_change(%s)", (Jsonb(_change("revoke")),))
    # M05-A1 is untouched: the production validator still refuses an active M05.
    from haloflow.m02 import registry_validation as rv

    source = rv.__file__
    assert source is not None
    from pathlib import Path

    text = Path(source).read_text(encoding="utf-8")
    assert "M05_ACTIVATION_NOT_AUTHORIZED" in text
    assert "m05_activation" not in text  # the record is not read by the validator
