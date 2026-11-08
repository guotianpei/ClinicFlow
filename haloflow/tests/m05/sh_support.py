"""L-1S package S-H, checkpoint H1 (send/recovery contract): red-test support.

Governing: decision table v1r2 (claude_sh-investigation-decision-table-and-contract-v1r2,
sha256 ca925689...), Codex ALIGN with pins 1-6 (codex_sh-v1r2-h1-test-preparation-
disposition.md), H1 packet v1 (04fdcaca...) as corrected by codex_sh-h1-packet-v1-review.md
(2026-11-08). Synthetic data only; fake sender only; TEST registry only; not composed.
Every import of S-H code is deferred into the row body (class-B red rule).

Interface pins (asserted by the rows; the red package note discloses them):
- `haloflow.m05.outbound_units`: SH_TENANT_MIGRATIONS (t047..t056, sequential, none
  omitted), SH_INSTALLED_STATE_PROFILES and SH_ACL_AMENDMENTS. Target 56 (Codex ALIGN
  20261108T145700Z, versioned ingress): t053 is a NEW typed function
  `m05_register_inbound_v2` (t040's command/result/codes plus the D3 guard); t054 is a
  remove-only `acl_closure_v1` unit (run as the M05 owner) whose single AclAmendment
  removes haloflow_runtime EXECUTE from the UNCHANGED t040 `m05_register_inbound`; t055
  `m05_outbound_read`; t056 `m05_outbound_held`. No accepted unit is rewritten.
- Catalogue: the S-H TEST catalogue binds the EXISTING key `m05.intake.inbound` to
  `SELECT m05_register_inbound_v2(%(command)s)` (outbound_statements.SH_INBOUND_STATEMENTS)
  and drops only that key from the S-A input mapping, so IntakeService/ReplyIntake are
  unchanged callers that now reach v2. Production registry/catalogue/supported set {36}
  untouched.
- SQL functions (runtime-executable, jsonb -> jsonb, P0001 closed codes):
  m05_outbound_prepare (t048), m05_outbound_claim (t049), m05_outbound_outcome (t050),
  m05_outbound_expire (t051), m05_register_outbound (t052), m05_register_inbound_v2
  (t053), m05_outbound_read (t055), m05_outbound_held (t056).
- No close_correlation in H1 (Codex review point 2): potentially-sent ambiguity stays
  open and inspectable; only a definite refused/never-sent op never opens it.
- `haloflow.m05.outbound_statements`: SH_STATEMENTS, SH_PROTECTED (prepare, claim,
  register). Outcome, expire, read and held are NOT protected (D5 bookkeeping).
- `haloflow.m05.outbound`: OutboundService, Accepted / Refused / Uncertain, FakeSender,
  OutboundWorker(service, sender, render).run_one(context, op_id, fence) and
  .recover(context, op_id) (registration only; never claims or sends).
- Principal (disclosed, Codex review 9e14672 item 2): NO new workload role. The outbound
  statements run as the existing dispatch workload W_DISPATCH with exactly
  sa.CAPS["dispatch"]; OutboundService refuses any other capability set with
  OutboundRefused("CAPABILITIES_INVALID") before I/O, and the SQL functions refuse any
  other workload with WORKLOAD_NOT_AUTHORIZED (activation exemption is never an
  authorization exemption).
- Stable identity: OutboundService.prepare(context, ref, digest, *, op_id, reminder_id) —
  the caller supplies op_id and reminder_id so a lost-ack retry converges.
- Codes: OP_CONFLICT, PREPARE_UNROUTABLE, CONTACT_OPTED_OUT, CLAIM_STALE, STALE_ATTEMPT,
  OUTCOME_CONFLICT, STATE_TERMINAL, NOT_ACCEPTED, COMMAND_MALFORMED; D1 codes in order
  IDENTITY_CHANGED, APPOINTMENT_MISSING, APPOINTMENT_CHANGED, START_PASSED, OPTED_OUT;
  LEASE_EXPIRED; worker code ACCEPTED_EVIDENCE_INVALID.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

import m02_support
import sa_support as sa
import sb_support as sb
import sc_support as sc

SH_UNIT_IDS: Final = (
    "t047_m05_outbound_ops", "t048_m05_outbound_prepare", "t049_m05_outbound_claim",
    "t050_m05_outbound_outcome", "t051_m05_outbound_expire", "t052_m05_register_outbound",
    "t053_m05_register_inbound_v2", "t054_m05_inbound_v1_runtime_closure",
    "t055_m05_outbound_read", "t056_m05_outbound_held",
)
SH_TARGET: Final = 56
V1_INBOUND_UNIT: Final = "t040_m05_register_inbound"
V2_INBOUND: Final = "m05_register_inbound_v2"
SH_SUPPORTED: Final = range(SH_TARGET, SH_TARGET + 1)
STATES: Final = frozenset({"prepared", "attempting", "accepted", "refused", "uncertain",
                           "registered", "accepted_unregistered", "abandoned"})
OPEN_STATES: Final = ("attempting", "accepted", "uncertain", "accepted_unregistered")
D1_CODES: Final = ("IDENTITY_CHANGED", "APPOINTMENT_MISSING", "APPOINTMENT_CHANGED",
                   "START_PASSED", "OPTED_OUT")
HELD_COLUMNS: Final = frozenset({"op_id", "appointment_ref", "state", "code", "created_at",
                                 "updated_at"})
TZ: Final = "America/New_York"


# --- registry, catalogue, install ---------------------------------------------------------


def sh_units() -> Any:
    from haloflow.m05.outbound_units import SH_TENANT_MIGRATIONS

    return SH_TENANT_MIGRATIONS


def sh_profiles() -> Any:
    from haloflow.m05.outbound_units import SH_INSTALLED_STATE_PROFILES

    return SH_INSTALLED_STATE_PROFILES


def sh_amendments() -> Any:
    from haloflow.m05.outbound_units import SH_ACL_AMENDMENTS

    return SH_ACL_AMENDMENTS


def sh_registry() -> Any:
    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    return build_tenant_migration_registry(
        *APPROVED_TENANT_MIGRATIONS, sa.sa_units(), sb.sb_units(), sc.sc_units(), sh_units(),
        approved_execution_roles=APPROVED_EXECUTION_ROLES,
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES, **sa.sa_profiles(),
                                  **sb.sb_profiles(), **sc.sc_profiles(), **sh_profiles()},
        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *sh_amendments()),
    )


def sh_statements() -> Any:
    from haloflow.m05.outbound_statements import SH_STATEMENTS

    return SH_STATEMENTS


def sh_inbound_statements() -> Any:
    from haloflow.m05.outbound_statements import SH_INBOUND_STATEMENTS

    return SH_INBOUND_STATEMENTS


def sa_statements_without_inbound() -> Any:
    from types import MappingProxyType

    from haloflow.m05.intake_statements import INBOUND_KEY

    return MappingProxyType({k: v for k, v in sa.sa_statements().items() if k != INBOUND_KEY})


def protected() -> frozenset[str]:
    from haloflow.m05.outbound_statements import SH_PROTECTED

    return frozenset(sc.protected() | SH_PROTECTED)


def sh_catalog() -> Any:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import build_statement_catalog

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, sa_statements_without_inbound(),
                                   sb.sb_statements(), sh_statements(), sh_inbound_statements(),
                                   activation_protected=protected())


class SHInstall(sa.SAInstall):
    """The S-A install shape, provisioned through the S-H registry (target 56)."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, sh_registry(), self.tenant,
                                           supported=SH_SUPPORTED, manifest=staff_manifest())
                if self.profile:
                    sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"S-H test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[SHInstall]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield SHInstall(ids, tenant)
    finally:
        reset(tenant)


