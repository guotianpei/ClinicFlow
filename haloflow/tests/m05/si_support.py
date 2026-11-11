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
SI_ACL_AMENDMENTS), TEST target 94, all additive, t001-t075 byte-unchanged:
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
  m05_writeback_permits(permit_id uuid PK, wb_op_id uuid -> m05_writeback_ops,
  issued_fence, r1_digest bytea (32 bytes), apply_deadline NULL, spent_at NULL,
  spent_fence NULL, voided_at NULL, closed_at NULL, created_at), with a partial UNIQUE
  index on (wb_op_id) WHERE voided_at IS NULL: AT MOST ONE LIVE permit per op. A voided
  permit row is KEPT (history) and is never spendable again; a later decide may issue a
  NEW permit (new permit_id) for the same op. A permit is never deleted or un-voided.
- t080 `m05_writeback_capture_baseline` {op_id, attempt_id} -> {schema_version,
  outcome: "captured" | "refused", code}; the t071 lock order (chain, then ticket row FOR
  UPDATE); refuses unless op claimed, ticket.attempt_id = attempt, consumed_at NULL, profile
  source_version = render_inputs.source_version; a second capture is refused.
  An op_id that is not this tenant's -> "refused" with code "OP_NOT_FOUND".
- t081 `m05_writeback_discover` {limit 1..1000} -> {schema_version, discovered}: one op per
  inbound reply with reply_class 'confirm' and decision 'recorded' matched to a reminder of
  an outbound op (ON CONFLICT DO NOTHING); never for any other reply class.
- t082 `m05_writeback_due` {limit} -> {schema_version, ops: [{wb_op_id, phase}]}, phase
  "check" (pending, or eligible_check / permitted whose lease has expired, with next_at
  NULL or <= clock_timestamp(): a permit not spent before its lease lapsed is recovered
  through due -> claim (void) -> a fresh decide -> a new permit) or "reconcile" (spent,
  unsettled, clock_timestamp() > apply_deadline + RECONCILE_MARGIN_S); read only.
- t083 `m05_writeback_claim` {wb_op_id, lease_seconds} -> {schema_version, outcome:
  "claimed" | "not_due" | "not_claimable", code, fence, phase: "check", appointment_ref};
  an op whose permit is spent and unsettled is "not_claimable" with code "PERMIT_SPENT";
  the check phase only (pending, eligible_check or permitted, lease expired or free); fence :=
  fence + 1, state := eligible_check; the live unspent permit from an older fence is
  VOIDED (voided_at) in the same transaction; a spent permit makes the op not_claimable.
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
  A wb_op_id that is not this tenant's -> "refused" with code "OP_NOT_FOUND".
- t087 `m05_writeback_settle` {wb_op_id, fence, permit_id, write_outcome, read} ->
  {schema_version, outcome: "observed_positive" | "item" | "stale", reason, event_id,
  patient_id}; fenced; NOT activation-protected. A wb_op_id that is not this tenant's ->
  "stale" (reason and event_id null).
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
- t094 `m05_writeback_create_settled_item` {wb_op_id, fence, permit_id, source_event_id,
  item_kind = ITEM_KIND} -> the t089 result (Codex 20261110T155135Z: the settlement's
  recovery create, key `m05.followup.create_settled`, capability `m05.followup.create`, NOT
  activation-protected). Refused (P0001 SETTLED_ITEM_NOT_ELIGIBLE) unless, under the op
  lock: state 'item', op.fence = fence, op.event_id = source_event_id, op.item_id NULL,
  the op's own live SPENT permit = permit_id (locked), and the event is this op's
  canonical check event (type, contract 1, metadata wb_op_id and reason). Then t089 with
  the same attribution. A decision's item uses `m05.followup.create` (protected).

