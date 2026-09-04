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

import re
from collections.abc import Iterator, Mapping
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Final

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.checksum import unit_checksum
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.roles import AUDIT_PROJECTOR_ROLE, RUNTIME_ROLE
from haloflow.m01.resolver import SCHEMA_KEY_PATTERN

MIGRATION_ID_PATTERN: Final = re.compile(r"^t\d{3}(_test)?_[a-z0-9_]{1,64}$")
_TEST_UNIT_PATTERN: Final = re.compile(r"^t\d{3}_test_")
_SCHEMA_PLACEHOLDER: Final = "{schema}"
_UNIT_ISSUER: Final = object()

UnitDefinitions = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class TenantMigrationUnit:
    """One ordered, checksummed per-tenant migration. Constructed only here."""

    migration_id: str
    template: str = field(repr=False)
    _issuer: InitVar[object | None] = None

    def __post_init__(self, _issuer: object | None) -> None:
        if _issuer is not _UNIT_ISSUER:
            raise MigrationUnitRejected(reason_code=PreconditionCode.UNTRUSTED_MIGRATION_UNIT.value)
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

    @property
    def is_test_unit(self) -> bool:
        return bool(_TEST_UNIT_PATTERN.match(self.migration_id))

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

        return unit_checksum(migration_id=self.migration_id, template=self.template)

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
    allow_test_units: bool = False,
) -> TenantMigrationRegistry:
    """Compose approved per-tenant migration definition sets. Startup-only.

    Ordering is by ``migration_id``, which the id grammar makes a total order,
    so composition order across sets cannot change what a tenant receives.
    ``allow_test_units`` is for tests only; a repository-control test asserts
    that no production module passes it (R-E12).
    """

    merged: dict[str, str] = {}
    for definitions in definition_sets:
        for migration_id, template in definitions.items():
            if migration_id in merged:
                raise MigrationUnitRejected(
                    reason_code=PreconditionCode.DUPLICATE_MIGRATION_ID.value
                )
            merged[migration_id] = template

    units = tuple(
        TenantMigrationUnit(migration_id, merged[migration_id], _issuer=_UNIT_ISSUER)
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
# on PostgreSQL 17.10, 2026-08-31.
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
