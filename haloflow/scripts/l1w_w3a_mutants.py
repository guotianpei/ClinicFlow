"""L-1W W3a targeted mutation controls (Codex 222540Z: final acceptance requires
targeted mutants and guard-before-membership source/order evidence).

Two targets, each judged by the shared part-A checker (``scripts/m02_observation_mutants.py``,
no judging logic added here): ``src/haloflow/m01/staff/l1w_units.py`` (t029, t032) and
``src/haloflow/m05/gateways_v2.py`` (t030/t031). The control run must pass completely;
a mutant is KILLED only by a completed run whose every failure is in its PREDECLARED
kill set (no error, no skip, the control's node set); each target is restored after
every run and its SHA-256 re-checked.

The typed bodies carry reviewed SHA-256 literals, so a mutated body would be refused
at composition and every row would fail for that reason alone. After the checker
writes a mutant, the runner below re-synchronises ONLY the body SHA literals of the
mutated target (computed by importing the mutated module in a subprocess), so the
mutated SQL itself is installed and the tests decide. The checker's restore puts the
original file (and literals) back.

Every run gets its own fresh database in the disposable cluster (the W2 runner
pattern). Kill sets are DECLARED here from the oracle analysis; PostgreSQL 17 is
required to install the production units, so the first PG17 CI run is the
measurement, and any difference is reported, never edited away silently.

Usage (from haloflow/, HALOFLOW_TEST_DATABASE_URL and HALOFLOW_TEST_DISPOSABLE_CLUSTER=1
set):
    "$VENV/bin/python" -I scripts/l1w_w3a_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/l1w-w3a-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TITLE_FROM = "m02-observation-mutants"
TEST_FILES: tuple[str, ...] = (
    "tests/m05/test_l1w_w3a_authority_postgres.py",
    "tests/m05/test_l1w_w3a_bootstrap_postgres.py",
    "tests/m05/test_l1w_w3a_locks_grants_postgres.py",
    "tests/m05/test_l1w_w3a_impl_units.py",
)

STAFF_TARGET = Path("src/haloflow/m01/staff/l1w_units.py")
M05_TARGET = Path("src/haloflow/m05/gateways_v2.py")
ROLE_SAFETY_TARGET = Path("src/haloflow/m01/provisioning/role_safety.py")

# target -> (module, ((SHA literal name, body attribute), ...))
SHA_LITERALS: dict[Path, tuple[str, tuple[tuple[str, str], ...]]] = {
    STAFF_TARGET: ("haloflow.m01.staff.l1w_units", (
        ("_T027_SHA256", "_SELF_STANDING_BODY"),
        ("_T028_SHA256", "_MEMBER_STANDING_BODY"),
        ("_T029_SHA256", "_ADMIN_V2_BODY"),
        ("_T032_SHA256", "_BOOTSTRAP_BODY"),
    )),
    M05_TARGET: ("haloflow.m05.gateways_v2", (
        ("_PREPARE_V2_BODY_SHA256", "_PREPARE_V2_BODY"),
        ("_APPLY_V2_BODY_SHA256", "_APPLY_V2_BODY"),
    )),
}

_GUARD_T029 = (
    "    -- 4. The tenant-local role guard, first, on every role- or status-changing branch.\n"
    "    IF v_op IN ('grant_role', 'revoke_role', 'deactivate', 'activate') THEN\n"
    "        PERFORM 1 FROM {schema}.m01_staff_role_guard AS g WHERE g.id FOR UPDATE;\n"
    "    END IF;\n\n"
)
_MEMBERSHIP_T029 = (
    "    -- 5. Membership rows in ascending staff_id order: actor FOR SHARE, target FOR UPDATE.\n"
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
    "        v_found := FOUND;\n"
    "        SELECT s.status INTO v_actor_status\n"
    "          FROM {schema}.m01_staff AS s WHERE s.staff_id = v_actor_id FOR SHARE;\n"
    "    END IF;\n\n"
)

# (id, description, old, new) per target. Every replacement keeps the SQL valid.
STAFF_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("A-AUTH", "t029 authority ignores the clinic_admin role row",
     "       OR NOT EXISTS (SELECT 1 FROM {schema}.m01_staff_roles AS r\n"
     "                       WHERE r.staff_id = v_actor_id AND r.role = 'clinic_admin') THEN\n",
     "       OR false THEN\n"),
    ("A-SELF", "t029 self rules removed",
     "    IF v_staff = v_actor_id\n       AND (v_op = 'bind_identity'\n",
     "    IF false\n       AND (v_op = 'bind_identity'\n"),
    ("A-GUARD", "t029 role/status changes take no guard",
     "    IF v_op IN ('grant_role', 'revoke_role', 'deactivate', 'activate') THEN\n"
     "        PERFORM 1 FROM {schema}.m01_staff_role_guard",
     "    IF false THEN\n"
     "        PERFORM 1 FROM {schema}.m01_staff_role_guard"),
    ("A-ORDER", "t029 guard taken AFTER the membership rows",
     _GUARD_T029 + _MEMBERSHIP_T029,
     _MEMBERSHIP_T029 + _GUARD_T029),
    ("B-OID", "t032 support login matched by OID only (renamed login accepted)",
     "     WHERE l.login_name = session_user\n       AND l.login_oid = (SELECT r.oid",
     "     WHERE l.login_oid = (SELECT r.oid"),
    ("B-GUARD", "t032 takes no guard",
     "    -- Locks: the tenant-local guard, then the target's membership row.\n"
     "    PERFORM 1 FROM {schema}.m01_staff_role_guard AS g WHERE g.id FOR UPDATE;\n",
     "    -- Locks: the tenant-local guard, then the target's membership row.\n"),
    ("B-RECHECK", "t032 grant not re-checked after the locks",
     "     ORDER BY g.expires_at DESC, g.grant_id\n     LIMIT 1;\n    IF v_grant IS NULL THEN\n",
     "     ORDER BY g.expires_at DESC, g.grant_id\n     LIMIT 1;\n    IF false THEN\n"),
    ("B-EXISTS", "t032 ignores an existing active clinic_admin",
     "    ELSIF EXISTS (SELECT 1\n                    FROM {schema}.m01_staff_roles AS r\n",
     "    ELSIF false AND EXISTS (SELECT 1\n"
     "                    FROM {schema}.m01_staff_roles AS r\n"),
)

M05_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("M-ORDER", "v2 re-reads the actor standing AFTER the item lock",
     '        "    IF NOT COALESCE((v_actor_standing OPERATOR(pg_catalog.->>)\\n"\n',
     '        "    IF NOT COALESCE(({schema}.m01_staff_member_standing(v_actor)'
     ' OPERATOR(pg_catalog.->>)\\n"\n'),
    ("M-ROLE", "supervise granted by the caller flag alone (role ignored)",
     "\"            'supervisor')::boolean, false)\\n\"",
     "\"            'supervisor')::boolean, false) OR true\\n\""),
    ("M-FLAG", "the caller flag cannot narrow supervise",
     "\"        AND (v_perm OPERATOR(pg_catalog.->) 'supervise') = 'true'::jsonb;\\n\"",
     "\"        AND true;\\n\""),
)

ROLE_SAFETY_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("R-PROFILES", "stage 1 skips profiles no registry unit runs as (support unchecked)",
     "        if role in profiles and role not in declared_roles\n",
     "        if False\n"),
)

_A = "tests/m05/test_l1w_w3a_authority_postgres.py::"
_B = "tests/m05/test_l1w_w3a_bootstrap_postgres.py::"
_L = "tests/m05/test_l1w_w3a_locks_grants_postgres.py::"
_U = "tests/m05/test_l1w_w3a_impl_units.py::"
_L06 = (
    _L + "test_l06_bootstrap_waits_on_an_admin_role_change_then_sees_the_admin",
    _L + "test_l06_bootstrap_waits_on_a_status_change_of_an_admin[deactivate]",
    _L + "test_l06_bootstrap_waits_on_a_status_change_of_an_admin[activate]",
)
KILLS: dict[str, frozenset[str]] = {
    "A-AUTH": frozenset({
        _A + "test_c04_forged_staff_admin_without_role_row_is_refused",
        _L + "test_l01_l02_reciprocal_admin_revoke_serializes_on_the_guard[a-first]",
        _L + "test_l01_l02_reciprocal_admin_revoke_serializes_on_the_guard[b-first]",
    }),
    "A-SELF": frozenset({
        _A + "test_r03_self_change_of_clinic_admin_is_refused[grant_role]",
        _A + "test_r03_self_change_of_clinic_admin_is_refused[revoke_role]",
        _L + "test_b05_self_bind_and_foreign_target_are_refused",
    }),
    "A-GUARD": frozenset({
        _B + "test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[grant_role-True]",  # noqa: E501
        _B + "test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[revoke_role-True]",  # noqa: E501
        _B + "test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[deactivate-True]",  # noqa: E501
        _B + "test_bs08_guard_is_taken_by_exactly_the_role_and_status_changing_paths[activate-True]",  # noqa: E501
        *_L06,
    }),
    "A-ORDER": frozenset({
        _U + "test_l07_guard_precedes_every_membership_lock[t029_m01_staff_admin_v2]",
    }),
    "B-OID": frozenset({
        _B + "test_bs02_identity_and_grant_refusals_write_nothing[renamed]",
    }),
    "B-GUARD": frozenset({
        _B + "test_bs04_concurrent_bootstraps_for_different_targets_serialize",
        _B + "test_bs05_grant_revoked_while_waiting_on_the_guard",
        _B + "test_bs08_bootstrap_blocks_on_the_guard",
        _U + "test_l07_guard_precedes_every_membership_lock[t032_m01_staff_bootstrap_admin]",
        *_L06,
    }),
    "B-RECHECK": frozenset({
        _B + "test_bs05_grant_revoked_while_waiting_on_the_guard",
    }),
    "B-EXISTS": frozenset({
        _B + "test_bs03_admin_exists_and_inactive_target",
        _B + "test_bs04_concurrent_bootstraps_for_different_targets_serialize",
        *_L06,
    }),
    "M-ORDER": frozenset({
        _L + "test_l05_v2_bodies_take_every_membership_lock_before_the_item",
    }),
    "M-ROLE": frozenset({
        _A + "test_c03_forged_supervise_without_role_row_is_refused_state_unchanged",
        _A + "test_c03_caller_flag_only_narrows",
        _A + "test_r02_supervisor_removed_is_refused_at_the_next_call",
    }),
    "R-PROFILES": frozenset({
        _U + "test_sup08_every_schema_privileged_profile_is_read_after_registry_roles",
        _U + "test_sup08_unsafe_or_missing_support_refuses_even_for_an_empty_registry[login]",
        _U + "test_sup08_unsafe_or_missing_support_refuses_even_for_an_empty_registry[bypassrls]",
        _U + "test_sup08_unsafe_or_missing_support_refuses_even_for_an_empty_registry[missing]",
    }),
    "M-FLAG": frozenset({
        _A + "test_c03_caller_flag_only_narrows",
    }),
}


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sync_sha_literals(target: Path, python: str | None = None) -> None:
    """Rewrite the target's body SHA literals to its CURRENT (mutated) bodies."""

    key = target.relative_to(HERE.parent) if target.is_absolute() else target
    module, pairs = SHA_LITERALS[key]
    target = HERE.parent / key
    script = (
        "import hashlib, importlib, json, sys\n"
        # The working tree, not an installed copy: CI's project install need not be
        # editable, while pytest imports src/ (CI #228 measured the stale-hash case).
        "sys.path.insert(0, 'src')\n"
        f"m = importlib.import_module({module!r})\n"
        "assert m.__file__.replace('\\\\', '/').startswith(str(__import__('os').getcwd())"
        ".replace('\\\\', '/') + '/src/'), m.__file__\n"
        "print(json.dumps({lit: hashlib.sha256(getattr(m, body).encode()).hexdigest()"
        f" for lit, body in {list(pairs)!r}}}))\n"
    )
    completed = subprocess.run([python or sys.executable, "-I", "-c", script],
                               capture_output=True,
                               text=True, check=True, cwd=HERE.parent)
    import json
    shas: dict[str, str] = json.loads(completed.stdout)
    text = target.read_text(encoding="utf-8")
    for literal, sha in shas.items():
        text, count = re.subn(rf'^{literal}: Final = "[0-9a-f]{{64}}"$',
                              f'{literal}: Final = "{sha}"', text, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(f"SHA literal {literal} not found exactly once")
    target.write_text(text, encoding="utf-8")
    drop_bytecode(target)


def drop_bytecode(target: Path) -> None:
    """Remove the target's cached bytecode (CI #229 measured stale literals).

    A literal rewrite keeps the file size, and the sync subprocess has just
    compiled the pre-rewrite text, so a rewrite in the same second leaves a
    timestamp-valid .pyc that still holds the OLD literals: the next import then
    pairs a mutated body with a stale hash (INSTALLED_STATE_PROFILE_INVALID).
    Deleting the .pyc makes the next import compile the file as written."""

    for cached in (target.parent / "__pycache__").glob(f"{target.stem}.*.pyc"):
        cached.unlink(missing_ok=True)


def _runner(checker: Any, target: Path) -> Any:
    """Fresh database per run; SHA literals synced to the mutated bodies first."""

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = os.environ["HALOFLOW_TEST_DATABASE_URL"]
    if os.environ.get("HALOFLOW_TEST_DISPOSABLE_CLUSTER") != "1":
        raise SystemExit("HALOFLOW_TEST_DISPOSABLE_CLUSTER=1 is required")
    admin = conninfo_to_dict(base)

    def runner(pytest: str, label: str, evidence: Path) -> Any:
        if label != "control" and target in SHA_LITERALS:
            sync_sha_literals(target)
        name = "haloflow_test_l1w_w3a_mut_" + "".join(
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


def _expand_test_files(checker: Any) -> None:
    """The checker passes one TESTS path; expand it to the W3a test files."""

    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, list) and TEST_FILES[0] in cmd:
            index = cmd.index(TEST_FILES[0])
            cmd = [*cmd[:index], *TEST_FILES, *cmd[index + 1:]]
        return real_run(cmd, *args, **kwargs)

    checker.subprocess = types.SimpleNamespace(run=run)
    checker.TESTS = TEST_FILES[0]


def build_mutants(checker: Any, specs: tuple[tuple[str, str, str, str], ...]) -> tuple[Any, ...]:
    return tuple(checker.Mutant(mid, desc, old, new, KILLS.get(mid, frozenset()))
                 for mid, desc, old, new in specs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    code = 0
    for target, specs, tag in ((STAFF_TARGET, STAFF_SPECS, "staff"),
                               (M05_TARGET, M05_SPECS, "m05"),
                               (ROLE_SAFETY_TARGET, ROLE_SAFETY_SPECS, "role-safety")):
        checker = _load_checker()
        _expand_test_files(checker)
        checker.TARGET = target
        checker.MUTANTS = build_mutants(checker, specs)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = int(checker.main(
                ["--pytest", args.pytest, "--evidence", f"{args.evidence}/{tag}",
                 "--target", str(target)],
                runner=_runner(checker, target)))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=l1w-w3a-mutants-{tag}"))
        code = code or result
    return code


if __name__ == "__main__":
    raise SystemExit(main())
