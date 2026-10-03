"""L-6 CP-2: `acl.install_schema_acl_entries` against PostgreSQL 17 (tier D).

Traceability: architecture v6 r3 OD-C5; CP-2 plan v2 section 3.2. Case IDs
TC-C2-D01 to TC-C2-D03 are the CP-2 amendment to test cases v4 (decision D4(b)).

Scope: these are **generic strict-subset installer tests**. The subset used is
the manifest expansion minus the runtime role's USAGE. They do not prove the
complete A-V1 execution-role adapter, which is tested with its later caller
(CP-3/CP-5).

Each case creates its own schema as the provisioner, which owns tenant schemas
(D13). It never touches `shared.tenants` and drops the schema afterwards. The
grants are real catalogue state, read back with the existing `read_schema_acl`.
Assertions that declared mutants must break carry a unique "TC-C2-Dnn" message.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql

from haloflow.m01.provisioning import PROVISIONER_ROLE, RUNTIME_ROLE
from haloflow.m01.provisioning.acl import (
    SchemaAclEntry,
    build_expected_schema_acl,
    install_schema_acl,
    read_schema_acl,
)
from haloflow.m01.provisioning.manifest import load_provisioning_manifest

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module", autouse=True)
def _record_server_version(migrated_database: str) -> None:
    """Census P7: each D/C run records its own server version line."""

    with psycopg.connect(migrated_database, autocommit=True) as conn:
        row = conn.execute("SELECT version()").fetchone()
    assert row is not None
    print(f"L6_SERVER_VERSION={row[0]}")


@pytest_asyncio.fixture
async def provisioner_schema(
    migrated_database: str, role_logins: dict[str, str]
) -> AsyncIterator[tuple[AsyncConnection, str]]:
    """A fresh empty schema owned by the provisioner, plus its connection."""

    schema_key = f"tenant_l6cp2_{uuid4().hex[:12]}"
    connection = await AsyncConnection.connect(role_logins[PROVISIONER_ROLE], autocommit=True)
    try:
        await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_key)))
        yield connection, schema_key
    finally:
        await connection.close()
        with psycopg.connect(migrated_database, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_key))
            )


def _manifest_minus_runtime_usage() -> frozenset[SchemaAclEntry]:
    """A strict, non-empty subset of the manifest expansion (generic subset shape)."""

    full = build_expected_schema_acl(load_provisioning_manifest())
    subset = frozenset(
        e for e in full if not (e.grantee == RUNTIME_ROLE and e.privilege_type == "USAGE")
    )
    assert subset and subset != full
    return subset


@pytest.mark.asyncio
async def test_tc_c2_d01_grants_land_exactly_as_the_given_entries(
    provisioner_schema: tuple[AsyncConnection, str],
) -> None:
    from haloflow.m01.provisioning.acl import install_schema_acl_entries

    connection, schema_key = provisioner_schema
    entries = _manifest_minus_runtime_usage()

    async with connection.transaction():
        await install_schema_acl_entries(connection, schema_key, entries)

    observed = await read_schema_acl(connection, schema_key)
    assert observed == entries, "TC-C2-D01: ACL equals the given entries exactly"


@pytest.mark.asyncio
async def test_tc_c2_d02_participates_in_the_callers_transaction(
    provisioner_schema: tuple[AsyncConnection, str],
) -> None:
    from haloflow.m01.provisioning.acl import install_schema_acl_entries

    connection, schema_key = provisioner_schema
    entries = _manifest_minus_runtime_usage()

    with pytest.raises(RuntimeError, match="force rollback"):
        async with connection.transaction():
            await install_schema_acl_entries(connection, schema_key, entries)
            inside = await read_schema_acl(connection, schema_key)
            assert inside == entries, "TC-C2-D02: grants visible inside the caller's transaction"
            raise RuntimeError("force rollback")

    after = await read_schema_acl(connection, schema_key)
    assert after == frozenset(), "TC-C2-D02: caller rollback removes every grant"


@pytest.mark.asyncio
async def test_tc_c2_d03_existing_grants_are_never_revoked(
    provisioner_schema: tuple[AsyncConnection, str],
) -> None:
    """A subset, then an empty set, installed over the full ACL leave it intact: no REVOKE."""

    from haloflow.m01.provisioning.acl import install_schema_acl_entries

    connection, schema_key = provisioner_schema
    manifest = load_provisioning_manifest()

    async with connection.transaction():
        await install_schema_acl(connection, schema_key, manifest)
    full = await read_schema_acl(connection, schema_key)
    assert full == build_expected_schema_acl(manifest)

    async with connection.transaction():
        await install_schema_acl_entries(connection, schema_key, _manifest_minus_runtime_usage())
        await install_schema_acl_entries(connection, schema_key, [])

    observed = await read_schema_acl(connection, schema_key)
    assert observed == full, "TC-C2-D03: no grant revoked"
