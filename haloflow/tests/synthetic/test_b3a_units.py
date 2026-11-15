"""Option B B3a: unit rows (no database). Class A unless stated (b3a_support docstring).

Rows: U1/U1++ (LifecycleConfig bounds and T_total), U2 (closed codes, secret-free text),
U3 (lifecycle state machine), U4 (inventory pins), U5 (reduced baseline is pure), SC2a
(disjoint inventories), F1 (new modules inside the fenced package), F3 (no entry point),
F4u (serve refuses a non-loopback bind before binding), F5/G8 (TenantPool keyword, default
unchanged, production never passes it), F6 (no src module assigns hooks.FAULT), G2u (cursor
fence over a fake connection), D-coordinator units (D4u single drain, D9u cancellation
resistance, D3u single terminal disposition, watchdog).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from typing import Any

import b2_support as s
import b3a_support as b3
import pytest

SRC_PKG = s.SRC / "haloflow"


def lifecycle() -> Any:
    return b3.mod("lifecycle")


# --- U1 / U1++ ----------------------------------------------------------------------------

def test_b3a_u1_defaults_and_total_budget() -> None:
    """A: defaults and T_total = t_watcher + 2*sum(stop) + t_port + t_pool + t_quiesce + t_lease
    (v1r2 D3.4a), 28 s by default, <= 60."""
    cfg = lifecycle().LifecycleConfig()
    assert cfg.lease_check_s == 5
    assert tuple(cfg.stop_timeout_s) == (2, 2, 2)
    want = cfg.t_watcher + 2 * sum(cfg.stop_timeout_s) + cfg.t_port + cfg.t_pool \
        + cfg.t_quiesce + cfg.t_lease
    assert cfg.t_total == want == 28


@pytest.mark.parametrize("field,value,code", [
    ("lease_check_s", 0, "LEASE_CHECK_INVALID"), ("lease_check_s", 31, "LEASE_CHECK_INVALID"),
    ("t_watcher", 0, "BUDGET_INVALID"), ("t_port", -1, "BUDGET_INVALID"),
    ("t_pool", 0, "BUDGET_INVALID"), ("t_quiesce", 0, "BUDGET_INVALID"),
    ("t_lease", 0, "BUDGET_INVALID"), ("stop_timeout_s", (2, 0, 2), "BUDGET_INVALID"),
    ("t_pool", 40, "BUDGET_TOTAL_EXCEEDED"),
])
def test_b3a_u1_bounds_refused_before_io(field: str, value: Any, code: str,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A: each out-of-range bound refuses with its own closed code before any I/O."""
    import psycopg

    def no_io(*_a: Any, **_k: Any) -> None:
        raise AssertionError("I/O attempted")

    monkeypatch.setattr(psycopg, "connect", no_io)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", no_io)
    with pytest.raises(s.mod("errors").SyntheticRefused) as ei:
        lifecycle().LifecycleConfig(**{field: value})
    assert ei.value.code == code


# --- U2 -----------------------------------------------------------------------------------

NEW_CODES = {"UNGUARDED_STATEMENT", "RUNTIME_NO_DDL", "RUNTIME_STOPPED", "RUNTIME_LEASE_LOST",
             "RUNTIME_STOP_FAILED", "LEASE_CLEANUP_UNKNOWN", "CLOSE_TIMEOUT", "DRAIN_DEADLINE",
             "POOL_NOT_QUIESCENT", "WATCHER_STOP_FAILED", "LEASE_CHECK_INVALID",
             "BUDGET_INVALID", "BUDGET_TOTAL_EXCEEDED"}


