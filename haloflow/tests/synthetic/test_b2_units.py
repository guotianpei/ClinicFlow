"""Option B increment B2: unit rows (no database). Governing records: see `b2_support.py`.

Red rule (as B1): until `haloflow.synthetic.{errors,dsn,attest,guard,state,hooks}` exist, every
row that names them fails with ModuleNotFoundError in its own body. Controls (marked CONTROL)
pass before and after.

Rows
- B2-U-C1   closed refusal-code set (exact).
- B2-U-P1   phase order (exact), legal successor only.
- B2-U-D*   admin DSN: allowlisted keys only; rejected keys, multi-host/port; loopback or an
            owned, symlink-free absolute socket dir; `localhost` resolved and pinned; explicit
            port; target prefix; any libpq `PG*` variable refused; normalised conninfo rebuilt
            from the pinned endpoint with `connect_timeout=5`.
- B2-U-A*   A0 pre-connection checks (deployment, operator attestation, state path).
- B2-U-S*   state directory: no-follow, 0700/0600, atomic dir_fd replace, crash between temp fsync
            and replace keeps the old state, flock exclusion, pinned lock inode.
- B2-U-K1   lock keys fixed and distinct; reservation value format.
- B2-U-H1   the fault hook is None by default and no `src` module assigns it.
- B2-U-R1   redaction: a canary password in the DSN never appears in any refusal's str/repr.
- B2-U-F1   CONTROL: no packaging entry point references `haloflow.synthetic`.
- B2-U-L1   CONTROL: the installed alembic equals the lock pin (locked-version equivalence
            premise for row B2-E1; `_upgrade_revs` is private).
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import socket
import stat
from pathlib import Path
from typing import Any

import b2_support as s
import pytest

CANARY = "c4n4ry-Pw-9f3e"
OK_ENV: dict[str, str] = {}


def refused(fn: Any, *a: Any, **kw: Any) -> Any:
    errors = s.mod("errors")
    with pytest.raises(errors.SyntheticRefused) as ei:
        fn(*a, **kw)
    return ei.value


# ---------------------------------------------------------------- C1 / P1 / K1

def test_b2_u_c1_closed_code_set() -> None:
    errors = s.mod("errors")
    assert frozenset({
        "DEPLOYMENT_PRODUCTION", "ATTESTATION_MISSING", "DSN_INVALID", "DSN_KEY_FORBIDDEN",
        "DSN_MULTI_HOST", "DSN_HOST_NOT_LOCAL", "DSN_SOCKET_UNSAFE", "DSN_PORT_MISSING",
        "DSN_DBNAME_PREFIX", "DSN_ENV_OVERRIDE", "STATE_PATH_INVALID", "STATE_MISSING",
        "STATE_SYMLINK", "STATE_PERMISSIONS", "STATE_CORRUPT", "STATE_LOCKED",
        "STATE_LOCK_REPLACED", "SERVER_VERSION_UNSUPPORTED", "CLUSTER_NOT_FRESH",
        "CLUSTER_MISMATCH", "TARGET_MISMATCH", "NOT_RESERVED", "RESERVED_BY_OTHER",
        "EPOCH_SUPERSEDED", "ISOLATION_NOT_READ_COMMITTED", "STATE_DIVERGED", "INSTALL_LOCKED",
        "INSTALL_LOCK_LOST"}) == errors.CODES
    e = errors.SyntheticRefused("CLUSTER_NOT_FRESH", ("role",))
    assert (e.code, e.detail) == ("CLUSTER_NOT_FRESH", ("role",))
    with pytest.raises(ValueError):
        errors.SyntheticRefused("NOT_A_CODE")


def test_b2_u_p1_phase_order() -> None:
    state = s.mod("state")
    assert state.PHASES == s.PHASES
    for i, p in enumerate(s.PHASES[:-1]):
        assert state.next_phase(p) == s.PHASES[i + 1]
    with pytest.raises(ValueError):
        state.next_phase("ready")
    with pytest.raises(ValueError):
        state.next_phase("bogus")


def test_b2_u_k1_lock_keys_and_reservation_format() -> None:
    guard = s.mod("guard")
    assert isinstance(guard.K_X, int) and isinstance(guard.K_S, int)
    assert guard.K_X != guard.K_S
    assert -(2 ** 63) <= guard.K_X < 2 ** 63 and -(2 ** 63) <= guard.K_S < 2 ** 63
    assert guard.RESERVATION_PREFIX == "haloflow-synthetic-reservation:v2:"
    v = guard.reservation_value("ab" * 16, "7707275025673703623", 16384, "e" * 32)
    assert v == ("haloflow-synthetic-reservation:v2:" + "ab" * 16
                 + ":7707275025673703623:16384:" + "e" * 32)
    src = (s.SRC / "haloflow/synthetic/guard.py").read_text()
    assert "nonce" not in re.sub(r"#.*", "", src.split("K_X")[1].split("\n")[0]), \
        "K_X must be a fixed constant, not derived from the nonce"


# ---------------------------------------------------------------- DSN

def parse(dsn: str, environ: dict[str, str] | None = None, *, resolve: Any = None,
          uid: int | None = None) -> Any:
    dsn_mod = s.mod("dsn")
    return dsn_mod.parse_admin_dsn(
        dsn, environ if environ is not None else {},
        resolve=resolve or (lambda host, *a, **k: [(socket.AF_INET, 0, 0, "", ("127.0.0.1", 0))]),
        uid=os.getuid() if uid is None else uid)


GOOD = f"host=127.0.0.1 port=5544 dbname={s.TARGET_DB} user=postgres password={CANARY}"


@pytest.mark.parametrize("extra", ["hostaddr=127.0.0.1", "service=x", "servicefile=/tmp/x",
                                   "passfile=/tmp/x", "options=-csearch_path=x",
                                   "target_session_attrs=any", "application_name=x",
                                   "sslrootcert=/tmp/x", "gssencmode=disable"])
def test_b2_u_d1_forbidden_keys(extra: str) -> None:
    assert refused(parse, f"{GOOD} {extra}").code == "DSN_KEY_FORBIDDEN"


@pytest.mark.parametrize("dsn", [
    f"host=127.0.0.1,127.0.0.1 port=5544 dbname={s.TARGET_DB} user=postgres",
    f"host=127.0.0.1 port=5544,5545 dbname={s.TARGET_DB} user=postgres"])
def test_b2_u_d2_multi_host_or_port(dsn: str) -> None:
    assert refused(parse, dsn).code == "DSN_MULTI_HOST"


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.2", "8.8.8.8", "::ffff:10.0.0.1"])
def test_b2_u_d3_non_loopback_ip(host: str) -> None:
    dsn = f"host={host} port=5544 dbname={s.TARGET_DB} user=postgres"
    assert refused(parse, dsn).code == "DSN_HOST_NOT_LOCAL"


def test_b2_u_d4_hostname_resolving_off_loopback_refused_and_localhost_pinned() -> None:
    mixed = lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),  # noqa: E731
                             (socket.AF_INET, 0, 0, "", ("10.1.1.1", 0))]
    dsn = f"host=localhost port=5544 dbname={s.TARGET_DB} user=postgres"
    assert refused(parse, dsn, resolve=mixed).code == "DSN_HOST_NOT_LOCAL"
    ep = parse(dsn)
    info = psycopg_conninfo(ep.conninfo(s.TARGET_DB))
    assert info["host"] == "127.0.0.1", "the checked endpoint must be pinned, not re-resolved"


def psycopg_conninfo(ci: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    return {k: str(v) for k, v in conninfo_to_dict(ci).items()}


def test_b2_u_d5_socket_dir_rules(tmp_path: Path) -> None:
    good = tmp_path / "sock"
    good.mkdir(mode=0o700)
    ok = parse(f"host={good} port=5544 dbname={s.TARGET_DB} user=postgres")
    assert psycopg_conninfo(ok.conninfo(s.TARGET_DB))["host"] == str(good)
    assert refused(parse, f"host=relative/dir port=5544 dbname={s.TARGET_DB} user=postgres"
                   ).code in ("DSN_SOCKET_UNSAFE", "DSN_HOST_NOT_LOCAL")
    link = tmp_path / "link"
    link.symlink_to(good)
    assert refused(parse, f"host={link} port=5544 dbname={s.TARGET_DB} user=postgres"
                   ).code == "DSN_SOCKET_UNSAFE"
    inner = good / "inner"
    inner.mkdir(mode=0o700)
    via = tmp_path / "via"
    via.symlink_to(good)
    assert refused(parse, f"host={via}/inner port=5544 dbname={s.TARGET_DB} user=postgres"
                   ).code == "DSN_SOCKET_UNSAFE"
    assert refused(parse, f"host={good} port=5544 dbname={s.TARGET_DB} user=postgres",
                   uid=os.getuid() + 1).code == "DSN_SOCKET_UNSAFE"


def test_b2_u_d6_port_and_prefix() -> None:
    assert refused(parse, f"host=127.0.0.1 dbname={s.TARGET_DB} user=postgres"
                   ).code == "DSN_PORT_MISSING"
    assert refused(parse, "host=127.0.0.1 port=5544 dbname=clinic user=postgres"
                   ).code == "DSN_DBNAME_PREFIX"
    assert refused(parse, "this is = not a dsn ===").code == "DSN_INVALID"


@pytest.mark.parametrize("var", ["PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE",
                                 "PGOPTIONS", "PGPASSFILE", "PGPASSWORD", "PGUSER", "PGDATABASE",
                                 "PGSSLMODE", "PGTARGETSESSIONATTRS", "PGAPPNAME"])
def test_b2_u_d7_libpq_environment_refused(var: str) -> None:
    assert refused(parse, GOOD, {var: "x"}).code == "DSN_ENV_OVERRIDE"


def test_b2_u_d8_normalised_conninfo() -> None:
    ep = parse(GOOD + " sslmode=disable")
    info = psycopg_conninfo(ep.conninfo(s.TARGET_DB))
    assert set(info) == {"host", "port", "dbname", "user", "password", "sslmode",
                         "connect_timeout"}
    assert (info["host"], info["port"], info["dbname"], info["connect_timeout"]) == (
        "127.0.0.1", "5544", s.TARGET_DB, "5")
    assert ep.target == s.TARGET_DB
    assert psycopg_conninfo(ep.conninfo("postgres"))["dbname"] == "postgres"


# ---------------------------------------------------------------- A0

def a0(environ: dict[str, str], flag: bool = True) -> Any:
    return s.mod("attest").check_a0(environ, attest_flag=flag)


def base_env(tmp_path: Path) -> dict[str, str]:
    return {"HALOFLOW_SYNTHETIC_ADMIN_DSN": GOOD, "HALOFLOW_SYNTHETIC_STATE": str(tmp_path),
            "HALOFLOW_SYNTHETIC_DISPOSABLE": "1"}


def test_b2_u_a1_positive(tmp_path: Path) -> None:
    assert a0(base_env(tmp_path)) is None


@pytest.mark.parametrize("value", ["production", "Production", " PRODUCTION "])
def test_b2_u_a2_production_deployment_refused(tmp_path: Path, value: str) -> None:
    env = base_env(tmp_path) | {"HALOFLOW_PLATFORM_DEPLOYMENT": value}
    assert refused(a0, env).code == "DEPLOYMENT_PRODUCTION"


def test_b2_u_a3_operator_attestation_required(tmp_path: Path) -> None:
    assert refused(a0, base_env(tmp_path), False).code == "ATTESTATION_MISSING"
    for v in (None, "0", "true", "yes"):
        env = base_env(tmp_path)
        if v is None:
            env.pop("HALOFLOW_SYNTHETIC_DISPOSABLE")
        else:
            env["HALOFLOW_SYNTHETIC_DISPOSABLE"] = v
        assert refused(a0, env).code == "ATTESTATION_MISSING", v


def test_b2_u_a4_state_path_and_dsn_presence(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env.pop("HALOFLOW_SYNTHETIC_STATE")
    assert refused(a0, env).code == "STATE_PATH_INVALID"
    assert refused(a0, base_env(tmp_path) | {"HALOFLOW_SYNTHETIC_STATE": "rel/dir"}
                   ).code == "STATE_PATH_INVALID"
    env = base_env(tmp_path)
    env.pop("HALOFLOW_SYNTHETIC_ADMIN_DSN")
    assert refused(a0, env).code == "DSN_INVALID"


# ---------------------------------------------------------------- state directory

def sdir(tmp_path: Path, mode: int = 0o700) -> Path:
    d = tmp_path / "st"
    d.mkdir()
    os.chmod(d, mode)
    return d


def test_b2_u_s1_directory_checks(tmp_path: Path) -> None:
    state = s.mod("state")
    assert _locked_code(state, tmp_path / "absent") == "STATE_MISSING"
    loose = sdir(tmp_path, 0o755)
    assert _locked_code(state, loose) == "STATE_PERMISSIONS"
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "lnk"
    link.symlink_to(real)
    assert _locked_code(state, link) == "STATE_SYMLINK"


def _locked_code(state: Any, d: Path) -> str:
    try:
        with state.StateDir(d).locked():
            return "OPENED"
    except s.mod("errors").SyntheticRefused as e:
        return str(e.code)


def test_b2_u_s2_write_read_roundtrip_and_modes(tmp_path: Path) -> None:
    state = s.mod("state")
    d = sdir(tmp_path)
    with state.StateDir(d).locked() as st:
        assert st.read() is None
        st.write({"phase": "intent", "nonce": "n"})
        assert st.read() == {"phase": "intent", "nonce": "n"}
    assert stat.S_IMODE((d / "state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((d / "state.lock").stat().st_mode) == 0o600
    assert sorted(p.name for p in d.iterdir()) == ["state.json", "state.lock"], \
        "no temp file may remain after a completed write"


def test_b2_u_s3_state_file_symlink_mode_corrupt(tmp_path: Path) -> None:
    state = s.mod("state")
    d = sdir(tmp_path)
    other = tmp_path / "other.json"
    other.write_text("{}")
    (d / "state.json").symlink_to(other)
    assert _read_code(state, d) == "STATE_SYMLINK"
    (d / "state.json").unlink()
    (d / "state.json").write_text('{"phase": "intent"}')
    os.chmod(d / "state.json", 0o644)
    assert _read_code(state, d) == "STATE_PERMISSIONS"
    os.chmod(d / "state.json", 0o600)
    (d / "state.json").write_text("{not json")
    assert _read_code(state, d) == "STATE_CORRUPT"


def _read_code(state: Any, d: Path) -> str:
    try:
        with state.StateDir(d).locked() as st:
            st.read()
            return "READ"
    except s.mod("errors").SyntheticRefused as e:
        return str(e.code)


def test_b2_u_s4_crash_between_temp_fsync_and_replace_keeps_old_state(tmp_path: Path) -> None:
    state, hooks = s.mod("state"), s.mod("hooks")
    d = sdir(tmp_path)
    with state.StateDir(d).locked() as st:
        st.write({"phase": "intent"})

        class Boom(Exception):
            pass

        def fault(point: str, info: Any) -> None:
            if point == "state.after_temp_fsync":
                raise Boom

        hooks.FAULT = fault
        try:
            with pytest.raises(Boom):
                st.write({"phase": "stamped"})
        finally:
            hooks.FAULT = None
        assert st.read() == {"phase": "intent"}
    with state.StateDir(d).locked() as st:
        assert st.read() == {"phase": "intent"}


def test_b2_u_s5_flock_exclusion_and_lock_inode_pin(tmp_path: Path) -> None:
    state = s.mod("state")
    d = sdir(tmp_path)
    with state.StateDir(d).locked() as st:
        assert _locked_code(state, d) == "STATE_LOCKED"
        replacement = d / "replacement"
        replacement.write_text("")
        os.chmod(replacement, 0o600)
        os.replace(replacement, d / "state.lock")
        with pytest.raises(s.mod("errors").SyntheticRefused) as ei:
            st.write({"phase": "intent"})
        assert ei.value.code == "STATE_LOCK_REPLACED"
        assert not (d / "state.json").exists()


def test_b2_u_s6_lock_file_symlink_refused(tmp_path: Path) -> None:
    state = s.mod("state")
    d = sdir(tmp_path)
    (tmp_path / "elsewhere").write_text("")
    (d / "state.lock").symlink_to(tmp_path / "elsewhere")
    assert _locked_code(state, d) == "STATE_SYMLINK"


# ---------------------------------------------------------------- hooks / redaction / fences

def test_b2_u_h1_fault_hook_test_only() -> None:
    hooks = s.mod("hooks")
    assert hooks.FAULT is None
    offenders = []
    for path in (s.SRC / "haloflow").rglob("*.py"):
        text = path.read_text()
        if path.name == "hooks.py" and path.parent.name == "synthetic":
            continue
        if re.search(r"\bFAULT\s*=(?!=)", text) or re.search(r"setattr\([^)]*['\"]FAULT", text):
            offenders.append(str(path.relative_to(s.SRC)))
    assert offenders == []


def test_b2_u_r1_dsn_canary_never_in_refusals(tmp_path: Path) -> None:
    errors = s.mod("errors")
    bad = [f"{GOOD} hostaddr=1.2.3.4", f"host=10.0.0.1 port=1 dbname={s.TARGET_DB} "
           f"password={CANARY}", f"host=127.0.0.1 dbname={s.TARGET_DB} password={CANARY}",
           f"host=127.0.0.1 port=1 dbname=clinic password={CANARY}",
           f"host=127.0.0.1,127.0.0.2 port=1 dbname={s.TARGET_DB} password={CANARY}"]
    seen = 0
    for dsn in bad:
        try:
            parse(dsn)
        except errors.SyntheticRefused as e:
            seen += 1
            chain = [e, e.__cause__, e.__context__]
            for x in chain:
                if x is not None:
                    assert CANARY not in str(x) and CANARY not in repr(x)
    assert seen == len(bad)
    env = base_env(tmp_path) | {"HALOFLOW_SYNTHETIC_ADMIN_DSN": bad[0]}
    try:
        a0(env)
    except errors.SyntheticRefused as e:
        assert CANARY not in str(e) and CANARY not in repr(e)


def test_b2_u_f1_no_packaging_entry_point_control() -> None:
    """CONTROL: passes before and after."""
    text = (s.HALOFLOW / "pyproject.toml").read_text()
    assert "haloflow.synthetic" not in text
    eps = [ep for ep in importlib.metadata.entry_points() if "haloflow.synthetic" in ep.value]
    assert eps == []


def test_b2_u_l1_locked_alembic_control() -> None:
    """CONTROL: the installed alembic is exactly the lock pin (CI installs from the lock)."""
    lock = (s.HALOFLOW / "locks/linux-x86_64-cp312.txt").read_text()
    pinned = re.search(r"^alembic==([^\s]+)", lock, re.M)
    assert pinned is not None
    assert importlib.metadata.version("alembic") == pinned.group(1)
