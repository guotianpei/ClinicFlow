"""L-1S package S-C (D-S1 activation switch, wired) red-test support.

Governing: S-C requirements v1 (7f338e61…), architecture v1 (58d4064b…), Codex reviews
20261106T201638Z / 202256Z, and the S-C red package note (Work Session 2026-11-06).
S-C builds on S-B: the S-C TEST registry is the S-B TEST registry plus t046 (target 46).
Every import of S-C code is deferred into the row body (class-B red rule).

Interface pins asserted here (red package delta, disclosed in the package note):
- `haloflow.m01.activation.ActivationPolicy` enum {ENFORCE, NOT_ENFORCED}.
- `haloflow.m01.errors.ActivationRefused(M01Error)`; reason codes M05_NOT_ACTIVATED,
  M05_ACTIVATION_UNREADABLE, ACTIVATION_ISOLATION_UNSUPPORTED.
- `build_statement_catalog(*sets, activation_protected=frozenset())`;
  `CompiledCatalog.activation_protected: frozenset[str]`; unknown protected key ->
  RepositoryStatementRejected("PROTECTED_KEY_UNKNOWN").
- `TenantRepositoryHandle(connection, catalog, *, allow_writes, capabilities,
  activation_policy=NOT_ENFORCED, activation_protected=frozenset())`; probe SQL
  exactly PROBE_SQL; `handle.activation_refused` -> reason code or None.
- `TenantTransactionGateway(..., activation_policy=NOT_ENFORCED)`;
  `compose_tenant_runtime(..., activation_policy=NOT_ENFORCED)`;
  `composition.build_production_tenant_runtime(dependencies, *, activation_policy)`
  (REQUIRED keyword).
- Module declarations: `haloflow.m05.statements.M05_PROTECTED`,
  `haloflow.m05.intake_statements.SA_PROTECTED`,
  `haloflow.m05.due_statements.SB_PROTECTED`.
- t046 `m05_activation_probe()` -> jsonb {"schema_version": 1, "active": bool}.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Final

import m02_support
import sa_support as sa
import sb_support as sb

SC_UNIT_ID: Final = "t046_m05_activation_probe"
SC_TARGET: Final = 46
SC_SUPPORTED: Final = range(SC_TARGET, SC_TARGET + 1)
PROBE_FUNCTION: Final = "m05_activation_probe"
PROBE_SQL: Final = ("SELECT m05_activation_probe(),"
                    " pg_catalog.current_setting('transaction_isolation')")
CODES: Final = ("M05_NOT_ACTIVATED", "M05_ACTIVATION_UNREADABLE",
                "ACTIVATION_ISOLATION_UNSUPPORTED")
# The latest-record rule, shared by t043 and t046 (Codex 202256Z pin 4).
LATEST_ORDER: Final = "ORDER BY r.created_at DESC, r.record_id DESC LIMIT 1"


def sc_units() -> Any:
    from haloflow.m05.activation_units import SC_TENANT_MIGRATIONS

    return SC_TENANT_MIGRATIONS


def sc_profiles() -> Any:
    from haloflow.m05.activation_units import SC_INSTALLED_STATE_PROFILES

    return SC_INSTALLED_STATE_PROFILES


def sc_registry() -> Any:
    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, sa.sa_units(), sb.sb_units(), sc_units(),
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **sa.sa_profiles(),
                                  **sb.sb_profiles(), **sc_profiles()},
        acl_amendments=APPROVED_ACL_AMENDMENTS,
    )


def protected() -> frozenset[str]:
    from haloflow.m05.due_statements import SB_PROTECTED
    from haloflow.m05.intake_statements import SA_PROTECTED
    from haloflow.m05.statements import M05_PROTECTED

    return frozenset(M05_PROTECTED | SA_PROTECTED | SB_PROTECTED)


def sc_catalog() -> Any:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import build_statement_catalog

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, sa.sa_statements(),
                                   sb.sb_statements(), activation_protected=protected())


class SCInstall(sa.SAInstall):
    """The S-A install shape, provisioned through the S-C registry (target 46)."""

    def __init__(self, ids: m02_support.Identities, tenant: tuple[str, str], *,
                 profile: bool = True, registry: str = "sc") -> None:
        super().__init__(ids, tenant, profile=profile)
        self.registry_kind = registry

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                if self.registry_kind == "sc":
                    registry, supported = sc_registry(), SC_SUPPORTED
                else:  # an S-B tenant WITHOUT t046 (the "relation missing" shape)
                    registry, supported = sb.sb_registry(), sb.SB_SUPPORTED
                m02_support.provision_sync(self.ids, registry, self.tenant,
                                           supported=supported, manifest=staff_manifest())
                if self.profile:
                    sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"S-C test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str, *,
                   registry: str = "sc") -> Iterator[SCInstall]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield SCInstall(ids, tenant, registry=registry)
    finally:
        reset(tenant)


def change(install: SCInstall, kind: str) -> None:
    """FIXTURE: append a grant/revoke through t042, as the M05 owner (the only path)."""

    import psycopg
    from psycopg.types.json import Jsonb

    command = {"schema_version": 1, "change": kind, "actor_id": sa.new_id(),
               "ticket_reference": "SC-TEST", "reason": "synthetic S-C row"}
    with psycopg.connect(install.ids.admin, autocommit=True) as conn, conn.transaction():
        conn.execute(f"SET LOCAL ROLE {sa.M05_OWNER}")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant_id,))
        conn.execute(sa.q(install, "SELECT {s}.m05_activation_change(%s)"), (Jsonb(command),))
