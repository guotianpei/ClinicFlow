"""Shared M01 test fixtures.

Helpers are exposed as fixtures rather than imported across test modules. The
earlier `from conftest import ...` worked only because pytest's prepend import
mode puts this directory on sys.path, which is an avoidable dependency on
collection mechanics.
"""

import copy
import importlib.util
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import pytest
import recording
import typed_recording
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from alembic import command
from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    PrincipalKind,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.provisioning import (
    AUDIT_PROJECTOR_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    RUNTIME_ROLE,
)
from haloflow.m01.provisioning.runner import TenantMigrationRunner
from haloflow.m01.resolver import LifecycleState, TenantRegistryRecord, TenantResolver

FIXTURE_EXECUTION_ID = uuid5(NAMESPACE_URL, "haloflow-test:fixture")
FIXTURE_CORRELATION_ID = uuid5(NAMESPACE_URL, "haloflow-test:fixture-correlation")

# Login shims. Every M01 database role is NOLOGIN by design, so a test that wants
# to act as one connects through a LOGIN role that is a member of it -- which is
# also how the application connects in production. The session then issues
# `SET ROLE`, so objects are owned by the group role rather than the shim.
TEST_ROLE_PASSWORD = "m01-local-test-only"
TEST_LOGIN_ROLES: dict[str, str] = {
    RUNTIME_ROLE: "haloflow_test_runtime_login",
    PROVISIONER_ROLE: "haloflow_test_provisioner_login",
    MIGRATOR_ROLE: "haloflow_test_migrator_login",
    AUDIT_PROJECTOR_ROLE: "haloflow_test_audit_projector_login",
}


class SingleTenantControlStore:
    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        if tenant_id != "clinic-a":
            return None
        return TenantRegistryRecord(
            tenant_id="clinic-a",
            schema_key="tenant_aaaaaaaa",
            lifecycle_state=LifecycleState.ACTIVE,
            schema_version=1,
        )


class ConfigurableControlStore:
    def __init__(self, record: TenantRegistryRecord | None) -> None:
        self._record = record

    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None:
        return self._record


def _principal_with(*capabilities: str) -> Principal:
    return Principal(
        kind=PrincipalKind.WORKLOAD,
        id="test-worker",
        auth_method="test",
        authorized_tenant_ids=frozenset({"clinic-a"}),
        capabilities=frozenset(capabilities),
    )


@pytest.fixture
def execution_id() -> UUID:
    return FIXTURE_EXECUTION_ID


@pytest.fixture
def correlation_id() -> UUID:
    return FIXTURE_CORRELATION_ID


@pytest.fixture
def principal_with() -> Callable[..., Principal]:
    return _principal_with


@pytest.fixture
def control_store() -> SingleTenantControlStore:
    return SingleTenantControlStore()


@pytest.fixture
def make_control_store() -> Callable[[TenantRegistryRecord | None], ConfigurableControlStore]:
    return ConfigurableControlStore


@pytest.fixture
def make_resolver() -> Callable[..., TenantResolver]:
    def _make(store: object | None = None, *, ttl_seconds: int = 60) -> TenantResolver:
        return TenantResolver(
            store or SingleTenantControlStore(),  # type: ignore[arg-type]
            supported_schema_versions=range(1, 2),
            context_ttl=timedelta(seconds=ttl_seconds),
            clock=lambda: datetime.now(UTC),
        )

    return _make


@pytest.fixture
def resolve(
    make_resolver: Callable[..., TenantResolver],
) -> Callable[..., Awaitable[TenantContext]]:
    """Resolve a context with sensible defaults; override any argument by keyword."""

    async def _resolve(*, store: object | None = None, **overrides: Any) -> TenantContext:
        kwargs: dict[str, Any] = {
            "principal": _principal_with("appointments:read"),
            "tenant_hint": "clinic-a",
            "purpose": "treatment",
            "capabilities": frozenset({"appointments:read"}),
            "source": TrustedSource.WORKER,
            "execution_id": FIXTURE_EXECUTION_ID,
            "correlation_id": FIXTURE_CORRELATION_ID,
            "correlation_source": CorrelationSource.TRUSTED_INFRASTRUCTURE,
        }
        kwargs.update(overrides)
        return await make_resolver(store).resolve(**kwargs)

    return _resolve


