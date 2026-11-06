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
          `activation_policy` (no default); BEHAVIOURAL: `start_production_services` (tenant
          verification stubbed, builder captured) passes ENFORCE for PRODUCTION and
          NOT_ENFORCED for NON_PRODUCTION; M05-A1 by direct validator invocation: a production
          generation with the M05 linkage active is refused, non-production accepted.
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
- SC-U08  every protected key x {execute, fetch_one, fetch_all}: inactive -> M05_NOT_ACTIVATED
          with zero business SQL (Codex 211059Z item 1).
- SC-U09  the protected set spans staff routes, t014 create, S-A intake and the S-B selector.
- SC-U10  missing capability / unknown statement / write in a read transaction: refused by
          the existing checks with NO probe and no business I/O.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import sa_support as sa
import sb_support as sb
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


def test_sc_s03_builder_requires_policy() -> None:
    from haloflow import composition
    from haloflow.m01.activation import ActivationPolicy

    parameter = inspect.signature(
        composition.build_production_tenant_runtime).parameters["activation_policy"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert {p.name for p in ActivationPolicy} == {"ENFORCE", "NOT_ENFORCED"}


class _Stop(Exception):
    """Raised by the captured builder: construction is not under test here."""


@pytest.mark.parametrize(("deployment", "policy"), (("PRODUCTION", "ENFORCE"),
                                                    ("NON_PRODUCTION", "NOT_ENFORCED")))
def test_sc_s03_startup_maps_deployment_to_policy(monkeypatch: pytest.MonkeyPatch,
                                                  deployment: str, policy: str) -> None:
    """Behavioural (Codex 211059Z item 2): start_production_services, with its tenant
    verification and static plan stubbed, hands build_production_tenant_runtime the
    policy for the trusted Deployment; the builder is captured and stops the run."""

    from haloflow import composition, startup
    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.runtime import TenantRuntimeDependencies

    captured: list[dict[str, Any]] = []

    def capture(dependencies: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        raise _Stop()

    async def verified(*args: Any, **kwargs: Any) -> tuple[str, str, int]:
        return ("t-sc", "t_sc_schema", 36)

    monkeypatch.setattr(startup, "_static_plan_or_code", lambda: object())
    monkeypatch.setattr(startup, "_verify_served_tenant", verified)
    monkeypatch.setattr(composition, "build_production_tenant_runtime", capture)
    dependencies = TenantRuntimeDependencies(pool=object(), control_store=object(),  # type: ignore[arg-type]
                                             migrator_connect=object(),  # type: ignore[arg-type]
                                             provisioner_connect=object())  # type: ignore[arg-type]
    # startup turns any construction failure into StartupRefused(COMPOSITION_INVALID)
    with pytest.raises(startup.StartupRefused) as refused:
        asyncio.run(startup.start_production_services(
            dependencies, deployment=startup.Deployment[deployment], served_tenants=("t-sc",)))
    assert refused.value.reason_code == "COMPOSITION_INVALID"
    assert captured == [{"activation_policy": ActivationPolicy[policy]}]


@pytest.mark.parametrize("policy", (None, "enforce", "ENFORCE", True, 0))
def test_sc_s03_builder_rejects_non_enum_policy_before_io(policy: Any) -> None:
    """Codex 220503Z: a policy that is not an ActivationPolicy member raises TypeError
    before any dependency is touched (the dependencies are inert sentinels that would
    raise on any attribute access)."""

    from haloflow import composition
    from haloflow.m01.runtime import TenantRuntimeDependencies

    class Inert:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"dependency touched before the policy check: {name}")

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("dependency called before the policy check")

    dependencies = TenantRuntimeDependencies(pool=Inert(), control_store=Inert(),  # type: ignore[arg-type]
                                             migrator_connect=Inert(),  # type: ignore[arg-type]
                                             provisioner_connect=Inert())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        composition.build_production_tenant_runtime(dependencies, activation_policy=policy)


def test_sc_s03_m05_a1_still_refuses_production() -> None:
    """Direct validator invocation (Codex 211059Z item 2): a production generation with
    the M05 linkage fully active is still refused M05_ACTIVATION_NOT_AUTHORIZED."""

    from haloflow.m02 import registry_validation as v

    generation = v.LoadedGeneration(
        contracts=(v.ContractRow(
            event_type=v.M05_EVENT_TYPE, version=1, event_level="observation", is_active=True,
            projection_rule_id=None,
            signal_rule={"handoff_kind": "event_signal", "signal_type": "m02.event.appended"},
            value_constraints={"operation_id": {"must_be_null": True}}),),
        resolvers=(v.ResolverRow(v.M05_RESOLVER, "module_owned", True),),
        test_targets_present=False,
        families=(v.FamilyRow(v.M05_FAMILY, True),),
    )
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(generation, deployment="production")
    assert caught.value.reason_code == "M05_ACTIVATION_NOT_AUTHORIZED"
    assert v.validate_loaded_generation(generation, deployment="non_production") is None


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
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
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


# The expected protected set, as a literal (stable node ids in red and green; SC-U09 pins
# it equal to the composed catalogue's set).
PROTECTED_KEYS = (
    "m05.followup.apply", "m05.followup.create", "m05.followup.get", "m05.followup.history",
    "m05.followup.list", "m05.followup.note.read", "m05.followup.prepare",
    "m05.intake.close", "m05.intake.directory", "m05.intake.dispatch", "m05.intake.inbound",
    "m05.intake.due_windows",
)


@pytest.mark.parametrize("method", ("execute", "fetch_one", "fetch_all"))
@pytest.mark.parametrize("key", PROTECTED_KEYS)
def test_sc_u08_every_protected_key_and_method_refuses(key: str, method: str) -> None:
    """Codex 211059Z item 1: every protected statement (staff routes, t014 create, S-A
    intake, S-B selector), through each supported handle method, refuses when inactive
    with zero business SQL sent."""

    async def body() -> tuple[str, list[str]]:
        from haloflow.m01.errors import ActivationRefused

        conn = FakeConnection([({"schema_version": 1, "active": False}, "read committed")])
        handle = _handle(conn)
        with pytest.raises(ActivationRefused) as caught:
            await getattr(handle, method)(key, {"command": None})
        return caught.value.reason_code, conn.queries

    assert key in _catalog().activation_protected
    code, queries = asyncio.run(body())
    assert code == "M05_NOT_ACTIVATED"
    assert queries == [sc.PROBE_SQL]


def test_sc_u09_protected_set_spans_every_m05_surface() -> None:
    assert _catalog().activation_protected == frozenset(PROTECTED_KEYS)
    assert sb.DUE_KEY in PROTECTED_KEYS


U10_CASES = {
    # case: (exception class name in haloflow.m01.errors, reason_code)
    "missing-capability": ("CapabilityDenied", "STATEMENT_CAPABILITY_DENIED"),
    "unknown-statement": ("RepositoryStatementRejected", "STATEMENT_NOT_REGISTERED"),
    "write-in-read": ("CapabilityDenied", "CAPABILITY_DENIED"),
}


@pytest.mark.parametrize("case", sorted(U10_CASES))
def test_sc_u10_authorization_before_probe(case: str) -> None:
    """Membership and capability are validated first and synchronously: each refusal is the
    EXISTING precise exception and reason code, with no probe and no business I/O (Codex
    202256Z pin 1, 211059Z item 1, 212251Z oracle pin)."""

    async def body() -> tuple[BaseException, list[str]]:
        from haloflow.m01.activation import ActivationPolicy
        from haloflow.m01.gateway import TenantRepositoryHandle

        conn = FakeConnection()
        catalog = _catalog()
        protected_key = _keys(catalog)[0]
        caps = frozenset(s.required_capability for s in catalog.catalog.statements())
        allow_writes = True
        if case == "missing-capability":
            caps = caps - {"m05.followup.read"}
        if case == "write-in-read":
            protected_key, allow_writes = "m05.followup.apply", False
        handle = TenantRepositoryHandle(conn, catalog.catalog, allow_writes=allow_writes,
                                        capabilities=caps,
                                        activation_policy=ActivationPolicy.ENFORCE,
                                        activation_protected=catalog.activation_protected)
        key = "m05.no.such.statement" if case == "unknown-statement" else protected_key
        try:
            await handle.fetch_all(key, {"x": None})
        except Exception as error:  # noqa: BLE001 - asserted precisely below
            return error, conn.queries
        raise AssertionError("statement was not refused")

    from haloflow.m01 import errors

    error, queries = asyncio.run(body())
    expected_class, expected_code = U10_CASES[case]
    assert type(error) is getattr(errors, expected_class), type(error)
    assert error.reason_code == expected_code  # type: ignore[attr-defined]
    assert queries == []


def test_sc_u07_not_enforced_never_probes() -> None:
    async def body() -> list[str]:
        conn = FakeConnection([({"schema_version": 1, "active": False}, "read committed")])
        handle = _handle(conn, policy="NOT_ENFORCED")
        await handle.fetch_all(_keys(_catalog())[0], {"x": None})
        return conn.queries

    assert sc.PROBE_SQL not in asyncio.run(body())
