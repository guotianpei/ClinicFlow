"""L-1W W3b targeted mutation controls (plan v1r1 §6; Codex 170024Z / 162405Z).

Five groups, each judged by the shared part-A checker
(``scripts/m02_observation_mutants.py``, no judging logic added here):

* ``first-member`` -- ``src/haloflow/m01/staff/l1w_w3b_units.py`` (t034), against the
  first-member suite.
* ``closure-units`` / ``closure-runner`` -- ``src/haloflow/m01/provisioning/units.py``
  and ``runner.py`` (the ``acl_closure_v1`` kind), against the closure suites.
* ``t029`` -- ``src/haloflow/m01/staff/l1w_units.py``: the R1 replay-integrity port
  (Codex 170024Z) and R-M2 on t029 (the v2 equivalent of the historical v1 row),
  against the RI suite and the assignee-eligibility suite.
* ``m05-v2`` -- ``src/haloflow/m05/gateways_v2.py``: GM-1/GM-2/SM-1/SM-2 on t030/t031
  (the v2 equivalents of the historical v1 rows, which now run v1 as its owner under
  the H-c ruling), against the assignee-eligibility suite.

A mutant is KILLED only by a completed run whose every failure is in its
PREDECLARED kill set (no error, no skip, the control's node set); each target is
restored after every run and its SHA-256 re-checked. Typed bodies carry reviewed
SHA-256 literals; after the checker writes a mutant the runner re-synchronises ONLY
the mutated target's literals (``l1w_w3a_mutants.sync_sha_literals``), so the mutated
SQL is installed and the tests decide. Every run gets its own fresh database in the
disposable cluster.

Usage (from haloflow/, HALOFLOW_TEST_DATABASE_URL and HALOFLOW_TEST_DISPOSABLE_CLUSTER=1
set):
    "$VENV/bin/python" -I scripts/l1w_w3b_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/l1w-w3b-mutation" [--group TAG ...]
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TITLE_FROM = "m02-observation-mutants"

FIRST_MEMBER_TARGET = Path("src/haloflow/m01/staff/l1w_w3b_units.py")
UNITS_TARGET = Path("src/haloflow/m01/provisioning/units.py")
RUNNER_TARGET = Path("src/haloflow/m01/provisioning/runner.py")
STAFF_TARGET = Path("src/haloflow/m01/staff/l1w_units.py")
M05_TARGET = Path("src/haloflow/m05/gateways_v2.py")

FM_FILE = "tests/m05/test_l1w_w3b_first_member_postgres.py"
CL_FILE = "tests/m05/test_l1w_w3b_closure_postgres.py"
CU_FILE = "tests/m01/test_l1w_w3b_acl_closure_units.py"
RI_FILE = "tests/m05/test_l1w_w3b_replay_integrity_postgres.py"
EL_FILE = "tests/m05/test_m05_assignee_eligibility_postgres.py"
DIR_FILE = "tests/m05/test_m05_staff_directory_postgres.py"

Spec = tuple[str, str, str, str]  # (id, description, old, new)

# --- first-member (t034) -------------------------------------------------------------

_KEY_INSERT = (
    "    INSERT INTO {schema}.m01_staff_bootstrap_keys (client_action_id, digest, actor_id,\n"
    "        staff_id, outcome, created_at, operation)\n"
    "    VALUES (v_key, v_digest, v_actor, v_staff, v_outcome, v_now, 'create_first_admin');\n"
)
_GRANT_RECHECK = (
    "    v_grant := NULL;\n"
    "    SELECT g.grant_id, g.ticket_reference INTO v_grant, v_ticket\n"
    "      FROM shared.support_access_grants AS g\n"
    "     WHERE g.actor_id = v_actor AND g.tenant_id = v_tenant AND g.state = 'active'\n"
    "       AND g.starts_at <= pg_catalog.clock_timestamp()\n"
    "       AND pg_catalog.clock_timestamp() < g.expires_at\n"
    "       AND 'l1w.roles.bootstrap' = ANY (g.capabilities)\n"
    "     ORDER BY g.expires_at DESC, g.grant_id\n"
    "     LIMIT 1;\n"
    "    IF v_grant IS NULL THEN\n"
)
_GRANT_RECHECK_KEEP = (
    "    SELECT g.ticket_reference INTO v_ticket\n"
    "      FROM shared.support_access_grants AS g\n"
    "     WHERE g.grant_id = v_grant;\n"
    "    IF false THEN\n"
)
_ROLE_VALUES = "            VALUES (v_staff, 'clinic_admin', 'support:' || v_actor, v_now);\n"

FIRST_MEMBER_SPECS: tuple[Spec, ...] = (
    ("F-EXISTS", "t034 ignores an existing active clinic_admin (no ADMIN_EXISTS)",
     "    IF EXISTS (SELECT 1\n                 FROM {schema}.m01_staff_roles AS r\n",
     "    IF false AND EXISTS (SELECT 1\n                 FROM {schema}.m01_staff_roles AS r\n"),
    ("F-GUARD", "t034 takes no tenant-local guard",
     "    PERFORM 1 FROM {schema}.m01_staff_role_guard AS g WHERE g.id FOR UPDATE;\n",
     ""),
    ("F-RECHECK-GRANT", "t034 does not re-check the grant after the guard",
     "     ORDER BY g.expires_at DESC, g.grant_id\n     LIMIT 1;\n    IF v_grant IS NULL THEN\n",
     "     ORDER BY g.expires_at DESC, g.grant_id\n     LIMIT 1;\n    IF false THEN\n"),
    # Codex 173009Z: F-RECHECK-GRANT is caught by the log's grant_id NOT NULL (an
    # error-contract change). This focused mutant skips ONLY the post-wait rejection
    # and keeps the grant selected before the wait (and its ticket) for the audit
    # insert, so every constraint holds and only forbidden success can kill it.
    ("F-RECHECK-GRANT-KEEP", "t034 skips the post-wait revoked-grant rejection, keeping the "
     "pre-wait grant for the audit row",
     _GRANT_RECHECK, _GRANT_RECHECK_KEEP),
    ("F-RECHECK-LOGIN","t034 does not re-check the login (OID and name) after the guard",
     "                            WHERE r.rolname = session_user);\n    IF NOT FOUND THEN\n",
     "                            WHERE r.rolname = session_user);\n    IF false THEN\n"),
    ("F-KEY", "t034 writes no action key on ADMIN_EXISTS",
     _KEY_INSERT,
     "    IF v_outcome <> 'ADMIN_EXISTS' THEN\n" + _KEY_INSERT + "    END IF;\n"),
    ("F-ACCESS", "t034 also grants follow-up access to the first member",
     _ROLE_VALUES,
     _ROLE_VALUES
     + "            INSERT INTO {schema}.m01_staff_followup_access (staff_id, access,"
       " granted_at)\n"
     + "            VALUES (v_staff, 'act', v_now);\n"),
    ("F-DIGEST", "t034 digests in t032's namespace",
     "'[\"m01.staff_bootstrap_first_admin/v1\",'",
     "'[\"m01.staff_bootstrap/v1\",'"),
)

# --- closure kind ----------------------------------------------------------------------

UNITS_SPECS: tuple[Spec, ...] = (
    ("C-TEMPLATE", "a closure definition may carry authored SQL",
     '        if self.kind == ACL_CLOSURE_KIND and self.template != "":\n',
     "        if False:\n"),
    ("C-OWNER", "an amendment may remove the owner's own entry",
     "                or grantee == profile.owner\n",
     "                or False\n"),
    ("C-CHECKSUM", "the closure checksum payload ignores the removals",
     '                    "removes": [list(entry) for entry in sorted(t.amendment.removes)],\n',
     '                    "removes": [],\n'),
)

RUNNER_SPECS: tuple[Spec, ...] = (
    ("C-VERIFY", "a closure unit is not verified against the amended profiles",
     "                    await self._verify_closure_targets(connection, unit, schema_key)\n",
     "                    pass\n"),
)

# --- t029 ------------------------------------------------------------------------------

_MEMBERSHIP_T029 = (
    "    IF v_actor_id = v_staff THEN\n"
    "        SELECT s.staff_id, s.status INTO v_row\n"
    "          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_staff FOR UPDATE;\n"
    "        v_found := FOUND;\n"
    "        v_actor_status := v_row.status;\n"
    "    ELSIF v_actor_id < v_staff THEN\n"
    "        SELECT s.status INTO v_actor_status\n"
    "          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_actor_id FOR SHARE;\n"
    "        SELECT s.staff_id, s.status INTO v_row\n"
    "          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_staff FOR UPDATE;\n"
    "        v_found := FOUND;\n"
    "    ELSE\n"
    "        SELECT s.staff_id, s.status INTO v_row\n"
    "          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_staff FOR UPDATE;\n"
)

STAFF_SPECS: tuple[Spec, ...] = (
    ("T029-TS", "t029 replay accepts an impossible stored timestamp (pre-R1)",
     "           OR NOT v_ts_ok\n",
     "           OR false\n"),
    ("T029-IMPLIED", "t029 replay of activate does not require an active snapshot (pre-R1)",
     "           OR (v_op IN ('add', 'activate')\n",
     "           OR (v_op IN ('add')\n"),
    ("R-M2-V2", "t029 takes no lock on the target's membership row (R-M2 on v2)",
     _MEMBERSHIP_T029,
     _MEMBERSHIP_T029.replace("WHERE s.staff_id = v_staff FOR UPDATE;",
                              "WHERE s.staff_id = v_staff;")),
)

# --- t029 against the directory suite: v2 equivalents of t023 AM-1/AM-2 (Codex 182053Z)

_T029_GUARD_REF = '    + _ISOLATION_GUARD\n    + r"""\n    -- 1c. Composition'
_T029_AUTHORITY = "    -- 6. Authority from the actor's current standing"
_GUARD_SQL = (
    "    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN\n"
    "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ISOLATION_UNSUPPORTED';\n"
    "    END IF;\n"
)


