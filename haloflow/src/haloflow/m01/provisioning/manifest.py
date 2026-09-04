"""The provisioning manifest: role profiles, membership edges, schema privileges
and table overrides, validated at load (R-P1B.12, R-P3.2, R-P3.4; A5, A7).

**Why a separate file from ``permissions.json``.** Design v7 says the blocks below
are siblings *inside* ``permissions.json``. They cannot be: that file is a flat
map of role name to ``{allow, deny}``, and four consumers iterate its top-level
keys as roles and index ``policy["allow"]``. Adding any sibling raises ``KeyError``
in all four, one of which is a prohibited test surface. Measured before changing
anything (``claude_note-14``), approved as a variance (``chatgpt_note-15``), and
carried as an open wording correction against R-P1B.12, A5 and A7.

``permissions.json`` is therefore untouched here. It is still *read*, for one
purpose the design requires: an override's ``narrows`` token must be one the role
actually holds (A5), and only the capability manifest knows that. The four blocks
are never sought there, never merged, and never defaulted from it.

**Everything fails closed.** A missing entry is an error rather than an empty
set. That is not defensiveness in the abstract: the audit-projector gap that D22
exists to close was a *silently absent* declaration, and an absent entry read as
"no expectation" is precisely how a grantee escapes a completeness check.

**The validation is strict and named, not heuristic.** Blocks are recognized by
name and an unknown sibling is refused, rather than sniffed by looking for a
characteristic child key -- a heuristic would silently accept tomorrow's typo as
a new kind of thing.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Final

from haloflow.m01.errors import MigrationManifestRejected
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.roles import (
    AUDIT_PROJECTOR_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    PROVISIONING_ROLES,
    RUNTIME_ROLE,
)

MANIFEST_PACKAGE: Final = "haloflow.m01.manifests"
PROVISIONING_MANIFEST: Final = "provisioning.json"
PERMISSIONS_MANIFEST: Final = "permissions.json"

_BLOCK_EXECUTION_ROLE_PROFILES: Final = "execution_role_profiles"
_BLOCK_ROLE_MEMBERSHIPS: Final = "role_memberships"
_BLOCK_SCHEMA_PRIVILEGES: Final = "tenant_schema_role_privileges"
_BLOCK_TABLE_OVERRIDES: Final = "tenant_table_overrides"

_REQUIRED_BLOCKS: Final[frozenset[str]] = frozenset(
    {
        _BLOCK_EXECUTION_ROLE_PROFILES,
        _BLOCK_ROLE_MEMBERSHIPS,
        _BLOCK_SCHEMA_PRIVILEGES,
        _BLOCK_TABLE_OVERRIDES,
    }
)

# A tenant schema admits exactly these two privileges. Anything else in the
# declaration is a category error, not a narrower grant.
SCHEMA_PRIVILEGES: Final[frozenset[str]] = frozenset({"USAGE", "CREATE"})

# The seven table privileges PostgreSQL can grant. An override narrows within
# this vocabulary and never outside it.
TABLE_PRIVILEGES: Final[frozenset[str]] = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)

# The M01-owned tables an override may name. `operation_registry` is the case A5
# exists for: V7 measured that `t001`'s default privileges hand the runtime role
# UPDATE and DELETE on it with no grant statement anywhere naming the table.
TENANT_TABLES: Final[frozenset[str]] = frozenset({"access_audit_outbox", "operation_registry"})

# The capability tokens a tenant schema recognizes, enumerated rather than
# inferred (R-P3.2). Finding F-3 was `permissions.json` verified only against
# itself; a token nobody has classified must fail rather than pass unnoticed.
_TENANT_SCHEMA_CAPABILITIES: Final[Mapping[str, tuple[str, ...]]] = {
    "business_dml": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "business_runtime": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "checksummed_ddl": (),
    "ownership": (),
    "provision": (),
    "write": ("INSERT", "UPDATE", "DELETE"),
    "approved_support_read": ("SELECT",),
    "approved_emergency_read": ("SELECT",),
    "separately_approved_emergency_write": ("SELECT", "INSERT", "UPDATE", "DELETE"),
}
_TENANT_TABLE_CAPABILITIES: Final[Mapping[str, tuple[str, ...]]] = {
    "select": ("SELECT",),
    "insert": ("INSERT",),
}

# R-P1B.4(b). The safe execution-role profile is fixed, not configurable: every
# one of these attributes must be false. Stage 1 compares the database role
# against the declaration, so a declaration permitted to say `true` would let a
# dangerous role match dangerous data and pass the check that exists to stop it.
_PROFILE_SAFETY_ATTRIBUTES: Final[tuple[str, ...]] = (
    "login",
    "superuser",
    "createdb",
    "createrole",
    "replication",
    "bypassrls",
)

# R-P1B.3, R-P1B.4(c), A7. The one permitted membership edge, by value.
# `INHERIT FALSE` is deliberate and measured (V15): the migrator's ordinary
# statements must not carry the execution role's privileges -- only an explicit
# `SET ROLE` does. `ADMIN FALSE` stops the migrator granting the role onward.
_REQUIRED_MEMBERSHIP: Final[Mapping[str, object]] = {
    "member": MIGRATOR_ROLE,
    "set": True,
    "inherit": False,
    "admin": False,
}

# R-P1B.13, A7, D22. The four infrastructure baselines are fixed sets, stored
# sorted so comparison is against canonical form. Stage 2 installs and stage 3
# verifies whatever is declared here, so a weakened entry becomes policy.
_INFRASTRUCTURE_SCHEMA_PRIVILEGES: Final[Mapping[str, tuple[str, ...]]] = {
    PROVISIONER_ROLE: ("CREATE", "USAGE"),
    MIGRATOR_ROLE: ("CREATE", "USAGE"),
    RUNTIME_ROLE: ("USAGE",),
    AUDIT_PROJECTOR_ROLE: ("USAGE",),
}

# The role class is derived from which role it is, never taken on trust.
_INFRASTRUCTURE_ROLE_CLASSES: Final[Mapping[str, str]] = {
    PROVISIONER_ROLE: "owner",
    MIGRATOR_ROLE: "infrastructure",
    RUNTIME_ROLE: "infrastructure",
    AUDIT_PROJECTOR_ROLE: "infrastructure",
}


def _reject(code: PreconditionCode) -> MigrationManifestRejected:
    """Refusals carry a code and never a value (R-E9)."""

    return MigrationManifestRejected(reason_code=code.value)


@dataclass(frozen=True, slots=True)
class ExecutionRoleProfile:
    """What a declared execution role must look like in ``pg_roles`` (A7)."""

    login: bool
    superuser: bool
    createdb: bool
    createrole: bool
    replication: bool
    bypassrls: bool
    tenant_schema_privileges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoleMembership:
    """One expected edge in ``pg_auth_members``, options included (A7)."""

    role: str
    member: str
    set: bool
    inherit: bool
    admin: bool


@dataclass(frozen=True, slots=True)
class SchemaPrivilegeEntry:
    """One grantee's complete tenant-schema expectation (A7, D22)."""

    privileges: tuple[str, ...]
    is_grantable: bool
    grantor: str
    role_class: str


