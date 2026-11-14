"""Option B increment B2: phased, resumable synthetic init (PostgreSQL, per-case clusters).

Governing records and the pinned interface: `b2_support.py`. Matrix: B2-I1 continuation §4 as
amended by v1r1 §2 (Codex ALIGN 2026-11-12).

Pinned identities (all derived from the 32-hex nonce created once at `intent`):
  tenant_id = "clinic-syn-<nonce[:8]>"      schema_key = "tenant_<nonce[:16]>"
  staff_id = uuid5(UUID(nonce), "staff")     activation actor = uuid5(UUID(nonce), "activation")
  ticket_reference = "SYN-<nonce[:8]>"      logins = haloflow_syn_<nonce[:8]>_<suffix>, each a
                                             member of exactly its group (b2_support.LOGIN_GROUPS)
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
          the state write, (c) inside the state writer after the temp fsync, plus (d) after the
          reservation alone (D1, before the target stamp); re-run -> ready;
          result equal to a single execution (semantic rows minus ONLY the explicit
          nondeterministic columns, identities normalised), secrets byte-identical to `intent`,
          no duplicate roles/tenant/grant/staff/event; ledger attempts differ ONLY on units the
          DB showed `running` at the crash, by exactly +1 (zero differences otherwise).
          Includes matrix row 5 (stamped, after commit).
- B2-I4b  ambiguous commit outcome, per phase: (ack) the COMMIT really happened but its
          acknowledgement is lost (fault raised inside the commit error scope); (rb) the
          committing backend is killed before COMMIT (real rollback through connection loss).
          BEFORE the retry: durable proof the fault fired at its exact point, the killed backend
          is gone, and phase p's DB effect is PRESENT (ack) / ABSENT (rb), read independently
          (tenant: its first guarded transaction's tenant row).
          The run fails closed without writing state p; the re-run -> ready, equal to a single
          execution, secrets unchanged. Interruption evidence for the attempt pin is read from
          the ledger at the failure point, never from the implementation.
- B2-I4c  CONTROL (real PostgreSQL, no B2 source): the I4b pre-retry discriminator accepts the
          correct order and rejects `commit_ack` fired before COMMIT and a pre-phase failure.
- B2-I5   diverged variants: a foreign object at each phase boundary -> STATE_DIVERGED; the only
          change is the owned epoch bump; never repaired or adopted. Owned-but-altered login
          (separately: password only, attribute only, extra membership) and settings ->
          STATE_DIVERGED; every stored verifier unchanged (compared in memory, never printed).
- B2-I6   concurrency: two state directories, concurrent first init -> exactly one ready, the
          other INSTALL_LOCKED with zero change; a running RuntimeLease -> init INSTALL_LOCKED.
- B2-I7   copied state directory (same nonce) with the holder paused INSIDE a provisioning
          guarded transaction: (P1) holder transaction killed; (P2) holder continues and is then
          fenced. The copy resumes to ready; equal to a single execution. The interrupted units
          are read from the ledger while the copy's takeover holds K_X before COMMIT (the
          holder can commit nothing more from that point); attempts differ on exactly those
          units, by exactly +1.
- B2-I8   refusals with no change: the CI suite cluster (read-only attestation); state missing
          against an initialised cluster; state copied to another cluster; corrupt state.
- B2-I9   secrets never in logs at DEBUG.
- B2-R1   (S1, Codex REVISE 2026-11-12T19:06Z) completed phases are reconciled against the
          state's `seal` before any write: a tamper of completed owned state at `ready` or at an
          intermediate recorded phase -> STATE_DIVERGED, missing objects never recreated, the
          only change is the owned epoch bump; `status` reports sealed/attested false and is
          strictly read-only.
- B2-R2   (S2) the pending phase's committed effect (crash after its commit, before the state
          write) is validated exactly: a same-count value / audit / key / ledger tamper ->
          STATE_DIVERGED with no change.
- B2-C    (S3) `python -m haloflow.synthetic init|status`: attestation required, exit 0 / 2,
          refusals print only the code, output carries no secret, no traceback.
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import b2_support as s
import psycopg
import pytest

pytestmark = pytest.mark.postgres

STATE_KEYS = {"schema_version", "phase", "cluster_sysid", "target", "target_oid", "nonce",
              "tenant_id", "schema_key", "staff_id", "activation_actor_id", "ticket_reference",
              "logins", "note_keys", "support", "seal"}
STATUS_KEYS = {"phase", "tenant_id", "schema_key", "tenant_count", "staff_count", "open_items",
               "attested", "sealed", "pending_effect"}
SETTINGS = {"lead_days": 2, "send_time": "10:00:00", "window_start": "09:00:00",
            "window_end": "19:00:00", "clinic_tz": "America/New_York",
            "clinic_display_name": "HaloVox Demo Clinic"}


@pytest.fixture(autouse=True)
def _reap_children() -> Iterator[None]:
    yield
    s.reap()


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
    """Cross-install normalisation: ONLY exact values recorded in (or derived from) this
    install's intent are replaced by tokens; every other value must be equal as is. Same-install
    no-change/retry assertions never use this (they compare raw values)."""
    n = st["nonce"]
    base = uuid.UUID(n)
    sup = st["support"]
    out = {n: "<NONCE>", n[:16]: "<NONCE16>", n[:8]: "<NONCE8>",
           st["staff_id"]: "<STAFF>", st["activation_actor_id"]: "<ACTOR>",
           sup["grant_id"]: "<GRANT>", sup["decision_ref"]: "<DECISION>",
           sup["starts_at"]: "<GRANT_STARTS>", sup["expires_at"]: "<GRANT_EXPIRES>"}
    for name in ("staff-first-admin", "staff-access-read", "staff-access-act",
                 "staff-execution", "provision"):
        out[str(uuid.uuid5(base, name))] = f"<UUID:{name}>"
    return out


def normalised(c: s.PgCluster, st: dict[str, Any]) -> dict[str, list[str]]:
    sem = s.semantic(c, st["schema_key"])
    with c.connect("postgres") as a:
        oid = a.execute("SELECT oid::text FROM pg_roles WHERE rolname = %s",
                        (st["logins"]["support"]["role"],)).fetchone()
    ids = identities(st)
    if oid is not None:
        ids[f'"login_oid": "{oid[0]}"'] = '"login_oid": "<SUPPORT_OID>"'   # oid -> JSON text
        ids[f'"login_oid": {oid[0]}'] = '"login_oid": "<SUPPORT_OID>"'
    subs = sorted(ids.items(), key=lambda kv: -len(kv[0]))
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
                            interrupted: frozenset[str]) -> None:
    """`interrupted` is REQUIRED and comes from independent DB evidence (s.ledger_running at the
    failure point). Exactly those units carry one extra attempt; every other unit is equal."""
    st = s.read_state(st_dir)
    assert st["phase"] == "ready"
    assert s.diff(reference["semantic"], normalised(c, st)) == {}
    assert roles_normalised(c, st) == reference["roles"]
    got = s.ledger_attempts(c)
    assert set(got) == set(reference["attempts"])
    diffs = {k: (reference["attempts"][k], v) for k, v in got.items()
             if v != reference["attempts"][k]}
    assert diffs == {u: (reference["attempts"][u], reference["attempts"][u] + 1)
                     for u in interrupted}, (sorted(interrupted), diffs)


# ---------------------------------------------------------------- I0 / I1 / I2 / I3

def test_b2_i0_pg_bin_control() -> None:
    """CONTROL: the B2 PostgreSQL rows FAIL (never skip) without 17.x binaries."""
    assert s.pg_bin().is_dir()


def test_b2_i1_positive_init(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    st = s.read_state(st_dir)
    assert set(st) == STATE_KEYS and st["schema_version"] == 1 and st["phase"] == "ready"
    assert set(st["seal"]) == {"profile", "point", "nondet", "identities", "ledger"}
    profile = s.mod("installed_profile")
    assert (st["seal"]["profile"], st["seal"]["point"]) == (profile.PROFILE_ID,
                                                            profile.COMPLETION["ready"])
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
        base = uuid.UUID(n)
        assert d.execute(f"SELECT client_action_id::text FROM {schema}.m01_staff_bootstrap_keys"
                         ).fetchall() == [(str(uuid.uuid5(base, "staff-first-admin")),)]
        assert sorted(d.execute(f"SELECT client_action_id::text FROM {schema}"
                                ".m01_staff_action_keys").fetchall()) == sorted(
            [(str(uuid.uuid5(base, "staff-access-read")),),
             (str(uuid.uuid5(base, "staff-access-act")),)])
        sup = st["support"]
        starts, expires = (datetime.fromisoformat(sup["starts_at"]),
                           datetime.fromisoformat(sup["expires_at"]))
        assert expires - starts == timedelta(hours=2), "bounded grant window"
        assert d.execute(
            "SELECT grant_id::text, actor_id, tenant_id, ticket_reference, purpose_code,"
            " capabilities, approver_id, starts_at, expires_at, state, break_glass, created_at"
            " FROM shared.support_access_grants").fetchall() == [(
                sup["grant_id"], f"syn-support-{n[:8]}", st["tenant_id"],
                st["ticket_reference"], "synthetic-install", ["l1w.roles.bootstrap"],
                "synthetic-init", starts, expires, "active", False, starts)]
        assert d.execute(
            "SELECT login_name::text, actor_id, added_at, decision_ref,"
            " login_oid = (SELECT oid FROM pg_roles WHERE rolname = login_name)"
            " FROM shared.support_operator_logins").fetchall() == [(
                st["logins"]["support"]["role"], f"syn-support-{n[:8]}", starts,
                sup["decision_ref"], True)]
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
                "SELECT g.rolname, m.admin_option, m.inherit_option, m.set_option,"
                " pg_get_userbyid(m.grantor) FROM pg_auth_members m"
                " JOIN pg_roles g ON g.oid=m.roleid JOIN pg_roles r ON r.oid=m.member"
                " WHERE r.rolname=%s", (spec["role"],)).fetchall()
            assert members == [(s.LOGIN_GROUPS[suffix], False, True, True, "postgres")]
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
    assert set(out) == STATUS_KEYS
    assert (out["phase"], out["tenant_count"], out["staff_count"], out["open_items"],
            out["attested"], out["sealed"], out["pending_effect"]) == (
        "ready", 1, 1, 0, True, True, None)
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
# The staff phase is three guarded transactions (Codex 20261112T155235Z): T1 support
# registration + grant (admin), T2 first-member bootstrap (support login), T3a follow-up read and
# T3b follow-up act (runtime login; t029 admits ONE semantic request per transaction, measured:
# COMPOSITION_UNSUPPORTED). Each is its own crash / ambiguous-commit boundary.
COMMIT_POINTS = [p for p in PHASE_COMMITS if p != "staff"] + [
    "staff.t1", "staff.t2", "staff.t3a", "staff.t3b"]


def state_phase_before(point: str) -> str:
    phase = point.split(".")[0]
    return s.PHASES[s.PHASES.index(phase) - 1]
WRITE_INDEX = {p: i + 1 for i, p in enumerate(s.PHASES)}   # n-th state write == phase p


def crash_points() -> list[tuple[str, str]]:
    pts = [("intent", "reserve.after_commit")]                # D1 only, before the stamp
    for p in COMMIT_POINTS:
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
    interrupted = s.ledger_running(pg)                     # independent evidence, at the crash
    again = init_child(pg, st_dir, tmp_path / "bar2")
    assert again.result() == {"ok": True, "phase": "ready"}
    st = s.read_state(st_dir)
    if intent_secrets is not None:
        assert (st["logins"], st["note_keys"]) == intent_secrets, "secrets must never rotate"
    assert_single_execution(pg, st_dir, reference_of(ref_root), interrupted=interrupted)


def phase_effect(c: s.PgCluster, st: dict[str, Any], phase: str) -> bool:
    """INDEPENDENT read of whether phase p's committed DB effect exists (the tenant phase: its
    FIRST guarded transaction, which registers the tenant row)."""
    n, schema = st["nonce"], st["schema_key"]

    def one(db: str, q: str, args: Any = None) -> Any:
        with c.connect(db) as d:
            row = d.execute(q, args).fetchone()
        return None if row is None else row[0]

    def has(table: str) -> bool:
        return one(s.TARGET_DB, "SELECT to_regclass(%s) IS NOT NULL", (table,)) is True

    if phase == "stamped":
        return one("postgres", "SELECT shobj_description(oid,'pg_database') FROM pg_database"
                   " WHERE datname=%s", (s.TARGET_DB,)) == f"haloflow-synthetic:{n}"
    if phase == "migrated":
        return has("public.alembic_version") and one(
            s.TARGET_DB, "SELECT version_num FROM public.alembic_version") == "009"
    if phase == "logins":
        return one("postgres", "SELECT count(*) FROM pg_roles WHERE rolname LIKE %s",
                   (f"haloflow_syn_{n[:8]}_%",)) == len(s.LOGIN_GROUPS)
    if phase == "tenant":
        return has("shared.tenants") and one(
            s.TARGET_DB, "SELECT count(*) FROM shared.tenants WHERE tenant_id=%s",
            (st["tenant_id"],)) == 1
    if phase == "profile":
        return has(f"{schema}.m05_intake_workloads") and one(
            s.TARGET_DB, f"SELECT count(*) FROM {schema}.m05_intake_workloads") == 4
    if phase == "activated":
        return has(f"{schema}.m05_activation_records") and one(
            s.TARGET_DB, f"SELECT count(*) FROM {schema}.m05_activation_records") == 1
    if phase == "settings":
        return has(f"{schema}.m05_reminder_settings") and one(
            s.TARGET_DB, f"SELECT settings_version FROM {schema}.m05_reminder_settings") == 2
    if phase == "staff.t1":
        return has("shared.support_access_grants") and one(
            s.TARGET_DB, "SELECT (SELECT count(*) FROM shared.support_access_grants)"
            " + (SELECT count(*) FROM shared.support_operator_logins)") == 2
    if phase == "staff.t2":
        return has(f"{schema}.m01_staff") and one(
            s.TARGET_DB, f"SELECT count(*) FROM {schema}.m01_staff") == 1
    if phase in ("staff.t3a", "staff.t3b"):
        want = ["read"] if phase == "staff.t3a" else ["act", "read"]
        if not has(f"{schema}.m01_staff_followup_access"):
            return False
        with c.connect() as d:
            got = sorted(r[0] for r in d.execute(
                f"SELECT access FROM {schema}.m01_staff_followup_access").fetchall())
        if phase == "staff.t3b":
            return got == want
        return "read" in got
    raise AssertionError(phase)


def assert_fault_outcome(c: s.PgCluster, bar: Path, st: dict[str, Any], phase: str, kind: str,
                         point: str) -> None:
    """Before ANY retry: the fault fired at exactly its point (durable proof), the killed
    backend is really gone, and the DB shows the phase's effect present (lost ack of a REAL
    commit) or absent (real rollback)."""
    hit_file = bar / "fault.hit"
    assert hit_file.exists(), "the fault never fired (a pre-phase failure cannot pass)"
    hit = json.loads(hit_file.read_text())
    assert hit["point"] == point, hit
    if kind == "rolled_back":
        assert hit["terminated"] is True
        with c.connect("postgres") as a:
            gone = a.execute("SELECT count(*) FROM pg_stat_activity WHERE pid=%s",
                             (hit["backend_pid"],)).fetchone()
        assert gone == (0,), "the committing backend must really be gone"
    expected = kind == "ack_lost"
    assert phase_effect(c, st, phase) is expected, (
        "ack_lost: the COMMIT must really have happened" if expected else
        "rolled_back: nothing of the phase may be committed")


@pytest.mark.parametrize("order", ["correct", "mutant_ack_before_commit", "mutant_prephase"])
def test_b2_i4b_discriminator_control(tmp_path: Path, order: str) -> None:
    """CONTROL (real PostgreSQL, no B2 source): the pre-retry checks above reject an
    implementation whose `commit_ack` fires BEFORE COMMIT (that is a rollback in disguise) and
    one that fails before the phase runs; they accept the correct order."""
    with s.cluster(tmp_path) as c:
        with c.connect() as d:
            d.execute("CREATE TABLE public.toy_effect(x int)")
        bar = tmp_path / "bar"
        bar.mkdir()

        def fire(point: str) -> None:
            (bar / "fault.hit").write_text(json.dumps({"point": point}))
            raise psycopg.OperationalError("toy fault")

        conn = c.connect()
        try:
            if order == "mutant_prephase":
                raise psycopg.OperationalError("failed before the phase")
            with conn.transaction():
                conn.execute("INSERT INTO public.toy_effect VALUES (1)")
                if order == "mutant_ack_before_commit":
                    fire("phase.toy.commit_ack")
            fire("phase.toy.commit_ack")                    # correct: after a real COMMIT
        except psycopg.OperationalError:
            pass
        finally:
            conn.close()

        def toy_effect() -> bool:
            with c.connect() as d:
                return d.execute("SELECT count(*) FROM public.toy_effect").fetchone() == (1,)

        def check() -> None:
            hit_file = bar / "fault.hit"
            assert hit_file.exists(), "fault never fired"
            assert json.loads(hit_file.read_text())["point"] == "phase.toy.commit_ack"
            assert toy_effect() is True, "ack_lost requires the commit to be real"

        if order == "correct":
            check()
        else:
            with pytest.raises(AssertionError):
                check()


@pytest.mark.parametrize("kind", ["ack_lost", "rolled_back"])
@pytest.mark.parametrize("phase", COMMIT_POINTS)
def test_b2_i4b_ambiguous_commit(pg: s.PgCluster, tmp_path: Path, ref_root: Path, phase: str,
                                 kind: str) -> None:
    st_dir = s.state_dir(tmp_path)
    fault = {"raise_at": f"phase.{phase}.commit_ack"} if kind == "ack_lost" else {
        "kill_at": f"phase.{phase}.before_commit"}
    first = init_child(pg, st_dir, tmp_path / "bar1", **fault)
    r = first.result()
    assert r["ok"] is False, r
    assert r.get("type") != "ModuleNotFoundError", r
    st0 = s.read_state(st_dir)
    previous = state_phase_before(phase)
    assert st0["phase"] == previous, "no state write for a phase whose commit is not known"
    assert_fault_outcome(pg, tmp_path / "bar1", st0, phase, kind, next(iter(fault.values())))
    secrets = (st0["logins"], st0["note_keys"])
    interrupted = s.ledger_running(pg)
    again = init_child(pg, st_dir, tmp_path / "bar2")
    assert again.result() == {"ok": True, "phase": "ready"}
    st = s.read_state(st_dir)
    assert (st["logins"], st["note_keys"]) == secrets
    assert_single_execution(pg, st_dir, reference_of(ref_root), interrupted=interrupted)


# ---------------------------------------------------------------- I5 diverged

FOREIGN = "CREATE ROLE foreign_after_crash"


@pytest.mark.parametrize("phase", COMMIT_POINTS)
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


def _verifiers(c: s.PgCluster) -> dict[str, Any]:
    with c.connect("postgres") as a:
        return dict(a.execute("SELECT rolname, rolpassword FROM pg_authid").fetchall())


TAMPER = {
    "password_only": "ALTER ROLE {role} PASSWORD 'changed-by-someone'",
    "attribute_only": "ALTER ROLE {role} CONNECTION LIMIT 3",
    "extra_membership": "GRANT haloflow_runtime TO {role}",
}


@pytest.mark.parametrize("kind", sorted(TAMPER))
def test_b2_i5_owned_login_altered_diverged(pg: s.PgCluster, tmp_path: Path, kind: str) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.logins.after_commit")
    first.crashed()
    st = s.read_state(st_dir)
    role = st["logins"]["audit_projector"]["role"]
    with pg.connect("postgres") as a:
        a.execute(TAMPER[kind].format(role=role))
    before, verifiers = s.snapshot(pg), _verifiers(pg)
    assert refused(init, pg, st_dir).code == "STATE_DIVERGED"
    assert set(s.diff(before, s.snapshot(pg))) <= {"shared_comments"}
    unchanged = _verifiers(pg) == verifiers               # in memory; never printed
    assert unchanged, "a stored verifier was changed (silent repair)"


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


# ---------------------------------------------------------------- I5b support grant

def _later(monkeypatch: pytest.MonkeyPatch) -> None:
    """The passage of time, through the installer's clock pin `provision._utcnow` ONLY: no
    owned row and no intent value is rewritten (Codex 20261112T194049Z)."""
    later = datetime.now().astimezone() + timedelta(hours=3)
    monkeypatch.setattr(s.mod("provision"), "_utcnow", lambda: later)


def test_b2_i5b_expired_grant_before_t2_fails_closed(pg: s.PgCluster, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.staff.t1.after_commit")
    first.crashed()
    _later(monkeypatch)
    before = s.snapshot(pg)
    e = refused(init, pg, st_dir)
    assert (e.code, e.detail) == ("STATE_DIVERGED", ("support_grant_expired",))
    assert set(s.diff(before, s.snapshot(pg))) <= {"shared_comments"}, \
        "no silent renewal or replacement of the grant"


def test_b2_i5b_expired_grant_after_t2_completes(pg: s.PgCluster, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.staff.t2.after_commit")
    first.crashed()
    _later(monkeypatch)
    with pg.connect() as d:
        grant = d.execute("SELECT * FROM shared.support_access_grants").fetchall()
    assert init(pg, st_dir).phase == "ready"            # T3 needs no support authority
    with pg.connect() as d:
        assert d.execute("SELECT * FROM shared.support_access_grants").fetchall() == grant
        schema = s.read_state(st_dir)["schema_key"]
        assert sorted(d.execute(f"SELECT access FROM {schema}.m01_staff_followup_access"
                                ).fetchall()) == [("act",), ("read",)]


def test_b2_i5b_expired_grant_at_ready_verifies(pg: s.PgCluster, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """At `ready` the owned rows are sealed (rewriting them is a tamper, B2-R1), so the passage
    of time is simulated through the installer's clock pin `provision._utcnow` only."""
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    _later(monkeypatch)
    before = s.snapshot(pg)
    assert init(pg, st_dir).phase == "ready"            # read-only verification
    assert set(s.diff(before, s.snapshot(pg))) <= {"shared_comments"}


