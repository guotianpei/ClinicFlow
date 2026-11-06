"""L-1S package S-B (due-window selection) — static and unit rows (no database).

Governing: S-B requirements v1/v1r1, architecture v1, interface pins v1 and the
red-package interface delta (Work Session 2026-11-06). Red until `haloflow.m05.due_units`,
`haloflow.m05.due_statements` and `haloflow.m05.due_windows` exist (SB-S01 is a control).

- SB-S01  CONTROL: production unchanged — supported {36}, no t044/t045 in the production
          registry, no `m05.intake.due_windows` key in the production catalogue,
          `composition.py` names no S-B module.
- SB-S02  S-B unit set exactly (t044 migrator index unit, t045 typed unit run as the M05
          owner); S-B TEST registry target 45 = S-A units then S-B units; `SA_STATEMENTS`
          is still exactly the S-A literal (S-A catalogue untouched); `SB_STATEMENTS` is
          exactly one READ statement.
- SB-S03  t045 declaration: SECURITY DEFINER, VOLATILE, PARALLEL UNSAFE, not strict,
          jsonb -> jsonb scalar (delta D1: M01 InstalledStateProfile admits scalar return
          types only, installed_state.py 119-122 — RETURNS TABLE is not used), pinned
          search_path, EXECUTE to the runtime only; t044 SQL creates exactly the partial
          index on (deadline, reminder_id) WHERE state = 'awaiting'.
- SB-S04  source tie (delta D2): `due_units.DUE_PREDICATE` is the comparison
          `w.deadline OPERATOR(pg_catalog.<=) v_cutoff`, appears exactly once in the t045
          body, `v_cutoff` is assigned exactly once from `pg_catalog.clock_timestamp()`,
          and the body takes no row lock and writes nothing.
- SB-S05  static R-SB-9: S-B modules import no SMS/EHR adapter, no network library, and
          reach no private attribute of another object (no `._` access outside `self`).
- SB-U01  constructor refusals before any I/O: actor principal, missing capability,
          SUPERSET of the capabilities, bad page/cap values (incl. bool, non-int, cap < page).
- SB-U02  run_once refusals before any I/O: tenant not str / empty / not authorized,
          execution id not a UUID; and RUN_IN_PROGRESS: busy is set synchronously before
          the first await — a second run_once on the SAME runner while the first is parked
          returns RUN_IN_PROGRESS with zero resolver/gateway calls.
- SB-U03  busy is cleared in `finally`: after a run that raised (resolver failure) and after
          a cancelled run, the SAME runner runs again (not RUN_IN_PROGRESS).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import re
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import sa_support as sa
import sb_support as sb

SRC = Path(__file__).resolve().parents[2] / "src" / "haloflow"
SB_MODULES = ("m05/due_units.py", "m05/due_statements.py", "m05/due_windows.py")

# The S-A catalogue literal at cdcd335 (SA-S05); S-B must leave it byte-identical.
SA_LITERAL = {
    "m05.intake.directory": ("WRITE", "m05.intake.directory",
                             "SELECT m05_directory_put(%(command)s)"),
    "m05.intake.dispatch": ("WRITE", "m05.intake.dispatch",
                            "SELECT m05_register_dispatch(%(command)s)"),
    "m05.intake.inbound": ("WRITE", "m05.intake.inbound",
                           "SELECT m05_register_inbound(%(command)s)"),
    "m05.intake.close": ("WRITE", "m05.intake.close", "SELECT m05_close_window(%(command)s)"),
}
SB_LITERAL = {
    sb.DUE_KEY: ("READ", sb.DUE_KEY, "SELECT m05_due_windows(%(command)s)"),
}


def _plain(statements: Any) -> dict[str, tuple[str, str, str]]:
    return {k: (mode.name, cap, query) for k, (mode, cap, query) in statements.items()}


# --- SB-S01 (control) ---------------------------------------------------------------------


def test_sb_s01_production_unchanged() -> None:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_catalog,
        build_production_tenant_migrations,
    )

    assert frozenset({sa.PRODUCTION_TARGET}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    registry = build_production_tenant_migrations()
    assert not set(registry.migration_ids) & set(sb.SB_UNIT_IDS)
    keys = build_production_catalog().catalog.keys()
    assert not [k for k in keys if k == sb.DUE_KEY]
    text = (SRC / "composition.py").read_text()
    assert "due_units" not in text and "due_statements" not in text and "due_windows" not in text


# --- SB-S02 ---------------------------------------------------------------------------------


def test_sb_s02_units_registry_catalogues() -> None:
    from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND

    units = sb.sb_units()
    assert tuple(units) == sb.SB_UNIT_IDS
    index_unit = units["t044_m05_reminder_windows_due"]
    assert getattr(index_unit, "execution_role", None) is None  # migrator by absence
    selector = units["t045_m05_due_windows"]
    assert selector.kind == TYPED_FUNCTION_KIND
    assert selector.execution_role == sa.M05_OWNER
    assert set(sb.sb_profiles()) == {"t045_m05_due_windows"}
    registry = sb.sb_registry()
    assert registry.target_version == sb.SB_TARGET
    assert registry.migration_ids[-9:] == (*sa.SA_UNIT_IDS, *sb.SB_UNIT_IDS)
    assert _plain(sa.sa_statements()) == SA_LITERAL
    assert isinstance(sb.sb_statements(), MappingProxyType)
    assert _plain(sb.sb_statements()) == SB_LITERAL


# --- SB-S03 ---------------------------------------------------------------------------------


def test_sb_s03_declarations() -> None:
    units = sb.sb_units()
    (function,) = units["t045_m05_due_windows"].policy["functions"]
    assert function["name"] == sb.SELECTOR
    assert [i["type"] for i in function["inputs"]] == ["jsonb"]
    assert function["outputs"] == []
    assert function["return_type"] == "jsonb"
    assert function["security_definer"] is True
    assert function["volatility"] == "volatile"
    assert function["parallel"] == "unsafe"
    assert function["strict"] is False
    assert function["config"] == ["search_path=pg_catalog, {schema}, pg_temp"]
    assert function["acl"] == [{"grantee": sa.RUNTIME, "privileges": ["EXECUTE"]}]
    profile = sb.sb_profiles()["t045_m05_due_windows"]
    assert profile.return_type == "jsonb" and profile.returns_set is False
    assert profile.installed_acl == sa.FUNCTION_ACL_RUNTIME
    assert profile.owner == sa.M05_OWNER and profile.security_definer is True
    index_sql = units["t044_m05_reminder_windows_due"]
    text = index_sql if isinstance(index_sql, str) else index_sql.sql
    normal = " ".join(text.split())
    assert normal.count("CREATE INDEX") == 1
    assert (f"CREATE INDEX {sb.INDEX_NAME} ON {{schema}}.m05_reminder_windows"
            " (deadline, reminder_id) WHERE state = 'awaiting'") in normal
    assert "GRANT" not in normal and "FUNCTION" not in normal


# --- SB-S04 ---------------------------------------------------------------------------------


def _selector_body() -> str:
    # The policy declaration carries only the body's sha256; the body is the template's
    # $body$ section (as SA-S06 reads it).
    body: str = sb.sb_units()["t045_m05_due_windows"].template.split("$body$")[1]
    return body


def test_sb_s04_predicate_source_tie() -> None:
    from haloflow.m05.due_units import DUE_PREDICATE, DUE_QUERIES

    assert DUE_PREDICATE == "w.deadline OPERATOR(pg_catalog.<=) v_cutoff"
    assert set(DUE_QUERIES) == {"first", "after", "until", "after_until"}
    body = _selector_body()
    for name, query in DUE_QUERIES.items():
        assert body.count(query) == 1, name
        assert query.count(DUE_PREDICATE) == 1, name
        assert "w.state OPERATOR(pg_catalog.=) 'awaiting'" in query, name
        assert "LIMIT v_limit" in query, name
    # every comparison of the deadline with the cutoff in the body is DUE_PREDICATE
    assert len(re.findall(r"w\.deadline\s+OPERATOR\(pg_catalog\.[<>=]+\)\s+v_cutoff", body)) \
        == body.count(DUE_PREDICATE) == len(DUE_QUERIES)
    assert len(re.findall(r"v_cutoff\s*:=", body)) == 1
    assert re.search(r"v_cutoff\s*:=\s*pg_catalog\.clock_timestamp\(\)\s*;", body)
    upper = body.upper()
    for forbidden in ("FOR UPDATE", "FOR SHARE", "FOR NO KEY", "INSERT ", "UPDATE ",
                      "DELETE ", "PG_ADVISORY"):
        assert forbidden not in upper, forbidden
    assert "intake_reply_is_timely" not in body  # the disclosed, guarded exception


# --- SB-S05 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("module", SB_MODULES)
def test_sb_s05_no_side_effect_imports(module: str) -> None:
    tree = ast.parse((SRC / module).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for name in imported:
        assert not name.startswith(("haloflow.integrations", "haloflow.ehr",
                                    "haloflow.modules", "httpx", "requests", "socket",
                                    "urllib", "aiohttp")), (module, name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_") \
                and not node.attr.startswith("__"):
            owner = node.value
            assert isinstance(owner, ast.Name) and owner.id in {"self", "cls"}, (
                module, node.attr)


# --- SB-U01..U03: fakes (no database) --------------------------------------------------------


class _Spy:
    """Records any use; the runner must not touch it in the refused rows."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            raise AssertionError(f"I/O reached: {name}")

        return record


