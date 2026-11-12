"""Option B increment B2: phased, resumable synthetic init (PostgreSQL, per-case clusters).

Governing records and the pinned interface: `b2_support.py`. Matrix: B2-I1 continuation §4 as
amended by v1r1 §2 (Codex ALIGN 2026-11-12).

Pinned identities (all derived from the 32-hex nonce created once at `intent`):
  tenant_id = "clinic-syn-<nonce[:8]>"      schema_key = "tenant_<nonce[:16]>"
  staff_id = uuid5(UUID(nonce), "staff")     activation actor = uuid5(UUID(nonce), "activation")
  ticket_reference = "SYN-<nonce[:8]>"      logins = haloflow_syn_<nonce[:8]>_{runtime,migrator,
                                             provisioner,m05}
Pinned seeded values: settings lead 2 days, send 10:00, window 09:00-19:00, clinic_tz
"America/New_York", display name "HaloVox Demo Clinic", settings_version initial+1; one staff
member (active, clinic_admin, follow-up read+act) through the first-member bootstrap; one
activation grant.

Rows
- B2-I0   CONTROL: HALOFLOW_SYNTHETIC_PG_BIN present and PostgreSQL 17.x (class-B: never skip).
- B2-I1   positive init -> ready: state file keys and modes; phase history; exact seeded rows;
          login roles exact attributes, one membership, `search_path=''`, SCRAM verifier equal to
          one computed independently from the state's password/salt/iterations; no ALTER of an
          existing role's password on re-run (verifier unchanged).
- B2-I2   `status`: exact keys, no secret (passwords, note keys, admin DSN password).
- B2-I3   SCRAM authentication in a scram-sha-256 cluster: intent password logs in, a wrong one
          fails.
- B2-I4   crash matrix: for every phase, crash (a) before its commit, (b) after its commit before
          the state write, (c) inside the state writer after the temp fsync; re-run -> ready;
          result equal to a single execution (semantic rows minus ONLY the explicit
          nondeterministic columns, identities normalised), secrets byte-identical to `intent`,
          no duplicate roles/tenant/grant/staff/event. Includes matrix row 5
          (stamped, after commit).
- B2-I5   diverged variants: a foreign object at each phase boundary -> STATE_DIVERGED; the only
          change is the owned epoch bump; never repaired or adopted. Owned-but-altered login and
          settings -> STATE_DIVERGED.
- B2-I6   concurrency: two state directories, concurrent first init -> exactly one ready, the
          other INSTALL_LOCKED with zero change; a running RuntimeLease -> init INSTALL_LOCKED.
- B2-I7   copied state directory (same nonce) with the holder paused INSIDE a provisioning
          guarded transaction: (P1) holder transaction killed; (P2) holder continues and is then
          fenced. The copy resumes to ready; equal to a single execution; ledger attempt differs
          on at most the interrupted unit, by exactly +1.
- B2-I8   refusals with no change: the CI suite cluster (read-only attestation); state missing
          against an initialised cluster; state copied to another cluster; corrupt state.
- B2-I9   secrets never in logs at DEBUG.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b2_support as s
import psycopg
import pytest

pytestmark = pytest.mark.postgres

STATE_KEYS = {"schema_version", "phase", "cluster_sysid", "target", "target_oid", "nonce",
              "tenant_id", "schema_key", "staff_id", "activation_actor_id", "ticket_reference",
              "logins", "note_keys"}
SETTINGS = {"lead_days": 2, "send_time": "10:00:00", "window_start": "09:00:00",
            "window_end": "19:00:00", "clinic_tz": "America/New_York",
            "clinic_display_name": "HaloVox Demo Clinic"}


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    with s.cluster(tmp_path) as c:
        yield c


def init(c: s.PgCluster, state: Path, **kw: Any) -> Any:
    return s.mod("provision").init(s.environ_for(c, state), attest_flag=True, **kw)


def init_child(c: s.PgCluster, state: Path, bar: Path, **kw: Any) -> s.Child:
    return s.spawn(bar, "init", environ=s.environ_for(c, state), **kw)


def refused(fn: Any, *a: Any, **kw: Any) -> Any:
    with pytest.raises(s.mod("errors").SyntheticRefused) as ei:
        fn(*a, **kw)
    return ei.value


def scram_verifier(password: str, salt_b64: str, iterations: int) -> str:
    salt = base64.b64decode(salt_b64)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client = hmac.new(salted, b"Client Key", "sha256").digest()
    stored = hashlib.sha256(client).digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{salt_b64}${b(stored)}:{b(server)}"


def identities(st: dict[str, Any]) -> dict[str, str]:
    n = st["nonce"]
    return {n: "<NONCE>", n[:16]: "<NONCE16>", n[:8]: "<NONCE8>",
            st["staff_id"]: "<STAFF>", st["activation_actor_id"]: "<ACTOR>"}


def normalised(c: s.PgCluster, st: dict[str, Any]) -> dict[str, list[str]]:
    sem = s.semantic(c, st["schema_key"])
    subs = sorted(identities(st).items(), key=lambda kv: -len(kv[0]))
    out: dict[str, list[str]] = {}
    for table, rows in sem.items():
        def norm(text: str) -> str:
            for k, v in subs:
                text = text.replace(k, v)
            return text
        out[norm(table)] = sorted(norm(r) for r in rows)
    return out


def roles_normalised(c: s.PgCluster, st: dict[str, Any]) -> list[Any]:
    snap = s.snapshot(c, rows=False)
    text = json.dumps([snap["roles"], snap["memberships"], snap["role_settings"]])
    return list(json.loads(text.replace(st["nonce"][:8], "<NONCE8>")))


_REFERENCE: dict[str, Any] = {}


@pytest.fixture(scope="module")
def ref_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ref")


def reference_of(root: Path) -> dict[str, Any]:
    """ONE uninterrupted init on its own cluster: the single-execution oracle. Built lazily
    INSIDE the first row that needs it (so a missing module fails that row's body, not a
    fixture) and cached for the module."""
    if not _REFERENCE:
        with s.cluster(root) as c:
            st_dir = s.state_dir(root)
            assert init(c, st_dir).phase == "ready"
            st = s.read_state(st_dir)
            _REFERENCE.update(semantic=normalised(c, st), roles=roles_normalised(c, st),
                              attempts=s.ledger_attempts(c))
    return _REFERENCE


def assert_single_execution(c: s.PgCluster, st_dir: Path, reference: dict[str, Any], *,
                            interrupted: str | None = None) -> None:
    st = s.read_state(st_dir)
    assert st["phase"] == "ready"
    assert s.diff(reference["semantic"], normalised(c, st)) == {}
    assert roles_normalised(c, st) == reference["roles"]
    got = s.ledger_attempts(c)
    assert set(got) == set(reference["attempts"])
    diffs = {k: (reference["attempts"][k], v) for k, v in got.items()
             if v != reference["attempts"][k]}
    if interrupted is None:
        assert len(diffs) <= 1 and all(g == r + 1 for r, g in diffs.values()), diffs
    else:
        assert set(diffs) <= {interrupted}
        assert all(g == r + 1 for r, g in diffs.values()), diffs


# ---------------------------------------------------------------- I0 / I1 / I2 / I3

def test_b2_i0_pg_bin_control() -> None:
    """CONTROL: the B2 PostgreSQL rows FAIL (never skip) without 17.x binaries."""
    assert s.pg_bin().is_dir()


def test_b2_i1_positive_init(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    st = s.read_state(st_dir)
    assert set(st) == STATE_KEYS and st["schema_version"] == 1 and st["phase"] == "ready"
    assert stat.S_IMODE((st_dir / "state.json").stat().st_mode) == 0o600
    n = st["nonce"]
    assert len(n) == 32 and int(n, 16) >= 0
    assert (st["tenant_id"], st["schema_key"], st["ticket_reference"]) == (
        f"clinic-syn-{n[:8]}", f"tenant_{n[:16]}", f"SYN-{n[:8]}")
    assert st["staff_id"] == str(uuid.uuid5(uuid.UUID(n), "staff"))
    assert st["activation_actor_id"] == str(uuid.uuid5(uuid.UUID(n), "activation"))
    assert set(st["logins"]) == set(s.LOGIN_SUFFIXES)
    assert set(st["note_keys"]) == {"hmac", "enc", "version"} and st["note_keys"]["version"] == 1
    schema = st["schema_key"]
    with pg.connect() as d:
        assert d.execute("SELECT tenant_id, schema_key, lifecycle_state, schema_version"
                         " FROM shared.tenants").fetchall() == [
            (st["tenant_id"], schema, "active", 94)]
        acts = d.execute(f"SELECT change, actor_id::text, ticket_reference FROM {schema}"
                         ".m05_activation_records").fetchall()
        assert acts == [("grant", st["activation_actor_id"], st["ticket_reference"])]
        row = d.execute(f"SELECT lead_days, send_time::text, window_start::text,"
                        f" window_end::text, clinic_tz, clinic_display_name, settings_version"
                        f" FROM {schema}.m05_reminder_settings").fetchone()
        assert dict(zip(list(SETTINGS) + ["v"], row, strict=True)) == SETTINGS | {"v": 2}
        assert d.execute(f"SELECT staff_id::text, status FROM {schema}.m01_staff").fetchall() \
            == [(st["staff_id"], "active")]
        assert d.execute(f"SELECT staff_id::text, role FROM {schema}.m01_staff_roles"
                         ).fetchall() == [(st["staff_id"], "clinic_admin")]
        assert sorted(d.execute(f"SELECT access FROM {schema}.m01_staff_followup_access"
                                ).fetchall()) == [("act",), ("read",)]
        assert d.execute(f"SELECT staff_id::text, ticket_reference FROM {schema}"
                         ".m01_staff_bootstrap_log").fetchall() == [
            (st["staff_id"], st["ticket_reference"])]
    with pg.connect("postgres") as a:
        for suffix in s.LOGIN_SUFFIXES:
            spec = st["logins"][suffix]
            assert spec["role"] == f"haloflow_syn_{n[:8]}_{suffix}"
            attrs = a.execute(
                "SELECT rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
                " rolreplication, rolbypassrls, rolconnlimit, rolpassword"
                " FROM pg_authid WHERE rolname=%s", (spec["role"],)).fetchone()
            assert attrs[:8] == (False, True, False, False, True, False, False, -1)
            assert attrs[8] == scram_verifier(spec["password"], spec["salt"], spec["iterations"])
            members = a.execute(
                "SELECT g.rolname, m.admin_option FROM pg_auth_members m"
                " JOIN pg_roles g ON g.oid=m.roleid JOIN pg_roles r ON r.oid=m.member"
                " WHERE r.rolname=%s", (spec["role"],)).fetchall()
            assert len(members) == 1 and members[0][1] is False
            cfg = a.execute("SELECT setconfig FROM pg_db_role_setting s JOIN pg_roles r"
                            " ON r.oid=s.setrole WHERE r.rolname=%s", (spec["role"],)).fetchall()
            assert cfg == [(["search_path=\"\""],)] or cfg == [(["search_path=''"],)]
        verifiers = a.execute("SELECT rolname, rolpassword FROM pg_authid WHERE rolname LIKE"
                              " 'haloflow_syn_%' ORDER BY 1").fetchall()
    before = s.snapshot(pg)
    assert init(pg, st_dir).phase == "ready"                   # owned re-run at ready
    after = s.snapshot(pg)
    assert set(s.diff(before, after)) <= {"shared_comments"}
    with pg.connect("postgres") as a:
        assert a.execute("SELECT rolname, rolpassword FROM pg_authid WHERE rolname LIKE"
                         " 'haloflow_syn_%' ORDER BY 1").fetchall() == verifiers


def test_b2_i2_status_exact_and_secret_free(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    init(pg, st_dir)
    st = s.read_state(st_dir)
    out = s.mod("provision").status(s.environ_for(pg, st_dir))
    assert set(out) == {"phase", "tenant_id", "schema_key", "tenant_count", "staff_count",
                        "open_items", "attested"}
    assert (out["phase"], out["tenant_count"], out["staff_count"], out["open_items"],
            out["attested"]) == ("ready", 1, 1, 0, True)
    text = json.dumps(out)
    secrets = [v["password"] for v in st["logins"].values()] + [
        st["note_keys"]["hmac"], st["note_keys"]["enc"]]
    assert not any(x in text for x in secrets)


def test_b2_i3_scram_authentication(tmp_path: Path) -> None:
    with s.cluster(tmp_path, scram=True) as c:
        st_dir = s.state_dir(tmp_path)
        assert init(c, st_dir).phase == "ready"
        spec = s.read_state(st_dir)["logins"]["runtime"]
        with psycopg.connect(c.dsn(user=spec["role"], password=spec["password"])) as ok:
            assert ok.execute("SELECT current_user").fetchone()[0] == spec["role"]
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(c.dsn(user=spec["role"], password=spec["password"] + "x")).close()


# ---------------------------------------------------------------- I4 crash matrix

PHASE_COMMITS = [p for p in s.PHASES if p not in ("intent", "ready")]
WRITE_INDEX = {p: i + 1 for i, p in enumerate(s.PHASES)}   # n-th state write == phase p


def crash_points() -> list[tuple[str, str]]:
    pts = []
    for p in PHASE_COMMITS:
        pts.append((p, f"phase.{p}.before_commit"))
        pts.append((p, f"phase.{p}.after_commit"))
    for p in s.PHASES:
        pts.append((p, f"state.after_temp_fsync#{WRITE_INDEX[p]}"))
    return pts


@pytest.mark.parametrize(("phase", "point"), crash_points(), ids=[p[1] for p in crash_points()])
def test_b2_i4_crash_matrix(pg: s.PgCluster, tmp_path: Path, ref_root: Path,
                            phase: str, point: str) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash=point)
    first.crashed()
    intent_secrets = None
    if (st_dir / "state.json").exists():
        st0 = s.read_state(st_dir)
        intent_secrets = (st0["logins"], st0["note_keys"])
    again = init_child(pg, st_dir, tmp_path / "bar2")
    assert again.result() == {"ok": True, "phase": "ready"}
    st = s.read_state(st_dir)
    if intent_secrets is not None:
        assert (st["logins"], st["note_keys"]) == intent_secrets, "secrets must never rotate"
    assert_single_execution(pg, st_dir, reference_of(ref_root))


# ---------------------------------------------------------------- I5 diverged

FOREIGN = "CREATE ROLE foreign_after_crash"


@pytest.mark.parametrize("phase", PHASE_COMMITS)
def test_b2_i5_foreign_object_diverged(pg: s.PgCluster, tmp_path: Path, phase: str) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash=f"phase.{phase}.after_commit")
    first.crashed()
    with pg.connect("postgres") as a:
        a.execute(FOREIGN)
    before = s.snapshot(pg)
    e = refused(init, pg, st_dir)
    assert e.code == "STATE_DIVERGED"
    assert set(s.diff(before, s.snapshot(pg))) <= {"shared_comments"}, \
        "only the owned epoch bump may change"
    with pg.connect("postgres") as a:
        assert a.execute("SELECT count(*) FROM pg_roles WHERE rolname='foreign_after_crash'"
                         ).fetchone()[0] == 1, "never repaired or dropped"


def test_b2_i5_owned_login_altered_diverged(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.logins.after_commit")
    first.crashed()
    st = s.read_state(st_dir)
    with pg.connect("postgres") as a:
        a.execute(f"ALTER ROLE {st['logins']['runtime']['role']} CONNECTION LIMIT 3")
        a.execute(f"ALTER ROLE {st['logins']['m05']['role']} PASSWORD 'changed-by-someone'")
    before = s.snapshot(pg)
    assert refused(init, pg, st_dir).code == "STATE_DIVERGED"
    assert set(s.diff(before, s.snapshot(pg))) <= {"shared_comments"}


def test_b2_i5_owned_settings_altered_diverged(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.settings.after_commit")
    first.crashed()
    st = s.read_state(st_dir)
    with pg.connect() as d:
        d.execute(f"UPDATE {st['schema_key']}.m05_reminder_settings SET lead_days = 3")
    before = s.snapshot(pg)
    assert refused(init, pg, st_dir).code == "STATE_DIVERGED"
    assert set(s.diff(before, s.snapshot(pg))) <= {"shared_comments"}


# ---------------------------------------------------------------- I6 concurrency / lease

def test_b2_i6_two_state_dirs_concurrent_first_init(pg: s.PgCluster, tmp_path: Path) -> None:
    a_dir, b_dir = s.state_dir(tmp_path, "a"), s.state_dir(tmp_path, "b")
    a = init_child(pg, a_dir, tmp_path / "bara", pause="phase.migrated.before_commit")
    a.reached("phase.migrated.before_commit")
    before = s.snapshot(pg)
    e = refused(init, pg, b_dir)
    assert e.code == "INSTALL_LOCKED"
    assert s.diff(before, s.snapshot(pg)) == {}
    assert sorted(p.name for p in b_dir.iterdir()) in ([], ["state.lock"])
    a.go("phase.migrated.before_commit")
    assert a.result() == {"ok": True, "phase": "ready"}


def test_b2_i6_runtime_lease_excludes_init(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    guard, dsn = s.mod("guard"), s.mod("dsn")
    lease = guard.RuntimeLease(dsn.parse_admin_dsn(pg.dsn(), {}))
    lease.acquire()
    try:
        before = s.snapshot(pg)
        assert refused(init, pg, st_dir).code == "INSTALL_LOCKED"
        assert s.diff(before, s.snapshot(pg)) == {}
    finally:
        lease.release()


def test_b2_i6_init_excludes_runtime_lease(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    a = init_child(pg, st_dir, tmp_path / "bar", pause="phase.tenant.before_commit")
    a.reached("phase.tenant.before_commit")
    guard, dsn = s.mod("guard"), s.mod("dsn")
    lease = guard.RuntimeLease(dsn.parse_admin_dsn(pg.dsn(), {}))
    assert refused(lease.acquire).code == "INSTALL_LOCKED"
    a.go("phase.tenant.before_commit")
    assert a.result() == {"ok": True, "phase": "ready"}


# ---------------------------------------------------------------- I7 copies (P1 / P2)

@pytest.mark.parametrize("kill", [True, False], ids=["P1_kill_holder_txn", "P2_holder_continues"])
def test_b2_i7_copied_state_mid_provisioning(pg: s.PgCluster, tmp_path: Path,
                                             ref_root: Path, kill: bool) -> None:
    a_dir = s.state_dir(tmp_path, "a")
    a = init_child(pg, a_dir, tmp_path / "bara", pause="guarded_txn#60")
    info = a.reached(["guarded_txn", "60"][0])
    b_dir = s.copy_state(a_dir, tmp_path / "b")
    assert s.read_state(b_dir)["phase"] in ("migrated", "logins")
    assert s.terminate_backend(pg, int(info["install_lock_pid"])), "lose the holder's K_S"
    b = init_child(pg, b_dir, tmp_path / "barb")
    s.wait_for(lambda: s.waiting_advisory(pg))
    assert b.proc.poll() is None, "the copy's takeover must wait for the holder's open txn"
    if kill:
        assert s.terminate_backend(pg, int(info["backend_pid"]))
    a.go("guarded_txn")
    assert b.result() == {"ok": True, "phase": "ready"}
    ra = a.result()
    assert ra["ok"] is False and ra["code"] in ("EPOCH_SUPERSEDED", "INSTALL_LOCK_LOST", None)
    assert_single_execution(pg, b_dir, reference_of(ref_root))


# ---------------------------------------------------------------- I8 refusals

def test_b2_i8_suite_cluster_refused_read_only(tmp_path: Path) -> None:
    url = os.environ.get("HALOFLOW_TEST_DATABASE_URL")
    assert url, "HALOFLOW_TEST_DATABASE_URL is required (CI provides the suite service)"

    def catalog() -> list[Any]:
        with psycopg.connect(url, autocommit=True) as c:
            return [c.execute(q).fetchall() for q in s.SNAPSHOT_SHARED.values()]

    before = catalog()
    env = {"HALOFLOW_SYNTHETIC_ADMIN_DSN": url, "HALOFLOW_SYNTHETIC_STATE":
           str(s.state_dir(tmp_path)), "HALOFLOW_SYNTHETIC_DISPOSABLE": "1"}
    e = refused(s.mod("provision").init, env, attest_flag=True)
    assert e.code in ("CLUSTER_NOT_FRESH", "DSN_DBNAME_PREFIX")
    assert catalog() == before


def test_b2_i8_state_missing_against_initialised_cluster(pg: s.PgCluster, tmp_path: Path) -> None:
    init(pg, s.state_dir(tmp_path, "a"))
    before = s.snapshot(pg)
    e = refused(init, pg, s.state_dir(tmp_path, "b"))
    assert (e.code, "reservation_slot" in e.detail) == ("CLUSTER_NOT_FRESH", True)
    assert s.diff(before, s.snapshot(pg)) == {}


def test_b2_i8_state_copied_to_another_cluster(pg: s.PgCluster, tmp_path: Path) -> None:
    a_dir = s.state_dir(tmp_path, "a")
    init(pg, a_dir)
    with s.cluster(tmp_path / "second") as c2:
        b_dir = s.copy_state(a_dir, tmp_path / "b")
        before = s.snapshot(c2)
        assert refused(init, c2, b_dir).code == "CLUSTER_MISMATCH"
        assert s.diff(before, s.snapshot(c2)) == {}


def test_b2_i8_corrupt_state(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    init(pg, st_dir)
    (st_dir / "state.json").write_text("{")
    before = s.snapshot(pg)
    assert refused(init, pg, st_dir).code == "STATE_CORRUPT"
    assert s.diff(before, s.snapshot(pg)) == {}


# ---------------------------------------------------------------- I9 logs

def test_b2_i9_no_secret_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with s.cluster(tmp_path, scram=True) as c:
        st_dir = s.state_dir(tmp_path)
        caplog.set_level(logging.DEBUG)
        init(c, st_dir)
        s.mod("provision").status(s.environ_for(c, st_dir))
        st = s.read_state(st_dir)
        text = caplog.text
        for x in [v["password"] for v in st["logins"].values()] + [
                st["note_keys"]["hmac"], st["note_keys"]["enc"], "bootstrap-test-only"]:
            assert x not in text
