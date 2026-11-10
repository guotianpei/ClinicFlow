"""L-1S package S-I (simulated EHR confirmation write-back): red-test support for the
PostgreSQL and harness rows.

Governing: S-I investigation v1 + v1r1 + v1r2, red-test packet v1 (Work Session
2026-11-10), Codex Option A + red-test ALIGN with pins (`codex_si-red-packet-v1-review.md`,
relay 20261110T134528Z), owner decision C5 (2026-11-10). Extends `sh3_support` (target 75).
Synthetic data only; SimulatedEhr only; FakeSender only; TEST registry only; nothing
composed; the production registry and catalogue are unchanged. Every import of S-I code is
deferred into a function body, so the expected red cause of every S-I row is
ModuleNotFoundError for `haloflow.m05.writeback*` / `haloflow.m05.simulated_ehr`, never a
collection error.

SQL interface pins (the S-I database contract these rows freeze). Every function is
SECURITY DEFINER, owned by and executing as `haloflow_m05_owner`, `search_path` pinned,
EXECUTE to `haloflow_runtime` only, with the t056 tenant / attribution / workload fences;
every command is a closed JSON object with `schema_version` 1 and the closed `attribution`;
anything else is COMMAND_MALFORMED.

Units (`haloflow.m05.writeback_units`: SI_TENANT_MIGRATIONS, SI_INSTALLED_STATE_PROFILES,
SI_ACL_AMENDMENTS), TEST target 92, all additive, t001-t075 byte-unchanged:
- t076 `m05_profile_identity_columns`: ADD `provider_ref`, `department_ref` text NULL,
  each CHECK char_length <= 64, on m05_appointment_profile.
- t077 `m05_directory_v3`: function `m05_directory_v3(p_command)`; the t063 body with key
  set v2 + {provider_ref, department_ref} EXACTLY (each string or null, <= 64), both
  written in the SAME version-bumping upsert.
- t078 `m05_directory_v2_closure`: ACL closure; removes runtime EXECUTE on
  `m05_directory_v2` (t063). The statement key `m05.intake.directory` is rebound to v3.
- t079 `m05_writeback_tables`:
  m05_writeback_baselines(op_id uuid PK -> m05_outbound_ops, attempt_id, appointment_ref,
  start_at, confirmation, provider_ref, department_ref, source_version, captured_at);
  owner INSERT + SELECT only (no UPDATE / DELETE).
  m05_writeback_ops(wb_op_id uuid PK, inbound_id uuid UNIQUE, op_id uuid UNIQUE,
  appointment_ref, patient_id, state in ('pending', 'eligible_check', 'permitted', 'spent',
  'observed_positive', 'item', 'skipped'), fence integer, lease_until, r1_failures,
  next_at, reason (one of REASONS or NULL), event_id uuid UNIQUE NULL, item_id uuid NULL,
  created_at, updated_at).
  m05_writeback_permits(wb_op_id uuid PK, permit_id uuid UNIQUE, issued_fence,
  r1_digest bytea (32 bytes), apply_deadline NULL, spent_at NULL, spent_fence NULL,
  voided_at NULL, closed_at NULL).
- t080 `m05_writeback_capture_baseline` {op_id, attempt_id} -> {schema_version,
  outcome: "captured" | "refused", code}; the t071 lock order (chain, then ticket row FOR
  UPDATE); refuses unless op claimed, ticket.attempt_id = attempt, consumed_at NULL, profile
  source_version = render_inputs.source_version; a second capture is refused.
- t081 `m05_writeback_discover` {limit 1..1000} -> {schema_version, discovered}: one op per
  inbound reply with reply_class 'confirm' and decision 'recorded' matched to a reminder of
  an outbound op (ON CONFLICT DO NOTHING); never for any other reply class.
- t082 `m05_writeback_due` {limit} -> {schema_version, ops: [{wb_op_id, phase}]}, phase
  "check" (pending / eligible_check with next_at <= now and lease free) or "reconcile"
  (spent, unsettled, clock_timestamp() > apply_deadline + RECONCILE_MARGIN_S); read only.
- t083 `m05_writeback_claim` {wb_op_id, lease_seconds} -> {schema_version, outcome:
  "claimed" | "not_due" | "not_claimable", fence, phase: "check", appointment_ref}; the
  check phase only; fence := fence + 1; an unspent permit from an older fence is VOIDED in
  the same transaction.
- t084 `m05_writeback_claim_reconcile` {wb_op_id, lease_seconds} -> {..., phase:
  "reconcile", permit_id}; only a spent, unsettled permit past apply_deadline + margin in
  clock_timestamp(); sets the permit's closed_at.
- t085 `m05_writeback_decide` {wb_op_id, fence, read, read_failed} (read is null or
  {status, appointment_id, start_at, provider_ref, department_ref, replaced_by}) ->
  {schema_version, outcome: "permitted" | "item" | "skipped" | "deferred" | "stale",
  permit_id, reason, event_id, patient_id}; guarded by fence, state 'eligible_check' and
  lease_until > clock_timestamp(); the WB2 matrix against the baseline; R1 failures use
  RETRY_DELAYS_S, the fourth is item "ehr_unreachable". An "item" result MINTS AND STORES
  the op's event_id once; a repeated decide returns the stored one.
- t086 `m05_writeback_spend` {wb_op_id, fence, permit_id} -> {schema_version, outcome:
  "spent" | "refused", code, apply_deadline}; one conditional UPDATE: spent_at NULL,
  voided_at NULL, issued_fence = fence, op.fence = fence, op.state 'permitted',
  op.lease_until > clock_timestamp(); apply_deadline := clock_timestamp() + WRITE_TIMEOUT_S.
- t087 `m05_writeback_settle` {wb_op_id, fence, permit_id, write_outcome, read} ->
  {schema_version, outcome: "observed_positive" | "item" | "stale", reason, event_id,
  patient_id}; fenced; NOT activation-protected.
- t088 `m05_writeback_check_contract`: event type `m05_writeback_check_raised` v1
  (family m05_intake, metadata {wb_op_id, appointment_ref, reason}), its envelope
  allowlist rows, the item-kind CHECK on m05_intake_map and m05_followup_items REPLACED
  by the five-kind set, and resolution contract `m05_followup_resolution_asserted` v2
  (item_kind enum of five) with its allowlist rows; v1 rows unchanged.
- t089 `m05_create_item_v2`: the t014 shape with the fifth kind.
- t090 / t091 `m05_prepare_action_v3` / `m05_apply_action_v3`: the v2 bodies with the
  resolution event accepted at contract v2 for every kind (v1 events still accepted for
  the four old kinds).
- t092 `m05_followup_reason` {item_id} -> {schema_version, item_id, reason}: staff read,
  only for kind ehr_confirmation_check, the reason read from the source event's allowlisted
  `reason` path; unknown, other-tenant or other-kind -> ITEM_NOT_FOUND; nothing else of the
  event is returned.
- t093 `m05_si_runtime_closure`: ACL closure; removes runtime EXECUTE on m05_create_item
  (t014) and the v2 actions (t030, t031).

Statements (`haloflow.m05.writeback_statements`): SI_STATEMENTS, SI_REBOUND (the existing
keys `m05.intake.directory`, `m05.followup.create`, `m05.followup.prepare`,
`m05.followup.apply` bound to v3 / v2 / v3 / v3), SI_PROTECTED (discover, claim, decide,
spend), SI_CLOSED_KEYS. New keys: CAPTURE_KEY "m05.writeback.capture" (capability
`m05.intake.dispatch`); DISCOVER / DUE / CLAIM / CLAIM_RECONCILE / DECIDE / SPEND / SETTLE
"m05.writeback.*" (capability "m05.writeback"); REASON_KEY "m05.followup.reason"
(capability `m05.followup.read`). `haloflow.m05.writeback.WritebackService(foundation)`
runs decide / settle in ONE gateway transaction with the M02 append and the item create
(the IntakeService pattern); its CAPABILITIES = {"m05.writeback",
"m02.observation.append", "m05.followup.create"}; item producer workload W_DISPATCH.
Constants: WRITE_TIMEOUT_S = 10, RECONCILE_MARGIN_S = 30, LEASE_S = 60.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final

import m02_support
import sa_support as sa
import sc_support as sc
import sh2_support as sh2
import sh3_support as sh3
import sh_support as sh

SI_UNIT_IDS: Final = (
    "t076_m05_profile_identity_columns", "t077_m05_directory_v3",
    "t078_m05_directory_v2_closure", "t079_m05_writeback_tables",
    "t080_m05_writeback_capture_baseline", "t081_m05_writeback_discover",
    "t082_m05_writeback_due", "t083_m05_writeback_claim",
    "t084_m05_writeback_claim_reconcile", "t085_m05_writeback_decide",
    "t086_m05_writeback_spend", "t087_m05_writeback_settle",
    "t088_m05_writeback_check_contract", "t089_m05_create_item_v2",
    "t090_m05_prepare_action_v3", "t091_m05_apply_action_v3",
    "t092_m05_followup_reason", "t093_m05_si_runtime_closure",
)
SI_TARGET: Final = 93
SI_SUPPORTED: Final = range(SI_TARGET, SI_TARGET + 1)
CLOSURE_UNITS: Final = ("t078_m05_directory_v2_closure", "t093_m05_si_runtime_closure")
TABLES: Final = ("m05_writeback_baselines", "m05_writeback_ops", "m05_writeback_permits")
FN: Final = {
    "directory": "m05_directory_v3", "capture": "m05_writeback_capture_baseline",
    "discover": "m05_writeback_discover", "due": "m05_writeback_due",
    "claim": "m05_writeback_claim", "claim_reconcile": "m05_writeback_claim_reconcile",
    "decide": "m05_writeback_decide", "spend": "m05_writeback_spend",
    "settle": "m05_writeback_settle", "create": "m05_create_item_v2",
    "prepare": "m05_prepare_action_v3", "apply": "m05_apply_action_v3",
    "reason": "m05_followup_reason",
}
CLOSED_FUNCTIONS: Final = ("m05_directory_v2", "m05_create_item", "m05_prepare_action_v2",
                           "m05_apply_action_v2")
WB_CAP: Final = "m05.writeback"
T_CHECK: Final = "m05_writeback_check_raised"
T_RESOLUTION: Final = "m05_followup_resolution_asserted"
ITEM_KIND: Final = "ehr_confirmation_check"
REASONS: Final = ("status_changed", "declined_or_unknown", "ehr_unreachable",
                  "could_not_verify", "no_baseline")
OP_STATES: Final = ("pending", "eligible_check", "permitted", "spent", "observed_positive",
                    "item", "skipped")
WRITE_TIMEOUT_S: Final = 10
RECONCILE_MARGIN_S: Final = 30
LEASE_S: Final = 60
PROVIDER: Final = "prov-42"
DEPARTMENT: Final = "dept-7"


# --- registry, catalogue, install -------------------------------------------------------------


def si_units() -> Any:
    from haloflow.m05.writeback_units import SI_TENANT_MIGRATIONS

    return SI_TENANT_MIGRATIONS


def si_profiles() -> Any:
    from haloflow.m05.writeback_units import SI_INSTALLED_STATE_PROFILES

    return SI_INSTALLED_STATE_PROFILES


def si_amendments() -> Any:
    from haloflow.m05.writeback_units import SI_ACL_AMENDMENTS

    return SI_ACL_AMENDMENTS


def si_registry() -> Any:
    """The H3 registry inputs plus the S-I units, profiles and closure amendments."""

    import sb_support as sb

    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, sa.sa_units(), sb.sb_units(), sc.sc_units(),
        sh.sh_units(), sh2.sh2_units(), sh3.sh3_units(), si_units(),
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **sa.sa_profiles(),
                                  **sb.sb_profiles(), **sc.sc_profiles(),
                                  **sh.sh_profiles(), **sh2.sh2_profiles(),
                                  **sh3.sh3_profiles(), **si_profiles()},
        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *sh.sh_amendments(),
                        *sh2.sh2_amendments(), *si_amendments()),
    )


def si_catalog() -> Any:
    """The H3 TEST catalogue with every key in SI_REBOUND dropped from its source part and
    re-added from SI_REBOUND, plus SI_STATEMENTS; protected = H3 set | SI_PROTECTED."""

    from types import MappingProxyType

    import sb_support as sb

    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import build_statement_catalog
    from haloflow.m05.intake_statements import DIRECTORY_KEY
    from haloflow.m05.writeback_statements import SI_PROTECTED, SI_REBOUND, SI_STATEMENTS

    rebound = frozenset(SI_REBOUND)

    def drop(part: Any) -> Any:
        return MappingProxyType({k: v for k, v in part.items() if k not in rebound})

    approved = tuple(drop(part) for part in APPROVED_MODULE_STATEMENTS)
    sa_part = MappingProxyType({k: v for k, v in sh.sa_statements_without_inbound().items()
                                if k != DIRECTORY_KEY})
    sh_part = MappingProxyType({k: v for k, v in sh.sh_statements().items()
                                if k not in sh2.CLOSED_KEYS()})
    return build_statement_catalog(*approved, sa_part, sb.sb_statements(), sh_part,
                                   sh.sh_inbound_statements(), sh2.sh2_statements(),
                                   sh3.sh3_statements(), SI_REBOUND, SI_STATEMENTS,
                                   activation_protected=(sh2.protected() | sh3.sh3_protected()
                                                         | frozenset(SI_PROTECTED)))


SI_TEST_PROFILE_SQL: Final = f"""
UPDATE {{schema}}.event_types SET is_active = true WHERE event_type = '{T_CHECK}';
INSERT INTO {{schema}}.m05_intake_map (source_event_type, source_contract_version,
    discriminator_path, discriminator_value, item_kind, producer_workload_id,
    active_in_production) VALUES
