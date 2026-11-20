"""Eligibility E3b test support (RED BY DESIGN until E3b source exists).

Governing: architecture v1 section 5, v1r1 R6, v1r2 M4, v1r3 C1-C3; stage-3 test cases v1
(E3 rows) + v1r1 T1/T3; E3 sequencing (`claude_e3-scope-v1-2026-11-19.md`); Codex E3b review
prep (`codex_e3b-red-review-prep.md`: P09 zero new items on replay / P10 one new case and
item; C05 forced overlap + durable state); E3b prep pins review (relay ca2b7ae2, F1-F4);
E3b interface pins v1 (`claude_e3b-interface-pins-v1.md`, the revision Codex reviewed is
sha256 eae4c712..., the final v1 file 01d89707...) and v1r1
(`claude_e3b-interface-pins-v1r1.md`, sha256 0fc23115...; Codex ALIGN 74643295).
Synthetic only. E3b builds on E3a (main 309d3ba).

INTERFACE PINS (summary; the two pins documents are authoritative):

Registry / catalogue
- `build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True,
  include_el3b=True)` and the same flags on `build_synthetic_catalog`; `include_el3b`
  without `include_el3a` -> ValueError. Units t122..t125, target 125. Production registry
  {36} and catalogue unchanged. No E3b test profile (the resolution event type is already
  active through RESOLUTION_PROFILE_SQL; contract v3 is a new contract row of that type).
- Statement keys: NO new key. The existing `m05.followup.prepare` / `m05.followup.apply`
  (capabilities unchanged) are re-bound to `m05_prepare_action_v4` / `m05_apply_action_v4`,
  as S-I re-bound them to v3 (correction C-1 to pins v1 section 4, which named new keys).

Schema (t122 `m05_el_staff_tables`)
- `m05_outcome_codes` += the six OD-EL6 codes (labels byte-exact, `el_other` note
  required); `m05_outcome_kinds` (48 rows); `m05_contact_codes` (12 rows, OD-EL7
  byte-exact); `log_contact` in the history `action` CHECK and the action-request
  `requested_action` CHECK; `m05_contact_entries (history_id PK, contact_code)`;
  resolution contract v3 (eight kinds, W1 + OD-EL6 outcomes) and its allow-list;
  `m05_el_cases.closure_outcome`.

Gateways t123 / t124 `m05_prepare_action_v4` / `m05_apply_action_v4`; t125 closes t090/t091
- Legacy kinds behave exactly as v3 (accept / refuse, refusal precedence incl. the pre-lock
  COMMAND_MALFORMED and W1 NOTE_REQUIRED, note rule, history, resolution contract v2/v1).
- OD-EL6 codes: (kind, code) membership under the item lock BEFORE the el_other note rule:
  legacy item + any OD-EL6 code -> COMMAND_MALFORMED; eligibility item + W1 code ->
  COMMAND_MALFORMED; eligibility item + el_other without note -> NOTE_REQUIRED.
- Eligibility kinds: resolve / claim_and_resolve / log_contact carry `case_token`
  {case_id, item_id, case_version} (missing -> COMMAND_MALFORMED). Lock order membership ->
  appointment key -> the ITEM's own case (located from the item, never from the token) ->
  item; relationship re-check, kind/code, standing, then the token. Stale token, or a token
  naming any other case -> CASE_CHANGED, a COMMITTED business response carrying the
  refreshed case view, with the projection committed and no closure side effect.
- Close: item resolved; case closed with closure_outcome; the appointment key's
  closure_watermark = next_seq - 1; resolution observation at contract v3.
- log_contact: eligibility kinds only, open / in_progress items, start_progress standing;
  one history row (state, assignee, outcome unchanged; item version unchanged), one contact
  entry, case_version unchanged; replay returns the stored result with zero new rows.

Service (`haloflow.m05.eligibility_staff`)
- `CaseToken(case_id, item_id, case_version)`, `EligibilityResolveAction(item_id,
  client_action_id, expected_version, type, outcome_code, note, token)`,
  `LogContactAction(item_id, client_action_id, expected_version, contact_code, token)`;
  `StaffServiceV4(gateway, *, foundation, keys)` (extends StaffServiceV3): `act` ->
  `ActionResult(outcome in {applied, replayed, case_changed}, snapshot)`; for case_changed
  the snapshot is the refreshed case view; a token-less `ResolveAction` on an eligibility
  item keeps the E3a P19 refusal (ActionFailed ACTION_RESULT_INVALID); `case(ctx, item_id)`.

Labels: `haloflow.m05.eligibility_labels.OUTCOME_LABELS` / `CONTACT_LABELS`.
Routes (`haloflow.synthetic.staff_routes`): `case` GET /{item_id}/case, `contact` POST
/{item_id}/contact, `resolve` with an optional `case_token` (synthetic-only parsers, F5).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, Final

import el3a_support as e3
import m02_support
import sa_support as sa

EL3B_TARGET: Final = 125
EL3B_SUPPORTED: Final = range(EL3B_TARGET, EL3B_TARGET + 1)
EL3B_UNITS: Final = ("t122_m05_el_staff_tables", "t123_m05_prepare_action_v4",
                     "t124_m05_apply_action_v4", "t125_m05_action_v3_closure")
REBOUND_KEYS: Final = {"m05.followup.prepare": "m05_prepare_action_v4",
                       "m05.followup.apply": "m05_apply_action_v4"}

LEGACY_KINDS: Final = ("no_reply", "reschedule_request", "cancel_request", "unclear_reply",
                       "ehr_confirmation_check")
HISTORICAL_KINDS: Final = LEGACY_KINDS[:4]
EL_KINDS: Final = e3.EL_KINDS
W1_CODES: Final = ("rescheduled", "cancelled_in_ehr", "reached_no_change",
                   "no_contact_left_message", "no_contact_no_answer", "other")

# OD-EL6 (Rachel, 2026-11-18), byte-exact; el_other requires a note.
OUTCOME_LABELS: Final = {
    "el_verified_active": "Verified active by phone or payer portal",
    "el_confirmed_inactive": "Confirmed not active",
    "el_insurance_updated": "Insurance updated in EHR",
    "el_self_pay": "Patient will self-pay",
    "el_appt_cancelled": "No action – appointment cancelled",
    "el_other": "Other",
}
NOTE_REQUIRED_CODES: Final = frozenset({"el_other"})

# OD-EL7 (Rachel, 2026-11-18), byte-exact; architecture v1r2 M4 codes.
CONTACT_LABELS: Final = {
    "el_left_message": "Left message for patient",
    "el_called_no_answer": "Called patient – no answer",
    "el_spoke_followup": "Spoke with patient – follow-up needed",
    "el_payer_awaiting": "Contacted payer – awaiting answer",
}


def el3b_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True,
                                    include_el3b=True)


def el3b_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(include_el1=True, include_el2=True, include_el3a=True,
                                   include_el3b=True)


class EL3BInstall(e3.EL3AInstall):
    """A synthetic tenant at target 125 (E3a + E3b), provisioned in the row, with the same
    test profiles as E3a."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                import sh2_support as sh2
                import si_support as si
                from staff_support import staff_manifest

                from haloflow.synthetic.composition import RESOLUTION_PROFILE_SQL

                m02_support.provision_sync(self.ids, el3b_registry(), self.tenant,
                                           supported=EL3B_SUPPORTED,
                                           manifest=staff_manifest())
                sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                sa.admin(self, si.si_test_profile_sql().format(schema=self.schema))
                sa.admin(self, RESOLUTION_PROFILE_SQL.format(schema=self.schema))
                sa.admin(self, e3.el3a_profile_sql().format(schema=self.schema))
                sh2.settings_raw(self, clinic_display_name=sh2.CLINIC, clinic_tz=e3.NY)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"E3b test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[EL3BInstall]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield EL3BInstall(ids, tenant)
    finally:
        reset(tenant)


