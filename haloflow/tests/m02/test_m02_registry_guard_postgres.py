"""CP2-2a guard outcomes, placement mutants, post-grant state and direct-SQL witnesses.

2A-X00–X30 (R-A6 outcome tests), 2A-O01–O07 (CP2-D25-order placement mutants,
**PENDING**: these do not discharge D25-order), 2A-Q00–Q10, 2A-S00–S08.

Controls first (test cases v4 §1): each family's positive control runs earlier
in this module and records that it passed. A mutant row fails if its control
did not pass, so a family result never counts without its control. Pytest runs
this file in definition order, and it must run serially.

The oracle for a refused mutant is the four-part refusal oracle (§1, as
corrected by erratum 1 ET-1). Each X/O/Q row is paired with a direct-SQL S row
that proves the same defect reaches the named guard group (§2.9).
"""

from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.postgres

_CONTROLS: dict[str, bool] = {}

# Row ids as test cases v4 names them; the builders live in `m02_support.MUTANTS`.
G3_MUTANTS = [f"x{n:02d}" for n in range(1, 14)]  # 2A-X01..X13 / S03
G45_MUTANTS = ["x14", "x15", "x16", "x17", "x18"]  # 2A-X14..X18 / S04
POST_GRANT_REFUSED = [f"q{n:02d}" for n in range(1, 10)]  # 2A-Q01..Q09 / S08


def _require_control(name: str) -> None:
    if not _CONTROLS.get(name):
        pytest.fail(f"positive control {name} has not passed in this session; result not counted")


# --- positive controls -----------------------------------------------------


def test_2a_x00_q00_positive_control_production_install(
    m02: ModuleType, m02_ids: Any, production_tenant: tuple[str, str]
) -> None:
    """2A-X00 and 2A-Q00: production installs; lock owner holds exactly §2.8."""

    schema = production_tenant[1]
    assert m02.admin_one(
        m02_ids, "SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (m02.qualified(schema),)
    ) == (True,)
    m02.assert_lock_owner_grants_exact(m02_ids, schema)
    _CONTROLS["X00"] = _CONTROLS["Q00"] = True


def _run_direct(
    m02: ModuleType,
    ids: Any,
    schema: str,
    setup: str | None,
    block: str,
    *,
    setup_check: str = "present",
) -> psycopg.Error | None:
    """§2.9: MIG runs `setup`, asserts it succeeded, then `block`; always rolled back.

    Returns the error `block` raised, or None if it completed.
    """

    raised: psycopg.Error | None = None
    with m02.connect_as(ids, "MIG") as conn:
        try:
            with conn.transaction():
                if setup is not None:
                    conn.execute(m02.render(setup, schema))
                present = conn.execute(
                    "SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (m02.qualified(schema),)
                ).fetchone()
                assert present == ((setup_check == "present"),), "setup did not reach the guard"
                try:
                    with conn.transaction():
                        conn.execute(m02.render(block, schema))
                except psycopg.Error as error:
                    raised = error
                raise psycopg.Rollback()
        finally:
            m02.reassert_role(conn, "MIG")
    return raised


def _assert_guard_message(error: psycopg.Error | None, message: str) -> None:
    assert error is not None, "the guard completed; expected a refusal"
    assert error.sqlstate == "P0001", error.sqlstate
    assert error.diag.message_primary == message


def test_2a_s00_guard_passes_on_the_production_template(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str]
) -> None:
    from haloflow.m02.units import T002_SQL

    template = m02.Template.parse(T002_SQL)
    error = _run_direct(
        m02,
        m02_ids,
        m01_only_tenant[1],
        template.only("table", "runtime_privileges", "rejector").render(),
        template.steps["guard"],
    )
    assert error is None
    _CONTROLS["S00"] = True


# --- 2A-X01 .. X18: one-defect mutants (outcome tests) ---------------------


@pytest.mark.parametrize("name", [*G3_MUTANTS, *G45_MUTANTS])
def test_2a_x_one_defect_mutant_is_refused(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str], name: str
) -> None:
    _require_control("X00")
    m02.expect_install_failure(m02_ids, m02.mutant_registry(name), m02_tenant, m02.mutant_id(name))


