"""L-1W W3a implementation-time PostgreSQL rows (Codex 224307Z, support profile (d)).

Rows:
- SUP-04: an authorized support login's membership in haloflow_support survives the
  provisioning of a tenant under the final manifest (support is now a controlled
  profile; its logins are uncontrolled and untouched), and that login holds the
  tenant schema's USAGE without CREATE.
- GB-01: m01_staff_roles.granted_by fits 'support:' plus a 128-character actor id
  (136 characters) and refuses 137; the empty value is refused.

Support logins are synthetic, token-named and exist only in the disposable cluster
(tests/platform/l1w_db_support.py guard). Role rows inserted here are FIXTURE SETUP.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import l1w_w3a_support as w3
import m05_support as m05
import pytest
import staff_support as st
from psycopg import errors, sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        yield handle


def test_sup04_support_login_membership_survives_provisioning(
    env: db.L1WEnv, staff_foreign_install: st.StaffInstall
) -> None:
    login = env.login("sup04", db.SUPPORT)
    member_sql = (
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid"
        " JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = %s AND u.rolname = %s")
    with env.connect("admin", autocommit=True) as conn:
        assert conn.execute(member_sql, (db.SUPPORT, login)).fetchone() == (1,)
    # Provision a fresh tenant under the final manifest (support = USAGE-only profile).
    w3.ready_with_staff(staff_foreign_install)
    with env.connect("admin", autocommit=True) as conn:
        assert conn.execute(member_sql, (db.SUPPORT, login)).fetchone() == (1,)
        assert conn.execute(
            "SELECT has_schema_privilege(%s, %s, 'USAGE'), has_schema_privilege(%s, %s, 'CREATE')",
            (login, staff_foreign_install.schema, login, staff_foreign_install.schema),
        ).fetchone() == (True, False)


@pytest.mark.parametrize(("length", "accepted"), [(136, True), (137, False), (0, False)])
def test_gb01_granted_by_length_boundary(
    staff_install: st.StaffInstall, length: int, accepted: bool
) -> None:
    w3.ready_with_staff(staff_install)
    w3.drop_role(staff_install, m05.S1, "supervisor")  # FIXTURE SETUP: clean slate
    granted_by = ("support:" + "a" * 129)[:length]  # 136 = support: + 128-char actor
    insert = sql.SQL(
        "INSERT INTO {}.m01_staff_roles (staff_id, role, granted_by, granted_at)"
        " VALUES (%s, 'supervisor', %s, clock_timestamp())"
    ).format(sql.Identifier(staff_install.schema))
    try:
        if accepted:
            st.admin_rows(staff_install, insert, (m05.S1, granted_by))  # FIXTURE SETUP
            assert (m05.S1, "supervisor") in w3.roles(staff_install)
        else:
            expected = errors.StringDataRightTruncation if length else errors.CheckViolation
            with pytest.raises(expected):
                st.admin_rows(staff_install, insert, (m05.S1, granted_by))
            assert (m05.S1, "supervisor") not in w3.roles(staff_install)
    finally:
        w3.drop_role(staff_install, m05.S1, "supervisor")  # FIXTURE SETUP
