"""Incoming fax routing FX-IN, package FX5 — fax access for staff who can act only (OD-FX11).
TEST SUPPORT (TEST ONLY). RED BY DESIGN.

Governing (Shared Workspace `Work Session 2026-11-26/`): package v1
(`claude_fx5-actor-only-package-v1-2026-11-26.md`), Codex ALIGN for red tests 273a6baf
(`codex_fx5-v1-package-review.md`), OD-FX11 (Rachel 2026-11-26: only staff who can act see the
fax screen; view-only staff have no fax access), OD-FX12 (wording, FX3b).
Synthetic only.

Red classes: C control; A absence-red (`include_fx5` -> TypeError; new names -> AttributeError /
ModuleNotFoundError; the new SQL functions do not exist; `fax_read` is not a route requirement).

FX5 interface pinned here
==========================================================================================
Registry: `build_synthetic_registry(<EL flags>, include_fx1=True, include_fx2=True,
include_fx4=True, include_fx5=True)` — FX4's ids plus FX5_IDS, target **153**; include_fx5
without include_fx4 -> ValueError("...FX5..."). Units in `haloflow.m05.fax_access_units`
(FX5_TENANT_MIGRATIONS, FX5_INSTALLED_STATE_PROFILES).
Catalogue: `build_synthetic_catalog(<EL flags>, include_fx1..include_fx4=True, include_fx5=True)`
REBINDS (fax_statements.FX5_REBOUND), each with capability m05.followup.apply:
- READ_KEY   READ  "SELECT m05_fx_staff_read_v3(%(command)s)"
- OPEN_KEY   WRITE "SELECT m05_fx_staff_action_v2(%(command)s OPERATOR(pg_catalog.||)
                    pg_catalog.jsonb_build_object('action', 'open'))"   (constant open kept)
- ACT_KEY    WRITE "SELECT m05_fx_staff_action_v2(%(command)s)"
- QUEUES_KEY READ  "SELECT m05_fx_staff_queues_v2(%(command)s)"
RECEIVE_KEY / LINES_KEY unchanged. include_fx5 without include_fx4 -> ValueError("...FX5...").
- t150 `m05_fx_staff_read_v3(jsonb)` (runtime, STABLE): t149 with the staff check 'act'.
- t151 `m05_fx_staff_queues_v2(jsonb)` (runtime, STABLE): t145 with the staff check 'act'.
- t152 `m05_fx_staff_action_v2(jsonb)` (runtime): t142 with the staff check 'act' for EVERY
  action, open included; the check stays first (a refused open writes nothing).
- t153 (migrator SQL): REVOKE EXECUTE on t142 m05_fx_staff_action(jsonb), t143
  m05_fx_staff_read(jsonb), t145 m05_fx_staff_queues(jsonb), t149 m05_fx_staff_read_v2(jsonb)
  from the runtime role (no superseded staff fax function stays callable).
A failed staff check keeps the internal code ACTOR_INACTIVE (t141 is one boolean; Codex 273a6baf
item 1); the HTTP refusal is a uniform 403 FORBIDDEN.
Platform (`haloflow.platform.capabilities`): ROUTE_REQUIREMENTS["fax_read"] ==
({m05.followup.read, m05.followup.prepare, m05.followup.apply},); ROUTE_OPTIONAL["fax_read"] ==
frozenset() (explicit entry). list / get / history / act unchanged.
Routes (`haloflow.synthetic.fax_staff_routes.ROUTES`): list, get, history, open, queues use
"fax_read"; act stays "act".
Profiles: TARGETS["fax"] == 153; installed_profile_fax re-measured.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any, Final

import fx1_support as fx
import fx2_support as f2
import fx4_support as f4
import m02_support
import sa_support as sa

FX5_TARGET: Final = 153
FX5_SUPPORTED: Final = range(FX5_TARGET, FX5_TARGET + 1)
FX5_IDS: Final = ("t150_m05_fx_staff_read_v3", "t151_m05_fx_staff_queues_v2",
                  "t152_m05_fx_staff_action_v2", "t153_m05_fx_revoke_superseded")
READ_V3: Final = "m05_fx_staff_read_v3"
QUEUES_V2: Final = "m05_fx_staff_queues_v2"
ACTION_V2: Final = "m05_fx_staff_action_v2"
# t153: the superseded staff fax functions (name, the single jsonb signature)
SUPERSEDED: Final = (f2.ACTION, f2.READ, f4.QUEUES, f4.READ_V2)
APPLY: Final = "m05.followup.apply"
FAX_READ: Final = "fax_read"
OPEN_SQL: Final = ("SELECT m05_fx_staff_action_v2(%(command)s OPERATOR(pg_catalog.||)"
                   " pg_catalog.jsonb_build_object('action', 'open'))")


def fx5_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                    include_fx2=True, include_fx4=True, include_fx5=True)


def fx5_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                   include_fx2=True, include_fx3=True, include_fx4=True,
                                   include_fx5=True)


class FX5Install(fx.FX1Install):
    """A synthetic tenant at target 153 (FX4 149 + FX5), provisioned in the row body."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, fx5_registry(), self.tenant,
                                           supported=FX5_SUPPORTED, manifest=staff_manifest())
                sa.admin(self, fx.FX_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"FX5 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[FX5Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield FX5Install(ids, tenant)
    finally:
        reset(tenant)


def upgrade_to_fx5(install: sa.SAInstall) -> None:
    """Apply the FX5 registry to a tenant provisioned at an earlier target (P06)."""
    from staff_support import staff_manifest

    from haloflow.m01.provisioning import TenantMigrationRunner

    runner = TenantMigrationRunner(m02_support._connection_factory(  # noqa: SLF001
        install.ids.logins[sa.MIGRATOR]), fx5_registry(), manifest=staff_manifest())
    asyncio.run(runner.apply(tenant_id=install.tenant_id, schema_key=install.schema))


def read(install: sa.SAInstall, view: str, staff_id: str, **fields: Any) -> dict[str, Any]:
    return sa.call(install, READ_V3, f2.read_cmd(view, staff_id, **fields))


def queues(install: sa.SAInstall, staff_id: str) -> dict[str, Any]:
    return sa.call(install, QUEUES_V2, f4.queues_cmd(staff_id))


def act(install: sa.SAInstall, action: str, fax_id: str, staff_id: str, **fields: Any
        ) -> dict[str, Any]:
    return sa.call(install, ACTION_V2, f2.action_cmd(action, fax_id, staff_id, **fields))


def history_actions(install: sa.SAInstall, fax_id: str) -> list[str]:
    return [str(r["action"]) for r in sorted(
        fx.rows(install, "m05_fx_history", "fax_id = %s::uuid", (fax_id,)),
        key=lambda r: int(r["seq"]))]


def runtime_can_execute(install: sa.SAInstall, function: str) -> bool:
    """Effective EXECUTE for the runtime role on `<schema>.<function>(jsonb)`."""
    got = sa.admin(install, sa.q(
        install, "SELECT has_function_privilege(%s, '{s}." + function + "(jsonb)', 'EXECUTE')"),
        (sa.RUNTIME,))
    return bool(got[0][0])


def runtime(install: sa.SAInstall, *, max_size: int = 6) -> Any:
    """A composed TEST runtime over the FX5 registry (153) and the FX5 catalogue."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cm() -> Any:
        import service_support as sv

        from haloflow.m01.activation import ActivationPolicy
        from haloflow.m01.control_store import PsycopgControlStore
        from haloflow.m01.pool import TenantPool
        from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

        pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=max_size)
        await pool.open()
        try:
            yield compose_tenant_runtime(
                TenantRuntimeDependencies(
                    pool=pool, control_store=PsycopgControlStore(pool),
                    migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),  # noqa: SLF001
                    provisioner_connect=sv._factory(  # noqa: SLF001
                        install.ids.logins["haloflow_provisioner"])),
                registry=fx5_registry(), catalog=fx5_catalog(),
                supported_schema_versions=frozenset({FX5_TARGET}),
                activation_policy=ActivationPolicy["ENFORCE"])
        finally:
            await pool.close()

    return cm()