async def _resolve_context(*, expired: bool = False) -> TenantContext:
    now = datetime.now(UTC)
    resolver = TenantResolver(
        SingleTenantControlStore(),
        supported_schema_versions=range(1, 2),
        context_ttl=timedelta(seconds=-1 if expired else 60),
        clock=lambda: now,
    )
    return await resolver.resolve(
        principal=_principal_with("appointments:read"),
        tenant_hint="clinic-a",
        purpose="treatment",
        capabilities=frozenset({"appointments:read"}),
        source=TrustedSource.WORKER,
        execution_id=FIXTURE_EXECUTION_ID,
        correlation_id=FIXTURE_CORRELATION_ID,
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
    )


@pytest.fixture
async def active_context() -> TenantContext:
    return await _resolve_context()


@pytest.fixture
async def expired_context() -> TenantContext:
    return await _resolve_context(expired=True)


# ---------------------------------------------------------------------------
# PostgreSQL fixtures shared by the gateway and provisioning suites.
#
# These are fixtures rather than importable helpers for the reason at the top of
# this file: `from conftest import ...` works only because of pytest's prepend
# import mode, and that is an avoidable dependency on collection mechanics.
# ---------------------------------------------------------------------------


def _database_url_from(params: dict[str, object], dbname: str) -> str:
    """Build a postgresql:// URL, which is what alembic/env.py can rewrite."""

    user = params.get("user") or "postgres"
    password = params.get("password")
    credentials = f"{user}:{password}" if password else f"{user}"
    host = params.get("host") or "127.0.0.1"
    port = params.get("port") or 5432
    return f"postgresql://{credentials}@{host}:{port}/{dbname}"


def _apply_migrations_to(conninfo: str, revision: str = "head") -> None:
    previous = os.environ.get("HALOFLOW_MIGRATION_DATABASE_URL")
    os.environ["HALOFLOW_MIGRATION_DATABASE_URL"] = conninfo
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        if previous is None:
            os.environ.pop("HALOFLOW_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["HALOFLOW_MIGRATION_DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def test_conninfo() -> str:
    conninfo = os.getenv("HALOFLOW_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("HALOFLOW_TEST_DATABASE_URL is not configured")
    return conninfo


@pytest.fixture(scope="session")
def database_url() -> Callable[[dict[str, object], str], str]:
    return _database_url_from


@pytest.fixture(scope="session")
def apply_migrations() -> Callable[..., None]:
    return _apply_migrations_to


@pytest.fixture(scope="session")
def migrated_database(test_conninfo: str) -> str:
    """The test database at `head`, with the server version checked once.

    Both PostgreSQL suites depend on this, and Alembic is a no-op when already at
    head, so it is safe for whichever runs first to do the work.
    """

    _apply_migrations_to(test_conninfo)
    with psycopg.connect(test_conninfo, autocommit=True) as conn:
        version = int(conn.execute("SHOW server_version_num").fetchone()[0])  # type: ignore[index]
        database = conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
    if version < 170000:
        pytest.fail(f"M01 tests require PostgreSQL 17+, found {version}")
    if not str(database).startswith("haloflow_test"):
        pytest.fail("Refusing to initialize a database not named haloflow_test*")
    return test_conninfo


@pytest.fixture(scope="session")
def role_logins(migrated_database: str) -> dict[str, str]:
    """Conninfo per M01 role, reached through a LOGIN member of that role.

    Idempotent, so two session-scoped harnesses can both depend on it.
    """

    with psycopg.connect(migrated_database, autocommit=True) as conn:
        for group_role, login_role in TEST_LOGIN_ROLES.items():
            exists = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (login_role,)
            ).fetchone()
            if not (exists and exists[0]):
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                        sql.Identifier(login_role),
                        sql.Literal(TEST_ROLE_PASSWORD),
                        sql.Identifier(group_role),
                    )
                )
            conn.execute(
                sql.SQL("ALTER ROLE {} SET search_path = ''").format(sql.Identifier(login_role))
            )

    conninfos: dict[str, str] = {}
    for group_role, login_role in TEST_LOGIN_ROLES.items():
        params = conninfo_to_dict(migrated_database)
        params.update(user=login_role, password=TEST_ROLE_PASSWORD)
        conninfos[group_role] = make_conninfo(**params)
    return conninfos