def _principal(kind: str = "workload", caps: frozenset[str] = sb.TIMER_CAPS,
               tenants: frozenset[str] = frozenset({"t-sb"})) -> Any:
    from haloflow.m01.context import Principal, PrincipalKind

    return Principal(kind=PrincipalKind(kind), id=sa.W_TIMER, auth_method="worker",
                     authorized_tenant_ids=tenants, capabilities=caps)


def _runner(principal: Any, *, gateway: Any = None, resolver: Any = None,
            page: Any = 10, cap: Any = 100) -> Any:
    from haloflow.m05.due_windows import DueWindowRunner

    return DueWindowRunner(gateway=gateway or _Spy(), resolver=resolver or _Spy(),
                           intake=_Spy(), principal=principal, page_limit=page,
                           attempt_cap=cap)


@pytest.mark.parametrize(("case", "code"), (
    ("actor", "PRINCIPAL_NOT_WORKLOAD"),
    ("missing", "CAPABILITIES_INVALID"),
    ("superset", "CAPABILITIES_INVALID"),
    ("page0", "LIMIT_INVALID"), ("page501", "LIMIT_INVALID"), ("pagebool", "LIMIT_INVALID"),
    ("pagefloat", "LIMIT_INVALID"), ("capbelowpage", "LIMIT_INVALID"),
    ("cap10001", "LIMIT_INVALID"), ("capbool", "LIMIT_INVALID"),
))
def test_sb_u01_constructor_refusals(case: str, code: str) -> None:
    from haloflow.m05.due_windows import SBRefused

    principal = _principal()
    page: Any = 10
    cap: Any = 100
    if case == "actor":
        principal = _principal(kind="actor")
    elif case == "missing":
        principal = _principal(caps=sb.TIMER_CAPS - {sb.APPEND})
    elif case == "superset":
        principal = _principal(caps=sb.TIMER_CAPS | {"m05.followup.read"})
    elif case == "page0":
        page = 0
    elif case == "page501":
        page, cap = 501, 1000
    elif case == "pagebool":
        page = True
    elif case == "pagefloat":
        page = 10.0
    elif case == "capbelowpage":
        page, cap = 50, 49
    elif case == "cap10001":
        cap = 10_001
    elif case == "capbool":
        page, cap = 1, True
    gateway, resolver = _Spy(), _Spy()
    with pytest.raises(SBRefused) as caught:
        _runner(principal, gateway=gateway, resolver=resolver, page=page, cap=cap)
    assert caught.value.code == code
    assert gateway.calls == [] and resolver.calls == []