def _t029_guard_after_membership() -> tuple[str, str]:
    """(old, new) for AM-1-V2. _ISOLATION_GUARD is shared by t027, t028, t029, t032
    and t034, so the span runs from t029's own reference to the step-6 marker. It
    drops that reference and writes the same guard SQL after the step-5 membership
    locks. The marker lies inside an open raw string, so the source stays valid."""

    text = (HERE.parent / STAFF_TARGET).read_text(encoding="utf-8")
    start, end = text.index(_T029_GUARD_REF), text.index(_T029_AUTHORITY)
    old = text[start:end]
    new = old.replace("    + _ISOLATION_GUARD\n", "", 1) + _GUARD_SQL + "\n"
    return old, new


_AM1_OLD, _AM1_NEW = _t029_guard_after_membership()

STAFF_DIRECTORY_SPECS: tuple[Spec, ...] = (
    ("AM-1-V2", "t029 isolation guard moved after the membership locks (AM-1 on v2)",
     _AM1_OLD, _AM1_NEW),
    ("AM-2-V2", "t029 key admission without ON CONFLICT (AM-2 on v2)",
     "    ON CONFLICT (client_action_id) DO NOTHING\n"
     "    RETURNING client_action_id INTO v_admitted;\n",
     "    RETURNING client_action_id INTO v_admitted;\n"),
)

