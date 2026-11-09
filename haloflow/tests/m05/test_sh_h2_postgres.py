"""L-1S package S-H, checkpoint H2 — reminder scheduling and one-shot send ticket,
PostgreSQL 17 rows (RED first).

Governing: see `sh2_support` (package v1 + v1r1 D1-D6, Codex ALIGN with seven pins, owner
decisions Q-SH2 / P-1, interface pins v1r1 D7-D10 and Codex review
codex_h2-interface-pins-v1-review.md). Synthetic only; the only sender is FakeSender; no
EHR; nothing composed. Integration rows use the REAL database clock (D7): zones are
fixed-offset Etc zones chosen so that local now is mid-window or after the window, and
start offsets are hours, never microseconds. Exact microsecond boundaries are asserted on
the owner-only pure decision function. Fresh-clock discriminators hold a lock the call
needs while the database clock crosses a boundary (generous margins): a pre-lock or
transaction-start clock would give the opposite result.

Rows (P = PostgreSQL; a parametrised row counts once per parameter):
- P00   install: target 74, t057..t074 in order, no production overlap, profiles exactly the
        typed H2 units, four amendments (t072 closes runtime EXECUTE on t038/t048/t049/t055),
        ACLs (runtime / owner-only / support-only), PUBLIC none, H1 and S-A bodies
        byte-identical to an S-H tenant's, seeded settings defaults.
- P00b  control (green now): an S-H tenant (target 56) has no H2 table or function.
- P00c  startup per-tenant verification over the H2 registry passes twice.
- P01   due instant (Codex zoneinfo cases x lead 2/3): exact UTC due; elapsed lead; one
        microsecond before due -> not_due with next_eligible_at = due; at due -> send.
- P02   window boundaries on and after the due day ([09:00, 19:00) local, microseconds).
- P03   floor: exactly 26 h -> send; 26 h - 1 us -> too_late (pure).
- P03b  floor (integration): start in 25 h -> removed TOO_LATE_26H; no op; row deleted.
- P03c  examine fresh-clock discriminator: blocked on the settings row while the clock
        crosses start - 26 h -> TOO_LATE_26H (a pre-lock clock would claim).
- P04   deferral round trip: a prepared H2 op, window closed -> deferred, still prepared,
        no attempt, row re-keyed to the next local 09:00; settings_put reopens and
        re-keys; examine claims; run_claimed sends exactly once.
- P05   gate fails inside consume -> refused PRECALL_WINDOW_CLOSED, op refused, ticket
        spent (committed), held lists it; again -> TICKET_SPENT; never re-examined.
- P05b  consume fresh-clock discriminator (floor crossed while blocked) -> refused.
- P06   paused worker past its lease -> consume CLAIM_STALE; ticket spent; expire ->
        uncertain; zero sends.
- P07   a second claim during a live attempt -> CLAIM_STALE, zero writes.
- P07b  consume with a wrong attempt -> CLAIM_STALE, zero writes; the valid ticket stays
        unspent and still authorises exactly one send.
- P08   X2: read v2 db_now advances across two reads in one transaction.
- P09   X3: confirmation arrives between prepare and claim -> abandoned CONFIRMED.
- P09b  claim v2 fresh-clock discriminator -> abandoned TOO_LATE_26H.
- P10   STALE_SNAPSHOT (settings bump / name change / confirmation change between render
        and examine) -> not_ready STALE_SNAPSHOT, no op/inputs, re-check now; a fresh render
        claims with the NEW inputs.
- P11   atomicity: a forced error after H1 prepare inside examine -> neither op nor inputs
        nor ticket; the schedule row unchanged.
- P12   prepare_rendered replay: same op_id -> stored inputs, zero writes; same op_id and
        hash with changed inputs -> INPUTS_CONFLICT, zero writes; a different hash ->
        OP_CONFLICT; a second op_id on the ref -> the original op and inputs.
- P13   timezone: department wins (clinic NULL never suppresses it, pin 7); invalid or NULL
        department falls back only to a VALID clinic zone; both invalid -> TZ_UNRESOLVED.
- P14   status / name / phone mapping, including every NAMES_OK / NAMES_BAD vector (the
        SQL rule equals the Python rule) and a phone shared by two patients.
- P15   backlog: C = 2, four permanent skips ahead of two sendable -> sent within 3 runs.
- P16   churn: directory update re-keys >= now; cancel removes; settings_put re-keys >= now.
- P17   two concurrent runners: at most one op and one send per ref; all sent; no error.
- P18   pin 1: examine holding its locks; a directory v2 write and a settings_put wait,
        then succeed; no deadlock.
- P19   crash: examine rolled back -> nothing; committed then no consume -> lease expiry
        uncertain; zero sends.
- P20   bootstrap on upgrade 56 -> 74: no-op and prepared refs enqueued, others excluded;
        PROFILE_MISSING; an H1-era prepared op -> abandoned NO_RENDER_INPUTS, zero sends;
        new H2 prepared inputs survive a re-run of the migration runner.
- P21   settings validation and support authority (runtime 42501; not allow-listed, no
        grant, other tenant, expired, revoked, wrong capability, expiry while waiting for
        the settings lock -> SUPPORT_NOT_AUTHORIZED); zero writes on every refusal.
- P22   runtime raw calls of the closed and owner-only functions -> 42501.
- P23   D9: while deactivated, settings_put applies and re-keys but creates no op/ticket;
        examine / consume / claim v2 through the gateway -> M05_NOT_ACTIVATED.
- P24   two concurrent run_claimed (same op and attempt): one send; a paused SQL winner
        makes the service loser TICKET_SPENT; a paused sending winner and a loser -> one send.
- P25   duplicate delivery after the first finished -> not_sent.
- P26   restart: consume committed by a dead process; a new process -> not_sent.
- P27   consume commit-unknown after commit and after rollback: zero sends in both; the
        ticket is spent only after commit; after lease expiry no consume returns send.
- P28   stale selection: re-keyed into the future between ready and examine -> not_ready
        REKEYED, zero writes.
- P29   direct runtime claim v2 / read v2 / consume on an op without render inputs ->
        NO_RENDER_INPUTS, zero writes.
- P30   wrong capability on every new gateway method -> CAPABILITIES_INVALID; identical ids
        in two tenants stay isolated.
- P35   SKIP LOCKED contention (Codex 011101Z): a FIXTURE holds ONLY the schedule row lock
        (no settings or contact lock); examine returns not_ready LOCKED at once, zero
        writes; under a 3 s lock_timeout a blocking examine would fail, never hang.
- P36   (Codex 26edf09 I1) a prepared op, then the source status becomes completed /
        unsupported / NULL -> examine removes it, op abandoned with the code, no ticket.
- P36b  the same change after claim -> run_claimed not_sent, op refused with the code,
        correlation closed, no window, ticket spent.
- P37   (I2) a snapshot taken while a directory update is open sees the old start AND the
        old version; after the commit that snapshot is STALE_SNAPSHOT; a fresh render claims
        and freezes the NEW start.
- P31   null-safety: clinic display name NULL -> SETTINGS_INCOMPLETE; department and clinic
        zone NULL -> TZ_UNRESOLVED; confirmation NULL -> sends (unknown); no profile row ->
        PROFILE_MISSING.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import m02_support
import psycopg
import pytest
import sa_support as sa
import sh2_support as sh2
import sh_support as sh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


@pytest.fixture(scope="module")
def h2(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
       m05_reset: Any) -> Iterator[sh2.SH2Install]:
    yield from sh2.module_install(request, m02_ids, m05_reset, "h2")


@pytest.fixture(scope="module")
def h2_other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
             m05_reset: Any) -> Iterator[sh2.SH2Install]:
    yield from sh2.module_install(request, m02_ids, m05_reset, "h2:other")


@pytest.fixture(scope="module")
def h2_runner(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
              m05_reset: Any) -> Iterator[sh2.SH2Install]:
    yield from sh2.module_install(request, m02_ids, m05_reset, "h2:runner")


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        yield handle


@pytest.fixture
def clean_settings(h2: sh2.SH2Install) -> Iterator[sh2.SH2Install]:
    """Rows that change settings restore the defaults afterwards (FIXTURE). The install
    itself happens in the row body (class-B red rule); teardown resets only an installed
    tenant."""

    try:
        yield h2
    finally:
        if h2._attempted and h2._error is None:
            sh2.reset_settings(h2)


def _sendable(install: sh2.SH2Install, **kw: Any) -> tuple[str, str | None, str, str]:
    zone, start = sh2.in_window(install)
    patient, phone, ref = sh2.seed(install, start, zone=zone, **kw)
    return patient, phone, ref, zone


def _code(error: BaseException) -> str:
    assert isinstance(error, psycopg.Error), repr(error)
    return str(error.diag.message_primary)


@contextmanager
def _settings_locked(install: sa.SAInstall) -> Iterator[None]:
    """FIXTURE held lock: the settings row FOR UPDATE on an admin transaction (examine,
    claim v2 and consume take it FOR SHARE first, pin 1)."""

    with psycopg.connect(install.ids.admin) as conn, conn.transaction():
        conn.execute(sa.q(install, "SELECT 1 FROM {s}.m05_reminder_settings FOR UPDATE"))
        yield


# --- P00 ----------------------------------------------------------------------------------


def _function_acl(install: sa.SAInstall, name: str) -> list[tuple[Any, ...]]:
    return sorted(sa.admin(install, (
        "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
        " x.privilege_type, pg_get_userbyid(x.grantor), x.is_grantable"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,"
        " LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x"
        " WHERE n.nspname = %s AND p.proname = %s"), (install.schema, name)))


def _prosrc(install: sa.SAInstall, name: str) -> str:
    rows = sa.admin(install, "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n"
                             " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                             " AND p.proname = %s", (install.schema, name))
    assert len(rows) == 1, (name, rows)
    text: str = rows[0][0]
    return text.replace(install.schema, "{schema}")


def _runtime_can_execute(install: sa.SAInstall, name: str) -> bool:
    rows = sa.admin(install, "SELECT has_function_privilege(%s, p.oid, 'EXECUTE')"
                             " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                             " WHERE n.nspname = %s AND p.proname = %s",
                    (sa.RUNTIME, install.schema, name))
    return bool(rows[0][0])


def _volatility(install: sa.SAInstall, name: str) -> str:
    rows = sa.admin(install, "SELECT p.provolatile FROM pg_proc p JOIN pg_namespace n"
                             " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                             " AND p.proname = %s", (install.schema, name))
    value: str = rows[0][0]
    return value


def test_h2_p00_install_target_and_boundaries(h2: sh2.SH2Install,
                                              request: pytest.FixtureRequest,
                                              m02_ids: m02_support.Identities,
                                              m05_reset: Any) -> None:
    from haloflow.composition import APPROVED_SUPPORTED_SCHEMA_VERSIONS, APPROVED_TENANT_MIGRATIONS
    from haloflow.m05.reminder_units import (
        SH2_ACL_AMENDMENTS,
        SH2_INSTALLED_STATE_PROFILES,
        SH2_TENANT_MIGRATIONS,
    )

    h2.require()
    assert tuple(SH2_TENANT_MIGRATIONS) == sh2.SH2_UNIT_IDS
    assert sh2.sh2_registry().target_version == sh2.SH2_TARGET == 74
    production_ids = {unit_id for group in APPROVED_TENANT_MIGRATIONS for unit_id in group}
    assert not production_ids & set(sh2.SH2_UNIT_IDS)
    assert frozenset({36}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert set(SH2_INSTALLED_STATE_PROFILES) == (set(sh2.SH2_UNIT_IDS) - sh2.TABLE_UNITS
                                                 - {sh2.CLOSURE_UNIT})
    amended = sorted((a.target_unit, a.activated_by, a.removes) for a in SH2_ACL_AMENDMENTS)
    assert amended == sorted(
        (unit, sh2.CLOSURE_UNIT, ((sa.RUNTIME, "EXECUTE", sa.M05_OWNER, False),))
        for unit, _name in sh2.CLOSED)
    owner = (sa.M05_OWNER, "EXECUTE", sa.M05_OWNER, False)
    for name in sh2.RUNTIME_FUNCTIONS:
        assert _function_acl(h2, name) == sorted(
            [owner, (sa.RUNTIME, "EXECUTE", sa.M05_OWNER, False)]), name
    for name in sh2.OWNER_FUNCTIONS:
        assert _function_acl(h2, name) == [owner], name
    assert _function_acl(h2, sh2.SUPPORT_FUNCTION) == sorted(
        [owner, (sh2.SUPPORT_ROLE, "EXECUTE", sa.M05_OWNER, False)])
    assert _function_acl(h2, sh2.AUTHORITY_FUNCTION) == sorted(
        [(sh2.STAFF_OWNER, "EXECUTE", sh2.STAFF_OWNER, False),
         (sa.M05_OWNER, "EXECUTE", sh2.STAFF_OWNER, False)])
    assert _runtime_can_execute(h2, sh2.AUTHORITY_FUNCTION) is False
    assert _volatility(h2, "m05_reminder_decision") == "s"  # STABLE
    for _unit, name in sh2.CLOSED:
        assert _function_acl(h2, name) == [owner], name
        assert _runtime_can_execute(h2, name) is False, name
    # Unchanged bodies: every closed / pre-existing function equals an S-H tenant's.
    for old in sh.module_install(request, m02_ids, m05_reset, "h2-p00:sh"):
        old.require()
        for name in ("m05_directory_put", "m05_outbound_prepare", "m05_outbound_claim",
                     "m05_outbound_outcome", "m05_outbound_expire", "m05_register_outbound",
                     "m05_outbound_read", "m05_outbound_held", sh.V2_INBOUND):
            assert _prosrc(h2, name) == _prosrc(old, name), name
    # Seeded settings (a tenant without the FIXTURE display name).
    for bare in sh2.module_install(request, m02_ids, m05_reset, "h2-p00:bare"):
        bare.profile = False
        bare.require()
        row = sh2.settings_row(bare)
        assert {k: row[k] for k in ("lead_days", "clinic_tz", "clinic_display_name",
                                    "settings_version")} == {
            "lead_days": 2, "clinic_tz": None, "clinic_display_name": None,
            "settings_version": 1}
        assert (row["send_time"][:5], row["window_start"][:5], row["window_end"][:5]) == (
            "10:00", "09:00", "19:00")


def test_h2_p00b_sh_tenant_has_no_h2_objects(request: pytest.FixtureRequest,
                                             m02_ids: m02_support.Identities,
                                             m05_reset: Any) -> None:
    """Control: green before any H2 code exists."""

    for old in sh.module_install(request, m02_ids, m05_reset, "h2-p00b:sh"):
        old.require()
        rows = sa.admin(old, "SELECT count(*) FROM pg_class c JOIN pg_namespace n"
                             " ON n.oid = c.relnamespace WHERE n.nspname = %s"
                             " AND c.relname = ANY(%s)",
                        (old.schema, list(sh2.H2_TABLES[:5])))
        assert rows[0][0] == 0
        fns = sa.admin(old, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                            " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                            " AND p.proname = ANY(%s)",
                       (old.schema, [*sh2.RUNTIME_FUNCTIONS, *sh2.OWNER_FUNCTIONS,
                                     sh2.SUPPORT_FUNCTION]))
        assert fns[0][0] == 0


def test_h2_p00c_startup_verification_twice(h2: sh2.SH2Install) -> None:
    import sg_harness as sg

    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies
    from haloflow.startup import Deployment, _verify_served_tenant

    h2.require()

    async def verify_once() -> Any:
        pool = TenantPool(h2.ids.logins[sa.RUNTIME], min_size=1, max_size=2)
        await pool.open()
        try:
            deps = TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sg._connector(h2.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sg.forbid_provisioning())
            return await _verify_served_tenant(
                deps, tenant_id=h2.tenant_id, deployment=Deployment.NON_PRODUCTION,
                plan=sg.build_static_plan(sh2.sh2_registry()))
        finally:
            await pool.close()

    for _ in range(2):
        assert asyncio.run(verify_once()) == (h2.tenant_id, h2.schema, sh2.SH2_TARGET)


# --- P01..P03: the pure decision function (D7, exact boundaries) ----------------------------


NYZ = ZoneInfo(sh2.NY)
# (visit local, lead) -> (due UTC, elapsed hours): Codex's zoneinfo cases plus lead 3.
DUE_CASES = (
    (datetime(2026, 11, 2, 9, 0, tzinfo=NYZ), 2, datetime(2026, 10, 31, 14, 0, tzinfo=UTC), 48),
    (datetime(2026, 11, 2, 9, 0, tzinfo=NYZ), 3, datetime(2026, 10, 30, 14, 0, tzinfo=UTC), 72),
    (datetime(2027, 3, 15, 9, 0, tzinfo=NYZ), 2, datetime(2027, 3, 13, 15, 0, tzinfo=UTC), 46),
    (datetime(2027, 3, 15, 9, 0, tzinfo=NYZ), 3, datetime(2027, 3, 12, 15, 0, tzinfo=UTC), 70),
    (datetime(2026, 11, 10, 8, 0, tzinfo=NYZ), 2, datetime(2026, 11, 8, 15, 0, tzinfo=UTC), 46),
    (datetime(2026, 11, 10, 8, 0, tzinfo=NYZ), 3, datetime(2026, 11, 7, 15, 0, tzinfo=UTC), 70),
)
US: timedelta = timedelta(microseconds=1)


def _ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@pytest.mark.parametrize(("visit", "lead", "due", "elapsed"), DUE_CASES,
                         ids=[f"c{i}" for i in range(len(DUE_CASES))])
def test_h2_p01_due_instant(h2: sh2.SH2Install, visit: datetime, lead: int, due: datetime,
                            elapsed: int) -> None:
    h2.require()
    assert (visit.astimezone(UTC) - due) == timedelta(hours=elapsed)  # literal self-check
    before = sh2.decision(h2, now=due - US, start=visit, zone=sh2.NY, lead_days=lead)
    assert before["verdict"] == "not_due"
    assert _ts(before["due_at"]) == due
    assert _ts(before["next_eligible_at"]) == due
    at = sh2.decision(h2, now=due, start=visit, zone=sh2.NY, lead_days=lead)
    assert at["verdict"] == "send"
    assert _ts(at["due_at"]) == due


VISIT = datetime(2026, 11, 20, 15, 0, tzinfo=NYZ)  # due (lead 2) Nov 18 10:00 EST


@pytest.mark.parametrize(("local", "verdict", "next_local"), [
    (datetime(2026, 11, 18, 9, 0, tzinfo=NYZ), "not_due", datetime(2026, 11, 18, 10, 0,
                                                                   tzinfo=NYZ)),
    (datetime(2026, 11, 18, 10, 0, tzinfo=NYZ), "send", None),
    (datetime(2026, 11, 18, 18, 59, 59, 999999, tzinfo=NYZ), "send", None),
    (datetime(2026, 11, 18, 19, 0, tzinfo=NYZ), "outside_window",
     datetime(2026, 11, 19, 9, 0, tzinfo=NYZ)),
    (datetime(2026, 11, 19, 8, 59, 59, 999999, tzinfo=NYZ), "outside_window",
     datetime(2026, 11, 19, 9, 0, tzinfo=NYZ)),
    (datetime(2026, 11, 19, 9, 0, tzinfo=NYZ), "send", None),
], ids=["0900-due-day", "1000", "185959", "1900", "085959-next", "0900-next"])
def test_h2_p02_window_boundaries(h2: sh2.SH2Install, local: datetime, verdict: str,
                                  next_local: datetime | None) -> None:
    h2.require()
    result = sh2.decision(h2, now=local, start=VISIT, zone=sh2.NY)
    assert result["verdict"] == verdict
    if next_local is not None:
        assert _ts(result["next_eligible_at"]) == next_local.astimezone(UTC)


def test_h2_p02b_narrowed_window_and_lead3(h2: sh2.SH2Install) -> None:
    h2.require()
    narrowed = {"send_time": "10:30", "window_start": "10:00", "window_end": "17:00"}
    at_17 = datetime(2026, 11, 18, 17, 0, tzinfo=NYZ)
    assert sh2.decision(h2, now=at_17 - US, start=VISIT, zone=sh2.NY,
                        **narrowed)["verdict"] == "send"
    out = sh2.decision(h2, now=at_17, start=VISIT, zone=sh2.NY, **narrowed)
    assert out["verdict"] == "outside_window"
    assert _ts(out["next_eligible_at"]) == datetime(2026, 11, 19, 10, 0, tzinfo=NYZ)
    lead3 = sh2.decision(h2, now=datetime(2026, 11, 17, 10, 0, tzinfo=NYZ), start=VISIT,
                         zone=sh2.NY, lead_days=3)
    assert lead3["verdict"] == "send"


def test_h2_p03_floor_exact(h2: sh2.SH2Install) -> None:
    h2.require()
    edge = VISIT.astimezone(UTC) - sh2.FLOOR  # Nov 19 13:00 EST: in window, after due
    assert sh2.decision(h2, now=edge, start=VISIT, zone=sh2.NY)["verdict"] == "send"
    late = sh2.decision(h2, now=edge + US, start=VISIT, zone=sh2.NY)
    assert late["verdict"] == "too_late"
    assert late["next_eligible_at"] is None
    # Precedence: too_late wins over outside_window (after 19:00 with < 26 h left).
    night = sh2.decision(h2, now=datetime(2026, 11, 19, 20, 0, tzinfo=NYZ), start=VISIT,
                         zone=sh2.NY)
    assert night["verdict"] == "too_late"


@pytest.mark.parametrize("bad", [
    {"zone": "Mars/Olympus"}, {"zone": None}, {"lead_days": None}, {"window_end": None},
    {"now": None}, {"start_at": None},
], ids=["zone-invalid", "zone-null", "lead-null", "window-null", "now-null", "start-null"])
def test_h2_p03d_decision_never_defaults_to_send(h2: sh2.SH2Install, bad: dict[str, Any]
                                                 ) -> None:
    """Missing or invalid inputs are refused, never a `send` (Codex D7)."""

    h2.require()
    cmd = {"schema_version": 1, "now": sh2.iso_z(VISIT - timedelta(days=1)),
           "start_at": sh2.iso_z(VISIT), "zone": sh2.NY, "lead_days": 2,
           "send_time": "10:00", "window_start": "09:00", "window_end": "19:00", **bad}
    with pytest.raises(psycopg.Error) as refused:
        sh2.owner_call(h2, "m05_reminder_decision", cmd)
    assert (refused.value.sqlstate, _code(refused.value)) == ("P0001", "COMMAND_MALFORMED")


def test_h2_p03b_floor_integration(h2: sh2.SH2Install) -> None:
    h2.require()
    now = sh2.db_now(h2)
    zone = sh2.zone_with_local_hour(now, 11, 15)
    _p, _ph, ref = sh2.seed(h2, now + timedelta(hours=25), zone=zone)
    result = sh2.examine(h2, ref)
    assert (result["outcome"], result["code"]) == ("removed", "TOO_LATE_26H")
    assert sh2.op_for(h2, ref) is None
    assert sh2.schedule_row(h2, ref) is None


def test_h2_p03c_examine_uses_post_lock_clock(h2: sh2.SH2Install) -> None:
    h2.require()
    now = sh2.db_now(h2)
    zone = sh2.zone_with_local_hour(now, 11, 15)
    start = now + sh2.FLOOR + timedelta(seconds=4)
    _p, _ph, ref = sh2.seed(h2, start, zone=zone)
    snap = sh2.render_snapshot(h2, ref)
    cmd = sh2.examine_cmd(ref, snap=snap)
    with _settings_locked(h2):
        waiter = sa.Waiter(h2, "m05_outbound_examine", cmd).start_and_block()
        sh2.wait_past(h2, start - sh2.FLOOR + timedelta(seconds=1))
    result = waiter.join()
    assert (result["outcome"], result["code"]) == ("removed", "TOO_LATE_26H")
    assert sh2.op_for(h2, ref) is None


# --- P04..P09: deferral, gate, lease, attempts ---------------------------------------------


def _local_hhmm(install: sa.SAInstall, zone: str) -> str:
    return sh2.db_now(install).astimezone(ZoneInfo(zone)).strftime("%H:%M")


def test_h2_p04_deferral_round_trip(clean_settings: sh2.SH2Install, env: db.L1WEnv) -> None:
    h2 = clean_settings
    h2.require()
    _p, _ph, ref, zone = _sendable(h2)
    op = sh2.prepare_rendered(h2, ref)
    fence_before = sh.op_row(h2, op["op_id"])["fence"]
    sh2.settings_raw(h2, window_end=_local_hhmm(h2, zone))  # FIXTURE: the window closes
    deferred = sh2.examine(h2, ref)
    assert deferred["outcome"] == "deferred"
    row = sh.op_row(h2, op["op_id"])
    assert (row["state"], row["fence"]) == ("prepared", fence_before)
    assert sh2.ticket_row(h2, op["op_id"]) is None
    assert sh2.next_check(h2, ref) == sh2.at_local(sh2.db_now(h2), zone, 1, 9)
    with sh2.support_login(env, h2, "p04") as support:
        t0 = sh2.db_now(h2)
        sh2.settings_put(support, h2)  # reopen 09:00-19:00 through the real path
    assert t0 <= sh2.next_check(h2, ref) <= sh2.db_now(h2)
    claimed = sh2.examine(h2, ref)
    assert (claimed["outcome"], claimed["op_id"]) == ("claimed", op["op_id"])

    async def body() -> tuple[str, int]:
        from haloflow.m05.outbound import FakeSender, OutboundWorker

        async with sh2.runtime(h2) as rt:
            sender = FakeSender()
            worker = OutboundWorker(sh2.service(rt), sender, _render)
            ctx = await sh2.dispatch_context(rt, h2)
            return await worker.run_claimed(ctx, op["op_id"], claimed["attempt_id"]), len(
                sender.calls)

    import sc_support as sc

    sc.change(h2, "grant")
    assert asyncio.run(body()) == ("accepted", 1)


def test_h2_p05_gate_refusal_commits(clean_settings: sh2.SH2Install) -> None:
    h2 = clean_settings
    h2.require()
    _p, _ph, ref, zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)
    sh2.settings_raw(h2, window_end=_local_hhmm(h2, zone))
    assert sh2.consume(h2, op_id, attempt) == {
        "schema_version": 1, "outcome": "refused", "code": "PRECALL_WINDOW_CLOSED"}
    row = sh.op_row(h2, op_id)
    assert (row["state"], row["code"]) == ("refused", "PRECALL_WINDOW_CLOSED")
    ticket = sh2.ticket_row(h2, op_id)
    assert ticket is not None and ticket["consumed_at"] is not None
    assert op_id in {o["op_id"] for o in sh.held_ops(h2)["ops"]}
    assert sh2.consume(h2, op_id, attempt)["code"] == "TICKET_SPENT"
    assert sh2.examine(h2, ref)["code"] == "NOT_SCHEDULED"


def test_h2_p05b_consume_uses_post_lock_clock(h2: sh2.SH2Install) -> None:
    h2.require()
    now = sh2.db_now(h2)
    zone = sh2.zone_with_local_hour(now, 11, 15)
    start = now + sh2.FLOOR + timedelta(seconds=5)
    _p, _ph, ref = sh2.seed(h2, start, zone=zone)
    op_id, attempt = sh2.claimed(h2, ref)
    with _settings_locked(h2):
        waiter = sa.Waiter(h2, "m05_outbound_consume",
                           sh2.consume_cmd(op_id, attempt)).start_and_block()
        sh2.wait_past(h2, start - sh2.FLOOR + timedelta(seconds=1))
    assert waiter.join()["code"] == "PRECALL_WINDOW_CLOSED"
    assert sh.op_row(h2, op_id)["state"] == "refused"


def test_h2_p06_paused_worker_past_lease(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)
    sh2.expire_lease(h2, op_id)  # FIXTURE time travel (H1 leases are 30-600 s)
    assert sh2.consume(h2, op_id, attempt)["code"] == "CLAIM_STALE"
    ticket = sh2.ticket_row(h2, op_id)
    assert ticket is not None and ticket["consumed_at"] is not None
    sh.expire(h2)
    assert (sh.op_row(h2, op_id)["state"], sh.op_row(h2, op_id)["code"]) == (
        "uncertain", "LEASE_EXPIRED")


def test_h2_p07_second_claim_during_attempt(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, _attempt = sh2.claimed(h2, ref)
    fence = sh.op_row(h2, op_id)["fence"]
    sh2.refused(h2, "m05_outbound_claim_v2", sh.claim_cmd(op_id, fence - 1), "CLAIM_STALE")
    sh2.refused(h2, "m05_outbound_claim_v2", sh.claim_cmd(op_id, fence), "CLAIM_STALE")


def test_h2_p07b_wrong_attempt_spends_nothing(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)
    before = sh2.h2_snapshot(h2)
    assert sh2.consume(h2, op_id, sa.new_id())["code"] == "CLAIM_STALE"
    assert sh2.h2_snapshot(h2) == before
    assert sh2.ticket_row(h2, op_id)["consumed_at"] is None  # type: ignore[index]
    assert sh2.consume(h2, op_id, attempt)["outcome"] == "send"


def test_h2_p08_read_v2_fresh_db_now(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, phone, ref, zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)
    with sa.runtime_conn(h2) as conn, conn.transaction():
        first = sa.call_in(conn, h2, "m05_outbound_read_v2", sh2.read_v2_cmd(op_id, attempt))
        conn.execute("SELECT pg_sleep(0.05)")
        second = sa.call_in(conn, h2, "m05_outbound_read_v2", sh2.read_v2_cmd(op_id, attempt))
    assert _ts(second["db_now"]) > _ts(first["db_now"])
    assert (first["phone"], first["zone"], first["first_name"]) == (phone, zone, sh2.FIRST_NAME)


def test_h2_p09_confirmation_between_prepare_and_claim(h2: sh2.SH2Install) -> None:
    h2.require()
    patient, _ph, ref, zone = _sendable(h2)
    op = sh2.prepare_rendered(h2, ref)
    start = sh.start_of(h2, ref)
    sh2.appointment_put(h2, ref, patient, start, department_tz=zone, confirmation="confirmed")
    result = sh2.examine(h2, ref)
    assert (result["outcome"], result["code"]) == ("removed", "CONFIRMED")
    assert (sh.op_row(h2, op["op_id"])["state"], sh.op_row(h2, op["op_id"])["code"]) == (
        "abandoned", "CONFIRMED")
    assert sh2.schedule_row(h2, ref) is None
    assert sh2.ticket_row(h2, op["op_id"]) is None


def test_h2_p09b_claim_v2_uses_post_lock_clock(h2: sh2.SH2Install) -> None:
    h2.require()
    now = sh2.db_now(h2)
    zone = sh2.zone_with_local_hour(now, 11, 15)
    start = now + sh2.FLOOR + timedelta(seconds=4)
    _p, _ph, ref = sh2.seed(h2, start, zone=zone)
    op = sh2.prepare_rendered(h2, ref)
    with _settings_locked(h2):
        waiter = sa.Waiter(h2, "m05_outbound_claim_v2",
                           sh.claim_cmd(op["op_id"], op["fence"])).start_and_block()
        sh2.wait_past(h2, start - sh2.FLOOR + timedelta(seconds=1))
    result = waiter.join()
    assert (result["outcome"], result["code"]) == ("abandoned", "TOO_LATE_26H")
    assert sh2.ticket_row(h2, op["op_id"]) is None


# --- P10..P14: snapshot, atomicity, replay, zone, mapping ----------------------------------


@pytest.mark.parametrize("change", ["settings", "name", "confirmation"])
def test_h2_p10_stale_snapshot(clean_settings: sh2.SH2Install, change: str) -> None:
    h2 = clean_settings
    h2.require()
    patient, _ph, ref, zone = _sendable(h2)
    snap = sh2.render_snapshot(h2, ref)
    start = sh.start_of(h2, ref)
    if change == "settings":
        sh2.settings_raw(h2, clinic_display_name="HaloVox Renamed Clinic")
    elif change == "name":
        sh2.appointment_put(h2, ref, patient, start, department_tz=zone, first_name="Renamed")
    else:
        sh2.appointment_put(h2, ref, patient, start, department_tz=zone, confirmation="unknown")
    t0 = sh2.db_now(h2)
    stale = sh2.examine(h2, ref, snap=snap)
    assert (stale["outcome"], stale["code"]) == ("not_ready", "STALE_SNAPSHOT")
    assert sh2.op_for(h2, ref) is None
    assert sh2.next_check(h2, ref) <= sh2.db_now(h2) and sh2.next_check(h2, ref) >= t0
    op_id, _attempt = sh2.claimed(h2, ref)
    inputs = sh2.inputs_row(h2, op_id)
    assert inputs is not None
    expected = {"settings": ("first_name", sh2.FIRST_NAME), "name": ("first_name", "Renamed"),
                "confirmation": ("first_name", sh2.FIRST_NAME)}[change]
    assert inputs[expected[0]] == expected[1]
    if change == "settings":
        assert inputs["clinic_display_name"] == "HaloVox Renamed Clinic"


def test_h2_p11_atomic_examine(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    row_before = sh2.schedule_row(h2, ref)
    fn = sa.q(h2, "{s}.h2_p11_forced")
    sa.admin(h2, sa.q(h2, "CREATE FUNCTION {s}.h2_p11_forced() RETURNS trigger"
                          " LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION USING"
                          " ERRCODE = 'P0001', MESSAGE = 'H2_P11_FORCED'; END $$"))
    sa.admin(h2, sa.q(h2, "CREATE TRIGGER h2_p11 BEFORE INSERT ON {s}.m05_outbound_render_inputs"
                          " FOR EACH ROW EXECUTE FUNCTION ") + fn + sa.q(h2, "()"))
    try:
        with pytest.raises(psycopg.Error) as forced:
            sh2.examine(h2, ref)
        assert _code(forced.value) == "H2_P11_FORCED"
    finally:
        sa.admin(h2, sa.q(h2, "DROP TRIGGER h2_p11 ON {s}.m05_outbound_render_inputs"))
        sa.admin(h2, sa.q(h2, "DROP FUNCTION {s}.h2_p11_forced()"))
    assert sh2.op_for(h2, ref) is None
    assert sh2.schedule_row(h2, ref) == row_before
    assert sa.admin(h2, sa.q(h2, "SELECT count(*) FROM {s}.m05_outbound_send_tickets t"
                                 " JOIN {s}.m05_outbound_ops o USING (op_id)"
                                 " WHERE o.appointment_ref = %s"), (ref,))[0][0] == 0


def test_h2_p12_prepare_rendered_replay(h2: sh2.SH2Install) -> None:
    h2.require()
    patient, _ph, ref, zone = _sendable(h2)
    snap = sh2.render_snapshot(h2, ref)
    cmd = sh2.prepare_rendered_cmd(ref, snap)
    first = sh2.owner_call(h2, "m05_outbound_prepare_rendered", cmd)
    stored = sh2.inputs_row(h2, cmd["op_id"])
    before = sh2.h2_snapshot(h2)
    again = sh2.owner_call(h2, "m05_outbound_prepare_rendered", cmd)
    assert again["outcome"] == "existing" and again["inputs"] == first["inputs"]
    assert sh2.h2_snapshot(h2) == before
    # Same op_id and hash, changed non-text input (confirmation bumps source_version).
    sh2.appointment_put(h2, ref, patient, sh.start_of(h2, ref), department_tz=zone,
                        confirmation="unknown")
    changed = sh2.prepare_rendered_cmd(ref, sh2.render_snapshot(h2, ref), op_id=cmd["op_id"],
                                       reminder=cmd["reminder_id"])
    assert changed["text_sha256"] == cmd["text_sha256"]
    # The UNCHANGED original command, replayed after the versions moved (lost-ack replay):
    before = sh2.h2_snapshot(h2)
    late = sh2.owner_call(h2, "m05_outbound_prepare_rendered", cmd)
    assert late["outcome"] == "existing" and late["inputs"] == first["inputs"]
    assert sh2.h2_snapshot(h2) == before
    before = sh2.h2_snapshot(h2)
    with pytest.raises(psycopg.Error) as conflict:
        sh2.owner_call(h2, "m05_outbound_prepare_rendered", changed)
    assert _code(conflict.value) == "INPUTS_CONFLICT"
    assert sh2.h2_snapshot(h2) == before
    with pytest.raises(psycopg.Error) as op_conflict:
        sh2.owner_call(h2, "m05_outbound_prepare_rendered", {**cmd, "text_sha256": "0" * 64})
    assert _code(op_conflict.value) == "OP_CONFLICT"
    second = sh2.prepare_rendered(h2, ref)  # a different op_id on the same ref
    assert second["op_id"] == cmd["op_id"]
    assert sh2.inputs_row(h2, cmd["op_id"]) == stored
    assert sa.admin(h2, sa.q(h2, "SELECT count(*) FROM {s}.m05_outbound_render_inputs i"
                                 " JOIN {s}.m05_outbound_ops o USING (op_id)"
                                 " WHERE o.appointment_ref = %s"), (ref,))[0][0] == 1


@pytest.mark.parametrize(("department", "clinic", "expect"), [
    ("valid", None, ("claimed", "department")),
    ("valid", "other", ("claimed", "department")),
    ("Mars/Olympus", "valid", ("claimed", "clinic_fallback")),
    (None, "valid", ("claimed", "clinic_fallback")),
    ("Mars/Olympus", "Mars/Olympus", ("skipped", "TZ_UNRESOLVED")),
    (None, None, ("skipped", "TZ_UNRESOLVED")),
], ids=["dept-clinic-null", "dept-wins", "invalid-dept-fallback", "null-dept-fallback",
        "both-invalid", "both-null"])
def test_h2_p13_timezone_resolution(clean_settings: sh2.SH2Install, department: str | None,
                                    clinic: str | None, expect: tuple[str, str]) -> None:
    h2 = clean_settings
    h2.require()
    zone, start = sh2.in_window(h2)
    clinic_zone = {"valid": zone, "other": "Pacific/Kiritimati", None: None}.get(clinic, clinic)
    if clinic_zone is not None:
        sh2.settings_raw(h2, clinic_tz=clinic_zone)
    dept = zone if department == "valid" else department
    _p, _ph, ref = sh2.seed(h2, start, zone=dept)
    result = sh2.examine(h2, ref)
    if expect[0] == "claimed":
        assert result["outcome"] == "claimed", result
        inputs = sh2.inputs_row(h2, result["op_id"])
        assert inputs is not None
        assert (inputs["zone"], inputs["zone_source"]) == (zone, expect[1])
    else:
        assert (result["outcome"], result["code"]) == expect
        assert sh2.op_for(h2, ref) is None


def _skip_expect(install: sa.SAInstall, ref: str, code: str, t0: datetime) -> None:
    row = sh2.schedule_row(install, ref)
    assert row is not None and row["last_reason"] == code, row
    assert row["examined_count"] >= 1
    assert t0 + sh2.RECHECK <= sh2.next_check(install, ref) <= sh2.db_now(install) + sh2.RECHECK
    assert sh2.op_for(install, ref) is None


@pytest.mark.parametrize(("kw", "code"), [
    ({"source_status": "completed"}, "COMPLETED"),
    ({"source_status": "no_show"}, "STATUS_UNSUPPORTED"),
    ({"source_status": None}, "STATUS_UNSUPPORTED"),
    ({"first_name": None}, "NAME_INVALID"),
    ({"phones": 0}, "UNROUTABLE_PHONE"),
    ({"phones": 2}, "UNROUTABLE_PHONE"),
    ({"confirmation": "confirmed"}, "CONFIRMED"),
], ids=["completed", "unsupported", "status-null", "name-null", "no-phone", "two-phones",
        "confirmed"])
def test_h2_p14_mapping_skips(h2: sh2.SH2Install, kw: dict[str, Any], code: str) -> None:
    h2.require()
    zone, start = sh2.in_window(h2)
    _p, _ph, ref = sh2.seed(h2, start, zone=zone, **kw)
    t0 = sh2.db_now(h2)
    result = sh2.examine(h2, ref)
    assert (result["outcome"], result["code"]) == ("skipped", code)
    _skip_expect(h2, ref, code, t0)


@pytest.mark.parametrize("name", sh2.NAMES_BAD, ids=[f"bad{i}" for i in range(len(sh2.NAMES_BAD))])
def test_h2_p14b_sql_name_rule_refuses(h2: sh2.SH2Install, name: str) -> None:
    h2.require()
    zone, start = sh2.in_window(h2)
    _p, _ph, ref = sh2.seed(h2, start, zone=zone, first_name=name)
    result = sh2.examine(h2, ref)
    assert (result["outcome"], result["code"]) == ("skipped", "NAME_INVALID")


@pytest.mark.parametrize("name", sh2.NAMES_OK, ids=[f"ok{i}" for i in range(len(sh2.NAMES_OK))])
def test_h2_p14c_sql_name_rule_accepts(h2: sh2.SH2Install, name: str) -> None:
    h2.require()
    zone, start = sh2.in_window(h2)
    _p, _ph, ref = sh2.seed(h2, start, zone=zone, first_name=name)
    result = sh2.examine(h2, ref)
    assert result["outcome"] == "claimed", result
    assert sh2.inputs_row(h2, result["op_id"])["first_name"] == name  # type: ignore[index]


def test_h2_p14d_opted_out_cancelled_shared_phone(h2: sh2.SH2Install) -> None:
    h2.require()
    zone, start = sh2.in_window(h2)
    # Opted out (STOP recorded for the phone) -> skipped OPTED_OUT, no op.
    _p, phone, ref = sh2.seed(h2, start, zone=zone)
    assert phone is not None
    sh.inbound(h2, sa.new_id(), phone, "stop")
    t0 = sh2.db_now(h2)
    assert sh2.examine(h2, ref)["code"] == "OPTED_OUT"
    _skip_expect(h2, ref, "OPTED_OUT", t0)
    # Cancelled: directory v2 removes the schedule row; examine -> NOT_SCHEDULED.
    patient, _ph2, ref2 = sh2.seed(h2, start, zone=zone)
    sh2.appointment_put(h2, ref2, patient, start, department_tz=zone, status="cancelled")
    assert sh2.schedule_row(h2, ref2) is None
    before = sh2.h2_snapshot(h2)
    assert sh2.examine(h2, ref2) == {"schema_version": 1, "outcome": "not_ready",
                                     "code": "NOT_SCHEDULED"}
    assert sh2.h2_snapshot(h2) == before
    # A phone shared by two patients is NOT suppressed: both claim.
    shared = sa.fresh_phone()
    _a, _pa, ref_a = sh2.seed(h2, start, zone=zone, phone=shared)
    _b, _pb, ref_b = sh2.seed(h2, start, zone=zone, phone=shared)
    assert sh2.examine(h2, ref_a)["outcome"] == "claimed"
    assert sh2.examine(h2, ref_b)["outcome"] == "claimed"


# --- P15..P18: the runner ------------------------------------------------------------------


def _render(inputs: Any) -> str:
    from haloflow.m05.reminder_text import reminder_text

    return reminder_text(inputs["first_name"], _ts(inputs["start_at"]), inputs["zone"],
                         inputs["clinic_display_name"])


async def _runs(install: sh2.SH2Install, times: int, batch: int) -> tuple[list[Any], Any]:
    import sc_support as sc

    from haloflow.m05.outbound import FakeSender, OutboundWorker
    from haloflow.m05.reminder import ReminderRunner

    sc.change(install, "grant")
    reports = []
    sender = FakeSender()
    async with sh2.runtime(install) as rt:
        svc = sh2.service(rt)
        runner = ReminderRunner(svc, OutboundWorker(svc, sender, _render), batch=batch,
                                lease_seconds=120)
        for _ in range(times):
            ctx = await sh2.dispatch_context(rt, install)
            reports.append(await runner.run_once(ctx))
    return reports, sender


def test_h2_p15_backlog_bound(h2_runner: sh2.SH2Install) -> None:
    h2_runner.require()
    sh2.clear_schedule(h2_runner)
    zone, start = sh2.in_window(h2_runner)
    skips = [sh2.seed(h2_runner, start, zone=zone, first_name=None)[2] for _ in range(4)]
    sendable = [sh2.seed(h2_runner, start, zone=zone)[2] for _ in range(2)]
    reports, sender = asyncio.run(_runs(h2_runner, 2, batch=2))
    assert sender.calls == []  # order preserved: four skips first
    assert [r.examined for r in reports] == [2, 2]
    reports, sender = asyncio.run(_runs(h2_runner, 1, batch=2))
    assert (reports[0].examined, reports[0].claimed, reports[0].sent) == (2, 2, 2)
    assert sorted(c[0] for c in sender.calls) == sorted(
        str(sh2.op_for(h2_runner, r)["op_id"]) for r in sendable)  # type: ignore[index]
    for ref in skips:
        assert sh2.schedule_row(h2_runner, ref)["last_reason"] == "NAME_INVALID"  # type: ignore[index]


def test_h2_p16_churn_rekeys(h2_runner: sh2.SH2Install, env: db.L1WEnv) -> None:
    h2_runner.require()
    sh2.clear_schedule(h2_runner)
    now = sh2.db_now(h2_runner)
    zone = sh2.zone_with_local_hour(now, 11, 15)
    start = sh2.at_local(now, zone, 3, 18)  # due tomorrow 10:00 (lead 2): not yet ready
    rows = [sh2.seed(h2_runner, start, zone=zone) for _ in range(3)]
    due = sh2.at_local(now, zone, 1, 10)
    for _p, _ph, ref in rows:
        assert sh2.next_check(h2_runner, ref) == due
    (p2, _x, r2), (p3, _y, r3) = rows[1], rows[2]
    t0 = sh2.db_now(h2_runner)
    sh2.appointment_put(h2_runner, r2, p2, start, department_tz=zone, first_name="Changed")
    assert sh2.next_check(h2_runner, r2) >= t0
    sh2.appointment_put(h2_runner, r3, p3, start, department_tz=zone, status="cancelled")
    assert sh2.schedule_row(h2_runner, r3) is None
    try:
        with sh2.support_login(env, h2_runner, "p16") as support:
            t1 = sh2.db_now(h2_runner)
            sh2.settings_put(support, h2_runner, lead_days=3)  # due moves to today 10:00
        for _p, _ph, ref in rows[:2]:
            assert t1 <= sh2.next_check(h2_runner, ref) <= sh2.db_now(h2_runner)
        assert set(sh2.ready(h2_runner)) >= {rows[0][2], r2}
    finally:
        sh2.reset_settings(h2_runner)


def test_h2_p17_two_concurrent_runners(h2_runner: sh2.SH2Install) -> None:
    import sc_support as sc

    h2_runner.require()
    sh2.clear_schedule(h2_runner)
    sc.change(h2_runner, "grant")
    zone, start = sh2.in_window(h2_runner)
    refs = [sh2.seed(h2_runner, start, zone=zone)[2] for _ in range(6)]

    async def body() -> list[Any]:
        from haloflow.m05.outbound import FakeSender, OutboundWorker
        from haloflow.m05.reminder import ReminderRunner

        senders = [FakeSender(), FakeSender()]
        async with sh2.runtime(h2_runner) as rt_a, sh2.runtime(h2_runner) as rt_b:
            runners = []
            for rt, sender in zip((rt_a, rt_b), senders, strict=True):
                svc = sh2.service(rt)
                runners.append((rt, ReminderRunner(svc, OutboundWorker(svc, sender, _render),
                                                   batch=3, lease_seconds=120)))
            for _ in range(3):
                ctxs = [await sh2.dispatch_context(rt, h2_runner) for rt, _r in runners]
                await asyncio.gather(*(r.run_once(c) for (_rt, r), c in
                                       zip(runners, ctxs, strict=True)))
        return [call for s in senders for call in s.calls]

    calls = asyncio.run(body())
    op_ids = [str(sh2.op_for(h2_runner, r)["op_id"]) for r in refs]  # type: ignore[index]
    assert sorted(c[0] for c in calls) == sorted(op_ids)  # every ref once, none twice


def test_h2_p18_lock_order_no_deadlock(clean_settings: sh2.SH2Install, env: db.L1WEnv) -> None:
    h2 = clean_settings
    h2.require()
    patient, _ph, ref, zone = _sendable(h2)
    start = sh.start_of(h2, ref)
    snap = sh2.render_snapshot(h2, ref)
    with sh2.support_login(env, h2, "p18") as support:
        with sa.held(h2, "m05_outbound_examine", sh2.examine_cmd(ref, snap=snap)) as held:
            assert held["outcome"] == "claimed"
            directory = sa.Waiter(h2, "m05_directory_v2", sa.directory_cmd(
                "appointment_put", **sh2.appointment_fields(
                    ref, patient, start, department_tz=zone,
                    first_name="After"))).start_and_block()
            settings = sh2.LoginWaiter(h2, support, sh2.SUPPORT_FUNCTION,
                                       sh2.settings_cmd(window_end="18:00")).start_and_block()
        assert directory.join()["outcome"] == "applied"
        assert settings.join()["outcome"] == "applied"
    assert sh.op_row(h2, held["op_id"])["state"] == "attempting"
    assert sh2.profile_row(h2, ref)["first_name"] == "After"  # type: ignore[index]
    assert sh2.settings_row(h2)["window_end"][:5] == "18:00"


# --- P19, P20: crash and bootstrap ---------------------------------------------------------


class _Abort(Exception):
    pass


def test_h2_p19_crash_recovery(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    row_before = sh2.schedule_row(h2, ref)
    with pytest.raises(_Abort), sa.held(h2, "m05_outbound_examine",
                                        sh2.examine_cmd(ref, snap=sh2.render_snapshot(h2, ref))):
        raise _Abort  # killed before commit: the transaction rolls back
    assert sh2.op_for(h2, ref) is None
    assert sh2.schedule_row(h2, ref) == row_before
    op_id, attempt = sh2.claimed(h2, ref)  # committed; the process dies
    sh2.expire_lease(h2, op_id)
    sh.expire(h2)
    assert sh.op_row(h2, op_id)["state"] == "uncertain"
    assert sh2.consume(h2, op_id, attempt)["code"] == "CLAIM_STALE"
    assert sh2.examine(h2, ref)["code"] == "NOT_SCHEDULED"


def _upgrade(install: sa.SAInstall) -> None:
    from staff_support import staff_manifest

    from haloflow.m01.provisioning import TenantMigrationRunner

    runner = TenantMigrationRunner(m02_support._connection_factory(
        install.ids.logins[sa.MIGRATOR]), sh2.sh2_registry(), manifest=staff_manifest())
    asyncio.run(runner.apply(tenant_id=install.tenant_id, schema_key=install.schema))


def test_h2_p20_bootstrap_on_upgrade(request: pytest.FixtureRequest,
                                     m02_ids: m02_support.Identities, m05_reset: Any) -> None:
    for old in sh.module_install(request, m02_ids, m05_reset, "h2-p20"):
        old.require()  # target 56; the S-A directory is open here
        _pa, _pha, (ref_a,) = sa.fresh_patient(old)
        _pb, _phb, (ref_b,) = sa.fresh_patient(old)
        _pc, _phc, (ref_c,) = sa.fresh_patient(old)
        pd, _phd, (ref_d,) = sa.fresh_patient(old)
        h1_prepared = sh.prepare(old, ref_b)
        sh.to_state(old, ref_c, "attempting")
        sa.directory(old, "appointment_put", appointment_ref=ref_d, patient_id=pd,
                     start_at=sa.FUTURE.format(n=1), status="cancelled")
        t0 = sh2.db_now(old)
        _upgrade(old)
        sh2.settings_raw(old, clinic_display_name=sh2.CLINIC)
        queued = {str(r[0]): r[1] for r in sa.admin(old, sa.q(
            old, "SELECT appointment_ref, next_check_at FROM {s}.m05_outbound_schedule"))}
        assert set(queued) == {ref_a, ref_b}
        assert all(t0 <= v <= sh2.db_now(old) for v in queued.values())
        assert sh2.examine(old, ref_a)["code"] == "PROFILE_MISSING"
        result = sh2.examine(old, ref_b)
        assert (result["outcome"], result["code"]) == ("removed", "NO_RENDER_INPUTS")
        assert (sh.op_row(old, h1_prepared["op_id"])["state"],
                sh.op_row(old, h1_prepared["op_id"])["code"]) == ("abandoned", "NO_RENDER_INPUTS")
        assert sh.op_row(old, sh.op_row_by_ref(old, ref_c))["state"] == "attempting"
        # Pin 6: a new H2 prepared op's inputs survive a re-run of the migration runner.
        zone, start = sh2.in_window(old)
        _pe, _phe, ref_e = sh2.seed(old, start, zone=zone)
        op = sh2.prepare_rendered(old, ref_e)
        stored = sh2.inputs_row(old, op["op_id"])
        _upgrade(old)  # idempotent re-run (restart)
        assert sh2.inputs_row(old, op["op_id"]) == stored
        assert sh2.examine(old, ref_e)["op_id"] == op["op_id"]


# --- P21..P23: settings authority, ACL closure, activation ---------------------------------


def _support_refused(install: sh2.SH2Install, conninfo: str, command: Any, code: str) -> None:
    before = (sh2.h2_snapshot(install), sh2.settings_row(install))
    with pytest.raises(psycopg.Error) as refused:
        sh2.call_as(conninfo, install, sh2.SUPPORT_FUNCTION, command)
    assert (refused.value.sqlstate, _code(refused.value)) == ("P0001", code)
    assert (sh2.h2_snapshot(install), sh2.settings_row(install)) == before


@pytest.mark.parametrize("values", [
    {"lead_days": 1}, {"lead_days": 4}, {"window_start": "08:30"}, {"window_end": "19:30"},
    {"send_time": "08:00"}, {"send_time": "19:00"}, {"window_start": "15:00",
                                                     "window_end": "12:00"},
    {"clinic_tz": "Mars/Olympus"}, {"clinic_display_name": "x" * 61},
    {"clinic_display_name": "Clinic\u200b"}, {"clinic_display_name": ""},
    {"send_time": "10:00:30"},
], ids=["lead1", "lead4", "widen-start", "widen-end", "send-before", "send-at-end",
        "inverted", "tz", "name-long", "name-format", "name-empty", "seconds"])
def test_h2_p21_settings_validation(clean_settings: sh2.SH2Install, env: db.L1WEnv,
                                    values: dict[str, Any]) -> None:
    h2 = clean_settings
    h2.require()
    with sh2.support_login(env, h2, "p21v") as support:
        _support_refused(h2, support, sh2.settings_cmd(**values), "SETTINGS_INVALID")


def test_h2_p21b_settings_applied_and_malformed(clean_settings: sh2.SH2Install,
                                                env: db.L1WEnv) -> None:
    h2 = clean_settings
    h2.require()
    with sh2.support_login(env, h2, "p21a") as support:
        version = sh2.settings_row(h2)["settings_version"]
        result = sh2.settings_put(support, h2, lead_days=3, window_start="10:00",
                                  window_end="17:00", send_time="11:00", clinic_tz=sh2.NY)
        assert result["outcome"] == "applied"
        row = sh2.settings_row(h2)
        assert (row["lead_days"], row["window_end"][:5], row["clinic_tz"],
                row["settings_version"]) == (3, "17:00", sh2.NY, version + 1)
        cmd = sh2.settings_cmd()
        del cmd["lead_days"]
        _support_refused(h2, support, cmd, "COMMAND_MALFORMED")


def test_h2_p21c_support_authority(clean_settings: sh2.SH2Install, env: db.L1WEnv,
                                   h2_other: sh2.SH2Install) -> None:
    h2 = clean_settings
    h2.require()
    h2_other.require()
    cmd = sh2.settings_cmd(lead_days=3)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        sa.call(h2, sh2.SUPPORT_FUNCTION, cmd)  # the runtime role
    env.login("p21-unlisted", db.SUPPORT)  # a support member that is not allow-listed
    _support_refused(h2, env.conninfo("p21-unlisted"), cmd, "SUPPORT_NOT_AUTHORIZED")
    with sh2.support_login(env, h2, "p21-nogrant", caps=None) as c:
        _support_refused(h2, c, cmd, "SUPPORT_NOT_AUTHORIZED")
    with sh2.support_login(env, h2_other, "p21-other") as c:  # grant for ANOTHER tenant
        _support_refused(h2, c, cmd, "SUPPORT_NOT_AUTHORIZED")
    with sh2.support_login(env, h2, "p21-expired", expires=timedelta(seconds=-1)) as c:
        _support_refused(h2, c, cmd, "SUPPORT_NOT_AUTHORIZED")
    with sh2.support_login(env, h2, "p21-revoked", state="revoked") as c:
        _support_refused(h2, c, cmd, "SUPPORT_NOT_AUTHORIZED")
    with sh2.support_login(env, h2, "p21-cap", caps=("l1w.roles.bootstrap",)) as c:
        _support_refused(h2, c, cmd, "SUPPORT_NOT_AUTHORIZED")
    # Expiry while waiting for the settings lock: authorization re-checked after the lock.
    with sh2.support_login(env, h2, "p21-wait", expires=timedelta(seconds=3)) as c:
        before = (sh2.h2_snapshot(h2), sh2.settings_row(h2))
        expiry = sh2.db_now(h2) + timedelta(seconds=3)
        with _settings_locked(h2):
            waiter = sh2.LoginWaiter(h2, c, sh2.SUPPORT_FUNCTION, cmd).start_and_block()
            sh2.wait_past(h2, expiry + timedelta(seconds=1))
        with pytest.raises(psycopg.Error) as late:
            waiter.join()
        assert _code(late.value) == "SUPPORT_NOT_AUTHORIZED"
        assert (sh2.h2_snapshot(h2), sh2.settings_row(h2)) == before


CLINIC_BAD = tuple(n for n in sh2.NAMES_BAD if n != "x" * 41) + ("x" * 61,)


@pytest.mark.parametrize("name", CLINIC_BAD, ids=[f"bad{i}" for i in range(len(CLINIC_BAD))])
def test_h2_p21d_clinic_name_rule(clean_settings: sh2.SH2Install, env: db.L1WEnv,
                                  name: str) -> None:
    h2 = clean_settings
    h2.require()
    with sh2.support_login(env, h2, "p21d") as support:
        _support_refused(h2, support, sh2.settings_cmd(clinic_display_name=name),
                         "SETTINGS_INVALID")


@pytest.mark.parametrize("name", [n for _u, n in sh2.CLOSED] + list(sh2.OWNER_FUNCTIONS))
def test_h2_p22_runtime_denied(h2: sh2.SH2Install, name: str) -> None:
    h2.require()
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
        sa.call(h2, name, {"schema_version": 1})
    assert denied.value.sqlstate == "42501"


def _activation_code(error: BaseException) -> str:
    from haloflow.m01.errors import ActivationRefused

    assert type(error) is ActivationRefused, repr(error)
    code: str = error.reason_code
    return code


def test_h2_p23_deactivated_config_only(h2: sh2.SH2Install, env: db.L1WEnv) -> None:
    import sc_support as sc

    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)  # an attempt that exists before deactivation
    _p2, _ph2, ref2, _z2 = _sendable(h2)
    sc.change(h2, "revoke")
    try:
        def counts() -> tuple[int, int, int]:
            return tuple(sa.admin(h2, sa.q(h2, "SELECT (SELECT count(*) FROM {s}."
                                               "m05_outbound_ops), (SELECT count(*) FROM {s}."
                                               "m05_outbound_send_tickets WHERE consumed_at IS"
                                               " NOT NULL), (SELECT count(*) FROM {s}."
                                               "m05_outbound_render_inputs)"))[0])  # type: ignore[return-value]

        before = counts()
        with sh2.support_login(env, h2, "p23") as support:
            t0 = sh2.db_now(h2)
            assert sh2.settings_put(support, h2, lead_days=3)["outcome"] == "applied"
        assert sh2.next_check(h2, ref2) >= t0  # re-keyed
        assert counts() == before

        async def body() -> list[str]:
            async with sh2.runtime(h2) as rt:
                svc = sh2.service(rt)
                ctx = await sh2.dispatch_context(rt, h2)
                codes = []
                for call in (lambda: svc.examine(ctx, ref2, op_id=sa.new_id(),
                                                 reminder_id=sa.new_id(), lease_seconds=120,
                                                 text_sha256="0" * 64, source_version=1,
                                                 settings_version=1),
                             lambda: svc.consume(ctx, op_id, attempt),
                             lambda: svc.claim_v2(ctx, op_id, 0)):
                    with pytest.raises(Exception) as refused:
                        await call()
                    codes.append(_activation_code(refused.value))
                return codes

        assert asyncio.run(body()) == ["M05_NOT_ACTIVATED"] * 3
        assert counts() == before
        assert sh2.ticket_row(h2, op_id)["consumed_at"] is None  # type: ignore[index]
    finally:
        sc.change(h2, "grant")
        sh2.reset_settings(h2)


# --- P24..P27: the one-shot ticket ---------------------------------------------------------


async def _worker(install: sh2.SH2Install, rt: Any, sender: Any) -> tuple[Any, Any]:
    from haloflow.m05.outbound import OutboundWorker

    return OutboundWorker(sh2.service(rt), sender, _render), await sh2.dispatch_context(
        rt, install)


def test_h2_p24_concurrent_run_claimed(h2: sh2.SH2Install) -> None:
    import sc_support as sc

    from haloflow.m05.outbound import FakeSender

    h2.require()
    sc.change(h2, "grant")
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)

    async def body() -> tuple[list[str], int]:
        sender = FakeSender()
        async with sh2.runtime(h2) as rt:
            worker, ctx = await _worker(h2, rt, sender)
            results = await asyncio.gather(worker.run_claimed(ctx, op_id, attempt),
                                           worker.run_claimed(ctx, op_id, attempt))
        return sorted(results), len(sender.calls)

    assert asyncio.run(body()) == (["accepted", "not_sent"], 1)


def test_h2_p24b_paused_sql_winner(h2: sh2.SH2Install) -> None:
    """A winner that holds its consume transaction (paused before commit) blocks the
    service loser on the op row; after the winner commits `send`, the loser gets
    TICKET_SPENT and sends nothing."""

    import sc_support as sc

    from haloflow.m05.outbound import FakeSender

    h2.require()
    sc.change(h2, "grant")
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)
    sender = FakeSender()
    loser: dict[str, Any] = {}

    def run_loser() -> None:
        async def body() -> str:
            async with sh2.runtime(h2) as rt:
                worker, ctx = await _worker(h2, rt, sender)
                result: str = await worker.run_claimed(ctx, op_id, attempt)
                return result

        try:
            loser["result"] = asyncio.run(body())
        except BaseException as error:  # noqa: BLE001 - re-raised below
            loser["error"] = error

    import threading

    with sa.held(h2, "m05_outbound_consume", sh2.consume_cmd(op_id, attempt)) as won:
        assert won["outcome"] == "send"
        thread = threading.Thread(target=run_loser, daemon=True)
        thread.start()
        _wait_any_blocked(h2)
    thread.join(timeout=30)
    assert "error" not in loser, loser
    assert (loser["result"], sender.calls) == ("not_sent", [])


def _wait_any_blocked(install: sa.SAInstall, timeout_s: float = 20.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = sa.admin(install, "SELECT count(*) FROM pg_stat_activity WHERE"
                                 " wait_event_type = 'Lock' AND datname = current_database()")
        if rows[0][0]:
            return
        time.sleep(0.02)
    raise AssertionError("the loser never blocked on the op row")


def test_h2_p24c_paused_sending_winner(h2: sh2.SH2Install) -> None:
    import sc_support as sc

    from haloflow.m05.outbound import Accepted

    h2.require()
    sc.change(h2, "grant")
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)

    class PausingSender:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, op: str, phone: str, text: str) -> Any:
            self.calls.append(op)
            self.entered.set()
            await self.release.wait()
            return Accepted("fake-p24c")

    async def body() -> tuple[str, str, int]:
        sender = PausingSender()
        async with sh2.runtime(h2) as rt:
            worker, ctx = await _worker(h2, rt, sender)
            winner = asyncio.create_task(worker.run_claimed(ctx, op_id, attempt))
            await sender.entered.wait()  # consume committed; the winner is inside send
            loser = await worker.run_claimed(ctx, op_id, attempt)
            sender.release.set()
            return await winner, loser, len(sender.calls)

    assert asyncio.run(body()) == ("accepted", "not_sent", 1)


def test_h2_p25_duplicate_delivery(h2: sh2.SH2Install) -> None:
    import sc_support as sc

    from haloflow.m05.outbound import FakeSender

    h2.require()
    sc.change(h2, "grant")
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)

    async def body() -> tuple[str, str, int]:
        sender = FakeSender()
        async with sh2.runtime(h2) as rt:
            worker, ctx = await _worker(h2, rt, sender)
            first = await worker.run_claimed(ctx, op_id, attempt)
            second = await worker.run_claimed(ctx, op_id, attempt)
        return first, second, len(sender.calls)

    assert asyncio.run(body()) == ("accepted", "not_sent", 1)


def test_h2_p26_restart_after_consume(h2: sh2.SH2Install) -> None:
    import sc_support as sc

    from haloflow.m05.outbound import FakeSender

    h2.require()
    sc.change(h2, "grant")
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)
    assert sh2.consume(h2, op_id, attempt)["outcome"] == "send"  # process A, then it dies

    async def body() -> tuple[str, int]:
        sender = FakeSender()
        async with sh2.runtime(h2) as rt:  # process B
            worker, ctx = await _worker(h2, rt, sender)
            return await worker.run_claimed(ctx, op_id, attempt), len(sender.calls)

    assert asyncio.run(body()) == ("not_sent", 0)
    sh2.expire_lease(h2, op_id)
    sh.expire(h2)
    assert sh.op_row(h2, op_id)["state"] == "uncertain"


class _FaultFoundation:
    """Wraps EventFoundation; the first transaction fails its acknowledgement either
    after the commit or by rolling back after the body completed."""

    def __init__(self, inner: Any, mode: str) -> None:
        self._inner, self._mode, self._faults = inner, mode, 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def run_in_transaction(self, context: Any, body: Any) -> Any:
        if self._faults == 0:
            return await self._inner.run_in_transaction(context, body)
        self._faults -= 1
        if self._mode == "after_commit":
            await self._inner.run_in_transaction(context, body)
            raise ConnectionError("acknowledgement lost")

        async def then_fail(binding: Any) -> Any:
            await body(binding)
            raise ConnectionError("connection lost before commit")

        return await self._inner.run_in_transaction(context, then_fail)


@pytest.mark.parametrize("mode", ["after_commit", "rollback"])
def test_h2_p27_consume_commit_unknown(h2: sh2.SH2Install, mode: str) -> None:
    import sc_support as sc

    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.outbound import FakeSender, OutboundWorker
    from haloflow.m05.reminder import ReminderService

    h2.require()
    sc.change(h2, "grant")
    _p, _ph, ref, _zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)

    async def body() -> tuple[str, int]:
        sender = FakeSender()
        async with sh2.runtime(h2) as rt:
            foundation = _FaultFoundation(
                EventFoundation(rt.gateway, producer_service=sa.PRODUCER), mode)
            worker = OutboundWorker(ReminderService(foundation), sender, _render)
            ctx = await sh2.dispatch_context(rt, h2)
            return await worker.run_claimed(ctx, op_id, attempt), len(sender.calls)

    assert asyncio.run(body()) == ("consume_unknown", 0)
    ticket = sh2.ticket_row(h2, op_id)
    assert ticket is not None
    assert (ticket["consumed_at"] is not None) is (mode == "after_commit")
    sh2.expire_lease(h2, op_id)
    sh.expire(h2)
    assert sh.op_row(h2, op_id)["state"] == "uncertain"
    assert sh2.consume(h2, op_id, attempt)["outcome"] == "refused"  # never `send`


# --- P32..P34: H1 recipient/consent safety on the consume path (Codex d350083 item 3) ---


def _after_claim(install: sa.SAInstall, kind: str, patient: str, phone: str, ref: str,
                 zone: str) -> str:
    """One change between claim and consume; returns the H1 D1 code it must produce."""

    start = sh.start_of(install, ref)
    if kind == "stop":
        sh.inbound(install, sa.new_id(), phone, "stop")
        return "OPTED_OUT"
    if kind == "phone_swap":
        sh2.directory2(install, "contact_remove", phone=phone, patient_id=patient)
        sh2.contact_put(install, sa.fresh_phone(), patient)
        return "IDENTITY_CHANGED"
    if kind == "reassigned":
        other = sa.new_id()
        sh2.contact_put(install, sa.fresh_phone(), other)
        sh2.appointment_put(install, ref, other, start, department_tz=zone)
        return "IDENTITY_CHANGED"
    if kind == "start_moved":
        sh2.appointment_put(install, ref, patient, start + timedelta(days=1), department_tz=zone)
        return "APPOINTMENT_CHANGED"
    if kind == "cancelled":
        sh2.appointment_put(install, ref, patient, start, department_tz=zone, status="cancelled")
        return "APPOINTMENT_CHANGED"
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["stop", "phone_swap", "reassigned", "start_moved",
                                  "cancelled"])
def test_h2_p32_change_after_claim_never_sends(h2: sh2.SH2Install, kind: str) -> None:
    """consume re-checks the H1 D1 predicate under the contact lock (pins v1r2 D11): a STOP
    or a recipient / appointment change after claim -> no send, no registration."""

    import sc_support as sc

    from haloflow.m05.outbound import FakeSender

    h2.require()
    sc.change(h2, "grant")
    patient, phone, ref, zone = _sendable(h2)
    assert phone is not None
    op_id, attempt = sh2.claimed(h2, ref)
    code = _after_claim(h2, kind, patient, phone, ref, zone)

    async def body() -> tuple[str, int]:
        sender = FakeSender()
        async with sh2.runtime(h2) as rt:
            worker, ctx = await _worker(h2, rt, sender)
            return await worker.run_claimed(ctx, op_id, attempt), len(sender.calls)

    assert asyncio.run(body()) == ("not_sent", 0)
    row = sh.op_row(h2, op_id)
    assert (row["state"], row["code"], row["correlation_open"]) == ("refused", code, False)
    assert sh.windows_for(h2, ref) == []
    ticket = sh2.ticket_row(h2, op_id)
    assert ticket is not None and ticket["consumed_at"] is not None


def test_h2_p34_frozen_inputs_after_edits(clean_settings: sh2.SH2Install) -> None:
    """Name and clinic-name edits after claim do not change the text: the send uses the
    frozen recipient, start and render inputs, and its hash equals the durable hash."""

    import hashlib

    import sc_support as sc

    from haloflow.m05.outbound import FakeSender
    from haloflow.m05.reminder_text import reminder_text

    h2 = clean_settings
    h2.require()
    sc.change(h2, "grant")
    patient, phone, ref, zone = _sendable(h2)
    start = sh.start_of(h2, ref)
    op_id, attempt = sh2.claimed(h2, ref)
    durable = sh.op_row(h2, op_id)
    sh2.appointment_put(h2, ref, patient, start, department_tz=zone, first_name="Edited")
    sh2.settings_raw(h2, clinic_display_name="Edited Clinic")

    async def body() -> tuple[str, list[Any]]:
        sender = FakeSender()
        async with sh2.runtime(h2) as rt:
            worker, ctx = await _worker(h2, rt, sender)
            return await worker.run_claimed(ctx, op_id, attempt), sender.calls

    result, calls = asyncio.run(body())
    expected = reminder_text(sh2.FIRST_NAME, start, zone, sh2.CLINIC)
    assert result == "accepted"
    assert calls == [(op_id, durable["phone"], expected)]
    # to_jsonb renders the bytea column as "\\x<hex>"; compare the hex digits exactly.
    assert "\\x" + hashlib.sha256(expected.encode("utf-8")).hexdigest() == durable[
        "text_sha256"]
    assert phone == durable["phone"]


# --- P28..P31 -----------------------------------------------------------------------------


def test_h2_p28_stale_selection(clean_settings: sh2.SH2Install, env: db.L1WEnv) -> None:
    """Independent of how settings_put re-keys (Codex d350083 item 1): a FRESH examination
    moves the row into the future; the stale call, selected before, then writes nothing."""

    h2 = clean_settings
    h2.require()
    _p, _ph, ref, zone = _sendable(h2)
    assert ref in sh2.ready(h2, 1000)
    stale_snap = sh2.render_snapshot(h2, ref)  # the runner's selection and render
    with sh2.support_login(env, h2, "p28") as support:
        sh2.settings_put(support, h2, send_time="18:30")  # due later today (local 11-15 now)
    fresh = sh2.examine(h2, ref)  # a fresh examination: not due -> re-keyed to the due
    assert fresh["outcome"] in ("deferred", "not_ready"), fresh
    future = sh2.next_check(h2, ref)
    assert future == sh2.at_local(sh2.db_now(h2), zone, 0, 18, 30)
    assert future > sh2.db_now(h2)
    before = sh2.h2_snapshot(h2)
    result = sh2.examine(h2, ref, snap=stale_snap)
    assert (result["outcome"], result["code"]) == ("not_ready", "REKEYED")
    assert sh2.h2_snapshot(h2) == before
    assert sh2.next_check(h2, ref) == future


def test_h2_p29_no_render_inputs_bypass(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    start = sh.start_of(h2, ref)
    digest = sh.text_hash(sh.render(sh.FIRST_NAME, start))
    op = sh2.owner_call(h2, "m05_outbound_prepare",  # FIXTURE: an H1-shape op, no inputs
                        sh.prepare_cmd(sa.new_id(), ref, sa.new_id(), digest))
    sh2.refused(h2, "m05_outbound_claim_v2", sh.claim_cmd(op["op_id"], op["fence"]),
                "NO_RENDER_INPUTS")
    sh2.refused(h2, "m05_outbound_read_v2", sh2.read_v2_cmd(op["op_id"], sa.new_id()),
                "NO_RENDER_INPUTS")
    sh2.refused(h2, "m05_outbound_consume", sh2.consume_cmd(op["op_id"], sa.new_id()),
                "NO_RENDER_INPUTS")
    sh2.refused(h2, "m05_outbound_consume", {"schema_version": 1, "op_id": op["op_id"]},
                "COMMAND_MALFORMED")


def test_h2_p30_gateway_capabilities_and_isolation(h2: sh2.SH2Install,
                                                   h2_other: sh2.SH2Install) -> None:
    import service_support as sv

    h2.require()
    h2_other.require()
    _p, _ph, ref, _zone = _sendable(h2)
    before = sh2.h2_snapshot(h2)

    async def body() -> int:
        from haloflow.m05.outbound import OutboundRefused

        async with sh2.runtime(h2) as rt:
            svc = sh2.service(rt)
            wrong = await sv.context(rt, h2, sa.W_DISPATCH, sa.CAPS["inbound"], kind="workload")
            calls = (
                lambda: svc.examine(wrong, ref, op_id=sa.new_id(), reminder_id=sa.new_id(),
                                    lease_seconds=120, text_sha256="0" * 64,
                                    source_version=1, settings_version=1),
                lambda: svc.consume(wrong, sa.new_id(), sa.new_id()),
                lambda: svc.claim_v2(wrong, sa.new_id(), 0),
                lambda: svc.read_v2(wrong, sa.new_id(), sa.new_id()),
                lambda: svc.ready(wrong, 10),
                lambda: svc.render_snapshot(wrong, ref),
            )
            checked = 0
            for call in calls:
                with pytest.raises(OutboundRefused) as refused:
                    await call()
                assert refused.value.code == "CAPABILITIES_INVALID"
                checked += 1
            return checked

    assert asyncio.run(body()) == 6
    assert sh2.h2_snapshot(h2) == before


def test_h2_p30b_gateway_tenant_isolation(h2: sh2.SH2Install, h2_other: sh2.SH2Install
                                          ) -> None:
    """Identical patient / phone / appointment / op / reminder ids in tenants A and B with
    DISTINCT payloads ("Alpha" / "Bravo"), every new gateway entry plus the rebound
    directory key executed in both (Codex d350083 item 2)."""

    import sc_support as sc

    from haloflow.m05.outbound import OutboundRefused

    for install in (h2, h2_other):
        install.require()
        sc.change(install, "grant")
    zone, start = sh2.in_window(h2)
    patient, phone, ref, ref2 = sa.new_id(), sa.fresh_phone(), sa.new_id(), sa.new_id()
    op_id, reminder, op2, reminder2 = sa.new_id(), sa.new_id(), sa.new_id(), sa.new_id()
    only_b = sa.new_id()

    async def body() -> None:
        async with sh2.runtime(h2) as ra, sh2.runtime(h2_other) as rb:
            for rt, inst, name in ((ra, h2, "Alpha"), (rb, h2_other, "Bravo")):
                await sh2.gateway_directory(rt, inst, "contact_put", phone=phone,
                                            patient_id=patient)
                for r in (ref, ref2):
                    await sh2.gateway_directory(rt, inst, "appointment_put",
                                                **sh2.appointment_fields(
                                                    r, patient, start, department_tz=zone,
                                                    first_name=name))
            await sh2.gateway_directory(rb, h2_other, "appointment_put",
                                        **sh2.appointment_fields(only_b, patient, start,
                                                                 department_tz=zone))
            svc_a, svc_b = sh2.service(ra), sh2.service(rb)
            ca, cb = await sh2.dispatch_context(ra, h2), await sh2.dispatch_context(rb, h2_other)
            snap_a = await svc_a.render_snapshot(ca, ref)
            snap_b = await svc_b.render_snapshot(cb, ref)
            assert (snap_a["first_name"], snap_b["first_name"]) == ("Alpha", "Bravo")
            ready_a, ready_b = await svc_a.ready(ca, 1000), await svc_b.ready(cb, 1000)
            assert ref in ready_a["refs"] and ref in ready_b["refs"]
            assert only_b in ready_b["refs"] and only_b not in ready_a["refs"]
            # examine: A claims; B unchanged; then B claims its own record (same ids).
            before_b = sh2.h2_snapshot(h2_other)
            ea = await svc_a.examine(ca, ref, **sh2.examine_kwargs(
                snap_a, op_id=op_id, reminder_id=reminder))
            assert ea["outcome"] == "claimed"
            assert sh2.h2_snapshot(h2_other) == before_b
            eb = await svc_b.examine(cb, ref, **sh2.examine_kwargs(
                snap_b, op_id=op_id, reminder_id=reminder))
            assert eb["outcome"] == "claimed" and eb["attempt_id"] != ea["attempt_id"]
            # read v2: bound-tenant data only; A's attempt is stale in B.
            assert (await svc_a.read_v2(ca, op_id, ea["attempt_id"]))["first_name"] == "Alpha"
            with pytest.raises(OutboundRefused) as stale:
                await svc_b.read_v2(cb, op_id, ea["attempt_id"])
            assert stale.value.code == "CLAIM_STALE"
            # consume: B refuses A's attempt and spends nothing; both tickets stay usable.
            before_a, before_b = sh2.h2_snapshot(h2), sh2.h2_snapshot(h2_other)
            assert (await svc_b.consume(cb, op_id, ea["attempt_id"]))["code"] == "CLAIM_STALE"
            assert (sh2.h2_snapshot(h2), sh2.h2_snapshot(h2_other)) == (before_a, before_b)
            got_a = await svc_a.consume(ca, op_id, ea["attempt_id"])
            got_b = await svc_b.consume(cb, op_id, eb["attempt_id"])
            assert (got_a["outcome"], got_a["inputs"]["first_name"]) == ("send", "Alpha")
            assert (got_b["outcome"], got_b["inputs"]["first_name"]) == ("send", "Bravo")
            # claim v2: the same op id prepared in both; claiming in A leaves B prepared.
            sh2.prepare_rendered(h2, ref2, op_id=op2, reminder=reminder2)
            sh2.prepare_rendered(h2_other, ref2, op_id=op2, reminder=reminder2)
            before_b = sh2.h2_snapshot(h2_other)
            fence = sh.op_row(h2, op2)["fence"]
            assert (await svc_a.claim_v2(ca, op2, fence))["outcome"] == "claimed"
            assert sh2.h2_snapshot(h2_other) == before_b
            assert sh.op_row(h2_other, op2)["state"] == "prepared"

    asyncio.run(body())


def test_h2_p31_null_safety(clean_settings: sh2.SH2Install) -> None:
    h2 = clean_settings
    h2.require()
    zone, start = sh2.in_window(h2)
    sh2.settings_raw(h2, clinic_display_name=None)
    _p, _ph, ref = sh2.seed(h2, start, zone=zone)
    assert sh2.examine(h2, ref)["code"] == "SETTINGS_INCOMPLETE"
    sh2.reset_settings(h2)
    _p, _ph, ref = sh2.seed(h2, start, zone=None)  # clinic_tz is NULL by default
    assert sh2.examine(h2, ref)["code"] == "TZ_UNRESOLVED"
    _p, _ph, ref = sh2.seed(h2, start, zone=zone, confirmation=None)
    assert sh2.examine(h2, ref)["outcome"] == "claimed"  # NULL confirmation = unknown
    # No profile row: an appointment written by the owner-internal t038 (FIXTURE).
    patient, phone, ref = sa.new_id(), sa.fresh_phone(), sa.new_id()
    sh2.contact_put(h2, phone, patient)
    sh2.owner_call(h2, "m05_directory_put", sa.directory_cmd(
        "appointment_put", appointment_ref=ref, patient_id=patient, start_at=sh2.iso_z(start),
        status="booked"))
    assert sh2.profile_row(h2, ref) is None
    sa.admin(h2, sa.q(h2, "INSERT INTO {s}.m05_outbound_schedule (appointment_ref,"
                          " next_check_at, examined_count, updated_at) VALUES (%s,"
                          " clock_timestamp(), 0, clock_timestamp())"), (ref,))  # FIXTURE
    assert sh2.examine(h2, ref)["code"] == "PROFILE_MISSING"


def test_h2_p35_skip_locked_schedule_row(h2: sh2.SH2Install) -> None:
    h2.require()
    _p, _ph, ref, _zone = _sendable(h2)
    cmd = sh2.examine_cmd(ref, snap=sh2.render_snapshot(h2, ref))
    with psycopg.connect(h2.ids.admin) as holder, holder.transaction():
        holder.execute(sa.q(h2, "SELECT 1 FROM {s}.m05_outbound_schedule"
                                " WHERE appointment_ref = %s FOR UPDATE"), (ref,))  # FIXTURE
        before = sh2.h2_snapshot(h2)
        with sa.runtime_conn(h2) as conn, conn.transaction():
            conn.execute("SET LOCAL lock_timeout = '3s'")
            result = sa.call_in(conn, h2, "m05_outbound_examine", cmd)
        assert result == {"schema_version": 1, "outcome": "not_ready", "code": "LOCKED"}
        assert sh2.h2_snapshot(h2) == before
    assert sh2.examine(h2, ref)["outcome"] == "claimed"  # the lock released: examined


STATUS_CHANGES = (("completed", "COMPLETED"), ("no_show", "STATUS_UNSUPPORTED"),
                  (None, "STATUS_UNSUPPORTED"))


@pytest.mark.parametrize(("status", "code"), STATUS_CHANGES,
                         ids=["completed", "unsupported", "null"])
def test_h2_p36_status_change_after_prepare(h2: sh2.SH2Install, status: str | None,
                                            code: str) -> None:
    h2.require()
    patient, _ph, ref, zone = _sendable(h2)
    op = sh2.prepare_rendered(h2, ref)
    sh2.appointment_put(h2, ref, patient, sh.start_of(h2, ref), department_tz=zone,
                        source_status=status)
    result = sh2.examine(h2, ref)
    assert (result["outcome"], result["code"]) == ("removed", code)
    row = sh.op_row(h2, op["op_id"])
    assert (row["state"], row["code"]) == ("abandoned", code)
    assert sh2.ticket_row(h2, op["op_id"]) is None


@pytest.mark.parametrize(("status", "code"), STATUS_CHANGES,
                         ids=["completed", "unsupported", "null"])
def test_h2_p36b_status_change_after_claim(h2: sh2.SH2Install, status: str | None,
                                           code: str) -> None:
    import sc_support as sc

    from haloflow.m05.outbound import FakeSender

    h2.require()
    sc.change(h2, "grant")
    patient, _ph, ref, zone = _sendable(h2)
    op_id, attempt = sh2.claimed(h2, ref)
    sh2.appointment_put(h2, ref, patient, sh.start_of(h2, ref), department_tz=zone,
                        source_status=status)

    async def body() -> tuple[str, int]:
        sender = FakeSender()
        async with sh2.runtime(h2) as rt:
            worker, ctx = await _worker(h2, rt, sender)
            return await worker.run_claimed(ctx, op_id, attempt), len(sender.calls)

    assert asyncio.run(body()) == ("not_sent", 0)
    row = sh.op_row(h2, op_id)
    assert (row["state"], row["code"], row["correlation_open"]) == ("refused", code, False)
    assert sh.windows_for(h2, ref) == []
    ticket = sh2.ticket_row(h2, op_id)
    assert ticket is not None and ticket["consumed_at"] is not None


def test_h2_p37_snapshot_during_directory_update(h2: sh2.SH2Install) -> None:
    h2.require()
    patient, _ph, ref, zone = _sendable(h2)
    old_start = sh.start_of(h2, ref)
    new_start = old_start + timedelta(hours=1)
    update = sa.directory_cmd("appointment_put", **sh2.appointment_fields(
        ref, patient, new_start, department_tz=zone, first_name="During"))
    with sa.held(h2, "m05_directory_v2", update):  # the update is open, not committed
        during = sh2.render_snapshot(h2, ref)
    assert (_ts(during["start_at"]), during["first_name"]) == (old_start, sh2.FIRST_NAME)
    stale = sh2.examine(h2, ref, snap=during)
    assert (stale["outcome"], stale["code"]) == ("not_ready", "STALE_SNAPSHOT")
    assert sh2.op_for(h2, ref) is None
    op_id, _attempt = sh2.claimed(h2, ref)
    row = sh.op_row(h2, op_id)
    assert (_ts(row["start_at"]), sh2.inputs_row(h2, op_id)["first_name"]) == (  # type: ignore[index]
        new_start, "During")