@pytest.mark.parametrize("tamper", [
    "UPDATE shared.support_access_grants SET capabilities = ARRAY['l1w.roles.bootstrap',"
    " 'extra']",
    "UPDATE shared.support_access_grants SET ticket_reference = 'OTHER'",
    "UPDATE shared.support_operator_logins SET actor_id = 'someone-else'",
], ids=["capabilities", "ticket", "operator_actor"])
def test_b2_i5b_support_rows_tampered_diverged(pg: s.PgCluster, tmp_path: Path,
                                              tamper: str) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.staff.t1.after_commit")
    first.crashed()
    with pg.connect() as d:
        d.execute(tamper)
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
    b = init_child(pg, b_dir, tmp_path / "barb", pause="takeover.before_commit")
    s.wait_for(lambda: s.waiting_advisory(pg))
    assert b.proc.poll() is None, "the copy's takeover must wait for the holder's open txn"
    if kill:
        assert s.terminate_backend(pg, int(info["backend_pid"]))
    a.go("guarded_txn")
    b.reached("takeover.before_commit")
    interrupted = s.ledger_running(pg)        # holder can commit nothing more from here on
    b.go("takeover.before_commit")
    assert b.result() == {"ok": True, "phase": "ready"}
    ra = a.result()
    assert ra["ok"] is False
    if not kill:
        assert ra["code"] == "EPOCH_SUPERSEDED", "the holder's next guarded txn is fenced"
    assert_single_execution(pg, b_dir, reference_of(ref_root), interrupted=interrupted)