def _reset_tenants_in(conninfo: str, tenant_ids: Sequence[str], schema_keys: Sequence[str]) -> None:
    """Remove test tenants and their schemas so a suite starts from nothing.

    Four tables reference `shared.tenants`, and two of them --
    `tenant_state_history` and `access_audit_log` -- are append-only by trigger,
    so a plain DELETE cannot clear a tenant the provisioner has activated. Both
    triggers are disabled for the duration, as the table owner, and re-enabled in
    a `finally`. This is a harness escape hatch and deliberately the only one: no
    production role can do it, and TC-E23 and TC-E25 assert that.
    """

    append_only = (
        ("shared.tenant_state_history", "tenant_state_history_append_only"),
        ("shared.access_audit_log", "access_audit_log_append_only"),
    )
    referencing = (
        "shared.schema_migrations",
        "shared.tenant_state_history",
        "shared.access_audit_log",
        "shared.isolation_alerts",
    )

    with psycopg.connect(conninfo, autocommit=True) as conn:
        for schema_key in schema_keys:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_key))
            )
        for table, trigger in append_only:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        try:
            for table in referencing:
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(%s)", (list(tenant_ids),)
                )
            conn.execute(
                "DELETE FROM shared.tenants WHERE tenant_id = ANY(%s)", (list(tenant_ids),)
            )
        finally:
            for table, trigger in append_only:
                conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


@pytest.fixture(scope="session")
def reset_tenants() -> Callable[[str, Sequence[str], Sequence[str]], None]:
    return _reset_tenants_in


# ---- conftest-addition.py ----

# Stage 1 (`assert_execution_roles_safe`) runs on every runner entry and makes
# two reads that no test is about: the controlled membership graph, and the
# migrator's `rolcreaterole`. They are answered here so a test declares only the
# answers it is actually reasoning about. A test that wants stage 1 to REFUSE
# supplies a conflicting answer explicitly rather than relying on omission.
STAGE_ONE_ANSWERS = (recording.NO_CONTROLLED_EDGES, recording.MIGRATOR_SAFE)


@pytest.fixture
def harness():
    """The recording harness module, so test modules import nothing across modules.

    `conftest.py` is the one place that imports `recording`, which is where
    pytest's prepend-import mechanic is meant to be used. Tests reach the
    declarative pieces -- `harness.Answer`, `harness.ledger_absent()`,
    `harness.UnscriptedQuery` -- through this fixture.
    """

    return recording


@pytest.fixture
def shared_clock():
    """One monotonic counter per test, shared by every connection it builds.

    `apply` drives two connections. Without a shared clock their traces cannot
    be ordered against each other, and an assertion built from two independent
    traces would pass a runner that released the lock before doing any work.
    """

    return recording.SharedClock()


@pytest.fixture
def recording_connection(shared_clock):
    """Build a `RecordingConnection` with stage 1's answers plus the test's.

    Returns the factory, not a connection: a test driving `apply` needs two
    connections and must be able to ask for them separately. Every connection
    it builds shares the test's clock.
    """

    def build(*answers, name: str = "connection") -> recording.RecordingConnection:
        return recording.RecordingConnection(
            answers=[*STAGE_ONE_ANSWERS, *answers], name=name, clock=shared_clock
        )

    return build


@pytest.fixture
def migration_driver(recording_connection):
    """Build the real `TenantMigrationRunner` over recording connections.

    `connect` is a production constructor parameter, so this wires a test
    connection into the shipping runner without patching, wrapping or
    monkeypatching anything. `manifest` is likewise a production parameter.

    Returns `(runner, connections)` where `connections` is the tuple handed to
    the factory in order -- for `apply_within_lock` that is one connection, for
    `apply` it is the lock connection then the work connection.
    """

    def build(registry, *answer_sets, manifest=None, names=()):
        labels = tuple(names) or tuple(f"c{index}" for index in range(len(answer_sets)))
        connections = tuple(
            recording_connection(*answers, name=label)
            for answers, label in zip(answer_sets, labels, strict=True)
        )
        runner = TenantMigrationRunner(
            recording.connection_factory(*connections),
            registry,
            **({"manifest": manifest} if manifest is not None else {}),
        )
        return runner, connections

    return build


# ---- conftest-addition-v12.py ----

_SEAM_SPEC = importlib.util.spec_from_file_location(
    "cp2_typed_plan_seam", Path(__file__).parent / "support" / "typed_plan_seam.py"
)
assert _SEAM_SPEC is not None and _SEAM_SPEC.loader is not None
_SEAM = importlib.util.module_from_spec(_SEAM_SPEC)
_SEAM_SPEC.loader.exec_module(_SEAM)


