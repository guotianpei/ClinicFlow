"""L-6 CP-1: durable maintenance state, triggers and indexes (revision `005`, part 1).

Cases (test cases v4 `a3d44758...3f3e`, §3 and §9): TC-S01, S02, S03, S04, S05,
S06, S07, S09, S11 and TC-V08. Plan v4 (`574dec8a...b7f3`) with amendment r3
(`b0b82277...523e`), both owner-approved on 2026-10-01.

Contract these tests freeze (each name or message below is asserted exactly):

* tables ``shared.tenant_maintenance_operations``, ``..._withheld`` and
  ``..._attempts``, keyed by ``maintenance_operation_id`` (IP-12);
* trigger refusals are ``P0001`` (RC-09) with the messages in ``MSG``;
* uniqueness refusals are ``23505`` (RC-10) with the constraint names in ``IDX``;
* privilege refusals are ``42501`` (RC-11).

**IP-15, pending the owner's reading.** v4 expects RC-09 for UPDATE and DELETE
in TC-S01 (DELETE), TC-S02 and TC-S03, and S03 names P and M. Under architecture
§3, P and M hold no UPDATE or DELETE privilege on those tables, and PostgreSQL
checks privileges before any trigger runs, so P and M receive RC-11. These tests
therefore assert both layers: P and M are refused by privilege (RC-11), and the
table owner, who does hold the privilege, is refused by the trigger (RC-09). If
the owner chooses another reading, the affected variants change before freeze.

Data is synthetic only (R2). Rows are written by the real roles through their
login shims; the only direct writes are the ones v4 itself prescribes for these
cases (§3 direct DML, TC-S11's sentinel). The per-test cleanup below is a
harness escape hatch, run as the superuser test administrator only.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from haloflow.m01.provisioning import MIGRATOR_ROLE, PROVISIONER_ROLE

pytestmark = pytest.mark.postgres

OWNER_ROLE = "haloflow_owner"

OPS = "shared.tenant_maintenance_operations"
WITHHELD = "shared.tenant_maintenance_withheld"
ATTEMPTS = "shared.tenant_maintenance_attempts"
NEW_TABLES = (ATTEMPTS, WITHHELD, OPS)  # deletion order for cleanup

# RC-09: trigger messages (contract; frozen with this file).
MSG = {
    "identity": "tenant_maintenance_operations: identity columns are immutable",
    "transition": "tenant_maintenance_operations: illegal state transition",
    "migrator_state": "tenant_maintenance_operations: migrator may only move activated to released",
    "no_delete": "tenant_maintenance_operations: rows cannot be deleted",
    "layer_role": "tenant_maintenance_withheld: layer not writable by this role",
    "layer_closed": "tenant_maintenance_withheld: layer already withheld",
    "append_only": "tenant maintenance evidence is append-only",
    "n_stopped": "tenant_maintenance_attempts: neutralization generation is stopped",
    "stop_after_neutralized": "tenant_maintenance_attempts: stop not allowed after neutralized",
    "stop_without_bootstrap": "tenant_maintenance_attempts: stop requires bootstrap_started",
}

# RC-10: unique index names (contract).
IDX = {
    "one_open": "tenant_maintenance_operations_one_open_per_tenant",
    "once_only": "tenant_maintenance_attempts_once_only",
    "gen": "tenant_maintenance_attempts_gen",
    "gen_round": "tenant_maintenance_attempts_gen_round",
    "neutralized_once": "tenant_maintenance_attempts_neutralized_once",
}

RC09, RC10, RC11 = "P0001", "23505", "42501"

# Architecture §3, the once-only list; TC-S04 covers all but `apply_applied`.
ONCE_ONLY = (
    "op_created",
    "l2_withheld",
    "l1_withheld",
    "drained",
    "exclusion_established",
    "lo_granted",
    "apply_applied",
    "l2b_withheld",
    "verified",
    "activated",
    "l1_restored",
    "l2_l2b_restored",
    "released",
    "completed",
    "abandoned",
)

ALT_TENANT = object()  # resolved to the module's second real tenant
ALT_EVENT = object()  # resolved to a real history event of that tenant

HISTORY_SELECTABLE = frozenset(
    {"tenant_id", "event_id", "new_state", "reason_code", "execution_id"}
)


# --- identities --------------------------------------------------------------


@contextmanager
def _session(conninfo: str, role: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        row = conn.execute("SELECT current_user").fetchone()
        assert row is not None and row[0] == role
        yield conn


@pytest.fixture
def as_role(
    migrated_database: str, role_logins: dict[str, str]
) -> Callable[[str], AbstractContextManager[psycopg.Connection[Any]]]:
    """`P`, `M` through their login shims; `OWNER` through the test administrator."""

    def _open(who: str) -> AbstractContextManager[psycopg.Connection[Any]]:
        if who == "P":
            return _session(role_logins[PROVISIONER_ROLE], PROVISIONER_ROLE)
        if who == "M":
            return _session(role_logins[MIGRATOR_ROLE], MIGRATOR_ROLE)
        if who == "OWNER":
            return _session(migrated_database, OWNER_ROLE)
        raise AssertionError(who)

    return _open


def _admin_value(admin: str, query: str, params: Any = None) -> Any:
    with psycopg.connect(admin, autocommit=True) as conn:
        row = conn.execute(query, params).fetchone()
        assert row is not None
        return row[0]


@pytest.fixture(scope="module", autouse=True)
def _record_server_version(migrated_database: str) -> None:
    """Evidence instrumentation only (plan v4 §5.3): the census reads this line from `-rA`."""

    print(f"L6_SERVER_VERSION={_admin_value(migrated_database, 'SELECT version()')}")


# --- tenant and cleanup ------------------------------------------------------


@pytest.fixture
def tenant(production_tenant: tuple[str, str], migrated_database: str) -> Iterator[str]:
    """A real tenant row for the operation rows to reference; L-6 rows removed before and after.

    **Schema-layer evidence only.** The tenant is provisioned by the module's
    `production_tenant` fixture through the full production registry, so it is at
    the current version with `t003` applied: it is **not** a valid L-6 C1
    pre-state. The CP-1 cases test the durable-state objects themselves (triggers,
    indexes, grants), none of which reads the tenant's version or ledger, and they
    write `from_version = 2, to_version = 3, t003_baseline = 'absent'` as synthetic
    values. The tenant's actual state is printed for the record. Valid C1
    pre-states are constructed in CP-3 onward. Missing-table safe, so a pre-change
    run fails inside the test body, never here.
    """

    tenant_id = production_tenant[0]
    state = _admin_value(
        migrated_database,
        "SELECT lifecycle_state || '/' || schema_version FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    )
    print(f"L6_CP1_FIXTURE_TENANT={tenant_id} state={state} (schema-layer setup only)")
    _purge(migrated_database, tenant_id)
    yield tenant_id
    _purge(migrated_database, tenant_id)


def _purge(admin: str, tenant_id: str) -> None:
    """Superuser test-only escape hatch. One transaction: DDL is transactional in
    PostgreSQL, so any failure rolls back the trigger DISABLE as well, and no later
    test can run against disabled triggers."""

    with psycopg.connect(admin, autocommit=False) as conn, conn.transaction():
        present = [
            t
            for t in NEW_TABLES
            if conn.execute("SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (t,)).fetchone()
            == (True,)
        ]
        for table in present:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        if ATTEMPTS in present:
            conn.execute(f"DELETE FROM {ATTEMPTS} WHERE tenant_id = %s", (tenant_id,))
        if WITHHELD in present and OPS in present:
            conn.execute(
                f"DELETE FROM {WITHHELD} WHERE maintenance_operation_id IN "
                f"(SELECT maintenance_operation_id FROM {OPS} WHERE tenant_id = %s)",
                (tenant_id,),
            )
        if OPS in present:
            conn.execute(f"DELETE FROM {OPS} WHERE tenant_id = %s", (tenant_id,))
        for table in present:
            conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


# --- writers -----------------------------------------------------------------


def _latest_history_event(admin: str, tenant_id: str) -> int:
    return int(
        _admin_value(
            admin,
            "SELECT max(event_id) FROM shared.tenant_state_history WHERE tenant_id = %s",
            (tenant_id,),
        )
    )


def create_operation(conn: psycopg.Connection[Any], admin: str, tenant_id: str) -> UUID:
    """P inserts an `establishing` operation row with synthetic values; returns its id.

    Schema-layer setup only (see `tenant`); not a production C1 step.
    """

    op = uuid4()
    conn.execute(
        f"""
        INSERT INTO {OPS} (maintenance_operation_id, tenant_id, from_version, to_version, state,
                           pre_entry_event_id, t003_baseline, current_attempt)
        VALUES (%s, %s, 2, 3, 'establishing', %s, 'absent', %s)
        """,
        (op, tenant_id, _latest_history_event(admin, tenant_id), uuid4()),
    )
    return op


def set_state(conn: psycopg.Connection[Any], op: UUID, state: str) -> None:
    conn.execute(f"UPDATE {OPS} SET state = %s WHERE maintenance_operation_id = %s", (state, op))


def advance(conn: psycopg.Connection[Any], op: UUID, *states: str) -> None:
    for state in states:
        set_state(conn, op, state)


def append(
    conn: psycopg.Connection[Any],
    op: UUID | None,
    tenant_id: str,
    event: str,
    detail: dict[str, Any] | None = None,
    attempt: UUID | None = None,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {ATTEMPTS} (attempt_id, maintenance_operation_id, tenant_id, event, detail)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        (attempt or uuid4(), op, tenant_id, event, json.dumps(detail or {})),
    )


def withhold(conn: psycopg.Connection[Any], op: UUID, layer: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {WITHHELD} (maintenance_operation_id, layer, object_kind, schema_name,
                                object_name, column_name, grantee, privilege, grantable, grantor)
        VALUES (%s, %s, 'schema', 'tenant_synthetic', 'tenant_synthetic', NULL,
                'haloflow_runtime', 'USAGE', false, 'haloflow_provisioner')
        """,
        (op, layer),
    )


