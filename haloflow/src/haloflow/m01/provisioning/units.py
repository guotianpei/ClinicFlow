"""Ordered, checksummed per-tenant migration units.

The construction path deliberately mirrors the statement catalogue: units carry
a private issuer sentinel, composition is startup-only through one public
builder, and production units live in exactly one place. A test-only unit is
supplied explicitly by tests, passes through the same validation, and cannot
enter the production registry (R-E12).

Unit SQL is a *template*. ``{schema}`` is substituted with a schema key that has
already been matched against ``SCHEMA_KEY_PATTERN``; a schema name cannot be a
bound parameter, so the pattern is the control that keeps an identifier out of
the injection position. Checksums are taken over the template, not the rendered
text, so one migration has one checksum across every tenant and drift means the
same thing everywhere.
"""

import copy
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Any, Final, cast

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.checksum import unit_checksum
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.function_checksum import (
    FUNCTION_CHECKSUM_VERSION,
    function_checksum,
)
from haloflow.m01.provisioning.installed_state import (
    NOT_REQUIRED,
    AclAmendment,
    InstalledStateProfile,
    InstalledStateRequirement,
    check_profile_agreement,
    profile_digest,
    requirement_for,
)
from haloflow.m01.provisioning.roles import (
    AUDIT_PROJECTOR_ROLE,
    PROVISIONING_ROLES,
    RUNTIME_ROLE,
)
from haloflow.m01.provisioning.verification import (
    FunctionMetadataVerification,
    Verification,
    validate_verification,
)
from haloflow.m01.resolver import SCHEMA_KEY_PATTERN

MIGRATION_ID_PATTERN: Final = re.compile(r"^t\d{3}(_test)?_[a-z0-9_]{1,64}$")
# A1. Narrower than PostgreSQL allows, deliberately: the name reaches SQL as an
# identifier, and a pattern that admits only what M01 needs is a smaller thing to
# reason about than quoting rules.
EXECUTION_ROLE_PATTERN: Final = re.compile(r"^haloflow_[a-z0-9_]{1,48}$")
_TEST_UNIT_PATTERN: Final = re.compile(r"^t\d{3}_test_")
_SCHEMA_PLACEHOLDER: Final = "{schema}"
_UNIT_ISSUER: Final = object()

# CP2-1. A unit is typed **because it says so**, never because its template
# happens to contain function DDL (TP-R2). One declared kind exists; anything
# else, including a kind of the wrong type, is refused rather than defaulted
# (TP-R2b), because defaulting either way is inference.
TYPED_FUNCTION_KIND: Final = "typed_function_v3"
# L-1W W3b (plan v1r1 §4, Codex 150630Z/151751Z): a remove-only closure unit. It
# carries NO authored template; the runner executes only the REVOKE statements
# rendered from the composition-declared `AclAmendment` records naming it, under its
# execution role (the targets' owner). CG4 and the typed policy are unchanged.
ACL_CLOSURE_KIND: Final = "acl_closure_v1"
ACL_CLOSURE_CHECKSUM_VERSION: Final = 2
_POLICY_FIELDS: Final = frozenset(
    {
        "policy_format",
        "semantic_version",
        "parser_package",
        "parser_version",
        "grammar_major",
        "functions",
    }
)
_POLICY_VERIFICATION_FIELDS: Final = frozenset({"kind", "functions"})


def _reject(code: PreconditionCode) -> MigrationUnitRejected:
    return MigrationUnitRejected(reason_code=code.value)


# B2, the ONE documented adapter boundary onto frozen CP1. `_closed_shape` is a
# pure, recursive, closed-key/exact-type check that already raises
# `INSTALL_POLICY_INVALID`; it touches no parser, catalogue, schema key or
# database. It is reused rather than mirrored here, because a second copy of a
# schema free to drift from the first is the exact defect CP2-1 exists to close.
#
# Deliberately NOT reused: `validate_function_installation` and `_declaration`.
# Those carry body-hash and policy-consistency checks that belong at
# pending-install, and hoisting them would move the checker's job into
# composition (TP-R13).
from haloflow.m01.provisioning.function_policy import (  # noqa: E402
    _closed_shape as _frozen_closed_shape,
)