_POLICY_FIXTURES = Path(__file__).parent / "fixtures" / "function_policy"


def _derive_typed_payloads() -> dict:
    """Every payload the typed cases use, derived from the FROZEN CP1 fixtures.

    Nothing here is authored from scratch. Each entry is a CP1 variant, or a CP1
    variant with the named single edit. Whether the frozen checker admits or
    refuses each one is asserted by `test_typed_plan_checksum.py`'s fixture
    controls, which run today -- so a typed case that later fails cannot be
    blamed on a fixture nobody checked.
    """

    variants = {
        variant["case_id"]: variant["payload"]
        for variant in json.loads((_POLICY_FIXTURES / "sql-fixtures.json").read_text())[
            "variants"
        ]
    }

    def renamed(payload: dict, migration_id: str, function_name: str) -> dict:
        # A second, distinct typed unit: new migration id AND new function name,
        # edited in the three places the name occurs. The body is untouched, so
        # `body_sha256` stays valid.
        result = copy.deepcopy(payload)
        result["migration_id"] = migration_id
        result["template"] = result["template"].replace("m02_annex_probe", function_name)
        result["policy"]["functions"][0]["name"] = function_name
        result["verification"]["functions"][0]["name"] = function_name
        return result

    first = copy.deepcopy(variants["POS-quoted-words"])
    annex = copy.deepcopy(first)
    # TP-24a / B-role alternate. The declared owner IS the execution role, so both
    # move together; an annex role with an m02_owner owner would describe a
    # function the D-layer verifier must then refuse.
    annex["execution_role"] = "haloflow_m02_annex"
    annex["verification"]["functions"][0]["owner"] = "haloflow_m02_annex"
    payload_nul = copy.deepcopy(first)
    # CP1 `test_nul_intake[template]`'s exact edit.
    payload_nul["template"] = payload_nul["template"].replace(
        "CREATE FUNCTION", "CREATE\0 FUNCTION", 1
    )
    return {
        "first": first,
        "second": renamed(first, "t003_annex_probe", "m02_annex_second"),
        "second_body_drift": renamed(
            variants["A-body-drift"], "t003_annex_probe", "m02_annex_second"
        ),
        "annex": annex,
        "body_drift": copy.deepcopy(variants["A-body-drift"]),
        "table": copy.deepcopy(variants["A-table"]),
        "unknown_select": copy.deepcopy(variants["A-unknown-select"]),
        "payload_nul": payload_nul,
    }


@pytest.fixture
def typed_payloads():
    """A fresh deep copy of every derived payload, so no test mutates another's."""

    return _derive_typed_payloads()


@pytest.fixture
def seam():
    """The typed-plan vocabulary. See `support/typed_plan_seam.py`."""

    return _SEAM


@pytest.fixture
def typed_harness():
    """The typed harness extension module (`TypedConnection`, `Fault`, `Hook`, ...)."""

    return typed_recording


@pytest.fixture
def call_spy(shared_clock):
    """A `CallSpy` on the test's shared clock, so its calls order against the trace."""

    def build() -> typed_recording.CallSpy:
        return typed_recording.CallSpy(clock=shared_clock)

    return build


@pytest.fixture
def typed_driver(shared_clock):
    """The real `TenantMigrationRunner` over `TypedConnection`s.

    Same shape as v11's `migration_driver`: `connect` and `manifest` are
    production constructor parameters, nothing is patched. Stage 1's two reads are
    answered first, exactly as there, so a test declares only the answers it is
    reasoning about.

    Returns `(runner, connections)`. Each answer set is a tuple; a connection that
    needs faults or hooks gets them by mutating `connection.faults` /
    `connection.hooks` before the runner is driven.
    """

    def build(registry, *answer_sets, manifest=None, names=()):
        labels = tuple(names) or tuple(f"c{index}" for index in range(len(answer_sets)))
        connections = tuple(
            typed_recording.TypedConnection(
                answers=[*STAGE_ONE_ANSWERS, *answers],
                name=label,
                clock=shared_clock,
            )
            for answers, label in zip(answer_sets, labels, strict=True)
        )
        runner = TenantMigrationRunner(
            recording.connection_factory(*connections),
            registry,
            **({"manifest": manifest} if manifest is not None else {}),
        )
        return runner, connections

    return build
