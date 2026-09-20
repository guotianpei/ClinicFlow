"""CP2-1 baseline -- what the runner does today between render and execute.

WHAT THIS MODULE CLAIMS, AND WHAT IT DOES NOT
---------------------------------------------
Every case here is GREEN: it pins behaviour the shipping runner already has, so
a CP2-1 implementation cannot change it by accident. There are **no red rows**,
and that is a deliberate correction rather than an omission.

An earlier draft carried three. All three are withdrawn:

  `B-nul-bound` x2   WITHDRAWN. They asserted a new refusal for a NUL in a
                     LEGACY ORDINARY unit template. `EX-01` in
                     `fixtures/function_policy/cases.json` records that exact
                     case:

                         rule      "V3a legacy NUL gap"
                         edit      "legacy unit template contains NUL"
                         expected  "out of scope by owner decision;
                                    no new rejection assertion or DB execution"
                         partition "recorded_exclusions"

                     The tests asserted precisely the new rejection that
                     exclusion rules out, and would have driven an
                     implementation broadening the policy to all ordinary SQL.
                     Bound-plan NUL coverage is PENDING until the typed function
                     path exists to express it; the legacy exclusion stands.

  `B-legacy-bypass`  WITHDRAWN. It asserted that a unit whose template creates a
                     function must not execute. `OD-04` deliberately installs
                     the approved registry rejector through an ordinary,
                     migrator-owned `SECURITY INVOKER` function OUTSIDE CP1, so
                     a blanket refusal of ordinary `CREATE FUNCTION` would break
                     an authorized route. The test also observed neither policy
                     invocation nor typed classification: it asserted an
                     exception and the absence of one exact string, which any
                     refusal for any reason would satisfy. PENDING until the
                     unauthorized-definition-to-legacy-route boundary is
                     specified.

The lesson is recorded here because it is the substantive one: an executable
test that contradicts a recorded owner decision does not become correct by
being executable.

HOW THESE TESTS REACH THE RUNNER
--------------------------------
Through `TenantMigrationRunner(connect=...)` and `manifest=...`, both production
constructor parameters, via the `migration_driver` fixture. No production module
is patched, wrapped, or given a test-only argument. The runner that executes
here is the runner that ships.

The first two cases are POSITIVE CONTROLS. Every later assertion of the form
"nothing was issued" would pass vacuously against a recorder the runner never
touches, so the controls establish that it does.

WHAT THE HARNESS CANNOT ESTABLISH
---------------------------------
It records that a call was made. It does not establish that SQL was valid, that
a privilege was enforced, that a transaction durably committed, or what a server
would do with any particular byte. `txn-commit` in the trace means a context
manager exited cleanly. Every claim of that kind belongs to the `D` layer on
PostgreSQL 17 and is out of scope here.
"""

from __future__ import annotations

import dataclasses

import pytest

from haloflow.m01.errors import MigrationUnitRejected, TenantMigrationFailed
from haloflow.m01.provisioning.codes import PreconditionCode, SanitizedErrorCode
from haloflow.m01.provisioning.manifest import (
    ExecutionRoleProfile,
    ProvisioningManifest,
    load_provisioning_manifest,
)
from haloflow.m01.provisioning.runner import (
    MIGRATION_LOCK_NAMESPACE,
    tenant_lock_key,
)
from haloflow.m01.provisioning.units import (
    TenantMigrationRegistry,
    UnitDefinition,
    build_tenant_migration_registry,
)

TENANT = "clinic-a"
SCHEMA = "tenant_aaaaaaaa"

ORDINARY_SQL = "CREATE TABLE {schema}.cp2_probe (id int);"
FUNCTION_SQL = (
    "CREATE FUNCTION {schema}.cp2_probe_fn() RETURNS int "
    "LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$;"
)
MODULE_ROLE = "haloflow_module_x"
MIGRATOR = "haloflow_migrator"


def registry(template: str, *, migration_id: str = "t001_test_cp2") -> TenantMigrationRegistry:
    """A real registry through the real public builder.

    `build_tenant_migration_registry` with `allow_test_units=True` is the
    sanctioned test path (R-E12): the unit passes every construction control a
    production unit passes -- the issuer sentinel, the id grammar, the non-empty
    template check, the `{schema}` placeholder requirement. No
    `TenantMigrationUnit` is constructed directly and no payload key is injected.
    """

    return build_tenant_migration_registry({migration_id: template}, allow_test_units=True)


