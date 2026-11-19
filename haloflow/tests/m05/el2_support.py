"""Eligibility E2 test support (RED BY DESIGN until E2 source exists).

Governing: eligibility requirements v1r1 (EL-5, EL-6a..6f, EL-7, EL-8, EL-9, EL-14, EL-14a,
EL-15); architecture v1, v1r1 (R1 dispatch/recovery, R2 triggers/revalidation, R4 results/
expiry), v1r2 amendment (M1 trigger termination, M5 worked transitions), v1r3 correction (C2
bound, slots vs calls); stage-3 test cases v1 (E2 rows) + v1r1 T3; owner decisions OD-EL1,
OD-EL8, OD-EL10. Synthetic only. E2 builds on E1 (main 8fedd71).

INTERFACE PINS (E2 red packet v1, section 2) - what the E2 source must provide:

Registry / catalogue
- `build_synthetic_registry(include_el1=True, include_el2=True)` and
  `build_synthetic_catalog(include_el1=True, include_el2=True)`; `include_el2` without
  `include_el1` is refused (ValueError). E2 units t106..t117, target **117** (a green deviation
  in the count is disclosed, as E1 did). Production registry {36} and catalogue unchanged.

Schema (migrator unit t106; owner `haloflow_m05_owner`; no runtime table privilege)
- `m05_el_verifications` gains: `epoch integer NOT NULL DEFAULT 0`, `lease_until timestamptz`,
  `patient_id uuid`, `payer_id varchar(64)`, `member_digest char(64)` (64 lowercase hex; never
  the member id; NULL after expiry), `service_date date` (clinic-local, from the key row zone),
  `observed_at timestamptz` (the settling answer's observation time; NULL without one).
  The freshness key columns are written when the verification is created.
- `m05_el_triggers` gains `returned boolean NOT NULL DEFAULT false` (M1: returned at most once).
- New `m05_el_attempts (attempt_id uuid PK, verification_id, n smallint CHECK 1..2, epoch,
  state CHECK IN ('dispatched','answered','timed_out','ambiguous'), dispatched_at timestamptz
  NOT NULL, finished_at timestamptz, answer jsonb)`, UNIQUE (verification_id, n). An attempt
  row is committed as `dispatched` BEFORE the port call (R1: no "unsent" state).
- New `m05_el_late_answers (late_id uuid PK, attempt_id uuid, verification_id uuid NULL,
  cause CHECK IN ('unknown_attempt','closed_attempt','superseded','stale_epoch','mismatch'),
  class varchar(16) NULL, received_at timestamptz NOT NULL)` - ids, class, cause, time only.
- `m05_el_case_events` gains `verification_id uuid NULL` with a UNIQUE index (v1r1, Codex D-3):
  every settlement writes exactly ONE event carrying its verification id, so a settlement has a
  durable identity and a replay source for E3's single M02 "verification settled" event
  (including settlements made before E3 exists). E1's intake events keep it NULL.
- `member_digest` (v1r1, Codex finding 2) = SHA-256 hex over (appointment_ref, member id):
  stable for one appointment and member, different for the same member on another appointment.
- `m05_el_case_events.reason` CHECK extended with the payer reasons (OD-EL8 codes):
  `coverage_active` (active; no staff text), `coverage_inactive`, `subscriber_not_found`,
  `name_dob_mismatch`, `member_id_not_recognized`, `service_type_unsupported`,
  `payer_unavailable`, `response_unreadable` (E1's seven stay).

Owner-only IMMUTABLE typed seams (E1 `m05_el_deadline_passed` pattern; no runtime EXECUTE)
- `m05_el_retry_ready(p_failed_at timestamptz, p_now timestamptz) boolean`
  = p_now >= p_failed_at + 120 s (RETRY_DELAY_S).
- `m05_el_takeover_ready(p_lease_until timestamptz, p_now timestamptz) boolean`
  = p_now >= p_lease_until + 30 s (TAKEOVER_MARGIN_S). The lease is LEASE_S = 120 s.
- `m05_el_fresh(p_observed timestamptz, p_window_start timestamptz) boolean`
  = p_observed >= p_window_start (observed_at, never settled_at).
- `m05_el_expired(p_service_date date, p_today date) boolean`
  = p_today > p_service_date + 30 (EXPIRY_DAYS; "after expires_after").

Gateways (jsonb, runtime EXECUTE, timer workload attribution, `schema_version` 1; every key
set is closed -> COMMAND_MALFORMED otherwise). Results are `{"schema_version": 1, ...}`.
- `m05_el_due({limit})` -> `{"outcome": "applied", "created", "attached", "skipped_fresh",
  "verifications": [uuid text, ...]}`. Turns DUE triggers (status pending, due through
  `m05_el_deadline_passed`) of booked, enrolled, eligibility-enabled appointments into work,
  per appointment in the fixed order booking, previsit, dayof:
  (a) a settled `active` verification whose stored key equals the current key (appointment,
  patient, payer, member digest, coverage version, service date) and whose observed_at is
  fresh for the trigger's window -> trigger `skipped_fresh` (previsit/dayof only);
  (b) else an unsettled verification of the same (appointment, coverage version) exists ->
  trigger `assigned` to it (attached);
  (c) else a new verification owned by this trigger (state `due`) -> trigger `assigned`.
  Window starts: previsit = its due instant (R5); dayof = 00:00 clinic-local on the service
  date. `verifications` lists (at most `limit`) the ids that may be claimed now: state `due`
  with no retry delay pending, or `claimed` with `m05_el_takeover_ready` true.
- `m05_el_claim({verification_id})` -> `{"outcome": "claimed", "epoch": e, "takeover": bool}`
  | `{"outcome": "not_claimable"}`. FOR UPDATE SKIP LOCKED; `due` or a takeover-ready
  `claimed` -> `claimed`, epoch + 1, lease_until = now + 120 s. A takeover marks every
  `dispatched` attempt of the verification `ambiguous` (slot consumed, never re-sent).
- `m05_el_dispatch({verification_id, epoch})` -> fenced on (verification_id, epoch):
  `fenced` | `superseded` | `cancelled` | `activation_off` (state back to `due`, no slot) |
  `answered` (an answered attempt exists: settle it) | `in_flight` | `exhausted` (two failed
  attempts: settle) | `retry_not_ready` (state back to `due`, lease cleared) |
  `{"outcome": "dispatched", "attempt_id", "n", "request": {...}}`. The request carries the
  correlation (attempt_id, tenant_id, appointment_ref, patient_id, coverage_version) and the
  coverage fields the port needs (payer_id, member_id, relationship, patient_name,
  patient_dob, subscriber_name, subscriber_dob, subscriber_member_id, service_date).
- `m05_el_record_answer({attempt_id, epoch, echo, result})` -> `{"outcome": "recorded"}` |
  `{"outcome": "late", "cause": c}`; `echo` = {attempt_id, tenant_id, appointment_ref,
  patient_id, member_id, coverage_version}; `result` = the normalized answer (below). Late
  (stored in m05_el_late_answers, nothing else changes): unknown attempt; attempt not
  `dispatched`; verification superseded/cancelled; stale epoch; any echo mismatch. A
  malformed `result` (v1r1, Codex finding 3) is COMMAND_MALFORMED and writes nothing: keys
  outside the normalized set, class outside the three, a reason not of its class
  (active -> coverage_active; inactive -> coverage_inactive; inconclusive -> the six), a
  missing or non-ISO/naive observed_at (it may be absent ONLY for response_unreadable), or a
  field of the wrong JSON type.
- `m05_el_timeout({attempt_id, epoch})` -> `timed_out` | `fenced` | `not_dispatched`.
- `m05_el_settle({verification_id, epoch})` -> fenced; revalidates like dispatch; settles from
  the stored answer (or `inconclusive`/`payer_unavailable` after two failed attempts) ->
  `{"outcome": "settled", "class", "reason"}` | `not_settleable` | the refusals above. One
  case event per settlement (kind `open_or_note`, the class and reason) under the key-row
  lock with the next seq. Triggers terminate per M1.
- `m05_el_expire({limit})` -> `{"outcome": "applied", "expired": n}`: appointments whose
  service date is past `m05_el_expired`: coverage identifiers (member_id, names, dobs,
  subscriber fields) NULL and the coverage block reduced to `{"state": s}`; attempt answers
  keep only class, reason, observed_at; verification member_digest NULL. Class, reason,
  timestamps and ids remain. Idempotent.

Normalized answer (`haloflow.m05.eligibility_results.normalize`): keys from the closed set
{class, reason, observed_at, source, payer_name, plan, copay, deductible_total,
deductible_remaining, coinsurance, referral_required, auth_required, txn_ref}; unknown or
absent fields stay absent; `class` in {active, inactive, inconclusive}.

Statement keys (catalogue, capability `m05.eligibility.worker`): `m05.eligibility.due`,
`.claim`, `.dispatch`, `.answer`, `.timeout`, `.settle`, `.expire`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import el1_support as el
import m02_support
import sa_support as sa
from psycopg import sql

EL2_TARGET: Final = 117
EL2_SUPPORTED: Final = range(EL2_TARGET, EL2_TARGET + 1)
NY: Final = el.NY
PRIORITY: Final = el.PRIORITY

RETRY_DELAY_S: Final = 120
LEASE_S: Final = 120
TAKEOVER_MARGIN_S: Final = 30
EXPIRY_DAYS: Final = 30

EL2_TABLES: Final = (*el.EL_TABLES, "m05_el_attempts", "m05_el_late_answers")
GATEWAYS: Final = ("m05_el_due", "m05_el_claim", "m05_el_dispatch", "m05_el_record_answer",
                   "m05_el_timeout", "m05_el_settle", "m05_el_expire")
SEAMS: Final = ("m05_el_retry_ready", "m05_el_takeover_ready", "m05_el_fresh",
                "m05_el_expired")
STATEMENT_KEYS: Final = ("m05.eligibility.due", "m05.eligibility.claim",
                         "m05.eligibility.dispatch", "m05.eligibility.answer",
                         "m05.eligibility.timeout", "m05.eligibility.settle",
                         "m05.eligibility.expire")
WORKER_CAPABILITY: Final = "m05.eligibility.worker"
INCONCLUSIVE_REASONS: Final = ("subscriber_not_found", "name_dob_mismatch",
                               "member_id_not_recognized", "service_type_unsupported",
                               "payer_unavailable", "response_unreadable")
PAYER_ERRORS: Final = INCONCLUSIVE_REASONS[:4]


def el2_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(include_el1=True, include_el2=True)


def el2_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(include_el1=True, include_el2=True)


class EL2Install(el.EL1Install):
    """A synthetic tenant at target 117 (S-I TEST + B5 + E1 + E2), provisioned in the row."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                import sh2_support as sh2
                import si_support as si
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, el2_registry(), self.tenant,
                                           supported=EL2_SUPPORTED, manifest=staff_manifest())
                sa.admin(self, sa.SA_TEST_PROFILE_SQL.format(schema=self.schema))
                sa.admin(self, si.si_test_profile_sql().format(schema=self.schema))
                sh2.settings_raw(self, clinic_display_name=sh2.CLINIC, clinic_tz=NY)
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"E2 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[EL2Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield EL2Install(ids, tenant)
    finally:
        reset(tenant)