""" + ",\n".join(
    f"    ('{T_CHECK}', 1, 'reason', '{reason}', '{ITEM_KIND}', '{sa.W_DISPATCH}', false)"
    for reason in REASONS) + ";\n"


class SIInstall(sh3.SH3Install):
    """The H3 install shape, provisioned through the S-I registry (target 93), with the S-I
    test profile (FIXTURE: activates the check event type and maps the five reasons)."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, si_registry(), self.tenant,
                                           supported=SI_SUPPORTED, manifest=staff_manifest())
                if self.profile:
                    sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                    sa.admin(self, SI_TEST_PROFILE_SQL.format(schema=self.schema))
                    sh2.settings_raw(self, clinic_display_name=sh2.CLINIC)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"S-I test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[SIInstall]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield SIInstall(ids, tenant)
    finally:
        reset(tenant)


def ready_install(install: SIInstall) -> SIInstall:
    install.require()
    sc.change(install, "grant")
    return install


@asynccontextmanager
async def runtime(install: SIInstall, *, policy: str = "ENFORCE",
                  max_size: int = 4) -> AsyncIterator[Any]:
    """An S-I TEST runtime (target 93, S-I catalogue, real resolver and gateway)."""

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
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=si_registry(),
            catalog=si_catalog(),
            supported_schema_versions=frozenset({SI_TARGET}),
            activation_policy=ActivationPolicy[policy],
        )
    finally:
        await pool.close()