def role_registry(template: str, *, role: str = MODULE_ROLE) -> TenantMigrationRegistry:
    """A registry whose one unit declares an execution role, through the builder.

    `UnitDefinition` is the public record for a unit that needs to say more than
    its template; `approved_execution_roles` is the composition-root parameter.
    Both controls run exactly as they do at startup.
    """

    return build_tenant_migration_registry(
        {"t001_test_cp2_role": UnitDefinition(template, execution_role=role)},
        approved_execution_roles=frozenset({role}),
        allow_test_units=True,
    )


def manifest_declaring(role: str) -> ProvisioningManifest:
    """The shipped manifest with one execution-role profile added.

    Built with `dataclasses.replace` on the LOADED manifest rather than
    constructed from scratch, so `controlled_roles` and `role_memberships` keep
    their real values and the membership control stage 1 runs first is not
    quietly dropped.
    """

    return dataclasses.replace(
        load_provisioning_manifest(),
        execution_role_profiles={
            role: ExecutionRoleProfile(
                login=False,
                superuser=False,
                createdb=False,
                createrole=False,
                replication=False,
                bypassrls=False,
                tenant_schema_privileges=("USAGE",),
            )
        },
    )


def ledger_writes(connection: object) -> tuple[object, ...]:
    """Every statement that WRITES the ledger. The read is excluded by verb."""

    return tuple(
        entry
        for entry in connection.statements  # type: ignore[attr-defined]
        if "shared.schema_migrations" in entry.fingerprint
        and not entry.fingerprint.startswith("select")
    )


def is_statement(text: str) -> object:
    """A trace predicate matching one statement by exact text."""

    return lambda entry: entry.kind == "statement" and entry.text == text


def starts_with(prefix: str) -> object:
    """A trace predicate matching one statement by fingerprint prefix."""

    lowered = prefix.casefold()
    return lambda entry: entry.kind == "statement" and entry.fingerprint.startswith(lowered)


def applied_write() -> object:
    """The ledger write that actually sets `applied`.

    Matching `UPDATE shared.schema_migrations` plus the right parameters is not
    enough: `_record_failed` is also an UPDATE on that table with those two
    parameters. The state being set is the whole point of the assertion, so it
    is part of the predicate rather than assumed from the prefix.
    """

    return lambda entry: (
        entry.kind == "statement"
        and entry.fingerprint.startswith("update shared.schema_migrations")
        and "set state = 'applied'" in entry.fingerprint
    )


def autocommit_precedes_every_statement(connection: object) -> None:
    """`set_autocommit` was called before the connection issued anything.

    A final `autocommit is True` is a value, not an ordering: it holds equally
    if the flag were set halfway through. This asserts the position in the
    trace, which is what the claim actually needs.
    """

    trace = connection.trace  # type: ignore[attr-defined]
    autocommit_at = connection.index_of(  # type: ignore[attr-defined]
        lambda entry: entry.kind == "autocommit"
    )
    statements = [index for index, entry in enumerate(trace) if entry.kind == "statement"]
    assert statements, "connection issued no statements; the ordering claim would be vacuous"
    assert autocommit_at < min(statements)


def boundary(kind: str, txn: int) -> object:
    """A trace predicate matching one transaction boundary by id."""

    return lambda entry: entry.kind == kind and entry.txn == txn


# ---------------------------------------------------------------------------
# Positive controls.
# ---------------------------------------------------------------------------


async def test_control_recorder_observes_exactly_one_rendered_execution(
    migration_driver: object, harness: object
) -> None:
    """CONTROL. The recorder is on the path the runner actually uses.

    Asserts identity with `unit.render(SCHEMA)` -- the exact string the runner
    computes -- that it was executed exactly ONCE, and the applied outcome. Were
    the harness off the runner's path, no statement would carry these bytes and
    this fails, which is why it runs first.
    """

    units = registry(ORDINARY_SQL)
    runner, (connection,) = migration_driver(units, (harness.ledger_absent(),))  # type: ignore[operator]

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    rendered = units.units[0].render(SCHEMA)
    assert len(outcomes) == 1
    assert outcomes[0].migration_id == units.units[0].migration_id
    assert outcomes[0].applied is True
    assert connection.texts.count(rendered) == 1
    assert SCHEMA in rendered
    assert "{schema}" not in rendered