# ---------------------------------------------------------------- I8 refusals

def test_b2_i8_suite_cluster_refused_read_only(tmp_path: Path) -> None:
    url = os.environ.get("HALOFLOW_TEST_DATABASE_URL")
    assert url, "HALOFLOW_TEST_DATABASE_URL is required (CI provides the suite service)"

    def catalog() -> list[Any]:
        with psycopg.connect(url, autocommit=True) as c:
            return [c.execute(q).fetchall() for q in s.SNAPSHOT_SHARED.values()]

    # The expected code is chosen from the server address read INDEPENDENTLY here (Codex ALIGN
    # 20261112T192821Z): behind a container port publish the server sees a non-loopback address
    # and A1 refuses TARGET_MISMATCH; on true loopback/socket the fresh baseline refuses.
    with psycopg.connect(url, autocommit=True) as c:
        addr = c.execute("SELECT host(inet_server_addr())").fetchone()[0]
    expected = "CLUSTER_NOT_FRESH" if addr in (None, "127.0.0.1", "::1") else "TARGET_MISMATCH"
    if expected == "CLUSTER_NOT_FRESH":
        # PREMISE, asserted read-only BEFORE init (Codex cce26d12): on true loopback the refusal
        # rests on the shared suite cluster being non-fresh. A fresh one would let this negative
        # control install into it, so the row fails here instead. The loopback refusal itself
        # is covered independently on a disposable cluster (nonfresh_cluster_refused_on_loopback).
        ep = s.mod("dsn").parse_admin_dsn(url, {})
        premise = s.mod("attest").fresh_failures(ep, ep.target)
        assert premise, ("PREMISE NOT MET: the suite cluster is fresh on loopback; this row needs "
                         "a used suite service (as in CI) and must not install into it")
    before = catalog()
    env = {"HALOFLOW_SYNTHETIC_ADMIN_DSN": url, "HALOFLOW_SYNTHETIC_STATE":
           str(s.state_dir(tmp_path)), "HALOFLOW_SYNTHETIC_DISPOSABLE": "1"}
    e = refused(s.mod("provision").init, env, attest_flag=True)
    assert e.code == expected
    assert catalog() == before