# --- direct SQL calls (runtime role, one transaction each) -----------------------------------


def cmd(principal: str = sa.W_DISPATCH, **fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, **fields, "attribution": sa.attribution(principal)}


def call(install: sa.SAInstall, name: str, principal: str = sa.W_DISPATCH,
         **fields: Any) -> dict[str, Any]:
    return sa.call(install, FN[name], cmd(principal, **fields))


def refused(install: sa.SAInstall, name: str, code: str, principal: str = sa.W_DISPATCH,
            **fields: Any) -> None:
    sh2.refused(install, FN[name], cmd(principal, **fields), code)


def directory3(install: sa.SAInstall, op: str, **fields: Any) -> dict[str, Any]:
    return sa.call(install, FN["directory"], {"schema_version": 1, "op": op, **fields,
                                              "attribution": sa.attribution(sa.W_DIRECTORY)})


def appointment3(ref: str, patient: str, start: datetime | str, *,
                 provider_ref: str | None = PROVIDER, department_ref: str | None = DEPARTMENT,
                 **kw: Any) -> dict[str, Any]:
    """The strict v3 appointment fields: the v2 fields plus the two identity keys."""

    return {**sh2.appointment_fields(ref, patient, start, **kw),
            "provider_ref": provider_ref, "department_ref": department_ref}