# --- M05 v2 (t030/t031) ------------------------------------------------------------------

_ISOLATION = (
    "\\n    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN\\n"
    "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ISOLATION_UNSUPPORTED';\\n"
    "    END IF;\\n"
)
_ITEM_LOCK = "     WHERE i.item_id = v_item\\n       FOR UPDATE;\\n"
_COMPOSITION = (
    "    v_prior := pg_catalog.current_setting('haloflow.txn_scope', true);\\n"
    "    IF v_prior IS NOT NULL AND v_prior <> '' AND v_prior <> v_scope THEN\\n"
    "        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMPOSITION_UNSUPPORTED';\\n"
    "    END IF;\\n"
    "    PERFORM pg_catalog.set_config('haloflow.txn_scope', v_scope, true);\\n"
)


def _move_after_item_lock(block: str) -> str:
    """Python source for `return template` with `block` moved after the item lock
    (both prepare and apply render from this template)."""

    keep = "\\n" if block.startswith("\\n") else ""
    return (
        "    return template.replace(\n"
        f'        "{block}", "{keep}", 1).replace(\n'
        f'        "{_ITEM_LOCK}", "{_ITEM_LOCK}{block}", 1)\n'
    )


M05_SPECS: tuple[Spec, ...] = (
    ("GM-1-V2", "v2 isolation guard moved after the item lock (GM-1 on v2)",
     "    return template\n", _move_after_item_lock(_ISOLATION)),
    ("GM-2-V2", "v2 composition check moved after the item lock (GM-2 on v2)",
     "    return template\n", _move_after_item_lock(_COMPOSITION)),
    ("SM-1-V2", "v2 step 4b (the actor's standing) removed (SM-1 on v2)",
     '        "    IF NOT COALESCE((v_actor_standing OPERATOR(pg_catalog.->>)\\n"\n',
     '        "    IF false AND NOT COALESCE((v_actor_standing OPERATOR(pg_catalog.->>)\\n"\n'),
    ("SM-2-V2", "v2 step 6e skipped for every target (SM-2 on v2)",
     '        "       AND NOT COALESCE((v_target_standing OPERATOR(pg_catalog.->>)\\n"\n',
     '        "       AND false AND NOT COALESCE((v_target_standing'
     ' OPERATOR(pg_catalog.->>)\\n"\n'),
)