async def test_control_running_and_the_ddl_are_in_different_transactions(
    migration_driver: object, harness: object
) -> None:
    """CONTROL. The `running` INSERT commits in a transaction of its own.

    This is the fact every "no ledger row" assertion depends on, and it is
    asserted by TRANSACTION IDENTITY, not by depth. An earlier version compared
    a statement list against a separate event list and checked that both were at
    "depth 1" -- which both statements landing in the FIRST block, with an empty
    second block, would also satisfy.

    Here the INSERT carries transaction id 1 and the DDL carries id 2, and the
    single ordered trace shows commit-1 strictly before begin-2. A rollback of
    transaction 2 therefore cannot remove what transaction 1 committed.
    """

    units = registry(ORDINARY_SQL)
    runner, (connection,) = migration_driver(units, (harness.ledger_absent(),))  # type: ignore[operator]

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    insert = connection.index_of(starts_with("insert into shared.schema_migrations"))
    ddl = connection.index_of(is_statement(units.units[0].render(SCHEMA)))

    assert connection.trace[insert].txn == 1
    assert connection.trace[ddl].txn == 2

    assert (
        connection.index_of(boundary("txn-begin", 1))
        < insert
        < connection.index_of(boundary("txn-commit", 1))
        < connection.index_of(boundary("txn-begin", 2))
        < ddl
        < connection.index_of(boundary("txn-commit", 2))
    )


async def test_the_fake_refuses_to_invent_a_row_for_an_undeclared_read(
    migration_driver: object, harness: object
) -> None:
    """HARNESS. `UnscriptedQuery` fires, so no assertion rests on an invented row.

    This tests the harness, not the runner, and is labelled so. The failure it
    guards against is silent: a fake answering an unrecognized SELECT with
    `None` would send the runner down the first-attempt branch regardless of
    what the test meant to set up. The ledger answer is omitted deliberately;
    stage 1's answers are present, so the unanswered read is unambiguous.
    """

    units = registry(ORDINARY_SQL)
    runner, (connection,) = migration_driver(units, ())  # type: ignore[operator]

    with pytest.raises(harness.UnscriptedQuery) as excinfo:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert "shared.schema_migrations" in str(excinfo.value)
    assert ledger_writes(connection) == ()


# ---------------------------------------------------------------------------
# The ordinary route.
# ---------------------------------------------------------------------------


async def test_applied_at_the_same_checksum_touches_nothing(
    migration_driver: object, harness: object
) -> None:
    """Idempotence: no DDL, no ledger write, no transaction opened."""

    units = registry(ORDINARY_SQL)
    runner, (connection,) = migration_driver(  # type: ignore[operator]
        units, (harness.ledger_row("applied", units.units[0].checksum),)
    )

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert outcomes[0].applied is False
    assert ledger_writes(connection) == ()
    assert units.units[0].render(SCHEMA) not in connection.texts
    assert "txn-begin" not in connection.kinds


async def test_applied_at_a_different_checksum_is_drift_and_changes_nothing(
    migration_driver: object, harness: object
) -> None:
    """Drift refuses with the sanitized code and writes no evidence over it."""

    units = registry(ORDINARY_SQL)
    runner, (connection,) = migration_driver(  # type: ignore[operator]
        units, (harness.ledger_row("applied", "0" * 64),)
    )

    with pytest.raises(TenantMigrationFailed) as excinfo:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert excinfo.value.reason_code == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
    assert ledger_writes(connection) == ()
    assert units.units[0].render(SCHEMA) not in connection.texts
    assert "txn-begin" not in connection.kinds


async def test_an_invalid_tenant_id_refuses_before_a_connection_is_taken(
    migration_driver: object, harness: object
) -> None:
    """`_validate_tenant_id` precedes the factory call.

    Supplying NO connections makes the assertion sharp: had the runner reached
    the factory, `connection_factory` raises its own `AssertionError` and this
    test fails with that instead of the expected refusal.
    """

    units = registry(ORDINARY_SQL)
    runner, _ = migration_driver(units)  # type: ignore[operator]

    with pytest.raises(TenantMigrationFailed) as excinfo:
        await runner.apply_within_lock(tenant_id="Clinic A", schema_key=SCHEMA)

    assert excinfo.value.reason_code == PreconditionCode.TENANT_ID_INVALID.value