Statements (`haloflow.m05.writeback_statements`): SI_STATEMENTS, SI_REBOUND (the existing
keys `m05.intake.directory`, `m05.followup.create`, `m05.followup.prepare`,
`m05.followup.apply` bound to v3 / v2 / v3 / v3), SI_PROTECTED (discover, claim, decide,
spend), SI_CLOSED_KEYS. New keys: CAPTURE_KEY "m05.writeback.capture" (capability
`m05.intake.dispatch`); DISCOVER / DUE / CLAIM / CLAIM_RECONCILE / DECIDE / SPEND / SETTLE
"m05.writeback.*" (capability "m05.writeback"); REASON_KEY "m05.followup.reason"
(capability `m05.followup.read`); CREATE_SETTLED_KEY "m05.followup.create_settled" (t094,
capability `m05.followup.create`, not protected; used by a settlement's item only).
`haloflow.m05.writeback.WritebackService(foundation)`
runs decide / settle in ONE gateway transaction with the M02 append and the item create
(the IntakeService pattern); its CAPABILITIES = {"m05.writeback",
"m02.observation.append", "m05.followup.create"}; item producer workload W_DISPATCH.
Constants: WRITE_TIMEOUT_S = 10, RECONCILE_MARGIN_S = 30, LEASE_S = 60. Statement key
values (`haloflow.m05.writeback_statements`): CAPTURE_KEY "m05.writeback.capture",
DISCOVER_KEY "m05.writeback.discover", DUE_KEY "m05.writeback.due", CLAIM_KEY
"m05.writeback.claim", CLAIM_RECONCILE_KEY "m05.writeback.claim_reconcile", DECIDE_KEY
"m05.writeback.decide", SPEND_KEY "m05.writeback.spend", SETTLE_KEY "m05.writeback.settle",
REASON_KEY "m05.followup.reason".

Service pins (`haloflow.m05.writeback`):
- `WritebackService(foundation)`: `capture_baseline(context, op_id, attempt_id) -> str`
  (dispatch context; the capture statement only); `discover(context, limit=100) -> int`;
  `due(context, limit=100) -> list[{"wb_op_id", "phase"}]`; `claim(context, wb_op_id) ->
  dict`: the protected check claim (CLAIM_KEY) first; ONLY when it returns
  {"outcome": "not_claimable", "code": "PERMIT_SPENT"} or raises ActivationRefused, the
  unprotected reconcile claim (CLAIM_RECONCILE_KEY) follows and its result is returned;
  every other result is returned as is and every other refusal (TenantDenied,
  CapabilityDenied, WritebackRefused, WritebackFailed) propagates; `decide(context, wb_op_id,
  fence, read, read_failed) -> dict`; `spend(context, wb_op_id, fence, permit_id) ->
  dict`; `settle(context, wb_op_id, fence, permit_id, write_outcome, read) -> dict`.
  `read` is a `simulated_ehr.EhrRead` or None. An "item" decision or settlement runs in
  ONE gateway transaction: the SQL call (which mints and stores the event id once), the
  M02 append of `m05_writeback_check_raised` v1 (correlation_id = wb_op_id,
  producer_dedupe_key "wbcheck:<wb_op_id hex>", metadata exactly {wb_op_id,
  appointment_ref, reason}), and `m05.followup.create` (v2) with item_kind
  ITEM_KIND; the result carries "item_id". A failure anywhere rolls all three back; a
  database refusal is WritebackFailed(reason_code) (the P0001 message, else
  "DATABASE_ERROR", as IntakeFailed); after the commit marker a lost acknowledgement is
  WritebackCommitUnknown (the IntakeService rule). Admission before any I/O: a WORKLOAD
  principal and context capabilities EQUAL to CAPABILITIES (capture: the dispatch set)
  else WritebackRefused
  ("PRINCIPAL_NOT_WORKLOAD" / "CAPABILITIES_INVALID").
- `StaffServiceV3(M05Service)`: identical to M05Service except that the resolution
  observation is appended at contract_version 2 (every kind), plus `reason(context,
  item_id) -> {"schema_version": 1, "item_id", "reason"}` through REASON_KEY (ReadFailed
  ITEM_NOT_FOUND otherwise). `haloflow.m05.service.M05Service` and every production
  binding are unchanged.
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

import haloflow.synthetic.composition as _synthetic

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
    "t094_m05_writeback_create_settled_item",
)
SI_TARGET: Final = 94
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
    "reason": "m05_followup_reason", "create_settled": "m05_writeback_create_settled_item",
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
    """The S-I TEST registry (target 94). Single source since Option B B1:
    `haloflow.synthetic.composition.build_synthetic_registry`, pinned against the frozen
    pre-move manifest (tests/synthetic, B1-E1)."""

    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry()


