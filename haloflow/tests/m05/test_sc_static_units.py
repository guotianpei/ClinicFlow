"""L-1S package S-C — static and handle-unit rows (no database).

Governing: S-C requirements v1, architecture v1 A1-A8, Codex 201638Z and 202256Z pins 1-5,
red package note (Work Session 2026-11-06). Red until the S-C interface exists; SC-S01 is
a control.

- SC-S01  CONTROL: production supported versions {36}; no t046 in the production registry.
- SC-S02  protected-set provenance and completeness: the production catalogue's
          `activation_protected` equals `M05_PROTECTED`; every statement (production + S-A +
          S-B catalogues) whose query calls an `m05_*` function is protected; every protected
          key exists; an unknown protected key is refused at composition
          (PROTECTED_KEY_UNKNOWN); no capability or key prefix is consulted (a protected
          set that omits one m05 key composes, and the oracle — not the runtime — catches it).
- SC-S03  trusted policy: `build_production_tenant_runtime` has a REQUIRED keyword
          `activation_policy` (no default); `start_production_services` passes ENFORCE for
          PRODUCTION and NOT_ENFORCED for NON_PRODUCTION (source-level); M05-A1 still refuses
          a production generation with the M05 linkage active.
- SC-S04  t046 declaration: owner-run SECURITY DEFINER, STABLE, jsonb, no inputs, EXECUTE to
          the runtime only; body reads only m05_activation_records with the shared
          LATEST_ORDER; t042/t043 remain owner-only.
- SC-U01  ENFORCE: no protected statement executes before the probe; unprotected statements
          before the first protected one run without a probe; the probe runs exactly once per
          handle (cached on success), with the exact PROBE_SQL.
- SC-U02  inactive -> ActivationRefused(M05_NOT_ACTIVATED); the protected statement is never
          sent; the handle is poisoned: every later statement (protected or not) refuses with
          the same code, and `activation_refused` reports it.
- SC-U03  unreadable envelopes -> M05_ACTIVATION_UNREADABLE (never active): zero rows, two
          rows, NULL, non-object, extra key, missing key, schema_version 2, active not bool,
          database error from the probe.
- SC-U04  isolation not 'read committed' -> ACTIVATION_ISOLATION_UNSUPPORTED.
- SC-U05  CancelledError raised by the probe propagates unchanged (not UNREADABLE); the
          handle is not marked authorized.
- SC-U06  concurrent first protected calls on one handle: exactly one probe; no protected
          statement is sent before it completes; on inactive both refuse; on active both run.
- SC-U07  NOT_ENFORCED: never probes (target-36 tenants keep current behaviour).
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import sa_support as sa
import sc_support as sc

SRC = Path(__file__).resolve().parents[2] / "src" / "haloflow"
M05_CALL = re.compile(r"\bm05_\w+\(")


# --- SC-S01 / SC-S02 --------------------------------------------------------------------


def test_sc_s01_production_unchanged() -> None:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_tenant_migrations,
    )

    assert frozenset({sa.PRODUCTION_TARGET}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    assert sc.SC_UNIT_ID not in build_production_tenant_migrations().migration_ids


def test_sc_s02_protected_provenance_and_completeness() -> None:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS, build_production_catalog
    from haloflow.m01.errors import RepositoryStatementRejected
    from haloflow.m01.statements import build_statement_catalog
    from haloflow.m05.statements import M05_PROTECTED

    production = build_production_catalog()
    assert production.activation_protected == M05_PROTECTED
    composed = sc.sc_catalog()
    calls_m05 = {s.key for s in composed.catalog.statements() if M05_CALL.search(s.query)}
    assert calls_m05 == set(composed.activation_protected), (
        sorted(calls_m05 ^ set(composed.activation_protected)))
    with pytest.raises(RepositoryStatementRejected) as caught:
        build_statement_catalog(*APPROVED_MODULE_STATEMENTS,
                                activation_protected=frozenset({"m05.no.such.key"}))
    assert caught.value.reason_code == "PROTECTED_KEY_UNKNOWN"
    partial = build_statement_catalog(
        *APPROVED_MODULE_STATEMENTS,
        activation_protected=frozenset(sorted(M05_PROTECTED)[1:]))
    assert partial.activation_protected == frozenset(sorted(M05_PROTECTED)[1:])


# --- SC-S03 ---------------------------------------------------------------------------------


def test_sc_s03_trusted_policy() -> None:
    from haloflow import composition, startup
    from haloflow.m01.activation import ActivationPolicy

    parameter = inspect.signature(
        composition.build_production_tenant_runtime).parameters["activation_policy"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert {p.name for p in ActivationPolicy} == {"ENFORCE", "NOT_ENFORCED"}
    text = inspect.getsource(startup)
    call = re.search(r"build_production_tenant_runtime\((?P<args>[^)]*)\)", text)
    assert call is not None and "activation_policy" in call["args"]
    assert re.search(r"Deployment\.PRODUCTION\b[^\n]*ActivationPolicy\.ENFORCE"
                     r"|ActivationPolicy\.ENFORCE[^\n]*Deployment\.PRODUCTION", text)


def test_sc_s03_m05_a1_unchanged() -> None:
    from haloflow.m02.registry_validation import (
        M05_EVENT_TYPE,
        M05_FAMILY,
        M05_RESOLVER,
        RegistryRefused,
        validate_loaded_generation,
    )

    source = inspect.getsource(validate_loaded_generation)
    assert '"M05_ACTIVATION_NOT_AUTHORIZED"' in source
    assert M05_FAMILY and M05_EVENT_TYPE and M05_RESOLVER and RegistryRefused


# --- SC-S04 ---------------------------------------------------------------------------------


def test_sc_s04_probe_declaration() -> None:
    units = sc.sc_units()
    assert tuple(units) == (sc.SC_UNIT_ID,)
    unit = units[sc.SC_UNIT_ID]
    assert unit.execution_role == sa.M05_OWNER
    (function,) = unit.policy["functions"]
    assert function["name"] == sc.PROBE_FUNCTION
    assert function["inputs"] == [] and function["outputs"] == []
    assert function["return_type"] == "jsonb"
    assert function["security_definer"] is True and function["volatility"] == "stable"
    assert function["acl"] == [{"grantee": sa.RUNTIME, "privileges": ["EXECUTE"]}]
    profile = sc.sc_profiles()[sc.SC_UNIT_ID]
    assert profile.installed_acl == sa.FUNCTION_ACL_RUNTIME and profile.returns_set is False
    body = unit.template.split("$body$")[1]
    assert sc.LATEST_ORDER in body
    tables = set(re.findall(r"\{schema\}\.(\w+)", body))
    assert tables == {"m05_activation_records"}, tables
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", body.upper())
    for migration_id in sa.ACTIVATION_UNITS:
        assert sa.sa_profiles()[migration_id].installed_acl == sa.FUNCTION_ACL_OWNER_ONLY
    t043 = sa.sa_units()["t043_m05_activation_state"].template.split("$body$")[1]
    assert sc.LATEST_ORDER in t043  # one latest-record rule for both


# --- SC-U*: TenantRepositoryHandle with a fake connection --------------------------------------


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConnection:
    """Records every query; the probe answer is `probe_rows` (or `probe_error` raised);
    `park` parks the probe until `release` is set."""

    def __init__(self, probe_rows: Any = None, *, probe_error: BaseException | None = None,
                 park: bool = False) -> None:
        self.queries: list[str] = []
        self.probe_rows = ([({"schema_version": 1, "active": True}, "read committed")]
                           if probe_rows is None else probe_rows)
        self.probe_error = probe_error
        self.park = park
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, query: Any, params: Any = None) -> _Cursor:
        text = query if isinstance(query, str) else str(query)
        self.queries.append(text)
        if text == sc.PROBE_SQL:
            if self.park:
                self.parked.set()
                await self.release.wait()
            if self.probe_error is not None:
                raise self.probe_error
            return _Cursor(self.probe_rows)
        return _Cursor([(1,)])


def _catalog() -> Any:
    return sc.sc_catalog()


def _keys(catalog: Any) -> tuple[str, str]:
    protected_key = "m05.followup.get"
    unprotected_key = "m02.observation.append"
    assert protected_key in catalog.activation_protected
    assert unprotected_key not in catalog.activation_protected
    return protected_key, unprotected_key


def _handle(conn: FakeConnection, policy: str = "ENFORCE") -> Any:
    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.gateway import TenantRepositoryHandle

    catalog = _catalog()
    caps = frozenset(s.required_capability for s in catalog.catalog.statements())
    return TenantRepositoryHandle(conn, catalog.catalog, allow_writes=True, capabilities=caps,
                                  activation_policy=ActivationPolicy[policy],
                                  activation_protected=catalog.activation_protected)


def _query(catalog: Any, key: str) -> str:
    return next(s.query for s in catalog.catalog.statements() if s.key == key)


def test_sc_u01_probe_once_before_first_protected() -> None:
    async def body() -> list[str]:
        conn = FakeConnection()
        handle = _handle(conn)
        protected_key, unprotected_key = _keys(_catalog())
        await handle.fetch_all(unprotected_key, {"command": None})
        assert sc.PROBE_SQL not in conn.queries
        await handle.fetch_all(protected_key, {"query": None})
        await handle.fetch_all(protected_key, {"query": None})
        return conn.queries

    queries = asyncio.run(body())
    catalog = _catalog()
    protected_key, unprotected_key = _keys(catalog)
    assert queries == [_query(catalog, unprotected_key), sc.PROBE_SQL,
                       _query(catalog, protected_key), _query(catalog, protected_key)]


def test_sc_u02_inactive_refuses_and_poisons() -> None:
    async def body() -> tuple[list[str], Any, list[str]]:
        from haloflow.m01.errors import ActivationRefused

        conn = FakeConnection([({"schema_version": 1, "active": False}, "read committed")])
        handle = _handle(conn)
        protected_key, unprotected_key = _keys(_catalog())
        codes = []
        for key in (protected_key, protected_key, unprotected_key):
            with pytest.raises(ActivationRefused) as caught:
                await handle.fetch_all(key, {"x": None})
            codes.append(caught.value.reason_code)
        return conn.queries, handle.activation_refused, codes

    queries, refused, codes = asyncio.run(body())
    assert queries == [sc.PROBE_SQL]                     # nothing else was ever sent
    assert refused == "M05_NOT_ACTIVATED"
    assert codes == ["M05_NOT_ACTIVATED"] * 3


_UNREADABLE = {
    "zero-rows": [],
    "two-rows": [({"schema_version": 1, "active": True}, "read committed")] * 2,
    "null": [(None, "read committed")],
    "non-object": [([True], "read committed")],
    "extra-key": [({"schema_version": 1, "active": True, "x": 1}, "read committed")],
    "missing-key": [({"schema_version": 1}, "read committed")],
    "schema-2": [({"schema_version": 2, "active": True}, "read committed")],
    "active-not-bool": [({"schema_version": 1, "active": 1}, "read committed")],
}


@pytest.mark.parametrize("case", sorted([*_UNREADABLE, "db-error"]))
def test_sc_u03_unreadable_is_never_active(case: str) -> None:
    async def body() -> tuple[str, list[str]]:
        import psycopg

        from haloflow.m01.errors import ActivationRefused

        if case == "db-error":
            conn = FakeConnection(probe_error=psycopg.errors.UndefinedFunction(
                "function m05_activation_probe() does not exist"))
        else:
            conn = FakeConnection(_UNREADABLE[case])
        handle = _handle(conn)
        with pytest.raises(ActivationRefused) as caught:
            await handle.fetch_all(_keys(_catalog())[0], {"x": None})
        return caught.value.reason_code, conn.queries

    code, queries = asyncio.run(body())
    assert code == "M05_ACTIVATION_UNREADABLE"
    assert queries == [sc.PROBE_SQL]


@pytest.mark.parametrize("isolation", ("repeatable read", "serializable", None))
def test_sc_u04_isolation_must_be_read_committed(isolation: str | None) -> None:
    async def body() -> str:
        from haloflow.m01.errors import ActivationRefused

        conn = FakeConnection([({"schema_version": 1, "active": True}, isolation)])
        with pytest.raises(ActivationRefused) as caught:
            await _handle(conn).fetch_all(_keys(_catalog())[0], {"x": None})
        return caught.value.reason_code

    assert asyncio.run(body()) == "ACTIVATION_ISOLATION_UNSUPPORTED"


def test_sc_u05_cancellation_propagates() -> None:
    async def body() -> tuple[list[str], Any]:
        conn = FakeConnection(probe_error=asyncio.CancelledError())
        handle = _handle(conn)
        with pytest.raises(asyncio.CancelledError):
            await handle.fetch_all(_keys(_catalog())[0], {"x": None})
        return conn.queries, handle.activation_refused

    queries, refused = asyncio.run(body())
    assert queries == [sc.PROBE_SQL]
    assert refused is None  # not translated into a refusal code


@pytest.mark.parametrize("active", (True, False))
def test_sc_u06_concurrent_first_calls(active: bool) -> None:
    async def body() -> tuple[list[str], list[Any]]:
        conn = FakeConnection([({"schema_version": 1, "active": active}, "read committed")],
                              park=True)
        handle = _handle(conn)
        key = _keys(_catalog())[0]
        first = asyncio.create_task(handle.fetch_all(key, {"a": None}))
        second = asyncio.create_task(handle.fetch_all(key, {"b": None}))
        try:
            await asyncio.wait_for(conn.parked.wait(), 10)
            await asyncio.sleep(0)
            sent_before_release = [q for q in conn.queries if q != sc.PROBE_SQL]
            assert sent_before_release == [], "a protected statement bypassed the probe"
            conn.release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), 10)
        finally:
            conn.release.set()
            for task in (first, second):
                task.cancel()
        return conn.queries, list(results)

    queries, results = asyncio.run(body())
    assert queries.count(sc.PROBE_SQL) == 1
    if active:
        assert all(isinstance(r, list) for r in results), results
        assert len(queries) == 3
    else:
        from haloflow.m01.errors import ActivationRefused

        assert all(isinstance(r, ActivationRefused) for r in results), results
        assert queries == [sc.PROBE_SQL]


def test_sc_u07_not_enforced_never_probes() -> None:
    async def body() -> list[str]:
        conn = FakeConnection([({"schema_version": 1, "active": False}, "read committed")])
        handle = _handle(conn, policy="NOT_ENFORCED")
        await handle.fetch_all(_keys(_catalog())[0], {"x": None})
        return conn.queries

    assert sc.PROBE_SQL not in asyncio.run(body())
