"""CP2-2b R-B11: the CI ownership inventory (test cases v3 section 3.6).

Test-only code. Two collections gathered INDEPENDENTLY are classified together,
then the dependents of every collected object are walked, and the target-schema
assertion is made over the result.

  A  the spine: pg_shdepend owner rows (deptype 'o') for the lock owner, current db
  B  a direct scan of the owner column of each of the 11 mapped catalogues
  W  a breadth-first walk over pg_depend DEPENDENTS with deptype in a, i, P, S, e, x

Normal ('n') dependents are excluded from the walk: a normal dependent is an
independent object with its own owner column, so if the lock owner owns it, B finds
it (D25l checks this on the server for one concrete case; it is not universal proof).

The classifier is pure over a `Source`. `ServerSource` reads PostgreSQL; the
constructed rows D25a to D25i feed a `ConstructedSource` and are labelled as such.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from psycopg import sql

# catalogue -> (owner column, namespace column)
MAPPED: Mapping[str, tuple[str, str]] = {
    "pg_proc": ("proowner", "pronamespace"),
    "pg_class": ("relowner", "relnamespace"),
    "pg_type": ("typowner", "typnamespace"),
    "pg_operator": ("oprowner", "oprnamespace"),
    "pg_collation": ("collowner", "collnamespace"),
    "pg_conversion": ("conowner", "connamespace"),
    "pg_opclass": ("opcowner", "opcnamespace"),
    "pg_opfamily": ("opfowner", "opfnamespace"),
    "pg_ts_config": ("cfgowner", "cfgnamespace"),
    "pg_ts_dict": ("dictowner", "dictnamespace"),
    "pg_statistic_ext": ("stxowner", "stxnamespace"),
}
INCLUDED_DEPTYPES = frozenset({"a", "i", "P", "S", "e", "x"})


@dataclass(frozen=True, order=True)
class Address:
    """A full object address. `objsubid` is kept; classification uses the parent."""

    catalog: str
    objid: int
    objsubid: int = 0

    @property
    def parent(self) -> Address:
        return Address(self.catalog, self.objid, 0)


class Source(Protocol):
    def spine(self) -> Iterable[Address]: ...
    def direct(self) -> Iterable[Address]: ...
    def namespace_of(self, address: Address) -> int | None: ...
    def namespace_name(self, namespace_oid: int) -> str | None: ...
    def dependents(self, address: Address) -> Iterable[tuple[Address, str]]: ...


@dataclass
class Result:
    target_objects: set[Address] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)
    b_not_in_a: set[Address] = field(default_factory=set)
    walked: list[Address] = field(default_factory=list)
    gateway_dependents: list[Address] = field(default_factory=list)

    def passes(self, gateway: Address) -> bool:
        return (
            not self.failures
            and self.target_objects == {gateway}
            and not self.gateway_dependents
        )


def classify(source: Source, *, target_schema: str, gateway: Address) -> Result:
    result = Result()
    spine = {a.parent for a in source.spine()}
    direct = {a.parent for a in source.direct()}

    for address in sorted(spine - direct):
        if address.catalog in MAPPED:
            result.failures.append(f"spine-only:{address}")
    result.b_not_in_a = direct - spine
    if gateway not in spine | direct:
        # The expected gateway is seeded into the walk; it must also be OWNED, as
        # seen by at least one ownership source, or the inventory cannot pass.
        result.failures.append(f"gateway-not-owned:{gateway}")

    visited: set[Address] = set()
    queue: deque[Address] = deque(sorted(spine | direct | {gateway}))
    while queue:
        address = queue.popleft()
        if address in visited:  # cycle termination and deduplication
            continue
        visited.add(address)
        result.walked.append(address)
        key = address.parent
        if key.catalog not in MAPPED:
            result.failures.append(f"unmapped:{address}")
        else:
            namespace = source.namespace_of(key)
            name = None if namespace is None else source.namespace_name(namespace)
            if name is None:
                result.failures.append(f"unresolved:{address}")
            elif name == target_schema:
                result.target_objects.add(key)
        for dependent, deptype in source.dependents(key):
            if deptype not in INCLUDED_DEPTYPES:
                continue
            if key == gateway:
                result.gateway_dependents.append(dependent)
            if dependent not in visited:
                queue.append(dependent)
    return result


@dataclass
class ConstructedSource:
    """CONSTRUCTED input for D25a to D25i. Never catalogue or server evidence."""

    spine_rows: list[Address] = field(default_factory=list)
    direct_rows: list[Address] = field(default_factory=list)
    namespaces: dict[Address, int | None] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)
    edges: dict[Address, list[tuple[Address, str]]] = field(default_factory=dict)

    def spine(self) -> Iterable[Address]:
        return list(self.spine_rows)

    def direct(self) -> Iterable[Address]:
        return list(self.direct_rows)

    def namespace_of(self, address: Address) -> int | None:
        return self.namespaces.get(address)

    def namespace_name(self, namespace_oid: int) -> str | None:
        return self.names.get(namespace_oid)

    def dependents(self, address: Address) -> Iterable[tuple[Address, str]]:
        return list(self.edges.get(address, []))


class ServerSource:
    """Reads the live catalogue through a (setup/observation) ADMIN connection."""

    def __init__(self, conn: Any, role: str) -> None:
        self.conn = conn
        self.role = role

    def spine(self) -> Iterable[Address]:
        rows = self.conn.execute(
            """
            SELECT classid::pg_catalog.regclass::text, objid::bigint, objsubid
              FROM pg_catalog.pg_shdepend
             WHERE dbid = (SELECT oid FROM pg_catalog.pg_database
                            WHERE datname = pg_catalog.current_database())
               AND refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
               AND refobjid = %s::pg_catalog.regrole
               AND deptype = 'o'
            """,
            (self.role,),
        ).fetchall()
        return [Address(_bare(r[0]), int(r[1]), int(r[2])) for r in rows]

    def direct(self) -> Iterable[Address]:
        found: list[Address] = []
        for catalog, (owner, _) in MAPPED.items():
            rows = self.conn.execute(
                sql.SQL("SELECT oid::bigint FROM pg_catalog.{} WHERE {} = %s::pg_catalog.regrole")
                .format(sql.Identifier(catalog), sql.Identifier(owner)),
                (self.role,),
            ).fetchall()
            found.extend(Address(catalog, int(r[0])) for r in rows)
        return found

    def namespace_of(self, address: Address) -> int | None:
        _, namespace_column = MAPPED[address.catalog]
        row = self.conn.execute(
            sql.SQL("SELECT {}::bigint FROM pg_catalog.{} WHERE oid = %s").format(
                sql.Identifier(namespace_column), sql.Identifier(address.catalog)
            ),
            (address.objid,),
        ).fetchone()
        return None if row is None else int(row[0])

    def namespace_name(self, namespace_oid: int) -> str | None:
        row = self.conn.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace WHERE oid = %s", (namespace_oid,)
        ).fetchone()
        return None if row is None else str(row[0])

    def dependents(self, address: Address) -> Iterable[tuple[Address, str]]:
        rows = self.conn.execute(
            """
            SELECT classid::pg_catalog.regclass::text, objid::bigint, objsubid, deptype::text
              FROM pg_catalog.pg_depend
             WHERE refclassid = %s::pg_catalog.regclass AND refobjid = %s
            """,
            (f"pg_catalog.{address.catalog}", address.objid),
        ).fetchall()
        return [(Address(_bare(r[0]), int(r[1]), int(r[2])), str(r[3])) for r in rows]


def _bare(regclass_text: str) -> str:
    return regclass_text.removeprefix("pg_catalog.")


def gateway_address(conn: Any, schema: str) -> Address:
    row = conn.execute(
        "SELECT pg_catalog.to_regprocedure(%s)::oid::bigint",
        (f"{schema}.m02_lock_operation(uuid)",),
    ).fetchone()
    assert row is not None and row[0] is not None, "the gateway is not installed"
    return Address("pg_proc", int(row[0]))
