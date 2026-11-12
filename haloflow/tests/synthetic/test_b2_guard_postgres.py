"""Option B increment B2: reservation, guard, attestation and Alembic rows (PostgreSQL).

Every row runs on its OWN `initdb` cluster (b2_support.PgCluster; socket only). Governing records
and the interface: `b2_support.py`. Protocol F as aligned in B2-I1 (Codex 2026-11-12).

Rows
- B2-G0   CONTROL: fixture facts the protocol rests on. initdb comments the `postgres` database
          (so it is NOT the slot); the oid-10 slot is NULL; a plain LOGIN can read
          `pg_control_system()` and its own database oid.
- B2-O1   CONTROL: oracle sensitivity. Every fresh-baseline prep changes `snapshot()` (so the
          "no change" assertions below can fail).
- B2-O2   CONTROL: a silent verifier replacement changes the snapshot (digest only).
- B2-F1   first run on a fresh cluster: RESERVED; slot value exact
          (nonce, sysid, target oid, epoch).
- B2-F2   FULL fresh attestation, one row per class: CLUSTER_NOT_FRESH naming the class, refused
          BEFORE any reservation write; the full snapshot (all databases, catalogs and rows)
          is unchanged and the slot stays NULL.
- B2-F3   (R-b) a change injected between the read-only preflight and the reserving transaction:
          the serialized re-validation refuses; the full snapshot equals the pre-run snapshot
          plus ONLY the injected fixture (captured inside the hook).
- B2-F4   slot NULL with a recorded phase >= stamped: STATE_DIVERGED before any reserve; no change.
- B2-F5   owned re-run: TAKEN_OVER, the ONLY change is the epoch in the slot; another nonce:
          CLUSTER_NOT_FRESH(reservation_slot) without state, RESERVED_BY_OTHER with state;
          no change.
- B2-T2   the UNCHANGED original expected value on another database (sync guard and the guarded
          async factory), and on another cluster: TARGET_MISMATCH / CLUSTER_MISMATCH; both
          databases' full snapshots unchanged.
- B2-T3   begin_run with the same nonce against another target: TARGET_MISMATCH, no change.
- B2-G1   isolation: a guarded transaction under REPEATABLE READ is refused.
- B2-G2   coverage: a guarded-factory session refuses any write outside a guarded transaction
          (ReadOnlySqlTransaction); savepoints inside a guarded transaction are not re-guarded.
- B2-G3   late-transaction guard: while a takeover holds K_X before COMMIT, an old-epoch guarded
          transaction (sync and async) waits on K_X and is then refused EPOCH_SUPERSEDED.
- B2-G4   drain: an in-flight SHARED-guarded (runtime) transaction blocks a takeover until it
          commits; later shared transactions on the old epoch are refused. (Database level only;
          B3 scheduler shutdown stays a B3 obligation.)
- B2-E1   locked Alembic equivalence: `upgrade_guarded` vs stock `alembic upgrade head` give equal
          catalogs and rows; a second call is a no-op at 009.
- B2-E2   (A1/A2) barrier inside the one Alembic transaction: holder commits -> challenger no-op;
          holder's committing backend killed after all DDL -> zero roles survive, challenger runs
          once, result equals a single execution; holder's stale re-run refused.
- B2-E3   (A3) a takeover paused before COMMIT; an old-epoch Alembic run waits, then
          EPOCH_SUPERSEDED, nothing applied.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import psycopg
import pytest

pytestmark = pytest.mark.postgres

NONCE = "a1" * 16
NONCE2 = "b2" * 16


@pytest.fixture(autouse=True)
def _reap_children() -> Iterator[None]:
    yield
    s.reap()


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    with s.cluster(tmp_path) as c:
        yield c


def endpoint(c: s.PgCluster, db: str = s.TARGET_DB) -> Any:
    return s.mod("dsn").parse_admin_dsn(c.dsn(db), {})


def ident(c: s.PgCluster, db: str = s.TARGET_DB) -> tuple[str, int]:
    with c.connect(db) as d:
        sysid, oid = d.execute(
            "SELECT (SELECT system_identifier FROM pg_control_system())::text,"
            " (SELECT oid FROM pg_database WHERE datname=current_database())").fetchone()
    return str(sysid), int(oid)


def expected(c: s.PgCluster, nonce: str, epoch: str, db: str = s.TARGET_DB) -> str:
    sysid, oid = ident(c, db)
    return str(s.mod("guard").reservation_value(nonce, sysid, oid, epoch))


def refused(fn: Any, *a: Any, **kw: Any) -> Any:
    with pytest.raises(s.mod("errors").SyntheticRefused) as ei:
        fn(*a, **kw)
    return ei.value


# ---------------------------------------------------------------- controls

def test_b2_g0_fixture_facts_control(pg: s.PgCluster) -> None:
    """CONTROL."""
    with pg.connect("postgres") as a:
        assert a.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database"
                         " WHERE datname='postgres'").fetchone()[0] == \
            "default administrative connection database"
        a.execute("CREATE ROLE plain_probe LOGIN")
    assert s.slot(pg) is None
    with pg.connect(user="plain_probe") as p:
        sysid, oid = p.execute(
            "SELECT (SELECT system_identifier FROM pg_control_system())::text,"
            " (SELECT oid FROM pg_database WHERE datname=current_database())").fetchone()
    assert sysid and int(oid) >= 16384


FRESH_PREPS: dict[str, tuple[str, list[str]]] = {
    "role": ("postgres", ["CREATE ROLE stray_role"]),
    "role_attribute": ("postgres", ["ALTER ROLE postgres CONNECTION LIMIT 50"]),
    "membership": ("postgres", ["GRANT pg_monitor TO postgres"]),
    "role_setting": ("postgres", ["ALTER ROLE postgres SET work_mem = '8MB'"]),
    "database": ("postgres", ["CREATE DATABASE stray_db"]),
    "database_acl": ("postgres", [f"REVOKE CONNECT ON DATABASE {s.TARGET_DB} FROM PUBLIC"]),
    "target_comment": ("postgres", [f"COMMENT ON DATABASE {s.TARGET_DB} IS 'x'"]),
    "reservation_slot": ("postgres", ["COMMENT ON ROLE postgres IS 'someone else'"]),
    "schema": (s.TARGET_DB, ["CREATE SCHEMA stray"]),
    "extension": (s.TARGET_DB, ["CREATE EXTENSION citext"]),
    "relation": ("postgres", ["CREATE TABLE public.stray(x int)"]),
    "function": (s.TARGET_DB, [
        "CREATE FUNCTION public.stray() RETURNS int LANGUAGE sql AS 'SELECT 1'"]),
    "type": (s.TARGET_DB, ["CREATE TYPE public.stray AS ENUM ('a')"]),
    "event_trigger": ("postgres", [
        "CREATE FUNCTION public.et() RETURNS event_trigger LANGUAGE plpgsql AS 'BEGIN END'",
        "CREATE EVENT TRIGGER stray_et ON ddl_command_start EXECUTE FUNCTION public.et()"]),
    "public_owner": (s.TARGET_DB, ["ALTER SCHEMA public OWNER TO postgres"]),
    "public_acl": (s.TARGET_DB, ["GRANT CREATE ON SCHEMA public TO PUBLIC"]),
    "default_acl": (s.TARGET_DB, ["ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO PUBLIC"]),
    "fdw": (s.TARGET_DB, ["CREATE FOREIGN DATA WRAPPER stray_fdw"]),
    "publication": (s.TARGET_DB, ["CREATE PUBLICATION stray_pub"]),
}


def prep(c: s.PgCluster, cls: str) -> None:
    db, stmts = FRESH_PREPS[cls]
    with c.connect(db) as d:
        for q in stmts:
            d.execute(q)


@pytest.mark.parametrize("cls", sorted(FRESH_PREPS))
def test_b2_o1_oracle_sensitivity_control(pg: s.PgCluster, cls: str) -> None:
    """CONTROL: each fresh-baseline violation is visible to the snapshot oracle."""
    before = s.snapshot(pg)
    prep(pg, cls)
    assert s.snapshot(pg) != before


def test_b2_o2_verifier_replacement_visible_control(pg: s.PgCluster) -> None:
    """CONTROL: replacing one non-NULL verifier with another changes the snapshot (digest), so
    "no change" assertions catch a silent password repair; the verifier itself is not in it."""
    with pg.connect("postgres") as a:
        a.execute("CREATE ROLE probe_login LOGIN PASSWORD 'first-value'")
    before = s.snapshot(pg)
    with pg.connect("postgres") as a:
        a.execute("ALTER ROLE probe_login PASSWORD 'second-value'")
    after = s.snapshot(pg)
    assert set(s.diff(before, after)) == {"verifier_digests"}
    assert "SCRAM-SHA-256$" not in json.dumps(after)


def test_b2_tablespace_oracle_and_class_control(pg: s.PgCluster, tmp_path: Path) -> None:
    """CONTROL for the tablespace class (needs a directory, so not in FRESH_PREPS)."""
    loc = tmp_path / "ts"
    loc.mkdir(mode=0o700)
    before = s.snapshot(pg)
    with pg.connect("postgres") as a:
        a.execute(f"CREATE TABLESPACE stray_ts LOCATION '{loc}'")
    assert s.snapshot(pg) != before


# ---------------------------------------------------------------- F rows

def test_b2_f1_first_run_reserves(pg: s.PgCluster) -> None:
    guard = s.mod("guard")
    assert guard.begin_run(endpoint(pg), None, NONCE, "e1") == "RESERVED"
    assert s.slot(pg) == expected(pg, NONCE, "e1")


def test_b2_f2_fresh_classes_exact() -> None:
    assert s.mod("attest").FRESH_CLASSES == (
        "role", "role_attribute", "membership", "role_setting", "database", "database_acl",
        "tablespace", "target_comment", "reservation_slot", "schema", "extension", "relation",
        "function", "type", "event_trigger", "public_owner", "public_acl", "default_acl", "fdw",
        "publication")


@pytest.mark.parametrize("cls", sorted(FRESH_PREPS))
def test_b2_f2_full_fresh_attestation(pg: s.PgCluster, cls: str) -> None:
    prep(pg, cls)
    before = s.snapshot(pg)
    e = refused(s.mod("guard").begin_run, endpoint(pg), None, NONCE, "e1")
    assert e.code == "CLUSTER_NOT_FRESH"
    assert cls in e.detail, e.detail
    assert s.diff(before, s.snapshot(pg)) == {}
    if cls != "reservation_slot":
        assert s.slot(pg) is None


def test_b2_f2_tablespace(pg: s.PgCluster, tmp_path: Path) -> None:
    loc = tmp_path / "ts"
    loc.mkdir(mode=0o700)
    with pg.connect("postgres") as a:
        a.execute(f"CREATE TABLESPACE stray_ts LOCATION '{loc}'")
    before = s.snapshot(pg)
    e = refused(s.mod("guard").begin_run, endpoint(pg), None, NONCE, "e1")
    assert (e.code, "tablespace" in e.detail) == ("CLUSTER_NOT_FRESH", True)
    assert s.diff(before, s.snapshot(pg)) == {}


def test_b2_f3_change_between_preflight_and_reserve(pg: s.PgCluster) -> None:
    hooks, guard = s.mod("hooks"), s.mod("guard")
    before = s.snapshot(pg)
    captured: dict[str, Any] = {}

    def fault(point: str, info: Any) -> None:
        if point == "reserve.before_txn":
            with pg.connect("postgres") as a:
                a.execute("CREATE ROLE injected_concurrent_role")
            captured["expected"] = s.snapshot(pg)

    hooks.FAULT = fault
    try:
        e = refused(guard.begin_run, endpoint(pg), None, NONCE, "e1")
    finally:
        hooks.FAULT = None
    assert (e.code, "role" in e.detail) == ("CLUSTER_NOT_FRESH", True)
    assert "expected" in captured, "the hook point reserve.before_txn was not reached"
    after = s.snapshot(pg)
    assert s.diff(captured["expected"], after) == {}, "only the injected fixture may differ"
    assert s.diff(before, after).keys() == {"roles"}
    assert s.slot(pg) is None


@pytest.mark.parametrize("phase", ["stamped", "migrated", "ready"])
def test_b2_f4_missing_slot_after_stamped(pg: s.PgCluster, phase: str) -> None:
    before = s.snapshot(pg)
    e = refused(s.mod("guard").begin_run, endpoint(pg), phase, NONCE, "e1")
    assert e.code == "STATE_DIVERGED"
    assert s.diff(before, s.snapshot(pg)) == {}
    assert s.slot(pg) is None


def test_b2_f5_owned_rerun_and_other_nonce(pg: s.PgCluster) -> None:
    guard = s.mod("guard")
    ep = endpoint(pg)
    assert guard.begin_run(ep, None, NONCE, "e1") == "RESERVED"
    before = s.snapshot(pg)
    assert guard.begin_run(ep, "intent", NONCE, "e2") == "TAKEN_OVER"
    after = s.snapshot(pg)
    assert set(s.diff(before, after)) == {"shared_comments"}
    assert s.slot(pg) == expected(pg, NONCE, "e2")
    e = refused(guard.begin_run, ep, None, NONCE2, "x")
    assert (e.code, e.detail) == ("CLUSTER_NOT_FRESH", ("reservation_slot",))
    assert refused(guard.begin_run, ep, "intent", NONCE2, "x").code == "RESERVED_BY_OTHER"
    assert s.diff(after, s.snapshot(pg)) == {}


# ---------------------------------------------------------------- T rows

def other_db(c: s.PgCluster) -> str:
    name = "haloflow_test_syn_other"
    with c.connect("postgres") as a:
        a.execute(f"CREATE DATABASE {name}")
    return name


def test_b2_t2_original_intent_on_wrong_database(pg: s.PgCluster) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    exp = expected(pg, NONCE, "e1")                      # ORIGINAL, unchanged
    other = other_db(pg)
    before = s.snapshot(pg)
    with psycopg.connect(pg.dsn(other), autocommit=True) as o:
        o.execute("SET default_transaction_read_only = on")
        with pytest.raises(s.mod("errors").SyntheticRefused) as ei, o.transaction():
            guard.guard_sync(o, exp)
            o.execute("CREATE TABLE public.wrong_target_sync(x int)")
    assert ei.value.code == "TARGET_MISMATCH"

    async def async_path() -> str:
        conn = await guard.guarded_factory(pg.dsn(other), exp)()
        try:
            async with conn.transaction():
                await conn.execute("CREATE TABLE public.wrong_target_async(x int)")
            return "COMMITTED"
        except s.mod("errors").SyntheticRefused as e:
            return str(e.code)
        finally:
            await conn.close()

    assert asyncio.run(async_path()) == "TARGET_MISMATCH"
    assert s.diff(before, s.snapshot(pg)) == {}, "neither database may change"


def test_b2_t2_original_intent_on_other_cluster(pg: s.PgCluster, tmp_path: Path) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    exp = expected(pg, NONCE, "e1")
    with s.cluster(tmp_path / "second") as c2:
        before = s.snapshot(c2)

        async def async_path() -> str:
            conn = await guard.guarded_factory(c2.dsn(), exp)()
            try:
                async with conn.transaction():
                    await conn.execute("CREATE TABLE public.x(x int)")
                return "COMMITTED"
            except s.mod("errors").SyntheticRefused as e:
                return str(e.code)
            finally:
                await conn.close()

        assert asyncio.run(async_path()) == "CLUSTER_MISMATCH"
        assert s.diff(before, s.snapshot(c2)) == {}


def test_b2_t3_same_nonce_other_target(pg: s.PgCluster) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    other = other_db(pg)
    before = s.snapshot(pg)
    e = refused(guard.begin_run, endpoint(pg, other), "intent", NONCE, "e2")
    assert e.code == "TARGET_MISMATCH"
    assert s.diff(before, s.snapshot(pg)) == {}


# ---------------------------------------------------------------- G rows

def test_b2_g1_isolation_refused(pg: s.PgCluster) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    exp = expected(pg, NONCE, "e1")
    with pg.connect() as d:
        d.execute("SET default_transaction_read_only = on")
        with pytest.raises(s.mod("errors").SyntheticRefused) as ei, d.transaction():
            d.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            guard.guard_sync(d, exp)
        assert ei.value.code == "ISOLATION_NOT_READ_COMMITTED"


def test_b2_g2_guarded_session_coverage(pg: s.PgCluster) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    exp = expected(pg, NONCE, "e1")

    async def run() -> tuple[str, str]:
        conn = await guard.guarded_factory(pg.dsn(), exp)()
        try:
            try:
                await conn.execute("CREATE TABLE public.unguarded(x int)")
                outside = "WRITE_ACCEPTED"
            except psycopg.errors.ReadOnlySqlTransaction:
                outside = "ReadOnlySqlTransaction"
            async with conn.transaction():
                await conn.execute("CREATE TABLE public.guarded(x int)")
                async with conn.transaction():            # savepoint: not re-guarded
                    await conn.execute("INSERT INTO public.guarded VALUES (1)")
            n = (await (await conn.execute("SELECT count(*) FROM public.guarded")).fetchone())
            return outside, str(n[0] if n else None)
        finally:
            await conn.close()

    assert asyncio.run(run()) == ("ReadOnlySqlTransaction", "1")


def _takeover_paused(c: s.PgCluster, tmp_path: Path) -> s.Child:
    env = s.environ_for(c, tmp_path / "unused")
    child = s.spawn(tmp_path / "bar", "begin_run", environ=env,
                    pause="takeover.before_commit",
                    extra={"phase": "intent", "nonce": NONCE, "epoch": "e2"})
    child.reached("takeover.before_commit")
    return child


def test_b2_g3_late_transaction_guard_sync(pg: s.PgCluster, tmp_path: Path) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    old = expected(pg, NONCE, "e1")
    child = _takeover_paused(pg, tmp_path)
    result: dict[str, Any] = {}

    def holder() -> None:
        with pg.connect() as d:
            d.execute("SET default_transaction_read_only = on")
            try:
                with d.transaction():
                    guard.guard_sync(d, old)
                    d.execute("CREATE TABLE public.late(x int)")
                result["code"] = "COMMITTED"
            except s.mod("errors").SyntheticRefused as e:
                result["code"] = e.code

    import threading

    t = threading.Thread(target=holder)
    t.start()
    s.wait_for(lambda: s.waiting_advisory(pg))
    child.go("takeover.before_commit")
    t.join(60)
    assert child.result() == {"ok": True, "result": "TAKEN_OVER"}
    assert result == {"code": "EPOCH_SUPERSEDED"}
    with pg.connect() as d:
        assert d.execute("SELECT to_regclass('public.late')").fetchone()[0] is None


def test_b2_g3_late_transaction_guard_async(pg: s.PgCluster, tmp_path: Path) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    old = expected(pg, NONCE, "e1")
    child = _takeover_paused(pg, tmp_path)

    async def holder() -> str:
        conn = await guard.guarded_factory(pg.dsn(), old)()
        try:
            async with conn.transaction():
                await conn.execute("CREATE TABLE public.late(x int)")
            return "COMMITTED"
        except s.mod("errors").SyntheticRefused as e:
            return str(e.code)
        finally:
            await conn.close()

    async def run() -> str:
        task = asyncio.create_task(holder())
        while not s.waiting_advisory(pg):
            await asyncio.sleep(0.05)
        child.go("takeover.before_commit")
        return await task

    assert asyncio.run(run()) == "EPOCH_SUPERSEDED"
    assert child.result() == {"ok": True, "result": "TAKEN_OVER"}


def test_b2_g4_shared_guard_drain(pg: s.PgCluster, tmp_path: Path) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    old = expected(pg, NONCE, "e1")
    with pg.connect() as a:
        a.execute("CREATE TABLE public.rt(n int)")
    rt = pg.connect()
    rt.execute("SET default_transaction_read_only = on")
    tx = rt.transaction()
    tx.__enter__()
    guard.guard_sync(rt, old, shared=True)
    rt.execute("INSERT INTO public.rt VALUES (1)")
    env = s.environ_for(pg, tmp_path / "unused")
    child = s.spawn(tmp_path / "bar", "begin_run", environ=env,
                    extra={"phase": "intent", "nonce": NONCE, "epoch": "e2"})
    s.wait_for(lambda: s.waiting_advisory(pg))
    assert child.proc.poll() is None, "takeover must wait for the in-flight shared transaction"
    tx.__exit__(None, None, None)
    assert child.result() == {"ok": True, "result": "TAKEN_OVER"}
    with pytest.raises(s.mod("errors").SyntheticRefused) as ei, rt.transaction():
        guard.guard_sync(rt, old, shared=True)
        rt.execute("INSERT INTO public.rt VALUES (2)")
    assert ei.value.code == "EPOCH_SUPERSEDED"
    rt.close()
    with pg.connect() as a:
        assert a.execute("SELECT array_agg(n) FROM public.rt").fetchone()[0] == [1]


# ---------------------------------------------------------------- E rows

def stock_alembic(c: s.PgCluster) -> None:
    url = f"postgresql://postgres@/{s.TARGET_DB}?host={c.sock}&port={c.port}"
    env = {"PATH": os.environ.get("PATH", ""), "HALOFLOW_MIGRATION_DATABASE_URL": url}
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=s.HALOFLOW,
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]


def comparable(snap: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snap.items() if k != "shared_comments"}


def test_b2_e1_locked_alembic_equivalence(pg: s.PgCluster, tmp_path: Path) -> None:
    guard, migrate = s.mod("guard"), s.mod("migrate")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    exp = expected(pg, NONCE, "e1")
    assert migrate.upgrade_guarded(pg.dsn(), exp) == (None, "009")
    assert migrate.upgrade_guarded(pg.dsn(), exp) == ("009", "009")
    with s.cluster(tmp_path / "stock") as ref:
        stock_alembic(ref)
        assert s.diff(comparable(s.snapshot(ref)), comparable(s.snapshot(pg))) == {}


def _migrate_child(c: s.PgCluster, tmp_path: Path, name: str, exp: str, pause: str = "") -> s.Child:
    env = s.environ_for(c, tmp_path / "unused")
    return s.spawn(tmp_path / name, "migrate", environ=env, pause=pause,
                   extra={"conninfo": c.dsn(), "expected": exp})


def test_b2_e2_alembic_holder_commits(pg: s.PgCluster, tmp_path: Path) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    old = expected(pg, NONCE, "e1")
    a = _migrate_child(pg, tmp_path, "a", old, pause="migrate.before_commit")
    a.reached("migrate.before_commit")
    import threading

    box: dict[str, Any] = {}
    t = threading.Thread(target=lambda: box.update(
        r=guard.begin_run(endpoint(pg), "intent", NONCE, "e2")))
    t.start()
    s.wait_for(lambda: s.waiting_advisory(pg))
    a.go("migrate.before_commit")
    t.join(60)
    assert a.result() == {"ok": True, "before": None, "after": "009"}
    assert box == {"r": "TAKEN_OVER"}
    new = expected(pg, NONCE, "e2")
    b = _migrate_child(pg, tmp_path, "b", new)
    assert b.result() == {"ok": True, "before": "009", "after": "009"}
    stale = _migrate_child(pg, tmp_path, "c", old)
    assert stale.result()["code"] == "EPOCH_SUPERSEDED"


def test_b2_e2_alembic_lock_loss_before_commit(pg: s.PgCluster, tmp_path: Path) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    old = expected(pg, NONCE, "e1")
    a = _migrate_child(pg, tmp_path, "a", old, pause="migrate.before_commit")
    info = a.reached("migrate.before_commit")
    import threading

    box: dict[str, Any] = {}
    t = threading.Thread(target=lambda: box.update(
        r=guard.begin_run(endpoint(pg), "intent", NONCE, "e2")))
    t.start()
    s.wait_for(lambda: s.waiting_advisory(pg))
    assert s.terminate_backend(pg, int(info["backend_pid"]))
    t.join(60)
    a.go("migrate.before_commit")
    assert a.result()["ok"] is False
    assert box == {"r": "TAKEN_OVER"}
    with pg.connect("postgres") as d:
        assert d.execute("SELECT count(*) FROM pg_roles WHERE oid >= 16384").fetchone()[0] == 0
    b = _migrate_child(pg, tmp_path, "b", expected(pg, NONCE, "e2"))
    assert b.result() == {"ok": True, "before": None, "after": "009"}
    assert _migrate_child(pg, tmp_path, "c", old).result()["code"] == "EPOCH_SUPERSEDED"
    with s.cluster(tmp_path / "single") as ref:
        stock_alembic(ref)
        assert s.diff(comparable(s.snapshot(ref)), comparable(s.snapshot(pg))) == {}


def test_b2_e3_alembic_fenced_after_takeover(pg: s.PgCluster, tmp_path: Path) -> None:
    guard = s.mod("guard")
    guard.begin_run(endpoint(pg), None, NONCE, "e1")
    old = expected(pg, NONCE, "e1")
    tk = _takeover_paused(pg, tmp_path)
    a = _migrate_child(pg, tmp_path, "a", old)
    s.wait_for(lambda: s.waiting_advisory(pg))
    tk.go("takeover.before_commit")
    assert tk.result() == {"ok": True, "result": "TAKEN_OVER"}
    assert a.result()["code"] == "EPOCH_SUPERSEDED"
    with pg.connect() as d:
        assert d.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0] is None