@pytest.mark.parametrize(
    "attribute", ["LOGIN", "SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"]
)
def test_2a_x19_x20_unsafe_pre_existing_lock_owner_is_refused_by_g1(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str], attribute: str
) -> None:
    """X19 = LOGIN; X20a–e = the other five. Template unchanged. L-5 staging asserted."""

    _require_control("X00")
    with m02.connect_admin(m02_ids) as conn:
        conn.execute(sql.SQL("ALTER ROLE {} " + attribute).format(sql.Identifier(m02.LOCK_OWNER)))
    try:
        m02.expect_install_failure(m02_ids, m02.production_registry(), m02_tenant, m02.T002_ID)
        # L-5, asserted rather than hidden: the edge and the schema USAGE made
        # before t002 remain; the column grants are absent (oracle part 4).
        edges = m02.edges_into(m02_ids, m02.LOCK_OWNER)
        assert [(m, s, i, a) for m, s, i, a, _ in edges] == [(m02.MIGRATOR, True, False, False)]
        assert m02.admin_one(
            m02_ids,
            "SELECT pg_catalog.has_schema_privilege(%s, %s, 'USAGE')",
            (m02.LOCK_OWNER, m02_tenant[1]),
        ) == (True,)
    finally:
        m02.restore_lock_owner(m02_ids)


def test_2a_x30_kill_control_guard_removed_installs_the_defect(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """X03's defect plus the guard deleted installs: the guard is load-bearing."""

    _require_control("X00")
    m02.provision_sync(m02_ids, m02.mutant_registry("x30"), m02_tenant)
    schema = m02_tenant[1]
    m02.assert_lock_owner_grants_exact(m02_ids, schema)
    assert m02.admin_all(
        m02_ids,
        "SELECT tgqual IS NOT NULL FROM pg_catalog.pg_trigger "
        "WHERE tgrelid = pg_catalog.to_regclass(%s) AND NOT tgisinternal",
        (m02.qualified(schema),),
    ) == [(True,)]


# --- 2A-O01 .. O07: D25-order placement mutants (PENDING) -------------------


@pytest.mark.parametrize("name", ["o01", "o02", "o03", "o04", "o05"])
def test_2a_o_grant_placed_before_g6_is_refused(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str], name: str
) -> None:
    """Outcome evidence for the tested placements only. CP2-D25-order stays PENDING."""

    _require_control("X00")
    m02.expect_install_failure(m02_ids, m02.mutant_registry(name), m02_tenant, m02.mutant_id(name))


def test_2a_o06_control_g6_removed_installs(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """O02's placement with G-6 deleted installs: G-6 is what refuses O01–O05."""

    _require_control("X00")
    m02.provision_sync(m02_ids, m02.mutant_registry("o06"), m02_tenant)
    m02.assert_lock_owner_grants_exact(m02_ids, m02_tenant[1])


def test_2a_o07_named_non_detection_grant_then_revoke(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """Recorded blind spot, not a failure: grant+revoke before G-6 is not detected."""

    _require_control("X00")
    m02.provision_sync(m02_ids, m02.mutant_registry("o07"), m02_tenant)
    m02.assert_lock_owner_grants_exact(m02_ids, m02_tenant[1])


# --- 2A-Q01 .. Q10: post-grant check ---------------------------------------


@pytest.mark.parametrize("name", [f"q{n:02d}" for n in range(1, 10)])
def test_2a_q_post_grant_defect_is_refused(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str], name: str
) -> None:
    _require_control("Q00")
    m02.expect_install_failure(m02_ids, m02.mutant_registry(name), m02_tenant, m02.mutant_id(name))


def test_2a_q10_kill_control_check_removed_installs_maintain(
    m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    _require_control("Q00")
    m02.provision_sync(m02_ids, m02.mutant_registry("q10"), m02_tenant)
    assert m02.table_privilege(m02_ids, m02.LOCK_OWNER, m02_tenant[1], "MAINTAIN") is True


# --- 2A-S01 .. S08: direct-SQL witnesses (message level) --------------------


def test_2a_s01_empty_schema_guard_is_missing_safe(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str]
) -> None:
    from haloflow.m02.units import T002_SQL

    _require_control("S00")
    guard = m02.Template.parse(T002_SQL).steps["guard"]
    error = _run_direct(m02, m02_ids, m01_only_tenant[1], None, guard, setup_check="absent")
    _assert_guard_message(error, m02.MSG_REJECTOR)


def test_2a_s02_table_without_function_or_trigger_is_missing_safe(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str]
) -> None:
    from haloflow.m02.units import T002_SQL

    _require_control("S00")
    schema = m01_only_tenant[1]
    template = m02.Template.parse(T002_SQL)
    raised: psycopg.Error | None = None
    with m02.connect_as(m02_ids, "MIG") as conn:
        try:
            with conn.transaction():
                conn.execute(
                    m02.render(template.only("table", "runtime_privileges").render(), schema)
                )
                state = conn.execute(
                    """
                    SELECT pg_catalog.to_regclass(%s) IS NOT NULL,
                           pg_catalog.to_regprocedure(%s) IS NULL,
                           (SELECT count(*) FROM pg_catalog.pg_trigger
                             WHERE tgrelid = pg_catalog.to_regclass(%s) AND NOT tgisinternal)
                    """,
                    (
                        m02.qualified(schema),
                        f"{schema}.{m02.REJECTOR_FUNCTION}()",
                        m02.qualified(schema),
                    ),
                ).fetchone()
                assert state == (True, True, 0), "setup did not produce the S02 state"
                try:
                    with conn.transaction():
                        conn.execute(m02.render(template.steps["guard"], schema))
                except psycopg.Error as error:
                    raised = error
                raise psycopg.Rollback()
        finally:
            m02.reassert_role(conn, "MIG")
    _assert_guard_message(raised, m02.MSG_REJECTOR)