def test_b2_i8_nonfresh_cluster_refused_on_loopback(pg: s.PgCluster, tmp_path: Path) -> None:
    """True-loopback (socket) coverage of the fresh-baseline refusal, independent of how CI
    hosts its suite service."""
    with pg.connect("postgres") as a:
        a.execute("CREATE ROLE suite_like_role")
    before = s.snapshot(pg)
    e = refused(init, pg, s.state_dir(tmp_path))
    assert e.code == "CLUSTER_NOT_FRESH"
    assert s.diff(before, s.snapshot(pg)) == {}


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


# ---------------------------------------------------------------- R1 / R2 reconciliation (S1, S2)

def tamper(c: s.PgCluster, statements: list[str], db: str = s.TARGET_DB) -> None:
    """Out-of-band change as the superuser, bypassing triggers and FK checks (an operator or
    attacker editing owned state directly). PROVES the change happened: every UPDATE/DELETE/
    INSERT affects >= 1 row, and the snapshot differs afterwards (a zero-row tamper can never
    pass as a meaningful red case)."""
    before = s.snapshot(c)
    with c.connect(db) as d, d.transaction():
        d.execute("SET LOCAL session_replication_role = replica")
        for q in statements:
            cur = d.execute(q)
            if q.lstrip().split()[0].upper() in ("UPDATE", "DELETE", "INSERT"):
                assert cur.rowcount >= 1, f"tamper changed no row: {q}"
    assert s.diff(before, s.snapshot(c)) != {}, "tamper changed nothing"


def _comments_without_slot(snap: dict[str, Any]) -> list[Any]:
    return [r for r in snap["shared_comments"] if r[:2] != ["pg_authid", "10"]]


def assert_only_epoch_bump(before: dict[str, Any], after: dict[str, Any], nonce: str) -> None:
    """The ONLY permitted change: the owned reservation slot's epoch field; every other
    shared comment byte-identical (Codex 20261112T194049Z)."""
    changed = set(s.diff(before, after))
    assert changed <= {"shared_comments"}, changed
    assert _comments_without_slot(before) == _comments_without_slot(after)
    slots = [[r[2] for r in snap["shared_comments"] if r[:2] == ["pg_authid", "10"]]
             for snap in (before, after)]
    assert [len(x) for x in slots] == [1, 1]
    b, a = slots[0][0], slots[1][0]
    assert f":{nonce}:" in b and b.rsplit(":", 1)[0] == a.rsplit(":", 1)[0], "slot identity"


def assert_refused_no_change(c: s.PgCluster, st_dir: Path) -> None:
    st = s.read_state(st_dir)
    before = s.snapshot(c)
    e = refused(init, c, st_dir)
    assert e.code == "STATE_DIVERGED"
    assert_only_epoch_bump(before, s.snapshot(c), st["nonce"])
    assert s.read_state(st_dir) == st, "state must not advance or change"


