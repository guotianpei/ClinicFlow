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
    "$VENV/bin/python" -I scripts/l1w_w2_mutants.py --pytest "$VENV/bin/pytest" \\
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


# Predeclared kill sets: mutant id -> exact failing nodes, measured before CI on the
# a72d833 tree (local PostgreSQL 16 scaffolding, fresh database per run; control 243
# cases, restored). CI on PostgreSQL 17 confirms; a difference is reported, not edited.
KILLS: dict[str, frozenset[str]] = {
    "AM-ANCHOR": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b08_unbind_rereads_after_the_anchor_lock_and_never_revokes_another_binding",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp08_grant_revoked_while_waiting_on_the_anchor_is_not_used",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_b07d_rebind_and_admission_serialize_on_the_anchor",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t5_admission_waits_for_unbind_then_is_unbound_without_a_row",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t6_unbind_waits_for_an_inflight_admission_then_both_sids_unbound",  # noqa: E501
    }),
    "AM-BINDING": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b02_unbind_then_rebind_creates_a_new_binding_and_keeps_the_anchor",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_s10_unbind_revokes_every_active_session_of_the_identity",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t5_admission_waits_for_unbind_then_is_unbound_without_a_row",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t6_unbind_waits_for_an_inflight_admission_then_both_sids_unbound",  # noqa: E501
    }),
    "AM-FUTURE": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s07_auth_time_inside_skew_allowed_beyond_skew_refused",  # noqa: E501
    }),
    "AM-ABSPRE": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s08_absolute_lifetime_existing_row_revoked_absent_row_not_created",  # noqa: E501
    }),
    "AM-CLOCK": frozenset({
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s13_t8_idle_decided_with_the_post_wait_clock",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s14_absolute_decided_with_the_post_wait_clock",  # noqa: E501
    }),
    "AM-REVOKED": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b02_unbind_then_rebind_creates_a_new_binding_and_keeps_the_anchor",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b07c_rebind_to_other_staff_old_sid_revoked_new_sid_admitted",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_lg01_logout_of_a_never_admitted_sid_leaves_a_tombstone",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s01_logout_is_per_device",
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s02_refreshed_token_cannot_revive_a_revoked_sid",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s04_idle_expiry_is_revoked_and_persisted",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t3_logout_waits_for_an_inflight_admission_then_revokes",  # noqa: E501
    }),
    "AM-MISMATCH": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b07c_unrevoked_row_for_another_binding_is_binding_mismatch_and_revoked",  # noqa: E501
    }),
    "AM-SID": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s03_same_sid_other_auth_time_is_sid_mismatch_and_row_unchanged",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t1_concurrent_create_other_auth_time_is_sid_mismatch",  # noqa: E501
    }),
    "AM-ABS": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s08_absolute_lifetime_existing_row_revoked_absent_row_not_created",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s14_absolute_decided_with_the_post_wait_clock",  # noqa: E501
    }),
    "AM-IDLE": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s04_idle_expiry_is_revoked_and_persisted",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a03_idle_change_applies_to_existing_sessions",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a07_new_tenant_uses_the_owner_defaults",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s13_t8_idle_decided_with_the_post_wait_clock",  # noqa: E501
    }),
    "AM-IDLEDEF": frozenset({
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a07_new_tenant_uses_the_owner_defaults",  # noqa: E501
    }),
    "AM-MFANULL": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_assured_with_mfa_on_is_mfa_required_and_off_is_admitted",  # noqa: E501
    }),
    "AM-COALESCE": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s06_activity_is_coalesced_to_one_write_per_60s",  # noqa: E501
    }),
    "AM-INPUT": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_x01_invalid_input_is_a_fixed_refusal_and_errors_carry_no_identifiers",  # noqa: E501
    }),
    "LM-TOMBSTONE": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_lg01_logout_of_a_never_admitted_sid_leaves_a_tombstone",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t3_logout_waits_for_an_inflight_admission_then_revokes",  # noqa: E501
    }),
    "WM-REREAD": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b08_unbind_rereads_after_the_anchor_lock_and_never_revokes_another_binding",  # noqa: E501
    }),
    "WM-SESSIONS": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b02_unbind_then_rebind_creates_a_new_binding_and_keeps_the_anchor",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b07c_rebind_to_other_staff_old_sid_revoked_new_sid_admitted",  # noqa: E501
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_s10_unbind_revokes_every_active_session_of_the_identity",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_b07d_rebind_and_admission_serialize_on_the_anchor",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t6_unbind_waits_for_an_inflight_admission_then_both_sids_unbound",  # noqa: E501
    }),
    "WM-POST": frozenset({
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_set03_support_grant_revoked_while_waiting_on_the_first_row_is_refused",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp08_grant_revoked_while_waiting_on_the_anchor_is_not_used",  # noqa: E501
    }),
    "WM-LOGINNAME": frozenset({
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp02_renamed_login_no_longer_matches_the_allowlist",  # noqa: E501
    }),
    "WM-CAP": frozenset({
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_capabilities_are_separate",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_grant_must_be_active_current_for_the_tenant_and_capable[missing-capability]",  # noqa: E501
    }),
    "WM-SETROW": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_n01_null_assured_with_mfa_on_is_mfa_required_and_off_is_admitted",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s04_idle_expiry_is_revoked_and_persisted",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_logs_are_append_only[staff_identity_log]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_logs_are_append_only[tenant_auth_settings_log]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a01_mfa_on_refuses_unassured_without_revoking",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a03_idle_change_applies_to_existing_sessions",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_bounds_are_inclusive",
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a05_admin_and_support_changes_are_logged_with_attribution",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_set01_concurrent_first_setters_of_different_settings_keep_both_values",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_set02_concurrent_first_setters_of_one_setting_log_an_accurate_chain",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_set03_support_grant_revoked_while_waiting_on_the_first_row_is_refused",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_sp03_capabilities_are_separate",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_s13_t8_idle_decided_with_the_post_wait_clock",  # noqa: E501
        "tests/platform/test_l1w_w2_timelines_postgres.py::test_t7_waiting_admission_sees_the_refresh_made_by_the_holder",  # noqa: E501
    }),
    "WM-BOUNDS": frozenset({
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[idle_timeout_minutes-4-INVALID_VALUE]",  # noqa: E501
        "tests/platform/test_l1w_w2_settings_support_postgres.py::test_a04_out_of_bounds_or_unknown_setting_refused_without_writes[idle_timeout_minutes-61-INVALID_VALUE]",  # noqa: E501
    }),
    "WM-IDBOUND": frozenset({
        "tests/platform/test_l1w_w2_bindings_postgres.py::test_b03_one_active_binding_per_identity_and_per_staff",  # noqa: E501
    }),
    "TM-SID": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_forbidden_session_updates[sid]",  # noqa: E501
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_x01_invalid_input_is_a_fixed_refusal_and_errors_carry_no_identifiers",  # noqa: E501
    }),
    "TM-FINAL": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_allows_the_single_revocation_and_then_freezes_it",  # noqa: E501
    }),
    "TM-INSERT": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_delete_and_non_logout_revoked_insert",  # noqa: E501
    }),
    "TM-DELETE": frozenset({
        "tests/platform/test_l1w_w2_sessions_postgres.py::test_s12_trigger_refuses_delete_and_non_logout_revoked_insert",  # noqa: E501
    }),
    "GM-PUBLIC": frozenset({
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_priv01_function_execute_grantees_are_exact",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[runtime-l1w_admin_bind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[runtime-l1w_support_unbind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[staff_owner-l1w_admit]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[staff_owner-l1w_support_bind]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[support-l1w_admin_set_setting]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp05_sp07a_cross_role_calls_are_permission_denied[support-l1w_admit]",  # noqa: E501
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_sp07a_runtime_has_no_execute_on_admin_or_support_definers",  # noqa: E501
    }),
    "GM-COLUMNS": frozenset({
        "tests/platform/test_l1w_w2_inventory_postgres.py::test_priv01_identity_owner_reads_exactly_eight_grant_columns",  # noqa: E501
    }),
    "PM-ATTR": frozenset({
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_identity_owner-BYPASSRLS]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf02_unsafe_attribute_refused_before_any_mutation[haloflow_support-BYPASSRLS]",  # noqa: E501
    }),
    "PM-OUT": frozenset({
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf04_extra_outgoing_membership_refused[haloflow_identity_owner]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf04_extra_outgoing_membership_refused[haloflow_support]",  # noqa: E501
    }),
    "PM-OWNERIN": frozenset({
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_extra_incoming_membership_refused[haloflow_identity_owner-group-role]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_extra_incoming_membership_refused[haloflow_identity_owner-superuser-login]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_identity_owner_refuses_even_a_plain_login_member",  # noqa: E501
    }),
    "PM-SUPPORTIN": frozenset({
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf03_extra_incoming_membership_refused[haloflow_support-group-role]",  # noqa: E501
    }),
    "PM-EDGE": frozenset({
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf05_migrator_edge_with_other_options_refused[haloflow_identity_owner]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf05_migrator_edge_with_other_options_refused[haloflow_support]",  # noqa: E501
    }),
    "PM-ORDER": frozenset({
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf05_migrator_edge_with_other_options_refused[haloflow_identity_owner]",  # noqa: E501
        "tests/platform/test_l1w_w2_preflight_postgres.py::test_pf05_migrator_edge_with_other_options_refused[haloflow_support]",  # noqa: E501
    }),
}