# --- declared kill sets ----------------------------------------------------------------


def _n(file: str, *tests: str) -> frozenset[str]:
    return frozenset(f"{file}::{t}" for t in tests)


def _p(name: str, *ids: str) -> tuple[str, ...]:
    return tuple(f"{name}[{i}]" for i in ids)


_CHANGES = ("deactivate", "revoke_read", "revoke_act")
_MA10K = _p("test_ma10k_isolation_guard", *(
    f"{a}-{f}-{i}" for a in ("claim", "start_progress", "assign", "reassign")
    for f in ("m05_prepare_action_v2", "m05_apply_action_v2")
    for i in ("REPEATABLE READ", "SERIALIZABLE")))

# Declared from the oracle analysis, checked against a local PG17.11 measurement on
# 2026-11-03 (disclosed to Codex before declaration); every declared node is one the
# analysis named as relevant. Analysis-predicted nodes that did NOT fail are listed
# in the W3b acceptance packet with the reason; they are not declared here.
_FM05B = _p("test_fm05b_authorization_lost_while_waiting_on_the_guard", "renamed", "revoked")
_R_REVOKE = (
    *_p("test_r2_change_uncommitted_apply_waits",
        *(f"{c}-{e}" for c in ("revoke_read", "revoke_act") for e in ("commit", "rollback"))),
    *_p("test_r4_r5_apply_first_admin_waits",
        *(f"{m}-{c}" for m in ("apply", "prepare_apply") for c in ("revoke_read", "revoke_act"))),
    *_p("test_r8_lock_timeout_never_accepts", "revoke_read", "revoke_act"),
    *_p("test_ra2_actor_change_uncommitted_apply_waits",
        *(f"{c}-{e}" for c in ("revoke_read", "revoke_act") for e in ("commit", "rollback"))),
    *_p("test_ra4_ra5_apply_first_actor_change_waits",
        *(f"{m}-{c}" for m in ("apply", "prepare_apply") for c in ("revoke_read", "revoke_act"))),
    *_p("test_ra8_actor_lock_timeout_never_accepts", "revoke_read", "revoke_act"),
)
_INELIGIBLE = ("unknown", "inactive", "foreign", "no_read", "no_act")