def _mutant_setup_and_guard(m02: ModuleType, name: str) -> tuple[str, str]:
    from haloflow.m02.units import T002_SQL

    mutated = m02.MUTANTS[name](m02.Template.parse(T002_SQL))
    return (
        mutated.only("table", "runtime_privileges", "rejector").render(),
        mutated.steps["guard"],
    )


@pytest.mark.parametrize("name", G3_MUTANTS)
def test_2a_s03_g3_defect_reaches_the_rejector_group(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str], name: str
) -> None:
    _require_control("S00")
    setup, guard = _mutant_setup_and_guard(m02, name)
    _assert_guard_message(
        _run_direct(m02, m02_ids, m01_only_tenant[1], setup, guard), m02.MSG_REJECTOR
    )


@pytest.mark.parametrize("name", G45_MUTANTS)
def test_2a_s04_g4_g5_defect_reaches_the_privilege_group(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str], name: str
) -> None:
    _require_control("S00")
    setup, guard = _mutant_setup_and_guard(m02, name)
    _assert_guard_message(
        _run_direct(m02, m02_ids, m01_only_tenant[1], setup, guard), m02.MSG_PRIVILEGE
    )


@pytest.mark.parametrize(
    "attribute", ["LOGIN", "SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"]
)
def test_2a_s05_unsafe_lock_owner_reaches_g1(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str], attribute: str
) -> None:
    from haloflow.m02.units import T002_SQL

    _require_control("S00")
    template = m02.Template.parse(T002_SQL)
    with m02.connect_admin(m02_ids) as conn:
        conn.execute(sql.SQL("ALTER ROLE {} " + attribute).format(sql.Identifier(m02.LOCK_OWNER)))
    try:
        error = _run_direct(
            m02,
            m02_ids,
            m01_only_tenant[1],
            template.only("table", "runtime_privileges", "rejector").render(),
            template.steps["guard"],
        )
        _assert_guard_message(error, m02.MSG_LOCK_OWNER)
    finally:
        m02.restore_lock_owner(m02_ids)


def test_2a_s06_lock_owner_renamed_away_reaches_g1_missing_safe(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str]
) -> None:
    from haloflow.m02.units import T002_SQL

    _require_control("S00")
    template = m02.Template.parse(T002_SQL)
    saved = f"{m02.LOCK_OWNER}_s06_saved"
    with m02.connect_admin(m02_ids) as conn:
        conn.execute(
            sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                sql.Identifier(m02.LOCK_OWNER), sql.Identifier(saved)
            )
        )
    try:
        error = _run_direct(
            m02,
            m02_ids,
            m01_only_tenant[1],
            template.only("table", "runtime_privileges", "rejector").render(),
            template.steps["guard"],
        )
        _assert_guard_message(error, m02.MSG_LOCK_OWNER)
    finally:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute(
                sql.SQL("ALTER ROLE {} RENAME TO {}").format(
                    sql.Identifier(saved), sql.Identifier(m02.LOCK_OWNER)
                )
            )
        m02.assert_baseline_role(m02_ids)


def test_2a_s07_grant_before_guard_reaches_g6(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str]
) -> None:
    from haloflow.m02.units import T002_SQL

    _require_control("S00")
    template = m02.Template.parse(T002_SQL)
    setup = template.only("table", "runtime_privileges", "rejector", "lock_owner_grants").render()
    error = _run_direct(m02, m02_ids, m01_only_tenant[1], setup, template.steps["guard"])
    _assert_guard_message(error, m02.MSG_GRANT_ORDER)


@pytest.mark.parametrize("name", POST_GRANT_REFUSED)
def test_2a_s08_post_grant_defect_reaches_the_post_grant_check(
    m02: ModuleType, m02_ids: Any, m01_only_tenant: tuple[str, str], name: str
) -> None:
    from haloflow.m02.units import T002_SQL

    _require_control("S00")
    mutated = m02.MUTANTS[name](m02.Template.parse(T002_SQL))
    setup = mutated.without("post_grant_check").render()
    error = _run_direct(m02, m02_ids, m01_only_tenant[1], setup, mutated.steps["post_grant_check"])
    _assert_guard_message(error, m02.MSG_POST_GRANT)
