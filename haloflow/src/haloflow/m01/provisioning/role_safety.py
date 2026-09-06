"""Stage 1: the role-safety preflight (R-P1B.4, R-P1B.6, R-P1B.7, R-P1B.15).

Before any schema mutation and before the ledger is touched, two things are
checked: the **membership graph** among controlled application roles, and each
**execution role** the registry declares. An allow-listed *name* is not a safe
*role*: the composition allow-list is fixed at startup and cannot see catalogue
drift afterwards, so a role that later gained ``SUPERUSER`` or ``LOGIN`` would be
assumed by the runner deliberately.

**The comparisons are pure and the catalogue reads are thin**, deliberately.
Every test the design writes for this checkpoint needs a live PostgreSQL 17
server, so a single component would leave no rule failable anywhere else -- and a
rule that cannot be failed on demand is a rule nobody has watched fail.
``assess_membership_graph`` and ``assess_execution_role`` take what the catalogue
said and what the manifest declared and answer; nothing in either opens a
connection.

**The membership control is a property of the graph, not of one role**
(R-P1B.7). The requirement says the existing "no ``haloflow_*`` membership edges"
control *becomes* an exact-set assertion, so the comparison covers **every edge
between two controlled application roles** and is made once per preflight, not
once per execution role. Scoping it to edges targeting the registry's execution
role leaves ``GRANT haloflow_migrator TO haloflow_runtime`` undetected -- an
escalation between two infrastructure roles, which is precisely the case the
control it replaces was written for.

**Both endpoints must be controlled** (Codex note-22). A deployment's LOGIN
identities are legitimately members of application group roles; governing every
edge with a single controlled endpoint would drag those identities into a
security declaration they do not belong in. Widening to one-endpoint edges is a
broader policy decision than R-P1B.7 authorizes, and it is deferred rather than
assumed. This is not a claim that one-endpoint edges are harmless.

**The expected set is the declaration, never a constant.** It is
``manifest.role_memberships`` -- validated and pinned by value at load. A7 warns
that "no role-name literal" is easy to satisfy today and easy to violate later
with one convenient constant. The constant this module used to carry was exactly
that, and it left the declaration it duplicated entirely unused.

**Structural and effective checks are both required, and neither substitutes.**
The graph is read from ``pg_auth_members`` for its ``set_option``,
``inherit_option`` and ``admin_option``; ``pg_has_role(..., 'SET')`` is asked
separately. A role can be structurally correct and effectively unusable, and it
can be effectively reachable through an intermediate while the declared direct
edge is absent -- transitive-only satisfaction presents as a *missing declared
edge* and is refused even though the capability check passes.

``'SET'``, never ``'MEMBER'`` (V8): a role granted ``WITH SET FALSE`` satisfies
``MEMBER`` and cannot ``SET ROLE``. Asking the wrong question here would pass
exactly the configuration this stage exists to catch.

Failure raises ``EXECUTION_ROLE_UNAVAILABLE``, a ``PreconditionCode``: the
refusal happens with no schema grant made and no ``running`` row written, so a
configuration fault is never recorded as a tenant migration failure. Per R-P1B.6
the database remains the authority at execution time; this is fail-fast, not a
substitute for it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.manifest import ExecutionRoleProfile, RoleMembership
from haloflow.m01.provisioning.roles import MIGRATOR_ROLE

# R-P1B.4(b). The attributes compared, by name, so the observed row and the
# declared profile are read through one list and cannot drift apart.
_SAFETY_ATTRIBUTES: Final[tuple[str, ...]] = (
    "login",
    "superuser",
    "createdb",
    "createrole",
    "replication",
    "bypassrls",
)


@dataclass(frozen=True, slots=True)
class MembershipEdge:
    """One row of ``pg_auth_members``, both endpoints and all three options.

    ``role`` is granted **to** ``member``: ``member`` is the role that gains the
    ability to assume ``role``. Both endpoints are carried because the control is
    over the graph, and an edge identified only by its target cannot be compared
    against a declaration that names both ends.
    """

    role: str
    member: str
    set: bool
    inherit: bool
    admin: bool


@dataclass(frozen=True, slots=True)
class ObservedRole:
    """What the catalogue says about one execution role, at one moment."""

    exists: bool
    login: bool
    superuser: bool
    createdb: bool
    createrole: bool
    replication: bool
    bypassrls: bool
    has_role_set: bool


def declared_edges(declared: Iterable[RoleMembership]) -> frozenset[MembershipEdge]:
    """The expected graph, from the manifest and from nothing else (R-P1B.7, A7).

    A translation between two record types and deliberately nothing more: no
    default, no supplement, no role-name literal. If the declaration is empty the
    expected graph is empty, which is the state the shipped manifest is in today
    and is what TC-E19's original ``== []`` asserted as its degenerate case.
    """

    return frozenset(
        MembershipEdge(
            role=edge.role,
            member=edge.member,
            set=edge.set,
            inherit=edge.inherit,
            admin=edge.admin,
        )
        for edge in declared
    )


def assess_membership_graph(
    *, observed: frozenset[MembershipEdge], declared: Iterable[RoleMembership]
) -> str | None:
    """Compare the controlled membership graph against the declaration. Pure.

    Set equality, so an undeclared edge fails as firmly as a missing one and a
    declared edge whose ``SET``, ``INHERIT`` or ``ADMIN`` option differs fails as
    a mismatched pair rather than being tolerated as "present". Not "at most",
    not "at least" -- an undeclared edge is a path nobody reviewed, and a missing
    declared edge means the configuration the design rests on is not there.
    """

    if observed != declared_edges(declared):
        return PreconditionCode.EXECUTION_ROLE_UNAVAILABLE.value
    return None


def assess_execution_role(*, observed: ObservedRole, declared: ExecutionRoleProfile) -> str | None:
    """Compare one role's catalogue state against its declaration. Pure.

    Returns ``None`` when the role is safe to assume, or the sanitized code the
    caller raises. One code for every cause: an operator learns *that* the role
    is unusable, and the specific reason belongs in telemetry rather than in an
    exception payload that a tenant-facing error might carry (R-E9).

    The membership edge is **not** checked here. It is a property of the graph
    and is asserted once per preflight by ``assess_membership_graph``.
    """

    unavailable = PreconditionCode.EXECUTION_ROLE_UNAVAILABLE.value

    if not observed.exists:
        return unavailable

    # (b) Attributes match the declared profile. The manifest loader already
    # refuses a profile that declares an unsafe value, so this compares against
    # a declaration that is known safe -- but it compares rather than assumes,
    # because the loader guards the file and this guards the database.
    for attribute in _SAFETY_ATTRIBUTES:
        if getattr(observed, attribute) != getattr(declared, attribute):
            return unavailable

    # (d) Effective capability, on top of the structure rather than instead of
    # it. `SET`, never `MEMBER` (V8).
    if not observed.has_role_set:
        return unavailable

    return None


def observed_from_row(row: Mapping[str, object], *, has_role_set: bool) -> ObservedRole:
    """Assemble an ``ObservedRole`` from a catalogue row.

    Separated from the query so the shape of what the catalogue returned is
    visible in one place, and so a change to the query is a change to one
    function rather than to the comparison.
    """

    return ObservedRole(
        exists=True,
        login=bool(row["rolcanlogin"]),
        superuser=bool(row["rolsuper"]),
        createdb=bool(row["rolcreatedb"]),
        createrole=bool(row["rolcreaterole"]),
        replication=bool(row["rolreplication"]),
        bypassrls=bool(row["rolbypassrls"]),
        has_role_set=has_role_set,
    )


MISSING_ROLE: Final = ObservedRole(
    exists=False,
    login=False,
    superuser=False,
    createdb=False,
    createrole=False,
    replication=False,
    bypassrls=False,
    has_role_set=False,
)


_ROLE_ATTRIBUTES_SQL: Final = """
    SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
           rolreplication, rolbypassrls
      FROM pg_roles
     WHERE rolname = %s