_AM1_KILLS = (
    "test_dt08_isolation_guard_precedes_shape",
    "test_dt08_isolation_guard_takes_no_lock_first[REPEATABLE READ]",
    "test_dt08_isolation_guard_takes_no_lock_first[SERIALIZABLE]",
)
_AM2_KILLS = (
    "test_dad2_transport_change_replays[auth_method]",
    "test_dad2_transport_change_replays[execution_id]",
    "test_dad2_transport_change_replays[purpose_code]",
    "test_dad2_transport_change_replays[request_id]",
    "test_dad3_integrity_key_without_log_fails_closed",
    "test_dad4_replay_after_intervening_changes_returns_original",
    "test_dad7_constraint_valid_corrupt_log_never_replays[codex_counterexample]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[key_staff_other]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[log_actor_other]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[log_digest_other]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[log_operation_rename]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[log_staff_other]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[result_key_other]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[result_operation_wrong]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[result_schema_version_999]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[result_staff_differs_from_after]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[result_staff_not_object]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[result_staff_null]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_access_granted_on_add]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_access_invalid]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_name_not_command]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_name_null]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_schema_version_2]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_staff_id_other]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_status_bogus]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_status_inactive_for_add]",
    "test_dad7_constraint_valid_corrupt_log_never_replays[snapshot_timestamp_format]",
    "test_dad7_impossible_timestamp_never_replays[add-feb_29_non_leap]",
    "test_dad7_impossible_timestamp_never_replays[add-feb_30]",
    "test_dad7_impossible_timestamp_never_replays[add-hour_25]",
    "test_dad7_impossible_timestamp_never_replays[add-minute_60]",
    "test_dad7_impossible_timestamp_never_replays[add-month_13]",
    "test_dad7_impossible_timestamp_never_replays[deactivate-feb_29_non_leap]",
    "test_dad7_impossible_timestamp_never_replays[deactivate-feb_30]",
    "test_dad7_impossible_timestamp_never_replays[deactivate-hour_25]",
    "test_dad7_impossible_timestamp_never_replays[deactivate-minute_60]",
    "test_dad7_impossible_timestamp_never_replays[deactivate-month_13]",
    "test_dad7_non_add_snapshot_name_rule[81_code_points]",
    "test_dad7_non_add_snapshot_name_rule[control_inside]",
    "test_dad7_non_add_snapshot_name_rule[leading_space]",
    "test_dad7_non_add_snapshot_name_rule[line_separator]",
    "test_dad7_non_add_snapshot_name_rule[not_nfc]",
    "test_dad7_non_add_snapshot_name_rule[trailing_nbsp]",
    "test_dad8_valid_controls_leap_day_and_non_add[add_leap_day]",
    "test_dad8_valid_controls_leap_day_and_non_add[deactivate_leap_day_end]",
    "test_dad8_valid_controls_leap_day_and_non_add[deactivate_ordinary]",
    "test_dad8_valid_historical_replay_control",
    "test_dak1_concurrent_identical_requests_replay",
    "test_dak2_dak3_concurrent_conflicting_requests[display_name]",
    "test_dak2_dak3_concurrent_conflicting_requests[operation]",
    "test_dak2_dak3_concurrent_conflicting_requests[staff_id]",
    "test_dak5_different_actor_same_key[False]",
    "test_dak5_different_actor_same_key[True]",
    "test_dt01_add_creates_active_member_without_access_and_replays",
    "test_dt07_same_key_different_digest_conflict",
)

