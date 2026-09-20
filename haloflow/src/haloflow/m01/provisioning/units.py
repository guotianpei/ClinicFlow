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
        if not self.template.strip():
            raise MigrationUnitRejected(reason_code=PreconditionCode.MIGRATION_TEMPLATE_EMPTY.value)
        if _SCHEMA_PLACEHOLDER not in self.template:
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
        return self.template.replace(_SCHEMA_PLACEHOLDER, schema_key)


class TenantMigrationRegistry:
    """An immutable, ordered registry of units. Construction is restricted."""

    __slots__ = ("__units",)

    def __init__(
        self,
        units: tuple[TenantMigrationUnit, ...],
        *,
        _issuer: object | None = None,
    ) -> None:
        if _issuer is not _UNIT_ISSUER:
            raise MigrationUnitRejected(
                reason_code=PreconditionCode.UNTRUSTED_MIGRATION_REGISTRY.value
            )
        self.__units = units

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


def build_tenant_migration_registry(
    *definition_sets: UnitDefinitions,
    approved_execution_roles: frozenset[str] = frozenset(),
    allow_test_units: bool = False,
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

    units = tuple(
        TenantMigrationUnit(
            migration_id,
            merged[migration_id].template,
            merged[migration_id].execution_role,
            _issuer=_UNIT_ISSUER,
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
    return TenantMigrationRegistry(units, _issuer=_UNIT_ISSUER)


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
