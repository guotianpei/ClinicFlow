"""Option B B3a: CONTROL rows (class C, pass now with no B3 code) and B2-BEHAVIOUR rows
(class B, run now and fail with a named B2 behaviour; v1r1 D4.1, v1r2 Item 3).

Class B rows assert the B3 property through the B3 interface when it exists, and otherwise
at the accepted B2 composition point. Today they fail on the named B2 behaviour; after B3
they run on the B3 interface. See b3a_support for the governing records.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import b2_support as s
import b3a_support as b3
import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    with s.cluster(tmp_path) as c:
        yield c
    s.reap()


@pytest.fixture
def inst(pg: s.PgCluster, tmp_path: Path) -> b3.Install:
    return b3.install(pg, tmp_path)


# =========================================================================== class C

def test_b3a_c1_fixture_ready_and_runtime_login_passes_f2(inst: b3.Install) -> None:
    """C (also pins G4 / M3): a fresh B2 install is `ready` and attested; the B2 runtime login
    passes F2 in its SHARED form inside a transaction (pg_control_system is executable)."""
    status = s.mod("provision").status(inst.env)
    assert (status["phase"], status["sealed"], status["attested"]) == ("ready", True, True)
    expected = s.slot(inst.c)
    assert expected is not None
    guard = s.mod("guard")
    with psycopg_connect(b3.runtime_conninfo(inst)) as conn, conn.transaction():
        guard.guard_sync(conn, expected, shared=True)


def psycopg_connect(conninfo: str) -> object:
    import psycopg

    return psycopg.connect(conninfo, autocommit=True)


def test_b3a_c2_recorder_detects_out_of_order_close() -> None:
    """C: the Recorder oracle is order-sensitive (a deliberate pool-before-port order is seen)."""
    r = b3.Recorder()
    for what in ("watcher", "writeback", "reminder", "due", "pool", "port", "lease"):
        r.note(what)
    assert r.order().index("pool") < r.order().index("port")
    assert r.order() != ["watcher", "writeback", "reminder", "due", "port", "pool", "lease"]


def test_b3a_c3_trace_parser_sees_guarded_and_unguarded(inst: b3.Install) -> None:
    """C: the server-log trace oracle (G1/G2) is sensitive. A hand-made guarded transaction
    shows the five F2 statements first; a hand-made statement outside a transaction is listed
    as `outside`; another application's statements are excluded."""
    b3.trace_on(inst)
    expected = s.slot(inst.c)
    guard = s.mod("guard")
    import psycopg

    with psycopg.connect(b3.runtime_conninfo(inst, application_name="b3a-c3"),
                         autocommit=True) as conn:
        with conn.transaction():
            guard.guard_sync(conn, expected, shared=True)
            conn.execute("SELECT 1 AS inside")
        conn.execute("SELECT 2 AS outside_probe")
    with psycopg.connect(b3.runtime_conninfo(inst, application_name="other-app"),
                         autocommit=True) as other:
        other.execute("SELECT 3 AS other_app")
    per = b3.wait_until(lambda: b3.trace(inst, "b3a-c3") or None, 5)
    ((pid, stmts),) = per.items()
    txs, outside = b3.transactions(stmts)
    assert any("outside_probe" in x for x in outside)
    (tx,) = [t for t in txs if any("inside" in x for x in t)]
    assert all(rx.search(st) for rx, st in zip(b3.F2_PREFIX, tx[:5], strict=True)), tx[:6]
    assert not any("other_app" in x for x in stmts)


def test_b3a_c4_journal_survives_os_exit(tmp_path: Path) -> None:
    """C: an external-effect line appended (fsync) immediately before os._exit survives."""
    path = tmp_path / "journal.jsonl"
    code = (f"import sys; sys.path.insert(0, {str(b3.HERE)!r}); import b3a_support as b3;"
            f" b3.Journal(__import__('pathlib').Path({str(path)!r})).append(effect='send',"
            " op_id='x'); import os; os._exit(97)")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={"PATH": os.environ.get("PATH", ""),
                            "PYTHONPATH": f"{s.SRC}{os.pathsep}{b3.HERE}"}, timeout=60)
    assert p.returncode == 97, p.stdout + p.stderr
    assert b3.Journal(path).lines() == [{"effect": "send", "op_id": "x"}]


