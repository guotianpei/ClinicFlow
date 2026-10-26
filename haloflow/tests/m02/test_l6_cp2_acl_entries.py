"""L-6 CP-2: `acl.install_schema_acl_entries`, statement composition (tier U).

Traceability: architecture v6 r3 OD-C5 ("`acl.install_schema_acl_entries`
added"); CP-2 plan v2 section 3.2. Case IDs TC-C2-U01 to TC-C2-U08 are the
CP-2 amendment to test cases v4 (decision D4(b)).

Contract under test:
- The installer is a pure GRANT issuer in a caller-owned transaction. It issues
  no REVOKE, opens no transaction or savepoint, makes no comparison or repair,
  never reads, and chooses no tuples.
- Statements are grouped by (grantee, grant option). Grantees are sorted, and
  privileges are sorted within a group. Duplicates collapse.
- Every privilege is validated against the closed vocabulary {CREATE, USAGE}
  before the first statement.
- The installer issues no GRANTED BY and does not replay the supplied `grantor`.
  PostgreSQL 17 records the grantor from the executing context. The caller
  establishes that context and proves the result by exact readback.

Every assertion that a declared mutant must break carries a unique message
("TC-C2-Unn: ..."), so the mutation map can name the exact failing assertion.
No database is needed: a recording connection captures every call.
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

import pytest
from psycopg import sql

from haloflow.m01.provisioning.acl import SchemaAclEntry


class RecordingConnection:
    """Captures executed statements and any transaction, commit, rollback or cursor use."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.boundary_calls: list[str] = []

    async def execute(self, statement: object, params: object = None) -> None:
        if params is not None or not isinstance(statement, sql.Composable):
            self.boundary_calls.append("execute-with-params-or-plain-text")
            return
        self.statements.append(statement.as_string(None))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.boundary_calls.append("transaction")
        yield

    async def commit(self) -> None:
        self.boundary_calls.append("commit")

    async def rollback(self) -> None:
        self.boundary_calls.append("rollback")

    def cursor(self) -> object:
        self.boundary_calls.append("cursor")
        raise RuntimeError("recording connection: cursor() is not part of the installer contract")


async def _install(
    entries: Iterable[SchemaAclEntry], schema_key: str = "tenant_a"
) -> RecordingConnection:
    from haloflow.m01.provisioning.acl import install_schema_acl_entries

    connection = RecordingConnection()
    await install_schema_acl_entries(connection, schema_key, entries)
    return connection


@pytest.mark.asyncio
async def test_tc_c2_u01_one_grant_per_grantee_in_deterministic_order() -> None:
    """Input order does not matter; grantees, then privileges, are sorted."""

    entries = [
        SchemaAclEntry("role_b", "USAGE", False, "grantor_x"),
        SchemaAclEntry("role_a", "USAGE", False, "grantor_x"),
        SchemaAclEntry("role_b", "CREATE", False, "grantor_x"),
    ]
    expected = [
        'GRANT USAGE ON SCHEMA "tenant_a" TO "role_a"',
        'GRANT CREATE, USAGE ON SCHEMA "tenant_a" TO "role_b"',
    ]

    forward = (await _install(entries)).statements
    backward = (await _install(list(reversed(entries)))).statements

    assert forward == expected, "TC-C2-U01: sorted grants (forward input)"
    assert backward == expected, "TC-C2-U01: sorted grants (reversed input)"


@pytest.mark.asyncio
async def test_tc_c2_u02_grant_option_is_represented_faithfully() -> None:
    """`is_grantable` becomes WITH GRANT OPTION and never merges with a non-grantable tuple."""

    statements = (
        await _install(
            [
                SchemaAclEntry("role_a", "USAGE", True, "grantor_x"),
                SchemaAclEntry("role_a", "CREATE", False, "grantor_x"),
            ]
        )
    ).statements

    expected = [
        'GRANT CREATE ON SCHEMA "tenant_a" TO "role_a"',
        'GRANT USAGE ON SCHEMA "tenant_a" TO "role_a" WITH GRANT OPTION',
    ]
    assert statements == expected, "TC-C2-U02: grant option statements"


@pytest.mark.asyncio
async def test_tc_c2_u03_empty_input_issues_no_statement() -> None:
    connection = await _install([])
    assert connection.statements == [], "TC-C2-U03: no statement for empty input"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "privilege",
    ["ALL", "TEMPORARY", "usage", "USAGE; DROP SCHEMA x", ""],
    ids=["all", "temporary", "lowercase", "injection", "empty"],
)
async def test_tc_c2_u04_unknown_privilege_is_refused_before_any_statement(
    privilege: str,
) -> None:
    """Closed vocabulary {CREATE, USAGE}; validation completes before the first GRANT."""

    from haloflow.m01.provisioning.acl import install_schema_acl_entries

    connection = RecordingConnection()
    entries = [
        SchemaAclEntry("role_a", "USAGE", False, "grantor_x"),
        SchemaAclEntry("role_z", privilege, False, "grantor_x"),
    ]
    with pytest.raises(KeyError):
        await install_schema_acl_entries(connection, "tenant_a", entries)
    assert connection.statements == [], "TC-C2-U04: no statement before refusal"


@pytest.mark.asyncio
async def test_tc_c2_u05_schema_and_grantee_are_quoted_identifiers() -> None:
    hostile_schema = 'tenant_x" CASCADE; --'
    hostile_role = 'r" WITH GRANT OPTION; --'

    connection = await _install(
        [SchemaAclEntry(hostile_role, "USAGE", False, "grantor_x")], schema_key=hostile_schema
    )

    expected = ['GRANT USAGE ON SCHEMA "tenant_x"" CASCADE; --" TO "r"" WITH GRANT OPTION; --"']
    assert connection.statements == expected, "TC-C2-U05: quoted identifiers"


@pytest.mark.asyncio
async def test_tc_c2_u06_never_revokes_and_does_not_replay_the_grantor() -> None:
    """No REVOKE and no GRANTED BY. Two grantors for one tuple yield one plain GRANT."""

    connection = await _install(
        [
            SchemaAclEntry("role_a", "USAGE", False, "grantor_x"),
            SchemaAclEntry("role_a", "USAGE", False, "grantor_y"),
        ]
    )

    expected = ['GRANT USAGE ON SCHEMA "tenant_a" TO "role_a"']
    assert connection.statements == expected, "TC-C2-U06: one plain grant, no REVOKE/GRANTED BY"


@pytest.mark.asyncio
async def test_tc_c2_u07_duplicates_collapse_and_any_iterable_is_accepted() -> None:
    entry = SchemaAclEntry("role_a", "CREATE", False, "grantor_x")

    from_generator = (await _install(e for e in (entry, entry))).statements
    from_frozenset = (await _install(frozenset({entry}))).statements

    expected = ['GRANT CREATE ON SCHEMA "tenant_a" TO "role_a"']
    assert from_generator == expected, "TC-C2-U07: generator input, duplicates collapse"
    assert from_frozenset == expected, "TC-C2-U07: frozenset input"


@pytest.mark.asyncio
async def test_tc_c2_u08_opens_no_transaction_and_never_reads() -> None:
    """The caller owns the transaction: no transaction(), savepoint, commit, rollback or cursor."""

    connection = await _install(
        [
            SchemaAclEntry("role_a", "USAGE", False, "grantor_x"),
            SchemaAclEntry("role_b", "CREATE", False, "grantor_x"),
        ]
    )

    assert connection.boundary_calls == [], "TC-C2-U08: no transaction boundary or read"