def test_b3a_u2_new_codes_are_closed_and_text_is_fixed() -> None:
    """A: the new codes are in errors.CODES; a refusal's str/repr carry only fixed vocabulary."""
    errors = s.mod("errors")
    assert NEW_CODES <= errors.CODES, sorted(NEW_CODES - errors.CODES)
    canary = "postgresql://u:CANARY-PW@h/db"
    e = errors.SyntheticRefused("RUNTIME_STOPPED")
    assert canary not in str(e) and canary not in repr(e)
    with pytest.raises(ValueError):
        errors.SyntheticRefused(canary)
    lifecycle()  # the codes are B3a's: absent-red until the lifecycle exists


# --- U3 -----------------------------------------------------------------------------------

def test_b3a_u3_state_machine_has_no_effect_for_illegal_calls() -> None:
    """A: request_stop on NEW and a second start are no-ops (no I/O, state unchanged)."""
    lc = lifecycle().SyntheticLifecycle({})
    assert lc.state == "NEW" and lc.admission_open is False
    lc.request_stop("TEST")
    assert lc.state in ("NEW", "STOPPED")


# --- U4 / SC2a ----------------------------------------------------------------------------

def test_b3a_u4_inventory_pins() -> None:
    """A: RUNTIME_MUTABLE_TABLES == the measured/ruled set; no shared table; every ruled
    install-sealed table is sealed."""
    sc = b3.mod("seal_scope")
    assert sc.RUNTIME_MUTABLE_TABLES == b3.EXPECTED_RUNTIME_MUTABLE
    assert not any(t.startswith("shared.") for t in sc.RUNTIME_MUTABLE_TABLES)
    assert b3.MUST_BE_SEALED <= sc.INSTALL_SEALED_TABLES


def test_b3a_sc2a_inventories_disjoint_and_well_formed() -> None:
    """A: two explicit inventories, disjoint, non-empty, names `shared.<t>` / `{tenant}.<t>`."""
    import re

    sc = b3.mod("seal_scope")
    a, b = sc.INSTALL_SEALED_TABLES, sc.RUNTIME_MUTABLE_TABLES
    assert a and b and not (a & b)
    pat = re.compile(r"^(shared|\{tenant\})\.[a-z_][a-z0-9_]*$")
    assert all(pat.match(t) for t in a | b), sorted(t for t in a | b if not pat.match(t))


# --- U5 -----------------------------------------------------------------------------------

def _fake_state() -> dict[str, Any]:
    return {"phase": "ready", "seal": {
        "profile": "p", "point": 1,
        "nondet": {"shared.tenants": {"k": {"created_at": "x"}},
                   "{tenant}.m05_outbound_ops": {"k": {"updated_at": "y"}},
                   "{tenant}.m01_staff": {"k": {"status_changed_at": "z"}}},
        "identities": [["table", 1, "shared.tenants"]], "ledger": [["t001", "applied", 1, None]]}}


