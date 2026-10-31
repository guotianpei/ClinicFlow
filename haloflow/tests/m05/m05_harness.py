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
    """Production unit sets + the M05 set, composed through the one M01 builder,
    with the M05 owner approved and the M05 installed-state profiles added. No
    `allow_test_units`: the M05 units are module units, not test units."""

    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry
    from haloflow.m05.gateways import M05_GATEWAY_PROFILES
    from haloflow.m05.units import M05_TENANT_MIGRATIONS

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS,
        M05_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES | {m05_support.M05_OWNER},
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **M05_GATEWAY_PROFILES},
    )


def production_manifest_document() -> dict[str, Any]:
    from haloflow.m01.provisioning.manifest import MANIFEST_PACKAGE, PROVISIONING_MANIFEST

    text = resources.files(MANIFEST_PACKAGE).joinpath(PROVISIONING_MANIFEST).read_text()
    document: dict[str, Any] = json.loads(text)
    return document


def m05_manifest_document() -> dict[str, Any]:
    """The production document with exactly one change: the M05 owner's tenant
    schema privileges move from USAGE (part A amendment A4) to USAGE+CREATE."""

    document = production_manifest_document()
    document["execution_role_profiles"][m05_support.M05_OWNER]["tenant_schema_privileges"] = [
        "USAGE",
        "CREATE",
    ]
    return document


def m05_manifest() -> Any:
    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    return load_provisioning_manifest(m05_manifest_document())


class M05Install:
    """One tenant, installed through the M05 test registry on first `require()`.

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
                self._result = m02_support.provision_sync(
                    self.ids,
                    m05_registry(),
                    self.tenant,
                    supported=m05_support.M05_TEST_SUPPORTED,
                    manifest=m05_manifest(),
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