@asynccontextmanager
async def runtime(install: SHInstall, *, policy: str = "ENFORCE") -> AsyncIterator[Any]:
    """An S-H TEST runtime (target 56, S-H catalogue, real resolver and gateway)."""

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
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=sh_registry(),
            catalog=sh_catalog(),
            supported_schema_versions=frozenset({SH_TARGET}),
            activation_policy=ActivationPolicy[policy],
        )
    finally:
        await pool.close()


# --- fixed H1 template, bound to the frozen snapshot (Codex point 6) -----------------------


def render(first_name: str, start_utc: datetime, tz: str = TZ) -> str:
    """The fixed H1 test template (Q-B4 shape; clinic name fixed). H2 owns real rendering."""

    local = start_utc.astimezone(ZoneInfo(tz))
    hour = local.strftime("%I").lstrip("0") or "12"
    when = f"{local:%A, %b %d} at {hour}:{local:%M %p}"
    return (f"Hi {first_name}, this is HaloVox Test Clinic. You have an appointment on "
            f"{when}. Reply 1 to confirm, 2 to reschedule, or 3 to cancel. "
            "Reply STOP to opt out.")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


FIRST_NAME: Final = "Synth"


def start_of(install: sa.SAInstall, ref: str) -> datetime:
    rows = sa.admin(install, sa.q(install, "SELECT start_at FROM {s}.m05_appointments"
                                           " WHERE appointment_ref = %s"), (ref,))
    value: datetime = rows[0][0]
    return value