def si_catalog() -> Any:
    """The S-I TEST catalogue. Single source since Option B B1:
    `haloflow.synthetic.composition.build_synthetic_catalog` (B1-E2)."""

    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog()


# Single source since Option B B1 (B1-E3 pins its digest); identities must not drift.
SI_TEST_PROFILE_SQL: Final = _synthetic.SI_TEST_PROFILE_SQL
assert (T_CHECK, ITEM_KIND, REASONS, sa.W_DISPATCH) == (
    _synthetic.T_CHECK, _synthetic.ITEM_KIND_CHECK, _synthetic.CHECK_REASONS,
    _synthetic.W_DISPATCH), "fixture identity drift"


class SIInstall(sh3.SH3Install):
    """The H3 install shape, provisioned through the S-I registry (target 94), with the S-I
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
    """An S-I TEST runtime (target 94, S-I catalogue, real resolver and gateway)."""

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


def age(install: sa.SAInstall, wb_op_id: str, *, lease: int = 0, next_at: int = 0,
        deadline: int = 0) -> None:
    """FIXTURE time travel, each clock INDEPENDENTLY (the H1 P08 pattern): move the op's
    lease_until by `lease` seconds, its next_at by `next_at`, and the live permit's
    apply_deadline and spent_at by `deadline`, into the past."""

    if lease or next_at:
        sa.admin(install, sa.q(install, "UPDATE {s}.m05_writeback_ops SET lease_until ="
                                        " lease_until - make_interval(secs => %s),"
                                        " next_at = next_at - make_interval(secs => %s)"
                                        " WHERE wb_op_id = %s::uuid"),
                 (lease, next_at, wb_op_id))
    if deadline:
        sa.admin(install, sa.q(install, "UPDATE {s}.m05_writeback_permits SET apply_deadline ="
                                        " apply_deadline - make_interval(secs => %s),"
                                        " spent_at = spent_at - make_interval(secs => %s)"
                                        " WHERE wb_op_id = %s::uuid AND voided_at IS NULL"),
                 (deadline, deadline, wb_op_id))


def set_deadline(install: sa.SAInstall, wb_op_id: str, instant: datetime) -> None:
    """FIXTURE: put the live permit's apply_deadline at an exact instant (and spent_at
    WRITE_TIMEOUT_S before it)."""

    sa.admin(install, sa.q(install, "UPDATE {s}.m05_writeback_permits SET apply_deadline = %s,"
                                    " spent_at = %s - make_interval(secs => %s)"
                                    " WHERE wb_op_id = %s::uuid AND voided_at IS NULL"),
             (instant, instant, WRITE_TIMEOUT_S, wb_op_id))


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
    """The op's LIVE (not voided) permit, if any."""

    found = rows(install, "m05_writeback_permits",
                 "wb_op_id = %s::uuid AND voided_at IS NULL", (wb_op_id,))
    assert len(found) <= 1, found
    return found[0] if found else None


def permits(install: sa.SAInstall, wb_op_id: str) -> list[dict[str, Any]]:
    """Every permit row of the op, voided history included, oldest first."""

    return rows(install, "m05_writeback_permits",
                "wb_op_id = %s::uuid ORDER BY created_at, permit_id", (wb_op_id,))


def due(install: sa.SAInstall, wb_op_id: str) -> list[str]:
    """The phases `due` lists for this op (empty: not due)."""

    result = call(install, "due", limit=1000)
    return [o["phase"] for o in result["ops"] if o["wb_op_id"] == wb_op_id]


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


# --- service-level helpers (runtime) ---------------------------------------------------------


def wb_caps() -> frozenset[str]:
    from haloflow.m05.writeback import CAPABILITIES

    return frozenset(CAPABILITIES)


async def wb_context(rt: Any, install: sa.SAInstall, caps: frozenset[str] | None = None,
                     principal: str = sa.W_DISPATCH, kind: str = "workload") -> Any:
    import service_support as sv

    return await sv.context(rt, install, principal, wb_caps() if caps is None else caps,
                            kind=kind)


def wb_service(rt: Any) -> Any:
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.writeback import WritebackService

    return WritebackService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER))