def test_b3a_c5_sj_harness_drives_runtime_on_a_b2_install(inst: b3.Install) -> None:
    """C: the accepted S-J harness (test-side composition) runs real runtime activity on a
    real B2 install: one confirm write-back, an item claimed and resolved, J03 replies."""
    rec = b3.sj_flow(inst)
    assert rec["j01"]["observed_positive"] == 1, rec
    assert rec["j02"]["item_raised"] == 1, rec
    assert (rec["claim"], rec["resolve"]) == ("applied", "applied"), rec
    assert rec["not_yet_due"] == "applied", rec
    assert rec["ehr_writes"] == 1 and rec["shutdown"] == {}, rec


def test_b3a_c7_runtime_tables_premise_on_b2(inst: b3.Install) -> None:
    """C (SC1 premise, F-N1/F-S1): the composed paths change ONLY expected runtime-mutable
    tables on a real B2 install, none of the ruled install-sealed tables, nothing in `shared`,
    and the not-yet-due appointment leaves its schedule row."""
    before = b3.table_digests(inst)
    b3.sj_flow(inst)
    diff = b3.changed(before, b3.table_digests(inst))
    assert diff <= b3.EXPECTED_RUNTIME_MUTABLE, sorted(diff - b3.EXPECTED_RUNTIME_MUTABLE)
    assert not diff & b3.MUST_BE_SEALED
    assert not any(t.startswith("shared.") for t in diff)
    assert "{tenant}.m05_outbound_schedule" in diff
    assert "{tenant}.patient_event_links" in diff


@pytest.mark.parametrize("where", ["{schema}", "shared", "public"])
def test_b3a_c2b_extra_table_at_ready_already_refused_by_b2(inst: b3.Install,
                                                           where: str) -> None:
    """C (v1r2 Item 3, measured): B2's full seal already refuses an extra table at `ready`."""
    inst.admin(f"CREATE TABLE {where.format(schema=inst.schema)}.zz_extra (x int)")
    st = s.mod("provision").status(inst.env)
    assert (st["sealed"], st["attested"]) == (False, False)
    with pytest.raises(s.mod("errors").SyntheticRefused) as ei:
        s.mod("provision").init(inst.env, attest_flag=True)
    assert ei.value.code == "STATE_DIVERGED"