# --- SQL helpers (runtime role, dispatch workload principal) --------------------------------


def _attr() -> dict[str, Any]:
    return sa.attribution(sa.W_DISPATCH)


def prepare_cmd(op_id: str, ref: str, reminder: str, digest: str) -> dict[str, Any]:
    return {"schema_version": 1, "op_id": op_id, "appointment_ref": ref,
            "reminder_id": reminder, "text_sha256": digest, "attribution": _attr()}


def claim_cmd(op_id: str, fence: int, lease_seconds: int = 120) -> dict[str, Any]:
    return {"schema_version": 1, "op_id": op_id, "fence": fence,
            "lease_seconds": lease_seconds, "attribution": _attr()}


def outcome_cmd(op_id: str, attempt: str, outcome: str, *, provider_ref: str | None = None,
                code: str | None = None) -> dict[str, Any]:
    cmd: dict[str, Any] = {"schema_version": 1, "op_id": op_id, "attempt_id": attempt,
                           "outcome": outcome, "attribution": _attr()}
    if provider_ref is not None:
        cmd["provider_ref"] = provider_ref
    if code is not None:
        cmd["code"] = code
    return cmd


def register_cmd(op_id: str) -> dict[str, Any]:
    return {"schema_version": 1, "op_id": op_id, "attribution": _attr()}


def prepare(install: sa.SAInstall, ref: str, *, op_id: str | None = None,
            reminder: str | None = None, digest: str | None = None) -> dict[str, Any]:
    digest = digest or text_hash(render(FIRST_NAME, start_of(install, ref)))
    return sa.call(install, "m05_outbound_prepare",
                   prepare_cmd(op_id or sa.new_id(), ref, reminder or sa.new_id(), digest))


def claim(install: sa.SAInstall, op_id: str, fence: int, lease_seconds: int = 120
          ) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_claim", claim_cmd(op_id, fence, lease_seconds))


def outcome(install: sa.SAInstall, op_id: str, attempt: str, result: str, **kw: Any
            ) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_outcome", outcome_cmd(op_id, attempt, result, **kw))


def register(install: sa.SAInstall, op_id: str) -> dict[str, Any]:
    return sa.call(install, "m05_register_outbound", register_cmd(op_id))


def expire(install: sa.SAInstall, limit: int = 100) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_expire",
                   {"schema_version": 1, "limit": limit, "attribution": _attr()})


def read(install: sa.SAInstall, op_id: str, attempt: str) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_read",
                   {"schema_version": 1, "op_id": op_id, "attempt_id": attempt,
                    "attribution": _attr()})


def held_ops(install: sa.SAInstall, limit: int = 100) -> dict[str, Any]:
    return sa.call(install, "m05_outbound_held",
                   {"schema_version": 1, "limit": limit, "attribution": _attr()})


def op_row(install: sa.SAInstall, op_id: str) -> dict[str, Any]:
    rows = sa.admin(install, sa.q(install, "SELECT to_jsonb(o) FROM {s}.m05_outbound_ops AS o"
                                           " WHERE op_id = %s"), (op_id,))
    assert len(rows) == 1, rows
    value: dict[str, Any] = rows[0][0]
    return value


def ops_for(install: sa.SAInstall, ref: str) -> int:
    rows = sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.m05_outbound_ops"
                                           " WHERE appointment_ref = %s"), (ref,))
    count: int = rows[0][0]
    return count