def ehr_from_flow(install: sa.SAInstall, flow: dict[str, str], clock: Any,
                  status: str = "neutral", **over: Any) -> Any:
    """A SimulatedEhr holding one appointment whose read matches the flow's baseline."""

    from haloflow.m05.simulated_ehr import EhrRead, SimulatedEhr

    ehr = SimulatedEhr(clock=clock)
    fields: dict[str, Any] = {"status": status, "appointment_id": flow["ref"],
                              "start_at": sh.start_of(install, flow["ref"]),
                              "provider_ref": PROVIDER, "department_ref": DEPARTMENT,
                              "replaced_by": None}
    fields.update(over)
    ehr.put(EhrRead(**fields))
    return ehr


def db_clock(install: sa.SAInstall) -> Callable[[], datetime]:
    """The SimulatedEhr clock on the test database's clock_timestamp() (one time base)."""

    def clock() -> datetime:
        value: datetime = sa.admin(install, "SELECT clock_timestamp()")[0][0]
        return value

    return clock


class StaffChain:
    """The S-D staff chain with the S-I staff service (resolution contract v2)."""

    def __init__(self, install: Any, rt: Any) -> None:
        import sd_support as sd
        import service_support as sv

        from haloflow.m02.observation import EventFoundation
        from haloflow.m05.writeback import StaffServiceV3

        self.install, self.rt = install, rt
        self.chain = sd.Chain(install, rt)
        self.chain.staff = StaffServiceV3(
            rt.gateway, foundation=EventFoundation(rt.gateway, producer_service=sv.PRODUCER),
            keys=self.chain.keys)

    async def reason(self, item: uuid.UUID, caps: frozenset[str] | None = None) -> Any:
        import service_support as sv

        actor = await self.chain.actor(sv.READ_ONLY_CAPS if caps is None else caps)
        return await self.chain.staff.reason(actor, item)


# --- test seams: statement spy and lost-acknowledgement gateway ------------------------------

KEYS: Final = {name: f"m05.writeback.{name}" for name in
               ("capture", "discover", "due", "claim", "claim_reconcile", "decide", "spend",
                "settle")}
CREATE_KEY: Final = "m05.followup.create"
CREATE_SETTLED_KEY: Final = "m05.followup.create_settled"
APPEND_KEY: Final = "m02.observation.append"


class StatementSpy:
    """Every statement the tenant handle runs (execute / fetch_one / fetch_all), in order,
    as (key, "ok" | "raised:<ExceptionClass>")."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def ordered_keys(self) -> list[str]:
        return [key for key, _ in self.calls]

    def since(self, mark: int) -> list[tuple[str, str]]:
        return self.calls[mark:]


def spy_statements(monkeypatch: Any) -> StatementSpy:
    """Patch TenantRepositoryHandle for ONE row (pytest's monkeypatch undoes it)."""

    from haloflow.m01.gateway import TenantRepositoryHandle

    spy = StatementSpy()
    for name in ("execute", "fetch_one", "fetch_all"):
        original = getattr(TenantRepositoryHandle, name)

        def make(method: Any) -> Any:
            async def recorded(self: Any, statement_key: str, params: Any = None) -> Any:
                try:
                    result = await method(self, statement_key, params)
                except BaseException as error:
                    spy.calls.append((statement_key, f"raised:{type(error).__name__}"))
                    raise
                spy.calls.append((statement_key, "ok"))
                return result

            return recorded

        monkeypatch.setattr(TenantRepositoryHandle, name, make(original))
    return spy


class AckGateway:
    """Wraps the runtime gateway: counts tenant transactions and, for the transaction
    numbers in `lose`, lets the transaction COMMIT and then raises ConnectionError (the
    acknowledgement is lost after the commit)."""

    def __init__(self, gateway: Any, lose: tuple[int, ...] = ()) -> None:
        self.gateway = gateway
        self.lose = frozenset(lose)
        self.transactions = 0

    async def with_tenant_transaction(self, context: Any, callback: Any) -> Any:
        self.transactions += 1
        result = await self.gateway.with_tenant_transaction(context, callback)
        if self.transactions in self.lose:
            raise ConnectionError("acknowledgement lost after commit")
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.gateway, name)


def wb_service_on(gateway: Any) -> Any:
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.writeback import WritebackService

    return WritebackService(EventFoundation(gateway, producer_service=sa.PRODUCER))