@asynccontextmanager
async def staff_runtime(install: e3.EL3AInstall, *, el3b: bool = True) -> AsyncIterator[Any]:
    """The tenant runtime for the staff chain: the E3b registry and catalogue (target 125),
    or with `el3b=False` the E3a ones (target 121) for the v3 parity oracle."""
    import service_support as sv

    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    registry, catalog = ((el3b_registry(), el3b_catalog()) if el3b
                         else (e3.el3a_registry(), e3.el3a_catalog()))
    target = EL3B_TARGET if el3b else e3.EL3A_TARGET
    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=3)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"])),
            registry=registry, catalog=catalog, supported_schema_versions=frozenset({target}),
            activation_policy=ActivationPolicy.ENFORCE)
    finally:
        await pool.close()


class Staff:
    """The staff chain over StaffServiceV4 (E3b) - or StaffServiceV3 for the parity oracle."""

    def __init__(self, install: Any, rt: Any, *, v4: bool = True) -> None:
        import sd_support as sd
        import service_support as sv

        from haloflow.m02.observation import EventFoundation

        self.install, self.rt = install, rt
        self.chain = sd.Chain(install, rt)
        foundation = EventFoundation(rt.gateway, producer_service=sv.PRODUCER)
        if v4:
            from haloflow.m05.eligibility_staff import StaffServiceV4

            self.chain.staff = StaffServiceV4(rt.gateway, foundation=foundation,
                                              keys=self.chain.keys)
        else:
            from haloflow.m05.writeback import StaffServiceV3

            self.chain.staff = StaffServiceV3(rt.gateway, foundation=foundation,
                                              keys=self.chain.keys)

    async def actor(self, caps: frozenset[str] | None = None) -> Any:
        return await (self.chain.actor() if caps is None else self.chain.actor(caps))

    async def claim(self, item: uuid.UUID, version: int = 1) -> Any:
        return await self.chain.claim(item, version=version)

    async def case(self, item: uuid.UUID) -> Any:
        return await self.chain.staff.case(await self.actor(), item)

    async def token(self, item: uuid.UUID) -> Any:
        from haloflow.m05.eligibility_staff import CaseToken

        view = await self.case(item)
        return CaseToken(case_id=uuid.UUID(str(view["case_id"])), item_id=item,
                         case_version=int(view["case_version"]))

    async def resolve(self, item: uuid.UUID, version: int, outcome: str, token: Any, *,
                      kind: str = "resolve", note: str | None = None,
                      key: uuid.UUID | None = None) -> Any:
        from haloflow.m05.eligibility_staff import EligibilityResolveAction

        return await self.chain.staff.act(await self.actor(), EligibilityResolveAction(
            item_id=item, client_action_id=key or uuid.uuid4(), expected_version=version,
            type=kind, outcome_code=outcome, note=note, token=token))

    async def legacy_resolve(self, item: uuid.UUID, version: int, outcome: str, *,
                             kind: str = "resolve", note: str | None = None,
                             key: uuid.UUID | None = None) -> Any:
        from haloflow.m05.service import ResolveAction

        return await self.chain.staff.act(await self.actor(), ResolveAction(
            item_id=item, client_action_id=key or uuid.uuid4(), expected_version=version,
            type=kind, outcome_code=outcome, note=note))

    async def contact(self, item: uuid.UUID, version: int, code: str, token: Any, *,
                      key: uuid.UUID | None = None, caps: frozenset[str] | None = None) -> Any:
        from haloflow.m05.eligibility_staff import LogContactAction

        return await self.chain.staff.act(await self.actor(caps), LogContactAction(
            item_id=item, client_action_id=key or uuid.uuid4(), expected_version=version,
            contact_code=code, token=token))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- observation helpers (admin connection) -----------------------------------------------

