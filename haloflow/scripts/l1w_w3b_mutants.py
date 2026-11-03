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
import os
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

Spec = tuple[str, str, str, str]  # (id, description, old, new)

# --- first-member (t034) -------------------------------------------------------------

_KEY_INSERT = (
    "    INSERT INTO {schema}.m01_staff_bootstrap_keys (client_action_id, digest, actor_id,\n"
    "        staff_id, outcome, created_at, operation)\n"
    "    VALUES (v_key, v_digest, v_actor, v_staff, v_outcome, v_now, 'create_first_admin');\n"
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
    ("F-RECHECK-LOGIN", "t034 does not re-check the login (OID and name) after the guard",
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

KILLS: dict[str, frozenset[str]] = {}  # filled from the oracle analysis below


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


def sync_sha_literals(target: Path) -> None:
    w3a = _w3a()
    w3a.SHA_LITERALS = sha_literals()
    w3a.sync_sha_literals(target)


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