def ready(install: EL2Install, **settings: Any) -> EL2Install:
    el.ready(install, **settings)
    return install


def future(days: float) -> datetime:
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0)


def utc(value: Any) -> Any:
    return value.astimezone(UTC) if isinstance(value, datetime) else value


# --- gateway wrappers (direct SQL as the runtime role; the timer workload attribution) ---


def _cmd(**fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, **fields, "attribution": sa.attribution(sa.W_TIMER)}


def due(install: EL2Install, limit: int = 100) -> dict[str, Any]:
    return sa.call(install, "m05_el_due", _cmd(limit=limit))


def claim(install: EL2Install, verification_id: str) -> dict[str, Any]:
    return sa.call(install, "m05_el_claim", _cmd(verification_id=verification_id))


def dispatch(install: EL2Install, verification_id: str, epoch: int) -> dict[str, Any]:
    return sa.call(install, "m05_el_dispatch",
                   _cmd(verification_id=verification_id, epoch=epoch))


def record(install: EL2Install, attempt_id: str, epoch: int, echo: dict[str, Any],
           result: dict[str, Any]) -> dict[str, Any]:
    return sa.call(install, "m05_el_record_answer",
                   _cmd(attempt_id=attempt_id, epoch=epoch, echo=echo, result=result))


def timeout(install: EL2Install, attempt_id: str, epoch: int) -> dict[str, Any]:
    return sa.call(install, "m05_el_timeout", _cmd(attempt_id=attempt_id, epoch=epoch))


