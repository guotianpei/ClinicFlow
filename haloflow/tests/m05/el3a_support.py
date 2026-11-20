"""Eligibility E3a test support (RED BY DESIGN until E3a source exists).

Governing: eligibility requirements v1r1 (EL-10, EL-11, EL-13, EL-14, EL-14a); architecture
v1 section 5, v1r1 R3 (durable case handoff) and R4 (display projection, audit), v1r2 M2
(per-appointment ordering, applier rules), v1r3 C1 (case_version; note-only events never
overwrite the current reason); stage-3 test cases v1 (E3 rows) + v1r1 T1/T3; E3 sequencing
v1 (`claude_e3-scope-v1-2026-11-19.md`, sha256 58da39b9...) and Codex ALIGN with three
obligations (`codex_e3-sequencing-review.md`, relay f14a103a). Synthetic only. E3a builds on
E2 (main 4105f74).

INTERFACE PINS (E3a red packet v1, section 2) - what the E3a source must provide:

Registry / catalogue
- `build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True)` and the
  same flags on `build_synthetic_catalog`; `include_el3a` without `include_el2` is refused
  (ValueError). E3a units t118..t121, target **121** (a green deviation in the count is
  disclosed, as E1/E2 did). Production registry {36} and catalogue unchanged.

Schema (migrator unit t118 `m05_el_case_tables`; owner `haloflow_m05_owner`; no runtime
table privilege)
- The item / intake-map kind CHECKs become the eight kinds: the five existing plus
  `eligibility_inactive`, `eligibility_inconclusive`, `eligibility_manual`.
- Event type `m05_eligibility_case_event` (family `m05_intake`, INACTIVE; the E3a test
  profile activates it), contract v1, metadata closed to exactly
  {appointment_ref, case_event_id, verification_id (string|null), seq (int),
  coverage_version (int), class (string|null), reason, effect, open_kind, record};
  `effect` in {opened, noted, none}; `open_kind` is the item kind when effect = opened,
  else "none". `record` (v1r1, Codex red review 1 and event-identity review) is the
  settlement discriminator: "settlement" iff the case event records a verification
  settlement - an E2 settlement (the event's own verification_id) or an E1 manual
  settlement (no verification_id on the event: the applier resolves the BOOKING-owned
  verification of the same appointment and coverage version, settled manual_required) -
  and then `verification_id` is that verification; "finding" for an unresolved finding with
  no verification (`coverage_not_received`); "note" for a note-only event. Only
  "settlement" carries a verification_id. So exactly one settlement observation exists per
  settled verification, and a note can never count as a settlement.
  Every metadata path is allow-listed for consumer m05. No identity or benefit field.
  (Deviation D-R1, declared: architecture v1 section 5 named an opens-only type
  `m05_eligibility_case_raised`; Codex obligation 1 needs one evidence event for EVERY
  applied case event, so one type carries all three effects.)
- `m05_el_cases (case_id uuid PK, appointment_ref uuid NOT NULL -> m05_el_appointments,
  state CHECK IN ('open','closed'), item_kind varchar(32) NOT NULL (one of the three),
  source_event_id uuid NOT NULL UNIQUE (the opening evidence event), item_id uuid UNIQUE NULL
  (set by create_item_v3 in the same transaction), current_class varchar(16) NOT NULL,
  current_reason varchar(32) NOT NULL, case_version integer NOT NULL CHECK >= 1,
  opened_at timestamptz NOT NULL, closed_at timestamptz)`; partial UNIQUE (appointment_ref)
  WHERE state = 'open'. Closed rows are kept.
- `m05_el_case_notes (note_id uuid PK, case_id uuid -> m05_el_cases, case_event_id uuid ->
  m05_el_case_events, seq integer, kind varchar(16), class varchar(16) NULL, reason
  varchar(32), created_at timestamptz)`, UNIQUE (case_id, case_event_id). The opening event
  is note 1 of its case (so the note history always starts with the finding that opened it).
- `m05_el_case_events` gains `evidence_event_id uuid UNIQUE NULL` (the M02 event id, minted
  and stored once when the event is applied) and `effect varchar(8) NULL CHECK IN
  ('opened','noted','none')`; both NULL exactly while `applied_at` is NULL.

Gateways (jsonb, schema_version 1, closed key sets -> COMMAND_MALFORMED)
- t119 `m05_create_item_v3(p_command jsonb)`: the t089 body with the three kinds added; for
  an eligibility kind the source event must be the `source_event_id` of an OPEN case of that
  kind whose item_id is NULL (else SOURCE_NOT_ELIGIBLE) and the new item id is written to
  that case in the same transaction. Replay of the same create returns the same item
  (`created_or_existing` = existing) and changes nothing. Legacy kinds behave as t089.
- t120 `m05_el_apply({})` (timer workload attribution): picks the appointment whose lowest
  unapplied case event is oldest, FOR UPDATE SKIP LOCKED on its key row; applies ONLY that
  lowest-seq event, under the lock order appointment key -> case -> item, by the rules in
  order (architecture v1r2 M2, v1r3 C1):
    1. already applied -> never selected (a second call finds the next event or idle);
    2. coverage_version < the key row's current version -> effect none;
    3. seq <= closure_watermark -> effect none (reachable in E3a only through a FIXTURE;
       E3b sets the watermark by staff closure);
    4. an open case -> effect noted: a note; case_version + 1; `current_class` /
       `current_reason` := the event's class / reason ONLY for an unresolved finding (class
       inactive / inconclusive / manual_required, including `coverage_not_received`); never
       for class active (the note records "later check: active" and the case stays open,
       OD-EL3; `coverage_active` has no staff text) and never for a note-only event (C1);
    5. no open case: class inactive / inconclusive / manual_required -> effect opened: a new
       case (case_version 1, the event as note 1, item_kind by class: inactive ->
       eligibility_inactive, inconclusive -> eligibility_inconclusive, manual_required ->
       eligibility_manual); class active or a note-only event -> effect none.
  It sets applied_at, effect and a newly minted evidence_event_id, and returns
  `{"outcome": "applied", "effect", "case_event_id", "appointment_ref", "seq",
  "case_id" (string|null), "item_kind" (string|null), "evidence": {"event_id",
  "patient_id", "occurred_at" (the case event's created_at, UTC ISO), "metadata": {...}}}`
  or `{"outcome": "idle"}`. Refusals: a closed key set (COMMAND_MALFORMED); a principal
  that is not the timer workload (WORKLOAD_NOT_AUTHORIZED, the `_workload("timer")` check).
  NOT activation-protected (deviation D-R2, declared; SI-R12 precedent): a finding already
  settled while the clinic was enabled still reaches staff after the switch goes off;
  nothing new is checked against a payer by applying it.
- t121 `m05_el_case_view(p_item_id uuid)` STABLE, runtime EXECUTE (read key, actor
  attribution through the existing read path): for an eligibility item with a case ->
  `{"schema_version": 1, "item_id", "item_kind", "case_id", "case_version",
  "case_state", "class", "reason", "summary", "notes"}`; `class` / `reason` are the case's
  current ones; `summary` is null unless the latest applied payer finding of this case
  carries a verification whose answered attempt still holds benefit fields, then exactly
  the OD-EL4 projection {payer_name, plan, copay, deductible_remaining, referral_required,
  auth_required, checked_at} with absent fields omitted (never coinsurance,
  deductible_total, txn_ref, member or name or DOB); `notes` = [{"seq", "kind", "class",
  "reason", "at"}] by seq. Any other id (unknown, legacy kind, other tenant) ->
  `{"schema_version": 1, "error": "ITEM_NOT_FOUND"}` (the t092 pattern).

Service pins (`haloflow.m05.eligibility_cases`)
- `CASE_CAPABILITIES = frozenset({"m05.eligibility.cases", "m02.observation.append"})`;
  `EligibilityCaseService(foundation)` with `apply_one(context) -> str` (the effect, or
  "idle") and `drain(context, limit=100) -> dict[str, int]` (counts by effect, stopping at
  idle or limit). `apply_one` runs ONE gateway transaction: `m05.eligibility.apply`, then
  the M02 append through `foundation.append_observation_in(binding, Observation(...))` of
  `m05_eligibility_case_event` v1 (event_id = the evidence event id,
  correlation_id = the case event id, producer_dedupe_key "elcase:<case_event_id hex>",
  patient_id from the result, source_time_quality "trusted", links ()), then for effect
  opened `m05.eligibility.create_item` (v3) with that event as source and the result's
  item_kind. A failure anywhere rolls all of it back (the event stays unapplied); a database
  refusal is `EligibilityCaseFailed(reason_code)`; any other exception raised before the body
  completes propagates unchanged (the IntakeService rule); a lost acknowledgement after the commit
  marker is `EligibilityCaseCommitUnknown`. Admission before any I/O: a WORKLOAD principal
  and context capabilities EQUAL to CASE_CAPABILITIES, else
  `EligibilityCaseRefused("PRINCIPAL_NOT_WORKLOAD" | "CAPABILITIES_INVALID")`.
- `EligibilityScheduler(..., cases=None, case_contexts=None)`: two optional keyword
  arguments (an `EligibilityCaseService` and a `Callable[[str], Awaitable[TenantContext]]`,
  both or neither, else `EligibilitySchedulerRefused("CASES_INVALID")`). `TenantReport`
  gains `apply: PhaseReport` (counts {"opened", "noted", "none"}); with cases given, each
  tenant runs run -> expire -> apply (drain up to batch_limit); without, `apply` is
  NOT_RUN with zero counts and every existing behaviour is unchanged.
- Staff read: `haloflow.m05.eligibility_cases.case_view(service_gateway_context, item_id)`
  through statement `m05.eligibility.case_view` (capability `m05.followup.read`).

Labels (`haloflow.m05.eligibility_labels`)
- `REASON_LABELS`: the eleven staff-facing reasons, byte-exact OD-EL8 text (E3-U01, reason
  part). The outcome and contact-entry labels ship with E3b.

Statement keys (catalogue): `m05.eligibility.apply`, `m05.eligibility.create_item`
(capability `m05.eligibility.cases`), `m05.eligibility.case_view` (`m05.followup.read`).

Legacy staff path (Codex obligation 3; v1r1 pins the refusal): until E3b, a resolve or
claim_and_resolve of an eligibility item through the existing staff chain (M05Service actions
+ StaffServiceV3, resolution contract v2) raises `haloflow.m05.service.ActionFailed` with
reason_code "ACTION_RESULT_INVALID": the service checks the NEW decision's item kind against
its closed staff kind set before any key use, M02 append or apply (green correction G-2,
measured; v1r1 had predicted AppendFailed COMMAND_MALFORMED at the append, which would also
have refused, later). The transaction rolls back: item state, assignee and version unchanged.

Test profile: `haloflow.synthetic.composition.EL3A_TEST_PROFILE_SQL` activates the event
type and inserts three intake-map rows (`m05_eligibility_case_event`, 1, 'open_kind',
<kind>, <kind>, W_TIMER, false).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, Final

import el1_support as el
import el2_support as e2
import m02_support
import sa_support as sa

EL3A_TARGET: Final = 121
EL3A_SUPPORTED: Final = range(EL3A_TARGET, EL3A_TARGET + 1)
NY: Final = el.NY

EL_KINDS: Final = ("eligibility_inactive", "eligibility_inconclusive", "eligibility_manual")
KIND_BY_CLASS: Final = {"inactive": "eligibility_inactive",
                        "inconclusive": "eligibility_inconclusive",
                        "manual_required": "eligibility_manual"}
EVENT_TYPE: Final = "m05_eligibility_case_event"
METADATA_KEYS: Final = ("appointment_ref", "case_event_id", "class", "coverage_version",
                        "effect", "open_kind", "reason", "record", "seq", "verification_id")
EL3A_TABLES: Final = (*e2.EL2_TABLES, "m05_el_cases", "m05_el_case_notes")
CASE_CAPABILITIES: Final = frozenset({"m05.eligibility.cases", "m02.observation.append"})
STATEMENT_KEYS: Final = ("m05.eligibility.apply", "m05.eligibility.create_item",
                         "m05.eligibility.case_view")
VIEW_KEYS: Final = ("case_id", "case_state", "case_version", "class", "item_id",
                    "item_kind", "notes", "reason", "schema_version", "summary")
SUMMARY_KEYS: Final = ("auth_required", "checked_at", "copay", "deductible_remaining",
                       "payer_name", "plan", "referral_required")
NOTE_ONLY: Final = ("appointment_cancelled", "appointment_rescheduled", "coverage_changed")

# OD-EL8 (Rachel, 2026-11-18), byte-exact.
REASON_LABELS: Final = {
    "coverage_inactive": "Payer reports coverage not active",
    "subscriber_not_found": "Payer could not find the member",
    "name_dob_mismatch": "Name or date of birth does not match payer records",
    "member_id_not_recognized": "Member ID not recognized by payer",
    "service_type_unsupported": "Payer could not answer for this service type",
    "payer_unavailable": "Payer did not respond (tried twice)",
    "response_unreadable": "Payer response could not be read",
    "nonpriority": "Payer not set up for automatic checks",
    "no_coverage": "No insurance on file",
    "incomplete": "Insurance details incomplete",
    "coverage_not_received": "Insurance not received from EHR",
}


def el3a_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True)


def el3a_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(include_el1=True, include_el2=True, include_el3a=True)


def el3a_profile_sql() -> str:
    from haloflow.synthetic.composition import EL3A_TEST_PROFILE_SQL

    return str(EL3A_TEST_PROFILE_SQL)


class EL3AInstall(e2.EL2Install):
    """A synthetic tenant at target 121 (S-I TEST + B5 + E1 + E2 + E3a), provisioned in the
    row, with the S-I and E3a test profiles and the staff resolution activation."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                import sh2_support as sh2
                import si_support as si
                from staff_support import staff_manifest

                from haloflow.synthetic.composition import RESOLUTION_PROFILE_SQL

                m02_support.provision_sync(self.ids, el3a_registry(), self.tenant,
                                           supported=EL3A_SUPPORTED,
                                           manifest=staff_manifest())
                sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                sa.admin(self, si.si_test_profile_sql().format(schema=self.schema))
                sa.admin(self, RESOLUTION_PROFILE_SQL.format(schema=self.schema))
                sa.admin(self, el3a_profile_sql().format(schema=self.schema))
                sh2.settings_raw(self, clinic_display_name=sh2.CLINIC, clinic_tz=NY)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"E3a test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[EL3AInstall]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield EL3AInstall(ids, tenant)
    finally:
        reset(tenant)