@dataclass(frozen=True, slots=True)
class TableOverride:
    """A declared narrowing of one capability token, for one table (A5)."""

    role: str
    table: str
    narrows: str
    privileges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProvisioningManifest:
    """The validated manifest. Every collection is canonically ordered."""

    execution_role_profiles: Mapping[str, ExecutionRoleProfile]
    role_memberships: tuple[RoleMembership, ...]
    tenant_schema_role_privileges: Mapping[str, SchemaPrivilegeEntry]
    tenant_table_overrides: tuple[TableOverride, ...]


def classify_tenant_schema_token(token: str) -> tuple[str, ...]:
    """The table privileges a ``tenant_schema`` token confers.

    Raises rather than returning ``None`` for an unrecognized token (R-P3.2): a
    caller that forgot to check a sentinel is exactly the silence this control
    exists to remove. An empty tuple is a real answer -- ``ownership`` and
    ``checksummed_ddl`` confer no table privilege.
    """

    if token.startswith("tenant_schema."):
        table_and_capability = token[len("tenant_schema.") :]
        table, separator, capability = table_and_capability.partition(":")
        if not separator or table not in TENANT_TABLES:
            raise _reject(PreconditionCode.MANIFEST_TOKEN_UNRECOGNIZED)
        if capability not in _TENANT_TABLE_CAPABILITIES:
            raise _reject(PreconditionCode.MANIFEST_TOKEN_UNRECOGNIZED)
        return _TENANT_TABLE_CAPABILITIES[capability]

    if not token.startswith("tenant_schema:"):
        raise _reject(PreconditionCode.MANIFEST_TOKEN_UNRECOGNIZED)

    capability = token[len("tenant_schema:") :]
    if capability not in _TENANT_SCHEMA_CAPABILITIES:
        raise _reject(PreconditionCode.MANIFEST_TOKEN_UNRECOGNIZED)
    return _TENANT_SCHEMA_CAPABILITIES[capability]