def settle(install: EL2Install, verification_id: str, epoch: int) -> dict[str, Any]:
    return sa.call(install, "m05_el_settle", _cmd(verification_id=verification_id, epoch=epoch))


def expire(install: EL2Install, limit: int = 100) -> dict[str, Any]:
    return sa.call(install, "m05_el_expire", _cmd(limit=limit))


def echo_of(dispatched: dict[str, Any]) -> dict[str, Any]:
    """The correlation a well-behaved port echoes back (from the dispatch request)."""
    req = dispatched["request"]
    return {"attempt_id": dispatched["attempt_id"], "tenant_id": req["tenant_id"],
            "appointment_ref": req["appointment_ref"], "patient_id": req["patient_id"],
            "member_id": req["member_id"], "coverage_version": req["coverage_version"]}


def active_result(observed_at: datetime, **over: Any) -> dict[str, Any]:
    got: dict[str, Any] = {"class": "active", "reason": "coverage_active",
                           "observed_at": observed_at.astimezone(UTC).isoformat(),
                           "source": "simulated", "payer_name": "Synthetic Payer",
                           "plan": "SYN-PPO", "copay": "25.00", "deductible_total": "1000.00",
                           "deductible_remaining": "400.00", "coinsurance": "20",
                           "referral_required": False, "auth_required": False,
                           "txn_ref": "SYN-TXN-0001"}
    got.update(over)
    return got


