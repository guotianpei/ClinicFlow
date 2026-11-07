"""L-1S package S-C — the D-S1 activation switch through the real M01 gateway (PostgreSQL 17).

Governing: S-C requirements v1, architecture v1 A2-A5, Codex 202256Z pins 1-5. Every row
runs through `compose_tenant_runtime` with the S-C TEST registry (target 46) and the S-C
TEST catalogue (protected keys declared by the M05, S-A and S-B statement modules).
ENFORCE here is mechanism evidence only: M05-A1 keeps production from starting with M05
active (SC-S03); nothing in this module claims a production run.

- SC-P01  ENFORCE, no activation record: a protected call (S-A dispatch) refuses
          M05_NOT_ACTIVATED and writes nothing.
- SC-P02  grant -> the same call succeeds; revoke -> the next call refuses.
- SC-P03  linearization (A3): a transaction authorized at its probe may finish after a
          revocation commits; a transaction whose probe starts after the revocation
          commits refuses.
- SC-P04  caught refusal rolls back (pin 3): an M02 append (unprotected) then a protected
          statement whose ActivationRefused the callback CATCHES and returns normally ->
          the gateway still raises ActivationRefused and the append is not committed; a
          second protected attempt in the same callback also refuses.
- SC-P05  effective isolation (pin 5): the probe reports 'read committed' on the runtime
          connection (no global change).
- SC-P06  a tenant without t046 (S-B registry, target 45) under ENFORCE ->
          M05_ACTIVATION_UNREADABLE (fail closed); under NOT_ENFORCED the same call works.
- SC-P07  NOT_ENFORCED with no record: behaviour unchanged (the call succeeds).
- SC-P08  probe output is exactly {schema_version: 1, active: bool} and follows the latest
          record (grant, revoke, grant).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sa_support as sa
import sc_support as sc
import service_support as sv

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]


@pytest.fixture(scope="module")
def sc_install(request: pytest.FixtureRequest, m02_ids: Any, m05_reset: Any) -> Any:
    yield from sc.module_install(request, m02_ids, m05_reset, "sc")


@pytest.fixture(scope="module")
def sc_old_install(request: pytest.FixtureRequest, m02_ids: Any, m05_reset: Any) -> Any:
    yield from sc.module_install(request, m02_ids, m05_reset, "sc:old", registry="sb")


@asynccontextmanager
async def _runtime(install: sc.SCInstall, policy: str) -> AsyncIterator[Any]:
    import sb_support as sb

    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    old = install.registry_kind != "sc"
    pool = TenantPool(install.ids.logins[sa.RUNTIME], min_size=1, max_size=2)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=sb.sb_registry() if old else sc.sc_registry(),
            catalog=sc.sc_catalog(),
            supported_schema_versions=frozenset({sb.SB_TARGET if old else sc.SC_TARGET}),
            activation_policy=ActivationPolicy[policy],
        )
    finally:
        await pool.close()


async def _dispatch(rt: Any, install: sc.SCInstall, ref: str) -> Any:
    from haloflow.m02.observation import EventFoundation
    from haloflow.m05.intake import DispatchCommand, IntakeService

    ctx = await sv.context(rt, install, sa.W_DISPATCH, sa.CAPS["dispatch"], kind="workload")
    service = IntakeService(EventFoundation(rt.gateway, producer_service=sa.PRODUCER))
    return await service.register_dispatch(ctx, DispatchCommand(
        reminder_id=uuid.uuid4(), appointment_ref=uuid.UUID(ref)))


def _windows(install: sc.SCInstall, ref: str) -> int:
    rows = sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.m05_reminder_windows"
                                           " WHERE appointment_ref = %s"), (ref,))
    return int(rows[0][0])


def _refused(error: BaseException) -> str | None:
    from haloflow.m01.errors import ActivationRefused

    current: BaseException | None = error
    while current is not None:
        if isinstance(current, ActivationRefused):
            return current.reason_code
        current = current.__cause__ or current.__context__
    return None


# --- SC-P01 / SC-P02 / SC-P07 -------------------------------------------------------------


def test_sc_p01_p02_absent_grant_revoke(sc_install: sc.SCInstall) -> None:
    sc_install.require()
    _, _, (ref1, ref2, ref3) = sa.fresh_patient(sc_install, appointments=3)

    async def body() -> list[str | None]:
        outcomes: list[str | None] = []
        async with _runtime(sc_install, "ENFORCE") as rt:
            for step, ref in (("absent", ref1), ("grant", ref2), ("revoke", ref3)):
                if step != "absent":
                    sc.change(sc_install, step)
                try:
                    await _dispatch(rt, sc_install, ref)
                    outcomes.append(None)
                except Exception as error:  # noqa: BLE001 - classified below
                    outcomes.append(_refused(error) or type(error).__name__)
        return outcomes

    assert asyncio.run(body()) == ["M05_NOT_ACTIVATED", None, "M05_NOT_ACTIVATED"]
    assert (_windows(sc_install, ref1), _windows(sc_install, ref2),
            _windows(sc_install, ref3)) == (0, 1, 0)


def test_sc_p07_not_enforced_unchanged(sc_old_install: sc.SCInstall,
                                        sc_install: sc.SCInstall) -> None:
    for install in (sc_install, sc_old_install):
        install.require()
        _, _, (ref,) = sa.fresh_patient(install)

        async def body(inst: sc.SCInstall = install, r: str = ref) -> Any:
            async with _runtime(inst, "NOT_ENFORCED") as rt:
                return await _dispatch(rt, inst, r)

        assert asyncio.run(body()).outcome == "registered"
        assert _windows(install, ref) == 1


# --- SC-P03 ------------------------------------------------------------------------------


def _change_in_thread(install: sc.SCInstall, kind: str) -> None:
    error: list[BaseException] = []

    def run() -> None:
        try:
            sc.change(install, kind)
        except BaseException as caught:  # noqa: BLE001 - re-raised below
            error.append(caught)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive() and not error, error


def test_sc_p03_linearization_at_the_probe(sc_install: sc.SCInstall) -> None:
    sc_install.require()
    from psycopg.types.json import Jsonb

    def selector() -> dict[str, Any]:
        return {"command": Jsonb({"schema_version": 1, "after": None, "until": None,
                                  "limit": 1, "attribution": sa.attribution(sa.W_TIMER)})}

    async def body() -> tuple[str, str | None]:
        import sb_support as sb

        sc.change(sc_install, "grant")
        async with _runtime(sc_install, "ENFORCE") as rt:
            ctx = await sv.context(rt, sc_install, sa.W_TIMER, frozenset({sb.DUE_KEY}),
                                   kind="workload")

            async def authorized(handle: Any) -> str:
                await handle.fetch_all(sb.DUE_KEY, selector())          # probe: active
                await asyncio.to_thread(_change_in_thread, sc_install, "revoke")
                await handle.fetch_all(sb.DUE_KEY, selector())          # still authorized
                return "completed"

            first = await rt.gateway.with_tenant_transaction(ctx, authorized)
            sc.change(sc_install, "grant")

            async def late_probe(handle: Any) -> str:
                await asyncio.to_thread(_change_in_thread, sc_install, "revoke")
                await handle.fetch_all(sb.DUE_KEY, selector())          # probe after revoke
                return "should not complete"

            try:
                await rt.gateway.with_tenant_transaction(ctx, late_probe)
                second: str | None = None
            except Exception as error:  # noqa: BLE001 - classified below
                second = _refused(error)
            return first, second

    assert asyncio.run(body()) == ("completed", "M05_NOT_ACTIVATED")


# --- SC-P04 ------------------------------------------------------------------------------


def test_sc_p04_caught_refusal_rolls_back(sc_install: sc.SCInstall) -> None:
    sc_install.require()
    sc.change(sc_install, "revoke")
    event_id = uuid.uuid4()
    reminder = uuid.uuid4()

    async def body() -> tuple[list[str], str | None]:
        from haloflow.m01.errors import ActivationRefused
        from haloflow.m02.observation import EventFoundation, Observation

        seen: list[str] = []
        async with _runtime(sc_install, "ENFORCE") as rt:
            ctx = await sv.context(rt, sc_install, sa.W_DISPATCH, sa.CAPS["dispatch"],
                                   kind="workload")
            foundation = EventFoundation(rt.gateway, producer_service=sa.PRODUCER)

            async def swallow(binding: Any) -> str:
                await foundation.append_observation_in(binding, Observation(
                    event_id=event_id, event_type=sa.T_DISPATCHED, contract_version=1,
                    correlation_id=reminder, producer_dedupe_key=f"dispatch:{reminder.hex}",
                    links=(), metadata={"reminder_id": str(reminder),
                                        "appointment_ref": str(uuid.uuid4())},
                    patient_id=uuid.uuid4()))
                for _ in range(2):
                    try:
                        await binding.handle.fetch_all("m05.intake.dispatch", {"command": None})
                    except ActivationRefused as refused:
                        seen.append(refused.reason_code)
                return "swallowed"

            try:
                await foundation.run_in_transaction(ctx, swallow)
                outer: str | None = None
            except Exception as error:  # noqa: BLE001 - classified below
                outer = _refused(error)
        return seen, outer

    seen, outer = asyncio.run(body())
    assert seen == ["M05_NOT_ACTIVATED", "M05_NOT_ACTIVATED"]
    assert outer == "M05_NOT_ACTIVATED"
    rows = sa.admin(sc_install, sa.q(sc_install, "SELECT count(*) FROM {s}.patient_events"
                                                 " WHERE event_id = %s"), (event_id,))
    assert rows[0][0] == 0, "the append before the refusal was committed"


# --- SC-P05 / SC-P08 ----------------------------------------------------------------------


def _probe(install: sc.SCInstall) -> tuple[Any, str]:
    with sa.runtime_conn(install) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant_id,))
        row = conn.execute(sa.q(install, "SELECT {s}.m05_activation_probe(),"
                                         " pg_catalog.current_setting('transaction_isolation')")
                           ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_sc_p05_p08_probe_shape_isolation_latest(sc_install: sc.SCInstall) -> None:
    sc_install.require()
    states = []
    for kind in ("grant", "revoke", "grant"):
        sc.change(sc_install, kind)
        value, isolation = _probe(sc_install)
        assert isolation == "read committed"
        assert set(value) == {"schema_version", "active"} and value["schema_version"] == 1
        assert type(value["active"]) is bool
        states.append(value["active"])
    assert states == [True, False, True]


# --- SC-P06 ------------------------------------------------------------------------------


def test_sc_p06_missing_probe_fails_closed(sc_old_install: sc.SCInstall) -> None:
    sc_old_install.require()
    _, _, (ref,) = sa.fresh_patient(sc_old_install)

    async def body() -> str | None:
        async with _runtime(sc_old_install, "ENFORCE") as rt:
            try:
                await _dispatch(rt, sc_old_install, ref)
            except Exception as error:  # noqa: BLE001 - classified below
                return _refused(error) or type(error).__name__
        return None

    assert asyncio.run(body()) == "M05_ACTIVATION_UNREADABLE"
    assert _windows(sc_old_install, ref) == 0