async def test_an_ordinary_unit_runs_as_the_migrator_and_assumes_no_other_role(
    migration_driver: object, harness: object
) -> None:
    """`I-ordinary-role`. The migrator is assumed FIRST, and nothing else is.

    An earlier version filtered only `SET LOCAL ROLE` and saw the unconditional
    restore that follows execution. That passes even when the initial session
    `SET ROLE` is absent or names the wrong role -- the restore is issued either
    way, so on its own it establishes nothing about who ran the DDL.

    So the assertion is over EVERY role transition on the path, in order:
    the session-level `SET ROLE` to the migrator comes first and precedes the
    DDL, and the only other transition is the restore to the same role. The DDL
    executes exactly once and the outcome is `applied`.

    This still describes what the runner ISSUED. Actual ownership of the created
    object is a catalogue fact and is PENDING on PostgreSQL.
    """

    units = registry(ORDINARY_SQL)
    runner, (connection,) = migration_driver(units, (harness.ledger_absent(),))  # type: ignore[operator]

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    rendered = units.units[0].render(SCHEMA)
    assert len(outcomes) == 1
    assert outcomes[0].applied is True
    assert connection.texts.count(rendered) == 1

    transitions = tuple(
        entry.text
        for entry in connection.statements
        if entry.fingerprint.startswith(("set role", "set local role"))
    )
    assert transitions == (
        f'SET ROLE "{MIGRATOR}"',
        f'SET LOCAL ROLE "{MIGRATOR}"',
    )

    assume = connection.index_of(starts_with(f'set role "{MIGRATOR}"'))
    ddl = connection.index_of(is_statement(rendered))
    assert assume < ddl
    # The session-level assumption happens outside any transaction; the DDL is
    # inside the second one. Nothing changes the role in between.
    assert connection.trace[assume].txn is None


# ---------------------------------------------------------------------------
# Execution roles.
# ---------------------------------------------------------------------------


async def test_an_execution_role_the_manifest_never_describes_is_refused(
    migration_driver: object, harness: object
) -> None:
    """`I-missing-role`. Approval by name is not a description.

    `approved_execution_roles` admitted the name at composition; the manifest
    carries no profile, so stage 1 has nothing to compare the catalogue against.
    The refusal precedes any ledger write: a configuration fault is not a tenant
    migration failure.
    """

    units = role_registry(ORDINARY_SQL)
    runner, (connection,) = migration_driver(units, (harness.ledger_absent(),))  # type: ignore[operator]

    with pytest.raises(TenantMigrationFailed) as excinfo:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert excinfo.value.reason_code == PreconditionCode.EXECUTION_ROLE_UNAVAILABLE.value
    assert ledger_writes(connection) == ()
    assert units.units[0].render(SCHEMA) not in connection.texts
    assert "txn-begin" not in connection.kinds


async def test_an_unapproved_execution_role_never_reaches_a_runner() -> None:
    """Composition refuses first; there is no registry to run.

    Asserted at the builder, where the control lives. The default for
    `approved_execution_roles` is empty, so forgetting to pass it denies.
    """

    with pytest.raises(MigrationUnitRejected) as excinfo:
        build_tenant_migration_registry(
            {"t001_test_cp2_role": UnitDefinition(ORDINARY_SQL, execution_role=MODULE_ROLE)},
            allow_test_units=True,
        )

    assert excinfo.value.reason_code == PreconditionCode.EXECUTION_ROLE_NOT_APPROVED.value