def seed3(install: sa.SAInstall, *, provider_ref: str | None = PROVIDER,
          department_ref: str | None = DEPARTMENT) -> tuple[str, str, str, str]:
    """A due, in-window, sendable reminder ingested through directory v3:
    (patient, phone, ref, zone)."""

    zone, start = sh2.in_window(install)
    patient, phone, ref = sa.new_id(), sa.fresh_phone(), sa.new_id()
    assert directory3(install, "contact_put", phone=phone,
                      patient_id=patient)["outcome"] in ("applied", "existing")
    result = directory3(install, "appointment_put",
                        **appointment3(ref, patient, start, department_tz=zone,
                                       provider_ref=provider_ref,
                                       department_ref=department_ref))
    assert result["outcome"] == "applied", result
    return patient, phone, ref, zone


# --- flow builders (real runtime statements, SQL path; FIXTURE only where named) -------------


def sent(install: sa.SAInstall, *, capture: bool = True, provider_ref: str | None = PROVIDER,
         department_ref: str | None = DEPARTMENT) -> dict[str, str]:
    """directory v3 -> examine (claim) -> [T_cap] -> consume -> outcome accepted -> register
    (opens the reply window). The provider call itself is simulated (no sender exists in
    SQL), exactly as `sh3.persisted_accepted_op`. Returns op / attempt / ref / patient /
    phone."""

    patient, phone, ref, _zone = seed3(install, provider_ref=provider_ref,
                                       department_ref=department_ref)
    op_id, attempt = sh2.claimed(install, ref)
    if capture:
        result = call(install, "capture", op_id=op_id, attempt_id=attempt)
        assert result["outcome"] == "captured", result
    assert sh2.consume(install, op_id, attempt)["outcome"] == "send"
    sh.outcome(install, op_id, attempt, "accepted",
               provider_ref="fake-si-" + uuid.uuid4().hex[:12])
    assert sh.register(install, op_id)["outcome"] == "registered"
    return {"op_id": op_id, "attempt": attempt, "ref": ref, "patient": patient,
            "phone": phone}