rows = e3.rows
count = e3.count
q = e3.q
cases = e3.cases


def item(install: e3.EL3AInstall, item_id: str) -> dict[str, Any]:
    (row,) = rows(install, "m05_followup_items", "item_id = %s::uuid", (item_id,))
    return row


def key_row(install: e3.EL3AInstall, ref: str) -> dict[str, Any]:
    (row,) = rows(install, "m05_el_appointments", "appointment_ref = %s::uuid", (ref,))
    return row


def contact_entries(install: e3.EL3AInstall, item_id: str) -> list[dict[str, Any]]:
    got = q(install, "SELECT c.contact_code FROM {s}.m05_contact_entries c"
            " JOIN {s}.m05_followup_history h ON h.history_id = c.history_id"
            " WHERE h.item_id = %s::uuid ORDER BY h.created_at", (item_id,))
    return [str(r[0]) for r in got]


def side_effects(install: e3.EL3AInstall, ref: str, item_id: str) -> tuple[Any, ...]:
    """Every closure / contact side effect of one eligibility item (C1: none on
    CASE_CHANGED): item state, assignee, outcome, version; case rows; the watermark; history
    count; resolution events; contact entries."""
    it = item(install, item_id)
    return ((it["state"], it["assignee_id"], it["outcome_code"], it["version"]),
            [(c["case_id"], c["state"], c.get("closure_outcome")) for c in cases(install, ref)],
            key_row(install, ref).get("closure_watermark"),
            count(install, "m05_followup_history", "item_id = %s", (item_id,)),
            count(install, "patient_events",
                  "event_type = 'm05_followup_resolution_asserted'"),
            contact_entries(install, item_id))


# --- lock evidence seams (Codex tranche-1 review, C04) ------------------------------------