def count_events(admin: str, op: UUID, event: str) -> int:
    return int(
        _admin_value(
            admin,
            f"SELECT count(*) FROM {ATTEMPTS} WHERE maintenance_operation_id = %s AND event = %s",
            (op, event),
        )
    )


# --- detail payloads (plan v4 §9.3; the closed schema the validator accepts) ---

T = "2026-10-01T12:00:00+00:00"


def once_only_detail(event: str) -> dict[str, Any]:
    if event in ("l1_withheld", "l2_withheld", "l2b_withheld"):
        return {"tuple_count": 0}
    if event == "verified":
        return {
            "checks": ["ledger_checksums", "acl_phase5", "step5_subset", "t003_profile"],
            "t003_checksum": "a" * 64,
        }
    if event == "activated":
        raise AssertionError("use activated_detail(): it must name a real verified event")
    return {}


def activated_detail(conn: psycopg.Connection[Any], op: UUID, tenant_id: str) -> dict[str, Any]:
    """Event-ID domains: maintenance events are `tenant_maintenance_attempts.event_id`
    (uuid); history events are `tenant_state_history.event_id` (bigint). `activated`
    names the operation's real `verified` event by its uuid. Whether that link is
    enforced, and against which activation, is the controller's obligation (G5/G6,
    TC-V04, CP-5); here it only has to be a real event for TC-S04's index check."""

    append(conn, op, tenant_id, "verified", once_only_detail("verified"))
    row = conn.execute(
        f"SELECT event_id FROM {ATTEMPTS} "
        "WHERE maintenance_operation_id = %s AND event = 'verified'",
        (op,),
    ).fetchone()
    assert row is not None
    return {"verified_event_id": str(row[0])}


def n_detail(event: str, gen: int, admin: str, round_: int = 1) -> dict[str, Any]:
    if event == "neutralization_bootstrap_started":
        return {"gen": gen, "t_bootstrap_deadline": T}
    if event == "neutralization_started":
        return {
            "gen": gen,
            "t_deadline": T,
            "database_oid": int(
                _admin_value(
                    admin,
                    "SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()",
                )
            ),
            "s0": [],
            "membership_snapshot": [],
            "classification_counts": {
                "target": 0,
                "protected": 0,
                "other": 0,
                "out_of_scope": 0,
                "unknown": 0,
            },
        }
    if event == "neutralization_round_started":
        return {"gen": gen, "round": round_}
    if event == "neutralization_round_outcome":
        return {"gen": gen, "round": round_, "results": []}
    if event == "neutralized":
        return {"gen": gen}
    if event == "neutralization_stopped":
        return {"gen": gen, "phase": "rounds", "reason": "deadline_passed"}
    raise AssertionError(event)


# --- refusal oracle ------------------------------------------------------------


def refused(
    conn: psycopg.Connection[Any],
    statement: Callable[[psycopg.Connection[Any]], None],
    sqlstate: str,
    *,
    message: str | None = None,
    constraint: str | None = None,
) -> None:
    """Run `statement` in its own transaction and assert the exact refusal."""

    with pytest.raises(psycopg.Error) as caught, conn.transaction():
        statement(conn)
    error = caught.value
    assert error.sqlstate == sqlstate, (error.sqlstate, str(error))
    if message is not None:
        assert error.diag.message_primary == message
    if constraint is not None:
        assert error.diag.constraint_name == constraint


# --- TC-S01: trigger T2 and the one-open-operation index --------------------


