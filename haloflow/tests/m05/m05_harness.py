"""UC1 part B (M05) test harness helpers: the M05 test registry, the M05 test
provisioning manifest and the deferred installer. Imported by conftest.py and by
the static rows; holds no fixtures."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import m02_support
import m05_support


def m05_registry() -> Any:
    """The isolated M05 test registry (target 20): the part A unit sets (M01 + M02)
    + the M05 set, composed through the one M01 builder, with the M02 owners and the
    M05 owner approved and the part A + M05 installed-state profiles. No
    `allow_test_units`: the M05 units are module units, not test units.

    B5 harness migration H2 (Codex 319b5f3 ruling: retain an explicit target-20
    isolated M05 fixture). Since B5 the production declarations already contain
    M05 and staff, so this registry names its unit sets, roles and profiles
    explicitly instead of extending `APPROVED_TENANT_MIGRATIONS` (which would
    compose M05 twice). Its units, target and meaning are unchanged."""

    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry
    from haloflow.m02.gateway_profile import LOCK_OPERATION_PROFILE
    from haloflow.m02.part_a_gateways import PART_A_GATEWAY_PROFILES
    from haloflow.m02.roles import M02_EXECUTION_ROLES
    from haloflow.m02.units import M02_TENANT_MIGRATIONS, T003_MIGRATION_ID
    from haloflow.m05.gateways import M05_GATEWAY_PROFILES
    from haloflow.m05.units import M05_TENANT_MIGRATIONS

    return build_tenant_migration_registry(
        TENANT_MIGRATIONS,
        M02_TENANT_MIGRATIONS,
        M05_TENANT_MIGRATIONS,
        approved_execution_roles=M02_EXECUTION_ROLES | {m05_support.M05_OWNER},
        installed_state_profiles={
            T003_MIGRATION_ID: LOCK_OPERATION_PROFILE,
            **PART_A_GATEWAY_PROFILES,
            **M05_GATEWAY_PROFILES,
        },
    )


def production_manifest_document() -> dict[str, Any]:
    from haloflow.m01.provisioning.manifest import MANIFEST_PACKAGE, PROVISIONING_MANIFEST

    text = resources.files(MANIFEST_PACKAGE).joinpath(PROVISIONING_MANIFEST).read_text()
    document: dict[str, Any] = json.loads(text)
    return document


def m05_manifest_document() -> dict[str, Any]:
    """The M05 test manifest document. B1..B4 used the production document with the
    M05 owner moved from USAGE to USAGE+CREATE. B5 makes exactly that change (and
    the same one for the staff owner) in production, so this is now the production
    document itself (measured equal; MB1-S07 retired with B5)."""

    return production_manifest_document()


def m05_manifest() -> Any:
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    return load_provisioning_manifest(m05_manifest_document())


class M05Install:
    """One tenant, installed on first `require()`.

    MA-10 tranche 2 (deliberate harness migration, package v1 section 5 step 3):
    prepare/apply now call `m01_staff_assignable`, so the behaviour rows install
    through the staff test registry (target 25 since B3b t025; since B5 harness
    migration H1 this is the production registry and manifest).
    `m05_registry()` / `m05_manifest()` above keep the isolated target-20 M05
    fixture: the static and refusal-path rows still pin the M05 unit set by itself.

    The outcome (result or exception) is memoised per module, so a failing install
    is attempted once and reported in every row that needs it."""

    def __init__(self, ids: m02_support.Identities, tenant: tuple[str, str]) -> None:
        self.ids = ids
        self.tenant = tenant
        self._attempted = False
        self._result: Any = None
        self._error: Exception | None = None

    @property
    def schema(self) -> str:
        return self.tenant[1]

    def require(self) -> Any:
        if not self._attempted:
            self._attempted = True
            try:
                import staff_support  # deferred: staff_support imports this module

                self._result = m02_support.provision_sync(
                    self.ids,
                    staff_support.staff_registry(),
                    self.tenant,
                    supported=staff_support.STAFF_TEST_SUPPORTED,
                    manifest=staff_support.staff_manifest(),
                )
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"M05 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error
        return self._result