def ready(install: EL3AInstall, **settings: Any) -> EL3AInstall:
    el.ready(install, **settings)
    return install


# --- direct gateway wrappers --------------------------------------------------------------


def apply_raw(install: EL3AInstall) -> dict[str, Any]:
    """The t120 gateway alone (no M02 append, no create): used only where a row inspects
    the gateway's own result or refusal."""
    return sa.call(install, "m05_el_apply",
                   {"schema_version": 1, "attribution": sa.attribution(sa.W_TIMER)})


def view(install: EL3AInstall, item_id: str) -> dict[str, Any]:
    """t121 as the runtime role (its EXECUTE grant is part of what is tested)."""
    from psycopg import sql

    with sa.runtime_conn(install) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant_id,))
        row = conn.execute(
            sql.SQL("SELECT {}.m05_el_case_view(%s::uuid)").format(
                sql.Identifier(install.schema)), (item_id,)).fetchone()
    assert row is not None
    return dict(row[0])


# --- observation helpers (admin connection) -----------------------------------------------


q = e2.q
count = e2.count


def rows(install: EL3AInstall, table: str, where: str = "true", params: Any = None
         ) -> list[dict[str, Any]]:
    got = sa.admin(install, sa.q(install, f"SELECT to_jsonb(t) FROM {{s}}.{table} AS t"
                                          f" WHERE {where}"), params)
    return [dict(r[0]) for r in got]


