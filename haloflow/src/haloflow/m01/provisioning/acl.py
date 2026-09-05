"""Tenant-schema ACL reading and expected-set construction (CP-5a).

This module deliberately stops at representation.  It reads PostgreSQL's
catalogue into an immutable four-dimensional set and expands the validated
manifest into the same type.  Stage 3, introduced by CP-5c, owns comparison and
failure handling; putting equality here would cross that review boundary.

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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

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


__all__ = [
    "SchemaAclEntry",
    "build_expected_schema_acl",
    "entry_from_row",
    "read_schema_acl",
]