def result_of(cls: str, reason: str, observed_at: datetime | None) -> dict[str, Any]:
    got: dict[str, Any] = {"class": cls, "reason": reason, "source": "simulated"}
    if observed_at is not None:
        got["observed_at"] = observed_at.astimezone(UTC).isoformat()
    return got


# --- observation and FIXTURE helpers (admin connection) ---------------------------------


def q(install: EL2Install, text: str, params: Any = None) -> list[tuple[Any, ...]]:
    return sa.admin(install, sa.q(install, text), params)


def count(install: EL2Install, table: str, where: str = "true", params: Any = None) -> int:
    return el.count(install, table, where, params)


def triggers(install: EL2Install, ref: str) -> dict[str, tuple[Any, ...]]:
    """{trigger: (status, verification_id text or None, returned)}"""
    got = q(install, "SELECT trigger, status, verification_id::text, returned"
            " FROM {s}.m05_el_triggers WHERE appointment_ref = %s", (ref,))
    return {r[0]: (r[1], r[2], r[3]) for r in got}


def verifications(install: EL2Install, ref: str) -> list[tuple[Any, ...]]:
    """[(verification_id, owner_trigger, state, result_class, reason)] by creation."""
    return q(install, "SELECT verification_id::text, owner_trigger, state, result_class, reason"
             " FROM {s}.m05_el_verifications WHERE appointment_ref = %s"
             " ORDER BY created_at, owner_trigger", (ref,))