# L-1W W4a (Codex 366f7e94 / b0bd5d3e, option B): ADDITIVE kill-set entries. TESTS is the
# whole tests/platform directory, and the W4a admission and HTTP rows also detect these
# alembic-008 mutants. Every entry above is unchanged; each node below was measured with
# this harness (PG16 calibration, then PG17 CI) and maps to one causal assertion, listed in
# Work Session 2026-11-04/claude_w2-killset-extension-causal-map-2026-11-04.md.
# WM-SETROW x ADMIT-MAP is settings-operation detection (the explicit l1w_admin_set_setting
# call fails on the mutated default-row materialization), not MFA-admission coverage.
W4A_ADDED_KILLS: dict[str, frozenset[str]] = {
    "AM-REVOKED": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_lo01_adapter_logout_revokes_only_that_session",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_i2_http_logout_waits_behind_inflight_admission_then_applies",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_i2_http_requests_admitted_before_logout_complete_later_ones_refused",  # noqa: E501
        "tests/platform/test_l1w_w4a_app_postgres.py::test_lo01_http_logout_is_per_device",
    }),
    "AM-MISMATCH": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_persist_confirmed_refusal_revocation_is_durable[BINDING_MISMATCH]",  # noqa: E501
    }),
    "AM-SID": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
    }),
    "AM-ABS": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_persist_confirmed_refusal_revocation_is_durable[ABSOLUTE]",  # noqa: E501
    }),
    "AM-IDLE": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_persist_confirmed_refusal_revocation_is_durable[IDLE]",  # noqa: E501
    }),
    "AM-IDLEDEF": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_acf4_committed_but_ack_lost_on_idle_is_unavailable_revocation_persisted",  # noqa: E501
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_persist_confirmed_refusal_revocation_is_durable[IDLE]",  # noqa: E501
    }),
    "AM-INPUT": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
    }),
    "WM-SETROW": frozenset({
        "tests/platform/test_l1w_w4a_admission_postgres.py::test_admit_map_every_refusal_outcome",
    }),
}
for _mid, _nodes in W4A_ADDED_KILLS.items():
    assert _nodes.isdisjoint(KILLS[_mid]), _mid  # strictly additive
    KILLS[_mid] = KILLS[_mid] | _nodes


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