def case_events(install: EL3AInstall, ref: str) -> list[dict[str, Any]]:
    got = rows(install, "m05_el_case_events", "appointment_ref = %s::uuid", (ref,))
    return sorted(got, key=lambda r: int(r["seq"]))


def cases(install: EL3AInstall, ref: str) -> list[dict[str, Any]]:
    got = rows(install, "m05_el_cases", "appointment_ref = %s::uuid", (ref,))
    return sorted(got, key=lambda r: str(r["opened_at"]))


def notes(install: EL3AInstall, case_id: str) -> list[dict[str, Any]]:
    got = rows(install, "m05_el_case_notes", "case_id = %s::uuid", (case_id,))
    return sorted(got, key=lambda r: int(r["seq"]))


def evidence(install: EL3AInstall, ref: str | None = None) -> list[dict[str, Any]]:
    """The M02 evidence events (optionally for one appointment), by occurrence."""
    got = rows(install, "patient_events", "event_type = %s", (EVENT_TYPE,))
    if ref is not None:
        got = [r for r in got if (r.get("metadata") or {}).get("appointment_ref") == ref]
    return got


def el_items(install: EL3AInstall, patient: str | None = None) -> list[dict[str, Any]]:
    kinds = ", ".join(f"'{k}'" for k in EL_KINDS)
    got = rows(install, "m05_followup_items", f"item_kind IN ({kinds})")
    if patient is not None:
        got = [r for r in got if r["patient_id"] == patient]
    return got