def replied(install: sa.SAInstall, flow: dict[str, str], reply_class: str = "confirm"
            ) -> dict[str, Any]:
    """The patient's reply through intake v2 (t053), persisted with its reply class."""

    message = sa.new_id()
    result = sh.inbound(install, message, flow["phone"], reply_class)
    assert result["outcome"] in ("registered", "existing"), result
    flow["message"] = message
    return result


def discover(install: sa.SAInstall, limit: int = 100) -> int:
    result = call(install, "discover", limit=limit)
    assert set(result) == {"schema_version", "discovered"}, result
    count: int = result["discovered"]
    return count


def op_for_flow(install: sa.SAInstall, flow: dict[str, str]) -> dict[str, Any]:
    (found,) = rows(install, "m05_writeback_ops", "op_id = %s::uuid", (flow["op_id"],))
    return found


def confirmed_op(install: sa.SAInstall, **kw: Any) -> tuple[dict[str, str], str]:
    """A sent reminder, a confirm reply, discovered: (flow, wb_op_id)."""

    flow = sent(install, **kw)
    replied(install, flow, "confirm")
    assert discover(install) == 1
    return flow, str(op_for_flow(install, flow)["wb_op_id"])


def claim(install: sa.SAInstall, wb_op_id: str, lease_seconds: int = LEASE_S) -> dict[str, Any]:
    return call(install, "claim", wb_op_id=wb_op_id, lease_seconds=lease_seconds)


def checked(install: sa.SAInstall, **kw: Any) -> tuple[dict[str, str], str, int]:
    """A confirmed op claimed for the check phase: (flow, wb_op_id, fence)."""

    flow, wb = confirmed_op(install, **kw)
    result = claim(install, wb)
    assert (result["outcome"], result["phase"]) == ("claimed", "check"), result
    return flow, wb, int(result["fence"])


def ehr_read(install: sa.SAInstall, flow: dict[str, str], status: str = "neutral",
             **over: Any) -> dict[str, Any]:
    """A typed R1/R2 read as the worker passes it to decide / settle; by default it matches
    the frozen baseline exactly (appointment id = ref, start, provider, department)."""

    read: dict[str, Any] = {"status": status, "appointment_id": flow["ref"],
                            "start_at": sh2.iso_z(sh.start_of(install, flow["ref"])),
                            "provider_ref": PROVIDER, "department_ref": DEPARTMENT,
                            "replaced_by": None}
    read.update(over)
    return read