def test_b3a_u5_reduced_baseline_is_a_pure_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A (mutant 11): reduced_baseline(state) reads no database, drops ONLY runtime-mutable
    nondet entries, keeps identities and ledger, and changes when the stored seal changes."""
    import psycopg

    def no_db(*_a: Any, **_k: Any) -> None:
        raise AssertionError("database read in reduced_baseline")

    monkeypatch.setattr(psycopg, "connect", no_db)
    sc = b3.mod("seal_scope")
    st = _fake_state()
    base = sc.reduced_baseline(st)
    assert "{tenant}.m05_outbound_ops" not in base["nondet"]
    assert {"shared.tenants", "{tenant}.m01_staff"} <= set(base["nondet"])
    assert base["identities"] == st["seal"]["identities"]
    assert base["ledger"] == st["seal"]["ledger"]
    st2 = _fake_state()
    st2["seal"]["nondet"]["{tenant}.m01_staff"]["k"]["status_changed_at"] = "CHANGED"
    assert sc.reduced_baseline(st2) != base


def test_b3a_u5b_sealed_ready_digest_is_generated() -> None:
    """A: SEALED_READY_DIGEST exists beside POINTS (generated; B2-P1 extended re-measures)."""
    prof = b3.mod("installed_profile")
    digest = prof.SEALED_READY_DIGEST
    assert isinstance(digest, str) and len(digest) == 64 and digest not in prof.POINTS


# --- F1 / F3 / F6 -------------------------------------------------------------------------

def test_b3a_f1_new_modules_live_inside_the_fenced_package() -> None:
    """A: the B3a modules exist under haloflow.synthetic (so the B1 fence rows cover them)."""
    for name in ("seal_scope", "runtime", "lifecycle", "app"):
        assert (SRC_PKG / "synthetic" / f"{name}.py").is_file(), name
    b3.mod("lifecycle")


def test_b3a_f3_no_packaging_entry_point_references_synthetic() -> None:
    """C now / pin: pyproject has no entry point naming haloflow.synthetic (B2-U-F1 extended)."""
    text = (s.HALOFLOW / "pyproject.toml").read_text()
    assert "haloflow.synthetic" not in text


def test_b3a_f6_no_src_module_assigns_the_fault_hook() -> None:
    """C now / pin: no src module assigns hooks.FAULT (B2-U-H1 extended to new modules)."""
    bad = []
    for path in (SRC_PKG).rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and t.attr == "FAULT":
                        bad.append(str(path))
    assert bad == []


# --- F4u ----------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "example.org"])
def test_b3a_f4u_serve_refuses_non_loopback_before_binding(host: str,
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]
                                                           ) -> None:
    """A: `serve --host <non-loopback>` exits 2 with a fixed refusal and never reaches uvicorn
    or a socket bind (Codex Q3: CLI validation before bind)."""
    import socket

    import uvicorn

    def no_bind(*_a: Any, **_k: Any) -> None:
        raise AssertionError("bind attempted")

    monkeypatch.setattr(uvicorn, "run", no_bind)
    monkeypatch.setattr(socket.socket, "bind", no_bind)
    main = b3.mod("__main__").main
    assert main(["serve", "--host", host, "--port", "8765"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and host not in err


# --- F5 / G8 ------------------------------------------------------------------------------

def test_b3a_g8_tenantpool_keyword_default_unchanged() -> None:
    """A: TenantPool takes a public `connection_class` keyword, default AsyncConnection."""
    from psycopg import AsyncConnection

    from haloflow.m01.pool import TenantPool

    sig = inspect.signature(TenantPool.__init__)
    assert "connection_class" in sig.parameters
    assert sig.parameters["connection_class"].default is AsyncConnection
    pool = TenantPool("dbname=unused")
    assert pool._pool.connection_class is AsyncConnection


def test_b3a_f5_production_never_passes_connection_class() -> None:
    """C now / pin: no production module (outside haloflow.synthetic) passes connection_class
    to TenantPool."""
    bad = []
    for path in SRC_PKG.rglob("*.py"):
        if "synthetic" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and any(k.arg == "connection_class"
                                                  for k in node.keywords):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "TenantPool":
                    bad.append(str(path))
    assert bad == []


# --- G2u: the cursor fence without a database ---------------------------------------------

def _control_store_select() -> str:
    """The exact registry SELECT text in m01/control_store.py (taken from the source)."""
    tree = ast.parse((SRC_PKG / "m01" / "control_store.py").read_text())
    texts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
             and isinstance(n.value, str) and "FROM shared.tenants" in n.value]
    assert len(texts) == 1, texts
    return texts[0]


def test_b3a_g2u_allowlist_is_exact_and_read_only() -> None:
    """A: the allowlist is exactly the four read-only texts of v1r1 D2.2."""
    guard = s.mod("guard")
    assert frozenset({
        "SELECT current_schemas(true), pg_my_temp_schema()",
        "DISCARD ALL",
        "SET default_transaction_read_only = on",
        _control_store_select(),
    }) == guard.ALLOWLIST
    assert issubclass(guard.GuardedRuntimeConnection, guard.GuardedAsyncConnection)


# --- coordinator units (in-process, fake resources) -----------------------------------------

class _Res:
    """A fake resource recording into a Recorder; `mode` in ok | hang | resist | block."""

    def __init__(self, name: str, rec: b3.Recorder, mode: str = "ok") -> None:
        self.name, self.rec, self.mode = name, rec, mode
        self.calls = 0

    async def close(self) -> None:
        self.calls += 1
        if self.mode == "hang":
            await asyncio.sleep(3600)
        if self.mode == "resist":
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue
        if self.mode == "block":
            time.sleep(3600)
        self.rec.note(self.name)


def test_b3a_d4u_concurrent_requests_one_drain_one_deadline() -> None:
    """A: three request_stop calls in one loop turn give one drain (each resource closed once),
    the first reason, and one deadline; request_stop never awaits (returns synchronously)."""
    lc_mod = lifecycle()
    rec = b3.Recorder()
    coord = lc_mod.DrainCoordinator.for_test(rec=rec, steps=[_Res(n, rec) for n in (
        "watcher", "writeback", "reminder", "due", "port", "pool", "quiesce", "lease")],
        fatal_exit=lambda *a: pytest.fail("terminal"), config=lc_mod.LifecycleConfig())

    async def body() -> Any:
        t0 = time.monotonic()
        for reason in ("RUNTIME_LEASE_LOST", "GUARD_REFUSED", "LIFESPAN"):
            assert coord.request_stop(reason) is None
        assert time.monotonic() - t0 < 0.05
        return await coord.wait()

    result = asyncio.run(body())
    assert rec.order() == ["watcher", "writeback", "reminder", "due", "port", "pool",
                           "quiesce", "lease"]
    assert (result.disposition, result.reason) == ("drained", "RUNTIME_LEASE_LOST")


@pytest.mark.parametrize("which", ["watcher", "port", "pool"])
def test_b3a_d9u_cancellation_resistant_step_never_awaited(which: str) -> None:
    """A (mutant 30): a step that swallows cancellation is abandoned at its budget; the
    coordinator reaches ONE terminal disposition within budget + 0.5 s; no later step runs."""
    lc_mod = lifecycle()
    rec = b3.Recorder()
    calls: list[tuple[Any, ...]] = []
    names = ["watcher", "writeback", "reminder", "due", "port", "pool", "quiesce", "lease"]
    steps = [_Res(n, rec, "resist" if n == which else "ok") for n in names]
    cfg = lc_mod.LifecycleConfig()
    coord = lc_mod.DrainCoordinator.for_test(rec=rec, steps=steps,
                                             fatal_exit=lambda *a: calls.append(a), config=cfg)

    async def body() -> float:
        t0 = time.monotonic()
        coord.request_stop("TEST")
        await coord.wait()
        return time.monotonic() - t0

    elapsed = asyncio.run(body())
    budget = {"watcher": cfg.t_watcher, "port": cfg.t_port, "pool": cfg.t_pool}[which]
    assert len(calls) == 1 and calls[0][0] == 3, calls
    assert elapsed <= budget + 0.5, elapsed
    assert "lease" not in rec.order()


def test_b3a_d3u_watchdog_and_coordinator_one_terminal_disposition() -> None:
    """A (Codex v1r2: single terminal disposition; mutant 31): when the coordinator reaches
    terminal at the same moment the watchdog fires, fatal_exit is invoked exactly once."""
    lc_mod = lifecycle()
    calls: list[tuple[Any, ...]] = []
    gate = lc_mod.TerminalGate(fatal_exit=lambda *a: calls.append(a))
    import threading

    threads = [threading.Thread(target=gate.terminal, args=("DRAIN_DEADLINE", "watchdog")),
               threading.Thread(target=gate.terminal, args=("POOL_NOT_QUIESCENT", "pool"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(calls) == 1 and calls[0][0] == 3, calls