KILLS: dict[str, frozenset[str]] = {
    "F-EXISTS": _n(FM_FILE, "test_fm03_active_admin_exists_writes_only_the_key",
                   "test_fm07_concurrent_first_members_one_wins"),
    "F-GUARD": _n(FM_FILE, *_FM05B, "test_fm07_concurrent_first_members_one_wins",
                  "test_fm07b_first_member_and_t032_serialize_on_the_guard"),
    # Error-contract kill (log grant_id NOT NULL), retained and labelled as such.
    "F-RECHECK-GRANT": _n(FM_FILE, _FM05B[1]),
    # Behavioral: the revoked-grant race must not succeed (pre-measurement analysis).
    "F-RECHECK-GRANT-KEEP": _n(FM_FILE, _FM05B[1]),
    "F-RECHECK-LOGIN": _n(FM_FILE, _FM05B[0]),
    "F-KEY": _n(FM_FILE, "test_fm03_active_admin_exists_writes_only_the_key"),
    "F-ACCESS": _n(FM_FILE, "test_fm01_empty_clinic_gets_member_and_admin_in_one_step"),
    "F-DIGEST": _n(FM_FILE, "test_fm01_empty_clinic_gets_member_and_admin_in_one_step"),
    "C-TEMPLATE": _n(CU_FILE, "test_am04_a_closure_unit_carrying_a_template_is_refused"),
    "C-OWNER": _n(CU_FILE, "test_am03_owner_entry_removal_is_refused"),
    "C-CHECKSUM": _n(CU_FILE, *(
        f"{t}[{u}]" for t in ("test_am12_closure_checksum_payload_is_exact",
                              "test_am12b_checksum_is_the_pinned_digest_and_every_field_is_bound")
        for u in ("t035_m05_v1_runtime_closure", "t036_m01_staff_v1_runtime_closure"))),
    "C-VERIFY": _n(CL_FILE, "test_cl03_partial_closure_fails_verification_and_is_not_applied"),
    "T029-TS": _n(RI_FILE, *_p("test_ri02_impossible_timestamp_never_replays",
                               "grant_role-supervisor", "activate-None")),
    "T029-IMPLIED": _n(RI_FILE, "test_ri04_activate_with_inactive_snapshot_never_replays"),
    "R-M2-V2": _n(EL_FILE, *_R_REVOKE),
    "GM-1-V2": _n(EL_FILE, *_MA10K),
    "GM-2-V2": _n(EL_FILE, "test_rc1_admin_then_m05_and_reverse", "test_rc1_two_items_in_one_txn",
                  "test_rc2_changed_target_and_reused_admin_key"),
    # Step 4b is the only actor check on claim (target = actor) and on the replay
    # path; for supervisor actions t028's `supervisor` (active AND access AND role)
    # refuses on its own, so RA-1/RA-2/RA-4/5/RA-8 are layered, not discriminating.
    "SM-1-V2": _n(EL_FILE, *_p("test_ma10bcde_ineligible_target_refused",
                               *(f"claim-{k}" for k in _INELIGIBLE)),
                  *_p("test_ra1r_replay_refused_after_actor_change", *_CHANGES)),
    "SM-2-V2": _n(EL_FILE, *_p("test_ma10bcde_ineligible_target_refused",
                               *(f"{a}-{k}" for a in ("assign", "reassign")
                                 for k in _INELIGIBLE)),
                  *_p("test_r1_change_committed_before_apply", *_CHANGES),
                  *_p("test_r2_change_uncommitted_apply_waits",
                      *(f"{c}-commit" for c in _CHANGES)),
                  *_p("test_r6_split_prepare_apply_change_between", *_CHANGES)),
    # AM-1-V2/AM-2-V2 (Codex 182053Z): measured locally after the analysis written at
    # 18:25:19Z; AM-2-V2 fails every same-key path (concurrent and sequential).
    "AM-1-V2": _n(DIR_FILE, *_AM1_KILLS),
    "AM-2-V2": _n(DIR_FILE, *_AM2_KILLS),
}


@dataclass(frozen=True)
class Group:
    tag: str
    target: Path
    specs: tuple[Spec, ...]
    test_files: tuple[str, ...]


GROUPS: tuple[Group, ...] = (
    Group("first-member", FIRST_MEMBER_TARGET, FIRST_MEMBER_SPECS, (FM_FILE,)),
    Group("closure-units", UNITS_TARGET, UNITS_SPECS, (CU_FILE, CL_FILE)),
    Group("closure-runner", RUNNER_TARGET, RUNNER_SPECS, (CL_FILE, CU_FILE)),
    Group("t029", STAFF_TARGET, STAFF_SPECS, (RI_FILE, EL_FILE)),
    Group("m05-v2", M05_TARGET, M05_SPECS, (EL_FILE,)),
    Group("t029-directory", STAFF_TARGET, STAFF_DIRECTORY_SPECS, (DIR_FILE,)),
)
TEST_FILES: tuple[str, ...] = tuple(sorted({f for g in GROUPS for f in g.test_files}))