EXECUTION = uuid.UUID("00000000-0000-4000-8000-0000000005b1")


@pytest.mark.parametrize(("tenant", "execution", "code"), (
    (1, EXECUTION, "TENANT_INVALID"),
    ("", EXECUTION, "TENANT_INVALID"),
    ("t-other", EXECUTION, "TENANT_INVALID"),
    ("t-sb", str(EXECUTION), "EXECUTION_ID_INVALID"),
), ids=("tenant-int", "tenant-empty", "tenant-unauthorized", "execution-str"))
def test_sb_u02_run_once_refusals(tenant: Any, execution: Any, code: str) -> None:
    from haloflow.m05.due_windows import SBRefused

    gateway, resolver = _Spy(), _Spy()
    runner = _runner(_principal(), gateway=gateway, resolver=resolver)
    with pytest.raises(SBRefused) as caught:
        asyncio.run(runner.run_once(tenant, execution))
    assert caught.value.code == code
    assert gateway.calls == [] and resolver.calls == []


class _ParkingResolver:
    """resolve() parks until released, then raises (or returns `context`)."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.error = error or RuntimeError("resolver unavailable (test)")

    async def resolve(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        raise self.error


BOUND_S = 10.0  # every wait is bounded; a timeout is an assertion, never a hang (SB-RED-1)


async def _bounded(awaitable: Any, what: str, timeout: float = BOUND_S) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError:
        raise AssertionError(f"not reached within {timeout}s: {what}") from None


async def _drain(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        with contextlib.suppress(BaseException):  # cleanup only
            await asyncio.wait_for(task, BOUND_S)


def test_sb_u02_run_in_progress_without_io() -> None:
    async def body() -> None:
        resolver = _ParkingResolver()
        gateway = _Spy()
        runner = _runner(_principal(), gateway=gateway, resolver=resolver)
        first = asyncio.create_task(runner.run_once("t-sb", uuid.uuid4()))
        second_task: asyncio.Task[Any] | None = None
        try:
            await _bounded(resolver.entered.wait(), "first run reaches the resolver")
            second_task = asyncio.create_task(runner.run_once("t-sb", uuid.uuid4()))
            # immediate: the second run must not reach the (parked) resolver
            done, _ = await asyncio.wait({second_task}, timeout=2.0)
            assert resolver.calls == 1, "second run entered the resolver (busy not held)"
            assert second_task in done, "second run did not return immediately"
            second = second_task.result()
            assert second.code == "RUN_IN_PROGRESS"
            assert (second.attempted, second.no_reply, second.opted_out, second.not_awaiting,
                    second.not_due, second.commit_unknown, dict(second.failed),
                    second.wrapped) == (0, 0, 0, 0, 0, 0, {}, False)
            assert gateway.calls == []
            resolver.release.set()
            result = await _bounded(first, "first run completes after release")
            assert result.code == "SELECTION_FAILED"
        finally:
            resolver.release.set()
            await _drain(first, *([second_task] if second_task else []))

    asyncio.run(body())


def test_sb_u03_busy_cleared_after_error_and_cancel() -> None:
    async def body() -> None:
        resolver = _ParkingResolver()
        runner = _runner(_principal(), resolver=resolver)
        tasks: list[asyncio.Task[Any]] = []
        try:
            # 1. an error run (resolver raises -> SELECTION_FAILED)
            resolver.release.set()
            first = await _bounded(runner.run_once("t-sb", uuid.uuid4()), "error run")
            assert first.code == "SELECTION_FAILED"
            # 2. a cancelled run
            resolver.release.clear()
            resolver.entered.clear()
            task = asyncio.create_task(runner.run_once("t-sb", uuid.uuid4()))
            tasks.append(task)
            entered = asyncio.ensure_future(resolver.entered.wait())
            tasks.append(entered)
            await asyncio.wait({task, entered}, timeout=BOUND_S,
                               return_when=asyncio.FIRST_COMPLETED)
            if task.done() and not task.cancelled() and task.exception() is None:
                assert task.result().code != "RUN_IN_PROGRESS", "busy leaked after an error run"
            assert entered.done(), "cancelled run did not reach the resolver"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _bounded(task, "cancelled run ends")
            # 3. the SAME runner runs again: not RUN_IN_PROGRESS
            resolver.release.set()
            again = await _bounded(runner.run_once("t-sb", uuid.uuid4()), "third run")
            assert again.code != "RUN_IN_PROGRESS", "busy leaked after a cancelled run"
            assert again.code == "SELECTION_FAILED"
            assert resolver.calls == 3
        finally:
            resolver.release.set()
            await _drain(*tasks)

    asyncio.run(body())