"""

# Direct edges only, and only between two controlled roles. `pg_auth_members`
# rows are the direct grants; a role reached through an intermediate has no row
# here, so transitive-only satisfaction presents as a declared edge that is
# absent rather than as a pass.
#
# Both endpoints are constrained to the controlled set, which is passed in from
# the manifest rather than expressed as a name pattern here: a `LIKE 'haloflow%'`
# predicate would be a role-name literal, and would be wrong in both directions.
_CONTROLLED_EDGES_SQL: Final = """
    SELECT role_row.rolname AS role, member_role.rolname AS member,
           edge.set_option, edge.inherit_option, edge.admin_option
      FROM pg_auth_members AS edge
      JOIN pg_roles AS role_row ON role_row.oid = edge.roleid
      JOIN pg_roles AS member_role ON member_role.oid = edge.member
     WHERE role_row.rolname = ANY(%s) AND member_role.rolname = ANY(%s)
"""

# `SET`, never `MEMBER` (V8).
_HAS_ROLE_SET_SQL: Final = "SELECT pg_has_role(%s, %s, 'SET')"


async def read_membership_graph(
    connection: object, controlled: frozenset[str]
) -> frozenset[MembershipEdge]:
    """Read every direct edge between two controlled roles. One query, no writes.

    An empty controlled set is answered without a query: there is no graph to
    read, and asking would be a round trip to learn nothing.
    """

    if not controlled:
        return frozenset()

    names = sorted(controlled)
    async with connection.cursor() as cursor:  # type: ignore[attr-defined]
        await cursor.execute(_CONTROLLED_EDGES_SQL, (names, names))
        rows = await cursor.fetchall()

    return frozenset(
        MembershipEdge(
            role=str(row[0]),
            member=str(row[1]),
            set=bool(row[2]),
            inherit=bool(row[3]),
            admin=bool(row[4]),
        )
        for row in rows
    )


async def read_execution_role(connection: object, role: str) -> ObservedRole:
    """Read one role's attributes and effective capability. No writes.

    Every input is readable with no prior write (V17, V18), so this genuinely
    precedes the grant rather than merely being ordered before it.
    """

    async with connection.cursor() as cursor:  # type: ignore[attr-defined]
        await cursor.execute(_ROLE_ATTRIBUTES_SQL, (role,))
        attributes = await cursor.fetchone()
        if attributes is None:
            return MISSING_ROLE

        await cursor.execute(_HAS_ROLE_SET_SQL, (MIGRATOR_ROLE, role))
        has_role_set_row = await cursor.fetchone()

    return observed_from_row(
        {
            "rolcanlogin": attributes[0],
            "rolsuper": attributes[1],
            "rolcreatedb": attributes[2],
            "rolcreaterole": attributes[3],
            "rolreplication": attributes[4],
            "rolbypassrls": attributes[5],
        },
        has_role_set=bool(has_role_set_row and has_role_set_row[0]),
    )


async def assert_execution_roles_safe(
    connection: object,
    registry: object,
    *,
    manifest: object | None = None,
) -> None:
    """Stage 1 (R-P1B.4, R-P1B.7, R-P1B.15).

    One component, invoked by the provisioner before the grant and by the runner
    before it applies units -- one implementation and two call sites, not two
    checks that could drift apart. Idempotent, and read-only throughout.

    **The membership check runs unconditionally**, before and independently of
    any execution role. R-P1B.7 governs the application role graph, not the
    registry: ``GRANT haloflow_migrator TO haloflow_runtime`` is an escalation
    between two infrastructure roles and must be refused in a deployment that
    declares no execution role at all -- which is every deployment today. Making
    it conditional on the registry would leave the shipped configuration
    unguarded, which is the state R-P1B.7 exists to end.

    Raises the neutral ``ExecutionRoleUnavailable``; each entry point translates
    it into its own taxonomy.
    """

    from haloflow.m01.errors import ExecutionRoleUnavailable
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    loaded = load_provisioning_manifest() if manifest is None else manifest
    profiles = loaded.execution_role_profiles  # type: ignore[attr-defined]

    observed_graph = await read_membership_graph(
        connection,
        loaded.controlled_roles,  # type: ignore[attr-defined]
    )
    refusal = assess_membership_graph(
        observed=observed_graph,
        declared=loaded.role_memberships,  # type: ignore[attr-defined]
    )
    if refusal is not None:
        raise ExecutionRoleUnavailable(reason_code=refusal)

    declared_roles = sorted(
        {
            unit.execution_role
            for unit in registry  # type: ignore[attr-defined]
            if unit.execution_role is not None
        }
    )

    for role in declared_roles:
        declared = profiles.get(role)
        if declared is None:
            # Composition approved the name; the manifest never described it.
            # There is nothing to compare the catalogue against, and assuming a
            # role no declaration covers is the failure this stage prevents.
            raise ExecutionRoleUnavailable(
                reason_code=PreconditionCode.EXECUTION_ROLE_UNAVAILABLE.value
            )
        observed = await read_execution_role(connection, role)
        refusal = assess_execution_role(observed=observed, declared=declared)
        if refusal is not None:
            raise ExecutionRoleUnavailable(reason_code=refusal)

    # R-P1B.2 applies to the infrastructure migrator even when no execution
    # role is declared. Read live state on every entry; never normalize a role
    # that may have been created by a different deployment authority.
    async with connection.cursor() as cursor:  # type: ignore[attr-defined]
        await cursor.execute(
            "SELECT rolcreaterole FROM pg_catalog.pg_roles WHERE rolname = %s",
            (MIGRATOR_ROLE,),
        )
        migrator = await cursor.fetchone()
    if migrator is None:
        raise ExecutionRoleUnavailable(
            reason_code=PreconditionCode.MIGRATOR_ROLE_MISSING.value
        )
    if migrator[0]:
        raise ExecutionRoleUnavailable(
            reason_code=PreconditionCode.MIGRATOR_ROLE_UNSAFE.value
        )


__all__ = [
    "MISSING_ROLE",
    "MembershipEdge",
    "ObservedRole",
    "assert_execution_roles_safe",
    "assess_execution_role",
    "assess_membership_graph",
    "declared_edges",
    "observed_from_row",
    "read_execution_role",
    "read_membership_graph",
]