def _permissions_document() -> Mapping[str, object]:
    """The capability manifest, read only to resolve which tokens a role holds."""

    text = resources.files(MANIFEST_PACKAGE).joinpath(PERMISSIONS_MANIFEST).read_text()
    document = json.loads(text)
    if not isinstance(document, Mapping):
        raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)
    return document


def _declared_roles(permissions: Mapping[str, object]) -> frozenset[str]:
    """Every role the capability manifest names."""

    return frozenset(permissions)


def _tokens_held_by(permissions: Mapping[str, object], role: str) -> tuple[str, ...]:
    policy = permissions.get(role)
    if not isinstance(policy, Mapping):
        return ()
    allow = policy.get("allow", ())
    if not isinstance(allow, Sequence) or isinstance(allow, str):
        return ()
    return tuple(token for token in allow if isinstance(token, str))


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)
    for key in value:
        if not isinstance(key, str):
            raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)
    return value


def _require_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)
    return tuple(value)


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)
    return value


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)
    return value


def _require_privileges(
    value: object, allowed: frozenset[str], unknown: PreconditionCode
) -> tuple[str, ...]:
    """A privilege collection, canonically ordered, every member recognized."""

    members = _require_sequence(value)
    names: list[str] = []
    for member in members:
        name = _require_str(member)
        if name not in allowed:
            raise _reject(unknown)
        names.append(name)
    if len(set(names)) != len(names):
        raise _reject(PreconditionCode.MANIFEST_DUPLICATE_DECLARATION)
    return tuple(sorted(names))


def _load_execution_role_profiles(block: object) -> Mapping[str, ExecutionRoleProfile]:
    profiles: dict[str, ExecutionRoleProfile] = {}
    for role, raw in sorted(_require_mapping(block).items()):
        entry = _require_mapping(raw)
        if role in PROVISIONING_ROLES:
            # R-P1B.22(a): an infrastructure role is never an execution role, so
            # it never has a profile here either.
            raise _reject(PreconditionCode.MANIFEST_ROLE_UNKNOWN)
        for attribute in _PROFILE_SAFETY_ATTRIBUTES:
            if _require_bool(entry.get(attribute)):
                # R-P1B.4(b). The profile is what stage 1 compares the database
                # role against, so it may not declare the unsafe value: a
                # dangerous role would match dangerous data and pass.
                raise _reject(PreconditionCode.EXECUTION_ROLE_PROFILE_UNSAFE)
        profiles[role] = ExecutionRoleProfile(
            login=False,
            superuser=False,
            createdb=False,
            createrole=False,
            replication=False,
            bypassrls=False,
            tenant_schema_privileges=_require_privileges(
                entry.get("tenant_schema_privileges"),
                SCHEMA_PRIVILEGES,
                PreconditionCode.MANIFEST_PRIVILEGE_UNKNOWN,
            ),
        )
    return profiles