def attempts(install: EL2Install, verification_id: str) -> list[tuple[Any, ...]]:
    """[(attempt_id, n, state)] by n."""
    return q(install, "SELECT attempt_id::text, n, state FROM {s}.m05_el_attempts"
             " WHERE verification_id = %s ORDER BY n", (verification_id,))


def slots(install: EL2Install, ref: str) -> int:
    """Durable dispatch slots (attempt rows) for one appointment (C2: slots vs calls)."""
    got = q(install, "SELECT count(*) FROM {s}.m05_el_attempts AS a JOIN"
            " {s}.m05_el_verifications AS v USING (verification_id)"
            " WHERE v.appointment_ref = %s", (ref,))
    return int(got[0][0])


def events(install: EL2Install, ref: str) -> list[tuple[Any, ...]]:
    return q(install, "SELECT seq, coverage_version, kind, class, reason"
             " FROM {s}.m05_el_case_events WHERE appointment_ref = %s ORDER BY seq", (ref,))


def late(install: EL2Install) -> list[tuple[Any, ...]]:
    return q(install, "SELECT attempt_id::text, verification_id::text, cause, class"
             " FROM {s}.m05_el_late_answers ORDER BY received_at")


def make_due(install: EL2Install, ref: str, *names: str, offset_s: float = -1.0) -> None:
    """FIXTURE: move trigger due instants relative to database time (the S-I age pattern)."""
    for name in names:
        q(install, "UPDATE {s}.m05_el_triggers SET due_at = clock_timestamp()"
          " + make_interval(secs => %s) WHERE appointment_ref = %s AND trigger = %s",
          (offset_s, ref, name))


def age_lease(install: EL2Install, verification_id: str, offset_s: float) -> None:
    """FIXTURE: lease_until := now + offset (negative: expired)."""
    q(install, "UPDATE {s}.m05_el_verifications SET lease_until = clock_timestamp()"
      " + make_interval(secs => %s) WHERE verification_id = %s", (offset_s, verification_id))


def age_failure(install: EL2Install, attempt_id: str, offset_s: float) -> None:
    """FIXTURE: a failed attempt's finished_at := now + offset (retry-delay aging)."""
    q(install, "UPDATE {s}.m05_el_attempts SET finished_at = clock_timestamp()"
      " + make_interval(secs => %s) WHERE attempt_id = %s", (offset_s, attempt_id))


def window_starts(start: datetime, lead_days: int = 3) -> dict[str, datetime]:
    """The M1 windows in UTC: previsit = R5 due instant; dayof = local midnight, NY."""
    from zoneinfo import ZoneInfo

    from haloflow.m05 import eligibility_rules as rules

    local = start.astimezone(ZoneInfo(NY))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"previsit": rules.previsit_due(start, NY, lead_days).astimezone(UTC),
            "dayof": midnight.astimezone(UTC)}


def enrol(install: EL2Install, start: datetime, **cov: Any) -> tuple[str, str]:
    ref, patient = sa.new_id(), sa.new_id()
    el.put(install, ref, patient, start, coverage=el.present(**cov))
    return ref, patient


def own_verification(install: EL2Install, ref: str, trigger: str) -> str:
    got = q(install, "SELECT verification_id::text FROM {s}.m05_el_verifications"
            " WHERE appointment_ref = %s AND owner_trigger = %s", (ref, trigger))
    assert len(got) == 1, got
    return str(got[0][0])