# Tampers of COMPLETED owned state. {schema}, {login}, {staff} are filled from the state.
COMPLETED_TAMPER: dict[str, tuple[str, list[str]]] = {
    "tenant_row_deleted": (s.TARGET_DB, ["DELETE FROM shared.tenants"]),
    "tenant_schema_dropped": (s.TARGET_DB, ["DROP SCHEMA {schema} CASCADE"]),
    "login_password": ("postgres", ["ALTER ROLE {login} PASSWORD 'changed-by-someone'"]),
    "login_attribute": ("postgres", ["ALTER ROLE {login} CONNECTION LIMIT 3"]),
    "settings_value_same_version": (s.TARGET_DB, [
        "UPDATE {schema}.m05_reminder_settings SET clinic_display_name = 'Other Clinic'"]),
    "staff_display_name": (s.TARGET_DB, [
        "UPDATE {schema}.m01_staff SET display_name = 'Someone Else'"]),
    "bootstrap_log_deleted": (s.TARGET_DB, ["DELETE FROM {schema}.m01_staff_bootstrap_log"]),
    "profile_value_same_count": (s.TARGET_DB, [
        "UPDATE {schema}.m05_intake_map SET item_kind = 'unclear_reply'"
        " WHERE discriminator_value = 'cancel_request'"]),
    "ledger_state": (s.TARGET_DB, [
        "UPDATE shared.schema_migrations SET state = 'failed'"
        " WHERE migration_id = 't057_m05_reminder_settings'"]),
}


def _fill(statements: list[str], st: dict[str, Any]) -> list[str]:
    return [q.format(schema=st["schema_key"], login=st["logins"]["audit_projector"]["role"],
                     staff=st["staff_id"]) for q in statements]


@pytest.mark.parametrize("kind", sorted(COMPLETED_TAMPER))
def test_b2_r1_completed_tamper_at_ready_refused(pg: s.PgCluster, tmp_path: Path,
                                                 kind: str) -> None:
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    db, statements = COMPLETED_TAMPER[kind]
    tamper(pg, _fill(statements, s.read_state(st_dir)), db)
    assert_refused_no_change(pg, st_dir)


# Recorded phase `settings` (crash before T1 commits); every tamper hits an EARLIER phase.
INTERMEDIATE = ["tenant_row_deleted", "login_password", "login_attribute",
                "profile_value_same_count", "ledger_state", "settings_value_same_version",
                "activation_reason"]


@pytest.mark.parametrize("kind", INTERMEDIATE)
def test_b2_r1_completed_tamper_at_intermediate_phase_refused(pg: s.PgCluster, tmp_path: Path,
                                                              kind: str) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.staff.t1.before_commit")
    first.crashed()
    st = s.read_state(st_dir)
    assert st["phase"] == "settings"
    if kind == "activation_reason":
        db, statements = s.TARGET_DB, [
            "UPDATE {schema}.m05_activation_records SET reason = 'other reason'"]
    else:
        db, statements = COMPLETED_TAMPER[kind]
    tamper(pg, _fill(statements, st), db)
    assert_refused_no_change(pg, st_dir)


def test_b2_r1_status_sealed_and_read_only(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    env = s.environ_for(pg, st_dir)
    status = s.mod("provision").status
    before = s.snapshot(pg)
    ok = status(env)
    assert (ok["sealed"], ok["attested"], ok["pending_effect"]) == (True, True, None)
    assert s.diff(before, s.snapshot(pg)) == {}, "status must be strictly read-only"
    tamper(pg, _fill(COMPLETED_TAMPER["settings_value_same_version"][1], s.read_state(st_dir)))
    before = s.snapshot(pg)
    bad = status(env)
    assert (bad["sealed"], bad["attested"]) == (False, False)
    assert s.diff(before, s.snapshot(pg)) == {}, "status must be strictly read-only"


def test_b2_r1_status_pending_effect(pg: s.PgCluster, tmp_path: Path) -> None:
    """Committed-but-unrecorded phase: status names it (read-only), never attests it."""
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash="phase.settings.after_commit")
    first.crashed()
    before = s.snapshot(pg)
    out = s.mod("provision").status(s.environ_for(pg, st_dir))
    assert (out["phase"], out["sealed"], out["pending_effect"], out["attested"]) == (
        "activated", False, "settings", False)
    assert s.diff(before, s.snapshot(pg)) == {}


# Changes the configuration of one (deterministically chosen) function of the tenant schema.
TENANT_FUNCTION_CONFIG = (
    "DO $t$ BEGIN EXECUTE pg_catalog.format('ALTER FUNCTION %s SET work_mem = %L',"
    " (SELECT p.oid::regprocedure FROM pg_catalog.pg_proc p"
    " WHERE p.pronamespace = '{schema}'::regnamespace ORDER BY p.proname, p.oid LIMIT 1),"
    " '8MB'); END $t$")


# Tampers of the PENDING phase's committed effect (crash after its commit, before the state
# write): same count, changed value / audit / key / ledger.
PENDING_TAMPER: dict[str, tuple[str, list[str]]] = {
    "profile_intake_value": ("profile", [
        "UPDATE {schema}.m05_intake_map SET item_kind = 'unclear_reply'"
        " WHERE discriminator_value = 'cancel_request'"]),
    "profile_event_type_extra_flag": ("profile", [
        "UPDATE {schema}.event_types SET is_active = true WHERE is_active = false"]),
    "tenant_ledger_checksum": ("tenant", [
        "UPDATE shared.schema_migrations SET checksum = repeat('0', 64)"
        " WHERE migration_id = 't057_m05_reminder_settings'"]),
    "tenant_ledger_state": ("tenant", [
        "UPDATE shared.schema_migrations SET state = 'failed'"
        " WHERE migration_id = 't057_m05_reminder_settings'"]),
    "activated_actor": ("activated", [
        "UPDATE {schema}.m05_activation_records"
        " SET actor_id = '00000000-0000-4000-8000-000000000001'"]),
    "settings_tz": ("settings", [
        "UPDATE {schema}.m05_reminder_settings SET clinic_tz = 'UTC'"]),
    "staff_t1_grant_purpose": ("staff.t1", [
        "UPDATE shared.support_access_grants SET purpose_code = 'other'"]),
    "staff_t2_bootstrap_log_actor": ("staff.t2", [
        "UPDATE {schema}.m01_staff_bootstrap_log SET support_actor_id = 'someone-else'"]),
    "staff_t2_bootstrap_key_outcome": ("staff.t2", [
        "UPDATE {schema}.m01_staff_bootstrap_keys SET outcome = 'ADMIN_EXISTS'"]),
    "staff_t2_bootstrap_key_digest": ("staff.t2", [
        "UPDATE {schema}.m01_staff_bootstrap_keys SET digest = sha256('x'::bytea)"]),
    "staff_t3a_admin_log_operation": ("staff.t3a", [
        "UPDATE {schema}.m01_staff_admin_log SET operation = 'revoke'"]),
    "staff_t3a_action_key_digest": ("staff.t3a", [
        "UPDATE {schema}.m01_staff_action_keys SET digest = sha256('x'::bytea)"]),
    "staff_t3a_access_staff_id": ("staff.t3a", [
        "UPDATE {schema}.m01_staff_followup_access"
        " SET staff_id = '00000000-0000-4000-8000-000000000002'"]),
    "staff_t3b_admin_log_deleted": ("staff.t3b", [
        "DELETE FROM {schema}.m01_staff_admin_log WHERE after::text LIKE '%\"act\"%'"]),
    "staff_t3a_admin_log_duplicate": ("staff.t3a", [
        "INSERT INTO {schema}.m01_staff_admin_log SELECT (pg_catalog.jsonb_populate_record("
        "NULL::{schema}.m01_staff_admin_log, pg_catalog.to_jsonb(x)"
        " || pg_catalog.jsonb_build_object('log_id', pg_catalog.gen_random_uuid(),"
        " 'client_action_id', pg_catalog.gen_random_uuid()))).*"
        " FROM {schema}.m01_staff_admin_log x"]),
    "staff_t2_out_of_order_act": ("staff.t2", [
        "INSERT INTO {schema}.m01_staff_followup_access (staff_id, access, granted_at)"
        " VALUES ('{staff}', 'act', pg_catalog.now())"]),
    # Catalog / seed drift with the ledger and version UNCHANGED (Codex 20261112T193442Z #1)
    "migrated_function_config": ("migrated", [
        "ALTER FUNCTION shared.l1w_admin_unbind(character varying, uuid, uuid)"
        " SET work_mem = '8MB'"]),
    "migrated_function_acl": ("migrated", [
        "GRANT EXECUTE ON FUNCTION shared.l1w_admin_unbind(character varying, uuid, uuid)"
        " TO PUBLIC"]),
    "tenant_seed_value": ("tenant", [
        "UPDATE {schema}.m05_outcome_codes SET display_label = 'Other'"
        " WHERE outcome_code = 'cancelled_in_ehr'"]),
    "tenant_table_acl": ("tenant", [
        "GRANT SELECT ON {schema}.m05_outcome_codes TO PUBLIC"]),
    "tenant_function_config": ("tenant", [TENANT_FUNCTION_CONFIG]),
}