def _w3a() -> Any:
    spec = importlib.util.spec_from_file_location("_l1w_w3a_mutants", HERE / "l1w_w3a_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha_literals() -> dict[Path, tuple[str, tuple[tuple[str, str], ...]]]:
    literals = dict(_w3a().SHA_LITERALS)
    literals[FIRST_MEMBER_TARGET] = ("haloflow.m01.staff.l1w_w3b_units", (
        ("_T034_SHA256", "_FIRST_ADMIN_BODY"),
    ))
    return literals


def current_shas(target: Path, python: str | None = None) -> dict[str, str]:
    """The SHA each literal of `target` must hold for its CURRENT body text.

    The reviewed literals are SHA-256 over ``normalize_body(body)`` -- the form the
    installed-state comparator hashes. For the W3a bodies that equals the raw text;
    t034 differs (measured locally: the W3a raw-text sync left every t034 mutant
    INSTALLED_STATE_PROFILE_INVALID), so this harness hashes the normalized body for
    every literal. The static test pins that this reproduces every committed literal."""

    key = target.relative_to(HERE.parent) if target.is_absolute() else target
    module, pairs = sha_literals()[key]
    script = (
        "import hashlib, importlib, json, os, sys\n"
        "sys.path.insert(0, 'src')\n"
        "from haloflow.m01.provisioning.installed_state import normalize_body\n"
        f"m = importlib.import_module({module!r})\n"
        "assert m.__file__.replace('\\\\', '/').startswith(os.getcwd().replace('\\\\', '/')"
        " + '/src/'), m.__file__\n"
        "print(json.dumps({lit: hashlib.sha256(normalize_body(getattr(m, body))"
        f".encode('utf-8')).hexdigest() for lit, body in {list(pairs)!r}}}))\n"
    )
    completed = subprocess.run([python or sys.executable, "-I", "-c", script],
                               capture_output=True, text=True, check=True, cwd=HERE.parent)
    shas: dict[str, str] = json.loads(completed.stdout)
    return shas


def sync_sha_literals(target: Path) -> None:
    """Rewrite the target's body SHA literals to its CURRENT (mutated) bodies."""

    key = target.relative_to(HERE.parent) if target.is_absolute() else target
    path = HERE.parent / key
    text = path.read_text(encoding="utf-8")
    for literal, sha in current_shas(key).items():
        text, count = re.subn(rf'^{literal}: Final = "[0-9a-f]{{64}}"$',
                              f'{literal}: Final = "{sha}"', text, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(f"SHA literal {literal} not found exactly once")
    path.write_text(text, encoding="utf-8")
    _w3a().drop_bytecode(path)


def _runner(checker: Any, group: Group) -> Any:
    """Fresh database per run; SHA literals synced to the mutated bodies first."""

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = os.environ["HALOFLOW_TEST_DATABASE_URL"]
    if os.environ.get("HALOFLOW_TEST_DISPOSABLE_CLUSTER") != "1":
        raise SystemExit("HALOFLOW_TEST_DISPOSABLE_CLUSTER=1 is required")
    admin = conninfo_to_dict(base)
    w3a = _w3a()

    def runner(pytest: str, label: str, evidence: Path) -> Any:
        if label != "control" and group.target in sha_literals():
            sync_sha_literals(group.target)
        else:
            w3a.drop_bytecode(HERE.parent / group.target)
        name = "haloflow_test_l1w_w3b_mut_" + "".join(
            ch if ch.isalnum() else "_" for ch in f"{group.tag}_{label}".lower())
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


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _expand_test_files(checker: Any, files: tuple[str, ...]) -> None:
    """The checker passes one TESTS path; expand it to the group's test files."""

    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, list) and files[0] in cmd:
            index = cmd.index(files[0])
            cmd = [*cmd[:index], *files, *cmd[index + 1:]]
        return real_run(cmd, *args, **kwargs)

    checker.subprocess = types.SimpleNamespace(run=run)
    checker.TESTS = files[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--group", action="append", default=None)
    args = parser.parse_args(argv)
    code = 0
    for group in GROUPS:
        if args.group and group.tag not in args.group:
            continue
        checker = _load_checker()
        _expand_test_files(checker, group.test_files)
        checker.TARGET = group.target
        checker.MUTANTS = tuple(
            checker.Mutant(mid, desc, old, new, KILLS.get(mid, frozenset()))
            for mid, desc, old, new in group.specs)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = int(checker.main(
                ["--pytest", args.pytest, "--evidence", f"{args.evidence}/{group.tag}",
                 "--target", str(group.target)],
                runner=_runner(checker, group)))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=l1w-w3b-mutants-{group.tag}"))
        code = code or result
    return code


if __name__ == "__main__":
    raise SystemExit(main())