def set_watermark(install: EL3AInstall, ref: str, value: int) -> None:
    """FIXTURE (Codex obligation 2): the closure watermark that E3b's staff close sets."""
    q(install, "UPDATE {s}.m05_el_appointments SET closure_watermark = %s"
      " WHERE appointment_ref = %s", (value, ref))


# --- scenario builders over E1/E2 (each leaves UNAPPLIED case events) ---------------------


def manual(install: EL3AInstall, start: Any, reason: str = "nonpriority") -> tuple[str, str]:
    """An E1 manual settlement at intake: nonpriority (default), no_coverage or incomplete."""
    ref, patient = sa.new_id(), sa.new_id()
    if reason == "no_coverage":
        el.put(install, ref, patient, start, coverage={"state": "none"})
    elif reason == "incomplete":
        el.put(install, ref, patient, start, coverage=el.present(member_id=None))
    else:
        el.put(install, ref, patient, start, coverage=el.present(el.OTHER_PAYER))
    return ref, patient


def answer_of(cls: str, reason: str, **extra: Any) -> Any:
    """A normalized answer observed now; "timeout" passes through (a timed-out attempt)."""
    from datetime import UTC, datetime

    if cls == "timeout":
        return "timeout"
    now = datetime.now(UTC)
    got = e2.active_result(now) if cls == "active" else e2.result_of(cls, reason, now)
    got.update(extra)
    return got