@pytest.mark.parametrize(
    ("who", "column", "value"),
    [
        ("P", "t003_baseline", "failed:9"),
        ("OWNER", "maintenance_operation_id", str(uuid4())),
        ("OWNER", "tenant_id", ALT_TENANT),
        ("OWNER", "from_version", 1),
        ("OWNER", "to_version", 4),
        ("OWNER", "pre_entry_event_id", ALT_EVENT),
    ],
    ids=[
        "t003_baseline-P",
        "op_id-owner",
        "tenant-owner",
        "from-owner",
        "to-owner",
        "pre_entry-owner",
    ],
)
def test_tc_s01_identity_column_is_immutable(
    as_role: Any,
    migrated_database: str,
    tenant: str,
    m01_only_tenant: tuple[str, str],
    who: str,
    column: str,
    value: Any,
) -> None:
    """Every new value is otherwise valid (a real tenant, a real history event, a fresh
    uuid, positive versions, a well-formed baseline), so only the identity guard can
    refuse the update; removing that guard must let the update succeed (MU-01)."""

    if value is ALT_TENANT:
        value = m01_only_tenant[0]
    elif value is ALT_EVENT:
        value = _admin_value(
            migrated_database,
            "SELECT min(event_id) FROM shared.tenant_state_history WHERE tenant_id = %s",
            (m01_only_tenant[0],),
        )
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
    try:
        with as_role(who) as conn:
            refused(
                conn,
                lambda c: c.execute(
                    sql.SQL("UPDATE {} SET {} = %s WHERE maintenance_operation_id = %s").format(
                        sql.SQL(OPS), sql.Identifier(column)
                    ),
                    (value, op),
                ),
                RC09,
                message=MSG["identity"],
            )
    finally:
        # Only matters if the guard is missing and the row moved to the second tenant.
        _purge(migrated_database, m01_only_tenant[0])


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ((), "activated"),
        ((), "released"),
        (("entered",), "establishing"),
        (("entered", "activated"), "entered"),
        (("entered", "activated", "released"), "activated"),
        (("abandoned",), "establishing"),
        (("entered",), "abandoned"),
    ],
    ids=[
        "establishing-activated",
        "establishing-released",
        "entered-establishing",
        "activated-entered",
        "released-activated",
        "abandoned-establishing",
        "entered-abandoned",
    ],
)
def test_tc_s01_illegal_state_move(
    as_role: Any, migrated_database: str, tenant: str, path: tuple[str, ...], illegal: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        advance(p, op, *path)
        refused(p, lambda c: set_state(c, op, illegal), RC09, message=MSG["transition"])


def test_tc_s01_delete_refused_by_trigger_for_owner(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
    with as_role("OWNER") as owner:
        refused(
            owner,
            lambda c: c.execute(f"DELETE FROM {OPS} WHERE maintenance_operation_id = %s", (op,)),
            RC09,
            message=MSG["no_delete"],
        )


@pytest.mark.parametrize("who", ["P", "M"])
def test_tc_s01_delete_refused_by_privilege_for_p_and_m(
    as_role: Any, migrated_database: str, tenant: str, who: str
) -> None:
    """IP-15 reading: P and M are stopped by privilege before any trigger."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
    with as_role(who) as conn:
        refused(
            conn,
            lambda c: c.execute(f"DELETE FROM {OPS} WHERE maintenance_operation_id = %s", (op,)),
            RC11,
        )


def test_tc_s01_second_open_operation_refused_by_partial_unique_index(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    with as_role("P") as p:
        first = create_operation(p, migrated_database, tenant)
        refused(
            p,
            lambda c: create_operation(c, migrated_database, tenant),
            RC10,
            constraint=IDX["one_open"],
        )
        # Positive control: the index is partial; once the first is abandoned, a new one is allowed.
        set_state(p, first, "abandoned")
        create_operation(p, migrated_database, tenant)


# --- TC-S02: withheld layers --------------------------------------------------


@pytest.mark.parametrize(
    ("who", "layer"),
    [("P", "L2"), ("P", "L2b"), ("M", "L1")],
    ids=["P-L2", "P-L2b", "M-L1"],
)
def test_tc_s02_layer_written_by_wrong_role(
    as_role: Any, migrated_database: str, tenant: str, who: str, layer: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
    with as_role(who) as conn:
        refused(conn, lambda c: withhold(c, op, layer), RC09, message=MSG["layer_role"])


def test_tc_s02_each_layer_writable_by_its_role(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    """Positive control for the refusals above."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        withhold(p, op, "L1")
    with as_role("M") as m:
        withhold(m, op, "L2")
        withhold(m, op, "L2b")


@pytest.mark.parametrize(
    ("who", "layer", "closing_event"),
    [("P", "L1", "l1_withheld"), ("M", "L2", "l2_withheld"), ("M", "L2b", "l2b_withheld")],
    ids=["L1", "L2", "L2b"],
)
def test_tc_s02_insert_after_layer_withheld_event(
    as_role: Any, migrated_database: str, tenant: str, who: str, layer: str, closing_event: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(p, op, tenant, closing_event, {"tuple_count": 0})
    with as_role(who) as conn:
        refused(conn, lambda c: withhold(c, op, layer), RC09, message=MSG["layer_closed"])


@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_tc_s02_update_delete_refused_by_trigger_for_owner(
    as_role: Any, migrated_database: str, tenant: str, verb: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        withhold(p, op, "L1")
    statement = (
        f"UPDATE {WITHHELD} SET grantable = true WHERE maintenance_operation_id = %s"
        if verb == "UPDATE"
        else f"DELETE FROM {WITHHELD} WHERE maintenance_operation_id = %s"
    )
    with as_role("OWNER") as owner:
        refused(owner, lambda c: c.execute(statement, (op,)), RC09, message=MSG["append_only"])


@pytest.mark.parametrize(
    ("who", "verb"), [(w, v) for w in ("P", "M") for v in ("UPDATE", "DELETE")]
)
def test_tc_s02_update_delete_refused_by_privilege_for_p_and_m(
    as_role: Any, migrated_database: str, tenant: str, who: str, verb: str
) -> None:
    """IP-15 reading."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        withhold(p, op, "L1")
    statement = (
        f"UPDATE {WITHHELD} SET grantable = true WHERE maintenance_operation_id = %s"
        if verb == "UPDATE"
        else f"DELETE FROM {WITHHELD} WHERE maintenance_operation_id = %s"
    )
    with as_role(who) as conn:
        refused(conn, lambda c: c.execute(statement, (op,)), RC11)


# --- TC-S03: attempts append-only -------------------------------------------


@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_tc_s03_attempts_update_delete_refused_by_trigger_for_owner(
    as_role: Any, migrated_database: str, tenant: str, verb: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(p, op, tenant, "op_created")
    statement = (
        f"UPDATE {ATTEMPTS} SET event = 'completed' WHERE maintenance_operation_id = %s"
        if verb == "UPDATE"
        else f"DELETE FROM {ATTEMPTS} WHERE maintenance_operation_id = %s"
    )
    with as_role("OWNER") as owner:
        refused(owner, lambda c: c.execute(statement, (op,)), RC09, message=MSG["append_only"])


@pytest.mark.parametrize(
    ("who", "verb"), [(w, v) for w in ("P", "M") for v in ("UPDATE", "DELETE")]
)
def test_tc_s03_attempts_update_delete_refused_by_privilege_for_p_and_m(
    as_role: Any, migrated_database: str, tenant: str, who: str, verb: str
) -> None:
    """IP-15 reading of 'UPDATE or DELETE by P or M'."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(p, op, tenant, "op_created")
    statement = (
        f"UPDATE {ATTEMPTS} SET event = 'completed' WHERE maintenance_operation_id = %s"
        if verb == "UPDATE"
        else f"DELETE FROM {ATTEMPTS} WHERE maintenance_operation_id = %s"
    )
    with as_role(who) as conn:
        refused(conn, lambda c: c.execute(statement, (op,)), RC11)


# --- TC-S04: once-only events ------------------------------------------------


@pytest.mark.parametrize("event", [e for e in ONCE_ONLY if e != "apply_applied"])
def test_tc_s04_once_only_event_twice_rolls_back_whole_transaction(
    as_role: Any, migrated_database: str, tenant: str, event: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        detail = (
            activated_detail(p, op, tenant) if event == "activated" else once_only_detail(event)
        )

        def twice(c: psycopg.Connection[Any]) -> None:
            append(c, op, tenant, event, detail)
            append(c, op, tenant, event, detail)

        refused(p, twice, RC10, constraint=IDX["once_only"])
    assert count_events(migrated_database, op, event) == 0


# --- TC-S05: N event keys ----------------------------------------------------


@pytest.mark.parametrize(
    ("event", "index"),
    [
        ("neutralization_bootstrap_started", "gen"),
        ("neutralization_started", "gen"),
        ("neutralization_stopped", "gen"),
        ("neutralization_round_started", "gen_round"),
        ("neutralization_round_outcome", "gen_round"),
    ],
)
def test_tc_s05_duplicate_n_event_key(
    as_role: Any, migrated_database: str, tenant: str, event: str, index: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        if event == "neutralization_stopped":
            # The N-terminal trigger needs bootstrap_started{g} before a stop (TC-S07).
            append(
                p,
                op,
                tenant,
                "neutralization_bootstrap_started",
                n_detail("neutralization_bootstrap_started", 1, migrated_database),
            )
        append(p, op, tenant, event, n_detail(event, 1, migrated_database))
        refused(
            p,
            lambda c: append(c, op, tenant, event, n_detail(event, 1, migrated_database)),
            RC10,
            constraint=IDX[index],
        )
        # Positive control: the same event under a different key is accepted.
        if index == "gen_round":
            append(p, op, tenant, event, n_detail(event, 1, migrated_database, round_=2))
        elif event != "neutralization_stopped":
            append(p, op, tenant, event, n_detail(event, 2, migrated_database))


def test_tc_s05_neutralized_for_a_second_generation(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(p, op, tenant, "neutralized", n_detail("neutralized", 1, migrated_database))
        refused(
            p,
            lambda c: append(
                c, op, tenant, "neutralized", n_detail("neutralized", 2, migrated_database)
            ),
            RC10,
            constraint=IDX["neutralized_once"],
        )


# --- TC-S06 / TC-S07: the N-terminal trigger -----------------------------------

N_AFTER_STOP = (
    "neutralization_bootstrap_started",
    "neutralization_started",
    "neutralization_round_started",
    "neutralization_round_outcome",
    "neutralized",
)


@pytest.mark.parametrize("event", N_AFTER_STOP)
def test_tc_s06_n_event_after_stop_for_that_generation(
    as_role: Any, migrated_database: str, tenant: str, event: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(
            p,
            op,
            tenant,
            "neutralization_bootstrap_started",
            n_detail("neutralization_bootstrap_started", 1, migrated_database),
        )
        append(
            p,
            op,
            tenant,
            "neutralization_stopped",
            n_detail("neutralization_stopped", 1, migrated_database),
        )
        refused(
            p,
            lambda c: append(c, op, tenant, event, n_detail(event, 1, migrated_database, round_=5)),
            RC09,
            message=MSG["n_stopped"],
        )


def test_tc_s06_other_generation_is_not_stopped(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    """Positive control: the stop is terminal for its generation only."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(
            p,
            op,
            tenant,
            "neutralization_bootstrap_started",
            n_detail("neutralization_bootstrap_started", 1, migrated_database),
        )
        append(
            p,
            op,
            tenant,
            "neutralization_stopped",
            n_detail("neutralization_stopped", 1, migrated_database),
        )
        append(
            p,
            op,
            tenant,
            "neutralization_bootstrap_started",
            n_detail("neutralization_bootstrap_started", 2, migrated_database),
        )


def test_tc_s07_stop_after_neutralized(as_role: Any, migrated_database: str, tenant: str) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(
            p,
            op,
            tenant,
            "neutralization_bootstrap_started",
            n_detail("neutralization_bootstrap_started", 1, migrated_database),
        )
        append(p, op, tenant, "neutralized", n_detail("neutralized", 1, migrated_database))
        refused(
            p,
            lambda c: append(
                c,
                op,
                tenant,
                "neutralization_stopped",
                n_detail("neutralization_stopped", 1, migrated_database),
            ),
            RC09,
            message=MSG["stop_after_neutralized"],
        )


def test_tc_s07_stop_without_bootstrap_started(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        refused(
            p,
            lambda c: append(
                c,
                op,
                tenant,
                "neutralization_stopped",
                n_detail("neutralization_stopped", 1, migrated_database),
            ),
            RC09,
            message=MSG["stop_without_bootstrap"],
        )


# --- TC-S09: OD-C3 history column SELECT for P ---------------------------------


def test_tc_s09_provisioner_selects_exactly_five_history_columns(
    as_role: Any, migrated_database: str
) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as admin:
        rows = admin.execute(
            """
            SELECT a.attname,
                   pg_catalog.has_column_privilege(%s, 'shared.tenant_state_history',
                                                   a.attname, 'SELECT')
              FROM pg_catalog.pg_attribute AS a
             WHERE a.attrelid = 'shared.tenant_state_history'::regclass
               AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (PROVISIONER_ROLE,),
        ).fetchall()
    assert {name for name, allowed in rows if allowed} == HISTORY_SELECTABLE
    with as_role("P") as p:
        p.execute(
            "SELECT tenant_id, event_id, new_state, reason_code, execution_id "
            "FROM shared.tenant_state_history LIMIT 0"
        )
        for column in sorted({name for name, _ in rows} - HISTORY_SELECTABLE):
            refused(
                p,
                lambda c, col=column: c.execute(
                    sql.SQL("SELECT {} FROM shared.tenant_state_history LIMIT 0").format(
                        sql.Identifier(col)
                    )
                ),
                RC11,
            )


# --- TC-S11: once-only apply_applied (A6) ---------------------------------------

SENTINEL_SCHEMA = "l6_fx"
SENTINEL = f"{SENTINEL_SCHEMA}.sentinel"


@pytest.fixture
def fx_sentinel(migrated_database: str) -> Iterator[str]:
    """FX-sentinel (v4 §0.1): a scratch table the migrator may write. Test database only."""

    with psycopg.connect(migrated_database, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA IF EXISTS {SENTINEL_SCHEMA} CASCADE")
        admin.execute(f"CREATE SCHEMA {SENTINEL_SCHEMA}")
        admin.execute(f"CREATE TABLE {SENTINEL} (marker uuid NOT NULL)")
        admin.execute(f"GRANT USAGE ON SCHEMA {SENTINEL_SCHEMA} TO {MIGRATOR_ROLE}")
        admin.execute(f"GRANT SELECT, INSERT ON {SENTINEL} TO {MIGRATOR_ROLE}")
    try:
        yield SENTINEL
    finally:
        with psycopg.connect(migrated_database, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {SENTINEL_SCHEMA} CASCADE")


def test_tc_s11_apply_applied_is_once_only_and_aborts_the_transaction(
    as_role: Any, migrated_database: str, tenant: str, fx_sentinel: str
) -> None:
    with as_role("P") as p:
        op_a = create_operation(p, migrated_database, tenant)
        set_state(p, op_a, "abandoned")  # one open operation per tenant; opB is the open one
        op_b = create_operation(p, migrated_database, tenant)
    marker = uuid4()
    with as_role("M") as m:
        append(m, op_a, tenant, "apply_applied", attempt=uuid4())

        def sentinel_then_duplicate(c: psycopg.Connection[Any]) -> None:
            c.execute(f"INSERT INTO {fx_sentinel} (marker) VALUES (%s)", (marker,))
            append(c, op_a, tenant, "apply_applied", attempt=uuid4())

        refused(m, sentinel_then_duplicate, RC10, constraint=IDX["once_only"])
        assert (
            _admin_value(
                migrated_database,
                f"SELECT count(*) FROM {fx_sentinel} WHERE marker = %s",
                (marker,),
            )
            == 0
        )
        assert count_events(migrated_database, op_a, "apply_applied") == 1
        append(m, op_b, tenant, "apply_applied", attempt=uuid4())  # positive control
    assert count_events(migrated_database, op_b, "apply_applied") == 1


# --- TC-V08: M's state changes (OD-D1) ------------------------------------------


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ((), "abandoned"),
        ((), "entered"),
        (("entered",), "activated"),
        (("entered", "activated"), "abandoned"),
        (("entered", "activated"), "entered"),
    ],
    ids=[
        "establishing-abandoned",
        "establishing-entered",
        "entered-activated",
        "activated-abandoned",
        "activated-entered",
    ],
)
def test_tc_v08_migrator_state_change_other_than_activated_to_released(
    as_role: Any, migrated_database: str, tenant: str, path: tuple[str, ...], target: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        advance(p, op, *path)
    with as_role("M") as m:
        refused(m, lambda c: set_state(c, op, target), RC09, message=MSG["migrator_state"])


def test_tc_v08_migrator_may_release_an_activated_operation(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    """Positive control for TC-V08 (OD-D1)."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        advance(p, op, "entered", "activated")
    with as_role("M") as m:
        set_state(m, op, "released")
    assert (
        _admin_value(
            migrated_database, f"SELECT state FROM {OPS} WHERE maintenance_operation_id = %s", (op,)
        )
        == "released"
    )


# =============================================================================
# [A1] IP-16 (owner decision 2026-10-01, amendment draft v2 `2068f441...de48`):
# TC-S13 to TC-S17, the closed `detail` contract and operation association.
#
# Everything above this banner is unchanged from the 72-node packet, so its line
# numbers stay valid. The validator domains below are the values that plan v4
# addendum 1 r3 Part B item 4 leaves to be "pinned at freeze"; they are part of
# the frozen contract. Order of the attempts-table insert checks (pinned): the
# detail validation runs before the operation-association check, and both run
# before the N-terminal and stop checks; unique indexes are checked after all
# BEFORE INSERT checks (PostgreSQL behaviour).
# =============================================================================

MSG_A1 = {
    "invalid_detail": "tenant_maintenance_attempts: invalid detail",
    "invalid_association": "tenant_maintenance_attempts: invalid operation association",
}
IDX_A1 = {"per_attempt": "tenant_maintenance_attempts_per_attempt"}
RC_JSON = "22P02"  # invalid_text_representation, raised by the jsonb input parser

COUNTER_MAX = 999_999_999  # r3 Part B item 2
R_MAX = 8  # architecture v6r3 §6b
L_SNAPSHOT = 4096  # r3 Part B item 3

ENUMS: dict[str, tuple[str, ...]] = {
    "claim": ("CL", "CLc"),
    "stop_phase": ("bootstrap", "rounds"),
    "stop_reason": (
        "bootstrap_deadline_passed",
        "contradictory_classification",
        "deployment_precondition_failed",
        "membership_snapshot_too_large",
        "deadline_passed",
        "budget_exhausted",
    ),
    "result": (
        "terminated",
        "timeout",
        "gone",
        "identity_changed",
        "budget_exhausted",
        "lock_lost",
    ),
    # SanitizedErrorCode at base 0367601 (haloflow.m01.provisioning.codes), pinned here.
    "sanitized_error_code": (
        "SCHEMA_CREATE_FAILED",
        "MIGRATION_DDL_FAILED",
        "MIGRATION_COMMIT_FAILED",
        "MIGRATION_CHECKSUM_DRIFT",
        "LEDGER_WRITE_FAILED",
        "LOCK_UNAVAILABLE",
        "GRANT_APPLY_FAILED",
        "SCHEMA_ACL_MISMATCH",
        "VERIFICATION_FAILED",
        "REGISTRY_WRITE_FAILED",
    ),
    "point": ("a", "b", "c", "d"),  # requirements addendum 4 r3, T-2 named points
    "kind": ("registry", "acl", "ledger", "other"),
    "refusal_code": (  # r3 A2
        "LOCK_UNAVAILABLE",
        "MAINTENANCE_FENCE_LOST",
        "MAINTENANCE_LOCK_LOST",
        "MAINTENANCE_TOKEN_INVALID",
        "MAINTENANCE_CASE_REFUSED",
        "MAINTENANCE_STATE_UNKNOWN",
        "MAINTENANCE_CLAIM_REFUSED",
        "MAINTENANCE_N_REFUSED",
        "MAINTENANCE_ESTABLISHMENT_INCOMPLETE",
        "MAINTENANCE_DRAIN_TIMEOUT",
        "MAINTENANCE_VERIFICATION_FAILED",
    ),
    "refusal_phase": (  # r3 A2
        "lock_acquire",
        "capability",
        "fence",
        "classify",
        "claim",
        "establish",
        "neutralize",
        "drain",
        "exclusion",
        "apply",
        "verify",
        "activate",
        "restore",
        "finalize",
    ),
}
# `verified.checks`: exactly these four codes, each once, in any order (V runs all four).
CHECKS = ("ledger_checksums", "acl_phase5", "step5_subset", "t003_profile")

# Field kinds. Domains (pinned):
#   counter  JSON integer 1..999,999,999 (text form ^[1-9][0-9]{0,8}$); gen, round, ledger_attempt
#   from     JSON integer 1..999,999,998, so that `to` (the new generation) stays within the
#            counter range; a restart from generation 999,999,999 is refused (TC-K09 boundary)
#   next     `to` = `from` + 1
#   int0     JSON integer 0..2,147,483,647
#   pid      JSON integer 1..2,147,483,647
#   oid      JSON integer 1..4,294,967,295
#   ts       JSON string matching
#            ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$
#            and a valid timestamptz
#   uuid     JSON string, canonical lowercase 8-4-4-4-12 hex; uuid_null also allows JSON null
#   enum     JSON string in ENUMS[name], exact case
#   ref      JSON string, ^[A-Za-z0-9._:-]{1,64}$
#   hex64    JSON string, ^[0-9a-f]{64}$
#   checks   JSON array equal as a set to CHECKS, no duplicates
#   s0, results   JSON arrays of closed objects; length 0..max_connections / 0..R_MAX
#   snapshot JSON array of closed {role_oid, member_oids}; role_oid strictly ascending;
#            member_oids strictly ascending; total member OIDs 0..L_SNAPSHOT
#   counts   closed object of five int0 keys
INDEXED = ("gen", "round", "ledger_attempt")
S0_ELEM = {"pid": "pid", "datid": "oid", "usesysid": "oid", "backend_start": "ts"}
RESULT_ELEM = {"pid": "pid", "backend_start": "ts", "result": "enum:result"}
COUNTS = ("target", "protected", "other", "out_of_scope", "unknown")

NO_DETAIL = (
    "started",
    "op_created",
    "drained",
    "exclusion_established",
    "lo_granted",
    "apply_applied",
    "l1_restored",
    "l2_l2b_restored",
    "released",
    "completed",
    "abandoned",
)
DETAIL_SCHEMA: dict[str, dict[str, str]] = {event: {} for event in NO_DETAIL}
DETAIL_SCHEMA.update(
    {
        "claimed": {"claim": "enum:claim", "prior_attempt": "uuid_null"},
        "l1_withheld": {"tuple_count": "int0"},
        "l2_withheld": {"tuple_count": "int0"},
        "l2b_withheld": {"tuple_count": "int0"},
        "neutralization_bootstrap_started": {"gen": "counter", "t_bootstrap_deadline": "ts"},
        "neutralization_started": {
            "gen": "counter",
            "t_deadline": "ts",
            "database_oid": "oid",
            "s0": "s0",
            "membership_snapshot": "snapshot",
            "classification_counts": "counts",
        },
        "neutralization_round_started": {"gen": "counter", "round": "counter"},
        "neutralization_round_outcome": {
            "gen": "counter",
            "round": "counter",
            "results": "results",
        },
        "neutralized": {"gen": "counter"},
        "neutralization_restarted": {"from": "from", "to": "next", "ref": "ref"},
        "neutralization_stopped": {
            "gen": "counter",
            "phase": "enum:stop_phase",
            "reason": "enum:stop_reason",
        },
        "apply_running": {"ledger_attempt": "counter"},
        "apply_outcome_unrecorded": {"ledger_attempt": "counter"},
        "apply_failed": {
            "ledger_attempt": "counter",
            "sanitized_error_code": "enum:sanitized_error_code",
        },
        "verified": {"checks": "checks", "t003_checksum": "hex64"},
        "activated": {"verified_event_id": "uuid"},
        "interference_detected": {"point": "enum:point", "kind": "enum:kind"},
        "reconciled_by_owner": {"ref": "ref"},
        "attempt_refused": {"code": "enum:refusal_code", "phase": "enum:refusal_phase"},
    }
)
NULL_OPERATION_ALLOWED = ("started", "interference_detected", "attempt_refused")  # plan §9.3, r3 A3

U1 = "00000000-0000-4000-8000-000000000001"
SAMPLE: dict[str, Any] = {
    "counter": 1,
    "from": 1,
    "int0": 3,
    "pid": 4242,
    "oid": 16384,
    "ts": T,
    "uuid": U1,
    "uuid_null": U1,
    "ref": "rec-2026-10-01:a94b2f62",
    "hex64": "a" * 64,
}


def _sample(kind: str) -> Any:
    if kind.startswith("enum:"):
        return ENUMS[kind[5:]][0]
    if kind == "checks":
        return list(CHECKS)
    if kind == "s0":
        return [{"pid": 4242, "datid": 16384, "usesysid": 16385, "backend_start": T}]
    if kind == "results":
        return [{"pid": 4242, "backend_start": T, "result": "terminated"}]
    if kind == "snapshot":
        return [{"role_oid": 16385, "member_oids": [16390, 16391]}]
    if kind == "counts":
        return {key: 0 for key in COUNTS}
    if kind == "next":
        return 2
    return SAMPLE[kind]


def valid_detail(event: str) -> dict[str, Any]:
    return {key: _sample(kind) for key, kind in DETAIL_SCHEMA[event].items()}


MISSING = object()
# Wrong JSON type, per kind (the value's JSON type is not the kind's type).
WRONG_TYPE: dict[str, list[tuple[str, Any]]] = {
    "counter": [("type_string", "1"), ("type_bool", True)],
    "from": [("type_string", "1"), ("type_bool", True)],
    "int0": [("type_string", "3"), ("type_bool", True)],
    "pid": [("type_string", "4242")],
    "oid": [("type_string", "16384")],
    "ts": [("type_number", 1)],
    "uuid": [("type_number", 1)],
    "uuid_null": [("type_number", 1)],
    "enum": [("type_number", 1)],
    "ref": [("type_number", 1)],
    "hex64": [("type_number", 1)],
    "checks": [("type_object", {})],
    "s0": [("type_object", {})],
    "results": [("type_object", {})],
    "snapshot": [("type_object", {})],
    "counts": [("type_array", [])],
    "next": [("type_string", "2")],
}
# Domain violations, per kind (the JSON type is right, the value is outside the domain).
DOMAIN: dict[str, list[tuple[str, Any]]] = {
    "counter": [("zero", 0), ("over_max", COUNTER_MAX + 1), ("fraction", 1.5)],
    "from": [("zero", 0), ("at_counter_max", COUNTER_MAX), ("fraction", 1.5)],
    "int0": [("negative", -1), ("fraction", 1.5), ("over_int4", 2_147_483_648)],
    "pid": [("zero", 0), ("over_int4", 2_147_483_648)],
    "oid": [("zero", 0), ("over_uint32", 4_294_967_296)],
    "ts": [("no_zone", "2026-10-01T12:00:00"), ("bad_month", "2026-13-01T12:00:00+00:00")],
    "uuid": [("not_uuid", "not-a-uuid"), ("uppercase", U1.upper().replace("0", "A", 1))],
    "uuid_null": [("not_uuid", "not-a-uuid"), ("no_hyphens", U1.replace("-", ""))],
    "ref": [("empty", ""), ("space", "rec 1"), ("too_long", "r" * 65)],
    "hex64": [("short", "a" * 63), ("uppercase", "A" * 64), ("non_hex", "g" * 64)],
    "checks": [
        ("one_missing", list(CHECKS[:3])),
        ("duplicate", [*CHECKS, CHECKS[0]]),
        ("unknown_code", [*CHECKS[:3], "other"]),
    ],
    "snapshot": [
        (
            "role_unsorted",
            [{"role_oid": 16386, "member_oids": [1]}, {"role_oid": 16385, "member_oids": [2]}],
        ),
        (
            "role_duplicate",
            [{"role_oid": 16385, "member_oids": [1]}, {"role_oid": 16385, "member_oids": [2]}],
        ),
        ("member_unsorted", [{"role_oid": 16385, "member_oids": [16391, 16390]}]),
        ("member_duplicate", [{"role_oid": 16385, "member_oids": [16390, 16390]}]),
        ("member_not_oid", [{"role_oid": 16385, "member_oids": [0]}]),
    ],
    "next": [("equals_from", 1), ("skips", 3)],
}
# Wrong value for one key inside each element of an array of closed objects, or of `counts`.
NESTED_BAD = {"pid": ("zero", 0), "oid": ("zero", 0), "ts": ("no_zone", "2026-10-01T12:00:00")}


def _kind_base(kind: str) -> str:
    return "enum" if kind.startswith("enum:") else kind


def _top_variants(event: str, key: str, kind: str) -> list[tuple[str, Any]]:
    base = _kind_base(kind)
    out: list[tuple[str, Any]] = [("missing", MISSING)]
    if kind != "uuid_null":
        out.append(("null", None))
    out += WRONG_TYPE[base]
    if base == "enum":
        member = ENUMS[kind[5:]][0]
        out += [("not_member", "not_a_member"), ("case_changed", member.swapcase())]
    else:
        out += DOMAIN.get(base, [])
    return out


def _nested_variants(kind: str) -> list[tuple[str, Any]]:
    """Variants of the first element (arrays) or of the object itself (`counts`)."""

    out: list[tuple[str, Any]] = []
    if kind in ("s0", "results"):
        elem_schema = S0_ELEM if kind == "s0" else RESULT_ELEM
        elem = _sample(kind)[0]
        out.append(("elem_not_object", [1]))
        out.append(("elem_extra_key", [{**elem, "extra": 0}]))
        for sub, sub_kind in elem_schema.items():
            out.append((f"elem_{sub}_missing", [{k: v for k, v in elem.items() if k != sub}]))
            out.append((f"elem_{sub}_null", [{**elem, sub: None}]))
            if sub_kind.startswith("enum:"):
                out.append((f"elem_{sub}_not_member", [{**elem, sub: "not_a_member"}]))
            else:
                name, bad = NESTED_BAD[sub_kind]
                out.append((f"elem_{sub}_{name}", [{**elem, sub: bad}]))
                if sub_kind == "ts":
                    out.append((f"elem_{sub}_type_number", [{**elem, sub: 1}]))
                else:
                    out.append((f"elem_{sub}_type_string", [{**elem, sub: str(elem[sub])}]))
    elif kind == "snapshot":
        entry = _sample(kind)[0]
        out.append(("elem_not_object", [1]))
        out.append(("elem_extra_key", [{**entry, "extra": 0}]))
        out.append(("elem_role_oid_missing", [{"member_oids": entry["member_oids"]}]))
        out.append(("elem_member_oids_missing", [{"role_oid": entry["role_oid"]}]))
        out.append(("elem_role_oid_type_string", [{**entry, "role_oid": "16385"}]))
        out.append(("elem_member_oids_type_object", [{**entry, "member_oids": {}}]))
        out.append(("elem_member_oid_type_string", [{**entry, "member_oids": ["16390"]}]))
    elif kind == "counts":
        counts = _sample(kind)
        out.append(("extra_key", {**counts, "extra": 0}))
        for sub in COUNTS:
            out.append((f"{sub}_missing", {k: v for k, v in counts.items() if k != sub}))
            out.append((f"{sub}_null", {**counts, sub: None}))
            out.append((f"{sub}_negative", {**counts, sub: -1}))
            out.append((f"{sub}_type_string", {**counts, sub: "0"}))
    elif kind == "checks":
        out.append(("elem_type_number", [1, *CHECKS[1:]]))
    return out


def s13_invalid_cases() -> list[Any]:
    """Every S13 refusal variant. Indexed counters (gen, round, ledger_attempt) are
    exercised in TC-S14 instead; `from` is a counter that no index reads, so it stays here."""

    params = []
    for event, schema in DETAIL_SCHEMA.items():
        params.append(
            pytest.param(event, {**valid_detail(event), "extra": 0}, id=f"{event}-extra_key")
        )
        for key, kind in schema.items():
            if key in INDEXED:
                continue
            for name, value in _top_variants(event, key, kind):
                detail = valid_detail(event)
                if value is MISSING:
                    del detail[key]
                else:
                    detail[key] = value
                params.append(pytest.param(event, detail, id=f"{event}-{key}-{name}"))
            for name, value in _nested_variants(kind):
                detail = valid_detail(event)
                detail[key] = value
                params.append(pytest.param(event, detail, id=f"{event}-{key}-{name}"))
    return params


def seed_prerequisites(
    conn: psycopg.Connection[Any], op: UUID, tenant_id: str, event: str, gen: Any = 1
) -> None:
    """The one approved prerequisite for a valid append: a stop needs bootstrap_started{g}."""

    if event == "neutralization_stopped":
        append(
            conn,
            op,
            tenant_id,
            "neutralization_bootstrap_started",
            {"gen": gen, "t_bootstrap_deadline": T},
        )


def stored_details(admin: str, op: UUID, event: str) -> list[Any]:
    with psycopg.connect(admin, autocommit=True) as conn:
        rows = conn.execute(
            f"SELECT detail::text FROM {ATTEMPTS} "
            "WHERE maintenance_operation_id = %s AND event = %s ORDER BY detail::text",
            (op, event),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def append_raw(
    conn: psycopg.Connection[Any], op: UUID | None, tenant_id: str, event: str, text: str
) -> None:
    """Send `detail` as raw text, so invalid JSON reaches the server's jsonb parser."""

    conn.execute(
        f"""
        INSERT INTO {ATTEMPTS} (attempt_id, maintenance_operation_id, tenant_id, event, detail)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        (uuid4(), op, tenant_id, event, text),
    )


# --- TC-S13: the closed detail schema (D-1) -----------------------------------


@pytest.mark.parametrize("event", sorted(DETAIL_SCHEMA))
def test_tc_s13_valid_detail_accepted_and_stored_unchanged(
    as_role: Any, migrated_database: str, tenant: str, event: str
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        seed_prerequisites(p, op, tenant, event)
        detail = valid_detail(event)
        append(p, op, tenant, event, detail)
    assert stored_details(migrated_database, op, event) == [detail]


def test_tc_s13_claimed_prior_attempt_null_accepted(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    """Accepted-null control: `prior_attempt` is the one nullable field (plan §9.3)."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        detail = {"claim": "CLc", "prior_attempt": None}
        append(p, op, tenant, "claimed", detail)
    assert stored_details(migrated_database, op, "claimed") == [detail]


@pytest.mark.parametrize(("event", "detail"), s13_invalid_cases())
def test_tc_s13_invalid_detail_refused(
    as_role: Any, migrated_database: str, tenant: str, event: str, detail: dict[str, Any]
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        seed_prerequisites(p, op, tenant, event)
        refused(
            p,
            lambda c: append(c, op, tenant, event, detail),
            RC09,
            message=MSG_A1["invalid_detail"],
        )
    assert count_events(migrated_database, op, event) == 0


# --- TC-S14: indexed counters, range and overflow (D-5) -----------------------

COUNTER_SITES = [
    (event, key)
    for event in sorted(DETAIL_SCHEMA)
    for key in DETAIL_SCHEMA[event]
    if key in INDEXED
]
COUNTER_INVALID = [
    ("zero", 0),
    ("negative", -1),
    ("over_max", COUNTER_MAX + 1),
    ("string", "1"),
    ("fraction", 1.5),
    ("missing", MISSING),
    ("null", None),
    ("bool", True),
]


def _with_counter(event: str, key: str, value: Any) -> dict[str, Any]:
    detail = valid_detail(event)
    if value is MISSING:
        del detail[key]
    else:
        detail[key] = value
    return detail


@pytest.mark.parametrize("value", [1, COUNTER_MAX], ids=["one", "max"])
@pytest.mark.parametrize(
    ("event", "key"), COUNTER_SITES, ids=[f"{e}-{k}" for e, k in COUNTER_SITES]
)
def test_tc_s14_counter_bounds_accepted(
    as_role: Any, migrated_database: str, tenant: str, event: str, key: str, value: int
) -> None:
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        seed_prerequisites(p, op, tenant, event, gen=value if key == "gen" else 1)
        detail = _with_counter(event, key, value)
        append(p, op, tenant, event, detail)
    assert stored_details(migrated_database, op, event) == [detail]


@pytest.mark.parametrize(
    "value", [v for _, v in COUNTER_INVALID], ids=[n for n, _ in COUNTER_INVALID]
)
@pytest.mark.parametrize(
    ("event", "key"), COUNTER_SITES, ids=[f"{e}-{k}" for e, k in COUNTER_SITES]
)
def test_tc_s14_counter_invalid_refused_before_any_index(
    as_role: Any, migrated_database: str, tenant: str, event: str, key: str, value: Any
) -> None:
    """A valid twin with the counter at 1 is written first, so a validator that let a
    value through to the unique index would surface as 23505, not P0001."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        seed_prerequisites(p, op, tenant, event)
        append(p, op, tenant, event, valid_detail(event))
        refused(
            p,
            lambda c: append(c, op, tenant, event, _with_counter(event, key, value)),
            RC09,
            message=MSG_A1["invalid_detail"],
        )
    assert count_events(migrated_database, op, event) == 1


@pytest.mark.parametrize(
    ("event", "key"), COUNTER_SITES, ids=[f"{e}-{k}" for e, k in COUNTER_SITES]
)
def test_tc_s14_invalid_json_text_refused_by_parser(
    as_role: Any, migrated_database: str, tenant: str, event: str, key: str
) -> None:
    """A leading-zero number is not JSON: the jsonb input parser refuses it (22P02)
    before any trigger runs."""

    text = json.dumps(valid_detail(event)).replace(f'"{key}": 1', f'"{key}": 01', 1)
    assert f'"{key}": 01' in text
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        seed_prerequisites(p, op, tenant, event)
        refused(p, lambda c: append_raw(c, op, tenant, event, text), RC_JSON)
    assert count_events(migrated_database, op, event) == 0


# --- TC-S15: operation association (r3 A3) -------------------------------------


@pytest.fixture
def alt_tenant(m01_only_tenant: tuple[str, str], migrated_database: str) -> Iterator[str]:
    """The module's second real tenant, for a foreign operation; L-6 rows removed around use."""

    _purge(migrated_database, m01_only_tenant[0])
    yield m01_only_tenant[0]
    _purge(migrated_database, m01_only_tenant[0])


def count_null_operation_events(admin: str, tenant_id: str, event: str) -> int:
    return int(
        _admin_value(
            admin,
            f"SELECT count(*) FROM {ATTEMPTS} "
            "WHERE tenant_id = %s AND event = %s AND maintenance_operation_id IS NULL",
            (tenant_id, event),
        )
    )


@pytest.mark.parametrize("event", NULL_OPERATION_ALLOWED)
def test_tc_s15_null_operation_accepted(
    as_role: Any, migrated_database: str, tenant: str, event: str
) -> None:
    with as_role("P") as p:
        create_operation(p, migrated_database, tenant)
        append(p, None, tenant, event, valid_detail(event))
    assert count_null_operation_events(migrated_database, tenant, event) == 1


@pytest.mark.parametrize(
    "event", sorted(e for e in DETAIL_SCHEMA if e not in NULL_OPERATION_ALLOWED)
)
def test_tc_s15_null_operation_refused(
    as_role: Any, migrated_database: str, tenant: str, event: str
) -> None:
    with as_role("P") as p:
        create_operation(p, migrated_database, tenant)
        refused(
            p,
            lambda c: append(c, None, tenant, event, valid_detail(event)),
            RC09,
            message=MSG_A1["invalid_association"],
        )
    assert count_null_operation_events(migrated_database, tenant, event) == 0


@pytest.mark.parametrize("event", sorted(DETAIL_SCHEMA))
def test_tc_s15_operation_of_another_tenant_refused(
    as_role: Any, migrated_database: str, tenant: str, alt_tenant: str, event: str
) -> None:
    with as_role("P") as p:
        create_operation(p, migrated_database, tenant)
        foreign = create_operation(p, migrated_database, alt_tenant)
        refused(
            p,
            lambda c: append(c, foreign, tenant, event, valid_detail(event)),
            RC09,
            message=MSG_A1["invalid_association"],
        )
    assert count_events(migrated_database, foreign, event) == 0


# --- TC-S16: per-attempt uniqueness (architecture §3; r3 A5) --------------------


@pytest.mark.parametrize("event", ["started", "attempt_refused"])
def test_tc_s16_second_event_for_one_attempt_refused(
    as_role: Any, migrated_database: str, tenant: str, event: str
) -> None:
    attempt = uuid4()
    with as_role("P") as p:
        create_operation(p, migrated_database, tenant)
        append(p, None, tenant, event, valid_detail(event), attempt=attempt)
        refused(
            p,
            lambda c: append(c, None, tenant, event, valid_detail(event), attempt=attempt),
            RC10,
            constraint=IDX_A1["per_attempt"],
        )
    assert count_null_operation_events(migrated_database, tenant, event) == 1


@pytest.mark.parametrize("event", ["started", "attempt_refused"])
def test_tc_s16_same_event_for_a_different_attempt_accepted(
    as_role: Any, migrated_database: str, tenant: str, event: str
) -> None:
    with as_role("P") as p:
        create_operation(p, migrated_database, tenant)
        append(p, None, tenant, event, valid_detail(event), attempt=uuid4())
        append(p, None, tenant, event, valid_detail(event), attempt=uuid4())
    assert count_null_operation_events(migrated_database, tenant, event) == 2


def test_tc_s16_started_and_refusal_share_an_attempt(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    attempt = uuid4()
    with as_role("P") as p:
        create_operation(p, migrated_database, tenant)
        append(p, None, tenant, "started", {}, attempt=attempt)
        append(p, None, tenant, "attempt_refused", valid_detail("attempt_refused"), attempt=attempt)
    assert count_null_operation_events(migrated_database, tenant, "attempt_refused") == 1


# --- TC-S17: array bounds (R_max, L_snapshot, max_connections) -------------------


def _bounded(array: str, size: int) -> tuple[str, dict[str, Any]]:
    if array == "results":
        detail = valid_detail("neutralization_round_outcome")
        detail["results"] = [
            {"pid": 1000 + i, "backend_start": T, "result": "terminated"} for i in range(size)
        ]
        return "neutralization_round_outcome", detail
    detail = valid_detail("neutralization_started")
    if array == "membership_snapshot":
        first = size // 2
        detail["membership_snapshot"] = [
            {"role_oid": 16385, "member_oids": list(range(20_000, 20_000 + first))},
            {"role_oid": 16386, "member_oids": list(range(40_000, 40_000 + size - first))},
        ]
    else:
        detail["s0"] = [
            {"pid": 1000 + i, "datid": 16384, "usesysid": 16385, "backend_start": T}
            for i in range(size)
        ]
    return "neutralization_started", detail


def _bound(array: str, admin: str) -> int:
    if array == "results":
        return R_MAX
    if array == "membership_snapshot":
        return L_SNAPSHOT
    return int(_admin_value(admin, "SELECT current_setting('max_connections')::int"))


BOUNDED_ARRAYS = ("results", "membership_snapshot", "s0")


@pytest.mark.parametrize("array", BOUNDED_ARRAYS)
def test_tc_s17_array_at_bound_accepted_untruncated(
    as_role: Any, migrated_database: str, tenant: str, array: str
) -> None:
    event, detail = _bounded(array, _bound(array, migrated_database))
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(p, op, tenant, event, detail)
    assert stored_details(migrated_database, op, event) == [detail]


@pytest.mark.parametrize("array", BOUNDED_ARRAYS)
def test_tc_s17_array_above_bound_refused(
    as_role: Any, migrated_database: str, tenant: str, array: str
) -> None:
    event, detail = _bounded(array, _bound(array, migrated_database) + 1)
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        refused(
            p,
            lambda c: append(c, op, tenant, event, detail),
            RC09,
            message=MSG_A1["invalid_detail"],
        )
    assert count_events(migrated_database, op, event) == 0


# --- TC-S13 (packet v5): every enum member, root type, `checks` order ----------

ENUM_FIELDS = [
    ("claimed", ("claim",), "claim"),
    ("neutralization_stopped", ("phase",), "stop_phase"),
    ("neutralization_stopped", ("reason",), "stop_reason"),
    ("neutralization_round_outcome", ("results", 0, "result"), "result"),
    ("apply_failed", ("sanitized_error_code",), "sanitized_error_code"),
    ("interference_detected", ("point",), "point"),
    ("interference_detected", ("kind",), "kind"),
    ("attempt_refused", ("code",), "refusal_code"),
    ("attempt_refused", ("phase",), "refusal_phase"),
]


def _with_member(event: str, path: tuple[Any, ...], member: str) -> dict[str, Any]:
    detail = valid_detail(event)
    target: Any = detail
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = member
    return detail


ENUM_MEMBER_CASES = [
    pytest.param(
        event,
        _with_member(event, path, member),
        id=f"{event}-{'.'.join(str(x) for x in path)}-{member}",
    )
    for event, path, enum in ENUM_FIELDS
    for member in ENUMS[enum]
]


@pytest.mark.parametrize(("event", "detail"), ENUM_MEMBER_CASES)
def test_tc_s13_each_enum_member_accepted(
    as_role: Any, migrated_database: str, tenant: str, event: str, detail: dict[str, Any]
) -> None:
    """One valid field varied at a time: every approved member is accepted and stored."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        seed_prerequisites(p, op, tenant, event)
        append(p, op, tenant, event, detail)
    assert stored_details(migrated_database, op, event) == [detail]


def test_tc_s13_checks_in_another_order_accepted(
    as_role: Any, migrated_database: str, tenant: str
) -> None:
    detail = {**valid_detail("verified"), "checks": list(reversed(CHECKS))}
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        append(p, op, tenant, "verified", detail)
    assert stored_details(migrated_database, op, "verified") == [detail]


ROOT_NOT_OBJECT = [
    ("array", "[]"),
    ("string", '"x"'),
    ("number", "1"),
    ("boolean", "true"),
    ("null", "null"),
]


@pytest.mark.parametrize(
    "text", [t for _, t in ROOT_NOT_OBJECT], ids=[n for n, _ in ROOT_NOT_OBJECT]
)
@pytest.mark.parametrize("event", ["started", "claimed"], ids=["empty_family", "populated_family"])
def test_tc_s13_root_detail_not_an_object_refused(
    as_role: Any, migrated_database: str, tenant: str, event: str, text: str
) -> None:
    """The closed-object rule at the root: valid JSON that is not an object (JSON
    `null` included) is refused by the validator; nothing is written."""

    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        refused(
            p,
            lambda c: append_raw(c, op, tenant, event, text),
            RC09,
            message=MSG_A1["invalid_detail"],
        )
    assert count_events(migrated_database, op, event) == 0


# --- TC-S14 (packet v5): restart-generation boundary (D-5; TC-K09 at DB level) ---

RESTART_BOUNDARY = [
    ("last_allowed", COUNTER_MAX - 1, COUNTER_MAX, True),
    ("beyond_counter_max", COUNTER_MAX, COUNTER_MAX + 1, False),
]


@pytest.mark.parametrize(
    ("from_", "to", "accepted"),
    [(f, t, ok) for _, f, t, ok in RESTART_BOUNDARY],
    ids=[n for n, *_ in RESTART_BOUNDARY],
)
def test_tc_s14_restart_generation_boundary(
    as_role: Any, migrated_database: str, tenant: str, from_: int, to: int, accepted: bool
) -> None:
    detail = {**valid_detail("neutralization_restarted"), "from": from_, "to": to}
    with as_role("P") as p:
        op = create_operation(p, migrated_database, tenant)
        if accepted:
            append(p, op, tenant, "neutralization_restarted", detail)
        else:
            refused(
                p,
                lambda c: append(c, op, tenant, "neutralization_restarted", detail),
                RC09,
                message=MSG_A1["invalid_detail"],
            )
    expected = [detail] if accepted else []
    assert stored_details(migrated_database, op, "neutralization_restarted") == expected