def test_b3a_c6_create_or_replace_keeps_the_oid(inst: b3.Install) -> None:
    """C (SC4c/g premise): CREATE OR REPLACE with the same signature keeps the OID and changes
    the body, so detection must come from the semantic digest, not the identity check."""
    schema = inst.schema
    inst.admin(f"CREATE FUNCTION {schema}.zz_probe() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    (oid1, src1), = inst.admin("SELECT p.oid, p.prosrc FROM pg_proc p JOIN pg_namespace n"
                               " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                               " AND p.proname = 'zz_probe'", (schema,))
    inst.admin(f"CREATE OR REPLACE FUNCTION {schema}.zz_probe() RETURNS int LANGUAGE sql"
               " AS 'SELECT 2'")
    (oid2, src2), = inst.admin("SELECT p.oid, p.prosrc FROM pg_proc p JOIN pg_namespace n"
                               " ON n.oid = p.pronamespace WHERE n.nspname = %s"
                               " AND p.proname = 'zz_probe'", (schema,))
    assert oid1 == oid2 and src1 != src2


# =========================================================================== class B

def _definer_command(inst: b3.Install) -> tuple[str, str, str]:
    import uuid

    (wid,), = inst.admin(f'SELECT workload_id::text FROM "{inst.schema}".m05_intake_workloads'
                         " WHERE workload_role = 'dispatch'")
    cmd = json.dumps({"schema_version": 1, "limit": 1, "attribution": {
        "principal_kind": "workload", "principal_id": wid, "execution_id": str(uuid.uuid4()),
        "auth_method": "test", "purpose_code": "operations", "request_id": None}})
    return cmd, inst.state["tenant_id"], f'SELECT "{inst.schema}".m05_outbound_expire(%s::jsonb)'


async def _runtime_connection(inst: b3.Install, app: str) -> object:
    """The runtime connection AS COMPOSED: B3's GuardedRuntimeConnection when it exists,
    otherwise the accepted B2 guarded_factory connection (the B2 composition point)."""
    guard = s.mod("guard")
    expected = s.slot(inst.c)
    cls = getattr(guard, "GuardedRuntimeConnection", None)
    conninfo = b3.runtime_conninfo(inst, application_name=app)
    if cls is None:
        return await guard.guarded_factory(conninfo, expected)()
    conn = await cls.connect(conninfo, autocommit=True)
    conn.expected = expected
    return conn


def test_b3a_g5pre_write_after_pool_reset_is_refused(inst: b3.Install) -> None:
    """B (G5-pre; measured on B2: claude_b3a-measure/b2-premise-measure.jsonl). After the pool
    reset (`DISCARD ALL`), a write-capable definer called outside any guarded transaction must
    be refused. B2 FAILS: the read-only default is lost and the UPDATE executes."""
    cmd, tenant, call = _definer_command(inst)

    async def body() -> tuple[str, str]:
        conn = await _runtime_connection(inst, "b3a-g5pre")
        try:
            await conn.execute("DISCARD ALL")  # type: ignore[attr-defined]
            ro = (await (await conn.execute(  # type: ignore[attr-defined]
                "SHOW default_transaction_read_only")).fetchone())[0]
            try:
                await conn.execute("SELECT pg_catalog.set_config('app.tenant_id', %s, false)",  # type: ignore[attr-defined]
                                   (tenant,))
                await conn.execute(call, (cmd,))  # type: ignore[attr-defined]
                return ro, "EXECUTED"
            except Exception as e:  # noqa: BLE001
                return ro, type(e).__name__
        finally:
            await conn.close()  # type: ignore[attr-defined]

    ro, outcome = asyncio.run(body())
    assert outcome != "EXECUTED", (
        f"write admitted after reset (read_only={ro}): a write-capable definer executed "
        "outside any guarded transaction")
    assert ro == "on", f"read-only default not re-applied after reset (read_only={ro})"


def test_b3a_g2pre_unguarded_write_never_reaches_the_server(inst: b3.Install) -> None:
    """B (G2-pre). A write-capable statement outside a guarded transaction must be refused
    BEFORE it reaches the server (client-side fence). B2 FAILS: the statement arrives (the
    server then refuses it as read-only, or executes it after a reset)."""
    b3.trace_on(inst)
    cmd, tenant, call = _definer_command(inst)

    async def body() -> None:
        conn = await _runtime_connection(inst, "b3a-g2pre")
        with contextlib.suppress(Exception):  # a B3 fence may refuse this too
            await conn.execute("SELECT pg_catalog.set_config('app.tenant_id', %s, false)",  # type: ignore[attr-defined]
                               (tenant,))
        try:
            await conn.execute(call, (cmd,))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001,S110 - the outcome is judged from the server log
            pass
        finally:
            await conn.close()  # type: ignore[attr-defined]

    asyncio.run(body())
    import time

    time.sleep(0.5)
    reached = [x for st in b3.trace(inst, "b3a-g2pre").values() for x in st
               if "m05_outbound_expire" in x]
    assert reached == [], "unguarded statement reached server"


def test_b3a_sc3pre_installer_accepts_legitimate_runtime_rows(inst: b3.Install) -> None:
    """B (SC3-pre; measured: claude_b3a-measure/b2-runtime-measure.jsonl). After legitimate
    runtime activity, `status` is sealed and an `init` re-run returns `ready`. With B3 the
    activity runs through the lifecycle (first-start marker); on B2 it runs through the
    test-side S-J harness. B2 FAILS: status not sealed, init STATE_DIVERGED (profile)."""
    try:
        lifecycle = s.mod("lifecycle")
    except ModuleNotFoundError:
        lifecycle = None
    if lifecycle is None:
        b3.sj_flow(inst)
    else:  # pragma: no cover - B3 path, exercised after implementation
        async def run() -> None:
            lc = lifecycle.SyntheticLifecycle(inst.env)
            await lc.start()
            lc.request_stop("TEST")
            await lc.wait_stopped()
        asyncio.run(run())
        b3.sj_flow(inst)
    status = s.mod("provision").status(inst.env)
    errors = s.mod("errors")
    try:
        phase = s.mod("provision").init(inst.env, attest_flag=True).phase
    except errors.SyntheticRefused as e:
        phase = f"refused {e.code} {list(e.detail)}"
    assert (status["sealed"], phase) == (True, "ready"), (
        f"installer rejects legitimate runtime rows: sealed={status['sealed']}, init={phase}")