def payer(install: EL3AInstall, start: Any, cls: str, reason: str,
          **extra: Any) -> tuple[str, str, str]:
    """An E2 settlement of the booking verification with one answer of `cls` / `reason`
    (or two timeouts when cls == "timeout"). Returns (appointment_ref, patient_id,
    verification_id)."""
    ref, patient = e2.enrol(install, start)
    e2.due(install)
    vid = e2.own_verification(install, ref, "booking")
    answers = (["timeout", "timeout"] if cls == "timeout"
               else [answer_of(cls, reason, **extra)])
    got = e2.run(install, vid, answers)["settle"]
    assert got["outcome"] == "settled", got
    return ref, patient, vid


def later(install: EL3AInstall, ref: str, trigger: str, cls: str, reason: str,
          **extra: Any) -> str:
    """Settle the appointment's `trigger` (previsit | dayof) verification with one answer."""
    e2.make_due(install, ref, trigger)
    e2.due(install)
    vid = e2.own_verification(install, ref, trigger)
    got = e2.run(install, vid, [answer_of(cls, reason, **extra)])["settle"]
    assert got["outcome"] == "settled", got
    return vid


# --- the routed case path (real resolver + gateway, el3a catalogue) -----------------------


@asynccontextmanager
async def case_runtime(install: EL3AInstall, *, caps: frozenset[str] = CASE_CAPABILITIES,
                       ) -> AsyncIterator[tuple[Any, Any]]:
    """Yields (EligibilityCaseService over the real gateway, async context factory) for the
    timer workload holding `caps`."""
    import uuid
    from uuid import NAMESPACE_URL, uuid5

    import service_support as sv

    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.eligibility_cases import EligibilityCaseService

    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=3)
    await pool.open()
    try:
        rt = compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"])),
            registry=el3a_registry(), catalog=el3a_catalog(),
            supported_schema_versions=frozenset({EL3A_TARGET}),
            activation_policy=ActivationPolicy.ENFORCE)

        async def context(_tenant: str = "") -> Any:
            return await rt.resolver.resolve(
                principal=Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_TIMER,
                                    auth_method="test",
                                    authorized_tenant_ids=frozenset({install.tenant[0]}),
                                    capabilities=caps),
                tenant_hint=install.tenant[0], purpose="operations", capabilities=caps,
                source=TrustedSource.WORKER,
                execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:el3a:{uuid.uuid4()}"),
                correlation_id=uuid.uuid4(),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

        service = EligibilityCaseService(EventFoundation(rt.gateway,
                                                         producer_service=sa.PRODUCER))
        yield service, context
    finally:
        await pool.close()


def drain(install: EL3AInstall, limit: int = 100) -> dict[str, int]:
    """The routed applier, synchronously (rows are sync; asyncio.run per call, D-G3)."""

    async def go() -> dict[str, int]:
        async with case_runtime(install) as (service, context):
            return dict(await service.drain(await context(), limit=limit))

    return asyncio.run(go())


