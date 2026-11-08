"""L-1S package S-H, checkpoint H1 — send/recovery contract, PostgreSQL 17 rows (RED first).

Governing: see `sh_support` docstring (decision table v1r2, Codex pins 1-6 and the H1
packet review). Synthetic only; no sender is called from SQL; nothing is sent; no EHR.
Each row is one scenario; a parametrised row counts once per parameter.

- H1-P00  install (versioned ingress, Codex ALIGN 20261108T145700Z): target 56 with units
          t047..t056 in order; no production overlap; production supported set {36};
          profiles exactly the typed S-H units; one amendment (t054 removes runtime
          EXECUTE from t040); every new function owner + runtime ACL; v1 body equal to
          an S-C tenant's, runtime privilege false (S-C control true); v2 holds the guard.
- H1-P00d a raw runtime call of v1 on an S-H tenant -> 42501.
- H1-P00e startup's per-tenant verification over the S-H registry (effective profiles)
          passes twice (restart); an S-C registry cannot install a tenant at 56.
- H1-P00f the unchanged IntakeService through the S-H runtime reaches the v2 guard via the
          same-key catalogue binding.
- H1-P00c an S-C runtime (supported {46}) refuses an S-H tenant: SCHEMA_VERSION_UNSUPPORTED.
- H1-P01  prepare: one op per appointment_ref (a second op_id returns the original);
          same op_id with a different hash -> OP_CONFLICT, zero writes; unroutable and
          opted-out refusals; snapshot frozen (patient, phone, start).
- H1-P01b concurrent prepare, two op_ids, held-lock seam: exactly one row.
- H1-P01c prepare lost acknowledgement: the identical retry returns the stored result.
- H1-P02  concurrent claim: one attempting; the loser CLAIM_STALE.
- H1-P03  claim after each directory change -> abandoned with the D1 code, no attempt.
- H1-P04  register after accepted, each change -> held (accepted_unregistered), zero
          windows, correlation stays open.
- H1-P05a register holds the frozen phone's contact lock; a concurrent contact swap
          demonstrably waits; register commits with phone A; then the swap applies.
- H1-P05b reverse: the directory swap holds first; register waits, then held
          IDENTITY_CHANGED, zero windows.
- H1-P06a lost acknowledgement: register commits (window + op registered in one
          transaction); after cancel, STOP and a phone swap, the retry returns the
          stored registration unchanged; exactly one window.
- H1-P06b rollback control: register inside a transaction that rolls back -> op still
          accepted, zero windows; the next call registers once.
- H1-P07  outcome bookkeeping: identical retry -> existing; conflicting -> OUTCOME_CONFLICT;
          stale attempt -> STALE_ATTEMPT; uncertain -> accepted (same attempt) allowed;
          on a terminal op an identical retry -> existing, conflicting -> STATE_TERMINAL.
- H1-P08  expire: an expired attempting op -> uncertain LEASE_EXPIRED (never prepared);
          a later claim -> CLAIM_STALE; read with the old attempt -> CLAIM_STALE.
- H1-P09  early-reply guard: each open state x 0/1/2 other current windows -> a
          non-STOP reply is unclear_reply, no attribution, no window change.
- H1-P09b the guard has no time expiry: an open op whose frozen start has passed plus
          another future appointment's window -> unclear_reply, that window unchanged.
- H1-P10  refused / abandoned (never sent) ops do not block: t040 result unchanged.
- H1-P11  STOP in each open state: opt-out persisted, windows opted_out (D-S2), never
          unclear_reply.
- H1-P12  replay precedence: a reply recorded before an op opens, replayed after, returns
          its stored disposition; a reply recorded while guarded, replayed after the op
          registered, returns its stored unclear disposition.
- H1-P13  serialization: claim holds the contact lock; a concurrent reply waits, then
          sees the open op -> unclear_reply.
- H1-P14  read: after the directory start and phone change, returns the ORIGINAL frozen
          start, phone and hash for the current attempt only.
- H1-P14b an attempting op whose lease passed is unreadable before any sweep (paused
          worker); its late outcome for the same attempt is still recordable.
- H1-P17  held: lists uncertain / accepted_unregistered / refused with exactly the
          HELD_COLUMNS keys (no phone, name or text).
- H1-P18  tenant isolation with IDENTICAL op/ref/reminder/patient/phone ids in two tenants:
          A's guard does not suppress B's matching; A's sweep expires A's expired attempt
          (control) but not B's genuinely expired attempt; neither tenant can read or
          record the other's attempt; A's registration leaves B's op and windows
          unchanged; held lists are per tenant.
- H1-P19  SQL least privilege: the ingress workload is refused by every outbound function
          (WORKLOAD_NOT_AUTHORIZED), protected or not.
- H1-P20  gateway: a dispatch principal with the wrong capability set is refused by every
          OutboundService method, bookkeeping included (CAPABILITIES_INVALID), zero writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import m02_support
import psycopg
import pytest
import sa_support as sa
import sh_support as sh

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


@pytest.fixture(scope="module")
def sh_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[sh.SHInstall]:
    yield from sh.module_install(request, m02_ids, m05_reset, "sh")


@pytest.fixture(scope="module")
def sh_other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
             m05_reset: Any) -> Iterator[sh.SHInstall]:
    yield from sh.module_install(request, m02_ids, m05_reset, "sh:other")


def _refused(install: sa.SAInstall, function: str, command: Any, code: str) -> None:
    sa.refused(install, function, command, code)


def _function_exists(install: sa.SAInstall, name: str) -> bool:
    rows = sa.admin(install, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                             " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                             " AND p.proname = %s", (install.schema, name))
    return bool(rows[0][0])


# --- H1-P00 ---------------------------------------------------------------------------------


def _prosrc(install: sa.SAInstall, name: str) -> str:
    rows = sa.admin(install, "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n"
                             " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                             " AND p.proname = %s", (install.schema, name))
    assert len(rows) == 1, rows
    text: str = rows[0][0]
    return text.replace(install.schema, "{schema}")


def _acl_of(install: sa.SAInstall, name: str) -> list[tuple[Any, ...]]:
    return sorted(sa.admin(install, (
        "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
        " x.privilege_type, pg_get_userbyid(x.grantor), x.is_grantable"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,"
        " LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x"
        " WHERE n.nspname = %s AND p.proname = %s"), (install.schema, name)))


def _runtime_can_execute(install: sa.SAInstall, name: str) -> bool:
    rows = sa.admin(install, "SELECT has_function_privilege(%s, p.oid, 'EXECUTE')"
                             " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                             " WHERE n.nspname = %s AND p.proname = %s",
                    (sa.RUNTIME, install.schema, name))
    return bool(rows[0][0])


def test_h1_p00_install_target_and_boundaries(sh_install: sh.SHInstall,
                                              request: pytest.FixtureRequest,
                                              m02_ids: m02_support.Identities,
                                              m05_reset: Any) -> None:
    import sc_support as sc

    from haloflow.composition import (
        APPROVED_ACL_AMENDMENTS,
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m05.outbound_units import (
        SH_ACL_AMENDMENTS,
        SH_INSTALLED_STATE_PROFILES,
        SH_TENANT_MIGRATIONS,
    )

    sh_install.require()
    # Exact unit sequence and target; nothing shared with production.
    assert tuple(SH_TENANT_MIGRATIONS) == sh.SH_UNIT_IDS
    assert sh.sh_registry().target_version == sh.SH_TARGET == 56
    production_ids = {unit_id for group in APPROVED_TENANT_MIGRATIONS for unit_id in group}
    assert not production_ids & set(sh.SH_UNIT_IDS)
    assert frozenset({36}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert not {a.activated_by for a in APPROVED_ACL_AMENDMENTS} & set(sh.SH_UNIT_IDS)
    # Profiles: every typed S-H unit, and none for the closure unit.
    typed = set(sh.SH_UNIT_IDS) - {"t047_m05_outbound_ops", "t054_m05_inbound_v1_runtime_closure"}
    assert set(SH_INSTALLED_STATE_PROFILES) == typed
    # Exactly one amendment: runtime EXECUTE removed from the unchanged t040 by t054.
    ((amendment,),) = (SH_ACL_AMENDMENTS,)
    assert (amendment.target_unit, amendment.activated_by, amendment.removes) == (
        sh.V1_INBOUND_UNIT, "t054_m05_inbound_v1_runtime_closure",
        ((sa.RUNTIME, "EXECUTE", sa.M05_OWNER, False),))
    for name in ("m05_outbound_prepare", "m05_outbound_claim", "m05_outbound_outcome",
                 "m05_outbound_expire", "m05_register_outbound", "m05_outbound_read",
                 "m05_outbound_held", sh.V2_INBOUND):
        assert _function_exists(sh_install, name), name
        assert _acl_of(sh_install, name) == sorted([
            (sa.M05_OWNER, "EXECUTE", sa.M05_OWNER, False),
            (sa.RUNTIME, "EXECUTE", sa.M05_OWNER, False)]), name
    # v1 stays installed, body unchanged (compared with an S-C tenant), runtime closed.
    for old in sc.module_install(request, m02_ids, m05_reset, "h1-p00:sc"):
        old.require()
        assert _prosrc(sh_install, "m05_register_inbound") == _prosrc(old, "m05_register_inbound")
        assert _runtime_can_execute(old, "m05_register_inbound") is True  # S-C control
    assert _acl_of(sh_install, "m05_register_inbound") == [
        (sa.M05_OWNER, "EXECUTE", sa.M05_OWNER, False)]
    assert _runtime_can_execute(sh_install, "m05_register_inbound") is False
    assert "m05_outbound_ops" in _prosrc(sh_install, sh.V2_INBOUND)


def test_h1_p00d_runtime_call_of_v1_denied(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, phone, (_ref,) = sa.fresh_patient(sh_install)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
        sa.inbound(sh_install, sa.new_id(), phone, "confirm")  # v1, raw runtime call
    assert denied.value.sqlstate == "42501"


def test_h1_p00e_restart_reverification_and_old_target_refusal(
        sh_install: sh.SHInstall, m02_ids: m02_support.Identities, m05_reset: Any) -> None:
    """Startup's own per-tenant verification over the S-H registry (effective profiles,
    incl. the t040 ACL as amended by t054) passes twice in a row; an S-C registry cannot
    install a tenant at 56 (no service use before complete installation)."""

    import sc_support as sc
    import sg_harness as sg

    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies
    from haloflow.startup import Deployment, _verify_served_tenant

    sh_install.require()

    async def verify_once() -> Any:
        pool = TenantPool(sh_install.ids.logins[sa.RUNTIME], min_size=1, max_size=2)
        await pool.open()
        try:
            deps = TenantRuntimeDependencies(
                pool=pool, control_store=PsycopgControlStore(pool),
                migrator_connect=sg._connector(sh_install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sg.forbid_provisioning())
            return await _verify_served_tenant(
                deps, tenant_id=sh_install.tenant_id, deployment=Deployment.NON_PRODUCTION,
                plan=sg.build_static_plan(sh.sh_registry()))
        finally:
            await pool.close()

    import asyncio

    for _ in range(2):  # restart: the second verification sees the same installed state
        outcome = asyncio.run(verify_once())
        assert outcome == (sh_install.tenant_id, sh_install.schema, sh.SH_TARGET), outcome
    fresh = m02_support.tenant_for("test_sh_h1_postgres:p00e-old")
    m05_reset(fresh)
    try:
        with pytest.raises(Exception) as caught:
            m02_support.provision_sync(m02_ids, sc.sc_registry(), fresh,
                                       supported=sh.SH_SUPPORTED)
        assert getattr(caught.value, "reason_code", None) == "SCHEMA_VERSION_UNSUPPORTED"
    finally:
        m05_reset(fresh)


def test_h1_p00f_gateway_intake_reaches_v2_guard(sh_install: sh.SHInstall) -> None:
    """The unchanged IntakeService, through the S-H catalogue's same-key binding, executes
    the v2 guard (not a raw helper)."""

    import sc_support as sc
    import service_support as sv

    sh_install.require()
    sc.change(sh_install, "grant")
    _patient, phone, (ref_a, ref_b) = sa.fresh_patient(sh_install, appointments=2)
    reminder = sa.new_id()
    sa.dispatch(sh_install, reminder, ref_b)
    sh.to_state(sh_install, ref_a, "uncertain")

    async def body() -> Any:
        from haloflow.m02.observation import EventFoundation
        from haloflow.m05.intake import InboundCommand, IntakeService

        async with sh.runtime(sh_install) as rt:
            intake = IntakeService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER))
            ctx = await sv.context(rt, sh_install, sa.W_INGRESS, sa.CAPS["inbound"],
                                   kind="workload")
            return await intake.register_inbound(ctx, InboundCommand(
                channel="sms", provider_message_id=sa.new_id(), phone=phone,
                reply_class="cancel"))

    import asyncio

    result = asyncio.run(body())
    assert (result.decision, result.triage_kind) == ("item", "unclear_reply")  # no attribution
    assert sa.window(sh_install, reminder)["state"] == "awaiting"
    from haloflow.m05.intake_statements import INBOUND_KEY

    resolved = sh.sh_catalog().catalog.resolve(INBOUND_KEY)
    assert resolved.query == "SELECT m05_register_inbound_v2(%(command)s)"


def test_h1_p00c_old_target_runtime_refuses_sh_tenant(sh_install: sh.SHInstall) -> None:
    """An S-C runtime (supported {46}) must refuse a tenant installed at target 54."""

    import sd_support as sd
    import service_support as sv

    sh_install.require()

    async def body() -> None:
        async with sd.runtime(sh_install) as rt:  # type: ignore[arg-type]
            with pytest.raises(Exception) as refused:
                await sv.context(rt, sh_install, sa.W_DISPATCH, sa.CAPS["dispatch"],
                                 kind="workload")
            assert getattr(refused.value, "reason_code", None) == "SCHEMA_VERSION_UNSUPPORTED"

    import asyncio

    asyncio.run(body())


def test_h1_p00b_sc_tenant_has_no_sh_units(request: pytest.FixtureRequest,
                                           m02_ids: m02_support.Identities,
                                           m05_reset: Any) -> None:
    import sc_support as sc

    for install in sc.module_install(request, m02_ids, m05_reset, "h1-p00b"):
        install.require()
        assert not _function_exists(install, "m05_outbound_prepare")


# --- H1-P01 ---------------------------------------------------------------------------------


def test_h1_p01_prepare_one_per_ref_and_frozen_snapshot(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    patient, phone, (ref,) = sa.fresh_patient(sh_install)
    first = sh.prepare(sh_install, ref)
    assert (first["outcome"], first["state"], first["fence"]) == ("prepared", "prepared", 0)
    second = sh.prepare(sh_install, ref)  # a different op_id, same ref
    assert (second["outcome"], second["op_id"]) == ("existing", first["op_id"])
    assert sh.ops_for(sh_install, ref) == 1
    row = sh.op_row(sh_install, first["op_id"])
    assert (row["patient_id"], row["phone"], row["correlation_open"]) == (patient, phone, False)
    assert sh.frozen_start(sh_install, first["op_id"]) == sh.start_of(sh_install, ref)
    bad = sh.prepare_cmd(first["op_id"], ref, row["reminder_id"], "00" * 32)
    _refused(sh_install, "m05_outbound_prepare", bad, "OP_CONFLICT")


def test_h1_p01_prepare_refusals(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    patient, phone, (ref,) = sa.fresh_patient(sh_install)
    sa.directory(sh_install, "appointment_put", appointment_ref=ref, patient_id=patient,
                 start_at=sa.FUTURE.format(n=1), status="cancelled")
    _refused(sh_install, "m05_outbound_prepare",
             sh.prepare_cmd(sa.new_id(), ref, sa.new_id(), "00" * 32), "PREPARE_UNROUTABLE")
    _p2, phone2, (ref2,) = sa.fresh_patient(sh_install)
    sh.inbound(sh_install, sa.new_id(), phone2, "stop")
    _refused(sh_install, "m05_outbound_prepare",
             sh.prepare_cmd(sa.new_id(), ref2, sa.new_id(), "00" * 32), "CONTACT_OPTED_OUT")
    assert phone != phone2


def test_h1_p01b_concurrent_prepare_one_row(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    digest = sh.text_hash(sh.render(sh.FIRST_NAME, sh.start_of(sh_install, ref)))
    a = sh.prepare_cmd(sa.new_id(), ref, sa.new_id(), digest)
    b = sh.prepare_cmd(sa.new_id(), ref, sa.new_id(), digest)
    with sa.held(sh_install, "m05_outbound_prepare", a) as first:
        waiter = sa.Waiter(sh_install, "m05_outbound_prepare", b).start_and_block()
    second = waiter.join()
    assert (first["outcome"], second["outcome"]) == ("prepared", "existing")
    assert second["op_id"] == first["op_id"]
    assert sh.ops_for(sh_install, ref) == 1


def test_h1_p01c_prepare_lost_ack_converges(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    digest = sh.text_hash(sh.render(sh.FIRST_NAME, sh.start_of(sh_install, ref)))
    cmd = sh.prepare_cmd(sa.new_id(), ref, sa.new_id(), digest)
    with sa.held(sh_install, "m05_outbound_prepare", cmd) as first:
        pass  # committed on exit; the caller "never saw" the acknowledgement
    retry = sa.call(sh_install, "m05_outbound_prepare", cmd)
    assert retry == {**first, "outcome": "existing"}
    assert sh.ops_for(sh_install, ref) == 1


# --- H1-P02, P03 ----------------------------------------------------------------------------


def test_h1_p02_concurrent_claim_one_winner(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    op = sh.prepare(sh_install, ref)
    with sa.held(sh_install, "m05_outbound_claim", sh.claim_cmd(op["op_id"], 0)) as won:
        waiter = sa.Waiter(sh_install, "m05_outbound_claim",
                           sh.claim_cmd(op["op_id"], 0)).start_and_block()
    with pytest.raises(psycopg.Error) as lost:
        waiter.join()
    assert won["outcome"] == "claimed"
    assert lost.value.diag.message_primary == "CLAIM_STALE"
    row = sh.op_row(sh_install, op["op_id"])
    assert (row["state"], row["fence"], row["correlation_open"]) == ("attempting", 1, True)


def _change(install: sa.SAInstall, kind: str, patient: str, phone: str, ref: str) -> str:
    """Apply one directory change; return the D1 code it must produce."""

    if kind == "phone_swap":
        sa.directory(install, "contact_remove", phone=phone, patient_id=patient)
        sa.directory(install, "contact_put", phone=sa.fresh_phone(), patient_id=patient)
        return "IDENTITY_CHANGED"
    if kind == "reassigned":
        other, _p, _ = sa.fresh_patient(install, appointments=0)
        sa.directory(install, "appointment_put", appointment_ref=ref, patient_id=other,
                     start_at=sa.FUTURE.format(n=1), status="booked")
        return "IDENTITY_CHANGED"
    if kind == "start_moved":
        sa.directory(install, "appointment_put", appointment_ref=ref, patient_id=patient,
                     start_at=sa.FUTURE.format(n=5), status="booked")
        return "APPOINTMENT_CHANGED"
    if kind == "cancelled":
        sa.directory(install, "appointment_put", appointment_ref=ref, patient_id=patient,
                     start_at=sa.FUTURE.format(n=1), status="cancelled")
        return "APPOINTMENT_CHANGED"
    if kind == "start_passed":
        sa.admin(install, sa.q(install, "UPDATE {s}.m05_appointments SET start_at ="
                                        " now() - interval '1 minute' WHERE appointment_ref"
                                        " = %s"), (ref,))
        sa.admin(install, sa.q(install, "UPDATE {s}.m05_outbound_ops SET start_at = a.start_at"
                                        " FROM {s}.m05_appointments a WHERE"
                                        " a.appointment_ref = %s AND m05_outbound_ops"
                                        ".appointment_ref = a.appointment_ref"), (ref,))
        return "START_PASSED"  # FIXTURE: frozen start kept equal so only time differs
    if kind == "opted_out":
        sh.inbound(install, sa.new_id(), phone, "stop")
        return "OPTED_OUT"
    if kind == "missing":
        sa.admin(install, sa.q(install, "DELETE FROM {s}.m05_appointments"
                                        " WHERE appointment_ref = %s"), (ref,))
        return "APPOINTMENT_MISSING"  # FIXTURE: no directory op deletes an appointment
    raise AssertionError(kind)


CHANGES = ("phone_swap", "reassigned", "start_moved", "cancelled", "start_passed",
           "opted_out", "missing")


@pytest.mark.parametrize("kind", CHANGES)
def test_h1_p03_claim_after_change_abandons(sh_install: sh.SHInstall, kind: str) -> None:
    sh_install.require()
    patient, phone, (ref,) = sa.fresh_patient(sh_install)
    op = sh.prepare(sh_install, ref)
    code = _change(sh_install, kind, patient, phone, ref)
    result = sh.claim(sh_install, op["op_id"], op["fence"])
    assert (result["outcome"], result["code"], result.get("attempt_id")) == (
        "abandoned", code, None)
    row = sh.op_row(sh_install, op["op_id"])
    assert (row["state"], row["correlation_open"]) == ("abandoned", False)


# --- H1-P04, P05, P06 -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", CHANGES)
def test_h1_p04_register_after_change_held(sh_install: sh.SHInstall, kind: str) -> None:
    sh_install.require()
    patient, phone, (ref,) = sa.fresh_patient(sh_install)
    op_id, _attempt = sh.to_accepted(sh_install, ref)
    code = _change(sh_install, kind, patient, phone, ref)
    result = sh.register(sh_install, op_id)
    assert (result["outcome"], result["state"], result["code"]) == (
        "held", "accepted_unregistered", code)
    assert sh.windows_for(sh_install, ref) == []
    assert sh.op_row(sh_install, op_id)["correlation_open"] is True


def test_h1_p05a_register_lock_blocks_directory(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    patient, phone_a, (ref,) = sa.fresh_patient(sh_install)
    op_id, _ = sh.to_accepted(sh_install, ref)
    swap = sa.directory_cmd("contact_put", phone=sa.fresh_phone(), patient_id=patient)
    with sa.held(sh_install, "m05_register_outbound", sh.register_cmd(op_id)) as reg:
        waiter = sa.Waiter(sh_install, "m05_directory_put", swap).start_and_block()
    assert waiter.join()["outcome"] == "applied"
    assert reg["outcome"] == "registered"
    (window,) = sh.windows_for(sh_install, ref)
    assert window["phone"] == phone_a


def test_h1_p05b_directory_lock_first_then_held(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    patient, phone_a, (ref,) = sa.fresh_patient(sh_install)
    op_id, _ = sh.to_accepted(sh_install, ref)
    # The directory change takes the frozen phone's contact lock first and holds it.
    remove = sa.directory_cmd("contact_remove", phone=phone_a, patient_id=patient)
    with sa.held(sh_install, "m05_directory_put", remove):
        waiter = sa.Waiter(sh_install, "m05_register_outbound",
                           sh.register_cmd(op_id)).start_and_block()
    result = waiter.join()  # sees the committed change: the patient has no phone A
    assert (result["outcome"], result["code"]) == ("held", "IDENTITY_CHANGED")
    assert sh.windows_for(sh_install, ref) == []


def test_h1_p06a_lost_ack_returns_stored_registration(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    patient, phone, (ref,) = sa.fresh_patient(sh_install)
    op_id, _ = sh.to_accepted(sh_install, ref)
    first = sh.register(sh_install, op_id)  # committed; the caller "never saw" it
    assert (first["outcome"], first["state"]) == ("registered", "registered")
    row = sh.op_row(sh_install, op_id)
    (window,) = sh.windows_for(sh_install, ref)
    assert window["reminder_id"] == row["reminder_id"]  # one transaction, both changed
    assert row["correlation_open"] is False
    _change(sh_install, "cancelled", patient, phone, ref)
    sh.inbound(sh_install, sa.new_id(), phone, "stop")
    _change(sh_install, "phone_swap", patient, phone, ref)
    retry = sh.register(sh_install, op_id)
    assert retry == {**first, "outcome": "existing"}
    assert len(sh.windows_for(sh_install, ref)) == 1


def test_h1_p06b_rollback_control(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    op_id, _ = sh.to_accepted(sh_install, ref)
    with sa.runtime_conn(sh_install) as conn:
        conn.execute("BEGIN")
        assert sa.call_in(conn, sh_install, "m05_register_outbound",
                          sh.register_cmd(op_id))["outcome"] == "registered"
        conn.execute("ROLLBACK")
    assert sh.op_row(sh_install, op_id)["state"] == "accepted"
    assert sh.windows_for(sh_install, ref) == []
    assert sh.register(sh_install, op_id)["outcome"] == "registered"
    assert len(sh.windows_for(sh_install, ref)) == 1


# --- H1-P07, P08 ----------------------------------------------------------------------------


def test_h1_p07_outcome_bookkeeping(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    op = sh.prepare(sh_install, ref)
    attempt = sh.claim(sh_install, op["op_id"], 0)["attempt_id"]
    sid = op["op_id"]
    assert sh.outcome(sh_install, sid, attempt, "uncertain", code="T")["state"] == "uncertain"
    assert sh.outcome(sh_install, sid, attempt, "uncertain", code="T")["outcome"] == "existing"
    _refused(sh_install, "m05_outbound_outcome",
             sh.outcome_cmd(sid, attempt, "refused", code="R"), "OUTCOME_CONFLICT")
    _refused(sh_install, "m05_outbound_outcome",
             sh.outcome_cmd(sid, sa.new_id(), "accepted", provider_ref="x"), "STALE_ATTEMPT")
    late = sh.outcome(sh_install, sid, attempt, "accepted", provider_ref="fake-late")
    assert (late["outcome"], late["state"]) == ("recorded", "accepted")
    assert sh.register(sh_install, sid)["state"] == "registered"
    same = sh.outcome(sh_install, sid, attempt, "accepted", provider_ref="fake-late")
    assert (same["outcome"], same["state"]) == ("existing", "registered")
    _refused(sh_install, "m05_outbound_outcome",
             sh.outcome_cmd(sid, attempt, "refused", code="R"), "STATE_TERMINAL")


def test_h1_p08_expire_to_uncertain_never_prepared(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    op = sh.prepare(sh_install, ref)
    attempt = sh.claim(sh_install, op["op_id"], 0, lease_seconds=30)["attempt_id"]
    sa.admin(sh_install, sa.q(sh_install, "UPDATE {s}.m05_outbound_ops SET lease_until ="
                                          " now() - interval '1 second' WHERE op_id = %s"),
             (op["op_id"],))  # FIXTURE: time travel, no sleep
    assert sh.expire(sh_install)["expired"] >= 1
    row = sh.op_row(sh_install, op["op_id"])
    assert (row["state"], row["code"], row["correlation_open"]) == (
        "uncertain", "LEASE_EXPIRED", True)
    _refused(sh_install, "m05_outbound_claim", sh.claim_cmd(op["op_id"], 1), "CLAIM_STALE")
    _refused(sh_install, "m05_outbound_read",
             {"schema_version": 1, "op_id": op["op_id"], "attempt_id": attempt,
              "attribution": sa.attribution(sa.W_DISPATCH)}, "CLAIM_STALE")


# --- H1-P09..P13: the early-reply guard -----------------------------------------------------


def _other_windows(install: sa.SAInstall, refs: list[str], n: int) -> list[str]:
    reminders = []
    for ref in refs[:n]:
        reminder = sa.new_id()
        sa.dispatch(install, reminder, ref)
        reminders.append(reminder)
    return reminders


@pytest.mark.parametrize("others", (0, 1, 2))
@pytest.mark.parametrize("state", sh.OPEN_STATES)
def test_h1_p09_guard_open_states(sh_install: sh.SHInstall, state: str, others: int) -> None:
    sh_install.require()
    _patient, phone, refs = sa.fresh_patient(sh_install, appointments=3)
    reminders = _other_windows(sh_install, refs[1:], others)
    sh.to_state(sh_install, refs[0], state)
    before = [sa.window(sh_install, r) for r in reminders]
    reply = sh.inbound(sh_install, sa.new_id(), phone, "reschedule")
    assert (reply["decision"], reply["triage_kind"], reply["reminder_id"],
            reply["appointment_ref"]) == ("item", "unclear_reply", None, None)
    assert [sa.window(sh_install, r) for r in reminders] == before


def test_h1_p09b_guard_survives_past_start(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, phone, (old_ref, new_ref) = sa.fresh_patient(sh_install, appointments=2)
    op_id = sh.to_state(sh_install, old_ref, "uncertain")
    sa.admin(sh_install, sa.q(sh_install, "UPDATE {s}.m05_outbound_ops SET start_at = now()"
                                          " - interval '1 day' WHERE op_id = %s"), (op_id,))
    reminder = sa.new_id()
    sa.dispatch(sh_install, reminder, new_ref)
    before = sa.window(sh_install, reminder)
    reply = sh.inbound(sh_install, sa.new_id(), phone, "cancel")
    assert (reply["triage_kind"], reply["reminder_id"]) == ("unclear_reply", None)
    assert sa.window(sh_install, reminder) == before


@pytest.mark.parametrize("state", ("refused", "abandoned"))
def test_h1_p10_never_sent_does_not_block(sh_install: sh.SHInstall, state: str) -> None:
    sh_install.require()
    _patient, phone, (ref_a, ref_b) = sa.fresh_patient(sh_install, appointments=2)
    sh.to_state(sh_install, ref_a, state)
    reminder = sa.new_id()
    sa.dispatch(sh_install, reminder, ref_b)
    reply = sh.inbound(sh_install, sa.new_id(), phone, "cancel")
    assert (reply["triage_kind"], reply["reminder_id"]) == ("cancel_request", reminder)


@pytest.mark.parametrize("state", sh.OPEN_STATES)
def test_h1_p11_stop_precedence(sh_install: sh.SHInstall, state: str) -> None:
    sh_install.require()
    _patient, phone, (ref_a, ref_b) = sa.fresh_patient(sh_install, appointments=2)
    reminder = sa.new_id()
    sa.dispatch(sh_install, reminder, ref_b)
    sh.to_state(sh_install, ref_a, state)
    reply = sh.inbound(sh_install, sa.new_id(), phone, "stop")
    assert (reply["decision"], reply["triage_kind"]) == ("opt_out", None)
    assert sa.window(sh_install, reminder)["state"] == "opted_out"
    assert sa.contact(sh_install, phone)["opted_out"] is True  # type: ignore[index]


def test_h1_p12_replay_precedence(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, phone, (ref_a, ref_b) = sa.fresh_patient(sh_install, appointments=2)
    reminder = sa.new_id()
    sa.dispatch(sh_install, reminder, ref_b)
    early_id = sa.new_id()
    early = sh.inbound(sh_install, early_id, phone, "confirm")  # before any op
    op_id = sh.to_state(sh_install, ref_a, "accepted")
    assert sh.inbound(sh_install, early_id, phone, "confirm") == {**early, "outcome": "existing"}
    guarded_id = sa.new_id()
    guarded = sh.inbound(sh_install, guarded_id, phone, "reschedule")
    assert guarded["triage_kind"] == "unclear_reply"
    assert sh.register(sh_install, op_id)["outcome"] == "registered"
    assert sh.inbound(sh_install, guarded_id, phone, "reschedule") == {
        **guarded, "outcome": "existing"}


def test_h1_p13_claim_serialises_with_reply(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, phone, (ref_a, ref_b) = sa.fresh_patient(sh_install, appointments=2)
    reminder = sa.new_id()
    sa.dispatch(sh_install, reminder, ref_b)
    op = sh.prepare(sh_install, ref_a)
    with sa.held(sh_install, "m05_outbound_claim", sh.claim_cmd(op["op_id"], 0)) as won:
        waiter = sa.Waiter(sh_install, sh.V2_INBOUND,
                           sa.inbound_cmd(sa.new_id(), phone, "cancel")).start_and_block()
    assert won["outcome"] == "claimed"
    reply = waiter.join()
    assert (reply["triage_kind"], reply["reminder_id"]) == ("unclear_reply", None)
    assert sa.window(sh_install, reminder)["state"] == "awaiting"


# --- H1-P14, P17, P18 -----------------------------------------------------------------------


def test_h1_p14_read_frozen_snapshot_current_attempt(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    patient, phone, (ref,) = sa.fresh_patient(sh_install)
    original_start = sh.start_of(sh_install, ref)
    original_hash = sh.text_hash(sh.render(sh.FIRST_NAME, original_start))
    op = sh.prepare(sh_install, ref)
    attempt = sh.claim(sh_install, op["op_id"], 0)["attempt_id"]
    # The directory changes AFTER the claim: start moved and phone removed.
    sa.directory(sh_install, "appointment_put", appointment_ref=ref, patient_id=patient,
                 start_at=sa.FUTURE.format(n=7), status="booked")
    sa.directory(sh_install, "contact_remove", phone=phone, patient_id=patient)
    snap = sh.read(sh_install, op["op_id"], attempt)
    assert (snap["phone"], snap["patient_id"], snap["attempt_id"]) == (phone, patient, attempt)
    from datetime import datetime

    assert datetime.fromisoformat(snap["start_at"]) == original_start
    assert snap["text_sha256"] == original_hash
    assert sh.start_of(sh_install, ref) != original_start  # the directory really moved
    _refused(sh_install, "m05_outbound_read",
             {"schema_version": 1, "op_id": op["op_id"], "attempt_id": sa.new_id(),
              "attribution": sa.attribution(sa.W_DISPATCH)}, "CLAIM_STALE")


def test_h1_p14b_expired_lease_unreadable_before_sweeper(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    op = sh.prepare(sh_install, ref)
    attempt = sh.claim(sh_install, op["op_id"], 0, lease_seconds=30)["attempt_id"]
    sa.admin(sh_install, sa.q(sh_install, "UPDATE {s}.m05_outbound_ops SET lease_until ="
                                          " now() - interval '1 second' WHERE op_id = %s"),
             (op["op_id"],))  # FIXTURE: the worker paused past its lease
    assert sh.op_row(sh_install, op["op_id"])["state"] == "attempting"  # no sweep yet
    _refused(sh_install, "m05_outbound_read",
             {"schema_version": 1, "op_id": op["op_id"], "attempt_id": attempt,
              "attribution": sa.attribution(sa.W_DISPATCH)}, "CLAIM_STALE")
    # The paused worker's late outcome for the same attempt is still recordable (D6):
    late = sh.outcome(sh_install, op["op_id"], attempt, "uncertain", code="PAUSED")
    assert late["state"] == "uncertain"


def test_h1_p17_held_inspection_no_pii(sh_install: sh.SHInstall) -> None:
    sh_install.require()
    _patient, _phone, (r1, r2, r3) = sa.fresh_patient(sh_install, appointments=3)
    ids = {sh.to_state(sh_install, r1, "uncertain"),
           sh.to_state(sh_install, r2, "accepted_unregistered"),
           sh.to_state(sh_install, r3, "refused")}
    listed = sh.held_ops(sh_install)["ops"]
    mine = [row for row in listed if row["op_id"] in ids]
    assert len(mine) == 3
    assert all(set(row) == sh.HELD_COLUMNS for row in mine)


def _expire_lease(install: sa.SAInstall, op_id: str) -> None:
    sa.admin(install, sa.q(install, "UPDATE {s}.m05_outbound_ops SET lease_until ="
                                    " now() - interval '1 second' WHERE op_id = %s"),
             (op_id,))  # FIXTURE: time travel


def test_h1_p18_tenant_isolation_identical_ids(sh_install: sh.SHInstall,
                                               sh_other: sh.SHInstall) -> None:
    sh_install.require()
    sh_other.require()
    patient, phone = sa.new_id(), sa.fresh_phone()
    ref, ref2, ref3 = sa.new_id(), sa.new_id(), sa.new_id()
    op_x, rem_x, op_y, rem_y = sa.new_id(), sa.new_id(), sa.new_id(), sa.new_id()
    for install in (sh_install, sh_other):
        sh.seed_same_ids(install, patient, phone, [ref, ref2, ref3])
    def digest(r: str) -> str:
        return sh.text_hash(sh.render(sh.FIRST_NAME, sh.start_of(sh_install, r)))
    for install in (sh_install, sh_other):
        assert sh.prepare(install, ref, op_id=op_x, reminder=rem_x,
                          digest=digest(ref))["outcome"] == "prepared"
        assert sh.prepare(install, ref3, op_id=op_y, reminder=rem_y,
                          digest=digest(ref3))["outcome"] == "prepared"
    other_window = sa.new_id()
    sa.dispatch(sh_other, other_window, ref2)  # B: an ordinary current window
    # 1. A claims X; A's guard must not suppress B's identical phone.
    attempt_a = sh.claim(sh_install, op_x, 0)["attempt_id"]
    reply_b = sh.inbound(sh_other, sa.new_id(), phone, "cancel")
    assert (reply_b["triage_kind"], reply_b["reminder_id"]) == ("cancel_request", other_window)
    reply_a = sh.inbound(sh_install, sa.new_id(), phone, "cancel")
    assert (reply_a["triage_kind"], reply_a["reminder_id"]) == ("unclear_reply", None)
    # 2. B: a genuinely expired attempting X. A: an expired attempting X (control).
    attempt_b = sh.claim(sh_other, op_x, 0)["attempt_id"]
    _expire_lease(sh_other, op_x)
    _expire_lease(sh_install, op_x)
    b_before = sh.op_row(sh_other, op_x)
    assert sh.expire(sh_install)["expired"] >= 1
    assert sh.op_row(sh_install, op_x)["state"] == "uncertain"  # A control expired
    assert sh.op_row(sh_other, op_x) == b_before  # B untouched by A's sweep
    assert b_before["state"] == "attempting"
    # 3. B cannot read or record A's attempt (and vice versa).
    _refused(sh_other, "m05_outbound_read",
             {"schema_version": 1, "op_id": op_x, "attempt_id": attempt_a,
              "attribution": sa.attribution(sa.W_DISPATCH)}, "CLAIM_STALE")
    _refused(sh_install, "m05_outbound_outcome",
             sh.outcome_cmd(op_x, attempt_b, "accepted", provider_ref="x"), "STALE_ATTEMPT")
    # 4. A registers Y; B's Y op and B's windows are unchanged.
    b_y_before = sh.op_row(sh_other, op_y)
    attempt_y = sh.claim(sh_install, op_y, 0)["attempt_id"]
    sh.outcome(sh_install, op_y, attempt_y, "accepted", provider_ref="fake-a")
    assert sh.register(sh_install, op_y)["outcome"] == "registered"
    assert len(sh.windows_for(sh_install, ref3)) == 1
    assert sh.op_row(sh_other, op_y) == b_y_before
    assert sh.windows_for(sh_other, ref3) == []
    # 5. held: each tenant lists only its own.
    assert [r for r in sh.held_ops(sh_other)["ops"] if r["op_id"] == op_x] == []
    assert [r["op_id"] for r in sh.held_ops(sh_install)["ops"]].count(op_x) == 1


@pytest.mark.parametrize("function", ("m05_outbound_prepare", "m05_outbound_claim",
                                      "m05_outbound_read", "m05_outbound_outcome",
                                      "m05_outbound_expire", "m05_register_outbound",
                                      "m05_outbound_held"))
def test_h1_p19_sql_wrong_workload_refused(sh_install: sh.SHInstall, function: str) -> None:
    """Least privilege at SQL: the ingress workload cannot run any outbound function."""

    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    op = sh.prepare(sh_install, ref)
    commands = {
        "m05_outbound_prepare": sh.prepare_cmd(sa.new_id(), ref, sa.new_id(), "00" * 32),
        "m05_outbound_claim": sh.claim_cmd(op["op_id"], 0),
        "m05_outbound_read": {"schema_version": 1, "op_id": op["op_id"],
                              "attempt_id": sa.new_id()},
        "m05_outbound_outcome": sh.outcome_cmd(op["op_id"], sa.new_id(), "uncertain",
                                               code="X"),
        "m05_outbound_expire": {"schema_version": 1, "limit": 10},
        "m05_register_outbound": sh.register_cmd(op["op_id"]),
        "m05_outbound_held": {"schema_version": 1, "limit": 10},
    }
    command = {**commands[function], "attribution": sa.attribution(sa.W_INGRESS)}
    _refused(sh_install, function, command, "WORKLOAD_NOT_AUTHORIZED")


def test_h1_p20_gateway_wrong_capabilities_refused(sh_install: sh.SHInstall) -> None:
    """Gateway: unprotected bookkeeping is NOT an authorization exemption."""

    import service_support as sv

    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    op = sh.prepare(sh_install, ref)
    before = sh.op_row(sh_install, op["op_id"])

    async def body() -> None:
        from haloflow.m05.outbound import OutboundRefused, OutboundService

        async with sh.runtime(sh_install) as rt:
            svc = OutboundService(rt.gateway)
            wrong = await sv.context(rt, sh_install, sa.W_DISPATCH, sa.CAPS["inbound"],
                                     kind="workload")
            calls = (
                lambda: svc.prepare(wrong, ref, "00" * 32, op_id=sa.new_id(),
                                    reminder_id=sa.new_id()),
                lambda: svc.claim(wrong, op["op_id"], 0),
                lambda: svc.read(wrong, op["op_id"], sa.new_id()),
                lambda: svc.record_outcome(wrong, op["op_id"], sa.new_id(), "uncertain",
                                           code="X"),
                lambda: svc.expire(wrong),
                lambda: svc.register(wrong, op["op_id"]),
                lambda: svc.held(wrong),
            )
            checked = 0
            for call in calls:  # each method is invoked AND awaited inside its own check
                with pytest.raises(OutboundRefused) as refused:
                    await call()
                assert refused.value.code == "CAPABILITIES_INVALID"
                checked += 1
            assert checked == 7

    import asyncio

    asyncio.run(body())
    assert sh.op_row(sh_install, op["op_id"]) == before


# --- H1-P15, P16: activation through the real gateway (D5) ----------------------------------
#
# Pin: the S-H statements are admitted under the dispatch workload's capabilities
# (sa.CAPS["dispatch"]). Through the gateway with ENFORCE, a revoked clinic refuses EVERY
# protected statement (prepare, claim, register) with ActivationRefused M05_NOT_ACTIVATED;
# unprotected bookkeeping (record_outcome, expire) still commits. No promise is made that
# reads or replays of protected statements return stored results while revoked.


async def _ctx(rt: Any, install: sh.SHInstall) -> Any:
    import service_support as sv

    return await sv.context(rt, install, sa.W_DISPATCH, sa.CAPS["dispatch"], kind="workload")


def _activation_code(error: BaseException) -> str:
    from haloflow.m01.errors import ActivationRefused

    assert type(error) is ActivationRefused, repr(error)
    code: str = error.reason_code
    return code


def test_h1_p15_revoke_after_attempt(sh_install: sh.SHInstall) -> None:
    import sc_support as sc

    sh_install.require()
    sc.change(sh_install, "grant")
    _patient, _phone, (ref_a, ref_b) = sa.fresh_patient(sh_install, appointments=2)

    async def body() -> None:
        from haloflow.m05.outbound import OutboundService

        async with sh.runtime(sh_install) as rt:
            svc = OutboundService(rt.gateway)
            ctx = await _ctx(rt, sh_install)
            digest_a = sh.text_hash(sh.render(sh.FIRST_NAME, sh.start_of(sh_install, ref_a)))
            digest_b = sh.text_hash(sh.render(sh.FIRST_NAME, sh.start_of(sh_install, ref_b)))
            a = await svc.prepare(ctx, ref_a, digest_a, op_id=sa.new_id(),
                                  reminder_id=sa.new_id())
            b = await svc.prepare(ctx, ref_b, digest_b, op_id=sa.new_id(),
                                  reminder_id=sa.new_id())  # genuinely prepared second op
            claimed = await svc.claim(ctx, a["op_id"], a["fence"])
            sc.change(sh_install, "revoke")
            ctx = await _ctx(rt, sh_install)
            recorded = await svc.record_outcome(ctx, a["op_id"], claimed["attempt_id"],
                                                "accepted", provider_ref="fake-p15")
            assert recorded["state"] == "accepted"
            assert isinstance((await svc.expire(ctx))["expired"], int)
            for call in (svc.claim(ctx, b["op_id"], b["fence"]),
                         svc.register(ctx, a["op_id"])):
                with pytest.raises(Exception) as refused:
                    await call
                assert _activation_code(refused.value) == "M05_NOT_ACTIVATED"
            sc.change(sh_install, "grant")
            ctx = await _ctx(rt, sh_install)
            assert (await svc.register(ctx, a["op_id"]))["outcome"] == "registered"

    import asyncio

    asyncio.run(body())
    assert sh.op_row(sh_install, sh.op_row_by_ref(sh_install, ref_b))["state"] == "prepared"


def test_h1_p16_revoked_clinic_refuses_new_work(sh_install: sh.SHInstall) -> None:
    import sc_support as sc

    sh_install.require()
    _patient, _phone, (ref,) = sa.fresh_patient(sh_install)
    sc.change(sh_install, "revoke")

    async def body() -> None:
        from haloflow.m05.outbound import OutboundService

        async with sh.runtime(sh_install) as rt:
            svc = OutboundService(rt.gateway)
            ctx = await _ctx(rt, sh_install)
            with pytest.raises(Exception) as refused:
                await svc.prepare(ctx, ref, "00" * 32, op_id=sa.new_id(),
                                  reminder_id=sa.new_id())
            assert _activation_code(refused.value) == "M05_NOT_ACTIVATED"

    import asyncio

    asyncio.run(body())
    assert sh.ops_for(sh_install, ref) == 0
    sc.change(sh_install, "grant")
