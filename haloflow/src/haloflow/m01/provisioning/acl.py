"""Tenant-schema ACL reading, expectation building and grant installation.

The module reads PostgreSQL's catalogue into an immutable four-dimensional set,
expands the validated manifest into the same type, and installs exactly the
declared schema grants inside a caller-owned transaction. Stage 3, introduced
by CP-5c, owns comparison and failure handling; putting equality here would
cross that review boundary.

Two PostgreSQL 17.11 measurements constrain the catalogue query (R-P1B.16):

* ``PUBLIC`` is represented by grantee OID zero and has no ``pg_roles`` row, so
  its name must be recovered without an inner join.
* ``aclexplode(NULL)`` safely returns no rows, while coalescing an ACL to an
  empty array raises ``ACL arrays must be one-dimensional``.  ``nspacl`` is
  therefore exploded as-is.

The expected-set builder has no role-name literal, default or supplement. Every
entry it emits is a direct expansion of ``tenant_schema_role_privileges``
(R-P1B.18, D22).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from psycopg import sql

from haloflow.m01.provisioning.manifest import ProvisioningManifest


@dataclass(frozen=True, slots=True)
class SchemaAclEntry:
    """One exploded schema-ACL tuple, including delegation provenance."""

    grantee: str
    privilege_type: str
    is_grantable: bool
    grantor: str


def build_expected_schema_acl(manifest: ProvisioningManifest) -> frozenset[SchemaAclEntry]:
    """Expand the complete manifest declaration into exact ACL tuples."""

    return frozenset(
        SchemaAclEntry(
            grantee=grantee,
            privilege_type=privilege,
            is_grantable=entry.is_grantable,
            grantor=entry.grantor,
        )
        for grantee, entry in manifest.tenant_schema_role_privileges.items()
        for privilege in entry.privileges
    )


_READ_SCHEMA_ACL_SQL: Final = """
    SELECT CASE
               WHEN acl.grantee = 0 THEN 'PUBLIC'
               ELSE grantee_role.rolname
           END AS grantee,
           acl.privilege_type,
           acl.is_grantable,
           grantor_role.rolname AS grantor
      FROM pg_namespace AS namespace
      CROSS JOIN LATERAL aclexplode(namespace.nspacl) AS acl
      LEFT JOIN pg_roles AS grantee_role ON grantee_role.oid = acl.grantee
      LEFT JOIN pg_roles AS grantor_role ON grantor_role.oid = acl.grantor
     WHERE namespace.nspname = %s
"""


def entry_from_row(row: Sequence[object]) -> SchemaAclEntry:
    """Map one catalogue row without inventing names for unresolved role OIDs."""

    grantee = row[0]
    grantor = row[3]
    if grantee is None or grantor is None:
        # PostgreSQL normally prevents this state through shared dependencies,
        # but a catalogue control must not rename an unresolvable principal to
        # Python's string ``"None"`` and continue by accident.
        raise RuntimeError("schema ACL references an unresolved role")
    return SchemaAclEntry(
        grantee=str(grantee),
        privilege_type=str(row[1]),
        is_grantable=bool(row[2]),
        grantor=str(grantor),
    )


async def read_schema_acl(connection: object, schema_key: str) -> frozenset[SchemaAclEntry]:
    """Read one schema's ACL as an immutable exact set; never mutate or compare.

    An absent schema and a present schema whose ``nspacl`` is NULL both produce
    the empty set. Stage 3 compares against a non-empty declaration after schema
    creation, so either state fails closed; CP-5c pins that integration boundary.
    """

    async with connection.cursor() as cursor:  # type: ignore[attr-defined]
        await cursor.execute(_READ_SCHEMA_ACL_SQL, (schema_key,))
        rows = await cursor.fetchall()

    return frozenset(entry_from_row(row) for row in rows)


# The manifest loader admits exactly this vocabulary. Keeping the SQL fragments
# closed here means even a directly constructed typed object cannot turn a
# privilege string into executable syntax.
_SCHEMA_PRIVILEGE_SQL: Final[Mapping[str, sql.SQL]] = {
    "CREATE": sql.SQL("CREATE"),
    "USAGE": sql.SQL("USAGE"),
}


async def install_schema_acl(
    connection: object,
    schema_key: str,
    manifest: ProvisioningManifest,
) -> None:
    """Install every declared schema grant, and no other grant (CP-5b).

    The caller owns the transaction deliberately. CP-5c wraps this installation
    and the exact ACL postcondition in one transaction so a mismatch rolls back
    every grant atomically. This function issues no ``REVOKE`` and performs no
    comparison or repair.

    Grantee names and the schema key are quoted as identifiers. Privileges come
    through a closed M01-owned vocabulary rather than becoming SQL text from the
    manifest. ``is_grantable`` is represented faithfully for the typed contract;
    today's validated manifest requires it to be false (R-P1B.22).
    """

    for grantee, entry in manifest.tenant_schema_role_privileges.items():
        privileges = sql.SQL(", ").join(
            _SCHEMA_PRIVILEGE_SQL[privilege] for privilege in entry.privileges
        )
        grant_option = sql.SQL(" WITH GRANT OPTION") if entry.is_grantable else sql.SQL("")
        statement = sql.SQL("GRANT {} ON SCHEMA {} TO {}{}").format(
            privileges,
            sql.Identifier(schema_key),
            sql.Identifier(grantee),
            grant_option,
        )
        await connection.execute(statement)  # type: ignore[attr-defined]


__all__ = [
    "SchemaAclEntry",
    "build_expected_schema_acl",
    "entry_from_row",
    "install_schema_acl",
    "read_schema_acl",
]