def apply_one(install: EL3AInstall) -> str:
    async def go() -> str:
        async with case_runtime(install) as (service, context):
            return str(await service.apply_one(await context()))

    return asyncio.run(go())


@asynccontextmanager
async def staff_runtime(install: EL3AInstall) -> AsyncIterator[Any]:
    """The tenant runtime on the E3a registry and catalogue, for the EXISTING staff chain
    (`si_support.StaffChain`: M05Service actions + StaffServiceV3 resolution at contract v2)."""
    import service_support as sv

    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=3)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"])),
            registry=el3a_registry(), catalog=el3a_catalog(),
            supported_schema_versions=frozenset({EL3A_TARGET}),
            activation_policy=ActivationPolicy.ENFORCE)
    finally:
        await pool.close()


@asynccontextmanager
async def worker_runtime(install: EL3AInstall) -> AsyncIterator[tuple[Any, Any]]:
    """The E2 worker path (EligibilityService, timer workload, worker capability) on the E3a
    registry and catalogue (target 121)."""
    import uuid
    from uuid import NAMESPACE_URL, uuid5

    import service_support as sv

    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.eligibility import EligibilityService

    caps = frozenset({e2.WORKER_CAPABILITY})
    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=3)
    await pool.open()
    try:
        rt = compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"])),
            registry=el3a_registry(), catalog=el3a_catalog(),
            supported_schema_versions=frozenset({EL3A_TARGET}),
            activation_policy=ActivationPolicy.ENFORCE)

        async def context(_tenant: str = "") -> Any:
            return await rt.resolver.resolve(
                principal=Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_TIMER,
                                    auth_method="test",
                                    authorized_tenant_ids=frozenset({install.tenant[0]}),
                                    capabilities=caps),
                tenant_hint=install.tenant[0], purpose="operations", capabilities=caps,
                source=TrustedSource.WORKER,
                execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:el3a-w:{uuid.uuid4()}"),
                correlation_id=uuid.uuid4(),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

        yield EligibilityService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER)), \
            context
    finally:
        await pool.close()


def settled_verifications(install: EL3AInstall, refs: list[str]) -> set[str]:
    """INDEPENDENT oracle (Codex red review 1): every settled verification of `refs`, read
    from m05_el_verifications - E1 manual booking owners and E2 settlements alike."""
    got = q(install, "SELECT verification_id::text FROM {s}.m05_el_verifications"
            " WHERE state = 'settled' AND appointment_ref = ANY(%s::uuid[])", (refs,))
    return {str(r[0]) for r in got}


async def legacy_check_item(install: EL3AInstall, rt: Any) -> Any:
    """FIXTURE (E3a-P19 positive control, Codex red review 2): ONE legacy
    `ehr_confirmation_check` item made through the EXISTING path - the M02 append of a
    `m05_writeback_check_raised` observation and `m05.followup.create` - as the dispatch
    workload, in one transaction. Returns the item id."""
    import uuid
    from datetime import UTC, datetime

    import service_support as sv
    from psycopg.types.json import Jsonb

    from haloflow.m02.observation import EventFoundation, Observation
    from haloflow.m05.intake import _attribution

    caps = frozenset({"m02.observation.append", "m05.followup.create"})
    ctx = await sv.context(rt, install, sa.W_DISPATCH, caps, kind="workload")
    foundation = EventFoundation(rt.gateway, producer_service=sa.PRODUCER)
    event, op = uuid.uuid4(), uuid.uuid4()

    async def body(binding: Any) -> Any:
        await foundation.append_observation_in(binding, Observation(
            event_id=event, event_type="m05_writeback_check_raised", contract_version=1,
            correlation_id=op, producer_dedupe_key=f"wbcheck:{op.hex}", links=(),
            metadata={"wb_op_id": str(op), "appointment_ref": str(uuid.uuid4()),
                      "reason": "status_changed"},
            patient_id=uuid.uuid4(), occurred_at=datetime.now(UTC),
            source_time_quality="trusted"))
        rows = await binding.handle.fetch_all("m05.followup.create", {"command": Jsonb({
            "schema_version": 1, "source_event_id": str(event),
            "item_kind": "ehr_confirmation_check", "attribution": _attribution(ctx)})})
        return uuid.UUID(str(rows[0][0]["item_id"]))

    return await foundation.run_in_transaction(ctx, body)
