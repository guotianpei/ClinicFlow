"""L-6 CP-3: bounded ATT regression cases (tiers D and C), v1, for implementation v5. PROPOSED.

Design: proposal v3 `claude_l6-cp3-att-timeout-reopen-proposal-v3.md` (887fbecc…0a60),
approved by owner record `claude_owner-decision-l6-cp3-att-v3-design-2026-10-06.md`
(05ed2315…d8fa); Q4 and the development go in `…-att-step2-go-2026-10-06.md` (e36f873d…c03e);
Codex ACCEPT `codex_att-timeout-proposal-v3-review.md` (076f09b3…9290).

Supplemental to the frozen packet-v7 module `test_l6_cp3_upgrade_postgres.py`, whose bytes
are unchanged; its harness is imported, not copied (the W5 precedent). Two nodes:
- TC-A03-h-b (mutant M-ATT-inside): TC-A03-h with a test-only `lock_timeout` injection on
  the appender's own connection (mode `h_bounded`, through the existing `appender_probe`
  hook), so the mutant's lock wait ends and the frozen lock-at-entry oracle is reached;
- TC-A04-b1-c (mutant M-ATT-retry): TC-A04-b1 plus a count of provisioner connections
  after the appender entry (the existing passive `connect_log`), so a retry on a fresh
  connection is seen; the frozen same-connection count is kept.
Both keep the frozen assertion messages. Each test body's first statement is its own
CP-3 import (the first-import census site). New-harness failures raise the plain
`Exception` classes below, never `AssertionError`, each with a fixed marker.
Data are synthetic (R2). Run only when Rachel authorizes it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import psycopg
import pytest

# Reused harness of the frozen v7 D/C module; imported, not copied. The autouse
# `_record_server_version` is imported by name; `k0_tenant` is exposed as `att_tenant`.
from test_l6_cp3_upgrade_postgres import (  # noqa: F401 - fixture, registered by name
    BARRIER_WAIT_SECONDS,
    POLL_SECONDS,
    T_LOCK_SECONDS,
    Tenant,
    _assert_code,
    _assert_one_refusal,
    _assert_r5,
    _assert_seam_order,
    _close_halted_k,
    _compose,
    _CountingConnection,
    _current_attempt,
    _halt_after,
    _login,
    _operation_id,
    _Probe,
    _r5_snapshot,
    _record_server_version,
    _refused,
    _request,
    _seed_t003,
    _spawn,
)
from test_l6_cp3_upgrade_postgres import k0_tenant as _v7_k0_tenant  # noqa: F401 - fixture

from haloflow.m01.provisioning import MIGRATOR_ROLE, PROVISIONER_ROLE

pytestmark = pytest.mark.postgres

INJECTED_LOCK_TIMEOUT = f"{int(T_LOCK_SECONDS)}s"  # h_bounded: the appender's lock wait bound
H_B_BOUND_SECONDS = T_LOCK_SECONDS + BARRIER_WAIT_SECONDS  # outer bound, TC-A03-h-b
B1_C_BOUND_SECONDS = BARRIER_WAIT_SECONDS  # outer bound, TC-A04-b1-c
CLEANUP_CONNECT_SECONDS = 5  # every cleanup admin connection
CLEANUP_STATEMENT_MS = 5000  # every cleanup admin statement (statement and lock timeout)
CLEANUP_OPTIONS = (
    f"-c statement_timeout={CLEANUP_STATEMENT_MS} -c lock_timeout={CLEANUP_STATEMENT_MS}"
)


# --- new-harness failure markers (proposal v3 §4.5); never AssertionError ----------


class AttHarnessInfra(Exception):
    """L6_ATT_INFRA: the injection could not be configured/verified, or the deadline
    cleanup failed or stayed uncertain."""


class AttHarnessUnmet(Exception):
    """L6_ATT_UNMET: required new instrumentation is absent."""


class AttBoundExpired(Exception):
    """L6_ATT_BOUND: the in-test deadline expired and the cleanup was verified."""


@pytest.fixture
def att_tenant(_v7_k0_tenant: Tenant) -> Tenant:  # noqa: F811 - requests the imported fixture
    """The frozen v7 `k0_tenant` (provision, K0 check, teardown purge), unchanged."""
    return _v7_k0_tenant


# --- bounded observation and deadline cleanup (proposal v3 §4.1) -------------------


def _admin_bounded(admin: str, query: str, params: Any = None) -> list[tuple[Any, ...]]:
    """One statement on a fresh superuser connection, bounded on the server and driver
    side (a synchronous call on the event-loop thread cannot be interrupted by asyncio)."""

    with psycopg.connect(
        admin,
        autocommit=True,
        connect_timeout=CLEANUP_CONNECT_SECONDS,
        options=CLEANUP_OPTIONS,
    ) as conn:
        cur = conn.execute(query, params)
        return list(cur.fetchall()) if cur.description is not None else []


_CLIENTS = (
    "FROM pg_stat_activity WHERE datname = current_database() "
    "AND backend_type = 'client backend' AND pid <> pg_backend_pid()"
)


async def _bounded(
    task: asyncio.Task[Any], seconds: float, case: str, role_logins: dict[str, str], admin: str
) -> None:
    """Observe the attempt without cancelling it. At the deadline, cleanup starts at once:
    request cancellation (not awaited); check that every session in this dedicated test
    database is the provisioner, migrator or admin login; terminate the provisioner and
    migrator backends; verify they are gone; observe the task's end without cancelling.
    Verified -> AttBoundExpired. Anything failed or uncertain -> AttHarnessInfra (the
    runner's process deadline is then the final containment)."""

    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task in done:
        return
    expired_at = time.monotonic()
    task.cancel()  # a request only; never awaited here
    logins = sorted({_login(role_logins[PROVISIONER_ROLE]), _login(role_logins[MIGRATOR_ROLE])})
    allowed = {*logins, _login(admin)}

    def infra(detail: str) -> AttHarnessInfra:
        return AttHarnessInfra(f"L6_ATT_INFRA: {case}: deadline cleanup incomplete: {detail}")

    try:
        sessions = _admin_bounded(admin, f"SELECT pid, usename {_CLIENTS}")
    except psycopg.Error as error:
        raise infra(f"precondition query failed ({type(error).__name__})") from error
    unrelated = sorted(str(user) for _pid, user in sessions if user not in allowed)
    if unrelated:
        raise infra(f"unrelated session present ({unrelated}); nothing terminated")
    try:
        _admin_bounded(
            admin, f"SELECT pg_terminate_backend(pid) {_CLIENTS} AND usename = ANY(%s)", (logins,)
        )
    except psycopg.Error as error:
        raise infra(f"termination failed ({type(error).__name__})") from error
    loop = asyncio.get_running_loop()
    verify_until = loop.time() + BARRIER_WAIT_SECONDS
    while True:
        try:
            remaining = _admin_bounded(
                admin, f"SELECT count(*) {_CLIENTS} AND usename = ANY(%s)", (logins,)
            )[0][0]
        except psycopg.Error as error:
            raise infra(f"verification query failed ({type(error).__name__})") from error
        if remaining == 0:
            break
        if loop.time() > verify_until:
            raise infra(f"{remaining} provisioner/migrator backend(s) remain")
        await asyncio.sleep(POLL_SECONDS)
    done, _ = await asyncio.wait({task}, timeout=BARRIER_WAIT_SECONDS)
    if task not in done:
        raise infra("the attempt task did not end after cleanup")
    if task.cancelled():
        how = "cancelled"
    else:
        exc = task.exception()
        how = type(exc).__name__ if exc is not None else "returned"
    raise AttBoundExpired(
        f"L6_ATT_BOUND: {case}: attempt exceeded {seconds:.0f} s; cleanup verified: backends "
        f"gone, task ended ({how}) {time.monotonic() - expired_at:.1f} s after the deadline"
    )


# --- TC-A03-h-b: mode h_bounded (proposal v3 §4.2) ----------------------------------


class _BoundedConnection(_CountingConnection):
    """For the INSERT only: set and verify the appender session's lock_timeout, then
    delegate to the unchanged frozen wrapper (pid, connection state, send count, the
    original SQL and parameters, visibility). Nothing else is intercepted, skipped or
    fabricated."""

    async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        text = query.as_string(self._inner) if hasattr(query, "as_string") else str(query)
        if text.lstrip().upper().startswith("INSERT"):
            try:
                await self._inner.execute(f"SET lock_timeout = '{INJECTED_LOCK_TIMEOUT}'")
                row = await (await self._inner.execute("SHOW lock_timeout")).fetchone()
            except psycopg.Error as error:
                raise AttHarnessInfra(
                    "L6_ATT_INFRA: TC-A03-h-b: lock_timeout injection not verified "
                    f"({type(error).__name__})"
                ) from error
            if row is None or row[0] != INJECTED_LOCK_TIMEOUT:
                raise AttHarnessInfra(
                    "L6_ATT_INFRA: TC-A03-h-b: lock_timeout injection not verified "
                    f"(SHOW returned {row!r})"
                )
            self._probe.lock_timeout_verified = True  # type: ignore[attr-defined]
        return await super().execute(query, params, **kwargs)


class _BoundedProbe(_Probe):
    """The frozen `_Probe` in mode "h" (entry observations, counts unchanged); only the
    appender connection wrapper differs."""

    def __init__(self, admin: str, tenant_id: str) -> None:
        super().__init__("h", admin, tenant_id)
        self.lock_timeout_verified = False

    def wrap(self, conn: Any) -> Any:
        return _BoundedConnection(conn, self)


async def test_tc_a03_h_b_bounded_rollback_then_append(
    att_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """TC-A03-h with the h_bounded injection: the refused step's transaction ended before
    the appender was entered (no lock on the operations table at entry), asserted first
    among the oracle checks with the frozen message; then the frozen d2/h sequence."""

    from haloflow.m01.provisioning.upgrade import UpgradeTestHooks  # noqa: F401 - census site

    admin, t = migrated_database, att_tenant
    probe = _BoundedProbe(admin, t.tenant_id)
    a = await _halt_after(role_logins, admin, "C1", _request(t, None), appender_probe=probe)
    op = _operation_id(admin, t.tenant_id)
    att_a = _current_attempt(admin, op)
    await _close_halted_k(admin, a, role_logins)
    before = _r5_snapshot(admin, t)
    a.release.set()  # the first line of the frozen Halted.resume()
    await _bounded(a.task, H_B_BOUND_SECONDS, "TC-A03-h-b", role_logins, admin)
    error = await _refused(a.task, "TC-A03-d2")
    if probe.ops_locks_at_entry is None:
        raise AttHarnessUnmet("L6_ATT_UNMET: TC-A03-h-b: the appender entry was not observed")
    if not (probe.sent >= 1 and probe.lock_timeout_verified is True):
        raise AttHarnessUnmet("L6_ATT_UNMET: TC-A03-h-b: the injection did not reach the INSERT")
    assert probe.ops_locks_at_entry == [], (
        "TC-A03-h: refused transaction ended before the appender was entered"
    )
    # the frozen d2/h sequence, verbatim and in its original order
    _assert_code(error, "MAINTENANCE_LOCK_LOST", "fence", "TC-A03-d2")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_LOCK_LOST", "fence", op, "TC-A03-d2"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-A03-h", new_events=["attempt_refused"])
    assert probe.invocations == 1, "TC-A03-h: the appender was entered once"
    assert probe.ops_locks_at_entry == [], (
        "TC-A03-h: refused transaction ended before the appender was entered"
    )
    assert probe.insert_conn_state == (True, psycopg.pq.TransactionStatus.IDLE), (
        "TC-A03-h: the append is a separate autocommit INSERT"
    )
    assert probe.visible_after_insert == 1, (
        "TC-A03-h: committed on its own when the INSERT returned"
    )


# --- TC-A04-b1-c: connection count (proposal v3 §4.3) -------------------------------


async def test_tc_a04_b1_c_no_retry_connection(
    att_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """TC-A04-b1 plus: exactly one appender entry, and exactly one provisioner connection
    requested after it (frozen no-retry message); then the frozen b1 sequence, including
    the same-connection count."""

    from haloflow.m01.provisioning.upgrade import UpgradeTestHooks  # census site

    admin, t = migrated_database, att_tenant
    log: list[tuple[str, ...]] = []
    probe = _Probe("b1", admin, t.tenant_id, log)
    _seed_t003(admin, t.tenant_id, "running")
    before = _r5_snapshot(admin, t)
    upgrade = _compose(role_logins, UpgradeTestHooks(appender_probe=probe), connect_log=log)
    task = _spawn(upgrade.run(_request(t, None)))
    await _bounded(task, B1_C_BOUND_SECONDS, "TC-A04-b1-c", role_logins, admin)
    error = await _refused(task, "TC-A04-b1")
    _assert_seam_order(probe, "TC-A04-b1")
    if not (probe.sent >= 1 and probe.visible_after_insert == 1):
        raise AttHarnessUnmet("L6_ATT_UNMET: TC-A04-b1-c: the injected reply drop did not occur")
    markers = [i for i, entry in enumerate(log) if entry == ("appender_entry",)]
    if not markers:
        raise AttHarnessUnmet("L6_ATT_UNMET: TC-A04-b1-c: the appender entry was not logged")
    p_after = sum(1 for entry in log[markers[0] + 1 :] if entry == ("connect", "P"))
    assert len(markers) == 1 and p_after == 1, "TC-A04-b1: no retry"
    # the frozen b1 sequence, verbatim and in its original order
    _assert_code(error, "MAINTENANCE_EVIDENCE_WRITE_FAILED", "classify", "TC-A04-b1")
    assert probe.visible_after_insert == 1, (
        "TC-A04-b1: commit observed independently before the drop"
    )
    _assert_r5(
        before, _r5_snapshot(admin, t), "TC-A04-b1", new_events=["started", "attempt_refused"]
    )
    assert (probe.invocations, probe.sent) == (1, 1), "TC-A04-b1: no retry"