def _load_role_memberships(
    block: object, profiles: Mapping[str, ExecutionRoleProfile]
) -> tuple[RoleMembership, ...]:
    edges: list[RoleMembership] = []
    seen: set[str] = set()
    for raw in _require_sequence(block):
        entry = _require_mapping(raw)
        role = _require_str(entry.get("role"))
        member = _require_str(entry.get("member"))
        if role not in profiles:
            raise _reject(PreconditionCode.MANIFEST_ROLE_UNKNOWN)
        if role in seen:
            # One edge per role: a second is a contradiction about the same
            # expectation, and stage 1 asserts an exact set.
            raise _reject(PreconditionCode.MANIFEST_DUPLICATE_DECLARATION)
        seen.add(role)

        declared: Mapping[str, object] = {
            "member": member,
            "set": _require_bool(entry.get("set")),
            "inherit": _require_bool(entry.get("inherit")),
            "admin": _require_bool(entry.get("admin")),
        }
        if declared != _REQUIRED_MEMBERSHIP:
            # R-P1B.3 and R-P1B.4(c) fix this edge by value. Letting the file
            # define it would make stage 1's exact-set check bless an unsafe
            # edge rather than reject it.
            raise _reject(PreconditionCode.ROLE_MEMBERSHIP_DECLARATION_UNSAFE)

        edges.append(
            RoleMembership(
                role=role,
                member=MIGRATOR_ROLE,
                set=True,
                inherit=False,
                admin=False,
            )
        )

    undeclared = set(profiles) - seen
    if undeclared:
        # A profile with no edge leaves stage 1 nothing to compare. An absent
        # expectation is how a grantee escapes a completeness check -- the shape
        # of the audit-projector gap D22 exists to close.
        raise _reject(PreconditionCode.ROLE_MEMBERSHIP_DECLARATION_MISSING)

    return tuple(sorted(edges, key=lambda edge: (edge.role, edge.member)))


def _load_schema_privileges(
    block: object, profiles: Mapping[str, ExecutionRoleProfile]
) -> Mapping[str, SchemaPrivilegeEntry]:
    """The complete tenant-schema ACL declaration -- all five grantee classes."""

    declared = _require_mapping(block)
    entries: dict[str, SchemaPrivilegeEntry] = {}

    for role, raw in sorted(declared.items()):
        entry = _require_mapping(raw)
        if role not in PROVISIONING_ROLES and role not in profiles:
            raise _reject(PreconditionCode.MANIFEST_ROLE_UNKNOWN)
        privileges = _require_privileges(
            entry.get("privileges"),
            SCHEMA_PRIVILEGES,
            PreconditionCode.MANIFEST_PRIVILEGE_UNKNOWN,
        )
        if _require_bool(entry.get("is_grantable")):
            # R-P1B.22(c): no manifest may declare grant option today. Onward
            # delegation is the mechanism stage 3 exists to make impossible.
            raise _reject(PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID)
        if _require_str(entry.get("grantor")) != PROVISIONER_ROLE:
            raise _reject(PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID)
        if role in _INFRASTRUCTURE_SCHEMA_PRIVILEGES:
            # R-P1B.13, A7, D22. These four sets are fixed, not declared.
            # Stage 2 installs and stage 3 verifies whatever this file says, so
            # a weakened entry becomes the policy rather than failing against it.
            # This subsumes TC-P68's runtime-CREATE case and refuses the other
            # direction too: an emptied or narrowed baseline is equally wrong.
            if privileges != _INFRASTRUCTURE_SCHEMA_PRIVILEGES[role]:
                raise _reject(PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID)
            if _require_str(entry.get("role_class")) != _INFRASTRUCTURE_ROLE_CLASSES[role]:
                raise _reject(PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID)
        elif _require_str(entry.get("role_class")) != "execution":
            raise _reject(PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_INVALID)
        entries[role] = SchemaPrivilegeEntry(
            privileges=privileges,
            is_grantable=False,
            grantor=PROVISIONER_ROLE,
            role_class=_INFRASTRUCTURE_ROLE_CLASSES.get(role, "execution"),
        )

    missing = PROVISIONING_ROLES - set(entries)
    if missing:
        raise _reject(PreconditionCode.SCHEMA_PRIVILEGE_DECLARATION_MISSING)

    # The fifth class: each declared execution role contributes its entry from
    # its profile, in the same shape, so all five reach the builder together.
    for role, profile in profiles.items():
        if role in entries:
            raise _reject(PreconditionCode.MANIFEST_DUPLICATE_DECLARATION)
        entries[role] = SchemaPrivilegeEntry(
            privileges=profile.tenant_schema_privileges,
            is_grantable=False,
            grantor=PROVISIONER_ROLE,
            role_class="execution",
        )

    return {role: entries[role] for role in sorted(entries)}