@pytest.mark.parametrize("case", sorted(PENDING_TAMPER))
def test_b2_r2_pending_effect_tamper_refused(pg: s.PgCluster, tmp_path: Path, case: str) -> None:
    point, statements = PENDING_TAMPER[case]
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash=f"phase.{point}.after_commit")
    first.crashed()
    st = s.read_state(st_dir)
    assert st["phase"] == state_phase_before(point)
    tamper(pg, _fill(statements, st))
    assert_refused_no_change(pg, st_dir)


# ---------------------------------------------------------------- C: command entry point (S3)

def cli(args: list[str], env: dict[str, str], cwd: Path) -> Any:
    import subprocess
    import sys

    full = {"PATH": os.environ.get("PATH", ""), **env}
    return subprocess.run([sys.executable, "-m", "haloflow.synthetic", *args], cwd=cwd,
                          env=full, capture_output=True, text=True, timeout=600)


def test_b2_c1_cli_requires_attestation_flag(pg: s.PgCluster, tmp_path: Path) -> None:
    before = s.snapshot(pg)
    r = cli(["init"], s.environ_for(pg, s.state_dir(tmp_path)), tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (2, "", "refused: ATTESTATION_MISSING\n")
    assert s.diff(before, s.snapshot(pg)) == {}


def test_b2_c2_cli_init_and_status_secret_free(tmp_path: Path) -> None:
    with s.cluster(tmp_path, scram=True) as c:
        st_dir = s.state_dir(tmp_path)
        env = s.environ_for(c, st_dir)
        r = cli(["init", "--i-attest-disposable-cluster"], env, tmp_path)
        assert (r.returncode, json.loads(r.stdout), r.stderr) == (0, {"phase": "ready"}, "")
        r2 = cli(["status"], env, tmp_path)
        assert r2.returncode == 0 and r2.stderr == ""
        out = json.loads(r2.stdout)
        assert set(out) == STATUS_KEYS and (out["phase"], out["attested"]) == ("ready", True)
        st = s.read_state(st_dir)
        secrets = [v["password"] for v in st["logins"].values()] + [
            st["note_keys"]["hmac"], st["note_keys"]["enc"], "bootstrap-test-only",
            "SCRAM-SHA-256$"]
        assert not any(x in r.stdout + r2.stdout + r.stderr + r2.stderr for x in secrets)


def test_b2_c3_cli_refusal_prints_code_only(pg: s.PgCluster, tmp_path: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    init(pg, st_dir)
    (st_dir / "state.json").write_text("{")
    for args in (["init", "--i-attest-disposable-cluster"], ["status"]):
        r = cli(args, s.environ_for(pg, st_dir), tmp_path)
        assert (r.returncode, r.stdout, r.stderr) == (2, "", "refused: STATE_CORRUPT\n")


@pytest.mark.parametrize("args", [[], ["reset"], ["init", "--unknown"]],
                         ids=["none", "unknown_command", "unknown_flag"])
def test_b2_c4_cli_usage_errors(pg: s.PgCluster, tmp_path: Path, args: list[str]) -> None:
    before = s.snapshot(pg)
    r = cli(args, s.environ_for(pg, s.state_dir(tmp_path)), tmp_path)
    assert r.returncode == 2 and r.stdout == "" and "Traceback" not in r.stderr
    assert s.diff(before, s.snapshot(pg)) == {}


# Mid-tenant interruption (the tenant phase is many guarded transactions): the process dies
# INSIDE guarded transaction #60, after earlier units committed. Codex 20261112T193442Z:
# exact recovery of the intact partial result, refusal of a tampered one before any change.
MID_TENANT = "guarded_txn#60"


def test_b2_r2_partial_tenant_intact_resumes(pg: s.PgCluster, tmp_path: Path,
                                             ref_root: Path) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash=MID_TENANT)
    first.crashed()
    assert s.read_state(st_dir)["phase"] == "logins"
    interrupted = s.ledger_running(pg)
    assert init(pg, st_dir).phase == "ready"
    assert_single_execution(pg, st_dir, reference_of(ref_root), interrupted=interrupted)


@pytest.mark.parametrize("kind", ["function_config", "seed_value", "ledger_checksum"])
def test_b2_r2_partial_tenant_tamper_refused(pg: s.PgCluster, tmp_path: Path, kind: str) -> None:
    st_dir = s.state_dir(tmp_path)
    first = init_child(pg, st_dir, tmp_path / "bar1", crash=MID_TENANT)
    first.crashed()
    st = s.read_state(st_dir)
    with pg.connect() as d:
        applied = d.execute("SELECT migration_id FROM shared.schema_migrations"
                            " WHERE state = 'applied' ORDER BY migration_id").fetchall()
    assert applied, "the crash point must follow committed units"
    statements = {
        "function_config": [TENANT_FUNCTION_CONFIG],
        "seed_value": ["UPDATE {schema}.ref_event_levels SET ordinal = ordinal + 1"
                       " WHERE code = 'business_outcome'"],
        "ledger_checksum": ["UPDATE shared.schema_migrations SET checksum = repeat('0', 64)"
                            f" WHERE migration_id = '{applied[0][0]}'"],
    }[kind]
    tamper(pg, _fill(statements, st))
    assert_refused_no_change(pg, st_dir)


# ---------------------------------------------------------------- pinned controls (v2 / v2r1)

def test_b2_p1_profile_pin_equals_measurement() -> None:
    """B2-P1: the generated approved installed-state profile equals a fresh measurement (two
    independent installs, identical traces). A unit, migration or view change without a
    re-pin fails here."""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, str(s.HALOFLOW / "scripts" / "b2_measure_profile.py"),
                        "--check"], capture_output=True, text=True, timeout=1500,
                       env={"PATH": os.environ.get("PATH", ""),
                            "HALOFLOW_SYNTHETIC_PG_BIN": str(s.pg_bin())})
    assert (r.returncode, r.stdout.strip().splitlines()[-1:]) == (0, ["profile pin MATCHES"]), \
        r.stdout[-2000:] + r.stderr[-2000:]