def decide(install: sa.SAInstall, wb_op_id: str, fence: int, read: Any,
           read_failed: bool = False) -> dict[str, Any]:
    return call(install, "decide", wb_op_id=wb_op_id, fence=fence, read=read,
                read_failed=read_failed)


def spend(install: sa.SAInstall, wb_op_id: str, fence: int, permit_id: str) -> dict[str, Any]:
    return call(install, "spend", wb_op_id=wb_op_id, fence=fence, permit_id=permit_id)


def settle(install: sa.SAInstall, wb_op_id: str, fence: int, permit_id: str,
           write_outcome: str, read: Any) -> dict[str, Any]:
    return call(install, "settle", wb_op_id=wb_op_id, fence=fence, permit_id=permit_id,
                write_outcome=write_outcome, read=read)


def permitted(install: sa.SAInstall, **kw: Any) -> tuple[dict[str, str], str, int, str]:
    """A checked op whose neutral, matching R1 produced a permit: (flow, wb, fence,
    permit_id)."""

    flow, wb, fence = checked(install, **kw)
    result = decide(install, wb, fence, ehr_read(install, flow))
    assert result["outcome"] == "permitted", result
    return flow, wb, fence, str(result["permit_id"])


def age(install: sa.SAInstall, wb_op_id: str, seconds: int) -> None:
    """FIXTURE time travel: move the op's lease, next_at and permit times `seconds` into the
    past (the H1 P08 pattern), so database-clock guards see them as elapsed."""

    shift = f"interval '{int(seconds)} seconds'"
    sa.admin(install, sa.q(install, "UPDATE {s}.m05_writeback_ops SET lease_until = lease_until"
                                    f" - {shift}, next_at = next_at - {shift}"
                                    " WHERE wb_op_id = %s::uuid"), (wb_op_id,))
    sa.admin(install, sa.q(install, "UPDATE {s}.m05_writeback_permits SET apply_deadline ="
                                    f" apply_deadline - {shift}, spent_at = spent_at - {shift}"
                                    " WHERE wb_op_id = %s::uuid"), (wb_op_id,))


# --- observation helpers (admin, FIXTURE reads only) -----------------------------------------


def rows(install: sa.SAInstall, table: str, where: str = "true", params: Any = None
         ) -> list[dict[str, Any]]:
    found = sa.admin(install, sa.q(install, f"SELECT to_jsonb(t) FROM {{s}}.{table} AS t"
                                            f" WHERE {where}"), params)
    return [dict(r[0]) for r in found]


def baseline(install: sa.SAInstall, op_id: str) -> dict[str, Any] | None:
    found = rows(install, "m05_writeback_baselines", "op_id = %s::uuid", (op_id,))
    return found[0] if found else None


def wb_op(install: sa.SAInstall, wb_op_id: str) -> dict[str, Any]:
    (found,) = rows(install, "m05_writeback_ops", "wb_op_id = %s::uuid", (wb_op_id,))
    return found


def wb_ops(install: sa.SAInstall) -> list[dict[str, Any]]:
    return rows(install, "m05_writeback_ops", "true ORDER BY created_at, wb_op_id")


def permit(install: sa.SAInstall, wb_op_id: str) -> dict[str, Any] | None:
    found = rows(install, "m05_writeback_permits", "wb_op_id = %s::uuid", (wb_op_id,))
    return found[0] if found else None


def check_events(install: sa.SAInstall) -> list[dict[str, Any]]:
    return rows(install, "patient_events", "event_type = %s", (T_CHECK,))


def check_items(install: sa.SAInstall) -> list[dict[str, Any]]:
    return rows(install, "m05_followup_items", "item_kind = %s", (ITEM_KIND,))


def execute_privilege(install: sa.SAInstall, function: str, role: str = sa.RUNTIME) -> bool:
    found = sa.admin(install, sa.q(install, "SELECT has_function_privilege(%s, p.oid, 'EXECUTE')"
                                            " FROM pg_proc AS p JOIN pg_namespace AS n"
                                            " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                                            " AND p.proname = %s"),
                     (role, install.schema, function))
    assert len(found) == 1, (function, found)
    return bool(found[0][0])


def db_now(install: sa.SAInstall) -> datetime:
    return sh2.db_now(install)


def new_uuid() -> str:
    return str(uuid.uuid4())