def windows_for(install: sa.SAInstall, ref: str) -> list[dict[str, Any]]:
    rows = sa.admin(install, sa.q(install, "SELECT to_jsonb(w) FROM {s}.m05_reminder_windows"
                                           " AS w WHERE appointment_ref = %s"), (ref,))
    return [r[0] for r in rows]


def to_accepted(install: sa.SAInstall, ref: str) -> tuple[str, str]:
    """FIXTURE path through the real statements: prepare -> claim -> accepted."""

    op = prepare(install, ref)
    claimed = claim(install, op["op_id"], op["fence"])
    assert claimed["outcome"] == "claimed", claimed
    outcome(install, op["op_id"], claimed["attempt_id"], "accepted",
            provider_ref="fake-" + uuid.uuid4().hex[:12])
    return op["op_id"], claimed["attempt_id"]


def to_state(install: sa.SAInstall, ref: str, state: str) -> str:
    """Drive a fresh op on `ref` into one of the open states (or refused/abandoned)."""

    op = prepare(install, ref)
    if state == "abandoned":
        sa.directory(install, "appointment_put", appointment_ref=ref,
                     patient_id=op_row(install, op["op_id"])["patient_id"],
                     start_at=sa.FUTURE.format(n=8), status="booked")  # start moved
        assert claim(install, op["op_id"], op["fence"])["outcome"] == "abandoned"
        return str(op["op_id"])
    claimed = claim(install, op["op_id"], op["fence"])
    attempt = claimed["attempt_id"]
    if state == "attempting":
        return str(op["op_id"])
    if state == "refused":
        outcome(install, op["op_id"], attempt, "refused", code="FAKE_REFUSED")
    elif state == "uncertain":
        outcome(install, op["op_id"], attempt, "uncertain", code="FAKE_TIMEOUT")
    elif state in ("accepted", "accepted_unregistered"):
        outcome(install, op["op_id"], attempt, "accepted", provider_ref="fake-1")
        if state == "accepted_unregistered":
            sa.directory(install, "appointment_put", appointment_ref=ref,
                         patient_id=op_row(install, op["op_id"])["patient_id"],
                         start_at=sa.FUTURE.format(n=8), status="booked")
            assert register(install, op["op_id"])["outcome"] == "held"
    else:
        raise AssertionError(state)
    assert op_row(install, op["op_id"])["state"] == state
    return str(op["op_id"])


def op_row_by_ref(install: sa.SAInstall, ref: str) -> str:
    rows = sa.admin(install, sa.q(install, "SELECT op_id FROM {s}.m05_outbound_ops"
                                           " WHERE appointment_ref = %s"), (ref,))
    assert len(rows) == 1, rows
    return str(rows[0][0])


def frozen_start(install: sa.SAInstall, op_id: str) -> datetime:
    rows = sa.admin(install, sa.q(install, "SELECT start_at FROM {s}.m05_outbound_ops"
                                           " WHERE op_id = %s"), (op_id,))
    value: datetime = rows[0][0]
    return value


def seed_same_ids(install: sa.SAInstall, patient: str, phone: str, refs: list[str],
                  start_n: int = 1) -> None:
    """Directory rows with CALLER-CHOSEN ids, so two tenants can hold identical ids."""

    sa.directory(install, "contact_put", phone=phone, patient_id=patient)
    for i, ref in enumerate(refs):
        sa.directory(install, "appointment_put", appointment_ref=ref, patient_id=patient,
                     start_at=sa.FUTURE.format(n=start_n + i), status="booked")


def inbound(install: sa.SAInstall, message_id: str, phone: str, reply_class: str
            ) -> dict[str, Any]:
    """Direct SQL inbound on an S-H tenant: v2 (v1 is closed to the runtime by t054)."""

    return sa.call(install, V2_INBOUND, sa.inbound_cmd(message_id, phone, reply_class))


def inbound_any(install: sa.SAInstall, message_id: str, phone: str, reply_class: str
                ) -> dict[str, Any]:
    """v2 on an S-H tenant, v1 on any other (the regression matrix)."""

    if isinstance(install, SHInstall):
        return inbound(install, message_id, phone, reply_class)
    return sa.inbound(install, message_id, phone, reply_class)