SEALED_VALUE_TAMPER = {
    # Excluded from the cross-install profile digest; only the same-install seal sees them.
    "clock": "UPDATE {schema}.m05_activation_records"
             " SET created_at = created_at - interval '1 second'",
    "log_id": "UPDATE {schema}.m01_staff_bootstrap_log SET log_id = pg_catalog.gen_random_uuid()",
    "ledger_attempt": "UPDATE shared.schema_migrations SET attempt = attempt + 1"
                      " WHERE migration_id = 't057_m05_reminder_settings'",
}


@pytest.mark.parametrize("kind", sorted(SEALED_VALUE_TAMPER))
def test_b2_r1_sealed_value_tamper_refused(pg: s.PgCluster, tmp_path: Path, kind: str) -> None:
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    tamper(pg, _fill([SEALED_VALUE_TAMPER[kind]], s.read_state(st_dir)))
    assert_refused_no_change(pg, st_dir)


@pytest.mark.parametrize("kind", ["missing", "point_outside_segment", "profile_id", "shape"])
def test_b2_r1_malformed_or_missing_seal_corrupt(pg: s.PgCluster, tmp_path: Path,
                                                 kind: str) -> None:
    """Never silently adopted, never re-minted from the current database state."""
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    st = s.read_state(st_dir)
    if kind == "missing":
        st["seal"] = None
    elif kind == "point_outside_segment":
        st["seal"]["point"] = 0
    elif kind == "profile_id":
        st["seal"]["profile"] = "0" * 64
    else:
        del st["seal"]["identities"]
    (st_dir / "state.json").write_text(json.dumps(st))
    before = s.snapshot(pg)
    assert refused(init, pg, st_dir).code == "STATE_CORRUPT"
    assert refused(s.mod("provision").status, s.environ_for(pg, st_dir)).code == "STATE_CORRUPT"
    assert s.diff(before, s.snapshot(pg)) == {}


def test_b2_r1_status_binds_recorded_cluster(pg: s.PgCluster, tmp_path: Path) -> None:
    a_dir = s.state_dir(tmp_path, "a")
    init(pg, a_dir)
    with s.cluster(tmp_path / "second") as c2:
        b_dir = s.copy_state(a_dir, tmp_path / "b")
        before = s.snapshot(c2)
        assert refused(s.mod("provision").status, s.environ_for(c2, b_dir)).code == \
            "CLUSTER_MISMATCH"
        assert s.diff(before, s.snapshot(c2)) == {}


def test_b2_r3_capture_excluded_by_a_guarded_writer(pg: s.PgCluster, tmp_path: Path) -> None:
    """The capture's shared K_X waits for a writer holding exclusive K_X (as every guarded
    installer transaction does), so it never reads across an in-flight commit."""
    import threading

    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    k_x = s.mod("guard").K_X
    box: dict[str, Any] = {}
    with pg.connect() as w, w.transaction():
        w.execute("SELECT pg_advisory_xact_lock(%s)", (k_x,))
        t = threading.Thread(target=lambda: box.update(
            out=s.mod("provision").status(s.environ_for(pg, st_dir))))
        t.start()
        s.wait_for(lambda: s.waiting_advisory(pg))           # the capture is waiting
        assert t.is_alive() and "out" not in box
    t.join(120)
    assert box["out"]["sealed"] is True


def _recreate_identical_function(c: s.PgCluster, schema: str,
                                 exclude: frozenset[str] = frozenset(),
                                 among: frozenset[int] | None = None) -> tuple[str, int, int]:
    """Drop and recreate one owned function with an identical definition, owner and ACL;
    returns (signature, old oid, new oid). `exclude`: signatures (schema written <SCHEMA>)
    that must not be chosen; `among`: if given, only these OIDs may be chosen."""
    with c.connect() as d, d.transaction():
        candidates = d.execute(
            "SELECT p.oid, p.oid::regprocedure::text, pg_get_functiondef(p.oid),"
            " pg_get_userbyid(p.proowner), p.proacl::text FROM pg_proc p"
            " WHERE p.pronamespace = %s::regnamespace AND p.prokind = 'f'"
            " AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.refobjid = p.oid"
            " AND x.refclassid = 'pg_proc'::regclass)"
            " ORDER BY p.proname, p.oid", (schema,)).fetchall()
        row = next(r for r in candidates
                   if r[1].replace(schema, "<SCHEMA>", 1) not in exclude
                   and (among is None or int(r[0]) in among))
        old_oid, sig, definition, owner, acl = row
        grants = d.execute("SELECT g.grantee, g.privilege_type FROM aclexplode(%s::aclitem[]) g",
                           (acl,)).fetchall() if acl else []
        d.execute(f"DROP FUNCTION {sig}")
        d.execute(definition)
        d.execute(f"ALTER FUNCTION {sig} OWNER TO {owner}")
        d.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
        for grantee, _priv in grants:
            name = "PUBLIC" if grantee == 0 else d.execute(
                "SELECT quote_ident(rolname) FROM pg_roles WHERE oid = %s", (grantee,)
            ).fetchone()[0]
            if grantee != 0 and name == owner:
                continue
            d.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {name}")
        new = d.execute("SELECT %s::regprocedure::oid, (SELECT proacl::text FROM pg_proc"
                        " WHERE oid = %s::regprocedure)", (sig, sig)).fetchone()
        assert new[1] == acl, (new[1], acl)               # premise: ACL text identical
    return sig, int(old_oid), int(new[0])


def test_b2_r1_identical_function_recreated_refused(pg: s.PgCluster, tmp_path: Path) -> None:
    """Same-name, same-definition recreation is visible only through the sealed raw identity
    (new OID); the catalog/function classes of the oracle snapshot stay equal."""
    st_dir = s.state_dir(tmp_path)
    assert init(pg, st_dir).phase == "ready"
    schema = s.read_state(st_dir)["schema_key"]
    before = s.snapshot(pg, rows=False)
    _sig, old, new = _recreate_identical_function(pg, schema)
    assert old != new, "premise: a new OID"
    after = s.snapshot(pg, rows=False)
    assert after[f"{s.TARGET_DB}:functions"] == before[f"{s.TARGET_DB}:functions"], "premise"
    assert_refused_no_change(pg, st_dir)