async def test_declared_role_switch_sequence(
    migration_driver: object, harness: object
) -> None:
    """RUNNER ROLE-SWITCH CONTROL. **Not EC-2, and not proof of separation.**

    What this establishes: the ORDER and TRANSACTION MEMBERSHIP of the calls the
    runner issues when a unit declares an execution role. The `running` INSERT
    is written under the migrator, in its own transaction, BEFORE the module
    role is assumed; the role is then assumed immediately before the DDL and
    dropped immediately after; and the `applied` write follows the drop, so the
    ledger row is never written under the module role. All four carry the same
    transaction id -- which is what makes `SET LOCAL` the right verb: the role
    reverts on commit or rollback.

    What it does NOT establish, stated because the distinction is the whole
    point of the label above. The safe-role attributes and `pg_has_role = true`
    below are ANSWERS THIS TEST SUPPLIES. They are assumptions, not observations:
    nothing here shows the deployment has such a role, that the membership
    exists, that the migrator may actually `SET ROLE` to it, that the created
    object ends up owned by it, or that a rollback behaves as described. Every
    one of those is a catalogue or server fact and stays PENDING on
    PostgreSQL 17. EC-2 is an entry condition about the real deployment; this is
    a call sequence consistent with it.
    """

    role_attributes = harness.Answer(
        markers=("select rolcanlogin", "pg_roles"), rows=((False,) * 6,)
    )
    role_is_settable = harness.Answer(markers=("pg_has_role",), rows=((True,),))

    units = role_registry(FUNCTION_SQL)
    runner, (connection,) = migration_driver(  # type: ignore[operator]
        units,
        (role_attributes, role_is_settable, harness.ledger_absent()),
        manifest=manifest_declaring(MODULE_ROLE),
    )

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)
    rendered = units.units[0].render(SCHEMA)
    assert len(outcomes) == 1
    assert outcomes[0].applied is True

    # Cardinality first. Every index below is `index_of`, which raises unless
    # there is exactly ONE match -- an ordering assertion over "the first
    # match" is silently wrong the moment a second appears.
    assert connection.texts.count(rendered) == 1
    assert len(ledger_writes(connection)) == 2

    # The COMPLETE role-transition sequence on this path, in order. A subset
    # assertion would pass with an extra transition spliced in anywhere.
    assert tuple(
        entry.text
        for entry in connection.statements
        if entry.fingerprint.startswith(("set role", "set local role"))
    ) == (
        f'SET ROLE "{MIGRATOR}"',
        f'SET LOCAL ROLE "{MODULE_ROLE}"',
        f'SET LOCAL ROLE "{MIGRATOR}"',
    )

    initial = connection.index_of(starts_with(f'set role "{MIGRATOR}"'))
    running = connection.index_of(starts_with("insert into shared.schema_migrations"))
    assume = connection.index_of(starts_with(f'set local role "{MODULE_ROLE}"'))
    ddl = connection.index_of(is_statement(rendered))
    restore = connection.index_of(starts_with(f'set local role "{MIGRATOR}"'))
    applied = connection.index_of(applied_write())

    # The migrator is assumed at session level first; the ledger's `running`
    # row is written under it, before the module role is ever assumed, and in a
    # DIFFERENT transaction.
    assert initial < running < assume
    assert connection.trace[initial].txn is None
    assert connection.trace[running].txn == 1
    assert connection.trace[assume].txn == 2

    assert assume + 1 == ddl
    assert ddl + 1 == restore
    assert restore < applied
    assert {connection.trace[index].txn for index in (assume, ddl, restore, applied)} == {2}

    # The applied write names this tenant and this migration, and nothing else.
    assert connection.trace[applied].params == (TENANT, units.units[0].migration_id)
    assert connection.trace[running].params == (
        TENANT,
        units.units[0].migration_id,
        units.units[0].checksum,
    )


# ---------------------------------------------------------------------------
# The `apply` route, which takes the lock on a second connection.
# ---------------------------------------------------------------------------