# The authoritative verification vocabulary, READ from the frozen declaration
# rather than restated as a literal. It is NOT `TYPED_FUNCTION_KIND`: that is the
# UNIT's kind (`typed_function_v3`), while this is the verification BLOCK's kind
# (`function_metadata`). Conflating the two would reject every valid typed unit.
VERIFICATION_KIND: Final[str] = cast(
    str, FunctionMetadataVerification.__dataclass_fields__["kind"].default
)


def _frozen(value: Any) -> Any:
    """Recursively immutable: mappings become read-only views, lists become tuples."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def _thawed(value: Any) -> Any:
    """A fresh, ordinary, MUTABLE structure, rebuilt on every call.

    The frozen serializer cannot encode a `MappingProxyType` at all, and a caller
    holding what it was handed must not be able to reach the snapshot. So the
    boundary thaws rather than exposing the frozen containers.
    """

    if isinstance(value, Mapping):
        return {key: _thawed(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thawed(item) for item in value]
    return value


def _adopt_declaration(unit: "TenantMigrationUnit") -> None:
    """Own the typed declaration: validate the ORIGINAL, then store it frozen.

    ORDER IS THE CONTRACT, and Codex ruled on it:

    1. Deep-copy the caller's structures, preserving their ORIGINAL types. A
       caller's tuple stays a tuple here, so step 2 rejects it. Freezing or
       thawing first would normalize that tuple into an accepted list and the
       malformed input would pass unnoticed.
    2. Validate STRUCTURE through the frozen shape helper. Every structural
       fault, INCLUDING a non-string `kind`, is `INSTALL_POLICY_INVALID`.
    3. Only then the narrow VOCABULARY check: a well-shaped block whose `kind`
       is an unsupported string is `VERIFICATION_KIND_UNKNOWN`. Running this
       before step 2 would report a vocabulary failure for an integer kind.
    4. Freeze recursively and store, so nothing reachable through `unit.policy`
       can alter the snapshot afterwards.
    """

    # Typed as `Any` on purpose: until `_closed_shape` has run these are
    # UNVALIDATED caller data of unknown shape, and annotating them as mappings
    # would assert a structure that is exactly what is still being checked.
    policy: Any = copy.deepcopy(unit.policy)
    verification: Any = copy.deepcopy(unit.policy_verification)

    _frozen_closed_shape(
        {
            "checksum_version": FUNCTION_CHECKSUM_VERSION,
            "migration_id": unit.migration_id,
            "execution_role": unit.execution_role,
            "template": unit.template,
            "verification": verification,
            "policy": policy,
        }
    )

    if verification["kind"] != VERIFICATION_KIND:
        raise _reject(PreconditionCode.VERIFICATION_KIND_UNKNOWN)

    object.__setattr__(unit, "policy", _frozen(policy))
    object.__setattr__(unit, "policy_verification", _frozen(verification))


def _declaration_checks(
    *,
    kind: str | None,
    execution_role: str | None,
    policy: Mapping[str, Any] | None,
    policy_verification: Mapping[str, Any] | None,
    verification: Verification | None,
) -> None:
    """Pure declaration checks for the typed/ordinary boundary (TP-R13).

    Pure: no parser, no catalogue, no schema key, no database. The actual-schema
    checker runs later, per pending install, and composition never calls it.
    """

    typed_fields = policy is not None or policy_verification is not None
    if kind == ACL_CLOSURE_KIND:
        # A closure unit is run by its targets' owner and declares nothing else:
        # no typed policy, no legacy verification. Its template is checked empty by
        # the definition and the unit, which receive it.
        if execution_role is None:
            raise _reject(PreconditionCode.MIGRATION_UNIT_ROLE_REQUIRED)
        if typed_fields or verification is not None:
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        return
    if kind is None:
        # TP-R2c. An ordinary definition carrying typed-only fields is refused,
        # never accepted with the policy silently unenforced.
        if typed_fields:
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        return
    if kind != TYPED_FUNCTION_KIND:
        raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)

    # TP-R3 / O-2. Checked before the policy's shape: the role is what the
    # installed function is owned by, and its absence is its own refusal.
    if execution_role is None:
        raise _reject(PreconditionCode.MIGRATION_UNIT_ROLE_REQUIRED)
    # TP-R2a. Missing, null or malformed `policy` is refused, never downgraded.
    for block, required in (
        (policy, _POLICY_FIELDS),
        (policy_verification, _POLICY_VERIFICATION_FIELDS),
    ):
        if not isinstance(block, Mapping) or not required <= set(block):
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
    if verification is not None:
        # The typed declaration carries its own verification block; the legacy
        # field would be a second, unreconciled expectation for one unit.
        raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)


@dataclass(frozen=True, slots=True)
class UnitDefinition:
    """A unit's definition when it needs to say more than its template.

    A definition set maps a migration id to either a bare template string --
    which is every M01 unit today -- or to this record. Keeping the bare string
    valid means adding the execution role did not touch a single production
    definition, so TC-P5 characterizes the baseline rather than a rewrite of it.
    Verification is optional declarative metadata, validated at construction.
    """

    template: str
    execution_role: str | None = None
    verification: Verification | None = None
    kind: str | None = None
    """CP2-1: `TYPED_FUNCTION_KIND`, or `None` for an ordinary unit (TP-R2)."""
    policy: Mapping[str, Any] | None = None
    policy_verification: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind == ACL_CLOSURE_KIND and self.template != "":
            # W3b AM-04: a closure unit never carries authored SQL.
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        validate_verification(self.verification)
        _declaration_checks(
            kind=self.kind,
            execution_role=self.execution_role,
            policy=self.policy,
            policy_verification=self.policy_verification,
            verification=self.verification,
        )


UnitDefinitions = Mapping[str, "str | UnitDefinition"]


@dataclass(frozen=True, slots=True)
class TenantMigrationUnit:
    """One ordered, checksummed per-tenant migration. Constructed only here."""

    migration_id: str
    template: str = field(repr=False)
    execution_role: str | None = None
    _issuer: InitVar[object | None] = None
    verification: Verification | None = field(default=None, kw_only=True)
    kind: str | None = field(default=None, kw_only=True)
    policy: Mapping[str, Any] | None = field(default=None, kw_only=True, repr=False)
    policy_verification: Mapping[str, Any] | None = field(
        default=None, kw_only=True, repr=False
    )
    closure_payload: Mapping[str, Any] | None = field(default=None, kw_only=True, repr=False)
    """W3b: the canonical declaration an ``acl_closure_v1`` unit's checksum digests.
    Built by the registry builder from the validated amendments; None otherwise."""

    def __post_init__(self, _issuer: object | None) -> None:
        if _issuer is not _UNIT_ISSUER:
            raise MigrationUnitRejected(reason_code=PreconditionCode.UNTRUSTED_MIGRATION_UNIT.value)
        validate_verification(self.verification)
        _declaration_checks(
            kind=self.kind,
            execution_role=self.execution_role,
            policy=self.policy,
            policy_verification=self.policy_verification,
            verification=self.verification,
        )
        if not MIGRATION_ID_PATTERN.fullmatch(self.migration_id):
            raise MigrationUnitRejected(reason_code=PreconditionCode.MIGRATION_ID_INVALID.value)
        if self.kind == ACL_CLOSURE_KIND:
            if self.template != "" or self.closure_payload is None:
                raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        elif self.closure_payload is not None:
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        elif not self.template.strip():
            raise MigrationUnitRejected(reason_code=PreconditionCode.MIGRATION_TEMPLATE_EMPTY.value)
        if self.kind != ACL_CLOSURE_KIND and _SCHEMA_PLACEHOLDER not in self.template:
            # A unit that names no schema is either unqualified DDL, which would
            # land wherever search_path points, or shared-schema DDL, which is
            # Alembic's territory and not a per-tenant migration at all.
            raise MigrationUnitRejected(
                reason_code=PreconditionCode.MIGRATION_TEMPLATE_UNSCOPED.value
            )
        if self.execution_role is not None:
            # R-P1.3: two controls, and they are independent on purpose. The
            # allow-list says which roles this deployment approved; the pattern
            # says what may reach an identifier position at all. A role can pass
            # one and fail the other, and a single merged check would let a name
            # the allow-list happened to contain through to SQL.
            if not EXECUTION_ROLE_PATTERN.fullmatch(self.execution_role):
                raise MigrationUnitRejected(
                    reason_code=PreconditionCode.EXECUTION_ROLE_INVALID.value
                )
            # R-P1B.22(a), D23. Checked here rather than only against the
            # approved set, so supplying a permissive set cannot reach it.
            # `haloflow_provisioner` is the load-bearing case: it owns every
            # tenant schema, and V29 measured that an execution role owning the
            # schema can mutate `nspacl` during stage 4 -- which would break
            # R-P1B.20 outright. Read from the role vocabulary rather than
            # copied, so a role added to it is covered without a second edit.
            if self.execution_role in PROVISIONING_ROLES:
                raise MigrationUnitRejected(
                    reason_code=PreconditionCode.EXECUTION_ROLE_IS_INFRASTRUCTURE.value
                )

        # LAST, deliberately. `_closed_shape` validates the whole root payload,
        # including the migration id and template, so running it earlier would
        # report a shape failure for a bad migration id in place of
        # `MIGRATION_ID_INVALID` and change refusal precedence the frozen v14
        # assertions already pin.
        if self.is_typed:
            _adopt_declaration(self)

    @property
    def is_test_unit(self) -> bool:
        return bool(_TEST_UNIT_PATTERN.match(self.migration_id))

    @property
    def is_closure(self) -> bool:
        """W3b: an ``acl_closure_v1`` unit (declared, never inferred)."""

        return self.kind == ACL_CLOSURE_KIND

    @property
    def is_typed(self) -> bool:
        """Declared typed at composition. Never inferred from the template."""

        return self.kind == TYPED_FUNCTION_KIND

    @property
    def declaration_payload(self) -> dict[str, Any]:
        """The frozen CP1 function-v3 payload this typed unit declares.

        Assembled from the unit's own fields, so the bytes handed to the checker
        and the bytes the checksum covers describe one declaration rather than
        two copies that could drift apart.
        """

        if not self.is_typed:
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        return {
            "checksum_version": FUNCTION_CHECKSUM_VERSION,
            "migration_id": self.migration_id,
            "execution_role": self.execution_role,
            "template": self.template,
            "verification": _thawed(self.policy_verification or {}),
            "policy": _thawed(self.policy or {}),
        }

    @property
    def checksum(self) -> str:
        """SHA-256 over the versioned canonical payload (R-P4.1, A6).

        Normalizing the template means reindenting a unit does not read as
        drift, while any change to a token does. The digest is over the
        template, not the rendered text, so the value is identical for every
        tenant the unit is applied to.

        v2 digests a payload rather than the template alone, because the
        execution role and the verification specification must be covered too
        and concatenating fields is ambiguous. ``checksum.py`` holds the
        canonicalization and explains why. Every existing checksum changes as a
        result -- known, intended, and gated by R-P4.4.
        """

        if self.is_closure:
            # W3b plan v1r1 §4: SHA-256 over the canonical closure declaration --
            # kind, version, id, role, and per target its unit, version, signature,
            # accepted-profile digest and removals. Any change is ledger drift.
            if self.closure_payload is None:
                raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
            return closure_checksum(self.closure_payload)
        if self.is_typed:
            # TP-R7. A typed unit's ledger identity is the function-v3 digest,
            # which covers the policy block; the v2 digest cannot see it, so a
            # policy change would not read as drift. TP-R7a leaves v2 untouched
            # for every ordinary unit, and TP-R7b falls out of the two digests
            # differing: an id applied under v2 that becomes typed is drift, not
            # a silent skip.
            payload = self.declaration_payload
            return function_checksum(
                migration_id=payload["migration_id"],
                template=payload["template"],
                execution_role=payload["execution_role"],
                verification=payload["verification"],
                policy=payload["policy"],
            )
        return unit_checksum(
            migration_id=self.migration_id,
            template=self.template,
            execution_role=self.execution_role,
            verification=self.verification,
        )

    def render(self, schema_key: str) -> str:
        """Substitute the tenant schema after re-validating it as an identifier."""

        if not SCHEMA_KEY_PATTERN.fullmatch(schema_key):
            raise MigrationUnitRejected(reason_code=PreconditionCode.SCHEMA_KEY_INVALID.value)
        if self.is_closure:
            # A closure unit has no template; its statements come from the registry.
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        return self.template.replace(_SCHEMA_PLACEHOLDER, schema_key)


def closure_checksum(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex over the canonical JSON of a closure payload (pinned formula)."""

    text = json.dumps(
        _thawed(payload), ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _ClosureTarget:
    """One validated amendment, as the registry keeps it."""

    target_unit: str
    target_version: int
    function_name: str
    argument_types: tuple[str, ...]
    activated_by: str
    activator_version: int
    amendment: AclAmendment


class TenantMigrationRegistry:
    """An immutable, ordered registry of units. Construction is restricted.

    CP2-2b (architecture v3 section 5.1(2)): the registry also owns one immutable
    `InstalledStateRequirement` per unit. It is not part of the unit, the
    declaration, the checksum or the ledger identity (R-B9.6).
    """

    __slots__ = ("__units", "__requirements", "__closure_targets")

    def __init__(
        self,
        units: tuple[TenantMigrationUnit, ...],
        *,
        _issuer: object | None = None,
        _requirements: Mapping[str, InstalledStateRequirement] | None = None,
        _closure_targets: tuple[_ClosureTarget, ...] = (),
    ) -> None:
        if _issuer is not _UNIT_ISSUER:
            raise MigrationUnitRejected(
                reason_code=PreconditionCode.UNTRUSTED_MIGRATION_REGISTRY.value
            )
        self.__units = units
        supplied = dict(_requirements or {})
        self.__requirements: Mapping[str, InstalledStateRequirement] = MappingProxyType(
            {unit.migration_id: supplied.get(unit.migration_id, NOT_REQUIRED) for unit in units}
        )
        self.__closure_targets: tuple[_ClosureTarget, ...] = tuple(
            sorted(_closure_targets, key=lambda t: (t.activator_version, t.target_version))
        )

    def __iter__(self) -> Iterator[TenantMigrationUnit]:
        return iter(self.__units)

    def __len__(self) -> int:
        return len(self.__units)

    @property
    def units(self) -> tuple[TenantMigrationUnit, ...]:
        return self.__units

    @property
    def migration_ids(self) -> tuple[str, ...]:
        return tuple(unit.migration_id for unit in self.__units)

    @property
    def target_version(self) -> int:
        """The schema version this registry brings a tenant to.

        The leading ``tNNN`` of the last unit. R-E11: the version's *meaning* is
        "the M01 infrastructure baseline through this unit", not "M02-ready".
        """

        if not self.__units:
            raise MigrationUnitRejected(reason_code=PreconditionCode.MIGRATION_REGISTRY_EMPTY.value)
        return int(self.__units[-1].migration_id[1:4])

    def installed_state_requirement(self, migration_id: str) -> InstalledStateRequirement:
        """The immutable descriptor recorded at composition. `KeyError` for an unknown id."""

        return self.__requirements[migration_id]

    # -- W3b closure ---------------------------------------------------------

    def acl_amendment(self, migration_id: str) -> tuple[AclAmendment, int] | None:
        """The amendment of a typed target and its activator's version, or None."""

        for target in self.__closure_targets:
            if target.target_unit == migration_id:
                return target.amendment, target.activator_version
        return None

    def closure_targets(self, migration_id: str) -> tuple[str, ...]:
        """The typed units a closure unit amends, in target-version order."""

        return tuple(
            t.target_unit for t in self.__closure_targets if t.activated_by == migration_id
        )

    def closure_checksum_payload(self, migration_id: str) -> Mapping[str, Any]:
        """The canonical payload a closure unit's checksum digests."""

        unit = next((u for u in self.__units if u.migration_id == migration_id), None)
        if unit is None or not unit.is_closure or unit.closure_payload is None:
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        thawed: dict[str, Any] = _thawed(unit.closure_payload)
        return thawed

    def closure_statements(self, migration_id: str, schema_key: str) -> tuple[str, ...]:
        """The exact statements a closure unit executes, rendered from its declaration
        only: one REVOKE per removal, ordered by target version then grantee. Names
        and argument types come from the target's accepted profile; grantees were
        validated against the identifier pattern at composition."""

        if not SCHEMA_KEY_PATTERN.fullmatch(schema_key):
            raise MigrationUnitRejected(reason_code=PreconditionCode.SCHEMA_KEY_INVALID.value)
        targets = [t for t in self.__closure_targets if t.activated_by == migration_id]
        if not targets:
            raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
        statements: list[str] = []
        for target in targets:
            signature = f'{target.function_name}({",".join(target.argument_types)})'
            for grantee, _privilege, _grantor, _grantable in sorted(target.amendment.removes):
                statements.append(
                    f'REVOKE EXECUTE ON FUNCTION "{schema_key}".{signature} FROM {grantee};'
                )
        return tuple(statements)


def build_tenant_migration_registry(
    *definition_sets: UnitDefinitions,
    approved_execution_roles: frozenset[str] = frozenset(),
    allow_test_units: bool = False,
    installed_state_profiles: Mapping[str, InstalledStateProfile] = MappingProxyType({}),
    acl_amendments: tuple[AclAmendment, ...] = (),
) -> TenantMigrationRegistry:
    """Compose approved per-tenant migration definition sets. Startup-only.

    Ordering is by ``migration_id``, which the id grammar makes a total order,
    so composition order across sets cannot change what a tenant receives.
    ``allow_test_units`` is for tests only; a repository-control test asserts
    that no production module passes it (R-E12).

    ``approved_execution_roles`` arrives from the composition root and defaults
    to empty (R-P1.2). M01 embeds no module role name, so a module cannot run a
    migration as a role this deployment did not approve in a reviewable place --
    and the default being empty means forgetting to pass the set denies rather
    than permits. The identifier pattern and the infrastructure-role exclusion
    are enforced on the unit itself, independently of this list (R-P1.3).

    This function performs **no database access** (R-P1B.5). Every control here
    is static, which is what lets the single-composition-path control call it
    with nothing configured.

    ``installed_state_profiles`` (CP2-2b, architecture v3 section 5.1(1)) maps an
    approved migration id to the fixed profile its installed function is verified
    against. It defaults to empty, so no existing route changes. A profile is
    refused with ``INSTALLED_STATE_PROFILE_INVALID`` when its key names no unit,
    the unit is not typed, its execution role or declared identity differs from
    the profile's, or any agreement cell of section 5.3 fails. The check runs over
    each unit's OWN frozen declaration, after the unit adopted it.
    """

    merged: dict[str, UnitDefinition] = {}
    for definitions in definition_sets:
        for migration_id, definition in definitions.items():
            if migration_id in merged:
                raise MigrationUnitRejected(
                    reason_code=PreconditionCode.DUPLICATE_MIGRATION_ID.value
                )
            merged[migration_id] = (
                UnitDefinition(definition) if isinstance(definition, str) else definition
            )

    for definition in merged.values():
        role = definition.execution_role
        if role is not None and role not in approved_execution_roles:
            raise MigrationUnitRejected(
                reason_code=PreconditionCode.EXECUTION_ROLE_NOT_APPROVED.value
            )

    closure_targets, closure_payloads = _closure_declarations(
        merged, installed_state_profiles, acl_amendments
    )
    units = tuple(
        TenantMigrationUnit(
            migration_id,
            merged[migration_id].template,
            merged[migration_id].execution_role,
            _issuer=_UNIT_ISSUER,
            closure_payload=closure_payloads.get(migration_id),
            verification=merged[migration_id].verification,
            # `getattr`, not attribute access: CP1's frozen composition tests
            # supply duck-typed definitions "independently of the new
            # constructor signature" (test_provisioning.py). A definition that
            # predates the typed fields is ordinary, which is what absent means.
            kind=getattr(merged[migration_id], "kind", None),
            policy=getattr(merged[migration_id], "policy", None),
            policy_verification=getattr(merged[migration_id], "policy_verification", None),
        )
        for migration_id in sorted(merged)
    )
    if not allow_test_units:
        for unit in units:
            if unit.is_test_unit:
                raise MigrationUnitRejected(
                    reason_code=PreconditionCode.TEST_MIGRATION_UNIT_REJECTED.value
                )
    return TenantMigrationRegistry(
        units,
        _issuer=_UNIT_ISSUER,
        _requirements=_installed_state_requirements(units, installed_state_profiles),
        _closure_targets=closure_targets,
    )


def _closure_declarations(
    merged: Mapping[str, UnitDefinition],
    profiles: Mapping[str, InstalledStateProfile],
    amendments: Any,
) -> tuple[tuple[_ClosureTarget, ...], dict[str, Mapping[str, Any]]]:
    """W3b (plan v1r1 §4): validate the ACL amendments; build each closure payload.

    Static, no database. Every refusal is INSTALLED_STATE_PROFILE_INVALID: an
    unknown or untyped or unprofiled target; an activator that is not an
    ``acl_closure_v1`` unit, is not later than the target, or does not run as the
    target profile's owner; empty or duplicate removals; a removal outside the
    profile's installed ACL, of the owner's own entry, of a non-EXECUTE privilege,
    with grant option, or naming a grantee outside the identifier pattern; two
    amendments on one target; a closure unit no amendment names.
    """

    refused = MigrationUnitRejected(
        reason_code=PreconditionCode.INSTALLED_STATE_PROFILE_INVALID.value
    )
    if type(amendments) is not tuple or not isinstance(profiles, Mapping):
        raise refused
    profile_map = dict(profiles)
    closure_ids = {m for m, d in merged.items() if getattr(d, "kind", None) == ACL_CLOSURE_KIND}
    targets: list[_ClosureTarget] = []
    seen: set[str] = set()
    for amendment in amendments:
        if type(amendment) is not AclAmendment:
            raise refused
        target_def = merged.get(amendment.target_unit)
        activator_def = merged.get(amendment.activated_by)
        profile = profile_map.get(amendment.target_unit)
        if (
            target_def is None
            or getattr(target_def, "kind", None) != TYPED_FUNCTION_KIND
            or type(profile) is not InstalledStateProfile
            or activator_def is None
            or amendment.activated_by not in closure_ids
            or amendment.target_unit in seen
        ):
            raise refused
        target_version = int(amendment.target_unit[1:4])
        activator_version = int(amendment.activated_by[1:4])
        if activator_version <= target_version:
            raise refused
        if activator_def.execution_role != profile.owner:
            raise refused
        removes = amendment.removes
        if not removes or len(set(removes)) != len(removes):
            raise refused
        for grantee, privilege, grantor, grantable in removes:
            if (
                (grantee, privilege, grantor, grantable) not in profile.installed_acl
                or grantee == profile.owner
                or privilege != "EXECUTE"
                or grantable is not False
                or not EXECUTION_ROLE_PATTERN.fullmatch(grantee)
            ):
                raise refused
        seen.add(amendment.target_unit)
        targets.append(_ClosureTarget(
            target_unit=amendment.target_unit,
            target_version=target_version,
            function_name=profile.function_name,
            argument_types=profile.argument_types,
            activated_by=amendment.activated_by,
            activator_version=activator_version,
            amendment=amendment,
        ))
    payloads: dict[str, Mapping[str, Any]] = {}
    for closure_id in sorted(closure_ids):
        own = sorted((t for t in targets if t.activated_by == closure_id),
                     key=lambda t: t.target_version)
        if not own:
            raise refused
        payloads[closure_id] = _frozen({
            "checksum_version": ACL_CLOSURE_CHECKSUM_VERSION,
            "kind": ACL_CLOSURE_KIND,
            "migration_id": closure_id,
            "version": int(closure_id[1:4]),
            "execution_role": merged[closure_id].execution_role,
            "amendments": [
                {
                    "target_unit": t.target_unit,
                    "target_version": t.target_version,
                    "target_signature": f'{t.function_name}({",".join(t.argument_types)})',
                    "target_profile_digest": profile_digest(profile_map[t.target_unit]),
                    "removes": [list(entry) for entry in sorted(t.amendment.removes)],
                }
                for t in own
            ],
        })
    return tuple(targets), payloads


def _installed_state_requirements(
    units: tuple[TenantMigrationUnit, ...],
    profiles: Mapping[str, InstalledStateProfile],
) -> dict[str, InstalledStateRequirement]:
    """Bind each supplied profile to its unit, or refuse (architecture v3 5.1(1)).

    Keyed by approved migration id, never by function name. The caller's mapping
    is read once, here; the registry keeps only the resulting descriptors.
    """

    refused = MigrationUnitRejected(
        reason_code=PreconditionCode.INSTALLED_STATE_PROFILE_INVALID.value
    )
    if not isinstance(profiles, Mapping):
        raise refused
    by_id = {unit.migration_id: unit for unit in units}
    requirements: dict[str, InstalledStateRequirement] = {}
    for migration_id, profile in dict(profiles).items():
        unit = by_id.get(migration_id)
        if unit is None or not unit.is_typed:
            raise refused
        if type(profile) is not InstalledStateProfile:
            raise refused
        if unit.execution_role != profile.execution_role:
            raise refused
        check_profile_agreement(
            profile,
            execution_role=unit.execution_role,
            policy=unit.policy,
            policy_verification=unit.policy_verification,
        )
        requirements[migration_id] = requirement_for(profile)
    return requirements


# ---------------------------------------------------------------------------
# The production baseline.
#
# R-E11: `t001` means "M01 infrastructure baseline", not "M02-ready". It carries
# the default privileges and the one M01-owned tenant object -- the audit outbox
# that `permissions.json` already names -- and nothing else. An otherwise empty
# application schema at this version is correct.
#
# This unit runs as `haloflow_migrator`, which is what makes the default
# privileges work: they apply to the objects their *creating* role goes on to
# create, and the migrator is that role. The provisioner could only set them on
# the migrator's behalf by being a member of it, which R-E6 forbids -- verified
# on PostgreSQL 17.11, 2026-08-31.
# ---------------------------------------------------------------------------
T001_BASELINE_SQL: Final = f"""
ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA_PLACEHOLDER}
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA_PLACEHOLDER}
    GRANT USAGE, SELECT ON SEQUENCES TO {RUNTIME_ROLE};

CREATE TABLE {_SCHEMA_PLACEHOLDER}.access_audit_outbox (
    source_event_id uuid PRIMARY KEY,
    action_code varchar(64) NOT NULL,
    resource_class varchar(64) NOT NULL,
    purpose_code varchar(64) NOT NULL,
    outcome_code varchar(64) NOT NULL,
    principal_kind varchar(16) NOT NULL,
    principal_id varchar(128) NOT NULL,
    execution_id uuid NOT NULL,
    request_id varchar(128),
    occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    projected_at timestamptz,
    CONSTRAINT access_audit_outbox_principal_kind
        CHECK (principal_kind IN ('actor', 'workload'))
);

CREATE INDEX access_audit_outbox_projection_idx
    ON {_SCHEMA_PLACEHOLDER}.access_audit_outbox (projected_at, occurred_at);

GRANT SELECT ON {_SCHEMA_PLACEHOLDER}.access_audit_outbox TO {AUDIT_PROJECTOR_ROLE};

COMMENT ON TABLE {_SCHEMA_PLACEHOLDER}.access_audit_outbox IS
    'M01 classification: pseudonymous-id; PHI prohibited';
"""


TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {"t001_m01_baseline": T001_BASELINE_SQL}
)