def run(install: EL2Install, verification_id: str, answers: list[Any]) -> dict[str, Any]:
    """The SQL protocol driven directly (claim -> dispatch -> answer|timeout ... -> settle).

    `answers` is consumed one per dispatched attempt: a dict = a normalized result recorded
    with the correct echo at the attempt's epoch; the string "timeout" = a timed-out
    attempt (followed by a retry-delay aging FIXTURE so attempt 2 may dispatch). Returns
    {"settle": settle result, "attempts": [attempt ids], "epoch": e}."""
    claimed = claim(install, verification_id)
    assert claimed["outcome"] == "claimed", claimed
    epoch = claimed["epoch"]
    sent: list[str] = []
    for answer in answers:
        got = dispatch(install, verification_id, epoch)
        assert got["outcome"] == "dispatched", got
        sent.append(got["attempt_id"])
        if answer == "timeout":
            assert timeout(install, got["attempt_id"], epoch)["outcome"] == "timed_out"
            age_failure(install, got["attempt_id"], -(RETRY_DELAY_S + 1))
            if len(sent) == 1:
                continue
            break
        assert record(install, got["attempt_id"], epoch, echo_of(got), answer) == {
            "schema_version": 1, "outcome": "recorded"}
        break
    return {"settle": settle(install, verification_id, epoch), "attempts": sent,
            "epoch": epoch}


def table_text(install: EL2Install) -> dict[str, list[tuple[Any, ...]]]:
    """Every E1/E2 table as row text (byte-identity comparisons)."""
    return {t: q(install, "SELECT to_jsonb(x)::text FROM {s}." + t + " AS x ORDER BY 1")
            for t in EL2_TABLES}


def ident(name: str) -> sql.Identifier:
    return sql.Identifier(name)


# --- the routed worker path (real resolver + gateway, el2 catalogue) ----------------------


@asynccontextmanager
async def worker_runtime(install: EL2Install) -> AsyncIterator[tuple[Any, Any]]:
    """Yields (EligibilityService over the real gateway, async context factory) for the
    timer workload holding only the worker capability."""
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

    caps = frozenset({WORKER_CAPABILITY})
    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=3)
    await pool.open()
    try:
        rt = compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"])),
            registry=el2_registry(), catalog=el2_catalog(),
            supported_schema_versions=frozenset({EL2_TARGET}),
            activation_policy=ActivationPolicy.ENFORCE)

        async def context() -> Any:
            return await rt.resolver.resolve(
                principal=Principal(kind=PrincipalKind.WORKLOAD, id=sa.W_TIMER,
                                    auth_method="test",
                                    authorized_tenant_ids=frozenset({install.tenant[0]}),
                                    capabilities=caps),
                tenant_hint=install.tenant[0], purpose="operations", capabilities=caps,
                source=TrustedSource.WORKER,
                execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:el2:{uuid.uuid4()}"),
                correlation_id=uuid.uuid4(),
                correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)

        service = EligibilityService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER))
        yield service, context
    finally:
        await pool.close()


class Crash(BaseException):
    """FIXTURE: a worker process dying mid-call. Not an Exception, so a worker that handles
    port failures (Exception) cannot turn it into a timeout; the run ends here."""


class RecordingPort:
    """FIXTURE port around the real SimulatedClearinghouse.

    At ENTRY it records the attempt id and reads, on a SEPARATE admin connection, the
    committed state of that attempt row (it must already be committed as `dispatched`:
    R1 intent before send). `crash_before` / `crash_after` are attempt ordinals (1-based)
    at which the call dies before reaching the inner port / after the inner port answered.
    `entries` = every submit that started; `calls` = every submit that reached the port."""

    def __init__(self, install: EL2Install, inner: Any, *, crash_before: int = 0,
                 crash_after: int = 0) -> None:
        self.install, self.inner = install, inner
        self.crash_before, self.crash_after = crash_before, crash_after
        self.entries: list[str] = []
        self.committed_at_entry: list[Any] = []
        self.calls: list[str] = []

    async def submit(self, attempt_id: str, request: Any) -> Any:
        self.entries.append(attempt_id)
        got = q(self.install, "SELECT state FROM {s}.m05_el_attempts WHERE attempt_id = %s",
                (attempt_id,))
        self.committed_at_entry.append(got[0][0] if got else None)
        if len(self.entries) == self.crash_before:
            raise Crash()
        answer = await self.inner.submit(attempt_id, request)
        self.calls.append(attempt_id)
        if len(self.entries) == self.crash_after:
            raise Crash()
        return answer