def test_b2_r3_takeover_waits_for_seal_persistence(pg: s.PgCluster, tmp_path: Path,
                                                   ref_root: Path) -> None:
    """v2r1 (3): the capture lock (shared K_X) is held through the atomic state write. A
    copied-state takeover started while the original is inside that write waits for it; the
    original is then fenced at its next guarded write; the copy completes exactly."""
    a_dir = s.state_dir(tmp_path, "a")
    point = f"state.after_temp_fsync#{WRITE_INDEX['logins']}"
    a = init_child(pg, a_dir, tmp_path / "bara", pause=point)
    a.reached(point.split("#")[0])
    b_dir = s.copy_state(a_dir, tmp_path / "b")             # still the `migrated` state
    assert s.read_state(b_dir)["phase"] == "migrated"
    with pg.connect("postgres") as a_conn:                   # lose the holder's K_S (as I7)
        holders = [r[0] for r in a_conn.execute(
            "SELECT l.pid FROM pg_locks l JOIN pg_database d ON d.oid = l.database"
            " WHERE l.locktype = 'advisory' AND l.granted AND d.datname = 'postgres'")]
    assert len(holders) == 1 and s.terminate_backend(pg, holders[0])
    b = init_child(pg, b_dir, tmp_path / "barb")
    s.wait_for(lambda: s.waiting_advisory(pg))               # the takeover waits on K_X
    assert b.proc.poll() is None, "no takeover inside the original's capture+persist"
    a.go(point.split("#")[0])
    out_a = a.result()
    # The original finishes its atomic write, releases the capture lock, and fails closed at
    # its next step (its K_S is gone); it never reports success.
    assert (out_a["ok"], out_a.get("code")) == (False, "INSTALL_LOCK_LOST"), out_a
    assert s.read_state(a_dir)["phase"] == "logins"
    assert b.result() == {"ok": True, "phase": "ready"}
    assert_single_execution(pg, b_dir, reference_of(ref_root), interrupted=frozenset())


def test_b2_r2_repeated_same_unit_interruption_resumes(pg: s.PgCluster, tmp_path: Path,
                                                       ref_root: Path) -> None:
    """The SAME unit (t029) interrupted twice: attempt 1 (run 1, txn #61) and attempt 2 (run
    2, txn #5, measured); run 3 completes it with attempt 3. Only that unit differs, by +2."""
    st_dir = s.state_dir(tmp_path)
    init_child(pg, st_dir, tmp_path / "bar1", crash="guarded_txn#61").crashed()
    unit = "t029_m01_staff_admin_v2"
    with pg.connect() as d:
        assert d.execute("SELECT state, attempt FROM shared.schema_migrations"
                         " WHERE migration_id = %s", (unit,)).fetchone() == ("running", 1)
    init_child(pg, st_dir, tmp_path / "bar2", crash="guarded_txn#5").crashed()
    with pg.connect() as d:
        assert d.execute("SELECT state, attempt FROM shared.schema_migrations"
                         " WHERE migration_id = %s", (unit,)).fetchone() == ("running", 2)
    assert init(pg, st_dir).phase == "ready"
    reference = reference_of(ref_root)
    st = s.read_state(st_dir)
    assert s.diff(reference["semantic"], normalised(pg, st)) == {}
    got = s.ledger_attempts(pg)
    assert {k: v for k, v in got.items() if v != reference["attempts"][k]} == {
        unit: reference["attempts"][unit] + 2}


@pytest.mark.parametrize("kind", ["unrelated_applied_attempt", "running_attempt_jump"])
def test_b2_r2_ledger_attempt_not_tied_to_interruption_refused(pg: s.PgCluster, tmp_path: Path,
                                                               kind: str) -> None:
    st_dir = s.state_dir(tmp_path)
    init_child(pg, st_dir, tmp_path / "bar1", crash="guarded_txn#61").crashed()
    statement = {
        "unrelated_applied_attempt": "UPDATE shared.schema_migrations SET attempt = 2"
                                     " WHERE migration_id = 't001_m01_baseline'",
        "running_attempt_jump": "UPDATE shared.schema_migrations SET attempt = 3"
                                " WHERE migration_id = 't029_m01_staff_admin_v2'",
    }[kind]
    with pg.connect() as d:
        before_rows = d.execute("SELECT * FROM shared.schema_migrations ORDER BY 1, 2").fetchall()
    tamper(pg, [statement])
    with pg.connect() as d:
        assert d.execute("SELECT * FROM shared.schema_migrations ORDER BY 1, 2").fetchall() \
            != before_rows
    assert_refused_no_change(pg, st_dir)


# Progress FROM a partial seal (Codex 20261113T011245Z): run 1 dies mid-tenant; run 2 persists
# the validated observation (a seal INSIDE the tenant segment), commits more units and dies
# again (txn #30); run 3 resumes from beyond the seal.
def _partial_seal(c: s.PgCluster, tmp_path: Path) -> tuple[Path, frozenset[str]]:
    st_dir = s.state_dir(tmp_path)
    init_child(c, st_dir, tmp_path / "bar1", crash="guarded_txn#60").crashed()
    first = s.ledger_running(c)
    init_child(c, st_dir, tmp_path / "bar2", crash="guarded_txn#30").crashed()
    st = s.read_state(st_dir)
    profile = s.mod("installed_profile")
    assert st["phase"] == "logins"
    assert profile.COMPLETION["logins"] < st["seal"]["point"] < profile.COMPLETION["tenant"], \
        "premise: the observation was sealed inside the tenant segment"
    sealed_applied = sum(1 for r in st["seal"]["ledger"] if r[1] == "applied")
    with c.connect() as d:
        now_applied = d.execute("SELECT count(*) FROM shared.schema_migrations"
                                " WHERE state = 'applied'").fetchone()[0]
    assert now_applied > sealed_applied, "premise: committed progress AFTER the partial seal"
    return st_dir, first | s.ledger_running(c)


def test_b2_r2_progress_from_partial_seal(pg: s.PgCluster, tmp_path: Path,
                                          ref_root: Path) -> None:
    st_dir, interrupted = _partial_seal(pg, tmp_path)
    assert init(pg, st_dir).phase == "ready"
    assert_single_execution(pg, st_dir, reference_of(ref_root), interrupted=interrupted)


@pytest.mark.parametrize("kind", ["applied_unit_completed_at", "unrelated_function_recreated"])
def test_b2_r2_partial_seal_unrelated_change_refused(pg: s.PgCluster, tmp_path: Path,
                                                     kind: str) -> None:
    """Inside the tenant segment only the EXACT measured transitions are allowed: an in-range
    clock edit of an already-applied unit, or the recreation of an object no crossed unit
    replaces, is refused before any change."""
    st_dir, _ = _partial_seal(pg, tmp_path)
    st = s.read_state(st_dir)
    if kind == "applied_unit_completed_at":
        tamper(pg, ["UPDATE shared.schema_migrations SET completed_at = completed_at"
                    " - interval '1 second' WHERE migration_id = 't001_m01_baseline'"])
    else:
        replaced = frozenset(n.removeprefix("pg_proc:")
                             for names in s.mod("installed_profile").REPLACED.values()
                             for n in names if n.startswith("pg_proc:"))
        sealed = frozenset(int(oid) for cls, oid, _n in st["seal"]["identities"]
                           if cls == "pg_proc")             # an object the seal covers
        _sig, old, new = _recreate_identical_function(pg, st["schema_key"], replaced, sealed)
        assert old != new
    assert_refused_no_change(pg, st_dir)