def _load_table_overrides(
    block: object, permissions: Mapping[str, object]
) -> tuple[TableOverride, ...]:
    """Per-table narrowing. An override may only ever reduce (A5)."""

    roles = _declared_roles(permissions)
    overrides: list[TableOverride] = []
    seen: set[tuple[str, str]] = set()

    for raw in _require_sequence(block):
        entry = _require_mapping(raw)
        role = _require_str(entry.get("role"))
        table = _require_str(entry.get("table"))
        narrows = _require_str(entry.get("narrows"))

        if role not in roles:
            raise _reject(PreconditionCode.MANIFEST_ROLE_UNKNOWN)
        if table not in TENANT_TABLES:
            raise _reject(PreconditionCode.MANIFEST_TABLE_UNKNOWN)
        if (role, table) in seen:
            # Both a duplicate and a conflicting pair land here: two statements
            # about one (role, table) is a contradiction whether or not they
            # agree, and resolving it by order would hide which one won.
            raise _reject(PreconditionCode.MANIFEST_DUPLICATE_DECLARATION)
        seen.add((role, table))

        privileges = _require_privileges(
            entry.get("privileges"),
            TABLE_PRIVILEGES,
            PreconditionCode.MANIFEST_PRIVILEGE_UNKNOWN,
        )
        if narrows not in _tokens_held_by(permissions, role):
            # Narrowing a token the role does not hold is not a narrowing.
            raise _reject(PreconditionCode.MANIFEST_OVERRIDE_INVALID)
        conferred = set(classify_tenant_schema_token(narrows))
        if not set(privileges) < conferred:
            # Strict subset: equal is not a narrowing, and wider is a widening
            # wearing the word "override".
            raise _reject(PreconditionCode.MANIFEST_OVERRIDE_INVALID)

        overrides.append(
            TableOverride(role=role, table=table, narrows=narrows, privileges=privileges)
        )

    return tuple(sorted(overrides, key=lambda override: (override.role, override.table)))


def load_provisioning_manifest(
    document: Mapping[str, object] | None = None,
) -> ProvisioningManifest:
    """Load and validate the provisioning manifest. Pure -- no database access.

    ``document`` is for tests, which exercise refusals without writing files. The
    shipped manifest is resolved through the package rather than the working
    directory, so a process started elsewhere loads the same file.
    """

    if document is None:
        text = resources.files(MANIFEST_PACKAGE).joinpath(PROVISIONING_MANIFEST).read_text()
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as error:
            raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED) from error
        document = loaded if isinstance(loaded, Mapping) else None
        if document is None:
            raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)

    blocks = _require_mapping(document)
    if set(blocks) != _REQUIRED_BLOCKS:
        # Named, not sniffed. An unknown sibling is a refusal rather than an
        # ignored key, so a typo cannot become a silently absent declaration.
        raise _reject(PreconditionCode.PROVISIONING_MANIFEST_MALFORMED)

    permissions = _permissions_document()
    profiles = _load_execution_role_profiles(blocks[_BLOCK_EXECUTION_ROLE_PROFILES])

    return ProvisioningManifest(
        execution_role_profiles=profiles,
        role_memberships=_load_role_memberships(blocks[_BLOCK_ROLE_MEMBERSHIPS], profiles),
        tenant_schema_role_privileges=_load_schema_privileges(
            blocks[_BLOCK_SCHEMA_PRIVILEGES], profiles
        ),
        tenant_table_overrides=_load_table_overrides(blocks[_BLOCK_TABLE_OVERRIDES], permissions),
    )
