"""Option B B3b: unit rows (no database). Class A unless stated (b3b_support docstring).

Rows: U1 (allowed hosts), U2 (closed codes), U3 (t_http bounds and t_total), U4 (canonical
phone, exhaustive), U5 (production parser pin: signatures and frozen constants), R7u (closed
route inventory), L2u (unknown path / wrong method labelled), H1u (spoofed Host), H7u (no CORS),
D1u/D2u/D2cu/D2wu (the http drain step on the coordinator), D5u (serve: port 0 refused, the
uvicorn graceful-shutdown bound), F1 (new modules inside the fenced package), F2 (exact
production-import allowance, with a negative control), F3 (no production admission import).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
import time
from typing import Any

import b2_support as s
import b3a_support as b3
import b3b_support as bb
import pytest

SRC_PKG = s.SRC / "haloflow"
SYN = SRC_PKG / "synthetic"


def lifecycle() -> Any:
    return b3.mod("lifecycle")


# --- U1 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("bind,want", [
    (("127.0.0.1", 8765), {"127.0.0.1:8765", "localhost:8765"}),
    (("127.0.0.2", 9000), {"127.0.0.2:9000", "localhost:9000"}),
    (("::1", 8765), {"[::1]:8765", "localhost:8765"}),
])
def test_b3b_u1_allowed_hosts_per_loopback_bind(bind: tuple[str, int], want: set[str]) -> None:
    """A (R-B3b-4 step 1): the Host allowlist is built from the bound address only."""
    assert b3.mod("gate").allowed_hosts(bind) == frozenset(want)


def test_b3b_u1b_allowed_hosts_refuses_unknown_port() -> None:
    """A: port 0 (unknowable) and a non-loopback address give no allowlist (refused)."""
    gate = b3.mod("gate")
    for bind in (("127.0.0.1", 0), ("0.0.0.0", 8765), ("10.0.0.1", 8765)):
        with pytest.raises(s.mod("errors").SyntheticRefused) as ei:
            gate.allowed_hosts(bind)
        assert ei.value.code == "BIND_NOT_LOOPBACK"


# --- U2 -----------------------------------------------------------------------------------

B3B_CODES = {"HTTP_NOT_DRAINED", "HOST_REFUSED", "CROSS_SITE_REFUSED", "ORIGIN_REFUSED",
             "CSRF_REFUSED"}


def test_b3b_u2_new_codes_closed() -> None:
    """A: the B3b codes are in errors.CODES (fixed vocabulary)."""
    errors = s.mod("errors")
    assert B3B_CODES <= errors.CODES, sorted(B3B_CODES - errors.CODES)
    b3.mod("gate")


# --- U3 -----------------------------------------------------------------------------------

def test_b3b_u3_t_http_default_and_total() -> None:
    """A (R-B3b-8.4): t_http defaults to 2 and t_total = 28 + t_http = 30."""
    cfg = lifecycle().LifecycleConfig()
    assert cfg.t_http == 2
    want = cfg.t_watcher + 2 * sum(cfg.stop_timeout_s) + cfg.t_http + cfg.t_port \
        + cfg.t_pool + cfg.t_quiesce + cfg.t_lease
    assert cfg.t_total == want == 30


@pytest.mark.parametrize("value", [0, 0.5, 10.5, 11, -1])
def test_b3b_u3b_t_http_bounds(value: float) -> None:
    """A: t_http outside [1, 10] refuses BUDGET_INVALID before any I/O."""
    with pytest.raises(s.mod("errors").SyntheticRefused) as ei:
        lifecycle().LifecycleConfig(t_http=value)
    assert ei.value.code == "BUDGET_INVALID"


# --- U4 -----------------------------------------------------------------------------------

def test_b3b_u4_canonical_phone_exhaustive() -> None:
    """A (v1r1 §2): the simulator's pattern accepts exactly the 100 canonical numbers among
    every `+1555555` + four digits, and refuses the listed near misses."""
    pattern = b3.mod("simulator").PHONE
    accepted = [f"+1555555{n:04d}" for n in range(10000)
                if pattern.fullmatch(f"+1555555{n:04d}")]
    assert accepted == [bb.syn_phone(n) for n in range(100)]
    for bad in ("+15555550200", "+15555550099", "+15550100001", "+12125550123", "5555550123",
                "+1 555 555 0123", "+155555501234", "+1555555010", "+15555550123\n"):
        assert pattern.fullmatch(bad) is None, bad


def test_b3b_u4c_seed_helper_is_canonical() -> None:
    """C: the test seed helper produces exactly the canonical set (v1r1 single source)."""
    nums = [bb.syn_phone(n) for n in range(100)]
    assert len(set(nums)) == 100 and all(bb.CANONICAL.fullmatch(p) for p in nums)
    for bad in (-1, 100, "1"):
        with pytest.raises(ValueError):
            bb.syn_phone(bad)  # type: ignore[arg-type]


# --- U5: production parser pin (Codex v1r1: signatures for callables, frozen constants) ----

PARSER_SIGNATURES = {
    "_list_query": "(request: 'Request') -> 'ListQuery'",
    "_path_uuid": "(value: 'str', field: 'str') -> 'uuid.UUID'",
    "_no_query": "(request: 'Request') -> 'None'",
    "_json_object": "(request: 'Request', declared: 'dict[str, bool]') -> 'dict[str, Any]'",
    "_staff_action": "(item_id: 'uuid.UUID', value: 'dict[str, Any]') -> 'StaffAction'",
    "_resolve_action": "(item_id: 'uuid.UUID', value: 'dict[str, Any]') -> 'ResolveAction'",
}
PARSER_CONSTANTS = {
    "ACT_FIELDS": {"client_action_id": True, "expected_version": True, "type": True,
                   "assignee_id": False},
    "RESOLVE_FIELDS": {"client_action_id": True, "expected_version": True, "type": True,
                       "outcome_code": True, "note": False},
    "LIST_PARAMETERS": ("limit", "states", "assignee", "after_created_at", "after_item_id"),
    "MAX_BODY": 16 * 1024,
    "MAX_QUERY_PARAMETERS": 8,
}


def test_b3b_u5_production_parser_pin() -> None:
    """C now / pin (Q1 = a): the production parsers B3b imports keep these exact signatures
    and these constants their frozen values; a production refactor fails here loudly."""
    routes = __import__("haloflow.platform.routes", fromlist=["_"])
    for name, sig in PARSER_SIGNATURES.items():
        fn = getattr(routes, name)
        assert callable(fn), name
        assert str(inspect.signature(fn)) == sig, (name, str(inspect.signature(fn)))
    for name, value in PARSER_CONSTANTS.items():
        assert getattr(routes, name) == value, name
    assert inspect.iscoroutinefunction(routes._json_object)
    http = __import__("haloflow.platform.http", fromlist=["_"])
    assert str(inspect.signature(http.error_response)) == \
        "(error: 'BaseException') -> 'JSONResponse'"


# --- R7u / L2u / H1u / H7u: the app object without a lifespan ------------------------------

def _app() -> Any:
    lc = lifecycle().SyntheticLifecycle({})          # NEW: opens nothing
    try:
        return b3.mod("app").create_synthetic_app(lifecycle=lc, bind=bb.BIND)
    except TypeError as e:  # B3a's factory has no `bind` keyword
        raise AssertionError(f"create_synthetic_app has no bind keyword: {e}") from None


def _app_b3a() -> Any:
    """The accepted B3a app (no bind keyword) for the behaviour-red rows."""
    lc = lifecycle().SyntheticLifecycle({})
    try:
        return b3.mod("app").create_synthetic_app(lifecycle=lc, bind=bb.BIND)
    except TypeError:
        return b3.mod("app").create_synthetic_app(lifecycle=lc)


def test_b3b_r7u_route_inventory_is_closed() -> None:
    """B (R-B3b-1): the served (method, path) set equals ROUTES exactly; docs are off.
    On B3a: fails `route inventory differs` (only health is served)."""
    from starlette.routing import Route

    app = _app_b3a()
    served = {(m, r.path) for r in app.routes if isinstance(r, Route)
              for m in (r.methods or set()) if m != "HEAD"}
    want = {(m, p) for m, p in bb.ROUTES.values()}
    assert served == want, f"route inventory differs: missing {sorted(want - served)}," \
        f" extra {sorted(served - want)}"
    assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)


@pytest.mark.parametrize("method,target", [("GET", "/nope"), ("DELETE", "/synthetic/health"),
                                           ("PUT", bb.P)])
def test_b3b_l2u_unknown_path_and_method_are_labelled(method: str, target: str) -> None:
    """B (R-B3b-5): 404 and 405 are JSON objects with simulated: true and the label header.
    On B3a: fails `unlabelled refusal` (Starlette's default plain-text/JSON without label)."""
    r = asyncio.run(bb.asgi(_app_b3a(), method, target, headers=bb.get_headers()))
    assert r.status in (404, 405), r.status
    assert r.header(bb.LABEL_HEADER) == ["true"], f"unlabelled refusal: {r.headers}"
    bb.assert_labelled(r)


@pytest.mark.parametrize("host", ["evil.example", "attacker.test:8765", "127.0.0.1:9999",
                                  "localhost", "127.0.0.1"])
def test_b3b_h1u_spoofed_host_refused_before_routing(host: str) -> None:
    """B (R-B3b-4 step 1; DNS rebinding / host spoof): GET /synthetic/health with a Host
    outside the allowlist gets 421 HOST_REFUSED, labelled. On B3a: fails `spoofed Host reached
    the route` (the health handler answers)."""
    r = asyncio.run(bb.asgi(_app_b3a(), "GET", "/synthetic/health",
                            headers=bb.get_headers(host=host)))
    assert r.status == 421, f"spoofed Host reached the route: {r.status} {r.raw[:120]!r}"
    assert r.json.get("code") == "HOST_REFUSED"
    bb.assert_labelled(r)


def test_b3b_h1v_duplicate_or_missing_host_refused() -> None:
    """A: no Host header, or two Host headers, is 421."""
    app = _app()
    for hdrs in ([], [("host", bb.HOST), ("host", bb.HOST)]):
        r = asyncio.run(bb.asgi(app, "GET", "/synthetic/health", headers=hdrs))
        assert r.status == 421 and r.json["code"] == "HOST_REFUSED", hdrs


def test_b3b_h7u_options_405_no_cors_headers() -> None:
    """A (R-B3b-4 step 3): OPTIONS (a CORS preflight shape) on every route path is 405,
    labelled, with no Access-Control-* header; no CORS middleware is installed."""
    app = _app()
    for _m, p in bb.ROUTES.values():
        r = asyncio.run(bb.asgi(app, "OPTIONS", p.replace("{item_id}", bb.new_uuid()),
                                headers=bb.get_headers(origin="http://evil.example") + [
                                    ("access-control-request-method", "POST")]))
        assert r.status == 405, (p, r.status)
        assert not [k for k, _ in r.headers if k.startswith("access-control-")], r.headers
        bb.assert_labelled(r)
    from starlette.middleware.cors import CORSMiddleware
    assert all(m.cls is not CORSMiddleware for m in app.user_middleware)


# --- D1u / D2u / D2cu / D2wu: the http step on the coordinator -----------------------------

class _Res:
    """A fake drain resource that records itself when it closes (B3a unit pattern)."""

    def __init__(self, name: str, rec: b3.Recorder, mode: str = "ok",
                 block_s: float = 0.0) -> None:
        self.name, self.rec, self.mode, self.block_s = name, rec, mode, block_s
        self.calls = 0

    async def close(self) -> None:
        self.calls += 1
        if self.mode == "hold":
            await asyncio.sleep(3600)
        if self.mode == "raise":
            raise RuntimeError("step raised")
        if self.mode == "block":
            time.sleep(self.block_s)        # blocks the event loop (synchronous)
        self.rec.note(self.name)


NAMES = ["watcher", "writeback", "reminder", "due", "http", "port", "pool", "quiesce", "lease"]


def _coord(steps: list[_Res], calls: list[tuple[Any, ...]], **cfg: Any) -> Any:
    lc_mod = lifecycle()
    return lc_mod.DrainCoordinator.for_test(rec=None, steps=steps,
                                            fatal_exit=lambda *a: calls.append(a),
                                            config=lc_mod.LifecycleConfig(**cfg))


def test_b3b_d1u_http_step_runs_after_producers_before_port() -> None:
    """B (R-B3b-8.2): the order is watcher, producers, http, port, pool, quiesce, lease.
    On B3a: fails `http step absent` (for_test ignores the resource)."""
    rec = b3.Recorder()
    calls: list[tuple[Any, ...]] = []
    coord = _coord([_Res(n, rec) for n in NAMES], calls)

    async def body() -> Any:
        coord.request_stop("TEST")
        return await coord.wait()

    result = asyncio.run(body())
    assert "http" in rec.order(), f"http step absent: {rec.order()}"
    assert rec.order() == ["watcher", "writeback", "reminder", "due", "http", "port", "pool",
                           "quiesce", "lease"]
    assert result.disposition == "drained" and calls == []


@pytest.mark.parametrize("mode", ["hold", "raise"])
def test_b3b_d2u_unconfirmed_http_is_terminal_no_close(mode: str) -> None:
    """A (v1r1 §1; mutant 14b): an http step that is still held at t_http, or that raises,
    gives exactly one fatal_exit (3, HTTP_NOT_DRAINED, http); none of port, pool, quiesce,
    lease runs; within t_http + 0.5 s of the step's start."""
    rec = b3.Recorder()
    calls: list[tuple[Any, ...]] = []
    coord = _coord([_Res(n, rec, mode if n == "http" else "ok") for n in NAMES], calls,
                   t_http=1)

    async def body() -> float:
        t0 = time.monotonic()
        coord.request_stop("TEST")
        await coord.wait()
        return time.monotonic() - t0

    elapsed = asyncio.run(body())
    assert calls == [(3, "HTTP_NOT_DRAINED", "http")], calls
    assert rec.order() == ["watcher", "writeback", "reminder", "due"], rec.order()
    assert elapsed <= 1.5, elapsed


def test_b3b_d2cu_absolute_deadline_inside_http_is_coordinator_deadline() -> None:
    """A (Codex v1r1: distinguish the http-step deadline disposition from the watchdog): a
    producer blocks the loop until little of the absolute deadline remains; the http step is
    then bounded by the deadline, not t_http, and the COORDINATOR's terminal is
    (3, DRAIN_DEADLINE, http). The watchdog is cancelled by that terminal: one call only,
    still one after t_total + 1.5 s."""
    rec = b3.Recorder()
    calls: list[tuple[Any, ...]] = []
    small = {"t_watcher": 0.1, "stop_timeout_s": (0.1, 0.1, 0.1), "t_http": 1, "t_port": 0.1,
             "t_pool": 0.1, "t_quiesce": 0.1, "t_lease": 0.1}
    total = 0.1 + 2 * 0.3 + 1 + 0.4
    block = total - 0.6                  # leaves ~0.6 s < t_http for the http step
    steps = [_Res(n, rec, "block", block) if n == "due" else
             _Res(n, rec, "hold" if n == "http" else "ok") for n in NAMES]
    coord = _coord(steps, calls, **small)

    async def body() -> None:
        coord.request_stop("TEST")
        await coord.wait()
        await asyncio.sleep(total + 1.5)          # the watchdog would have fired by now

    asyncio.run(body())
    assert calls == [(3, "DRAIN_DEADLINE", "http")], calls


def test_b3b_d2wu_watchdog_wins_when_the_loop_is_blocked_in_http() -> None:
    """A (Codex v1r1): the http step blocks the event loop past t_total + 1; the WATCHDOG's
    disposition (3, DRAIN_DEADLINE, watchdog) is the only call, even after the loop resumes
    and the coordinator reaches its own terminal."""
    rec = b3.Recorder()
    calls: list[tuple[Any, ...]] = []
    small = {"t_watcher": 0.1, "stop_timeout_s": (0.1, 0.1, 0.1), "t_http": 1, "t_port": 0.1,
             "t_pool": 0.1, "t_quiesce": 0.1, "t_lease": 0.1}
    total = 0.1 + 2 * 0.3 + 1 + 0.4
    steps = [_Res(n, rec, "block", total + 1.8) if n == "http" else _Res(n, rec) for n in NAMES]
    coord = _coord(steps, calls, **small)

    async def body() -> None:
        coord.request_stop("TEST")
        await coord.wait()

    asyncio.run(body())
    assert calls == [(3, "DRAIN_DEADLINE", "watchdog")], calls
    assert threading.active_count() >= 1


# --- D5u: serve configuration (Codex v1r1: pinned separately from socket-free ASGI rows) ----

def test_b3b_d5u_serve_refuses_port_zero(monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """B (R-B3b-4): `serve --port 0` exits 2 `refused: BIND_NOT_LOOPBACK` before uvicorn.
    On B3a: fails `port 0 accepted` (uvicorn reached)."""
    import uvicorn

    reached: list[Any] = []

    class _Server:
        def __init__(self, config: Any) -> None:
            reached.append(config)

        def run(self) -> None:
            return None

    monkeypatch.setattr(uvicorn, "Server", _Server)
    rc = b3.mod("__main__").main(["serve", "--port", "0"])
    assert reached == [], "port 0 accepted (uvicorn reached)"
    assert rc == 2 and capsys.readouterr().err == "refused: BIND_NOT_LOOPBACK\n"


def test_b3b_d5v_serve_sets_graceful_shutdown_bound_and_bind(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """B (R-B3b-8.4; Codex v1r1: the CLI shutdown configuration pinned separately): serve
    builds uvicorn.Config with timeout_graceful_shutdown = LifecycleConfig().t_total, the
    validated host and port, and passes the same bind to the app factory. On B3a: fails on
    the unset graceful-shutdown bound (None)."""
    import uvicorn

    seen: dict[str, Any] = {}

    class _Server:
        def __init__(self, config: Any) -> None:
            seen["config"] = config

        def run(self) -> None:
            return None

    real_factory = b3.mod("app").create_synthetic_app

    def factory(*a: Any, **k: Any) -> Any:
        seen["bind"] = k.get("bind")
        return real_factory(*a, **k)

    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setattr(b3.mod("app"), "create_synthetic_app", factory)
    assert b3.mod("__main__").main(["serve", "--host", "127.0.0.1", "--port", "8799"]) == 0
    cfg = seen["config"]
    assert (cfg.host, cfg.port) == ("127.0.0.1", 8799)
    assert cfg.timeout_graceful_shutdown == lifecycle().LifecycleConfig().t_total == 30
    assert seen["bind"] == ("127.0.0.1", 8799)


# --- F1 / F2 / F3: fences -----------------------------------------------------------------

B3B_MODULES = ("gate", "labels", "staff_routes", "simulator")

# R-B3b-9: the exact production `platform` names the synthetic package may import.
PLATFORM_ALLOWANCE = {
    "haloflow.platform.routes": {"_list_query", "_path_uuid", "_no_query", "_json_object",
                                 "_staff_action", "_resolve_action", "ACT_FIELDS",
                                 "RESOLVE_FIELDS", "_Malformed"},
    "haloflow.platform.http": {"error_response", "malformed", "body", "TENANT_HEADERS",
                               "TENANT_QUERY_KEYS"},
    "haloflow.platform.capabilities": {"SELF_STANDING", "capabilities_for",
                                       "route_capabilities"},
}


def platform_imports(root: Any = SYN) -> dict[str, set[str]]:
    """Every `from haloflow.platform... import x` / `import haloflow.platform...` in the
    synthetic package (module -> names; a bare module import records '*')."""
    found: dict[str, set[str]] = {}
    for py in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "haloflow.platform"):
                found.setdefault(node.module or "", set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("haloflow.platform"):
                        found.setdefault(a.name, set()).add("*")
    return found


def test_b3b_f1_new_modules_live_inside_the_fenced_package() -> None:
    """A: the B3b modules exist under haloflow.synthetic (the B1 static and runtime fences
    walk the package, so they cover them)."""
    for name in B3B_MODULES:
        b3.mod(name)
        assert (SYN / f"{name}.py").is_file(), name


def test_b3b_f2_platform_import_allowance_is_exact() -> None:
    """A (R-B3b-9): the synthetic package imports production `platform` names only from the
    allowance, and the staff routes do import the parsers (the allowance is used, not idle)."""
    b3.mod("staff_routes")
    found = platform_imports()
    for module, names in found.items():
        assert module in PLATFORM_ALLOWANCE, f"platform module not allowed: {module}"
        assert names <= PLATFORM_ALLOWANCE[module], (module, sorted(names -
                                                                    PLATFORM_ALLOWANCE[module]))
    assert "_list_query" in found.get("haloflow.platform.routes", set()), \
        "staff routes do not reuse the production parsers"


def test_b3b_f2n_allowance_check_detects_an_extra_import(tmp_path: Any) -> None:
    """C (negative control): the allowance check flags a module importing a name outside it,
    and a bare `import haloflow.platform.app`."""
    (tmp_path / "x.py").write_text("from haloflow.platform.routes import _admin\n"
                                   "import haloflow.platform.app\n")
    found = platform_imports(tmp_path)
    assert found == {"haloflow.platform.routes": {"_admin"}, "haloflow.platform.app": {"*"}}
    assert not found["haloflow.platform.routes"] <= PLATFORM_ALLOWANCE["haloflow.platform.routes"]
    assert "haloflow.platform.app" not in PLATFORM_ALLOWANCE


def test_b3b_f3_no_production_admission_import() -> None:
    """C now / pin (R-B3b-9): no synthetic module imports `haloflow.platform.app` (production
    admission) in any form, including a string passed to importlib."""
    for py in sorted(SYN.glob("*.py")):
        text = py.read_text()
        assert "haloflow.platform.app" not in text, py.name
        assert "platform.app" not in text, py.name
