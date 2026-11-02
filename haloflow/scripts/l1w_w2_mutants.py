"""L-1W W2 security mutation controls for alembic 008 (Codex red gate ad6cc0c:
exact-head green CI plus source and mutation review).

Each mutant removes or weakens one check in ``alembic/versions/008_l1w_identity.py``
and is judged by the shared part-A checker (``scripts/m02_observation_mutants.py``):
the control run must pass completely; a mutant is KILLED only by a completed run whose
every failure is in its PREDECLARED kill set (no error, no skip, the control's node
set); the target is restored after every run and its SHA-256 re-checked. No judging
logic is added here.

008 is a migration, so every run (control and each mutant) gets its own fresh
database in the disposable cluster: the runner below creates
``haloflow_test_l1w_mut_<label>``, points ``HALOFLOW_TEST_DATABASE_URL`` at it for that
pytest subprocess only, and drops it afterwards. Roles are cluster-global and are
created by the control's migration; the preflight mutants (PM-*) therefore act on
the role checks 008 performs against those existing roles.

Kill sets were measured locally (PostgreSQL 16 scaffolding) before CI and are
confirmed by the PG17 CI run; a CI difference is reported, never edited away.

Usage (from haloflow/, with HALOFLOW_TEST_DATABASE_URL and
HALOFLOW_TEST_DISPOSABLE_CLUSTER=1 set):
    python3 -I scripts/l1w_w2_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/l1w-w2-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TARGET = Path("alembic/versions/008_l1w_identity.py")
TESTS = "tests/platform"
TITLE_FROM = "m02-observation-mutants"

# (id, description, old, new). Every replacement keeps 008 valid SQL, so the
# migration itself runs and the tests, not a broken migration, decide.
SPECS: tuple[tuple[str, str, str, str], ...] = (
    # ---- l1w_admit ---------------------------------------------------------------
    ("AM-ANCHOR", "admission takes no anchor lock",
     "        WHERE a.issuer = p_issuer AND a.subject = p_subject FOR SHARE;\n"
     "    IF NOT FOUND THEN\n        RETURN QUERY",
     "        WHERE a.issuer = p_issuer AND a.subject = p_subject;\n"
     "    IF NOT FOUND THEN\n        RETURN QUERY"),
    ("AM-BINDING", "revoked bindings still admit",
     "        WHERE b.issuer = p_issuer AND b.subject = p_subject AND b.revoked_at IS NULL;\n"
     "    IF NOT FOUND THEN\n        RETURN QUERY",
     "        WHERE b.issuer = p_issuer AND b.subject = p_subject\n"
     "        ORDER BY b.created_at DESC LIMIT 1;\n"
     "    IF NOT FOUND THEN\n        RETURN QUERY"),
    ("AM-FUTURE", "pre-insert future auth_time gate removed",
     "    IF p_auth_time > v_t + c_skew THEN\n"
     "        RETURN QUERY SELECT 'AUTH_TIME_FUTURE'::text, NULL::varchar(64), NULL::uuid;\n",
     "    IF false THEN\n"
     "        RETURN QUERY SELECT 'AUTH_TIME_FUTURE'::text, NULL::varchar(64), NULL::uuid;\n"),
    ("AM-ABSPRE", "a row is created even past the 12 h limit",
     "    IF v_t < p_auth_time + c_max_session THEN\n",
     "    IF true THEN\n"),
    ("AM-CLOCK", "decision uses the pre-lock clock",
     "    IF NOT FOUND THEN\n"
     "        RETURN QUERY SELECT 'ABSOLUTE'::text, NULL::varchar(64), NULL::uuid;\n"
     "        RETURN;\n    END IF;\n    v_t := clock_timestamp();\n",
     "    IF NOT FOUND THEN\n"
     "        RETURN QUERY SELECT 'ABSOLUTE'::text, NULL::varchar(64), NULL::uuid;\n"
     "        RETURN;\n    END IF;\n"),
    ("AM-REVOKED", "revoked session admitted",
     "    IF s.revoked_at IS NOT NULL THEN\n"
     "        RETURN QUERY SELECT 'REVOKED'",
     "    IF false THEN\n"
     "        RETURN QUERY SELECT 'REVOKED'"),
    ("AM-MISMATCH", "binding mismatch not detected",
     "    IF s.tenant_id <> v_tenant OR s.staff_id <> v_staff THEN\n",
     "    IF false THEN\n"),
    ("AM-SID", "sid reuse with another auth_time admitted",
     "    IF s.auth_time <> p_auth_time THEN\n",
     "    IF false THEN\n"),
    ("AM-ABS", "absolute lifetime not enforced on existing rows",
     "    IF v_t >= s.auth_time + c_max_session THEN\n",
     "    IF false THEN\n"),
    ("AM-IDLE", "idle expiry not enforced",
     "    IF v_t - s.last_activity_at >= make_interval(mins => v_idle) THEN\n",
     "    IF false THEN\n"),
    ("AM-IDLEDEF", "missing settings row defaults to 60 min, not 15",
     "    v_idle := coalesce(v_idle, 15);\n",
     "    v_idle := coalesce(v_idle, 60);\n"),
    ("AM-MFANULL", "NULL assurance passes MFA",
     "    IF v_mfa AND p_assured IS NOT TRUE THEN\n",
     "    IF v_mfa AND NOT p_assured THEN\n"),
    ("AM-COALESCE", "activity written on every request",
     "    IF v_t - s.last_activity_at >= c_coalesce THEN\n",
     "    IF true THEN\n"),
    ("AM-INPUT", "out-of-length sid accepted",
     "    IF {_IDENTITY_INVALID} OR p_sid IS NULL OR length(p_sid) NOT BETWEEN 1 AND 255\n"
     "       OR p_auth_time IS NULL THEN\n"
     "        RETURN QUERY SELECT 'INVALID_INPUT'",
     "    IF {_IDENTITY_INVALID} OR p_sid IS NULL\n"
     "       OR p_auth_time IS NULL THEN\n"
     "        RETURN QUERY SELECT 'INVALID_INPUT'"),
    # ---- l1w_logout --------------------------------------------------------------
    ("LM-TOMBSTONE", "logout of an absent sid leaves no tombstone",
     "    VALUES (p_issuer, p_subject, p_sid, v_tenant, v_staff, p_auth_time, v_t, v_t, v_t, 'logout')\n",  # noqa: E501
     "    SELECT p_issuer, p_subject, p_sid, v_tenant, v_staff, p_auth_time, v_t, v_t, v_t,"
     " 'logout' WHERE false\n"),
    # ---- writers -----------------------------------------------------------------
    ("WM-REREAD", "unbind re-read does not check the locked anchor",
     "        WHERE b.tenant_id = p_tenant AND b.staff_id = p_staff AND b.revoked_at IS NULL\n"
     "          AND b.issuer = v_issuer AND b.subject = v_subject;\n",
     "        WHERE b.tenant_id = p_tenant AND b.staff_id = p_staff AND b.revoked_at IS NULL;\n"),
    ("WM-SESSIONS", "unbind leaves sessions active",
     "        WHERE x.issuer = v_issuer AND x.subject = v_subject AND x.revoked_at IS NULL;\n",
     "        WHERE false;\n"),
    ("WM-POST", "support authority not re-checked after the locks",
     "        post = _SUPPORT_GRANT.format(capability=capability, refuse=\"'SUPPORT_NOT_AUTHORIZED'\")\n",  # noqa: E501
     "        post = \"\"\n"),
    ("WM-LOGINNAME", "support login matched by OID only (rename not detected)",
     "        WHERE l.login_name = session_user\n"
     "          AND l.login_oid = (SELECT r.oid FROM pg_catalog.pg_roles r\n"
     "                             WHERE r.rolname = session_user);\n"
     "    IF NOT FOUND THEN\n        RETURN {refuse};",
     "        WHERE l.login_oid = (SELECT r.oid FROM pg_catalog.pg_roles r\n"
     "                             WHERE r.rolname = session_user);\n"
     "    IF NOT FOUND THEN\n        RETURN {refuse};"),
    ("WM-CAP", "support capability not required",
     "          AND '{capability}' = ANY (g.capabilities)\n",
     "          AND cardinality(g.capabilities) >= 0\n"),
    ("WM-SETROW", "first setters not serialized on a materialized default row",
     "        VALUES (p_tenant, 15, false, clock_timestamp(), c_kind, v_actor)\n"
     "        ON CONFLICT (tenant_id) DO NOTHING;\n",
     "        SELECT p_tenant, 15, false, clock_timestamp(), c_kind, v_actor WHERE false\n"
     "        ON CONFLICT (tenant_id) DO NOTHING;\n"),
    ("WM-BOUNDS", "idle bounds widened to 1..99",
     "        IF p_value::integer NOT BETWEEN 5 AND 60 THEN\n",
     "        IF p_value::integer NOT BETWEEN 1 AND 99 THEN\n"),
    ("WM-IDBOUND", "identity-already-bound check removed (unique index answers instead)",
     "        IF EXISTS (SELECT 1 FROM shared.staff_identity_bindings b WHERE b.issuer = p_issuer\n"
     "                   AND b.subject = p_subject AND b.revoked_at IS NULL) THEN\n",
     "        IF false THEN\n"),
    # ---- session triggers --------------------------------------------------------
    ("TM-SID", "sid no longer immutable",
     "       OR NEW.sid IS DISTINCT FROM OLD.sid OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n",
     "       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n"),
    ("TM-FINAL", "revoked session can be changed",
     "        IF NEW IS DISTINCT FROM OLD THEN\n"
     "            RAISE EXCEPTION 'revoked staff session is final';\n",
     "        IF false THEN\n"
     "            RAISE EXCEPTION 'revoked staff session is final';\n"),
    ("TM-INSERT", "rows may be created revoked for any reason",
     "    IF NEW.revoked_at IS NOT NULL AND NEW.revoke_reason IS DISTINCT FROM 'logout' THEN\n",
     "    IF false THEN\n"),
    ("TM-DELETE", "session rows can be deleted",
     "    RAISE EXCEPTION 'staff sessions are retained';\n",
     "    RETURN OLD;\n"),
    # ---- grants ------------------------------------------------------------------
    ("GM-PUBLIC", "definers keep the default PUBLIC EXECUTE",
     "    + [f\"REVOKE ALL ON FUNCTION {f} FROM PUBLIC;\" for f in _FUNCTIONS]\n",
     ""),
    ("GM-COLUMNS", "identity owner reads an extra grant column",
     "        \"GRANT SELECT (grant_id, actor_id, tenant_id, ticket_reference, capabilities, state,\"\n",  # noqa: E501
     "        \"GRANT SELECT (grant_id, actor_id, tenant_id, ticket_reference, capabilities, state,\"\n"  # noqa: E501
     "        \" purpose_code,\"\n"),
    # ---- preflight ---------------------------------------------------------------
    ("PM-ATTR", "BYPASSRLS not checked",
     "                        OR r.rolreplication OR r.rolbypassrls)) THEN\n",
     "                        OR r.rolreplication)) THEN\n"),
    ("PM-OUT", "outgoing membership not checked",
     "        IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members a\n"
     "                   JOIN pg_catalog.pg_roles m ON m.oid = a.member\n"
     "                   WHERE m.rolname = v_role) THEN\n",
     "        IF false THEN\n"),
    ("PM-OWNERIN", "identity owner incoming members not checked",
     "               WHERE r.rolname = '{IDENTITY_OWNER}' AND m.rolname <> 'haloflow_migrator') THEN\n",  # noqa: E501
     "               WHERE false) THEN\n"),
    ("PM-SUPPORTIN", "support group-role members accepted",
     "                 AND (NOT m.rolcanlogin OR m.rolsuper OR a.admin_option)) THEN\n",
     "                 AND (m.rolsuper OR a.admin_option)) THEN\n"),
    ("PM-EDGE", "migrator edge options not checked",
     "                     AND (a.inherit_option OR NOT a.set_option OR a.admin_option)) THEN\n",
     "                     AND false) THEN\n"),
    ("PM-ORDER", "preflight runs after the role grants",
     "    op.execute(PREFLIGHT_SQL)\n    op.execute(ROLES_SQL)\n",
     "    op.execute(ROLES_SQL)\n    op.execute(PREFLIGHT_SQL)\n"),
)


# Predeclared kill sets: mutant id -> exact failing nodes, measured before CI.
KILLS: dict[str, frozenset[str]] = {}


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fresh_database_runner(checker: Any) -> Any:
    """Wrap the checker's runner: one fresh database per run, dropped afterwards."""

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = os.environ["HALOFLOW_TEST_DATABASE_URL"]
    if os.environ.get("HALOFLOW_TEST_DISPOSABLE_CLUSTER") != "1":
        raise SystemExit("HALOFLOW_TEST_DISPOSABLE_CLUSTER=1 is required")
    admin = conninfo_to_dict(base)

    def runner(pytest: str, label: str, evidence: Path) -> Any:
        name = "haloflow_test_l1w_mut_" + "".join(
            ch if ch.isalnum() else "_" for ch in label.lower())
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        url = "postgresql:///?" + "&".join(
            f"{k}={v}" for k, v in sorted({**admin, "dbname": name}.items()) if v)
        previous = os.environ["HALOFLOW_TEST_DATABASE_URL"]
        os.environ["HALOFLOW_TEST_DATABASE_URL"] = make_conninfo(url)
        try:
            return checker.run_pytest(pytest, label, evidence)
        finally:
            os.environ["HALOFLOW_TEST_DATABASE_URL"] = previous
            with psycopg.connect(base, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)))

    return runner


def build_mutants(checker: Any) -> tuple[Any, ...]:
    kills = KILLS
    return tuple(
        checker.Mutant(mid, desc, old, new, kills.get(mid, frozenset()))
        for mid, desc, old, new in SPECS
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    checker = _load_checker()
    checker.TARGET = TARGET
    checker.TESTS = TESTS
    checker.MUTANTS = build_mutants(checker)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = int(checker.main(["--pytest", args.pytest, "--evidence", args.evidence,
                                 "--target", str(TARGET)],
                                runner=_fresh_database_runner(checker)))
    sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}", "title=l1w-w2-mutants"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
