"""Eligibility E1 test support (RED BY DESIGN until E1 source exists).

Governing: eligibility requirements v1r1; architecture v1, v1r1, v1r2 amendment, v1r3
correction (Codex ALIGN f10c1f2b); stage-3 test cases v1 + v1r1 (Codex ALIGN bca3bf33);
owner decisions OD-EL1..EL10. Synthetic only.

SQL interface pins (E1 red packet v1, section 2):
- Registry: `build_synthetic_registry(include_el1=True)` = the synthetic registry (S-I TEST units
  + B5 t095..t097) + E1 units t098..t101, target **101**. Catalogue:
  `build_synthetic_catalog(include_el1=True)`.
- t098 `m05_el_settings` (singleton: enabled bool DEFAULT false, lead_days 3..5 DEFAULT 3,
  not_received_days 0..5 DEFAULT 1, settings_version, updated_at) and
  `m05_el_priority_payers (payer_id)`; owner-written (support path), no runtime privilege.
- t099 tables `m05_el_appointments` (key row), `m05_el_coverage`, `m05_el_triggers`,
  `m05_el_verifications`, `m05_el_counts`, `m05_el_case_events`; gateway
  **`m05_directory_v4(jsonb)`** = `m05_directory_v3` + an optional `coverage` key on
  `appointment_put` (result unchanged: `{"schema_version": 1, "outcome": "applied"}`).
- t100 gateway **`m05_el_tick(jsonb)`** (timer workload): (a) raises the not-received case
  event for pending coverage whose deadline has passed, deciding ONLY through the seam
  **`m05_el_deadline_passed(p_due timestamptz, p_now timestamptz) RETURNS boolean`**
  (IMMUTABLE, `p_due <= p_now`, owner-internal: no runtime EXECUTE) with one
  `clock_timestamp()` read per tick; (b) re-plans appointments deferred for want of a zone
  once a zone is usable (idempotent). Result `{"schema_version": 1, "outcome": "applied",
  "raised": n, "replanned": m, "deferred_no_zone": k}` (k = appointments still deferred: the
  tenant-scoped configuration diagnostic).
- No usable zone (department tz and clinic tz both unusable; Codex D-1 ruling 5d2877d0): the
  booking and its coverage are recorded (key row `zone` NULL, `deferred_reason` 'no_zone');
  every action needing a local service date is deferred — all three triggers `deferred` with
  `due_at` NULL, no verification, case event or count (the count period is a local month).
  Never a guessed zone, never an inferred coverage class.
- t101 remove-only closure: runtime EXECUTE leaves `m05_directory_v3`.
- Coverage block: `{"state": "pending"}` | `{"state": "none"}` | `{"state": "present",
  "payer_id", "member_id", "relationship", "patient_name", "patient_dob", "subscriber_name",
  "subscriber_dob", "subscriber_member_id"}` (present: all nine keys; values string or null,
  <= 64 chars; dobs `YYYY-MM-DD` or null). Anything else -> COMMAND_MALFORMED.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final

import m02_support
import sa_support as sa
import sh2_support as sh2
import si_support as si
from psycopg import sql

EL1_TARGET: Final = 101
EL1_SUPPORTED: Final = range(EL1_TARGET, EL1_TARGET + 1)
NY: Final = "America/New_York"
PRIORITY: Final = ("SYN-ANTHEM", "SYN-SENTARA", "SYN-UHC")
OTHER_PAYER: Final = "SYN-OTHER"

EL_TABLES: Final = ("m05_el_appointments", "m05_el_coverage", "m05_el_triggers",
                    "m05_el_verifications", "m05_el_counts", "m05_el_case_events")


def el1_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(include_el1=True)


class EL1Install(si.SIInstall):
    """A synthetic tenant at target 101 (S-I TEST + B5 + E1), provisioned in the row body."""

    b5 = True

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, el1_registry(), self.tenant,
                                           supported=EL1_SUPPORTED, manifest=staff_manifest())
                sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                sa.admin(self, si.si_test_profile_sql().format(schema=self.schema))
                sh2.settings_raw(self, clinic_display_name=sh2.CLINIC, clinic_tz=NY)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"E1 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[EL1Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield EL1Install(ids, tenant)
    finally:
        reset(tenant)


def ready(install: EL1Install, *, enabled: bool = True, lead_days: int = 3,
          not_received_days: int = 1, priority: tuple[str, ...] = PRIORITY) -> EL1Install:
    install.require()
    si.sc.change(install, "grant")
    el_settings(install, enabled=enabled, lead_days=lead_days,
                not_received_days=not_received_days, priority=priority)
    return install


def el_settings(install: EL1Install, *, enabled: bool, lead_days: int,
                not_received_days: int, priority: tuple[str, ...]) -> None:
    """FIXTURE: the support-path settings write (owner role), as reminder settings_raw."""
    s = sql.Identifier(install.schema)
    sa.admin(install, sql.SQL(
        "UPDATE {}.m05_el_settings SET enabled = %s, lead_days = %s, not_received_days = %s,"
        " settings_version = settings_version + 1, updated_at = clock_timestamp()").format(s),
        (enabled, lead_days, not_received_days))
    sa.admin(install, sql.SQL("DELETE FROM {}.m05_el_priority_payers").format(s))
    for payer in priority:
        sa.admin(install, sql.SQL(
            "INSERT INTO {}.m05_el_priority_payers (payer_id) VALUES (%s)").format(s), (payer,))


def present(payer: str = PRIORITY[0], *, relationship: str = "self", **over: Any
            ) -> dict[str, Any]:
    cov: dict[str, Any] = {
        "state": "present", "payer_id": payer, "member_id": "M000001",
        "relationship": relationship, "patient_name": "Synthetic Patient",
        "patient_dob": "1970-01-01", "subscriber_name": None, "subscriber_dob": None,
        "subscriber_member_id": None}
    if relationship == "dependent":
        cov.update(subscriber_name="Synthetic Subscriber", subscriber_dob="1968-02-02",
                   subscriber_member_id="M000002")
    cov.update(over)
    return cov


def put(install: EL1Install, ref: str, patient: str, start: datetime | str, *,
        coverage: dict[str, Any] | None = None, status: str = "booked",
        department_tz: str | None = NY) -> dict[str, Any]:
    """appointment_put through m05_directory_v4; `coverage=None` omits the key (legacy shape)."""
    fields = si.appointment3(ref, patient, start, status=status, department_tz=department_tz)
    if coverage is not None:
        fields["coverage"] = coverage
    return sa.call(install, "m05_directory_v4",
                   {"schema_version": 1, "op": "appointment_put", **fields,
                    "attribution": sa.attribution(sa.W_DIRECTORY)})


def tick(install: EL1Install) -> dict[str, Any]:
    return sa.call(install, "m05_el_tick",
                   {"schema_version": 1, "attribution": sa.attribution(sa.W_TIMER)})


def rows(install: EL1Install, table: str, where: str = "true", params: Any = None,
         order: str = "1") -> list[tuple[Any, ...]]:
    assert table in EL_TABLES or table.startswith("m05_"), table
    return sa.admin(install, sql.SQL("SELECT * FROM {}.{} WHERE " + where + " ORDER BY " + order)
                    .format(sql.Identifier(install.schema), sql.Identifier(table)), params)


def count(install: EL1Install, table: str, where: str = "true", params: Any = None) -> int:
    got = sa.admin(install, sql.SQL("SELECT count(*) FROM {}.{} WHERE " + where).format(
        sql.Identifier(install.schema), sql.Identifier(table)), params)
    return int(got[0][0])


def el_snapshot(install: EL1Install) -> dict[str, int]:
    return {t: count(install, t) for t in EL_TABLES}


# --- the authorized routing boundary (Codex 878899b6) ------------------------------------
# The real resolver + gateway at target 101 with the E1 catalogue, in which the directory key
# `m05.intake.directory` is REBOUND to `m05_directory_v4` (pin; the t053 same-key pattern).


def el1_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(include_el1=True)


@asynccontextmanager
async def runtime(install: EL1Install, *, max_size: int = 4) -> AsyncIterator[Any]:
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
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=el1_registry(), catalog=el1_catalog(),
            supported_schema_versions=frozenset({EL1_TARGET}),
            activation_policy=ActivationPolicy.ENFORCE,
        )
    finally:
        await pool.close()


async def routed_put(rt: Any, install: EL1Install, fields: dict[str, Any], *,
                     authorized_for: EL1Install | None = None) -> dict[str, Any]:
    """appointment_put through the resolver and gateway (directory key, workload principal).
    `authorized_for` (default: `install`) is the tenant the principal is authorized for; the
    tenant hint is always `install`'s tenant."""
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from psycopg.types.json import Jsonb

    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.intake import _attribution
    from haloflow.m05.intake_statements import DIRECTORY_KEY

    owner = authorized_for or install
    caps = sa.CAPS["directory"]
    ctx = await rt.resolver.resolve(
        principal=Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_DIRECTORY, auth_method="test",
                            authorized_tenant_ids=frozenset({owner.tenant[0]}),
                            capabilities=caps),
        tenant_hint=install.tenant[0], purpose="operations", capabilities=caps,
        source=TrustedSource.HTTP,
        execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:el1:{uuid4()}"),
        correlation_id=uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)
    command = {"schema_version": 1, "op": "appointment_put", **fields,
               "attribution": _attribution(ctx)}

    async def body(binding: Any) -> dict[str, Any]:
        got = await binding.handle.fetch_all(DIRECTORY_KEY, {"command": Jsonb(command)})
        assert len(got) == 1, got
        return dict(got[0][0])

    result: dict[str, Any] = await EventFoundation(
        rt.gateway, producer_service=sa.PRODUCER).run_in_transaction(ctx, body)
    return result