async def test_apply_holds_the_lock_on_a_separate_connection_around_the_work(
    migration_driver: object, harness: object
) -> None:
    """`apply` = lock connection + work connection, ordered across BOTH.

    `runner.py`'s module docstring argues the lock must be session-level and
    must live on its OWN connection, because a transaction-scoped lock would be
    released by the ledger commit -- exactly when the DDL window opens.

    An earlier version asserted that shape from the two connections' SEPARATE
    traces: the acquire appeared on one, the work on the other. That proves
    nothing about their order, so a runner that released the lock BEFORE doing
    any work passed every assertion. It is the same defect as comparing
    transaction depth instead of transaction identity, one level up -- two
    orderings that are not comparable being compared anyway.

    So both connections share one `SharedClock`, and every assertion below is
    made against the MERGED trace: acquire strictly before the first work-side
    entry, release strictly after the last one.
    """

    units = registry(ORDINARY_SQL)
    runner, (lock, work) = migration_driver(  # type: ignore[operator]
        units,
        (harness.lock_timeout_set, harness.advisory_lock_taken, harness.advisory_lock_released),
        (harness.ledger_absent(),),
        names=("lock", "work"),
    )

    outcomes = await runner.apply(tenant_id=TENANT, schema_key=SCHEMA)

    rendered = units.units[0].render(SCHEMA)
    assert len(outcomes) == 1
    assert outcomes[0].applied is True
    assert lock is not work

    # Cardinality: exactly one acquire, one release, one DDL.
    assert len(lock.issued("pg_advisory_lock(")) == 1
    assert len(lock.issued("pg_advisory_unlock(")) == 1
    assert work.texts.count(rendered) == 1

    # Both connections were put into autocommit BEFORE issuing anything --
    # asserted as trace position, not as the flag's final value.
    assert lock.autocommit is True
    assert work.autocommit is True
    autocommit_precedes_every_statement(lock)
    autocommit_precedes_every_statement(work)

    # The lock connection does no work: no DDL, no ledger, no transaction.
    assert lock.issued("shared.schema_migrations") == ()
    assert rendered not in lock.texts
    assert "txn-begin" not in lock.kinds

    # The advisory lock and unlock name the SAME key, and it is the tenant's.
    expected_key = (MIGRATION_LOCK_NAMESPACE, tenant_lock_key(TENANT))
    assert lock.issued("pg_advisory_lock(")[0].params == expected_key
    assert lock.issued("pg_advisory_unlock(")[0].params == expected_key

    # THE ORDERING ASSERTION, across both connections by shared sequence.
    merged = harness.merged_trace(lock, work)
    acquire = next(e for e in merged if e.fingerprint.startswith("select pg_advisory_lock("))
    release = next(e for e in merged if e.fingerprint.startswith("select pg_advisory_unlock("))
    work_side = [entry for entry in merged if entry.connection == "work"]

    assert work_side, "the work connection recorded nothing; the merge would be vacuous"
    assert acquire.seq < min(entry.seq for entry in work_side)
    assert release.seq > max(entry.seq for entry in work_side)

    # The release is the last statement on the lock connection, outside any
    # transaction -- a session-level lock, as the design says.
    assert lock.statements[-1] is release
    assert release.txn is None

    assert lock.closed
    assert work.closed


# ---------------------------------------------------------------------------
# DELIVERED / PENDING LEDGER
#
# Delivered here, executable, all GREEN:
#   CONTROL  recorder observes exactly one rendered execution
#   CONTROL  `running` and the DDL are in DIFFERENT transactions (by id)
#   HARNESS  the fake refuses to invent a row for an unscripted read
#            applied at the same checksum touches nothing
#            applied at a different checksum is drift
#            invalid tenant id refuses before a connection is taken
#   I-ordinary-role  ordinary unit assumes no role but the migrator
#   I-missing-role   role approved but never described is refused
#            unapproved role never reaches a runner (builder control)
#   (*)      runner role-SWITCH control -- NOT EC-2, see note below
#            `apply` holds the lock on a separate connection
#
# (*) The role-switch row is a CALL-SEQUENCE control. It is NOT EC-2 closure and
#     NOT proof of M02 table/function separation -- one unit taking one route
#     shows the route exists, not that the two routes are separated. The
#     safe-role attributes and `pg_has_role = true` it relies on are answers the
#     test supplies. Real role existence, membership, `SET ROLE` capability,
#     created-object ownership, privilege behaviour and rollback are catalogue
#     and server facts and stay PENDING on PostgreSQL 17.
#
# PENDING -- not delivered, not claimed, and in two cases WITHDRAWN because the
# assertion contradicted a recorded decision (see the module docstring):
#   B-nul-bound      bound-plan NUL coverage. The legacy-ordinary form is
#                    excluded by EX-01 and must NOT be asserted. Needs the typed
#                    function path.
#   B-legacy-bypass  needs the unauthorized-definition-to-legacy-route boundary
#                    specified, preserving OD-04's authorized ordinary route. A
#                    blanket ordinary CREATE FUNCTION refusal is NOT the
#                    contract and must not be implemented to turn a test green.
#   B-schema, B-role, B-checksum, B-unit, B-registry, B-rerender, B-late-invalid
#                    -- the plan interface does not exist. Deliberately NOT
#                    written as imports of an absent module.
#   C-* (8 constructed AST cases)  -- function policy packet, separate.
#   Every D row                    -- PostgreSQL 17, owner-operated.
#
# Also NOT written: a row asserting that a unit which both creates a table and
# declares an execution role is refused. Whether a mixed unit must be REFUSED is
# a design proposal that has not been through requirements review.
#
# Nothing in this file has been written into the repository.
# ---------------------------------------------------------------------------