class Hold:
    """Pause the tenant transaction of the thread named `thread` right AFTER statement `key`
    returns (its locks still held) until `release` is set."""

    def __init__(self) -> None:
        import threading

        self.held = threading.Event()
        self.release = threading.Event()


def hold_after(monkeypatch: Any, key: str, thread: str) -> Hold:
    import threading

    from haloflow.m01.gateway import TenantRepositoryHandle

    hold = Hold()
    for name in ("execute", "fetch_one", "fetch_all"):
        original = getattr(TenantRepositoryHandle, name)

        def make(method: Any) -> Any:
            async def paused(self: Any, statement_key: str, params: Any = None) -> Any:
                result = await method(self, statement_key, params)
                if (statement_key == key and threading.current_thread().name == thread
                        and not hold.held.is_set()):
                    hold.held.set()
                    released = await asyncio.get_running_loop().run_in_executor(
                        None, hold.release.wait, 60)
                    assert released, "the hold was never released"
                return result

            return paused

        monkeypatch.setattr(TenantRepositoryHandle, name, make(original))
    return hold


def lockable(install: Any, table: str, column: str, value: str) -> bool:
    """From a SEPARATE admin connection: can the row be locked right now (FOR UPDATE NOWAIT)?
    False = another transaction holds it."""
    import psycopg
    from psycopg import errors, sql

    with psycopg.connect(install.ids.admin) as conn:
        try:
            conn.execute(sql.SQL("SELECT 1 FROM {}.{} WHERE {} = %s FOR UPDATE NOWAIT").format(
                sql.Identifier(install.schema), sql.Identifier(table),
                sql.Identifier(column)), (value,))
        except errors.LockNotAvailable:
            conn.rollback()
            return False
        conn.rollback()
        return True


async def legacy_item(install: Any, rt: Any, kind: str) -> uuid.UUID:
    """FIXTURE (P01, the four reminder kinds): one item of `kind` made through the EXISTING
    path - the M02 append of the kind's source observation (`m05_reminder_reply_received`
    with triage_kind, or `m05_reminder_no_reply`) by its mapped producer workload, then
    `m05.followup.create`, in one transaction. Avoids `m05_directory_put` (closed to runtime
    on this install; E3a green correction G-1). Returns the item id."""
    from datetime import UTC, datetime

    import service_support as sv

    from haloflow.m02.observation import EventFoundation, Observation
    from haloflow.m05.intake import _attribution

    if kind == "ehr_confirmation_check":
        return uuid.UUID(str(await e3.legacy_check_item(install, rt)))
    reply = kind != "no_reply"
    workload = sa.W_INGRESS if reply else sa.W_TIMER
    caps = frozenset({"m02.observation.append", "m05.followup.create"})
    ctx = await sv.context(rt, install, workload, caps, kind="workload")
    foundation = EventFoundation(rt.gateway, producer_service=sa.PRODUCER)
    event, inbound, reminder_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ref, reminder = str(uuid.uuid4()), str(reminder_id)
    # the intake service's own correlation and dedupe-key shapes (intake.py)
    correlation, dedupe = ((inbound, f"reply:{inbound.hex}") if reply
                           else (reminder_id, f"noreply:{reminder_id.hex}"))
    if reply:
        reply_class = {"reschedule_request": "reschedule", "cancel_request": "cancel",
                       "unclear_reply": "unclear"}[kind]
        event_type = "m05_reminder_reply_received"
        metadata: dict[str, Any] = {"inbound_id": str(inbound), "reply_class": reply_class,
                                    "triage_kind": kind, "appointment_ref": ref,
                                    "reminder_id": reminder}
    else:
        event_type = "m05_reminder_no_reply"
        metadata = {"reminder_id": reminder, "appointment_ref": ref, "triage_kind": "no_reply"}

    async def body(binding: Any) -> Any:
        from psycopg.types.json import Jsonb

        await foundation.append_observation_in(binding, Observation(
            event_id=event, event_type=event_type, contract_version=1,
            correlation_id=correlation, producer_dedupe_key=dedupe, links=(),
            metadata=metadata, patient_id=uuid.uuid4(), occurred_at=datetime.now(UTC),
            source_time_quality="trusted"))
        rows = await binding.handle.fetch_all("m05.followup.create", {"command": Jsonb({
            "schema_version": 1, "source_event_id": str(event), "item_kind": kind,
            "attribution": _attribution(ctx)})})
        return uuid.UUID(str(rows[0][0]["item_id"]))

    result: uuid.UUID = await foundation.run_in_transaction(ctx, body)
    return result
